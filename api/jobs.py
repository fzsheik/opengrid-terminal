"""OpenGrid Jobs API (methodology/jobs.md).

  public (accounts.auth principal; a key sees its own account's jobs, the operator sees all)
    GET  /v1/jobs/templates                     the allowlisted templates                         [data:read]
    GET  /v1/jobs/readiness                     what blocks a REAL job run now, item by item      [route:preview]
    POST /v1/jobs/preview                       workload-aware selection + what a submit would do [route:preview]
    POST /v1/jobs                       (*)     submit: sandbox (SIMULATED) or a real job -> pending_approval
                                                                                                  [route:execute]
    GET  /v1/jobs                               your jobs (?status=open|<state>)                  [deployments:read]
    GET  /v1/jobs/{job_id}                      one job: job state + deployment state + 12 checks [deployments:read]
    GET  /v1/jobs/{job_id}/logs?after=&limit=   runner log lines, cursor = id                     [deployments:read]
    GET  /v1/jobs/{job_id}/metrics              per-epoch metrics                                 [deployments:read]
    GET  /v1/jobs/{job_id}/artifacts            artifact list (name, size, sha256)                [deployments:read]
    GET  /v1/jobs/{job_id}/artifacts/{name}     download (attachment, X-Artifact-SHA256)          [deployments:read]
    POST /v1/jobs/{job_id}/cancel       (*)     before launch: reject; after: terminate           [route:execute]
  runner callbacks (ONLY the job token: Authorization: Bearer ogj_<job_id>.<token>; never an API key)
    POST /v1/jobs/{job_id}/runner/events        {"events": [{seq, type, at, data}, ...]}  duplicate seq ignored
    PUT  /v1/jobs/{job_id}/runner/artifacts/{name}   raw bytes, X-Content-SHA256 (or X-Artifact-SHA256); names
                                                ckpt-eNNNN.pt are checkpoints (own size limit, latest-N retention)
    GET  /v1/jobs/{job_id}/runner/resume        the checkpoint this job was created to resume from (404 otherwise)
    POST /v1/jobs/{job_id}/runner/complete      {outcome: succeeded|failed|oom|timeout, summary}
Automatic relaunch (jobs/relaunch.py, background job job_relaunch; off unless JOB_AUTO_RELAUNCH_ENABLED and the job's
retry.max_attempts > 1) submits through the same orchestrator.submit; a job in a chain carries a `chain` block.
(*) Idempotency-Key header REQUIRED (routing/idempotency.py): same key + same body replays; different body 422;
in flight 409 + Retry-After. Approval of a real job is the EXISTING POST /v1/route/{route_request_id}/approve.
Errors: HTTP 4xx/5xx with detail {code, message, ...} (422 for validation / refusals).
"""

from __future__ import annotations

import json
import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response

import jobs.relaunch  # noqa: F401  registers the job_relaunch background job (a no-op unless enabled)
import jobs.sync  # noqa: F401  registers the job_sync background job
from accounts.auth import Principal, require_scope
from api.common import ESTIMATED, INFERRED, TRANSACTION, envelope
from jobs import orchestrator as O
from jobs import tokens
from routing import control, idempotency

control.install_job_heartbeats(names=("job_sync", "job_relaunch"))

router = APIRouter()
READ = require_scope("deployments:read")
MAX_CALLBACK_BODY = 1024 * 1024
_JOB = r"^job-[0-9a-f]{8,32}\Z"   # \Z, not $: a trailing newline never matches
_NAME = r"^[a-z0-9][a-z0-9._-]{0,63}\Z"
METH = "jobs"


def _refused(fn):
    try:
        return fn()
    except O.JobRefused as r:
        raise HTTPException(r.status, r.detail()) from None


def _idem(request: Request, who: Principal, scope: str, body, fn, *, resource_of=None, reclaim_check=None):
    code, payload, replayed = idempotency.run(who=who, scope=scope, key=request.headers.get("Idempotency-Key"),
                                              body=body, fn=fn, resource_of=resource_of, reclaim_check=reclaim_check)
    return JSONResponse(payload, status_code=code, headers={"Idempotent-Replayed": "true"} if replayed else None)


async def _json_body(request: Request, limit: int = 64 * 1024) -> dict:
    ct = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ct != "application/json":
        raise HTTPException(415, {"code": "json_required", "message": "send Content-Type: application/json"})
    raw = await request.body()
    if len(raw) > limit:
        raise HTTPException(413, {"code": "body_too_large", "message": f"request body over {limit} bytes"})
    try:
        body = json.loads(raw or b"null")
    except ValueError:
        raise HTTPException(422, {"code": "invalid_json", "message": "the body is not valid JSON"}) from None
    if not isinstance(body, dict):
        raise HTTPException(422, {"code": "invalid_request", "message": "the job request must be a JSON object"})
    return body


