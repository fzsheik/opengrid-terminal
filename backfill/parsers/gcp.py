"""Google Cloud: archived designs of the Accelerator-optimized pricing page the live parser (providers/gcp.py) rejects.

    gcp_on_demand_column   Mar - Jul 2026: the machine table's price column is headed "On-Demand (USD)" (live:
                           "Price (USD)"), family heading rows ("A4 High") are one cell wide, and until Jul the
                           section is headed "Accelerator-optimized pricing" (live: "GPU-accelerated machines
                           pricing"). Everything else is the live parser's: the us-central1 region check on both
                           sections, the attachment table (providers.gcp._attachable), price-cell checks, and
                           GcpProvider.normalize with its GPU_MACHINES / ATTACHABLE tables and cross-checks.
"""
from __future__ import annotations

import re
from datetime import datetime
from types import SimpleNamespace

from backfill.sources import live_page
from models import ComputeListing


def _machines_old(body: str) -> list[dict]:
    from providers import gcp as G

    out = []
    for table in G._TABLE.findall(body):
        rows = G._rows(table)
        head = rows[0] if rows else []
        if head[:3] != ["Machine type", "GPU", "Components"] or len(head) < 4 or head[3] != "On-Demand (USD)":
            raise ValueError(f"gcp(archive): machine table header is not the 'On-Demand (USD)' design: {head[:4]}")
        for cells in rows[1:]:
            if len(cells) == 1 or (len(cells) == len(head) and not cells[1] and not cells[2]):
                continue  # a family heading row ("A3 High")
            if len(cells) != len(head):
                raise ValueError(f"gcp(archive): machine row has {len(cells)} cells, header has {len(head)}")
            if not G._MACHINE_TYPE.match(cells[0]):
                raise ValueError(f"gcp(archive): unreadable machine type {cells[0]!r}")
            parts = dict(G._COMPONENT.findall(cells[2]))
            out.append({"machine_type": cells[0], "gpu": cells[1], "gpus": parts.get("GPUs"),
                        "vcpus": parts.get("vCPUs"), "memory": parts.get("Memory"),
                        "price": G._check_price(cells[3], cells[0])})
    if not out:
        raise ValueError("gcp(archive): no GPU machine rows found")
    return out


def gcp_on_demand_column(page: str, at: datetime) -> list[ComputeListing]:
    from providers import gcp as G

    start = re.compile(G._H.format("GPU-accelerated machines pricing"))
    if not start.search(page):
        start = re.compile(G._H.format("Accelerator-optimized pricing"))
    machines = G._section(page, start, G._ATTACH_HEAD, "GPU-accelerated machines pricing (archived design)")
    attach = G._section(page, G._ATTACH_HEAD, G._VWS_HEAD, "GPU Attachment pricing")
    payload = {"region": G.REGION_LABEL, "machines": _machines_old(machines), "attachable": G._attachable(attach)}
    ep = G.ENDPOINT.split("?")[0]
    return G.GcpProvider.normalize({ep: [SimpleNamespace(endpoint=ep, payload=payload, fetched_at=at)]})


SOURCES = [live_page("gcp", "gcp", "cloud.google.com/products/compute/pricing/accelerator-optimized",
                     gcp_on_demand_column)]
