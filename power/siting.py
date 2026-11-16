"""Where a small datacenter (about 1 to 50 MW) could run on spare, cheap electricity: a county ranking and a point
query that join the physical sites, the nodal and hub prices, the wasted energy and the EIA-930 grid data
(methodology/power-siting.md). Everything is computed on read; nothing is stored.

    ranking(state=, iso=, min_idle_mw=, window_days=90, weights=None, limit=100, until=, as_of=, year=)
    near(lat, lon, km=50, window_days=90, weights=None, until=, as_of=, year=)
    match_nodes()                          the ERCOT node -> county rule, with its hit rate and refusals
    score(parts, weights=None)             the weighted sum (pure, testable by hand)
    parse_weights("idle:0.4,queue:-0.2")   for the route

The county is the common grain: plants, queues and North Dakota flaring all carry one; priced nodes do not (neither
ERCOT nor SPP publishes coordinates or counties). Every row keeps its parts apart, says the grain of its price
(node / zone / hub / none), and shows a missing part as missing (None), never as zero.

Internal: the ranking mixes public sources (EIA, Queued Up 2026, ERCOT) with internal ones (SPP prices, CAISO and
SPP curtailment, North Dakota flaring), so the whole read is internal and admin-only.
"""

from __future__ import annotations

import math
import re
from datetime import date, timedelta

from power import geo
from power.sites import reads as site_reads

# --------------------------------------------------------------------------- the weights (founders: change here)

# Each part is first scaled to 0..1 (SCALES), then weighted. The positive weights add up to 1, so a county with every
# part at its best scores 1 before the queue penalty. Why these:
#   idle 0.30       spare generation already connected is the strongest sign a connection has room
#   price_below_20  0.25: what the datacenter would actually pay; real-time intervals below $20/MWh
#   retiring 0.10   capacity leaving within 36 months frees a connection, but plans slip
#   flaring 0.10    gas burned for lack of a pipeline is energy with no buyer; but it is gas, not electricity
#   exporter 0.10   a balancing authority that makes more than it uses has power to spare on the margin
#   rarely_running 0.05, price_below_0 0.05, curtailment 0.05: supporting signs; curtailment is ISO-wide, so it
#                   cannot tell one county from another in the same market and stays small
#   queue -0.15     generation queued in the county competes for the same connection capacity (a penalty)
WEIGHTS: dict[str, float] = {
    "idle": 0.30, "retiring": 0.10, "rarely_running": 0.05,
    "price_below_20": 0.25, "price_below_0": 0.05,
    "flaring": 0.10, "curtailment": 0.05,
    "exporter": 0.10,
    "queue": -0.15,
}
# Value at which a part reaches 1 (more counts the same). Sized for a 1-50 MW load: 200 idle MW is four times the
# largest load in scope, so more idle capacity does not make a site better for it.
SCALES: dict[str, float] = {"idle_mw": 200.0, "retiring_mw": 200.0, "rarely_running_plants": 2.0,
                            "flared_average_mw": 50.0, "curtailed_average_mw": 1000.0, "queued_mw": 2000.0}
RETIRING_MONTHS = 36
LARGE_PLANT_MW = 100.0     # "large rarely-running plant": at least this much non-storage nameplate ...
RARELY_CF = 0.10           # ... and a capacity factor below this in the year read
CHEAP = 20.0               # $/MWh, strictly below (as power/nodal.py)
DEFAULT_WINDOW_DAYS = 90
MAX_WINDOW_DAYS = 365
EXPORT_DAYS = 365          # the net-exporter test looks at the last 12 months of EIA-930
HOURS_PER_MONTH = 730.0    # 8,760 / 12: a month of flaring as average MW

NOTE = ("Internal: mixes internal sources (SPP prices, CAISO / SPP curtailment, North Dakota flaring). Each part is "
        "shown beside the score; a missing part is null, never 0, and adds nothing to the score. Curtailment is "
        "ISO-wide (regional), not county-level. Flaring by county is North Dakota only (the World Bank cells carry "
        "no county; see /sites/near). No public source lists large-load requests by project, so large loads are "
        "null everywhere.")
LARGE_LOADS_NOTE = "no data: no public source lists large-load interconnection requests by project (ERCOT, Queued Up)"

# --------------------------------------------------------------------------- the ERCOT node -> county rule

# An ERCOT resource node is tied to a county only through a plant: one of the node's keys (its substation code from
# ERCOT's Settlement Points List, or its name before the first "_") must EQUAL one of a Texas ERCOT plant's keys
# (its EIA-860M name with letters and digits only, or that name without the generic words below). A key shorter
# than MIN_KEY or itself generic is refused (too many plants share it), and a node whose keys reach plants in more
# than one county is refused as ambiguous. No prefix, fuzzy or distance matching: ERCOT's codes are abbreviations
# ("LEG" for Limestone), and a near match would tie a node to the wrong county without anyone noticing.
MIN_KEY = 5
GENERIC = frozenset({
    "POWER", "PLANT", "STATION", "GENERATING", "GENERATION", "ELECTRIC", "ENERGY", "CENTER", "FACILITY", "PROJECT",
    "WIND", "WINDFARM", "SOLAR", "FARM", "STORAGE", "BATTERY", "BESS", "ESS", "LLC", "INC", "LP", "COGEN",
    "COGENERATION", "PEAKING", "PEAKER", "UNIT", "UNITS", "PHASE", "RANCH", "HYBRID", "PV", "GAS", "ENERGYCENTER",
    "NORTH", "SOUTH", "EAST", "WEST", "CENTRAL", "TEXAS", "COUNTY", "CITY", "LAKE", "RIVER", "CREEK", "HILLS",
    "VALLEY", "PANHANDLE", "COASTAL", "ALL", "THE", "OF", "AND", "I", "II", "III", "IV",
})
RESOURCE_TYPES = ("RN", "PCCRN", "LCCRN", "PUN")
NODE_MATCH = True          # False: never tie an ERCOT node to a county (every ERCOT county falls back to its hub)

