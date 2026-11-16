"""Berkeley Lab "Queued Up": every interconnection request in the US queues, one workbook a year.

    GET https://eta-publications.lbl.gov/sites/default/files/2026-05/lbnl_ix_queue_data_file_thru2025.xlsx
        sheet "03. Complete Queue Data": q_id, q_status, q_date, prop_date, on_date, wd_date, ia_date,
        IA_phase_raw, IA_phase_clean, county, state, fips_code, poi_name, region, project_name, utility, entity,
        developer, cluster, service, project_type, type_1..3, type_clean, mw_1..3, q_year, prop_year

Measured 2026-11-15: the 2026 edition (data through 2025-12-31) is 15.6 MB with 38,201 requests across the 7 ISOs
and ~50 non-ISO utilities ("West", "Southeast"). Generation and storage only: Queued Up carries no load requests.

Terms (power/sites/registry.py): the 2026 edition is CC BY 4.0 (public, with attribution to LBNL and GridTracker);
the 2025 edition's page states no licence (internal). The landing page emp.lbl.gov/queues sits behind a Cloudflare
browser challenge, which is not bypassed: each edition's file URL on eta-publications.lbl.gov is listed below by
hand (one new line a year, after reading that edition's licence).

Project id: "<entity>|<q_id>" (the codebook: "Combine with entity to form a unique identifier"). Some requests have
no queue id ("not assigned") or repeat one; those get "#2", "#3" in file order, so their ids are not stable across
editions. MW: mw_1 + mw_2 + mw_3 (hybrids list each part). Dates are Excel day numbers.
"""

from __future__ import annotations

import re
from datetime import date

from power.sites import xlsx

SOURCE = "lbnl_queues"
DS = "sites.lbnl_queued_up"
EDITIONS: dict[date, str] = {   # data as of -> file (eta-publications.lbl.gov; see the module docstring)
    date(2024, 12, 31): "https://eta-publications.lbl.gov/sites/default/files/2025-08/"
                        "lbnl_ix_queue_data_file_thru2024_v2.xlsx",
    date(2025, 12, 31): "https://eta-publications.lbl.gov/sites/default/files/2026-05/"
                        "lbnl_ix_queue_data_file_thru2025.xlsx",
}
# "unknown" (10 rows in the 2026 edition) is kept as such: never counted as queued
STATUS = {"active": "active", "withdrawn": "withdrawn", "suspended": "suspended", "operational": "operational",
          "unknown": "unknown"}


def _sheet(data: bytes) -> str:
    names = xlsx.sheets(data)
    for n in names:
        if "complete queue data" in n.lower():
            return n
    raise ValueError(f"lbnl: no 'Complete Queue Data' sheet in {names[:8]}")


# Column names moved between editions (2025: mw1, fips_codes, ia_status_clean; 2026: mw_1, fips_code,
# IA_phase_clean); the first present name wins
ALIASES = {"mw_1": ("mw_1", "mw1"), "mw_2": ("mw_2", "mw2"), "mw_3": ("mw_3", "mw3"),
           "fips": ("fips_code", "fips_codes"), "phase": ("ia_phase_clean", "ia_status_clean")}


def _get(r: dict, name: str) -> str:
    for k in ALIASES.get(name, (name,)):
        v = (r.get(k) or "").strip()
        if v and v.upper() != "NA":    # the 2025 edition writes NA for empty
            return v
    return ""


def _txt(v, n: int) -> str | None:
    s = re.sub(r"\s+", " ", str(v or "")).strip()
    return None if s.upper() == "NA" else (s[:n] or None)


def _fips(v) -> str | None:
    x = xlsx.num(re.split(r"[,;\s]+", str(v or "").strip())[0])   # several counties: the first, as for county
    s = f"{int(x):05d}" if x and x > 0 else ""
    return s if len(s) == 5 else None          # a few rows carry a non-county number: dropped, not truncated


def parse(data: bytes) -> list[dict]:
    _, rows = xlsx.table(data, _sheet(data), ("q_id", "q_status", "state", "county"))
    out, seen = [], {}
    for r in rows:
        status = STATUS.get(_get(r, "q_status").lower())
        if not status:
            continue
        base = f"{_get(r, 'entity')}|{(r.get('q_id') or '').strip()}"[:150]
        seen[base] = seen.get(base, 0) + 1
        pid = base if seen[base] == 1 else f"{base}#{seen[base]}"
        mws = [xlsx.num(_get(r, k)) for k in ("mw_1", "mw_2", "mw_3")]
        mw = sum(m for m in mws if m is not None) if any(m is not None for m in mws) else None
        st = _get(r, "state").upper()
        out.append({
            "project_id": pid, "iso": _txt(_get(r, "region"), 24), "entity": _txt(_get(r, "entity"), 120),
            "state": st if re.fullmatch(r"[A-Z]{2}", st) else None, "county": _txt(_get(r, "county"), 64),
            "fips": _fips(_get(r, "fips")), "kind": "generation", "type": _txt(_get(r, "type_clean"), 48),
            "type_detail": _txt(_get(r, "project_type"), 120), "mw": mw, "status": status,
            "phase": _txt(_get(r, "phase"), 80), "queue_date": xlsx.serial_date(_get(r, "q_date")),
            "proposed_cod": xlsx.serial_date(_get(r, "prop_date")), "on_date": xlsx.serial_date(_get(r, "on_date")),
            "withdrawn_date": xlsx.serial_date(_get(r, "wd_date")), "ia_date": xlsx.serial_date(_get(r, "ia_date")),
            "poi": _txt(_get(r, "poi_name"), 200), "project_name": _txt(_get(r, "project_name"), 200),
            "developer": _txt(_get(r, "developer"), 200)})
    if not out:
        raise ValueError("lbnl: no project rows")
    return out


async def sync(get, dry_run: bool = False, editions: dict[date, str] | None = None) -> dict:
    """Each listed edition not yet stored: one request each. Stored editions are never re-read."""
    import asyncio

    from power.sites import registry, store

    eds = editions if editions is not None else EDITIONS
    have = await asyncio.to_thread(store.editions, SOURCE)
    todo = [(e, u) for e, u in sorted(eds.items()) if e not in have]
    out = {"dataset": DS, "requests": 0, "editions_to_fetch": [str(e) for e, _ in todo], "editions": []}
    if dry_run:
        return out
    for ed, url in todo:
        f = await get.get(url)
        out["requests"] += 1
        if not f.ok:
            await asyncio.to_thread(store.record, SOURCE, DS, f, ed, ed)
            out["editions"].append({"edition": ed, "error": f.error or f"HTTP {f.status}"})
            continue
        try:
            rows = await asyncio.to_thread(parse, f.body)
        except Exception as exc:  # noqa: BLE001 - recorded as a failed parse
            f.error = f"parse: {type(exc).__name__}: {exc}"[:500]
            await asyncio.to_thread(store.record, SOURCE, DS, f, ed, ed)
            out["editions"].append({"edition": ed, "error": f.error})
            continue
        fid = await asyncio.to_thread(store.record, SOURCE, DS, f, ed, ed)
        res = await asyncio.to_thread(store.replace_edition, SOURCE, ed, rows, registry.visibility(SOURCE, str(ed)),
                                      fid)
        await asyncio.to_thread(store.mark_applied, fid)
        out["editions"].append({"edition": ed, **res})
    return out
