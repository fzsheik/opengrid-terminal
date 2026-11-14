"""RL post-training cost: trainer + rollouts + sandboxes + weight syncs, same cluster vs split across clouds.

    parse(body, resolve_gpu) -> job                 validated, bounded; raises RLInputError(code, message)
    estimate(job, listings, components, ctx) -> dict pure: strategies, comparison, break-even, sensitivity, caveats
    load(job) -> dict                                reads current listings and stored cost components (no writes)

Kind: estimated (methodology/rl-cost.md). List prices only; nothing here is a quote, and nothing here feeds
listings, indices, spreads, percentiles or routing (like cost/estimate.py, which it builds on).

Formula, per plan (one trainer provider + a set of rollout listings):
    trainer   = nodes x billed_hours x price per instance-hour           (cost/estimate.py: billing increments)
    storage   = stored GB x price per GB-month x hours / 730, on the trainer's provider
    rollouts  = sum over listings: machines x billed_hours x price per instance-hour x overhead
                overhead = interruption_overhead on an interruptible listing, 1 otherwise
    sandbox   = 0 (bundled) when the rollout machines' published vCPU and RAM cover the sandboxes, else the user's
                price per hour x hours, else UNKNOWN
    sync      = weights_gb x syncs_per_hour x hours x fanout through the trainer provider's egress tiers
                + each destination's share through that provider's ingress tiers
                fanout = destinations per sync: default one per distinct rollout provider other than the trainer's
                rollouts on the trainer's own provider: assumed $0 (same cluster), labelled an assumption
    total     = trainer + storage + rollouts + sandbox + sync
A component a plan uses whose price is not published (or not verified) is UNKNOWN: named in `unknown`, the total is
a lower bound, never $0. Plans rank complete totals first (a lower bound could be dearer than it shows).

Strategies:
    same_cluster  the cheapest single provider that hosts the trainer and every rollout GPU (the RL3 baseline)
    split         the cheapest trainer provider + the cheapest eligible rollout listings anywhere
    best          the cheapest plan found over: every trainer provider x (its own cluster, the global rollout fill,
                  its cluster + one other provider, one other provider alone)
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

import hardware
from cost import components as cc
from cost import estimate as est

D0, D1 = Decimal(0), Decimal(1)
_C = Decimal("0.01")
DEFAULT_OVERHEAD = 1.15
SENS_OVERHEADS = (1.0, 1.15, 1.3, 1.5)
SENS_SYNCS = (1, 2, 4, 8)
RANK_BY = ("price", "bandwidth_per_dollar")
HEURISTIC = ("Theoretical heuristic: GPU memory bandwidth (TB/s, vendor peak) per dollar per GPU-hour. Rollout "
             "decoding is often memory-bound, but this is not measured tokens per second: model, quantization, "
             "tensor parallelism, batch size, context length and software change the real ranking.")

# Reference RL jobs (methodology/rl-cost.md). GB are decimal.
_BASE = {"trainer": {"gpu": "NVIDIA H100 80GB SXM5", "count": 8, "nodes": 1}, "weights_gb": 140, "syncs_per_hour": 4,
         "sync_fanout": None, "hours": 72, "storage_gb": 2000, "storage_class": "auto", "sandboxes": None,
         "same_cluster_only": False}
PRESETS: dict[str, dict] = {
    "RL1": {**_BASE, "label": "1 x 8-GPU H100 trainer + 32 rollout GPUs (80 GB or more), 72 h, 140 GB weights synced 4x an hour",
            "rollouts": {"gpu": "auto", "count": 32, "min_vram_gb": 80, "interruptible": False,
                         "interruption_overhead": DEFAULT_OVERHEAD, "rank_by": "price"}},
    "RL2": {**_BASE, "label": "1 x 8-GPU H100 trainer + 128 rollout GPUs (48 GB or more, interruptible allowed), 72 h",
            "rollouts": {"gpu": "auto", "count": 128, "min_vram_gb": 48, "interruptible": True,
                         "interruption_overhead": DEFAULT_OVERHEAD, "rank_by": "price"}},
    "RL3": {**_BASE, "label": "Same-cluster baseline: RL1 with the trainer and every rollout GPU on one provider",
            "same_cluster_only": True,
            "rollouts": {"gpu": "auto", "count": 32, "min_vram_gb": 80, "interruptible": False,
                         "interruption_overhead": DEFAULT_OVERHEAD, "rank_by": "price"}},
}


def _money(v: Decimal | None) -> float | None:
    return None if v is None else float(v.quantize(_C, rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- input

class RLInputError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


TOP = {"preset", "trainer", "rollouts", "sandboxes", "weights_gb", "syncs_per_hour", "sync_fanout", "hours",
       "storage_gb", "storage_tb", "storage_class", "same_cluster_only"}
SUB = {"trainer": {"gpu", "count", "nodes"},
       "rollouts": {"gpu", "count", "min_vram_gb", "interruptible", "interruption_overhead", "rank_by"},
       "sandboxes": {"vcpu", "ram_gb", "count", "price_per_hour"}}


def _num(d: dict, key: str, lo: float, hi: float, *, integer=False, name=None):
    name = name or key
    v = d.get(key)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or (integer and not isinstance(v, int)):
        raise RLInputError("invalid_" + name.replace(".", "_"), f"{name} must be {'a whole number' if integer else 'a number'}")
    if not lo <= v <= hi:
        raise RLInputError("invalid_" + name.replace(".", "_"), f"{name} must be between {lo} and {hi}")
    return v


def parse(body: dict, resolve_gpu) -> dict:
    """A request body -> the estimator's job. A preset fills every field; the body overrides field by field
    (inside trainer / rollouts / sandboxes too). resolve_gpu(str) -> canonical name or None."""
    unknown = set(body) - TOP
    if unknown:
        raise RLInputError("unknown_field", f"unknown field(s): {sorted(unknown)}")
    for k, allowed in SUB.items():
        v = body.get(k)
        if v is None:
            continue
        if not isinstance(v, dict):
            raise RLInputError("invalid_" + k, f"{k} must be an object" + (" or null" if k == "sandboxes" else ""))
        if set(v) - allowed:
            raise RLInputError("unknown_field", f"unknown field(s) in {k}: {sorted(set(v) - allowed)}")
    job: dict = {}
    if body.get("preset") is not None:
        if body["preset"] not in PRESETS:
            raise RLInputError("invalid_preset", f"one of {list(PRESETS)}")
        job = {k: (dict(v) if isinstance(v, dict) else v) for k, v in PRESETS[body["preset"]].items() if k != "label"}
        job["preset"] = body["preset"]

    # trainer
    t = {**(job.get("trainer") or {}), **{k: v for k, v in (body.get("trainer") or {}).items() if v is not None}}
    g = t.get("gpu")
    gpu = resolve_gpu(g) if isinstance(g, str) and g else None
    if not gpu:
        raise RLInputError("invalid_trainer_gpu", "trainer.gpu: a GPU slug or canonical name")
    count = _num(t, "count", 1, 64, integer=True, name="trainer.count")
    if count is None:
        raise RLInputError("missing_trainer_count", "trainer.count (GPUs per trainer node) is required")
    nodes = _num(t, "nodes", 1, 128, integer=True, name="trainer.nodes") or 1
    job["trainer"] = {"gpu": gpu, "count": count, "nodes": nodes}

    # rollouts
    r = {**(job.get("rollouts") or {}), **{k: v for k, v in (body.get("rollouts") or {}).items() if v is not None}}
    rg = r.get("gpu", "auto")
    if rg in (None, "", "auto"):
        rgpu = "auto"
    else:
        rgpu = resolve_gpu(rg) if isinstance(rg, str) else None
        if not rgpu:
            raise RLInputError("invalid_rollouts_gpu", 'rollouts.gpu: "auto" or a GPU slug or canonical name')
    rcount = _num(r, "count", 1, 4096, integer=True, name="rollouts.count")
    if rcount is None:
        raise RLInputError("missing_rollouts_count", "rollouts.count (rollout GPUs) is required")
    vram = _num(r, "min_vram_gb", 0, 1024, name="rollouts.min_vram_gb") or 0
    inter = r.get("interruptible", False)
    if not isinstance(inter, bool):
        raise RLInputError("invalid_rollouts_interruptible", "rollouts.interruptible must be true or false")
    over = _num(r, "interruption_overhead", 1, 5, name="rollouts.interruption_overhead")
    rank_by = r.get("rank_by", "price")
    if rank_by not in RANK_BY:
        raise RLInputError("invalid_rollouts_rank_by", f"rollouts.rank_by: one of {list(RANK_BY)}")
    job["rollouts"] = {"gpu": rgpu, "count": rcount, "min_vram_gb": vram, "interruptible": inter,
                       "interruption_overhead": DEFAULT_OVERHEAD if over is None else over, "rank_by": rank_by}

    # sandboxes
    if "sandboxes" in body and body["sandboxes"] is None:
        job["sandboxes"] = None
    else:
        s = {**(job.get("sandboxes") or {}), **(body.get("sandboxes") or {})}
        if s:
            sb = {"vcpu": _num(s, "vcpu", 0, 1024, name="sandboxes.vcpu") or 0,
                  "ram_gb": _num(s, "ram_gb", 0, 8192, name="sandboxes.ram_gb") or 0,
                  "count": _num(s, "count", 0, 100_000, integer=True, name="sandboxes.count") or 0,
                  "price_per_hour": _num(s, "price_per_hour", 0, 1_000_000, name="sandboxes.price_per_hour")}
            job["sandboxes"] = sb if sb["count"] and (sb["vcpu"] or sb["ram_gb"]) else None
        else:
            job["sandboxes"] = None

    for key, lo, hi, integer in (("weights_gb", 0.001, 100_000, False), ("syncs_per_hour", 0, 3600, False),
                                 ("hours", 1 / 60, 24 * 365, False)):
        v = _num(body, key, lo, hi, integer=integer)
        if v is not None:
            job[key] = v
        elif job.get(key) is None:
            raise RLInputError("missing_" + key, f"{key} is required")
    if "sync_fanout" in body:
        job["sync_fanout"] = _num(body, "sync_fanout", 1, 1024, integer=True)
    job.setdefault("sync_fanout", None)
    tb, gb = _num(body, "storage_tb", 0, 10_000), _num(body, "storage_gb", 0, 10_000_000)
    if tb is not None and gb is not None:
        raise RLInputError("invalid_storage", "give storage_tb or storage_gb, not both")
    if tb is not None:
        job["storage_gb"] = tb * 1000
    elif gb is not None:
        job["storage_gb"] = gb
    job.setdefault("storage_gb", 0)
    sc = body.get("storage_class", job.get("storage_class", "auto"))
    if sc not in est.STORAGE_ORDER:
        raise RLInputError("invalid_storage_class", f"one of {list(est.STORAGE_ORDER)}")
    job["storage_class"] = sc
    sco = body.get("same_cluster_only", job.get("same_cluster_only", False))
    if not isinstance(sco, bool):
        raise RLInputError("invalid_same_cluster_only", "same_cluster_only must be true or false")
    job["same_cluster_only"] = sco
    return job


# --------------------------------------------------------------------------- candidates

def _cap_gpus(lst: dict) -> int | None:
    cap, unit = lst.get("capacity"), (lst.get("capacity_unit") or "").lower()
    if cap is None:
        return None
    if unit == "gpu":
        return int(cap)
    if unit == "instance":
        return int(cap) * int(lst.get("gpu_count") or 1)
    return None


def _interruptible(lst: dict) -> bool:
    return bool(lst.get("interruptible")) or lst.get("market_type") == "spot"


def rollout_candidates(job: dict, listings: list[dict], ctx: dict | None, overhead: float | None = None) -> tuple[list[dict], dict]:
    """Every listing a rollout worker could run on, with its effective price; and {reason: count} of the rest."""
    ro = job["rollouts"]
    over = Decimal(str(ro["interruption_overhead"] if overhead is None else overhead))
    hours = Decimal(str(job["hours"]))
    out, excluded = [], {}

    def skip(reason):
        excluded[reason] = excluded.get(reason, 0) + 1

    for lst in listings:
        gpu = lst.get("canonical_gpu_name")
        if not gpu or (ro["gpu"] != "auto" and gpu != ro["gpu"]):
            continue
        intr = _interruptible(lst)
        if lst.get("market_type") not in ("on_demand", "spot") or (intr and not ro["interruptible"]):
            skip("interruptible_not_allowed" if intr else "not_on_demand")
            continue
        if (lst.get("provider_tier") or "") in ("from_price", "gpu-attachment") or (
                lst["provider"] == "vast" and lst.get("provider_tier") == "median"):
            skip("not_a_rentable_offer")
            continue
        if lst.get("available") is False:
            skip("sold_out")
            continue
        hw = hardware.spec(gpu) or {}
        vram = hw.get("vram_gb")
        if vram is None:
            skip("vram_unknown")
            continue
        if vram < ro["min_vram_gb"]:
            skip("below_min_vram")
            continue
        price = est._instance_price(lst)
        if price is None or price <= 0:
            skip("no_price")
            continue
        cap = _cap_gpus(lst)
        if cap is not None and cap < int(lst.get("gpu_count") or 1):
            skip("no_listed_capacity")
            continue
        specs = est.listing_specs(lst, ctx)
        bh, exact = est.billed_hours(hours, specs["billing_increment_s"], specs["billing_minimum_s"])
        gc = int(lst.get("gpu_count") or 1)
        factor = over if intr else D1
        per_gpu = price / gc
        eff = per_gpu * factor
        bw = hw.get("memory_bandwidth_tbps")
        out.append({"listing": lst, "provider": lst["provider"], "gpu": gpu, "gpu_count": gc, "price": price,
                    "per_gpu": per_gpu, "eff_per_gpu": eff, "interruptible": intr, "factor": factor,
                    "vram_gb": vram, "bandwidth_tbps": bw,
                    "bw_per_dollar": None if bw is None else Decimal(str(bw)) / eff,
                    "cap_gpus": cap, "billed_hours": bh, "exact": exact, "specs": specs})
    if ro["rank_by"] == "bandwidth_per_dollar":
        out.sort(key=lambda c: (c["bw_per_dollar"] is None, -(c["bw_per_dollar"] or 0), c["eff_per_gpu"], c["provider"]))
    else:
        out.sort(key=lambda c: (c["eff_per_gpu"], c["provider"], c["listing"].get("listing_id") or ""))
    return out, excluded


def allocate(cands: list[dict], need: int, providers: set | None = None) -> tuple[list[tuple[dict, int]], int]:
    """Greedy fill in the candidates' order: whole machines, capped by listed capacity where it is published.
    Returns ([(candidate, machines)], GPUs still missing)."""
    left, picks = need, []
    for c in cands:
        if left <= 0:
            break
        if providers is not None and c["provider"] not in providers:
            continue
        gc = c["gpu_count"]
        if left < gc:  # the remainder: the single listing that covers it for the least money, not a half-empty machine
            cover = [(x, math.ceil(left / x["gpu_count"])) for x in cands
                     if (providers is None or x["provider"] in providers) and all(x is not p for p, _ in picks)]
            cover = [(x, m) for x, m in cover if x["cap_gpus"] is None or x["cap_gpus"] >= m * x["gpu_count"]]
            if cover:
                x, m = min(cover, key=lambda t: (t[1] * t[0]["price"] * t[0]["factor"] * t[0]["billed_hours"], t[1] * t[0]["gpu_count"]))
                picks.append((x, m))
                left -= m * x["gpu_count"]
                break
        want = math.ceil(left / gc)
        m = want if c["cap_gpus"] is None else min(want, c["cap_gpus"] // gc)
        if m <= 0:
            continue
        picks.append((c, m))
        left -= m * gc
    return picks, max(0, left)


def trainer_candidates(job: dict, listings: list[dict], components: list[dict], ctx: dict | None) -> tuple[list[dict], list[dict]]:
    """Per provider: its cheapest on-demand listing of the trainer GPU at exactly `count` per machine, priced for the
    job's hours and storage (cost/estimate.py). Returns (usable, unranked with a reason)."""
    t = job["trainer"]
    best: dict[str, dict] = {}
    for lst in listings:
        if not est._eligible(lst, t["gpu"], t["count"]):
            continue
        p, cur = est._instance_price(lst), best.get(lst["provider"])
        if p is not None and (cur is None or p < est._instance_price(cur)):
            best[lst["provider"]] = lst
    sub = {"hours": job["hours"], "egress_gb": 0, "storage_gb": job["storage_gb"], "storage_class": job["storage_class"]}
    ok, unranked = [], []
    for lst in best.values():
        r = est.estimate_listing(lst, sub, components, ctx)
        r["listing"] = lst
        s = r["specs"]
        reason = None
        if r["components"]["compute"]["amount"] is None:
            reason = "no compute price"
        elif s["on_demand_deployable"] is False:
            reason = "not deployable on demand"
        elif t["nodes"] > 1 and s["multi_node"] is False:
            reason = "not sold as a multi-node training fabric"
        r["fabric_unknown"] = t["nodes"] > 1 and s["multi_node"] is None
        (unranked if reason else ok).append(r | ({"unranked_reason": reason} if reason else {}))
    key = lambda r: (r["fabric_unknown"], r["total_is_lower_bound"], r["total"], r["provider"])  # noqa: E731
    ok.sort(key=key)
    return ok, unranked


