"""EIA-860M generator inventory (PUBLIC: U.S.-government public domain).

Two publications of the same survey, read for different things:

    API v2  GET https://api.eia.gov/v2/electricity/operating-generator-capacity/data/?frequency=monthly
            &start=YYYY-MM&end=YYYY-MM&data[]=...&sort=plantid,generatorid&offset=N&length=5000
            Operable generators only (status OP, SB, OA, OS), one month per query, 2008-01 onward, with plant
            and entity, state, county, latitude / longitude, balancing authority, sector, technology, energy
            source, prime mover, nameplate / summer / winter MW, operating month and planned retirement month.
            Measured 2026-11-15: ~28k rows a month (6 pages), endPeriod 2026-08. The monthly history is what
            gives the change-only record of retirements and capacity changes since 2008.
    file    https://www.eia.gov/electricity/data/eia860m/  -> <month>_generator<year>.xlsx (~14 MB)
            Sheets Operating, Planned, Retired, Canceled or Postponed (and the same for Puerto Rico), "as of"
            one month, usually a month ahead of the API. Planned generators (with their planned operation month
            and construction status) and the exact retirement month exist only here. Only the newest file is
            read: the Retired sheet lists every retirement EIA tracks, so older files add little.

The index page carries next months' rows inside an HTML comment (ignored); a listed name that is not posted yet
answers a 301 to an HTML page (measured for october_generator2026.xlsx on 2026-11-15), read as "not published". Times are months; a generator's state is keyed by the inventory month ("period").
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime

from power.registry import Fetched
from power.sites import xlsx

SOURCE = "eia860m"
DS_API, DS_FILE = "sites.eia860m_api", "sites.eia860m_file"
ROUTE = "electricity/operating-generator-capacity/"
EARLIEST = date(2008, 1, 1)
INDEX_URL = "https://www.eia.gov/electricity/data/eia860m/"
HOST = "https://www.eia.gov"
FIELDS = ("nameplate-capacity-mw", "net-summer-capacity-mw", "net-winter-capacity-mw", "operating-year-month",
          "planned-retirement-year-month", "county", "latitude", "longitude")
MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december")
GROUP = {"OP": "operating", "SB": "standby", "OA": "out_of_service", "OS": "out_of_service", "RE": "retired",
         "CN": "canceled", "IP": "canceled"}
PLANNED = {"P", "L", "T", "U", "V", "TS", "OT"}
SHEETS = {   # sheet name -> status when the row has none
    "Operating": None, "Planned": None, "Retired": "RE", "Canceled or Postponed": "CN",
    "Operating_PR": None, "Planned_PR": None, "Retired_PR": "RE", "Canceled or Postponed_PR": "CN",
}


def group(status: str) -> str:
    return GROUP.get(status) or ("planned" if status in PLANNED else "other")


def ym(v) -> str | None:
    s = str(v or "").strip()
    return s if re.fullmatch(r"\d{4}-\d{2}", s) else None


def _ym(year, month) -> str | None:
    y, m = xlsx.num(year), xlsx.num(month)
    if not y or not m or not (1 <= m <= 12) or y < 1890:
        return None
    return f"{int(y):04d}-{int(m):02d}"


def _r(v, nd: int):
    x = xlsx.num(v)
    return None if x is None else round(x, nd)


def _txt(v, n: int) -> str | None:
    s = re.sub(r"\s+", " ", str(v or "")).strip()
    return s[:n] or None


def _gid(v) -> str:
    s = str(v or "").strip()
    return s[:-2] if re.fullmatch(r"\d+\.0", s) else s


# --------------------------------------------------------------------------- API

def month_params(period: date) -> list[tuple[str, str]]:
    p = f"{period:%Y-%m}"
    return [("frequency", "monthly"), ("start", p), ("end", p), *[("data[]", f) for f in FIELDS]]


async def latest_api_month(get) -> tuple[date | None, Fetched]:
    from power.sources import eia

    f = await get.get(eia.BASE + ROUTE, params=[("api_key", eia.api_key())])
    try:
        end = json.loads(f.body)["response"]["endPeriod"] if f.ok else None
        return (date(int(end[:4]), int(end[5:7]), 1) if end else None), f
    except (ValueError, KeyError, TypeError):
        f.error = f.error or "eia860m: no endPeriod in the route metadata"
        return None, f


async def fetch_month(get, period: date) -> list[Fetched]:
    from power.sources import eia

    return await eia.paged(get, ROUTE + "data/", month_params(period), ("plantid", "generatorid"))


def parse_api(f: Fetched) -> list[dict]:
    from power.sources import eia

    out = []
    for r in eia.rows(f):
        try:
            pid = int(r["plantid"])
        except (KeyError, TypeError, ValueError):
            continue
        st = (r.get("status") or "").strip().upper()
        out.append({"plant_id": pid, "generator_id": _gid(r.get("generatorid"))[:16],
                    "plant_name": _txt(r.get("plantName"), 200),
                    "entity_id": int(r["entityid"]) if str(r.get("entityid") or "").isdigit() else None,
                    "entity_name": _txt(r.get("entityName"), 200), "state": _txt(r.get("stateid"), 2),
                    "county": _txt(r.get("county"), 64), "latitude": _r(r.get("latitude"), 4),
                    "longitude": _r(r.get("longitude"), 4), "ba_code": _txt(r.get("balancing_authority_code"), 16),
                    "sector": _txt(r.get("sectorName"), 64), "technology": _txt(r.get("technology"), 80),
                    "energy_source": _txt(r.get("energy_source_code"), 8),
                    "prime_mover": _txt(r.get("prime_mover_code"), 8),
                    "nameplate_mw": _r(r.get("nameplate-capacity-mw"), 3),
                    "summer_mw": _r(r.get("net-summer-capacity-mw"), 3),
                    "winter_mw": _r(r.get("net-winter-capacity-mw"), 3),
                    "status": st, "status_group": group(st), "operating_ym": ym(r.get("operating-year-month")),
                    "planned_operation_ym": None,
                    "planned_retirement_ym": ym(r.get("planned-retirement-year-month")), "retirement_ym": None})
    return out


# --------------------------------------------------------------------------- file

_LINK = re.compile(r'href="([^"]*/(' + "|".join(MONTHS) + r')_generator(\d{4})\.xlsx)"', re.I)


def file_links(html: bytes) -> list[tuple[date, str]]:
    """[(file month, absolute URL)], newest first, one per month (the current folder before the archive)."""
    out: dict[date, str] = {}
    # the page keeps next months' rows inside an HTML comment until each file is posted
    page = re.sub(r"<!--.*?-->", "", html.decode("utf-8", "replace"), flags=re.S)
    for m in _LINK.finditer(page):
        d = date(int(m.group(3)), MONTHS.index(m.group(2).lower()) + 1, 1)
        url = m.group(1) if m.group(1).startswith("http") else HOST + m.group(1)
        if d not in out or "/archive/" in out[d]:
            out[d] = url
    return sorted(out.items(), reverse=True)


def file_name(url: str) -> str:
    return url.rsplit("/", 1)[-1].lower()


_AS_OF = re.compile(r"as of\s+(" + "|".join(MONTHS) + r")\s+(\d{4})", re.I)


def as_of(data: bytes) -> date | None:
    """The month in the Operating sheet's title ("Inventory of Operating Generators as of September 2026")."""
    for i, r in enumerate(xlsx.rows(data, "Operating")):
        m = _AS_OF.search(" ".join(r))
        if m:
            return date(int(m.group(2)), MONTHS.index(m.group(1).lower()) + 1, 1)
        if i > 4:
            return None
    return None


