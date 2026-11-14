"""The monthly market report (INTERNAL; methodology/monthly-report.md).

The question it answers: over a calendar month, did GPU clouds move their prices proportionally? For each featured
GPU it compares every cloud's price at the start and at the end of the month (one price per cloud: its lowest
on-demand list price) and says how many raised, cut or kept it, by how much, and how unevenly; then whether each
cloud that repriced several GPUs moved them all by the same percentage; then older generations against flagships,
supply changes and the month's notable market events.

    generate(month)          -> report dict; deterministic, from stored data only (no provider call, no clock)
    store(month, report)     -> "created" | "exists" | "regenerated" (force=True only)
    run_job(now)             -> the daily `monthly_report` job: the PREVIOUS month, once, from the 1st 06:00 UTC
    public_summary(report)   -> the paste-able headline text: rounded numbers, no provider names
    discord_message(report)  -> the compact embed for the internal "market" channel (provider names allowed)

Bases (per cloud and GPU, never mixed inside one cloud's move):
    live     market_hourly (observed, public tier): the median of the cloud's hourly lowest price over its first
             (last) ANCHOR_SAMPLES priced hours inside the month's first (last) ANCHOR_HOURS
    archive  archived_prices (archived, public tier), only for a cloud and GPU OpenGrid had not yet recorded live by
             the month's end: the price in force at the month's start (the last document before it, at most
             HOLD_DAYS old, or an AWS price-list version still valid) against the last document dated in the month

Marketplaces and decentralized networks (host asks, not one company's list price) are left out of every cloud
count, as in analytics/shifts.py; they still count in the OpenGrid indices quoted in the headline.

CLI:
    python -m reports.monthly generate --month 2026-10 [--force]
    python -m reports.monthly backfill --from 2024-01 [--to 2026-09] [--force]
    python -m reports.monthly show --month 2026-10 [--json]
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

import normalize
import provider_meta
from config import settings
from jobs import job

log = logging.getLogger(__name__)

VERSION = "1"
METHODOLOGY = "monthly-report"
SEGMENT = "on_demand"

FEATURED = (
    "NVIDIA H100 80GB SXM5",
    "NVIDIA H200 141GB SXM5",
    "NVIDIA B200 180GB SXM",
    "NVIDIA B300 288GB SXM",
    "NVIDIA A100 80GB SXM4",
    "NVIDIA L40S 48GB",
    "AMD Instinct MI300X 192GB",
    "NVIDIA RTX 4090 24GB",
    "NVIDIA RTX 5090 32GB",
)
COMPUTE_INDEX = "gpu-compute"

MOVE_THRESHOLD = 0.01        # |change| >= 1%: the cloud moved (up / down); below: flat
UNIFORM_BAND_PP = 2.0        # within one cloud: every repriced GPU within 2 percentage points -> "uniform"
PROPORTIONAL_BAND_PP = 2.0   # across clouds: every cloud within 2 pp of the median move -> "proportional"
MIN_CLOUDS = 3               # median, middle half, range, dispersion and pattern need >= 3 clouds
ANCHOR_HOURS = 72            # live anchors are read in the month's first / last 72 hours
ANCHOR_SAMPLES = 24          # ... as the median of at most 24 priced hours
HOLD_DAYS = 45               # an archived price holds until the next document, at most this long (archived.py)
ARCHIVE_LOOKBACK_DAYS = 400  # how far back archived documents are read to find the price in force at the start
EXCLUDED_CLASSES = ("marketplace", "decentralized")
MAX_MOVERS = 5
MAX_EVENTS = 25
JOB_GRACE = timedelta(hours=6)   # the rollup and index tail are final well within this after the month ends

_MONTH = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
               "November", "December")


# ---------------------------------------------------------------------------------------------- months

def parse_month(month: str) -> tuple[datetime, datetime]:
    """'2026-10' -> [2026-10-01 00:00 UTC, 2026-11-01 00:00 UTC). ValueError for anything else."""
    m = _MONTH.match(month or "")
    if not m:
        raise ValueError(f"month must be YYYY-MM, got {month!r}")
    y, mo = int(m.group(1)), int(m.group(2))
    start = datetime(y, mo, 1, tzinfo=timezone.utc)
    end = datetime(y + (mo == 12), 1 if mo == 12 else mo + 1, 1, tzinfo=timezone.utc)
    return start, end


def month_of(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m")


def previous_month(now: datetime) -> str:
    first = now.astimezone(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return month_of(first - timedelta(days=1))


def month_label(month: str) -> str:
    start, _ = parse_month(month)
    return f"{MONTH_NAMES[start.month - 1]} {start.year}"


def months_range(first: str, last: str) -> list[str]:
    out, (t, _) = [], parse_month(first)
    end, _ = parse_month(last)
    while t <= end:
        out.append(month_of(t))
        t = parse_month(month_of(t))[1]
    return out


# ---------------------------------------------------------------------------------------------- pure statistics

def _r(v: float | None, nd: int = 6) -> float | None:
    return None if v is None else round(float(v), nd)


def direction(change: float, threshold: float = MOVE_THRESHOLD) -> str:
    # rounded first, so a change of exactly 1% on a decimal price ($2.00 -> $2.02) is a move, not float noise
    c = round(change, 9)
    return "up" if c >= threshold else "down" if c <= -threshold else "flat"


def short_gpu(gpu: str) -> str:
    return re.sub(r"^(NVIDIA|AMD Instinct|AMD|Intel)\s+", "", gpu)


def display(provider: str) -> str:
    return provider_meta.meta(provider).display_name


def cloud_stats(moves: list[dict], threshold: float = MOVE_THRESHOLD, band_pp: float = PROPORTIONAL_BAND_PP) -> dict:
    """moves: [{provider, from, to, change, basis}] for ONE GPU (one per cloud) -> how the clouds moved.

    Counts are always given; median, middle half (inclusive quartiles), full range, dispersion and the pattern need
    MIN_CLOUDS clouds (else null with the reason). Dispersion is in percentage points:
        mad_pp     median absolute deviation of the clouds' moves from the median move
        iqr_pp     q3 - q1 of the moves (the width of the middle half)
        movers_spread_pp  largest minus smallest move among the clouds that moved (>= 2 movers)
    pattern: no_moves | proportional (every cloud within band_pp of the median move, at least one moved) |
             few_movers (fewer than half moved) | broad_uneven (half or more moved, not proportional)
    """
    moves = sorted(moves, key=lambda m: (-abs(m["change"]), m["provider"]))
    ch = [m["change"] for m in moves]
    dirs = Counter(direction(c, threshold) for c in ch)
    movers = [m for m in moves if direction(m["change"], threshold) != "flat"]
    out = {"clouds": len(moves), "up": dirs["up"], "flat": dirs["flat"], "down": dirs["down"],
           "moved": len(movers), "median_move": None, "middle_half": None, "range": None,
           "dispersion": {"mad_pp": None, "iqr_pp": None, "movers_spread_pp": None},
           "pattern": None, "proportional": None, "reasons": {}}
    if len(movers) >= 2:
        out["dispersion"]["movers_spread_pp"] = _r((max(m["change"] for m in movers) - min(m["change"] for m in movers)) * 100, 4)
    out["biggest_movers"] = [{"provider": m["provider"], "provider_name": display(m["provider"]), "from": _r(m["from"]),
                              "to": _r(m["to"]), "change": _r(m["change"]), "basis": m["basis"]}
                             for m in movers[:MAX_MOVERS]]
    out["up_moves"] = [_r(m["change"]) for m in sorted(movers, key=lambda m: m["change"]) if m["change"] > 0]
    out["down_moves"] = [_r(m["change"]) for m in sorted(movers, key=lambda m: m["change"]) if m["change"] < 0]
    if len(moves) < MIN_CLOUDS:
        why = f"{len(moves)} cloud(s) priced at both ends of the month; at least {MIN_CLOUDS} needed"
        for k in ("median_move", "middle_half", "range", "dispersion", "pattern"):
            out["reasons"][k] = why
        return out
    med = statistics.median(ch)
    q1, _, q3 = statistics.quantiles(ch, n=4, method="inclusive")
    out["median_move"] = _r(med)
    out["middle_half"] = [_r(q1), _r(q3)]
    out["range"] = [_r(min(ch)), _r(max(ch))]
    out["dispersion"]["mad_pp"] = _r(statistics.median(abs(c - med) for c in ch) * 100, 4)
    out["dispersion"]["iqr_pp"] = _r((q3 - q1) * 100, 4)
    within = all(abs(c - med) * 100 <= band_pp + 1e-9 for c in ch)
    if not movers:
        out["pattern"], out["proportional"] = "no_moves", None
        out["reasons"]["proportional"] = "no cloud moved its price"
    elif within:
        out["pattern"], out["proportional"] = "proportional", True
    else:
        out["proportional"] = False
        out["pattern"] = "few_movers" if len(movers) * 2 < len(moves) else "broad_uneven"
    return out


def provider_class_of(changes: list[float], threshold: float = MOVE_THRESHOLD, band_pp: float = UNIFORM_BAND_PP) -> str | None:
    """One cloud's repriced GPUs (|change| >= threshold) -> uniform | same_direction | mixed; None below 2 GPUs."""
    moved = [c for c in changes if direction(c, threshold) != "flat"]
    if len(moved) < 2:
        return None
    if not (all(c > 0 for c in moved) or all(c < 0 for c in moved)):
        return "mixed"
    return "uniform" if (max(moved) - min(moved)) * 100 <= band_pp + 1e-9 else "same_direction"


