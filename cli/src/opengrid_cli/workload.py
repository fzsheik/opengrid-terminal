"""WorkloadRequirements (docs/OPENGRID2_CONTRACTS.md section 1) on the client side.

The server's `intelligence.requirements.WorkloadRequirements` is the authority; this mirror exists so the MCP
tools publish a precise input schema and the CLI can reject an obviously malformed object before sending it.
Every field is optional: unknown is null, never a guess. Nothing here estimates memory or ranks GPUs: that
is the server's job (POST /v1/recommend/estimate, POST /v1/recommend).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

WorkloadType = Literal["training", "fine_tuning", "inference", "batch", "no_gpu"]
Precision = Literal["fp32", "bf16", "fp16", "fp8", "int8", "int4"]
Quantization = Literal["none", "int8", "int4", "nf4", "gptq", "awq", "fp8"]
Method = Literal["full", "lora", "qlora"]
Optimizer = Literal["adamw", "adamw_8bit", "adafactor", "sgd"]
Engine = Literal["vllm", "tgi", "transformers", "llama.cpp"]
Confidence = Literal["high", "medium", "low"]


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelInfo(_M):
    name: str | None = Field(None, max_length=200, description="Hugging Face model id or free text, e.g. meta-llama/Llama-3.1-8B")
    architecture: str | None = Field(None, max_length=64, description="HF model_type, e.g. llama")
    parameters_b: float | None = Field(None, gt=0, le=10000, description="total parameters in billions (MoE: total)")
    active_parameters_b: float | None = Field(None, gt=0, le=10000, description="MoE only: active parameters, billions")
    num_layers: int | None = Field(None, ge=1, le=10000)
    hidden_size: int | None = Field(None, ge=1, le=1_000_000)
    num_attention_heads: int | None = Field(None, ge=1, le=100_000)
    num_kv_heads: int | None = Field(None, ge=1, le=100_000)
    head_dim: int | None = Field(None, ge=1, le=100_000)
    vocab_size: int | None = Field(None, ge=1, le=10_000_000)
    max_context: int | None = Field(None, ge=1, le=100_000_000)


class Training(_M):
    method: Method | None = Field(None, description="full fine-tuning, LoRA or QLoRA")
    optimizer: Optimizer | None = None
    micro_batch_size: int | None = Field(None, ge=1, le=1_000_000)
    global_batch_size: int | None = Field(None, ge=1, le=100_000_000)
    seq_len: int | None = Field(None, ge=1, le=100_000_000)
    gradient_checkpointing: bool | None = None
    lora_rank: int | None = Field(None, ge=1, le=65536)
    zero_stage: Literal[0, 1, 2, 3] | None = None
    cpu_offload: bool | None = None
    epochs: float | None = Field(None, gt=0, le=100_000)
    dataset_tokens: float | None = Field(None, ge=0)
    dataset_samples: int | None = Field(None, ge=0)


class Inference(_M):
    concurrency: int | None = Field(None, ge=1, le=1_000_000)
    context_len: int | None = Field(None, ge=1, le=100_000_000)
    output_tokens: int | None = Field(None, ge=1, le=100_000_000)
    target_latency_ms: float | None = Field(None, gt=0)
    target_tokens_per_s: float | None = Field(None, gt=0)
    engine: Engine | None = None


class Constraints(_M):
    budget_usd: float | None = Field(None, gt=0, le=10_000_000, description="total budget in USD")
    max_price_per_gpu_hour: float | None = Field(None, gt=0, le=1000)
    runtime_hours: float | None = Field(None, gt=0, le=100_000)
    region: str | None = Field(None, max_length=32, description="region group, e.g. us, eu")
    max_gpus: int | None = Field(None, ge=1, le=4096)
    allow_spot: bool | None = None
    vendor: Literal["NVIDIA", "AMD"] | None = None
    include_providers: list[str] = Field(default_factory=list, max_length=100)
    exclude_providers: list[str] = Field(default_factory=list, max_length=100)


class HardwareHints(_M):
    gpus_mentioned: list[str] = Field(default_factory=list, max_length=50)
    gpu_count: int | None = Field(None, ge=1, le=4096)
    cuda_min: str | None = Field(None, max_length=16)


class Evidence(_M):
    field: str = Field(..., max_length=100)
    value: str | float | int | bool | list[str | float | int] | None = None
    source: str = Field(..., max_length=300, description="file:line or 'user'; never file contents")
    confidence: Confidence | None = None


class WorkloadRequirements(_M):
    """What a workload needs. Only this object ever leaves the machine (never file contents)."""
    workload_type: WorkloadType | None = None
    frameworks: list[str] = Field(default_factory=list, max_length=50)
    model: ModelInfo | None = None
    precision: Precision | None = None
    quantization: Quantization | None = None
    training: Training | None = None
    inference: Inference | None = None
    constraints: Constraints | None = None
    hardware_hints: HardwareHints | None = None
    evidence: list[Evidence] = Field(default_factory=list, max_length=200)
    source: Literal["analyzer", "form", "assistant", "api"] | None = None


def validate(req: dict) -> dict:
    """Validate against the contract mirror; returns the JSON-ready dict without null / empty fields."""
    return compact(WorkloadRequirements.model_validate(req).model_dump(mode="json"))


def compact(obj: Any) -> Any:
    """Drop None values and empty containers (the server treats a missing field as unknown)."""
    if isinstance(obj, dict):
        out = {k: compact(v) for k, v in obj.items()}
        return {k: v for k, v in out.items() if v is not None and v != {} and v != []}
    if isinstance(obj, list):
        return [compact(v) for v in obj]
    return obj


def merge(base: dict, override: dict) -> dict:
    """Deep merge: `override` (explicit flags) wins over `base` (what the analyzer found)."""
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        else:
            out[k] = v
    return out


def from_flags(*, params_b=None, model=None, method=None, workload=None, precision=None, quantization=None,
               seq_len=None, batch=None, lora_rank=None, gradient_checkpointing=None, context_len=None,
               concurrency=None, engine=None, budget=None, max_price=None, runtime_hours=None, region=None,
               vendor=None, max_gpus=None, gpus=None, frameworks=None) -> dict:
    """Requirements from explicit CLI flags (source 'api'). Only what was given is set."""
    req: dict = {}
    if method == "inference":
        workload = workload or "inference"
        method = None
    if method in ("full", "lora", "qlora"):
        workload = workload or "fine_tuning"
    if workload:
        req["workload_type"] = workload
    m = {"name": model, "parameters_b": params_b}
    if any(v is not None for v in m.values()):
        req["model"] = m
    if precision:
        req["precision"] = precision
    if quantization:
        req["quantization"] = quantization
    t = {"method": method, "seq_len": seq_len, "micro_batch_size": batch, "lora_rank": lora_rank,
         "gradient_checkpointing": gradient_checkpointing}
    if any(v is not None for v in t.values()):
        req["training"] = t
    i = {"context_len": context_len, "concurrency": concurrency, "engine": engine}
    if any(v is not None for v in i.values()):
        req["inference"] = i
    c = {"budget_usd": budget, "max_price_per_gpu_hour": max_price, "runtime_hours": runtime_hours,
         "region": region, "vendor": vendor, "max_gpus": max_gpus}
    if any(v is not None for v in c.values()):
        req["constraints"] = c
    if gpus:
        req["hardware_hints"] = {"gpus_mentioned": list(gpus)}
    if frameworks:
        req["frameworks"] = list(frameworks)
    req = compact(req)
    if req:
        req["source"] = "api"
    return req
