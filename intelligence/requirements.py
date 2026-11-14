"""WorkloadRequirements: the request body of /v1/recommend and /v1/recommend/estimate (contract section 1).

Every field is optional: unknown is None, never a guess. extra="forbid" everywhere, so a typo is a 422, not a
silently ignored field. Bounds keep a request small and the arithmetic finite; they are sanity limits, not
claims about what is runnable.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

_STR = 200          # free-text field cap
_LIST = 32          # list cap (frameworks, providers, evidence, gpus mentioned)

WorkloadType = Literal["training", "fine_tuning", "inference", "batch", "no_gpu"]
Precision = Literal["fp32", "bf16", "fp16", "fp8", "int8", "int4"]
Quantization = Literal["none", "int8", "int4", "nf4", "gptq", "awq", "fp8"]
Method = Literal["full", "lora", "qlora"]
Optimizer = Literal["adamw", "adamw_8bit", "adafactor", "sgd"]
Engine = Literal["vllm", "tgi", "transformers", "llama.cpp"]
Confidence = Literal["high", "medium", "low"]
Source = Literal["analyzer", "form", "assistant", "api"]


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelInfo(_M):
    name: str | None = Field(None, max_length=_STR)
    architecture: str | None = Field(None, max_length=64)
    parameters_b: float | None = Field(None, gt=0, le=10_000)          # billions, total (MoE: total)
    active_parameters_b: float | None = Field(None, gt=0, le=10_000)   # MoE only
    num_layers: int | None = Field(None, ge=1, le=1_000)
    hidden_size: int | None = Field(None, ge=1, le=131_072)
    num_attention_heads: int | None = Field(None, ge=1, le=4_096)
    num_kv_heads: int | None = Field(None, ge=1, le=4_096)
    head_dim: int | None = Field(None, ge=1, le=4_096)
    vocab_size: int | None = Field(None, ge=1, le=10_000_000)
    max_context: int | None = Field(None, ge=1, le=100_000_000)


class Training(_M):
    method: Method | None = None
    optimizer: Optimizer | None = None
    micro_batch_size: int | None = Field(None, ge=1, le=65_536)
    global_batch_size: int | None = Field(None, ge=1, le=10_000_000)
    seq_len: int | None = Field(None, ge=1, le=10_000_000)
    gradient_checkpointing: bool | None = None
    lora_rank: int | None = Field(None, ge=1, le=4_096)
    zero_stage: Literal[0, 1, 2, 3] | None = None
    cpu_offload: bool | None = None
    epochs: float | None = Field(None, gt=0, le=100_000)
    dataset_tokens: float | None = Field(None, ge=0, le=1e15)
    dataset_samples: float | None = Field(None, ge=0, le=1e13)
    max_steps: int | None = Field(None, ge=1, le=100_000_000)   # optimizer steps (HF Trainer max_steps); with
    #                                                             global_batch_size x seq_len it gives the tokens


class Inference(_M):
    concurrency: int | None = Field(None, ge=1, le=100_000)
    context_len: int | None = Field(None, ge=1, le=100_000_000)
    output_tokens: int | None = Field(None, ge=1, le=10_000_000)
    target_latency_ms: float | None = Field(None, gt=0, le=1e7)
    target_tokens_per_s: float | None = Field(None, gt=0, le=1e9)
    engine: Engine | None = None


class Constraints(_M):
    budget_usd: float | None = Field(None, gt=0, le=10_000_000)
    max_price_per_gpu_hour: float | None = Field(None, gt=0, le=10_000)
    runtime_hours: float | None = Field(None, gt=0, le=100_000)
    deadline_hours: float | None = Field(None, gt=0, le=100_000)   # finish within: options estimated slower are flagged
    region: str | None = Field(None, max_length=32)
    max_gpus: int | None = Field(None, ge=1, le=1_024)
    allow_spot: bool | None = None  # null = not stated: treated as on-demand only
    vendor: Literal["NVIDIA", "AMD"] | None = None
    include_providers: list[str] = Field(default_factory=list, max_length=_LIST)
    exclude_providers: list[str] = Field(default_factory=list, max_length=_LIST)

    @field_validator("include_providers", "exclude_providers")
    @classmethod
    def _providers(cls, v: list[str]) -> list[str]:
        out = []
        for p in v:
            p = (p or "").strip().lower()
            if not p or len(p) > 64:
                raise ValueError("provider names are 1-64 characters")
            out.append(p)
        return out


class HardwareHints(_M):
    gpus_mentioned: list[str] = Field(default_factory=list, max_length=_LIST)
    gpu_count: int | None = Field(None, ge=1, le=1_024)
    cuda_min: str | None = Field(None, pattern=r"^\d{1,2}(\.\d{1,2})?$")

    @field_validator("gpus_mentioned")
    @classmethod
    def _gpus(cls, v: list[str]) -> list[str]:
        if any(len(g or "") > 64 for g in v):
            raise ValueError("GPU names are at most 64 characters")
        return v


class Evidence(_M):
    field: str = Field(..., max_length=_STR)
    value: str | int | float | bool | None = None
    source: str | None = Field(None, max_length=_STR)
    confidence: Confidence | None = None

    @field_validator("value")
    @classmethod
    def _value(cls, v):
        if isinstance(v, str) and len(v) > _STR:
            raise ValueError(f"evidence value is at most {_STR} characters")
        return v


Modality = Literal["text", "image", "audio", "video", "multimodal"]
MediaTask = Literal["understanding", "captioning", "transcription", "speech_synthesis", "generation"]
_RES = r"^\d{2,5}[xX]\d{2,5}$"     # "WIDTHxHEIGHT" in pixels, e.g. "1280x720"
MAX_SIDE = 8192


def parse_resolution(v: str | None) -> tuple[int, int] | None:
    """'1280x720' -> (1280, 720); None stays None. Validated by the pattern and Media._res."""
    if not v:
        return None
    w, h = v.lower().split("x")
    return int(w), int(h)


class Media(_M):
    """Audio, image, video and multimodal inputs and outputs (methodology/recommendations.md, "Audio, image and video
    workloads"). Every field is optional; a request without `media` is estimated exactly as before."""
    modality: list[Modality] = Field(default_factory=list, max_length=5)   # one value or a set; "multimodal" = image+text
    task: MediaTask | None = None
    images_per_request: int | None = Field(None, ge=1, le=256)
    image_resolution: str | None = Field(None, pattern=_RES)
    audio_seconds: float | None = Field(None, gt=0, le=86_400)        # per item (input for ASR, output for TTS)
    total_audio_hours: float | None = Field(None, gt=0, le=10_000_000)  # the whole batch
    video_seconds: float | None = Field(None, gt=0, le=86_400)        # input video per request (understanding)
    fps: float | None = Field(None, gt=0, le=240)                     # frames sampled per second of input video
    video_resolution: str | None = Field(None, pattern=_RES)
    output_video_seconds: float | None = Field(None, gt=0, le=600)    # generation: length of each video
    output_fps: float | None = Field(None, gt=0, le=240)
    output_resolution: str | None = Field(None, pattern=_RES)
    diffusion_steps: int | None = Field(None, ge=1, le=1_000)
    total_items: int | None = Field(None, ge=1, le=1_000_000_000)     # images / clips / videos / files in a batch

    @field_validator("modality", mode="before")
    @classmethod
    def _modality(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            v = [v]
        if not isinstance(v, (list, tuple)):
            raise ValueError("modality is one of text, image, audio, video, multimodal, or a list of them")
        out = []
        for x in v:
            x = x.strip().lower() if isinstance(x, str) else x
            if x not in out:
                out.append(x)
        return out

    @field_validator("image_resolution", "video_resolution", "output_resolution")
    @classmethod
    def _res(cls, v):
        if v is None:
            return v
        w, h = parse_resolution(v)
        if not (16 <= w <= MAX_SIDE and 16 <= h <= MAX_SIDE):
            raise ValueError(f"resolution sides are 16-{MAX_SIDE} pixels (WIDTHxHEIGHT)")
        return f"{w}x{h}"


class WorkloadRequirements(_M):
    workload_type: WorkloadType | None = None
    frameworks: list[str] = Field(default_factory=list, max_length=_LIST)
    model: ModelInfo | None = None
    precision: Precision | None = None
    quantization: Quantization | None = None
    training: Training | None = None
    inference: Inference | None = None
    media: Media | None = None
    constraints: Constraints | None = None
    hardware_hints: HardwareHints | None = None
    evidence: list[Evidence] = Field(default_factory=list, max_length=200)
    source: Source | None = None

    @field_validator("frameworks")
    @classmethod
    def _frameworks(cls, v: list[str]) -> list[str]:
        out = []
        for f in v:
            f = (f or "").strip().lower()
            if not f or len(f) > 64:
                raise ValueError("framework names are 1-64 characters")
            if f not in out:
                out.append(f)
        return out


def media_given(req) -> bool:
    """True when the request states at least one media field (an empty `media` object is the same as none)."""
    m = getattr(req, "media", None)
    return m is not None and any(v not in (None, []) for v in m.model_dump().values())
