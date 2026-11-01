"""How fresh OpenGrid's data is, per provider and stream (freshness.py; methodology/freshness.md).

    GET /v1/freshness   one row per (provider, stream price | capacity): tier, target cadence, SLA, last successful
                        price fetch, last capacity refresh, age now, p50 / p95 age over 24 h, detection lag (7 d)
                        where the provider publishes change times, throttled answers in 24 h. Filters provider, stream.

Public with public pages on, like /v1/capacity (api/pages.py PUBLIC_V1); keys need data:read.
"""

from fastapi import APIRouter, Depends, HTTPException, Query

import freshness
from accounts.auth import Principal, require_scope
from api.common import OBSERVED, envelope

router = APIRouter()
READ = require_scope("data:read")


@router.get("/v1/freshness", tags=["capacity"], summary="Data freshness per provider: cadence, age, p95, detection lag")
def current(provider: str | None = None, stream: str | None = Query(None, description="price or capacity"),
            who: Principal = Depends(READ)):
    if stream is not None and stream not in freshness.STREAMS:
        raise HTTPException(422, f"stream must be one of {list(freshness.STREAMS)}")
    rows = [r for r in freshness.report()
            if (provider is None or r["provider"] == provider) and (stream is None or r["stream"] == stream)]
    if provider is not None and not rows:
        raise HTTPException(404, f"no freshness data for provider {provider!r}")
    return envelope(rows, kind=OBSERVED, methodology="freshness", count=len(rows),
                    sla=f"p95 age over 24 h <= {freshness.SLA_FACTOR} x the stream's target interval")
