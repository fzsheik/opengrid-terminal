"""Backfill and forward runs over the registered power datasets (power/registry.py).

    run(mode, sources=None, since=None, until=None, dry_run=False)   "backfill" | "forward"; one task per source
                                                                     (sources in parallel, one request at a time
                                                                     within a source)
    status()                                                         per dataset: series, days, depth, bytes

backfill  every market day from max(since, the source's earliest) to its latest complete day that is not already
          stored complete; missing days grouped into runs of at most Dataset.chunk_days per request. Each chunk is
          fetched, recorded (url + sha256 only: the backfill keeps no bytes), parsed and committed on its own, so an
          interrupted backfill (a deploy restarts the container) resumes where it stopped: re-running it skips
          what is stored. Days the source answered "no data" for are recorded and skipped too.
forward   the last stored day minus Dataset.overlap_days (late corrections become counted revisions) to the latest
          complete day; with nothing stored, the last FORWARD_INITIAL_DAYS. Forward fetches keep the received bytes
          (compressed) where Dataset.keep_raw (the per-day size was measured small), else url + sha256.

Memory is bounded by one chunk's response (a few MB at most); nothing is written to disk.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from datetime import date, datetime, timedelta, timezone

import httpx

from power import registry, storage
from power.http import TIMEOUT, Client
from power.registry import Dataset, Fetched

log = logging.getLogger("power")

UTC = timezone.utc
FORWARD_INITIAL_DAYS = 7
POLL_SECONDS = 12 * 3600   # day-level data: twice a day catches CAISO's DA posting within half a day


def _secrets(source: str) -> tuple[str, ...]:
    from config import settings

    return tuple(str(getattr(settings, r, "") or "") for r in getattr(registry.module(source), "REQUIRES", ()))


def chunks(missing: list[date], size: int, align: str | None = None) -> list[tuple[date, date]]:
    """Contiguous runs of missing days, each at most `size` days; with align="month" never across a month."""
    out: list[tuple[date, date]] = []
    for d in sorted(missing):
        same_month = align != "month" or (out and (out[-1][0].year, out[-1][0].month) == (d.year, d.month))
        if out and same_month and (d - out[-1][1]).days == 1 and (d - out[-1][0]).days < size:
            out[-1] = (out[-1][0], d)
        else:
            out.append((d, d))
    return out


def plan(ds: Dataset, mode: str, now: datetime, since: date | None = None, until: date | None = None) -> dict:
    latest = ds.latest(now)
    if until:
        latest = min(latest, until)
    if mode == "backfill":
        first = max(since or ds.earliest(now), ds.earliest(now))
        empty_before = ds.latest(now) - timedelta(days=ds.overlap_days)   # the source's own edge, not --until
        skip = storage.done_days(ds, first, latest, empty_before) if first <= latest else set()
    else:
        last = storage.latest_day(ds)
        first = (last - timedelta(days=ds.overlap_days)) if last else latest - timedelta(days=FORWARD_INITIAL_DAYS - 1)
        first = max(first, ds.earliest(now))
        if since:
            first = max(first, since)
        keep_from = latest - timedelta(days=ds.overlap_days)
        # periodic series (one row per week / month): one request re-reads the whole window, nothing skipped
        skip = set() if ds.interval_minutes >= registry.DAILY else {
            d for d in storage.done_days(ds, first, latest) if d < keep_from}
    missing = [d for d in storage.days(first, latest) if d not in skip] if first <= latest else []
    return {"dataset": ds.key, "first": first, "latest": latest, "skipped": len(skip),
            "missing": len(missing), "chunks": chunks(missing, ds.chunk_days, ds.align)}


def _days(a: date, b: date) -> set[date]:
    return set(storage.days(a, b))


async def run_chunk(ds: Dataset, get: Client, a: date, b: date, ids: dict[str, int], keep: bool,
                    now: datetime | None = None) -> dict:
    """Fetch a chunk (one or more answers: pages, per-day files), record every answer raw-first, merge what parsed
    and upsert it once, so answers that each carry part of a day (EIA pages) never overwrite each other.

    Stored days are marked PER ANSWER: an answer that succeeded marks its own days (f.start..f.end) stored, unless a
    failed answer covers them too (a failed page of a paged query covers the whole chunk, so nothing is marked).
    A "not published" answer (Dataset.empty_statuses, or f.empty set by fetch / parse) is empty, not a failure;
    the runner re-asks empty and unmarked days while they are recent (power/storage.done_days)."""
    out = {"fetches": 0, "failed": 0, "empty": 0, "new": 0, "revised": 0, "unchanged": 0}
    try:
        if "now" in inspect.signature(ds.fetch).parameters:
            answers = await ds.fetch(get, a, b, now=now)
        else:
            answers = await ds.fetch(get, a, b)
    except Exception as exc:  # noqa: BLE001 - a fetch() bug is a failed fetch, recorded
        answers = [Fetched(f"{ds.key}:{a}..{b}", None, None, None, error=get.redact(f"{type(exc).__name__}: {exc}")[:500])]
    merged: dict[str, dict] = {}
    recorded: list[tuple[int, Fetched]] = []
    failed_days: set[date] = set()
    for f in answers:
        f.start, f.end = f.start or a, f.end or b
        out["fetches"] += 1
        if not f.ok and f.status in ds.empty_statuses:
            f.empty, f.error = True, None          # not published (yet): recorded, re-asked while recent
        if f.ok:
            try:
                for key, pts in ds.parse(f).items():
                    merged.setdefault(key, {}).update(pts)
            except Exception as exc:  # noqa: BLE001 - stored as a failed parse, never silently
                f.error = f"parse: {type(exc).__name__}: {exc}"[:500]
                f.empty = False
        fid = await asyncio.to_thread(storage.record_fetch, ds, f, keep)   # raw first
        recorded.append((fid, f))
        if f.empty and (f.ok or not f.error):
            out["empty"] += 1
        elif not f.ok:
            failed_days |= _days(f.start, f.end)
            out["failed"] += 1
            log.warning("power %s %s..%s: %s", ds.key, f.start, f.end, f.error or f.status)
    parsed = storage.to_days(ds, merged, a, b) if merged else {}
    res = {"new": 0, "revised": 0, "unchanged": 0}
    if parsed:
        res = await asyncio.to_thread(storage.upsert_days, parsed, ids, recorded[-1][0])
        for k in ("new", "revised", "unchanged"):
            out[k] += res[k]
    changed = res["new"] + res["revised"] > 0
    for fid, f in recorded:
        if f.ok and not f.empty:
            mine = _days(f.start, f.end) - failed_days
            if mine:
                await asyncio.to_thread(storage.set_days_stored, fid, len(mine), keep and not changed)
    return out


async def run_dataset(ds: Dataset, mode: str, get: Client, now: datetime, since=None, until=None,
                      dry_run: bool = False) -> dict:
    p = await asyncio.to_thread(plan, ds, mode, now, since, until)
    stats = {"dataset": ds.key, "first": p["first"], "latest": p["latest"], "days_missing": p["missing"],
             "days_skipped": p["skipped"], "chunks": len(p["chunks"]), "fetches": 0, "failed": 0, "empty": 0,
             "new": 0, "revised": 0, "unchanged": 0}
    log.info("power %s %s: %s..%s, %d days to fetch in %d requests, %d already stored%s", mode, ds.key, p["first"],
             p["latest"], p["missing"], len(p["chunks"]), p["skipped"], " (dry run)" if dry_run else "")
    if dry_run or not p["chunks"]:
        return stats
    ids = await asyncio.to_thread(storage.sync_series, [ds])
    keep = mode == "forward" and ds.keep_raw
    for i, (a, b) in enumerate(p["chunks"], 1):
        res = await run_chunk(ds, get, a, b, ids, keep, now)
        for k in ("fetches", "failed", "empty", "new", "revised", "unchanged"):
            stats[k] += res[k]
        if i == len(p["chunks"]) or i % 10 == 0 or mode == "forward":
            log.info("power %s %s: %d/%d chunks (through %s): %d new, %d revised, %d failed, %d empty", mode, ds.key,
                     i, len(p["chunks"]), b, stats["new"], stats["revised"], stats["failed"], stats["empty"])
    return stats


async def run_source(source: str, mode: str, client: httpx.AsyncClient, now: datetime, since=None, until=None,
                     dry_run=False, datasets: list[str] | None = None) -> dict:
    out = {"source": source, "visibility": registry.visibility(source), "datasets": []}
    dss = [d for d in registry.datasets(source) if not datasets or d.key in datasets]
    pace = max([d.pace_seconds for d in dss] or [1.0])
    get = Client(client, source, pace, _secrets(source))
    for ds in dss:
        try:
            out["datasets"].append(await run_dataset(ds, mode, get, now, since, until, dry_run))
        except Exception as exc:  # noqa: BLE001 - one dataset failing never stops the others
            log.exception("power %s %s failed", mode, ds.key)
            out["datasets"].append({"dataset": ds.key, "error": f"{type(exc).__name__}: {exc}"[:300]})
    out["requests"] = get.requests
    return out


async def run(mode: str = "forward", sources: list[str] | None = None, since: date | None = None,
              until: date | None = None, dry_run: bool = False, datasets: list[str] | None = None,
              client: httpx.AsyncClient | None = None, now: datetime | None = None) -> dict:
    """Named sources run even if not in POWER_SOURCES, but never without their REQUIRES settings."""
    if mode not in ("backfill", "forward"):
        raise ValueError(mode)
    now = now or datetime.now(UTC)
    names = sources or registry.selected()
    skipped = [s for s in names if not registry.configured(s)]
    names = [s for s in names if registry.configured(s)]
    own = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True)
    try:
        results = await asyncio.gather(*[run_source(s, mode, client, now, since, until, dry_run, datasets)
                                         for s in names])
    finally:
        if own:
            await client.aclose()
    return {"mode": mode, "dry_run": dry_run, "sources": list(results),
            "not_configured": [{"source": s, "requires": list(getattr(registry.module(s), "REQUIRES", ())),
                                "signup": registry.terms(s).signup} for s in skipped]}


async def forward_job() -> dict:
    """The power_forward job: forward for the selected sources, then the derived cross-analysis."""
    out = await run("forward")
    try:
        from power import cross

        out["cross"] = await asyncio.to_thread(cross.refresh)
    except ImportError:
        pass
    return out


# --------------------------------------------------------------------------- status

def status() -> dict:
    from sqlalchemy import text

    with storage._session()() as s:
        rows = {r.dataset: r for r in s.execute(text("""
            SELECT p.dataset, count(DISTINCT p.id) AS series, count(d.day) AS series_days,
                   count(*) FILTER (WHERE d.complete) AS complete, min(d.day) AS earliest, max(d.day) AS latest,
                   coalesce(sum(pg_column_size(d.vals)), 0) AS vals_bytes
            FROM power_series p LEFT JOIN power_days d ON d.series_id = p.id GROUP BY p.dataset"""))}
        fetches = {r.dataset: r for r in s.execute(text("""
            SELECT dataset, count(*) AS n, count(*) FILTER (WHERE NOT ok) AS failed,
                   count(*) FILTER (WHERE empty) AS empty, coalesce(sum(length(body)), 0) AS body_bytes,
                   max(fetched_at) AS last FROM power_fetches GROUP BY dataset"""))}
        sizes = dict(s.execute(text("""
            SELECT c.relname, pg_total_relation_size(c.oid) FROM pg_class c
            WHERE c.relkind = 'r' AND c.relname LIKE 'power\\_%'""")).all())
    out = []
    selected = set(registry.selected())
    for ds in registry.datasets():
        r, f = rows.get(ds.key), fetches.get(ds.key)
        out.append({"dataset": ds.key, "source": ds.source, "visibility": registry.visibility(ds.source),
                    "selected": ds.source in selected, "series_defined": len(ds.series),
                    "series": r.series if r else 0, "series_days": r.series_days if r else 0,
                    "complete": r.complete if r else 0, "earliest": r.earliest if r else None,
                    "latest": r.latest if r else None, "vals_bytes": int(r.vals_bytes) if r else 0,
                    "fetches": f.n if f else 0, "failed": f.failed if f else 0, "empty": f.empty if f else 0,
                    "raw_bytes_kept": int(f.body_bytes) if f else 0, "last_fetch": f.last if f else None})
    return {"datasets": out, "table_bytes": sizes, "total_bytes": sum(sizes.values())}


_registered = False


def register() -> None:
    """Idempotent: api/power.py calls it. The power_forward job runs only with POWER_ENABLED (and the poller)."""
    global _registered
    from config import settings
    from jobs import job

    if _registered or not settings.poller_enabled or not settings.power_enabled:
        return
    _registered = True
    job("power_forward", every_seconds=POLL_SECONDS, initial_delay_seconds=600)(forward_job)
