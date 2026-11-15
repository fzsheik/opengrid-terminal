"""Who runs the machines: operators per marketplace and candidate matches across marketplaces. INTERNAL.

Two marketplaces publish, for each machine, a key of whoever operates it:
    lium   `provider_key` (else `miner_hotkey`) on every machine with a free GPU   (providers/lium.py OPERATOR_FIELDS);
           fully rented machines come from the per-provider machine lists (lium_data provider_executors)
    vast   `host_id` on every offer of the price search and, with a key, of the whole-fleet census
Both are kept in the stored raw copies only. This module reads those copies and computes, on read:

    operators(machines)   per marketplace: machines, GPUs, GPU models, countries and price range per operator, and how
                          concentrated supply is (largest operator's share of GPUs, top five, HHI)
    overlap(a, b)         machines of two marketplaces with the same hardware fingerprint: candidates for the same
                          machine, or the same operator's build, listed in two places; and the price each asks

A fingerprint is not an identity. Two tiers, both stated with every match:
    strong   same GPU model, GPU count, CPU model and country, and RAM within 3%
    build    same GPU model, CPU model and country (any GPU count, RAM not compared)
`fingerprint_unique` says how many machines of each marketplace have a strong fingerprint no other machine there
shares; a match on a common build is weak evidence, and the read says so rather than hiding it.

The daily series (store/operators.py): snapshot_day() writes one row per marketplace and day, and one for the
Lium x Vast matches, as the market stood at 12:00 UTC; `python -m operators backfill --since YYYY-MM-DD` rebuilds
past days from the stored raw copies. Each row says what that day's stored data covered, because days read from
different coverage are not comparable.

Everything here is INTERNAL (methodology/internal/operators.md): operator keys identify a business or a person, and neither
marketplace licenses them for republishing. Nothing is written; no listing, index, statistic or public read imports
this module (tests/test_operators.py checks). Admin read: /v1/ops/operators (api/operators.py).
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal

MARKETPLACES = ("lium", "vast")
RAM_TOLERANCE = 0.03
KIB_PER_GIB = 1024 * 1024
# Vast: the price search is read every 5 minutes. Offers come and go between rounds (and before 2026-11-15 a round
# held only the 64 cheapest per model), so every round of the last three hours is read (a machine keeps its newest
# price).
# The keyed census runs every 1 to 12 hours.
VAST_LISTING_WINDOW = timedelta(hours=3)
VAST_CENSUS_WINDOW = timedelta(hours=26)
# Lium's rentable list is read every 15 minutes; an as-of read ignores a copy older than this.
LIUM_MAX_AGE = timedelta(hours=6)

_GPU_NOISE = re.compile(r"\b(?:NVIDIA|GEFORCE|TESLA)\b|\s+")
_CPU_NOISE = re.compile(r"\((?:R|TM)\)")


def gpu_key(name: str | None) -> str:
    """One spelling per GPU model across marketplaces: 'NVIDIA GeForce RTX 5090' and 'RTX 5090' -> 'RTX5090'."""
    return _GPU_NOISE.sub("", (name or "").upper())


def cpu_key(name: str | None) -> str:
    return re.sub(r"\s+", " ", _CPU_NOISE.sub("", (name or "").upper())).strip()


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool) else None


def _machine(marketplace: str, machine_id, operator, gpu_name, gpus, cpu, cores, ram_gb, country, price, free) -> dict:
    return {"marketplace": marketplace, "machine_id": str(machine_id), "operator": None if operator is None else str(operator),
            "gpu": gpu_key(gpu_name), "gpu_name": gpu_name, "gpus": gpus, "cpu": cpu_key(cpu) or None, "cpu_cores": cores,
            "ram_gb": None if not ram_gb else round(ram_gb, 1), "country": (country or None) and str(country).upper(),
            "price_per_gpu_hour": price, "free": free}


# --------------------------------------------------------------------------- stored raw -> machines (pure)

def lium_machines(executors) -> list[dict]:
    """One row per machine of a stored /api/executors payload (machines with a free GPU)."""
    out = []
    for e in executors if isinstance(executors, list) else []:
        if not isinstance(e, dict) or e.get("id") is None:
            continue
        gpus = e.get("gpu_count")
        ram = _num(e.get("ram_total"))
        out.append(_machine("lium", e["id"], e.get("provider_key") or e.get("miner_hotkey"),
                            e.get("gpu_class") or e.get("machine_name"), gpus if isinstance(gpus, int) else None,
                            e.get("cpu_model"), e.get("cpu_count"), None if ram is None else ram / KIB_PER_GIB,
                            e.get("country_code"), _num(e.get("price_per_gpu")),
                            bool(e.get("available_gpu_count")) if e.get("available_gpu_count") is not None else None))
    return out


def lium_provider_machines(hotkey: str, machines) -> list[dict]:
    """One row per machine of a stored per-provider machine list (lium_data provider_executors, slimmed): every
    machine the provider runs, fully rented ones included. The operator is the provider's miner hotkey."""
    out = []
    for e in machines if isinstance(machines, list) else []:
        if not isinstance(e, dict) or e.get("id") is None:
            continue
        gpus, ram, free = e.get("gpu_count"), _num(e.get("ram_total")), e.get("available_gpu_count")
        out.append(_machine("lium", e["id"], hotkey, e.get("machine_name"), gpus if isinstance(gpus, int) else None,
                            e.get("cpu_model"), e.get("cpu_count"), None if ram is None else ram / KIB_PER_GIB,
                            e.get("country_code"), _num(e.get("price_per_gpu")), None if free is None else bool(free)))
    return out


