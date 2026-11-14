"""Scale-out: N independent single-GPU replicas for embarrassingly parallel work. Pure arithmetic over the recommender's
internal option records and the listings it already read: no database write, no provider call.

    llm_batch_work(req, prof) -> dict                  total output (and input) tokens of a text LLM batch, when stated
    llm_batch_runtime(inf, n, spec, work, prof, params_b, link_kind) -> dict | None   one option's theoretical runtime
    eligibility(...) -> (bool, reason, kind)           whether scale-out is offered at all, and why not
    wall(T1, n) -> (central, fast, slow) hours         completion time of n replicas
    replicas_for(T1, target_h) -> int | None           the smallest n whose central completion meets target_h
    capacity_cap(units_by_provider, max_n) -> dict     the largest n the listed capacity + diversification cap allow
    allocate(listings, n) -> list | None               cheapest-first placement of n replicas under the cap
    build(...) -> dict                                 the recommendation's `scale_out` block

When it is offered (methodology/recommendations.md, "Scale-out"): an INFERENCE workload with a total amount of
independent work (media batch: media.total_audio_hours / total_items; text LLM batch: inference.total_requests x
output_tokens or inference.total_output_tokens), no latency target, GPU count not fixed, and per card: the model fits
ONE GPU of that card. Never for training (one run's gradients are combined every step), a model that needs several
GPUs, or latency-bound serving (replicas do not shorten one request).

Formulas (every figure ESTIMATED; slots: central, fast end, slow end):
    T1          the work on ONE GPU of the card: the theoretical runtime of that card's one-GPU option (or its MEASURED
                runtime when a consented OpenGrid measurement exists: measured first)
    busy(n)     setup + sharding + T1 x (1 + straggler) / (n x replica efficiency)
    cost(n)     sum over the n placed replicas of price_i x busy(n), each billed in its provider's billing unit
    sizing      deadline_hours given: the smallest feasible n whose central busy(n) <= deadline; otherwise the smallest n
                that finishes no later than the shortest single-machine option (central), at least 2; both capped by
                the listed capacity under the batch diversification cap and by BATCH_MAX_SHARDS (and max_gpus)
    capacity    per listing its reported capacity (GPUs, or instances x 1 GPU), else 1 (the listing itself);
                feasible(n) = sum over providers of min(cap(n), units_p) >= n, cap(n) = max(1, floor(0.5 n)): the
                default placement.max_share_per_provider of /v1/batches
"""

from __future__ import annotations

import math

SETUP_H = (0.25, 0.10, 0.50)        # per replica, in parallel: provision + image pull + model download and load
SHARD_H = (0.05, 0.02, 0.15)        # split the inputs into shards, merge the outputs
STRAGGLER = (0.10, 0.05, 0.25)      # marketplace capacity: the slowest replica, interrupted shards redone
REPLICA_EFF = (0.98, 1.00, 0.90)    # shard imbalance (central, fast, slow); intelligence/media.REPLICA_EFF re-ordered
DEFAULT_SHARE = 0.5                 # /v1/batches placement.max_share_per_provider default
MAX_CARDS = 3                       # scale-out options shown: the best-value card types, one option each
NEAR = 1.5                          # no deadline: "in reach" = finishes within 1.5x the fastest single machine
MIN_REPLICAS = 2
METHODOLOGY = "/methodology/recommendations#scale-out"
NOT_EVALUATED = "not evaluated: no GPU options for this request"


# ------------------------------------------------------------------------------------------ LLM batch work

