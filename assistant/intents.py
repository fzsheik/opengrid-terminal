"""Deterministic intent parser for the OpenGrid Assistant. No LLM, no I/O beyond the static GPU catalogue.

    parse(message) -> Parsed
        intent      one of INTENTS
        slots       WorkloadRequirements-shaped dict (docs/OPENGRID2_CONTRACTS.md §1): only what the text says;
                    unknown stays absent (never a guess)
        gpus        [GpuRef(kind="gpu"|"family"|"ambiguous", name, text, candidates)] in message order
        confidence  "high" | "medium" | "low"  ("low" lets an optional LLM classify instead)

GPU names resolve through api.common.resolve_gpu_or_family semantics: a canonical GPU wins, otherwise a
family ("H100") -- families are never merged into one market, the tools list each variant. A loose mention
("H100 SXM", "4090", "a100 80gb") narrows a family to one variant only when exactly one variant matches.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

INTENTS = ("cheapest", "recommend", "can_run", "compare", "price_change", "specs", "demo", "real_run", "deploy_custom",
           "jobs", "cancel", "unknown")
# the job intents (My Jobs): the assistant only READS jobs and offers ACTION cards; it never acts itself
JOB_INTENTS = ("demo", "real_run", "deploy_custom", "jobs", "cancel")

EXAMPLES = [
    "Cheapest available H100",
    "What GPU should I use to fine-tune Llama 3 8B with LoRA?",
    "Affordable GPU for inference of a 7B model",
    "Compare H100 SXM and H200",
    "Why did H200 prices change?",
    "Can I fine-tune a 13B model with QLoRA within $50?",
    "Specs of the L40S",
    "Try a free training demo",
    "How are my jobs doing?",
]


@dataclass
class GpuRef:
    kind: str                 # "gpu" | "family" | "ambiguous"
    name: str | None          # canonical name / family id; None when ambiguous
    text: str                 # the words matched in the message
    candidates: list[str] = field(default_factory=list)   # ambiguous: canonical names that match

    def as_dict(self) -> dict:
        return {"kind": self.kind, "name": self.name, "text": self.text, "candidates": self.candidates}


@dataclass
class Parsed:
    intent: str
    slots: dict
    gpus: list[GpuRef]
    confidence: str
    signals: list[str] = field(default_factory=list)      # why: which rules fired (for tests / debugging)
    job_id: str | None = None                             # a job id named in the message ("job-ab12cd34")
    pronoun: bool = False                                 # "it" / "this job": may mean the previous turn's job

    def as_dict(self) -> dict:
        return {"intent": self.intent, "slots": self.slots, "gpus": [g.as_dict() for g in self.gpus],
                "confidence": self.confidence, "signals": self.signals, "job_id": self.job_id,
                "pronoun": self.pronoun}


# ---------------------------------------------------------------------------------------------------------
# GPU mentions
# ---------------------------------------------------------------------------------------------------------

_VENDOR = re.compile(r"^(NVIDIA|AMD Instinct|AMD|Intel)\s+", re.I)
_MEM = re.compile(r"^\d+gb$")
_FORM = re.compile(r"^(sxm\d?|pcie|nvl|nvlink|sxm)$")


def _norm(text: str) -> str:
    """lowercase, hyphens/underscores -> spaces, '80 GB' -> '80gb', whitespace collapsed."""
    t = text.lower().replace("_", " ").replace("-", " ")
    t = re.sub(r"(\d)\s+gb\b", r"\1gb", t)
    return re.sub(r"\s+", " ", t).strip()


def _quals(tokens) -> frozenset:
    """Non-memory qualifier words of a GPU name, sxm5 / sxm4 / sxm2 counted as sxm."""
    return frozenset("sxm" if re.match(r"^sxm\d?$", x) else x for x in tokens if not _MEM.match(x))


@lru_cache(maxsize=1)
def _catalogue() -> tuple[dict[str, set], dict[str, str]]:
    """(alias -> {canonical names}, family alias -> family id). Built once from canonical.py / families.py."""
    import canonical
    import families

    aliases: dict[str, set] = {}

    def add(alias: str, name: str):
        alias = re.sub(r"\s+", " ", alias).strip()
        if alias:
            aliases.setdefault(alias, set()).add(name)

    for name in canonical.all_canonical_names():
        short = _norm(_VENDOR.sub("", name))
        toks = short.split()
        add(short, name)
        # model words = everything before the first memory token ("rtx 3090 ti", "h100", "a100")
        cut = next((i for i, t in enumerate(toks) if _MEM.match(t)), len(toks))
        model, rest = toks[:cut], toks[cut:]
        mem = [t for t in rest if _MEM.match(t)]
        quals = [t for t in rest if not _MEM.match(t)]
        if not model:
            continue
        m = " ".join(model)
        add(m, name)
        for q in quals:
            add(f"{m} {q}", name)
            if re.match(r"^sxm\d$", q):
                add(f"{m} sxm", name)
            for g in mem:
                add(f"{m} {g} {q}", name)
                if re.match(r"^sxm\d$", q):
                    add(f"{m} {g} sxm", name)
        for g in mem:
            add(f"{m} {g}", name)
        if len(quals) > 1:
            add(f"{m} {' '.join(quals)}", name)
        # "4090" for "RTX 4090": a bare 4-digit RTX model number
        if model[0] == "rtx" and len(model) >= 2 and re.match(r"^\d{4}$", model[1]):
            add(" ".join(model[1:]), name)
        # "tesla v100" -> also "v100"
        if model[0] in ("tesla", "quadro") and len(model) > 1:
            add(" ".join(model[1:]), name)
    fam: dict[str, str] = {}
    for f in families.all_families():
        fam[_norm(f["id"])] = f["id"]
    return aliases, fam


@lru_cache(maxsize=1)
def _alias_regex() -> re.Pattern:
    aliases, fam = _catalogue()
    keys = sorted(set(aliases) | set(fam), key=len, reverse=True)
    # word-bounded; never inside a price ($4090) or a longer number / name (l4 inside l40s)
    # (an optional plural "s": "H100s"; longer aliases are tried first, so "l40s" stays the L40S)
    return re.compile(r"(?<![\w$.])(" + "|".join(re.escape(k) for k in keys) + r")s?(?![\w])")


def find_gpus(message: str) -> list[GpuRef]:
    """Every GPU / family mention, longest match first, in message order, de-duplicated."""
    aliases, fam = _catalogue()
    text = _norm(message)
    out: list[GpuRef] = []
    seen: set = set()
    for m in _alias_regex().finditer(text):
        a = m.group(1)
        names = aliases.get(a, set())
        if a in fam:
            vs = _variants(fam[a])
            # a one-variant family IS that one product (nothing to merge); otherwise the family, never one market
            ref = GpuRef("gpu", vs[0], a) if len(vs) == 1 else GpuRef("family", fam[a], a)
        elif len(names) == 1:
            ref = GpuRef("gpu", next(iter(names)), a)
        elif names:
            # "h100 pcie": the variant whose qualifiers are exactly the words given wins (not "PCIe NVLink")
            want = _quals(a.split())
            exact = [n for n in names if _quals(_norm(_VENDOR.sub("", n)).split()) - _model_words(n) == want - _model_words(n)]
            ref = GpuRef("gpu", exact[0], a) if len(exact) == 1 else GpuRef("ambiguous", None, a, sorted(names))
        else:
            continue
        key = (ref.kind, ref.name or a)
        if key not in seen:
            seen.add(key)
            out.append(ref)
    return out


def _model_words(name: str) -> frozenset:
    toks = _norm(_VENDOR.sub("", name)).split()
    cut = next((i for i, t in enumerate(toks) if _MEM.match(t)), len(toks))
    return frozenset(toks[:cut])


def _variants(fid: str) -> list[str]:
    import families

    return families.variants(fid)


# ---------------------------------------------------------------------------------------------------------
# Slots (WorkloadRequirements fields; only what the message states)
# ---------------------------------------------------------------------------------------------------------

_NUM = r"(\d+(?:[.,]\d+)?)"
_MODEL_NAMES = re.compile(
    r"\b((?:meta[- ]?)?llama[- ]?\d(?:\.\d)?(?:[- ]?\d+(?:\.\d+)?b)?(?:[- ](?:instruct|chat))?|llama|"
    r"mistral(?:[- ]\d+b)?|mixtral(?:[- ]\d+x\d+b)?|qwen\s?\d?(?:\.\d)?(?:[- ]\d+(?:\.\d+)?b)?|"
    r"gemma\s?\d?(?:[- ]\d+b)?|phi[- ]?\d(?:\.\d)?(?:[- ](?:mini|small|medium))?|deepseek(?:[- ][a-z0-9.]+)?|"
    r"falcon(?:[- ]\d+b)?|gpt[- ]?2|gpt[- ]?j|bert(?:[- ](?:base|large))?|t5(?:[- ](?:small|base|large))?|"
    r"stable diffusion(?: xl)?|sdxl|flux(?:\.1)?|whisper(?:[- ](?:small|medium|large))?|resnet[- ]?\d*|mnist)\b",
    re.I)
_HF_ID = re.compile(r"\b([A-Za-z][\w.-]{1,40}/[A-Za-z][\w.-]{1,80})\b")
_PARAMS = re.compile(r"(?<![\w.])(\d+)\s*x\s*(\d+(?:\.\d+)?)\s*b\b|(?<![\w.$])(\d+(?:\.\d+)?)\s*(b|bn|billion|m|million)\b"
                     r"(?:\s*(?:param|parameter)s?)?", re.I)
_MONEY = re.compile(r"\$\s*" + _NUM + r"\s*(k)?\b|(?<![\w.])" + _NUM + r"\s*(k)?\s*(?:usd|dollars?|bucks)\b", re.I)
_PER_HOUR = re.compile(r"^\s*(?:/|per|an|a|each)\s*(?:gpu[- ]?)?(?:h|hr|hour)\b|^\s*/\s*gpu[- ]?h", re.I)


def _f(s: str) -> float:
    return float(s.replace(",", ""))


def _set(d: dict, path: str, value) -> None:
    keys = path.split(".")
    for k in keys[:-1]:
        d = d.setdefault(k, {})
    d[keys[-1]] = value


def extract_slots(message: str) -> tuple[dict, list[str]]:
    """WorkloadRequirements-shaped dict from one message. Absent = not stated."""
    t = message
    low = message.lower()
    slots: dict = {}
    sig: list[str] = []

    # workload type
    if re.search(r"\bfine[- ]?tun|\bfinetun|\b(q?lora|peft|sft)\b|\binstruction[- ]tun", low):
        _set(slots, "workload_type", "fine_tuning"); sig.append("workload:fine_tuning")
    elif re.search(r"\b(pre[- ]?train(?:ing)?|train(?:ing)?|trains)\b", low):
        _set(slots, "workload_type", "training"); sig.append("workload:training")
    elif re.search(r"\bbatch (?:inference|job|processing)|\boffline inference\b", low):
        _set(slots, "workload_type", "batch"); sig.append("workload:batch")
    elif re.search(r"\b(inference|infer|serv(?:e|ing)|host(?:ing)?|deploy(?:ing)?|chatbot|generate|generation)\b", low):
        _set(slots, "workload_type", "inference"); sig.append("workload:inference")

    # training method
    if re.search(r"\bqlora\b", low):
        _set(slots, "training.method", "qlora")
    elif re.search(r"\blora\b|\bpeft\b", low):
        _set(slots, "training.method", "lora")
    elif re.search(r"\bfull(?:[- ]parameter)?[- ](?:fine[- ]?tun|finetun|train)|\bfull ft\b|\bfull\b(?= fine)", low):
        _set(slots, "training.method", "full")

    # model name / size
    hf = _HF_ID.search(t)
    mn = _MODEL_NAMES.search(t)
    if hf and not re.match(r"^(gpu|usd|\$)", hf.group(1), re.I) and not re.search(r"/(h|hr|hour|month|gpu)\b", hf.group(1), re.I):
        _set(slots, "model.name", hf.group(1)); sig.append("model:hf_id")
    elif mn:
        _set(slots, "model.name", mn.group(1).strip()); sig.append("model:name")
    for m in _PARAMS.finditer(t):
        if m.group(1):   # 8x7b: total parameters of a mixture of experts
            _set(slots, "model.parameters_b", round(_f(m.group(1)) * _f(m.group(2)), 3))
        else:
            v = _f(m.group(3))
            unit = m.group(4).lower()
            after = t[m.end():m.end() + 16].lower()
            if re.search(r"^\W*\s*(tokens?|samples?|examples?|rows?|users?|requests?|records?)\b", after):
                continue
            if unit in ("m", "million"):
                v = v / 1000
            _set(slots, "model.parameters_b", round(v, 3))
        sig.append("model:parameters_b")
        break

    # precision / quantization
    for pat, prec, quant in ((r"\b(4[- ]?bit|int4|nf4)\b", None, "int4"), (r"\b(8[- ]?bit|int8)\b", None, "int8"),
                             (r"\bgptq\b", None, "gptq"), (r"\bawq\b", None, "awq"), (r"\bfp8\b", "fp8", None),
                             (r"\bbf16\b", "bf16", None), (r"\bfp16\b", "fp16", None), (r"\bfp32\b", "fp32", None)):
        if re.search(pat, low):
            if prec:
                _set(slots, "precision", prec)
            if quant:
                _set(slots, "quantization", "nf4" if "nf4" in low else quant)
    if slots.get("training", {}).get("method") == "qlora" and "quantization" not in slots:
        pass   # QLoRA implies 4-bit; the estimator knows this from the method, nothing to add here

    # money: "$2/hour" -> max price per GPU-hour; "$50" / "under $50" / "50 dollars" -> total budget
    for m in _MONEY.finditer(t):
        raw = m.group(1) or m.group(3)
        k = m.group(2) or m.group(4)
        v = _f(raw) * (1000 if k else 1)
        if _PER_HOUR.search(t[m.end():m.end() + 16]):
            _set(slots, "constraints.max_price_per_gpu_hour", v); sig.append("money:per_hour")
        else:
            _set(slots, "constraints.budget_usd", v); sig.append("money:budget")

    # sequence / context length
    m = re.search(r"(\d+(?:\.\d+)?)\s*(k)?\s*(?:token\s*)?(?:context|ctx|seq(?:uence)?(?:[- ]len(?:gth)?)?)\b", low)
    if m:
        n = int(_f(m.group(1)) * (1024 if m.group(2) else 1))
        wt = slots.get("workload_type")
        _set(slots, "inference.context_len" if wt == "inference" else "training.seq_len", n)

    # dataset size
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(k|m|b|thousand|million|billion)?\s*(tokens)\b", low)
    if m:
        mult = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "b": 1e9, "billion": 1e9}.get(m.group(2) or "", 1)
        _set(slots, "training.dataset_tokens", int(_f(m.group(1)) * mult))
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(k|m|thousand|million)?\s*(samples|examples|rows|records|conversations|documents)\b", low)
    if m:
        mult = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6}.get(m.group(2) or "", 1)
        _set(slots, "training.dataset_samples", int(_f(m.group(1)) * mult))
    m = re.search(r"\b(\d+)\s*epochs?\b", low)
    if m:
        _set(slots, "training.epochs", int(m.group(1)))

    # inference load
    m = re.search(r"(\d+)\s*(?:concurrent|simultaneous|parallel)?\s*(?:users|requests|sessions|streams)\b(?:\s*at once)?", low)
    if m and re.search(r"concurrent|simultaneous|parallel|at once|users", low):
        _set(slots, "inference.concurrency", int(m.group(1)))
    for eng in ("vllm", "tgi", "llama.cpp", "transformers"):
        if eng in low:
            _set(slots, "inference.engine", eng)
            break

    # runtime ("for 10 hours"), never "per hour"
    m = re.search(r"\bfor\s+(\d+(?:\.\d+)?)\s*(hours|hrs|h|days)\b", low)
    if m:
        h = _f(m.group(1)) * (24 if m.group(2) == "days" else 1)
        _set(slots, "constraints.runtime_hours", h)

    # gpu count ("2x H100", "4 GPUs")
    m = re.search(r"\b(\d+)\s*(?:x\s*(?=[a-z])|gpus\b|cards\b)", low)
    if m and 0 < int(m.group(1)) <= 64:
        _set(slots, "hardware_hints.gpu_count", int(m.group(1)))

    # region / vendor / spot
    if re.search(r"\b(in the us|us[- ]based|in us|usa|united states|north america)\b", low):
        _set(slots, "constraints.region", "us")
    elif re.search(r"\b(europe|in the eu|eu[- ]based|in eu|gdpr)\b", low):
        _set(slots, "constraints.region", "eu")
    if re.search(r"\b(amd|rocm|instinct)\b", low):
        _set(slots, "constraints.vendor", "AMD")
    elif re.search(r"\b(nvidia|cuda)\b", low):
        _set(slots, "constraints.vendor", "NVIDIA")
    if re.search(r"\b(spot|interruptible|preemptible)\b", low):
        _set(slots, "constraints.allow_spot", True)
    return slots, sig


# ---------------------------------------------------------------------------------------------------------
# Intent
# ---------------------------------------------------------------------------------------------------------

_CHEAP = re.compile(r"\b(cheapest|lowest[- ]price[d]?|lowest cost|least expensive|best price|cheap(?:er)?|"
                    r"price of|prices? for|how much (?:is|does|for|are)|cost of|where (?:can i|to) (?:get|rent|buy))\b")
_COMPARE = re.compile(r"\b(compare|comparison|vs\.?|versus|difference between|better than|or the)\b")
_CHANGE = re.compile(r"\b(why|what (?:happened|caused|drove|is driving)|explain)\b.*\b(price|prices|pricing|cost|costs)\b|"
                     r"\b(price|prices|pricing)\b.*\b(increase[sd]?|rise|rising|rose|went up|go up|jump(?:ed)?|spike[sd]?|"
                     r"drop(?:ped)?|fall|fell|fallen|went down|change[sd]?|moved?|moving|surge[sd]?|plunge[sd]?)\b")
_SPECS = re.compile(r"\b(specs?|specifications?|how much (?:vram|memory)|vram (?:of|on)|memory bandwidth|tflops|"
                    r"hardware details|datasheet|tell me about|what is the (?:vram|memory))\b")
_WHAT_GPU = re.compile(r"\b(what|which|best|recommend(?:ed|ation)?|suggest|right|good|affordable|budget|cheap(?:est)?|"
                       r"enough)\b.*\b(gpus?|hardware|instance|card|setup)\b|\bhow much (?:vram|memory|gpu)\b.*\b(need|for|to)\b")
_CAN_RUN = re.compile(r"\b(can|could) i\b.*\b(run|train|fine[- ]?tune|finetune|serve|host|do|afford)\b|"
                      r"\b(within|under|below|for less than|on a budget of|budget of|with)\s*\$")


# ---------------------------------------------------------------------------------------------------------
# Job intents (demo, real_run, deploy_custom, jobs, cancel). Checked before the market rules, but a message that
# names a GPU together with a price / compare / specs word stays a market question ("cheapest h100").
# ---------------------------------------------------------------------------------------------------------

JOB_ID = re.compile(r"(?<![\w-])job[-_ ]?([0-9a-f]{8,32})(?![\w])", re.I)

# Casual / mistyped words are corrected ONLY for the job-intent rules (GPU names and slots always come from the
# original message): a fixed table of common typos, plus a close match (difflib, >= 0.85) for words of 5+ letters
# that are not themselves ordinary words close to a keyword ("terminal" is not "terminate").
_VOCAB = ("cancel", "status", "running", "simulate", "simulation", "simulated", "sandbox", "deploy", "training",
          "mnist", "example", "progress", "custom", "script", "container", "docker", "finished", "terminate",
          "tutorial", "demonstration", "abort", "happening", "really", "actual", "actually", "jobs", "model",
          "models", "weights")
_TYPOS = {"dmeo": "demo", "deom": "demo", "demoo": "demo", "dem0": "demo", "jbo": "job", "jbos": "jobs",
          "ojb": "job", "joob": "job", "mnsit": "mnist", "minst": "mnist", "mnis": "mnist", "mnst": "mnist",
          "canel": "cancel", "cancle": "cancel", "cancell": "cancel", "cancl": "cancel", "stauts": "status",
          "staus": "status", "statu": "status", "runing": "running", "kil": "kill", "stp": "stop", "relly": "really",
          "rael": "real", "reall": "real", "trian": "train", "trainig": "training", "traning": "training",
          "simualte": "simulate", "simluate": "simulate", "simulaton": "simulation", "deplpoy": "deploy",
          "depoly": "deploy", "deply": "deploy", "doker": "docker", "dokcer": "docker", "whats": "what is",
          "u": "you", "pls": "please", "plz": "please", "modle": "model", "modl": "model", "scirpt": "script"}
_KEEP = {"customer", "customers", "customize", "process", "processes", "terminal", "terminals", "exempla", "examine",
         "statue", "states", "status", "running", "trainer", "trainers", "modes", "modal", "contain", "contained",
         "actor", "actors", "simulator", "deployment", "deployments", "progressive", "abroad", "abort"}


def _fix(word: str) -> str:
    import difflib

    if word in _TYPOS:
        return _TYPOS[word]
    if len(word) < 5 or word in _VOCAB or word in _KEEP or not word.isalpha():
        return word
    m = difflib.get_close_matches(word, _VOCAB, n=1, cutoff=0.85)
    return m[0] if m else word


def _jobtext(message: str) -> str:
    """lowercase, apostrophes dropped, punctuation to spaces (job ids and file names kept), typos corrected."""
    low = (message or "").lower().replace("'", "").replace("’", "")
    low = re.sub(r"[^\w$.\- ]+", " ", low)
    low = re.sub(r"(?<!\w)[.\-]+|[.\-]+(?!\w)", " ", low)
    return " ".join(_fix(w) for w in low.split())


def find_job_id(message: str) -> str | None:
    """The first job id in the message, normalised to "job-<hex>" ("JOB-AB12CD34", "job ab12cd34" too)."""
    m = JOB_ID.search(message or "")
    return f"job-{m.group(1).lower()}" if m else None


_CANCEL = re.compile(r"\b(cancel|cancell?ed|cancell?ing|abort|aborted|kill|killed|terminate|halt|stop|stopped|"
                     r"shut (?:it |this |that |the job |my job )?(?:down|off)|shutdown|pull the plug)\b")
_JOB_NOUN = re.compile(r"\b(jobs?|runs?|training|trainings|demo|simulation|mnist|it|this|that|everything|all|"
                       r"instance|gpu|deployment|one)\b")
_PRONOUN = re.compile(r"\b(it|this|that|this job|that job|the job|that one|this one|same job|the run|the training)\b")
_STATUS = re.compile(
    r"\b(my|our) (jobs?|runs?|trainings?|training runs?|training jobs?|demos?|simulations?)\b|"
    r"\b(status|progress|state|update|eta)\b.*\b(jobs?|runs?|training|demo|simulation|it)\b|"
    r"\bjobs?\b.*\b(status|progress|doing|going|running|done|finished|failed|look|state)\b|"
    r"\bwhat is (running|happening|going on)\b|\b(anything|something) running\b|"
    r"\bis (it|my job|the job|my training|the training|my run|the run|the demo|my demo) (still )?"
    r"(done|finished|running|over|complete|completed|working|ready|stuck)\b|"
    r"\bhow (is|are|s) (it|my|the|things|everything)\b.*\b(going|doing|coming along|looking|progressing)\b|"
    r"\b(list|show|see|check)( me)? (all )?(my|the) (jobs?|runs?|trainings?)\b|"
    r"\b(did|has) (it|my job|the job|my training|my run|the demo) "
    r"(finish|finished|fail|failed|succeed|succeeded|complete|completed|work|start|started)\b|\bhow far along\b")
_CUSTOM = re.compile(
    r"\b(deploy|host|run|launch|serve|train|execute|upload|use|start|ship|bring|containerize|fine[- ]?tune|put)\b.*"
    r"\b(my|our|mine|own|custom|personal|company|team|private)\b.*\b(models?|code|scripts?|containers?|docker|"
    r"images?|apps?|application|project|repo|repository|llm|weights|checkpoint|notebook|workload|program|service|"
    r"api|pipeline|training|bot|chatbot|python|pytorch|tensorflow|jax|llama|mistral|qwen|gemma|network|stuff)\b|"
    r"\b(my|our|mine|own|custom)\b.*\b(models?|code|scripts?|containers?|docker|images?|weights|llm|"
    r"workload|notebook|repo)\b.*\b(on opengrid|deploy|host|run|launch|serve|train)\b|"
    r"\b(docker|dockerfile|container image|custom code|custom container|arbitrary code|bring your own|byo)\b|"
    r"\b(run|execute|launch) (a |my |this |our )?([\w-]+\.py|python script|script|notebook|jupyter)\b")
_REAL = re.compile(r"\b(real|actual|really|production|live|cloud|physical) (gpu|gpus|hardware|run|job|"
                   r"training|machine|instance|cloud|thing|deal|one)\b|\bfor real\b|\bfor actual\b|"
                   r"\b(not|no|without) (a )?(simulat\w*|sandbox|demo|fake)\b|"
                   r"\bactually (run|train|launch|deploy)\b|\breal\b.*\bmnist\b|\bmnist\b.*\breal\b")
_EXAMPLE = re.compile(r"\b(mnist|it|this|the demo|example|template|demo|test job|first (training|job|model)|"
                      r"simulation|digits?|that|training job|one)\b")
_DEPLOY_EXAMPLE = re.compile(r"\b(deploy|launch|ship)\b.*\b(mnist|example|template)\b")
_DEMO = re.compile(
    r"\b(demo|demos|demonstration|simulate|simulation|simulated|sandbox|tutorial|walkthrough|walk through)\b|"
    r"\btry (it|this|opengrid|something|out|a job|a training|training|you out)\b|\b(test|example|sample|first|"
    r"practice|dummy|toy|hello world|quick) (job|run|training|model|training run|workload)\b|"
    r"\bshow me how (it|this|opengrid|jobs?|training|everything) works?\b|\bshow me how\b|"
    r"\bhow (does|do) (it|this|opengrid|jobs?|a job|training jobs?|training) work\b|\bmnist\b|"
    r"\b(see|watch) (a |an )?(job|training|run)\b|\bwhat can (i|you) run\b")


def _job_intent(message: str, low: str, slots: dict, gpus) -> tuple[str, str, str] | None:
    """(intent, confidence, rule) for a job-management message, else None (the market rules then apply)."""
    t = _jobtext(message)
    jid = find_job_id(message)
    market = bool(gpus) and bool(_CHEAP.search(low) or _COMPARE.search(low) or _SPECS.search(low)
                                 or _CHANGE.search(low))
    if _CANCEL.search(t) and (jid or _JOB_NOUN.search(t) or len(t.split()) <= 3) and not market:
        return "cancel", "high", "rule:cancel"
    if jid:
        return "jobs", "high", "rule:job_id"
    if market:
        return None
    mnist = re.search(r"\bmnist\b", t)
    if _CUSTOM.search(t) and not mnist:
        return "deploy_custom", "high", "rule:deploy_custom"
    if _REAL.search(t) and (_EXAMPLE.search(t) or re.search(r"\b(run|train|launch|deploy|start|try)\b", t)) \
            and not _get_path(slots, "model.parameters_b") and not (_get_path(slots, "model.name") and not mnist):
        return "real_run", "high", "rule:real_run"
    if _DEPLOY_EXAMPLE.search(t):
        return "real_run", "medium", "rule:deploy_example"
    if _STATUS.search(t):
        return "jobs", "high", "rule:jobs_status"
    if _DEMO.search(t):
        if re.search(r"\b(which|what|best|recommend\w*) (gpu|hardware|card)\b", low) and \
                not re.search(r"\b(demo|simulat\w*|sandbox|try)\b", t):
            return None      # "which GPU for MNIST?" stays a sizing question
        return "demo", "high", "rule:demo"
    if re.fullmatch(r"(my |show |list )?(jobs?|runs?)( please)?|status", t):
        return "jobs", "medium", "rule:jobs_bare"
    return None


def _get_path(d: dict, path: str):
    for k in path.split("."):
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def parse(message: str) -> Parsed:
    low = " ".join((message or "").lower().split())
    slots, sig = extract_slots(message or "")
    gpus = find_gpus(message or "")
    workloadish = bool(slots.get("workload_type") or slots.get("model") or slots.get("training"))

    jid = find_job_id(message or "")
    pron = bool(_PRONOUN.search(_jobtext(message or "")))

    def out(intent, conf, *why):
        return Parsed(intent, slots, gpus, conf, sig + list(why), job_id=jid, pronoun=pron)

    job = _job_intent(message or "", low, slots, gpus)
    if job:
        return out(*job)

    if _CHANGE.search(low):
        return out("price_change", "high" if gpus else "medium", "rule:price_change")
    if _COMPARE.search(low) and (len(gpus) >= 2 or re.search(r"\bcompare\b", low)):
        return out("compare", "high" if len(gpus) >= 2 else "medium", "rule:compare")
    if gpus and _SPECS.search(low) and not workloadish:
        return out("specs", "high", "rule:specs")
    if workloadish and (_CAN_RUN.search(low) and slots.get("constraints", {}).get("budget_usd") is not None):
        return out("can_run", "high", "rule:can_run")
    if workloadish:
        conf = "high" if (_WHAT_GPU.search(low) or slots.get("model")) else "medium"
        return out("recommend", conf, "rule:workload")
    if gpus and _CHEAP.search(low):
        return out("cheapest", "high", "rule:cheapest")
    if gpus and re.search(r"\b(available|availability|in stock|rent|price|prices|pricing|cost|\$)\b", low):
        return out("cheapest", "medium", "rule:gpu_price_words")
    if _WHAT_GPU.search(low):
        return out("recommend", "medium", "rule:what_gpu")
    if gpus and len(gpus) == 1 and len(low.split()) <= 4:
        return out("cheapest", "medium", "rule:bare_gpu")   # "H100?" -> its current market
    return out("unknown", "low", "rule:fallback")
