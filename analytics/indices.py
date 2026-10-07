"""OpenGrid market indices: definitions, the hourly computation, and readers.

Why an index and not just "the median price": the set of providers we record
changes (a provider is added, a feed breaks, a GPU is listed for the first time).
A plain cross-sectional median jumps whenever that set changes, which would
publish a market move that never happened. Every index here is CHAIN-LINKED:
each hour's move is measured only on the providers present in both this hour and
the previous published hour, and the level is the product of those moves. A
provider joining or leaving cannot move the index; a price change can.

Families (ids are stable slugs; see methodology/indices.md for the full rules)
    <gpu>                 per canonical GPU, on-demand, USD per GPU-hour   "h100-80gb-sxm5"
    <gpu>.spot            the same over spot / interruptible listings       "h100-80gb-sxm5.spot"
    <gpu>.<region>        on-demand, one region group (regions.region_group) "h100-80gb-sxm5.us"
    <gpu>.<class>         on-demand, one provider class (provider_meta)     "h100-80gb-sxm5.neocloud"
    <family>-class        base-100 composite of explicitly listed variants  "h100-class"
    gpu-compute           base-100 composite of the benchmark GPU indices   "gpu-compute"

Each hour, for a per-GPU index:
    1. one vote per provider: that provider's lowest priced, not-sold-out eligible
       listing (market_hourly, min over regions) -- 40 listings still count once
    2. drop providers whose feed was failing that hour (raw_snapshots ok=false, no ok)
    3. drop outliers: a vote more than OUTLIER_RATIO x above or below the median
    4. fewer than MIN_PROVIDERS left -> not published, with the reason
    5. statistic: median below TRIM_FROM providers, 20% trimmed mean from there
    6. level = previous level x stat(common set now) / stat(common set then)

Stored in `index_levels` (one row per index-hour), recomputed incrementally after
each rollup refresh (rollups.AFTER_REFRESH). Readers never touch raw observations.
"""

from __future__ import annotations

import logging
import math
import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

import canonical
import normalize
import provider_meta
from analytics import rollups
from api.common import gpu_slug
from cache import ttl_cache
from store.analytics import IndexLevel

log = logging.getLogger(__name__)

METHODOLOGY_VERSION = "1.0"
METHODOLOGY = "indices"

MIN_PROVIDERS = 3            # constituents needed to publish a per-GPU index hour
TRIM_FROM = 5                # from this many constituents, trimmed mean instead of median
TRIM = 0.2                   # fraction trimmed from EACH end
OUTLIER_RATIO = 3.0          # a vote above median x 3 or below median / 3 is excluded
MIN_COMPOSITE_WEIGHT = 0.5   # share of a composite's weight that must be published
MIN_COMPOSITE_COMPONENTS = 2  # a "composite" of one published child is just that child
OVERLAP = timedelta(hours=3)  # matches rollups.OVERLAP: the rollup may revise its tail
CHUNK = timedelta(days=7)
HOUR = timedelta(hours=1)
HOURS_PER_YEAR = 24 * 365
MIN_WINDOW_COVERAGE = 0.7    # volatility needs returns for 70% of the window's hours

# The contract in regions.py (STRUCTURE agent); repeated so ids exist even before it does.
REGION_GROUPS = ("US", "Canada", "Europe", "UK", "APAC", "Middle East", "LATAM", "Africa")

# Benchmark GPUs: the broadly sold data-centre parts. They get regional and
# provider-class indices and make up the OpenGrid GPU Compute Index.
BENCHMARK_GPUS = (
    "NVIDIA H100 80GB SXM5",
    "NVIDIA H200 141GB SXM5",
    "NVIDIA B200 180GB SXM",
    "NVIDIA A100 80GB SXM4",
    "NVIDIA L40S 48GB",
)

# Family composites. Each lists its variants explicitly; variants stay separate
# per-variant indices too. Equal weights (see methodology: no reliable volume data).
FAMILIES = {
    "h100-class": ("OpenGrid H100 Class Index", (
        "NVIDIA H100 80GB SXM5", "NVIDIA H100 80GB PCIe", "NVIDIA H100 80GB PCIe NVLink", "NVIDIA H100 94GB NVL")),
    "h200-class": ("OpenGrid H200 Class Index", ("NVIDIA H200 141GB SXM5", "NVIDIA H200 143GB NVL")),
    "a100-class": ("OpenGrid A100 Class Index", (
        "NVIDIA A100 80GB SXM4", "NVIDIA A100 80GB PCIe", "NVIDIA A100 80GB PCIe NVLink",
        "NVIDIA A100 40GB SXM4", "NVIDIA A100 40GB PCIe")),
}