def within_provider(pairs: dict[tuple[str, str], dict], threshold: float = MOVE_THRESHOLD,
                    band_pp: float = UNIFORM_BAND_PP) -> dict:
    """pairs: {(gpu, provider): move} over EVERY GPU -> per cloud that repriced >= 2 GPUs, its class; overall shares."""
    by = defaultdict(list)
    for (gpu, prov), m in pairs.items():
        by[prov].append((gpu, m))
    rows = []
    for prov in sorted(by):
        items = sorted(by[prov])
        cls = provider_class_of([m["change"] for _, m in items], threshold, band_pp)
        if cls is None:
            continue
        moved = [(g, m) for g, m in items if direction(m["change"], threshold) != "flat"]
        rows.append({"provider": prov, "provider_name": display(prov), "class": cls, "gpus_tracked": len(items),
                     "gpus_repriced": len(moved),
                     "spread_pp": _r((max(m["change"] for _, m in moved) - min(m["change"] for _, m in moved)) * 100, 4),
                     "moves": [{"gpu": g, "change": _r(m["change"]), "basis": m["basis"]} for g, m in moved]})
    n = len(rows)
    counts = Counter(r["class"] for r in rows)
    shares = {k: (_r(counts[k] / n, 4) if n else None) for k in ("uniform", "same_direction", "mixed")}
    return {"providers_repricing_2plus": n, "counts": {k: counts[k] for k in ("uniform", "same_direction", "mixed")},
            "shares": shares, "band_pp": band_pp, "providers": rows,
            "reason": None if n else "no cloud repriced two or more GPUs this month"}


