"""OpenGrid batch fan-out API (methodology/batches.md, jobs/batches.py).

    POST /v1/batches/preview            validate + expand shards + the placement plan (never writes)  [route:preview]
    POST /v1/batches            (*)     submit: sandbox (SIMULATED, 201) or real (202): the first wave of ordinary
                                        jobs; the rest queue                                            [route:execute]
    GET  /v1/batches                    your batches (?status=open|active|stopping|cancelling|finished) [deployments:read]
    GET  /v1/batches/{batch_id}         summary, budget, per-provider breakdown, per-shard rows, best result, events
                                        (database only: never a provider call)                         [deployments:read]
    POST /v1/batches/{batch_id}/approve (*)  ONE approval for the batch {quotes?: {shard_index: quote_id}, reason?}:
                                        every waiting shard through the EXISTING engine.approve        [admin]
    POST /v1/batches/{batch_id}/cancel  (*)  queued shards cancelled, submitted ones through the jobs cancel path
                                                                                                        [route:execute]
(*) Idempotency-Key header REQUIRED (routing/idempotency.py): same key + same body replays; different body 422; in
flight 409. Keys see their own account's batches; the operator (and an admin approving) sees all.
Errors: HTTP 4xx/5xx with detail {code, message, ...}. The batch_advance background job (registered on import) moves
queues forward; settings.batch_queue_enabled = false stops new submissions from the queue.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

import jobs.batches as B
from accounts.auth import Principal, require_scope
from api.common import ESTIMATED, TRANSACTION, envelope
from api.jobs import _json_body
from routing import control, disclosure, idempotency

control.install_job_heartbeats(names=("batch_advance",))

router = APIRouter()
READ = require_scope("deployments:read")
METH = "batches"
_BATCH = r"^bat-[0-9a-f]{8,32}\Z"
_STATUS = "^(open|" + "|".join(B.BATCH_STATES) + ")$"


def _refused(fn):
    try:
        return fn()
    except B.BatchRefused as r:
        raise HTTPException(r.status, r.detail()) from None


def _valid_id(batch_id: str) -> None:
    if not re.match(_BATCH, batch_id or ""):
        raise HTTPException(404, {"code": "batch_not_found", "message": "batch not found"})


def _idem(request: Request, who: Principal, scope: str, body, fn, *, resource_of=None, reclaim_check=None):
    code, payload, replayed = idempotency.run(who=who, scope=scope, key=request.headers.get("Idempotency-Key"),
                                              body=body, fn=fn, resource_of=resource_of, reclaim_check=reclaim_check)
    return JSONResponse(payload, status_code=code, headers={"Idempotent-Replayed": "true"} if replayed else None)


def _batches_since(who):
    """reclaim_check: a crashed POST /v1/batches that may already have created a batch is never re-run."""
    def check(row) -> list[str]:
        from sqlalchemy import select

        import normalize
        from store.batches import Batch
        acct = Batch.account_id.is_(None) if who.account_id is None else Batch.account_id == who.account_id
        with normalize.SessionLocal() as s:
            return list(s.scalars(select(Batch.batch_id).where(acct, Batch.created_at >= row.created_at)
                                  .order_by(Batch.created_at).limit(20)))
    return check


@router.post("/v1/batches/preview", tags=["batches"], summary="Preview a batch: shards, placement plan, budget")
async def batch_preview(request: Request, who: Principal = Depends(require_scope("route:preview"))):
    body = await _json_body(request)
    out = _refused(lambda: B.preview(body, who))
    return envelope(disclosure.redact(out, who), kind=ESTIMATED, methodology=METH,
                    also=["/methodology/jobs", "/methodology/best-execution"])


@router.post("/v1/batches", tags=["batches"], summary="Submit a batch (Idempotency-Key required)")
async def batch_submit(request: Request, who: Principal = Depends(require_scope("route:execute"))):
    body = await _json_body(request)

    def run():
        code, out = _refused(lambda: B.submit(body, who))
        return code, envelope(disclosure.redact(out, who), kind="simulated" if out.get("sandbox") else TRANSACTION,
                              methodology=METH, simulated=bool(out.get("sandbox")),
                              also=["/methodology/jobs", "/methodology/execution-safety"])

    return _idem(request, who, "batches", body, run, resource_of=lambda p: (p.get("data") or {}).get("batch_id"),
                 reclaim_check=_batches_since(who))


@router.get("/v1/batches", tags=["batches"], summary="Your batches")
def batch_list(status: str | None = Query(None, pattern=_STATUS), limit: int = Query(50, ge=1, le=200),
               offset: int = Query(0, ge=0), who: Principal = Depends(READ)):
    return envelope(disclosure.redact(B.list_for(who, status=status, limit=limit, offset=offset), who),
                    kind=TRANSACTION, methodology=METH, limit=limit, offset=offset)


@router.get("/v1/batches/{batch_id}", tags=["batches"], summary="One batch: shards, budget, providers, best result")
def batch_get(batch_id: str, who: Principal = Depends(READ)):
    _valid_id(batch_id)
    out = _refused(lambda: B.get(batch_id, who))
    return envelope(disclosure.redact(out, who), kind="simulated" if out.get("sandbox") else TRANSACTION,
                    methodology=METH, simulated=bool(out.get("sandbox")))


@router.post("/v1/batches/{batch_id}/approve", tags=["batches"],
             summary="Admin: ONE approval for the batch (Idempotency-Key required)")
async def batch_approve(batch_id: str, request: Request, who: Principal = Depends(require_scope("admin"))):
    _valid_id(batch_id)
    raw = await request.body()
    body = await _json_body(request) if raw.strip() else {}
    quotes, reason = body.get("quotes"), body.get("reason")
    if set(body) - {"quotes", "reason"}:
        raise HTTPException(422, {"code": "unknown_field", "message": "allowed fields: quotes, reason"})
    if quotes is not None and (not isinstance(quotes, dict) or not all(
            isinstance(v, str) and re.match(r"^\d{1,4}$", str(k)) for k, v in quotes.items())):
        raise HTTPException(422, {"code": "invalid_quotes", "message": "quotes must map shard_index to quote_id"})
    if reason is not None and (not isinstance(reason, str) or len(reason) > 500):
        raise HTTPException(422, {"code": "invalid_reason", "message": "reason must be a string of at most 500 chars"})
    _refused(lambda: B._load_batch(batch_id, who, admin_ok=True))   # 404 before claiming a key

    def run():
        code, out = _refused(lambda: B.approve(batch_id, who, quotes=quotes, reason=reason))
        return code, envelope(out, kind=TRANSACTION, methodology=METH)

    return _idem(request, who, f"batch_approve:{batch_id}", body, run)


@router.post("/v1/batches/{batch_id}/cancel", tags=["batches"], summary="Cancel a batch (Idempotency-Key required)")
def batch_cancel(batch_id: str, request: Request, who: Principal = Depends(require_scope("route:execute"))):
    _valid_id(batch_id)
    _refused(lambda: B._load_batch(batch_id, who))   # 404 before claiming a key for someone else's batch

    def run():
        code, out = _refused(lambda: B.cancel(batch_id, who))
        return code, envelope(out, kind=TRANSACTION, methodology=METH)

    return _idem(request, who, f"batch_cancel:{batch_id}", {}, run)
