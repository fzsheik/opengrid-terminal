"""Provider relative value: how each provider prices against the rest of the market, over time.

Everything historical comes from market_hourly (via store/structure daily sums), so a provider
is compared only with what other providers charged in the SAME hour.

Definitions (per GPU, per hour, provider p priced = had a live, priced, not-sold-out listing):
    own(p, h)        p's lowest eligible price that hour
    premium(p, h)    own(p, h) / median{ own(q, h) : q != p, q priced } - 1     (needs >= 1 other)
    premium(p)       mean of premium(p, h) over the hours where it exists ("compared hours")
    rank(p, h)       1 + number of providers strictly cheaper (ties share the rank)
    cheapest share   hours rank = 1 / "market hours", where market hours are hours at or after
                     p's first recorded hour for that GPU in which >= 1 OTHER provider was priced
                     (an hour p was absent while others sold counts against it)
    availability     hours priced / hours tracked (tracked = rollup hours since p's first record)
    volatility       sample stdev of daily log changes of p's closing lowest price (>= 7 changes)
Across GPUs ('*'): comparison sums add up over (GPU, hour) pairs; availability counts an hour once
if any GPU was priced. Minimum samples: MIN_HOURS hours for hour-based figures, MIN_RETURNS daily
changes for volatility; below that the figure is null with a reason. See methodology/provider-value.md.
"""

from __future__ import annotations

import logging
import math
import statistics
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert

import normalize
import provider_meta
from analytics import rollups
from cache import ttl_cache
from store.structure import StructureGpuDaily, StructureProviderDaily

log = logging.getLogger(__name__)

METHODOLOGY = "provider-value"
MIN_HOURS = 24
MIN_RETURNS = 7
MIN_LISTINGS_LIFETIME = 5
ALL = "*"
RANK_CAP = 10


# --------------------------------------------------------------------------
# Daily summaries (pure core + writer)
# --------------------------------------------------------------------------

def _blank():
    return {"hours_tracked": 0, "hours_live": 0, "hours_priced": 0, "hours_available": 0, "hours_market": 0,
            "hours_compared": 0, "premium_sum": 0.0, "hours_cheapest": 0, "hours_top3": 0, "rank_counts": {},
            "_prices": [], "_close": None}


