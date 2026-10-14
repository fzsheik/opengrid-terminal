"""A deliberately small YAML subset parser (stdlib has none and we take no dependencies).

Supports block mappings and sequences by indentation, ``- key: value`` items, flow lists/maps on one
line, quoted scalars, comments, block scalars (``|``/``>``, content kept as text), multiple documents,
and records the source line of every key. Anchors/tags are stripped; aliases stay strings. It never
constructs objects from tags, so it is safe on untrusted input.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_INT = re.compile(r"^[-+]?(?:0|[1-9][0-9_]*)$")
_FLOAT = re.compile(r"^[-+]?(?:\d[\d_]*\.\d*|\.\d+|\d[\d_]*)(?:[eE][-+]?\d+)?$")


@dataclass
class _Line:
    indent: int
    text: str
    lineno: int


def _strip_comment(line: str) -> str:
    out = []
    quote = None
    prev = " "
    for ch in line:
        if quote:
            if ch == quote:
                quote = None
        elif ch in ("'", '"') and prev in " \t[{,:-":
            quote = ch
        elif ch == "#" and prev in " \t":
            break
        out.append(ch)
        prev = ch
    return "".join(out).rstrip()


def scalar(tok: str) -> Any:
    t = tok.strip()
    if not t:
        return None
    # tags / anchors
    while t.startswith(("!", "&")):
        parts = t.split(None, 1)
        t = parts[1] if len(parts) > 1 else ""
    if not t:
        return None
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "'\"":
        body = t[1:-1]
        if t[0] == "'":
            return body.replace("''", "'")
        return body.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")
    if t[0] == "[" and t[-1] == "]":
        return [scalar(x) for x in _split_flow(t[1:-1])]
    if t[0] == "{" and t[-1] == "}":
        out = {}
        for item in _split_flow(t[1:-1]):
            k, sep, v = item.partition(":")
            if sep:
                out[str(scalar(k))] = scalar(v)
        return out
    low = t.lower()
    if low in ("null", "~"):
        return None
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if _INT.match(t):
        try:
            return int(t.replace("_", ""))
        except ValueError:
            return t
    if _FLOAT.match(t) and any(c.isdigit() for c in t):
        try:
            return float(t.replace("_", ""))
        except ValueError:
            return t
    return t


def _split_flow(s: str) -> list[str]:
    items, depth, cur, quote = [], 0, [], None
    for ch in s:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "'\"":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == "," and depth == 0:
            items.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    if "".join(cur).strip():
        items.append("".join(cur).strip())
    return items


def _split_key(text: str) -> tuple[str, str] | None:
    """Split ``key: value`` (key may be quoted). None if the line is not a mapping entry."""
    if text[:1] in "'\"":
        q = text[0]
        end = text.find(q, 1)
        if end > 0 and text[end + 1:end + 2] == ":" and (len(text) == end + 2 or text[end + 2] in " \t"):
            return text[1:end], text[end + 2:].strip()
        return None
    if text.startswith(("[", "{")):
        return None
    m = re.match(r"^([^:#]+?|[^\s:][^#]*?):(?:\s+|$)(.*)$", text)
    if not m:
        return None
    key = m.group(1).strip()
    if not key or "://" in key:
        return None
    return key, m.group(2).strip()


class _Parser:
    def __init__(self, lines: list[_Line]):
        self.lines = lines
        self.i = 0
        self.locs: dict[tuple, int] = {}

    def parse_block(self, indent: int, path: tuple) -> Any:
        if self.i >= len(self.lines):
            return None
        ln = self.lines[self.i]
        if ln.text == "-" or ln.text.startswith("- "):
            return self.parse_seq(ln.indent, path)
        return self.parse_map(ln.indent, path)

    def _block_scalar(self, parent_indent: int) -> str:
        parts = []
        while self.i < len(self.lines) and self.lines[self.i].indent > parent_indent:
            parts.append(self.lines[self.i].text)
            self.i += 1
        return "\n".join(parts)

    def _value(self, rest: str, indent: int, path: tuple, lineno: int) -> Any:
        if rest in ("|", ">", "|-", ">-", "|+", ">+") or re.match(r"^[|>][-+]?\d*$", rest):
            return self._block_scalar(indent)
        if rest == "" or re.match(r"^[&!]\S+$", rest):
            if self.i < len(self.lines):
                nxt = self.lines[self.i]
                if nxt.indent > indent:
                    return self.parse_block(nxt.indent, path)
                if nxt.indent == indent and (nxt.text == "-" or nxt.text.startswith("- ")):
                    return self.parse_seq(indent, path)
            return None
        return scalar(rest)

    def parse_map(self, indent: int, path: tuple) -> dict:
        out: dict[str, Any] = {}
        while self.i < len(self.lines):
            ln = self.lines[self.i]
            if ln.indent < indent:
                break
            if ln.indent > indent:  # continuation of a plain multi-line scalar; ignore
                self.i += 1
                continue
            if ln.text == "-" or ln.text.startswith("- "):
                break
            kv = _split_key(ln.text)
            self.i += 1
            if kv is None:
                continue
            key, rest = kv
            sub = path + (key,)
            self.locs.setdefault(sub, ln.lineno)
            out[key] = self._value(rest, indent, sub, ln.lineno)
        return out

    def parse_seq(self, indent: int, path: tuple) -> list:
        out: list[Any] = []
        idx = 0
        while self.i < len(self.lines):
            ln = self.lines[self.i]
            if ln.indent != indent or not (ln.text == "-" or ln.text.startswith("- ")):
                if ln.indent > indent:
                    self.i += 1
                    continue
                break
            rest = ln.text[1:].strip()
            sub = path + (idx,)
            self.locs.setdefault(sub, ln.lineno)
            if rest == "":
                self.i += 1
                if self.i < len(self.lines) and self.lines[self.i].indent > indent:
                    out.append(self.parse_block(self.lines[self.i].indent, sub))
                else:
                    out.append(None)
            elif _split_key(rest) is not None:
                # "- key: value" starts a mapping whose indent is the column of "key"
                col = ln.indent + (len(ln.text) - len(ln.text[1:].lstrip()))
                self.lines[self.i] = _Line(col, rest, ln.lineno)
                out.append(self.parse_map(col, sub))
            else:
                self.i += 1
                out.append(scalar(rest))
            idx += 1
        return out


def parse(text: str) -> tuple[list[Any], dict[tuple, int]]:
    """Return (documents, {(doc_index, *path): line}) for a YAML text."""
    docs: list[Any] = []
    locs: dict[tuple, int] = {}
    current: list[_Line] = []

    def flush() -> None:
        if not current:
            return
        p = _Parser(current)
        try:
            doc = p.parse_block(current[0].indent, ())
        except RecursionError:
            doc = None
        di = len(docs)
        docs.append(doc)
        for k, v in p.locs.items():
            locs[(di,) + k] = v

    for lineno, raw in enumerate(text.splitlines(), 1):
        if raw.strip() in ("---", "...") or raw.startswith("--- "):
            flush()
            current = []
            continue
        if raw.lstrip().startswith("#") or not raw.strip():
            continue
        if raw.startswith("%"):
            continue
        stripped = _strip_comment(raw.replace("\t", "  "))
        if not stripped.strip():
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        current.append(_Line(indent, stripped.strip(), lineno))
    flush()
    return docs, locs


def flatten(obj: Any, path: tuple = ()) -> list[tuple[tuple, Any]]:
    """Leaves of a nested dict/list as (path, value). Lists of scalars are kept as a leaf too."""
    out: list[tuple[tuple, Any]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.extend(flatten(v, path + (k,)))
    elif isinstance(obj, list):
        if all(not isinstance(v, (dict, list)) for v in obj):
            out.append((path, obj))
        for i, v in enumerate(obj):
            out.extend(flatten(v, path + (i,)))
    else:
        out.append((path, obj))
    return out
