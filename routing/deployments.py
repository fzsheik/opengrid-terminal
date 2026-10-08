"""Deployment lifecycle: an explicit state machine, the single provision call, pinned credentials,
status observation, stop and terminate.

States (methodology/execution-safety.md has the full table)
    created -> quoted -> pending_approval -> approved -> provisioning -> running -> stopping -> stopped
            -> terminating -> terminated
    failure / uncertain:
        quote_failed, quote_expired, rejected (admin rejected / cancelled before launch)
        provision_failed     the provider definitively created nothing (capacity / validation, no instance)
        provider_rejected    the provider refused for an account reason (auth / quota / rate limit)
        provider_timeout     the provision call timed out: existence unknown until reconciliation checks
        launch_unknown       any other ambiguous outcome: the provider may have created an instance
        degraded             the provider reports an error on an instance that may still exist (keeps billing)
        termination_failed   the provider refused the delete; the instance is still there
        orphan_suspected     set by reconciliation
        credentials_unavailable  the credential pinned at launch can no longer be used (alert; state kept)

Rules enforced here
    * transition() is the ONLY writer of `status`; it checks ALLOWED_TRANSITIONS and writes a
      deployment_events row with actor (user | admin | system | reconciler), reason and evidence.
      terminated is absorbing. Ambiguity is never collapsed into a failure.
    * At most ONE provision call per deployment, ever: launch() takes the row lock, requires status
      'approved' and no launch_token, sets the token, moves to provisioning and commits the
      provision_attempts row (outcome 'provisioning') BEFORE calling the provider. A crash after the
      provider accepted leaves that row for reconciliation (find_instance(client_name)).
    * running only after the provider reports the instance running (observe()).
    * Credentials are pinned at launch (credential_source, credential_ref); status/stop/terminate use
      credentials_for(deployment) and never anything else.
    * terminated only with provider evidence: a status read saying terminated, or two consecutive
      not_found reads >= 60 s apart (reconciliation adds list_instances evidence). terminate() moves to
      terminating; the tracker / reconciler confirms.

Uptime is OpenGrid-observed (summed between status reads that saw it running). An interruption is a
running deployment leaving running without OpenGrid having asked.
"""

from __future__ import annotations

import logging
import secrets
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select

import normalize
import provider_meta
from routing import adapters, credentials, transactions
from routing.adapters.results import InstanceState, ProvisionResult, TerminateResult, instance_name
from routing.audit import new_deployment_id, redacted
from routing.credentials import CredentialsUnavailable
from store.routing import Deployment, DeploymentEvent, ExecutionRecord, ProvisionAttempt

log = logging.getLogger(__name__)
plog = logging.getLogger("opengrid.provider")

# --------------------------------------------------------------------------
# The state machine
# --------------------------------------------------------------------------

PRE_LAUNCH_STATES = ("created", "quoted", "pending_approval", "approved", "quote_expired")
TERMINAL_STATES = ("terminated", "rejected", "provision_failed", "provider_rejected", "quote_failed")
UNCERTAIN_STATES = ("provider_timeout", "launch_unknown", "orphan_suspected", "credentials_unavailable",
                    "termination_failed")
# Every state in which an instance may exist at the provider (tracked, may bill).
LIVE_STATES = ("provisioning", "running", "degraded", "stopping", "stopped", "terminating",
               "termination_failed", "provider_timeout", "launch_unknown", "orphan_suspected",
               "credentials_unavailable")
# Counted by cost guards (an approved launch is about to exist).
ACTIVE_STATES = ("approved",) + LIVE_STATES
STATUSES = ("created", "quoted", "pending_approval", "approved", "provisioning", "running", "degraded",
            "stopping", "stopped", "terminating", "terminated", "quote_failed", "quote_expired", "rejected",
            "provision_failed", "provider_rejected", "provider_timeout", "launch_unknown", "termination_failed",
            "orphan_suspected", "credentials_unavailable")
LIVE = LIVE_STATES  # back-compat name
ACTORS = ("user", "admin", "system", "reconciler")

_INSTANCE_LIVE = ("running", "degraded", "stopping", "stopped", "terminating", "termination_failed",
                  "orphan_suspected", "credentials_unavailable", "terminated")

ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "created": ("quoted", "quote_failed", "rejected"),
    "quoted": ("pending_approval", "approved", "quote_expired", "quote_failed", "rejected"),
    "pending_approval": ("approved", "rejected", "quote_expired", "quote_failed"),
    "quote_expired": ("pending_approval", "rejected", "quote_failed"),
    "approved": ("provisioning", "pending_approval", "rejected", "provision_failed", "quote_expired"),
    "provisioning": ("running", "degraded", "stopping", "stopped", "terminating", "terminated",
                     "provision_failed", "provider_rejected", "provider_timeout", "launch_unknown",
                     "orphan_suspected", "credentials_unavailable"),
    "provider_timeout": ("launch_unknown", "provisioning", "running", "degraded", "stopped", "terminating",
                         "terminated", "provision_failed", "orphan_suspected", "credentials_unavailable"),
    "launch_unknown": ("provisioning", "running", "degraded", "stopped", "terminating", "terminated",
                       "provision_failed", "orphan_suspected", "credentials_unavailable"),
    "running": ("degraded", "stopping", "stopped", "terminating", "terminated", "orphan_suspected",
                "credentials_unavailable"),
    "degraded": ("running", "stopping", "stopped", "terminating", "terminated", "orphan_suspected",
                 "credentials_unavailable"),
    "stopping": ("stopped", "running", "degraded", "terminating", "terminated", "credentials_unavailable",
                 "orphan_suspected"),
    "stopped": ("running", "degraded", "stopping", "terminating", "terminated", "credentials_unavailable",
                "orphan_suspected"),
    "terminating": ("terminated", "termination_failed", "orphan_suspected", "credentials_unavailable"),
    "termination_failed": ("terminating", "terminated", "orphan_suspected", "credentials_unavailable"),
    "orphan_suspected": ("running", "degraded", "stopped", "terminating", "terminated", "credentials_unavailable"),
    "credentials_unavailable": ("provisioning", "running", "degraded", "stopping", "stopped", "terminating",
                                "terminated", "termination_failed", "launch_unknown", "orphan_suspected"),
    # Definitive "nothing was created" outcomes; reconciliation may still find contrary evidence.
    "provision_failed": ("orphan_suspected",),
    "provider_rejected": ("orphan_suspected",),
    "quote_failed": (),
    "rejected": (),
    "terminated": (),
}
assert set(ALLOWED_TRANSITIONS) == set(STATUSES)


class IllegalTransition(ValueError):
    def __init__(self, frm: str, to: str, dep_id: str | None = None):
        super().__init__(f"illegal deployment transition {frm} -> {to}" + (f" ({dep_id})" if dep_id else ""))
        self.frm, self.to = frm, to


def can_transition(frm: str, to: str) -> bool:
    return to in ALLOWED_TRANSITIONS.get(frm, ())


def _now():
    return datetime.now(timezone.utc)


def _d(v) -> Decimal | None:
    return None if v is None else Decimal(str(round(float(v), 6)))


def _meta(d: Deployment, **kv) -> None:
    d.provider_metadata = {**(d.provider_metadata or {}), **kv}   # reassign so the JSONB change is seen


def _jsonable(v):
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return v


