"""The OpenGrid Assistant engine: respond(message, context, who) -> the POST /v1/assistant payload (contract §5).

    {"reply": str, "cards": [{"type", "data"}], "context": {"requirements", "pending", "asked"},
     "engine": "deterministic" | "llm:<model>", "grounded": bool, "intent": str, "llm": {...}}

Flow
    1. validate + bound the echoed context (stateless server: the client sends back what we returned)
    2. intents.parse(message) (deterministic); only when it is unsure AND an LLM is configured, the LLM may
       classify -- its output is re-validated against the message (llm.py)
    3. ask only the questions that block a useful answer (workload type, model size, fine-tuning method);
       everything else is answered with what is known, and the engine's own follow-up questions are attached
    4. call read-only tools (tools.py; jobtools.py for the caller's own jobs) -> cards; compose `reply` from the
       cards only. Job actions (start_demo, cancel_job) are ACTION cards: the browser performs them after the user
       clicks and confirms; this module never creates, approves, cancels or launches anything
    5. optionally let the LLM rephrase; grounding.check rejects any number / link / claimed action not backed by
       the cards, and the deterministic reply is used instead
Never raises for a data problem: a failed tool becomes an honest "unavailable" reply (no 503).
"""

from __future__ import annotations

import copy
import json
import logging
import re

from fastapi.encoders import jsonable_encoder

from assistant import grounding, intents, jobtools, tools
from assistant import llm as llm_mod

log = logging.getLogger("opengrid.assistant")

MAX_MESSAGE = 2000
MAX_CONTEXT_BYTES = 8192
MAX_DEPTH = 6
CONTEXT_KEYS = {"requirements", "pending", "asked", "job_id"}
PENDING = (None, "recommend", "can_run", "cancel")
REQ_KEYS = {"workload_type", "frameworks", "model", "precision", "quantization", "training", "inference",
            "constraints", "hardware_hints", "evidence", "source"}
POPULAR = ["H100", "H200", "A100", "L40S", "RTX 4090"]
PICKS = (("cheapest_market", "Cheapest that fits"), ("best_for_workload", "Best for this workload"),
         ("best_executable", "Launchable through OpenGrid"))


class ContextError(ValueError):
    """The echoed context is malformed or too large (-> 422)."""


# ------------------------------------------------------------------ context

def _depth(x, d=0) -> int:
    if isinstance(x, dict):
        return max([_depth(v, d + 1) for v in x.values()] or [d + 1])
    if isinstance(x, list):
        return max([_depth(v, d + 1) for v in x] or [d + 1])
    return d


def sanitize_context(context) -> dict:
    """The client's echo of our previous `context`, validated. Raises ContextError."""
    if context in (None, {}):
        return {"requirements": {}, "pending": None, "asked": [], "job_id": None}
    if not isinstance(context, dict):
        raise ContextError("context must be an object")
    raw = json.dumps(context, separators=(",", ":"), default=str)
    if len(raw.encode()) > MAX_CONTEXT_BYTES:
        raise ContextError(f"context is larger than {MAX_CONTEXT_BYTES} bytes")
    if _depth(context) > MAX_DEPTH:
        raise ContextError(f"context is nested deeper than {MAX_DEPTH} levels")
    extra = set(context) - CONTEXT_KEYS
    if extra:
        raise ContextError(f"unknown context keys: {sorted(extra)}")
    req = context.get("requirements") or {}
    if not isinstance(req, dict):
        raise ContextError("context.requirements must be an object")
    bad = set(req) - REQ_KEYS
    if bad:
        raise ContextError(f"context.requirements has unknown fields: {sorted(bad)}")
    try:
        tools.requirements(req)  # the contract model (extra="forbid") when the engine is installed
    except tools.ToolUnavailable:
        pass
    except ValueError as e:
        raise ContextError(f"context.requirements is not valid WorkloadRequirements: {str(e)[:300]}")
    pending = context.get("pending")
    if pending not in PENDING:
        raise ContextError("context.pending must be null, 'recommend', 'can_run' or 'cancel'")
    asked = context.get("asked") or []
    if not isinstance(asked, list) or len(asked) > 12 or not all(isinstance(a, str) and len(a) <= 64 for a in asked):
        raise ContextError("context.asked must be a short list of field names")
    # the last job referred to ("cancel it" next turn): only a well-formed job id survives, anything else is
    # dropped. It is only a hint: every use re-reads the job through the caller's own ownership check.
    job_id = context.get("job_id")
    if not jobtools.valid_id(job_id):
        job_id = None
    return {"requirements": copy.deepcopy(req), "pending": pending, "asked": list(asked), "job_id": job_id}


