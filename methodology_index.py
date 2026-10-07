"""One-line summaries of the methodology documents, for GET /v1/methodology.

    methodology_summaries() -> {name: summary}

The summary is the first prose paragraph of the introduction (before the first "##" section;
not a list, table, code block or quote), skipping a paragraph that only points at code /
endpoints / version, converted to plain text and cut to <= 200 characters at a word boundary.
Names follow api/pages.py's rule (methodology/*.md, lowercase slug, README excluded).
Cached by file modification time, so editing a doc updates its summary without a restart.
"""

from __future__ import annotations

import re
from pathlib import Path

DIR = Path(__file__).resolve().parent / "methodology"
MAX = 200
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")
_cache: dict[str, tuple[float, str]] = {}


def _plain(md: str) -> str:
    s = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", md)
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"`([^`]*)`", r"\1", s)
    # emphasis only at word edges, so snake_case names and "gpu-time-*" paths survive
    s = re.sub(r"(?<![\w/*-])(\*\*|__|\*|_)(?=\S)(.+?)(?<=\S)\1(?![\w*])", r"\2", s)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", s).strip()


_BLOCK = re.compile(r"^(#|\||>|[-*+]\s|\d+[.)]\s)")
# A paragraph that only says where the code / endpoints / version live is not a summary.
_META = re.compile(r"^(version|methodology version|code:|used by|api:|endpoints?:)", re.I)


def paragraphs(md: str, intro_only: bool = False) -> list[str]:
    """Prose paragraphs, in order, as plain text; intro_only stops at the first section heading."""
    out, para, fence = [], [], False
    for line in md.splitlines() + [""]:
        t = line.strip()
        if intro_only and not fence and t.startswith("## "):
            break
        if t.startswith("```"):
            fence = not fence
            continue
        if fence:
            continue
        if not t or _BLOCK.match(t) or line.startswith("    "):
            if para:
                out.append(_plain(" ".join(para)))
                para = []
            continue
        para.append(t)
    return [p for p in out if p]


def first_paragraph(md: str) -> str:
    """The first intro paragraph that is not just code / endpoint / version pointers (else the first one)."""
    ps = paragraphs(md, intro_only=True) or paragraphs(md)
    return next((p for p in ps if not _META.match(p)), ps[0] if ps else "")


def shorten(text: str, n: int = MAX) -> str:
    if len(text) <= n:
        return text
    cut = text[: n - 1]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,;:-(") + "…"


def summary(name: str) -> str | None:
    p = DIR / f"{name}.md"
    if not _NAME.match(name) or not p.is_file():
        return None
    mtime = p.stat().st_mtime
    hit = _cache.get(name)
    if hit and hit[0] == mtime:
        return hit[1]
    s = shorten(first_paragraph(p.read_text(encoding="utf-8")))
    _cache[name] = (mtime, s)
    return s


def methodology_summaries() -> dict[str, str]:
    names = sorted(p.stem for p in DIR.glob("*.md") if p.stem.lower() != "readme" and _NAME.match(p.stem))
    return {n: summary(n) or "" for n in names}
