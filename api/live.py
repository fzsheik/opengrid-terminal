"""Live market feeds: the state of each push stream and the changes it recorded (live/; methodology/live-feeds.md).

    GET /v1/live/status   each stream: connected, frames, changes, gaps, last provider time, last write, errors
    GET /v1/live/events   change-only stream events, newest first (provider, item, since, limit)

Operator reads (not in api/pages.py PUBLIC_V1): connection internals, and the raw executor bodies.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text

from accounts.auth import Principal, require_scope
from api.common import OBSERVED, envelope
from live import lium_stream

router = APIRouter()
READ = require_scope("data:read")
METHOD = "live-feeds"

lium_stream.register()  # the lium_stream supervisor job (off when the poller or LIUM_STREAM_ENABLED is off)


@router.get("/internal/live/status", tags=["live"], summary="State of each live market stream")
def live_status(who: Principal = Depends(READ)):
    return envelope([lium_stream.status()], kind=OBSERVED, methodology=METHOD)


@router.get("/internal/live/events", tags=["live"], summary="Change-only events recorded from live streams, newest first")
def live_events(provider: str = "lium", item: str | None = None, since: datetime | None = None,
                limit: int = Query(100, ge=1, le=1000), raw: bool = False, who: Principal = Depends(READ)):
    import normalize

    cols = "id, provider, stream, connection_id, seq, provider_ts, received_at, prev_seen_at, kind, basis, item_id, " \
           "changed_fields, kept" + (", raw" if raw else "")
    sql = f"SELECT {cols} FROM stream_events WHERE provider = :p" + (" AND item_id = :i" if item else "") + \
          (" AND provider_ts >= :s" if since else "") + " ORDER BY provider_ts DESC, id DESC LIMIT :n"
    with normalize.SessionLocal() as s:
        rows = [dict(r._mapping) for r in s.execute(text(sql), {"p": provider, "i": item, "s": since, "n": limit})]
    return envelope(rows, kind=OBSERVED, methodology=METHOD, count=len(rows))
