"""Sync and status for the site datasets (power/sites/registry.py).

    sync(sources=None, since=None, dry_run=False)   each source fetches only what it has not stored yet, so the
                                                    first run is the backfill and every later run a refresh
    status()                                        rows, depth and bytes per table; fetches per dataset

What one sync asks, per source:
    eia860m    API: 1 metadata request + ~6 pages per month not yet stored (2008-01 onward: ~225 months, ~1,350
               requests the first time, ~7 a month after). File: the index page, then newest-first file URLs
               until one is a workbook (~14 MB; next months' names answer a redirect: "not published")
    eia923     1 metadata request + 3 pages per year: every year from 2008 the first time (~55 requests), the two
               newest complete years after that (~7)
    lbnl       one request per listed edition not yet stored (15.6 MB); nothing once stored
    ercot_gis  the document list + each GIS report not yet applied (101 the first time, ~670 KB each; 1 a month)

Sources run side by side, one request at a time within a source (EIA's two datasets share one queue).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timezone

import httpx

from power.http import TIMEOUT, Client
from power.sites import registry

log = logging.getLogger("power.sites")
UTC = timezone.utc
POLL_SECONDS = 7 * 86400   # EIA-860M and the GIS report are monthly; a weekly look picks each up within a week
PACE = {"eia": 1.0, "eia_www": 2.0, "lbnl_queues": 5.0, "ercot_gis": 2.5}


def _secrets() -> tuple[str, ...]:
    from config import settings

    return tuple(s for s in (settings.eia_api_key or "",) if s)


async def _eia860m(client, since, dry_run):
    from power.sites import eia860m

    api = Client(client, "eia", PACE["eia"], _secrets())
    www = Client(client, "eia_www", PACE["eia_www"])
    a = await eia860m.sync_api(api, since, dry_run)
    if a.get("error"):
        log.warning("power sites eia860m api: %s", a["error"])
    b = await eia860m.sync_file(www, dry_run)
    return {"source": "eia860m", "datasets": [a, b], "requests": api.requests + www.requests}


async def _eia923(client, since, dry_run):
    from power.sites import eia923

    get = Client(client, "eia", PACE["eia"], _secrets())
    return {"source": "eia923", "datasets": [await eia923.sync(get, since, dry_run)], "requests": get.requests}


async def _lbnl(client, since, dry_run):
    from power.sites import lbnl

    get = Client(client, "lbnl_queues", PACE["lbnl_queues"])
    return {"source": "lbnl_queues", "datasets": [await lbnl.sync(get, dry_run)], "requests": get.requests}


async def _ercot(client, since, dry_run):
    from power.sites import ercot_gis

    get = Client(client, "ercot_gis", PACE["ercot_gis"])
    return {"source": "ercot_gis", "datasets": [await ercot_gis.sync(get, since, dry_run)], "requests": get.requests}


RUN = {"eia860m": _eia860m, "eia923": _eia923, "lbnl_queues": _lbnl, "ercot_gis": _ercot}


async def _one(name, client, since, dry_run):
    try:
        return await RUN[name](client, since, dry_run)
    except Exception as exc:  # noqa: BLE001 - one source failing never stops the others
        log.exception("power sites %s failed", name)
        return {"source": name, "error": f"{type(exc).__name__}: {exc}"[:300]}


async def sync(sources: list[str] | None = None, since: date | None = None, dry_run: bool = False,
               client: httpx.AsyncClient | None = None) -> dict:
    """Named sources run even when the job would skip them, never without their REQUIRES settings."""
    names = sources or [s.key for s in registry.SOURCES]
    unknown = [n for n in names if n not in RUN]
    if unknown:
        raise ValueError(f"unknown site source(s) {unknown}; known: {sorted(RUN)}")
    skipped = [n for n in names if not registry.configured(n)]
    names = [n for n in names if registry.configured(n)]
    own = client is None
    # redirects are answers here: EIA sends unpublished file names to an HTML page with a 301
    client = client or httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False)
    try:
        results = await asyncio.gather(*[_one(n, client, since, dry_run) for n in names])
    finally:
        if own:
            await client.aclose()
    return {"dry_run": dry_run, "sources": list(results),
            "not_configured": [{"source": n, "requires": list(registry.source(n).requires),
                                "signup": registry.source(n).terms.signup} for n in skipped]}


async def job() -> dict:
    return await sync()


# --------------------------------------------------------------------------- status

TABLES = ("power_generators", "power_generator_history", "power_plant_generation", "power_queue_projects")


def status() -> dict:
    from sqlalchemy import text

    from power import storage

    with storage._session()() as s:
        sizes = dict(s.execute(text("""SELECT c.relname, pg_total_relation_size(c.oid) FROM pg_class c
                                       WHERE c.relkind = 'r' AND c.relname = ANY(:t)"""), {"t": list(TABLES)}).all())
        counts = {t: s.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t in TABLES}
        gens = s.execute(text("""SELECT status_group, count(*) AS n, coalesce(sum(nameplate_mw), 0) AS mw,
                                        min(first_period) AS first, max(period) AS last
                                 FROM power_generators GROUP BY 1 ORDER BY 1""")).all()
        years = s.execute(text("SELECT min(year), max(year), count(DISTINCT plant_id) FROM power_plant_generation"
                               )).one()
        queues = s.execute(text("""SELECT source, edition, visibility, count(*) AS n,
                                          coalesce(sum(mw) FILTER (WHERE status = 'active'), 0) AS active_mw
                                   FROM power_queue_projects GROUP BY 1, 2, 3 ORDER BY 1, 2""")).all()
        fetches = s.execute(text("""SELECT dataset, count(*) AS n, count(*) FILTER (WHERE NOT ok) AS failed,
                                           count(*) FILTER (WHERE empty) AS empty,
                                           coalesce(sum(bytes), 0) AS bytes_received, max(fetched_at) AS last
                                    FROM power_fetches WHERE dataset LIKE 'sites.%' GROUP BY 1 ORDER BY 1""")).all()
    return {"tables": {t: {"rows": counts[t], "bytes": int(sizes.get(t, 0))} for t in TABLES},
            "total_bytes": int(sum(sizes.values())),
            "generators": [dict(r._mapping) for r in gens],
            "generation_years": {"first": years[0], "last": years[1], "plants": years[2]},
            "queue_editions": [dict(r._mapping) for r in queues],
            "fetches": [dict(r._mapping) for r in fetches],
            "selected": registry.selected()}


_registered = False


def register() -> None:
    """Idempotent (api/power.py calls it). The power_sites job runs only with POWER_SITES_ENABLED and the poller;
    the EIA sources inside it also need EIA_API_KEY."""
    global _registered
    from config import settings
    from jobs import job as register_job

    if _registered or not settings.poller_enabled or not settings.power_sites_enabled:
        return
    _registered = True
    register_job("power_sites", every_seconds=POLL_SECONDS, initial_delay_seconds=1800)(job)
