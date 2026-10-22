"""Together AI: archived designs of www.together.ai/pricing the live parser (providers/together.py) rejects.

Same semantics as the live parser: prices are per GPU per hour, a listing is one GPU at the printed rate (the page
states no node size or region), on-demand / pay-as-you-go is on_demand, preemptible is spot, reserved terms are
commitments and are never read; listing ids are the slug of the printed hardware name (":preemptible" for spot).

Designs, newest first (each parser is tried only after the live one fails):
  per_gpu_table      Jul - Aug 2026: the live "On-demand hourly rates and reserved capacity" table ("All prices are
                     per GPU per hour.") before its Preemptible column existed: Hardware | ON-Demand | Reserved.
  on_demand_hourly   Mar - Jun 2026 (and the "Hourly" twin table still on the page until the live design): the
                     "GPU Clusters" section's "On-demand / Pay as you go GPU capacity on an hourly basis." table,
                     Hardware | Hourly (or, on 2 Mar 2026, Hardware | 1 Week - 3 Months* | 1 - 6 Days* | Hourly*).
                     This table does not restate its unit; it is read as per GPU because, from 1 Jul 2026, the page
                     prints it next to the per-GPU table above with identical prices for every GPU, and its rates
                     continue the per-GPU "Instant Clusters" rates before it (H100 $2.99 -> $3.49).
  instant_clusters   Oct 2025 - Feb 2026: "Instant Clusters ... Price per hour per GPU", Hardware | 1 Week - 3
                     Months | 1 - 6 Days | Hourly. Only Hourly (pay as you go) is read.
"""
from __future__ import annotations

import re
from datetime import datetime

from backfill.parsers import listing, slug, text
from backfill.sources import live_page
from models import ComputeListing
from providers.together import _price

_TABLE = re.compile(r"<table[^>]*>(?P<t>.*?)</table>", re.S)
_TR = re.compile(r"<tr[^>]*>(?P<r>.*?)</tr>", re.S)
_TD = re.compile(r"<td[^>]*>(?P<c>.*?)</td>", re.S)
_TH = re.compile(r"<th(?P<a>[^>]*)>(?P<c>.*?)</th>", re.S)
_COLSPAN = re.compile(r'colspan="(\d+)"')


def _tables_after(page: str, marker: str, limit: int = 4000):
    """Tables starting within `limit` characters after each occurrence of `marker` (plain text of the gap)."""
    for m in re.finditer(re.escape(marker), page):
        t = _TABLE.search(page, m.end())
        if t is not None and t.start() - m.end() <= limit:
            yield t["t"]


def _header(table: str) -> list[str]:
    """Top-level header labels, one per data column (a colspan=N header is repeated N times)."""
    first = _TR.search(table)
    scope = first["r"] if first and "<th" in first["r"] else table
    out = []
    for th in _TH.finditer(scope):
        span = _COLSPAN.search(th["a"])
        out += [text(th["c"]).rstrip("*").strip()] * (int(span.group(1)) if span else 1)
    return out


def _rows(table: str) -> list[list[str]]:
    return [cells for tr in _TR.finditer(table) if (cells := [text(c) for c in _TD.findall(tr["r"])])]


def _listings(rows, columns: dict[str, int], at: datetime) -> list[ComputeListing]:
    """columns: market -> td index. A row with a price cell in an unexpected shape raises (via _price)."""
    out = []
    for cells in rows:
        name = cells[0]
        for market, idx in columns.items():
            if idx >= len(cells):
                continue  # a "—" / "Contact us" row that collapses its reserved cells
            price = _price(cells[idx])
            if price is None or price <= 0:
                continue
            out.append(listing("together", sku=name, listing_id=slug(name) + (":preemptible" if market == "spot" else ""),
                               raw=name, count=1, instance_price=price, at=at, market=market, tier="gpu-cluster"))
    return out


def per_gpu_table(page: str, at: datetime) -> list[ComputeListing]:
    for t in _tables_after(page, "All prices are per GPU per hour.", 3000):
        head = _header(t)
        if head[:1] != ["Hardware"] or "ON-Demand" not in head:
            raise ValueError(f"together(archive): per-GPU table header not recognised: {head!r}")
        cols = {"on_demand": head.index("ON-Demand")}
        if "Preemptible Compute" in head:
            cols["spot"] = head.index("Preemptible Compute")
        out = _listings(_rows(t), cols, at)
        if out:
            return out
    raise ValueError("together(archive): no per-GPU hourly table")


def on_demand_hourly(page: str, at: datetime) -> list[ComputeListing]:
    sec = page.find(">GPU Clusters</h2>")
    if sec == -1:
        raise ValueError("together(archive): 'GPU Clusters' heading not found")
    for t in _tables_after(page[sec:], "Pay as you go GPU capacity on an hourly basis.", 3000):
        head = _header(t)
        if head[:1] != ["Hardware"] or "Hourly" not in head:
            raise ValueError(f"together(archive): on-demand table header not recognised: {head!r}")
        out = _listings(_rows(t), {"on_demand": head.index("Hourly")}, at)
        if out:
            return out
    raise ValueError("together(archive): no 'GPU Clusters / On-demand' hourly table")


def instant_clusters(page: str, at: datetime) -> list[ComputeListing]:
    for m in re.finditer(r"Instant Clusters", page):
        t = _TABLE.search(page, m.end())
        if t is None or t.start() - m.end() > 8000:
            continue
        if "Price per hour per GPU" not in text(page[m.end():t.start()]):
            continue
        head = _header(t["t"])
        if head[:1] != ["Hardware"] or "Hourly" not in head:
            raise ValueError(f"together(archive): Instant Clusters header not recognised: {head!r}")
        out = _listings(_rows(t["t"]), {"on_demand": head.index("Hourly")}, at)
        if out:
            return out
    raise ValueError("together(archive): no 'Instant Clusters' per-GPU table")


SOURCES = [live_page("together", "together", "www.together.ai/pricing", per_gpu_table, on_demand_hourly,
                     instant_clusters)]
