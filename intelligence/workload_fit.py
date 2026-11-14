"""How well an option's hardware suits the workload, beyond price and peak TFLOPS. Pure: no provider call; the only
read is the static fabric table in cost/manual.py (provider documentation).

    link_bandwidth(spec, kind) -> {"gbps", "basis"}                      per-direction GPU-to-GPU bandwidth
    interconnect_evidence(spec, n, listing) -> dict                      GPU link + node fabric, and how we know
    parallelism(method, n, zero_stage=, link_kind=, lora=) -> dict       DDP / ZeRO / FSDP / tensor parallel
    communication(method, n, ...) -> dict | None                         comm vs compute per training step
    inference_throughput(spec, n, ...) -> dict | None                    decode tokens/s roofline, targets
    cost_per_million_tokens(price_inst, inf) -> dict | None
    multi_gpu_advice(...) -> dict                                        when (not) to go multi-GPU

Everything here is THEORETICAL (vendor peaks, published link rates, stated planning assumptions) and labelled
kind "estimated". Nothing is a benchmark. Unknown stays unknown: a listing that says nothing about its GPU link or
node fabric is never upgraded to NVLink / InfiniBand. See methodology/recommendations.md.
"""

from __future__ import annotations

import re

from intelligence import performance as P

# PCIe x16 per-direction bandwidth by generation, GB/s (PCI-SIG: 8 / 16 / 32 / 64 GT/s per lane, 128b/130b
# encoding for Gen3-5, FLIT mode for Gen6).
PCIE_X16_GBPS = {"3": 15.75, "4": 31.5, "5": 63.0, "6": 121.0}
SOURCES = {
    "pcie": "https://pcisig.com/specifications",
    "nccl_allreduce": "https://github.com/NVIDIA/nccl-tests/blob/master/doc/PERFORMANCE.md",
    "fsdp": P.SOURCES["fsdp"],
    "zero": "https://arxiv.org/abs/1910.02054",
}
# Memory-bound decode: share of peak HBM/GDDR bandwidth a serving engine achieves. OpenGrid planning assumption
# (central, low, high), stated in every output; not a measurement.
DECODE_BW_UTIL = (0.60, 0.40, 0.80)
_LISTING_LINK = (
    (re.compile(r"nvswitch", re.I), "nvlink", "the listing names NVSwitch"),
    (re.compile(r"\bsxm\d?\b|\bhgx\b|\bdgx\b", re.I), "nvlink", "the listing names an SXM / HGX / DGX board"),
    (re.compile(r"nvlink", re.I), "nvlink", "the listing names NVLink"),
    (re.compile(r"\boam\b|infinity fabric", re.I), "nvlink", "the listing names OAM / Infinity Fabric"),
    (re.compile(r"\bpcie\b|\bpci-e\b", re.I), "pcie", "the listing names PCIe"),
)
_FABRIC_WORDS = (("infiniband", "infiniband"), ("roce", "roce"), ("efa", "efa"), ("tcpx", "tcpx"))


def _bw_gbps(text) -> float | None:
    """'NVLink 4, 900 GB/s' -> 900.0; 'NVLink 5, 1.8 TB/s' -> 1800.0; None when no figure is stated."""
    if not text:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*(TB|GB)/s", str(text), re.I)
    if not m:
        return None
    v = float(m.group(1))
    return v * 1000 if m.group(2).upper() == "TB" else v


def link_bandwidth(spec: dict, kind: str) -> dict:
    """Per-direction GPU-to-GPU bandwidth (GB/s) for an interconnect kind, from the spec sheet. NVLink figures are
    published as the total of both directions, so half is used per direction; PCIe from the host-link generation."""
    if kind in ("nvlink", "nvlink_bridge"):
        tot = _bw_gbps(spec.get("nvlink"))
        if tot:
            return {"gbps": round(tot / 2, 2),
                    "basis": f"half of the published {tot:g} GB/s total ({spec.get('nvlink')}), per direction"}
        return {"gbps": None, "basis": "NVLink / Infinity Fabric bandwidth not on record"}
    if kind == "pcie":
        m = re.search(r"gen\s*(\d)", str(spec.get("interconnect") or ""), re.I)
        if m and m.group(1) in PCIE_X16_GBPS:
            g = PCIE_X16_GBPS[m.group(1)]
            return {"gbps": g, "basis": f"PCIe Gen{m.group(1)} x16, {g:g} GB/s per direction (PCI-SIG); GPUs behind "
                                        "a shared PCIe switch or across CPU sockets get less"}
        return {"gbps": None, "basis": "PCIe generation not on record"}
    return {"gbps": None, "basis": "one GPU: no GPU-to-GPU traffic"}


