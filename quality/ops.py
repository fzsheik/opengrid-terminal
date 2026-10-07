"""The internal ops / data-quality view: one place to see what is broken.

Reads only; every number here comes from raw_snapshots, compute_listings, the quality
tables, jobs.JOBS, or (when they exist) other domains' tables, probed with to_regclass
because those are created by other modules and may not be there yet.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import text

import jobs
import normalize
from quality import incidents, quarantine, trust

_ERRORS_24H = text(
    """SELECT provider, endpoint, count(*) AS failures, max(fetched_at) AS last_at,
              (array_agg(left(coalesce(error, ''), 300) ORDER BY fetched_at DESC))[1] AS last_error,
              (array_agg(status_code ORDER BY fetched_at DESC))[1] AS last_status
       FROM raw_snapshots WHERE NOT ok AND fetched_at >= :since
       GROUP BY provider, endpoint ORDER BY max(fetched_at) DESC"""
)


def _now():
    return datetime.now(timezone.utc)


def provider_errors(hours: float = 24) -> list[dict]:
    with normalize.SessionLocal() as s:
        return [dict(r._mapping) for r in s.execute(_ERRORS_24H, {"since": _now() - timedelta(hours=hours)})]


def jobs_health() -> list[dict]:
    now = _now()
    out = []
    for j in jobs.JOBS.values():
        d = j.describe()
        last = j.last_finished
        d["overdue"] = bool(last and (now - last).total_seconds() > 3 * j.every_seconds)
        d["status"] = ("never_run" if j.runs == 0 else "failing" if j.last_error else "overdue" if d["overdue"] else "ok")
        out.append(d)
    return out


def stale_listings(include_aging: bool = True, provider: str | None = None) -> list[dict]:
    """Listings not seen recently, judged against each provider's own polling interval."""
    q = "SELECT provider, listing_id, canonical_gpu_name, raw_gpu_name, region, observed_at FROM compute_listings"
    params = {}
    if provider:
        q += " WHERE provider = :p"
        params["p"] = provider
    now = _now()
    out = []
    with normalize.SessionLocal() as s:
        for r in s.execute(text(q), params):
            fresh, age = trust.freshness(r.provider, r.observed_at, now)
            if fresh == "stale" or (include_aging and fresh == "aging"):
                out.append({**dict(r._mapping), "freshness": fresh, "age_seconds": round(age, 1)})
    out.sort(key=lambda x: -x["age_seconds"])
    return out


_PROBES = {
    # table: (time columns to try, failure predicates by column)
    "routing_decisions": ("created_at", "decided_at", "at"),
    "deployments": ("updated_at", "created_at"),
    "provision_attempts": ("started_at",),
    "news_sources": ("last_fetched_at", "last_checked_at", "updated_at", "created_at"),
    "news_fetch_log": ("fetched_at", "created_at", "at"),
}
_ERROR_COLS = ("error", "last_error", "error_message", "failure_reason")
_FAILED_STATUSES = ("failed", "error", "errored", "provision_failed")


