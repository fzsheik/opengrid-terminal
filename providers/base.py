"""The provider interface.

A provider owns both halves of its own integration in one file:

    fetch()      hits its endpoints and returns the responses untouched
    normalize()  turns stored raw snapshots into ComputeListing rows

normalize() is a staticmethod and never touches the network, so the normalizer
can be re-run over stored history without credentials.
"""

import asyncio
import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

import httpx

from models import CapacityObservation, ComputeListing, RawResponse

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from tables import RawSnapshot


TIERS = ("fast", "medium", "slow")
PRICE, CAPACITY = "price", "capacity"


@dataclass(frozen=True)
class PollingPolicy:
    """How often, and how patiently, one provider gets polled (methodology/freshness.md).

    Declared per provider because the APIs are not alike: a Salad poll costs 44
    requests against a 240/min key limit, Hyperstack 5 against 500/min per IP,
    and Lambda 3 against 1/s.

        interval_seconds           PRICE cadence: the longest gap between two refreshes of any of its price
                                   listings. Staleness (market, trust block, routing) and silence alerts key off it.
        capacity_interval_seconds  CAPACITY cadence, when it differs: shorter where stock is cheap to re-read on its
                                   own (Verda), longer where it is budgeted (Vast's keyed census) or slow (AWS
                                   placement scores). None: capacity refreshes with every poll.
        poll_seconds               how often the loop runs when a feed inside fetch() is faster than both (AWS and
                                   Nebius spot every 5 min while their list prices stay hourly). None: the shorter
                                   of the two cadences.
        tier                       fast (1-2 min) | medium (5-15 min) | slow (1-6 h); derived from the loop
                                   cadence when not given.
        rate_limit                 (requests, per_seconds) this provider's calls may spend, a share of its
                                   documented limit; a token bucket in Provider.call() enforces it. None: no
                                   budget beyond the provider's own pacing.

    Each poll is told which cadences are due (Provider.due) so a provider whose price and capacity endpoints are
    separate can skip the one that is not; one that cannot split fetches both on the faster cadence.
    """

    interval_seconds: float
    timeout_seconds: float = 30.0
    capacity_interval_seconds: float | None = None
    poll_seconds: float | None = None
    tier: str = ""
    rate_limit: tuple[int, float] | None = None

    def __post_init__(self):
        if not self.tier:
            loop = self.loop_seconds
            object.__setattr__(self, "tier", "fast" if loop <= 120 else "medium" if loop <= 900 else "slow")
        if self.tier not in TIERS:
            raise ValueError(f"unknown polling tier {self.tier!r}; one of {TIERS}")

    @property
    def loop_seconds(self) -> float:
        """How often the poll loop runs."""
        return float(self.poll_seconds or min(self.interval_seconds,
                                              self.capacity_interval_seconds or self.interval_seconds))

    @property
    def capacity_seconds(self) -> float:
        """The expected gap between capacity refreshes (the capacity stale flag and silence alert use it)."""
        return float(self.capacity_interval_seconds or self.loop_seconds)

    def cadence(self, stream: str) -> float:
        return self.capacity_seconds if stream == CAPACITY else float(self.interval_seconds)


# A call waits inline for a rate-limit window at most this long; beyond it the call is not made this poll.
MAX_INLINE_WAIT = 15.0
# Back-off after a 429 that names no wait: 2 s, doubling per consecutive 429, at most the poll loop (and an hour).
BACKOFF_FIRST, BACKOFF_MAX = 2.0, 3600.0


class RateBudget:
    """A token bucket: `requests` tokens, refilled at requests / per_seconds. acquire() waits for a token."""

    def __init__(self, requests: int, per_seconds: float):
        self.capacity, self.rate = float(requests), float(requests) / float(per_seconds)
        self.tokens, self.at = self.capacity, time.monotonic()

    def wait_for(self) -> float:
        """Take a token; return how long to sleep before using it (0 when one was free)."""
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.at) * self.rate)
        self.at = now
        self.tokens -= 1
        return 0.0 if self.tokens >= 0 else -self.tokens / self.rate

    async def acquire(self) -> None:
        wait = self.wait_for()
        if wait > 0:
            await asyncio.sleep(wait)


def _seconds(value: str | None, now: float) -> float | None:
    """A Retry-After / reset value: delta seconds, an epoch (> 1e9) or an HTTP date -> seconds from now."""
    if value is None:
        return None
    v = str(value).strip()
    try:
        n = float(v)
    except ValueError:
        try:
            from email.utils import parsedate_to_datetime

            return max(0.0, parsedate_to_datetime(v).timestamp() - now)
        except (TypeError, ValueError, IndexError):
            return None
    return max(0.0, n - now) if n > 1e9 else max(0.0, n)


