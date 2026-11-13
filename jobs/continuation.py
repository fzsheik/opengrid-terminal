"""Continuing a job on another GPU: Resume (user), OOM escalation (automatic, opt-in), Move (user), the read-only move
suggestion, and runner instructions (methodology/jobs.md "Resume", "OOM escalation", "Move", "Provider-initiated
stops"; execution-safety.md "Resume, OOM escalation and move").

Every successor is an ORDINARY job submitted through jobs.orchestrator.submit (validate / select / quote /
engine.route / approval / cost guards / one provision call per deployment / no failover on an ambiguous outcome) with
chain={retry_of, root_job_id, attempt, kind}: the new row carries retry_of under UNIQUE(retry_of), so ONE successor per
attempt, whoever asks (an automatic relaunch, a resume, an escalation or a move). Nothing here calls a provider except
the move's terminate, which is the EXISTING jobs.sync._terminate -> routing.deployments.terminate.

THE CORE RULE (jobs/relaunch.provider_confirmed_gone): a successor is submitted only once the previous machine is
provider-confirmed gone (job termination_confirmed / reconciliation_pending / reconciled AND its deployment
`terminated` with terminated_at). Never while it may still exist (degraded, stopped, termination_failed, orphan_suspected,
credentials_unavailable, provider_timeout, launch_unknown, ...).

Resume  POST /v1/jobs/{id}/resume {provider?: same|best|<id>, max_total_cost_usd, from_scratch?}  (owner, route:execute,
        Idempotency-Key). The job ended without finishing on purpose or by interruption: cancelled, gpu_interrupted,
        runner_lost, terminated_before_completion (never training_failed / out_of_memory / ... : those fail again; OOM
        has its own escalation). Refused while the machine may still exist, when it already has a successor, during a
        move, or with no verified checkpoint in the chain unless from_scratch=true. The successor resumes from the
        chain's latest checkpoint that re-verifies (sha256) -- relaunch.pick_resume. provider same = only the previous
        attempt's provider; best = the best-ranked eligible one (the previous provider included); <id> = only that one.
        limits.max_total_cost_usd = the caller's max_total_cost_usd (the successor's own ceiling; the max-price cap and
        the account cost guards bind it as for any job).
OOM escalation (consider_oom; in the job_relaunch pass, so OFF unless settings.job_auto_relaunch_enabled): a REAL job
        that failed with failure_code out_of_memory whose spec.retry.on_oom == "escalate", once its machine is
        provider-confirmed gone. ONE successor per chain (retry.max_oom_escalations, at most 2) on the NEXT GPU tier with
        more VRAM than the GPU that ran out (hardware.py vram_gb; the smallest larger tier first, the same family / then
        the same vendor preferred inside a tier; MIG slices and laptop parts never), from the latest verified checkpoint,
        within retry.max_total_usd (spend so far + the next attempt's est_max_total) and every guard. The decision is a
        job_relaunches row (kind oom_escalation): the same exactly-once claim as a relaunch. Never for another failure.
Move    POST /v1/jobs/{id}/move {provider?: best|<id>, max_total_cost_usd, reason}  (owner, route:execute,
        Idempotency-Key; refused unless settings.job_move_enabled). A RUNNING job only. The target is selected FIRST (no
        eligible provider within the ceiling -> refused; nothing touched). Then (the job_move pass, every
        settings.job_move_interval_seconds):
          checkpointing  a runner instruction checkpoint_now is queued on the existing runner channel (the response of
                         runner/events and GET runner/instructions); the runner saves + uploads a checkpoint at the end
                         of the current epoch. The pass waits for a checkpoint of THIS job stored AFTER the request that
                         re-verifies (sha256). None by settings.job_move_checkpoint_timeout_minutes -> ABORTED, the job
                         keeps running untouched (never killed without a verified checkpoint). A batch shard reserves
                         the successor's ceiling in the batch ledger here, BEFORE anything is terminated.
          terminating    the job -> terminating (outcome cancelled, failure_code moved), the EXISTING terminate path;
                         wait for provider-confirmed termination (never a successor before)
          submitting     the successor through orchestrator.submit, resume_from = that checkpoint, chain kind move,
                         inside routing.idempotency.run(scope jobs:move, key move:<move_id>) + UNIQUE(retry_of)
          moved | aborted | failed   (failed: the submission was refused after the old machine was gone; the
                         checkpoint stays and Resume works)
        At most one open move per job (partial unique index). Every step is a job event (evidence.relaunch_event).
Move suggestion (move_suggestion(job, dep); GET /v1/jobs/{id}): READ-ONLY, never acts. A running real job gets a
        suggestion object when an eligible offer elsewhere has an est. remaining cost (incl. the move overhead
        settings.job_move_overhead_minutes) at least settings.job_move_suggest_min_savings_pct lower, or its current
        provider is degraded (deployment degraded / provider killed) and an eligible offer exists.
Batch shards: every successor of a shard (resume, escalation, move) reserves its ceiling in the batch ledger under
        the batch lock (jobs/batches.reserve) BEFORE the submission, attached to the successor in the job-creating
        transaction, released if the submission is refused; refused when the batch is not active.
"""

from __future__ import annotations

import logging
import math
import secrets
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

import normalize
from config import settings
from jobs import job as _register_job
from jobs import states as S
from store.jobs import Job, JobMove, JobRelaunch, JobRunnerInstruction

log = logging.getLogger(__name__)

KINDS = ("relaunch", "resume", "oom_escalation", "move")
RESUMABLE_CODES = ("gpu_interrupted", "runner_lost", "terminated_before_completion")
MOVE_OPEN = ("requested", "checkpointing", "terminating", "submitting")
MOVE_FINAL = ("moved", "aborted", "failed")
NOTHING_CREATED = ("rejected", "provision_failed", "provider_rejected", "quote_failed")
INSTRUCTION_KINDS = ("checkpoint_now",)
MAX_TIER_TRIES = 8
SUGGEST_TTL = 60.0
_SUGGEST_CACHE: dict = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _f(v):
    return None if v is None else float(v)


def _iso(t):
    return None if t is None else t.isoformat()


class Refused(Exception):
    """A refusal the API returns as HTTP `status` with {code, message, ...}."""

    def __init__(self, code: str, message: str, status: int = 409, **extra):
        super().__init__(message)
        self.code, self.message, self.status, self.extra = code, message, status, extra

    def detail(self) -> dict:
        return {"code": self.code, "message": self.message, **self.extra}


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------

def _job(job_id: str):
    with normalize.SessionLocal() as s:
        return s.get(Job, job_id)


def _dep(job):
    if job is None or not job.deployment_id:
        return None
    from store.routing import Deployment
    with normalize.SessionLocal() as s:
        return s.get(Deployment, job.deployment_id)


def _owned(job_id: str, who):
    """The job, only for its owner (the same account; the operator for its own jobs). Anyone else: 404 (no oracle)."""
    job = _job(job_id)
    if job is None or job.account_id != who.account_id:
        raise Refused("job_not_found", "job not found", 404)
    return job


def successor_of(job_id: str):
    with normalize.SessionLocal() as s:
        return s.scalars(select(Job).where(Job.retry_of == job_id)).first()


def open_move(job_id: str):
    with normalize.SessionLocal() as s:
        return s.scalars(select(JobMove).where(JobMove.job_id == job_id, JobMove.status.in_(MOVE_OPEN))).first()


def machine_gone(job, dep) -> bool:
    """The previous machine cannot be billing: THE CORE RULE, or nothing was ever created (a job that ended before
    any instance existed)."""
    from jobs import relaunch as R
    if R.provider_confirmed_gone(job, dep):
        return True
    if not S.is_finished(job):
        return False
    return dep is None or (dep.status in NOTHING_CREATED and not dep.provider_instance_id)


def _provider_of(job, dep) -> str | None:
    return (dep.provider if dep is not None else None) or (job.selection or {}).get("provider")


def _epochs(job) -> int:
    return int((job.config or {}).get("epochs") or (job.spec or {}).get("config", {}).get("epochs") or 1)


def _chain(job):
    from jobs import relaunch as R
    with normalize.SessionLocal() as s:
        return R.chain_jobs(s, R.root_of(job))


def _ckpt_view(resume: dict | None) -> dict | None:
    """The pinned checkpoint + its age, for previews / events."""
    if not resume:
        return None
    from jobs import artifacts as A
    row = next((c for c in A.checkpoints(resume["job_id"]) if c["name"] == resume["artifact"]), None)
    at = row.get("created_at") if row else None
    age = None
    if at:
        try:
            age = max(0, int((_now() - datetime.fromisoformat(at)).total_seconds()))
        except ValueError:
            age = None
    return {"job_id": resume["job_id"], "artifact": resume["artifact"], "epoch": resume.get("epoch"),
            "sha256": resume.get("sha256"), "created_at": at, "age_seconds": age, "verified": True}


