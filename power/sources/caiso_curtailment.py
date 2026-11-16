"""CAISO wind and solar curtailment (methodology/power-waste.md): energy the ISO dispatched down or cut, hourly, by
fuel and by reason, beside the wind and solar output it did take. No key, no signup; www.caiso.com documents.

    GET https://www.caiso.com/documents/<archive>.xlsx                       the "Production and Curtailments Data"
                                                                            workbooks, 2014-05-01 .. 2025-12-31
    GET https://www.caiso.com/documents/daily-renewable-report-<mon>-<dd>-<yyyy>.html   one operating day, from
                                                                            2025-06-01 (posted ~10:00 PT next day)

The workbooks (library page /library/production-curtailments-data; CAISO stopped them on 2025-06-01 and posted a
one-time bulk curtailment file for the whole of 2025) hold two sheets, five-minute, in MW (CAISO's read-me: "All
data is in megawatt (MW) units"; "only intervals in which a curtailment was observed are listed"; "The amounts of
curtailments are not counted in the production quantities"):

    Production    Date, Hour, Interval, Load, Solar, Wind, Net Load, Renewables, Nuclear, Large Hydro, Imports, ...
    Curtailments  Date, Hour, Interval, Wind Curtailment, Solar Curtailment[, Reason]   Reason: Local | System
                  (no Reason column in the 2014-05..2017-05 file: totals only)

Date is an Excel serial (its fraction is not the interval time), Hour is hour ENDING 1..24 in Pacific prevailing
time, Interval 1..12. The spring-forward day has no hour 3; the fall-back day lists hour 2 twice (24 rows), the
second pass being the repeated 01:00-02:00 (fold=1). A five-minute MW value is 1/12 MWh, so an hour's MWh is the sum
of its intervals' MW / 12; checked by hand: the bulk 2025 file summed this way gives 130,267 MWh for January 2025,
CAISO's own monthly chart CSV (/content/charts/curtailments-monthly.csv) says 130,267.

The daily renewable report is an HTML page whose charts carry the data as JavaScript arrays: curt_hr_tot_<fuel>_
<type>_<scope>_mwh (24 hourly MWh values, fuel solar | wind, type econ | ss | oi, scope local | system) and
tot_gen_<fuel>_iso_telem (288 five-minute MW values of metered output in the CAISO balancing area). The arrays sit on
a fixed wall-clock axis (index i = i:00 local): on 2026-03-08 the 02:00 telemetry slots are "NA"; on the fall-back
day there are still 24 hours, so the repeated hour is not separable and stays NULL here. Checked by hand: for
2025-06-14 the report's hourly sums by fuel and scope equal the bulk file's hour-ending sums hour by hour (solar
local 13:00 hour 1,438 vs 1,439 MWh, rounding).

Series (hourly MWh; location CAISO):
    caiso_curtailment.rt.curtailment.<fuel>                    every day from 2014-05-01 (zero where nothing listed)
    caiso_curtailment.rt.curtailment.<fuel>_<scope>            from 2017-06-01 (local: transmission congestion;
                                                               system: system-wide oversupply)
    caiso_curtailment.rt.curtailment.<fuel>_<type>_<scope>     from 2025-06-01 (economic bids, self-schedule cuts,
                                                               operator instructions)
    caiso_curtailment.actual.generation.<fuel>                 Production sheet output, 2014-05-01 .. 2025-12-31
    caiso_curtailment.actual.generation_telemetry.<fuel>       report telemetry, from 2025-06-01

The two output series differ in level (2025-06-14 noon: production 20,399 MWh vs telemetry 18,130 MWh solar), so they
are kept apart; the curtailed share uses the production series where it exists, else telemetry (power/waste.py).

Depth and size: ten workbooks, 21-68 MB each (about 300 MB in all, read once per backfill: the last two answers are
cached in memory so month-sized chunks of one workbook re-use one download); daily pages ~100 KB. The forward job
keeps url + sha256 only for the workbooks and the page bytes (~100 KB a day, gzipped ~15 KB).
"""

