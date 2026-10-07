"""The opportunity monitor: where a buyer might get a better deal right now. Computed live, not stored.

Each item: type, score (0-100, higher = more unusual / larger), a one-sentence
explanation, the numbers behind it, the data kind (observed vs inferred), and
links (gpu slug, provider, the events feed). Methodology: methodology/opportunities.md.

These are observations about listed prices, not advice and not quotes: a listing
can sell out or change before anyone acts, and nothing here says why a price moved.
Coverage rules follow analytics/events.py: comparisons over time use providers
recorded at both times, and providers within their first day of coverage are not
used for market-level figures.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

import market
import normalize
import provider_meta
from analytics import events, movers, rollups
from api.common import gpu_slug
from cache import ttl_cache

SEGMENT = "on_demand"
HOUR, DAY = timedelta(hours=1), timedelta(days=1)

SPREAD_BASELINE_POINTS = 24 * 7
SPREAD_NO_BASELINE = 1.0     # 100%: flagged without history only when this wide
SPREAD_OVER_P90 = 1.10       # 10% wider than its own 30-day p90 (a flat history makes p90 = today)
CUT_MIN = 0.05               # price cut vs 24h ago
NEW_INVENTORY_HOURS = 24
PREMIUM_MIN_POINTS = 24 * 7
PREMIUM_DELTA = 0.10         # 10 points below its usual premium/discount
SCARCITY_RATIO = 0.5
SCARCITY_MIN_NORM = 2.0
SCARCITY_MIN_POINTS = 24 * 7
SUPPLY_PROVIDERS = 2
SUPPLY_LISTINGS = 0.5

TYPES = {
    "wide_spread": ("inferred", "Provider spread (highest/lowest - 1) more than 10% above that GPU's own 30-day p90; "
                    "without 7 days of history, only spreads >= 100%."),
    "price_cut": ("observed", "A provider's lowest price for a GPU is >= 5% below 24h ago."),
    "new_cheap_inventory": ("observed", "Listings first seen in the last 24h, purchasable, priced below the GPU's "
                            "current market median; excludes providers in their first day of coverage."),
    "regional_dislocation": ("inferred", "A region group's median provider price >= 20% away from the global median."),
    "below_usual_premium": ("inferred", "A provider's current price vs the market median is >= 10 points below its own "
                            "30-day average premium; needs 7 days of samples."),
    "scarcity": ("inferred", "Available listings <= 50% of the GPU's 30-day hourly average (average >= 2); needs "
                 "7 days of samples."),
    "new_cheapest_provider": ("observed", "A provider became the cheapest for a GPU in the last 24h and still is."),
    "supply_change": ("observed", "Providers with purchasable listings changed by >= 2, or available listings by "
                      ">= 50%, vs 24h ago (providers recorded at both times)."),
}


def _usd(x):
    return events._usd(x)


def _pctf(x):
    return f"{abs(x) * 100:.0f}%"


def _name(p):
    return provider_meta.meta(p).display_name


def item(type, score, explanation, numbers, *, gpu=None, provider=None, region_group=None) -> dict:
    slug = gpu_slug(gpu) if gpu else None
    links = {}
    if slug:
        links["events"] = f"/v1/events?gpu={slug}"
    if provider:
        links["provider_events"] = f"/v1/events?provider={provider}"
    return {"type": type, "score": round(max(0.0, min(100.0, score)), 1), "explanation": explanation,
            "numbers": numbers, "gpu": gpu, "gpu_slug": slug, "provider": provider,
            "provider_name": _name(provider) if provider else None, "region_group": region_group,
            "kind": TYPES[type][0], "links": links}


def _usual_premiums(s, segment, t0, t1) -> dict:
    """{(gpu, provider): (average premium vs market median, samples)} over [t0, t1)."""
    rows = s.execute(text("""
        WITH p AS (
            SELECT gpu, provider, hour, min(min_price) AS price FROM market_hourly
            WHERE segment = :s AND hour >= :t0 AND hour < :t1 AND min_price IS NOT NULL AND priced_listings > 0
            GROUP BY gpu, provider, hour)
        SELECT p.gpu, p.provider, avg(p.price / g.median - 1) AS prem, count(*) AS n
        FROM p JOIN market_gpu_hourly g ON g.segment = :s AND g.gpu = p.gpu AND g.hour = p.hour
        WHERE g.median > 0 AND g.providers_priced >= 2
        GROUP BY p.gpu, p.provider
    """), {"s": segment, "t0": t0, "t1": t1}).all()
    return {(r.gpu, r.provider): (float(r.prem), int(r.n)) for r in rows}


@ttl_cache(60)
def monitor(segment: str = SEGMENT) -> dict:
    H = movers.latest_hour(segment)
    if H is None:
        return {"as_of_hour": None, "items": [], "unavailable": [
            {"type": t, "reason": "no market history yet"} for t in TYPES]}
    now_t = datetime.now(timezone.utc)
    with normalize.SessionLocal() as s:
        ctx = events.load_context(s, segment)
        usual = _usual_premiums(s, segment, H - 30 * DAY, H)
        fresh = s.execute(text(f"""
            SELECT c.provider, c.listing_id, c.canonical_gpu_name AS gpu, c.sku, c.region, c.gpu_count,
                   c.price_per_gpu_hour AS price, c.available, c.first_seen_at, c.observed_at
            FROM compute_listings c
            WHERE {market._ELIGIBLE} AND c.first_seen_at >= :t0 AND c.price_per_gpu_hour > 0
              AND COALESCE(c.available, true)
        """), {"t0": now_t - timedelta(hours=NEW_INVENTORY_HOURS)}).all()
    table = movers._table(segment, H - 30 * DAY, H)
    now_rows = movers._by_gpu(rollups.provider_hourly(segment, None, None, H, H))
    then_rows = movers._by_gpu(rollups.provider_hourly(segment, None, None, H - DAY, H - DAY))
    items, unavailable = [], []

    # Current market over established providers.
    cur: dict[str, dict] = {}
    for g, prov in now_rows.items():
        cur[g] = {p: r["min_price"] for p, r in prov.items() if movers._priced(r) and ctx.established(g, p, H)}

    # 1. wide spreads vs own history.
    for g, prices in cur.items():
        if len(prices) < events.SPREAD_MIN_PROVIDERS:
            continue
        lo_p, hi_p = min(prices, key=prices.get), max(prices, key=prices.get)
        spread = prices[hi_p] / prices[lo_p] - 1
        med = statistics.median(prices.values())
        hist = sorted(r["highest"] / r["lowest"] - 1 for r in table.get(g, []) if r["hour"] < H and r["lowest"] and r["highest"])
        nums = {"spread": spread, "lowest": prices[lo_p], "lowest_provider": lo_p, "highest": prices[hi_p],
                "highest_provider": hi_p, "median": med, "providers": len(prices),
                "lowest_vs_median": prices[lo_p] / med - 1}
        if len(hist) >= SPREAD_BASELINE_POINTS:
            p90 = hist[int(0.9 * (len(hist) - 1))]
            nums.update({"p90_30d": p90, "median_30d": statistics.median(hist), "history_hours": len(hist)})
            if p90 > 0 and spread > p90 * SPREAD_OVER_P90:
                items.append(item("wide_spread", 50 * spread / p90,
                                  f"{g}: {_name(lo_p)} at {_usd(prices[lo_p])} vs median {_usd(med)}; spread "
                                  f"{spread * 100:.0f}% is above its 30-day p90 of {p90 * 100:.0f}%.",
                                  nums, gpu=g, provider=lo_p))
        elif spread >= SPREAD_NO_BASELINE:
            nums["baseline"] = "none: under 7 days of history"
            items.append(item("wide_spread", 25 * spread,
                              f"{g}: {_name(lo_p)} at {_usd(prices[lo_p])} vs {_name(hi_p)} at {_usd(prices[hi_p])} "
                              f"(spread {spread * 100:.0f}%; no history baseline yet).", nums, gpu=g, provider=lo_p))

    # 2. price cuts in the last 24h (provider's own lowest price, both times recorded).
    if then_rows:
        for g, prov in now_rows.items():
            for p, r in prov.items():
                t = then_rows.get(g, {}).get(p)
                if not (t and movers._priced(r) and movers._priced(t)):
                    continue
                pct = r["min_price"] / t["min_price"] - 1
                if pct <= -CUT_MIN and t["min_price"] - r["min_price"] >= events.MIN_USD:
                    items.append(item("price_cut", 200 * -pct,
                                      f"{_name(p)} cut its lowest {g} price {_pctf(pct)} in 24h, to {_usd(r['min_price'])}.",
                                      {"price": r["min_price"], "price_24h_ago": t["min_price"], "pct": pct},
                                      gpu=g, provider=p))
    else:
        unavailable.append({"type": "price_cut", "reason": "needs 24h of history"})

    # 3. new inventory priced below the market median.
    for r in fresh:
        if now_t - r.observed_at > market.stale_after(r.provider):
            continue
        cs = ctx.cov_start.get(r.provider)
        if cs is None or r.first_seen_at - cs < DAY:
            continue  # the provider's first day of coverage: everything looks new
        prices = cur.get(r.gpu) or {}
        if len(prices) < 2:
            continue
        med = statistics.median(prices.values())
        price = float(r.price)
        if price < med:
            disc = price / med - 1
            items.append(item("new_cheap_inventory", 150 * -disc,
                              f"New {r.gpu} listing at {_name(r.provider)} ({r.sku}) at {_usd(price)}, "
                              f"{_pctf(disc)} below the market median {_usd(med)}.",
                              {"price": price, "market_median": med, "discount": disc, "listing_id": r.listing_id,
                               "region": r.region, "gpu_count": r.gpu_count, "first_seen": r.first_seen_at.isoformat(),
                               "available": r.available},
                              gpu=r.gpu, provider=r.provider))

    # 4. regional dislocations.
    region_fn = events._region_fn()
    if region_fn is None:
        unavailable.append({"type": "regional_dislocation", "reason": "region grouping (regions.py) unavailable"})
    else:
        reg = {}
        for r in rollups.regional_hourly(segment, None, H, H):
            reg.setdefault(r["gpu"], []).append(r)
        for g, rows in reg.items():
            S = {p for p in now_rows.get(g, {}) if ctx.established(g, p, H)}
            for grp, (gap, med, gmed, n_in, n_all) in sorted(events._gaps(rows, S, region_fn).items()):
                if abs(gap) >= events.REGION_GAP:
                    word = "cheaper" if gap < 0 else "more expensive"
                    items.append(item("regional_dislocation", 150 * abs(gap),
                                      f"{g} in {grp} is {_pctf(gap)} {word} than the global median "
                                      f"({_usd(med)} vs {_usd(gmed)}).",
                                      {"gap": gap, "group_median": med, "global_median": gmed,
                                       "group_providers": n_in, "providers": n_all},
                                      gpu=g, region_group=grp))

    # 5. providers far below their usual premium.
    any_premium = False
    for g, prices in cur.items():
        if len(prices) < 2:
            continue
        med = statistics.median(prices.values())
        for p, price in prices.items():
            u = usual.get((g, p))
            if not u or u[1] < PREMIUM_MIN_POINTS:
                continue
            any_premium = True
            prem = price / med - 1
            delta = prem - u[0]
            if delta <= -PREMIUM_DELTA:
                items.append(item("below_usual_premium", 200 * -delta,
                                  f"{_name(p)} {g} is {_pctf(prem)} {'below' if prem < 0 else 'above'} the market median, "
                                  f"vs {_pctf(u[0])} {'below' if u[0] < 0 else 'above'} on its 30-day average.",
                                  {"price": price, "market_median": med, "premium_now": prem, "premium_30d": u[0],
                                   "delta": delta, "samples": u[1]}, gpu=g, provider=p))
    if not any_premium:
        unavailable.append({"type": "below_usual_premium", "reason": "needs 7 days of hourly samples per provider"})

    # 6. temporary scarcity vs the 30-day norm.
    any_scarcity = False
    for g, rows in table.items():
        past = [r["available_listings"] for r in rows if r["hour"] < H]
        nowr = next((r for r in rows if r["hour"] == H), None)
        if nowr is None or len(past) < SCARCITY_MIN_POINTS:
            continue
        any_scarcity = True
        norm = statistics.fmean(past)
        if norm >= SCARCITY_MIN_NORM and nowr["available_listings"] <= SCARCITY_RATIO * norm:
            ratio = nowr["available_listings"] / norm
            items.append(item("scarcity", 100 * (1 - ratio),
                              f"{g}: {nowr['available_listings']} available listings vs a 30-day average of {norm:.1f}.",
                              {"available_listings": nowr["available_listings"], "average_30d": norm, "ratio": ratio,
                               "providers_available": nowr["providers_available"], "samples": len(past)}, gpu=g))
    if not any_scarcity:
        unavailable.append({"type": "scarcity", "reason": "needs 7 days of hourly samples"})

    # 7. new cheapest provider in the last 24h that still is the cheapest.
    for e in events.recent(types=["new_cheapest_provider"], since=H - DAY, limit=500):
        prices = cur.get(e["gpu"]) or {}
        if prices and min(prices, key=prices.get) == e["provider"] and not any(
                x["type"] == "new_cheapest_provider" and x["gpu"] == e["gpu"] for x in items):
            pct = e["pct"] or 0.0
            items.append(item("new_cheapest_provider", 40 + 200 * max(0.0, -pct),
                              f"{_name(e['provider'])} became the cheapest {e['gpu']} at {e['occurred_at'][11:16]} UTC {e['occurred_at'][:10]} "
                              f"and still is, at {_usd(prices[e['provider']])}.",
                              {"price": prices[e["provider"]], "since": e["occurred_at"],
                               "previous_provider": e["detail"].get("previous_provider"),
                               "previous_price": e["detail"].get("previous_price"), "event_id": e["id"]},
                              gpu=e["gpu"], provider=e["provider"]))

    # 8. supply changes vs 24h ago, providers recorded at both times.
    if then_rows:
        for g, prov in now_rows.items():
            then = then_rows.get(g, {})
            common = set(prov) & set(then)
            if not common:
                continue
            pp_now = sum(1 for p in common if movers._priced(prov[p]))
            pp_then = sum(1 for p in common if movers._priced(then[p]))
            al_now = sum(prov[p]["available_listings"] for p in common)
            al_then = sum(then[p]["available_listings"] for p in common)
            rel = (al_now - al_then) / al_then if al_then >= 4 else None
            if abs(pp_now - pp_then) >= SUPPLY_PROVIDERS or (rel is not None and abs(rel) >= SUPPLY_LISTINGS):
                up = (pp_now - pp_then) > 0 or (rel or 0) > 0
                items.append(item("supply_change", 20 * abs(pp_now - pp_then) + 50 * abs(rel or 0),
                                  f"{g} supply {'grew' if up else 'shrank'}: {pp_then} -> {pp_now} providers with "
                                  f"purchasable listings, {al_then} -> {al_now} available listings in 24h.",
                                  {"providers_priced_now": pp_now, "providers_priced_then": pp_then,
                                   "available_listings_now": al_now, "available_listings_then": al_then,
                                   "providers_matched": len(common)}, gpu=g))
    else:
        unavailable.append({"type": "supply_change", "reason": "needs 24h of history"})

    items.sort(key=lambda x: (-x["score"], x["type"], x["gpu"] or "", x["provider"] or ""))
    return {"as_of_hour": H.isoformat(), "items": items, "unavailable": unavailable}


def opportunities(types=None, gpu=None, provider=None, min_score: float = 0, limit: int = 100) -> dict:
    m = monitor()
    out = [x for x in m["items"]
           if (not types or x["type"] in types) and (not gpu or x["gpu"] == gpu)
           and (not provider or x["provider"] == provider) and x["score"] >= min_score]
    return {**m, "items": out[:limit], "total": len(out)}


def catalogue() -> list[dict]:
    return [{"type": t, "kind": k, "description": d} for t, (k, d) in TYPES.items()]