def _merge(base: dict, new: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in new.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _get(d: dict, path: str):
    for k in path.split("."):
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


# ------------------------------------------------------------------ formatting (numbers only from data)

def _money(v) -> str:
    if v is None:
        return "n/a"
    v = float(v)
    return f"${v:,.2f}" if v >= 0.1 else f"${v:.4f}"


def _num(v) -> str:
    if v is None:
        return "n/a"
    v = float(v)
    return str(int(v)) if v.is_integer() else f"{v:g}"


def _short(name: str | None) -> str:
    return re.sub(r"^(NVIDIA|AMD Instinct|AMD|Intel)\s+", "", name or "")


def _providers(n) -> str:
    return f"{n} provider" + ("" if n == 1 else "s")


def _link(label: str, href: str) -> dict:
    return {"type": "link", "data": {"label": label, "href": href}}


def _question(field: str, question: str, why: str | None = None, options: list[tuple[str, str]] | None = None) -> dict:
    q = {"field": field, "question": question}
    if why:
        q["why"] = why
    if options:
        q["options"] = [{"label": a, "message": b} for a, b in options]
    return q


def _questions_card(qs: list[dict]) -> dict:
    return {"type": "questions", "data": qs}


# ------------------------------------------------------------------ intents -> (reply, cards)

def _need_gpu(intent: str, gpus) -> tuple[str, list] | None:
    """A question when the intent needs a GPU and the message names none (or an ambiguous one)."""
    amb = next((g for g in gpus if g.kind == "ambiguous"), None)
    phrase = {"cheapest": "Cheapest available {}", "specs": "Specs of {}", "price_change": "Why did {} prices change?"}
    if amb:
        opts = [(_short(c), phrase.get(intent, "Cheapest available {}").format(_short(c))) for c in amb.candidates[:6]]
        return (f"\"{amb.text.upper()}\" matches several different GPUs. Which one?",
                [_questions_card([_question("gpu", "Which variant?", "variants are different products, priced "
                                                                       "separately", opts)])])
    if not [g for g in gpus if g.kind != "ambiguous"]:
        opts = [(p, phrase.get(intent, "Cheapest available {}").format(p)) for p in POPULAR]
        return ("Which GPU do you mean?", [_questions_card([_question("gpu", "Which GPU?", None, opts)])])
    return None


def _refs(gpus) -> list[tuple[str, str]]:
    return [(g.kind, g.name) for g in gpus if g.kind in ("gpu", "family")]


def _price_line(d: dict) -> str:
    name = _short(d["gpu"])
    c, m = d["cheapest"], d["market"]
    if c:
        top = c[0]
        where = top.get("provider_display") or top.get("provider")
        reg = f" ({top['region']})" if top.get("region") else ""
        avail = ("the provider reports it available" if top.get("available") is True
                 else "availability not reported by the provider")
        s = (f"Cheapest available {name} right now: {_money(top['price_per_gpu_hour'])}/GPU-hour on {where}{reg}; "
             f"observed market price, not a quote; {avail}.")
        nxt, seen = [], {top.get("provider")}
        for x in c[1:]:
            if x.get("provider") not in seen and len(nxt) < 2:
                seen.add(x.get("provider"))
                nxt.append(f"{_money(x['price_per_gpu_hour'])} on {x.get('provider_display') or x['provider']}")
        if nxt:
            s += " Next cheapest providers: " + ", ".join(nxt) + "."
    else:
        why = ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in sorted(d["exclusions_by_code"].items())[:4])
        s = f"No fresh, in-stock {name} listing right now" + (f" (excluded: {why})." if why else ".")
    if m["providers"]:
        s += f" Market now: low {_money(m['low'])}, median {_money(m['median'])} across {_providers(m['providers'])}."
    return s


def _variant_line(d: dict) -> str:
    c, m = d["cheapest"], d["market"]
    name = _short(d["gpu"])
    if c:
        top = c[0]
        s = f"{name}: from {_money(top['price_per_gpu_hour'])}/GPU-hour on {top.get('provider_display') or top['provider']}"
    else:
        s = f"{name}: no fresh, in-stock listing"
    if m["providers"]:
        s += f" (median {_money(m['median'])}, {_providers(m['providers'])})"
    return s


def do_cheapest(p: intents.Parsed) -> tuple[str, list]:
    ask = _need_gpu("cheapest", p.gpus)
    if ask:
        return ask
    cons = p.slots.get("constraints", {})
    kind, name = _refs(p.gpus)[0]
    data = tools.gpu_prices(kind, name, max_price=cons.get("max_price_per_gpu_hour"))
    cards = [{"type": "gpu_prices", "data": data}]
    if kind == "family":
        lines = [f"{name} is a family of different products, so each variant is priced separately "
                 f"(observed market prices, not quotes):"]
        lines.append("; ".join(_variant_line(v) for v in data["variants"]) + ".")
        best = min((v for v in data["variants"] if v["cheapest"]),
                   key=lambda v: v["cheapest"][0]["price_per_gpu_hour"], default=None)
        if best:
            lines.append(f"Cheapest available right now: {_short(best['gpu'])} at "
                         f"{_money(best['cheapest'][0]['price_per_gpu_hour'])}/GPU-hour.")
        cards += [_link(f"{name} variants side by side", f"/gpu/{data['family_slug']}")]
        if best:
            cards.append(_link(f"Preview a route for {_short(best['gpu'])}", f"/route?gpu={best['slug']}"))
        return " ".join(lines), cards
    cards += [_link(f"Open the {_short(name)} market", f"/gpu/{data['slug']}"),
              _link(f"Preview a route for {_short(name)} (sign-in required)", f"/route?gpu={data['slug']}")]
    return _price_line(data), cards


