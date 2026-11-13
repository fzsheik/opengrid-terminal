"""Why one option over another, and how sure OpenGrid is. Pure: works on the recommender's internal option records.

    confidence(x, memory) -> {"level", "reasons", "components"}       per option
    deltas(x, ref) -> [delta]                                         this option vs the recommended one, factor by factor
    explanation(bfw, alts, rule, basis) -> dict                       the top-level "why this one" block

Confidence levels (high > medium > low); an option's level is the LOWEST of its components that apply:
    memory_fit    the memory estimate's own confidence (memory.py), capped at medium for a tight fit (< 10% headroom)
    runtime       measured on 3+ fresh real OpenGrid runs: high; measured on fewer or older runs: medium;
                  theoretical (peak throughput x assumed utilization): low; not estimated: not applicable
    price         observed price freshness (the listing's freshness factor): high when fresh, else medium
    availability  stock reported by the provider: high; stock not published: medium
    reliability   OpenGrid's own launch record for the provider: reported, but only LOWERS the level when a score
                  exists and is under 80/100 (no provider has the minimum sample yet, so it says "unproven")
Every reason says what kind of evidence it is: measured, observed, estimated (theoretical) or heuristic.
"""

from __future__ import annotations

RANK = {"low": 1, "medium": 2, "high": 3}
TIGHT_PCT = 10.0
FRESH_FACTOR = 0.75
RELIABLE = 0.80


def _lvl(levels: list[str]) -> str:
    return min(levels, key=lambda v: RANK[v]) if levels else "medium"


def confidence(x: dict, memory: dict) -> dict:
    comp: dict[str, dict] = {}
    mc = memory.get("confidence") if memory.get("confidence") in RANK else "low"
    if x["headroom"] < TIGHT_PCT and RANK[mc] > RANK["medium"]:
        mc = "medium"
    n_assumed = len(memory.get("assumptions") or [])
    comp["memory_fit"] = {"level": mc, "evidence": "estimated",
                          "reason": f"memory estimate confidence {memory.get('confidence')} ({n_assumed} stated "
                                    f"assumption{'s' if n_assumed != 1 else ''}), {x['headroom']:.0f}% headroom"}
    job = x.get("job")
    if job and job.get("runtime_kind") == "measured":
        comp["runtime"] = {"level": job["measured_confidence"][0], "evidence": "measured",
                           "reason": job["measured_confidence"][1]}
    elif job:
        comp["runtime"] = {"level": "low", "evidence": "estimated",
                           "reason": "runtime is theoretical (vendor peak throughput x assumed utilization and "
                                     "scaling); no measured run of this workload on this GPU"}
    fr = ((x["c"].get("factors") or {}).get("freshness") or {}).get("value")
    age = x["c"].get("age_seconds")
    comp["price"] = {"level": "high" if (fr is None or fr >= FRESH_FACTOR) else "medium", "evidence": "observed",
                     "reason": f"observed list price, {round((age or 0) / 60)} min old"
                               + ("" if (fr is None or fr >= FRESH_FACTOR) else " (aging)")}
    av = x["c"].get("available")
    comp["availability"] = {"level": "high" if av is True else "medium", "evidence": "observed",
                            "reason": "in stock per the provider" if av is True else "stock not published by the "
                                                                                      "provider"}
    rel = (x["c"].get("factors") or {}).get("reliability") or {}
    rv = rel.get("value")
    if rv is None:
        comp["reliability"] = {"level": None, "evidence": "transaction",
                               "reason": rel.get("note") or "reliability unproven: too few OpenGrid launches"}
    else:
        comp["reliability"] = {"level": "high" if rv >= RELIABLE else "low", "evidence": "transaction",
                               "reason": rel.get("note") or f"reliability {rv * 100:.0f}/100"}
    used = [c["level"] for c in comp.values() if c["level"]]
    level = _lvl(used)
    lowest = [k for k, c in comp.items() if c["level"] == level]
    return {"level": level, "limited_by": lowest,
            "reasons": [f"{k.replace('_', ' ')}: {comp[k]['reason']}" for k in lowest],
            "components": comp}


def _cmp(a, b, lower_is_better: bool, tol: float = 0.005):
    if a is None or b is None:
        return "unknown"
    if abs(a - b) <= tol * max(1.0, abs(b)):
        return "same"
    return ("this" if a < b else "recommended") if lower_is_better else ("this" if a > b else "recommended")


