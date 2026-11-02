"""RunPod: archived captures of www.runpod.io/pricing (OpenGrid's live RunPod data comes from the GraphQL API, which
needs a key; the archive's captures of api.runpod.io/graphql are bare GETs, not priced queries, so they are not
read).

Current design (Feb 2026 on, gpu_cards): the page lists one card per GPU: the GPU name, its VRAM ("80 GB VRAM", "288 GB HBM3e") and a price element
carrying BOTH clouds' per-GPU hourly prices as labelled attributes (data-secure="3.99" data-community="2.69").
Each card becomes up to two listings, one per cloud, shaped like the live normalizer's: provider_tier secure /
community, community interruptible, one GPU at the per-GPU rate, listing id "<api gpu id>:<cloud>:1" (the same
id the live 1-GPU listing has).

Page names are translated to the GraphQL gpu id the live API reports (PAGE_TO_API) only where the card's name and
VRAM make the product unambiguous. "RTX Pro 6000" (Server or Workstation Edition not stated) and the MIG slices
are left under the page's own name, so they stay unmapped unless canonical.py knows them.
"""
from __future__ import annotations

import re
from datetime import datetime

from backfill.parsers import dec, listing, text
from backfill.sources import own_page
from models import ComputeListing

# (card name, VRAM GB) -> the live GraphQL gpu id
PAGE_TO_API = {
    ("B300", 288): "NVIDIA B300 SXM6 AC",
    ("B200", 180): "NVIDIA B200",
    ("H200", 141): "NVIDIA H200",
    ("H200 NVL", 143): "NVIDIA H200 NVL",
    ("H100 NVL", 94): "NVIDIA H100 NVL",
    ("H100 PCIe", 80): "NVIDIA H100 PCIe",
    ("H100 SXM", 80): "NVIDIA H100 80GB HBM3",
    ("A100 PCIe", 80): "NVIDIA A100 80GB PCIe",
    ("A100 SXM", 80): "NVIDIA A100-SXM4-80GB",
    ("A100 SXM", 40): "NVIDIA A100-SXM4-40GB",
    ("L40S", 48): "NVIDIA L40S",
    ("L40", 48): "NVIDIA L40",
    ("L4", 24): "NVIDIA L4",
    ("A40", 48): "NVIDIA A40",
    ("RTX 6000 Ada", 48): "NVIDIA RTX 6000 Ada Generation",
    ("RTX 5000 Ada", 32): "NVIDIA RTX 5000 Ada Generation",
    ("RTX 4000 Ada", 20): "NVIDIA RTX 4000 Ada Generation",
    ("RTX 2000 Ada", 16): "NVIDIA RTX 2000 Ada Generation",
    ("RTX A6000", 48): "NVIDIA RTX A6000",
    ("RTX A5000", 24): "NVIDIA RTX A5000",
    ("RTX A4500", 20): "NVIDIA RTX A4500",
    ("RTX A4000", 16): "NVIDIA RTX A4000",
    ("RTX 5090", 32): "NVIDIA GeForce RTX 5090",
    ("RTX 5080", 16): "NVIDIA GeForce RTX 5080",
    ("RTX 4090", 24): "NVIDIA GeForce RTX 4090",
    ("RTX 3090", 24): "NVIDIA GeForce RTX 3090",
    ("MI300X", 192): "AMD Instinct MI300X OAM",
}

_CARD_SPLIT = re.compile(r'<div[^>]*role="listitem"')
_NAME = re.compile(r"<p[^>]*>(?P<n>[^<]+)</p>")
_VRAM = re.compile(r"<div[^>]*>\s*(?P<gb>\d+)\s*GB\s+(?P<kind>VRAM|HBM\w*|GDDR\w*)\s*</div>")
_PRICES = re.compile(r'data-secure="(?P<secure>[^"]*)"\s+data-community="(?P<community>[^"]*)"'
                     r'|data-community="(?P<community2>[^"]*)"\s+data-secure="(?P<secure2>[^"]*)"')


def _price(v: str, where: str):
    v = (v or "").strip()
    if v in ("", "0", "0.00", "-", "null", "undefined"):
        return None
    p = dec(v)
    if p is None:
        raise ValueError(f"runpod(archive): unreadable {where} price {v!r}")
    return p


def gpu_cards(page: str, at: datetime) -> list[ComputeListing]:
    """Cards with a name, a VRAM line and the data-secure / data-community price attributes."""
    out = []
    for card in _CARD_SPLIT.split(page)[1:]:
        pm = _PRICES.search(card)
        if pm is None:
            continue  # not a GPU card (storage, clusters, ...)
        nm, vm = _NAME.search(card), _VRAM.search(card)
        if nm is None or vm is None:
            raise ValueError("runpod(archive): a priced card lacks its name or VRAM line")
        name, gb = text(nm["n"]), int(vm["gb"])
        raw = PAGE_TO_API.get((name, gb), f"{name} {gb}GB")
        clouds = {"secure": pm["secure"] if pm["secure"] is not None else pm["secure2"],
                  "community": pm["community"] if pm["community"] is not None else pm["community2"]}
        for cloud, value in clouds.items():
            price = _price(value, cloud)
            if price is None:
                continue  # not offered in this cloud
            o = listing("runpod", sku=f"{raw}:{cloud}", listing_id=f"{raw}:{cloud}:1", raw=raw, count=1,
                        instance_price=price, at=at, tier=cloud)
            out.append(o.model_copy(update={"interruptible": cloud == "community"}))
    if not out:
        raise ValueError("runpod(archive): no priced GPU cards")
    return out


