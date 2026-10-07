"""Metric resolvers: what an alert rule measures.

Each resolver takes the rule's params and a per-evaluation Context and returns a Reading:
a float value with a short detail, or value None meaning UNKNOWN (with the reason). An unknown
reading never fires a rule: thin history, a missing module or a provider we are not recording
is reported as unknown, never guessed.

    market_low                {"gpu", "op", "value"}  lowest observed on-demand price now (USD/GPU-h)
    market_median             {"gpu", "op", "value"}  median of each provider's lowest price now
    available                 {"gpu", "provider"?, "op"?=">", "value"?=0}  listings explicitly available now
    provider_price            {"gpu", "provider", "op", "value"}  that provider's lowest price now
    provider_price_change_pct {"gpu", "provider", "window_hours", "op", "value"}  % change of its lowest price
    region_availability_change_pct {"gpu", "region_group", "window_hours", "op", "value"}  % change in
                              available listings in a region group (hourly rollup)
    index_level               {"index_id", "op", "value"}  published index level (analytics.indices)
    index_new_low             {"index_id", "window": "30d"}  1 when the latest published level is the lowest
                              of the window, else 0 (op/value default "==", 1)

"Now" uses market.py's rules exactly: eligible listings (canonical, on-demand, not interruptible,
not Vast 'cheapest', not 'from' prices), live only until last seen + stale_after, sold-out excluded
from prices. Short price-change windows (<= 48 h) replay listing_observations for one provider and
GPU; longer windows read the hourly rollup.
"""

from __future__ import annotations

import operator
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

import market
import normalize

