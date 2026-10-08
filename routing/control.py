"""The execution control plane: may OpenGrid launch real compute right now, on this provider, for this purpose?

Two layers, the stricter wins:
    settings.routing_live_provisioning   the DEPLOY-LEVEL ceiling (env var). False: the effective mode can
                                         never exceed PREVIEW_ONLY whatever the database says.
    execution_controls['mode']           the OPERATOR's runtime switch (admin API, logged, reason required):
                                         DISABLED | PREVIEW_ONLY | SUPERVISED | LIVE. No row: PREVIEW_ONLY.

Per provider (provider_execution_flags; no row = all defaults): adapter_status 'simulated' | 'validated',
supervised_enabled, live_enabled, killed. adapter_status becomes 'validated' only through mark_validated(),
which the validation/reconciliation code calls after a complete, provider-confirmed validation cycle.

launch_permission(provider, purpose=) rules (methodology/execution-safety.md):
    DISABLED / PREVIEW_ONLY / provider killed             -> never
    purpose 'validation'  SUPERVISED or LIVE mode, provider not killed, ANY adapter_status (this is how a
                          simulated adapter gets validated); always admin-approved; validation caps in guards
    purpose 'customer'    needs adapter_status 'validated'. LIVE mode + live_enabled -> 'LIVE' (no per-launch
                          approval); else supervised_enabled -> 'SUPERVISED' (admin approves every launch)
An unvalidated adapter can never provision customer compute.

kill_all() sets the mode to DISABLED: it stops NEW launches only. Status polling, reconciliation, stop and
terminate keep working in every mode (they are never gated here).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

import normalize
from config import settings
from store.routing import ExecutionControl, ExecutionControlLog, ProviderExecutionFlags

log = logging.getLogger(__name__)

DISABLED, PREVIEW_ONLY, SUPERVISED, LIVE = "DISABLED", "PREVIEW_ONLY", "SUPERVISED", "LIVE"
MODES = (DISABLED, PREVIEW_ONLY, SUPERVISED, LIVE)
_RANK = {m: i for i, m in enumerate(MODES)}
DEFAULT_MODE = PREVIEW_ONLY
PURPOSES = ("customer", "validation")
ADAPTER_STATUSES = ("simulated", "validated")

FLAG_DEFAULTS = {"adapter_status": "simulated", "validated_at": None, "validation_deployment_id": None,
                 "validation_evidence": None, "supervised_enabled": False, "live_enabled": False,
                 "killed": False, "kill_reason": None, "killed_at": None, "killed_by": None,
                 "updated_at": None, "updated_by": None}


class ControlError(ValueError):
    pass


def _now():
    return datetime.now(timezone.utc)


def _iso(t):
    return None if t is None else t.isoformat()


def log_action(s, action: str, target: str | None, before, after, reason: str | None, actor: str | None) -> None:
    """Append to execution_control_log inside the caller's transaction."""
    s.add(ExecutionControlLog(at=_now(), action=action, target=target, before=before, after=after,
                              reason=reason, actor=actor))


def record(action: str, target: str | None, *, before=None, after=None, reason: str | None = None,
           actor: str | None = None) -> None:
    """Append one admin/execution action to the log in its own transaction."""
    with normalize.SessionLocal.begin() as s:
        log_action(s, action, target, before, after, reason, actor)


def _need_reason(reason: str | None) -> str:
    if not reason or not str(reason).strip():
        raise ControlError("a reason is required for every execution-control change")
    return str(reason).strip()[:2000]


# --------------------------------------------------------------------------
# Mode
# --------------------------------------------------------------------------

def stored_mode() -> dict:
    with normalize.SessionLocal() as s:
        row = s.get(ExecutionControl, "mode")
    if row is None:
        return {"mode": DEFAULT_MODE, "updated_by": None, "updated_at": None, "reason": None, "default": True}
    m = (row.value or {}).get("mode", DEFAULT_MODE)
    return {"mode": m if m in MODES else DISABLED, "updated_by": row.updated_by,
            "updated_at": _iso(row.updated_at), "reason": row.reason, "default": False}