# --------------------------------------------------------------------------- one plan

def _line(item: str, amount: Decimal | None, **kw) -> dict:
    return {"item": item, "amount": _money(amount), "known": amount is not None, "price_concept": "list_price", **kw}


def _kind(comp: dict | None) -> str | None:
    if comp is None:
        return None
    return "manual" if comp["origin"] == "manual" else "observed"


def plan(job: dict, trainer: dict, picks: list[tuple[dict, int]], components: list[dict], strategy: str) -> dict:
    t = job["trainer"]
    nodes = t["nodes"]
    hours = Decimal(str(job["hours"]))
    tl = trainer["listing"]
    tp = trainer["provider"]
    lines, unknown, notes = [], [], []

    # trainer compute and storage (from the full-cost estimator's row for one machine)
    comp = trainer["components"]["compute"]
    tc = Decimal(str(comp["amount"])) * nodes if comp["amount"] is not None else None
    lines.append(_line("trainer_compute", tc, provider=tp, listing_id=tl.get("listing_id"), sku=tl.get("sku"),
                       region=tl.get("region"), gpu=t["gpu"], gpus=t["count"] * nodes, machines=nodes,
                       billed_hours=comp["billed_hours"], unit_price=comp["price_per_instance_hour"],
                       unit="usd_per_instance_hour", kind="observed", source="listing",
                       notes=[n for n in trainer["notes"] if "billing" in n]))
    st = trainer["components"].get("storage")
    if st is not None:
        amt = None if not st["known"] else Decimal(str(st["amount"]))
        lines.append(_line("trainer_storage", amt, provider=tp, kind=_kind(st) if st["known"] else None,
                           source=st.get("source"), product=st.get("product"), storage_class=st.get("class"),
                           gb=job["storage_gb"]))
        if amt is None:
            unknown.append(f"trainer_storage:{tp}")

    # rollouts
    rsum = D0
    vcpu = ram = D0
    vcpu_known = True
    for c, m in picks:
        lst = c["listing"]
        amt = Decimal(m) * c["price"] * c["billed_hours"] * c["factor"]
        rsum += amt
        lines.append(_line("rollout_compute", amt, provider=c["provider"], listing_id=lst.get("listing_id"),
                           sku=lst.get("sku"), region=lst.get("region"), gpu=c["gpu"], gpus=m * c["gpu_count"],
                           machines=m, billed_hours=float(c["billed_hours"]), unit_price=float(c["price"]),
                           unit="usd_per_instance_hour", interruptible=c["interruptible"],
                           interruption_overhead=float(c["factor"]), vram_gb=c["vram_gb"],
                           bandwidth_tbps=c["bandwidth_tbps"],
                           bandwidth_per_dollar=None if c["bw_per_dollar"] is None else float(round(c["bw_per_dollar"], 4)),
                           capacity_published=c["cap_gpus"] is not None, kind="observed", source="listing"))
        if lst.get("vcpu") is None or lst.get("ram_gb") is None:
            vcpu_known = False
        else:
            vcpu += Decimal(str(lst["vcpu"])) * m
            ram += Decimal(str(lst["ram_gb"])) * m
    if any(c["cap_gpus"] is None for c, _ in picks):
        notes.append("Some rollout listings do not publish capacity: the plan assumes the provider can supply them.")

    # sandboxes
    sb = job.get("sandboxes")
    if sb:
        need_cpu = Decimal(str(sb["vcpu"])) * sb["count"]
        need_ram = Decimal(str(sb["ram_gb"])) * sb["count"]
        if vcpu_known and picks and vcpu >= need_cpu and ram >= need_ram:
            lines.append(_line("sandbox", D0, kind="inferred", source="rollout machines' published vCPU and RAM",
                               notes=["bundled in the rollout machines' price (not free), assuming rollout "
                                      "inference leaves that much CPU and RAM idle"],
                               vcpu=float(need_cpu), ram_gb=float(need_ram)))
        elif sb.get("price_per_hour") is not None:
            lines.append(_line("sandbox", Decimal(str(sb["price_per_hour"])) * hours, kind="user_input",
                               source="your price per hour", vcpu=float(need_cpu), ram_gb=float(need_ram)))
        else:
            lines.append(_line("sandbox", None, kind=None, source=None, vcpu=float(need_cpu), ram_gb=float(need_ram),
                               notes=["no CPU sandbox price dataset yet; give sandboxes.price_per_hour to price it"]))
            unknown.append("sandbox")

    # weight sync
    per_dest = Decimal(str(job["weights_gb"])) * Decimal(str(job["syncs_per_hour"])) * hours
    dests = sorted({c["provider"] for c, _ in picks} - {tp})
    on_trainer = any(c["provider"] == tp for c, _ in picks)
    fanout = job.get("sync_fanout") or len(dests)
    basis = ("given" if job.get("sync_fanout") else "default: one copy per rollout provider other than the trainer's")
    transfer_gb = per_dest * fanout if dests else D0
    sync = {"gb_per_destination": float(per_dest), "destinations": dests, "fanout": fanout if dests else 0,
            "fanout_basis": basis, "transfer_gb": float(transfer_gb), "egress_price_per_gb": None,
            "ingress_price_per_gb": None}
    if on_trainer or not dests:
        lines.append(_line("sync_intra_cluster", D0, provider=tp, kind="assumption", source=None,
                           gb=float(per_dest) if on_trainer else 0.0,
                           notes=["rollouts on the trainer's provider: transfer inside one cluster assumed not billed "
                                  "(true when they share a region and private network; inter-region or inter-zone "
                                  "transfer inside a provider is not priced here)"]))
    egress = ingress = None
    ingress_complete = True
    if dests:
        ec = cc.lookup(components, tp, "egress_internet", tl.get("listing_id"))
        if ec is None:
            lines.append(_line("sync_egress", None, provider=tp, gb=float(transfer_gb), kind=None, source=None,
                               notes=[f"{tp} publishes no checked internet egress price"]))
            unknown.append(f"sync_egress:{tp}")
        else:
            allowance = Decimal(str(trainer["specs"].get("included_egress_gb_month") or 0))
            egress = est.tiered(transfer_gb, ec["tiers"], allowance)
            sync["egress_price_per_gb"] = float(round(egress / transfer_gb, 6)) if transfer_gb else 0.0
            lines.append(_line("sync_egress", egress, provider=tp, gb=float(transfer_gb), kind=_kind(ec),
                               source=ec["source"], product=ec["product"], tiers="monthly tiers, allowance applied first"))
        share = transfer_gb / len(dests)
        ingress = D0
        ingress_complete = True
        for d in dests:
            lid = next((c["listing"].get("listing_id") for c, _ in picks if c["provider"] == d), None)
            ic = cc.lookup(components, d, "ingress_internet", lid)
            if ic is None:
                shown = cc.lookup(components, d, "ingress_internet", lid, usable_only=False)
                lines.append(_line("sync_ingress", None, provider=d, gb=float(share), kind=None, source=None,
                                   notes=[f"{d}: ingress price {'unverified' if shown else 'not published'}; not counted"]))
                unknown.append(f"sync_ingress:{d}")
                ingress_complete = False
                continue
            amt = est.tiered(share, ic["tiers"])
            ingress += amt
            lines.append(_line("sync_ingress", amt, provider=d, gb=float(share), kind=_kind(ic), source=ic["source"],
                               product=ic["product"]))
        if transfer_gb and ingress_complete:
            sync["ingress_price_per_gb"] = float(round(ingress / transfer_gb, 6))

    known = [Decimal(str(ln["amount"])) for ln in lines if ln["amount"] is not None]
    total = sum(known, D0) if tc is not None else None
    if tc is None:
        unknown.insert(0, "trainer_compute")

    def part(*items):
        vals = [ln for ln in lines if ln["item"] in items]
        return {"amount": _money(sum((Decimal(str(v["amount"])) for v in vals if v["amount"] is not None), D0)) if vals else None,
                "complete": all(v["known"] for v in vals)}

    providers = sorted({tp} | {c["provider"] for c, _ in picks})
    return {"strategy": strategy, "trainer_provider": tp, "rollout_providers": sorted({c["provider"] for c, _ in picks}),
            "providers": providers, "split": bool(dests),
            "trainer": {"provider": tp, "listing_id": tl.get("listing_id"), "sku": tl.get("sku"), "region": tl.get("region"),
                        "gpu": t["gpu"], "gpus_per_node": t["count"], "nodes": nodes,
                        "node_fabric": trainer["specs"]["node_fabric"], "node_fabric_gbps": trainer["specs"]["node_fabric_gbps"],
                        "multi_node": trainer["specs"]["multi_node"]},
            "rollout_gpus": sum(m * c["gpu_count"] for c, m in picks),
            "parts": {"trainer": part("trainer_compute"), "storage": part("trainer_storage"),
                      "rollouts": part("rollout_compute"), "sandbox": part("sandbox"),
                      "sync": part("sync_egress", "sync_ingress", "sync_intra_cluster")},
            "lines": lines, "total": _money(total), "total_is_lower_bound": bool(unknown), "unknown": unknown,
            "sync": sync, "notes": notes,
            "_transfer_gb": transfer_gb, "_transfer": None if (dests and (egress is None or not ingress_complete)) else (egress or D0) + (ingress or D0),
            "_ex_transfer": None if total is None else total - (egress or D0) - (ingress or D0),
            "_egress_comp": cc.lookup(components, tp, "egress_internet", tl.get("listing_id")) if dests else None,
            "_allowance": Decimal(str(trainer["specs"].get("included_egress_gb_month") or 0)),
            "_ingress_comps": {d: cc.lookup(components, d, "ingress_internet",
                                            next((c["listing"].get("listing_id") for c, _ in picks if c["provider"] == d), None))
                               for d in dests}}