CLASS_LABELS = {"hyperscaler": "Hyperscaler", "neocloud": "Neocloud", "general_cloud": "General Cloud",
                "marketplace": "Marketplace", "decentralized": "Decentralized"}


def _slug(s: str) -> str:
    return gpu_slug(s.replace("_", " "))


def _short(gpu: str) -> str:
    return gpu.removeprefix("NVIDIA ").removeprefix("AMD Instinct ").removeprefix("AMD ").removeprefix("Intel ")


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class IndexDef:
    id: str
    name: str
    kind: str                      # gpu | spot | regional | provider_class | composite
    segment: str = "on_demand"
    unit: str = "usd_per_gpu_hour"  # or "points" (base 100)
    gpu: str | None = None
    region_group: str | None = None
    provider_class: str | None = None
    components: tuple = field(default=())  # composites: ((index_id, weight), ...)
    description: str = ""

    def as_dict(self) -> dict:
        d = asdict(self)
        d["components"] = [{"index_id": c, "weight": w, "name": REGISTRY[c].name if c in REGISTRY else c}
                           for c, w in self.components]
        d["methodology_version"] = METHODOLOGY_VERSION
        d["methodology"] = f"/methodology/{METHODOLOGY}"
        d["price_concept"] = "observed_market_price"
        return d


def gpu_index_id(gpu: str, segment: str = "on_demand") -> str:
    return gpu_slug(gpu) + (".spot" if segment == "spot" else "")


def _build_registry() -> dict[str, IndexDef]:
    reg: dict[str, IndexDef] = {}
    names = canonical.all_canonical_names()
    for gpu in sorted(names):
        s = gpu_slug(gpu)
        reg[s] = IndexDef(s, f"OpenGrid {_short(gpu)} Index", "gpu", gpu=gpu,
                          description=f"On-demand {gpu}: one vote per provider (its lowest eligible price).")
        reg[s + ".spot"] = IndexDef(
            s + ".spot", f"OpenGrid {_short(gpu)} Spot Index (interruptible)", "spot", segment="spot", gpu=gpu,
            description=f"Spot / interruptible {gpu}. Interruptible capacity: can be reclaimed by the provider.")
    for gpu in BENCHMARK_GPUS:
        if gpu not in names:
            continue
        s = gpu_slug(gpu)
        for rg in REGION_GROUPS:
            i = f"{s}.{_slug(rg)}"
            reg[i] = IndexDef(i, f"OpenGrid {_short(gpu)} {rg} Index", "regional", gpu=gpu, region_group=rg,
                              description=f"On-demand {gpu}, listings whose region maps to {rg}.")
        for cls in provider_meta.PROVIDER_CLASSES:
            i = f"{s}.{_slug(cls)}"
            reg[i] = IndexDef(i, f"OpenGrid {_short(gpu)} {CLASS_LABELS[cls]} Index", "provider_class",
                              gpu=gpu, provider_class=cls,
                              description=f"On-demand {gpu}, providers classed '{cls}' in provider_meta.")
    for fid, (name, variants) in FAMILIES.items():
        comps = tuple((gpu_slug(v), round(1 / len(variants), 6)) for v in variants if v in names)
        reg[fid] = IndexDef(fid, name, "composite", unit="points", components=comps,
                            description="Base-100 composite of the listed variants, equally weighted. "
                                        "Not the price of any single SKU.")
    comps = tuple((gpu_slug(g), round(1 / len(BENCHMARK_GPUS), 6)) for g in BENCHMARK_GPUS if g in names)
    reg["gpu-compute"] = IndexDef("gpu-compute", "OpenGrid GPU Compute Index", "composite", unit="points",
                                  components=comps,
                                  description="Base-100 composite of the benchmark GPU indices, equally weighted.")
    return reg


REGISTRY: dict[str, IndexDef] = _build_registry()
BASE_IDS = [i for i, d in REGISTRY.items() if d.kind != "composite"]
COMPOSITE_IDS = [i for i, d in REGISTRY.items() if d.kind == "composite"]
_BY_GPU = {(d.segment, d.gpu): i for i, d in REGISTRY.items() if d.kind in ("gpu", "spot")}
_BENCH = set(BENCHMARK_GPUS)


def definition(index_id: str) -> IndexDef | None:
    return REGISTRY.get(index_id)


