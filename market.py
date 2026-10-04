"""The market view: each canonical GPU's price across providers, now and over time.

Prices come from `listing_observations`, which only records CHANGES, so a
listing's price at any moment is its last observation at or before that moment.
Everything is resampled onto one shared time grid so charts line up.

Which listings count (the same rules the leaderboard used):
    - a canonical GPU, on-demand, not interruptible
    - not Vast's "cheapest" row (one host's ask; its median row stands for Vast)
    - not a "from $x" floor price
    - in stock, or stock unknown (a sold-out listing is not a price you can pay)

Two honesty rules for history:
    - A listing counts only until it was last seen (plus a few polls). One that
      vanished never records a "gone" row, so without this its last price would
      sit in the chart forever.
    - The market lines (lowest / median / highest) start only once every provider
      currently selling the GPU was being recorded. A provider we began tracking
      later would otherwise make the market look like it dropped when it joined.
"""

from __future__ import annotations

import bisect
import statistics
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

import normalize
from providers import PROVIDERS

MIN_SPAN = timedelta(minutes=30)  # a change over less than two polls is not a trend
MAX_HOURS = 24 * 90

_ELIGIBLE = """
    c.canonical_gpu_name IS NOT NULL
    AND c.market_type = 'on_demand'
    AND COALESCE(c.interruptible, false) = false
    AND COALESCE(c.provider_tier, '') NOT IN ('cheapest', 'from_price')
"""

_LISTINGS = text(
    f"""
    SELECT c.provider, c.listing_id, c.canonical_gpu_name AS gpu, c.observed_at AS last_seen,
           c.price_per_gpu_hour AS price, c.available, c.sku, c.gpu_count, c.region
    FROM compute_listings c
    WHERE {_ELIGIBLE} AND (CAST(:gpu AS text) IS NULL OR c.canonical_gpu_name = :gpu)
    """
)

_EVENTS = text(
    f"""
    SELECT o.provider, o.listing_id, o.observed_at AS obs_at, o.price_per_gpu_hour AS price, o.available
    FROM listing_observations o
    JOIN compute_listings c ON c.provider = o.provider AND c.listing_id = o.listing_id
    WHERE {_ELIGIBLE} AND (CAST(:gpu AS text) IS NULL OR c.canonical_gpu_name = :gpu)
      AND o.observed_at <= :t1
    ORDER BY o.provider, o.listing_id, o.observed_at
    """
)


def stale_after(provider: str) -> timedelta:
    """How long since a provider's last poll before its listings count as gone."""
    cls = PROVIDERS.get(provider)
    interval = cls.polling.interval_seconds if cls else 900
    return timedelta(seconds=max(2.5 * interval, 600))


def grid(t0: datetime, t1: datetime, n: int) -> list[datetime]:
    """n+1 evenly spaced times from t0 to t1 inclusive."""
    step = (t1 - t0) / n
    return [t0 + step * i for i in range(n)] + [t1]


def price_at(times, events, last_seen, stale, t) -> float | None:
    """The price in force at `t` for one listing, or None if it was not live.

    `events` is [(time, price, available)] ascending; `times` is their times.
    """
    i = bisect.bisect_right(times, t) - 1
    if i < 0 or t > last_seen + stale:
        return None  # not yet recorded, or gone since it was last seen
    _, price, available = events[i]
    if price is None or price <= 0 or available is False:
        return None
    return float(price)


def provider_series(listings, events_by_key, t0, t1, n):
    """{gpu: {provider: [price|None per grid time]}} and {(gpu, provider): first time recorded}.

    A provider's price at a time is the lowest price among its live listings.
    """
    times = grid(t0, t1, n)
    out: dict[str, dict[str, list]] = {}
    first: dict[tuple[str, str], datetime] = {}
    for lst in listings:
        key = (lst["provider"], lst["listing_id"])
        ev = events_by_key.get(key)
        if not ev:
            continue
        ts = [e[0] for e in ev]
        stale = stale_after(lst["provider"])
        gp = (lst["gpu"], lst["provider"])
        first[gp] = min(first.get(gp, ts[0]), ts[0])
        cur = out.setdefault(lst["gpu"], {}).setdefault(lst["provider"], [None] * len(times))
        for k, t in enumerate(times):
            p = price_at(ts, ev, lst["last_seen"], stale, t)
            if p is not None and (cur[k] is None or p < cur[k]):
                cur[k] = p
    return times, out, first


def aggregate(series_by_provider: dict[str, list], start_index: int = 0):
    """Lowest, median and highest across providers at each grid time (None where nobody sells)."""
    n = len(next(iter(series_by_provider.values())))
    lo, mid, hi = [None] * n, [None] * n, [None] * n
    for k in range(start_index, n):
        prices = [s[k] for s in series_by_provider.values() if s[k] is not None]
        if prices:
            lo[k], mid[k], hi[k] = min(prices), statistics.median(prices), max(prices)
    return lo, mid, hi


