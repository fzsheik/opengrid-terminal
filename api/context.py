"""Market context: numeric series beside GPU prices (context/; methodology/market-context.md).

    GET /v1/macro/series                    the catalogue: every series, its source, unit, whether it is enabled, latest
    GET /v1/macro/series/{series}           one series
    GET /v1/macro/series/{series}/history   its points (since=YYYY-MM-DD)
    GET /v1/macro/power                     US states with current listings, joined to the state's latest electricity price

    GET /v1/ops/context/equities          ADMIN ONLY. INTERNAL equity research data (Alpaca): coverage per ticker,
                                          ?symbol= daily points (field, adjustment, since, until) or hourly bars
                                          (timeframe=1Hour); never public, never displayed, never republished
    GET /v1/ops/context/equities/moves    ADMIN ONLY. ?start=&end=: equity % moves beside GPU price moves

The /v1/macro reads are public (api/pages.py PUBLIC_V1 "macro") and NEVER carry visibility "internal" series,
whoever calls them. Context, not cause: nothing here says one series moved another.
"""

from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from accounts.auth import Principal, require_scope
from api.common import OBSERVED, envelope
from cache import ttl_cache
from context import edgar, eia, equities
from context import series as cs

router = APIRouter()
READ = require_scope("data:read")
ADMIN = require_scope("admin")
METHOD = "market-context"
ATTRIBUTION = {"eia": eia.ATTRIBUTION, "sec_edgar": edgar.ATTRIBUTION}

cs.register()  # the context_series job (off until EIA_API_KEY or SEC_USER_AGENT is set)


@ttl_cache(300)
def _catalog():
    return cs.catalog()


def _meta(**kw) -> dict:
    return {"rule": cs.NOT_CAUSAL, "attribution": ATTRIBUTION, "enabled": cs.enabled(), **kw}


def _series(series: str) -> dict:
    c = next((c for c in _catalog() if c["series_id"] == series), None)
    if c is None:
        raise HTTPException(404, {"code": "unknown_series", "param": "series",
                                  "message": f"unknown series {series!r}; see /v1/macro/series"})
    return c


@router.get("/v1/macro/series", tags=["macro"], summary="Market-context series: the catalogue")
def macro_series(source: str | None = None, entity: str | None = None, geo: str | None = None,
                 who: Principal = Depends(READ)):
    rows = [c for c in _catalog() if (not source or c["source"] == source) and (not entity or c["entity"] == entity)
            and (not geo or c["geo"] == geo)]
    return envelope(rows, kind=OBSERVED, methodology=METHOD, count=len(rows), **_meta())


@router.get("/v1/macro/series/{series}", tags=["macro"], summary="One market-context series: source, unit, latest")
def macro_series_one(series: str, who: Principal = Depends(READ)):
    return envelope(_series(series), kind=OBSERVED, methodology=METHOD, **_meta())


@router.get("/v1/macro/series/{series}/history", tags=["macro"], summary="One market-context series over time")
def macro_series_history(series: str, since: date | None = None, who: Principal = Depends(READ)):
    c = _series(series)
    rows = cs.points([series], since)
    return envelope(rows, kind=OBSERVED, methodology=METHOD, count=len(rows), series=c, **_meta())


@router.get("/v1/macro/power", tags=["macro"], summary="US listings by state, beside the state's electricity price")
def macro_power(who: Principal = Depends(READ)):
    rows = cs.power_by_region()
    return envelope(rows, kind=OBSERVED, methodology=METHOD, count=len(rows), **_meta(
        note="Listings are mapped to a state only where the region names one (context/geo.py). "
             "Retail prices are averages for the state and sector, not any provider's tariff."))


# --------------------------------------------------------------------------- INTERNAL (admin only)

DOC_INTERNAL = "methodology/internal/equities.md (in the repository; never served as a page)"


def _internal(resp: Response, rows, **kw):
    resp.headers["Cache-Control"] = "private, no-store"
    return envelope(rows, kind=OBSERVED, doc=DOC_INTERNAL, visibility=equities.VISIBILITY, rule=equities.RULE,
                    source="Alpaca Market Data API (SIP), internal research use only", **kw)


@router.get("/v1/ops/context/equities", tags=["ops"], include_in_schema=False,
            summary="Ops (internal): equity research series from Alpaca")
def ops_equities(response: Response, symbol: str | None = None,
                 field: str = Query("close", pattern="^(open|high|low|close|volume|vwap)$"),
                 adjustment: str = Query("split", pattern="^(split|raw|all)$"),
                 timeframe: str = Query("1Day", pattern="^(1Day|1Hour)$"),
                 since: date | None = None, until: date | None = None, who: Principal = Depends(ADMIN)):
    if not symbol:
        rows = equities.coverage()
        return _internal(response, rows, count=len(rows), enabled=equities.enabled())
    sym = symbol.strip().upper()
    if sym not in equities.tickers():
        raise HTTPException(404, {"code": "unknown_symbol", "message": f"{sym} is not tracked"})
    if timeframe == "1Hour":
        rows = equities.hourly(sym, since and datetime.combine(since, datetime.min.time()),
                               until and datetime.combine(until, datetime.max.time()))
    else:
        if field not in equities.DAILY_PLANS[adjustment]:
            raise HTTPException(422, {"code": "not_stored", "message": f"{field} is not stored for adjustment={adjustment}"})
        rows = equities.points(sym, field, adjustment, since, until)
    return _internal(response, rows, count=len(rows), symbol=sym, timeframe=timeframe, field=field,
                     adjustment=adjustment, unit=equities.unit(field))


@router.get("/v1/ops/context/equities/moves", tags=["ops"], include_in_schema=False,
            summary="Ops (internal): equity moves beside GPU price moves for a window")
def ops_equity_moves(response: Response, start: date, end: date, segment: str = "on_demand",
                     who: Principal = Depends(ADMIN)):
    if end < start:
        raise HTTPException(422, {"code": "bad_window", "message": "end before start"})
    return _internal(response, equities.moves(start, end, segment=segment))