def stored_version() -> str:
    """The version stamped on rows: the methodology version, plus whether regions were computable.
    A mismatch with what is stored triggers a full rebuild (e.g. when regions.py first appears)."""
    return METHODOLOGY_VERSION + ("+regions" if _region_fn() is not None else "")


def _region_fn():
    """regions.region_group if that module exists, else None (regional indices unavailable)."""
    try:
        import regions
        return regions.region_group
    except ImportError:
        return None


# --------------------------------------------------------------------------
# The math (pure)
# --------------------------------------------------------------------------

def robust_stat(prices) -> tuple[float, str]:
    """Median below TRIM_FROM values; from there the mean after trimming TRIM from each end."""
    xs = sorted(prices)
    n = len(xs)
    if n == 0:
        raise ValueError("no prices")
    if n < TRIM_FROM:
        return float(statistics.median(xs)), "median"
    k = int(n * TRIM)
    core = xs[k:n - k]
    return sum(core) / len(core), "trimmed_mean_20"


def screen(votes: dict[str, tuple[float, str | None]], down: set[str] = frozenset()):
    """(included {provider: price}, detail [...]) after the feed-down and outlier rules."""
    detail, cand = [], {}
    for p, (price, region) in sorted(votes.items()):
        d = {"provider": p, "price": round(price, 6)}
        if region:
            d["region"] = region
        if p in down:
            d["status"] = "excluded_feed_down"
        else:
            cand[p] = price
            d["status"] = "included"
        detail.append(d)
    if len(cand) >= MIN_PROVIDERS:  # a median of fewer is too fragile to screen against
        med = statistics.median(cand.values())
        for d in detail:
            if d["status"] == "included" and not (med / OUTLIER_RATIO <= d["price"] <= med * OUTLIER_RATIO):
                d["status"] = "excluded_outlier"
                cand.pop(d["provider"])
    return cand, detail


def _fmt(t: datetime) -> str:
    return t.strftime("%Y-%m-%d %H:00Z")


def step_base(state: dict | None, votes: dict, down: set, hour: datetime) -> tuple[dict, dict | None]:
    """One hour of a per-GPU index. Returns (row fields, new chain state)."""
    included, detail = screen(votes, down)
    raw, method = robust_stat(included.values()) if included else (None, None)
    row = {"published": False, "level": None, "raw_level": raw, "constituents": len(included),
           "link_constituents": None, "method": method, "segment_no": state and state["segment_no"],
           "reason": None, "detail": detail}
    if len(included) < MIN_PROVIDERS:
        row["reason"] = f"insufficient coverage: {len(included)} eligible provider(s), {MIN_PROVIDERS} required"
        return row, state
    if state is None:
        level, seg, reason = raw, 1, "base hour: level starts at the cross-sectional value"
    else:
        common = sorted(set(included) & set(state["prices"]))
        if not common:
            level, seg = raw, state["segment_no"] + 1
            reason = f"rebased: no constituent in common with {_fmt(state['hour'])}"
        else:
            now_v = robust_stat([included[p] for p in common])[0]
            then_v = robust_stat([state["prices"][p] for p in common])[0]
            level, seg, reason = state["level"] * now_v / then_v, state["segment_no"], None
            row["link_constituents"] = len(common)
    row.update(published=True, level=level, segment_no=seg, reason=reason)
    return row, {"hour": hour, "level": level, "prices": dict(included), "segment_no": seg}


