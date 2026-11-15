"""Hetzner: archived captures of www.hetzner.com/dedicated-rootserver/matrix-gpu/ (from Mar 2024).

Live, OpenGrid scrapes this page (providers/hetzner.py): the HOURLY rate Hetzner prints for each GEX server, EUR excl.
VAT, converted at the ECB EUR->USD rate in force at fetch. The live parser reads the current card design; older ones:

  1. Mar - mid 2024: "GEX44 ... starting from € 184.00 monthly, excl. VAT": a MONTHLY price only. Not read: the live
     poller reads the printed hourly rate and never derives one from the monthly price (Hetzner's hourly rate is the
     monthly / 624 h, not / 730), so these captures store nothing (reported).
  2. 2024 (hetzner_server_rendered): one card per model, "<span class="product-pph-price"> € 0.3503 </span> /hr",
     "max/mo." and a page-wide VAT label in each card ("excl. VAT", or "incl. 19 % VAT" for a capture made from a
     German address). Only a card labelled "excl. VAT" is read; an incl.-VAT capture raises (reported, never divided).
  3. 2025 (hetzner_price_slots): "<price-container class="product-pph-price"><span slot="eur">0.2948</span>
     <span slot="usd">0.3285</span>". The VAT label is drawn by script (<vat-holder>), so the HTML does not state it:
     read as excl. VAT because the same card's monthly cap (184.00) and setup fee (79.00) are the figures the design-1
     and live pages label "excl. VAT". Hetzner's own USD slot is not read: EUR is converted at the ECB rate, as live.
  4. 2026 before the live design: prices are fetched by script per product key (<ho-price-container product-key=...>),
     none are in the HTML: nothing to read (reported).

Specs come from the page's spec grid (one "gpu-information-grid" per model; Apr 2024 one unnamed grid for its one
model): the GPU cell ("Nvidia RTX(TM) 4000 SFF Ada Generation (20GB GDDR6 ECC)", or a GPU cell plus a vRAM cell "20 GB
GDDR6") and RAM. Raw GPU name in the live shape: model with "(TM)" removed + " <VRAM>GB" ("NVIDIA RTX 6000 Ada
Generation 48GB"). Where canonical.py knows the card only without its VRAM ("NVIDIA RTX 4000 SFF Ada Generation"),
that name is used when its canonical VRAM is the one the page prints. Dec 2024 grids print no VRAM: the name is kept as
printed and mapped only if canonical.py knows it so. listing id = model lowercased ("gex44"), as live; tier "dedicated"; country DE when the page says "Our GPU
Servers are located in Germany" (2024), else none (2025: "in Europe").
"""
from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal

import canonical
from backfill.eur import eur_listing, rate
from backfill.parsers import text
from backfill.sources import live_page
from models import ComputeListing

_CARD = re.compile(r'<div\s+class="(?:overflow-hidden\s+)?product-wrapper\s+gpu-wrapper[^"]*"\s*>(?P<c>.*?)'
                   r'(?=<div\s+class="(?:overflow-hidden\s+|align-self-end\s+)?product-wrapper\s|'
                   r'<div\s+class="swiper-container\s+swiperSecondary)', re.S)
_NAME = re.compile(r'<div\s+class="d-flex\s+flex-column\s+product-text-name"\s*>(?P<v>.*?)</div>', re.S)
_PPH_TEXT = re.compile(r'<span class="product-pph-price">(?P<v>[^<]*)</span>\s*<span class="product-pph-label">\s*/hr', re.S)
_PPH_SLOT = re.compile(r'<price-container class="product-pph-price">\s*<span slot="eur">(?P<v>[^<]*)</span>', re.S)
_VAT = re.compile(r'<span class="product-vat-text">(?P<v>.*?)</span>', re.S)
_EUR = re.compile(r"^€\s*(?P<v>\d+(?:\.\d+)?)$")
_NUM = re.compile(r"^\d+(?:\.\d+)?$")
_GRID = re.compile(r'<div class="gpu-information-grid">(?P<g>.*?)(?=<div class="gpu-information-grid">|gpu-matrix-pagination|$)', re.S)
_GRID_NAME = re.compile(r'<div class="grid-header-name">(?P<v>.*?)</div>', re.S)
_GRID_HEAD = re.compile(r'<div class="item-header-[a-z] grid-header-text">(?:\s*<div class="grid-header-name">.*?</div>)?(?P<v>.*?)</div>', re.S)
_GRID_DATA = re.compile(r'<div class="item-[a-z] grid-data-text">(?P<v>.*?)</div>', re.S)
_GPU_PAREN = re.compile(r"^(?:(?P<n>\d+)x\s+)?(?P<model>(?:NVIDIA|Nvidia) .+?)\s*\((?P<vram>\d+)\s*GB\b[^)]*\)$")
_GPU_PLAIN = re.compile(r"^(?:(?P<n>\d+)x\s+)?(?P<model>(?:NVIDIA|Nvidia) .+)$")
_VRAM = re.compile(r"^(?:\d+x\s+)?(?P<vram>\d+)\s*GB\b")
_RAM = re.compile(r"^(?P<gb>\d+)\s*GB\b")
_MODEL = re.compile(r"\bGEX\d+\b")


