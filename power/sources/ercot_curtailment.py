"""ERCOT wind and solar curtailment PROXY (methodology/power-waste.md). ERCOT publishes no curtailment figure. Its
system-wide wind and solar production reports carry, per hour, the actual output and the system-wide HSL (High
Sustained Limit: what the plants report they could produce right now; ERCOT calls the forecast target "HSL, which is
uncurtailed power generation potential"). The proxy is max(0, HSL - output): energy the fleet could have produced and
did not. No key, no signup (public MIS documents, as power/sources/ercot.py).

    13028  NP4-732-CD Wind Power Production - Hourly Averaged Actual and Forecasted Values   (~10 KB zip, hourly)
    13483  NP4-737-CD Solar Power Production - Hourly Averaged Actual and Forecasted Values  (~2.5 KB zip, hourly)

Each document holds the last 48 hours of actuals (SYSTEM_WIDE_GEN, SYSTEM_WIDE_HSL; MW averaged over the hour, so
MWh) and 168 forecast hours (blank actuals, skipped). DELIVERY_DATE, HOUR_ENDING 01..24 Central prevailing, DSTFlag
"Y" on the repeated hour. Regional columns exist, but their HSL is the COP plan, not the actual HSL, so the proxy is
system-wide only. ERCOT keeps the documents about 7 days (measured 2026-11-15: the list starts at midnight seven
days back) and the archive behind api.ercot.com needs an account, so there is NO history: the series starts on the
day the forward job first runs, and a missed week is lost.

What the proxy measures, and what it does not:
    counts    ERCOT's economic dispatch below HSL (base point below HSL), manual and congestion curtailment
    adds      units lagging their dispatch, regulation, telemetry error (noise counted as curtailment); solar HSL
              sits near 23 MW at night against ~0.3 MW output, so solar hours with output below SOLAR_FLOOR_MW count
              as 0
    misses    a plant that lowers its own HSL to self-curtail; outages and derates (correctly not counted)
It is an estimate, labelled measure "proxy_hsl_minus_output" on every series, never called curtailment alone.
A day is read from the latest document posted on the next day (it covers the whole day); the forward job re-reads
the last 2 days. Each document is downloaded once per run (the API terms' three-downloads-a-year rule concerns the
reports' bulk API, not these MIS documents; ercot.py reads its documents the same way).
"""

from __future__ import annotations

import io
import zipfile
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from power.registry import PUBLIC, Dataset, Fetched, Series, Terms
from power.sources import ercot
from power.storage import days

SOURCE = "ercot_curtailment"
DEFAULT_ON = True
REQUIRES: tuple[str, ...] = ()
TZ = "America/Chicago"
CT = ZoneInfo(TZ)
UTC = timezone.utc
RETENTION_DAYS = 7
SOLAR_FLOOR_MW = 50.0
REPORTS = {"wind": ("13028", "WPPHRLYAVGACTNP4732_csv"), "solar": ("13483", "PVGRHRLYAVGACTNP4737_csv")}
MEASURE = "proxy_hsl_minus_output"

TERMS = Terms(
    operator="Electric Reliability Council of Texas",
    visibility=PUBLIC,
    reason=(ercot.TERMS.reason + " The proxy is OpenGrid's arithmetic on that raw data (HSL minus output), "
            "labelled as an estimate."),
    terms_url=ercot.TERMS.terms_url,
    checked="2026-11-15",
    signup=None,
    attribution="Source: ERCOT (NP4-732-CD, NP4-737-CD); curtailment proxy computed by OpenGrid",
)


def key(kind: str, fuel: str) -> str:
    return f"{SOURCE}.{'rt' if kind == 'curtailment' else 'actual'}.{kind}.{fuel}"


def _series() -> tuple[Series, ...]:
    out = []
    for f in REPORTS:
        out.append(Series(key("curtailment", f), f"ERCOT {f} curtailment PROXY, max(0, HSL - output), system-wide "
                          f"(MWh, hourly; an estimate)", "curtailment", "rt", f, "ba", "mwh", "ERCOT",
                          {"fuel": f, "scope": None, "type": None, "measure": MEASURE}))
        out.append(Series(key("generation", f), f"ERCOT {f} output, system-wide (MWh, hourly)", "generation",
                          "actual", f, "ba", "mwh", "ERCOT", {"fuel": f, "basis": "actual"}))
        out.append(Series(key("hsl", f), f"ERCOT {f} high sustained limit, system-wide (MW averaged over the hour)",
                          "hsl", "actual", f, "ba", "mwh", "ERCOT", {"fuel": f}))
    return tuple(out)