def _apply(s, d: Deployment, new: str, *, reason: str | None, evidence: dict | None = None, actor: str,
           actor_id: str | None, now: datetime, detail: dict | None = None) -> bool:
    prev = d.status
    if new == prev:
        return False
    if not can_transition(prev, new):
        raise IllegalTransition(prev, new, d.deployment_id)
    if actor not in ACTORS:
        raise ValueError(f"actor must be one of {ACTORS}")
    detail = dict(detail or {})
    md = d.provider_metadata or {}
    if prev == "running" and d.running_since is not None:
        d.uptime_seconds = (d.uptime_seconds or 0) + max(0, int((now - d.running_since).total_seconds()))
        d.running_since = None
    if prev == "running" and new in ("degraded", "stopping", "stopped", "terminating", "terminated"):
        ours = ((new in ("stopping", "stopped") and md.get("requested_action") == "stop")
                or (new in ("terminating", "terminated") and d.terminate_requested_at is not None))
        if not ours:
            d.interruptions = (d.interruptions or 0) + 1
            detail["interruption"] = True
    if new == "running":
        d.running_since = now
        if d.provisioned_at is None:
            d.provisioned_at = now
    if new == "terminated":
        if d.terminated_at is None:
            ended = (evidence or {}).get("ended_at")
            try:
                d.terminated_at = datetime.fromisoformat(ended) if isinstance(ended, str) else (ended or now)
            except ValueError:
                d.terminated_at = now
        if d.termination_reason is None:
            d.termination_reason = "provider_terminated"
    d.status = new
    d.state_changed_at = now
    s.add(DeploymentEvent(deployment_id=d.deployment_id, at=now, from_status=prev, to_status=new,
                          detail=_jsonable(detail) or None, actor=actor, actor_id=actor_id,
                          reason=(reason or "")[:4000] or None, evidence=_jsonable(evidence) if evidence else None))
    return True


def transition(dep, to: str, reason: str | None = None, evidence: dict | None = None, *, actor: str = "system",
               actor_id: str | None = None, s=None, detail: dict | None = None) -> bool:
    """Move a deployment to `to`. `dep` is a Deployment row (with `s`, the caller's session, holding the row
    lock) or a deployment id (own transaction, row locked FOR UPDATE). Raises IllegalTransition.
    Returns False when already in `to` (no event written)."""
    now = _now()
    if s is not None:
        d = dep if isinstance(dep, Deployment) else s.get(Deployment, dep, with_for_update=True)
        changed = _apply(s, d, to, reason=reason, evidence=evidence, actor=actor, actor_id=actor_id, now=now,
                         detail=detail)
        if changed and to == "terminated":
            _sync_record(s, d)
        return changed
    dep_id = dep.deployment_id if isinstance(dep, Deployment) else dep
    with normalize.SessionLocal.begin() as s2:
        d = s2.get(Deployment, dep_id, with_for_update=True)
        if d is None:
            raise KeyError(dep_id)
        changed = _apply(s2, d, to, reason=reason, evidence=evidence, actor=actor, actor_id=actor_id, now=now,
                         detail=detail)
        if changed and to == "terminated":
            _sync_record(s2, d)
    if changed and to == "terminated":
        _bill(dep_id)
    return changed


def note_event(s, d: Deployment, reason: str, evidence: dict | None = None, *, actor: str = "system",
               actor_id: str | None = None) -> None:
    """An auditable event that does not change state (terminate requested while the id is unknown, ...)."""
    s.add(DeploymentEvent(deployment_id=d.deployment_id, at=_now(), from_status=d.status, to_status=d.status,
                          actor=actor, actor_id=actor_id, reason=reason[:4000],
                          evidence=_jsonable(evidence) if evidence else None))


def _sync_record(s, d: Deployment) -> None:
    try:
        transactions.sync(s, d)
    except Exception:  # noqa: BLE001 - the execution record never blocks a state change
        log.exception("execution record sync failed for %s", d.deployment_id)


def _bill(dep_id: str) -> None:
    try:
        transactions.bill(dep_id)
    except Exception:  # noqa: BLE001 - billing retries from the tracker
        log.exception("billing %s failed; the tracker retries", dep_id)


# --------------------------------------------------------------------------
# Creation (before any provider call)
# --------------------------------------------------------------------------

def _seal_env(env: dict | None) -> str | None:
    """Launch env VALUES may be secrets: kept only encrypted (accounts.credentials Fernet) until launch."""
    if not env:
        return None
    import json

    from accounts import credentials as ac
    return ac.encrypt(json.dumps(env)).decode()


def _open_env(blob: str | None) -> dict:
    if not blob:
        return {}
    import json

    from accounts import credentials as ac
    return json.loads(ac.decrypt(blob.encode()))


def create(*, rr_id: str, who, spec: dict, quote: dict, candidate: dict | None, purpose: str,
           approval_mode: str, limit_violations: list[dict], launch_request: dict | None,
           max_runtime_minutes: int | None, auto_approve: bool, actor: str, actor_id: str | None,
           runtime: dict | None = None, ssh: dict | None = None) -> str:
    """created -> quoted -> pending_approval (or -> approved for a LIVE launch without violations).

    runtime: guards.runtime_ceiling() (computed here from max_runtime_minutes when not given); the deployment
    always carries a finite effective_max_runtime_minutes. ssh: engine.ssh_access_for() (fingerprint of the
    CUSTOMER key, operator access). A LIVE auto-approval re-checks the guards ATOMICALLY (guards.gate: advisory
    lock + count in this transaction); a violation found there keeps it pending_approval."""
    from routing import guards

    dep_id = new_deployment_id()
    now = _now()
    if runtime is None:
        runtime = guards.runtime_ceiling(who.account_id, max_runtime_minutes, purpose=purpose)
    eff = int(runtime["effective_max_runtime_minutes"])
    ssh = ssh or {}
    lr = dict(launch_request or {})
    env_sealed = _seal_env(lr.get("env"))
    stored_launch = redacted({"launch": lr}).get("launch") or {}
    if env_sealed:
        stored_launch["env_sealed"] = env_sealed
    with normalize.SessionLocal.begin() as s:
        d = Deployment(
            deployment_id=dep_id, account_id=who.account_id, key_id=who.key_id, route_request_id=rr_id,
            provider=quote["provider"], listing_id=quote["listing_id"], gpu=quote["gpu"], gpu_count=quote["gpu_count"],
            region=(quote.get("region") or None), status="created", created_at=now, uptime_seconds=0,
            interruptions=0, launch=stored_launch or None, provider_metadata={},
            observed_market_price_per_gpu_hour=_d(quote.get("observed_price_per_gpu_hour")),
            list_price_per_gpu_hour=_d(((candidate or {}).get("list_price_per_gpu_hour"))),
            quoted_price_per_gpu_hour=_d(quote["quote_price_per_gpu_hour"]),
            quote_basis=quote.get("price_source"), purpose=purpose, quote_id=quote["quote_id"],
            approval_mode=approval_mode, max_runtime_minutes=eff, effective_max_runtime_minutes=eff,
            runtime_ceiling_source=runtime["runtime_ceiling_source"],
            ssh_key_fingerprint=ssh.get("customer_key_fingerprint") if purpose != "validation" else None,
            operator_access=ssh.get("operator_access") or (
                "validation_operator_key" if purpose == "validation" else "none"),
            limit_violations=limit_violations or None, override_limits=False, client_name=instance_name(dep_id),
            state_changed_at=now)
        s.add(d)
        s.flush()
        s.add(DeploymentEvent(deployment_id=dep_id, at=now, from_status=None, to_status="created", actor=actor,
                              actor_id=actor_id, reason=f"route {rr_id} ({purpose})"))
        _apply(s, d, "quoted", reason=f"quote {quote['quote_id']}", actor="system", actor_id=None, now=now,
               evidence={"quote_id": quote["quote_id"], "price_per_gpu_hour": quote["quote_price_per_gpu_hour"],
                         "price_source": quote.get("price_source"), "expires_at": quote["expires_at"]})
        if auto_approve and not limit_violations:
            violations, held = guards.gate(s, d, override=False)
            if held:
                limit_violations = violations
                d.limit_violations = violations
        if auto_approve and not limit_violations:
            d.approved_by, d.approved_at = "system:live", now
            d.terminate_deadline_at = now + timedelta(minutes=eff)
            _apply(s, d, "approved", reason="LIVE mode: launch without per-launch approval (validated, live-enabled "
                                            "provider; guards passed under the account lock)", actor="system",
                   actor_id=None, now=now, evidence={"mode": "LIVE", "effective_max_runtime_minutes": eff,
                                                     "terminate_deadline_at": d.terminate_deadline_at.isoformat()})
        else:
            why = ("limit violations: " + "; ".join(v["code"] for v in limit_violations)) if limit_violations \
                else ("provider may install account-level ssh keys: admin approval with an operator-access "
                      "override required" if ssh.get("blocked") else f"{approval_mode}: waiting for admin approval")
            _apply(s, d, "pending_approval", reason=why, actor="system", actor_id=None, now=now,
                   evidence={"limit_violations": limit_violations} if limit_violations else None)
    return dep_id


