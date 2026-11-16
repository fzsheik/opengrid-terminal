"""Stock counts per GPU type: one table per canonical GPU of what every provider says is free now. ADMIN ONLY.

    GET /v1/ops/availability/counts?gpu=...   one table for that GPU; without `gpu`, one per GPU seen in capacity
                                             or listings, most GPUs known free first

Built on read from capacity_current (every row, internal ones included: this is an admin read) and compute_listings
(only to name the providers that price the GPU but report no stock for it). Nothing is stored; `table()` is pure.

Per provider and region, one line:
    shape        count | tier | yes-no | none (rows exist, every state unknown)
    gpus_free    the provider's count in GPUs: GPUs as stated, or instances x GPUs per instance (`gpu_count`)
    gpus_total   the same for the provider's total, where it gives one
    unit         the provider's own unit (gpu | instance); sizes = GPUs per instance in the rows
    figure       stock (a count of what is free now) | estimate (Salad's own estimate of online nodes) for counts;
                 level | likelihood (AWS's spot placement score) for tiers. A cap ("most GPUs one order can take")
                 is never stored as a count (methodology/capacity.md), so it cannot reach this table.
    combine      how several rows of one line became one number. Rows that draw on one pool (one region's 1x / 2x
                 / 4x sizes of the same cards; one SimplePod host's offers; Salad's cumulative priorities) are never
                 added: the line takes the largest of them in GPUs and says `at_least` (a floor, since that many
                 GPUs are free in at least one shape). Rows that are different machines (ADDITIVE) are added.
    age_seconds  since the oldest row of the line was read; `stale` after three missed capacity reads

The summary adds up gpus_free over count lines only, never a yes/no or a tier (unknown stays unknown), and leaves
out resellers of other clouds' capacity (RESOLD), which it reports apart so nothing is counted twice.
Methodology: methodology/capacity.md ("Stock counts per GPU").
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Iterable

from fastapi import APIRouter, Depends
from sqlalchemy import select

import capacity
import normalize
from accounts.auth import Principal, require_scope
from api.common import OBSERVED, envelope, resolve_gpu
from providers import PROVIDERS
from store.capacity import CapacityCurrent
from tables import ComputeListingRow

router = APIRouter()
admin = require_scope("admin")

# Rows of these providers are different machines even within one GPU and region (Lium's machine types per city,
# Latitude's plans, Hydra Host's devices, one row per GPU model for Vast / Clore / Akash, one row per market for
# Nosana), so their counts add up. Every other provider's rows in one line are taken as one pool.
ADDITIVE = frozenset({"akash", "clore", "hydrahost", "latitude", "lium", "nosana", "vast"})
# Resold capacity of clouds OpenGrid also reads directly: shown, never in the summed total.
RESOLD = frozenset({"gpuai", "huggingface"})
ESTIMATES = frozenset({"salad"})
LIKELIHOODS = frozenset({"aws"})
STALE_AFTER_READS = 3  # the same rule as /v1/capacity's `stale`
TIER_ORDER = {"low": 1, "medium": 2, "high": 3}
_HOST = re.compile(r"\bhost=([^;\s]+)")


def _gpus(count: int | None, unit: str | None, per_instance: int) -> int | None:
    """A count in GPUs, or None when the unit is not stated."""
    if count is None:
        return None
    if unit == "gpu":
        return count
    if unit == "instance":
        return count * per_instance
    return None


def _pool(provider: str, row) -> str:
    """Rows with the same pool key draw on the same cards; different keys are different machines."""
    if provider in ADDITIVE:
        return row.sku
    if provider == "simplepod":
        hit = _HOST.search(row.raw_value or "")
        return f"host:{hit.group(1)}" if hit and hit.group(1) != "None" else row.sku
    return ""


def _combine(provider: str) -> str:
    if provider in ADDITIVE:
        return "sum: different machines"
    if provider == "simplepod":
        return "largest per host, summed over hosts: one host's offers share its cards"
    return "largest: the rows share one pool"


def _state(values: list[bool | None]) -> bool | None:
    if any(v is True for v in values):
        return True
    if values and all(v is False for v in values):
        return False
    return None


def _line(provider: str, region: str | None, rows: list, now: datetime) -> dict:
    detail, pools, totals, members = [], {}, {}, {}
    for r in sorted(rows, key=lambda r: (r.gpu_count, r.sku)):
        free, total = _gpus(r.available_count, r.count_unit, r.gpu_count), _gpus(r.total_count, r.count_unit, r.gpu_count)
        detail.append({"sku": r.sku, "gpu_count": r.gpu_count, "in_stock": r.in_stock, "tier": r.tier,
                       "available_count": r.available_count, "total_count": r.total_count, "count_unit": r.count_unit,
                       "gpus_free": free, "gpus_total": total, "raw_value": r.raw_value})
        key = _pool(provider, r)
        if free is not None:
            pools[key] = max(pools.get(key, 0), free)
            members[key] = members.get(key, 0) + 1
        if total is not None:
            totals[key] = max(totals.get(key, 0), total)
    counted = [r for r in rows if r.available_count is not None]
    tiers = [r.tier for r in rows if r.tier]
    shape = ("count" if counted else "tier" if tiers else
             "yes-no" if any(r.in_stock is not None for r in rows) else "none")
    units = sorted({r.count_unit for r in counted if r.count_unit})
    oldest = min(r.observed_at for r in rows)
    cls = PROVIDERS.get(provider)
    interval = cls.polling.capacity_seconds if cls else 900
    figure = None
    if shape == "count":
        figure = "estimate" if provider in ESTIMATES else "stock"
    elif shape == "tier":
        figure = "likelihood" if provider in LIKELIHOODS else "level"
    # A pool read from several shapes gives its largest, which is a floor on the GPUs free in it; all zero is exact.
    shared = any(n > 1 and pools[k] > 0 for k, n in members.items())
    return {
        "provider": provider, "region": region, "shape": shape,
        "unit": units[0] if len(units) == 1 else (units or None),
        "sizes": sorted({r.gpu_count for r in rows}),
        "gpus_free": sum(pools.values()) if pools else None,
        "gpus_total": sum(totals.values()) if totals else None,
        "figure": figure,
        "combine": _combine(provider) if len(counted) > 1 else None,
        "at_least": bool(shared),
        "in_stock": _state([r.in_stock for r in rows]),
        "tier": max(tiers, key=TIER_ORDER.get) if tiers else None,
        "observed_at": oldest, "age_seconds": int((now - oldest).total_seconds()),
        "stale": (now - oldest).total_seconds() > STALE_AFTER_READS * interval,
        "resold": provider in RESOLD,
        "visibility": "internal" if any(getattr(r, "visibility", "public") == "internal" for r in rows) else "public",
        "rows": detail,
    }


def table(gpu: str, rows: Iterable, listed: dict[str, set[str | None]] | None = None,
          now: datetime | None = None, supported: Iterable[str] | None = None) -> dict:
    """One GPU's table from capacity rows (CapacityCurrent or CapacityObservation) of that GPU.

    `listed`: provider -> regions where the provider prices this GPU, to name the providers with no stock row.
    `supported`: providers that have a capacity feed at all (capacity.supported()).
    """
    now = now or datetime.now(timezone.utc)
    supported = set(capacity.supported() if supported is None else supported)
    groups: dict[tuple[str, str | None], list] = {}
    for r in rows:
        if r.canonical_gpu_name == gpu:
            groups.setdefault((r.provider, r.region), []).append(r)
    lines = [_line(p, region, rs, now) for (p, region), rs in groups.items()]
    shape_rank = {"count": 0, "tier": 1, "yes-no": 2, "none": 3}
    lines.sort(key=lambda l: (shape_rank[l["shape"]], -(l["gpus_free"] or 0), l["provider"], l["region"] or ""))

    by_provider: dict[str, list[dict]] = {}
    for line in lines:
        by_provider.setdefault(line["provider"], []).append(line)
    gaps = [{"provider": p, "regions": sorted(r for r in regions if r) or None,
             "reason": "no stock feed" if p not in supported else "its stock feed has no row for this GPU"}
            for p, regions in sorted((listed or {}).items()) if p not in by_provider]

    with_counts, in_stock, out_of_stock, unknown = [], [], [], []
    for p, ls in sorted(by_provider.items()):
        if any(l["shape"] == "count" for l in ls):
            with_counts.append(p)
        elif (state := _state([l["in_stock"] for l in ls])) is True:
            in_stock.append(p)
        elif state is False:
            out_of_stock.append(p)
        else:
            unknown.append(p)
    counted = [l for l in lines if l["shape"] == "count" and l["gpus_free"] is not None]
    free = sum(l["gpus_free"] for l in counted if not l["resold"])
    resold = sum(l["gpus_free"] for l in counted if l["resold"])
    floor = any(l["at_least"] for l in counted if not l["resold"])
    direct = [p for p in with_counts if p not in RESOLD]
    unknown_all = unknown + [g["provider"] for g in gaps]
    line = (f"{gpu}: {'at least ' if floor else ''}{free} GPUs known free at {len(direct)} provider(s) with counts"
            f"{f' (+{resold} resold)' if resold else ''}; {len(in_stock)} more in stock by yes/no or tier, "
            f"{len(out_of_stock)} out of stock; {len(unknown_all)} unknown ({len(gaps)} with no stock row)")
    return {
        "gpu": gpu,
        "summary": {
            "line": line, "gpus_free_known": free, "at_least": floor, "resold_gpus_free_known": resold,
            "providers_with_counts": direct, "resold_providers_with_counts": [p for p in with_counts if p in RESOLD],
            "providers_in_stock_no_count": in_stock, "providers_out_of_stock_no_count": out_of_stock,
            "providers_unknown": unknown_all,
        },
        "lines": lines,
        "no_stock_feed": gaps,
    }


def tables(rows: list, listed: dict[str, dict[str, set[str | None]]], now: datetime | None = None,
           supported: Iterable[str] | None = None) -> list[dict]:
    """Every GPU's table: GPUs from capacity rows and from listings; rows with no canonical GPU are left out."""
    now = now or datetime.now(timezone.utc)
    supported = list(capacity.supported() if supported is None else supported)
    gpus = {r.canonical_gpu_name for r in rows if r.canonical_gpu_name} | {g for g in listed if g}
    out = [table(g, rows, listed.get(g), now, supported) for g in gpus]
    return sorted(out, key=lambda t: (-t["summary"]["gpus_free_known"], t["gpu"]))


