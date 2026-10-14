"""Recommendation: estimate -> compatible GPUs and counts -> fresh market listings -> tiers, picks, cost scenarios.

    recommend(req: WorkloadRequirements, who: Principal, *, limit=10, now=None) -> dict   (contract section 3)

Pipeline
    1. estimate(req): memory per GPU for 1/2/4/8 GPUs, needs_gpu, questions. needs_gpu False -> no options;
       memory unavailable -> no options (questions say what is missing; nothing is made up)
    2. hardware filter over every canonical GPU (workloads.hardware_req.check_gpu): vendor, CUDA when a CUDA
       version is required, compute capability 8.0 for bf16 / 8.9 for fp8 on NVIDIA, FP8 support elsewhere; then
       the smallest GPU count whose per-GPU need (with the safety margin) fits the GPU's VRAM
    3. market, READ-ONLY: routing.scoring.rank_listings(gpu, count=n, ...) per fitting GPU (never
       routing.engine.preview, so no audit / quote rows). It keeps only fresh, on-demand, priced, not sold-out
       listings with exactly n GPUs per instance; everything else becomes an exclusion with its code
    4. per option: memory fit, cost scenarios (price x GPUs x billed hours, billing unit via workloads.select.billing),
       execution readiness (workloads.select.provider_readiness for the caller AND a supported workload)
    5. workload value score -> tiers (lower_cost / balanced / higher_performance by theoretical specs, never
       "fastest") and picks (cheapest_market / best_for_workload / best_executable)
"""

from __future__ import annotations

import math
import statistics
from datetime import datetime, timezone

import hardware
import provider_meta
from api.common import gpu_slug
from intelligence.estimate import METHODOLOGY, estimate
from intelligence.requirements import Constraints, WorkloadRequirements
from routing import adapters, scoring
from workloads import hardware_req
from workloads import select as wsel
from workloads import templates as T

MAX_EXCLUDED = 200
HOURS = (1, 10, 100)
MIN_HEADROOM_PCT = 10.0
UNKNOWN_COSTS = ["storage", "egress", "startup / provisioning time", "idle time before and after the run"]
DROP_EXCLUSION_CODES = ("wrong_count",)     # other instance shapes of the same GPU: not a reason, just a shape
REGION_ALIASES = {"us": "US", "usa": "US", "eu": "Europe", "europe": "Europe", "uk": "UK", "ca": "Canada",
                  "canada": "Canada", "apac": "APAC", "asia": "APAC", "latam": "LATAM", "me": "Middle East",
                  "middle east": "Middle East", "africa": "Africa"}


class RecommendError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _region(v: str | None) -> str | None:
    if not v:
        return None
    import regions

    low = v.strip().lower()
    if low in REGION_ALIASES:
        return REGION_ALIASES[low]
    for g in regions.REGION_GROUPS:
        if g.lower() == low:
            return g
    raise RecommendError("unknown_region", f"unknown region {v!r}; use one of us, eu or "
                                           f"{', '.join(regions.REGION_GROUPS)}")


def _hw_requirements(req: WorkloadRequirements, prof: dict) -> dict:
    c = req.constraints
    hints = req.hardware_hints
    out = {"vendor": c.vendor if c is not None else None,
           "cuda_required": bool(hints is not None and hints.cuda_min)}
    if out["cuda_required"]:
        out["vendor"] = out["vendor"] or "NVIDIA"
    prec, quant = prof.get("precision"), prof.get("quantization")
    if prof.get("known", {}).get("precision") == "assumed":
        prec = None     # an assumed dtype never excludes hardware (fp16 runs where bf16 does not)
    if prec == "fp8" or quant == "fp8":
        out["min_compute_capability"] = 8.9
        out["needs_fp8"] = True
    elif prec == "bf16":
        out["min_compute_capability"] = 8.0
    return out


def _perf(spec: dict) -> tuple:
    """Theoretical ordering key: memory bandwidth, dense 16-bit throughput, VRAM, release year (spec sheet)."""
    return (spec.get("memory_bandwidth_tbps") or 0.0, spec.get("bf16_tflops_dense") or spec.get("fp16_tflops_dense")
            or 0.0, spec.get("vram_gb") or 0.0, spec.get("released") or 0)


