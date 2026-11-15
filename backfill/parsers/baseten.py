"""Baseten: archived captures of www.baseten.co/pricing/.

    live parser             providers/baseten.py extract + BasetenProvider.normalize unchanged (the Next.js stream's
                            "Dedicated Deployments" instanceList, or since 2026-11 its "Compute pricing" table; hourly
                            prices cross-checked against per-minute x 60).
    baseten_specs_with_cpu  May - Nov 2025: the same instanceList, but each GPU instance's specs also name its CPUs
                            and memory ("80 GiB VRAM, 12 vCPUs, 144 GiB RAM"), which the live parser's VRAM check
                            rejects. The specs must read exactly "<n> GiB VRAM|VM, <n> vCPUs, <n> GiB RAM"; the VRAM
                            part goes through the live normalizer (same ids, raw names, per-minute cross-check) and
                            the vCPUs / RAM are kept on the listing.

Captures before May 2025 hold neither table in the stream (or no stream at all) and store nothing.
"""
from __future__ import annotations

import re
from datetime import datetime
from types import SimpleNamespace

from backfill.sources import Source, live_page
from models import ComputeListing

_SPECS = re.compile(r"^(\d+\s*GiB\s+(?:VRAM|VM)),\s*(\d+) vCPUs,\s*(\d+) GiB RAM$")


def baseten_specs_with_cpu(page: str, at: datetime) -> list[ComputeListing]:
    from providers import baseten as B

    payload = B.extract(page)
    extra: dict[str, tuple[int, float]] = {}
    for i in payload["instances"]:
        if i["instanceType"] != "gpu":
            continue
        m = _SPECS.match(str(i["specs"]).strip())
        if m is None:
            raise ValueError(f"baseten(archive): GPU instance {i['name']!r} specs {i['specs']!r} are not "
                             "'<n> GiB VRAM, <n> vCPUs, <n> GiB RAM'")
        i["specs"] = m.group(1)
        extra[str(i["name"]).strip()] = (int(m.group(2)), float(m.group(3)))
    if not extra:
        raise ValueError("baseten(archive): no GPU instance with CPU and RAM in its specs")
    snap = SimpleNamespace(endpoint=B.ENDPOINT, payload=payload, fetched_at=at, ok=True)
    out = B.BasetenProvider.normalize({B.ENDPOINT: [snap]})
    return [l.model_copy(update={"vcpu": extra[l.sku][0], "ram_gb": extra[l.sku][1]}) if l.sku in extra else l
            for l in out]


def _source() -> Source:
    base = live_page("baseten", "baseten", "www.baseten.co/pricing/")
    older = ("backfill.parsers.baseten.baseten_specs_with_cpu",
             lambda body, at: baseten_specs_with_cpu(body.decode("utf-8", errors="replace"), at))
    return Source(base.provider, base.url, (*base.parsers, older))


SOURCES = [_source()]