def effective_mode() -> str:
    """The mode launches obey: the stored mode, capped at PREVIEW_ONLY by the env ceiling."""
    try:
        m = stored_mode()["mode"]
    except Exception:  # noqa: BLE001 - an unreadable control plane fails closed
        log.exception("execution mode unreadable; failing closed (DISABLED)")
        return DISABLED
    if not settings.routing_live_provisioning and _RANK[m] > _RANK[PREVIEW_ONLY]:
        return PREVIEW_ONLY
    return m


def mode_status() -> dict:
    st = stored_mode()
    eff = effective_mode()
    return {**st, "stored_mode": st["mode"], "effective_mode": eff,
            "env_ceiling": {"routing_live_provisioning": bool(settings.routing_live_provisioning),
                            "note": "env flag false: effective mode is capped at PREVIEW_ONLY"
                            if not settings.routing_live_provisioning else "env flag true: the stored mode applies"},
            "modes": list(MODES)}


def set_mode(mode: str, *, reason: str, by: str) -> dict:
    mode = (mode or "").strip().upper()
    if mode not in MODES:
        raise ControlError(f"mode must be one of {', '.join(MODES)}")
    reason = _need_reason(reason)
    with normalize.SessionLocal.begin() as s:
        row = s.get(ExecutionControl, "mode", with_for_update=True)
        before = None if row is None else dict(row.value or {})
        if row is None:
            row = ExecutionControl(key="mode", value={}, updated_at=_now())
            s.add(row)
        row.value = {"mode": mode}
        row.reason, row.updated_by, row.updated_at = reason, by, _now()
        log_action(s, "set_mode", "mode", before, {"mode": mode}, reason, by)
    log.warning("execution mode set to %s by %s: %s", mode, by, reason)
    return mode_status()


def kill_all(reason: str, by: str) -> dict:
    """Global kill switch: mode DISABLED. New launches stop; monitoring and termination keep working."""
    reason = _need_reason(reason)
    out = set_mode(DISABLED, reason=reason, by=by)
    record("kill_all", "mode", after={"mode": DISABLED}, reason=reason, actor=by)
    _ops_alert(f"ALL live provisioning stopped by {by}: {reason}", None)
    return out


# --------------------------------------------------------------------------
# Provider flags
# --------------------------------------------------------------------------

def _flags_dict(provider: str, row: ProviderExecutionFlags | None) -> dict:
    if row is None:
        return {"provider": provider, **FLAG_DEFAULTS, "default": True}
    return {"provider": provider, "adapter_status": row.adapter_status, "validated_at": _iso(row.validated_at),
            "validation_deployment_id": row.validation_deployment_id,
            "validation_evidence": row.validation_evidence, "supervised_enabled": bool(row.supervised_enabled),
            "live_enabled": bool(row.live_enabled), "killed": bool(row.killed), "kill_reason": row.kill_reason,
            "killed_at": _iso(row.killed_at), "killed_by": row.killed_by, "updated_at": _iso(row.updated_at),
            "updated_by": row.updated_by, "default": False}


def provider_flags(provider: str) -> dict:
    provider = (provider or "").lower()
    with normalize.SessionLocal() as s:
        return _flags_dict(provider, s.get(ProviderExecutionFlags, provider))


def all_provider_flags() -> list[dict]:
    from routing import adapters
    with normalize.SessionLocal() as s:
        rows = {r.provider: r for r in s.scalars(select(ProviderExecutionFlags))}
    names = sorted(set(adapters.ADAPTERS) | set(rows))
    return [_flags_dict(p, rows.get(p)) for p in names]


def _row(s, provider: str) -> ProviderExecutionFlags:
    row = s.get(ProviderExecutionFlags, provider, with_for_update=True)
    if row is None:
        row = ProviderExecutionFlags(provider=provider, adapter_status="simulated", supervised_enabled=False,
                                     live_enabled=False, killed=False)
        s.add(row)
        s.flush()
    return row


