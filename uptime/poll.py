"""The `uptime` job: list UptimeProject's services, keep the ones that belong to providers OpenGrid tracks, and
for each fetch the detail (checks + per-probe latency) and the last 7 days of hourly buckets.

Hourly cadence: upstream caches data endpoints for 5 minutes and buckets by hour, so a faster poll adds nothing.
A failing service never blocks another; the fetch log records the poll as ok when the service list came back, and
carries the per-service errors. Disabled by settings.uptime_enabled = False.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from config import settings
from jobs import job
from uptime import fetch, store

log = logging.getLogger(__name__)

CONCURRENCY = 4
PERIOD = "7d"  # hourly buckets (169 points); every longer period is daily upstream
DAILY_REFRESH = "30d"  # keeps the recent days current (today's bucket fills during the day)
DAILY_BACKFILL = "1y"  # all the daily history upstream has (it keeps aggregates indefinitely; raw only 90 days)
BACKFILL_BELOW = 30    # a service with fewer stored daily buckets than this is backfilled


async def _collect(rows: list[dict], c, have_daily: dict[str, int]) -> dict[str, dict]:
    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(row: dict):
        async with sem:
            try:
                detail = fetch.parse_detail(await fetch.get_json(c, f"/v1/services/{row['id']}"))
                points = fetch.parse_points(await fetch.get_json(c, f"/v1/services/{row['id']}/uptime", {"period": PERIOD}))
                period = DAILY_BACKFILL if have_daily.get(row["id"], 0) < BACKFILL_BELOW else DAILY_REFRESH
                daily = fetch.parse_points(await fetch.get_json(c, f"/v1/services/{row['id']}/uptime", {"period": period}))
                return row["id"], {"detail": detail, "points": points, "daily": daily, "error": None}
            except Exception as exc:  # noqa: BLE001 - recorded per service, never raised
                return row["id"], {"detail": None, "points": [], "daily": [], "error": f"{type(exc).__name__}: {exc}"[:300]}

    return dict(await asyncio.gather(*(one(r) for r in rows)))


async def _run() -> tuple[list[dict], dict[str, dict]]:
    async with fetch.client() as c:
        rows = fetch.parse_services(await fetch.get_json(c, "/v1/services"))
        return rows, await _collect(rows, c, store.daily_counts())


def run() -> dict:
    started = time.perf_counter()
    now = datetime.now(timezone.utc)
    try:
        rows, results = asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001 - the service list itself failed
        err = f"{type(exc).__name__}: {exc}"[:500]
        store.log_fetch(now, False, 0, 0, err, int((time.perf_counter() - started) * 1000))
        log.warning("uptime poll failed: %s", err)
        return {"ok": False, "error": err}
    points = 0
    errors = []
    for row in rows:
        store.upsert_service(row, now)
        res = results[row["id"]]
        if res["error"]:
            errors.append(f"{row['id']}: {res['error']}")
            continue
        store.save_detail(row["id"], res["detail"], now)
        points += store.save_points(row["id"], res["points"], now)
        points += store.save_daily(row["id"], res["daily"], now)
    store.log_fetch(now, True, len(rows), points, "; ".join(errors)[:1000] if errors else None,
                    int((time.perf_counter() - started) * 1000))
    pruned = store.prune(settings.uptime_probe_retention_days)
    return {"ok": True, "services": len(rows), "points": points, "failed": len(errors), "pruned": pruned}


_registered = False


def register() -> None:
    """Idempotent: importing api.uptime calls this once."""
    global _registered
    if _registered or not settings.uptime_enabled:
        return
    _registered = True
    job("uptime", every_seconds=settings.uptime_poll_seconds, initial_delay_seconds=90)(run)
