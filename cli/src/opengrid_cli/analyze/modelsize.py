"""Exact parameter counts of simple PyTorch ``nn.Module`` classes, from source (``ast`` only: nothing is run).

A class is counted only when every layer assigned in its ``__init__`` is a known layer with arguments that
resolve to integers (literals, ``__init__`` defaults, module/class constants, simple arithmetic), a local
module class we can count the same way, or a container of those (``nn.Sequential``, ``nn.ModuleList``,
``[Layer(...) for _ in range(n)]``). Anything else (pretrained factories, RNNs, layers built in loops or
under ``if``) makes the class "unknown": we never guess a parameter count.

Also: well-known torchvision training datasets -> number of training samples.

    scan(tree) -> {"classes": {name: ClassInfo}, "called": set[str], "datasets": [(lineno, name, samples)]}
    count(classes, name) -> (params | None, reason)
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from math import prod
from typing import Any

MAX_DEPTH = 4

ZERO_PARAM = {
    "ReLU", "ReLU6", "GELU", "SiLU", "Tanh", "Sigmoid", "Softmax", "LogSoftmax", "LeakyReLU", "ELU", "SELU", "Mish",
    "Hardswish", "Hardsigmoid", "Softplus", "Dropout", "Dropout1d", "Dropout2d", "Dropout3d", "AlphaDropout",
    "MaxPool1d", "MaxPool2d", "MaxPool3d", "AvgPool1d", "AvgPool2d", "AvgPool3d", "AdaptiveAvgPool1d",
    "AdaptiveAvgPool2d", "AdaptiveAvgPool3d", "AdaptiveMaxPool1d", "AdaptiveMaxPool2d", "AdaptiveMaxPool3d",
    "Flatten", "Unflatten", "Identity", "Upsample", "UpsamplingNearest2d", "UpsamplingBilinear2d", "PixelShuffle",
    "ZeroPad2d", "ConstantPad2d", "ReflectionPad2d", "CrossEntropyLoss", "MSELoss", "NLLLoss", "BCELoss",
    "BCEWithLogitsLoss", "L1Loss", "SmoothL1Loss",
}
# torchvision.datasets class -> training-split size (torchvision docs / the datasets' papers)
DATASET_TRAIN_SAMPLES = {
    "MNIST": 60_000, "FashionMNIST": 60_000, "KMNIST": 60_000, "EMNIST": None, "QMNIST": 60_000,
    "CIFAR10": 50_000, "CIFAR100": 50_000, "SVHN": 73_257, "STL10": 5_000, "ImageNet": 1_281_167,
    "Food101": 75_750, "Flowers102": 1_020, "OxfordIIITPet": 3_680,
}


class Unknown(Exception):
    """The class can't be counted exactly; the message says why."""


@dataclass
class ClassInfo:
    name: str
    lineno: int
    params: list[str] = field(default_factory=list)       # __init__ positional parameter names (after self)
    defaults: dict[str, ast.AST] = field(default_factory=dict)
    layers: list[ast.AST] = field(default_factory=list)   # RHS of self.<attr> = <call> in __init__
    problem: str | None = None                            # structural reason we can't count it
    consts: dict[str, ast.AST] = field(default_factory=dict)
    has_conv: bool = False
    is_module: bool = False


def _last(name: ast.AST) -> str:
    """'nn.Conv2d' / 'torch.nn.Conv2d' / 'Conv2d' -> 'Conv2d'."""
    if isinstance(name, ast.Attribute):
        return name.attr
    if isinstance(name, ast.Name):
        return name.id
    return ""


def _module_base(b: ast.AST) -> bool:
    return _last(b) in ("Module", "LightningModule")


