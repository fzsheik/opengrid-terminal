"""CAISO OASIS: day-ahead (hourly) and real-time (5-minute) LMP at the three trading hubs and the default load
aggregation points. No key, no signup.

    GET https://oasis.caiso.com/oasisapi/SingleZip?queryname=PRC_LMP&version=12&market_run_id=DAM
        &node=TH_NP15_GEN-APND,...&startdatetime=YYYYMMDDTHH:MM-0000&enddatetime=...&resultformat=6
    GET ...queryname=PRC_INTVL_LMP&version=3&market_run_id=RTM...

Answers are a zip holding one CSV (resultformat=6), one row per (interval, node, LMP component): LMP, MCE (energy),
MCC (congestion), MCL (loss), MGHG; only LMP is kept. "No data" is a zip holding an XML error (ERR_CODE 1000).
Times are GMT; the operating day is Pacific prevailing time (OPR_DT), so a market day is local midnight to local
midnight and the DST days have 23 / 25 hours (276 / 300 five-minute intervals).

Depth: OASIS serves a rolling ~39 months (measured 2026-11-05: 2023-08 answered, 2023-07 and earlier "no data"),
so the backfill reaches about 2023-08, not 2021. Measured sizes, seven nodes: DA one day 8 KB zipped (146 KB CSV),
31 days 246 KB; RT one day 113 KB zipped (1.7 MB CSV). The forward job keeps DA bytes (small) and RT url + sha256.
OASIS asks for no more than one request every 5 seconds; it answers faster bursts with ERR_CODE 1015.
"""

from __future__ import annotations

import asyncio
import csv
import io
import re
import zipfile
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from power.registry import INTERNAL, Dataset, Fetched, Series, Terms
from power.storage import day_start

SOURCE = "caiso"
DEFAULT_ON = True
REQUIRES: tuple[str, ...] = ()
URL = "https://oasis.caiso.com/oasisapi/SingleZip"
TZ = "America/Los_Angeles"
PT = ZoneInfo(TZ)
RETENTION_MONTHS = 39
DA_PUBLISHED_HOUR = 14     # DAM results post around 13:00 PT; tomorrow is "complete" after 14:00
RATE_LIMITED_WAIT = 30.0

TERMS = Terms(
    operator="California ISO",
    visibility=INTERNAL,
    reason=("Site terms: 'No material or information from this Website ... may be copied, reproduced, republished, "
            "uploaded, posted, transmitted or distributed except (i) as authorized in these Terms of Use ...' ; the "
            "same page allows use of public-records material with credit, but its API terms say 'CAISO owns all "
            "right, title and interest in and to the CAISO API and CAISO Data' and bar use 'for any other "
            "benchmarking or competitive purposes'. Redistribution of OASIS prices in a commercial product is not "
            "clearly permitted, so internal."),
    terms_url="https://www.caiso.com/privacy-terms-of-use",
    checked="2026-11-05",
    signup=None,
    attribution="Source: California ISO OASIS",
)

NODES = {   # node -> (series location id, type, name)
    "TH_NP15_GEN-APND": ("th_np15", "hub", "NP15 trading hub (Northern California)"),
    "TH_SP15_GEN-APND": ("th_sp15", "hub", "SP15 trading hub (Southern California)"),
    "TH_ZP26_GEN-APND": ("th_zp26", "hub", "ZP26 trading hub (Central California)"),
    "DLAP_PGAE-APND": ("dlap_pgae", "load_area", "PG&E default load aggregation point"),
    "DLAP_SCE-APND": ("dlap_sce", "load_area", "SCE default load aggregation point"),
    "DLAP_SDGE-APND": ("dlap_sdge", "load_area", "SDG&E default load aggregation point"),
    "DLAP_VEA-APND": ("dlap_vea", "load_area", "VEA default load aggregation point"),
}
MARKETS = {   # market -> (queryname, version, market_run_id, value column, interval minutes)
    "da": ("PRC_LMP", "12", "DAM", "MW", 60),
    "rt": ("PRC_INTVL_LMP", "3", "RTM", "VALUE", 5),
}


def series_key(market: str, node: str) -> str:
    return f"caiso.{market}.lmp.{NODES[node][0]}"


