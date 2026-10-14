"""Well-known open model configurations, for filling what a request does not state.

    lookup(name) -> {"entry": dict, "match": "exact"|"loose", "candidates": [ids]} | None
    get(model_id) -> dict | None
    all_models() -> [dict]

Every number comes from the model's published config.json / model card / paper (the `source` URL). A value we
could not confirm is None, never guessed. parameters_b is the total parameter count in billions as stated by the
publisher (MoE: total, with active_parameters_b alongside).

kind
    decoder    decoder-only transformer (LLM): KV cache and the transformer activation formula apply
    encoder    encoder-only transformer (BERT): the activation formula applies, no KV cache
    moe        mixture-of-experts decoder: all expert weights are resident; active params drive compute only
    vit        vision transformer: the activation formula applies with seq = patches + 1
    cnn        convolutional network: activations from `activation_elements_per_sample` (see below)
    diffusion  latent diffusion pipeline: weights only; activation memory is not modelled

ResNet-50 activation elements are COMPUTED from the architecture (He et al. 2016, Table 1; torchvision v1.5
strides) at 224x224: the sum of the output elements of every conv, batch-norm, pooling and residual-add layer
(27,948,032 per image; ReLU is in place in torchvision and shares its input's storage). Counting the ReLU outputs
separately as well gives 37,556,736 (the `_high` figure). The same count reproduces torchvision's 25,557,032
parameters exactly, which checks the layer shapes. It is an architectural count, not a measurement.
"""

from __future__ import annotations

import re

HF = "https://huggingface.co/{}/blob/main/config.json"

_CATALOG: list[dict] = []


def _m(id_, family, version, size, kind, params, *, layers=None, hidden=None, heads=None, kv=None, head_dim=None,
       vocab=None, ctx=None, active=None, aliases=(), source=None, paper=None, note=None, **extra):
    _CATALOG.append({
        "id": id_, "family": family, "version": version, "size": size, "kind": kind, "parameters_b": params,
        "active_parameters_b": active, "num_layers": layers, "hidden_size": hidden, "num_attention_heads": heads,
        "num_kv_heads": kv, "head_dim": head_dim, "vocab_size": vocab, "max_context": ctx,
        "aliases": list(aliases), "source": source or HF.format(id_), "paper": paper, "note": note, **extra})


LLAMA3 = "https://arxiv.org/abs/2407.21783"
LLAMA2 = "https://arxiv.org/abs/2307.09288"
QWEN25 = "https://arxiv.org/abs/2412.15115"
GEMMA2 = "https://arxiv.org/abs/2408.00118"
PHI3 = "https://arxiv.org/abs/2404.14219"

# ---------------------------------------------------------------- Llama
_m("meta-llama/Llama-3.2-1B", "llama", "3.2", "1b", "decoder", 1.23, layers=16, hidden=2048, heads=32, kv=8,
   head_dim=64, vocab=128256, ctx=131072, paper=LLAMA3, aliases=["meta-llama/Llama-3.2-1B-Instruct"])
_m("meta-llama/Llama-3.2-3B", "llama", "3.2", "3b", "decoder", 3.21, layers=28, hidden=3072, heads=24, kv=8,
   head_dim=128, vocab=128256, ctx=131072, paper=LLAMA3, aliases=["meta-llama/Llama-3.2-3B-Instruct"])
_m("meta-llama/Llama-3.1-8B", "llama", "3.1", "8b", "decoder", 8.03, layers=32, hidden=4096, heads=32, kv=8,
   head_dim=128, vocab=128256, ctx=131072, paper=LLAMA3,
   aliases=["meta-llama/Llama-3.1-8B-Instruct", "meta-llama/Meta-Llama-3.1-8B", "meta-llama/Meta-Llama-3.1-8B-Instruct"])
_m("meta-llama/Llama-3.1-70B", "llama", "3.1", "70b", "decoder", 70.6, layers=80, hidden=8192, heads=64, kv=8,
   head_dim=128, vocab=128256, ctx=131072, paper=LLAMA3,
   aliases=["meta-llama/Llama-3.1-70B-Instruct", "meta-llama/Meta-Llama-3.1-70B", "meta-llama/Meta-Llama-3.1-70B-Instruct"])
_m("meta-llama/Llama-3.1-405B", "llama", "3.1", "405b", "decoder", 405.0, layers=126, hidden=16384, heads=128, kv=8,
   head_dim=128, vocab=128256, ctx=131072, paper=LLAMA3,
   aliases=["meta-llama/Llama-3.1-405B-Instruct", "meta-llama/Meta-Llama-3.1-405B"])
_m("meta-llama/Meta-Llama-3-8B", "llama", "3", "8b", "decoder", 8.03, layers=32, hidden=4096, heads=32, kv=8,
   head_dim=128, vocab=128256, ctx=8192, paper=LLAMA3, aliases=["meta-llama/Meta-Llama-3-8B-Instruct"])
_m("meta-llama/Meta-Llama-3-70B", "llama", "3", "70b", "decoder", 70.6, layers=80, hidden=8192, heads=64, kv=8,
   head_dim=128, vocab=128256, ctx=8192, paper=LLAMA3, aliases=["meta-llama/Meta-Llama-3-70B-Instruct"])