def merge_lium(listed: list[dict], per_provider: list[dict]) -> list[dict]:
    """The Lium fleet: machines of the rentable list (they carry the operator's own key, which for a pool is finer
    than the miner hotkey) plus every machine only the per-provider lists show, which are the fully rented ones."""
    have = {m["machine_id"] for m in listed}
    return listed + [m for m in per_provider if m["machine_id"] not in have]


def _vast_total_gpus(o: dict) -> int | None:
    n, frac = o.get("num_gpus"), o.get("gpu_frac")
    if isinstance(n, int) and n > 0 and isinstance(frac, (int, float)) and frac > 0:
        return round(n / frac)  # an offer may be a slice of the machine
    return None


def _vast_country(o: dict) -> str | None:
    geo = o.get("geolocation")
    tail = geo.split(",")[-1].strip() if isinstance(geo, str) and geo.strip() else None
    return tail if tail and len(tail) == 2 else None


def vast_machines(listing_payloads, census=()) -> list[dict]:
    """One row per Vast machine. `listing_payloads`: stored price-search payloads (lists of offers), OLDEST FIRST:
    a machine seen in several keeps the newest one's price (within one payload, the cheapest of its offers).
    `census`: (gpu_name, rentable, payload) of stored census halves. A machine seen in both keeps the price search's
    row (it has RAM and a price); the census adds the machines the price search's 64 cheapest offers leave out."""
    by_id: dict = {}
    seen_in: dict = {}
    for index, payload in enumerate(listing_payloads):
        for o in payload if isinstance(payload, list) else []:
            if not isinstance(o, dict) or o.get("machine_id") is None:
                continue
            total = _vast_total_gpus(o)
            eff, cores, ram_mb = _num(o.get("cpu_cores_effective")), _num(o.get("cpu_cores")), _num(o.get("cpu_ram"))
            # cpu_ram is the offer's slice of RAM; scale by the slice's share of the cores for the machine's total.
            ram = None if ram_mb is None else ram_mb / 1024 * (cores / eff if cores and eff else 1)
            n, price = o.get("num_gpus"), _num(o.get("dph_total"))
            per_gpu = None if price is None or not isinstance(n, int) or n < 1 else price / n
            row = _machine("vast", o["machine_id"], o.get("host_id"), o.get("gpu_name"), total, o.get("cpu_name"),
                           o.get("cpu_cores"), ram, _vast_country(o), per_gpu, True)
            old = by_id.get(row["machine_id"])
            # The same machine in several offers: the largest GPU count; the cheapest price per GPU within one
            # payload, and a newer payload's price replaces an older one's.
            if old is not None:
                row["gpus"] = max(x for x in (old["gpus"], row["gpus"], 0) if x is not None) or None
                if seen_in[row["machine_id"]] == index or row["price_per_gpu_hour"] is None:
                    prices = [x for x in (old["price_per_gpu_hour"], row["price_per_gpu_hour"]) if x is not None]
                    row["price_per_gpu_hour"] = min(prices) if prices else None
            seen_in[row["machine_id"]] = index
            by_id[row["machine_id"]] = row
    for gpu_name, rentable, payload in census:
        for o in payload if isinstance(payload, list) else []:
            if not isinstance(o, dict) or o.get("machine_id") is None:
                continue
            mid = str(o["machine_id"])
            if mid in by_id:
                if by_id[mid]["operator"] is None and o.get("host_id") is not None:
                    by_id[mid]["operator"] = str(o["host_id"])
                continue
            by_id[mid] = _machine("vast", mid, o.get("host_id"), gpu_name, _vast_total_gpus(o), o.get("cpu_name"),
                                  o.get("cpu_cores"), None, _vast_country(o), None, bool(rentable))
    return list(by_id.values())