from __future__ import annotations

import hashlib
import re
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from power.registry import INTERNAL, Dataset, Fetched, Series, Terms
from power.sources import ercot
from power.storage import days

SOURCE = "caiso_curtailment"
DEFAULT_ON = True
REQUIRES: tuple[str, ...] = ()
BASE = "https://www.caiso.com/documents/"
TZ = "America/Los_Angeles"
PT = ZoneInfo(TZ)
UTC = timezone.utc
EARLIEST = date(2014, 5, 1)
REPORT_FIRST = date(2025, 6, 1)
REPORT_POSTED_HOUR = 11     # posted about 10:00 PT the next day
MAX_BYTES = 96 * 1024 * 1024

TERMS = Terms(
    operator="California ISO",
    visibility=INTERNAL,
    reason=("Same site terms as CAISO OASIS (power/sources/caiso.py): 'No material or information from this Website "
            "... may be copied, reproduced, republished, uploaded, posted, transmitted or distributed except (i) as "
            "authorized in these Terms of Use ...'. The production and curtailment workbooks and the daily "
            "renewable reports carry no separate licence, so internal. robots.txt disallows only /resources/*."),
    terms_url="https://www.caiso.com/privacy-terms-of-use",
    checked="2026-11-15",
    signup=None,
    attribution="Source: California ISO, Production and Curtailments Data and Daily Renewable Reports",
)


@dataclass(frozen=True)
class Archive:
    first: date
    last: date
    name: str
    production: bool        # read its Production sheet
    curtailment: bool       # read its Curtailments sheet


ARCHIVES = (
    Archive(date(2014, 5, 1), date(2017, 5, 31), "productionandcurtailmentsdata-may1_2014-may31_2017.xlsx", True, True),
    Archive(date(2017, 6, 1), date(2017, 12, 31), "productionandcurtailmentsdata-jun1_2017-dec31_2017.xlsx", True, True),
    *(Archive(date(y, 1, 1), date(y, 12, 31), f"productionandcurtailmentsdata_{y}.xlsx", True, True)
      for y in range(2018, 2024)),
    Archive(date(2024, 1, 1), date(2024, 12, 31), "production-and-curtailments-data-2024.xlsx", True, True),
    # the 2025 workbook's curtailment sheet stops at 2025-05-31; the bulk file has the whole year (identical Jan-May)
    Archive(date(2025, 1, 1), date(2025, 12, 31), "production-and-curtailments-data-2025.xlsx", True, False),
    Archive(date(2025, 1, 1), date(2025, 12, 31), "production-and-curtailments-data-june-december-2025.xlsx",
            False, True),
)
ARCHIVE_LAST = max(a.last for a in ARCHIVES)

FUELS = ("solar", "wind")
SCOPES = ("local", "system")
TYPES = {"econ": "economic", "ss": "selfschedule", "oi": "operator"}
TYPE_NAMES = {"economic": "economic bids", "selfschedule": "self-schedule cuts", "operator": "operator instructions"}
MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")


def curtailment_key(fuel: str, scope: str | None = None, typ: str | None = None) -> str:
    loc = "_".join(x for x in (fuel, typ, scope) if x)
    return f"{SOURCE}.rt.curtailment.{loc}"


def generation_key(fuel: str, telemetry: bool = False) -> str:
    return f"{SOURCE}.actual.{'generation_telemetry' if telemetry else 'generation'}.{fuel}"


