"""Background job: observe every live deployment with its PINNED credentials, and meter usage incrementally.

Each tick (settings.tracker_interval_seconds, default 60):
  1. poll   every live deployment that has an instance id: adapter.status() with exactly the credential it
            was launched with (deployments.adapter_for -> credentials_for), applied through
            deployments.observe() -> transition() (the core's state machine: running only when the provider
            says so, one not_found is never termination, stale reads ignored). Uptime and interruptions
            are kept by the state machine. The provider's own end time (when it reports one) is passed as
            evidence so terminated_at is the provider-confirmed time.
            Deployments without an instance id (launch_unknown, provider_timeout, crash mid-provision) are
            NOT polled here: routing/reconcile.py resolves them by name first.
  2. meter  hour slices per deployment (billing.usage.record_usage_slice), idempotent per
            (deployment, period_start). Closed hours only, and only up to the last successful provider
            observation, while live; the final partial hour once terminated. States billed:
                running / degraded* / stopping / terminating / termination_failed / orphan_suspected /
                credentials_unavailable (after the instance first ran)      -> GPU rate
                stopped                                                     -> per CAPABILITIES.stopped_billing
                provisioning / provider_timeout / launch_unknown before first running -> recorded, not billed
            (*degraded is not billed where the provider documents no charge in error states: Hyperstack.)
            End time = the provider-confirmed termination time when the provider gives one, else the first
            observation of terminated (the final slice is flagged end_estimated).
  3. retry  usage records that failed to write, and cost reconciliation of terminated deployments.

Every stopped-time decision records its basis (the provider's documented stopped_billing) on the slice.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select

import normalize
from config import settings
from jobs import job
from routing import adapters, deployments, transactions
from store.reconcile import DeploymentWatch, UsageSlice
from store.routing import Deployment, DeploymentEvent, ExecutionRecord

log = logging.getLogger(__name__)

HOUR = timedelta(hours=1)
# States in which an instance that has run is billed at the GPU rate.
BILLED_STATES = ("running", "degraded", "stopping", "terminating", "termination_failed", "orphan_suspected",
                 "credentials_unavailable")
STOPPED_STATES = ("stopped",)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Per-deployment watch row (shared with routing/reconcile.py)
# --------------------------------------------------------------------------

def watch_row(s, dep_id: str, *, lock: bool = True) -> DeploymentWatch:
    w = s.get(DeploymentWatch, dep_id, with_for_update=lock)
    if w is None:
        from sqlalchemy.dialects.postgresql import insert
        s.execute(insert(DeploymentWatch).values(deployment_id=dep_id, consecutive_errors=0, not_found_count=0,
                                                 terminate_attempts=0, metering_complete=False, updated_at=_now())
                  .on_conflict_do_nothing())
        w = s.get(DeploymentWatch, dep_id, with_for_update=lock)
    return w


# Our alert kinds -> the ops alert kinds (alerts/ops.py). Anything else is logged + recorded only.
OPS_KINDS = {"orphan": "orphan_detected", "termination_failed": "termination_failed",
             "launch_unresolved": "launch_unknown", "credentials_unavailable": "credentials_unavailable",
             "reconciliation_failed": "reconciliation_failed", "deadline_terminate": "deadline_terminate",
             "provider_terminated": "provider_terminated", "duplicate_launch": "orphan_detected",
             "rejected_but_created": "orphan_detected", "boot_timeout": "boot_timeout"}
ALERT_REPEAT = timedelta(hours=6)     # the same kind for the same deployment is not re-sent sooner


def alert(kind: str, subject: str | None, message: str, *, dep_id: str | None = None, provider: str | None = None,
          detail: dict | None = None, severity: str = "major") -> None:
    """Operator alert: the EXECUTION_ALERT log marker, alerts.ops.alert (incident on /ops + signed webhook)
    for the kinds in OPS_KINDS, and the deployment's watch row (last 20). Deduplicated per deployment and
    kind (ALERT_REPEAT) so a stuck item does not page every run; callers dedupe non-deployment subjects."""
    log.error("EXECUTION_ALERT %s %s: %s", kind, subject or "", message)
    send = True
    if dep_id:
        try:
            with normalize.SessionLocal.begin() as s:
                w = watch_row(s, dep_id)
                prev = [x for x in (w.alerts or []) if x.get("kind") == kind]
                if prev:
                    try:
                        send = _now() - datetime.fromisoformat(prev[0]["at"]) >= ALERT_REPEAT
                    except (KeyError, ValueError):
                        send = True
                if send:
                    w.alerts = ([{"kind": kind, "at": _now().isoformat(), "message": message[:300]}]
                                + list(w.alerts or []))[:20]
                    w.updated_at = _now()
        except Exception:  # noqa: BLE001
            log.exception("could not record alert on %s", dep_id)
    if not send or kind not in OPS_KINDS:
        return
    try:
        from alerts import ops
        ops.alert(OPS_KINDS[kind], message[:300], severity=severity, provider=provider,
                  detail={"subject": subject, "deployment_id": dep_id, "our_kind": kind, **(detail or {})},
                  dedupe=f"{kind}:{dep_id or subject}")
    except Exception:  # noqa: BLE001 - alerting never breaks reconciliation
        log.exception("ops alert %s failed", kind)


def caps(provider: str | None):
    cls = adapters.get(provider or "")
    return None if cls is None else cls.CAPABILITIES


def stopped_billing(provider: str | None) -> str:
    c = caps(provider)
    v = (c.stopped_billing[0] if c is not None else "UNKNOWN") or "UNKNOWN"
    # 'n/a' (no stop) and unknown are treated as full billing: never assume a saving the provider did not document.
    return v if v in ("full", "storage_only", "none") else "full"


def error_state_billed(provider: str | None) -> bool:
    cls = adapters.get(provider or "")
    return True if cls is None else bool(getattr(cls, "ERROR_STATE_BILLED", True))


# --------------------------------------------------------------------------
# 1. Polling
# --------------------------------------------------------------------------

def poll(dep_id: str) -> dict:
    """One status read with the pinned credentials, applied through the core's observe()."""
    d = deployments.load_row(dep_id)
    if d is None or d.status not in deployments.LIVE_STATES:
        return {"polled": False, "reason": "not live"}
    if not d.provider_instance_id:
        return {"polled": False, "reason": "no instance id: resolved by reconciliation (find by name)"}
    try:
        a = deployments.adapter_for(d)
    except deployments.CredentialsUnavailable as exc:
        deployments.mark_credentials_unavailable(dep_id, exc)
        _note_error(dep_id, f"credentials_unavailable: {exc.message}")
        return {"polled": False, "reason": "credentials_unavailable"}
    except Exception as exc:  # noqa: BLE001 - no adapter
        return {"polled": False, "reason": str(getattr(exc, "detail", exc))}
    checked_at = _now()
    try:
        out, _ = deployments.provider_call("status", a.status, d.provider_instance_id, provider=d.provider,
                                           deployment_id=dep_id, route_request_id=d.route_request_id)
        st = deployments.as_instance_state(out, d.provider_instance_id)
        if d.purpose == "validation" and st.state == "running":
            _validation_checks(a, d)
    finally:
        a.close()
    extra = {"ended_at": st.ended_at.isoformat()} if st.ended_at else None
    if st.state == "terminated":
        # Before observe(): the transition bills immediately, and metering reads the end-time basis.
        with normalize.SessionLocal.begin() as s:
            w = watch_row(s, dep_id)
            if w.ended_at is None:
                w.ended_at = st.ended_at or checked_at
                w.ended_basis = "provider_reported" if st.ended_at else "first_observed"
            w.last_observed_at, w.last_observed_state, w.updated_at = checked_at, st.state, _now()
    r = deployments.observe(dep_id, st, checked_at=checked_at, extra_evidence=extra)
    with normalize.SessionLocal.begin() as s:
        w = watch_row(s, dep_id)
        if st.state == "unknown":
            w.consecutive_errors = (w.consecutive_errors or 0) + 1
            w.last_error = f"{st.error_kind}: {(st.message or '')[:300]}"
        else:
            w.consecutive_errors = 0
            w.last_observed_at, w.last_observed_state = checked_at, st.state
            w.last_provider_status = (st.provider_status or "")[:64] or None
            if st.state == "running":
                w.last_running_at = checked_at
        errors = w.consecutive_errors
        w.updated_at = _now()
    if errors and errors in (5, 30, 120):
        alert("status_errors", dep_id, f"{errors} consecutive failed status reads ({st.error_kind})", dep_id=dep_id,
              provider=d.provider)
    return {"polled": True, "state": st.state, **r}


