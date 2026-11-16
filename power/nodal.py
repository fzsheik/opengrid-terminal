"""Where power is stranded, node by node: one summary row per priced node and market day, never the interval prices.

    run(mode, sources=None, since=None, until=None, dry_run=False, datasets=None)   "backfill" | "forward"
    status()                                                       per nodal dataset: nodes, rows, depth; bytes
    cheap_nodes(window_days=90, market=None, ...)                  the ranking: share of intervals below $20
    summarize(prices)                                              one day's summary of one node (pure)

A hub average hides stranded power: it sits at nodes behind congested lines, priced below zero for hours while the
hub clears near its usual level. The nodal datasets (power/registry.py NodalDataset; ERCOT settlement points, SPP
settlement locations) are read one market day at a time from the same public files as the hub data. Each day's
prices are summarised per node in memory and stored as one power_node_days row: intervals priced, intervals below
$0 and below $20 (strictly), mean, p10, p50 (linear interpolation, Postgres's percentile_cont), min, max. The node
catalogue (power_nodes) takes what the answers and the source's node list state about each node.

backfill  every market day from max(--since, the source's earliest; default the last BACKFILL_DAYS) to its latest
          complete day that is not already read. One request per day (ERCOT RT: one per 15-minute file), each day
          recorded raw-first in power_fetches (url + sha256; bodies never kept: SPP's RT day is 39-51 MB) and
          committed on its own, so an interrupted run resumes and a re-run stores nothing twice.
forward   from the last read day minus the dataset's overlap to the latest complete day (nothing read yet: the last
          FORWARD_INITIAL_DAYS); runs after the hub forward job in the same process, so ERCOT's documents are
          read from memory, not downloaded again.

A day is "read" when an answer for it succeeded and nothing for it failed (power_fetches.days_stored > 0), or the
source answered "no data" for it more than UNPUBLISHED_RETRY_DAYS before its latest day.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
from datetime import date, datetime, timedelta, timezone

import httpx

from power import registry, storage
from power.http import TIMEOUT, Client
from power.registry import Fetched, NodalDataset, NodalParsed

log = logging.getLogger("power")

UTC = timezone.utc
FORWARD_INITIAL_DAYS = 7
BACKFILL_DAYS = 366
UNPUBLISHED_RETRY_DAYS = 14
CHEAP = 20.0                  # $/MWh: "below $20" counts prices strictly under it
MAX_WINDOW_DAYS = 365
NODE_LIST = "node_list"       # power_fetches.dataset suffix of a source's node catalogue file ("ercot.node_list")
READ_NOTE = ("Shares count intervals priced strictly below $0 and $20/MWh; median is the median of the daily medians "
             "(interval prices are not kept per node). Coordinates are null: neither ERCOT nor SPP publishes them "
             "for these nodes. Internal: never republish.")


# --------------------------------------------------------------------------- the summary (pure)

def percentile(xs: list[float], q: float) -> float:
    """Linear interpolation between closest ranks on a SORTED list (numpy "linear", Postgres percentile_cont)."""
    pos = (len(xs) - 1) * q
    lo = math.floor(pos)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def summarize(prices: list[float]) -> dict | None:
    xs = sorted(v for v in prices if v is not None and math.isfinite(v))
    if not xs:
        return None
    return {"n": len(xs), "n_neg": sum(1 for v in xs if v < 0), "n_lt20": sum(1 for v in xs if v < CHEAP),
            "mean": sum(xs) / len(xs), "p10": percentile(xs, 0.10), "p50": percentile(xs, 0.50),
            "min": xs[0], "max": xs[-1]}


def day_values(ds: NodalDataset, pts: dict[datetime, float], day: date) -> list[float]:
    """The node's prices on the dataset's grid within the market day (one per interval; off-grid points and other
    days dropped, as power/storage.to_days does for hub series)."""
    start = storage.day_start(day, ds.tz)
    end = storage.day_start(day + timedelta(days=1), ds.tz)
    step = ds.interval_minutes * 60
    out = []
    for t, v in pts.items():
        if start <= t < end and not (t - start).total_seconds() % step:
            out.append(v)
    return out


def summaries(ds: NodalDataset, parsed: NodalParsed, day: date) -> dict[str, dict]:
    out = {}
    for node, pts in parsed.prices.items():
        sm = summarize(day_values(ds, pts, day))
        if sm:
            out[node] = sm
    return out


# --------------------------------------------------------------------------- storage

def _session():
    return storage._session()


def sync_nodes(iso: str, nodes: dict[str, dict], day: date | None, now: datetime | None = None) -> dict[str, int]:
    """Upsert catalogue rows: a field the answer states replaces the stored one, a field it leaves out keeps it;
    meta keys merge; first_day / last_day widen. day=None (a node list, not a priced day) never widens them."""
    from sqlalchemy import func, literal_column, select
    from sqlalchemy.dialects.postgresql import insert

    from store.power import PowerNode as N

    if not nodes:
        return {}
    now = now or datetime.now(UTC)
    rows = [{"iso": iso, "node": k[:64], "node_type": (v.get("node_type") or None),
             "zone": (v.get("zone") or None), "substation": (v.get("substation") or None),
             "county": v.get("county"), "latitude": v.get("latitude"), "longitude": v.get("longitude"),
             "meta": v.get("meta") or None, "first_day": day, "last_day": day,
             "updated_at": now} for k, v in nodes.items()]
    out: dict[str, int] = {}
    with _session().begin() as s:
        for i in range(0, len(rows), 1000):
            stmt = insert(N).values(rows[i:i + 1000])
            ex = stmt.excluded
            set_ = {c: func.coalesce(getattr(ex, c), getattr(N, c))
                    for c in ("node_type", "zone", "substation", "county", "latitude", "longitude")}
            # LEAST / GREATEST skip NULLs: a node-list row (no priced day) never moves the priced range
            set_.update(meta=literal_column("coalesce(nullif(power_nodes.meta, 'null'::jsonb), '{}'::jsonb) || "
                                            "coalesce(nullif(excluded.meta, 'null'::jsonb), '{}'::jsonb)"),
                        first_day=func.least(N.first_day, ex.first_day),
                        last_day=func.greatest(N.last_day, ex.last_day), updated_at=ex.updated_at)
            # rewrite a row only when something moves: every node is seen every day, and a no-op update still
            # writes a new row version (measured: 4.8 MB for 2,757 nodes after 38 days without this)
            changed = literal_column("""(
                excluded.node_type IS NOT NULL AND excluded.node_type IS DISTINCT FROM power_nodes.node_type
                OR excluded.zone IS NOT NULL AND excluded.zone IS DISTINCT FROM power_nodes.zone
                OR excluded.substation IS NOT NULL AND excluded.substation IS DISTINCT FROM power_nodes.substation
                OR excluded.county IS NOT NULL AND excluded.county IS DISTINCT FROM power_nodes.county
                OR excluded.latitude IS NOT NULL AND excluded.latitude IS DISTINCT FROM power_nodes.latitude
                OR excluded.longitude IS NOT NULL AND excluded.longitude IS DISTINCT FROM power_nodes.longitude
                OR excluded.meta IS NOT NULL AND NOT coalesce(power_nodes.meta @> excluded.meta, false)
                OR excluded.first_day < power_nodes.first_day OR excluded.last_day > power_nodes.last_day
                OR (power_nodes.first_day IS NULL AND excluded.first_day IS NOT NULL))""")
            stmt = stmt.on_conflict_do_update(constraint="uq_power_nodes_iso_node", set_=set_, where=changed)
            s.execute(stmt)
        # ON CONFLICT .. WHERE returns no row for a skipped update: the ids are read back
        names = [r["node"] for r in rows]
        for i in range(0, len(names), 5000):
            out.update(dict(s.execute(select(N.node, N.id).where(N.iso == iso, N.node.in_(names[i:i + 5000]))).all()))
    return out


def upsert_days(market: str, day: date, rows: dict[int, dict]) -> dict:
    """INSERT .. ON CONFLICT: a known (market, day, node) is replaced only when its summary changed and the new one
    covers at least as many intervals (a partial re-read never replaces a fuller day)."""
    from sqlalchemy import literal_column, tuple_
    from sqlalchemy.dialects.postgresql import insert

    from store.power import PowerNodeDay as D

    vals = [{"market": market, "day": day, "node_id": nid, **sm} for nid, sm in rows.items()]
    if not vals:
        return {"new": 0, "revised": 0, "unchanged": 0}
    cols = ("n", "n_neg", "n_lt20", "mean", "p10", "p50", "min", "max")
    new = revised = 0
    with _session().begin() as s:
        for i in range(0, len(vals), 2000):
            stmt = insert(D).values(vals[i:i + 2000])
            ex = stmt.excluded
            stmt = stmt.on_conflict_do_update(
                index_elements=[D.market, D.day, D.node_id], set_={c: getattr(ex, c) for c in cols},
                where=(tuple_(*[getattr(D, c) for c in cols]).is_distinct_from(tuple_(*[getattr(ex, c) for c in cols]))
                       & (ex.n >= D.n)),
            ).returning(literal_column("(xmax = 0)").label("inserted"))
            for r in s.execute(stmt):
                if r.inserted:
                    new += 1
                else:
                    revised += 1
    return {"new": new, "revised": revised, "unchanged": len(vals) - new - revised}


def done_days(ds: NodalDataset, start: date, end: date, latest: date) -> set[date]:
    from sqlalchemy import text

    out: set[date] = set()
    with _session()() as s:
        for a, b, empty in s.execute(text("""
                SELECT period_start, period_end, empty FROM power_fetches
                WHERE dataset = :ds AND ok AND (empty OR days_stored > 0) AND period_start IS NOT NULL
                  AND period_end >= :a AND period_start <= :b"""), {"ds": ds.key, "a": start, "b": end}).all():
            for d in storage.days(max(a, start), min(b, end)):
                if not empty or d < latest - timedelta(days=UNPUBLISHED_RETRY_DAYS):
                    out.add(d)
        # a day with any failed answer since its last success is not done (one of 96 ERCOT files failed)
        for (d,) in s.execute(text("""
                SELECT f.period_start FROM power_fetches f
                WHERE f.dataset = :ds AND NOT f.ok AND f.period_start = f.period_end
                  AND f.period_start BETWEEN :a AND :b
                  AND f.fetched_at > coalesce((SELECT max(g.fetched_at) FROM power_fetches g WHERE g.dataset = :ds
                                               AND g.ok AND g.days_stored > 0 AND g.period_start = f.period_start),
                                              '-infinity')"""), {"ds": ds.key, "a": start, "b": end}).all():
            out.discard(d)
    return out


def last_read_day(ds: NodalDataset) -> date | None:
    from sqlalchemy import text

    with _session()() as s:
        return s.execute(text("SELECT max(period_end) FROM power_fetches WHERE dataset = :ds AND ok AND days_stored > 0"),
                         {"ds": ds.key}).scalar()


# --------------------------------------------------------------------------- runs

def plan(ds: NodalDataset, mode: str, now: datetime, since: date | None = None, until: date | None = None) -> dict:
    latest = ds.latest(now)
    if until:
        latest = min(latest, until)
    earliest = ds.earliest(now)
    if mode == "backfill":
        first = max(since or ds.latest(now) - timedelta(days=BACKFILL_DAYS - 1), earliest)
        skip = done_days(ds, first, latest, ds.latest(now)) if first <= latest else set()
    else:
        last = last_read_day(ds)
        first = (last - timedelta(days=ds.overlap_days)) if last else latest - timedelta(days=FORWARD_INITIAL_DAYS - 1)
        first = max(first, earliest, since or earliest)
        # the last overlap_days days are re-read even when done; 0 re-reads nothing (ERCOT's per-report limit)
        reread_from = latest - timedelta(days=ds.overlap_days - 1)
        skip = {d for d in done_days(ds, first, latest, ds.latest(now)) if d < reread_from} if first <= latest else set()
    missing = [d for d in storage.days(first, latest) if d not in skip] if first <= latest else []
    return {"dataset": ds.key, "first": first, "latest": latest, "skipped": len(skip), "missing": missing}


async def run_day(ds: NodalDataset, get: Client, day: date, now: datetime | None = None) -> dict:
    """Fetch one market day, record every answer raw-first (url + sha256), summarise every node, upsert once.
    The day is marked read only when no answer for it failed."""
    out = {"fetches": 0, "failed": 0, "empty": 0, "nodes": 0, "new": 0, "revised": 0, "unchanged": 0}
    try:
        if "now" in inspect.signature(ds.fetch).parameters:
            answers = await ds.fetch(get, day, day, now=now)
        else:
            answers = await ds.fetch(get, day, day)
    except Exception as exc:  # noqa: BLE001 - a fetch() bug is a failed fetch, recorded
        answers = [Fetched(f"{ds.key}:{day}", None, None, None, error=get.redact(f"{type(exc).__name__}: {exc}")[:500])]
    merged = NodalParsed()
    recorded: list[tuple[int, Fetched]] = []
    failed = False
    for f in answers:
        f.start, f.end = f.start or day, f.end or day
        out["fetches"] += 1
        if not f.ok and f.status in ds.empty_statuses:
            f.empty, f.error = True, None
        if f.ok:
            try:
                p = ds.parse(f)
                for node, pts in p.prices.items():
                    merged.prices.setdefault(node, {}).update(pts)
                for node, info in p.nodes.items():
                    if node not in merged.nodes or (info.get("node_type") and not merged.nodes[node].get("node_type")):
                        merged.nodes[node] = info
            except Exception as exc:  # noqa: BLE001 - stored as a failed parse, never silently
                f.error, f.empty = f"parse: {type(exc).__name__}: {exc}"[:500], False
        fid = await asyncio.to_thread(storage.record_fetch, ds, f, False)   # raw first; bodies never kept
        recorded.append((fid, f))
        if f.empty and (f.ok or not f.error):
            out["empty"] += 1
        elif not f.ok:
            failed = True
            out["failed"] += 1
            log.warning("power nodal %s %s: %s", ds.key, day, f.error or f.status)
    sums = summaries(ds, merged, day)
    if sums:
        info = {k: merged.nodes.get(k, {}) for k in sums}
        ids = await asyncio.to_thread(sync_nodes, ds.iso, info, day)
        res = await asyncio.to_thread(upsert_days, ds.market, day, {ids[k[:64]]: v for k, v in sums.items()})
        out["nodes"] = len(sums)
        for k in ("new", "revised", "unchanged"):
            out[k] += res[k]
    if not failed:
        for fid, f in recorded:
            if f.ok and not f.empty:
                await asyncio.to_thread(storage.set_days_stored, fid, 1)
    return out


async def run_dataset(ds: NodalDataset, mode: str, get: Client, now: datetime, since=None, until=None,
                      dry_run: bool = False) -> dict:
    p = await asyncio.to_thread(plan, ds, mode, now, since, until)
    stats = {"dataset": ds.key, "first": p["first"], "latest": p["latest"], "days_missing": len(p["missing"]),
             "days_skipped": p["skipped"], "fetches": 0, "failed": 0, "empty": 0, "nodes": 0, "new": 0,
             "revised": 0, "unchanged": 0}
    log.info("power nodal %s %s: %s..%s, %d days to read, %d already read%s", mode, ds.key, p["first"], p["latest"],
             len(p["missing"]), p["skipped"], " (dry run)" if dry_run else "")
    if dry_run:
        return stats
    for i, d in enumerate(p["missing"], 1):
        res = await run_day(ds, get, d, now)
        for k in ("fetches", "failed", "empty", "new", "revised", "unchanged"):
            stats[k] += res[k]
        stats["nodes"] = max(stats["nodes"], res["nodes"])
        if i == len(p["missing"]) or i % 10 == 0 or mode == "forward":
            log.info("power nodal %s %s: %d/%d days (through %s): %d rows new, %d revised, %d failed, %d empty",
                     mode, ds.key, i, len(p["missing"]), d, stats["new"], stats["revised"], stats["failed"],
                     stats["empty"])
    return stats


async def refresh_catalogue(source: str, get: Client) -> dict | None:
    """A source's node list (ERCOT's Settlement Points List): read when a new one is posted, else skipped."""
    from sqlalchemy import text

    m = registry.module(source)
    fetch = getattr(m, "fetch_node_list", None)
    if fetch is None:
        return None
    ds = registry.NodalDataset(key=f"{source}.{NODE_LIST}", source=source, iso="", market="", interval_minutes=0,
                               tz="UTC", earliest=None, latest=None, fetch=None, parse=None)

    def read_before() -> set[str]:
        with _session()() as s:
            return {u for (u,) in s.execute(text("SELECT url FROM power_fetches WHERE dataset = :ds AND ok "
                                                 "AND days_stored > 0"), {"ds": ds.key})}

    urls = await asyncio.to_thread(read_before)
    f = await fetch(get, lambda url: url in urls)
    if f is None:
        return {"node_list": "unchanged"}
    nodes: dict = {}
    if f.ok:
        try:
            nodes = m.parse_node_list(f.body)
        except Exception as exc:  # noqa: BLE001 - recorded as a failed parse
            f.error = f"parse: {type(exc).__name__}: {exc}"[:500]
    fid = await asyncio.to_thread(storage.record_fetch, ds, f, False)
    if f.ok and nodes:
        isos = {d.iso for d in m.NODAL_DATASETS}
        for iso in isos:
            await asyncio.to_thread(sync_nodes, iso, nodes, None)
        await asyncio.to_thread(storage.set_days_stored, fid, 1)
    return {"node_list": "read" if f.ok else "failed", "nodes": len(nodes), "error": f.error}


async def run_source(source: str, mode: str, client: httpx.AsyncClient, now: datetime, since=None, until=None,
                     dry_run=False, datasets: list[str] | None = None) -> dict:
    out = {"source": source, "visibility": registry.nodal_visibility(source), "datasets": []}
    dss = [d for d in registry.nodal_datasets(source) if not datasets or d.key in datasets]
    get = Client(client, source, max([d.pace_seconds for d in dss] or [2.5]))
    if not dry_run and dss:
        try:
            out["catalogue"] = await refresh_catalogue(source, get)
        except Exception as exc:  # noqa: BLE001 - the node list failing never stops the prices
            log.exception("power nodal %s catalogue failed", source)
            out["catalogue"] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
    for ds in dss:
        try:
            out["datasets"].append(await run_dataset(ds, mode, get, now, since, until, dry_run))
        except Exception as exc:  # noqa: BLE001 - one dataset failing never stops the others
            log.exception("power nodal %s %s failed", mode, ds.key)
            out["datasets"].append({"dataset": ds.key, "error": f"{type(exc).__name__}: {exc}"[:300]})
    out["requests"] = get.requests
    return out


async def run(mode: str = "forward", sources: list[str] | None = None, since: date | None = None,
              until: date | None = None, dry_run: bool = False, datasets: list[str] | None = None,
              client: httpx.AsyncClient | None = None, now: datetime | None = None) -> dict:
    if mode not in ("backfill", "forward"):
        raise ValueError(mode)
    now = now or datetime.now(UTC)
    have = {d.source for d in registry.nodal_datasets()}
    names = [s for s in (sources or registry.selected()) if s in have and registry.configured(s)]
    own = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True)
    try:
        results = await asyncio.gather(*[run_source(s, mode, client, now, since, until, dry_run, datasets)
                                         for s in names])
    finally:
        if own:
            await client.aclose()
    return {"mode": mode, "dry_run": dry_run, "sources": list(results)}


# --------------------------------------------------------------------------- reads

def status() -> dict:
    from sqlalchemy import text

    with _session()() as s:
        rows = {(r.iso, r.market): r for r in s.execute(text("""
            SELECT n.iso, d.market, count(DISTINCT d.node_id) AS nodes, count(*) AS rows, count(DISTINCT d.day) AS days,
                   min(d.day) AS earliest, max(d.day) AS latest
            FROM power_node_days d JOIN power_nodes n ON n.id = d.node_id GROUP BY 1, 2"""))}
        catalogue = dict(s.execute(text("SELECT iso, count(*) FROM power_nodes GROUP BY 1")).all())
        sizes = dict(s.execute(text("""SELECT c.relname, pg_total_relation_size(c.oid) FROM pg_class c
                                       WHERE c.relkind = 'r' AND c.relname IN ('power_nodes', 'power_node_days')""")).all())
    out = []
    for ds in registry.nodal_datasets():
        r = rows.get((ds.iso, ds.market))
        out.append({"dataset": ds.key, "iso": ds.iso, "market": ds.market, "visibility": registry.nodal_visibility(ds.source),
                    "nodes": r.nodes if r else 0, "rows": r.rows if r else 0, "days": r.days if r else 0,
                    "earliest": r.earliest if r else None, "latest": r.latest if r else None,
                    "catalogue_nodes": catalogue.get(ds.iso, 0)})
    return {"datasets": out, "table_bytes": sizes, "total_bytes": sum(sizes.values())}


def _interval_minutes() -> dict[tuple[str, str], int]:
    return {(d.iso, d.market): d.interval_minutes for d in registry.nodal_datasets()}


def _source_of(iso: str) -> str:
    return next((d.source for d in registry.nodal_datasets() if d.iso == iso), iso.lower())


def cheap_nodes(window_days: int = 90, market: str | None = None, *, series: str = "rt", zone: str | None = None,
                node_type: str | None = None, min_days: int = 7, limit: int = 100, until: date | None = None
                ) -> list[dict]:
    """Nodes ranked by the share of their intervals priced below $20/MWh over the window (ties: the share below $0,
    then the lower mean). market: "ercot" | "spp" | None (both); series: "rt" | "da". The window ends at `until`
    (default: the latest day stored for the series) and covers window_days market days back from it.

    `median` is the median of the nodes' daily medians: the exact median of every interval would need the interval
    prices, which are not kept. Coordinates are NULL where the source publishes none (ERCOT and SPP, today)."""
    from sqlalchemy import text

    if series not in ("rt", "da"):
        raise ValueError(f"series must be rt or da, not {series!r}")
    window_days = max(1, min(int(window_days), MAX_WINDOW_DAYS))
    iso = market.upper() if market else None
    with _session()() as s:
        if until is None:
            until = s.execute(text("SELECT max(day) FROM power_node_days WHERE market = :s"), {"s": series}).scalar()
            if until is None:
                return []
        first = until - timedelta(days=window_days - 1)
        rows = s.execute(text("""
            WITH w AS (
                SELECT d.node_id, count(*) AS days, sum(d.n) AS n, sum(d.n_neg) AS n_neg, sum(d.n_lt20) AS n_lt20,
                       sum(d.mean * d.n) / sum(d.n) AS mean,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY d.p50) AS median,
                       min(d.min) AS min, max(d.max) AS max, min(d.day) AS first_day, max(d.day) AS last_day
                FROM power_node_days d JOIN power_nodes n ON n.id = d.node_id
                WHERE d.market = :s AND d.day BETWEEN :a AND :b
                  AND (CAST(:iso AS text) IS NULL OR n.iso = :iso)
                  AND (CAST(:zone AS text) IS NULL OR n.zone = :zone)
                  AND (CAST(:typ AS text) IS NULL OR n.node_type = :typ)
                GROUP BY d.node_id HAVING count(*) >= :min_days AND sum(d.n) > 0)
            SELECT n.iso, n.node, n.node_type, n.zone, n.substation, n.county, n.latitude, n.longitude, n.meta, w.*
            FROM w JOIN power_nodes n ON n.id = w.node_id
            ORDER BY w.n_lt20::float / w.n DESC, w.n_neg::float / w.n DESC, w.mean, n.iso, n.node
            LIMIT :lim"""), {"s": series, "a": first, "b": until, "iso": iso, "zone": zone, "typ": node_type,
                             "min_days": max(1, int(min_days)), "lim": max(1, int(limit))}).all()
    minutes = _interval_minutes()
    out = []
    for r in rows:
        step = minutes.get((r.iso, series), 60) / 60
        out.append({"market": r.iso.lower(), "series": series, "node": r.node, "node_type": r.node_type,
                    "zone": r.zone, "substation": r.substation, "county": r.county, "latitude": r.latitude,
                    "longitude": r.longitude, "meta": r.meta,
                    "share_below_0": round(r.n_neg / r.n, 4), "share_below_20": round(r.n_lt20 / r.n, 4),
                    "hours_below_0": round(r.n_neg * step, 2), "hours_below_20": round(r.n_lt20 * step, 2),
                    "median": round(float(r.median), 2), "mean": round(float(r.mean), 2),
                    "min": round(float(r.min), 2), "max": round(float(r.max), 2),
                    "days": r.days, "intervals": int(r.n), "first_day": r.first_day, "last_day": r.last_day,
                    "window": {"first": first, "last": until, "days": window_days},
                    "visibility": registry.nodal_visibility(_source_of(r.iso))})
    return out
