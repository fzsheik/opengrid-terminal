"""Routing quality: what each route decided, what actually happened, and how good that was.

One route_outcomes row per route request (store/metrics.py), derived from the routing tables
(route_requests, routing_decisions, deployments, deployment_events, provision_attempts,
quotes, usage_records) by the `route_outcomes` job. Recompute is idempotent (upsert by
route_request_id) and incremental: rows marked `final` are not touched again unless
recompute(full=True). See methodology/routing-quality.md.

Definitions (all transaction data except the market median, which is observed):
    winner / runner-up      rank 1 / rank 2 candidates of the routing decision
    market median           the decision's market snapshot: median of each provider's lowest current
                            eligible (canonical, on-demand) price for that GPU, at decision time
    cheapest valid option   the lowest-priced candidate that passed every eligibility rule
    expected savings        (median - winner observed price) / median, at decision time
    realized savings        (median - execution price) / median, only for deployments that ran
    provisioned             the provider reported the instance running (a deployment_events row
                            to 'running'); an accepted launch alone is not "provisioned"
    provisioning latency    first provision attempt start -> first 'running' event
    quote error             (execution price - quote) / quote
A savings figure exists only with a valid comparison (methodology/economics.md): same canonical
GPU (not a family route), median from >= 3 providers. Otherwise comparison_reason says why.
"""

from __future__ import annotations

import logging
import statistics
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert

import normalize
import observability as obs
from config import settings
from jobs import job
from store.metrics import RouteOutcome

log = logging.getLogger(__name__)

MIN_MEDIAN_PROVIDERS = 3
TERMINAL = {"terminated", "provision_failed", "provider_rejected", "rejected", "quote_failed", "quote_expired",
            "failed"}
UNEXPECTED_END = {"provider_terminated", "provider_failed", "interrupted", "preempted"}


def _f(v):
    return None if v is None else float(v)


def _ts(v):
    if v is None or isinstance(v, datetime):
        return v
    t = datetime.fromisoformat(str(v))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _pct(a, b):
    """(a - b) / a as a percent; None if either is missing or a <= 0."""
    if a is None or b is None or float(a) <= 0:
        return None
    return round((float(a) - float(b)) / float(a) * 100, 4)


def comparison(gpu: str, decided_gpu: str | None, family: str | None, market: dict | None) -> tuple[bool, str | None]:
    """Is there a valid market-median comparison for this route? (valid, reason-if-not)."""
    m = market or {}
    if family:
        return False, "no valid comparison: family route (variants are different products; no single median)"
    if decided_gpu and gpu != decided_gpu:
        return False, f"no valid comparison: deployed GPU {gpu!r} differs from the requested {decided_gpu!r}"
    if m.get("median") is None:
        return False, "no valid comparison: no market median recorded at decision time"
    if (m.get("providers") or 0) < MIN_MEDIAN_PROVIDERS:
        return False, (f"no valid comparison: median from {m.get('providers') or 0} provider(s) at decision time "
                       f"(needs >= {MIN_MEDIAN_PROVIDERS})")
    if m.get("kind") not in (None, "observed_market_price"):
        return False, f"no valid comparison: median is {m.get('kind')}, not an observed on-demand market price"
    return True, None


def _primary(deps: list[dict], ran: set[str]) -> dict | None:
    """The deployment that represents the route: the one that ran, else the latest."""
    for d in deps:
        if d["deployment_id"] in ran:
            return d
    return deps[-1] if deps else None


