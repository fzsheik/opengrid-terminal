"""Civo: archived captures of www.civo.com/pricing (GPU tables from early 2024 on).

Live, OpenGrid scrapes this page (providers/civo.py): its "nvidia-gpus" section, one table per GPU model headed
"<model> GPU pricing", the on-demand hourly cell of each row. Captures from 2025 on are read by that live parser
unchanged. Before Civo split on-demand from commitment prices (2024 - early 2025) each table had ONE price column:

    Size | RAM | CPU | Storage | Data Transfer | Price          <td data-title="Price"> with an hourly and a monthly div

"$1.785714 per hour" / "$1,200 per month" for "1 x NVIDIA L40S - 48GB". There was no commitment offer on the page, so
that price is the list (on-demand) price; its hourly figure is the one Civo printed (the monthly price / 672 h, which
is how Civo derived it, not OpenGrid). civo_single_price reads that hourly div and hands the rows to the live
normalizer (CivoProvider.normalize), so listing ids ("nvidia-l40s-48gb:1x"), GPU names and the per-GPU rule are the
live ones. A row without an hourly "Price" div raises (design not known), and so does a row whose hourly and monthly
figures are in different currencies: Civo's first GPU table (Jun - Sep 2023, "Nvidia GPU pricing", A100 80GB) prints
"$2.380952 per hour" beside "£1,600 per month", so its currency is unknown and it is not read. Earlier captures have no
GPU section.
"""
from __future__ import annotations

import html as _html
import re
from datetime import datetime

from backfill.sources import _snapshot, live_page
from models import ComputeListing
from providers import civo

_PRICE = re.compile(
    r'<td data-title="Price"[^>]*>\s*<div[^>]*data-option="hourly"[^>]*>(?P<v>[^<]*)<span>per hour</span>')
_MONTHLY = re.compile(r'<div[^>]*data-option="monthly"[^>]*>(?P<v>[^<]*)<span>per month</span>')
_CELL = re.compile(r'<td data-title="(?P<label>RAM|CPU|Storage)">(?P<v>[^<]*)')


def civo_single_price(page: str, at: datetime) -> list[ComputeListing]:
    sec = civo._SECTION.search(page)
    if sec is None:
        raise ValueError("civo(archive): no 'nvidia-gpus' section")
    body = sec["body"]
    heads = list(civo._HEADING.finditer(body))
    if not heads:
        raise ValueError("civo(archive): no '<model> GPU pricing' heading")
    rows = []
    for i, h in enumerate(heads):
        chunk = body[h.end(): heads[i + 1].start() if i + 1 < len(heads) else len(body)]
        for r in civo._ROW.finditer(chunk):
            row = r["row"]
            if "<th" in row:
                continue
            if 'data-title="On-demand price"' in row:
                raise ValueError("civo(archive): an on-demand column: the live design, not the single-price one")
            detail, price = civo._DETAIL.search(row), _PRICE.search(row)
            if detail is None or price is None:
                raise ValueError(f"civo(archive): a {h['model']} row has no size or hourly 'Price' cell")
            monthly = _MONTHLY.search(row)
            hourly = _html.unescape(price["v"]).strip()
            if monthly is not None and _html.unescape(monthly["v"]).strip()[:1] != hourly[:1]:
                raise ValueError(f"civo(archive): {detail['v'].strip()}: hourly {hourly!r} and monthly "
                                 f"{monthly['v'].strip()!r} in different currencies (not read)")
            cells = {c["label"]: _html.unescape(c["v"]).strip() for c in _CELL.finditer(row)}
            rows.append({"table": _html.unescape(h["model"]).strip(), "size": _html.unescape(detail["v"]).strip(),
                         "ram": cells.get("RAM"), "vcpu": cells.get("CPU"), "storage": cells.get("Storage"),
                         "on_demand_hourly": hourly})
    if not rows:
        raise ValueError("civo(archive): GPU tables have no row")
    return civo.CivoProvider.normalize({civo.ENDPOINT: [_snapshot(civo.ENDPOINT, {"rows": rows}, at)]})


SOURCES = [live_page("civo", "civo", "www.civo.com/pricing", civo_single_price)]
