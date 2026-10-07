"""Cost guards: hard limits checked when a quote is issued AND again immediately before launch.

Limits per account (table account_limits; a NULL column, or no row, means the settings default):
    max_price_per_gpu_hour   quote price per GPU-hour            (default settings.default_max_price_per_gpu_hour, None = off)
    max_hourly_cost          quote price x GPUs                   (default settings.default_max_hourly_cost = 50)
    max_total_cost           est_total_cost when a duration is known (default settings.default_max_total_cost, None = off)
    max_gpus                 GPUs across the account's ACTIVE deployments + this one (default 8)
    max_active_deployments   ACTIVE deployments + this one         (default 2)
    provider_allowlist       None = any provider
    region_allowlist         None = any; matched against the quote's region group, then region
    monthly_spend_limit      this UTC month: actual cost of finished deployments + estimated cost so far of
                             active ones + this launch's estimate (est_total_cost, else one hour)  (default 2000)
ACTIVE = deployments.ACTIVE_STATES: approved, every state where an instance may exist (uncertain ones too:
an unresolved launch_unknown may be billing).

Validation launches (purpose='validation') additionally, and NOT overridable: one GPU instance, total
price <= settings.validation_max_price_per_hour, max runtime required and <= validation_max_runtime_minutes,
and no other validation deployment active.

A violation never launches: the route stays pending_approval with `limit_violations`; an admin approval may
set override_limits=true with a reason (recorded on the deployment and in execution_control_log). Validation
caps cannot be overridden.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select

import normalize
from config import settings
from store.routing import AccountLimits, Deployment, ExecutionRecord

LIMIT_FIELDS = ("max_price_per_gpu_hour", "max_hourly_cost", "max_total_cost", "max_gpus", "max_active_deployments",
                "provider_allowlist", "region_allowlist", "monthly_spend_limit")


def _f(v):
    return None if v is None else float(v)


def _account_key(account_id: int | None) -> int | None:
    if account_id is not None:
        return account_id
    try:
        from accounts.accounts import operator_account_id
        return operator_account_id()
    except Exception:  # noqa: BLE001
        return None


def defaults() -> dict:
    return {"max_price_per_gpu_hour": settings.default_max_price_per_gpu_hour,
            "max_hourly_cost": settings.default_max_hourly_cost, "max_total_cost": settings.default_max_total_cost,
            "max_gpus": settings.default_max_gpus, "max_active_deployments": settings.default_max_active_deployments,
            "provider_allowlist": None, "region_allowlist": None,
            "monthly_spend_limit": settings.default_monthly_spend_limit}


def limits_for(account_id: int | None) -> dict:
    out = defaults()
    src = {k: "default" for k in out}
    key = _account_key(account_id)
    if key is not None:
        with normalize.SessionLocal() as s:
            row = s.get(AccountLimits, key)
        if row is not None:
            for k in LIMIT_FIELDS:
                v = getattr(row, k)
                if v is not None:
                    out[k] = list(v) if isinstance(v, (list, tuple)) else (int(v) if k in ("max_gpus", "max_active_deployments") else float(v))
                    src[k] = "account"
    return {**out, "source": src, "account_id": key}


def set_limits(account_id: int, values: dict, *, by: str, reason: str) -> dict:
    from routing import control
    reason = control._need_reason(reason)
    with normalize.SessionLocal.begin() as s:
        row = s.get(AccountLimits, account_id, with_for_update=True)
        before = None if row is None else {k: _json(getattr(row, k)) for k in LIMIT_FIELDS}
        if row is None:
            row = AccountLimits(account_id=account_id)
            s.add(row)
        for k, v in values.items():
            if k not in LIMIT_FIELDS:
                raise ValueError(f"unknown limit {k!r}")
            setattr(row, k, v)
        row.updated_at, row.updated_by = datetime.now(timezone.utc), by
        after = {k: _json(getattr(row, k)) for k in LIMIT_FIELDS}
        control.log_action(s, "set_account_limits", f"account:{account_id}", before, after, reason, by)
    return limits_for(account_id)


def _json(v):
    if isinstance(v, Decimal):
        return float(v)
    return list(v) if isinstance(v, (list, tuple)) else v


def _month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def usage(account_id: int | None, *, exclude_deployment_id: str | None = None) -> dict:
    """Active deployments / GPUs, and this month's spend (actual finished + estimated active-to-date)."""
    from routing.deployments import ACTIVE_STATES

    now = datetime.now(timezone.utc)
    m0 = _month_start(now)
    acct = Deployment.account_id.is_(None) if account_id is None else Deployment.account_id == account_id
    with normalize.SessionLocal() as s:
        q = select(Deployment).where(acct, Deployment.status.in_(ACTIVE_STATES))
        if exclude_deployment_id:
            q = q.where(Deployment.deployment_id != exclude_deployment_id)
        active = list(s.scalars(q))
        finished = s.scalar(select(func.coalesce(func.sum(ExecutionRecord.provider_cost_usd), 0))
                            .join(Deployment, Deployment.deployment_id == ExecutionRecord.deployment_id)
                            .where(acct, Deployment.status == "terminated", Deployment.terminated_at >= m0)) or 0
    est_active = 0.0
    for d in active:
        price = d.actual_price_per_gpu_hour or d.quoted_price_per_gpu_hour
        start = d.provisioned_at or d.approved_at
        if price is None or start is None:
            continue
        hours = max(0.0, (now - max(start, m0)).total_seconds() / 3600)
        est_active += float(price) * d.gpu_count * hours
    return {"active_deployments": len(active), "active_gpus": sum(d.gpu_count for d in active),
            "month_finished_cost": float(finished), "month_active_estimated_cost": round(est_active, 4),
            "month_spend": round(float(finished) + est_active, 4), "month_start": m0.isoformat()}


