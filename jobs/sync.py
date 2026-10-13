"""job_sync: the job state from (a) runner callbacks and (b) the deployment state; never from callbacks alone.

    ingest(job_id, events, token_id=, source=)      runner/events: the callback ledger + logs/metrics/evidence/phases
    complete(job_id, body, token_id=, source=)      runner/complete: verified outcome (never success without the
                                                    runner's result + persisted, sha256-verified artifacts)
    on_artifact(job_id, meta)                        after an artifact upload: evidence
    sync_job(job_id, enforce=True)                   deployment state -> job state; enforcement (below)
    run_all()                                        every open job (the `job_sync` background job)

Deployment -> job (the deployment is the authority on the resource; tracker / reconcile move it):
    created/quoted/pending_approval -> pending_approval   quote_expired -> quote_expired   approved -> approved
    rejected -> approval_rejected            provision_failed / provider_rejected -> no_eligible_provider
    provisioning -> provisioning             provider_timeout -> provider_timeout   launch_unknown -> provisioning_uncertain
    running (no runner phase yet) -> initializing   (later phases come from the runner)
    terminating -> terminating               termination_failed / orphan_suspected / credentials_unavailable -> same
    terminated (provider-confirmed) -> termination_confirmed -> reconciliation_pending -> reconciled (cost reconciled)
Enforcement (sync_job(enforce=True); terminate = the EXISTING deployments.terminate, actor system, idempotent):
    runner complete (any outcome), training failure, OOM, artifact failure, cancel  -> terminate
    running with NO runner callback for settings.job_bootstrap_timeout_minutes   -> bootstrap_failed + terminate
    past the deployment deadline while the workload runs                          -> training_timed_out (the
                                                                                     reconciler terminates)
Everything is reconstructed from the database (jobs, job_runner_events, deployments) on every pass, so a worker
restart loses nothing; Postgres row locks (SELECT ... FOR UPDATE on the job) serialise concurrent workers.
"""

from __future__ import annotations

import json
import logging
import math
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

import normalize
from config import settings
from jobs import job as _register_job
from jobs import models as M
from jobs import states as S
from store.jobs import Job, JobLog, JobMetric, JobRunnerEvent

log = logging.getLogger(__name__)

EVENT_TYPES = ("phase", "log", "metric", "gpu", "result", "error")
MAX_EVENTS_PER_BATCH = 500
LOG_LINE_MAX = 4096
MAX_LINES_PER_EVENT = 200
MAX_SEQ = 2 ** 53
# Per-JOB storage quotas: whoever holds the runner token (e.g. a customer with SSH on their own instance) must not
# grow the database without bound (one 1 MB body could otherwise add ~100k log rows). The reporter forwards at most
# ~5k log lines, so a real run stays far below these. phase / result / error events keep being accepted for
# EVENTS_RESERVE more so a job can still finish truthfully once the soft cap is hit.
MAX_EVENTS_PER_JOB = 20_000
EVENTS_RESERVE = 1_000
MAX_LOG_LINES_PER_JOB = 20_000
MAX_METRICS_PER_JOB = 10_000
_ESSENTIAL = ("phase", "result", "error")
METRIC_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,63}\Z")
_METRIC_META = {"epoch", "step", "name", "value", "at"}

# Runner phases that move the job forward (anything else seen from the runner = initializing).
PHASE_STATE = {
    "training_started": "training", "epoch_started": "training", "epoch_completed": "training", "training": "training",
    "saving_artifacts": "saving_artifacts", "uploading_artifacts": "saving_artifacts",
    "artifacts_uploaded": "saving_artifacts", "training_completed": "saving_artifacts",
}
_FORWARD = ("provisioning", "initializing", "training", "saving_artifacts")
# Runner error codes -> (job state, failure_code)
ERROR_STATE = {
    "oom": ("out_of_memory", "out_of_memory"), "out_of_memory": ("out_of_memory", "out_of_memory"),
    "cuda_unavailable": ("bootstrap_failed", "cuda_init_failed"), "cuda_init_failed": ("bootstrap_failed", "cuda_init_failed"),
    "no_gpu": ("bootstrap_failed", "cuda_init_failed"), "nvidia_smi_failed": ("bootstrap_failed", "cuda_init_failed"),
    "torch_import_failed": ("bootstrap_failed", "bootstrap_failed"), "install_failed": ("bootstrap_failed", "bootstrap_failed"),
    "bootstrap_failed": ("bootstrap_failed", "bootstrap_failed"), "pip_install_failed": ("bootstrap_failed", "bootstrap_failed"),
    "timeout": ("training_timed_out", "training_timed_out"), "training_timed_out": ("training_timed_out", "training_timed_out"),
    "training_failed": ("training_failed", "training_failed"),
    "artifact_upload_failed": ("artifact_upload_failed", "artifact_upload_failed"),
}
COMPLETE_OUTCOMES = ("succeeded", "failed", "oom", "timeout")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_at(v) -> datetime | None:
    if not isinstance(v, str) or len(v) > 40:
        return None
    try:
        t = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _bounded(obj, max_chars: int = 16000):
    """A JSON-safe, size-bounded copy of runner-supplied data (never trusted, never huge)."""
    try:
        s = json.dumps(obj, default=str, allow_nan=False)
    except (TypeError, ValueError):
        return {"unserialisable": True}
    if len(s) > max_chars:
        return {"truncated": True, "preview": s[:max_chars]}
    return json.loads(s)


