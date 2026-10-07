"""Market events: structured, deduplicated "what just happened" records.

The detector reads the hourly rollup (market_hourly), never raw observations, and
compares each new hour with the hour before, 24 hours before and the last 30 days.
Every event carries a severity, a human title and the numbers behind it in
`detail`. Methodology: methodology/events.md.

Coverage honesty (the reason most of this module exists):
    A provider we only began recording is not news. "Coverage start" is the
    earlier of its first successful fetch and its first rollup hour. A (gpu,
    provider) pair whose first hour is within GRACE of coverage start was
    already on sale when we started looking: that is `coverage_started`, never
    `provider_added_gpu`, and it joins market-level figures (lowest, median,
    cheapest, spread, regions) only after WARMUP. Market comparisons between two
    times use one provider set (established at the later time), so a provider
    entering the set cannot look like a price drop. Pairs whose listings were
    first seen long before their first rollup hour (a mapping fix, a rebuilt
    rollup) are treated the same way.

Idempotency: each event has a `dedupe_key` (type, segment, gpu, provider,
qualifier, hour); inserts are ON CONFLICT DO NOTHING, so running the detector
twice, or over overlapping hours, writes the same rows once. Cooldowns are
evaluated against events strictly before the hour, so a re-run decides the same.

Watermarks live in event_detector_state. The first run backfills the cross-
provider table (market_gpu_hourly) over all rollup history and events over the
last BACKFILL days; later runs redo the last OVERLAP hours (the rollup rewrites
them) and then the new ones.
"""

from __future__ import annotations

import bisect
import logging
import statistics
import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert

import market
import normalize
import provider_meta
from analytics import rollups
from jobs import job
from providers import PROVIDERS
from store.events import MarketEvent, MarketGpuHourly

log = logging.getLogger(__name__)

SEGMENT = "on_demand"
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)

# --- thresholds (documented in methodology/events.md; change both together) ---
PROVIDER_MOVE = 0.10          # provider's lowest price, vs previous hour or 24h ago
MOVE_NOTABLE, MOVE_MAJOR = 0.15, 0.25
MARKETPLACE_MAJOR = 0.40      # marketplace provider moves are capped at notable unless they exceed 40%
MARKET_MOVE = 0.05            # market median (coverage-matched) vs 24h ago
MARKET_MOVE_MAJOR = 0.10
MIN_USD = float(normalize.CHANGE_MIN_USD)
RECORD_MARGIN = float(normalize.CHANGE_MIN_PCT)   # a record must beat the old one by 0.5%
RECORD_WINDOW = timedelta(days=30)
RECORD_FILL = 0.8             # share of the 30 days that must have a market price
ALL_TIME_MIN = timedelta(days=14)
CHEAPEST_MARGIN = 0.005
CHEAPEST_NOTABLE = 0.05
CHEAPEST_COOLDOWN = timedelta(hours=6)
SPREAD_MIN_PROVIDERS = 3
SPREAD_HISTORY_MIN = timedelta(days=14)
SPREAD_ABS = 3.0              # highest / lowest - 1, when there is no 14-day baseline
SPREAD_PCTL = 0.95
SPREAD_P95_MULT = 1.25       # must exceed its usual extreme by 25%: a bare p95 is crossed 5% of hours
REGION_GAP, REGION_NOTABLE = 0.20, 0.35
REGION_MIN_PROVIDERS = 2
CAPACITY_MIN_PAIRS, CAPACITY_SHARE, CAPACITY_MAJOR = 3, 0.20, 0.40
FEED_DOWN_ROUNDS = 3
FEED_LOOKBACK = timedelta(hours=6)   # re-read before the watermark: covers a streak not yet 3 rounds long
COOLDOWN = DAY
GRACE = timedelta(hours=2)    # a pair first seen this close to coverage start was already on sale
WARMUP = DAY                  # a newly covered provider joins market figures after this
BACKFILL = timedelta(days=90)
OVERLAP = timedelta(hours=3)  # = rollups.OVERLAP: those hours are rewritten each refresh
CHUNK = timedelta(days=7)
LEAD = timedelta(hours=26)    # rows before a chunk needed for 24h comparisons

SEVERITIES = ("info", "notable", "major")
_OBS, _INF = "observed", "inferred"

# type -> (kind, what fires it, severity rule). Served by /v1/events/types; mirrored in methodology/events.md.
TYPES = {
    "price_move": (_OBS, "A provider's lowest price for a GPU moved >= 10% (and >= $0.001) vs the previous hour, "
                   "or vs 24h ago (24h basis: at most once per 24h per provider and GPU); or the coverage-matched "
                   "market median moved >= 5% in 24h (once per 24h per GPU).",
                   "provider: info <15%, notable >=15%, major >=25%. market median: notable >=5%, major >=10%. "
                   "Marketplace providers (provider_meta class 'marketplace', e.g. Vast's median-of-host-asks row): "
                   "the move must hold for 2 consecutive hourly samples (title says 'held 2h'), and severity is "
                   "capped at notable unless the move exceeds 40%."),
    "sold_out": (_OBS, "Every live listing a provider has for a GPU went sold out (provider=null: every established "
                 "provider is sold out).", "info; notable if that provider was the cheapest; major market-wide."),
    "capacity_returned": (_OBS, "A provider (or the whole market) that was sold out of a GPU has purchasable "
                          "listings again.", "info; notable if it is now the cheapest, or market-wide."),
    "provider_added_gpu": (_INF, "A provider we were already tracking started listing a GPU: new listings first "
                           "seen now, at least 2h after our coverage of that provider began.",
                           "info; notable if it undercuts every established provider."),
    "provider_removed_gpu": (_INF, "A provider's listings for a GPU were gone for 2 consecutive hours while its feed "
                             "kept working.", "info; notable if it was the cheapest or the only priced provider."),
    "new_cheapest_provider": (_OBS, "A different provider has the lowest price for a GPU, judged on one fixed set of "
                              "established providers at both hours; >= 0.5% below the previous cheapest; at most "
                              "once per 6h per GPU.", "info; notable if the new floor is >= 5% below the old one."),
    "new_30d_low": (_OBS, "Market lowest (or median) below every hour of the previous 30 days by >= 0.5%. Needs 30 "
                    "days of history with >= 80% of hours priced.", "lowest: notable; median: info."),
    "new_30d_high": (_OBS, "Market lowest (or median) above every hour of the previous 30 days by >= 0.5%. Same "
                     "history requirement.", "lowest: notable; median: info."),
    "new_all_time_low": (_OBS, "Market lowest below every hour since tracking began (>= 14 days ago) by >= 0.5%. "
                         "Labelled with the tracking start date.", "major."),
    "new_all_time_high": (_OBS, "Market lowest above every hour since tracking began (>= 14 days ago) by >= 0.5%.",
                          "major."),
    "spread_anomaly": (_INF, "Highest/lowest - 1 across >= 3 established providers crossed (from below) its own "
                       "30-day p95 x 1.25, or 300% when there is no 14-day baseline. Once per 24h per GPU.",
                       "notable; major at >= 1.5x the threshold."),
    "regional_dislocation": (_INF, "A region group's median provider price crossed >= 20% away from the global "
                             "median (>= 2 providers inside and outside the group). Region groups come from "
                             "regions.region_group and are never guessed.", "info; notable at >= 35%."),
    "market_capacity": (_INF, "Market-wide: >= max(3, 20% of live markets) (gpu, provider) markets sold out (or "
                        "came back) within 24h. Once per 24h per direction.", "notable; major at >= 40%."),
    "provider_feed_down": (_OBS, "3 consecutive poll rounds of a provider failed outright, after it had worked.",
                           "notable."),
    "provider_feed_recovered": (_OBS, "A provider's feed succeeded again after a provider_feed_down streak.", "info."),
    "coverage_started": (_OBS, "OpenGrid began recording a provider. Not a market event: shown so its appearance is "
                         "not mistaken for one.", "info."),
}