_ROW_SPLIT = re.compile(r'<a[^>]*id="gpu-row"')
_ROW_NAME = re.compile(r'<div[^>]*data-line-clamp="[^"]*"[^>]*>(?P<n>[^<$]+)</div>')
_ROW_VRAM = re.compile(r"<div[^>]*>(?P<gb>\d+)</div>\s*<div[^>]*>\s*GB VRAM\s*</div>")
_ROW_PRICES = re.compile(r'data-secure-cloud-price="(?P<secure>[^"]*)"\s+data-community-cloud-price="(?P<community>[^"]*)"')
# The attributes hold the HOURLY rate (the page derives its per-second display from them client-side; the same
# captures' description reads "H100 80GB from $1.99/hr" = the H100 PCIe community attribute). A per-second
# value would be ~1/3600 of that, so anything below this is refused rather than read as hourly.
_MIN_HOURLY = 0.02


def gpu_rows(page: str, at: datetime) -> list[ComputeListing]:
    """Oct 2025 - Feb 2026: one <a id="gpu-row"> per GPU, name + "<n> GB VRAM" + data-secure-cloud-price /
    data-community-cloud-price."""
    out = []
    for row in _ROW_SPLIT.split(page)[1:]:
        pm = _ROW_PRICES.search(row)
        if pm is None:
            continue
        nm, vm = _ROW_NAME.search(row), _ROW_VRAM.search(row)
        if nm is None or vm is None:
            raise ValueError("runpod(archive): a priced gpu-row lacks its name or VRAM")
        name, gb = text(nm["n"]), int(vm["gb"])
        raw = PAGE_TO_API.get((name, gb), f"{name} {gb}GB")
        for cloud in ("secure", "community"):
            price = _price(pm[cloud], cloud)
            if price is None:
                continue
            if price < _MIN_HOURLY:
                raise ValueError(f"runpod(archive): {name} {cloud} price {price} looks per-second, not hourly")
            o = listing("runpod", sku=f"{raw}:{cloud}", listing_id=f"{raw}:{cloud}:1", raw=raw, count=1,
                        instance_price=price, at=at, tier=cloud)
            out.append(o.model_copy(update={"interruptible": cloud == "community"}))
    if not out:
        raise ValueError("runpod(archive): no priced gpu-rows")
    return out


_NEXT = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(?P<j>.*?)</script>', re.S)


def next_data_gpu_types(page: str, at: datetime) -> list[ComputeListing]:
    """www.runpod.io/gpu-instance/pricing, Jul 2022 - mid 2024: the page's __NEXT_DATA__ carries RunPod's GraphQL
    GpuType objects themselves (pageProps.data: a list, or from May 2024 {"gpu": {id: GpuType}}), with the live
    API's gpu `id`. Each cloud the type is offered in (secureCloud / communityCloud true) becomes one 1-GPU listing at
    that cloud's per-GPU price (securePrice / communityPrice): the live normalizer's listing id "<id>:<cloud>:1".
    A price for a cloud whose flag is false is not read (the page fills it anyway).
    Jul - Sep 2022 carried only lowestPrice.uninterruptablePrice (the cheaper cloud's price): read only when the type is
    offered in exactly one cloud, so the cloud is known. Spot / bid prices (communitySpotPrice, minimumBidPrice) are
    not read: the live RunPod listings have no spot market."""
    import json

    m = _NEXT.search(page)
    if m is None:
        raise ValueError("runpod(archive): no __NEXT_DATA__")
    data = ((json.loads(m["j"]).get("props") or {}).get("pageProps") or {}).get("data")
    if isinstance(data, dict) and isinstance(data.get("gpu"), dict):
        types = list(data["gpu"].values())
    elif isinstance(data, list):
        types = data
    else:
        raise ValueError("runpod(archive): __NEXT_DATA__ pageProps.data is not a GpuType list / map")
    out = []
    for g in types:
        if not isinstance(g, dict) or g.get("__typename") != "GpuType" or not g.get("id"):
            raise ValueError(f"runpod(archive): not a GpuType: {str(g)[:80]}")
        gpu_id = g["id"]
        offered = [c for c, flag in (("secure", "secureCloud"), ("community", "communityCloud")) if g.get(flag) is True]
        prices = {}
        if "securePrice" in g or "communityPrice" in g:
            for cloud in offered:
                prices[cloud] = g.get(f"{cloud}Price")
        elif len(offered) == 1:
            prices[offered[0]] = (g.get("lowestPrice") or {}).get("uninterruptablePrice")
        for cloud, v in prices.items():
            if v is None:
                continue
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ValueError(f"runpod(archive): {gpu_id} {cloud} price {v!r}")
            if v <= 0:
                continue
            o = listing("runpod", sku=f"{gpu_id}:{cloud}", listing_id=f"{gpu_id}:{cloud}:1", raw=gpu_id, count=1,
                        instance_price=dec(str(v)), at=at, tier=cloud)
            out.append(o.model_copy(update={"interruptible": cloud == "community"}))
    if not out:
        raise ValueError("runpod(archive): no priced GpuType in __NEXT_DATA__")
    return out


SOURCES = [own_page("runpod", "www.runpod.io/pricing", gpu_cards, gpu_rows, next_data_gpu_types),
           own_page("runpod", "www.runpod.io/gpu-instance/pricing", next_data_gpu_types)]
