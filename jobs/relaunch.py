"""Automatic relaunch of an interrupted job (methodology/jobs.md "Automatic relaunch"; execution-safety.md).

OFF unless BOTH settings.job_auto_relaunch_enabled AND the job's own policy ask for it:

    retry: {max_attempts: 1..3 (1 = never; default), max_total_usd: the ceiling across ALL attempts (required > 1)}

Trigger (all of them, re-read from the database on every pass):
    * a REAL job (never a sandbox: a simulation is never interrupted, so nothing is ever relaunched for one)
    * failure_code gpu_interrupted or runner_lost (jobs/sync.py); never training_failed / out_of_memory /
      bootstrap_* / training_timed_out / artifact_upload_failed / a cancel / a validation error: those would fail
      again, or were asked for
    * THE CORE RULE: the previous machine is provider-confirmed gone: the job is termination_confirmed /
      reconciliation_pending / reconciled AND its deployment is `terminated` (terminated_at set, provider
      evidence). Never from degraded, termination_failed, orphan_suspected, credentials_unavailable,
      provider_timeout, provisioning_uncertain, launch_unknown or any state where existence is unproven
    * no cancel was ever requested on it

Decision (consider(job_id); the `job_relaunch` background job every settings.job_relaunch_interval_seconds):
    1. claim: INSERT job_relaunches(failed_job_id PRIMARY KEY) ... ON CONFLICT DO NOTHING. Exactly one worker owns the
       decision; a decided row (relaunched | stopped) is final; a `blocked` (internal error) or stale `claimed` row
       is re-claimed optimistically (UPDATE ... WHERE status/updated_at unchanged)
    2. an existing successor (jobs.retry_of = this job) -> recorded, nothing submitted
    3. attempts: attempt >= max_attempts -> stop max_attempts_reached
    4. owner: the job's ORIGINAL principal, re-checked now: its API key exists, belongs to the account, is not
       revoked (nor any key above it), not expired, still holds route:execute, the account is active. Otherwise
       stop (owner_key_revoked | owner_key_expired | owner_scope_missing | account_suspended | owner_unavailable).
       The operator's own job relaunches as the operator; a customer job never gets more than its own key had
    5. exclusions: every provider that interrupted / lost an attempt of the chain (+ the original request's
       routing.exclude_providers)
    6. money: spend_so_far = per attempt, max(reconciled transaction cost, provider-reported cost, metered usage)
       once reconciled, else max(metered usage, the attempt's est_max_total_usd) (an upper bound while the
       reconciliation is pending). The successor's limits.max_total_cost_usd = min(the original limit,
       max_total_usd - spend_so_far), and the selection's est_max_total must satisfy
       spend_so_far + est_max_total <= max_total_usd, else stop retry_budget_exhausted. The existing per-job and
       account cost guards apply on top, unchanged (the normal route re-checks them)
    7. resume_from: the failed attempt's latest verified checkpoint, else the newest one anywhere in the chain; none
       (or none that re-verifies) -> from scratch, said so
    8. nothing eligible after the exclusions -> stop no_eligible_provider (the chain ends; no further attempts)
    9. submit: jobs.orchestrator.submit(body, owner, chain=...) -- the SAME validate / select / quote /
       engine.route / approval / guard path a human submission takes (SUPERVISED -> pending_approval for an admin),
       inside routing.idempotency.run(scope "jobs:relaunch", key "relaunch:<failed job id>"), and the new job row
       carries retry_of under UNIQUE(retry_of). Any refusal ends the chain with that code
   10. events: the old job gets "relaunched as job-X on <provider> resuming from epoch N", the new one is created
       with "attempt N of M, relaunched after <cause> on <provider>"; a stop is a note "not relaunched: <reason>".
       Both carry evidence.relaunch_event for the jobs feed (alerts/channels.py). An internal error -> `blocked`,
       retried next pass, ops alert relaunch_blocked (alerts/ops.py)

Reliability (routing/reliability.py) reads deployments, never jobs: the interrupted attempt's deployment is counted
once (its own interruption), the successor is a NEW deployment counted like any other launch.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import Integer, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

import normalize
from config import settings
from jobs import job as _register_job
from jobs import states as S
from store.jobs import Job, JobRelaunch

log = logging.getLogger(__name__)

TRIGGER_CODES = ("gpu_interrupted", "runner_lost")
CONFIRMED_GONE = ("termination_confirmed", "reconciliation_pending", "reconciled")
FINAL = ("relaunched", "stopped")
CLAIM_STALE = timedelta(minutes=15)
IDEM_SCOPE = "jobs:relaunch"
CAUSE = {"gpu_interrupted": "the provider interrupted the GPU", "runner_lost": "the GPU machine stopped reporting"}
STOP_TEXT = {
    "max_attempts_reached": "every attempt the retry policy allows has been used",
    "retry_budget_exhausted": "retry budget exhausted: the spend so far plus the next attempt's maximum would pass "
                              "retry.max_total_usd",
    "no_eligible_provider": "no eligible provider is left after excluding the ones that interrupted this job",
    "owner_key_revoked": "the API key that submitted the job was revoked",
    "owner_key_expired": "the API key that submitted the job has expired",
    "owner_scope_missing": "the API key that submitted the job no longer has the route:execute scope",
    "account_suspended": "the account is suspended",
    "owner_unavailable": "the job's owner could not be verified",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _f(v) -> float | None:
    return None if v is None else float(v)


# --------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------

def policy(job_or_spec) -> dict | None:
    """{max_attempts, max_total_usd} from a job (its spec) or a validated request; None without a retry block."""
    spec = job_or_spec if isinstance(job_or_spec, dict) else (getattr(job_or_spec, "spec", None) or {})
    r = spec.get("retry") if isinstance(spec, dict) else None
    if not isinstance(r, dict):
        return None
    try:
        n = int(r.get("max_attempts") or 1)
    except (TypeError, ValueError):
        return None
    cap = r.get("max_total_usd")
    return {"max_attempts": n, "max_total_usd": None if cap is None else float(cap)}


def policy_preview(request: dict) -> dict:
    """The retry block of POST /v1/jobs/preview's submission."""
    p = policy(request) or {"max_attempts": 1, "max_total_usd": None}
    on = bool(settings.job_auto_relaunch_enabled)
    sandbox = bool(request.get("sandbox"))
    if p["max_attempts"] <= 1:
        note = "max_attempts 1: never relaunched automatically"
    elif sandbox:
        note = "sandbox: a simulation is never interrupted, so nothing is ever relaunched (policy recorded only)"
    elif not on:
        note = ("automatic relaunch is switched off on this OpenGrid (job_auto_relaunch_enabled): the policy is "
                "recorded but nothing is relaunched")
    else:
        note = (f"if the GPU is interrupted or stops reporting, and only once the provider confirms it is gone, OpenGrid "
                f"submits up to {p['max_attempts'] - 1} new attempt(s) through the same quote / approval / cost-guard "
                f"path, on another provider, resuming from the latest checkpoint, while all attempts together stay "
                f"within ${p['max_total_usd']:.2f}")
    return {**p, "auto_relaunch_enabled": on, "triggers": list(TRIGGER_CODES), "applies": p["max_attempts"] > 1
            and on and not sandbox, "note": note}


