"""Background polling, one independent loop per provider.

Each provider keeps its own cadence from its `polling` policy, so a slow or
failing provider never holds up the others. A cycle that fails is logged and
the loop carries on at the same interval.

Cadences are tiered (providers/base.PollingPolicy; methodology/freshness.md): a price cadence and a capacity
cadence per provider, and a loop that runs at the shorter of the two (or faster, for a sub-feed such as AWS spot).
Before each fetch the provider is told which cadences are due (Provider.due), so one with separate price and
capacity endpoints refreshes each on its own clock. A provider that asked us to wait (429, Retry-After,
RateLimit-Reset) is not polled again before that wait is over.

After a provider's fetch only that provider is re-normalized, so Salad's
minute does not cost a re-read of everyone else's raw data.
"""

import asyncio
import logging
import time

import freshness
import normalize
from fetch import build_fetchers, save
from live.locks import refresh_provider
from providers.base import CAPACITY, PRICE, PollingPolicy, Provider

log = logging.getLogger(__name__)

# The providers this process is actually polling, name -> loop interval seconds (set when polling starts). The
# silence watch (alerts/silence.py) reads it: a provider whose fetcher was never built (no API key, POLLER_ENABLED
# off) is not here, so it is never alerted on.
ACTIVE: dict[str, float] = {}
# A cadence counts as due this many seconds early, so a 360 s price cadence on a 120 s loop runs every third loop.
SLACK_SECONDS = 5.0


class Schedule:
    """When each cadence (price, capacity) of one provider last ran, and which are due now."""

    def __init__(self, policy: PollingPolicy):
        self.policy = policy
        self.last: dict[str, float] = {}

    def due(self, now: float) -> frozenset:
        return frozenset(s for s in (PRICE, CAPACITY)
                         if s not in self.last or now - self.last[s] >= self.policy.cadence(s) - SLACK_SECONDS)

    def ran(self, streams, now: float) -> None:
        for s in streams:
            self.last[s] = now


async def poll_once(provider: Provider) -> dict:
    """One cycle: fetch, store raw, normalize just this provider."""
    started = time.perf_counter()
    async with asyncio.timeout(provider.polling.timeout_seconds):
        responses = await provider.fetch()

    throttled = sum(1 for r in responses if r.throttled)
    failed = sum(1 for r in responses if not r.ok and not r.throttled)
    await asyncio.to_thread(save, responses)
    # one normalize per provider at a time: a live stream (live/lium_stream.py) may re-normalize it too
    result = await asyncio.to_thread(refresh_provider, provider.name)
    await asyncio.to_thread(freshness.record, provider.name)  # fails open
    result["responses"] = len(responses)
    result["failed"] = failed
    result["throttled"] = throttled
    result["ms"] = int((time.perf_counter() - started) * 1000)
    return result


def _last_ok(name: str) -> dict:
    """Each endpoint's newest successful fetch time for one provider (normalize's loose index scan)."""
    with normalize.SessionLocal() as s:
        return dict(s.execute(normalize._NEWEST_PER_ENDPOINT, {"p": name}).all())


async def _resume(provider: Provider) -> None:
    try:
        provider.resume(await asyncio.to_thread(_last_ok, provider.name))
    except Exception:  # noqa: BLE001 - a provider that cannot resume starts fresh
        log.exception("%s: could not read its last stored poll; starting fresh", provider.name)


async def poll_provider(provider: Provider) -> None:
    """Poll one provider forever on its own policy."""
    policy = provider.polling
    schedule = Schedule(policy)
    await _resume(provider)
    while True:
        started = time.monotonic()
        due = schedule.due(started)
        provider.due = due
        try:
            result = await poll_once(provider)
            schedule.ran(due, started)
            log.info(
                "%s [%s]: %d responses (%d failed, %d throttled), %d listings, %d changes, %dms",
                provider.name,
                "+".join(sorted(due)) or "sub-feed",
                result["responses"],
                result["failed"],
                result["throttled"],
                result["listings"],
                result["observations"],
                result["ms"],
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("%s: poll failed", provider.name)
        # Measure from the start of the cycle so a slow fetch does not drift; never call before a requested wait.
        wait = max(0.0, policy.loop_seconds - (time.monotonic() - started))
        hold = getattr(provider, "blocked_until", 0.0) - time.time()
        if hold > wait:
            log.info("%s: rate limited; next poll in %.0fs", provider.name, hold)
            wait = hold
        await asyncio.sleep(wait)


def describe_policy(p: Provider) -> str:
    pol = p.polling
    parts = [f"{pol.tier}", f"price {pol.interval_seconds:g}s"]
    if pol.capacity_interval_seconds:
        parts.append(f"capacity {pol.capacity_interval_seconds:g}s")
    if pol.poll_seconds:
        parts.append(f"loop {pol.poll_seconds:g}s")
    return f"{p.name}/" + ",".join(parts)


class Ingest:
    """Owns one polling task per configured provider."""

    def __init__(self) -> None:
        self.providers: list[Provider] = build_fetchers()
        self._tasks: list[asyncio.Task] = []

    @property
    def running(self) -> bool:
        return any(not t.done() for t in self._tasks)

    def start(self) -> None:
        if self._tasks:
            return
        self._tasks = [
            asyncio.create_task(poll_provider(p), name=f"poll-{p.name}") for p in self.providers
        ]
        ACTIVE.clear()
        ACTIVE.update({p.name: p.polling.loop_seconds for p in self.providers})
        log.info("polling started: %s", ", ".join(describe_policy(p) for p in self.providers) or "none")

    def describe(self) -> str:
        if not self.providers:
            return "none configured"
        return ", ".join(describe_policy(p) for p in self.providers)

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        ACTIVE.clear()
        for provider in self.providers:
            await provider.aclose()
