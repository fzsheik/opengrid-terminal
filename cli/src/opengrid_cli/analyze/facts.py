"""Fact collection and the shared key vocabulary used by every extractor.

Extractors never write requirements directly: they emit ``Fact``s (a field, a small value, a
``file:line`` source and a confidence) and ``signals``. ``engine.merge`` decides the final values.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .redact import is_secret_like

# source priority (tie-break after confidence)
P_CONFIG = 6      # HF config.json, adapter_config.json, DeepSpeed/Accelerate configs, OpenGrid manifests
P_TRAINCFG = 5    # training/serving YAML configs
P_CODE = 4        # literal values in code / CLI flags in launch commands
P_DEFAULT = 3     # argparse / dataclass defaults
P_DERIVED = 2     # derived from another fact (e.g. parameter count from the model name)
P_README = 1      # README text (untrusted)

CONF_RANK = {"high": 3, "medium": 2, "low": 1}


@dataclass
class Fact:
    field: str
    value: Any
    source: str
    confidence: str
    prio: int


@dataclass
class Collector:
    facts: list[Fact] = field(default_factory=list)
    signals: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    frameworks: dict[str, str] = field(default_factory=dict)
    # nn.Module classes seen in any file (name -> (modelsize.ClassInfo, "file:line")) and names called anywhere,
    # so engine.merge can count the parameters of the model a script builds (model.py defines, train.py calls)
    module_classes: dict[str, tuple] = field(default_factory=dict)
    called: set = field(default_factory=set)
    _sig_seen: set = field(default_factory=set)

    def fact(self, fld: str, value: Any, source: str, confidence: str = "high", prio: int = P_CODE) -> None:
        if value is None:
            return
        if isinstance(value, str):
            value = value.strip()
            if not value or len(value) > 200:
                return
            if is_secret_like(value):
                self.warn(f"{source}: a secret-looking value was found where {fld} was expected; it was dropped")
                return
        self.facts.append(Fact(fld, value, source, confidence, prio))

    def signal(self, kind: str, value: Any, source: str) -> None:
        if isinstance(value, str) and len(value) > 120:
            value = value[:117] + "..."
        key = (kind, str(value), source)
        if key in self._sig_seen:
            return
        self._sig_seen.add(key)
        self.signals.append({"kind": kind, "value": value, "source": source})

    def framework(self, name: str, source: str) -> None:
        self.frameworks.setdefault(name, source)
        self.signal("framework", name, source)

    def warn(self, msg: str) -> None:
        if msg not in self.warnings:
            self.warnings.append(msg)

    def has(self, *kinds: str) -> bool:
        return any(s["kind"] in kinds for s in self.signals)

    def first(self, *kinds: str) -> dict | None:
        for s in self.signals:
            if s["kind"] in kinds:
                return s
        return None


# ------------------------------------------------------------------------------------------------
# Vocabularies

# python import name / pip distribution name -> (category, canonical name)
# categories: ml (GPU-relevant), cpu_ml (classic ML, CPU is fine), web (web/app framework)
PACKAGES: dict[str, tuple[str, str]] = {
    "torch": ("ml", "pytorch"), "torchvision": ("ml", "pytorch"), "torchaudio": ("ml", "pytorch"),
    "pytorch": ("ml", "pytorch"), "pytorch-cuda": ("ml", "pytorch"),
    "tensorflow": ("ml", "tensorflow"), "tensorflow-gpu": ("ml", "tensorflow"), "tf-keras": ("ml", "tensorflow"),
    "keras": ("ml", "tensorflow"), "tensorflow-cpu": ("cpu_ml", "tensorflow"),
    "jax": ("ml", "jax"), "jaxlib": ("ml", "jax"), "flax": ("ml", "jax"), "optax": ("ml", "jax"),
    "transformers": ("ml", "transformers"), "peft": ("ml", "peft"), "trl": ("ml", "trl"),
    "accelerate": ("ml", "accelerate"), "deepspeed": ("ml", "deepspeed"), "vllm": ("ml", "vllm"),
    "bitsandbytes": ("ml", "bitsandbytes"), "bnb": ("ml", "bitsandbytes"), "unsloth": ("ml", "unsloth"),
    "axolotl": ("ml", "axolotl"), "lightning": ("ml", "lightning"), "pytorch-lightning": ("ml", "lightning"),
    "pytorch_lightning": ("ml", "lightning"), "diffusers": ("ml", "diffusers"),
    "llama-cpp-python": ("ml", "llama.cpp"), "llama_cpp": ("ml", "llama.cpp"),
    "sentence-transformers": ("ml", "sentence-transformers"), "sentence_transformers": ("ml", "sentence-transformers"),
    "timm": ("ml", "timm"), "flash-attn": ("ml", "flash-attn"), "flash_attn": ("ml", "flash-attn"),
    "xformers": ("ml", "xformers"), "auto-gptq": ("ml", "auto-gptq"), "auto_gptq": ("ml", "auto-gptq"),
    "autoawq": ("ml", "autoawq"), "awq": ("ml", "autoawq"), "tensorrt-llm": ("ml", "tensorrt-llm"),
    "tensorrt_llm": ("ml", "tensorrt-llm"), "torchtune": ("ml", "torchtune"), "llamafactory": ("ml", "llama-factory"),
    "llmtuner": ("ml", "llama-factory"), "onnxruntime-gpu": ("ml", "onnxruntime-gpu"), "cupy": ("ml", "cupy"),
    "triton": ("ml", "triton"), "megatron": ("ml", "megatron"), "megatron-core": ("ml", "megatron"),
    "colossalai": ("ml", "colossalai"), "sglang": ("ml", "sglang"), "mlx": ("ml", "mlx"),
    "text-generation-inference": ("ml", "tgi"), "optimum": ("ml", "optimum"), "liger-kernel": ("ml", "liger-kernel"),
    "sklearn": ("cpu_ml", "scikit-learn"), "scikit-learn": ("cpu_ml", "scikit-learn"),
    "xgboost": ("cpu_ml", "xgboost"), "lightgbm": ("cpu_ml", "lightgbm"), "catboost": ("cpu_ml", "catboost"),
    "statsmodels": ("cpu_ml", "statsmodels"),
    "flask": ("web", "flask"), "django": ("web", "django"), "fastapi": ("web", "fastapi"),
    "starlette": ("web", "starlette"), "sanic": ("web", "sanic"), "tornado": ("web", "tornado"),
    "aiohttp": ("web", "aiohttp"), "bottle": ("web", "bottle"), "pyramid": ("web", "pyramid"),
    "falcon": ("web", "falcon"), "gunicorn": ("web", "gunicorn"), "uvicorn": ("web", "uvicorn"),
    "celery": ("web", "celery"), "express": ("web", "express"), "next": ("web", "next"), "react": ("web", "react"),
    "vue": ("web", "vue"), "streamlit": ("web", "streamlit"), "gradio": ("web", "gradio"),
}

GPU_NAMES = re.compile(
    r"(?<![A-Za-z0-9_])(GB200|GB300|B200|B300|H200|H100|H800|GH200|A100|A800|A10G|A10|A30|A40|A6000|A5000|A4000|"
    r"L40S|L40|L4|T4|V100|P100|RTX\s?(?:PRO\s?)?6000|RTX\s?5090|RTX\s?4090|RTX\s?3090|RTX\s?A6000|"
    r"MI300X|MI325X|MI250X?|MI210|MI355X|Gaudi\s?[23]?)(?![A-Za-z0-9_])")

MODEL_NAME_KEYS = {"model_name_or_path", "base_model", "model_name", "model_id", "pretrained_model_name_or_path",
                   "base_model_name_or_path", "_name_or_path", "model", "model-id", "pretrained_model",
                   "hf_model", "base_model_id", "model_path"}
MICRO_BATCH_KEYS = {"per_device_train_batch_size", "micro_batch_size", "train_micro_batch_size_per_gpu",
                    "batch_size", "per_device_batch_size", "train_batch_size_per_device", "per_gpu_train_batch_size",
                    "batch-size"}
GLOBAL_BATCH_KEYS = {"global_batch_size", "total_batch_size", "effective_batch_size"}
SEQ_KEYS = {"max_seq_length", "max_seq_len", "seq_len", "sequence_len", "seq_length", "max_length", "block_size",
            "cutoff_len", "model_max_length", "max_len", "context_length", "max_sequence_length", "sequence_length"}
EPOCH_KEYS = {"num_train_epochs", "num_epochs", "epochs", "max_epochs", "n_epochs"}
GPU_COUNT_KEYS = {"num_processes", "nproc_per_node", "num_gpus", "n_gpus", "gpu_count", "gpus", "devices",
                  "num_shard", "world_size", "ngpus", "num_gpu"}
GC_KEYS = {"gradient_checkpointing", "enable_activation_checkpointing", "activation_checkpointing",
           "use_gradient_checkpointing"}
DTYPE_KEYS = {"torch_dtype", "dtype", "mixed_precision", "precision", "bnb_4bit_compute_dtype", "compute_dtype"}
QUANT_KEYS = {"quantization", "quant_method", "quantize", "quantization_method"}

_EVAL_CTX = re.compile(r"(?i)(?:^|[_.\-])(eval|val|valid|validation|test|infer|inference|predict|generation)"
                       r"(?:$|[_.\-])")

_KNOWN_ORGS = {"meta-llama", "mistralai", "qwen", "google", "microsoft", "tiiuae", "eleutherai", "bigscience",
               "facebook", "openai", "stabilityai", "deepseek-ai", "nousresearch", "thebloke", "hugging-quants",
               "huggingfaceh4", "huggingfacetb", "allenai", "01-ai", "bigcode", "databricks", "mosaicml",
               "nvidia", "ibm-granite", "cohereforai", "unsloth", "teknium", "lmsys", "openlm-research",
               "black-forest-labs", "runwayml", "sentence-transformers", "intfloat", "baai", "thudm",
               "internlm", "codellama", "upstage", "snowflake", "amd", "neuralmagic", "casperhansen"}
_FAMILY = re.compile(r"(?i)llama|mistral|mixtral|qwen|gemma|phi-?\d|gpt|falcon|bert|roberta|t5|opt-|bloom|deepseek|"
                     r"\byi-|stablelm|starcoder|pythia|vicuna|zephyr|olmo|smollm|whisper|stable-diffusion|sdxl|"
                     r"flux|clip|vit|resnet|granite|command-r|dbrx|jamba|internlm|chatglm|baichuan|nemotron")


def looks_like_model_id(s: Any) -> bool:
    if not isinstance(s, str):
        return False
    s = s.strip()
    if not (2 <= len(s) <= 120) or any(c.isspace() for c in s):
        return False
    if s.startswith(("$", "{", "<", "%", "-", "_component_", "torchtune.")) or "${" in s or "{{" in s:
        return False
    low = s.lower()
    if low.endswith((".py", ".yaml", ".yml", ".json", ".txt", ".sh", ".md")):
        return False
    if low in {"none", "null", "true", "false", "auto", "required", "model", "default", "str"}:
        return False
    if re.match(r"^[A-Za-z0-9][\w.\-]*/[\w.\-]+$", s):
        return True
    if s.startswith(("./", "../", "/", "~")):
        return True
    return bool(_FAMILY.search(s))


def readme_model_candidates(line: str) -> list[str]:
    out = []
    for m in re.finditer(r"(?<![\w./-])([A-Za-z0-9][\w.\-]{1,40})/([A-Za-z0-9][\w.\-]{1,80})", line):
        org, name = m.group(1), m.group(2)
        if org.lower() in _KNOWN_ORGS or (_FAMILY.search(name) and re.search(r"\d+(?:\.\d+)?[bBmM]\b", name)):
            out.append(f"{org}/{name}".rstrip("."))
    return out


def params_from_name(name: str) -> tuple[float | None, bool]:
    """(parameters_b, is_moe) parsed from a model name like Llama-2-7b-hf or Mixtral-8x7B."""
    base = name.rsplit("/", 1)[-1]
    if re.search(r"(?i)(?<![a-z0-9])\d+x\d+(?:\.\d+)?b(?![a-z])", base):
        return None, True
    m = re.search(r"(?i)(?<![a-z0-9.])(\d+(?:\.\d+)?)\s*([bm])(?![a-z])", base)
    if not m:
        m = re.search(r"(?i)(?<=[_\-])(\d+(?:\.\d+)?)([bm])(?=[_\-]|$)", base)
    if not m:
        return None, False
    val = float(m.group(1))
    if m.group(2).lower() == "m":
        val = val / 1000.0
    if val <= 0 or val > 3000:
        return None, False
    return round(val, 3), bool(re.search(r"(?i)-a\d+(?:\.\d+)?b|moe", base))


def as_int(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str) and re.fullmatch(r"\s*\d+\s*", v):
        return int(v)
    return None


def as_number(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    return None


def as_bool(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        low = v.strip().lower()
        if low in ("true", "yes", "1", "on"):
            return True
        if low in ("false", "no", "0", "off"):
            return False
    if isinstance(v, int) and v in (0, 1):
        return bool(v)
    return None


def map_dtype(v: Any) -> str | None:
    if isinstance(v, int) and not isinstance(v, bool):
        return {16: "fp16", 32: "fp32", 64: "fp32"}.get(v)
    if not isinstance(v, str):
        return None
    s = v.strip().lower().replace("torch.", "")
    if s in ("auto", ""):
        return None
    if "bf16" in s or "bfloat16" in s:
        return "bf16"
    if "fp8" in s or "float8" in s:
        return "fp8"
    if s in ("fp16", "float16", "half", "16", "16-mixed", "16-true", "f16") or "fp16" in s:
        return "fp16"
    if s in ("fp32", "float32", "float", "32", "32-true", "full", "no", "f32", "tf32"):
        return "fp32"
    if s in ("int8",):
        return "int8"
    return None


def map_quant(v: Any) -> str | None:
    if not isinstance(v, str):
        return None
    s = v.strip().lower()
    if not s:
        return None
    if "awq" in s:
        return "awq"
    if "gptq" in s or "marlin" in s or "exl2" in s:
        return "gptq"
    if "nf4" in s:
        return "nf4"
    if "fp8" in s or "float8" in s:
        return "fp8"
    if s in ("int4", "w4a16", "fp4", "4bit", "q4", "bitsandbytes-fp4") or "q4_" in s:
        return "int4"
    if s in ("int8", "w8a8", "eetq", "8bit", "llm_int8", "bitsandbytes", "bitsandbytes-8bit") or "q8_" in s:
        return "int8"
    if s in ("none", "no", "null"):
        return "none"
    return None


def map_optimizer(v: Any) -> str | None:
    if not isinstance(v, str):
        return None
    s = v.strip().lower()
    if "8bit" in s or "8_bit" in s or "adamw8bit" in s:
        return "adamw_8bit"
    if "adafactor" in s:
        return "adafactor"
    if "adam" in s:
        return "adamw"
    if "sgd" in s:
        return "sgd"
    return None


def apply_key(c: Collector, path: tuple, value: Any, source: str, confidence: str, prio: int) -> None:
    """Map one (path, value) from a config file, CLI flag or keyword argument to facts/signals."""
    keys = [str(p).lower().replace("-", "_") for p in path if not isinstance(p, int)]
    if not keys:
        return
    last = keys[-1]
    ctx = ".".join(keys)
    in_lora = any(k in ctx for k in ("lora", "peft", "adapter"))
    in_eval = bool(_EVAL_CTX.search(".".join(keys[:-1]))) or bool(_EVAL_CTX.search(last))

    # model name
    if (last in MODEL_NAME_KEYS or (len(keys) >= 2 and keys[-2] == "model" and last in ("name", "path",
                                                                                        "pretrained", "id"))):
        if looks_like_model_id(value) and "tokenizer" not in ctx and "draft" not in ctx and "reward" not in ctx:
            c.fact("model.name", value, source, confidence, prio)
        return
    if last.strip("_") == "component" and isinstance(value, str) and value.startswith("torchtune."):
        _torchtune_component(c, ctx, value, source, prio)
        return

    if last in MICRO_BATCH_KEYS and not in_eval:
        n = as_int(value)
        if n:
            c.fact("training.micro_batch_size", n, source, confidence, prio)
        return
    if last in GLOBAL_BATCH_KEYS or last == "train_batch_size":  # DeepSpeed train_batch_size is global
        n = as_int(value)
        if n:
            c.fact("training.global_batch_size", n, source, confidence, prio)
        return
    if last == "gradient_accumulation_steps":
        n = as_int(value)
        if n:
            c.signal("gradient_accumulation_steps", n, source)
        return
    if last in SEQ_KEYS and not in_eval:
        n = as_int(value)
        if n and n >= 16:
            c.fact("training.seq_len", n, source, confidence, prio)
        return
    if last in ("max_model_len", "max_total_tokens", "max_context_len"):
        n = as_int(value)
        if n:
            c.fact("inference.context_len", n, source, confidence, prio)
        return
    if last in ("max_num_seqs", "max_concurrent_requests", "max_batch_size", "concurrency"):
        n = as_int(value)
        if n:
            c.fact("inference.concurrency", n, source, confidence, prio)
        return
    if last == "gpu_memory_utilization":
        c.signal("gpu_memory_utilization", value, source)
        return
    if last in ("tensor_parallel_size", "tp_size", "tensor_parallel_degree"):
        n = as_int(value)
        if n:
            c.signal("tensor_parallel_size", n, source)
            c.fact("hardware_hints.gpu_count", n, source, confidence, prio)
        return
    if last == "pipeline_parallel_size":
        n = as_int(value)
        if n:
            c.signal("pipeline_parallel_size", n, source)
        return
    if last in ("lora_r", "lora_rank") or (last in ("r", "rank") and in_lora):
        n = as_int(value)
        if n:
            c.fact("training.lora_rank", n, source, confidence, prio)
        return
    if last == "adapter" and isinstance(value, str) and value.lower() in ("lora", "qlora"):
        c.fact("training.method", value.lower(), source, confidence, prio)
        c.signal("peft_lora", value.lower(), source)
        return
    if last == "finetuning_type" and isinstance(value, str):
        v = value.lower()
        if v in ("lora", "full"):
            c.fact("training.method", v, source, confidence, prio)
            if v == "lora":
                c.signal("peft_lora", "finetuning_type: lora", source)
            c.signal("training_config", "finetuning_type", source)
        return
    if last == "peft_type" and isinstance(value, str) and value.upper() in ("LORA", "QLORA", "ADALORA"):
        c.signal("peft_lora", value.upper(), source)
        return
    if last in ("use_peft", "use_lora", "lora", "load_in_lora") and as_bool(value) is True:
        c.signal("peft_lora", last, source)
        return
    if last in EPOCH_KEYS:
        n = as_number(value)
        if n and n > 0:
            c.fact("training.epochs", int(n) if float(n).is_integer() else n, source, confidence, prio)
        return
    if last in ("optim", "optimizer", "optimizer_type", "type") and (last != "type" or "optimizer" in ctx):
        o = map_optimizer(value)
        if o:
            c.fact("training.optimizer", o, source, confidence, prio)
        return
    if last in ("bf16", "fp16", "fp32", "tf32") or (last == "enabled" and len(keys) >= 2
                                                    and keys[-2] in ("bf16", "fp16")):
        name = last if last != "enabled" else keys[-2]
        b = as_bool(value)
        if b is True and name in ("bf16", "fp16"):
            c.fact("precision", name, source, confidence, prio)
        return
    if last in DTYPE_KEYS:
        d = map_dtype(value)
        if d:
            c.fact("precision", d, source, confidence if last != "torch_dtype" else confidence, prio)
        return
    if last in GC_KEYS:
        b = as_bool(value)
        if b is not None:
            c.fact("training.gradient_checkpointing", b, source, confidence, prio)
        return
    if last == "load_in_4bit" and as_bool(value) is True:
        c.fact("quantization", "int4", source, confidence, prio)
        return
    if last == "load_in_8bit" and as_bool(value) is True:
        c.fact("quantization", "int8", source, confidence, prio)
        return
    if last == "bnb_4bit_quant_type" and isinstance(value, str) and value.lower() == "nf4":
        c.fact("quantization", "nf4", source, confidence, prio + 1)
        return
    if last == "quantization_bit":
        n = as_int(value)
        if n in (4, 8):
            c.fact("quantization", f"int{n}", source, confidence, prio)
        return
    if last in QUANT_KEYS:
        q = map_quant(value)
        if q:
            c.fact("quantization", q, source, confidence, prio)
        return
    if last in ("stage", "zero_stage") and ("zero" in ctx or "deepspeed" in ctx or last == "zero_stage"):
        n = as_int(value)
        if n is not None and 0 <= n <= 3:
            c.fact("training.zero_stage", n, source, confidence, prio)
        return
    if (last == "device" and ("offload_optimizer" in ctx or "offload_param" in ctx)) or last in (
            "offload_optimizer_device", "offload_param_device"):
        if isinstance(value, str):
            v = value.lower()
            if v in ("cpu", "nvme"):
                c.fact("training.cpu_offload", True, source, confidence, prio + (1 if "optimizer" in ctx else 0))
            elif v == "none":
                c.fact("training.cpu_offload", False, source, confidence, prio)
        return
    if last in ("deepspeed", "deepspeed_config_file", "deepspeed_config") and isinstance(value, str):
        c.signal("deepspeed_config_ref", value, source)
        return
    if last == "distributed_type" and isinstance(value, str):
        c.signal("distributed_type", value, source)
        return
    if last in GPU_COUNT_KEYS or (last == "count" and "devices" in ctx):
        n = as_int(value)
        if n and n > 0:
            c.fact("hardware_hints.gpu_count", n, source, confidence, prio)
        return
    if last in ("gpu", "gpu_type", "accelerator", "gpu_name", "instance_type") and isinstance(value, str):
        for m in GPU_NAMES.finditer(value):
            c.signal("gpu_mentioned", m.group(1).replace(" ", ""), source)
        return
    if last in ("num_local_experts", "num_experts", "n_routed_experts"):
        n = as_int(value)
        if n and n > 1:
            c.signal("moe", n, source)
        return
    if last in ("dataset_samples", "num_samples", "max_samples", "train_samples"):
        n = as_int(value)
        if n and not in_eval:
            c.fact("training.dataset_samples", n, source, "medium", prio)
        return


def _torchtune_component(c: Collector, ctx: str, value: str, source: str, prio: int) -> None:
    c.framework("torchtune", source)
    m = re.match(r"torchtune\.models\.([a-z0-9_]+)\.(q?lora_)?([a-z0-9_]+?)_(\d+(?:_\d+)?)b(?:_[a-z]+)?$", value)
    if ctx.startswith("model") and m:
        arch = re.sub(r"\d.*$", "", m.group(1)) or m.group(1)
        c.fact("model.architecture", arch, source, "medium", prio)
        c.fact("model.parameters_b", float(m.group(4).replace("_", ".")), source, "medium", prio)
        c.signal("training_config", "torchtune recipe", source)
        if m.group(2):
            meth = "qlora" if m.group(2).startswith("q") else "lora"
            c.fact("training.method", meth, source, "high", prio)
            c.signal("peft_lora", meth, source)
        return
    if "optimizer" in ctx:
        o = map_optimizer(value.rsplit(".", 1)[-1])
        if o:
            c.fact("training.optimizer", o, source, "high", prio)