def rate_limit_wait(headers, now: float | None = None) -> float | None:
    """Seconds the provider asks us to wait, from its headers, or None when it asks nothing.

    Reads Retry-After; and, when the remaining quota is 0, the reset of RateLimit-* / X-RateLimit-* / ratelimit-*
    (RunPod, Verda, DigitalOcean, Linode) or of the structured `RateLimit: remaining=0, reset=30` header.
    Azure's x-ms-ratelimit-remaining-* counts are read as remaining (no reset is given: back off a minute).
    """
    now = time.time() if now is None else now
    h = {k.lower(): v for k, v in (headers or {}).items()}
    wait = _seconds(h.get("retry-after"), now)
    if wait is not None:
        return wait
    for prefix in ("ratelimit-", "x-ratelimit-", "x-rate-limit-"):
        remaining = h.get(prefix + "remaining")
        if remaining is not None and remaining.strip().split(",")[0].strip() in ("0", "0.0"):
            return _seconds(h.get(prefix + "reset"), now) or 1.0
    structured = h.get("ratelimit")
    if structured and "remaining=" in structured:
        parts = dict(p.strip().split("=", 1) for p in structured.split(",") if "=" in p)
        if parts.get("remaining", "").strip() == "0":
            return _seconds(parts.get("reset"), now) or 1.0
    if any(k.startswith("x-ms-ratelimit-remaining") and str(v).strip() == "0" for k, v in h.items()):
        return 60.0
    return None


