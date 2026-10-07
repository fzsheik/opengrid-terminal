"""API key usage: one row per authenticated request, written in batches.

Why buffered: an INSERT per request on the hot path would double the database
work of a cheap data read. Requests append to an in-memory buffer; the
`api_usage_flush` job writes it every 10 s (and readers flush first, so
/v1/usage is never stale). The buffer is bounded (MAX_BUFFER); if the database is
down long enough to fill it, the oldest rows are dropped and counted in `dropped`
rather than growing memory without limit. A crash loses at most ~10 s of rows.

Retention: rows older than settings.api_usage_retention_days (90) are deleted by
the daily `api_usage_retention` job. Long-run billing of the data API reads
these rows when an invoice is drafted, so drafts must run within the window.

Only API-key requests are logged. Operator (site password) requests are not.

The ASGI middleware (installed by api/accounts.py `install(app)`) times each
request, reads the principal / rate-limit decision that accounts.keys.verify
left on request.state, adds X-RateLimit-* headers, and enqueues the row.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone

from sqlalchemy import insert, text

import normalize
from config import settings
from jobs import job
from store.accounts import ApiKeyUsage

log = logging.getLogger(__name__)

MAX_BUFFER = 50_000
_buffer: deque = deque()
_lock = threading.Lock()
dropped = 0


def record(key_id: int, account_id: int, method: str, path: str, status: int, duration_ms: int | None,
           ts: datetime | None = None) -> None:
    global dropped
    row = {"key_id": key_id, "account_id": account_id, "ts": ts or datetime.now(timezone.utc),
           "method": method[:8], "path": path[:300], "status": int(status), "duration_ms": duration_ms}
    with _lock:
        if len(_buffer) >= MAX_BUFFER:
            _buffer.popleft()
            dropped += 1
        _buffer.append(row)


def flush() -> int:
    """Write the buffer. Rows go back on the front if the write fails."""
    with _lock:
        rows = list(_buffer)
        _buffer.clear()
    if not rows:
        return 0
    try:
        with normalize.SessionLocal.begin() as s:
            for i in range(0, len(rows), 1000):
                s.execute(insert(ApiKeyUsage), rows[i:i + 1000])
    except Exception:
        with _lock:
            _buffer.extendleft(reversed(rows))
        raise
    return len(rows)


def pending() -> int:
    return len(_buffer)


@job("api_usage_flush", every_seconds=10, initial_delay_seconds=10)
def _flush_job():
    return {"written": flush(), "dropped_total": dropped}


@job("api_usage_retention", every_seconds=86400, initial_delay_seconds=600)
def prune() -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.api_usage_retention_days)
    with normalize.SessionLocal.begin() as s:
        n = s.execute(text("DELETE FROM api_key_usage WHERE ts < :c"), {"c": cutoff}).rowcount
    return {"deleted": n, "cutoff": cutoff.isoformat()}


def summary(account_id: int | None = None, hours: float = 24, key_id: int | None = None, recent: int = 50) -> dict:
    """Totals, by status class, by key and top paths over the window, plus the latest rows."""
    flush()
    t0 = datetime.now(timezone.utc) - timedelta(hours=hours)
    p = {"a": account_id, "k": key_id, "t0": t0}
    where = """ts >= :t0 AND (CAST(:a AS integer) IS NULL OR account_id = :a)
               AND (CAST(:k AS integer) IS NULL OR key_id = :k)"""
    with normalize.SessionLocal() as s:
        tot = s.execute(text(f"""
            SELECT count(*) AS requests,
                   count(*) FILTER (WHERE status < 400) AS ok,
                   count(*) FILTER (WHERE status = 429) AS rate_limited,
                   count(*) FILTER (WHERE status >= 400 AND status <> 429) AS errors,
                   avg(duration_ms) AS avg_ms
            FROM api_key_usage WHERE {where}"""), p).one()
        by_key = s.execute(text(f"""
            SELECT key_id, account_id, count(*) AS requests, max(ts) AS last_ts
            FROM api_key_usage WHERE {where} GROUP BY key_id, account_id ORDER BY requests DESC LIMIT 100"""), p).all()
        by_path = s.execute(text(f"""
            SELECT method, path, count(*) AS requests FROM api_key_usage WHERE {where}
            GROUP BY method, path ORDER BY requests DESC LIMIT 20"""), p).all()
        latest = s.execute(text(f"""
            SELECT key_id, account_id, ts, method, path, status, duration_ms FROM api_key_usage WHERE {where}
            ORDER BY ts DESC LIMIT :n"""), {**p, "n": recent}).all()
    return {
        "window_hours": hours, "since": t0.isoformat(),
        "requests": tot.requests, "ok": tot.ok, "rate_limited": tot.rate_limited, "errors": tot.errors,
        "avg_duration_ms": None if tot.avg_ms is None else round(float(tot.avg_ms), 1),
        "by_key": [dict(r._mapping) for r in by_key],
        "top_paths": [dict(r._mapping) for r in by_path],
        "recent": [dict(r._mapping) for r in latest],
        "retention_days": settings.api_usage_retention_days,
    }


def count_requests(account_id: int, t0: datetime, t1: datetime) -> int:
    """Authenticated API requests in [t0, t1): what a data_api fee component bills."""
    flush()
    with normalize.SessionLocal() as s:
        return s.execute(text("""SELECT count(*) FROM api_key_usage
                                 WHERE account_id = :a AND ts >= :t0 AND ts < :t1 AND status < 400"""),
                         {"a": account_id, "t0": t0, "t1": t1}).scalar() or 0


class UsageMiddleware:
    """Pure ASGI (no BaseHTTPMiddleware): adds rate-limit headers and logs key requests."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith("/v1/"):
            return await self.app(scope, receive, send)
        state = scope.setdefault("state", {})  # the dict request.state writes into, shared downstream
        started = time.perf_counter()
        status_holder = {"status": 500}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                decision = state.get("ratelimit")
                if decision is not None and message["status"] != 429:  # 429s carry their own headers
                    headers = list(message.get("headers", []))
                    headers += [(k.lower().encode(), v.encode()) for k, v in decision.headers().items()]
                    message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            who = state.get("usage_key")  # (key_id, account_id), set by keys.verify once the key is known
            if who is not None:
                try:
                    record(who[0], who[1], scope.get("method", "GET"), scope.get("path", ""),
                           status_holder["status"], int((time.perf_counter() - started) * 1000))
                except Exception:
                    log.exception("usage record failed")