def llm_batch_work(req, prof: dict) -> dict:
    """The total work of a text LLM batch. {"known": False, "reason"} unless the request states it."""
    inf = getattr(req, "inference", None)
    md = prof.get("media") or {}
    if prof.get("method") != "inference" or md.get("media_kind") or md.get("kind"):
        return {"known": False, "reason": "not a text LLM inference workload"}
    if inf is None or not (inf.total_output_tokens or inf.total_requests):
        return {"known": False, "reason": "the total amount of work is unknown (give inference.total_requests with "
                                          "output_tokens, or inference.total_output_tokens)"}
    A: list[str] = []
    if inf.total_output_tokens:
        out_tok = float(inf.total_output_tokens)
        basis = f"{out_tok:,.0f} output tokens (inference.total_output_tokens)"
    elif inf.output_tokens:
        out_tok = float(inf.total_requests) * inf.output_tokens
        basis = f"{inf.total_requests:,} requests x {inf.output_tokens:,} output tokens"
    else:
        return {"known": False, "reason": "inference.total_requests is given but not output_tokens per request"}
    in_tok = None
    if inf.total_requests and inf.context_len and inf.output_tokens and inf.context_len > inf.output_tokens:
        in_tok = float(inf.total_requests) * (inf.context_len - inf.output_tokens)
        basis += f"; prefill {inf.total_requests:,} x {inf.context_len - inf.output_tokens:,} prompt tokens"
    else:
        A.append("prompt (prefill) tokens unknown: prefill compute not included (give total_requests, context_len "
                 "and output_tokens)")
    return {"known": True, "kind": "estimated", "output_tokens": out_tok, "input_tokens": in_tok, "basis": basis,
            "requests": inf.total_requests, "assumptions": A, "unit": {"name": "output tokens"}}


def llm_batch_runtime(inf: dict | None, n: int, spec: dict, work: dict, prof: dict, params_b: float | None,
                      scaling: dict) -> dict | None:
    """Theoretical runtime of a text LLM batch on one option: output tokens / aggregate decode tokens/s (the
    memory-bandwidth roofline, intelligence/workload_fit.inference_throughput) + prefill compute
    (2 x active parameters x prompt tokens at the GPU's dense 16-bit tensor peak x the central utilization)."""
    from intelligence import performance as P

    if not inf or not inf.get("aggregate_tokens_per_s") or not work.get("known"):
        return None
    agg = [inf["aggregate_tokens_per_s"], inf["aggregate_range"][1], inf["aggregate_range"][0]]   # central, fast, slow
    if not all(a and a > 0 for a in agg):
        return None
    dec = [work["output_tokens"] / a / 3600 for a in agg]
    pre = [0.0, 0.0, 0.0]
    A = list(work.get("assumptions") or [])
    if work.get("input_tokens"):
        b = P.throughput_basis("training_full", prof.get("precision"), spec)
        act = ((prof.get("model") or {}).get("active_parameters_b")) or params_b
        if b and act:
            u = P.utilization(params_b)
            sp = [scaling["speedup"], scaling["speedup_range"][1], scaling["speedup_range"][0]]
            uu = [u[0], u[2], u[1]]
            pre = [2 * act * 1e9 * work["input_tokens"] / (b["per_gpu"] * 1e12 * sp[j] * uu[j]) / 3600 for j in range(3)]
            A.append(f"prefill: 2 x {act:g}B parameters x {work['input_tokens']:,.0f} prompt tokens at "
                     f"{b['per_gpu']:g} TFLOPS ({b['label'].split(' (')[0]}) x {u[0]:.0%} utilization")
        else:
            A.append("prefill not included: no dense tensor throughput on record for this GPU")
    h = [dec[j] + pre[j] for j in range(3)]
    return {"known": True, "kind": "estimated", "hours": round(h[0], 4), "range_hours": [round(h[1], 4), round(h[2], 4)],
            "basis": (f"{work['basis']}: {work['output_tokens']:,.0f} output tokens / {agg[0]:,.0f} tokens/s "
                      f"(theoretical decode roofline at concurrency {inf.get('concurrency')}, {n} GPU(s))"
                      + (" + prefill compute" if any(pre) else "") + ". Theoretical, not a benchmark"),
            "assumptions": A + ["the server is kept full (every concurrent slot busy) for the whole batch",
                                "excludes startup, model download and writing results"]}


