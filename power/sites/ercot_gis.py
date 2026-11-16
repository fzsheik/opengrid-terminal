"""ERCOT Generator Interconnection Status (GIS) report, PG7-200-ER: the generation projects queued to connect in
ERCOT, monthly, from the keyless public MIS (no account).

    GET https://www.ercot.com/misapp/servlets/IceDocListJsonWS?reportTypeId=15933   the report's documents
    GET https://www.ercot.com/misdownload/servlets/mirDownload?doclookupId=<DocID>    one xlsx

The list mixes "GIS_Report_<Month><Year>" (also "GIS_Report_<Month>_<Year>" in older ones) with the monthly
"Co-located_Battery_Identification_Report"; only GIS reports are read. Measured 2026-11-15: 101 GIS documents
covering 94 months from December 2018 (posted from 2020), 430-670 KB each; the October 2026 report lists 1,766
projects, 446 GW (sheet "Project Details -
Large Gen": INR, project name, GIM study phase, interconnecting entity, POI, county, CDR zone, projected COD, fuel,
technology, capacity MW, milestone dates) and a few dozen small ones ("Project Details - Small Gen"). Inactive
projects are excluded by ERCOT; only projects with a full interconnection study requested are listed.

Edition: the last day of the month the report is named for. A month republished under a new DocID replaces that
edition. Each document is downloaded once (ERCOT's API terms cap a report at three downloads a year).

Large loads: ERCOT publishes no project-level large-load queue (power/sites/registry.py ERCOT_TERMS), so none.
"""

from __future__ import annotations

import calendar
import json
import re
from datetime import date

from power.sites import xlsx

SOURCE = "ercot_gis"
DS = "sites.ercot_gis"
REPORT = "15933"
LIST_URL = "https://www.ercot.com/misapp/servlets/IceDocListJsonWS"
DOC_URL = "https://www.ercot.com/misdownload/servlets/mirDownload"
MONTHS = [m.lower() for m in calendar.month_name]
_NAME = re.compile(r"^GIS_Report_([A-Za-z]+)_?(\d{4})$")
_INR = re.compile(r"^\d{2}INR\d{4}[A-Za-z0-9]*$")
FUEL = {"SOL": "Solar", "WIN": "Wind", "GAS": "Gas", "NUC": "Nuclear", "COA": "Coal", "HYD": "Hydro",
        "BIO": "Biomass", "GEO": "Geothermal", "OIL": "Oil", "OTH": "Other", "WAT": "Hydro", "MWH": "Battery"}
TECH_TYPE = {"BA": "Battery"}     # ERCOT files storage as fuel OTH (or MWH), technology BA


def documents(body: bytes) -> list[dict]:
    """[{"doc_id", "name", "edition", "published"}] of the GIS reports, newest publish first per edition."""
    try:
        lst = json.loads(body)["ListDocsByRptTypeRes"]["DocumentList"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"ercot_gis: not a document list: {exc}") from exc
    out = []
    for x in lst or []:
        d = x.get("Document") or {}
        m = _NAME.match(str(d.get("FriendlyName") or "").strip())
        if not m or m.group(1).lower() not in MONTHS:
            continue
        y, mo = int(m.group(2)), MONTHS.index(m.group(1).lower())
        out.append({"doc_id": str(d.get("DocID")), "name": d["FriendlyName"],
                    "edition": date(y, mo, calendar.monthrange(y, mo)[1]), "published": d.get("PublishDate") or ""})
    out.sort(key=lambda r: (r["edition"], r["published"]), reverse=True)
    newest: dict[date, dict] = {}
    for r in out:
        newest.setdefault(r["edition"], r)
    return list(newest.values())


def _txt(v, n: int) -> str | None:
    s = re.sub(r"\s+", " ", str(v or "")).strip()
    return s[:n] or None


def _county(v) -> str | None:
    s = _txt(v, 64)
    return re.sub(r"\s+county$", "", s, flags=re.I) if s else None