def step_composite(defn: IndexDef, state: dict | None, children: dict[str, dict], hour: datetime):
    """One hour of a base-100 composite: the weighted mean of child relatives, chained.

    Returns (row or None when no child has data, new state).
    """
    total = sum(w for _, w in defn.components) or 1.0
    detail, avail, any_row = [], {}, False
    for cid, w in defn.components:
        r = children.get(cid)
        d = {"index_id": cid, "weight": round(w / total, 6)}
        if r is None:
            d["status"] = "no_data"
        else:
            any_row = True
            if r["published"]:
                avail[cid] = (r["level"], r["segment_no"])
                d.update(status="included", level=r["level"], segment_no=r["segment_no"])
            else:
                d["status"] = "unpublished"
        detail.append(d)
    if not any_row:
        return None, state
    share = sum(w for c, w in defn.components if c in avail) / total
    row = {"published": False, "level": None, "raw_level": None, "constituents": len(avail),
           "link_constituents": None, "method": "chained_weighted_relatives",
           "segment_no": state and state["segment_no"], "reason": None, "detail": detail}
    if share < MIN_COMPOSITE_WEIGHT or len(avail) < MIN_COMPOSITE_COMPONENTS:
        row["reason"] = (f"insufficient coverage: {len(avail)} component(s) / {share:.0%} of component weight "
                         f"published; {MIN_COMPOSITE_COMPONENTS} and {MIN_COMPOSITE_WEIGHT:.0%} required")
        return row, state
    if state is None:
        level, seg, reason = 100.0, 1, "base hour: 100"
    else:
        link = [(c, w) for c, w in defn.components
                if c in avail and c in state["children"] and state["children"][c][1] == avail[c][1]]
        if not link:
            level, seg = 100.0, state["segment_no"] + 1
            reason = f"rebased to 100: no component linkable to {_fmt(state['hour'])}"
        else:
            rel = sum(w * avail[c][0] / state["children"][c][0] for c, w in link) / sum(w for _, w in link)
            level, seg, reason = state["level"] * rel, state["segment_no"], None
            row["link_constituents"] = len(link)
            linked = {c for c, _ in link}
            for d in detail:
                if d["status"] == "included" and d["index_id"] not in linked:
                    d["linked"] = False
    row.update(published=True, level=level, segment_no=seg, reason=reason)
    return row, {"hour": hour, "level": level, "children": avail, "segment_no": seg}


def votes_by_index(rows, region_fn=None) -> dict[str, dict[str, tuple[float, str | None]]]:
    """market_hourly rows of ONE hour -> {index_id: {provider: (lowest price, region)}}."""
    out: dict[str, dict] = {}

    def vote(iid, provider, price, region):
        v = out.setdefault(iid, {})
        cur = v.get(provider)
        if cur is None or price < cur[0]:
            v[provider] = (price, region)

    for r in rows:
        iid = _BY_GPU.get((r["segment"], r["gpu"]))
        if iid is None:
            continue
        price = float(r["min_price"])
        vote(iid, r["provider"], price, None)
        if r["segment"] != "on_demand" or r["gpu"] not in _BENCH:
            continue
        cls = provider_meta.meta(r["provider"]).provider_class
        if cls in CLASS_LABELS:
            vote(f"{iid}.{_slug(cls)}", r["provider"], price, None)
        if region_fn is not None:
            rg = _region_group(region_fn, r["provider"], r["region"] or None, r["country"])
            if rg in REGION_GROUPS:
                vote(f"{iid}.{_slug(rg)}", r["provider"], price, r["region"] or None)
    return out


_rg_cache: dict = {}


def _region_group(fn, provider, region, country):
    key = (provider, region, country)
    if key not in _rg_cache:
        try:
            _rg_cache[key] = fn(provider, region, country)
        except Exception:  # an unknown region is "unknown", never a guess
            _rg_cache[key] = None
    return _rg_cache[key]


# --------------------------------------------------------------------------
# Computation and storage
# --------------------------------------------------------------------------

_ROWS = text("""
    SELECT segment, gpu, provider, region, country, hour, min_price FROM market_hourly
    WHERE hour >= :t0 AND hour <= :t1 AND min_price IS NOT NULL
    ORDER BY hour
""")

# A provider's feed counts as down for hour H when every snapshot in [H-1h, H) failed.
_FEED_DOWN = text("""
    SELECT provider, (date_trunc('hour', fetched_at AT TIME ZONE 'UTC') AT TIME ZONE 'UTC') + interval '1 hour' AS hour
    FROM raw_snapshots
    WHERE fetched_at >= :t0 AND fetched_at < :t1
    GROUP BY 1, 2
    HAVING NOT bool_or(ok)
""")

_STATES = text("""
    SELECT x.index_id, x.hour, x.level, x.segment_no, x.detail
    FROM unnest(CAST(:ids AS text[])) AS d(id)
    CROSS JOIN LATERAL (
      SELECT index_id, hour, level, segment_no, detail FROM index_levels
      WHERE index_id = d.id AND published AND hour < :t0 ORDER BY hour DESC LIMIT 1
    ) x
""")


def _state_from_row(r) -> dict:
    if REGISTRY[r.index_id].kind == "composite":
        ch = {d["index_id"]: (d["level"], d["segment_no"]) for d in r.detail or [] if d.get("status") == "included"}
        return {"hour": r.hour, "level": r.level, "children": ch, "segment_no": r.segment_no}
    pr = {d["provider"]: d["price"] for d in r.detail or [] if d.get("status") == "included"}
    return {"hour": r.hour, "level": r.level, "prices": pr, "segment_no": r.segment_no}


