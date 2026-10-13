"""Official index fixings: the settlement ledger (analytics/settlement.py, methodology/settlement.md).

    GET  /v1/fixings                               latest daily + hourly fixing per settlement index, ledger head
    GET  /v1/fixings/methodology                   schedule, thresholds, windows, methodology change notices
    GET  /v1/fixings/restatements                  every restatement (public correction notices)
    GET  /v1/fixings/chain/verify                  whole-ledger hash-chain check, paged by seq
    GET  /v1/fixings/{fixing_id}                   one fixing with its full inputs and raw evidence
    GET  /v1/fixings/{fixing_id}/verify            recompute the value from stored inputs, check hashes + links
    GET  /v1/indices/{index_id}/fixings            fixing history of one index (filters)
    POST /v1/admin/fixings/{fixing_id}/restate     admin: append a correction (within the window)
    POST /v1/admin/methodology/notices             admin: record a methodology change notice

Reads use the same scope as /v1/indices (data:read; public when PUBLIC_PAGES is on). Fixings are
observed market prices fixed under a frozen methodology: never quotes, never execution prices.
"""

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field

from accounts.auth import Principal, require_scope
from analytics import indices
from analytics import settlement as S
from api.common import OBSERVED, OBSERVED_MARKET_PRICE, envelope
from routing import control

control.install_job_heartbeats(names=("index_fixings",))

router = APIRouter()
READ = require_scope("data:read")
ADMIN = require_scope("admin")
FIXING_ID = r"^fx-[0-9a-f]{24}$"  # pydantic's Rust regex: $ is the end of text (no trailing-newline match)


def _meta(**kw):
    return {"kind": OBSERVED, "methodology": S.METHODOLOGY, "price_concept": OBSERVED_MARKET_PRICE,
            "settlement_version": S.SETTLEMENT_VERSION, **kw}


def _utc(t: datetime | None) -> datetime | None:
    if t is None:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _actor(who: Principal) -> str:
    return "operator" if who.kind == "operator" else f"key:{who.key_id}"


def _refused(fn):
    try:
        return fn()
    except S.Refused as r:
        raise HTTPException(r.status, r.detail) from None


@router.get("/v1/fixings", summary="Latest official fixings of every settlement index")
def fixings_latest(_: Principal = Depends(READ)):
    return envelope(S.latest(), **_meta(),
                    note="Fixings are taken only after the hour is final and never change; corrections are new "
                         "'restated' rows. 'provisional' is the current index level, not a fixing.")


@router.get("/v1/fixings/methodology", summary="Fixing schedule, thresholds, correction windows, change notices")
def fixings_methodology(_: Principal = Depends(READ)):
    return envelope({**S.schedule(), "index_methodology_version": indices.METHODOLOGY_VERSION,
                     "notices": S.notices()}, **_meta())


@router.get("/v1/fixings/restatements", summary="Every restatement: what was corrected, by whom, why")
def fixings_restatements(limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0),
                         _: Principal = Depends(READ)):
    items, total = S.restatements(limit, offset)
    return envelope(items, **_meta(), pagination={"limit": limit, "offset": offset, "total": total})


@router.get("/v1/fixings/chain/verify", summary="Check the hash chain over the whole fixings ledger (paged)")
def fixings_chain_verify(from_seq: int | None = Query(None, ge=1), limit: int = Query(1000, ge=1, le=5000),
                         _: Principal = Depends(READ)):
    return envelope(S.verify_chain(from_seq, limit), **_meta(),
                    note="Pass next_seq as from_seq to continue. Compare head_hash with a copy you stored earlier: "
                         "a chain can only be extended, so a different head at the same seq means the ledger changed.")


@router.get("/v1/fixings/{fixing_id}", summary="One fixing: value, flags, full inputs and raw evidence")
def fixing_detail(fixing_id: str = Path(..., pattern=FIXING_ID), _: Principal = Depends(READ)):
    f = S.get_fixing(fixing_id)
    if f is None:
        raise HTTPException(404, f"unknown fixing {fixing_id!r}")
    return envelope(f, **_meta())


@router.get("/v1/fixings/{fixing_id}/verify", summary="Recompute a fixing from its stored inputs; check hashes and links")
def fixing_verify(fixing_id: str = Path(..., pattern=FIXING_ID), _: Principal = Depends(READ)):
    v = S.verify_fixing(fixing_id)
    if v is None:
        raise HTTPException(404, f"unknown fixing {fixing_id!r}")
    return envelope(v, **_meta())


@router.get("/v1/indices/{index_id}/fixings", summary="Official fixing history of one index")
def index_fixings(index_id: str, type: Literal["hourly", "daily"] | None = None,
                  status: Literal["final", "restated", "not_published"] | None = None,
                  from_: datetime | None = Query(None, alias="from"), to: datetime | None = None,
                  effective: bool = False, limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0),
                  _: Principal = Depends(READ)):
    d = indices.definition(index_id)
    if d is None:
        raise HTTPException(404, f"unknown index {index_id!r}; see /v1/indices")
    settled = index_id in S.SETTLEMENT_INDICES
    items, total = S.list_fixings(index_id, type, status, _utc(from_), _utc(to), limit, offset, effective) \
        if settled else ([], 0)
    return envelope({"index_id": index_id, "name": d.name, "unit": d.unit, "settlement_index": settled,
                     "fixings": items}, **_meta(pagination={"limit": limit, "offset": offset, "total": total}),
                    note=None if settled else "not a settlement index: no official fixings are taken for it; "
                                              f"settlement indices are {', '.join(S.SETTLEMENT_INDICES)}")


class Exclusion(BaseModel):
    provider: str = Field(..., min_length=1, max_length=64)
    reason: str = Field(..., min_length=3, max_length=500)


class RestateBody(BaseModel):
    reason: str = Field(..., min_length=10, max_length=2000)
    exclude: list[Exclusion] = Field(default_factory=list, max_length=50)
    include: list[str] = Field(default_factory=list, max_length=50)
    rederive: bool = False


@router.post("/v1/admin/fixings/{fixing_id}/restate", tags=["admin"], status_code=201,
             summary="Append a correction of a fixing (admin; within the correction window)")
def fixing_restate(body: RestateBody, fixing_id: str = Path(..., pattern=FIXING_ID), who: Principal = Depends(ADMIN)):
    out = _refused(lambda: S.restate(fixing_id, actor=_actor(who), reason=body.reason,
                                     exclude=[e.model_dump() for e in body.exclude], include=body.include,
                                     rederive=body.rederive))
    return envelope(out, **_meta(), note="The original fixing is unchanged; this restatement is listed publicly at "
                                         "/v1/fixings/restatements and an ops alert was raised.")


class NoticeBody(BaseModel):
    scope: Literal["indices", "settlement"]
    version: str = Field(..., min_length=1, max_length=16)
    effective_from: datetime
    notice_days: int = Field(..., ge=0, le=365)
    summary: str = Field(..., min_length=10, max_length=4000)


@router.post("/v1/admin/methodology/notices", tags=["admin"], status_code=201,
             summary="Record a methodology change notice (admin)")
def methodology_notice(body: NoticeBody, who: Principal = Depends(ADMIN)):
    out = _refused(lambda: S.record_notice(body.scope, body.version, _utc(body.effective_from), body.notice_days,
                                           body.summary, _actor(who)))
    return envelope(out, **_meta(), note="Fixings are published under a new version only from effective_from; past "
                                         "fixings are never rewritten.")
