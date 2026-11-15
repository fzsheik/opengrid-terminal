"""Hyperbolic: archived "starting from" GPU prices on its marketing pages (hyperbolic.xyz/pricing, then
www.hyperbolic.ai/marketplace).

Live, OpenGrid reads Hyperbolic's anonymous rental-options API (providers/hyperbolic.py: one on-demand listing per
rental option, whole-option price). That API is not in the Internet Archive. What is archived are the marketing pages'
"GPU Type | (Prices) Start(ing) From (per GPU hour)" tables: the lowest on-demand rate per GPU-hour across
Hyperbolic's suppliers that week ("Pricing is refreshed weekly based on the best available rates from suppliers").
That is a different number from any one rental option, so each row is its own listing: one GPU at the printed
per-GPU-hour rate, listing id "<gpu>:from", provider_tier "starting-from", market on_demand. The GPU name drops the
page's "Nvidia " prefix, giving the live API's form ("H100 SXM", "H200", "RTX 4090").

    xyz_pricing_table      hyperbolic.xyz/pricing, Jan - Jun 2025 (Next.js): rows "table_row" with a gpuCell name and a
                           priceCell "$2.4 / hr". The page fills most price cells in the browser, so many captures
                           print no price at all (reported, nothing stored). Only the "Rent GPUs" table under the
                           per-GPU-hour header is read: the "Supply" table below it ("Max Rental Payment", what
                           suppliers may charge at most, and "Idle Reward" points) is never read.
    marketplace_list       www.hyperbolic.ai/marketplace, from Jun 2025: "GPU Type | Starting From (per GPU hour)"
                           list rows "Nvidia H100 SXM" "$1.50 / HR".
    marketplace_table      www.hyperbolic.ai/marketplace, later in 2026: the same two columns as an HTML table.

Not read: app.hyperbolic.xyz/compute (2024 - 2025, a browser app shell: the archived HTML holds no price) and
api.hyperbolic.xyz/v1/marketplace (7 JSON captures, Jan - Jul 2025: node prices "amount" with no unit or basis
stated, per GPU or per node and dollars or cents, so not read rather than guessed).
"""
from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal

from backfill.parsers import listing, slug, text
from backfill.sources import own_page
from models import ComputeListing

_PRICE = re.compile(r"^\$\s*(?P<v>\d+(?:\.\d+)?)\s*/\s*(?:hr|hour)$", re.I)


def _row(name: str, price: str, at: datetime) -> ComputeListing | None:
    name = re.sub(r"^nvidia\s+", "", name.strip(), flags=re.I)
    if not price:
        return None  # filled in the browser: nothing printed
    m = _PRICE.match(price)
    if m is None:
        if re.search(r"\d", price):
            raise ValueError(f"hyperbolic(archive): {name} price {price!r} is not '$x / hr'")
        return None  # "Coming Soon"
    if not name:
        raise ValueError(f"hyperbolic(archive): a priced row ({price!r}) has no GPU name")
    return listing("hyperbolic", sku=f"{name}:from", listing_id=f"{name}:from", raw=name, count=1,
                   instance_price=Decimal(m["v"]), at=at, tier="starting-from")


_XYZ_HEAD = re.compile(r">\s*Prices Start From \(per GPU hour\)\s*<")
_XYZ_ROW = re.compile(r'class="table_row__[^"]*"[^>]*>(?P<r>.*?)(?=class="table_row__|class="table_rows__|</section>|$)',
                      re.S)
_XYZ_NAME = re.compile(r'class="table_gpuCell__[^"]*">.*?<span>(?P<n>[^<]*)</span>', re.S)
_XYZ_PRICE = re.compile(r'class="table_priceCell__[^"]*">(?P<p>[^<]*)<')