# ------------------------------------------------------------------------------------------ eligibility

def eligibility(*, method: str | None, media_batch: bool, llm_known: bool, latency_target: bool, fixed: bool,
                has_pool: bool, training_reason: str | None = None) -> tuple[bool, str | None, str | None]:
    if not has_pool:
        return False, NOT_EVALUATED, None
    if method is None or str(method).startswith("training"):
        return False, ("a training run is one job: its GPUs combine gradients every step, so it cannot be split into "
                       "independent replicas (see the multi-GPU options instead)"), None
    if method != "inference":
        return False, "not an inference or batch workload", None
    if latency_target:
        return False, ("latency-bound serving (target_latency_ms): independent replicas add throughput, they do not "
                       "make one request faster"), None
    if fixed:
        return False, "hardware_hints.gpu_count fixes the instance shape", None
    if media_batch:
        return True, None, "media_batch"
    if llm_known:
        return True, None, "llm_batch"
    return False, ("no total amount of work: scale-out sizes replicas to a batch (media.total_audio_hours / "
                   "media.total_items, or inference.total_requests / total_output_tokens); open-ended serving is "
                   "not a batch"), None


# ------------------------------------------------------------------------------------------ sizing

def wall(T1: tuple, n: int) -> tuple:
    """Completion hours of n replicas (central, fast, slow) for single-GPU work T1 (central, fast, slow)."""
    return tuple(SETUP_H[j] + SHARD_H[j] + T1[j] * (1 + STRAGGLER[j]) / (n * REPLICA_EFF[j]) for j in range(3))


def replicas_for(T1: tuple, target_h: float) -> int | None:
    """Smallest n whose CENTRAL completion is <= target_h; None when the fixed overhead alone exceeds it."""
    a = SETUP_H[0] + SHARD_H[0]
    b = T1[0] * (1 + STRAGGLER[0]) / REPLICA_EFF[0]
    if target_h <= a + 1e-12:
        return None
    return max(1, math.ceil(b / (target_h - a) - 1e-9))


def provider_cap(n: int) -> int:
    return max(1, math.floor(DEFAULT_SHARE * n + 1e-9))


def feasible(units: dict, n: int) -> bool:
    c = provider_cap(n)
    return sum(min(c, u) for u in units.values()) >= n


def capacity_cap(units: dict, max_n: int) -> dict:
    """The largest n in [1, max_n] the listed capacity allows under the diversification cap, and the feasible set."""
    ok = [n for n in range(1, max_n + 1) if feasible(units, n)]
    return {"max": max(ok) if ok else 0, "feasible": ok, "listed_units": sum(units.values()),
            "providers": len(units)}


def allocate(listings: list[dict], n: int) -> list[dict] | None:
    """Cheapest-first placement of n replicas: each listing holds up to its units, each provider up to cap(n)."""
    cap = provider_cap(n)
    used: dict[str, int] = {}
    out = []
    for li in sorted(listings, key=lambda x: (x["price"], x["provider"], x["listing_id"])):
        room = min(li["units"], cap - used.get(li["provider"], 0), n - len(out))
        if room <= 0:
            continue
        out += [li] * room
        used[li["provider"]] = used.get(li["provider"], 0) + room
        if len(out) >= n:
            return out
    return None


def choose_n(T1: tuple, units: dict, *, max_n: int, deadline: float | None, target_h: float | None) -> dict:
    cc = capacity_cap(units, max_n)
    if cc["max"] < MIN_REPLICAS:
        return {"n": None, "reason": (f"listed capacity allows {cc['max']} replica(s) under the {DEFAULT_SHARE:.0%} "
                                      f"per-provider diversification cap ({cc['providers']} provider(s), "
                                      f"{cc['listed_units']} listed GPU(s))"), "capacity": cc}
    goal = deadline if deadline else target_h
    rule = "deadline" if deadline else ("match_fastest_single_node" if target_h else "capacity")
    need = replicas_for(T1, goal) if goal else None
    capped = False
    if need is None:
        n = cc["max"]
        capped = bool(goal)
    else:
        need = max(MIN_REPLICAS, need)
        n = next((k for k in cc["feasible"] if k >= need), None)
        if n is None:
            n, capped = cc["max"], True
    return {"n": n, "needed": need, "rule": rule, "target_hours": goal, "capped_by_capacity": capped, "capacity": cc}


