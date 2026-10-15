"""Read-only job tools for the OpenGrid Assistant (the My Jobs box). Each returns a contract card payload (§5).

    can_read(who)              deployments:read (the operator holds every scope); the PUBLIC principal never does
    can_act(who)               route:execute: only then is a cancel_job ACTION card offered (the browser calls
                               POST /v1/jobs/{id}/cancel after the user confirms; this module never does)
    list_jobs(who, ...)        the caller's own jobs, newest first: jobs.orchestrator.list_for (the SAME query and
                               ownership rule as GET /v1/jobs: a key sees its account's jobs, the operator all)
    get_job(who, job_id)       one job through jobs.orchestrator._load (the same 404 rule as GET /v1/jobs/{id}:
                               another account's job and a missing job look identical -> None)
    item(public_job, ...)      the assistant's job item: plain-language status_text derived from the real state

Nothing here writes: no sync on read (GET /v1/jobs/{id} runs sync_job first, which can transition a job; the
assistant does not), no cancel, no submit, no commit. Sessions are plain reads that are rolled back on close.
"""

from __future__ import annotations

import re

from jobs import models as M
from jobs import orchestrator as O
from jobs import states as S

MAX_ITEMS = 10
JOB_ID_RE = re.compile(r"^job-[0-9a-f]{8,32}\Z")    # api/jobs.py _JOB

# states in which a cancel is already under way or the GPU is being shut down (web/pages/job.js canCancel)
_ENDING = ("cancel_requested", "cancelling", "terminating", "termination_confirmed", "reconciliation_pending",
           "reconciled")

FAILURE_TEXT = {
    "out_of_memory": "out of memory",
    "training_timed_out": "reached its runtime limit",
    "training_failed": "the training script reported an error",
    "bootstrap_failed": "setting up the GPU environment failed",
    "bootstrap_timeout": "setting up the GPU environment took too long",
    "cuda_init_failed": "CUDA could not start on the GPU",
    "artifact_upload_failed": "the results could not be saved",
    "approval_rejected": "the launch was not approved",
    "gpu_unavailable": "the GPU was no longer available",
    "provider_rejected": "the provider refused the launch",
    "terminated_before_completion": "the GPU stopped before training completed",
    "no_eligible_provider": "no eligible GPU was available",
    "validation_failed": "the request was not valid",
}

_STATE_TEXT = {
    "created": "Submitted, being checked",
    "validating": "Submitted, being checked",
    "quoted": "Priced, waiting to be sent for approval",
    "pending_approval": "Waiting for OpenGrid approval",
    "approved": "Approved, waiting to launch",
    "quote_expired": "Price quote expired: it needs a new quote before it can launch",
    "provisioning": "Starting a GPU",
    "initializing": "Setting up the GPU environment",
    "saving_artifacts": "Saving results",
    "training_completed": "Training finished, shutting down the GPU",
    "terminating": "Shutting down the GPU",
    "termination_confirmed": "GPU shut down, settling the cost",
    "reconciliation_pending": "GPU shut down, settling the cost",
    "cancel_requested": "Cancelling: shutting down the GPU",
    "cancelling": "Cancelling: shutting down the GPU",
    "provider_timeout": "Launch outcome unknown: OpenGrid is checking with the provider",
    "provisioning_uncertain": "Launch outcome unknown: OpenGrid is checking with the provider",
    "termination_failed": "GPU shutdown not confirmed yet: it may still be billing; OpenGrid keeps retrying",
    "orphan_suspected": "GPU shutdown not confirmed yet: it may still be billing; OpenGrid keeps retrying",
    "credentials_unavailable": "GPU shutdown not confirmed yet: it may still be billing; OpenGrid keeps retrying",
}


def can_read(who) -> bool:
    return who is not None and who.kind != "public" and who.has("deployments:read")


def can_act(who) -> bool:
    return can_read(who) and who.has("route:execute")


def valid_id(job_id) -> bool:
    return isinstance(job_id, str) and bool(JOB_ID_RE.match(job_id))


def _failure(pub: dict) -> str:
    code = (pub.get("failure") or {}).get("code") or pub.get("status")
    return FAILURE_TEXT.get(code) or (code or "unknown reason").replace("_", " ")