def _note_error(dep_id: str, msg: str) -> None:
    with normalize.SessionLocal.begin() as s:
        w = watch_row(s, dep_id)
        w.consecutive_errors = (w.consecutive_errors or 0) + 1
        w.last_error = msg[:500]
        w.updated_at = _now()


def _validation_checks(a, d: Deployment) -> None:
    """For a validation deployment observed running: prove find_instance(name) and list_instances() see it."""
    with normalize.SessionLocal() as s:
        w = s.get(DeploymentWatch, d.deployment_id)
        v = dict((w.validation if w else None) or {})
    if v.get("find_instance", {}).get("ok") and v.get("list_instances", {}).get("ok"):
        return
    now = _now().isoformat()
    try:
        f = a.find_instance(d.client_name)
        v["find_instance"] = {"ok": bool(f and str(f.instance_id) == str(d.provider_instance_id)), "at": now,
                              "name": d.client_name, "found_id": f.instance_id if f else None}
    except Exception as exc:  # noqa: BLE001
        v["find_instance"] = {"ok": False, "at": now, "error": str(getattr(exc, "message", exc))[:300]}
    try:
        ids = [str(x.instance_id) for x in a.list_instances()]
        v["list_instances"] = {"ok": str(d.provider_instance_id) in ids, "at": now, "listed": len(ids)}
    except Exception as exc:  # noqa: BLE001
        v["list_instances"] = {"ok": False, "at": now, "error": str(getattr(exc, "message", exc))[:300]}
    with normalize.SessionLocal.begin() as s:
        w = watch_row(s, d.deployment_id)
        w.validation = v
        w.updated_at = _now()


