"""The internal monthly market report (reports/monthly.py; methodology/monthly-report.md).

    GET  /v1/reports/monthly                      stored months, newest first (data:read)
    GET  /v1/reports/monthly/{month}              one month: the full report + its public-safe summary (data:read)
    POST /v1/reports/monthly/{month}/regenerate   (admin) generate the month now; overwrites a stored one

PRIVATE: "reports" is in api/pages.PRIVATE_V1 (never readable anonymously, whatever PUBLIC_PAGES says) and not in
api/contract.DATA_AREAS (the Data API host answers 404). The report names providers next to their prices: it is
for the operator and signed-in readers (the viewer login or a data:read key), never the public site.

The regenerate POST needs no Idempotency-Key: it spends nothing and is deterministic (the same stored data gives
the same report), so a repeated click writes the same row again; generated_at is kept.
Importing this module registers the daily `monthly_report` background job.
"""

from fastapi import APIRouter, Depends, HTTPException, Query

from accounts.auth import Principal, require_scope
from api.common import envelope
from reports import monthly

router = APIRouter()
READ = require_scope("data:read")
ADMIN = require_scope("admin")
TAGS = ["reports"]


def _month(month: str) -> str:
    try:
        monthly.parse_month(month)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    return month


@router.get("/v1/reports/monthly", tags=TAGS, summary="Internal monthly market reports: stored months, newest first")
def list_monthly(limit: int = Query(120, ge=1, le=1000), cursor: str | None = None, who: Principal = Depends(READ)):
    return envelope(monthly.list_reports(), methodology=monthly.METHODOLOGY, page=(limit, cursor))


@router.get("/v1/reports/monthly/{month}", tags=TAGS, summary="One internal monthly market report (YYYY-MM, UTC)")
def get_monthly(month: str, who: Principal = Depends(READ)):
    row = monthly.get(_month(month))
    if row is None:
        raise HTTPException(404, f"no report stored for {month}: the monthly_report job writes the previous month "
                                 "after the 1st; an admin can POST .../regenerate")
    return envelope(row, kind="derived", methodology=monthly.METHODOLOGY)


@router.post("/v1/reports/monthly/{month}/regenerate", tags=TAGS,
             summary="(admin) Generate one month's report now, overwriting a stored one (generated_at kept)")
def regenerate_monthly(month: str, who: Principal = Depends(ADMIN)):
    try:
        out = monthly.generate_and_store(_month(month), force=True)
    except ValueError as e:
        raise HTTPException(409, str(e)) from None
    return envelope({**out, **(monthly.get(month) or {})}, kind="derived", methodology=monthly.METHODOLOGY)