def compute(t0: datetime, t1: datetime, states: dict | None = None, session=None) -> tuple[list[dict], dict]:
    """Index rows for every hour in [t0, t1] from market_hourly, continuing the chains in `states`."""
    states = dict(states or {})
    region_fn = _region_fn()
    own = session is None
    s = session or normalize.SessionLocal()
    try:
        data = s.execute(_ROWS, {"t0": t0, "t1": t1}).all()
        down: dict[datetime, set] = {}
        for r in s.execute(_FEED_DOWN, {"t0": t0 - HOUR, "t1": t1}):
            down.setdefault(r.hour, set()).add(r.provider)
    finally:
        if own:
            s.close()
    by_hour: dict[datetime, list] = {}
    for r in data:
        by_hour.setdefault(r.hour, []).append(r._mapping)
    now = datetime.now(timezone.utc)
    version = stored_version()
    out = []
    for hour in sorted(by_hour):
        votes = votes_by_index(by_hour[hour], region_fn)
        dn = down.get(hour, set())
        this: dict[str, dict] = {}
        for iid, v in votes.items():
            row, states[iid] = step_base(states.get(iid), v, dn, hour)
            this[iid] = row
        for cid in COMPOSITE_IDS:
            row, states[cid] = step_composite(REGISTRY[cid], states.get(cid), this, hour)
            if row is not None:
                this[cid] = row
        for iid, row in this.items():
            out.append({"index_id": iid, "hour": hour, **row,
                        "methodology_version": version, "computed_at": now})
    return out, states


def refresh(full: bool = False) -> dict:
    """Recompute index levels after the rollup moved. Incremental from the last stored hour
    minus OVERLAP; a full rebuild when empty or when the stored version differs (methodology
    changed, or regional indices became computable because regions.py appeared)."""
    with normalize.SessionLocal() as s:
        first_mh, last_mh = s.execute(text("SELECT min(hour), max(hour) FROM market_hourly")).one()
        if last_mh is None:
            return {"hours": 0, "rows": 0}
        last_il = s.execute(text("SELECT max(hour) FROM index_levels")).scalar()
        if not full and last_il is not None:
            stored = s.execute(text("SELECT methodology_version FROM index_levels ORDER BY hour DESC LIMIT 1")).scalar()
            full = stored != stored_version()
        if full or last_il is None:
            t0, states = first_mh, {}
        else:
            t0 = max(first_mh, last_il - OVERLAP)
            states = {r.index_id: _state_from_row(r) for r in s.execute(_STATES, {"ids": list(REGISTRY), "t0": t0})}
    total, c0, first_chunk = 0, t0, True
    while c0 <= last_mh:
        c1 = min(c0 + CHUNK - HOUR, last_mh)
        rows, states = compute(c0, c1, states)
        with normalize.SessionLocal.begin() as s:
            if first_chunk:
                if full or last_il is None:
                    s.execute(text("DELETE FROM index_levels"))
                else:
                    s.execute(text("DELETE FROM index_levels WHERE hour >= :t0"), {"t0": t0})
                first_chunk = False
            if rows:  # executemany: one compiled statement, batched by the driver
                s.connection().execute(IndexLevel.__table__.insert(), rows)
        total += len(rows)
        c0 = c1 + HOUR
    return {"from": t0, "to": last_mh, "rows": total, "full": bool(full or last_il is None)}


def _after_rollup():
    refresh()
    clear_caches()
    try:
        from analytics import stats
        stats.clear_caches()
    except Exception:
        log.exception("stats cache clear failed")
    index_list()  # warm the list in the job thread, not on the next request


rollups.AFTER_REFRESH.append(_after_rollup)


# --------------------------------------------------------------------------
# Readers
# --------------------------------------------------------------------------

WINDOWS = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30),
           "90d": timedelta(days=90)}
# How far before the target time a reference level may be (the index may be unpublished at that exact hour).
TOLERANCE = {"24h": timedelta(hours=1), "7d": timedelta(hours=3), "30d": timedelta(hours=12),
             "90d": timedelta(hours=24), "ytd": timedelta(hours=24)}


def _iso(t):
    return None if t is None else t.isoformat()


def latest_hour(session=None) -> datetime | None:
    """The newest hour the rollup has sampled: 'now' for every index."""
    if session is not None:
        return session.execute(text("SELECT max(hour) FROM market_hourly")).scalar()
    with normalize.SessionLocal() as s:
        return s.execute(text("SELECT max(hour) FROM market_hourly")).scalar()


