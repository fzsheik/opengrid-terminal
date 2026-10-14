"""A modular GPU-memory (VRAM) estimator. Every term is a published formula; every default is an assumption the
output states. Units: GB = 1e9 bytes (decimal), compared against vendor VRAM figures (which are GiB-sized, so the
comparison errs on the safe side).

    weight_bytes_per_param(precision, quantization) -> float
    optimizer_bytes_per_param(optimizer, *, master_fp32, hidden_size=None) -> float
    lora_trainable_params(rank, *, layers, hidden_size, num_heads, num_kv_heads, head_dim) -> int
    transformer_activation_bytes(*, seq, batch, hidden, heads, layers, flash, checkpointing, vocab, act_bytes=2)
    kv_cache_bytes(*, layers, kv_heads, head_dim, context, concurrency, bytes_per_value=2) -> float
    profile(req) -> dict            resolved inputs + assumptions + uncertain knobs (low / central / high)
    estimate_memory(req) -> dict    contract section 2 "memory" + gpu_count / interconnect / per-count needs

Components (no double counting: each byte belongs to exactly one)
    weights             params x bytes/param for the weight dtype. Quantized weights: the embedding and output
                        head (2 x vocab x hidden) stay 16-bit when vocab and hidden are known. LoRA/QLoRA: frozen
                        base weights + the adapter weights (fp32).
    gradients           TRAINABLE params only: full = all params at the weight dtype (2 B mixed, 4 B fp32);
                        LoRA/QLoRA = adapter params x 4 B (fp32 adapters).
    optimizer           TRAINABLE params only: AdamW m+v fp32 = 8 B; 8-bit AdamW = 2 B; SGD momentum = 4 B;
                        Adafactor factored second moment ~ 4 B x 2/hidden; PLUS the fp32 master copy (4 B) when the
                        trainable weights are 16-bit (mixed precision). Full AdamW mixed precision = 2+2+12 = 16 B/param
                        (Rajbhandari et al. 2020, ZeRO, section 3).
    activations         training: per layer s*b*h*(34 + 5*a*s/h) bytes for 16-bit activations (Korthikanti et al.
                        2022, eq. 1); with FlashAttention the 5*a*s/h attention-score term is not stored (central
                        assumption, stated in the output). Gradient checkpointing (full recomputation): only
                        each layer's input is kept, 2*s*b*h per layer, plus one layer recomputed at a time.
                        Plus the fp32 logits for the loss: 4*s*b*vocab. CNNs: catalog activation elements x batch x
                        activation bytes. Inference: transient, covered by the framework overhead.
    kv_cache            inference: 2 (K and V) x layers x kv_heads x head_dim x context x concurrency x 2 B.
    framework_overhead  CUDA context, cuBLAS/cuDNN workspaces, allocator fragmentation: 1.5 GB fixed + 5% of the
                        other components, per GPU.
    min_total_gb        the sum (theoretical minimum); recommended_gb = min_total x 1.20 (20% safety margin).

Sharding across N GPUs: ZeRO stage 1 shards optimizer states, 2 also gradients, 3 also weights (Rajbhandari
et al. 2020); activations stay per GPU (each GPU has its own micro-batch). CPU offload moves the optimizer states
off the GPU. Inference across N GPUs is tensor parallel: weights and KV cache split N ways.
"""

from __future__ import annotations

import hardware

GB = 1e9
FIXED_OVERHEAD_GB = 1.5
OVERHEAD_FRACTION = 0.05
MARGIN = 0.20
COUNTS = (1, 2, 4, 8)
VLLM_USABLE = 0.90           # vLLM's default gpu_memory_utilization: it never plans past 90% of VRAM
ADAPTER_BYTES = 4            # PEFT keeps adapter weights in fp32 (autocast_adapter_dtype=True)
LOGIT_BYTES = 4              # logits upcast to fp32 for the cross-entropy loss
NF4_CONST_BITS = 0.127       # QLoRA double quantization: quantization constants per parameter (Dettmers 2023)
GROUP128_CONST_BITS = 20 / 128  # GPTQ/AWQ group size 128: fp16 scale + 4-bit zero point per 128 weights