def xyz_pricing_table(page: str, at: datetime) -> list[ComputeListing]:
    head = _XYZ_HEAD.search(page)
    if head is None:
        raise ValueError("hyperbolic(archive): no 'Prices Start From (per GPU hour)' table")
    rows_at = page.find('class="table_rows__', head.end())
    stop = page.find("Max Rental Payment", head.end())
    if rows_at == -1 or (stop != -1 and stop < rows_at):
        raise ValueError("hyperbolic(archive): the per-GPU-hour table has no rows")
    # the table ends where the next table (the Supply one) begins
    nxt = page.find('class="table_rows__', rows_at + 20)
    body = page[rows_at: nxt if nxt != -1 else (stop if stop != -1 else len(page))]
    out, rows = [], 0
    for r in _XYZ_ROW.finditer(body):
        n, p = _XYZ_NAME.search(r["r"]), _XYZ_PRICE.search(r["r"])
        if n is None or p is None:
            raise ValueError(f"hyperbolic(archive): a row lacks its GPU or price cell: {text(r['r'])[:100]!r}")
        rows += 1
        o = _row(text(n["n"]), text(p["p"]), at)
        if o is not None:
            out.append(o)
    if not rows:
        raise ValueError("hyperbolic(archive): the per-GPU-hour table has no rows")
    if not out:
        raise ValueError("hyperbolic(archive): no price printed in the per-GPU-hour table (filled in the browser)")
    return out


_LIST = re.compile(r'<div class="d-list-wrapper">(?P<b>.*?)(?=<div class="row actions|</section>|$)', re.S)
_LIST_ROW = re.compile(r'<div class="row (?P<kind>title|item)\b[^"]*">(?P<r>.*?)(?=<div class="row |$)', re.S)
_CELL = re.compile(r"<(?:p|label)\b[^>]*>(?P<v>.*?)</(?:p|label)>", re.S)


def marketplace_list(page: str, at: datetime) -> list[ComputeListing]:
    out, tables = [], 0
    for lm in _LIST.finditer(page):
        rows = [(r["kind"], [text(c["v"]) for c in _CELL.finditer(r["r"])]) for r in _LIST_ROW.finditer(lm["b"])]
        if not rows or rows[0] != ("title", ["GPU Type", "Starting From (per GPU hour)"]):
            continue
        tables += 1
        for kind, cells in rows[1:]:
            if kind != "item" or len(cells) != 2:
                raise ValueError(f"hyperbolic(archive): list row {cells!r} is not 'GPU | price'")
            o = _row(cells[0], cells[1], at)
            if o is not None:
                out.append(o)
    if not tables:
        raise ValueError("hyperbolic(archive): no 'GPU Type | Starting From (per GPU hour)' list")
    if not out:
        raise ValueError("hyperbolic(archive): the 'Starting From' list has no price")
    return out


_TABLE = re.compile(r"<table[^>]*>(?P<t>.*?)</table>", re.S)
_TR = re.compile(r"<tr[^>]*>(?P<r>.*?)</tr>", re.S)
_THD = re.compile(r"<t[hd][^>]*>(?P<v>.*?)</t[hd]>", re.S)


def marketplace_table(page: str, at: datetime) -> list[ComputeListing]:
    out, tables = [], 0
    for tm in _TABLE.finditer(page):
        rows = [[text(c["v"]) for c in _THD.finditer(r["r"])] for r in _TR.finditer(tm["t"])]
        if not rows or rows[0] != ["GPU Type", "Starting From (per GPU hour)"]:
            continue
        tables += 1
        for cells in rows[1:]:
            if len(cells) != 2:
                raise ValueError(f"hyperbolic(archive): table row {cells!r} is not 'GPU | price'")
            o = _row(cells[0], cells[1], at)
            if o is not None:
                out.append(o)
    if not tables:
        raise ValueError("hyperbolic(archive): no 'GPU Type | Starting From (per GPU hour)' table")
    if not out:
        raise ValueError("hyperbolic(archive): the 'Starting From' table has no price")
    return out


SOURCES = [own_page("hyperbolic", "hyperbolic.xyz/pricing", xyz_pricing_table),
           own_page("hyperbolic", "www.hyperbolic.ai/marketplace", marketplace_list, marketplace_table)]