_lock = threading.Lock()


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _name(p: str | None) -> str:
    return provider_meta.meta(p).display_name if p else ""


def _usd(x: float | None) -> str:
    if x is None:
        return "n/a"
    return f"${x:,.2f}/hr" if x >= 0.1 else f"${x:,.3f}/hr"


def _pct(x: float) -> str:
    return f"{abs(x) * 100:.1f}%"


def _iso(t):
    return None if t is None else t.isoformat()


def _priced(r) -> bool:
    return r is not None and r["priced_listings"] > 0 and r["min_price"] is not None


def _sold_out(r) -> bool:
    return r is not None and r["live_listings"] > 0 and r["priced_listings"] == 0 and r["sold_out_listings"] > 0


def _median(vals):
    return statistics.median(vals) if vals else None


def _matched(a: dict, b: dict, agg):
    """agg over providers priced at both times: (then, now) or (None, None)."""
    common = [p for p in a if p in b]
    if not common:
        return None, None
    return agg([b[p] for p in common]), agg([a[p] for p in common])


def _chg(then, now):
    if then is None or now is None or then <= 0:
        return None
    return (now - then) / then


# --------------------------------------------------------------------------
# Context: coverage start, first hours, listing first-seen times
# --------------------------------------------------------------------------

class Context:
    """Everything the coverage rules need, loaded once per run."""

    def __init__(self, first, cov_start, pair_seen, segment=SEGMENT):
        self.first = first                  # {(gpu, provider): first rollup hour}
        self.cov_start = cov_start          # {provider: coverage start}
        self.pair_seen = pair_seen          # {(gpu, provider): sorted first_seen_at of its listings}
        self.segment = segment

    def genuine_pair(self, g, p) -> bool:
        """The pair appeared after we were already watching the provider, with fresh listings."""
        fh, cs = self.first.get((g, p)), self.cov_start.get(p)
        if fh is None or cs is None or fh <= rollups.floor_hour(cs) + GRACE:
            return False
        seen = self.pair_seen.get((g, p))
        return bool(seen) and seen[0] >= fh - GRACE - HOUR

    def established(self, g, p, h) -> bool:
        fh = self.first.get((g, p))
        return fh is not None and (fh <= h - WARMUP or self.genuine_pair(g, p))

    def fresh_listing(self, g, p, h) -> bool:
        """Some listing of the pair was first seen just before h (it is new, not newly mapped)."""
        seen = self.pair_seen.get((g, p)) or []
        i = bisect.bisect_right(seen, h - HOUR - GRACE)
        return i < len(seen) and seen[i] <= h + timedelta(minutes=5)


def _coverage_start(s, first) -> dict:
    """{provider: earliest of first ok fetch and first rollup hour}. Stored once known (it never moves)."""
    st = _get_state(s, "coverage_start")
    known = dict((st or {}).get("info") or {})
    providers = {p for _, p in first} | set(PROVIDERS)
    missing = providers - set(known)
    for p in missing:
        t = s.execute(text("SELECT min(fetched_at) FROM raw_snapshots WHERE provider = :p AND ok"), {"p": p}).scalar()
        if t is not None:
            known[p] = t.isoformat()
    if missing:
        _set_state(s, "coverage_start", None, known)
    out = {p: datetime.fromisoformat(v) for p, v in known.items()}
    for (g, p), h in first.items():
        if p not in out or h < out[p]:
            out[p] = h
    return out


SEP = "|"  # gpu|provider keys in stored JSON (neither name contains "|")


def _first_hours(s, segment, save: bool) -> dict:
    """rollups.first_hours, kept incrementally in event_detector_state.

    A pair's first hour only moves earlier (a rebuilt rollup), so the stored map is merged with the
    minimum over recent hours instead of re-aggregating all of market_hourly on every call.
    """
    name = f"first_hours:{segment}"
    st = _get_state(s, name)
    if st is None or st["watermark"] is None:
        first = rollups.first_hours(segment)
    else:
        first = {tuple(k.split(SEP, 1)): datetime.fromisoformat(v) for k, v in (st["info"] or {}).items()}
        for r in s.execute(text("""
            SELECT gpu, provider, min(hour) AS first FROM market_hourly WHERE segment = :s AND hour >= :t
            GROUP BY gpu, provider
        """), {"s": segment, "t": st["watermark"] - OVERLAP}):
            k = (r.gpu, r.provider)
            if k not in first or r.first < first[k]:
                first[k] = r.first
    if save and first:
        _set_state(s, name, max(first.values()) if st is None or st["watermark"] is None
                   else max(st["watermark"], max(first.values())),
                   {f"{g}{SEP}{p}": h.isoformat() for (g, p), h in first.items()})
    return first


def load_context(s, segment=SEGMENT, save: bool = False) -> Context:
    first = _first_hours(s, segment, save)
    cov = _coverage_start(s, first)
    seen: dict[tuple, list] = defaultdict(list)
    for r in s.execute(text(f"SELECT c.canonical_gpu_name AS gpu, c.provider, c.first_seen_at FROM compute_listings c "
                            f"WHERE {market._ELIGIBLE}")):
        seen[(r.gpu, r.provider)].append(r.first_seen_at)
    for v in seen.values():
        v.sort()
    return Context(first, cov, dict(seen), segment)


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

def _get_state(s, name):
    r = s.execute(text("SELECT watermark, info FROM event_detector_state WHERE name = :n"), {"n": name}).first()
    return None if r is None else {"watermark": r.watermark, "info": r.info}