# ---------------------------------------------------------------------------------------------- reads

_LIVE = text("""
    SELECT gpu, provider, hour, min(min_price) AS price
    FROM market_hourly
    WHERE segment = :seg AND min_price IS NOT NULL AND min_price > 0
      AND ((hour >= :a0 AND hour < :a1) OR (hour >= :b0 AND hour < :b1))
    GROUP BY gpu, provider, hour
    ORDER BY gpu, provider, hour
""")

_LIVE_BEFORE = text("""
    SELECT DISTINCT gpu, provider FROM market_hourly WHERE segment = :seg AND hour < :m1
""")

_ARCHIVE = text("""
    SELECT provider, gpu, observed_at, max(valid_to) AS valid_to, min(price_per_gpu_hour) AS price
    FROM archived_prices
    WHERE gpu IS NOT NULL AND market_type = :seg AND visibility = 'public' AND price_per_gpu_hour > 0
      AND observed_at >= :lo AND observed_at < :m1
    GROUP BY provider, gpu, observed_at
    ORDER BY provider, gpu, observed_at
""")


def _excluded(provider: str) -> bool:
    return provider_meta.meta(provider).provider_class in EXCLUDED_CLASSES


def _live_pairs(s, m0: datetime, m1: datetime) -> tuple[dict, dict]:
    """{(gpu, provider): {from, to, basis}} for clouds priced in both live anchor windows; plus half-priced ones."""
    a1 = m0 + timedelta(hours=ANCHOR_HOURS)
    b0 = m1 - timedelta(hours=ANCHOR_HOURS)
    first, last = defaultdict(list), defaultdict(list)
    for r in s.execute(_LIVE, {"seg": SEGMENT, "a0": m0, "a1": a1, "b0": b0, "b1": m1}):
        key, p = (r.gpu, r.provider), float(r.price)
        if r.hour < a1:
            first[key].append(p)
        if r.hour >= b0:
            last[key].append(p)
    pairs, partial = {}, {}
    for key in sorted(set(first) | set(last)):
        f, la = first.get(key), last.get(key)
        if f and la:
            pairs[key] = {"from": statistics.median(f[:ANCHOR_SAMPLES]), "to": statistics.median(la[-ANCHOR_SAMPLES:]),
                          "basis": "live"}
        else:
            partial[key] = {"from": statistics.median(f[:ANCHOR_SAMPLES]) if f else None,
                            "to": statistics.median(la[-ANCHOR_SAMPLES:]) if la else None}
    return pairs, partial


def _archive_pairs(s, m0: datetime, m1: datetime, live_before: set) -> tuple[dict, dict]:
    """Archived start/end per (gpu, provider) not recorded live by the month's end."""
    lo = m0 - timedelta(days=ARCHIVE_LOOKBACK_DAYS)
    series = defaultdict(list)
    for r in s.execute(_ARCHIVE, {"seg": SEGMENT, "lo": lo, "m1": m1}):
        if (r.gpu, r.provider) in live_before:
            continue
        series[(r.gpu, r.provider)].append(r)
    pairs, ends = {}, {}
    for key in sorted(series):
        pts = series[key]
        before = [p for p in pts if p.observed_at < m0]
        inside = [p for p in pts if p.observed_at >= m0]
        start = None
        if before:
            b = before[-1]
            if (b.valid_to is not None and b.valid_to >= m0) or (b.valid_to is None and m0 - b.observed_at <= timedelta(days=HOLD_DAYS)):
                start = float(b.price)
        end = float(inside[-1].price) if inside else None
        if end is not None:
            ends[key] = end
        if start is not None and end is not None:
            pairs[key] = {"from": start, "to": end, "basis": "archive"}
    return pairs, ends


def _index_window(s, ids: list[str], m0: datetime, m1: datetime) -> dict[str, dict]:
    rows = s.execute(text("""
        SELECT index_id, hour, level, segment_no FROM index_levels
        WHERE index_id = ANY(:ids) AND published AND level IS NOT NULL AND hour >= :m0 AND hour < :m1
        ORDER BY index_id, hour"""), {"ids": ids, "m0": m0, "m1": m1}).all()
    by = defaultdict(list)
    for r in rows:
        by[r.index_id].append(r)
    first_any = dict(s.execute(text("""
        SELECT index_id, min(hour) FROM index_levels WHERE index_id = ANY(:ids) AND published GROUP BY index_id"""),
        {"ids": ids}).all())
    out = {}
    a1, b0 = m0 + timedelta(hours=ANCHOR_HOURS), m1 - timedelta(hours=ANCHOR_HOURS)
    for iid in ids:
        rs = by.get(iid, [])
        d = {"index_id": iid, "start": None, "end": None, "change": None, "start_hour": None, "end_hour": None,
             "published_hours": len(rs), "reason": None}
        st = rs[0] if rs and rs[0].hour < a1 else None
        en = rs[-1] if rs and rs[-1].hour >= b0 else None
        if not rs:
            fa = first_any.get(iid)
            d["reason"] = ("no published hour this month" + (f"; the index is published from {fa.date().isoformat()}"
                                                             if fa and fa >= m1 else ""))
        elif st is None or en is None:
            d["reason"] = f"not published within the first and last {ANCHOR_HOURS} hours of the month"
        if st is not None:
            d["start"], d["start_hour"] = _r(st.level), st.hour.isoformat()
        if en is not None:
            d["end"], d["end_hour"] = _r(en.level), en.hour.isoformat()
        if st is not None and en is not None:
            if st.segment_no != en.segment_no:
                d["reason"] = "the index chain was rebased during the month: levels are not comparable"
            elif st.level:
                d["change"] = _r(en.level / st.level - 1)
        out[iid] = d
    return out


