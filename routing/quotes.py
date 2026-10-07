"""Quotes: a priced, expiring offer that every launch must reference.

    issue(...)       write a quote (id q_<hex>), expires after settings.quote_ttl_seconds
    get(quote_id)    the quote as a dict (status recomputed: an active quote past expires_at reads 'expired')
    revalidate(...)  at launch: a FRESH live check/quote through the adapter; refuse if the listing is gone,
                     unavailable, or the price moved more than settings.quote_price_tolerance (either way)
    consume(...)     active -> consumed by exactly one deployment (conditional UPDATE: single use)
    supersede(...)   active -> superseded by a new quote (re-approval needed)

Four price concepts stay apart (methodology/data-kinds.md):
    observed_price_per_gpu_hour   the market observation the ranking used (OBSERVED_MARKET_PRICE)
    quote_price_per_gpu_hour      what OpenGrid quotes (QUOTE): the live-check price when there was one
                                  (price_source 'live_check'), else the observation ('observed')
    list price                    on the availability snapshot (provider catalogue read on the check)
    execution price               only on the deployment, once the provider reports it
Fees are the billing fee policy in force applied to the estimate (billing.policy.price_usage), itemized.
Taxes are not computed: taxes = null with the reason. billing_unit / minimum_commitment come from the
adapter's capability matrix (CAPABILITIES) when it declares them, else 'unknown'.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import update

import normalize
from config import settings
from store.routing import QuoteRow

log = logging.getLogger(__name__)

TAXES = {"amount_usd": None, "note": "unknown: taxes not computed"}


def _now():
    return datetime.now(timezone.utc)


def new_quote_id() -> str:
    return "q_" + secrets.token_hex(10)


def _d(v, q="0.000001"):
    return None if v is None else Decimal(str(v)).quantize(Decimal(q))


def _cap(cls, field: str) -> str | None:
    caps = getattr(cls, "CAPABILITIES", None)
    if caps is None:
        return None
    v = caps.get(field) if isinstance(caps, dict) else getattr(caps, field, None)
    if isinstance(v, (tuple, list)) and v:
        v = v[0]
    return None if v in (None, "") else str(v)[:128]


def fee_preview(account_id: int | None, *, kind: str, hourly_cost: float, gpu_count: int,
                hours: float | None) -> dict:
    """The fee lines the policy in force would charge on this estimate (hours, else one hour)."""
    h = hours if hours is not None else 1.0
    basis = "est_total_cost" if hours is not None else "one hour (no duration given)"
    try:
        from billing import policy as policies
        p = policies.active(account_id)
    except Exception:  # noqa: BLE001 - billing not available: say so, invent nothing
        return {"lines": [], "total_usd": None, "basis": basis, "note": "fee policy unavailable"}
    if p is None:
        return {"lines": [], "total_usd": 0.0, "basis": basis, "policy_id": None,
                "note": "no fee policy in force: provider cost only"}
    cost = Decimal(str(round(hourly_cost * h, 6)))
    gpu_hours = Decimal(str(round(gpu_count * h, 6)))
    lines = policies.price_usage(p.components, kind, cost, gpu_hours)
    return {"policy_id": p.id, "basis": basis,
            "lines": [{"component": ln["component"].get("kind"), "description": ln["description"],
                       "amount_usd": float(ln["amount"])} for ln in lines],
            "total_usd": float(sum((ln["amount"] for ln in lines), Decimal(0)))}


def _offer_dict(offer) -> dict:
    d = asdict(offer)
    for k, v in list(d.items()):
        if isinstance(v, datetime):
            d[k] = v.isoformat()
    return d


def _avail_dict(avail) -> dict | None:
    if avail is None:
        return None
    return {"available": avail.available, "live": avail.live, "region": avail.region,
            "list_price_per_gpu_hour": avail.list_price_per_gpu_hour, "note": avail.note,
            "checked_at": avail.checked_at.isoformat() if avail.checked_at else None,
            "metadata": avail.metadata or {}}


def issue(*, route_request_id: str | None, account_id: int | None, offer, observed_price: float | None,
          quote_price: float, price_source: str, duration_hours: float | None, region_group: str | None,
          availability=None, adapter_cls=None, purpose: str = "customer", credential_source: str | None = None,
          ttl_seconds: int | None = None) -> dict:
    now = _now()
    hourly = float(quote_price) * offer.gpu_count
    total = None if duration_hours is None else hourly * float(duration_hours)
    fees = fee_preview(account_id, kind="byo" if credential_source == "byo" else "compute", hourly_cost=hourly,
                       gpu_count=offer.gpu_count, hours=duration_hours)
    row = QuoteRow(
        id=new_quote_id(), route_request_id=route_request_id, account_id=account_id, provider=offer.provider,
        listing_id=offer.listing_id, offer=_offer_dict(offer), availability=_avail_dict(availability),
        gpu=offer.gpu, gpu_count=offer.gpu_count,
        region=((availability.region if availability is not None else None) or offer.region or None),
        region_group=region_group, observed_price_per_gpu_hour=_d(observed_price),
        quote_price_per_gpu_hour=_d(quote_price), est_hourly_cost=_d(hourly, "0.0001"),
        est_total_cost=_d(total, "0.0001"), duration_hours=_d(duration_hours, "0.001"), fees=fees, taxes=TAXES,
        billing_unit=_cap(adapter_cls, "billing_unit") or "unknown",
        minimum_commitment=_cap(adapter_cls, "minimum_commitment") or "unknown",
        price_source=price_source, purpose=purpose, created_at=now,
        expires_at=now + timedelta(seconds=ttl_seconds or settings.quote_ttl_seconds), status="active")
    if row.region:
        row.region = row.region[:64]
    with normalize.SessionLocal.begin() as s:
        s.add(row)
    return as_dict(row)


def _status(r: QuoteRow) -> str:
    if r.status == "active" and r.expires_at <= _now():
        return "expired"
    return r.status


def as_dict(r: QuoteRow) -> dict:
    f = lambda v: None if v is None else float(v)  # noqa: E731
    return {
        "quote_id": r.id, "kind": "quote", "route_request_id": r.route_request_id, "account_id": r.account_id,
        "provider": r.provider, "listing_id": r.listing_id, "gpu": r.gpu, "gpu_count": r.gpu_count,
        "region": r.region, "region_group": r.region_group,
        "observed_price_per_gpu_hour": f(r.observed_price_per_gpu_hour),
        "quote_price_per_gpu_hour": f(r.quote_price_per_gpu_hour), "price_source": r.price_source,
        "est_hourly_cost": f(r.est_hourly_cost), "est_total_cost": f(r.est_total_cost),
        "duration_hours": f(r.duration_hours), "fees": r.fees, "taxes": r.taxes, "billing_unit": r.billing_unit,
        "minimum_commitment": r.minimum_commitment, "purpose": r.purpose,
        "created_at": r.created_at.isoformat(), "expires_at": r.expires_at.isoformat(),
        "expires_in_seconds": max(0, int((r.expires_at - _now()).total_seconds())),
        "status": _status(r), "consumed_by_deployment_id": r.consumed_by_deployment_id,
        "superseded_by": r.superseded_by,
        "price_concepts": {"observed_price_per_gpu_hour": "observed_market_price",
                           "quote_price_per_gpu_hour": "quote"},
    }


def get(quote_id: str, who=None) -> dict | None:
    with normalize.SessionLocal() as s:
        r = s.get(QuoteRow, quote_id)
    if r is None:
        return None
    if who is not None and who.account_id is not None and r.account_id != who.account_id:
        return None
    return as_dict(r)


def row(quote_id: str) -> QuoteRow | None:
    with normalize.SessionLocal() as s:
        return s.get(QuoteRow, quote_id)


def offer_of(q: dict | QuoteRow):
    from routing.adapters.base import Offer
    d = dict(q.offer if isinstance(q, QuoteRow) else row(q["quote_id"]).offer)
    if d.get("observed_at"):
        d["observed_at"] = datetime.fromisoformat(d["observed_at"])
    return Offer(**{k: d.get(k) for k in Offer.__dataclass_fields__ if k in d})


def usable(r: QuoteRow | None) -> tuple[bool, str]:
    if r is None:
        return False, "quote not found"
    st = _status(r)
    if st != "active":
        return False, f"quote is {st}"
    return True, "ok"


def listing_drift(r: QuoteRow) -> str | None:
    """The quoted listing's market record now names a different canonical GPU (mapping changed) or GPU
    count. A missing row is not drift: the live check is the authority on whether it still exists."""
    from tables import ComputeListingRow
    with normalize.SessionLocal() as s:
        row = s.get(ComputeListingRow, (r.provider, r.listing_id))
    if row is None:
        return None
    if row.canonical_gpu_name != r.gpu:
        return (f"GPU mapping changed: listing {r.listing_id} is now {row.canonical_gpu_name or 'unmapped'} "
                f"(quoted {r.gpu})")
    if row.gpu_count != r.gpu_count:
        return f"instance shape changed: listing {r.listing_id} now has {row.gpu_count} GPUs (quoted {r.gpu_count})"
    return None


def revalidate(r: QuoteRow, adapter) -> dict:
    """Fresh live check through `adapter`. {ok, reason, price, availability, quote(adapter Quote)}."""
    from routing.adapters.base import AdapterError

    ok, why = usable(r)
    expired = r is not None and _status(r) == "expired"
    if not ok and not expired:
        return {"ok": False, "code": "quote_invalid", "reason": why}
    drift = listing_drift(r)
    if drift:   # never launch a GPU other than the one the customer was quoted
        return {"ok": False, "code": "quote_invalid", "reason": drift}
    offer = offer_of(r)
    try:
        avail = adapter.check_availability(offer)
        q = adapter.quote(offer, avail)
    except AdapterError as exc:
        return {"ok": False, "code": "quote_invalid", "reason": f"live re-check failed: {exc.kind}"}
    except Exception as exc:  # noqa: BLE001
        log.exception("re-validation of %s failed", r.id)
        return {"ok": False, "code": "quote_invalid", "reason": f"live re-check failed: {type(exc).__name__}"}
    if avail.available is False:
        return {"ok": False, "code": "quote_invalid", "reason": "listing no longer available on live check",
                "availability": avail, "offer": offer}
    if expired:  # never launch on an expired quote; the fresh check lets the caller re-quote
        return {"ok": False, "code": "quote_expired", "reason": why, "availability": avail, "offer": offer,
                "quote": q, "price": float(q.price_per_gpu_hour)}
    old = float(r.quote_price_per_gpu_hour)
    new = float(q.price_per_gpu_hour)
    moved = abs(new - old) / old if old else (0.0 if new == old else 1.0)
    if moved > settings.quote_price_tolerance:
        return {"ok": False, "code": "quote_invalid", "price": new, "availability": avail, "offer": offer, "quote": q,
                "reason": f"price moved {moved:.1%} (${old:.4f} -> ${new:.4f}/GPU-h), over the "
                          f"{settings.quote_price_tolerance:.0%} tolerance"}
    return {"ok": True, "code": "ok", "reason": "re-validated", "price": new, "availability": avail, "offer": offer,
            "quote": q, "moved": moved}


def consume(s, quote_id: str, deployment_id: str) -> bool:
    """Inside the caller's transaction: active+unexpired -> consumed. False if someone else got it first."""
    got = s.execute(update(QuoteRow).where(QuoteRow.id == quote_id, QuoteRow.status == "active",
                                           QuoteRow.expires_at > _now())
                    .values(status="consumed", consumed_by_deployment_id=deployment_id)
                    .returning(QuoteRow.id)).scalar()
    return got is not None


def supersede(quote_id: str, by_quote_id: str) -> None:
    with normalize.SessionLocal.begin() as s:
        s.execute(update(QuoteRow).where(QuoteRow.id == quote_id, QuoteRow.status == "active")
                  .values(status="superseded", superseded_by=by_quote_id))


def expire(quote_id: str) -> None:
    with normalize.SessionLocal.begin() as s:
        s.execute(update(QuoteRow).where(QuoteRow.id == quote_id, QuoteRow.status == "active")
                  .values(status="expired"))
