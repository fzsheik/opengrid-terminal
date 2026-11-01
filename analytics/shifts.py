"""Major market shifts, each with the items found around it (methodology/shifts.md).

A shift is a move large against its own history, built from what OpenGrid already measures:

    market_median        a GPU's coverage-matched 24h median change (market_gpu_hourly.chg24_median) at least
                         max(MEDIAN_MIN, K x its trailing 30-day RMS 24h change), with >= 2 providers priced
    index_move           a composite index (gpu-compute, *-class) 24h level change within one chain segment, at
                         least max(INDEX_MIN, K x its trailing RMS)
    provider_repricing   one provider's own prices moved the same way on >= REPRICE_MIN_GPUS GPUs within 24h
                         (market_events price_move, cause price_change; marketplaces excluded: host asks)
    capacity_collapse    market_events market_capacity 'tightening' at severity major, or a GPU sold out at every
                         tracked provider when >= 2 providers carried it
    archived_price_change         (data_kind 'archived') a provider's lowest list price for a GPU differs by >=
                                  ARCHIVED_MIN between two consecutive archived documents
    archived_provider_repricing   (archived) >= REPRICE_MIN_GPUS GPUs of one provider changed the same way between
                                  the same two documents (their per-GPU changes are folded into it)

Consecutive firing hours of the same sign (gaps < 24h) are one episode: one shift, measured at its largest
hour, keyed on the episode's first hour so a re-run updates it instead of adding another.

Around each shift the related items are ranked and STORED (market_shift_items): news, provider announcements,
SEC filings and OpenGrid's own market events in [start - BEFORE, at + AFTER], each with its score components and
a snapshot of what was shown. A shift whose window closed more than SETTLE ago keeps its items unless the
definition or ranking version changes or a rebuild is asked for, so the explanation does not drift.

Never causal: an item is listed because it is about the same GPU, provider or vendor (or is market-wide) and
near in time. The heading says so wherever shifts are shown (NOT_CAUSAL).
"""
from __future__ import annotations

import bisect
import logging
import math
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert

import normalize
import provider_meta
from jobs import job
from news import classify as cls
from news import sources as registry
from news import store as news_store
from store.shifts import MarketShift, MarketShiftItem

log = logging.getLogger(__name__)

UTC = timezone.utc
HOUR, DAY = timedelta(hours=1), timedelta(days=1)
DEFINITION_VERSION = "1"
ITEMS_VERSION = "1"
NOT_CAUSAL = "Related news and events around this shift (not necessarily causal)"
NOTE = ("A shift is a large move measured by OpenGrid. The items listed were published near it and are about the same "
        "GPU, provider or vendor, or are market-wide. Appearing together in time does not mean one explains the other.")

# --- definition (methodology/shifts.md; change both together and bump DEFINITION_VERSION) ---
SEGMENT = "on_demand"
K = 3.0                       # times the trailing RMS 24h change
BASELINE = timedelta(days=30)
BASELINE_MIN = 7 * 24         # hourly 24h-changes needed in the baseline window
MEDIAN_MIN = 0.10             # a GPU median move is never a shift below 10%
MEDIAN_MIN_PROVIDERS = 2
INDEX_MIN = 0.05
REPRICE_MIN_GPUS = 3
REPRICE_WINDOW = DAY
ARCHIVED_MIN = 0.15
ARCHIVED_MAX_GAP = timedelta(days=60)   # captures further apart: the change is placed no better than this
EPISODE_GAP = DAY
CAPACITY_GAP = timedelta(days=3)
MAJOR_MULT = 2.0              # severity major at >= 2x the threshold (or >= 25%)
MAJOR_ABS = 0.25
# --- ranking ---
BEFORE, AFTER = timedelta(hours=72), timedelta(hours=48)
ARCHIVED_PAD = timedelta(days=7)
SETTLE = timedelta(days=3)    # items of a shift whose window closed this long ago are frozen
MAX_ITEMS = 25
MAX_EVENTS = 6                # of which OpenGrid's own market events at most
MARKET_WIDE_TOPICS = ("export_controls", "price_change", "pricing_report", "supply_deal", "futures", "capex")
MARKET_WIDE_MIN_RELEVANCE = 45
MIN_RELEVANCE = 20           # news about the GPU / provider below this is not listed (passing mentions)
MATCH_WEIGHT = {"gpu": 1.0, "provider": 1.0, "vendor": 0.7, "market-wide": 0.4}
SEVERITY_WEIGHT = {"major": 1.0, "notable": 0.7, "info": 0.4}
EXCLUDED_CLASSES = {"marketplace", "decentralized"}

TYPES = {
    "market_median": ("observed", "GPU median moved beyond its own volatility"),
    "index_move": ("observed", "Composite index moved beyond its own volatility"),
    "provider_repricing": ("observed", "One provider repriced several GPUs the same way within 24h"),
    "capacity_collapse": ("observed", "Market-wide sell-outs"),
    "archived_price_change": ("archived", "A provider's archived list price changed between two captures"),
    "archived_provider_repricing": ("archived", "A provider's archived list prices changed on several GPUs"),
}