def _events(s, m0: datetime, m1: datetime) -> list[dict]:
    rows = s.execute(text("""
        SELECT id, occurred_at, type, severity, title, gpu, provider, region_group, pct, value_before, value_after
        FROM market_events
        WHERE occurred_at >= :m0 AND occurred_at < :m1 AND type <> 'coverage_started'
        ORDER BY occurred_at, id"""), {"m0": m0, "m1": m1}).all()
    return [dict(r._mapping) for r in rows]


def _capacity(s, gpus: list[str], m0: datetime, m1: datetime) -> dict[str, dict]:
    prev = {(r.provider, r.sku, r.region_key): r.in_stock for r in s.execute(text("""
        SELECT DISTINCT ON (provider, sku, region_key) provider, sku, region_key, in_stock
        FROM capacity_changes
        WHERE observed_at < :m0 AND visibility = 'public' AND canonical_gpu_name = ANY(:gpus)
        ORDER BY provider, sku, region_key, observed_at DESC, id DESC"""), {"m0": m0, "gpus": gpus})}
    rows = s.execute(text("""
        SELECT provider, sku, region_key, canonical_gpu_name AS gpu, observed_at, in_stock
        FROM capacity_changes
        WHERE observed_at >= :m0 AND observed_at < :m1 AND visibility = 'public' AND canonical_gpu_name = ANY(:gpus)
        ORDER BY observed_at, id"""), {"m0": m0, "m1": m1, "gpus": gpus}).all()
    out = {g: {"sold_out": 0, "back_in_stock": 0, "providers_sold_out": set(), "providers_back": set(), "changes": 0}
           for g in gpus}
    for r in rows:
        k = (r.provider, r.sku, r.region_key)
        before = prev.get(k, "unseen")
        d = out[r.gpu]
        d["changes"] += 1
        if r.in_stock is False and before is not False and before != "unseen":
            d["sold_out"] += 1
            d["providers_sold_out"].add(r.provider)
        elif r.in_stock is True and before is False:
            d["back_in_stock"] += 1
            d["providers_back"].add(r.provider)
        prev[k] = r.in_stock
    for d in out.values():
        d["providers_sold_out"] = sorted(d["providers_sold_out"])
        d["providers_back"] = sorted(d["providers_back"])
    return out


def _live_start(s):
    return s.execute(text("SELECT min(hour) FROM market_hourly WHERE segment = :seg"), {"seg": SEGMENT}).scalar()


# ---------------------------------------------------------------------------------------------- generate

def _generation(gpu: str) -> tuple[str, int]:
    import hardware

    hw = hardware.spec(gpu) or {}
    gen = hw.get("generation")
    if not gen:
        return "Unknown", 0
    seg = hw.get("segment")
    return (gen if seg == "datacenter" else f"{gen} ({seg})"), int(hw.get("released") or 0)


def generate(month: str, *, threshold: float = MOVE_THRESHOLD, now: datetime | None = None, session=None) -> dict:
    """The report for one calendar month (UTC). Raises ValueError for a malformed or unfinished month."""
    m0, m1 = parse_month(month)
    now = now or datetime.now(timezone.utc)
    if now < m1:
        raise ValueError(f"{month} has not ended yet (it ends {m1.isoformat()})")
    own = session is None
    s = session or normalize.SessionLocal()
    try:
        return _generate(s, month, m0, m1, threshold)
    finally:
        if own:
            s.close()