def attempt_of(job) -> int:
    return int(job.attempt or 1)


def root_of(job) -> str:
    return job.root_job_id or job.job_id


# --------------------------------------------------------------------------
# Eligibility (THE CORE RULE)
# --------------------------------------------------------------------------

def provider_confirmed_gone(job, dep) -> bool:
    """The previous machine cannot be billing: job past termination AND the deployment provider-confirmed terminated."""
    return (job.status in CONFIRMED_GONE and dep is not None and dep.status == "terminated"
            and dep.terminated_at is not None)


def trigger(job, dep) -> tuple[bool, str]:
    """(eligible to be considered, why not). Never True unless the core rule holds."""
    if job.sandbox:
        return False, "sandbox"
    p = policy(job)
    if not p or p["max_attempts"] <= 1:
        return False, "no retry policy"
    if job.failure_code not in TRIGGER_CODES:
        return False, f"failure {job.failure_code or 'none'} is not a relaunch trigger"
    if job.cancel_requested_at is not None or job.outcome == "cancelled":
        return False, "cancelled"
    if not provider_confirmed_gone(job, dep):
        return False, "the previous GPU is not provider-confirmed terminated yet"
    return True, ""


def _dep(s, job):
    from store.routing import Deployment
    return s.get(Deployment, job.deployment_id) if job.deployment_id else None


