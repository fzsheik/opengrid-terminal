"""Reads of the site tables, as plain dicts (the admin routes and the site-ranking work call these).

    plants(state=, ba=, technology=, status=, sort=, limit=, year=)   plants with capacity factor and retirements
    plants_near(lat, lon, km=50, **filters)                           the same, within km of a point, nearest first
    plants_in_county(state, county, **filters)
    queue_projects(source=, edition=, state=, county=, iso=, type=, status=, kind=, limit=)
    queued_mw_by_county(source=, edition=, state=, iso=, kind=, status="active")
    capacity_factor(net_mwh, generators, year)                        the arithmetic, testable by hand

A plant is the sum of its generators (power_generators). Its capacity factor for year Y is
    net generation in Y (EIA-923) / (sum over its non-storage generators of nameplate MW x the hours of Y each was
    in service)
where a generator is in service from its operating month and, if retired, until the month before its retirement
month (a currently operable generator is assumed in service through Y: EIA's monthly status history before 2008
is not read). Storage (batteries, pumped storage) is left out of the denominator: it consumes more than it makes,
so a plant with storage would otherwise look idle. Net generation includes the storage's losses, so a plant
with much storage can read slightly low. The factor is not clamped; a value above 1 means the inventory's
nameplate understates the plant.

"Idle MW" = operable non-storage nameplate x (1 - capacity factor, clamped to 0..1): the MW that sat unused on
average over the year. A large plant that rarely runs, or one with a planned retirement, marks a grid connection
that has room. Queued MW totals count only "active" requests by default (not withdrawn, suspended or operational),
per source and edition; LBNL already includes ERCOT's queue, so the two sources are never added together.
"""

from __future__ import annotations

import calendar
import math
import re
from datetime import date

from power.registry import PUBLIC

METHOD = ("capacity factor = net generation in the year (EIA-923) / (non-storage nameplate MW x hours in service "
          "that year); idle MW = operable non-storage nameplate x (1 - capacity factor clamped to 0..1)")
OPERABLE = ("operating", "standby", "out_of_service")
STORAGE_PRIME_MOVERS = {"BA", "ES", "FW", "CE", "PS"}     # batteries, other storage, flywheels, CAES, pumped hydro
SORTS = ("idle", "retirement", "nameplate", "cf")


def _session():
    from power import storage

    return storage._session()


def county_key(county: str | None) -> str | None:
    if not county:
        return None
    s = re.sub(r"\s+(county|parish|borough|census area|municipality)$", "", county.strip(), flags=re.I)
    return re.sub(r"\s+", " ", s).lower() or None


def is_storage(g: dict) -> bool:
    return (g.get("energy_source") or "").upper() == "MWH" or (g.get("prime_mover") or "").upper() in STORAGE_PRIME_MOVERS


def _ym(s: str | None) -> tuple[int, int] | None:
    if not s or len(s) < 7:
        return None
    try:
        return int(s[:4]), int(s[5:7])
    except ValueError:
        return None


def service_hours(g: dict, year: int) -> float:
    """Hours of `year` the generator was in service (whole months; see the module docstring)."""
    if g.get("status_group") not in (*OPERABLE, "retired"):
        return 0.0
    start = _ym(g.get("operating_ym")) or (year, 1)
    end = _ym(g.get("retirement_ym")) if g.get("status_group") == "retired" else None
    if g.get("status_group") == "retired" and end is None:
        return 0.0                     # left the inventory at an unknown month: not counted
    h = 0.0
    for m in range(1, 13):
        if (year, m) < start or (end is not None and (year, m) >= end):
            continue
        h += calendar.monthrange(year, m)[1] * 24
    return h


def capacity_factor(net_mwh: float | None, generators: list[dict], year: int) -> float | None:
    if net_mwh is None:
        return None
    mwh = sum((g.get("nameplate_mw") or 0.0) * service_hours(g, year) for g in generators if not is_storage(g))
    return net_mwh / mwh if mwh > 0 else None


# --------------------------------------------------------------------------- plants

