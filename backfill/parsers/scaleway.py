"""Scaleway: archived captures of www.scaleway.com/en/pricing/ (2019 - 2024) and /en/pricing/gpu/ (May 2024 on).

Live, OpenGrid reads Scaleway's own public product catalogue (providers/scaleway.py): EUR list prices converted to USD
at the ECB rate in force at fetch, one listing per general-availability GPU offer, listing id = the offer id (or the
id Shadeform gave it: LEGACY_LISTING_IDS), raw GPU name = the catalogue's GPU type + the offer's per-GPU VRAM
("H100-PCIe 80GB"). The archive has no copy of that API, but Scaleway's price pages print the same offers and the same
EUR prices ("Prices before tax"). Each design is a <table> whose header starts Name | Cores (or vCPUs) | GPU; one row
per offer, the hourly EUR price in the Price column:

  scaleway_pricing_2019    /en/pricing/ 2019:      Name | Xeon Gold Cores | Nvidia P100 GPU | Memory | SSD Storage | Bandwidth |
                                                   Price, "€500/ mo €1/ hr"
  scaleway_pricing_2020    /en/pricing/ 2019 - 22: Name | Cores | GPU | RAM | SSD | Bandwidth | Price,
                                                   "€500/month €1/hour" or "€1/hour"
  scaleway_pricing_2023    /en/pricing/ Jun 2022 - May 2024: Name | Cores | GPU | RAM | Disks | Bandwidth | Price,
                                                   "€0.85/hour", from Mar 2023 "€1.221/hour (~€891/month)"
  scaleway_gpu_pricing_2024 /en/pricing/gpu/ 2024 - 25: ... | Price | Approx. per month, "€2.52 /hour"
  scaleway_gpu_pricing_2026 /en/pricing/gpu/ 2026: one table per GPU family, each for one availability zone ("Select
                                                   Availability Zone PAR-1"; Jan 2026 without one: no region), Price
                                                   (€/hour) "€0.79 / HOUR", or "Not available in this zone" (skipped)

Read: the hourly EUR price of each GPU row (the whole instance), converted at the ECB EUR->USD rate in force at the
capture (backfill/eur.py; fail closed). Never read: monthly figures, a price marked "From" / "starting at" (a floor,
not the offer's price: the 2024 /en/pricing/ H100 rows), rows marked "coming soon". Where the page says "the H100 PCIe
GPU instance is offered as a spot instance" (2024), the H100 PCIe rows are stored as market spot, interruptible.

Names: the offer id as printed ("H100-1-80G", "L40S-4-48G", "RENDER-S"; "GPU - 3070" -> "GPU-3070"). GPU type per
offer family, as the live catalogue names it (providers/scaleway.py raw_gpu_name): H100-<n>-80G "H100-PCIe",
H100-SXM-* "H100-SXM", L4-* "L4", L40S-* "L40S", B300-SXM-* "B300-SXM", RENDER-S "P100" (unmapped, as live). GPU-3070
(retired 2025, never in the live catalogue) is NVIDIA's RTX 3070 per Scaleway's product page, a card made only with
8 GB: "RTX 3070 8GB". Any other offer keeps its id as the raw name (stored unmapped). The GPU count printed in the row
must equal the count in the offer id. Region: the zone a 2026 table shows ("fr-par-1"), else none.
"""
from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal

from backfill.eur import eur_listing, rate
from backfill.parsers import text
from backfill.sources import own_page
from models import ComputeListing
from providers.scaleway import LEGACY_LISTING_IDS, raw_gpu_name