# ------------------------------------------------------------------------------------------ the block

def _money(v: float) -> str:
    return f"${v:,.2f}"


def _h(v: float) -> str:
    return f"{v:.3g} h"


def _short(gpu: str) -> str:
    return gpu.replace("NVIDIA ", "").replace("AMD ", "")


def _slug(s: str) -> str:
    import re
    t = re.sub(r"[^a-z0-9._-]+", "-", s.lower()).strip("-.") or "batch"
    return t[:47]


def _deadline_state(w: tuple, deadline: float | None) -> str | None:
    if not deadline:
        return None
    if w[2] <= deadline + 1e-9:
        return "yes"
    if w[0] <= deadline + 1e-9:
        return "likely"
    return "no"


def _hint(gpu: str, n: int, alloc: list, w: tuple, cost_slow: float, template: str | None, supported: bool,
          req_name: str | None) -> dict:
    max_price = math.ceil(max(li["price"] for li in alloc) * 100) / 100
    budget = math.ceil(cost_slow * 1.10 * 100) / 100
    provs = sorted({li["provider"] for li in alloc})
    body = {"name": _slug((req_name or _short(gpu)) + "-scale-out"), "job_type": "batch_compute",
            "template": template if supported and template else "<allowlisted-template-id>",
            "resources": {"gpu": gpu, "gpu_count": 1},
            "shards": [{"config": {"shard_index": i, "shard_count": n}} for i in range(n)],
            "limits": {"max_price_per_gpu_hour_usd": max_price, "max_runtime_minutes": math.ceil(w[2] * 60)},
            "budget": {"max_total_usd": min(budget, 10000.0)},
            "placement": {"max_share_per_provider": DEFAULT_SHARE, "include_providers": provs},
            "concurrency": {"max_running": n},
            "policy": {"on_failure": "continue", "max_attempts": 2}}
    note = ("POST this to /v1/batches/preview to see the placement plan (no spend). "
            + ("" if supported else "Batches run allowlisted templates only today and this workload is not one, so "
                                    "the preview will refuse it (unknown_template) until a template for it exists; "
                                    "the body shows the shape. ")
            + "Each shard's config tells your code which share of the items it processes; the budget is the slow-end "
              "estimate + 10% for relaunches" + (" (capped at the $10,000 batch maximum)" if budget > 10000 else ""))
    return {"endpoint": "POST /v1/batches/preview", "runnable_today": bool(supported and template), "body": body,
            "note": note}