# --------------------------------------------------------------------------
# 2. Metering
# --------------------------------------------------------------------------

def _floor_hour(t: datetime) -> datetime:
    return t.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def segments(events: list[tuple[datetime, str]], end: datetime, *, provider: str | None) -> list[tuple]:
    """[(t0, t1, cls)] with cls 'run' | 'stopped' | 'unbilled', from the ordered status events, clipped at end.

    Time before the instance is first observed running is never billed; degraded time is billed unless the
    provider documents no charge in error states."""
    out, ran = [], False
    err_billed = error_state_billed(provider)
    for i, (t, status) in enumerate(events):
        t1 = events[i + 1][0] if i + 1 < len(events) else end
        if status == "running":
            ran = True
        if status == "terminated" or t >= end:
            break
        t1 = min(t1, end)
        if t1 <= t:
            continue
        if ran and status in BILLED_STATES and (status != "degraded" or err_billed):
            cls = "run"
        elif ran and status in STOPPED_STATES:
            cls = "stopped"
        else:
            cls = "unbilled"
        out.append((t, t1, cls))
    return out


def hour_slices(segs: list[tuple], start: datetime, end: datetime) -> list[dict]:
    """Per UTC hour in [start, end): seconds per class. An hour never crosses a month boundary."""
    out = []
    h = _floor_hour(start)
    while h < end:
        h1 = min(h + HOUR, end)
        acc = {"run": 0.0, "stopped": 0.0, "unbilled": 0.0}
        for t0, t1, cls in segs:
            lo, hi = max(t0, h), min(t1, h1)
            if hi > lo:
                acc[cls] += (hi - lo).total_seconds()
        out.append({"period_start": h, "period_end": h1, "running_seconds": int(round(acc["run"])),
                    "stopped_seconds": int(round(acc["stopped"])), "unbilled_seconds": int(round(acc["unbilled"]))})
        h = h + HOUR
    return out


