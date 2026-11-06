"""Cross-analysis of power (and gas) beside GPU prices: DERIVED tables, never inputs to any GPU statistic.

    refresh(now=None)       rebuild the last WINDOW_DAYS of power_gpu_region_daily and today's power_leadlag
    regional(...)           admin read of power_gpu_region_daily
    leadlag(...)            admin read of power_leadlag

power_gpu_region_daily, per (day, provider, region, GPU) where the region maps to a price series (power/geo.py):
    power cost per GPU-hour = TDP (hardware.py tdp_w; a GPU with no published TDP is skipped) / 1000 x PUE
                              (settings.power_pue, default 1.3) x the day's mean day-ahead price ($/MWh) / 1000
    spread                  = that day's median listed price there (USD per GPU-hour) - power cost
    power_share             = power cost / median price
  GPU side: PUBLIC on-demand listings only (visibility "public", market_type "on_demand"), each listing's price as
  of the end of the day (observations are change-only, so the last one on or before it), counted on the days the
  listing was live (first seen .. last seen). Board power x PUE is the facility draw of one GPU at full load; it is
  NOT the provider's cost: hosts buy power on tariffs and contracts, not at the hub price, and idle GPUs draw less.

power_leadlag, per (GPU, ISO, price series, metric, lag) over the last LEADLAG_DAYS:
    gpu_price_median     corr(day-on-day change of the power price on day t, change of the GPU's median price
                         across that series' regions on day t + lag)
    capacity_available   the same with the share of those listings in stock
    gpu_price_vs_henry_hub / power_price_vs_henry_hub   Henry Hub spot (carried over weekends) as the slow input,
                         against the GPU median price / the power price itself (gpu "*"), at GAS_LAGS
  Pearson correlation of first differences (levels, not logs: power prices go negative), n >= MIN_N pairs, else
  null. lag > 0 means the GPU (or power) series is read AFTER the input. spike_days counts days whose power price
  exceeded SPIKE_X x its trailing 30-day median. A CORRELATION, NEVER A CAUSE (RULE): a lagged co-movement over a
  few months of daily data says nothing about why prices moved.

Visibility: every row is "internal" unless every input is public. GPU inputs are public listings; the power side is
the price series' source tier (ISO prices are internal; Henry Hub, eia_gas, is internal by choice), so today every
row is internal and is read only by admins.
"""

from __future__ import annotations

import bisect
import logging
import math
import statistics
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone

from power import geo, reads, registry, storage

log = logging.getLogger(__name__)

UTC = timezone.utc
WINDOW_DAYS = 120
LEADLAG_DAYS = 180
LAGS = tuple(range(-7, 8))
GAS_LAGS = (-14, -7, 0, 7, 14, 21, 30)
MIN_N = 20
SPIKE_X = 2.0
HENRY_HUB = "eia_gas.spot.spot_price.henry_hub"
RULE = ("Correlation, never cause: these figures put power and gas prices beside GPU prices and capacity. A lagged "
        "co-movement does not mean one moved the other. Power cost is board power x PUE at the hub price, not any "
        "provider's actual cost. INTERNAL: admin-only.")
METHOD = (f"Pearson correlation of day-on-day changes, x = input on day t, y = series on day t+lag; n >= {MIN_N}; "
          f"spike = daily mean > {SPIKE_X} x trailing 30-day median")


def _session():
    return storage._session()


def pue() -> float:
    from config import settings

    return float(settings.power_pue or 1.3)


def tdp(gpu: str) -> int | None:
    import hardware

    s = hardware.spec(gpu) or {}
    v = s.get("tdp_w")
    return int(v) if isinstance(v, (int, float)) and v > 0 else None


def cost_per_gpu_hour(tdp_w: float, price_usd_mwh: float, pue_: float) -> float:
    return tdp_w / 1000.0 * pue_ * price_usd_mwh / 1000.0


def _series_visibility(key: str) -> str:
    idx = registry.series_index()
    return registry.visibility(idx[key][0].source) if key in idx else registry.INTERNAL


# --------------------------------------------------------------------------- GPU side (public listings only)

