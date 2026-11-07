"""Bulk export: one completed UTC day of a dataset as one gzipped JSON-lines file (docs/API_CONTRACT.md).

    GET /v1/exports                              the datasets and the first day each one has data
    GET /v1/exports/{dataset}?date=YYYY-MM-DD    that day as application/gzip (one JSON object per line)

Datasets: listings (every public listing as it stood at the end of the day, the /v1/listings?as_of= read),
listing_changes (that day's recorded changes), index_levels (every public index, hourly), fixings (official
fixings of that day). Rows follow the Data API body rules (public ids, Z timestamps; fixing values as exact
strings). Built from the database on request: a past day never changes, so a client caches it for a day.
"""

from __future__ import annotations

import gzip
import json
from datetime import date, datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi.responses import Response
from sqlalchemy import text

import normalize
from accounts.auth import Principal, require_scope
from analytics import indices
from analytics import settlement as S
from api import contract
from api.common import OBSERVED, envelope

router = APIRouter()
READ = require_scope("data:read")
DATASETS = {
    "listings": "every public listing as it stood at 23:59:59 UTC (the /v1/listings?as_of= read)",
    "listing_changes": "recorded listing changes (price, availability, capacity) during the day",
    "index_levels": "every public index, hourly levels of the day",
    "fixings": "official fixings taken for times in the day",
}
_FIRST = {"listings": "SELECT min(first_seen_at) FROM compute_listings WHERE visibility = 'public'",
          "listing_changes": "SELECT min(observed_at) FROM listing_observations",
          "index_levels": "SELECT min(hour) FROM index_levels",
          "fixings": "SELECT min(fixing_time) FROM index_fixings"}


@router.get("/v1/exports", tags=["exports"], summary="Bulk export datasets and the first day each has data")
def catalogue(limit: int = Query(100, ge=1, le=1000), cursor: str | None = None, who: Principal = Depends(READ)):
    with normalize.SessionLocal() as s:
        rows = []
        for name, what in DATASETS.items():
            first = s.execute(text(_FIRST[name])).scalar()
            rows.append({"dataset": name, "description": what, "first_date": first and first.date().isoformat(),
                         "format": "application/gzip: JSON lines", "url": f"/v1/exports/{name}?date=YYYY-MM-DD"})
    return envelope(page=(limit, cursor), data=rows, kind=OBSERVED, note="one completed UTC day per file")


def _rows(dataset: str, d0: datetime, d1: datetime) -> list[dict]:
    if dataset == "listings":
        from api.listings import _listings_as_of

        out, cur = [], None
        while True:  # every page of the point-in-time read
            body = _listings_as_of(d1 - timedelta(microseconds=1), None, None, None, None, None, 1000, cur)
            out += body["data"]
            cur = body["meta"]["next_cursor"]
            if not cur:
                return out
    with normalize.SessionLocal() as s:
        if dataset == "listing_changes":
            q = ("SELECT o.provider, o.listing_id, o.observed_at, c.canonical_gpu_name AS gpu, c.gpu_count, c.region, "
                 "c.market_type, o.price_per_gpu_hour, o.price_per_instance_hour, o.available, o.capacity, "
                 "o.capacity_unit FROM listing_observations o JOIN compute_listings c USING (provider, listing_id) "
                 "WHERE c.visibility = 'public' AND o.observed_at >= :a AND o.observed_at < :b "
                 "ORDER BY o.observed_at, o.provider, o.listing_id")
            return [dict(r._mapping) for r in s.execute(text(q), {"a": d0, "b": d1})]
        if dataset == "index_levels":
            q = ("SELECT index_id, hour, published, level, raw_level, constituents, method, reason FROM index_levels "
                 "WHERE index_id = ANY(:ids) AND hour >= :a AND hour < :b ORDER BY index_id, hour")
            return [dict(r._mapping) for r in s.execute(text(q), {"ids": list(indices.REGISTRY), "a": d0, "b": d1})]
    from api.settlement import _exact

    out = []
    for iid in S.SETTLEMENT_INDICES:
        rows, _ = S.list_fixings(iid, t0=d0, t1=d1 - timedelta(microseconds=1), limit=100000)
        out += _exact(rows)
    return out


@router.get("/v1/exports/{dataset}", tags=["exports"], summary="One completed UTC day of a dataset (gzipped JSON lines)")
def export(dataset: str = Path(..., pattern="^(listings|listing_changes|index_levels|fixings)$"),
           day: date = Query(..., alias="date", description="a completed UTC day, YYYY-MM-DD"),
           who: Principal = Depends(READ)):
    if day >= datetime.now(timezone.utc).date():
        raise HTTPException(422, {"code": "invalid_parameter", "param": "date",
                                  "message": "date must be a completed UTC day (yesterday or earlier)"})
    d0 = datetime.combine(day, time.min, tzinfo=timezone.utc)
    lines = (json.dumps(contract.public_shape(r), default=str, separators=(",", ":")) for r in _rows(dataset, d0, d0 + timedelta(days=1)))
    body = gzip.compress(("\n".join(lines) + "\n").encode())
    return Response(body, media_type="application/gzip", headers={
        "Content-Disposition": f'attachment; filename="opengrid-{dataset}-{day.isoformat()}.jsonl.gz"',
        "Cache-Control": "private, max-age=86400"})  # a past day never changes