def successor_body(job, *, provider: str, prev_provider: str | None, resume: dict | None, max_total: float,
                   ssh_public_key: str | None) -> dict:
    """The successor's request: THIS attempt's validated request (template, config, resources, strategy, price cap,
    runtime, retry policy, name) with only the provider choice, resume_from and the caller's total cost ceiling."""
    from workloads.spec import WorkloadSpec
    spec = dict(job.spec or {})
    body = {k: spec[k] for k in WorkloadSpec.model_fields if k in spec}
    r = dict(spec.get("routing") or {})
    ex = set(r.get("exclude_providers") or ())
    r.pop("exclude_providers", None)
    include = None
    if provider == "same":
        if not prev_provider:
            raise Refused("provider_unknown", "the previous attempt has no provider to resume on; use provider best",
                          422)
        include = [prev_provider]
    elif provider not in ("best", None):
        include = [provider]
    if include:            # an explicit choice replaces the request's own include list (best keeps it)
        ex -= set(include)
        r["include_providers"] = include
    if ex:
        r["exclude_providers"] = sorted(ex)
    body["routing"] = r
    body["limits"] = {**spec["limits"], "max_total_cost_usd": round(float(max_total), 2)}
    body.pop("resume_from", None)
    if resume:
        body["resume_from"] = {"job_id": resume["job_id"], "artifact": resume["artifact"]}
    body["ssh_public_key"] = ssh_public_key
    body["sandbox"] = False
    return body


def _provider_arg(v, *, allow_same: bool) -> str:
    from workloads.spec import PROVIDER_RE
    p = str(v or "best").strip().lower()
    if p == "same" and allow_same:
        return p
    if p == "best" or PROVIDER_RE.match(p):
        if p == "same":
            raise Refused("invalid_provider", "provider must be best or a provider id", 422)
        return p
    raise Refused("invalid_provider", "provider must be " + ("same, best" if allow_same else "best")
                  + " or a provider id", 422)


def _max_total_arg(v) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)) or float(v) <= 0:
        raise Refused("invalid_max_total_cost_usd", "max_total_cost_usd is required: a positive number (the "
                                                    "successor's total cost ceiling)", 422)
    return round(float(v), 2)


def _select(body, who) -> dict:
    from workloads import select as W
    from workloads.spec import WorkloadError
    try:
        return W.select(body, who)
    except WorkloadError as exc:
        raise Refused(exc.code, exc.message, exc.status, **({"field": exc.field} if exc.field else {})) from None


def _quote_view(sel: dict, body: dict) -> dict:
    ready = sel.get("execution_ready") or {}
    cost = ready.get("cost") or {}
    return {"provider": ready.get("provider"), "gpu": ready.get("gpu"), "gpu_count": (body.get("resources") or {})
            .get("gpu_count"), "listing_id": ready.get("listing_id"), "region": ready.get("region"),
            "price_per_gpu_hour": ready.get("price_per_gpu_hour"), "price_kind": ready.get("price_kind"),
            "est_max_total_usd": cost.get("est_max_total"), "est_typical_total_usd": cost.get("est_typical_total"),
            "max_total_cost_usd": body["limits"]["max_total_cost_usd"],
            "approval_mode": ((ready.get("readiness") or {}).get("approval_mode")),
            "kind": "estimated", "note": "the submission quotes this listing and re-validates it live; an admin "
                                        "approves it (SUPERVISED) before anything launches or bills"}


def _refusal(sel: dict) -> Refused:
    from jobs import orchestrator as O
    r = O.refusal_for(sel)
    return Refused(r.code, r.message, 422, **{k: v for k, v in r.extra.items() if k in ("blockers", "market_best",
                                                                                         "exclusions_by_code")})


# --------------------------------------------------------------------------
# Batch shards: the batch ledger is the outer ceiling (jobs/batches.py)
# --------------------------------------------------------------------------

def _batch_terms(job):
    """None for an ordinary job; the shard's batch terms otherwise. Refused when the batch is not active."""
    from jobs import batches as B
    from jobs.relaunch import Stop
    try:
        return B.relaunch_terms(job)
    except Stop as e:
        raise Refused(e.code, e.message, 409) from None


def _batch_shape(bt: dict, body: dict, *, remaining: float, explicit: list | None = None) -> None:
    """The batch's provider lists + per-shard limit apply; an explicit provider must be one the batch allows."""
    from jobs import batches as B
    want = list(explicit or [])
    cap = min(float(bt["per_shard_limit_usd"]), float(remaining))
    if float(body["limits"]["max_total_cost_usd"]) > cap + 1e-9:
        raise Refused("batch_budget_exhausted", f"batch {bt['batch_id']}: max_total_cost_usd "
                                                f"${body['limits']['max_total_cost_usd']:.2f} is over what the batch "
                                                f"allows for this shard now (${cap:.2f})", 422,
                      batch_remaining_usd=remaining)
    mine = float(body["limits"]["max_total_cost_usd"])
    B.shape_relaunch_body(bt, body, remaining=remaining)
    body["limits"]["max_total_cost_usd"] = round(min(mine, float(body["limits"]["max_total_cost_usd"])), 2)
    if want:
        allowed = bt.get("include_providers")
        bad = [p for p in want if p in (bt.get("exclude_providers") or []) or (allowed and p not in allowed)]
        if bad:
            raise Refused("provider_not_allowed_by_batch", f"the batch does not allow {', '.join(bad)}", 422)
        body["routing"]["include_providers"] = want


def _batch_reserve(bt: dict, job_id: str, *, kind: str, est: float, body: dict, ref: str | None = None) -> dict:
    """Reserve the successor's ceiling in the batch ledger (under the batch lock) BEFORE submitting it. `ref` names
    the reservation (a crashed pass reuses an unused one, never a second)."""
    from jobs import batches as B
    limit = float(body["limits"]["max_total_cost_usd"])
    amount = round(min(limit, B._ceil_cents(est * (1 + settings.quote_price_tolerance))), 2)
    try:
        r = B.reserve(bt["batch_id"], bt["shard_index"], amount=amount, kind=kind,
                      ref=f"s{bt['shard_index']}:{kind}:{ref or job_id}"[:80], prev_job_id=job_id)
    except B.BatchRefused as e:
        raise Refused(e.code, f"batch {bt['batch_id']}: {e.message}", 409 if e.status < 400 else e.status) from None
    body["limits"]["max_total_cost_usd"] = round(min(limit, float(r["reserved_usd"])), 2)
    with normalize.SessionLocal.begin() as s:
        B._event(s, bt["batch_id"], f"{kind}_reserved", f"${r['reserved_usd']:.2f} reserved for the {kind} of shard "
                 f"{bt['shard_index']} after {job_id}", shard_index=bt["shard_index"],
                 evidence={"job_id": job_id, "reserved_usd": r["reserved_usd"], "kind": kind})
    return {"attempt_id": r["attempt_id"], "reserved_usd": r["reserved_usd"], "batch_id": bt["batch_id"],
            "shard_index": bt["shard_index"], "replayed": r.get("replayed")}


def _release(batch_ref: dict | None) -> None:
    if batch_ref:
        from jobs import batches as B
        B.release(batch_ref["attempt_id"])


def _chain_info(job, *, kind: str, reason: str, actor: str, actor_id: str | None) -> dict:
    from jobs import relaunch as R
    n = R.attempt_of(job)
    p = R.policy(job) or {"max_attempts": 1}
    return {"retry_of": job.job_id, "root_job_id": R.root_of(job), "attempt": n + 1,
            "max_attempts": max(int(p["max_attempts"]), n + 1), "kind": kind, "reason": reason, "actor": actor,
            "actor_id": actor_id}


def _submit(body, who, chain: dict, batch_ref: dict | None) -> dict:
    """orchestrator.submit (the one submit path) -> the successor summary. Raises Refused; releases the batch
    reservation of a refusal that left no job."""
    from jobs import orchestrator as O
    try:
        code, out = O.submit(body, who, chain=chain, batch=batch_ref)
    except O.JobRefused as r:
        if not r.extra.get("job_id"):
            _release(batch_ref)
        if r.code == "relaunch_exists":
            succ = successor_of(chain["retry_of"])
            raise Refused("already_continued", f"job {chain['retry_of']} already has a successor"
                                               + (f" ({succ.job_id})" if succ else ""), 409,
                          successor_job_id=succ.job_id if succ else None) from None
        raise Refused(r.code, r.message, r.status, **r.extra) from None
    return {"http_code": code, "job": out, "job_id": out["job_id"], "status": out.get("status"),
            "provider": (out.get("selection") or {}).get("provider")}


def _note(job_id: str, text: str, evidence: dict, *, actor: str = "system", actor_id: str | None = None) -> None:
    with normalize.SessionLocal.begin() as s:
        j = s.get(Job, job_id, with_for_update=True)
        if j is not None:
            S.note(s, j, text[:4000], evidence, actor=actor, actor_id=actor_id)


