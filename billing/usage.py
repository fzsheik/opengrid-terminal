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
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

import normalize
from billing import policy as policies
from store.accounts import Charge, UsageRecord


def record_usage(*, account_id: int | None, deployment_id: str, provider: str, gpu: str, gpu_count: int,
                 period_start: datetime, period_end: datetime, provider_cost_usd: Decimal,
                 kind: str = "compute") -> int:
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
    gpu_hours = (Decimal(gpu_count) * Decimal(str((period_end - period_start).total_seconds())) / 3600).quantize(policies.Q)

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
