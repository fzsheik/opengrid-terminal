"""The job state machine (methodology/jobs.md). A job is the user's workload; a deployment is the cloud
resource. Both have their own state and both are shown; this module never touches a deployment.

Happy path
    created -> validating -> quoted -> pending_approval -> approved -> provisioning -> initializing -> training
            -> saving_artifacts -> training_completed -> terminating -> termination_confirmed
            -> reconciliation_pending -> reconciled
Sandbox (SIMULATED, no provider):  created -> validating -> initializing -> training -> saving_artifacts
            -> training_completed  (outcome 'simulated'; finished there: nothing existed to terminate)
Failure / uncertain
    validation_failed, no_eligible_provider, quote_expired, approval_rejected      (before any launch)
    provider_timeout, provisioning_uncertain                                       (launch outcome unknown)
    bootstrap_failed, training_failed, out_of_memory, training_timed_out, artifact_upload_failed
                                                                                   (workload failed: the GPU is
                                                                                    still terminated afterwards)
    cancel_requested, cancelling                                                   (customer cancel after launch)
    termination_failed, orphan_suspected, credentials_unavailable                  (possibly still billing)

Rules
    * transition() is the ONLY writer of jobs.status; it checks ALLOWED and writes a job_events row with actor,
      reason and evidence. Moving to the current state is a no-op (idempotent; returns False).
    * The OUTCOME (succeeded | failed | cancelled | simulated) is a separate column, set once; the job keeps
      moving (terminating -> ... -> reconciled) after a failure so the resource disposition is always proven.
    * FINISHED states end the job (finished_at set, runner tokens revoked). A failed workload is never
      finished while its GPU may still exist: training_failed -> terminating -> ... -> reconciled.
"""

from __future__ import annotations

from datetime import datetime, timezone

STATES = (
    "created", "validating", "quoted", "pending_approval", "approved", "provisioning", "initializing", "training",
    "saving_artifacts", "training_completed", "terminating", "termination_confirmed", "reconciliation_pending",
    "reconciled",
    "validation_failed", "no_eligible_provider", "quote_expired", "approval_rejected", "provider_timeout",
    "provisioning_uncertain", "bootstrap_failed", "training_failed", "out_of_memory", "cancel_requested",
    "cancelling", "training_timed_out", "artifact_upload_failed", "termination_failed", "orphan_suspected",
    "credentials_unavailable",
)
OUTCOMES = ("succeeded", "failed", "cancelled", "simulated")
ACTORS = ("user", "admin", "system", "runner", "reconciler")

# Before a launch: nothing exists at a provider.
PRE_LAUNCH = ("created", "validating", "quoted", "pending_approval", "approved", "quote_expired")
# The workload failed or was stopped while a GPU may exist: the next step is always termination.
WORKLOAD_FAILED = ("bootstrap_failed", "training_failed", "out_of_memory", "training_timed_out",
                   "artifact_upload_failed")
# A resource may exist and OpenGrid cannot (yet) prove its disposition.
UNCERTAIN = ("provider_timeout", "provisioning_uncertain", "termination_failed", "orphan_suspected",
             "credentials_unavailable")
# The runner is (or may be) running on the GPU.
RUNNING = ("provisioning", "initializing", "training", "saving_artifacts")
# The job is over: outcome set, disposition settled (never launched, or reconciled).
FINISHED = ("reconciled", "validation_failed", "no_eligible_provider", "approval_rejected")
# Sandbox jobs finish where the simulation ends (no resource ever existed).
SANDBOX_FINISHED = ("training_completed", "validation_failed") + WORKLOAD_FAILED

_RESOURCE_END = ("terminating", "termination_confirmed", "termination_failed", "orphan_suspected",
                 "credentials_unavailable")
_LIVE_ANY = ("cancel_requested", "terminating", "termination_confirmed", "termination_failed", "orphan_suspected",
             "credentials_unavailable", "training_timed_out")