def _key(p: dict):
    return (p["total"] is None, p["total_is_lower_bound"], p["total"] if p["total"] is not None else 0,
            len(p["providers"]), p["trainer_provider"])


# --------------------------------------------------------------------------- strategies

def strategies(job: dict, trainers: list[dict], cands: list[dict], components: list[dict], search: bool = True) -> dict:
    need = job["rollouts"]["count"]
    out: dict = {"same_cluster": None, "split": None, "best": None, "unavailable": {}, "plans_searched": 0}
    same = []
    for t in trainers:
        picks, left = allocate(cands, need, {t["provider"]})
        if not left:
            same.append(plan(job, t, picks, components, "same_cluster"))
    if same:
        out["same_cluster"] = min(same, key=_key)
    else:
        out["unavailable"]["same_cluster"] = ("no single provider lists both the trainer and enough eligible rollout GPUs"
                                              if trainers else "no current listing of the trainer GPU at that size")
    if job.get("same_cluster_only"):
        out["best"] = out["same_cluster"]
        out["unavailable"]["split"] = "same_cluster_only: the split plan was not asked for"
        out["plans_searched"] = len(same)
        return out
    if not trainers:
        out["unavailable"]["split"] = out["unavailable"]["same_cluster"]
        out["unavailable"]["best"] = out["unavailable"]["same_cluster"]
        return out
    picks, left = allocate(cands, need)
    if left:
        out["unavailable"]["split"] = f"only {need - left} of {need} rollout GPUs are listed with that VRAM and terms"
    else:
        out["split"] = plan(job, trainers[0], picks, components, "split")
    pool = list(same) + ([out["split"]] if out["split"] else [])
    if search and not left:
        rprov = sorted({c["provider"] for c in cands})
        for t in trainers:
            tried = set()
            for provs in [None] + [{t["provider"], p} for p in rprov if p != t["provider"]] + [{p} for p in rprov if p != t["provider"]]:
                pk, lf = allocate(cands, need, provs)
                sig = tuple((c["listing"].get("listing_id"), c["provider"], m) for c, m in pk)
                if lf or sig in tried:
                    continue
                tried.add(sig)
                pool.append(plan(job, t, pk, components, "best"))
    out["plans_searched"] = len(pool)
    if pool:
        b = dict(min(pool, key=_key))
        b["found_as"] = b["strategy"]
        b["strategy"] = "best"
        out["best"] = b
        known = [p for p in pool if p["total"] is not None]
        cheap = min(known, key=lambda p: (p["total"], len(p["providers"]))) if known else None
        if cheap is not None and cheap["total_is_lower_bound"] and b["total"] is not None and cheap["total"] < b["total"]:
            c = dict(cheap)
            c["strategy"] = "cheapest_lower_bound"
            out["cheapest_lower_bound"] = c
    else:
        out["unavailable"]["best"] = "no plan could host every rollout GPU"
    return out


