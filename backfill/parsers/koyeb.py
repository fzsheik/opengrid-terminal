"""Koyeb: archived captures of www.koyeb.com/pricing.

The page prints one card per GPU instance: a name ("A100 SXM", "2x H100", "RTX-4000-SFF-ADA"), "$2.15 /hr" for the
whole instance, and vCPU / RAM / Disk. It prints NO VRAM, and the bare names map to the wrong variant in
canonical.py ("A100 SXM" -> A100 40GB SXM4, while Koyeb's is the 80GB card).

Live, OpenGrid reads Koyeb's anonymous catalogue (providers/koyeb.py): listing id = the instance type id
("gpu-nvidia-a100-sxm", "2-gpu-nvidia-h100"), raw name = gpu.name + per-GPU VRAM ("gpu-nvidia-a100-sxm 80GB"). A card
name is that type's display name, so the id follows from it ("2x A100 SXM" -> "2-gpu-nvidia-a100-sxm"), and the
per-GPU VRAM is the catalogue's for the same GPU type (LIVE_VRAM, read from the live catalogue on 2026-11-01; Koyeb
has one memory size per GPU type). Each card then becomes the live listing exactly: same id, raw name and canonical
GPU, provider_tier "serverless". A card whose GPU is not in LIVE_VRAM keeps the page's own name and id and is stored
unmapped. Cards the live catalogue marks RESTRICTED (sales-gated, e.g. 8x H100) give no live listing; the page
printed their price, so the archived row is kept under the same id.

Until 2026-11 SOURCES was empty (production had no archived Koyeb row for that reason alone): names without VRAM
could not be mapped. koyeb_cards reads every capture from Dec 2024 to Oct 2026; e.g. "H100" $3.30/hr until late 2025
vs $2.50/hr in Oct 2026.

Nov 2024 (koyeb_vram_cards): one line per GPU, "RTX-A6000 VRAM 48 GB ( 10 vCPUs / 100 GB RAM ) $0.73 /hr", one GPU
each; the page states the VRAM itself, which is used (it agrees with LIVE_VRAM). Earlier captures print no GPU price.

koyeb_catalog: the one capture of the live endpoint itself (Mar 2025), read by the live normalizer; it states the same
per-type VRAM and the same prices as the page that month.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from types import SimpleNamespace

import canonical
from backfill.parsers import dec, slug, text
from backfill.sources import Source, own_page
from models import ComputeListing
from providers import PROVIDERS
from providers.koyeb import ENDPOINT

_CARD = re.compile(r'<div class="row items-center gap-3">(?P<name>.*?)</div>\$<!-- -->(?P<p>[\d.,]+)<!-- --> /hr</div>', re.S)
_MULTI = re.compile(r"^(\d+)x\s+(.+)$")
# GPU type (the live id after "gpu-nvidia-") -> per-GPU VRAM in GB, from the live catalogue (gpu.memory / count).
LIVE_VRAM = {"rtx-4000-sff-ada": 20, "l4": 24, "rtx-a6000": 48, "l40s": 48, "a100": 80, "a100-sxm": 80,
             "rtx-pro-6000": 96, "h100": 80, "h200": 141, "b200": 180}
_LABELLED = re.compile(r'text-dark/50">(?P<label>vCPU|RAM|Disk)</div><div class="typo-body-lg">(?P<v>[^<]+)</div>')


def koyeb_cards(page: str, at: datetime) -> list[ComputeListing]:
    out = []
    cards = list(_CARD.finditer(page))
    for i, m in enumerate(cards):
        rest = page[m.end(): cards[i + 1].start() if i + 1 < len(cards) else m.end() + 1500]
        name = text(re.sub(r"<span.*?</span>", "", m["name"], flags=re.S))   # drop the "Popular" badge
        mm = _MULTI.match(name)
        count, model = (int(mm.group(1)), mm.group(2)) if mm else (1, name)
        price = dec(m["p"])
        if price is None or price <= 0:
            continue
        spec: dict[str, str] = {}
        for x in _LABELLED.finditer(rest):
            spec.setdefault(x["label"], x["v"])       # this card's own labels come first
        gpu = slug(model)
        if gpu in LIVE_VRAM:
            lid = (f"{count}-" if count > 1 else "") + f"gpu-nvidia-{gpu}"
            raw = f"gpu-nvidia-{gpu} {LIVE_VRAM[gpu]}GB"
            mapped = canonical.canonical_gpu_name(raw)
        else:
            lid, raw, mapped = slug(name), model, None
        out.append(ComputeListing(
            provider="koyeb", sku=lid, listing_id=lid, raw_gpu_name=raw, canonical_gpu_name=mapped,
            gpu_count=count, region=None, country=None, price_per_gpu_hour=price / count,
            price_per_instance_hour=price, currency="USD", market_type="on_demand", provider_tier="serverless",
            interruptible=False, available=None, capacity=None, capacity_unit=None,
            vcpu=int(spec["vCPU"]) if spec.get("vCPU", "").isdigit() else None,
            ram_gb=float(spec["RAM"][:-2]) if re.fullmatch(r"\d+GB", spec.get("RAM", "")) else None,
            storage_gb=float(spec["Disk"][:-2]) if re.fullmatch(r"\d+GB", spec.get("Disk", "")) else None,
            observed_at=at))
    if not out:
        raise ValueError("koyeb(archive): no priced instance card")
    return out


_VRAM_CARD = re.compile(r"(?P<name>[A-Z][A-Za-z0-9-]*) VRAM (?P<vram>\d+) GB \( (?P<vcpu>\d+) vCPUs / (?P<ram>\d+) GB RAM \)"
                        r" \$(?P<p>\d+(?:\.\d+)?) /hr")


def koyeb_vram_cards(page: str, at: datetime) -> list[ComputeListing]:
    t = text(re.sub(r"<(script|style)\b.*?</\1>", " ", page, flags=re.S))
    out = []
    for m in _VRAM_CARD.finditer(t):
        gpu, vram, price = slug(m["name"]), int(m["vram"]), dec(m["p"])
        if price is None or price <= 0:
            continue
        raw = f"gpu-nvidia-{gpu} {vram}GB"
        out.append(ComputeListing(
            provider="koyeb", sku=f"gpu-nvidia-{gpu}", listing_id=f"gpu-nvidia-{gpu}", raw_gpu_name=raw,
            canonical_gpu_name=canonical.canonical_gpu_name(raw), gpu_count=1, region=None, country=None,
            price_per_gpu_hour=price, price_per_instance_hour=price, currency="USD", market_type="on_demand",
            provider_tier="serverless", interruptible=False, available=None, capacity=None, capacity_unit=None,
            vcpu=int(m["vcpu"]), ram_gb=float(m["ram"]), storage_gb=None, observed_at=at))
    if not out:
        raise ValueError("koyeb(archive): no '<GPU> VRAM <N> GB ( .. ) $x /hr' line")
    return out


def koyeb_catalog(body: bytes, at: datetime) -> list[ComputeListing]:
    """A capture of the live poller's own endpoint (app.koyeb.com/v1/catalog/instances?limit=100; one, Mar 2025), read
    exactly as live: the GPU instances kept as fetch() keeps them, then KoyebProvider.normalize."""
    d = json.loads(body)
    if not isinstance(d, dict) or not isinstance(d.get("instances"), list):
        raise ValueError("koyeb(archive): catalogue has no instances list")
    payload = {"count": d.get("count"), "listed": len(d["instances"]),
               "instances": [i for i in d["instances"] if i.get("gpu")]}
    return PROVIDERS["koyeb"].normalize({ENDPOINT: [SimpleNamespace(endpoint=ENDPOINT, payload=payload, fetched_at=at,
                                                                    ok=True)]})


SOURCES = [own_page("koyeb", "www.koyeb.com/pricing", koyeb_cards, koyeb_vram_cards),
           Source("koyeb", "app.koyeb.com/v1/catalog/instances?limit=100",
                  (("backfill.parsers.koyeb.koyeb_catalog (KoyebProvider.normalize, live parser)", koyeb_catalog),))]
