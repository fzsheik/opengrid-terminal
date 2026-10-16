"""Theoretical performance of a GPU option, summed over its GPUs, and (for training, when the work is known) a
runtime and whole-job cost range. Pure: no database, no provider call.

    interconnect(spec, n) -> {"kind", "label", "detail"}
    scaling(kind, n, method) -> {"efficiency", "range", "speedup", "speedup_range", "basis"}
    throughput_basis(method, precision, spec) -> {"metric", "per_gpu", "unit", "label"} | None
    work(req, profile) -> {"tokens", "flops", "formula", "assumptions"} | {"tokens": None, "reason"}
    utilization(params_b) -> (central, low, high)
    option_performance(spec, n, *, method, precision, work, params_b) -> dict (an option's `performance`)

Everything here is ESTIMATED from vendor peak specs (hardware.py: dense throughput, memory bandwidth) and
planning assumptions stated in the output. Nothing is a benchmark. A measured benchmark, when one exists for a
workload and GPU (performance_observations), is the only thing that could replace these figures.

Why two GPUs are not "twice one GPU": a model split across GPUs exchanges data on every step (gradient
reduce-scatter / all-gather for sharded data parallel, activations every layer for tensor parallel). On NVLink
or Infinity Fabric that costs a few percent to a few tens of percent; over PCIe, much more. The scaling table
below is OpenGrid's planning assumption per interconnect class, shown with a range.

Throughput basis per workload:
    training / fine-tuning: compute bound. Dense tensor throughput at the training precision (bf16/fp16; fp8 when
        the run is fp8 and the GPU publishes a dense fp8 rate; fp32 for fp32 runs).
    inference: token generation is memory-bandwidth bound at small batch (every weight is read once per token), so
        the basis is memory bandwidth. Large batches move toward compute; that is not modelled.

Training work: FLOPs = k x N x T, N = parameters (active parameters for a mixture of experts), T = tokens trained.
    k = 6 for full training (forward 2N + backward 4N per token; Kaplan et al. 2020, section 2.1).
    k = 4 for LoRA / QLoRA: the frozen base weights get no weight gradients (the 2N for weight gradients is
        skipped), but activation gradients still flow through every layer (LoRA, Hu et al. 2021).
    + 2 (one extra forward pass) with gradient checkpointing (full recomputation; Chen et al. 2016).
Runtime = FLOPs / (GPUs x peak x scaling efficiency x utilization). Utilization (model FLOPs utilization, MFU):
    central 35%, range 20-50% for models of 1B+ parameters. Well-tuned pretraining reaches 46-52% (PaLM 46.2% on
    TPU v4, Megatron-LM 52% at 1T scale on A100); typical fine-tuning stacks land lower, so the central figure is
    conservative. Smaller models keep GPUs less busy: central 20%, range 5-40%.
"""

from __future__ import annotations

import math

SOURCES = {
    "training_flops": "https://arxiv.org/abs/2001.08361",
    "lora": "https://arxiv.org/abs/2106.09685",
    "checkpointing": "https://arxiv.org/abs/1604.06174",
    "mfu_palm": "https://arxiv.org/abs/2204.02311",
    "mfu_megatron": "https://arxiv.org/abs/2104.04473",
    "fsdp": "https://arxiv.org/abs/2304.11277",
}

# Per-GPU scaling efficiency (central, low, high) when the work is split over n GPUs: speedup = n x efficiency.
# OpenGrid planning assumptions, not measurements. Keys: interconnect kind -> method family -> n.
_TRAIN = {
    "nvlink": {2: (0.93, 0.85, 0.98), 4: (0.90, 0.80, 0.96), 8: (0.87, 0.75, 0.94)},
    "nvlink_bridge": {2: (0.90, 0.80, 0.96), 4: (0.75, 0.55, 0.88), 8: (0.65, 0.45, 0.82)},
    "pcie": {2: (0.80, 0.60, 0.92), 4: (0.70, 0.50, 0.86), 8: (0.60, 0.40, 0.80)},
}
_INFER = {
    "nvlink": {2: (0.85, 0.70, 0.95), 4: (0.75, 0.60, 0.90), 8: (0.65, 0.50, 0.85)},
    "nvlink_bridge": {2: (0.80, 0.65, 0.92), 4: (0.60, 0.40, 0.80), 8: (0.50, 0.30, 0.70)},
    "pcie": {2: (0.65, 0.45, 0.85), 4: (0.50, 0.30, 0.75), 8: (0.40, 0.25, 0.65)},
}
MFU_LARGE = (0.35, 0.20, 0.50)      # models >= 1B parameters
MFU_SMALL = (0.20, 0.05, 0.40)      # smaller models
SMALL_B = 1.0
SWITCHED = ("SXM", "OAM", "Superchip")
LABEL = {"single_gpu": "one GPU", "nvlink": "NVLink / Infinity Fabric between every GPU",
         "nvlink_bridge": "NVLink bridges between pairs of cards", "pcie": "PCIe only (no NVLink on record)"}