def gpu_days(start: date, end: date) -> tuple[dict, dict]:
    """{(day, provider, region, gpu): {"prices": [...], "avail": [...]}} and {(provider, region): locate()}."""
    from sqlalchemy import text

    with _session()() as s:
        listings = s.execute(text("""
            SELECT provider, listing_id, region, country, canonical_gpu_name AS gpu, first_seen_at, observed_at
            FROM compute_listings WHERE visibility = 'public' AND canonical_gpu_name IS NOT NULL
              AND market_type = 'on_demand' AND region IS NOT NULL""")).all()
        places: dict[tuple[str, str], dict | None] = {}
        keep = []
        for r in listings:
            k = (r.provider, r.region)
            if k not in places:
                places[k] = geo.locate(r.provider, r.region, r.country)
            if places[k] and tdp(r.gpu):
                keep.append(r)
        obs: dict[tuple[str, str], list] = defaultdict(list)
        if keep:
            ids = [r.listing_id for r in keep]
            for i in range(0, len(ids), 2000):
                for o in s.execute(text("""
                        SELECT provider, listing_id, observed_at, price_per_gpu_hour, available
                        FROM listing_observations WHERE listing_id = ANY(:ids) AND observed_at <= :end
                        ORDER BY observed_at"""), {"ids": ids[i:i + 2000],
                                                   "end": datetime.combine(end, time.max, tzinfo=UTC)}):
                    obs[(o.provider, o.listing_id)].append(o)
    out: dict[tuple, dict] = defaultdict(lambda: {"prices": [], "avail": []})
    for r in keep:
        series = obs.get((r.provider, r.listing_id))
        if not series:
            continue
        times = [o.observed_at for o in series]
        d = max(start, r.first_seen_at.astimezone(UTC).date())
        last = min(end, r.observed_at.astimezone(UTC).date())
        while d <= last:
            i = bisect.bisect_right(times, datetime.combine(d, time.max, tzinfo=UTC)) - 1
            if i >= 0:
                o = series[i]
                g = out[(d, r.provider, r.region, r.gpu)]
                if o.price_per_gpu_hour is not None and o.price_per_gpu_hour > 0:
                    g["prices"].append(float(o.price_per_gpu_hour))
                if o.available is not None:
                    g["avail"].append(1.0 if o.available else 0.0)
            d += timedelta(days=1)
    return dict(out), {k: v for k, v in places.items() if v}


# --------------------------------------------------------------------------- correlation

def _diffs(series: dict[date, float]) -> dict[date, float]:
    out = {}
    for d, v in series.items():
        p = series.get(d - timedelta(days=1))
        if p is not None:
            out[d] = v - p
    return out


def pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < MIN_N:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


def lagged(x: dict[date, float], y: dict[date, float], lag: int) -> tuple[float | None, int]:
    dx, dy = _diffs(x), _diffs(y)
    pairs = [(v, dy[d + timedelta(days=lag)]) for d, v in dx.items() if d + timedelta(days=lag) in dy]
    if not pairs:
        return None, 0
    xs, ys = zip(*pairs)
    return pearson(list(xs), list(ys)), len(pairs)


def spikes(x: dict[date, float]) -> int:
    days = sorted(x)
    n = 0
    for i, d in enumerate(days):
        prior = [x[p] for p in days[max(0, i - 30):i] if (d - p).days <= 30]
        if len(prior) >= 10:
            med = statistics.median(prior)
            if med > 0 and x[d] > SPIKE_X * med:
                n += 1
    return n


def carried(x: dict[date, float], start: date, end: date) -> dict[date, float]:
    """A daily-on-trading-days series carried over weekends and holidays (at most 5 days)."""
    out, last, age = {}, None, 0
    d = start - timedelta(days=7)
    while d <= end:
        if d in x:
            last, age = x[d], 0
        elif last is not None:
            age += 1
        if last is not None and age <= 5 and d >= start:
            out[d] = last
        d += timedelta(days=1)
    return out


# --------------------------------------------------------------------------- refresh