_ITEMS_FOR = {"market_median", "index_move", "provider_repricing", "capacity_collapse", "archived_price_change",
              "archived_provider_repricing"}


def _iso(t):
    return t.isoformat() if t else None


def _pct(x: float) -> str:
    return f"{x * 100:+.1f}%"


def _sev(mag: float, thr: float) -> str:
    return "major" if abs(mag) >= max(MAJOR_ABS, MAJOR_MULT * thr) else "notable"


# --------------------------------------------------------------------------- pure detection

def rms_threshold(times: list[datetime], values: list[float], i: int, floor: float) -> tuple[float | None, int]:
    """max(floor, K x RMS) over values in [t_i - BASELINE, t_i - 24h): the move's own 24h is never its baseline.
    (None, n) when fewer than BASELINE_MIN values are there."""
    t = times[i]
    lo = bisect.bisect_left(times, t - BASELINE)
    hi = bisect.bisect_left(times, t - DAY)
    window = [v for v in values[lo:hi] if v is not None]
    if len(window) < BASELINE_MIN:
        return None, len(window)
    rms = math.sqrt(sum(v * v for v in window) / len(window))
    return max(floor, K * rms), len(window)


def episodes(times: list[datetime], values: list[float | None], floor: float, ok=None) -> list[dict]:
    """Hours whose |value| crosses the threshold, grouped into same-sign episodes (gaps < EPISODE_GAP).
    Each: {start, at (largest |value|; earliest on ties), end, value, threshold, baseline_n, hours}."""
    out, cur = [], None
    for i, (t, v) in enumerate(zip(times, values)):
        if v is None or (ok is not None and not ok(i)):
            continue
        thr, n = rms_threshold(times, values, i, floor)
        if thr is None or abs(v) < thr:
            continue
        sign = 1 if v > 0 else -1
        if cur and cur["sign"] == sign and t - cur["end"] < EPISODE_GAP:
            cur["end"] = t
            cur["hours"] += 1
            if abs(v) > abs(cur["value"]):
                cur.update(at=t, value=v, threshold=thr, baseline_n=n, i=i)
            continue
        cur = {"start": t, "at": t, "end": t, "value": v, "threshold": thr, "baseline_n": n, "sign": sign,
               "hours": 1, "i": i}
        out.append(cur)
    return out


def repricings(events: list[dict]) -> list[dict]:
    """provider price_move events -> clusters of >= REPRICE_MIN_GPUS distinct GPUs, one direction, within 24h."""
    by = defaultdict(list)
    for e in events:
        if e.get("pct") is None or not e.get("provider"):
            continue
        by[(e["provider"], 1 if e["pct"] > 0 else -1)].append(e)
    out = []
    for (prov, sign), evs in sorted(by.items()):
        evs.sort(key=lambda e: (e["occurred_at"], e["gpu"] or ""))
        i = 0
        while i < len(evs):
            start = evs[i]["occurred_at"]
            j = i
            while j < len(evs) and evs[j]["occurred_at"] - start <= REPRICE_WINDOW:
                j += 1
            group = evs[i:j]
            gpus = {}
            for e in group:
                gpus.setdefault(e["gpu"], e)
            if len(gpus) >= REPRICE_MIN_GPUS:
                pcts = [e["pct"] for e in gpus.values()]
                out.append({"provider": prov, "sign": sign, "start": start, "at": max(e["occurred_at"] for e in group),
                            "gpus": sorted(gpus), "median_pct": statistics.median(pcts),
                            "events": [e["id"] for e in group]})
                i = j
            else:
                i += 1
    return out


def archived_changes(points: list[dict]) -> tuple[list[dict], list[dict]]:
    """Archived per-(provider, gpu) points {provider, gpu, t, price, source_url} -> (per-GPU changes, repricings).
    Consecutive documents of the same provider and GPU; a GPU change that is part of a repricing (>= 3 GPUs of
    the provider, same direction, same pair of capture times) is folded into it."""
    series = defaultdict(list)
    for p in points:
        series[(p["provider"], p["gpu"])].append(p)
    changes = []
    for (prov, gpu), pts in series.items():
        pts.sort(key=lambda p: p["t"])
        for a, b in zip(pts, pts[1:]):
            if a["price"] <= 0:
                continue
            ch = b["price"] / a["price"] - 1
            if abs(ch) >= ARCHIVED_MIN:
                changes.append({"provider": prov, "gpu": gpu, "from_t": a["t"], "t": b["t"], "from": a["price"],
                                "to": b["price"], "change": ch, "from_url": a["source_url"], "url": b["source_url"]})
    groups = defaultdict(list)
    for c in changes:
        groups[(c["provider"], c["from_t"], c["t"], c["change"] > 0)].append(c)
    singles, reprices = [], []
    for key, cs in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][2])):
        if len({c["gpu"] for c in cs}) >= REPRICE_MIN_GPUS:
            reprices.append({"provider": key[0], "from_t": key[1], "t": key[2], "changes": sorted(cs, key=lambda c: c["gpu"]),
                             "median": statistics.median(c["change"] for c in cs)})
        else:
            singles.extend(cs)
    return singles, reprices