def build(*, kind: str, cards: list[dict], deadline: float | None, max_n: int, singles: list[dict],
          billed, template: str | None, supported: bool, req_name: str | None, budget: float | None,
          work_label: str) -> dict:
    """cards: [{gpu, spec, T1 (central, fast, slow), runtime_kind, measured_confidence, listings: [{provider,
    provider_display, listing_id, price, units, units_reported, available, visibility, bill}]}]
    singles: visible single-machine options with a job estimate [{option_id, gpu, n, provider_display, hours,
    range_hours, usd, range_usd}]"""
    lowest = min(singles, key=lambda s: (s["usd"], s["hours"]), default=None)
    quickest = min(singles, key=lambda s: (s["hours"], s["usd"]), default=None)
    target_h = quickest["hours"] if quickest else None
    opts, skipped = [], []
    for cd in cards:
        units: dict[str, int] = {}
        for li in cd["listings"]:
            units[li["provider"]] = units.get(li["provider"], 0) + li["units"]
        ch = choose_n(cd["T1"], units, max_n=max_n, deadline=deadline, target_h=target_h)
        if not ch["n"]:
            skipped.append({"gpu": cd["gpu"], "reason": ch["reason"]})
            continue
        n = ch["n"]
        alloc = allocate(cd["listings"], n)
        if alloc is None:       # cannot happen when feasible(); defensive
            skipped.append({"gpu": cd["gpu"], "reason": "no placement under the diversification cap"})
            continue
        w = wall(cd["T1"], n)
        cost = [round(sum(li["price"] * billed(li, w[j]) for li in alloc), 2) for j in range(3)]
        per_hour = round(sum(li["price"] for li in alloc), 4)
        by_prov: dict[str, dict] = {}
        for li in alloc:
            p = by_prov.setdefault(li["provider"], {"provider": li["provider"], "provider_display": li["provider_display"],
                                                    "replicas": 0, "prices": [], "visibility": li["visibility"]})
            p["replicas"] += 1
            p["prices"].append(li["price"])
            if li["visibility"] != "public":
                p["visibility"] = li["visibility"]
        provs = []
        for p in sorted(by_prov.values(), key=lambda p: (-p["replicas"], p["provider"])):
            listed = sum(li["units"] for li in cd["listings"] if li["provider"] == p["provider"])
            unrep = any(not li["units_reported"] for li in cd["listings"] if li["provider"] == p["provider"])
            provs.append({"provider": p["provider"], "provider_display": p["provider_display"],
                          "replicas": p["replicas"], "price_per_gpu_hour_range": [min(p["prices"]), max(p["prices"])],
                          "listed_gpus": listed, "listed_gpus_is_lower_bound": unrep,
                          "visibility": p["visibility"]})
        prices = [li["price"] for li in alloc]
        vis = "internal" if any(li["visibility"] != "public" for li in alloc) else "public"
        conf_lvl = cd.get("measured_confidence") or "low"
        conf = {"level": conf_lvl,
                "reasons": [("runtime measured on real OpenGrid runs of this workload" if cd["runtime_kind"] == "measured"
                             else "runtime is theoretical (vendor peak throughput x assumed utilization)"),
                            "replica overheads and the straggler allowance are planning assumptions",
                            "marketplace capacity can disappear between now and launch"]}
        meets = _deadline_state(w, deadline)
        lim = []
        if ch["capped_by_capacity"]:
            lim.append(f"capped at {n} replicas by listed capacity / the diversification cap / the shard limit: "
                       + (f"{ch['needed']} would be needed" if ch["needed"] else "the fixed overhead alone exceeds the target"))
        if meets == "no":
            lim.append(f"estimated {_h(w[0])} to finish, past your {deadline:g} h deadline")
        if budget and cost[0] > budget + 1e-9:
            lim.append(f"estimated {_money(cost[0])} is over your {_money(budget)} budget")
        if any(p["listed_gpus_is_lower_bound"] for p in provs):
            lim.append("some providers do not report capacity: each such listing is counted as ONE GPU")
        if not supported:
            lim.append("OpenGrid batches run allowlisted templates only: this is a market plan you run yourself today")
        sid = f"scale-out:{_slug(_short(cd['gpu']))}:{n}"
        opt = {"scale_out_id": sid, "gpu": cd["gpu"], "gpu_count_per_replica": 1, "replicas": n,
               "vram_gb": cd["spec"].get("vram_gb"), "architecture": cd["spec"].get("architecture"),
               "providers": provs, "providers_count": len(provs),
               "price_per_hour_total": per_hour,
               "price_per_gpu_hour": {"min": min(prices), "max": max(prices), "mean": round(per_hour / n, 4)},
               "price_kind": "observed",
               "completion_hours": round(w[0], 3), "completion_range_hours": [round(w[1], 3), round(w[2], 3)],
               "job_cost_usd": cost[0], "job_cost_range_usd": [cost[1], cost[2]],
               "single_gpu_hours": {"central": round(cd["T1"][0], 3), "range": [round(cd["T1"][1], 3),
                                                                                  round(cd["T1"][2], 3)],
                                    "kind": cd["runtime_kind"]},
               "runtime_kind": cd["runtime_kind"], "kind": "estimated",
               "overheads": {
                   "replica_setup_hours": {"central": SETUP_H[0], "range": [SETUP_H[1], SETUP_H[2]],
                                           "what": "per replica, in parallel: provisioning, image pull, model "
                                                   "download and load (assumption)"},
                   "data_sharding_hours": {"central": SHARD_H[0], "range": [SHARD_H[1], SHARD_H[2]],
                                           "what": "split inputs into shards and merge outputs (assumption)"},
                   "straggler_allowance": {"central": STRAGGLER[0], "range": [STRAGGLER[1], STRAGGLER[2]],
                                           "what": "extra work time for the slowest replica and interrupted shards "
                                                   "redone on marketplace capacity (assumption)"},
                   "replica_efficiency": {"central": REPLICA_EFF[0], "range": [REPLICA_EFF[2], REPLICA_EFF[1]],
                                          "what": "shard imbalance (assumption)"}},
               "sizing": {"rule": ch["rule"], "target_hours": None if ch["target_hours"] is None else
                          round(ch["target_hours"], 3), "replicas_needed": ch["needed"],
                          "capped_by_capacity": ch["capped_by_capacity"],
                          "max_replicas_listed": ch["capacity"]["max"], "listed_gpus": ch["capacity"]["listed_units"],
                          "providers_listing": ch["capacity"]["providers"], "max_shards": max_n,
                          "diversification_cap": f"at most {provider_cap(n)} replica(s) per provider "
                                                 f"(placement.max_share_per_provider {DEFAULT_SHARE:g})"},
               "meets_deadline": meets, "confidence": conf, "visibility": vis,
               "limitations": lim,
               "batch_hint": _hint(cd["gpu"], n, alloc, w, cost[2], template, supported, req_name),
               "basis": (f"{n} x one {_short(cd['gpu'])} each on 1/{n} of the {work_label}: setup {SETUP_H[0]:g} h + "
                         f"sharding {SHARD_H[0]:g} h + {cd['T1'][0]:.3g} h of single-GPU work x {1 + STRAGGLER[0]:g} "
                         f"(straggler allowance) / ({n} x {REPLICA_EFF[0]:.0%}); cost = sum of the {n} replicas' "
                         "observed prices x that time, billed per provider. Theoretical, not a benchmark")}
        opts.append(opt)
    # in reach first (meets the deadline; without one, finishes within NEAR x the fastest single machine), cheapest
    # first; then the plans that cannot get there, soonest first. The best scale-out is the first one.
    for o in opts:
        goal = deadline or target_h
        o["sizing"]["in_reach"] = (o["meets_deadline"] in ("yes", "likely") if deadline else
                                   True if not goal else o["completion_hours"] <= goal * NEAR + 1e-9)
    opts.sort(key=lambda o: (0, o["job_cost_usd"], o["completion_hours"], o["gpu"]) if o["sizing"]["in_reach"] else
              (1, o["completion_hours"], o["job_cost_usd"], o["gpu"]))
    opts = opts[:MAX_CARDS]
    best = opts[0] if opts else None
    out = {"offered": bool(opts), "kind": "estimated", "workload": kind, "options": opts, "skipped": skipped,
           "picks": {"best_scale_out": None}, "comparison": None, "methodology": METHODOLOGY,
           "reason": None if opts else ("no card type that fits one GPU has enough listed capacity across providers "
                                        "for 2+ replicas" if skipped else "no single-GPU option has an estimable "
                                                                          "runtime"),
           "assumptions": [f"replica setup {SETUP_H[0]:g} h (range {SETUP_H[1]:g}-{SETUP_H[2]:g}), data sharding "
                           f"{SHARD_H[0]:g} h (range {SHARD_H[1]:g}-{SHARD_H[2]:g}), straggler / interruption "
                           f"allowance {STRAGGLER[0]:.0%} (range {STRAGGLER[1]:.0%}-{STRAGGLER[2]:.0%}), replica "
                           f"efficiency {REPLICA_EFF[0]:.0%} (range {REPLICA_EFF[2]:.0%}-{REPLICA_EFF[1]:.0%}): "
                           "OpenGrid planning assumptions, not measurements",
                           "each replica is billed for the whole completion time (replicas that finish early can be "
                           "stopped sooner, so the cost is an upper estimate on that count)",
                           f"capacity: a listing's reported capacity, else 1 GPU; at most "
                           f"{DEFAULT_SHARE:.0%} of the replicas per provider (the /v1/batches default) and at most "
                           f"{max_n} shards per batch"]}
    if best is not None:
        out["picks"]["best_scale_out"] = {
            "scale_out_id": best["scale_out_id"],
            "reason": (("lowest estimated whole-job cost among scale-out plans that "
                        + (f"meet the {deadline:g} h deadline" if deadline else
                           f"finish within {NEAR:g}x the fastest single machine")
                        if best["sizing"]["in_reach"] else "no scale-out plan gets there on listed capacity; the soonest")
                       + f": {best['replicas']} x {_short(best['gpu'])} across {best['providers_count']} provider(s), "
                         f"about {_h(best['completion_hours'])} and {_money(best['job_cost_usd'])} (theoretical)")}
        out["comparison"] = comparison(best, lowest, quickest)
    return out


