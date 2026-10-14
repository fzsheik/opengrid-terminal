"""Orchestration: walk (policy) -> read (policy.safe_read) -> extract -> merge -> questions -> redact."""

from __future__ import annotations

import ast

import os
import re
from typing import Any

from . import extract as ex
from . import policy
from . import modelsize
from .facts import CONF_RANK, P_CODE, P_DERIVED, PACKAGES, Collector, Fact, params_from_name
from .pyextract import PyAnalyzer
from .redact import find_injections, find_secrets, redact_obj

ANALYZER_VERSION = "1"
MAX_SKIPPED_LISTED = 300
MAX_SIGNALS_LISTED = 300

GPU_TEXT_KINDS = {"dockerfile", "compose", "shell", "config_file", "opengrid_manifest", "readme", "python",
                  "python_import", "notebook", "accelerate"}
QUANTIZED = {"int4", "nf4", "int8", "gptq", "awq", "fp8"}
ML_CATEGORIES = {name for (cat, name) in PACKAGES.values() if cat == "ml"} | {"tgi", "llama.cpp", "sglang", "cupy"}


def _empty_requirements() -> dict[str, Any]:
    return {
        "workload_type": None,
        "frameworks": [],
        "model": {"name": None, "architecture": None, "parameters_b": None, "active_parameters_b": None,
                  "num_layers": None, "hidden_size": None, "num_attention_heads": None, "num_kv_heads": None,
                  "head_dim": None, "vocab_size": None, "max_context": None},
        "precision": None,
        "quantization": None,
        "training": {"method": None, "optimizer": None, "micro_batch_size": None, "global_batch_size": None,
                     "seq_len": None, "gradient_checkpointing": None, "lora_rank": None, "zero_stage": None,
                     "cpu_offload": None, "epochs": None, "dataset_tokens": None, "dataset_samples": None},
        "inference": {"concurrency": None, "context_len": None, "output_tokens": None, "target_latency_ms": None,
                      "target_tokens_per_s": None, "engine": None},
        "constraints": {"budget_usd": None, "max_price_per_gpu_hour": None, "runtime_hours": None, "region": None,
                        "max_gpus": None, "allow_spot": None, "vendor": None, "include_providers": [],
                        "exclude_providers": []},
        "hardware_hints": {"gpus_mentioned": [], "gpu_count": None, "cuda_min": None},
        "evidence": [],
        "source": "analyzer",
    }


