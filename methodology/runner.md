# Job runner: what runs on the GPU

When a `pytorch-mnist` job launches, the rented instance gets ONE thing from OpenGrid: a cloud-init user_data
bash script rendered by `runner/bootstrap.py`. It contains the OpenGrid runner (`runner/__init__.py`,
`VERSION`, `mnist_train.py`, `reporter.py`, the hash-locked requirement files) as sha256-verified base64, the
validated job config as base64 JSON, and a job-scoped callback token. **It does not contain or run any user
code**, and it never receives provider or storage credentials. Code: `runner/`. Tests: `tests/test_runner.py`,
`tests/test_bootstrap.py`.

## 1. Sequence on the instance

| # | Step | Phase event (type `phase`, `data.phase`) | Evidence in the event |
|---|---|---|---|
| 1 | stage 1 (cloud-init, root): umask 077, token → root-only file, unpack + `sha256sum -c` the runner, detach stage 2 as a transient systemd unit (fallback `setsid nohup`) so cloud-init finishes | n/a (logged) | `/var/log/opengrid-job.log` |
| 2 | stage 2 starts | `bootstrap_started` | template + version, OS, kernel, arch, CPUs, RAM, free disk, user |
| 3 | `nvidia-smi` | `gpu_detected` | `name, driver_version, memory.total, compute_cap` CSV + full `nvidia-smi` text |
| 4 | system `python3` ≥ 3.10 | `python_ready` | version, path |
| 5 | venv + pinned PyTorch (§2) or fallback | `torch_installed` (+ `env_fallback` if used) | python/torch/torchvision/numpy versions, `torch.version.cuda`, `env_source` |
| 6 | `torch.cuda.is_available()` | `cuda_available` | device count, name, compute capability, cuDNN |
| 7 | reporter starts trainer under `timeout` | `device_selected` (from the trainer) | device, GPU name, CC, torch, CUDA runtime, cuDNN |
| 8 | MNIST download | `dataset_ready` | train/test sample counts, size + sha256 of every raw file (torchvision itself MD5-checks each archive) |
| 9 | training | `training_started`, then per epoch `metric` + `epoch_completed` | parameter count, the device the parameters and batches are on, per-epoch mean train loss over every sample, held-out test loss/accuracy, epoch seconds, samples/s, peak CUDA memory |
| 10 | save + reload check | `saving_artifacts`, `result` | final metrics, accuracy of `model.pt` re-loaded from disk, L2 distance between initial and final weights, artifact sha256s |
| 11 | upload | `uploading_artifacts`, `artifacts_uploaded` | names, sha256, bytes; failures |
| 12 | finish | `training_completed` or `training_failed`, then `POST /complete` | outcome + summary |
| n/a | any bootstrap failure | `bootstrap_failed` (+ `POST /complete` `failed`) | reason, step |

During training the reporter also sends a `gpu` event every 15 s (`nvidia-smi`: utilization, memory used,
temperature, power) and forwards every non-JSON line of trainer stdout/stderr as `log` (≤ 4,000 chars per
line, ≤ 5,000 lines, then counted).

After `complete` the script idles. **It never shuts the OS down**: Lambda documents that `shutdown`/`poweroff`
put the instance in *Alert* status "and billing will continue". OpenGrid terminates the instance through
the provider API (existing `deployments.terminate` → tracker/reconcile), on complete, on failure, on the
deadline, or on cancel, whether or not the runner ever calls back.

## 2. Pinned software and where the pins come from

| Package | Pin | Source |
|---|---|---|
| torch | `2.4.1+cu121` | `https://download.pytorch.org/whl/cu121/torch/` lists `torch-2.4.1+cu121-cp310/cp311/cp312-linux_x86_64.whl` (e.g. cp310 sha256 `9a5f0b10…fe975`, cp312 `ab491610…05a53`) |
| torchvision | `0.19.1+cu121` | `https://download.pytorch.org/whl/cu121/torchvision/` (cp310 `b8cc4bf3…65379`, cp312 `fb0cd062…27eae`); 0.19.x is the torchvision release paired with torch 2.4.x |
| numpy | `1.26.4` | pypi / the same index (cp310-cp312 manylinux x86_64) |
| everything else | exact (nvidia-*-cu12 12.1.x, cudnn 9.1.0.70, triton 3.0.0, sympy, jinja2, …) | resolved by `uv pip compile --generate-hashes` per Python version |