def refresh(now: datetime | None = None) -> dict:
    from sqlalchemy import delete, text
    from sqlalchemy.dialects.postgresql import insert

    from store.power import PowerGpuRegionDaily as R
    from store.power import PowerLeadLag as L

    now = now or datetime.now(UTC)
    end = now.astimezone(UTC).date()
    start = end - timedelta(days=max(WINDOW_DAYS, LEADLAG_DAYS))
    groups, places = gpu_days(start, end)
    pue_ = pue()
    price_keys = sorted({p["price_series"] for p in places.values()})
    power = {k: reads.daily_means(k, start - timedelta(days=40), end) for k in price_keys}
    rows = []
    for (d, prov, region, gpu), g in groups.items():
        p = places[(prov, region)]
        pm = power.get(p["price_series"], {}).get(d)
        if pm is None or d < end - timedelta(days=WINDOW_DAYS):
            continue
        t = tdp(gpu)
        cost = cost_per_gpu_hour(t, pm[0], pue_)
        med = statistics.median(g["prices"]) if g["prices"] else None
        rows.append({"day": d, "provider": prov, "region": region[:128], "gpu": gpu, "state": p["state"],
                     "iso": p["iso"], "price_series": p["price_series"], "match": p["match"],
                     "power_price_usd_mwh": pm[0], "power_price_max_usd_mwh": pm[1], "tdp_w": t, "pue": pue_,
                     "power_cost_usd_per_gpu_hour": cost, "gpu_price_median": med,
                     "gpu_price_low": min(g["prices"]) if g["prices"] else None, "listings": len(g["prices"]),
                     "spread_usd_per_gpu_hour": None if med is None else med - cost,
                     "power_share": None if not med else cost / med,
                     "visibility": _series_visibility(p["price_series"]), "computed_at": now})
    with _session().begin() as s:
        s.execute(delete(R).where(R.day >= end - timedelta(days=WINDOW_DAYS)))
        for i in range(0, len(rows), 2000):
            s.execute(insert(R).values(rows[i:i + 2000]).on_conflict_do_nothing())

    # ---- lead / lag
    ll_start = end - timedelta(days=LEADLAG_DAYS)
    by_series: dict[tuple[str, str, str], dict[date, list]] = defaultdict(lambda: defaultdict(list))
    avail: dict[tuple[str, str, str], dict[date, list]] = defaultdict(lambda: defaultdict(list))
    for (d, prov, region, gpu), g in groups.items():
        if d < ll_start:
            continue
        p = places[(prov, region)]
        k = (gpu, p["iso"], p["price_series"])
        if g["prices"]:
            by_series[k][d].append(statistics.median(g["prices"]))
        if g["avail"]:
            avail[k][d].append(sum(g["avail"]) / len(g["avail"]))
    hh_raw = reads.daily_means(HENRY_HUB, ll_start - timedelta(days=10), end)
    hh = carried({d: v[0] for d, v in hh_raw.items()}, ll_start, end)
    hh_vis = _series_visibility(HENRY_HUB)
    out_rows = []

    def add(gpu, iso, key, metric, x, y, lags, vis, spike_n):
        for lag in lags:
            c, n = lagged(x, y, lag)
            out_rows.append({"gpu": gpu, "iso": iso, "price_series": key, "metric": metric, "lag_days": lag,
                             "window_start": ll_start, "window_end": end, "n": n,
                             "corr": None if c is None else round(c, 6), "spike_days": spike_n, "visibility": vis,
                             "computed_at": now, "method": METHOD})

    for key in price_keys:
        x = {d: v[0] for d, v in power[key].items() if d >= ll_start}
        if not x:
            continue
        iso = next((p["iso"] for p in places.values() if p["price_series"] == key), "")
        vis = _series_visibility(key)
        sp = spikes(x)
        for (gpu, i_iso, k), days in by_series.items():
            if k != key:
                continue
            y = {d: statistics.mean(v) for d, v in days.items()}
            add(gpu, iso, key, "gpu_price_median", x, y, LAGS, vis, sp)
            if hh:
                add(gpu, iso, HENRY_HUB, "gpu_price_vs_henry_hub", hh, y, GAS_LAGS,
                    "public" if hh_vis == vis == "public" else "internal", 0)
        for (gpu, i_iso, k), days in avail.items():
            if k == key:
                add(gpu, iso, key, "capacity_available", x, {d: statistics.mean(v) for d, v in days.items()}, LAGS,
                    vis, sp)
        if hh:
            add("*", iso, key, "power_price_vs_henry_hub", hh, x, GAS_LAGS,
                "public" if hh_vis == vis == "public" else "internal", sp)
    with _session().begin() as s:
        s.execute(delete(L).where(L.window_end == end))
        for i in range(0, len(out_rows), 2000):
            s.execute(insert(L).values(out_rows[i:i + 2000]).on_conflict_do_nothing())
        # retention: the last 35 daily windows, then one per month (window_end on the 1st)
        s.execute(text("""DELETE FROM power_leadlag WHERE window_end < :old AND extract(day FROM window_end) <> 1"""),
                  {"old": end - timedelta(days=35)})
    return {"regional_rows": len(rows), "leadlag_rows": len(out_rows), "regions_mapped": len(places),
            "price_series": price_keys}


# --------------------------------------------------------------------------- reads (admin only)

