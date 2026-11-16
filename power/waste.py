"""Wasted energy a small datacenter could use (methodology/power-waste.md): renewable output the grid threw away
(curtailment, stored as power_series in power_days) and natural gas flared at oil and gas sites (power_flare_*).
Every read here is internal: the sources' terms are judged in power/registry.py (curtailment) and
power/flaring.py (flaring), and the routes are admin-only.

    curtailment(since, until, iso=None, fuel=None)       per region, reason and day: curtailed MWh, and the
                                                          curtailed share of potential output where output is known
    curtailed_mwh(iso, since, until, fuel=None)           one region's window total (plain dict, for site ranking)
    flare_sites(country=, state=, year=, limit=)          flare sites ranked by volume, with coordinates
    flared_near(lat, lon, radius_km, year=None)           per source, the volume flared within a radius of a point
    flared_by_county(state=None, period=None)             regulator volumes by county (or other area)

Share curtailed = curtailed / (curtailed + output): the part of what the wind and solar fleet could have produced
that was thrown away. CAISO's read-me says curtailment is not counted in its production figures, so the two add up.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, timedelta

from power import storage

NOTE = ("Internal: curtailment and flaring data are read under terms that do not allow republishing them; for "
        "OpenGrid's own site ranking only.")
EARTH_KM = 6371.0088


def _session():
    return storage._session()


# --------------------------------------------------------------------------- curtailment

def _daily(since: date, until: date, iso: str | None, kinds: tuple[str, ...]) -> list:
    from sqlalchemy import text

    with _session()() as s:
        return s.execute(text("""
            SELECT p.key, p.source, p.iso, p.kind, p.location, p.meta, p.visibility, d.day, d.n, d.complete,
                   (SELECT sum(x) FROM unnest(d.vals) x) AS total
            FROM power_days d JOIN power_series p ON p.id = d.series_id
            WHERE p.kind = ANY(:kinds) AND d.day BETWEEN :a AND :b
              AND (CAST(:iso AS text) IS NULL OR upper(p.iso) = upper(:iso))
            ORDER BY p.iso, p.key, d.day"""), {"kinds": list(kinds), "a": since, "b": until, "iso": iso}).all()


def _window(since: date | None, until: date | None, days: int = 30) -> tuple[date, date]:
    until = until or date.today()
    return since or until - timedelta(days=days - 1), until


def _output_by_fuel(rows) -> dict[tuple[str, str, date], float]:
    """{(iso, fuel, day): MWh}: the production-workbook series where it exists, else telemetry (they differ in level;
    power/sources/caiso_curtailment.py), else any generation series of that fuel."""
    best: dict[tuple[str, str, date], tuple[int, float]] = {}
    rank = {"production": 0, "telemetry": 1}
    for r in rows:
        if r.kind != "generation" or r.total is None:
            continue
        meta = r.meta or {}
        k = (r.iso, meta.get("fuel"), r.day)
        p = rank.get(meta.get("basis"), 2)
        if k not in best or p < best[k][0]:
            best[k] = (p, float(r.total))
    return {k: v for k, (_, v) in best.items()}


def curtailment(since: date | None = None, until: date | None = None, iso: str | None = None,
                fuel: str | None = None) -> list[dict]:
    """One row per (series, day): curtailed MWh by region, fuel and reason; the all-reasons rows also carry the
    fuel's output and the curtailed share where the output is known."""
    since, until = _window(since, until)
    rows = _daily(since, until, iso, ("curtailment", "generation"))
    output = _output_by_fuel(rows)
    out = []
    for r in rows:
        if r.kind != "curtailment":
            continue
        meta = r.meta or {}
        if fuel and meta.get("fuel") != fuel:
            continue
        d = {"day": r.day, "iso": r.iso, "series": r.key, "source": r.source, "fuel": meta.get("fuel"),
             "scope": meta.get("scope"), "type": meta.get("type"), "measure": meta.get("measure", "curtailment"),
             "curtailed_mwh": None if r.total is None else round(float(r.total), 3), "hours": r.n,
             "complete": r.complete}
        if not meta.get("scope") and not meta.get("type"):
            g = output.get((r.iso, meta.get("fuel"), r.day))
            d["output_mwh"] = None if g is None else round(g, 3)
            d["share_curtailed"] = (round(r.total / (r.total + g), 5)
                                    if g is not None and r.total is not None and r.total + g > 0 else None)
        out.append(d)
    return out


