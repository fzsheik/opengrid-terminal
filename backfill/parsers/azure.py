"""Azure: archived captures of the Retail Prices API (prices.azure.com/api/retail/prices).

The Internet Archive holds only the BARE request (no $filter): the first 1,000 rows of Azure's whole retail
catalogue, in the API's own order, not the per-region GPU query the live poller sends (providers/azure.py). The rows
are the same shape, so the archived page goes through the live normalizer unchanged (per region in
providers.azure.REGIONS: Consumption, non-Windows, not Low Priority, GPU_SIZES only, whole-VM price / GPU count).

Difference from live, stated: a capture covers whichever GPU sizes happen to fall in those first 1,000 rows (today
one: eastus2 Standard_NC48ads_A100_v4), never the full GPU catalogue.

Second source, the Linux VM pricing page (azure.microsoft.com/en-us/pricing/details/virtual-machines/linux/,
captured from 2016): every series is a table headed Instance | ... | Pay as you go | 1 year reserved | ... [| Spot],
and each price cell carries the price as data, not text: data-amount='{"regional":{"us-east":27.197,...}}', the
whole VM's hourly price per page region ("us-east" is eastus, "us-east-2" eastus2). Only "Pay as you go" (on-demand)
and "Spot" are read; reserved columns are commitments and never read. A row's display name ("ND96asr A100 v4") is
turned into its ARM size (Standard_ND96asr_v4) and kept only when it is a GPU_SIZES size, so the listing is the live
poller's: same sku, listing id "<size>:<region>[:spot]", raw GPU name and count. Retired sizes (K80, P100, P40, M60
series) and fractional ones are not in GPU_SIZES and are skipped, exactly as live.
"""
from __future__ import annotations

import html as htmllib
import json
import re
from datetime import datetime
from types import SimpleNamespace

from backfill.sources import Source
from models import ComputeListing
import canonical
from providers.azure import _FIELDS, _REQUIRED, COUNTRY, GPU_SIZES, REGIONS, AzureProvider, endpoint


def azure_retail_first_page(body: bytes, at: datetime) -> list[ComputeListing]:
    d = json.loads(body)
    if not isinstance(d, dict) or not isinstance(d.get("Items"), list):
        raise ValueError("azure(archive): response has no Items list")
    if d.get("BillingCurrency", "USD") != "USD":
        raise ValueError(f"azure(archive): billing currency {d.get('BillingCurrency')!r}")
    by_endpoint = {}
    for region in REGIONS:
        rows = []
        for item in d["Items"]:
            if item.get("armRegionName") != region or item.get("type") != "Consumption":
                continue
            missing = [k for k in _REQUIRED if k not in item]
            if missing:
                raise ValueError(f"azure(archive): price row lost {missing}")
            rows.append({k: item.get(k) for k in _FIELDS})
        # Keyed by the exact consumption endpoint: the live normalizer reads only those labels.
        by_endpoint[endpoint(region)] = [SimpleNamespace(endpoint=endpoint(region), payload={"region": region, "items": rows},
                                                         fetched_at=at, ok=True)]
    return AzureProvider.normalize(by_endpoint)


# The page's region keys for the live poller's regions.
PAGE_REGION = {"us-east": "eastus", "us-east-2": "eastus2"}
# Display names whose ARM size is not "Standard_" + the name with "_" for spaces.
# (2018 designs name the NCv3 sizes without their "s": "NC6 v3", "NC24r v3".)
DISPLAY_TO_ARM = {"ND96asr A100 v4": "Standard_ND96asr_v4",
                  "NC6 v3": "Standard_NC6s_v3", "NC12 v3": "Standard_NC12s_v3", "NC24 v3": "Standard_NC24s_v3",
                  "NC24r v3": "Standard_NC24rs_v3"}
# The on-demand column: "Pay as you go" (2017 on), "Price" (2016-17, one price column).
ON_DEMAND = ("Pay as you go", "Price")

_TABLE = re.compile(r"<table[^>]*>(?P<t>.*?)</table>", re.S)
_TH = re.compile(r"<th[^>]*>(?P<c>.*?)</th>", re.S)
_TBODY = re.compile(r"<tbody[^>]*>(?P<b>.*?)</tbody>", re.S)
_TR = re.compile(r"<tr[^>]*>(?P<r>.*?)</tr>", re.S)
_TD = re.compile(r"<td[^>]*>(?P<c>.*?)</td>", re.S)
_AMOUNT = re.compile(r"""data-amount=(?P<q>['"])(?P<j>.*?)(?P=q)""", re.S)
_GPU_CELL = re.compile(r"^(?P<n>\d+)\s*[xX]\b")


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def _arm(display: str) -> str:
    return DISPLAY_TO_ARM.get(display, "Standard_" + display.replace(" ", "_"))