def set_provider_flags(provider: str, *, reason: str, by: str, supervised_enabled: bool | None = None,
                       live_enabled: bool | None = None, adapter_status: str | None = None) -> dict:
    """Enable/disable supervised or live launches. adapter_status may only be DEMOTED here ('simulated');
    promotion to 'validated' happens only through mark_validated() after a recorded validation cycle."""
    provider = (provider or "").lower()
    reason = _need_reason(reason)
    if adapter_status is not None and adapter_status != "simulated":
        raise ControlError("adapter_status can only be set to 'simulated' here; 'validated' is recorded by "
                           "mark_validated() after a complete validation cycle")
    with normalize.SessionLocal.begin() as s:
        row = _row(s, provider)
        before = _flags_dict(provider, row)
        if supervised_enabled is not None:
            row.supervised_enabled = bool(supervised_enabled)
        if live_enabled is not None:
            row.live_enabled = bool(live_enabled)
        if adapter_status == "simulated":
            row.adapter_status = "simulated"
        row.updated_at, row.updated_by = _now(), by
        after = _flags_dict(provider, row)
        log_action(s, "set_provider_flags", provider, _jsonable(before), _jsonable(after), reason, by)
    return provider_flags(provider)


def kill_provider(provider: str, reason: str, by: str) -> dict:
    provider = (provider or "").lower()
    reason = _need_reason(reason)
    with normalize.SessionLocal.begin() as s:
        row = _row(s, provider)
        before = _flags_dict(provider, row)
        row.killed, row.kill_reason, row.killed_at, row.killed_by = True, reason, _now(), by
        row.updated_at, row.updated_by = _now(), by
        log_action(s, "kill_provider", provider, _jsonable(before), {"killed": True}, reason, by)
    log.warning("provider %s killed by %s: %s", provider, by, reason)
    _ops_alert(f"Live provisioning on {provider} stopped by {by}: {reason}", provider)
    return provider_flags(provider)


def _ops_alert(title: str, provider: str | None) -> None:
    try:
        from alerts import ops

        ops.alert("kill_switch", title, severity="major", provider=provider)
    except Exception:  # noqa: BLE001 - a kill switch must work even if alerting does not
        log.exception("kill switch ops alert failed")


def unkill_provider(provider: str, reason: str, by: str) -> dict:
    provider = (provider or "").lower()
    reason = _need_reason(reason)
    with normalize.SessionLocal.begin() as s:
        row = _row(s, provider)
        before = _flags_dict(provider, row)
        row.killed, row.kill_reason, row.killed_at, row.killed_by = False, None, None, None
        row.updated_at, row.updated_by = _now(), by
        log_action(s, "unkill_provider", provider, _jsonable(before), {"killed": False}, reason, by)
    return provider_flags(provider)


def mark_validated(provider: str, deployment_id: str, evidence: dict, by: str) -> dict:
    """Record that `provider`'s adapter completed a real validation cycle (launched -> observed running ->
    termination confirmed by the provider -> cost reconciled). Called only by the validation/reconciliation
    code. It does NOT enable launches: supervised_enabled / live_enabled stay operator decisions."""
    provider = (provider or "").lower()
    if not deployment_id or not isinstance(evidence, dict) or not evidence:
        raise ControlError("mark_validated needs the validation deployment id and its evidence")
    with normalize.SessionLocal.begin() as s:
        row = _row(s, provider)
        before = _flags_dict(provider, row)
        row.adapter_status, row.validated_at = "validated", _now()
        row.validation_deployment_id, row.validation_evidence = deployment_id, evidence
        row.updated_at, row.updated_by = _now(), by
        log_action(s, "mark_validated", provider, _jsonable(before),
                   {"adapter_status": "validated", "deployment_id": deployment_id}, "validation cycle complete", by)
    return provider_flags(provider)


# --------------------------------------------------------------------------
# The decision
# --------------------------------------------------------------------------

