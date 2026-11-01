"""DigitalOcean: archived captures of www.digitalocean.com/pricing/gpu-droplets.

Live, OpenGrid reads DigitalOcean's API (providers/digitalocean.py). The archived page prints one card per GPU
Droplet: the GPU ("NVIDIA HGX H100", "AMD Instinct™ MI300X×8"), "$4.41 /GPU/hour" and what that price is
("On-Demand Price", "Current Spot Price*", or "12 Month Reserved Price" / "Reserved Price"), then labelled specs
("GPUs per Droplet 8" or "Choose between 1 or 8 GPU's per droplet", "GPU Memory 640 GB"). On-demand and spot cards
become listings at the printed per-GPU rate; reserved prices are commitments and are never read. GPU count: the
"GPUs per Droplet" figure, else the name's "×8", else 1 ("Choose between 1 or 8": the per-GPU price is the same).

Raw GPU name: the card's name without "×N" and "™", plus the per-GPU memory ("GPU Memory" / count): "NVIDIA HGX H100
80GB". The 2025 cards say "NVIDIA H100" (no HGX), which canonical.py rightly leaves unmapped (SXM or PCIe not
stated); from mid-2026 they say "NVIDIA HGX H100", which maps to the SXM5 card the live API reports.
"""
from __future__ import annotations

import re
from datetime import datetime

from backfill.parsers import dec, listing, slug, text
from backfill.sources import own_page
from models import ComputeListing

_STRIP = re.compile(r"<(script|style)\b.*?</\1>", re.S)
_CARD = re.compile(
    r"(?P<name>(?:NVIDIA|AMD Instinct™?)\s[^$]{1,60}?)\s+\$\s?(?P<p>\d+(?:\.\d+)?)\s*/GPU/hour\s+"
    r"(?P<kind>On-Demand Price|Current Spot Price\*?|(?:12 Month )?Reserved Price\*?)"
    r"(?=(?P<rest>.{0,260}))")      # look ahead only: the next card may start inside this window
_PER = re.compile(r"^GPUs per Droplet (?P<n>\d+)\b|^GPUs per Droplet Choose between")
_MEM = re.compile(r"GPU Memory (?P<m>\d+(?:,\d{3})*) GB")
_TIMES = re.compile(r"\s*×\s*(?P<n>\d+)\s*$")


def digitalocean_cards(page: str, at: datetime) -> list[ComputeListing]:
    t = text(_STRIP.sub(" ", page))
    out, seen_any = [], False
    for m in _CARD.finditer(t):
        seen_any = True
        if "Reserved" in m["kind"]:
            continue
        market = "spot" if "Spot" in m["kind"] else "on_demand"
        name = m["name"].strip()
        times = _TIMES.search(name)
        base = _TIMES.sub("", name).replace("™", "").strip()
        rest = m["rest"].strip()
        per = _PER.match(rest)
        count = int(per["n"]) if per and per["n"] else int(times["n"]) if times else 1
        mem = _MEM.search(rest) if market == "on_demand" else None
        if market == "on_demand" and (per is None or mem is None):
            raise ValueError(f"digitalocean(archive): on-demand card {name!r} lacks 'GPUs per Droplet' / 'GPU Memory'")
        vram = None
        if mem is not None:
            total = int(mem["m"].replace(",", ""))
            vram = total // count if per and per["n"] else total
        raw = f"{base} {vram}GB" if vram else base
        out.append(listing("digitalocean", sku=name, listing_id=slug(f"{base} {count}x") + ("" if market == "on_demand" else ":spot"),
                           raw=raw, count=count, instance_price=dec(m["p"]) * count, at=at, market=market,
                           tier="digitalocean-page"))
    if not seen_any:
        raise ValueError("digitalocean(archive): no '$x /GPU/hour' card")
    if not out:
        raise ValueError("digitalocean(archive): only reserved prices on the page")
    return out


SOURCES = [own_page("digitalocean", "www.digitalocean.com/pricing/gpu-droplets", digitalocean_cards)]
