"""SimplePod: archived captures of its public rental market, api.simplepod.ai/instances/market/list (the live poller's
endpoint, captured Jun and Oct 2026 without the poller's itemsPerPage=500).

Read exactly as live (providers/simplepod.py: the kept fields, then SimplePodProvider.normalize): one listing per
rentable on-demand offer, the WHOLE offer's `pricePerGpu` divided by its gpuCount, listing id "<mode>:<offer id>".

Without itemsPerPage the API returns its default page of 30 offers. A capture holding 30 offers (both Docker-market
captures) may be the first page of a longer book, and a marketplace read from part of its book is biased, so it is
refused, as live refuses a full page. The VM-market captures (11 and 12 offers) are whole books and are read.
"""
from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

from backfill.sources import Source
from models import ComputeListing
from providers import PROVIDERS
from providers.simplepod import _KEEP, MARKETS

DEFAULT_PAGE = 30   # the API's page size when itemsPerPage is not given


def _parser(mode: str):
    endpoint = MARKETS[mode][0]

    def simplepod_market(body: bytes, at: datetime) -> list[ComputeListing]:
        offers = json.loads(body)
        if not isinstance(offers, list):
            raise ValueError("simplepod(archive): the market is not a list of offers")
        if len(offers) >= DEFAULT_PAGE:
            raise ValueError(f"simplepod(archive): {len(offers)} offers fill the default page of {DEFAULT_PAGE}; "
                             "the capture may hold part of the book only")
        payload = [{k: o.get(k) for k in _KEEP} for o in offers if isinstance(o, dict)]
        snap = SimpleNamespace(endpoint=endpoint, payload=payload, fetched_at=at, ok=True)
        return PROVIDERS["simplepod"].normalize({endpoint: [snap]})
    return f"backfill.parsers.simplepod.simplepod_market[{mode}] (SimplePodProvider.normalize, live parser)", simplepod_market


SOURCES = [Source("simplepod", "api.simplepod.ai/instances/market/list?rentalStatus=active", (_parser("docker"),)),
           Source("simplepod", "api.simplepod.ai/instances/market/list?mode=vm&rentalStatus=active", (_parser("vm"),))]
