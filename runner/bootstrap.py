"""Render the cloud-init user_data that turns a fresh GPU instance into one OpenGrid training job.

APP-SIDE ONLY (stdlib, no torch). routing/deployments.launch swaps the placeholder startup_script
"opengrid-job:<job_id>" for render(job_id, token, ...) at provision time. The rendered script contains the job's
callback token, so callers must never log or store it — record TEMPLATE_VERSION, BUNDLE_SHA256 and
script_sha256(script) instead.

What the script does (methodology/runner.md has the full list and the evidence each step produces):
  stage 1 (cloud-init, root): umask 077, write the token to a root-only file without echoing it, decode the
          embedded runner files and verify their sha256, then detach stage 2 as a transient systemd unit so
          cloud-init finishes normally (fallback: setsid nohup).
  stage 2: log to /var/log/opengrid-job.log; phases bootstrap_started -> gpu_detected (nvidia-smi) ->
          python_ready -> torch_installed (pinned venv: torch 2.4.1+cu121 / torchvision 0.19.1+cu121 /
          numpy 1.26.4, every wheel hash-checked; fallback to the image's preinstalled PyTorch ONLY if that
          fails, recorded as env_source=preinstalled_fallback) -> cuda_available -> reporter runs the trainer
          (dataset_ready, training_started, epoch_completed..., artifacts_uploaded, training_completed |
          training_failed) under `timeout`. ANY failure posts complete(outcome=failed, reason=bootstrap_failed
          ...) so OpenGrid terminates the instance via the provider API. The script never shuts the OS down:
          on Lambda an OS shutdown does NOT stop billing.
Injection safety: every interpolated value is validated against a strict allowlist (job id, token, base URL,
template) or is base64 (config JSON, runner files). Output is deterministic for the same inputs except the token.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path

from runner import RUNNER_VERSION
from runner.mnist_train import validate_config

TEMPLATES = ("pytorch-mnist",)
TEMPLATE_VERSION = f"pytorch-mnist@{RUNNER_VERSION}"
USER_DATA_LIMIT = 1_000_000          # Lambda: "plain text and cannot exceed 1MB"
PYTORCH_INDEX = "https://download.pytorch.org/whl/cu121"
PYPI_INDEX = "https://pypi.org/simple"
PINS = {"torch": "2.4.1+cu121", "torchvision": "0.19.1+cu121", "numpy": "1.26.4"}
LOCKS = {"3.10": "requirements-gpu.txt", "3.11": "requirements-gpu-py311.txt", "3.12": "requirements-gpu-py312.txt"}

_RUNNER_DIR = Path(__file__).resolve().parent
EMBEDDED = ("__init__.py", "VERSION", "mnist_train.py", "reporter.py", *LOCKS.values())

_JOB_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}\Z")   # \Z, not $: no trailing newline
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{16,256}\Z")
_BASE_URL = re.compile(r"^https://[A-Za-z0-9.-]{1,253}(:[0-9]{1,5})?(/[A-Za-z0-9._~-]+)*/?\Z")
_HTTP_LOCAL = re.compile(r"^http://(localhost|127\.0\.0\.1)(:[0-9]{1,5})?(/[A-Za-z0-9._~-]+)*/?\Z")


def _files() -> dict:
    return {name: (_RUNNER_DIR / name).read_bytes() for name in EMBEDDED}


def bundle_sha256() -> str:
    """One hash over every embedded file (name + content): changes whenever the GPU-side code changes."""
    h = hashlib.sha256()
    for name, data in sorted(_files().items()):
        h.update(name.encode() + b"\0" + hashlib.sha256(data).digest())
    return h.hexdigest()


BUNDLE_SHA256 = bundle_sha256()


def script_sha256(script: str) -> str:
    return hashlib.sha256(script.encode("utf-8")).hexdigest()


def _b64_block(data: bytes) -> str:
    s = base64.b64encode(data).decode("ascii")
    return "\n".join(s[i:i + 76] for i in range(0, len(s), 76))


def validate_inputs(job_id, token, base_url, template, config, max_runtime_minutes, allow_http=False):
    if not isinstance(job_id, str) or not _JOB_ID.match(job_id):
        raise ValueError("job_id must match ^[A-Za-z0-9_-]{1,64}$")
    if not isinstance(token, str) or not _TOKEN.match(token):
        raise ValueError("token must match ^[A-Za-z0-9_-]{16,256}$")
    if not isinstance(base_url, str) or not (_BASE_URL.match(base_url) or (allow_http and _HTTP_LOCAL.match(base_url))):
        raise ValueError("base_url must be https://host[:port][/path] with only [A-Za-z0-9._~-] characters")
    if template not in TEMPLATES:
        raise ValueError(f"template must be one of {TEMPLATES}")
    if not isinstance(config, dict):
        raise ValueError("config must be a dict")
    cfg = validate_config(config)
    if isinstance(max_runtime_minutes, bool) or not isinstance(max_runtime_minutes, int) \
            or not 5 <= max_runtime_minutes <= 720:
        raise ValueError("max_runtime_minutes must be an integer in [5, 720]")
    return cfg


def render(job_id: str, token: str, *, base_url: str, template: str = "pytorch-mnist", config: dict,
           max_runtime_minutes: int = 30, allow_http: bool = False) -> str:
    """The user_data bash script. Raises ValueError on any invalid input (nothing is ever escaped 'around')."""
    cfg = validate_inputs(job_id, token, base_url, template, config, max_runtime_minutes, allow_http)
    base_url = base_url.rstrip("/")
    config_b64 = base64.b64encode(json.dumps(cfg, sort_keys=True, separators=(",", ":")).encode()).decode()
    files = _files()
    blocks, sums = [], []
    for name in EMBEDDED:
        blocks.append(f"base64 -d > \"$OG_DIR/app/runner/{name}\" <<'OG_B64_EOF'\n{_b64_block(files[name])}\nOG_B64_EOF")
        sums.append(f"{hashlib.sha256(files[name]).hexdigest()}  runner/{name}")
    subst = {
        "@@JOB_ID@@": job_id, "@@BASE_URL@@": base_url, "@@TEMPLATE@@": template,
        "@@TEMPLATE_VERSION@@": TEMPLATE_VERSION, "@@BUNDLE_SHA256@@": BUNDLE_SHA256,
        "@@CONFIG_B64@@": config_b64, "@@MAX_SECONDS@@": str(max_runtime_minutes * 60),
        "@@PYTORCH_INDEX@@": PYTORCH_INDEX, "@@PYPI_INDEX@@": PYPI_INDEX,
        "@@PIN_TORCH@@": PINS["torch"], "@@PIN_TORCHVISION@@": PINS["torchvision"],
        "@@FILES@@": "\n".join(blocks), "@@SUMS@@": "\n".join(sums),
    }
    script = _TEMPLATE
    for k, v in subst.items():
        script = script.replace(k, v)
    # The token goes in last and in exactly one place: a quoted heredoc (no expansion), under `set +x`.
    script = script.replace("@@TOKEN@@", token)
    if len(script.encode()) >= USER_DATA_LIMIT:
        raise ValueError(f"user_data is {len(script.encode())} bytes; Lambda's limit is 1 MB")
    return script


_TEMPLATE = r"""#!/bin/bash
# OpenGrid job bootstrap. template=@@TEMPLATE@@ version=@@TEMPLATE_VERSION@@ bundle=@@BUNDLE_SHA256@@
# Generated by runner/bootstrap.py. Contains a job-scoped, expiring callback token: never share or log it.
# Runs no user-supplied code: only the embedded, sha256-verified OpenGrid runner (see methodology/runner.md).
set -euo pipefail
umask 077
{ set +x; } 2>/dev/null
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export LC_ALL=C.UTF-8 DEBIAN_FRONTEND=noninteractive PIP_NO_INPUT=1 PIP_DISABLE_PIP_VERSION_CHECK=1