def _who_text(who) -> tuple[str, str]:
    from jobs import orchestrator as O
    return O._actor(who)


# --------------------------------------------------------------------------
# Resume
# --------------------------------------------------------------------------

def resume_check(job, dep) -> tuple[bool, str | None, str | None]:
    """(available, code, reason) for the job page and the API. Never a provider call."""
    if job.sandbox:
        return False, "resume_not_real", "a sandbox job is a simulation: run it again instead"
    succ = successor_of(job.job_id)
    if succ is not None:
        return False, "already_continued", f"this job already continues as {succ.job_id}"
    if open_move(job.job_id) is not None:
        return False, "move_in_progress", "a move of this job is in progress"
    cancelled = job.outcome == "cancelled" and job.failure_code != "moved"
    if not cancelled and job.failure_code not in RESUMABLE_CODES:
        if job.outcome in ("succeeded",):
            return False, "not_resumable", "the job finished successfully"
        if job.failure_code == "out_of_memory":
            return False, "not_resumable", "out of GPU memory: it would fail again on the same GPU (retry.on_oom " \
                                           "escalate relaunches it on a larger one)"
        if job.outcome is None:
            return False, "not_resumable", "the job has not ended: cancel it (or move it) first"
        return False, "not_resumable", f"a {job.failure_code or job.outcome} job would fail again; start a new job"
    if not machine_gone(job, dep):
        return False, "machine_may_exist", "the previous GPU is not provider-confirmed gone yet: resume is possible " \
                                           "once the provider confirms the termination"
    return True, None, None


def _resume_plan(job, who, body: dict) -> dict:
    from jobs import relaunch as R
    dep = _dep(job)
    ok, code, why = resume_check(job, dep)
    if not ok:
        extra = {}
        if code == "already_continued":
            extra["successor_job_id"] = successor_of(job.job_id).job_id
        raise Refused(code, why, 409, **extra)
    provider = _provider_arg(body.get("provider"), allow_same=True)
    max_total = _max_total_arg(body.get("max_total_cost_usd"))
    from_scratch = body.get("from_scratch") is True
    unknown = set(body) - {"provider", "max_total_cost_usd", "from_scratch"}
    if unknown:
        raise Refused("unknown_field", f"unknown field(s): {', '.join(sorted(unknown))}", 422)
    chain = _chain(job)
    resume, notes = R.pick_resume(job, chain, _epochs(job))
    if resume is None and not from_scratch:
        raise Refused("no_verified_checkpoint", "no checkpoint of this job's chain re-verifies (sha256): resume "
                                                "from scratch only with from_scratch: true", 409, notes=notes)
    if from_scratch:
        resume = None
    prev = _provider_of(job, dep)
    sb = successor_body(job, provider=provider, prev_provider=prev, resume=resume, max_total=max_total,
                        ssh_public_key=R._customer_key(job, dep))
    bt = _batch_terms(job)
    if bt is not None:
        if bt["remaining_usd"] <= 0:
            raise Refused("batch_budget_exhausted", f"batch {bt['batch_id']} budget exhausted", 409)
        _batch_shape(bt, sb, remaining=bt["remaining_usd"], explicit=None if provider == "best" else
                     ([prev] if provider == "same" else [provider]))
    sel = _select(sb, who)
    return {"job": job, "dep": dep, "provider": provider, "prev_provider": prev, "resume": resume,
            "resume_notes": notes, "body": sb, "sel": sel, "batch": bt, "from_scratch": resume is None}


def resume_preview(job_id: str, who, body: dict) -> dict:
    job = _owned(job_id, who)
    p = _resume_plan(job, who, body)
    out = {"job_id": job_id, "action": "resume", "provider_choice": p["provider"],
           "previous_provider": p["prev_provider"], "from_scratch": p["from_scratch"],
           "checkpoint": _ckpt_view(p["resume"]), "resume_notes": p["resume_notes"],
           "batch": {"batch_id": p["batch"]["batch_id"], "remaining_usd": p["batch"]["remaining_usd"]}
           if p["batch"] else None}
    if p["sel"].get("execution_ready"):
        out.update(can_submit=True, quote=_quote_view(p["sel"], p["body"]), refusal=None)
    else:
        out.update(can_submit=False, quote=None, refusal=_refusal(p["sel"]).detail())
    return out


def resume(job_id: str, who, body: dict) -> tuple[int, dict]:
    """Submit the successor (owner only). Raises Refused."""
    job = _owned(job_id, who)
    p = _resume_plan(job, who, body)
    sel = p["sel"]
    if not sel.get("execution_ready"):
        raise _refusal(sel)
    est = float(((sel["execution_ready"].get("cost") or {}).get("est_max_total")) or 0)
    batch_ref = _batch_reserve(p["batch"], job.job_id, kind="resume", est=est, body=p["body"],
                               ref=f"{job.job_id}:{secrets.token_hex(3)}") if p["batch"] else None
    actor, actor_id = _who_text(who)
    frm = (f"resuming from epoch {p['resume']['epoch']} of {p['resume']['job_id']}" if p["resume"] else
           "starting from scratch (from_scratch: true)")
    cause = job.failure_code or job.outcome
    chain = _chain_info(job, kind="resume", actor=actor, actor_id=actor_id,
                        reason=f"resumed by its owner after {cause} on {p['prev_provider'] or '?'}; {frm}")
    out = _submit(p["body"], who, chain, batch_ref)
    _note(job.job_id, f"resumed as {out['job_id']} on {out['provider']} " + (
        f"from epoch {p['resume']['epoch']}" if p["resume"] else "from scratch"),
          {"relaunch_event": "resumed", "successor_job_id": out["job_id"], "provider": out["provider"],
           "provider_choice": p["provider"], "resume_from": p["resume"], "max_total_cost_usd":
           p["body"]["limits"]["max_total_cost_usd"], "batch": batch_ref}, actor=actor, actor_id=actor_id)
    res = dict(out["job"])
    res["resume"] = {"resumed_job_id": job.job_id, "successor_job_id": out["job_id"], "checkpoint": _ckpt_view(
        p["resume"]), "from_scratch": p["from_scratch"], "provider_choice": p["provider"]}
    return out["http_code"], res


# --------------------------------------------------------------------------
# OOM escalation (automatic; the job_relaunch pass)
# --------------------------------------------------------------------------

def oom_policy(job) -> dict | None:
    r = (job.spec or {}).get("retry") if isinstance(job.spec, dict) else None
    if not isinstance(r, dict) or r.get("on_oom") != "escalate":
        return None
    from workloads.spec import MAX_OOM_ESCALATIONS
    try:
        n = int(r.get("max_oom_escalations") or 1)
    except (TypeError, ValueError):
        n = 1
    cap = r.get("max_total_usd")
    return {"max_oom_escalations": max(1, min(MAX_OOM_ESCALATIONS, n)),
            "max_total_usd": None if cap is None else float(cap)}


def oom_trigger(job, dep) -> tuple[bool, str]:
    from jobs import relaunch as R
    if job.sandbox:
        return False, "sandbox"
    if job.failure_code != "out_of_memory":
        return False, f"failure {job.failure_code or 'none'} is not out_of_memory"
    if oom_policy(job) is None:
        return False, "retry.on_oom is not escalate"
    if job.cancel_requested_at is not None or job.outcome == "cancelled":
        return False, "cancelled"
    if not R.provider_confirmed_gone(job, dep):
        return False, "the previous GPU is not provider-confirmed terminated yet"
    return True, ""


def oom_candidate_ids(limit: int = 200) -> list[str]:
    decided = select(JobRelaunch.failed_job_id).where(JobRelaunch.failed_job_id == Job.job_id,
                                                      JobRelaunch.status.in_(("relaunched", "stopped"))).exists()
    with normalize.SessionLocal() as s:
        return list(s.scalars(select(Job.job_id).where(
            Job.sandbox.is_(False), Job.failure_code == "out_of_memory",
            Job.status.in_(("termination_confirmed", "reconciliation_pending", "reconciled")),
            Job.cancel_requested_at.is_(None), Job.spec["retry"]["on_oom"].astext == "escalate", ~decided)
            .order_by(Job.updated_at).limit(limit)))