def compute(s, rr: dict, *, has: dict) -> dict:
    """The route_outcomes row for one route_requests row (dict from row_to_json)."""
    rr_id = rr["id"]
    now = datetime.now(timezone.utc)
    dec = obs.rows(s, "routing_decisions", "route_request_id = :i", {"i": rr_id}, "created_at DESC, id DESC", 1)
    dec = dec[0] if dec else {}
    cands = sorted(dec.get("candidates") or [], key=lambda c: (c.get("rank") or 10 ** 6))
    market = dec.get("market_snapshot") or {}
    winner = cands[0] if cands else None
    runner = cands[1] if len(cands) > 1 else None
    priced = [c for c in cands if c.get("price_per_gpu_hour") is not None]
    cheapest = min(priced, key=lambda c: c["price_per_gpu_hour"]) if priced else None
    request = rr.get("request") or {}
    family = request.get("family")
    winner_price = _f(winner.get("price_per_gpu_hour")) if winner else _f(dec.get("selected_observed_price_per_gpu_hour"))

    deps = obs.rows(s, "deployments", "route_request_id = :i", {"i": rr_id}, "created_at")
    ids = [d["deployment_id"] for d in deps]
    events = obs.rows(s, "deployment_events", "deployment_id = ANY(:ids)", {"ids": ids}, "at, id", 5000) if ids else []
    attempts = obs.rows(s, "provision_attempts", "deployment_id = ANY(:ids)", {"ids": ids}, "started_at, id",
                        5000) if ids else []
    ran = {e["deployment_id"] for e in events if e.get("to_status") == "running"}
    d = _primary(deps, ran)

    out = {
        "route_request_id": rr_id, "account_id": rr.get("account_id"), "created_at": _ts(rr["created_at"]),
        "preview": bool(rr.get("preview")), "gpu": rr.get("gpu") or "", "strategy": rr.get("mode"),
        "request_status": rr.get("status"),
        "winner_provider": winner and winner.get("provider") or dec.get("selected_provider"),
        "winner_listing_id": winner and winner.get("listing_id") or dec.get("selected_listing_id"),
        "winner_price_per_gpu_hour": winner_price,
        "runner_up_provider": runner and runner.get("provider"),
        "runner_up_price_per_gpu_hour": runner and _f(runner.get("price_per_gpu_hour")),
        "candidates_total": len(cands),
        "candidates": [{k: c.get(k) for k in ("rank", "provider", "listing_id", "price_per_gpu_hour", "score")}
                       for c in cands[:10]],
        "market_median_per_gpu_hour": _f(market.get("median")), "market_providers": market.get("providers"),
        "cheapest_valid_provider": cheapest and cheapest.get("provider"),
        "cheapest_valid_price_per_gpu_hour": cheapest and _f(cheapest.get("price_per_gpu_hour")),
        "deployment_id": None, "deployment_provider": None, "deployment_purpose": None, "deployment_status": None, "launched": None,
        "provisioned": None, "provisioning_latency_ms": None, "quoted_price_per_gpu_hour": None,
        "actual_price_per_gpu_hour": None, "realized_savings_pct": None, "quote_error_pct": None,
        "interrupted": None, "interruptions": None, "uptime_seconds": None, "gpu_hours": None,
        "computed_at": now,
    }
    valid, reason = comparison(d["gpu"] if d else rr.get("gpu"), rr.get("gpu"), family, market)
    out["comparison_valid"], out["comparison_reason"] = valid, reason
    out["expected_savings_pct"] = _pct(market.get("median"), winner_price) if valid else None

    final = bool(rr.get("preview"))
    if d is not None:
        did = d["deployment_id"]
        mine_att = [a for a in attempts if a["deployment_id"] == did]
        mine_ev = [e for e in events if e["deployment_id"] == did]
        first_run = next((_ts(e["at"]) for e in mine_ev if e.get("to_status") == "running"), None)
        first_att = _ts(mine_att[0]["started_at"]) if mine_att else None
        quote = _f(d.get("quoted_price_per_gpu_hour"))
        if quote is None and d.get("quote_id") and has.get("quotes"):
            q = obs.rows(s, "quotes", "id = :q", {"q": d["quote_id"]}, None, 1)
            quote = _f(q[0].get("quote_price_per_gpu_hour")) if q else None
        actual = _f(d.get("actual_price_per_gpu_hour"))
        gpu_hours = None
        if has.get("usage_slices"):  # incremental metering: GPU-hours actually run (stopped time excluded)
            rs = s.execute(text("SELECT sum(running_seconds), count(*) FROM usage_slices WHERE deployment_id = :d"),
                           {"d": did}).first()
            if rs and rs[1]:
                gpu_hours = round(float(rs[0] or 0) * int(d.get("gpu_count") or 1) / 3600, 6)
        if gpu_hours is None and has.get("usage_records"):
            gh = s.execute(text("SELECT sum(gpu_hours) FROM usage_records WHERE deployment_id = :d"), {"d": did}).scalar()
            gpu_hours = _f(gh)
        uptime = int(d.get("uptime_seconds") or 0)
        if gpu_hours is None and uptime:
            gpu_hours = round(uptime * int(d.get("gpu_count") or 1) / 3600, 6)
        interruptions = int(d.get("interruptions") or 0)
        status = d.get("status")
        out.update({
            "deployment_id": did, "deployment_provider": d.get("provider"), "deployment_purpose": d.get("purpose") or "customer", "deployment_status": status,
            "launched": bool(mine_att), "provisioned": did in ran,
            "provisioning_latency_ms": int((first_run - first_att).total_seconds() * 1000)
            if first_run and first_att and first_run >= first_att else None,
            "quoted_price_per_gpu_hour": quote, "actual_price_per_gpu_hour": actual,
            "realized_savings_pct": _pct(market.get("median"), actual) if valid and did in ran else None,
            "quote_error_pct": None if quote in (None, 0) or actual is None
            else round((actual - quote) / quote * 100, 4),
            "interrupted": interruptions > 0 or (d.get("termination_reason") in UNEXPECTED_END),
            "interruptions": interruptions, "uptime_seconds": uptime, "gpu_hours": gpu_hours,
        })
        ended = _ts(d.get("terminated_at")) or _ts(d.get("created_at"))
        settled = d.get("reconciled_at") is not None or "reconciled_at" not in d
        final = status in TERMINAL and settled and ended is not None and now - ended > timedelta(days=1)
    elif not rr.get("preview"):
        final = now - _ts(rr["created_at"]) > timedelta(days=1)
    out["final"] = final
    return out


