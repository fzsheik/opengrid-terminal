"""A small .gitignore matcher (patterns, negation, dir-only, anchoring, ``**``, character classes).

Semantics follow gitignore(5) closely enough for skipping files: the last matching rule wins, rules
from deeper .gitignore files are evaluated after (and so override) shallower ones, and a pattern
without a slash (other than a trailing one) matches a name at any depth below its .gitignore.
Directories that are ignored are pruned by the walker, so files inside them cannot be re-included
(which is also what git does).
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Rule:
    pattern: str
    regex: re.Pattern[str]
    negate: bool
    dir_only: bool


def _translate(pat: str) -> str:
    out: list[str] = []
    i, n = 0, len(pat)
    while i < n:
        c = pat[i]
        if c == "*":
            if pat.startswith("**", i):
                before_ok = i == 0 or pat[i - 1] == "/"
                after = pat[i + 2] if i + 2 < n else ""
                if before_ok and after == "/":
                    out.append("(?:.*/)?")
                    i += 3
                    continue
                if before_ok and after == "":
                    out.append(".*")
                    i += 2
                    continue
                out.append("[^/]*")
                i += 2
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = i + 1
            if j < n and pat[j] in "!^":
                j += 1
            if j < n and pat[j] == "]":
                j += 1
            while j < n and pat[j] != "]":
                j += 1
            if j >= n:
                out.append(re.escape(c))
            else:
                body = pat[i + 1:j]
                if body[:1] in "!^":
                    body = "^" + body[1:]
                body = body.replace("\\", "\\\\")
                out.append(f"[{body}]")
                i = j
        elif c == "\\" and i + 1 < n:
            i += 1
            out.append(re.escape(pat[i]))
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


def parse_rule(line: str) -> Rule | None:
    raw = line.rstrip("\n").rstrip("\r")
    # trailing spaces are ignored unless escaped with a backslash
    while raw.endswith(" ") and not raw.endswith("\\ "):
        raw = raw[:-1]
    if not raw or raw.startswith("#"):
        return None
    negate = False
    if raw.startswith("!"):
        negate = True
        raw = raw[1:]
    elif raw.startswith("\\!") or raw.startswith("\\#"):
        raw = raw[1:]
    dir_only = raw.endswith("/")
    raw = raw.rstrip("/")
    if not raw:
        return None
    anchored = "/" in raw
    raw = raw.lstrip("/")
    body = _translate(raw)
    if not anchored and not body.startswith("(?:.*/)?"):
        body = "(?:.*/)?" + body
    return Rule(pattern=line.strip(), regex=re.compile(body + r"\Z", re.DOTALL), negate=negate, dir_only=dir_only)


def parse_rules(text: str) -> list[Rule]:
    rules = []
    for line in text.splitlines():
        r = parse_rule(line)
        if r is not None:
            rules.append(r)
    return rules


class IgnoreSet:
    """Rules grouped by the directory (root-relative POSIX path, "" for root) they apply to."""

    def __init__(self) -> None:
        self._groups: list[tuple[str, list[Rule]]] = []

    def add(self, base: str, rules: list[Rule]) -> None:
        if rules:
            self._groups.append((base.strip("/"), rules))
            # shallower first so deeper files override them
            self._groups.sort(key=lambda g: (g[0].count("/") + (1 if g[0] else 0)))

    def match(self, rel: str, is_dir: bool) -> Rule | None:
        """Return the deciding rule if ``rel`` is ignored, else None."""
        decided: Rule | None = None
        ignored = False
        for base, rules in self._groups:
            if base:
                if not rel.startswith(base + "/"):
                    continue
                sub = rel[len(base) + 1:]
            else:
                sub = rel
            for rule in rules:
                if rule.dir_only and not is_dir:
                    continue
                if rule.regex.match(sub):
                    ignored = not rule.negate
                    decided = rule
        return decided if ignored else None