def _set(req: dict, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    cur = req
    for p in parts[:-1]:
        cur = cur[p]
    cur[parts[-1]] = value


def _get(req: dict, dotted: str) -> Any:
    cur: Any = req
    for p in dotted.split("."):
        cur = cur[p]
    return cur


# ------------------------------------------------------------------------------------------------
# Reading


class _Run:
    def __init__(self, root_real: str, max_file_bytes: int, max_files: int, max_total_bytes: int) -> None:
        self.root = root_real
        self.max_file_bytes = max_file_bytes
        self.max_files = max_files
        self.max_total = max_total_bytes
        self.c = Collector()
        self.inspected: list[dict] = []
        self.skipped: list[policy.Skipped] = []
        self.total = 0
        self.count = 0
        self.local_imports: list[tuple[str, int, str]] = []  # (importing rel, level, module)
        self.budget_warned = False

    def read(self, cand: policy.Candidate) -> str | None:
        if self.count >= self.max_files:
            self.skipped.append(policy.Skipped(cand.rel, f"file limit reached ({self.max_files} files)", "budget"))
            if not self.budget_warned:
                self.c.warn(f"stopped reading after {self.max_files} files (max_files); analysis may be partial")
                self.budget_warned = True
            return None
        if self.total + cand.size > self.max_total:
            self.skipped.append(policy.Skipped(cand.rel, f"total read budget reached ({self.max_total} bytes)",
                                               "budget"))
            if not self.budget_warned:
                self.c.warn(f"total read budget of {self.max_total} bytes reached; analysis may be partial")
                self.budget_warned = True
            return None
        try:
            data = policy.safe_read(self.root, cand.abs, min(self.max_file_bytes, self.max_total - self.total),
                                    cand.ino)
        except policy.AccessError as e:
            self.skipped.append(policy.Skipped(cand.rel, e.reason, e.category))
            return None
        if policy.looks_binary(data):
            self.skipped.append(policy.Skipped(cand.rel, "binary content (not parsed)", "binary_content"))
            return None
        self.total += len(data)
        self.count += 1
        reason = cand.reason if cand.imported_by is None else f"{cand.reason} ({cand.imported_by})"
        self.inspected.append({"path": cand.rel, "bytes": len(data), "reason": reason})
        return policy.decode(data)

    # --------------------------------------------------------------------------------------------
    def process(self, cand: policy.Candidate) -> None:
        text = self.read(cand)
        if text is None:
            return
        rel, kind, c = cand.rel, cand.kind, self.c
        if kind == "notebook":
            cells = ex.notebook_cells(text)
            if cells is None:
                c.warn(f"{rel}: not a valid notebook")
                return
            scan_text = "\n".join(code for _, code in cells)
        else:
            cells = None
            scan_text = text
        injections = find_injections(scan_text, is_readme=kind == "readme")
        for ln, label in injections:
            where = f"{rel}:{ln}" if cells is None else rel
            c.warn(f"{where}: text that looks like instructions to an AI agent ({label}); it is treated as data "
                   f"and has no effect on the analysis")
        by_line: dict[int, list[str]] = {}
        for ln, label in find_secrets(scan_text):
            by_line.setdefault(ln, []).append(label)
        for ln, labels in by_line.items():
            where = f"{rel}:{ln}" if cells is None else rel
            c.warn(f"{where}: secret-looking value ({', '.join(labels)}); it is never included in the output. "
                   f"Consider removing it from the project and rotating it")
        skip_lines = {ln for ln, _ in injections}
        if kind == "readme" and injections:
            c.warn(f"{rel}: README ignored for extraction because it contains instruction-like text")
            return

        if kind == "requirements":
            ex.requirements(c, rel, text)
        elif kind == "pyproject":
            ex.pyproject(c, rel, text)
        elif kind == "setup_cfg":
            ex.setup_cfg(c, rel, text)
        elif kind == "setup_py":
            ex.setup_py(c, rel, text)
        elif kind == "pipfile":
            ex.pipfile(c, rel, text)
        elif kind == "conda":
            ex.conda(c, rel, text)
        elif kind == "package_json":
            ex.package_json(c, rel, text)
        elif kind == "hf_config":
            ex.hf_config(c, rel, text)
        elif kind == "generation_config":
            ex.generation_config(c, rel, text)
        elif kind == "adapter_config":
            ex.adapter_config(c, rel, text)
        elif kind == "deepspeed":
            ex.deepspeed(c, rel, text)
        elif kind == "accelerate":
            ex.accelerate(c, rel, text)
        elif kind == "opengrid_manifest":
            ex.opengrid_manifest(c, rel, text)
        elif kind == "config_file":
            ex.config_file(c, rel, text)
        elif kind == "dockerfile":
            ex.dockerfile(c, rel, text)
        elif kind == "compose":
            ex.compose(c, rel, text)
        elif kind == "shell":
            ex.shell(c, rel, text)
        elif kind in ("python", "python_import"):
            pa = PyAnalyzer(c, lambda ln, rel=rel: f"{rel}:{ln}")
            for level, mod in pa.run(text):
                self.local_imports.append((rel, level, mod))
        elif kind == "notebook":
            for n, code in cells or []:
                lines = []
                for line in code.splitlines():
                    s = line.lstrip()
                    if s.startswith(("!", "%")):
                        if re.match(r"^[!%]\s*(pip|uv pip|pip3)\s+install\b", s):
                            toks = s.lstrip("!%").split()
                            ex._pip_install(c, toks if toks[0] != "uv" else toks[1:], f"{rel}#cell{n}:1")
                        lines.append("")
                    else:
                        lines.append(line)
                pa = PyAnalyzer(c, lambda ln, rel=rel, n=n: f"{rel}#cell{n}:{ln}")
                pa.run("\n".join(lines))
        elif kind == "readme":
            ex.readme(c, rel, text)
        if kind in GPU_TEXT_KINDS:
            ex.gpu_mentions(c, rel, scan_text, skip_lines)

    def resolve_imports(self, pool: dict[str, policy.Candidate]) -> list[policy.Candidate]:
        out: list[policy.Candidate] = []
        seen: set[str] = set()
        for importer, level, mod in self.local_imports:
            base_dir = importer.rsplit("/", 1)[0] if "/" in importer else ""
            if level:
                parts = base_dir.split("/") if base_dir else []
                up = level - 1
                if up > len(parts):
                    continue
                parts = parts[:len(parts) - up] if up else parts
                bases = ["/".join(parts)]
            else:
                bases = [base_dir, "", "src"]
            mod_path = mod.replace(".", "/") if mod else ""
            for b in dict.fromkeys(bases):
                found = None
                segs = mod_path.split("/") if mod_path else []
                # "from pkg.mod import name" may refer to pkg/mod.py or pkg/mod/name.py: try longest first
                for k in range(len(segs), 0, -1):
                    stem = "/".join(([b] if b else []) + segs[:k])
                    for candidate in (f"{stem}.py", f"{stem}/__init__.py"):
                        if candidate in pool:
                            found = candidate
                            break
                    if found:
                        break
                if found and found not in seen:
                    seen.add(found)
                    cand = pool[found]
                    cand.imported_by = f"imported by {importer}"
                    out.append(cand)
                    break
            if len(out) >= policy.MAX_IMPORTED_MODULES:
                self.c.warn(f"only the first {policy.MAX_IMPORTED_MODULES} imported local modules were inspected")
                break
        return out


# ------------------------------------------------------------------------------------------------
# Merge


def _best(facts: list[Fact]) -> tuple[Fact, list[Fact]]:
    ranked = sorted(enumerate(facts), key=lambda t: (-CONF_RANK[t[1].confidence], -t[1].prio, t[0]))
    top = ranked[0][1]
    rivals = [f for _, f in ranked[1:] if f.confidence == top.confidence and f.prio == top.prio
              and f.value != top.value]
    return top, rivals


def _version_key(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)) or (0,)