def _hw_line(it: dict) -> str:
    hw, m = it["hardware"], it["market"]
    parts = []
    if hw.get("vram_gb") is not None:
        parts.append(f"{_num(hw['vram_gb'])} GB {hw.get('memory_type') or ''}".strip())
    if hw.get("memory_bandwidth_tbps") is not None:
        parts.append(f"{_num(hw['memory_bandwidth_tbps'])} TB/s")
    if hw.get("bf16_tflops_dense") is not None:
        parts.append(f"BF16 {_num(hw['bf16_tflops_dense'])} TFLOPS dense")
    if hw.get("fp8_tflops_dense") is not None:
        parts.append(f"FP8 {_num(hw['fp8_tflops_dense'])} TFLOPS dense")
    if hw.get("form_factor"):
        parts.append(hw["form_factor"])
    price = (f"lowest {_money(m['low'])}/GPU-hour, median {_money(m['median'])} ({_providers(m['providers'])})"
             if m["providers"] else "no live priced listing")
    return f"{_short(it['gpu'])}: " + ", ".join(parts) + f"; {price}."


def do_compare(p: intents.Parsed) -> tuple[str, list]:
    refs = _refs(p.gpus)
    if len(refs) < 2:
        if any(g.kind == "ambiguous" for g in p.gpus):
            return _need_gpu("cheapest", p.gpus)
        first = _short(refs[0][1]) if refs else None
        opts = [(x, f"Compare {first} and {x}" if first else f"Compare H100 and {x}") for x in POPULAR
                if not first or x.lower() != first.lower()][:5]
        q = f"Compare {first} with which GPU?" if first else "Which two GPUs should I compare?"
        return q, [_questions_card([_question("gpu", q, None, opts)])]
    data = tools.compare(refs[:4])
    lines = ["Side by side (different products, not equivalents):"] + [_hw_line(it) for it in data["items"]]
    cards = [{"type": "compare", "data": data}]
    gpus = [it for it in data["items"]]
    if len(gpus) >= 2:
        cards.append(_link(f"Compare {_short(gpus[0]['gpu'])} vs {_short(gpus[1]['gpu'])} in detail",
                           f"/compare/{gpus[0]['slug']}-vs-{gpus[1]['slug']}"))
    return " ".join(lines), cards


def do_specs(p: intents.Parsed) -> tuple[str, list]:
    ask = _need_gpu("specs", p.gpus)
    if ask:
        return ask
    kind, name = _refs(p.gpus)[0]
    data = tools.specs(kind, name)
    lines = ([f"{name} is a family; its variants are different products:"] if kind == "family" else [])
    lines += [_hw_line(it) for it in data["items"]]
    lines.append("Hardware figures are vendor peak specs.")
    cards = [{"type": "specs", "data": data}]
    cards += [_link(f"Open the {_short(it['gpu'])} market", f"/gpu/{it['slug']}") for it in data["items"][:3]]
    return " ".join(lines), cards


def do_price_change(p: intents.Parsed) -> tuple[str, list]:
    ask = _need_gpu("price_change", p.gpus)
    if ask:
        return ask
    kind, name = _refs(p.gpus)[0]
    data = tools.market_context(kind, name)
    lines = []
    for it in data["items"]:
        nm = _short(it["gpu"])
        cur = it.get("current") or {}
        head = (f"{nm}: lowest {_money(cur.get('lowest'))}, median {_money(cur.get('median'))} now "
                f"({_providers(cur.get('providers') or 0)})." if cur.get("lowest") is not None
                else f"{nm}: no current priced market ({it.get('reason') or 'no data'}).")
        lines.append(head)
        if it["summaries"]:
            lines.append(" ".join(it["summaries"][:2]))
        if it["moves"]:
            mv = "; ".join(f"{m['direction']} {_num(abs(m['change_pct']))}% at {m['at'][:13].replace('T', ' ')}:00Z "
                           f"({_money(m['from'])} to {_money(m['to'])}"
                           + (", provider set changed" if m["provider_set_changed"] else "") + ")"
                           for m in it["moves"][-3:])
            lines.append(f"Recorded hourly moves of at least {_num(it['move_threshold_pct'])}% in the last "
                         f"{data['window_days']} days: {mv}.")
        else:
            lines.append(f"No hourly move of at least {_num(it['move_threshold_pct'])}% recorded in the last "
                         f"{data['window_days']} days.")
    if data["news"]:
        lines.append("Related news in the same window: " + "; ".join(
            f"\"{n['title']}\" ({n.get('source') or 'unknown source'})" for n in data["news"][:3]) + ".")
    else:
        lines.append(f"No related news recorded in the last {data['window_days']} days.")
    lines.append("This is correlation, not causation: OpenGrid shows prices, moves and news side by side and does "
                 "not know why a price moved.")
    slug = data["items"][0]["slug"] if len(data["items"]) == 1 else None
    cards = [{"type": "market_context", "data": data}]
    if kind == "family":
        import families
        fam = families.family(name)
        cards.append(_link(f"{name} variants side by side", f"/gpu/{fam['slug'] if fam else name.lower()}"))
    elif slug:
        cards.append(_link(f"{_short(name)} price history", f"/gpu/{slug}"))
    cards.append(_link("Market events", "/events"))
    cards.append(_link("GPU market news", "/news"))
    return " ".join(lines), cards


# ------------------------------------------------------------------ workload -> recommendation

