"""Wholesale power markets (power/; methodology/power-markets.md).

    GET /v1/ops/power/sources        ADMIN ONLY. The registry: every source, its terms verdict, what it needs
    GET /v1/ops/power/status         ADMIN ONLY. Per dataset: series, series-days, depth, bytes; table sizes
    GET /v1/ops/power/series         ADMIN ONLY. The catalogue (every tier), or ?key=a,b: day rows (since, until,
                                     intervals=true for the per-interval values)

Internal-tier series (each ISO whose terms do not clearly permit redistribution) are only ever read here, by
admins, with Cache-Control: private, no-store. Context, not cause: nothing here says one series moved another.
"""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from accounts.auth import Principal, require_scope
from api.common import OBSERVED, envelope
from power import reads, registry, runner

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
