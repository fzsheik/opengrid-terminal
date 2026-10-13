"""The public job shape and the 12 verification checks (methodology/jobs.md).

A job response always shows BOTH states: the job's own (`status`, `outcome`) and the deployment's
(`resource.deployment_status`, `resource.disposition`). A failed job never implies the GPU stopped billing:
`resource.disposition` says 'possibly_billing' until the provider confirmed termination.

The 12 checks (founder): every one carries {status, source, evidence, at}. status is
    pending   no evidence yet
    pass      evidence from the named source (provider API / runner on the GPU / OpenGrid's own records)
    fail      contrary evidence
    simulated sandbox run: nothing real happened (never 'pass')
"""

from __future__ import annotations

from datetime import datetime, timezone

CHECKS = (
    ("instance_created", "GPU instance created", "provider"),
    ("runtime_initialized", "Runtime initialized on the instance", "runner"),
    ("torch_installed", "PyTorch installed", "runner"),
    ("gpu_detected", "GPU detected", "runner"),
    ("cuda_available", "CUDA available to PyTorch", "runner"),
    ("training_ran", "Training ran", "runner"),
    ("params_updated", "Model parameters updated", "runner"),
    ("real_metrics", "Real loss / accuracy reported", "runner"),
    ("weights_saved", "Weights saved (model.pt)", "runner"),
    ("artifacts_persisted", "Artifacts persisted in OpenGrid (sha256 verified)", "opengrid"),
    ("gpu_terminated", "GPU termination requested and accepted", "provider"),
    ("termination_verified", "Termination verified with the provider", "provider"),
)
CHECK_KEYS = tuple(k for k, _, _ in CHECKS)
STATUSES = ("pending", "pass", "fail", "simulated")

