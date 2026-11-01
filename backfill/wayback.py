"""The Internet Archive's Wayback Machine: list a page's captures and read one capture's original bytes.

    captures(url, since, until)  -> [Capture]   one per UTC day (the day's first 200 capture), oldest first
    body(capture)                -> bytes        the archived response exactly as captured (the `id_` form:
                                                 no Wayback toolbar, no rewritten links)

Polite by construction: one request at a time, at least MIN_INTERVAL seconds apart, exponential back-off on 429 /
5xx, and every body is cached on disk by its archive URL, so a re-run reads nothing twice. The Archive asks bulk
readers to stay well under ~15 requests a minute on the CDX server; MIN_INTERVAL keeps us there.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

try:
    import brotli  # noqa: F401  (lets httpx decode Content-Encoding: br)
    _BROTLI = True
except ImportError:
    _BROTLI = False

CDX = "https://web.archive.org/cdx/search/cdx"
MIN_INTERVAL = 4.5
USER_AGENT = "opengrid-terminal/0.1 (price-history backfill; one request at a time)"
DEFAULT_CACHE = Path(os.environ.get("OPENGRID_ARCHIVE_CACHE", Path.home() / ".cache" / "opengrid" / "wayback"))


@dataclass(frozen=True)
class Capture:
    url: str            # the original URL as the Archive recorded it
    timestamp: str      # YYYYMMDDhhmmss, UTC
    mimetype: str
    digest: str         # the Archive's SHA-1 (base32) of the body

    @property
    def captured_at(self) -> datetime:
        return datetime.strptime(self.timestamp, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)

    @property
    def archive_url(self) -> str:
        """The human-viewable capture (what a reader clicks to check the number)."""
        return f"https://web.archive.org/web/{self.timestamp}/{self.url}"

    @property
    def raw_url(self) -> str:
        return f"https://web.archive.org/web/{self.timestamp}id_/{self.url}"


class Wayback:
    def __init__(self, cache_dir: Path | str | None = None, client: httpx.Client | None = None,
                 min_interval: float = MIN_INTERVAL, log=print):
        self.cache = Path(cache_dir or DEFAULT_CACHE)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.client = client or httpx.Client(headers={"user-agent": USER_AGENT}, timeout=90, follow_redirects=True)
        self.min_interval = min_interval
        self.log = log
        self._last = 0.0

    def _get(self, url: str, params: dict | None = None) -> httpx.Response:
        delay = 10.0
        for attempt in range(7):
            wait = self._last + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                r = self.client.get(url, params=params)
            except httpx.TransportError as exc:
                self.log(f"wayback: {type(exc).__name__} on {url}; retry in {delay:.0f}s")
            else:
                if r.status_code not in (429, 500, 502, 503, 504):
                    return r
                self.log(f"wayback: HTTP {r.status_code} on {url}; retry in {delay:.0f}s")
            time.sleep(delay)
            delay = min(delay * 2, 300)
        raise RuntimeError(f"wayback: gave up on {url}")

    def _cached(self, key: str, suffix: str) -> Path:
        return self.cache / f"{hashlib.sha256(key.encode()).hexdigest()}{suffix}"

    def captures(self, url: str, since: str, until: str, *, refresh: bool = False) -> list[Capture]:
        """Successful captures of `url` between YYYYMMDD `since` and `until`, at most one per UTC day.
        The listing is cached per (url, since, until); refresh=True re-asks (new captures appear daily)."""
        path = self._cached(f"cdx|{url}|{since}|{until}", ".cdx.json")
        if path.exists() and not refresh:
            rows = json.loads(path.read_text())
        else:
            r = self._get(CDX, {"url": url, "from": since, "to": until, "filter": "statuscode:200",
                                "collapse": "timestamp:8", "output": "json",
                                "fl": "original,timestamp,mimetype,digest"})
            r.raise_for_status()
            rows = r.json() if r.text.strip() else []
            path.write_text(json.dumps(rows))
        return [Capture(url=o, timestamp=t, mimetype=m, digest=d) for o, t, m, d in rows[1:]]

    def body(self, capture: Capture) -> bytes:
        path = self._cached(capture.raw_url, ".body")
        if path.exists():
            return path.read_bytes()
        r = self._get(capture.raw_url)
        r.raise_for_status()
        if r.headers.get("content-encoding", "").strip().lower() == "br" and not _BROTLI:
            # The Archive serves some captures brotli-encoded whatever we accept; httpx decodes them only when
            # the brotli package is installed (the "backfill" extra). Never cache undecoded bytes.
            raise RuntimeError("wayback: capture is brotli-encoded; install the backfill extra (uv sync --extra backfill)")
        path.write_bytes(r.content)
        return r.content