def _saving(base: dict | None, split: dict | None) -> dict | None:
    if not base or not split or base["total"] is None or split["total"] is None:
        return None
    s = Decimal(str(base["total"])) - Decimal(str(split["total"]))
    lb_b, lb_s = base["total_is_lower_bound"], split["total_is_lower_bound"]
    bound = "exact" if not (lb_b or lb_s) else "at_most" if lb_s and not lb_b else "at_least" if lb_b and not lb_s else "indeterminate"
    return {"saving": _money(s), "saving_pct": float(round(s / Decimal(str(base["total"])) * 100, 1)) if base["total"] else None,
            "bound": bound}


def _transfer_cost(p: dict, gb: Decimal) -> Decimal | None:
    ec = p["_egress_comp"]
    if ec is None or any(v is None for v in p["_ingress_comps"].values()):
        return None
    n = max(1, len(p["_ingress_comps"]))
    return est.tiered(gb, ec["tiers"], p["_allowance"]) + sum(
        (est.tiered(gb / n, ic["tiers"]) for ic in p["_ingress_comps"].values()), D0)


def break_even(job: dict, base: dict | None, split: dict | None) -> dict:
    """At what transfer price (egress + ingress, $/GB) or sync volume the split plan stops saving against the
    same-cluster baseline, every other line held fixed."""
    out = {"available": False, "reason": None}
    if not base or not split:
        out["reason"] = "needs both the same-cluster and the split plan"
        return out
    if not split["split"]:
        out["reason"] = "the split plan found uses only the trainer's provider: no cross-cloud transfer to break even on"
        return out
    if base["total"] is None or split["_ex_transfer"] is None:
        out["reason"] = "a compute price is missing"
        return out
    margin = Decimal(str(base["total"])) - split["_ex_transfer"]
    gb = split["_transfer_gb"]
    out.update(available=True, transfer_gb=float(gb), margin_before_transfer=_money(margin),
               current_transfer_cost=_money(split["_transfer"]),
               current_price_per_gb=None if split["_transfer"] is None or not gb else float(round(split["_transfer"] / gb, 6)),
               lower_bound_inputs=base["total_is_lower_bound"] or any(
                   not u.startswith(("sync_egress", "sync_ingress")) for u in split["unknown"]))
    if margin <= 0:
        out.update(break_even_price_per_gb=0.0, break_even_syncs_per_hour=0.0, splits_save=False,
                   summary="Splitting costs more than the same-cluster baseline even if moving the weights were free.")
        return out
    out["break_even_price_per_gb"] = float(round(margin / gb, 6)) if gb else None
    per_sync_hour = Decimal(str(job["weights_gb"])) * Decimal(str(job["hours"])) * split["sync"]["fanout"]
    if split["_transfer"] is None:
        out.update(break_even_syncs_per_hour=None, splits_save=None,
                   summary=(f"An egress or ingress price on this route is not published: splitting saves money only if "
                            f"moving the weights costs less than ${out['break_even_price_per_gb']:.4f} per GB "
                            f"({float(gb):,.0f} GB over the job)."))
        return out
    hi = Decimal(10) ** 12
    if _transfer_cost(split, hi) < margin:
        out.update(break_even_syncs_per_hour=None, splits_save=True,
                   summary="Moving the weights is free or nearly free on this route: splitting saves at any sync volume.")
        return out
    lo = D0
    for _ in range(200):
        mid = (lo + hi) / 2
        if _transfer_cost(split, mid) < margin:
            lo = mid
        else:
            hi = mid
    be_s = lo / per_sync_hour if per_sync_hour else None
    out.update(break_even_transfer_gb=float(round(lo, 1)),
               break_even_syncs_per_hour=None if be_s is None else float(round(be_s, 3)),
               splits_save=split["_transfer"] < margin,
               summary=(f"Splitting saves money while moving the weights costs under "
                        f"${out['break_even_price_per_gb']:.4f}/GB, or, at today's prices, while syncs stay under "
                        f"{float(be_s):,.2f} an hour ({float(lo):,.0f} GB over the job)." if be_s is not None else None))
    return out