GEN_COLS = ("plant_id", "generator_id", "plant_name", "entity_name", "state", "county", "latitude", "longitude",
            "ba_code", "technology", "energy_source", "prime_mover", "nameplate_mw", "summer_mw", "status",
            "status_group", "operating_ym", "planned_operation_ym", "planned_retirement_ym", "retirement_ym",
            "period")


def latest_year() -> int | None:
    """The newest year with generation for at least half as many plants as the year before (a partial year is
    skipped)."""
    from sqlalchemy import text

    with _session()() as s:
        rows = s.execute(text("SELECT year, count(*) FROM power_plant_generation GROUP BY year ORDER BY year DESC "
                              "LIMIT 3")).all()
    for i, (y, n) in enumerate(rows):
        if i + 1 >= len(rows) or n >= rows[i + 1][1] / 2:
            return y
    return None


def _generators(where: str, params: dict) -> list[dict]:
    from sqlalchemy import text

    with _session()() as s:
        return [dict(r._mapping) for r in s.execute(text(
            f"SELECT {', '.join(GEN_COLS)} FROM power_generators WHERE {where} ORDER BY plant_id, generator_id"),
            params)]


def _generation(plant_ids: list[int], year: int) -> dict[int, float | None]:
    from sqlalchemy import text

    if not plant_ids:
        return {}
    with _session()() as s:
        return dict(s.execute(text("SELECT plant_id, net_mwh FROM power_plant_generation WHERE year = :y "
                                   "AND plant_id = ANY(:p)"), {"y": year, "p": plant_ids}).all())


def _first(gs: list[dict], key: str):
    return next((g[key] for g in gs if g.get(key) not in (None, "")), None)


def _plant(pid: int, gs: list[dict], net: float | None, year: int | None) -> dict:
    op = [g for g in gs if g["status_group"] in OPERABLE]
    op_gen = [g for g in op if not is_storage(g)]
    mw = lambda xs: round(sum(g.get("nameplate_mw") or 0.0 for g in xs), 3)  # noqa: E731
    cf = capacity_factor(net, gs, year) if year else None
    nonstorage = mw(op_gen)
    idle = round(nonstorage * (1 - min(max(cf, 0.0), 1.0)), 3) if cf is not None else None
    retiring = [g for g in op if g.get("planned_retirement_ym")]
    retired = [g for g in gs if g["status_group"] == "retired"]
    groups: dict[str, int] = {}
    for g in gs:
        groups[g["status_group"]] = groups.get(g["status_group"], 0) + 1
    return {
        "plant_id": pid, "plant_name": _first(gs, "plant_name"), "operator": _first(gs, "entity_name"),
        "state": _first(gs, "state"), "county": _first(gs, "county"), "latitude": _first(gs, "latitude"),
        "longitude": _first(gs, "longitude"), "ba_code": _first(op or gs, "ba_code"),
        "technologies": sorted({g["technology"] for g in op if g.get("technology")}),
        "energy_sources": sorted({g["energy_source"] for g in op if g.get("energy_source")}),
        "nameplate_mw": mw(op), "nonstorage_mw": nonstorage, "storage_mw": round(mw(op) - nonstorage, 3),
        "summer_mw": round(sum(g.get("summer_mw") or 0.0 for g in op), 3),
        "planned_mw": mw([g for g in gs if g["status_group"] == "planned"]),
        "planned_operation_ym": min((g["planned_operation_ym"] for g in gs
                                     if g["status_group"] == "planned" and g.get("planned_operation_ym")), default=None),
        "retired_mw": mw(retired),
        "last_retirement_ym": max((g["retirement_ym"] for g in retired if g.get("retirement_ym")), default=None),
        "planned_retirement_ym": min((g["planned_retirement_ym"] for g in retiring), default=None),
        "planned_retirement_mw": mw(retiring),
        "generators": groups, "year": year if net is not None else None, "net_mwh": net,
        "capacity_factor": None if cf is None else round(cf, 4), "idle_mw": idle,
        "period": max(g["period"] for g in gs), "visibility": PUBLIC,
    }


