"""SPP Integrated Marketplace: day-ahead (hourly) and real-time (5-minute) LMP at the two SPP trading hubs,
SPPNORTH_HUB and SPPSOUTH_HUB. Public marketplace files on the SPP portal's file browser; no key, no signup.

    GET https://portal.spp.org/file-browser-api/download/da-lmp-by-settlement-location
        ?path=/YYYY/MM/By_Day/DA-LMP-SL-YYYYMMDD0100.csv                  one DA operating day, every location
    GET https://portal.spp.org/file-browser-api/download/rtbm-lmp-by-location
        ?path=/YYYY/MM/By_Day/RTBM-LMP-DAILY-SL-YYYYMMDD.csv              one RTBM operating day, 5-minute
    GET ...?path=/YYYY/YYYY.zip  with  Range: bytes=-70000 / bytes=a-b     an archived year, read member by member

Each file holds every settlement location (DA ~33,000 rows a day, RT ~170,000); only the two hubs are kept. Rows
are "Interval" (Central prevailing time, interval ENDING), a GMT interval-end column, the settlement location,
the pnode, LMP and its components (MLC, MCC, MEC); only LMP is kept, at its GMT interval end minus the interval,
so the DST days come out right (DA 23 / 25 hours, RT 276 / 300 intervals) without reading the local column, which
repeats an hour on the fall-back day. Column names and the time format drift between years ("GMT Interval" vs
"GMTIntervalEnd", "Settlement Location Name" vs "Settlement Location", "11/1/2026 6:00" vs "11/03/2026 07:00:00"),
so columns are found by name and both time formats are read.

Depth: folders go back to 2013 (the Integrated Marketplace opened 2014-03-01); the backfill starts at 2021-01. The
current and prior year are daily files; older years are rolled into one zip per year about 13 months after the
year ends (2024.zip was posted 2026-02-15): DA ~260-300 MB a year, RT ~4.6-5.2 GB a year (zip64), each holding the
daily files (and the 5-minute ones) as members. An archived day is read with HTTP Range: the zip's tail and its
central directory once per year (DA 67 KB, RT 13 MB; the By_Day entries are kept in memory), then that day's
member alone (DA ~480 KB, RT ~6 MB compressed), inflated in slices. A day is looked up where its year should be
and, on a 404, in the other place (the roll-up date is not announced); a 404 in both is "not published yet"
(empty_statuses), re-asked while recent.

Measured sizes (2026-11-05): DA day file 4.1-4.3 MB (2025-03: 3.1 MB), RT day file 39 MB (2025-11) to 51 MB
(2026-10, after the SPP RTO West expansion added the SWPW locations); archived members DA ~480 KB, RT ~5.6-6.6 MB.
None is small, so the forward job keeps url + sha256 only. Day files are posted DA about 13:30 CT the day before
the operating day (and sometimes re-posted days later: 2026-11-01 was re-posted 11-05), RT about 18:00 CT five days
after the operating day. Within a day file the memory is the body (bounded by power/http.MAX_BYTES, 64 MB) plus
one line: rows are filtered as bytes before any decoding. Re-pricing files (RePrice/, e.g. Winter Storm Uri,
2021-02) are not applied: they correct single intervals, and a partial day never replaces a complete one.
"""

from __future__ import annotations

import io
import struct
import zlib
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from power.registry import INTERNAL, Dataset, Fetched, Series, Terms
from power.storage import days

SOURCE = "spp"
DEFAULT_ON = True
REQUIRES: tuple[str, ...] = ()
BASE = "https://portal.spp.org/file-browser-api/download/"
TZ = "America/Chicago"
CT = ZoneInfo(TZ)
UTC = timezone.utc
EARLIEST = date(2021, 1, 1)
DA_PUBLISHED_HOUR = 15     # DA results post about 13:30 CT; tomorrow is "complete" after 15:00
RT_PUBLISHED_DAYS = 5      # the RT day file posts about 18:00 CT five days after the operating day
RT_PUBLISHED_HOUR = 19
TAIL_BYTES = 70000         # the zip's end records (EOCD, zip64 locator and record) sit in the last 22..~100 bytes
SLACK_BYTES = 4096         # a member's local header can carry a longer extra field than its central entry says
INFLATE_SLICE = 256 * 1024