def ranking(cands: list[dict], limit: int = 15) -> list[dict]:
    """Rollout GPU models by memory bandwidth per dollar (the cheapest eligible listing of each)."""
    best: dict[str, dict] = {}
    for c in cands:
        cur = best.get(c["gpu"])
        if cur is None or c["eff_per_gpu"] < cur["eff_per_gpu"]:
            best[c["gpu"]] = c
    rows = [{"gpu": g, "vram_gb": c["vram_gb"], "bandwidth_tbps": c["bandwidth_tbps"],
             "price_per_gpu_hour": float(round(c["eff_per_gpu"], 4)), "provider": c["provider"],
             "interruptible": c["interruptible"],
             "bandwidth_per_dollar": None if c["bw_per_dollar"] is None else float(round(c["bw_per_dollar"], 4))}
            for g, c in best.items()]
    rows.sort(key=lambda r: (r["bandwidth_per_dollar"] is None, -(r["bandwidth_per_dollar"] or 0), r["gpu"]))
    return rows[:limit]


def caveats(job: dict, res: dict) -> list[str]:
    """Every known gap (methodology/rl-cost.md, 'Gaps') that applies to this request, in plain words."""
    plans = [p for p in (res.get("same_cluster"), res.get("split"), res.get("best")) if p]
    unk = {u for p in plans for u in p["unknown"]}
    out = ["List prices only. Enterprise discounts, committed-use deals and negotiated rates are not included."]
    if job.get("sandboxes"):
        out.append("OpenGrid has no dataset of CPU sandbox prices yet. Sandboxes are counted as bundled only when the "
                   "rollout machines publish enough vCPU and RAM; otherwise they are unknown unless you give a price.")
    if job["trainer"]["nodes"] > 1:
        out.append("Node-to-node fabric is not published for every provider, so whether a multi-node trainer can run "
                   "well there is not always known. Plans on a provider with an unpublished fabric are flagged.")
    if job["rollouts"]["interruptible"]:
        out.append(f"Spot reclaim rates are not measured. The {job['rollouts']['interruption_overhead']}x interruption "
                   "overhead on interruptible rollout GPUs is an assumption you can change; provider API uptime is "
                   "not a measure of how often spot machines are taken back.")
    if any(u.startswith("sync_ingress") for u in unk):
        names = sorted({u.split(":", 1)[1] for u in unk if u.startswith("sync_ingress")})
        out.append("Ingress (data in) prices are not published or not verified for: " + ", ".join(names)
                   + ". They are left out, so those totals are lower bounds.")
    if any(u.startswith("sync_egress") for u in unk):
        out.append("The trainer provider's internet egress price is not published or not verified, so the weight-sync "
                   "cost is left out and the total is a lower bound. The break-even price still tells you the limit.")
    out.append("Rollout throughput (tokens per second per GPU) is not benchmarked. The rollout GPU count is yours, and "
               "the bandwidth-per-dollar ranking is a theoretical heuristic, not a measurement.")
    out.append("The engineering cost of running training and inference on different clouds is not included: latency, "
               "cross-cloud reliability, where checkpoints live, and keeping rollout workers on the right weights.")
    out.append("Weight sync is modelled as full copies (weights x syncs per hour x hours x fanout). Real systems may "
               "compress, send partial updates or relay between workers, which changes the volume either way.")
    out.append("Egress tiers and free allowances are monthly and assumed fully available to this job.")
    if any(p for p in plans if any("capacity" in n for n in p["notes"])):
        out.append("Some rollout providers do not publish capacity; those plans assume the GPUs are available.")
    return out