SOURCES = {
    "zero": "https://arxiv.org/abs/1910.02054",
    "activations": "https://arxiv.org/abs/2205.05198",
    "flash_attention": "https://arxiv.org/abs/2205.14135",
    "qlora": "https://arxiv.org/abs/2305.14314",
    "lora": "https://arxiv.org/abs/2106.09685",
    "adam_8bit": "https://arxiv.org/abs/2110.02861",
    "adafactor": "https://arxiv.org/abs/1804.04235",
    "vllm": "https://docs.vllm.ai/en/latest/configuration/engine_args.html",
}

LLM_KINDS = ("decoder", "moe")
TRANSFORMER_KINDS = ("decoder", "moe", "encoder", "vit")


# --------------------------------------------------------------------------
# Pure formulas
# --------------------------------------------------------------------------

def weight_bytes_per_param(precision: str | None, quantization: str | None) -> float:
    """Bytes per parameter of stored weights. Quantization wins over precision."""
    q = (quantization or "none").lower()
    if q in ("int4", "gptq", "awq"):
        return 0.5 + GROUP128_CONST_BITS / 8
    if q == "nf4":
        return 0.5 + NF4_CONST_BITS / 8
    if q in ("int8", "fp8"):
        return 1.0
    p = (precision or "bf16").lower()
    return {"fp32": 4.0, "bf16": 2.0, "fp16": 2.0, "fp8": 1.0, "int8": 1.0,
            "int4": 0.5 + GROUP128_CONST_BITS / 8}.get(p, 2.0)


def optimizer_bytes_per_param(optimizer: str | None, *, master_fp32: bool, hidden_size: int | None = None) -> float:
    """Optimizer states per TRAINABLE parameter, plus the fp32 master weights when training in 16-bit."""
    o = (optimizer or "adamw").lower()
    if o == "adamw":
        states = 8.0                          # m and v, fp32
    elif o == "adamw_8bit":
        states = 2.0                          # m and v, 8-bit blockwise (Dettmers et al. 2022)
    elif o == "sgd":
        states = 4.0                          # momentum buffer, fp32
    elif o == "adafactor":
        h = hidden_size or 4096               # factored row + column second moments of an h x h matrix
        states = 4.0 * 2.0 / h
    else:
        states = 8.0
    return states + (4.0 if master_fp32 else 0.0)


def lora_trainable_params(rank: int, *, layers: int, hidden_size: int, num_heads: int, num_kv_heads: int,
                          head_dim: int) -> int:
    """LoRA adapter parameters on the attention projections q, k, v, o of every layer (Hu et al. 2021).

    An adapter on a d_in x d_out matrix adds r x (d_in + d_out) parameters (A is d_in x r, B is r x d_out).
    q: h -> a*d, k and v: h -> kv*d (grouped-query attention), o: a*d -> h.
    """
    q_out = num_heads * head_dim
    kv_out = num_kv_heads * head_dim
    per_layer = rank * ((hidden_size + q_out) + 2 * (hidden_size + kv_out) + (q_out + hidden_size))
    return int(per_layer * layers)


def activation_bytes_per_layer(*, seq: int, batch: int, hidden: int, heads: int, flash: bool) -> float:
    """Korthikanti et al. 2022, eq. 1 (no tensor/sequence parallelism, 16-bit activations): s*b*h*(34 + 5*a*s/h).
    FlashAttention does not materialise the a*s*s attention scores, dropping the 5*a*s/h term."""
    return seq * batch * hidden * (34 + (0 if flash else 5 * heads * seq / hidden))


def transformer_activation_bytes(*, seq: int, batch: int, hidden: int, heads: int, layers: int, flash: bool,
                                 checkpointing: bool, vocab: int | None, act_bytes: float = 2) -> float:
    one = activation_bytes_per_layer(seq=seq, batch=batch, hidden=hidden, heads=heads, flash=flash)
    if checkpointing:
        body = layers * 2 * seq * batch * hidden + one   # stored layer inputs + one layer being recomputed
    else:
        body = layers * one
    body *= act_bytes / 2                                # the formula is for 2-byte activations
    logits = LOGIT_BYTES * seq * batch * (vocab or 0)
    return body + logits


