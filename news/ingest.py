"""The news poll: fetch every due source, store what came back, log every attempt.

Each enabled source has its own interval (sources.py). The job wakes every minute and
fetches only sources that are due, at most CONCURRENCY at once, each under its own
timeout. Fetches run concurrently; storing runs one source at a time, each in its own
transaction, so a failing source (network, parse, database) never blocks or rolls back
another. Disabled by settings.news_enabled = False.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import normalize
from config import settings
from jobs import job
from news import fetch as fetcher
from news import parse, sources as registry
from news import store

log = logging.getLogger(__name__)

CONCURRENCY = 4
SOURCE_TIMEOUT = fetcher.TIMEOUT_SECONDS + 10


def due(states: dict[str, dict], now: datetime, force: bool = False, only: list[str] | None = None) -> list:
    out = []
    for src in registry.enabled():
        if only and src.id not in only:
            continue
        last = (states.get(src.id) or {}).get("last_fetched_at")
        if force or last is None or now - last >= timedelta(seconds=src.poll_seconds):
            out.append(src)
    return out


async def _fetch_all(srcs: list, states: dict[str, dict], conditional: bool) -> list[tuple]:
    sem = asyncio.Semaphore(CONCURRENCY)
    async with fetcher.client() as c:
        async def one(src):
            st = states.get(src.id) or {}
            # A changed URL must not reuse the old URL's validators.
            same_url = st.get("url") == src.url
            etag = st.get("etag") if conditional and same_url else None
            lm = st.get("last_modified") if conditional and same_url else None
            async with sem:
                try:
                    return src, await asyncio.wait_for(fetcher.fetch(c, src.url, etag, lm), SOURCE_TIMEOUT)
                except Exception as exc:  # wait_for timeout or anything fetch() missed
                    return src, {"fetched_at": datetime.now(timezone.utc), "ok": False, "status": None,
                                 "error": f"{type(exc).__name__}: {exc}"[:500], "duration_ms": None}
        return await asyncio.gather(*(one(s) for s in srcs))


_synced_for = None


def run(force: bool = False, only: list[str] | None = None, conditional: bool = True) -> dict:
    """Poll due sources (all enabled ones when force). Returns a per-source summary."""
    global _synced_for
    if _synced_for is not normalize.SessionLocal:  # the registry is static: mirror it once per database
        store.sync_sources()
        _synced_for = normalize.SessionLocal
    states = store.source_states()
    srcs = due(states, datetime.now(timezone.utc), force=force, only=only)
    if not srcs:
        return {"fetched": 0, "sources": {}}
    results = asyncio.run(_fetch_all(srcs, states, conditional))
    summary = {}
    for src, res in results:
        items = new = 0
        err = None
        try:
            if res["ok"] and not res.get("not_modified"):
                parsed = parse.parse(res["body"], src.kind)
                r = store.ingest(src, parsed, res["fetched_at"])
                items, new = r["items"], r["new_items"]
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"[:500]
            log.warning("news source %s failed: %s", src.id, err)
        try:
            store.log_fetch(src.id, res, items, new, error=err)
        except Exception:
            log.exception("could not log fetch for %s", src.id)
        summary[src.id] = {"ok": res["ok"] and err is None, "status": res.get("status"),
                           "not_modified": bool(res.get("not_modified")), "items": items, "new_items": new,
                           "error": err or res.get("error"), "duration_ms": res.get("duration_ms")}
    return {"fetched": len(results), "sources": summary}


@job("news", every_seconds=60, initial_delay_seconds=90)
def _news_job():
    if not settings.news_enabled:
        return {"skipped": "news_enabled is False"}
    return run()