def _set_state(s, name, watermark, info=None):
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy import bindparam
    s.execute(text("""
        INSERT INTO event_detector_state (name, watermark, updated_at, info) VALUES (:n, :w, now(), :i)
        ON CONFLICT (name) DO UPDATE SET watermark = excluded.watermark, updated_at = now(), info = excluded.info
    """).bindparams(bindparam("i", type_=JSONB)), {"n": name, "w": watermark, "i": info})


# --------------------------------------------------------------------------
# Loading rows
# --------------------------------------------------------------------------

class Rows:
    """Rollup rows for a time window, indexed for the detector."""

    def __init__(self, rows):
        self.by_gh: dict[tuple, dict] = defaultdict(dict)   # (gpu, hour) -> {provider: row}
        self.alive: set[tuple] = set()                      # (provider, hour) with any row
        self.gpus: set[str] = set()
        for r in rows:
            self.by_gh[(r["gpu"], r["hour"])][r["provider"]] = r
            self.alive.add((r["provider"], r["hour"]))
            self.gpus.add(r["gpu"])
        self.raw_ok: set[tuple] = set()                     # (provider, hour bucket) with an ok fetch

    def at(self, g, h) -> dict:
        return self.by_gh.get((g, h), {})

    def provider_alive(self, p, h) -> bool:
        return (p, h) in self.alive or (p, h - HOUR) in self.raw_ok


def _load_rows(s, segment, t0, t1) -> Rows:
    rows = Rows(rollups.provider_hourly(segment, None, None, t0, t1))
    for r in s.execute(text("""
        SELECT provider, date_trunc('hour', fetched_at) AS b FROM raw_snapshots
        WHERE ok AND fetched_at >= :t0 AND fetched_at <= :t1 GROUP BY 1, 2
    """), {"t0": t0 - HOUR, "t1": t1}):
        rows.raw_ok.add((r.provider, r.b))
    return rows


# --------------------------------------------------------------------------
# Pass 1: the cross-provider table (market_gpu_hourly)
# --------------------------------------------------------------------------

def gpu_hour_row(segment, g, h, rows: Rows) -> dict:
    now, p1, p24 = rows.at(g, h), rows.at(g, h - HOUR), rows.at(g, h - DAY)
    prices = {p: r["min_price"] for p, r in now.items() if _priced(r)}
    pr1 = {p: r["min_price"] for p, r in p1.items() if _priced(r)}
    pr24 = {p: r["min_price"] for p, r in p24.items() if _priced(r)}
    vals = list(prices.values())
    m1 = _matched(prices, pr1, _median)
    m24 = _matched(prices, pr24, _median)
    l24 = _matched(prices, pr24, min)
    two = lambda a, b: len([p for p in a if p in b]) >= 2  # noqa: E731
    return {
        "segment": segment, "gpu": g, "hour": h,
        "providers_live": len(now), "providers_priced": len(prices),
        "providers_available": sum(1 for r in now.values() if r["available_listings"] > 0),
        "live_listings": sum(r["live_listings"] for r in now.values()),
        "priced_listings": sum(r["priced_listings"] for r in now.values()),
        "available_listings": sum(r["available_listings"] for r in now.values()),
        "sold_out_listings": sum(r["sold_out_listings"] for r in now.values()),
        "lowest": min(vals) if vals else None, "median": _median(vals), "highest": max(vals) if vals else None,
        "chg1h_median": _chg(*m1) if two(prices, pr1) else None,
        "chg24_median": _chg(*m24) if two(prices, pr24) else None,
        "chg24_lowest": _chg(*l24),
    }


def _write_table(s, segment, out: list[dict], h0, h1):
    s.execute(text("DELETE FROM market_gpu_hourly WHERE segment = :s AND hour >= :h0 AND hour <= :h1"),
              {"s": segment, "h0": h0, "h1": h1})
    for i in range(0, len(out), 2000):
        s.execute(insert(MarketGpuHourly).values(out[i:i + 2000]))


def _table_history(s, segment, t0, t1) -> dict[str, list]:
    """{gpu: [(hour, lowest, median, highest)]} ascending, from market_gpu_hourly."""
    out: dict[str, list] = defaultdict(list)
    for r in s.execute(text("""
        SELECT gpu, hour, lowest, median, highest FROM market_gpu_hourly
        WHERE segment = :s AND hour >= :t0 AND hour <= :t1 ORDER BY gpu, hour
    """), {"s": segment, "t0": t0, "t1": t1}):
        out[r.gpu].append((r.hour, _f(r.lowest), _f(r.median), _f(r.highest)))
    return out


def _f(v):
    return None if v is None else float(v)


# --------------------------------------------------------------------------
# Pass 2: event detection
# --------------------------------------------------------------------------

class Emitter:
    """Collects events, applies cooldowns against earlier events, builds dedupe keys."""

    def __init__(self, prior: list[dict], segment):
        self.out: list[dict] = []
        self.segment = segment
        self.seen: dict[tuple, list] = defaultdict(list)
        for e in prior:
            self.seen[self._ck(e)].append(e["occurred_at"])

    @staticmethod
    def _ck(e):
        d = e.get("detail") or {}
        return (e["type"], e.get("gpu"), e.get("provider"), e.get("region_group"), d.get("metric"), d.get("direction"))

    def cooling(self, e, window) -> bool:
        t = e["occurred_at"]
        return any(t - window <= x < t for x in self.seen.get(self._ck(e), ()))

    def emit(self, *, type, at, severity, title, gpu=None, provider=None, region_group=None, detail=None,
             before=None, after=None, pct=None, qualifier="", cooldown=None, segment="__default__"):
        seg = self.segment if segment == "__default__" else segment
        e = {"type": type, "occurred_at": at, "segment": seg, "gpu": gpu, "provider": provider,
             "region_group": region_group, "severity": severity, "title": title, "detail": detail or {},
             "value_before": before, "value_after": after, "pct": pct}
        if cooldown and self.cooling(e, cooldown):
            return None
        e["dedupe_key"] = "|".join([type, seg or "", gpu or "", provider or "", region_group or "",
                                    qualifier, at.isoformat()])[:300]
        self.seen[self._ck(e)].append(at)
        self.out.append(e)
        return e


def _sev_move(pct, notable=MOVE_NOTABLE, major=MOVE_MAJOR):
    a = abs(pct)
    return "major" if a >= major else "notable" if a >= notable else "info"


def _marketplace(p: str | None) -> bool:
    """Many independent hosts set the price (provider_meta class 'marketplace'): require persistence."""
    return bool(p) and provider_meta.meta(p).provider_class == "marketplace"


def _moved(a, b, pct) -> bool:
    return pct is not None and abs(pct) >= PROVIDER_MOVE and abs(b - a) >= MIN_USD