def _amounts(cell: str, where: str) -> dict[str, float] | None:
    """The regional prices a cell carries as data-amount, or None when the cell has none (N/A, blank)."""
    m = _AMOUNT.search(cell)
    if m is None:
        return None
    try:
        d = json.loads(htmllib.unescape(m["j"]))
    except ValueError:
        raise ValueError(f"azure(page): {where} data-amount is not JSON: {m['j'][:80]!r}")
    if not isinstance(d, dict) or not isinstance(d.get("regional", {}), dict):
        raise ValueError(f"azure(page): {where} data-amount has no regional map: {m['j'][:80]!r}")
    return d.get("regional") or {}


def azure_vm_page(page: str, at: datetime) -> list[ComputeListing]:
    """The Linux VM pricing page: data-amount JSON in the Pay as you go / Spot cells of each series table."""
    from decimal import Decimal

    out, priced_tables = [], 0
    for tm in _TABLE.finditer(page):
        head = [_text(c).rstrip(" *") for c in _TH.findall(tm["t"])]
        od = next((c for c in ON_DEMAND if c in head), None)
        if "Instance" not in head or od is None:
            continue
        priced_tables += 1
        cols = {name: head.index(name) for name in ("Instance", od, "Spot", "GPU") if name in head}
        body = _TBODY.search(tm["t"])
        for rm in _TR.finditer(body["b"] if body else tm["t"]):
            cells = _TD.findall(rm["r"])
            if not cells:
                continue
            if len(cells) != len(head):
                if len(cells) > cols["Instance"] and _arm(_text(cells[cols["Instance"]])) in GPU_SIZES:
                    raise ValueError(f"azure(page): GPU row has {len(cells)} cells under {len(head)} headers")
                continue
            size = _arm(_text(cells[cols["Instance"]]))
            if size not in GPU_SIZES:
                continue
            count, raw = GPU_SIZES[size]
            if "GPU" in cols:
                g = _GPU_CELL.match(_text(cells[cols["GPU"]]))
                if g and int(g["n"]) != count:
                    raise ValueError(f"azure(page): {size} GPU cell {_text(cells[cols['GPU']])!r} disagrees with {count}")
            for column, market in ((od, "on_demand"), ("Spot", "spot")):
                if column not in cols:
                    continue
                regional = _amounts(cells[cols[column]], f"{size} {column}")
                for page_region, region in PAGE_REGION.items():
                    v = (regional or {}).get(page_region)
                    if v is None:
                        continue
                    if not isinstance(v, (int, float)) or isinstance(v, bool):
                        raise ValueError(f"azure(page): {size} {column} {page_region} amount {v!r}")
                    price = Decimal(str(v))
                    if price <= 0:
                        continue
                    out.append(ComputeListing(
                        provider="azure", sku=size, listing_id=f"{size}:{region}" + (":spot" if market == "spot" else ""),
                        raw_gpu_name=raw, canonical_gpu_name=canonical.canonical_gpu_name(raw), gpu_count=count,
                        region=region, country=COUNTRY.get(region), price_per_gpu_hour=price / count,
                        price_per_instance_hour=price, currency="USD", market_type=market, provider_tier="hyperscaler",
                        interruptible=market == "spot", available=None, capacity=None, capacity_unit=None, vcpu=None,
                        ram_gb=None, storage_gb=None, observed_at=at))
    if not priced_tables:
        if page.count("data-amount") <= 1:
            raise ValueError("azure(page): no price tables in the HTML (the 2025+ page loads prices by script)")
        raise ValueError("azure(page): no 'Instance ... Pay as you go' table")
    if not out:
        # 2016-17: only K80 / M60 / P40 series, none of which is a GPU_SIZES size.
        raise ValueError("azure(page): no GPU_SIZES size priced in eastus / eastus2")
    return out


SOURCES = [Source("azure", "prices.azure.com/api/retail/prices",
                  (("backfill.parsers.azure.azure_retail_first_page (live normalizer)", azure_retail_first_page),)),
           Source("azure", "azure.microsoft.com/en-us/pricing/details/virtual-machines/linux/",
                  (("backfill.parsers.azure.azure_vm_page",
                    lambda body, at: azure_vm_page(body.decode("utf-8", errors="replace"), at)),))]