def _specs(page: str) -> dict[str | None, dict]:
    """model -> {raw, canonical, count, ram_gb} from the spec grid; key None for a grid that names no model (Apr 2024,
    one model on the page)."""
    out = {}
    for g in _GRID.finditer(page):
        body = g["g"]
        name = _GRID_NAME.search(body)
        model = _MODEL.search(text(name["v"])) if name else None
        label = model.group(0) if model else None
        heads = [text(h["v"]) for h in _GRID_HEAD.finditer(body)]
        data = [text(d["v"]) for d in _GRID_DATA.finditer(body)]
        # the CPU column takes two cells (model, cores); every other column one
        cells = dict(zip(heads, [data[0]] + data[2:])) if len(data) == len(heads) + 1 else {}
        gpu = cells.get("GPU", "").replace("™", "")
        m = _GPU_PAREN.match(gpu)
        vram = m["vram"] if m else None
        if m is None:
            m = _GPU_PLAIN.match(gpu)
            v = _VRAM.match(cells.get("vRAM", ""))
            vram = v["vram"] if v else None
        if m is None:
            raise ValueError(f"hetzner(archive): {label}: GPU spec {cells.get('GPU')!r} unreadable")
        base = re.sub(r"\s+", " ", m["model"]).strip()
        base = re.sub(r"^Nvidia\b", "NVIDIA", base)
        if vram is None:  # Dec 2024: the grid names the card without its memory: kept as printed
            raw, canon = base, canonical.canonical_gpu_name(base)
        else:
            raw = f"{base} {vram}GB"
            canon = canonical.canonical_gpu_name(raw)
            if canon is None:
                bare = canonical.canonical_gpu_name(base)
                canon = bare if bare and bare.endswith(f" {vram}GB") else None
        ram = _RAM.match(cells.get("RAM", ""))
        out[label] = {"raw": raw, "canonical": canon, "count": int(m["n"] or 1),
                               "ram_gb": float(ram["gb"]) if ram else None}
    return out


def _country(page: str) -> str | None:
    return "DE" if "Our GPU Servers are located in Germany" in text(page) else None


def _read(page: str, at: datetime, design: str, hourly) -> list[ComputeListing]:
    if "<ho-price-container" in page:
        raise ValueError("hetzner(archive): prices are loaded by script per product key; none in the HTML (design 4)")
    cards = list(_CARD.finditer(page))
    if not cards:
        raise ValueError(f"hetzner(archive): no GEX product card ({design})")
    specs, country, out = _specs(page), _country(page), []
    fx_rate = None
    for c in cards:
        name = _NAME.search(c["c"])
        model = _MODEL.search(text(name["v"])) if name else None
        if model is None:
            raise ValueError("hetzner(archive): a GPU card has no GEX model name")
        eur = hourly(c["c"], model.group(0))
        if eur is None:
            continue
        spec = specs.get(model.group(0))
        if spec is None and len(cards) == 1 and list(specs) == [None]:
            spec = specs[None]  # one card, one unnamed spec grid: the grid is that card's
        if spec is None:
            raise ValueError(f"hetzner(archive): {model.group(0)} has a price but no spec in the GPU grid")
        fx_rate = fx_rate or rate("hetzner", at)
        out.append(eur_listing("hetzner", sku=model.group(0), listing_id=model.group(0).lower(), raw=spec["raw"],
                               canonical=spec["canonical"], count=spec["count"], eur_instance_price=eur, at=at,
                               fx_rate=fx_rate, country=country, tier="dedicated", ram_gb=spec["ram_gb"]))
    if not out:
        raise ValueError(f"hetzner(archive): no card with an hourly price ({design})")
    return out


def hetzner_server_rendered(page: str, at: datetime) -> list[ComputeListing]:
    """2024: '€ 0.3503 /hr' in the card, the card's VAT label must read 'excl. VAT'."""
    def hourly(card: str, model: str) -> Decimal | None:
        p = _PPH_TEXT.search(card)
        if p is None:
            if "monthly" in text(card):
                return None  # design 1: a monthly price only
            raise ValueError(f"hetzner(archive): {model}: no '/hr' price")
        vat = _VAT.search(card)
        label = text(vat["v"]) if vat else ""
        if label != "excl. VAT":
            raise ValueError(f"hetzner(archive): {model}: price labelled {label!r}, not 'excl. VAT' (not read)")
        m = _EUR.match(text(p["v"]))
        if m is None:
            raise ValueError(f"hetzner(archive): {model}: hourly price {text(p['v'])!r} is not '€ x'")
        return Decimal(m["v"])
    return _read(page, at, "server-rendered '€ x /hr'", hourly)


def hetzner_price_slots(page: str, at: datetime) -> list[ComputeListing]:
    """2025: <price-container class="product-pph-price"><span slot="eur">0.2948</span> (excl. VAT, see module doc)."""
    def hourly(card: str, model: str) -> Decimal | None:
        p = _PPH_SLOT.search(card)
        if p is None:
            raise ValueError(f"hetzner(archive): {model}: no EUR hourly price slot")
        v = text(p["v"])
        if not _NUM.match(v):
            raise ValueError(f"hetzner(archive): {model}: EUR hourly slot {v!r} is not a number")
        return Decimal(v)
    return _read(page, at, "price-container slots", hourly)


SOURCES = [live_page("hetzner", "hetzner", "www.hetzner.com/dedicated-rootserver/matrix-gpu/",
                     hetzner_server_rendered, hetzner_price_slots)]
