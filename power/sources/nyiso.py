"""NYISO MIS: day-ahead (hourly) and real-time (5-minute) zonal LBMP for the 11 NYCA zones and the 4 external
proxy zones (H Q, NPX, O H, PJM: they are in the same files, so they cost nothing). Public CSVs, no key, no signup.

    GET https://mis.nyiso.com/public/csv/damlbmp/YYYYMMDDdamlbmp_zone.csv           one market day (recent only)
    GET https://mis.nyiso.com/public/csv/damlbmp/YYYYMM01damlbmp_zone_csv.zip       one month: a zip of the days
    GET https://mis.nyiso.com/public/csv/realtime/YYYYMMDDrealtime_zone.csv         (same, real-time 5-minute)
    GET https://mis.nyiso.com/public/csv/realtime/YYYYMM01realtime_zone_csv.zip

Each CSV is one market day, one row per (time stamp, zone): Time Stamp, Name, PTID, LBMP ($/MWHr), Marginal Cost
Losses, Marginal Cost Congestion; only LBMP is kept. Time stamps are Eastern prevailing time with NO zone label: on
the fall-back day the 01:xx labels simply appear twice, the first run EDT and the second EST, so a repeated (zone,
label) in a file is read as the later (fold=1) instant. Day-ahead stamps are the hour's START (00:00 .. 23:00);
real-time stamps are the 5-minute interval's END (00:05 .. next day 00:00), so 5 minutes are subtracted. RTD also
posts off-cycle intervals (a few a day, e.g. 08:00:51, 08:02:39, 08:07:57, 08:10:03): off the 5-minute grid, they
are dropped, except one at most SNAP_SECONDS past a boundary that has no on-time row (2025-11-05 has 08:10:03 and no
08:10:00): it fills that slot, so a day is not left one interval short. Real time
is stored at its native 5-minute granularity: 15 zones x 288 values a day as real[] (~1.2 KB per series-day).

Fetching: the monthly zip for every month that is not recent (one request per month, held in memory, read member by
member, never written to disk), the daily CSV for days within DAILY_DAYS of the run's clock (daily files are kept
about 10 days: measured 2026-11-05, 10-26 answered and 10-25 was a 404; the current month's zip also exists,
partial). Chunks are month-aligned (align="month"), so each month's zip is downloaded once. A 404 is "not published"
(empty_statuses): a daily file posted late is re-asked, not counted as a failure.

Depth: the monthly zips go back to 1999-11 (DA) / 2000 (RT); the same CSV layout was verified for 2021-01, so the
backfill starts there (EARLIEST). Measured sizes: DA one day 16.8 KB CSV (3.2 KB gzipped), one month 112 KB zipped;
RT one day 236 KB CSV (31 KB gzipped), one month 1.1 MB zipped. The forward job keeps DA bytes and RT url + sha256
(forward re-reads the overlap days on every run: 31 KB x 3 days x 4 runs a day is too much to keep).
"""

from __future__ import annotations

import csv
import io
import zipfile
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from power.registry import INTERNAL, Dataset, Fetched, Series, Terms

SOURCE = "nyiso"
DEFAULT_ON = True
REQUIRES: tuple[str, ...] = ()
BASE = "https://mis.nyiso.com/public/csv"
TZ = "America/New_York"
ET = ZoneInfo(TZ)
EARLIEST = date(2021, 1, 1)
DAILY_DAYS = 7              # days within this of today come from the daily CSVs (kept ~10 days), older from zips
DA_PUBLISHED_HOUR = 12      # DAM results post around 11:00 ET; tomorrow is "complete" after 12:00
SNAP_SECONDS = 30         # an off-cycle RT stamp this late for a 5-minute boundary stands in for it

