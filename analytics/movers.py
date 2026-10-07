"""The homepage market overview: what moved, what is scarce, what is unusual, in one payload.

Everything reads the hourly rollup (market_hourly) and the detector's cross-provider
table (market_gpu_hourly), never raw observations over long windows. Methodology:
methodology/events.md#overview.

Coverage rules, the same as the event detector's:
    - 24h changes are coverage-matched: only providers priced at BOTH times vote,
      one vote per provider (its lowest price), so a provider we began recording
      in between cannot look like a market move.
    - "Newly available" GPUs exclude pairs that were already on sale when our
      coverage of that provider began.
    - Statistics that need history (7d volatility, 30d z-scores) say so and are
      returned as unavailable, with the reason, when the history is not there.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

import market
import normalize
from analytics import events, rollups
from api.common import gpu_slug
from cache import ttl_cache

SEGMENT = "on_demand"
HOUR, DAY = timedelta(hours=1), timedelta(days=1)
TOP = 8
VOL_MIN_POINTS = 72          # hourly changes needed in the 7-day window
Z_MIN_SAMPLES = 14           # daily 24h-changes needed in the 30-day window
Z_FLAG = 2.0
NEW_WINDOW = timedelta(days=7)


def _f(v):
    return None if v is None else float(v)


def _iso(t):
    return None if t is None else t.isoformat()


def section(items=None, *, kind="observed", reason=None, **extra) -> dict:
    """{"available", "reason", "kind", "items", ...}. Unavailable sections carry a reason, never zeros."""
    if reason is not None:
        return {"available": False, "reason": reason, "kind": kind, "items": [], **extra}
    return {"available": True, "reason": None, "kind": kind, "items": items or [], **extra}


def latest_hour(segment=SEGMENT):
    with normalize.SessionLocal() as s:
        return s.execute(text("SELECT max(hour) FROM market_gpu_hourly WHERE segment = :s"), {"s": segment}).scalar()


def _table(segment, t0, t1) -> dict[str, list[dict]]:
    with normalize.SessionLocal() as s:
        rows = s.execute(text("""
            SELECT * FROM market_gpu_hourly WHERE segment = :s AND hour >= :t0 AND hour <= :t1 ORDER BY gpu, hour
        """), {"s": segment, "t0": t0, "t1": t1}).all()
    out: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        d = dict(r._mapping)
        for k in ("lowest", "median", "highest"):
            d[k] = _f(d[k])
        out[d["gpu"]].append(d)
    return out


def _by_gpu(rows) -> dict[str, dict]:
    out: dict[str, dict] = defaultdict(dict)
    for r in rows:
        out[r["gpu"]][r["provider"]] = r
    return out


def _priced(r) -> bool:
    return r["priced_listings"] > 0 and r["min_price"] is not None


def matched_change(now: dict, then: dict) -> dict:
    """24h change over providers priced at both times; one vote per provider."""
    a = {p: r["min_price"] for p, r in now.items() if _priced(r)}
    b = {p: r["min_price"] for p, r in then.items() if _priced(r)}
    common = sorted(set(a) & set(b))
    if not common:
        return {"providers_matched": 0, "median_pct": None, "lowest_pct": None}
    mn, mt = statistics.median([a[p] for p in common]), statistics.median([b[p] for p in common])
    ln, lt = min(a[p] for p in common), min(b[p] for p in common)
    return {"providers_matched": len(common),
            "median_now": mn, "median_then": mt, "median_pct": (mn - mt) / mt if len(common) >= 2 and mt else None,
            "lowest_now": ln, "lowest_then": lt, "lowest_pct": (ln - lt) / lt if lt else None,
            "excluded_providers": sorted((set(a) | set(b)) - set(common))}


def _gpu_ref(g) -> dict:
    return {"gpu": g, "gpu_slug": gpu_slug(g)}


def price_changes(hours: float = 24, limit: int = 30) -> list[dict]:
    """Recent per-listing price changes (eligible on-demand listings), newest first.

    Uses normalize.classify_change's noise floor (0.5% and $0.001): smaller moves are not changes.
    """
    sql = text(f"""
        SELECT o.provider, o.listing_id, c.canonical_gpu_name AS gpu, c.region, c.sku, c.gpu_count,
               o.observed_at, o.price_per_gpu_hour AS price, prev.price AS prev_price, prev.observed_at AS prev_at
        FROM listing_observations o
        JOIN compute_listings c ON c.provider = o.provider AND c.listing_id = o.listing_id
        CROSS JOIN LATERAL (
            SELECT p.price_per_gpu_hour AS price, p.observed_at FROM listing_observations p
            WHERE p.provider = o.provider AND p.listing_id = o.listing_id AND p.observed_at < o.observed_at
            ORDER BY p.observed_at DESC LIMIT 1) prev
        WHERE o.observed_at > :t0 AND {market._ELIGIBLE}
          AND o.price_per_gpu_hour > 0 AND prev.price > 0
          AND abs(o.price_per_gpu_hour - prev.price) >= :usd
          AND abs(o.price_per_gpu_hour - prev.price) / prev.price >= :pct
        ORDER BY o.observed_at DESC LIMIT :limit
    """)
    t0 = datetime.now(timezone.utc) - timedelta(hours=hours)
    with normalize.SessionLocal() as s:
        rows = s.execute(sql, {"t0": t0, "usd": normalize.CHANGE_MIN_USD, "pct": normalize.CHANGE_MIN_PCT,
                               "limit": limit}).all()
    out = []
    for r in rows:
        status, pct = normalize.classify_change(r.price, r.prev_price, r.prev_at, r.prev_at, r.prev_at)
        if status not in ("up", "down"):
            continue
        out.append({**_gpu_ref(r.gpu), "provider": r.provider, "listing_id": r.listing_id, "sku": r.sku,
                    "region": r.region, "gpu_count": r.gpu_count, "status": status, "pct": float(pct),
                    "price": float(r.price), "previous_price": float(r.prev_price),
                    "changed_at": _iso(r.observed_at), "previous_at": _iso(r.prev_at)})
    return out


def tape(gpu: str | None = None, limit: int = 40) -> list[dict]:
    """Compact ticker: latest price moves and notable/major events, newest first."""
    evs, _ = events.query(gpu=gpu, limit=limit * 3)
    out = []
    for e in evs:
        if e["type"] == "coverage_started":
            continue
        if e["severity"] == "info" and e["type"] not in ("price_move", "new_cheapest_provider"):
            continue
        out.append({"at": e["occurred_at"], "type": e["type"], "severity": e["severity"], "gpu": e["gpu"],
                    "gpu_slug": e["gpu_slug"], "provider": e["provider"], "region_group": e["region_group"],
                    "title": e["title"], "pct": e["pct"], "value": e["value_after"], "kind": e["kind"]})
        if len(out) >= limit:
            break
    return out


@ttl_cache(60)
def overview(segment: str = SEGMENT) -> dict:
    H = latest_hour(segment)
    if H is None:
        reason = "no market history yet (the rollup and event detector have not run)"
        empty = section(reason=reason)
        return {"as_of_hour": None, "history_hours": 0, "board": empty, "gainers": empty, "losers": empty,
                "volatile": empty, "liquid": empty, "availability_gaining": empty, "availability_losing": empty,
                "newly_available": empty, "sold_out": empty, "unusual": empty,
                "price_changes": section(price_changes()), "capacity": empty, "tape": section(tape())}
    table = _table(segment, H - timedelta(days=31), H)
    with normalize.SessionLocal() as s:
        first_hour = s.execute(text("SELECT min(hour) FROM market_gpu_hourly WHERE segment = :s"), {"s": segment}).scalar()
        first_priced = dict(s.execute(text("""
            SELECT gpu, min(hour) FROM market_gpu_hourly WHERE segment = :s AND lowest IS NOT NULL GROUP BY gpu
        """), {"s": segment}).all())
        ctx = events.load_context(s, segment)
    now_rows = _by_gpu(rollups.provider_hourly(segment, None, None, H, H))
    then_rows = _by_gpu(rollups.provider_hourly(segment, None, None, H - DAY, H - DAY))
    history_hours = int((H - first_hour) / HOUR) if first_hour else 0
    has_24h = history_hours >= 24 and bool(then_rows)
    no24 = f"needs 24h of history; {history_hours}h recorded"

    # Board: every GPU priced now, with its matched 24h change.
    board, changes = [], {}
    for g, prov in sorted(now_rows.items()):
        priced = {p: r["min_price"] for p, r in prov.items() if _priced(r)}
        ch = matched_change(prov, then_rows.get(g, {})) if has_24h else {"providers_matched": 0}
        changes[g] = ch
        if not priced:
            continue
        low_p = min(priced, key=priced.get)
        board.append({**_gpu_ref(g), "providers": len(priced), "lowest": priced[low_p], "lowest_provider": low_p,
                      "median": statistics.median(priced.values()), "highest": max(priced.values()),
                      "change_24h_median": ch.get("median_pct"), "change_24h_lowest": ch.get("lowest_pct"),
                      "providers_matched": ch.get("providers_matched")})
    board.sort(key=lambda x: (-x["providers"], x["gpu"]))

    def movers(sign):
        if not has_24h:
            return section(reason=no24)
        items = [{**_gpu_ref(g), **{k: v for k, v in ch.items() if k != "excluded_providers"},
                  "excluded_providers": ch.get("excluded_providers", [])}
                 for g, ch in changes.items() if ch.get("median_pct") is not None and sign * ch["median_pct"] > 0]
        items.sort(key=lambda x: -sign * x["median_pct"])
        return section(items[:TOP], rule="matched 24h change of the median provider price; >= 2 providers priced "
                                         "at both times; one vote per provider")

    # Volatility over 7 days: stdev of the matched hourly median change.
    vol = []
    for g, rows in table.items():
        pts = [r["chg1h_median"] for r in rows if r["hour"] > H - 7 * DAY and r["chg1h_median"] is not None]
        meds = [r["median"] for r in rows if r["hour"] > H - 7 * DAY and r["median"] is not None]
        if len(pts) >= VOL_MIN_POINTS:
            vol.append({**_gpu_ref(g), "hourly_stdev": statistics.pstdev(pts), "points": len(pts),
                        "range_7d": (max(meds) / min(meds) - 1) if meds and min(meds) > 0 else None})
    vol.sort(key=lambda x: -x["hourly_stdev"])
    volatile = section(vol[:TOP], kind="inferred") if vol else section(
        reason=f"needs {VOL_MIN_POINTS} hourly matched changes in 7 days; {history_hours}h recorded", kind="inferred")

    # Liquidity / coverage now.
    liquid = []
    for g, prov in now_rows.items():
        pa = sum(1 for r in prov.values() if r["available_listings"] > 0)
        al = sum(r["available_listings"] for r in prov.values())
        pp = sum(1 for r in prov.values() if _priced(r))
        if pp:
            liquid.append({**_gpu_ref(g), "providers_priced": pp, "providers_available": pa,
                           "available_listings": al, "live_listings": sum(r["live_listings"] for r in prov.values()),
                           "score": pa * al})
    liquid.sort(key=lambda x: (-x["providers_priced"], -x["available_listings"], x["gpu"]))

    # Availability now vs 24h ago, over providers recorded at both times.
    gain, lose = [], []
    if has_24h:
        for g, prov in now_rows.items():
            then = then_rows.get(g, {})
            common = set(prov) & set(then)
            if not common:
                continue
            a_now = sum(prov[p]["available_listings"] for p in common)
            a_then = sum(then[p]["available_listings"] for p in common)
            pa_now = sum(1 for p in common if _priced(prov[p]))
            pa_then = sum(1 for p in common if _priced(then[p]))
            item = {**_gpu_ref(g), "available_listings_now": a_now, "available_listings_then": a_then,
                    "providers_priced_now": pa_now, "providers_priced_then": pa_then,
                    "delta_listings": a_now - a_then, "delta_providers": pa_now - pa_then, "providers_matched": len(common)}
            if a_now - a_then > 0 or pa_now - pa_then > 0:
                gain.append(item)
            elif a_now - a_then < 0 or pa_now - pa_then < 0:
                lose.append(item)
        gain.sort(key=lambda x: (-x["delta_providers"], -x["delta_listings"]))
        lose.sort(key=lambda x: (x["delta_providers"], x["delta_listings"]))

    # Newly available: first priced in the last 7 days, by a provider that was already being tracked.
    new = []
    for g, t in first_priced.items():
        if t < H - NEW_WINDOW:
            continue
        pairs = sorted((fh, p) for (gg, p), fh in ctx.first.items() if gg == g)
        if not pairs or not ctx.genuine_pair(g, pairs[0][1]):
            continue  # first seen at the start of our coverage: not new on the market
        b = next((x for x in board if x["gpu"] == g), None)
        new.append({**_gpu_ref(g), "first_priced": _iso(t), "first_provider": pairs[0][1],
                    "lowest_now": b and b["lowest"], "providers_now": b and b["providers"]})
    new.sort(key=lambda x: x["first_priced"], reverse=True)
    started = f"{first_hour:%Y-%m-%d %H:%M} UTC" if first_hour else "not yet"
    new_note = None if first_hour and first_hour <= H - NEW_WINDOW else \
        f"tracking began {started}: GPUs on sale when coverage started are not counted as new"

    # Sold-out markets: live listings, nothing purchasable anywhere.
    sold = []
    for g, prov in now_rows.items():
        if prov and not any(_priced(r) for r in prov.values()) and any(r["sold_out_listings"] > 0 for r in prov.values()):
            rows = table.get(g, [])
            last = next((r for r in reversed(rows) if r["lowest"] is not None), None)
            sold.append({**_gpu_ref(g), "providers": sorted(prov), "live_listings": sum(r["live_listings"] for r in prov.values()),
                         "last_priced_at": _iso(last and last["hour"]), "last_lowest": last and last["lowest"]})

    # Unusual: today's matched 24h change vs the same statistic on each of the previous 30 days.
    unusual, z_counted = [], 0
    for g, rows in table.items():
        by_h = {r["hour"]: r for r in rows}
        x = by_h.get(H, {}).get("chg24_median")
        if x is None:
            continue
        hist = [by_h[H - k * DAY]["chg24_median"] for k in range(1, 31)
                if H - k * DAY in by_h and by_h[H - k * DAY]["chg24_median"] is not None]
        if len(hist) < Z_MIN_SAMPLES:
            continue
        z_counted += 1
        sd = statistics.pstdev(hist)
        if sd <= 0:
            continue
        z = (x - statistics.fmean(hist)) / sd
        if abs(z) >= Z_FLAG:
            unusual.append({**_gpu_ref(g), "change_24h_median": x, "z": z, "samples": len(hist),
                            "mean": statistics.fmean(hist), "stdev": sd})
    unusual.sort(key=lambda x: -abs(x["z"]))
    unusual_s = section(unusual[:TOP], kind="inferred", rule=f"|z| >= {Z_FLAG} vs >= {Z_MIN_SAMPLES} daily samples") \
        if z_counted else section(reason=f"needs {Z_MIN_SAMPLES} days of matched 24h changes; {history_hours}h recorded",
                                  kind="inferred")

    # Market-wide capacity.
    since = H - DAY
    so_events, _ = events.query(types=["sold_out"], since=since, limit=1000)
    cr_events, _ = events.query(types=["capacity_returned"], since=since, limit=1000)
    pairs_live = sum(len(p) for p in now_rows.values())
    pairs_sold = sum(1 for prov in now_rows.values() for r in prov.values()
                     if r["live_listings"] > 0 and r["priced_listings"] == 0 and r["sold_out_listings"] > 0)
    capacity = {
        "available": True, "kind": "observed",
        "markets_live": pairs_live, "markets_sold_out_now": pairs_sold,
        "gpus_sold_out_everywhere": len(sold),
        "sold_out_events_24h": sum(1 for e in so_events if e["provider"]),
        "returned_events_24h": sum(1 for e in cr_events if e["provider"]),
        "available_listings_now": sum(x["available_listings"] for x in liquid),
        "available_listings_change_24h": (sum(x["delta_listings"] for x in gain + lose) if has_24h else None),
        "note": "a market is one (GPU, provider) pair; listing changes are over providers recorded at both times",
    }

    return {
        "as_of_hour": _iso(H), "segment": segment, "history_hours": history_hours,
        "tracking_since": _iso(first_hour),
        "board": section(board),
        "gainers": movers(+1), "losers": movers(-1),
        "volatile": volatile,
        "liquid": section(liquid[:TOP], rule="providers priced, then available listings"),
        "availability_gaining": section(gain[:TOP]) if has_24h else section(reason=no24),
        "availability_losing": section(lose[:TOP]) if has_24h else section(reason=no24),
        "newly_available": section(new, kind="inferred", note=new_note),
        "sold_out": section(sold),
        "unusual": unusual_s,
        "price_changes": section(price_changes()),
        "capacity": capacity,
        "tape": section(tape()),
    }


def clear_caches():
    overview.cache_clear()
    try:
        from analytics import opportunities
        opportunities.monitor.cache_clear()
    except Exception:
        pass