def _blocking(req: dict, asked: list[str]) -> list[dict]:
    qs = []
    wt = req.get("workload_type")
    if not wt:
        qs.append(_question("workload_type", "What will you run?", "training, fine-tuning and inference need very "
                            "different memory", [("Fine-tuning", "fine-tuning"), ("Training from scratch", "training"),
                                                 ("Inference / serving", "inference")]))
    if _get(req, "model.parameters_b") is None and not _get(req, "model.name"):
        qs.append(_question("model.parameters_b", "Which model, or how many parameters?",
                            "model size drives the memory estimate",
                            [("7B", "7B parameters"), ("13B", "13B parameters"), ("70B", "70B parameters"),
                             ("Llama 3 8B", "Llama 3 8B")]))
    if wt == "fine_tuning" and not _get(req, "training.method") and "training.method" not in asked:
        qs.append(_question("training.method", "Full fine-tuning, LoRA or QLoRA?",
                            "changes optimizer and weight memory by up to ~10x",
                            [("LoRA", "LoRA"), ("QLoRA", "QLoRA"), ("Full fine-tuning", "full fine-tuning")]))
    return qs


def _opt_line(label: str, o: dict) -> str:
    where = o.get("provider_display") or o.get("provider")
    kind = o.get("price_kind") or "observed"
    cnt = f"{o['gpu_count']}x " if (o.get("gpu_count") or 1) > 1 else ""
    s = f"{label}: {cnt}{_short(o.get('gpu'))} on {where} at {_money(o.get('price_per_gpu_hour'))}/GPU-hour ({kind}"
    s += ")" if o.get("fresh", True) else ", not fresh)"
    fit = o.get("memory_fit") or {}
    if fit.get("headroom_pct") is not None:
        s += f", {_num(fit['headroom_pct'])}% memory headroom"
    return s + "."


def _rec_reply(rec: dict, req: dict, intent: str) -> str:
    est = rec.get("estimate") or {}
    if est.get("needs_gpu") is False:
        return f"This workload does not look like it needs a GPU: {est.get('needs_gpu_reason') or 'no GPU signal'}."
    lines = []
    mem = est.get("memory") or {}
    if mem.get("recommended_gb") is not None:
        s = f"Estimated memory: about {_num(mem['recommended_gb'])} GB"
        if mem.get("per_gpu_gb") is not None and (est.get("gpu_count") or {}).get("suggested", 1) > 1:
            s += f" ({_num(mem['per_gpu_gb'])} GB per GPU across {est['gpu_count']['suggested']} GPUs)"
        s += f", {mem.get('confidence') or 'unknown'} confidence (an estimate, not a measurement)."
        lines.append(s)
    elif mem.get("method") == "unavailable":
        lines.append("Memory could not be estimated from what is known so far.")
    opts = {o.get("option_id"): o for o in rec.get("options") or []}
    picks = rec.get("picks") or {}
    cheapest = None
    for key, label in PICKS:
        ref = picks.get(key)
        o = opts.get(ref.get("option_id")) if isinstance(ref, dict) else None
        if o:
            lines.append(_opt_line(label, o))
            cheapest = cheapest or (o if key == "cheapest_market" else None)
        elif key == "best_executable":
            reason = next((r.get("reason") for o2 in opts.values()
                           for r in ((o2.get("execution") or {}).get("reasons") or []) if r.get("reason")), None)
            lines.append("Launchable through OpenGrid: none for you right now" + (f" ({reason})." if reason else "."))
        elif key == "cheapest_market":
            lines.append("No fresh listing that fits the estimate is available right now.")
    # the whole job, when the training work is known: more GPUs can cost more per hour and less overall
    ref = picks.get("lowest_estimated_job_cost")
    low = opts.get(ref.get("option_id")) if isinstance(ref, dict) else None
    jc = _get(low or {}, "performance.job_cost")
    rt = _get(low or {}, "performance.runtime")
    if low and jc and rt and rt.get("known"):
        cnt = f"{low['gpu_count']}x " if (low.get("gpu_count") or 1) > 1 else ""
        lines.append(f"Lowest estimated cost for the whole job: {cnt}{_short(low.get('gpu'))} on "
                     f"{low.get('provider_display') or low.get('provider')}, about {_money(jc['usd'])} "
                     f"({_money(jc['range_usd'][0])} to {_money(jc['range_usd'][1])}) for about "
                     f"{_num(rt['hours'])} hours. That is a theoretical estimate from spec-sheet throughput, not a "
                     f"benchmark.")
    budget = _get(req, "constraints.budget_usd")
    if budget is not None:
        hrs = _get(cheapest or {}, "cost.budget_hours")
        if low and jc and rt and rt.get("known"):
            fits = jc["range_usd"][1] <= budget
            lines.append(f"Against {_money(budget)}: " + ("the estimate fits even at its slow end." if fits else
                         "the slow end of the estimate is above it." if jc["range_usd"][0] <= budget else
                         "even the fast end of the estimate is above it."))
        elif cheapest and hrs is not None:
            lines.append(f"Within {_money(budget)}: that buys about {_num(hrs)} hours on the cheapest fitting option "
                         f"(estimated; storage, egress and startup time not included). Runtime is not benchmarked and "
                         f"the amount of training data is unknown, so whether that is enough time is not known.")
        elif intent == "can_run" and not cheapest:
            lines.append(f"So within {_money(budget)}: not with what is listed right now.")
    if rec.get("notes"):
        lines.append(str(rec["notes"][0]))
    return " ".join(lines) or "No recommendation could be made from what is known."