def _actor(source: str) -> str:
    return "runner" if source in ("runner", "simulated") else "system"


def _check_status(source: str, ok: bool) -> str:
    if source == "simulated":
        return "simulated"
    return "pass" if ok else "fail"


# --------------------------------------------------------------------------
# State walking
# --------------------------------------------------------------------------

def _walk(s, job, target: str, *, actor: str, reason: str, evidence: dict | None = None, outcome: str | None = None,
          failure_code: str | None = None, forward_only: bool = False, now=None) -> bool:
    """Move job to `target` along the shortest legal path. forward_only: every intermediate state must be one of
    the forward runner states (a late phase never drags a job through failure states)."""
    if job.status == target:
        return False
    path = S.path_to(job.status, target)
    if not path:
        S.note(s, job, f"ignored: no legal path {job.status} -> {target} ({reason})", evidence, actor=actor)
        return False
    if forward_only and any(p not in _FORWARD for p in path[:-1]):
        return False
    for i, st in enumerate(path):
        last = i == len(path) - 1
        S.transition(s, job, st, actor=actor, reason=reason if last else f"{reason} (via {st})",
                     evidence=evidence, outcome=outcome if last else None,
                     failure_code=failure_code if last else None, now=now)
    return True


def _advance_phase(s, job, target: str, *, actor: str, reason: str, evidence: dict | None) -> bool:
    if job.status not in S.PRE_LAUNCH + _FORWARD + ("provider_timeout", "provisioning_uncertain", "validating",
                                                     "credentials_unavailable"):
        return False   # failed / terminating / uncertain: recorded, never moved backwards
    cur = S.PHASE_ORDER.get(job.status, -1)
    if S.PHASE_ORDER.get(target, -1) <= cur:
        return False   # out of order / late phase
    return _walk(s, job, target, actor=actor, reason=reason, evidence=evidence, forward_only=True)


# --------------------------------------------------------------------------
# Runner callbacks
# --------------------------------------------------------------------------

class CallbackError(ValueError):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def _valid(ev) -> str | None:
    if not isinstance(ev, dict):
        return "event must be an object"
    seq = ev.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0 or seq >= MAX_SEQ:
        return "seq must be a non-negative integer"
    if ev.get("type") not in EVENT_TYPES:
        return f"type must be one of {', '.join(EVENT_TYPES)}"
    if ev.get("data") is not None and not isinstance(ev.get("data"), dict):
        return "data must be an object"
    return None


def ingest(job_id: str, events: list, *, token_id: int | None = None, source: str = "runner") -> dict:
    if not isinstance(events, list):
        raise CallbackError("invalid_events", "events must be a list")
    if len(events) > MAX_EVENTS_PER_BATCH:
        raise CallbackError("too_many_events", f"at most {MAX_EVENTS_PER_BATCH} events per call")
    now = _now()
    accepted, duplicates, rejected, max_seq = 0, 0, [], None
    with normalize.SessionLocal.begin() as s:
        job = s.get(Job, job_id, with_for_update=True)
        if job is None:
            raise CallbackError("job_not_found", "no such job", 404)
        n_events = _count(s, JobRunnerEvent, job_id)
        quota = {"logs": _count(s, JobLog, job_id), "metrics": _count(s, JobMetric, job_id), "dropped": 0}
        for ev in events:
            why = _valid(ev)
            if why:
                rejected.append({"seq": ev.get("seq") if isinstance(ev, dict) else None, "reason": why})
                continue
            if n_events >= MAX_EVENTS_PER_JOB + (EVENTS_RESERVE if ev["type"] in _ESSENTIAL else 0):
                rejected.append({"seq": ev["seq"], "reason": f"per-job runner event quota reached "
                                                            f"({MAX_EVENTS_PER_JOB} events)"})
                continue
            data = _bounded(ev.get("data") or {})
            ins = (insert(JobRunnerEvent).values(job_id=job_id, seq=ev["seq"], token_id=token_id, type=ev["type"],
                                                 at=_parse_at(ev.get("at")), received_at=now, source=source,
                                                 data=data)
                   .on_conflict_do_nothing(index_elements=["job_id", "seq"]).returning(JobRunnerEvent.seq))
            if s.execute(ins).scalar() is None:
                duplicates += 1        # delivered before: ignored (idempotent)
                continue
            accepted += 1
            n_events += 1
            max_seq = ev["seq"] if max_seq is None else max(max_seq, ev["seq"])
            _apply_event(s, job, ev["type"], data, seq=ev["seq"], at=_parse_at(ev.get("at")) or now, source=source,
                         quota=quota)
        if accepted:
            job.last_runner_event_at = now
            job.updated_at = now
        status = job.status
    if token_id is not None:
        from jobs import tokens
        tokens.touch(token_id, max_seq)
    out = {"accepted": accepted, "duplicates": duplicates, "rejected": rejected, "job_status": status}
    if quota["dropped"]:
        out["dropped_over_quota"] = quota["dropped"]
    return out


