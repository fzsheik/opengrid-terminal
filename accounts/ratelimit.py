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

Every key request ALSO spends from its ACCOUNT's bucket per class
(settings.account_rate_limit_*_per_minute, or the account's own override), so
minting more keys never multiplies an account's budget. A self-service key can
never be created with a higher limit than the key that created it.

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


_buckets: dict[tuple, _Bucket] = {}
_lock = threading.Lock()
clock = time.monotonic  # tests may replace


def _refill(bucket_id, limit: int, now: float) -> _Bucket:
    """The bucket, refilled to `now`. A changed limit keeps the tokens already spent (capped to the
    new capacity) instead of handing out a fresh full bucket, so alternating between keys or
    limits cannot reset a budget."""
    b = _buckets.get(bucket_id)
    if b is None:
        b = _buckets[bucket_id] = _Bucket(limit, now)
    else:
        b.tokens = min(float(b.capacity), b.tokens + (now - b.stamp) * b.capacity / 60.0)
        if b.capacity != limit:
            b.tokens, b.capacity = min(b.tokens, float(limit)), limit
    b.stamp = now
    return b


def _decision(allowed: bool, limit: int, tokens: float, cls: str) -> Decision:
    rate = limit / 60.0
    reset = math.ceil((limit - tokens) / rate) if rate else 0
    retry = 0 if allowed else max(1, math.ceil((1.0 - tokens) / rate))
    return Decision(allowed, limit, int(tokens), reset, retry, cls)


def take(key_id, cls: str, limit: int) -> Decision:
    return take_all([((key_id, cls), limit)], cls)


def take_all(buckets: list[tuple[tuple, int]], cls: str) -> Decision:
    """Spend one token from EVERY bucket, or from none (all-or-nothing, so a refused request does
    not drain the others). Returns the most restrictive bucket's decision."""
    now = clock()
    with _lock:
        bs = [(_refill(bid, limit, now), limit) for bid, limit in buckets]
        allowed = all(b.tokens >= 1.0 for b, _ in bs)
        if allowed:
            for b, _ in bs:
                b.tokens -= 1.0
        snap = [(b.tokens, limit) for b, limit in bs]
    if allowed:
        tokens, limit = min(snap, key=lambda t: t[0])
    else:
        tokens, limit = min((t for t in snap if t[0] < 1.0), key=lambda t: t[0] / t[1])
    return _decision(allowed, limit, tokens, cls)


def account_limit_for(cls: str, account_settings: dict | None = None) -> int:
    """The per-ACCOUNT budget for a class: settings default, or the account's own override
    (accounts.settings {"rate_limit_account": {"read": n}}, set by the operator)."""
    default = {
        "read": settings.account_rate_limit_read_per_minute,
        "write": settings.account_rate_limit_write_per_minute,
        "execute": settings.account_rate_limit_execute_per_minute,
    }[cls]
    try:
        v = int(((account_settings or {}).get("rate_limit_account") or {}).get(cls) or 0)
    except (TypeError, ValueError, AttributeError):
        v = 0
    return v if v >= 1 else default


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