def launch_permission(provider: str, *, purpose: str, mode: str | None = None,
                      flags: dict | None = None) -> tuple[bool, str | None, str]:
    """(allowed, mode_used 'SUPERVISED'|'LIVE'|None, reason). mode_used SUPERVISED means an admin must
    approve this launch; LIVE means it may launch without per-launch approval (guards still apply)."""
    if purpose not in PURPOSES:
        return False, None, f"unknown purpose {purpose!r}"
    mode = mode or effective_mode()
    if mode == DISABLED:
        return False, None, "execution is DISABLED (kill switch or operator setting)"
    if mode == PREVIEW_ONLY:
        if not settings.routing_live_provisioning:
            return False, None, "live provisioning disabled in this environment"
        return False, None, "execution mode is PREVIEW_ONLY"
    f = flags if flags is not None else provider_flags(provider)
    if f.get("killed"):
        return False, None, f"provider {provider} is killed: {f.get('kill_reason') or 'no reason recorded'}"
    if purpose == "validation":
        return True, SUPERVISED, "validation launch: admin approval and validation caps required"
    if f.get("adapter_status") != "validated":
        return False, None, f"{provider} adapter is not validated (simulated): no customer compute"
    if mode == LIVE and f.get("live_enabled"):
        return True, LIVE, "LIVE: validated and live-enabled provider; guards and quote re-validation apply"
    if f.get("supervised_enabled"):
        return True, SUPERVISED, "SUPERVISED: an admin must approve this launch"
    return False, None, f"{provider} is validated but not enabled for {'live' if mode == LIVE else 'supervised'} launches"


def _jsonable(d: dict) -> dict:
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in d.items()}


def recent_log(limit: int = 200, target: str | None = None) -> list[dict]:
    q = select(ExecutionControlLog).order_by(ExecutionControlLog.id.desc()).limit(limit)
    if target:
        q = q.where(ExecutionControlLog.target == target)
    with normalize.SessionLocal() as s:
        return [{"id": r.id, "at": _iso(r.at), "action": r.action, "target": r.target, "before": r.before,
                 "after": r.after, "reason": r.reason, "actor": r.actor} for r in s.scalars(q)]


# --------------------------------------------------------------------------
# Readiness evidence (the validation launch gate and the first-route checklist read these)
# --------------------------------------------------------------------------
# Everything here is read from Postgres, so it is correct when the API and the background jobs run in
# different processes: job heartbeats are rows in execution_controls ('job:<name>'), drills and the ops test
# alert are rows in execution_control_log.

def _ts(v):
    if v is None or isinstance(v, datetime):
        return v
    t = datetime.fromisoformat(str(v))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def record_job_health(name: str, *, ok: bool, error: str | None = None) -> None:
    """Durable heartbeat of one background job run (written by the wrapper install_job_heartbeats() puts
    around the registered job). Never raises."""
    try:
        now = _now()
        with normalize.SessionLocal.begin() as s:
            row = s.get(ExecutionControl, f"job:{name}", with_for_update=True)
            v = dict(row.value or {}) if row is not None else {}
            v["runs"] = int(v.get("runs") or 0) + 1
            if ok:
                v.update(last_ok_at=now.isoformat(), consecutive_failures=0)
            else:
                v.update(last_error_at=now.isoformat(), last_error=(error or "")[:500],
                         consecutive_failures=int(v.get("consecutive_failures") or 0) + 1)
            v["last_run_at"] = now.isoformat()
            if row is None:
                s.add(ExecutionControl(key=f"job:{name}", value=v, updated_by="job", updated_at=now,
                                       reason="job heartbeat"))
            else:
                row.value, row.updated_at, row.updated_by = v, now, "job"
    except Exception:  # noqa: BLE001 - a heartbeat must never break the job
        log.exception("job heartbeat for %s not recorded", name)


def install_job_heartbeats(names=("reconcile", "routing_tracker")) -> None:
    """Wrap the registered jobs so every run leaves a durable heartbeat (idempotent)."""
    import functools

    import jobs

    for name in names:
        j = jobs.JOBS.get(name)
        if j is None or getattr(j.fn, "_og_heartbeat", False):
            continue
        fn = j.fn

        @functools.wraps(fn)
        def wrapped(_fn=fn, _name=name):
            try:
                out = _fn()
            except Exception as exc:
                record_job_health(_name, ok=False, error=f"{type(exc).__name__}: {exc}")
                raise
            record_job_health(_name, ok=True)
            return out

        wrapped._og_heartbeat = True
        j.fn = wrapped


