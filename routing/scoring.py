"""Best execution: a transparent weighted ranking of where a workload should run. Not "cheapest wins".

    rank_listings(gpu, *, count=1, region_group=None, max_price=None, mode='BALANCED', weights=None)

Candidates are current compute_listings rows that pass market.py's eligibility (canonical,
on-demand, not interruptible, not Vast's cheapest row, not a "from" floor), are priced, not
sold out, seen within stale_after(provider), and whose instance has exactly `count` GPUs.
`count` is GPUs PER INSTANCE: a route launches one instance. Shapes that would need several
instances (gpu_count divides count) are scored and reported separately as multi-instance
alternatives, never auto-provisioned. Everything else is listed in `exclusions` with a reason.

Each factor is normalized to 0..1 and reported with its raw value and data kind:
    price                     observed   0.5 at the market median, +/-1 per 100% below/above, clamped
    availability_now          observed   explicit available 1.0, unknown 0.5 (sold out is excluded)
    region_match              observed   in the wanted group 1.0, location unknown 0.5 (only when asked)
    freshness                 observed   1.0 within one polling interval, falling to 0 at stale_after
    availability_persistence  inferred   share of hours (30d) with priced availability, market_hourly
    price_stability           inferred   1 - CV(30d hourly lowest price)/0.25, clamped
    integration_level         registry   OpenGrid adapter level / 3 (can OpenGrid provision it?)
    reliability, performance  no data -- not used (weight 0, value null) until transaction records exist

A factor with weight but no value (thin history) scores a neutral 0.5 and says so ("imputed").
See methodology/best-execution.md.
"""

from __future__ import annotations

import math
import statistics
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

import market
import normalize
import provider_meta
from cache import ttl_cache
from providers import PROVIDERS
from routing import adapters

METHODOLOGY_VERSION = "routing-1.0"
MODES = ("CHEAPEST", "FASTEST_AVAILABLE", "BALANCED", "MOST_STABLE", "USER_DEFINED")
FACTORS = ("price", "availability_now", "region_match", "freshness", "availability_persistence",
           "price_stability", "integration_level", "reliability", "performance")
NO_DATA = ("reliability", "performance")
LABEL = {
    "price": "price", "availability_now": "availability now", "region_match": "region match",
    "freshness": "data freshness", "availability_persistence": "availability history",
    "price_stability": "price stability", "integration_level": "OpenGrid integration",
    "reliability": "reliability", "performance": "performance",
}
MODE_WEIGHTS = {
    "CHEAPEST": {"price": 1.0},
    "FASTEST_AVAILABLE": {"availability_now": 0.35, "integration_level": 0.30, "freshness": 0.20,
                          "availability_persistence": 0.10, "price": 0.05},
    "BALANCED": {"price": 0.40, "availability_now": 0.20, "price_stability": 0.15,
                 "availability_persistence": 0.15, "freshness": 0.05, "integration_level": 0.05},
    "MOST_STABLE": {"availability_persistence": 0.45, "price_stability": 0.25, "availability_now": 0.15,
                    "price": 0.10, "freshness": 0.05},
}
REGION_WEIGHT = 0.10
MIN_HISTORY_HOURS = 24
CV_ZERO = 0.25          # a 25% coefficient of variation scores 0 stability
MAX_EXCLUSIONS = 200


class ScoringError(ValueError):
    """Bad mode or weights: the API turns it into a 422."""


# --------------------------------------------------------------------------
# Weights
# --------------------------------------------------------------------------

def validate_weights(weights: dict) -> dict:
    """User weights -> normalized weights over FACTORS. Raises ScoringError with the reason."""
    if not isinstance(weights, dict) or not weights:
        raise ScoringError("USER_DEFINED mode needs a non-empty weights object")
    out = {}
    for k, v in weights.items():
        if k not in FACTORS:
            raise ScoringError(f"unknown factor {k!r}; factors are {', '.join(FACTORS)}")
        try:
            v = float(v)
        except (TypeError, ValueError):
            raise ScoringError(f"weight for {k!r} must be a number")
        if not math.isfinite(v) or v < 0:
            raise ScoringError(f"weight for {k!r} must be a finite number >= 0")
        if k in NO_DATA and v > 0:
            raise ScoringError(f"{k!r} has no data yet (no execution history) and cannot be weighted")
        out[k] = v
    total = sum(out.values())
    if total <= 0:
        raise ScoringError("weights must not all be zero")
    return {k: v / total for k, v in out.items() if v > 0}


