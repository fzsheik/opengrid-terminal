"""Currency conversion: the ECB euro reference rate OpenGrid converts with, and what it converted.

    GET /v1/fx/rates            in force now: eur_usd (the EUR->USD reference rate, its date, and whether conversion
                                runs (ok) or is stopped (stale / missing: fail closed)) and to_usd (every ECB
                                currency's X->USD cross rate, its two legs and its state)
    GET /v1/fx/rates/history    stored EUR->X reference rates, newest first (days, quote)

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


@router.get("/v1/fx/rates", tags=["fx"], summary="Reference rates in force now: EUR->USD and every X->USD cross")
def current(who: Principal = Depends(READ)):
    return envelope({"eur_usd": fx.status(), "to_usd": fx.cross_rates()}, kind=OBSERVED, methodology=METHOD,
                    cross_method="X->USD = (EUR->USD) / (EUR->X), both legs from the same ECB date; 8 decimals",
                    min_rate=fx.MIN_CROSS_RATE, max_age_days=fx.MAX_AGE_DAYS,
                    note="eur_usd.state says whether conversion runs (ok) or is stopped (stale / missing: fail closed)")


@router.get("/v1/fx/rates/history", tags=["fx"], summary="Stored ECB EUR->X reference rates (default USD), newest first")
def history(days: int = Query(30, ge=1, le=3650), quote: str = Query("USD", pattern="^[A-Z]{3}$"),
            who: Principal = Depends(READ)):
    rows = fx.rates(days, quote)
    return envelope(rows, kind=OBSERVED, methodology=METHOD, count=len(rows), window_days=days, quote=quote)