def candidate_ids(limit: int = 200) -> list[str]:
    """Failed attempts whose decision is not taken yet (consider() re-checks everything)."""
    max_att = func.coalesce(Job.spec["retry"]["max_attempts"].astext.cast(Integer), 1)
    decided = select(JobRelaunch.failed_job_id).where(JobRelaunch.failed_job_id == Job.job_id,
                                                      JobRelaunch.status.in_(FINAL)).exists()
    with normalize.SessionLocal() as s:
        return list(s.scalars(select(Job.job_id).where(
            Job.sandbox.is_(False), Job.failure_code.in_(TRIGGER_CODES), Job.status.in_(CONFIRMED_GONE),
            Job.cancel_requested_at.is_(None), max_att > 1, ~decided).order_by(Job.updated_at).limit(limit)))


# --------------------------------------------------------------------------
# Chain, spend, exclusions, resume
# --------------------------------------------------------------------------

def chain_jobs(s, root_id: str) -> list:
    return list(s.scalars(select(Job).where(or_(Job.job_id == root_id, Job.root_job_id == root_id))
                          .order_by(func.coalesce(Job.attempt, 1), Job.created_at)))


def attempt_spend(job, dep) -> dict:
    """What one attempt cost (conservatively). See the module docstring, step 6."""
    if job.sandbox:
        return {"usd": 0.0, "basis": "simulated: nothing billed"}
    if dep is None:
        return {"usd": 0.0, "basis": "no deployment: nothing launched"}
    from routing import transactions as T
    tot = T.metered_totals(dep.deployment_id)
    metered = max(float(tot["usage_cost_usd"]), float(tot["slice_cost_usd"]))
    if dep.status in ("rejected", "provision_failed", "provider_rejected", "quote_failed") and \
            not dep.provider_instance_id and metered == 0:
        return {"usd": 0.0, "basis": f"deployment {dep.status}: nothing was created"}
    if dep.status == "terminated" and dep.reconciled_at is not None:
        rec = dep.reconciliation or {}
        txn = float(((rec.get("opengrid_transaction_cost") or {}).get("amount_usd")) or 0)
        prov = float(dep.provider_reported_cost or 0)
        return {"usd": round(max(txn, prov, metered), 4), "basis": "reconciled (max of transaction, provider-reported "
                                                                    "and metered cost)"}
    est = float(job.est_max_total_usd or 0)
    return {"usd": round(max(metered, est), 4), "basis": "not reconciled yet: max of metered usage and the attempt's "
                                                         "est_max_total_usd (upper bound)"}


def chain_spend(s, chain) -> dict:
    per = []
    for j in chain:
        sp = attempt_spend(j, _dep(s, j))
        per.append({"job_id": j.job_id, "attempt": attempt_of(j), **sp})
    return {"usd": round(sum(p["usd"] for p in per), 4), "attempts": per}


def _provider_of(s, j) -> str | None:
    d = _dep(s, j)
    return (d.provider if d is not None else None) or (j.selection or {}).get("provider")


def exclusions(s, chain, root_spec: dict) -> list[str]:
    out = set(((root_spec or {}).get("routing") or {}).get("exclude_providers") or ())
    for j in chain:
        if j.failure_code in TRIGGER_CODES:
            p = _provider_of(s, j)
            if p:
                out.add(p)
    return sorted(out)


def pick_resume(job, chain, epochs: int) -> tuple[dict | None, list[str]]:
    """({job_id, artifact, epoch, sha256} | None, notes). The failed attempt first, then the chain newest first."""
    from jobs import artifacts as A
    notes = []
    order = [job] + [j for j in sorted(chain, key=lambda x: (attempt_of(x), x.created_at), reverse=True)
                     if j.job_id != job.job_id]
    best = None
    for j in order:
        try:
            lc = A.latest_checkpoint(j.job_id)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"{j.job_id}: checkpoints unreadable ({type(exc).__name__})")
            continue
        if lc is None:
            continue
        if best is not None and lc["epoch"] <= best["epoch"]:
            continue
        try:
            pinned = A.resolve_resume(job.account_id, {"job_id": j.job_id, "artifact": lc["artifact"]}, epochs=epochs)
        except A.ArtifactError as exc:
            notes.append(f"{j.job_id} {lc['artifact']}: not usable ({exc.code})")
            continue
        best = pinned
        if j.job_id == job.job_id:
            break     # the failed attempt's own latest checkpoint wins
    return best, notes


# --------------------------------------------------------------------------
# The owner (never escalated)
# --------------------------------------------------------------------------

class Stop(Exception):
    def __init__(self, code: str, message: str | None = None, **detail):
        super().__init__(message or STOP_TEXT.get(code, code))
        self.code, self.message, self.detail = code, message or STOP_TEXT.get(code, code), detail


