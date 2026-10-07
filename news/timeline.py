"""News next to market movements, in one time-ordered view. Never as an explanation.

`timeline(gpu, provider, t0, t1)` overlays four independent things on one window:
    prices         observed, from the hourly rollup (market_hourly): the market lowest and
                   median provider price for a GPU, or one provider's lowest
    price_moves    inferred: hour-over-hour changes of at least MOVE_PCT in that line, each
                   flagged when the set of providers changed at the same hour (a provider we
                   started or stopped recording moves the lowest without the market moving)
    availability   inferred: per-provider transitions between priced, sold out and absent
    market_events  rows of market_events (the events engine), if that table exists
    news           news.store.related() for the same GPU / provider and window

`around_move(gpu, at, window_hours)` lists the news and market events near one moment,
ranked by relevance and closeness in time. It is headed, always, "Related news and
events around this move (not necessarily causal)": being near in time is all it shows.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from statistics import median

from sqlalchemy import text

import normalize
from analytics import rollups
from news import store

MOVE_PCT = 3.0
NOT_CAUSAL = "Related news and events around this move (not necessarily causal)"
OVERLAY_NOTE = ("Observed prices, inferred moves and availability changes, detected market events and related news "
                "shown on one time axis. Appearing together in time does not mean one explains the other.")
_SEVERITY = {"major": 1.0, "notable": 0.7, "info": 0.4}


def _f(v):
    return None if v is None else round(float(v), 6)


# --------------------------------------------------------------------------- prices

def _series(gpu: str | None, provider: str | None, t0, t1, segment: str) -> tuple[list[dict], dict]:
    rows = rollups.provider_hourly(segment, gpu=gpu, provider=provider, t0=t0, t1=t1)
    by_hour: dict[datetime, list[dict]] = {}
    for r in rows:
        by_hour.setdefault(r["hour"], []).append(r)
    first = rollups.first_hours(segment) if rows else {}
    series, per_provider = [], {}
    prev_set = None
    for h in sorted(by_hour):
        rs = by_hour[h]
        priced = {r["provider"]: r["min_price"] for r in rs if r["min_price"] is not None}
        pset = set(priced)
        newly = sorted(p for p in pset if any(first.get((r["gpu"], p)) == h for r in rs if r["provider"] == p))
        point = {
            "hour": h,
            "lowest": _f(min(priced.values())) if priced else None,
            "median": _f(median(priced.values())) if priced else None,
            "priced_providers": len(priced),
            "available_listings": sum(int(r["available_listings"] or 0) for r in rs),
            "sold_out_listings": sum(int(r["sold_out_listings"] or 0) for r in rs),
            "live_listings": sum(int(r["live_listings"] or 0) for r in rs),
        }
        if prev_set is not None and pset != prev_set:
            point["provider_set_changed"] = {"added": sorted(pset - prev_set), "removed": sorted(prev_set - pset),
                                             "newly_recorded": newly}
        prev_set = pset
        series.append(point)
        for r in rs:
            st = ("priced" if r["min_price"] is not None
                  else "sold_out" if int(r["sold_out_listings"] or 0) > 0 else "unpriced")
            per_provider.setdefault(r["provider"], []).append((h, st, r["min_price"]))
    return series, per_provider


def _moves(series: list[dict], basis: str, pct: float) -> list[dict]:
    out = []
    for a, b in zip(series, series[1:]):
        if a["lowest"] is None or b["lowest"] is None or a["lowest"] <= 0:
            continue
        ch = (b["lowest"] - a["lowest"]) / a["lowest"] * 100
        if abs(ch) >= pct:
            out.append({"at": b["hour"], "basis": basis, "from": a["lowest"], "to": b["lowest"],
                        "change_pct": round(ch, 2), "direction": "up" if ch > 0 else "down",
                        "provider_set_changed": b.get("provider_set_changed"),
                        "kind": "inferred"})
    return out


def _availability(per_provider: dict) -> list[dict]:
    out = []
    for prov, pts in per_provider.items():
        prev_h, prev = None, None
        for h, st, _price in pts:
            if prev is not None:
                gap = h - prev_h > timedelta(hours=1)
                if gap:
                    out.append({"at": prev_h + timedelta(hours=1), "provider": prov, "change": "absent",
                                "from": prev, "to": "absent"})
                    prev = "absent"
                if st != prev:
                    change = {("priced", "sold_out"): "sold_out", ("sold_out", "priced"): "capacity_returned",
                              ("absent", "priced"): "reappeared"}.get((prev, st), f"{prev}_to_{st}")
                    out.append({"at": h, "provider": prov, "change": change, "from": prev, "to": st})
            prev_h, prev = h, st
    out.sort(key=lambda x: x["at"])
    return out


# --------------------------------------------------------------------------- market events

def market_events_available() -> bool:
    with normalize.SessionLocal() as s:
        return s.execute(text("SELECT to_regclass('public.market_events') IS NOT NULL")).scalar()


def market_events(gpu=None, provider=None, t0=None, t1=None, limit=200) -> list[dict] | None:
    """Rows from market_events (events agent's table) or None when that table does not exist."""
    if not market_events_available():
        return None
    sql = text("""
        SELECT id, occurred_at, detected_at, type, segment, gpu, provider, region_group, severity, title, detail, dedupe_key
        FROM market_events
        WHERE (CAST(:gpu AS text) IS NULL OR gpu = :gpu OR gpu IS NULL AND CAST(:provider AS text) IS NOT NULL)
          AND (CAST(:provider AS text) IS NULL OR provider = :provider OR provider IS NULL AND CAST(:gpu AS text) IS NOT NULL)
          AND (CAST(:t0 AS timestamptz) IS NULL OR occurred_at >= :t0)
          AND (CAST(:t1 AS timestamptz) IS NULL OR occurred_at <= :t1)
        ORDER BY occurred_at DESC LIMIT :limit
    """)
    with normalize.SessionLocal() as s:
        return [dict(r._mapping) for r in s.execute(sql, {"gpu": gpu, "provider": provider, "t0": t0, "t1": t1,
                                                          "limit": limit})]


# --------------------------------------------------------------------------- timeline

def timeline(gpu: str | None = None, provider: str | None = None, t0: datetime | None = None,
             t1: datetime | None = None, segment: str = "on_demand", move_pct: float = MOVE_PCT) -> dict:
    t1 = t1 or datetime.now(timezone.utc)
    t0 = t0 or t1 - timedelta(days=7)
    series, per_provider = _series(gpu, provider, t0, t1, segment)
    basis = (f"provider_lowest:{provider}" if provider else "market_lowest") if gpu else (
        f"provider_lowest_any_gpu:{provider}" if provider else None)
    prices = {"kind": "observed", "basis": basis, "segment": segment, "points": series}
    if not gpu:
        # A provider's lowest across all its GPUs is not one price; keep per-GPU lines instead.
        prices["points"] = []
        prices["by_gpu"] = {}
        for g in sorted({r["gpu"] for r in rollups.provider_hourly(segment, provider=provider, t0=t0, t1=t1)}):
            prices["by_gpu"][g] = _series(g, provider, t0, t1, segment)[0]
    if not series and not prices.get("by_gpu"):
        prices["unavailable"] = "insufficient coverage: no hourly rollup rows for this selection and window"
    moves = _moves(series, "market_lowest" if not provider else "provider_lowest", move_pct) if gpu else []
    if not gpu:
        for g, pts in prices.get("by_gpu", {}).items():
            for m in _moves(pts, "provider_lowest", move_pct):
                moves.append({**m, "gpu": g})
        moves.sort(key=lambda m: m["at"])
    ev = market_events(gpu, provider, t0, t1)
    news = store.related(gpu=gpu, provider=provider, t0=t0, t1=t1, limit=200)
    return {
        "gpu": gpu, "provider": provider, "t0": t0, "t1": t1, "note": OVERLAY_NOTE,
        "prices": prices,
        "price_moves": {"kind": "inferred", "threshold_pct": move_pct, "moves": moves},
        "availability_changes": {"kind": "inferred", "changes": _availability(per_provider)},
        "market_events": ({"available": False, "reason": "market_events table not present", "events": []} if ev is None
                          else {"available": True, "events": ev}),
        "news": {"items": news, "count": len(news)},
    }


def _price_near(series: list[dict], at: datetime) -> dict | None:
    best = None
    for p in series:
        if p["hour"] <= at and p["lowest"] is not None:
            best = p
    return best


def around_move(gpu: str, at: datetime, window_hours: float = 48, provider: str | None = None,
                segment: str = "on_demand") -> dict:
    """News and market events within +-window_hours of `at`, ranked by relevance and time proximity."""
    w = timedelta(hours=window_hours)
    t0, t1 = at - w, at + w
    series, _ = _series(gpu, provider, t0 - timedelta(hours=1), t1, segment)
    before, now_pt = _price_near(series, t0), _price_near(series, at)
    move = None
    if before and now_pt and before["lowest"]:
        move = {"kind": "observed", "basis": "provider_lowest" if provider else "market_lowest",
                "from_hour": before["hour"], "from": before["lowest"], "to_hour": now_pt["hour"], "to": now_pt["lowest"],
                "change_pct": round((now_pt["lowest"] - before["lowest"]) / before["lowest"] * 100, 2)}

    def prox(t: datetime) -> float:
        return round(max(0.0, 1 - abs((t - at).total_seconds()) / w.total_seconds()), 4)

    ranked = []
    for n in store.related(gpu=gpu, provider=provider, t0=t0, t1=t1, limit=200):
        p = prox(n["published_at"])
        ranked.append({"type": "news", "at": n["published_at"], "title": n["title"], "item": n,
                       "hours_from_move": round((n["published_at"] - at).total_seconds() / 3600, 2),
                       "position": "before" if n["published_at"] <= at else "after",
                       "rank": round(0.6 * n["relevance"] / 100 + 0.4 * p, 4),
                       "rank_components": {"relevance": n["relevance"], "proximity": p,
                                           "formula": "0.6 * relevance/100 + 0.4 * proximity"}})
    ev = market_events(gpu, provider, t0, t1)
    for e in ev or []:
        p = prox(e["occurred_at"])
        sev = _SEVERITY.get(e.get("severity"), 0.4)
        ranked.append({"type": "market_event", "at": e["occurred_at"], "title": e["title"], "event": e,
                       "hours_from_move": round((e["occurred_at"] - at).total_seconds() / 3600, 2),
                       "position": "before" if e["occurred_at"] <= at else "after",
                       "rank": round(0.6 * sev + 0.4 * p, 4),
                       "rank_components": {"severity_weight": sev, "proximity": p,
                                           "formula": "0.6 * severity_weight + 0.4 * proximity"}})
    ranked.sort(key=lambda r: (-r["rank"], abs(r["hours_from_move"])))
    return {"heading": NOT_CAUSAL, "gpu": gpu, "provider": provider, "at": at, "window_hours": window_hours,
            "move": move or {"unavailable": "insufficient coverage: no rollup price at both ends of the window"},
            "market_events_available": ev is not None, "related": ranked}