def _count(s, model, job_id: str) -> int:
    return int(s.scalar(select(func.count()).select_from(model).where(model.job_id == job_id)) or 0)


def _apply_event(s, job, typ: str, data: dict, *, seq: int, at: datetime, source: str,
                 quota: dict | None = None) -> None:
    quota = quota if quota is not None else {"logs": 0, "metrics": 0, "dropped": 0}
    actor = _actor(source)
    if ((job.evidence or {}).get("runtime_initialized") or {}).get("status") in (None, "pending"):
        M.set_check(job, "runtime_initialized", _check_status(source, True),
                    {"first_runner_event": typ, "seq": seq, "at": at.isoformat()}, source)
    if typ == "log":
        stream = data.get("stream") if data.get("stream") in ("stdout", "stderr", "system") else "stdout"
        if source == "simulated":
            stream = "simulated"
        lines = data.get("lines") if isinstance(data.get("lines"), list) else [data.get("line", data.get("message", ""))]
        lines = lines[:MAX_LINES_PER_EVENT]
        room = max(0, MAX_LOG_LINES_PER_JOB - quota["logs"])
        quota["dropped"] += max(0, len(lines) - room)
        lines = lines[:room]
        quota["logs"] += len(lines)
        for ln in lines:
            txt = str(ln if ln is not None else "")
            if len(txt) > LOG_LINE_MAX:
                txt = txt[:LOG_LINE_MAX] + " [truncated]"
            s.add(JobLog(job_id=job.job_id, seq=seq, at=at, stream=stream, line=txt))
    elif typ == "metric":
        rows = _metric_rows(data)
        room = max(0, MAX_METRICS_PER_JOB - quota["metrics"])
        quota["dropped"] += max(0, len(rows) - room)
        quota["metrics"] += min(len(rows), room)
        for name, value in rows[:room]:
            s.add(JobMetric(job_id=job.job_id, seq=seq, epoch=_int(data.get("epoch")), step=_int(data.get("step")),
                            name=name, value=value, at=at, source=source))
        names = {n for n, _ in rows}
        if rows and data.get("epoch") is not None:
            M.set_check(job, "training_ran", _check_status(source, True),
                        {"epoch": _int(data.get("epoch")), "metrics": sorted(names), "seq": seq}, source)
        loss = next((v for n, v in rows if n in ("train_loss", "loss")), None)
        acc = next((v for n, v in rows if n in ("test_accuracy", "accuracy", "val_accuracy")), None)
        if loss is not None and acc is not None:
            dev = str(data.get("batch_device") or "")
            real = source == "runner" and dev.startswith("cuda")   # trained on a GPU, per the batch device
            M.set_check(job, "real_metrics", "simulated" if source == "simulated" else ("pass" if real else "fail"),
                        {"epoch": _int(data.get("epoch")), "train_loss": loss, "test_accuracy": acc,
                         "batch_device": dev or None, "seq": seq}, source)
        _advance_phase(s, job, "training", actor=actor, reason="runner reported a training metric",
                       evidence={"seq": seq})
    elif typ == "phase":
        name = str(data.get("phase") or "")[:32] or "unknown"
        job.runner_phase = name
        _phase_evidence(job, name, data, source)
        target = PHASE_STATE.get(name, "initializing")
        _advance_phase(s, job, target, actor=actor, reason=f"runner phase {name}",
                       evidence={"seq": seq, "phase": name})
    elif typ == "gpu":
        gname = data.get("name") or data.get("gpu_name")
        if gname:
            M.set_check(job, "gpu_detected", _check_status(source, True),
                        {k: data.get(k) for k in ("name", "gpu_name", "driver", "driver_version", "memory_total_mb",
                                                  "uuid_sha256", "nvidia_smi") if data.get(k) is not None}, source)
        if data.get("cuda_available") is not None:
            M.set_check(job, "cuda_available", _check_status(source, bool(data["cuda_available"])),
                        {"cuda_available": bool(data["cuda_available"]), "torch_cuda": data.get("torch_cuda")}, source)
        _advance_phase(s, job, "initializing", actor=actor, reason="runner reported the GPU", evidence={"seq": seq})
    elif typ == "result":
        prev = dict(job.runner_result or {})
        prev["result"] = data
        prev["result_seq"] = seq
        job.runner_result = prev
        l2 = data.get("param_l2_change")
        if isinstance(l2, (int, float)) and not isinstance(l2, bool):
            M.set_check(job, "params_updated", _check_status(source, float(l2) > 0),
                        {"param_l2_change": float(l2), "seq": seq}, source, overwrite=source != "simulated")
        elif isinstance(data.get("params_changed"), bool):
            M.set_check(job, "params_updated", _check_status(source, data["params_changed"]),
                        {"params_changed": data["params_changed"], "seq": seq}, source)
    elif typ == "error":
        code = str(data.get("code") or "training_failed")[:48]
        msg = str(data.get("message") or code)[:2000]
        target, fcode = ERROR_STATE.get(code, ("training_failed", "training_failed"))
        if code == "cuda_unavailable":
            M.set_check(job, "cuda_available", _check_status(source, False),
                        {"error": code, "torch": data.get("torch"), "torch_cuda_build": data.get("torch_cuda_build")},
                        source, overwrite=True)
        if job.status in S.PRE_LAUNCH + _FORWARD + ("provider_timeout", "provisioning_uncertain", "validating"):
            _walk(s, job, target, actor=actor, reason=f"runner error {code}: {msg}",
                  evidence={"seq": seq, "code": code}, outcome="simulated" if job.sandbox else "failed",
                  failure_code=fcode)
        else:
            S.note(s, job, f"runner error {code} while {job.status}: {msg}", {"seq": seq, "code": code}, actor=actor)


