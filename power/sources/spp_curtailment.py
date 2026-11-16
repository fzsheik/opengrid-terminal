"""SPP wind and solar curtailment (methodology/power-waste.md): what SPP's real-time market and its operators cut
from variable energy resources, by fuel and reason, hourly. Public marketplace files; no key, no signup.

    GET https://portal.spp.org/file-browser-api/download/ver-curtailments?path=/YYYY/MM/VER-Curtailments-YYYYMMDD.csv
        one operating day (~55 KB), posted about 01:00 CT the next day
    GET https://portal.spp.org/file-browser-api/download/ver-curtailments?path=/YYYY/YYYY.zip
        an archived year (2014 0.8 MB .. 2024 3.8 MB): the daily files as members YYYY/MM/VER-Curtailments-*.csv,
        plus monthly files and an annual roll-up, which are NOT read (they repeat the daily intervals)

Columns: LocalIntervalEnding, GMTIntervalEnding, WindRedispatchCurtailments, WindManualCurtailments,
WindCurtailedForEnergy, SolarRedispatchCurtailments, SolarManualCurtailments, SolarCurtailedForEnergy[, BAA].
Five-minute, interval ENDING, MW. 2014 files carry only the two wind columns; from 2026 a BAA column splits each
interval into SPP and SWPW (SPP West) rows, summed here. Measured: CurtailedForEnergy is never above Redispatch and
never non-zero when Redispatch is zero, so it is the oversupply part of Redispatch, not a third reason; it is kept as
its own series and never added. Redispatch: the real-time market dispatched the unit down (congestion or price);
Manual: an operator instruction for reliability. Self-curtailment by the owner is not in these files, so they
understate total curtailment.

Each interval is placed at its GMT end minus 5 minutes (the local column repeats an hour on the fall-back day), and
an hour's MWh is the sum of its intervals' MW / 12. The year archives exist through 2024 (2024.zip was posted
2026-02-15); later years are daily files; a daily 404 for a past year falls back to the year archive (the roll-up
date is not announced). Depth: from 2014-03-07.
"""

from __future__ import annotations

import io
import zipfile
from collections import OrderedDict, defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from power.registry import INTERNAL, Dataset, Fetched, Series, Terms
from power.sources import spp
from power.storage import days

SOURCE = "spp_curtailment"
DEFAULT_ON = True
REQUIRES: tuple[str, ...] = ()
URL = "https://portal.spp.org/file-browser-api/download/ver-curtailments"
TZ = "America/Chicago"
CT = ZoneInfo(TZ)
UTC = timezone.utc
EARLIEST = date(2014, 3, 1)
ZIP_THROUGH = 2024          # the last year measured rolled into a year archive (2026-11-15)
PUBLISHED_HOUR = 3          # the day's file posts about 01:00 CT

TERMS = Terms(
    operator="Southwest Power Pool",
    visibility=INTERNAL,
    reason=spp.TERMS.reason + " The VER curtailment files are on the same portal under the same terms.",
    terms_url=spp.TERMS.terms_url,
    checked="2026-11-15",
    signup=None,
    attribution="Source: Southwest Power Pool (SPP), VER curtailments",
)

FUELS = ("wind", "solar")
COLUMNS = {   # (fuel, part) -> column
    ("wind", "redispatch"): "WindRedispatchCurtailments", ("wind", "manual"): "WindManualCurtailments",
    ("wind", "energy"): "WindCurtailedForEnergy", ("solar", "redispatch"): "SolarRedispatchCurtailments",
    ("solar", "manual"): "SolarManualCurtailments", ("solar", "energy"): "SolarCurtailedForEnergy",
}
PART_META = {   # part -> (type, scope, words)
    "redispatch": ("redispatch", None, "market redispatch"),
    "manual": ("manual", None, "manual (operator instruction)"),
    "energy": ("redispatch", "energy", "market redispatch for energy (oversupply; part of redispatch)"),
}


def curtailment_key(fuel: str, part: str | None = None) -> str:
    return f"{SOURCE}.rt.curtailment.{fuel}" + (f"_{part}" if part else "")


def _series() -> tuple[Series, ...]:
    out = []
    for f in FUELS:
        out.append(Series(curtailment_key(f), f"SPP {f} curtailment, redispatch + manual (MWh, hourly)",
                          "curtailment", "rt", f, "ba", "mwh", "SPP", {"fuel": f, "scope": None, "type": None}))
        for part, (typ, scope, words) in PART_META.items():
            out.append(Series(curtailment_key(f, part), f"SPP {f} curtailment, {words} (MWh, hourly)", "curtailment",
                              "rt", f"{f}_{part}", "ba", "mwh", "SPP", {"fuel": f, "scope": scope, "type": typ}))
    return tuple(out)


SERIES = _series()


def earliest(now: datetime) -> date:
    return EARLIEST


