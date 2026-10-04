"""Background polling, one independent loop per provider.

Each provider keeps its own cadence from its `polling` policy, so a slow or
failing provider never holds up the others. A cycle that fails is logged and
the loop carries on at the same interval.

After a provider's fetch only that provider is re-normalized, so Salad's
minute does not cost a re-read of everyone else's raw data.
"""

import asyncio
import logging
import time

import normalize
from fetch import build_fetchers, save
from providers.base import Provider

log = logging.getLogger(__name__)


async def poll_once(provider: Provider) -> dict:
    """One cycle: fetch, store raw, normalize just this provider."""
    started = time.perf_counter()
    async with asyncio.timeout(provider.polling.timeout_seconds):
        responses = await provider.fetch()

    failed = sum(1 for r in responses if not r.ok)
    await asyncio.to_thread(save, responses)
    result = await asyncio.to_thread(normalize.refresh, [provider.name])
    result["responses"] = len(responses)
    result["failed"] = failed
    result["ms"] = int((time.perf_counter() - started) * 1000)
    return result


async def poll_provider(provider: Provider) -> None:
    """Poll one provider forever on its own policy."""
    interval = provider.polling.interval_seconds
    while True:
        started = time.monotonic()
        try:
            result = await poll_once(provider)
            log.info(
                "%s: %d responses (%d failed), %d listings, %d changes, %dms",
                provider.name,
                result["responses"],
                result["failed"],
                result["listings"],
                result["observations"],
                result["ms"],
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("%s: poll failed", provider.name)
        # Measure from the start of the cycle so a slow fetch does not drift.
        await asyncio.sleep(max(0.0, interval - (time.monotonic() - started)))


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
        log.info(
            "polling started: %s",
            ", ".join(f"{p.name}/{p.polling.interval_seconds:g}s" for p in self.providers) or "none",
        )

    def describe(self) -> str:
        if not self.providers:
            return "none configured"
        return ", ".join(f"{p.name}/{p.polling.interval_seconds:g}s" for p in self.providers)

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        for provider in self.providers:
            await provider.aclose()