def regional(gpu: str | None = None, provider: str | None = None, iso: str | None = None,
             since: date | None = None, limit: int = 2000) -> list[dict]:
    from sqlalchemy import text

    with _session()() as s:
        rows = s.execute(text("""
            SELECT * FROM power_gpu_region_daily
            WHERE (CAST(:g AS text) IS NULL OR gpu = :g) AND (CAST(:p AS text) IS NULL OR provider = :p)
              AND (CAST(:i AS text) IS NULL OR iso = :i) AND (CAST(:a AS date) IS NULL OR day >= :a)
            ORDER BY day DESC, provider, region, gpu LIMIT :n"""),
                         {"g": gpu, "p": provider, "i": iso, "a": since, "n": limit}).all()
    return [dict(r._mapping) for r in rows]


def leadlag(gpu: str | None = None, iso: str | None = None, metric: str | None = None) -> list[dict]:
    """The latest window's rows."""
    from sqlalchemy import text

    with _session()() as s:
        rows = s.execute(text("""
            SELECT gpu, iso, price_series, metric, lag_days, window_start, window_end, n, corr, spike_days,
                   visibility, computed_at, method FROM power_leadlag
            WHERE window_end = (SELECT max(window_end) FROM power_leadlag)
              AND (CAST(:g AS text) IS NULL OR gpu = :g) AND (CAST(:i AS text) IS NULL OR iso = :i)
              AND (CAST(:m AS text) IS NULL OR metric = :m)
            ORDER BY gpu, iso, metric, lag_days"""), {"g": gpu, "i": iso, "m": metric}).all()
    return [dict(r._mapping) for r in rows]


# --------------------------------------------------------------------------- shifts (related, not causal)

def spike_days(key: str, start: date, end: date) -> list[dict]:
    """Days in start..end whose daily mean exceeded SPIKE_X x the trailing 30-day median (>= 10 prior days)."""
    x = reads.daily_means(key, start - timedelta(days=31), end)
    days = sorted(x)
    out = []
    for i, d in enumerate(days):
        if d < start:
            continue
        prior = [x[p][0] for p in days[:i] if 0 < (d - p).days <= 30]
        if len(prior) < 10:
            continue
        med = statistics.median(prior)
        if med > 0 and x[d][0] > SPIKE_X * med:
            out.append({"series": key, "day": d, "mean": x[d][0], "max": x[d][1], "trailing_median": med,
                        "ratio": round(x[d][0] / med, 3)})
    return out


def shift_power(limit: int = 50, gpu: str | None = None) -> list[dict]:
    """INTERNAL. For the latest major shifts (analytics/shifts.py), the power-price spikes in each shift's own
    related-items window, joined at READ time: nothing is written to market_shifts / market_shift_items, so the
    shifts and their public items are exactly what they were. Each spike says how it relates: "gpu_region" (the
    series prices a region where the shift's GPU is listed), "provider_region" (one of the shift provider's
    regions), else "market_wide". Related in time, not causal (analytics/shifts.NOT_CAUSAL)."""
    from analytics import shifts

    rows, _ = shifts.query(gpu=gpu, limit=limit, top_n=0)
    if not rows:
        return []
    _, places = gpu_days(date.today(), date.today())   # mapped regions of current public listings
    by_gpu: dict[str, set] = defaultdict(set)
    by_provider: dict[str, set] = defaultdict(set)
    from sqlalchemy import text

    with _session()() as s:
        for r in s.execute(text("""SELECT DISTINCT provider, region, canonical_gpu_name FROM compute_listings
                                   WHERE visibility = 'public' AND region IS NOT NULL""")):
            p = places.get((r.provider, r.region))
            if p:
                by_gpu[r.canonical_gpu_name].add(p["price_series"])
                by_provider[r.provider].add(p["price_series"])
    keys = sorted({p["price_series"] for p in places.values()} |
                  {s.key for d in registry.datasets() for s in d.series if s.market == "da" and s.kind == "lmp"})
    out = []
    for sh in rows:
        a, b = sh["window_start"].astimezone(UTC).date(), sh["window_end"].astimezone(UTC).date()
        items = []
        for k in keys:
            for sp in spike_days(k, a, b):
                match = ("gpu_region" if k in by_gpu.get(sh.get("gpu") or "", ()) else
                         "provider_region" if k in by_provider.get(sh.get("provider") or "", ()) else "market_wide")
                items.append({**sp, "match": match, "visibility": _series_visibility(k)})
        items.sort(key=lambda i: ({"gpu_region": 0, "provider_region": 1}.get(i["match"], 2), -i["ratio"]))
        out.append({"shift": {k: sh.get(k) for k in ("id", "type", "gpu", "provider", "at", "window_start",
                                                    "window_end", "direction", "magnitude", "severity", "title")},
                    "heading": shifts.NOT_CAUSAL, "power_spikes": items})
    return out
