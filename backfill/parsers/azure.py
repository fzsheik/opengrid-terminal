"""Azure: archived captures of the Retail Prices API (prices.azure.com/api/retail/prices).

The Internet Archive holds only the BARE request (no $filter): the first 1,000 rows of Azure's whole retail
catalogue, in the API's own order, not the per-region GPU query the live poller sends (providers/azure.py). The rows
are the same shape, so the archived page goes through the live normalizer unchanged (per region in
providers.azure.REGIONS: Consumption, non-Windows, not Low Priority, GPU_SIZES only, whole-VM price / GPU count).

Difference from live, stated: a capture covers whichever GPU sizes happen to fall in those first 1,000 rows (today
one: eastus2 Standard_NC48ads_A100_v4), never the full GPU catalogue.
"""
from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

from backfill.sources import Source
from models import ComputeListing
from providers.azure import _FIELDS, _REQUIRED, PATH, REGIONS, AzureProvider, endpoint


def azure_retail_first_page(body: bytes, at: datetime) -> list[ComputeListing]:
    d = json.loads(body)
    if not isinstance(d, dict) or not isinstance(d.get("Items"), list):
        raise ValueError("azure(archive): response has no Items list")
    if d.get("BillingCurrency", "USD") != "USD":
        raise ValueError(f"azure(archive): billing currency {d.get('BillingCurrency')!r}")
    snaps = []
    for region in REGIONS:
        rows = []
        for item in d["Items"]:
            if item.get("armRegionName") != region or item.get("type") != "Consumption":
                continue
            missing = [k for k in _REQUIRED if k not in item]
            if missing:
                raise ValueError(f"azure(archive): price row lost {missing}")
            rows.append({k: item.get(k) for k in _FIELDS})
        snaps.append(SimpleNamespace(endpoint=endpoint(region), payload={"region": region, "items": rows},
                                     fetched_at=at, ok=True))
    return AzureProvider.normalize({PATH: snaps})


SOURCES = [Source("azure", "prices.azure.com/api/retail/prices",
                  (("backfill.parsers.azure.azure_retail_first_page (live normalizer)", azure_retail_first_page),))]
