"""Idempotency keys: a retried or duplicated request never performs its side effect twice.

Header `Idempotency-Key` is REQUIRED on POST /v1/route, POST /v1/route/{id}/approve,
POST /v1/deployments/{id}/terminate and /stop (api/routing.py).

    same key + same body, completed     -> the stored response is replayed (same HTTP code)
    same key + different body           -> 422 idempotency_key_reused
    same key while the first is running -> 409 with Retry-After
    first attempt crashed (failed), or in_progress older than settings.idempotency_stale_seconds
                                        -> the key may be reclaimed and the request re-run. Safe because the
                                           side effects below are themselves single-shot: a deployment's
                                           provision call is guarded by its launch token (deployments.py).

Concurrency: the claim is one `INSERT ... ON CONFLICT DO NOTHING RETURNING id` against
UNIQUE(principal, scope, key), so of two identical concurrent requests exactly one proceeds.
Keys are scoped per principal (account, or the operator) and per operation (scope), and expire after
settings.idempotency_ttl_hours.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

import normalize
from config import settings
from store.routing import IdempotencyKey

log = logging.getLogger(__name__)

MAX_KEY = 255


def _now():
    return datetime.now(timezone.utc)


def principal_key(who) -> str:
    if who.account_id is not None:
        return f"acct:{who.account_id}"
    return who.kind or "operator"


def request_hash(body) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def _check_key(key: str | None) -> str:
    if not key or not key.strip():
        raise HTTPException(428, {"code": "idempotency_key_required",
                                  "message": "this request spends money or changes a deployment: send an "
                                             "Idempotency-Key header (e.g. a UUID per user action)"})
    key = key.strip()
    if len(key) > MAX_KEY:
        raise HTTPException(422, {"code": "idempotency_key_invalid", "message": f"Idempotency-Key over {MAX_KEY} chars"})
    return key


def claim(principal: str, scope: str, key: str, rhash: str, reclaim_check=None):
    """('new', id) when this request owns the key now, or ('replay', code, body).

    reclaim_check(row) -> list of resource ids the earlier (crashed / stale) attempt may have created. When it
    returns any, the key is NOT reclaimed: re-running would create a second resource (POST /v1/route ->
    a second deployment, i.e. a second paid instance). 409 idempotency_outcome_unknown names them instead."""
    now = _now()
    stmt = (insert(IdempotencyKey)
            .values(principal=principal, scope=scope, key=key, request_hash=rhash, status="in_progress",
                    created_at=now, updated_at=now, expires_at=now + timedelta(hours=settings.idempotency_ttl_hours))
            .on_conflict_do_nothing(constraint="ux_idem_principal_scope_key")
            .returning(IdempotencyKey.id))
    with normalize.SessionLocal.begin() as s:
        new_id = s.execute(stmt).scalar()
    if new_id is not None:
        return "new", new_id
    with normalize.SessionLocal() as s:
        row = s.scalars(select(IdempotencyKey).where(IdempotencyKey.principal == principal,
                                                     IdempotencyKey.scope == scope, IdempotencyKey.key == key)).first()
    if row is None:  # deleted between the two statements; try once more
        return claim(principal, scope, key, rhash)
    expired = row.expires_at <= now
    if not expired and row.request_hash != rhash:
        raise HTTPException(422, {"code": "idempotency_key_reused",
                                  "message": "this Idempotency-Key was used with a different request body"})
    if not expired and row.status == "completed":
        return "replay", row.response_code or 200, row.response
    stale = row.status == "in_progress" and (now - row.updated_at).total_seconds() > settings.idempotency_stale_seconds
    if (row.status == "failed" or stale) and not expired and reclaim_check is not None:
        created = reclaim_check(row)
        if created:
            raise HTTPException(409, {"code": "idempotency_outcome_unknown",
                                      "message": "an earlier request with this Idempotency-Key did not finish, and "
                                                 "it may have created these resources; it is NOT re-run. Inspect "
                                                 "them (GET /v1/deployments/{id}); to route again use a new key",
                                      "resources": created})
    if expired or row.status == "failed" or stale:
        with normalize.SessionLocal.begin() as s:  # optimistic reclaim: only one reclaimer wins
            got = s.execute(update(IdempotencyKey)
                            .where(IdempotencyKey.id == row.id, IdempotencyKey.status == row.status,
                                   IdempotencyKey.updated_at == row.updated_at)
                            .values(status="in_progress", request_hash=rhash, updated_at=now, response=None,
                                    response_code=None,
                                    expires_at=now + timedelta(hours=settings.idempotency_ttl_hours))
                            .returning(IdempotencyKey.id)).scalar()
        if got is not None:
            return "new", got
    raise HTTPException(409, {"code": "idempotency_in_progress",
                              "message": "a request with this Idempotency-Key is still being processed"},
                        headers={"Retry-After": "5"})


def complete(row_id: int, code: int, body, resource_id: str | None = None) -> None:
    with normalize.SessionLocal.begin() as s:
        s.execute(update(IdempotencyKey).where(IdempotencyKey.id == row_id)
                  .values(status="completed", response_code=code, response=body, resource_id=resource_id,
                          updated_at=_now()))


def fail(row_id: int) -> None:
    try:
        with normalize.SessionLocal.begin() as s:
            s.execute(update(IdempotencyKey).where(IdempotencyKey.id == row_id)
                      .values(status="failed", updated_at=_now()))
    except Exception:  # noqa: BLE001
        log.exception("could not mark idempotency key %s failed", row_id)


def deployments_since(who):
    """reclaim_check for scopes that create deployments: the principal's deployments created since the key
    was first claimed (any of them may be the crashed attempt's)."""
    def check(row) -> list[str]:
        from store.routing import Deployment
        acct = Deployment.account_id.is_(None) if who.account_id is None else Deployment.account_id == who.account_id
        with normalize.SessionLocal() as s:
            return list(s.scalars(select(Deployment.deployment_id).where(
                acct, Deployment.created_at >= row.created_at)
                .order_by(Deployment.created_at).limit(20)))
    return check


def run(*, who, scope: str, key: str | None, body, fn, resource_of=None, reclaim_check=None):
    """Run fn() at most once per (principal, scope, key). Returns (code, payload, replayed).

    fn returns (code, payload). An HTTPException from fn is a definitive answer: it is stored and replayed
    like a success (same code, same detail). Any other exception marks the key failed (reclaimable) and
    propagates.
    """
    from fastapi.encoders import jsonable_encoder

    key = _check_key(key)
    got = claim(principal_key(who), scope, key, request_hash(body), reclaim_check)
    if got[0] == "replay":
        return got[1], got[2], True
    row_id = got[1]
    try:
        code, payload = fn()
    except HTTPException as exc:
        payload = jsonable_encoder({"detail": exc.detail})
        complete(row_id, exc.status_code, payload)
        raise
    except Exception:
        fail(row_id)
        raise
    payload = jsonable_encoder(payload)
    complete(row_id, code, payload, resource_of(payload) if resource_of else None)
    return code, payload, False