def job_health(name: str, interval_seconds: float) -> dict:
    """{ok, reason, evidence}: the job ran successfully within 2x its interval and its last run(s) did not fail.
    Reads the durable heartbeat; falls back to the in-process jobs registry."""
    now = _now()
    window = timedelta(seconds=2 * max(float(interval_seconds), 1.0))
    try:
        with normalize.SessionLocal() as s:
            row = s.get(ExecutionControl, f"job:{name}")
        hb = dict(row.value or {}) if row is not None else None
    except Exception as exc:  # noqa: BLE001
        hb = None
        log.exception("job heartbeat unreadable")
        return {"ok": False, "reason": f"job {name}: heartbeat unreadable ({type(exc).__name__})", "evidence": None}
    if hb:
        last_ok = _ts(hb.get("last_ok_at"))
        fails = int(hb.get("consecutive_failures") or 0)
        ev = {"source": "heartbeat", **hb}
        if fails:
            return {"ok": False, "reason": f"job {name}: {fails} consecutive failure(s), last error "
                                           f"{hb.get('last_error')!r}", "evidence": ev}
        if last_ok is None or now - last_ok > window:
            return {"ok": False, "reason": f"job {name}: last successful run {hb.get('last_ok_at') or 'never'} is older "
                                           f"than 2x its interval ({int(window.total_seconds())} s)", "evidence": ev}
        return {"ok": True, "reason": f"job {name}: last successful run {hb.get('last_ok_at')}", "evidence": ev}
    try:
        import jobs
        j = jobs.JOBS.get(name)
    except Exception:  # noqa: BLE001
        j = None
    if j is None or j.last_finished is None:
        return {"ok": False, "reason": f"job {name}: no heartbeat recorded and not run in this process",
                "evidence": None}
    ev = {"source": "process", "last_finished": _iso(j.last_finished), "last_error": j.last_error, "runs": j.runs,
          "failures": j.failures}
    if j.last_error:
        return {"ok": False, "reason": f"job {name}: last run failed ({j.last_error})", "evidence": ev}
    if now - j.last_finished > window:
        return {"ok": False, "reason": f"job {name}: last run {_iso(j.last_finished)} is older than 2x its interval",
                "evidence": ev}
    return {"ok": True, "reason": f"job {name}: last run {_iso(j.last_finished)} ok", "evidence": ev}


def reconcile_health(provider: str, *, strict: bool = True) -> dict:
    """The latest finished reconciliation pass covering `provider`: fresh (< 2x interval), not failed, and with
    no error for THIS provider (another provider's list failure does not count). strict: the provider must
    also have been reconciled in that pass (listed with credentials, not skipped)."""
    provider = (provider or "").lower()
    window = timedelta(seconds=2 * max(float(settings.reconcile_interval_seconds), 1.0))
    try:
        from store.reconcile import ReconciliationRun
        with normalize.SessionLocal() as s:
            r = s.scalars(select(ReconciliationRun).where(
                ReconciliationRun.finished_at.is_not(None),
                (ReconciliationRun.provider.is_(None)) | (ReconciliationRun.provider == provider))
                .order_by(ReconciliationRun.finished_at.desc()).limit(1)).first()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"reconciliation_runs unreadable ({type(exc).__name__})", "evidence": None}
    if r is None:
        return {"ok": False, "reason": "no finished reconciliation run recorded", "evidence": None}
    p = (r.providers or {}).get(provider)
    ev = {"run_id": r.id, "finished_at": _iso(r.finished_at), "status": r.status, "error": r.error, "provider": p}
    if _now() - r.finished_at > window:
        return {"ok": False, "reason": f"last reconciliation run {_iso(r.finished_at)} is older than 2x the interval",
                "evidence": ev}
    if r.status == "failed" or (r.error and r.status != "partial"):
        return {"ok": False, "reason": f"last reconciliation run failed: {r.error!r}", "evidence": ev}
    if isinstance(p, dict) and (p.get("error") or p.get("list_errors")):
        return {"ok": False, "reason": f"last reconciliation run errored for {provider}: "
                                       f"{p.get('error') or str(p.get('list_errors')) + ' list error(s)'}",
                "evidence": ev}
    if strict and (not isinstance(p, dict) or p.get("skipped")):
        return {"ok": False, "reason": f"last reconciliation run did not reconcile {provider}"
                                       + (f" ({p.get('skipped')})" if isinstance(p, dict) else ""), "evidence": ev}
    return {"ok": True, "reason": f"reconciliation run {r.id} at {_iso(r.finished_at)} ok for {provider}",
            "evidence": ev}


