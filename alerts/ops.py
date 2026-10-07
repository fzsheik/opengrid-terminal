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
}


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