def _held(a, mid, pct) -> bool:
    """The intermediate sample already showed a move of the same direction past the threshold."""
    m = _chg(a, mid)
    return _moved(a, mid, m) and (m > 0) == (pct > 0)


def _sev_provider(pct, marketplace: bool) -> str:
    sev = _sev_move(pct)
    if marketplace and sev == "major" and abs(pct) <= MARKETPLACE_MAJOR:
        return "notable"
    return sev


def detect_hour(g, h, rows: Rows, ctx: Context, hist: list, em: Emitter, region_fn=None, regional=None,
                gh_row: dict | None = None):
    """All per-GPU events at hour h.

    `hist` is market_gpu_hourly for g, ascending, up to and including h; `gh_row` is h's own row.
    """
    now, p1, p2, p24 = rows.at(g, h), rows.at(g, h - HOUR), rows.at(g, h - 2 * HOUR), rows.at(g, h - DAY)
    G = g

    # ---- provider level -------------------------------------------------------------
    for p in sorted(set(now) | set(p1) | set(p2)):
        rn, r1, r2 = now.get(p), p1.get(p), p2.get(p)
        cs = ctx.cov_start.get(p)
        watched = cs is not None and cs <= h - GRACE

        if rn and not r1 and watched and rows.provider_alive(p, h - HOUR) and ctx.fresh_listing(g, p, h):
            others = [r["min_price"] for q, r in now.items() if q != p and _priced(r) and ctx.established(g, q, h)]
            price = rn["min_price"] if _priced(rn) else None
            cheapest = price is not None and (not others or price < min(others))
            relisted = ctx.first.get((g, p)) is not None and ctx.first[(g, p)] < h
            if price is not None:
                title = f"{_name(p)} started selling {G} at {_usd(price)}"
            else:
                title = f"{_name(p)} listed {G} (no purchasable price yet)"
            em.emit(type="provider_added_gpu", at=h, gpu=g, provider=p, after=price,
                    severity="notable" if cheapest else "info", title=title,
                    detail={"price": price, "listings": rn["live_listings"], "priced_listings": rn["priced_listings"],
                            "cheapest": cheapest, "relisted": relisted, "other_providers": len(others),
                            "coverage_start": _iso(cs)})

        if r2 and not r1 and not rn and rows.provider_alive(p, h - HOUR) and rows.provider_alive(p, h) \
                and ctx.pair_seen.get((g, p)) and watched:
            prev = {q: r["min_price"] for q, r in p2.items() if _priced(r)}
            was_cheapest = p in prev and prev[p] == min(prev.values())
            em.emit(type="provider_removed_gpu", at=h - HOUR, gpu=g, provider=p,
                    before=prev.get(p), severity="notable" if (was_cheapest or len(prev) <= 1) else "info",
                    title=f"{_name(p)} stopped listing {G}",
                    detail={"last_price": prev.get(p), "listings_before": r2["live_listings"],
                            "was_cheapest": was_cheapest, "confirmed_at": _iso(h)})

        if rn and r1 and _priced(r1) and _sold_out(rn):
            prev = {q: r["min_price"] for q, r in p1.items() if _priced(r)}
            was_cheapest = prev[p] == min(prev.values())
            em.emit(type="sold_out", at=h, gpu=g, provider=p, before=r1["min_price"],
                    severity="notable" if was_cheapest else "info",
                    title=f"{_name(p)} sold out of {G}",
                    detail={"last_price": r1["min_price"], "listings": rn["live_listings"],
                            "sold_out_listings": rn["sold_out_listings"], "was_cheapest": was_cheapest})

        if rn and r1 and _sold_out(r1) and _priced(rn):
            cur = {q: r["min_price"] for q, r in now.items() if _priced(r) and ctx.established(g, q, h)}
            is_cheapest = p in cur and cur[p] == min(cur.values())
            em.emit(type="capacity_returned", at=h, gpu=g, provider=p, after=rn["min_price"],
                    severity="notable" if is_cheapest else "info",
                    title=f"{G} back in stock at {_name(p)} from {_usd(rn['min_price'])}",
                    detail={"price": rn["min_price"], "priced_listings": rn["priced_listings"],
                            "is_cheapest": is_cheapest})

        step = None
        mkt = _marketplace(p)
        held = " (held 2h)" if mkt else ""
        base_ok = True
        if mkt:
            # Marketplace (many hosts, e.g. Vast's median-of-asks row): the move from h-2 must hold at
            # h-1 AND h, same direction, both past the threshold. A one-hour swing never fires, and
            # neither does its reversion: the h-2 level must not itself be a fresh jump from h-3.
            base, mid = r2, r1
            r3 = rows.at(g, h - 3 * HOUR).get(p)
            if base and r3 and _priced(base) and _priced(r3):
                a3, a2 = r3["min_price"], base["min_price"]
                base_ok = not _moved(a3, a2, _chg(a3, a2))
        else:
            base, mid = r1, None
        if base_ok and rn and base and _priced(rn) and _priced(base) and (mid is None or _priced(mid)):
            a, b = base["min_price"], rn["min_price"]
            pct = _chg(a, b)
            if _moved(a, b, pct) and (mid is None or _held(a, mid["min_price"], pct)):
                mix = rn["priced_listings"] != base["priced_listings"] or rn["live_listings"] != base["live_listings"]
                if mix:
                    title = f"{_name(p)}'s lowest {G} price {'fell' if pct < 0 else 'rose'} {_pct(pct)} to {_usd(b)}"
                else:
                    title = f"{_name(p)} {'cut' if pct < 0 else 'raised'} {G} by {_pct(pct)} to {_usd(b)}"
                det = {"metric": "provider_lowest", "basis": "hour", "from": a, "to": b,
                       "since": _iso(h - (2 * HOUR if mkt else HOUR)), "cause": "listing_mix" if mix else "price_change",
                       "priced_listings": [base["priced_listings"], rn["priced_listings"]]}
                if mkt:
                    det.update(persistence={"held_hours": 2, "samples": [mid["min_price"], b],
                                            "rule": "marketplace: move must hold for 2 consecutive hourly samples"},
                               provider_class="marketplace")
                step = em.emit(type="price_move", at=h, gpu=g, provider=p, before=a, after=b, pct=pct,
                               severity=_sev_provider(pct, mkt), title=title + held, qualifier="hour", detail=det)
        r24 = p24.get(p)
        if step is None and rn and _priced(rn) and _priced(r24):
            a, b = r24["min_price"], rn["min_price"]
            pct = _chg(a, b)
            # Marketplace: the previous hour must already show the same move vs 24h ago (it held 2 samples).
            ok = not mkt or (r1 is not None and _priced(r1) and _held(a, r1["min_price"], pct))
            if _moved(a, b, pct) and ok:
                det = {"metric": "provider_lowest", "basis": "24h", "from": a, "to": b, "since": _iso(h - DAY)}
                if mkt:
                    det.update(persistence={"held_hours": 2, "samples": [r1["min_price"], b],
                                            "rule": "marketplace: move must hold for 2 consecutive hourly samples"},
                               provider_class="marketplace")
                em.emit(type="price_move", at=h, gpu=g, provider=p, before=a, after=b, pct=pct,
                        severity=_sev_provider(pct, mkt), qualifier="24h", cooldown=COOLDOWN,
                        title=f"{_name(p)}'s lowest {G} price {'down' if pct < 0 else 'up'} {_pct(pct)} in 24h to {_usd(b)}{held}",
                        detail=det)

    # ---- market level, over one provider set: established at h -------------------
    S = {p for p in set(now) | set(p1) | set(p24) if ctx.established(g, p, h)}
    cur = {p: r["min_price"] for p, r in now.items() if p in S and _priced(r)}
    prv = {p: r["min_price"] for p, r in p1.items() if p in S and _priced(r)}

    if cur and prv and len(set(cur) | set(prv)) >= 2:
        x = min(cur, key=lambda p: (cur[p], p))
        y = min(prv, key=lambda p: (prv[p], p))
        if x != y and cur[x] < cur.get(y, float("inf")) * (1 - CHEAPEST_MARGIN):
            drop = _chg(prv[y], cur[x])
            ry = now.get(y)
            reason = ("undercut" if y in cur else "previous_sold_out" if _sold_out(ry)
                      else "previous_gone" if ry is None else "previous_unpriced")
            was = {"undercut": f"was {_name(y)} at {_usd(prv[y])}",
                   "previous_sold_out": f"{_name(y)} at {_usd(prv[y])} sold out",
                   "previous_gone": f"{_name(y)} at {_usd(prv[y])} no longer listed",
                   "previous_unpriced": f"{_name(y)} at {_usd(prv[y])} no longer priced"}[reason]
            em.emit(type="new_cheapest_provider", at=h, gpu=g, provider=x, before=prv[y], after=cur[x], pct=drop,
                    severity="notable" if drop is not None and drop <= -CHEAPEST_NOTABLE else "info",
                    cooldown=CHEAPEST_COOLDOWN,
                    title=f"{_name(x)} is now the cheapest {G} at {_usd(cur[x])} ({was})",
                    detail={"previous_provider": y, "previous_price": prv[y], "price": cur[x], "reason": reason,
                            "previous_provider_now": cur.get(y), "providers": len(cur)})

    live_now = [p for p in now if p in S]
    live_prev = [p for p in p1 if p in S]
    if live_now and not cur and prv and any(_sold_out(now[p]) for p in live_now):
        em.emit(type="sold_out", at=h, gpu=g, severity="major", before=min(prv.values()),
                title=f"{G} sold out at every tracked provider",
                detail={"providers": sorted(live_now), "last_lowest": min(prv.values())})
    if cur and live_prev and not prv:
        p = min(cur, key=cur.get)
        em.emit(type="capacity_returned", at=h, gpu=g, severity="notable", after=cur[p],
                title=f"{G} available again after selling out everywhere, from {_usd(cur[p])} ({_name(p)})",
                detail={"providers": sorted(cur), "lowest": cur[p], "lowest_provider": p})

    # Market median move (coverage-matched, from pass 1).
    chg24 = gh_row and gh_row.get("chg24_median")
    if chg24 is not None and abs(chg24) >= MARKET_MOVE:
        med = gh_row["median"]
        em.emit(type="price_move", at=h, gpu=g, pct=chg24, after=med, qualifier="market_median",
                severity="major" if abs(chg24) >= MARKET_MOVE_MAJOR else "notable", cooldown=COOLDOWN,
                title=f"{G} market median {'down' if chg24 < 0 else 'up'} {_pct(chg24)} in 24h to {_usd(med)}",
                detail={"metric": "market_median", "basis": "24h", "matched": True, "median": med,
                        "since": _iso(h - DAY)})

    # Spread: same provider set at h and h-1.
    def spread(d):
        return (max(d.values()) / min(d.values()) - 1) if len(d) >= SPREAD_MIN_PROVIDERS else None

    sp_now, sp_prev = spread(cur), spread(prv)
    if sp_now is not None and sp_prev is not None:
        thr, basis, n_hist = _spread_threshold(hist, h)
        if sp_now > thr >= sp_prev:
            lo_p, hi_p = min(cur, key=cur.get), max(cur, key=cur.get)
            em.emit(type="spread_anomaly", at=h, gpu=g, pct=sp_now, cooldown=COOLDOWN,
                    severity="major" if sp_now >= 1.5 * thr else "notable",
                    title=f"{G} provider spread widened to {sp_now * 100:.0f}% ({_name(lo_p)} {_usd(cur[lo_p])} "
                          f"vs {_name(hi_p)} {_usd(cur[hi_p])})",
                    detail={"spread": sp_now, "previous_spread": sp_prev, "threshold": thr, "basis": basis,
                            "history_hours": n_hist, "lowest_provider": lo_p, "highest_provider": hi_p,
                            "providers": len(cur)})

    # Records: current value over the established set vs history as recorded.
    if cur:
        _records(g, h, cur, hist, em)

    # Regional dislocation (needs regions.region_group; skipped when it is missing).
    if region_fn is not None and regional is not None:
        _regional(g, h, S, regional, region_fn, em)