def _has(s) -> dict:
    return {t: obs.has_table(s, t) for t in ("quotes", "usage_records", "usage_slices", "route_outcomes",
                                             "partner_profiles")}


def recompute(full: bool = False, limit: int = 2000) -> dict:
    """Upsert route_outcomes for every route request that has no row or a non-final one."""
    done = 0
    with normalize.SessionLocal() as s:
        has = _has(s)
        if not has["route_outcomes"]:
            return {"computed": 0, "unavailable": "route_outcomes table missing (migration 0012 not applied)"}
        where = "true" if full else ("NOT EXISTS (SELECT 1 FROM route_outcomes o WHERE o.route_request_id = t.id "
                                     "AND o.final)")
        rrs = obs.rows(s, "route_requests", where, None, "created_at", limit)
        out_rows = []
        for rr in rrs:
            try:
                out_rows.append(compute(s, rr, has=has))
            except Exception:
                log.exception("route outcome for %s failed", rr.get("id"))
    if out_rows:
        with normalize.SessionLocal.begin() as s:
            for row in out_rows:
                stmt = insert(RouteOutcome).values(**row)
                s.execute(stmt.on_conflict_do_update(
                    index_elements=["route_request_id"],
                    set_={k: stmt.excluded[k] for k in row if k != "route_request_id"}))
                done += 1
    return {"computed": done, "scanned": len(rrs)}


@job("route_outcomes", every_seconds=settings.metrics_recompute_seconds, initial_delay_seconds=90)
def _recompute_job():
    return recompute()


# ---------------------------------------------------------------- reading

def _stat(values: list[float], *, unit: str) -> dict:
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return {"value": None, "n": 0, "unit": unit, "status": "unavailable: no data"}
    return {"mean": round(statistics.fmean(vals), 4), "median": round(statistics.median(vals), 4),
            "n": len(vals), "unit": unit, "status": "ok"}


def _rate(num: int, den: int) -> dict:
    if den == 0:
        return {"value": None, "n": 0, "status": "unavailable: no data"}
    return {"value": round(num / den, 4), "successes": num, "n": den, "status": "ok"}


def outcome_dict(r) -> dict:
    out = {c.name: getattr(r, c.name) for c in RouteOutcome.__table__.columns}
    for k, v in out.items():
        if isinstance(v, Decimal):
            out[k] = float(v)
        elif isinstance(v, datetime):
            out[k] = v.isoformat()
    return out


