"""Alternatives to a GPU: other GPUs that share a stated dimension with it.

Never "equivalent": two different GPUs are different products. An alternative is
listed with WHICH dimensions it shares and the deltas, and the buyer decides.

Dimensions (from hardware.spec; a GPU missing the figure cannot share it):
    similar_vram          VRAM within +/- VRAM_BAND of the target's
    same_architecture     same architecture (e.g. Hopper)
    same_generation       same generation family but a different architecture (Blackwell vs Blackwell Ultra)
    training_oriented     both classed 'training' or 'training+inference'
    inference_oriented    both classed 'inference', 'training+inference' or 'graphics/inference'
    similar_price_performance   current median $/dense-BF16-TFLOP-hour within +/- PRICE_PERF_BAND

MIG slices are partitions of a card, not GPUs, and are never offered as alternatives
to whole GPUs (nor whole GPUs to slices).
"""

from __future__ import annotations

import hardware
from analytics import capability, dispersion

VRAM_BAND = 0.25
PRICE_PERF_BAND = 0.25
TRAINING = {"training", "training+inference"}
INFERENCE = {"inference", "training+inference", "graphics/inference"}


def shared(a: dict, b: dict, pp_a: float | None, pp_b: float | None) -> list[str]:
    """Dimensions specs a and b share. Pure."""
    dims = []
    if a.get("vram_gb") and b.get("vram_gb") and abs(b["vram_gb"] - a["vram_gb"]) <= VRAM_BAND * a["vram_gb"]:
        dims.append("similar_vram")
    if a.get("architecture") and a["architecture"] == b.get("architecture"):
        dims.append("same_architecture")
    elif a.get("generation") and a["generation"] == b.get("generation"):
        dims.append("same_generation")
    if a.get("workload_class") in TRAINING and b.get("workload_class") in TRAINING:
        dims.append("training_oriented")
    if a.get("workload_class") in INFERENCE and b.get("workload_class") in INFERENCE:
        dims.append("inference_oriented")
    if pp_a and pp_b and abs(pp_b / pp_a - 1) <= PRICE_PERF_BAND:
        dims.append("similar_price_performance")
    return dims


def _delta(a, b):
    return None if a is None or b is None or a == 0 else b / a - 1


def alternatives(gpu: str, priced_only: bool = True, limit: int = 30) -> dict:
    target = hardware.spec(gpu)
    markets = dispersion.all_markets()
    tm = markets.get(gpu) or dispersion.market_now(gpu)
    out = {"gpu": gpu, "note": "alternatives share the listed dimensions; they are not equivalent products",
           "target": {"low": tm["low"], "median": tm["median"], "providers": tm["providers"],
                      "available_listings": tm["listings"]["available"]},
           "alternatives": []}
    if target is None:
        out["reason"] = "no hardware entry for this GPU"
        return out
    is_slice = target.get("form_factor") == "MIG slice"
    pp_t = capability.ratio(gpu, tm["median"])
    for name, sp in hardware.all_specs().items():
        if name == gpu or (sp.get("form_factor") == "MIG slice") != is_slice:
            continue
        m = markets.get(name)
        if priced_only and (m is None or m["low"] is None):
            continue
        med = m["median"] if m else None
        dims = shared(target, sp, pp_t, capability.ratio(name, med))
        if not dims:
            continue
        out["alternatives"].append({
            "gpu": name, "shares": dims, "architecture": sp["architecture"], "vram_gb": sp["vram_gb"],
            "workload_class": sp["workload_class"],
            "low": m["low"] if m else None, "median": med, "providers": m["providers"] if m else 0,
            "available_listings": m["listings"]["available"] if m else 0,
            "delta": {"median_price": _delta(tm["median"], med), "low_price": _delta(tm["low"], m["low"] if m else None),
                      "vram": _delta(target["vram_gb"], sp["vram_gb"]),
                      "bf16_tflops": _delta(target["bf16_tflops_dense"], sp["bf16_tflops_dense"]),
                      "memory_bandwidth": _delta(target["memory_bandwidth_tbps"], sp["memory_bandwidth_tbps"])},
        })
    out["alternatives"].sort(key=lambda x: (-len(x["shares"]), x["median"] is None, x["median"] or 0, x["gpu"]))
    out["alternatives"] = out["alternatives"][:limit]
    return out