def latest(now: datetime) -> date:
    t = now.astimezone(CT)
    return t.date() - timedelta(days=1 if t.hour >= PUBLISHED_HOUR else 2)


def day_path(d: date) -> str:
    return f"/{d:%Y}/{d:%m}/VER-Curtailments-{d:%Y%m%d}.csv"


def member_name(d: date) -> str:
    return f"VER-Curtailments-{d:%Y%m%d}.csv"


_ZIPS: OrderedDict[int, Fetched] = OrderedDict()   # year -> the archive answer (one kept: <= 4 MB)


async def _zip(get, year: int) -> Fetched:
    hit = _ZIPS.get(year)
    if hit is not None:
        return Fetched(hit.url, hit.status, hit.ms, hit.body, hit.content_type)
    f = await get.get(URL, params={"path": f"/{year}/{year}.zip"})
    if f.ok:
        _ZIPS.clear()
        _ZIPS[year] = f
    return f


async def fetch(get, start: date, end: date, now: datetime | None = None) -> list[Fetched]:
    this_year = (now or datetime.now(UTC)).astimezone(CT).year
    out: list[Fetched] = []
    archived: list[date] = []
    for d in days(start, end):
        if d.year <= ZIP_THROUGH:
            archived.append(d)
            continue
        f = await get.get(URL, params={"path": day_path(d)})
        if f.status == 404 and d.year < this_year:
            archived.append(d)          # rolled into the year archive since
            continue
        f.start = f.end = d
        out.append(f)
    for year in sorted({d.year for d in archived}):
        ds = [d for d in archived if d.year == year]
        f = await _zip(get, year)
        f.start, f.end = ds[0], ds[-1]
        out.append(f)
    return out


def parse_csv(data: bytes) -> dict[str, dict[datetime, float]]:
    """One daily file -> hourly MWh per series (an hour holds what its five-minute rows give)."""
    lines = data.decode("utf-8-sig", "replace").splitlines()
    if not lines:
        return {}
    head = [h.strip() for h in lines[0].split(",")]
    if "GMTIntervalEnding" not in head:
        raise ValueError(f"spp_curtailment: no GMTIntervalEnding column in {head}")
    gi = head.index("GMTIntervalEnding")
    cols = {k: head.index(c) for k, c in COLUMNS.items() if c in head}
    if ("wind", "redispatch") not in cols:
        raise ValueError(f"spp_curtailment: no wind redispatch column in {head}")
    acc: dict[tuple[str, datetime], float] = defaultdict(float)
    for line in lines[1:]:
        row = [x.strip() for x in line.split(",")]
        if len(row) <= gi or not row[gi]:
            continue
        vals = {k: row[i] for k, i in cols.items() if i < len(row)}
        if not any(vals.values()):
            continue                     # an empty row (2026-04-01 00:05)
        t_end = datetime.strptime(row[gi].split(".")[0], "%m/%d/%Y %H:%M:%S").replace(tzinfo=UTC)
        t = t_end - timedelta(minutes=5)
        hour = t.replace(minute=0, second=0)
        for fuel in FUELS:
            parts = {}
            for part in PART_META:
                v = vals.get((fuel, part), "")
                if v:
                    parts[part] = float(v)
            for part, v in parts.items():
                acc[(curtailment_key(fuel, part), hour)] += v / 12
            if "redispatch" in parts or "manual" in parts:   # blank columns (2014 solar, some 2024 days): not given
                acc[(curtailment_key(fuel), hour)] += (parts.get("redispatch", 0.0) + parts.get("manual", 0.0)) / 12
    out: dict[str, dict[datetime, float]] = defaultdict(dict)
    for (k, hour), v in acc.items():
        out[k][hour] = round(v, 4)
    return dict(out)


def parse(f: Fetched) -> dict[str, dict[datetime, float | None]]:
    if f.body[:4] == b"PK\x03\x04":
        out: dict[str, dict[datetime, float]] = defaultdict(dict)
        with zipfile.ZipFile(io.BytesIO(f.body)) as z:
            members = {n.rsplit("/", 1)[-1]: n for n in z.namelist()}
            for d in days(f.start, f.end):
                n = members.get(member_name(d))
                if n is None:
                    continue
                for k, pts in parse_csv(z.read(n)).items():
                    out[k].update(pts)
        out = dict(out)
    else:
        out = parse_csv(f.body)
    if not out:
        f.empty = True
    return out


DATASETS = (
    Dataset(key="spp_curtailment.hourly", source=SOURCE, series=SERIES, interval_minutes=60, tz=TZ, chunk_days=31,
            earliest=earliest, latest=latest, fetch=fetch, parse=parse, pace_seconds=2.5, overlap_days=3,
            keep_raw=True, empty_statuses=(404,), align="month",
            description="VER curtailments, wind + solar, redispatch / manual / for-energy, hourly MWh; year zips "
                        "2014-2024 (<= 4 MB), daily files from 2025 (~55 KB, kept forward)"),
)
