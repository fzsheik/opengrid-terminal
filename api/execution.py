"""Execution observability: metrics, traces, routing quality, reliability, economics, the first-route checklist.

    GET  /v1/admin/metrics                      in-process counters/latencies + DB-derived execution counts  [admin]
    GET  /v1/admin/trace/{id}                   route request or deployment: the whole chain, in time order  [admin]
    GET  /v1/admin/checklist?provider=          first-real-route checklist, computed live                    [admin]
    GET  /v1/admin/execution/overview           live deployments, stuck states, orphans, reconciliation runs [admin]
    GET  /v1/admin/quality                      routing quality aggregates + per-route outcomes (all)        [admin]
    POST /v1/admin/quality/recompute            recompute route_outcomes now (full=true: every row)          [admin]
    GET  /v1/quality                            the same, for the calling account                            [deployments:read]
    GET  /v1/reliability                        provider reliability from real transactions (customer)       [data:read]
    GET  /v1/admin/reliability                  ... plus validation deployments and in-process call counters [admin]
    GET  /v1/economics                          savings vs market median (account-scoped; operator: all)     [deployments:read]
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text

import jobs
import normalize
import observability as obs
from accounts.auth import Principal, require_scope
from api.common import TRANSACTION, envelope
from cache import ttl_cache
from config import settings
from routing import checklist, quality, reliability

router = APIRouter()
admin = require_scope("admin")

TERMINAL = ("terminated", "provision_failed", "provider_rejected", "rejected", "quote_failed", "quote_expired", "failed")
STUCK = ("provisioning", "provider_timeout", "launch_unknown", "terminating", "termination_failed", "orphan_suspected",
         "credentials_unavailable", "stopping", "routing", "pending")


def _now():
    return datetime.now(timezone.utc)


def _count(s, sql: str, **p) -> int:
    return int(s.execute(text(sql), p).scalar() or 0)


@ttl_cache(300)
def _poll_failures(hours: float) -> dict:
    t = _now() - timedelta(hours=hours)
    with normalize.SessionLocal() as s:
        rows = s.execute(text("SELECT provider, count(*) FILTER (WHERE NOT ok), count(*) FROM raw_snapshots "
                              "WHERE fetched_at >= :t GROUP BY provider"), {"t": t}).all()
    return {"by_provider": {p: {"failed": f, "polls": n} for p, f, n in rows if f},
            "failed": sum(r[1] for r in rows), "polls": sum(r[2] for r in rows)}


def _lifecycle(s, since: datetime) -> dict:
    """created -> approved -> provisioning -> running -> terminated durations from deployment_events."""
    cols = obs.columns(s, "deployments")
    appr = "d.approved_at" if "approved_at" in cols else "NULL::timestamptz"
    rows = s.execute(text(
        f"SELECT d.deployment_id, d.created_at, {appr} AS approved_at,"
        " min(e.at) FILTER (WHERE e.to_status = 'approved') AS approved_ev,"
        " min(e.at) FILTER (WHERE e.to_status = 'provisioning') AS provisioning,"
        " min(e.at) FILTER (WHERE e.to_status = 'running') AS running,"
        " min(e.at) FILTER (WHERE e.to_status = 'terminated') AS terminated"
        " FROM deployments d LEFT JOIN deployment_events e ON e.deployment_id = d.deployment_id"
        " WHERE d.created_at >= :t GROUP BY 1, 2, 3"), {"t": since}).all()
    spans = {"created_to_approved": [], "approved_to_provisioning": [], "provisioning_to_running": [],
             "running_to_terminated": [], "created_to_running": []}
    for r in rows:
        approved = r.approved_at or r.approved_ev

        def put(name, a, b):
            if a and b and b >= a:
                spans[name].append((b - a).total_seconds())
        put("created_to_approved", r.created_at, approved)
        put("approved_to_provisioning", approved, r.provisioning)
        put("provisioning_to_running", r.provisioning, r.running)
        put("running_to_terminated", r.running, r.terminated)
        put("created_to_running", r.created_at, r.running)
    return {k: {"n": len(v), "p50_s": obs.percentile(v, 0.5), "p95_s": obs.percentile(v, 0.95)}
            for k, v in spans.items()}


def _jobs() -> dict:
    now = _now()
    out = {"running": [], "overdue": [], "failing": [], "total": len(jobs.JOBS)}
    for j in jobs.JOBS.values():
        if j.running:
            out["running"].append(j.name)
        last = j.last_started
        if last is not None and now - last > timedelta(seconds=2 * j.every_seconds + 60):
            out["overdue"].append({"name": j.name, "last_started": last.isoformat(), "every_seconds": j.every_seconds})
        if j.last_error:
            out["failing"].append({"name": j.name, "error": obs.redact(j.last_error)[:300]})
    out["never_started"] = [j.name for j in jobs.JOBS.values() if j.last_started is None]
    out["note"] = ("jobs run in this process; never_started is expected when OPENGRID_NO_JOBS is set or right "
                   "after start")
    return out


def db_metrics(hours: float) -> dict:
    """Execution counts over the window, from the database (survive restarts)."""
    t = _now() - timedelta(hours=hours)
    out, unavailable = {}, []
    with normalize.SessionLocal() as s:
        out["route_previews"] = _count(s, "SELECT count(*) FROM route_requests WHERE preview AND created_at >= :t", t=t)
        out["route_requests"] = _count(s, "SELECT count(*) FROM route_requests WHERE NOT preview AND created_at >= :t",
                                       t=t)
        out["deployments_created"] = _count(s, "SELECT count(*) FROM deployments WHERE created_at >= :t", t=t)
        out["route_launches"] = _count(s, "SELECT count(*) FROM provision_attempts WHERE started_at >= :t", t=t)
        out["launch_failures"] = _count(s, "SELECT count(*) FROM provision_attempts WHERE started_at >= :t AND NOT ok",
                                        t=t)
        if "approved_at" in obs.columns(s, "deployments"):
            out["approvals"] = _count(s, "SELECT count(*) FROM deployments WHERE approved_at >= :t", t=t)
        else:
            out["approvals"] = None
            unavailable.append("approvals: deployments.approved_at not present (execution core migration)")
        out["terminations"] = _count(s, "SELECT count(*) FROM deployment_events WHERE to_status = 'terminated' "
                                        "AND at >= :t", t=t)
        out["termination_failures"] = _count(s, "SELECT count(*) FROM deployment_events WHERE "
                                                "to_status = 'termination_failed' AND at >= :t", t=t)
        if obs.has_table(s, "reconciliation_runs"):
            cols = obs.columns(s, "reconciliation_runs")
            tc = next((c for c in ("started_at", "created_at", "at", "finished_at") if c in cols), None)
            fail = " OR ".join(x for x in ("error IS NOT NULL" if "error" in cols else "",
                                           "status IN ('failed','error')" if "status" in cols else "") if x) or "false"
            out["reconciliation_runs"] = _count(s, f"SELECT count(*) FROM reconciliation_runs WHERE {tc} >= :t", t=t) \
                if tc else None
            out["reconciliation_failures"] = _count(s, f"SELECT count(*) FROM reconciliation_runs WHERE {tc} >= :t "
                                                       f"AND ({fail})", t=t) if tc else None
        else:
            out["reconciliation_runs"] = out["reconciliation_failures"] = None
            unavailable.append("reconciliation_runs: table not present (adapters/reconciliation migration)")
        if obs.has_table(s, "orphan_resources"):
            cols = obs.columns(s, "orphan_resources")
            tc = next((c for c in ("detected_at", "first_seen_at", "created_at") if c in cols), None)
            out["orphan_detections"] = _count(s, f"SELECT count(*) FROM orphan_resources WHERE {tc} >= :t", t=t) \
                if tc else _count(s, "SELECT count(*) FROM orphan_resources")
        else:
            out["orphan_detections"] = None
            unavailable.append("orphan_resources: table not present (adapters/reconciliation migration)")
        nf = s.execute(text("SELECT source_id, count(*) FROM news_fetch_log WHERE NOT ok AND fetched_at >= :t "
                            "GROUP BY 1"), {"t": t}).all()
        out["news_ingestion_failures"] = {"failed": sum(n for _, n in nf), "by_source": {a: n for a, n in nf}}
        out["deployment_lifecycle_seconds"] = _lifecycle(s, _now() - timedelta(days=30))
    try:
        out["polling_failures"] = _poll_failures(hours)
    except Exception as e:
        out["polling_failures"] = None
        unavailable.append(f"polling_failures: {type(e).__name__}")
    return {"window_hours": hours, **out, "unavailable": unavailable,
            "lifecycle_window": "deployments created in the last 30 days"}


@router.get("/v1/admin/metrics", tags=["admin"], summary="Execution and platform metrics")
def admin_metrics(hours: float = Query(24, gt=0, le=24 * 90), who: Principal = Depends(admin)):
    snap = obs.metrics.snapshot()
    db_lat = next((h for h in snap["histograms"] if h["name"] == "db_latency_ms"), None)
    return envelope({"in_process": snap, "database": db_metrics(hours), "jobs": _jobs(),
                     "db_latency_ms": db_lat or {"status": "unavailable: no DB ping recorded yet (job runs every 60s)"},
                     "provider_calls": reliability.in_process_calls()}, methodology="routing-quality")


@router.get("/v1/admin/trace/{ident}", tags=["admin"], summary="Full chain for a route request or deployment")
def admin_trace(ident: str, who: Principal = Depends(admin)):
    t = obs.trace(ident)
    if t is None:
        raise HTTPException(404, "no route request or deployment with that id")
    return envelope(t, kind=TRANSACTION, methodology="routing-quality")


@router.get("/v1/admin/checklist", tags=["admin"], summary="First-real-route checklist, computed live")
def admin_checklist(provider: str = Query(..., min_length=2, max_length=64), route_request_id: str | None = None,
                    account_id: int | None = None, probe: bool = True, who: Principal = Depends(admin)):
    return envelope(checklist.checklist(provider, route_request_id, account_id, probe), methodology="first-live-route")


@router.get("/v1/admin/execution/overview", tags=["admin"], summary="Live deployments, stuck states, orphans")
def admin_overview(who: Principal = Depends(admin)):
    now = _now()
    stale = timedelta(minutes=float(getattr(settings, "provisioning_timeout_minutes", 15)))
    out, unavailable = {}, []
    with normalize.SessionLocal() as s:
        live = obs.rows(s, "deployments", "status <> ALL(:t)", {"t": list(TERMINAL)}, "created_at", 1000)
        items = []
        for d in live:
            created = datetime.fromisoformat(d["created_at"]) if d.get("created_at") else None
            run = datetime.fromisoformat(d["running_since"]) if d.get("running_since") else None
            price = d.get("actual_price_per_gpu_hour") or d.get("quoted_price_per_gpu_hour")
            hours = ((d.get("uptime_seconds") or 0) + ((now - run).total_seconds() if run else 0)) / 3600
            items.append({
                "deployment_id": d["deployment_id"], "account_id": d.get("account_id"), "provider": d.get("provider"),
                "status": d.get("status"), "purpose": d.get("purpose"), "gpu": d.get("gpu"),
                "gpu_count": d.get("gpu_count"), "created_at": d.get("created_at"),
                "age_minutes": round((now - created).total_seconds() / 60, 1) if created else None,
                "accrued_cost_estimate_usd": round(float(price) * int(d.get("gpu_count") or 1) * hours, 2)
                if price is not None else None,
                "stuck": bool(created and d.get("status") in STUCK and now - created > stale),
            })
        out["live_deployments"] = items
        out["stuck"] = [i for i in items if i["stuck"]]
        if obs.has_table(s, "orphan_resources"):
            cols = obs.columns(s, "orphan_resources")
            where = "resolved_at IS NULL" if "resolved_at" in cols else (
                "status <> 'resolved'" if "status" in cols else "true")
            out["orphans"] = obs.redact_value(obs.rows(s, "orphan_resources", where, None, None, 500))
        else:
            out["orphans"] = None
            unavailable.append("orphan_resources: table not present")
        if obs.has_table(s, "reconciliation_runs"):
            cols = obs.columns(s, "reconciliation_runs")
            tc = next((c for c in ("started_at", "created_at", "at") if c in cols), None)
            out["reconciliation_runs"] = obs.redact_value(
                obs.rows(s, "reconciliation_runs", "true", None, f"{tc} DESC" if tc else None, 20))
        else:
            out["reconciliation_runs"] = None
            unavailable.append("reconciliation_runs: table not present")
    out["unavailable"] = unavailable
    out["note"] = "accrued cost is ESTIMATED: (execution price, else quote) x GPUs x observed running hours"
    return envelope(out, kind=TRANSACTION, methodology="routing-quality")


def _since(days: float | None):
    return None if not days else _now() - timedelta(days=days)


@router.get("/v1/admin/quality", tags=["admin"], summary="Routing quality: aggregates and per-route outcomes")
def admin_quality(days: float | None = Query(30, gt=0, le=3650), include_validation: bool = False,
                  limit: int = Query(100, ge=1, le=1000), who: Principal = Depends(admin)):
    since = _since(days)
    return envelope({"summary": quality.summary(None, since, include_validation),
                     "outcomes": quality.outcomes(None, since, include_validation, limit=limit)},
                    kind=TRANSACTION, methodology="routing-quality")


@router.post("/v1/admin/quality/recompute", tags=["admin"], summary="Recompute route outcomes now")
def admin_quality_recompute(full: bool = False, who: Principal = Depends(admin)):
    return envelope(quality.recompute(full=full), methodology="routing-quality")


@router.get("/v1/quality", tags=["routing"], summary="Routing quality for your account")
def my_quality(days: float | None = Query(90, gt=0, le=3650), limit: int = Query(100, ge=1, le=1000),
               who: Principal = Depends(require_scope("deployments:read"))):
    acct = None if who.kind == "operator" else who.account_id  # the operator sees every account
    since = _since(days)
    return envelope({"summary": quality.summary(acct, since), "outcomes": quality.outcomes(acct, since, limit=limit)},
                    kind=TRANSACTION, methodology="routing-quality")


@router.get("/v1/reliability", tags=["routing"], summary="Provider reliability from OpenGrid's real launches")
def get_reliability(provider: str | None = None, who: Principal = Depends(require_scope("data:read"))):
    return envelope(reliability.report(provider=provider), kind=TRANSACTION, methodology="reliability")


@router.get("/v1/admin/reliability", tags=["admin"], summary="Reliability incl. validation deployments")
def admin_reliability(provider: str | None = None, who: Principal = Depends(admin)):
    return envelope(reliability.admin_report(provider), kind=TRANSACTION, methodology="reliability")


@router.get("/v1/economics", tags=["routing"], summary="Savings vs the market median, per deployment and in total")
def get_economics(days: float | None = Query(None, gt=0, le=3650), account_id: int | None = None,
                  who: Principal = Depends(require_scope("deployments:read"))):
    if who.kind != "operator":
        if account_id is not None and account_id != who.account_id:
            raise HTTPException(403, "a key sees only its own account")
        account_id = who.account_id
    return envelope(quality.economics(account_id, _since(days)), kind=TRANSACTION, methodology="economics",
                    note="savings exist only with a valid comparison: same canonical GPU, on-demand, market median "
                         "from >= 3 providers at decision time")