def load_row(dep_id: str) -> Deployment | None:
    with normalize.SessionLocal() as s:
        return s.get(Deployment, dep_id)


def for_request(rr_id: str) -> Deployment | None:
    """The newest deployment of a route request."""
    with normalize.SessionLocal() as s:
        return s.scalars(select(Deployment).where(Deployment.route_request_id == rr_id)
                         .order_by(Deployment.created_at.desc())).first()


# --------------------------------------------------------------------------
# Credentials pinned at launch
# --------------------------------------------------------------------------

def credentials_for(d: Deployment) -> dict:
    """The exact credential the deployment was launched with. Raises CredentialsUnavailable."""
    if not d.provider:
        raise CredentialsUnavailable("deployment has no provider")
    ref = d.credential_ref
    if ref is None and d.credential_source == "opengrid":   # rows written before 0010 without a ref
        ref = f"platform:{credentials.credential_provider(d.provider)}"
    creds = credentials.for_ref(ref, d.provider)
    credentials.check_pinned(creds, credentials.pinned_fingerprint(d), ref)   # the very secret used at launch
    return creds


def adapter_for(d: Deployment, **log_context):
    """An adapter for managing this deployment with its pinned credentials. Raises CredentialsUnavailable,
    or HTTPException(409) when no adapter exists for the provider."""
    creds = credentials_for(d)
    a = adapters.build(d.provider, creds)
    if a is None:
        raise HTTPException(409, f"no adapter for {d.provider}")
    _set_log_context(a, deployment_id=d.deployment_id, route_request_id=d.route_request_id, **log_context)
    return a


def _set_log_context(a, **ctx) -> None:
    try:
        a.log_context = {**(getattr(a, "log_context", None) or {}), **{k: v for k, v in ctx.items() if v}}
    except Exception:  # noqa: BLE001
        pass


def mark_credentials_unavailable(dep_id: str, exc: CredentialsUnavailable, *, actor: str = "system") -> None:
    with normalize.SessionLocal.begin() as s:
        d = s.get(Deployment, dep_id, with_for_update=True)
        if d is None or d.status not in LIVE_STATES or d.status == "credentials_unavailable":
            return
        _meta(d, status_before_credentials_unavailable=d.status, credentials_error=exc.message)
        _apply(s, d, "credentials_unavailable", reason=exc.message, actor=actor, actor_id=None, now=_now(),
               evidence={"credential_ref": d.credential_ref})
    log.error("deployment %s: pinned credentials unavailable (%s): %s", dep_id, exc.ref, exc.message)
    _alert("credentials_unavailable", dep_id, exc.message)


def _alert(kind: str, dep_id: str, message: str) -> None:
    """Operator alert hook. Logged at ERROR with a stable marker; the alerts module may subscribe."""
    log.error("EXECUTION_ALERT %s deployment=%s: %s", kind, dep_id, message)
    try:
        from alerts import ops as alert_ops

        alert_ops.alert(kind, f"{dep_id}: {message}", severity="major",
                        detail={"deployment_id": dep_id}, dedupe=f"{kind}:{dep_id}")
    except Exception:  # noqa: BLE001 - alerting must never break a state transition
        log.exception("ops alert for %s failed", dep_id)


# --------------------------------------------------------------------------
# Normalising adapter results (new result types; legacy shapes accepted)
# --------------------------------------------------------------------------

_ACCOUNT_KINDS = ("auth", "quota", "rate_limit", "rate_limited", "payment", "account")


def as_provision_result(out) -> ProvisionResult:
    from routing.adapters.base import AdapterError, Instance
    if isinstance(out, ProvisionResult):
        if out.outcome == "accepted" and not out.instance_id:
            return ProvisionResult("unknown", error_kind="parse", message="accepted without an instance id",
                                   status_code=out.status_code)
        return out if out.outcome in ("accepted", "rejected", "unknown") else ProvisionResult(
            "unknown", error_kind="parse", message=f"unrecognised outcome {out.outcome!r}")
    if isinstance(out, Instance):
        if out.instance_id:
            return ProvisionResult("accepted", instance_id=str(out.instance_id), message=out.provider_status or "")
        return ProvisionResult("unknown", error_kind="parse", message="accepted without an instance id")
    if isinstance(out, AdapterError):
        k = out.kind
        if k in ("timeout",):
            return ProvisionResult("unknown", error_kind="timeout", message=out.message, status_code=out.status_code)
        if k in ("unknown_state", "provider_error", "server", "network", "parse", "ambiguous"):
            return ProvisionResult("unknown", error_kind=k, message=out.message, status_code=out.status_code)
        mapped = {"rate_limited": "rate_limit", "invalid": "validation", "config": "validation",
                  "not_found": "validation"}.get(k, k)
        return ProvisionResult("rejected", error_kind=mapped, message=out.message, status_code=out.status_code)
    if isinstance(out, BaseException):
        return ProvisionResult("unknown", error_kind="internal", message=f"unexpected {type(out).__name__}")
    return ProvisionResult("unknown", error_kind="parse", message=f"unrecognised provision result {type(out).__name__}")


_LEGACY_STATE = {"provisioning": "pending", "running": "running", "stopped": "stopped", "failed": "error",
                 "terminating": "terminating", "terminated": "terminated", "unknown": "unknown"}


def as_instance_state(out, instance_id: str | None = None) -> InstanceState:
    from routing.adapters.base import AdapterError, Instance
    if isinstance(out, InstanceState):
        return out
    if isinstance(out, Instance):
        price = None if out.price_per_gpu_hour is None else out.price_per_gpu_hour
        st = InstanceState(_LEGACY_STATE.get(out.status, "unknown"), instance_id=out.instance_id,
                           provider_status=out.provider_status, region=out.region, ip=out.ip)
        st._price_per_gpu_hour = price  # type: ignore[attr-defined]
        return st
    if isinstance(out, AdapterError):
        if out.kind == "not_found":
            return InstanceState("not_found", instance_id=instance_id, message=out.message)
        return InstanceState("unknown", instance_id=instance_id, error_kind=out.kind, message=out.message)
    return InstanceState("unknown", instance_id=instance_id, error_kind="internal",
                         message=f"unexpected {type(out).__name__}")