def _ref(s, iid, target, tol):
    return s.execute(text("""
        SELECT hour, level, segment_no FROM index_levels
        WHERE index_id = :i AND published AND hour <= :t AND hour >= :lo
        ORDER BY hour DESC LIMIT 1"""), {"i": iid, "t": target, "lo": target - tol}).first()


def _first_published(s, iid, segment_no=None):
    return s.execute(text("""
        SELECT hour, level FROM index_levels
        WHERE index_id = :i AND published AND (CAST(:seg AS int) IS NULL OR segment_no = :seg)
        ORDER BY hour LIMIT 1"""), {"i": iid, "seg": segment_no}).first()


def realized_vol(points, end: datetime, window: timedelta) -> dict:
    """Annualized stdev of hourly log returns over `window` ending at `end`.

    `points` is [(hour, level|None, segment_no)] ascending. A return exists only between
    consecutive hours both published in the same chain segment.
    """
    need = int(window / HOUR)
    start = end - window
    rets = []
    for (h0, l0, s0), (h1, l1, s1) in zip(points, points[1:]):
        if h1 <= start or h1 > end or h1 - h0 != HOUR or l0 is None or l1 is None or s0 != s1 or l0 <= 0:
            continue
        rets.append(math.log(l1 / l0))
    out = {"window_hours": need, "returns": len(rets), "annualized": None, "basis": "hourly log returns"}
    if len(rets) < max(2, MIN_WINDOW_COVERAGE * need):
        out["reason"] = (f"insufficient history: {len(rets)} hourly returns, "
                         f"{math.ceil(MIN_WINDOW_COVERAGE * need)} required")
        return out
    out["annualized"] = statistics.stdev(rets) * math.sqrt(HOURS_PER_YEAR)
    return out


@ttl_cache(600, maxsize=2048)
def index_level(index_id: str) -> dict | None:
    """Current level, changes, extremes, volatility and coverage for one index; None if unknown id."""
    d = REGISTRY.get(index_id)
    if d is None:
        return None
    out = {"id": d.id, "name": d.name, "kind": d.kind, "segment": d.segment, "unit": d.unit, "gpu": d.gpu,
           "region_group": d.region_group, "provider_class": d.provider_class,
           "interruptible": d.segment == "spot", "methodology_version": METHODOLOGY_VERSION,
           "published": False, "level": None, "raw_level": None, "hour": None, "reason": None,
           "constituents": 0, "last_published": None, "changes": {}, "high": None, "low": None,
           "volatility": {}, "coverage": {}}
    if d.kind == "regional" and _region_fn() is None:
        out["reason"] = "regional indices unavailable: region grouping (regions.py) is not installed"
    with normalize.SessionLocal() as s:
        now_h = latest_hour(s)
        last = s.execute(text("SELECT * FROM index_levels WHERE index_id = :i ORDER BY hour DESC LIMIT 1"),
                         {"i": index_id}).first()
        if last is None or now_h is None:
            out["reason"] = out["reason"] or "no data: no eligible listing has been recorded for this index"
            return out
        lp = s.execute(text("""SELECT hour, level, segment_no FROM index_levels
                               WHERE index_id = :i AND published ORDER BY hour DESC LIMIT 1"""),
                       {"i": index_id}).first()
        if lp is not None:
            out["last_published"] = {"hour": _iso(lp.hour), "level": lp.level}
        out["hour"] = _iso(now_h)
        if last.hour < now_h:
            out["reason"] = f"no eligible constituents at {_fmt(now_h)} (last data {_fmt(last.hour)})"
        else:
            out.update(published=last.published, raw_level=last.raw_level, constituents=last.constituents,
                       method=last.method, reason=last.reason if not last.published else None,
                       note=last.reason if last.published else None)
            if last.published:
                out["level"] = last.level
        first_any = _first_published(s, index_id)
        seg_first = _first_published(s, index_id, lp.segment_no) if lp is not None else None
        out["coverage"] = {
            "first_published": _iso(first_any and first_any.hour),
            "chain_segment": lp and lp.segment_no,
            "chain_segment_start": _iso(seg_first and seg_first.hour),
            "constituents_now": out["constituents"],
        }
        if lp is not None:
            for name, sql in (("high", "level DESC"), ("low", "level ASC")):
                r = s.execute(text(f"""SELECT hour, level FROM index_levels
                                       WHERE index_id = :i AND published AND segment_no = :seg
                                       ORDER BY {sql}, hour DESC LIMIT 1"""),
                              {"i": index_id, "seg": lp.segment_no}).first()
                out[name] = {"level": r.level, "hour": _iso(r.hour), "since": _iso(seg_first.hour)}
        pts = s.execute(text("""SELECT hour, CASE WHEN published THEN level END AS level, segment_no
                                FROM index_levels WHERE index_id = :i AND hour > :t0 AND hour <= :t1
                                ORDER BY hour"""),
                        {"i": index_id, "t0": now_h - timedelta(days=30) - HOUR, "t1": now_h}).all()
        out["coverage"]["published_hours_30d"] = sum(1 for p in pts if p.level is not None and p.hour > now_h - timedelta(days=30))
        out["coverage"]["hours_30d"] = int(timedelta(days=30) / HOUR)
        for w in ("7d", "30d"):
            out["volatility"][w] = realized_vol([tuple(p) for p in pts], now_h, WINDOWS[w])
        # Last 7 days, hourly, null where unpublished: a board sparkline without a second request.
        out["spark_7d"] = [p.level for p in pts if p.hour > now_h - timedelta(days=7)]
        # Changes, measured from the current published level.
        targets = {**{k: now_h - v for k, v in WINDOWS.items()},
                   "ytd": datetime(now_h.year, 1, 1, tzinfo=timezone.utc)}
        for w, target in targets.items():
            out["changes"][w] = _change(s, index_id, out, lp, target, TOLERANCE[w], first_any)
        out["changes"]["all"] = _change_all(out, lp, seg_first, first_any)
    return out


