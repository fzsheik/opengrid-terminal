"""File access policy for the local analyzer.

Rules (all enforced here, nowhere else opens project files):

* Only paths inside the explicitly given root are considered. The root is resolved once; every read
  re-checks that the real path is inside it. Symlinked directories are never followed; symlinked files
  are followed only when their target is a regular file inside the root that is itself allowed.
* Only regular files are read (no FIFOs, sockets, devices). Reads use O_NOFOLLOW|O_NONBLOCK, check the
  inode against the walk-time lstat, and are capped per file and in total.
* Directories pruned before descent: VCS, dependency/virtualenv, caches, build output, data/datasets,
  checkpoints and credential directories (.ssh, .aws, .kube, gcloud...). Plus .gitignore rules and
  explicit user excludes.
* Denylist (checked first; never read, listed by name only): secrets/keys/credentials, model weights,
  datasets, media, archives, binaries.
* Allowlist by name/extension: dependency manifests, container/compose files, HF/PEFT/DeepSpeed/
  Accelerate configs, training configs under config(s)/, relevant Python scripts (by name, plus local
  modules they import, one hop), notebooks (code cells), launch scripts, README (model name only),
  OpenGrid job manifests. Everything else is skipped.
"""

from __future__ import annotations

import fnmatch
import os
import stat
from dataclasses import dataclass, field

from .gitignore import IgnoreSet, parse_rule, parse_rules

MAX_DEPTH = 12
MAX_ENTRIES = 50_000
MAX_IMPORTED_MODULES = 40
MAX_READMES = 3
DEFAULT_MAX_TOTAL_BYTES = 4_000_000
MAX_GITIGNORE_BYTES = 64_000

# ------------------------------------------------------------------------------------------------
# Directory skips

BUILTIN_SKIP_DIRS: dict[str, str] = {
    ".git": "version control", ".hg": "version control", ".svn": "version control",
    "node_modules": "dependencies", "bower_components": "dependencies", "vendor": "dependencies",
    ".venv": "virtualenv", "venv": "virtualenv", "env": "virtualenv", ".env": "virtualenv",
    "virtualenv": "virtualenv", ".virtualenv": "virtualenv", "site-packages": "installed packages",
    "dist-packages": "installed packages", ".conda": "virtualenv", "conda-env": "virtualenv",
    "__pycache__": "cache", ".cache": "cache", ".mypy_cache": "cache", ".pytest_cache": "cache",
    ".ruff_cache": "cache", ".tox": "cache", ".nox": "cache", ".ipynb_checkpoints": "cache",
    ".gradle": "cache", ".next": "build output", ".turbo": "cache",
    "build": "build output", "dist": "build output", "target": "build output", "out": "build output",
    "data": "data directory", "datasets": "data directory", "dataset": "data directory",
    "checkpoints": "checkpoints", "checkpoint": "checkpoints", "ckpt": "checkpoints", "weights": "model weights",
    "wandb": "experiment logs", "mlruns": "experiment logs", "lightning_logs": "experiment logs",
    "runs": "experiment logs", "logs": "logs",
    ".ssh": "credential directory", ".aws": "credential directory", ".kube": "credential directory",
    ".gnupg": "credential directory", ".docker": "credential directory", "gcloud": "credential directory",
    ".gcloud": "credential directory", ".azure": "credential directory", ".terraform": "credential directory",
    ".huggingface": "credential directory", ".idea": "editor settings", ".vscode": "editor settings",
}

# ------------------------------------------------------------------------------------------------
# Denylist (name based, case-insensitive). Category -> reason prefix.

