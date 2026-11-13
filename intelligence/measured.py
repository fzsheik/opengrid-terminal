"""Measured performance: completed REAL OpenGrid jobs -> performance_observations -> recommendations.

    signature(template=, model=, method=, precision=) -> str        the workload signature observations are keyed by
    request_signature(req, profile) -> str | None                   the same signature for a recommendation request
    observation_from_job(job, metrics, *, consent, now) -> (row | None, reason)   PURE: is this job a measurement?
    record_job(job_id, *, now=None) -> {"recorded", "reason", "id"?}             write one (idempotent)
    record_safely(job_id) -> dict | None                            the hook jobs/sync.complete calls; never raises
    backfill(limit=500) -> {"scanned", "recorded", "skipped": {reason: n}}   record every eligible past job
    lookup(gpus, sig, *, now) -> {gpu: {gpu_count: summary}}        consented, fresh measurements for a signature
    request_work(req, profile) -> {"units", "unit", "basis", "assumptions"} | None
    runtime(summary, work) -> {"hours", "range_hours", ...} | None  measured throughput x the request's work
    provider_startup() -> {provider: {"p50_s", "n"}}                time to capacity from OpenGrid's own launches

What counts as a measurement (all must hold; anything else is skipped with a reason, never guessed):
    - the job is NOT sandbox (a simulated job has outcome 'simulated' and never counts), its outcome is 'succeeded'
      and its real_metrics evidence check passed (the runner reported real GPU metrics);
    - every metric row is from the runner (source 'runner'): one 'simulated' row and the job is skipped;
    - the selection names the provider and canonical GPU it ran on; when the runner reported the GPU name
      (nvidia-smi), the canonical GPU's model token must appear in it (e.g. "A100" in "NVIDIA A100-SXM4-80GB");
    - at least one epoch has both its wall-clock (epoch_seconds) and its work (train_samples, or
      samples_per_second x epoch_seconds; tokens likewise).
Throughput is total work / total seconds over the timed epochs, dropping the first epoch when there are two or more
(warm-up: cuDNN autotuning, data-loader start). Setup overhead = initializing -> training_completed wall-clock minus
the timed training seconds (bootstrap, dataset download, evaluation, artifact upload; NOT provisioning). Data kind "measured"; source_kind "completed_job"; one row per job (unique index).

Consent: a run by OpenGrid itself (the operator login, or an account flagged is_operator) is consented. A customer's run is recorded with consent=false
unless the account opted in (accounts.settings["share_performance_measurements"] is true); only consented rows are
ever read back into recommendations. Nothing here ever exposes an account, a job id or a provider's internal price.
"""

from __future__ import annotations

import logging
import re
import statistics
from datetime import datetime, timedelta, timezone

log = logging.getLogger("opengrid.intelligence.measured")

MEASURED = "measured"
SOURCE_KIND = "completed_job"
MAX_AGE_DAYS = 180          # older measurements describe older drivers / images: not used
HIGH_N = 3                  # observations for "high" confidence (with FRESH_DAYS)
FRESH_DAYS = 30
CONSENT_SETTING = "share_performance_measurements"
_UNIT_BY_WORK = {"samples": "samples_per_second", "tokens": "tokens_per_second"}


# ------------------------------------------------------------------ signatures

def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())[:120]


def signature(*, template: str | None = None, model: str | None = None, method: str | None = None,
              precision: str | None = None) -> str | None:
    """'template:pytorch-mnist' for an allowlisted template (the template fixes model, data and precision);
    'model:<catalog id or name>|<method>|<precision>' otherwise. None without a template or a model name."""
    if template:
        return f"template:{_norm(template)}"
    if not model:
        return None
    return f"model:{_norm(model)}|{method or '?'}|{precision or '?'}"


def request_signature(req, profile: dict) -> str | None:
    from workloads import templates as T

    name = (req.model.name if req.model is not None else None) or ""
    if name and T.get_template(name) is not None:
        return signature(template=name)
    cat = (profile or {}).get("catalog_id")
    model = cat or name
    return signature(model=model, method=(profile or {}).get("method"), precision=(profile or {}).get("precision"))


