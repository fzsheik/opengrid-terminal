"""Provider-reported capacity: how much of each SKU a provider says it can supply, by region.

    GET /v1/capacity          current state per (provider, sku, region); filters gpu, provider, in_stock
    GET /v1/capacity/history  state changes, newest first; filters gpu, provider, sku, region, days

Every value is the provider's own report, read from its API (kind: observed). Shapes differ by provider and are
kept, not flattened: `in_stock` is comparable everywhere, `tier` and the counts only where the provider gives them.
Public tier only: rows from an internal source (provider_meta.INTERNAL_SOURCES) are never returned here.
Methodology: /methodology/capacity, /methodology/data-visibility.
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, or_, select, tuple_

import capacity
import normalize
import regions
from accounts.auth import Principal, require_scope
from api.common import OBSERVED, cursor_page, envelope, keyset_cursor, keyset_values, resolve_gpu
from providers import PROVIDERS
from store.capacity import CapacityChange, CapacityCurrent

router = APIRouter()
READ = require_scope("data:read")
METHOD = "capacity"
STALE_AFTER_POLLS = 3  # a row not refreshed within 3 of its provider's capacity intervals is flagged stale


def _provider(p: str | None) -> str | None:
    if p and p not in capacity.supported():
        raise HTTPException(404, f"no capacity data for provider {p!r}; supported: {capacity.supported()}")
    return p


def _row(r: CapacityCurrent, now: datetime) -> dict:
    cls = PROVIDERS.get(r.provider)
    interval = cls.polling.capacity_seconds if cls else 900  # the capacity cadence (PollingPolicy)
    return {"provider": r.provider, "sku": r.sku, "region": r.region, "country": r.country,
            "region_group": regions.region_group(r.provider, r.region, r.country),
            "gpu": r.canonical_gpu_name, "raw_gpu_name": r.raw_gpu_name, "gpu_count": r.gpu_count,
            "in_stock": r.in_stock, "tier": r.tier, "available_count": r.available_count,
            "total_count": r.total_count, "count_unit": r.count_unit, "raw_value": r.raw_value,
            "observed_at": r.observed_at, "changed_at": r.changed_at, "first_seen_at": r.first_seen_at,
            "stale": (now - r.observed_at).total_seconds() > STALE_AFTER_POLLS * interval}


@router.get("/v1/capacity", tags=["capacity"], summary="Provider-reported capacity, by SKU and region")
def current(gpu: str | None = Query(None, description="GPU slug or canonical name"), provider: str | None = None,
            in_stock: bool | None = None, limit: int = Query(100, ge=1, le=1000), cursor: str | None = None,
            who: Principal = Depends(READ)):
    q = (select(CapacityCurrent).where(CapacityCurrent.visibility == "public")
         .order_by(CapacityCurrent.provider, CapacityCurrent.sku, CapacityCurrent.region_key))
    if gpu:
        q = q.where(CapacityCurrent.canonical_gpu_name == resolve_gpu(gpu))
    if _provider(provider):
        q = q.where(CapacityCurrent.provider == provider)
    if in_stock is not None:
        q = q.where(CapacityCurrent.in_stock.is_(in_stock))
    now = datetime.now(timezone.utc)
    with normalize.SessionLocal() as s:
        rows = [_row(r, now) for r in s.scalars(q)]
    items, pg = cursor_page(rows, limit, cursor)
    return envelope(items, kind=OBSERVED, methodology=METHOD, count=len(rows), providers=capacity.supported(), **pg)


@router.get("/v1/capacity/history", tags=["capacity"], summary="Capacity state changes, newest first")
def history(gpu: str | None = None, provider: str | None = None, sku: str | None = None, region: str | None = None,
            days: int = Query(7, ge=1, le=90), limit: int = Query(500, ge=1, le=5000), cursor: str | None = None,
            who: Principal = Depends(READ)):
    q = select(CapacityChange).where(CapacityChange.visibility == "public",
                                     CapacityChange.observed_at >= datetime.now(timezone.utc) - timedelta(days=days))
    if gpu:
        q = q.where(CapacityChange.canonical_gpu_name == resolve_gpu(gpu))
    if _provider(provider):
        q = q.where(CapacityChange.provider == provider)
    if sku:
        q = q.where(CapacityChange.sku == sku)
    if region is not None:
        q = q.where(CapacityChange.region_key == region)
    C = CapacityChange
    if cursor:  # keyset: strictly after the previous page's last row in (observed_at desc, provider, sku, region)
        t, p, k, rk = keyset_values(cursor, 4)
        q = q.where(or_(C.observed_at < t, and_(C.observed_at == t, tuple_(C.provider, C.sku, C.region_key) > (p, k, rk))))
    with normalize.SessionLocal() as s:
        got = list(s.scalars(q.order_by(C.observed_at.desc(), C.provider, C.sku, C.region_key).limit(limit + 1)))
    more, got = len(got) > limit, got[:limit]
    rows = [{"provider": r.provider, "sku": r.sku, "region": r.region_key or None, "gpu": r.canonical_gpu_name,
             "observed_at": r.observed_at, "in_stock": r.in_stock, "tier": r.tier,
             "available_count": r.available_count, "total_count": r.total_count, "raw_value": r.raw_value}
            for r in got]
    last = got[-1] if more and got else None
    return envelope(rows, kind=OBSERVED, methodology=METHOD, days=days, count=len(rows), limit=limit,
                    next_cursor=last and keyset_cursor(last.observed_at, last.provider, last.sku, last.region_key))
