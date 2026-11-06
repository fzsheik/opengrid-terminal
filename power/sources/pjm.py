"""PJM Data Miner 2: day-ahead and real-time hourly LMP at the PJM trading hubs and the transmission zones (DOM, the
Dominion zone, is Northern Virginia: the world's largest datacenter market). Needs a free Data Miner 2 API key
(pjm_api_key); off without it.

    GET https://api.pjm.com/api/v1/da_hrl_lmps?datetime_beginning_utc=M/D/YYYY HH:MMtoM/D/YYYY HH:MM&type=HUB
        &row_is_current=true&rowCount=50000&startRow=1          header Ocp-Apim-Subscription-Key: <key>
    GET https://api.pjm.com/api/v1/rt_hrl_lmps?...               the same with the _rt fields

UNVERIFIED LIVE: built from the Data Miner 2 API guide (v15, 2026-02-10) and the feeds' /metadata, and tested on
real answers of these feeds (tests/fixtures_power/pjm_*.json); never run with OpenGrid's own key (none yet).

Answers are {"items": [...], "totalRows": n, "links": [...], "searchSpecification": {...}}, one item per (hour,
pnode): datetime_beginning_utc (naive, UTC), datetime_beginning_ept, pnode_id, pnode_name, type, total_lmp_da (or
_rt) and its components; only total_lmp is kept. Errors are HTTP 400 {"errors": [{"field", "message"}]}. The time
filter is on UTC: Data Miner reads an EPT wall time in the repeated fall-back hour as standard time, so an EPT
filter would lose an hour; on UTC the DST days simply have 23 / 25 rows per pnode.

Data older than 731 days is "archived": it can be filtered only by dates, type, row_is_current and version_nbr (a
pnode_id filter is a 400), a request must stay within one UTC calendar year, and it must not span archived and
current data. So every request asks by type (HUB, ZONE: 12 + 23 pnodes, ~8,900 / ~17,100 rows a month) and keeps
the listed pnode_ids, and a chunk's UTC window is split at each 1 January 00:00 UTC and at the archive cutoff;
a market day cut by a split is carried by both answers (the runner merges them).

Depth: da_hrl_lmps from 2000-06-01, rt_hrl_lmps from 1998-04-01; the backfill starts at 2021-01. Rate limit:
"Non-members may not exceed 6 data connections per minute" (a request every 11 s here). Sizes (from the sample
rows, ~420 bytes per JSON row): ~10 KB per series-day; a month's two requests ~11 MB. url + sha256 only.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from power.registry import INTERNAL, Dataset, Fetched, Series, Terms
from power.storage import day_start, market_day

SOURCE = "pjm"
DEFAULT_ON = False
REQUIRES: tuple[str, ...] = ("pjm_api_key",)
BASE = "https://api.pjm.com/api/v1/"
TZ = "America/New_York"
ET = ZoneInfo(TZ)
UTC = timezone.utc
EARLIEST = date(2021, 1, 1)
DA_PUBLISHED_HOUR = 14     # "Daily between 12:00 p.m. and 01:30 p.m." EPT; tomorrow is "complete" after 14:00
ARCHIVE_DAYS = 731         # the feeds' archiveCutoffDays
ROW_COUNT = 50000          # the API's maximum rows per request

TERMS = Terms(
    operator="PJM Interconnection",
    visibility=INTERNAL,
    reason=("Data Miner 2 terms of use, section 1: 'you may use the Data and the Tools for your internal business "
            "use or commercial purposes, including publishing and making derivatives of the Data, Members may "
            "republish data, however non-members are specifically prohibited from republishing data.' The Data "
            "Miner page: 'redistribution of information and or data contained in or derived from Data Miner is "
            "strictly prohibited without an active PJM Membership.' OpenGrid is not a member: internal only."),
    terms_url="https://www.pjm.com/-/media/DotCom/etools/edatafeed/data-license-agreement-edata-feed-data-miner-2.pdf",
    checked="2026-11-05",
    signup=("Create a PJM Tools account at https://accountmanager.pjm.com/accountmanager/pages/public/new-user.jsf, "
            "then (non-member) email accountmanager@pjm.com: 'I confirm that the PJM Data will be used for internal "
            "business purposes only.' with your Account Manager user id and email; once provisioned, sign in at "
            "https://apiportal.pjm.com, open Data Miner > DataMiner Web API, Profile > Show Primary key, and set "
            "PJM_API_KEY."),
    attribution="Source: PJM Interconnection Data Miner 2",
)

HUBS = {   # pnode_id -> pnode_name
    51217: "EASTERN HUB", 51287: "WEST INT HUB", 51288: "WESTERN HUB", 4669664: "NEW JERSEY HUB",
    33092311: "CHICAGO GEN HUB", 33092313: "CHICAGO HUB", 33092315: "N ILLINOIS HUB", 34497125: "AEP GEN HUB",
    34497127: "AEP-DAYTON HUB", 34497151: "OHIO HUB", 35010337: "DOMINION HUB", 116013751: "ATSI GEN HUB",
}
ZONES = {
    1: "PJM-RTO", 3: "MID-ATL/APS", 51291: "AECO", 51292: "BGE", 51293: "DPL", 51295: "JCPL", 51296: "METED",
    51297: "PECO", 51298: "PEPCO", 51299: "PPL", 51300: "PENELEC", 51301: "PSEG", 7633629: "RECO", 8394954: "APS",
    8445784: "AEP", 33092371: "COMED", 34508503: "DAY", 34964545: "DOM", 37737283: "DUQ", 116013753: "ATSI",
    124076095: "DEOK", 970242670: "EKPC", 1709725933: "OVEC",
}
PNODES = {**{i: (n, "hub") for i, n in HUBS.items()}, **{i: (n, "zone") for i, n in ZONES.items()}}
FEEDS = {"da": "da_hrl_lmps", "rt": "rt_hrl_lmps"}


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def series_key(market: str, pnode_id: int) -> str:
    return f"pjm.{market}.lmp.{slug(PNODES[pnode_id][0])}"


def _series(market: str) -> tuple[Series, ...]:
    label = {"da": "day-ahead hourly", "rt": "real-time hourly"}[market]
    return tuple(Series(key=series_key(market, i), name=f"PJM {n}{' zone' if t == 'zone' else ''}, {label} LMP",
                        kind="lmp", market=market, location=n, location_type=t, unit="usd_per_mwh", iso="PJM",
                        meta={"pnode_id": i})
                 for i, (n, t) in PNODES.items())


def earliest(now: datetime) -> date:
    return EARLIEST


def latest_da(now: datetime) -> date:
    t = now.astimezone(ET)
    return t.date() + timedelta(days=1) if t.hour >= DA_PUBLISHED_HOUR else t.date()


def latest_rt(now: datetime) -> date:
    return now.astimezone(ET).date() - timedelta(days=1)   # posted daily on business days: overlap_days covers it


def _fmt(t: datetime) -> str:
    t = t.astimezone(UTC)
    return f"{t.month}/{t.day}/{t.year} {t:%H:%M}"


def windows(start: date, end: date, now: datetime) -> list[tuple[datetime, datetime]]:
    """[a, b) UTC windows covering market days start..end, split at 1 January UTC and at the archive cutoff."""
    a, b = day_start(start, TZ), day_start(end + timedelta(days=1), TZ)
    cut = datetime.combine(now.astimezone(UTC).date() - timedelta(days=ARCHIVE_DAYS), datetime.min.time(), UTC)
    marks = sorted({datetime(y, 1, 1, tzinfo=UTC) for y in range(a.year, b.year + 1)} | {cut})
    edges = [a] + [m for m in marks if a < m < b] + [b]
    return list(zip(edges, edges[1:]))


def params(market: str, a: datetime, b: datetime, typ: str, start_row: int = 1) -> dict:
    return {"datetime_beginning_utc": f"{_fmt(a)}to{_fmt(b - timedelta(hours=1))}", "type": typ,
            "row_is_current": "true", "rowCount": str(ROW_COUNT), "startRow": str(start_row)}


def _key() -> str:
    from config import settings

    return settings.pjm_api_key or ""


def _make_fetch(market: str):
    url = BASE + FEEDS[market]

    async def fetch(get, start: date, end: date, now: datetime | None = None) -> list[Fetched]:
        headers = {"Ocp-Apim-Subscription-Key": _key(), "Accept": "application/json"}
        out = []
        for a, b in windows(start, end, now or datetime.now(UTC)):
            first, last = market_day(a, TZ), market_day(b - timedelta(hours=1), TZ)
            for typ in ("HUB", "ZONE"):
                row = 1
                while True:
                    f = await get.get(url, params=params(market, a, b, typ, row), headers=headers)
                    f.start, f.end = first, last
                    out.append(f)
                    total = _total(f) if f.ok else None
                    if total is None or row + ROW_COUNT > total:
                        break
                    row += ROW_COUNT
        return out
    return fetch


def _load(body: bytes):
    try:
        return json.loads(body)
    except ValueError as exc:
        raise ValueError(f"pjm: not JSON: {body[:200]!r}") from exc


def _total(f: Fetched) -> int | None:
    try:
        d = json.loads(f.body)
        return int(d["totalRows"]) if isinstance(d, dict) and "totalRows" in d else None
    except (ValueError, TypeError):
        return None


def _make_parse(market: str):
    col = f"total_lmp_{market}"

    def parse(f: Fetched) -> dict[str, dict[datetime, float | None]]:
        d = _load(f.body)
        if isinstance(d, dict) and d.get("errors"):
            raise ValueError(f"pjm: {d['errors']}"[:500])
        items = d.get("items") if isinstance(d, dict) else d     # download=true answers a bare list
        if not isinstance(items, list):
            raise ValueError(f"pjm: no items in {str(d)[:200]}")
        out: dict[str, dict[datetime, float | None]] = {}
        for r in items:
            try:
                pid = int(r["pnode_id"])
                if pid not in PNODES or r.get("row_is_current") is False:
                    continue
                t = datetime.fromisoformat(r["datetime_beginning_utc"]).replace(tzinfo=UTC)
                v = r.get(col)
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"pjm: bad item {str(r)[:200]}: {exc}") from exc
            out.setdefault(series_key(market, pid), {})[t] = None if v is None else float(v)
        if not out:
            f.empty = True
        return out
    return parse


DATASETS = (
    Dataset(key="pjm.da_lmp", source=SOURCE, series=_series("da"), interval_minutes=60, tz=TZ, chunk_days=31,
            earliest=earliest, latest=latest_da, fetch=_make_fetch("da"), parse=_make_parse("da"),
            pace_seconds=11.0, overlap_days=2, keep_raw=False, align="month",
            description="da_hrl_lmps, hourly LMP; 12 hubs + 23 zones; by type per month, ~10 KB per series-day "
                        "(url + sha256 only); UNVERIFIED LIVE (no key yet)"),
    Dataset(key="pjm.rt_lmp", source=SOURCE, series=_series("rt"), interval_minutes=60, tz=TZ, chunk_days=31,
            earliest=earliest, latest=latest_rt, fetch=_make_fetch("rt"), parse=_make_parse("rt"),
            pace_seconds=11.0, overlap_days=3, keep_raw=False, align="month",
            description="rt_hrl_lmps, hourly verified LMP; 12 hubs + 23 zones; ~10 KB per series-day "
                        "(url + sha256 only); UNVERIFIED LIVE (no key yet)"),
)