def _json_field(v):
    """The bootstrap attaches small JSON files as strings (reporter --kv-file); parse them when they are JSON."""
    if isinstance(v, dict):
        return v
    if isinstance(v, str) and v.strip().startswith("{"):
        try:
            out = json.loads(v)
            return out if isinstance(out, dict) else None
        except ValueError:
            return None
    return None


def _phase_evidence(job, name: str, data: dict, source: str) -> None:
    """Runner phases -> the founder's checks (bootstrap: gpu_detected / torch_installed / cuda_available; trainer:
    cuda_available with the device, training_started, ...)."""
    st = lambda ok: _check_status(source, ok)  # noqa: E731
    if name == "gpu_detected" and data.get("gpus"):
        first = str(data.get("gpus")).strip().splitlines()[0][:300] if str(data.get("gpus")).strip() else ""
        M.set_check(job, "gpu_detected", st(bool(first)), {"nvidia_smi_query": first, "via": "nvidia-smi"}, source)
    versions = _json_field(data.get("versions"))
    if name == "torch_installed" or data.get("torch"):
        ev = {k: v for k, v in (versions or {}).items() if k in ("python", "torch", "torchvision", "numpy",
                                                                   "torch_cuda_build")}
        if data.get("torch"):
            ev["torch"] = data.get("torch")
        if data.get("env_source"):
            ev["env_source"] = data.get("env_source")
        M.set_check(job, "torch_installed", st(bool(ev.get("torch"))), ev or {"phase": name}, source)
    if name in ("cuda_available", "device_selected"):
        cuda = _json_field(data.get("cuda"))
        if cuda is not None:      # bootstrap: torch.cuda.is_available() probe
            ok = bool(cuda.get("cuda_available"))
            M.set_check(job, "cuda_available", st(ok), {k: cuda.get(k) for k in ("cuda_available", "device_count",
                                                                                  "name", "capability", "cudnn")},
                        source, overwrite=not ok)
            if cuda.get("name"):
                M.set_check(job, "gpu_detected", st(True), {"gpu_name": cuda.get("name"),
                                                            "compute_capability": cuda.get("capability"),
                                                            "via": "torch.cuda"}, source)
        else:                     # trainer: the device it actually trains on
            dev = str(data.get("device") or "")
            M.set_check(job, "cuda_available", st(dev.startswith("cuda") or source == "simulated"),
                        {"device": dev or None, "compute_capability": data.get("compute_capability"),
                         "cuda_runtime": data.get("cuda_runtime"), "gpu_name": data.get("gpu_name")}, source,
                        overwrite=not (dev.startswith("cuda") or source == "simulated"))
            if data.get("gpu_name"):
                M.set_check(job, "gpu_detected", st(True), {"gpu_name": data.get("gpu_name"),
                                                            "compute_capability": data.get("compute_capability"),
                                                            "via": "torch (trainer)"}, source)
    if name == "cpu_selected" and not job.sandbox:
        M.set_check(job, "cuda_available", "fail", {"device": data.get("device"), "phase": name}, source,
                    overwrite=True)


def _int(v) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return None