class Provider(ABC):
    """One provider integration: how to fetch it, how to read it, how often."""

    name: str
    polling: PollingPolicy
    # Which cadences this poll serves (PRICE, CAPACITY); the poller sets it before each fetch(). A provider with
    # separate price and capacity endpoints skips the one not due; the default (both) is a full fetch.
    due: frozenset = frozenset({PRICE, CAPACITY})

    def __init__(self, client: httpx.AsyncClient):
        self._client = client
        self._budget: RateBudget | None = None
        self.blocked_until = 0.0      # time.time() before which the provider asked us not to call
        self._backoff = 0.0           # the last no-header back-off, doubled on each consecutive 429

    async def fetch(self) -> list[RawResponse]:
        """Pull every endpoint we care about, in whatever order suits the API."""

    async def aclose(self) -> None:
        await self._client.aclose()

    def resume(self, last_ok: "dict[str, datetime]") -> None:
        """Called once by the poller before the first poll with each endpoint's newest stored successful fetch time,
        so a provider that queries "since the last poll" (AWS spot) can pick up where a previous process stopped.
        Default: nothing to resume."""

    # -- request budget and rate-limit headers (generic; see PollingPolicy.rate_limit) --

    async def _before_call(self, path: str, method: str = "GET") -> RawResponse | None:
        """Spend a token and honour a pending provider wait. Returns a throttled response (not sent) when the
        provider asked for more than MAX_INLINE_WAIT; None when the call may go ahead."""
        wait = getattr(self, "blocked_until", 0.0) - time.time()
        if wait > MAX_INLINE_WAIT:
            return RawResponse(provider=self.name, endpoint=path, method=method, fetched_at=datetime.now(timezone.utc),
                               ok=False, throttled=True,
                               error=f"throttled: {self.name} asked us to wait {wait:.0f}s more; not called")
        if wait > 0:
            await asyncio.sleep(wait)
        limit = getattr(self.polling, "rate_limit", None)
        if limit:
            if getattr(self, "_budget", None) is None:
                self._budget = RateBudget(*limit)
            await self._budget.acquire()
        return None

    def _after_response(self, resp, backoff: bool = True) -> bool:
        """Read the rate-limit headers; True when this response is a throttle (HTTP 429)."""
        throttled = getattr(resp, "status_code", None) == 429
        try:
            wait = rate_limit_wait(resp.headers)
        except Exception:  # noqa: BLE001 - a malformed header never fails a call
            wait = None
        if throttled and backoff:
            if wait is None:
                self._backoff = min(max(BACKOFF_FIRST, getattr(self, "_backoff", 0.0) * 2), BACKOFF_MAX,
                                    max(BACKOFF_FIRST, getattr(self.polling, "loop_seconds", BACKOFF_MAX)))
                wait = self._backoff
            log.warning("%s: throttled (429); backing off %.0fs", self.name, wait)
        elif not throttled:
            self._backoff = 0.0
        if wait is not None and backoff:
            self.blocked_until = max(getattr(self, "blocked_until", 0.0), time.time() + wait)
        return throttled

    async def call(self, method: str, path: str, *, client: httpx.AsyncClient | None = None, backoff: bool = True,
                   **kw) -> RawResponse:
        """One HTTP call, recorded whether or not it worked. `client` overrides the provider's own (a second API
        host, such as GCP's Compute API beside its Billing API).

        Spends the provider's request budget, waits out a short provider-requested pause, and reads rate-limit
        headers. A 429 is recorded as `throttled` (not a failure); `backoff=False` records it without pausing
        the provider's other calls (a separately limited identity, e.g. Vast's keyed census quota)."""
        skipped = await self._before_call(path, method)
        if skipped is not None:
            return skipped
        started_wall = datetime.now(timezone.utc)
        started = time.perf_counter()
        request_body = kw.get("json")
        try:
            resp = await (client or self._client).request(method, path, **kw)
        except Exception as exc:
            return RawResponse(
                provider=self.name,
                endpoint=path,
                method=method,
                fetched_at=started_wall,
                duration_ms=int((time.perf_counter() - started) * 1000),
                ok=False,
                request=request_body,
                error=f"{type(exc).__name__}: {exc}",
            )
        elapsed = int((time.perf_counter() - started) * 1000)
        throttled = self._after_response(resp, backoff)
        try:
            body = resp.json()
        except (json.JSONDecodeError, ValueError):
            body = None
        return RawResponse(
            provider=self.name,
            endpoint=path,
            method=method,
            fetched_at=started_wall,
            status_code=resp.status_code,
            duration_ms=elapsed,
            ok=resp.is_success,
            throttled=throttled,
            error=None if resp.is_success else resp.text[:500],
            request=request_body,
            payload=body,
        )

    async def get(self, path: str, **kw) -> RawResponse:
        return await self.call("GET", path, **kw)

    async def post(self, path: str, **kw) -> RawResponse:
        return await self.call("POST", path, **kw)

    @staticmethod
    @abstractmethod
    def normalize(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        """Turn this provider's stored raw snapshots into listings.

        `by_endpoint` maps endpoint path -> the snapshots from the newest fetch
        round, so an endpoint called many times per fetch (Salad's per-class
        availability) arrives as a list.
        """

    @staticmethod
    def normalize_capacity(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[CapacityObservation]:
        """Turn the same raw snapshots into capacity observations (capacity.py).

        Optional: a provider that publishes no capacity signal keeps this default. Like
        normalize(), it never touches the network, so it can be re-run over stored history.
        """
        return []


async def gather_limited(factories, limit: int = 4) -> list:
    """Run calls with a concurrency cap, so we stay under rate limits.

    Takes zero-arg callables rather than coroutines: a cancelled batch would
    otherwise leave the not-yet-started coroutines never awaited.
    """
    sem = asyncio.Semaphore(limit)

    async def run(make):
        async with sem:
            return await make()

    return await asyncio.gather(*(run(f) for f in factories))


def reshaped(raw: RawResponse, payload, request=None) -> RawResponse:
    """The same response with its payload replaced by the fields we read.

    For providers whose bodies are huge or noisy (live GPU utilization, a whole
    HTML page), storing the body untouched would bloat the table and make every
    poll look different. The listing fields are kept verbatim; the rest is not.
    """
    update = {"payload": payload}
    if request is not None:
        update["request"] = request
    return raw.model_copy(update=update)


async def get_page(provider: "Provider", path: str, extract) -> RawResponse:
    """GET an HTML page and store what `extract(html)` pulls out of it.

    A page that changed shape raises inside `extract`; that is recorded as a
    failed response, never as a healthy provider with no prices.
    """
    skipped = await provider._before_call(path)
    if skipped is not None:
        return skipped
    started_wall = datetime.now(timezone.utc)
    started = time.perf_counter()
    resp = None
    try:
        resp = await provider._client.get(path)
        provider._after_response(resp)
        resp.raise_for_status()
        payload = extract(resp.text)
    except Exception as exc:
        return RawResponse(
            provider=provider.name,
            endpoint=path,
            fetched_at=started_wall,
            status_code=getattr(resp, "status_code", None),
            duration_ms=int((time.perf_counter() - started) * 1000),
            ok=False,
            throttled=getattr(resp, "status_code", None) == 429,
            error=f"{type(exc).__name__}: {exc}"[:500],
        )
    return RawResponse(
        provider=provider.name,
        endpoint=path,
        fetched_at=started_wall,
        status_code=resp.status_code,
        duration_ms=int((time.perf_counter() - started) * 1000),
        ok=True,
        payload=payload,
    )


def to_decimal(value) -> Decimal | None:
    """Parse a price without letting a bad value take down a whole poll."""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        log.warning("could not read %r as a price", value)
        return None


def find_endpoint(
    by_endpoint: "dict[str, list[RawSnapshot]]", fragment: str
) -> "list[RawSnapshot]":
    """Snapshots whose endpoint contains `fragment`."""
    return [r for ep, rows in by_endpoint.items() if fragment in ep for r in rows]
