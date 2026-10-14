"""Grounded, read-only tools for the OpenGrid Assistant. Each returns a contract card payload (§5).

Every function calls an existing in-process service and returns plain data; the assistant's reply is
composed ONLY from these payloads (engine.py), and an optional LLM's reply is checked against them.

    gpu_prices(name|family)   analytics.dispersion.market_now + routing.scoring.rank_listings (CHEAPEST)
    compare(targets)          hardware.spec + market_now, one item per canonical GPU (families expanded)
    specs(name|family)        hardware.spec (+ current market low/median)
    market_context(name|fam)  analytics.stats.gpu_context + news.timeline price moves + news.store.related
    estimate(req)             intelligence.estimate.estimate        (other engineer; imported lazily)
    recommend(req, who)       intelligence.recommend.recommend      (read-only: scoring.rank_listings)

There are NO mutation tools: nothing here previews a route, issues a quote, launches, writes a row or
calls a provider. rank_listings reads compute_listings; engine.preview (which persists quotes) is never used.
"""

from __future__ import annotations

import importlib
from datetime import datetime, timedelta, timezone

from api.common import gpu_slug

MAX_VARIANTS = 6
TOP = 5
CONTEXT_DAYS = 7
NOT_CAUSAL = "Shown side by side: correlation, not causation. OpenGrid does not know why a price moved."
CAND_KEYS = ("provider", "provider_display", "gpu", "gpu_count", "region", "country", "price_per_gpu_hour",
             "price_per_instance_hour", "price_kind", "available", "observed_at", "age_seconds", "integration_level")
SPEC_KEYS = ("vendor", "architecture", "generation", "released", "segment", "vram_gb", "memory_type",
             "memory_bandwidth_tbps", "fp32_tflops", "fp16_tflops_dense", "bf16_tflops_dense", "fp8_tflops_dense",
             "fp8_supported", "interconnect", "nvlink", "form_factor", "tdp_w", "workload_class")


class ToolUnavailable(RuntimeError):
    """A dependency of a tool is not installed / not reachable; the engine says so instead of guessing."""


def expand(kind: str, name: str) -> list[str]:
    """Canonical GPU names for a reference: the GPU itself, or each variant of a family (never merged)."""
    if kind == "family":
        import families

        return families.variants(name)[:MAX_VARIANTS]
    return [name]


def _market(name: str) -> dict:
    from analytics import dispersion

    m = dispersion.market_now(name)
    lst = m.get("listings") or {}
    return {"low": m.get("low"), "median": m.get("median"), "high": m.get("high"), "providers": m.get("providers", 0),
            "listings": {k: lst.get(k) for k in ("live", "priced", "available", "availability_unknown", "sold_out")}}


def _one_price(name: str, *, max_price: float | None = None, region_group: str | None = None) -> dict:
    from routing import scoring

    r = scoring.rank_listings(name, mode="CHEAPEST", limit=TOP, max_price=max_price, region_group=region_group)
    cands = [{k: c.get(k) for k in CAND_KEYS} | {"fresh": True} for c in r["candidates"][:TOP]]
    return {"gpu": name, "slug": gpu_slug(name), "market": _market(name), "cheapest": cands,
            "candidates_total": r["candidates_total"], "exclusions_by_code": r["exclusions_by_code"],
            "max_price_per_gpu_hour": max_price, "region_group": region_group,
            "price_concept": "observed_market_price", "kind": "observed", "as_of": r["as_of"],
            "note": "observed market prices (normalized list prices), not quotes; sold-out and stale listings "
                    "are excluded"}


def gpu_prices(kind: str, name: str, *, max_price: float | None = None, region_group: str | None = None) -> dict:
    """Card `gpu_prices`: the cheapest fresh, not-sold-out listings now (GET /v1/best/{gpu}?mode=CHEAPEST)."""
    if kind == "family":
        import families

        fam = families.family(name) or {"id": name, "slug": name.lower()}
        return {"family": fam["id"], "family_slug": fam["slug"], "note": families.NOTE,
                "variants": [_one_price(v, max_price=max_price, region_group=region_group) for v in expand(kind, name)]}
    return _one_price(name, max_price=max_price, region_group=region_group)