def _series(market: str) -> tuple[Series, ...]:
    label = {"da": "day-ahead hourly", "rt": "real-time 5-minute"}[market]
    return tuple(Series(key=series_key(market, n), name=f"CAISO {name}, {label} LMP", kind="lmp", market=market,
                        location=n, location_type=typ, unit="usd_per_mwh", iso="CAISO")
                 for n, (_, typ, name) in NODES.items())


def earliest(now: datetime) -> date:
    t = now.astimezone(PT)
    y, m = t.year, t.month - RETENTION_MONTHS
    while m <= 0:
        y, m = y - 1, m + 12
    return date(y, m, 1)


def latest_da(now: datetime) -> date:
    t = now.astimezone(PT)
    return t.date() + timedelta(days=1) if t.hour >= DA_PUBLISHED_HOUR else t.date()


def latest_rt(now: datetime) -> date:
    return now.astimezone(PT).date() - timedelta(days=1)


def _gmt(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y%m%dT%H:%M-0000")


def params(market: str, start: date, end: date) -> dict:
    q, v, run, _, _ = MARKETS[market]
    return {"queryname": q, "startdatetime": _gmt(day_start(start, TZ)),
            "enddatetime": _gmt(day_start(end + timedelta(days=1), TZ)), "version": v, "market_run_id": run,
            "node": ",".join(NODES), "resultformat": "6"}


_ERR = re.compile(rb"<m:ERR_CODE>(\d+)</m:ERR_CODE>")


def _member(body: bytes) -> tuple[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(body)) as z:
        names = z.namelist()
        if len(names) != 1:
            raise ValueError(f"caiso: expected one file in the zip, got {names}")
        return names[0], z.read(names[0])


def error_code(body: bytes) -> str | None:
    """OASIS errors come back as HTTP 200 with an XML file in the zip."""
    try:
        name, data = _member(body)
    except (zipfile.BadZipFile, ValueError):
        return None
    if not name.lower().endswith(".xml"):
        return None
    m = _ERR.search(data)
    return m.group(1).decode() if m else "xml"


def _make_fetch(market: str):
    async def fetch(get, start: date, end: date) -> list[Fetched]:
        for attempt in range(3):
            f = await get.get(URL, params=params(market, start, end))
            if f.ok and error_code(f.body) == "1015" and attempt < 2:   # too many requests
                await asyncio.sleep(RATE_LIMITED_WAIT * (attempt + 1))
                continue
            return [f]
        return [f]
    return fetch


def _make_parse(market: str):
    col = MARKETS[market][3]

    def parse(f: Fetched) -> dict[str, dict[datetime, float | None]]:
        name, data = _member(f.body)
        if name.lower().endswith(".xml"):
            code = error_code(f.body)
            if code == "1000":
                f.empty = True
                return {}
            raise ValueError(f"caiso: OASIS error {code}: {data[:300]!r}")
        out: dict[str, dict[datetime, float | None]] = {}
        reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig")))
        need = {"INTERVALSTARTTIME_GMT", "NODE_ID", "LMP_TYPE", col}
        if not need <= set(reader.fieldnames or ()):
            raise ValueError(f"caiso: columns {reader.fieldnames} lack {need}")
        for r in reader:
            if r["LMP_TYPE"] != "LMP" or r["NODE_ID"] not in NODES:
                continue
            t = datetime.fromisoformat(r["INTERVALSTARTTIME_GMT"])
            v = r[col].strip()
            out.setdefault(series_key(market, r["NODE_ID"]), {})[t] = float(v) if v else None
        if not out:
            f.empty = True
        return out
    return parse


DATASETS = (
    Dataset(key="caiso.da_lmp", source=SOURCE, series=_series("da"), interval_minutes=60, tz=TZ, chunk_days=31,
            earliest=earliest, latest=latest_da, fetch=_make_fetch("da"), parse=_make_parse("da"),
            pace_seconds=5.0, overlap_days=2, keep_raw=True,
            description="PRC_LMP DAM, hourly LMP; 7 nodes; ~8 KB zipped per day (bytes kept forward)"),
    Dataset(key="caiso.rt_lmp", source=SOURCE, series=_series("rt"), interval_minutes=5, tz=TZ, chunk_days=3,
            earliest=earliest, latest=latest_rt, fetch=_make_fetch("rt"), parse=_make_parse("rt"),
            pace_seconds=5.0, overlap_days=2, keep_raw=False,
            description="PRC_INTVL_LMP RTM, 5-minute LMP; 7 nodes; ~113 KB zipped per day (url + sha256 only)"),
)
