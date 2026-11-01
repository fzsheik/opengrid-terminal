"""Currency conversion: the ECB euro reference rate OpenGrid converts with, and what it converted.

    GET /v1/fx            the EUR->USD rate in force now, its date, and whether conversion runs (ok) or is
                          stopped (stale / missing: fail closed)
    GET /v1/fx/rates      stored EUR->USD reference rates, newest first (days)
    GET /v1/fx/listings   current listings priced in another currency and converted to USD, with provenance:
                          original currency and amount, the rate and the rate's date (provider)

Every converted listing is still a USD listing (`currency` USD) for statistics; the provenance fields are also on
GET /listings and /history rows. Methodology: /methodology/fx.
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


@router.get("/v1/fx/rates", tags=["fx"], summary="Stored ECB EUR->USD reference rates, newest first")
def history(days: int = Query(30, ge=1, le=3650), who: Principal = Depends(READ)):
    rows = fx.rates(days)
    return envelope(rows, kind=OBSERVED, methodology=METHOD, count=len(rows), window_days=days)


@router.get("/v1/fx/listings", tags=["fx"], summary="Listings converted to USD, with their FX provenance")
def converted(provider: str | None = None, who: Principal = Depends(READ)):
    rows = fx.converted_listings(provider)
    return envelope(rows, kind=OBSERVED, methodology=METHOD, count=len(rows), price_concept="list_price converted at the ECB reference rate")
