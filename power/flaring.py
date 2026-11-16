"""Gas flared at oil and gas sites (methodology/power-waste.md): where gas is burned for lack of a pipeline, a
generator could burn it for power instead. Sites with coordinates and their flared volume per year, and regulator
volumes per county and month, in power_flare_sites / power_flare_volumes / power_flare_areas (0046_power_waste).

    run(sources=None, refresh=False, dry_run=False, client=None, now=None)   fetch, record raw first, upsert
    status()                                                                    rows and depth per source

Datasets (verdicts in SOURCES and power/registry.DECLINED):

    worldbank_ggfd   World Bank Global Gas Flaring Database v5 (data catalog 0037743), CC BY 4.0: one workbook per
                     year 2017-2021, every detected flare with latitude, longitude, country, type and BCM flared
                     (VIIRS Nightfire, Colorado School of Mines, for the World Bank's GGFR). Five fixed files, ~1 MB
                     each; read once (a stored file is not fetched again unless refresh)
    ndic             North Dakota Industrial Commission, Oil and Gas Division, Monthly Production Report: one workbook
                     per month from 2015-05 (~2-3.5 MB), every producing well with county, PLSS location, latitude,
                     longitude, gas produced and gas flared (MCF). Posted ~6 weeks after the month; the forward run
                     asks for the next month and re-reads the last REVISE_MONTHS stored

Sites. A satellite flare is a detection whose position moves a little from year to year and whose id changes
between releases, so a World Bank site is a 0.01 degree cell (about 1.1 km north-south): detections falling in one
cell in a year are summed, and the cell keeps the volume-weighted position of its latest year. A North Dakota site
is the PLSS section (one square mile: township, range, section) holding the flaring wells; its position is the mean
of those wells' coordinates, its year row holds the twelve months (monthly_mcf) and their sum.

Energy (DERIVED, never observed): electricity a gas generator could make from the flared gas, at
    HHV_BTU_PER_SCF = 1,037 Btu per standard cubic foot (EIA's average heat content of U.S. natural gas delivered to
                      consumers; flared associated gas is usually richer, 1,200-1,500, so this is conservative), and
    HEAT_RATE_BTU_PER_KWH = 10,000 Btu per kWh (a reciprocating gas genset or small simple-cycle turbine, ~34% HHV)
so 1 MCF -> 0.1037 MWh and 1 BCM (35.3147 million MCF) -> 3.66 TWh, about 418 MW all year.
"""

from __future__ import annotations

import asyncio
import io
import logging
import math
import re
import zipfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import httpx

from power import storage
from power.http import TIMEOUT, Client
from power.registry import INTERNAL, PUBLIC, Fetched, Terms
from power.sources import ercot

log = logging.getLogger("power.flaring")
UTC = timezone.utc
M3_PER_MCF = 28.316846592
HHV_BTU_PER_SCF = 1037.0
HEAT_RATE_BTU_PER_KWH = 10000.0
MWH_PER_MCF = 1000 * HHV_BTU_PER_SCF / HEAT_RATE_BTU_PER_KWH / 1000      # 0.1037
CELL = 100                     # 1 / 0.01 degree
REVISE_MONTHS = 2
PACE_SECONDS = 5.0
ENERGY_RULE = (f"DERIVED: MWh = MCF x {MWH_PER_MCF:.4f} (heat content {HHV_BTU_PER_SCF:.0f} Btu/scf, generator heat "
               f"rate {HEAT_RATE_BTU_PER_KWH:.0f} Btu/kWh); electricity a gas generator could make, not observed")


def mwh(mcf: float | None) -> float | None:
    return None if mcf is None else round(mcf * MWH_PER_MCF, 3)


@dataclass(frozen=True)
class FlareSource:
    source: str
    terms: Terms
    description: str