def _filter_and_sort(out: list[dict], technology, status, sort, limit) -> list[dict]:
    if technology:
        t = technology.lower()
        out = [p for p in out if any(t in x.lower() for x in p["technologies"]) or
               any(t == x.lower() for x in p["energy_sources"])]
    if status:
        out = [p for p in out if p["generators"].get(status)]
    if sort == "retirement":
        out = [p for p in out if p["planned_retirement_ym"]]
        out.sort(key=lambda p: (p["planned_retirement_ym"], -p["planned_retirement_mw"]))
    elif sort == "nameplate":
        out.sort(key=lambda p: -p["nameplate_mw"])
    elif sort == "cf":
        out.sort(key=lambda p: (p["capacity_factor"] is None, p["capacity_factor"] or 0.0, -p["nameplate_mw"]))
    elif sort == "idle":
        out.sort(key=lambda p: (p["idle_mw"] is None, -(p["idle_mw"] or 0.0), -p["nameplate_mw"]))
    return out[:limit] if limit else out


def _build(where: str, params: dict, year: int | None) -> list[dict]:
    gens = _generators(where, params)
    by: dict[int, list[dict]] = {}
    for g in gens:
        by.setdefault(g["plant_id"], []).append(g)
    year = year or latest_year()
    net = _generation(list(by), year) if year else {}
    return [_plant(pid, gs, net.get(pid), year) for pid, gs in by.items()]


def plants(state: str | None = None, ba: str | None = None, technology: str | None = None,
           status: str | None = None, sort: str = "idle", limit: int | None = 100, year: int | None = None,
           county: str | None = None) -> list[dict]:
    if sort not in SORTS:
        raise ValueError(f"sort must be one of {SORTS}")
    where, params = ["true"], {}
    if state:
        where.append("plant_id IN (SELECT plant_id FROM power_generators WHERE state = :st)")
        params["st"] = state.upper()
    if ba:
        where.append("plant_id IN (SELECT plant_id FROM power_generators WHERE upper(ba_code) = :ba)")
        params["ba"] = ba.upper()
    if county:
        where.append("plant_id IN (SELECT plant_id FROM power_generators WHERE lower(regexp_replace(county, "
                     "'\\s+(County|Parish|Borough)$', '', 'i')) = :cty)")
        params["cty"] = county_key(county)
    return _filter_and_sort(_build(" AND ".join(where), params, year), technology, status, sort, limit)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(min(1.0, a)))


def plants_near(lat: float, lon: float, km: float = 50.0, technology: str | None = None,
                status: str | None = None, limit: int | None = None, year: int | None = None) -> list[dict]:
    dlat = km / 111.0
    dlon = km / max(1e-6, 111.0 * math.cos(math.radians(lat)))
    where = ("plant_id IN (SELECT plant_id FROM power_generators WHERE latitude BETWEEN :a AND :b "
             "AND longitude BETWEEN :c AND :d)")
    out = []
    for p in _build(where, {"a": lat - dlat, "b": lat + dlat, "c": lon - dlon, "d": lon + dlon}, year):
        if p["latitude"] is None or p["longitude"] is None:
            continue
        d = haversine_km(lat, lon, p["latitude"], p["longitude"])
        if d <= km:
            out.append({**p, "distance_km": round(d, 2)})
    out = _filter_and_sort(out, technology, status, None, None)
    out.sort(key=lambda p: p["distance_km"])
    return out[:limit] if limit else out


def plants_in_county(state: str, county: str, **kw) -> list[dict]:
    kw.setdefault("limit", None)
    return plants(state=state, county=county, **kw)


# --------------------------------------------------------------------------- queues

def latest_editions(public_only: bool = False) -> dict[str, date]:
    from sqlalchemy import text

    with _session()() as s:
        rows = s.execute(text("SELECT source, max(edition) FROM power_queue_projects "
                              + ("WHERE visibility = 'public' " if public_only else "") + "GROUP BY source")).all()
    return dict(rows)