def estimate(job: dict, listings: list[dict], components: list[dict], ctx: dict | None = None,
             sensitivity: bool = True) -> dict:
    trainers, unranked = trainer_candidates(job, listings, components, ctx)
    cands, excluded = rollout_candidates(job, listings, ctx)
    res = strategies(job, trainers, cands, components)
    base, split = res["same_cluster"], res["split"]
    out = {"same_cluster": base, "split": split, "best": res["best"],
           "cheapest_lower_bound": res.get("cheapest_lower_bound"), "unavailable": res["unavailable"],
           "plans_searched": res["plans_searched"],
           "comparison": {"split_vs_same_cluster": _saving(base, split),
                          "best_vs_same_cluster": _saving(base, res["best"])},
           "break_even": break_even(job, base, split) if not job.get("same_cluster_only") else
           {"available": False, "reason": "same_cluster_only"},
           "rollout_ranking": {"basis": HEURISTIC, "kind": "estimated", "rows": ranking(cands)},
           "trainer_unranked": [{"provider": r["provider"], "listing_id": r["listing_id"], "reason": r["unranked_reason"]}
                                for r in unranked],
           "rollout_excluded": excluded, "rollout_candidates": len(cands),
           "caveats": caveats(job, res)}
    if sensitivity and not job.get("same_cluster_only"):
        out["sensitivity"] = sensitivity_table(job, listings, components, ctx, trainers)
    for p in (base, split, res["best"], out["cheapest_lower_bound"]):
        if p:
            for k in [k for k in p if k.startswith("_")]:
                p.pop(k)
    return out