def coherent_start(series_by_provider, first, gpu, times) -> int:
    """First grid index from which every provider live at the end was already recorded."""
    live_now = [p for p, s in series_by_provider.items() if s[-1] is not None]
    if not live_now:
        return len(times) - 1
    joined = max(first[(gpu, p)] for p in live_now)
    return next((k for k, t in enumerate(times) if t >= joined), len(times) - 1)


def change_over(lowest, times, start_index):
    """(percent change, since) of the lowest price from the coherent start to now, or (None, None)."""
    if lowest[-1] is None:
        return None, None
    for k in range(start_index, len(lowest)):
        if lowest[k] is not None:
            if times[-1] - times[k] < MIN_SPAN or lowest[k] <= 0:
                return None, None
            return (lowest[-1] - lowest[k]) / lowest[k], times[k]
    return None, None


def _load(gpu: str | None, hours: float):
    now = datetime.now(timezone.utc)
    with normalize.SessionLocal() as s:
        listings = [dict(r._mapping) for r in s.execute(_LISTINGS, {"gpu": gpu})]
        rows = s.execute(_EVENTS, {"gpu": gpu, "t1": now}).all()
    events: dict[tuple[str, str], list] = {}
    for r in rows:
        events.setdefault((r.provider, r.listing_id), []).append((r.obs_at, r.price, r.available))
    if hours > 0:
        t0 = now - timedelta(hours=hours)
    else:  # everything we have
        t0 = min((e[0][0] for e in events.values()), default=now - timedelta(hours=1))
    return now, t0, listings, events


def _iso(t):
    return None if t is None else t.isoformat()


def overview(hours: float = 24, points: int = 48) -> dict:
    """One row per canonical GPU with a current price, a sparkline and a change figure."""
    now, t0, listings, events = _load(None, hours)
    times, series, first = provider_series(listings, events, t0, now, points)
    gpus = []
    for gpu, by_prov in series.items():
        live = {p: s[-1] for p, s in by_prov.items() if s[-1] is not None}
        if not live:
            continue
        start = coherent_start(by_prov, first, gpu, times)
        lo, mid, hi = aggregate(by_prov, start)
        pct, since = change_over(lo, times, start)
        low_p = min(live, key=live.get)
        high_p = max(live, key=live.get)
        gpus.append({
            "gpu": gpu, "providers": len(live),
            "lowest": live[low_p], "lowest_provider": low_p,
            "median": statistics.median(live.values()),
            "highest": live[high_p], "highest_provider": high_p,
            "change_pct": pct, "change_since": _iso(since),
            "spark": lo,                      # null before every provider was recorded
        })
    gpus.sort(key=lambda g: (-g["providers"], g["gpu"]))
    return {"hours": hours, "t0": _iso(t0), "t1": _iso(now), "times": [_iso(t) for t in times], "gpus": gpus}


def detail(gpu: str, hours: float = 24, points: int = 120) -> dict:
    """One GPU: every provider's price over time, the market lines, and who sells it now."""
    now, t0, listings, events = _load(gpu, hours)
    times, series, first = provider_series(listings, events, t0, now, points)
    by_prov = series.get(gpu, {})
    if not by_prov:
        return {"gpu": gpu, "hours": hours, "times": [_iso(t) for t in times], "providers": [], "lowest": [], "median": [], "highest": []}
    start = coherent_start(by_prov, first, gpu, times)
    lo, mid, hi = aggregate(by_prov, start)
    pct, since = change_over(lo, times, start)

    # The listing each provider is selling at its current lowest price.
    best: dict[str, dict] = {}
    for lst in listings:
        if lst["gpu"] != gpu or lst["price"] is None or lst["available"] is False:
            continue
        if now - lst["last_seen"] > stale_after(lst["provider"]):
            continue
        cur = best.get(lst["provider"])
        if cur is None or lst["price"] < cur["price"]:
            best[lst["provider"]] = lst
    providers = []
    for p, s in by_prov.items():
        b = best.get(p)
        providers.append({
            "provider": p, "series": s, "now": s[-1],
            "first_seen": _iso(first[(gpu, p)]),
            "sku": b and b["sku"], "gpu_count": b and b["gpu_count"], "region": b and b["region"],
            "available": b and b["available"],
            "change_pct": change_over(s, times, 0)[0],
        })
    providers.sort(key=lambda x: (x["now"] is None, x["now"] if x["now"] is not None else 0, x["provider"]))
    return {
        "gpu": gpu, "hours": hours, "t0": _iso(t0), "t1": _iso(now),
        "times": [_iso(t) for t in times], "providers": providers,
        "lowest": lo, "median": mid, "highest": hi,
        "market_from": _iso(times[start]), "change_pct": pct, "change_since": _iso(since),
    }