def _metric_rows(data: dict) -> list[tuple[str, float]]:
    """{name, value} or {metrics: {name: value}} or a flat {train_loss: .., test_accuracy: ..}. Finite numbers
    only; names [a-z][a-z0-9_.-]."""
    if "name" in data and "value" in data:
        pairs = [(data["name"], data["value"])]
    elif isinstance(data.get("metrics"), dict):
        pairs = list(data["metrics"].items())
    else:
        pairs = [(k, v) for k, v in data.items() if k not in _METRIC_META]
    out = []
    for n, v in pairs[:64]:
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not isinstance(n, str):
            continue
        if not math.isfinite(float(v)) or not METRIC_NAME.match(n):
            continue
        out.append((n, float(v)))
    return out


def complete(job_id: str, body: dict, *, token_id: int | None = None, source: str = "runner") -> dict:
    """The runner's final word. Never success without the runner's result + persisted, verified artifacts."""
    from jobs import artifacts as A
    from workloads import templates as T

    outcome = (body or {}).get("outcome")
    if outcome not in COMPLETE_OUTCOMES:
        raise CallbackError("invalid_outcome", f"outcome must be one of {', '.join(COMPLETE_OUTCOMES)}")
    summary = _bounded((body or {}).get("summary"), 32000)
    now = _now()
    with normalize.SessionLocal.begin() as s:
        job = s.get(Job, job_id, with_for_update=True)
        if job is None:
            raise CallbackError("job_not_found", "no such job", 404)
        rr = dict(job.runner_result or {})
        if rr.get("complete"):
            return {"replayed": True, "job_status": job.status, "outcome": job.outcome}
        rr["complete"] = {"outcome": outcome, "summary": summary, "at": now.isoformat(), "source": source}
        job.runner_result = rr
        job.last_runner_event_at = now
        actor = _actor(source)
        sim = job.sandbox
        if outcome == "succeeded":
            tmpl = T.get_template(job.template) or {}
            required = tmpl.get("artifacts") or ["model.pt"]
            result = rr.get("result") or (summary.get("result") if isinstance(summary, dict) else None) or {}
            if not isinstance(result, dict):
                result = {}
            problems, code, target = [], None, "training_completed"
            if result.get("outcome") != "succeeded":
                problems.append("the runner sent no successful result event (final metrics) before complete")
                code, target = "result_missing", "training_failed"
            if sim:
                have = {a["name"] for a in A.store().list(job_id)}
                ok, missing = (not problems), []
            else:
                ok_art, missing = A.persisted(job_id, required)
                mism = _manifest_mismatch(job_id, result.get("artifacts"))
                if missing or mism:
                    problems.append(("missing artifacts: " + ", ".join(missing)) if missing else
                                    ("artifact sha256 differs from the runner's manifest: " + ", ".join(mism)))
                    if code is None:
                        code, target = "artifact_upload_failed", "artifact_upload_failed"
                real = (job.evidence or {}).get("real_metrics", {}).get("status")
                if real != "pass" and code is None:
                    problems.append("no real (GPU) loss/accuracy metrics were reported")
                    code, target = "metrics_missing", "training_failed"
                ok = not problems
                have = set(required) - set(missing)
            _artifact_checks(job, have, required, source, sim)
            if ok:
                _walk(s, job, "training_completed", actor=actor,
                      reason="runner completed: result received, artifacts persisted and verified" if not sim
                      else "SIMULATED run completed (sandbox: nothing real happened)",
                      evidence={"outcome": outcome, "artifacts": sorted(have)},
                      outcome="simulated" if sim else "succeeded")
            else:
                _walk(s, job, target, actor="system", reason="; ".join(problems),
                      evidence={"outcome": outcome, "missing": missing}, outcome="simulated" if sim else "failed",
                      failure_code=code)
        else:
            target, fcode = {"failed": ("training_failed", "training_failed"),
                             "oom": ("out_of_memory", "out_of_memory"),
                             "timeout": ("training_timed_out", "training_timed_out")}[outcome]
            why = (summary or {}).get("reason") or (summary or {}).get("code") if isinstance(summary, dict) else None
            if outcome == "failed" and isinstance(why, str) and why in ERROR_STATE:
                target, fcode = ERROR_STATE[why]      # e.g. cuda_unavailable -> bootstrap_failed / cuda_init_failed
            if job.status in S.WORKLOAD_FAILED:     # an error event already put it there (e.g. cuda_init_failed)
                if job.outcome is None:
                    job.outcome = "simulated" if sim else "failed"
                S.note(s, job, f"runner complete: {outcome}", {"summary": summary}, actor=actor)
            else:
                _walk(s, job, target, actor=actor, reason=f"runner completed with outcome {outcome}",
                      evidence={"outcome": outcome}, outcome="simulated" if sim else "failed", failure_code=fcode)
        status, out_outcome = job.status, job.outcome
    if not sim:
        sync_job(job_id, enforce=True)
    return {"replayed": False, "job_status": status, "outcome": out_outcome}