# --------------------------------------------------------------------------- detection over the database

def _shift(key, kind, typ, at, move_start, window, mag, sev, title, detail, gpu=None, provider=None, index_id=None,
           direction=None) -> dict:
    """move_start..at is the move itself; `window` (default [move_start - BEFORE, at + AFTER]) is where related
    items are read."""
    w0, w1 = window or (move_start - BEFORE, at + AFTER)
    detail = {**detail, "move_start": _iso(move_start)}
    return {"shift_key": key[:300], "data_kind": kind, "type": typ, "segment": SEGMENT, "gpu": gpu,
            "provider": provider, "index_id": index_id, "at": at, "window_start": w0, "window_end": w1,
            "direction": direction or ("up" if (mag or 0) > 0 else "down"), "magnitude": mag, "severity": sev,
            "title": title, "detail": detail, "definition_version": DEFINITION_VERSION}


def detect_market_median(s, t0=None) -> list[dict]:
    rows = s.execute(text("""
        SELECT gpu, hour, chg24_median, providers_priced, median FROM market_gpu_hourly
        WHERE segment = :seg AND (CAST(:t0 AS timestamptz) IS NULL OR hour >= CAST(:t0 AS timestamptz) - interval '31 days')
        ORDER BY gpu, hour"""), {"seg": SEGMENT, "t0": t0}).all()
    by = defaultdict(list)
    for r in rows:
        by[r.gpu].append(r)
    out = []
    for gpu, rs in by.items():
        times = [r.hour for r in rs]
        vals = [float(r.chg24_median) if r.chg24_median is not None else None for r in rs]
        for ep in episodes(times, vals, MEDIAN_MIN, ok=lambda i: (rs[i].providers_priced or 0) >= MEDIAN_MIN_PROVIDERS):
            if t0 and ep["end"] < t0:
                continue
            r = rs[ep["i"]]
            start = ep["start"] - DAY  # the 24h change began a day before the first firing hour
            out.append(_shift(
                f"market_median|{SEGMENT}|{gpu}|{_iso(ep['start'])}", "observed", "market_median", ep["at"], start,
                None, ep["value"], _sev(ep["value"], ep["threshold"]),
                f"{gpu} market median {_pct(ep['value'])} in 24h (threshold {ep['threshold'] * 100:.1f}%)",
                {"rule": f"|chg24_median| >= max({MEDIAN_MIN:.0%}, {K:g} x trailing 30d RMS)", "threshold": ep["threshold"],
                 "baseline_hours": ep["baseline_n"], "episode_start": _iso(ep["start"]), "episode_end": _iso(ep["end"]),
                 "episode_hours": ep["hours"], "median_after": float(r.median) if r.median is not None else None,
                 "providers_priced": r.providers_priced, "basis": "coverage-matched (providers priced at both times)"},
                gpu=gpu))
    return out


def detect_index(s, t0=None) -> list[dict]:
    rows = s.execute(text("""
        SELECT index_id, hour, level, segment_no FROM index_levels
        WHERE published AND level IS NOT NULL AND (index_id = 'gpu-compute' OR index_id LIKE '%-class')
          AND (CAST(:t0 AS timestamptz) IS NULL OR hour >= CAST(:t0 AS timestamptz) - interval '32 days')
        ORDER BY index_id, hour"""), {"t0": t0}).all()
    by = defaultdict(list)
    for r in rows:
        by[r.index_id].append(r)
    out = []
    for iid, rs in by.items():
        times = [r.hour for r in rs]
        at = {r.hour: r for r in rs}
        vals = []
        for r in rs:
            p = at.get(r.hour - DAY)
            vals.append(float(r.level) / float(p.level) - 1 if p and p.segment_no == r.segment_no and p.level else None)
        for ep in episodes(times, vals, INDEX_MIN):
            if t0 and ep["end"] < t0:
                continue
            out.append(_shift(
                f"index_move|{iid}|{_iso(ep['start'])}", "observed", "index_move", ep["at"], ep["start"] - DAY, None,
                ep["value"], _sev(ep["value"], ep["threshold"]),
                f"Index {iid} {_pct(ep['value'])} in 24h (threshold {ep['threshold'] * 100:.1f}%)",
                {"rule": f"|24h level change| >= max({INDEX_MIN:.0%}, {K:g} x trailing 30d RMS), one chain segment",
                 "threshold": ep["threshold"], "baseline_hours": ep["baseline_n"], "episode_start": _iso(ep["start"]),
                 "episode_end": _iso(ep["end"]), "episode_hours": ep["hours"]},
                index_id=iid))
    return out


