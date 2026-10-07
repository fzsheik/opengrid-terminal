"""Market indices, history statistics, percentiles (analytics/indices.py, analytics/stats.py).

    GET /v1/indices                                     every index with data: level + changes
    GET /v1/indices/{index_id}                          definition, level, constituents, stats
    GET /v1/indices/{index_id}/history                  stored levels over a window
    GET /v1/history/{gpu}                               lowest / median / highest / providers over time + index
    GET /v1/markets/{gpu}/context                       percentiles vs 30d / 90d (stats.gpu_context)
    GET /v1/providers/{provider}/gpus/{gpu}/context     one provider's price vs its own history
    GET /v1/providers/{provider}/listings/context       one listing's price vs its own history

All are observed market prices (list prices as normalized), never quotes or execution prices.
"""

from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text

import analytics.rollups  # noqa: F401  (registers the market_hourly job)
import normalize
import provider_meta
from accounts.auth import Principal, require_scope
from analytics import indices, stats
from api.common import OBSERVED, OBSERVED_MARKET_PRICE, envelope, resolve_gpu, resolve_gpu_or_family
from cache import ttl_cache

router = APIRouter()
READ = require_scope("data:read")

Window = Literal["24h", "7d", "30d", "90d", "ytd", "all"]
Resolution = Literal["1h", "1d"]
Segment = Literal["on_demand", "spot"]
_SPANS = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30), "90d": timedelta(days=90)}


def _bounds(window: str) -> tuple[datetime | None, datetime | None]:
    end = indices.latest_hour()
    if end is None:
        return None, None
    if window == "all":
        return None, end
    if window == "ytd":
        return datetime(end.year, 1, 1, tzinfo=timezone.utc), end
    return end - _SPANS[window], end


def _resolution(window: str, resolution: str | None) -> str:
    return resolution or ("1h" if window in ("24h", "7d", "30d") else "1d")


def _index(index_id: str):
    d = indices.definition(index_id)
    if d is None:
        raise HTTPException(404, f"unknown index {index_id!r}; see /v1/indices")
    return d


def _meta():
    return {"kind": OBSERVED, "methodology": indices.METHODOLOGY, "price_concept": OBSERVED_MARKET_PRICE,
            "methodology_version": indices.METHODOLOGY_VERSION}


@router.get("/v1/indices", summary="OpenGrid indices: current level and changes")
def list_indices(kind: Literal["gpu", "spot", "regional", "provider_class", "composite"] | None = None,
                 _: Principal = Depends(READ)):
    items = indices.index_list(kind)
    return envelope(items, **_meta(), count=len(items),
                    note="Only indices with data in the last 90 days are listed; "
                         "an index with too few constituents is listed unpublished with its reason.")


@router.get("/v1/indices/{index_id}", summary="One index: definition, level, constituents, statistics")
def index_detail(index_id: str, _: Principal = Depends(READ)):
    d = _index(index_id)
    lv = indices.index_level(index_id)
    return envelope({"definition": d.as_dict(), **lv, "constituents_now": indices.constituents_now(index_id)},
                    **_meta())


@ttl_cache(300, maxsize=512)
def _history(index_id: str, window: str, resolution: str):
    t0, t1 = _bounds(window)
    if t1 is None:
        return []
    return indices.index_history(index_id, t0, t1, resolution)


@router.get("/v1/indices/{index_id}/history", summary="Stored index levels over a window")
def index_history(index_id: str, window: Window = "30d", resolution: Resolution | None = None,
                  _: Principal = Depends(READ)):
    d = _index(index_id)
    res = _resolution(window, resolution)
    return envelope({"index_id": index_id, "name": d.name, "unit": d.unit, "window": window, "resolution": res,
                     "points": _history(index_id, window, res)}, **_meta())


@ttl_cache(300, maxsize=512)
def _gpu_history(gpu: str, segment: str, window: str, resolution: str):
    t0, t1 = _bounds(window)
    out = stats.gpu_history(gpu, segment, t0, t1, resolution)
    iid = indices.gpu_index_id(gpu, segment)
    out["index"] = {"index_id": iid, "points": indices.index_history(iid, t0, t1, resolution) if t1 else []}
    return out


@router.get("/v1/history/{gpu}", summary="One GPU over time: lowest / median / highest / provider count + index")
def gpu_history(gpu: str, window: Window = "30d", resolution: Resolution | None = None,
                segment: Segment = "on_demand", _: Principal = Depends(READ)):
    kind, name = resolve_gpu_or_family(gpu)
    if kind == "family":
        from api.families import family_response
        return family_response(name, requested=gpu, endpoint="history")
    res = _resolution(window, resolution)
    data = _gpu_history(name, segment, window, res)
    return envelope({**data, "window": window}, **_meta(),
                    note="lowest/median/highest are the plain cross-section of provider prices (one vote per "
                         "provider) and can move when a provider starts being recorded; the index is chain-linked.")


@router.get("/v1/markets/{gpu}/context", summary="Where the GPU's price sits in its 30d / 90d history")
def market_context(gpu: str, segment: Segment = "on_demand", _: Principal = Depends(READ)):
    kind, name = resolve_gpu_or_family(gpu)
    if kind == "family":
        from api.families import family_response
        return family_response(name, requested=gpu, endpoint="context")
    return envelope(stats.gpu_context(name, segment), kind=OBSERVED, methodology=stats.METHODOLOGY,
                    price_concept=OBSERVED_MARKET_PRICE)


def _provider(value: str) -> str:
    from providers import PROVIDERS

    if value in PROVIDERS or value in provider_meta.PROVIDER_META:
        return value
    low = value.strip().lower()
    for name in list(PROVIDERS) + list(provider_meta.PROVIDER_META):
        if name.lower() == low or provider_meta.meta(name).display_name.lower() == low:
            return name
    with normalize.SessionLocal() as s:
        if s.execute(text("SELECT 1 FROM market_hourly WHERE provider = :p LIMIT 1"), {"p": value}).first():
            return value
    raise HTTPException(404, f"unknown provider {value!r}")


@router.get("/v1/providers/{provider}/gpus/{gpu}/context",
            summary="One provider's price for a GPU against its own recorded history")
def provider_gpu_context(provider: str, gpu: str, segment: Segment = "on_demand", _: Principal = Depends(READ)):
    p, name = _provider(provider), resolve_gpu(gpu)
    return envelope(stats.provider_gpu_context(p, name, segment), kind=OBSERVED, methodology=stats.METHODOLOGY,
                    price_concept=OBSERVED_MARKET_PRICE)


@router.get("/v1/providers/{provider}/listings/context",
            summary="One listing's price against its own recorded history (90 days)")
def listing_context(provider: str, listing_id: str = Query(...), _: Principal = Depends(READ)):
    p = _provider(provider)
    data = stats.listing_context(p, listing_id)
    if data is None:
        raise HTTPException(404, f"unknown listing {listing_id!r} for provider {p!r}")
    return envelope(data, kind=OBSERVED, methodology=stats.METHODOLOGY, price_concept=OBSERVED_MARKET_PRICE)
