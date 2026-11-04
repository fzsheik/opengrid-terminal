"""Major market shifts and the items found around each (analytics/shifts.py; methodology/shifts.md).

    GET /v1/events/shifts                shifts, newest first: gpu, provider, kind (observed | archived), type, severity,
                                         since, until, cursor; each with its best related items and counts by type
    GET /v1/events/shifts/definition     the rule: thresholds, windows, ranking formula, versions
    GET /v1/events/shifts/{shift}        one shift with every stored related item, ranked, and the score behind each

Observed shifts are measured on OpenGrid's own price history; archived ones on list prices recovered from
public archives (methodology/archived-history), labelled as such and never used in any statistic. Every
response says it: related items are near in time and about the same GPU, provider or vendor, which is all
they are. Nothing here says an item caused a shift.
"""
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

import analytics.shifts as shifts  # noqa: F401  (registers the market_shifts job)
from accounts.auth import Principal, require_scope
from api.common import ARCHIVED, OBSERVED, envelope, keyset_cursor, keyset_values, resolve_gpu

router = APIRouter()
READ = require_scope("data:read")
METHODOLOGY = "shifts"


@router.get("/v1/events/shifts", tags=["events"], summary="Major market shifts with related items (not necessarily causal)")
def list_shifts(gpu: str | None = Query(None, description="GPU slug or canonical name"), provider: str | None = None,
                kind: Literal["observed", "archived"] | None = None,
                type: str | None = Query(None, description="comma-separated shift types; see /v1/events/shifts/definition"),
                severity: Literal["notable", "major"] | None = None,
                since: datetime | None = None, until: datetime | None = None,
                limit: int = Query(50, ge=1, le=200), cursor: str | None = None,
                top: int = Query(3, ge=0, le=10, description="related items per shift in the list"),
                _: Principal = Depends(READ)):
    types = [t.strip() for t in type.split(",") if t.strip()] if type else None
    bad = [t for t in types or [] if t not in shifts.TYPES]
    if bad:
        raise HTTPException(422, f"unknown shift type(s) {bad}; see /v1/events/shifts/definition")
    g = resolve_gpu(gpu) if gpu else None
    rows, total = shifts.query(gpu=g, provider=provider.lower() if provider else None, kind=kind, types=types,
                               since=since, until=until, severity=severity, limit=limit + 1, top_n=top,
                               before=keyset_values(cursor, 2) if cursor else None)
    more, rows = len(rows) > limit, rows[:limit]
    nxt = keyset_cursor(rows[-1]["at"], rows[-1]["id"]) if more and rows else None
    return envelope(rows, kind=kind or f"{OBSERVED} or {ARCHIVED}: see each shift's kind", methodology=METHODOLOGY,
                    heading=shifts.NOT_CAUSAL, note=shifts.NOTE, definition_version=shifts.DEFINITION_VERSION,
                    limit=limit, next_cursor=nxt, total=total)


@router.get("/v1/events/shifts/definition", tags=["events"], summary="How a major shift is defined and its items ranked")
def shift_definition(_: Principal = Depends(READ)):
    return envelope(shifts.definition(), methodology=METHODOLOGY)


@router.get("/v1/events/shifts/{shift_id}", tags=["events"], summary="One shift with every related item (not necessarily causal)")
def shift_detail(shift_id: int, _: Principal = Depends(READ)):
    d = shifts.get(shift_id)
    if d is None:
        raise HTTPException(404, f"no shift {shift_id}")
    return envelope(d, kind=d["kind"], methodology=METHODOLOGY, heading=shifts.NOT_CAUSAL, note=shifts.NOTE)
