"""Market events, the homepage overview, the opportunity monitor and the tape.

    GET /v1/events              structured market events, newest first (filters, pagination)
    GET /v1/events/types        the event catalogue: what fires each type, severity rule, data kind
    GET /v1/overview            the whole homepage payload in one request (cached 60s)
    GET /v1/opportunities       live opportunity monitor (computed, not stored; cached 60s)
    GET /v1/tape                compact ticker of the latest price moves and notable events

All prices are observed market prices (list prices as normalized), never quotes or
execution prices. Methodology: /methodology/events, /methodology/opportunities.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query

import analytics.rollups  # noqa: F401  (registers the market_hourly job)
from accounts.auth import Principal, require_scope
from analytics import events, movers, opportunities
from api.common import INFERRED, OBSERVED, OBSERVED_MARKET_PRICE, envelope, resolve_gpu

router = APIRouter()
READ = require_scope("data:read")
METHOD_EVENTS, METHOD_OPPS = "events", "opportunities"


def _csv(v: str | None) -> list[str] | None:
    return [x.strip() for x in v.split(",") if x.strip()] if v else None


def _types(v: str | None, known) -> list[str] | None:
    t = _csv(v)
    bad = [x for x in t or [] if x not in known]
    if bad:
        raise HTTPException(422, f"unknown type(s) {bad}; see /v1/events/types")
    return t


@router.get("/v1/events", tags=["events"], summary="Market events, newest first")
def list_events(gpu: str | None = Query(None, description="GPU slug or canonical name"),
                provider: str | None = None,
                type: str | None = Query(None, description="comma-separated event types"),
                severity: str | None = Query(None, description="comma-separated: info, notable, major"),
                min_severity: str | None = Query(None, pattern="^(info|notable|major)$"),
                since: datetime | None = None, until: datetime | None = None,
                limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0),
                who: Principal = Depends(READ)):
    sev = _csv(severity)
    if sev and any(x not in events.SEVERITIES for x in sev):
        raise HTTPException(422, "severity must be info, notable or major")
    g = resolve_gpu(gpu) if gpu else None
    items, total = events.query(gpu=g, provider=provider, since=since, until=until,
                                types=_types(type, events.TYPES), severities=sev, min_severity=min_severity,
                                limit=limit, offset=offset)
    return envelope(items, kind="mixed: see each event's kind", methodology=METHOD_EVENTS,
                    price_concept=OBSERVED_MARKET_PRICE,
                    pagination={"limit": limit, "offset": offset, "total": total})


@router.get("/v1/events/types", tags=["events"], summary="Event catalogue: triggers, severity rules, data kind")
def event_types(who: Principal = Depends(READ)):
    return envelope({"events": events.catalogue(), "severities": list(events.SEVERITIES),
                     "opportunities": opportunities.catalogue()}, methodology=METHOD_EVENTS)


@router.get("/v1/overview", tags=["events"], summary="Homepage market overview in one payload")
def overview(who: Principal = Depends(READ)):
    return envelope(movers.overview(), kind="mixed: see each section's kind", methodology=f"{METHOD_EVENTS}#overview",
                    price_concept=OBSERVED_MARKET_PRICE, cache_seconds=60)


@router.get("/v1/opportunities", tags=["events"], summary="Live opportunity monitor")
def list_opportunities(type: str | None = Query(None, description="comma-separated opportunity types"),
                       gpu: str | None = None, provider: str | None = None,
                       min_score: float = Query(0, ge=0, le=100), limit: int = Query(100, ge=1, le=500),
                       who: Principal = Depends(READ)):
    g = resolve_gpu(gpu) if gpu else None
    data = opportunities.opportunities(types=_types(type, opportunities.TYPES), gpu=g, provider=provider,
                                       min_score=min_score, limit=limit)
    return envelope(data, kind=f"mixed: {OBSERVED} or {INFERRED} per item", methodology=METHOD_OPPS,
                    price_concept=OBSERVED_MARKET_PRICE, cache_seconds=60,
                    note="observations about listed prices, not advice or quotes")


@router.get("/v1/tape", tags=["events"], summary="Ticker: latest price moves and notable events")
def tape(gpu: str | None = None, limit: int = Query(40, ge=1, le=200), who: Principal = Depends(READ)):
    g = resolve_gpu(gpu) if gpu else None
    return envelope(movers.tape(g, limit), kind="mixed: see each item's kind", methodology=METHOD_EVENTS,
                    price_concept=OBSERVED_MARKET_PRICE)
