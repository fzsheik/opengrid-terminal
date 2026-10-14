"""The OpenGrid Assistant engine: respond(message, context, who) -> the POST /v1/assistant payload (contract §5).

    {"reply": str, "cards": [{"type", "data"}], "context": {"requirements", "pending", "asked"},
     "engine": "deterministic" | "llm:<model>", "grounded": bool, "intent": str, "llm": {...}}

Flow
    1. validate + bound the echoed context (stateless server: the client sends back what we returned)
    2. intents.parse(message) (deterministic); only when it is unsure AND an LLM is configured, the LLM may
       classify -- its output is re-validated against the message (llm.py)
    3. ask only the questions that block a useful answer (workload type, model size, fine-tuning method);
       everything else is answered with what is known, and the engine's own follow-up questions are attached
    4. call read-only tools (tools.py) -> cards; compose `reply` from the cards only
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

from assistant import grounding, intents, tools
from assistant import llm as llm_mod

log = logging.getLogger("opengrid.assistant")

MAX_MESSAGE = 2000
MAX_CONTEXT_BYTES = 8192
MAX_DEPTH = 6
CONTEXT_KEYS = {"requirements", "pending", "asked"}
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
        return {"requirements": {}, "pending": None, "asked": []}
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
    if pending not in (None, "recommend", "can_run"):
        raise ContextError("context.pending must be null, 'recommend' or 'can_run'")
    asked = context.get("asked") or []
    if not isinstance(asked, list) or len(asked) > 12 or not all(isinstance(a, str) and len(a) <= 64 for a in asked):
        raise ContextError("context.asked must be a short list of field names")
    return {"requirements": copy.deepcopy(req), "pending": pending, "asked": list(asked)}


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
    budget = _get(req, "constraints.budget_usd")
    if budget is not None:
        hrs = _get(cheapest or {}, "cost.budget_hours")
        if cheapest and hrs is not None:
            lines.append(f"Within {_money(budget)}: that buys about {_num(hrs)} hours on the cheapest fitting option "
                         f"(estimated; storage, egress and startup time not included). Runtime is not benchmarked, so "
                         f"whether that is enough time is not known.")
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


# ------------------------------------------------------------------ unknown

def do_unknown() -> tuple[str, list]:
    reply = ("I answer questions about OpenGrid's live GPU market: the cheapest available GPU, which GPU fits a "
             "training, fine-tuning or inference job, comparisons, specs, and recent price moves. Try one of these:")
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
    return intents.Parsed(intent, slots, gpus, "medium", p.signals + ["llm:classify"])


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

    # an answer to the question we asked last turn ("LoRA", "$50", "no budget") continues that workload
    if ctx["pending"] and p.intent == "unknown":
        p = intents.Parsed(ctx["pending"], p.slots, p.gpus, "medium", p.signals + ["context:pending"])
    elif p.confidence == "low" and prov is not None:
        q = _llm_classify(prov, message, p)
        if q is not None:
            p = q
            llm_info["classified"] = True

    state = {"requirements": ctx["requirements"], "pending": None, "asked": ctx["asked"]}
    try:
        if p.intent in ("recommend", "can_run"):
            reply, cards, state = do_recommend(p, ctx, who)
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
        reply = "Live market data is unavailable right now; please try again shortly."
        cards = [_link("GPU markets", "/gpus")]

    cards = jsonable_encoder(cards)
    ok = grounding.allowed(cards, message, ctx["requirements"])
    problems = grounding.check(reply, ok)
    if problems:  # a bug in a composer: surface it in logs and in `grounded`
        log.error("deterministic assistant reply not grounded: %s", problems)
    engine = "deterministic"
    if prov is not None and cards and p.intent != "unknown" and not problems \
            and not all(c["type"] in ("questions", "link") for c in cards):
        text, info = _llm_phrase(prov, message, cards, reply, ok)
        llm_info.update(info)
        if text:
            reply, engine = text, f"llm:{info.get('model') or getattr(prov, 'model', 'model')}"
    return {"reply": reply, "cards": cards, "context": jsonable_encoder(state), "engine": engine,
            "grounded": not problems, "intent": p.intent, "llm": llm_info}