def _events(s, types, t0=None) -> list[dict]:
    rows = s.execute(text("""
        SELECT id, occurred_at, type, gpu, provider, severity, title, detail, pct FROM market_events
        WHERE type = ANY(:types) AND (segment = :seg OR segment IS NULL)
          AND (CAST(:t0 AS timestamptz) IS NULL OR occurred_at >= CAST(:t0 AS timestamptz) - interval '2 days')
        ORDER BY occurred_at"""), {"types": list(types), "seg": SEGMENT, "t0": t0}).all()
    return [dict(r._mapping) for r in rows]


def detect_repricing(s, t0=None) -> list[dict]:
    evs = [e for e in _events(s, ["price_move"], t0)
           if e["provider"] and (e["detail"] or {}).get("cause", "price_change") == "price_change"
           and (e["detail"] or {}).get("metric", "provider_lowest") == "provider_lowest"
           and provider_meta.meta(e["provider"]).provider_class not in EXCLUDED_CLASSES]
    out = []
    for c in repricings(evs):
        name = provider_meta.meta(c["provider"]).display_name
        verb = "raised" if c["sign"] > 0 else "cut"
        out.append(_shift(
            f"provider_repricing|{c['provider']}|{'up' if c['sign'] > 0 else 'down'}|{_iso(c['start'])}", "observed",
            "provider_repricing", c["at"], c["start"] - DAY, None, c["median_pct"],
            "major" if len(c["gpus"]) >= 2 * REPRICE_MIN_GPUS or abs(c["median_pct"]) >= MAJOR_ABS else "notable",
            f"{name} {verb} prices on {len(c['gpus'])} GPUs within 24h (median {_pct(c['median_pct'])})",
            {"rule": f">= {REPRICE_MIN_GPUS} GPUs, one direction, within 24h (market_events price_move)",
             "gpus": c["gpus"], "event_ids": c["events"]}, provider=c["provider"]))
    return out


def capacity_episodes(events: list[dict]) -> list[list[dict]]:
    """Qualifying capacity events grouped per (type, gpu): one that follows the previous within CAPACITY_GAP
    continues its episode (a GPU flapping in and out of stock is one collapse, not ten)."""
    keep = []
    for e in events:
        d = e["detail"] or {}
        if e["type"] == "market_capacity":
            if d.get("direction") == "tightening" and e["severity"] == "major":
                keep.append(e)
        elif e["provider"] is None and len(d.get("providers") or []) >= 2:
            keep.append(e)
    groups, last = [], {}
    for e in sorted(keep, key=lambda e: (e["occurred_at"], e["id"])):
        k = (e["type"], e["gpu"])
        g = last.get(k)
        if g and e["occurred_at"] - g[-1]["occurred_at"] <= CAPACITY_GAP:
            g.append(e)
        else:
            g = [e]
            groups.append(g)
            last[k] = g
    return groups


def detect_capacity(s, t0=None) -> list[dict]:
    out = []
    for g in capacity_episodes(_events(s, ["market_capacity", "sold_out"], t0 and t0 - CAPACITY_GAP)):
        first, last = g[0], g[-1]
        d = first["detail"] or {}
        mag = -max((e["detail"] or {}).get("share") or 0 for e in g) if first["type"] == "market_capacity" else None
        out.append(_shift(f"capacity_collapse|{first['type']}|{first['gpu'] or ''}|{_iso(first['occurred_at'])}",
                          "observed", "capacity_collapse", first["occurred_at"], first["occurred_at"] - DAY, None, mag,
                          "major", "Capacity collapse: " + first["title"] + (f" ({len(g)} times until {last['occurred_at']:%Y-%m-%d %H:%M})" if len(g) > 1 else ""),
                          {"rule": "market_capacity tightening (major), or sold out at every tracked provider (>= 2 "
                                   f"providers); repeats within {CAPACITY_GAP.days} days are one episode",
                           "event_ids": [e["id"] for e in g], "event": d, "episode_end": _iso(last["occurred_at"])},
                          gpu=first["gpu"], direction="down"))
    return out


def _archived_points(s) -> list[dict]:
    """Archived points from BEFORE OpenGrid recorded that provider and GPU live: once observed data exists the
    observed shifts cover it, and archived samples are never mixed into it (methodology/archived-history.md)."""
    rows = s.execute(text("""
        SELECT a.provider, a.gpu, a.observed_at AS captured, min(a.price_per_gpu_hour) AS price,
               min(a.source_url) AS source_url
        FROM archived_prices a
        LEFT JOIN (SELECT gpu, provider, min(hour) AS first_hour FROM market_hourly WHERE segment = :seg
                   GROUP BY gpu, provider) o ON o.gpu = a.gpu AND o.provider = a.provider
        WHERE a.gpu IS NOT NULL AND a.market_type = :seg AND (o.first_hour IS NULL OR a.observed_at < o.first_hour)
        GROUP BY a.provider, a.gpu, a.observed_at"""), {"seg": SEGMENT}).all()
    # Marketplace / decentralized rows are host asks, not one provider's list price: excluded, as for live shifts.
    return [{"provider": r.provider, "gpu": r.gpu, "t": r.captured, "price": float(r.price), "source_url": r.source_url}
            for r in rows if provider_meta.meta(r.provider).provider_class not in EXCLUDED_CLASSES]