# --------------------------------------------------------------------------- operators of one marketplace (pure)

def operators(machines: list[dict]) -> dict:
    """Per operator: machines, GPUs, models, countries, price range. Plus the concentration of GPUs among the
    operators that are identified. A machine without an operator key is counted, never guessed."""
    groups: dict[str, list[dict]] = defaultdict(list)
    unidentified = 0
    for m in machines:
        if m["operator"] is None:
            unidentified += 1
        else:
            groups[m["operator"]].append(m)
    rows = []
    for op, ms in groups.items():
        models: Counter = Counter()
        for m in ms:
            models[m["gpu_name"] or "?"] += m["gpus"] or 0
        prices = [m["price_per_gpu_hour"] for m in ms if m["price_per_gpu_hour"] is not None]
        rows.append({"operator": op, "machines": len(ms), "gpus": sum(m["gpus"] or 0 for m in ms),
                     "gpu_models": dict(models.most_common()),
                     "countries": sorted({m["country"] for m in ms if m["country"]}),
                     "min_price_per_gpu_hour": min(prices) if prices else None,
                     "max_price_per_gpu_hour": max(prices) if prices else None})
    rows.sort(key=lambda r: (-r["gpus"], -r["machines"], r["operator"]))
    total = sum(r["gpus"] for r in rows)
    shares = [r["gpus"] / total for r in rows] if total else []
    for r, s in zip(rows, shares):
        r["gpu_share"] = round(s, 4)
    return {"machines": len(machines), "identified_machines": len(machines) - unidentified,
            "unidentified_machines": unidentified, "operators": len(rows), "gpus": total,
            "top1_gpu_share": round(shares[0], 4) if shares else None,
            "top5_gpu_share": round(sum(shares[:5]), 4) if shares else None,
            # Herfindahl-Hirschman index on GPU shares, 0-10,000 (10,000 = one operator has everything).
            "hhi": round(sum(s * s for s in shares) * 10000) if shares else None,
            "rows": rows}


# --------------------------------------------------------------------------- across two marketplaces (pure)

def _build_fp(m: dict):
    return (m["gpu"], m["cpu"], m["country"]) if m["gpu"] and m["cpu"] and m["country"] else None


def _strong_bin(m: dict):
    """For the uniqueness count only: the build, the GPU count and RAM in 8 GB bins."""
    b = _build_fp(m)
    if b is None or not m["gpus"] or not m["ram_gb"]:
        return None
    return (*b, m["gpus"], round(m["ram_gb"] / 8))


def _ram_close(a: dict, b: dict) -> bool:
    x, y = a["ram_gb"], b["ram_gb"]
    return bool(x and y) and abs(x - y) / max(x, y) <= RAM_TOLERANCE


