"""Official, settlement-grade fixings of the OpenGrid indices (methodology/settlement.md).

`index_levels` is a display series: recomputed with a 3 h overlap and fully rebuilt on a
methodology change. A settlement price must instead be FIXED, IMMUTABLE, REPRODUCIBLE and
AUDITABLE. A fixing is that: a row in the append-only ledger `index_fixings`, taken only after
the hour can no longer be revised, carrying every input its value is recomputed from, a hash of
those inputs, and a hash chain over the whole ledger.

    hourly   for hour H, once H <= min(rollup frontier, index frontier) - FINALITY_LAG:
             the published index level at H, recomputed here from the frozen inputs
             (constituent votes + statuses, the chain link state) by frozen S1 math
    daily    fixing_time D+1 00:00 UTC ("daily settlement for D"): the TWAP (arithmetic mean)
             of D's 24 hourly fixings, over hours that are published, carry no severe flag and
             lie in the chain segment of the day's last such hour; >= MIN_DAILY_HOURS needed

Fallback waterfall (daily): 1. TWAP over eligible hours if >= MIN_DAILY_HOURS; 2. otherwise
not published, with the reason. A value is never carried forward or invented.

Restatement: a correction is a NEW row (status 'restated', `restates` = the original id) with
the corrected inputs, reason and actor. The original stays. Only within the correction window;
only an admin; the value is always recomputed from inputs (staff never type a value).
"""

from __future__ import annotations

import hashlib
import json
import logging
import statistics
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

import normalize
from analytics import indices, rollups
from api.common import gpu_slug
from jobs import job

log = logging.getLogger(__name__)

METHODOLOGY = "settlement"
SETTLEMENT_VERSION = "S1.0"     # the settlement rules + the frozen recompute math below
INPUTS_SCHEMA = "og.fixing.inputs/1"

# The per-GPU on-demand benchmark indices and the GPU Compute Index. Components before composites:
# a composite's hourly fixing reads its components' fixings for the same hour.
SETTLEMENT_INDICES: tuple[str, ...] = tuple(
    gpu_slug(g) for g in indices.BENCHMARK_GPUS if gpu_slug(g) in indices.REGISTRY) + ("gpu-compute",)

HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
# The rollup and the index refresh rewrite every hour >= (their newest hour - OVERLAP). Hour H is
# therefore out of reach of every future incremental refresh once the frontier is >= H + OVERLAP
# + 1h. SAFETY_MARGIN adds 2 h on top of that strict bound (1 h beyond it): it absorbs a refresh
# that is running while we read, observations normalized late (a slow poll or normalize cycle) and
# clock skew between the job host and the database. Daily fixings follow ~5 h after 00:00 UTC.
SAFETY_MARGIN = timedelta(hours=2)
FINALITY_LAG = rollups.OVERLAP + SAFETY_MARGIN

# Data sufficiency, stricter than indices.MIN_PROVIDERS (3) / MIN_COMPOSITE_COMPONENTS (2).
SETTLEMENT_MIN_CONSTITUENTS = 4
SETTLEMENT_MIN_COMPONENTS = 3
DAILY_HOURS = 24
MIN_DAILY_HOURS = 18            # 75% of the day's hours must be eligible for a TWAP

# Surveillance thresholds.
CONCENTRATION_WARN = 0.5        # one constituent carries >= 50% of the statistic's weight
CONCENTRATION_SEVERE = 1.0      # one constituent IS the statistic
JUMP_PCT = 0.25                 # a constituent's vote moved > 25% since the previous published hour
PRE_FIX_HOURS = 3               # "immediately before the daily fix": the last 3 hourly fixings of the day
NEW_LISTING_HOURS = 6           # a constituent first included within 6 h of the fix
FEED_OUTAGE_SEVERE_SHARE = 0.5  # >= half of the candidate votes excluded as feed-down

# Correction windows, measured from the original fixing's publication (created_at).
HOURLY_CORRECTION_WINDOW = timedelta(hours=24)
DAILY_CORRECTION_BUSINESS_DAYS = 2

MAX_HOURS_PER_RUN = 48          # catch-up is bounded per run; the next run continues
GENESIS = "0" * 64
LOCK = 7014630018               # pg advisory lock: one ledger writer at a time
INITIAL_EFFECTIVE = datetime(2000, 1, 1, tzinfo=timezone.utc)
FIXED = ("final", "restated")   # statuses that can carry a value