def _change(s, iid, out, lp, target, tol, first_any) -> dict:
    c = {"pct": None, "from_hour": None, "from_level": None, "reason": None}
    if out["level"] is None:
        c["reason"] = "index not published now"
        return c
    ref = _ref(s, iid, target, tol)
    if ref is None:
        if first_any is None or first_any.hour > target:
            c["reason"] = (f"history does not cover the window: published since "
                           f"{_fmt(first_any.hour) if first_any else 'never'}, window starts {_fmt(target)}")
        else:
            c["reason"] = f"index not published within {int(tol / HOUR)}h before {_fmt(target)}"
        return c
    if ref.segment_no != lp.segment_no:
        c["reason"] = "chain rebased within the window; levels before the rebase are not comparable"
        return c
    c.update(pct=out["level"] / ref.level - 1, from_hour=_iso(ref.hour), from_level=ref.level)
    return c


def _change_all(out, lp, seg_first, first_any) -> dict:
    c = {"pct": None, "from_hour": None, "from_level": None, "reason": None}
    if out["level"] is None or seg_first is None:
        c["reason"] = "index not published now"
        return c
    if out["hour"] == _iso(seg_first.hour):
        c["reason"] = "history covers a single published hour"
        return c
    c.update(pct=out["level"] / seg_first.level - 1, from_hour=_iso(seg_first.hour), from_level=seg_first.level)
    if first_any is not None and first_any.hour < seg_first.hour:
        c["note"] = "since the last chain rebase; earlier levels are not comparable"
    return c


def _summary(lv: dict) -> dict:
    keep = ("id", "name", "kind", "segment", "unit", "gpu", "region_group", "provider_class", "interruptible",
            "published", "level", "raw_level", "hour", "reason", "constituents", "last_published")
    out = {k: lv.get(k) for k in keep}
    out["changes"] = {w: c["pct"] for w, c in lv["changes"].items()}
    out["change_reasons"] = {w: c["reason"] for w, c in lv["changes"].items() if c["pct"] is None}
    out["first_published"] = lv["coverage"].get("first_published")
    # The board's columns, so it needs no per-index follow-up request.
    out["high"], out["low"] = lv["high"], lv["low"]
    out["volatility"], out["coverage"] = lv["volatility"], lv["coverage"]
    out["spark_7d"] = lv.get("spark_7d", [])
    return out


# Which registered indices have any row since :t (one PK probe per id, no table scan).
_HAS_DATA = text("""
    SELECT d.id AS index_id FROM unnest(CAST(:ids AS text[])) AS d(id)
    WHERE EXISTS (SELECT 1 FROM index_levels x WHERE x.index_id = d.id AND x.hour >= :t)
""")