def _archived_move(from_t, t):
    """(move start, window): the change happened between the captures (placed no better than ARCHIVED_MAX_GAP)."""
    start = max(from_t, t - ARCHIVED_MAX_GAP)
    return start, (start - ARCHIVED_PAD, t + ARCHIVED_PAD)


def detect_archived(s) -> list[dict]:
    if not s.execute(text("SELECT to_regclass('public.archived_prices') IS NOT NULL")).scalar():
        return []
    singles, reprices = archived_changes(_archived_points(s))
    out = []
    for c in singles:
        ms, win = _archived_move(c["from_t"], c["t"])
        name = provider_meta.meta(c["provider"]).display_name
        out.append(_shift(
            f"archived_price_change|{c['provider']}|{c['gpu']}|{_iso(c['from_t'])}|{_iso(c['t'])}", "archived", "archived_price_change",
            c["t"], ms, win, c["change"], _sev(c["change"], ARCHIVED_MIN),
            f"Archived list price: {name} {c['gpu']} {_pct(c['change'])} between captures "
            f"({c['from_t']:%Y-%m-%d} to {c['t']:%Y-%m-%d})",
            {"rule": f"lowest archived list price changed >= {ARCHIVED_MIN:.0%} between consecutive documents",
             "from": c["from"], "to": c["to"], "from_capture": _iso(c["from_t"]), "to_capture": _iso(c["t"]),
             "from_url": c["from_url"], "url": c["url"],
             "placement": "the change happened somewhere between the two captures"},
            gpu=c["gpu"], provider=c["provider"]))
    for r in reprices:
        ms, win = _archived_move(r["from_t"], r["t"])
        name = provider_meta.meta(r["provider"]).display_name
        out.append(_shift(
            f"archived_provider_repricing|{r['provider']}|{'up' if r['median'] > 0 else 'down'}|{_iso(r['from_t'])}|{_iso(r['t'])}", "archived", "archived_provider_repricing",
            r["t"], ms, win, r["median"], "major",
            f"Archived list prices: {name} changed {len(r['changes'])} GPUs (median {_pct(r['median'])}) between captures "
            f"({r['from_t']:%Y-%m-%d} to {r['t']:%Y-%m-%d})",
            {"rule": f">= {REPRICE_MIN_GPUS} GPUs of one provider changed >= {ARCHIVED_MIN:.0%} the same way between "
                     "the same two documents", "from_capture": _iso(r["from_t"]), "to_capture": _iso(r["t"]),
             "changes": [{k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in c.items()} for c in r["changes"]],
             "placement": "the change happened somewhere between the two captures"},
            provider=r["provider"]))
    return out


def detect_all(s, t0=None, archived: bool = True) -> list[dict]:
    out = detect_market_median(s, t0) + detect_index(s, t0) + detect_repricing(s, t0) + detect_capacity(s, t0)
    if archived:
        out += detect_archived(s)
    unique = {}
    for f in out:
        unique.setdefault(f["shift_key"], f)
    return list(unique.values())


# --------------------------------------------------------------------------- related items

def _vendor(gpu: str | None) -> str | None:
    if not gpu:
        return None
    g = gpu.lower()
    return "nvidia" if g.startswith("nvidia") else "amd" if g.startswith("amd") else "intel" if g.startswith("intel") else None


def _item_type(source_id: str) -> str:
    src = registry.get(source_id)
    if src is None:
        return "news"
    if src.category == "filings":
        return "filing"
    if src.trust_tier == "official" and src.category in ("provider", "hyperscaler", "vendor"):
        return "provider_announcement"
    return "news"


def _snapshot_news(n: dict) -> dict:
    return {"title": n["title"], "url": n["url"], "source_id": n["source_id"], "source_name": n["source_name"],
            "trust_tier": n["trust_tier"], "published_at": _iso(n["published_at"]), "relevance": n["relevance"],
            "topics": n["topics"], "backfilled": n.get("backfilled", False),
            "provenance": (n.get("provenance") or {}).get("method") if n.get("backfilled") else None,
            "source_count": n.get("source_count", 1)}


def _proximity(t: datetime, start: datetime, at: datetime, w0: datetime, w1: datetime) -> float:
    """1 inside the move [start, at]; falls linearly to 0 at the window edges."""
    if start <= t <= at:
        return 1.0
    if t < start:
        span = (start - w0).total_seconds()
        return round(max(0.0, 1 - (start - t).total_seconds() / span), 4) if span > 0 else 0.0
    span = (w1 - at).total_seconds()
    return round(max(0.0, 1 - (t - at).total_seconds() / span), 4) if span > 0 else 0.0