def daily_rows(hourly: list[dict], first: dict, rollup_hours: list[datetime], segment: str = "on_demand"):
    """(provider-day rows, gpu-day rows) from provider_hourly rows. Pure: no database.

    `first` is rollups.first_hours(); `rollup_hours` every hour the rollup sampled in the window.
    """
    by_gpu_hour: dict[tuple, dict] = defaultdict(dict)
    for r in hourly:
        by_gpu_hour[(r["gpu"], r["hour"])][r["provider"]] = r
    gpus = {g for g, _ in by_gpu_hour}
    providers_of = defaultdict(set)
    for (g, p) in first:
        providers_of[g].add(p)
    for (g, _), rows in by_gpu_hour.items():
        providers_of[g].update(rows)
    first_any: dict[str, datetime] = {}
    for (g, p), t in first.items():
        first_any[p] = min(first_any.get(p, t), t)

    prov: dict[tuple, dict] = defaultdict(_blank)    # (gpu, provider, day)
    union: dict[tuple, dict] = defaultdict(lambda: {"tracked": set(), "live": set(), "priced": set(),
                                                     "available": set()})  # (provider, day) -> hour sets
    gday: dict[tuple, dict] = {}
    hours_sorted = sorted(set(rollup_hours))

    for g in sorted(gpus):
        prev_median, prev_hour = None, None
        for h in hours_sorted:
            day = h.date()
            rows = by_gpu_hour.get((g, h), {})
            priced = {p: r["min_price"] for p, r in rows.items() if r["min_price"] is not None and r["min_price"] > 0}
            gd = gday.setdefault((g, day), {"hours_market": 0, "hours_cv": 0, "cv_sum": 0.0, "iqr_rel_sum": 0.0,
                                            "hours_spread": 0, "spread_sum": 0.0, "providers_max": 0,
                                            "provider_hours_tracked": 0, "provider_hours_priced": 0,
                                            "_close": None, "_returns": []})
            for p in providers_of[g]:
                f = first.get((g, p))
                if f is None or h < f:
                    continue
                d = prov[(g, p, day)]
                d["hours_tracked"] += 1
                gd["provider_hours_tracked"] += 1
                union[(p, day)]["tracked"].add(h)
                r = rows.get(p)
                if r is not None:
                    d["hours_live"] += 1
                    union[(p, day)]["live"].add(h)
                    if (r.get("available_listings") or 0) > 0:
                        d["hours_available"] += 1
                        union[(p, day)]["available"].add(h)
                if p in priced:
                    d["hours_priced"] += 1
                    gd["provider_hours_priced"] += 1
                    union[(p, day)]["priced"].add(h)
                    d["_prices"].append(priced[p])
                    d["_close"] = priced[p]
                others = [v for q, v in priced.items() if q != p]
                if others:
                    d["hours_market"] += 1
                    if p in priced:
                        own = priced[p]
                        d["hours_compared"] += 1
                        d["premium_sum"] += own / statistics.median(others) - 1
                        rank = 1 + sum(1 for v in others if v < own)
                        d["hours_cheapest"] += rank == 1
                        d["hours_top3"] += rank <= 3
                        k = str(min(rank, RANK_CAP))
                        d["rank_counts"][k] = d["rank_counts"].get(k, 0) + 1
            if priced:
                v = sorted(priced.values())
                n = len(v)
                med = statistics.median(v)
                gd["hours_market"] += 1
                gd["providers_max"] = max(gd["providers_max"], n)
                gd["_close"] = (v[0], med, v[-1], n)
                if n >= 2:
                    gd["hours_spread"] += 1
                    gd["spread_sum"] += v[-1] / v[0] - 1
                if n >= 3:
                    q1, _, q3 = statistics.quantiles(v, n=4, method="inclusive")
                    mean = statistics.fmean(v)
                    gd["hours_cv"] += 1
                    gd["cv_sum"] += statistics.stdev(v) / mean
                    gd["iqr_rel_sum"] += (q3 - q1) / med
                if prev_median and prev_hour is not None and h - prev_hour == rollups.HOUR and prev_hour.date() == day:
                    gd["_returns"].append(math.log(med / prev_median))
                prev_median, prev_hour = med, h
            else:
                prev_median, prev_hour = None, None

    out_p = []
    agg: dict[tuple, dict] = defaultdict(_blank)
    for (g, p, day), d in prov.items():
        a = agg[(p, day)]
        for k in ("hours_market", "hours_compared", "hours_cheapest", "hours_top3"):
            a[k] += d[k]
        a["premium_sum"] += d["premium_sum"]
        for k, n in d["rank_counts"].items():
            a["rank_counts"][k] = a["rank_counts"].get(k, 0) + n
        out_p.append(_prow(segment, g, p, day, d))
    for (p, day), a in agg.items():
        u = union[(p, day)]
        a["hours_tracked"], a["hours_live"] = len(u["tracked"]), len(u["live"])
        a["hours_priced"], a["hours_available"] = len(u["priced"]), len(u["available"])
        out_p.append(_prow(segment, ALL, p, day, a))

    out_g = []
    for (g, day), gd in gday.items():
        if gd["provider_hours_tracked"] == 0 and gd["hours_market"] == 0:
            continue
        c = gd.pop("_close")
        ret = gd.pop("_returns")
        out_g.append({"segment": segment, "gpu": g, "day": day, **gd,
                      "close_low": c and c[0], "close_median": c and c[1], "close_high": c and c[2],
                      "close_providers": c[3] if c else 0,
                      "median_vol": statistics.stdev(ret) if len(ret) >= 12 else None, "vol_returns": len(ret)})
    return out_p, out_g


def _prow(segment, g, p, day, d):
    prices = d.get("_prices") or []
    return {"segment": segment, "gpu": g, "provider": p, "day": day,
            **{k: d[k] for k in ("hours_tracked", "hours_live", "hours_priced", "hours_available", "hours_market",
                                 "hours_compared", "hours_cheapest", "hours_top3")},
            "premium_sum": float(d["premium_sum"]), "rank_counts": d["rank_counts"] or None,
            "price_low": min(prices) if prices else None, "price_high": max(prices) if prices else None,
            "price_avg": sum(prices) / len(prices) if prices else None, "price_close": d.get("_close")}