def _spread_threshold(hist, h):
    t0 = h - RECORD_WINDOW
    vals = [hi / lo - 1 for (t, lo, _, hi) in hist if t0 <= t < h and lo and hi]
    span = (h - hist[0][0]) if hist else timedelta(0)
    if span >= SPREAD_HISTORY_MIN and len(vals) >= 24 * 7:
        vals.sort()
        p95 = vals[min(len(vals) - 1, int(SPREAD_PCTL * len(vals)))]
        return max(p95 * SPREAD_P95_MULT, 0.05), "30d_p95", len(vals)
    return SPREAD_ABS, "absolute", len(vals)


def _records(g, h, cur, hist, em):
    lowest, median = min(cur.values()), _median(list(cur.values()))
    low_p = min(cur, key=cur.get)
    prior = [x for x in hist if x[0] < h]
    if not prior:
        return
    start = prior[0][0]
    t0 = h - RECORD_WINDOW
    win = [x for x in prior if x[0] >= t0]
    enough_30d = start <= t0 and len([x for x in win if x[1] is not None]) >= RECORD_FILL * RECORD_WINDOW / HOUR
    all_time = h - start >= ALL_TIME_MIN
    since = start.date().isoformat()

    fired_all_time = set()
    if all_time:
        lows = [x[1] for x in prior if x[1] is not None]
        if lows and lowest < min(lows) * (1 - RECORD_MARGIN):
            em.emit(type="new_all_time_low", at=h, gpu=g, provider=low_p, before=min(lows), after=lowest,
                    pct=_chg(min(lows), lowest), severity="major", cooldown=COOLDOWN, qualifier="lowest",
                    title=f"{g} lowest price at an all-time low {_usd(lowest)} ({_name(low_p)}), since tracking began {since}",
                    detail={"metric": "lowest", "previous_record": min(lows), "tracking_since": since,
                            "history_hours": len(lows)})
            fired_all_time.add("low")
        if lows and lowest > max(lows) * (1 + RECORD_MARGIN):
            em.emit(type="new_all_time_high", at=h, gpu=g, provider=low_p, before=max(lows), after=lowest,
                    pct=_chg(max(lows), lowest), severity="major", cooldown=COOLDOWN, qualifier="lowest",
                    title=f"{g} lowest price at an all-time high {_usd(lowest)}, since tracking began {since}",
                    detail={"metric": "lowest", "previous_record": max(lows), "tracking_since": since,
                            "history_hours": len(lows)})
            fired_all_time.add("high")
    if not enough_30d:
        return
    for metric, value, idx, sev in (("lowest", lowest, 1, "notable"), ("median", median, 2, "info")):
        if metric == "median" and len(cur) < 2:
            continue
        vals = [x[idx] for x in win if x[idx] is not None]
        if not vals:
            continue
        lo, hi = min(vals), max(vals)
        label = "lowest price" if metric == "lowest" else "market median"
        if value < lo * (1 - RECORD_MARGIN) and not (metric == "lowest" and "low" in fired_all_time):
            em.emit(type="new_30d_low", at=h, gpu=g, provider=low_p if metric == "lowest" else None,
                    before=lo, after=value, pct=_chg(lo, value), severity=sev, cooldown=COOLDOWN, qualifier=metric,
                    title=f"{g} {label} hit a 30-day low: {_usd(value)}" + (f" ({_name(low_p)})" if metric == "lowest" else ""),
                    detail={"metric": metric, "previous_30d_low": lo, "previous_30d_high": hi, "hours": len(vals)})
        if value > hi * (1 + RECORD_MARGIN) and not (metric == "lowest" and "high" in fired_all_time):
            em.emit(type="new_30d_high", at=h, gpu=g, provider=low_p if metric == "lowest" else None,
                    before=hi, after=value, pct=_chg(hi, value), severity=sev, cooldown=COOLDOWN, qualifier=metric,
                    title=f"{g} {label} hit a 30-day high: {_usd(value)}",
                    detail={"metric": metric, "previous_30d_low": lo, "previous_30d_high": hi, "hours": len(vals)})