SECRET_EXACT = {
    ".env", ".envrc", ".netrc", "_netrc", ".npmrc", ".pypirc", ".pgpass", ".git-credentials", ".htpasswd",
    ".boto", ".s3cfg", "s3cfg", ".dockercfg", "token", "kubeconfig", "credentials", ".yarnrc.yml",
    "known_hosts", "authorized_keys",
}
SECRET_GLOBS = [
    ".env.*", "*.env", ".env-*", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore", "*.token",
    "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*", "*_rsa", "*_rsa.pub", "*.ppk", "*credential*",
    "*secret*", "kubeconfig*", "*.kubeconfig", "service-account*", "*service_account*", "*serviceaccount*",
    "*.tfstate", "*.tfstate.*", "*.tfvars", "*.gpg", "*.asc", "*.ovpn", "hf_token*", "*api_key*", "*apikey*",
    "*.crt", "*.cer", "*.der", "adc.json", "application_default_credentials*", "*.sops.*",
]
WEIGHT_EXT = {
    "safetensors", "bin", "pt", "pth", "ckpt", "gguf", "ggml", "ggjt", "onnx", "h5", "hdf5", "keras", "tflite",
    "pb", "mlmodel", "mlpackage", "engine", "plan", "pkl", "pickle", "joblib", "msgpack", "npz", "npy", "model",
    "params", "nemo", "distcp", "trt",
}
DATA_EXT = {
    "parquet", "arrow", "feather", "jsonl", "ndjson", "tfrecord", "tfrecords", "csv", "tsv", "avro", "orc",
    "lmdb", "db", "sqlite", "sqlite3", "xlsx", "xls", "mdb", "idx", "hdf", "zarr", "mat", "rec",
}
MEDIA_EXT = {
    "png", "jpg", "jpeg", "gif", "bmp", "webp", "tif", "tiff", "ico", "heic", "psd", "mp3", "wav", "flac", "ogg",
    "m4a", "aac", "opus", "mp4", "mov", "avi", "mkv", "webm", "m4v", "wmv", "pdf", "ttf", "otf", "woff", "woff2",
}
ARCHIVE_EXT = {"zip", "tar", "gz", "tgz", "bz2", "xz", "7z", "rar", "zst", "lz4", "whl", "egg", "jar", "war",
               "deb", "rpm", "dmg", "iso", "img"}
BINARY_EXT = {"so", "dylib", "dll", "exe", "o", "a", "lib", "pyc", "pyo", "pyd", "class", "wasm", "dat", "sys"}

# ------------------------------------------------------------------------------------------------
# Allowlist

PY_KEYWORDS = ("train", "finetune", "fine_tune", "fine-tune", "infer", "serve", "server", "predict", "main",
               "app", "model", "config", "sft", "lora", "launch", "wsgi", "asgi", "manage")
SH_KEYWORDS = ("train", "finetune", "fine_tune", "infer", "serve", "server", "launch", "run", "start", "sft",
               "lora", "predict", "deploy", "entrypoint")
CONFIG_DIRS = {"config", "configs", "conf", "recipes", "cfg"}
CONFIG_NAME_KEYWORDS = ("train", "finetune", "fine_tune", "sft", "lora", "qlora", "axolotl", "serve", "infer",
                        "vllm", "deepspeed", "fsdp", "dpo", "torchtune", "llama_factory", "llamafactory")

# kind -> (reading priority, reason)
KINDS: dict[str, tuple[int, str]] = {
    "gitignore": (-1, "ignore rules"),
    "requirements": (0, "dependency manifest"),
    "pyproject": (0, "dependency manifest (pyproject.toml)"),
    "setup_cfg": (0, "dependency manifest (setup.cfg)"),
    "setup_py": (0, "dependency manifest (setup.py, parsed as text, never executed)"),
    "pipfile": (0, "dependency manifest (Pipfile)"),
    "conda": (0, "conda environment"),
    "package_json": (0, "node manifest (dependency names only)"),
    "hf_config": (1, "Hugging Face model config"),
    "generation_config": (1, "Hugging Face generation config"),
    "adapter_config": (1, "PEFT adapter config"),
    "deepspeed": (1, "DeepSpeed config"),
    "accelerate": (1, "Accelerate config"),
    "opengrid_manifest": (1, "OpenGrid job manifest"),
    "config_file": (2, "training/serving config candidate"),
    "dockerfile": (3, "container build file"),
    "compose": (3, "compose file"),
    "shell": (3, "launch script"),
    "python": (4, "Python script (name suggests training/serving/model code)"),
    "python_import": (5, "local module imported by a relevant script"),
    "notebook": (6, "Jupyter notebook (code cells only)"),
    "readme": (7, "README (model name hints only; untrusted, low confidence)"),
}