def _gpu_token(gpu: str) -> str | None:
    """The model token of a canonical GPU name: 'NVIDIA A100 80GB SXM4' -> 'A100', 'NVIDIA RTX 4090 24GB' -> '4090',
    'AMD Instinct MI300X 192GB' -> 'MI300X'. None when no token carries a digit."""
    for w in re.split(r"[^A-Za-z0-9]+", gpu or ""):
        if re.search(r"\d", w) and not re.fullmatch(r"\d+GB", w, re.I) and w.upper() not in ("SXM4", "SXM5", "SXM"):
            return w.upper()
    return None


def gpu_matches(canonical: str, detected: str | None) -> bool | None:
    """None when nothing was detected; else whether the canonical model token is a whole word of the detected name."""
    if not detected:
        return None
    tok = _gpu_token(canonical)
    if tok is None:
        return None
    words = {w.upper() for w in re.split(r"[^A-Za-z0-9]+", detected) if w}
    return tok in words


# ------------------------------------------------------------------ job -> observation (pure)

def _detected_gpu(evidence: dict | None) -> str | None:
    c = ((evidence or {}).get("gpu_detected") or {})
    if c.get("status") not in ("pass",):
        return None
    ev = c.get("evidence") or {}
    for k in ("gpu_name", "name"):
        if isinstance(ev.get(k), str):
            return ev[k][:128]
    q = ev.get("nvidia_smi_query")
    if isinstance(q, dict):
        v = q.get("name") or q.get("gpu_name")
        return v[:128] if isinstance(v, str) else None
    if isinstance(q, str):
        return q.split(",")[0].strip()[:128] or None
    return None


def _epochs(metrics: list[dict]) -> list[dict]:
    by: dict[int, dict] = {}
    for m in metrics:
        if m.get("epoch") is None:
            continue
        by.setdefault(int(m["epoch"]), {})[m["name"]] = float(m["value"])
    out = []
    for ep in sorted(by):
        r = by[ep]
        secs = r.get("epoch_seconds")
        if not secs or secs <= 0:
            continue
        tokens = r.get("train_tokens") or ((r.get("tokens_per_second") or 0) * secs) or None
        samples = r.get("train_samples") or ((r.get("samples_per_second") or 0) * secs) or None
        out.append({"epoch": ep, "seconds": secs, "tokens": tokens, "samples": samples})
    return out


def observation_from_job(job, metrics: list[dict], *, consent: bool, now: datetime | None = None,
                         completed_at: datetime | None = None) -> tuple[dict | None, str]:
    """(row, "ok") when `job` is a measurement (module docstring), else (None, reason). `job` needs the store/jobs.Job
    attributes; `metrics` are {epoch, name, value, source} dicts; completed_at is when the job reached
    training_completed (its finished_at is only set after termination and reconciliation). Pure."""
    now = now or datetime.now(timezone.utc)
    if getattr(job, "sandbox", False):
        return None, "sandbox"
    if getattr(job, "outcome", None) != "succeeded":
        return None, "not_succeeded"
    ev = getattr(job, "evidence", None) or {}
    if (ev.get("real_metrics") or {}).get("status") != "pass":
        return None, "no_real_metrics"
    if not metrics:
        return None, "no_metrics"
    if any((m.get("source") or "runner") != "runner" for m in metrics):
        return None, "simulated_metrics"
    sel = getattr(job, "selection", None) or {}
    gpu, provider = sel.get("gpu"), sel.get("provider")
    if not gpu or not provider:
        return None, "no_selection"
    detected = _detected_gpu(ev)
    if gpu_matches(gpu, detected) is False:
        return None, "gpu_mismatch"
    eps = _epochs(metrics)
    if not eps:
        return None, "no_timed_epochs"
    unit_key = "tokens" if all(e["tokens"] for e in eps) else "samples" if all(e["samples"] for e in eps) else None
    if unit_key is None:
        return None, "no_work_measured"
    timed = eps[1:] if len(eps) >= 2 else eps
    work = sum(e[unit_key] for e in timed)
    secs = sum(e["seconds"] for e in timed)
    if work <= 0 or secs <= 0:
        return None, "no_work_measured"
    all_secs = sum(e["seconds"] for e in eps)
    # started_at = the job entered initializing (the instance was up): overhead is bootstrap + data download +
    # artifact upload, not provisioning (that is the provider's time to capacity, reported separately)
    started, finished = getattr(job, "started_at", None), completed_at or getattr(job, "finished_at", None)
    overhead = None
    if started and finished and finished > started:
        overhead = max(0.0, (finished - started).total_seconds() - all_secs)
    spec = getattr(job, "spec", None) or {}
    count = int(((spec.get("resources") or {}).get("gpu_count")) or 1)
    cfg = getattr(job, "config", None) or {}
    mb = cfg.get("batch_size") if isinstance(cfg.get("batch_size"), int) else None
    template = getattr(job, "template", None)
    row = {
        "gpu": gpu, "gpu_count": count, "provider": provider,
        "workload_class": "training_full" if getattr(job, "job_type", "training") == "training" else str(job.job_type),
        "model_name": template, "method": "full", "precision": None,
        "workload_signature": signature(template=template) if template else None,
        "micro_batch_size": mb,
        "throughput": round(work / secs, 4), "throughput_unit": _UNIT_BY_WORK[unit_key],
        "work_measured": round(work, 2), "train_seconds": round(secs, 3),
        "epochs_measured": len(timed), "overhead_s": None if overhead is None else round(overhead, 1),
        "runtime_s": None if not (started and finished) else round((finished - started).total_seconds(), 1),
        "gpu_detected": detected, "outcome": "succeeded", "source_kind": SOURCE_KIND, "data_kind": MEASURED,
        "consent": bool(consent), "account_id": getattr(job, "account_id", None), "job_id": job.job_id,
        "observed_at": finished or now, "created_at": now,
        "notes": ("throughput over epochs " + ", ".join(str(e["epoch"]) for e in timed)
                  + (" (first epoch dropped as warm-up)" if len(eps) >= 2 else " (single epoch: includes warm-up)")
                  + ("; GPU name not reported by the runner" if detected is None else "")),
    }
    if row["workload_signature"] is None:
        return None, "no_signature"
    return row, "ok"


