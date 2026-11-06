"""MISO market reports: day-ahead ex-post and real-time final hourly LMP at the eight trading hubs (Arkansas,
Illinois, Indiana, Louisiana, Michigan, Minnesota, Mississippi, Texas). Public files, no key, no signup.

    GET https://docs.misoenergy.org/marketreports/YYYYMMDD_da_expost_lmp.csv       one market day (2023-01 on)
    GET https://docs.misoenergy.org/marketreports/YYYYMM_da_expost_lmp_csv.zip     one month: a zip of the days
    GET https://docs.misoenergy.org/marketreports/YYYYMMDD_rt_lmp_final.csv        (same, real-time final)
    GET https://docs.misoenergy.org/marketreports/YYYYMM_rt_lmp_final_csv.zip

Each CSV is one market day for EVERY node (~2,600 nodes x LMP / MCC / MLC, ~1.25 MB): a title line, the date
(MM/DD/YYYY, not always zero-padded), a blank line, ",,,All Hours-Ending are Eastern Standard Time (EST)", then
"Node,Type,Value,HE 1..HE 24". The file is read line by line and only the hubs' LMP rows are kept, so memory is the
answer's bytes plus one line (a monthly zip member is streamed, never decompressed whole). MISO runs on EST all year:
no DST, every market day has 24 hours; HE h is the hour starting h-1 hours after midnight EST.

Fetching: daily CSVs from DAILY_FROM (measured 2026-11-05: 2023-01-01 answered, 2022-12 days 404) and monthly zips
before it (202212_..._csv.zip answered, 202301 404; zips go back to at least 2016-01, 2014 404). A backfill chunk is
7 days (seven daily files are ~9 MB in memory), so a monthly zip spans several chunks: the last one is cached for a
few minutes and reused instead of re-downloaded. The real-time FINAL file posts in batches on business days, 3 to 7
days after the market day (10-31 posted 11-05, 10-28 posted 11-04), so latest_rt is RT_FINAL_LAG_DAYS back; the
preliminary file (rt_lmp_prelim, next day) is not read: it would be revised by the final one outside the overlap.
Real-time 5-minute LMPs exist only as a 24 MB xlsx per day (5min_exante_lmp.xlsx): not read.

Depth: 2016-01 (monthly zips) to now; the same layout was verified for 2021-01, so the backfill starts there
(EARLIEST). Measured sizes: one day 1.24 MB (DA) / 1.29 MB (RT) CSV, 280 / 259 KB gzipped; one month 6.0 / 5.7 MB
zipped (2021-01). Bytes are never kept (url + sha256): a day is far over the ~32 KB a day kept for small sources.

Historical depth beyond the public files (and the 5-minute prices) is in MISO's Data Exchange pricing API
(data-exchange.misoenergy.org; a self-serve subscription key, Ocp-Apim-Subscription-Key): not implemented.
"""

from __future__ import annotations

import dataclasses
import io
import time as _time
import zipfile
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from power.registry import INTERNAL, Dataset, Fetched, Series, Terms
from power.storage import day_start

SOURCE = "miso"
# The site terms bar 'any automated means ... to access, monitor, or copy any part of this Website': off unless
# POWER_SOURCES names it (or `python -m power ... --source miso`), until that is cleared.
DEFAULT_ON = False
REQUIRES: tuple[str, ...] = ()
BASE = "https://docs.misoenergy.org/marketreports"
TZ = "Etc/GMT+5"            # EST all year (POSIX sign: UTC-5)
EST = ZoneInfo(TZ)
EARLIEST = date(2021, 1, 1)
DAILY_FROM = date(2023, 1, 1)
DA_PUBLISHED_HOUR = 15      # DA ex-post results post early afternoon EST; tomorrow is "complete" after 15:00
RT_FINAL_LAG_DAYS = 8
CACHE_SECONDS = 600.0

TERMS = Terms(
    operator="Midcontinent ISO",
    visibility=INTERNAL,
    reason=("Site terms: 'You are not permitted to modify, publish, transmit, participate in the transfer or sale of, "
            "reproduce, create derivative works of, distribute, publicly perform, publicly display or in any way "
            "exploit any of the materials or content on this Website or the App in whole or in part.' They also say "
            "'You agree not use any automated means, including, without limitation, agents, robots, scripts, or "
            "spiders, to access, monitor, or copy any part of this Website or the App.' Redistribution is not "
            "permitted, so internal (and the source is off by default)."),
    terms_url="https://www.misoenergy.org/meet-miso/legal-and-privacy/",
    checked="2026-11-05",
    signup=None,
    attribution="Source: MISO",
)

HUBS = {   # MISO node -> (series location id, name)
    "ARKANSAS.HUB": ("arkansas_hub", "Arkansas hub"),
    "ILLINOIS.HUB": ("illinois_hub", "Illinois hub"),
    "INDIANA.HUB": ("indiana_hub", "Indiana hub"),
    "LOUISIANA.HUB": ("louisiana_hub", "Louisiana hub"),
    "MICHIGAN.HUB": ("michigan_hub", "Michigan hub"),
    "MINN.HUB": ("minn_hub", "Minnesota hub"),
    "MS.HUB": ("ms_hub", "Mississippi hub"),
    "TEXAS.HUB": ("texas_hub", "Texas hub"),
}
MARKETS = {   # market -> (file stem, title line)
    "da": ("da_expost_lmp", "Day Ahead Market ExPost LMPs"),
    "rt": ("rt_lmp_final", "Realtime Market LMPs"),
}
_PREFIXES = tuple(f"{h}," for h in HUBS)


def series_key(market: str, node: str) -> str:
    return f"miso.{market}.lmp.{HUBS[node][0]}"