def fingerprint_unique(machines: list[dict]) -> dict:
    """How many machines carry a strong fingerprint nobody else in the same marketplace shares."""
    bins = Counter(b for b in map(_strong_bin, machines) if b is not None)
    comparable = sum(bins.values())
    return {"comparable_machines": comparable, "unique": sum(1 for n in bins.values() if n == 1)}


def overlap(a: list[dict], b: list[dict]) -> dict:
    """Candidate matches between two marketplaces' machines, by hardware fingerprint (see the module docstring)."""
    by_build: dict = defaultdict(list)
    for m in b:
        fp = _build_fp(m)
        if fp is not None:
            by_build[fp].append(m)
    matches = []
    for x in a:
        for y in by_build.get(_build_fp(x), ()):
            strong = bool(x["gpus"]) and x["gpus"] == y["gpus"] and _ram_close(x, y)
            px, py = x["price_per_gpu_hour"], y["price_per_gpu_hour"]
            matches.append({
                "tier": "strong" if strong else "build", "gpu": x["gpu_name"], "cpu": x["cpu"], "country": x["country"],
                "a": {k: x[k] for k in ("marketplace", "machine_id", "operator", "gpus", "ram_gb", "price_per_gpu_hour")},
                "b": {k: y[k] for k in ("marketplace", "machine_id", "operator", "gpus", "ram_gb", "price_per_gpu_hour")},
                # b's ask over a's, in percent: positive means b is dearer for what may be the same hardware.
                "price_gap_pct": None if not px or py is None else round((py - px) / px * 100, 1),
            })
    matches.sort(key=lambda m: (m["tier"] != "strong", m["gpu"] or "", m["a"]["machine_id"], m["b"]["machine_id"]))
    pairs: dict = defaultdict(lambda: {"strong": 0, "build": 0})
    for m in matches:
        if m["a"]["operator"] is not None and m["b"]["operator"] is not None:
            pairs[(m["a"]["operator"], m["b"]["operator"])][m["tier"]] += 1
    operator_pairs = [{"operator_a": oa, "operator_b": ob, **n} for (oa, ob), n in pairs.items()]
    operator_pairs.sort(key=lambda p: (-p["strong"], -p["build"], p["operator_a"], p["operator_b"]))
    strong = [m for m in matches if m["tier"] == "strong"]
    return {
        "strong_matches": len(strong), "build_matches": len(matches) - len(strong),
        "machines_a_matched": len({m["a"]["machine_id"] for m in matches}),
        "machines_b_matched": len({m["b"]["machine_id"] for m in matches}),
        "machines_a_strong": len({m["a"]["machine_id"] for m in strong}),
        "machines_b_strong": len({m["b"]["machine_id"] for m in strong}),
        "fingerprint_unique": {"a": fingerprint_unique(a), "b": fingerprint_unique(b)},
        "operator_pairs": operator_pairs, "matches": matches,
    }


# --------------------------------------------------------------------------- database read (no writes)

def _newest_per_request(session, provider: str, endpoint: str, since: datetime, census: bool, until: datetime | None = None):
    """ids of the newest ok snapshot per distinct request in [since, until], without loading any payload."""
    from sqlalchemy import select

    from tables import RawSnapshot

    where = [RawSnapshot.provider == provider, RawSnapshot.endpoint == endpoint, RawSnapshot.ok.is_(True),
             RawSnapshot.fetched_at >= since]
    if until is not None:
        where.append(RawSnapshot.fetched_at <= until)
    rows = session.execute(select(RawSnapshot.id, RawSnapshot.request, RawSnapshot.fetched_at).where(*where)
                           .order_by(RawSnapshot.fetched_at.desc(), RawSnapshot.id.desc())).all()
    picked: dict = {}
    for id_, request, at in rows:
        req = request if isinstance(request, dict) else {}
        if bool(req.get("census")) != census:
            continue
        key = (req.get("gpu_name"), req.get("rentable"))
        picked.setdefault(key, (id_, req, at))
    return picked