class Refused(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


# --------------------------------------------------------------------------
# Canonical hashing (pure)
# --------------------------------------------------------------------------

def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256(obj) -> str:
    return hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()


def _iso(t: datetime | None) -> str | None:
    return None if t is None else t.astimezone(timezone.utc).isoformat()


def _ts(v) -> datetime:
    return v if isinstance(v, datetime) else datetime.fromisoformat(v)


def row_payload(r: dict) -> dict:
    """The fields the row hash covers: everything except seq (assigned by the database)."""
    return {"fixing_id": r["fixing_id"], "index_id": r["index_id"], "fixing_type": r["fixing_type"],
            "fixing_time": _iso(_ts(r["fixing_time"])), "status": r["status"], "value": r["value"],
            "reason": r["reason"], "methodology_version": r["methodology_version"],
            "settlement_version": r["settlement_version"], "input_hash": r["input_hash"], "flags": r["flags"],
            "restates": r["restates"], "actor": r["actor"], "prev_hash": r["prev_hash"],
            "created_at": _iso(_ts(r["created_at"]))}


def row_hash(r: dict) -> str:
    return sha256(row_payload(r))


def fixing_id_for(index_id: str, fixing_type: str, fixing_time: datetime, revision: int) -> str:
    return "fx-" + hashlib.sha256(f"{index_id}|{fixing_type}|{_iso(fixing_time)}|{revision}".encode()).hexdigest()[:24]


def _close(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= 1e-9 * max(1.0, abs(b))


# --------------------------------------------------------------------------
# Frozen recompute math, settlement version S1 (pure; never imports the live index math, so a
# later methodology change cannot change how an old fixing verifies)
# --------------------------------------------------------------------------

def _stat(xs, p) -> tuple[float, str]:
    xs = sorted(xs)
    n = len(xs)
    if n < p["trim_from"]:
        return float(statistics.median(xs)), "median"
    k = int(n * p["trim"])
    core = xs[k:n - k]
    return sum(core) / len(core), "trimmed_mean_20"


def stat_weights(prices: dict[str, float], p) -> dict[str, float]:
    """Each constituent's share of the statistic (median: the middle one or two; trimmed mean: 1/core)."""
    order = sorted(prices, key=lambda k: (prices[k], k))
    n = len(order)
    if n == 0:
        return {}
    if n < p["trim_from"]:
        lo, hi = (n - 1) // 2, n // 2
        return {k: (1.0 if lo == hi else 0.5) if i in (lo, hi) else 0.0 for i, k in enumerate(order)}
    k = int(n * p["trim"])
    return {name: (0.0 if i < k or i >= n - k else 1 / (n - 2 * k)) for i, name in enumerate(order)}


FACT_STATUSES = ("excluded_feed_down", "excluded_correction")  # inputs, not derived


def recompute_gpu(inputs: dict) -> dict:
    """An hourly per-GPU fixing from its inputs alone: screening, statistic, chain link."""
    p = inputs["params"]
    votes = {c["provider"]: c["price"] for c in inputs["constituents"] if c["status"] not in FACT_STATUSES}
    statuses = {c["provider"]: c["status"] for c in inputs["constituents"] if c["status"] in FACT_STATUSES}
    included = dict(votes)
    if len(included) >= p["min_providers"]:
        med = statistics.median(included.values())
        for prov, price in votes.items():
            if not (med / p["outlier_ratio"] <= price <= med * p["outlier_ratio"]):
                included.pop(prov)
                statuses[prov] = "excluded_outlier"
    for prov in included:
        statuses[prov] = "included"
    out = {"value": None, "raw": None, "method": None, "segment_no": None, "statuses": statuses,
           "included": sorted(included), "link_constituents": None, "reason": None}
    if included:
        out["raw"], out["method"] = _stat(included.values(), p)
    if len(included) < p["min_providers"]:
        out["reason"] = f"insufficient coverage: {len(included)} eligible provider(s), {p['min_providers']} required"
        return out
    link = inputs.get("link")
    if link is None:
        out.update(value=out["raw"], segment_no=1)
        return out
    common = sorted(set(included) & set(link["prices"]))
    if not common:
        out.update(value=out["raw"], segment_no=link["segment_no"] + 1)
        return out
    now_v = _stat([included[c] for c in common], p)[0]
    then_v = _stat([link["prices"][c] for c in common], p)[0]
    out.update(value=link["level"] * now_v / then_v, segment_no=link["segment_no"], link_constituents=len(common))
    return out


def recompute_composite(inputs: dict) -> dict:
    """An hourly composite fixing: chained weighted relatives of the components' levels."""
    p = inputs["params"]
    comps = inputs["components"]
    total = sum(c["weight"] for c in comps) or 1.0
    avail = {c["index_id"]: (c["level"], c["segment_no"]) for c in comps if c["status"] == "included"}
    share = sum(c["weight"] for c in comps if c["index_id"] in avail) / total
    out = {"value": None, "raw": None, "method": "chained_weighted_relatives", "segment_no": None,
           "included": sorted(avail), "link_constituents": None, "reason": None, "share": share}
    if share < p["min_weight"] or len(avail) < p["min_components"]:
        out["reason"] = (f"insufficient coverage: {len(avail)} component(s) / {share:.0%} of component weight "
                         f"published; {p['min_components']} and {p['min_weight']:.0%} required")
        return out
    link = inputs.get("link")
    if link is None:
        out.update(value=100.0, segment_no=1)
        return out
    ch = {k: tuple(v) for k, v in link["children"].items()}
    linked = [c for c in comps if c["index_id"] in avail and c["index_id"] in ch
              and ch[c["index_id"]][1] == avail[c["index_id"]][1]]
    if not linked:
        out.update(value=100.0, segment_no=link["segment_no"] + 1)
        return out
    rel = (sum(c["weight"] * avail[c["index_id"]][0] / ch[c["index_id"]][0] for c in linked)
           / sum(c["weight"] for c in linked))
    out.update(value=link["level"] * rel, segment_no=link["segment_no"], link_constituents=len(linked))
    return out


def recompute_daily(inputs: dict) -> dict:
    """TWAP of the day's eligible hourly fixings (waterfall step 1), else nothing (step 2)."""
    p = inputs["params"]
    cand = [h for h in inputs["hours"] if h["status"] in FIXED and h["value"] is not None and not h["severe"]]
    seg = max(cand, key=lambda h: h["fixing_time"])["segment_no"] if cand else None
    used = sorted(h["fixing_id"] for h in cand if h["segment_no"] == seg)
    vals = [h["value"] for h in cand if h["segment_no"] == seg]
    out = {"value": None, "method": "twap_hourly_fixings", "used": used, "hours_used": len(vals),
           "segment_no": seg, "reason": None}
    if len(vals) < p["min_hours"]:
        out["reason"] = (f"insufficient eligible hours: {len(vals)} of {p['hours']} hourly fixings usable "
                         f"({p['min_hours']} required); not published, no value carried forward")
        return out
    out["value"] = sum(vals) / len(vals)
    return out


def recompute(inputs: dict) -> dict:
    kind = inputs["kind"]
    if inputs["fixing_type"] == "daily":
        return recompute_daily(inputs)
    if kind == "composite":
        return recompute_composite(inputs)
    if kind == "no_data":
        return {"value": None, "reason": inputs.get("reason") or "no data", "included": []}
    return recompute_gpu(inputs)


# --------------------------------------------------------------------------
# Surveillance and data-sufficiency flags (pure)
# --------------------------------------------------------------------------

def _flag(code, severity, detail, **kw):
    return {"code": code, "severity": severity, "detail": detail, **kw}


def hourly_flags(inputs: dict, res: dict) -> list[dict]:
    flags = []
    if inputs["kind"] == "no_data":
        return flags
    if inputs["kind"] == "composite":
        inc = [c for c in inputs["components"] if c["status"] == "included"]
        if inc and len(inc) < SETTLEMENT_MIN_COMPONENTS:
            flags.append(_flag("low_constituents", "severe",
                               f"{len(inc)} component(s) published, settlement minimum {SETTLEMENT_MIN_COMPONENTS}"))
        tot = sum(c["weight"] for c in inc)
        top = max(inc, key=lambda c: c["weight"], default=None)
        if top and tot and top["weight"] / tot >= CONCENTRATION_WARN:
            sev = "severe" if top["weight"] / tot >= CONCENTRATION_SEVERE else "warn"
            flags.append(_flag("concentration", sev, f"{top['index_id']} carries {top['weight'] / tot:.0%} of the "
                               "composite's weight", index_id=top["index_id"], weight=round(top["weight"] / tot, 6)))
        unfixed = [c["index_id"] for c in inc if c.get("fixing_status") not in FIXED or not c.get("fixing_matches")]
        if unfixed:
            flags.append(_flag("component_not_fixed", "warn", "included component(s) without a matching settlement-grade "
                               "hourly fixing this hour: " + ", ".join(unfixed), components=unfixed))
    else:
        cons = inputs["constituents"]
        inc = {c["provider"]: c["price"] for c in cons if res.get("statuses", {}).get(c["provider"]) == "included"}
        if inc and len(inc) < SETTLEMENT_MIN_CONSTITUENTS:
            flags.append(_flag("low_constituents", "severe", f"{len(inc)} included constituent(s), settlement minimum "
                               f"{SETTLEMENT_MIN_CONSTITUENTS}"))
        w = stat_weights(inc, inputs["params"])
        if w:
            top = max(w, key=lambda k: (w[k], k))
            if w[top] >= CONCENTRATION_WARN:
                sev = "severe" if w[top] >= CONCENTRATION_SEVERE else "warn"
                flags.append(_flag("concentration", sev, f"{top} carries {w[top]:.0%} of the statistic's weight "
                                   f"({len(inc)} constituents, {res.get('method')})", provider=top,
                                   weight=round(w[top], 6), decisive=[k for k, v in w.items() if v >= CONCENTRATION_WARN]))
        down = [c["provider"] for c in cons if c["status"] == "excluded_feed_down"]
        if down:
            share = len(down) / len(cons)
            flags.append(_flag("feed_outage", "severe" if share >= FEED_OUTAGE_SEVERE_SHARE else "warn",
                               f"{len(down)} of {len(cons)} candidate feed(s) failing this hour: " + ", ".join(down),
                               providers=down))
        link = inputs.get("link")
        if link is not None:
            for prov, price in sorted(inc.items()):
                then = link["prices"].get(prov)
                if then is None:
                    flags.append(_flag("new_constituent", "info", f"{prov} was not a constituent at {link['hour']} "
                                       "(chain-linking keeps a joiner from moving the level)", provider=prov,
                                       weight=round(w.get(prov, 0.0), 6)))
                elif then > 0 and abs(price / then - 1) > JUMP_PCT:
                    flags.append(_flag("price_jump", "warn", f"{prov} moved {price / then - 1:+.1%} since {link['hour']} "
                                       f"(threshold {JUMP_PCT:.0%})", provider=prov, change=round(price / then - 1, 6),
                                       weight=round(w.get(prov, 0.0), 6)))
        untraced = [c["provider"] for c in cons if c["provider"] in inc and not (c.get("evidence") or {}).get("traceable")]
        if untraced:
            flags.append(_flag("evidence_untraceable", "warn", "no stored raw snapshot found for: " + ", ".join(untraced),
                               providers=untraced))
    if res.get("value") is not None and res.get("link_constituents") is not None and res["link_constituents"] < 2:
        flags.append(_flag("thin_link", "warn", f"the hour's move is measured on {res['link_constituents']} constituent"))
    link = inputs.get("link")
    if res.get("value") is not None and link is not None and res.get("segment_no") != link["segment_no"]:
        flags.append(_flag("rebased", "warn", "chain rebased this hour: not comparable with earlier levels"))
    return flags


def daily_flags(inputs: dict, res: dict) -> list[dict]:
    flags = []
    hours = sorted(inputs["hours"], key=lambda h: h["fixing_time"])
    excluded = [h for h in hours if h["status"] in FIXED and h["value"] is not None and h["severe"]]
    if excluded:
        flags.append(_flag("severe_hours_excluded", "info", f"{len(excluded)} hourly fixing(s) with severe flags left "
                           "out of the TWAP", fixings=[h["fixing_id"] for h in excluded]))
    segs = {h["segment_no"] for h in hours if h["status"] in FIXED and h["value"] is not None}
    if len(segs) > 1:
        flags.append(_flag("rebased_in_day", "warn", "the chain was rebased during the day; only the last segment's "
                           "hours are averaged"))
    late = [h for h in hours[-PRE_FIX_HOURS:] if h["status"] in FIXED]
    for h in late:
        for f in h.get("flags", []):
            if f["code"] == "price_jump":
                influential = f.get("weight", 0) >= CONCENTRATION_WARN
                flags.append(_flag("pre_fix_jump" if not influential else "pre_fix_influential_jump",
                                   "severe" if influential else "warn",
                                   f"{f['provider']} moved {f['change']:+.1%} at {h['fixing_time']}, within "
                                   f"{PRE_FIX_HOURS} h of the fix" + (" while carrying "
                                   f"{f['weight']:.0%} of the statistic" if influential else ""),
                                   provider=f["provider"], hour=h["fixing_time"]))
    for h in hours[-NEW_LISTING_HOURS:]:
        for f in h.get("flags", []):
            if f["code"] == "new_constituent" and f.get("weight", 0) > 0:
                flags.append(_flag("pre_fix_new_listing", "warn", f"{f['provider']} entered the index at "
                                   f"{h['fixing_time']}, within {NEW_LISTING_HOURS} h of the fix",
                                   provider=f["provider"], hour=h["fixing_time"]))
    outages = sum(1 for h in hours for f in h.get("flags", []) if f["code"] == "feed_outage")
    if outages:
        flags.append(_flag("feed_outage_hours", "info", f"{outages} hour(s) of the day had failing constituent feeds"))
    missing = [h["fixing_time"] for h in hours if h["status"] == "missing"]
    if missing:
        flags.append(_flag("hours_not_fixed", "info", f"{len(missing)} hour(s) of the day have no hourly fixing "
                           "(before settlement fixings began)"))
    return flags


def severe(flags) -> list[dict]:
    return [f for f in flags if f["severity"] == "severe"]


# --------------------------------------------------------------------------
# Schedule / finality
# --------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def floor_day(t: datetime) -> datetime:
    return t.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def final_through(s, now: datetime | None = None) -> datetime | None:
    """The newest hour no refresh can revise any more, or None when nothing is final yet."""
    now = now or _now()
    mh = s.execute(text("SELECT max(hour) FROM market_hourly")).scalar()
    il = s.execute(text("SELECT max(hour) FROM index_levels")).scalar()
    if mh is None or il is None:
        return None
    return min(mh, il, rollups.floor_hour(now)) - FINALITY_LAG


def add_business_days(t: datetime, n: int) -> datetime:
    out = t
    while n > 0:
        out += DAY
        if out.weekday() < 5:
            n -= 1
    return out


def correction_deadline(fixing_type: str, published_at: datetime) -> datetime:
    if fixing_type == "hourly":
        return published_at + HOURLY_CORRECTION_WINDOW
    return add_business_days(published_at, DAILY_CORRECTION_BUSINESS_DAYS)


# --------------------------------------------------------------------------
# Methodology governance
# --------------------------------------------------------------------------

def base_version(v: str) -> str:
    return (v or "").split("+", 1)[0]


def ensure_initial_notices(now: datetime | None = None) -> None:
    """The first version of each scope gets a notice automatically (there was no prior version to give
    notice of). Every LATER version needs a notice recorded by an admin (record_notice)."""
    from sqlalchemy.dialects.postgresql import insert

    from store.settlement import MethodologyNotice

    now = now or _now()
    with normalize.SessionLocal.begin() as s:
        for scope, version in (("indices", indices.METHODOLOGY_VERSION), ("settlement", SETTLEMENT_VERSION)):
            if s.execute(text("SELECT 1 FROM methodology_notices WHERE scope = :s LIMIT 1"), {"s": scope}).first():
                continue
            s.execute(insert(MethodologyNotice).values(
                scope=scope, version=version, effective_from=INITIAL_EFFECTIVE, notice_days=0, actor="system",
                summary="Initial version: in force from the first settlement fixing (no prior version, no notice period).",
                published_at=now).on_conflict_do_nothing(index_elements=["scope", "version"]))


def record_notice(scope: str, version: str, effective_from: datetime, notice_days: int, summary: str,
                  actor: str, now: datetime | None = None) -> dict:
    from store.settlement import MethodologyNotice

    now = now or _now()
    if scope not in ("indices", "settlement"):
        raise Refused(422, "scope must be 'indices' or 'settlement'")
    if not version or len(version) > 16:
        raise Refused(422, "version must be 1-16 characters")
    if not summary or len(summary.strip()) < 10:
        raise Refused(422, "summary must describe the change (10+ characters)")
    if notice_days < 0 or notice_days > 365:
        raise Refused(422, "notice_days must be 0-365")
    if effective_from < now + timedelta(days=notice_days) - timedelta(minutes=5):
        raise Refused(422, f"effective_from must be at least notice_days ({notice_days}) after now")
    with normalize.SessionLocal.begin() as s:
        if s.execute(text("SELECT 1 FROM methodology_notices WHERE scope = :s AND version = :v"),
                     {"s": scope, "v": version}).first():
            raise Refused(409, f"a notice for {scope} version {version!r} already exists")
        n = MethodologyNotice(scope=scope, version=version, effective_from=effective_from, notice_days=notice_days,
                              summary=summary.strip(), actor=actor, published_at=now)
        s.add(n)
        s.flush()
        return _notice(n)


def _notice(n) -> dict:
    return {"scope": n.scope, "version": n.version, "effective_from": _iso(n.effective_from),
            "notice_days": n.notice_days, "summary": n.summary, "actor": n.actor, "published_at": _iso(n.published_at)}


def notices() -> list[dict]:
    from sqlalchemy import select

    from store.settlement import MethodologyNotice

    with normalize.SessionLocal() as s:
        return [_notice(n) for n in s.scalars(select(MethodologyNotice).order_by(MethodologyNotice.published_at,
                                                                                 MethodologyNotice.id))]


def _governance_gap(s, methodology_version: str, at: datetime) -> str | None:
    """Why fixings may not be published under these versions at `at` (None when they may)."""
    for scope, v in (("indices", base_version(methodology_version)), ("settlement", SETTLEMENT_VERSION)):
        eff = s.execute(text("SELECT effective_from FROM methodology_notices WHERE scope = :s AND version = :v"),
                        {"s": scope, "v": v}).scalar()
        if eff is None:
            return f"governance: {scope} methodology version {v!r} has no recorded change notice"
        if eff > at:
            return f"governance: {scope} methodology version {v!r} is effective only from {_iso(eff)}"
    return None


# --------------------------------------------------------------------------
# Building fixings from the database
# --------------------------------------------------------------------------

def _gpu_params():
    return {"min_providers": indices.MIN_PROVIDERS, "trim_from": indices.TRIM_FROM, "trim": indices.TRIM,
            "outlier_ratio": indices.OUTLIER_RATIO, "settlement_min_constituents": SETTLEMENT_MIN_CONSTITUENTS}


def _comp_params():
    return {"min_weight": indices.MIN_COMPOSITE_WEIGHT, "min_components": indices.MIN_COMPOSITE_COMPONENTS,
            "settlement_min_components": SETTLEMENT_MIN_COMPONENTS}


_ROW = text("""SELECT index_id, hour, published, level, raw_level, constituents, link_constituents, method,
                      segment_no, reason, detail, methodology_version, computed_at
               FROM index_levels WHERE index_id = :i AND hour = :h""")
_LINK = text("""SELECT hour, level, segment_no, detail FROM index_levels
                WHERE index_id = :i AND published AND hour < :h ORDER BY hour DESC LIMIT 1""")
# Each of the provider's listings of this GPU: its last observation at or before H (one index probe each).
_OBS = text("""
    SELECT c.listing_id, o.observed_at, o.price
    FROM compute_listings c
    CROSS JOIN LATERAL (
        SELECT x.observed_at, x.price_per_gpu_hour AS price FROM listing_observations x
        WHERE x.provider = c.provider AND x.listing_id = c.listing_id AND x.observed_at <= :h
        ORDER BY x.observed_at DESC LIMIT 1
    ) o
    WHERE c.provider = :p AND c.canonical_gpu_name = :g""")
# The snapshot that recorded the vote's price, plus the provider's latest successful round at or before H.
_SNAPS = text("""
    SELECT id, endpoint, fetched_at, coalesce(payload_hash, sha256) AS sha FROM raw_snapshots
    WHERE provider = :p AND ok AND (
        fetched_at = ANY(CAST(:obs AS timestamptz[]))
        OR (fetched_at <= :h AND fetched_at > :h - interval '2 hours' AND fetched_at >= (
            SELECT max(fetched_at) FROM raw_snapshots WHERE provider = :p AND ok AND fetched_at <= :h
        ) - interval '120 seconds'))
    ORDER BY fetched_at DESC, id LIMIT 50""")


def _evidence(s, provider: str, gpu: str, price: float, hour: datetime) -> dict:
    obs = [r for r in s.execute(_OBS, {"p": provider, "g": gpu, "h": hour})
           if r.price is not None and abs(float(r.price) - price) < 5e-7]
    obs.sort(key=lambda r: (r.observed_at, r.listing_id), reverse=True)
    snaps = s.execute(_SNAPS, {"p": provider, "h": hour, "obs": [r.observed_at for r in obs[:3]]}).all()
    return {"observation": ({"listing_id": obs[0].listing_id, "observed_at": _iso(obs[0].observed_at),
                             "price": round(float(obs[0].price), 6)} if obs else None),
            "snapshots": [{"id": r.id, "endpoint": r.endpoint, "fetched_at": _iso(r.fetched_at),
                           "payload_sha256": r.sha} for r in snaps],
            "traceable": bool(snaps)}


def _effective(s, index_id: str, fixing_type: str, fixing_time: datetime):
    """The effective row for a slot: the latest restatement if any, else the original."""
    return s.execute(text("""
        SELECT * FROM index_fixings WHERE index_id = :i AND fixing_type = :t AND fixing_time = :f
        ORDER BY seq DESC LIMIT 1"""), {"i": index_id, "t": fixing_type, "f": fixing_time}).first()


def build_hourly(s, index_id: str, hour: datetime) -> dict:
    """Inputs + outcome of one hourly fixing, read from index_levels (and raw evidence)."""
    d = indices.REGISTRY[index_id]
    row = s.execute(_ROW, {"i": index_id, "h": hour}).first()
    base = {"schema": INPUTS_SCHEMA, "index_id": index_id, "fixing_type": "hourly", "hour": _iso(hour),
            "unit": d.unit, "settlement_version": SETTLEMENT_VERSION}
    if row is None:
        inputs = {**base, "kind": "no_data", "reason": "no data: no eligible listing recorded for this hour",
                  "constituents": [], "link": None, "index_row": None}
        return {"inputs": inputs, "methodology_version": indices.stored_version()}
    lk = s.execute(_LINK, {"i": index_id, "h": hour}).first()
    index_row = {"published": row.published, "level": row.level, "raw_level": row.raw_level, "method": row.method,
                 "segment_no": row.segment_no, "constituents": row.constituents,
                 "link_constituents": row.link_constituents, "reason": row.reason,
                 "methodology_version": row.methodology_version, "computed_at": _iso(row.computed_at)}
    if d.kind == "composite":
        comps = []
        for c in row.detail or []:
            item = {"index_id": c["index_id"], "weight": c["weight"], "status": c["status"],
                    "level": c.get("level"), "segment_no": c.get("segment_no")}
            fx = _effective(s, c["index_id"], "hourly", hour)
            item["fixing_id"] = fx.fixing_id if fx else None
            item["fixing_status"] = fx.status if fx else None
            item["fixing_matches"] = bool(fx and _close(fx.value, item["level"]))
            comps.append(item)
        link = None
        if lk is not None:
            link = {"hour": _iso(lk.hour), "level": lk.level, "segment_no": lk.segment_no,
                    "children": {c["index_id"]: [c["level"], c["segment_no"]]
                                 for c in lk.detail or [] if c.get("status") == "included"}}
        inputs = {**base, "kind": "composite", "params": _comp_params(), "components": comps, "link": link,
                  "index_row": index_row}
    else:
        cons = []
        for c in row.detail or []:
            item = {"provider": c["provider"], "price": c["price"], "status": c["status"]}
            if c.get("region"):
                item["region"] = c["region"]
            item["evidence"] = _evidence(s, c["provider"], d.gpu, c["price"], hour)
            cons.append(item)
        link = None
        if lk is not None:
            link = {"hour": _iso(lk.hour), "level": lk.level, "segment_no": lk.segment_no,
                    "prices": {c["provider"]: c["price"] for c in lk.detail or [] if c.get("status") == "included"}}
        inputs = {**base, "kind": "gpu", "gpu": d.gpu, "params": _gpu_params(), "constituents": cons, "link": link,
                  "index_row": index_row}
    return {"inputs": inputs, "methodology_version": row.methodology_version}


def outcome_hourly(s, inputs: dict, methodology_version: str, *, check_index_row: bool) -> dict:
    """status / value / reason / flags of an hourly fixing from its inputs."""
    res = recompute(inputs)
    flags = hourly_flags(inputs, res)
    reason = None
    if inputs["kind"] == "no_data":
        reason = inputs["reason"]
    elif res["value"] is None:
        reason = res["reason"]
    else:
        ir = inputs.get("index_row") or {}
        if check_index_row and not (ir.get("published") and _close(res["value"], ir.get("level"))
                                    and res.get("segment_no") == ir.get("segment_no")):
            flags.append(_flag("recompute_mismatch", "severe",
                               f"settlement recompute {res['value']!r} != published index level {ir.get('level')!r}"))
            reason = "not published: settlement recompute disagrees with the published index level"
        inc = len(res["included"])
        need = SETTLEMENT_MIN_COMPONENTS if inputs["kind"] == "composite" else SETTLEMENT_MIN_CONSTITUENTS
        if reason is None and inc < need:
            reason = f"settlement data sufficiency: {inc} constituent(s) included, {need} required"
    if reason is None:
        reason = _governance_gap(s, methodology_version, _ts(inputs["hour"]))
    published = reason is None
    return {"status": "final" if published else "not_published", "value": res["value"] if published else None,
            "reason": reason, "flags": flags, "result": res}


def build_daily(s, index_id: str, day_end: datetime) -> dict:
    d = indices.REGISTRY[index_id]
    hours = []
    for i in range(DAILY_HOURS):
        t = day_end - DAY + i * HOUR
        fx = _effective(s, index_id, "hourly", t)
        if fx is None:
            hours.append({"fixing_id": None, "fixing_time": _iso(t), "status": "missing", "value": None,
                          "segment_no": None, "severe": False, "row_hash": None, "flags": []})
            continue
        seg = (recompute(fx.inputs) or {}).get("segment_no") if fx.value is not None else None
        hours.append({"fixing_id": fx.fixing_id, "fixing_time": _iso(t), "status": fx.status, "value": fx.value,
                      "segment_no": seg, "severe": bool(severe(fx.flags)), "row_hash": fx.row_hash,
                      "flags": [{k: f[k] for k in ("code", "severity", "provider", "change", "weight") if k in f}
                                for f in fx.flags]})
    inputs = {"schema": INPUTS_SCHEMA, "index_id": index_id, "fixing_type": "daily", "kind": d.kind,
              "unit": d.unit, "settlement_version": SETTLEMENT_VERSION, "day": _iso(day_end - DAY),
              "fixing_time": _iso(day_end),
              "params": {"hours": DAILY_HOURS, "min_hours": MIN_DAILY_HOURS, "statistic": "arithmetic_mean"},
              "hours": hours}
    versions = [r.methodology_version for r in s.execute(text("""
        SELECT DISTINCT methodology_version FROM index_fixings
        WHERE index_id = :i AND fixing_type = 'hourly' AND fixing_time >= :a AND fixing_time < :b"""),
        {"i": index_id, "a": day_end - DAY, "b": day_end})]
    mv = sorted(versions)[-1] if versions else indices.stored_version()
    return {"inputs": inputs, "methodology_version": mv, "versions": versions}


def outcome_daily(s, inputs: dict, methodology_version: str, versions=()) -> dict:
    res = recompute(inputs)
    flags = daily_flags(inputs, res)
    if len(set(versions)) > 1:
        flags.append(_flag("methodology_changed_in_day", "severe", "hourly fixings of this day were taken under "
                           "different methodology versions: " + ", ".join(sorted(set(versions)))))
    reason = res["reason"]
    if reason is None and severe(flags):
        reason = "not published: surveillance hold (" + ", ".join(f["code"] for f in severe(flags)) + ")"
    if reason is None:
        reason = _governance_gap(s, methodology_version, _ts(inputs["fixing_time"]))
    published = reason is None
    return {"status": "final" if published else "not_published", "value": res["value"] if published else None,
            "reason": reason, "flags": flags, "result": res}


def _evidence_rows(inputs: dict) -> list[tuple[int, str | None]]:
    out = {}
    for c in inputs.get("constituents") or []:
        for snap in (c.get("evidence") or {}).get("snapshots", []):
            out[snap["id"]] = snap.get("payload_sha256")
    return sorted(out.items())


def append(s, *, index_id: str, fixing_type: str, fixing_time: datetime, status: str, value, reason,
           methodology_version: str, inputs: dict, flags: list, restates: str | None = None,
           actor: str = "system", now: datetime | None = None) -> dict | None:
    """Append one row to the ledger (caller's transaction). None when the original already exists."""
    from store.settlement import FixingEvidence, IndexFixing

    s.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": LOCK})
    revision = s.execute(text("""SELECT count(*) FROM index_fixings
                                 WHERE index_id = :i AND fixing_type = :t AND fixing_time = :f"""),
                         {"i": index_id, "t": fixing_type, "f": fixing_time}).scalar()
    if restates is None and revision:
        return None  # idempotent: the original fixing for this slot is already in the ledger
    prev = s.execute(text("SELECT row_hash FROM index_fixings ORDER BY seq DESC LIMIT 1")).scalar() or GENESIS
    # created_at: microsecond precision, as Postgres stores it, so the row hash survives the round trip
    now = (now or _now()).astimezone(timezone.utc)
    inputs = json.loads(canonical(inputs))  # exactly what the database will return
    flags = json.loads(canonical(flags))
    r = {"fixing_id": fixing_id_for(index_id, fixing_type, fixing_time, revision), "index_id": index_id,
         "fixing_type": fixing_type, "fixing_time": fixing_time, "status": status,
         "value": None if value is None else float(value), "reason": reason,
         "methodology_version": methodology_version, "settlement_version": SETTLEMENT_VERSION,
         "inputs": inputs, "input_hash": sha256(inputs), "flags": flags, "restates": restates, "actor": actor,
         "prev_hash": prev, "created_at": now}
    r["row_hash"] = row_hash(r)
    obj = IndexFixing(**r)
    s.add(obj)
    s.flush()
    ev = _evidence_rows(inputs)
    if ev:
        s.add_all([FixingEvidence(fixing_seq=obj.seq, snapshot_id=sid, payload_sha256=sha) for sid, sha in ev])
    r["seq"] = obj.seq
    return r


# --------------------------------------------------------------------------
# The job
# --------------------------------------------------------------------------

def _alert(kind: str, title: str, severity: str, detail: dict, dedupe: str | None = None) -> None:
    try:
        from alerts import ops

        ops.alert(kind, title, severity=severity, detail=detail, dedupe=dedupe)
    except Exception:  # noqa: BLE001 - alerting never breaks the ledger
        log.exception("settlement alert %s failed", kind)


def _after_append(s, r: dict) -> None:
    sev = severe(r["flags"])
    detail = {"fixing_id": r["fixing_id"], "index_id": r["index_id"], "fixing_type": r["fixing_type"],
              "fixing_time": _iso(r["fixing_time"]), "status": r["status"], "reason": r["reason"],
              "flags": [f["code"] for f in sev], "suggested_action": "review /v1/fixings/" + r["fixing_id"]}
    if r["fixing_type"] == "daily":
        if r["status"] == "not_published":
            _alert("fixing_not_published", f"Daily settlement fixing {r['index_id']} {_iso(r['fixing_time'])} NOT "
                   f"published: {r['reason']}", "major", detail)
        elif sev:
            _alert("fixing_severe_flags", f"Daily fixing {r['index_id']} published with severe flags", "major", detail)
        return
    if r["status"] == "not_published" or sev:
        # at most one hourly alert per index per UTC day: thin hours repeat
        day = floor_day(r["fixing_time"])
        earlier = s.execute(text("""SELECT 1 FROM index_fixings WHERE index_id = :i AND fixing_type = 'hourly'
                                    AND fixing_time >= :d AND fixing_time < :t
                                    AND (status = 'not_published' OR flags @> '[{"severity": "severe"}]')
                                    LIMIT 1"""), {"i": r["index_id"], "d": day, "t": r["fixing_time"]}).first()
        if not earlier:
            _alert("fixing_not_published" if r["status"] == "not_published" else "fixing_severe_flags",
                   f"Hourly fixing {r['index_id']} {_iso(r['fixing_time'])}: {r['reason'] or 'severe flags'}",
                   "notable", detail, dedupe=f"{r['index_id']}:{day.date()}")


def run(now: datetime | None = None, start: datetime | None = None, max_hours: int = MAX_HOURS_PER_RUN) -> dict:
    """Take every fixing that is due and not yet taken. Idempotent: re-running adds nothing."""
    now = now or _now()
    ensure_initial_notices(now)
    out = {"hourly": 0, "daily": 0, "final_through": None}
    with normalize.SessionLocal() as s:
        ft = final_through(s, now)
        if ft is None:
            return out
        last = {r.index_id: r.last_time for r in s.execute(text("""
            SELECT index_id, max(fixing_time) AS last_time FROM index_fixings
            WHERE fixing_type = 'hourly' AND restates IS NULL GROUP BY index_id"""))}
    out["final_through"] = _iso(ft)
    first = start or floor_day(ft)
    plan: dict[datetime, list[str]] = {}
    for iid in SETTLEMENT_INDICES:
        h = last[iid] + HOUR if iid in last else rollups.floor_hour(first)
        for _ in range(max_hours):
            if h > ft:
                break
            plan.setdefault(h, []).append(iid)
            h += HOUR
    for hour in sorted(plan):
        for iid in plan[hour]:  # SETTLEMENT_INDICES order: components before the composite
            with normalize.SessionLocal.begin() as s:
                b = build_hourly(s, iid, hour)
                o = outcome_hourly(s, b["inputs"], b["methodology_version"], check_index_row=True)
                r = append(s, index_id=iid, fixing_type="hourly", fixing_time=hour, status=o["status"],
                           value=o["value"], reason=o["reason"], methodology_version=b["methodology_version"],
                           inputs=b["inputs"], flags=o["flags"], now=now)
                if r:
                    out["hourly"] += 1
                    _after_append(s, r)
    for iid in SETTLEMENT_INDICES:
        with normalize.SessionLocal() as s:
            lh = s.execute(text("""SELECT min(fixing_time), max(fixing_time) FROM index_fixings
                                   WHERE index_id = :i AND fixing_type = 'hourly'"""), {"i": iid}).one()
            ld = s.execute(text("""SELECT max(fixing_time) FROM index_fixings
                                   WHERE index_id = :i AND fixing_type = 'daily' AND restates IS NULL"""),
                           {"i": iid}).scalar()
        if lh[0] is None:
            continue
        day_end = ld + DAY if ld else floor_day(lh[0]) + DAY
        while day_end - HOUR <= lh[1]:  # the day's last hour is fixed
            with normalize.SessionLocal.begin() as s:
                b = build_daily(s, iid, day_end)
                o = outcome_daily(s, b["inputs"], b["methodology_version"], b["versions"])
                r = append(s, index_id=iid, fixing_type="daily", fixing_time=day_end, status=o["status"],
                           value=o["value"], reason=o["reason"], methodology_version=b["methodology_version"],
                           inputs=b["inputs"], flags=o["flags"], now=now)
                if r:
                    out["daily"] += 1
                    _after_append(s, r)
            day_end += DAY
    log.info("index fixings: %s", out)
    return out


@job("index_fixings", every_seconds=600, initial_delay_seconds=150)
def _fixings_job():
    return run()


# --------------------------------------------------------------------------
# Restatement
# --------------------------------------------------------------------------

def restate(fixing_id: str, *, actor: str, reason: str, exclude: list[dict] | None = None,
            include: list[str] | None = None, rederive: bool = False, now: datetime | None = None) -> dict:
    """Append a correction of `fixing_id` (or of its original). Admin-only at the API layer."""
    now = now or _now()
    reason = (reason or "").strip()
    if len(reason) < 10:
        raise Refused(422, "a restatement needs a reason (10+ characters)")
    exclude, include = list(exclude or []), list(include or [])
    with normalize.SessionLocal.begin() as s:
        target = s.execute(text("SELECT * FROM index_fixings WHERE fixing_id = :f"), {"f": fixing_id}).first()
        if target is None:
            raise Refused(404, f"unknown fixing {fixing_id!r}")
        original = target if target.restates is None else s.execute(
            text("SELECT * FROM index_fixings WHERE fixing_id = :f"), {"f": target.restates}).first()
        deadline = correction_deadline(original.fixing_type, original.created_at)
        if now > deadline:
            raise Refused(409, f"outside the correction window: {original.fixing_type} fixings can be restated until "
                               f"{_iso(deadline)} (published {_iso(original.created_at)})")
        current = _effective(s, original.index_id, original.fixing_type, original.fixing_time)
        ft = original.fixing_time
        if original.fixing_type == "daily":
            if exclude or include:
                raise Refused(422, "a daily fixing is restated by recomputing it from the effective hourly fixings; "
                                   "correct the hourly fixings first (exclude/include apply to hourly fixings)")
            b = build_daily(s, original.index_id, ft)
            inputs, mv = b["inputs"], b["methodology_version"]
        else:
            if rederive:
                b = build_hourly(s, original.index_id, ft)
                inputs, mv = b["inputs"], b["methodology_version"]
            else:
                inputs, mv = json.loads(canonical(current.inputs)), current.methodology_version
            if exclude or include:
                if inputs["kind"] != "gpu":
                    raise Refused(422, "exclude/include apply to per-GPU constituents; composites are re-derived")
                by = {c["provider"]: c for c in inputs["constituents"]}
                for e in exclude:
                    p, why = e.get("provider"), (e.get("reason") or "").strip()
                    if p not in by:
                        raise Refused(422, f"{p!r} is not a constituent of this fixing")
                    if not why:
                        raise Refused(422, f"excluding {p!r} needs a reason")
                    by[p]["status"], by[p]["correction_reason"] = "excluded_correction", why
                for p in include:
                    if p not in by or by[p]["status"] != "excluded_correction":
                        raise Refused(422, f"{p!r} was not excluded by a correction; nothing to re-include")
                    by[p]["status"] = "included"
                    by[p].pop("correction_reason", None)
        inputs["correction"] = {"reason": reason, "actor": actor, "original": original.fixing_id,
                                "supersedes": current.fixing_id, "exclude": exclude, "include": include,
                                "rederive": bool(rederive or original.fixing_type == "daily"), "at": _iso(now)}
        if original.fixing_type == "daily":
            o = outcome_daily(s, inputs, mv, b.get("versions", ()))
        else:
            o = outcome_hourly(s, inputs, mv, check_index_row=False)
        r = append(s, index_id=original.index_id, fixing_type=original.fixing_type, fixing_time=ft,
                   status="restated", value=o["value"], reason=o["reason"] or reason, methodology_version=mv,
                   inputs=inputs, flags=o["flags"], restates=original.fixing_id, actor=actor, now=now)
    _alert("fixing_restated", f"Fixing {original.index_id} {original.fixing_type} {_iso(ft)} restated by {actor}: "
           f"{current.value!r} -> {r['value']!r}", "notable",
           {"fixing_id": r["fixing_id"], "restates": original.fixing_id, "reason": reason, "actor": actor,
            "suggested_action": "public notice is listed at /v1/fixings/restatements"})
    return get_fixing(r["fixing_id"])


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------

def _rowdict(r) -> dict:
    return dict(r._mapping)


def verify_fixing(fixing_id: str) -> dict | None:
    with normalize.SessionLocal() as s:
        r = s.execute(text("SELECT * FROM index_fixings WHERE fixing_id = :f"), {"f": fixing_id}).first()
        if r is None:
            return None
        row = _rowdict(r)
        prev = s.execute(text("SELECT row_hash FROM index_fixings WHERE seq < :q ORDER BY seq DESC LIMIT 1"),
                         {"q": r.seq}).scalar()
        nxt = s.execute(text("SELECT prev_hash FROM index_fixings WHERE seq > :q ORDER BY seq LIMIT 1"),
                        {"q": r.seq}).first()
        ev_ids = {x.snapshot_id for x in s.execute(text("SELECT snapshot_id FROM fixing_evidence WHERE fixing_seq = :q"),
                                                   {"q": r.seq})}
        want = {sid for sid, _ in _evidence_rows(r.inputs)}
        present = {x.id: x for x in s.execute(text("""
            SELECT id, coalesce(payload_hash, sha256) AS sha, (payload IS NOT NULL OR payload_hash IS NOT NULL) AS body
            FROM raw_snapshots WHERE id = ANY(CAST(:ids AS bigint[]))"""), {"ids": sorted(want)})}
        refs_ok, link_note = None, None
        if r.fixing_type == "daily":
            refs_ok = True
            for h in r.inputs.get("hours", []):
                if h.get("fixing_id") is None:
                    continue
                x = s.execute(text("SELECT value, row_hash FROM index_fixings WHERE fixing_id = :f"),
                              {"f": h["fixing_id"]}).first()
                if x is None or x.row_hash != h["row_hash"] or not _close(x.value, h["value"]):
                    refs_ok = False
        elif (r.inputs.get("link") or None) is not None:
            lf = _effective(s, r.index_id, "hourly", _ts(r.inputs["link"]["hour"]))
            if lf is not None and lf.value is not None:
                link_note = {"link_hour": r.inputs["link"]["hour"], "link_fixing_id": lf.fixing_id,
                             "attested": _close(lf.value, r.inputs["link"]["level"])}
    try:
        res = recompute(row["inputs"])
    except Exception as exc:  # noqa: BLE001 - malformed (tampered) inputs are a mismatch, not a crash
        res = {"value": None, "error": f"inputs cannot be recomputed: {type(exc).__name__}: {exc}"}
    stored_v = row["value"]
    if stored_v is not None:
        # a published value must be exactly what the inputs give, and the inputs must be sufficient
        value_ok = row["status"] in FIXED and _close(res.get("value"), stored_v)
        if row["fixing_type"] == "hourly":
            need = SETTLEMENT_MIN_COMPONENTS if row["inputs"].get("kind") == "composite" else SETTLEMENT_MIN_CONSTITUENTS
            value_ok = value_ok and len(res.get("included") or []) >= need
    else:  # nothing published: there is no value to reproduce, the reason is on the row
        value_ok = row["status"] in ("not_published", "restated") and bool(row["reason"])
    checks = {
        "input_hash": sha256(row["inputs"]) == row["input_hash"],
        "value": value_ok,
        "row_hash": row_hash(row) == row["row_hash"],
        "prev_link": row["prev_hash"] == (prev or GENESIS),
        "next_link": nxt is None or nxt.prev_hash == row["row_hash"],
        "evidence_index": ev_ids == want,
        "evidence_retained": all(sid in present for sid in want),
    }
    if refs_ok is not None:
        checks["hourly_references"] = refs_ok
    ok = all(checks.values())
    notes = [{"recompute_error": res["error"]}] if res.get("error") else []
    if link_note:
        notes.append(link_note)
    if want and present and not all(present[sid].body for sid in present):
        notes.append({"evidence_bodies": f"{sum(1 for x in present.values() if x.body)} of {len(present)} referenced "
                                         "snapshots still hold their body"})
    return {"fixing_id": fixing_id, "ok": ok, "result": "ok" if ok else "mismatch", "checks": checks,
            "stored": {"value": stored_v, "input_hash": row["input_hash"], "row_hash": row["row_hash"],
                       "prev_hash": row["prev_hash"], "status": row["status"]},
            "recomputed": {"value": res.get("value"), "input_hash": sha256(row["inputs"]), "row_hash": row_hash(row),
                           "method": res.get("method"), "segment_no": res.get("segment_no"),
                           "included": res.get("included") or res.get("used")},
            "notes": notes, "settlement_version": row["settlement_version"]}


def verify_chain(from_seq: int | None = None, limit: int = 1000) -> dict:
    with normalize.SessionLocal() as s:
        q0 = from_seq or 0
        prev = s.execute(text("SELECT row_hash FROM index_fixings WHERE seq < :q ORDER BY seq DESC LIMIT 1"),
                         {"q": q0}).scalar() or GENESIS
        rows = s.execute(text("SELECT * FROM index_fixings WHERE seq >= :q ORDER BY seq LIMIT :n"),
                         {"q": q0, "n": limit + 1}).all()
        total = s.execute(text("SELECT count(*), max(seq) FROM index_fixings")).one()
    more = len(rows) > limit
    rows = rows[:limit]
    breaks = []
    for r in rows:
        d = _rowdict(r)
        problems = []
        if d["prev_hash"] != prev:
            problems.append("prev_hash does not link to the previous row")
        if row_hash(d) != d["row_hash"]:
            problems.append("row_hash does not match the row's contents")
        if sha256(d["inputs"]) != d["input_hash"]:
            problems.append("input_hash does not match the stored inputs")
        if problems:
            breaks.append({"seq": d["seq"], "fixing_id": d["fixing_id"], "problems": problems})
        prev = d["row_hash"]
    return {"ok": not breaks, "result": "ok" if not breaks else "mismatch", "checked": len(rows),
            "from_seq": rows[0].seq if rows else None, "to_seq": rows[-1].seq if rows else None,
            "next_seq": rows[-1].seq + 1 if more else None, "breaks": breaks[:50], "break_count": len(breaks),
            "ledger_rows": total[0], "head_seq": total[1], "head_hash": prev if not more else None}


# --------------------------------------------------------------------------
# Readers
# --------------------------------------------------------------------------

def _public(r, superseded_by: list | None = None, effective: bool | None = None) -> dict:
    d = indices.REGISTRY.get(r.index_id)
    out = {"fixing_id": r.fixing_id, "seq": r.seq, "index_id": r.index_id, "index_name": d.name if d else r.index_id,
           "fixing_type": r.fixing_type, "fixing_time": _iso(r.fixing_time), "status": r.status, "value": r.value,
           "unit": d.unit if d else None, "reason": r.reason, "methodology_version": r.methodology_version,
           "settlement_version": r.settlement_version, "flags": r.flags,
           "severity": "severe" if severe(r.flags) else ("warn" if any(f["severity"] == "warn" for f in r.flags)
                                                        else None),
           "restates": r.restates, "superseded_by": superseded_by or [], "effective": effective,
           "actor": r.actor, "input_hash": r.input_hash, "prev_hash": r.prev_hash, "row_hash": r.row_hash,
           "created_at": _iso(r.created_at),
           "links": {"evidence": f"/v1/fixings/{r.fixing_id}", "verify": f"/v1/fixings/{r.fixing_id}/verify",
                     "methodology": "/methodology/settlement"}}
    out["correction_deadline"] = (_iso(correction_deadline(r.fixing_type, r.created_at))
                                  if r.restates is None else None)
    return out


def _decorate(s, rows) -> list[dict]:
    originals = sorted({r.restates or r.fixing_id for r in rows})
    rest: dict[str, list] = {}
    if originals:
        for x in s.execute(text("""SELECT fixing_id, restates FROM index_fixings
                                   WHERE restates = ANY(CAST(:ids AS text[])) ORDER BY seq"""), {"ids": originals}):
            rest.setdefault(x.restates, []).append(x.fixing_id)
    out = []
    for r in rows:
        chain = rest.get(r.restates or r.fixing_id, [])
        latest = chain[-1] if chain else (r.restates or r.fixing_id)
        sup = chain if r.restates is None else chain[chain.index(r.fixing_id) + 1:]
        out.append(_public(r, sup, latest == r.fixing_id))
    return out


def list_fixings(index_id: str, fixing_type: str | None = None, status: str | None = None,
                 t0: datetime | None = None, t1: datetime | None = None, limit: int = 100, offset: int = 0,
                 effective_only: bool = False) -> tuple[list[dict], int]:
    where = ["index_id = :i"]
    params = {"i": index_id, "n": limit, "o": offset}
    if fixing_type:
        where.append("fixing_type = :t")
        params["t"] = fixing_type
    if status:
        where.append("status = :s")
        params["s"] = status
    if t0:
        where.append("fixing_time >= :t0")
        params["t0"] = t0
    if t1:
        where.append("fixing_time <= :t1")
        params["t1"] = t1
    if effective_only:
        where.append("""seq = (SELECT max(x.seq) FROM index_fixings x WHERE x.index_id = index_fixings.index_id
                         AND x.fixing_type = index_fixings.fixing_type AND x.fixing_time = index_fixings.fixing_time)""")
    w = " AND ".join(where)
    with normalize.SessionLocal() as s:
        total = s.execute(text(f"SELECT count(*) FROM index_fixings WHERE {w}"), params).scalar()
        rows = s.execute(text(f"SELECT * FROM index_fixings WHERE {w} ORDER BY fixing_time DESC, seq DESC "
                              "LIMIT :n OFFSET :o"), params).all()
        return _decorate(s, rows), total


def get_fixing(fixing_id: str) -> dict | None:
    with normalize.SessionLocal() as s:
        r = s.execute(text("SELECT * FROM index_fixings WHERE fixing_id = :f"), {"f": fixing_id}).first()
        if r is None:
            return None
        out = _decorate(s, [r])[0]
        ev = s.execute(text("""
            SELECT e.snapshot_id, e.payload_sha256, r.provider, r.endpoint, r.fetched_at,
                   (r.id IS NOT NULL) AS retained, (r.payload IS NOT NULL OR r.payload_hash IS NOT NULL) AS body
            FROM fixing_evidence e LEFT JOIN raw_snapshots r ON r.id = e.snapshot_id
            WHERE e.fixing_seq = :q ORDER BY e.snapshot_id"""), {"q": r.seq}).all()
    out["inputs"] = r.inputs
    out["evidence"] = [{"snapshot_id": e.snapshot_id, "payload_sha256": e.payload_sha256, "provider": e.provider,
                        "endpoint": e.endpoint, "fetched_at": _iso(e.fetched_at), "retained": bool(e.retained),
                        "body_retained": bool(e.body)} for e in ev]
    return out


def restatements(limit: int = 100, offset: int = 0) -> tuple[list[dict], int]:
    with normalize.SessionLocal() as s:
        total = s.execute(text("SELECT count(*) FROM index_fixings WHERE restates IS NOT NULL")).scalar()
        rows = s.execute(text("""SELECT * FROM index_fixings WHERE restates IS NOT NULL
                                 ORDER BY seq DESC LIMIT :n OFFSET :o"""), {"n": limit, "o": offset}).all()
        out = _decorate(s, rows)
    for o, r in zip(out, rows):
        c = (r.inputs or {}).get("correction") or {}
        o["correction"] = {k: c.get(k) for k in ("reason", "actor", "original", "supersedes", "exclude", "include",
                                                  "rederive", "at")}
    return out, total


def latest(index_ids=None) -> dict:
    """Per settlement index: the latest effective daily and hourly fixing, plus the provisional
    (not yet final) index level when one is newer."""
    ids = list(index_ids or SETTLEMENT_INDICES)
    out = []
    with normalize.SessionLocal() as s:
        ft = final_through(s)
        for iid in ids:
            item = {"index_id": iid, "index_name": indices.REGISTRY[iid].name if iid in indices.REGISTRY else iid,
                    "unit": indices.REGISTRY[iid].unit if iid in indices.REGISTRY else None}
            for t in ("daily", "hourly"):
                r = s.execute(text("""SELECT * FROM index_fixings WHERE index_id = :i AND fixing_type = :t
                                      ORDER BY fixing_time DESC, seq DESC LIMIT 1"""), {"i": iid, "t": t}).first()
                item[t] = _decorate(s, [r])[0] if r else None
            pv = s.execute(text("""SELECT hour, published, level, reason FROM index_levels WHERE index_id = :i
                                   ORDER BY hour DESC LIMIT 1"""), {"i": iid}).first()
            last_h = item["hourly"] and _ts(item["hourly"]["fixing_time"])
            item["provisional"] = None
            if pv is not None and (last_h is None or pv.hour > last_h):
                item["provisional"] = {"status": "provisional", "fixing_time": _iso(pv.hour),
                                       "value": pv.level if pv.published else None,
                                       "reason": None if pv.published else pv.reason,
                                       "final_after": _iso(pv.hour + FINALITY_LAG),
                                       "note": "index level not yet final: the rollup can still revise this hour; "
                                               "not a fixing, not in the ledger"}
            out.append(item)
        head = s.execute(text("SELECT seq, row_hash FROM index_fixings ORDER BY seq DESC LIMIT 1")).first()
    return {"indices": out, "final_through": _iso(ft), "ledger_head": {"seq": head.seq, "row_hash": head.row_hash}
            if head else None}


def schedule() -> dict:
    return {"settlement_version": SETTLEMENT_VERSION, "settlement_indices": list(SETTLEMENT_INDICES),
            "hourly": {"fixing_time": "top of each UTC hour H",
                       "taken_after": f"H <= min(rollup frontier, index frontier) - {int(FINALITY_LAG / HOUR)} h",
                       "rollup_overlap_hours": int(rollups.OVERLAP / HOUR),
                       "safety_margin_hours": int(SAFETY_MARGIN / HOUR)},
            "daily": {"fixing_time": "00:00 UTC (end of day D)", "statistic": "TWAP: arithmetic mean of D's hourly "
                      "fixings 00:00..23:00 UTC", "min_hours": MIN_DAILY_HOURS, "hours": DAILY_HOURS},
            "sufficiency": {"min_constituents": SETTLEMENT_MIN_CONSTITUENTS,
                            "min_components": SETTLEMENT_MIN_COMPONENTS},
            "surveillance": {"concentration_warn": CONCENTRATION_WARN, "concentration_severe": CONCENTRATION_SEVERE,
                             "jump_pct": JUMP_PCT, "pre_fix_hours": PRE_FIX_HOURS,
                             "new_listing_hours": NEW_LISTING_HOURS,
                             "feed_outage_severe_share": FEED_OUTAGE_SEVERE_SHARE},
            "correction_windows": {"hourly_hours": int(HOURLY_CORRECTION_WINDOW / HOUR),
                                   "daily_business_days": DAILY_CORRECTION_BUSINESS_DAYS}}