TERMS = Terms(
    operator="Southwest Power Pool",
    visibility=INTERNAL,
    reason=("SPP Terms & Conditions: 'Permission is implicitly granted to copy and distribute ... in whole or in "
            "part (with appropriate citation) EXCEPT when such materials will be used, in whole or in part, within a "
            "commercial publication ... Any commercial use of these materials requires prior, express written "
            "authorization from the author(s) or a duly authorized officer of SPP.' OpenGrid is commercial, so "
            "internal until SPP authorizes it in writing."),
    terms_url="https://www.spp.org/terms-conditions/",
    checked="2026-11-05",
    signup=None,
    attribution="Source: Southwest Power Pool (SPP) Integrated Marketplace",
)

HUBS = {   # settlement location -> name
    "SPPNORTH_HUB": "SPP North hub",
    "SPPSOUTH_HUB": "SPP South hub",
}
MARKETS = {   # market -> (file source, interval minutes)
    "da": ("da-lmp-by-settlement-location", 60),
    "rt": ("rtbm-lmp-by-location", 5),
}
_NEEDLES = tuple(h.encode() for h in HUBS)
_INDEX: dict[str, dict[str, tuple[int, int, int]]] = {}   # archive url -> {member basename: (offset, csize, method)}


def series_key(market: str, hub: str) -> str:
    return f"spp.{market}.lmp.{hub.lower()}"


def _series(market: str) -> tuple[Series, ...]:
    label = {"da": "day-ahead hourly", "rt": "real-time 5-minute"}[market]
    return tuple(Series(key=series_key(market, h), name=f"SPP {name}, {label} LMP", kind="lmp", market=market,
                        location=h, location_type="hub", unit="usd_per_mwh", iso="SPP")
                 for h, name in HUBS.items())


def earliest(now: datetime) -> date:
    return EARLIEST


def latest_da(now: datetime) -> date:
    t = now.astimezone(CT)
    return t.date() + timedelta(days=1) if t.hour >= DA_PUBLISHED_HOUR else t.date()


def latest_rt(now: datetime) -> date:
    t = now.astimezone(CT)
    return t.date() - timedelta(days=RT_PUBLISHED_DAYS if t.hour >= RT_PUBLISHED_HOUR else RT_PUBLISHED_DAYS + 1)


def file_name(market: str, d: date) -> str:
    return f"DA-LMP-SL-{d:%Y%m%d}0100.csv" if market == "da" else f"RTBM-LMP-DAILY-SL-{d:%Y%m%d}.csv"


def day_url(market: str, d: date) -> tuple[str, dict]:
    return BASE + MARKETS[market][0], {"path": f"/{d:%Y}/{d:%m}/By_Day/{file_name(market, d)}"}


def archive_url(market: str, year: int) -> tuple[str, dict]:
    return BASE + MARKETS[market][0], {"path": f"/{year}/{year}.zip"}


def archived(d: date, today: date | None = None) -> bool:
    """Where the day should be: a year is rolled into its zip in mid-February two years on."""
    today = today or datetime.now(CT).date()
    return today >= date(d.year + 2, 2, 16)


# --------------------------------------------------------------------------- the yearly zips (HTTP Range)

def central_directory(tail: bytes) -> tuple[int, int]:
    """(offset, size) of the central directory from the zip's last bytes (zip64 when the 32-bit fields overflow)."""
    i = tail.rfind(b"PK\x05\x06")
    if i < 0 or len(tail) < i + 22:
        raise ValueError("spp: no end-of-central-directory record in the zip tail")
    n, size, off = struct.unpack("<HII", tail[i + 10:i + 20])
    if off == 0xFFFFFFFF or size == 0xFFFFFFFF or n == 0xFFFF:
        j = tail.rfind(b"PK\x06\x06", 0, i)
        if j < 0 or len(tail) < j + 56:
            raise ValueError("spp: zip64 archive without a zip64 end record in the tail")
        size, off = struct.unpack("<QQ", tail[j + 40:j + 56])
    return off, size