def _count_local_model(c: Collector, chosen: dict[str, Fact]) -> None:
    """Pick the model class a script builds and count its parameters exactly (modelsize); never guess."""
    mods = {n: v for n, v in c.module_classes.items() if v[0].is_module}
    if not mods:
        return
    classes = {n: v[0] for n, v in c.module_classes.items()}
    used_inside = {sub for ci, _ in mods.values() for lay in ci.layers for node in ast.walk(lay)
                   if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                   for sub in [node.func.id] if sub in mods}
    top = [n for n in mods if n not in used_inside]
    built = [n for n in top if n in c.called]
    pick, conf = (built, "high") if built else (top, "medium")
    if len(pick) != 1:
        if len(pick) > 1:
            c.warn("several model classes are built (" + ", ".join(sorted(pick)[:5]) + "): parameter count not "
                   "inferred; state model.parameters_b")
        return
    name = pick[0]
    n, why = modelsize.count(classes, name)
    src = mods[name][1]
    if n is None:
        c.warn(f"{src}: parameters of {name} not counted ({why})")
        return
    chosen["model.parameters_b"] = Fact("model.parameters_b", round(n / 1e9, 9), src, conf, P_CODE)
    c.signal("model_parameters", f"{name}: {n:,} parameters (exact, from its layer definitions)", src)
    if "model.architecture" not in chosen and mods[name][0].has_conv:
        chosen["model.architecture"] = Fact("model.architecture", "cnn", src, "medium", P_DERIVED)