def kv_cache_bytes(*, layers: int, kv_heads: int, head_dim: int, context: int, concurrency: int,
                   bytes_per_value: float = 2) -> float:
    return 2 * layers * kv_heads * head_dim * context * concurrency * bytes_per_value


def framework_overhead_bytes(other_bytes: float) -> float:
    return FIXED_OVERHEAD_GB * GB + OVERHEAD_FRACTION * other_bytes


def largest_vram_gb(vendor: str | None = None) -> float:
    """The largest whole-GPU VRAM tier on record (MIG slices and laptops excluded), optionally for one vendor."""
    best = 0.0
    for s in hardware.all_specs().values():
        if s.get("form_factor") in ("MIG slice", "Laptop") or s.get("vram_gb") is None:
            continue
        if vendor and s.get("vendor") != vendor:
            continue
        best = max(best, float(s["vram_gb"]))
    return best


# --------------------------------------------------------------------------
# Resolving a request into inputs (+ the assumptions that filled gaps)
# --------------------------------------------------------------------------

def _d(obj, name):
    return None if obj is None else getattr(obj, name, None)


ML_FRAMEWORKS = {"pytorch", "torch", "torchvision", "transformers", "peft", "trl", "vllm", "tgi", "tensorflow", "keras",
                 "jax", "flax", "deepspeed", "accelerate", "diffusers", "bitsandbytes", "xformers", "flash-attn",
                 "flash_attn", "lightning", "pytorch-lightning", "sglang", "llama.cpp", "llama-cpp-python", "onnxruntime-gpu",
                 "tensorrt", "tensorrt-llm", "cupy", "rapids", "cudf", "mxnet", "paddlepaddle", "unsloth", "axolotl",
                 "sentence-transformers", "timm", "mlx", "triton", "megatron", "nemo", "auto-gptq", "autoawq", "optimum"}


def workload_class(req) -> str | None:
    """'training' (training + fine_tuning), 'inference' (inference + batch), 'no_gpu', or None (unknown)."""
    wt = req.workload_type
    if wt in ("training", "fine_tuning"):
        return "training"
    if wt in ("inference", "batch"):
        return "inference"
    if wt == "no_gpu":
        return "no_gpu"
    if req.training is not None and any(v is not None for v in req.training.model_dump().values()):
        return "training"
    if req.inference is not None and any(v is not None for v in req.inference.model_dump().values()):
        return "inference"
    fw = set(req.frameworks or ())
    if fw & {"vllm", "tgi", "sglang", "llama.cpp", "llama-cpp-python", "tensorrt-llm"}:
        return "inference"
    if fw & {"peft", "trl", "deepspeed", "unsloth", "axolotl"}:
        return "training"
    return None


