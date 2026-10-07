"""Per-key rate limits: an in-process token bucket per (key, request class).

In-process is correct here because the app is deliberately one process (the
poller, jobs and caches all assume it). Running several replicas would give
each its own buckets, i.e. N x the budget; move this to Postgres or Redis then.

Request classes, so cheap reads are not starved by an expensive budget:
    read     GET / HEAD / OPTIONS                      settings.rate_limit_read_per_minute (120)
    write    other methods                             settings.rate_limit_write_per_minute (30)
    execute  non-GET under /v1/route, /v1/deployments  settings.rate_limit_execute_per_minute (10)
A key's `rate_limit_per_minute` override replaces the read budget and caps the
other two (an override can lower, never raise, write/execute).

Bucket: capacity = the per-minute budget (so a full minute's burst is allowed),
refilled continuously at budget/60 tokens per second.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass

from config import settings

_EXECUTE_PREFIXES = ("/v1/route", "/v1/deployments")


def request_class(method: str, path: str) -> str:
    if method.upper() in ("GET", "HEAD", "OPTIONS"):
        return "read"
    if path.startswith(_EXECUTE_PREFIXES):
        return "execute"
    return "write"


def limit_for(cls: str, override: int | None) -> int:
    default = {
        "read": settings.rate_limit_read_per_minute,
        "write": settings.rate_limit_write_per_minute,
        "execute": settings.rate_limit_execute_per_minute,
    }[cls]
    if override is None:
        return default
    return override if cls == "read" else min(default, override)


@dataclass
class Decision:
    allowed: bool
    limit: int
    remaining: int
    reset_seconds: int        # until the bucket is full again
    retry_after: int          # until one token is available (0 when allowed)
    request_class: str

    def headers(self) -> dict[str, str]:
        h = {"X-RateLimit-Limit": str(self.limit), "X-RateLimit-Remaining": str(self.remaining),
             "X-RateLimit-Reset": str(self.reset_seconds), "X-RateLimit-Class": self.request_class}
        if not self.allowed:
            h["Retry-After"] = str(self.retry_after)
        return h


class _Bucket:
    __slots__ = ("tokens", "stamp", "capacity")

    def __init__(self, capacity: int, now: float):
        self.tokens, self.stamp, self.capacity = float(capacity), now, capacity


_buckets: dict[tuple[int, str], _Bucket] = {}
_lock = threading.Lock()
clock = time.monotonic  # tests may replace


def take(key_id: int, cls: str, limit: int) -> Decision:
    rate = limit / 60.0
    now = clock()
    with _lock:
        b = _buckets.get((key_id, cls))
        if b is None or b.capacity != limit:
            b = _buckets[(key_id, cls)] = _Bucket(limit, now)
        b.tokens = min(limit, b.tokens + (now - b.stamp) * rate)
        b.stamp = now
        allowed = b.tokens >= 1.0
        if allowed:
            b.tokens -= 1.0
        tokens = b.tokens
    reset = math.ceil((limit - tokens) / rate) if rate else 0
    retry = 0 if allowed else max(1, math.ceil((1.0 - tokens) / rate))
    return Decision(allowed, limit, int(tokens), reset, retry, cls)


def status(key_id: int, override: int | None) -> dict:
    """Current budget per class for one key, without spending anything."""
    now = clock()
    out = {}
    for cls in ("read", "write", "execute"):
        limit = limit_for(cls, override)
        with _lock:
            b = _buckets.get((key_id, cls))
            tokens = limit if b is None or b.capacity != limit else min(limit, b.tokens + (now - b.stamp) * limit / 60.0)
        out[cls] = {"limit_per_minute": limit, "remaining": int(tokens)}
    return out


def reset() -> None:
    with _lock:
        _buckets.clear()