ALLOWED: dict[str, tuple[str, ...]] = {
    "created": ("validating", "validation_failed", "no_eligible_provider"),
    "validating": ("quoted", "validation_failed", "no_eligible_provider", "initializing"),   # initializing: sandbox
    "quoted": ("pending_approval", "approved", "provisioning", "approval_rejected", "quote_expired",
               "no_eligible_provider", "provider_timeout", "provisioning_uncertain"),
    "pending_approval": ("approved", "provisioning", "approval_rejected", "quote_expired", "no_eligible_provider",
                         "provider_timeout", "provisioning_uncertain"),
    "quote_expired": ("pending_approval", "approved", "provisioning", "approval_rejected", "no_eligible_provider"),
    "approved": ("provisioning", "pending_approval", "quote_expired", "approval_rejected", "no_eligible_provider",
                 "provider_timeout", "provisioning_uncertain", "cancel_requested"),
    "provisioning": ("initializing", "provider_timeout", "provisioning_uncertain", "no_eligible_provider",
                     "bootstrap_failed") + _LIVE_ANY,
    "provider_timeout": ("provisioning", "provisioning_uncertain", "initializing", "no_eligible_provider",
                         "bootstrap_failed") + _LIVE_ANY,
    "provisioning_uncertain": ("provisioning", "provider_timeout", "initializing", "no_eligible_provider",
                               "bootstrap_failed") + _LIVE_ANY,
    "initializing": ("training", "saving_artifacts", "training_completed", "bootstrap_failed", "training_failed",
                     "out_of_memory", "artifact_upload_failed") + _LIVE_ANY,
    "training": ("saving_artifacts", "training_completed", "training_failed", "out_of_memory",
                 "artifact_upload_failed", "bootstrap_failed") + _LIVE_ANY,
    "saving_artifacts": ("training_completed", "artifact_upload_failed", "training_failed", "out_of_memory")
                        + _LIVE_ANY,
    "training_completed": _LIVE_ANY,
    "bootstrap_failed": _LIVE_ANY,
    "training_failed": _LIVE_ANY,
    "out_of_memory": _LIVE_ANY,
    "training_timed_out": ("cancel_requested",) + _RESOURCE_END,
    "artifact_upload_failed": _LIVE_ANY,
    "cancel_requested": ("cancelling", "approval_rejected") + _RESOURCE_END,
    "cancelling": _RESOURCE_END,
    "terminating": ("termination_confirmed", "termination_failed", "orphan_suspected", "credentials_unavailable"),
    "termination_failed": ("terminating", "termination_confirmed", "orphan_suspected", "credentials_unavailable"),
    "orphan_suspected": ("terminating", "termination_confirmed", "termination_failed", "credentials_unavailable"),
    "credentials_unavailable": ("terminating", "termination_confirmed", "termination_failed", "orphan_suspected",
                                "provisioning", "initializing", "training", "saving_artifacts"),
    "termination_confirmed": ("reconciliation_pending", "reconciled"),
    "reconciliation_pending": ("reconciled",),
    "reconciled": (),
    "validation_failed": (),
    "no_eligible_provider": (),
    "approval_rejected": (),
}
assert set(ALLOWED) == set(STATES), set(STATES) ^ set(ALLOWED)
assert all(t in STATES for v in ALLOWED.values() for t in v)

# Phase order of the runner-driven states: an out-of-order (late) phase never moves a job backwards.
PHASE_ORDER = {"provisioning": 0, "initializing": 1, "training": 2, "saving_artifacts": 3, "training_completed": 4}


class IllegalJobTransition(ValueError):
    def __init__(self, frm: str, to: str, job_id: str | None = None):
        super().__init__(f"illegal job transition {frm} -> {to}" + (f" ({job_id})" if job_id else ""))
        self.frm, self.to = frm, to


def can(frm: str, to: str) -> bool:
    return to in ALLOWED.get(frm, ())


def is_finished(job) -> bool:
    if job.finished_at is not None:
        return True
    return job.status in (SANDBOX_FINISHED if job.sandbox else FINISHED)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _jsonable(v):
    from decimal import Decimal
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return v


def transition(s, job, to: str, *, actor: str, reason: str | None = None, evidence: dict | None = None,
               actor_id: str | None = None, outcome: str | None = None, failure_code: str | None = None,
               now: datetime | None = None) -> bool:
    """Move `job` (a store.jobs.Job held FOR UPDATE in session `s`) to `to`. Raises IllegalJobTransition.
    False (no event) when it is already there. outcome / failure_code are recorded only if not set yet."""
    from store.jobs import JobEvent

    if to not in STATES:
        raise ValueError(f"unknown job state {to!r}")
    if actor not in ACTORS:
        raise ValueError(f"actor must be one of {ACTORS}")
    now = now or _now()
    prev = job.status
    if prev == to:
        return False
    if not can(prev, to):
        raise IllegalJobTransition(prev, to, job.job_id)
    job.status = to
    job.state_changed_at = job.updated_at = now
    if outcome is not None and job.outcome is None:
        if outcome not in OUTCOMES:
            raise ValueError(f"outcome must be one of {OUTCOMES}")
        job.outcome = outcome
    if failure_code and job.failure_code is None:
        job.failure_code = failure_code[:48]
        job.failure_reason = (reason or "")[:4000] or None
    if to in ("initializing", "training") and job.started_at is None:
        job.started_at = now
    if to in (SANDBOX_FINISHED if job.sandbox else FINISHED) and job.finished_at is None:
        job.finished_at = now
    s.add(JobEvent(job_id=job.job_id, at=now, from_status=prev, to_status=to, actor=actor, actor_id=actor_id,
                   reason=(reason or "")[:4000] or None, evidence=_jsonable(evidence) if evidence else None))
    return True


def note(s, job, reason: str, evidence: dict | None = None, *, actor: str = "system",
         actor_id: str | None = None) -> None:
    """An auditable event that does not change the state."""
    from store.jobs import JobEvent

    s.add(JobEvent(job_id=job.job_id, at=_now(), from_status=job.status, to_status=job.status, actor=actor,
                   actor_id=actor_id, reason=reason[:4000], evidence=_jsonable(evidence) if evidence else None))


def path_to(frm: str, to: str) -> list[str]:
    """The shortest legal path frm -> to (BFS), [] when unreachable. Used by job_sync to catch up after a
    restart (e.g. pending_approval -> provisioning when the approval happened while no worker watched)."""
    if frm == to:
        return []
    seen, frontier = {frm: None}, [frm]
    while frontier:
        nxt = []
        for a in frontier:
            for b in ALLOWED.get(a, ()):
                if b in seen:
                    continue
                seen[b] = a
                if b == to:
                    out = [b]
                    while seen[out[-1]] != frm:
                        out.append(seen[out[-1]])
                    return list(reversed(out))
                nxt.append(b)
        frontier = nxt
    return []