def profile(req) -> dict:
    """Everything the formulas need, each value with where it came from. Pure (catalog is static)."""
    from intelligence import models_catalog

    A: list[str] = []          # assumptions, in plain words
    known: dict[str, str] = {}  # field -> 'request' | 'catalog' | 'assumed'
    m = req.model
    model = {k: _d(m, k) for k in ("name", "architecture", "parameters_b", "active_parameters_b", "num_layers",
                                   "hidden_size", "num_attention_heads", "num_kv_heads", "head_dim", "vocab_size",
                                   "max_context")}
    for k, v in model.items():
        if v is not None:
            known[k] = "request"
    cat = models_catalog.lookup(model["name"]) if model["name"] else None
    entry = None
    if cat:
        entry = cat["entry"]
        filled = []
        for k in ("parameters_b", "active_parameters_b", "num_layers", "hidden_size", "num_attention_heads",
                  "num_kv_heads", "head_dim", "vocab_size", "max_context"):
            if model[k] is None and entry.get(k) is not None:
                model[k] = entry[k]
                known[k] = "catalog"
                filled.append(k)
        if filled:
            how = "matched" if cat["match"] == "exact" else f"loosely matched (candidates: {', '.join(cat['candidates'])})"
            A.append(f"model {model['name']!r} {how} to {entry['id']}: {', '.join(filled)} from its published config "
                     f"({entry['source']})")
    kind = entry["kind"] if entry else None
    arch = (model["architecture"] or "").lower()
    if kind is None and arch:
        if arch in ("bert", "roberta", "deberta", "deberta-v2", "electra", "distilbert"):
            kind = "encoder"
        elif arch in ("vit", "deit", "swin"):
            kind = "vit"
        elif arch in ("resnet", "convnext", "efficientnet", "cnn", "vgg"):
            kind = "cnn"
        elif arch in ("mixtral", "qwen2_moe", "deepseek_v2", "deepseek_v3", "dbrx", "olmoe"):
            kind = "moe"
        elif arch:
            kind = "decoder" if model["num_layers"] and model["hidden_size"] else None
    if kind is None and model["num_layers"] and model["hidden_size"] and model["vocab_size"]:
        kind = "decoder"
        A.append("architecture not stated: treated as a decoder-only transformer (layers, hidden size and vocab given)")
    if model["head_dim"] is None and model["hidden_size"] and model["num_attention_heads"]:
        model["head_dim"] = model["hidden_size"] // model["num_attention_heads"]
        known["head_dim"] = "derived"
    if model["num_kv_heads"] is None and model["num_attention_heads"] and kind in TRANSFORMER_KINDS:
        model["num_kv_heads"] = model["num_attention_heads"]
        known["num_kv_heads"] = "assumed"
        A.append("KV heads not stated: assumed equal to attention heads (no grouped-query attention; the KV cache "
                 "is then an upper bound)")
    if model["parameters_b"] is None and model["num_layers"] and model["hidden_size"] and kind in TRANSFORMER_KINDS:
        L, h, V = model["num_layers"], model["hidden_size"], model["vocab_size"] or 0
        model["parameters_b"] = (12 * L * h * h + V * h) / 1e9
        known["parameters_b"] = "derived"
        A.append(f"parameter count not stated: approximated as 12*layers*hidden^2 + vocab*hidden = "
                 f"{model['parameters_b']:.2f}B (Kaplan et al. 2020; ignores grouped-query attention and gated MLPs)")

    wc = workload_class(req)
    tr, inf = req.training, req.inference
    frameworks = set(req.frameworks or ())
    out = {"assumptions": A, "known": known, "model": model, "kind": kind, "catalog": entry,
           "catalog_match": cat["match"] if cat else None, "catalog_candidates": cat["candidates"] if cat else [],
           "workload_class": wc, "workload_type": req.workload_type, "frameworks": sorted(frameworks)}

    precision, quant = req.precision, req.quantization
    if precision in ("int8", "int4") and quant in (None, "none"):
        quant = precision
        precision = None
    engine = _d(inf, "engine") or ("vllm" if "vllm" in frameworks else None)
    out["engine"] = engine
    out["usable_fraction"] = VLLM_USABLE if engine == "vllm" else 1.0
    if engine == "vllm":
        A.append("vLLM plans memory against 90% of VRAM (gpu_memory_utilization 0.9 default): fits are checked "
                 "against 0.9 x VRAM")

    if wc == "training":
        method = _d(tr, "method")
        if method is None:
            if req.workload_type == "training":
                method = "full"
                A.append("training method not stated: full training assumed (workload_type 'training')")
            elif quant in ("nf4", "int4") or ("bitsandbytes" in frameworks and "peft" in frameworks):
                method = "qlora"
                A.append("fine-tuning method not stated: QLoRA assumed (4-bit quantization / bitsandbytes + peft)")
            elif "peft" in frameworks:
                method = "lora"
                A.append("fine-tuning method not stated: LoRA assumed (peft in the project)")
            else:
                method = "full"
                A.append("fine-tuning method not stated: full fine-tuning assumed (the most memory; LoRA needs far less)")
            known["training.method"] = "assumed"
        out["method"] = {"full": "training_full", "lora": "training_lora", "qlora": "training_qlora"}[method]
        if precision is None:
            precision = "bf16"
            A.append("precision not stated: bf16 mixed precision assumed")
            known["precision"] = "assumed"
        if method == "full" and quant not in (None, "none"):
            A.append(f"quantization {quant!r} ignored for full training: weights are trained in {precision}")
            quant = None
        if method == "qlora":
            if quant in (None, "none"):
                quant = "nf4"
                A.append("QLoRA base weights assumed NF4 4-bit with double quantization (Dettmers et al. 2023)")
            if precision == "fp32":
                precision = "bf16"
        out["precision"], out["quantization"] = precision, quant
        opt = _d(tr, "optimizer")
        if opt is None:
            opt = "adamw"
            A.append("optimizer not stated: AdamW assumed (fp32 m and v states)")
        out["optimizer"] = opt
        rank = _d(tr, "lora_rank")
        if method in ("lora", "qlora") and rank is None:
            rank = 16
            A.append("LoRA rank not stated: r=16 assumed on the q, k, v, o attention projections")
        elif method in ("lora", "qlora"):
            A.append(f"LoRA adapters assumed on the q, k, v, o attention projections (r={rank}); adding the MLP "
                     "projections roughly doubles-to-triples the (small) adapter memory")
        out["lora_rank"] = rank
        big = (model["parameters_b"] or 0) >= 1
        b = _d(tr, "micro_batch_size")
        if b is None:
            out["batch"] = (1, 4, 8)
            A.append("micro batch size not stated: 4 per GPU assumed (range 1-8)")
            known["training.micro_batch_size"] = "assumed"
        else:
            out["batch"] = (b, b, b)
        s = _d(tr, "seq_len")
        if kind == "vit":
            s = s or model["max_context"]
        if s is None and kind in TRANSFORMER_KINDS:
            mc = model["max_context"]
            c = min(mc, 2048) if mc else 2048
            out["seq"] = (min(512, c), c, min(mc, 4096) if mc else 4096)
            A.append(f"sequence length not stated: {c} tokens assumed (range {out['seq'][0]}-{out['seq'][2]})")
            known["training.seq_len"] = "assumed"
        else:
            out["seq"] = (s, s, s)
        ck = _d(tr, "gradient_checkpointing")
        if ck is None and kind in TRANSFORMER_KINDS:
            c = big
            out["ckpt"] = (c, c, c)
            A.append(f"gradient checkpointing not stated: {'on' if c else 'off'} assumed "
                     f"({'usual for billion-parameter fine-tuning recipes' if c else 'small model'}); "
                     f"{'turning it off multiplies stored activations roughly 17x' if c else 'turning it on cuts activations'}")
            known["training.gradient_checkpointing"] = "assumed"
        else:
            out["ckpt"] = (bool(ck),) * 3
        if kind in TRANSFORMER_KINDS:
            out["flash"] = (True, True, True)
            A.append("attention assumed FlashAttention / PyTorch SDPA (attention scores not stored); eager attention "
                     "adds Korthikanti's 5*a*s/h term per layer")
        out["zero_stage"] = _d(tr, "zero_stage")
        out["cpu_offload"] = bool(_d(tr, "cpu_offload"))
        if _d(tr, "global_batch_size"):
            A.append("global batch size does not change per-GPU memory (gradient accumulation); micro batch does")
    elif wc == "inference":
        out["method"] = "inference"
        if precision is None and quant in (None, "none"):
            precision = "bf16"
            A.append("precision not stated: bf16 weights assumed (no quantization)")
            known["precision"] = "assumed"
        out["precision"], out["quantization"] = precision or "bf16", quant
        ctx = _d(inf, "context_len")
        mc = model["max_context"]
        if ctx is None and kind in LLM_KINDS:
            c = min(mc, 4096) if mc else 4096
            out["context"] = (min(c, 2048), c, min(mc, 8192) if mc else 8192)
            A.append(f"context length not stated: {c} tokens per sequence assumed (range {out['context'][0]}-"
                     f"{out['context'][2]})")
            known["inference.context_len"] = "assumed"
        else:
            out["context"] = (ctx, ctx, ctx)
        cc = _d(inf, "concurrency")
        if cc is None and kind in LLM_KINDS:
            out["concurrency"] = (1, 1, 8)
            A.append("concurrency not stated: 1 sequence at a time assumed (range 1-8)")
            known["inference.concurrency"] = "assumed"
        else:
            out["concurrency"] = (cc or 1,) * 3
        if quant == "fp8":
            A.append("fp8 weights: KV cache assumed 16-bit (vLLM kv_cache_dtype 'auto')")
        out["zero_stage"], out["cpu_offload"] = None, False
    else:
        out["method"] = None
        out["precision"], out["quantization"] = precision, quant
    return out