def interconnect(spec: dict, n: int) -> dict:
    """GPU-to-GPU link class for n GPUs of this model in one instance."""
    nvl = spec.get("nvlink")
    detail = nvl or spec.get("interconnect") or "not on record"
    if n <= 1:
        return {"kind": "single_gpu", "label": LABEL["single_gpu"], "detail": detail}
    if spec.get("form_factor") in SWITCHED:
        return {"kind": "nvlink", "label": LABEL["nvlink"], "detail": detail}
    if nvl:
        text = str(nvl).lower()
        four_way = "4-way" in text
        if n == 2 or (n == 4 and four_way):
            return {"kind": "nvlink", "label": "NVLink bridge between the cards", "detail": detail}
        return {"kind": "nvlink_bridge", "label": LABEL["nvlink_bridge"], "detail": detail}
    return {"kind": "pcie", "label": LABEL["pcie"], "detail": detail}


def _nearest(table: dict, n: int) -> tuple:
    if n in table:
        return table[n]
    ks = sorted(table)
    if n > ks[-1]:      # beyond the table: keep losing efficiency per doubling (5 points, floor 30%)
        c, lo, hi = table[ks[-1]]
        d = 0.05 * math.log2(n / ks[-1])
        return (max(0.3, c - d), max(0.2, lo - d), max(0.35, hi - d))
    k = min(ks, key=lambda x: abs(x - n))
    return table[k]


def scaling(kind: str, n: int, method: str | None) -> dict:
    if n <= 1 or kind == "single_gpu":
        return {"efficiency": 1.0, "range": [1.0, 1.0], "speedup": 1.0, "speedup_range": [1.0, 1.0],
                "basis": "one GPU: nothing is split", "kind": "estimated"}
    inference = method == "inference"
    c, lo, hi = _nearest((_INFER if inference else _TRAIN)[kind], n)
    what = ("tensor parallel inference exchanges activations every layer" if inference else
            "sharded data-parallel training exchanges gradients and weights every step")
    return {"efficiency": c, "range": [lo, hi], "speedup": round(n * c, 2), "speedup_range": [round(n * lo, 2), round(n * hi, 2)],
            "basis": f"{what}; over {LABEL.get(kind, kind)} OpenGrid assumes {c:.0%} of linear scaling per GPU "
                     f"(range {lo:.0%} to {hi:.0%}). Planning assumption, not a benchmark",
            "kind": "estimated"}


def throughput_basis(method: str | None, precision: str | None, spec: dict) -> dict | None:
    if method == "inference":
        v = spec.get("memory_bandwidth_tbps")
        return None if not v else {"metric": "memory_bandwidth", "per_gpu": float(v), "unit": "TB/s",
                                   "label": "memory bandwidth (token generation is bandwidth bound)"}
    if method is None or not str(method).startswith("training"):
        return None
    if precision == "fp8" and spec.get("fp8_tflops_dense"):
        v, lab = spec["fp8_tflops_dense"], "dense FP8"
    elif precision == "fp32" and spec.get("fp32_tflops"):
        v, lab = spec["fp32_tflops"], "FP32"
    else:
        v = spec.get("bf16_tflops_dense") or spec.get("fp16_tflops_dense")
        lab = "dense BF16" if spec.get("bf16_tflops_dense") else "dense FP16"
    return None if not v else {"metric": "tflops", "per_gpu": float(v), "unit": "TFLOPS",
                               "label": f"{lab} tensor throughput (training is compute bound)"}


def utilization(params_b: float | None) -> tuple:
    return MFU_SMALL if (params_b is not None and params_b < SMALL_B) else MFU_LARGE


def work(req, profile: dict) -> dict:
    """Training work in FLOPs, when the tokens trained are known. Never guesses the dataset."""
    method = profile.get("method") or ""
    if not method.startswith("training"):
        return {"tokens": None, "flops": None, "reason": "runtime is only estimated for training and fine-tuning"}
    m = profile.get("model") or {}
    n_params = m.get("active_parameters_b") or m.get("parameters_b")
    if not n_params:
        return {"tokens": None, "flops": None, "reason": "parameter count unknown"}
    tr = req.training
    A: list[str] = []
    tokens = None
    if tr is not None and tr.dataset_tokens:
        tokens = float(tr.dataset_tokens)
        basis = f"{tokens:,.0f} dataset tokens"
    elif tr is not None and tr.dataset_samples and tr.seq_len:
        tokens = float(tr.dataset_samples) * tr.seq_len
        basis = f"{tr.dataset_samples:,.0f} samples x {tr.seq_len:,} tokens per sample"
    if tokens is None:
        return {"tokens": None, "flops": None,
                "reason": "the amount of training data is unknown (dataset tokens, or samples and sequence length)"}
    epochs = tr.epochs if tr.epochs else 1.0
    if not tr.epochs:
        A.append("1 epoch assumed (epochs not stated)")
    tokens *= epochs
    k = 6 if method == "training_full" else 4
    ck = (profile.get("ckpt") or (None, None, None))
    ckpt = ck[1] if isinstance(ck, (list, tuple)) else ck
    if ckpt:
        k += 2
    moe = bool(m.get("active_parameters_b"))
    formula = (f"{k} x {'active ' if moe else ''}parameters x tokens ("
               + ("full training: forward 2N + backward 4N" if method == "training_full" else
                  "LoRA/QLoRA: forward 2N + activation gradients 2N, no base-weight gradients")
               + ("; + 2N for recomputing activations (gradient checkpointing)" if ckpt else "") + ")")
    if method == "training_qlora":
        A.append("QLoRA dequantizes the 4-bit weights on every pass: its real runtime is usually longer than this")
    return {"tokens": tokens, "epochs": epochs, "tokens_basis": basis, "flops": k * n_params * 1e9 * tokens,
            "flops_per_token_factor": k, "formula": formula, "assumptions": A, "kind": "estimated",
            "sources": {"training_flops": SOURCES["training_flops"], "lora": SOURCES["lora"],
                        "checkpointing": SOURCES["checkpointing"]}}