def do_recommend(p: intents.Parsed, ctx: dict, who) -> tuple[str, list, dict]:
    new = copy.deepcopy(p.slots)
    hints = [g.name for g in p.gpus if g.kind in ("gpu", "family")]
    if hints:
        new.setdefault("hardware_hints", {})["gpus_mentioned"] = hints[:8]
    base = ctx["requirements"]
    fresh = bool(new.get("workload_type") and new.get("model") and base.get("model")
                 and _get(base, "model") != new.get("model"))
    req = _merge({} if fresh else base, new)
    req["source"] = "assistant"
    asked = [] if fresh else list(ctx["asked"])
    qs = _blocking(req, asked)
    state = {"requirements": req, "pending": p.intent if p.intent in ("recommend", "can_run") else "recommend",
             "asked": asked}
    if qs:
        state["asked"] = sorted(set(asked) | {q["field"] for q in qs})[:12]
        lead = {"workload_type": "what you will run", "model.parameters_b": "the model size",
                "training.method": "the fine-tuning method"}
        what = " and ".join(lead[q["field"]] for q in qs[:2])
        return f"To recommend a GPU I need {what}.", [_questions_card(qs)], state
    rec = tools.recommend(req, who)
    cards = [{"type": "recommendation", "data": rec}]
    follow = [q for q in (rec.get("questions") or []) if isinstance(q, dict)][:3]
    if _get(req, "constraints.budget_usd") is None and not any(q.get("field") == "constraints.budget_usd" for q in follow) \
            and "constraints.budget_usd" not in asked:
        follow.append(_question("constraints.budget_usd", "Do you have a total budget?",
                                "shows how many hours each option buys",
                                [("$25", "budget $25"), ("$50", "budget $50"), ("$100", "budget $100")]))
        state["asked"] = sorted(set(state["asked"]) | {"constraints.budget_usd"})[:12]
    if follow:
        cards.append(_questions_card(follow))
    cards.append(_link("Refine this in Find Compute", "/find"))
    return _rec_reply(rec, req, p.intent), cards, state


# ------------------------------------------------------------------ jobs (My Jobs): read + ACTION cards only
#
# HARD RULE: nothing here creates, approves, cancels or launches anything. An ACTION card is a button the browser
# shows; the browser calls the existing API (POST /v1/jobs, POST /v1/jobs/{id}/cancel) only after the user clicks
# and confirms. Job data comes from assistant/jobtools.py (the caller's own jobs, GET /v1/jobs ownership rules).

MNIST = "pytorch-mnist"
DEMO_NOTE = "Simulated: no GPU, no provider, no spend"
REAL_HREF = "/jobs/new?preset=mnist&real=1"
REAL_LABEL = "Check price and run on a real GPU"
SIGN_IN = ("To see your jobs here you need to be signed in, or to use an API key with the deployments:read scope. "
           "Anonymous visitors cannot see any jobs.")


def _template() -> dict:
    from workloads import templates

    t = templates.get_template(MNIST)
    if t is None:  # the allowlist is code; a missing template is a deploy bug, not a guess
        raise tools.ToolUnavailable("the MNIST template is not installed")
    return t


def _demo_action(t: dict) -> dict:
    return {"type": "action", "data": {"action": "start_demo", "label": "Start free simulation",
                                       "epochs": t["preset"]["config"]["epochs"], "note": DEMO_NOTE, "confirm": None}}


def _keys_link() -> dict:
    return _link("Sign in or create an API key", "/keys")


def _act_note(who) -> str:
    return ("" if jobtools.can_act(who) else
            " Starting a job needs you to be signed in (an API key with the route:execute scope).")


def do_demo(who) -> tuple[str, list]:
    t = _template()
    reply = (f"You can try OpenGrid with a free simulation of the {t['title'].split(': ')[-1]} example. It walks "
             f"through the whole job lifecycle with synthetic numbers that are labelled simulated: no GPU, no "
             f"provider and no spend, and it takes about a minute. Nothing starts until you click \"Start free "
             f"simulation\".") + _act_note(who)
    cards = [_demo_action(t), _link("Open the guided demo page", "/jobs/new?preset=mnist&sandbox=1")]
    if not jobtools.can_act(who):
        cards.append(_keys_link())
    return reply, cards


def _template_card(t: dict) -> dict:
    lb, pre, rt = t["limits_bounds"], t["preset"]["limits"], t["expected_runtime"]
    cfg = t["config_schema"]["epochs"]
    return {"type": "template", "data": {
        "template": t["id"], "version": t["version"], "title": t["title"], "gpu_count": t["requirements"]["gpu_count"],
        "vendor": t["requirements"]["vendor"], "on_demand_only": t["requirements"]["on_demand_only"],
        "epochs": {"min": cfg["min"], "max": cfg["max"], "default": cfg["default"]},
        "limits_max": {"max_runtime_minutes": lb["max_runtime_minutes"]["max"],
                       "max_total_cost_usd": lb["max_total_cost_usd"]["max"],
                       "max_price_per_gpu_hour_usd": lb["max_price_per_gpu_hour_usd"]["max"]},
        "preset_limits": dict(pre),
        "expected_runtime": {"kind": rt["kind"], "default_band_minutes": list(rt["default_band_minutes"]),
                             "note": rt["note"]},
        "approval_required": True, "custom_code": False}}