def meter(dep_id: str, *, now: datetime | None = None) -> dict:
    """Write every hour slice that is due for one deployment. Idempotent; safe to call any time."""
    from billing.usage import record_usage_slice

    now = now or _now()
    with normalize.SessionLocal() as s:
        d = s.get(Deployment, dep_id)
        if d is None or not d.provider:
            return {"metered": 0, "reason": "no deployment / provider"}
        rec = s.get(ExecutionRecord, dep_id)
        w = s.get(DeploymentWatch, dep_id)
        if w is not None and w.metering_complete:
            return {"metered": 0, "complete": True}
        has_slices = s.scalar(select(UsageSlice.id).where(UsageSlice.deployment_id == dep_id).limit(1)) is not None
        if rec is not None and rec.usage_record_id is not None and not has_slices:
            legacy = True     # billed in one record before incremental metering existed
        else:
            legacy = False
        events = [(e.at, e.to_status) for e in s.scalars(
            select(DeploymentEvent).where(DeploymentEvent.deployment_id == dep_id)
            .order_by(DeploymentEvent.at, DeploymentEvent.id)) if e.to_status != e.from_status]
        last_obs = w.last_observed_at if w else None
        ended_basis = w.ended_basis if w else None
    if legacy:
        _complete(dep_id, "legacy single usage record")
        return {"metered": 0, "complete": True, "legacy": True}
    terminal = d.status in deployments.TERMINAL_STATES
    if terminal and d.status != "terminated":
        _complete(dep_id, f"{d.status}: no instance ran")
        return {"metered": 0, "complete": True}
    if d.status == "terminated":
        end = d.terminated_at or (events[-1][0] if events else now)
        estimated = ended_basis != "provider_reported"
    else:
        # Live: only up to the last successful provider observation, closed hours only.
        # (d.last_checked_at is also set by FAILED reads, so only the watch's successful observation counts.)
        end = min(now, last_obs) if last_obs is not None else None
        estimated = False
        if end is None:
            return {"metered": 0, "reason": "never observed"}
    if not events:
        return {"metered": 0, "reason": "no events"}
    segs = segments(events, end, provider=d.provider)
    first = next((t0 for t0, _, cls in segs if cls != "unbilled"), None)
    if first is None:
        if d.status == "terminated":
            _complete(dep_id, "never ran")
        return {"metered": 0, "reason": "nothing billable yet"}
    sb = stopped_billing(d.provider)
    price, basis = d.actual_price_per_gpu_hour, "execution"
    if price is None:
        price, basis = d.quoted_price_per_gpu_hour, "quote"
    kind = "byo" if d.credential_source == "byo" else "compute"
    written = 0
    last_end = None
    for sl in hour_slices(segs, first, end):
        closed = sl["period_end"] - sl["period_start"] >= HOUR
        final = d.status == "terminated" and sl["period_end"] >= end
        if not (closed or final):
            continue
        if sl["running_seconds"] + sl["stopped_seconds"] <= 0:
            last_end = sl["period_end"]
            continue
        if price is None and sl["running_seconds"] > 0:
            _note_error(dep_id, "metering: no execution price and no quote price; slice not written")
            alert("metering_no_price", dep_id, "running time cannot be priced", dep_id=dep_id)
            return {"metered": written, "reason": "no price"}
        record_usage_slice(
            deployment_id=dep_id, account_id=d.account_id, provider=d.provider, gpu=d.gpu, gpu_count=d.gpu_count,
            period_start=sl["period_start"], period_end=sl["period_end"], running_seconds=sl["running_seconds"],
            stopped_seconds=sl["stopped_seconds"], unbilled_seconds=sl["unbilled_seconds"], stopped_billing=sb,
            price_per_gpu_hour=None if price is None else Decimal(str(price)), price_basis=basis, kind=kind,
            end_estimated=bool(final and estimated), final=final,
            detail={"stopped_billing_basis": (caps(d.provider).stopped_billing[1] if caps(d.provider) else None),
                    "purpose": d.purpose})
        written += 1
        last_end = sl["period_end"]
    with normalize.SessionLocal.begin() as s:
        w = watch_row(s, dep_id)
        if last_end is not None and (w.metered_through is None or last_end > w.metered_through):
            w.metered_through = last_end
        if d.status == "terminated":
            w.metering_complete = True
            w.ended_at = w.ended_at or end
            w.ended_basis = w.ended_basis or ("first_observed" if estimated else "provider_reported")
        w.updated_at = _now()
    return {"metered": written, "complete": d.status == "terminated"}