def _queue_where(source, edition, state, county, iso, type, status, kind, public_only):
    eds =latest_editions(public_only) if edition is None else None
    where, params = [], {}
    if source:
        where.append("source = :src")
        params["src"] = source
    if edition is not None:
        where.append("edition = :ed")
        params["ed"] = edition
    else:
        pairs = [(s, e) for s, e in eds.items() if not source or s == source]
        if not pairs:
            return None, None
        where.append("(" + " OR ".join(f"(source = :s{i} AND edition = :e{i})" for i in range(len(pairs))) + ")")
        for i, (s, e) in enumerate(pairs):
            params[f"s{i}"], params[f"e{i}"] = s, e
    for col, val, key in (("state", state and state.upper(), "st"), ("upper(iso)", iso and iso.upper(), "iso"),
                          ("kind", kind, "kind"), ("lower(type)", type and type.lower(), "typ")):
        if val:
            where.append(f"{col} = :{key}")
            params[key] = val
    if status:
        sts = [status] if isinstance(status, str) else list(status)
        where.append("status = ANY(:sts)")
        params["sts"] = sts
    if county:
        where.append("lower(regexp_replace(county, '\\s+(County|Parish|Borough)$', '', 'i')) = :cty")
        params["cty"] = county_key(county)
    if public_only:
        where.append("visibility = 'public'")
    return " AND ".join(where) or "true", params


def queue_projects(source: str | None = None, edition: date | None = None, state: str | None = None,
                   county: str | None = None, iso: str | None = None, type: str | None = None,
                   status: str | None = None, kind: str | None = None, limit: int | None = 500,
                   public_only: bool = False) -> list[dict]:
    from sqlalchemy import text

    where, params = _queue_where(source, edition, state, county, iso, type, status, kind, public_only)
    if where is None:
        return []
    with _session()() as s:
        rows = s.execute(text(f"""SELECT source, edition, project_id, iso, entity, state, county, fips, kind, type,
                                         type_detail, mw, status, phase, queue_date, proposed_cod, on_date,
                                         withdrawn_date, ia_date, poi, project_name, developer, visibility
                                  FROM power_queue_projects WHERE {where}
                                  ORDER BY mw DESC NULLS LAST, project_id {'LIMIT :lim' if limit else ''}"""),
                         {**params, **({"lim": limit} if limit else {})}).all()
    return [dict(r._mapping) for r in rows]


def queued_mw_by_county(source: str | None = None, edition: date | None = None, state: str | None = None,
                        iso: str | None = None, kind: str | None = None, status: str | None = "active",
                        type: str | None = None, public_only: bool = False, limit: int | None = None) -> list[dict]:
    """[{source, edition, state, county, kind, mw, projects, by_type: {type: mw}}], largest first. Generation and
    load are separate rows (kind); sources are never summed together."""
    from sqlalchemy import text

    where, params = _queue_where(source, edition, state, None, iso, type, status, kind, public_only)
    if where is None:
        return []
    with _session()() as s:
        rows = s.execute(text(f"""SELECT source, edition, state, county, kind, type, sum(mw) AS mw, count(*) AS n
                                  FROM power_queue_projects WHERE {where} AND county IS NOT NULL
                                  GROUP BY 1, 2, 3, 4, 5, 6"""), params).all()
    agg: dict[tuple, dict] = {}
    for r in rows:
        k = (r.source, r.edition, r.state, county_key(r.county), r.kind)
        a = agg.setdefault(k, {"source": r.source, "edition": r.edition, "state": r.state, "county": r.county,
                               "kind": r.kind, "mw": 0.0, "projects": 0, "by_type": {}})
        a["mw"] += r.mw or 0.0
        a["projects"] += r.n
        t = r.type or "Unknown"
        a["by_type"][t] = round(a["by_type"].get(t, 0.0) + (r.mw or 0.0), 3)
    out = sorted(agg.values(), key=lambda a: -a["mw"])
    for a in out:
        a["mw"] = round(a["mw"], 3)
    return out[:limit] if limit else out