# --------------------------------------------------------------------------
# Components
# --------------------------------------------------------------------------

def _knob(p: dict, name: str, scenario: int):
    v = p.get(name)
    return None if v is None else v[scenario]


def components(p: dict, scenario: int = 1) -> dict:
    """Unsharded component bytes for scenario 0 (low), 1 (central) or 2 (high). None = not estimable.
    Also returns notes on what could not be estimated."""
    m, kind = p["model"], p["kind"]
    P = (m["parameters_b"] or 0) * 1e9
    notes: list[str] = []
    out = {"weights": 0.0, "gradients": 0.0, "optimizer": 0.0, "activations": 0.0, "kv_cache": 0.0}
    L, h, a, kv, hd, V = (m["num_layers"], m["hidden_size"], m["num_attention_heads"], m["num_kv_heads"], m["head_dim"],
                          m["vocab_size"])
    bpp = weight_bytes_per_param(p["precision"], p["quantization"])
    quantized = (p["quantization"] or "none") not in ("none",)
    if quantized and V and h:
        emb = 2 * V * h                 # input embedding + output head kept 16-bit
        weights = (P - emb) * bpp + emb * 2.0 if P > emb else P * bpp
    else:
        weights = P * bpp
    method = p["method"]
    if method in ("training_full", "training_lora", "training_qlora"):
        master = p["precision"] in ("bf16", "fp16", "fp8")
        if method == "training_full":
            grad_b = 4.0 if p["precision"] == "fp32" else 2.0
            out["weights"] = weights
            out["gradients"] = P * grad_b
            out["optimizer"] = P * optimizer_bytes_per_param(p["optimizer"], master_fp32=master, hidden_size=h)
        else:
            if L and h and a and kv and hd:
                T = lora_trainable_params(p["lora_rank"], layers=L, hidden_size=h, num_heads=a, num_kv_heads=kv,
                                          head_dim=hd)
            else:
                T = None
                notes.append("LoRA adapter size not computed (layers / hidden size / heads unknown): adapter "
                             "weights, gradients and optimizer states not included")
            p.setdefault("_trainable", T)
            out["weights"] = weights + (T or 0) * ADAPTER_BYTES
            out["gradients"] = (T or 0) * ADAPTER_BYTES
            out["optimizer"] = (T or 0) * optimizer_bytes_per_param(p["optimizer"], master_fp32=False, hidden_size=h)
            if T is None:
                out["gradients"] = out["optimizer"] = None
        b = _knob(p, "batch", scenario)
        act_bytes = 4 if p["precision"] == "fp32" else 2
        if kind in TRANSFORMER_KINDS and L and h and a and _knob(p, "seq", scenario):
            out["activations"] = transformer_activation_bytes(
                seq=_knob(p, "seq", scenario), batch=b, hidden=h, heads=a, layers=L, flash=_knob(p, "flash", scenario),
                checkpointing=_knob(p, "ckpt", scenario), vocab=V if kind != "vit" else None, act_bytes=act_bytes)
        elif kind == "cnn" and p["catalog"] and p["catalog"].get("activation_elements_per_sample"):
            key = "activation_elements_per_sample_high" if scenario == 2 else "activation_elements_per_sample"
            out["activations"] = p["catalog"][key] * b * act_bytes
        else:
            out["activations"] = None
            notes.append("activation memory not estimated: no per-layer formula for this model without its "
                         "architecture (layers, hidden size, heads, sequence length); the total is a lower bound")
    elif method == "inference":
        out["weights"] = weights
        ctx, cc = _knob(p, "context", scenario), _knob(p, "concurrency", scenario)
        if kind in LLM_KINDS:
            if L and kv and hd and ctx:
                kv_b = 4 if p["precision"] == "fp32" else 2
                out["kv_cache"] = kv_cache_bytes(layers=L, kv_heads=kv, head_dim=hd, context=ctx, concurrency=cc,
                                                 bytes_per_value=kv_b)
            else:
                out["kv_cache"] = None
                notes.append("KV cache not estimated (layers / KV heads / head dim / context unknown): the total is "
                             "a lower bound")
    return {"bytes": out, "notes": notes}