def probe(table: str) -> dict:
    """Row and failure counts for another domain's table, if it exists yet.

    Column names come from information_schema and are matched against fixed lists, so
    nothing user-supplied reaches the SQL.
    """
    with normalize.SessionLocal() as s:
        if s.execute(text("SELECT to_regclass(:t)"), {"t": table}).scalar() is None:
            return {"table": table, "available": False, "reason": "table not present (owned by another module)"}
        cols = set(s.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = :t"),
            {"t": table}).scalars())
        tcol = next((c for c in _PROBES.get(table, ()) if c in cols), None)
        fails = [f'("{c}" IS NOT NULL AND CAST("{c}" AS text) <> \'\')' for c in _ERROR_COLS if c in cols]
        if "status" in cols:
            fails.append("lower(CAST(status AS text)) IN (" + ", ".join(f"'{v}'" for v in _FAILED_STATUSES) + ")")
        if "ok" in cols:
            fails.append("ok IS FALSE")
        if "consecutive_failures" in cols:
            fails.append("consecutive_failures > 0")
        window = f'"{tcol}" >= now() - interval \'24 hours\'' if tcol else "true"
        fail_sql = " OR ".join(fails) if fails else "false"
        r = s.execute(text(
            f'SELECT count(*) AS total, count(*) FILTER (WHERE {window}) AS rows_24h, '
            f'count(*) FILTER (WHERE {window} AND ({fail_sql})) AS failures_24h FROM "{table}"')).one()
        out = {"table": table, "available": True, "rows": r.total, "rows_24h": r.rows_24h if tcol else None,
               "failures_24h": r.failures_24h, "time_column": tcol,
               "failure_rule": fail_sql if fails else "no error/status/ok column to judge failures by"}
        err = next((c for c in _ERROR_COLS if c in cols), None)
        if err:
            order = f'ORDER BY "{tcol}" DESC NULLS LAST' if tcol else ""
            out["recent_errors"] = [
                str(e)[:300] for e in s.execute(text(
                    f'SELECT "{err}" FROM "{table}" WHERE "{err}" IS NOT NULL AND CAST("{err}" AS text) <> \'\' {order} LIMIT 5')).scalars()
            ]
        return out


def summary() -> dict:
    health = trust.all_provider_health()
    pending = quarantine.queue("pending", limit=1000)
    by_rule: dict[str, int] = {}
    for q in pending:
        by_rule[q["rule"]] = by_rule.get(q["rule"], 0) + 1
    open_inc = incidents.recent(status="open", limit=1000)
    open_by_kind: dict[str, int] = {}
    for i in open_inc:
        open_by_kind[i["kind"]] = open_by_kind.get(i["kind"], 0) + 1
    since7 = _now() - timedelta(days=7)
    schema = incidents.recent(kinds=["schema_change"], since=since7, limit=50)
    stale = stale_listings(include_aging=False)
    stale_by_provider: dict[str, int] = {}
    for x in stale:
        stale_by_provider[x["provider"]] = stale_by_provider.get(x["provider"], 0) + 1
    unmapped = normalize.unmapped_gpus()
    return {
        "providers": {
            "count": len(health),
            "by_status": {s: sum(1 for h in health if h["status"] == s) for s in ("healthy", "degraded", "down")},
            "items": [
                {k: h[k] for k in ("provider", "status", "status_reasons", "last_ok_fetch", "consecutive_failures",
                                   "failure_rate_24h", "latency_ms_p50_24h", "latency_ms_p95_24h",
                                   "listings_now", "listings_24h_ago", "quarantined", "schema_changes_7d")}
                for h in health
            ],
        },
        "jobs": jobs_health(),
        "provider_errors_24h": provider_errors(24)[:50],
        "schema_changes_7d": {"count": len(schema), "recent": schema[:10]},
        "unmapped_gpus": {"count": len(unmapped)},
        "stale_listings": {"count": len(stale), "by_provider": stale_by_provider},
        "duplicate_explosions": {"pending": by_rule.get("duplicate_explosion", 0),
                                 "collapses_open": open_by_kind.get("listing_collapse", 0)},
        "missing_regions": {"pending": by_rule.get("region_disappeared", 0)},
        "suspicious_price_moves": {
            "pending": sum(v for k, v in by_rule.items() if k.startswith("price_")),
            "by_rule": {k: v for k, v in by_rule.items() if k.startswith("price_")},
            "instance_price_mismatch_open": open_by_kind.get("instance_price_mismatch", 0),
        },
        "quarantine": {"pending": len(pending), "by_rule": by_rule},
        "incidents_open": open_by_kind,
        "routing": {t: probe(t) for t in ("routing_decisions", "deployments", "provision_attempts")},
        "news": {t: probe(t) for t in ("news_sources", "news_fetch_log")},
    }