def _drill_window() -> timedelta:
    return timedelta(days=max(1, int(getattr(settings, "validation_drill_window_days", 7) or 7)))


def drill_status(provider: str | None = None) -> dict:
    """Kill-switch drills in the window: global (a kill_all / mode DISABLED followed by a mode change away from
    DISABLED) and, for `provider`, a kill_provider followed by an unkill_provider."""
    since = _now() - _drill_window()
    with normalize.SessionLocal() as s:
        rows = list(s.scalars(select(ExecutionControlLog).where(ExecutionControlLog.at >= since)
                              .order_by(ExecutionControlLog.at, ExecutionControlLog.id)))
    g_kill = g_unkill = p_kill = p_unkill = None
    for r in rows:
        after = r.after if isinstance(r.after, dict) else {}
        before = r.before if isinstance(r.before, dict) else {}
        if r.action == "kill_all" or (r.action == "set_mode" and after.get("mode") == DISABLED):
            g_kill, g_unkill = r.at, None
        elif r.action == "set_mode" and g_kill is not None and after.get("mode") not in (None, DISABLED) \
                and (before.get("mode") in (None, DISABLED)):
            g_unkill = g_unkill or r.at
        if provider and r.target == provider:
            if r.action == "kill_provider":
                p_kill, p_unkill = r.at, None
            elif r.action == "unkill_provider" and p_kill is not None:
                p_unkill = p_unkill or r.at
    out = {"window_days": _drill_window().days,
           "global": {"ok": bool(g_kill and g_unkill), "kill_at": _iso(g_kill), "unkill_at": _iso(g_unkill)}}
    if provider:
        out["provider"] = {"provider": provider, "ok": bool(p_kill and p_unkill), "kill_at": _iso(p_kill),
                           "unkill_at": _iso(p_unkill)}
    return out


def ops_channel_configured() -> bool:
    try:
        from alerts import ops
        return bool(ops.channel_configured())
    except Exception:  # noqa: BLE001
        log.exception("ops alert channel check failed")
        return False


def send_test_alert(*, by: str, reason: str | None = None) -> dict:
    """Send ONE ops test alert through the real channel and record the result (execution_control_log
    'ops_test_alert'). The validation gate needs a delivered one within the drill window."""
    configured = ops_channel_configured()
    result = {"recorded": False, "delivered": False}
    error = None
    try:
        from alerts import ops
        result = ops.alert("test_alert", f"OpenGrid ops test alert from {by}", severity="notable",
                           detail={"test": True}, dedupe=f"test_alert:{_now().isoformat()}") or result
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {str(exc)[:200]}"
    after = {"channel_configured": configured, "delivered": bool(result.get("delivered")),
             "recorded": bool(result.get("recorded")), "error": error}
    record("ops_test_alert", "alerts:ops", after=after, reason=(reason or "ops test alert"), actor=by)
    return {**after, "at": _now().isoformat(), **ops_alert_health()}


def ops_alert_health() -> dict:
    configured = ops_channel_configured()
    since = _now() - _drill_window()
    with normalize.SessionLocal() as s:
        r = s.scalars(select(ExecutionControlLog).where(ExecutionControlLog.action == "ops_test_alert",
                                                        ExecutionControlLog.at >= since)
                      .order_by(ExecutionControlLog.at.desc()).limit(1)).first()
    delivered = bool(r is not None and (r.after or {}).get("delivered"))
    ok = configured and delivered
    reason = ("ops alert channel configured; test alert delivered at " + _iso(r.at)) if ok else (
        "no ops alert channel configured (OPS_ALERT_WEBHOOK_URL + OPS_ALERT_WEBHOOK_SECRET)" if not configured else
        "no successfully delivered ops test alert in the last "
        f"{_drill_window().days} days (POST /v1/admin/ops/test-alert)")
    return {"ok": ok, "channel_configured": configured, "last_test_alert_at": _iso(r.at) if r is not None else None,
            "last_test_alert_delivered": delivered, "reason": reason}