SOURCES = {
    "worldbank_ggfd": FlareSource("worldbank_ggfd", Terms(
        operator="World Bank (Global Gas Flaring Reduction Partnership); data by the Earth Observation Group, "
                 "Colorado School of Mines",
        visibility=PUBLIC,
        reason=("World Bank Data Catalog dataset 0037743 'Global Gas Flaring Database': 'License : Creative Commons "
                "Attribution 4.0. This dataset is licensed under Creative Commons Attribution 4.0' (catalog page, "
                "dateModified 2023-01-18). CC BY 4.0 allows commercial use and redistribution with attribution. "
                "Read only from the catalog's files; the newer 2012-2025 files are NOT these (see DECLINED)."),
        terms_url="https://datacatalog.worldbank.org/search/dataset/0037743",
        checked="2026-11-15",
        attribution="Source: World Bank Global Gas Flaring Database (CC BY 4.0); VIIRS Nightfire, Earth Observation "
                    "Group, Payne Institute, Colorado School of Mines",
    ), "per-flare annual volume with coordinates, 2017-2021"),
    "ndic": FlareSource("ndic", Terms(
        operator="North Dakota Industrial Commission, Department of Mineral Resources, Oil and Gas Division",
        visibility=INTERNAL,
        reason=("Public monthly production reports, no account. The site states no reuse licence, only a "
                "disclaimer ('Any person or entity that relies on any information obtained from this system does so "
                "at his or her own risk'); no clause forbids automated access or commercial use. Redistribution is "
                "not clearly granted, so internal."),
        terms_url="https://www.dmr.nd.gov/oilgas/",
        checked="2026-11-15",
        attribution="Source: North Dakota Industrial Commission, Oil and Gas Division, Monthly Production Report",
    ), "per-well monthly gas produced and flared, 2015-05 on"),
}

WB_BASE = "https://datacatalogfiles.worldbank.org/ddh-published/0037743/5/"
WB_FILES = {
    2017: WB_BASE + "DR0045623/viirs_global_flaring_d.7_slope_0.029353_2017_web_v1.xlsx",
    2018: WB_BASE + "DR0045622/viirs_global_flaring_d.7_slope_0.029353_2018_web.xlsx",
    2019: WB_BASE + "DR0045621/viirs_global_flaring_d.7_slope_0.029353_2019_web_v20201114-3.xlsx",
    2020: WB_BASE + "DR0084248/2020%20Global%20Gas%20Flaring%20Volumes.xlsx",
    2021: WB_BASE + "DR0087112/2021%20Global%20Gas%20Flaring%20Volumes.xlsx",
}
ND_URL = "https://www.dmr.nd.gov/oilgas/mpr/{y:04d}_{m:02d}.xlsx"
ND_FIRST = date(2015, 5, 1)
ND_COUNTIES = {
    "ADM": "Adams", "BIL": "Billings", "BOT": "Bottineau", "BOW": "Bowman", "BRK": "Burke", "DIV": "Divide",
    "DUN": "Dunn", "GV": "Golden Valley", "HET": "Hettinger", "MCH": "McHenry", "MCK": "McKenzie", "MCL": "McLean",
    "MER": "Mercer", "MTL": "Mountrail", "OLI": "Oliver", "REN": "Renville", "SLP": "Slope", "STK": "Stark",
    "WIL": "Williams", "WRD": "Ward", "BRL": "Burleigh", "EMM": "Emmons", "GRT": "Grant", "MOR": "Morton",
}


@dataclass(frozen=True)
class _Ds:
    """What storage.record_fetch needs of a dataset."""
    source: str
    key: str


WB_DS = _Ds("worldbank_ggfd", "worldbank_ggfd.annual")
ND_DS = _Ds("ndic", "ndic.monthly")


# --------------------------------------------------------------------------- workbooks

