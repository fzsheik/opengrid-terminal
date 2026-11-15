"""Who runs the machines: operators per marketplace and candidate matches across marketplaces. INTERNAL.

Two marketplaces publish, for each machine, a key of whoever operates it:
    lium   `provider_key` (else `miner_hotkey`) on every machine with a free GPU   (providers/lium.py OPERATOR_FIELDS)
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
# Vast: the price search is read every 5 minutes; the keyed census every 1 to 12 hours.
VAST_LISTING_WINDOW = timedelta(minutes=30)
VAST_CENSUS_WINDOW = timedelta(hours=26)

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
    """One row per Vast machine. `listing_payloads`: stored price-search payloads (lists of offers).
    `census`: (gpu_name, rentable, payload) of stored census halves. A machine seen in both keeps the price search's
    row (it has RAM and a price); the census adds the machines the price search's 64 cheapest offers leave out."""
    by_id: dict = {}
    for payload in listing_payloads:
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
            # The same machine in several offers: the largest GPU count, the cheapest price per GPU.
            if old is not None:
                row["gpus"] = max(x for x in (old["gpus"], row["gpus"], 0) if x is not None) or None
                prices = [x for x in (old["price_per_gpu_hour"], row["price_per_gpu_hour"]) if x is not None]
                row["price_per_gpu_hour"] = min(prices) if prices else None
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

def _newest_per_request(session, provider: str, endpoint: str, since: datetime, census: bool):
    """ids of the newest ok snapshot per distinct request, without loading any payload."""
    from sqlalchemy import select

    from tables import RawSnapshot

    rows = session.execute(
        select(RawSnapshot.id, RawSnapshot.request, RawSnapshot.fetched_at)
        .where(RawSnapshot.provider == provider, RawSnapshot.endpoint == endpoint, RawSnapshot.ok.is_(True),
               RawSnapshot.fetched_at >= since)
        .order_by(RawSnapshot.fetched_at.desc(), RawSnapshot.id.desc())).all()
    picked: dict = {}
    for id_, request, at in rows:
        req = request if isinstance(request, dict) else {}
        if bool(req.get("census")) != census:
            continue
        key = (req.get("gpu_name"), req.get("rentable"))
        picked.setdefault(key, (id_, req, at))
    return picked


def _payloads(session, ids: list[int]) -> dict:
    from sqlalchemy import select

    from tables import RawSnapshot

    if not ids:
        return {}
    return {r.id: r.payload for r in session.execute(select(RawSnapshot).where(RawSnapshot.id.in_(ids))).scalars()}


def load(now: datetime | None = None) -> dict:
    """Read the newest stored Lium executors and Vast price-search / census payloads; return machines per
    marketplace and when each was read. Read-only."""
    import normalize
    from providers import lium, vast

    now = now or datetime.now(timezone.utc)
    with normalize.SessionLocal() as s:
        raw = normalize._latest_raw(s, "lium")
        ex = [r for ep, rows in raw.items() if ep == lium.ENDPOINT for r in rows]
        lium_at = ex[0].fetched_at if ex else None
        lm = lium_machines(ex[0].payload) if ex else []
        listing = _newest_per_request(s, "vast", vast.ENDPOINT, now - VAST_LISTING_WINDOW, census=False)
        census = _newest_per_request(s, "vast", vast.ENDPOINT, now - VAST_CENSUS_WINDOW, census=True)
        bodies = _payloads(s, [v[0] for v in listing.values()] + [v[0] for v in census.values()])
    vm = vast_machines([bodies.get(i) for i, _, _ in listing.values()],
                       [(req.get("gpu_name"), req.get("rentable"), bodies.get(i)) for i, req, _ in census.values()])
    return {
        "machines": {"lium": lm, "vast": vm},
        "as_of": {"lium": lium_at,
                  "vast": max((at for _, _, at in listing.values()), default=None),
                  "vast_census": max((at for _, _, at in census.values()), default=None)},
        "coverage": {
            "lium": "machines with a free GPU (the whole-fleet feed carries no operator key or CPU model)",
            "vast": "the 64 cheapest offers per GPU model, plus every machine of the keyed census when it has run "
                    "in the last 26 hours (census rows have no RAM or price, so they match on the build tier only)"},
    }