def sensitivity_table(job: dict, listings, components, ctx, trainers) -> dict:
    ro = job["rollouts"]
    overs = sorted(set(SENS_OVERHEADS) | {ro["interruption_overhead"]}) if ro["interruptible"] else [ro["interruption_overhead"]]
    syncs = sorted(set(SENS_SYNCS) | {job["syncs_per_hour"]})
    rows = []
    for o in overs:
        cands, _ = rollout_candidates(job, listings, ctx, overhead=o)
        for s in syncs:
            j = {**job, "syncs_per_hour": s, "rollouts": {**ro, "interruption_overhead": o}}
            r = strategies(j, trainers, cands, components, search=False)
            b, sp = r["same_cluster"], r["split"]
            sv = _saving(b, sp)
            rows.append({"interruption_overhead": o, "syncs_per_hour": s,
                         "same_cluster_total": b and b["total"], "split_total": sp and sp["total"],
                         "saving": sv and sv["saving"], "saving_bound": sv and sv["bound"],
                         "lower_bound": bool((b and b["total_is_lower_bound"]) or (sp and sp["total_is_lower_bound"])),
                         "current": o == ro["interruption_overhead"] and s == job["syncs_per_hour"]})
    return {"interruption_overheads": overs, "syncs_per_hour": syncs, "rows": rows,
            "note": ("Same-cluster baseline vs the split plan, recomputed for each pair. The interruption overhead "
                     "only changes interruptible rollout GPUs" + ("" if ro["interruptible"] else
                                                                   " (not allowed in this job, so only yours is shown)") + ".")}


