"""Fee policies: OpenGrid's economics as data, not code.

A policy is a named, versioned list of fee COMPONENTS, either global (account_id null) or an
override for one account. Exactly one policy per scope is in force at any instant:
[effective_from, effective_to). Policies are never edited; a new version closes the previous
one (its effective_to becomes the new effective_from), so every charge line keeps pointing at
the exact terms that priced it (charges.policy_id + charges.component).

Lookup for an account at time t: its own override in force at t, else the global policy in
force at t, else NO policy (only provider cost passes through; nothing is invented).

Components (compose any number; amounts in USD):
    {"kind": "buyer_fee_pct",     "pct": 5}                      pct of provider cost, per usage
    {"kind": "flat_per_gpu_hour", "usd": 0.05}                   per metered GPU-hour, per usage
    {"kind": "spread", "usd_per_gpu_hour": 0.10} | {"pct": 3}    markup over provider cost, per usage
    {"kind": "subscription",      "usd_per_month": 49}           once per invoice period
    {"kind": "data_api", "usd_per_1k_requests": 0.5, "free_requests": 10000}   per invoice period
Optional on any component: "label" (shown on the line), and for per-usage components
"applies_to": ["compute", "byo"] (default both: with BYO credentials the provider bills the
account directly, so only OpenGrid's fee lines are charged).
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import or_, select

import normalize
from store.accounts import FeePolicy

PER_USAGE = ("buyer_fee_pct", "flat_per_gpu_hour", "spread")
PER_PERIOD = ("subscription", "data_api")
KINDS = PER_USAGE + PER_PERIOD
USAGE_KINDS = ("compute", "byo")
Q = Decimal("0.000001")


def _num(c: dict, key: str) -> Decimal:
    try:
        v = Decimal(str(c[key]))
    except Exception:
        raise ValueError(f"component {c.get('kind')!r} needs a numeric {key!r}")
    if v < 0:
        raise ValueError(f"{key} must be >= 0")
    return v


def validate(components) -> list[dict]:
    if not isinstance(components, list):
        raise ValueError("components must be a list")
    out = []
    for c in components:
        if not isinstance(c, dict) or c.get("kind") not in KINDS:
            raise ValueError(f"each component needs kind in {KINDS}")
        k = c["kind"]
        if k == "buyer_fee_pct":
            _num(c, "pct")
        elif k == "flat_per_gpu_hour":
            _num(c, "usd")
        elif k == "spread":
            if ("pct" in c) == ("usd_per_gpu_hour" in c):
                raise ValueError("spread needs exactly one of pct, usd_per_gpu_hour")
            _num(c, "pct" if "pct" in c else "usd_per_gpu_hour")
        elif k == "subscription":
            _num(c, "usd_per_month")
        elif k == "data_api":
            _num(c, "usd_per_1k_requests")
            if "free_requests" in c:
                _num(c, "free_requests")
        applies = c.get("applies_to", list(USAGE_KINDS))
        if not set(applies) <= set(USAGE_KINDS):
            raise ValueError(f"applies_to must be a subset of {USAGE_KINDS}")
        out.append(dict(c))
    return out


def as_dict(p: FeePolicy) -> dict:
    return {"id": p.id, "name": p.name, "account_id": p.account_id, "scope": "account" if p.account_id else "global",
            "version": p.version, "components": p.components, "effective_from": p.effective_from,
            "effective_to": p.effective_to, "note": p.note, "created_at": p.created_at}


def _in_force(q, t):
    return q.where(FeePolicy.effective_from <= t, or_(FeePolicy.effective_to.is_(None), FeePolicy.effective_to > t))


def active(account_id: int | None, t: datetime | None = None, session=None) -> FeePolicy | None:
    t = t or datetime.now(timezone.utc)

    def find(s):
        if account_id is not None:
            p = s.scalars(_in_force(select(FeePolicy).where(FeePolicy.account_id == account_id), t)
                          .order_by(FeePolicy.effective_from.desc()).limit(1)).first()
            if p is not None:
                return p
        return s.scalars(_in_force(select(FeePolicy).where(FeePolicy.account_id.is_(None)), t)
                         .order_by(FeePolicy.effective_from.desc()).limit(1)).first()

    if session is not None:
        return find(session)
    with normalize.SessionLocal() as s:
        return find(s)


def create(name: str, components, account_id: int | None = None, effective_from: datetime | None = None,
           note: str | None = None) -> dict:
    """A new version in its scope. Closes whatever is in force at effective_from."""
    components = validate(components)
    t = effective_from or datetime.now(timezone.utc)
    with normalize.SessionLocal.begin() as s:
        scope = (FeePolicy.account_id == account_id) if account_id is not None else FeePolicy.account_id.is_(None)
        versions = list(s.scalars(select(FeePolicy).where(scope)))
        later = [p.effective_from for p in versions if p.effective_from > t]
        for p in versions:
            if p.effective_from <= t and (p.effective_to is None or p.effective_to > t):
                p.effective_to = t
        new = FeePolicy(name=name, account_id=account_id, components=components, effective_from=t,
                        effective_to=min(later) if later else None, note=note,
                        version=1 + max((p.version for p in versions), default=0))
        s.add(new)
        s.flush()
        return as_dict(new)


def history(account_id: int | None = None) -> list[dict]:
    with normalize.SessionLocal() as s:
        q = select(FeePolicy).order_by(FeePolicy.account_id.nulls_first(), FeePolicy.effective_from)
        if account_id is not None:
            q = q.where(or_(FeePolicy.account_id == account_id, FeePolicy.account_id.is_(None)))
        return [as_dict(p) for p in s.scalars(q)]


def price_usage(components: list[dict], usage_kind: str, provider_cost: Decimal, gpu_hours: Decimal) -> list[dict]:
    """Fee lines for one usage record: [{"component", "amount", "description"}]. Pure."""
    lines = []
    for c in components:
        if c["kind"] not in PER_USAGE or usage_kind not in c.get("applies_to", USAGE_KINDS):
            continue
        k, label = c["kind"], c.get("label")
        if k == "buyer_fee_pct":
            amt = provider_cost * Decimal(str(c["pct"])) / 100
            desc = label or f"OpenGrid fee {c['pct']}% of provider cost"
        elif k == "flat_per_gpu_hour":
            amt = gpu_hours * Decimal(str(c["usd"]))
            desc = label or f"OpenGrid fee ${c['usd']}/GPU-hour x {gpu_hours.normalize():f} GPU-h"
        else:  # spread
            if "pct" in c:
                amt = provider_cost * Decimal(str(c["pct"])) / 100
                desc = label or f"OpenGrid spread {c['pct']}% over provider cost"
            else:
                amt = gpu_hours * Decimal(str(c["usd_per_gpu_hour"]))
                desc = label or f"OpenGrid spread ${c['usd_per_gpu_hour']}/GPU-hour"
        lines.append({"component": c, "amount": amt.quantize(Q), "description": desc})
    return lines