TERMS = Terms(
    operator="New York ISO",
    visibility=INTERNAL,
    reason=("Legal notice: 'Access to this Web site does not confer any license or ownership interest in either the "
            "form or content of the Web site, including any confidential or proprietary information or intellectual "
            "property of any kind or nature, and the NYISO hereby expressly reserves such rights and property in its "
            "entirety.' No license to redistribute the MIS market data is granted, so internal."),
    terms_url="https://www.nyiso.com/legal-notice",
    checked="2026-11-05",
    signup=None,
    attribution="Source: NYISO",
)

ZONES = {   # NYISO zone name -> (series location id, NYISO zone letter or None for external proxies, name)
    "WEST": ("west", "A", "West"),
    "GENESE": ("genese", "B", "Genesee"),
    "CENTRL": ("centrl", "C", "Central"),
    "NORTH": ("north", "D", "North"),
    "MHK VL": ("mhk_vl", "E", "Mohawk Valley"),
    "CAPITL": ("capitl", "F", "Capital"),
    "HUD VL": ("hud_vl", "G", "Hudson Valley"),
    "MILLWD": ("millwd", "H", "Millwood"),
    "DUNWOD": ("dunwod", "I", "Dunwoodie"),
    "N.Y.C.": ("nyc", "J", "New York City"),
    "LONGIL": ("longil", "K", "Long Island"),
    "H Q": ("hq", None, "Hydro-Quebec external proxy"),
    "NPX": ("npx", None, "ISO-NE external proxy"),
    "O H": ("oh", None, "Ontario (IESO) external proxy"),
    "PJM": ("pjm", None, "PJM external proxy"),
}
MARKETS = {   # market -> (directory, file stem, interval minutes, stamp is the interval END)
    "da": ("damlbmp", "damlbmp_zone", 60, False),
    "rt": ("realtime", "realtime_zone", 5, True),
}
LBMP = "LBMP ($/MWHr)"


def series_key(market: str, zone: str) -> str:
    return f"nyiso.{market}.lmp.{ZONES[zone][0]}"


def _series(market: str) -> tuple[Series, ...]:
    label = {"da": "day-ahead hourly", "rt": "real-time 5-minute"}[market]
    return tuple(Series(key=series_key(market, z), name=f"NYISO {name}, {label} zonal LBMP", kind="lmp",
                        market=market, location=z, location_type="zone", unit="usd_per_mwh", iso="NYISO",
                        meta={"zone": letter} if letter else {"external_proxy": True})
                 for z, (_, letter, name) in ZONES.items())


def earliest(now: datetime) -> date:
    return EARLIEST


def latest_da(now: datetime) -> date:
    t = now.astimezone(ET)
    return t.date() + timedelta(days=1) if t.hour >= DA_PUBLISHED_HOUR else t.date()


def latest_rt(now: datetime) -> date:
    return now.astimezone(ET).date() - timedelta(days=1)


def daily_url(market: str, d: date) -> str:
    folder, stem, _, _ = MARKETS[market]
    return f"{BASE}/{folder}/{d:%Y%m%d}{stem}.csv"


def monthly_url(market: str, d: date) -> str:
    folder, stem, _, _ = MARKETS[market]
    return f"{BASE}/{folder}/{d:%Y%m}01{stem}_csv.zip"


def months(start: date, end: date) -> list[tuple[date, date]]:
    """start..end split at month boundaries."""
    out, a = [], start
    while a <= end:
        nxt = (a.replace(day=1) + timedelta(days=32)).replace(day=1)
        b = min(end, nxt - timedelta(days=1))
        out.append((a, b))
        a = nxt
    return out



def _make_fetch(market: str):
    async def fetch(get, start: date, end: date, now: datetime | None = None) -> list[Fetched]:
        out: list[Fetched] = []
        recent = (now or datetime.now(timezone.utc)).astimezone(ET).date() - timedelta(days=DAILY_DAYS)
        for a, b in months(start, end):
            if a >= recent:
                d = a
                while d <= b:
                    f = await get.get(daily_url(market, d))
                    f.start = f.end = d
                    out.append(f)
                    d += timedelta(days=1)
            else:
                f = await get.get(monthly_url(market, a))
                f.start, f.end = a, b
                out.append(f)
        return out
    return fetch