def per_gpu(p: dict, comp: dict, n: int) -> dict:
    """Per-GPU bytes on n GPUs (sharding per the module docstring) + overhead."""
    c = dict(comp)
    zero, parallel = None, "single GPU"
    if n > 1:
        if p["method"] == "inference":
            parallel = f"tensor parallel x{n}"
            c["weights"] = c["weights"] / n
            if c["kv_cache"]:
                c["kv_cache"] = c["kv_cache"] / n
        else:
            zero = p.get("zero_stage")
            if zero is None:
                zero = 3
            parallel = f"ZeRO-{zero} / FSDP data parallel x{n}" if zero else f"data parallel x{n} (no sharding)"
            if zero >= 3:
                c["weights"] = c["weights"] / n
            if zero >= 2 and c["gradients"]:
                c["gradients"] = c["gradients"] / n
            if zero >= 1 and c["optimizer"]:
                c["optimizer"] = c["optimizer"] / n
    if p.get("cpu_offload") and c.get("optimizer"):
        c["optimizer"] = 0.0
    other = sum(v for v in c.values() if v)
    c["framework_overhead"] = framework_overhead_bytes(other)
    total = sum(v for v in c.values() if v)
    return {"bytes": c, "total": total, "zero_stage": zero, "parallelism": parallel}