def _group_medians(rows, S, region_fn):
    """{group: {provider: lowest price in that group}} and {provider: lowest anywhere}, over set S."""
    by_group: dict[str, dict] = defaultdict(dict)
    glob: dict[str, float] = {}
    for r in rows:
        p = r["provider"]
        if p not in S or r["min_price"] is None or r["priced_listings"] <= 0:
            continue
        price = float(r["min_price"])
        glob[p] = min(glob.get(p, price), price)
        grp = region_fn(p, r["region"] or None, r["country"])
        if grp:
            d = by_group[grp]
            d[p] = min(d.get(p, price), price)
    return by_group, glob


def _gaps(rows, S, region_fn):
    by_group, glob = _group_medians(rows, S, region_fn)
    out = {}
    if not glob:
        return out
    gmed = _median(list(glob.values()))
    for grp, d in by_group.items():
        outside = [p for p in glob if p not in d]
        if len(d) >= REGION_MIN_PROVIDERS and len(outside) >= REGION_MIN_PROVIDERS and gmed:
            med = _median(list(d.values()))
            out[grp] = (med / gmed - 1, med, gmed, len(d), len(glob))
    return out


def _regional(g, h, S, regional, region_fn, em):
    now = _gaps(regional.get((g, h), []), S, region_fn)
    prev = _gaps(regional.get((g, h - HOUR), []), S, region_fn)
    for grp, (gap, med, gmed, n_in, n_all) in sorted(now.items()):
        if abs(gap) < REGION_GAP or grp not in prev or abs(prev[grp][0]) >= REGION_GAP:
            continue
        direction = "cheaper" if gap < 0 else "dearer"
        em.emit(type="regional_dislocation", at=h, gpu=g, region_group=grp, pct=gap, before=gmed, after=med,
                severity="notable" if abs(gap) >= REGION_NOTABLE else "info", cooldown=COOLDOWN, qualifier=direction,
                title=f"{g} in {grp} is {_pct(gap)} {'cheaper' if gap < 0 else 'more expensive'} than the global "
                      f"median ({_usd(med)} vs {_usd(gmed)})",
                detail={"direction": direction, "group_median": med, "global_median": gmed, "gap": gap,
                        "group_providers": n_in, "providers": n_all})


def _capacity(h, rows: Rows, ctx: Context, gpus, em, transitions):
    """Market-wide: how many (gpu, provider) markets sold out / came back in the 24h to h."""
    live = sum(1 for g in gpus for p in rows.at(g, h) if ctx.established(g, p, h))
    if not live:
        return
    for direction, kind in (("tightening", "sold_out"), ("loosening", "returned")):
        hits = sorted({(g, p) for (g, p, t, k) in transitions if k == kind and h - DAY < t <= h})
        need = max(CAPACITY_MIN_PAIRS, CAPACITY_SHARE * live)
        if len(hits) < need:
            continue
        share = len(hits) / live
        n_gpus = len({g for g, _ in hits})
        verb = "sold out at a provider" if kind == "sold_out" else "came back in stock at a provider"
        em.emit(type="market_capacity", at=h, cooldown=COOLDOWN, qualifier=direction, pct=share,
                severity="major" if share >= CAPACITY_MAJOR else "notable",
                title=f"{len(hits)} GPU markets {verb} in 24h ({n_gpus} GPUs, {share * 100:.0f}% of live markets)",
                detail={"direction": direction, "markets": len(hits), "gpus": n_gpus, "live_markets": live,
                        "share": share, "examples": [{"gpu": g, "provider": p} for g, p in hits[:10]]})


def _transitions(rows: Rows, ctx: Context, hours):
    out = []
    for g in rows.gpus:
        for h in hours:
            now, p1 = rows.at(g, h), rows.at(g, h - HOUR)
            for p, rn in now.items():
                r1 = p1.get(p)
                if not r1 or not ctx.established(g, p, h):
                    continue
                if _priced(r1) and _sold_out(rn):
                    out.append((g, p, h, "sold_out"))
                elif _sold_out(r1) and _priced(rn):
                    out.append((g, p, h, "returned"))
    return out