def merge(c: Collector, inspected: list[dict]) -> dict[str, Any]:
    req = _empty_requirements()
    evidence: list[dict] = []
    by_field: dict[str, list[Fact]] = {}
    for f in c.facts:
        by_field.setdefault(f.field, []).append(f)

    def ev(field: str, value: Any, source: str, confidence: str) -> None:
        evidence.append({"field": field, "value": value, "source": source, "confidence": confidence})

    def choose(field: str) -> Fact | None:
        facts = by_field.get(field)
        if not facts:
            return None
        if field == "hardware_hints.cuda_min":
            top_conf = max(CONF_RANK[f.confidence] for f in facts)
            pool = [f for f in facts if CONF_RANK[f.confidence] == top_conf]
            best = max(pool, key=lambda f: _version_key(str(f.value)))
            return best
        best, rivals = _best(facts)
        if rivals:
            alts = ", ".join(f"{r.value!r} ({r.source})" for r in rivals[:3])
            c.warn(f"conflicting values for {field}: using {best.value!r} ({best.source}); also found {alts}")
        return best

    simple_fields = ["model.name", "model.architecture", "model.parameters_b", "model.num_layers",
                     "model.hidden_size", "model.num_attention_heads", "model.num_kv_heads", "model.head_dim",
                     "model.vocab_size", "model.max_context", "precision", "quantization", "training.optimizer",
                     "training.micro_batch_size", "training.global_batch_size", "training.seq_len",
                     "training.gradient_checkpointing", "training.lora_rank", "training.zero_stage",
                     "training.cpu_offload", "training.epochs", "training.dataset_samples", "inference.concurrency",
                     "inference.context_len", "hardware_hints.gpu_count", "hardware_hints.cuda_min"]
    chosen: dict[str, Fact] = {}
    for fld in simple_fields:
        f = choose(fld)
        if f is not None:
            chosen[fld] = f

    # exact parameter count of a locally defined nn.Module (no model name, no stated size)
    if "model.parameters_b" not in chosen and "model.name" not in chosen:
        _count_local_model(c, chosen)

    # parameter count from the model name ("Llama-2-7b-hf" -> 7)
    if "model.parameters_b" not in chosen and "model.name" in chosen:
        p, moe = params_from_name(str(chosen["model.name"].value))
        if moe:
            c.signal("moe", "mixture-of-experts name pattern", chosen["model.name"].source)
        elif p is not None:
            chosen["model.parameters_b"] = Fact("model.parameters_b", p, chosen["model.name"].source, "medium",
                                                P_DERIVED)

    if "model.name" not in chosen:
        notes = {}
        for s in c.signals:
            if s["kind"] == "model_reference_unresolved":
                notes.setdefault(s["value"], s["source"])
        for note, src in notes.items():
            c.warn(f"{src}: model is loaded from {note}; its value is not available to a static analysis "
                   f"(environment and runtime values are never read)")

    # frameworks
    frameworks = list(c.frameworks.keys())
    req["frameworks"] = frameworks
    for fw in frameworks:
        ev("frameworks", fw, c.frameworks[fw], "high")

    ml = [f for f in frameworks if f in ML_CATEGORIES]
    web = [s for s in c.signals if s["kind"] in ("web_framework", "web_server", "server")
           and s["value"] not in ("vllm serve", "vllm api_server", "text-generation-launcher", "vllm image",
                                  "tgi image", "llama.cpp server", "sglang")]
    cpu_ml = [s for s in c.signals if s["kind"] == "cpu_ml"]

    lora = c.first("peft_lora")
    lora_cond = c.first("peft_lora_conditional") if lora is None else None
    training_sig = c.first("hf_trainer", "lightning_trainer", "training_loop", "training_config", "deepspeed_config")
    weak_training = c.first("distributed_launch")
    inference_engine = c.first("inference_engine")
    serving = [s for s in c.signals if s["kind"] == "server"]
    inference_sig = inference_engine or c.first("inference_call", "offline_inference")
    method_fact = choose("training.method")
    gpu_evidence = (chosen.get("hardware_hints.gpu_count") or chosen.get("hardware_hints.cuda_min")
                    or c.first("gpu_base_image", "gpu_runtime"))

    workload = None
    wl_src, wl_conf, wl_val = None, "medium", None
    if training_sig or lora or lora_cond or method_fact or (weak_training and not inference_sig):
        sig = training_sig or lora or lora_cond or weak_training
        pretrained = bool(c.first("pretrained_load") or "model.name" in chosen or lora or lora_cond
                          or (method_fact and method_fact.value in ("lora", "qlora")))
        workload = "fine_tuning" if pretrained else "training"
        wl_src = (method_fact.source if (method_fact and not sig) else sig["source"])
        wl_conf = "high" if (training_sig and (training_sig["kind"] in ("hf_trainer", "training_config",
                                                                          "training_loop", "lightning_trainer"))) \
            else "medium"
        wl_val = workload
    elif inference_sig:
        offline_only = not serving and bool(c.first("offline_inference", "inference_call"))
        batchy = any(re.search(r"(?i)batch|offline|predict|bulk", i["path"]) for i in inspected)
        workload = "batch" if offline_only and batchy else "inference"
        wl_src = (serving[0] if serving else inference_sig)["source"]
        wl_conf = "high" if (serving and inference_engine) else "medium"
        wl_val = workload
    elif not ml and inspected:
        has_manifest_or_code = any(i["reason"].startswith(("dependency manifest", "Python", "node manifest",
                                                           "container", "compose"))
                                   for i in inspected)
        if (web or cpu_ml or has_manifest_or_code) and "model.name" not in chosen and not gpu_evidence:
            workload = "no_gpu"
            if web:
                wl_src = web[0]["source"]
                wl_val = f"no_gpu (no ML/GPU frameworks found; {web[0]['value']} web app)"
            elif cpu_ml:
                wl_src = cpu_ml[0]["source"]
                wl_val = f"no_gpu (only CPU ML libraries such as {cpu_ml[0]['value']}; no GPU frameworks)"
            else:
                wl_src = inspected[0]["path"]
                wl_val = "no_gpu (no ML/GPU frameworks in dependencies or code)"
                wl_conf = "low"
    req["workload_type"] = workload
    if workload:
        ev("workload_type", wl_val if wl_val else workload, wl_src or "", wl_conf)

    # training method
    method = None
    if workload in ("training", "fine_tuning"):
        quant = chosen.get("quantization")
        quantized = (quant is not None and quant.value in QUANTIZED) or c.has("kbit_training")
        if method_fact:
            method = method_fact.value
            if method == "lora" and quantized:
                method = "qlora"
            ev("training.method", method, method_fact.source, method_fact.confidence)
        elif lora:
            method = "qlora" if quantized else "lora"
            ev("training.method", method, lora["source"], "high")
        elif lora_cond:
            method = None
            c.warn(f"{lora_cond['source']}: LoRA/PEFT is applied only under a condition (e.g. a --use_lora flag); "
                   f"training method left unknown")
        else:
            method = "full"
            src = (training_sig or weak_training or {"source": ""})["source"]
            ev("training.method", "full (training without PEFT/LoRA adapters)", src, "medium")
        req["training"]["method"] = method

    for fld, f in chosen.items():
        _set(req, fld, f.value)
        ev(fld, f.value, f.source, f.confidence)

    # quantized base + LoRA but quantization only implied
    if method == "qlora" and req["quantization"] is None and c.has("kbit_training"):
        s = c.first("kbit_training")
        req["quantization"] = "int4"
        ev("quantization", "int4 (prepare_model_for_kbit_training)", s["source"], "low")

    # inference engine
    if workload in ("inference", "batch"):
        eng = None
        for s in c.signals:
            if s["kind"] == "inference_engine":
                eng = s
                break
        if eng:
            req["inference"]["engine"] = eng["value"]
            ev("inference.engine", eng["value"], eng["source"], "high")
        elif "transformers" in frameworks and c.first("inference_call"):
            s = c.first("inference_call")
            req["inference"]["engine"] = "transformers"
            ev("inference.engine", "transformers", s["source"], "medium")
        # training-only fields do not apply to inference
        seq = req["training"]["seq_len"]
        if seq and not req["inference"]["context_len"]:
            req["inference"]["context_len"] = seq
            ev("inference.context_len", seq, chosen["training.seq_len"].source, "medium")
        for k in req["training"]:
            req["training"][k] = None
        evidence = [e for e in evidence if not e["field"].startswith("training.")]
    elif workload == "no_gpu":
        keep = {"workload_type", "frameworks"}
        evidence = [e for e in evidence if e["field"] in keep]
        base = _empty_requirements()
        base["workload_type"] = "no_gpu"
        base["frameworks"] = req["frameworks"]
        req = base

    # GPU names mentioned anywhere: hardware hints only
    gpus = []
    for s in c.signals:
        if s["kind"] == "gpu_mentioned" and s["value"] not in gpus:
            gpus.append(s["value"])
            if workload != "no_gpu":
                ev("hardware_hints.gpus_mentioned", s["value"], s["source"], "low")
    if workload != "no_gpu":
        req["hardware_hints"]["gpus_mentioned"] = gpus

    req["evidence"] = evidence
    return req


