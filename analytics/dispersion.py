"""Price dispersion: how far apart providers price the same GPU, now and over time.

Current state uses market.py's rules exactly: eligible listings (canonical,
on-demand, not interruptible, not Vast's "cheapest" row, not a "from" price),
still live (seen within market.stale_after), priced (> 0) and not sold out.

Two populations, never mixed:
    provider votes   one price per provider = its lowest eligible price. The market
                     statistics (low / median / high / IQR / CV / score) use these, so
                     a provider with 40 listings does not outvote one with 1.
    listings         every eligible priced listing, reported alongside for context.

Statistics (provider votes, N = providers):
    spread_abs              high - low
    spread_pct_of_low       high / low - 1
    spread_pct_of_median    (high - low) / median
    iqr, iqr_rel            Q3 - Q1 (quartiles, inclusive method), and that / median   N >= 3
    stdev, cv               sample standard deviation, and stdev / mean               N >= 3
Fragmentation score 0-100 (higher = more fragmented), N >= 3:
    cv_adj  = cv * (1 + 1 / (4N))         small-sample correction of the CV
    c_cv    = min(cv_adj / 0.60, 1)
    c_iqr   = min(iqr_rel / 0.60, 1)
    c_range = min(spread_pct_of_low / 3.00, 1)       (saturates when high = 4x low)
    fragmentation = 100 * (0.4 c_cv + 0.4 c_iqr + 0.2 c_range); efficiency = 100 - fragmentation
    label: < 25 efficient, < 50 moderately fragmented, else highly fragmented
See methodology/dispersion.md.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

import market
import normalize
from cache import ttl_cache

METHODOLOGY = "dispersion"
MIN_N_STATS = 3
CV_SATURATION = 0.60
IQR_SATURATION = 0.60
RANGE_SATURATION = 3.00
WEIGHTS = {"cv": 0.4, "iqr": 0.4, "range": 0.2}

_CURRENT = text(f"""
    SELECT c.provider, c.listing_id, c.canonical_gpu_name AS gpu, c.sku, c.gpu_count, c.region, c.country,
           c.price_per_gpu_hour AS price, c.available, c.observed_at AS last_seen, c.first_seen_at
    FROM compute_listings c
    WHERE {market._ELIGIBLE}