def interconnect_evidence(spec: dict, n: int, listing: dict | None = None) -> dict:
    """The option's GPU link and node fabric, and how OpenGrid knows. Explicit listing text (raw GPU name, SKU,
    provider tier) wins over the spec sheet; the node fabric (InfiniBand / RoCE / EFA / TCPX) comes from the listing
    text or provider documentation recorded in cost/manual.FABRIC. Unknown stays None."""
    base = P.interconnect(spec, n)
    kind = base["kind"]
    src = "single_gpu" if n <= 1 else "spec_sheet"
    note = base["label"]
    listing = listing or {}
    text = " ".join(str(listing.get(k) or "") for k in ("raw_gpu_name", "sku", "provider_tier"))
    if n > 1 and text.strip():
        for rx, k, why in _LISTING_LINK:
            if not rx.search(text):
                continue
            if k == "pcie":
                # "PCIe" in a listing names the card's form factor or host link; it says nothing about whether
                # NVLink bridges are fitted, so it never changes what the spec sheet says
                break
            if k == "nvlink" and kind == "nvlink_bridge":
                pass    # bridged PCIe cards: the bridge is NVLink, but it does not span every GPU
            else:
                kind = k
            src, note = "listing", why
            break
    bw = link_bandwidth(spec, kind if n > 1 else "single_gpu")
    out = {"gpu_link": kind, "gpu_link_source": src, "gpu_link_note": note,
           "per_direction_gbps": bw["gbps"], "bandwidth_basis": bw["basis"],
           "node_fabric": None, "node_fabric_gbps": None, "node_fabric_source": None, "multi_node_capable": None,
           "kind": "estimated"}
    for word, fk in _FABRIC_WORDS:
        if re.search(rf"(?<![a-z0-9]){word}(?![a-z0-9])", text, re.I):
            out.update(node_fabric=fk, node_fabric_source="listing")
            break
    if listing.get("provider"):
        try:
            from cost import estimate as cost_estimate
            ls = cost_estimate.listing_specs({"provider": listing["provider"], "sku": listing.get("sku"),
                                              "listing_id": listing.get("listing_id"),
                                              "canonical_gpu_name": listing.get("gpu"),
                                              "gpu_count": listing.get("gpu_count") or n})
            if ls.get("node_fabric"):
                out.update(node_fabric=ls["node_fabric"], node_fabric_gbps=ls.get("node_fabric_gbps"),
                           node_fabric_source=ls.get("fabric_source") or "provider documentation",
                           multi_node_capable=ls.get("multi_node"))
        except Exception:  # noqa: BLE001 - fabric data is optional context, never a reason to fail
            pass
    return out


def _mid(v):
    return v[1] if isinstance(v, (list, tuple)) else v