@ttl_cache(600)
def index_list(kind: str | None = None) -> list[dict]:
    """Every index with data in the last 90 days (current level + changes); composites and benchmarks first."""
    with normalize.SessionLocal() as s:
        now_h = latest_hour(s)
        if now_h is None:
            return []
        ids = {r.index_id for r in s.execute(_HAS_DATA, {"ids": list(REGISTRY), "t": now_h - timedelta(days=90)})}
    order = {i: n for n, i in enumerate(REGISTRY)}
    bench = {gpu_slug(g) for g in BENCHMARK_GPUS}

    def rank(i):
        d = REGISTRY[i]
        return (d.kind != "composite", not (d.kind == "gpu" and i in bench), d.kind != "gpu", order[i])

    out = []
    for i in sorted((i for i in ids if i in REGISTRY), key=rank):
        if kind and REGISTRY[i].kind != kind:
            continue
        lv = index_level(i)
        if lv is not None:
            out.append(_summary(lv))
    return out


def index_history(index_id: str, t0: datetime | None, t1: datetime | None, resolution: str = "1h") -> list[dict]:
    """Stored levels in [t0, t1]. 1h: every stored hour (unpublished hours carry their reason).
    1d: per UTC day, the last published level (close), high, low and published-hour count."""
    if resolution == "1d":
        sql = text("""
            SELECT (date_trunc('day', hour AT TIME ZONE 'UTC')) AS day,
                   (array_agg(level ORDER BY hour DESC) FILTER (WHERE published))[1] AS close,
                   (array_agg(raw_level ORDER BY hour DESC) FILTER (WHERE published))[1] AS raw_close,
                   max(level) FILTER (WHERE published) AS high, min(level) FILTER (WHERE published) AS low,
                   count(*) FILTER (WHERE published) AS published_hours,
                   (array_agg(segment_no ORDER BY hour DESC) FILTER (WHERE published))[1] AS segment_no,
                   max(constituents) FILTER (WHERE published) AS max_constituents
            FROM index_levels
            WHERE index_id = :i AND (CAST(:t0 AS timestamptz) IS NULL OR hour >= :t0)
              AND (CAST(:t1 AS timestamptz) IS NULL OR hour <= :t1)
            GROUP BY 1 ORDER BY 1""")
        with normalize.SessionLocal() as s:
            rows = s.execute(sql, {"i": index_id, "t0": t0, "t1": t1}).all()
        return [{"t": r.day.replace(tzinfo=timezone.utc).isoformat(), "level": r.close, "raw_level": r.raw_close,
                 "high": r.high, "low": r.low, "published_hours": r.published_hours,
                 "segment_no": r.segment_no, "constituents": r.max_constituents,
                 "published": r.published_hours > 0} for r in rows]
    sql = text("""
        SELECT hour, published, level, raw_level, constituents, link_constituents, segment_no, reason
        FROM index_levels
        WHERE index_id = :i AND (CAST(:t0 AS timestamptz) IS NULL OR hour >= :t0)
          AND (CAST(:t1 AS timestamptz) IS NULL OR hour <= :t1)
        ORDER BY hour""")
    with normalize.SessionLocal() as s:
        rows = s.execute(sql, {"i": index_id, "t0": t0, "t1": t1}).all()
    return [{"t": r.hour.isoformat(), "published": r.published, "level": r.level, "raw_level": r.raw_level,
             "constituents": r.constituents, "link_constituents": r.link_constituents,
             "segment_no": r.segment_no, "reason": r.reason} for r in rows]


def constituents_now(index_id: str) -> dict:
    """The latest stored hour's constituents (each with its status), plus since-when each was recorded."""
    d = REGISTRY.get(index_id)
    with normalize.SessionLocal() as s:
        r = s.execute(text("""SELECT hour, detail FROM index_levels WHERE index_id = :i
                              ORDER BY hour DESC LIMIT 1"""), {"i": index_id}).first()
    if r is None:
        return {"hour": None, "constituents": []}
    items = list(r.detail or [])
    if d is not None and d.kind != "composite" and d.gpu:
        first = _first_hours(d.segment)
        for c in items:
            f = first.get((d.gpu, c["provider"]))
            c["recorded_since"] = _iso(f)
            c["provider_class"] = provider_meta.meta(c["provider"]).provider_class
    else:
        for c in items:
            c["name"] = REGISTRY[c["index_id"]].name if c["index_id"] in REGISTRY else c["index_id"]
    return {"hour": _iso(r.hour), "constituents": items}


@ttl_cache(600)
def _first_hours(segment: str):
    return rollups.first_hours(segment)


def clear_caches():
    index_level.cache_clear()
    index_list.cache_clear()
    _first_hours.cache_clear()

