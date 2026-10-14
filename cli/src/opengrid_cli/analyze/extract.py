"""Extractors for every allowlisted file kind except Python (see pyextract)."""

from __future__ import annotations

import configparser
import json
import re
import shlex
from typing import Any

from . import yamlish
from .facts import (GPU_NAMES, P_CODE, P_CONFIG, P_DEFAULT, P_README, P_TRAINCFG, PACKAGES, Collector, apply_key,
                    as_int, map_dtype, map_quant, readme_model_candidates)

try:
    import tomllib
except ImportError:  # pragma: no cover - Python < 3.11
    tomllib = None  # type: ignore[assignment]

_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._\-]*)(\[[^\]]*\])?\s*(.*)$")
_CU_PIN = re.compile(r"(?i)\+cu(\d{2,3})\b|/whl/cu(\d{2,3})\b|\bcu(\d{3})\b")


def _cuda_from_cu(digits: str) -> str:
    return f"{digits[:-1]}.{digits[-1]}" if len(digits) == 3 else f"{digits[0]}.{digits[1]}"


def _locate(lines: list[str], path: tuple, start: int = 0) -> int:
    """Best-effort 1-based line for a JSON/TOML key path."""
    pos = start
    for key in path:
        if isinstance(key, int):
            continue
        pat = re.compile(r'["\']?' + re.escape(str(key)) + r'["\']?\s*[:=\]]|\[\s*' + re.escape(str(key)))
        for i in range(pos, len(lines)):
            if pat.search(lines[i]):
                pos = i
                break
    return pos + 1


# ------------------------------------------------------------------------------------------------
# Dependencies

def dependency(c: Collector, name: str, spec: str, src: str) -> None:
    norm = name.lower().replace("_", "-")
    info = PACKAGES.get(norm) or PACKAGES.get(name.lower())
    if norm.startswith("nvidia-") and norm.endswith("-cu12"):
        c.fact("hardware_hints.cuda_min", "12.0", src, "low", P_DEFAULT)
    if norm.startswith("cupy-cuda"):
        c.framework("cupy", src)
        m = re.search(r"cuda(\d+)", norm)
        if m:
            c.fact("hardware_hints.cuda_min", f"{m.group(1)[:2]}.0" if len(m.group(1)) >= 2 else None, src, "low",
                   P_DEFAULT)
        return
    if not info:
        return
    cat, canon = info
    if cat == "web":
        c.signal("web_framework", canon, src)
    else:
        c.framework(canon, src)
        if cat == "cpu_ml":
            c.signal("cpu_ml", canon, src)
    m = _CU_PIN.search(spec)
    if m and canon == "pytorch":
        c.fact("hardware_hints.cuda_min", _cuda_from_cu(next(g for g in m.groups() if g)), src, "medium", P_CODE)


def requirements(c: Collector, rel: str, text: str) -> None:
    for i, raw in enumerate(text.splitlines(), 1):
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        src = f"{rel}:{i}"
        if line.startswith("-"):
            m = _CU_PIN.search(line)
            if m and ("pytorch" in line or "torch" in line):
                c.fact("hardware_hints.cuda_min", _cuda_from_cu(next(g for g in m.groups() if g)), src, "medium",
                       P_CODE)
            continue
        if "://" in line and "@" not in line:
            continue
        if " @ " in line:
            line = line.split(" @ ", 1)[0]
        m = _REQ_NAME.match(line)
        if m:
            dependency(c, m.group(1), m.group(3) or "", src)


def _dep_strings(c: Collector, rel: str, text: str, deps: list[Any]) -> None:
    lines = text.splitlines()
    for d in deps:
        if not isinstance(d, str):
            continue
        m = _REQ_NAME.match(d)
        if not m:
            continue
        ln = next((i for i, l in enumerate(lines, 1) if m.group(1) in l), 1)
        dependency(c, m.group(1), m.group(3) or "", f"{rel}:{ln}")


