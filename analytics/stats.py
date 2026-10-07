"""Historical context: where today's price sits in its own recorded history.

    gpu_context(gpu)                    the market's lowest and median price vs 30d / 90d
    provider_gpu_context(provider, gpu) one provider's lowest price vs its own history
    listing_context(provider, listing)  one listing's price vs its own history
    gpu_history(gpu, ...)               hourly / daily lowest, median, highest, provider count

Everything reads the hourly rollup (market_hourly), so a sample is "the state at
the top of an hour" and a 30-day window is 720 samples. Rules (methodology/historical-context.md):

    percentile    mid-rank: 100 x (samples below + half the samples equal) / samples,
                  the current hour included in its own window
    coverage      samples / hours in the window; below MIN_COVERAGE (70%) no
                  percentile, median distance or label is emitted, only the reason
    labels        <=10 very cheap, <=30 cheap, <70 normal, <90 expensive, >=90 very expensive
    panel         market-level windows use only providers recorded since the window
                  began, so a provider we started tracking last week cannot make the
                  market look cheap against a history it was never part of

Low percentile = cheap relative to this history. None of this says anything about
the future, and none of it is shown when the history is too thin to support it.
"""

from __future__ import annotations

import bisect
import statistics
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

import market
import normalize
import provider_meta
from cache import ttl_cache

METHODOLOGY = "historical-context"
HOUR = timedelta(hours=1)
MIN_COVERAGE = 0.7
MIN_EXTREME_SAMPLES = 24     # a "lowest since" needs at least a day of hourly samples
WINDOW_DAYS = {"30d": 30, "90d": 90}
BANDS = ((10, "very cheap", True), (30, "cheap", True), (70, "normal", False), (90, "expensive", False))


# --------------------------------------------------------------------------
# Pure
# --------------------------------------------------------------------------

def percentile_rank(samples, x: float) -> float:
    """Mid-rank percentile of x within samples (0..100)."""
    xs = sorted(samples)
    if not xs:
        raise ValueError("no samples")
    below = bisect.bisect_left(xs, x)
    equal = bisect.bisect_right(xs, x) - below
    return 100.0 * (below + 0.5 * equal) / len(xs)


def label(pct: float) -> str:
    """<=10 very cheap, <=30 cheap, <70 normal, <90 expensive, >=90 very expensive."""
    for bound, name, inclusive in BANDS:
        if pct < bound or (inclusive and pct == bound):
            return name
    return "very expensive"


def ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def window_stats(series, current: float | None, end: datetime, days: int) -> dict:
    """Percentile / median distance / label of `current` against `series` [(hour, value|None)] in (end-days, end]."""
    hours = days * 24
    start = end - timedelta(days=days)
    samples = [v for h, v in series if start < h <= end and v is not None]
    out = {"window": f"{days}d", "window_hours": hours, "samples": len(samples),
           "coverage": round(len(samples) / hours, 4), "first_sample": None,
           "percentile": None, "median": None, "min": None, "max": None,
           "distance_from_median_pct": None, "label": None, "reason": None}
    if samples:
        out["first_sample"] = next(h for h, v in series if start < h <= end and v is not None).isoformat()
    if current is None:
        out["reason"] = "no current price"
        return out
    if len(samples) < MIN_COVERAGE * hours:
        out["reason"] = (f"insufficient history: {len(samples)} of {hours} hourly samples "
                         f"({len(samples) / hours:.0%}); {MIN_COVERAGE:.0%} required")
        return out
    med = statistics.median(samples)
    pct = percentile_rank(samples, current)
    out.update(percentile=round(pct, 2), median=med, min=min(samples), max=max(samples),
               distance_from_median_pct=(current - med) / med if med else None, label=label(pct))
    return out


def extremes(series) -> dict:
    """Lowest and highest value in a [(hour, value|None)] series, with when (latest occurrence)."""
    pts = [(h, v) for h, v in series if v is not None]
    if not pts:
        return {"low": None, "high": None, "since": None}
    lo = min(pts, key=lambda p: (p[1], -p[0].timestamp()))
    hi = max(pts, key=lambda p: (p[1], p[0].timestamp()))
    return {"low": {"value": lo[1], "hour": lo[0].isoformat()}, "high": {"value": hi[1], "hour": hi[0].isoformat()},
            "since": pts[0][0].isoformat(), "samples": len(pts)}


def _distance(current, ex) -> dict:
    if current is None or ex["low"] is None:
        return {"from_low_pct": None, "from_high_pct": None, "reason": "no current price or no history"}
    if ex["samples"] < MIN_EXTREME_SAMPLES:
        return {"from_low_pct": None, "from_high_pct": None,
                "reason": f"insufficient history: {ex['samples']} hourly samples, {MIN_EXTREME_SAMPLES} required"}
    lo, hi = ex["low"]["value"], ex["high"]["value"]
    return {"from_low_pct": (current - lo) / lo if lo else None, "from_high_pct": (current - hi) / hi if hi else None}


