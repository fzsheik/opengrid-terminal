"""Template requirements -> the canonical GPUs a workload may run on.

    compute_capability(gpu) -> (cc float | None, basis str)
    check_gpu(gpu, requirements) -> {gpu, eligible, code, reason, vendor, architecture, compute_capability, ...}
    resolve(requirements, gpu='auto') -> {"eligible": [...], "ineligible": [{gpu, code, reason}, ...]}

Facts come from hardware.spec() (vendor, architecture, vram_gb, form_factor); compute capability is derived
from the architecture with the table below, per NVIDIA's published compute-capability list. A GPU whose spec,
architecture or VRAM is unknown is EXCLUDED ("hardware spec unknown"), never assumed compatible. Variants are
separate canonical GPUs and are checked one by one: "H100" (a family) is not a GPU here.

Sources for compute capability:
    https://developer.nvidia.com/cuda-gpus                          (NVIDIA: CUDA GPUs - Compute Capability)
    https://docs.nvidia.com/cuda/cuda-c-programming-guide/#compute-capabilities
"""

from __future__ import annotations

import canonical
import hardware

CC_SOURCES = ["https://developer.nvidia.com/cuda-gpus",
              "https://docs.nvidia.com/cuda/cuda-c-programming-guide/#compute-capabilities"]

# architecture -> list of (name substring that selects this row | None for the default, cc, chip note).
# First matching substring wins; the None row is the architecture default.
CC_TABLE: dict[str, list[tuple[str | None, float, str]]] = {
    "Pascal": [("P100", 6.0, "GP100"), (None, 6.1, "GP10x (GeForce 10-series, P40/P4)")],
    "Volta": [(None, 7.0, "GV100 (V100)")],
    "Turing": [(None, 7.5, "TU10x/TU11x (T4, RTX 20-series, GTX 16-series, Quadro RTX)")],
    "Ampere": [("A100", 8.0, "GA100"), ("A30", 8.0, "GA100"),
               (None, 8.6, "GA10x (A10, A10G, A40, RTX A-series, RTX 30-series)")],
    "Ada Lovelace": [(None, 8.9, "AD10x (L4, L40, L40S, RTX Ada, RTX 40-series)")],
    "Hopper": [(None, 9.0, "GH100 (H100, H200, GH200)")],
    "Blackwell": [("RTX", 12.0, "GB20x (RTX 50-series, RTX PRO Blackwell)"), (None, 10.0, "GB100 (B200, GB200)")],
    "Blackwell Ultra": [(None, 10.3, "GB110 (B300, GB300)")],
}

UNKNOWN = "hardware spec unknown"


def compute_capability(gpu: str) -> tuple[float | None, str]:
    """(cc, basis). cc None when the GPU is not NVIDIA or its architecture is not in CC_TABLE."""
    s = hardware.spec(gpu)
    if s is None:
        return None, f"{UNKNOWN}: no hardware entry for {gpu!r}"
    if s.get("vendor") != "NVIDIA":
        return None, f"not an NVIDIA GPU (vendor {s.get('vendor') or 'unknown'}): no CUDA compute capability"
    rows = CC_TABLE.get(s.get("architecture") or "")
    if not rows:
        return None, f"{UNKNOWN}: no compute capability on record for architecture {s.get('architecture')!r}"
    for sub, cc, note in rows:
        if sub is None or sub in gpu:
            return cc, f"{s['architecture']} {note}: compute capability {cc:.1f} (NVIDIA CUDA GPUs list)"
    return None, f"{UNKNOWN}: architecture {s.get('architecture')!r} has no default row"


def _info(gpu: str, s: dict | None, cc: float | None, cc_basis: str) -> dict:
    from api.common import gpu_slug
    s = s or {}
    return {"gpu": gpu, "slug": gpu_slug(gpu), "vendor": s.get("vendor"), "architecture": s.get("architecture"),
            "compute_capability": cc, "compute_capability_basis": cc_basis, "vram_gb": s.get("vram_gb"),
            "form_factor": s.get("form_factor"), "segment": s.get("segment")}


def check_gpu(gpu: str, req: dict) -> dict:
    """One canonical GPU against the effective requirements. {..., eligible, code, reason}."""
    s = hardware.spec(gpu)
    cc, basis = compute_capability(gpu) if s is not None else (None, f"{UNKNOWN}: no hardware entry for {gpu!r}")
    out = _info(gpu, s, cc, basis)

    def no(code, reason):
        return {**out, "eligible": False, "code": code, "reason": reason}

    if s is None:
        return no("hardware_spec_unknown", f"{UNKNOWN}: no hardware entry for {gpu}")
    vendor = req.get("vendor")
    if vendor and s.get("vendor") != vendor:
        return no("wrong_vendor", f"{s.get('vendor') or 'unknown'} GPU: the template needs an {vendor} "
                                  f"{'CUDA ' if req.get('cuda_required') else ''}GPU")
    if req.get("cuda_required") and cc is None:
        return no("hardware_spec_unknown", basis)
    mn, mx = req.get("min_compute_capability"), req.get("max_compute_capability")
    if mn is not None and cc is not None and cc < mn - 1e-9:
        return no("compute_capability_too_low", f"compute capability {cc:.1f} ({s['architecture']}) is below the "
                                                f"required {mn:.1f}")
    if mx is not None and cc is not None and cc > mx + 1e-9:
        why = req.get("max_compute_capability_reason") or "not supported by the pinned framework build"
        return no("compute_capability_unsupported", f"compute capability {cc:.1f} ({s['architecture']}) is newer "
                                                    f"than {mx:.1f}: {why}")
    vram = s.get("vram_gb")
    if vram is None:
        return no("hardware_spec_unknown", f"{UNKNOWN}: VRAM of {gpu} not on record")
    if req.get("min_vram_gb") is not None and float(vram) < float(req["min_vram_gb"]) - 1e-9:
        return no("insufficient_vram", f"{vram:g} GB VRAM is below the required {float(req['min_vram_gb']):g} GB")
    if req.get("host_cpu_arch") == "x86_64" and s.get("form_factor") == "Superchip":
        return no("host_cpu_unsupported", f"{gpu} is a Grace (ARM) superchip host: "
                                          f"{req.get('host_cpu_arch_reason') or 'x86_64 host required'}")
    return {**out, "eligible": True, "code": None, "reason": None}


def universe() -> list[str]:
    """Every canonical GPU name OpenGrid knows (sorted)."""
    return sorted(set(canonical.all_canonical_names()) | set(hardware.all_specs()))


def resolve(req: dict, gpu: str = "auto") -> dict:
    """auto: every canonical GPU checked; an explicit canonical GPU: just that one, and an incompatible choice
    raises WorkloadError('gpu_incompatible') with the reason."""
    from workloads.spec import WorkloadError

    if gpu and gpu != "auto":
        r = check_gpu(gpu, req)
        if not r["eligible"]:
            raise WorkloadError("gpu_incompatible", f"{gpu} cannot run this workload: {r['reason']}",
                                field="resources.gpu", gpu=gpu, gpu_code=r["code"])
        return {"mode": "explicit", "eligible": [r], "ineligible": []}
    elig, inel = [], []
    for g in universe():
        r = check_gpu(g, req)
        if r["eligible"]:
            elig.append(r)
        else:
            inel.append({"gpu": g, "slug": r["slug"], "code": r["code"], "reason": r["reason"]})
    return {"mode": "auto", "eligible": elig, "ineligible": inel}