def larger_tiers(gpu: str | None) -> list[dict]:
    """The GPUs with more VRAM than `gpu` (hardware.py), next tier first: (vram, same family, same vendor, name).
    Only GPUs that have a listing in the market now; never MIG slices or laptop parts."""
    import families
    import hardware
    cur = hardware.spec(gpu or "") if gpu else None
    if not cur or not cur.get("vram_gb"):
        return []
    v0 = float(cur["vram_gb"])
    fam0 = set(families.families_for_gpu(gpu) or [])
    from tables import ComputeListingRow
    with normalize.SessionLocal() as s:
        listed = set(s.scalars(select(ComputeListingRow.canonical_gpu_name).where(
            ComputeListingRow.price_per_gpu_hour.isnot(None)).distinct()))
    out = []
    for name, sp in hardware.all_specs().items():
        v = sp.get("vram_gb")
        if not v or float(v) <= v0 or name not in listed:
            continue
        if sp.get("form_factor") in ("MIG slice", "Laptop") or sp.get("segment") == "laptop":
            continue
        same_fam = bool(fam0 & set(families.families_for_gpu(name) or []))
        same_vendor = sp.get("vendor") == cur.get("vendor")
        out.append({"gpu": name, "vram_gb": float(v), "same_family": same_fam, "same_vendor": same_vendor,
                    "rank": (float(v), 0 if same_fam else 1 if same_vendor else 2, name)})
    return sorted(out, key=lambda x: x["rank"])


def _oom_claim(job, now) -> bool:
    from jobs import relaunch as R
    stmt = (insert(JobRelaunch).values(failed_job_id=job.job_id, root_job_id=R.root_of(job), attempt=R.attempt_of(job),
                                       status="claimed", tries=1, kind="oom_escalation", created_at=now,
                                       updated_at=now)
            .on_conflict_do_nothing(index_elements=["failed_job_id"]).returning(JobRelaunch.failed_job_id))
    with normalize.SessionLocal.begin() as s:
        if s.execute(stmt).scalar() is not None:
            return True
    with normalize.SessionLocal() as s:
        row = s.get(JobRelaunch, job.job_id)
    if row is None or row.status in ("relaunched", "stopped"):
        return False
    if row.status == "claimed" and now - row.updated_at < R.CLAIM_STALE:
        return False
    with normalize.SessionLocal.begin() as s:
        got = s.execute(update(JobRelaunch).where(JobRelaunch.failed_job_id == job.job_id,
                                                  JobRelaunch.status == row.status,
                                                  JobRelaunch.updated_at == row.updated_at)
                        .values(status="claimed", tries=JobRelaunch.tries + 1, updated_at=now)
                        .returning(JobRelaunch.failed_job_id)).scalar()
    return bool(got)


def consider_oom(job_id: str, *, now: datetime | None = None) -> dict:
    """Decide (at most once) whether OOM-failed attempt `job_id` is escalated to a larger GPU. Never raises."""
    from jobs import relaunch as R
    now = now or _now()
    if not settings.job_auto_relaunch_enabled:
        return {"job_id": job_id, "decision": "skipped", "reason": "job_auto_relaunch_enabled is off"}
    job = _job(job_id)
    if job is None:
        return {"job_id": job_id, "decision": "skipped", "reason": "no such job"}
    ok, why = oom_trigger(job, _dep(job))
    if not ok:
        return {"job_id": job_id, "decision": "skipped", "reason": why}
    if not _oom_claim(job, now):
        return {"job_id": job_id, "decision": "skipped", "reason": "decided or in progress"}
    ctx: dict = {"kind": "oom_escalation"}
    try:
        return _escalate(job, ctx)
    except R.Stop as exc:
        return _oom_stop(job, exc, ctx)
    except Exception as exc:  # noqa: BLE001 - internal error: blocked, retried next pass
        log.exception("job %s: OOM escalation blocked by an internal error", job_id)
        err = f"{type(exc).__name__}: {exc}"[:500]
        try:      # a batch reservation stays (unused): the next pass reuses it by its ref, never a second one
            R._decide(job_id, "blocked", code="internal_error", reason=err, detail=S._jsonable(ctx))
        except Exception:  # noqa: BLE001
            log.exception("job %s: could not record the blocked escalation", job_id)
        return {"job_id": job_id, "decision": "blocked", "reason": err}


def _oom_stop(job, exc, ctx: dict) -> dict:
    from jobs import relaunch as R
    _release(ctx.get("batch_ref"))
    R._decide(job.job_id, "stopped", code=exc.code, reason=exc.message, detail=S._jsonable({**ctx, **exc.detail}),
              spend_so_far_usd=R._dec(ctx.get("spend_so_far_usd")), max_total_usd=R._dec(ctx.get("max_total_usd")),
              est_next_usd=R._dec(ctx.get("est_next_usd")))
    _note(job.job_id, f"not escalated after out_of_memory: {exc.message}",
          {"relaunch_event": "oom_not_escalated", "code": exc.code, **{k: ctx.get(k) for k in (
              "from_gpu", "from_vram_gb", "tiers_tried", "spend_so_far_usd", "max_total_usd") if ctx.get(k) is not None}})
    return {"job_id": job.job_id, "decision": "stopped", "code": exc.code, "reason": exc.message}


def _escalate(job, ctx: dict) -> dict:
    import hardware

    from jobs import relaunch as R

    pol = oom_policy(job)
    ctx.update(max_total_usd=pol["max_total_usd"], max_oom_escalations=pol["max_oom_escalations"])
    succ = successor_of(job.job_id)
    if succ is not None:
        R._decide(job.job_id, "relaunched", code="oom_escalated", reason=f"successor {succ.job_id} already exists",
                  successor_job_id=succ.job_id)
        return {"job_id": job.job_id, "decision": "relaunched", "successor_job_id": succ.job_id, "replayed": True}
    with normalize.SessionLocal() as s:
        chain = R.chain_jobs(s, R.root_of(job))
        spend = R.chain_spend(s, chain)
    done = sum(1 for j in chain if j.succession_kind == "oom_escalation")
    ctx.update(escalations_so_far=done, spend_so_far_usd=spend["usd"])
    if done >= pol["max_oom_escalations"]:
        raise R.Stop("oom_escalations_exhausted", f"{done} escalation(s) already made in this chain "
                                                  f"(retry.max_oom_escalations {pol['max_oom_escalations']})")
    if pol["max_total_usd"] is None:
        raise R.Stop("retry_budget_exhausted", "the retry policy has no max_total_usd")
    who = R.owner(job)
    remaining = math.floor((pol["max_total_usd"] - spend["usd"]) * 100) / 100
    budget_code = "retry_budget_exhausted"
    bt = None
    try:
        bt = _batch_terms(job)
    except Refused as e:
        raise R.Stop(e.code, e.message) from None
    if bt is not None:
        if bt["remaining_usd"] < remaining:
            budget_code = "batch_budget_exhausted"
        remaining = min(remaining, bt["remaining_usd"])
        ctx.update(batch_id=bt["batch_id"], batch_remaining_usd=bt["remaining_usd"])
    if remaining <= 0:
        raise R.Stop(budget_code, f"budget exhausted: ${spend['usd']:.2f} spent of ${pol['max_total_usd']:.2f}")
    dep = _dep(job)
    cur_gpu = (job.selection or {}).get("gpu") or (dep.gpu if dep is not None else None)
    cur = hardware.spec(cur_gpu or "") or {}
    ctx.update(from_gpu=cur_gpu, from_vram_gb=cur.get("vram_gb"))
    tiers = larger_tiers(cur_gpu)
    if not tiers:
        raise R.Stop("no_larger_gpu", f"no listed GPU has more VRAM than {cur_gpu or 'the GPU that ran out'} "
                                      f"({cur.get('vram_gb') or '?'} GB)")
    resume, rnotes = R.pick_resume(job, chain, _epochs(job))
    ctx.update(resume_from=resume, resume_notes=rnotes)
    root = next((j for j in chain if j.job_id == R.root_of(job)), job)
    tried, chosen = [], None
    for t in tiers[:MAX_TIER_TRIES]:
        body = successor_body(job, provider="best", prev_provider=None, resume=resume, max_total=remaining,
                              ssh_public_key=R._customer_key(job, dep))
        body["limits"]["max_total_cost_usd"] = round(min(remaining, float(
            ((root.spec or {}).get("limits") or {}).get("max_total_cost_usd") or remaining)), 2)
        body["resources"] = {**(body.get("resources") or {}), "gpu": t["gpu"], "min_vram_gb": None}
        if bt is not None:
            from jobs import batches as B
            B.shape_relaunch_body(bt, body, remaining=remaining)
        try:
            sel = _select(body, who)
        except Refused as e:      # the workload cannot run on that GPU at all (e.g. gpu_incompatible): skipped
            tried.append({"gpu": t["gpu"], "vram_gb": t["vram_gb"], "result": "incompatible", "refusal": e.code})
            continue
        ready = sel.get("execution_ready")
        if not ready:
            by = sel.get("exclusions_by_code") or {}
            rc = _refusal(sel).code
            tried.append({"gpu": t["gpu"], "vram_gb": t["vram_gb"], "refusal": rc,
                          "result": "over_budget" if rc == "budget_exceeded" or (
                              by.get("over_budget") and not sel.get("market_best")) else "not_executable"})
            continue
        est = float((ready.get("cost") or {}).get("est_max_total") or 0)
        if spend["usd"] + est > pol["max_total_usd"] + 1e-9:
            tried.append({"gpu": t["gpu"], "vram_gb": t["vram_gb"], "result": "over_budget", "est_max_total": est})
            continue
        chosen = (t, body, ready, est)
        tried.append({"gpu": t["gpu"], "vram_gb": t["vram_gb"], "result": "chosen", "est_max_total": est})
        break
    ctx["tiers_tried"] = tried
    if chosen is None:
        usable = [x for x in tried if x["result"] != "incompatible"]
        if usable and all(x["result"] == "over_budget" for x in usable):
            raise R.Stop(budget_code, f"every larger-VRAM GPU tier is over the remaining ${remaining:.2f}")
        raise R.Stop("no_larger_gpu", "no larger-VRAM GPU tier is eligible to run now: " +
                     ", ".join(f"{x['gpu']} ({x['result']})" for x in tried)[:1500])
    t, body, ready, est = chosen
    ctx.update(est_next_usd=est, to_gpu=t["gpu"], to_vram_gb=t["vram_gb"], next_provider=ready.get("provider"))
    batch_ref = None
    if bt is not None:
        try:
            batch_ref = _batch_reserve(bt, job.job_id, kind="oom_escalation", est=est, body=body)
        except Refused as e:
            raise R.Stop("batch_budget_exhausted" if e.code == "batch_budget_exhausted" else "batch_stopped",
                         e.message) from None
        ctx["batch_ref"] = batch_ref
    why = (f"out_of_memory on {cur_gpu} ({cur.get('vram_gb')} GB): escalated to {t['gpu']} ({t['vram_gb']:g} GB, the "
           f"next VRAM tier" + (", same family" if t["same_family"] else "") + ")")
    frm = (f"resuming from epoch {resume['epoch']} of {resume['job_id']}" if resume else
           "starting from scratch (no stored checkpoint)")
    chain_info = _chain_info(job, kind="oom_escalation", actor="system", actor_id=f"oom:{job.job_id}"[:64],
                             reason=f"{why}; {frm}")

    def run():
        from fastapi import HTTPException
        try:
            out = _submit(body, who, chain_info, batch_ref)
        except Refused as r:
            raise HTTPException(r.status, r.detail()) from None
        return out["http_code"], {"job_id": out["job_id"], "status": out["status"], "provider": out["provider"]}

    data = _idem_submit(who, "jobs:oom_escalation", f"oom:{job.job_id}", job.job_id, run)
    ctx.pop("batch_ref", None)
    new_id, new_provider = data.get("job_id"), data.get("provider") or ready.get("provider")
    R._decide(job.job_id, "relaunched", code="oom_escalated", reason=f"{why}; escalated as {new_id} on {new_provider}",
              successor_job_id=new_id, resume_from=S._jsonable(resume), spend_so_far_usd=R._dec(spend["usd"]),
              max_total_usd=R._dec(pol["max_total_usd"]), est_next_usd=R._dec(est), detail=S._jsonable(ctx))
    _note(job.job_id, f"escalated as {new_id} on {new_provider}: {why}",
          {"relaunch_event": "oom_escalated", "successor_job_id": new_id, "provider": new_provider,
           "from_gpu": cur_gpu, "from_vram_gb": cur.get("vram_gb"), "to_gpu": t["gpu"], "to_vram_gb": t["vram_gb"],
           "tiers_tried": tried, "resume_from": resume, "spend_so_far_usd": spend["usd"],
           "max_total_usd": pol["max_total_usd"], "est_next_usd": est})
    log.info("job %s: OOM-escalated as %s on %s (%s)", job.job_id, new_id, new_provider, t["gpu"])
    return {"job_id": job.job_id, "decision": "relaunched", "kind": "oom_escalation", "successor_job_id": new_id,
            "provider": new_provider, "to_gpu": t["gpu"], "from_gpu": cur_gpu, "est_next_usd": est}