OG_JOB_ID='@@JOB_ID@@'
OG_BASE_URL='@@BASE_URL@@'
OG_TEMPLATE='@@TEMPLATE@@'
OG_TEMPLATE_VERSION='@@TEMPLATE_VERSION@@'
OG_CONFIG_B64='@@CONFIG_B64@@'
OG_MAX_SECONDS=@@MAX_SECONDS@@
if [ "$(id -u)" = 0 ]; then
  OG_DIR=/opt/opengrid-job
  OG_LOG=/var/log/opengrid-job.log
else
  OG_DIR="${HOME:-/tmp}/.opengrid-job"
  OG_LOG="$OG_DIR/job.log"
fi
OG_STEP=stage1

# Last-resort failure report that needs nothing but curl (used if python3/the reporter is unusable).
og_curl_fail() {
  [ -f "$OG_DIR/curl.hdr" ] || return 1
  curl -sS -o /dev/null --max-time 20 --retry 5 --retry-delay 5 --retry-all-errors \
    -H @"$OG_DIR/curl.hdr" -H 'Content-Type: application/json' \
    --data "{\"outcome\":\"failed\",\"summary\":{\"reason\":\"$1\",\"message\":\"step $OG_STEP\",\"via\":\"curl\"}}" \
    "$OG_BASE_URL/v1/jobs/$OG_JOB_ID/runner/complete" || \
  curl -sS -o /dev/null --max-time 20 --retry 5 --retry-delay 5 \
    -H @"$OG_DIR/curl.hdr" -H 'Content-Type: application/json' \
    --data "{\"outcome\":\"failed\",\"summary\":{\"reason\":\"$1\",\"message\":\"step $OG_STEP\",\"via\":\"curl\"}}" \
    "$OG_BASE_URL/v1/jobs/$OG_JOB_ID/runner/complete"
}

