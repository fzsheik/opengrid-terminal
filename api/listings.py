"""Price listings: every offer as the provider publishes it (docs/API_CONTRACT.md section 13).

    GET /v1/listings           current public listings, cheapest first; filters gpu, provider, region_group,
                               market_type, available; each row with its FX provenance and trust block
    GET /v1/listings/history   recorded changes (price / availability / capacity), newest first, keyset cursor

A listing row is observed (normalized list price, USD per GPU-hour); trust is inferred. Internal-tier listings are
never served. Replaces /v1/trust/listings and /v1/fx/listings.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text

import fx
import normalize
import regions
from accounts.auth import Principal, require_scope
from quality import trust
from api.common import OBSERVED, cursor_page, envelope, resolve_gpu

router = APIRouter()
READ = require_scope("data:read")

_COLS = ("provider, listing_id, sku, raw_gpu_name, canonical_gpu_name, gpu_count, region, country, "
         "price_per_gpu_hour, price_per_instance_hour, currency, market_type, provider_tier, interruptible, "
         "available, capacity, capacity_unit, vcpu, ram_gb, storage_gb, observed_at, first_seen_at, "
         "original_currency, original_price_per_gpu_hour, original_price_per_instance_hour, fx_rate, fx_rate_date, "
         "fx_source")


def _num(v):
    return None if v is None else float(v)


def listing_row(r: dict, trust_ctx) -> dict:
    """One compute_listings row -> the public listing object."""
    original = None
    if r["original_currency"] and r["original_currency"] != "USD":
        original = {"currency": r["original_currency"],
                    "price_per_gpu_hour": _num(r["original_price_per_gpu_hour"]),
                    "price_per_instance_hour": _num(r["original_price_per_instance_hour"]),
                    "fx": {"rate": _num(r["fx_rate"]), "date": r["fx_rate_date"] and r["fx_rate_date"].isoformat(),
                           "source": r["fx_source"],
                           "legs": fx.legs_for(r["fx_rate_date"], r["original_currency"])
                           if r["fx_source"] == fx.CROSS_SOURCE else None}}
    return {"provider": r["provider"], "listing_id": r["listing_id"], "sku": r["sku"],
            "gpu": r["canonical_gpu_name"], "raw_gpu_name": r["raw_gpu_name"], "gpu_count": r["gpu_count"],
            "region": r["region"], "country": r["country"],
            "region_group": regions.region_group(r["provider"], r["region"], r["country"]) or "Unassigned",
            "market_type": r["market_type"], "interruptible": r["interruptible"], "provider_tier": r["provider_tier"],
            "price_per_gpu_hour": _num(r["price_per_gpu_hour"]),
            "price_per_instance_hour": _num(r["price_per_instance_hour"]), "currency": r["currency"],
            "original": original,
            "available": r["available"], "capacity": r["capacity"], "capacity_unit": r["capacity_unit"],
            "vcpu": r["vcpu"], "ram_gb": r["ram_gb"], "storage_gb": r["storage_gb"],
            "observed_at": r["observed_at"].isoformat(), "first_seen_at": r["first_seen_at"].isoformat(),
            "trust": trust.listing_trust(r, trust_ctx)}


@router.get("/v1/listings", summary="Current price listings: every offer, cheapest first, with provenance")
def listings(gpu: str | None = None, provider: str | None = None, region_group: str | None = None,
             market_type: str | None = None, available: bool | None = None,
             limit: int = Query(100, ge=1, le=1000), cursor: str | None = None, who: Principal = Depends(READ)):
    q = f"SELECT {_COLS} FROM compute_listings WHERE visibility = 'public'"  # public read: public tier only
    params: dict = {}
    if gpu:
        q += " AND canonical_gpu_name = :g"
        params["g"] = resolve_gpu(gpu)
    if provider:
        q += " AND provider = :p"
        params["p"] = provider
    if market_type:
        q += " AND market_type = :m"
        params["m"] = market_type
    if available is not None:
        q += " AND available IS NOT DISTINCT FROM :a"
        params["a"] = available
    q += " ORDER BY price_per_gpu_hour NULLS LAST, provider, listing_id"
    with normalize.SessionLocal() as s:
        rows = [dict(r._mapping) for r in s.execute(text(q), params)]
    if region_group:
        rows = [r for r in rows if (regions.region_group(r["provider"], r["region"], r["country"]) or "Unassigned")
                .lower() == region_group.lower()]
    items, pg = cursor_page(rows, limit, cursor)
    ctx = trust.context(sorted({r["provider"] for r in items}) or [provider or ""],
                        [(r["provider"], r["listing_id"]) for r in items])
    return envelope([listing_row(r, ctx) for r in items], kind=OBSERVED, methodology="data-trust",
                    price_concept="list_price", trust_kind="inferred", **pg)


def _enc(t: datetime, provider: str, listing_id: str) -> str:
    return base64.urlsafe_b64encode(json.dumps([t.isoformat(), provider, listing_id]).encode()).decode().rstrip("=")


def _dec(cursor: str) -> tuple:
    try:
        t, p, lid = json.loads(base64.urlsafe_b64decode(cursor.encode() + b"==").decode())
        return datetime.fromisoformat(t), p, lid
    except Exception:
        raise HTTPException(400, {"code": "invalid_cursor", "param": "cursor",
                                  "message": "cursor is not one this API returned; omit it to start over"})


@router.get("/v1/listings/history", summary="Recorded listing changes (price, availability, capacity), newest first")
def listings_history(gpu: str | None = None, provider: str | None = None, listing_id: str | None = None,
                     from_: datetime | None = Query(None, alias="from"), to: datetime | None = None,
                     limit: int = Query(100, ge=1, le=1000), cursor: str | None = None,
                     who: Principal = Depends(READ)):
    q = ("SELECT o.provider, o.listing_id, o.observed_at, o.price_per_gpu_hour, o.price_per_instance_hour, "
         "o.available, o.capacity, o.capacity_unit, c.canonical_gpu_name, c.gpu_count, c.region, c.market_type "
         "FROM listing_observations o JOIN compute_listings c USING (provider, listing_id) "
         "WHERE c.visibility = 'public'")
    params: dict = {"n": limit + 1}
    if gpu:
        q += " AND c.canonical_gpu_name = :g"
        params["g"] = resolve_gpu(gpu)
    if provider:
        q += " AND o.provider = :p"
        params["p"] = provider
    if listing_id:
        q += " AND o.listing_id = :l"
        params["l"] = listing_id
    if from_:
        q += " AND o.observed_at >= :t0"
        params["t0"] = from_
    if to:
        q += " AND o.observed_at < :t1"
        params["t1"] = to
    if cursor:  # keyset: strictly after the last row of the previous page in (observed_at desc, provider, id)
        params["ct"], params["cp"], params["cl"] = _dec(cursor)
        q += " AND (o.observed_at < :ct OR (o.observed_at = :ct AND (o.provider, o.listing_id) > (:cp, :cl)))"
    q += " ORDER BY o.observed_at DESC, o.provider, o.listing_id LIMIT :n"
    with normalize.SessionLocal() as s:
        rows = s.execute(text(q), params).all()
    more, rows = len(rows) > limit, rows[:limit]
    out = [{"provider": r.provider, "listing_id": r.listing_id, "observed_at": r.observed_at.isoformat(),
            "gpu": r.canonical_gpu_name, "gpu_count": r.gpu_count, "region": r.region, "market_type": r.market_type,
            "price_per_gpu_hour": _num(r.price_per_gpu_hour), "price_per_instance_hour": _num(r.price_per_instance_hour),
            "available": r.available, "capacity": r.capacity, "capacity_unit": r.capacity_unit} for r in rows]
    nxt = _enc(rows[-1].observed_at, rows[-1].provider, rows[-1].listing_id) if more and rows else None
    return envelope(out, kind=OBSERVED, methodology="data-trust", price_concept="list_price",
                    note="change-only: a row is written when price, availability or capacity changes",
                    limit=limit, next_cursor=nxt)