def as_terminate_result(out) -> TerminateResult:
    from routing.adapters.base import AdapterError, Instance
    if isinstance(out, TerminateResult):
        return out
    if isinstance(out, Instance):
        return TerminateResult("accepted", out.provider_status or out.status)
    if isinstance(out, AdapterError):
        if out.kind == "not_found":
            return TerminateResult("already_gone", out.message, out.status_code, error_kind="not_found")
        if out.kind in ("auth", "invalid", "config", "rate_limited", "quota", "capacity"):
            return TerminateResult("failed", out.message, out.status_code, error_kind=out.kind)
        return TerminateResult("unknown", out.message, out.status_code, error_kind=out.kind)
    return TerminateResult("unknown", f"unexpected {type(out).__name__}", error_kind="internal")


def provider_call(op: str, fn, *args, provider: str, deployment_id: str | None = None,
                  route_request_id: str | None = None, **kw):
    """Run one provider verb with a structured log line (logger 'opengrid.provider'). Exceptions are
    returned, not raised, so the caller classifies them. No arguments or bodies are logged (secrets)."""
    t0 = time.perf_counter()
    try:
        out = fn(*args, **kw)
    except Exception as exc:  # noqa: BLE001
        out = exc
    ms = int((time.perf_counter() - t0) * 1000)
    status = (getattr(out, "outcome", None) or getattr(out, "state", None)
              or (getattr(out, "kind", None) and f"error:{out.kind}")
              or (isinstance(out, Exception) and f"exception:{type(out).__name__}") or "ok")
    plog.info("provider %s %s -> %s (%d ms)", provider, op, status,
              ms, extra={"provider": provider, "op": op, "status": str(status), "latency_ms": ms,
                         "deployment_id": deployment_id, "route_request_id": route_request_id,
                         "error_kind": getattr(out, "error_kind", None) or getattr(out, "kind", None),
                         "status_code": getattr(out, "status_code", None)})
    return out, ms


# --------------------------------------------------------------------------
# The one provision call
# --------------------------------------------------------------------------

def launch(dep_id: str, *, adapter, offer, availability, launch_spec, resolved, actor: str = "system",
           actor_id: str | None = None, rank: int | None = None, quote_id: str | None = None,
           after_provider_call=None) -> dict:
    """Make THE provision call for an approved deployment. Returns {outcome, status, failover_allowed, ...}.

    Never calls the provider twice for one deployment: the row lock + status 'approved' + launch_token IS NULL
    check and the conditional state change happen in one committed transaction, with the write-ahead
    provision_attempts row, before the call."""
    now = _now()
    token = secrets.token_hex(16)
    with normalize.SessionLocal.begin() as s:
        d = s.get(Deployment, dep_id, with_for_update=True)
        if d is None:
            raise KeyError(dep_id)
        if d.status != "approved" or d.launch_token is not None:
            return {"outcome": None, "status": d.status, "launched": False, "failover_allowed": False,
                    "reason": f"deployment is {d.status}; its provision call was already made or it is not approved"}
        # The control plane is re-read here, under the row lock, immediately before the provider call: a kill
        # switch (global or provider) or mode change made while the approval was re-validating still wins.
        from routing import control
        allowed, _, why = control.launch_permission(d.provider, purpose=d.purpose or "customer")
        if not allowed:
            _apply(s, d, "pending_approval", reason=f"launch refused at the last check: {why}", actor="system",
                   actor_id=None, now=now, evidence={"code": "launch_not_permitted"})
            return {"outcome": None, "status": "pending_approval", "launched": False, "failover_allowed": False,
                    "code": "launch_not_permitted", "reason": why}
        # THE ATOMIC LIMITS GATE: advisory lock on the account (+ validation locks), count active / uncertain
        # deployments, GPUs, hourly burn and monthly projection in THIS transaction; refuse, or move to
        # provisioning and commit; only then (lock released) call the provider.
        from routing import guards
        violations, block = guards.gate(s, d)
        if block:
            d.limit_violations = violations
            codes = ", ".join(v["code"] for v in block)
            _apply(s, d, "pending_approval", reason=f"launch refused at the provisioning gate: limits exceeded ({codes})",
                   actor="system", actor_id=None, now=now, evidence={"code": "limits_exceeded", "violations": block})
            return {"outcome": None, "status": "pending_approval", "launched": False, "failover_allowed": False,
                    "code": "limits_exceeded", "violations": block,
                    "reason": "limits exceeded at the provisioning gate: " + "; ".join(v["message"] for v in block)}
        key_problem = _operator_key_problem(d, launch_spec, resolved)
        if key_problem:
            _apply(s, d, "pending_approval", reason=f"launch refused: {key_problem}", actor="system", actor_id=None,
                   now=now, evidence={"code": "operator_key_forbidden"})
            return {"outcome": None, "status": "pending_approval", "launched": False, "failover_allowed": False,
                    "code": "operator_key_forbidden", "reason": key_problem}
        if quote_id is not None:
            from routing import quotes
            if quote_id != d.quote_id or not quotes.consume(s, quote_id, dep_id):
                return {"outcome": None, "status": d.status, "launched": False, "failover_allowed": False,
                        "code": "quote_invalid", "reason": "the quote is no longer active (expired, superseded or "
                                                           "already consumed): re-quote and re-approve"}
        d.launch_token = token
        d.client_name = d.client_name or instance_name(dep_id)
        d.credential_source, d.credential_ref = resolved.source, resolved.ref
        d.credential_account_id = resolved.credential_account_id
        cfp = credentials.secret_fingerprint(getattr(resolved, "credentials", None))
        if cfp:   # which SECRET was used (one-way), so a replaced key is never used on this instance
            _meta(d, credential_fingerprint=cfp)
        # Re-stated at launch: launch time + the effective ceiling (never null, never unlimited).
        eff = int(d.effective_max_runtime_minutes or d.max_runtime_minutes or guards.runtime_ceiling(
            d.account_id, None, purpose=d.purpose or "customer")["effective_max_runtime_minutes"])
        d.effective_max_runtime_minutes = d.max_runtime_minutes = eff
        d.terminate_deadline_at = now + timedelta(minutes=eff)
        deadline = d.terminate_deadline_at
        summary = {"provider": offer.provider, "listing_id": offer.listing_id, "gpu": offer.gpu,
                   "gpu_count": offer.gpu_count, "region": getattr(availability, "region", None) or offer.region,
                   "name": d.client_name, "image": launch_spec.image, "disk_gb": launch_spec.disk_gb,
                   "ssh": ("public_key" if launch_spec.ssh_public_key else "key_ref" if launch_spec.ssh_key else None),
                   "ssh_key_fingerprint": d.ssh_key_fingerprint, "operator_access": d.operator_access,
                   "operator_defaults_applied": getattr(launch_spec, "defaults_applied", None),
                   "operator_defaults_withheld": getattr(launch_spec, "defaults_withheld", None),
                   "effective_max_runtime_minutes": eff, "terminate_deadline_at": deadline.isoformat(),
                   "env_names": sorted((launch_spec.env or {}).keys())}
        _apply(s, d, "provisioning", reason="provision call starting", actor=actor, actor_id=actor_id, now=now,
               evidence={"launch_token": token, "quote_id": d.quote_id, "client_name": d.client_name,
                         "credential_ref": resolved.ref})
        att = ProvisionAttempt(deployment_id=dep_id, route_request_id=d.route_request_id, provider=offer.provider,
                               listing_id=offer.listing_id, rank=rank, started_at=now, ok=None, outcome="provisioning",
                               launch_token=token, client_name=d.client_name, credential_ref=resolved.ref,
                               quote_id=d.quote_id, request_summary=summary)
        s.add(att)
        s.flush()
        attempt_id, client_name, rr_id = att.id, d.client_name, d.route_request_id
        fp, op_access, purpose = d.ssh_key_fingerprint, d.operator_access, d.purpose
    # Only the fingerprint is ever logged, never key material.
    log.info("launch %s (%s): ssh key fingerprint %s, operator access %s, auto-terminate at %s", dep_id, purpose,
             fp or "-", op_access or "none", deadline.isoformat())
    auto_term = {"terminate_deadline_at": deadline.isoformat(), "effective_max_runtime_minutes": eff,
                 "basis": "launch time + effective_max_runtime_minutes"}
    _set_log_context(adapter, deployment_id=dep_id, route_request_id=rr_id)
    out, ms = provider_call("provision", adapter.provision, offer, availability, launch_spec, client_name,
                            provider=offer.provider, deployment_id=dep_id, route_request_id=rr_id)
    res = as_provision_result(out)
    if after_provider_call is not None:  # test hook: simulate a crash between provider accept and DB write
        after_provider_call(res)
    for i in range(3):
        try:
            return {**_record_launch(dep_id, attempt_id, res, ms, actor=actor, actor_id=actor_id),
                    "auto_termination": auto_term}
        except IllegalTransition:
            raise
        except Exception:  # noqa: BLE001
            log.exception("recording provision result for %s failed (try %d)", dep_id, i + 1)
            time.sleep(0.2 * (i + 1))
    log.critical("PROVISION RESULT NOT RECORDED deployment=%s provider=%s outcome=%s instance_id=%s: the attempt row "
                 "stays 'provisioning'; reconciliation resolves it via find_instance(%s)",
                 dep_id, offer.provider, res.outcome, res.instance_id, client_name)
    return {"outcome": res.outcome, "status": "provisioning", "launched": True, "recorded": False,
            "failover_allowed": False, "instance_id": res.instance_id, "auto_termination": auto_term,
            "reason": "the provider answered but OpenGrid could not record it; reconciliation will resolve it"}


