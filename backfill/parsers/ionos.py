"""IONOS Cloud: archived captures of IONOS CLOUD Inc.'s USD price list on docs.ionos.com.

Live, OpenGrid reads the list's Markdown twin (providers/ionos.py: ".../price-list/ionos-cloud-inc.md"). The archive has
one capture of that (Jan 2026) and many of the rendered page (".../price-list/ionos-cloud-inc", Sep 2023 on). Both
parsers rebuild the live extract()'s rows (keyed by the "Cloud GPU VM" table's own nine column names, which must be
exactly the live ones) and hand them to IonosProvider.normalize, so listing ids (the template, "H200-S"), GPU name
("NVIDIA H200 PCIe"), region, prices (USD, whole VM) and skips are the live ones. Both require "Inc." in the page's
title (the USD contracting entity) and a "$" price; the EUR / GBP lists are never read. The "Cloud Savings Plans"
table (1- and 3-year commitment rates) is a separate table and is never read.

    markdown_bold     the .md twin with a bold heading ("### **Cloud GPU VM**", Jan 2026), which the live section
                      pattern does not match; the table is read up to its last row.
    gitbook_table     the rendered GitBook page (role="columnheader" / role="cell" grid): the first table after the
                      "Cloud GPU VM" heading.

Captures before Cloud GPU VMs were listed (2023 - 2025) have no such table and are reported, storing nothing.
"""
from __future__ import annotations

import re
from datetime import datetime
from types import SimpleNamespace

from backfill.parsers import text
from backfill.sources import live_page, own_page
from models import ComputeListing


def _normalize(rows: list[dict], at: datetime) -> list[ComputeListing]:
    from providers.ionos import ENDPOINT, IonosProvider

    return IonosProvider.normalize({ENDPOINT: [SimpleNamespace(endpoint=ENDPOINT, payload={"rows": rows},
                                                                fetched_at=at, ok=True)]})


def _columns():
    from providers.ionos import _COLUMNS

    return _COLUMNS


_MD_HEAD = re.compile(r"^###\s+\**Cloud GPU VM\**\s*$", re.M)


def markdown_bold(md: str, at: datetime) -> list[ComputeListing]:
    m = _MD_HEAD.search(md)
    if m is None:
        raise ValueError("ionos(archive): no '### Cloud GPU VM' heading in the Markdown")
    if "Inc." not in md.split("\n## ", 1)[0]:
        raise ValueError("ionos(archive): not the IONOS CLOUD Inc. (USD) price list")
    lines = []
    for line in md[m.end():].lstrip("\n").splitlines():
        if not line.strip().startswith("|"):
            break  # the table ends at its first non-table line
        lines.append([c.strip() for c in line.strip().strip("|").split("|")])
    if len(lines) < 3 or lines[0] != _columns():
        raise ValueError(f"ionos(archive): Cloud GPU VM columns {lines[0] if lines else None}")
    return _normalize([dict(zip(lines[0], r)) for r in lines[2:]], at)


_H = re.compile(r"<h[23][^>]*>(?P<h>.*?)</h[23]>", re.S)
_NEXT_H = re.compile(r"<h[234]\b")
_SVG = re.compile(r"<svg\b.*?</svg>", re.S)   # heading anchors carry an icon titled "hashtag"


def _value(chunk: str) -> str:
    """One GitBook grid cell, from just after its role attribute: its first <p> when it has one, else the text up to
    its first closing </div> (GitBook nests divs, so a cell is never read up to the next one)."""
    chunk = chunk[chunk.find(">") + 1:]
    p = re.search(r"<p\b[^>]*>(?P<v>.*?)</p>", chunk, re.S)
    end = chunk.find("</div>")
    if p is not None and (end == -1 or p.start() < end or not text(chunk[:end])):
        return text(p["v"])
    return text(chunk[: end if end != -1 else len(chunk)])


def gitbook_table(page: str, at: datetime) -> list[ComputeListing]:
    title = text(re.search(r"<title>(.*?)</title>", page, re.S).group(1)) if "<title>" in page else ""
    if "Inc." not in title:
        raise ValueError(f"ionos(archive): page title {title!r} is not the IONOS CLOUD Inc. (USD) list")
    head = next((h for h in _H.finditer(page) if text(_SVG.sub(" ", h["h"])) == "Cloud GPU VM"), None)
    if head is None:
        raise ValueError("ionos(archive): no 'Cloud GPU VM' heading (Cloud GPU VMs not on this list yet)")
    nxt = _NEXT_H.search(page, head.end())
    sec = page[head.end(): nxt.start() if nxt else len(page)]
    cols, rows = None, []
    for r in re.split(r'role="row"', sec)[1:]:
        if 'role="columnheader"' in r:
            if cols is not None:
                break  # the next table's header (Cloud Savings Plans): not read
            cols = [_value(c) for c in re.split(r'role="columnheader"', r)[1:]]
            if cols != _columns():
                raise ValueError(f"ionos(archive): Cloud GPU VM columns changed: {cols}")
            continue
        if cols is None:
            continue
        cells = [_value(c) for c in re.split(r'role="cell"', r)[1:]]
        if not cells:
            continue
        if len(cells) != len(cols):
            raise ValueError(f"ionos(archive): a Cloud GPU VM row has {len(cells)} cells: {cells}")
        rows.append(dict(zip(cols, cells)))
    if cols is None:
        raise ValueError("ionos(archive): the Cloud GPU VM section has no table")
    if not rows:
        raise ValueError("ionos(archive): the Cloud GPU VM table has no rows")
    return _normalize(rows, at)


SOURCES = [live_page("ionos", "ionos", "docs.ionos.com/support/general-information/price-list/ionos-cloud-inc.md",
                     markdown_bold),
           own_page("ionos", "docs.ionos.com/support/general-information/price-list/ionos-cloud-inc", gitbook_table)]