def _headline(windows: dict) -> tuple[str | None, str | None]:
    for w in ("90d", "30d"):
        if windows.get(w, {}).get("label"):
            return windows[w]["label"], w
    return None, None


def _money(v):
    return f"${v:,.2f}/GPU-hr"


def summaries(subject: str, current, windows: dict, ex: dict, *, own: bool) -> list[str]:
    """Plain sentences built only from the numbers above; nothing that is not computed."""
    out = []
    for w in ("90d", "30d"):
        s = windows.get(w) or {}
        if s.get("percentile") is not None:
            days = s["window"][:-1]
            out.append(f"{subject} is in the {ordinal(int(round(s['percentile'])))} percentile of its "
                       f"{days}-day range ({s['label']}).")
            break
    s30 = windows.get("30d") or {}
    if s30.get("distance_from_median_pct") is not None:
        d = s30["distance_from_median_pct"]
        whose = "its own" if own else "its"
        if abs(d) < 0.005:
            out.append(f"{subject} is at {whose} 30-day median ({_money(s30['median'])}).")
        else:
            out.append(f"{subject} is {abs(d):.0%} {'below' if d < 0 else 'above'} {whose} 30-day median "
                       f"({_money(s30['median'])}).")
    if current is not None and ex.get("low") and ex["samples"] >= MIN_EXTREME_SAMPLES:
        lo = ex["low"]["value"]
        since = ex["since"][:10]
        if current <= lo:
            out.append(f"{subject} is at its lowest recorded level since {since} ({_money(lo)}).")
        else:
            out.append(f"{subject} is {(current - lo) / lo:.0%} above its lowest recorded level since {since} "
                       f"({_money(lo)}).")
    if not out:
        out.append(f"Not enough recorded history for {subject} to place today's price in context.")
    return out


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------

# One vote per provider (its lowest priced listing over regions), then across providers per hour.
_MARKET = text("""
    WITH p AS (
      SELECT hour, provider, min(min_price) AS price FROM market_hourly
      WHERE segment = :seg AND gpu = :gpu AND min_price IS NOT NULL
        AND (CAST(:t0 AS timestamptz) IS NULL OR hour > :t0)
        AND (CAST(:t1 AS timestamptz) IS NULL OR hour <= :t1)
        AND (CAST(:panel AS text[]) IS NULL OR provider = ANY(CAST(:panel AS text[])))
      GROUP BY hour, provider)
    SELECT hour, min(price) AS lowest, percentile_cont(0.5) WITHIN GROUP (ORDER BY price) AS median,
           max(price) AS highest, count(*) AS providers
    FROM p GROUP BY hour ORDER BY hour
""")

_FIRST = text("""SELECT provider, min(hour) AS first FROM market_hourly
                 WHERE segment = :seg AND gpu = :gpu GROUP BY provider""")


def latest_hour(s) -> datetime | None:
    return s.execute(text("SELECT max(hour) FROM market_hourly")).scalar()


def _market_rows(s, seg, gpu, t0=None, t1=None, panel=None):
    return [{"hour": r.hour, "lowest": float(r.lowest), "median": float(r.median), "highest": float(r.highest),
             "providers": int(r.providers)}
            for r in s.execute(_MARKET, {"seg": seg, "gpu": gpu, "t0": t0, "t1": t1, "panel": panel})]


def _gpu_name(gpu: str) -> str:
    return gpu.removeprefix("NVIDIA ")