class InFlight(RuntimeError):
    """Another worker's submission of the same successor is in flight (or its outcome is not known yet)."""


def _idem_submit(who, scope: str, key: str, failed_job_id: str, run) -> dict:
    """routing.idempotency.run around a successor submission of `failed_job_id` (a crashed pass never submits twice:
    the stored outcome replays, an in-flight / unknown one is resolved by UNIQUE(retry_of))."""
    from fastapi import HTTPException

    from jobs import relaunch as R
    from routing import idempotency

    def made(row) -> list[str]:
        s2 = successor_of(failed_job_id)
        return [s2.job_id] if s2 is not None else []

    try:
        code, payload, replayed = idempotency.run(who=who, scope=scope, key=key, body={"continue": failed_job_id},
                                                  fn=run, resource_of=lambda x: x.get("job_id"), reclaim_check=made)
    except HTTPException as exc:
        d = exc.detail if isinstance(exc.detail, dict) else {"code": "route_refused", "message": str(exc.detail)}
        if d.get("code") in ("idempotency_in_progress", "idempotency_outcome_unknown", "already_continued"):
            succ = successor_of(failed_job_id)
            if succ is None:
                raise InFlight(f"successor submission state unknown ({d.get('code')})") from None
            return {"job_id": succ.job_id, "status": succ.status, "provider": (succ.selection or {}).get("provider"),
                    "replayed": True}
        raise R.Stop(d.get("code") or "route_refused", f"the submission was refused: {d.get('code')}: "
                                                       f"{d.get('message')}"[:2000],
                     refused_job_id=d.get("job_id")) from None
    if replayed and (payload or {}).get("detail"):
        d = payload["detail"]
        raise R.Stop(d.get("code") or "route_refused", f"the submission was refused: {d.get('message')}",
                     refused_job_id=d.get("job_id"))
    return (payload or {}).get("data", payload) or {}


def run_oom() -> dict:
    out = {"considered": 0, "escalated": 0, "stopped": 0, "blocked": 0}
    if not settings.job_auto_relaunch_enabled:
        return out
    for jid in oom_candidate_ids():
        r = consider_oom(jid)
        out["considered"] += 1
        d = r.get("decision")
        if d == "relaunched":
            out["escalated"] += 1
        elif d in ("stopped", "blocked"):
            out[d] += 1
    return out


# --------------------------------------------------------------------------
# Runner instructions (the existing runner channel)
# --------------------------------------------------------------------------

def issue_instruction(s, job_id: str, kind: str, *, ref: str | None, expires_at: datetime | None) -> int:
    if kind not in INSTRUCTION_KINDS:
        raise ValueError(kind)
    row = JobRunnerInstruction(job_id=job_id, kind=kind, ref=ref, issued_at=_now(), expires_at=expires_at)
    s.add(row)
    s.flush()
    return row.id


def cancel_instructions(s, job_id: str, ref: str) -> None:
    s.execute(update(JobRunnerInstruction).where(JobRunnerInstruction.job_id == job_id,
                                                 JobRunnerInstruction.ref == ref,
                                                 JobRunnerInstruction.cancelled_at.is_(None))
              .values(cancelled_at=_now()))


def pending_instructions(job_id: str) -> list[dict]:
    """The runner's open instructions (delivered_at stamped on first delivery). Repeated until expired / cancelled:
    the runner treats a known id as already done."""
    now = _now()
    with normalize.SessionLocal.begin() as s:
        rows = list(s.scalars(select(JobRunnerInstruction).where(
            JobRunnerInstruction.job_id == job_id, JobRunnerInstruction.cancelled_at.is_(None),
            (JobRunnerInstruction.expires_at.is_(None)) | (JobRunnerInstruction.expires_at > now))
            .order_by(JobRunnerInstruction.id).limit(10)))
        for r in rows:
            if r.delivered_at is None:
                r.delivered_at = now
        return [{"id": r.id, "kind": r.kind, "ref": r.ref, "issued_at": r.issued_at.isoformat(),
                 "expires_at": _iso(r.expires_at)} for r in rows]


# --------------------------------------------------------------------------
# Move
# --------------------------------------------------------------------------

def new_move_id() -> str:
    return "mv-" + secrets.token_hex(8)


def _last_epoch(job_id: str) -> int:
    from store.jobs import JobMetric
    with normalize.SessionLocal() as s:
        return int(s.scalar(select(func.max(JobMetric.epoch)).where(JobMetric.job_id == job_id)) or 0)


