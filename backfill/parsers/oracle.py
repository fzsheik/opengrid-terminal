"""Oracle Cloud (OCI): archived captures of the public price list API and of the one compute pricing page design that
printed its prices in the HTML.

    oracle_price_list      apexapps.oracle.com/pls/apex/cetools/api/v1/products/ (any query: ?currencyCode=USD, the
                           bare all-currency request, ?partNumber=...): the live poller's own source, read through
                           providers/oracle.py extract + OracleProvider.normalize unchanged (SHAPES, "GPU Per Hour",
                           one flat USD pay-as-you-go price per part). A capture without a USD price (?currencyCode=BRL)
                           raises. Captured only from May 2026.
    oracle_static_page     cloud.oracle.com/compute/pricing, Apr 2018: the "Compute - GPU Instances" table (Instance
                           Type | Shape | Pay as You Go (GPU Per Hour) | Monthly Flex | Service Includes) where a price
                           is printed as "USD $2.25". Cells still holding the script placeholder
                           "[OPC:PRICE]B88518[/OPC:PRICE]", or empty (filled in the browser from the price list),
                           carry no price and are skipped; shapes marked "(Coming Soon)" were not on sale and are skipped. The Monthly
                           Flex column is a commitment and is never read. A shape must be in SHAPES and print the same
                           GPU count ("GPU: 8x V100").

Why Oracle's archive is thin: from late 2018 every Oracle price page (cloud.oracle.com/compute/pricing,
oracle.com/cloud/compute/pricing, oracle.com/cloud/price-list) fills its prices in the browser from the price list
API, so the archived HTML has part numbers and no prices, and the API itself was not captured before 2026.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

from backfill.parsers import listing, text
from backfill.sources import Source
from models import ComputeListing
from providers.oracle import ENDPOINT, SHAPES, OracleProvider, extract

_TABLE = re.compile(r"<table\b.*?</table>", re.S)
_TR = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.S)
_TD = re.compile(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", re.S)
_USD = re.compile(r"^USD \$(\d+(?:\.\d+)?)$")
_PLACEHOLDER = re.compile(r"^\[OPC:PRICE\][A-Z0-9:_]+\[/OPC:PRICE\]$")
_GPUS = re.compile(r"GPU: (\d+)x ")
_HEAD = ["Instance Type", "Shape", "Pay as You Go (GPU Per Hour)"]


def oracle_price_list(body: bytes, at: datetime) -> list[ComputeListing]:
    payload = extract(json.loads(body))
    return OracleProvider.normalize({ENDPOINT: [SimpleNamespace(endpoint=ENDPOINT, payload=payload, fetched_at=at, ok=True)]})


def oracle_static_page(page: str, at: datetime) -> list[ComputeListing]:
    out: dict[str, ComputeListing] = {}
    placeholders = tables = 0
    for table in _TABLE.findall(page):
        rows = [[text(c) for c in _TD.findall(tr)] for tr in _TR.findall(table)]
        if not rows or rows[0][:3] != _HEAD:
            continue
        tables += 1
        for cells in rows[1:]:
            if len(cells) != len(rows[0]):
                raise ValueError(f"oracle(page): GPU row has {len(cells)} cells, header has {len(rows[0])}: {cells[:2]}")
            shape, price = cells[1], cells[2]
            if "Coming Soon" in shape:
                continue  # announced, not on sale
            if not price or _PLACEHOLDER.match(price):
                placeholders += 1
                continue  # filled in the browser from the price list: no price in the HTML
            m = _USD.match(price)
            if m is None:
                raise ValueError(f"oracle(page): {shape} pay-as-you-go cell is not 'USD $x': {price!r}")
            spec = SHAPES.get(shape)
            if spec is None:
                raise ValueError(f"oracle(page): shape {shape!r} is not in providers.oracle.SHAPES")
            _, count, raw = spec
            printed = _GPUS.search(cells[-1])
            if printed is None or int(printed.group(1)) != count:
                raise ValueError(f"oracle(page): {shape} prints {cells[-1][:40]!r}, SHAPES says {count} GPUs")
            per_gpu = Decimal(m.group(1))
            if per_gpu <= 0:
                continue
            seen = out.get(shape)
            if seen is not None and seen.price_per_gpu_hour != per_gpu:
                raise ValueError(f"oracle(page): {shape} printed at ${seen.price_per_gpu_hour} and ${per_gpu}")
            out[shape] = listing("oracle", sku=shape, listing_id=shape, raw=raw, count=count,
                                 instance_price=per_gpu * count, at=at,
                                 tier="bare-metal" if shape.startswith("BM.") else "vm")
    if not tables:
        raise ValueError("oracle(page): no 'Instance Type | Shape | Pay as You Go (GPU Per Hour)' table")
    if not out:
        raise ValueError(f"oracle(page): no printed GPU price ({placeholders} cells are script placeholders)")
    return list(out.values())


SOURCES = [
    Source("oracle", "apexapps.oracle.com/pls/apex/cetools/api/v1/products/*",
           (("backfill.parsers.oracle.oracle_price_list (live parser)", oracle_price_list),)),
    Source("oracle", "cloud.oracle.com/compute/pricing",
           (("backfill.parsers.oracle.oracle_static_page",
             lambda body, at: oracle_static_page(body.decode("utf-8", errors="replace"), at)),)),
]
