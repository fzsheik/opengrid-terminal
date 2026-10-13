"""Reserved-compute price-lock requests API (methodology/reservations.md). A request / offer workflow (RFQ):
nothing here is a quote, a binding price, a guaranteed reservation, a launch or a charge.

  customer (accounts.auth principal; a key sees its own account's requests, the operator sees all)
    GET  /v1/reservations/reference?gpu=&gpu_count=&region_group=   INDICATIVE reference now        [route:preview]
    POST /v1/reservations/requests      (*)   submit a request -> requested (+ indicative reference)  [route:preview]
    GET  /v1/reservations                     your requests (?status=open|<state>)                     [route:preview]
    GET  /v1/reservations/{request_id}        one request: status, reference, offer, events            [route:preview]
    POST /v1/reservations/{id}/accept   (*)   {offer_seq} accept the CURRENT offer: intent only        [route:execute]
    POST /v1/reservations/{id}/withdraw (*)   {reason?} withdraw (or decline an offer)                [route:preview]
  operator (scope admin; reasons recorded on every event)
    GET  /v1/admin/reservations               every account's requests (?status=)
    GET  /v1/admin/reservations/{id}          one request, any account
    POST /v1/admin/reservations/{id}/review   {reason?}                         requested -> under_review
    POST /v1/admin/reservations/{id}/offer    {price_per_gpu_hour, valid_until, terms, supply_basis, reason?}
                                              supply_basis REQUIRED (the provider capacity commitment)
    POST /v1/admin/reservations/{id}/decline  {reason}
    POST /v1/admin/reservations/{id}/fulfil   {reason}                          accepted -> fulfilled
    POST /v1/admin/reservations/{id}/cancel   {reason}                          accepted -> cancelled
(*) Idempotency-Key header REQUIRED (routing/idempotency.py): same key + same body replays; different body 422;
in flight 409 + Retry-After. Errors: HTTP 4xx with detail {code, message, ...} (422 for validation).
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request

import reservations.service as R
from accounts.auth import Principal, require_scope
from api.common import envelope
from api.jobs import _idem, _json_body
from reservations import states as S

router = APIRouter()
PREVIEW = require_scope("route:preview")
ADMIN = require_scope("admin")
METH = R.METHODOLOGY
_ID = r"^rsv-[0-9a-f]{8,32}\Z"
_STATUS = "^(open|" + "|".join(S.STATES) + ")$"
ALSO = ["/methodology/indices", "/methodology/settlement"]


def _env(data, **meta) -> dict:
    return envelope(data, methodology=METH, binding=False, price_concept="indicative", also=ALSO, **meta)


def _refused(fn):
    try:
        return fn()
    except R.Refused as r:
        raise HTTPException(r.status, r.detail()) from None


def _valid_id(request_id: str) -> None:
    if not re.match(_ID, request_id or ""):
        raise HTTPException(404, {"code": "reservation_not_found", "message": "price-lock request not found"})


def _created_since(who):
    """reclaim_check: a crashed / stale POST that may already have created a request is never re-run."""
    def check(row) -> list[str]:
        from sqlalchemy import select

        import normalize
        from store.reservations import ReservationRequest as RR
        acct = RR.account_id.is_(None) if who.account_id is None else RR.account_id == who.account_id
        with normalize.SessionLocal() as s:
            return list(s.scalars(select(RR.request_id).where(acct, RR.created_at >= row.created_at)
                                  .order_by(RR.created_at).limit(20)))
    return check


# --------------------------------------------------------------------------
# Customer
# --------------------------------------------------------------------------

@router.get("/v1/reservations/reference", tags=["reservations"],
            summary="Indicative reference for a GPU: index level, best on-demand listings, latest fixing")
def reservation_reference(gpu: str = Query(..., min_length=1, max_length=128),
                          gpu_count: int = Query(1, ge=1, le=R.MAX_GPU_COUNT),
                          region_group: str | None = Query(None, max_length=32),
                          max_price_per_gpu_hour: float | None = Query(None, gt=0, le=R.MAX_PRICE),
                          who: Principal = Depends(PREVIEW)):
    def run():
        name = R.resolve_gpu(gpu)
        return R.reference(name, gpu_count, R._region(region_group), max_price_per_gpu_hour)
    return _env(_refused(run), kind="indicative", note=R.INDICATIVE_NOTE)


@router.post("/v1/reservations/requests", tags=["reservations"],
             summary="Submit a price-lock request (Idempotency-Key required)")
async def reservation_create(request: Request, who: Principal = Depends(PREVIEW)):
    body = await _json_body(request)

    def run():
        code, out = _refused(lambda: R.create(body, who))
        return code, _env(out, note=R.NOT_A_CONTRACT)

    return _idem(request, who, "reservations", body, run,
                 resource_of=lambda p: (p.get("data") or {}).get("request_id"), reclaim_check=_created_since(who))


@router.get("/v1/reservations", tags=["reservations"], summary="Your price-lock requests")
def reservation_list(status: str | None = Query(None, pattern=_STATUS), limit: int = Query(100, ge=1, le=500),
                     offset: int = Query(0, ge=0), who: Principal = Depends(PREVIEW)):
    return _env(R.list_for(who, status=status, limit=limit, offset=offset), limit=limit, offset=offset)


@router.get("/v1/reservations/{request_id}", tags=["reservations"], summary="One price-lock request")
def reservation_get(request_id: str, who: Principal = Depends(PREVIEW)):
    _valid_id(request_id)
    return _env(_refused(lambda: R.get(request_id, who)))


@router.post("/v1/reservations/{request_id}/accept", tags=["reservations"],
             summary="Accept the current offer: records intent only, launches and bills nothing")
async def reservation_accept(request_id: str, request: Request, who: Principal = Depends(require_scope("route:execute"))):
    _valid_id(request_id)
    body = await _json_body(request)
    _refused(lambda: R.get(request_id, who))   # 404 before claiming a key for someone else's request

    def run():
        code, out = _refused(lambda: R.accept(request_id, body, who))
        return code, _env(out, note=R.ACCEPTED_MESSAGE)

    return _idem(request, who, f"rsv_accept:{request_id}", body, run)


@router.post("/v1/reservations/{request_id}/withdraw", tags=["reservations"],
             summary="Withdraw a request (or decline its offer)")
async def reservation_withdraw(request_id: str, request: Request, who: Principal = Depends(PREVIEW)):
    _valid_id(request_id)
    body = await _json_body(request)
    _refused(lambda: R.get(request_id, who))

    def run():
        code, out = _refused(lambda: R.withdraw(request_id, body, who))
        return code, _env(out)

    return _idem(request, who, f"rsv_withdraw:{request_id}", body, run)


# --------------------------------------------------------------------------
# Operator
# --------------------------------------------------------------------------

@router.get("/v1/admin/reservations", tags=["admin"], summary="Every account's price-lock requests")
def admin_reservations(status: str | None = Query(None, pattern=_STATUS), limit: int = Query(200, ge=1, le=1000),
                       offset: int = Query(0, ge=0), who: Principal = Depends(ADMIN)):
    return _env(R.list_for(who, status=status, limit=limit, offset=offset, admin=True), limit=limit, offset=offset)


@router.get("/v1/admin/reservations/{request_id}", tags=["admin"], summary="One price-lock request (any account)")
def admin_reservation(request_id: str, who: Principal = Depends(ADMIN)):
    _valid_id(request_id)
    return _env(_refused(lambda: R.get(request_id, who, admin=True)))


def _admin_action(fn):
    async def handler(request_id: str, request: Request, who: Principal = Depends(ADMIN)):
        _valid_id(request_id)
        body = await _json_body(request)
        return _env(_refused(lambda: fn(request_id, body, who)))
    return handler


for _name, _fn, _summary in (
        ("review", R.review, "Mark a request under review"),
        ("offer", R.offer, "Make or revise an offer (supply_basis required)"),
        ("decline", R.decline, "Decline a request or retract an offer (reason required)"),
        ("fulfil", R.fulfil, "Mark an accepted request fulfilled (reason required)"),
        ("cancel", R.cancel, "Cancel an accepted request (reason required)")):
    router.add_api_route(f"/v1/admin/reservations/{{request_id}}/{_name}", _admin_action(_fn), methods=["POST"],
                         tags=["admin"], summary=_summary, name=f"admin_reservation_{_name}")
