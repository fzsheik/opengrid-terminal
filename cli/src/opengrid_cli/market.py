"""Read-only market views shared by the CLI and the MCP server.

Each function calls PUBLIC /v1 GET endpoints (no key needed when the server has PUBLIC_PAGES on, or on a local
dev server) and returns a compact, stable dict. Prices are observed listings as reported by providers (not
quotes); runtime is never estimated here. Nothing is ranked on the client beyond simple sorting the user asked for:
rankings come from the server (/v1/best) and are labelled as such.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from .client import Client, Response

PRICE_NOTE = ("prices are observed list prices per GPU-hour as published by providers, not quotes; availability "
              "is what the provider reports and may change; runtime is unknown unless benchmarked")
HARDWARE_FIELDS = ("vendor", "architecture", "vram_gb", "memory_type", "memory_bandwidth_tbps", "bf16_tflops_dense",
                   "fp16_tflops_dense", "fp8_tflops_dense", "fp32_tflops", "form_factor", "interconnect", "nvlink",
                   "tdp_w", "workload_class", "segment", "released")
MAX_TEXT = 400


def clip(s: Any, n: int = MAX_TEXT) -> Any:
    if isinstance(s, str) and len(s) > n:
        return s[: n - 3] + "..."
    return s


def bound(items: list, n: int) -> tuple[list, bool]:
    items = list(items or [])
    return items[:n], len(items) > n


def seg(s: str) -> str:
    """One URL path segment (a GPU slug / provider / job id), escaped so it cannot change the path."""
    v = str(s).strip()
    if not v or v.strip(".") == "":
        from .client import RefusedError

        raise RefusedError(f"not a valid name: {v!r}", code="invalid_name")
    return quote(v, safe="")


def _meta(r: Response) -> dict:
    m = r.meta or {}
    return {"as_of": m.get("as_of"), "data_kind": m.get("kind"), "methodology": m.get("methodology"),
            "request_id": r.request_id}


def _hw(h: dict | None) -> dict:
    h = h or {}
    return {k: h.get(k) for k in HARDWARE_FIELDS if h.get(k) is not None}


# ---------------------------------------------------------------- search

def search_gpus(c: Client, *, model: str | None = None, vendor: str | None = None, min_vram: float | None = None,
                max_price: float | None = None, available: bool = False, sort: str = "providers",
                limit: int = 50) -> dict:
    r = c.get("/v1/gpus", params={"limit": 1000})
    rows = r.data if isinstance(r.data, list) else []
    needle = (model or "").strip().lower().replace(" ", "-")
    out = []
    for g in rows:
        h = g.get("hardware") or {}
        if needle and needle not in (g.get("slug") or "").lower() and \
                needle.replace("-", " ") not in (g.get("name") or "").lower():
            continue
        if vendor and (h.get("vendor") or "").lower() != vendor.lower():
            continue
        if min_vram is not None and (h.get("vram_gb") is None or h["vram_gb"] < min_vram):
            continue
        if max_price is not None and (g.get("low") is None or g["low"] > max_price):
            continue
        if available and not g.get("available_listings"):
            continue
        out.append({
            "slug": g.get("slug"), "name": g.get("name"), "vendor": h.get("vendor"),
            "architecture": h.get("architecture"), "vram_gb": h.get("vram_gb"), "form_factor": h.get("form_factor"),
            "low_price_per_gpu_hour": g.get("low"), "median_price_per_gpu_hour": g.get("median"),
            "high_price_per_gpu_hour": g.get("high"), "providers": g.get("providers", 0),
            "available_listings": g.get("available_listings", 0), "priced_listings": g.get("priced_listings", 0),
        })
    if sort == "price":
        out.sort(key=lambda x: (x["low_price_per_gpu_hour"] is None, x["low_price_per_gpu_hour"] or 0, x["name"] or ""))
    elif sort == "vram":
        out.sort(key=lambda x: (-(x["vram_gb"] or 0), x["name"] or ""))
    items, truncated = bound(out, limit)
    return {"items": items, "total_matched": len(out), "returned": len(items), "truncated": truncated,
            "filters": {"model": model, "vendor": vendor, "min_vram_gb": min_vram,
                        "max_price_per_gpu_hour": max_price, "available_only": available, "sort": sort},
            "price_kind": "observed", "notes": [PRICE_NOTE] + ([f"showing {len(items)} of {len(out)} matches; "
                                                                f"narrow the filters or raise the limit"]
                                                               if truncated else []),
            **_meta(r)}


# ---------------------------------------------------------------- one GPU

def gpu_specs(c: Client, gpu: str, *, max_providers: int = 10) -> dict:
    r = c.get(f"/v1/gpus/{seg(gpu)}")
    d = r.data or {}
    if (r.meta or {}).get("resolved_as") == "family" or "variants" in d:
        variants, truncated = bound(d.get("variants") or [], 25)
        return {"kind": "family", "requested": gpu, "family": d.get("name"), "slug": d.get("slug"),
                "note": clip((r.meta or {}).get("note") or d.get("note")),
                "variants": [{"slug": v.get("slug"), "gpu": v.get("gpu"), "low_price_per_gpu_hour": v.get("low"),
                              "median_price_per_gpu_hour": v.get("median"), "providers": v.get("providers"),
                              "available_listings": v.get("available_listings")} for v in variants],
                "variants_truncated": truncated, "price_kind": "observed", "notes": [PRICE_NOTE], **_meta(r)}
    m = d.get("market") or {}
    lst = m.get("listings") or {}
    by_p = sorted(m.get("by_provider") or [], key=lambda p: (p.get("price") is None, p.get("price") or 0))
    shown, truncated = bound(by_p, max_providers)
    alts, _ = bound((d.get("alternatives") or {}).get("alternatives") or [], 5)
    return {
        "kind": "gpu", "requested": gpu, "gpu": d.get("name"), "slug": d.get("slug"),
        "hardware": _hw(d.get("hardware")), "hardware_kind": "vendor peak specs (not a workload benchmark)",
        "market": {"low_price_per_gpu_hour": m.get("low"), "median_price_per_gpu_hour": m.get("median"),
                   "high_price_per_gpu_hour": m.get("high"), "providers": m.get("providers"),
                   "listings": {k: lst.get(k) for k in ("live", "priced", "available", "availability_unknown",
                                                        "sold_out")},
                   "fragmentation_label": (m.get("score") or {}).get("label")},
        "cheapest_by_provider": [{"provider": p.get("provider"), "price_per_gpu_hour": p.get("price"),
                                  "region": p.get("region"), "available": p.get("available"),
                                  "listings": p.get("listings"),
                                  "change_24h_pct": (p.get("change_24h") or {}).get("pct")} for p in shown],
        "providers_truncated": truncated,
        "alternatives": [clip(a.get("gpu") or a.get("name"), 80) for a in alts if isinstance(a, dict)],
        "price_kind": "observed", "notes": [PRICE_NOTE], **_meta(r)}


# ---------------------------------------------------------------- compare

def _side(s: dict) -> dict:
    m, lst = s.get("market") or {}, s.get("listings") or {}
    cap = s.get("capability") or {}
    cap = cap.get("metrics", cap) if isinstance(cap, dict) else {}
    per = {k: v.get("median") for k, v in cap.items() if isinstance(v, dict)}
    return {"gpu": s.get("name"), "slug": s.get("slug"), "hardware": _hw(s.get("hardware")),
            "low_price_per_gpu_hour": m.get("low"), "median_price_per_gpu_hour": m.get("median"),
            "high_price_per_gpu_hour": m.get("high"), "providers": m.get("providers"),
            "available_listings": lst.get("available"), "priced_listings": lst.get("priced"),
            "fragmentation_label": (s.get("score") or {}).get("label"),
            "median_price_per_capability_hour": {k: v for k, v in per.items() if v is not None}}


def compare_gpus(c: Client, a: str, b: str) -> dict:
    r = c.get("/v1/compare", params={"a": a.strip(), "b": b.strip()})
    d = r.data or {}
    return {"a": _side(d.get("a") or {}), "b": _side(d.get("b") or {}), "note": clip(d.get("note")),
            "shared_dimensions": d.get("shared_dimensions") or [],
            "capability_kind": "theoretical: observed price divided by vendor peak specs, not a benchmark",
            "price_kind": "observed", "notes": [PRICE_NOTE], **_meta(r)}


# ---------------------------------------------------------------- providers

def providers(c: Client, *, limit: int = 100) -> dict:
    r = c.get("/v1/providers")
    rows = r.data if isinstance(r.data, list) else []
    items, truncated = bound(rows, limit)
    out = []
    for p in items:
        meta, cov, fh = p.get("meta") or {}, p.get("coverage") or {}, p.get("feed_health") or {}
        out.append({"provider": p.get("provider"), "display_name": meta.get("display_name"),
                    "provider_class": meta.get("provider_class"), "source_type": meta.get("source_type"),
                    "gpus": cov.get("gpus"), "live_listings": cov.get("live_listings"),
                    "priced_listings": cov.get("priced_listings"), "region_groups": cov.get("region_groups") or [],
                    "last_ok_fetch": fh.get("last_ok_fetch"), "failure_rate_24h": fh.get("failure_rate_24h")})
    return {"items": out, "total": len(rows), "returned": len(out), "truncated": truncated,
            "notes": ["source_type says how prices are collected (public pricing page, API, ...)"], **_meta(r)}


# ---------------------------------------------------------------- market context + best execution

def market_context(c: Client, gpu: str, *, count: int = 1, region: str | None = None,
                   max_price: float | None = None, max_candidates: int = 5) -> dict:
    ctx = c.get(f"/v1/markets/{seg(gpu)}/context")
    best = c.get(f"/v1/best/{seg(gpu)}", params={"count": count, "region": region, "max_price": max_price,
                                                  "limit": max(1, min(max_candidates, 20))})
    cd, bd = ctx.data or {}, best.data or {}
    hist = {}
    for k in ("lowest", "median"):
        h = ((cd.get("metrics") or {}).get(k) or {}).get("historical") or {}
        if h:
            hist[k] = {"low": (h.get("low") or {}).get("value"), "high": (h.get("high") or {}).get("value"),
                       "since": h.get("since"), "samples": h.get("samples")}
    if "by_variant" in bd:  # a family: one ranking per variant
        cands = []
        for v in bd.get("by_variant") or []:
            for x in (v.get("top") or [])[:1]:
                cands.append({**x, "variant": v.get("slug")})
    else:
        cands = bd.get("candidates") or []
    shown, truncated = bound(cands, max_candidates)
    return {
        "gpu": cd.get("gpu") or bd.get("gpu") or bd.get("family"), "requested": gpu,
        "current": cd.get("current"), "label": cd.get("label"), "label_reason": clip(cd.get("label_reason")),
        "summaries": [clip(s) for s in (cd.get("summaries") or [])[:5]],
        "historical": hist,
        "best_available_now": [{
            "rank": x.get("rank"), "provider": x.get("provider"), "gpu": x.get("gpu"), "variant": x.get("variant"),
            "region": x.get("region"), "gpu_count": x.get("gpu_count"),
            "price_per_gpu_hour": x.get("price_per_gpu_hour"), "price_per_instance_hour": x.get("price_per_instance_hour"),
            "available": x.get("available"), "observed_at": x.get("observed_at"), "age_seconds": x.get("age_seconds"),
            "provisionable_by_opengrid": x.get("provisionable"), "score": x.get("score"),
            "explanation": clip(x.get("explanation"))} for x in shown],
        "candidates_total": bd.get("candidates_total", len(cands)), "candidates_truncated": truncated,
        "exclusions_by_code": bd.get("exclusions_by_code") or {}, "ranking_mode": bd.get("mode"),
        "ranking_kind": "inferred from observed prices (GET /v1/best), not a quote",
        "price_kind": "observed", "notes": [PRICE_NOTE],
        "as_of": (best.meta or {}).get("as_of"), "request_id": best.request_id}
