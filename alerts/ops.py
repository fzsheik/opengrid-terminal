"""Operator alerts for execution safety: orphans, termination failures, ambiguous launches, kill switches.

Every alert is recorded as a quality incident (so it is on /ops whatever else happens) and, when
OPS_ALERT_WEBHOOK_URL is set, delivered to that webhook signed with OPS_ALERT_WEBHOOK_SECRET using
the same SSRF-safe, pinned delivery as customer alerts (alerts/notifier.py). A Slack or PagerDuty
incoming-webhook URL works as the target.

Delivery never raises: an alert path that fails must not break reconciliation or termination.

    from alerts import ops
    ops.alert("orphan_detected", "Orphan GPU instance at lambda", severity="major",
              provider="lambda", detail={"instance_id": "...", "hourly_cost": 2.49})
"""

import logging
from datetime import datetime, timezone

from config import settings

log = logging.getLogger(__name__)

# Kinds the execution layer raises. Free-form kinds are accepted too; these are the documented ones.
KINDS = {
    "orphan_detected": "an instance OpenGrid launched (or may have launched) has no live deployment",
    "termination_failed": "a terminate did not complete; the instance may still be billing",
    "launch_unknown": "a launch had an ambiguous outcome; reconciliation must resolve it",
    "credentials_unavailable": "a live deployment's pinned credentials can no longer be used",
    "reconciliation_failed": "a reconciliation run errored",
    "kill_switch": "live provisioning was stopped (global or per provider)",
    "deadline_terminate": "a deployment hit its runtime deadline and was auto-terminated",
    # Cost-exposure alerts (exposure() below): deduplicated, re-escalated, resolved only on evidence.
    "resource_state_unknown": "a resource that may bill has been in an unknown/uncertain state too long",
    "past_deadline": "a deployment is past its runtime deadline and still not confirmed terminated",
    "suspected_orphan": "a provider instance may bill with no live OpenGrid deployment",
    "provider_api_unavailable": "a provider API cannot be read while OpenGrid has active deployments there",
    "overspend": "spend exceeds the quote by more than settings.alert_overspend_pct",
    "active_without_billing": "a live, billable deployment has no usage slice for more than 2 intervals",
    "unowned_provider_resource": "an og-* provider resource exists with no OpenGrid ownership record",
    "resource_delete_failed": "a temporary provider resource (ssh key) could not be deleted",
    "unexpected_ssh_key": "an instance lists SSH keys OpenGrid did not request (provider-forced access?)",
}

# Kinds whose payload must carry the cost-exposure fields.
EXPOSURE_KINDS = ("resource_state_unknown", "termination_failed", "past_deadline", "suspected_orphan",
                  "provider_api_unavailable", "overspend", "active_without_billing", "unowned_provider_resource",
                  "resource_delete_failed")
REQUIRED_FIELDS = ("deployment_id", "provider", "account_id", "est_hourly_exposure_usd", "time_in_state",
                   "suggested_action")


def channel_configured() -> bool:
    """True when ops alerts leave the app (a webhook is configured); the /ops incident feed always works."""
    return bool(settings.ops_alert_webhook_url and settings.ops_alert_webhook_secret)


ops_channel_configured = channel_configured  # the name routing/checklist.py looks for


def alert(kind: str, title: str, *, severity: str = "major", provider: str | None = None,
          detail: dict | None = None, dedupe: str | None = None) -> dict:
    """Record + deliver one ops alert. Returns {"recorded": bool, "delivered": bool}."""
    out = {"recorded": False, "delivered": False}
    detail = {**(detail or {}), "title": title}
    try:
        from quality import incidents

        incidents.record_now(f"exec_{kind}", provider=provider, severity=severity, detail=detail,
                             key=incidents.key_for(f"exec_{kind}", provider, None, dedupe or title[:120]))
        out["recorded"] = True
    except Exception:  # noqa: BLE001 - never break the caller
        log.exception("ops alert %s could not be recorded", kind)
    if channel_configured():
        try:
            from alerts import notifier

            payload = {"source": "opengrid", "kind": kind, "severity": severity, "title": title,
                       "provider": provider, "detail": detail, "at": datetime.now(timezone.utc).isoformat(),
                       "text": f"[OpenGrid {severity}] {title}"}  # "text" makes Slack webhooks render it
            via, _status = notifier.deliver([{"type": "webhook", "url": settings.ops_alert_webhook_url}],
                                            payload, settings.ops_alert_webhook_secret)
            out["delivered"] = "webhook" in via
        except Exception:  # noqa: BLE001
            log.exception("ops alert %s could not be delivered", kind)
    log.warning("ops alert %s: %s", kind, title, extra={"ops_alert": kind, "provider": provider})
    return out


# --------------------------------------------------------------------------
# Cost-exposure alerts: one open condition per (kind, subject), re-escalated, resolved only on evidence
# --------------------------------------------------------------------------