_m("meta-llama/Llama-2-7b-hf", "llama", "2", "7b", "decoder", 6.74, layers=32, hidden=4096, heads=32, kv=32,
   head_dim=128, vocab=32000, ctx=4096, paper=LLAMA2, aliases=["meta-llama/Llama-2-7b-chat-hf"])
_m("meta-llama/Llama-2-13b-hf", "llama", "2", "13b", "decoder", 13.0, layers=40, hidden=5120, heads=40, kv=40,
   head_dim=128, vocab=32000, ctx=4096, paper=LLAMA2, aliases=["meta-llama/Llama-2-13b-chat-hf"])
_m("meta-llama/Llama-2-70b-hf", "llama", "2", "70b", "decoder", 69.0, layers=80, hidden=8192, heads=64, kv=8,
   head_dim=128, vocab=32000, ctx=4096, paper=LLAMA2, aliases=["meta-llama/Llama-2-70b-chat-hf"])

# ---------------------------------------------------------------- Mistral
_m("mistralai/Mistral-7B-v0.1", "mistral", "0.1", "7b", "decoder", 7.24, layers=32, hidden=4096, heads=32, kv=8,
   head_dim=128, vocab=32000, ctx=32768, paper="https://arxiv.org/abs/2310.06825",
   aliases=["mistralai/Mistral-7B-Instruct-v0.1"])
_m("mistralai/Mixtral-8x7B-v0.1", "mixtral", "0.1", "8x7b", "moe", 46.7, active=12.9, layers=32, hidden=4096,
   heads=32, kv=8, head_dim=128, vocab=32000, ctx=32768, paper="https://arxiv.org/abs/2401.04088",
   aliases=["mistralai/Mixtral-8x7B-Instruct-v0.1"],
   note="all 8 experts are resident in memory (46.7B); 12.9B parameters are active per token")

