"""Feed bodies -> plain item dicts. Stdlib only (xml.etree, json, email.utils).

One entry point, `parse(body, kind)`, returning a list of:

    {"guid", "url", "title", "summary", "author", "categories",
     "published_raw", "published_at" (aware UTC datetime or None), "raw" (the item as stored)}

Robustness, because real feeds are not always well formed:
    - XML feeds are sniffed by root element (rss / rdf:RDF / feed), not trusted by `kind`,
      so a source that switches RSS -> Atom keeps working.
    - a body that fails to parse is repaired once (control characters dropped, bare '&'
      escaped, junk before the first '<' removed) and retried; if that fails too, a
      regex pass pulls <item>/<entry> blocks out one by one, so one broken item does not
      lose the other 49.
    - documents declaring entities (<!ENTITY) are refused outright: no feed needs them and
      they are the vector for entity-expansion attacks. xml.etree never fetches external
      entities.
Times: RFC 822 (RSS), ISO 8601 (Atom, JSON Feed), and plain dates (Federal Register,
taken as 00:00 UTC on that date). Naive times are read as UTC. Anything unreadable
returns None; the caller falls back to first-seen and flags it.
"""

from __future__ import annotations

import html
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

MAX_SUMMARY = 1200
MAX_RAW_XML = 20_000

_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "dc": "http://purl.org/dc/elements/1.1/",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "rss1": "http://purl.org/rss/1.0/",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
}


class FeedError(ValueError):
    """The body is not a feed we can read at all."""


# --------------------------------------------------------------------------- text helpers

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def clean_text(s: str | None, limit: int | None = None) -> str | None:
    """HTML fragment -> plain text: tags dropped, entities unescaped, whitespace collapsed."""
    if s is None:
        return None
    t = html.unescape(_TAG.sub(" ", html.unescape(s) if "&lt;" in s else s))
    t = _WS.sub(" ", t).strip()
    if limit and len(t) > limit:
        t = t[: limit - 1].rstrip() + "…"
    return t or None


def parse_time(value: str | None) -> datetime | None:
    """RFC 822, ISO 8601 or YYYY-MM-DD -> aware UTC datetime; None when unreadable."""
    if not value:
        return None
    v = value.strip()
    dt = None
    try:
        dt = parsedate_to_datetime(v)
    except (TypeError, ValueError, IndexError):
        dt = None
    if dt is None:
        iso = v.replace("Z", "+00:00").replace("z", "+00:00")
        try:
            dt = datetime.fromisoformat(iso)
        except ValueError:
            m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", v)
            if not m:
                return None
            try:
                dt = datetime(int(m[1]), int(m[2]), int(m[3]))
            except ValueError:
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# --------------------------------------------------------------------------- XML

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_BARE_AMP = re.compile(r"&(?!(?:[a-zA-Z][a-zA-Z0-9]{1,31}|#[0-9]{1,7}|#x[0-9a-fA-F]{1,6});)")


_DECL = re.compile(r"^\s*<\?xml[^>]*\?>")


def _decode(body: bytes) -> str:
    """UTF-8 when it is; otherwise Windows-1252 (the usual mislabelled 'UTF-8' feed), never U+FFFD soup."""
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return body.decode("cp1252", errors="replace")


def _repair(text: str) -> str:
    text = _DECL.sub("", text.lstrip("\ufeff"))  # parsed as str now: the declared encoding no longer applies
    text = _CTRL.sub("", text)
    i = text.find("<")
    if i > 0:
        text = text[i:]
    return _BARE_AMP.sub("&amp;", text)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _child(el, *names):
    """First direct child whose local name is one of `names` (namespace-agnostic)."""
    for c in el:
        if _local(c.tag) in names:
            return c
    return None


def _children(el, name):
    return [c for c in el if _local(c.tag) == name]


def _text(el) -> str | None:
    if el is None:
        return None
    t = "".join(el.itertext())
    return t.strip() or None