def _operator_key_problem(d: Deployment, launch_spec, resolved) -> str | None:
    """Defence in depth at the last moment: a customer launch on OpenGrid-managed credentials may carry ONLY
    the customer's own public key: no key-name reference, never the operator's default key material."""
    if (d.purpose or "customer") == "validation" or launch_spec is None:
        return None
    if getattr(resolved, "source", None) == "byo":
        return None
    from config import settings

    if launch_spec.ssh_key:
        return "ssh key references are forbidden on OpenGrid-managed provider accounts"
    defaults = dict((settings.routing_launch_defaults or {}).get(d.provider) or {})
    op = {" ".join(str(v).split()[:2]) for v in (defaults.get("ssh_public_key"), defaults.get("ssh_key")) if v}
    if launch_spec.ssh_public_key and " ".join(launch_spec.ssh_public_key.split()[:2]) in op:
        return "the operator's default ssh key may never be installed on a customer machine"
    return None


def _record_launch(dep_id: str, attempt_id: int, res: ProvisionResult, ms: int, *, actor: str,
                   actor_id: str | None) -> dict:
    now = _now()
    msg = (res.message or "")[:2000]
    with normalize.SessionLocal.begin() as s:
        d = s.get(Deployment, dep_id, with_for_update=True)
        att = s.get(ProvisionAttempt, attempt_id, with_for_update=True)
        att.finished_at, att.latency_ms, att.outcome = now, ms, res.outcome
        att.ok = res.outcome == "accepted"
        att.error_kind = None if res.outcome == "accepted" else (res.error_kind or res.outcome)[:24]
        att.error = None if res.outcome == "accepted" else msg
        att.instance_id = res.instance_id
        att.status_code = res.status_code
        ev = {"outcome": res.outcome, "error_kind": res.error_kind, "status_code": res.status_code,
              "latency_ms": ms, "instance_id": res.instance_id}
        if d.status != "provisioning" and res.outcome != "accepted":
            # Reconciliation already resolved this launch while the call was in flight (adopted the instance
            # by name). Its provider evidence wins; the call's own answer is recorded on the attempt only.
            note_event(s, d, f"provision call answered {res.outcome} after reconciliation moved it to {d.status}",
                       ev, actor="system")
            return {"outcome": res.outcome, "status": d.status, "launched": True, "recorded": True,
                    "failover_allowed": False, "instance_id": d.provider_instance_id, "error_kind": res.error_kind,
                    "latency_ms": ms}
        if res.outcome == "accepted":
            d.provider_instance_id = res.instance_id
            d.provisioned_at = d.provisioned_at or now
            d.last_checked_at = now
            if res.raw_redacted is not None:
                _meta(d, launch_response=res.raw_redacted)
            note_event(s, d, "provider accepted the launch", ev, actor="system")
            new = "provisioning"
        elif res.outcome == "rejected":
            new = "provider_rejected" if (res.error_kind or "") in _ACCOUNT_KINDS else "provision_failed"
            d.failure_reason = f"{res.error_kind or 'rejected'}: provider definitively created nothing"
            _apply(s, d, new, reason=f"provider rejected the launch ({res.error_kind})", evidence=ev, actor="system",
                   actor_id=None, now=now)
        else:
            new = "provider_timeout" if res.error_kind == "timeout" else "launch_unknown"
            d.failure_reason = (f"provision outcome unknown ({res.error_kind}): the provider may have created an "
                                "instance; reconciliation must resolve it before any retry")
            _meta(d, needs_reconciliation=True)
            _apply(s, d, new, reason="ambiguous provision outcome: never failed over", evidence=ev, actor="system",
                   actor_id=None, now=now)
        try:
            transactions.open_record(s, d, ok=res.outcome == "accepted", attempts=1, latency_ms=ms)
        except Exception:  # noqa: BLE001
            log.exception("execution record for %s not opened", dep_id)
        status = d.status
    if res.outcome == "unknown":
        _alert("launch_unknown", dep_id, f"ambiguous provision outcome ({res.error_kind})")
    return {"outcome": res.outcome, "status": status, "launched": True, "recorded": True,
            "failover_allowed": res.outcome == "rejected", "instance_id": res.instance_id,
            "error_kind": res.error_kind, "latency_ms": ms}


# --------------------------------------------------------------------------
# Observation (status reads)
# --------------------------------------------------------------------------

NOT_FOUND_CONFIRM_SECONDS = 60
AUTH_ERROR = "auth"      # AdapterError kind for HTTP 401/403: the provider rejects the credential