def _complete(dep_id: str, why: str) -> None:
    with normalize.SessionLocal.begin() as s:
        w = watch_row(s, dep_id)
        w.metering_complete = True
        w.last_error = None if not why else f"metering: {why}"
        w.updated_at = _now()


def to_meter() -> list[str]:
    """Live deployments plus terminated ones whose metering is not complete."""
    with normalize.SessionLocal() as s:
        live = set(s.scalars(select(Deployment.deployment_id).where(Deployment.status.in_(deployments.LIVE_STATES))))
        done = set(s.scalars(select(DeploymentWatch.deployment_id).where(DeploymentWatch.metering_complete.is_(True))))
        ended = set(s.scalars(select(Deployment.deployment_id).where(Deployment.status == "terminated")))
    return sorted(live | (ended - done))


# --------------------------------------------------------------------------
# The job
# --------------------------------------------------------------------------

def track() -> dict:
    ids = deployments.live_ids()
    polled = skipped = failed = 0
    for dep_id in ids:
        try:
            r = poll(dep_id)
            polled += bool(r.get("polled"))
            skipped += not r.get("polled")
        except Exception:  # noqa: BLE001
            failed += 1
            log.exception("tracking %s failed", dep_id)
    metered = 0
    for dep_id in to_meter():
        try:
            metered += meter(dep_id).get("metered", 0)
        except Exception:  # noqa: BLE001
            log.exception("metering %s failed; retried next tick", dep_id)
    from billing.usage import complete_slice, incomplete_slices
    retried = 0
    for sid in incomplete_slices():
        try:
            retried += complete_slice(sid) is not None
        except Exception:  # noqa: BLE001
            log.exception("usage record for slice %s failed; retried next tick", sid)
    billed = 0
    for dep_id in transactions.unbilled():
        try:
            billed += transactions.bill(dep_id) is not None
        except Exception:  # noqa: BLE001
            log.exception("billing %s failed", dep_id)
    return {"live": len(ids), "polled": polled, "not_polled": skipped, "errors": failed, "slices_written": metered,
            "usage_retried": retried, "billed": billed}


@job("routing_tracker", every_seconds=settings.tracker_interval_seconds, initial_delay_seconds=40)
def _track_job():
    try:
        out = track()
    except Exception as exc:
        _reconcile.job_failed("routing_tracker", exc)   # status polling / metering stalled: page the operator
        raise
    _reconcile.job_ok("routing_tracker")
    return out


# Registers the reconcile job too (api/routing.py imports this module).
from routing import reconcile as _reconcile  # noqa: E402,F401