def move_check(job, dep) -> tuple[bool, str | None, str | None]:
    if job.sandbox:
        return False, "move_not_real", "a sandbox job is a simulation"
    if not settings.job_move_enabled:
        return False, "move_disabled", "moving a running job is switched off on this OpenGrid (job_move_enabled)"
    if open_move(job.job_id) is not None:
        return False, "move_in_progress", "a move of this job is already in progress"
    if job.cancel_requested_at is not None or job.status not in ("initializing", "training") \
            or job.last_runner_event_at is None or (job.runner_result or {}).get("complete"):
        return False, "job_not_running", f"only a running job (its runner reporting) can be moved; it is {job.status}"
    if dep is None or dep.status != "running" or dep.terminate_requested_at is not None:
        return False, "job_not_running", "the GPU is not running (" + (dep.status if dep is not None else "none") + ")"
    if _last_epoch(job.job_id) + 1 >= _epochs(job):
        return False, "nothing_to_move", "the job is in its last epoch: no checkpoint comes before it finishes"
    return True, None, None


def _move_plan(job, who, body: dict) -> dict:
    from jobs import relaunch as R
    dep = _dep(job)
    ok, code, why = move_check(job, dep)
    if not ok:
        raise Refused(code, why, 409)
    provider = _provider_arg(body.get("provider"), allow_same=False)
    max_total = _max_total_arg(body.get("max_total_cost_usd"))
    reason = body.get("reason")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
        raise Refused("reason_required", "reason is required: a short text (at most 500 characters)", 422)
    unknown = set(body) - {"provider", "max_total_cost_usd", "reason"}
    if unknown:
        raise Refused("unknown_field", f"unknown field(s): {', '.join(sorted(unknown))}", 422)
    prev = _provider_of(job, dep)
    if provider == prev:
        raise Refused("same_provider", f"the job already runs on {prev}; a move goes to another provider", 422)
    sb = successor_body(job, provider=provider, prev_provider=prev, resume=None, max_total=max_total,
                        ssh_public_key=R._customer_key(job, dep))
    if provider == "best" and prev:
        ex = set(sb["routing"].get("exclude_providers") or ()) | {prev}
        sb["routing"]["exclude_providers"] = sorted(ex)
    bt = _batch_terms(job)
    if bt is not None:
        if bt["remaining_usd"] <= 0:
            raise Refused("batch_budget_exhausted", f"batch {bt['batch_id']} budget exhausted", 409)
        _batch_shape(bt, sb, remaining=bt["remaining_usd"], explicit=None if provider == "best" else [provider])
        if provider == "best" and prev:
            sb["routing"]["exclude_providers"] = sorted(set(sb["routing"].get("exclude_providers") or ()) | {prev})
    sel = _select(sb, who)
    latest = None
    from jobs import artifacts as A
    try:
        latest = A.latest_checkpoint(job.job_id)
    except Exception:  # noqa: BLE001
        latest = None
    return {"job": job, "dep": dep, "provider": provider, "prev_provider": prev, "reason": reason.strip(),
            "body": sb, "sel": sel, "batch": bt, "max_total": max_total, "latest_checkpoint": latest}


def move_preview(job_id: str, who, body: dict) -> dict:
    job = _owned(job_id, who)
    p = _move_plan(job, who, body)
    lc = p["latest_checkpoint"]
    out = {"job_id": job_id, "action": "move", "from_provider": p["prev_provider"], "provider_choice": p["provider"],
           "latest_checkpoint": _ckpt_view({"job_id": job_id, "artifact": lc["artifact"], "epoch": lc["epoch"],
                                            "sha256": lc["sha256"]}) if lc else None,
           "checkpoint_timeout_minutes": settings.job_move_checkpoint_timeout_minutes,
           "steps": ["the runner saves a NEW checkpoint at the end of the current epoch",
                     "OpenGrid verifies it (sha256), then terminates the current GPU",
                     "once the provider confirms the GPU is gone, the successor is submitted resuming from it",
                     f"no new verified checkpoint within {settings.job_move_checkpoint_timeout_minutes} min: the move "
                     "is aborted and the job keeps running"]}
    if p["sel"].get("execution_ready"):
        out.update(can_submit=True, quote=_quote_view(p["sel"], p["body"]), refusal=None)
    else:
        out.update(can_submit=False, quote=None, refusal=_refusal(p["sel"]).detail())
    return out


def move_public(m: JobMove | None) -> dict | None:
    if m is None:
        return None
    return {"move_id": m.move_id, "status": m.status, "open": m.status in MOVE_OPEN, "provider": m.provider,
            "max_total_cost_usd": _f(m.max_total_cost_usd), "reason": m.reason, "requested_at": _iso(m.requested_at),
            "checkpoint_deadline_at": _iso(m.checkpoint_deadline_at), "checkpoint": m.checkpoint,
            "terminate_requested_at": _iso(m.terminate_requested_at), "successor_job_id": m.successor_job_id,
            "outcome_code": m.outcome_code, "outcome_reason": m.outcome_reason, "finished_at": _iso(m.finished_at)}


def latest_move(job_id: str) -> JobMove | None:
    with normalize.SessionLocal() as s:
        return s.scalars(select(JobMove).where(JobMove.job_id == job_id).order_by(JobMove.created_at.desc())).first()


def request_move(job_id: str, who, body: dict) -> tuple[int, dict]:
    """Create the move (owner only): the target is selected first; nothing is touched when it is refused."""
    job = _owned(job_id, who)
    p = _move_plan(job, who, body)
    if not p["sel"].get("execution_ready"):
        raise _refusal(p["sel"])
    now = _now()
    mid = new_move_id()
    actor, actor_id = _who_text(who)
    deadline = now + timedelta(minutes=int(settings.job_move_checkpoint_timeout_minutes))
    q = _quote_view(p["sel"], p["body"])
    try:
        with normalize.SessionLocal.begin() as s:
            j = s.get(Job, job_id, with_for_update=True)
            ok, code, why = move_check(j, _dep(j))
            if not ok:
                raise Refused(code, why, 409)
            m = JobMove(move_id=mid, job_id=job_id, account_id=job.account_id, key_id=who.key_id,
                        principal_kind=who.kind, status="checkpointing", provider=p["provider"],
                        max_total_cost_usd=p["max_total"], reason=p["reason"], requested_at=now,
                        checkpoint_deadline_at=deadline, created_at=now, updated_at=now,
                        detail=S._jsonable({"from_provider": p["prev_provider"], "target_at_request": q}))
            s.add(m)
            s.flush()     # the partial unique index: one open move per job
            iid = issue_instruction(s, job_id, "checkpoint_now", ref=mid, expires_at=deadline)
            S.note(s, j, f"move requested to {p['provider']} (reason: {p['reason']}); waiting for a new verified "
                         f"checkpoint (instruction checkpoint_now #{iid}, deadline {deadline.isoformat()})",
                   {"relaunch_event": "move_requested", "move_id": mid, "provider": p["provider"],
                    "max_total_cost_usd": p["max_total"], "target": q, "instruction_id": iid},
                   actor=actor, actor_id=actor_id)
    except IntegrityError:
        raise Refused("move_in_progress", "a move of this job is already in progress", 409) from None
    from jobs import orchestrator as O
    out = O.get(job_id, who)
    out["move_request"] = {"move_id": mid, "status": "checkpointing", "target": q,
                           "checkpoint_deadline_at": deadline.isoformat()}
    return 202, out


def _move_set(move_id: str, frm: tuple, **values) -> bool:
    """Compare-and-set the move's status (only one worker acts on a step)."""
    values["updated_at"] = _now()
    with normalize.SessionLocal.begin() as s:
        got = s.execute(update(JobMove).where(JobMove.move_id == move_id, JobMove.status.in_(frm))
                        .values(**values).returning(JobMove.move_id)).scalar()
    return bool(got)


def _move_end(m: JobMove, status: str, code: str, reason: str, *, event: str, **values) -> bool:
    if not _move_set(m.move_id, MOVE_OPEN, status=status, outcome_code=code, outcome_reason=reason[:4000],
                     finished_at=_now(), **values):
        return False
    with normalize.SessionLocal.begin() as s:
        cancel_instructions(s, m.job_id, m.move_id)
        j = s.get(Job, m.job_id, with_for_update=True)
        S.note(s, j, f"move {m.move_id} {status}: {reason}"[:4000],
               {"relaunch_event": event, "move_id": m.move_id, "code": code, **{k: v for k, v in values.items()
                                                                                 if k == "successor_job_id"}})
    if status == "aborted" or status == "failed":
        _release((m.detail or {}).get("batch_ref"))
    return True


def _new_checkpoint(m: JobMove) -> dict | None:
    """A checkpoint of the job stored AFTER the move request that re-verifies (sha256), newest first."""
    from jobs import artifacts as A
    for c in A.checkpoints(m.job_id):
        try:
            at = datetime.fromisoformat(c["created_at"])
        except (TypeError, ValueError):
            continue
        if at <= m.requested_at:
            continue
        try:
            got = A.store().get(m.job_id, c["name"])
        except A.ArtifactError:
            continue
        if got is None:
            continue
        if c["epoch"] >= _epochs(_job(m.job_id)):
            continue
        return {"job_id": m.job_id, "artifact": c["name"], "epoch": c["epoch"], "sha256": c["sha256"],
                "size": c["size"], "created_at": c["created_at"], "verified_at": _now().isoformat()}
    return None