def pyproject(c: Collector, rel: str, text: str) -> None:
    if tomllib is None:
        return
    try:
        data = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError):
        c.warn(f"{rel}: could not parse TOML")
        return
    deps: list[Any] = []
    proj = data.get("project", {}) if isinstance(data.get("project"), dict) else {}
    deps += proj.get("dependencies", []) or []
    for group in (proj.get("optional-dependencies") or {}).values():
        deps += group or []
    for group in (data.get("dependency-groups") or {}).values():
        deps += [g for g in group if isinstance(g, str)]
    poetry = (data.get("tool") or {}).get("poetry") or {}
    for key in ("dependencies", "dev-dependencies"):
        deps += list((poetry.get(key) or {}).keys())
    for grp in (poetry.get("group") or {}).values():
        deps += list(((grp or {}).get("dependencies") or {}).keys())
    _dep_strings(c, rel, text, deps)
    uv_sources = ((data.get("tool") or {}).get("uv") or {}).get("index") or []
    for idx in uv_sources if isinstance(uv_sources, list) else []:
        url = idx.get("url", "") if isinstance(idx, dict) else ""
        m = _CU_PIN.search(url)
        if m:
            c.fact("hardware_hints.cuda_min", _cuda_from_cu(next(g for g in m.groups() if g)),
                   f"{rel}:{_locate(text.splitlines(), ('index',))}", "medium", P_CODE)


def pipfile(c: Collector, rel: str, text: str) -> None:
    if tomllib is None:
        return
    try:
        data = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError):
        c.warn(f"{rel}: could not parse TOML")
        return
    deps = list((data.get("packages") or {}).keys()) + list((data.get("dev-packages") or {}).keys())
    _dep_strings(c, rel, text, deps)


def setup_cfg(c: Collector, rel: str, text: str) -> None:
    cp = configparser.ConfigParser(interpolation=None)
    try:
        cp.read_string(text)
    except configparser.Error:
        c.warn(f"{rel}: could not parse setup.cfg")
        return
    deps: list[str] = []
    if cp.has_option("options", "install_requires"):
        deps += [d.strip() for d in cp.get("options", "install_requires").splitlines() if d.strip()]
    if cp.has_section("options.extras_require"):
        for _, v in cp.items("options.extras_require"):
            deps += [d.strip() for d in v.splitlines() if d.strip()]
    _dep_strings(c, rel, text, deps)


def setup_py(c: Collector, rel: str, text: str) -> None:
    """setup.py is parsed with ast (never executed): string literals inside install_requires/extras."""
    import ast
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        c.warn(f"{rel}: could not parse setup.py")
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg in ("install_requires", "extras_require", "requires"):
            for sub in ast.walk(node.value):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    m = _REQ_NAME.match(sub.value)
                    if m:
                        dependency(c, m.group(1), m.group(3) or "", f"{rel}:{sub.lineno}")


def conda(c: Collector, rel: str, text: str) -> None:
    docs, locs = yamlish.parse(text)
    doc = docs[0] if docs else None
    if not isinstance(doc, dict):
        return
    for i, dep in enumerate(doc.get("dependencies") or []):
        ln = locs.get((0, "dependencies", i), 1)
        if isinstance(dep, str):
            m = re.match(r"^(?:[\w\-]+::)?([A-Za-z0-9_.\-]+)\s*[=<>!~]*\s*([\w.*]*)", dep)
            if not m:
                continue
            name, ver = m.group(1).lower(), m.group(2)
            src = f"{rel}:{ln}"
            if name in ("pytorch-cuda", "cudatoolkit", "cuda-version", "cuda-toolkit", "cuda") and ver:
                v = re.match(r"(\d+)\.(\d+)", ver)
                if v:
                    c.fact("hardware_hints.cuda_min", f"{v.group(1)}.{v.group(2)}", src, "medium", P_CODE)
            dependency(c, name, ver, src)
        elif isinstance(dep, dict):
            for j, p in enumerate(dep.get("pip") or []):
                if isinstance(p, str):
                    m = _REQ_NAME.match(p)
                    if m:
                        dependency(c, m.group(1), m.group(3) or "", f"{rel}:{locs.get((0, 'dependencies', i, 'pip', j), ln)}")