def _coverage_events(ctx: Context, rows: Rows, h0, h1, em):
    for p, cs in sorted(ctx.cov_start.items()):
        if not (h0 - HOUR < cs <= h1):
            continue
        gpus = sorted(g for (g, q), fh in ctx.first.items() if q == p and fh <= cs + GRACE + HOUR)
        em.emit(type="coverage_started", at=cs, provider=p, severity="info", segment=None,
                title=f"OpenGrid began tracking {_name(p)} ({len(gpus)} GPU{'s' if len(gpus) != 1 else ''})",
                detail={"gpus": gpus, "note": "start of OpenGrid's coverage, not a market change"})


# --------------------------------------------------------------------------
# Feed outages, from raw_snapshots
# --------------------------------------------------------------------------

def _rounds(rows, gap: timedelta):
    """Group one provider's fetch rows (ascending) into poll rounds: (start, all_failed, error)."""
    out, cur = [], None
    for t, ok, err in rows:
        if cur is None or t - cur["t"] > gap:
            cur = {"t": t, "ok": 0, "bad": 0, "error": None}
            out.append(cur)
        if ok:
            cur["ok"] += 1
        else:
            cur["bad"] += 1
            cur["error"] = cur["error"] or (err or "")[:200]
    return [(r["t"], r["ok"] == 0, r["error"]) for r in out]


def detect_feeds(s, ctx: Context, em: Emitter, now: datetime) -> dict:
    st = _get_state(s, "feeds") or {}
    info = dict(st.get("info") or {})
    open_ = dict(info.get("open") or {})
    wm = st.get("watermark")
    t0 = (wm - FEED_LOOKBACK) if wm else now - BACKFILL
    if open_:
        t0 = min([t0] + [datetime.fromisoformat(v) - timedelta(minutes=1) for v in open_.values()])
    by_p: dict[str, list] = defaultdict(list)
    last = wm
    for r in s.execute(text("""
        SELECT provider, fetched_at, ok, error FROM raw_snapshots WHERE fetched_at > :t0 AND fetched_at <= :t1
        ORDER BY provider, fetched_at
    """), {"t0": t0, "t1": now}):
        by_p[r.provider].append((r.fetched_at, r.ok, r.error))
        last = r.fetched_at if last is None or r.fetched_at > last else last
    for p, rs in by_p.items():
        cls = PROVIDERS.get(p)
        interval = cls.polling.interval_seconds if cls else 900
        gap = timedelta(seconds=min(120, interval / 2))
        cs = ctx.cov_start.get(p)
        fails, worked = [], False   # a feed that never worked is "not set up", not "down"
        for t, failed, err in _rounds(rs, gap):
            had_worked = worked or (cs is not None and cs < (fails[0][0] if fails else t))
            if failed:
                fails.append((t, err))
                if len(fails) == FEED_DOWN_ROUNDS and had_worked:
                    em.emit(type="provider_feed_down", at=fails[0][0], provider=p, severity="notable", segment=None,
                            title=f"{_name(p)} price feed down: {FEED_DOWN_ROUNDS} consecutive failed polls since "
                                  f"{fails[0][0].strftime('%Y-%m-%d %H:%M')} UTC",
                            detail={"first_failure": _iso(fails[0][0]), "rounds": FEED_DOWN_ROUNDS,
                                    "error": fails[0][1]})
            else:
                if len(fails) >= FEED_DOWN_ROUNDS and had_worked:
                    dur = t - fails[0][0]
                    em.emit(type="provider_feed_recovered", at=t, provider=p, segment=None,
                            severity="info", qualifier=fails[0][0].isoformat(),
                            title=f"{_name(p)} price feed recovered after {_dur(dur)}",
                            detail={"first_failure": _iso(fails[0][0]), "failed_rounds": len(fails),
                                    "outage_seconds": int(dur.total_seconds())})
                fails, worked = [], True
        if len(fails) >= FEED_DOWN_ROUNDS:
            open_[p] = fails[0][0].isoformat()
        else:
            open_.pop(p, None)
    info["open"] = open_
    _set_state(s, "feeds", last, info)
    return {"providers": len(by_p), "open_outages": sorted(open_)}


