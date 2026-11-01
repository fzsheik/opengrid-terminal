"""Lium: archived captures of the lium.io marketplace page.

The page is a Next.js app whose __NEXT_DATA__ carries the dehydrated result of the marketplace query (queryKey
["/executors", ...], ["/nodes", ...] from Oct 2026): executor objects of the same shape the live poller reads from
/api/executors (machine_name, price_per_gpu, gpu_count, location, specs). They go through the live parser unchanged
(providers/lium.py: _keep, then LiumProvider.normalize: one listing per executor at its own per-GPU price).

Differences from live, stated: the page embeds only the executors it server-rendered (9 to 92 per capture, against
~700 from the API), so an archived capture is a SUBSET of the marketplace and its lowest price is an upper bound on
the true lowest; and captures before mid-2026 carry no top-level gpu_count, so the count is taken from the
executor's own specs.gpu.count (the field the API added later mirrors it).
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from types import SimpleNamespace

from backfill.sources import own_page
from models import ComputeListing
from providers.lium import ENDPOINT, LiumProvider, _keep

_NEXT = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(?P<j>.*?)</script>', re.S)
QUERY_KEYS = ("/executors", "/nodes")


def lium_next_data(page: str, at: datetime) -> list[ComputeListing]:
    m = _NEXT.search(page)
    if m is None:
        raise ValueError("lium(archive): no __NEXT_DATA__")
    queries = (((json.loads(m["j"]).get("props") or {}).get("pageProps") or {}).get("dehydratedState") or {}).get("queries") or []
    executors = None
    for q in queries:
        key = q.get("queryKey") or []
        data = (q.get("state") or {}).get("data")
        if key and key[0] in QUERY_KEYS and isinstance(data, list):
            executors = data
            break
    if executors is None:
        raise ValueError("lium(archive): no executors query in the page data")
    rows = []
    for e in executors:
        if not isinstance(e, dict):
            continue
        if e.get("gpu_count") is None:
            gpu = (e.get("specs") or {}).get("gpu") or {}
            e = {**e, "gpu_count": gpu.get("count") if isinstance(gpu.get("count"), int) else None}
        rows.append(_keep(e))
    snap = SimpleNamespace(endpoint=ENDPOINT, payload=rows, fetched_at=at, ok=True)
    return LiumProvider.normalize({ENDPOINT: [snap]})


SOURCES = [own_page("lium", "lium.io", lium_next_data)]
