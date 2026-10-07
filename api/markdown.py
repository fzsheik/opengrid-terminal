"""A small, safe Markdown -> HTML renderer for the methodology docs.

Why not a library: the docs use a small subset (headings, paragraphs, lists, tables, code,
bold/italic, links, blockquotes, rules) and the output is served inside our own pages, so the
renderer must be safe by construction. Everything is HTML-escaped first; markup is only ever
added by this module. Raw HTML in the source is shown as text, never rendered. Link targets
are limited to http(s), mailto, site-relative paths and anchors.
"""

import html
import re

_FENCE = re.compile(r"^\s*(```|~~~)\s*([A-Za-z0-9_+-]*)\s*$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_HR = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_LIST = re.compile(r"^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_QUOTE = re.compile(r"^\s*>\s?(.*)$")

_LINK = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)(?:\s+&quot;[^&]*&quot;)?\)")
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*|__(?=\S)(.+?)(?<=\S)__")
_ITALIC = re.compile(r"(?<![\w*])\*(?=\S)(.+?)(?<=\S)\*(?![\w*])|(?<![\w_])_(?=\S)(.+?)(?<=\S)_(?![\w_])")
_SAFE_URL = re.compile(r"^(https?://|mailto:|/(?!/)|#|\./|\.\./|[A-Za-z0-9_-]+(\.md)?(#.*)?$)", re.I)


def esc(s: str) -> str:
    return html.escape(s, quote=True)


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "section"


def _safe_url(escaped_url: str) -> str | None:
    raw = html.unescape(escaped_url).strip()
    if any(ord(c) < 32 for c in raw) or not _SAFE_URL.match(raw):
        return None
    # relative "other-doc.md" -> "/methodology/other-doc"
    m = re.match(r"^(?:\./)?([A-Za-z0-9_-]+)\.md(#.*)?$", raw)
    if m:
        raw = f"/methodology/{m.group(1)}{m.group(2) or ''}"
    return esc(raw)


def inline(text: str) -> str:
    """Escape, then add code spans, links, bold and italic."""
    out = []
    for i, part in enumerate(re.split(r"(`[^`\n]+`)", text)):
        if i % 2:
            out.append(f"<code>{esc(part[1:-1])}</code>")
            continue
        s = esc(part)

        def link(m):
            url = _safe_url(m.group(2))
            label = m.group(1)
            if url is None:
                return label
            ext = ' rel="noopener" target="_blank"' if url.startswith("http") else ""
            return f'<a href="{url}"{ext}>{label}</a>'

        s = _LINK.sub(link, s)
        s = _BOLD.sub(lambda m: f"<strong>{m.group(1) or m.group(2)}</strong>", s)
        s = _ITALIC.sub(lambda m: f"<em>{m.group(1) or m.group(2)}</em>", s)
        out.append(s)
    return "".join(out)


def _cells(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", line)]


def _starts_block(lines: list[str], i: int) -> bool:
    ln = lines[i]
    return bool(
        _FENCE.match(ln) or _HEADING.match(ln) or _HR.match(ln) or _LIST.match(ln) or _QUOTE.match(ln)
        or ("|" in ln and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]))
    )


def _indent(s: str) -> int:
    return len(s) - len(s.lstrip(" "))


def _blocks(lines: list[str]) -> list[str]:
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        ln = lines[i].replace("\t", "    ")
        if not ln.strip():
            i += 1
            continue
        m = _FENCE.match(ln)
        if m:
            fence, lang = m.group(1), m.group(2)
            body = []
            i += 1
            while i < n and not lines[i].strip().startswith(fence):
                body.append(lines[i])
                i += 1
            i += 1
            cls = f' class="language-{esc(lang.lower())}"' if lang else ""
            out.append(f"<pre><code{cls}>{esc(chr(10).join(body))}</code></pre>")
            continue
        m = _HEADING.match(ln)
        if m:
            level, text = len(m.group(1)), m.group(2)
            out.append(f'<h{level} id="{slugify(text)}">{inline(text)}</h{level}>')
            i += 1
            continue
        if _HR.match(ln):
            out.append("<hr>")
            i += 1
            continue
        if "|" in ln and i + 1 < n and _TABLE_SEP.match(lines[i + 1]):
            head = _cells(ln)
            aligns = []
            for c in _cells(lines[i + 1]):
                aligns.append("center" if c.startswith(":") and c.endswith(":") else "right" if c.endswith(":") else "")
            i += 2
            body = []
            while i < n and lines[i].strip() and "|" in lines[i]:
                body.append(_cells(lines[i]))
                i += 1

            def td(tag, c, k):
                a = aligns[k] if k < len(aligns) and aligns[k] else ""
                return f'<{tag}{f" style=text-align:{a}" if a else ""}>{inline(c)}</{tag}>'

            rows = ["<tr>" + "".join(td("th", c, k) for k, c in enumerate(head)) + "</tr>"]
            for r in body:
                r = (r + [""] * len(head))[: len(head)]
                rows.append("<tr>" + "".join(td("td", c, k) for k, c in enumerate(r)) + "</tr>")
            out.append("<table><thead>" + rows[0] + "</thead><tbody>" + "".join(rows[1:]) + "</tbody></table>")
            continue
        if _QUOTE.match(ln):
            body = []
            while i < n and lines[i].strip() and _QUOTE.match(lines[i]):
                body.append(_QUOTE.match(lines[i]).group(1))
                i += 1
            out.append("<blockquote>" + "".join(_blocks(body)) + "</blockquote>")
            continue
        m = _LIST.match(ln)
        if m:
            base = _indent(ln)
            ordered = m.group(2)[0].isdigit()
            items: list[list[str]] = []
            while i < n:
                cur = lines[i].replace("\t", "    ")
                lm = _LIST.match(cur)
                if lm and _indent(cur) == base and lm.group(2)[0].isdigit() == ordered:
                    items.append([lm.group(3)])
                elif cur.strip() and _indent(cur) > base and items:
                    items[-1].append(cur[min(_indent(cur), base + 4 if _indent(cur) >= base + 4 else _indent(cur)):])
                elif not cur.strip() and i + 1 < n and items and _indent(lines[i + 1]) > base and lines[i + 1].strip():
                    items[-1].append("")
                else:
                    break
                i += 1
            lis = []
            for it in items:
                head, rest = [], []
                for k, t in enumerate(it):
                    if k and (not t.strip() or _starts_block(it, k)):
                        rest = it[k:]
                        break
                    head.append(t.strip())
                lis.append("<li>" + inline(" ".join(head)) + "".join(_blocks(rest)) + "</li>")
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>" + "".join(lis) + f"</{tag}>")
            continue
        para = []
        while i < n and lines[i].strip() and (not para or not _starts_block(lines, i)):
            para.append(lines[i].strip())
            i += 1
        out.append("<p>" + inline(" ".join(para)) + "</p>")
    return out


def render(md: str) -> str:
    """Markdown source -> safe HTML fragment."""
    return "\n".join(_blocks(md.replace("\r\n", "\n").replace("\r", "\n").split("\n")))


def title(md: str, default: str = "") -> str:
    """The first heading's text (plain), for page titles."""
    for line in md.splitlines():
        m = _HEADING.match(line)
        if m:
            return re.sub(r"[`*_]", "", m.group(2)).strip()
    return default