Lock files (hash-locked, every file checked by `pip install --require-hashes --no-deps --only-binary :all:`
from `--index-url https://download.pytorch.org/whl/cu121 --extra-index-url https://pypi.org/simple`):
`runner/requirements-gpu.txt` (CPython 3.10: Lambda Stack 22.04, the default image),
`requirements-gpu-py311.txt`, `requirements-gpu-py312.txt` (Lambda Stack 24.04). The CUDA 12.1 build bundles
its own CUDA runtime/cuDNN wheels, so it needs only an NVIDIA driver ≥ 525 on the host, and it supports
compute capability ≥ 7.0 (hence the workload's sm_70+ requirement).

**Fallback** (recorded as `env_source=preinstalled_fallback` in `env_fallback`, `torch_installed`, `env.json`
and the complete summary): used ONLY when the pinned install cannot be used: Python other than 3.10-3.12,
a non-x86_64 machine (there are **no** `torch 2.4.1+cu121` aarch64 wheels, so e.g. GH200 arm64 instances would
always fall back), or the install/verification failed. It then runs on the image's preinstalled PyTorch
(Lambda Stack); if there is none, `bootstrap_failed`.

## 3. Lambda's default image (what the bootstrap assumes)

From Lambda's docs (checked 2026-10-13):
- Default image is Ubuntu 22.04 LTS with **Lambda Stack** preinstalled: NVIDIA driver, CUDA, cuDNN, NCCL,
  **NVIDIA Container Toolkit**, PyTorch, torchvision, TensorFlow, JAX, Triton, JupyterLab; **Python 3.10**.
  Alternative images via the API: Lambda Stack 24.04 (Python 3.12), GPU Base 22.04/24.04, Ubuntu Server.
  https://docs.lambda.ai/public-cloud/on-demand/
- "Your ODC instance has Docker and the NVIDIA Container Toolkit installed by default"; venvs are supported
  (`--system-site-packages` to reuse Lambda Stack). Exact preinstalled PyTorch version is not documented.
  https://docs.lambda.ai/public-cloud/on-demand/managing-system-environment/
- user_data: "An instance configuration string specified in a valid cloud-init user-data format … must be plain
  text and cannot exceed 1MB". https://docs.lambda.ai/api/cloud ; default login user `ubuntu`.
- OS shutdown does not stop billing: https://docs.lambda.ai/public-cloud/on-demand/creating-managing-instances/
- Firewall rules restrict **inbound** traffic only (default: SSH 22 + ICMP); egress is not documented as
  restricted (https://docs.lambda.ai/public-cloud/firewalls/). Outbound HTTPS to download.pytorch.org,
  pypi.org, the MNIST mirror and OpenGrid is ASSUMED and is the first thing the first real run proves
  (`torch_installed`, `dataset_ready`, and every callback arriving at all).
- cloud-init runs user_data scripts once per instance, late in boot (final stage, `scripts_user`), as root.
  https://docs.cloud-init.io/en/latest/explanation/format/user-data-script.html

## 4. The founder's checks and the event that proves each

The brief refers to "the founder's 12 checks" without listing them; this is the list the runner is built to
prove (each item names the event/artifact OpenGrid stores):

1. **A real NVIDIA GPU was attached**: `gpu_detected` (nvidia-smi name/driver/VRAM/CC) and `env.json`.
2. **PyTorch saw it**: `cuda_available` (`torch.cuda.is_available()`, device name, CC); the trainer refuses CPU
   (exit 5 → `bootstrap_failed`/`cuda_unavailable`) unless `--allow-cpu`, which the bootstrap never passes.
3. **Training ran on the GPU**: `training_started.param_device`, every `metric.batch_device = cuda:0`,
   `peak_cuda_memory_bytes` > 0, `gpu` samples with utilization/memory during training.
4. **Exact software versions**: `torch_installed` + `env.json` (python, torch, torchvision, numpy, CUDA build,
   cuDNN, driver, `env_source` pinned vs fallback).
5. **The real dataset**: `dataset_ready` (60,000 / 10,000 samples, sha256 + bytes per raw file; torchvision's
   MD5 checks).
6. **Learning happened**: per-epoch mean train loss (`metric`), `metrics.loss_decreased`.
7. **Held-out accuracy**: `test_accuracy` / `test_loss` on the 10,000-image test set each epoch.
8. **Weights actually changed**: `metrics.param_l2_change` > 0 (L2 between initial and final weights).
9. **The model file is the trained model**: `result.reloaded_test_accuracy == test_accuracy`
   (`model.pt` re-loaded from disk and re-evaluated); `load_model()`+`evaluate()` let anyone repeat it.
10. **Artifacts are intact**: `manifest.json` sha256 per file; the server verifies `X-Content-SHA256` on
    upload; the reporter fails the job (`artifact_upload_failed`) if any upload fails or a hash differs.
11. **Measured, not estimated, runtime/throughput**: epoch seconds, samples/s, `runtime_seconds`.
12. **Clean ending**: exactly one `complete` with outcome `succeeded|failed|oom|timeout`; then OpenGrid
    terminates via the provider API and reconciliation confirms the instance is gone (job states
    `termination_confirmed` → `reconciled`; that part is proven by the deployment tracker, not the runner).

## 5. Wire format (runner side; the server is `jobs/` in JOBS CORE)

`Authorization: Bearer ogj_<job_id>.<token>` on every call; redirects are refused.
- `POST /v1/jobs/{job}/runner/events` body `{"events": [{"seq", "type", "at", "data"}, …]}`: ≤ 100 events and
  ≤ 512 KB per batch, single event data ≤ 64 KB (else replaced by a truncated preview).
  `type` ∈ phase|log|metric|gpu|result|error. `phase` events carry `data.phase`; `error` events `data.code`.
- `PUT /v1/jobs/{job}/runner/artifacts/{name}` raw bytes, `Content-Type: application/octet-stream`,
  `X-Content-SHA256: <hex>`; names uploaded: `model.pt config.json metrics.json env.json manifest.json`.
- `POST /v1/jobs/{job}/runner/complete` `{"outcome": "succeeded|failed|oom|timeout", "summary": {...}}`;
  `summary.reason` ∈ `null | bootstrap_failed | training_failed | out_of_memory | training_timed_out |
  cuda_unavailable | artifact_upload_failed | dataset…`; also `exit_code, runtime_seconds, env_source,
  result, error, artifacts`.

Delivery: seq is a persisted counter shared by every process on the instance; events are written to an
on-disk spool before any send and deleted only after a 2xx (the server ignores duplicate seqs, so a retried
batch is harmless). Network errors, 408/425/429/5xx → exponential backoff with jitter (U(0.5,1.5)·2^n s,
cap 60 s); 401/403 or 409 → stop calling (token revoked/expired or job terminal); other 4xx → that batch is
moved aside (`.rejected`) so it cannot wedge the queue. An undelivered `complete` is kept in
`complete.pending.json` and retried by the bootstrap for ~5 minutes. Exit codes of the trainer: 0 succeeded,
2 failed, 3 oom (`torch.cuda.OutOfMemoryError`), 4 timeout (`--max-seconds`), 5 CUDA unavailable.

Time limits (from the job's `max_runtime_minutes`, 5-720): deadline = boot + limit − 120 s; the reporter runs
under `timeout --kill-after=30 <remaining>`; it kills the trainer at remaining − 60 s; the trainer stops itself
at remaining − 120 s with a clean `timeout` outcome. OpenGrid's own deadline enforcement still applies.

## 6. Security

- **Token scope**: a per-job token derived server-side (HMAC of the pepper, job id and a nonce; only the nonce is
  stored), valid only for that job's three runner routes, until deadline + 15 min, revocable. It cannot read
  anything, cannot touch other jobs and grants no API-key scopes.
- **No secrets in logs**: the script starts with `set +x`, writes the token with a quoted heredoc into a 0600
  file in a 0700 directory, builds the curl header file from that file at runtime, and never echoes it; the
  reporter reads it from the file (never argv/env), masks it in `repr`, never logs request headers, and scrubs
  it from any forwarded child output. `/var/log/opengrid-job.log` is 0600. Callers must not log/store the
  rendered script; record `TEMPLATE_VERSION`, `BUNDLE_SHA256` and `script_sha256(script)` instead.
  Residual: cloud-init itself keeps a root-only copy of user_data on the instance, and the provider's metadata
  service holds it; with an ephemeral SSH key nobody can log in, and the token dies with the job.
- **No arbitrary code / no injection**: templates are allowlisted (`pytorch-mnist`); `job_id`
  `^[A-Za-z0-9_-]{1,64}$`, token `^[A-Za-z0-9_-]{16,256}$`, base URL `https://host[:port][/path]` from
  `[A-Za-z0-9.-]` / `[A-Za-z0-9._~-]` (plain http only for localhost in tests); config validated with the
  trainer's own bounds (epochs 1-20, batch 16-1024, lr 1e-5-0.1, seed 0-2³¹−1; unknown keys rejected) and
  passed as base64 JSON. Anything else raises `ValueError`; values are never "escaped around".
- **Supply chain**: every wheel is hash-pinned; the embedded runner files are sha256-verified before stage 2
  is started (a tampered file stops the bootstrap and reports `bootstrap_failed`).
- The output is deterministic for the same inputs except the token line.

## 7. What has and has not been verified

All verification so far is **local**: no cloud GPU, no provider call.
- **CPU run of the real trainer on real MNIST** (Windows, Python 3.12.15, torch 2.4.1+cpu, torchvision
  0.19.1+cpu, numpy 1.26.4; `--allow-cpu`, 2 full epochs, batch 128, lr 0.001, seed 42): epoch 1 train loss
  0.2400, test accuracy 98.32% (loss 0.0500); epoch 2 train loss 0.0852, test accuracy 98.81% (loss 0.0382);
  `model.pt` reload reproduced 98.81%; L2 weight change 23.36; 1,199,882 parameters; ~1,600 samples/s;
  88.6 s total. MNIST came from torchvision's mirror list (yann.lecun.com returned 404, the
  `ossci-datasets.s3.amazonaws.com` mirror served all four files, MD5-checked).
- Reporter against a fake callback server: seq, batching, retry/backoff, offline spool + recovery, auth stop,
  artifact sha256 upload, complete, supervisor success / oom / timeout / upload failure, token scrubbing,
  and an end-to-end CPU smoke (subset) through `python -m runner.reporter run`.
- Bootstrap: bash syntax, size, determinism, injection rejection, embedded files byte-identical, and stage 1
  executed under Git Bash (token file, header file, sha256 verification, tamper → fail closed).

- **The rendered user_data, executed for real in `ubuntu:22.04` Docker containers** (Python 3.10.12, run as
  root from `/var/lib/cloud/instance/scripts/part-001` like cloud-init would; no systemd → `setsid` path;
  fake callback server on 127.0.0.1):
  1. *Pinned path, real local GPU* (`docker --gpus all`, NVIDIA GeForce RTX 3070, sm_86, Windows driver
     610.88 via WSL2; a desktop GPU, NOT a cloud instance): hash-locked install of torch 2.4.1+cu121 /
     torchvision 0.19.1+cu121 / numpy 1.26.4 + all CUDA wheels succeeded (~4 min at ~16 MB/s);
     `cuda_available` true (cuDNN 90100); 3 epochs batch 128 lr 0.001 seed 42 on `cuda:0`: train loss
     0.2394 → 0.0892 → 0.0668, test accuracy 98.35% → 98.69% → 98.94%, reload 98.94% (match), L2 weight change
     27.56, ~9,950 samples/s, peak CUDA memory 471 MB, 26.3 s trainer runtime; 5 artifacts uploaded with
     matching sha256; one `complete` `succeeded`; token count in the job log: 0; token file/log 0600,
     job dir 0700.
  2. *Pinned path, no GPU* (nvidia-smi stub): same install succeeded, then `bootstrap_failed`
     "torch.cuda.is_available() is False" was reported and `complete` `failed` posted.
  3. *Fallback path*: download.pytorch.org blocked, system torch 2.4.1+cpu preinstalled → `env_fallback`,
     `torch_installed env_source=preinstalled_fallback`, then `bootstrap_failed` at the CUDA check.

Not verifiable without a real cloud GPU instance: that Lambda runs this user_data as documented (cloud-init
final stage, as root, systemd-run available), outbound network from Lambda, install time from Lambda's
network, `torch.cuda` on Lambda's datacenter driver/GPUs, and the termination/reconciliation that follows.