def outcomes(account_id: int | None = None, since: datetime | None = None, include_validation: bool = False,
             include_previews: bool = True, limit: int = 500) -> list[dict]:
    from sqlalchemy import select

    q = select(RouteOutcome).order_by(RouteOutcome.created_at.desc())
    if account_id is not None:
        q = q.where(RouteOutcome.account_id == account_id)
    if since is not None:
        q = q.where(RouteOutcome.created_at >= since)
    if not include_validation:
        q = q.where((RouteOutcome.deployment_purpose.is_(None)) | (RouteOutcome.deployment_purpose != "validation"))
    if not include_previews:
        q = q.where(RouteOutcome.preview.is_(False))
    with normalize.SessionLocal() as s:
        return [outcome_dict(r) for r in s.scalars(q.limit(limit))]


def summarize(rows: list[dict], partner_prices: dict[int, float] | None = None) -> dict:
    """Aggregates over outcome dicts (pure; tests feed crafted rows)."""
    partner_prices = partner_prices or {}
    routes = [r for r in rows if not r["preview"]]
    launched = [r for r in routes if r.get("launched")]
    ran = [r for r in launched if r.get("provisioned")]
    valid = [r for r in rows if r.get("comparison_valid")]
    by_provider: dict[str, dict] = {}
    for r in launched:
        p = by_provider.setdefault(r.get("deployment_provider") or r.get("winner_provider") or "?",
                                   {"launched": 0, "ran": 0})
        p["launched"] += 1
        p["ran"] += 1 if r.get("provisioned") else 0
    lat = [r["provisioning_latency_ms"] for r in ran if r.get("provisioning_latency_ms") is not None]
    qerr = [abs(r["quote_error_pct"]) for r in ran if r.get("quote_error_pct") is not None]
    tol = float(getattr(settings, "quote_price_tolerance", 0.02)) * 100
    prev = []
    for r in ran:
        normal = partner_prices.get(r.get("account_id"))
        if normal and r.get("actual_price_per_gpu_hour") is not None:
            prev.append((normal - r["actual_price_per_gpu_hour"]) / normal * 100)
    no_cmp: dict[str, int] = {}
    for r in rows:
        if not r.get("comparison_valid") and r.get("comparison_reason"):
            key = r["comparison_reason"].split(":", 1)[-1].strip().split(" (")[0]
            no_cmp[key] = no_cmp.get(key, 0) + 1
    return {
        "route_requests": len(rows), "previews": len(rows) - len(routes), "routes": len(routes),
        "routing_success_rate": {**_rate(len([r for r in routes if r.get("provisioned")]), len(routes)),
                                 "definition": "routes whose deployment the provider reported running / all "
                                               "non-preview route requests"},
        "provisioning_success_rate": {**_rate(len(ran), len(launched)),
                                      "definition": "deployments reported running / deployments with a provision call"},
        "expected_savings_vs_median_pct": {**_stat([r["expected_savings_pct"] for r in valid], unit="%"),
                                           "basis": "winner observed price vs market median at decision time"},
        "realized_savings_vs_median_pct": {**_stat([r["realized_savings_pct"] for r in ran if r.get("comparison_valid")],
                                                   unit="%"),
                                           "basis": "execution price vs market median at decision time"},
        "savings_vs_previous_provider_pct": {**_stat(prev, unit="%"),
                                             "basis": "design-partner profile's normal price vs execution price; "
                                                      "only where the partner gave a normal price"},
        "provider_failure_rate": {p: {**_rate(v["launched"] - v["ran"], v["launched"]),
                                      "meaning": "share of launches that never reported running"}
                                  for p, v in sorted(by_provider.items())},
        "provisioning_latency_ms": {**_stat(lat, unit="ms"), "p50": obs.percentile(lat, 0.5),
                                    "p95": obs.percentile(lat, 0.95)},
        "quote_accuracy": {"mean_abs_error_pct": _stat(qerr, unit="%"),
                           "within_tolerance": _rate(len([e for e in qerr if e <= tol]), len(qerr)),
                           "tolerance_pct": tol},
        "no_valid_comparison": no_cmp,
    }


def partner_prices() -> dict[int, float]:
    with normalize.SessionLocal() as s:
        if not obs.has_table(s, "partner_profiles"):
            return {}
        return {a: float(p) for a, p in s.execute(text(
            "SELECT account_id, normal_price_per_gpu_hour FROM partner_profiles "
            "WHERE normal_price_per_gpu_hour IS NOT NULL"))}