DISPOSITIONS = {
    "none": "no provider resource was ever created",
    "not_launched_yet": "nothing launched yet (waiting for approval / launch)",
    "possibly_billing": "a provider resource may exist and may be billing: OpenGrid is tracking it",
    "terminated_confirmed": "the provider confirmed the instance is gone",
    "simulated_none": "sandbox: no provider was called, nothing was created",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(t):
    return None if t is None else t.isoformat()


def blank_evidence(simulated: bool = False) -> dict:
    return {k: {"status": "pending", "source": None, "evidence": None, "at": None} for k in CHECK_KEYS}


def set_check(job, key: str, status: str, evidence: dict | None, source: str, *, overwrite: bool = False) -> bool:
    """Record a check result on job.evidence (reassigned so the JSONB change is seen). A 'pass' or 'fail' is not
    overwritten unless `overwrite` (later contrary evidence, e.g. a fail after pass, is passed explicitly)."""
    assert key in CHECK_KEYS, key
    assert status in STATUSES, status
    ev = dict(job.evidence or blank_evidence())
    cur = ev.get(key) or {}
    if cur.get("status") in ("pass", "fail", "simulated") and not overwrite:
        if cur.get("status") == status:
            return False
        if not (cur.get("status") == "pass" and status == "fail"):
            return False
    ev[key] = {"status": status, "source": source, "evidence": evidence, "at": _now().isoformat()}
    job.evidence = ev
    return True


def checks_list(job) -> list[dict]:
    ev = job.evidence or blank_evidence()
    out = []
    for key, label, src in CHECKS:
        c = ev.get(key) or {}
        out.append({"key": key, "label": label, "status": c.get("status") or "pending",
                    "source": c.get("source") or None, "expected_source": src,
                    "evidence": c.get("evidence"), "at": c.get("at")})
    return out


def disposition(job, dep) -> str:
    from routing import deployments as D

    if job.sandbox:
        return "simulated_none"
    if dep is None:
        return "none" if job.finished_at is not None else "not_launched_yet"
    if dep.status == "terminated":
        return "terminated_confirmed"
    if dep.status in D.LIVE_STATES:
        return "possibly_billing"
    if dep.status in D.PRE_LAUNCH_STATES and dep.launch_token is None:
        return "not_launched_yet"
    return "none"   # rejected / provision_failed / provider_rejected / quote_failed: nothing created


def resource_block(job, dep) -> dict:
    disp = disposition(job, dep)
    out = {"deployment_id": job.deployment_id, "route_request_id": job.route_request_id, "quote_id": job.quote_id,
           "deployment_status": None if dep is None else dep.status, "disposition": disp,
           "disposition_note": DISPOSITIONS[disp], "possibly_billing": disp == "possibly_billing"}
    if dep is not None:
        out.update(provider=dep.provider, gpu=dep.gpu, gpu_count=dep.gpu_count, region=dep.region,
                   provider_instance_id=dep.provider_instance_id,
                   terminate_deadline_at=_iso(dep.terminate_deadline_at),
                   terminate_requested_at=_iso(dep.terminate_requested_at), terminated_at=_iso(dep.terminated_at),
                   reconciled_at=_iso(dep.reconciled_at),
                   link=f"/v1/deployments/{dep.deployment_id}")
        if dep.status in ("termination_failed", "orphan_suspected", "credentials_unavailable", "terminating"):
            out["termination_unconfirmed"] = True
            out["termination_note"] = ("termination is NOT confirmed by the provider yet: the instance may still be "
                                       "billing; OpenGrid keeps retrying and the operator is alerted")
    return out


def approval_block(job, dep, quote: dict | None) -> dict | None:
    if dep is None or job.route_request_id is None or dep.status not in ("pending_approval", "quote_expired"):
        return None
    q = quote or {}
    return {"required": True, "code": "approval_required",
            "message": "an OpenGrid admin must approve this launch (SUPERVISED execution); the job waits in "
                       "pending_approval and nothing is billed until then",
            "approve": f"POST /v1/route/{job.route_request_id}/approve",
            "reject": f"POST /v1/route/{job.route_request_id}/reject",
            "body": {"quote_id": dep.quote_id}, "quote_id": dep.quote_id,
            "quote_price_per_gpu_hour": q.get("quote_price_per_gpu_hour"), "est_total_cost": q.get("est_total_cost"),
            "quote_expires_at": q.get("expires_at"), "quote_status": q.get("status"),
            "limit_violations": dep.limit_violations or []}


def public(job, dep=None, *, quote: dict | None = None, detail: bool = False, events=None, artifacts=None,
           metrics_latest=None) -> dict:
    """The job as the API returns it (no key material, no token, no bootstrap script)."""
    from jobs import states

    spec = dict(job.spec or {})
    spec.pop("ssh_public_key", None)
    simulated = bool(job.sandbox)
    sel = job.selection or {}
    out = {
        "job_id": job.job_id, "name": job.name, "job_type": job.job_type, "template": job.template,
        "template_version": job.template_version, "status": job.status, "outcome": job.outcome,
        "finished": states.is_finished(job), "sandbox": simulated, "simulated": simulated,
        "simulated_note": ("SIMULATED: sandbox run; no provider was called, no GPU existed, every event, metric and "
                           "artifact is labelled simulated") if simulated else None,
        "runner_phase": job.runner_phase,
        "config": job.config, "request": spec,
        "resource": resource_block(job, dep),
        "estimates": {"kind": "estimated", "est_max_total_usd": None if job.est_max_total_usd is None
                      else float(job.est_max_total_usd),
                      "est_typical_total_usd": None if job.est_typical_total_usd is None
                      else float(job.est_typical_total_usd),
                      "runtime_ceiling_minutes": job.runtime_ceiling_minutes},
        "selection": {"provider": sel.get("provider"), "gpu": sel.get("gpu"), "listing_id": sel.get("listing_id"),
                      "region": sel.get("region"), "price_per_gpu_hour": sel.get("price_per_gpu_hour"),
                      "price_kind": sel.get("price_kind"), "why": sel.get("why"),
                      "market_best": sel.get("market_best"), "execution_mode": sel.get("execution_mode")},
        "approval": approval_block(job, dep, quote),
        "ssh": {"mode": job.ssh_key_mode, "fingerprint": job.ssh_key_fingerprint,
                "note": ("ephemeral key: the private half was discarded; nobody (including OpenGrid) can SSH in"
                         if job.ssh_key_mode == "ephemeral" else
                         "your key: only its fingerprint is stored" if job.ssh_key_mode == "customer" else None)},
        "bootstrap": {"sha256": job.bootstrap_sha256, "template_version": job.bootstrap_template_version,
                      "rendered_at": _iso(job.bootstrap_rendered_at),
                      "note": "the rendered startup script is never stored or logged; only its sha256"},
        "checks": checks_list(job),
        "failure": None if not job.failure_code else {"code": job.failure_code, "reason": job.failure_reason},
        "result": job.runner_result,
        "created_at": _iso(job.created_at), "updated_at": _iso(job.updated_at), "started_at": _iso(job.started_at),
        "finished_at": _iso(job.finished_at), "state_changed_at": _iso(job.state_changed_at),
        "cancel_requested_at": _iso(job.cancel_requested_at), "last_runner_event_at": _iso(job.last_runner_event_at),
        "links": {"self": f"/v1/jobs/{job.job_id}", "logs": f"/v1/jobs/{job.job_id}/logs",
                  "metrics": f"/v1/jobs/{job.job_id}/metrics", "artifacts": f"/v1/jobs/{job.job_id}/artifacts",
                  "cancel": f"/v1/jobs/{job.job_id}/cancel",
                  "deployment": f"/v1/deployments/{job.deployment_id}" if job.deployment_id else None,
                  "route": f"/v1/route/{job.route_request_id}" if job.route_request_id else None},
    }
    if detail:
        out["events"] = events or []
        out["artifacts"] = artifacts or []
        out["metrics_latest"] = metrics_latest or {}
    return out