def option_performance(spec: dict, n: int, *, method: str | None, precision: str | None, wk: dict | None,
                       params_b: float | None) -> dict:
    """The `performance` block of one option. Totals are sums over the GPUs; the effective figure applies the
    scaling assumption; runtime is filled only when the training work is known."""
    ic = interconnect(spec, n)
    sc = scaling(ic["kind"], n, method)
    b = throughput_basis(method, precision, spec)
    vram = spec.get("vram_gb")
    total = {"gpu_count": n, "vram_gb": round(vram * n, 2) if vram else None,
             "bf16_tflops_dense": round(spec["bf16_tflops_dense"] * n, 1) if spec.get("bf16_tflops_dense") else None,
             "memory_bandwidth_tbps": round(spec["memory_bandwidth_tbps"] * n, 3) if spec.get("memory_bandwidth_tbps") else None}
    out = {"kind": "estimated", "basis": b, "total": total, "interconnect": ic, "scaling": sc,
           "effective": None, "relative_to_balanced": None, "runtime": {"known": False, "hours": None, "range_hours": None}}
    if n > 1:
        out["memory_note"] = (f"{total['vram_gb']:g} GB in total, split {n} ways ({vram:g} GB per GPU): the model is "
                              "sharded across the GPUs, not held in one memory")
    if b is None:
        out["runtime"]["reason"] = "no throughput figure on record for this GPU or workload"
        return out
    eff, lo, hi = b["per_gpu"] * sc["speedup"], b["per_gpu"] * sc["speedup_range"][0], b["per_gpu"] * sc["speedup_range"][1]
    out["effective"] = {"value": round(eff, 2), "range": [round(lo, 2), round(hi, 2)], "unit": b["unit"],
                        "basis": f"{n} x {b['per_gpu']:g} {b['unit']} x {sc['efficiency']:.0%} scaling"
                                 if n > 1 else f"{b['per_gpu']:g} {b['unit']} (one GPU)"}
    if not wk or not wk.get("flops"):
        out["runtime"]["reason"] = (wk or {}).get("reason") or "training work unknown"
        return out
    if b["metric"] != "tflops":
        out["runtime"]["reason"] = "runtime is only estimated for compute-bound training"
        return out
    u_c, u_lo, u_hi = utilization(params_b)
    flops = wk["flops"]
    # hours = FLOPs / (TFLOPS x 1e12 x utilization) / 3600; the slow end pairs low scaling with low utilization
    h_c = flops / (eff * 1e12 * u_c) / 3600
    h_slow = flops / (lo * 1e12 * u_lo) / 3600
    h_fast = flops / (hi * 1e12 * u_hi) / 3600
    out["runtime"] = {"known": True, "kind": "estimated", "hours": round(h_c, 3), "range_hours": [round(h_fast, 3), round(h_slow, 3)],
                      "basis": (f"{flops:.3g} FLOPs ({wk['formula']}) / ({out['effective']['basis']} x {u_c:.0%} "
                                f"utilization); range uses {u_lo:.0%} to {u_hi:.0%} utilization and the scaling range. "
                                "Theoretical: not a benchmark"),
                      "assumptions": list(wk.get("assumptions") or []) + [
                          f"model FLOPs utilization {u_c:.0%} (range {u_lo:.0%} to {u_hi:.0%}) for a "
                          f"{'model under 1B parameters' if (params_b or 0) < SMALL_B else 'model of 1B+ parameters'}",
                          "excludes startup, data loading stalls, evaluation and checkpoint saving"],
                      "sources": {"mfu_palm": SOURCES["mfu_palm"], "mfu_megatron": SOURCES["mfu_megatron"],
                                  "fsdp": SOURCES["fsdp"]}}
    return out


def job_cost(price_inst: float, hours: float, hours_range: list, billed) -> dict:
    """Whole-job cost at the instance price, each runtime rounded up to the provider's billing unit."""
    def at(h):
        b = billed(h * 60) / 60
        return round(price_inst * b, 2), round(b, 4)
    c, bh = at(hours)
    lo, _ = at(hours_range[0])
    hi, _ = at(hours_range[1])
    return {"usd": c, "range_usd": [lo, hi], "billed_hours": bh, "kind": "estimated",
            "basis": "estimated runtime x instance price, billed in the provider's billing unit; storage, egress and "
                     "startup time not included"}