def summary(account_id: int | None = None, since: datetime | None = None, include_validation: bool = False) -> dict:
    rows = outcomes(account_id, since, include_validation, limit=100_000)
    return summarize(rows, partner_prices())


# ---------------------------------------------------------------- economic value (methodology/economics.md)

def economics_from(rows: list[dict]) -> dict:
    """Per-deployment savings vs the market median and totals (pure; rows are outcome dicts).

    savings $ = (median - selected price) x GPU-hours actually run, only with a valid comparison.
    Selected price = execution price, else the quote (labelled). Negative savings are kept.
    """
    deployments, excluded = [], {}
    for r in rows:
        if r.get("preview") or not r.get("deployment_id") or r.get("deployment_purpose") == "validation":
            continue
        price, basis = r.get("actual_price_per_gpu_hour"), "execution_price"
        if price is None and r.get("quoted_price_per_gpu_hour") is not None:
            price, basis = r["quoted_price_per_gpu_hour"], "quote"
        median = r.get("market_median_per_gpu_hour")
        e = {"deployment_id": r["deployment_id"], "route_request_id": r["route_request_id"], "gpu": r["gpu"],
             "provider": r.get("deployment_provider") or r.get("winner_provider"), "strategy": r.get("strategy"),
             "account_id": r.get("account_id"), "market_median_per_gpu_hour": median,
             "market_median_kind": "observed_market_price", "market_providers": r.get("market_providers"),
             "selected_price_per_gpu_hour": price, "selected_price_basis": basis if price is not None else None,
             "gpu_hours": r.get("gpu_hours"), "savings_pct": None, "savings_usd": None}
        reason = None
        if not r.get("comparison_valid"):
            reason = r.get("comparison_reason") or "no valid comparison"
        elif price is None:
            reason = "no valid comparison: no execution price or quote recorded"
        elif not r.get("provisioned"):
            reason = "no valid comparison: the deployment never ran"
        if reason is None:
            e["savings_pct"] = _pct(median, price)
            if r.get("gpu_hours"):
                e["savings_usd"] = round((float(median) - float(price)) * float(r["gpu_hours"]), 4)
            else:
                e["note"] = "no GPU-hours recorded yet: savings $ pending"
        else:
            e["comparison"] = reason
            key = reason.split(":", 1)[-1].strip().split(" (")[0]
            excluded[key] = excluded.get(key, 0) + 1
        deployments.append(e)

    valid = [e for e in deployments if e["savings_pct"] is not None]

    def group(field):
        g: dict[str, dict] = {}
        for e in valid:
            x = g.setdefault(e.get(field) or "?", {"n": 0, "total_savings_usd": 0.0, "_pcts": []})
            x["n"] += 1
            x["total_savings_usd"] += e["savings_usd"] or 0.0
            x["_pcts"].append(e["savings_pct"])
        return {k: {"n": v["n"], "total_savings_usd": round(v["total_savings_usd"], 2),
                    "mean_savings_pct": round(statistics.fmean(v["_pcts"]), 4),
                    "median_savings_pct": round(statistics.median(v["_pcts"]), 4)} for k, v in sorted(g.items())}

    pcts = [e["savings_pct"] for e in valid]
    usd = [e["savings_usd"] for e in valid if e["savings_usd"] is not None]
    return {
        "kind": "transaction",
        "totals": {
            "deployments": len(deployments), "with_valid_comparison": len(valid),
            "total_customer_savings_usd": round(sum(usd), 2) if usd else None,
            "mean_savings_pct": round(statistics.fmean(pcts), 4) if pcts else None,
            "median_savings_pct": round(statistics.median(pcts), 4) if pcts else None,
            "status": "ok" if valid else "unavailable: no deployment has a valid comparison yet",
        },
        "by_gpu": group("gpu"), "by_provider": group("provider"), "by_strategy": group("strategy"),
        "excluded_no_valid_comparison": excluded,
        "deployments": deployments,
    }


def economics(account_id: int | None = None, since: datetime | None = None) -> dict:
    rows = outcomes(account_id, since, include_validation=False, include_previews=False, limit=100_000)
    out = economics_from(rows)
    out["computed_at"] = max((r["computed_at"] for r in rows), default=None)
    return out
