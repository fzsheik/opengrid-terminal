"""Rollup rules: results from several vantage points -> ok / regional / global. Pure functions, no I/O.

A probe's `location` is where it ran (a vantage point). A target's `region` is the provider region it serves. This
module only speaks about vantage points; the provider-region question is answered by comparing a provider's regional
targets in the same hour (see probes.store.dips).

A SLOT is one probe interval of one target (default 5 minutes). In a slot every reporting location either succeeded
or failed, and the slot is classified:

    ok         every reporting location succeeded
    regional   some locations failed and at least one succeeded: the target was reachable from somewhere
    global     every reporting location failed, and at least two reported: unreachable from every vantage point
    single     the only reporting location failed: one vantage point cannot tell global from local

`regional` names the failing locations. It means "failed from these vantage points", which can be the provider's
region or the network path to it; that distinction needs the provider's regional targets and its status page.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone

MIN_LOCATIONS = 2  # fewer reporting locations than this can never be called global


def slot_start(ts: datetime, interval_s: int) -> datetime:
    return datetime.fromtimestamp(int(ts.timestamp()) // interval_s * interval_s, tz=timezone.utc)


def classify_slot(results: dict[str, bool]) -> tuple[str, list[str]]:
    """results: {location: ok}. Returns (class, failing locations)."""
    bad = sorted(loc for loc, ok in results.items() if not ok)
    if not bad:
        return "ok", []
    if len(bad) < len(results):
        return "regional", bad
    return ("global" if len(results) >= MIN_LOCATIONS else "single"), bad


def percentile(values: list[int], q: float) -> int | None:
    v = sorted(x for x in values if x is not None)
    if not v:
        return None
    k = (len(v) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(v) - 1)
    return int(round(v[lo] + (v[hi] - v[lo]) * (k - lo)))


def rollup_hour(rows: list[dict], interval_s: int) -> dict[str, dict]:
    """rows: raw results for ONE target inside ONE hour. Returns {"target": {...}, "locations": {loc: {...}}}.

    target: slots by class, availability (ok checks / all checks across every location), failing locations,
            error classes, p50 of total_ms, locations that reported.
    locations: per location checks / ok / p50 / p95 / max / error classes.
    """
    by_slot: dict[datetime, dict[str, bool]] = defaultdict(dict)
    per_loc: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_slot[slot_start(r["observed_at"], interval_s)][r["location"]] = bool(r["ok"])
        per_loc[r["location"]].append(r)
    slots = Counter()
    failing: Counter = Counter()
    for res in by_slot.values():
        cls, bad = classify_slot(res)
        slots[cls] += 1
        failing.update(bad)
    errors = Counter(r["error_class"] for r in rows if not r["ok"] and r["error_class"])
    lat = [r["total_ms"] for r in rows if r["ok"] and r["total_ms"] is not None]
    checks = len(rows)
    target = {"checks": checks, "ok_checks": sum(1 for r in rows if r["ok"]),
              "availability": (sum(1 for r in rows if r["ok"]) / checks * 100) if checks else None,
              "slots_ok": slots["ok"], "slots_regional": slots["regional"], "slots_global": slots["global"],
              "slots_single": slots["single"], "failing_locations": dict(failing), "error_classes": dict(errors),
              "latency_p50_ms": percentile(lat, 0.5), "locations": sorted(per_loc), "slot_minutes": interval_s // 60}
    locs = {}
    for loc, rs in per_loc.items():
        ll = [r["total_ms"] for r in rs if r["ok"] and r["total_ms"] is not None]
        locs[loc] = {"checks": len(rs), "ok_checks": sum(1 for r in rs if r["ok"]), "latency_p50_ms": percentile(ll, 0.5),
                     "latency_p95_ms": percentile(ll, 0.95), "latency_max_ms": max(ll) if ll else None,
                     "error_classes": dict(Counter(r["error_class"] for r in rs if not r["ok"] and r["error_class"]))}
    return {"target": target, "locations": locs}


def hour_scope(t: dict) -> tuple[str | None, str]:
    """(scope, basis) of an hour's rollup; None when every slot was ok. The worst slot class wins."""
    if t["slots_global"]:
        return "global", f"every vantage point failed in {t['slots_global']} slot(s)"
    if t["slots_regional"]:
        locs = ", ".join(sorted(t["failing_locations"])) or "some locations"
        return "regional", f"failed from {locs} while other vantage points succeeded"
    if t["slots_single"]:
        return "unconfirmed", "only one vantage point reported, so global and local cannot be told apart"
    return None, "every check succeeded"