# --------------------------------------------------------------------------- price locations without a node

# CAISO prices no single reference hub (power/geo.py). A county is tied to a trading hub only where it lies wholly
# on one side of Path 15 and inside the area CAISO's hub covers (NP15: PG&E north of Path 15; SP15: SCE and SDG&E).
# Counties near the Path 15 / Path 26 boundary (Monterey, San Benito, Fresno, Kings, Tulare, Kern, San Luis Obispo,
# Merced, Madera) are left out on purpose: they get no price rather than a guessed one. The county's plants must
# also report to CISO (a SMUD or LADWP county is not CAISO).
CAISO_COUNTY_HUB: dict[str, str] = {
    **{c: "TH_NP15_GEN-APND" for c in (
        "san francisco", "san mateo", "santa clara", "alameda", "contra costa", "marin", "sonoma", "napa", "solano",
        "sacramento", "placer", "yolo", "sutter", "yuba", "butte", "shasta", "tehama", "glenn", "colusa", "lake",
        "mendocino", "humboldt", "san joaquin", "stanislaus", "santa cruz")},
    **{c: "TH_SP15_GEN-APND" for c in (
        "los angeles", "orange", "san diego", "riverside", "san bernardino", "ventura", "santa barbara")},
}
# MISO prices a hub per state where the state is named in the hub (MISO's spelling); geo.STATE_HUB has four of them
MISO_STATE_HUB: dict[str, str] = {
    "MI": "MICHIGAN.HUB", "IL": "ILLINOIS.HUB", "IN": "INDIANA.HUB", "TX": "TEXAS.HUB",
    **{s: h for s, (iso, h) in geo.STATE_HUB.items() if iso == "MISO"},
}
ISO_OF_BA = {"CISO": "CAISO", "ERCO": "ERCOT", "NYIS": "NYISO", "ISNE": "ISO-NE", "PJM": "PJM", "MISO": "MISO",
             "SWPP": "SPP"}
CURTAILMENT_ISOS = ("CAISO", "SPP", "ERCOT")


def _market_area(state: str | None) -> str | None:
    # context/geo.py's state -> market table, reached through power/geo.py: tests/test_context.py allows only that
    # module (and api/context.py) to import context
    return geo.geo.market_area(state)


def _session():
    from power import storage

    return storage._session()