def owner(job):
    """The job's original principal, re-verified now (module docstring, step 4). Raises Stop."""
    from accounts.auth import ALL, OPERATOR, Principal

    if job.principal_kind == "operator" and job.account_id is None and job.key_id is None:
        return OPERATOR
    if job.principal_kind != "api_key" or job.key_id is None or job.account_id is None:
        raise Stop("owner_unavailable", f"the job's principal ({job.principal_kind or 'unknown'}) cannot be "
                                        "re-verified")
    from accounts import keys
    from store.accounts import Account, ApiKey
    from store.security import ApiKeyLineage
    now = _now()
    with normalize.SessionLocal() as s:
        k = s.get(ApiKey, job.key_id)
        acct = s.get(Account, job.account_id)
        lin = s.get(ApiKeyLineage, job.key_id)
        ancestor_revoked = s.execute(keys._ANCESTOR_REVOKED, {"k": job.key_id}).first() is not None
    if k is None or k.account_id != job.account_id or acct is None:
        raise Stop("owner_unavailable", "the API key or account that submitted the job no longer exists")
    if k.revoked_at is not None or ancestor_revoked:
        raise Stop("owner_key_revoked")
    if k.expires_at is not None and k.expires_at <= now:
        raise Stop("owner_key_expired")
    if acct.status != "active":
        raise Stop("account_suspended")
    platform_admin = bool(lin.platform_admin) if lin is not None else False
    scopes = frozenset(x for x in (k.scopes or []) if x != ALL)   # the same reduction accounts.keys.verify applies
    if not platform_admin:
        scopes = scopes - {"admin"}
    if "route:execute" not in scopes:
        raise Stop("owner_scope_missing")
    return Principal(kind="api_key", account_id=k.account_id, key_id=k.id, scopes=scopes,
                     platform_admin=platform_admin)


# --------------------------------------------------------------------------
# Claim + record
# --------------------------------------------------------------------------

def _claim(job, now) -> JobRelaunch | None:
    """The decision row, owned by this worker now; None if decided or being decided elsewhere."""
    stmt = (insert(JobRelaunch).values(failed_job_id=job.job_id, root_job_id=root_of(job), attempt=attempt_of(job),
                                       status="claimed", tries=1, created_at=now, updated_at=now)
            .on_conflict_do_nothing(index_elements=["failed_job_id"]).returning(JobRelaunch.failed_job_id))
    with normalize.SessionLocal.begin() as s:
        if s.execute(stmt).scalar() is not None:
            return s.get(JobRelaunch, job.job_id)
    with normalize.SessionLocal() as s:
        row = s.get(JobRelaunch, job.job_id)
    if row is None or row.status in FINAL:
        return None
    if row.status == "claimed" and now - row.updated_at < CLAIM_STALE:
        return None
    with normalize.SessionLocal.begin() as s:     # optimistic re-claim: only one worker wins
        got = s.execute(update(JobRelaunch).where(JobRelaunch.failed_job_id == job.job_id,
                                                  JobRelaunch.status == row.status,
                                                  JobRelaunch.updated_at == row.updated_at)
                        .values(status="claimed", tries=JobRelaunch.tries + 1, updated_at=now)
                        .returning(JobRelaunch.failed_job_id)).scalar()
        return s.get(JobRelaunch, job.job_id) if got else None


def _decide(job_id: str, status: str, *, code: str | None = None, reason: str | None = None, **values) -> None:
    now = _now()
    with normalize.SessionLocal.begin() as s:
        row = s.get(JobRelaunch, job_id, with_for_update=True)
        row.status, row.reason_code, row.reason = status, code, (reason or None)
        row.updated_at = now
        if status in FINAL:
            row.decided_at = now
        for k, v in values.items():
            setattr(row, k, v)


def _dec(v):
    return None if v is None else Decimal(str(round(float(v), 4)))


def _stop(job, exc: Stop, ctx: dict) -> dict:
    _decide(job.job_id, "stopped", code=exc.code, reason=exc.message, detail=S._jsonable({**ctx, **exc.detail}),
            excluded_providers=ctx.get("excluded_providers"), spend_so_far_usd=_dec(ctx.get("spend_so_far_usd")),
            max_total_usd=_dec(ctx.get("max_total_usd")), est_next_usd=_dec(ctx.get("est_next_usd")),
            successor_job_id=exc.detail.get("refused_job_id"))
    with normalize.SessionLocal.begin() as s:
        j = s.get(Job, job.job_id, with_for_update=True)
        S.note(s, j, f"not relaunched: {exc.message}"[:4000],
               {"relaunch_event": "chain_stopped", "code": exc.code, "attempt": attempt_of(job),
                **{k: ctx.get(k) for k in ("max_attempts", "spend_so_far_usd", "max_total_usd", "est_next_usd",
                                           "excluded_providers") if ctx.get(k) is not None},
                **exc.detail})
    log.info("job %s: relaunch chain stopped (%s): %s", job.job_id, exc.code, exc.message)
    return {"job_id": job.job_id, "decision": "stopped", "code": exc.code, "reason": exc.message}