# ------------------------------------------------------------------ database

def _consent(s, job) -> bool:
    if getattr(job, "principal_kind", None) == "operator":
        return True
    if job.account_id is None:
        return False
    try:
        from store.accounts import Account
        a = s.get(Account, job.account_id)
        if a is None:
            return False
        # OpenGrid's own operator account is consented; a customer only by opting in
        return bool(getattr(a, "is_operator", False)) or (a.settings or {}).get(CONSENT_SETTING) is True
    except Exception:  # noqa: BLE001
        return False


def record_job(job_id: str, *, now: datetime | None = None) -> dict:
    """Record one job as a measured observation when it qualifies. Idempotent: a job already recorded is skipped."""
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    import normalize
    from store.intelligence import PerformanceObservation as PO
    from store.jobs import Job, JobEvent, JobMetric

    now = now or datetime.now(timezone.utc)
    with normalize.SessionLocal() as s:
        job = s.get(Job, job_id)
        if job is None:
            return {"recorded": False, "reason": "job_not_found"}
        if s.scalar(select(PO.id).where(PO.job_id == job_id, PO.source_kind == SOURCE_KIND)) is not None:
            return {"recorded": False, "reason": "already_recorded"}
        metrics = [{"epoch": m.epoch, "name": m.name, "value": m.value, "source": m.source}
                   for m in s.scalars(select(JobMetric).where(JobMetric.job_id == job_id))]
        done_at = s.scalar(select(JobEvent.at).where(JobEvent.job_id == job_id,
                                                     JobEvent.to_status == "training_completed")
                           .order_by(JobEvent.at).limit(1))
        row, why = observation_from_job(job, metrics, consent=_consent(s, job), now=now, completed_at=done_at)
    if row is None:
        return {"recorded": False, "reason": why}
    try:
        with normalize.SessionLocal.begin() as s:
            ob = PO(**row)
            s.add(ob)
            s.flush()
            oid = ob.id
    except IntegrityError:
        return {"recorded": False, "reason": "already_recorded"}
    return {"recorded": True, "reason": "ok", "id": oid}


def record_safely(job_id: str) -> dict | None:
    """The completion hook: a measurement must never break a job's completion. Logs and returns None on error."""
    try:
        return record_job(job_id)
    except Exception as e:  # noqa: BLE001
        log.warning("measured.record_job(%s) failed: %s", job_id, e)
        return None