""")


@ttl_cache(30)
def current_listings() -> list[dict]:
    """Every eligible listing live now (seen within stale_after), priced or not, sold out or not.

    Cached briefly and shared: callers must not mutate the rows."""
    now = datetime.now(timezone.utc)
    with normalize.SessionLocal() as s:
        rows = [dict(r._mapping) for r in s.execute(_CURRENT)]
    out = []
    for r in rows:
        if now - r["last_seen"] > market.stale_after(r["provider"]):
            continue
        r["price"] = None if r["price"] is None else float(r["price"])
        r["priced"] = r["price"] is not None and r["price"] > 0 and r["available"] is not False
        out.append(r)
    return out


def _r(x, nd=6):
    return None if x is None else round(float(x), nd)


def stats(values: list[float]) -> dict:
    """Dispersion statistics of a list of prices. Pure. Null fields carry a reason."""
    v = sorted(float(x) for x in values if x is not None)
    n = len(v)
    out = {"n": n, "low": None, "median": None, "high": None, "mean": None, "spread_abs": None,
           "spread_pct_of_low": None, "spread_pct_of_median": None, "q1": None, "q3": None, "iqr": None,
           "iqr_rel": None, "stdev": None, "cv": None, "reasons": {}}
    if n == 0:
        out["reasons"]["all"] = "no priced providers"
        return out
    lo, hi, med = v[0], v[-1], statistics.median(v)
    mean = statistics.fmean(v)
    out.update(low=lo, high=hi, median=med, mean=_r(mean), spread_abs=_r(hi - lo))
    out["spread_pct_of_low"] = _r(hi / lo - 1) if lo > 0 else None
    out["spread_pct_of_median"] = _r((hi - lo) / med) if med > 0 else None
    if n >= MIN_N_STATS:
        q1, _, q3 = statistics.quantiles(v, n=4, method="inclusive")
        sd = statistics.stdev(v)
        out.update(q1=_r(q1), q3=_r(q3), iqr=_r(q3 - q1), iqr_rel=_r((q3 - q1) / med) if med > 0 else None,
                   stdev=_r(sd), cv=_r(sd / mean) if mean > 0 else None)
    else:
        why = f"needs at least {MIN_N_STATS} providers (have {n})"
        out["reasons"].update(iqr=why, stdev=why, cv=why)
    return out


def score(st: dict) -> dict:
    """Fragmentation / efficiency score from `stats` output, with components. Null below N=3."""
    n = st["n"]
    if n < MIN_N_STATS or st["cv"] is None or st["iqr_rel"] is None or st["spread_pct_of_low"] is None:
        return {"fragmentation": None, "efficiency": None, "label": None, "components": None,
                "confidence": None, "reason": f"needs at least {MIN_N_STATS} priced providers (have {n})"}
    cv_adj = st["cv"] * (1 + 1 / (4 * n))
    comp = {
        "cv_adj": round(cv_adj, 6), "c_cv": round(min(cv_adj / CV_SATURATION, 1.0), 6),
        "iqr_rel": st["iqr_rel"], "c_iqr": round(min(st["iqr_rel"] / IQR_SATURATION, 1.0), 6),
        "range": st["spread_pct_of_low"], "c_range": round(min(st["spread_pct_of_low"] / RANGE_SATURATION, 1.0), 6),
    }
    frag = 100 * (WEIGHTS["cv"] * comp["c_cv"] + WEIGHTS["iqr"] * comp["c_iqr"] + WEIGHTS["range"] * comp["c_range"])
    label = "efficient" if frag < 25 else "moderately fragmented" if frag < 50 else "highly fragmented"
    return {"fragmentation": round(frag, 1), "efficiency": round(100 - frag, 1), "label": label,
            "components": comp, "weights": WEIGHTS,
            "saturation": {"cv": CV_SATURATION, "iqr_rel": IQR_SATURATION, "range": RANGE_SATURATION}, "confidence": "low" if n < 6 else "normal", "reason": None}


def premium(own: float, others: list[float]) -> float | None:
    """own / median(other providers' lowest) - 1, or None when nobody else prices it."""
    others = [x for x in others if x is not None and x > 0]
    if not others or own is None:
        return None
    return own / statistics.median(others) - 1


def _market(gpu: str, rows: list[dict]) -> dict:
    priced = [r for r in rows if r["priced"]]
    best: dict[str, dict] = {}
    for r in priced:
        b = best.get(r["provider"])
        if b is None or r["price"] < b["price"]:
            best[r["provider"]] = r
    votes = {p: b["price"] for p, b in best.items()}
    st = stats(list(votes.values()))
    lst = stats([r["price"] for r in priced])
    providers = []
    for p, b in sorted(best.items(), key=lambda kv: (kv[1]["price"], kv[0])):
        others = [v for q, v in votes.items() if q != p]
        prem = premium(b["price"], others)
        providers.append({
            "provider": p, "price": b["price"], "listing_id": b["listing_id"], "sku": b["sku"],
            "region": b["region"], "available": b["available"],
            "premium_vs_others_median": _r(prem),
            "rank": 1 + sum(1 for v in votes.values() if v < b["price"]),
            "listings": sum(1 for r in priced if r["provider"] == p),
        })
    return {
        "gpu": gpu, "providers": len(votes), "low": st["low"], "median": st["median"], "high": st["high"],
        "stats": st, "score": score(st),
        "listings": {"live": len(rows), "priced": len(priced),
                     "available": sum(1 for r in rows if r["available"] is True),
                     "availability_unknown": sum(1 for r in rows if r["available"] is None),
                     "sold_out": sum(1 for r in rows if r["available"] is False),
                     "stats": {k: lst[k] for k in ("n", "low", "median", "high", "q1", "q3", "stdev")}},
        "by_provider": providers,
    }


@ttl_cache(60)
def all_markets() -> dict[str, dict]:
    """{gpu: current market} for every GPU with at least one live eligible listing."""
    by_gpu: dict[str, list] = defaultdict(list)
    for r in current_listings():
        by_gpu[r["gpu"]].append(r)
    return {g: _market(g, rows) for g, rows in by_gpu.items()}


def market_now(gpu: str) -> dict:
    """Current market for one GPU; an empty market (providers 0) when nothing is live."""
    m = all_markets().get(gpu)
    return m if m is not None else _market(gpu, [])


def spreads() -> list[dict]:
    """Every GPU's current dispersion, most fragmented first (unscored GPUs last)."""
    out = []
    for g, m in all_markets().items():
        st, sc = m["stats"], m["score"]
        out.append({"gpu": g, "providers": m["providers"], "low": m["low"], "median": m["median"], "high": m["high"],
                    "spread_abs": st["spread_abs"], "spread_pct_of_low": st["spread_pct_of_low"],
                    "spread_pct_of_median": st["spread_pct_of_median"], "cv": st["cv"], "iqr_rel": st["iqr_rel"],
                    "fragmentation": sc["fragmentation"], "efficiency": sc["efficiency"], "label": sc["label"],
                    "confidence": sc["confidence"], "reason": sc["reason"],
                    "available_listings": m["listings"]["available"]})
    out.sort(key=lambda x: (x["fragmentation"] is None, -(x["fragmentation"] or 0),
                            -(x["spread_pct_of_low"] or 0), x["gpu"]))
    return out


# --------------------------------------------------------------------------
# History
# --------------------------------------------------------------------------

def history(gpu: str, days: int = 30, resolution: str = "1d", segment: str = "on_demand") -> dict:
    """Dispersion over time. 1d from structure_gpu_daily; 1h recomputed from the rollup (<= 14 days)."""
    now = datetime.now(timezone.utc)
    if resolution == "1h":
        from analytics import rollups

        days = min(days, 14)
        rows = rollups.provider_hourly(segment, gpu=gpu, t0=now - timedelta(days=days))
        by_hour: dict = defaultdict(list)
        for r in rows:
            if r["min_price"] is not None:
                by_hour[r["hour"]].append(r["min_price"])
        points = []
        for h in sorted(by_hour):
            st = stats(by_hour[h])
            points.append({"t": h.isoformat(), "providers": st["n"], "low": st["low"], "median": st["median"],
                           "high": st["high"], "cv": st["cv"], "iqr_rel": st["iqr_rel"],
                           "spread_pct_of_low": st["spread_pct_of_low"],
                           "fragmentation": score(st)["fragmentation"]})
        return {"gpu": gpu, "resolution": "1h", "days": days, "points": points}
    d0 = (now - timedelta(days=days - 1)).date()
    sql = text("""SELECT * FROM structure_gpu_daily WHERE segment = :s AND gpu = :g AND day >= :d0 ORDER BY day""")
    with normalize.SessionLocal() as s:
        rows = s.execute(sql, {"s": segment, "g": gpu, "d0": d0}).all()
    points = []
    for r in rows:
        points.append({
            "t": r.day.isoformat(), "hours_market": r.hours_market, "providers_max": r.providers_max,
            "cv_mean": r.cv_sum / r.hours_cv if r.hours_cv else None, "cv_hours": r.hours_cv,
            "iqr_rel_mean": r.iqr_rel_sum / r.hours_cv if r.hours_cv else None,
            "spread_pct_of_low_mean": r.spread_sum / r.hours_spread if r.hours_spread else None,
            "close_low": _r(r.close_low), "close_median": _r(r.close_median), "close_high": _r(r.close_high),
            "close_providers": r.close_providers,
        })
    return {"gpu": gpu, "resolution": "1d", "days": days, "points": points,
            "note": "daily means of hourly cross-provider statistics; cv/iqr only over hours with >= 3 providers"}


def log_returns(series: list[float | None]) -> list[float]:
    """log(x_t / x_{t-1}) over consecutive non-null positive pairs."""
    out = []
    for a, b in zip(series, series[1:]):
        if a and b and a > 0 and b > 0:
            out.append(math.log(b / a))
    return out


# --------------------------------------------------------------------------
# Per-provider 24h change (for /v1/markets/{gpu})
# --------------------------------------------------------------------------

_THEN = text(f"""
    -- One index probe per listing (ix_obs_listing_time): the observation in force at the cutoff.
    SELECT c.provider, c.listing_id, c.observed_at AS last_seen, o.price, o.available
    FROM compute_listings c
    CROSS JOIN LATERAL (
        SELECT price_per_gpu_hour AS price, available FROM listing_observations o
        WHERE o.provider = c.provider AND o.listing_id = c.listing_id AND o.observed_at <= :cutoff
        ORDER BY o.observed_at DESC LIMIT 1
    ) o
    WHERE {market._ELIGIBLE} AND c.canonical_gpu_name = :gpu
""")


@ttl_cache(3600, maxsize=256)
def _first_fetch(provider: str):
    """When OpenGrid first heard from a provider (normalize.price_changes' coverage rule)."""
    with normalize.SessionLocal() as s:
        return s.execute(text("SELECT min(fetched_at) FROM raw_snapshots WHERE ok AND provider = :p"),
                         {"p": provider}).scalar()


@ttl_cache(60, maxsize=256)
def provider_changes_24h(gpu: str, hours: float = 24) -> dict[str, dict]:
    """{provider: 24h change of its lowest current eligible price}, from listing_observations.

    The price 24h ago is, per provider, the lowest price in force at the cutoff (last change-only
    observation at or before it) among listings live then (market.price_at's rule). Status and
    thresholds are normalize.classify_change's: up / down (>= 0.5% and >= $0.001), flat, new
    (no priced listing for this GPU then), nodata (we were not recording the provider then).
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)
    cur = {p["provider"]: p["price"] for p in market_now(gpu)["by_provider"]}
    with normalize.SessionLocal() as s:
        rows = s.execute(_THEN, {"gpu": gpu, "cutoff": cutoff}).all()
    then: dict[str, float] = {}
    for r in rows:
        if r.price is None or r.price <= 0 or r.available is False:
            continue
        if cutoff > r.last_seen + market.stale_after(r.provider):
            continue  # that listing was already gone at the cutoff
        p = float(r.price)
        if r.provider not in then or p < then[r.provider]:
            then[r.provider] = p
    out = {}
    for prov, price in cur.items():
        first = _first_fetch(prov)
        status, pct = normalize.classify_change(price, then.get(prov), cutoff if prov in then else None, first, cutoff)
        reason = None
        if status == "nodata":
            reason = (f"not recorded {int(hours)}h ago: OpenGrid began recording {prov} at "
                      f"{first.isoformat() if first else 'an unknown time'}")
        elif status == "new":
            reason = f"no priced listing for this GPU {int(hours)}h ago"
        out[prov] = {"pct": None if pct is None else round(float(pct), 6), "status": status,
                     "from": then.get(prov), "to": price, "since": cutoff.isoformat(), "reason": reason,
                     "basis": "provider's lowest eligible price now vs in force 24h ago (listing_observations)"}
    return out