def _rss_item(it) -> dict:
    link = _text(_child(it, "link"))
    guid_el = _child(it, "guid")
    guid = _text(guid_el)
    if not link and guid_el is not None and guid_el.get("isPermaLink", "true").lower() != "false" and guid \
            and guid.startswith("http"):
        link = guid
    if not link:
        for c in it:  # <atom:link href=...> inside RSS
            if _local(c.tag) == "link" and c.get("href"):
                link = c.get("href")
                break
    published = _text(_child(it, "pubDate")) or _text(_child(it, "date", "published", "updated"))
    summary = _text(_child(it, "description")) or _text(_child(it, "encoded"))
    author = _text(_child(it, "creator")) or _text(_child(it, "author"))
    cats = [t for c in _children(it, "category") if (t := _text(c))]
    return {"guid": guid or link, "url": link, "title": clean_text(_text(_child(it, "title"))),
            "summary": clean_text(summary, MAX_SUMMARY), "author": clean_text(author),
            "categories": cats[:20], "published_raw": published}


def _atom_entry(e) -> dict:
    link = None
    for l in _children(e, "link"):
        rel = l.get("rel", "alternate")
        if rel == "alternate" and l.get("href"):
            link = l.get("href")
            break
        link = link or l.get("href")
    author_el = _child(e, "author")
    author = _text(_child(author_el, "name")) if author_el is not None else None
    published = _text(_child(e, "published")) or _text(_child(e, "updated")) or _text(_child(e, "issued", "modified"))
    summary = _text(_child(e, "summary")) or _text(_child(e, "content"))
    cats = [c.get("term") or c.get("label") for c in _children(e, "category")]
    gid = _text(_child(e, "id"))
    return {"guid": gid or link, "url": link, "title": clean_text(_text(_child(e, "title"))),
            "summary": clean_text(summary, MAX_SUMMARY), "author": clean_text(author),
            "categories": [c for c in cats if c][:20], "published_raw": published}


def _from_tree(root) -> list[dict]:
    name = _local(root.tag)
    out = []
    if name == "rss":
        channel = _child(root, "channel")
        items = _children(channel, "item") if channel is not None else []
        for it in items:
            out.append((_rss_item(it), it))
    elif name == "RDF":
        for it in _children(root, "item"):
            out.append((_rss_item(it), it))
    elif name == "feed":
        for e in _children(root, "entry"):
            out.append((_atom_entry(e), e))
    else:
        raise FeedError(f"not a feed: root element <{name}>")
    items = []
    for d, el in out:
        raw = ET.tostring(el, encoding="unicode")
        d["raw"] = {"xml": raw[:MAX_RAW_XML], "truncated": len(raw) > MAX_RAW_XML}
        items.append(d)
    return items


_BLOCK = re.compile(r"<(item|entry)\b[^>]*>.*?</\1>", re.S | re.I)


def _salvage(text: str) -> list[dict]:
    """Last resort: parse each <item>/<entry> block on its own."""
    items = []
    for m in _BLOCK.finditer(text):
        # Out of its document a block loses its namespace declarations: drop prefixes (dc:creator -> creator).
        block = re.sub(r"<(/?)[A-Za-z][\w.-]*:(?=[A-Za-z])", r"<\1", _repair(m.group(0)))
        block = re.sub(r"\s[A-Za-z][\w.-]*:([A-Za-z][\w.-]*=)", r" \1", block)
        try:
            el = ET.fromstring(block)
        except ET.ParseError:
            continue
        d = _rss_item(el) if m.group(1).lower() == "item" else _atom_entry(el)
        d["raw"] = {"xml": block[:MAX_RAW_XML], "truncated": len(block) > MAX_RAW_XML, "salvaged": True}
        items.append(d)
    return items


