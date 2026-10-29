"""The in-process agent and the rollup job.

`probe_local` wakes every 30 s, probes whichever targets are due (each target has its own interval, default 5 min,
and writes the raw results straight to the
database as this process's vantage point. `probe_rollup` rebuilds the recent hourly rollups every 5 minutes and prunes
raw results past their retention. Disabled with PROBES_LOCAL_ENABLED=false (the rollup still runs, so results posted by
remote agents are still rolled up).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

from config import settings
from jobs import job
from probes import measure, store, targets

log = logging.getLogger(__name__)

CONCURRENCY = 8
AGENT_ID = "web"
_last: dict[str, float] = {}


def location() -> str:
    raw = settings.probes_location or os.environ.get("RAILWAY_REGION") or "local"
    return "".join(c if c.isalnum() or c == "-" else "-" for c in raw.lower())[:32] or "local"


def due(now: float, last: dict[str, float] | None = None, pool: tuple[targets.Target, ...] | None = None) -> list[targets.Target]:
    """Targets never probed by this process, or last probed at least their interval ago."""
    last = _last if last is None else last
    return [t for t in (targets.TARGETS if pool is None else pool) if last.get(t.id) is None or now - last[t.id] >= t.interval_s]


async def _round(due_targets: list[targets.Target], loc: str) -> list[dict]:
    sem = asyncio.Semaphore(CONCURRENCY)
    async with measure.client() as c:
        async def one(t):
            async with sem:
                return await measure.measure(c, t, loc)
        return await asyncio.gather(*(one(t) for t in due_targets))


def probe_once() -> dict:
    now = time.time()
    batch = due(now)
    if not batch:
        return {"probed": 0}
    loc = location()
    results = asyncio.run(_round(batch, loc))
    for t in batch:
        _last[t.id] = now
    rec = store.record(results, AGENT_ID, loc)
    return {"probed": len(batch), "location": loc, "ok": sum(1 for r in results if r["ok"]), **rec}


def rollup_once() -> dict:
    out = store.rollup(hours=3)
    out["pruned_raw"] = store.prune_raw(settings.probes_raw_retention_days)
    return out


_registered = False


def register() -> None:
    global _registered
    if _registered:
        return
    _registered = True
    if settings.probes_local_enabled:
        job("probe_local", every_seconds=30, initial_delay_seconds=45)(probe_once)
    job("probe_rollup", every_seconds=300, initial_delay_seconds=120)(rollup_once)