def do_real_run(who) -> tuple[str, list]:
    t = _template()
    card = _template_card(t)
    d = card["data"]
    lm, pre, band = d["limits_max"], d["preset_limits"], d["expected_runtime"]["default_band_minutes"]
    reply = (f"The only workload OpenGrid can run on a real GPU today is the {t['title'].split(': ')[-1]} template "
             f"(no custom code yet). It is capped: {d['gpu_count']} {d['vendor']} GPU, on-demand only, up to "
             f"{d['epochs']['max']} epochs, a runtime ceiling of at most {_num(lm['max_runtime_minutes'])} minutes "
             f"and a total cost cap of at most {_money(lm['max_total_cost_usd'])}; the preset uses "
             f"{_num(pre['max_runtime_minutes'])} minutes, {_money(pre['max_total_cost_usd'])} total and at most "
             f"{_money(pre['max_price_per_gpu_hour_usd'])}/GPU-hour. With the default {d['epochs']['default']} epochs "
             f"it is expected to take about {_num(band[0])} to {_num(band[1])} minutes (an estimate, not a "
             f"benchmark). Nothing launches until OpenGrid approves it: the job waits for approval and is not billed "
             f"before that. The next page shows the live price before you submit. The free simulation is the no-cost "
             f"alternative.") + _act_note(who)
    cards = [card, _link(REAL_LABEL, REAL_HREF), _demo_action(t)]
    if not jobtools.can_act(who):
        cards.append(_keys_link())
    return reply, cards


def do_deploy_custom(p: intents.Parsed, ctx: dict, who) -> tuple[str, list, dict]:
    lead = ("OpenGrid cannot run your own code, containers or models yet: the only workload it runs today is its "
            "PyTorch MNIST example. It can recommend where to run yours, using live market prices.")
    budget = _get(p.slots, "constraints.budget_usd") is not None
    sub = intents.Parsed("can_run" if budget else "recommend", p.slots, p.gpus, p.confidence, p.signals)
    reply, cards, state = do_recommend(sub, ctx, who)
    if not any(c["type"] == "link" and c["data"]["href"] == "/find" for c in cards):
        cards.append(_link("Find compute for your workload", "/find"))
    return f"{lead} {reply}", cards, state


def _job_line(it: dict) -> str:
    return f"{it['name']} ({it['job_id']}): {it['status_text']}"


def _cost_line(it: dict) -> str:
    c = it.get("cost") or {}
    parts = []
    if c.get("est_max_usd") is not None:
        parts.append(f"estimated maximum cost {_money(c['est_max_usd'])}")
    if c.get("actual_usd") is not None:
        parts.append(f"actual cost {_money(c['actual_usd'])}")
    if not parts:
        return ""
    s = "; ".join(parts)
    return " " + s[0].upper() + s[1:] + "."


def _not_found() -> tuple[str, list]:
    return ("I can't find that job among yours. Check the job id on your Jobs page.",
            [_link("Your jobs", "/jobs")])


def _target_id(p: intents.Parsed, ctx: dict) -> str | None:
    """A job named in the message, else the previous turn's job when the message says "it" / "this job"."""
    if p.job_id:
        return p.job_id
    if p.pronoun and ctx.get("job_id"):
        return ctx["job_id"]
    return None


def do_jobs(p: intents.Parsed, ctx: dict, who) -> tuple[str, list, str | None]:
    """-> (reply, cards, job_id for the context echo)."""
    if not jobtools.can_read(who):
        return SIGN_IN, [_keys_link()], None
    jid = _target_id(p, ctx)
    if jid:
        it = jobtools.get_job(who, jid)
        if it is None:
            r, c = _not_found()
            return r, c, None
        return (_job_line(it) + "." + _cost_line(it)), \
            [{"type": "job", "data": it}, _link(f"Open {it['job_id']}", it["href"])], it["job_id"]
    data = jobtools.list_jobs(who)
    items = data["items"]
    if not items:
        t = _template()
        return ("You have no jobs yet. A good first step is the free simulation of the MNIST example: no GPU and "
                "no spend." + _act_note(who)), [{"type": "jobs", "data": data}, _demo_action(t)], None
    shown = items[:5]
    reply = "Your latest jobs, newest first: " + "; ".join(_job_line(it) for it in shown) + "."
    if len(items) > len(shown):
        reply += f" {len(items) - len(shown)} more in the list."
    if data["more"]:
        reply += " Older jobs are on the Jobs page."
    active = [it for it in items if it["cancellable"]]
    focus = items[0]["job_id"] if len(items) == 1 else (active[0]["job_id"] if len(active) == 1 else ctx.get("job_id"))
    return reply, [{"type": "jobs", "data": data}, _link("All your jobs", "/jobs")], focus


def _cancel_card(it: dict) -> dict:
    pre = it["status"] in jobtools.S.PRE_LAUNCH
    what = ("It has not launched yet: its approval request is rejected and nothing is billed." if pre else
            "This terminates the GPU: the instance and its local data are destroyed, and artifacts not yet uploaded "
            "are lost.")
    return {"type": "action", "data": {
        "action": "cancel_job", "job_id": it["job_id"], "label": f"Cancel {it['job_id']}",
        "confirm": f"Cancel {it['name']} ({it['job_id']})? {what}",
        "note": f"Nothing happens until you confirm; the browser then calls POST /v1/jobs/{it['job_id']}/cancel. "
                f"Records, logs and uploaded artifacts are kept."}}