# ------------------------------------------------------------------------------------------------
# Questions


def questions(req: dict[str, Any], c: Collector) -> list[dict[str, str]]:
    q: list[dict[str, str]] = []
    wl = req["workload_type"]
    m, t, inf = req["model"], req["training"], req["inference"]
    if wl == "no_gpu":
        return q
    if wl is None:
        q.append({"field": "workload_type",
                  "question": "What will this project run on a GPU: training from scratch, fine-tuning, or "
                              "inference/serving?",
                  "why": "the memory estimate is computed differently for each"})
    if m["parameters_b"] is None and m["num_layers"] is None:
        if m["name"] is None:
            q.append({"field": "model.name",
                      "question": "Which model (Hugging Face id) or how many parameters does it have?",
                      "why": "weights, gradients and optimizer state all scale with the parameter count; no model "
                             "name or size was found in the project"})
        else:
            q.append({"field": "model.parameters_b",
                      "question": f"How many parameters does {m['name']} have (in billions)?",
                      "why": "the size could not be read from the name or a config.json"})
    if wl in ("training", "fine_tuning"):
        if t["method"] is None:
            q.append({"field": "training.method", "question": "Full fine-tuning, LoRA or QLoRA?",
                      "why": "changes optimizer and gradient memory by ~10x"})
        llm = bool({"transformers", "trl", "peft", "unsloth", "axolotl", "torchtune", "llama-factory"}
                   & set(req["frameworks"]))
        if llm and t["seq_len"] is None:
            q.append({"field": "training.seq_len", "question": "What sequence length (tokens per sample) will you "
                                                               "train with?",
                      "why": "activation memory grows with sequence length"})
        if t["micro_batch_size"] is None:
            q.append({"field": "training.micro_batch_size", "question": "What per-GPU batch size will you use?",
                      "why": "activation memory grows with batch size"})
        if t["dataset_samples"] is None and t["dataset_tokens"] is None:
            q.append({"field": "training.dataset_tokens",
                      "question": "How large is the training dataset (samples or tokens), and how many epochs?",
                      "why": "datasets are never read by the analyzer; size drives runtime and cost"})
    if wl in ("inference", "batch"):
        if inf["concurrency"] is None:
            q.append({"field": "inference.concurrency", "question": "How many concurrent requests or sequences?",
                      "why": "KV-cache memory scales with concurrency"})
        if inf["context_len"] is None:
            q.append({"field": "inference.context_len", "question": "What maximum context length (prompt + output)?",
                      "why": "KV-cache memory scales with context length"})
    q.append({"field": "constraints.budget_usd", "question": "What is your budget (total USD or max $/GPU-hour)?",
              "why": "used to rank options and size runtime scenarios"})
    return q


