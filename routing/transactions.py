"""Execution (transaction) records and the billing hook.

One execution_records row per deployment: quoted vs actual price, whether provisioning
succeeded, how long it took, how many attempts, uptime, interruptions, why it ended, and
(when the caller tells us) whether the workload completed. Kind: transaction. These are
the only source reliability scoring may ever use; until enough exist, scoring does not.

Cost, at termination: price x GPUs x instance lifetime (provisioned_at -> terminated_at),
with price = the provider-reported execution price when known (cost_basis "actual"), else
the quote (cost_basis "quote"). The provider's invoice is authoritative; no invoice
reconciliation is implemented. billing.usage.record_usage is idempotent per period, so
retrying after a failure cannot double-charge.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select

import normalize
from store.routing import Deployment, ExecutionRecord

log = logging.getLogger(__name__)


def open_record(s, d: Deployment, *, ok: bool, attempts: int, latency_ms: int | None) -> None:
    now = datetime.now(timezone.utc)
    r = s.get(ExecutionRecord, d.deployment_id)
    if r is None:
        r = ExecutionRecord(deployment_id=d.deployment_id, created_at=now, uptime_seconds=0, interruptions=0)
        s.add(r)
    r.account_id, r.route_request_id, r.provider = d.account_id, d.route_request_id, d.provider
    r.gpu, r.gpu_count = d.gpu, d.gpu_count
    r.observed_market_price_per_gpu_hour = d.observed_market_price_per_gpu_hour
    r.quoted_price_per_gpu_hour = d.quoted_price_per_gpu_hour
    r.actual_price_per_gpu_hour = d.actual_price_per_gpu_hour
    r.provision_ok, r.attempts, r.provision_latency_ms = ok, attempts, latency_ms
    r.updated_at = now


def sync(s, d: Deployment) -> None:
    """Copy the deployment's running totals onto its record."""
    r = s.get(ExecutionRecord, d.deployment_id)
    if r is None:
        return
    r.actual_price_per_gpu_hour = d.actual_price_per_gpu_hour
    r.uptime_seconds, r.interruptions = d.uptime_seconds, d.interruptions
    r.termination_reason = d.termination_reason
    r.updated_at = datetime.now(timezone.utc)
    if d.status == "terminated" and r.provider_cost_usd is None:
        cost, basis = cost_of(d)
        r.provider_cost_usd, r.cost_basis = cost, basis


def cost_of(d: Deployment) -> tuple[Decimal | None, str | None]:
    if d.provisioned_at is None or d.terminated_at is None:
        return None, None
    price, basis = d.actual_price_per_gpu_hour, "actual"
    if price is None:
        price, basis = d.quoted_price_per_gpu_hour, "quote"
    if price is None:
        return None, None
    hours = Decimal(str(max(0.0, (d.terminated_at - d.provisioned_at).total_seconds()))) / 3600
    return (Decimal(str(price)) * d.gpu_count * hours).quantize(Decimal("0.0001")), basis


def bill(deployment_id: str) -> int | None:
    """Send a terminated deployment's usage to billing once. Returns the usage record id, or None."""
    with normalize.SessionLocal() as s:
        d = s.get(Deployment, deployment_id)
        r = s.get(ExecutionRecord, deployment_id)
        if d is None or r is None or d.status != "terminated" or r.usage_record_id is not None:
            return None if r is None else r.usage_record_id
        if r.provider_cost_usd is None or d.provisioned_at is None:
            return None
        args = dict(account_id=d.account_id, deployment_id=d.deployment_id, provider=d.provider, gpu=d.gpu,
                    gpu_count=d.gpu_count, period_start=d.provisioned_at, period_end=d.terminated_at,
                    provider_cost_usd=r.provider_cost_usd,
                    kind="byo" if d.credential_source == "byo" else "compute")
    try:
        from billing.usage import record_usage
    except ImportError:
        log.warning("billing.usage not available; usage for %s not recorded yet", deployment_id)
        return None
    try:
        uid = record_usage(**args)
    except Exception:
        log.exception("record_usage failed for %s; will retry", deployment_id)
        return None
    with normalize.SessionLocal.begin() as s:
        r = s.get(ExecutionRecord, deployment_id)
        r.usage_record_id = uid
        r.updated_at = datetime.now(timezone.utc)
    return uid


def unbilled() -> list[str]:
    with normalize.SessionLocal() as s:
        return list(s.scalars(
            select(ExecutionRecord.deployment_id).join(Deployment, Deployment.deployment_id == ExecutionRecord.deployment_id)
            .where(Deployment.status == "terminated", ExecutionRecord.usage_record_id.is_(None),
                   ExecutionRecord.provider_cost_usd.is_not(None))))


def record_outcome(deployment_id: str, workload_completed: bool) -> None:
    """The caller's own report of whether the workload finished (OpenGrid cannot observe it)."""
    with normalize.SessionLocal.begin() as s:
        r = s.get(ExecutionRecord, deployment_id)
        if r is not None:
            r.workload_completed = workload_completed
            r.updated_at = datetime.now(timezone.utc)


def as_dict(r: ExecutionRecord) -> dict:
    f = lambda v: None if v is None else float(v)  # noqa: E731
    return {
        "kind": "transaction",
        "observed_market_price_per_gpu_hour": f(r.observed_market_price_per_gpu_hour),
        "quoted_price_per_gpu_hour": f(r.quoted_price_per_gpu_hour),
        "execution_price_per_gpu_hour": f(r.actual_price_per_gpu_hour),
        "provision_ok": r.provision_ok, "provision_latency_ms": r.provision_latency_ms, "attempts": r.attempts,
        "uptime_seconds": r.uptime_seconds, "interruptions": r.interruptions,
        "termination_reason": r.termination_reason, "workload_completed": r.workload_completed,
        "provider_cost_usd": f(r.provider_cost_usd), "cost_basis": r.cost_basis,
        "usage_recorded": r.usage_record_id is not None,
    }
