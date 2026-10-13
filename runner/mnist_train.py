"""The allowlisted `pytorch-mnist` workload: train a small CNN on MNIST and prove it really happened.

Runs ON the GPU instance under runner.reporter (which forwards our stdout JSON lines to OpenGrid). Python 3.10+.
torch/torchvision/numpy are imported ONLY inside functions so the app server (no torch) can import this module
(tests, bootstrap embedding, artifact verification helpers).

Evidence produced (methodology/runner.md maps each to a founder check):
  - env.json      python/torch/torchvision/CUDA runtime/cuDNN/GPU name/driver/compute capability/is_available
  - dataset       torchvision's own MD5 check on download + our size/sha256 of every raw file
  - metrics.json  per-epoch mean train loss over ALL batches, held-out test loss/accuracy, samples/sec,
                  peak CUDA memory, runtime, L2 distance between initial and final weights
  - model.pt      state_dict; load_model()+evaluate() reproduce the reported test accuracy
  - manifest.json sha256 + size of every artifact
CUDA is REQUIRED unless --allow-cpu (tests / local verification only; recorded as device=cpu everywhere).

stdout protocol: one JSON object per line {"og": 1, "type": phase|metric|result|error|log, "at": iso, "data": {..}}.
Anything else on stdout/stderr is forwarded by the reporter as a plain log line.
Exit codes: 0 succeeded, 2 failed, 3 oom, 4 timeout, 5 cuda unavailable (bootstrap problem, not training).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import platform
import random
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

TEMPLATE = "pytorch-mnist"
EXIT_OK, EXIT_FAILED, EXIT_OOM, EXIT_TIMEOUT, EXIT_NO_CUDA = 0, 2, 3, 4, 5
OUTCOME_BY_EXIT = {EXIT_OK: "succeeded", EXIT_FAILED: "failed", EXIT_OOM: "oom", EXIT_TIMEOUT: "timeout",
                   EXIT_NO_CUDA: "failed"}

# Bounds mirror the workload validation on the app side; enforced again here so the GPU never trusts input.
DEFAULTS = {"epochs": 3, "batch_size": 128, "learning_rate": 0.001, "seed": 42}
BOUNDS = {"epochs": (1, 20), "batch_size": (16, 1024), "learning_rate": (1e-5, 0.1), "seed": (0, 2**31 - 1)}
ARTIFACTS = ("model.pt", "config.json", "metrics.json", "env.json")


class TrainingTimeout(Exception):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def emit(type_: str, **data) -> None:
    """One structured event per stdout line (flushed: the reporter streams it live)."""
    print(json.dumps({"og": 1, "type": type_, "at": now_iso(), "data": data}, default=str), flush=True)


def phase(name: str, **data) -> None:
    emit("phase", phase=name, **data)


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def validate_config(raw: dict) -> dict:
    unknown = set(raw) - set(DEFAULTS) - {"limit_train", "limit_test"}
    if unknown:
        raise ValueError(f"unknown config keys: {sorted(unknown)}")
    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in raw.items() if v is not None})
    for k, (lo, hi) in BOUNDS.items():
        v = cfg[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError(f"{k} must be a number")
        if k != "learning_rate":
            if int(v) != v:
                raise ValueError(f"{k} must be an integer")
            v = cfg[k] = int(v)
        else:
            v = cfg[k] = float(v)
        if not lo <= v <= hi:
            raise ValueError(f"{k}={v} outside [{lo}, {hi}]")
    for k in ("limit_train", "limit_test"):
        v = cfg.get(k)
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 60000):
            raise ValueError(f"{k} must be an integer in [1, 60000]")
    return cfg


# ---------------------------------------------------------------------------------------------- model / data
def build_model():
    """conv32 -> conv64 -> maxpool -> dropout -> fc128 -> dropout -> fc10 (the classic PyTorch MNIST example)."""
    import torch.nn as nn

    class Net(nn.Module):
        def __init__(self):
            super().__init__()
            self.conv1 = nn.Conv2d(1, 32, 3, 1)
            self.conv2 = nn.Conv2d(32, 64, 3, 1)
            self.dropout1 = nn.Dropout(0.25)
            self.dropout2 = nn.Dropout(0.5)
            self.fc1 = nn.Linear(9216, 128)
            self.fc2 = nn.Linear(128, 10)

        def forward(self, x):
            import torch.nn.functional as F
            x = F.relu(self.conv1(x))
            x = F.relu(self.conv2(x))
            x = F.max_pool2d(x, 2)
            x = self.dropout1(x)
            x = x.flatten(1)
            x = F.relu(self.fc1(x))
            x = self.dropout2(x)
            return self.fc2(x)  # logits; cross_entropy applies log_softmax

    return Net()


def _transform():
    from torchvision import transforms
    return transforms.Compose([transforms.ToTensor(), transforms.Normalize((0.1307,), (0.3081,))])


def load_datasets(data_dir, download: bool = True):
    """torchvision MNIST (default mirrors; torchvision checks each archive's MD5 on download)."""
    from torchvision import datasets
    train = datasets.MNIST(str(data_dir), train=True, download=download, transform=_transform())
    test = datasets.MNIST(str(data_dir), train=False, download=download, transform=_transform())
    return train, test


def dataset_fingerprint(data_dir) -> dict:
    raw = Path(data_dir) / "MNIST" / "raw"
    files = {}
    for p in sorted(raw.glob("*")) if raw.is_dir() else []:
        if p.is_file():
            files[p.name] = {"bytes": p.stat().st_size, "sha256": sha256_file(p)}
    return files


def _subset(ds, n, seed):
    import torch
    if n is None or n >= len(ds):
        return ds
    g = torch.Generator().manual_seed(seed)
    idx = torch.randperm(len(ds), generator=g)[:n].tolist()
    return torch.utils.data.Subset(ds, idx)


def evaluate(model, loader, device) -> dict:
    """Held-out loss (mean over samples) and accuracy, eval mode, no grad."""
    import torch
    import torch.nn.functional as F
    model.eval()
    loss_sum, correct, n = 0.0, 0, 0
    with torch.no_grad():
        for x, y in loader:
            x, y = x.to(device, non_blocking=True), y.to(device, non_blocking=True)
            out = model(x)
            loss_sum += F.cross_entropy(out, y, reduction="sum").item()
            correct += (out.argmax(1) == y).sum().item()
            n += y.numel()
    return {"test_loss": loss_sum / max(n, 1), "test_accuracy": correct / max(n, 1), "test_samples": n}


def load_model(path, device="cpu"):
    """Reload a saved model.pt (state_dict) into the same architecture."""
    import torch
    model = build_model()
    try:
        state = torch.load(str(path), map_location=device, weights_only=True)
    except TypeError:  # torch < 1.13 has no weights_only
        state = torch.load(str(path), map_location=device)
    model.load_state_dict(state)
    return model.to(device)


def test_loader(data_dir, *, batch_size=1000, limit_test=None, seed=42):
    import torch
    from torchvision import datasets
    test = datasets.MNIST(str(data_dir), train=False, download=False, transform=_transform())
    return torch.utils.data.DataLoader(_subset(test, limit_test, seed + 1), batch_size=batch_size, shuffle=False)


# ---------------------------------------------------------------------------------------------- environment
def _nvidia_smi() -> dict:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,driver_version,memory.total,compute_cap,uuid",
             "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=20)
        if out.returncode != 0:
            return {"error": (out.stderr or out.stdout).strip()[:500]}
        rows = [[c.strip() for c in line.split(",")] for line in out.stdout.strip().splitlines() if line.strip()]
        keys = ("name", "driver_version", "memory_total_mib", "compute_cap", "uuid")
        return {"gpus": [dict(zip(keys, r)) for r in rows]}
    except (OSError, subprocess.SubprocessError) as e:
        return {"error": f"nvidia-smi unavailable: {type(e).__name__}"}


def collect_env(device) -> dict:
    import numpy
    import torch
    import torchvision
    env = {
        "python": platform.python_version(), "python_executable": sys.executable,
        "platform": platform.platform(), "machine": platform.machine(),
        "torch": torch.__version__, "torchvision": torchvision.__version__, "numpy": numpy.__version__,
        "torch_cuda_build": torch.version.cuda, "cuda_available": torch.cuda.is_available(),
        "cudnn": torch.backends.cudnn.version() if torch.backends.cudnn.is_available() else None,
        "device": str(device), "env_source": os.environ.get("OG_ENV_SOURCE", "unknown"),
        "cpu_count": os.cpu_count(), "nvidia_smi": _nvidia_smi(),
    }
    if torch.cuda.is_available():
        i = torch.cuda.current_device()
        p = torch.cuda.get_device_properties(i)
        env.update({"gpu_name": p.name, "gpu_count": torch.cuda.device_count(),
                    "gpu_memory_bytes": p.total_memory, "compute_capability": f"{p.major}.{p.minor}",
                    "cuda_runtime": torch.version.cuda})
    smi = env["nvidia_smi"].get("gpus") or []
    if smi:
        env["driver"] = smi[0].get("driver_version")
    return env


def _param_vector(model):
    import torch
    return torch.cat([p.detach().flatten().float().cpu() for p in model.parameters()])


# ---------------------------------------------------------------------------------------------- training
def train(cfg: dict, *, out_dir, data_dir, allow_cpu=False, max_seconds=None, num_workers=2) -> int:
    import numpy as np
    import torch
    import torch.nn.functional as F

    t_start = time.monotonic()
    deadline = t_start + max_seconds if max_seconds else None
    out_dir, data_dir = Path(out_dir), Path(data_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    def check_deadline():
        if deadline and time.monotonic() > deadline:
            raise TrainingTimeout(f"wall-clock limit {max_seconds}s exceeded")

    # Seeds + cheap determinism (exact bitwise reproducibility across GPUs is not claimed).
    os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")
    seed = cfg["seed"]
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False

    if torch.cuda.is_available():
        device = torch.device("cuda")
        torch.cuda.manual_seed_all(seed)
    elif allow_cpu:
        device = torch.device("cpu")
    else:
        emit("error", code="cuda_unavailable",
             message="torch.cuda.is_available() is False: no usable NVIDIA GPU/driver for this PyTorch build. "
                     "Refusing to train on CPU (pass --allow-cpu only for local tests).",
             torch=torch.__version__, torch_cuda_build=torch.version.cuda, nvidia_smi=_nvidia_smi())
        return EXIT_NO_CUDA

    env = collect_env(device)
    (out_dir / "env.json").write_text(json.dumps(env, indent=2, sort_keys=True), encoding="utf-8")
    phase("device_selected", device=str(device),
          gpu_name=env.get("gpu_name"), compute_capability=env.get("compute_capability"),
          torch=env["torch"], cuda_runtime=env.get("cuda_runtime"), cudnn=env["cudnn"])

    t0 = time.monotonic()
    try:
        train_ds, test_ds = load_datasets(data_dir)
    except Exception as e:  # noqa: BLE001  (mirror unreachable, MD5 mismatch, disk full...)
        emit("error", code="dataset_failed", message=f"{type(e).__name__}: {str(e)[:1000]}")
        return EXIT_FAILED
    fingerprint = dataset_fingerprint(data_dir)
    phase("dataset_ready", dataset="MNIST", source="torchvision.datasets.MNIST (default mirrors, MD5-checked)",
          train_samples=len(train_ds), test_samples=len(test_ds), files=fingerprint,
          seconds=round(time.monotonic() - t0, 2))
    train_ds = _subset(train_ds, cfg.get("limit_train"), seed)
    test_ds = _subset(test_ds, cfg.get("limit_test"), seed + 1)

    pin = device.type == "cuda"
    g = torch.Generator().manual_seed(seed)
    train_loader = torch.utils.data.DataLoader(train_ds, batch_size=cfg["batch_size"], shuffle=True,
                                               generator=g, num_workers=num_workers, pin_memory=pin,
                                               persistent_workers=num_workers > 0)
    test_loader_ = torch.utils.data.DataLoader(test_ds, batch_size=1000, shuffle=False,
                                               num_workers=num_workers, pin_memory=pin)

    model = build_model().to(device)
    opt = torch.optim.Adam(model.parameters(), lr=cfg["learning_rate"])
    initial = _param_vector(model)
    n_params = int(initial.numel())
    first_param_device = str(next(model.parameters()).device)

    phase("training_started", device=str(device), param_device=first_param_device, parameters=n_params,
          epochs=cfg["epochs"], batch_size=cfg["batch_size"], learning_rate=cfg["learning_rate"],
          train_samples=len(train_ds), test_samples=len(test_ds), optimizer="Adam")

    epochs = []
    train_seconds = 0.0
    for epoch in range(1, cfg["epochs"] + 1):
        model.train()
        if device.type == "cuda":
            torch.cuda.reset_peak_memory_stats()
            torch.cuda.synchronize()
        te = time.monotonic()
        loss_sum, seen, batches, batch_device = 0.0, 0, 0, None
        for x, y in train_loader:
            check_deadline()
            x, y = x.to(device, non_blocking=pin), y.to(device, non_blocking=pin)
            batch_device = batch_device or str(x.device)
            opt.zero_grad(set_to_none=True)
            loss = F.cross_entropy(model(x), y)
            loss.backward()
            opt.step()
            loss_sum += loss.item() * y.numel()  # sample-weighted: exact mean over the epoch
            seen += y.numel()
            batches += 1
        if device.type == "cuda":
            torch.cuda.synchronize()
        dt = time.monotonic() - te
        train_seconds += dt
        check_deadline()
        ev = evaluate(model, test_loader_, device)
        row = {"epoch": epoch, "train_loss": loss_sum / max(seen, 1), "train_samples": seen, "batches": batches,
               "epoch_seconds": round(dt, 3), "samples_per_second": round(seen / dt, 1) if dt > 0 else None,
               "batch_device": batch_device, **ev}
        if device.type == "cuda":
            row["peak_cuda_memory_bytes"] = int(torch.cuda.max_memory_allocated())
        epochs.append(row)
        emit("metric", **row)
        phase("epoch_completed", epoch=epoch, of=cfg["epochs"], train_loss=row["train_loss"],
              test_accuracy=row["test_accuracy"], test_loss=row["test_loss"])

    final = _param_vector(model)
    l2 = float(torch.linalg.vector_norm(final - initial).item())
    phase("saving_artifacts")
    torch.save(model.state_dict(), out_dir / "model.pt")
    # Reload check on the saved file itself: the accuracy we report must be reproducible from model.pt.
    reloaded = evaluate(load_model(out_dir / "model.pt", device), test_loader_, device)

    losses = [e["train_loss"] for e in epochs]
    metrics = {
        "template": TEMPLATE, "device": str(device), "epochs": epochs,
        "final": {"train_loss": losses[-1], "test_loss": epochs[-1]["test_loss"],
                  "test_accuracy": epochs[-1]["test_accuracy"],
                  "reloaded_test_accuracy": reloaded["test_accuracy"],
                  "reload_matches": abs(reloaded["test_accuracy"] - epochs[-1]["test_accuracy"]) < 1e-9},
        "loss_decreased": len(losses) < 2 or losses[-1] < losses[0],
        "parameters": n_params, "param_l2_change": l2, "params_changed": l2 > 0.0,
        "train_seconds": round(train_seconds, 3),
        "samples_per_second": round(sum(e["train_samples"] for e in epochs) / train_seconds, 1)
        if train_seconds > 0 else None,
        "runtime_seconds": round(time.monotonic() - t_start, 3), "dataset_files": fingerprint,
    }
    (out_dir / "metrics.json").write_text(json.dumps(metrics, indent=2, sort_keys=True), encoding="utf-8")
    (out_dir / "config.json").write_text(json.dumps({"template": TEMPLATE, "config": cfg,
                                                     "architecture": "conv32-conv64-maxpool-dropout-fc128-fc10",
                                                     "optimizer": "Adam", "loss": "cross_entropy"},
                                                    indent=2, sort_keys=True), encoding="utf-8")
    manifest = {name: {"sha256": sha256_file(out_dir / name), "bytes": (out_dir / name).stat().st_size}
                for name in ARTIFACTS}
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    emit("result", outcome="succeeded", **metrics["final"], param_l2_change=l2,
         samples_per_second=metrics["samples_per_second"], runtime_seconds=metrics["runtime_seconds"],
         device=str(device), gpu_name=env.get("gpu_name"), artifacts=manifest)
    # `training_completed` is emitted by the reporter AFTER the artifacts are uploaded and hash-checked.
    return EXIT_OK


def _parse_args(argv=None):
    ap = argparse.ArgumentParser(description="OpenGrid pytorch-mnist training workload")
    ap.add_argument("--out", required=True, help="artifact output directory")
    ap.add_argument("--data", required=True, help="dataset cache directory")
    ap.add_argument("--config-b64", help="base64 JSON config (what bootstrap passes)")
    ap.add_argument("--epochs", type=int)
    ap.add_argument("--batch-size", type=int)
    ap.add_argument("--learning-rate", type=float)
    ap.add_argument("--seed", type=int)
    ap.add_argument("--limit-train", type=int, help="tests only: train on a seeded subset")
    ap.add_argument("--limit-test", type=int, help="tests only: evaluate on a seeded subset")
    ap.add_argument("--max-seconds", type=float, help="wall-clock limit for this process")
    ap.add_argument("--allow-cpu", action="store_true", help="tests/local verification only")
    ap.add_argument("--num-workers", type=int, default=2)
    return ap.parse_args(argv)


def main(argv=None) -> int:
    a = _parse_args(argv)
    try:
        raw = json.loads(base64.b64decode(a.config_b64, validate=True)) if a.config_b64 else {}
        if not isinstance(raw, dict):
            raise ValueError("config must be a JSON object")
        for k in ("epochs", "batch_size", "learning_rate", "seed", "limit_train", "limit_test"):
            if getattr(a, k) is not None:
                raw[k] = getattr(a, k)
        cfg = validate_config(raw)
    except (ValueError, TypeError) as e:
        emit("error", code="invalid_config", message=str(e))
        return EXIT_FAILED
    try:
        import torch  # noqa: F401  (fail clearly if the environment is broken)
    except Exception as e:  # noqa: BLE001
        emit("error", code="torch_import_failed", message=f"{type(e).__name__}: {e}")
        return EXIT_FAILED
    try:
        return train(cfg, out_dir=a.out, data_dir=a.data, allow_cpu=a.allow_cpu, max_seconds=a.max_seconds,
                     num_workers=a.num_workers)
    except TrainingTimeout as e:
        emit("error", code="timeout", message=str(e))
        return EXIT_TIMEOUT
    except Exception as e:  # noqa: BLE001
        import torch
        oom_types = tuple(t for t in (getattr(torch.cuda, "OutOfMemoryError", None),) if t)
        if (oom_types and isinstance(e, oom_types)) or "out of memory" in str(e).lower():
            emit("error", code="oom", message=str(e)[:2000])
            return EXIT_OOM
        emit("error", code="training_failed", message=f"{type(e).__name__}: {str(e)[:2000]}")
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