def _series() -> tuple[Series, ...]:
    out = []
    for f in FUELS:
        out.append(Series(curtailment_key(f), f"CAISO {f} curtailment, all reasons (MWh, hourly)", "curtailment", "rt",
                          f, "ba", "mwh", "CAISO", {"fuel": f, "scope": None, "type": None}))
        for sc in SCOPES:
            out.append(Series(curtailment_key(f, sc), f"CAISO {f} curtailment, {sc} (MWh, hourly)", "curtailment",
                              "rt", f"{f}_{sc}", "ba", "mwh", "CAISO", {"fuel": f, "scope": sc, "type": None}))
            for t in TYPES.values():
                out.append(Series(curtailment_key(f, sc, t), f"CAISO {f} curtailment, {sc}, {TYPE_NAMES[t]} "
                                  f"(MWh, hourly)", "curtailment", "rt", f"{f}_{t}_{sc}", "ba", "mwh", "CAISO",
                                  {"fuel": f, "scope": sc, "type": t}))
        out.append(Series(generation_key(f), f"CAISO {f} output, production workbook (MWh, hourly)", "generation",
                          "actual", f, "ba", "mwh", "CAISO", {"fuel": f, "basis": "production"}))
        out.append(Series(generation_key(f, True), f"CAISO {f} output, metered telemetry, daily renewable report "
                          f"(MWh, hourly)", "generation", "actual", f"{f}_telemetry", "ba", "mwh", "CAISO",
                          {"fuel": f, "basis": "telemetry"}))
    return tuple(out)


SERIES = _series()


def earliest(now: datetime) -> date:
    return EARLIEST


def latest(now: datetime) -> date:
    t = now.astimezone(PT)
    return t.date() - timedelta(days=1 if t.hour >= REPORT_POSTED_HOUR else 2)


def report_url(d: date) -> str:
    return f"{BASE}daily-renewable-report-{MONTHS[d.month - 1]}-{d.day:02d}-{d.year}.html"


# --------------------------------------------------------------------------- fetch

_BODIES: OrderedDict[str, Fetched] = OrderedDict()   # url -> the answer; two kept (2025 needs two workbooks)
_PARSED: OrderedDict[str, dict] = OrderedDict()      # sha256 -> the workbook's whole parse


def _remember(cache: OrderedDict, k, v, n: int = 2):
    cache[k] = v
    cache.move_to_end(k)
    while len(cache) > n:
        cache.popitem(last=False)


async def _archive(get, a: Archive) -> Fetched:
    url = BASE + a.name
    hit = _BODIES.get(url)
    if hit is not None:
        return Fetched(hit.url, hit.status, hit.ms, hit.body, hit.content_type)
    f = await get.get(url, max_bytes=MAX_BYTES)
    if f.ok:
        _remember(_BODIES, url, f)
    return f


async def fetch(get, start: date, end: date) -> list[Fetched]:
    out: list[Fetched] = []
    for a in ARCHIVES:
        lo, hi = max(start, a.first), min(end, a.last)
        if lo <= hi:
            f = await _archive(get, a)
            f.start, f.end = lo, hi
            out.append(f)
    # reports after the workbooks: where both cover a day (2025-06..12) the report's values are merged last
    for d in days(max(start, REPORT_FIRST), end):
        f = await get.get(report_url(d))
        f.start = f.end = d
        out.append(f)
    return out


# --------------------------------------------------------------------------- parse: workbooks

def _hour_utc(d: date, hour_ending: int, fold: int) -> datetime | None:
    """The UTC start of hour-ending h on local day d; None for a wall-clock hour that does not exist."""
    t = (datetime.combine(d, time(0)) + timedelta(hours=hour_ending - 1)).replace(tzinfo=PT, fold=fold)
    u = t.astimezone(UTC)
    if u.astimezone(PT).replace(tzinfo=None) != t.replace(tzinfo=None):
        return None
    return u


def _num(s: str) -> float | None:
    s = (s or "").strip().replace(",", "")
    if not s:
        return None
    return float(s)


def _day(s: str) -> date:
    """An Excel serial date (its fraction ignored: it does not match the interval) or m/d/yyyy."""
    s = s.strip()
    if re.fullmatch(r"\d+(\.\d*)?", s):
        return date(1899, 12, 30) + timedelta(days=int(float(s)))
    return ercot._day(s.split()[0])


def archive_for(url: str) -> Archive | None:
    return next((a for a in ARCHIVES if url.split("?")[0].endswith(a.name)), None)