def index_entries(cd: bytes) -> dict[str, tuple[int, int, int]]:
    """{member basename: (local header offset, compressed size, method)} for the By_Day members only."""
    out: dict[str, tuple[int, int, int]] = {}
    p = 0
    while p + 46 <= len(cd) and cd[p:p + 4] == b"PK\x01\x02":
        method, = struct.unpack("<H", cd[p + 10:p + 12])
        csize, usize = struct.unpack("<II", cd[p + 20:p + 28])
        nl, el, cl = struct.unpack("<HHH", cd[p + 28:p + 34])
        off, = struct.unpack("<I", cd[p + 42:p + 46])
        name = cd[p + 46:p + 46 + nl].decode("utf-8", "replace")
        if 0xFFFFFFFF in (csize, usize, off):          # zip64 extended information (header 0x0001)
            extra, q = cd[p + 46 + nl:p + 46 + nl + el], 0
            while q + 4 <= len(extra):
                hid, hl = struct.unpack("<HH", extra[q:q + 4])
                if hid == 1:
                    vals, r = extra[q + 4:q + 4 + hl], 0
                    for field in ("usize", "csize", "off"):
                        cur = {"usize": usize, "csize": csize, "off": off}[field]
                        if cur == 0xFFFFFFFF and r + 8 <= len(vals):
                            v, = struct.unpack("<Q", vals[r:r + 8])
                            r += 8
                            usize, csize, off = (v, csize, off) if field == "usize" else \
                                (usize, v, off) if field == "csize" else (usize, csize, v)
                    break
                q += 4 + hl
        if "/By_Day/" in name and name.lower().endswith(".csv"):
            out[name.rsplit("/", 1)[1]] = (off, csize, method)
        p += 46 + nl + el + cl
    if p == 0 and cd:
        raise ValueError("spp: the central directory does not start with a central file header")
    return out


async def _archive_index(get, market: str, year: int) -> tuple[dict | None, Fetched | None]:
    url, params = archive_url(market, year)
    key = f"{url}?{params['path']}"
    if key in _INDEX:
        return _INDEX[key], None
    f = await get.get(url, params=params, headers={"Range": f"bytes=-{TAIL_BYTES}"})
    if not f.ok:
        return None, f
    try:
        off, size = central_directory(f.body)
    except ValueError as exc:
        f.error = str(exc)
        return None, f
    c = await get.get(url, params=params, headers={"Range": f"bytes={off}-{off + size - 1}"})
    if not c.ok:
        return None, c
    try:
        idx = index_entries(c.body)
    except ValueError as exc:
        c.error = str(exc)
        return None, c
    _INDEX[key] = idx
    return idx, None


async def _from_archive(get, market: str, d: date) -> Fetched:
    idx, failed = await _archive_index(get, market, d.year)
    if idx is None:
        return failed
    url, params = archive_url(market, d.year)
    name = file_name(market, d)
    if name not in idx:
        return Fetched(f"{url}?path={params['path']}#{name}", 404, None, None, error=f"{name} not in the archive")
    off, csize, _ = idx[name]
    f = await get.get(url, params=params, headers={"Range": f"bytes={off}-{off + 30 + csize + SLACK_BYTES - 1}"})
    f.url = f"{f.url}#{name}"[:1000]
    return f


async def _from_day_file(get, market: str, d: date) -> Fetched:
    url, params = day_url(market, d)
    return await get.get(url, params=params)


def _make_fetch(market: str):
    async def fetch(get, start: date, end: date, now: datetime | None = None) -> list[Fetched]:
        today = (now or datetime.now(UTC)).astimezone(CT).date()
        out = []
        for d in days(start, end):
            order = (_from_archive, _from_day_file) if archived(d, today) else (_from_day_file, _from_archive)
            first = None
            for how in order:
                f = await how(get, market, d)
                if f.ok or f.status != 404:
                    break
                first = first or f
            else:
                f = first
            f.start = f.end = d
            out.append(f)
        return out
    return fetch


# --------------------------------------------------------------------------- parsing