def effective_weights(mode: str, weights: dict | None, region_requested: bool) -> dict:
    """Normalized weight per factor (0 for unused) for a mode."""
    mode = (mode or "BALANCED").upper()
    if mode not in MODES:
        raise ScoringError(f"unknown mode {mode!r}; modes are {', '.join(MODES)}")
    if mode == "USER_DEFINED":
        base = validate_weights(weights or {})
    elif weights:
        raise ScoringError("weights are only accepted with mode USER_DEFINED")
    else:
        base = dict(MODE_WEIGHTS[mode])
        if region_requested and mode != "CHEAPEST":
            base["region_match"] = REGION_WEIGHT
    if not region_requested:
        base.pop("region_match", None)  # nothing to match
    total = sum(base.values())
    if total <= 0:
        raise ScoringError("no usable weights remain (region_match needs a region)")
    return {f: round(base.get(f, 0.0) / total, 6) for f in FACTORS}


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------

_LISTINGS = text(f"""
    SELECT c.provider, c.listing_id, c.sku, c.raw_gpu_name, c.canonical_gpu_name AS gpu, c.gpu_count,
           c.region, c.country, c.price_per_gpu_hour, c.price_per_instance_hour, c.market_type,
           c.provider_tier, c.interruptible, c.available, c.capacity, c.capacity_unit,
           c.observed_at, c.first_seen_at, ({market._ELIGIBLE}) AS eligible
    FROM compute_listings c
    WHERE c.canonical_gpu_name = :gpu
""")

_HISTORY = text("""
    WITH h AS (
      SELECT provider, hour, min(min_price) AS p, sum(priced_listings) AS priced
      FROM market_hourly
      WHERE segment = 'on_demand' AND gpu = :gpu AND hour >= :t30
      GROUP BY provider, hour
    )
    SELECT provider,
           count(*) FILTER (WHERE priced > 0) AS priced_30,
           count(*) FILTER (WHERE priced > 0 AND hour >= :t7) AS priced_7,
           avg(p) AS mean_p, stddev_pop(p) AS sd_p, count(p) AS n_p
    FROM h GROUP BY provider
""")