def curtailed_mwh(iso: str, since: date, until: date, fuel: str | None = None) -> dict:
    """A region's curtailed MWh over a window (all reasons; wind + solar unless `fuel`), with the output and share
    over the days where both are known. For the site-ranking work: a plain dict."""
    rows = [r for r in curtailment(since, until, iso, fuel) if not r["scope"] and not r["type"]
            and r["measure"] == "curtailment"]
    total = sum(r["curtailed_mwh"] or 0 for r in rows)
    both = [r for r in rows if r.get("output_mwh") is not None and r["curtailed_mwh"] is not None]
    c, g = sum(r["curtailed_mwh"] for r in both), sum(r["output_mwh"] for r in both)
    by_fuel: dict[str, float] = defaultdict(float)
    for r in rows:
        by_fuel[r["fuel"]] += r["curtailed_mwh"] or 0
    return {"iso": iso.upper(), "since": since, "until": until, "fuel": fuel, "days": len({r["day"] for r in rows}),
            "curtailed_mwh": round(total, 3), "by_fuel": {k: round(v, 3) for k, v in sorted(by_fuel.items())},
            "output_mwh_same_days": round(g, 3) if both else None,
            "share_curtailed": round(c / (c + g), 5) if both and c + g > 0 else None}


# --------------------------------------------------------------------------- flaring

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_KM * math.asin(min(1.0, math.sqrt(a)))


_YEAR = """
    WITH latest AS (SELECT f.source, max(v.year) AS year FROM power_flare_volumes v
                    JOIN power_flare_sites f ON f.id = v.site_id GROUP BY f.source)
"""


def flare_sites(country: str | None = None, state: str | None = None, year: int | None = None,
                limit: int = 100, source: str | None = None) -> list[dict]:
    """Flare sites ranked by flared volume in `year` (default: each source's latest year; North Dakota's latest
    year may be partial: months_reported), with coordinates. Sources are not merged: a North Dakota section and a
    satellite cell can be the same flares, measured twice."""
    from sqlalchemy import text

    with _session()() as s:
        rows = s.execute(text(_YEAR + """
            SELECT f.id, f.source, f.site_key, f.name, f.country, f.country_name, f.state, f.county, f.lat, f.lon,
                   f.kind, f.visibility, v.year, v.volume_m3, v.volume_mcf, v.months_reported, v.detections,
                   v.energy_mwh_derived
            FROM power_flare_sites f JOIN power_flare_volumes v ON v.site_id = f.id
            JOIN latest l ON l.source = f.source
            WHERE v.year = coalesce(CAST(:y AS integer), l.year)
              AND (CAST(:c AS text) IS NULL OR upper(f.country) = upper(:c) OR upper(f.country_name) = upper(:c))
              AND (CAST(:st AS text) IS NULL OR upper(f.state) = upper(:st))
              AND (CAST(:src AS text) IS NULL OR f.source = :src)
            ORDER BY v.volume_m3 DESC NULLS LAST LIMIT :n"""),
                         {"y": year, "c": country, "st": state, "src": source, "n": limit}).all()
    return [{"site_id": r.id, "source": r.source, "site_key": r.site_key, "name": r.name, "country": r.country,
             "country_name": r.country_name, "state": r.state, "county": r.county, "lat": r.lat, "lon": r.lon,
             "kind": r.kind, "year": r.year, "months_reported": r.months_reported, "detections": r.detections,
             "volume_bcm": None if r.volume_m3 is None else round(r.volume_m3 / 1e9, 6),
             "volume_mmcf": None if r.volume_mcf is None else round(r.volume_mcf / 1e3, 3),
             "energy_mwh_derived": r.energy_mwh_derived, "visibility": r.visibility} for r in rows]