# ------------------------------------------------------------------------------------------------
# Public API


def _skipped_out(items: list[policy.Skipped], c: Collector | None) -> list[dict]:
    ordered = policy.sort_skipped(items)
    out = [{"path": s.path, "reason": s.reason} for s in ordered[:MAX_SKIPPED_LISTED]]
    if len(ordered) > MAX_SKIPPED_LISTED and c is not None:
        c.warn(f"{len(ordered) - MAX_SKIPPED_LISTED} more skipped files are not listed")
    return out


def analyze(root: str | os.PathLike, *, exclude: list[str] | tuple[str, ...] = (), max_file_bytes: int = 256_000,
            max_files: int = 400, dry_run: bool = False,
            max_total_bytes: int = policy.DEFAULT_MAX_TOTAL_BYTES) -> dict[str, Any]:
    """Analyze a local project directory. Reads only allowlisted files; never executes or uploads anything.

    Returns the contract §4 shape. With ``dry_run=True`` returns ``plan(...)`` instead (nothing is read).
    """
    if dry_run:
        return plan(root, exclude=exclude, max_file_bytes=max_file_bytes, max_files=max_files,
                    max_total_bytes=max_total_bytes)
    root_real = policy.resolve_root(root)
    w = policy.walk(root_real, exclude=list(exclude), max_file_bytes=max_file_bytes, read_gitignore=True)
    run = _Run(root_real, max_file_bytes, max_files, max_total_bytes)
    for wmsg in w.warnings:
        run.c.warn(wmsg)
    for g in w.gitignores:
        run.inspected.append({"path": g.rel, "bytes": g.size, "reason": g.reason})
        run.total += g.size
    skipped = list(w.skipped)
    for cand in w.candidates:
        run.process(cand)
    imported = run.resolve_imports({k: v for k, v in w.py_pool.items()})
    if imported:
        imported_rels = {c.rel for c in imported}
        skipped = [s for s in skipped if s.path not in imported_rels]
        for cand in imported:
            run.process(cand)
    skipped += run.skipped

    req = merge(run.c, run.inspected)
    qs = questions(req, run.c)
    out = {
        "root": root_real,
        "requirements": req,
        "files_inspected": run.inspected,
        "files_skipped": _skipped_out(skipped, run.c),
        "signals": run.c.signals[:MAX_SIGNALS_LISTED],
        "questions": qs,
        "warnings": run.c.warnings,
        "analyzer_version": ANALYZER_VERSION,
    }
    return redact_obj(out)


