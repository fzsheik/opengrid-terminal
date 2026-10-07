"""Deployment lifecycle: records, status transitions, refresh from the provider, stop, terminate.

Statuses
    routing       created by /v1/route; candidates are being tried
    provisioning  a provider accepted the launch; not yet running
    running       the provider reports it running
    stopped       stopped (by us on request, or by the provider: an interruption)
    failed        no provider accepted it, or the provider reports it failed
    terminating   terminate requested; the provider has not confirmed it gone
    terminated    gone at the provider

A deployment exists only when live provisioning actually ran: a route with live
provisioning disabled creates none, so no record can claim compute that was never launched.

Uptime is OpenGrid-observed: summed over stretches between status checks that saw it
running, so it is accurate to the tracker interval, not to the provider's billing clock.
An interruption is a running deployment leaving "running" without OpenGrid having asked.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select

import normalize
import provider_meta
from routing import adapters, credentials, transactions
from routing.adapters.base import (
    FAILED, NOT_FOUND, PROVISIONING, RUNNING, STOPPED, TERMINATED, TERMINATING, AdapterError, Instance,
)
from routing.audit import new_id, redacted
from store.routing import Deployment, DeploymentEvent, ExecutionRecord, ProvisionAttempt

log = logging.getLogger(__name__)

STATUSES = ("pending", "routing", "provisioning", "running", "stopped", "failed", "terminating", "terminated")
LIVE = ("provisioning", "running", "stopped", "terminating")
CANON = {PROVISIONING: "provisioning", RUNNING: "running", STOPPED: "stopped", FAILED: "failed",
         TERMINATING: "terminating", TERMINATED: "terminated"}   # "unknown" keeps the previous status


def _now():
    return datetime.now(timezone.utc)


def _d(v) -> Decimal | None:
    return None if v is None else Decimal(str(round(float(v), 6)))


def _meta(d: Deployment, **kv) -> None:
    d.provider_metadata = {**(d.provider_metadata or {}), **kv}   # reassign so JSONB change is seen


def transition(s, d: Deployment, new: str, now: datetime, detail: dict | None = None) -> None:
    prev = d.status
    if new == prev:
        return
    requested = (d.provider_metadata or {}).get("requested_action")
    if prev == "running":
        if d.running_since is not None:
            d.uptime_seconds = (d.uptime_seconds or 0) + int((now - d.running_since).total_seconds())
            d.running_since = None
        ours = (new == "stopped" and requested == "stop") or (new in ("terminating", "terminated")
                                                             and d.termination_reason == "user_requested")
        if not ours and new in ("stopped", "failed", "terminating", "terminated"):
            d.interruptions = (d.interruptions or 0) + 1
            detail = {**(detail or {}), "interruption": True}
    if new == "running":
        d.running_since = now
        if d.provisioned_at is None:
            d.provisioned_at = now
    if new in ("terminated", "failed") and d.termination_reason is None and prev in LIVE:
        d.termination_reason = "provider_failed" if new == "failed" else "provider_terminated"
    if new == "terminated" and d.terminated_at is None:
        d.terminated_at = now
    d.status = new
    s.add(DeploymentEvent(deployment_id=d.deployment_id, at=now, from_status=prev, to_status=new, detail=detail))


def create(rr_id: str, who, spec: dict) -> str:
    dep_id = new_id("dep")
    now = _now()
    with normalize.SessionLocal.begin() as s:
        d = Deployment(deployment_id=dep_id, account_id=who.account_id, key_id=who.key_id, route_request_id=rr_id,
                       gpu=spec["gpu"], gpu_count=spec["count"], status="pending", created_at=now,
                       uptime_seconds=0, interruptions=0, launch=redacted(spec).get("launch") or None, provider_metadata={})
        s.add(d)
        s.flush()
        transition(s, d, "routing", now, {"mode": spec.get("mode")})
    return dep_id


def record_attempt(dep_id: str, rr_id: str, cand: dict, started: datetime, latency_ms: int, ok: bool,
                   err: AdapterError | None) -> None:
    with normalize.SessionLocal.begin() as s:
        s.add(ProvisionAttempt(deployment_id=dep_id, route_request_id=rr_id, provider=cand["provider"],
                               listing_id=cand["listing_id"], rank=cand.get("rank"), started_at=started,
                               finished_at=_now(), latency_ms=latency_ms, ok=ok,
                               error_kind=err.kind if err else None, error=err.message[:2000] if err else None))


def provisioned(dep_id: str, cand: dict, inst: Instance, quote, avail, source: str, latency_ms: int,
                attempts: int) -> None:
    now = _now()
    with normalize.SessionLocal.begin() as s:
        d = s.get(Deployment, dep_id, with_for_update=True)
        d.provider, d.listing_id, d.provider_instance_id = cand["provider"], cand["listing_id"], inst.instance_id
        d.region = (inst.region or quote.region or cand.get("region") or "")[:64] or None
        d.observed_market_price_per_gpu_hour = _d(cand["price_per_gpu_hour"])
        d.list_price_per_gpu_hour = _d(avail.list_price_per_gpu_hour)
        d.quoted_price_per_gpu_hour = _d(quote.price_per_gpu_hour)
        d.quote_basis = quote.basis
        d.actual_price_per_gpu_hour = _d(inst.price_per_gpu_hour)
        d.credential_source = source
        d.provider_status = inst.provider_status
        d.provisioned_at = now
        d.last_checked_at = now
        _meta(d, **{"instance": inst.metadata, "ip": inst.ip, "availability": avail.metadata,
                    "availability_note": avail.note})
        transition(s, d, "provisioning", now, {"provider": cand["provider"], "latency_ms": latency_ms})
        if inst.status in CANON and CANON[inst.status] != "provisioning":
            transition(s, d, CANON[inst.status], now, {"provider_status": inst.provider_status})
        transactions.open_record(s, d, ok=True, attempts=attempts, latency_ms=latency_ms)


def failed(dep_id: str, reason: str, attempts: int, **meta) -> None:
    now = _now()
    with normalize.SessionLocal.begin() as s:
        d = s.get(Deployment, dep_id, with_for_update=True)
        d.failure_reason = reason[:4000]
        if meta:
            _meta(d, **meta)
        transition(s, d, "failed", now, {"reason": reason[:500]})
        transactions.open_record(s, d, ok=False, attempts=attempts, latency_ms=None)


def apply(dep_id: str, inst: Instance, *, detail: dict | None = None) -> None:
    """Apply a provider-reported state (status / stop / terminate result) to the record."""
    now = _now()
    terminated = False
    with normalize.SessionLocal.begin() as s:
        d = s.get(Deployment, dep_id, with_for_update=True)
        d.last_checked_at = now
        d.provider_status = inst.provider_status
        if inst.price_per_gpu_hour is not None:
            d.actual_price_per_gpu_hour = _d(inst.price_per_gpu_hour)
        if inst.ip:
            _meta(d, ip=inst.ip)
        new = CANON.get(inst.status)
        if new is not None:
            if new == "failed" and d.failure_reason is None:
                d.failure_reason = f"provider reported failure (status {inst.provider_status})"
            transition(s, d, new, now, {"provider_status": inst.provider_status, **(detail or {})})
        transactions.sync(s, d)
        terminated = d.status == "terminated"
    if terminated:
        transactions.bill(dep_id)


def _adapter(d: Deployment):
    if not d.provider or not d.provider_instance_id:
        raise HTTPException(409, "this deployment never reached a provider")
    creds, _ = credentials.resolve(d.account_id, d.provider)
    a = adapters.build(d.provider, creds)
    if a is None:
        raise HTTPException(409, f"no adapter for {d.provider}")
    if a.missing_credentials():
        raise HTTPException(409, f"no credentials to manage {d.provider} deployments")
    return a


def _load(dep_id: str, who) -> Deployment:
    with normalize.SessionLocal() as s:
        d = s.get(Deployment, dep_id)
    if d is None or (who is not None and who.account_id is not None and d.account_id != who.account_id):
        raise HTTPException(404, "deployment not found")
    return d


def refresh(dep_id: str, who=None) -> dict:
    """Ask the provider for the current state and record it. Errors are recorded, not raised."""
    d = _load(dep_id, who)
    if d.status not in LIVE:
        return {"refreshed": False, "reason": f"status {d.status} is final"}
    try:
        a = _adapter(d)
    except HTTPException as exc:
        return {"refreshed": False, "reason": exc.detail}
    try:
        inst = a.status(d.provider_instance_id)
    except AdapterError as exc:
        if exc.kind == NOT_FOUND:
            apply(dep_id, Instance(d.provider_instance_id, TERMINATED, "not_found"),
                  detail={"note": "instance no longer exists at the provider"})
            return {"refreshed": True, "note": "instance not found at provider; marked terminated"}
        with normalize.SessionLocal.begin() as s:
            row = s.get(Deployment, dep_id, with_for_update=True)
            row.last_checked_at = _now()
            _meta(row, last_status_error=exc.as_dict())
        return {"refreshed": False, "reason": exc.message}
    finally:
        a.close()
    apply(dep_id, inst)
    return {"refreshed": True}


def terminate(dep_id: str, who) -> dict:
    d = _load(dep_id, who)
    if d.status == "terminated":
        return public(dep_id)
    if d.status in ("routing", "pending", "failed") and not d.provider_instance_id:
        raise HTTPException(409, f"deployment is {d.status}; there is no instance to terminate")
    a = _adapter(d)
    with normalize.SessionLocal.begin() as s:
        row = s.get(Deployment, dep_id, with_for_update=True)
        row.termination_reason = "user_requested"
        _meta(row, requested_action="terminate", terminate_requested_at=_now().isoformat())
    try:
        inst = a.terminate(d.provider_instance_id)
    except AdapterError as exc:
        if exc.kind != NOT_FOUND:
            raise HTTPException(502, f"provider refused terminate: {exc.message}")
        inst = Instance(d.provider_instance_id, TERMINATED, "not_found")
    finally:
        a.close()
    apply(dep_id, inst, detail={"action": "terminate"})
    return public(dep_id)


def stop(dep_id: str, who) -> dict:
    d = _load(dep_id, who)
    cls = adapters.get(d.provider or "")
    if cls is None or not cls.SUPPORTS_STOP:
        raise HTTPException(409, f"{d.provider}'s API has no stop; terminate instead")
    if d.status not in ("running", "provisioning"):
        raise HTTPException(409, f"deployment is {d.status}")
    a = _adapter(d)
    with normalize.SessionLocal.begin() as s:
        row = s.get(Deployment, dep_id, with_for_update=True)
        _meta(row, requested_action="stop", stop_requested_at=_now().isoformat())
    try:
        inst = a.stop(d.provider_instance_id)
    except AdapterError as exc:
        raise HTTPException(502, f"provider refused stop: {exc.message}")
    finally:
        a.close()
    apply(dep_id, inst, detail={"action": "stop"})
    return public(dep_id)


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------

def _f(v):
    return None if v is None else float(v)


def _iso(t):
    return None if t is None else t.isoformat()


def view(d: Deployment, record: ExecutionRecord | None = None) -> dict:
    """The public shape. provider_metadata (provider-specific fields) is never included."""
    from api.common import gpu_slug

    uptime = d.uptime_seconds or 0
    if d.status == "running" and d.running_since:
        uptime += int((_now() - d.running_since).total_seconds())
    cls = adapters.get(d.provider or "")
    out = {
        "deployment_id": d.deployment_id, "status": d.status, "route_request_id": d.route_request_id,
        "provider": d.provider, "provider_display": d.provider and provider_meta.meta(d.provider).display_name,
        "provider_instance_id": d.provider_instance_id,
        "gpu": d.gpu, "gpu_slug": gpu_slug(d.gpu), "gpu_count": d.gpu_count, "region": d.region,
        "ip": (d.provider_metadata or {}).get("ip"),
        "prices": {
            "currency": "USD", "unit": "per GPU-hour",
            "observed_market_price": _f(d.observed_market_price_per_gpu_hour),
            "list_price": _f(d.list_price_per_gpu_hour),
            "quote": _f(d.quoted_price_per_gpu_hour), "quote_basis": d.quote_basis,
            "execution_price": _f(d.actual_price_per_gpu_hour),
        },
        "created_at": _iso(d.created_at), "provisioned_at": _iso(d.provisioned_at),
        "terminated_at": _iso(d.terminated_at), "last_checked_at": _iso(d.last_checked_at),
        "uptime_seconds": uptime, "interruptions": d.interruptions or 0,
        "failure_reason": d.failure_reason, "termination_reason": d.termination_reason,
        "credential_source": d.credential_source,
        "supports_stop": bool(cls and cls.SUPPORTS_STOP),
    }
    if record is not None:
        out["transaction"] = transactions.as_dict(record)
    return out


def public(dep_id: str, *, detail: bool = True) -> dict:
    with normalize.SessionLocal() as s:
        d = s.get(Deployment, dep_id)
        rec = s.get(ExecutionRecord, dep_id)
        out = view(d, rec)
        if detail:
            out["events"] = [{"at": e.at.isoformat(), "from": e.from_status, "to": e.to_status,
                              "detail": {k: v for k, v in (e.detail or {}).items() if k != "provider_status"}}
                             for e in s.scalars(select(DeploymentEvent).where(DeploymentEvent.deployment_id == dep_id)
                                                .order_by(DeploymentEvent.at, DeploymentEvent.id))]
            out["provision_attempts"] = [
                {"provider": a.provider, "listing_id": a.listing_id, "rank": a.rank, "started_at": a.started_at.isoformat(),
                 "latency_ms": a.latency_ms, "ok": a.ok, "error_kind": a.error_kind, "error": a.error}
                for a in s.scalars(select(ProvisionAttempt).where(ProvisionAttempt.deployment_id == dep_id)
                                   .order_by(ProvisionAttempt.id))]
    return out


def get(dep_id: str, who, *, refresh_now: bool = False) -> dict:
    _load(dep_id, who)
    note = refresh(dep_id, who) if refresh_now else None
    out = public(dep_id)
    if note is not None:
        out["refresh"] = note
    return out


def list_for(who, status: str | None = None) -> list[dict]:
    q = select(Deployment).order_by(Deployment.created_at.desc())
    if who.account_id is not None:
        q = q.where(Deployment.account_id == who.account_id)
    if status:
        q = q.where(Deployment.status == status)
    with normalize.SessionLocal() as s:
        return [view(d) for d in s.scalars(q.limit(1000))]


def live_ids() -> list[str]:
    with normalize.SessionLocal() as s:
        return list(s.scalars(select(Deployment.deployment_id).where(Deployment.status.in_(LIVE))))