def _gpus_of(sh: dict) -> list[str]:
    if sh["gpu"]:
        return [sh["gpu"]]
    d = sh.get("detail") or {}
    if sh["type"] == "provider_repricing":
        return list(d.get("gpus") or [])[:12]
    if sh["type"] == "archived_provider_repricing":
        return sorted({c["gpu"] for c in d.get("changes") or []})[:12]
    if sh["type"] == "index_move" and sh.get("index_id", "").endswith("-class"):
        fam = cls.family_id(sh["index_id"].removesuffix("-class"))
        return [fam] if fam else []
    return []


def related_items(s, sh: dict) -> list[dict]:
    """Ranked candidates for one shift (pure read; nothing stored)."""
    at = sh["at"]
    ms = (sh.get("detail") or {}).get("move_start")
    start = datetime.fromisoformat(ms) if ms else at
    w0, w1 = sh["window_start"], sh["window_end"]
    cand: dict[tuple, dict] = {}

    def add(kind, ref, t, title, snap, base, base_name, match):
        p = _proximity(t, start, at, w0, w1)
        score = round(0.5 * base + 0.3 * p + 0.2 * MATCH_WEIGHT[match], 4)
        key = (kind if kind == "market_event" else "news", ref)
        if key in cand and cand[key]["score"] >= score:
            return
        cand[key] = {"item_type": kind, "ref_id": ref, "at": t, "title": title, "snapshot": snap, "match": match,
                     "hours_from_shift": round((t - at).total_seconds() / 3600, 2),
                     "position": "before" if t <= at else "after", "score": score,
                     "score_components": {base_name: round(base, 4), "proximity": p, "match_weight": MATCH_WEIGHT[match],
                                          "formula": f"0.5 * {base_name} + 0.3 * proximity + 0.2 * match_weight"}}

    def add_news(rows, match):
        for n in rows:
            add(_item_type(n["source_id"]), n["id"], n["published_at"], n["title"], _snapshot_news(n),
                n["relevance"] / 100, "relevance", match)

    for g in _gpus_of(sh):
        add_news(news_store.related(gpu=g, t0=w0, t1=w1, limit=60, min_relevance=MIN_RELEVANCE), "gpu")
    if sh["provider"]:
        add_news(news_store.related(provider=sh["provider"], t0=w0, t1=w1, limit=60, min_relevance=MIN_RELEVANCE), "provider")
    # SEC filings: the GPU's vendor, or the provider itself (entity hints on the filing source).
    vendor = _vendor(sh["gpu"] or (_gpus_of(sh) or [None])[0])
    filing_srcs = [x.id for x in registry.SOURCES.values() if x.category == "filings" and (
        (vendor and x.entity_hints.get("vendor") == vendor) or (sh["provider"] and x.entity_hints.get("provider") == sh["provider"])
        or (sh["type"] in ("index_move", "capacity_collapse") and x.entity_hints.get("vendor")))]
    for sid in filing_srcs:
        src = registry.get(sid)
        match = "provider" if sh["provider"] and src.entity_hints.get("provider") == sh["provider"] else "vendor"
        rows, _ = news_store.list_stories(limit=20, source=sid, t0=w0, t1=w1, min_relevance=0)
        add_news(rows, match)
    # Market-wide items: high relevance on market-moving topics, whatever entity they name.
    for topic in MARKET_WIDE_TOPICS:
        rows, _ = news_store.list_stories(limit=15, topic=topic, t0=w0, t1=w1, min_relevance=MARKET_WIDE_MIN_RELEVANCE)
        add_news(rows, "market-wide")
    # OpenGrid's own market events (observed shifts only: archived shifts predate them).
    if sh["data_kind"] == "observed":
        own = set((sh.get("detail") or {}).get("event_ids") or []) | {(sh.get("detail") or {}).get("event_id")}
        evs = s.execute(text("""
            SELECT id, occurred_at, type, gpu, provider, severity, title FROM market_events
            WHERE occurred_at BETWEEN :w0 AND :w1 AND severity IN ('notable', 'major')
              AND type NOT IN ('coverage_started', 'provider_feed_recovered')
              AND ((CAST(:gpus AS text[]) IS NOT NULL AND gpu = ANY(CAST(:gpus AS text[])))
                   OR (CAST(:prov AS text) IS NOT NULL AND provider = :prov)
                   OR (gpu IS NULL AND provider IS NULL))
            ORDER BY occurred_at LIMIT 400"""), {"w0": w0, "w1": w1, "gpus": [g for g in _gpus_of(sh) if " " in g] or None,
                                                 "prov": sh["provider"]}).all()
        for e in evs:
            if e.id in own:
                continue
            match = "gpu" if e.gpu and e.gpu in _gpus_of(sh) else "provider" if e.provider and e.provider == sh["provider"] \
                else "market-wide"
            add("market_event", e.id, e.occurred_at, e.title,
                {"title": e.title, "type": e.type, "severity": e.severity, "gpu": e.gpu, "provider": e.provider,
                 "occurred_at": _iso(e.occurred_at)}, SEVERITY_WEIGHT.get(e.severity, 0.4), "severity_weight", match)
    order = lambda c: (-c["score"], abs(c["hours_from_shift"]), c["item_type"], c["ref_id"])  # noqa: E731
    # OpenGrid's own events are capped so they never crowd out the outside items (they are often the same move
    # seen from another angle); news / announcements / filings fill the rest.
    events = sorted((c for c in cand.values() if c["item_type"] == "market_event"), key=order)[:MAX_EVENTS]
    outside = sorted((c for c in cand.values() if c["item_type"] != "market_event"), key=order)[:MAX_ITEMS - len(events)]
    return sorted(events + outside, key=order)