@ttl_cache(600, maxsize=512)
def gpu_context(gpu: str, segment: str = "on_demand") -> dict:
    """The market's lowest and median price now, against its 30d / 90d hourly distribution."""
    out = {"gpu": gpu, "segment": segment, "interruptible": segment == "spot", "hour": None,
           "current": {"lowest": None, "median": None, "providers": 0},
           "metrics": {}, "label": None, "label_window": None, "summaries": [], "reason": None,
           "price_concept": "observed_market_price"}
    with normalize.SessionLocal() as s:
        end = latest_hour(s)
        if end is None:
            out["reason"] = "no history recorded yet"
            return out
        first = {r.provider: r.first for r in s.execute(_FIRST, {"seg": segment, "gpu": gpu})}
        if not first:
            out["reason"] = "no eligible listing has been recorded for this GPU"
            return out
        full = _market_rows(s, segment, gpu, t1=end)
        panels = {}
        for w, days in WINDOW_DAYS.items():
            start = end - timedelta(days=days)
            panel = sorted(p for p, f in first.items() if f <= start + HOUR)
            rows = _market_rows(s, segment, gpu, start, end, panel) if panel else []
            panels[w] = (panel, sorted(p for p in first if p not in panel), rows)
    out["hour"] = end.isoformat()
    now_row = full[-1] if full and full[-1]["hour"] == end else None
    if now_row:
        out["current"] = {"lowest": now_row["lowest"], "median": now_row["median"], "highest": now_row["highest"],
                          "providers": now_row["providers"]}
    else:
        out["reason"] = f"no provider has a priced, in-stock listing at {end:%Y-%m-%d %H:00Z}"
    name = _gpu_name(gpu)
    for metric in ("lowest", "median"):
        windows = {}
        for w, days in WINDOW_DAYS.items():
            panel, left_out, rows = panels[w]
            series = [(r["hour"], r[metric]) for r in rows]
            cur = rows[-1][metric] if rows and rows[-1]["hour"] == end else None
            st = window_stats(series, cur, end, days)
            st["current"] = cur
            st["panel"] = panel
            st["excluded_recent_providers"] = left_out
            if not panel:
                st["reason"] = f"history does not cover the window: recording began {min(first.values()):%Y-%m-%d %H:00Z}"
            windows[w] = st
        ex = extremes([(r["hour"], r[metric]) for r in full])
        cur = out["current"][metric]
        subject = f"{name} market {'lowest' if metric == 'lowest' else 'median'} price"
        out["metrics"][metric] = {
            "current": cur, "windows": windows, "historical": {**ex, **_distance(cur, ex)},
            "summaries": summaries(subject, cur, windows, ex, own=False),
        }
    lab, w = _headline(out["metrics"]["median"]["windows"])
    out["label"], out["label_window"] = lab, w
    if lab is None:
        out["label_reason"] = (out["metrics"]["median"]["windows"]["30d"]["reason"]
                               or out["metrics"]["median"]["windows"]["90d"]["reason"])
    out["summaries"] = out["metrics"]["median"]["summaries"][:1] + out["metrics"]["lowest"]["summaries"][:2]
    return out


@ttl_cache(600, maxsize=2048)
def provider_gpu_context(provider: str, gpu: str, segment: str = "on_demand") -> dict:
    """One provider's lowest price for a GPU against its own recorded history."""
    from analytics import rollups

    out = {"provider": provider, "gpu": gpu, "segment": segment, "interruptible": segment == "spot",
           "hour": None, "current": None, "windows": {}, "historical": None, "vs_market_median_pct": None,
           "label": None, "label_window": None, "summaries": [], "reason": None,
           "price_concept": "observed_market_price"}
    with normalize.SessionLocal() as s:
        end = latest_hour(s)
    if end is None:
        out["reason"] = "no history recorded yet"
        return out
    rows = rollups.provider_hourly(segment, gpu, provider, None, end)
    if not rows:
        out["reason"] = "this provider has no recorded eligible listing for this GPU"
        return out
    out["hour"] = end.isoformat()
    series = [(r["hour"], r["min_price"]) for r in rows]
    cur = rows[-1]["min_price"] if rows[-1]["hour"] == end else None
    if cur is None:
        out["reason"] = f"no priced, in-stock listing at {end:%Y-%m-%d %H:00Z}"
    out["current"] = cur
    out["recorded_since"] = rows[0]["hour"].isoformat()
    for w, days in WINDOW_DAYS.items():
        out["windows"][w] = window_stats(series, cur, end, days)
    ex = extremes(series)
    out["historical"] = {**ex, **_distance(cur, ex)}
    mk = gpu_context(gpu, segment)["current"]
    if cur is not None and mk.get("median"):
        out["vs_market_median_pct"] = (cur - mk["median"]) / mk["median"]
    out["label"], out["label_window"] = _headline(out["windows"])
    if out["label"] is None:
        out["label_reason"] = out["windows"]["30d"]["reason"]
    pname = provider_meta.meta(provider).display_name
    out["summaries"] = summaries(f"{pname} {_gpu_name(gpu)}", cur, out["windows"], ex, own=True)
    if out["vs_market_median_pct"] is not None:
        d = out["vs_market_median_pct"]
        out["summaries"].append(f"{pname} {_gpu_name(gpu)} is {abs(d):.0%} "
                                f"{'below' if d < 0 else 'above'} the market median right now.")
    return out


_LISTING = text("""SELECT provider, listing_id, canonical_gpu_name AS gpu, observed_at AS last_seen, sku, region,
                          market_type, interruptible FROM compute_listings
                   WHERE provider = :p AND listing_id = :l""")
_LISTING_OBS = text("""
    SELECT observed_at, price_per_gpu_hour AS price, available FROM (
      SELECT observed_at, price_per_gpu_hour, available,
             row_number() OVER (PARTITION BY observed_at < :t0 ORDER BY observed_at DESC) AS rn
      FROM listing_observations WHERE provider = :p AND listing_id = :l
    ) x WHERE observed_at >= :t0 OR rn = 1 ORDER BY observed_at
""")