def _gb(x):
    return None if x is None else round(x / GB, 2)


def estimate_memory(req, *, vendor: str | None = None) -> dict:
    """Contract section 2 `memory`, plus gpu_count / interconnect / per-count needs. Pure."""
    p = profile(req)
    A = p["assumptions"]
    m = p["model"]
    base = {"kind": "estimated", "margin_pct": round(MARGIN * 100), "usable_vram_fraction": p["usable_fraction"],
            "sources": SOURCES}
    if p["method"] is None or not m["parameters_b"]:
        why = ("workload type unknown" if p["method"] is None and p["workload_class"] is None
               else "no GPU workload" if p["workload_class"] == "no_gpu" else "parameter count unknown")
        return {**base, "method": "unavailable", "components_gb": None, "min_total_gb": None, "recommended_gb": None,
                "range_gb": None, "per_gpu_gb": None, "confidence": "none", "assumptions": A,
                "unavailable_reason": why, "per_gpu_by_count": {}, "profile": _public_profile(p),
                "gpu_count": {"min": None, "suggested": None, "reason": f"not estimated: {why}"},
                "interconnect": {"needed": None, "reason": f"not estimated: {why}"}}

    scen = [components(p, i) for i in range(3)]
    notes = scen[1]["notes"]
    for n_ in notes:
        if n_ not in A:
            A.append(n_)
    tier = largest_vram_gb(vendor) * p["usable_fraction"]
    max_gpus = None
    if req.constraints is not None and req.constraints.max_gpus:
        max_gpus = req.constraints.max_gpus
    counts = [n for n in COUNTS if max_gpus is None or n <= max_gpus]
    if req.hardware_hints is not None and req.hardware_hints.gpu_count and req.hardware_hints.gpu_count not in counts:
        if max_gpus is None or req.hardware_hints.gpu_count <= max_gpus:
            counts = sorted(set(counts) | {req.hardware_hints.gpu_count})
    by_count = {}
    for n in counts:
        lo, mid, hi = (per_gpu(p, s["bytes"], n) for s in scen)
        by_count[n] = {"min_gb": _gb(mid["total"]), "recommended_gb": _gb(mid["total"] * (1 + MARGIN)),
                       "range_gb": [_gb(lo["total"]), _gb(hi["total"])],
                       "components_gb": {k: _gb(v) for k, v in mid["bytes"].items()},
                       "zero_stage": mid["zero_stage"], "parallelism": mid["parallelism"]}
    fit_min = next((n for n in counts if by_count[n]["min_gb"] <= tier), None)
    fit_rec = next((n for n in counts if by_count[n]["recommended_gb"] <= tier), None)
    suggested = fit_rec or fit_min
    if suggested is None:
        gc = {"min": None, "suggested": None,
              "reason": (f"even {counts[-1]} GPUs need {by_count[counts[-1]]['min_gb']:g} GB each, above the largest "
                         f"GPU on record ({tier:g} GB usable): needs multi-node or model-parallel training, which "
                         "OpenGrid does not plan" + ("" if p.get("zero_stage") != 0 else
                                                      "; ZeRO stage 0 replicates the model on every GPU"))}
    else:
        gc = {"min": fit_min, "suggested": suggested,
              "reason": (f"smallest GPU count (of {', '.join(map(str, counts))}) whose per-GPU need "
                         f"({by_count[suggested]['recommended_gb']:g} GB with {round(MARGIN * 100)}% margin) fits the "
                         f"largest GPU on record ({tier:g} GB usable)")
              + ("" if suggested == 1 else f"; sharding: {by_count[suggested]['parallelism']}")}
    n_for = suggested or 1
    sel = by_count.get(n_for) or by_count[counts[0]]
    one = by_count[counts[0]]
    if suggested and suggested > 1:
        if p["method"] == "inference":
            ic = {"needed": True, "reason": f"tensor parallelism across {suggested} GPUs exchanges activations every "
                                            "layer: NVLink / Infinity Fabric strongly preferred over PCIe"}
        else:
            ic = {"needed": True, "reason": f"sharded training (ZeRO-3 / FSDP) across {suggested} GPUs gathers weights "
                                            "and reduces gradients every step: NVLink / SXM strongly preferred"}
    elif suggested == 1:
        ic = {"needed": False, "reason": "fits on one GPU: no GPU-to-GPU interconnect needed"}
    else:
        ic = {"needed": None, "reason": gc["reason"]}

    unknown_component = any(v is None for v in scen[1]["bytes"].values())
    uncertain = any(k for k, src in p["known"].items() if src == "assumed")
    if unknown_component or (p["catalog_match"] == "loose" and len(p["catalog_candidates"]) > 1) \
            or p["known"].get("parameters_b") == "derived":
        conf = "low"
    elif uncertain:
        conf = "medium"
    else:
        conf = "high"
    rng = sel["range_gb"]
    return {**base, "method": p["method"], "components_gb": sel["components_gb"],
            "min_total_gb": sel["min_gb"], "recommended_gb": sel["recommended_gb"],
            "range_gb": rng if rng[0] != rng[1] else None, "per_gpu_gb": sel["recommended_gb"],
            "single_gpu_total_gb": one["min_gb"], "for_gpu_count": n_for,
            "lower_bound": unknown_component, "confidence": conf, "assumptions": A,
            "per_gpu_by_count": by_count, "trainable_parameters": p.get("_trainable") if p["method"] in (
                "training_lora", "training_qlora") else (round(m["parameters_b"] * 1e9) if p["method"] == "training_full"
                                                         else 0),
            "profile": _public_profile(p), "gpu_count": gc, "interconnect": ic}


def _public_profile(p: dict) -> dict:
    keys = ("workload_class", "method", "kind", "precision", "quantization", "optimizer", "lora_rank", "engine",
            "zero_stage", "cpu_offload", "batch", "seq", "ckpt", "context", "concurrency", "catalog_match")
    out = {k: p.get(k) for k in keys if k in p}
    out["model"] = dict(p["model"])
    out["catalog_id"] = p["catalog"]["id"] if p.get("catalog") else None
    out["catalog_source"] = p["catalog"]["source"] if p.get("catalog") else None
    out["known"] = dict(p["known"])
    return out

