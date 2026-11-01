"""The full cost of a reference job on every matching listing: compute + egress + storage, unknowns named.

    estimate(job, listings, components, ctx) -> {"rows", "unranked", "notes"}   pure
    listing_specs(listing, ctx)               -> spec fields (form factor, GPU link, fabric, billing, deployable)
    load(job)                                 -> reads current listings, stored cost sources and provider raw

Kind: estimated (methodology/full-cost.md). List prices only; nothing here is a quote, and nothing here feeds
listings, indices, spreads, percentiles or routing.

Formula, per listing:
    compute  = billed_hours x price per instance-hour
               billed_hours = ceil(hours to the provider's billing increment), at least its minimum
    egress   = sum over the provider's monthly tiers of the job's egress GB in each tier (free tiers price 0)
    storage  = stored GB x price per GB-month x hours / 730, priced with the first known class of storage_class
    total    = compute + egress + storage
A component the job uses (egress > 0, storage > 0) that the provider does not publish, or publishes only in a value
OpenGrid could not verify, is UNKNOWN: it is listed in `unknown`, the total is a lower bound (`total_is_lower_bound`),
and it is never counted as $0.

Which listings: the GPU asked for, exactly `count` GPUs per machine, on demand, not interruptible, not a "from"
price, in stock or stock unknown, seen within the provider's staleness window. Vast's "median" row is not a machine
anyone can rent, so Vast is priced from its "cheapest" row (one real offer, with that offer's own storage and
transfer prices). One row per provider: its cheapest listing by compute. Rows are ranked by total, complete totals
first, then lower bounds (a lower bound could be dearer than it shows, so it never outranks a complete total). A
listing whose node fabric is below
`min_fabric_gbps`, or that is not deployable on demand, is returned in `unranked` with the reason.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

import hardware
from cost import components as cc
from cost import manual

STORAGE_CLASSES = ("shared_fs", "block_storage", "object_storage")
STORAGE_ORDER = {"auto": STORAGE_CLASSES, "shared_fs": ("shared_fs",), "block_storage": ("block_storage",),
                 "object_storage": ("object_storage",)}
_C = Decimal("0.01")
_Q6 = Decimal("0.000001")

# Reference jobs (methodology/full-cost.md). GB are decimal: 10 TB = 10,000 GB.
PRESETS: dict[str, dict] = {
    "R0": {"label": "Inference box: 1 GPU for a month", "gpu": "NVIDIA H100 80GB SXM5", "count": 1, "hours": 730,
           "egress_gb": 1000, "storage_gb": 500},
    "R1": {"label": "Training run: 8x H100 for 72 h, 10 TB out, 20 TB stored", "gpu": "NVIDIA H100 80GB SXM5",
           "count": 8, "hours": 72, "egress_gb": 10000, "storage_gb": 20000},
    "R2": {"label": "Egress-heavy: 8x H100 for 72 h, 50 TB out", "gpu": "NVIDIA H100 80GB SXM5", "count": 8,
           "hours": 72, "egress_gb": 50000, "storage_gb": 20000},
    "R3": {"label": "Short benchmark: 1 GPU for 10 minutes", "gpu": "NVIDIA H100 80GB SXM5", "count": 1,
           "hours": 1 / 6, "egress_gb": 0, "storage_gb": 0},
}


def _money(v: Decimal | None) -> float | None:
    return None if v is None else float(v.quantize(_C, rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- spec fields

def _billing(provider: str, sku: str | None) -> dict:
    b = manual.BILLING.get(provider)
    if not b:
        return {"increment_s": None, "minimum_s": None, "status": None, "source": None, "notes": None}
    out = {k: b.get(k) for k in ("increment_s", "minimum_s", "status", "source", "notes")}
    for prefix, over in (b.get("products") or {}).items():
        if sku and sku.startswith(prefix):
            out.update(over)
    if out["status"] != "checked":
        out["increment_s"] = out["minimum_s"] = None
    return out


def _fabric(listing: dict) -> dict | None:
    sku, lid = listing.get("sku") or "", listing.get("listing_id") or ""
    for f in manual.FABRIC:
        if f["provider"] != listing["provider"]:
            continue
        if f["sku"] and not (sku.startswith(f["sku"]) or lid.startswith(f["sku"])):
            continue
        if f.get("match") and f["match"] not in lid and f["match"] not in sku:
            continue
        return f
    return None


def _gpu_link(hw: dict | None, count: int) -> str | None:
    if not hw or count < 2:
        return None
    ff = (hw.get("form_factor") or "").lower()
    if ff in ("sxm", "superchip"):
        return "nvlink"
    if ff == "oam":
        return "infinity_fabric"
    if ff == "pcie":
        return "nvlink_bridge" if hw.get("nvlink") else "pcie"
    return None


def listing_specs(listing: dict, ctx: dict | None = None) -> dict:
    ctx = ctx or {}
    hw = hardware.spec(listing.get("canonical_gpu_name") or "")
    count = int(listing.get("gpu_count") or 1)
    fab = _fabric(listing)
    bill = _billing(listing["provider"], listing.get("sku"))
    deployable, included_egress = None, None
    if listing["provider"] == "vultr":
        plan = (ctx.get("vultr_plans") or {}).get(listing.get("sku"))
        if plan:
            deployable = plan["deploy_ondemand"]
            included_egress = plan["bandwidth_gb"]
    if fab and fab.get("on_demand_deployable") is not None:
        deployable = fab["on_demand_deployable"]
    return {
        "gpu_memory_gb": hw.get("vram_gb") if hw else None,
        "gpu_form_factor": ((hw.get("form_factor") or "").lower() or None) if hw else None,
        "gpu_link": _gpu_link(hw, count),
        "gpu_link_detail": hw.get("nvlink") if hw and count >= 2 else None,
        "node_fabric": fab.get("node_fabric") if fab else None,
        "node_fabric_gbps": fab.get("node_fabric_gbps") if fab else None,
        "frontend_gbps": fab.get("frontend_gbps") if fab else None,
        "multi_node": fab.get("multi_node") if fab else None,
        "fabric_source": fab.get("source") if fab else None,
        "fabric_notes": fab.get("notes") if fab else None,
        "on_demand_deployable": deployable,
        "included_egress_gb_month": None if included_egress is None else float(included_egress),
        "billing_increment_s": bill["increment_s"],
        "billing_minimum_s": bill["minimum_s"],
        "billing_source": bill["source"],
        "billing_notes": bill["notes"],
        "spec_verified_at": manual.V,
    }


# --------------------------------------------------------------------------- arithmetic

def billed_hours(hours: Decimal, increment_s: int | None, minimum_s: int | None) -> tuple[Decimal, bool]:
    """(hours billed, exact). Unknown increment: the job's own hours, flagged not exact unless that is whole hours
    of at least one hour (every published increment divides an hour)."""
    secs = hours * 3600
    if increment_s is None:
        exact = secs % 3600 == 0 and secs >= 3600
        return hours, exact
    billed = Decimal(math.ceil(secs / increment_s)) * increment_s
    if minimum_s:
        billed = max(billed, Decimal(minimum_s))
    return billed / 3600, True


def tiered(amount_gb: Decimal, tiers: list[dict], allowance_gb: Decimal = Decimal(0)) -> Decimal:
    """Cost of amount_gb over [start, end) tiers. `allowance_gb` (a plan's included transfer) comes off first."""
    q = max(Decimal(0), amount_gb - allowance_gb)
    total = Decimal(0)
    for t in tiers:
        lo, hi = t["start_gb"], t["end_gb"]
        if q <= lo:
            break
        span = (q if hi is None else min(q, hi)) - lo
        total += span * t["price"]
    return total


# --------------------------------------------------------------------------- estimate

def _eligible(listing: dict, gpu: str, count: int) -> bool:
    if listing.get("canonical_gpu_name") != gpu or int(listing.get("gpu_count") or 0) != count:
        return False
    if listing.get("market_type") != "on_demand" or listing.get("interruptible"):
        return False
    if (listing.get("provider_tier") or "") in ("from_price", "gpu-attachment"):
        return False
    if listing["provider"] == "vast" and listing.get("provider_tier") == "median":
        return False  # not a machine anyone can rent; the "cheapest" row is a real offer
    return listing.get("available") is not False


def _instance_price(listing: dict) -> Decimal | None:
    p = listing.get("price_per_instance_hour")
    if p is not None:
        return Decimal(str(p))
    g = listing.get("price_per_gpu_hour")
    return None if g is None else Decimal(str(g)) * int(listing.get("gpu_count") or 1)


def estimate_listing(listing: dict, job: dict, components: list[dict], ctx: dict | None = None) -> dict:
    hours = Decimal(str(job["hours"]))
    egress_gb = Decimal(str(job.get("egress_gb") or 0))
    storage_gb = Decimal(str(job.get("storage_gb") or 0))
    order = STORAGE_ORDER[job.get("storage_class") or "auto"]
    provider, lid = listing["provider"], listing.get("listing_id")
    specs = listing_specs(listing, ctx)
    unknown, parts, notes = [], {}, []

    price = _instance_price(listing)
    bh, exact = billed_hours(hours, specs["billing_increment_s"], specs["billing_minimum_s"])
    compute = None if price is None else bh * price
    if price is None:
        unknown.append("compute")
    if not exact:
        notes.append("billing increment not published: compute assumes per-second billing")
    parts["compute"] = {"amount": _money(compute), "billed_hours": float(bh.quantize(_Q6)), "exact": exact,
                        "price_per_instance_hour": None if price is None else float(price)}

    # Egress
    eg = None
    if egress_gb > 0:
        comp = cc.lookup(components, provider, "egress_internet", lid)
        if comp is None:
            unknown.append("egress_internet")
            shown = cc.lookup(components, provider, "egress_internet", lid, usable_only=False)
            if shown:
                notes.append(f"egress price {shown['status']} ({shown['source']}): not counted")
            parts["egress_internet"] = {"amount": None, "known": False}
        else:
            allowance = Decimal(str(specs["included_egress_gb_month"] or 0))
            eg = tiered(egress_gb, comp["tiers"], allowance)
            parts["egress_internet"] = {"amount": _money(eg), "known": True, "product": comp["product"],
                                        "origin": comp["origin"], "source": comp["source"],
                                        "as_of": cc.to_json(comp)["as_of"], "allowance_scope": comp["allowance_scope"],
                                        "included_gb": float(allowance) if allowance else None}
    # Storage
    st = None
    if storage_gb > 0:
        comp = next((c for c in (cc.lookup(components, provider, k, lid) for k in order) if c), None)
        if comp is None:
            unknown.append("storage")
            parts["storage"] = {"amount": None, "known": False, "classes_tried": list(order)}
        else:
            per_month = tiered(storage_gb, comp["tiers"])
            st = per_month * hours / cc.HOURS_PER_MONTH
            parts["storage"] = {"amount": _money(st), "known": True, "class": comp["component"],
                                "product": comp["product"], "origin": comp["origin"], "source": comp["source"],
                                "as_of": cc.to_json(comp)["as_of"], "billed_when_stopped": comp["billed_when_stopped"]}

    region = listing.get("region")
    priced_at = sorted({c["region"] for c in (cc.lookup(components, provider, k, lid) for k in ("egress_internet", *order))
                        if c and c["region"]} - {region})
    if region and priced_at:
        notes.append(f"egress and storage are priced at {', '.join(priced_at)} rates; this listing is in {region}")

    known = [v for v in (compute, eg, st) if v is not None]
    total = sum(known, Decimal(0)) if compute is not None else None
    gpus = int(listing.get("gpu_count") or 1)
    eff = None if total is None else total / (gpus * hours)
    extras = None if compute in (None, 0) or total is None else (total - compute) / compute * 100
    return {
        "provider": provider, "listing_id": lid, "sku": listing.get("sku"), "region": listing.get("region"),
        "gpu": listing.get("canonical_gpu_name"), "gpu_count": gpus,
        "total": _money(total), "total_is_lower_bound": bool(unknown), "unknown": unknown,
        "effective_per_gpu_hour": None if eff is None else float(eff.quantize(_Q6)),
        "extras_vs_compute_pct": None if extras is None else float(extras.quantize(Decimal("0.1"))),
        "components": parts, "specs": specs, "notes": notes,
        "listing_observed_at": listing.get("observed_at").isoformat()
        if isinstance(listing.get("observed_at"), datetime) else listing.get("observed_at"),
    }


def estimate(job: dict, listings: list[dict], components: list[dict], ctx: dict | None = None) -> dict:
    gpu, count = job["gpu"], int(job["count"])
    fabric_min = job.get("min_fabric_gbps")
    best: dict[str, dict] = {}
    for lst in listings:
        if not _eligible(lst, gpu, count):
            continue
        p = _instance_price(lst)
        cur = best.get(lst["provider"])
        if p is not None and (cur is None or p < _instance_price(cur)):
            best[lst["provider"]] = lst
    rows, unranked = [], []
    for lst in best.values():
        r = estimate_listing(lst, job, components, ctx)
        s = r["specs"]
        reason = None
        if s["on_demand_deployable"] is False:
            reason = "not deployable on demand"
        elif fabric_min:
            if s["node_fabric_gbps"] is None:
                reason = "node fabric not published"
            elif s["node_fabric_gbps"] < fabric_min:
                reason = f"node fabric {s['node_fabric_gbps']} Gbps < {fabric_min}"
        if reason:
            r["unranked_reason"] = reason
            unranked.append(r)
        else:
            rows.append(r)
    # Complete totals first: a lower bound could be dearer than it shows, so it never outranks a complete total.
    key = lambda r: (r["total"] is None, r["total_is_lower_bound"], r["total"] if r["total"] is not None else 0,  # noqa: E731
                     r["provider"])
    rows.sort(key=key)
    unranked.sort(key=key)
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    notes = ["List prices; egress allowances are assumed fully available to this job (no other use that month).",
             "A total marked as a lower bound leaves out the components listed in `unknown`; it is never a $0 guess."]
    return {"rows": rows, "unranked": unranked, "notes": notes}


# --------------------------------------------------------------------------- database read (no writes)

_LISTINGS_SQL = """
    SELECT provider, listing_id, sku, raw_gpu_name, canonical_gpu_name, gpu_count, region, price_per_gpu_hour,
           price_per_instance_hour, currency, market_type, interruptible, provider_tier, available, observed_at
    FROM compute_listings
    WHERE canonical_gpu_name = :gpu AND gpu_count = :count AND market_type = 'on_demand'
      AND visibility = 'public'  -- public tier only (methodology/data-visibility.md): the estimate is republished
"""


def context(provider_raw: dict, listings: list[dict]) -> dict:
    """Per-listing facts from provider raw: Vultr plans, and each Vast cheapest offer's own cost fields."""
    vultr = [r.payload for rs in (provider_raw.get("vultr") or {}).values() for r in rs]
    return {"vultr_plans": cc.vultr_plans(vultr)}


def vast_components(provider_raw: dict, listings: list[dict]) -> list[dict]:
    """For each Vast 'cheapest' listing, the components of the offer it was built from (the cheapest per-GPU ask
    in that GPU's stored search)."""
    out = []
    by_gpu = {}
    for ep, rows in (provider_raw.get("vast") or {}).items():
        for r in rows:
            if (r.request or {}).get("census") or not (r.request or {}).get("gpu_name"):
                continue
            offers = [o for o in r.payload or [] if o.get("dph_total") and (o.get("num_gpus") or 0) > 0]
            if offers:
                by_gpu[r.request["gpu_name"]] = (min(offers, key=lambda o: o["dph_total"] / o["num_gpus"]), r.fetched_at)
    for lst in listings:
        if lst["provider"] == "vast" and lst.get("provider_tier") == "cheapest" and lst.get("sku") in by_gpu:
            offer, at = by_gpu[lst["sku"]]
            out += cc.vast_offer_components(offer, lst["listing_id"], at)
    return out


def load_components() -> tuple[list[dict], list[str], dict]:
    import normalize
    from cost import sources

    with normalize.SessionLocal() as s:
        snaps = sources.latest(s)
        raw = {p: normalize._latest_raw(s, p) for p in ("hyperstack", "vultr", "vast")}
    comps, notes = cc.build(snaps, raw)
    return comps, notes, {"snapshots": snaps, "raw": raw}


def load(job: dict) -> dict:
    from sqlalchemy import text

    import market
    import normalize

    now = datetime.now(timezone.utc)
    comps, notes, extra = load_components()
    with normalize.SessionLocal() as s:
        listings = [dict(r._mapping) for r in s.execute(text(_LISTINGS_SQL), {"gpu": job["gpu"], "count": int(job["count"])})]
    listings = [lst for lst in listings if now - lst["observed_at"] <= market.stale_after(lst["provider"])]
    comps += vast_components(extra["raw"], listings)
    out = estimate(job, listings, comps, context(extra["raw"], listings))
    out["notes"] = notes + out["notes"]
    return out