def flared_near(lat: float, lon: float, radius_km: float = 50.0, year: int | None = None,
                source: str | None = None) -> dict:
    """The flare sites within radius_km of a point: per source, the year's flared volume and derived energy
    (default: each source's latest year), and the nearest sites. Never summed across sources (the same flare can
    be in two)."""
    from sqlalchemy import text

    dlat = radius_km / 111.0
    dlon = radius_km / max(1e-6, 111.0 * math.cos(math.radians(lat)))
    with _session()() as s:
        rows = s.execute(text(_YEAR + """
            SELECT f.id, f.source, f.name, f.site_key, f.lat, f.lon, f.country, f.state, f.county, v.year,
                   v.volume_m3, v.months_reported, v.energy_mwh_derived
            FROM power_flare_sites f JOIN power_flare_volumes v ON v.site_id = f.id
            JOIN latest l ON l.source = f.source
            WHERE v.year = coalesce(CAST(:y AS integer), l.year) AND f.lat BETWEEN :la AND :lb
              AND f.lon BETWEEN :oa AND :ob AND (CAST(:src AS text) IS NULL OR f.source = :src)"""),
                         {"y": year, "src": source, "la": lat - dlat, "lb": lat + dlat, "oa": lon - dlon,
                          "ob": lon + dlon}).all()
    near, by = [], {}
    for r in rows:
        km = haversine_km(lat, lon, r.lat, r.lon)
        if km > radius_km:
            continue
        bcm = (r.volume_m3 or 0) / 1e9
        near.append({"site_id": r.id, "source": r.source, "site_key": r.site_key, "name": r.name,
                     "km": round(km, 2), "lat": r.lat, "lon": r.lon, "country": r.country, "state": r.state,
                     "county": r.county, "year": r.year, "volume_bcm": round(bcm, 6),
                     "energy_mwh_derived": r.energy_mwh_derived})
        b = by.setdefault(r.source, {"year": r.year, "sites": 0, "volume_bcm": 0.0, "energy_mwh_derived": 0.0,
                                     "months_reported": r.months_reported})
        b["sites"] += 1
        b["volume_bcm"] += bcm
        b["energy_mwh_derived"] += r.energy_mwh_derived or 0
    for b in by.values():
        b["volume_bcm"] = round(b["volume_bcm"], 6)
        b["energy_mwh_derived"] = round(b["energy_mwh_derived"], 1)
        b["average_mw_derived"] = round(b["energy_mwh_derived"] / 8760, 2)
    return {"lat": lat, "lon": lon, "radius_km": radius_km, "by_source": by,
            "nearest": sorted(near, key=lambda x: x["km"])[:20]}


def flared_by_county(state: str | None = None, period: date | None = None, period_type: str = "month",
                     source: str | None = None, limit: int = 500) -> list[dict]:
    """Regulator volumes by area (county, or the source's own area), ranked; default: each source's latest period."""
    from sqlalchemy import text

    with _session()() as s:
        rows = s.execute(text("""
            WITH latest AS (SELECT source, max(period) AS period FROM power_flare_areas
                            WHERE period_type = :pt GROUP BY source)
            SELECT a.source, a.area_type, a.country, a.state, a.area, a.period, a.period_type, a.volume_mcf,
                   a.energy_mwh_derived, a.visibility, a.meta
            FROM power_flare_areas a JOIN latest l ON l.source = a.source
            WHERE a.period_type = :pt AND a.period = coalesce(CAST(:p AS date), l.period)
              AND (CAST(:st AS text) IS NULL OR upper(a.state) = upper(:st))
              AND (CAST(:src AS text) IS NULL OR a.source = :src)
            ORDER BY a.volume_mcf DESC NULLS LAST LIMIT :n"""),
                         {"pt": period_type, "p": period, "st": state, "src": source, "n": limit}).all()
    return [{"source": r.source, "area_type": r.area_type, "country": r.country, "state": r.state, "area": r.area,
             "period": r.period, "period_type": r.period_type, "volume_mcf": r.volume_mcf,
             "energy_mwh_derived": r.energy_mwh_derived, "visibility": r.visibility, **(r.meta or {})} for r in rows]