@ttl_cache(300, maxsize=2048)
def listing_context(provider: str, listing_id: str) -> dict | None:
    """One listing's price at the top of each hour over 90 days (market.py's sampling rules)
    against its current price. None when the listing is unknown."""
    now = datetime.now(timezone.utc)
    end = now.replace(minute=0, second=0, microsecond=0)
    t0 = end - timedelta(days=max(WINDOW_DAYS.values()))
    with normalize.SessionLocal() as s:
        lst = s.execute(_LISTING, {"p": provider, "l": listing_id}).first()
        if lst is None:
            return None
        ev = [(r.observed_at, r.price, r.available) for r in s.execute(_LISTING_OBS, {"p": provider, "l": listing_id, "t0": t0})]
    out = {"provider": provider, "listing_id": listing_id, "gpu": lst.gpu, "sku": lst.sku, "region": lst.region,
           "market_type": lst.market_type, "interruptible": bool(lst.interruptible), "current": None,
           "windows": {}, "historical": None, "label": None, "summaries": [],
           "price_concept": "observed_market_price"}
    if not ev:
        out["reason"] = "no recorded observations"
        return out
    ts = [e[0] for e in ev]
    stale = market.stale_after(provider)
    hours, h = [], end - timedelta(days=max(WINDOW_DAYS.values())) + HOUR
    while h <= end:
        hours.append(h)
        h += HOUR
    series = [(h, market.price_at(ts, ev, lst.last_seen, stale, h)) for h in hours]
    cur = market.price_at(ts, ev, lst.last_seen, stale, now)
    out["current"] = cur
    for w, days in WINDOW_DAYS.items():
        out["windows"][w] = window_stats(series, cur, end, days)
    ex = extremes(series)
    out["historical"] = {**ex, **_distance(cur, ex), "note": "within the last 90 days"}
    out["label"], out["label_window"] = _headline(out["windows"])
    out["summaries"] = summaries(f"{provider_meta.meta(provider).display_name} listing {lst.sku or listing_id}",
                                 cur, out["windows"], ex, own=True)
    return out


def gpu_history(gpu: str, segment: str = "on_demand", t0: datetime | None = None, t1: datetime | None = None,
                resolution: str = "1h") -> dict:
    """The observed cross-section over time: lowest / median / highest provider price and provider count.

    Not chain-linked: when a provider starts being recorded these lines can move without any
    price changing, so `providers_joined` lists those moments and `coherent_from` is the
    first hour from which every provider selling now was already recorded (market.py's rule).
    The chain-linked index is the like-for-like measure.
    """
    with normalize.SessionLocal() as s:
        end = latest_hour(s)
        rows = _market_rows(s, segment, gpu, t0 - HOUR if t0 else None, t1 or end) if end else []
        first = {r.provider: r.first for r in s.execute(_FIRST, {"seg": segment, "gpu": gpu})}
        live_now = []
        if end is not None:
            live_now = [r.provider for r in s.execute(text("""
                SELECT DISTINCT provider FROM market_hourly WHERE segment = :seg AND gpu = :gpu AND hour = :h
                  AND min_price IS NOT NULL"""), {"seg": segment, "gpu": gpu, "h": end})]
    coherent = max((first[p] for p in live_now if p in first), default=None)
    joined = sorted(({"provider": p, "first_hour": f.isoformat()} for p, f in first.items()
                     if (t0 is None or f >= t0) and (t1 is None or f <= t1)), key=lambda x: x["first_hour"])
    if resolution == "1d":
        days: dict = {}
        for r in rows:
            days.setdefault(r["hour"].date(), []).append(r)
        series = [{"t": datetime(d.year, d.month, d.day, tzinfo=timezone.utc).isoformat(),
                   "lowest": min(x["lowest"] for x in rs), "median": statistics.median(x["median"] for x in rs),
                   "highest": max(x["highest"] for x in rs), "providers": max(x["providers"] for x in rs),
                   "hours": len(rs)} for d, rs in sorted(days.items())]
    else:
        series = [{"t": r["hour"].isoformat(), "lowest": r["lowest"], "median": r["median"], "highest": r["highest"],
                   "providers": r["providers"]} for r in rows]
    return {"gpu": gpu, "segment": segment, "resolution": resolution, "series": series,
            "coherent_from": coherent.isoformat() if coherent else None, "providers_joined": joined,
            "latest_hour": end.isoformat() if end else None}


def clear_caches():
    gpu_context.cache_clear()
    provider_gpu_context.cache_clear()
    listing_context.cache_clear()