def _alert_blocked(job, err: str, tries: int) -> None:
    if tries not in (1, 10, 60):
        return
    try:
        from alerts import ops
        ops.alert("relaunch_blocked", f"Job {job.job_id}: an automatic relaunch was due but an internal error "
                                      "blocked it", severity="major", provider=(job.selection or {}).get("provider"),
                  detail={"job_id": job.job_id, "account_id": job.account_id, "error": err[:300], "tries": tries,
                          "suggested_action": "the previous GPU is provider-confirmed terminated (nothing bills); "
                                              "fix the error or resubmit the job with resume_from by hand"},
                  dedupe=f"relaunch:{job.job_id}")
    except Exception:  # noqa: BLE001 - an alert path never breaks the pass
        log.exception("relaunch_blocked alert failed for %s", job.job_id)


# --------------------------------------------------------------------------
# The decision
# --------------------------------------------------------------------------

def _body(root_spec: dict, *, exclude: list[str], resume: dict | None, max_total: float, ssh_public_key: str | None):
    """The successor's request: the ROOT attempt's validated request (same template, config, resources, strategy,
    price cap, runtime, retry policy, name), with only exclude_providers, resume_from and a total cost limit that can
    only go DOWN (the remaining chain budget)."""
    from workloads.spec import WorkloadSpec
    spec = dict(root_spec or {})
    body = {k: spec[k] for k in WorkloadSpec.model_fields if k in spec}
    body["routing"] = {**(spec.get("routing") or {}), "exclude_providers": exclude}
    body["limits"] = {**spec["limits"], "max_total_cost_usd": round(min(float(spec["limits"]["max_total_cost_usd"]),
                                                                         max_total), 2)}
    body.pop("resume_from", None)
    if resume:
        body["resume_from"] = {"job_id": resume["job_id"], "artifact": resume["artifact"]}
    body["ssh_public_key"] = ssh_public_key
    body["sandbox"] = False
    return body


def _customer_key(job, dep) -> str | None:
    """The customer's own public key from the previous deployment, only if it is exactly the job's fingerprint."""
    if job.ssh_key_mode != "customer" or dep is None:
        return None
    pk = (dep.launch or {}).get("ssh_public_key")
    if not pk:
        return None
    try:
        from routing import credentials
        parsed = credentials.parse_public_key(pk)
    except Exception:  # noqa: BLE001
        return None
    return parsed["public_key"] if parsed["fingerprint"] == job.ssh_key_fingerprint else None


def consider(job_id: str, *, now: datetime | None = None) -> dict:
    """Decide (at most once) whether failed attempt `job_id` is relaunched. Never raises."""
    now = now or _now()
    if not settings.job_auto_relaunch_enabled:
        return {"job_id": job_id, "decision": "skipped", "reason": "job_auto_relaunch_enabled is off"}
    with normalize.SessionLocal() as s:
        job = s.get(Job, job_id)
        if job is None:
            return {"job_id": job_id, "decision": "skipped", "reason": "no such job"}
        ok, why = trigger(job, _dep(s, job))
    if not ok:
        return {"job_id": job_id, "decision": "skipped", "reason": why}
    row = _claim(job, now)
    if row is None:
        return {"job_id": job_id, "decision": "skipped", "reason": "decided or in progress"}
    tries = int(row.tries or 1)
    ctx: dict = {}
    try:
        return _relaunch(job, ctx)
    except Stop as exc:
        return _stop(job, exc, ctx)
    except Exception as exc:  # noqa: BLE001 - internal error: blocked, retried next pass, ops alerted
        log.exception("job %s: relaunch blocked by an internal error", job_id)
        err = f"{type(exc).__name__}: {exc}"[:500]
        try:
            _decide(job_id, "blocked", code="internal_error", reason=err, detail=S._jsonable(ctx))
        except Exception:  # noqa: BLE001
            log.exception("job %s: could not record the blocked relaunch", job_id)
        _alert_blocked(job, err, tries)
        return {"job_id": job_id, "decision": "blocked", "reason": err}


