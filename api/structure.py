"""Market structure: dispersion, provider relative value, hardware, alternatives, capability pricing, heatmaps.

    GET /v1/gpus                     every canonical GPU: reference data (id, name, vendor, families, vendor specs)
    GET /v1/gpus/{gpu}               one GPU's reference data
    GET /v1/markets                  one current-market row per GPU with a live listing: price, spread, score
    GET /v1/markets/{gpu}            one GPU's market: price, stats, score, per-provider best + premium + change_24h,
                                     capability pricing, alternatives, historical context
    GET /v1/markets/{gpu}/regions    per region group: cheapest + provider, median, providers, premium vs global
    ({gpu} is a GPU; a family slug is a 404 that points at /v1/families/{family})
    GET /v1/providers                every provider: meta, coverage, relative value, feed health
    GET /v1/providers/{provider}     one provider in depth
    GET /v1/heatmaps/{kind}          matrices for heatmaps (see analytics/heatmaps.KINDS)
    GET /v1/regions                  region groups and current coverage
    GET /v1/compare?a=..&b=..        GPU vs GPU or provider vs provider, side by side

Prices are observed market prices (normalized list prices); statistics derived from them are
inferred. Hardware figures are vendor peak specs. Nothing here is a quote or an execution price.
"""

from __future__ import annotations

from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Query

import analytics.rollups  # noqa: F401  (registers the market_hourly job and its AFTER_REFRESH list)
import hardware
import provider_meta
import regions
from accounts.auth import Principal, require_scope
from analytics import alternatives, capability, dispersion, heatmaps, providers
import families
import gpu_ids
from api.common import (INFERRED, OBSERVED, OBSERVED_MARKET_PRICE, cursor_page, envelope, gpu_slug, provider_slug,
                        resolve_gpu, resolve_gpu_or_family)
from providers import PROVIDERS

router = APIRouter()
READ = require_scope("data:read")
_PC = OBSERVED_MARKET_PRICE


def _known_providers() -> list[str]:
    names = set(PROVIDERS) | set(providers.feed_health()) | set(providers.provider_value_all())
    names |= {r["provider"] for r in dispersion.current_listings()}
    return sorted(n for n in names if provider_meta.is_public_provider(n))


def _resolve_provider(value: str, strict: bool = True) -> str | None:
    v = value.strip().lower()
    for p in _known_providers():
        if v in (p.lower(), provider_slug(p), provider_slug(provider_meta.meta(p).display_name)):
            return p
    if strict:
        raise HTTPException(404, f"unknown provider {value!r}; see /v1/providers")
    return None


def _gpu_only(value: str) -> str:
    """A GPU id / name / alias -> canonical name; a family is a 404 that names its own route."""
    kind, name = resolve_gpu_or_family(value)
    if kind == "family":
        raise HTTPException(404, {"code": "not_found", "param": "gpu",
                                  "message": f"{value!r} is a GPU family, not one GPU; see /v1/families/{value}"})
    return name


def gpu_object(name: str) -> dict:
    """The GPU reference object (docs/API_CONTRACT.md section 13): id, name, families, then the vendor specs."""
    spec = {k: v for k, v in (hardware.spec(name) or {}).items() if k != "name"}
    return {"id": gpu_ids.gpu_id(name), "name": name,
            "families": [families.family_slug(f) for f in families.families_for_gpu(name)], **spec}


def price_block(low, median, high) -> dict:
    return {"low": low, "median": median, "high": high, "currency": "USD", "unit": "gpu_hour"}


def _market_row(m: dict) -> dict:
    st, sc, li = m["stats"], m["score"], m["listings"]
    return {"gpu": m["gpu"], "price": price_block(m["low"], m["median"], m["high"]), "providers": m["providers"],
            "listings": {k: li[k] for k in ("live", "priced", "available", "sold_out", "availability_unknown")},
            "spread": {"abs": st.get("spread_abs"), "pct_of_low": st.get("spread_pct_of_low"),
                       "pct_of_median": st.get("spread_pct_of_median")},
            "score": {k: sc.get(k) for k in ("efficiency", "fragmentation", "label", "confidence", "reason")}}