def _ext(name: str) -> str:
    return name.rsplit(".", 1)[1].lower() if "." in name.strip(".") else ""


def unsafe_name(name: str) -> bool:
    """Names that try to look like paths or smuggle control characters."""
    lowered = name.lower()
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        return True
    if "\\" in name or "/" in name:
        return True
    if "%2f" in lowered or "%5c" in lowered or "%2e%2e" in lowered or "%00" in lowered:
        return True
    if name in (".", "..") or name.startswith("..") and len(name.strip(".")) == 0:
        return True
    return False


def denied(name: str, size: int | None = None) -> tuple[str, str] | None:
    """(category, reason) if the file must never be read, else None."""
    lowered = name.lower()
    if lowered in SECRET_EXACT or any(fnmatch.fnmatchcase(lowered, g) for g in SECRET_GLOBS):
        return "secret", "secret or credential file (never read)"
    ext = _ext(lowered)
    if ext in WEIGHT_EXT:
        return "weights", f"model weights or serialized object (.{ext}, never read)"
    if ext in DATA_EXT:
        if ext in ("csv", "tsv") and size is not None:
            big = " over 1 MB" if size > 1_000_000 else ""
            return "data", f"dataset file (.{ext}{big}, never read)"
        return "data", f"dataset file (.{ext}, never read)"
    if ext in MEDIA_EXT:
        return "media", f"media/document file (.{ext}, never read)"
    if ext in ARCHIVE_EXT:
        return "archive", f"archive (.{ext}, never read)"
    if ext in BINARY_EXT:
        return "binary", f"binary file (.{ext}, never read)"
    return None


def classify(name: str, rel_parts: tuple[str, ...]) -> str | None:
    """Allowlist: the kind of an allowed file, or None. ``rel_parts`` includes ``name`` last."""
    n = name.lower()
    parent_dirs = {p.lower() for p in rel_parts[:-1]}
    depth = len(rel_parts) - 1
    ext = _ext(n)
    if n == ".gitignore":
        return "gitignore"
    if n.endswith(".txt") and ("requirements" in n or "requirements" in parent_dirs):
        return "requirements"
    if n == "pyproject.toml":
        return "pyproject"
    if n == "setup.cfg":
        return "setup_cfg"
    if n == "setup.py":
        return "setup_py"
    if n == "pipfile":
        return "pipfile"
    if n in ("environment.yml", "environment.yaml") or (n.startswith("conda") and ext in ("yml", "yaml")):
        return "conda"
    if n == "package.json":
        return "package_json"
    if n in ("dockerfile", "containerfile") or n.startswith("dockerfile.") or n.endswith(".dockerfile"):
        return "dockerfile"
    if ext in ("yml", "yaml") and (n.startswith("docker-compose") or n.startswith("compose")):
        return "compose"
    if n == "config.json":
        return "hf_config"
    if n == "generation_config.json":
        return "generation_config"
    if n == "adapter_config.json":
        return "adapter_config"
    if ext == "json" and (n.startswith("ds_config") or n.startswith("deepspeed") or n.startswith("zero")
                          or "ds_z" in n):
        return "deepspeed"
    if ext in ("yml", "yaml") and (n.startswith("accelerate") or "accelerate" in parent_dirs
                                   or n in ("default_config.yaml", "fsdp_config.yaml")):
        return "accelerate"
    if n.startswith("opengrid") and ext in ("yml", "yaml", "json"):
        return "opengrid_manifest"
    if ext in ("yml", "yaml", "json") and (parent_dirs & CONFIG_DIRS):
        return "config_file"
    if ext in ("yml", "yaml") and any(k in n for k in CONFIG_NAME_KEYWORDS):
        return "config_file"
    if ext == "json" and any(k in n for k in ("train", "finetune", "sft", "lora")) and "config" in n:
        return "config_file"
    if ext == "py":
        stem = n[:-3]
        if any(k in stem for k in PY_KEYWORDS):
            return "python"
        return None
    if ext == "ipynb":
        return "notebook"
    if ext == "sh" and any(k in n for k in SH_KEYWORDS):
        return "shell"
    if (n.startswith("readme") and ext in ("", "md", "rst", "txt", "markdown")) and depth <= 1:
        return "readme"
    return None