_TABLE = re.compile(r"<table\b.*?</table>", re.S)
_TH = re.compile(r"<th\b[^>]*>(?P<c>.*?)</th>", re.S)
_TR = re.compile(r"<tr\b[^>]*>(?P<r>.*?)</tr>", re.S)
_TD = re.compile(r"<td\b[^>]*>(?P<c>.*?)</td>", re.S)
_STRIP = re.compile(r"<(script|style|svg)\b.*?</\1>", re.S)
_NUM = r"(?P<v>\d+(?:,\d{3})*(?:\.\d+)?)"
_BUTTON = re.compile(r"\s+(?:Create|Order)$")
_FLOOR = re.compile(r"^(?:From|from|starting at|Starting at)\b")
_ZONE = re.compile(r"Select Availability Zone\s+(?P<z>[A-Z]{3})-(?P<n>\d)")
_ZONE_PREFIX = {"PAR": "fr-par", "AMS": "nl-ams", "WAW": "pl-waw", "MIL": "it-mil"}
_OFFER = re.compile(r"^(?P<id>[A-Z0-9]+(?:-[A-Z0-9]+)*)(?:\s*/\s*(?P<tag>.+))?$")
_COUNT_IN_ID = re.compile(r"^[A-Z0-9]+(?:-SXM)?-(?P<n>\d+)-\d+G$")
_GPUS = re.compile(r"^(?P<n>\d+)\s*GPUs?$")
_INT = re.compile(r"^(?P<n>\d+)\b")
_GB = re.compile(r"^(?P<n>\d+(?:\.\d+)?)\s*GB\b")
# offer family -> the live catalogue's GPU type
_TYPES = ((re.compile(r"^H100-SXM-\d+-80G$"), "H100-SXM"), (re.compile(r"^H100-\d+-80G$"), "H100-PCIe"),
          (re.compile(r"^L40S-\d+-48G$"), "L40S"), (re.compile(r"^L4-\d+-24G$"), "L4"),
          (re.compile(r"^B300-SXM-\d+-288G$"), "B300-SXM"), (re.compile(r"^RENDER-S$"), "P100"))
FIXED_RAW = {"GPU-3070": "RTX 3070 8GB"}
SPOT_NOTE = "the H100 PCIe GPU instance is offered as a spot instance"


def _raw(offer: str) -> str:
    if offer in FIXED_RAW:
        return FIXED_RAW[offer]
    for pattern, gpu_type in _TYPES:
        if pattern.match(offer):
            return raw_gpu_name(gpu_type, offer)
    return offer


def _read(page: str, at: datetime, design: str, heads: tuple[tuple[str, ...], ...], price: re.Pattern,
          zones: bool = False) -> list[ComputeListing]:
    page = _STRIP.sub(" ", page)
    spot_h100 = SPOT_NOTE in text(page)
    out, tables, seen, fx_rate = [], 0, set(), None
    prev_end = 0
    for tm in _TABLE.finditer(page):
        t, start, prev_end = tm.group(0), prev_end, tm.end()
        head = tuple(text(h["c"]) for h in _TH.finditer(t))
        if len(head) < 3 or head[0] != "Name" or "GPU" not in head[2]:
            continue  # not a GPU table
        if head not in heads:
            raise ValueError(f"scaleway(archive): GPU table header {head!r} is not the {design} design")
        tables += 1
        i_price = next(i for i, h in enumerate(head) if h.startswith("Price"))
        region = country = None
        if zones:
            before = text(page[start:tm.start()])
            z = list(_ZONE.finditer(before))
            if z:  # Jan 2026 printed one list without a zone selector: region stays empty then
                z = z[-1]
                region = f"{_ZONE_PREFIX.get(z['z'], z['z'].lower())}-{z['n']}"
                country = region[:2].upper() if z["z"] in _ZONE_PREFIX else None
        for rm in _TR.finditer(t):
            cells = [text(c["c"]) for c in _TD.finditer(rm["r"])]
            if len(cells) <= 1:
                continue  # the header row, or a full-width "Legal Notice" row
            if len(cells) <= i_price - (1 if zones else 0) or len(cells) < 4:
                raise ValueError(f"scaleway(archive): row {cells!r} is shorter than its header {head!r}")
            name = re.sub(r"\s*-\s*", "-", cells[0])
            m = _OFFER.match(name)
            if m is None:
                raise ValueError(f"scaleway(archive): row name {cells[0]!r} is not an offer id")
            if m["tag"] and "coming soon" in m["tag"].lower():
                continue
            offer = m["id"]
            cell = _BUTTON.sub("", cells[i_price]) if i_price < len(cells) else ""
            if "Not available in this zone" in cell or "Not available in this zone" in " ".join(cells[3:]):
                continue
            if _FLOOR.match(cell):
                continue  # a floor price, not the offer's
            p = price.match(cell)
            if p is None:
                raise ValueError(f"scaleway(archive): {offer}: price {cell!r} is not the {design} hourly format")
            gpus = _GPUS.match(cells[2])
            if gpus is None:
                raise ValueError(f"scaleway(archive): {offer}: GPU cell {cells[2]!r} is not 'N GPU(s)'")
            count = int(gpus["n"])
            in_id = _COUNT_IN_ID.match(offer)
            if in_id and int(in_id["n"]) != count:
                raise ValueError(f"scaleway(archive): {offer} prints {count} GPU(s)")
            if offer in seen:
                continue
            seen.add(offer)
            eur = Decimal(p["v"].replace(",", ""))
            if eur <= 0:
                continue
            spot = spot_h100 and _raw(offer).startswith("H100-PCIe")
            fx_rate = fx_rate or rate("scaleway", at)
            vcpu, ram = _INT.match(cells[1]), _GB.match(cells[3])
            o = eur_listing("scaleway", sku=offer, listing_id=LEGACY_LISTING_IDS.get(offer, offer), raw=_raw(offer),
                            count=count, eur_instance_price=eur, at=at, fx_rate=fx_rate, region=region,
                            country=country, tier="vm", vcpu=int(vcpu["n"]) if vcpu else None,
                            ram_gb=float(ram["n"]) if ram else None)
            if spot:
                o = o.model_copy(update={"market_type": "spot", "interruptible": True})
            out.append(o)
    if not tables:
        raise ValueError(f"scaleway(archive): no GPU table ({design})")
    if not out:
        raise ValueError(f"scaleway(archive): GPU table has no priced on-demand row ({design})")
    return out