@router.get("/v1/gpus", summary="Every canonical GPU: reference data and vendor specs")
def gpus(vendor: str | None = None, family: str | None = None, limit: int = Query(100, ge=1, le=1000),
         cursor: str | None = None, who: Principal = Depends(READ)):
    rows = [gpu_object(n) for n in sorted(hardware.all_specs())]
    if vendor:
        rows = [r for r in rows if (r.get("vendor") or "").lower() == vendor.lower()]
    if family:
        fid = families.resolve_family(family)
        rows = [r for r in rows if fid and families.family_slug(fid) in r["families"]]
    items, pg = cursor_page(rows, limit, cursor)
    return envelope(items, methodology="hardware", source="vendor peak specifications (see each entry's sources)",
                    note="theoretical vendor peak figures, dense (no sparsity); not benchmarks", **pg)


@router.get("/v1/gpus/{gpu}", summary="One GPU: reference data and vendor specs")
def gpu_detail(gpu: str, who: Principal = Depends(READ)):
    return envelope(gpu_object(_gpu_only(gpu)), methodology="hardware",
                    source="vendor peak specifications (see sources)")


@router.get("/v1/markets", summary="Current market per GPU: price, spread, efficiency score")
def markets(min_providers: int = Query(1, ge=1), vendor: str | None = None, limit: int = Query(100, ge=1, le=1000),
            cursor: str | None = None, who: Principal = Depends(READ)):
    ms = [m for m in dispersion.all_markets().values() if m["providers"] >= min_providers]
    if vendor:
        ms = [m for m in ms if ((hardware.spec(m["gpu"]) or {}).get("vendor") or "").lower() == vendor.lower()]
    ms.sort(key=lambda m: (-m["providers"], m["gpu"]))
    items, pg = cursor_page([_market_row(m) for m in ms], limit, cursor)
    return envelope(items, kind=INFERRED, methodology=dispersion.METHODOLOGY, price_concept=_PC, **pg)


@router.get("/v1/markets/{gpu}", summary="One GPU's market: price, stats, providers, capability, alternatives")
def market(gpu: str, who: Principal = Depends(READ)):
    from analytics import stats as history_stats

    name = _gpu_only(gpu)
    m = dispersion.market_now(name)
    ch = dispersion.provider_changes_24h(name)
    data = {"gpu": name, "price": price_block(m["low"], m["median"], m["high"]), "providers": m["providers"],
            "listings": m["listings"], "stats": m["stats"], "score": m["score"],
            "by_provider": [{**r, "change_24h": ch.get(r["provider"]) or {
                "pct": None, "status": "nodata", "reason": "no change record"}} for r in m["by_provider"]],
            "capability": capability.capability(name, m["low"], m["median"]),
            "alternatives": alternatives.alternatives(name),
            "context": history_stats.gpu_context(name, "on_demand")}
    return envelope(data, kind=INFERRED, methodology=dispersion.METHODOLOGY, price_concept=_PC,
                    also=["/methodology/hardware", "/methodology/provider-value", "/methodology/historical-context"],
                    change_24h="by_provider[].change_24h: observed, normalize.classify_change thresholds "
                               "(>= 0.5% and >= $0.001); null pct with a reason when not recorded 24h ago")


@router.get("/v1/markets/{gpu}/regions", summary="One GPU by region group: cheapest, median, providers, premium")
def market_regions(gpu: str, who: Principal = Depends(READ)):
    name = _gpu_only(gpu)
    glob = dispersion.market_now(name)
    by_g: dict = defaultdict(list)
    for r in dispersion.current_listings():
        if r["gpu"] == name:
            by_g[regions.region_group(r["provider"], r["region"], r["country"]) or "Unassigned"].append(r)
    out = []
    for g in (*regions.REGION_GROUPS, "Unassigned"):
        rows = by_g.get(g, [])
        votes: dict[str, float] = {}
        for r in rows:
            if r["priced"] and (r["provider"] not in votes or r["price"] < votes[r["provider"]]):
                votes[r["provider"]] = r["price"]
        st = dispersion.stats(list(votes.values()))
        cheap = min(votes, key=lambda p: (votes[p], p)) if votes else None
        reason = None if votes else (f"no live listing in {g}" if not rows else f"listings in {g}, none priced")
        out.append({
            "region_group": g, "cheapest": votes.get(cheap), "cheapest_provider": cheap, "median": st["median"],
            "providers": len(votes), "provider_names": sorted(votes),
            "live_listings": len(rows), "available_listings": sum(1 for r in rows if r["available"] is True),
            "premium_vs_global_median": (round(st["median"] / glob["median"] - 1, 6)
                                         if votes and glob["median"] else None),
            "reason": reason})
    return envelope({"gpu": name, "slug": gpu_slug(name), "global_median": glob["median"],
                     "global_providers": glob["providers"], "regions": out},
                    kind=INFERRED, methodology="dispersion", price_concept=_PC,
                    note="region groups from regions.region_group, never guessed; Unassigned = no location given or "
                         "a listing spanning several groups. median = median of each provider's lowest price in the "
                         "group; premium = group median / global median - 1")