def backfill(limit: int = 500) -> dict:
    """Record every past succeeded, non-sandbox job not yet recorded (newest first, at most `limit`)."""
    from sqlalchemy import select

    import normalize
    from store.intelligence import PerformanceObservation as PO
    from store.jobs import Job

    with normalize.SessionLocal() as s:
        done = select(PO.job_id).where(PO.source_kind == SOURCE_KIND, PO.job_id.is_not(None))
        ids = list(s.scalars(select(Job.job_id).where(Job.sandbox.is_(False), Job.outcome == "succeeded",
                                                      Job.job_id.not_in(done))
                             .order_by(Job.finished_at.desc().nulls_last()).limit(max(1, int(limit)))))
    out = {"scanned": len(ids), "recorded": 0, "skipped": {}}
    for jid in ids:
        r = record_safely(jid) or {"recorded": False, "reason": "error"}
        if r["recorded"]:
            out["recorded"] += 1
        else:
            out["skipped"][r["reason"]] = out["skipped"].get(r["reason"], 0) + 1
    return out


def lookup(gpus: list[str], sig: str | None, *, now: datetime | None = None) -> dict:
    """{gpu: {gpu_count: summary}} from consented measured observations of `sig` no older than MAX_AGE_DAYS.
    Empty when there are none, the signature is None, or the table / columns are missing (old schema)."""
    if not gpus or not sig:
        return {}
    now = now or datetime.now(timezone.utc)
    try:
        from sqlalchemy import select

        import normalize
        from store.intelligence import MEASURED_KINDS, PerformanceObservation as PO
        with normalize.SessionLocal() as s:
            rows = list(s.execute(
                select(PO.gpu, PO.gpu_count, PO.provider, PO.throughput, PO.throughput_unit, PO.overhead_s,
                       PO.micro_batch_size, PO.observed_at, PO.created_at)
                .where(PO.gpu.in_(gpus), PO.workload_signature == sig, PO.source_kind.in_(MEASURED_KINDS),
                       PO.consent.is_(True), PO.throughput.is_not(None), PO.throughput > 0,
                       PO.created_at >= now - timedelta(days=MAX_AGE_DAYS))))
    except Exception:  # noqa: BLE001 - no table / columns yet: same as no measurement
        return {}
    groups: dict[tuple, list] = {}
    for r in rows:
        groups.setdefault((r.gpu, int(r.gpu_count or 1), r.throughput_unit), []).append(r)
    out: dict[str, dict] = {}
    for (gpu, n, unit), rs in groups.items():
        cur = out.setdefault(gpu, {}).get(n)
        if cur is not None and cur["n"] >= len(rs):
            continue        # one unit per (gpu, count): the better-sampled one
        tp = [float(r.throughput) for r in rs]
        oh = [float(r.overhead_s) for r in rs if r.overhead_s is not None]
        newest = max((r.observed_at or r.created_at) for r in rs)
        age_days = max(0.0, (now - newest).total_seconds() / 86400)
        out[gpu][n] = {"kind": MEASURED, "n": len(rs), "unit": unit,
                       "throughput_median": round(statistics.median(tp), 4),
                       "throughput_range": [round(min(tp), 4), round(max(tp), 4)],
                       "overhead_s_median": round(statistics.median(oh), 1) if oh else None,
                       "providers": sorted({r.provider for r in rs if r.provider}),
                       "micro_batch_sizes": sorted({r.micro_batch_size for r in rs if r.micro_batch_size}),
                       "newest_observed_at": newest.isoformat(), "age_days": round(age_days, 1),
                       "signature": sig,
                       "basis": f"{len(rs)} completed real OpenGrid job(s) on this GPU and count; training-loop "
                                "throughput from per-epoch runner timings (first epoch dropped as warm-up when "
                                "there were two or more)"}
    return out


def confidence(summary: dict | None) -> tuple[str, str]:
    if not summary:
        return "low", "no measurement"
    n, age = summary["n"], summary["age_days"]
    if n >= HIGH_N and age <= FRESH_DAYS:
        return "high", f"measured on {n} real runs, newest {age:.0f} days old"
    return "medium", (f"measured on {n} real run{'s' if n != 1 else ''}" + (f", newest {age:.0f} days old"
                                                                           if age > 1 else ""))


