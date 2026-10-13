"""The job request (POST /v1/jobs/preview, POST /v1/jobs) and its strict validation.

    WorkloadSpec                pydantic model of the request (shape and types only; extra fields forbidden)
    parse(body) -> WorkloadSpec raises WorkloadError (code invalid_request | missing_field | unknown_field)
    validate(body) -> Validated the template-aware check: every bound, the GPU choice, the region, the strategy,
                                the ssh key; fills defaults. Raises WorkloadError with a machine-readable code.
    WorkloadError(code, message, field=None, status=422)   .as_dict() -> {code, message, field, ...}

Job types finetuning / inference / batch_compute / custom_container PARSE (they are part of the schema) but
validate() refuses them with code `unsupported_workload_type`. Only allowlisted templates run; no code, no
command and no dataset URL can be supplied. See methodology/workloads.md.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from workloads import templates as T

JOB_TYPES = ("training", "finetuning", "inference", "batch_compute", "custom_container")
SUPPORTED_JOB_TYPES = ("training",)
ARTIFACT_DESTINATIONS = ("opengrid",)
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}\Z")  # \Z: `$` would accept a trailing newline


class WorkloadError(ValueError):
    """A refusal the API turns into HTTP `status` (422 by default) with {code, message, ...}."""

    def __init__(self, code: str, message: str, *, field: str | None = None, status: int = 422, **extra):
        super().__init__(message)
        self.code, self.message, self.field, self.status, self.extra = code, message, field, status, extra

    def as_dict(self) -> dict:
        d = {"code": self.code, "message": self.message}
        if self.field:
            d["field"] = self.field
        d.update(self.extra)
        return d


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Resources(_M):
    gpu: str = "auto"
    gpu_count: int = 1
    min_vram_gb: float | None = None
    region: str | None = None
    strict_region: bool = False
    allow_spot: bool = False
    min_vcpu: int | None = None
    min_ram_gb: float | None = None
    min_disk_gb: float | None = None
    cuda_required: bool = True


class Routing(_M):
    strategy: str = "BALANCED"
    weights: dict[str, float] | None = None


class Limits(_M):
    max_price_per_gpu_hour_usd: float
    max_total_cost_usd: float
    max_runtime_minutes: int


class WorkloadSpec(_M):
    name: str | None = None
    job_type: Literal["training", "finetuning", "inference", "batch_compute", "custom_container"]
    template: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    resources: Resources = Field(default_factory=Resources)
    routing: Routing = Field(default_factory=Routing)
    limits: Limits
    ssh_public_key: str | None = None
    sandbox: bool = False
    artifact_destination: str = "opengrid"
    dataset_ref: str | None = None


@dataclass
class Validated:
    """A validated, normalised request. `request` has the WorkloadSpec shape (re-validates to itself)."""
    request: dict
    template: dict
    requirements: dict
    notes: list = field(default_factory=list)
    ssh_key_fingerprint: str | None = None
    runtime_hard_max_minutes: int | None = None

    def as_dict(self) -> dict:
        return {"request": self.request, "template_id": self.template["id"],
                "template_version": self.template["version"], "requirements": self.requirements,
                "notes": list(self.notes), "ssh_key_fingerprint": self.ssh_key_fingerprint}


# --------------------------------------------------------------------------

def _loc(e: dict) -> str:
    return ".".join(str(p) for p in e.get("loc") or ()) or "body"


def parse(body) -> WorkloadSpec:
    if isinstance(body, WorkloadSpec):
        return body
    if not isinstance(body, dict):
        raise WorkloadError("invalid_request", "the job request must be a JSON object")
    try:
        return WorkloadSpec.model_validate(body)
    except ValidationError as exc:
        errs = exc.errors()
        first = errs[0]
        kind = first.get("type")
        loc = _loc(first)
        if loc == "job_type" and kind == "literal_error":
            raise WorkloadError("invalid_job_type", f"unknown job_type {body.get('job_type')!r}; one of "
                                                    f"{', '.join(JOB_TYPES)}", field="job_type") from None
        code = {"missing": "missing_field", "extra_forbidden": "unknown_field"}.get(kind, "invalid_request")
        msg = "; ".join(f"{_loc(e)}: {e.get('msg')}" for e in errs[:5])
        raise WorkloadError(code, msg, field=loc) from None


def _num(v, name: str, *, integer: bool) -> float | int:
    if isinstance(v, bool) or v is None:
        raise WorkloadError("invalid_config", f"config.{name} must be a{'n integer' if integer else ' number'}",
                            field=f"config.{name}")
    if integer:
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        if not isinstance(v, int):
            raise WorkloadError("invalid_config", f"config.{name} must be an integer", field=f"config.{name}")
        return v
    if not isinstance(v, (int, float)) or not math.isfinite(float(v)):
        raise WorkloadError("invalid_config", f"config.{name} must be a finite number", field=f"config.{name}")
    return float(v)


def validate_config(template: dict, config: dict) -> dict:
    schema = template["config_schema"]
    unknown = sorted(set(config or {}) - set(schema))
    if unknown:
        raise WorkloadError("invalid_config", f"unknown config field(s) {', '.join(unknown)} for template "
                                              f"{template['id']}; allowed: {', '.join(schema)}",
                            field=f"config.{unknown[0]}")
    out = {}
    for name, f in schema.items():
        v = (config or {}).get(name, f["default"])
        v = _num(v, name, integer=f["type"] == "integer")
        if v < f["min"] or v > f["max"]:
            raise WorkloadError("config_out_of_bounds", f"config.{name} = {v:g} is outside {f['min']:g}..{f['max']:g}",
                                field=f"config.{name}")
        out[name] = v
    return out


def _region(value: str | None) -> str | None:
    if value is None or not str(value).strip():
        return None
    from regions import REGION_GROUPS
    for g in REGION_GROUPS:
        if g.lower() == str(value).strip().lower():
            return g
    raise WorkloadError("invalid_region", f"unknown region {value!r}; one of {', '.join(REGION_GROUPS)}",
                        field="resources.region")


def _gpu(value: str) -> str:
    v = (value or "").strip()
    if not v or v.lower() == "auto":
        return "auto"
    from api.common import resolve_gpu
    hit = resolve_gpu(v, strict=False)
    if hit:
        return hit
    import families
    fid = families.resolve_family(v)
    if fid:
        raise WorkloadError("gpu_family_not_allowed",
                            f"{v!r} is a GPU family ({fid}) whose variants are different products; choose one "
                            f"canonical variant ({', '.join(families.variants(fid)[:8])}) or 'auto'",
                            field="resources.gpu")
    raise WorkloadError("unknown_gpu", f"unknown GPU {v!r}; use 'auto' or a GPU slug / canonical name from /v1/gpus",
                        field="resources.gpu")


def _bounded(v: float, name: str, b: dict, unit: str) -> None:
    if not math.isfinite(v):
        raise WorkloadError("limit_out_of_bounds", f"limits.{name} must be a finite number", field=f"limits.{name}")
    if "min_exclusive" in b and v <= b["min_exclusive"]:
        raise WorkloadError("limit_out_of_bounds", f"limits.{name} must be > {b['min_exclusive']:g} {unit}",
                            field=f"limits.{name}")
    if "min" in b and v < b["min"]:
        raise WorkloadError("limit_out_of_bounds", f"limits.{name} = {v:g} is below the minimum {b['min']:g} {unit}",
                            field=f"limits.{name}")
    if "max" in b and v > b["max"]:
        raise WorkloadError("limit_out_of_bounds", f"limits.{name} = {v:g} is above the maximum {b['max']:g} {unit}",
                            field=f"limits.{name}")


def _min_resource(user, tmpl, name: str, notes: list, unit: str):
    if user is not None:
        if isinstance(user, bool) or not math.isfinite(float(user)) or float(user) <= 0:
            raise WorkloadError("invalid_resources", f"resources.{name} must be a positive number",
                                field=f"resources.{name}")
        if tmpl is not None and float(user) < float(tmpl):
            notes.append(f"resources.{name} {float(user):g} {unit} is below the template's {float(tmpl):g} {unit}; "
                         f"the template's minimum applies")
            return tmpl
        return user
    return tmpl


def validate(body) -> Validated:
    """The full, template-aware validation. Raises WorkloadError."""
    if isinstance(body, Validated):
        return body
    from config import settings

    s = parse(body)
    if s.job_type not in SUPPORTED_JOB_TYPES:
        raise WorkloadError("unsupported_workload_type",
                            f"job_type {s.job_type!r} is part of the schema but not supported in this release; "
                            f"supported: {', '.join(SUPPORTED_JOB_TYPES)} (template pytorch-mnist)",
                            field="job_type", job_type=s.job_type)
    if not s.template:
        raise WorkloadError("missing_field", "template is required (allowlisted templates: "
                                             f"{', '.join(t['id'] for t in T.list_templates())})", field="template")
    tmpl = T.get_template(s.template)
    if tmpl is None:
        raise WorkloadError("unknown_template", f"template {s.template!r} is not allowlisted; available: "
                                                f"{', '.join(t['id'] for t in T.list_templates())}", field="template")
    if tmpl["job_type"] != s.job_type:
        raise WorkloadError("template_job_type_mismatch", f"template {tmpl['id']} is a {tmpl['job_type']} template, "
                                                          f"not {s.job_type}", field="template")
    notes: list[str] = []
    name = s.name if s.name is not None else tmpl["preset"]["name"]
    if not NAME_RE.match(name or ""):
        raise WorkloadError("invalid_name", "name must be 1-64 characters [a-z0-9._-], starting with a letter or "
                                            "digit", field="name")
    config = validate_config(tmpl, s.config)
    req0 = tmpl["requirements"]
    r = s.resources

    gpu = _gpu(r.gpu)
    if r.gpu_count != req0["gpu_count"]:
        raise WorkloadError("gpu_count_out_of_bounds", f"resources.gpu_count must be {req0['gpu_count']} for "
                                                       f"{tmpl['id']} (got {r.gpu_count})", field="resources.gpu_count")
    if req0.get("cuda_required") and not r.cuda_required:
        raise WorkloadError("cuda_required", f"{tmpl['id']} requires an NVIDIA CUDA GPU; cuda_required cannot be false",
                            field="resources.cuda_required")
    if r.allow_spot and req0.get("on_demand_only"):
        raise WorkloadError("spot_not_supported", "spot / interruptible capacity is not supported in this release; "
                                                  "set allow_spot false", field="resources.allow_spot")
    region = _region(r.region)
    if r.strict_region and not region:
        raise WorkloadError("invalid_region", "strict_region needs a region", field="resources.strict_region")
    if r.min_vram_gb is not None and (not math.isfinite(r.min_vram_gb) or r.min_vram_gb <= 0 or r.min_vram_gb > 1024):
        raise WorkloadError("invalid_resources", "resources.min_vram_gb must be in (0, 1024]",
                            field="resources.min_vram_gb")
    vram = _min_resource(r.min_vram_gb, req0["min_vram_gb"], "min_vram_gb", notes, "GB")
    vcpu = _min_resource(r.min_vcpu, req0.get("min_vcpu"), "min_vcpu", notes, "vCPU")
    ram = _min_resource(r.min_ram_gb, req0.get("min_ram_gb"), "min_ram_gb", notes, "GB")
    disk = _min_resource(r.min_disk_gb, req0.get("min_disk_gb"), "min_disk_gb", notes, "GB")

    from routing import scoring
    strategy = (s.routing.strategy or "BALANCED").strip().upper()
    try:
        scoring.effective_weights(strategy, s.routing.weights, bool(region))
    except scoring.ScoringError as exc:
        raise WorkloadError("invalid_strategy", str(exc), field="routing") from None

    lim, lb = s.limits, tmpl["limits_bounds"]
    _bounded(float(lim.max_price_per_gpu_hour_usd), "max_price_per_gpu_hour_usd", lb["max_price_per_gpu_hour_usd"], "USD")
    _bounded(float(lim.max_total_cost_usd), "max_total_cost_usd", lb["max_total_cost_usd"], "USD")
    rb = dict(lb["max_runtime_minutes"])
    hard = int(settings.runtime_hard_max_minutes)
    rb["max"] = min(rb["max"], hard)
    _bounded(float(lim.max_runtime_minutes), "max_runtime_minutes", rb, "minutes")
    lo, hi = T.expected_minutes(tmpl, config)
    if hi > lim.max_runtime_minutes:
        notes.append(f"the expected runtime band ({lo:g}-{hi:g} min, an estimate) reaches past max_runtime_minutes "
                     f"{lim.max_runtime_minutes}: the job may be stopped at the ceiling before it finishes")

    fp, pk = None, None
    if s.ssh_public_key is not None and s.ssh_public_key.strip():
        from routing import credentials
        if credentials.looks_private(s.ssh_public_key):
            raise WorkloadError("ssh_private_key_rejected", credentials.PRIVATE_KEY_MESSAGE, field="ssh_public_key")
        try:
            parsed = credentials.parse_public_key(s.ssh_public_key)
        except credentials.SSHKeyError as exc:
            raise WorkloadError("ssh_public_key_invalid", str(exc), field="ssh_public_key") from None
        pk, fp = parsed["public_key"], parsed["fingerprint"]

    if s.artifact_destination not in ARTIFACT_DESTINATIONS:
        raise WorkloadError("unsupported_artifact_destination", f"artifact_destination must be one of "
                                                                f"{', '.join(ARTIFACT_DESTINATIONS)} in this release",
                            field="artifact_destination")
    if s.dataset_ref is not None and s.dataset_ref != tmpl["dataset"]["id"]:
        raise WorkloadError("dataset_not_allowed", f"dataset_ref must be omitted or {tmpl['dataset']['id']!r}: only the "
                                                   "template's own dataset is allowed in this release",
                            field="dataset_ref")

    request = {
        "name": name, "job_type": s.job_type, "template": tmpl["id"], "config": config,
        "resources": {"gpu": gpu, "gpu_count": r.gpu_count, "min_vram_gb": r.min_vram_gb, "region": region,
                      "strict_region": bool(r.strict_region), "allow_spot": False, "min_vcpu": r.min_vcpu,
                      "min_ram_gb": r.min_ram_gb, "min_disk_gb": r.min_disk_gb, "cuda_required": True},
        "routing": {"strategy": strategy, "weights": s.routing.weights},
        "limits": {"max_price_per_gpu_hour_usd": float(lim.max_price_per_gpu_hour_usd),
                   "max_total_cost_usd": float(lim.max_total_cost_usd),
                   "max_runtime_minutes": int(lim.max_runtime_minutes)},
        "ssh_public_key": pk, "sandbox": bool(s.sandbox), "artifact_destination": s.artifact_destination,
        "dataset_ref": tmpl["dataset"]["id"],
    }
    requirements = {**req0, "min_vram_gb": vram, "min_vcpu": vcpu, "min_ram_gb": ram, "min_disk_gb": disk,
                    "gpu_count": r.gpu_count, "region": region, "strict_region": bool(r.strict_region)}
    return Validated(request=request, template=tmpl, requirements=requirements, notes=notes,
                     ssh_key_fingerprint=fp, runtime_hard_max_minutes=hard)