def _generate(s, month: str, m0: datetime, m1: datetime, threshold: float) -> dict:
    from api.common import gpu_slug

    live_start = _live_start(s)
    live, partial = _live_pairs(s, m0, m1)
    live_before = {(r.gpu, r.provider) for r in s.execute(_LIVE_BEFORE, {"seg": SEGMENT, "m1": m1})}
    archive, archive_ends = _archive_pairs(s, m0, m1, live_before)
    all_pairs = {**archive, **live}   # disjoint by construction (archive only where nothing live before m1)
    excluded = sorted({p for (_, p) in all_pairs if _excluded(p)})
    pairs = {k: {**v, "change": v["to"] / v["from"] - 1} for k, v in all_pairs.items()
             if not _excluded(k[1]) and v["from"] > 0}
    # half-priced clouds: why each featured GPU lost one (reported, never filled in)
    not_both = Counter(g for (g, p) in partial if not _excluded(p) and (g, p) not in pairs)

    # cheapest at month end: every included cloud with an end price (live end anchor, else archive end document)
    end_prices = defaultdict(dict)
    for (g, p), v in pairs.items():
        end_prices[g][p] = v["to"]
    for (g, p), v in partial.items():
        if v["to"] is not None and not _excluded(p):
            end_prices[g].setdefault(p, v["to"])
    for (g, p), v in archive_ends.items():
        if not _excluded(p):
            end_prices[g].setdefault(p, v)

    ids = [COMPUTE_INDEX] + [gpu_slug(g) for g in FEATURED]
    idx = _index_window(s, ids, m0, m1)
    events = _events(s, m0, m1)
    cap = _capacity(s, list(FEATURED), m0, m1)

    by_gpu = defaultdict(list)
    for (g, p), v in sorted(pairs.items()):
        by_gpu[g].append({"provider": p, **v})

    # ---- b. per featured GPU
    featured = []
    for g in FEATURED:
        st = cloud_stats(by_gpu.get(g, []), threshold)
        bases = Counter(m["basis"] for m in by_gpu.get(g, []))
        ends = end_prices.get(g, {})
        cheapest = min(ends.items(), key=lambda kv: (kv[1], kv[0])) if ends else None
        med_end = statistics.median(ends.values()) if len(ends) >= MIN_CLOUDS else None
        movs = by_gpu.get(g, [])
        matched = None
        if len(movs) >= MIN_CLOUDS:
            a, b = statistics.median(m["from"] for m in movs), statistics.median(m["to"] for m in movs)
            matched = {"start": _r(a), "end": _r(b), "change": _r(b / a - 1)}
        reasons = dict(st.pop("reasons"))
        if not movs:
            reasons["clouds"] = "no cloud priced at both the start and the end of the month"
        if not_both.get(g):
            reasons["partial"] = f"{not_both[g]} cloud(s) priced at only one end of the month were left out"
        if med_end is None:
            reasons["spread"] = f"{len(ends)} cloud(s) priced at month end; at least {MIN_CLOUDS} needed for a median"
        featured.append({
            "gpu": g, "slug": gpu_slug(g), "short": short_gpu(g), "index": idx[gpu_slug(g)],
            "basis": {"live": bases["live"], "archive": bases["archive"]}, **st,
            "matched_median": matched,
            "cheapest_end": ({"provider": cheapest[0], "provider_name": display(cheapest[0]), "price": _r(cheapest[1])}
                             if cheapest else None),
            "median_end": _r(med_end), "clouds_priced_end": len(ends),
            "cheapest_below_median": _r(1 - cheapest[1] / med_end) if cheapest and med_end else None,
            "reasons": reasons,
        })

    # ---- d. generations: every GPU with >= MIN_CLOUDS clouds, grouped by hardware generation (+ segment)
    groups = defaultdict(lambda: {"gpus": [], "released": 0, "moves": []})
    for g, movs in sorted(by_gpu.items()):
        if len(movs) < MIN_CLOUDS:
            continue
        label, rel = _generation(g)
        grp = groups[label]
        grp["released"] = max(grp["released"], rel)
        gm = statistics.median(m["change"] for m in movs)
        grp["gpus"].append({"gpu": g, "short": short_gpu(g), "clouds": len(movs), "median_move": _r(gm)})
        grp["moves"].extend(m["change"] for m in movs)
    generations = []
    for label, grp in sorted(groups.items(), key=lambda kv: (-kv[1]["released"], kv[0])):
        d = Counter(direction(c, threshold) for c in grp["moves"])
        generations.append({"generation": label, "released": grp["released"] or None, "gpus": grp["gpus"],
                            "clouds": len(grp["moves"]), "up": d["up"], "flat": d["flat"], "down": d["down"],
                            "median_gpu_move": _r(statistics.median(x["median_move"] for x in grp["gpus"])),
                            "pooled_median_move": _r(statistics.median(grp["moves"]))})

    # ---- e. supply
    ev_by = defaultdict(lambda: defaultdict(set))
    for e in events:
        if e["type"] in ("provider_added_gpu", "provider_removed_gpu") and e["gpu"] in FEATURED and e["provider"]:
            ev_by[e["gpu"]][e["type"]].add(e["provider"])
    supply = []
    for g in FEATURED:
        c = cap[g]
        supply.append({"gpu": g, "short": short_gpu(g), "sold_out": c["sold_out"], "back_in_stock": c["back_in_stock"],
                       "capacity_changes": c["changes"], "providers_sold_out": c["providers_sold_out"],
                       "providers_back_in_stock": c["providers_back"],
                       "providers_listed": sorted(ev_by[g]["provider_added_gpu"]),
                       "providers_delisted": sorted(ev_by[g]["provider_removed_gpu"])})
    supply_reason = None
    if live_start is None or live_start >= m1:
        supply_reason = ("OpenGrid's capacity feeds and catalogue events start with live recording"
                         + (f" ({live_start.date().isoformat()})" if live_start else "") + ": nothing to count for this month")

    # ---- f. events
    sev_rank = {"major": 0, "notable": 1, "info": 2}
    notable = sorted((e for e in events if e["severity"] in ("major", "notable")),
                     key=lambda e: (sev_rank.get(e["severity"], 3), e["occurred_at"], e["id"]))
    event_block = {
        "total": len(events),
        "by_type": dict(sorted(Counter(e["type"] for e in events).items())),
        "by_severity": {k: sum(1 for e in events if e["severity"] == k) for k in ("major", "notable", "info")},
        "notable": [{"id": e["id"], "occurred_at": e["occurred_at"].isoformat(), "type": e["type"],
                     "severity": e["severity"], "title": e["title"], "gpu": e["gpu"], "provider": e["provider"],
                     "pct": _r(e["pct"]), "value_before": _r(e["value_before"]), "value_after": _r(e["value_after"])}
                    for e in notable[:MAX_EVENTS]],
        "notable_total": len(notable),
        "reason": None if events else ("no market events stored for this month"
                                       + (" (events start with live recording)" if live_start is None or live_start >= m1 else "")),
    }

    # ---- g. coverage
    n_live = sum(1 for v in pairs.values() if v["basis"] == "live")
    n_arch = len(pairs) - n_live
    basis = "live" if n_live and not n_arch else "archive" if n_arch and not n_live else "mixed" if pairs else "none"
    providers = sorted({p for (_, p) in pairs})
    caveats = [
        "List prices, not transactions: a cloud's price is its lowest advertised on-demand price per GPU-hour.",
        f"A move is a change of at least {threshold:.0%} between the month's start and end; intra-month round trips "
        "do not show.",
        "Marketplaces and decentralized networks (host asks) are left out of every cloud count; the indices include them.",
    ]
    if n_arch:
        caveats.append("Archived prices exist only on the days a capture or price-list version exists; a cloud without "
                       "a document at the month's start and inside the month is not counted.")
    if live_start is None or live_start >= m1:
        caveats.append("OpenGrid indices start with live recording" + (f" ({live_start.date().isoformat()})" if live_start else "")
                       + "; index levels are missing for this month.")
    coverage = {
        "basis": basis, "pairs": len(pairs), "pairs_live": n_live, "pairs_archive": n_arch,
        "providers_counted": len(providers), "providers": providers, "providers_excluded_class": excluded,
        "live_recording_start": live_start.isoformat() if live_start else None,
        "data_kinds": sorted({"observed" if n_live else None, "archived" if n_arch else None, "derived"} - {None}),
        "thresholds": {"move": threshold, "uniform_band_pp": UNIFORM_BAND_PP,
                       "proportional_band_pp": PROPORTIONAL_BAND_PP, "min_clouds": MIN_CLOUDS,
                       "anchor_hours": ANCHOR_HOURS, "anchor_samples": ANCHOR_SAMPLES, "hold_days": HOLD_DAYS},
        "visibility": "public tier only (internal sources never enter a cloud count)",
        "caveats": caveats,
        "reason": None if pairs else "no cloud was priced at both the start and the end of the month in stored data",
    }

    report = {
        "month": month, "label": month_label(month), "version": VERSION, "segment": SEGMENT,
        "period": {"start": m0.isoformat(), "end": m1.isoformat()},
        "headline": {"compute_index": idx[COMPUTE_INDEX],
                     "featured": [{"gpu": f["gpu"], "short": f["short"], **{k: f["index"][k] for k in ("start", "end", "change", "reason")}}
                                  for f in featured]},
        "featured": featured,
        "within_provider": within_provider(pairs, threshold),
        "generations": generations,
        "supply": {"gpus": supply, "reason": supply_reason},
        "events": event_block,
        "coverage": coverage,
        "methodology": f"/methodology/{METHODOLOGY}",
    }
    report["public_summary"] = public_summary(report)
    return report