def refresh_daily(segment: str = "on_demand", since: date | None = None) -> dict:
    """Recompute the daily summaries from `since` (default: the last stored day - 1; all if empty)."""
    with normalize.SessionLocal() as s:
        last = s.execute(text("SELECT max(day) FROM structure_gpu_daily WHERE segment = :s"), {"s": segment}).scalar()
    d0 = since or (last - timedelta(days=1) if last else None)
    t0 = None if d0 is None else datetime.combine(d0, time(0), tzinfo=timezone.utc)
    hourly = rollups.provider_hourly(segment, t0=t0)
    first = rollups.first_hours(segment)
    with normalize.SessionLocal() as s:
        hours = [r[0] for r in s.execute(text(
            "SELECT DISTINCT hour FROM market_hourly WHERE segment = :s AND (CAST(:t0 AS timestamptz) IS NULL OR hour >= :t0)"),
            {"s": segment, "t0": t0})]
    prows, grows = daily_rows(hourly, first, hours, segment)
    if not hours:
        return {"provider_rows": 0, "gpu_rows": 0}
    d_from = d0 or min(h.date() for h in hours)
    with normalize.SessionLocal.begin() as s:
        s.execute(text("DELETE FROM structure_provider_daily WHERE segment = :s AND day >= :d"), {"s": segment, "d": d_from})
        s.execute(text("DELETE FROM structure_gpu_daily WHERE segment = :s AND day >= :d"), {"s": segment, "d": d_from})
        for i in range(0, len(prows), 2000):
            s.execute(insert(StructureProviderDaily).values(prows[i:i + 2000]))
        for i in range(0, len(grows), 2000):
            s.execute(insert(StructureGpuDaily).values(grows[i:i + 2000]))
    return {"provider_rows": len(prows), "gpu_rows": len(grows), "from": d_from.isoformat()}


def _after_refresh():
    refresh_daily()
    clear_caches()


def clear_caches():
    from analytics import dispersion

    for fn in (provider_value_all, feed_health, dispersion.all_markets, dispersion.current_listings):
        fn.cache_clear()


if _after_refresh not in rollups.AFTER_REFRESH:
    rollups.AFTER_REFRESH.append(_after_refresh)


# --------------------------------------------------------------------------
# Readers
# --------------------------------------------------------------------------

def _null(reason):
    return {"value": None, "reason": reason}


def _daily(segment: str, days: int, provider: str | None = None) -> list:
    d0 = (datetime.now(timezone.utc) - timedelta(days=days - 1)).date()
    sql = text("""SELECT * FROM structure_provider_daily WHERE segment = :s AND day >= :d0
                  AND (CAST(:p AS text) IS NULL OR provider = :p) ORDER BY provider, gpu, day""")
    with normalize.SessionLocal() as s:
        return s.execute(sql, {"s": segment, "d0": d0, "p": provider}).all()