def parse(data: bytes) -> list[dict]:
    out, seen = [], set()
    sheets = [n for n in xlsx.sheets(data) if n.lower().startswith("project details")]
    if not sheets:
        raise ValueError("ercot_gis: no 'Project Details' sheet")
    for sheet in sheets:
        small = "small" in sheet.lower()
        _, rows = xlsx.table(data, sheet, ("INR", "County"))
        for r in rows:
            inr = (r.get("inr") or "").strip()
            if not _INR.match(inr) or inr in seen:
                continue
            seen.add(inr)
            mw_key = next((k for k in r if k.startswith("capacity")), None)
            fuel = (r.get("fuel") or "").strip().upper()
            tech = (r.get("technology") or "").strip().upper()
            typ = TECH_TYPE.get(tech) or FUEL.get(fuel) or (fuel.title() or None)
            out.append({
                "project_id": inr, "iso": "ERCOT", "entity": "ERCOT", "state": "TX", "county": _county(r.get("county")),
                "fips": None, "kind": "generation", "type": typ,
                "type_detail": _txt(f"{fuel}/{tech}" if fuel or tech else "", 120),
                "mw": xlsx.num(r.get(mw_key)) if mw_key else None, "status": "active",
                "phase": _txt("small generator" if small else r.get("gim_study_phase"), 80),
                "queue_date": xlsx.serial_date(r.get("screening_study_started")),
                "proposed_cod": xlsx.serial_date(r.get("projected_cod")), "on_date": None, "withdrawn_date": None,
                "ia_date": xlsx.serial_date(r.get("ia_signed")), "poi": _txt(r.get("poi_location"), 200),
                "project_name": _txt(r.get("project_name"), 200),
                "developer": _txt(r.get("interconnecting_entity"), 200)})
    if not out:
        raise ValueError("ercot_gis: no project rows")
    return out


async def sync(get, since: date | None = None, dry_run: bool = False) -> dict:
    """The report list (one request), then every GIS document not yet applied, oldest first."""
    import asyncio

    from power.sites import registry, store

    out = {"dataset": DS, "requests": 1, "editions": [], "failed": 0}
    lf = await get.get(LIST_URL, params={"reportTypeId": REPORT})
    try:
        docs = documents(lf.body) if lf.ok else None
    except ValueError as exc:
        lf.error, docs = str(exc)[:500], None
    if docs is None:
        await asyncio.to_thread(store.record, SOURCE, DS, lf)
        out.update(failed=1, error=lf.error or f"HTTP {lf.status}")
        return out
    done = await asyncio.to_thread(store.applied, DS)
    done_ids = {m.group(1) for u in done for m in [re.search(r"doclookupId=(\d+)", u)] if m}
    todo = sorted((d for d in docs if d["doc_id"] not in done_ids and (not since or d["edition"] >= since)),
                  key=lambda d: d["edition"])
    out.update(listed=len(docs), editions_to_fetch=len(todo))
    if dry_run:
        return out
    vis = registry.visibility(SOURCE)
    for d in todo:
        f = await get.get(DOC_URL, params={"doclookupId": d["doc_id"]})
        out["requests"] += 1
        ed = d["edition"]
        try:
            if not f.ok:
                raise ValueError(f.error or f"HTTP {f.status}")
            rows = await asyncio.to_thread(parse, f.body)
        except Exception as exc:  # noqa: BLE001 - one bad document never stops the others
            f.error = f.error or f"parse: {type(exc).__name__}: {exc}"[:500]
            await asyncio.to_thread(store.record, SOURCE, DS, f, ed, ed)
            out["failed"] += 1
            out["editions"].append({"edition": ed, "doc": d["name"], "error": f.error})
            continue
        fid = await asyncio.to_thread(store.record, SOURCE, DS, f, ed, ed)
        res = await asyncio.to_thread(store.replace_edition, SOURCE, ed, rows, vis, fid)
        await asyncio.to_thread(store.mark_applied, fid)
        out["editions"].append({"edition": ed, "doc": d["name"], **res})
    return out