SERIES = _series()


def earliest(now: datetime) -> date:
    return now.astimezone(CT).date() - timedelta(days=RETENTION_DAYS)


def latest(now: datetime) -> date:
    t = now.astimezone(CT)
    return t.date() - timedelta(days=1 if t.hour >= 2 else 2)


def pick(docs: list[dict], name: str, d: date) -> dict | None:
    """The latest CSV document posted on d + 1 (Central): its 48 hours of actuals cover all of day d."""
    nxt = d + timedelta(days=1)
    mine = [x for x in docs if x.get("FriendlyName") == name
            and ercot._published(x).astimezone(CT).date() == nxt]
    return max(mine, key=ercot._published) if mine else None


async def fetch(get, start: date, end: date) -> list[Fetched]:
    out: list[Fetched] = []
    for fuel, (report, name) in REPORTS.items():
        docs, lf = await ercot._list(get, report)
        if docs is None:
            lf.start, lf.end = start, end
            out.append(lf)
            continue
        for d in days(start, end):
            doc = pick(docs, name, d)
            if doc is None:
                continue          # not posted yet: no answer, so the day is asked again next run
            f = await get.get(ercot.DOC_URL, params={"doclookupId": doc["DocID"]})
            f.start = f.end = d
            out.append(f)
    return out


def _csv(body: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(body)) as z:
        names = [n for n in z.namelist() if n.lower().endswith(".csv")]
        if len(names) != 1:
            raise ValueError(f"ercot_curtailment: expected one CSV, got {z.namelist()}")
        return z.read(names[0])


def parse(f: Fetched) -> dict[str, dict[datetime, float | None]]:
    lines = _csv(f.body).decode("utf-8-sig", "replace").splitlines()
    head = [h.strip().upper() for h in lines[0].split(",")]
    need = ("DELIVERY_DATE", "HOUR_ENDING", "SYSTEM_WIDE_GEN", "SYSTEM_WIDE_HSL", "DSTFLAG")
    if any(n not in head for n in need):
        raise ValueError(f"ercot_curtailment: columns {head} lack {need}")
    i = {n: head.index(n) for n in need}
    fuel = "solar" if "STPPF_SYSTEM_WIDE" in head or "PVGRPP_SYSTEM_WIDE" in head else "wind"
    lo = datetime.combine(f.start, datetime.min.time()).replace(tzinfo=CT) if f.start else None
    hi = datetime.combine(f.end + timedelta(days=1), datetime.min.time()).replace(tzinfo=CT) if f.end else None
    out: dict[str, dict[datetime, float | None]] = {key(k, fuel): {} for k in ("curtailment", "generation", "hsl")}
    for line in lines[1:]:
        row = [x.strip() for x in line.split(",")]
        if len(row) < len(head):
            continue
        gen, hsl = row[i["SYSTEM_WIDE_GEN"]], row[i["SYSTEM_WIDE_HSL"]]
        if not gen or not hsl:
            continue                      # a forecast hour
        t = ercot.interval_start(ercot._day(row[i["DELIVERY_DATE"]]), int(row[i["HOUR_ENDING"]].split(":")[0]), 1,
                                 60, row[i["DSTFLAG"]].upper() == "Y")
        if lo and not (lo <= t < hi):
            continue
        g, h = float(gen), float(hsl)
        proxy = 0.0 if fuel == "solar" and g < SOLAR_FLOOR_MW else max(0.0, h - g)
        out[key("generation", fuel)][t] = g
        out[key("hsl", fuel)][t] = h
        out[key("curtailment", fuel)][t] = round(proxy, 4)
    out = {k: v for k, v in out.items() if v}
    if not out:
        f.empty = True
    return out


DATASETS = (
    Dataset(key="ercot_curtailment.hourly", source=SOURCE, series=SERIES, interval_minutes=60, tz=TZ, chunk_days=8,
            earliest=earliest, latest=latest, fetch=fetch, parse=parse, pace_seconds=2.5, overlap_days=2,
            keep_raw=True,
            description="Curtailment PROXY max(0, HSL - output), wind + solar system-wide, hourly; NP4-732 / "
                        "NP4-737 documents (~2.5-10 KB), kept ~7 days by ERCOT: forward only, no history"),
)