def observe(dep_id: str, st: InstanceState, *, checked_at: datetime | None = None, actor: str = "system",
            actor_id: str | None = None, extra_evidence: dict | None = None) -> dict:
    """Apply one provider-reported state. Never raises on a transition the machine forbids (stale or
    out-of-order reads are recorded and ignored)."""
    checked_at = checked_at or _now()
    terminated = False
    with normalize.SessionLocal.begin() as s:
        d = s.get(Deployment, dep_id, with_for_update=True)
        if d is None:
            return {"applied": False, "reason": "no such deployment"}
        if d.last_checked_at and checked_at < d.last_checked_at:
            return {"applied": False, "reason": "stale read (older than the last recorded check)"}
        if d.status in TERMINAL_STATES:
            return {"applied": False, "reason": f"status {d.status} is final"}
        d.last_checked_at = checked_at
        if st.provider_status:
            d.provider_status = str(st.provider_status)[:64]
        price = getattr(st, "_price_per_gpu_hour", None)
        if price is None and st.price_per_hour is not None and d.gpu_count:
            price = float(st.price_per_hour) / d.gpu_count
        if price is not None:
            d.actual_price_per_gpu_hour = _d(price)
        if st.ip:
            _meta(d, ip=st.ip)
        ev = {"state": st.state, "provider_status": st.provider_status, "observed_at": checked_at.isoformat(),
              "instance_id": st.instance_id, **(extra_evidence or {})}
        target = _target(d, st, checked_at, ev)
        md = d.provider_metadata or {}
        if st.state != "not_found" and md.get("not_found_reads"):
            _meta(d, not_found_reads=[])
        if st.state == "unknown":
            _meta(d, last_status_error={"error_kind": st.error_kind, "message": (st.message or "")[:300],
                                        "at": checked_at.isoformat()})
        applied, why = False, None
        cred_lost = (st.state == "unknown" and st.error_kind == AUTH_ERROR and d.status != "credentials_unavailable"
                     and can_transition(d.status, "credentials_unavailable"))
        if cred_lost:
            # The provider rejects the PINNED credential (expired / revoked key): never other credentials,
            # never terminated; the operator is alerted and observation resumes once the key works again.
            _meta(d, status_before_credentials_unavailable=d.status,
                  credentials_error=f"provider rejected the pinned credential: {(st.message or '')[:200]}")
            _apply(s, d, "credentials_unavailable", reason="provider rejected the pinned credential (HTTP 401/403)",
                   evidence=ev, actor=actor, actor_id=actor_id, now=checked_at)
            applied = True
        elif target and target != d.status:
            if d.terminate_requested_at and checked_at < d.terminate_requested_at and target not in ("terminating", "terminated"):
                why = "read taken before terminate was requested; ignored"
            elif can_transition(d.status, target):
                _apply(s, d, target, reason=f"provider reports {st.state}", evidence=ev, actor=actor,
                       actor_id=actor_id, now=checked_at)
                applied = True
            else:
                why = f"{d.status} -> {target} not allowed; read recorded only"
        _sync_record(s, d)
        terminated = applied and d.status == "terminated"
        status = d.status
    if terminated:
        _bill(dep_id)
    if target == "degraded" and applied:
        _alert("degraded", dep_id, f"provider reports an error state ({st.provider_status})")
    if cred_lost:
        _alert("credentials_unavailable", dep_id, "the provider rejected the credential pinned at launch")
    return {"applied": applied, "status": status, "reason": why}


def _target(d: Deployment, st: InstanceState, checked_at: datetime, ev: dict) -> str | None:
    s = st.state
    tr = d.terminate_requested_at is not None
    if s == "terminated":
        return "terminated"
    if s == "not_found":
        reads = list((d.provider_metadata or {}).get("not_found_reads") or [])
        reads.append(checked_at.isoformat())
        _meta(d, not_found_reads=reads[-5:])
        first = datetime.fromisoformat(reads[0])
        if (checked_at - first).total_seconds() >= NOT_FOUND_CONFIRM_SECONDS and len(reads) >= 2:
            ev["basis"] = f"{len(reads)} consecutive not_found reads >= {NOT_FOUND_CONFIRM_SECONDS}s apart"
            ev["not_found_reads"] = reads
            return "terminated"
        return None  # one not_found is never proof of termination
    if s == "unknown":
        return None
    if s == "error":
        return "terminating" if tr and d.status in ("terminating", "termination_failed") else "degraded"
    if tr and s in ("pending", "running", "stopping", "stopped"):
        # terminate was requested: the instance still showing alive keeps us terminating (re-issue via reconcile)
        return "terminating" if d.status in ("credentials_unavailable", "orphan_suspected") else None
    if s == "pending":
        return "provisioning" if d.status in ("provider_timeout", "launch_unknown", "credentials_unavailable",
                                              "orphan_suspected") else None
    if s == "terminating":
        return "terminating"
    return {"running": "running", "stopping": "stopping", "stopped": "stopped"}.get(s)


def refresh(dep_id: str, who=None) -> dict:
    """Ask the provider (with the PINNED credentials) for the current state and record it."""
    d = _load(dep_id, who)
    if d.status not in LIVE_STATES:
        return {"refreshed": False, "reason": f"status {d.status} is not live"}
    if not d.provider_instance_id:
        return {"refreshed": False, "reason": "no instance id yet: reconciliation resolves it via find_instance"}
    try:
        a = adapter_for(d)
    except CredentialsUnavailable as exc:
        mark_credentials_unavailable(dep_id, exc)
        return {"refreshed": False, "reason": "credentials_unavailable: " + exc.message}
    except HTTPException as exc:
        return {"refreshed": False, "reason": exc.detail}
    checked_at = _now()
    try:
        out, _ = provider_call("status", a.status, d.provider_instance_id, provider=d.provider,
                               deployment_id=dep_id, route_request_id=d.route_request_id)
    finally:
        a.close()
    st = as_instance_state(out, d.provider_instance_id)
    r = observe(dep_id, st, checked_at=checked_at)
    return {"refreshed": st.state != "unknown", "state": st.state, **r}


# --------------------------------------------------------------------------
# Terminate / stop (never gated by execution mode or account suspension)
# --------------------------------------------------------------------------

def _load(dep_id: str, who) -> Deployment:
    with normalize.SessionLocal() as s:
        d = s.get(Deployment, dep_id)
    if d is None or (who is not None and who.account_id is not None and d.account_id != who.account_id):
        raise HTTPException(404, "deployment not found")
    return d


def _actor_of(who) -> tuple[str, str | None]:
    if who is None:
        return "system", None
    aid = f"key:{who.key_id}" if who.key_id is not None else (who.kind or "operator")
    return ("admin" if who.has("admin") else "user"), aid


