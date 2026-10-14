"""Static extraction from Python source with ``ast`` (parse only: nothing is compiled, imported or run)."""

from __future__ import annotations

import ast
import re
from typing import Any, Callable

from .facts import (P_CODE, P_DEFAULT, PACKAGES, Collector, apply_key, as_int, looks_like_model_id,
                    map_dtype, map_optimizer)
from . import modelsize

TRAINING_ARG_CLASSES = {"TrainingArguments", "Seq2SeqTrainingArguments", "SFTConfig", "DPOConfig", "ORPOConfig",
                        "KTOConfig", "GRPOConfig", "RewardConfig", "PPOConfig", "CPOConfig", "UnslothTrainingArguments"}
OPTIMIZERS = {"Adam": "adamw", "AdamW": "adamw", "SGD": "sgd", "Adafactor": "adafactor", "AdamW8bit": "adamw_8bit",
              "Adam8bit": "adamw_8bit", "PagedAdamW8bit": "adamw_8bit", "PagedAdamW": "adamw",
              "PagedAdamW32bit": "adamw", "FusedAdam": "adamw", "DeepSpeedCPUAdam": "adamw", "RMSprop": None,
              "Lion": None}
SERVER_CLASSES = {"Flask", "FastAPI", "Starlette", "Sanic", "Quart", "Litestar"}
MODEL_LOAD_KW = ("pretrained_model_name_or_path", "model_name", "model_id", "model_name_or_path", "model")
_EVAL_NAME = re.compile(r"(?i)test|val|eval|valid")

ResolveResult = tuple[Any, str, int, str]  # value, confidence, prio, note


def _dotted(node: ast.AST) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    elif isinstance(node, ast.Call):
        parts.append(_dotted(node.func) + "()")
    else:
        parts.append("?")
    return ".".join(reversed(parts))


def _is_main_guard(test: ast.AST) -> bool:
    return (isinstance(test, ast.Compare) and isinstance(test.left, ast.Name) and test.left.id == "__name__")