def scan(tree: ast.AST) -> dict[str, Any]:
    module_consts: dict[str, ast.AST] = {}
    for node in getattr(tree, "body", []):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            module_consts[node.targets[0].id] = node.value
    classes: dict[str, ClassInfo] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            ci = ClassInfo(node.name, node.lineno, is_module=any(_module_base(b) for b in node.bases))
            ci.consts = dict(module_consts)
            for st in node.body:
                if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
                    ci.consts[st.targets[0].id] = st.value
            init = next((st for st in node.body if isinstance(st, ast.FunctionDef) and st.name == "__init__"), None)
            if init is not None:
                args = init.args.args[1:]
                ci.params = [a.arg for a in args]
                for a, d in zip(args[len(args) - len(init.args.defaults):], init.args.defaults):
                    ci.defaults[a.arg] = d
                for a, d in zip(init.args.kwonlyargs, init.args.kw_defaults):
                    if d is not None:
                        ci.defaults[a.arg] = d
                _collect_layers(ci, init)
            classes[node.name] = ci
    called: set[str] = set()
    datasets: list[tuple[int, str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            nm = _last(node.func)
            if isinstance(node.func, ast.Name):
                called.add(nm)
            if nm in DATASET_TRAIN_SAMPLES and DATASET_TRAIN_SAMPLES[nm] and _dataset_is_train(node):
                datasets.append((node.lineno, nm, DATASET_TRAIN_SAMPLES[nm]))
    return {"classes": classes, "called": called, "datasets": datasets}


def _dataset_is_train(call: ast.Call) -> bool:
    for kw in call.keywords:
        if kw.arg == "train":
            return not (isinstance(kw.value, ast.Constant) and kw.value.value is False)
        if kw.arg == "split":
            return isinstance(kw.value, ast.Constant) and kw.value.value == "train"
    return True  # torchvision defaults: train=True / split="train"


def _is_self_attr(t: ast.AST) -> bool:
    return isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == "self"


def _collect_layers(ci: ClassInfo, init: ast.FunctionDef) -> None:
    for st in init.body:
        if isinstance(st, (ast.Assign, ast.AnnAssign)):
            targets = st.targets if isinstance(st, ast.Assign) else [st.target]
            if any(_is_self_attr(t) for t in targets) and isinstance(st.value, (ast.Call, ast.ListComp, ast.List)):
                ci.layers.append(st.value)
                if any(isinstance(n, ast.Call) and _last(n.func).startswith(("Conv", "ConvTranspose"))
                       for n in ast.walk(st.value)):
                    ci.has_conv = True
        elif isinstance(st, ast.Expr) and isinstance(st.value, ast.Call):
            f = st.value.func
            if isinstance(f, ast.Attribute) and f.attr in ("add_module", "register_parameter", "append", "extend"):
                ci.problem = f"layers added with .{f.attr}() in __init__"
        else:
            # layers created under if / for / with / try depend on runtime values
            for n in ast.walk(st):
                if isinstance(n, (ast.Assign, ast.AnnAssign)):
                    targets = n.targets if isinstance(n, ast.Assign) else [n.target]
                    if any(_is_self_attr(t) for t in targets):
                        ci.problem = f"layers assigned inside a {type(st).__name__.lower()} statement (line {st.lineno}), so they depend on runtime values"
                        return
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in (
                        "add_module", "append", "extend", "register_parameter"):
                    ci.problem = f"layers added with .{n.func.attr}() inside a block (line {st.lineno})"
                    return


class _Counter:
    def __init__(self, classes: dict[str, ClassInfo]) -> None:
        self.classes = classes

    # ---- integer resolution
    def num(self, node: ast.AST | None, env: dict[str, Any], ci: ClassInfo, depth: int = 0) -> Any:
        if node is None or depth > 12:
            raise Unknown("an argument could not be resolved")
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, bool)):
            return node.value
        if isinstance(node, ast.Constant) and isinstance(node.value, float) and node.value.is_integer():
            return int(node.value)
        if isinstance(node, (ast.Tuple, ast.List)):
            return tuple(self.num(e, env, ci, depth + 1) for e in node.elts)
        if isinstance(node, ast.Name):
            if node.id in env:
                return env[node.id]
            if node.id in ci.consts:
                return self.num(ci.consts[node.id], env, ci, depth + 1)
            raise Unknown(f"'{node.id}' is not a constant we can resolve")
        if isinstance(node, ast.Attribute) and _is_self_attr(node) and node.attr in env:
            return env[node.attr]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -self.num(node.operand, env, ci, depth + 1)
        if isinstance(node, ast.BinOp):
            a, b = self.num(node.left, env, ci, depth + 1), self.num(node.right, env, ci, depth + 1)
            if not (isinstance(a, int) and isinstance(b, int)):
                raise Unknown("non-integer arithmetic in a layer size")
            ops = {ast.Add: a + b, ast.Sub: a - b, ast.Mult: a * b}
            if type(node.op) in ops:
                return ops[type(node.op)]
            if isinstance(node.op, ast.FloorDiv) and b:
                return a // b
            if isinstance(node.op, ast.Pow) and 0 <= b <= 8:
                return a ** b
            raise Unknown("unsupported arithmetic in a layer size")
        raise Unknown("a layer size is computed at runtime")

    @staticmethod
    def _arg(call: ast.Call, pos: int, kw: str) -> ast.AST | None:
        for k in call.keywords:
            if k.arg == kw:
                return k.value
        return call.args[pos] if len(call.args) > pos else None

    def _flag(self, call: ast.Call, kw: str, default: bool, env, ci, pos: int | None = None) -> bool:
        node = self._arg(call, pos, kw) if pos is not None else next((k.value for k in call.keywords if k.arg == kw), None)
        return default if node is None else bool(self.num(node, env, ci))

    # ---- layers
    def layer(self, node: ast.AST, env: dict[str, Any], ci: ClassInfo, depth: int) -> int:
        if depth > MAX_DEPTH:
            raise Unknown("modules nested too deeply")
        if isinstance(node, ast.List):
            return sum(self.layer(e, env, ci, depth + 1) for e in node.elts)
        if isinstance(node, ast.ListComp):
            if len(node.generators) != 1 or node.generators[0].ifs:
                raise Unknown("a list comprehension of layers we can't size")
            it = node.generators[0].iter
            if not (isinstance(it, ast.Call) and _last(it.func) == "range" and len(it.args) == 1):
                raise Unknown("layers built from a loop over runtime values")
            n = self.num(it.args[0], env, ci)
            loopvar = node.generators[0].target
            if isinstance(loopvar, ast.Name) and loopvar.id != "_" and any(
                    isinstance(x, ast.Name) and x.id == loopvar.id for x in ast.walk(node.elt)):
                raise Unknown("layer sizes depend on the loop index")
            return n * self.layer(node.elt, env, ci, depth + 1)
        if not isinstance(node, ast.Call):
            raise Unknown("a non-layer value assigned as a module")
        if any(isinstance(a, ast.Starred) for a in node.args) or any(k.arg is None for k in node.keywords):
            raise Unknown("layers passed with * or ** unpacking")
        n = _last(node.func)
        A = lambda pos, kw: self._arg(node, pos, kw)  # noqa: E731
        num = lambda x: self.num(x, env, ci)          # noqa: E731
        if n in ZERO_PARAM:
            return 0
        if n in ("Sequential", "ModuleList"):
            if n == "ModuleList" and node.args:
                return self.layer(node.args[0], env, ci, depth + 1)
            return sum(self.layer(a, env, ci, depth + 1) for a in node.args)
        if n == "Linear":
            i, o = num(A(0, "in_features")), num(A(1, "out_features"))
            return i * o + (o if self._flag(node, "bias", True, env, ci, 2) else 0)
        if n in ("Conv1d", "Conv2d", "Conv3d", "ConvTranspose1d", "ConvTranspose2d", "ConvTranspose3d"):
            i, o, k = num(A(0, "in_channels")), num(A(1, "out_channels")), num(A(2, "kernel_size"))
            dims = int(n[-2])
            ks = k if isinstance(k, tuple) else (k,) * dims
            g_node = next((kw.value for kw in node.keywords if kw.arg == "groups"), None)
            if g_node is None and len(node.args) > 6:
                g_node = node.args[6]
            groups = num(g_node) if g_node is not None else 1
            bias = self._flag(node, "bias", True, env, ci, 7)
            return (i // groups) * o * prod(ks) + (o if bias else 0)
        if n in ("BatchNorm1d", "BatchNorm2d", "BatchNorm3d", "SyncBatchNorm"):
            c = num(A(0, "num_features"))
            return 2 * c if self._flag(node, "affine", True, env, ci) else 0
        if n == "LayerNorm":
            s = num(A(0, "normalized_shape"))
            size = prod(s) if isinstance(s, tuple) else s
            if not self._flag(node, "elementwise_affine", True, env, ci):
                return 0
            return 2 * size if self._flag(node, "bias", True, env, ci) else size
        if n == "GroupNorm":
            c = num(A(1, "num_channels"))
            return 2 * c if self._flag(node, "affine", True, env, ci) else 0
        if n in ("InstanceNorm1d", "InstanceNorm2d", "InstanceNorm3d"):
            c = num(A(0, "num_features"))
            return 2 * c if self._flag(node, "affine", False, env, ci) else 0
        if n == "Embedding":
            return num(A(0, "num_embeddings")) * num(A(1, "embedding_dim"))
        if n == "Parameter":
            inner = A(0, "data")
            if isinstance(inner, ast.Call) and _last(inner.func) in ("zeros", "ones", "randn", "rand", "empty"):
                dims = [num(a) for a in inner.args]
                dims = list(dims[0]) if len(dims) == 1 and isinstance(dims[0], tuple) else dims
                return prod(dims)
            raise Unknown("an nn.Parameter whose shape is computed at runtime")
        if n in self.classes and isinstance(node.func, ast.Name):
            return self.cls(self.classes[n], node, depth + 1, env, ci)
        raise Unknown(f"{n}() is not a layer we can size exactly")

    def cls(self, ci: ClassInfo, call: ast.Call | None, depth: int, caller_env: dict[str, Any] | None = None,
            caller_ci: ClassInfo | None = None) -> int:
        if ci.problem:
            raise Unknown(f"{ci.name}: {ci.problem}")
        if depth > MAX_DEPTH:
            raise Unknown("modules nested too deeply")
        env: dict[str, Any] = {}
        bound: dict[str, ast.AST] = {}
        if call is not None:
            for p, a in zip(ci.params, call.args):
                bound[p] = a
            for k in call.keywords:
                if k.arg in ci.params or k.arg in ci.defaults:
                    bound[k.arg] = k.value
        for p in ci.params + [k for k in ci.defaults if k not in ci.params]:
            if p in bound:
                env[p] = ("call", bound[p])
            elif p in ci.defaults:
                env[p] = ("default", ci.defaults[p])
        resolved: dict[str, Any] = {}
        for p, (kind, node) in env.items():
            try:
                if kind == "call":   # an argument is evaluated where the module is constructed
                    resolved[p] = self.num(node, caller_env or {}, caller_ci or ci)
                else:
                    resolved[p] = self.num(node, resolved, ci)
            except Unknown:
                pass  # only an error if a layer actually uses it
        return sum(self.layer(lay, resolved, ci, depth) for lay in ci.layers)


def count(classes: dict[str, ClassInfo], name: str) -> tuple[int | None, str]:
    ci = classes.get(name)
    if ci is None:
        return None, f"{name} is not defined in the inspected files"
    if not ci.layers and not ci.problem:
        return None, f"{name}.__init__ defines no layers we can see"
    try:
        n = _Counter(classes).cls(ci, None, 0)
    except Unknown as e:
        return None, str(e)
    except RecursionError:
        return None, "modules nested too deeply"
    if n <= 0:
        return None, f"{name} has no trainable layers we can count"
    return n, "exact count from the layer definitions"