def _files(body: bytes):
    """The CSV text streams in an answer: a monthly zip (member by member) or one daily CSV."""
    if body[:4] == b"PK\x03\x04":
        with zipfile.ZipFile(io.BytesIO(body)) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".csv")]
            if not names:
                raise ValueError(f"nyiso: no CSV in the zip: {z.namelist()[:5]}")
            for n in sorted(names):
                with z.open(n) as raw:
                    yield io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
    else:
        head = body[:40].lstrip(b"\xef\xbb\xbf").lstrip(b'"')
        if not head.startswith(b"Time Stamp"):
            raise ValueError(f"nyiso: not a zonal LBMP CSV: {body[:200]!r}")
        yield io.StringIO(body.decode("utf-8-sig"), newline="")


def _stamp(s: str) -> datetime:
    for fmt in ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    raise ValueError(f"nyiso: bad time stamp {s!r}")


def _make_parse(market: str):
    _, _, minutes, ending = MARKETS[market]
    step = timedelta(minutes=minutes)

    def parse(f: Fetched) -> dict[str, dict[datetime, float | None]]:
        out: dict[str, dict[datetime, float | None]] = {}
        for fh in _files(f.body):
            reader = csv.DictReader(fh)
            need = {"Time Stamp", "Name", LBMP}
            if not need <= set(reader.fieldnames or ()):
                raise ValueError(f"nyiso: columns {reader.fieldnames} lack {need}")
            seen: set[tuple[str, str]] = set()      # (zone, label) already read in THIS file: a repeat is the EST run
            late: list[tuple[str, datetime, float | None]] = []
            for r in reader:
                zone, label = r["Name"], r["Time Stamp"]
                if zone not in ZONES:
                    continue
                local = _stamp(label)
                fold = 1 if (zone, label) in seen else 0
                seen.add((zone, label))
                t = local.replace(tzinfo=ET, fold=fold).astimezone(timezone.utc)
                v = (r[LBMP] or "").strip()
                val = float(v) if v else None
                off = (t.minute % minutes) * 60 + t.second
                if off:
                    # an off-cycle RTD interval: kept only when it is a few seconds late for a 5-minute boundary
                    # that has no on-time row (e.g. 08:10:03 standing in for 08:10:00), else dropped
                    if off <= SNAP_SECONDS:
                        late.append((zone, t.replace(second=0) - timedelta(minutes=t.minute % minutes), val))
                    continue
                out.setdefault(series_key(market, zone), {})[t - step if ending else t] = val
            for zone, t, val in late:
                pts = out.setdefault(series_key(market, zone), {})
                pts.setdefault(t - step if ending else t, val)
        if not out:
            f.empty = True
        return out
    return parse


DATASETS = (
    Dataset(key="nyiso.da_lmp", source=SOURCE, series=_series("da"), interval_minutes=60, tz=TZ, chunk_days=31,
            earliest=earliest, latest=latest_da, fetch=_make_fetch("da"), parse=_make_parse("da"),
            pace_seconds=2.0, overlap_days=2, keep_raw=True, empty_statuses=(404,), align="month",
            description="damlbmp zone, hourly LBMP; 15 zones; 16.8 KB CSV (3.2 KB gzipped) per day (bytes kept forward)"),
    Dataset(key="nyiso.rt_lmp", source=SOURCE, series=_series("rt"), interval_minutes=5, tz=TZ, chunk_days=31,
            earliest=earliest, latest=latest_rt, fetch=_make_fetch("rt"), parse=_make_parse("rt"),
            pace_seconds=2.0, overlap_days=2, keep_raw=False, empty_statuses=(404,), align="month",
            description="realtime zone, 5-minute LBMP; 15 zones; 236 KB CSV (31 KB gzipped) per day (url + sha256 only)"),
)