def _jobs_since(who):
    """reclaim_check: a crashed / stale POST /v1/jobs that may already have created a job is never re-run."""
    def check(row) -> list[str]:
        from sqlalchemy import select

        import normalize
        from store.jobs import Job
        acct = Job.account_id.is_(None) if who.account_id is None else Job.account_id == who.account_id
        with normalize.SessionLocal() as s:
            return list(s.scalars(select(Job.job_id).where(acct, Job.created_at >= row.created_at)
                                  .order_by(Job.created_at).limit(20)))
    return check


# --------------------------------------------------------------------------
# Public
# --------------------------------------------------------------------------

@router.get("/v1/jobs/templates", tags=["jobs"], summary="The allowlisted job templates")
def job_templates(who: Principal = Depends(require_scope("data:read"))):
    return envelope(O.templates(), methodology=METH, also=["/methodology/workloads", "/methodology/runner"])


@router.get("/v1/jobs/readiness", tags=["jobs"], summary="What blocks a real job run right now")
def job_readiness(who: Principal = Depends(require_scope("route:preview"))):
    return envelope(O.readiness(who), kind=INFERRED, methodology=METH, also=["/methodology/execution-safety"])


@router.post("/v1/jobs/preview", tags=["jobs"], summary="Preview a job: workload-aware selection, estimate, readiness")
async def job_preview(request: Request, who: Principal = Depends(require_scope("route:preview"))):
    body = await _json_body(request)
    out = _refused(lambda: O.preview(body, who))
    return envelope(out, kind=ESTIMATED, methodology=METH, also=["/methodology/workloads"])


@router.post("/v1/jobs", tags=["jobs"], summary="Submit a job (Idempotency-Key required)")
async def job_submit(request: Request, who: Principal = Depends(require_scope("route:execute"))):
    body = await _json_body(request)

    def run():
        code, out = _refused(lambda: O.submit(body, who))
        return code, envelope(out, kind=TRANSACTION if not out.get("sandbox") else "simulated", methodology=METH,
                              simulated=bool(out.get("sandbox")),
                              also=["/methodology/execution-safety", "/methodology/runner"])

    return _idem(request, who, "jobs", body, run, resource_of=lambda p: (p.get("data") or {}).get("job_id"),
                 reclaim_check=_jobs_since(who))


_STATUS = "^(open|" + "|".join(O.S.STATES) + ")$"


@router.get("/v1/jobs", tags=["jobs"], summary="Your jobs")
def job_list(status: str | None = Query(None, pattern=_STATUS), limit: int = Query(100, ge=1, le=500),
             offset: int = Query(0, ge=0), who: Principal = Depends(READ)):
    return envelope(O.list_for(who, status=status, limit=limit, offset=offset), kind=TRANSACTION, methodology=METH,
                    limit=limit, offset=offset)


@router.get("/v1/jobs/{job_id}", tags=["jobs"], summary="One job: job state, deployment state, verification checks")
def job_get(job_id: str, who: Principal = Depends(READ)):
    _valid_id(job_id)
    out = _refused(lambda: O.get(job_id, who, sync_first=True))
    return envelope(out, kind="simulated" if out.get("sandbox") else TRANSACTION, methodology=METH,
                    simulated=bool(out.get("sandbox")))


@router.get("/v1/jobs/{job_id}/logs", tags=["jobs"], summary="Runner log lines (cursor: ?after=<id>)")
def job_logs(job_id: str, after: int | None = Query(None, ge=0), limit: int = Query(500, ge=1, le=5000),
             who: Principal = Depends(READ)):
    _valid_id(job_id)
    return envelope(_refused(lambda: O.logs(job_id, who, after=after, limit=limit)), methodology=METH)


@router.get("/v1/jobs/{job_id}/metrics", tags=["jobs"], summary="Per-epoch training metrics")
def job_metrics(job_id: str, who: Principal = Depends(READ)):
    _valid_id(job_id)
    return envelope(_refused(lambda: O.metrics(job_id, who)), methodology=METH)


@router.get("/v1/jobs/{job_id}/artifacts", tags=["jobs"], summary="The job's artifacts (name, size, sha256)")
def job_artifacts(job_id: str, who: Principal = Depends(READ)):
    _valid_id(job_id)
    return envelope(_refused(lambda: O.artifacts_list(job_id, who)), methodology=METH)


