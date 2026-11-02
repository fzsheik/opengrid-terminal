"""Fetch, store raw, parse and read the market-context series (methodology/market-context.md).

    refresh()            the context_series job: EIA (if EIA_API_KEY) and SEC EDGAR (if SEC_USER_AGENT); each
                         response stored raw in context_snapshots, failures too, then parsed into context_series
    catalog()            every series OpenGrid can carry, with whether its source is enabled and its latest point
    points(ids, since)   the values of some PUBLIC series (visibility "internal" rows, i.e. Alpaca equities, never)
    power_by_region()    current US listings joined to their state's latest electricity price (context, not cause)

Nothing here reads into listings, indices, spreads, percentiles or routing.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time as _time
from datetime import date, datetime, timezone
from decimal import Decimal

import httpx

from context import edgar, eia, geo

log = logging.getLogger(__name__)

POLL_SECONDS = 24 * 3600
SEC_PACE_SECONDS = 0.2   # <= 5 requests a second (SEC allows 10)
TIMEOUT = 60
NOT_CAUSAL = "Context, not cause: these series are shown beside GPU prices; nothing here says one moved the other."


def _session():
    import normalize

    return normalize.SessionLocal


def enabled() -> dict[str, bool]:
    from config import settings

    return {"eia": bool(settings.eia_api_key), "sec_edgar": bool(settings.sec_user_agent)}


# --------------------------------------------------------------------------- storage

def save_snapshot(source: str, key: str, url: str, fetched_at: datetime, status: int | None, ms: int | None,
                  ok: bool, error: str | None, payload) -> int:
    from store.context import ContextSnapshot

    body = None if payload is None else json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    with _session().begin() as s:
        snap = ContextSnapshot(source=source, key=key, url=url[:500], fetched_at=fetched_at, status_code=status,
                               duration_ms=ms, ok=ok, error=error, payload=payload,
                               sha256=hashlib.sha256(body.encode()).hexdigest() if body else None)
        s.add(snap)
        s.flush()
        return snap.id


def upsert(points: list[dict], snapshot_id: int | None, now: datetime | None = None) -> dict:
    """Insert new periods; a changed value for a known period is a revision (value replaced, counted)."""
    from sqlalchemy import select

    from store.context import ContextSeries

    now = now or datetime.now(timezone.utc)
    new = revised = 0
    if not points:
        return {"new": 0, "revised": 0}
    with _session().begin() as s:
        keys = {(p["series_id"], p["period_start"], p["period_end"]) for p in points}
        ids = {k[0] for k in keys}
        old = {(r.series_id, r.period_start, r.period_end): r for r in s.execute(
            select(ContextSeries).where(ContextSeries.series_id.in_(ids))).scalars() if (r.series_id, r.period_start, r.period_end) in keys}
        for p in points:
            k = (p["series_id"], p["period_start"], p["period_end"])
            row = old.get(k)
            if row is None:
                s.add(ContextSeries(**p, first_seen_at=now, updated_at=now, revisions=0, snapshot_id=snapshot_id))
                old[k] = True
                new += 1
            elif row is not True and Decimal(row.value) != Decimal(p["value"]):
                row.meta = {**(row.meta or {}), **(p.get("meta") or {}), "previous_value": str(row.value)}
                row.value, row.updated_at, row.revisions, row.snapshot_id = p["value"], now, row.revisions + 1, snapshot_id
                revised += 1
    return {"new": new, "revised": revised}


# --------------------------------------------------------------------------- fetch

async def _get(client: httpx.AsyncClient, url: str, params=None, headers=None):
    started = _time.perf_counter()
    resp = await client.get(url, params=params, headers=headers)
    return resp, int((_time.perf_counter() - started) * 1000)


async def refresh_eia(client: httpx.AsyncClient, api_key: str) -> dict:
    now = datetime.now(timezone.utc)
    status = ms = None
    try:
        resp, ms = await _get(client, eia.URL, params=eia.params(api_key))
        status = resp.status_code
        resp.raise_for_status()
        payload = eia.reshape(resp.json())
    except Exception as exc:  # noqa: BLE001 - stored as a failed fetch
        await asyncio.to_thread(save_snapshot, eia.SOURCE, "retail-sales", eia.URL, now, status, ms, False,
                                f"{type(exc).__name__}: {exc}"[:500].replace(api_key, "***"), None)
        return {"source": "eia", "ok": False}
    sid = await asyncio.to_thread(save_snapshot, eia.SOURCE, "retail-sales", eia.URL, now, status, ms, True, None, payload)
    res = await asyncio.to_thread(upsert, eia.points(payload), sid, now)
    return {"source": "eia", "ok": True, **res}


async def refresh_edgar(client: httpx.AsyncClient, user_agent: str) -> dict:
    headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}
    out = {"source": "sec_edgar", "fetches": 0, "failed": 0, "new": 0, "revised": 0}

    async def fetch(key: str, url: str, reshape):
        await asyncio.sleep(SEC_PACE_SECONDS)
        now = datetime.now(timezone.utc)
        status = ms = None
        out["fetches"] += 1
        try:
            resp, ms = await _get(client, url, headers=headers)
            status = resp.status_code
            resp.raise_for_status()
            payload = reshape(resp)
        except Exception as exc:  # noqa: BLE001
            out["failed"] += 1
            await asyncio.to_thread(save_snapshot, edgar.SOURCE, key, url, now, status, ms, False,
                                    f"{type(exc).__name__}: {exc}"[:500], None)
            return None, None
        sid = await asyncio.to_thread(save_snapshot, edgar.SOURCE, key, url, now, status, ms, True, None, payload)
        return payload, sid

    def add(res):
        out["new"] += res["new"]
        out["revised"] += res["revised"]

    for ticker, (cik, _name, capex, rev) in edgar.COMPANIES.items():
        concepts = tuple(capex) + (edgar.REVENUE if rev else ())
        payload, sid = await fetch(f"companyfacts:{cik}", edgar.FACTS_URL.format(cik=cik),
                                   lambda r, c=concepts: edgar.reshape_facts(r.json(), c))
        if payload:
            add(await asyncio.to_thread(upsert, edgar.facts_points(ticker, payload), sid))
        if ticker not in edgar.SEGMENTS:
            continue
        subs, _ = await fetch(f"submissions:{cik}", edgar.SUBMISSIONS_URL.format(cik=cik),
                              lambda r: edgar.reshape_submissions(r.json()))
        if not subs:
            continue
        have = await asyncio.to_thread(_stored_instances, cik)
        for f in subs["filings"]:
            if f["accn"] in have:
                continue
            url = edgar.INSTANCE_URL.format(cik=cik, accn=f["accn"].replace("-", ""), doc=edgar.instance_doc(f["doc"]))
            await fetch(f"instance:{cik}:{f['accn']}", url,
                        lambda r, f=f: {"accn": f["accn"], "filed": f["filed"], "form": f["form"], "report": f["report"],
                                        "facts": edgar.parse_instance(r.content, edgar.REVENUE, edgar.SEGMENTS[ticker])})
        instances = await asyncio.to_thread(_instances, cik)
        add(await asyncio.to_thread(upsert, edgar.segment_points(ticker, instances), None))
    return out


def _stored_instances(cik: int) -> set[str]:
    return {p["accn"] for p in _instances(cik)}


def _instances(cik: int) -> list[dict]:
    from sqlalchemy import select

    from store.context import ContextSnapshot

    with _session()() as s:
        rows = s.execute(select(ContextSnapshot.payload).where(
            ContextSnapshot.source == edgar.SOURCE, ContextSnapshot.ok.is_(True),
            ContextSnapshot.key.like(f"instance:{cik}:%"))).scalars().all()
    seen, out = set(), []
    for p in rows:
        if p and p["accn"] not in seen:
            seen.add(p["accn"])
            out.append(p)
    return out


async def refresh(client: httpx.AsyncClient | None = None) -> dict:
    from config import settings

    own = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True)
    out = []
    try:
        if settings.eia_api_key:
            out.append(await refresh_eia(client, settings.eia_api_key))
        if settings.sec_user_agent:
            out.append(await refresh_edgar(client, settings.sec_user_agent))
    finally:
        if own:
            await client.aclose()
    return {"sources": out, "enabled": enabled()}


_registered = False


def register() -> None:
    """Idempotent: importing api.context calls this. The job runs only when a source is configured."""
    global _registered
    from config import settings
    from jobs import job

    from context import equities

    equities.register()   # INTERNAL equities job (context_equities), only with the Alpaca market-data keys
    if _registered or not settings.poller_enabled or not any(enabled().values()):
        return
    _registered = True
    job("context_series", every_seconds=POLL_SECONDS, initial_delay_seconds=180)(refresh)


# --------------------------------------------------------------------------- reads

def catalog() -> list[dict]:
    """The PUBLIC catalogue: EIA and EDGAR only. Internal series (Alpaca equities) are never listed here."""
    from sqlalchemy import text

    cat = eia.catalog() + edgar.catalog()
    with _session()() as s:
        latest = {r.series_id: r for r in s.execute(text("""
            SELECT DISTINCT ON (series_id) series_id, period_start, period_end, value, published_at, first_seen_at, basis
            FROM context_series WHERE visibility = 'public' ORDER BY series_id, period_end DESC"""))}
        counts = dict(s.execute(text("""SELECT series_id, count(*) FROM context_series WHERE visibility = 'public'
                                         GROUP BY series_id""")).all())
    on = enabled()
    for c in cat:
        r = latest.get(c["series_id"])
        c["enabled"] = on[c["source"]]
        c["points"] = int(counts.get(c["series_id"], 0))
        c["latest"] = None if r is None else {"period_start": r.period_start, "period_end": r.period_end,
                                              "value": float(r.value), "published_at": r.published_at,
                                              "first_seen_at": r.first_seen_at, "basis": r.basis}
    return cat


def points(series_ids: list[str], since: date | None = None) -> list[dict]:
    from sqlalchemy import text

    with _session()() as s:
        rows = s.execute(text("""
            SELECT series_id, period_start, period_end, value, unit, source, basis, published_at, first_seen_at,
                   revisions, meta FROM context_series
            WHERE series_id = ANY(:ids) AND visibility = 'public'
              AND (CAST(:since AS date) IS NULL OR period_end >= :since)
            ORDER BY series_id, period_end"""), {"ids": series_ids, "since": since}).all()
    return [{**dict(r._mapping), "value": float(r.value)} for r in rows]


def power_by_region() -> list[dict]:
    """Per US state with current listings: the listings there, and the state's latest industrial and commercial price."""
    from sqlalchemy import text

    import market

    now = datetime.now(timezone.utc)
    with _session()() as s:
        rows = s.execute(text("""SELECT provider, region, country, observed_at FROM compute_listings
                                 WHERE canonical_gpu_name IS NOT NULL""")).all()
    by_state: dict[str, dict] = {}
    for r in rows:
        if now - r.observed_at > market.stale_after(r.provider):
            continue
        st = geo.us_state(r.provider, r.region, r.country)
        if not st:
            continue
        g = by_state.setdefault(st, {"state": st, "state_name": geo.STATES[st], "market_area": geo.market_area(st),
                                     "listings": 0, "providers": set(), "regions": set()})
        g["listings"] += 1
        g["providers"].add(r.provider)
        g["regions"].add(f"{r.provider}:{r.region}")
    ids = [eia.series_id(sec, st) for st in by_state for sec in eia.SECTORS]
    latest: dict[str, dict] = {}
    for p in points(ids):
        latest[p["series_id"]] = p   # ordered by period_end: the last one wins
    out = []
    for st, g in sorted(by_state.items()):
        g["providers"], g["regions"] = sorted(g["providers"]), sorted(g["regions"])
        for sec in eia.SECTORS:
            p = latest.get(eia.series_id(sec, st))
            g[f"price_{sec.lower()}"] = None if p is None else {"value": p["value"], "unit": p["unit"],
                                                               "period": p["period_start"].strftime("%Y-%m")}
        out.append(g)
    return out
