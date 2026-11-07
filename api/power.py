"""Wholesale power markets (power/; methodology/power-markets.md).

    GET /v1/ops/power/sources        ADMIN ONLY. The registry: every source, its terms verdict, what it needs
    GET /v1/ops/power/status         ADMIN ONLY. Per dataset: series, series-days, depth, bytes; table sizes
    GET /v1/ops/power/series         ADMIN ONLY. The catalogue (every tier), or ?key=a,b: day rows (since, until,
                                     intervals=true for the per-interval values). ?source=eia_gas: natural gas
    GET /v1/ops/power/map            ADMIN ONLY. Provider regions with current listings -> ISO / price series
    GET /v1/ops/power/cross/regional ADMIN ONLY. Power cost per GPU-hour and the spread to the GPU price, by region
    GET /v1/ops/power/cross/leadlag  ADMIN ONLY. Lagged correlations of GPU price / capacity (and Henry Hub) moves
                                     with power price moves. Correlation, never cause
    GET /v1/ops/power/shifts         ADMIN ONLY. Power-price spikes in each major shift's window, joined at read
                                     time (market_shift_items untouched). Related, not causal

Every power read is admin-only, with Cache-Control: private, no-store. There is no public route: power data is for
OpenGrid's own routing and research (the EIA-930 series are public domain and could be published later, through
the Data API's versioned contract). Context, not cause: nothing here says one series moved another.
"""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from accounts.auth import Principal, require_scope
from api.common import OBSERVED, envelope
from power import cross, reads, registry, runner

router = APIRouter()
ADMIN = require_scope("admin")
DOC = "methodology/power-markets.md"
MAX_KEYS = 20

runner.register()  # the power_forward job (off until POWER_ENABLED)


def _ops(resp: Response, rows, **kw):
    resp.headers["Cache-Control"] = "private, no-store"
    return envelope(rows, kind=OBSERVED, doc=DOC, rule=reads.NOT_CAUSAL, **kw)


@router.get("/v1/ops/power/sources", tags=["ops"], include_in_schema=False,
            summary="Ops: power sources, terms verdicts and configuration")
def ops_power_sources(response: Response, who: Principal = Depends(ADMIN)):
    rows = registry.as_list()
    return _ops(response, rows, count=len(rows), selected=registry.selected())


@router.get("/v1/ops/power/status", tags=["ops"], include_in_schema=False, summary="Ops: power storage status")
def ops_power_status(response: Response, who: Principal = Depends(ADMIN)):
    st = runner.status()
    return _ops(response, st["datasets"], count=len(st["datasets"]), table_bytes=st["table_bytes"],
                total_bytes=st["total_bytes"])


@router.get("/v1/ops/power/series", tags=["ops"], include_in_schema=False,
            summary="Ops (internal tiers included): power series catalogue or day rows")
def ops_power_series(response: Response, key: str | None = Query(None, description="comma-separated series keys"),
                     source: str | None = None, since: date | None = None, until: date | None = None,
                     intervals: bool = False, who: Principal = Depends(ADMIN)):
    if not key:
        rows = reads.catalog(public_only=False, source=source)
        return _ops(response, rows, count=len(rows))
    keys = [k.strip() for k in key.split(",") if k.strip()]
    if len(keys) > MAX_KEYS:
        raise HTTPException(422, {"code": "too_many_series", "message": f"at most {MAX_KEYS} series per request"})
    rows = reads.days(keys, since, until, public_only=False, intervals=intervals)
    return _ops(response, rows, count=len(rows), keys=keys)


@router.get("/v1/ops/power/map", tags=["ops"], include_in_schema=False,
            summary="Ops: provider regions mapped to power markets (documented places only)")
def ops_power_map(response: Response, who: Principal = Depends(ADMIN)):
    from power import geo
    from sqlalchemy import text

    import normalize

    with normalize.SessionLocal() as s:
        regions = s.execute(text("""SELECT provider, region, country, count(*) AS listings FROM compute_listings
                                    WHERE canonical_gpu_name IS NOT NULL AND region IS NOT NULL
                                    GROUP BY 1, 2, 3 ORDER BY 1, 2""")).all()
    rows = [{"provider": r.provider, "region": r.region, "country": r.country, "listings": r.listings,
             "power": geo.locate(r.provider, r.region, r.country)} for r in regions]
    return _ops(response, rows, count=len(rows), mapped=sum(1 for r in rows if r["power"]))


@router.get("/v1/ops/power/cross/regional", tags=["ops"], include_in_schema=False,
            summary="Ops (internal): power cost per GPU-hour and spread by region")
def ops_power_regional(response: Response, gpu: str | None = None, provider: str | None = None,
                       iso: str | None = None, since: date | None = None,
                       limit: int = Query(2000, ge=1, le=20000), who: Principal = Depends(ADMIN)):
    rows = cross.regional(gpu, provider, iso, since, limit)
    return _ops(response, rows, count=len(rows), visibility="internal", pue=cross.pue(), note=cross.RULE)


@router.get("/v1/ops/power/cross/leadlag", tags=["ops"], include_in_schema=False,
            summary="Ops (internal): lagged correlations of GPU moves with power / gas moves")
def ops_power_leadlag(response: Response, gpu: str | None = None, iso: str | None = None,
                      metric: str | None = None, who: Principal = Depends(ADMIN)):
    rows = cross.leadlag(gpu, iso, metric)
    return _ops(response, rows, count=len(rows), visibility="internal", method=cross.METHOD, note=cross.RULE)


@router.get("/v1/ops/power/shifts", tags=["ops"], include_in_schema=False,
            summary="Ops (internal): power-price spikes around major market shifts (related, not causal)")
def ops_power_shifts(response: Response, gpu: str | None = None, limit: int = Query(50, ge=1, le=200),
                     who: Principal = Depends(ADMIN)):
    rows = cross.shift_power(limit, gpu)
    return _ops(response, rows, count=len(rows), visibility="internal", note=cross.RULE)
