"""EIA API v2 natural gas: Henry Hub spot and NYMEX futures (daily), working gas in storage (weekly), the price
paid by the electric power sector by state (monthly). INTERNAL BY CHOICE: EIA data is public domain, but these
series are for OpenGrid's own models only (the founder's decision, 2026-11-05): no public page or endpoint.

    natural-gas/pri/fut   frequency=daily    RNGWHHD (Henry Hub spot, $/MMBtu, from 1997-01-07), RNGC1..RNGC4
                                             (NYMEX contracts 1-4, from 1993-12). EIA STOPPED the futures series
                                             on 2024-04-05 (measured 2026-11-05: the last period of all four), so
                                             they are history only; the spot continues
    natural-gas/stor/wkly frequency=weekly   working gas, BCF, week ending Friday, from 2010-01-01: Lower 48 (R48)
                                             and the five regions (R31..R35), plus South Central salt / non-salt
    natural-gas/pri/sum   frequency=monthly  process PEU, $/MCF: price sold to electric power consumers, per state
                                             and the U.S. (NUS from 2002-01; many states blank: EIA withholds them)

Periodic series: one value per row, dated by EIA's own period (the trading day, the week-ending Friday, the first
of the month). Regional hub dailies (ICE-licensed), pipeline flows, LNG and rig counts are deliberately not read.
Small (a few KB per forward fetch): forward keeps the received JSON, gzipped.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from power.registry import DAILY, INTERNAL, MONTHLY, WEEKLY, Dataset, Fetched, Series, Terms
from power.sources import eia
from context import geo

SOURCE = "eia_gas"
DEFAULT_ON = True
REQUIRES = ("eia_api_key",)
UTC = timezone.utc
FUTURES_LAST = date(2024, 4, 5)

TERMS = Terms(
    operator="U.S. Energy Information Administration",
    visibility=INTERNAL,
    reason=("INTERNAL BY CHOICE, not by terms: EIA data is U.S.-government public domain "
            "(https://www.eia.gov/about/copyrights_reuse.php), but natural gas is read for OpenGrid's internal "
            "models only (founder's decision 2026-11-05): admin-only reads, no public page or endpoint."),
    terms_url="https://www.eia.gov/about/copyrights_reuse.php",
    checked="2026-11-05",
    signup=eia.TERMS.signup,
    attribution="Source: U.S. Energy Information Administration, Natural Gas (API v2)",
)

DAILY_SERIES = {"RNGWHHD": ("spot", "spot_price", "henry_hub", "Henry Hub natural gas spot price")}
DAILY_SERIES.update({f"RNGC{i}": ("futures", f"futures_price_c{i}", "nymex", f"NYMEX natural gas futures, contract {i}")
                     for i in range(1, 5)})
STORAGE = {
    "NW2_EPG0_SWO_R48_BCF": ("r48", "Lower 48 states"), "NW2_EPG0_SWO_R31_BCF": ("r31", "East region"),
    "NW2_EPG0_SWO_R32_BCF": ("r32", "Midwest region"), "NW2_EPG0_SWO_R33_BCF": ("r33", "South Central region"),
    "NW2_EPG0_SWO_R34_BCF": ("r34", "Mountain region"), "NW2_EPG0_SWO_R35_BCF": ("r35", "Pacific region"),
    "NW2_EPG0_SSO_R33_BCF": ("r33_salt", "South Central salt"),
    "NW2_EPG0_SNO_R33_BCF": ("r33_nonsalt", "South Central non-salt"),
}
PRICE_AREAS = {"NUS": "US", **{f"S{st}": st for st in geo.STATES}}


def daily_key(sid: str) -> str:
    market, kind, loc, _ = DAILY_SERIES[sid]
    return f"eia_gas.{market}.{kind}.{loc}"


def storage_key(sid: str) -> str:
    return f"eia_gas.actual.storage_working_gas.{STORAGE[sid][0]}"


def price_key(area: str) -> str:
    return f"eia_gas.actual.electric_power_price.{PRICE_AREAS[area].lower()}"


def _gas(**kw) -> Series:
    return Series(commodity="natural_gas", iso=None, **kw)


SERIES_DAILY = tuple(_gas(key=daily_key(s), name=f"{n} ($/MMBtu, daily{'; EIA stopped it 2024-04-05' if m == 'futures' else ''})",
                          kind=k, market=m, location=s, location_type="hub", unit="usd_per_mmbtu")
                     for s, (m, k, _, n) in DAILY_SERIES.items())
SERIES_STORAGE = tuple(_gas(key=storage_key(s), name=f"Working gas in underground storage, {n} (BCF, weekly)",
                            kind="storage_working_gas", market="actual", location=s, location_type="region", unit="bcf")
                       for s, (_, n) in STORAGE.items())
SERIES_PRICE = tuple(_gas(key=price_key(a), name=f"Natural gas price, electric power consumers, "
                                                 f"{geo.STATES.get(st, 'United States')} ($/MCF, monthly)",
                          kind="electric_power_price", market="actual", location=a, location_type="state",
                          unit="usd_per_mcf", meta={"state": st})
                     for a, st in PRICE_AREAS.items())


def _yesterday(now: datetime) -> date:
    return now.astimezone(UTC).date() - timedelta(days=1)


def _today(now: datetime) -> date:
    return now.astimezone(UTC).date()


async def fetch_daily(get, start: date, end: date) -> list[Fetched]:
    p = [("frequency", "daily"), ("data[0]", "value"), *[("facets[series][]", s) for s in DAILY_SERIES],
         ("start", start.isoformat()), ("end", end.isoformat())]
    return await eia.paged(get, "natural-gas/pri/fut/data/", p, ("period", "series"))


async def fetch_storage(get, start: date, end: date) -> list[Fetched]:
    p = [("frequency", "weekly"), ("data[0]", "value"), *[("facets[series][]", s) for s in STORAGE],
         ("start", start.isoformat()), ("end", end.isoformat())]
    return await eia.paged(get, "natural-gas/stor/wkly/data/", p, ("period", "series"))


async def fetch_price(get, start: date, end: date) -> list[Fetched]:
    p = [("frequency", "monthly"), ("data[0]", "value"), ("facets[process][]", "PEU"),
         ("start", start.strftime("%Y-%m")), ("end", end.strftime("%Y-%m"))]
    return await eia.paged(get, "natural-gas/pri/sum/data/", p, ("period", "duoarea"))


def _units(r: dict, want: str) -> None:
    u = r.get("units")
    if u and u != want:
        raise ValueError(f"eia_gas: unexpected unit {u!r} (want {want!r})")


def parse_daily(f: Fetched) -> dict[str, dict[date, float | None]]:
    out: dict[str, dict[date, float | None]] = {}
    data = eia.rows(f)
    for r in data:
        if r.get("series") in DAILY_SERIES:
            _units(r, "$/MMBTU")
            out.setdefault(daily_key(r["series"]), {})[date.fromisoformat(r["period"])] = eia._num(r.get("value"))
    f.empty = not data
    return out


def parse_storage(f: Fetched) -> dict[str, dict[date, float | None]]:
    out: dict[str, dict[date, float | None]] = {}
    data = eia.rows(f)
    for r in data:
        if r.get("series") in STORAGE:
            _units(r, "BCF")
            out.setdefault(storage_key(r["series"]), {})[date.fromisoformat(r["period"])] = eia._num(r.get("value"))
    f.empty = not data
    return out


def parse_price(f: Fetched) -> dict[str, dict[date, float | None]]:
    out: dict[str, dict[date, float | None]] = {}
    data = eia.rows(f)
    for r in data:
        if r.get("process") == "PEU" and r.get("duoarea") in PRICE_AREAS:
            _units(r, "$/MCF")
            y, m = (int(x) for x in r["period"].split("-"))
            v = eia._num(r.get("value"))
            if v is not None:   # EIA withholds many states ("W" / blank): no row, not a zero
                out.setdefault(price_key(r["duoarea"]), {})[date(y, m, 1)] = v
    f.empty = not data
    return out


DATASETS = (
    Dataset(key="eia_gas.daily", source=SOURCE, series=SERIES_DAILY, interval_minutes=DAILY, tz="America/New_York",
            chunk_days=3650, earliest=lambda now: date(1993, 12, 1), latest=_yesterday, fetch=fetch_daily,
            parse=parse_daily, pace_seconds=eia.PACE, overlap_days=10, keep_raw=True,
            description="Henry Hub spot (RNGWHHD) + NYMEX contracts 1-4 (stopped 2024-04-05), $/MMBtu, daily"),
    Dataset(key="eia_gas.storage_weekly", source=SOURCE, series=SERIES_STORAGE, interval_minutes=WEEKLY,
            tz="America/New_York", chunk_days=3650, earliest=lambda now: date(2010, 1, 1), latest=_today,
            fetch=fetch_storage, parse=parse_storage, pace_seconds=eia.PACE, overlap_days=21, keep_raw=True,
            description="Working gas in underground storage, BCF, weekly (week ending): Lower 48 + regions"),
    Dataset(key="eia_gas.electric_price_monthly", source=SOURCE, series=SERIES_PRICE, interval_minutes=MONTHLY,
            tz="America/New_York", chunk_days=7300, earliest=lambda now: date(2002, 1, 1), latest=_today,
            fetch=fetch_price, parse=parse_price, pace_seconds=eia.PACE, overlap_days=150, keep_raw=True,
            description="Natural gas price sold to electric power consumers, $/MCF, monthly, by state + U.S."),
)
