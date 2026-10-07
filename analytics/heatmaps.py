"""Heatmap matrices for the front end: {rows, cols, cells: [[value|null]], meta}.

A null cell means no data (not zero). Kinds:
    gpu-provider-premium        current: provider's lowest / median(other providers' lowest) - 1
    gpu-provider-availability   window: share of tracked hours the provider had a priced listing
    gpu-provider-change24h      provider's lowest at the latest rollup hour vs 24 hours earlier, as a fraction
    gpu-region-cheapest         current: cheapest priced listing in the region group (USD/GPU-hour)
    gpu-region-premium          current: median of provider lowest prices in the region / global median - 1
    gpu-region-availability     current: number of priced listings in the region group
    gpu-time-volatility         per day: stdev of hourly log changes of the market median (>= 12 changes)
    gpu-time-availability       per day: provider-hours priced / provider-hours tracked
    gpu-time-change             per day: market median close vs previous day's close, as a fraction
Region columns include "Unassigned" (no single known region group) so nothing is silently dropped.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

import normalize
import regions
from analytics import dispersion, providers, rollups

KINDS = ("gpu-provider-premium", "gpu-provider-availability", "gpu-provider-change24h",
         "gpu-region-cheapest", "gpu-region-premium", "gpu-region-availability",
         "gpu-time-volatility", "gpu-time-availability", "gpu-time-change")
UNASSIGNED = "Unassigned"


def _matrix(cells: dict[tuple, float | None], rows: list, cols: list, **meta) -> dict:
    return {"rows": rows, "cols": cols,
            "cells": [[cells.get((r, c)) for c in cols] for r in rows], "meta": meta}


def gpu_provider(kind: str, days: int = 7) -> dict:
    cells = {}
    if kind == "gpu-provider-premium":
        for g, m in dispersion.all_markets().items():
            for p in m["by_provider"]:
                cells[(g, p["provider"])] = p["premium_vs_others_median"]
        meta = {"unit": "fraction", "basis": "current", "definition": "own lowest / median(other providers' lowest) - 1"}
    elif kind == "gpu-provider-availability":
        for p, by in providers.provider_value_all(days).items():
            for g, v in by.items():
                if g != providers.ALL:
                    cells[(g, p)] = v["availability"]
        meta = {"unit": "fraction", "basis": f"last {days} days",
                "definition": "hours with a priced listing / hours tracked", "min_hours": providers.MIN_HOURS}
    else:
        last = rollups.coverage().get("last")
        meta = {"unit": "fraction", "basis": "latest rollup hour vs 24h earlier"}
        if last is not None:
            now_rows = rollups.provider_hourly(t0=last, t1=last)
            then = {(r["gpu"], r["provider"]): r["min_price"]
                    for r in rollups.provider_hourly(t0=last - timedelta(hours=24), t1=last - timedelta(hours=24))}
            for r in now_rows:
                a = then.get((r["gpu"], r["provider"]))
                if a and r["min_price"]:
                    cells[(r["gpu"], r["provider"])] = r["min_price"] / a - 1
            meta["hour"] = last.isoformat()
    rows = sorted({k[0] for k in cells})
    cols = sorted({k[1] for k in cells})
    return _matrix(cells, rows, cols, kind=kind, **meta)


def gpu_region(kind: str) -> dict:
    by: dict[tuple, list] = defaultdict(list)
    for r in dispersion.current_listings():
        if r["priced"]:
            g = regions.region_group(r["provider"], r["region"], r["country"]) or UNASSIGNED
            by[(r["gpu"], g)].append(r)
    markets = dispersion.all_markets()
    cells = {}
    for (gpu, grp), rows in by.items():
        if kind == "gpu-region-cheapest":
            cells[(gpu, grp)] = min(x["price"] for x in rows)
        elif kind == "gpu-region-availability":
            cells[(gpu, grp)] = len(rows)
        else:
            best: dict[str, float] = {}
            for x in rows:
                best[x["provider"]] = min(best.get(x["provider"], x["price"]), x["price"])
            gm = markets.get(gpu, {}).get("median")
            cells[(gpu, grp)] = statistics.median(best.values()) / gm - 1 if gm else None
    meta = {"gpu-region-cheapest": {"unit": "USD per GPU-hour"},
            "gpu-region-availability": {"unit": "priced listings"},
            "gpu-region-premium": {"unit": "fraction",
                                   "definition": "median of provider lowest prices in region / global median - 1"}}[kind]
    cols = [g for g in (*regions.REGION_GROUPS, UNASSIGNED) if any(k[1] == g for k in cells)]
    return _matrix(cells, sorted({k[0] for k in cells}), cols, kind=kind, basis="current", **meta)


def gpu_time(kind: str, days: int = 30, segment: str = "on_demand") -> dict:
    d0 = (datetime.now(timezone.utc) - timedelta(days=days)).date()
    sql = text("SELECT * FROM structure_gpu_daily WHERE segment = :s AND day >= :d0 ORDER BY gpu, day")
    with normalize.SessionLocal() as s:
        rows = s.execute(sql, {"s": segment, "d0": d0}).all()
    cells, prev = {}, {}
    for r in rows:
        key = (r.gpu, r.day.isoformat())
        if kind == "gpu-time-volatility":
            cells[key] = r.median_vol
        elif kind == "gpu-time-availability":
            cells[key] = r.provider_hours_priced / r.provider_hours_tracked if r.provider_hours_tracked else None
        else:
            p = prev.get(r.gpu)
            if p is not None and r.close_median is not None and (r.day - p[0]).days == 1 and p[1]:
                cells[key] = float(r.close_median) / float(p[1]) - 1
            prev[r.gpu] = (r.day, r.close_median)
    day_cols = [(d0 + timedelta(days=i)).isoformat() for i in range(days + 1)]
    unit = {"gpu-time-volatility": "stdev of hourly log changes", "gpu-time-availability": "fraction",
            "gpu-time-change": "fraction"}[kind]
    return _matrix(cells, sorted({k[0] for k in cells}), day_cols, kind=kind, unit=unit, basis=f"last {days} days")


def heatmap(kind: str, days: int | None = None) -> dict:
    if kind.startswith("gpu-provider-"):
        return gpu_provider(kind, days or 7)
    if kind.startswith("gpu-region-"):
        return gpu_region(kind)
    return gpu_time(kind, days or 30)