@router.get("/v1/markets/{gpu}/dispersion", summary="Dispersion of one GPU over time")
def market_dispersion(gpu: str, days: int = Query(30, ge=1, le=365), resolution: str = Query("1d", pattern="^(1h|1d)$"),
                      who: Principal = Depends(READ)):
    name = resolve_gpu(gpu)
    return envelope(dispersion.history(name, days, resolution), kind=INFERRED, methodology=dispersion.METHODOLOGY)


def _coverage(listings: list[dict]) -> dict:
    gpus = sorted({r["gpu"] for r in listings})
    fams = sorted({(hardware.spec(g) or {}).get("architecture") for g in gpus} - {None})
    groups = sorted({regions.region_group(r["provider"], r["region"], r["country"]) for r in listings} - {None})
    return {"gpus": len(gpus), "gpu_names": gpus, "architectures": fams, "region_groups": groups,
            "live_listings": len(listings), "priced_listings": sum(1 for r in listings if r["priced"])}


def _summary_value(v: dict | None) -> dict | None:
    if v is None:
        return None
    keys = ("premium_avg", "cheapest_share", "top3_share", "availability", "volatility_daily", "samples", "reasons")
    return {k: v[k] for k in keys}


@router.get("/v1/providers", summary="Every provider: meta, coverage, relative value, feed health")
def providers_list(days: int = Query(30, ge=1, le=365), who: Principal = Depends(READ)):
    value = providers.provider_value_all(days)
    health = providers.feed_health()
    life = providers.listing_lifetime()
    by_p = defaultdict(list)
    for r in dispersion.current_listings():
        by_p[r["provider"]].append(r)
    out = []
    for p in _known_providers():
        out.append({"provider": p, "slug": provider_slug(p), "meta": provider_meta.as_dict(p),
                    "coverage": _coverage(by_p.get(p, [])),
                    "relative_value": _summary_value(value.get(p, {}).get(providers.ALL)),
                    "listing_lifetime": life.get(p), "feed_health": health.get(p),
                    "facts": providers.facts(p, value.get(p, {}), days)})
    return envelope(out, kind=INFERRED, methodology=providers.METHODOLOGY, window_days=days)


@router.get("/v1/providers/{provider}", summary="One provider: catalog, pricing vs market, rank history, health")
def provider_detail(provider: str, days: int = Query(30, ge=1, le=365), who: Principal = Depends(READ)):
    p = _resolve_provider(provider)
    value = providers.provider_value_all(days).get(p, {})
    listings = [r for r in dispersion.current_listings() if r["provider"] == p]
    markets = dispersion.all_markets()
    catalog, current = [], {}
    by_gpu = defaultdict(list)
    for r in listings:
        by_gpu[r["gpu"]].append(r)
    for g, rows in sorted(by_gpu.items()):
        mine = next((x for x in markets.get(g, {}).get("by_provider", []) if x["provider"] == p), None)
        current[g] = mine and mine["premium_vs_others_median"]
        regs = sorted({regions.region_group(p, r["region"], r["country"]) or "Unassigned" for r in rows})
        catalog.append({"gpu": g, "slug": gpu_slug(g), "listings": len(rows),
                        "priced_listings": sum(1 for r in rows if r["priced"]),
                        "available_listings": sum(1 for r in rows if r["available"] is True),
                        "best_price": mine and mine["price"], "rank_now": mine and mine["rank"],
                        "market_providers": markets.get(g, {}).get("providers", 0),
                        "market_median": markets.get(g, {}).get("median"),
                        "premium_now": current[g], "region_groups": regs,
                        "window": _summary_value(value.get(g))})
    beats, dear = providers.beats_and_expensive(value, current)
    data = {"provider": p, "meta": provider_meta.as_dict(p), "coverage": _coverage(listings), "catalog": catalog,
            "relative_value": _summary_value(value.get(providers.ALL)),
            "rank_distribution": (value.get(providers.ALL) or {}).get("rank_distribution"),
            "rank_history": providers.rank_history(p, days),
            "beats_the_market": beats, "expensive": dear,
            "listing_lifetime": providers.listing_lifetime(p).get(p),
            "feed_health": providers.feed_health().get(p), "facts": providers.facts(p, value, days)}
    return envelope(data, kind=INFERRED, methodology=providers.METHODOLOGY, window_days=days, price_concept=_PC)