# ------------------------------------------------------------------------------------------------
# Walk


@dataclass
class Candidate:
    rel: str            # root-relative POSIX path (what the user sees)
    abs: str            # real path that will be opened (inside root)
    size: int
    kind: str
    reason: str
    ino: tuple[int, int]
    via_symlink: bool = False
    imported_by: str | None = None

    @property
    def priority(self) -> tuple[int, int, str]:
        return (KINDS[self.kind][0], self.rel.count("/"), self.rel)


@dataclass
class Skipped:
    path: str
    reason: str
    category: str


SKIP_ORDER = ["secret", "unsafe", "symlink", "special", "weights", "data", "too_large", "binary_content",
              "budget", "gitignore", "exclude", "builtin", "media", "archive", "binary", "depth", "not_allowed"]


@dataclass
class WalkResult:
    root: str
    candidates: list[Candidate] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)
    py_pool: dict[str, Candidate] = field(default_factory=dict)  # rel -> non-allowlisted .py
    gitignores: list[Candidate] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class AccessError(Exception):
    def __init__(self, reason: str, category: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.category = category


def resolve_root(root: str | os.PathLike) -> str:
    path = os.path.realpath(os.fspath(root))
    st = os.stat(path)
    if not stat.S_ISDIR(st.st_mode):
        raise NotADirectoryError(f"not a directory: {root}")
    return path


def inside(root_real: str, path: str) -> bool:
    try:
        return os.path.commonpath([root_real, path]) == root_real
    except ValueError:
        return False


def safe_read(root_real: str, cand_abs: str, limit: int, expect_ino: tuple[int, int] | None = None) -> bytes:
    """Read a regular file inside ``root_real`` without following a final symlink. Raises AccessError."""
    real = os.path.realpath(cand_abs)
    if real != os.path.normpath(cand_abs) or not inside(root_real, real):
        raise AccessError("path resolves outside the project root or through a symlink (refused)", "symlink")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(real, flags)
    except OSError as e:
        raise AccessError(f"could not open ({e.__class__.__name__})", "special") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise AccessError("not a regular file", "special")
        if expect_ino is not None and (st.st_dev, st.st_ino) != expect_ino:
            raise AccessError("file changed during analysis (refused)", "special")
        if st.st_size > limit:
            raise AccessError(f"too large ({st.st_size} bytes > {limit} byte limit)", "too_large")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(65536, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise AccessError(f"too large (> {limit} byte limit)", "too_large")
        return b"".join(chunks)
    finally:
        os.close(fd)


def looks_binary(data: bytes) -> bool:
    head = data[:8192]
    if b"\x00" in head:
        return True
    if not head:
        return False
    ctrl = sum(1 for b in head if b < 9 or 13 < b < 32)
    return ctrl / len(head) > 0.1


def decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def walk(root_real: str, *, exclude: list[str] | tuple[str, ...] = (), max_file_bytes: int,
         read_gitignore: bool) -> WalkResult:
    """Enumerate the project (metadata only; at most reads .gitignore files when ``read_gitignore``)."""
    res = WalkResult(root=root_real)
    ignores = IgnoreSet()
    user_rules = [r for r in (parse_rule(p) for p in exclude) if r is not None]
    user_set = IgnoreSet()
    user_set.add("", user_rules)
    entries_seen = 0
    stack: list[tuple[str, str, int]] = [(root_real, "", 0)]
    real_candidates: list[Candidate] = []

    def rel_of(dir_rel: str, name: str) -> str:
        return f"{dir_rel}/{name}" if dir_rel else name

    while stack:
        dir_abs, dir_rel, depth = stack.pop()
        try:
            with os.scandir(dir_abs) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError as e:
            res.skipped.append(Skipped(dir_rel or ".", f"could not list directory ({e.__class__.__name__})",
                                       "special"))
            continue

        if not read_gitignore:
            for e in entries:
                if e.name == ".gitignore":
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        break
                    if stat.S_ISREG(st.st_mode):
                        res.gitignores.append(Candidate(rel_of(dir_rel, e.name), e.path, st.st_size, "gitignore",
                                                        KINDS["gitignore"][1], (st.st_dev, st.st_ino)))
                    break
        else:
            for e in entries:
                if e.name == ".gitignore":
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        break
                    rel = rel_of(dir_rel, e.name)
                    if not stat.S_ISREG(st.st_mode):
                        res.skipped.append(Skipped(rel, "special or symlinked .gitignore (not read)", "symlink"))
                        break
                    try:
                        data = safe_read(root_real, e.path, MAX_GITIGNORE_BYTES, (st.st_dev, st.st_ino))
                    except AccessError as err:
                        res.skipped.append(Skipped(rel, err.reason, err.category))
                        break
                    ignores.add(dir_rel, parse_rules(decode(data)))
                    res.gitignores.append(Candidate(rel, e.path, len(data), "gitignore", KINDS["gitignore"][1],
                                                    (st.st_dev, st.st_ino)))
                    break

        subdirs: list[tuple[str, str]] = []
        for e in entries:
            entries_seen += 1
            if entries_seen > MAX_ENTRIES:
                res.warnings.append(f"stopped listing after {MAX_ENTRIES} entries; analysis is partial")
                stack.clear()
                break
            name = e.name
            if unsafe_name(name):
                res.skipped.append(Skipped(rel_of(dir_rel, repr(name)[1:-1]), "unsafe file name (refused)",
                                           "unsafe"))
                continue
            rel = rel_of(dir_rel, name)
            if name == ".gitignore":
                continue
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                res.skipped.append(Skipped(rel, "could not stat", "special"))
                continue
            mode = st.st_mode

            if stat.S_ISLNK(mode):
                target = os.path.realpath(e.path)
                if not inside(root_real, target):
                    res.skipped.append(Skipped(rel, "symlink pointing outside the project root (not followed)",
                                               "symlink"))
                    continue
                try:
                    tst = os.stat(target)
                except OSError:
                    res.skipped.append(Skipped(rel, "broken symlink (not followed)", "symlink"))
                    continue
                if stat.S_ISDIR(tst.st_mode):
                    res.skipped.append(Skipped(rel, "symlinked directory (not followed)", "symlink"))
                    continue
                if not stat.S_ISREG(tst.st_mode):
                    res.skipped.append(Skipped(rel, "symlink to a special file (not followed)", "symlink"))
                    continue
                target_rel = os.path.relpath(target, root_real).replace(os.sep, "/")
                tparts = tuple(target_rel.split("/"))
                bad_dir = next((p for p in tparts[:-1] if p.lower() in BUILTIN_SKIP_DIRS), None)
                d = denied(name, tst.st_size) or denied(tparts[-1], tst.st_size)
                if d:
                    res.skipped.append(Skipped(rel, d[1], d[0]))
                    continue
                if bad_dir or ignores.match(target_rel, False) or user_set.match(target_rel, False):
                    res.skipped.append(Skipped(rel, "symlink to a skipped/ignored location (not followed)",
                                               "symlink"))
                    continue
                kind = classify(name, tuple(rel.split("/")))
                if kind is None or classify(tparts[-1], tparts) is None:
                    res.skipped.append(Skipped(rel, "symlink not in allowlist (not followed)", "not_allowed"))
                    continue
                if tst.st_size > max_file_bytes:
                    res.skipped.append(Skipped(rel, f"too large ({tst.st_size} bytes > {max_file_bytes} byte "
                                                    f"limit)", "too_large"))
                    continue
                c = Candidate(rel, target, tst.st_size, kind, KINDS[kind][1] + " (via symlink inside root)",
                              (tst.st_dev, tst.st_ino), via_symlink=True)
                real_candidates.append(c)
                continue

            if stat.S_ISDIR(mode):
                lname = name.lower()
                why = BUILTIN_SKIP_DIRS.get(lname)
                if why is None and lname.endswith(".egg-info"):
                    why = "build output"
                if why is None and os.path.lexists(os.path.join(e.path, "pyvenv.cfg")):
                    why = "virtualenv"
                if why is not None:
                    res.skipped.append(Skipped(rel + "/", f"built-in skip ({why})", "builtin"))
                    continue
                if user_set.match(rel, True):
                    res.skipped.append(Skipped(rel + "/", "excluded by user", "exclude"))
                    continue
                rule = ignores.match(rel, True)
                if rule:
                    res.skipped.append(Skipped(rel + "/", f"ignored by .gitignore ({rule.pattern})", "gitignore"))
                    continue
                if depth + 1 > MAX_DEPTH:
                    res.skipped.append(Skipped(rel + "/", f"deeper than {MAX_DEPTH} levels", "depth"))
                    continue
                subdirs.append((e.path, rel))
                continue

            if not stat.S_ISREG(mode):
                res.skipped.append(Skipped(rel, "special file (not a regular file)", "special"))
                continue

            d = denied(name, st.st_size)
            if d:
                res.skipped.append(Skipped(rel, d[1], d[0]))
                continue
            if user_set.match(rel, False):
                res.skipped.append(Skipped(rel, "excluded by user", "exclude"))
                continue
            rule = ignores.match(rel, False)
            if rule:
                res.skipped.append(Skipped(rel, f"ignored by .gitignore ({rule.pattern})", "gitignore"))
                continue
            kind = classify(name, tuple(rel.split("/")))
            if kind is None:
                if name.lower().endswith(".py"):
                    res.py_pool[rel] = Candidate(rel, e.path, st.st_size, "python_import",
                                                 KINDS["python_import"][1], (st.st_dev, st.st_ino))
                    res.skipped.append(Skipped(rel, "Python file whose name does not suggest training/serving "
                                                    "code", "not_allowed"))
                else:
                    res.skipped.append(Skipped(rel, "not in allowlist", "not_allowed"))
                continue
            if st.st_size > max_file_bytes:
                res.skipped.append(Skipped(rel, f"too large ({st.st_size} bytes > {max_file_bytes} byte limit)",
                                           "too_large"))
                continue
            real_candidates.append(Candidate(rel, e.path, st.st_size, kind, KINDS[kind][1],
                                             (st.st_dev, st.st_ino)))

        for sub in reversed(subdirs):
            stack.append((sub[0], sub[1], depth + 1))

    # A symlink inside root that points at a file we already read is redundant.
    seen_real: set[str] = set()
    ordered = sorted(real_candidates, key=lambda c: (c.via_symlink, c.priority))
    for c in ordered:
        if c.abs in seen_real:
            res.skipped.append(Skipped(c.rel, "symlink to a file that is already inspected", "symlink"))
            continue
        seen_real.add(c.abs)
        res.candidates.append(c)

    readmes = [c for c in res.candidates if c.kind == "readme"]
    for extra in sorted(readmes, key=lambda c: c.priority)[MAX_READMES:]:
        res.candidates.remove(extra)
        res.skipped.append(Skipped(extra.rel, "README limit reached", "not_allowed"))
    res.candidates.sort(key=lambda c: c.priority)
    return res


def sort_skipped(items: list[Skipped]) -> list[Skipped]:
    order = {c: i for i, c in enumerate(SKIP_ORDER)}
    return sorted(items, key=lambda s: (order.get(s.category, len(order)), s.path))