def _series(market: str) -> tuple[Series, ...]:
    label = {"da": "day-ahead ex-post hourly", "rt": "real-time final hourly"}[market]
    return tuple(Series(key=series_key(market, n), name=f"MISO {name}, {label} LMP", kind="lmp", market=market,
                        location=n, location_type="hub", unit="usd_per_mwh", iso="MISO")
                 for n, (_, name) in HUBS.items())


def earliest(now: datetime) -> date:
    return EARLIEST


def latest_da(now: datetime) -> date:
    t = now.astimezone(EST)
    return t.date() + timedelta(days=1) if t.hour >= DA_PUBLISHED_HOUR else t.date()


def latest_rt(now: datetime) -> date:
    return now.astimezone(EST).date() - timedelta(days=RT_FINAL_LAG_DAYS)


def daily_url(market: str, d: date) -> str:
    return f"{BASE}/{d:%Y%m%d}_{MARKETS[market][0]}.csv"


def monthly_url(market: str, d: date) -> str:
    return f"{BASE}/{d:%Y%m}_{MARKETS[market][0]}_csv.zip"


def months(start: date, end: date) -> list[tuple[date, date]]:
    out, a = [], start
    while a <= end:
        nxt = (a.replace(day=1) + timedelta(days=32)).replace(day=1)
        out.append((a, min(end, nxt - timedelta(days=1))))
        a = nxt
    return out


_cache: dict[str, tuple[float, Fetched]] = {}   # the last monthly zip (one entry, ~6-8 MB)


async def _monthly(get, url: str) -> Fetched:
    hit = _cache.get(url)
    if hit and _time.monotonic() - hit[0] < CACHE_SECONDS:
        return dataclasses.replace(hit[1], empty=False, start=None, end=None)
    _cache.clear()
    f = await get.get(url)
    if f.ok:
        _cache[url] = (_time.monotonic(), dataclasses.replace(f))
    return f


def _make_fetch(market: str):
    async def fetch(get, start: date, end: date) -> list[Fetched]:
        out: list[Fetched] = []
        for a, b in months(start, end):
            if a >= DAILY_FROM:
                d = a
                while d <= b:
                    f = await get.get(daily_url(market, d))
                    f.start = f.end = d
                    out.append(f)
                    d += timedelta(days=1)
            else:
                f = await _monthly(get, monthly_url(market, a))
                f.start, f.end = a, b
                out.append(f)
        return out
    return fetch


def _files(body: bytes):
    """Line iterators over the CSVs in an answer: a monthly zip (each member streamed) or one daily CSV."""
    if body[:4] == b"PK\x03\x04":
        with zipfile.ZipFile(io.BytesIO(body)) as z:
            names = sorted(n for n in z.namelist() if n.lower().endswith(".csv"))
            if not names:
                raise ValueError(f"miso: no CSV in the zip: {z.namelist()[:5]}")
            for n in names:
                with z.open(n) as raw:
                    yield io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
    else:
        yield io.StringIO(body.decode("utf-8-sig"), newline="")


def _parse_file(lines, market: str, out: dict) -> None:
    title = next(lines, "").strip().strip(",")
    if title != MARKETS[market][1]:
        raise ValueError(f"miso: expected {MARKETS[market][1]!r}, got {title[:120]!r}")
    raw = next(lines, "").strip().strip(",")
    try:
        day = datetime.strptime(raw, "%m/%d/%Y").date()
    except ValueError:
        raise ValueError(f"miso: bad date line {raw[:60]!r}") from None
    for line in lines:
        if line.startswith("Node,"):
            hdr = line.rstrip("\r\n").split(",")
            break
        if "Hours-Ending" in line and "EST" not in line:
            raise ValueError(f"miso: hours are not EST: {line[:120]!r}")
    else:
        raise ValueError("miso: no Node,Type,Value header")
    if hdr[:3] != ["Node", "Type", "Value"] or hdr[3:] != [f"HE {h}" for h in range(1, 25)]:
        raise ValueError(f"miso: unexpected header {hdr[:6]}..{hdr[-2:]}")
    t0 = day_start(day, TZ)
    for line in lines:
        if not line.startswith(_PREFIXES):
            continue                                   # every other node: skipped without splitting
        cells = line.rstrip("\r\n").split(",")
        if len(cells) < 27 or cells[2] != "LMP":
            continue
        pts = out.setdefault(series_key(market, cells[0]), {})
        for h, v in enumerate(cells[3:27]):
            v = v.strip()
            pts[t0 + timedelta(hours=h)] = float(v) if v else None


def _make_parse(market: str):
    def parse(f: Fetched) -> dict[str, dict[datetime, float | None]]:
        out: dict[str, dict[datetime, float | None]] = {}
        for fh in _files(f.body):
            _parse_file(iter(fh), market, out)
        if not out:
            f.empty = True
        return out
    return parse


DATASETS = (
    Dataset(key="miso.da_lmp", source=SOURCE, series=_series("da"), interval_minutes=60, tz=TZ, chunk_days=7,
            earliest=earliest, latest=latest_da, fetch=_make_fetch("da"), parse=_make_parse("da"),
            pace_seconds=2.0, overlap_days=1, keep_raw=False,
            description="da_expost_lmp, hourly LMP; 8 hubs from an all-node file of 1.24 MB per day (url + sha256)"),
    Dataset(key="miso.rt_lmp", source=SOURCE, series=_series("rt"), interval_minutes=60, tz=TZ, chunk_days=7,
            earliest=earliest, latest=latest_rt, fetch=_make_fetch("rt"), parse=_make_parse("rt"),
            pace_seconds=2.0, overlap_days=1, keep_raw=False,
            description="rt_lmp_final, hourly LMP; 8 hubs from an all-node file of 1.29 MB per day (url + sha256)"),
)