class PyAnalyzer:
    def __init__(self, c: Collector, loc: Callable[[int], str]) -> None:
        self.c = c
        self.loc = loc
        self.assigns: dict[str, list[ast.AST]] = {}
        self.defaults: dict[str, tuple[Any, int, str]] = {}   # dest -> (value, lineno, note)
        self.parents: dict[ast.AST, ast.AST] = {}
        self.imports: set[str] = set()
        self.local_imports: list[tuple[int, str]] = []

    # ---------------------------------------------------------------------------------------------
    def run(self, text: str) -> list[tuple[int, str]]:
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError, RecursionError):
            self.c.warn(f"{self.loc(1).rsplit(':', 1)[0]}: could not parse as Python; only imports were scanned")
            for i, line in enumerate(text.splitlines(), 1):
                m = re.match(r"^\s*(?:from|import)\s+([A-Za-z_][\w.]*)", line)
                if m:
                    self._import(m.group(1), i)
            return self.local_imports
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                self.parents[child] = node
        self._collect_imports(tree)
        self._model_classes(tree)
        self._collect_assigns(tree)
        self._collect_defaults(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                try:
                    self._call(node)
                except RecursionError:
                    pass
            elif isinstance(node, ast.ClassDef):
                for b in node.bases:
                    d = _dotted(b)
                    if d.endswith(("nn.Module", "LightningModule", "keras.Model", "tf.keras.Model")) or d == "Module":
                        self.c.signal("custom_model", node.name, self.loc(node.lineno))
            elif isinstance(node, ast.Assign):
                self._env_assign(node)
            elif isinstance(node, (ast.List, ast.Tuple)) and 2 <= len(node.elts) <= 200:
                self._command_list(node)
        return self.local_imports

    def _model_classes(self, tree: ast.AST) -> None:
        """nn.Module classes (for an exact parameter count in engine.merge) and torchvision training datasets."""
        try:
            res = modelsize.scan(tree)
        except RecursionError:
            return
        for name, ci in res["classes"].items():
            self.c.module_classes.setdefault(name, (ci, self.loc(ci.lineno)))
        self.c.called |= set(list(res["called"])[:2000])
        for lineno, ds, samples in res["datasets"]:
            self.c.fact("training.dataset_samples", samples, self.loc(lineno), "medium", P_DEFAULT)
            self.c.signal("dataset", f"torchvision {ds} (training split: {samples:,} samples)", self.loc(lineno))

    def _command_list(self, node: ast.List | ast.Tuple) -> None:
        """A literal argv list such as subprocess.run(["torchrun", "--nproc_per_node", "8", ...])."""
        from .extract import command  # local import: extract never imports this module
        toks: list[str] = []
        for e in node.elts:
            v = self.resolve(e)[0]
            if v is None and _dotted(e) == "sys.executable":
                v = "python"
            if not isinstance(v, (str, int, float)) or isinstance(v, bool):
                return
            toks.append(str(v))
        launchers = ("torchrun", "accelerate", "deepspeed", "vllm", "text-generation-launcher", "llama-server")
        joined = " ".join(toks[:4])
        if any(t.rsplit("/", 1)[-1] in launchers for t in toks[:3]) or "vllm.entrypoints" in joined or \
                "torch.distributed.run" in joined:
            command(self.c, toks, self.loc(node.lineno))

    # ---------------------------------------------------------------------------------------------
    def _import(self, mod: str, lineno: int, level: int = 0) -> None:
        if level:
            self.local_imports.append((level, mod))
            return
        top = mod.split(".")[0]
        self.imports.add(top)
        info = PACKAGES.get(top)
        src = self.loc(lineno)
        if info:
            cat, name = info
            if cat == "web":
                self.c.signal("web_framework", name, src)
            else:
                self.c.framework(name, src)
                if cat == "cpu_ml":
                    self.c.signal("cpu_ml", name, src)
        else:
            self.local_imports.append((0, mod))

    def _collect_imports(self, tree: ast.AST) -> None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    self._import(a.name, node.lineno)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    self._import(node.module or "", node.lineno, node.level)
                    for a in node.names:
                        if not node.module:
                            self.local_imports.append((node.level, a.name))
                elif node.module:
                    self._import(node.module, node.lineno)
                    if node.module.split(".")[0] not in PACKAGES:
                        for a in node.names:
                            self.local_imports.append((0, f"{node.module}.{a.name}"))

    def _collect_assigns(self, tree: ast.AST) -> None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        self.assigns.setdefault(t.id, []).append(node.value)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
                if not isinstance(self.parents.get(node), ast.ClassDef):
                    self.assigns.setdefault(node.target.id, []).append(node.value)

    def _collect_defaults(self, tree: ast.AST) -> None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "add_argument":
                opts = [a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
                if not opts:
                    continue
                dest = None
                kw = {k.arg: k.value for k in node.keywords if k.arg}
                if "dest" in kw and isinstance(kw["dest"], ast.Constant):
                    dest = kw["dest"].value
                if dest is None:
                    longs = [o for o in opts if o.startswith("--")]
                    dest = (longs[0] if longs else opts[0]).lstrip("-").replace("-", "_")
                if "default" in kw:
                    val, conf, _, note = self.resolve(kw["default"], allow_defaults=False)
                    if val is not None:
                        self.defaults[dest] = (val, node.lineno, note or "argparse default")
                elif "action" in kw and isinstance(kw["action"], ast.Constant) and kw["action"].value == "store_true":
                    self.defaults[dest] = (False, node.lineno, "argparse flag (off unless passed)")
            elif isinstance(node, ast.ClassDef):
                for stmt in node.body:
                    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None:
                        v = stmt.value
                        if isinstance(v, ast.Call) and _dotted(v.func).endswith("field"):
                            dv = next((k.value for k in v.keywords if k.arg == "default"), None)
                            if dv is None:
                                continue
                            v = dv
                        val, conf, _, note = self.resolve(v, allow_defaults=False)
                        if val is not None:
                            self.defaults[stmt.target.id] = (val, stmt.lineno, "dataclass default")
        for dest, (val, lineno, note) in self.defaults.items():
            if val is False and note.startswith("argparse flag"):
                continue
            apply_key(self.c, (dest,), val, self.loc(lineno), "medium" if "env" not in note else "low", P_DEFAULT)
        # module/function level constants such as MAX_SEQ_LEN = 2048 or MODEL_NAME = "..."
        for name, values in self.assigns.items():
            if len(values) != 1 or not isinstance(values[0], ast.Constant):
                continue
            if values[0].value is None or isinstance(values[0].value, bool) and not name.isupper():
                continue
            node = values[0]
            apply_key(self.c, (name.lower(),), node.value, self.loc(node.lineno), "medium", P_DEFAULT)

    # ---------------------------------------------------------------------------------------------
    def resolve(self, node: ast.AST | None, allow_defaults: bool = True, depth: int = 0) -> ResolveResult:
        """Best-effort constant value of an expression: (value, confidence, prio, note)."""
        if node is None or depth > 6:
            return None, "low", P_CODE, ""
        if isinstance(node, ast.Constant):
            return node.value, "high", P_CODE, ""
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub) and isinstance(node.operand, ast.Constant):
            v = node.operand.value
            return (-v if isinstance(v, (int, float)) else None), "high", P_CODE, ""
        if isinstance(node, ast.Attribute):
            d = _dotted(node)
            if d.startswith(("torch.", "jnp.", "tf.", "np.")):
                return d, "high", P_CODE, ""
            if allow_defaults and node.attr in self.defaults:
                val, _, note = self.defaults[node.attr]
                if isinstance(val, bool) and note.startswith("argparse flag"):
                    return None, "low", P_DEFAULT, f"command-line flag --{node.attr}"
                return val, "medium" if "env" not in note else "low", P_DEFAULT, note
            return None, "low", P_CODE, f"attribute {d}"
        if isinstance(node, ast.Name):
            vals = self.assigns.get(node.id, [])
            if not vals:
                if allow_defaults and node.id in self.defaults:
                    val, _, note = self.defaults[node.id]
                    return val, "medium", P_DEFAULT, note
                return None, "low", P_CODE, f"variable {node.id}"
            resolved = [self.resolve(v, allow_defaults, depth + 1) for v in vals[-3:]]
            values = {repr(r[0]) for r in resolved}
            last = resolved[-1]
            if len(values) == 1:
                return last
            return last[0], "medium" if last[1] == "high" else last[1], last[2], last[3]
        if isinstance(node, ast.Call):
            d = _dotted(node.func)
            if d in ("os.environ.get", "os.getenv", "environ.get", "getenv"):
                var = node.args[0].value if node.args and isinstance(node.args[0], ast.Constant) else "?"
                default = node.args[1] if len(node.args) > 1 else next(
                    (k.value for k in node.keywords if k.arg == "default"), None)
                val = self.resolve(default, allow_defaults, depth + 1)[0] if default is not None else None
                return val, "low", P_DEFAULT, f"environment variable {var}"
            if d in ("int", "float", "str", "bool") and node.args:
                return self.resolve(node.args[0], allow_defaults, depth + 1)
            return None, "low", P_CODE, f"call {d}"
        if isinstance(node, ast.Subscript):
            d = _dotted(node.value)
            if d in ("os.environ", "environ"):
                key = node.slice.value if isinstance(node.slice, ast.Constant) else "?"
                return None, "low", P_CODE, f"environment variable {key}"
            return None, "low", P_CODE, "subscript"
        if isinstance(node, ast.IfExp):
            return None, "low", P_CODE, "conditional expression"
        return None, "low", P_CODE, ""

    def _resolve_call(self, node: ast.AST | None) -> ast.Call | None:
        """Follow a Name to the Call that produced it (e.g. quantization_config=bnb_config)."""
        for _ in range(4):
            if isinstance(node, ast.Call):
                return node
            if isinstance(node, ast.Name) and self.assigns.get(node.id):
                node = self.assigns[node.id][-1]
                continue
            return None
        return None

    def _conditional(self, node: ast.AST) -> bool:
        cur = self.parents.get(node)
        while cur is not None:
            if isinstance(cur, ast.IfExp):
                return True
            if isinstance(cur, ast.If) and not _is_main_guard(cur.test):
                return True
            cur = self.parents.get(cur)
        return False

    def _kw(self, call: ast.Call, ctx: str, conditional: bool = False) -> None:
        for k in call.keywords:
            if not k.arg:
                continue
            val, conf, prio, _ = self.resolve(k.value)
            if val is None:
                continue
            if isinstance(val, str) and val.startswith("torch."):
                val = val[len("torch."):]
            if conditional and conf == "high":
                conf = "medium"
            apply_key(self.c, (ctx, k.arg), val, self.loc(k.value.lineno), conf, prio)

    def _model_name(self, node: ast.AST | None, lineno: int, *, prio_delta: int = 0, conf_cap: str | None = None) -> None:
        if node is None:
            return
        val, conf, prio, note = self.resolve(node)
        src = self.loc(lineno)
        if val is None:
            if note.startswith(("environment variable", "variable", "attribute", "command-line", "call",
                                "conditional", "subscript")):
                self.c.signal("model_reference_unresolved", note, src)
            return
        if not looks_like_model_id(val):
            return
        if conf_cap == "medium" and conf == "high":
            conf = "medium"
        self.c.fact("model.name", val, src, conf, prio + prio_delta)

    # ---------------------------------------------------------------------------------------------
    def _call(self, node: ast.Call) -> None:
        d = _dotted(node.func)
        short = d.rsplit(".", 1)[-1]
        owner = d.rsplit(".", 1)[0].rsplit(".", 1)[-1] if "." in d else ""
        src = self.loc(node.lineno)
        cond = self._conditional(node)
        kw = {k.arg: k.value for k in node.keywords if k.arg}

        if short == "from_pretrained":
            if owner.startswith("Peft") or owner.startswith("AutoPeft"):
                self.c.signal("peft_adapter_load", owner, src)
                return
            arg = node.args[0] if node.args else next((kw[k] for k in MODEL_LOAD_KW if k in kw), None)
            weak = any(x in owner for x in ("Tokenizer", "Processor", "Config", "FeatureExtractor"))
            self._model_name(arg, node.lineno, prio_delta=-1 if weak else 0, conf_cap="medium" if weak else None)
            if not weak:
                self.c.signal("pretrained_load", owner or d, src)
                if owner == "FastLanguageModel":
                    self.c.framework("unsloth", src)
                self._kw(node, "from_pretrained", cond)
                qc = self._resolve_call(kw.get("quantization_config"))
                if qc is not None:
                    self._call(qc)
            return
        if short == "pipeline" and (d == "pipeline" or "transformers" in d):
            self.c.signal("inference_call", "transformers.pipeline", src)
            arg = kw.get("model") or (node.args[1] if len(node.args) > 1 else None)
            self._model_name(arg, node.lineno)
            return
        if short in ("LLM", "AsyncLLMEngine", "EngineArgs", "AsyncEngineArgs") and ("vllm" in self.imports or
                                                                                  d.startswith("vllm")):
            self.c.signal("inference_engine", "vllm", src)
            if short == "LLM":
                self.c.signal("offline_inference", "vllm.LLM", src)
            self._model_name(kw.get("model") or (node.args[0] if node.args else None), node.lineno)
            self._kw(node, "vllm", cond)
            return
        if short in ("SentenceTransformer", "CrossEncoder"):
            self._model_name(node.args[0] if node.args else kw.get("model_name_or_path"), node.lineno)
            return
        if short in ("LoraConfig", "AdaLoraConfig", "LoftQConfig", "LoKrConfig", "LoHaConfig") or (
                short == "get_peft_model"):
            self.c.signal("peft_lora_conditional" if cond else "peft_lora", short, src)
            self._kw(node, "lora", cond)
            return
        if short == "prepare_model_for_kbit_training":
            self.c.signal("kbit_training_conditional" if cond else "kbit_training", short, src)
            return
        if short == "BitsAndBytesConfig":
            self.c.signal("bitsandbytes_config", "conditional" if cond else "always", src)
            self._kw(node, "bitsandbytesconfig", cond)
            return
        if short in ("GPTQConfig", "AwqConfig", "HqqConfig", "QuantoConfig", "TorchAoConfig", "FbgemmFp8Config"):
            q = {"GPTQConfig": "gptq", "AwqConfig": "awq", "FbgemmFp8Config": "fp8"}.get(short)
            if q:
                self.c.fact("quantization", q, src, "medium" if cond else "high")
            self._kw(node, short.lower(), cond)
            return
        if short in TRAINING_ARG_CLASSES:
            self.c.signal("training_config", short, src)
            self._kw(node, short.lower(), cond)
            return
        if short.endswith("Trainer") and short not in ("Trainer",) or short == "Trainer":
            is_lightning = owner in ("pl", "L", "lightning", "pytorch_lightning") or (
                "lightning" in self.c.frameworks and "transformers" not in self.imports)
            self.c.signal("hf_trainer" if not is_lightning else "lightning_trainer", short, src)
            if is_lightning:
                self._kw(node, "lightning_trainer", cond)
            else:
                for k in ("max_seq_length", "max_length"):
                    if k in kw:
                        val, conf, prio, _ = self.resolve(kw[k])
                        apply_key(self.c, (k,), val, self.loc(kw[k].lineno), conf, prio)
                pc = kw.get("peft_config")
                if pc is not None and not (isinstance(pc, ast.Constant) and pc.value is None):
                    target = pc
                    if isinstance(pc, ast.Name) and self.assigns.get(pc.id):
                        target = self.assigns[pc.id][-1]
                    if isinstance(target, ast.IfExp) or cond:
                        self.c.signal("peft_lora_conditional", f"{short}(peft_config=...)", src)
                    else:
                        self.c.signal("peft_lora", f"{short}(peft_config=...)", src)
            return
        if short == "DataLoader":
            first = node.args[0] if node.args else kw.get("dataset")
            if first is not None and _EVAL_NAME.search(_dotted(first) if not isinstance(first, ast.Call) else ""):
                return
            bs = kw.get("batch_size") or (node.args[1] if len(node.args) > 1 else None)
            val, conf, prio, _ = self.resolve(bs)
            n = as_int(val)
            if n:
                self.c.fact("training.micro_batch_size", n, self.loc(bs.lineno), "medium" if conf == "high" else conf,
                            prio)
            return
        if short in OPTIMIZERS and ("optim" in d.lower() or short in ("AdamW8bit", "PagedAdamW8bit", "Adafactor")
                                    or d == short):
            opt = OPTIMIZERS[short]
            if opt:
                self.c.fact("training.optimizer", opt, src, "medium" if cond else "high")
            return
        if short == "Accelerator":
            self.c.signal("accelerate", "Accelerator()", src)
            self._kw(node, "accelerator", cond)
            return
        if short in ("autocast",):
            dt = kw.get("dtype")
            if dt is not None:
                val = self.resolve(dt)[0]
                p = map_dtype(val) if isinstance(val, str) else None
                if p:
                    self.c.fact("precision", p, src, "medium")
            return
        if short in SERVER_CLASSES:
            self.c.signal("server", short, src)
            return
        if short == "run" and owner in ("uvicorn", "app", "socketio", "web"):
            self.c.signal("server", f"{owner}.run", src)
            return
        if d in ("deepspeed.initialize",):
            self.c.signal("training_loop", d, src)
            return
        if short == "init_process_group":
            self.c.signal("distributed", d, src)
            return
        if short == "gradient_checkpointing_enable":
            self.c.fact("training.gradient_checkpointing", True, src, "medium" if cond else "high")
            return
        if short == "backward" or (short in ("step", "zero_grad") and "optim" in owner.lower()):
            self.c.signal("training_loop", f"{short}()", src)
            return
        if short == "train" and "trainer" in owner.lower():
            self.c.signal("training_loop", f"{owner}.train()", src)
            return
        if short == "fit" and (self.imports & {"tensorflow", "keras", "lightning", "pytorch_lightning"}):
            self.c.signal("training_loop", f"{owner}.fit()", src)
            return
        if short == "generate" and owner not in ("np", "random"):
            self.c.signal("inference_call", f"{owner}.generate()", src)
            return
        if short in ("half", "bfloat16") and not node.args:
            self.c.fact("precision", "fp16" if short == "half" else "bf16", src, "medium")
            return
        if short == "to" and node.args:
            for a in node.args:
                v = self.resolve(a)[0]
                if isinstance(v, str) and v.startswith("torch.") and map_dtype(v):
                    self.c.fact("precision", map_dtype(v), src, "medium")
            return
        if "token" in short.lower() and "max_length" in kw:
            val, conf, prio, _ = self.resolve(kw["max_length"])
            n = as_int(val)
            if n and n >= 16:
                self.c.fact("training.seq_len", n, src, "medium", prio)
            return
        if short in ("tokenizer", "tok") and "max_length" in kw:
            val, conf, prio, _ = self.resolve(kw["max_length"])
            n = as_int(val)
            if n and n >= 16:
                self.c.fact("training.seq_len", n, src, "medium", prio)
            return
        if short == "compile" and "optimizer" in kw:  # keras model.compile(optimizer="adam")
            val = self.resolve(kw["optimizer"])[0]
            o = map_optimizer(val) if isinstance(val, str) else None
            if o:
                self.c.fact("training.optimizer", o, src, "medium")

    def _env_assign(self, node: ast.Assign) -> None:
        for t in node.targets:
            if (isinstance(t, ast.Subscript) and _dotted(t.value) in ("os.environ", "environ")
                    and isinstance(t.slice, ast.Constant) and t.slice.value == "CUDA_VISIBLE_DEVICES"
                    and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
                ids = [x for x in node.value.value.split(",") if x.strip()]
                if ids:
                    self.c.fact("hardware_hints.gpu_count", len(ids), self.loc(node.lineno), "medium", P_DEFAULT)