@router.get("/v1/jobs/{job_id}/artifacts/{name}", tags=["jobs"], summary="Download one artifact")
def job_artifact(job_id: str, name: str, who: Principal = Depends(READ)):
    _valid_id(job_id)
    if not re.match(_NAME, name) or ".." in name:
        raise HTTPException(422, {"code": "artifact_name_invalid", "message": "invalid artifact name"})
    meta, data = _refused(lambda: O.artifact(job_id, name, who))
    return Response(data, media_type="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "X-Artifact-SHA256": meta["sha256"],
                             "X-Content-SHA256": meta["sha256"], "X-Artifact-Source": meta["source"],
                             "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})


@router.post("/v1/jobs/{job_id}/cancel", tags=["jobs"], summary="Cancel a job (Idempotency-Key required)")
def job_cancel(job_id: str, request: Request, who: Principal = Depends(require_scope("route:execute"))):
    _valid_id(job_id)
    _refused(lambda: O._load(job_id, who))   # 404 before claiming a key for someone else's job

    def run():
        code, out = _refused(lambda: O.cancel(job_id, who))
        return code, envelope(out, kind=TRANSACTION, methodology=METH)

    return _idem(request, who, f"job_cancel:{job_id}", {}, run)


def _valid_id(job_id: str) -> None:
    if not re.match(_JOB, job_id or ""):
        raise HTTPException(404, {"code": "job_not_found", "message": "job not found"})


# --------------------------------------------------------------------------
# Runner callbacks: the job token only
# --------------------------------------------------------------------------

async def _runner_body(request: Request, limit: int = MAX_CALLBACK_BODY) -> bytes:
    cl = request.headers.get("content-length")
    if cl is not None and cl.isdigit() and int(cl) > limit:
        raise HTTPException(413, {"code": "body_too_large", "message": f"callback body over {limit} bytes"})
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > limit:
            raise HTTPException(413, {"code": "body_too_large", "message": f"callback body over {limit} bytes"})
    return bytes(buf)


def _runner_auth(request: Request, job_id: str):
    if not re.match(_JOB, job_id or ""):
        raise HTTPException(401, {"code": "runner_token_invalid", "message": "invalid runner token"})
    return tokens.verify(request.headers.get("authorization"), job_id)


def _parse(raw: bytes) -> dict:
    try:
        body = json.loads(raw or b"null")
    except ValueError:
        raise HTTPException(422, {"code": "invalid_json", "message": "the body is not valid JSON"}) from None
    if not isinstance(body, dict):
        raise HTTPException(422, {"code": "invalid_request", "message": "the body must be a JSON object"})
    return body


def _callback_error(fn):
    from jobs.sync import CallbackError
    try:
        return fn()
    except CallbackError as e:
        raise HTTPException(e.status, {"code": e.code, "message": e.message}) from None


@router.post("/v1/jobs/{job_id}/runner/events", tags=["jobs-runner"], include_in_schema=False)
async def runner_events(job_id: str, request: Request):
    auth = _runner_auth(request, job_id)
    body = _parse(await _runner_body(request))
    events = body.get("events")
    if events is None and "seq" in body:
        events = [body]
    out = _callback_error(lambda: jobs.sync.ingest(job_id, events, token_id=auth.token_id))
    try:
        jobs.sync.sync_job(job_id, enforce=True)
    except Exception:  # noqa: BLE001 - the events are recorded; job_sync retries
        import logging
        logging.getLogger(__name__).exception("job %s: sync after runner events failed", job_id)
    return {"ok": True, **out}


@router.put("/v1/jobs/{job_id}/runner/artifacts/{name}", tags=["jobs-runner"], include_in_schema=False)
async def runner_artifact(job_id: str, name: str, request: Request):
    auth = _runner_auth(request, job_id)
    from jobs import artifacts as A

    try:
        A.check_name(name)
    except A.ArtifactError as e:
        raise HTTPException(e.status, {"code": e.code, "message": e.message}) from None
    limit = A.checkpoint_max_bytes() if A.is_checkpoint(name) else A.max_bytes()
    data = await _runner_body(request, limit)
    sha = request.headers.get("x-content-sha256") or request.headers.get("x-artifact-sha256")
    try:
        meta = A.store().put(job_id, name, data, sha, content_type=request.headers.get("content-type"))
    except A.ArtifactError as e:
        raise HTTPException(e.status, {"code": e.code, "message": e.message}) from None
    if not meta.get("replayed") and not meta.get("superseded"):
        jobs.sync.on_artifact(job_id, meta)
    tokens.touch(auth.token_id, None)
    return {"ok": True, "artifact": meta}


@router.get("/v1/jobs/{job_id}/runner/resume", tags=["jobs-runner"], include_in_schema=False)
def runner_resume(job_id: str, request: Request):
    """The checkpoint THIS job was created to resume from (pinned at submission). The job token is the only
    credential; nothing else of the source job is reachable through it."""
    auth = _runner_auth(request, job_id)
    from jobs import artifacts as A

    try:
        meta, data = A.resume_source(job_id)
    except A.ArtifactError as e:
        raise HTTPException(e.status, {"code": e.code, "message": e.message}) from None
    tokens.touch(auth.token_id, None)
    return Response(data, media_type="application/octet-stream",
                    headers={"X-Content-SHA256": meta["sha256"], "X-Artifact-SHA256": meta["sha256"],
                             "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})


@router.post("/v1/jobs/{job_id}/runner/complete", tags=["jobs-runner"], include_in_schema=False)
async def runner_complete(job_id: str, request: Request):
    auth = _runner_auth(request, job_id)
    body = _parse(await _runner_body(request))
    out = _callback_error(lambda: jobs.sync.complete(job_id, body, token_id=auth.token_id))
    tokens.touch(auth.token_id, None)
    return {"ok": True, **out}