def norm(s: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def _ym(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1)


def _window(window_days: int) -> int:
    return max(1, min(int(window_days), MAX_WINDOW_DAYS))


# --------------------------------------------------------------------------- the score (pure)

def parse_weights(s: str | None) -> dict[str, float]:
    """"idle:0.4,queue:-0.2" -> {"idle": 0.4, "queue": -0.2}. Unknown names and non-numbers raise ValueError."""
    out: dict[str, float] = {}
    for part in (s or "").split(","):
        if not part.strip():
            continue
        k, sep, v = part.partition(":")
        k = k.strip()
        if not sep or k not in WEIGHTS:
            raise ValueError(f"weights: '{part.strip()}' is not name:number with a name in {sorted(WEIGHTS)}")
        try:
            out[k] = float(v)
        except ValueError:
            raise ValueError(f"weights: '{v.strip()}' is not a number") from None
        if not math.isfinite(out[k]):
            raise ValueError(f"weights: '{v.strip()}' is not a finite number")
    return out


def weights_of(weights: dict[str, float] | None) -> dict[str, float]:
    w = dict(WEIGHTS)
    for k, v in (weights or {}).items():
        if k not in WEIGHTS:
            raise ValueError(f"unknown weight {k!r}; known: {sorted(WEIGHTS)}")
        w[k] = float(v)
    return w


def _cap(x: float | None, scale: float) -> float | None:
    if x is None:
        return None
    return max(0.0, min(float(x) / scale, 1.0))


def normalized(raw: dict) -> dict[str, float | None]:
    """Raw values -> 0..1 per part (None stays None: missing, not zero)."""
    exp = raw.get("net_exporter")
    return {
        "idle": _cap(raw.get("idle_mw"), SCALES["idle_mw"]),
        "retiring": _cap(raw.get("retiring_mw"), SCALES["retiring_mw"]),
        "rarely_running": _cap(raw.get("rarely_running_plants"), SCALES["rarely_running_plants"]),
        "price_below_20": raw.get("share_below_20"),
        "price_below_0": raw.get("share_below_0"),
        "flaring": _cap(raw.get("flared_average_mw"), SCALES["flared_average_mw"]),
        "curtailment": _cap(raw.get("curtailed_average_mw"), SCALES["curtailed_average_mw"]),
        "exporter": None if exp is None else (1.0 if exp else 0.0),
        "queue": _cap(raw.get("queued_mw"), SCALES["queued_mw"]),
    }


def score(raw: dict, weights: dict[str, float] | None = None) -> dict:
    """{"score", "coverage", "parts": {name: {value, normalized, weight, contribution}}}. score = sum over the parts
    with data of weight x normalized value. A missing part adds nothing and is not re-weighted onto the others, so
    a county known only by its price cannot outrank one known on every count; `coverage` is the share of the total
    absolute weight that had data."""
    w = weights_of(weights)
    n = normalized(raw)
    value_of = {"idle": "idle_mw", "retiring": "retiring_mw", "rarely_running": "rarely_running_plants",
                "price_below_20": "share_below_20", "price_below_0": "share_below_0",
                "flaring": "flared_average_mw", "curtailment": "curtailed_average_mw", "exporter": "net_exporter",
                "queue": "queued_mw"}
    parts, total, have, all_w = {}, 0.0, 0.0, 0.0
    for k, wk in w.items():
        x = n[k]
        c = None if x is None else wk * x
        parts[k] = {"value": raw.get(value_of[k]), "normalized": None if x is None else round(x, 4), "weight": wk,
                    "contribution": None if c is None else round(c, 4)}
        all_w += abs(wk)
        if c is not None:
            total += c
            have += abs(wk)
    return {"score": round(total, 4), "coverage": round(have / all_w, 4) if all_w else None, "parts": parts}


# --------------------------------------------------------------------------- node -> county

def plant_keys(name: str | None) -> tuple[set[str], set[str]]:
    """(valid keys, refused keys) of a plant name."""
    words = re.findall(r"[A-Z0-9]+", (name or "").upper())
    ok, bad = _split_keys({"".join(words), "".join(w for w in words if w not in GENERIC)})
    # the first word is never a key on its own, but a node keyed on it is reported as refused, so the hit rate's
    # misses are visible ("Central Energy Plant" -> CENTRAL)
    if words:
        bad |= _split_keys({words[0]})[1]
    return ok, bad


def node_keys(node: str, substation: str | None) -> tuple[set[str], set[str]]:
    cands = {norm(substation)} if substation else set()
    head = node.split("_", 1)[0]
    if head:
        cands.add(norm(head))
    return _split_keys(cands)


def _split_keys(cands: set[str]) -> tuple[set[str], set[str]]:
    ok, bad = set(), set()
    for k in cands:
        if not k:
            continue
        (bad if len(k) < MIN_KEY or k in GENERIC else ok).add(k)
    return ok, bad


def match_nodes() -> dict:
    """The rule above over every ERCOT resource node in power_nodes and every Texas ERCOT plant in
    power_generators. -> {"matches": {node: {...}}, "refused": [...], "stats": {..., "hit_rate"}}."""
    from sqlalchemy import text

    with _session()() as s:
        nodes = s.execute(text("""SELECT id, node, node_type, zone, substation FROM power_nodes WHERE iso = 'ERCOT'
                                  ORDER BY node""")).all()
        plants = s.execute(text("""
            SELECT plant_id, min(plant_name) AS plant_name, min(county) AS county
            FROM power_generators
            WHERE state = 'TX' AND county IS NOT NULL AND (ba_code IS NULL OR upper(ba_code) = 'ERCO')
            GROUP BY plant_id""")).all()
    index: dict[str, list] = {}
    refused_keys: set[str] = set()
    for p in plants:
        ok, bad = plant_keys(p.plant_name)
        for k in ok:
            index.setdefault(k, []).append(p)
        refused_keys |= bad
    resource = [n for n in nodes if n.node_type in RESOURCE_TYPES or n.substation]
    matches, refused = {}, []
    for n in resource:
        ok, bad = node_keys(n.node, n.substation)
        hits = {p.plant_id: p for k in ok for p in index.get(k, [])}
        if not hits:
            near = sorted(bad & refused_keys)
            if near:
                refused.append({"node": n.node, "reason": "key too short or generic", "keys": near})
            continue
        counties = {site_reads.county_key(p.county) for p in hits.values()}
        if len(counties) > 1:
            refused.append({"node": n.node, "reason": "plants in more than one county",
                            "counties": sorted({p.county for p in hits.values()}),
                            "plant_ids": sorted(hits)})
            continue
        ps = sorted(hits.values(), key=lambda p: p.plant_id)
        matches[n.node] = {"node": n.node, "node_id": n.id, "node_type": n.node_type, "zone": n.zone,
                           "substation": n.substation, "state": "TX", "county": ps[0].county,
                           "county_key": counties.pop(), "plant_ids": [p.plant_id for p in ps],
                           "plant_names": [p.plant_name for p in ps],
                           "key": sorted(ok & {k for p in ps for k in plant_keys(p.plant_name)[0]})[0]}
    stats = {"resource_nodes": len(resource), "matched": len(matches),
             "refused_ambiguous": sum(1 for r in refused if r["reason"].startswith("plants")),
             "refused_short_or_generic": sum(1 for r in refused if r["reason"].startswith("key")),
             "hit_rate": round(len(matches) / len(resource), 4) if resource else None,
             "texas_ercot_plants": len(plants), "enabled": NODE_MATCH}
    return {"matches": matches if NODE_MATCH else {}, "refused": refused, "stats": stats}


# --------------------------------------------------------------------------- prices

def _node_shares(node_ids: list[int], market: str, window_days: int, until: date | None) -> dict[int, dict]:
    from sqlalchemy import text

    if not node_ids:
        return {}
    with _session()() as s:
        last = until or s.execute(text("SELECT max(day) FROM power_node_days WHERE market = :m AND node_id = ANY(:i)"),
                                  {"m": market, "i": node_ids}).scalar()
        if last is None:
            return {}
        first = last - timedelta(days=window_days - 1)
        rows = s.execute(text("""
            SELECT node_id, sum(n) AS n, sum(n_neg) AS n_neg, sum(n_lt20) AS n_lt20, count(*) AS days,
                   sum(mean * n) / nullif(sum(n), 0) AS mean, min(day) AS first_day, max(day) AS last_day
            FROM power_node_days WHERE market = :m AND node_id = ANY(:i) AND day BETWEEN :a AND :b
            GROUP BY node_id HAVING sum(n) > 0"""), {"m": market, "i": node_ids, "a": first, "b": last}).all()
    return {r.node_id: {"market": market, "intervals": int(r.n), "days": r.days,
                        "share_below_20": round(r.n_lt20 / r.n, 4), "share_below_0": round(r.n_neg / r.n, 4),
                        "mean": round(float(r.mean), 2), "first_day": r.first_day, "last_day": r.last_day,
                        "window": {"first": first, "last": last, "days": window_days}} for r in rows}


def _series_shares(key: str, window_days: int, until: date | None) -> dict | None:
    from sqlalchemy import text

    with _session()() as s:
        sid, last = s.execute(text("""SELECT p.id, max(d.day) FROM power_series p LEFT JOIN power_days d
                                      ON d.series_id = p.id WHERE p.key = :k GROUP BY p.id"""), {"k": key}).first() \
            or (None, None)
        if sid is None:
            return None
        last = until or last
        if last is None:
            return None
        first = last - timedelta(days=window_days - 1)
        r = s.execute(text("""
            SELECT count(v) AS n, count(*) FILTER (WHERE v < 0) AS n_neg, count(*) FILTER (WHERE v < :cheap) AS n_lt20,
                   count(DISTINCT d.day) AS days, avg(v) AS mean, min(d.day) AS first_day, max(d.day) AS last_day
            FROM power_days d, unnest(d.vals) AS u(v)
            WHERE d.series_id = :sid AND d.day BETWEEN :a AND :b AND v IS NOT NULL"""),
            {"sid": sid, "a": first, "b": last, "cheap": CHEAP}).one()
    if not r.n:
        return None
    return {"intervals": int(r.n), "days": r.days, "share_below_20": round(r.n_lt20 / r.n, 4),
            "share_below_0": round(r.n_neg / r.n, 4), "mean": round(float(r.mean), 2), "first_day": r.first_day,
            "last_day": r.last_day, "window": {"first": first, "last": last, "days": window_days}}


class Prices:
    """Price lookups for one read, cached: many counties share a hub."""

    def __init__(self, window_days: int, until: date | None):
        from sqlalchemy import text

        self.window_days, self.until, self._cache = window_days, until, {}
        with _session()() as s:
            self.series = {(r.iso, norm(r.location), r.market): r.key for r in s.execute(text(
                "SELECT key, iso, location, market FROM power_series WHERE kind = 'lmp'"))}
            self.nodes = {(r.iso, r.node): r.id for r in s.execute(text("SELECT id, iso, node FROM power_nodes"))}

    def location(self, iso: str, location: str) -> dict | None:
        """The real-time series of (iso, location), else its nodal real-time summaries, else the day-ahead ones."""
        ck = (iso, location)
        if ck in self._cache:
            return self._cache[ck]
        out = None
        for market in ("rt", "da"):
            key = self.series.get((iso, norm(location), market))
            if key:
                got = _series_shares(key, self.window_days, self.until)
                if got:
                    out = {"market": market, "series": key, **got}
                    break
            nid = self.nodes.get((iso, location))
            if nid:
                got = _node_shares([nid], market, self.window_days, self.until).get(nid)
                if got:
                    out = {"series": None, "node": location, **got}
                    break
        self._cache[ck] = out
        return out

    def nodes_best(self, matched: list[dict]) -> tuple[dict, dict] | None:
        """The matched node with the most intervals below $20 (real-time, else day-ahead), and its shares."""
        ids = {m["node_id"]: m for m in matched}
        for market in ("rt", "da"):
            got = _node_shares(list(ids), market, self.window_days, self.until)
            if got:
                nid = max(got, key=lambda i: (got[i]["share_below_20"], got[i]["share_below_0"], -got[i]["mean"],
                                              ids[i]["node"]))
                return ids[nid], {**got[nid], "candidates": [
                    {"node": ids[i]["node"], "share_below_20": g["share_below_20"], "intervals": g["intervals"]}
                    for i, g in sorted(got.items(), key=lambda x: ids[x[0]]["node"])]}
        return None


def hub_for(iso: str | None, state: str | None, county_key: str | None) -> tuple[str | None, str]:
    """(price location, how it was chosen) for a county without a node: the ISO's documented area for the place."""
    if iso == "ERCOT":
        return geo.REFERENCE_HUB["ERCOT"], "market reference hub (no node or zone tied to the county)"
    if iso == "SPP":
        return geo.REFERENCE_HUB["SPP"], "market reference hub (SPP's hubs are not tied to counties)"
    if iso == "CAISO":
        h = CAISO_COUNTY_HUB.get(county_key or "")
        if h:
            return h, "trading hub of the county (CAISO_COUNTY_HUB)"
        return None, "CAISO has no reference hub and this county is not in CAISO_COUNTY_HUB"
    if iso == "MISO" and state in MISO_STATE_HUB:
        return MISO_STATE_HUB[state], "the state's MISO hub"
    if iso in geo.REFERENCE_HUB:
        h = geo.REFERENCE_HUB[iso]
        if h:
            return h, "market reference hub (state-level, power/geo.py)"
        return None, f"{iso} has no single reference hub (power/geo.py)"
    return None, "not in a wholesale market OpenGrid prices"


def price_part(prices: Prices, iso: str | None, state: str | None, ckey: str | None, ba: str | None,
               matched: list[dict]) -> dict:
    """The best price location that can honestly be tied to the county, with its grain."""
    if iso == "ERCOT" and matched:
        best = prices.nodes_best(matched)
        if best:
            m, got = best
            return {"grain": "node", "iso": iso, "location": m["node"],
                    "match": f"node at substation {m['substation']} matched to plant(s) "
                             f"{', '.join(m['plant_names'])} by key {m['key']}", **got}
        zones = sorted({m["zone"] for m in matched if m.get("zone")})
        if len(zones) == 1:
            got = prices.location(iso, zones[0])
            if got:
                return {"grain": "zone", "iso": iso, "location": zones[0],
                        "match": "load zone of the county's matched node(s), which have no prices in the window",
                        **got}
    loc, how = hub_for(iso, state, ckey)
    if loc:
        got = prices.location(iso, loc)
        if got:
            return {"grain": "hub", "iso": iso, "location": loc, "match": how, **got}
        how = f"{how}; no prices stored for {loc} in the window"
    if iso is None and ba:
        how = f"balancing authority {ba} runs no wholesale market OpenGrid prices"
    return {"grain": "none", "iso": iso, "location": loc, "match": how, "share_below_20": None,
            "share_below_0": None}


# --------------------------------------------------------------------------- waste, competition, grid

def curtailment_part(iso: str | None, window_days: int, until: date | None, cache: dict) -> dict | None:
    """The ISO's wind + solar curtailment over the window (regional: the whole ISO), or None where none is read."""
    from sqlalchemy import text

    from power import waste

    if iso not in CURTAILMENT_ISOS:
        return None
    if iso in cache:
        return cache[iso]
    with _session()() as s:
        last = until or s.execute(text("""SELECT max(d.day) FROM power_days d JOIN power_series p ON p.id = d.series_id
                                          WHERE p.kind = 'curtailment' AND upper(p.iso) = :iso"""),
                                  {"iso": iso}).scalar()
    out = None
    if last is not None:
        first = last - timedelta(days=window_days - 1)
        rows = [r for r in waste.curtailment(first, last, iso) if not r["scope"] and not r["type"]
                and r["curtailed_mwh"] is not None]
        days = {r["day"] for r in rows}
        if rows:
            mwh = sum(r["curtailed_mwh"] for r in rows)
            measures = sorted({r["measure"] for r in rows})
            out = {"grain": "regional", "region": f"{iso} (whole ISO)", "curtailed_mwh": round(mwh, 1),
                   "days": len(days), "average_mw": round(mwh / (len(days) * 24), 1),
                   "by_fuel": {f: round(sum(r["curtailed_mwh"] for r in rows if r["fuel"] == f), 1)
                               for f in sorted({r["fuel"] for r in rows})},
                   "measure": measures, "estimate": any(m != "curtailment" for m in measures),
                   "window": {"first": first, "last": last, "days": window_days}}
    cache[iso] = out
    return out


def exporter_part(ba: str | None, cache: dict) -> dict | None:
    """EIA-930 net generation minus demand of the BA, hour by hour where both are reported, over the last 12 months
    to the latest day stored. None where the BA's series are not stored."""
    from sqlalchemy import text

    if not ba:
        return None
    if ba in cache:
        return cache[ba]
    b = ba.lower()
    with _session()() as s:
        ids = dict(s.execute(text("SELECT key, id FROM power_series WHERE key IN (:g, :d)"),
                             {"g": f"eia.actual.net_generation.{b}", "d": f"eia.actual.demand.{b}"}).all())
        out = None
        if len(ids) == 2:
            gid, did = ids[f"eia.actual.net_generation.{b}"], ids[f"eia.actual.demand.{b}"]
            last = s.execute(text("""SELECT max(g.day) FROM power_days g JOIN power_days d ON d.day = g.day
                                     WHERE g.series_id = :g AND d.series_id = :d"""), {"g": gid, "d": did}).scalar()
            if last is not None:
                first = last - timedelta(days=EXPORT_DAYS - 1)
                r = s.execute(text("""
                    SELECT count(*) AS hours, count(DISTINCT g.day) AS days, sum(x.ng - x.dm) AS net,
                           sum(x.ng) AS ng, sum(x.dm) AS dm
                    FROM power_days g JOIN power_days d ON d.day = g.day, unnest(g.vals, d.vals) AS x(ng, dm)
                    WHERE g.series_id = :g AND d.series_id = :d AND g.day BETWEEN :a AND :b
                      AND x.ng IS NOT NULL AND x.dm IS NOT NULL"""), {"g": gid, "d": did, "a": first, "b": last}).one()
                if r.hours:
                    out = {"ba": ba, "net_exporter": float(r.net) > 0, "net_mwh": round(float(r.net), 1),
                           "net_generation_mwh": round(float(r.ng), 1), "demand_mwh": round(float(r.dm), 1),
                           "hours": r.hours, "days": r.days,
                           "window": {"first": first, "last": last, "days": EXPORT_DAYS}}
    cache[ba] = out
    return out


def _queues(state: str | None) -> dict[tuple[str, str], list[dict]]:
    out: dict[tuple[str, str], list[dict]] = {}
    for t in site_reads.queued_mw_by_county(state=state, kind="generation"):
        ck = site_reads.county_key(t["county"])
        if t["state"] and ck:
            out.setdefault((t["state"], ck), []).append(t)
    return out


def competition_part(rows: list[dict] | None, iso: str | None, loads_known: bool,
                     loaded: dict | None = None) -> dict:
    """Queued generation MW (active requests) from ONE source: ERCOT's GIS report for an ERCOT county (monthly,
    newer), else Queued Up. The two are never added: Queued Up already includes ERCOT's queue. A county with no
    active request is 0 MW only when Queued Up (which covers every US queue) is loaded; else unknown (None)."""
    rows = rows or []
    pick = None
    if iso == "ERCOT":
        pick = next((r for r in rows if r["source"] == "ercot_gis"), None)
    pick = pick or next((r for r in rows if r["source"] == "lbnl_queues"), None) or (rows[0] if rows else None)
    if pick is None and (loaded or {}).get("lbnl_queues"):
        return {"queued_mw": 0.0, "source": "lbnl_queues", "edition": loaded["lbnl_queues"], "projects": 0,
                "by_type": {}, "by_source": [], "note": "no active generation request in the county",
                "large_loads_mw": None, "large_loads": None if loads_known else LARGE_LOADS_NOTE}
    return {"queued_mw": pick["mw"] if pick else None, "source": pick["source"] if pick else None,
            "edition": pick["edition"] if pick else None, "projects": pick["projects"] if pick else None,
            "by_type": pick["by_type"] if pick else None,
            "by_source": [{"source": r["source"], "edition": r["edition"], "mw": r["mw"], "projects": r["projects"]}
                          for r in rows],
            "large_loads_mw": None, "large_loads": None if loads_known else LARGE_LOADS_NOTE}


def _flaring_by_county(state: str | None) -> dict[tuple[str, str], dict]:
    """Regulator flaring per county for each source's latest year (North Dakota today), summed over the months
    of that year that are loaded."""
    from sqlalchemy import text

    with _session()() as s:
        rows = s.execute(text("""
            WITH latest AS (SELECT source, extract(year FROM max(period))::int AS year FROM power_flare_areas
                            WHERE period_type = 'month' AND area_type = 'county' GROUP BY source)
            SELECT a.source, a.state, a.area, l.year, sum(a.volume_mcf) AS mcf,
                   sum(a.energy_mwh_derived) AS mwh, count(DISTINCT a.period) AS months
            FROM power_flare_areas a JOIN latest l ON l.source = a.source
            WHERE a.period_type = 'month' AND a.area_type = 'county'
              AND extract(year FROM a.period)::int = l.year
              AND (CAST(:st AS text) IS NULL OR upper(a.state) = upper(:st))
            GROUP BY 1, 2, 3, 4"""), {"st": state}).all()
    out = {}
    for r in rows:
        ck = site_reads.county_key(r.area)
        if not ck or r.mwh is None:
            continue
        out[(r.state.upper(), ck)] = {
            "source": r.source, "county": r.area, "year": r.year, "months": r.months,
            "volume_mcf": round(float(r.mcf or 0), 1), "energy_mwh_derived": round(float(r.mwh), 1),
            "average_mw_derived": round(float(r.mwh) / (r.months * HOURS_PER_MONTH), 2),
            "note": "derived: MCF x 0.1037 MWh (methodology/power-waste.md); latest year, months loaded only"}
    return out


def _retiring(as_of: date, state: str | None = None, plant_ids: list[int] | None = None) -> list:
    from sqlalchemy import text

    a, b = _ym(as_of), _ym(_add_months(as_of, RETIRING_MONTHS))
    with _session()() as s:
        return s.execute(text(f"""
            SELECT state, county, plant_id, sum(nameplate_mw) AS mw, min(planned_retirement_ym) AS first_ym,
                   max(planned_retirement_ym) AS last_ym
            FROM power_generators
            WHERE status_group = ANY(:op) AND planned_retirement_ym BETWEEN :a AND :b
              AND (CAST(:st AS text) IS NULL OR state = :st)
              {"AND plant_id = ANY(:ids)" if plant_ids is not None else ""}
            GROUP BY state, county, plant_id"""),
            {"op": list(site_reads.OPERABLE), "a": a, "b": b, "st": state and state.upper(),
             **({"ids": plant_ids} if plant_ids is not None else {})}).all()


def spare_part(plants: list[dict], retiring_rows: list, as_of: date) -> dict:
    """Idle MW (latest complete EIA-923 year), MW retiring within RETIRING_MONTHS, and large rarely-running
    plants. idle_mw is None when no plant has a capacity factor (no EIA-923 row), never 0."""
    with_cf = [p for p in plants if p["idle_mw"] is not None]
    rare = [p for p in plants if p["capacity_factor"] is not None and p["nonstorage_mw"] >= LARGE_PLANT_MW
            and p["capacity_factor"] < RARELY_CF]
    ret_mw = sum(r.mw or 0.0 for r in retiring_rows)
    years = sorted({p["year"] for p in with_cf if p["year"]})
    return {"plants": len(plants), "operable_mw": round(sum(p["nameplate_mw"] for p in plants), 1),
            "idle_mw": round(sum(p["idle_mw"] for p in with_cf), 1) if with_cf else None,
            "plants_with_capacity_factor": len(with_cf), "capacity_factor_year": years[-1] if years else None,
            "retiring_mw": round(ret_mw, 1) if plants or retiring_rows else None,
            "retiring_window": {"from": _ym(as_of), "to": _ym(_add_months(as_of, RETIRING_MONTHS))},
            "retiring_first_ym": min((r.first_ym for r in retiring_rows), default=None),
            "retiring_last_ym": max((r.last_ym for r in retiring_rows), default=None),
            "rarely_running_plants": len(rare) if with_cf else None,
            "rarely_running": [{"plant_id": p["plant_id"], "plant_name": p["plant_name"],
                                "nonstorage_mw": p["nonstorage_mw"], "capacity_factor": p["capacity_factor"]}
                               for p in sorted(rare, key=lambda p: -p["nonstorage_mw"])],
            "top_plants": [{"plant_id": p["plant_id"], "plant_name": p["plant_name"], "idle_mw": p["idle_mw"],
                            "capacity_factor": p["capacity_factor"], "nameplate_mw": p["nameplate_mw"],
                            **({"distance_km": p["distance_km"]} if "distance_km" in p else {})}
                           for p in sorted(plants, key=lambda p: -(p["idle_mw"] or 0))[:5]]}


def _ba_of(plants: list[dict]) -> str | None:
    """The balancing authority holding the most operable nameplate among the plants."""
    mw: dict[str, float] = {}
    for p in plants:
        if p.get("ba_code"):
            mw[p["ba_code"].upper()] = mw.get(p["ba_code"].upper(), 0.0) + (p["nameplate_mw"] or 0.0)
    return max(sorted(mw), key=lambda b: mw[b]) if mw else None


def _raw(spare: dict, price: dict, flared: dict | None, curt: dict | None, comp: dict, grid: dict | None) -> dict:
    return {"idle_mw": spare.get("idle_mw"), "retiring_mw": spare.get("retiring_mw"),
            "rarely_running_plants": spare.get("rarely_running_plants"),
            "share_below_20": price.get("share_below_20"), "share_below_0": price.get("share_below_0"),
            "flared_average_mw": flared["average_mw_derived"] if flared else None,
            "curtailed_average_mw": curt["average_mw"] if curt else None,
            "net_exporter": grid["net_exporter"] if grid else None, "queued_mw": comp.get("queued_mw")}


# --------------------------------------------------------------------------- the sentence

def _pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def why(row: dict) -> str:
    """One plain sentence: where the county ranks, the parts that lifted it most, what held it back, what is
    missing."""
    sc, sp, pr, wa, co, gr = (row["score"], row["spare"], row["price"], row["waste"], row["competition"],
                              row["grid"])
    phr = {
        "idle": lambda: f"{sp['idle_mw']:,.0f} idle MW across {sp['plants_with_capacity_factor']} plant(s) "
                        f"({sp['capacity_factor_year']})",
        "retiring": lambda: f"{sp['retiring_mw']:,.0f} MW planned to retire by {sp['retiring_last_ym']}",
        "rarely_running": lambda: f"{sp['rarely_running_plants']} large plant(s) running under "
                                  f"{RARELY_CF:.0%} of the year",
        "price_below_20": lambda: f"power below $20/MWh in {_pct(pr['share_below_20'])} of {pr['market']} "
                                  f"intervals at {pr['location']} ({pr['grain']} grain)",
        "price_below_0": lambda: f"below $0 in {_pct(pr['share_below_0'])} of them",
        "flaring": lambda: f"{wa['flared']['average_mw_derived']:,.1f} MW of gas flared on average "
                           f"({wa['flared']['year']}, {wa['flared']['months']} month(s), derived)",
        "curtailment": lambda: f"{wa['curtailment']['average_mw']:,.0f} MW curtailed on average in "
                               f"{wa['curtailment']['region']} (regional)",
        "exporter": lambda: f"{gr['ba']} is a net {'exporter' if gr['net_exporter'] else 'importer'} over "
                            f"{gr['days']} day(s)",
        "queue": lambda: f"{co['queued_mw']:,.0f} MW of generation queued ({co['source']})",
    }
    parts = sc["parts"]
    up = sorted((k for k, p in parts.items() if (p["contribution"] or 0) > 0),
                key=lambda k: -parts[k]["contribution"])[:3]
    down = [k for k, p in parts.items() if (p["contribution"] or 0) < 0]
    missing = [k for k, p in parts.items() if p["contribution"] is None]
    name = f"{row['county']}, {row['state']}"
    s = (f"{name} ranks {row['rank']} of {row['of']} (score {sc['score']:.3f}; {_pct(sc['coverage'])} of the "
         f"weight has data)")
    s += ": " + ("; ".join(phr[k]() for k in up) if up else "no part scored above zero")
    if down:
        s += "; held back by " + "; ".join(phr[k]() for k in down)
    if missing:
        s += "; missing: " + ", ".join(missing).replace("_", " ")
    if pr["grain"] == "none":
        s += f" (no price: {pr['match']})"
    return s + "."


# --------------------------------------------------------------------------- the reads

def ranking(state: str | None = None, iso: str | None = None, min_idle_mw: float | None = None,
            window_days: int = DEFAULT_WINDOW_DAYS, weights: dict[str, float] | None = None,
            limit: int | None = 100, until: date | None = None, as_of: date | None = None,
            year: int | None = None) -> dict:
    """{"rows": [...], "meta": {...}}: every county with any signal (a plant, an active queue request or North
    Dakota flaring), scored and ranked; rows filtered by iso and min_idle_mw after scoring, so a rank is the
    county's place among all counties in the state filter."""
    w = weights_of(weights)
    window_days = _window(window_days)
    as_of = as_of or date.today()
    st = state.upper() if state else None
    want_iso = iso.upper() if iso else None

    plants_by: dict[tuple[str, str], list[dict]] = {}
    names: dict[tuple[str, str], str] = {}
    no_county = 0
    for p in site_reads.plants(state=st, sort="nameplate", limit=None, year=year):
        ck = site_reads.county_key(p["county"])
        if not p["state"] or not ck or (st and p["state"] != st):
            no_county += 1
            continue
        if not (p["nameplate_mw"] or p["planned_mw"] or p["retired_mw"]):
            continue
        plants_by.setdefault((p["state"], ck), []).append(p)
        names.setdefault((p["state"], ck), p["county"])
    retiring: dict[tuple[str, str], list] = {}
    for r in _retiring(as_of, st):
        ck = site_reads.county_key(r.county)
        if r.state and ck:
            retiring.setdefault((r.state, ck), []).append(r)
    queues = _queues(st)
    loads_known = bool(site_reads.queued_mw_by_county(state=st, kind="load"))
    loaded = site_reads.latest_editions()
    flared = _flaring_by_county(st)
    # a state a regulator source covers: a county of it with no row flared nothing (0), elsewhere flaring is unknown
    flare_states = {k[0]: f for k, f in flared.items()}
    for k, rows in queues.items():
        names.setdefault(k, rows[0]["county"])
    for k, f in flared.items():
        names.setdefault(k, f["county"])

    nm = match_nodes() if (st in (None, "TX")) else {"matches": {}, "refused": [], "stats": None}
    matched_by: dict[tuple[str, str], list[dict]] = {}
    for m in nm["matches"].values():
        matched_by.setdefault((m["state"], m["county_key"]), []).append(m)

    prices = Prices(window_days, until)
    curt_cache: dict = {}
    grid_cache: dict = {}
    rows = []
    for key in sorted(set(plants_by) | set(queues) | set(flared)):
        stt, ck = key
        plants = [p for p in plants_by.get(key, []) if p["nameplate_mw"]]
        ba = _ba_of(plants)
        if ba:
            r_iso, ba_basis = ISO_OF_BA.get(ba), "operable nameplate of the county's plants"
        else:
            # no operable plant: the state's market, only where all of the state's load sits in one ISO (queue rows
            # carry Queued Up regions such as "West", not markets, so they are not used)
            r_iso = _market_area(stt)
            ba = geo.BA.get(r_iso) if r_iso else None
            ba_basis = "the state's market area (power/geo.py): no operable plant in the county" if r_iso else None
        spare = spare_part(plants, retiring.get(key, []), as_of) if plants or retiring.get(key) else \
            {"plants": 0, "idle_mw": None, "retiring_mw": None, "rarely_running_plants": None,
             "note": "no operable plant in the inventory for this county"}
        price = price_part(prices, r_iso, stt, ck, ba, matched_by.get(key, []) if r_iso == "ERCOT" else [])
        curt = curtailment_part(r_iso, window_days, until, curt_cache)
        grid = exporter_part(ba, grid_cache)
        comp = competition_part(queues.get(key), r_iso, loads_known, loaded)
        fl = flared.get(key)
        if fl is None and stt in flare_states:
            f0 = flare_states[stt]
            fl = {"source": f0["source"], "county": names.get(key), "year": f0["year"], "months": f0["months"],
                  "volume_mcf": 0.0, "energy_mwh_derived": 0.0, "average_mw_derived": 0.0,
                  "note": f"no flaring reported for this county by {f0['source']} in {f0['year']}"}
        sc = score(_raw(spare, price, fl, curt, comp, grid), w)
        rows.append({"state": stt, "county": names.get(key) or ck.title(), "county_key": ck, "iso": r_iso,
                     "ba": ba, "ba_basis": ba_basis, "spare": spare, "price": price,
                     "waste": {"flared": fl, "curtailment": curt,
                               "flaring_note": None if fl else "no county-level flaring data for this state "
                                                               "(North Dakota only)"},
                     "competition": comp, "grid": grid, "score": sc})
    rows.sort(key=lambda r: (-r["score"]["score"], -r["score"]["coverage"], r["state"], r["county_key"]))
    for i, r in enumerate(rows, 1):
        r["rank"], r["of"] = i, len(rows)
        r["why"] = why(r)
    out = [r for r in rows if (not want_iso or (r["iso"] or "") == want_iso)
           and (min_idle_mw is None or (r["spare"].get("idle_mw") is not None
                                        and r["spare"]["idle_mw"] >= min_idle_mw))]
    grains: dict[str, int] = {}
    for r in out:
        grains[r["price"]["grain"]] = grains.get(r["price"]["grain"], 0) + 1
    return {"rows": out[:limit] if limit else out,
            "meta": {"counties": len(rows), "matching": len(out), "weights": w, "scales": SCALES,
                     "window_days": window_days, "until": until, "as_of_day": as_of,
                     "retiring_months": RETIRING_MONTHS,
                     "rarely_running": {"min_nonstorage_mw": LARGE_PLANT_MW, "max_capacity_factor": RARELY_CF},
                     "price_grains": grains, "node_match": nm["stats"], "plants_without_county": no_county,
                     "large_loads": None if loads_known else LARGE_LOADS_NOTE, "note": NOTE}}


def near(lat: float, lon: float, km: float = 50.0, window_days: int = DEFAULT_WINDOW_DAYS,
         weights: dict[str, float] | None = None, until: date | None = None, as_of: date | None = None,
         year: int | None = None) -> dict:
    """The same parts for the area within km of a point: plants near it, flaring near it (every source, never
    summed across sources), the nearest priced location and the county's queue. The county is the county of the
    nearest plant (no county boundaries are held), and is labelled so."""
    w = weights_of(weights)
    window_days = _window(window_days)
    as_of = as_of or date.today()
    plants = [p for p in site_reads.plants_near(lat, lon, km, year=year) if p["nameplate_mw"]]
    nearest = plants[0] if plants else None
    stt = nearest["state"] if nearest else None
    ck = site_reads.county_key(nearest["county"]) if nearest else None
    ba = _ba_of(plants)
    r_iso = ISO_OF_BA.get(ba) if ba else None
    retiring = _retiring(as_of, None, [p["plant_id"] for p in plants]) if plants else []
    spare = spare_part(plants, retiring, as_of) if plants else \
        {"plants": 0, "idle_mw": None, "retiring_mw": None, "rarely_running_plants": None,
         "note": f"no operable plant within {km} km"}

    from power import waste

    fl_near = waste.flared_near(lat, lon, km)
    fl = None
    # one source only, regulator first: a North Dakota section and a satellite cell can be the same flares
    for src in ("ndic", "worldbank_ggfd"):
        b = fl_near["by_source"].get(src)
        if b:
            months = b.get("months_reported") or 12
            fl = {"source": src, "year": b["year"], "months": months, "sites": b["sites"],
                  "volume_bcm": b["volume_bcm"], "energy_mwh_derived": b["energy_mwh_derived"],
                  "average_mw_derived": round(b["energy_mwh_derived"] / (months * HOURS_PER_MONTH), 2),
                  "note": "derived; sources never summed (the same flare can be in two)"}
            break

    prices = Prices(window_days, until)
    price = None
    if r_iso == "ERCOT":
        nm = match_nodes()
        by_plant = {pid: m for m in nm["matches"].values() for pid in m["plant_ids"]}
        tied = [by_plant[p["plant_id"]] for p in plants if p["plant_id"] in by_plant]
        if tied:
            price = price_part(prices, r_iso, stt, ck, ba, tied)
            if price["grain"] in ("node", "zone"):
                price["match"] += " (nodes of plants within the radius)"
    if price is None:
        price = price_part(prices, r_iso, stt, ck, ba, [])
    queues = _queues(stt) if stt else {}
    loads_known = bool(site_reads.queued_mw_by_county(state=stt, kind="load")) if stt else False
    comp = competition_part(queues.get((stt, ck)) if ck else None, r_iso, loads_known,
                            site_reads.latest_editions() if ck else None)
    curt = curtailment_part(r_iso, window_days, until, {})
    grid = exporter_part(ba, {})
    sc = score(_raw(spare, price, fl, curt, comp, grid), w)
    return {"lat": lat, "lon": lon, "km": km, "iso": r_iso, "ba": ba,
            "county": ({"state": stt, "county": nearest["county"], "basis":
                        f"county of the nearest plant ({nearest['plant_name']}, {nearest['distance_km']} km)"}
                       if nearest else None),
            "spare": spare, "price": price,
            "waste": {"flared": fl, "flared_near": fl_near, "curtailment": curt},
            "competition": comp, "grid": grid, "score": sc,
            "meta": {"weights": w, "scales": SCALES, "window_days": window_days, "as_of_day": as_of, "note": NOTE}}