OPS = {"<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge, "==": operator.eq, "!=": operator.ne}


@dataclass
class Reading:
    value: float | None
    detail: str = ""

    @property
    def unknown(self) -> bool:
        return self.value is None


@dataclass
class Context:
    """Shared across all rules in one evaluation run, so the current market is read once."""
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    _current: list | None = None

    def current(self) -> list[dict]:
        if self._current is None:
            sql = text(f"""SELECT c.provider, c.listing_id, c.canonical_gpu_name AS gpu, c.price_per_gpu_hour AS price,
                                  c.available, c.observed_at AS last_seen, c.region, c.country
                           FROM compute_listings c WHERE {market._ELIGIBLE}""")
            with normalize.SessionLocal() as s:
                rows = [dict(r._mapping) for r in s.execute(sql)]
            self._current = [r for r in rows if self.now <= r["last_seen"] + market.stale_after(r["provider"])]
        return self._current


def _provider_lows(ctx: Context, gpu: str) -> dict[str, float]:
    lows: dict[str, float] = {}
    for r in ctx.current():
        if r["gpu"] != gpu or r["price"] is None or r["price"] <= 0 or r["available"] is False:
            continue
        p = float(r["price"])
        if r["provider"] not in lows or p < lows[r["provider"]]:
            lows[r["provider"]] = p
    return lows


def _need(params: dict, *keys):
    missing = [k for k in keys if not params.get(k)]
    if missing:
        raise ValueError(f"params need {missing}")


def market_low(params, ctx):
    _need(params, "gpu")
    lows = _provider_lows(ctx, params["gpu"])
    if not lows:
        return Reading(None, f"no live priced listing for {params['gpu']}")
    p = min(lows, key=lows.get)
    return Reading(lows[p], f"lowest {lows[p]:.4f} at {p} across {len(lows)} providers")


def market_median(params, ctx):
    _need(params, "gpu")
    lows = _provider_lows(ctx, params["gpu"])
    if not lows:
        return Reading(None, f"no live priced listing for {params['gpu']}")
    return Reading(float(statistics.median(lows.values())), f"median of {len(lows)} providers' lowest prices")


def provider_price(params, ctx):
    _need(params, "gpu", "provider")
    lows = _provider_lows(ctx, params["gpu"])
    if params["provider"] not in lows:
        return Reading(None, f"{params['provider']} has no live priced {params['gpu']} listing")
    return Reading(lows[params["provider"]], f"{params['provider']} lowest now")


def available(params, ctx):
    _need(params, "gpu")
    rows = [r for r in ctx.current() if r["gpu"] == params["gpu"]
            and (not params.get("provider") or r["provider"] == params["provider"])]
    if not rows:
        return Reading(None, "no live listing (cannot tell sold out from not recorded)")
    known = [r for r in rows if r["available"] is not None]
    if not known:
        return Reading(None, "availability not reported by these listings")
    n = sum(1 for r in known if r["available"] is True)
    return Reading(float(n), f"{n} of {len(rows)} live listings explicitly available ({len(rows) - len(known)} unknown)")


def _provider_low_at(gpu: str, provider: str, t: datetime, now: datetime) -> float | None | str:
    """That provider's lowest eligible price in force at t, by market.price_at; 'unrecorded' before coverage."""
    sql = text(f"""
        SELECT c.listing_id, c.observed_at AS last_seen, o.observed_at AS obs_at, o.price_per_gpu_hour AS price, o.available
        FROM listing_observations o JOIN compute_listings c ON c.provider = o.provider AND c.listing_id = o.listing_id
        WHERE {market._ELIGIBLE} AND c.provider = :p AND c.canonical_gpu_name = :g AND o.observed_at <= :t
        ORDER BY c.listing_id, o.observed_at""")
    with normalize.SessionLocal() as s:
        rows = s.execute(sql, {"p": provider, "g": gpu, "t": t}).all()
    if not rows:
        return "unrecorded"
    by: dict[str, list] = {}
    seen: dict[str, datetime] = {}
    for r in rows:
        by.setdefault(r.listing_id, []).append((r.obs_at, r.price, r.available))
        seen[r.listing_id] = r.last_seen
    stale = market.stale_after(provider)
    best = None
    for lid, ev in by.items():
        p = market.price_at([e[0] for e in ev], ev, seen[lid], stale, t)
        if p is not None and (best is None or p < best):
            best = p
    return best


def provider_price_change_pct(params, ctx):
    _need(params, "gpu", "provider")
    hours = float(params.get("window_hours", 24))
    if hours <= 0:
        raise ValueError("window_hours must be > 0")
    now_r = provider_price(params, ctx)
    if now_r.unknown:
        return now_r
    t0 = ctx.now - timedelta(hours=hours)
    if hours <= 48:
        then = _provider_low_at(params["gpu"], params["provider"], t0, ctx.now)
    else:
        from analytics import rollups

        first = rollups.first_hours().get((params["gpu"], params["provider"]))
        if first is None or first > t0:
            then = "unrecorded"
        else:
            h = rollups.floor_hour(t0)
            rows = rollups.provider_hourly(gpu=params["gpu"], provider=params["provider"], t0=h, t1=h)
            then = rows[0]["min_price"] if rows else None
    if then == "unrecorded":
        return Reading(None, f"insufficient coverage: {params['provider']} not recorded {hours:g}h ago")
    if then is None or then <= 0:
        return Reading(None, f"no live priced listing {hours:g}h ago")
    pct = (now_r.value - then) / then * 100
    return Reading(pct, f"{then:.4f} -> {now_r.value:.4f} over {hours:g}h")


def region_availability_change_pct(params, ctx):
    _need(params, "gpu", "region_group")
    try:
        from regions import region_group
    except ImportError:
        return Reading(None, "region grouping unavailable")
    from analytics import rollups

    hours = float(params.get("window_hours", 24))
    h1 = rollups.floor_hour(ctx.now)
    h0 = rollups.floor_hour(ctx.now - timedelta(hours=hours))

    def avail_at(h):
        rows = rollups.regional_hourly(gpu=params["gpu"], t0=h, t1=h)
        rows = [r for r in rows if region_group(r["provider"], r["region"] or None, r["country"]) == params["region_group"]]
        return None if not rows else sum(r["available_listings"] for r in rows)

    now_n = avail_at(h1) if avail_at(h1) is not None else avail_at(h1 - timedelta(hours=1))
    then_n = avail_at(h0)
    if now_n is None or then_n is None:
        return Reading(None, "insufficient coverage in the hourly rollup for this region group")
    if then_n == 0:
        return Reading(None, "no available listings at the window start (% change undefined)")
    return Reading((now_n - then_n) / then_n * 100, f"{then_n} -> {now_n} available listings over {hours:g}h")


def _indices():
    try:
        from analytics import indices
        return indices
    except Exception:
        return None


def index_level(params, ctx):
    _need(params, "index_id")
    ix = _indices()
    if ix is None:
        return Reading(None, "indices unavailable")
    try:
        lvl = ix.index_level(params["index_id"])
    except Exception as e:
        return Reading(None, f"index error: {type(e).__name__}")
    if not lvl or not lvl.get("published") or not isinstance(lvl.get("level"), (int, float)):
        return Reading(None, (lvl or {}).get("reason") or "index not published")
    return Reading(float(lvl["level"]), f"{params['index_id']} level")


def index_new_low(params, ctx):
    now_r = index_level(params, ctx)
    if now_r.unknown:
        return now_r
    window = str(params.get("window", "30d"))
    try:
        days = float(window.rstrip("d"))
    except ValueError:
        raise ValueError("window must look like '30d'")
    try:
        hist = _indices().index_history(params["index_id"], ctx.now - timedelta(days=days), ctx.now, resolution="1h")
    except Exception as e:
        return Reading(None, f"index history error: {type(e).__name__}")
    levels = [h["level"] for h in hist if h.get("level") is not None and h.get("published", True)]
    if len(levels) < max(24, days * 24 * 0.5):  # need at least half the window's hours
        return Reading(None, f"insufficient history for a {window} low ({len(levels)} published hours)")
    prior = min(levels[:-1])
    return Reading(1.0 if now_r.value < prior else 0.0, f"level {now_r.value:.4f} vs prior {window} low {prior:.4f}")


REGISTRY = {
    "market_low": market_low,
    "market_median": market_median,
    "provider_price": provider_price,
    "available": available,
    "provider_price_change_pct": provider_price_change_pct,
    "region_availability_change_pct": region_availability_change_pct,
    "index_level": index_level,
    "index_new_low": index_new_low,
}
DEFAULT_CONDITION = {"available": (">", 0.0), "index_new_low": ("==", 1.0)}


def condition(params: dict) -> tuple[str, float]:
    metric = params.get("metric")
    op, value = DEFAULT_CONDITION.get(metric, (None, None))
    op = params.get("op", op)
    value = params.get("value", value)
    if op not in OPS or value is None:
        raise ValueError(f"params need op in {list(OPS)} and a numeric value")
    return op, float(value)


def validate(params: dict) -> dict:
    if not isinstance(params, dict) or params.get("metric") not in REGISTRY:
        raise ValueError(f"params.metric must be one of {sorted(REGISTRY)}")
    condition(params)
    return params


def resolve(params: dict, ctx: Context | None = None) -> Reading:
    fn = REGISTRY.get(params.get("metric"))
    if fn is None:
        return Reading(None, f"unknown metric {params.get('metric')!r}")
    try:
        return fn(params, ctx or Context())
    except ValueError as e:
        return Reading(None, f"invalid params: {e}")