def plan(root: str | os.PathLike, *, exclude: list[str] | tuple[str, ...] = (), max_file_bytes: int = 256_000,
         max_files: int = 400, max_total_bytes: int = policy.DEFAULT_MAX_TOTAL_BYTES,
         dry_run: bool = True) -> dict[str, Any]:
    """Dry run: which files WOULD be read and why. Uses directory listings and stat only; opens no file."""
    root_real = policy.resolve_root(root)
    w = policy.walk(root_real, exclude=list(exclude), max_file_bytes=max_file_bytes, read_gitignore=False)
    planned: list[dict] = [{"path": g.rel, "bytes": g.size, "reason": g.reason} for g in w.gitignores]
    skipped = list(w.skipped)
    total = 0
    count = 0
    for cand in w.candidates:
        if count >= max_files:
            skipped.append(policy.Skipped(cand.rel, f"file limit reached ({max_files} files)", "budget"))
            continue
        if total + cand.size > max_total_bytes:
            skipped.append(policy.Skipped(cand.rel, f"total read budget reached ({max_total_bytes} bytes)", "budget"))
            continue
        total += cand.size
        count += 1
        planned.append({"path": cand.rel, "bytes": cand.size, "reason": cand.reason})
    notes = [
        "dry run: nothing was opened or read; only directory listings and file metadata were used",
        ".gitignore rules are not applied in a dry run (applying them requires reading .gitignore); files they "
        "ignore will additionally be skipped by a real run",
        f"local Python modules imported by the listed scripts may also be read (one hop, at most "
        f"{policy.MAX_IMPORTED_MODULES})",
        "secrets, keys, credentials, model weights, datasets, media and archives are never read",
    ]
    out = {
        "root": root_real,
        "dry_run": True,
        "files_planned": planned,
        "files_skipped": _skipped_out(skipped, None),
        "notes": notes,
        "analyzer_version": ANALYZER_VERSION,
    }
    return redact_obj(out)