def _listing_rounds(session, provider: str, endpoint: str, since: datetime, until: datetime | None = None):
    """(fetched_at, payload, request) of every ok price-search snapshot in [since, until], oldest first; each
    distinct body once."""
    from sqlalchemy import select

    from tables import RawSnapshot

    where = [RawSnapshot.provider == provider, RawSnapshot.endpoint == endpoint, RawSnapshot.ok.is_(True),
             RawSnapshot.fetched_at >= since]
    if until is not None:
        where.append(RawSnapshot.fetched_at <= until)
    rows = session.execute(select(RawSnapshot.id, RawSnapshot.request, RawSnapshot.fetched_at, RawSnapshot.sha256)
                           .where(*where).order_by(RawSnapshot.fetched_at.desc(), RawSnapshot.id.desc())).all()
    keep, hashes = [], set()
    for id_, request, at, sha in rows:  # newest first: an unchanged body is read once, at its newest time
        req = request if isinstance(request, dict) else {}
        if req.get("census"):
            continue
        if sha is not None and sha in hashes:
            continue
        hashes.add(sha)
        keep.append((id_, at, req))
    bodies = _payloads(session, [i for i, _, _ in keep])
    return [(at, bodies.get(i), req) for i, at, req in reversed(keep)]


def _payloads(session, ids: list[int]) -> dict:
    from sqlalchemy import select

    from tables import RawSnapshot

    out = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        out.update({r.id: r.payload for r in session.execute(select(RawSnapshot).where(RawSnapshot.id.in_(chunk))).scalars()})
    return out


# What a day's stored data covered (operator_daily.coverage). Days with different coverage are not comparable:
# a marketplace read from the 64 cheapest offers per model looks smaller and more concentrated than it is.
COVERAGE_VAST_CAPPED = "cheapest_64_per_model"            # before 2026-11-15
COVERAGE_VAST_BOOK = "whole_book"
COVERAGE_VAST_FLEET = "whole_book_and_census"             # the keyed census carries host ids: the whole fleet
COVERAGE_LIUM_FREE = "machines_with_a_free_gpu"
COVERAGE_LIUM_FLEET = "whole_fleet"                       # with the per-provider machine lists
COVERAGE_NO_KEYS = "no_operator_keys"                     # stored copies of that day carry no operator key


def load(now: datetime | None = None) -> dict:
    """Read the stored Lium executors, Lium per-provider machine lists and Vast price-search / census payloads as
    they stood at `now` (default: the present); return machines per marketplace, when each was read and what the
    data covered. Read-only. A past `now` rebuilds that moment from the stored raw copies."""
    import normalize
    from lium_data import reads as lium_reads
    from providers import lium, vast

    present = now is None
    now = now or datetime.now(timezone.utc)
    with normalize.SessionLocal() as s:
        raw = normalize._latest_raw(s, "lium", None if present else now)
        ex = [r for ep, rows in raw.items() if ep == lium.ENDPOINT for r in rows]
        lium_at = ex[0].fetched_at if ex else None
        if lium_at is not None and now - lium_at > LIUM_MAX_AGE:
            ex, lium_at = [], None     # a copy that old says nothing about this moment
        lm = lium_machines(ex[0].payload) if ex else []
        listed = len(lm)
        rounds = _listing_rounds(s, "vast", vast.ENDPOINT, now - VAST_LISTING_WINDOW, now)
        census = _newest_per_request(s, "vast", vast.ENDPOINT, now - VAST_CENSUS_WINDOW, census=True, until=now)
        bodies = _payloads(s, [v[0] for v in census.values()])
    lm = merge_lium(lm, [m for hk, body in lium_reads.provider_machines(as_of=None if present else now)
                         for m in lium_provider_machines(hk, body)])
    census_rows = [(req.get("gpu_name"), req.get("rentable"), bodies.get(i)) for i, req, _ in census.values()]
    vm = vast_machines([payload for _, payload, _ in rounds], census_rows)
    whole_book = any(req.get("book") == "full" for _, _, req in rounds)
    census_hosts = any(isinstance(o, dict) and o.get("host_id") is not None
                       for _, _, body in census_rows for o in (body if isinstance(body, list) else [])[:1])
    vast_cov = (COVERAGE_VAST_FLEET if whole_book and census_hosts else COVERAGE_VAST_BOOK if whole_book
                else COVERAGE_VAST_CAPPED)
    lium_cov = COVERAGE_LIUM_FLEET if len(lm) > listed else COVERAGE_LIUM_FREE
    return {
        "machines": {"lium": lm, "vast": vm},
        "as_of": {"lium": lium_at,
                  "vast": max((at for at, _, _ in rounds), default=None),
                  "vast_census": max((at for _, _, at in census.values()), default=None)},
        "coverage_key": {"lium": lium_cov, "vast": vast_cov},
        "coverage": {
            "lium": f"{listed} machines with a free GPU, plus {len(lm) - listed} more (fully rented ones) from the "
                    "per-provider machine lists read in the last 6 hours",
            "vast": "every machine offered in the last 3 hours (each model's whole book), plus every machine "
                    "of the keyed census when it has run in the last 26 hours (census rows have no RAM or price, "
                    "so they match on the build tier only)"},
    }