if [ "${1:-}" != "--stage2" ]; then
  # ------------------------------------------------------------------ stage 1 (cloud-init)
  mkdir -p "$OG_DIR/app/runner" "$OG_DIR/state" "$OG_DIR/out" "$OG_DIR/data"
  chmod 700 "$OG_DIR"
  touch "$OG_LOG" && chmod 600 "$OG_LOG"
  echo "opengrid: job $OG_JOB_ID bootstrap; log in $OG_LOG"
  exec >>"$OG_LOG" 2>&1
  echo "[opengrid $(date -u +%FT%TZ)] stage1 start template=$OG_TEMPLATE version=$OG_TEMPLATE_VERSION user=$(id -un)"
  trap 'rc=$?; if [ $rc -ne 0 ]; then echo "[opengrid] stage1 failed rc=$rc"; og_curl_fail bootstrap_failed || true; fi' EXIT
  echo $(( $(date +%s) + OG_MAX_SECONDS - 120 )) > "$OG_DIR/deadline"
  { set +x; } 2>/dev/null
  cat > "$OG_DIR/token" <<'OG_TOKEN_EOF'
@@TOKEN@@
OG_TOKEN_EOF
  { printf 'Authorization: Bearer ogj_%s.' "$OG_JOB_ID"; tr -d '\n' < "$OG_DIR/token"; printf '\n'; } > "$OG_DIR/curl.hdr"
  chmod 600 "$OG_DIR/token" "$OG_DIR/curl.hdr"
  OG_STEP=unpack_runner
@@FILES@@
  ( cd "$OG_DIR/app" && sha256sum -c --quiet - <<'OG_SUMS_EOF'
@@SUMS@@
OG_SUMS_EOF
  )
  OG_STEP=detach
  if [ -f "$0" ] && [ "$0" != "$OG_DIR/job.sh" ]; then install -m 700 "$0" "$OG_DIR/job.sh"; fi
  # cloud-init always runs user_data from a file (scripts-user); anything else is a failed bootstrap.
  [ -f "$OG_DIR/job.sh" ] || { echo "[opengrid] cannot locate own script file"; exit 1; }
  trap - EXIT
  if command -v systemd-run >/dev/null 2>&1 && \
     systemd-run --unit "opengrid-job-$OG_JOB_ID" --description "OpenGrid job $OG_JOB_ID" --collect --quiet \
       /bin/bash "$OG_DIR/job.sh" --stage2; then
    echo "[opengrid] stage2 started as systemd unit opengrid-job-$OG_JOB_ID"
  else
    setsid nohup /bin/bash "$OG_DIR/job.sh" --stage2 </dev/null >/dev/null 2>&1 &
    echo "[opengrid] stage2 started with setsid (pid $!)"
  fi
  exit 0
