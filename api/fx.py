"""Currency conversion: the ECB euro reference rate OpenGrid converts with, and what it converted.

    GET /v1/fx            the EUR->USD rate in force now, its date, and whether conversion runs (ok) or is
                          stopped (stale / missing: fail closed)
    GET /v1/fx/rates      stored EUR->USD reference rates, newest first (days)
    GET /v1/fx/cross      every ECB currency's X->USD cross rate in force now, its two legs and its state

Every converted listing is still a USD listing (`currency` USD) for statistics; the provenance is on every
GET /v1/listings row (`original`). Methodology: /methodology/fx.
"""

from fastapi import APIRouter, Depends, Query

import fx
from accounts.auth import Principal, require_scope
from api.common import OBSERVED, envelope

router = APIRouter()
READ = require_scope("data:read")
METHOD = "fx"

fx.register()  # the fx_rates background job (off when the poller is off)


@router.get("/v1/fx", tags=["fx"], summary="The EUR->USD reference rate in force now, and the fail-closed state")
def current(who: Principal = Depends(READ)):
    return envelope(fx.status(), kind=OBSERVED, methodology=METHOD)


@router.get("/v1/fx/rates", tags=["fx"], summary="Stored ECB EUR->X reference rates (default USD), newest first")
def history(days: int = Query(30, ge=1, le=3650), quote: str = Query("USD", pattern="^[A-Z]{3}$"),
            who: Principal = Depends(READ)):
    rows = fx.rates(days, quote)
    return envelope(rows, kind=OBSERVED, methodology=METHOD, count=len(rows), window_days=days, quote=quote)


@router.get("/v1/fx/cross", tags=["fx"], summary="X->USD cross rates for every ECB currency, with both legs")
def cross(who: Principal = Depends(READ)):
    rows = fx.cross_rates()
    return envelope(rows, kind="derived", methodology=METHOD, count=len(rows),
                    method="X->USD = (EUR->USD) / (EUR->X), both legs from the same ECB date; 8 decimals",
                    min_rate=fx.MIN_CROSS_RATE, max_age_days=fx.MAX_AGE_DAYS)


