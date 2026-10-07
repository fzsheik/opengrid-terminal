"""Price per unit of theoretical capability: $/GB-VRAM-hour, $/TFLOP-hour, $/(TB/s)-hour.

    price_per_unit = observed market price per GPU-hour / vendor peak figure

computed at the current market low and median (provider votes, see dispersion.py)
for every figure hardware.spec has. Dense (non-sparsity) peaks only. These are
theoretical, from vendor peak specs; not a workload benchmark: real utilisation
varies by model, precision, kernel and interconnect. See methodology/hardware.md.
"""

from __future__ import annotations

import hardware

LABEL = "theoretical, from vendor peak specs; not a workload benchmark"

METRICS = (
    ("per_gb_vram_hour", "vram_gb", "USD per GB of VRAM per hour"),
    ("per_bf16_tflop_hour", "bf16_tflops_dense", "USD per dense BF16 TFLOP-hour"),
    ("per_fp16_tflop_hour", "fp16_tflops_dense", "USD per dense FP16 TFLOP-hour"),
    ("per_fp8_tflop_hour", "fp8_tflops_dense", "USD per dense FP8 TFLOP-hour"),
    ("per_tbps_bandwidth_hour", "memory_bandwidth_tbps", "USD per TB/s of memory bandwidth per hour"),
)


def capability(gpu: str, low: float | None, median: float | None) -> dict:
    """{metric: {low, median, unit, spec_value, reason}} plus the label. Pure apart from hardware lookup."""
    sp = hardware.spec(gpu)
    out = {"gpu": gpu, "label": LABEL, "metrics": {}}
    for key, field, unit in METRICS:
        v = sp.get(field) if sp else None
        m = {"unit": unit, "spec_field": field, "spec_value": v, "low": None, "median": None, "reason": None}
        if not v:
            m["reason"] = "no hardware entry" if sp is None else f"{field} not published / not recorded"
        elif low is None:
            m["reason"] = "no current market price"
        else:
            m["low"] = low / v
            m["median"] = None if median is None else median / v
        out["metrics"][key] = m
    return out


def ratio(gpu: str, price: float | None, field: str = "bf16_tflops_dense") -> float | None:
    sp = hardware.spec(gpu)
    v = sp.get(field) if sp else None
    return None if not v or price is None else price / v