def parse_xml(body: bytes | str) -> list[dict]:
    text = _decode(body) if isinstance(body, bytes) else body
    if "<!ENTITY" in text[:20000]:
        raise FeedError("document declares XML entities; refused")
    try:
        root = ET.fromstring(body if isinstance(body, bytes) else text.encode("utf-8"))
    except ET.ParseError:
        try:
            root = ET.fromstring(_repair(text))
        except ET.ParseError as exc:
            items = _salvage(text)
            if not items:
                raise FeedError(f"unparseable XML: {exc}") from exc
            return _finish(items)
    return _finish(_from_tree(root))


# --------------------------------------------------------------------------- JSON

def parse_json_feed(body: bytes | str) -> list[dict]:
    """JSON Feed 1.x (jsonfeed.org)."""
    doc = json.loads(body)
    if not isinstance(doc, dict) or not isinstance(doc.get("items"), list):
        raise FeedError("not a JSON Feed: no items list")
    items = []
    for it in doc["items"]:
        if not isinstance(it, dict):
            continue
        authors = it.get("authors") or ([it["author"]] if isinstance(it.get("author"), dict) else [])
        items.append({
            "guid": str(it.get("id") or it.get("url") or ""), "url": it.get("url") or it.get("external_url"),
            "title": clean_text(it.get("title")),
            "summary": clean_text(it.get("summary") or it.get("content_text") or it.get("content_html"), MAX_SUMMARY),
            "author": clean_text(", ".join(a.get("name", "") for a in authors if isinstance(a, dict)) or None),
            "categories": [str(t) for t in (it.get("tags") or [])][:20],
            "published_raw": it.get("date_published") or it.get("date_modified"),
            "raw": it,
        })
    return _finish(items)


def parse_federal_register(body: bytes | str) -> list[dict]:
    """Federal Register API /documents.json search results."""
    doc = json.loads(body)
    if not isinstance(doc, dict) or "results" not in doc:
        raise FeedError("not a Federal Register search response")
    items = []
    for r in doc.get("results") or []:
        if not isinstance(r, dict):
            continue
        agencies = [a.get("name") for a in (r.get("agencies") or []) if isinstance(a, dict) and a.get("name")]
        items.append({
            "guid": r.get("document_number") or r.get("html_url"), "url": r.get("html_url"),
            "title": clean_text(r.get("title")), "summary": clean_text(r.get("abstract"), MAX_SUMMARY),
            "author": ", ".join(agencies) or None,
            "categories": [c for c in [r.get("type")] if c],
            "published_raw": r.get("publication_date"), "raw": r,
        })
    return _finish(items)


_SAFE_SCHEMES = ("http", "https")


def safe_url(url) -> str | None:
    """The link only if it is an absolute http(s) URL, else None. Feed links are third-party input
    rendered as <a href>: a javascript: / data: / vbscript: link would run script on our origin
    (stored XSS). Control characters and whitespace that browsers strip before parsing the scheme
    (e.g. "java<TAB>script:") are removed before the check."""
    if not isinstance(url, str):
        return None
    u = re.sub(r"[\t\n\r]", "", url.strip("".join(map(chr, range(0x21)))))  # what browsers strip
    if not u or len(u) > 4096 or re.search(r"[\x00-\x1f\x7f]", u):
        return None
    scheme, sep, rest = u.partition(":")
    if not sep or scheme.lower() not in _SAFE_SCHEMES or not rest.startswith("//") or len(rest) < 3:
        return None
    return u


def _finish(items: list[dict]) -> list[dict]:
    out = []
    for d in items:
        d["url"] = safe_url(d.get("url"))
        if not (d.get("title") or d.get("url")):
            continue  # nothing to show or link to
        d["published_at"] = parse_time(d.get("published_raw"))
        out.append(d)
    return out


def parse(body: bytes | str, kind: str) -> list[dict]:
    if kind == "federal_register":
        return parse_federal_register(body)
    if kind == "json_api":
        return parse_json_feed(body)
    head = (body[:200].decode("utf-8", "replace") if isinstance(body, bytes) else body[:200]).lstrip()
    if head.startswith("{"):
        return parse_json_feed(body)
    return parse_xml(body)