def do_cancel(p: intents.Parsed, ctx: dict, who) -> tuple[str, list, str | None, str | None]:
    """-> (reply, cards, job_id for the context echo, pending). Never cancels: at most ONE cancel_job action."""
    if not jobtools.can_read(who):
        return (SIGN_IN + " Cancelling also needs the route:execute scope."), [_keys_link()], None, None
    jid = _target_id(p, ctx)
    if jid:
        it = jobtools.get_job(who, jid)
        if it is None:
            r, c = _not_found()
            return r, c, None, None
    else:
        data = jobtools.list_jobs(who, open_only=True)
        active = [x for x in data["items"] if x["cancellable"]]
        if not active:
            return ("You have no job that can be cancelled right now: none is waiting for approval or running. "
                    "Simulations end by themselves and have nothing to cancel."), \
                [_link("Your jobs", "/jobs")], ctx.get("job_id"), None
        if len(active) > 1:
            data = {"items": active, "returned": len(active), "more": data["more"]}
            opts = [(f"Cancel {x['job_id']}", f"cancel {x['job_id']}") for x in active[:6]]
            return (f"You have {len(active)} active jobs. Which one should be cancelled?"), \
                [{"type": "jobs", "data": data}, _questions_card([_question("job_id", "Which job?", None, opts)])], \
                ctx.get("job_id"), "cancel"
        it = active[0]
    card = {"type": "job", "data": it}
    if not it["cancellable"]:
        why = ("it is a simulation, so there is nothing to cancel" if it["sandbox"] else
               "it is already over" if it["status"] in jobtools.S.FINISHED else
               "a cancel or GPU shutdown is already under way")
        return f"{_job_line(it)}. It cannot be cancelled: {why}.", [card, _link(f"Open {it['job_id']}", it["href"])], \
            it["job_id"], None
    if not jobtools.can_act(who):
        return (f"{_job_line(it)}. Your key can see this job but needs the route:execute scope to cancel it."), \
            [card, _keys_link()], it["job_id"], None
    act = _cancel_card(it)
    reply = (f"{_job_line(it)}. To cancel it, click \"{act['data']['label']}\" and confirm. "
             + act["data"]["confirm"].split("? ", 1)[1] + " Nothing changes until you confirm.")
    return reply, [act, card], it["job_id"], None


# ------------------------------------------------------------------ unknown

def do_unknown() -> tuple[str, list]:
    reply = ("I answer questions about OpenGrid's live GPU market: the cheapest available GPU, which GPU fits a "
             "training, fine-tuning or inference job, comparisons, specs, and recent price moves. I can also show "
             "your jobs, set up a free training simulation, or prepare a cancel for you to confirm. Try one of these:")
    return reply, [_questions_card([_question("intent", "Try asking", None, [(e, e) for e in intents.EXAMPLES])])]


# ------------------------------------------------------------------ LLM (optional)

def _numbers_in(text: str) -> set:
    return {float(x) for x in re.findall(r"\d+(?:\.\d+)?", text or "")}


def _llm_classify(provider, message: str, p: intents.Parsed) -> intents.Parsed | None:
    """Ask the model to route an unclear question. Its output is validated against the message itself."""
    if not llm_mod.allow_call():
        return None
    try:
        c = provider.complete(llm_mod.CLASSIFY_SYSTEM, [{"role": "user", "content": message}], [llm_mod.ROUTE_TOOL],
                              max_tokens=400)
    except llm_mod.LLMError as e:
        log.info("assistant LLM classify unavailable: %s", e)
        return None
    call = next((t for t in c.tool_calls if t.get("name") == "route_question"), None)
    if not call or not isinstance(call.get("input"), dict):
        return None
    a = call["input"]
    intent = a.get("intent")
    if intent not in intents.INTENTS or intent == "unknown":
        return None
    low = message.lower()
    nums = _numbers_in(message)
    slots = copy.deepcopy(p.slots)
    if a.get("workload_type") in ("training", "fine_tuning", "inference", "batch"):
        slots["workload_type"] = a["workload_type"]
    if a.get("training_method") in ("full", "lora", "qlora") and a["training_method"] in low:
        slots.setdefault("training", {})["method"] = a["training_method"]
    if isinstance(a.get("model_name"), str) and 0 < len(a["model_name"]) <= 120 and a["model_name"].lower() in low:
        slots.setdefault("model", {})["name"] = a["model_name"]
    for key, path in (("parameters_b", ("model", "parameters_b")), ("budget_usd", ("constraints", "budget_usd"))):
        v = a.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and float(v) in nums:   # must be in the message
            slots.setdefault(path[0], {})[path[1]] = float(v)
    gpus = list(p.gpus)
    for g in (a.get("gpus") or [])[:4]:
        if isinstance(g, str) and g.lower() in low:
            for ref in intents.find_gpus(g):
                if all((ref.kind, ref.name) != (x.kind, x.name) for x in gpus):
                    gpus.append(ref)
    # job ids are never taken from the model: only the deterministic parse of the message itself
    return intents.Parsed(intent, slots, gpus, "medium", p.signals + ["llm:classify"], job_id=p.job_id,
                          pronoun=p.pronoun)


def _compact(cards: list, limit: int = 24000) -> str:
    s = json.dumps(cards, default=str, separators=(",", ":"))
    return s if len(s) <= limit else s[:limit] + "...(truncated)"


