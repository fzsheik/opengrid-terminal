"""E2E Networks: archived captures of www.e2enetworks.com/pricing.json (the live poller's feed) and of the
www.e2enetworks.com/pricing page.

Live, OpenGrid reads pricing.json (providers/e2e.py): each GPU row's USD on-demand hourly rate, one GPU per listing
("On-demand prices are per GPU-hour"), listing id "<id>:1x", raw name "<model> <vramGB>GB". Captures of the feed
(from Sep 2026) go through E2EProvider.normalize unchanged.

The page (Oct 2025 on, as the Archive's crawler sees it) prints one "NVIDIA GPU" table under the "1 x | 2 x | 4 x |
8 x" size buttons, the 1x tab rendered: Item (or GPU) | vRAM | vCPUs | RAM, GB | Price per Hour (or Hourly/On-Demand)
| monthly / 6-month / yearly columns, one row per GPU, "$3.49" per GPU-hour, and under it "Prices are per GPU-hour"
(the line is required). Read: the hourly column only, by its header; the term columns are commitments and never read.
Each row becomes the live feed's listing: id from the model ("NVIDIA A100" + 80 -> "a100-80gb:1x", the feed's own
ids) and raw name "NVIDIA A100 80GB", so the archived and live series join and map alike.

www.e2enetworks.com/us/pricing (Dec 2023 - Jul 2025, "Pricing - In $USD", required): E2E's international price list,
one block per product, rows "<plan> [GPU memory] <vCPUs> <RAM> <disk> $x/hr ..." for the WHOLE plan, "exclusive of 18%
GST" (as the feed). Read by e2e_us_plans: Linux plans only (GDC. / GDC3.; GDCW. is Windows, licence included) whose
plan code spells count and GPU ("GDC3.2xA10080-32.230GB" = 2 x A100 80GB, "GDC.A30-16.90GB" = 1 x A30) or the
Dec 2024 on "1xH200" / "4×H100" rows, with a USD "$x/hr". Rows in "Spot Instances" tables, INR (₹) rows, codes with
a tail the pattern does not know (the page has copy-paste codes such as "GDC3.2xL4-50.220GB.A30-32.90GB"), V100 / T4
/ RTX 8000 (memory not in the code) and rows whose own GPU-memory cell contradicts the code are skipped. Listing id
"<plan>" (the "N×GPU" rows: "<feed id>:<N>x"), raw name the feed's ("NVIDIA A100 80GB"), so the GPU series join.

Not read: the /pricing page before Oct 2025 (2019 - Jun 2025) prints INR (₹) only. The live poller reads E2E's separately
published USD list and converts nothing, so an INR page gives no row (reported, never converted). The 2x/4x/8x tabs
are not in the captured HTML.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from types import SimpleNamespace

from backfill.parsers import dec, listing, slug, text
from backfill.sources import Source, own_page
from models import ComputeListing
from providers.e2e import ENDPOINT, E2EProvider

_SECTION = "per-hour pricing for all NVIDIA GPU configurations"
_PER_GPU = re.compile(r"(?:On-demand p|P)rices are per GPU-hour")
_TABLE = re.compile(r"<table[^>]*>(?P<t>.*?)</table>", re.S)
_TR = re.compile(r"<tr[^>]*>(?P<r>.*?)</tr>", re.S)
_CELL = re.compile(r"<t[hd][^>]*>(?P<v>.*?)</t[hd]>", re.S)
_HOURLY = ("Price per Hour", "Hourly/On-Demand")
_MODEL = re.compile(r"^NVIDIA [A-Z0-9][A-Za-z0-9 ]*$")


def e2e_pricing_json(body: bytes, at: datetime) -> list[ComputeListing]:
    """A capture of pricing.json, read by the live normalizer."""
    d = json.loads(body)
    if not isinstance(d, dict) or not isinstance(d.get("gpuInstances"), list):
        raise ValueError("e2e(archive): pricing.json has no gpuInstances list")
    payload = {k: d.get(k) for k in ("generatedAt", "notes", "gpuInstances")}
    return E2EProvider.normalize({ENDPOINT: [SimpleNamespace(endpoint=ENDPOINT, payload=payload, fetched_at=at, ok=True)]})


# the page's own spelling of a feed model (some 2026 captures print "NVIDIA RTXPRO6000")
SPELLING = {"NVIDIA RTXPRO6000": "NVIDIA RTX PRO 6000"}


def live_id(model: str, vram: int) -> str:
    """The feed's id for a page row: "NVIDIA H200" -> "h200", "NVIDIA A100" + 40 -> "a100-40gb" (the only model the
    feed lists in two memory sizes), "NVIDIA RTX PRO 6000" -> "rtx-pro-6000"."""
    base = slug(model.removeprefix("NVIDIA "))
    return f"{base}-{vram}gb" if base == "a100" else base


def e2e_gpu_table(page: str, at: datetime) -> list[ComputeListing]:
    """Oct 2025 on: the "NVIDIA GPU" table, its hourly USD column, per GPU-hour."""
    start = page.find(_SECTION)
    if start == -1:
        if "₹" in page:
            raise ValueError("e2e(archive): the page prints INR only; the live poller reads the USD list, nothing converted")
        raise ValueError("e2e(archive): no 'NVIDIA GPU' price table")
    tm = _TABLE.search(page, start)
    if tm is None:
        raise ValueError("e2e(archive): no table after the NVIDIA GPU heading")
    if not _PER_GPU.search(text(page[tm.end(): tm.end() + 3000])):
        raise ValueError("e2e(archive): the table is no longer stated 'per GPU-hour'")
    rows = [[text(c["v"]) for c in _CELL.finditer(r["r"])] for r in _TR.finditer(tm["t"])]
    head = rows[0] if rows else []
    hourly = next((h for h in _HOURLY if h in head), None)
    if not head or head[0] not in ("Item", "GPU") or hourly is None or head[1:4] != ["vRAM", "vCPUs", "RAM, GB"]:
        raise ValueError(f"e2e(archive): GPU table header {head}")
    pi = head.index(hourly)
    out = []
    for cells in rows[1:]:
        if len(cells) != len(head):
            raise ValueError(f"e2e(archive): row {cells[:1]} has {len(cells)} cells, header {len(head)}")
        model, vram, price = SPELLING.get(cells[0], cells[0]), cells[1], cells[pi]
        if not re.search(r"\d", price) or re.search(r"\s-\s*Coming soon$", model, re.I):
            continue  # "Contact Sales": no price; "NVIDIA B200 - Coming soon" (Feb 2026): priced but not rentable yet
        if not _MODEL.match(model) or not vram.isdigit():
            raise ValueError(f"e2e(archive): unreadable GPU row {cells[:2]}")
        if not price.startswith("$"):
            raise ValueError(f"e2e(archive): {model} hourly price {price!r} is not in USD")
        p = dec(price)
        if p is None:
            raise ValueError(f"e2e(archive): {model} hourly price {price!r} is not '$x'")
        out.append(listing("e2e", sku=live_id(model, int(vram)), listing_id=f"{live_id(model, int(vram))}:1x",
                           raw=f"{model} {vram}GB", count=1, instance_price=p, at=at, tier="vm",
                           vcpu=int(cells[2]) if cells[2].isdigit() else None,
                           ram_gb=float(cells[3]) if cells[3].isdigit() else None))
    if not out:
        raise ValueError("e2e(archive): the GPU table has no priced row")
    return out


# plan-code GPU -> (feed model, VRAM GB)
_CODE_GPU = {"H10080": ("NVIDIA H100", 80), "A10080": ("NVIDIA A100", 80), "A100": ("NVIDIA A100", 40),
             "A40": ("NVIDIA A40", 48), "A30": ("NVIDIA A30", 24), "L40S": ("NVIDIA L40S", 48), "L4": ("NVIDIA L4", 24)}
_NX_GPU = {"H100": ("NVIDIA H100", 80), "H200": ("NVIDIA H200", 141)}
_US_ROW = re.compile(
    r"(?P<plan>GDCW?3?\.(?:(?P<n>\d)[xX×])?(?P<code>H10080|A10080|A100|A40|A30|L40S|L4|V100|T4|RTX)[-.\w]*"
    r"|(?P<nn>\d)[x×](?P<nx>H100|H200)) (?P<mid>[^$₹]{0,90}?)(?P<cur>[$₹])(?P<p>\d[\d,]*(?:\.\d+)?)/hr")
_STRICT = re.compile(r"^GDC3?\.(?:\d[x×])?(?:H10080|A10080|A100|A40|A30|L40S|L4)-\d+\.\d+GB(?:_SXM|_HGX)?$")
_US_SECTION = re.compile(r"Spot Instances|GPU Dedicated Compute")
_MEM_EACH = re.compile(r"^(\d+) x (\d+) GB\b")
_MEM_TOTAL = re.compile(r"^(\d+) GB \d+ vCPUs")
_VCPUS = re.compile(r"(\d+) vCPUs")


def e2e_us_plans(page: str, at: datetime) -> list[ComputeListing]:
    t = text(re.sub(r"<(script|style)\b.*?</\1>", " ", page, flags=re.S))
    if "Pricing - In $USD" not in t:
        raise ValueError("e2e(archive): /us/pricing no longer says 'Pricing - In $USD'")
    spots = [(m.start(), m.group(0) == "Spot Instances") for m in _US_SECTION.finditer(t)]
    out = []
    for m in _US_ROW.finditer(t):
        in_spot = next((spot for at_, spot in reversed(spots) if at_ < m.start()), False)
        if m["cur"] != "$" or in_spot:
            continue  # an INR row on the US page, or a spot (interruptible) price
        mid = m["mid"]
        if m["nx"]:
            model, vram = _NX_GPU[m["nx"]]
            n = int(m["nn"])
            lid = f"{live_id(model, vram)}:{n}x"
            tm = _MEM_TOTAL.match(mid)
            if tm is None or int(tm.group(1)) != n * vram:
                continue  # its own memory cell does not read as N x the GPU's VRAM
        else:
            if not _STRICT.match(m["plan"]):
                continue  # Windows, V100 / T4 / RTX, or a code with an unknown tail
            model, vram = _CODE_GPU[m["code"]]
            n = int(m["n"] or 1)
            lid = m["plan"]
            em = _MEM_EACH.match(mid)
            if em and (int(em.group(1)), int(em.group(2))) != (n, vram):
                continue  # the row's GPU memory names another shape (page copy-paste)
        price = dec(m["p"])
        if price is None or price <= 0:
            continue
        vc = _VCPUS.search(mid)
        out.append(listing("e2e", sku=m["plan"], listing_id=lid, raw=f"{model} {vram}GB", count=n,
                           instance_price=price, at=at, tier="vm", vcpu=int(vc.group(1)) if vc else None))
    if not out:
        raise ValueError("e2e(archive): /us/pricing has no Linux USD plan row")
    return out


SOURCES = [
    Source("e2e", "www.e2enetworks.com/pricing.json",
           (("backfill.parsers.e2e.e2e_pricing_json (E2EProvider.normalize, live parser)", e2e_pricing_json),)),
    own_page("e2e", "www.e2enetworks.com/pricing", e2e_gpu_table),
    own_page("e2e", "www.e2enetworks.com/pricing?region=us", e2e_gpu_table),
    own_page("e2e", "www.e2enetworks.com/us/pricing", e2e_us_plans),
]