def _mover(m: JobMove, job):
    """The move's requester, re-verified now (never anyone else)."""
    from jobs import relaunch as R
    return R.owner(SimpleNamespace(principal_kind=m.principal_kind, account_id=m.account_id, key_id=m.key_id,
                                   job_id=m.job_id))


def advance_move(move_id: str, *, now: datetime | None = None) -> dict:
    """One step of a move. Never raises (an internal error leaves the step for the next pass)."""
    now = now or _now()
    with normalize.SessionLocal() as s:
        m = s.get(JobMove, move_id)
    if m is None or m.status not in MOVE_OPEN:
        return {"move_id": move_id, "status": m.status if m else None, "acted": False}
    try:
        if m.status in ("requested", "checkpointing"):
            return _step_checkpoint(m, now)
        if m.status == "terminating":
            return _step_terminating(m, now)
        if m.status == "submitting":
            return _step_submit(m)
    except Exception as exc:  # noqa: BLE001
        log.exception("move %s: step failed", move_id)
        return {"move_id": move_id, "status": m.status, "acted": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
    return {"move_id": move_id, "status": m.status, "acted": False}


def _step_checkpoint(m: JobMove, now: datetime) -> dict:
    from jobs import relaunch as R
    from jobs import sync
    job = _job(m.job_id)
    dep = _dep(job)
    if job.cancel_requested_at is not None:
        _move_end(m, "aborted", "job_cancelled", "the job was cancelled during the move", event="move_aborted")
        return {"move_id": m.move_id, "status": "aborted", "acted": True}
    if job.status not in ("initializing", "training") or dep is None or dep.status != "running" \
            or dep.terminate_requested_at is not None or (job.runner_result or {}).get("complete"):
        _move_end(m, "aborted", "job_not_running", f"the job is no longer running ({job.status}; GPU "
                                                   f"{dep.status if dep is not None else 'none'}): nothing was moved",
                  event="move_aborted")
        return {"move_id": m.move_id, "status": "aborted", "acted": True}
    ck = _new_checkpoint(m)
    if ck is None:
        if now >= m.checkpoint_deadline_at:
            _move_end(m, "aborted", "checkpoint_timeout", f"no new verified checkpoint within "
                                                         f"{settings.job_move_checkpoint_timeout_minutes} min of the "
                                                         "request: the job keeps running on its current GPU",
                      event="move_aborted")
            return {"move_id": m.move_id, "status": "aborted", "acted": True}
        return {"move_id": m.move_id, "status": m.status, "acted": False, "waiting": "checkpoint"}
    # The target is re-selected now (the market moved since the request); nothing eligible -> abort, job untouched.
    who = _mover(m, job)
    body = successor_body(job, provider=m.provider, prev_provider=_provider_of(job, dep), resume=ck,
                          max_total=float(m.max_total_cost_usd), ssh_public_key=R._customer_key(job, dep))
    prev = _provider_of(job, dep)
    if m.provider == "best" and prev:
        body["routing"]["exclude_providers"] = sorted(set(body["routing"].get("exclude_providers") or ()) | {prev})
    bt = _batch_terms(job)
    if bt is not None:
        _batch_shape(bt, body, remaining=bt["remaining_usd"],
                     explicit=None if m.provider == "best" else [m.provider])
        if m.provider == "best" and prev:
            body["routing"]["exclude_providers"] = sorted(set(body["routing"].get("exclude_providers") or ()) | {prev})
    sel = _select(body, who)
    ready = sel.get("execution_ready")
    if not ready:
        r = _refusal(sel)
        _move_end(m, "aborted", r.code, f"no eligible target now ({r.message[:300]}): the job keeps running",
                  event="move_aborted")
        return {"move_id": m.move_id, "status": "aborted", "acted": True}
    est = float((ready.get("cost") or {}).get("est_max_total") or 0)
    batch_ref = _batch_reserve(bt, m.job_id, kind="move", est=est, body=body, ref=m.move_id) \
        if bt is not None else None
    det = {**(m.detail or {}), "batch_ref": batch_ref, "target_at_checkpoint": _quote_view(sel, body)}
    if not _move_set(m.move_id, ("requested", "checkpointing"), status="terminating", checkpoint=S._jsonable(ck),
                     terminate_requested_at=_now(), detail=S._jsonable(det)):
        _release(batch_ref)
        return {"move_id": m.move_id, "status": "?", "acted": False, "reason": "another worker took the step"}
    with normalize.SessionLocal.begin() as s:
        cancel_instructions(s, m.job_id, m.move_id)
        j = s.get(Job, m.job_id, with_for_update=True)
        S.note(s, j, f"move {m.move_id}: new checkpoint {ck['artifact']} (epoch {ck['epoch']}) verified (sha256); "
                     "terminating the current GPU", {"relaunch_event": "move_checkpoint_verified",
                                                     "move_id": m.move_id, "checkpoint": ck, "batch": batch_ref})
        if S.can(j.status, "terminating"):
            S.transition(s, j, "terminating", actor="system", actor_id=f"move:{m.move_id}",
                         reason=f"moving to another GPU ({m.reason or 'requested by the owner'}): terminating after "
                                f"the verified checkpoint {ck['artifact']}",
                         evidence={"move_id": m.move_id, "checkpoint": ck}, outcome="cancelled", failure_code="moved")
    term = sync._terminate(m.job_id, job.deployment_id, f"moving to another GPU (move {m.move_id})")
    return {"move_id": m.move_id, "status": "terminating", "acted": True, "terminate": term}


def _step_terminating(m: JobMove, now: datetime) -> dict:
    from jobs import relaunch as R
    from jobs import sync
    job = _job(m.job_id)
    dep = _dep(job)
    if R.provider_confirmed_gone(job, dep):
        if _move_set(m.move_id, ("terminating",), status="submitting"):
            _note(m.job_id, f"move {m.move_id}: the provider confirmed the GPU is gone; submitting the successor",
                  {"relaunch_event": "move_terminated", "move_id": m.move_id})
        with normalize.SessionLocal() as s:
            m2 = s.get(JobMove, m.move_id)
        return _step_submit(m2) if m2.status == "submitting" else {"move_id": m.move_id, "status": m2.status,
                                                                    "acted": False}
    if dep is not None and dep.terminate_requested_at is None and dep.status in ("running", "degraded", "stopped",
                                                                                 "stopping"):
        sync._terminate(m.job_id, dep.deployment_id, f"moving to another GPU (move {m.move_id}; re-issued)")
    else:
        sync.sync_job(m.job_id, enforce=False)
    limit = timedelta(minutes=int(settings.job_move_termination_timeout_minutes))
    if m.terminate_requested_at is not None and now - m.terminate_requested_at > limit:
        det = dict(m.detail or {})
        if not det.get("termination_alerted"):
            det["termination_alerted"] = True
            _move_set(m.move_id, ("terminating",), detail=S._jsonable(det))
            try:
                from alerts import ops
                ops.alert("move_termination_stalled", f"Job {m.job_id}: a move is waiting for the provider to confirm "
                                                      "the termination", severity="major",
                          provider=_provider_of(job, dep), detail={"job_id": m.job_id, "move_id": m.move_id,
                                                                   "deployment_status": dep.status if dep else None},
                          dedupe=f"move:{m.move_id}")
            except Exception:  # noqa: BLE001
                log.exception("move %s: alert failed", m.move_id)
    return {"move_id": m.move_id, "status": "terminating", "acted": False, "waiting": "provider_confirmation"}


def _step_submit(m: JobMove) -> dict:
    from jobs import relaunch as R
    job = _job(m.job_id)
    succ = successor_of(m.job_id)
    if succ is not None:
        _move_end(m, "moved", "moved", f"continues as {succ.job_id}", event="moved", successor_job_id=succ.job_id)
        return {"move_id": m.move_id, "status": "moved", "successor_job_id": succ.job_id, "acted": False}
    try:
        who = _mover(m, job)
    except R.Stop as e:
        _move_end(m, "failed", e.code, f"the successor was not submitted: {e.message}; the verified checkpoint "
                                       "is kept (Resume works once the owner can launch again)", event="move_failed")
        return {"move_id": m.move_id, "status": "failed", "acted": True}
    dep = _dep(job)
    ck = m.checkpoint or {}
    body = successor_body(job, provider=m.provider, prev_provider=_provider_of(job, dep), resume=ck,
                          max_total=float(m.max_total_cost_usd), ssh_public_key=R._customer_key(job, dep))
    prev = _provider_of(job, dep)
    if m.provider == "best" and prev:
        body["routing"]["exclude_providers"] = sorted(set(body["routing"].get("exclude_providers") or ()) | {prev})
    batch_ref = (m.detail or {}).get("batch_ref")
    if batch_ref:
        bt = None
        try:
            bt = _batch_terms(job)
        except Refused:
            bt = None
        if bt is not None:
            from jobs import batches as B
            B.shape_relaunch_body(bt, body, remaining=max(float(batch_ref["reserved_usd"]), bt["remaining_usd"]))
            if m.provider != "best":
                body["routing"]["include_providers"] = [m.provider]
            if m.provider == "best" and prev:
                body["routing"]["exclude_providers"] = sorted(set(body["routing"].get("exclude_providers") or ())
                                                              | {prev})
        body["limits"]["max_total_cost_usd"] = round(min(float(m.max_total_cost_usd),
                                                         float(batch_ref["reserved_usd"])), 2)
    actor_id = f"key:{m.key_id}" if m.key_id is not None else (m.principal_kind or "operator")
    chain_info = _chain_info(job, kind="move", actor="system", actor_id=f"move:{m.move_id}",
                             reason=f"moved by its owner ({m.reason}) from {prev or '?'}; resuming from epoch "
                                    f"{ck.get('epoch')} of {m.job_id} (checkpoint {ck.get('artifact')}, verified "
                                    "before the old GPU was terminated)")
    chain_info["requested_by"] = actor_id

    def run():
        from fastapi import HTTPException
        try:
            out = _submit(body, who, chain_info, batch_ref)
        except Refused as r:
            raise HTTPException(r.status, r.detail()) from None
        return out["http_code"], {"job_id": out["job_id"], "status": out["status"], "provider": out["provider"]}

    try:
        data = _idem_submit(who, "jobs:move", f"move:{m.move_id}", m.job_id, run)
    except InFlight:
        return {"move_id": m.move_id, "status": "submitting", "acted": False, "waiting": "submission in flight"}
    except R.Stop as e:
        _move_end(m, "failed", e.code, f"the successor was refused: {e.message}; the old GPU is gone and the "
                                       "verified checkpoint is kept: Resume the job to try again", event="move_failed")
        return {"move_id": m.move_id, "status": "failed", "acted": True, "code": e.code}
    nid = data.get("job_id")
    _move_end(m, "moved", "moved", f"continues as {nid} on {data.get('provider')} from epoch {ck.get('epoch')}",
              event="moved", successor_job_id=nid)
    return {"move_id": m.move_id, "status": "moved", "successor_job_id": nid, "acted": True}


def open_move_ids(limit: int = 200) -> list[str]:
    with normalize.SessionLocal() as s:
        return list(s.scalars(select(JobMove.move_id).where(JobMove.status.in_(MOVE_OPEN))
                              .order_by(JobMove.updated_at).limit(limit)))


def run_moves() -> dict:
    if not settings.job_move_enabled:
        return {"enabled": False}
    out = {"enabled": True, "open": 0, "acted": 0}
    for mid in open_move_ids():
        out["open"] += 1
        if advance_move(mid).get("acted"):
            out["acted"] += 1
    return out


@_register_job("job_move", every_seconds=settings.job_move_interval_seconds, initial_delay_seconds=80)
def _job_move():
    return run_moves()


# --------------------------------------------------------------------------
# Move suggestion (read-only; GET /v1/jobs/{id})
# --------------------------------------------------------------------------

def _degraded(dep) -> str | None:
    if dep is None:
        return None
    if dep.status == "degraded":
        return "the GPU's deployment is degraded (the provider reports an error state)"
    try:
        from routing import control
        f = control.provider_flags(dep.provider or "")
        if f.get("killed"):
            return f"launches on {dep.provider} are paused by OpenGrid (provider kill switch)"
    except Exception:  # noqa: BLE001
        return None
    return None


def move_suggestion(job, dep, who) -> dict | None:
    """A suggestion object, or None. Never acts; cached for SUGGEST_TTL seconds per job."""
    if job is None or job.sandbox or job.status not in ("initializing", "training") or dep is None \
            or dep.status not in ("running", "degraded") or job.cancel_requested_at is not None:
        return None
    if open_move(job.job_id) is not None:
        return None
    key = job.job_id
    hit = _SUGGEST_CACHE.get(key)
    if hit and time.monotonic() - hit[0] < SUGGEST_TTL:
        return hit[1]
    try:
        out = _suggest(job, dep, who)
    except Exception:  # noqa: BLE001 - a suggestion never breaks a read
        log.exception("job %s: move suggestion failed", job.job_id)
        out = None
    _SUGGEST_CACHE[key] = (time.monotonic(), out)
    if len(_SUGGEST_CACHE) > 2000:
        _SUGGEST_CACHE.clear()
    return out


def _suggest(job, dep, who) -> dict | None:
    from jobs import relaunch as R
    epochs = _epochs(job)
    done = _last_epoch(job.job_id)
    rem = max(0.0, (epochs - done) / epochs)
    if rem <= 0 or done + 1 >= epochs:
        return None
    typ_now = _f(job.est_typical_total_usd)
    if not typ_now:
        return None
    current = round(typ_now * rem, 4)
    prev = _provider_of(job, dep)
    lim = float(((job.spec or {}).get("limits") or {}).get("max_total_cost_usd") or 0) or 1.0
    body = successor_body(job, provider="best", prev_provider=prev, resume=None, max_total=lim, ssh_public_key=None)
    if prev:
        body["routing"]["exclude_providers"] = sorted(set(body["routing"].get("exclude_providers") or ()) | {prev})
    try:
        sel = _select(body, who)
    except Refused:
        return None
    ready = sel.get("execution_ready")
    if not ready:
        return None
    cost = ready.get("cost") or {}
    typ_alt = float(cost.get("est_typical_total") or cost.get("est_max_total") or 0)
    count = int(((job.spec or {}).get("resources") or {}).get("gpu_count") or 1)
    overhead = round(float(ready.get("price_per_gpu_hour") or 0) * count * float(settings.job_move_overhead_minutes)
                     / 60.0, 4)
    after = round(typ_alt * rem + overhead, 4)
    savings = round((current - after) / current * 100.0, 1) if current > 0 else 0.0
    degraded = _degraded(dep)
    cheaper = savings >= float(settings.job_move_suggest_min_savings_pct)
    if not cheaper and not degraded:
        return None
    return {"suggested": True, "reason": "provider_degraded" if degraded and not cheaper else "cheaper",
            "degraded": degraded, "from": {"provider": prev, "gpu": (job.selection or {}).get("gpu"),
                                           "price_per_gpu_hour": _f(dep.actual_price_per_gpu_hour)
                                           or (job.selection or {}).get("price_per_gpu_hour")},
            "to": {"provider": ready.get("provider"), "gpu": ready.get("gpu"), "region": ready.get("region"),
                   "price_per_gpu_hour": ready.get("price_per_gpu_hour")},
            "remaining_fraction": round(rem, 4), "epochs_done": done, "epochs": epochs,
            "est_remaining_cost_now_usd": current, "est_remaining_cost_after_move_usd": after,
            "move_overhead_usd": overhead, "savings_pct": savings,
            "threshold_pct": float(settings.job_move_suggest_min_savings_pct), "kind": "estimated",
            "basis": "remaining = typical total x remaining epochs / epochs; after the move = the alternative's typical "
                     f"total x the same fraction + {settings.job_move_overhead_minutes:g} min of its price (boot + "
                     "resume)", "auto": False, "move_enabled": bool(settings.job_move_enabled),
            "action": f"POST /v1/jobs/{job.job_id}/move",
            "note": "a suggestion only: OpenGrid never moves a job on its own"}


# --------------------------------------------------------------------------
# The job page block (GET /v1/jobs/{id})
# --------------------------------------------------------------------------

def actions_block(job, dep, who) -> dict:
    ok, code, why = resume_check(job, dep)
    mok, mcode, mwhy = move_check(job, dep)
    lm = latest_move(job.job_id)
    return {"resume": {"available": ok, "code": code, "reason": why,
                       "endpoint": f"/v1/jobs/{job.job_id}/resume", "preview": f"/v1/jobs/{job.job_id}/resume/preview"},
            "move": {"available": mok, "enabled": bool(settings.job_move_enabled), "code": mcode, "reason": mwhy,
                     "endpoint": f"/v1/jobs/{job.job_id}/move", "preview": f"/v1/jobs/{job.job_id}/move/preview",
                     "latest": move_public(lm)}}


def pending_succession(job_ids) -> set[str]:
    """Jobs whose successor is being prepared (an open move, or a claimed OOM escalation): a batch shard stays
    `relaunching`, never cancelled / failed, in between."""
    ids = [j for j in job_ids if j]
    if not ids:
        return set()
    with normalize.SessionLocal() as s:
        moves = set(s.scalars(select(JobMove.job_id).where(JobMove.job_id.in_(ids), JobMove.status.in_(MOVE_OPEN))))
    return moves
