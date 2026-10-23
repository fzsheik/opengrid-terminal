"""Writes and reads for the uptime tables. Sessions come from `normalize.SessionLocal` so tests can point
everything at a scratch database by patching that one name (same contract as news/store.py).

Reads never invent numbers: a window with no hourly rows returns None, not 100.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

import normalize
from store.uptime import (ProviderIncident, UptimeDaily, UptimeFetchLog, UptimeHourly, UptimeProbeSample,
                          UptimeService)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _f(v):
    return None if v is None else float(v)


# --------------------------------------------------------------------------- writes

def upsert_service(row: dict, now: datetime) -> None:
    with normalize.SessionLocal.begin() as s:
        values = {**row, "first_seen_at": now, "last_seen_at": now}
        stmt = pg_insert(UptimeService).values(**values)
        mutable = {k: stmt.excluded[k] for k in row if k != "id"} | {"last_seen_at": now}
        s.execute(stmt.on_conflict_do_update(index_elements=["id"], set_=mutable))


def save_detail(service_id: str, detail: dict, now: datetime) -> None:
    with normalize.SessionLocal.begin() as s:
        svc = s.get(UptimeService, service_id)
        if svc is not None:
            svc.checks = detail["checks"]
        for p in detail["probes"]:
            s.add(UptimeProbeSample(service_id=service_id, probe_id=p["probe_id"], observed_at=now,
                                    status=p["status"], latency_ms=p["latency_ms"]))


def save_points(service_id: str, points: list[dict], now: datetime) -> int:
    if not points:
        return 0
    rows = [{**p, "service_id": service_id, "fetched_at": now} for p in points]
    with normalize.SessionLocal.begin() as s:
        stmt = pg_insert(UptimeHourly).values(rows)
        s.execute(stmt.on_conflict_do_update(
            index_elements=["service_id", "bucket"],
            set_={k: stmt.excluded[k] for k in ("availability_pct", "status", "down_minutes", "degraded_minutes",
                                                "latency_p50_ms", "fetched_at")}))
    return len(rows)


def save_daily(service_id: str, points: list[dict], now: datetime) -> int:
    if not points:
        return 0
    rows = [{**p, "service_id": service_id, "fetched_at": now} for p in points]
    with normalize.SessionLocal.begin() as s:
        stmt = pg_insert(UptimeDaily).values(rows)
        s.execute(stmt.on_conflict_do_update(
            index_elements=["service_id", "bucket"],
            set_={k: stmt.excluded[k] for k in ("availability_pct", "status", "down_minutes", "degraded_minutes",
                                                "latency_p50_ms", "fetched_at")}))
    return len(rows)


def daily_counts() -> dict[str, int]:
    """Stored daily buckets per service: a service with fewer than a month is backfilled from the long period."""
    with normalize.SessionLocal() as s:
        return dict(s.execute(select(UptimeDaily.service_id, func.count()).group_by(UptimeDaily.service_id)).all())


def log_fetch(fetched_at: datetime, ok: bool, services: int, points: int, error: str | None, duration_ms: int) -> None:
    with normalize.SessionLocal.begin() as s:
        s.add(UptimeFetchLog(fetched_at=fetched_at, ok=ok, services=services, points=points,
                             error=(error or None) and error[:1000], duration_ms=duration_ms))


def prune(probe_days: int, log_days: int = 90) -> dict:
    now = _now()
    with normalize.SessionLocal.begin() as s:
        a = s.execute(delete(UptimeProbeSample).where(UptimeProbeSample.observed_at < now - timedelta(days=probe_days))).rowcount
        b = s.execute(delete(UptimeFetchLog).where(UptimeFetchLog.fetched_at < now - timedelta(days=log_days))).rowcount
    return {"probe_samples": a, "fetch_log": b}


# --------------------------------------------------------------------------- reads

def _service_dict(svc: UptimeService) -> dict:
    return {"id": svc.id, "provider": svc.provider, "name": svc.name, "category": svc.category, "region": svc.region,
            "status": svc.current_status, "checks_count": svc.checks_count, "checks": svc.checks or [],
            "availability": {"24h": _f(svc.availability_24h), "7d": _f(svc.availability_7d),
                             "30d": _f(svc.availability_30d), "90d": _f(svc.availability_90d)},
            "homepage_url": svc.homepage_url, "status_page_url": svc.status_page_url,
            "last_seen_at": svc.last_seen_at}


def latency_stats(service_ids: list[str], hours: int) -> dict[str, dict]:
    """Over the last `hours`: p50 of the hourly p50s (the median of medians), the worst hourly p50, and the
    hours with data. Hourly p50 is already a cross-probe figure, so this is a trend, not a percentile of requests."""
    since = _now() - timedelta(hours=hours)
    with normalize.SessionLocal() as s:
        rows = s.execute(
            select(UptimeHourly.service_id,
                   func.percentile_cont(0.5).within_group(UptimeHourly.latency_p50_ms),
                   func.max(UptimeHourly.latency_p50_ms), func.min(UptimeHourly.latency_p50_ms),
                   func.count(UptimeHourly.latency_p50_ms),
                   func.coalesce(func.sum(UptimeHourly.down_minutes), 0),
                   func.coalesce(func.sum(UptimeHourly.degraded_minutes), 0))
            .where(UptimeHourly.service_id.in_(service_ids), UptimeHourly.bucket >= since,
                   UptimeHourly.latency_p50_ms.is_not(None))
            .group_by(UptimeHourly.service_id)).all()
    return {r[0]: {"latency_p50_ms": None if r[1] is None else round(float(r[1])), "latency_max_ms": r[2],
                   "latency_min_ms": r[3], "hours_with_latency": r[4], "down_minutes": int(r[5]),
                   "degraded_minutes": int(r[6])} for r in rows}


def services(provider: str | None = None, category: str | None = None) -> list[dict]:
    q = select(UptimeService).order_by(UptimeService.provider, UptimeService.category, UptimeService.id)
    if provider:
        q = q.where(UptimeService.provider == provider)
    if category:
        q = q.where(UptimeService.category == category)
    with normalize.SessionLocal() as s:
        out = [_service_dict(x) for x in s.scalars(q)]
    stats = latency_stats([x["id"] for x in out], 24) if out else {}
    for x in out:
        x["last_24h"] = stats.get(x["id"])
    return out


def history(service_id: str, hours: int) -> list[dict]:
    since = _now() - timedelta(hours=hours)
    with normalize.SessionLocal() as s:
        rows = s.scalars(select(UptimeHourly).where(UptimeHourly.service_id == service_id, UptimeHourly.bucket >= since)
                         .order_by(UptimeHourly.bucket)).all()
    return [{"bucket": r.bucket, "availability_pct": _f(r.availability_pct), "status": r.status,
             "down_minutes": r.down_minutes, "degraded_minutes": r.degraded_minutes,
             "latency_p50_ms": r.latency_p50_ms} for r in rows]


def daily(service_id: str, days: int) -> list[dict]:
    since = _now() - timedelta(days=days)
    with normalize.SessionLocal() as s:
        rows = s.scalars(select(UptimeDaily).where(UptimeDaily.service_id == service_id, UptimeDaily.bucket >= since)
                         .order_by(UptimeDaily.bucket)).all()
    return [{"bucket": r.bucket, "availability_pct": _f(r.availability_pct), "status": r.status,
             "down_minutes": r.down_minutes, "degraded_minutes": r.degraded_minutes,
             "latency_p50_ms": r.latency_p50_ms} for r in rows]


def probes(service_id: str, hours: int = 24) -> list[dict]:
    """Per probe: the latest sample, and the range over `hours` of our own polling."""
    since = _now() - timedelta(hours=hours)
    with normalize.SessionLocal() as s:
        rows = s.scalars(select(UptimeProbeSample).where(UptimeProbeSample.service_id == service_id,
                                                         UptimeProbeSample.observed_at >= since)
                         .order_by(UptimeProbeSample.observed_at)).all()
    by: dict[str, list] = {}
    for r in rows:
        by.setdefault(r.probe_id, []).append(r)
    out = []
    for pid, rs in sorted(by.items()):
        lat = [r.latency_ms for r in rs if r.latency_ms is not None]
        last = rs[-1]
        out.append({"probe_id": pid, "status": last.status, "latency_ms": last.latency_ms, "observed_at": last.observed_at,
                    "samples": len(rs), "latency_min_ms": min(lat) if lat else None,
                    "latency_max_ms": max(lat) if lat else None})
    return out


def health() -> dict:
    with normalize.SessionLocal() as s:
        last = s.scalars(select(UptimeFetchLog).order_by(UptimeFetchLog.fetched_at.desc()).limit(1)).first()
        last_ok = s.scalars(select(UptimeFetchLog).where(UptimeFetchLog.ok).order_by(UptimeFetchLog.fetched_at.desc())
                            .limit(1)).first()
        n = s.scalar(select(func.count()).select_from(UptimeService)) or 0
        oldest = s.scalar(select(func.min(UptimeHourly.bucket)))
        oldest_daily = s.scalar(select(func.min(UptimeDaily.bucket)))
    return {"services": n, "last_fetch_at": last.fetched_at if last else None, "last_ok": last.ok if last else None,
            "last_error": last.error if last else None, "last_ok_at": last_ok.fetched_at if last_ok else None,
            "history_since": oldest, "daily_since": oldest_daily}


# --------------------------------------------------------------------------- incidents (reported by the provider)

def upsert_incidents(rows: list[dict], now: datetime) -> int:
    if not rows:
        return 0
    with normalize.SessionLocal.begin() as s:
        stmt = pg_insert(ProviderIncident).values([{**r, "first_seen_at": now, "last_seen_at": now} for r in rows])
        keep = ("source", "title", "impact", "status", "started_at", "resolved_at", "url", "scope", "scope_basis",
                "regions", "components", "updates")
        s.execute(stmt.on_conflict_do_update(index_elements=["provider", "external_id"],
                                             set_={**{k: stmt.excluded[k] for k in keep}, "last_seen_at": now}))
    return len(rows)


def _incident_dict(r: ProviderIncident, full: bool = True) -> dict:
    d = {"id": r.external_id, "provider": r.provider, "title": r.title, "impact": r.impact, "status": r.status,
         "started_at": r.started_at, "resolved_at": r.resolved_at, "url": r.url, "scope": r.scope,
         "scope_basis": r.scope_basis, "regions": r.regions or [], "components": r.components or []}
    if full:
        d["updates"] = r.updates or []
    return d


def incidents(provider: str | None = None, days: int = 90, scope: str | None = None, limit: int = 200) -> list[dict]:
    since = _now() - timedelta(days=days)
    q = select(ProviderIncident).where(or_(ProviderIncident.started_at >= since, ProviderIncident.resolved_at.is_(None),
                                           ProviderIncident.resolved_at >= since))
    if provider:
        q = q.where(ProviderIncident.provider == provider)
    if scope:
        q = q.where(ProviderIncident.scope == scope)
    with normalize.SessionLocal() as s:
        return [_incident_dict(r) for r in s.scalars(q.order_by(ProviderIncident.started_at.desc()).limit(limit))]


def incident_counts(days: int = 30) -> dict[str, dict]:
    """Per provider with at least one stored incident: counts by scope in the window. Providers with no status feed
    are absent, which the caller reads as 'no feed', not 'zero incidents'."""
    since = _now() - timedelta(days=days)
    with normalize.SessionLocal() as s:
        feeds = {p for (p,) in s.execute(select(ProviderIncident.provider).distinct())}
        rows = s.execute(select(ProviderIncident.provider, ProviderIncident.scope, func.count())
                         .where(ProviderIncident.started_at >= since).group_by(ProviderIncident.provider, ProviderIncident.scope)).all()
    out = {p: {"total": 0, "global": 0, "regional": 0, "unspecified": 0} for p in feeds}
    for p, sc, n in rows:
        out[p][sc] = n
        out[p]["total"] += n
    return out


# --------------------------------------------------------------------------- dips (measured), with scope and cause

GRACE = timedelta(minutes=30)  # an incident this close to a dip counts as overlapping it


def _region_ok(service_region: str | None, inc: dict) -> bool:
    """A regional incident only explains a dip in a service of a different region if it is global."""
    if inc["scope"] != "regional" or not service_region:
        return True
    sr = service_region.lower()
    return any(sr == r or sr.startswith(r) or r.startswith(sr) for r in inc["regions"])


def dips(provider: str, days: int = 7, service_id: str | None = None) -> list[dict]:
    from uptime import scope as scoping

    days = max(1, min(days, 90))
    hourly = days <= 7
    table = UptimeHourly if hourly else UptimeDaily
    res = timedelta(hours=1) if hourly else timedelta(days=1)
    since = _now() - timedelta(days=days)
    with normalize.SessionLocal() as s:
        svcs = [x for x in s.scalars(select(UptimeService).where(UptimeService.provider == provider,
                                                                   UptimeService.category == "iaas"))]
        if not svcs:
            return []
        ids = [x.id for x in svcs]
        rows = s.scalars(select(table).where(table.service_id.in_(ids), table.bucket >= since,
                                             or_(table.down_minutes > 0, table.degraded_minutes > 0,
                                                 table.availability_pct < 100))
                         .order_by(table.bucket.desc())).all()
        fails = {}
        if hourly and rows:
            for sm in s.scalars(select(UptimeProbeSample).where(UptimeProbeSample.service_id.in_(ids),
                                                                UptimeProbeSample.observed_at >= since,
                                                                UptimeProbeSample.status != "up")):
                fails.setdefault((sm.service_id, sm.observed_at.replace(minute=0, second=0, microsecond=0)), set()).add(sm.probe_id)
        incs = [_incident_dict(r, full=False) for r in s.scalars(select(ProviderIncident).where(
            ProviderIncident.provider == provider, ProviderIncident.started_at <= _now(),
            or_(ProviderIncident.resolved_at.is_(None), ProviderIncident.resolved_at >= since - GRACE)))]
    by_svc = {x.id: x for x in svcs}
    measured = [{"id": x.id, "region": x.region} for x in svcs]
    by_bucket: dict[datetime, dict[str, dict]] = {}
    for r in rows:
        by_bucket.setdefault(r.bucket, {})[r.service_id] = {"down_minutes": r.down_minutes or 0, "degraded_minutes": r.degraded_minutes or 0}
    out = []
    for r in rows:
        if service_id and r.service_id != service_id:
            continue
        svc = by_svc[r.service_id]
        row = {"id": r.service_id, "region": svc.region, "down_minutes": r.down_minutes or 0, "degraded_minutes": r.degraded_minutes or 0}
        sc, basis = scoping.classify_dip(row, by_bucket[r.bucket], measured)
        end = r.bucket + res
        related = [i for i in incs if i["started_at"] <= end + GRACE and (i["resolved_at"] is None or i["resolved_at"] >= r.bucket - GRACE)
                   and _region_ok(svc.region, i)]
        out.append({"service_id": r.service_id, "service": svc.name, "region": svc.region, "bucket": r.bucket,
                    "resolution": "hour" if hourly else "day", "availability_pct": _f(r.availability_pct),
                    "down_minutes": row["down_minutes"], "degraded_minutes": row["degraded_minutes"],
                    "kind": scoping.dip_kind(row["down_minutes"], row["degraded_minutes"]), "scope": sc, "scope_basis": basis,
                    "probes_failing": sorted(fails.get((r.service_id, r.bucket), [])),
                    "reported": [{"id": i["id"], "title": i["title"], "scope": i["scope"], "regions": i["regions"],
                                  "impact": i["impact"], "url": i["url"], "started_at": i["started_at"],
                                  "resolved_at": i["resolved_at"]} for i in related[:5]]})
    return out