def parse_file(data: bytes) -> list[dict]:
    out = []
    names = set(xlsx.sheets(data))
    if "Operating" not in names:
        raise ValueError("eia860m file: no Operating sheet")
    for sheet, default in SHEETS.items():
        if sheet not in names:
            continue
        _, rows = xlsx.table(data, sheet, ("Plant ID", "Generator ID"))
        for r in rows:
            try:
                pid = int(float(r.get("plant_id") or ""))
            except ValueError:
                continue                       # footnote rows at the end of each sheet
            raw = r.get("status", "")
            m = re.match(r"\(\s*([A-Z]{1,3})\s*\)", raw)
            st = (m.group(1) if m else (default or raw.strip().upper()[:8])) or "OT"
            out.append({
                "plant_id": pid, "generator_id": _gid(r.get("generator_id"))[:16],
                "plant_name": _txt(r.get("plant_name"), 200),
                "entity_id": int(float(r["entity_id"])) if xlsx.num(r.get("entity_id")) is not None else None,
                "entity_name": _txt(r.get("entity_name"), 200), "state": _txt(r.get("plant_state"), 2),
                "county": _txt(r.get("county"), 64), "latitude": _r(r.get("latitude"), 4),
                "longitude": _r(r.get("longitude"), 4), "ba_code": _txt(r.get("balancing_authority_code"), 16),
                "sector": _txt(r.get("sector"), 64), "technology": _txt(r.get("technology"), 80),
                "energy_source": _txt(r.get("energy_source_code"), 8),
                "prime_mover": _txt(r.get("prime_mover_code"), 8),
                "nameplate_mw": _r(r.get("nameplate_capacity_mw"), 3),
                "summer_mw": _r(r.get("net_summer_capacity_mw"), 3),
                "winter_mw": _r(r.get("net_winter_capacity_mw"), 3),
                "status": st, "status_group": group(st),
                "operating_ym": _ym(r.get("operating_year"), r.get("operating_month")),
                "planned_operation_ym": _ym(r.get("planned_operation_year"), r.get("planned_operation_month")),
                "planned_retirement_ym": _ym(r.get("planned_retirement_year"), r.get("planned_retirement_month")),
                "retirement_ym": _ym(r.get("retirement_year"), r.get("retirement_month")),
            })
    return out