def _existing_successor(job_id: str) -> Job | None:
    with normalize.SessionLocal() as s:
        return s.scalars(select(Job).where(Job.retry_of == job_id)).first()


def _relaunch(job, ctx: dict) -> dict:
    from fastapi import HTTPException

    from jobs import orchestrator as O
    from routing import idempotency
    from workloads import select as W
    from workloads.spec import WorkloadError

    p = policy(job)
    n, m = attempt_of(job), p["max_attempts"]
    ctx.update(max_attempts=m, max_total_usd=p["max_total_usd"], attempt=n)
    succ = _existing_successor(job.job_id)
    if succ is not None:      # a previous (crashed) pass already created it: record, never a second one
        _decide(job.job_id, "relaunched", code="relaunched", reason=f"successor {succ.job_id} already exists",
                successor_job_id=succ.job_id)
        return {"job_id": job.job_id, "decision": "relaunched", "successor_job_id": succ.job_id, "replayed": True}
    if n >= m:
        raise Stop("max_attempts_reached", f"attempt {n} of {m}: every attempt the retry policy allows has been used")
    if p["max_total_usd"] is None:
        raise Stop("retry_budget_exhausted", "the retry policy has no max_total_usd")
    who = owner(job)
    with normalize.SessionLocal() as s:
        chain = chain_jobs(s, root_of(job))
        root = next((j for j in chain if j.job_id == root_of(job)), job)
        exclude = exclusions(s, chain, root.spec)
        spend = chain_spend(s, chain)
        prev_dep = _dep(s, job)
        prev_provider = _provider_of(s, job)
    ctx.update(excluded_providers=exclude, spend_so_far_usd=spend["usd"], spend=spend)
    remaining = math.floor((p["max_total_usd"] - spend["usd"]) * 100) / 100
    if remaining <= 0:
        raise Stop("retry_budget_exhausted", f"retry budget exhausted: ${spend['usd']:.2f} spent of the "
                                             f"${p['max_total_usd']:.2f} ceiling")
    epochs = int((root.config or {}).get("epochs") or 1)
    resume, rnotes = pick_resume(job, chain, epochs)
    ctx.update(resume_from=resume, resume_notes=rnotes)
    body = _body(root.spec, exclude=exclude, resume=resume, max_total=remaining,
                 ssh_public_key=_customer_key(job, prev_dep))
    # The estimate for the next attempt: the same workload-aware selection submit() runs (no provider call).
    try:
        sel = W.select(body, who)
    except WorkloadError as exc:
        raise Stop("retry_budget_exhausted" if exc.code == "limit_out_of_bounds" else exc.code,
                   f"the next attempt's request is refused: {exc.message}") from None
    ready = sel.get("execution_ready")
    if not ready:
        by = sel.get("exclusions_by_code") or {}
        if by.get("over_budget") and sel.get("market_best") is None:
            raise Stop("retry_budget_exhausted", f"retry budget exhausted: no eligible GPU fits the remaining "
                                                 f"${remaining:.2f} (spent ${spend['usd']:.2f} of "
                                                 f"${p['max_total_usd']:.2f})", exclusions_by_code=by)
        r = O.refusal_for(sel)
        raise Stop("no_eligible_provider", f"{STOP_TEXT['no_eligible_provider']} ({', '.join(exclude) or 'none'} "
                                           f"excluded): {r.message}"[:2000], refusal_code=r.code,
                   exclusions_by_code=by)
    est = float((ready.get("cost") or {}).get("est_max_total") or 0)
    ctx.update(est_next_usd=est, next_provider=ready.get("provider"))
    if spend["usd"] + est > p["max_total_usd"] + 1e-9:
        raise Stop("retry_budget_exhausted", f"retry budget exhausted: ${spend['usd']:.2f} spent + up to ${est:.2f} "
                                             f"for the next attempt > ${p['max_total_usd']:.2f}")
    cause = CAUSE.get(job.failure_code, job.failure_code)
    from_txt = (f"resuming from epoch {resume['epoch']} of {resume['job_id']}" if resume else
                "starting from scratch (no stored checkpoint to resume from)")
    chain_info = {"retry_of": job.job_id, "root_job_id": root_of(job), "attempt": n + 1, "max_attempts": m,
                  "reason": f"attempt {n + 1} of {m}, relaunched after {job.failure_code} on {prev_provider or '?'} "
                            f"({cause}); {from_txt}"}

    def run():
        try:
            code, out = O.submit(body, who, chain=chain_info)
        except O.JobRefused as r:
            raise HTTPException(r.status, r.detail()) from None
        return code, {"job_id": out["job_id"], "status": out["status"],
                      "provider": (out.get("selection") or {}).get("provider")}

    def made(row) -> list[str]:
        s2 = _existing_successor(job.job_id)
        return [s2.job_id] if s2 is not None else []

    try:
        code, payload, replayed = idempotency.run(who=who, scope=IDEM_SCOPE, key=f"relaunch:{job.job_id}",
                                                  body={"relaunch_of": job.job_id}, fn=run, resource_of=lambda x:
                                                  x.get("job_id"), reclaim_check=made)
    except HTTPException as exc:
        d = exc.detail if isinstance(exc.detail, dict) else {"code": "route_refused", "message": str(exc.detail)}
        if d.get("code") in ("idempotency_in_progress", "idempotency_outcome_unknown"):
            succ = _existing_successor(job.job_id)
            if succ is None:
                raise RuntimeError(f"relaunch submission state unknown ({d.get('code')})") from None
            _decide(job.job_id, "relaunched", code="relaunched", reason=f"successor {succ.job_id}",
                    successor_job_id=succ.job_id)
            return {"job_id": job.job_id, "decision": "relaunched", "successor_job_id": succ.job_id, "replayed": True}
        if d.get("code") == "relaunch_exists":
            succ = _existing_successor(job.job_id)
            _decide(job.job_id, "relaunched", code="relaunched", reason="successor already exists",
                    successor_job_id=succ.job_id if succ else None)
            return {"job_id": job.job_id, "decision": "relaunched",
                    "successor_job_id": succ.job_id if succ else None, "replayed": True}
        raise Stop(d.get("code") or "route_refused", f"the relaunch submission was refused: "
                                                      f"{d.get('code')}: {d.get('message')}"[:2000],
                   refused_job_id=d.get("job_id")) from None
    if replayed and (payload or {}).get("detail"):
        d = payload["detail"]
        raise Stop(d.get("code") or "route_refused", f"the relaunch submission was refused: {d.get('message')}",
                   refused_job_id=d.get("job_id"))
    data = (payload or {}).get("data", payload) or {}
    new_id = data.get("job_id")
    new_provider = data.get("provider") or ready.get("provider")
    _decide(job.job_id, "relaunched", code="relaunched",
            reason=f"relaunched as {new_id} on {new_provider}; {from_txt}", successor_job_id=new_id,
            excluded_providers=exclude, resume_from=S._jsonable(resume), spend_so_far_usd=_dec(spend["usd"]),
            max_total_usd=_dec(p["max_total_usd"]), est_next_usd=_dec(est),
            detail=S._jsonable({"spend": spend, "resume_notes": rnotes, "http_code": code,
                                "successor_status": data.get("status")}))
    with normalize.SessionLocal.begin() as s:
        j = s.get(Job, job.job_id, with_for_update=True)
        S.note(s, j, f"relaunched as {new_id} on {new_provider} " + (f"resuming from epoch {resume['epoch']}" if resume
                                                                      else "starting from scratch (no checkpoint)"),
               {"relaunch_event": "relaunched", "successor_job_id": new_id, "provider": new_provider,
                "attempt": n + 1, "max_attempts": m, "resume_from": resume, "excluded_providers": exclude,
                "spend_so_far_usd": spend["usd"], "max_total_usd": p["max_total_usd"], "est_next_usd": est})
    log.info("job %s: relaunched as %s on %s (attempt %d of %d)", job.job_id, new_id, new_provider, n + 1, m)
    return {"job_id": job.job_id, "decision": "relaunched", "successor_job_id": new_id, "provider": new_provider,
            "status": data.get("status"), "resume_from": resume, "spend_so_far_usd": spend["usd"],
            "est_next_usd": est}