def _lines(body: bytes):
    """The CSV's lines as bytes: a plain file, or one zip member (local header + raw deflate) inflated in slices."""
    if body[:4] != b"PK\x03\x04":
        yield from io.BytesIO(body)
        return
    if len(body) < 30:
        raise ValueError("spp: truncated zip member")
    method, = struct.unpack("<H", body[8:10])
    nl, el = struct.unpack("<HH", body[26:30])
    data = memoryview(body)[30 + nl + el:]
    if method == 0:
        csize, = struct.unpack("<I", body[18:22])
        yield from io.BytesIO(bytes(data[:csize]))
        return
    if method != 8:
        raise ValueError(f"spp: zip member compression method {method}")
    z = zlib.decompressobj(-15)
    rest = b""
    for i in range(0, len(data), INFLATE_SLICE):
        try:
            chunk = z.decompress(data[i:i + INFLATE_SLICE])
        except zlib.error as exc:
            raise ValueError(f"spp: zip member does not inflate: {exc}") from exc
        if chunk:
            *full, rest = (rest + chunk).split(b"\n")
            for line in full:
                yield line + b"\n"
        if z.eof:
            break
    if not z.eof:
        raise ValueError("spp: zip member cut short (range too small)")
    if rest:
        yield rest


def _time(s: str) -> datetime:
    """'11/03/2026 07:00:00' or '11/1/2026 6:00' (GMT) -> aware UTC."""
    d, _, t = s.strip().partition(" ")
    m, dd, y = d.split("/")
    hms = (t.split(":") + ["0", "0"])[:3]
    return datetime(int(y), int(m), int(dd), tzinfo=UTC) + timedelta(hours=int(hms[0]), minutes=int(hms[1]),
                                                                     seconds=int(hms[2] or 0))


def _columns(header: bytes) -> tuple[int, int, int]:
    cols = [c.strip().lower().replace(" ", "").replace("_", "") for c in header.decode("utf-8-sig").split(",")]
    gmt = next((i for i, c in enumerate(cols) if c.startswith("gmt")), None)
    loc = next((i for i, c in enumerate(cols) if c in ("settlementlocation", "settlementlocationname")), None)
    lmp = cols.index("lmp") if "lmp" in cols else None
    if None in (gmt, loc, lmp):
        raise ValueError(f"spp: columns {cols} lack a GMT interval, a settlement location or LMP")
    return gmt, loc, lmp


def _make_parse(market: str):
    step = timedelta(minutes=MARKETS[market][1])

    def parse(f: Fetched) -> dict[str, dict[datetime, float | None]]:
        lines = _lines(f.body)
        header = next(lines, b"")
        if not header.strip():
            raise ValueError("spp: empty file")
        if header.lstrip()[:1] in (b"<", b"{"):
            raise ValueError(f"spp: not a CSV: {header[:200]!r}")
        gmt, loc, lmp = _columns(header)
        out: dict[str, dict[datetime, float | None]] = {}
        for line in lines:
            if not any(n in line for n in _NEEDLES):
                continue
            row = line.decode("utf-8", "replace").rstrip("\r\n").split(",")
            hub = row[loc].strip() if len(row) > loc else ""
            if hub not in HUBS:
                continue
            try:
                t = _time(row[gmt]) - step
                v = row[lmp].strip()
                val = float(v) if v else None
            except (ValueError, IndexError) as exc:
                raise ValueError(f"spp: bad row {line[:200]!r}: {exc}") from exc
            out.setdefault(series_key(market, hub), {})[t] = val
        if not out:
            f.empty = True
        return out
    return parse


DATASETS = (
    Dataset(key="spp.da_lmp", source=SOURCE, series=_series("da"), interval_minutes=60, tz=TZ, chunk_days=7,
            earliest=earliest, latest=latest_da, fetch=_make_fetch("da"), parse=_make_parse("da"),
            pace_seconds=2.0, overlap_days=2, keep_raw=False, empty_statuses=(404,),
            description="DA LMP by settlement location, hourly; 2 hubs; ~4.2 MB per day file, ~480 KB per archived "
                        "member (url + sha256 only)"),
    Dataset(key="spp.rt_lmp", source=SOURCE, series=_series("rt"), interval_minutes=5, tz=TZ, chunk_days=1,
            earliest=earliest, latest=latest_rt, fetch=_make_fetch("rt"), parse=_make_parse("rt"),
            pace_seconds=2.0, overlap_days=1, keep_raw=False, empty_statuses=(404,),
            description="RTBM LMP by location, 5-minute; 2 hubs; 39-51 MB per day file, ~6 MB per archived member "
                        "(url + sha256 only)"),
)
