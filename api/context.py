"""Market context: numeric series beside GPU prices (context/; methodology/market-context.md).

    GET /v1/context/series            the catalogue: every series, its source, unit, whether it is enabled, latest
    GET /v1/context/series?id=a,b     the points of up to 20 series (since=YYYY-MM-DD)
    GET /v1/context/power             US states with current listings, joined to the state's latest electricity price

Public reads (api/pages.py PUBLIC_V1 "context"). Context, not cause: nothing here says one series moved another.
"""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query

from accounts.auth import Principal, require_scope
from api.common import OBSERVED, envelope
from cache import ttl_cache
from context import edgar, eia
from context import series as cs

router = APIRouter()
READ = require_scope("data:read")
METHOD = "market-context"
MAX_IDS = 20
ATTRIBUTION = {"eia": eia.ATTRIBUTION, "sec_edgar": edgar.ATTRIBUTION}

cs.register()  # the context_series job (off until EIA_API_KEY or SEC_USER_AGENT is set)


@ttl_cache(300)
def _catalog():
    return cs.catalog()


def _meta(**kw) -> dict:
    return {"rule": cs.NOT_CAUSAL, "attribution": ATTRIBUTION, "enabled": cs.enabled(), **kw}


@router.get("/v1/context/series", tags=["context"], summary="Market-context series: the catalogue, or the points of some")
def context_series(id: str | None = Query(None, description="comma-separated series ids"), source: str | None = None,
                   entity: str | None = None, geo: str | None = None, since: date | None = None,
                   who: Principal = Depends(READ)):
    cat = _catalog()
    if not id:
        rows = [c for c in cat if (not source or c["source"] == source) and (not entity or c["entity"] == entity)
                and (not geo or c["geo"] == geo)]
        return envelope(rows, kind=OBSERVED, methodology=METHOD, count=len(rows), **_meta())
    ids = [i.strip() for i in id.split(",") if i.strip()]
    if len(ids) > MAX_IDS:
        raise HTTPException(422, {"code": "too_many_series", "message": f"at most {MAX_IDS} series per request"})
    known = {c["series_id"]: c for c in cat}
    unknown = [i for i in ids if i not in known]
    if unknown:
        raise HTTPException(404, {"code": "unknown_series", "message": f"unknown series {unknown}; see /v1/context/series"})
    rows = cs.points(ids, since)
    return envelope(rows, kind=OBSERVED, methodology=METHOD, count=len(rows),
                    series=[known[i] for i in ids], **_meta())


@router.get("/v1/context/power", tags=["context"], summary="US listings by state, beside the state's electricity price")
def context_power(who: Principal = Depends(READ)):
    rows = cs.power_by_region()
    return envelope(rows, kind=OBSERVED, methodology=METHOD, count=len(rows), **_meta(
        note="Listings are mapped to a state only where the region names one (context/geo.py). "
             "Retail prices are averages for the state and sector, not any provider's tariff."))