def check(account_id: int | None, *, provider: str, gpu_count: int, price_per_gpu_hour: float,
          est_total_cost: float | None, region: str | None = None, region_group: str | None = None,
          purpose: str = "customer", max_runtime_minutes: int | None = None,
          exclude_deployment_id: str | None = None, limits: dict | None = None,
          use: dict | None = None) -> list[dict]:
    """Every violated limit, [] when the launch is within limits. Each: {code, limit, value, message, overridable}.
    limits / use: precomputed limits_for() / usage() (preview checks several candidates with one read)."""
    lim = limits if limits is not None else limits_for(account_id)
    use = use if use is not None else usage(account_id, exclude_deployment_id=exclude_deployment_id)
    hourly = float(price_per_gpu_hour) * gpu_count
    out: list[dict] = []

    def v(code, limit, value, msg, overridable=True):
        out.append({"code": code, "limit": limit, "value": value, "message": msg, "overridable": overridable})

    if lim["max_price_per_gpu_hour"] is not None and price_per_gpu_hour > lim["max_price_per_gpu_hour"]:
        v("max_price_per_gpu_hour", lim["max_price_per_gpu_hour"], price_per_gpu_hour,
          f"${price_per_gpu_hour:.4f}/GPU-h is over the account limit ${lim['max_price_per_gpu_hour']:.4f}")
    if lim["max_hourly_cost"] is not None and hourly > lim["max_hourly_cost"]:
        v("max_hourly_cost", lim["max_hourly_cost"], round(hourly, 4),
          f"${hourly:.2f}/h is over the account limit ${lim['max_hourly_cost']:.2f}/h")
    if lim["max_total_cost"] is not None and est_total_cost is not None and est_total_cost > lim["max_total_cost"]:
        v("max_total_cost", lim["max_total_cost"], est_total_cost,
          f"estimated total ${est_total_cost:.2f} is over the account limit ${lim['max_total_cost']:.2f}")
    if lim["max_gpus"] is not None and use["active_gpus"] + gpu_count > lim["max_gpus"]:
        v("max_gpus", lim["max_gpus"], use["active_gpus"] + gpu_count,
          f"{use['active_gpus']} active GPUs + {gpu_count} would exceed the limit of {lim['max_gpus']}")
    if lim["max_active_deployments"] is not None and use["active_deployments"] + 1 > lim["max_active_deployments"]:
        v("max_active_deployments", lim["max_active_deployments"], use["active_deployments"] + 1,
          f"{use['active_deployments']} active deployments + 1 would exceed the limit of {lim['max_active_deployments']}")
    if lim["provider_allowlist"] is not None and provider not in lim["provider_allowlist"]:
        v("provider_allowlist", lim["provider_allowlist"], provider, f"{provider} is not in the account's provider allowlist")
    if lim["region_allowlist"] is not None and not ({region_group, region} & set(lim["region_allowlist"])):
        v("region_allowlist", lim["region_allowlist"], region_group or region,
          f"region {region_group or region or 'unknown'} is not in the account's region allowlist")
    if lim["monthly_spend_limit"] is not None:
        projected = use["month_spend"] + (est_total_cost if est_total_cost is not None else hourly)
        if projected > lim["monthly_spend_limit"]:
            v("monthly_spend_limit", lim["monthly_spend_limit"], round(projected, 2),
              f"this month's spend ${use['month_spend']:.2f} + this launch would reach ${projected:.2f}, over "
              f"the ${lim['monthly_spend_limit']:.2f} monthly limit")
    if purpose == "validation":
        out.extend(validation_violations(gpu_count=gpu_count, hourly=hourly, max_runtime_minutes=max_runtime_minutes,
                                         exclude_deployment_id=exclude_deployment_id))
    return out


def validation_violations(*, gpu_count: int, hourly: float, max_runtime_minutes: int | None,
                          exclude_deployment_id: str | None = None) -> list[dict]:
    from routing.deployments import ACTIVE_STATES

    out = []

    def v(code, limit, value, msg):
        out.append({"code": code, "limit": limit, "value": value, "message": msg, "overridable": False})

    if hourly > settings.validation_max_price_per_hour:
        v("validation_max_price_per_hour", settings.validation_max_price_per_hour, round(hourly, 4),
          f"validation launches are capped at ${settings.validation_max_price_per_hour:.2f}/h total; this is ${hourly:.2f}/h")
    cap = settings.validation_max_runtime_minutes
    if max_runtime_minutes is None or max_runtime_minutes > cap or max_runtime_minutes <= 0:
        v("validation_max_runtime_minutes", cap, max_runtime_minutes,
          f"validation launches need a max runtime of at most {cap} minutes (auto-terminate)")
    with normalize.SessionLocal() as s:
        q = select(func.count()).select_from(Deployment).where(Deployment.purpose == "validation",
                                                               Deployment.status.in_(ACTIVE_STATES))
        if exclude_deployment_id:
            q = q.where(Deployment.deployment_id != exclude_deployment_id)
        n = s.scalar(q) or 0
    if n:
        v("validation_one_instance", 1, n + 1, "another validation deployment is still active: one at a time")
    return out


def blocking(violations: list[dict], *, override: bool) -> list[dict]:
    """The violations that still block a launch: all of them, or (admin override) the non-overridable ones."""
    return [v for v in violations if not (override and v.get("overridable"))]