_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
_PKG = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def sheets(body: bytes):
    """[(sheet name, rows)] of an xlsx, rows as lists of strings, in workbook order (streamed per sheet)."""
    z = zipfile.ZipFile(io.BytesIO(body))
    names = z.namelist()
    strings: list[str] = []
    if "xl/sharedStrings.xml" in names:
        for _, el in ET.iterparse(z.open("xl/sharedStrings.xml")):
            if el.tag == ercot.NS + "si":
                strings.append("".join(x.text or "" for x in el.iter(ercot.NS + "t")))
                el.clear()
    rels = {r.get("Id"): r.get("Target") for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
            if r.tag == _PKG + "Relationship"}
    out = []
    for sh in ET.fromstring(z.read("xl/workbook.xml")).iter(ercot.NS + "sheet"):
        target = rels.get(sh.get(_REL), "")
        path = target.lstrip("/") if target.startswith("/") else "xl/" + target
        if path not in names:
            continue

        def rows(path=path):
            for _, el in ET.iterparse(z.open(path)):
                if el.tag == ercot.NS + "row":
                    row: list[str] = []
                    for c in el.iter(ercot.NS + "c"):
                        i = ercot._col(c.get("r", "")) if c.get("r") else len(row)
                        row += [""] * (i - len(row))
                        row.append(ercot._cell(c, strings))
                    yield row
                    el.clear()
        out.append((sh.get("name", ""), rows))
    return out


