"""Cost guards and runtime ceilings: hard limits checked when a quote is issued, again ATOMICALLY when a
deployment is approved, and again ATOMICALLY at the moment it moves to provisioning.

Limits per account (table account_limits; a NULL column, or no row, means the settings default):
    max_price_per_gpu_hour   quote price per GPU-hour            (default settings.default_max_price_per_gpu_hour, None = off)
    max_hourly_cost          concurrent burn: quote x GPUs of every ACTIVE deployment + this one
                                                                  (default settings.default_max_hourly_cost = 50)
    max_total_cost           est_total_cost when a duration is known (default settings.default_max_total_cost, None = off)
    max_gpus                 GPUs across the account's ACTIVE deployments + this one (default 8)
    max_active_deployments   ACTIVE deployments + this one         (default 2)
    provider_allowlist       None = any provider
    region_allowlist         None = any; matched against the quote's region group, then region
    monthly_spend_limit      this UTC month: actual cost of finished deployments + the projected cost of every
                             ACTIVE one up to its auto-terminate deadline + this launch's maximum exposure
                             (quote x GPUs x its effective runtime ceiling)                     (default 2000)
    max_runtime_minutes / default_runtime_minutes   runtime ceilings, see runtime_ceiling()
ACTIVE = deployments.ACTIVE_STATES: approved, every state where an instance may exist (uncertain ones too:
an unresolved launch_unknown may be billing).

Atomicity (gate()): the check that lets a deployment become `approved`, and the check that lets it become
`provisioning`, run INSIDE the transaction that makes that state change, after
pg_advisory_xact_lock(account) (+ a global lock and a per-provider lock for validation launches). Counting
happens under the lock, the state change commits, the lock is released, and only THEN is the provider called.
Postgres locks only: correct across processes and workers.

Runtime ceilings (runtime_ceiling()): effective = min(settings.runtime_hard_max_minutes, account
max_runtime_minutes, request | account default_runtime_minutes | settings.runtime_default_minutes); validation
<= settings.validation_max_runtime_minutes. A request above the effective max is CLAMPED (with a note), never
exceeded. Never unlimited.

Validation launches (purpose='validation') additionally, and NOT overridable: one GPU instance, total
price <= settings.validation_max_price_per_hour, max runtime required and <= validation_max_runtime_minutes,
and no other validation deployment active (globally).

A violation never launches: the route stays pending_approval with `limit_violations`; an admin approval may
set override_limits=true with a reason (recorded on the deployment and in execution_control_log); the override
covers exactly the violations present at approval. Validation caps cannot be overridden.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select, text

import normalize
from config import settings
from store.routing import AccountLimits, Deployment, ExecutionRecord, QuoteRow

LIMIT_FIELDS = ("max_price_per_gpu_hour", "max_hourly_cost", "max_total_cost", "max_gpus", "max_active_deployments",
                "provider_allowlist", "region_allowlist", "monthly_spend_limit", "max_runtime_minutes",
                "default_runtime_minutes")
INT_FIELDS = ("max_gpus", "max_active_deployments", "max_runtime_minutes", "default_runtime_minutes")
RUNTIME_SOURCES = ("request", "account", "system_default", "system_hard_max", "validation_cap", "legacy_backfill")

# pg_advisory_xact_lock(int4 namespace, int4 key) namespaces
LOCK_ACCOUNT = 0x4F470001
LOCK_VALIDATION = 0x4F470002           # key 0: the global "one active validation deployment" rule
LOCK_VALIDATION_PROVIDER = 0x4F470003  # key hashtext(provider)


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
            "monthly_spend_limit": settings.default_monthly_spend_limit,
            "max_runtime_minutes": None, "default_runtime_minutes": None}


def limits_for(account_id: int | None, *, s=None) -> dict:
    out = defaults()
    src = {k: "default" for k in out}
    key = _account_key(account_id)
    if key is not None:
        if s is not None:
            row = s.get(AccountLimits, key)
        else:
            with normalize.SessionLocal() as s2:
                row = s2.get(AccountLimits, key)
        if row is not None:
            for k in LIMIT_FIELDS:
                v = getattr(row, k)
                if v is not None:
                    out[k] = list(v) if isinstance(v, (list, tuple)) else (int(v) if k in INT_FIELDS else float(v))
                    src[k] = "account"
    return {**out, "source": src, "account_id": key, "runtime_hard_max_minutes": settings.runtime_hard_max_minutes,
            "runtime_default_minutes": settings.runtime_default_minutes}


def set_limits(account_id: int, values: dict, *, by: str, reason: str) -> dict:
    from routing import control
    reason = control._need_reason(reason)
    for k in ("max_runtime_minutes", "default_runtime_minutes"):
        if values.get(k) is not None and int(values[k]) <= 0:
            raise control.ControlError(f"{k} must be a positive number of minutes (NULL = system default)")
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


# --------------------------------------------------------------------------
# Runtime ceilings
# --------------------------------------------------------------------------

def runtime_ceiling(account_id: int | None, requested: int | None, *, purpose: str = "customer",
                    limits: dict | None = None) -> dict:
    """The effective auto-terminate ceiling for a new deployment. Never None, never unlimited.

    {effective_max_runtime_minutes, runtime_ceiling_source, requested_max_runtime_minutes, clamped, note}"""
    lim = limits if limits is not None else limits_for(account_id)
    hard = max(1, int(settings.runtime_hard_max_minutes))
    acct_max = lim.get("max_runtime_minutes")
    acct_def = lim.get("default_runtime_minutes")
    req = int(requested) if requested else None
    notes = []
    if req:
        value, source = req, "request"
    elif acct_def:
        value, source = int(acct_def), "account"
    else:
        value, source = max(1, int(settings.runtime_default_minutes)), "system_default"
    if acct_max is not None and value > int(acct_max):
        notes.append(f"{source} runtime {value} min clamped to the account maximum of {int(acct_max)} min")
        value, source = max(1, int(acct_max)), "account"
    if value > hard:
        notes.append(f"{source} runtime {value} min clamped to the system hard maximum of {hard} min")
        value, source = hard, "system_hard_max"
    if purpose == "validation":
        vcap = max(1, int(settings.validation_max_runtime_minutes))
        if value > vcap:
            notes.append(f"validation launches are capped at {vcap} min")
            value, source = vcap, "validation_cap"
    return {"effective_max_runtime_minutes": value, "runtime_ceiling_source": source,
            "requested_max_runtime_minutes": req, "clamped": bool(req is not None and value < req),
            "note": "; ".join(notes) or None, "system_hard_max_minutes": hard}


def runtime_ticket(ceiling: dict, *, deadline: datetime | None = None, basis: str | None = None,
                   duration_hours: float | None = None) -> dict:
    """What a quote / approval ticket shows about auto-termination."""
    now = datetime.now(timezone.utc)
    eff = ceiling["effective_max_runtime_minutes"]
    out = {**ceiling, "terminate_deadline_at": deadline.isoformat() if deadline else None,
           "deadline_basis": basis or ("not set yet: launch time + effective_max_runtime_minutes (set at approval, "
                                       "re-stated at launch)"),
           "auto_terminate_at_if_launched_now": (now + timedelta(minutes=eff)).isoformat(),
           "rule": "OpenGrid terminates the instance automatically at terminate_deadline_at; it can never run longer"}
    if duration_hours and float(duration_hours) * 60 > eff:
        out["duration_warning"] = (f"duration_hours {float(duration_hours):g} h is longer than the {eff} min runtime "
                                   "ceiling: the instance WILL be terminated at the deadline; pass max_runtime_minutes "
                                   "(up to the account / system maximum) for a longer run")
    return out


# --------------------------------------------------------------------------
# Usage
# --------------------------------------------------------------------------

def _month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def usage(account_id: int | None, *, exclude_deployment_id: str | None = None, s=None) -> dict:
    """Active deployments / GPUs / hourly burn, and this month's spend: actual finished + estimated active
    (to date, and projected to each active deployment's auto-terminate deadline)."""
    if s is None:
        with normalize.SessionLocal() as s2:
            return usage(account_id, exclude_deployment_id=exclude_deployment_id, s=s2)
    from routing.deployments import ACTIVE_STATES

    now = datetime.now(timezone.utc)
    m0 = _month_start(now)
    acct = Deployment.account_id.is_(None) if account_id is None else Deployment.account_id == account_id
    q = select(Deployment).where(acct, Deployment.status.in_(ACTIVE_STATES))
    if exclude_deployment_id:
        q = q.where(Deployment.deployment_id != exclude_deployment_id)
    active = list(s.scalars(q))
    finished = s.scalar(select(func.coalesce(func.sum(ExecutionRecord.provider_cost_usd), 0))
                        .join(Deployment, Deployment.deployment_id == ExecutionRecord.deployment_id)
                        .where(acct, Deployment.status == "terminated", Deployment.terminated_at >= m0)) or 0
    est_active, proj_active, hourly = 0.0, 0.0, 0.0
    for d in active:
        price = d.actual_price_per_gpu_hour or d.quoted_price_per_gpu_hour
        if price is None:
            continue
        rate = float(price) * d.gpu_count
        hourly += rate
        start = d.provisioned_at or d.approved_at or now
        begin = max(start, m0)
        est_active += rate * max(0.0, (now - begin).total_seconds() / 3600)
        end = max(now, d.terminate_deadline_at) if d.terminate_deadline_at else now + timedelta(
            minutes=d.effective_max_runtime_minutes or settings.runtime_default_minutes)
        proj_active += rate * max(0.0, (end - begin).total_seconds() / 3600)
    return {"active_deployments": len(active), "active_gpus": sum(d.gpu_count for d in active),
            "active_hourly_cost": round(hourly, 4),
            "month_finished_cost": float(finished), "month_active_estimated_cost": round(est_active, 4),
            "month_active_projected_cost": round(proj_active, 4),
            "month_spend": round(float(finished) + est_active, 4),
            "month_projected_spend": round(float(finished) + proj_active, 4), "month_start": m0.isoformat()}


# --------------------------------------------------------------------------
# The check
# --------------------------------------------------------------------------

def check(account_id: int | None, *, provider: str, gpu_count: int, price_per_gpu_hour: float,
          est_total_cost: float | None, region: str | None = None, region_group: str | None = None,
          purpose: str = "customer", max_runtime_minutes: int | None = None,
          exclude_deployment_id: str | None = None, limits: dict | None = None,
          use: dict | None = None, s=None) -> list[dict]:
    """Every violated limit, [] when the launch is within limits. Each: {code, limit, value, message, overridable}.
    limits / use: precomputed limits_for() / usage() (preview checks several candidates with one read).
    max_runtime_minutes: the EFFECTIVE ceiling (computed from the account when None). s: the caller's
    (locked) session, so the count happens in the same transaction as the state change."""
    lim = limits if limits is not None else limits_for(account_id, s=s)
    use = use if use is not None else usage(account_id, exclude_deployment_id=exclude_deployment_id, s=s)
    if max_runtime_minutes is None:
        max_runtime_minutes = runtime_ceiling(account_id, None, purpose=purpose,
                                              limits=lim)["effective_max_runtime_minutes"]
    hourly = float(price_per_gpu_hour) * gpu_count
    out: list[dict] = []

    def v(code, limit, value, msg, overridable=True):
        out.append({"code": code, "limit": limit, "value": value, "message": msg, "overridable": overridable})

    if lim["max_price_per_gpu_hour"] is not None and price_per_gpu_hour > lim["max_price_per_gpu_hour"]:
        v("max_price_per_gpu_hour", lim["max_price_per_gpu_hour"], price_per_gpu_hour,
          f"${price_per_gpu_hour:.4f}/GPU-h is over the account limit ${lim['max_price_per_gpu_hour']:.4f}")
    running = float(use.get("active_hourly_cost", 0.0))
    burn = running + hourly
    if lim["max_hourly_cost"] is not None and burn > lim["max_hourly_cost"] + 1e-9:
        v("max_hourly_cost", lim["max_hourly_cost"], round(burn, 4),
          f"${running:.2f}/h already active + ${hourly:.2f}/h would be ${burn:.2f}/h, over the account limit "
          f"${lim['max_hourly_cost']:.2f}/h")
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
        exposure = hourly * max_runtime_minutes / 60
        base = float(use.get("month_projected_spend", use["month_spend"]))
        projected = base + exposure
        if projected > lim["monthly_spend_limit"] + 1e-9:
            v("monthly_spend_limit", lim["monthly_spend_limit"], round(projected, 2),
              f"this month's spend (actual ${use['month_finished_cost']:.2f} + active projected to their deadlines "
              f"${use.get('month_active_projected_cost', 0.0):.2f}) + this launch's maximum ${exposure:.2f} "
              f"({max_runtime_minutes} min ceiling) would reach ${projected:.2f}, over the "
              f"${lim['monthly_spend_limit']:.2f} monthly limit")
    if purpose == "validation":
        out.extend(validation_violations(gpu_count=gpu_count, hourly=hourly, max_runtime_minutes=max_runtime_minutes,
                                         exclude_deployment_id=exclude_deployment_id, s=s))
    return out


def validation_violations(*, gpu_count: int, hourly: float, max_runtime_minutes: int | None,
                          exclude_deployment_id: str | None = None, s=None) -> list[dict]:
    out = []

    def v(code, limit, value, msg):
        out.append({"code": code, "limit": limit, "value": value, "message": msg, "overridable": False})

    if gpu_count != 1:
        v("validation_one_gpu", 1, gpu_count, f"validation launches use exactly one GPU; this has {gpu_count}")
    if hourly > settings.validation_max_price_per_hour:
        v("validation_max_price_per_hour", settings.validation_max_price_per_hour, round(hourly, 4),
          f"validation launches are capped at ${settings.validation_max_price_per_hour:.2f}/h total; this is ${hourly:.2f}/h")
    cap = settings.validation_max_runtime_minutes
    if max_runtime_minutes is None or max_runtime_minutes > cap or max_runtime_minutes <= 0:
        v("validation_max_runtime_minutes", cap, max_runtime_minutes,
          f"validation launches need a max runtime of at most {cap} minutes (auto-terminate)")
    n = active_validation_count(exclude_deployment_id=exclude_deployment_id, s=s)
    if n:
        v("validation_one_instance", 1, n + 1,
          "another validation deployment is still active or uncertain: one at a time")
    return out


def active_validation_count(*, exclude_deployment_id: str | None = None, s=None) -> int:
    from routing.deployments import ACTIVE_STATES

    q = select(func.count()).select_from(Deployment).where(Deployment.purpose == "validation",
                                                           Deployment.status.in_(ACTIVE_STATES))
    if exclude_deployment_id:
        q = q.where(Deployment.deployment_id != exclude_deployment_id)
    if s is not None:
        return s.scalar(q) or 0
    with normalize.SessionLocal() as s2:
        return s2.scalar(q) or 0


def blocking(violations: list[dict], *, override: bool) -> list[dict]:
    """The violations that still block a launch: all of them, or (admin override) the non-overridable ones."""
    return [v for v in violations if not (override and v.get("overridable"))]


# --------------------------------------------------------------------------
# The atomic gate (approval and provisioning)
# --------------------------------------------------------------------------

def lock(s, account_id: int | None, *, purpose: str, provider: str | None) -> None:
    """Transaction-scoped Postgres advisory locks, always taken in the same order (account, validation global,
    validation provider). Released at COMMIT / ROLLBACK of `s`'s transaction."""
    key = _account_key(account_id)
    s.execute(text("SELECT pg_advisory_xact_lock(:ns, :k)"), {"ns": LOCK_ACCOUNT, "k": int(key or 0)})
    if purpose == "validation":
        s.execute(text("SELECT pg_advisory_xact_lock(:ns, 0)"), {"ns": LOCK_VALIDATION})
        s.execute(text("SELECT pg_advisory_xact_lock(:ns, hashtext(:p))"),
                  {"ns": LOCK_VALIDATION_PROVIDER, "p": provider or ""})


def gate(s, d: Deployment, *, override: bool | None = None) -> tuple[list[dict], list[dict]]:
    """Inside the caller's transaction (which must also make the state change): take the advisory locks, then
    count and check against committed state. Returns (violations, blocking).

    override None: the deployment's RECORDED override (override_limits), which covers only the violation codes
    recorded at approval; True / False: an approval deciding now (True covers every overridable violation)."""
    purpose = d.purpose or "customer"
    lock(s, d.account_id, purpose=purpose, provider=d.provider)
    q = s.get(QuoteRow, d.quote_id) if d.quote_id else None
    price = float(q.quote_price_per_gpu_hour) if q is not None else float(d.quoted_price_per_gpu_hour or 0)
    # The re-validated live price recorded at approval (engine._launch_price): a move inside the quote tolerance
    # may be UP, and the launch pays that price, so every limit is checked against the higher of the two.
    reval = (d.provider_metadata or {}).get("revalidated_price_per_gpu_hour")
    if reval is not None:
        price = max(price, float(reval))
    violations = check(d.account_id, provider=d.provider, gpu_count=d.gpu_count, price_per_gpu_hour=price,
                       est_total_cost=_f(q.est_total_cost) if q is not None else None,
                       region=d.region or (q.region if q is not None else None),
                       region_group=q.region_group if q is not None else None, purpose=purpose,
                       max_runtime_minutes=d.effective_max_runtime_minutes, exclude_deployment_id=d.deployment_id, s=s)
    if override is None:
        covered = {v.get("code") for v in (d.limit_violations or [])} if d.override_limits else set()
        block = [v for v in violations if not (v.get("overridable") and v.get("code") in covered)]
    else:
        block = blocking(violations, override=override)
    return violations, block