# ---------------------------------------------------------------------------------------------- public summary

def _pct_int(x: float) -> int:
    return int(round(x * 100))


def _moved(x: float) -> str:
    r = _pct_int(x)
    return "roughly unchanged" if r == 0 else f"up {r}%" if r > 0 else f"down {-r}%"


def _signed(x: float) -> str:
    r = _pct_int(x)
    return f"{r:+d}%" if r else "0%"


def public_summary_lines(report: dict) -> list[str]:
    """Headline sentences only: rounded index levels (points to a whole number, $ to 10 cents) and whole-number
    percentages, counts. Never a provider name, never one cloud's price."""
    label = report["label"]
    lines = []
    ci = report["headline"]["compute_index"]
    if ci.get("end") is not None and ci.get("change") is not None:
        lines.append(f"The OpenGrid GPU Compute Index ended {label} at about {round(ci['end'])}, "
                     f"{_moved(ci['change'])} on the month.")
    feats = [f for f in report["featured"] if f["clouds"] >= MIN_CLOUDS]
    lead = feats[0] if feats else None
    if lead:
        ix = lead["index"]
        if ix.get("end") is not None and ix.get("change") is not None:
            lines.append(f"The OpenGrid {lead['short']} index ended the month at about ${ix['end']:.1f}0 per GPU-hour, "
                         f"{_moved(ix['change'])}.")
        med = lead["median_move"]
        lines.append(f"{lead['short']}: {lead['up']} of {lead['clouds']} clouds raised prices, {lead['down']} cut them and "
                     f"{lead['flat']} left them unchanged; the median cloud's price was {_moved(med)}.")
        if lead["pattern"] == "proportional":
            lines.append(f"The {lead['short']} moves were proportional: every cloud moved within "
                         f"{PROPORTIONAL_BAND_PP:.0f} points of the median.")
        elif lead["moved"] >= 2:
            lo, hi = min(lead["up_moves"] + lead["down_moves"]), max(lead["up_moves"] + lead["down_moves"])
            lines.append(f"The {lead['short']} moves were not proportional: the clouds that moved changed prices by "
                         f"between {_signed(lo)} and {_signed(hi)}.")
    if feats:
        moved = sum(f["moved"] for f in feats)
        total = sum(f["clouds"] for f in feats)
        lines.append(f"Across {len(feats)} featured GPUs, {moved} of {total} cloud prices changed by "
                     f"{_pct_int(report['coverage']['thresholds']['move'])}% or more.")
    wp = report["within_provider"]
    if wp["providers_repricing_2plus"]:
        n, u = wp["providers_repricing_2plus"], wp["counts"]["uniform"]
        clouds = f"{n} cloud{'s' if n != 1 else ''} that repriced two or more GPUs"
        band = f"by about the same percentage (within {UNIFORM_BAND_PP:.0f} points)"
        lines.append(f"Of the {clouds}, {u} moved them all {band}." if u else
                     f"None of the {clouds} moved them all {band}.")
    gens = [g for g in report["generations"] if g["median_gpu_move"] is not None][:4]
    if len(gens) >= 2:
        lines.append("By GPU generation, median price moves: "
                     + ", ".join(f"{g['generation']} {_moved(g['median_gpu_move'])}" for g in gens) + ".")
    sup = report["supply"]
    if not sup["reason"]:
        so, back = sum(g["sold_out"] for g in sup["gpus"]), sum(g["back_in_stock"] for g in sup["gpus"])
        if so or back:
            lines.append(f"Featured GPUs sold out {so} time{'s' if so != 1 else ''} at individual clouds and came back "
                         f"in stock {back} time{'s' if back != 1 else ''}.")
    cov = report["coverage"]
    if len(lines) < 4 or not cov["pairs"]:
        src = {"live": "observed live", "archive": "recovered from archived price pages", "mixed":
               "observed live and recovered from archived price pages", "none": "available"}[cov["basis"]]
        lines.append(f"OpenGrid compared {cov['pairs']} cloud prices across {cov['providers_counted']} clouds "
                     f"at the start and end of {label} ({src})." if cov["pairs"] else
                     f"Stored data for {label} is not enough for a price comparison.")
    return lines[:8]