def terminate(dep_id: str, who, *, force: bool = False, reason: str | None = None) -> dict:
    """Request termination. Never marks terminated: moves to terminating and calls the provider; the tracker
    / reconciler confirms. Idempotent: an already-terminating deployment is not re-called unless force.
    force (admin force-terminate, api/routing.py, scope admin) is cross-tenant: a platform-admin API key belongs
    to an account but may stop ANY deployment, including validation deployments (account NULL)."""
    d = _load(dep_id, None if force else who)
    actor, actor_id = _actor_of(who)
    if force:
        actor = "admin"
    why = reason or ("admin force-terminate" if force else "terminate requested")
    if d.status == "terminated":
        return {**public(dep_id), "terminate": {"requested": False, "note": "already terminated"}}
    if d.status in PRE_LAUNCH_STATES:
        with normalize.SessionLocal.begin() as s:
            row = s.get(Deployment, dep_id, with_for_update=True)
            if row.status in PRE_LAUNCH_STATES and row.launch_token is None:
                row.termination_reason = "cancelled_before_launch"
                row.requested_termination_at = row.requested_termination_at or _now()
                _apply(s, row, "rejected", reason=f"cancelled before launch: {why}", actor=actor, actor_id=actor_id,
                       now=_now())
        return {**public(dep_id), "terminate": {"requested": False, "note": "never launched: cancelled"}}
    if d.status in TERMINAL_STATES:
        return {**public(dep_id), "terminate": {"requested": False,
                                                "note": f"deployment is {d.status}: no instance was created"}}
    now = _now()
    with normalize.SessionLocal.begin() as s:
        row = s.get(Deployment, dep_id, with_for_update=True)
        first = row.terminate_requested_at is None
        if first:
            row.terminate_requested_at = now
        # recorded whenever termination is requested, including while the launch outcome is unresolved
        row.requested_termination_at = row.requested_termination_at or now
        if row.termination_reason is None:
            row.termination_reason = "admin_forced" if force else "user_requested"
        _meta(row, requested_action="terminate")
        if not row.provider_instance_id:
            note_event(s, row, f"{why}: instance id unknown; reconciliation terminates it once found",
                       {"client_name": row.client_name}, actor=actor, actor_id=actor_id)
            return_note = "instance id not known yet (launch outcome pending/ambiguous): reconciliation will " \
                          "find it by name and terminate it"
            call = False
        elif row.status == "terminating" and not force and not first:
            return_note, call = "termination already in progress; awaiting provider confirmation", False
        else:
            if row.status != "credentials_unavailable" and can_transition(row.status, "terminating"):
                _apply(s, row, "terminating", reason=why, actor=actor, actor_id=actor_id, now=now)
            call, return_note = True, None
    if not call:
        return {**public(dep_id), "terminate": {"requested": True, "provider_called": False, "note": return_note}}
    d = load_row(dep_id)
    try:
        a = adapter_for(d)
    except CredentialsUnavailable as exc:
        mark_credentials_unavailable(dep_id, exc)
        raise HTTPException(409, {"code": "credentials_unavailable", "message": exc.message,
                                  "deployment": public(dep_id, detail=False),
                                  "note": "termination is recorded as requested; the operator is alerted. OpenGrid "
                                          "never uses other credentials and never marks it terminated unconfirmed"})
    try:
        out, ms = provider_call("terminate", a.terminate, d.provider_instance_id, provider=d.provider,
                                deployment_id=dep_id, route_request_id=d.route_request_id)
    finally:
        a.close()
    tr = as_terminate_result(out)
    ev = {"outcome": tr.outcome, "message": (tr.message or "")[:300], "status_code": tr.status_code,
          "error_kind": getattr(tr, "error_kind", None), "latency_ms": ms}
    with normalize.SessionLocal.begin() as s:
        row = s.get(Deployment, dep_id, with_for_update=True)
        _meta(row, last_terminate=ev | {"at": _now().isoformat()})
        if row.status == "credentials_unavailable" and can_transition(row.status, "terminating"):
            _apply(s, row, "terminating", reason="credentials restored; terminate issued", actor=actor,
                   actor_id=actor_id, now=_now(), evidence=ev)
        cred_lost = tr.outcome == "failed" and getattr(tr, "error_kind", None) == AUTH_ERROR
        if cred_lost and row.status != "credentials_unavailable" and can_transition(row.status, "credentials_unavailable"):
            # an expired/revoked pinned key: the instance is still there; not a refused delete
            _meta(row, status_before_credentials_unavailable=row.status,
                  credentials_error="provider rejected the pinned credential on terminate")
            _apply(s, row, "credentials_unavailable", reason="provider rejected the pinned credential (HTTP 401/403) "
                   "on terminate", evidence=ev, actor="system", actor_id=None, now=_now())
        elif cred_lost:
            note_event(s, row, "terminate call rejected: pinned credential unusable", ev, actor="system")
        elif tr.outcome == "failed" and not getattr(tr, "retryable", False) and can_transition(row.status, "termination_failed"):
            _apply(s, row, "termination_failed", reason="provider refused the delete", evidence=ev, actor="system",
                   actor_id=None, now=_now())
        else:
            note_event(s, row, f"terminate call: {tr.outcome} (awaiting provider confirmation)", ev, actor="system")
    if tr.outcome == "failed" and getattr(tr, "error_kind", None) == AUTH_ERROR:
        _alert("credentials_unavailable", dep_id, "terminate rejected: the pinned credential is no longer accepted")
    elif tr.outcome == "failed":
        _alert("termination_failed", dep_id, tr.message or "provider refused the delete")
    return {**public(dep_id), "terminate": {"requested": True, "provider_called": True, "outcome": tr.outcome,
                                            "note": "terminating: OpenGrid marks it terminated only once the provider "
                                                    "confirms (status or instance list)"}}


def stop(dep_id: str, who) -> dict:
    d = _load(dep_id, who)
    cls = adapters.get(d.provider or "")
    if cls is None or not cls.SUPPORTS_STOP:
        raise HTTPException(409, f"{d.provider}'s API has no stop; terminate instead")
    if d.status not in ("running", "degraded") or not d.provider_instance_id:
        if d.status in ("stopping", "stopped"):
            return {**public(dep_id), "stop": {"requested": False, "note": f"already {d.status}"}}
        raise HTTPException(409, f"deployment is {d.status}")
    actor, actor_id = _actor_of(who)
    try:
        a = adapter_for(d)
    except CredentialsUnavailable as exc:
        mark_credentials_unavailable(dep_id, exc)
        raise HTTPException(409, {"code": "credentials_unavailable", "message": exc.message})
    with normalize.SessionLocal.begin() as s:
        row = s.get(Deployment, dep_id, with_for_update=True)
        _meta(row, requested_action="stop", stop_requested_at=_now().isoformat())
        if can_transition(row.status, "stopping"):
            _apply(s, row, "stopping", reason="stop requested", actor=actor, actor_id=actor_id, now=_now())
    try:
        out, ms = provider_call("stop", a.stop, d.provider_instance_id, provider=d.provider, deployment_id=dep_id,
                                route_request_id=d.route_request_id)
    finally:
        a.close()
    from routing.adapters.base import Instance
    if isinstance(out, (InstanceState, Instance)):
        observe(dep_id, as_instance_state(out, d.provider_instance_id))
        outcome = "accepted"
    else:
        r = as_terminate_result(out)
        outcome = r.outcome
        with normalize.SessionLocal.begin() as s:
            row = s.get(Deployment, dep_id, with_for_update=True)
            note_event(s, row, f"stop call: {r.outcome}", {"message": (r.message or "")[:300],
                                                            "status_code": r.status_code, "latency_ms": ms})
            if r.outcome == "failed" and can_transition(row.status, "running"):
                _apply(s, row, "running", reason="provider refused stop; instance still running", actor="system",
                       actor_id=None, now=_now())
    caps = getattr(cls, "CAPABILITIES", None)
    sb = getattr(caps, "stopped_billing", None) if caps is not None else None
    return {**public(dep_id), "stop": {"requested": True, "outcome": outcome,
                                       "stopped_billing": {"value": sb[0], "evidence": sb[1]} if sb else "unknown",
                                       "note": "a stopped instance may keep billing (storage or full price) "
                                               "depending on the provider; see stopped_billing"}}


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------

def _f(v):
    return None if v is None else float(v)


def _iso(t):
    return None if t is None else t.isoformat()