# --------------------------------------------------------------------------- the daily series

DAY_HOUR_UTC = 12   # a day's row is the market as it stood at 12:00 UTC that day


def day_moment(day) -> datetime:
    return datetime(day.year, day.month, day.day, DAY_HOUR_UTC, tzinfo=timezone.utc)


def snapshot_day(day, now: datetime | None = None) -> dict:
    """Build `day`'s rows from the stored raw copies as they stood at 12:00 UTC that day and write them
    (operator_daily, operator_overlap_daily), replacing the day's rows. A marketplace with no stored data at that
    moment gets no row; a day whose moment has not come yet is skipped. Idempotent."""
    import statistics

    import normalize
    from sqlalchemy import delete

    from store.operators import OperatorDaily, OperatorOverlapDaily

    now = now or datetime.now(timezone.utc)
    moment = day_moment(day)
    if moment > now:
        return {"day": str(day), "skipped": "the day's 12:00 UTC has not come yet"}
    model = load(moment)
    rows, summaries = [], {}
    for name in MARKETPLACES:
        machines = model["machines"][name]
        if not machines:
            continue
        s = operators(machines)
        summaries[name] = s
        as_of = max((t for k, t in model["as_of"].items() if k.startswith(name) and t is not None), default=moment)
        coverage = model["coverage_key"][name] if s["identified_machines"] else COVERAGE_NO_KEYS
        rows.append(OperatorDaily(
            marketplace=name, day=day, as_of=as_of, machines=s["machines"],
            identified_machines=s["identified_machines"], operators=s["operators"], gpus=s["gpus"],
            top1_gpu_share=s["top1_gpu_share"], top5_gpu_share=s["top5_gpu_share"], hhi=s["hhi"], coverage=coverage,
            computed_at=now))
    overlap_row = None
    a, b = model["machines"]["lium"], model["machines"]["vast"]
    if a and b:
        ov = overlap(a, b)
        gaps = [m["price_gap_pct"] for m in ov["matches"] if m["tier"] == "strong" and m["price_gap_pct"] is not None]
        comp_a = ov["fingerprint_unique"]["a"]["comparable_machines"]
        comp_b = ov["fingerprint_unique"]["b"]["comparable_machines"]
        overlap_row = OperatorOverlapDaily(
            day=day, marketplace_a="lium", marketplace_b="vast", comparable_a=comp_a, comparable_b=comp_b,
            strong_matches=ov["strong_matches"], build_matches=ov["build_matches"],
            machines_a_matched=ov["machines_a_matched"], machines_b_matched=ov["machines_b_matched"],
            operator_pairs=len(ov["operator_pairs"]),
            median_strong_gap_pct=None if not gaps else round(statistics.median(gaps), 2),
            # Without a CPU model on one side nothing can match: say so rather than store a zero that reads as "none".
            note=None if comp_a and comp_b else "one side's stored copies carry no CPU model or RAM: not comparable",
            computed_at=now)
    with normalize.SessionLocal.begin() as s:
        s.execute(delete(OperatorDaily).where(OperatorDaily.day == day))
        s.execute(delete(OperatorOverlapDaily).where(OperatorOverlapDaily.day == day))
        s.add_all(rows)
        if overlap_row is not None:
            s.add(overlap_row)
    return {"day": str(day), "marketplaces": {n: {"machines": x["machines"], "operators": x["operators"]}
                                              for n, x in summaries.items()},
            "overlap": None if overlap_row is None else {"strong": overlap_row.strong_matches,
                                                         "build": overlap_row.build_matches}}


