"""Polite HTTP for power sources: one request at a time per source, paced, retried, secrets redacted.

    Client(client, source, pace_seconds, secrets=()).get(url, params=None, headers=None) -> Fetched

The body is read whole (bounded by MAX_BYTES): every power request is one market day or a few, at most a few MB
(the largest, a MISO all-node day file, is about 3 MB); nothing is written to disk. A 429 waits Retry-After (or
RETRY_SECONDS); a dropped connection, a timeout or a 5xx backs off and retries. The last answer is returned, never
raised: the runner stores failures as power_fetches rows too.
"""

from __future__ import annotations

import asyncio
import logging
import time as _time
from urllib.parse import urlencode

import httpx

from power.registry import Fetched

log = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(120.0, connect=20.0)
MAX_BYTES = 64 * 1024 * 1024
RETRY_SECONDS = 60.0
MAX_429 = 4
BACKOFF_SECONDS = (5.0, 20.0, 60.0)
USER_AGENT = "OpenGrid market data (power markets; https://opengrid.markets)"

_locks: dict[str, asyncio.Lock] = {}


class Client:
    def __init__(self, client: httpx.AsyncClient, source: str, pace_seconds: float, secrets: tuple[str, ...] = ()):
        self.client, self.source, self.pace = client, source, pace_seconds
        self.secrets = tuple(s for s in secrets if s)
        self.requests = 0
        self._last = 0.0

    def redact(self, s: str) -> str:
        for x in self.secrets:
            s = s.replace(x, "***")
        return s

    async def get(self, url: str, params=None, headers=None) -> Fetched:
        full = f"{url}?{urlencode(params, doseq=True)}" if params else url
        shown = self.redact(full)[:1000]
        lock = _locks.setdefault(self.source, asyncio.Lock())
        async with lock:
            transient = throttled = 0
            while True:
                wait = self.pace - (_time.monotonic() - self._last)
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last = _time.monotonic()
                self.requests += 1
                started = _time.perf_counter()
                try:
                    resp = await self.client.get(url, params=params,
                                                 headers={"User-Agent": USER_AGENT, **(headers or {})})
                except httpx.TransportError as exc:
                    if transient < len(BACKOFF_SECONDS):
                        await asyncio.sleep(BACKOFF_SECONDS[transient])
                        transient += 1
                        continue
                    return Fetched(shown, None, None, None, error=self.redact(f"{type(exc).__name__}: {exc}")[:500])
                ms = int((_time.perf_counter() - started) * 1000)
                if resp.status_code == 429 and throttled < MAX_429:
                    throttled += 1
                    try:
                        delay = float(resp.headers.get("retry-after") or RETRY_SECONDS)
                    except ValueError:
                        delay = RETRY_SECONDS
                    log.warning("power %s: 429, waiting %.0fs", self.source, delay)
                    await asyncio.sleep(min(delay, 300.0))
                    continue
                if resp.status_code >= 500 and transient < len(BACKOFF_SECONDS):
                    await asyncio.sleep(BACKOFF_SECONDS[transient])
                    transient += 1
                    continue
                body = resp.content
                if len(body) > MAX_BYTES:
                    return Fetched(shown, resp.status_code, ms, None, resp.headers.get("content-type"),
                                   error=f"body over {MAX_BYTES} bytes")
                err = None if resp.status_code < 400 else f"HTTP {resp.status_code}: {self.redact(resp.text[:300])}"
                return Fetched(shown, resp.status_code, ms, body, resp.headers.get("content-type"), error=err)