_H_2019 = (("Name", "Xeon Gold Cores", "Nvidia P100 GPU", "Memory", "SSD Storage", "Bandwidth", "Price"),)
_H_2020 = (("Name", "Cores", "GPU", "RAM", "SSD", "Bandwidth", "Price"),)
_H_2023 = (("Name", "Cores", "GPU", "RAM", "Disks", "Bandwidth", "Price"),)
_H_2024 = tuple((("Name", c, "GPU", "RAM", "Disks", "Bandwidth", "Price", "Approx. per month")) for c in ("Cores", "vCPUs"))
_H_2026 = (("Name", "vCPU", "GPU", "RAM", "Disks", "Bandwidth", "Price (€/hour)", "Approx. per month"),)

_P_2019 = re.compile(rf"^(?:€\d+(?:\.\d+)?/ ?mo )?€{_NUM}/ ?hr$")
_P_2020 = re.compile(rf"^(?:€\d+(?:,\d{{3}})*(?:\.\d+)?/month )?€{_NUM}/hour$")
_P_2023 = re.compile(rf"^€{_NUM}/hour(?: \(~€\d+(?:,\d{{3}})*(?:\.\d+)?/month\))?$")
_P_2024 = re.compile(rf"^€{_NUM} ?/hour$")
_P_2026 = re.compile(rf"^€{_NUM} / HOUR$")


def scaleway_pricing_2019(page: str, at: datetime) -> list[ComputeListing]:
    return _read(page, at, "2019 'Nvidia P100 GPU' column", _H_2019, _P_2019)


def scaleway_pricing_2020(page: str, at: datetime) -> list[ComputeListing]:
    return _read(page, at, "2020-22 '€x/hour'", _H_2020, _P_2020)


def scaleway_pricing_2023(page: str, at: datetime) -> list[ComputeListing]:
    return _read(page, at, "2022-24 '€x/hour [(~€y/month)]'", _H_2023, _P_2023)


def scaleway_gpu_pricing_2024(page: str, at: datetime) -> list[ComputeListing]:
    return _read(page, at, "pricing/gpu 2024-25 '€x /hour'", _H_2024, _P_2024)


def scaleway_gpu_pricing_2026(page: str, at: datetime) -> list[ComputeListing]:
    return _read(page, at, "pricing/gpu 2026 per-zone '€x / HOUR'", _H_2026, _P_2026, zones=True)


SOURCES = [
    own_page("scaleway", "www.scaleway.com/en/pricing/", scaleway_pricing_2019, scaleway_pricing_2020,
             scaleway_pricing_2023),
    own_page("scaleway", "www.scaleway.com/en/pricing/gpu/", scaleway_gpu_pricing_2024, scaleway_gpu_pricing_2026),
]