def _billing_label(b: dict) -> str:
    if b["unit"] == "unknown" or b["assumed"]:
        return "unknown"
    um = b["unit_minutes"]
    if um < 1:
        return "per_second"
    if um == 1:
        return "per_minute"
    if um == 60:
        return "per_hour"
    return f"per_{um:g}_minutes"


def _cost(price_inst: float, b: dict, budget: float | None, runtime_h: float | None) -> dict:
    hours = sorted(set(HOURS) | ({runtime_h} if runtime_h else set()))
    sc = []
    for h in hours:
        billed = wsel.billed_minutes(h * 60, b) / 60
        what = (f"{h:g} hour{'s' if h != 1 else ''} of runtime" + (" (your runtime)" if h == runtime_h else "")
                + (f", billed as {billed:g} h" if abs(billed - h) > 1e-9 else ""))
        sc.append({"hours": h, "billed_hours": round(billed, 4), "total_usd": round(price_inst * billed, 2),
                   "assumption": what})
    out = {"billing_unit": _billing_label(b), "billing_basis": b["basis"], "minimum_minutes": b["minimum_minutes"],
           "price_per_instance_hour": round(price_inst, 6), "scenarios": sc,
           "budget_hours": None, "unknown_costs": list(UNKNOWN_COSTS), "kind": "estimated"}
    if b["assumed"]:
        out["unknown_costs"].append("billing granularity (per-hour billing assumed)")
    if budget:
        out["budget_hours"] = round(math.floor(budget / price_inst * 10) / 10, 1) if price_inst > 0 else None
    if runtime_h:
        out["runtime_total_usd"] = next(s["total_usd"] for s in sc if s["hours"] == runtime_h)
    return out


def _workload_supported(req: WorkloadRequirements) -> tuple[bool, str]:
    ids = [t["id"] for t in T.list_templates()]
    name = (req.model.name if req.model is not None else None) or ""
    if name.strip().lower() in ids:
        return True, f"matches the allowlisted template {name.strip().lower()}"
    return False, (f"OpenGrid can only run the allowlisted template(s) {', '.join(ids)} today; this workload is not "
                   "one of them, so it is a market recommendation only")


def _benchmarks(gpus: list[str], method: str | None) -> dict[str, int]:
    """Measured, consented observations per GPU for this workload class (none exist yet)."""
    if not gpus:
        return {}
    try:
        from sqlalchemy import func, select

        import normalize
        from store.intelligence import MEASURED_KINDS, PerformanceObservation as PO
        with normalize.SessionLocal() as s:
            rows = s.execute(select(PO.gpu, func.count()).where(PO.gpu.in_(gpus), PO.workload_class == (method or ""),
                                                                PO.source_kind.in_(MEASURED_KINDS),
                                                                PO.consent.is_(True)).group_by(PO.gpu)).all()
        return {g: int(n) for g, n in rows}
    except Exception:  # noqa: BLE001 - table missing on an old schema: same as no benchmark
        return {}


def _excl(gpu: str, code: str, reason: str, **kw) -> dict:
    return {"gpu": gpu, "gpu_slug": gpu_slug(gpu), "code": code, "reason": reason, **kw}


