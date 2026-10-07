"""Metered compute -> usage record + priced charge lines. Called by routing for each deployment period.

    record_usage(account_id=..., deployment_id="dep_...", provider="lambda", gpu="NVIDIA H100 80GB SXM5",
                 gpu_count=8, period_start=t0, period_end=t1, provider_cost_usd=Decimal("23.92"))

Lines written (all `transaction` data; see methodology/billing.md):
    compute   provider cost passed through (kind="compute" only; with kind="byo" the provider bills
              the account directly, so no compute line)
    fee       one line per per-usage component of the fee policy in force at period_start
Credits are NOT applied here; they are applied when an invoice draft is built.

Idempotent on (deployment_id, period_start, period_end): a repeat call returns the existing id.
account_id None (the operator's own deployments) records against the operator account.
gpu_hours (optional) overrides the default period length x gpu_count: an hour slice that was partly
stopped is charged only for its billable seconds.

Incremental metering (routing/tracker.py, methodology/reconciliation.md):

    record_usage_slice(deployment_id=..., period_start=hour, period_end=hour+1h (or the end time),
                       running_seconds=..., stopped_seconds=..., stopped_billing="storage_only", ...)

writes one usage_slices row per deployment per UTC hour (idempotent on (deployment_id, period_start))
and, when the slice has billable seconds, ONE usage record for exactly that slice via record_usage.
Hour slices never cross a month boundary, so invoices (selected by period_start month) see each hour
in the month it happened. Stopped seconds bill at the GPU rate only when the provider bills stopped
instances in full (stopped_billing 'full'); 'storage_only' / 'none' record the seconds at $0 GPU time
(OpenGrid does not meter storage; it shows up in cost reconciliation as a provider fee).
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

import normalize
from billing import policy as policies
from store.accounts import Charge, UsageRecord


def record_usage(*, account_id: int | None, deployment_id: str, provider: str, gpu: str, gpu_count: int,
                 period_start: datetime, period_end: datetime, provider_cost_usd: Decimal,
                 kind: str = "compute", gpu_hours: Decimal | None = None) -> int:
    if kind not in policies.USAGE_KINDS:
        raise ValueError(f"kind must be one of {policies.USAGE_KINDS}")
    if period_end < period_start:
        raise ValueError("period_end before period_start")
    if gpu_count < 1:
        raise ValueError("gpu_count must be >= 1")
    cost = Decimal(str(provider_cost_usd))
    if cost < 0:
        raise ValueError("provider_cost_usd must be >= 0")
    if account_id is None:
        from accounts.accounts import operator_account_id

        account_id = operator_account_id()
    if gpu_hours is None:
        gpu_hours = Decimal(gpu_count) * Decimal(str((period_end - period_start).total_seconds())) / 3600
    gpu_hours = Decimal(str(gpu_hours)).quantize(policies.Q)
    if gpu_hours < 0:
        raise ValueError("gpu_hours must be >= 0")

    def existing():
        with normalize.SessionLocal() as s:
            return s.scalar(select(UsageRecord.id).where(
                UsageRecord.deployment_id == deployment_id, UsageRecord.period_start == period_start,
                UsageRecord.period_end == period_end))

    found = existing()
    if found is not None:
        return found
    try:
        with normalize.SessionLocal.begin() as s:
            u = UsageRecord(account_id=account_id, deployment_id=deployment_id, kind=kind, provider=provider, gpu=gpu,
                            gpu_count=gpu_count, period_start=period_start, period_end=period_end,
                            gpu_hours=gpu_hours, provider_cost_usd=cost.quantize(policies.Q))
            s.add(u)
            s.flush()
            if kind == "compute":
                s.add(Charge(account_id=account_id, usage_record_id=u.id, kind="compute", amount_usd=cost.quantize(policies.Q),
                             description=f"{gpu} x{gpu_count} on {provider}, {gpu_hours.normalize():f} GPU-h (provider cost)"))
            p = policies.active(account_id, period_start, session=s)
            if p is not None:
                for line in policies.price_usage(p.components, kind, cost, gpu_hours):
                    s.add(Charge(account_id=account_id, usage_record_id=u.id, kind="fee", amount_usd=line["amount"],
                                 description=line["description"], policy_id=p.id, component=line["component"]))
            return u.id
    except IntegrityError:  # a concurrent duplicate won the unique constraint
        found = existing()
        if found is None:
            raise
        return found


# --------------------------------------------------------------------------
# Incremental metering: hour slices
# --------------------------------------------------------------------------

BILLABLE_STOPPED = ("full",)


def slice_cost(*, gpu_count: int, running_seconds: int, stopped_seconds: int, stopped_billing: str | None,
               price_per_gpu_hour) -> tuple[int, Decimal]:
    """(billable_seconds, cost_usd) for one slice. Stopped time bills only under 'full'."""
    billable = int(running_seconds) + (int(stopped_seconds) if stopped_billing in BILLABLE_STOPPED else 0)
    if price_per_gpu_hour is None or billable <= 0:
        return max(billable, 0), Decimal("0")
    cost = Decimal(str(price_per_gpu_hour)) * Decimal(gpu_count) * Decimal(billable) / Decimal(3600)
    return billable, cost.quantize(policies.Q)


def _month(t: datetime) -> tuple[int, int]:
    t = t.astimezone(timezone.utc)
    return t.year, t.month


def record_usage_slice(*, deployment_id: str, account_id: int | None, provider: str, gpu: str, gpu_count: int,
                       period_start: datetime, period_end: datetime, running_seconds: int, stopped_seconds: int = 0,
                       unbilled_seconds: int = 0, stopped_billing: str | None = None, price_per_gpu_hour=None,
                       price_basis: str | None = None, kind: str = "compute", end_estimated: bool = False,
                       final: bool = False, detail: dict | None = None) -> int:
    """Write (once) the slice for (deployment_id, period_start) and its usage record. Returns the slice id.

    A repeat call for the same (deployment, period_start) returns the existing slice and only completes a
    missing usage record (crash between the two writes); it never re-prices or double-bills."""
    from sqlalchemy.dialects.postgresql import insert

    from store.reconcile import UsageSlice

    if kind not in policies.USAGE_KINDS:
        raise ValueError(f"kind must be one of {policies.USAGE_KINDS}")
    if period_end <= period_start:
        raise ValueError("period_end must be after period_start")
    last_instant = period_end - (period_end - period_start) / 1_000_000
    if _month(period_start) != _month(last_instant):
        raise ValueError("a usage slice may not cross a month boundary")
    span = (period_end - period_start).total_seconds()
    if running_seconds + stopped_seconds + unbilled_seconds > span + 1:
        raise ValueError("slice seconds exceed the slice period")
    if price_per_gpu_hour is None and running_seconds > 0:
        raise ValueError("a slice with running time needs a price")
    billable, cost = slice_cost(gpu_count=gpu_count, running_seconds=running_seconds, stopped_seconds=stopped_seconds,
                                stopped_billing=stopped_billing, price_per_gpu_hour=price_per_gpu_hour)
    now = datetime.now(timezone.utc)
    with normalize.SessionLocal.begin() as s:
        stmt = insert(UsageSlice).values(
            deployment_id=deployment_id, account_id=account_id, provider=provider, gpu=gpu, gpu_count=gpu_count,
            period_start=period_start, period_end=period_end, running_seconds=int(running_seconds),
            stopped_seconds=int(stopped_seconds), unbilled_seconds=int(unbilled_seconds), billable_seconds=billable,
            stopped_billing=stopped_billing,
            price_per_gpu_hour=None if price_per_gpu_hour is None else Decimal(str(price_per_gpu_hour)),
            price_basis=price_basis, cost_usd=cost, kind=kind, end_estimated=end_estimated, final=final,
            detail=detail, created_at=now,
        ).on_conflict_do_nothing(constraint="uq_usage_slice_dep_period").returning(UsageSlice.id)
        sid = s.execute(stmt).scalar()
        if sid is None:
            sid = s.scalar(select(UsageSlice.id).where(UsageSlice.deployment_id == deployment_id,
                                                       UsageSlice.period_start == period_start))
    complete_slice(sid)
    return sid


def complete_slice(slice_id: int) -> int | None:
    """Ensure a billable slice has its usage record (idempotent). Returns the usage record id or None."""
    from store.reconcile import UsageSlice

    with normalize.SessionLocal() as s:
        sl = s.get(UsageSlice, slice_id)
        if sl is None or sl.usage_record_id is not None or sl.billable_seconds <= 0:
            return None if sl is None else sl.usage_record_id
        args = dict(account_id=sl.account_id, deployment_id=sl.deployment_id, provider=sl.provider, gpu=sl.gpu,
                    gpu_count=sl.gpu_count, period_start=sl.period_start, period_end=sl.period_end,
                    provider_cost_usd=sl.cost_usd, kind=sl.kind,
                    gpu_hours=Decimal(sl.gpu_count) * Decimal(sl.billable_seconds) / Decimal(3600))
    uid = record_usage(**args)
    with normalize.SessionLocal.begin() as s:
        sl = s.get(UsageSlice, slice_id, with_for_update=True)
        if sl.usage_record_id is None:
            sl.usage_record_id = uid
    return uid


def incomplete_slices(limit: int = 500) -> list[int]:
    """Billable slices whose usage record was not written yet (retried by the tracker)."""
    from store.reconcile import UsageSlice

    with normalize.SessionLocal() as s:
        return list(s.scalars(select(UsageSlice.id).where(UsageSlice.usage_record_id.is_(None),
                                                          UsageSlice.billable_seconds > 0)
                              .order_by(UsageSlice.id).limit(limit)))


def slices_for(deployment_id: str) -> list[dict]:
    """A deployment's metered slices, oldest first (transaction data)."""
    from store.reconcile import UsageSlice

    f = lambda v: None if v is None else float(v)  # noqa: E731
    with normalize.SessionLocal() as s:
        rows = s.scalars(select(UsageSlice).where(UsageSlice.deployment_id == deployment_id)
                         .order_by(UsageSlice.period_start)).all()
        return [{"period_start": r.period_start.isoformat(), "period_end": r.period_end.isoformat(),
                 "running_seconds": r.running_seconds, "stopped_seconds": r.stopped_seconds,
                 "unbilled_seconds": r.unbilled_seconds, "billable_seconds": r.billable_seconds,
                 "stopped_billing": r.stopped_billing, "price_per_gpu_hour": f(r.price_per_gpu_hour),
                 "price_basis": r.price_basis, "cost_usd": f(r.cost_usd), "kind": r.kind,
                 "usage_record_id": r.usage_record_id, "end_estimated": r.end_estimated, "final": r.final}
                for r in rows]
