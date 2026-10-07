"""Periodic background jobs that run inside the web process, next to the poller.

A module registers its job at import time:

    from jobs import job

    @job("indices", every_seconds=300)
    def recompute_indices():           # sync functions run in a worker thread
        ...

The runner (started in main.py's lifespan) gives each job its own loop, so a
slow or failing job never holds up another, the same contract as the poller.
Every run is recorded in memory for the ops page: last start, duration, error.
"""

import asyncio
import inspect
import logging
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

log = logging.getLogger(__name__)


@dataclass
class Job:
    name: str
    fn: Callable
    every_seconds: float
    initial_delay_seconds: float = 30.0
    # Health, for the ops page.
    runs: int = 0
    failures: int = 0
    last_started: datetime | None = None
    last_finished: datetime | None = None
    last_duration_ms: int | None = None
    last_error: str | None = None
    last_result: object = None
    running: bool = False
    _task: asyncio.Task | None = field(default=None, repr=False)

    def describe(self) -> dict:
        return {
            "name": self.name, "every_seconds": self.every_seconds, "runs": self.runs,
            "failures": self.failures, "running": self.running,
            "last_started": self.last_started, "last_finished": self.last_finished,
            "last_duration_ms": self.last_duration_ms, "last_error": self.last_error,
        }


JOBS: dict[str, Job] = {}


def job(name: str, every_seconds: float, initial_delay_seconds: float = 30.0):
    """Register `fn` to run every `every_seconds`. Registering a name twice replaces it."""

    def register(fn):
        JOBS[name] = Job(name, fn, every_seconds, initial_delay_seconds)
        return fn

    return register


async def run_once(j: Job):
    j.running = True
    j.last_started = datetime.now(timezone.utc)
    started = time.perf_counter()
    try:
        if inspect.iscoroutinefunction(j.fn):
            result = await j.fn()
        else:
            result = await asyncio.to_thread(j.fn)
        j.last_error = None
        j.last_result = result
        return result
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        j.failures += 1
        j.last_error = f"{type(exc).__name__}: {exc}"[:1000]
        log.error("job %s failed:\n%s", j.name, traceback.format_exc())
        return None
    finally:
        j.runs += 1
        j.running = False
        j.last_finished = datetime.now(timezone.utc)
        j.last_duration_ms = int((time.perf_counter() - started) * 1000)


async def _loop(j: Job) -> None:
    await asyncio.sleep(j.initial_delay_seconds)
    while True:
        started = time.monotonic()
        await run_once(j)
        await asyncio.sleep(max(0.0, j.every_seconds - (time.monotonic() - started)))


def start() -> None:
    for j in JOBS.values():
        if j._task is None or j._task.done():
            j._task = asyncio.create_task(_loop(j), name=f"job-{j.name}")
    if JOBS:
        log.info("jobs started: %s", ", ".join(f"{j.name}/{j.every_seconds:g}s" for j in JOBS.values()))


async def stop() -> None:
    tasks = [j._task for j in JOBS.values() if j._task]
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    for j in JOBS.values():
        j._task = None