def _store_items(s, shift_id: int, items: list[dict], now: datetime) -> None:
    s.execute(delete(MarketShiftItem).where(MarketShiftItem.shift_id == shift_id))
    if items:
        s.execute(insert(MarketShiftItem).values([
            {"shift_id": shift_id, "rank": i + 1, **{k: c[k] for k in ("item_type", "ref_id", "at", "hours_from_shift",
                                                                    "position", "score", "score_components", "snapshot",
                                                                    "match")}}
            for i, c in enumerate(items)]))
    s.execute(text("UPDATE market_shifts SET items_version = :v, items_computed_at = :t WHERE id = :id"),
              {"v": ITEMS_VERSION, "t": now, "id": shift_id})


# --------------------------------------------------------------------------- run

_CHANGING = ("at", "window_start", "window_end", "magnitude", "severity", "title", "detail", "direction",
             "definition_version")


def run(now: datetime | None = None, t0: datetime | None = None, archived: bool = True, rebuild_items: bool = False) -> dict:
    """Detect (idempotent upsert on shift_key), then (re)compute stored items where they may still change."""
    now = now or datetime.now(UTC)
    with normalize.SessionLocal.begin() as s:
        found = detect_all(s, t0, archived=archived)
        existing = {r.shift_key: r for r in s.scalars(select(MarketShift).where(
            MarketShift.shift_key.in_([f["shift_key"] for f in found])))} if found else {}
        new = changed = 0
        for f in found:
            row = existing.get(f["shift_key"])
            if row is None:
                s.add(MarketShift(**f, detected_at=now))
                new += 1
                continue
            if any(getattr(row, k) != f[k] for k in _CHANGING):
                for k in _CHANGING:
                    setattr(row, k, f[k])
                row.items_version = None  # the shift itself changed: its items are recomputed
                changed += 1
    items = recompute_items(now, force=rebuild_items)
    return {"detected": len(found), "new": new, "updated": changed, **items}


def recompute_items(now: datetime | None = None, force: bool = False, shift_ids: list[int] | None = None) -> dict:
    now = now or datetime.now(UTC)
    done = 0
    with normalize.SessionLocal() as s:
        q = select(MarketShift.id)
        if shift_ids:
            q = q.where(MarketShift.id.in_(shift_ids))
        elif not force:
            q = q.where((MarketShift.items_version.is_(None)) | (MarketShift.items_version != ITEMS_VERSION)
                        | (MarketShift.window_end > now - SETTLE))
        ids = list(s.scalars(q.order_by(MarketShift.id)))
    for sid in ids:
        with normalize.SessionLocal.begin() as s:
            sh = s.get(MarketShift, sid)
            d = {c: getattr(sh, c) for c in ("id", "data_kind", "type", "gpu", "provider", "index_id", "at", "window_start",
                                           "window_end", "detail")}
            _store_items(s, sid, related_items(s, d), now)
            done += 1
    return {"items_recomputed": done}


# --------------------------------------------------------------------------- readers

def shift_dict(r: MarketShift, items: list | None = None, top: list | None = None) -> dict:
    d = {"id": r.id, "key": r.shift_key, "kind": r.data_kind, "type": r.type, "type_label": TYPES.get(r.type, ("", ""))[1],
         "segment": r.segment, "gpu": r.gpu, "provider": r.provider, "index_id": r.index_id, "at": r.at,
         "window_start": r.window_start, "window_end": r.window_end, "direction": r.direction, "magnitude": r.magnitude,
         "severity": r.severity, "title": r.title, "detail": r.detail, "definition_version": r.definition_version,
         "detected_at": r.detected_at, "items_version": r.items_version, "items_computed_at": r.items_computed_at}
    if r.data_kind == "archived":
        d["label"] = "archived: list prices recovered from public archives (methodology/archived-history), never used in statistics"
    if items is not None:
        d["heading"] = NOT_CAUSAL
        d["items"] = items
    if top is not None:
        d["top_items"] = top
    return d


