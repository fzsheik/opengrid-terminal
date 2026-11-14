"""Estimate: does this workload need a GPU, how much GPU memory, how many GPUs, what is still unknown.

    estimate(req: WorkloadRequirements) -> dict      (contract section 2)

Pure computation: no database, no provider call. Runtime depends on the GPU, so it is not a single number here:
`work` is the training work (FLOPs from the tokens trained, when known) and each recommended option carries its own
theoretical runtime range (intelligence/performance.py). Nothing is a benchmark. Questions are asked only for
fields that are unknown AND change the answer.
"""

from __future__ import annotations

from intelligence import media, memory, performance
from intelligence.requirements import WorkloadRequirements

METHODOLOGY = "/methodology/recommendations"
MAX_QUESTIONS = 6


def _has_model(req: WorkloadRequirements) -> bool:
    m = req.model
    return m is not None and any(v is not None for v in m.model_dump().values())


def needs_gpu(req: WorkloadRequirements, wc: str | None) -> tuple[bool | None, str]:
    """False only on evidence: workload_type 'no_gpu', or frameworks listed and none is an ML framework and no
    model is named. Unknown stays None."""
    if req.workload_type == "no_gpu":
        return False, "workload_type is 'no_gpu': nothing in this workload uses a GPU"
    fw = set(req.frameworks or ())
    ml = sorted(fw & memory.ML_FRAMEWORKS)
    if wc in ("training", "inference"):
        what = req.workload_type or wc
        return True, f"{what.replace('_', '-')} workload" + (f" ({', '.join(ml)})" if ml else "")
    if _has_model(req):
        return True, "a model is named: model training or inference uses a GPU"
    if ml:
        return True, f"ML framework(s) found: {', '.join(ml)}"
    if fw:
        return False, (f"no ML framework among {', '.join(sorted(fw))} and no model found: this looks like an "
                       "application that does not need a GPU")
    return None, "not enough information to tell whether this workload needs a GPU"


def _q(field: str, question: str, why: str) -> dict:
    return {"field": field, "question": question, "why": why}


def questions(req: WorkloadRequirements, prof: dict, need: bool | None) -> list[dict]:
    if need is False:
        return []
    out = []
    wc, known, m = prof["workload_class"], prof["known"], prof["model"]
    if wc is None:
        out.append(_q("workload_type", "Is this training, fine-tuning or inference?",
                      "training needs gradients and optimizer states (about 16 bytes per parameter for full AdamW "
                      "training) where inference needs only weights and a KV cache"))
    if not m.get("parameters_b"):
        out.append(_q("model.name", "Which model (Hugging Face id) or how many parameters?",
                      "weights dominate GPU memory: without a parameter count no memory estimate is possible"))
    elif prof["kind"] is None and m.get("num_layers") is None:
        out.append(_q("model.name", "Which model architecture (Hugging Face id, or layers / hidden size / heads)?",
                      "activation and KV-cache memory depend on the architecture, not just the parameter count"))
    if wc == "training":
        if known.get("training.method") == "assumed" and req.workload_type != "training":
            out.append(_q("training.method", "Full fine-tuning, LoRA or QLoRA?", "changes optimizer memory by ~10x"))
        if known.get("training.micro_batch_size") == "assumed":
            out.append(_q("training.micro_batch_size", "What micro batch size per GPU?",
                          "activation memory grows linearly with the micro batch"))
        if known.get("training.seq_len") == "assumed":
            out.append(_q("training.seq_len", "What sequence length do you train with?",
                          "activation memory grows linearly (quadratically without FlashAttention) with it"))
        if known.get("training.gradient_checkpointing") == "assumed":
            out.append(_q("training.gradient_checkpointing", "Is gradient checkpointing on?",
                          "it cuts stored activations by roughly the per-layer factor (~17x) at ~30% more compute"))
    if wc == "inference":
        if known.get("inference.concurrency") == "assumed":
            out.append(_q("inference.concurrency", "How many requests at once?",
                          "the KV cache grows linearly with concurrent sequences"))
        if known.get("inference.context_len") == "assumed":
            out.append(_q("inference.context_len", "What context length (prompt + output tokens) per request?",
                          "the KV cache grows linearly with context"))
    if known.get("precision") == "assumed" and wc is not None:
        out.append(_q("precision", "Which precision or quantization (bf16, fp8, int8, 4-bit)?",
                      "weights take 2 bytes per parameter in bf16, 1 in 8-bit, about 0.5 in 4-bit"))
    out += _media_questions(req, prof)
    tr = req.training
    if wc == "training" and m.get("parameters_b") and not (tr is not None and (
            tr.dataset_tokens or (tr.dataset_samples and tr.seq_len)
            or (tr.max_steps and tr.global_batch_size and tr.seq_len))):
        out.append(_q("training.dataset_tokens", "How many tokens will you train on (and for how many epochs)?",
                      "lets OpenGrid estimate the runtime and the whole-job cost of each GPU option, including "
                      "whether more GPUs finish it for less"))
    return out[:MAX_QUESTIONS]