def deltas(x: dict, ref: dict) -> list[dict]:
    """Factor by factor: this option vs the recommended one. `better` says which side wins on that factor."""
    out = []

    def add(factor, this, rec, better, unit=None, kind=None):
        out.append({"factor": factor, "this": this, "recommended": rec, "better": better,
                    **({"unit": unit} if unit else {}), **({"kind": kind} if kind else {})})
    jx, jr = x.get("job"), ref.get("job")
    if jx and jr:
        add("estimated_job_cost_usd", jx["usd"], jr["usd"], _cmp(jx["usd"], jr["usd"], True), "USD",
            "estimated" if "measured" not in (jx["runtime_kind"], jr["runtime_kind"]) else "estimated_from_measured")
        add("estimated_completion_hours", jx["hours"], jr["hours"], _cmp(jx["hours"], jr["hours"], True), "h",
            jx["runtime_kind"] if jx["runtime_kind"] == jr["runtime_kind"] else "mixed")
    add("price_per_instance_hour", round(x["price_inst"], 4), round(ref["price_inst"], 4),
        _cmp(x["price_inst"], ref["price_inst"], True), "USD/h", "observed")
    add("memory_headroom_pct", x["headroom"], ref["headroom"],
        "same" if min(x["headroom"], ref["headroom"]) >= TIGHT_PCT else _cmp(x["headroom"], ref["headroom"], False, 0.02),
        "%", "estimated")
    ex = (x["perf"].get("effective") or {}).get("value")
    er = (ref["perf"].get("effective") or {}).get("value")
    if ex and er and x["perf"]["basis"] and ref["perf"]["basis"] and x["perf"]["basis"]["unit"] == ref["perf"]["basis"]["unit"]:
        add("effective_throughput", ex, er, _cmp(ex, er, False, 0.05), x["perf"]["basis"]["unit"], "estimated")
    ix, ir = x["perf"].get("inference") or {}, ref["perf"].get("inference") or {}
    if ix.get("aggregate_tokens_per_s") and ir.get("aggregate_tokens_per_s"):
        add("decode_tokens_per_s", ix["aggregate_tokens_per_s"], ir["aggregate_tokens_per_s"],
            _cmp(ix["aggregate_tokens_per_s"], ir["aggregate_tokens_per_s"], False, 0.05), "tokens/s", "estimated")
    ax, ar = x["c"].get("available"), ref["c"].get("available")
    add("in_stock_reported", ax, ar, "same" if ax == ar else ("this" if ax is True else "recommended"), None,
        "observed")
    add("listing_score", x["c"]["score"], ref["c"]["score"], _cmp(x["c"]["score"], ref["c"]["score"], False, 0.02),
        None, "inferred")
    lx = (x["perf"].get("interconnect") or {}).get("kind")
    lr = (ref["perf"].get("interconnect") or {}).get("kind")
    if x["n"] > 1 or ref["n"] > 1:
        order = {"single_gpu": 3, "nvlink": 2, "nvlink_bridge": 1, "pcie": 0}
        add("gpu_link", lx, lr, "same" if order.get(lx) == order.get(lr) else
            ("this" if order.get(lx, 0) > order.get(lr, 0) else "recommended"), None, "estimated")
    ex_, er_ = x["option"]["execution"]["executable"], ref["option"]["execution"]["executable"]
    add("executable_via_opengrid", ex_, er_, "same" if ex_ == er_ else ("this" if ex_ else "recommended"))
    return out


def label(x: dict) -> str:
    return f"{x['n']} x {x['gpu']} on {x['option']['provider_display']}"


def explanation(bfw: dict | None, alts: list[dict], rule: str | None, basis: str | None, *, top: int = 3) -> dict | None:
    """The recommended option, the rule and basis that chose it, its confidence, and for the next `top`
    alternatives the factors on which the recommended option wins."""
    if bfw is None:
        return None
    o = bfw["option"]
    alt_out = []
    for x in alts[:top]:
        d = x["option"].get("decision", {}).get("vs_recommended") or []
        wins = [e["factor"] for e in d if e["better"] == "recommended"]
        loses = [e["factor"] for e in d if e["better"] == "this"]
        alt_out.append({"option_id": x["option"]["option_id"], "label": label(x), "tier": x["option"]["tier"],
                        "summary": x["option"].get("summary"), "recommended_wins_on": wins,
                        "this_wins_on": loses, "confidence": x["option"]["decision"]["confidence"]["level"]})
    return {"recommended_option_id": o["option_id"], "label": label(bfw),
            "rule": rule, "rank_basis": "whole_job_cost" if basis == "job" else "hourly_price",
            "why": list(o.get("why") or [])[:3],
            "confidence": o["decision"]["confidence"],
            "alternatives": alt_out,
            "kind": "mixed",
            "note": "prices observed; memory, runtime and throughput estimated (or measured where labelled); the "
                    "confidence level is the weakest evidence behind the option"}