def recommend(req: WorkloadRequirements, who, *, limit: int = 10, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    limit = max(1, min(int(limit), 20))
    c = req.constraints or Constraints()
    region = _region(c.region)
    est = estimate(req)
    mem = est["memory"]
    prof = mem["profile"]
    notes = ["Prices are observed list prices, not quotes: a route quotes and re-validates before any launch.",
             "Runtime is not estimated (no measured benchmark); costs are scenarios at stated hours.",
             "Hardware comparisons use theoretical vendor specs (memory bandwidth, dense throughput), not benchmarks."]
    # picks_unavailable: for each pick that is null, why (so no interface shows an unexplained blank)
    out = {"estimate": est, "picks": {"cheapest_market": None, "best_for_workload": None, "best_executable": None},
           "picks_unavailable": {},
           "options": [], "excluded": [], "excluded_total": 0, "excluded_by_code": {}, "questions": est["questions"],
           "notes": notes, "as_of": now.isoformat(), "methodology": METHODOLOGY}
    if est["needs_gpu"] is False:
        out["notes"] = [f"This workload does not need a GPU: {est['needs_gpu_reason']}. No GPU options are shown; "
                        "a regular CPU server is enough."]
        out["picks_unavailable"] = {k: "this workload does not need a GPU" for k in out["picks"]}
        return out
    if mem["method"] == "unavailable":
        out["notes"] = [f"GPU memory could not be estimated ({mem.get('unavailable_reason')}), so no GPU is "
                        "recommended: answer the questions and ask again."] + notes[:1]
        out["picks_unavailable"] = {k: "GPU memory could not be estimated: answer the questions" for k in out["picks"]}
        return out
    if c.allow_spot:
        notes.append("allow_spot is set, but spot / interruptible listings are not ranked in this release: on-demand "
                     "only.")
    if region:
        notes.append(f"region {region}: listings outside it are excluded; listings with no known location are kept "
                     "and say so.")

    by_count = {int(k): v for k, v in mem["per_gpu_by_count"].items()}
    counts = sorted(by_count)
    if req.hardware_hints is not None and req.hardware_hints.gpu_count:
        hint = req.hardware_hints.gpu_count
        if hint in by_count:
            counts = [hint]
            notes.append(f"GPU count fixed at {hint} (hardware_hints.gpu_count).")
    usable = float(mem.get("usable_vram_fraction") or 1.0)
    hreq = _hw_requirements(req, prof)
    listed = wsel._listed_gpus()
    training = (mem["method"] or "").startswith("training")

    excluded: list[dict] = []
    fitting: list[tuple[str, int, dict]] = []   # (gpu, count, spec)
    unlisted_fit = 0
    for g in hardware_req.universe():
        r = hardware_req.check_gpu(g, hreq)
        spec = hardware.spec(g) or {}
        if r["eligible"] and hreq.get("needs_fp8") and spec.get("fp8_supported") is False:
            r = {**r, "eligible": False, "code": "precision_unsupported", "reason": f"{g} has no FP8 support"}
        if not r["eligible"]:
            if g in listed:
                excluded.append(_excl(g, r["code"], r["reason"]))
            continue
        vram = float(spec["vram_gb"])
        n = next((k for k in counts if by_count[k]["recommended_gb"] <= vram * usable + 1e-9), None)
        if n is None:
            if g in listed:
                k = counts[-1]
                need = by_count[k]
                excluded.append(_excl(g, "insufficient_vram",
                                      f"{vram:g} GB{' (90% usable by vLLM)' if usable < 1 else ''} < "
                                      f"{need['recommended_gb']:g} GB needed per GPU even at {k} GPU"
                                      f"{'s' if k > 1 else ''} (minimum {need['min_gb']:g} GB + "
                                      f"{mem['margin_pct']}% margin)"))
            continue
        if g not in listed:
            unlisted_fit += 1
            continue
        why_n = ""
        if n > 1:
            k0 = counts[0]
            why_n = (f"needs {n} GPUs: {k0 if k0 > 1 else 'one'} has {vram:g} GB"
                     f"{' (90% usable by vLLM)' if usable < 1 else ''} < {by_count[k0]['recommended_gb']:g} GB needed "
                     f"(minimum {by_count[k0]['min_gb']:g} GB + {mem['margin_pct']}% margin); ")
        fitting.append((g, n, spec, why_n))
    if unlisted_fit:
        notes.append(f"{unlisted_fit} other compatible GPU model(s) have no market listings at all.")

    budget, runtime_h = c.budget_usd, c.runtime_hours
    cands: list[dict] = []
    bills: dict[str, dict] = {}
    for g, n, spec, why_n in fitting:
        rk = scoring.rank_listings(g, count=n, region_group=region, max_price=c.max_price_per_gpu_hour,
                                   mode="BALANCED", exclude_providers=c.exclude_providers,
                                   include_providers=c.include_providers or None, limit=1000, now=now)
        for e in rk["exclusions"]:
            if e["code"] in DROP_EXCLUSION_CODES:
                continue
            excluded.append(_excl(g, e["code"], e["reason"], provider=e["provider"], listing_id=e["listing_id"],
                                  region=e.get("region"), price_per_gpu_hour=e.get("price_per_gpu_hour")))
        for m in rk["multi_instance_alternatives"]:
            excluded.append(_excl(g, "multi_instance", m.get("note") or "needs several instances",
                                  provider=m["provider"], listing_id=m["listing_id"], region=m.get("region"),
                                  price_per_gpu_hour=m["price_per_gpu_hour"]))
        kept = 0
        for cd in rk["candidates"]:
            price_inst = cd["price_per_gpu_hour"] * n
            b = bills.setdefault(cd["provider"], wsel.billing(cd["provider"]))
            cost = _cost(price_inst, b, budget, runtime_h)
            if budget and runtime_h and cost["runtime_total_usd"] > budget + 1e-9:
                excluded.append(_excl(g, "over_budget", f"${cost['runtime_total_usd']:.2f} for {runtime_h:g} h on "
                                                        f"{n} GPU{'s' if n > 1 else ''} is over the ${budget:.2f} budget",
                                      provider=cd["provider"], listing_id=cd["listing_id"], region=cd.get("region"),
                                      price_per_gpu_hour=cd["price_per_gpu_hour"]))
                continue
            kept += 1
            cands.append({"c": cd, "gpu": g, "n": n, "spec": spec, "cost": cost, "price_inst": price_inst,
                          "need": by_count[n]})
        if not kept and not rk["candidates"]:
            codes = sorted({e["code"] for e in rk["exclusions"] if e["code"] not in DROP_EXCLUSION_CODES})
            excluded.append(_excl(g, "no_fresh_listing",
                                  f"{why_n}no fresh on-demand {n}-GPU listing"
                                  + (f" (listings excluded: {', '.join(codes)})" if codes else "")))

    # one option per (gpu, count, provider, region): the best-scored listing
    best: dict[tuple, dict] = {}
    for x in cands:
        k = (x["gpu"], x["n"], x["c"]["provider"], x["c"]["region"])
        if k not in best or (x["c"]["score"], -x["price_inst"]) > (best[k]["c"]["score"], -best[k]["price_inst"]):
            best[k] = x
    pool = list(best.values())

    if pool:
        med = statistics.median(x["price_inst"] for x in pool)
        for x in pool:
            vram = float(x["spec"]["vram_gb"]) * usable
            need = x["need"]["recommended_gb"]
            x["headroom"] = round((vram - need) / vram * 100, 1) if vram else 0.0
            pct = (x["price_inst"] - med) / med if med else 0.0
            nvl = bool(x["spec"].get("nvlink")) or x["spec"].get("form_factor") in ("SXM", "OAM", "Superchip")
            x["nvlink"] = nvl
            penalty = 0.1 if (x["n"] > 1 and not nvl) else 0.0
            x["value"] = round(0.55 * scoring.clamp(0.5 - pct) + 0.25 * x["c"]["score"]
                               + 0.20 * scoring.clamp(x["headroom"] / 40) - penalty, 4)
        pool.sort(key=lambda x: (-x["value"], x["price_inst"], x["gpu"], x["c"]["provider"], x["c"]["listing_id"]))

    mode = None
    pr: dict[str, dict] = {}
    glob: list[dict] | None = None
    supported, sup_why = _workload_supported(req)
    bench = _benchmarks(sorted({x["gpu"] for x in pool}), mem["method"])
    ids: dict[str, int] = {}
    for x in pool:
        cd, g, n, spec = x["c"], x["gpu"], x["n"], x["spec"]
        oid = f"{gpu_slug(g)}:{cd['provider']}:{cd['region'] or 'unknown'}:{n}"
        ids[oid] = ids.get(oid, 0) + 1
        if ids[oid] > 1:
            oid += f":{ids[oid]}"
        reasons: list[dict] = []
        if not supported:
            reasons.append({"code": "workload_not_supported", "reason": sup_why})
        if getattr(who, "kind", None) == "public":
            reasons.append({"code": "sign_in_required", "reason": "execution readiness needs an OpenGrid API key or "
                                                                  "sign-in; it is never assumed for an anonymous caller"})
        else:
            if not who.has("route:execute"):
                reasons.append({"code": "scope_missing", "reason": "this API key lacks the route:execute scope "
                                                                   "needed to launch compute"})
            if mode is None:
                from routing import control
                mode = control.effective_mode()
                glob = wsel._global_blockers(mode)
            p = pr.get(cd["provider"]) or pr.setdefault(cd["provider"], wsel.provider_readiness(
                cd["provider"], who, mode, None))
            reasons += list(glob or []) + [{k: r[k] for k in ("code", "reason")} for r in p["reasons"]]
        if n > 1 and supported:
            reasons.append({"code": "multi_gpu_not_supported", "reason": "OpenGrid jobs run on one GPU in this release"})
        executable = supported and not reasons
        vram = float(spec["vram_gb"])
        why = [f"fits the {x['need']['recommended_gb']:g} GB per-GPU estimate with {x['headroom']:.0f}% headroom"
               + (f" ({n} GPUs, {x['need']['parallelism']})" if n > 1 else "")]
        lim = []
        if not bench.get(g):
            lim.append("runtime not benchmarked: no measured benchmark for this workload on this GPU")
        if cd["available"] is None:
            lim.append("availability not reported by provider")
        if x["cost"]["billing_unit"] == "unknown":
            lim.append("billing unit unknown: per-hour billing assumed in the cost scenarios")
        if n > 1 and not x["nvlink"]:
            lim.append("no NVLink / SXM interconnect on record: multi-GPU traffic goes over PCIe (slower for "
                       "sharded training and tensor parallelism)")
        if region and cd["factors"]["region_match"]["value"] == 0.5:
            lim.append(f"location not confirmed to be in {region}")
        if adapters.level(cd["provider"]) < 2:
            lim.append("market data only: OpenGrid cannot provision this provider")
        if x["headroom"] < MIN_HEADROOM_PCT:
            lim.append(f"tight fit: under {MIN_HEADROOM_PCT:.0f}% headroom over the estimate")
        x["option"] = {
            "option_id": oid, "tier": None, "gpu": g, "gpu_slug": gpu_slug(g), "vram_gb": vram,
            "form_factor": spec.get("form_factor"), "architecture": spec.get("architecture"),
            "gpu_count": n, "provider": cd["provider"], "provider_display": provider_meta.meta(cd["provider"]).display_name,
            "region": cd["region"], "listing_id": cd["listing_id"],
            "price_per_gpu_hour": round(cd["price_per_gpu_hour"], 6), "price_per_instance_hour": round(x["price_inst"], 6),
            "price_kind": "observed", "available": cd["available"], "observed_at": cd["observed_at"],
            "age_seconds": cd["age_seconds"], "fresh": True,
            "memory_fit": {"needed_per_gpu_gb": x["need"]["recommended_gb"], "min_per_gpu_gb": x["need"]["min_gb"],
                           "vram_gb": vram, "usable_vram_gb": round(vram * usable, 2), "headroom_pct": x["headroom"],
                           "fits": True},
            "cost": x["cost"],
            "theoretical": {"memory_bandwidth_tbps": spec.get("memory_bandwidth_tbps"),
                            "bf16_tflops_dense": spec.get("bf16_tflops_dense"), "nvlink": spec.get("nvlink"),
                            "released": spec.get("released"), "kind": "theoretical"},
            "value_score": x["value"], "market_score": cd["score"],
            "why": why, "limitations": lim,
            "execution": {"executable": executable, "reasons": reasons, "workload_supported": supported,
                          "route_preview_url": f"/route?gpu={gpu_slug(g)}&count={n}"},
        }

    # picks
    cheapest = min(pool, key=lambda x: (x["price_inst"], x["c"]["age_seconds"], x["gpu"], x["c"]["provider"]),
                   default=None)
    roomy = [x for x in pool if x["headroom"] >= MIN_HEADROOM_PCT]
    bfw = (roomy or pool)[0] if pool else None
    exe = next((x for x in pool if x["option"]["execution"]["executable"]), None)

    # tiers, relative to the balanced pick
    if bfw is not None:
        ref_perf, ref_price = _perf(bfw["spec"]), bfw["price_inst"]
        for x in pool:
            o = x["option"]
            if x is bfw:
                o["tier"] = "balanced"
                o["why"].append("best workload value: price against the compatible market, OpenGrid's market score "
                                "and memory headroom")
                continue
            perf = _perf(x["spec"])
            if perf[0] > ref_perf[0] * 1.1 or (perf[0] == ref_perf[0] and perf[2] > ref_perf[2]):
                o["tier"] = "higher_performance"
                bw, rbw = x["spec"].get("memory_bandwidth_tbps"), bfw["spec"].get("memory_bandwidth_tbps")
                o["why"].append(f"higher theoretical memory bandwidth ({bw:g} vs {rbw:g} TB/s, spec sheet, not a "
                                "benchmark)" if bw and rbw else f"more VRAM ({x['spec']['vram_gb']:g} vs "
                                                               f"{bfw['spec']['vram_gb']:g} GB)")
            elif x["price_inst"] < ref_price * 0.9:
                o["tier"] = "lower_cost"
                o["why"].append(f"{(1 - x['price_inst'] / ref_price):.0%} cheaper per hour than the balanced pick")
            else:
                o["tier"] = "balanced"
            if x["price_inst"] > ref_price * 1.005:
                o["why"].append(f"+{(x['price_inst'] / ref_price - 1):.0%} per hour vs the balanced pick")
    if cheapest is not None:
        cheapest["option"]["why"].append("cheapest fresh listing that fits the memory estimate")

    chosen = [x for x in pool][:limit]
    for must in (cheapest, bfw, exe):
        if must is not None and must not in chosen:
            chosen = chosen[:limit - 1] + [must] if len(chosen) >= limit else chosen + [must]
    # presented lower cost -> balanced -> higher performance, cheapest first within a tier (value order breaks ties)
    order = {id(x): i for i, x in enumerate(pool)}
    tier_rank = {"lower_cost": 0, "balanced": 1, "higher_performance": 2}
    chosen.sort(key=lambda x: (tier_rank.get(x["option"]["tier"], 1), x["price_inst"], order[id(x)]))
    out["options"] = [x["option"] for x in chosen]

    def ref(x, reason):
        return None if x is None else {"option_id": x["option"]["option_id"], "reason": reason}

    if cheapest is not None:
        out["picks"]["cheapest_market"] = ref(cheapest, f"lowest instance price that fits: "
                                                        f"${cheapest['price_inst']:.2f}/h for {cheapest['n']} x "
                                                        f"{cheapest['gpu']} on {cheapest['option']['provider_display']}")
    if bfw is not None:
        out["picks"]["best_for_workload"] = ref(bfw, f"best balance of price, market quality and memory headroom "
                                                     f"({bfw['headroom']:.0f}%) among fresh listings")
    if exe is not None:
        out["picks"]["best_executable"] = ref(exe, "first option OpenGrid can launch for you now (readiness checks "
                                                   "pass and the workload is a supported template)")
    elif pool:
        why_not = ("the workload is not a supported template" if not supported else
                   "readiness checks did not pass (see each option's execution.reasons)")
        out["picks_unavailable"]["best_executable"] = why_not
        notes.append("No option is executable through OpenGrid for this request: " + why_not
                     + ". The market recommendations above still apply.")
    if not pool:
        for k in ("cheapest_market", "best_for_workload", "best_executable"):
            out["picks_unavailable"][k] = "no fresh, on-demand listing fits this workload under the constraints"
    if not pool:
        notes.append("No fresh, on-demand listing fits this workload under the constraints; see excluded.")

    excluded.sort(key=lambda e: (e["gpu"], e.get("provider") or "", str(e.get("listing_id") or ""), e["code"]))
    by_code: dict[str, int] = {}
    for e in excluded:
        by_code[e["code"]] = by_code.get(e["code"], 0) + 1
    out["excluded"] = excluded[:MAX_EXCLUDED]
    out["excluded_total"] = len(excluded)
    out["excluded_by_code"] = by_code
    out["candidates_total"] = len(pool)
    return out