fi

# -------------------------------------------------------------------- stage 2 (detached)
exec >>"$OG_LOG" 2>&1
cd "$OG_DIR/app"
OG_DEADLINE=$(cat "$OG_DIR/deadline")
export OG_ENV_SOURCE=none
echo "[opengrid $(date -u +%FT%TZ)] stage2 start"

og() {
  local action=$1; shift
  python3 -m runner.reporter "$action" --base-url "$OG_BASE_URL" --job "$OG_JOB_ID" \
    --token-file "$OG_DIR/token" --state-dir "$OG_DIR/state" "$@"
}
og_phase() { local name=$1; shift; og emit --type phase --name "$name" "$@" || true; }
og_fail() {   # og_fail <reason> <message>: report and stop; OpenGrid then terminates the instance
  echo "[opengrid] FAILED reason=$1 step=$OG_STEP: $2"
  touch "$OG_DIR/completed"
  og complete --outcome failed --code "$1" --message "$2 (step $OG_STEP)" || og_curl_fail "$1" || true
  exit 1
}
trap 'rc=$?; if [ $rc -ne 0 ] && [ ! -f "$OG_DIR/completed" ]; then touch "$OG_DIR/completed"; og complete --outcome failed --code bootstrap_failed --message "unexpected exit $rc at step $OG_STEP" || og_curl_fail bootstrap_failed || true; fi' EXIT

OG_STEP=bootstrap_started
command -v python3 >/dev/null 2>&1 || { og_curl_fail bootstrap_failed || true; touch "$OG_DIR/completed"; exit 1; }
og_phase bootstrap_started --kv template="$OG_TEMPLATE" --kv template_version="$OG_TEMPLATE_VERSION" \
  --kv os="$( (. /etc/os-release && echo "$PRETTY_NAME") 2>/dev/null || echo unknown)" --kv kernel="$(uname -r)" \
  --kv arch="$(uname -m)" --kv cpus="$(nproc)" --kv user="$(id -un)" \
  --kv mem_kb="$(awk '/MemTotal/{print $2}' /proc/meminfo)" \
  --kv disk_free_kb="$(df -Pk "$OG_DIR" | awk 'NR==2{print $4}')"

OG_STEP=gpu_detected
if ! nvidia-smi --query-gpu=name,driver_version,memory.total,compute_cap --format=csv,noheader \
     > "$OG_DIR/state/gpu.txt" 2>&1; then
  og_fail bootstrap_failed "nvidia-smi failed: no NVIDIA GPU/driver visible"
fi
nvidia-smi > "$OG_DIR/state/nvidia-smi.txt" 2>&1 || true
og_phase gpu_detected --kv-file gpus="$OG_DIR/state/gpu.txt" --kv-file nvidia_smi="$OG_DIR/state/nvidia-smi.txt"

OG_STEP=python_ready
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || og_fail bootstrap_failed "python3 >= 3.10 required"
OG_PYV=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
og_phase python_ready --kv python="$OG_PYV" --kv path="$(command -v python3)"

OG_STEP=torch_install
case "$OG_PYV" in
  3.10) OG_LOCK=requirements-gpu.txt ;;
  3.11) OG_LOCK=requirements-gpu-py311.txt ;;
  3.12) OG_LOCK=requirements-gpu-py312.txt ;;
  *) OG_LOCK= ;;
esac
OG_FALLBACK_REASON=
if [ "$(uname -m)" != x86_64 ]; then OG_LOCK=; OG_FALLBACK_REASON="no torch 2.4.1+cu121 wheels for $(uname -m)"; fi
[ -n "$OG_LOCK" ] || OG_FALLBACK_REASON="${OG_FALLBACK_REASON:-no pinned lock for python $OG_PYV}"
OG_PY=
if [ -n "$OG_LOCK" ]; then
  if python3 -m venv "$OG_DIR/venv" && \
     timeout 1500 "$OG_DIR/venv/bin/python" -m pip install --no-cache-dir --require-hashes --no-deps \
       --only-binary :all: --index-url '@@PYTORCH_INDEX@@' --extra-index-url '@@PYPI_INDEX@@' \
       -r "runner/$OG_LOCK" && \
     "$OG_DIR/venv/bin/python" -c 'import sys, torch, torchvision; sys.exit(0 if (torch.__version__, torchvision.__version__) == ("@@PIN_TORCH@@", "@@PIN_TORCHVISION@@") else 1)'; then
    OG_PY="$OG_DIR/venv/bin/python"
    export OG_ENV_SOURCE=pinned
  else
    OG_FALLBACK_REASON="pinned install failed (see $OG_LOG)"
  fi
