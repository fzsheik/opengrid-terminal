"""The allowlisted workload templates. A template is the ONLY thing a job may run in this release:
no arbitrary commands, no user-supplied code, no arbitrary dataset URLs.

    list_templates() -> [template dict]     public copies, stable order
    get_template(id) -> template dict | None

A template declares what it runs (framework pins, dataset, model), its config schema (types, bounds,
defaults), its hardware requirements (hardware_req.py turns them into eligible canonical GPUs), the
limits it accepts, an EXPECTED runtime band (an estimate, never a benchmark) and the founder's preset
request. See methodology/workloads.md.
"""

from __future__ import annotations

import copy

TEMPLATE_SCHEMA_VERSION = 1

_MNIST = {
    "id": "pytorch-mnist",
    "version": "1.0.0",
    "job_type": "training",
    "title": "Train Your First AI Model — PyTorch MNIST",
    "summary": "Train a small convolutional network to recognise handwritten digits on one cloud GPU.",
    "description": (
        "Downloads the MNIST handwritten-digit dataset (60,000 training and 10,000 test images) from "
        "torchvision's default mirror with torchvision's built-in MD5 checks, trains a small CNN (2 "
        "convolutional + 2 fully-connected layers, Adam) for the chosen number of epochs on one NVIDIA GPU, "
        "reports per-epoch training loss and test accuracy, and saves the trained weights (model.pt), the "
        "config, the metrics and the software/GPU environment as downloadable artifacts. OpenGrid "
        "terminates the GPU when the run ends, fails or reaches its runtime ceiling."),
    "framework": {
        "name": "PyTorch", "torch": "2.4.1", "torchvision": "0.19.1", "cuda": "12.1",
        "wheel_index": "https://download.pytorch.org/whl/cu121",
        "note": "pinned versions installed into a fresh venv from the official cu121 wheel index; if that fails "
                "the bootstrap may fall back to the image's preinstalled PyTorch and records which one ran",
    },
    "dataset": {
        "id": "mnist", "name": "MNIST", "source": "torchvision.datasets.MNIST default mirror",
        "verification": "MD5 checksums built into torchvision", "train_samples": 60000, "test_samples": 10000,
        "user_supplied": False,
    },
    "model": {"name": "small CNN", "architecture": "2 convolutional + 2 fully-connected layers",
              "optimizer": "Adam", "loss": "cross-entropy"},
    "config_schema": {
        "epochs": {"type": "integer", "min": 1, "max": 10, "default": 3, "description": "passes over the training set"},
        "batch_size": {"type": "integer", "min": 32, "max": 512, "default": 128, "description": "images per step"},
        "learning_rate": {"type": "number", "min": 1e-5, "max": 1e-1, "default": 1e-3,
                          "description": "Adam learning rate"},
        "seed": {"type": "integer", "min": 0, "max": 2**31 - 1, "default": 42,
                 "description": "random seed (results are seeded, not bit-for-bit reproducible across GPUs)"},
    },
    "requirements": {
        "vendor": "NVIDIA",
        "cuda_required": True,
        # sm_70+: the oldest architecture the pinned torch 2.4.1+cu121 build ships kernels for that we accept
        # (Volta). Pascal (6.x) is excluded.
        "min_compute_capability": 7.0,
        # The NEWEST architecture the pinned build ships kernels for: torch 2.4.1+cu121 is compiled for
        # sm_50..sm_90 (CUDA 12.1 predates Blackwell; Blackwell sm_100/sm_120 needs a CUDA 12.8+ build).
        # Raising this needs a newer framework pin in runner/bootstrap.py, not just a number change.
        "max_compute_capability": 9.0,
        "max_compute_capability_reason": "the pinned PyTorch 2.4.1 + CUDA 12.1 build has no kernels newer than "
                                         "sm_90 (Hopper); Blackwell needs a CUDA 12.8+ build",
        "min_vram_gb": 8,
        "vram_note": "MNIST training with this CNN uses well under 1 GB of GPU memory; 8 GB is a generous margin "
                     "(CUDA context, cuDNN workspaces, larger batch sizes) and excludes only very small cards",
        "gpu_count": 1,
        "host_cpu_arch": "x86_64",
        "host_cpu_arch_reason": "the pinned wheels are installed and tested on x86_64 hosts; ARM (Grace) "
                                "superchip hosts are not verified for this template",
        "min_vcpu": 4,
        "min_ram_gb": 16,
        "min_disk_gb": 30,
        "resource_minimums_note": "vCPU / RAM / disk minimums apply only when the listing reports the value; an "
                                  "unreported value is not a reason to exclude (and is shown as unknown)",
        "disk_note": "the PyTorch + CUDA wheels are ~3 GB installed; 30 GB leaves room for the OS image, pip "
                     "cache, dataset (~60 MB) and artifacts",
        "on_demand_only": True,
        "on_demand_note": "spot / interruptible capacity is refused in this release",
    },
    "limits_bounds": {
        "max_runtime_minutes": {"min": 5, "max": 60},
        "max_price_per_gpu_hour_usd": {"min_exclusive": 0, "max": 100},
        "max_total_cost_usd": {"min_exclusive": 0, "max": 100},
    },
    "expected_runtime": {
        "kind": "estimated",
        "note": "an ESTIMATE from the steps involved, not a benchmark: environment setup dominates and varies "
                "with the provider's image, network and pip mirror speed",
        "setup_minutes": [3, 8],          # boot to venv ready (pip download + install of pinned wheels)
        "overhead_minutes": [1.5, 2.5],   # dataset download + checks, artifact upload, reporting
        "per_epoch_minutes": [0.5, 1.5],  # one pass over 60k images on a modern GPU (data-loader bound)
        "default_band_minutes": [6, 15],  # with the default 3 epochs
    },
    "artifacts": ["model.pt", "config.json", "metrics.json", "env.json"],
    "preset": {
        "name": "opengrid-first-training-test",
        "job_type": "training",
        "template": "pytorch-mnist",
        "config": {"epochs": 3, "batch_size": 128, "learning_rate": 0.001, "seed": 42},
        "resources": {"gpu": "auto", "gpu_count": 1, "min_vram_gb": None, "region": "US", "strict_region": False,
                      "allow_spot": False},
        "routing": {"strategy": "BALANCED", "weights": None},
        "limits": {"max_price_per_gpu_hour_usd": 3, "max_total_cost_usd": 2, "max_runtime_minutes": 30},
        "ssh_public_key": None,
        "sandbox": False,
    },
}

TEMPLATES: dict[str, dict] = {_MNIST["id"]: _MNIST}


def list_templates() -> list[dict]:
    return [copy.deepcopy(TEMPLATES[k]) for k in sorted(TEMPLATES)]


def get_template(template_id: str | None) -> dict | None:
    t = TEMPLATES.get((template_id or "").strip().lower())
    return None if t is None else copy.deepcopy(t)


def config_defaults(template: dict) -> dict:
    return {k: f["default"] for k, f in template["config_schema"].items()}


def expected_minutes(template: dict, config: dict) -> tuple[float, float]:
    """(low, high) expected wall-clock minutes for this config: setup + overhead + epochs x per-epoch band.
    An estimate (kind 'estimated'); see expected_runtime.note."""
    e = template["expected_runtime"]
    epochs = int((config or {}).get("epochs") or template["config_schema"]["epochs"]["default"])
    lo = e["setup_minutes"][0] + e["overhead_minutes"][0] + epochs * e["per_epoch_minutes"][0]
    hi = e["setup_minutes"][1] + e["overhead_minutes"][1] + epochs * e["per_epoch_minutes"][1]
    return round(lo, 2), round(hi, 2)