def _human(seconds):
    if seconds is None:
        return None
    m = int(seconds // 60)
    return f"{m // 60}h{m % 60:02d}m" if m >= 60 else f"{m}m"


def exposure(kind: str, subject: str, title: str, *, deployment_id: str | None, provider: str | None,
             account_id: int | None, est_hourly_exposure_usd: float | None, time_in_state_seconds: float | None,
             suggested_action: str, severity: str = "major", detail: dict | None = None,
             now: datetime | None = None) -> dict:
    """Raise (or keep raising) one cost-exposure condition. Sent the first time, then again every
    settings.alert_reescalate_minutes while it stays open; a resolved condition that recurs re-opens.
    Returns {"sent": bool, "id": int, "sent_count": int}. Never raises."""
    from datetime import timedelta
    from decimal import Decimal

    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert

    import normalize
    from store.reconcile import OpsAlertState

    now = now or datetime.now(timezone.utc)
    est = None if est_hourly_exposure_usd is None else round(float(est_hourly_exposure_usd), 4)
    if time_in_state_seconds is not None:
        time_in_state_seconds = max(0.0, float(time_in_state_seconds))
    payload = {"kind": kind, "deployment_id": deployment_id, "provider": provider, "account_id": account_id,
               "est_hourly_exposure_usd": est, "time_in_state": _human(time_in_state_seconds),
               "time_in_state_seconds": None if time_in_state_seconds is None else max(0, int(time_in_state_seconds)),
               "suggested_action": suggested_action, **(detail or {})}
    every = timedelta(minutes=max(0, int(getattr(settings, "alert_reescalate_minutes", 30))))
    subject = subject[:160]
    try:
        with normalize.SessionLocal.begin() as s:
            s.execute(insert(OpsAlertState).values(
                kind=kind, subject=subject, deployment_id=deployment_id, provider=provider, account_id=account_id,
                status="open", first_at=now, last_seen_at=now, sent_count=0,
                est_hourly_exposure_usd=None if est is None else Decimal(str(est)), payload=payload, updated_at=now,
            ).on_conflict_do_nothing(constraint="uq_ops_alert_kind_subject"))
            row = s.scalars(select(OpsAlertState).where(OpsAlertState.kind == kind, OpsAlertState.subject == subject)
                            .with_for_update()).one()
            if row.status == "resolved":       # it came back: a new episode
                row.status, row.first_at, row.resolved_at, row.sent_count, row.last_sent_at = "open", now, None, 0, None
            row.last_seen_at, row.updated_at, row.payload = now, now, payload
            row.est_hourly_exposure_usd = None if est is None else Decimal(str(est))
            due = row.last_sent_at is None or now - row.last_sent_at >= every
            if due:
                row.last_sent_at = now
                row.sent_count = (row.sent_count or 0) + 1
            rid, n = row.id, row.sent_count
    except Exception:  # noqa: BLE001 - alerting never breaks the caller; fall back to a plain alert
        log.exception("exposure alert state for %s %s failed", kind, subject)
        alert(kind, title, severity=severity, provider=provider, detail=payload, dedupe=subject)
        return {"sent": True, "id": None, "sent_count": None}
    if due:
        payload["escalation"] = n
        alert(kind, (f"[re-escalation {n}] " if n > 1 else "") + title, severity=severity, provider=provider,
              detail=payload, dedupe=f"{subject}#{n}")
    return {"sent": due, "id": rid, "sent_count": n}


def resolve(kind: str, subject: str, evidence: dict, *, by: str = "system") -> bool:
    """Close an open condition ON EVIDENCE (a non-empty dict saying what proves it is over). Returns True
    when something was resolved. Without evidence nothing is resolved (never silently)."""
    if not evidence:
        raise ValueError("resolving a cost-exposure alert needs evidence")
    from sqlalchemy import select

    import normalize
    from store.reconcile import OpsAlertState

    now = datetime.now(timezone.utc)
    try:
        with normalize.SessionLocal.begin() as s:
            row = s.scalars(select(OpsAlertState).where(OpsAlertState.kind == kind,
                                                        OpsAlertState.subject == subject[:160],
                                                        OpsAlertState.status == "open").with_for_update()).first()
            if row is None:
                return False
            row.status, row.resolved_at, row.updated_at = "resolved", now, now
            row.resolution = {"by": by, "at": now.isoformat(), **evidence}
    except Exception:  # noqa: BLE001
        log.exception("resolving %s %s failed", kind, subject)
        return False
    log.warning("ops alert %s resolved: %s", kind, subject, extra={"ops_alert": kind})
    return True


def open_exposures(kind: str | None = None) -> list[dict]:
    """Open cost-exposure conditions (admin view)."""
    from sqlalchemy import select

    import normalize
    from store.reconcile import OpsAlertState

    with normalize.SessionLocal() as s:
        q = select(OpsAlertState).where(OpsAlertState.status == "open").order_by(OpsAlertState.first_at)
        if kind:
            q = q.where(OpsAlertState.kind == kind)
        return [{"id": r.id, "subject": r.subject, "first_at": r.first_at.isoformat(),
                 "last_sent_at": r.last_sent_at.isoformat() if r.last_sent_at else None, "sent_count": r.sent_count,
                 **(r.payload or {}), "kind": r.kind} for r in s.scalars(q)]


def open_subjects(kind: str) -> list[str]:
    from sqlalchemy import select

    import normalize
    from store.reconcile import OpsAlertState

    with normalize.SessionLocal() as s:
        return list(s.scalars(select(OpsAlertState.subject).where(OpsAlertState.kind == kind,
                                                                  OpsAlertState.status == "open")))