def run_all() -> dict:
    if not settings.job_auto_relaunch_enabled:
        return {"enabled": False}
    out = {"enabled": True, "considered": 0, "relaunched": 0, "stopped": 0, "blocked": 0}
    for jid in candidate_ids():
        r = consider(jid)
        out["considered"] += 1
        if r.get("decision") in ("relaunched", "stopped", "blocked"):
            out[r["decision"]] += 1
    return out


@_register_job("job_relaunch", every_seconds=settings.job_relaunch_interval_seconds, initial_delay_seconds=75)
def _job_relaunch():
    return run_all()


# --------------------------------------------------------------------------
# The public chain block (jobs/models.public(chain=...))
# --------------------------------------------------------------------------

def _row_dict(r: JobRelaunch | None) -> dict | None:
    if r is None:
        return None
    return {"status": r.status, "code": r.reason_code, "reason": r.reason, "next_job_id": r.successor_job_id,
            "excluded_providers": r.excluded_providers, "resume_from": r.resume_from,
            "spend_so_far_usd": _f(r.spend_so_far_usd), "max_total_usd": _f(r.max_total_usd),
            "est_next_usd": _f(r.est_next_usd), "decided_at": r.decided_at.isoformat() if r.decided_at else None}


def chain_infos(jobs) -> dict:
    """job_id -> the chain block, for the jobs that have a retry policy or belong to a chain. One query per kind."""
    jobs = [j for j in jobs if j is not None and (policy(j) or j.retry_of or j.root_job_id)]
    if not jobs:
        return {}
    ids = [j.job_id for j in jobs]
    prev_ids = [j.retry_of for j in jobs if j.retry_of]
    from store.routing import Deployment
    with normalize.SessionLocal() as s:
        rows = {r.failed_job_id: r for r in s.scalars(select(JobRelaunch).where(
            JobRelaunch.failed_job_id.in_(ids + prev_ids)))}
        succ = {j.retry_of: j.job_id for j in s.scalars(select(Job).where(Job.retry_of.in_(ids)))}
        prevs = {j.job_id: j for j in s.scalars(select(Job).where(Job.job_id.in_(prev_ids)))} if prev_ids else {}
        dep_ids = [j.deployment_id for j in list(jobs) + list(prevs.values()) if j.deployment_id]
        deps = {d.deployment_id: d for d in s.scalars(select(Deployment).where(
            Deployment.deployment_id.in_(dep_ids)))} if dep_ids else {}
    on = bool(settings.job_auto_relaunch_enabled)
    out = {}
    for j in jobs:
        p = policy(j) or {"max_attempts": 1, "max_total_usd": None}
        n = attempt_of(j)
        mine = rows.get(j.job_id)
        block = {"attempt": n, "max_attempts": p["max_attempts"], "max_total_usd": p["max_total_usd"],
                 "root_job_id": root_of(j), "previous_job_id": j.retry_of,
                 "next_job_id": (mine.successor_job_id if mine is not None and mine.status == "relaunched" else None)
                 or succ.get(j.job_id),
                 "auto_relaunch_enabled": on, "simulated": bool(j.sandbox), "relaunched_after": None,
                 "relaunch": _row_dict(mine), "stopped": None}
        if mine is not None and mine.status == "stopped":
            block["stopped"] = {"code": mine.reason_code, "reason": mine.reason}
        if j.retry_of:
            pr, prow = prevs.get(j.retry_of), rows.get(j.retry_of)
            pd = deps.get(pr.deployment_id) if pr is not None and pr.deployment_id else None
            rf = (prow.resume_from if prow is not None else None) or (j.spec or {}).get("resume_from") or None
            block["relaunched_after"] = {
                "previous_job_id": j.retry_of, "code": pr.failure_code if pr is not None else None,
                "cause": CAUSE.get(pr.failure_code) if pr is not None else None,
                "provider": (pd.provider if pd is not None else None) or ((pr.selection or {}).get("provider")
                                                                          if pr is not None else None),
                "resume_from_epoch": (rf or {}).get("epoch"), "resume_from_job_id": (rf or {}).get("job_id"),
                "from_scratch": not rf, "spend_before_usd": _f(prow.spend_so_far_usd) if prow is not None else None}
            block["spend_so_far_usd"] = block["relaunched_after"]["spend_before_usd"]
        if mine is not None and mine.spend_so_far_usd is not None:
            block["spend_so_far_usd"] = _f(mine.spend_so_far_usd)
        if mine is None and not j.sandbox and j.failure_code in TRIGGER_CODES and p["max_attempts"] > 1 \
                and j.cancel_requested_at is None:
            d = deps.get(j.deployment_id) if j.deployment_id else None
            if n >= p["max_attempts"]:
                waiting = "every attempt the retry policy allows has been used"
            elif not on:
                waiting = "automatic relaunch is switched off on this OpenGrid"
            elif not provider_confirmed_gone(j, d):
                waiting = "waiting for the provider to confirm the GPU is gone before relaunching"
            else:
                waiting = "a relaunch is due: OpenGrid's next pass submits it"
            block["relaunch"] = {"status": "pending" if on and n < p["max_attempts"] else "not_relaunched",
                                 "code": None, "reason": waiting}
        out[j.job_id] = block
    return out