def _item(r: MarketShiftItem) -> dict:
    return {"rank": r.rank, "item_type": r.item_type, "ref_id": r.ref_id, "at": r.at, "hours_from_shift": r.hours_from_shift,
            "position": r.position, "score": r.score, "score_components": r.score_components, "match": r.match,
            **r.snapshot}


def query(gpu=None, provider=None, kind=None, types=None, since=None, until=None, severity=None, limit=50, offset=0,
          top_n: int = 3) -> tuple[list[dict], int]:
    from sqlalchemy import func
    w = []
    if gpu:
        w.append(MarketShift.gpu == gpu)
    if provider:
        w.append(MarketShift.provider == provider)
    if kind:
        w.append(MarketShift.data_kind == kind)
    if types:
        w.append(MarketShift.type.in_(types))
    if since:
        w.append(MarketShift.at >= since)
    if until:
        w.append(MarketShift.at <= until)
    if severity:
        w.append(MarketShift.severity == severity)
    with normalize.SessionLocal() as s:
        total = s.execute(select(func.count()).select_from(MarketShift).where(*w)).scalar() or 0
        rows = list(s.scalars(select(MarketShift).where(*w).order_by(MarketShift.at.desc(), MarketShift.id.desc())
                              .limit(limit).offset(offset)))
        tops, counts = defaultdict(list), defaultdict(lambda: defaultdict(int))
        if rows and top_n:
            # The list shows the best outside items first (news, announcements, filings), then OpenGrid's events.
            items = list(s.scalars(select(MarketShiftItem).where(MarketShiftItem.shift_id.in_([r.id for r in rows]))
                                   .order_by(MarketShiftItem.shift_id, MarketShiftItem.rank)))
            by = defaultdict(list)
            for it in items:
                by[it.shift_id].append(it)
                counts[it.shift_id][it.item_type] += 1
            for sid, its in by.items():
                pick = [i for i in its if i.item_type != "market_event"][:top_n]
                pick += [i for i in its if i.item_type == "market_event"][:max(0, top_n - len(pick))]
                tops[sid] = [_item(i) for i in pick]
    return [{**shift_dict(r, top=tops.get(r.id, [])), "item_counts": dict(counts.get(r.id, {}))} for r in rows], int(total)


def get(shift_id: int) -> dict | None:
    with normalize.SessionLocal() as s:
        r = s.get(MarketShift, shift_id)
        if r is None:
            return None
        items = [_item(i) for i in s.scalars(select(MarketShiftItem).where(MarketShiftItem.shift_id == shift_id)
                                             .order_by(MarketShiftItem.rank))]
        return shift_dict(r, items=items)


def definition() -> dict:
    return {"version": DEFINITION_VERSION, "items_version": ITEMS_VERSION, "heading": NOT_CAUSAL, "note": NOTE,
            "types": {k: {"kind": v[0], "label": v[1]} for k, v in TYPES.items()},
            "thresholds": {"k_times_rms": K, "baseline_days": BASELINE.days, "baseline_min_hours": BASELINE_MIN,
                           "median_min": MEDIAN_MIN, "median_min_providers": MEDIAN_MIN_PROVIDERS, "index_min": INDEX_MIN,
                           "reprice_min_gpus": REPRICE_MIN_GPUS, "archived_min": ARCHIVED_MIN,
                           "major": f">= {MAJOR_MULT:g}x threshold or >= {MAJOR_ABS:.0%}"},
            "window": {"before_hours": BEFORE.total_seconds() / 3600, "after_hours": AFTER.total_seconds() / 3600,
                       "archived_pad_days": ARCHIVED_PAD.days, "settle_days": SETTLE.days, "max_items": MAX_ITEMS},
            "ranking": "0.5 * relevance/100 (news) or severity weight (market events) + 0.3 * proximity + 0.2 * match "
                       "weight (gpu / provider 1.0, vendor 0.7, market-wide 0.4)",
            "segment": SEGMENT}


@job("market_shifts", every_seconds=15 * 60, initial_delay_seconds=240)
def _shifts_job():
    # Last 10 days are re-detected each run (episodes still open, rollups rewritten); older shifts are kept.
    return run(t0=datetime.now(UTC) - timedelta(days=10))


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m analytics.shifts")
    ap.add_argument("cmd", choices=["run", "rebuild-items", "list"])
    ap.add_argument("--no-archived", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "run":
        print(run(archived=not a.no_archived))
    elif a.cmd == "rebuild-items":
        print(recompute_items(force=True))
    else:
        rows, total = query(limit=200, top_n=3)
        for r in rows:
            print(f"{r['at']:%Y-%m-%d %H:%M} {r['kind']:8} {r['type']:28} {r['severity']:7} {r['title']}")
            for it in r["top_items"]:
                print(f"      {it['hours_from_shift']:+7.1f}h {it['item_type']:21} {it['score']:.2f} {it['title'][:100]}")
        print(total, "shifts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