def _spec(name: str) -> dict:
    import hardware

    s = hardware.spec(name) or {}
    return {k: s.get(k) for k in SPEC_KEYS} | {"sources": list(s.get("sources") or [])[:3]}


def compare(targets: list[tuple[str, str]]) -> dict:
    """Card `compare`: each canonical GPU side by side (hardware + market). Families list every variant."""
    items, seen = [], set()
    for kind, name in targets:
        for g in expand(kind, name):
            if g in seen or len(items) >= 8:
                continue
            seen.add(g)
            items.append({"gpu": g, "slug": gpu_slug(g), "from_family": name if kind == "family" else None,
                          "hardware": _spec(g), "market": _market(g)})
    return {"items": items, "note": "different products side by side, never treated as equivalent; hardware "
                                    "figures are vendor peak specs, prices are observed market prices",
            "kind": "observed"}


def specs(kind: str, name: str) -> dict:
    """Card `specs`: vendor peak specs (hardware.py) per canonical GPU, with its current market."""
    return {"items": [{"gpu": g, "slug": gpu_slug(g), "hardware": _spec(g), "market": _market(g)}
                      for g in expand(kind, name)],
            "family": name if kind == "family" else None,
            "note": "vendor peak specifications (see sources); prices are observed market prices"}


def _iso(v):
    return v.isoformat() if isinstance(v, datetime) else v


def _context_one(name: str, now: datetime) -> dict:
    from analytics import stats
    from news import timeline as tl

    ctx = stats.gpu_context(name)
    t1 = now
    t0 = now - timedelta(days=CONTEXT_DAYS)
    t = tl.timeline(gpu=name, t0=t0, t1=t1)
    moves = [{"at": _iso(m["at"]), "direction": m["direction"], "change_pct": m["change_pct"], "from": m["from"],
              "to": m["to"], "basis": m["basis"], "kind": "inferred",
              "provider_set_changed": bool(m.get("provider_set_changed"))}
             for m in t["price_moves"]["moves"]][-6:]
    return {"gpu": name, "slug": gpu_slug(name),
            "current": ctx.get("current"), "label": ctx.get("label"), "label_window": ctx.get("label_window"),
            "summaries": list(ctx.get("summaries") or [])[:3], "reason": ctx.get("reason"),
            "moves": moves, "move_threshold_pct": t["price_moves"]["threshold_pct"],
            "prices_unavailable": t["prices"].get("unavailable")}


def market_context(kind: str, name: str, *, now: datetime | None = None) -> dict:
    """Card `market_context`: where the price sits vs its history, recorded moves, and news in the same window.

    News titles are untrusted text from public feeds: shown, never followed, never executed."""
    from news import store

    now = now or datetime.now(timezone.utc)
    items = [_context_one(g, now) for g in expand(kind, name)]
    news = store.related(gpu=name, t0=now - timedelta(days=CONTEXT_DAYS), t1=now, limit=5)
    news = [{"title": (n.get("title") or "")[:300], "url": n.get("url"), "source": n.get("source_name"),
             "published_at": _iso(n.get("published_at")), "relevance": n.get("relevance")} for n in news]
    return {"target": name, "kind": kind, "window_days": CONTEXT_DAYS, "items": items, "news": news,
            "note": NOT_CAUSAL, "data_kinds": {"prices": "observed", "moves": "inferred", "news": "observed text"}}


# ------------------------------------------------------------------ recommendation engine (intelligence/)

def _intel(module: str):
    try:
        return importlib.import_module(f"intelligence.{module}")
    except ImportError as e:  # the engine ships separately; never guess in its place
        raise ToolUnavailable(f"the recommendation engine (intelligence.{module}) is not available: {e}") from e


def requirements(req: dict):
    """A validated WorkloadRequirements (contract §1). Raises ValueError (pydantic) on a bad shape."""
    return _intel("requirements").WorkloadRequirements.model_validate(req)


def estimate(req: dict) -> dict:
    return _intel("estimate").estimate(requirements(req))


def recommend(req: dict, who, *, limit: int = TOP) -> dict:
    rec = _intel("recommend").recommend(requirements(req), who, limit=limit)
    if isinstance(rec, dict) and isinstance(rec.get("excluded"), list):
        rec = {**rec, "excluded": rec["excluded"][:10]}
    return rec