def _load(gpu: str | None) -> tuple[list, dict[str, dict[str, set[str | None]]]]:
    cq = select(CapacityCurrent)
    lq = select(ComputeListingRow.canonical_gpu_name, ComputeListingRow.provider, ComputeListingRow.region).distinct()
    if gpu:
        cq = cq.where(CapacityCurrent.canonical_gpu_name == gpu)
        lq = lq.where(ComputeListingRow.canonical_gpu_name == gpu)
    with normalize.SessionLocal() as s:
        rows = list(s.scalars(cq))
        listed: dict[str, dict[str, set[str | None]]] = {}
        for g, p, region in s.execute(lq):
            if g:
                listed.setdefault(g, {}).setdefault(p, set()).add(region)
    return rows, listed


@router.get("/v1/ops/availability/counts", tags=["ops"], include_in_schema=False,
            summary="Ops: GPUs free now per GPU type, provider and region, in GPUs where the provider counts (internal)")
def counts(gpu: str | None = None, who: Principal = Depends(admin)):
    name = resolve_gpu(gpu) if gpu else None
    rows, listed = _load(name)
    now = datetime.now(timezone.utc)
    if name:
        data = [table(name, rows, listed.get(name), now)]
    else:
        data = tables(rows, listed, now)
    return envelope(data, kind=OBSERVED, methodology="capacity", visibility="internal", count=len(data),
                    unmapped_rows=sum(1 for r in rows if not r.canonical_gpu_name),
                    note="each provider's own report; counts converted to GPUs where the instance size is stated; "
                         "yes/no and tiers are never turned into numbers")
