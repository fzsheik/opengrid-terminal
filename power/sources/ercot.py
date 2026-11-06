"""ERCOT: day-ahead (hourly) and real-time (15-minute) settlement point prices at the trading hubs and load zones,
from ERCOT's public MIS reports. No key, no signup (the Public API at api.ercot.com needs a registration; the same
data is posted keyless as MIS documents, so the API is not used).

    GET https://www.ercot.com/misapp/servlets/IceDocListJsonWS?reportTypeId=<id>     the documents of a report
    GET https://www.ercot.com/misdownload/servlets/mirDownload?doclookupId=<DocID>   one document (a zip)

    13060  Historical DAM Load Zone and Hub Prices   one zip per year (DAMLZHBSPP_YYYY), 1.7-2.1 MB: an xlsx
    13061  Historical RTM Load Zone and Hub Prices   one zip per year (RTMLZHBSPP_YYYY), 11-14 MB: an xlsx
    12331  NP4-190-CD DAM Settlement Point Prices    one zip per operating day (~188 KB), kept ~31 days
    12301  NP6-905-CD Settlement Point Prices at Resource Nodes, Hubs and Load Zones: one zip per 15-minute interval
           (~8 KB), kept 7 days

The yearly archives hold one sheet per month (Jan..Dec) with only the hubs and load zones: DA "Delivery Date, Hour
Ending, Repeated Hour Flag, Settlement Point, Settlement Point Price"; RT "Delivery Date, Delivery Hour, Delivery
Interval, Repeated Hour Flag, Settlement Point Name, Settlement Point Type, Settlement Point Price". The current
year's file is re-posted on the 1st of each month through the month before. The daily files hold every settlement
point (NP4-190: "DeliveryDate, HourEnding, SettlementPoint, SettlementPointPrice, DSTFlag"; NP6-905 adds
DeliveryHour / DeliveryInterval and the type). The xlsx is read with the standard library (zip + iterparse).

Times are Central prevailing, hour ENDING. The fall-back day has 25 hours: HE 02 twice, the second flagged
"Y" (DSTFlag / Repeated Hour Flag), i.e. 01:00-02:00 CST; the spring-forward day has no HE 03. A value is placed
at its local start (HE h, interval i -> (h-1):(i-1)*15) with fold=1 when flagged, then UTC. RT load zones come twice
(type LZ and LZEW, energy-weighted); LZ is the zone's settlement price and the one kept (the DA has one per zone).

A day is read from the newest place that has it: RT days of the last RT_DAILY_DAYS from the 15-minute files, DA
days of the last DA_DAILY_DAYS from the daily files, older days from the year's archive. A recent day is read only
once every file of it is listed (96 intervals, 92 / 100 on the DST days); an archive answer covers only the months
it holds (the current year's: through the month before its publish date). A day no document can hold yet gets no
answer at all, so it is never marked stored (nor "not published", which turns final after 14 days): it is asked
again on every run until the next monthly archive has it. Chunks are month-aligned and the last archive read per dataset is cached in memory (one
download per year file: ERCOT's API terms allow at most three downloads of a report a year).

Depth: archives from 2010 (the nodal market opened 2010-12-01); the backfill starts at 2011-01. Measured sizes:
DA archive ~2 MB a year (~5.6 KB per day for 14 series), RT archive ~14 MB a year (~38 KB per day); NP4-190 day
188 KB, NP6-905 interval 8 KB (~800 KB a day). The forward job keeps DA bytes (~190 KB a day: no) and RT bytes
(no): url + sha256 only. ERCOT's API limit is 30 requests a minute; the MIS pace here is one request every 2.5 s.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
import xml.etree.ElementTree as ET
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from power.registry import PUBLIC, Dataset, Fetched, Series, Terms
from power.storage import days, slots

SOURCE = "ercot"
DEFAULT_ON = True
REQUIRES: tuple[str, ...] = ()
LIST_URL = "https://www.ercot.com/misapp/servlets/IceDocListJsonWS"
DOC_URL = "https://www.ercot.com/misdownload/servlets/mirDownload"
TZ = "America/Chicago"
CT = ZoneInfo(TZ)
UTC = timezone.utc
EARLIEST = date(2011, 1, 1)
DA_PUBLISHED_HOUR = 14     # the DAM posts about 12:30 CT; tomorrow is "complete" after 14:00
DA_DAILY_DAYS = 25         # NP4-190 day files are kept ~31 days
RT_DAILY_DAYS = 5          # NP6-905 interval files are kept 7 days

TERMS = Terms(
    operator="Electric Reliability Council of Texas",
    visibility=PUBLIC,
    reason=("ERCOT Terms of Use, section 5: 'The publicly available contents of this website may be used, "
            "reproduced, and redistributed, provided that the contents are not modified ... Notwithstanding the "
            "foregoing, raw data provided in public portions of this website may be used, reproduced, and "
            "redistributed in compilations, charts, and analyses without maintaining such notices.' Settlement "
            "point prices are raw public data, published unmodified, so public. Section 6 bars use that "
            "'negatively affects the performance of this website'; the API terms add 'You may not download the "
            "same report ... more than three times within a 12-month period' (each document is read once, its "
            "re-reads within the overlap only). ERCOT's name and logo are trademarks: attribution only."),
    terms_url="https://www.ercot.com/help/terms",
    checked="2026-11-05",
    signup=None,
    attribution="Source: ERCOT",
)

POINTS = {   # settlement point -> (type, name)
    "HB_HOUSTON": ("hub", "Houston hub"),
    "HB_NORTH": ("hub", "North hub"),
    "HB_SOUTH": ("hub", "South hub"),
    "HB_WEST": ("hub", "West hub"),
    "HB_HUBAVG": ("hub", "hub average"),
    "HB_BUSAVG": ("hub", "hub bus average"),
    "LZ_HOUSTON": ("zone", "Houston load zone"),
    "LZ_NORTH": ("zone", "North load zone"),
    "LZ_SOUTH": ("zone", "South load zone"),
    "LZ_WEST": ("zone", "West load zone"),
    "LZ_AEN": ("zone", "Austin Energy load zone"),
    "LZ_CPS": ("zone", "CPS Energy load zone"),
    "LZ_LCRA": ("zone", "LCRA load zone"),
    "LZ_RAYBN": ("zone", "Rayburn load zone"),
}
REPORTS = {   # market -> (yearly archive report, archive name prefix, daily report, daily name prefix, minutes)
    "da": ("13060", "DAMLZHBSPP_", "12331", "DAMSPNP4190", 60),
    "rt": ("13061", "RTMLZHBSPP_", "12301", "SPPHLZNP6905_", 15),
}
SKIP_TYPES = {"LZEW", "LZ_DC", "LZ_DCEW"}
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_ARCHIVE: dict[str, tuple[str, bytes, str | None, int | None]] = {}   # dataset market -> (DocID, body, type, ms)


def series_key(market: str, point: str) -> str:
    return f"ercot.{market}.lmp.{point.lower()}"


def _series(market: str) -> tuple[Series, ...]:
    label = {"da": "day-ahead hourly", "rt": "real-time 15-minute"}[market]
    return tuple(Series(key=series_key(market, p), name=f"ERCOT {name}, {label} settlement point price", kind="lmp",
                        market=market, location=p, location_type=typ, unit="usd_per_mwh", iso="ERCOT")
                 for p, (typ, name) in POINTS.items())


def earliest(now: datetime) -> date:
    return EARLIEST


def latest_da(now: datetime) -> date:
    t = now.astimezone(CT)
    return t.date() + timedelta(days=1) if t.hour >= DA_PUBLISHED_HOUR else t.date()


def latest_rt(now: datetime) -> date:
    return now.astimezone(CT).date() - timedelta(days=1)


# --------------------------------------------------------------------------- documents

def documents(body: bytes) -> list[dict]:
    try:
        d = json.loads(body)
        docs = d["ListDocsByRptTypeRes"].get("DocumentList") or []
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"ercot: not a document list: {body[:200]!r}") from exc
    if isinstance(docs, dict):
        docs = [docs]
    return [x["Document"] for x in docs if isinstance(x, dict) and "Document" in x]


def _published(doc: dict) -> datetime:
    return datetime.fromisoformat(doc["PublishDate"])


async def _list(get, report: str) -> tuple[list[dict] | None, Fetched]:
    f = await get.get(LIST_URL, params={"reportTypeId": report})
    if not f.ok:
        return None, f
    try:
        return documents(f.body), f
    except ValueError as exc:
        f.error = str(exc)
        return None, f


def archive_covers(name: str, published: datetime) -> tuple[date, date] | None:
    """The days a yearly archive holds: the whole year once the year is over, else through the month before the
    month it was published in."""
    m = re.search(r"_(\d{4})$", name)
    if not m:
        return None
    y = int(m.group(1))
    first, last = date(y, 1, 1), date(y, 12, 31)
    pub = published.astimezone(CT).date()
    if pub <= last:
        last = pub.replace(day=1) - timedelta(days=1)
    return (first, last) if last >= first else None


async def _archive(get, market: str, a: date, b: date, docs: list[dict]) -> list[Fetched]:
    prefix = REPORTS[market][1]
    want = f"{prefix}{a.year}"
    doc = next((x for x in sorted(docs, key=_published, reverse=True) if x.get("FriendlyName") == want), None)
    if doc is None:
        return []
    cover = archive_covers(want, _published(doc))
    lo, hi = (max(a, cover[0]), min(b, cover[1])) if cover else (a, a - timedelta(days=1))
    out = []
    if lo <= hi:
        cached = _ARCHIVE.get(market)
        if cached and cached[0] == doc["DocID"]:
            f = Fetched(f"{DOC_URL}?doclookupId={doc['DocID']}", 200, cached[3], cached[1], cached[2])
        else:
            f = await get.get(DOC_URL, params={"doclookupId": doc["DocID"]})
            if f.ok:
                _ARCHIVE[market] = (doc["DocID"], f.body, f.content_type, f.ms)   # one year file per dataset
        f.start, f.end = lo, hi
        out.append(f)
    return out


def daily_docs(market: str, d: date, docs: list[dict]) -> list[dict]:
    """The CSV documents holding day d: NP4-190 posted the day before (the newest, if re-posted); NP6-905 every
    interval ending on d after 00:00 plus d+1 00:00 (the day's last interval)."""
    prefix = REPORTS[market][3]
    csv_docs = [x for x in docs if x.get("FriendlyName", "").startswith(prefix)
                and x.get("FriendlyName", "").endswith("_csv")]
    if market == "da":
        posted = [x for x in csv_docs if _published(x).astimezone(CT).date() == d - timedelta(days=1)]
        return sorted(posted, key=_published)[-1:]
    nxt = d + timedelta(days=1)
    out = []
    for x in csv_docs:
        m = re.search(r"_(\d{8})_(\d{4})_csv$", x["FriendlyName"])
        if not m:
            continue
        day, hhmm = datetime.strptime(m.group(1), "%Y%m%d").date(), m.group(2)
        if (day == d and hhmm != "0000") or (day == nxt and hhmm == "0000"):
            out.append(x)
    return sorted(out, key=lambda x: (x["FriendlyName"], x["DocID"]))


def _make_fetch(market: str):
    archive_report, _, daily_report, _, _ = REPORTS[market]
    window = DA_DAILY_DAYS if market == "da" else RT_DAILY_DAYS

    async def fetch(get, start: date, end: date, now: datetime | None = None) -> list[Fetched]:
        today = (now or datetime.now(UTC)).astimezone(CT).date()
        recent_from = today - timedelta(days=window)
        out: list[Fetched] = []
        old = [d for d in days(start, end) if d < recent_from]
        new = [d for d in days(start, end) if d >= recent_from]
        for year in sorted({d.year for d in old}):
            ds = [d for d in old if d.year == year]
            docs, lf = await _list(get, archive_report)
            if docs is None:
                lf.start, lf.end = ds[0], ds[-1]
                out.append(lf)
                continue
            out += await _archive(get, market, ds[0], ds[-1], docs)
        if new:
            docs, lf = await _list(get, daily_report)
            if docs is None:
                lf.start, lf.end = new[0], new[-1]
                return out + [lf]
            for d in new:
                mine = daily_docs(market, d, docs)
                if len(mine) < (1 if market == "da" else slots(d, TZ, 15)):
                    continue   # not all posted (or one never will be): left to the next run, then the archive
                for doc in mine:
                    f = await get.get(DOC_URL, params={"doclookupId": doc["DocID"]})
                    f.start = f.end = d
                    out.append(f)
        return out
    return fetch


# --------------------------------------------------------------------------- parsing

def _cell(c, strings: list[str]) -> str:
    t = c.get("t")
    if t == "inlineStr":
        return "".join(x.text or "" for x in c.iter(NS + "t"))
    v = c.find(NS + "v")
    if v is None or v.text is None:
        return ""
    return strings[int(v.text)] if t == "s" else v.text


def _col(ref: str) -> int:
    n = 0
    for ch in ref:
        if ch.isalpha():
            n = n * 26 + ord(ch.upper()) - 64
        else:
            break
    return n - 1


def _xlsx_rows(data: bytes):
    """Every row of every sheet of an xlsx, as lists of strings (streamed with iterparse; header rows included)."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        strings: list[str] = []
        if "xl/sharedStrings.xml" in names:
            for _, el in ET.iterparse(z.open("xl/sharedStrings.xml")):
                if el.tag == NS + "si":
                    strings.append("".join(x.text or "" for x in el.iter(NS + "t")))
                    el.clear()
        sheets = sorted((n for n in names if re.fullmatch(r"xl/worksheets/[Ss]heet\d+\.xml", n)),
                        key=lambda n: int(re.search(r"(\d+)\.xml$", n).group(1)))
        for n in sheets:
            for _, el in ET.iterparse(z.open(n)):
                if el.tag == NS + "row":
                    row: list[str] = []
                    for c in el.iter(NS + "c"):
                        i = _col(c.get("r", "")) if c.get("r") else len(row)
                        row += [""] * (i - len(row))
                        row.append(_cell(c, strings))
                    yield row
                    el.clear()


def _csv_rows(data: bytes):
    for line in io.BytesIO(data):
        yield [x.strip() for x in line.decode("utf-8-sig", "replace").rstrip("\r\n").split(",")]


def _rows(body: bytes):
    """Rows of a document: a zip holding a CSV or an xlsx (rows of several files run on, each with its header)."""
    try:
        z = zipfile.ZipFile(io.BytesIO(body))
    except zipfile.BadZipFile as exc:
        raise ValueError(f"ercot: not a zip: {body[:200]!r}") from exc
    with z:
        for n in z.namelist():
            low = n.lower()
            if low.endswith(".xlsx"):
                yield from _xlsx_rows(z.read(n))
            elif low.endswith(".csv"):
                yield from _csv_rows(z.read(n))
            elif low.endswith(".zip"):
                yield from _rows(z.read(n))
            else:
                raise ValueError(f"ercot: unexpected document member {n}")


def _norm(h: str) -> str:
    return re.sub(r"[^a-z]", "", h.lower())


def _day(s: str) -> date:
    s = s.strip()
    if re.fullmatch(r"\d+(\.0+)?", s):   # an Excel serial date
        return date(1899, 12, 30) + timedelta(days=int(float(s)))
    m, d, y = s.split("/")
    return date(int(y), int(m), int(d))


def interval_start(day: date, hour_ending: int, interval: int, minutes: int, repeated: bool) -> datetime:
    """HE h (1..24), interval i (1..4 for 15 minutes, 1 for hourly) -> UTC start; repeated = the second 01:00."""
    local = datetime.combine(day, time(0)) + timedelta(hours=hour_ending - 1, minutes=(interval - 1) * minutes)
    return local.replace(tzinfo=CT, fold=1 if repeated else 0).astimezone(UTC)


def _make_parse(market: str):
    minutes = REPORTS[market][4]

    def parse(f: Fetched) -> dict[str, dict[datetime, float | None]]:
        out: dict[str, dict[datetime, float | None]] = {}
        cols: dict[str, int] | None = None
        seen_header = False
        for row in _rows(f.body):
            if row and _norm(row[0]) == "deliverydate":
                h = [_norm(x) for x in row]
                cols = {
                    "date": h.index("deliverydate"),
                    "point": next(i for i, x in enumerate(h) if x in ("settlementpoint", "settlementpointname")),
                    "price": h.index("settlementpointprice"),
                    "hour": next((i for i, x in enumerate(h) if x in ("hourending", "deliveryhour")), -1),
                    "interval": h.index("deliveryinterval") if "deliveryinterval" in h else -1,
                    "flag": next((i for i, x in enumerate(h) if x in ("dstflag", "repeatedhourflag")), -1),
                    "type": h.index("settlementpointtype") if "settlementpointtype" in h else -1,
                }
                if cols["hour"] < 0:
                    raise ValueError(f"ercot: no hour column in {row}")
                seen_header = True
                continue
            if cols is None or not row or len(row) <= cols["price"]:
                continue
            p = row[cols["point"]].strip()
            if p not in POINTS or (cols["type"] >= 0 and row[cols["type"]].strip() in SKIP_TYPES):
                continue
            try:
                he = row[cols["hour"]].strip()
                hour = int(he.split(":")[0])
                iv = int(row[cols["interval"]]) if cols["interval"] >= 0 else 1
                rep = cols["flag"] >= 0 and row[cols["flag"]].strip().upper() == "Y"
                t = interval_start(_day(row[cols["date"]]), hour, iv, minutes, rep)
                v = row[cols["price"]].strip()
                out.setdefault(series_key(market, p), {})[t] = float(v) if v else None
            except (ValueError, IndexError) as exc:
                raise ValueError(f"ercot: bad row {row[:8]}: {exc}") from exc
        if not seen_header:
            raise ValueError("ercot: no header row (DeliveryDate ...) in the document")
        if not out:
            f.empty = True
        return out
    return parse


DATASETS = (
    Dataset(key="ercot.da_spp", source=SOURCE, series=_series("da"), interval_minutes=60, tz=TZ, chunk_days=31,
            earliest=earliest, latest=latest_da, fetch=_make_fetch("da"), parse=_make_parse("da"),
            pace_seconds=2.5, overlap_days=1, keep_raw=False, align="month",
            description="DAM settlement point prices, hourly; 6 hubs + 8 load zones; yearly archive ~2 MB, "
                        "NP4-190 day ~188 KB (url + sha256 only)"),
    Dataset(key="ercot.rt_spp", source=SOURCE, series=_series("rt"), interval_minutes=15, tz=TZ, chunk_days=31,
            earliest=earliest, latest=latest_rt, fetch=_make_fetch("rt"), parse=_make_parse("rt"),
            pace_seconds=2.5, overlap_days=1, keep_raw=False, align="month",
            description="RTM settlement point prices, 15-minute; 6 hubs + 8 load zones; yearly archive ~14 MB, "
                        "NP6-905 interval ~8 KB, 96 a day (url + sha256 only)"),
)