# --------------------------------------------------------------------------- sync

def _month_add(d: date, n: int) -> date:
    m = d.year * 12 + d.month - 1 + n
    return date(m // 12, m % 12 + 1, 1)


def months(a: date, b: date) -> list[date]:
    out, d = [], date(a.year, a.month, 1)
    while d <= b:
        out.append(d)
        d = _month_add(d, 1)
    return out


async def sync_api(get, since: date | None = None, dry_run: bool = False) -> dict:
    """Every API month after the newest one stored (from `since` or 2008-01 when nothing is), oldest first: the
    history is change-only, so months must be applied in order."""
    import asyncio

    from power.sites import store

    out = {"dataset": DS_API, "requests": 0, "months": 0, "failed": 0, "new": 0, "changed": 0, "left": 0}
    end, meta = await latest_api_month(get)
    out["requests"] += 1
    if end is None:
        await asyncio.to_thread(store.record, SOURCE, DS_API, meta)
        out["failed"] += 1
        out["error"] = meta.error or f"HTTP {meta.status}"
        return out
    done = await asyncio.to_thread(store.applied_periods, DS_API)
    first = _month_add(max(done), 1) if done else max(since or EARLIEST, EARLIEST)
    if since and done:
        first = max(first, since)
    todo = months(first, end)
    out.update(first=first, latest=end, months_to_fetch=len(todo))
    if dry_run:
        return out
    for p in todo:
        answers = await fetch_month(get, p)
        out["requests"] += len(answers)
        rows, bad = [], None
        for f in answers:
            if not f.ok:
                bad = f.error or f"HTTP {f.status}"
                continue
            try:
                rows += parse_api(f)
            except ValueError as exc:
                bad = f.error = f"parse: {exc}"[:500]
        ids = [await asyncio.to_thread(store.record, SOURCE, DS_API, f, p, p) for f in answers]
        if bad or not rows:
            # a month is applied whole or not at all: a missing page would read as generators that left
            out["failed"] += 1
            out["error"] = bad or "no rows"
            break
        res = await asyncio.to_thread(store.apply_generators, rows, p, "api", "operable", ids[-1])
        for i in ids:
            await asyncio.to_thread(store.mark_applied, i)
        out["months"] += 1
        for k in ("new", "changed", "left"):
            out[k] += res[k]
    return out


async def sync_file(get, dry_run: bool = False) -> dict:
    """The newest published EIA-860M file, unless that file was already applied."""
    import asyncio

    from power.sites import store

    out = {"dataset": DS_FILE, "requests": 0, "applied": None}
    idx = await get.get(INDEX_URL)
    out["requests"] += 1
    if not idx.ok:
        await asyncio.to_thread(store.record, SOURCE, DS_FILE, idx)
        out["error"] = idx.error or f"HTTP {idx.status}"
        return out
    seen = {file_name(u) for u in await asyncio.to_thread(store.applied, DS_FILE)}
    links = file_links(idx.body)
    newest_done = max((d for d, u in links if file_name(u) in seen), default=None)
    todo = [(d, u) for d, u in links if newest_done is None or d > newest_done]
    out["candidates"] = [u for _, u in todo]
    if dry_run or not todo:
        return out
    for d, url in todo:
        f = await get.get(url)
        out["requests"] += 1
        if f.ok and f.body[:4] != b"PK\x03\x04":
            f.empty = True                  # an HTML page (the 301 target): not published yet
        if (f.status or 0) in (301, 302, 404) or f.empty:
            f.empty, f.error = True, None
            await asyncio.to_thread(store.record, SOURCE, DS_FILE, f, d, d)
            continue
        if not f.ok:
            await asyncio.to_thread(store.record, SOURCE, DS_FILE, f, d, d)
            out["error"] = f.error or f"HTTP {f.status}"
            return out
        try:
            period = await asyncio.to_thread(as_of, f.body) or d
            rows = await asyncio.to_thread(parse_file, f.body)
        except Exception as exc:  # noqa: BLE001 - a reshaped workbook is one failed fetch, recorded
            f.error = f"parse: {type(exc).__name__}: {exc}"[:500]
            await asyncio.to_thread(store.record, SOURCE, DS_FILE, f, d, d)
            out["error"] = f.error
            return out
        fid = await asyncio.to_thread(store.record, SOURCE, DS_FILE, f, period, period)
        res = await asyncio.to_thread(store.apply_generators, rows, period, "file", "all", fid)
        await asyncio.to_thread(store.mark_applied, fid)
        out.update(applied=url, period=period, **{k: res[k] for k in ("rows", "new", "changed", "updated", "left",
                                                                      "stale")})
        return out
    return out