def _dur(d: timedelta) -> str:
    m = int(d.total_seconds() // 60)
    return f"{m // 60}h {m % 60}m" if m >= 60 else f"{m}m"


# --------------------------------------------------------------------------
# Run
# --------------------------------------------------------------------------

def _region_fn():
    try:
        import regions
        return regions.region_group
    except Exception:  # module not there yet, or broken: regional events are skipped, never guessed
        return None


def _prior_events(s, t0) -> list[dict]:
    rows = s.execute(text("""
        SELECT type, occurred_at, gpu, provider, region_group, detail FROM market_events WHERE occurred_at >= :t0
    """), {"t0": t0})
    return [dict(r._mapping) for r in rows]


def _insert(s, events: list[dict], now) -> int:
    if not events:
        return 0
    n = 0
    for i in range(0, len(events), 1000):
        batch = [{**e, "detected_at": now} for e in events[i:i + 1000]]
        res = s.execute(insert(MarketEvent).values(batch)
                        .on_conflict_do_nothing(index_elements=["dedupe_key"]).returning(MarketEvent.id))
        n += len(res.all())
    return n


def run(now: datetime | None = None, segment: str = SEGMENT) -> dict:
    """Process new rollup hours (and recent fetches) into events. Safe to call any time."""
    if not _lock.acquire(blocking=False):
        return {"skipped": "already running"}
    try:
        return _run(now or datetime.now(timezone.utc), segment)
    finally:
        _lock.release()


def _run(now, segment) -> dict:
    with normalize.SessionLocal.begin() as s:
        rng = s.execute(text("SELECT min(hour), max(hour) FROM market_hourly WHERE segment = :s"), {"s": segment}).one()
        ctx = load_context(s, segment, save=True)
        st = _get_state(s, f"market:{segment}")
    min_h, max_h = rng
    written, hours_done = 0, 0
    region_fn = _region_fn()
    if max_h is not None:
        wm = st and st["watermark"]
        if wm is None:
            table_from, events_from = min_h, max(min_h, max_h - BACKFILL)
        else:
            table_from = events_from = max(min_h, wm - OVERLAP)
        c0 = table_from
        while c0 <= max_h:
            c1 = min(c0 + CHUNK - HOUR, max_h)
            written += _chunk(segment, ctx, c0, c1, events_from, region_fn, now)
            hours_done += int((c1 - c0) / HOUR) + 1
            c0 = c1 + HOUR
    with normalize.SessionLocal.begin() as s:
        em = Emitter(_prior_events(s, now - BACKFILL - DAY), None)
        feeds = detect_feeds(s, ctx, em, now)
        written += _insert(s, em.out, now)
        if max_h is not None:
            _set_state(s, f"market:{segment}", max_h, {"last_run": now.isoformat()})
    _after_write()
    return {"hours": hours_done, "events_written": written, "to": _iso(max_h), "feeds": feeds,
            "regions": region_fn is not None}


def _chunk(segment, ctx, c0, c1, events_from, region_fn, now) -> int:
    hours = rollups.hours_between(c0, c1)
    with normalize.SessionLocal() as s:
        rows = _load_rows(s, segment, c0 - LEAD, c1)
    table = []
    for g in sorted(rows.gpus):
        for h in hours:
            if rows.at(g, h):
                table.append(gpu_hour_row(segment, g, h, rows))
    by_gh = {(r["gpu"], r["hour"]): r for r in table}
    with normalize.SessionLocal.begin() as s:
        _write_table(s, segment, table, c0, c1)
    ev_hours = [h for h in hours if h >= events_from]
    if not ev_hours:
        return 0
    with normalize.SessionLocal.begin() as s:
        hist = _history_for(s, segment, c0, c1)
        prior = _prior_events(s, ev_hours[0] - 2 * DAY)
        regional = None
        if region_fn is not None:
            regional = defaultdict(list)
            for r in rollups.regional_hourly(segment, None, ev_hours[0] - HOUR, c1):
                regional[(r["gpu"], r["hour"])].append(r)
        em = Emitter(prior, segment)
        transitions = _transitions(rows, ctx, rollups.hours_between(ev_hours[0] - DAY, c1))
        for h in ev_hours:
            for g in sorted(rows.gpus):
                if not (rows.at(g, h) or rows.at(g, h - HOUR) or rows.at(g, h - 2 * HOUR)):
                    continue
                gh = hist.get(g, [])
                k = bisect.bisect_right([x[0] for x in gh], h)
                detect_hour(g, h, rows, ctx, gh[:k], em, region_fn, regional, by_gh.get((g, h)))
            _capacity(h, rows, ctx, rows.gpus, em, transitions)
        _coverage_events(ctx, rows, ev_hours[0], c1, em)
        return _insert(s, em.out, now)


def _history_for(s, segment, c0, c1) -> dict[str, list]:
    """market_gpu_hourly rows for the record windows: all of it (all-time records need the start).

    Hours older than the 30-day window are summarised as one row per GPU (its first hour, min and max
    lowest), which is all the all-time rules read.
    """
    t0 = c0 - RECORD_WINDOW - DAY
    recent = _table_history(s, segment, t0, c1)
    for r in s.execute(text("""
        SELECT gpu, min(hour) AS first, min(lowest) AS lo, max(lowest) AS hi FROM market_gpu_hourly
        WHERE segment = :s AND hour < :t0 GROUP BY gpu
    """), {"s": segment, "t0": t0}):
        # Two synthetic rows stand for the older history: one at its start carrying the min,
        # one just after carrying the max; neither falls inside any 30-day window.
        old = [(r.first, _f(r.lo), None, None), (r.first + timedelta(seconds=1), _f(r.hi), None, None)]
        recent[r.gpu] = old + recent.get(r.gpu, [])
    return recent


# --------------------------------------------------------------------------
# Readers
# --------------------------------------------------------------------------

def _after_write():
    try:
        from analytics import movers
        movers.clear_caches()
    except Exception:  # never let a cache clear break detection
        log.debug("movers cache clear failed", exc_info=True)


def _row(r) -> dict:
    from api.common import gpu_slug
    d = dict(r._mapping)
    for k in ("value_before", "value_after"):
        d[k] = _f(d[k])
    d["occurred_at"], d["detected_at"] = _iso(d["occurred_at"]), _iso(d["detected_at"])
    d["gpu_slug"] = gpu_slug(d["gpu"]) if d.get("gpu") else None
    d["provider_name"] = _name(d["provider"]) if d.get("provider") else None
    d["kind"] = TYPES.get(d["type"], (_INF,))[0]
    return d


def query(gpu=None, provider=None, since=None, types=None, severities=None, min_severity=None,
          limit=200, offset=0, until=None) -> tuple[list[dict], int]:
    """Events newest first, plus the total matching count (for pagination)."""
    sev = list(severities or [])
    if min_severity in SEVERITIES:
        sev = [x for x in SEVERITIES[SEVERITIES.index(min_severity):] if not severities or x in severities]
    where = ["(CAST(:gpu AS text) IS NULL OR gpu = :gpu)",
             "(CAST(:provider AS text) IS NULL OR provider = :provider)",
             "(CAST(:since AS timestamptz) IS NULL OR occurred_at >= :since)",
             "(CAST(:until AS timestamptz) IS NULL OR occurred_at <= :until)"]
    params = {"gpu": gpu, "provider": provider, "since": since, "until": until,
              "limit": max(1, min(int(limit), 1000)), "offset": max(0, int(offset))}
    if types:
        where.append("type = ANY(:types)")
        params["types"] = list(types)
    if sev or min_severity:
        where.append("severity = ANY(:sev)")
        params["sev"] = sev
    w = " AND ".join(where)
    with normalize.SessionLocal() as s:
        total = s.execute(text(f"SELECT count(*) FROM market_events WHERE {w}"), params).scalar()
        rows = s.execute(text(f"""
            SELECT id, occurred_at, detected_at, type, segment, gpu, provider, region_group, severity, title,
                   detail, value_before, value_after, pct, dedupe_key
            FROM market_events WHERE {w} ORDER BY occurred_at DESC, id DESC LIMIT :limit OFFSET :offset
        """), params).all()
    return [_row(r) for r in rows], int(total or 0)


def recent(gpu=None, provider=None, since=None, types=None, limit=200) -> list[dict]:
    """The contract other modules use: newest events first."""
    return query(gpu=gpu, provider=provider, since=since, types=types, limit=limit)[0]


def catalogue() -> list[dict]:
    return [{"type": t, "kind": k, "description": d, "severity": sv} for t, (k, d, sv) in TYPES.items()]


def rebuild() -> dict:
    """Forget watermarks and the derived table (events are kept; dedupe stops duplicates), then run."""
    with normalize.SessionLocal.begin() as s:
        s.execute(text("DELETE FROM market_gpu_hourly"))
        s.execute(text("DELETE FROM event_detector_state WHERE name LIKE 'market:%'"))
    return run()


# Detection follows each rollup refresh; the job is a backstop (a busy run is skipped, not doubled).
rollups.AFTER_REFRESH.append(lambda: run())


@job("market_events", every_seconds=900, initial_delay_seconds=240)
def _events_job():
    return run()