fi
if [ -z "$OG_PY" ]; then
  if python3 -c 'import torch, torchvision' >/dev/null 2>&1; then
    OG_PY=python3
    export OG_ENV_SOURCE=preinstalled_fallback
    og_phase env_fallback --kv reason="$OG_FALLBACK_REASON" --kv env_source=preinstalled_fallback
  else
    og_fail bootstrap_failed "$OG_FALLBACK_REASON; no preinstalled PyTorch to fall back to"
  fi
fi
"$OG_PY" -c 'import json, sys, torch, torchvision, numpy; print(json.dumps({"python": sys.version.split()[0], "torch": torch.__version__, "torchvision": torchvision.__version__, "numpy": numpy.__version__, "torch_cuda_build": torch.version.cuda}))' \
  > "$OG_DIR/state/torch.json" 2>&1 || og_fail bootstrap_failed "torch import failed"
og_phase torch_installed --kv env_source="$OG_ENV_SOURCE" --kv-file versions="$OG_DIR/state/torch.json"

OG_STEP=cuda_available
if ! "$OG_PY" -c 'import json, sys, torch; ok = torch.cuda.is_available(); print(json.dumps({"cuda_available": ok, "device_count": torch.cuda.device_count() if ok else 0, "name": torch.cuda.get_device_name(0) if ok else None, "capability": ".".join(map(str, torch.cuda.get_device_capability(0))) if ok else None, "cudnn": torch.backends.cudnn.version() if ok else None})); sys.exit(0 if ok else 1)' \
     > "$OG_DIR/state/cuda.json" 2>&1; then
  og_fail bootstrap_failed "torch.cuda.is_available() is False"
fi
og_phase cuda_available --kv-file cuda="$OG_DIR/state/cuda.json"

OG_STEP=training
OG_REMAIN=$(( OG_DEADLINE - $(date +%s) ))
[ "$OG_REMAIN" -gt 180 ] || og_fail training_timed_out "less than 3 minutes of the runtime ceiling left before training"
set +e
timeout --kill-after=30 "$OG_REMAIN" python3 -m runner.reporter run --base-url "$OG_BASE_URL" --job "$OG_JOB_ID" \
  --token-file "$OG_DIR/token" --state-dir "$OG_DIR/state" --out "$OG_DIR/out" --max-seconds $(( OG_REMAIN - 60 )) \
  -- "$OG_PY" -m runner.mnist_train --out "$OG_DIR/out" --data "$OG_DIR/data" \
     --config-b64 "$OG_CONFIG_B64" --max-seconds $(( OG_REMAIN - 120 ))
OG_RC=$?
set -e
touch "$OG_DIR/completed"
if [ ! -f "$OG_DIR/state/complete.done" ] && [ ! -f "$OG_DIR/state/complete.pending.json" ]; then
  # the reporter did not get to post complete (killed by the outer guard, or crashed)
  if [ "$OG_RC" -eq 124 ] || [ "$OG_RC" -eq 137 ]; then
    og complete --outcome timeout --code training_timed_out --message "outer timeout guard fired" || og_curl_fail training_timed_out || true
  else
    og complete --outcome failed --code training_failed --message "runner exited rc=$OG_RC without reporting" || og_curl_fail training_failed || true
  fi
fi
for _ in 1 2 3 4 5 6 7 8 9 10; do   # deliver anything still spooled (events / an undelivered complete)
  og retry && break
  sleep 30
done
echo "[opengrid $(date -u +%FT%TZ)] done rc=$OG_RC; waiting for OpenGrid to terminate this instance (no OS shutdown: it would not stop billing)"
exit 0
"""
