"""Massed Compute: archived designs of vm.massedcompute.com/pricing the live parser (providers/massedcompute.py)
rejects.

    massedcompute_h3_tables   Mar - Apr 2026: each model is an <h3> heading followed by a table headed
                              GPU Quantity | VRAM | vCPUs | RAM | Storage | Price ("x 1", ..., "$1.52/hr"). Rows are
                              rebuilt into the live payload (model, qty, vcpu, ram, storage, price) and go through
                              MassedComputeProvider.normalize, so per-instance pricing, ids and "Request" rows (no
                              price, skipped) are the live ones.
"""
from __future__ import annotations

import re
from datetime import datetime
from types import SimpleNamespace

from backfill.parsers import text
from backfill.sources import live_page
from models import ComputeListing

_BLOCK = re.compile(r"<h3\b[^>]*>(?P<model>[^<]+)</h3>|(?P<table><table\b.*?</table>)", re.S)
_HEAD = ["GPU Quantity", "VRAM", "vCPUs", "RAM", "Storage", "Price"]


def massedcompute_h3_tables(page: str, at: datetime) -> list[ComputeListing]:
    from providers.massedcompute import ENDPOINT, MassedComputeProvider

    model, shapes = None, []
    for m in _BLOCK.finditer(page):
        if m["model"]:
            model = text(m["model"])
            continue
        header = [text(c) for c in re.findall(r"<th\b.*?</th>", m["table"], re.S)]
        if header != _HEAD:
            continue
        if model is None:
            raise ValueError("massedcompute(archive): price table with no <h3> model heading before it")
        for tr in re.findall(r"<tr\b.*?</tr>", m["table"], re.S)[1:]:
            cells = [text(c) for c in re.findall(r"<td\b.*?</td>", tr, re.S)]
            if len(cells) < 6:
                continue  # "x 8 | Request": no price
            shapes.append({"model": model, "qty": cells[0], "vcpu": cells[2], "ram": cells[3], "storage": cells[4],
                           "price": cells[5]})
    if not shapes:
        raise ValueError("massedcompute(archive): no 'GPU Quantity ... Price' tables under <h3> headings")
    payload = {"shapes": shapes}
    return MassedComputeProvider.normalize({ENDPOINT: [SimpleNamespace(endpoint=ENDPOINT, payload=payload, fetched_at=at)]})


SOURCES = [live_page("massedcompute", "massedcompute", "vm.massedcompute.com/pricing", massedcompute_h3_tables)]
