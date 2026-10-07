"""Provider reliability from REAL OpenGrid transactions only. Kind: transaction.

Inputs are deployments OpenGrid actually launched, their provision_attempts and
deployment_events. Nothing here reads market data, provider marketing, or synthetic rows
(syn_* providers are tests only and never exist in production).

Per provider, each metric carries its sample size n:
    launches                 deployments with at least one provision call
    successful_launches      ... that the provider reported running
    failed_launches          by kind: rejected (provider definitively refused: provision_failed /
                             provider_rejected / a capacity|auth|validation error), timeout
                             (provider_timeout / a timed-out call), unknown (launch_unknown,
                             orphan_suspected, an ambiguous error); a launch that later ran is
                             not a failure even if an earlier status read failed
    provisioning_latency     first provision call -> first 'running' event, p50 / p95
    time_to_capacity         approval (else creation) -> first 'running' event, p50 / p95
    unexpected_terminations  ran, then ended without OpenGrid asking (interruption, provider
                             terminated / failed)
    api_error_rate           failed provision calls / provision calls (attempt rows); the in-process
                             provider-call counters are shown separately (since last restart)
    termination_success      termination requested -> terminated confirmed (vs termination_failed)
    quote_accuracy           |execution price - quote| / quote, mean and share within tolerance
A score exists only when launches n >= settings.reliability_min_samples (default 10); below
that the answer is "insufficient sample (n=...)". One failure is never a verdict.
Validation deployments (purpose='validation') are excluded from customer-facing numbers;
the admin view shows them separately. See methodology/reliability.md.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timezone

from sqlalchemy import text

import normalize
import observability as obs
from config import settings

REJECTED_STATES = {"provision_failed", "provider_rejected"}
TIMEOUT_STATES = {"provider_timeout"}
UNKNOWN_STATES = {"launch_unknown", "orphan_suspected"}
REJECTED_KINDS = {"capacity", "invalid", "auth", "quota", "rejected", "validation", "not_found", "unavailable",
                  "insufficient_capacity", "bad_request", "unauthorized", "forbidden"}
TIMEOUT_KINDS = {"timeout", "provider_timeout"}
UNEXPECTED_END = {"provider_terminated", "provider_failed", "interrupted", "preempted"}
RAN_STATES = {"running", "stopping", "stopped"}


def _ts(v):
    if v is None or isinstance(v, datetime):
        return v
    t = datetime.fromisoformat(str(v))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def min_samples() -> int:
    return max(1, int(settings.reliability_min_samples))


def gated(value, n: int, *, unit: str | None = None, **extra) -> dict:
    """A metric with its n; withheld (value None) below the minimum sample."""
    m = min_samples()
    out = {"n": n, **({"unit": unit} if unit else {}), **extra}
    if n == 0:
        return {**out, "value": None, "status": "no data (n=0)"}
    if n < m:
        return {**out, "value": None, "status": f"insufficient sample (n={n}, need {m})"}
    return {**out, "value": value, "status": "ok"}


def _first(events, state):
    return next((_ts(e["at"]) for e in events if e.get("to_status") == state), None)


def classify(dep: dict) -> str | None:
    """'success' | 'rejected' | 'timeout' | 'unknown' | None (never launched / still in flight)."""
    events = dep.get("events") or []
    attempts = dep.get("attempts") or []
    if any(e.get("to_status") == "running" for e in events):
        return "success"
    states = {dep.get("status")} | {e.get("to_status") for e in events}
    if states & UNKNOWN_STATES:
        return "unknown"
    if states & TIMEOUT_STATES:
        return "timeout"
    if states & REJECTED_STATES:
        return "rejected"
    if not attempts:
        return None
    kinds = {(a.get("error_kind") or "").lower() for a in attempts if not a.get("ok")}
    if dep.get("status") in ("provisioning", "approved", "created", "quoted", "pending_approval", "routing"):
        return None  # in flight: no verdict yet
    if kinds & TIMEOUT_KINDS:
        return "timeout"
    if kinds and kinds <= REJECTED_KINDS:
        return "rejected"
    if kinds or dep.get("status") == "failed":
        return "unknown"
    return None


def compute(deps: list[dict]) -> dict:
    """Per-provider reliability over deployment dicts (pure; see load() for the shape)."""
    by: dict[str, list[dict]] = {}
    for d in deps:
        if d.get("provider"):
            by.setdefault(d["provider"], []).append(d)
    tol = float(getattr(settings, "quote_price_tolerance", 0.02)) * 100
    out = {}
    for p, ds in sorted(by.items()):
        kinds = {"success": 0, "rejected": 0, "timeout": 0, "unknown": 0}
        prov_lat, cap_lat, qerr = [], [], []
        ran = unexpected = term_req = term_ok = term_fail = calls = call_err = 0
        for d in ds:
            ev = sorted(d.get("events") or [], key=lambda e: _ts(e["at"]))
            att = sorted(d.get("attempts") or [], key=lambda a: _ts(a["started_at"]))
            calls += len(att)
            call_err += sum(1 for a in att if not a.get("ok"))
            k = classify({**d, "events": ev, "attempts": att})
            if k:
                kinds[k] += 1
            run_at = _first(ev, "running")
            if run_at:
                ran += 1
                if att:
                    prov_lat.append((run_at - _ts(att[0]["started_at"])).total_seconds() * 1000)
                start = _ts(d.get("approved_at")) or _ts(d.get("created_at"))
                if start:
                    cap_lat.append((run_at - start).total_seconds())
                ended_unasked = (d.get("termination_reason") in UNEXPECTED_END) or (d.get("interruptions") or 0) > 0
                unexpected += 1 if ended_unasked else 0
                q, a = d.get("quoted_price_per_gpu_hour"), d.get("actual_price_per_gpu_hour")
                if q and a is not None:
                    qerr.append(abs(float(a) - float(q)) / float(q) * 100)
            states = [e.get("to_status") for e in ev]
            asked = d.get("termination_reason") not in (None, *UNEXPECTED_END)
            if "terminating" in states or "termination_failed" in states or ("terminated" in states and asked):
                term_req += 1
                if "terminated" in states or d.get("status") == "terminated":
                    term_ok += 1
                elif "termination_failed" in states:
                    term_fail += 1
        launches = sum(kinds.values())
        m = {
            "launches": launches,
            "successful_launches": gated(round(kinds["success"] / launches, 4) if launches else None, launches,
                                         unit="share", count=kinds["success"]),
            "failed_launches": {"rejected": kinds["rejected"], "timeout": kinds["timeout"], "unknown": kinds["unknown"],
                                "n": launches,
                                "note": "counts, not a verdict; rejected = provider definitively created nothing"},
            "provisioning_latency_ms": gated({"p50": obs.percentile(prov_lat, 0.5), "p95": obs.percentile(prov_lat, 0.95)},
                                             len(prov_lat), unit="ms"),
            "time_to_capacity_seconds": gated({"p50": obs.percentile(cap_lat, 0.5), "p95": obs.percentile(cap_lat, 0.95)},
                                              len(cap_lat), unit="s"),
            "unexpected_terminations": gated(round(unexpected / ran, 4) if ran else None, ran, unit="share of runs",
                                             count=unexpected),
            "api_error_rate": gated(round(call_err / calls, 4) if calls else None, calls, unit="share of provision calls",
                                    count=call_err),
            "termination_success": gated(round(term_ok / term_req, 4) if term_req else None, term_req, unit="share",
                                         count=term_ok, failed=term_fail),
            "quote_accuracy": gated({"mean_abs_error_pct": round(statistics.fmean(qerr), 4) if qerr else None,
                                     "within_tolerance": round(sum(1 for e in qerr if e <= tol) / len(qerr), 4)
                                     if qerr else None, "tolerance_pct": tol}, len(qerr)),
        }
        m["score"] = score(m)
        out[p] = m
    return out


def score(m: dict) -> dict:
    """0-100 = 100 x launch success x (1 - unexpected termination share) x termination success,
    each factor used only when its own n clears the minimum. No score below the minimum launches."""
    n = m["launches"]
    if n < min_samples():
        return {"value": None, "n": n, "status": f"insufficient sample (n={n}, need {min_samples()})"}
    s = m["successful_launches"]["value"]
    used, skipped = ["successful_launches"], []
    if m["unexpected_terminations"]["value"] is not None:
        s *= 1 - m["unexpected_terminations"]["value"]
        used.append("unexpected_terminations")
    else:
        skipped.append("unexpected_terminations")
    if m["termination_success"]["value"] is not None:
        s *= m["termination_success"]["value"]
        used.append("termination_success")
    else:
        skipped.append("termination_success")
    return {"value": round(100 * s, 1), "n": n, "status": "ok", "factors_used": used,
            "factors_skipped_insufficient_sample": skipped}


def load(include_validation: bool = False, only_validation: bool = False, provider: str | None = None) -> list[dict]:
    """Deployments with their attempts and events, from the routing tables."""
    with normalize.SessionLocal() as s:
        if not obs.has_table(s, "deployments"):
            return []
        cols = obs.columns(s, "deployments")
        where, params = ["provider IS NOT NULL"], {}
        if "purpose" in cols:
            if only_validation:
                where.append("purpose = 'validation'")
            elif not include_validation:
                where.append("coalesce(purpose, 'customer') <> 'validation'")
        elif only_validation:
            return []
        if provider:
            where.append("provider = :p")
            params["p"] = provider
        deps = obs.rows(s, "deployments", " AND ".join(where), params, "created_at", 200_000)
        ids = [d["deployment_id"] for d in deps]
        ev, att = {}, {}
        if ids:
            for e in s.execute(text("SELECT deployment_id, at, to_status FROM deployment_events "
                                    "WHERE deployment_id = ANY(:ids) ORDER BY at, id"), {"ids": ids}):
                ev.setdefault(e.deployment_id, []).append({"at": e.at, "to_status": e.to_status})
            for a in s.execute(text("SELECT deployment_id, provider, started_at, ok, error_kind, latency_ms "
                                    "FROM provision_attempts WHERE deployment_id = ANY(:ids) ORDER BY started_at, id"),
                               {"ids": ids}):
                att.setdefault(a.deployment_id, []).append(
                    {"provider": a.provider, "started_at": a.started_at, "ok": a.ok, "error_kind": a.error_kind,
                     "latency_ms": a.latency_ms})
    for d in deps:
        d["events"] = ev.get(d["deployment_id"], [])
        d["attempts"] = att.get(d["deployment_id"], [])
    return deps


def in_process_calls() -> dict:
    """Provider-call counters from the structured provider log (since the last restart)."""
    out: dict[str, dict] = {}
    ok_words = {"ok", "accepted", "running", "pending", "stopped", "terminated", "terminating", "already_gone"}
    for c in obs.metrics.snapshot()["counters"]:
        if c["name"] in ("provider_calls", "provider_ops"):
            group = "http" if c["name"] == "provider_calls" else "ops"
            p = out.setdefault(c["labels"].get("provider", "?"), {}).setdefault(
                group, {"calls": 0, "errors": 0, "by_outcome": {}})
            p["calls"] += c["value"]
            oc = c["labels"].get("outcome", "?")
            p["by_outcome"][oc] = p["by_outcome"].get(oc, 0) + c["value"]
            if oc not in ok_words:
                p["errors"] += c["value"]
    return {"by_provider": out, "note": "http = HTTP requests to provider APIs; ops = execution verbs (provision, "
                                        "status, terminate...) as the core made them; since the last restart"}


def report(include_validation: bool = False, provider: str | None = None) -> dict:
    return {"kind": "transaction", "min_samples": min_samples(),
            "providers": compute(load(include_validation=include_validation, provider=provider)),
            "validation_included": include_validation,
            "note": "from OpenGrid's own launches only; a provider with few launches has no score"}


def admin_report(provider: str | None = None) -> dict:
    return {"kind": "transaction", "min_samples": min_samples(),
            "customer": compute(load(provider=provider)),
            "validation": compute(load(only_validation=True, provider=provider)),
            "provider_api_calls_in_process": in_process_calls()}