def parallelism(method: str | None, n: int, *, zero_stage: int | None, link_kind: str, lora: bool = False) -> dict:
    """The parallelism strategy an n-GPU option implies, in the words a developer would configure it."""
    if n <= 1:
        return {"strategy": "single_gpu", "label": "one GPU: no parallelism", "configure": None}
    if method == "inference":
        out = {"strategy": "tensor_parallel", "label": f"tensor parallel across {n} GPUs",
               "configure": f"vLLM --tensor-parallel-size {n} (or TGI --num-shard {n})"}
        if link_kind == "pcie":
            out["warning"] = ("tensor parallelism over PCIe exchanges activations every layer: when the model fits on "
                              "fewer GPUs, independent replicas usually serve more tokens per dollar")
        return out
    z = 3 if zero_stage is None else zero_stage
    if z == 0:
        return {"strategy": "ddp", "label": f"data parallel (DDP) across {n} GPUs: a full model copy on each",
                "configure": f"torchrun --nproc_per_node {n}", "zero_stage": 0}
    lab = {1: "ZeRO-1 (optimizer states sharded)", 2: "ZeRO-2 (optimizer states and gradients sharded)",
           3: "FSDP / ZeRO-3 (weights, gradients and optimizer states sharded)"}[z]
    cfg = ("FSDP FULL_SHARD or DeepSpeed ZeRO stage 3" if z == 3 else f"DeepSpeed ZeRO stage {z}") \
        + f", torchrun --nproc_per_node {n}"
    if lora and z == 3:
        cfg += "; LoRA adapters are small, so most traffic is gathering the frozen base weights"
    return {"strategy": "fsdp" if z == 3 else f"zero{z}", "label": f"{lab} across {n} GPUs", "configure": cfg,
            "zero_stage": z}


def communication(method: str | None, n: int, *, params_b: float | None, trainable_params: float | None,
                  zero_stage: int | None, profile: dict, spec: dict, link_kind: str,
                  precision: str | None) -> dict | None:
    """Theoretical communication vs compute per optimizer step for n-GPU training (None for one GPU, inference or
    no parameter count). Ring-collective volume per GPU (NCCL): all-reduce 2(n-1)/n x bytes; all-gather and
    reduce-scatter (n-1)/n x bytes each. DDP / ZeRO-1/2: one gradient all-reduce. ZeRO-3 / FSDP: weight all-gather
    in forward and in backward + gradient reduce-scatter. Communication time is a LOWER BOUND (peak per-direction
    link bandwidth, no protocol overhead); overlap with compute is not assumed. Compute time uses the central
    utilization (performance.utilization). A ratio near or above 1 means the link, not the GPU, sets the pace."""
    if n <= 1 or method is None or not str(method).startswith("training") or not params_b:
        return None
    bw = link_bandwidth(spec, link_kind)
    b = P.throughput_basis(method, precision, spec)
    batch, seq = _mid(profile.get("batch")), _mid(profile.get("seq"))
    if not bw["gbps"] or not b or not batch or not seq:
        return {"known": False, "kind": "estimated",
                "reason": "link bandwidth, throughput, micro batch or sequence length not on record"}
    p = params_b * 1e9
    t = float(trainable_params) if trainable_params else p
    wbytes = 4.0 if precision == "fp32" else 2.0
    z = 3 if zero_stage is None else zero_stage
    f = (n - 1) / n
    if z >= 3:
        vol = 2 * f * p * wbytes + f * t * wbytes
        what = "FSDP / ZeRO-3: weights all-gathered in forward and backward, gradients reduce-scattered"
    else:
        vol = 2 * f * t * wbytes
        what = "gradient all-reduce" + (f" (ZeRO-{z})" if z else " (DDP)")
    comm_s = vol / (bw["gbps"] * 1e9)
    k = 6 if method == "training_full" else 4
    if _mid(profile.get("ckpt")):
        k += 2
    tokens = float(batch) * float(seq)
    u = P.utilization(params_b)[0]
    compute_s = k * p * tokens / (b["per_gpu"] * 1e12 * u)
    ratio = comm_s / compute_s if compute_s > 0 else None
    if ratio is None:
        verdict = "unknown"
    elif ratio >= 1:
        verdict = "communication-bound: the link sets the pace; larger micro batches or fewer, larger GPUs help"
    elif ratio >= 0.3:
        verdict = "communication is a large share of each step unless the framework overlaps it with compute"
    else:
        verdict = "compute-bound: communication is a small share of each step"
    return {"known": True, "kind": "estimated", "pattern": what,
            "bytes_per_gpu_per_step": round(vol), "link_per_direction_gbps": bw["gbps"], "link_basis": bw["basis"],
            "comm_seconds_lower_bound": round(comm_s, 4), "compute_seconds": round(compute_s, 4),
            "tokens_per_gpu_per_step": int(tokens),
            "comm_to_compute": None if ratio is None else round(ratio, 3), "verdict": verdict,
            "basis": (f"{what}: {vol / 1e9:.3g} GB per GPU per step at {bw['gbps']:g} GB/s (lower bound) vs "
                      f"{k} x {params_b:g}B parameters x {int(tokens):,} tokens per GPU at {b['per_gpu']:g} TFLOPS x "
                      f"{u:.0%} utilization. Theoretical, not a benchmark; overlap not assumed"),
            "sources": {"nccl_allreduce": SOURCES["nccl_allreduce"], "fsdp": SOURCES["fsdp"], "zero": SOURCES["zero"]}}