def summarize(rows: list) -> dict:
    """Window statistics for one (provider, gpu) from its daily rows (ordered by day). Pure."""
    tot = defaultdict(float)
    ranks: dict[str, int] = {}
    closes = []
    for r in rows:
        for k in ("hours_tracked", "hours_priced", "hours_available", "hours_market", "hours_compared",
                  "hours_cheapest", "hours_top3", "premium_sum"):
            tot[k] += getattr(r, k) or 0
        for k, n in (r.rank_counts or {}).items():
            ranks[k] = ranks.get(k, 0) + n
        closes.append((r.day, None if r.price_close is None else float(r.price_close)))

    def frac(num, den, what):
        # For the all-GPU row the counters are GPU-hours, so also require MIN_HOURS distinct hours
        # of history: 5 hours across 12 GPUs is not a day of evidence.
        if tot["hours_tracked"] < MIN_HOURS:
            return None, f"{what}: needs >= {MIN_HOURS} hours of history (have {int(tot['hours_tracked'])})"
        if tot[den] < MIN_HOURS:
            return None, f"{what}: needs >= {MIN_HOURS} hours (have {int(tot[den])})"
        return tot[num] / tot[den], None

    out, reasons = {}, {}
    out["premium_avg"], reasons["premium_avg"] = frac("premium_sum", "hours_compared", "premium")
    out["cheapest_share"], reasons["cheapest_share"] = frac("hours_cheapest", "hours_market", "cheapest share")
    out["top3_share"], reasons["top3_share"] = frac("hours_top3", "hours_market", "top-3 share")
    out["availability"], reasons["availability"] = frac("hours_priced", "hours_tracked", "availability")
    out["explicit_availability"], reasons["explicit_availability"] = frac("hours_available", "hours_tracked",
                                                                         "explicit availability")
    if tot["hours_compared"] >= MIN_HOURS and tot["hours_tracked"] >= MIN_HOURS:
        total = sum(ranks.values())
        out["rank_distribution"] = {k: ranks[k] / total for k in sorted(ranks, key=int)}
        reasons["rank_distribution"] = None
    else:
        out["rank_distribution"], reasons["rank_distribution"] = None, f"needs >= {MIN_HOURS} compared hours"
    rets = []
    for (d1, a), (d2, b) in zip(closes, closes[1:]):
        if a and b and (d2 - d1).days == 1:
            rets.append(math.log(b / a))
    if len(rets) >= MIN_RETURNS:
        out["volatility_daily"], reasons["volatility_daily"] = statistics.stdev(rets), None
    else:
        out["volatility_daily"] = None
        reasons["volatility_daily"] = f"needs >= {MIN_RETURNS} consecutive daily changes (have {len(rets)})"
    out["samples"] = {k: int(tot[k]) for k in ("hours_tracked", "hours_priced", "hours_market", "hours_compared")}
    out["samples"]["days"] = len(rows)
    out["reasons"] = {k: v for k, v in reasons.items() if v}
    return out


@ttl_cache(300)
def provider_value_all(days: int = 30, segment: str = "on_demand") -> dict[str, dict]:
    """{provider: {gpu or '*': summarize(...)}} over the window."""
    grouped: dict[tuple, list] = defaultdict(list)
    for r in _daily(segment, days):
        grouped[(r.provider, r.gpu)].append(r)
    out: dict[str, dict] = defaultdict(dict)
    for (p, g), rows in grouped.items():
        out[p][g] = summarize(rows)
    for p, by in out.items():
        vols = [v["volatility_daily"] for g, v in by.items() if g != ALL and v["volatility_daily"] is not None]
        if ALL in by:
            # Across GPUs a single closing price is meaningless: report the median of per-GPU volatilities.
            by[ALL]["volatility_daily"] = statistics.median(vols) if vols else None
            if vols:
                by[ALL]["reasons"].pop("volatility_daily", None)
            else:
                by[ALL]["reasons"]["volatility_daily"] = "no GPU of this provider has enough daily history"
    return dict(out)


def rank_history(provider: str, days: int = 30, segment: str = "on_demand", gpu: str = ALL) -> list[dict]:
    """Per day: cheapest share, top-3 share, mean rank and premium, over that day's compared hours."""
    pts = []
    for r in _daily(segment, days, provider):
        if r.gpu != gpu:
            continue
        ranks = r.rank_counts or {}
        n = sum(ranks.values())
        pts.append({"day": r.day.isoformat(), "hours_market": r.hours_market, "hours_compared": r.hours_compared,
                    "cheapest_share": r.hours_cheapest / r.hours_market if r.hours_market else None,
                    "top3_share": r.hours_top3 / r.hours_market if r.hours_market else None,
                    "mean_rank": sum(int(k) * v for k, v in ranks.items()) / n if n else None,
                    "premium_avg": r.premium_sum / r.hours_compared if r.hours_compared else None,
                    "availability": r.hours_priced / r.hours_tracked if r.hours_tracked else None})
    return pts


