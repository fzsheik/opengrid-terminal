"""EIA API v2, EIA-930 hourly grid data per balancing authority (PUBLIC: U.S.-government public domain).

    GET https://api.eia.gov/v2/electricity/rto/region-data/data/?api_key=...&frequency=hourly&data[0]=value
        &start=YYYY-MM-DDTHH&end=...&sort[0][column]=period&...&offset=N&length=5000
    GET https://api.eia.gov/v2/electricity/rto/fuel-type-data/data/?...   (net generation by fuel)

region-data: per respondent (74 BAs and EIA regions, US48 included) four types: D demand, NG net generation,
TI total interchange, DF day-ahead demand forecast, in MWh per hour. fuel-type-data: net generation per respondent
and fuel (16 EIA fuel codes). Measured 2026-11-05 for one UTC day: region-data 6,674 rows (914 KB of JSON per
5,000), fuel-type-data 11,112 rows. The API returns at most 5,000 rows per request, so a chunk is paged with
offset (stable sort: period, respondent, type).

Time: "hourly" periods are UTC and, as everywhere in EIA-930, HOUR ENDING: period T01 is 00:00-01:00 UTC. A market
day here is the UTC day (tz "UTC"), slot i = the hour starting at i:00 UTC, so day D is periods D T01 .. D+1 T00.
Depth: 2019-01 (the API's startPeriod); the backfill default asks from 2021-01. EIA revises recent hours (late
reports, corrections): forward re-reads OVERLAP days and counts revisions.

Raw: url + sha256 only, forward too. A day is ~1.5 MB of JSON (~100 KB gzipped) across 4-5 pages, the data is
public domain and EIA keeps the full history online, so any day can be re-fetched; keeping bytes would cost
~100 MB a year for nothing the API cannot give again.

Needs EIA_API_KEY (free: https://www.eia.gov/opendata/register.php), already set in production for context/eia.py.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

from power.registry import PUBLIC, Dataset, Fetched, Series, Terms

SOURCE = "eia"
DEFAULT_ON = True
REQUIRES = ("eia_api_key",)
BASE = "https://api.eia.gov/v2/"
PAGE = 5000
EARLIEST = date(2019, 1, 1)
UTC = timezone.utc
PACE = 1.0   # EIA asks for reasonable use; one request a second, one at a time

TERMS = Terms(
    operator="U.S. Energy Information Administration",
    visibility=PUBLIC,
    reason=("U.S.-government work: 'U.S. government publications are in the public domain and are not subject to "
            "copyright protection. You may use and/or distribute any of our data, files, databases, reports, "
            "graphs, charts, and other information products' (EIA copyright and reuse). EIA asks for attribution."),
    terms_url="https://www.eia.gov/about/copyrights_reuse.php",
    checked="2026-11-05",
    signup="free EIA API key: https://www.eia.gov/opendata/register.php (EIA_API_KEY; already set in production)",
    attribution="Source: U.S. Energy Information Administration, Hourly Electric Grid Monitor (EIA-930), API v2",
)

RESPONDENTS = {
    'AECI': 'Associated Electric Cooperative, Inc.', 'AVA': 'Avista Corporation', 'AVRN': 'Avangrid Renewables, LLC',
    'AZPS': 'Arizona Public Service Company', 'BANC': 'Balancing Authority of Northern California',
    'BPAT': 'Bonneville Power Administration', 'CAL': 'California', 'CAR': 'Carolinas', 'CENT': 'Central',
    'CHPD': 'Public Utility District No. 1 of Chelan County', 'CISO': 'California Independent System Operator',
    'CPLE': 'Duke Energy Progress East', 'CPLW': 'Duke Energy Progress West', 'DEAA': 'Arlington Valley, LLC',
    'DOPD': 'PUD No. 1 of Douglas County', 'DUK': 'Duke Energy Carolinas', 'EPE': 'El Paso Electric Company',
    'ERCO': 'Electric Reliability Council of Texas, Inc.', 'FLA': 'Florida', 'FMPP': 'Florida Municipal Power Pool',
    'FPC': 'Duke Energy Florida, Inc.', 'FPL': 'Florida Power & Light Co.',
    'GCPD': 'Public Utility District No. 2 of Grant County, Washington', 'GRID': 'Gridforce Energy Management, LLC',
    'GVL': 'Gainesville Regional Utilities', 'GWA': 'NaturEner Power Watch, LLC', 'HST': 'City of Homestead',
    'IID': 'Imperial Irrigation District', 'IPCO': 'Idaho Power Company', 'ISNE': 'ISO New England', 'JEA': 'JEA',
    'LDWP': 'Los Angeles Department of Water and Power', 'LGEE': 'LG&E and KU Services Company',
    'MIDA': 'Mid-Atlantic', 'MIDW': 'Midwest', 'MISO': 'Midcontinent Independent System Operator, Inc.',
    'NE': 'New England', 'NEVP': 'Nevada Power Company', 'NW': 'Northwest', 'NWMT': 'NorthWestern Corporation',
    'NY': 'New York', 'NYIS': 'New York Independent System Operator', 'PACE': 'PacifiCorp East',
    'PACW': 'PacifiCorp West', 'PGE': 'Portland General Electric Company', 'PJM': 'PJM Interconnection, LLC',
    'PNM': 'Public Service Company of New Mexico', 'PSCO': 'Public Service Company of Colorado',
    'PSEI': 'Puget Sound Energy, Inc.', 'SC': 'South Carolina Public Service Authority',
    'SCEG': 'Dominion Energy South Carolina, Inc.', 'SCL': 'Seattle City Light', 'SE': 'Southeast',
    'SEC': 'Seminole Electric Cooperative', 'SEPA': 'Southeastern Power Administration',
    'SIKE': 'Sikeston Board of Municipal Utilities', 'SOCO': 'Southern Company Services, Inc. - Trans',
    'SPA': 'Southwestern Power Administration', 'SRP': 'Salt River Project', 'SW': 'Southwest',
    'SWPP': 'Southwest Power Pool', 'TAL': 'City of Tallahassee', 'TEC': 'Tampa Electric Company', 'TEN': 'Tennessee',
    'TEPC': 'Tucson Electric Power', 'TEX': 'Texas', 'TIDC': 'Turlock Irrigation District',
    'TPWR': 'City of Tacoma, Department of Public Utilities, Light Division', 'TVA': 'Tennessee Valley Authority',
    'US48': 'United States Lower 48', 'WACM': 'Western Area Power Administration - Rocky Mountain Region',
    'WALC': 'Western Area Power Administration - Desert Southwest Region',
    'WAUW': 'Western Area Power Administration - Upper Great Plains West',
    'YAD': 'Alcoa Power Generating, Inc. - Yadkin Division',
    # in the 2019-2022 history, since retired or merged
    'EEI': 'Electric Energy, Inc.', 'GRIF': 'Griffith Energy, LLC', 'GRMA': 'Gila River Power, LLC',
    'HGMA': 'New Harquahala Generating Company, LLC', 'WWA': 'NaturEner Wind Watch, LLC',
    'OVEC': 'Ohio Valley Electric Corporation', 'GLHB': 'GridLiance', 'SPC': 'Saskatchewan Power Corporation',
    'AEC': 'PowerSouth Energy Cooperative', 'NSB': 'Utilities Commission of New Smyrna Beach',
}
# EIA's regions (sums of BAs), not balancing authorities
REGIONS = frozenset({"CAL", "CAR", "CENT", "FLA", "MIDA", "MIDW", "NE", "NW", "NY", "SE", "SW", "TEN", "TEX", "US48"})
ISO_OF = {"CISO": "CAISO", "ERCO": "ERCOT", "NYIS": "NYISO", "ISNE": "ISO-NE", "PJM": "PJM", "MISO": "MISO",
          "SWPP": "SPP"}
TYPES = {"D": ("demand", "actual", "Demand"), "NG": ("net_generation", "actual", "Net generation"),
         "TI": ("interchange", "actual", "Total interchange (exports positive)"),
         "DF": ("demand_forecast", "forecast", "Day-ahead demand forecast")}
FUELS = {'COL': 'Coal', 'NG': 'Natural gas', 'WND': 'Wind', 'OTH': 'Other', 'SUN': 'Solar', 'WAT': 'Hydro',
         'BAT': 'Battery storage', 'NUC': 'Nuclear', 'OIL': 'Petroleum', 'UES': 'Unknown energy storage',
         'SNB': 'Solar with integrated battery storage', 'GEO': 'Geothermal', 'PS': 'Pumped storage',
         'OES': 'Other energy storage', 'UNK': 'Unknown', 'WNB': 'Wind with integrated battery storage'}


def region_key(resp: str, typ: str) -> str:
    kind, market, _ = TYPES[typ]
    return f"eia.{market}.{kind}.{resp.lower()}"


def fuel_key(resp: str, fuel: str) -> str:
    return f"eia.actual.net_generation_{fuel.lower()}.{resp.lower()}"


def _loc_type(resp: str) -> str:
    return "region" if resp in REGIONS else "ba"


def _region_series() -> tuple[Series, ...]:
    return tuple(Series(key=region_key(r, t), name=f"{name}: {TYPES[t][2]} (hourly, MWh)", kind=TYPES[t][0],
                        market=TYPES[t][1], location=r, location_type=_loc_type(r), unit="mwh",
                        iso=ISO_OF.get(r, r), meta={"eia_type": t})
                 for r, name in RESPONDENTS.items() for t in TYPES)


def _fuel_series() -> tuple[Series, ...]:
    return tuple(Series(key=fuel_key(r, f), name=f"{name}: net generation, {fname} (hourly, MWh)",
                        kind=f"net_generation_{f.lower()}", market="actual", location=r, location_type=_loc_type(r),
                        unit="mwh", iso=ISO_OF.get(r, r), meta={"eia_fueltype": f})
                 for r, name in RESPONDENTS.items() for f, fname in FUELS.items())


def earliest(now: datetime) -> date:
    return EARLIEST


def latest(now: datetime) -> date:
    """BAs report within about a day; the UTC day before yesterday is the last one reliably whole."""
    return now.astimezone(UTC).date() - timedelta(days=2)


# --------------------------------------------------------------------------- shared EIA v2 paging (eia_gas uses it)

def api_key() -> str:
    from config import settings

    return settings.eia_api_key or ""


async def paged(get, route: str, params: list[tuple[str, str]], sort: tuple[str, ...]) -> list[Fetched]:
    """Every page of one EIA v2 data query, one Fetched per page (offset paging over a stable sort)."""
    out, offset = [], 0
    base = [("api_key", api_key()), *params]
    for i, col in enumerate(sort):
        base += [(f"sort[{i}][column]", col), (f"sort[{i}][direction]", "asc")]
    while True:
        f = await get.get(BASE + route, params=base + [("offset", str(offset)), ("length", str(PAGE))])
        out.append(f)
        if not f.ok:
            return out
        try:
            resp = json.loads(f.body)["response"]
            total, n = int(resp.get("total") or 0), len(resp.get("data") or [])
        except (ValueError, KeyError, TypeError):
            return out      # parse() reports the malformed page
        offset += n
        if n == 0 or offset >= total:
            return out


def rows(f: Fetched) -> list[dict]:
    try:
        body = json.loads(f.body)
    except ValueError as exc:
        raise ValueError(f"eia: not JSON: {exc}") from exc
    resp = body.get("response") if isinstance(body, dict) else None
    if not isinstance(resp, dict) or not isinstance(resp.get("data"), list):
        raise ValueError(f"eia: no response.data ({str(body)[:200]})")
    return resp["data"]


def _num(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def hour_start(period: str) -> datetime:
    """'2025-11-01T23' (UTC, hour ending) -> 22:00 UTC, the start of that hour."""
    return datetime.strptime(period, "%Y-%m-%dT%H").replace(tzinfo=UTC) - timedelta(hours=1)


def _hourly_params(start: date, end: date) -> list[tuple[str, str]]:
    a = datetime.combine(start, datetime.min.time(), tzinfo=UTC) + timedelta(hours=1)
    b = datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
    return [("frequency", "hourly"), ("data[0]", "value"), ("start", a.strftime("%Y-%m-%dT%H")),
            ("end", b.strftime("%Y-%m-%dT%H"))]


async def fetch_region(get, start: date, end: date) -> list[Fetched]:
    return await paged(get, "electricity/rto/region-data/data/", _hourly_params(start, end),
                       ("period", "respondent", "type"))


async def fetch_fuel(get, start: date, end: date) -> list[Fetched]:
    return await paged(get, "electricity/rto/fuel-type-data/data/", _hourly_params(start, end),
                       ("period", "respondent", "fueltype"))


def _check_units(r: dict) -> None:
    u = (r.get("value-units") or "").lower()
    if u and u != "megawatthours":
        raise ValueError(f"eia: unexpected unit {u!r}")


def parse_region(f: Fetched) -> dict[str, dict[datetime, float | None]]:
    out: dict[str, dict[datetime, float | None]] = {}
    data = rows(f)
    for r in data:
        resp, typ = r.get("respondent"), r.get("type")
        if resp not in RESPONDENTS or typ not in TYPES:
            continue
        _check_units(r)
        out.setdefault(region_key(resp, typ), {})[hour_start(r["period"])] = _num(r.get("value"))
    if not data:
        f.empty = True
    return out


def parse_fuel(f: Fetched) -> dict[str, dict[datetime, float | None]]:
    out: dict[str, dict[datetime, float | None]] = {}
    data = rows(f)
    for r in data:
        resp, fuel = r.get("respondent"), r.get("fueltype")
        if resp not in RESPONDENTS or fuel not in FUELS:
            continue
        _check_units(r)
        out.setdefault(fuel_key(resp, fuel), {})[hour_start(r["period"])] = _num(r.get("value"))
    if not data:
        f.empty = True
    return out


DATASETS = (
    Dataset(key="eia.region_hourly", source=SOURCE, series=_region_series(), interval_minutes=60, tz="UTC",
            chunk_days=3, earliest=earliest, latest=latest, fetch=fetch_region, parse=parse_region, pace_seconds=PACE,
            overlap_days=5, keep_raw=False,
            description="EIA-930 region-data: demand, net generation, total interchange, DA demand forecast per "
                        "BA / region, hourly UTC (hour ending); ~6.7k rows per day"),
    Dataset(key="eia.fuel_hourly", source=SOURCE, series=_fuel_series(), interval_minutes=60, tz="UTC",
            chunk_days=2, earliest=earliest, latest=latest, fetch=fetch_fuel, parse=parse_fuel, pace_seconds=PACE,
            overlap_days=5, keep_raw=False,
            description="EIA-930 fuel-type-data: net generation by fuel per BA / region, hourly UTC; ~11k rows/day"),
)