def inference_throughput(spec: dict, n: int, *, per_gpu_gb: dict | None, profile: dict, params_b: float | None,
                         active_params_b: float | None, ic_kind: str, target_tps: float | None = None,
                         target_latency_ms: float | None = None, output_tokens: int | None = None) -> dict | None:
    """Theoretical decode throughput (output tokens/s) of one inference option: a memory-bandwidth roofline.

    One decode step serves every concurrent sequence: it reads the per-GPU weights once and each sequence's KV cache.
    step = (weights + KV read) / (bandwidth x achieved share) / tensor-parallel efficiency. Aggregate = concurrency /
    step, capped by compute (2 x active parameters FLOPs per token at the central utilization). Central: KV half full
    (mid-generation); fast end: weights only at the high bandwidth share; slow end: full KV at the low share.
    Prefill (prompt processing), batching gaps and engine overheads are not modelled. Theoretical, not a benchmark."""
    if not per_gpu_gb or not params_b:
        return None
    bw = spec.get("memory_bandwidth_tbps")
    w = float(per_gpu_gb.get("weights") or 0.0) * 1e9
    kv = float(per_gpu_gb.get("kv_cache") or 0.0) * 1e9
    if not bw or w <= 0:
        return None
    conc = int(_mid(profile.get("concurrency")) or 1)
    sc = P.scaling(ic_kind, n, "inference")
    bps = float(bw) * 1e12
    uc, ul, uh = DECODE_BW_UTIL
    ec, (el, eh) = sc["efficiency"], sc["range"]
    step_c = (w + 0.5 * kv) / (bps * uc) / ec
    step_fast = w / (bps * uh) / eh
    step_slow = (w + kv) / (bps * ul) / el
    agg = [conc / step_c, conc / step_slow, conc / step_fast]
    cap = None
    b = P.throughput_basis("training_full", profile.get("precision"), spec)   # the GPU's tensor throughput figure
    act = active_params_b or params_b
    if b:
        cap = n * b["per_gpu"] * 1e12 * P.utilization(params_b)[0] / (2 * act * 1e9)
        agg = [min(a, cap) for a in agg]
    out = {"kind": "estimated", "concurrency": conc,
           "aggregate_tokens_per_s": round(agg[0], 1), "aggregate_range": [round(agg[1], 1), round(agg[2], 1)],
           "per_stream_tokens_per_s": round(1 / step_c, 1),
           "per_stream_range": [round(1 / step_slow, 1), round(1 / step_fast, 1)],
           "decode_step_ms": round(step_c * 1000, 2),
           "compute_cap_tokens_per_s": None if cap is None else round(cap, 1),
           "basis": (f"memory-bandwidth roofline: each decode step reads {w / 1e9:.3g} GB of weights plus up to "
                     f"{kv / 1e9:.3g} GB of KV cache per GPU at {float(bw):g} TB/s x {uc:.0%} achieved bandwidth "
                     f"(range {ul:.0%}-{uh:.0%}, planning assumption) and {ec:.0%} tensor-parallel efficiency; "
                     f"{conc} concurrent sequence(s). Decode only: prefill and engine overheads not modelled. "
                     "Theoretical, not a benchmark")}
    if target_tps:
        lo, hi = out["aggregate_range"]
        out["target_tokens_per_s"] = target_tps
        out["meets_target"] = True if lo >= target_tps else False if hi < target_tps else None
        out["target_note"] = ("target read as total output tokens per second across all concurrent requests; "
                              + ("met even at the slow end" if out["meets_target"] else
                                 "not met even at the fast end" if out["meets_target"] is False else
                                 "met only in part of the estimated range"))
    if target_latency_ms and output_tokens:
        lat = [output_tokens * s * 1000 for s in (step_c, step_slow, step_fast)]
        out["latency_ms"] = {"central": round(lat[0], 1), "range": [round(lat[2], 1), round(lat[1], 1)],
                             "basis": f"{output_tokens} output tokens x decode step time; prefill not included"}
        out["target_latency_ms"] = target_latency_ms
        out["meets_latency"] = True if lat[1] <= target_latency_ms else False if lat[2] > target_latency_ms else None
    return out