def status_text(pub: dict, progress: dict | None) -> str:
    """Plain language from the recorded state, outcome and failure code; never more than they say."""
    st, outcome, sandbox = pub.get("status"), pub.get("outcome"), bool(pub.get("sandbox"))
    finished = bool(pub.get("finished"))
    ep = ""
    if progress and progress.get("epochs"):
        ep = (f", epoch {progress['epoch']} of {progress['epochs']}" if progress.get("epoch")
              else f", starting epoch 1 of {progress['epochs']}")
    if sandbox:
        if st == "training_completed":
            return "Simulated run finished (no GPU was used)"
        if finished or st in S.WORKLOAD_FAILED:
            return f"Simulated run failed: {_failure(pub)}"
        if st == "training":
            return "Simulated run in progress" + ep
        return "Simulated run starting"
    if st == "training":
        return "Running" + ep
    if st in S.WORKLOAD_FAILED:
        return f"Failed: {_failure(pub)} (the GPU is being shut down)"
    if st == "reconciled":
        if outcome == "succeeded":
            return "Finished"
        if outcome == "cancelled":
            return "Cancelled (the GPU was shut down)"
        return f"Failed: {_failure(pub)}"
    if st == "approval_rejected":
        return "Cancelled before launch (nothing launched, nothing billed)" if outcome == "cancelled" \
            else "Not approved (nothing launched, nothing billed)"
    if st in ("validation_failed", "no_eligible_provider"):
        return f"Failed before launch: {_failure(pub)} (nothing billed)"
    if st == "training_completed" and outcome == "cancelled":
        return "Cancelling: shutting down the GPU"
    if st == "terminating" and outcome == "cancelled":
        return "Cancelling: shutting down the GPU"
    if st in ("termination_confirmed", "reconciliation_pending") and outcome == "cancelled":
        return "Cancelled: GPU shut down, settling the cost"
    return _STATE_TEXT.get(st) or (st or "unknown").replace("_", " ")


def cancellable(pub: dict) -> bool:
    """A real job that is not over and not already being cancelled / shut down. A sandbox run has nothing to
    cancel (jobs.orchestrator.cancel answers 'nothing to cancel')."""
    return not pub.get("sandbox") and not pub.get("finished") and pub.get("status") not in _ENDING \
        and pub.get("outcome") != "cancelled"


def pre_launch(pub: dict) -> bool:
    return pub.get("status") in S.PRE_LAUNCH


def _progress(pub: dict, last_epoch) -> dict | None:
    epochs = (pub.get("config") or {}).get("epochs")
    if not isinstance(epochs, int) or isinstance(epochs, bool) or epochs < 1:
        return None
    if last_epoch is None and pub.get("status") not in ("training", "saving_artifacts"):
        return None
    return {"epoch": int(last_epoch or 0), "epochs": epochs}


def item(pub: dict, *, last_epoch=None, actual_usd=None) -> dict:
    progress = _progress(pub, last_epoch)
    est = (pub.get("estimates") or {}).get("est_max_total_usd")
    actual = None if pub.get("sandbox") else actual_usd
    return {"job_id": pub["job_id"], "name": pub.get("name"), "template": pub.get("template"),
            "status": pub.get("status"), "status_text": status_text(pub, progress), "sandbox": bool(pub.get("sandbox")),
            "outcome": pub.get("outcome"), "progress": progress,
            "cost": None if est is None and actual is None else {"est_max_usd": est, "actual_usd": actual},
            "created_at": pub.get("created_at"), "href": f"/jobs/{pub['job_id']}", "cancellable": cancellable(pub)}


def _extras(pubs: list[dict]) -> tuple[dict, dict]:
    """{job_id: last reported epoch} and {job_id: reconciled transaction cost} for these (already owned) jobs."""
    from sqlalchemy import func, select

    import normalize
    from store.jobs import JobMetric
    from store.routing import Deployment

    ids = [p["job_id"] for p in pubs]
    deps = {p["resource"]["deployment_id"]: p["job_id"] for p in pubs
            if (p.get("resource") or {}).get("deployment_id") and not p.get("sandbox")}
    epochs, costs = {}, {}
    if not ids:
        return epochs, costs
    with normalize.SessionLocal() as s:
        for jid in ids:   # <= MAX_ITEMS small indexed reads
            epochs[jid] = s.scalar(select(func.max(JobMetric.epoch)).where(JobMetric.job_id == jid))
        if deps:
            for d in s.scalars(select(Deployment).where(Deployment.deployment_id.in_(list(deps)))):
                amt = ((d.reconciliation or {}).get("opengrid_transaction_cost") or {}).get("amount_usd")
                if isinstance(amt, (int, float)) and not isinstance(amt, bool):
                    costs[deps[d.deployment_id]] = float(amt)
    return epochs, costs


def _items(pubs: list[dict]) -> list[dict]:
    epochs, costs = _extras(pubs)
    return [item(p, last_epoch=epochs.get(p["job_id"]), actual_usd=costs.get(p["job_id"])) for p in pubs]


def list_jobs(who, *, limit: int = MAX_ITEMS, open_only: bool = False) -> dict:
    """Card `jobs`: {items, returned, more}. Callers check can_read(who) first."""
    if not can_read(who):
        raise PermissionError("deployments:read required")
    pubs = O.list_for(who, status="open" if open_only else None, limit=limit + 1)
    items = _items(pubs[:limit])
    return {"items": items, "returned": len(items), "more": len(pubs) > limit}


def get_job(who, job_id: str) -> dict | None:
    """Card `job`, or None when the id is malformed, missing or not the caller's (indistinguishable)."""
    if not can_read(who):
        raise PermissionError("deployments:read required")
    if not valid_id(job_id):
        return None
    try:
        job = O._load(job_id, who)
    except O.JobRefused:
        return None
    pub = M.public(job, O._dep(job))
    return _items([pub])[0]