def request_work(req, profile: dict) -> dict | None:
    """The request's work in measurement units: tokens (performance.work) or samples (dataset_samples x epochs,
    or for an allowlisted template its own dataset and default epochs, stated). None when unknown."""
    from intelligence import performance as P
    from workloads import templates as T

    A: list[str] = []
    wk = P.work(req, profile) if (profile.get("method") or "").startswith("training") else {}
    tr = req.training
    epochs = tr.epochs if tr is not None and tr.epochs else None
    name = (req.model.name if req.model is not None else None) or ""
    tmpl = T.get_template(name) if name else None
    samples = None
    if tr is not None and tr.dataset_samples:
        samples = float(tr.dataset_samples)
        basis = f"{samples:,.0f} samples"
    elif tmpl is not None and (tmpl.get("dataset") or {}).get("train_samples"):
        samples = float(tmpl["dataset"]["train_samples"])
        basis = f"{samples:,.0f} training samples ({tmpl['dataset'].get('name')}, from the template)"
        A.append("dataset size taken from the template")
    if samples is not None:
        if epochs is None:
            epochs = (T.config_defaults(tmpl).get("epochs") if tmpl else None) or 1
            A.append(f"{epochs:g} epoch(s) assumed (epochs not stated)")
        out = {"samples": samples * epochs, "basis": f"{basis} x {epochs:g} epoch(s)", "assumptions": A}
        if wk.get("tokens"):
            out["tokens"] = wk["tokens"]
        return out
    if wk.get("tokens"):
        return {"tokens": wk["tokens"], "basis": wk.get("tokens_basis") or "tokens trained",
                "assumptions": list(wk.get("assumptions") or [])}
    return None


def runtime(summary: dict | None, work: dict | None) -> dict | None:
    """Measured runtime for the request's work: work / median throughput (+ median setup overhead); the range uses
    the fastest and slowest measured throughput. None when the units do not match."""
    if not summary or not work:
        return None
    key = "tokens" if summary["unit"] == "tokens_per_second" else "samples"
    units = work.get(key)
    if not units:
        return None
    oh = summary.get("overhead_s_median") or 0.0
    lo_tp, hi_tp = summary["throughput_range"]
    h = (units / summary["throughput_median"] + oh) / 3600
    fast = (units / hi_tp + oh) / 3600
    slow = (units / lo_tp + oh) / 3600
    return {"known": True, "kind": MEASURED, "hours": round(h, 4), "range_hours": [round(fast, 4), round(slow, 4)],
            "includes_setup": bool(summary.get("overhead_s_median")),
            "basis": (f"{units:,.0f} {key} ({work['basis']}) / {summary['throughput_median']:g} "
                      f"{summary['unit'].replace('_', ' ')} measured on {summary['n']} real OpenGrid run(s)"
                      + (f" + {oh:.0f} s measured setup overhead (median)" if oh else "")
                      + "; range from the fastest and slowest run"),
            "assumptions": list(work.get("assumptions") or [])}


def provider_startup() -> dict:
    """{provider: {"p50_s", "p95_s", "n"}}: approval -> first 'running' from OpenGrid's own launches, only where the
    reliability minimum sample is met. Empty on any error (no data is the normal case today)."""
    return dict(_startup_cached())


def _startup_impl() -> dict:
    try:
        from routing import reliability
        provs = reliability.compute(reliability.load())
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for p, m in provs.items():
        t = m.get("time_to_capacity_seconds") or {}
        v = t.get("value") if isinstance(t, dict) else None
        if isinstance(v, dict) and v.get("p50") is not None:
            out[p] = {"p50_s": v["p50"], "p95_s": v.get("p95"), "n": t.get("n")}
    return out


try:
    from cache import ttl_cache
    _startup_cached = ttl_cache(300)(_startup_impl)
except Exception:  # noqa: BLE001
    _startup_cached = _startup_impl


if __name__ == "__main__":     # .venv/bin/python -m intelligence.measured backfill [limit]
    import json
    import sys
    if len(sys.argv) >= 2 and sys.argv[1] == "backfill":
        print(json.dumps(backfill(int(sys.argv[2]) if len(sys.argv) > 2 else 500), indent=2))
    else:
        print("usage: python -m intelligence.measured backfill [limit]")