def view(d: Deployment, record: ExecutionRecord | None = None) -> dict:
    """The public shape. provider_metadata and sealed launch env are never included."""
    from api.common import gpu_slug

    uptime = d.uptime_seconds or 0
    if d.status == "running" and d.running_since:
        uptime += int((_now() - d.running_since).total_seconds())
    cls = adapters.get(d.provider or "")
    launch = {k: v for k, v in (d.launch or {}).items() if k != "env_sealed"}
    out = {
        "deployment_id": d.deployment_id, "status": d.status, "route_request_id": d.route_request_id,
        "purpose": d.purpose or "customer",
        "provider": d.provider, "provider_display": d.provider and provider_meta.meta(d.provider).display_name,
        "provider_instance_id": d.provider_instance_id, "client_name": d.client_name,
        "gpu": d.gpu, "gpu_slug": gpu_slug(d.gpu), "gpu_count": d.gpu_count, "region": d.region,
        "ip": (d.provider_metadata or {}).get("ip"),
        "prices": {
            "currency": "USD", "unit": "per GPU-hour",
            "observed_market_price": _f(d.observed_market_price_per_gpu_hour),
            "list_price": _f(d.list_price_per_gpu_hour),
            "quote": _f(d.quoted_price_per_gpu_hour), "quote_basis": d.quote_basis,
            "execution_price": _f(d.actual_price_per_gpu_hour),
        },
        "quote_id": d.quote_id, "approval_mode": d.approval_mode, "approved_by": d.approved_by,
        "approved_at": _iso(d.approved_at), "limit_violations": d.limit_violations or [],
        "override_limits": bool(d.override_limits), "override_reason": d.override_reason,
        "max_runtime_minutes": d.max_runtime_minutes, "terminate_deadline_at": _iso(d.terminate_deadline_at),
        "effective_max_runtime_minutes": d.effective_max_runtime_minutes,
        "runtime_ceiling_source": d.runtime_ceiling_source,
        "auto_termination": {"terminate_deadline_at": _iso(d.terminate_deadline_at),
                             "effective_max_runtime_minutes": d.effective_max_runtime_minutes,
                             "source": d.runtime_ceiling_source,
                             "basis": ("launch time + effective_max_runtime_minutes" if d.launch_token else
                                       "approval time + effective_max_runtime_minutes (re-stated at launch)"
                                       if d.approved_at else "set at approval")},
        "ssh_access": ssh_access_view(d),
        "requested_termination_at": _iso(d.requested_termination_at),
        "provider_created_at": _iso(d.provider_created_at), "provider_running_at": _iso(d.provider_running_at),
        "provider_terminated_at": _iso(d.provider_terminated_at), "billable_start": _iso(d.billable_start),
        "billable_end": _iso(d.billable_end), "billable_basis": d.billable_basis,
        "created_at": _iso(d.created_at), "provisioned_at": _iso(d.provisioned_at),
        "terminated_at": _iso(d.terminated_at), "last_checked_at": _iso(d.last_checked_at),
        "state_changed_at": _iso(d.state_changed_at), "terminate_requested_at": _iso(d.terminate_requested_at),
        "uptime_seconds": uptime, "interruptions": d.interruptions or 0,
        "failure_reason": d.failure_reason, "termination_reason": d.termination_reason,
        "credential_source": d.credential_source,
        "supports_stop": bool(cls and cls.SUPPORTS_STOP),
        "uncertain": d.status in UNCERTAIN_STATES,
        "launch": launch or None,
        "reconciled_at": _iso(d.reconciled_at),  # provider_reported_cost: added by public() when allowed
    }
    if record is not None:
        out["transaction"] = transactions.as_dict(record)
    return out


def operator_access_label(value: str | None) -> str:
    if not value or value == "none":
        return "NONE"
    return value


def ssh_access_view(d: Deployment) -> dict:
    """{customer_key_fingerprint, operator_access}: who can log in to this machine. Never key material."""
    if (d.purpose or "customer") == "validation":
        return {"customer_key_fingerprint": None, "operator_access": "validation_operator_key",
                "note": "validation deployment on OpenGrid's account: the operator key is used"}
    out = {"customer_key_fingerprint": d.ssh_key_fingerprint,
           "operator_access": operator_access_label(d.operator_access)}
    if (d.operator_access or "").startswith("blocked:"):
        out["note"] = ("the provider may install account-level ssh keys (operator access): an admin must approve "
                       "with allow_provider_account_keys and a reason, or reject")
    return out


def public(dep_id: str, *, detail: bool = True, operator: bool = False) -> dict:
    with normalize.SessionLocal() as s:
        d = s.get(Deployment, dep_id)
        rec = s.get(ExecutionRecord, dep_id)
        out = view(d, rec)
        # What the provider charged OpenGrid is OpenGrid's cost price on its own accounts: only the
        # operator, or a customer whose own (BYO) account paid it, may see it.
        sees_provider_cost = operator or d.credential_source == "byo"
        rec_json = dict(d.reconciliation or {})
        out["provider_reported_cost"] = _f(d.provider_reported_cost) if sees_provider_cost else None
        if not sees_provider_cost:
            for k in ("provider_reported_cost", "unexpected_fees"):
                rec_json.pop(k, None)
            if isinstance(rec_json.get("effective_hourly_rate"), dict):
                rec_json["effective_hourly_rate"] = {
                    k: v for k, v in rec_json["effective_hourly_rate"].items() if k != "per_gpu_hour_provider"}
        out["reconciliation"] = rec_json or None
        if detail:
            out["events"] = [{"at": e.at.isoformat(), "from": e.from_status, "to": e.to_status, "actor": e.actor,
                              "actor_id": e.actor_id, "reason": e.reason, "evidence": e.evidence,
                              "detail": {k: v for k, v in (e.detail or {}).items() if k != "provider_status"}}
                             for e in s.scalars(select(DeploymentEvent).where(DeploymentEvent.deployment_id == dep_id)
                                                .order_by(DeploymentEvent.at, DeploymentEvent.id))]
            out["provision_attempts"] = [
                {"provider": a.provider, "listing_id": a.listing_id, "rank": a.rank, "started_at": a.started_at.isoformat(),
                 "finished_at": _iso(a.finished_at), "latency_ms": a.latency_ms, "ok": a.ok, "outcome": a.outcome,
                 "error_kind": a.error_kind, "status_code": a.status_code,
                 **({"error": a.error, "credential_ref": a.credential_ref} if operator else {})}
                for a in s.scalars(select(ProvisionAttempt).where(ProvisionAttempt.deployment_id == dep_id)
                                   .order_by(ProvisionAttempt.id))]
    return out


def get(dep_id: str, who, *, refresh_now: bool = False) -> dict:
    _load(dep_id, who)
    note = refresh(dep_id, who) if refresh_now else None
    out = public(dep_id, operator=bool(who is not None and who.has("admin")))
    if note is not None:
        out["refresh"] = note
    return out


def list_for(who, status: str | None = None, *, limit: int = 500, offset: int = 0) -> list[dict]:
    q = select(Deployment).order_by(Deployment.created_at.desc())
    if who is not None and who.account_id is not None:
        q = q.where(Deployment.account_id == who.account_id)
    if status == "live":
        q = q.where(Deployment.status.in_(LIVE_STATES))
    elif status == "uncertain":
        q = q.where(Deployment.status.in_(UNCERTAIN_STATES))
    elif status:
        q = q.where(Deployment.status == status)
    with normalize.SessionLocal() as s:
        return [view(d) for d in s.scalars(q.offset(offset).limit(limit))]


def admin_list(state: str | None = None, *, limit: int = 500, offset: int = 0) -> list[dict]:
    """Every account's deployments (admin), with an accrued-cost estimate for live ones."""
    rows = list_for(None, state, limit=limit, offset=offset)
    now = _now()
    for r in rows:
        if r["status"] in LIVE_STATES and r["provisioned_at"]:
            price = r["prices"]["execution_price"] or r["prices"]["quote"]
            hours = (now - datetime.fromisoformat(r["provisioned_at"])).total_seconds() / 3600
            r["accrued_cost_estimate_usd"] = None if price is None else round(price * r["gpu_count"] * hours, 4)
        else:
            r["accrued_cost_estimate_usd"] = None
    return rows


def live_ids() -> list[str]:
    with normalize.SessionLocal() as s:
        return list(s.scalars(select(Deployment.deployment_id).where(Deployment.status.in_(LIVE_STATES))))
