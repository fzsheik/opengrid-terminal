"""Raw writes, rollups, retention and reads for OpenGrid's own probes. Sessions come from `normalize.SessionLocal`
(tests patch that one name), like uptime/store.py.

Order of truth: probe_results (raw, untouched) -> probe_location_hourly + probe_target_hourly (derived, rebuildable
with rollup(hours=N)). Nothing is estimated: an hour with no results has no row.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

import normalize
from probes import consensus, targets
from store.uptime import (ProbeAgent, ProbeLocationHourly, ProbeResult, ProbeTargetHourly, ProviderIncident)
from uptime import store as uptime_store

MAX_SKEW_FUTURE = timedelta(minutes=5)
MAX_AGE = timedelta(hours=6)  # an agent may flush a backlog after an outage, but not rewrite history


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _f(v):
    return None if v is None else float(v)


def _hour(ts: datetime) -> datetime:
    return ts.replace(minute=0, second=0, microsecond=0)


# --------------------------------------------------------------------------- raw writes

def record(results: list[dict], agent_id: str, location: str, now: datetime | None = None) -> dict:
    """Store raw results. Unknown targets and implausible times are refused (counted), duplicates ignored."""
    now = now or _now()
    rows, refused = [], 0
    for r in results:
        t = r.get("observed_at")
        if r.get("target_id") not in targets.BY_ID or not isinstance(t, datetime) or t > now + MAX_SKEW_FUTURE or t < now - MAX_AGE:
            refused += 1
            continue
        rows.append({"target_id": r["target_id"], "location": location, "observed_at": t, "agent_id": agent_id,
                     "ok": bool(r.get("ok")), "status_code": r.get("status_code"), "error_class": r.get("error_class"),
                     "error": (r.get("error") or None) and str(r["error"])[:300], "dns_ms": r.get("dns_ms"),
                     "connect_ms": r.get("connect_ms"), "tls_ms": r.get("tls_ms"), "ttfb_ms": r.get("ttfb_ms"),
                     "total_ms": r.get("total_ms"), "resolved_ip": r.get("resolved_ip"), "received_at": now})
    stored = 0
    with normalize.SessionLocal.begin() as s:
        if rows:
            # RETURNING, not rowcount: with a multi-row INSERT the driver reports attempted rows, not inserted ones
            stored = len(s.execute(pg_insert(ProbeResult).values(rows).on_conflict_do_nothing()
                                   .returning(ProbeResult.target_id)).all())
        stmt = pg_insert(ProbeAgent).values(agent_id=agent_id, location=location, first_seen_at=now, last_seen_at=now,
                                            results_total=stored)
        s.execute(stmt.on_conflict_do_update(index_elements=["agent_id"], set_={
            "location": location, "last_seen_at": now, "results_total": ProbeAgent.results_total + stored}))
    return {"received": len(results), "stored": stored, "duplicates": len(rows) - stored, "refused": refused}


def prune_raw(days: int) -> int:
    with normalize.SessionLocal.begin() as s:
        return s.execute(delete(ProbeResult).where(ProbeResult.observed_at < _now() - timedelta(days=days))).rowcount


# --------------------------------------------------------------------------- rollups (derived, idempotent)

def rollup(hours: int = 3, now: datetime | None = None) -> dict:
    """Recompute the last `hours` hourly buckets (current one included) from the raw rows."""
    now = now or _now()
    first = _hour(now) - timedelta(hours=hours - 1)
    with normalize.SessionLocal() as s:
        raw = s.scalars(select(ProbeResult).where(ProbeResult.observed_at >= first)).all()
    groups: dict[tuple[str, datetime], list[dict]] = defaultdict(list)
    for r in raw:
        groups[(r.target_id, _hour(r.observed_at))].append({
            "location": r.location, "observed_at": r.observed_at, "ok": r.ok, "error_class": r.error_class, "total_ms": r.total_ms})
    n_t = n_l = 0
    with normalize.SessionLocal.begin() as s:
        for (tid, bucket), rows in groups.items():
            interval = targets.BY_ID[tid].interval_s if tid in targets.BY_ID else 300
            out = consensus.rollup_hour(rows, interval)
            t = out["target"]
            tv = {"target_id": tid, "bucket": bucket, "checks": t["checks"], "ok_checks": t["ok_checks"],
                  "availability": t["availability"], "slots_ok": t["slots_ok"], "slots_regional": t["slots_regional"],
                  "slots_global": t["slots_global"], "slots_single": t["slots_single"], "slot_minutes": t["slot_minutes"],
                  "failing_locations": t["failing_locations"], "error_classes": t["error_classes"],
                  "locations": t["locations"], "latency_p50_ms": t["latency_p50_ms"], "computed_at": now}
            stmt = pg_insert(ProbeTargetHourly).values(tv)
            s.execute(stmt.on_conflict_do_update(index_elements=["target_id", "bucket"],
                                                 set_={k: stmt.excluded[k] for k in tv if k not in ("target_id", "bucket")}))
            n_t += 1
            for loc, l in out["locations"].items():
                lv = {"target_id": tid, "location": loc, "bucket": bucket, "checks": l["checks"], "ok_checks": l["ok_checks"],
                      "latency_p50_ms": l["latency_p50_ms"], "latency_p95_ms": l["latency_p95_ms"],
                      "latency_max_ms": l["latency_max_ms"], "error_classes": l["error_classes"]}
                stmt = pg_insert(ProbeLocationHourly).values(lv)
                s.execute(stmt.on_conflict_do_update(index_elements=["target_id", "location", "bucket"],
                                                     set_={k: stmt.excluded[k] for k in lv if k not in ("target_id", "location", "bucket")}))
                n_l += 1
    return {"target_hours": n_t, "location_hours": n_l}


# --------------------------------------------------------------------------- reads

def agents() -> list[dict]:
    with normalize.SessionLocal() as s:
        return [{"agent_id": a.agent_id, "location": a.location, "first_seen_at": a.first_seen_at,
                 "last_seen_at": a.last_seen_at, "results_total": a.results_total}
                for a in s.scalars(select(ProbeAgent).order_by(ProbeAgent.location, ProbeAgent.agent_id))]


def _current(provider_targets: list[str], minutes: int = 20) -> dict[str, dict]:
    """Latest raw result per (target, location) in the last `minutes`: the 'right now' view."""
    since = _now() - timedelta(minutes=minutes)
    with normalize.SessionLocal() as s:
        rows = s.scalars(select(ProbeResult).where(ProbeResult.target_id.in_(provider_targets), ProbeResult.observed_at >= since)
                         .order_by(ProbeResult.observed_at)).all()
    latest: dict[tuple[str, str], ProbeResult] = {}
    for r in rows:
        latest[(r.target_id, r.location)] = r
    out: dict[str, dict] = defaultdict(dict)
    for (tid, loc), r in latest.items():
        out[tid][loc] = {"ok": r.ok, "status_code": r.status_code, "error_class": r.error_class, "total_ms": r.total_ms,
                         "observed_at": r.observed_at}
    return out


def _status(by_loc: dict[str, dict]) -> str:
    if not by_loc:
        return "unknown"
    ok = [v["ok"] for v in by_loc.values()]
    return "up" if all(ok) else "down" if not any(ok) and len(ok) >= consensus.MIN_LOCATIONS else "degraded" if any(ok) else "unconfirmed"


def target_dict(t: targets.Target) -> dict:
    return t.public()


def summary(hours: int = 24) -> list[dict]:
    """One row per target provider: availability, latency and failure reasons over the window, who reported."""
    since = _hour(_now()) - timedelta(hours=hours - 1)
    with normalize.SessionLocal() as s:
        rows = s.scalars(select(ProbeTargetHourly).where(ProbeTargetHourly.bucket >= since)).all()
    by_t: dict[str, list[ProbeTargetHourly]] = defaultdict(list)
    for r in rows:
        by_t[r.target_id].append(r)
    cur = _current([t.id for t in targets.TARGETS])
    out = []
    for provider in sorted({t.provider for t in targets.TARGETS}):
        ts = targets.for_provider(provider)
        tsum = []
        for t in ts:
            hs = by_t.get(t.id, [])
            checks, ok = sum(h.checks for h in hs), sum(h.ok_checks for h in hs)
            locs = sorted({l for h in hs for l in h.locations})
            err: dict[str, int] = defaultdict(int)
            for h in hs:
                for k, v in (h.error_classes or {}).items():
                    err[k] += v
            lat = [h.latency_p50_ms for h in hs if h.latency_p50_ms is not None]
            tsum.append({**t.public(), "status": _status(cur.get(t.id, {})), "checks": checks,
                         "availability": (ok / checks * 100) if checks else None, "latency_p50_ms": consensus.percentile(lat, 0.5),
                         "locations": locs, "slots": {"regional": sum(h.slots_regional for h in hs), "global": sum(h.slots_global for h in hs),
                                                      "single": sum(h.slots_single for h in hs)}, "error_classes": dict(err)})
        meas = [x for x in tsum if x["checks"]]
        sev = {"down": 3, "degraded": 2, "unconfirmed": 2, "unknown": 1, "up": 0}
        out.append({"provider": provider, "targets": tsum,
                    "status": max((x["status"] for x in tsum), key=lambda v: sev.get(v, 1), default="unknown") if meas else "unknown",
                    "availability": min((x["availability"] for x in meas), default=None),
                    "latency_p50_ms": consensus.percentile([x["latency_p50_ms"] for x in meas if x["latency_p50_ms"] is not None], 0.5),
                    "locations": sorted({l for x in tsum for l in x["locations"]}),
                    "kinds": sorted({x["kind"] for x in tsum})})
    return out


def target_series(target_id: str, hours: int) -> dict:
    since = _hour(_now()) - timedelta(hours=hours - 1)
    with normalize.SessionLocal() as s:
        th = s.scalars(select(ProbeTargetHourly).where(ProbeTargetHourly.target_id == target_id, ProbeTargetHourly.bucket >= since)
                       .order_by(ProbeTargetHourly.bucket)).all()
        lh = s.scalars(select(ProbeLocationHourly).where(ProbeLocationHourly.target_id == target_id, ProbeLocationHourly.bucket >= since)
                       .order_by(ProbeLocationHourly.bucket)).all()
    locs = defaultdict(list)
    for l in lh:
        locs[l.location].append({"bucket": l.bucket, "checks": l.checks, "ok_checks": l.ok_checks, "latency_p50_ms": l.latency_p50_ms,
                                 "latency_p95_ms": l.latency_p95_ms, "latency_max_ms": l.latency_max_ms, "error_classes": l.error_classes})
    hourly = []
    for h in th:
        scope, basis = consensus.hour_scope({"slots_global": h.slots_global, "slots_regional": h.slots_regional,
                                             "slots_single": h.slots_single, "failing_locations": h.failing_locations or {}})
        hourly.append({"bucket": h.bucket, "checks": h.checks, "availability": _f(h.availability), "slots_ok": h.slots_ok,
                       "slots_regional": h.slots_regional, "slots_global": h.slots_global, "slots_single": h.slots_single,
                       "scope": scope, "scope_basis": basis, "failing_locations": h.failing_locations or {},
                       "error_classes": h.error_classes or {}, "locations": h.locations or [], "latency_p50_ms": h.latency_p50_ms})
    return {"hourly": hourly, "by_location": dict(locs), "current": _current([target_id]).get(target_id, {})}


def raw(target_id: str, location: str | None = None, limit: int = 100, failures_only: bool = False) -> list[dict]:
    q = select(ProbeResult).where(ProbeResult.target_id == target_id)
    if location:
        q = q.where(ProbeResult.location == location)
    if failures_only:
        q = q.where(ProbeResult.ok.is_(False))
    with normalize.SessionLocal() as s:
        return [{"target_id": r.target_id, "location": r.location, "observed_at": r.observed_at, "agent_id": r.agent_id,
                 "ok": r.ok, "status_code": r.status_code, "error_class": r.error_class, "error": r.error, "dns_ms": r.dns_ms,
                 "connect_ms": r.connect_ms, "tls_ms": r.tls_ms, "ttfb_ms": r.ttfb_ms, "total_ms": r.total_ms,
                 "resolved_ip": r.resolved_ip}
                for r in s.scalars(q.order_by(ProbeResult.observed_at.desc()).limit(max(1, min(limit, 500))))]


def _reported(provider: str, start: datetime, end: datetime, region: str | None) -> list[dict]:
    grace = uptime_store.GRACE
    with normalize.SessionLocal() as s:
        rows = s.scalars(select(ProviderIncident).where(ProviderIncident.provider == provider, ProviderIncident.started_at <= end + grace)).all()
    out = []
    for r in rows:
        if r.resolved_at is not None and r.resolved_at < start - grace:
            continue
        d = uptime_store._incident_dict(r, full=False)
        if uptime_store._region_ok(region, d):
            out.append({k: d[k] for k in ("id", "title", "scope", "regions", "impact", "url", "started_at", "resolved_at")})
    return out[:5]


def dips(provider: str, days: int = 7) -> list[dict]:
    """Every hour where some check failed, with the vantage-point scope, the failure reasons, the provider-region
    context (did this target's sibling regions dip too?) and provider-reported incidents that overlap."""
    days = max(1, min(days, 30))
    ts = targets.for_provider(provider)
    ids = [t.id for t in ts]
    since = _hour(_now()) - timedelta(days=days)
    with normalize.SessionLocal() as s:
        rows = s.scalars(select(ProbeTargetHourly).where(ProbeTargetHourly.target_id.in_(ids), ProbeTargetHourly.bucket >= since)).all()
    bad = [r for r in rows if r.ok_checks < r.checks]
    regional_t = [t for t in ts if t.region]
    dipped_at: dict[datetime, set[str]] = defaultdict(set)
    for r in bad:
        dipped_at[r.bucket].add(r.target_id)
    out = []
    for r in sorted(bad, key=lambda x: x.bucket, reverse=True):
        t = targets.BY_ID[r.target_id]
        scope, basis = consensus.hour_scope({"slots_global": r.slots_global, "slots_regional": r.slots_regional,
                                             "slots_single": r.slots_single, "failing_locations": r.failing_locations or {}})
        region_scope = region_basis = None
        if t.region and len(regional_t) >= 2:
            hit = [x for x in regional_t if x.id in dipped_at[r.bucket]]
            if len(hit) == len(regional_t):
                region_scope, region_basis = "all_regions", f"all {len(regional_t)} measured regions dipped in this hour"
            else:
                region_scope = "some_regions"
                region_basis = f"only {', '.join(sorted(x.region for x in hit))} dipped; {len(regional_t) - len(hit)} of {len(regional_t)} measured regions did not"
        out.append({"target_id": r.target_id, "target": t.name, "kind": t.kind, "region": t.region, "bucket": r.bucket,
                    "availability": _f(r.availability), "checks": r.checks, "failed_checks": r.checks - r.ok_checks,
                    "scope": scope, "scope_basis": basis, "region_scope": region_scope, "region_basis": region_basis,
                    "failing_locations": r.failing_locations or {}, "error_classes": r.error_classes or {},
                    "locations_reporting": r.locations or [],
                    "reported": _reported(provider, r.bucket, r.bucket + timedelta(hours=1), t.region)})
    return out