def _manifest_mismatch(job_id: str, manifest) -> list[str]:
    if not isinstance(manifest, dict):
        return []
    from jobs import artifacts as A
    have = {a["name"]: a for a in A.store().list(job_id)}
    out = []
    for name, m in manifest.items():
        if isinstance(m, dict) and name in have and m.get("sha256") and m["sha256"] != have[name]["sha256"]:
            out.append(name)
    return out


def _artifact_checks(job, have: set, required, source: str, sim: bool) -> None:
    if sim:
        M.set_check(job, "weights_saved", "simulated", {"note": "sandbox: no model was trained"}, "simulated")
        M.set_check(job, "artifacts_persisted", "simulated", {"artifacts": sorted(have)}, "simulated")
        return
    M.set_check(job, "weights_saved", "pass" if "model.pt" in have else "fail",
                {"model.pt": "model.pt" in have}, "opengrid", overwrite=True)
    missing = [n for n in required if n not in have]
    M.set_check(job, "artifacts_persisted", "pass" if not missing else "fail",
                {"required": list(required), "missing": missing}, "opengrid", overwrite=True)


def on_artifact(job_id: str, meta: dict) -> None:
    with normalize.SessionLocal.begin() as s:
        job = s.get(Job, job_id, with_for_update=True)
        if job is None:
            return
        src = meta.get("source") or "runner"
        if meta["name"] == "model.pt":
            M.set_check(job, "weights_saved", "simulated" if src == "simulated" else "pass",
                        {"name": "model.pt", "size": meta["size"], "sha256": meta["sha256"]},
                        "simulated" if src == "simulated" else "opengrid")
        S.note(s, job, f"artifact {meta['name']} stored ({meta['size']} bytes, sha256 verified)",
               {"name": meta["name"], "size": meta["size"], "sha256": meta["sha256"], "backend": meta["backend"]},
               actor=_actor(src))
        job.updated_at = _now()


# --------------------------------------------------------------------------
# Deployment state -> job state
# --------------------------------------------------------------------------

def _dep_for(s, job):
    from store.routing import Deployment
    if job.deployment_id:
        return s.get(Deployment, job.deployment_id)
    if job.route_request_id:
        d = s.scalars(select(Deployment).where(Deployment.route_request_id == job.route_request_id)
                      .order_by(Deployment.created_at.desc())).first()
        if d is not None:
            job.deployment_id = d.deployment_id
            job.quote_id = job.quote_id or d.quote_id
        return d
    return None


def _dep_ev(dep) -> dict:
    return {"deployment_id": dep.deployment_id, "deployment_status": dep.status,
            "provider_instance_id": dep.provider_instance_id}


def _terminated_evidence(s, dep) -> dict | None:
    from store.routing import DeploymentEvent
    e = s.scalars(select(DeploymentEvent).where(DeploymentEvent.deployment_id == dep.deployment_id,
                                                DeploymentEvent.to_status == "terminated")
                  .order_by(DeploymentEvent.at.desc())).first()
    if e is None:
        return None
    return {"at": e.at.isoformat(), "actor": e.actor, "reason": e.reason, "evidence": e.evidence}


def _completed(job) -> bool:
    """The runner posted complete (any outcome). A result event alone is not completion."""
    return bool((job.runner_result or {}).get("complete"))


def _past_deadline(dep, now) -> bool:
    return dep.terminate_deadline_at is not None and now >= dep.terminate_deadline_at