# --------------------------------------------------------------------------- database read (no writes)

_LISTINGS_SQL = """
    SELECT provider, listing_id, sku, raw_gpu_name, canonical_gpu_name, gpu_count, region, price_per_gpu_hour,
           price_per_instance_hour, currency, market_type, interruptible, provider_tier, available, capacity,
           capacity_unit, vcpu, ram_gb, observed_at
    FROM compute_listings
    WHERE canonical_gpu_name IS NOT NULL AND market_type IN ('on_demand', 'spot')
      AND visibility = 'public'  -- public tier only (methodology/data-visibility.md): the estimate is republished
"""


def load_inputs() -> dict:
    """The database read every estimate starts from: fresh public listings, cost components, context. Read-only
    for the estimate (api/cost.py caches it briefly)."""
    from sqlalchemy import text

    import market
    import normalize

    now = datetime.now(timezone.utc)
    comps, notes, extra = est.load_components()
    with normalize.SessionLocal() as s:
        listings = [dict(r._mapping) for r in s.execute(text(_LISTINGS_SQL))]
    listings = [lst for lst in listings if now - lst["observed_at"] <= market.stale_after(lst["provider"])]
    comps += est.vast_components(extra["raw"], listings)
    return {"listings": listings, "components": comps, "notes": notes, "ctx": est.context(extra["raw"], listings)}


def load(job: dict, inputs: dict | None = None) -> dict:
    inp = inputs if inputs is not None else load_inputs()
    out = estimate(job, inp["listings"], inp["components"], inp["ctx"])
    out["notes"] = list(inp["notes"]) + ["List prices, not quotes. Unknown components are named and never counted as $0."]
    return out