@router.get("/v1/heatmaps/{kind}", summary="Heatmap matrix: rows, cols, cells (null = no data)")
def heatmap(kind: str, days: int | None = Query(None, ge=1, le=365), who: Principal = Depends(READ)):
    if kind not in heatmaps.KINDS:
        raise HTTPException(404, f"unknown heatmap {kind!r}; one of {', '.join(heatmaps.KINDS)}")
    meth = providers.METHODOLOGY if kind == "gpu-provider-availability" else dispersion.METHODOLOGY
    return envelope(heatmaps.heatmap(kind, days), kind=INFERRED, methodology=meth)


@router.get("/v1/regions", summary="Region groups and what is listed in each now")
def regions_list(who: Principal = Depends(READ)):
    acc = defaultdict(lambda: {"providers": set(), "gpus": set(), "live_listings": 0, "priced_listings": 0})
    for r in dispersion.current_listings():
        g = regions.region_group(r["provider"], r["region"], r["country"]) or "Unassigned"
        a = acc[g]
        a["providers"].add(r["provider"])
        a["gpus"].add(r["gpu"])
        a["live_listings"] += 1
        a["priced_listings"] += r["priced"]
    out = []
    for g in (*regions.REGION_GROUPS, "Unassigned"):
        a = acc.get(g)
        out.append({"region_group": g, "providers": sorted(a["providers"]) if a else [],
                    "gpus": len(a["gpus"]) if a else 0, "live_listings": a["live_listings"] if a else 0,
                    "priced_listings": a["priced_listings"] if a else 0})
    return envelope(out, kind=OBSERVED, methodology="dispersion",
                    note="Unassigned = no location given, or a listing spanning several groups")


def _gpu_side(name: str) -> dict:
    m = dispersion.market_now(name)
    return {"type": "gpu", "name": name, "slug": gpu_slug(name), "hardware": hardware.spec(name),
            "market": {k: m[k] for k in ("low", "median", "high", "providers")},
            "listings": m["listings"], "score": m["score"],
            "capability": capability.capability(name, m["low"], m["median"])["metrics"]}


def _provider_side(p: str, days: int) -> dict:
    value = providers.provider_value_all(days).get(p, {})
    listings = [r for r in dispersion.current_listings() if r["provider"] == p]
    prices = {}
    for g, m in dispersion.all_markets().items():
        mine = next((x for x in m["by_provider"] if x["provider"] == p), None)
        if mine:
            prices[g] = {"price": mine["price"], "premium_now": mine["premium_vs_others_median"], "rank_now": mine["rank"]}
    return {"type": "provider", "name": p, "meta": provider_meta.as_dict(p), "coverage": _coverage(listings),
            "relative_value": _summary_value(value.get(providers.ALL)), "prices": prices,
            "feed_health": providers.feed_health().get(p)}


@router.get("/v1/compare", summary="Side by side: two GPUs or two providers")
def compare(a: str, b: str, days: int = Query(30, ge=1, le=365), who: Principal = Depends(READ)):
    ga, gb = resolve_gpu(a, strict=False), resolve_gpu(b, strict=False)
    if ga and gb:
        sa, sb = _gpu_side(ga), _gpu_side(gb)
        return envelope({"type": "gpu", "a": sa, "b": sb,
                         "note": "different GPUs are different products; figures are side by side, not equivalences",
                         "shared_dimensions": alternatives.shared(sa["hardware"] or {}, sb["hardware"] or {},
                                                                  capability.ratio(ga, sa["market"]["median"]),
                                                                  capability.ratio(gb, sb["market"]["median"]))},
                        kind=INFERRED, methodology=dispersion.METHODOLOGY, price_concept=_PC)
    pa, pb = _resolve_provider(a, strict=False), _resolve_provider(b, strict=False)
    if pa and pb:
        sa, sb = _provider_side(pa, days), _provider_side(pb, days)
        common = sorted(set(sa["prices"]) & set(sb["prices"]))
        head = [{"gpu": g, "a": sa["prices"][g]["price"], "b": sb["prices"][g]["price"],
                 "b_vs_a": sb["prices"][g]["price"] / sa["prices"][g]["price"] - 1} for g in common]
        return envelope({"type": "provider", "a": sa, "b": sb, "common_gpus": head},
                        kind=INFERRED, methodology=providers.METHODOLOGY, window_days=days, price_concept=_PC)
    raise HTTPException(400, "a and b must both be GPUs (see /v1/gpus) or both be providers (see /v1/providers)")