def _hours(a: datetime, b: datetime) -> int:
    """Top-of-hour samples in [a, b]."""
    if b < a:
        return 0
    return int((b - a).total_seconds() // 3600) + 1


@ttl_cache(300)
def history(gpu: str) -> dict:
    """{provider: persistence and stability stats over the last 7/30 days} from market_hourly.

    The denominator is every hour from when the provider was first recorded for this GPU
    (or the window start, if later) to the newest rollup hour: an hour with no rollup row
    means the provider had no live listing, which counts as not available.
    """
    with normalize.SessionLocal() as s:
        last = s.execute(text("SELECT max(hour) FROM market_hourly WHERE segment = 'on_demand'")).scalar()
        if last is None:
            return {"last_hour": None, "providers": {}}
        t30, t7 = last - timedelta(days=30) + timedelta(hours=1), last - timedelta(days=7) + timedelta(hours=1)
        rows = s.execute(_HISTORY, {"gpu": gpu, "t30": t30, "t7": t7}).all()
        firsts = dict(s.execute(text(
            "SELECT provider, min(hour) FROM market_hourly WHERE segment = 'on_demand' AND gpu = :gpu GROUP BY provider"
        ), {"gpu": gpu}).all())
    out = {}
    for r in rows:
        first = firsts.get(r.provider) or last
        d30, d7 = _hours(max(first, t30), last), _hours(max(first, t7), last)
        mean = None if r.mean_p is None else float(r.mean_p)
        sd = None if r.sd_p is None else float(r.sd_p)
        out[r.provider] = {
            "hours_30d": d30, "hours_7d": d7,
            "share_30d": r.priced_30 / d30 if d30 >= MIN_HISTORY_HOURS else None,
            "share_7d": r.priced_7 / d7 if d7 >= MIN_HISTORY_HOURS else None,
            "priced_hours_30d": int(r.priced_30),
            "cv_30d": (sd / mean) if (mean and sd is not None and r.n_p >= MIN_HISTORY_HOURS) else None,
            "mean_30d": mean, "priced_samples_30d": int(r.n_p),
            "first_recorded": first.isoformat(),
        }
    return {"last_hour": last.isoformat(), "providers": out}


def _region_groups(provider, region, country):
    """(set of groups, module available?)."""
    try:
        import regions
    except ImportError:
        return set(), False
    return regions.region_groups(provider, region, country), True


def _availability_basis(row: dict) -> str | None:
    try:
        from quality.trust import listing_trust
    except ImportError:
        return None
    try:
        return listing_trust(row).get("availability_basis")
    except Exception:  # a quality-module bug must not break routing
        return None


def _interval(provider: str) -> float:
    cls = PROVIDERS.get(provider)
    return cls.polling.interval_seconds if cls else 900


def _age_text(seconds: float) -> str:
    if seconds < 90:
        return f"{int(seconds)} s"
    if seconds < 5400:
        return f"{round(seconds / 60)} min"
    return f"{seconds / 3600:.1f} h"


def _source_text(provider: str) -> str:
    st = provider_meta.meta(provider).source_type
    return {"authenticated_api": "API data", "public_api": "API data", "public_page": "price-page data",
            "aggregator": "aggregator (Shadeform) data", "public_pricing_file": "price-file data"}.get(st, "data")


def _f(v):
    return None if v is None else float(v)


def _iso(t):
    return None if t is None else t.isoformat()


def clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


# --------------------------------------------------------------------------
# Factors
# --------------------------------------------------------------------------

def _factor(value, raw, kind, note, weight=0.0):
    return {"value": None if value is None else round(value, 4), "raw": raw, "kind": kind, "note": note,
            "weight": weight, "contribution": None, "imputed": False}


def factors(row: dict, *, median: float, hist: dict, want_group: str | None, now: datetime) -> dict:
    p = float(row["price_per_gpu_hour"])
    pct = (p - median) / median if median else 0.0
    if abs(pct) < 0.005:
        price_note = f"at the current market median (${median:.2f})"
    else:
        price_note = f"{abs(pct):.0%} {'below' if pct < 0 else 'above'} current market median (${median:.2f})"
    out = {"price": _factor(clamp(0.5 - pct), {"price_per_gpu_hour": p, "market_median": median,
                                               "pct_vs_median": round(pct, 4)}, "observed", price_note)}

    basis = _availability_basis(row)
    if row["available"] is True:
        word = "inferred" if basis == "inferred" else "explicit"
        out["availability_now"] = _factor(1.0, {"available": True, "basis": basis or word}, "observed",
                                          f"available now ({word})")
    else:
        out["availability_now"] = _factor(0.5, {"available": None, "basis": basis or "unknown"}, "observed",
                                          "availability unknown (provider does not report stock)")

    if want_group:
        groups, ok = _region_groups(row["provider"], row["region"], row["country"])
        if not ok:
            out["region_match"] = _factor(None, {"region": row["region"]}, "observed", "region data unavailable")
        elif want_group in groups:
            out["region_match"] = _factor(1.0, {"region": row["region"], "groups": sorted(groups)}, "observed",
                                          f"region matched ({want_group})")
        else:
            out["region_match"] = _factor(0.5, {"region": row["region"], "groups": []}, "observed",
                                          f"location unknown: not confirmed to be in {want_group}")
    else:
        out["region_match"] = _factor(None, {"region": row["region"]}, "observed", "no region requested; not used")

    age = (now - row["observed_at"]).total_seconds()
    interval = _interval(row["provider"])
    stale = market.stale_after(row["provider"]).total_seconds()
    fresh = clamp(1 - max(0.0, age - interval) / max(1.0, stale - interval))
    word = "fresh" if age <= interval else "aging"
    out["freshness"] = _factor(fresh, {"age_seconds": round(age), "polling_interval_seconds": interval,
                                       "stale_after_seconds": stale}, "observed",
                               f"{word} {_source_text(row['provider'])} ({_age_text(age)} old)")

    h = hist.get(row["provider"])
    if h and h["share_30d"] is not None:
        out["availability_persistence"] = _factor(
            h["share_30d"], {k: h[k] for k in ("share_30d", "share_7d", "hours_30d", "priced_hours_30d")},
            "inferred", f"priced availability {h['share_30d']:.0%} of last 30 days")
    else:
        hrs = h["hours_30d"] if h else 0
        out["availability_persistence"] = _factor(None, {"hours_30d": hrs}, "inferred",
                                                  f"availability history insufficient ({hrs} h recorded)")
    if h and h["cv_30d"] is not None:
        cv = h["cv_30d"]
        out["price_stability"] = _factor(clamp(1 - cv / CV_ZERO), {"cv_30d": round(cv, 4), "mean_30d": h["mean_30d"]},
                                         "inferred", f"price {'stable' if cv < 0.05 else 'varied'} "
                                                     f"(±{cv:.0%} over 30 days)")
    else:
        out["price_stability"] = _factor(None, {"priced_samples_30d": h["priced_samples_30d"] if h else 0},
                                         "inferred", "price history insufficient")

    lvl = adapters.level(row["provider"])
    out["integration_level"] = _factor(lvl / 3, {"level": lvl}, "registry",
                                       f"OpenGrid can provision via API (level {lvl})" if lvl >= 2
                                       else "market data only: OpenGrid cannot provision here")
    for f in NO_DATA:
        out[f] = _factor(None, None, None, "no data: not used")
    return out


def score(fs: dict, weights: dict) -> float:
    total = 0.0
    for name, f in fs.items():
        w = weights.get(name, 0.0)
        f["weight"] = w
        if w <= 0:
            f["contribution"] = 0.0
            continue
        v = f["value"]
        if v is None:
            v, f["imputed"] = 0.5, True
            f["note"] += " (scored neutral 0.5)"
        f["contribution"] = round(w * v, 4)
        total += w * v
    return round(total, 4)


def explanation(c: dict) -> str:
    fs = c["factors"]
    parts = [fs["price"]["note"], fs["availability_now"]["note"]]
    if fs["region_match"]["value"] is not None:
        parts.append(fs["region_match"]["note"])
    parts += [fs["freshness"]["note"], fs["availability_persistence"]["note"]]
    if fs["price_stability"]["value"] is not None:
        parts.append(fs["price_stability"]["note"])
    parts.append(fs["integration_level"]["note"])
    return "; ".join(parts)


def versus(alt: dict, top: dict) -> str:
    """Why this alternative ranks where it does relative to the selected candidate."""
    pa, pt = alt["price_per_gpu_hour"], top["price_per_gpu_hour"]
    pct = (pa - pt) / pt if pt else 0.0
    price = ("same price" if abs(pct) < 0.005 else
             f"+{pct:.0%} more expensive" if pct > 0 else f"{abs(pct):.0%} cheaper")
    better, worse = [], []
    for name in ("availability_now", "availability_persistence", "price_stability", "freshness",
                 "integration_level", "region_match"):
        a, t = alt["factors"][name]["value"], top["factors"][name]["value"]
        if a is None or t is None:
            continue
        if a - t >= 0.1:
            better.append(LABEL[name])
        elif t - a >= 0.1:
            worse.append(LABEL[name])
    s = price
    if better:
        s += (" but stronger " if pct > 0 else " and stronger ") + ", ".join(better)
    if worse:
        s += (" but weaker " if pct <= 0 and not better else "; weaker ") + ", ".join(worse)
    return s


# --------------------------------------------------------------------------
# Ranking
# --------------------------------------------------------------------------

def _exclusion(row, code, reason):
    return {"provider": row["provider"], "listing_id": row["listing_id"], "gpu_count": row["gpu_count"],
            "region": row["region"], "price_per_gpu_hour": _f(row["price_per_gpu_hour"]),
            "code": code, "reason": reason}


def _ineligible_reason(row) -> tuple[str, str]:
    if row["market_type"] != "on_demand" or row["interruptible"]:
        return "not_on_demand", f"{row['market_type'] or 'unknown'} / interruptible capacity: not on-demand"
    if row["provider_tier"] == "cheapest":
        return "single_host_ask", "Vast's cheapest single-host ask; the median row stands for Vast"
    if row["provider_tier"] == "from_price":
        return "floor_price", "a 'from $x' floor price, not a purchasable price"
    return "ineligible", "not eligible under market rules"


def market_snapshot(rows: list[dict], now: datetime) -> dict:
    """Median and low of each provider's lowest current eligible price (market.detail's rule)."""
    best: dict[str, float] = {}
    as_of = None
    for r in rows:
        if not r["eligible"] or r["price_per_gpu_hour"] is None or r["price_per_gpu_hour"] <= 0:
            continue
        if r["available"] is False or now - r["observed_at"] > market.stale_after(r["provider"]):
            continue
        p = float(r["price_per_gpu_hour"])
        if r["provider"] not in best or p < best[r["provider"]]:
            best[r["provider"]] = p
        as_of = max(as_of or r["observed_at"], r["observed_at"])
    if not best:
        return {"median": None, "low": None, "providers": 0, "as_of": None, "kind": "observed_market_price"}
    low_p = min(best, key=best.get)
    return {"median": round(statistics.median(best.values()), 6), "low": best[low_p], "low_provider": low_p,
            "providers": len(best), "as_of": _iso(as_of), "kind": "observed_market_price",
            "rule": "median across providers of each provider's lowest current eligible per-GPU-hour price"}


def rank_listings(gpu: str, *, count: int = 1, region_group: str | None = None, max_price: float | None = None,
                  mode: str = "BALANCED", weights: dict | None = None, exclude_providers=(),
                  include_providers=None, require_level: int = 0, require_available: bool = False,
                  limit: int = 25, now: datetime | None = None, strict_region: bool = False) -> dict:
    """The best-execution ranking for one GPU. See the module docstring.

    strict_region: with a region_group, also EXCLUDE listings whose location is unknown
    (code region_unconfirmed) instead of scoring them 0.5 on region_match.
    """
    mode = (mode or "BALANCED").upper()
    if count < 1:
        raise ScoringError("count must be >= 1")
    w = effective_weights(mode, weights, bool(region_group))
    now = now or datetime.now(timezone.utc)
    with normalize.SessionLocal() as s:
        rows = [dict(r._mapping) for r in s.execute(_LISTINGS, {"gpu": gpu})]
    hist = history(gpu)
    mkt = market_snapshot(rows, now)
    excluded_set = {p.lower() for p in exclude_providers or ()}
    include_set = None if include_providers is None else {p.lower() for p in include_providers}

    exclusions, exact, multi = [], [], []
    for r in rows:
        prov = r["provider"]
        if not r["eligible"]:
            exclusions.append(_exclusion(r, *_ineligible_reason(r)))
            continue
        if r["price_per_gpu_hour"] is None or r["price_per_gpu_hour"] <= 0:
            exclusions.append(_exclusion(r, "no_price", "no price published"))
            continue
        if r["available"] is False:
            exclusions.append(_exclusion(r, "sold_out", "sold out"))
            continue
        age = now - r["observed_at"]
        if age > market.stale_after(prov):
            exclusions.append(_exclusion(r, "stale", f"stale: last seen {_age_text(age.total_seconds())} ago, "
                                                     f"past {_age_text(market.stale_after(prov).total_seconds())}"))
            continue
        shape = "exact"
        if r["gpu_count"] != count:
            if r["gpu_count"] < count and count % r["gpu_count"] == 0:
                shape = "multi"
            else:
                exclusions.append(_exclusion(r, "wrong_count", f"instance has {r['gpu_count']} GPUs; "
                                                               f"request is {count} GPUs per instance"))
                continue
        if max_price is not None and float(r["price_per_gpu_hour"]) > max_price:
            exclusions.append(_exclusion(r, "over_max_price", f"${float(r['price_per_gpu_hour']):.2f}/GPU-h is over "
                                                              f"the ${max_price:.2f} maximum"))
            continue
        if region_group:
            groups, ok = _region_groups(prov, r["region"], r["country"])
            if ok and groups and region_group not in groups:
                exclusions.append(_exclusion(r, "wrong_region", f"in {', '.join(sorted(groups))}, "
                                                                f"not {region_group}"))
                continue
            if strict_region and not (ok and region_group in groups):
                exclusions.append(_exclusion(r, "region_unconfirmed", f"region not confirmed as {region_group}"))
                continue
        if prov.lower() in excluded_set or (include_set is not None and prov.lower() not in include_set):
            exclusions.append(_exclusion(r, "excluded_by_preference", "provider excluded by request preferences"))
            continue
        if adapters.level(prov) < require_level:
            exclusions.append(_exclusion(r, "below_required_level", f"OpenGrid integration level "
                                                                    f"{adapters.level(prov)} < required {require_level}"))
            continue
        if require_available and r["available"] is not True:
            exclusions.append(_exclusion(r, "availability_unknown", "availability not reported; explicit "
                                                                    "availability required"))
            continue
        (exact if shape == "exact" else multi).append(r)

    def build(r):
        fs = factors(r, median=mkt["median"], hist=hist["providers"], want_group=region_group, now=now)
        lvl = adapters.level(r["provider"])
        c = {
            "provider": r["provider"], "provider_display": provider_meta.meta(r["provider"]).display_name,
            "listing_id": r["listing_id"], "sku": r["sku"], "raw_gpu_name": r["raw_gpu_name"],
            "gpu": r["gpu"], "gpu_count": r["gpu_count"], "instances": count // r["gpu_count"],
            "region": r["region"], "country": r["country"], "provider_tier": r["provider_tier"],
            "price_per_gpu_hour": float(r["price_per_gpu_hour"]),
            "price_per_instance_hour": _f(r["price_per_instance_hour"]),
            "price_kind": "observed_market_price", "available": r["available"],
            "observed_at": _iso(r["observed_at"]), "age_seconds": round((now - r["observed_at"]).total_seconds()),
            "integration_level": lvl, "provisionable": lvl >= 2,
            "score": score(fs, w), "factors": fs,
        }
        c["explanation"] = explanation(c)
        return c

    def order(cs):
        if mode == "CHEAPEST":
            cs.sort(key=lambda c: (c["price_per_gpu_hour"], c["age_seconds"], c["provider"], c["listing_id"]))
        else:
            cs.sort(key=lambda c: (-c["score"], c["price_per_gpu_hour"], c["age_seconds"], c["provider"],
                                   c["listing_id"]))
        for i, c in enumerate(cs, 1):
            c["rank"] = i
        return cs

    cands = order([build(r) for r in exact])
    for c in cands[1:]:
        c["vs_selected"] = versus(c, cands[0])
    multis = order([build(r) for r in multi])
    for c in multis:
        c["note"] = (f"needs {c['instances']} instances of {c['gpu_count']} GPUs; OpenGrid routes one instance "
                     f"per request, so this is not provisioned automatically")

    by_code: dict[str, int] = {}
    for e in exclusions:
        by_code[e["code"]] = by_code.get(e["code"], 0) + 1
    return {
        "gpu": gpu, "count": count, "count_semantics": "GPUs per instance; one instance per route",
        "mode": mode, "weights": w, "region_group": region_group, "strict_region": bool(strict_region),
        "max_price_per_gpu_hour": max_price,
        "as_of": now.isoformat(), "methodology_version": METHODOLOGY_VERSION,
        "market": mkt, "history_last_hour": hist["last_hour"],
        "candidates_total": len(cands), "candidates": cands[:limit],
        "multi_instance_alternatives": multis[:limit],
        "exclusions_total": len(exclusions), "exclusions_by_code": by_code,
        "exclusions": exclusions[:MAX_EXCLUSIONS],
        "not_used": {f: "no data: OpenGrid has no execution history for this yet" for f in NO_DATA},
    }


def rank_variants(variants: list[str], *, family: str, limit: int = 25, **kw) -> dict:
    """A route across a family's variants (only when the caller set allow_variants).

    Each variant is ranked by rank_listings on its OWN market (its price factor is relative to
    that variant's median); the candidates are then merged into one list, each tagged with its
    variant. CHEAPEST orders the merged list by price; other modes by score. There is no
    family-wide market price: `market` is empty and each variant's market is in by_variant.
    """
    from api.common import gpu_slug

    mode = (kw.get("mode") or "BALANCED").upper()
    per, cands, multis, excl = [], [], [], []
    last = None
    for v in variants:
        r = rank_listings(v, limit=limit, **kw)
        last = r
        for c in r["candidates"] + r["multi_instance_alternatives"]:
            c["variant"], c["variant_slug"] = v, gpu_slug(v)
            c.pop("vs_selected", None)
        cands += r["candidates"]
        multis += r["multi_instance_alternatives"]
        excl += [{**e, "gpu": v} for e in r["exclusions"]]
        per.append({"gpu": v, "slug": gpu_slug(v), "market": r["market"], "candidates_total": r["candidates_total"],
                    "exclusions_total": r["exclusions_total"]})

    def order(cs):
        if mode == "CHEAPEST":
            cs.sort(key=lambda c: (c["price_per_gpu_hour"], c["age_seconds"], c["provider"], c["listing_id"]))
        else:
            cs.sort(key=lambda c: (-c["score"], c["price_per_gpu_hour"], c["age_seconds"], c["provider"],
                                   c["listing_id"]))
        for i, c in enumerate(cs, 1):
            c["rank"] = i
        return cs

    cands, multis = order(cands), order(multis)
    for c in cands[1:]:
        c["vs_selected"] = versus(c, cands[0]) + (f" (variant {c['variant']})" if c["variant"] != cands[0]["variant"] else "")
    by_code: dict[str, int] = {}
    for e in excl:
        by_code[e["code"]] = by_code.get(e["code"], 0) + 1
    base = last or rank_listings(variants[0], limit=0, **kw)
    return {
        **{k: base[k] for k in ("count", "count_semantics", "mode", "weights", "region_group", "strict_region",
                                "max_price_per_gpu_hour", "as_of", "methodology_version", "history_last_hour",
                                "not_used")},
        "gpu": family, "family": family, "variants": list(variants), "by_variant": per,
        "market": {"median": None, "low": None, "providers": None, "as_of": None, "kind": "observed_market_price",
                   "note": "family route: no family-wide market price (variants are different products); "
                           "see by_variant[].market"},
        "variant_note": "each candidate is tagged with its variant; its price factor is relative to its own "
                        "variant's market median",
        "candidates_total": len(cands), "candidates": cands[:limit], "multi_instance_alternatives": multis[:limit],
        "exclusions_total": len(excl), "exclusions_by_code": by_code, "exclusions": excl[:MAX_EXCLUSIONS],
    }


def variant_market(ranking: dict, candidate: dict | None) -> dict:
    """The market snapshot that applies to a candidate: its own variant's in a family route."""
    if candidate is None or "by_variant" not in ranking:
        return ranking["market"]
    return next((v["market"] for v in ranking["by_variant"] if v["gpu"] == candidate["gpu"]), ranking["market"])