def _f(s: str) -> float | None:
    s = (s or "").strip().replace(",", "")
    if s in ("", "NULL", "NR", "-", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _h(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


# --------------------------------------------------------------------------- World Bank v5

def _wb_type(sheet: str, typ: str) -> str:
    t = (typ or "").strip().lower()
    if t and t != "flare":
        return t[:32]
    s = sheet.lower().replace("_", " ")
    for k in ("upstream", "midstream", "oil downstream", "gas downstream", "downstream"):
        if k in s:
            return k
    return "flare"


def parse_worldbank(body: bytes, year: int) -> list[dict]:
    """Every flare of the year: {lat, lon, bcm, iso3, country, kind}. Site sheets are those whose header has a
    latitude, a longitude and a BCM column; country and world summaries and the 2019 'all flares' sheet (a copy of
    the upstream and downstream sheets) are skipped."""
    out = []
    for name, rows in sheets(body):
        low = name.lower()
        if any(k in low for k in ("countr", "iso_", "world", "readme", "definition", "all flares")):
            continue
        cols = None
        for row in rows():
            h = [_h(x) for x in row]
            if cols is None:
                lat = next((i for i, x in enumerate(h) if x == "latitude"), None)
                lon = next((i for i, x in enumerate(h) if x == "longitude"), None)
                bcm = next((i for i, x in enumerate(h) if x.startswith("bcm")), None)
                if lat is not None and lon is not None and bcm is not None:
                    cols = {"lat": lat, "lon": lon, "bcm": bcm,
                            "iso": next((i for i, x in enumerate(h) if x in ("isocode", "iso")), None),
                            "country": next((i for i, x in enumerate(h) if x == "country"), None),
                            "type": next((i for i, x in enumerate(h) if x == "type"), None)}
                continue
            if len(row) <= max(cols["lat"], cols["lon"], cols["bcm"]):
                continue
            la, lo, v = _f(row[cols["lat"]]), _f(row[cols["lon"]]), _f(row[cols["bcm"]])
            if la is None or lo is None or v is None or not (-90 <= la <= 90 and -180 <= lo <= 180):
                continue

            def cell(k):
                i = cols[k]
                return row[i].strip() if i is not None and i < len(row) else ""
            out.append({"lat": la, "lon": lo, "bcm": v, "iso3": cell("iso")[:3].upper() or None,
                        "country": cell("country") or None, "kind": _wb_type(name, cell("type"))})
    if not out:
        raise ValueError(f"worldbank_ggfd {year}: no flare rows found")
    return out


def cell_key(lat: float, lon: float) -> str:
    return f"c{math.floor(lat * CELL):+06d}{math.floor(lon * CELL):+06d}"


def worldbank_sites(flares: list[dict]) -> dict[str, dict]:
    """Flares -> 0.01 degree cells: summed BCM, volume-weighted position, the largest flare's country and type."""
    cells: dict[str, dict] = {}
    for x in flares:
        k = cell_key(x["lat"], x["lon"])
        c = cells.setdefault(k, {"bcm": 0.0, "wlat": 0.0, "wlon": 0.0, "n": 0, "top": None, "lat0": x["lat"],
                                 "lon0": x["lon"]})
        c["bcm"] += x["bcm"]
        w = max(x["bcm"], 0.0)
        c["wlat"] += x["lat"] * w
        c["wlon"] += x["lon"] * w
        c["n"] += 1
        if c["top"] is None or x["bcm"] > c["top"]["bcm"]:
            c["top"] = x
    out = {}
    for k, c in cells.items():
        tot = c["bcm"] if c["bcm"] > 0 else 0
        lat = c["wlat"] / tot if tot else c["lat0"]
        lon = c["wlon"] / tot if tot else c["lon0"]
        out[k] = {"lat": round(lat, 6), "lon": round(lon, 6), "bcm": c["bcm"], "n": c["n"],
                  "iso3": c["top"]["iso3"], "country": c["top"]["country"], "kind": c["top"]["kind"]}
    return out


# --------------------------------------------------------------------------- North Dakota

def parse_ndic(body: bytes) -> dict:
    """The month's wells -> {"period": date, "wells": [...]} from the 'Oil' sheet (the workbook's other sheet,
    SkimmedCrudeRecovery, is not production)."""
    sh = sheets(body)
    pick = next(((n, r) for n, r in sh if n.strip().lower() == "oil"), sh[0] if sh else None)
    if pick is None:
        raise ValueError("ndic: empty workbook")
    cols, period, wells = None, None, []
    for row in pick[1]():
        if cols is None:
            h = [_h(x) for x in row]
            if "flared" in h and "county" in h:
                cols = {k: (h.index(k) if k in h else None) for k in
                        ("reportdate", "apiwellno", "company", "wellname", "section", "township", "range", "county",
                         "fieldname", "gas", "gassold", "flared", "lat", "long")}
            continue
        if not row or not row[0].strip():
            continue

        def g(k):
            i = cols[k]
            return row[i].strip() if i is not None and i < len(row) else ""
        if period is None and g("reportdate"):
            try:
                rd = g("reportdate")
                d = (date(1899, 12, 30) + timedelta(days=int(float(rd)))) if re.fullmatch(r"[\d.]+", rd) \
                    else ercot._day(rd.split()[0])
                period = d.replace(day=1)
            except ValueError:
                pass
        wells.append({"api": g("apiwellno"), "company": g("company"), "county": g("county").upper(),
                      "field": g("fieldname"), "section": g("section"), "township": g("township"),
                      "range": g("range"), "gas": _f(g("gas")), "flared": _f(g("flared")), "lat": _f(g("lat")),
                      "lon": _f(g("long"))})
    if cols is None:
        raise ValueError("ndic: no header with County and Flared")
    return {"period": period, "wells": wells}


def section_key(w: dict) -> str | None:
    try:
        return f"t{int(float(w['township']))}r{int(float(w['range']))}s{int(float(w['section']))}"
    except (TypeError, ValueError):
        return None


def ndic_aggregate(month: dict) -> tuple[dict, dict]:
    """-> ({county code: {flared, gas, wells}}, {section key: {flared, lat, lon, county, wells, fields}}). Wells
    flaring nothing (or not reporting) count toward the county's gas, not toward any site."""
    counties: dict[str, dict] = defaultdict(lambda: {"flared": 0.0, "gas": 0.0, "wells": 0, "reported": 0})
    sites: dict[str, dict] = {}
    for w in month["wells"]:
        c = counties[w["county"] or "?"]
        if w["gas"] is not None:
            c["gas"] += w["gas"]
        if w["flared"] is not None:
            c["flared"] += w["flared"]
            c["reported"] += 1
        if not w["flared"] or w["flared"] <= 0:
            continue
        c["wells"] += 1
        k = section_key(w)
        if k is None or w["lat"] is None or w["lon"] is None:
            continue
        s = sites.setdefault(k, {"flared": 0.0, "lats": [], "lons": [], "county": w["county"], "wells": 0,
                                 "fields": set(), "companies": set()})
        s["flared"] += w["flared"]
        s["lats"].append(w["lat"])
        s["lons"].append(w["lon"])
        s["wells"] += 1
        if w["field"]:
            s["fields"].add(w["field"])
        if w["company"]:
            s["companies"].add(w["company"])
    for s in sites.values():
        s["lat"] = round(sum(s.pop("lats")) / s["wells"], 6)
        s["lon"] = round(sum(s.pop("lons")) / s["wells"], 6)
        s["fields"], s["companies"] = sorted(s["fields"])[:5], sorted(s["companies"])[:5]
    return dict(counties), sites


# --------------------------------------------------------------------------- storage

def _session():
    return storage._session()


def _upsert_sites(rows: list[dict], now: datetime) -> dict[str, int]:
    """{site_key: id}; a known site's position, names and meta are refreshed (the latest load wins)."""
    from sqlalchemy.dialects.postgresql import insert

    from store.power import PowerFlareSite as S

    out = {}
    with _session().begin() as s:
        for i in range(0, len(rows), 1000):
            stmt = insert(S).values([{**r, "created_at": now, "updated_at": now} for r in rows[i:i + 1000]])
            ex = stmt.excluded
            cols = ("name", "kind", "country", "country_name", "state", "county", "lat", "lon", "meta", "visibility")
            stmt = stmt.on_conflict_do_update(
                constraint="uq_power_flare_sites_source_key",
                set_={**{c: getattr(ex, c) for c in cols}, "updated_at": ex.updated_at},
                where=or_distinct(S, ex, cols),
            ).returning(S.site_key, S.id)
            out.update(dict(s.execute(stmt).all()))
        missing = [r["site_key"] for r in rows if r["site_key"] not in out]
        if missing:   # unchanged rows are not RETURNed by a guarded DO UPDATE
            from sqlalchemy import select
            src = rows[0]["source"]
            for i in range(0, len(missing), 5000):
                out.update(dict(s.execute(select(S.site_key, S.id).where(
                    S.source == src, S.site_key.in_(missing[i:i + 5000]))).all()))
    return out


def or_distinct(T, ex, cols):
    from sqlalchemy import or_

    return or_(*[getattr(T, c).is_distinct_from(getattr(ex, c)) for c in cols])


def _upsert_volumes(rows: list[dict], now: datetime) -> dict:
    """INSERT .. ON CONFLICT (site, year): replaced only when a value differs. {new, changed, unchanged}."""
    from sqlalchemy import literal_column
    from sqlalchemy.dialects.postgresql import insert

    from store.power import PowerFlareVolume as V

    new = changed = 0
    cols = ("volume_m3", "volume_mcf", "monthly_mcf", "months_reported", "detections", "energy_mwh_derived")
    with _session().begin() as s:
        for i in range(0, len(rows), 1000):
            stmt = insert(V).values([{**r, "updated_at": now} for r in rows[i:i + 1000]])
            ex = stmt.excluded
            stmt = stmt.on_conflict_do_update(
                index_elements=[V.site_id, V.year],
                set_={**{c: getattr(ex, c) for c in cols}, "fetch_id": ex.fetch_id, "updated_at": ex.updated_at},
                where=or_distinct(V, ex, cols),
            ).returning(literal_column("(xmax = 0)").label("inserted"))
            for r in s.execute(stmt):
                if r.inserted:
                    new += 1
                else:
                    changed += 1
    return {"new": new, "changed": changed, "unchanged": len(rows) - new - changed}


def _upsert_areas(rows: list[dict], now: datetime) -> dict:
    from sqlalchemy import literal_column
    from sqlalchemy.dialects.postgresql import insert

    from store.power import PowerFlareArea as A

    if not rows:
        return {"new": 0, "changed": 0, "unchanged": 0}
    new = changed = 0
    cols = ("volume_mcf", "gas_produced_mcf", "wells", "energy_mwh_derived", "meta", "visibility")
    with _session().begin() as s:
        stmt = insert(A).values([{**r, "updated_at": now} for r in rows])
        ex = stmt.excluded
        stmt = stmt.on_conflict_do_update(
            index_elements=[A.source, A.area_type, A.country, A.state, A.area, A.period, A.period_type],
            set_={**{c: getattr(ex, c) for c in cols}, "fetch_id": ex.fetch_id, "updated_at": ex.updated_at},
            where=or_distinct(A, ex, cols),
        ).returning(literal_column("(xmax = 0)").label("inserted"))
        for r in s.execute(stmt):
            if r.inserted:
                new += 1
            else:
                changed += 1
    return {"new": new, "changed": changed, "unchanged": len(rows) - new - changed}


def _stored_urls(ds: _Ds) -> set[str]:
    from sqlalchemy import text

    with _session()() as s:
        return {r[0] for r in s.execute(text("""SELECT url FROM power_fetches WHERE dataset = :d AND ok
                                                AND NOT empty AND days_stored > 0"""), {"d": ds.key})}


def _record(ds: _Ds, f: Fetched, n: int = 0) -> int:
    fid = storage.record_fetch(ds, f, keep=False)
    if n:
        storage.set_days_stored(fid, n)
    return fid


# --------------------------------------------------------------------------- loaders

def load_worldbank(body: bytes, year: int, fetch_id: int | None, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    vis = SOURCES["worldbank_ggfd"].terms.visibility
    cells = worldbank_sites(parse_worldbank(body, year))
    ids = _upsert_sites([{"source": "worldbank_ggfd", "site_key": k, "name": None, "kind": c["kind"],
                          "country": c["iso3"], "country_name": c["country"], "state": None, "county": None,
                          "lat": c["lat"], "lon": c["lon"], "meta": {"cell_deg": 1 / CELL}, "visibility": vis}
                         for k, c in cells.items()], now)
    vols = []
    for k, c in cells.items():
        m3 = c["bcm"] * 1e9
        mcf = m3 / M3_PER_MCF
        vols.append({"site_id": ids[k], "year": year, "volume_m3": round(m3, 1), "volume_mcf": round(mcf, 1),
                     "monthly_mcf": None, "months_reported": None, "detections": c["n"],
                     "energy_mwh_derived": mwh(mcf), "fetch_id": fetch_id})
    res = _upsert_volumes(vols, now)
    return {"year": year, "flares": sum(c["n"] for c in cells.values()), "sites": len(cells),
            "bcm": round(sum(c["bcm"] for c in cells.values()), 4), **res}


def load_ndic(body: bytes, fetch_id: int | None, now: datetime | None = None,
              period: date | None = None) -> dict:
    """One month: county rows (power_flare_areas) and the month's slot of each section's year row."""
    from sqlalchemy import select

    from store.power import PowerFlareArea as A, PowerFlareSite as S, PowerFlareVolume as V

    now = now or datetime.now(UTC)
    vis = SOURCES["ndic"].terms.visibility
    month = parse_ndic(body)
    p = month["period"] or period
    if p is None:
        raise ValueError("ndic: no ReportDate")
    if period and p != period:
        raise ValueError(f"ndic: file for {p}, asked for {period}")
    counties, sites = ndic_aggregate(month)
    areas = [{"source": "ndic", "area_type": "county", "country": "USA", "state": "ND",
              "area": ND_COUNTIES.get(code, code), "period": p, "period_type": "month",
              "volume_mcf": round(c["flared"], 1), "gas_produced_mcf": round(c["gas"], 1), "wells": c["wells"],
              "energy_mwh_derived": mwh(c["flared"]),
              "meta": {"county_code": code, "wells_reporting_flared": c["reported"],
                       "flared_share": round(c["flared"] / c["gas"], 5) if c["gas"] else None},
              "fetch_id": fetch_id, "visibility": vis} for code, c in counties.items()]
    area_res = _upsert_areas(areas, now)
    ids = _upsert_sites([{"source": "ndic", "site_key": k, "name": ", ".join(s["fields"]) or None, "kind": "wells",
                          "country": "USA", "country_name": "United States", "state": "ND",
                          "county": ND_COUNTIES.get(s["county"], s["county"]), "lat": s["lat"], "lon": s["lon"],
                          "meta": {"plss": k, "fields": s["fields"], "operators": s["companies"],
                                   "wells_flaring_last_month": s["wells"], "last_month": p.isoformat()},
                          "visibility": vis} for k, s in sites.items()], now)
    # the year rows: this month's slot set (sections flaring nothing this month get 0 if they have a row)
    with _session()() as s:
        have = {r.site_id: list(r.monthly_mcf or [None] * 12) for r in s.execute(
            select(V.site_id, V.monthly_mcf).join(S, S.id == V.site_id)
            .where(V.year == p.year, S.source == "ndic"))}
        # months of the year already loaded: a section first seen now flared nothing in them (0, not "unknown"), so
        # the result does not depend on the order months are loaded in
        loaded = {r[0].month for r in s.execute(select(A.period).where(
            A.source == "ndic", A.period_type == "month", A.period >= date(p.year, 1, 1),
            A.period <= date(p.year, 12, 1)).distinct())}
    by_id = {ids[k]: s["flared"] for k, s in sites.items()}
    vols = []
    for sid in set(have) | set(by_id):
        arr = have.get(sid) or [0.0 if m + 1 in loaded else None for m in range(12)]
        arr = (arr + [None] * 12)[:12]
        arr[p.month - 1] = round(by_id.get(sid, 0.0), 1)
        xs = [x for x in arr if x is not None]
        mcf = sum(xs)
        vols.append({"site_id": sid, "year": p.year, "volume_m3": round(mcf * M3_PER_MCF, 1),
                     "volume_mcf": round(mcf, 1), "monthly_mcf": arr, "months_reported": len(xs),
                     "detections": None, "energy_mwh_derived": mwh(mcf), "fetch_id": fetch_id})
    res = _upsert_volumes(vols, now)
    return {"period": p, "wells": len(month["wells"]), "counties": len(counties), "sites": len(sites),
            "flared_mcf": round(sum(c["flared"] for c in counties.values()), 1), "areas": area_res, **res}


# --------------------------------------------------------------------------- runs

def _months(a: date, b: date) -> list[date]:
    out, d = [], a.replace(day=1)
    while d <= b:
        out.append(d)
        d = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return out


def _nd_stored() -> set[date]:
    from sqlalchemy import text

    with _session()() as s:
        return {r[0] for r in s.execute(text("""SELECT DISTINCT period FROM power_flare_areas
                                                WHERE source = 'ndic' AND period_type = 'month'"""))}


async def _run_worldbank(get: Client, refresh: bool, dry_run: bool) -> dict:
    stored = set() if refresh else await asyncio.to_thread(_stored_urls, WB_DS)
    todo = {y: u for y, u in WB_FILES.items() if u not in stored}
    out = {"source": "worldbank_ggfd", "files": len(WB_FILES), "to_fetch": len(todo), "years": []}
    if dry_run:
        return out
    for year, url in sorted(todo.items()):
        f = await get.get(url)
        f.start, f.end = date(year, 1, 1), date(year, 12, 31)
        if not f.ok:
            await asyncio.to_thread(_record, WB_DS, f)
            out["years"].append({"year": year, "error": f.error or f.status})
            continue
        try:
            fid = await asyncio.to_thread(_record, WB_DS, f)
            res = await asyncio.to_thread(load_worldbank, f.body, year, fid)
            await asyncio.to_thread(storage.set_days_stored, fid, res["sites"])
            out["years"].append(res)
        except Exception as exc:  # noqa: BLE001 - a malformed file is a failed row, the others go on
            f.error = f"parse: {type(exc).__name__}: {exc}"[:500]
            await asyncio.to_thread(_record, WB_DS, f)
            out["years"].append({"year": year, "error": f.error})
    return out


async def _run_ndic(get: Client, now: datetime, since: date | None, refresh: bool, dry_run: bool) -> dict:
    stored = await asyncio.to_thread(_nd_stored)
    last_possible = (now.astimezone(UTC).date().replace(day=1) - timedelta(days=1)).replace(day=1)
    months = _months(max(since or ND_FIRST, ND_FIRST), last_possible)
    recent = sorted(stored)[-REVISE_MONTHS:] if stored else []
    todo = [m for m in months if refresh or m not in stored or m in recent]
    out = {"source": "ndic", "months": len(months), "stored": len(stored), "to_fetch": len(todo), "loaded": [],
           "not_posted": [], "failed": []}
    if dry_run:
        return out
    for m in todo:
        f = await get.get(ND_URL.format(y=m.year, m=m.month))
        f.start, f.end = m, (m.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        if f.status == 404:
            f.empty, f.error = True, None
            await asyncio.to_thread(_record, ND_DS, f)
            out["not_posted"].append(m)
            continue
        if not f.ok:
            await asyncio.to_thread(_record, ND_DS, f)
            out["failed"].append({"month": m, "error": f.error or f.status})
            continue
        try:
            fid = await asyncio.to_thread(_record, ND_DS, f)
            res = await asyncio.to_thread(load_ndic, f.body, fid, None, m)
            await asyncio.to_thread(storage.set_days_stored, fid, res["sites"] or 1)
            out["loaded"].append({k: res[k] for k in ("period", "wells", "sites", "flared_mcf", "new", "changed",
                                                      "unchanged")})
        except Exception as exc:  # noqa: BLE001
            f.error = f"parse: {type(exc).__name__}: {exc}"[:500]
            await asyncio.to_thread(_record, ND_DS, f)
            out["failed"].append({"month": m, "error": f.error})
    return out


async def run(sources: list[str] | None = None, since: date | None = None, refresh: bool = False,
              dry_run: bool = False, client: httpx.AsyncClient | None = None, now: datetime | None = None) -> dict:
    """Idempotent and resumable: a stored World Bank file is not fetched again; a stored North Dakota month is not
    fetched again except the last REVISE_MONTHS (late corrections become counted changes)."""
    now = now or datetime.now(UTC)
    names = [s for s in (sources or list(SOURCES)) if s in SOURCES]
    own = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True)
    out = {"dry_run": dry_run, "sources": []}
    try:
        if "worldbank_ggfd" in names:
            get = Client(client, "worldbank_ggfd", PACE_SECONDS)
            out["sources"].append(await _run_worldbank(get, refresh, dry_run))
            out["sources"][-1]["requests"] = get.requests
        if "ndic" in names:
            get = Client(client, "ndic", PACE_SECONDS)
            out["sources"].append(await _run_ndic(get, now, since, refresh, dry_run))
            out["sources"][-1]["requests"] = get.requests
    finally:
        if own:
            await client.aclose()
    return out


FORWARD_EVERY = timedelta(days=7)


def _last_fetch(ds: _Ds) -> datetime | None:
    from sqlalchemy import text

    with _session()() as s:
        return s.execute(text("SELECT max(fetched_at) FROM power_fetches WHERE dataset = :d"), {"d": ds.key}).scalar()


async def forward_if_due(now: datetime | None = None) -> dict:
    """For the power_forward job (twice a day): the monthly reports change once a month, so a run at most weekly."""
    now = now or datetime.now(UTC)
    last = await asyncio.to_thread(_last_fetch, ND_DS)
    if last and now - last < FORWARD_EVERY:
        return {"skipped": f"last flaring fetch {last.isoformat()}, under {FORWARD_EVERY.days} days ago"}
    return await run(now=now)


def status() -> dict:
    from sqlalchemy import text

    with _session()() as s:
        sites = [dict(r._mapping) for r in s.execute(text("""
            SELECT f.source, count(DISTINCT f.id) AS sites, count(v.*) AS site_years, min(v.year) AS first_year,
                   max(v.year) AS last_year, sum(v.volume_m3) / 1e9 AS bcm_all_years
            FROM power_flare_sites f LEFT JOIN power_flare_volumes v ON v.site_id = f.id GROUP BY f.source"""))]
        areas = [dict(r._mapping) for r in s.execute(text("""
            SELECT source, area_type, period_type, count(*) AS rows, min(period) AS first, max(period) AS last
            FROM power_flare_areas GROUP BY 1, 2, 3"""))]
        sizes = dict(s.execute(text("""SELECT c.relname, pg_total_relation_size(c.oid) FROM pg_class c
                                       WHERE c.relkind = 'r' AND c.relname LIKE 'power\\_flare\\_%'""")).all())
    return {"sites": sites, "areas": areas, "table_bytes": sizes}


def verdicts() -> list[dict]:
    return [{"source": k, "operator": v.terms.operator, "visibility": v.terms.visibility, "pulled": True,
             "reason": v.terms.reason, "terms_url": v.terms.terms_url, "terms_checked": v.terms.checked,
             "attribution": v.terms.attribution, "description": v.description} for k, v in SOURCES.items()]