def _media_questions(req: WorkloadRequirements, prof: dict) -> list[dict]:
    """Questions for unknown audio / image / video inputs that change the answer (only for media workloads)."""
    md = prof.get("media") or {}
    kind, known = md.get("media_kind"), prof["known"]
    out = []
    if not md:
        return out
    if kind is None and not prof["model"].get("parameters_b"):
        return out          # the model question already covers it
    if known.get("media.images_per_request") == "assumed":
        out.append(_q("media.images_per_request", "How many images per request?",
                      "every image adds its tokens to the KV cache of each request"))
    if known.get("media.image_resolution") == "assumed":
        out.append(_q("media.image_resolution", "What image resolution (e.g. 1024x768)?",
                      "this model's tokens per image grow with the resolution"))
    if known.get("media.video_seconds") == "assumed":
        out.append(_q("media.video_seconds", "How long is each video (seconds)?",
                      "sampled frames, and so video tokens, grow with the length"))
    if known.get("media.output_video_seconds") == "assumed" or known.get("media.output_resolution") == "assumed":
        out.append(_q("media.output_resolution", "What output resolution and length (seconds) per video?",
                      "latent video tokens grow with frames x height x width; attention grows with their square"))
    wk = md.get("work") or {}
    if wk.get("known") and not wk.get("items") and kind in ("asr", "audio_encoder", "tts"):
        out.append(_q("media.total_audio_hours", "How many hours of audio in total?",
                      "lets OpenGrid estimate the runtime and whole-job cost of each GPU option"))
    elif wk.get("known") and not wk.get("items") and kind == "vlm":
        out.append(_q("media.total_items", "How many images or requests in total (for a batch job)?",
                      "lets OpenGrid estimate the runtime and whole-job cost of each GPU option"))
    return out


def estimate(req: WorkloadRequirements) -> dict:
    vendor = req.constraints.vendor if req.constraints is not None else None
    mem = memory.estimate_memory(req, vendor=vendor)
    prof = mem["profile"]
    need, need_why = needs_gpu(req, prof["workload_class"])
    if need is False:
        mem = {"method": "unavailable", "components_gb": None, "min_total_gb": None, "recommended_gb": None,
               "range_gb": None, "per_gpu_gb": None, "confidence": "none",
               "assumptions": [], "kind": "estimated", "unavailable_reason": "no GPU needed", "per_gpu_by_count": {},
               "profile": prof, "gpu_count": {"min": 0, "suggested": 0, "reason": need_why},
               "interconnect": {"needed": False, "reason": "no GPU needed"}}
    gc, ic = mem.pop("gpu_count"), mem.pop("interconnect")
    wk = performance.work(req, prof) if mem.get("method") not in (None, "unavailable") else {
        "tokens": None, "flops": None, "reason": "no memory estimate"}
    md = prof.get("media")
    internal = mem.pop("_p", None)
    if md is not None and need is not False:
        md["work"] = (media.work(req, internal) if internal is not None and mem.get("method") != "unavailable"
                      else {"known": False, "reason": "no memory estimate"})
    mwork = (md or {}).get("work") or {}
    if mwork.get("known") and mwork.get("items"):
        what = mwork["unit"]["name"]
        runtime = {"known": False, "estimable": True, "kind": "estimated",
                   "reason": "runtime depends on the GPU: each option has a theoretical runtime range for the "
                             + what + " to process (compute at an assumed utilization + memory-bound decode; not a "
                             "benchmark)"}
    elif wk.get("flops"):
        runtime = {"known": False, "estimable": True, "kind": "estimated",
                   "reason": "runtime depends on the GPU: each option has a theoretical runtime range (vendor peak "
                             "throughput x stated utilization and multi-GPU scaling assumptions, not a benchmark)"}
    else:
        runtime = {"known": False, "estimable": False,
                   "reason": f"runtime not estimated: {wk.get('reason') or 'training work unknown'}, and no measured "
                             "benchmark exists for this workload"}
    out = {
        "needs_gpu": need,
        "needs_gpu_reason": need_why,
        "workload_type": req.workload_type or (prof["workload_class"] if need is not False else None),
        "memory": mem,
        "gpu_count": gc,
        "interconnect": ic,
        "runtime": runtime,
        "work": wk,
        "questions": questions(req, prof, need),
        "methodology": METHODOLOGY,
    }
    if md is not None:
        out["media"] = md
        h = media.headline(out)
        if h:
            md["headline"] = h
    return out