def _llm_phrase(provider, message: str, cards: list, draft: str, ok: grounding.Allowed) -> tuple[str | None, dict]:
    if not llm_mod.allow_call():
        return None, {"used": False, "reason": "model call budget exhausted"}
    msgs = [
        {"role": "user", "content": message},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "lookup_1", "name": "opengrid_lookup",
                                           "input": {"question": message[:500]}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "lookup_1",
             "content": json.dumps({"cards": _compact(cards), "draft_reply": draft})},
            {"type": "text", "text": "Write the final reply from these results now."}]},
    ]
    try:
        c = provider.complete(llm_mod.PHRASE_SYSTEM, msgs, [llm_mod.LOOKUP_TOOL], tool_choice="none", max_tokens=600)
    except llm_mod.LLMError as e:
        return None, {"used": False, "reason": str(e)}
    text = (c.text or "").strip()
    if not text:
        return None, {"used": False, "reason": "empty model reply"}
    if len(text) > 1500:
        return None, {"used": False, "reason": "model reply too long"}
    problems = grounding.check(text, ok)
    if problems:
        log.warning("assistant LLM reply rejected: %s", problems[:3])
        return None, {"used": False, "rejected": problems[:5], "reason": "ungrounded model reply; deterministic "
                                                                         "reply used"}
    return text, {"used": True, "model": c.model or getattr(provider, "model", "")}


# ------------------------------------------------------------------ entry point

def respond(message: str, context: dict | None, who, *, provider=...) -> dict:
    """The POST /v1/assistant payload. `provider`: tests may inject a fake ModelProvider (default: configured)."""
    if not isinstance(message, str) or not message.strip():
        raise ContextError("message must be a non-empty string")
    if len(message) > MAX_MESSAGE:
        raise ContextError(f"message is longer than {MAX_MESSAGE} characters")
    ctx = sanitize_context(context)
    prov = llm_mod.provider() if provider is ... else provider
    p = intents.parse(message)
    llm_info: dict = {"configured": prov is not None, "used": False}

    # an answer to the question we asked last turn ("LoRA", "$50", "no budget") continues that workload;
    # after "which job should be cancelled?" a bare job id ("job-ab12cd34") is that answer
    if ctx["pending"] == "cancel" and p.intent == "jobs" and p.job_id:
        p = intents.Parsed("cancel", p.slots, p.gpus, "medium", p.signals + ["context:pending"], job_id=p.job_id)
    elif ctx["pending"] and p.intent == "unknown":
        p = intents.Parsed(ctx["pending"], p.slots, p.gpus, "medium", p.signals + ["context:pending"],
                           job_id=p.job_id, pronoun=p.pronoun)
    elif p.confidence == "low" and prov is not None:
        q = _llm_classify(prov, message, p)
        if q is not None:
            p = q
            llm_info["classified"] = True

    state = {"requirements": ctx["requirements"], "pending": None, "asked": ctx["asked"]}
    job_id = ctx["job_id"]
    try:
        if p.intent in ("recommend", "can_run"):
            reply, cards, state = do_recommend(p, ctx, who)
        elif p.intent == "deploy_custom":
            reply, cards, state = do_deploy_custom(p, ctx, who)
        elif p.intent == "demo":
            reply, cards = do_demo(who)
        elif p.intent == "real_run":
            reply, cards = do_real_run(who)
        elif p.intent == "jobs":
            reply, cards, job_id = do_jobs(p, ctx, who)
        elif p.intent == "cancel":
            reply, cards, job_id, pending = do_cancel(p, ctx, who)
            state = {**state, "pending": pending}
        elif p.intent == "cheapest":
            reply, cards = do_cheapest(p)
        elif p.intent == "compare":
            reply, cards = do_compare(p)
        elif p.intent == "specs":
            reply, cards = do_specs(p)
        elif p.intent == "price_change":
            reply, cards = do_price_change(p)
        else:
            reply, cards = do_unknown()
    except tools.ToolUnavailable as e:
        log.warning("assistant tool unavailable: %s", e)
        reply = ("The recommendation engine is not available on this server right now, so I cannot size this "
                 "workload. You can still ask for GPU prices, specs or comparisons.")
        cards = [_link("Browse GPU markets", "/gpus")]
        state = {**state, "pending": None}
    except Exception:  # noqa: BLE001  a data outage must not become a 500/503: say so, with no numbers
        log.exception("assistant tool failed")
        if p.intent in ("jobs", "cancel"):
            reply = "Job data is unavailable right now; please try again shortly. Nothing was changed."
            cards = [_link("Your jobs", "/jobs")]
        else:
            reply = "Live market data is unavailable right now; please try again shortly."
            cards = [_link("GPU markets", "/gpus")]
    if job_id:
        state = {**state, "job_id": job_id}
    else:
        state = {k: v for k, v in state.items() if k != "job_id"}

    cards = jsonable_encoder(cards)
    ok = grounding.allowed(cards, message, ctx["requirements"])
    problems = grounding.check(reply, ok)
    if problems:  # a bug in a composer: surface it in logs and in `grounded`
        log.error("deterministic assistant reply not grounded: %s", problems)
    engine = "deterministic"
    # A cancel is never re-phrased by a model: its wording (what happens, that nothing happens until the user
    # confirms) is exact. Everything else may be phrased and is then checked (numbers, links, claimed actions).
    if prov is not None and cards and p.intent not in ("unknown", "cancel") and not problems \
            and not all(c["type"] in ("questions", "link") for c in cards):
        text, info = _llm_phrase(prov, message, cards, reply, ok)
        llm_info.update(info)
        if text:
            reply, engine = text, f"llm:{info.get('model') or getattr(prov, 'model', 'model')}"
    return {"reply": reply, "cards": cards, "context": jsonable_encoder(state), "engine": engine,
            "grounded": not problems, "intent": p.intent, "llm": llm_info}