def package_json(c: Collector, rel: str, text: str) -> None:
    try:
        data = json.loads(text)
    except ValueError:
        return
    if not isinstance(data, dict):
        return
    lines = text.splitlines()
    for key in ("dependencies", "devDependencies"):
        for name in (data.get(key) or {}):
            n = str(name).lower()
            src = f"{rel}:{_locate(lines, (key, name))}"
            if n in ("express", "next", "react", "vue", "koa", "fastify", "@nestjs/core", "svelte", "nuxt"):
                c.signal("web_framework", n, src)
            elif n in ("@tensorflow/tfjs-node-gpu", "onnxruntime-node"):
                c.framework(n, src)
    c.signal("node_manifest", "package.json", f"{rel}:1")


# ------------------------------------------------------------------------------------------------
# Config files

def _apply_tree(c: Collector, rel: str, tree: Any, locate, confidence: str, prio: int) -> int:
    n = 0
    for path, value in yamlish.flatten(tree):
        if isinstance(value, list):
            continue
        before = len(c.facts) + len(c.signals)
        apply_key(c, path, value, f"{rel}:{locate(path)}", confidence, prio)
        n += (len(c.facts) + len(c.signals)) - before
    return n


def _json(c: Collector, rel: str, text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        c.warn(f"{rel}: could not parse JSON")
        return None


def hf_config(c: Collector, rel: str, text: str) -> None:
    data = _json(c, rel, text)
    if not isinstance(data, dict):
        return
    lines = text.splitlines()

    def src(*path: str) -> str:
        return f"{rel}:{_locate(lines, path)}"

    if not ("model_type" in data or "architectures" in data):
        # not a Hugging Face model config; maybe a training config with familiar keys
        _apply_tree(c, rel, data, lambda p: _locate(lines, p), "medium", P_TRAINCFG)
        return
    c.signal("hf_model_config", data.get("model_type") or "unknown", src("model_type"))
    if isinstance(data.get("model_type"), str):
        c.fact("model.architecture", data["model_type"], src("model_type"), "high", P_CONFIG)
    name = data.get("_name_or_path")
    if isinstance(name, str) and name and not name.startswith(("/", ".")):
        c.fact("model.name", name, src("_name_or_path"), "high", P_CONFIG)
    text_cfg = data.get("text_config") if isinstance(data.get("text_config"), dict) else {}
    merged = {**text_cfg, **{k: v for k, v in data.items() if not isinstance(v, dict)}}
    fields = [
        ("model.num_layers", ("num_hidden_layers", "n_layer", "num_layers", "n_layers")),
        ("model.hidden_size", ("hidden_size", "n_embd", "d_model", "dim")),
        ("model.num_attention_heads", ("num_attention_heads", "n_head", "num_heads", "n_heads")),
        ("model.num_kv_heads", ("num_key_value_heads", "num_kv_heads", "n_kv_heads", "multi_query_group_num")),
        ("model.head_dim", ("head_dim",)),
        ("model.vocab_size", ("vocab_size", "padded_vocab_size")),
        ("model.max_context", ("max_position_embeddings", "n_positions", "max_sequence_length", "seq_length",
                               "n_ctx")),
    ]
    found: dict[str, int] = {}
    for fld, keys in fields:
        for k in keys:
            n = as_int(merged.get(k))
            if n:
                c.fact(fld, n, src(k), "high", P_CONFIG)
                found[fld] = n
                break
    if "model.head_dim" not in found and found.get("model.hidden_size") and found.get("model.num_attention_heads"):
        h, a = found["model.hidden_size"], found["model.num_attention_heads"]
        if h % a == 0:
            c.fact("model.head_dim", h // a, src("hidden_size"), "medium", P_CONFIG)
    for k in ("num_parameters", "n_params", "num_params", "total_params"):
        n = merged.get(k)
        if isinstance(n, (int, float)) and not isinstance(n, bool) and n > 1e5:
            c.fact("model.parameters_b", round(n / 1e9, 3), src(k), "high", P_CONFIG)
    for k in ("num_local_experts", "num_experts", "n_routed_experts"):
        n = as_int(merged.get(k))
        if n and n > 1:
            c.signal("moe", n, src(k))
    dt = map_dtype(data.get("torch_dtype") or data.get("dtype"))
    if dt:
        c.fact("precision", dt, src("torch_dtype" if "torch_dtype" in data else "dtype"), "medium", P_CONFIG - 2)
    qc = data.get("quantization_config")
    if isinstance(qc, dict):
        q = map_quant(qc.get("quant_method"))
        if qc.get("load_in_4bit"):
            q = "nf4" if qc.get("bnb_4bit_quant_type") == "nf4" else "int4"
        elif qc.get("load_in_8bit"):
            q = "int8"
        if q:
            c.fact("quantization", q, src("quantization_config"), "high", P_CONFIG)


def adapter_config(c: Collector, rel: str, text: str) -> None:
    data = _json(c, rel, text)
    if not isinstance(data, dict):
        return
    lines = text.splitlines()
    pt = str(data.get("peft_type", "")).upper()
    if pt in ("LORA", "ADALORA", "LOHA", "LOKR"):
        c.signal("peft_lora", f"adapter_config peft_type {pt}", f"{rel}:{_locate(lines, ('peft_type',))}")
        c.fact("training.method", "lora", f"{rel}:{_locate(lines, ('peft_type',))}", "medium", P_CONFIG)
    r = as_int(data.get("r"))
    if r:
        c.fact("training.lora_rank", r, f"{rel}:{_locate(lines, ('r',))}", "high", P_CONFIG)
    base = data.get("base_model_name_or_path")
    if isinstance(base, str) and base:
        c.fact("model.name", base, f"{rel}:{_locate(lines, ('base_model_name_or_path',))}", "high", P_CONFIG)
    c.framework("peft", f"{rel}:1")


def generation_config(c: Collector, rel: str, text: str) -> None:
    data = _json(c, rel, text)
    if isinstance(data, dict):
        c.signal("generation_config", "present", f"{rel}:1")


def deepspeed(c: Collector, rel: str, text: str) -> None:
    data = _json(c, rel, text)
    if not isinstance(data, dict):
        return
    lines = text.splitlines()
    c.framework("deepspeed", f"{rel}:1")
    c.signal("deepspeed_config", rel, f"{rel}:1")
    _apply_tree(c, rel, data, lambda p: _locate(lines, p), "high", P_CONFIG)


def accelerate(c: Collector, rel: str, text: str) -> None:
    docs, locs = yamlish.parse(text)
    doc = docs[0] if docs else None
    if not isinstance(doc, dict):
        return
    if not any(k in doc for k in ("distributed_type", "compute_environment", "num_processes", "mixed_precision")):
        config_file(c, rel, text)
        return
    c.framework("accelerate", f"{rel}:1")
    c.signal("accelerate_config", rel, f"{rel}:1")
    if str(doc.get("distributed_type", "")).upper() == "DEEPSPEED":
        c.framework("deepspeed", f"{rel}:{locs.get((0, 'distributed_type'), 1)}")
    if str(doc.get("distributed_type", "")).upper() == "FSDP" or "fsdp_config" in doc:
        c.signal("fsdp", "accelerate", f"{rel}:{locs.get((0, 'distributed_type'), 1)}")
    _apply_tree(c, rel, doc, lambda p: locs.get((0,) + p, 1), "high", P_CONFIG)


def opengrid_manifest(c: Collector, rel: str, text: str) -> None:
    if rel.lower().endswith(".json"):
        data = _json(c, rel, text)
        lines = text.splitlines()
        if data is not None:
            _apply_tree(c, rel, data, lambda p: _locate(lines, p), "high", P_CONFIG)
        return
    docs, locs = yamlish.parse(text)
    for di, doc in enumerate(docs):
        _apply_tree(c, rel, doc, lambda p, di=di: locs.get((di,) + p, 1), "high", P_CONFIG)


_TRAIN_CFG_HINTS = re.compile(r"(?i)\b(base_model|model_name_or_path|lora_r|lora_rank|learning_rate|lr|"
                              r"micro_batch_size|per_device_train_batch_size|num_epochs|epochs|optimizer|"
                              r"sequence_len|max_seq_len|cutoff_len|finetuning_type|_component_|deepspeed|"
                              r"gradient_accumulation_steps|max_model_len|tensor_parallel_size)\b")


def config_file(c: Collector, rel: str, text: str) -> None:
    """YAML/JSON under config(s)/ or named like a training/serving config. Only used if it looks like one."""
    if not _TRAIN_CFG_HINTS.search(text):
        c.signal("config_ignored", "no training/serving keys", f"{rel}:1")
        return
    if rel.lower().endswith(".json"):
        data = _json(c, rel, text)
        lines = text.splitlines()
        if data is None:
            return
        trees = [(data, lambda p: _locate(lines, p))]
    else:
        docs, locs = yamlish.parse(text)
        trees = [(d, (lambda p, di=di: locs.get((di,) + p, 1))) for di, d in enumerate(docs)]
    low = text.lower()
    if re.search(r"(?m)^\s*(base_model|adapter|sequence_len|micro_batch_size)\s*:", text) and "adapter" in low:
        c.framework("axolotl", f"{rel}:1")
    if "finetuning_type" in low or re.search(r"(?m)^\s*stage\s*:\s*(sft|pt|dpo|rm|ppo|kto)\b", text):
        c.framework("llama-factory", f"{rel}:1")
    if re.search(r"(?mi)^\s*(learning_rate|lr|optimizer|num_epochs|epochs|num_train_epochs|micro_batch_size|"
                 r"per_device_train_batch_size|finetuning_type|gradient_accumulation_steps)\s*[:=\"]", text) or \
            re.search(r'"(learning_rate|num_train_epochs|per_device_train_batch_size)"\s*:', text):
        c.signal("training_config", rel, f"{rel}:1")
    if re.search(r"(?mi)^\s*\"?(max_model_len|max_num_seqs|tensor_parallel_size|gpu_memory_utilization)\"?\s*:",
                 text):
        c.signal("inference_engine", "vllm", f"{rel}:1")
    for tree, loc in trees:
        _apply_tree(c, rel, tree, loc, "high", P_TRAINCFG)


# ------------------------------------------------------------------------------------------------
# Launch commands (Dockerfile, compose, shell)

_LAUNCHERS = {"torchrun", "accelerate", "deepspeed", "vllm", "text-generation-launcher", "python", "python3",
              "llamafactory-cli", "axolotl", "tune", "sglang", "trtllm-serve", "llama-server", "uvicorn", "gunicorn"}


def _scalarize(v: str) -> Any:
    low = v.lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        return v


def command(c: Collector, tokens: list[str], src: str) -> None:
    """Interpret a launch command line (already tokenized). Values are data, never executed."""
    if not tokens:
        return
    toks = [t for t in tokens if t not in ("\\",)]
    joined = " ".join(toks)
    head = [t.rsplit("/", 1)[-1] for t in toks[:4]]
    if "torchrun" in head or "torch.distributed.run" in joined or "torch.distributed.launch" in joined:
        c.signal("distributed_launch", "torchrun", src)
    if "accelerate" in head and "launch" in toks:
        c.framework("accelerate", src)
        c.signal("distributed_launch", "accelerate launch", src)
    if head and head[0] == "deepspeed":
        c.framework("deepspeed", src)
        c.signal("distributed_launch", "deepspeed", src)
    if "vllm" in head and "serve" in toks:
        c.framework("vllm", src)
        c.signal("inference_engine", "vllm", src)
        c.signal("server", "vllm serve", src)
        i = toks.index("serve")
        if i + 1 < len(toks) and not toks[i + 1].startswith("-"):
            apply_key(c, ("model",), toks[i + 1], src, "high", P_CODE)
    if "vllm.entrypoints" in joined:
        c.framework("vllm", src)
        c.signal("inference_engine", "vllm", src)
        c.signal("server", "vllm api_server", src)
    if "text-generation-launcher" in head:
        c.framework("tgi", src)
        c.signal("inference_engine", "tgi", src)
        c.signal("server", "text-generation-launcher", src)
    if "llama-server" in head or "llama_cpp.server" in joined:
        c.framework("llama.cpp", src)
        c.signal("inference_engine", "llama.cpp", src)
        c.signal("server", "llama.cpp server", src)
    if any(h in ("sglang",) for h in head) or "sglang.launch_server" in joined:
        c.framework("sglang", src)
        c.signal("server", "sglang", src)
    if "gunicorn" in head or "uvicorn" in head:
        c.signal("web_server", head[0] if head[0] in ("gunicorn", "uvicorn") else "gunicorn/uvicorn", src)
    nproc = nnodes = None
    i = 0
    while i < len(toks):
        t = toks[i]
        if t.startswith("--") and len(t) > 2:
            key, eq, val = t[2:].partition("=")
            if not eq:
                if i + 1 < len(toks) and not toks[i + 1].startswith("-"):
                    val = toks[i + 1]
                    i += 1
                else:
                    val = "true"
            key_n = key.replace("-", "_").lower()
            value = _scalarize(val)
            if key_n in ("nproc_per_node", "nproc-per-node"):
                nproc = as_int(value)
            elif key_n == "nnodes":
                nnodes = as_int(value) if as_int(value) else None
            elif key_n in ("config_file", "config"):
                c.signal("config_ref", val, src)
            elif key_n == "num_shard":
                n = as_int(value)
                if n:
                    c.fact("hardware_hints.gpu_count", n, src, "high", P_CODE)
            elif key_n == "max_input_length":
                n = as_int(value)
                if n:
                    c.signal("max_input_length", n, src)
            else:
                apply_key(c, (key_n,), value, src, "high", P_CODE)
        i += 1
    if nproc:
        c.fact("hardware_hints.gpu_count", nproc * (nnodes or 1), src, "high", P_CODE)


def _commands_from_lines(lines: list[tuple[int, str]]) -> list[tuple[int, list[str]]]:
    """Join backslash continuations and split on shell separators. Returns (first line, tokens)."""
    out: list[tuple[int, list[str]]] = []
    buf, start = "", 0
    for ln, raw in lines:
        s = raw.rstrip()
        if not buf:
            start = ln
        if s.endswith("\\"):
            buf += s[:-1] + " "
            continue
        buf += s
        for part in re.split(r"\s*(?:&&|\|\||;|\|)\s*", buf):
            part = part.strip()
            if not part or part.startswith("#"):
                continue
            try:
                toks = shlex.split(part, comments=True)
            except ValueError:
                toks = part.split()
            # strip env assignments (FOO=bar cmd ...)
            while toks and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[0]):
                toks = toks[1:]
            if toks:
                out.append((start, toks))
        buf = ""
    return out


def shell(c: Collector, rel: str, text: str) -> None:
    lines = list(enumerate(text.splitlines(), 1))
    for ln, toks in _commands_from_lines(lines):
        if toks[0].rsplit("/", 1)[-1] in _LAUNCHERS or any(t in _LAUNCHERS for t in toks[:3]):
            command(c, toks, f"{rel}:{ln}")
        if toks[0] in ("pip", "pip3", "uv") or toks[:3] == ["python", "-m", "pip"]:
            _pip_install(c, toks, f"{rel}:{ln}")
        if toks[0] == "export" and len(toks) > 1:
            m = re.match(r"^CUDA_VISIBLE_DEVICES=([\d,]+)$", toks[1])
            if m:
                c.fact("hardware_hints.gpu_count", len([x for x in m.group(1).split(",") if x]), f"{rel}:{ln}",
                       "medium", P_DEFAULT)


def _pip_install(c: Collector, toks: list[str], src: str) -> None:
    if "install" not in toks:
        return
    after = toks[toks.index("install") + 1:]
    skip_next = False
    for t in after:
        if skip_next:
            skip_next = False
            m = _CU_PIN.search(t)
            if m:
                c.fact("hardware_hints.cuda_min", _cuda_from_cu(next(g for g in m.groups() if g)), src, "medium",
                       P_CODE)
            continue
        if t in ("-r", "--requirement", "-c", "--index-url", "--extra-index-url", "-i", "-f", "--find-links"):
            skip_next = True
            continue
        if t.startswith("-"):
            m = _CU_PIN.search(t)
            if m:
                c.fact("hardware_hints.cuda_min", _cuda_from_cu(next(g for g in m.groups() if g)), src, "medium",
                       P_CODE)
            continue
        m = _REQ_NAME.match(t.strip("'\""))
        if m:
            dependency(c, m.group(1), m.group(3) or "", src)


def dockerfile(c: Collector, rel: str, text: str) -> None:
    lines = list(enumerate(text.splitlines(), 1))
    # join continuations into instructions
    instrs: list[tuple[int, str]] = []
    buf, start = "", 0
    for ln, raw in lines:
        s = raw.strip()
        if not buf and (not s or s.startswith("#")):
            continue
        if not buf:
            start = ln
        if s.startswith("#"):
            continue
        if s.endswith("\\"):
            buf += s[:-1] + " "
            continue
        buf += s
        instrs.append((start, buf))
        buf = ""
    if buf:
        instrs.append((start, buf))
    for ln, ins in instrs:
        src = f"{rel}:{ln}"
        op, _, rest = ins.partition(" ")
        op = op.upper()
        rest = rest.strip()
        if op == "FROM":
            image = rest.split()[0] if rest else ""
            if image.startswith("--platform"):
                image = rest.split()[1] if len(rest.split()) > 1 else ""
            _image(c, image, src)
        elif op == "RUN":
            for _, toks in _commands_from_lines([(ln, rest)]):
                if toks and (toks[0] in ("pip", "pip3", "uv", "conda", "mamba") or toks[:3] == ["python", "-m", "pip"]):
                    _pip_install(c, toks, src)
        elif op in ("CMD", "ENTRYPOINT"):
            toks = _exec_form(rest)
            if toks:
                if toks[:2] in (["/bin/sh", "-c"], ["sh", "-c"], ["bash", "-c"]) and len(toks) > 2:
                    for _, t2 in _commands_from_lines([(ln, " ".join(toks[2:]))]):
                        command(c, t2, src)
                else:
                    command(c, toks, src)
        elif op == "ENV":
            for m in re.finditer(r"([A-Za-z_][A-Za-z0-9_]*)[= ]([^\s]+)", rest):
                k, v = m.group(1), m.group(2).strip("'\"")
                if k == "CUDA_VISIBLE_DEVICES" and re.fullmatch(r"[\d,]+", v):
                    c.fact("hardware_hints.gpu_count", len([x for x in v.split(",") if x]), src, "medium", P_DEFAULT)
                elif k.upper() in ("MODEL", "MODEL_NAME", "MODEL_ID", "BASE_MODEL", "HF_MODEL", "MODEL_NAME_OR_PATH"):
                    apply_key(c, ("model_name",), v, src, "medium", P_DEFAULT)
                elif k.upper().startswith("CUDA_VERSION"):
                    mv = re.match(r"(\d+)\.(\d+)", v)
                    if mv:
                        c.fact("hardware_hints.cuda_min", f"{mv.group(1)}.{mv.group(2)}", src, "high", P_CODE)


def _exec_form(rest: str) -> list[str]:
    if rest.startswith("["):
        try:
            v = json.loads(rest)
            return [str(x) for x in v] if isinstance(v, list) else []
        except ValueError:
            return []
    try:
        return shlex.split(rest)
    except ValueError:
        return rest.split()


def _image(c: Collector, image: str, src: str) -> None:
    low = image.lower()
    m = re.search(r"nvidia/cuda:(\d+)\.(\d+)", low) or re.search(r"cuda[-_]?(\d+)\.(\d+)", low)
    if m:
        c.fact("hardware_hints.cuda_min", f"{m.group(1)}.{m.group(2)}", src, "high", P_CODE)
        c.signal("gpu_base_image", image, src)
    m2 = re.search(r"cu(\d{3})", low)
    if not m and m2:
        c.fact("hardware_hints.cuda_min", _cuda_from_cu(m2.group(1)), src, "medium", P_CODE)
    if low.startswith(("pytorch/pytorch", "nvcr.io/nvidia/pytorch")):
        c.framework("pytorch", src)
    if "vllm/vllm-openai" in low or "vllm" in low.split(":")[0]:
        c.framework("vllm", src)
        c.signal("inference_engine", "vllm", src)
        c.signal("server", "vllm image", src)
    if "text-generation-inference" in low:
        c.framework("tgi", src)
        c.signal("inference_engine", "tgi", src)
        c.signal("server", "tgi image", src)
    if "tensorflow/tensorflow" in low:
        c.framework("tensorflow", src)
    if "huggingface/transformers" in low:
        c.framework("transformers", src)
    if "axolotl" in low:
        c.framework("axolotl", src)


def compose(c: Collector, rel: str, text: str) -> None:
    docs, locs = yamlish.parse(text)
    doc = docs[0] if docs else None
    if not isinstance(doc, dict):
        return
    services = doc.get("services") if isinstance(doc.get("services"), dict) else {}
    for sname, svc in services.items():
        if not isinstance(svc, dict):
            continue
        base = (0, "services", sname)
        img = svc.get("image")
        if isinstance(img, str):
            _image(c, img, f"{rel}:{locs.get(base + ('image',), 1)}")
        for key in ("command", "entrypoint"):
            cmd = svc.get(key)
            src = f"{rel}:{locs.get(base + (key,), 1)}"
            toks: list[str] = []
            if isinstance(cmd, list):
                toks = [str(x) for x in cmd]
            elif isinstance(cmd, str):
                try:
                    toks = shlex.split(cmd)
                except ValueError:
                    toks = cmd.split()
            if toks:
                is_vllm = isinstance(img, str) and "vllm" in img.lower()
                if is_vllm and toks and not toks[0].startswith("-") and toks[0] not in _LAUNCHERS:
                    apply_key(c, ("model",), toks[0], src, "high", P_CODE)
                command(c, toks, src)
        env = svc.get("environment")
        items = env.items() if isinstance(env, dict) else (
            (str(e).split("=", 1) + [""])[:2] for e in env) if isinstance(env, list) else []
        for k, v in items:
            if str(k).upper() in ("MODEL", "MODEL_NAME", "MODEL_ID", "BASE_MODEL") and isinstance(v, str):
                apply_key(c, ("model_name",), v, f"{rel}:{locs.get(base + ('environment',), 1)}", "medium", P_DEFAULT)
        if svc.get("runtime") == "nvidia":
            c.signal("gpu_runtime", "nvidia", f"{rel}:{locs.get(base + ('runtime',), 1)}")
        devices = (((svc.get("deploy") or {}).get("resources") or {}).get("reservations") or {}).get("devices") \
            if isinstance(svc.get("deploy"), dict) else None
        if isinstance(devices, list):
            for di, dev in enumerate(devices):
                if not isinstance(dev, dict):
                    continue
                caps = dev.get("capabilities") or []
                if "gpu" in [str(x) for x in (caps if isinstance(caps, list) else [caps])] or dev.get("driver") == "nvidia":
                    src = f"{rel}:{locs.get(base + ('deploy', 'resources', 'reservations', 'devices', di), 1)}"
                    c.signal("gpu_runtime", "compose gpu reservation", src)
                    n = as_int(dev.get("count"))
                    if n:
                        c.fact("hardware_hints.gpu_count", n, src, "high", P_CODE)
                    ids = dev.get("device_ids")
                    if isinstance(ids, list) and ids:
                        c.fact("hardware_hints.gpu_count", len(ids), src, "high", P_CODE)


# ------------------------------------------------------------------------------------------------
# Notebooks and README

def notebook_cells(text: str) -> list[tuple[int, str]] | None:
    """Return [(cell_number, code)] for code cells only. Outputs and markdown are discarded."""
    try:
        nb = json.loads(text)
    except ValueError:
        return None
    if not isinstance(nb, dict) or not isinstance(nb.get("cells"), list):
        return None
    out = []
    for i, cell in enumerate(nb["cells"], 1):
        if not isinstance(cell, dict) or cell.get("cell_type") != "code":
            continue
        src = cell.get("source", "")
        code = "".join(src) if isinstance(src, list) else str(src)
        out.append((i, code))
    return out


def readme(c: Collector, rel: str, text: str) -> None:
    """README text is untrusted: only model-name candidates and GPU mentions, at low confidence."""
    seen = set()
    for ln, line in enumerate(text.splitlines(), 1):
        for cand in readme_model_candidates(line):
            if cand.lower() in seen:
                continue
            seen.add(cand.lower())
            if len(seen) > 3:
                return
            c.fact("model.name", cand, f"{rel}:{ln}", "low", P_README)


def gpu_mentions(c: Collector, rel: str, text: str, skip_lines: set[int]) -> None:
    for ln, line in enumerate(text.splitlines(), 1):
        if ln in skip_lines:
            continue
        for m in GPU_NAMES.finditer(line[:2000]):
            c.signal("gpu_mentioned", m.group(1).replace(" ", ""), f"{rel}:{ln}")