def _single_text(s: dict, role: str) -> str:
    return (f"{s['n']} x {_short(s['gpu'])} on {s['provider_display']} ({role}): ~{_h(s['hours'])}, "
            f"~{_money(s['usd'])}")


def comparison(best: dict, lowest: dict | None, quickest: dict | None) -> dict:
    so = (f"{best['replicas']} x {_short(best['gpu'])} across {best['providers_count']} provider(s): "
          f"~{_h(best['completion_hours'])}, ~{_money(best['job_cost_usd'])}")
    parts, refs = [], []
    if lowest:
        parts.append(_single_text(lowest, "lowest-cost single machine"))
        refs.append({"role": "lowest_cost_single_machine", "option_id": lowest["option_id"], "hours": lowest["hours"],
                     "usd": lowest["usd"]})
    if quickest and quickest is not lowest and quickest["option_id"] != (lowest or {}).get("option_id"):
        parts.append(_single_text(quickest, "fastest single machine"))
        refs.append({"role": "shortest_single_machine", "option_id": quickest["option_id"],
                     "hours": quickest["hours"], "usd": quickest["usd"]})
    text = so + (" vs " + "; vs ".join(parts) if parts else " (no single-machine option has a runtime estimate)")
    verdicts = []
    for ref, role in ((lowest, "the lowest-cost single machine"), (quickest, "the fastest single machine")):
        if not ref or (role.endswith("fastest single machine") and ref is lowest):
            continue
        ratio = ref["hours"] / best["completion_hours"] if best["completion_hours"] > 0 else None
        when = ("" if ratio is None else f"about {ratio:.2g}x sooner" if ratio >= 1.05 else
                f"about {1 / ratio:.2g}x longer" if ratio <= 1 / 1.05 else "about as long")
        dc = best["job_cost_usd"] - ref["usd"]
        cost = f"{_money(-dc)} cheaper" if dc < -0.005 else f"{_money(dc)} more" if dc > 0.005 else "the same cost"
        verdicts.append(f"{when} and {cost} than {role}")
    if verdicts:
        text += ". Scale-out: " + "; ".join(verdicts) + " (theoretical estimates)."
    return {"text": text, "scale_out_id": best["scale_out_id"], "single_machine": refs, "verdicts": verdicts,
            "kind": "estimated"}