def parse_workbook(body: bytes, a: Archive) -> dict[str, dict[datetime, float | None]]:
    """The whole workbook -> hourly MWh per series over the archive's days (curtailment hours with nothing listed
    are 0; output hours missing any of their 12 intervals are left out)."""
    curt: dict[tuple[str, datetime], float] = defaultdict(float)
    gen: dict[tuple[str, datetime], list[float]] = defaultdict(list)
    seen: dict[tuple, int] = defaultdict(int)
    mode, cols, has_reason = None, {}, False
    gen_days: set[date] = set()
    for row in ercot._xlsx_rows(body):
        if not row or not row[0]:
            continue
        if row[0].strip().lower() == "date":
            h = [x.strip().lower() for x in row]
            cols = {name: i for i, name in enumerate(h) if name}
            if "wind curtailment" in cols and "solar curtailment" in cols:
                mode, has_reason = "curt", "reason" in cols
            elif "solar" in cols and "wind" in cols and "load" in cols:
                mode = "prod"
            else:
                raise ValueError(f"caiso_curtailment: unknown sheet header {row[:8]}")
            if "hour" not in cols or "interval" not in cols:
                raise ValueError(f"caiso_curtailment: no hour / interval column in {row[:8]}")
            continue
        if mode is None:
            continue   # the read-me sheet
        if mode == "prod" and not a.production or mode == "curt" and not a.curtailment:
            continue
        try:
            d = _day(row[0])
            he, iv = int(float(row[cols["hour"]])), int(float(row[cols["interval"]]))
        except (ValueError, IndexError) as exc:
            raise ValueError(f"caiso_curtailment: bad row {row[:6]}: {exc}") from exc
        if not (a.first <= d <= a.last):
            continue
        reason = row[cols["reason"]].strip().lower() if has_reason and cols["reason"] < len(row) else ""
        k = (mode, d, he, iv, reason)
        seen[k] += 1
        fold = 1 if seen[k] > 1 else 0          # the fall-back day's second hour-ending 2
        t = _hour_utc(d, he, fold)
        if t is None:
            continue
        if mode == "prod":
            gen_days.add(d)
            for fuel in FUELS:
                v = _num(row[cols[fuel]]) if cols[fuel] < len(row) else None
                if v is not None:
                    gen[(generation_key(fuel), t)].append(v)
            continue
        for fuel in FUELS:
            i = cols[f"{fuel} curtailment"]
            v = _num(row[i]) if i < len(row) else None
            if not v:
                continue
            curt[(curtailment_key(fuel), t)] += v / 12
            if reason in SCOPES:
                curt[(curtailment_key(fuel, reason), t)] += v / 12
            elif reason:
                raise ValueError(f"caiso_curtailment: unknown reason {reason!r}")
    out: dict[str, dict[datetime, float | None]] = defaultdict(dict)
    if a.curtailment:
        keys = [curtailment_key(f) for f in FUELS]
        if has_reason:
            keys += [curtailment_key(f, sc) for f in FUELS for sc in SCOPES]
        for d in days(a.first, a.last):
            start = datetime.combine(d, time(0)).replace(tzinfo=PT).astimezone(UTC)
            end = datetime.combine(d + timedelta(days=1), time(0)).replace(tzinfo=PT).astimezone(UTC)
            t = start
            while t < end:
                for k in keys:
                    out[k][t] = round(curt.get((k, t), 0.0), 4)
                t += timedelta(hours=1)
    for (k, t), vs in gen.items():
        if len(vs) == 12:
            out[k][t] = round(sum(vs) / 12, 4)
    return dict(out)


# --------------------------------------------------------------------------- parse: daily report

_ARRAY = re.compile(r"\b(\w+)\s*=\s*\[([^\]]*)\]")
_DATE = re.compile(r"var\s+chart_date\s*=\s*new\s+Date\(Date\.UTC\((\d{4}),\s*(\d{1,2}),\s*(\d{1,2})\)\)")