def listing_lifetime(provider: str | None = None) -> dict[str, dict]:
    """Median observed lifetime (last seen - first seen) of eligible listings, per provider."""
    import market

    sql = text(f"""
        SELECT c.provider, percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM c.observed_at - c.first_seen_at)) AS med,
               count(*) AS n
        FROM compute_listings c WHERE {market._ELIGIBLE} AND (CAST(:p AS text) IS NULL OR c.provider = :p)
        GROUP BY c.provider""")
    with normalize.SessionLocal() as s:
        rows = s.execute(sql, {"p": provider}).all()
    out = {}
    for r in rows:
        if r.n < MIN_LISTINGS_LIFETIME:
            out[r.provider] = {"median_hours": None, "listings": r.n,
                               "reason": f"needs >= {MIN_LISTINGS_LIFETIME} listings"}
        else:
            out[r.provider] = {"median_hours": float(r.med) / 3600, "listings": r.n, "reason": None}
    for v in out.values():
        v["label"] = ("observed lifetime: first seen to last seen by OpenGrid; listings still live are counted to "
                      "now, so this understates true lifetime")
    return out


@ttl_cache(60)
def feed_health() -> dict[str, dict]:
    """From raw_snapshots over the last 24h: fetches, failure rate, mean latency, last ok fetch."""
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    sql = text("""
        SELECT provider, count(*) AS fetches, sum(CASE WHEN ok THEN 0 ELSE 1 END) AS failures,
               avg(duration_ms) AS latency_ms, max(CASE WHEN ok THEN fetched_at END) AS last_ok,
               max(fetched_at) AS last_fetch
        FROM raw_snapshots WHERE fetched_at >= :since GROUP BY provider""")
    last_ok_sql = text("SELECT provider, max(fetched_at) AS last_ok FROM raw_snapshots WHERE ok GROUP BY provider")
    with normalize.SessionLocal() as s:
        rows = s.execute(sql, {"since": since}).all()
        ever = {r.provider: r.last_ok for r in s.execute(last_ok_sql)}
    out = {}
    for p, last in ever.items():
        out[p] = {"fetches_24h": 0, "failure_rate_24h": None, "avg_latency_ms_24h": None,
                  "last_ok_fetch": last.isoformat() if last else None, "last_fetch": None,
                  "reason": "no fetches in the last 24h"}
    for r in rows:
        out[r.provider] = {
            "fetches_24h": r.fetches, "failure_rate_24h": r.failures / r.fetches if r.fetches else None,
            "avg_latency_ms_24h": None if r.latency_ms is None else float(r.latency_ms),
            "last_ok_fetch": (r.last_ok or ever.get(r.provider)).isoformat() if (r.last_ok or ever.get(r.provider)) else None,
            "last_fetch": r.last_fetch.isoformat(), "reason": None}
    return out


def _pct(x):
    return f"{x * 100:.0f}%" if abs(x) >= 0.1 else f"{x * 100:.1f}%"


def facts(provider: str, value: dict, days: int) -> list[str]:
    """Plain sentences built only from non-null statistics."""
    name = provider_meta.meta(provider).display_name
    out = []
    for g, v in sorted(value.items()):
        if g == ALL:
            continue
        if v["cheapest_share"] is not None and v["cheapest_share"] >= 0.1:
            out.append(f"{name} has been the cheapest {g} provider {_pct(v['cheapest_share'])} of recorded hours "
                       f"in the last {days} days.")
    a = value.get(ALL)
    if a and a["premium_avg"] is not None:
        side = "above" if a["premium_avg"] > 0 else "below"
        s = f"{name} averages {abs(a['premium_avg']) * 100:.1f}% {side} the median of other providers"
        if a["availability"] is not None:
            s += f" and had priced availability {_pct(a['availability'])} of hours"
        out.append(s + f" (last {days} days).")
    return out


def beats_and_expensive(value: dict, current: dict[str, float | None], n: int = 5):
    """GPUs where the provider is cheapest vs the market and where it is dearest.

    Uses the window's average premium where it has enough hours, else the current premium (labelled).
    """
    items = []
    for g in set(value) | set(current):
        if g == ALL:
            continue
        v = value.get(g)
        if v and v["premium_avg"] is not None:
            items.append({"gpu": g, "premium": v["premium_avg"], "basis": "window_average"})
        elif current.get(g) is not None:
            items.append({"gpu": g, "premium": current[g], "basis": "current"})
    items.sort(key=lambda x: x["premium"])
    beats = [x for x in items if x["premium"] < 0][:n]
    dear = [x for x in reversed(items) if x["premium"] > 0][:n]
    return beats, dear