def _follow(s, job, dep, now) -> None:
    D, J = dep.status, job.status
    ev = _dep_ev(dep)
    go = lambda to, why, **kw: _walk(s, job, to, actor="system", reason=why, evidence=ev, now=now, **kw)  # noqa: E731
    cancelled = job.cancel_requested_at is not None
    if D in ("created", "quoted", "pending_approval"):
        if J in ("created", "validating", "quoted", "approved", "quote_expired"):
            go("pending_approval", "deployment pending admin approval")
    elif D == "quote_expired":
        if J in ("quoted", "pending_approval", "approved"):
            go("quote_expired", "the quote expired before approval: an admin re-approves a fresh quote")
    elif D == "approved":
        if J in ("quoted", "pending_approval", "quote_expired"):
            go("approved", f"approved by {dep.approved_by or 'admin'}")
    elif D == "rejected":
        if not S.is_finished(job):
            go("approval_rejected", "cancelled before launch" if cancelled else
               f"the launch was rejected: {dep.termination_reason or 'admin rejected'}",
               outcome="cancelled" if cancelled else "failed", failure_code=None if cancelled else "approval_rejected")
    elif D in ("provision_failed", "provider_rejected"):
        if not S.is_finished(job):
            code = "gpu_unavailable" if D == "provision_failed" else "provider_rejected"
            go("no_eligible_provider", f"{D}: {dep.failure_reason or 'the provider created nothing'}",
               outcome="cancelled" if cancelled else "failed", failure_code=code)
    elif D == "provisioning":
        if J in S.PRE_LAUNCH + ("provider_timeout", "provisioning_uncertain"):
            go("provisioning", "provision call made; waiting for the provider to report running")
    elif D == "provider_timeout":
        if J in S.PRE_LAUNCH + ("provisioning",):
            go("provider_timeout", "the provision call timed out: the instance may exist; reconciliation resolves "
                                   "it by name before any retry")
    elif D == "launch_unknown":
        if J in S.PRE_LAUNCH + ("provisioning", "provider_timeout"):
            go("provisioning_uncertain", "ambiguous provision outcome: the instance may exist (never re-launched)")
    elif D in ("running", "degraded", "stopping", "stopped"):
        if J in S.PRE_LAUNCH + ("provisioning", "provider_timeout", "provisioning_uncertain"):
            go("initializing", "the provider reports the instance running; waiting for the runner")
        elif J == "credentials_unavailable":
            back = PHASE_STATE.get(job.runner_phase or "", "initializing") if job.runner_phase else "initializing"
            go(back, "the pinned credentials work again")
        if J in S.RUNNING and _past_deadline(dep, now) and not _completed(job):
            go("training_timed_out", "past the runtime ceiling (deployment deadline) before the runner completed; "
                                     "the reconciler terminates the instance", outcome="failed",
               failure_code="training_timed_out")
    elif D == "terminating":
        if J in S.RUNNING + ("provider_timeout", "provisioning_uncertain") and not _completed(job):
            if _past_deadline(dep, now):
                go("training_timed_out", "runtime ceiling reached: the instance is being terminated",
                   outcome="failed", failure_code="training_timed_out")
            elif not cancelled:
                S.note(s, job, "the deployment is terminating while the workload had not completed", ev)
        if job.status not in ("terminating",) + tuple(x for x in S.FINISHED):
            go("terminating", "termination requested; awaiting provider confirmation",
               outcome=("cancelled" if cancelled else "failed") if not _completed(job) else None,
               failure_code=None if cancelled or _completed(job) else "terminated_before_completion")
    elif D in ("termination_failed", "orphan_suspected", "credentials_unavailable"):
        if not S.is_finished(job) and job.status != D:
            if S.can(job.status, D) or S.path_to(job.status, D):
                go(D, {"termination_failed": "termination NOT confirmed: the provider refused the delete; the instance "
                                             "may still be billing (retrying, operator alerted)",
                       "orphan_suspected": "reconciliation suspects an orphan instance (possibly billing)",
                       "credentials_unavailable": "the credential pinned at launch is unusable (state kept, operator "
                                                  "alerted)"}[D])
    elif D == "terminated":
        if job.status not in ("termination_confirmed", "reconciliation_pending", "reconciled"):
            tev = _terminated_evidence(s, dep) or {}
            if job.status in S.RUNNING and not _completed(job) and _past_deadline(dep, now):
                go("training_timed_out", "runtime ceiling reached", outcome="failed", failure_code="training_timed_out")
            no_result = job.outcome is None
            go("termination_confirmed", "the provider confirmed the instance is gone",
               outcome=("cancelled" if cancelled else "failed") if no_result else None,
               failure_code=None if (cancelled or not no_result) else "terminated_before_completion")
            if tev:
                S.note(s, job, "termination evidence", {**ev, "terminated": tev})
        if job.status in ("termination_confirmed", "reconciliation_pending"):
            if dep.reconciled_at is not None:
                go("reconciled", "cost reconciled (routing/transactions.py)",
                   outcome=("cancelled" if cancelled else "failed") if job.outcome is None else None)
            elif job.status == "termination_confirmed":
                go("reconciliation_pending", "waiting for cost reconciliation")


def _deployment_checks(s, job, dep) -> None:
    if dep is None:
        return
    if dep.provider_instance_id:
        M.set_check(job, "instance_created", "pass",
                    {"provider": dep.provider, "instance_id": dep.provider_instance_id,
                     "deployment_id": dep.deployment_id, "client_name": dep.client_name}, "provider")
    elif dep.status in ("provision_failed", "provider_rejected"):
        M.set_check(job, "instance_created", "fail", {"deployment_status": dep.status,
                                                      "reason": dep.failure_reason}, "provider")
    lt = (dep.provider_metadata or {}).get("last_terminate") or {}
    if dep.status == "terminated" or (dep.terminate_requested_at and lt.get("outcome") in ("accepted", "already_gone")):
        M.set_check(job, "gpu_terminated", "pass",
                    {"terminate_requested_at": dep.terminate_requested_at.isoformat() if dep.terminate_requested_at
                     else None, "provider_answer": lt.get("outcome"), "deployment_status": dep.status}, "provider")
    elif dep.status == "termination_failed":
        M.set_check(job, "gpu_terminated", "fail", {"deployment_status": dep.status,
                                                    "provider_answer": lt.get("outcome")}, "provider", overwrite=True)
    if dep.status == "terminated":
        M.set_check(job, "termination_verified", "pass",
                    {"terminated_at": dep.terminated_at.isoformat() if dep.terminated_at else None,
                     "provider_event": _terminated_evidence(s, dep)}, "provider")