def run_daily(now: datetime | None = None) -> dict:
    """The `operators_daily` job (hourly): write today's row once 12:00 UTC has passed, and yesterday's if a
    restart skipped it. A day already written is left alone."""
    import normalize
    from sqlalchemy import select

    from store.operators import OperatorDaily

    now = now or datetime.now(timezone.utc)
    today = now.date()
    done = []
    for day in (today - timedelta(days=1), today):
        if day_moment(day) > now:
            continue
        with normalize.SessionLocal() as s:
            have = s.execute(select(OperatorDaily.day).where(OperatorDaily.day == day).limit(1)).first()
        if have is None:
            done.append(snapshot_day(day, now))
    return {"written": done}


def backfill(since, until=None, now: datetime | None = None) -> list[dict]:
    """Rebuild every day from `since` to `until` (default: today), oldest first, from the stored raw copies."""
    now = now or datetime.now(timezone.utc)
    until = until or now.date()
    out, day = [], since
    while day <= until:
        out.append(snapshot_day(day, now))
        day += timedelta(days=1)
    return out


def history(days: int = 90) -> dict:
    """The stored daily series, newest first."""
    import normalize
    from sqlalchemy import select

    from store.operators import OperatorDaily, OperatorOverlapDaily

    since = datetime.now(timezone.utc).date() - timedelta(days=days)
    with normalize.SessionLocal() as s:
        daily = s.execute(select(OperatorDaily).where(OperatorDaily.day >= since)
                          .order_by(OperatorDaily.day.desc(), OperatorDaily.marketplace)).scalars().all()
        over = s.execute(select(OperatorOverlapDaily).where(OperatorOverlapDaily.day >= since)
                         .order_by(OperatorOverlapDaily.day.desc())).scalars().all()
    dcols = ("day", "marketplace", "machines", "identified_machines", "operators", "gpus", "top1_gpu_share",
             "top5_gpu_share", "hhi", "coverage", "as_of")
    ocols = ("day", "marketplace_a", "marketplace_b", "comparable_a", "comparable_b", "strong_matches",
             "build_matches", "machines_a_matched", "machines_b_matched", "operator_pairs", "median_strong_gap_pct",
             "note")
    return {"daily": [{c: getattr(r, c) for c in dcols} for r in daily],
            "overlap": [{c: getattr(r, c) for c in ocols} for r in over]}


_registered = False


def register() -> list[str]:
    """Register the hourly `operators_daily` job (api/operators.py calls this on import). Needs the poller."""
    global _registered
    from config import settings
    from jobs import job

    if _registered or not settings.poller_enabled:
        return []
    _registered = True
    job("operators_daily", every_seconds=3600, initial_delay_seconds=300)(run_daily)
    return ["operators_daily"]


def main(argv=None) -> int:
    """python -m operators backfill --since YYYY-MM-DD [--until YYYY-MM-DD]"""
    import argparse
    import json
    from datetime import date

    ap = argparse.ArgumentParser(prog="operators")
    sub_ = ap.add_subparsers(dest="cmd", required=True)
    b = sub_.add_parser("backfill", help="rebuild the daily operator series from the stored raw copies")
    b.add_argument("--since", required=True, type=date.fromisoformat)
    b.add_argument("--until", type=date.fromisoformat)
    args = ap.parse_args(argv)
    for r in backfill(args.since, args.until):
        print(json.dumps(r, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