def _arrays(text: str) -> dict[str, list[float | None]]:
    out = {}
    for m in _ARRAY.finditer(text):
        name = m.group(1)
        if not (name.startswith("curt_hr_tot_") or name.startswith("tot_gen_")):
            continue
        vals = []
        for x in m.group(2).split(","):
            x = x.strip().strip('"').strip("'")
            try:
                vals.append(float(x))
            except ValueError:
                vals.append(None)      # "NA"
        out[name] = vals
    return out


def parse_report(body: bytes, want: date | None = None) -> dict[str, dict[datetime, float | None]]:
    text = body.decode("utf-8", "replace")
    m = _DATE.search(text)
    if not m:
        raise ValueError("caiso_curtailment: no chart_date in the daily report")
    d = date(int(m.group(1)), int(m.group(2)) + 1, int(m.group(3)))   # JavaScript months are 0-based
    if want and d != want:
        raise ValueError(f"caiso_curtailment: report for {d}, asked for {want}")
    arr = _arrays(text)
    out: dict[str, dict[datetime, float | None]] = defaultdict(dict)
    hours = [_hour_utc(d, i + 1, 0) for i in range(24)]
    for fuel in FUELS:
        by = {}
        for short, typ in TYPES.items():
            for sc in SCOPES:
                name = f"curt_hr_tot_{fuel}_{short}_{sc}_mwh"
                if name not in arr or len(arr[name]) != 24:
                    raise ValueError(f"caiso_curtailment: {name} missing or not 24 hours")
                by[(typ, sc)] = arr[name]
        for i, t in enumerate(hours):
            if t is None:
                continue
            parts = {k: v[i] for k, v in by.items()}
            for (typ, sc), v in parts.items():
                out[curtailment_key(fuel, sc, typ)][t] = v
            for sc in SCOPES:
                xs = [parts[(typ, sc)] for typ in TYPES.values()]
                out[curtailment_key(fuel, sc)][t] = None if None in xs else round(sum(xs), 4)
            xs = list(parts.values())
            out[curtailment_key(fuel)][t] = None if None in xs else round(sum(xs), 4)
        tel = arr.get(f"tot_gen_{fuel}_iso_telem")
        if tel is None or len(tel) != 288:
            raise ValueError(f"caiso_curtailment: tot_gen_{fuel}_iso_telem missing or not 288 intervals")
        for i, t in enumerate(hours):
            xs = tel[i * 12:(i + 1) * 12]
            if t is not None and None not in xs:
                out[generation_key(fuel, True)][t] = round(sum(xs) / 12, 4)
    return dict(out)


def parse(f: Fetched) -> dict[str, dict[datetime, float | None]]:
    a = archive_for(f.url)
    if a is not None:
        h = hashlib.sha256(f.body).hexdigest()
        whole = _PARSED.get(h)
        if whole is None:
            whole = parse_workbook(f.body, a)
            _remember(_PARSED, h, whole)
        lo = datetime.combine(f.start or a.first, time(0)).replace(tzinfo=PT).astimezone(UTC)
        hi = datetime.combine((f.end or a.last) + timedelta(days=1), time(0)).replace(tzinfo=PT).astimezone(UTC)
        out = {k: {t: v for t, v in pts.items() if lo <= t < hi} for k, pts in whole.items()}
        out = {k: v for k, v in out.items() if v}
    else:
        out = parse_report(f.body, f.start if f.start == f.end else None)
    if not out:
        f.empty = True
    return out


DATASETS = (
    Dataset(key="caiso_curtailment.hourly", source=SOURCE, series=SERIES, interval_minutes=60, tz=TZ, chunk_days=31,
            earliest=earliest, latest=latest, fetch=fetch, parse=parse, pace_seconds=5.0, overlap_days=3,
            keep_raw=True, empty_statuses=(404,), align="month",
            description="Wind and solar curtailment by reason + wind / solar output, hourly MWh; workbooks "
                        "2014-05..2025-12 (21-68 MB each, read once), daily renewable reports from 2025-06 (~100 KB)"),
)