# ---------------------------------------------------------------- Qwen2.5 (Q heads / KV heads per the model cards)
for _size, _p, _l, _h, _a, _kv, _v, _c in (("0.5B", 0.49, 24, 896, 14, 2, 151936, 32768),
                                          ("1.5B", 1.54, 28, 1536, 12, 2, 151936, 32768),
                                          ("3B", 3.09, 36, 2048, 16, 2, 151936, 32768),
                                          ("7B", 7.61, 28, 3584, 28, 4, 152064, 131072),
                                          ("14B", 14.7, 48, 5120, 40, 8, 152064, 131072),
                                          ("32B", 32.5, 64, 5120, 40, 8, 152064, 131072),
                                          ("72B", 72.7, 80, 8192, 64, 8, 152064, 131072)):
    _m(f"Qwen/Qwen2.5-{_size}", "qwen", "2.5", _size.lower(), "decoder", _p, layers=_l, hidden=_h, heads=_a, kv=_kv,
       head_dim=_h // _a, vocab=_v, ctx=_c, paper=QWEN25, aliases=[f"Qwen/Qwen2.5-{_size}-Instruct"])

# ---------------------------------------------------------------- Gemma 2
_m("google/gemma-2-2b", "gemma", "2", "2b", "decoder", 2.61, layers=26, hidden=2304, heads=8, kv=4, head_dim=256,
   vocab=256000, ctx=8192, paper=GEMMA2, aliases=["google/gemma-2-2b-it"])
_m("google/gemma-2-9b", "gemma", "2", "9b", "decoder", 9.24, layers=42, hidden=3584, heads=16, kv=8, head_dim=256,
   vocab=256000, ctx=8192, paper=GEMMA2, aliases=["google/gemma-2-9b-it"])
_m("google/gemma-2-27b", "gemma", "2", "27b", "decoder", 27.2, layers=46, hidden=4608, heads=32, kv=16, head_dim=128,
   vocab=256000, ctx=8192, paper=GEMMA2, aliases=["google/gemma-2-27b-it"])

# ---------------------------------------------------------------- Phi-3
_m("microsoft/Phi-3-mini-4k-instruct", "phi", "3", "mini", "decoder", 3.82, layers=32, hidden=3072, heads=32, kv=32,
   head_dim=96, vocab=32064, ctx=4096, paper=PHI3, aliases=["phi-3-mini"])
_m("microsoft/Phi-3-medium-4k-instruct", "phi", "3", "medium", "decoder", 14.0, layers=40, hidden=5120, heads=40,
   kv=10, head_dim=128, vocab=32064, ctx=4096, paper=PHI3, aliases=["phi-3-medium"])

# ---------------------------------------------------------------- GPT-2, BERT
_GPT2 = "https://cdn.openai.com/better-language-models/language_models_are_unsupervised_multitask_learners.pdf"
_m("openai-community/gpt2", "gpt2", None, "124m", "decoder", 0.124, layers=12, hidden=768, heads=12, kv=12,
   head_dim=64, vocab=50257, ctx=1024, paper=_GPT2, aliases=["gpt2"])
_m("openai-community/gpt2-xl", "gpt2", None, "1.5b", "decoder", 1.56, layers=48, hidden=1600, heads=25, kv=25,
   head_dim=64, vocab=50257, ctx=1024, paper=_GPT2, aliases=["gpt2-xl"])
_BERT = "https://arxiv.org/abs/1810.04805"
_m("google-bert/bert-base-uncased", "bert", None, "base", "encoder", 0.110, layers=12, hidden=768, heads=12, kv=12,
   head_dim=64, vocab=30522, ctx=512, paper=_BERT, aliases=["bert-base-uncased", "bert-base-cased", "bert-base"])
_m("google-bert/bert-large-uncased", "bert", None, "large", "encoder", 0.340, layers=24, hidden=1024, heads=16, kv=16,
   head_dim=64, vocab=30522, ctx=512, paper=_BERT, aliases=["bert-large-uncased", "bert-large-cased", "bert-large"])

# ---------------------------------------------------------------- vision / diffusion
_m("microsoft/resnet-50", "resnet", None, "50", "cnn", 0.025557, paper="https://arxiv.org/abs/1512.03385",
   source="https://pytorch.org/vision/stable/models/generated/torchvision.models.resnet50.html",
   aliases=["resnet50", "resnet-50", "torchvision/resnet50", "timm/resnet50.a1_in1k"],
   input_resolution=224, activation_elements_per_sample=27_948_032, activation_elements_per_sample_high=37_556_736,
   note="25,557,032 parameters (torchvision); activation elements computed from the architecture at 224x224")
_m("google/vit-base-patch16-224", "vit", None, "base", "vit", 0.0866, layers=12, hidden=768, heads=12, kv=12,
   head_dim=64, ctx=197, paper="https://arxiv.org/abs/2010.11929", aliases=["vit-base", "vit-b/16", "vit_b_16"],
   input_resolution=224, note="sequence = 196 patches of 16x16 at 224x224 + 1 class token = 197")
_m("stabilityai/stable-diffusion-xl-base-1.0", "sdxl", "1.0", "base", "diffusion", 3.5,
   paper="https://arxiv.org/abs/2307.01952",
   source="https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0",
   aliases=["sdxl", "stable-diffusion-xl", "sdxl-base"],
   note="2.6B UNet + 0.817B text encoders (paper) + VAE, about 3.5B total; activation memory not modelled")

_FAMILY_WORDS = {
    "llama": "llama", "mistral": "mistral", "mixtral": "mixtral", "qwen": "qwen", "gemma": "gemma", "phi": "phi",
    "gpt2": "gpt2", "gpt-2": "gpt2", "bert": "bert", "resnet": "resnet", "vit": "vit", "sdxl": "sdxl",
}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def get(model_id: str) -> dict | None:
    want = _norm(model_id)
    for e in _CATALOG:
        if want == e["id"].lower() or want in (a.lower() for a in e["aliases"]):
            return _copy(e)
    return None


def all_models() -> list[dict]:
    return [_copy(e) for e in _CATALOG]


def _copy(e: dict) -> dict:
    return {**e, "aliases": list(e["aliases"])}


_SIZE = re.compile(r"(?<![\w.])(\d+x)?(\d+(?:\.\d+)?)\s*([bm])(?![a-z])")


def lookup(name: str | None) -> dict | None:
    """An exact HF id / alias match, else a loose one ("7B llama", "llama 3.1 70b", "qwen2.5 7b instruct").

    Loose matching needs the family word and, when the text has one, the size; a version number in the text
    must match too. Several survivors: the newest version wins and every candidate is listed (match "loose").
    """
    if not name or not name.strip():
        return None
    hit = get(name)
    if hit is not None:
        return {"entry": hit, "match": "exact", "candidates": [hit["id"]]}
    text = _norm(name)
    tail = text.rsplit("/", 1)[-1]
    families = {fam for word, fam in _FAMILY_WORDS.items() if word in text}
    if "mixtral" in families:
        families.discard("mistral")
    if not families:
        return None
    size = None
    flat = tail.replace("-", " ").replace("_", " ")
    m = _SIZE.search(flat)
    if m:
        size = f"{m.group(1) or ''}{m.group(2)}{m.group(3)}"
        flat = flat[:m.start()] + " " + flat[m.end():]
    else:
        for word in ("mini", "medium", "base", "large", "xl"):
            if re.search(rf"\b{word}\b", flat):
                size = word
                break
    versions = set(re.findall(r"(?<![\d.])(\d+(?:\.\d+)?)(?![\d.])", flat))
    pool = []
    for e in _CATALOG:
        if e["family"] not in families:
            continue
        if size and e["size"] != size:
            continue
        if versions and e["version"] and not any(v == e["version"] or e["version"].startswith(v + ".")
                                                 for v in versions):
            continue
        pool.append(e)
    if not pool:
        return None

    def key(e):
        try:
            v = tuple(int(x) for x in (e["version"] or "0").split("."))
        except ValueError:
            v = (0,)
        return (e["version"] in versions, v)

    pool.sort(key=key, reverse=True)
    return {"entry": _copy(pool[0]), "match": "loose", "candidates": [e["id"] for e in pool]}
