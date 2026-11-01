"""EIA API v2: average retail electricity price by US state and sector, monthly (methodology/market-context.md).

    GET https://api.eia.gov/v2/electricity/retail-sales/data/?api_key=...&frequency=monthly&data[0]=price
        &facets[sectorid][]=IND&facets[sectorid][]=COM&start=YYYY-MM&length=5000

One request covers every state, both sectors, MONTHS months. Needs a free key (EIA_API_KEY); without one this
source is off. EIA data is public domain ("U.S. government publications are in the public domain"); EIA asks for an
acknowledgment with the publication date, carried as ATTRIBUTION.

Series: eia.retail_price.<sector>.<state> in cents per kWh, as published. Sectors: IND (industrial: the closest
published proxy for a data centre's tariff) and COM (commercial). "US" is EIA's national average. EIA's census
divisions (NEW, MAT, ...) are left out. EIA gives no publication time per value, so published_at is null and
first_seen_at (when OpenGrid first read it) is the time to use. No hourly price series: EIA publishes no free hourly
electricity prices (its hourly grid data, EIA-930, is demand and generation, not price).
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

from context import geo

SOURCE = "eia"
URL = "https://api.eia.gov/v2/electricity/retail-sales/data/"
SECTORS = {"IND": "industrial", "COM": "commercial"}
MONTHS = 36
UNIT = "cents_per_kwh"
ATTRIBUTION = "Source: U.S. Energy Information Administration, Electric Power Monthly / retail sales (API v2)"
_KEEP = ("period", "stateid", "stateDescription", "sectorid", "price", "price-units")


def params(api_key: str, today: date | None = None) -> list[tuple[str, str]]:
    today = today or datetime.now(timezone.utc).date()
    y, m = today.year, today.month - MONTHS
    while m <= 0:
        y, m = y - 1, m + 12
    return [("api_key", api_key), ("frequency", "monthly"), ("data[0]", "price"),
            *[("facets[sectorid][]", s) for s in SECTORS], ("start", f"{y:04d}-{m:02d}"),
            ("sort[0][column]", "period"), ("sort[0][direction]", "desc"), ("length", "5000")]


def reshape(body: dict) -> dict:
    """Keep the rows read (states and US, both sectors) and the response's own totals and warnings."""
    resp = (body or {}).get("response")
    if not isinstance(resp, dict) or not isinstance(resp.get("data"), list):
        raise ValueError("eia: no response.data - reshaped")
    rows = [{k: r.get(k) for k in _KEEP} for r in resp["data"]
            if r.get("sectorid") in SECTORS and (r.get("stateid") in geo.STATES or r.get("stateid") == "US")]
    if not rows:
        raise ValueError("eia: no state rows for the requested sectors")
    return {"total": resp.get("total"), "frequency": resp.get("frequency"), "warnings": body.get("warnings"),
            "data": rows}


def series_id(sector: str, state: str) -> str:
    return f"eia.retail_price.{sector.lower()}.{state}"


def catalog() -> list[dict]:
    out = []
    for st in ["US", *geo.STATES]:
        for sec, name in SECTORS.items():
            out.append({"series_id": series_id(sec, st), "source": SOURCE, "unit": UNIT, "frequency": "monthly",
                        "name": f"Average {name} electricity price, {geo.STATES.get(st, 'United States')}",
                        "entity": None, "geo": st, "market_area": geo.market_area(st), "attribution": ATTRIBUTION})
    return out


def points(payload: dict) -> list[dict]:
    """Stored payload -> series points. A month with no price (EIA leaves some blank) is skipped, not zero."""
    out = []
    for r in payload.get("data") or []:
        p, st, sec = r.get("period"), r.get("stateid"), r.get("sectorid")
        try:
            y, m = (int(x) for x in str(p).split("-"))
            value = Decimal(str(r.get("price")))
        except (ValueError, TypeError, InvalidOperation):
            continue
        if not value.is_finite() or value <= 0:
            continue
        units = (r.get("price-units") or "").lower()
        if units and "cents per kilowatt" not in units:
            raise ValueError(f"eia: unexpected price unit {units!r}")
        out.append({"series_id": series_id(sec, st), "period_start": date(y, m, 1),
                    "period_end": date(y, m, calendar.monthrange(y, m)[1]), "value": value, "unit": UNIT,
                    "source": SOURCE, "basis": "reported", "published_at": None,
                    "meta": {"state": st, "sector": sec}})
    return out