def methodology_line(report: dict) -> str:
    t = report["coverage"]["thresholds"]
    return (f"Methodology: OpenGrid compares each cloud's lowest on-demand list price per GPU-hour at the start and "
            f"the end of the month; a change of {_pct_int(t['move'])}% or more counts as a move. Marketplaces are left "
            f"out of the cloud counts. Index levels are OpenGrid's chain-linked indices. Source: OpenGrid.")


def public_summary(report: dict) -> str:
    return "\n".join(f"- {line}" for line in public_summary_lines(report)) + "\n\n" + methodology_line(report)


# ---------------------------------------------------------------------------------------------- Discord embed

def discord_message(report: dict) -> dict:
    """Compact embed for the INTERNAL #opengrid-market channel (alerts/channels.py conventions; names allowed)."""
    from alerts import channels

    ci = report["headline"]["compute_index"]
    desc = (f"GPU Compute Index {ci['end']:.1f} ({_signed(ci['change'])})" if ci.get("end") is not None and ci.get("change") is not None
            else f"GPU Compute Index: {ci.get('reason') or 'n/a'}")
    fields = []
    for f in report["featured"]:
        if not f["clouds"]:
            continue
        big = f["biggest_movers"][0] if f["biggest_movers"] else None
        med = "n/a" if f["median_move"] is None else _signed(f["median_move"])
        val = f"{f['up']}↑ {f['flat']}= {f['down']}↓ of {f['clouds']} · median {med}"
        if big:
            val += f" · biggest {big['provider_name']} {_signed(big['change'])}"
        fields.append((f["short"], val, False))
    wp = report["within_provider"]
    if wp["providers_repricing_2plus"]:
        c = wp["counts"]
        fields.append(("Within-cloud repricing", f"{wp['providers_repricing_2plus']} clouds: {c['uniform']} uniform · "
                                                 f"{c['same_direction']} same direction · {c['mixed']} mixed", False))
    ev = report["events"]
    fields.append(("Events", f"{ev['by_severity']['major']} major · {ev['by_severity']['notable']} notable", True))
    fields.append(("Basis", f"{report['coverage']['basis']} · {report['coverage']['providers_counted']} clouds", True))
    return {**channels._base(), "embeds": [channels._embed(
        f"📊 Monthly market report: {report['label']}", desc, fields[:25],
        url=channels._link(f"/reports/{report['month']}"), at=datetime.fromisoformat(report["period"]["end"]),
        footer="OpenGrid monthly report · internal")]}


# ---------------------------------------------------------------------------------------------- storage

def get(month: str) -> dict | None:
    from store.reports import MonthlyReport

    parse_month(month)
    with normalize.SessionLocal() as s:
        r = s.get(MonthlyReport, month)
        return _row(r, full=True) if r else None


def _row(r, full: bool = False) -> dict:
    d = {"month": r.month, "label": month_label(r.month), "generated_at": r.generated_at.isoformat(),
         "regenerated_at": r.regenerated_at.isoformat() if r.regenerated_at else None, "version": r.version,
         "posted_at": r.posted_at.isoformat() if r.posted_at else None,
         "basis": (r.report or {}).get("coverage", {}).get("basis")}
    if full:
        d["report"] = r.report
        d["public_summary"] = r.public_summary
    return d