def cost_per_million_tokens(price_inst: float, inf: dict | None) -> dict | None:
    """Instance price / theoretical aggregate output tokens per hour x 1e6, for a fully busy server."""
    if not inf or not inf.get("aggregate_tokens_per_s") or price_inst <= 0:
        return None
    lo, hi = inf["aggregate_range"]

    def per(tps):
        return round(price_inst / (tps * 3600) * 1e6, 4) if tps and tps > 0 else None
    return {"usd": per(inf["aggregate_tokens_per_s"]), "range_usd": [per(hi), per(lo)], "kind": "estimated",
            "basis": "instance price / (theoretical aggregate output tokens/s x 3600) x 1e6 with the server fully "
                     "busy; idle time costs extra"}


def multi_gpu_advice(*, method: str | None, params_b: float | None, suggested: int | None, zero_stage: int | None,
                     fits_one: bool, scale_pairs: list[dict]) -> dict:
    """When (not) to go multi-GPU, from the memory estimate and the options' theoretical scaling.

    scale_pairs: [{"gpu", "from", "to", "speedup", "speedup_range", "link", "job_cost_ratio"}] where job_cost_ratio is
    the scale-up option's estimated whole-job cost / the minimum-count option's (None when runtime is unknown)."""
    not_when: list[str] = []
    if fits_one:
        not_when.append("the model fits on one GPU: a second GPU only buys speed, paid for in communication and a more "
                        "complex launch (torchrun / FSDP / tensor parallel)")
    if params_b is not None and params_b < P.SMALL_B:
        not_when.append(f"a {params_b:g}B-parameter model already keeps one GPU only partly busy; splitting it adds "
                        "communication for little compute gain")
    if zero_stage == 0 and method and str(method).startswith("training"):
        not_when.append("ZeRO stage 0 (plain DDP) keeps a full model copy on every GPU: more GPUs never reduce memory")
    if method == "inference":
        not_when.append("for serving, tensor parallelism helps only when the model does not fit one GPU or per-token "
                        "latency must drop; for throughput, independent replicas scale better")
    pcie = sorted({p["gpu"] for p in scale_pairs if p.get("link") == "pcie"})
    if pcie:
        not_when.append("PCIe-only multi-GPU (" + ", ".join(pcie) + ") loses the most to communication: prefer "
                        "NVLink / SXM boards for sharded training or tensor parallelism")
    cheaper = [p for p in scale_pairs if p.get("job_cost_ratio") is not None and p["job_cost_ratio"] <= 1.0]
    if suggested and suggested > 1:
        verdict = "multi_gpu_required"
        head = f"the model needs at least {suggested} GPUs of the largest size on record, for memory"
    elif cheaper:
        verdict = "scale_up_optional"
        head = ("fits on one GPU; for " + ", ".join(sorted({p["gpu"] for p in cheaper}))
                + " more GPUs are estimated to finish sooner for the same or a lower whole-job cost")
    else:
        verdict = "single_gpu"
        head = "fits on one GPU: stay on one unless you need it finished sooner"
    return {"verdict": verdict, "summary": head, "when_not_to_go_multi_gpu": not_when, "scale_up": scale_pairs,
            "kind": "estimated",
            "note": "scaling efficiencies are OpenGrid planning assumptions per interconnect class, not benchmarks"}