def _enforce(s, job, dep, now) -> str | None:
    from routing import deployments as D

    if dep is None or dep.status not in D.LIVE_STATES or dep.terminate_requested_at is not None:
        return None
    if job.status == "training_timed_out" and _past_deadline(dep, now):
        return None   # the deadline itself: the reconciler's auto-terminate owns it (termination_reason deadline)
    if job.status in ("training_completed", "cancel_requested", "cancelling") + S.WORKLOAD_FAILED:
        return f"job {job.status}"
    if job.status == "initializing" and job.last_runner_event_at is None and dep.status in ("running", "degraded"):
        ref = dep.provider_running_at or job.state_changed_at or dep.provisioned_at
        limit = timedelta(minutes=int(settings.job_bootstrap_timeout_minutes))
        if ref is not None and now - ref >= limit:
            _walk(s, job, "bootstrap_failed", actor="system",
                  reason=f"no runner callback {settings.job_bootstrap_timeout_minutes} min after the instance was "
                         "running: bootstrap failed (check the image / network); terminating",
                  evidence={**_dep_ev(dep), "running_since": ref.isoformat()}, outcome="failed",
                  failure_code="bootstrap_timeout", now=now)
            return "bootstrap timeout"
    return None


def _terminate(job_id: str, dep_id: str, reason: str) -> dict:
    from fastapi import HTTPException

    from routing import deployments as D

    with normalize.SessionLocal.begin() as s:
        job = s.get(Job, job_id, with_for_update=True)
        job.terminate_requested_at = job.terminate_requested_at or _now()
        S.note(s, job, f"terminating the GPU: {reason}", {"deployment_id": dep_id})
    try:
        out = D.terminate(dep_id, None, reason=f"job {job_id}: {reason}")
        return {"ok": True, "terminate": out.get("terminate")}
    except HTTPException as exc:
        log.error("job %s: terminate %s refused: %s", job_id, dep_id, exc.detail)
        return {"ok": False, "error": exc.detail}
    except Exception:  # noqa: BLE001 - retried by the next job_sync pass / the reconciler
        log.exception("job %s: terminate %s failed", job_id, dep_id)
        return {"ok": False, "error": "terminate failed; retried by job_sync and reconciliation"}


def sync_job(job_id: str, *, enforce: bool = True, now: datetime | None = None, _depth: int = 0) -> dict:
    from jobs import tokens

    now = now or _now()
    term, dep_id = None, None
    with normalize.SessionLocal.begin() as s:
        job = s.get(Job, job_id, with_for_update=True)
        if job is None:
            return {"synced": False, "reason": "no such job"}
        if job.sandbox or S.is_finished(job):
            if S.is_finished(job):
                tokens.revoke_all(s, job_id, f"job {job.status}")
            return {"synced": False, "status": job.status, "reason": "finished" if S.is_finished(job) else "sandbox"}
        dep = _dep_for(s, job)
        if dep is not None:
            _follow(s, job, dep, now)
            _deployment_checks(s, job, dep)
            if enforce:
                term = _enforce(s, job, dep, now)
            dep_id = dep.deployment_id
        if S.is_finished(job) or job.status in ("termination_confirmed", "reconciliation_pending"):
            tokens.revoke_all(s, job_id, f"job {job.status}")
        status = job.status
    out = {"synced": True, "status": status}
    if term and dep_id:
        out["terminate"] = _terminate(job_id, dep_id, term)
        if _depth < 2:
            out["status"] = sync_job(job_id, enforce=False, _depth=_depth + 1).get("status", status)
    return out


def open_job_ids(limit: int = 1000) -> list[str]:
    with normalize.SessionLocal() as s:
        return list(s.scalars(select(Job.job_id).where(Job.finished_at.is_(None), Job.sandbox.is_(False))
                              .order_by(Job.created_at).limit(limit)))


def run_all() -> dict:
    ids = open_job_ids()
    n, failed = 0, 0
    for jid in ids:
        try:
            sync_job(jid)
            n += 1
        except Exception:  # noqa: BLE001 - one bad job never stalls the others
            failed += 1
            log.exception("job_sync %s failed", jid)
    return {"open": len(ids), "synced": n, "errors": failed}


@_register_job("job_sync", every_seconds=settings.job_sync_interval_seconds, initial_delay_seconds=45)
def _job_sync():
    return run_all()