def list_reports() -> list[dict]:
    from store.reports import MonthlyReport

    with normalize.SessionLocal() as s:
        return [_row(r) for r in s.query(MonthlyReport).order_by(MonthlyReport.month.desc()).all()]


def store(month: str, report: dict, *, force: bool = False, now: datetime | None = None) -> str:
    """Write the month's row once. An existing row is overwritten only with force=True (generated_at is kept)."""
    from sqlalchemy.dialects.postgresql import insert

    from store.reports import MonthlyReport

    now = now or datetime.now(timezone.utc)
    summary = report.get("public_summary") or public_summary(report)
    with normalize.SessionLocal.begin() as s:
        ins = insert(MonthlyReport).values(month=month, generated_at=now, version=report["version"], report=report,
                                           public_summary=summary)
        if not force:
            done = s.execute(ins.on_conflict_do_nothing(index_elements=["month"]).returning(MonthlyReport.month)).first()
            return "created" if done else "exists"
        existed = s.get(MonthlyReport, month) is not None
        s.execute(ins.on_conflict_do_update(index_elements=["month"], set_={
            "report": report, "public_summary": summary, "version": report["version"], "regenerated_at": now}))
        return "regenerated" if existed else "created"


def generate_and_store(month: str, *, force: bool = False, now: datetime | None = None) -> dict:
    existing = get(month)
    if existing and not force:
        return {"month": month, "result": "exists"}
    rep = generate(month, now=now)
    return {"month": month, "result": store(month, rep, force=force, now=now)}


def post(month: str, *, now: datetime | None = None) -> str:
    """Post the month's embed to the market channel once. 'posted' | 'already' | 'not configured' | 'failed'."""
    from alerts import channels

    from store.reports import MonthlyReport

    if not channels.configured().get("market"):
        return "not configured"
    now = now or datetime.now(timezone.utc)
    key = f"monthly_report:{month}"
    with normalize.SessionLocal.begin() as s:
        r = s.get(MonthlyReport, month)
        if r is None:
            return "missing"
        if r.posted_at is not None:
            return "already"
        report = r.report
        if not channels._claim(s, "market", key):   # posted before a crash could record posted_at
            r.posted_at = now
            return "already"
    try:
        ok = channels.send("market", discord_message(report))
    except Exception:  # noqa: BLE001 - a feed must never break the job
        log.exception("monthly report %s: posting failed", month)
        ok = False
    with normalize.SessionLocal.begin() as s:
        if ok:
            s.get(MonthlyReport, month).posted_at = now
        else:
            channels._unclaim(s, "market", key)     # retried on the next daily run
    return "posted" if ok else "failed"


# ---------------------------------------------------------------------------------------------- job

def run_job(now: datetime | None = None) -> dict:
    """Generate the previous month once, from the 1st of the month JOB_GRACE after midnight UTC; then post it."""
    if not settings.monthly_report_enabled:
        return {"skipped": "monthly_report_enabled is False"}
    now = now or datetime.now(timezone.utc)
    first = now.astimezone(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month = previous_month(now)
    if now < first + JOB_GRACE:
        return {"skipped": f"waiting until {(first + JOB_GRACE).isoformat()} to generate {month}"}
    out = {"month": month, "result": "exists"}
    if get(month) is None:
        out["result"] = store(month, generate(month, now=now), now=now)
    out["post"] = post(month, now=now)
    return out


@job("monthly_report", every_seconds=86400, initial_delay_seconds=1800)
def _monthly_job():
    return run_job()


# ---------------------------------------------------------------------------------------------- CLI

def _print_report(row: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(row, indent=2, default=str))
        return
    rep = row["report"]
    print(f"{rep['label']} (v{row['version']}, generated {row['generated_at']}, basis {rep['coverage']['basis']})")
    ci = rep["headline"]["compute_index"]
    print(f"  GPU Compute Index: {ci['start']} -> {ci['end']} ({ci['change']})  {ci['reason'] or ''}")
    for f in rep["featured"]:
        print(f"  {f['short']:<22} clouds {f['clouds']:>2}  up {f['up']:>2} flat {f['flat']:>2} down {f['down']:>2}  "
              f"median {f['median_move']}  pattern {f['pattern']}")
    print("\nPublic summary:\n" + row["public_summary"])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m reports.monthly", description="Internal monthly market report")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate", help="generate one month (stored once; --force overwrites)")
    g.add_argument("--month", required=True)
    g.add_argument("--force", action="store_true")
    b = sub.add_parser("backfill", help="generate every month from --from to --to (default: the last full month)")
    b.add_argument("--from", dest="first", required=True)
    b.add_argument("--to", dest="last")
    b.add_argument("--force", action="store_true")
    sh = sub.add_parser("show", help="print a stored month")
    sh.add_argument("--month", required=True)
    sh.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "generate":
            print(json.dumps(generate_and_store(a.month, force=a.force)))
        elif a.cmd == "backfill":
            last = a.last or previous_month(datetime.now(timezone.utc))
            for m in months_range(a.first, last):
                print(json.dumps(generate_and_store(m, force=a.force)), flush=True)
        else:
            row = get(a.month)
            if row is None:
                print(f"no report stored for {a.month}", file=sys.stderr)
                return 1
            _print_report(row, a.json)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
