"""Credits and DRAFT invoices. Nothing here charges a card or moves money.

draft(period="2026-10") builds (or rebuilds) one draft invoice per account for that UTC month:
    1. un-apply the previous draft (restore credit balances, drop its period lines, detach charges)
    2. attach every not-yet-invoiced usage charge whose usage period starts in the month
    3. add per-period components of the policy in force at the period start: subscription,
       data_api (successful API-key requests in the month, minus free_requests)
    4. apply credits (unexpired at period start, soonest-expiring first) as negative `credit` lines
       up to the subtotal; balances are decremented
Issued or void invoices are never touched. Rebuilding a draft is idempotent.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import delete, or_, select, update

import normalize
from billing import policy as policies
from store.accounts import Account, Charge, Credit, Invoice, UsageRecord

Q = policies.Q
PERIOD_LINES = ("subscription", "data_api", "credit")


def period_bounds(period: str) -> tuple[datetime, datetime]:
    try:
        y, m = (int(x) for x in period.split("-"))
        start = datetime(y, m, 1, tzinfo=timezone.utc)
    except Exception:
        raise ValueError("period must be YYYY-MM")
    end = datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=timezone.utc)
    return start, end


def add_credit(account_id: int, amount_usd, reason: str, expires_at: datetime | None = None) -> dict:
    amt = Decimal(str(amount_usd)).quantize(Q)
    if amt <= 0:
        raise ValueError("amount must be > 0")
    with normalize.SessionLocal.begin() as s:
        if s.get(Account, account_id) is None:
            raise KeyError(account_id)
        c = Credit(account_id=account_id, amount_usd=amt, remaining_usd=amt, reason=reason or "credit",
                   expires_at=expires_at)
        s.add(c)
        s.flush()
        return credit_dict(c)


def credit_dict(c: Credit) -> dict:
    return {"id": c.id, "account_id": c.account_id, "amount_usd": float(c.amount_usd),
            "remaining_usd": float(c.remaining_usd), "reason": c.reason, "expires_at": c.expires_at,
            "created_at": c.created_at}


def credits_for(account_id: int) -> list[dict]:
    with normalize.SessionLocal() as s:
        return [credit_dict(c) for c in s.scalars(select(Credit).where(Credit.account_id == account_id).order_by(Credit.id))]


def _draft_one(s, account_id: int, period: str, start: datetime, end: datetime) -> dict | None:
    inv = s.scalars(select(Invoice).where(Invoice.account_id == account_id, Invoice.period == period)).first()
    if inv is not None and inv.status != "draft":
        return {"account_id": account_id, "invoice_id": inv.id, "skipped": f"invoice is {inv.status}"}
    if inv is not None:  # 1. un-apply the previous draft
        for line in s.scalars(select(Charge).where(Charge.invoice_id == inv.id, Charge.kind == "credit")):
            c = s.get(Credit, line.credit_id) if line.credit_id else None
            if c is not None:
                c.remaining_usd = (c.remaining_usd - line.amount_usd).quantize(Q)  # amount is negative
        s.execute(delete(Charge).where(Charge.invoice_id == inv.id, Charge.kind.in_(PERIOD_LINES)))
        s.execute(update(Charge).where(Charge.invoice_id == inv.id).values(invoice_id=None))

    usage_ids = select(UsageRecord.id).where(UsageRecord.account_id == account_id,
                                             UsageRecord.period_start >= start, UsageRecord.period_start < end)
    usage_lines = list(s.scalars(select(Charge).where(Charge.account_id == account_id, Charge.invoice_id.is_(None),
                                                       Charge.usage_record_id.in_(usage_ids))))
    p = policies.active(account_id, start, session=s)
    period_lines = []
    for comp in (p.components if p else []):
        if comp["kind"] == "subscription":
            amt = Decimal(str(comp["usd_per_month"])).quantize(Q)
            period_lines.append(("subscription", amt, comp.get("label") or f"Subscription {period}", comp))
        elif comp["kind"] == "data_api":
            from accounts.usage import count_requests

            n = count_requests(account_id, start, end)
            billable = max(0, n - int(Decimal(str(comp.get("free_requests", 0)))))
            amt = (Decimal(billable) / 1000 * Decimal(str(comp["usd_per_1k_requests"]))).quantize(Q)
            if billable:
                period_lines.append(("data_api", amt, comp.get("label") or
                                     f"Data API: {billable} billable of {n} requests @ ${comp['usd_per_1k_requests']}/1k", comp))
    if not usage_lines and not period_lines and inv is None:
        return None
    if inv is None:
        inv = Invoice(account_id=account_id, period=period, period_start=start, period_end=end, status="draft")
        s.add(inv)
        s.flush()
    for line in usage_lines:
        line.invoice_id = inv.id
    for kind, amt, desc, comp in period_lines:
        s.add(Charge(account_id=account_id, invoice_id=inv.id, kind=kind, amount_usd=amt, description=desc,
                     policy_id=p.id, component=comp))
    s.flush()

    totals: dict[str, Decimal] = {}
    for line in s.scalars(select(Charge).where(Charge.invoice_id == inv.id)):
        totals[line.kind] = totals.get(line.kind, Decimal(0)) + line.amount_usd
    subtotal = sum(totals.values(), Decimal(0))

    left, applied = subtotal, Decimal(0)  # 4. credits
    credits = s.scalars(select(Credit).where(Credit.account_id == account_id, Credit.remaining_usd > 0,
                                             or_(Credit.expires_at.is_(None), Credit.expires_at > start))
                        .order_by(Credit.expires_at.asc().nulls_last(), Credit.id).with_for_update())
    for c in credits:
        if left <= 0:
            break
        use = min(c.remaining_usd, left).quantize(Q)
        c.remaining_usd = (c.remaining_usd - use).quantize(Q)
        left -= use
        applied += use
        s.add(Charge(account_id=account_id, invoice_id=inv.id, kind="credit", amount_usd=-use, credit_id=c.id,
                     description=f"Credit #{c.id}: {c.reason}"))
    if applied:
        totals["credit"] = -applied
    inv.subtotal_usd = subtotal.quantize(Q)
    inv.credits_usd = applied.quantize(Q)
    inv.total_usd = (subtotal - applied).quantize(Q)
    inv.totals = {k: float(v) for k, v in totals.items()}
    inv.updated_at = datetime.now(timezone.utc)
    return {"account_id": account_id, "invoice_id": inv.id, "status": inv.status,
            "subtotal_usd": float(inv.subtotal_usd), "credits_usd": float(inv.credits_usd), "total_usd": float(inv.total_usd)}


def draft(period: str, account_id: int | None = None) -> list[dict]:
    start, end = period_bounds(period)
    with normalize.SessionLocal() as s:
        ids = [account_id] if account_id is not None else list(s.scalars(select(Account.id).order_by(Account.id)))
    out = []
    for a in ids:
        with normalize.SessionLocal.begin() as s:
            r = _draft_one(s, a, period, start, end)
        if r is not None:
            out.append(r)
    return out


def invoice_dict(inv: Invoice, lines: list[Charge] | None = None) -> dict:
    d = {"id": inv.id, "account_id": inv.account_id, "period": inv.period, "period_start": inv.period_start,
         "period_end": inv.period_end, "status": inv.status, "subtotal_usd": float(inv.subtotal_usd),
         "credits_usd": float(inv.credits_usd), "total_usd": float(inv.total_usd), "totals": inv.totals,
         "updated_at": inv.updated_at}
    if lines is not None:
        d["lines"] = [charge_dict(c) for c in lines]
    return d


def charge_dict(c: Charge) -> dict:
    return {"id": c.id, "kind": c.kind, "description": c.description, "amount_usd": float(c.amount_usd),
            "usage_record_id": c.usage_record_id, "invoice_id": c.invoice_id, "policy_id": c.policy_id,
            "component": c.component, "created_at": c.created_at}


def invoices_for(account_id: int, with_lines: bool = True) -> list[dict]:
    with normalize.SessionLocal() as s:
        out = []
        for inv in s.scalars(select(Invoice).where(Invoice.account_id == account_id).order_by(Invoice.period.desc())):
            lines = list(s.scalars(select(Charge).where(Charge.invoice_id == inv.id).order_by(Charge.id))) if with_lines else None
            out.append(invoice_dict(inv, lines))
        return out


def usage_for(account_id: int, t0: datetime | None = None, t1: datetime | None = None, limit: int = 500) -> dict:
    """Usage records with their charge lines, newest first, and totals by charge kind."""
    with normalize.SessionLocal() as s:
        q = select(UsageRecord).where(UsageRecord.account_id == account_id)
        if t0:
            q = q.where(UsageRecord.period_start >= t0)
        if t1:
            q = q.where(UsageRecord.period_start < t1)
        recs = list(s.scalars(q.order_by(UsageRecord.period_start.desc()).limit(limit)))
        lines = {}
        if recs:
            for c in s.scalars(select(Charge).where(Charge.usage_record_id.in_([r.id for r in recs])).order_by(Charge.id)):
                lines.setdefault(c.usage_record_id, []).append(charge_dict(c))
    totals: dict[str, float] = {}
    items = []
    for r in recs:
        ls = lines.get(r.id, [])
        for c in ls:
            totals[c["kind"]] = round(totals.get(c["kind"], 0.0) + c["amount_usd"], 6)
        items.append({"id": r.id, "deployment_id": r.deployment_id, "kind": r.kind, "provider": r.provider, "gpu": r.gpu,
                      "gpu_count": r.gpu_count, "period_start": r.period_start, "period_end": r.period_end,
                      "gpu_hours": float(r.gpu_hours), "provider_cost_usd": float(r.provider_cost_usd), "charges": ls})
    return {"records": items, "totals_by_kind": totals,
            "gpu_hours": round(sum(i["gpu_hours"] for i in items), 6)}
