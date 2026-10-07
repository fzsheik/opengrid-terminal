"""Fetch one source: conditional GET, size cap, never raises.

Conditional GET: the ETag / Last-Modified the source sent last time go back as
If-None-Match / If-Modified-Since; a 304 is a healthy fetch with nothing new, so polite
polling of a quiet feed costs the publisher almost nothing.

Bodies over MAX_BYTES are refused (recorded as a failed fetch), so one runaway archive
feed cannot eat memory. Every outcome, including exceptions and timeouts, comes back as a
result dict for the fetch log; nothing here raises.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import httpx

from config import settings

MAX_BYTES = 12 * 1024 * 1024
TIMEOUT_SECONDS = 20.0
USER_AGENT = f"OpenGridTerminal-News/1.0 (+{settings.public_base_url})"
ACCEPT = ("application/rss+xml, application/atom+xml, application/feed+json, application/json;q=0.9, "
          "application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5")


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(headers={"User-Agent": USER_AGENT, "Accept": ACCEPT},
                             timeout=httpx.Timeout(TIMEOUT_SECONDS), follow_redirects=True)


async def fetch(c: httpx.AsyncClient, url: str, etag: str | None = None, last_modified: str | None = None) -> dict:
    started_wall = datetime.now(timezone.utc)
    started = time.perf_counter()
    headers = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    out = {"fetched_at": started_wall, "ok": False, "status": None, "not_modified": False, "error": None,
           "body": None, "etag": None, "last_modified": None, "bytes": None, "final_url": None}
    try:
        async with c.stream("GET", url, headers=headers) as resp:
            out["status"] = resp.status_code
            out["final_url"] = str(resp.url)
            out["etag"] = resp.headers.get("etag")
            out["last_modified"] = resp.headers.get("last-modified")
            if resp.status_code == 304:
                out.update(ok=True, not_modified=True, etag=out["etag"] or etag,
                           last_modified=out["last_modified"] or last_modified)
            elif resp.is_success:
                chunks, size = [], 0
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise ValueError(f"body larger than {MAX_BYTES} bytes")
                    chunks.append(chunk)
                out.update(ok=True, body=b"".join(chunks), bytes=size)
            else:
                body = (await resp.aread())[:300].decode("utf-8", "replace")
                out["error"] = f"HTTP {resp.status_code}: {body.strip()[:200]}"
    except Exception as exc:  # timeouts, DNS, TLS, size cap: all recorded, never raised
        out.update(ok=False, body=None, error=f"{type(exc).__name__}: {exc}"[:500])
    out["duration_ms"] = int((time.perf_counter() - started) * 1000)
    return out
