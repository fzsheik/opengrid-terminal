"""CoreWeave: archived designs of www.coreweave.com/pricing the live parser (providers/coreweave.py) rejects, and
the older a la carte pages (www.coreweave.com/gpu-cloud-pricing, Aug 2022 - May 2024; docs.coreweave.com's
resource-based pricing, 2021 - 2023).

A la carte (2021 - 2024): CoreWeave priced a GPU instance as a GPU component plus vCPUs ($0.01/hr each) plus RAM
($0.005/GB/hr), one GPU at a time. The pages print the GPU COMPONENT per GPU-hour ("GPU Component Cost Per Hour"),
which is what is stored: one GPU, listing id "<model>:gpu-component", provider_tier "a-la-carte". It is not an
instance price: a valid instance adds at least 1 vCPU and 2 GB RAM ($0.02/hr), a typical one far more (up to
48 vCPU + 256 GB per GPU, +$1.76/hr). So it is a lower bound on what a CoreWeave GPU cost then, and is never
the same listing as today's whole-instance rows.

Model names are the page's; COMPONENT_CANONICAL gives the canonical GPU only where the name states the variant
(CoreWeave's "NVLINK" A100 / V100 are the HGX SXM boards). "Quadro RTX 4000/5000" and the like stay unmapped.
"""
from __future__ import annotations

import re
from datetime import datetime

from backfill.parsers import dec, listing, slug, text
from backfill.sources import live_page, own_page
from models import ComputeListing

# --------------------------------------------------------------------------------------------------------------
# CoreWeave, designs before the regional tables (Nov 2025 - Feb 2026): one table, instance price only, no spot.
# --------------------------------------------------------------------------------------------------------------

_CW_SECTION = re.compile(r">On-demand GPU instances</h[24]>(?P<body>.*?)>On-demand CPU instances</h[24]>", re.S)
_CW_META = re.compile(r'class="table-meta-value[^"]*"[^>]*>(?P<v>[^<]*)</div>\s*<div[^>]*>(?P<label>[^<]+)</div>')
# "$49.24 / Hour" in one div (Dec 2025 - Feb 2026) or "$49.24</div><div>/ Hour" (Nov 2025).
_CW_PRICE = re.compile(r'class="table-meta-value[^"]*"[^>]*>\s*\$(?P<v>[\d,]+(?:\.\d+)?)\s*(?:</div>\s*<div>)?/\s*Hour')
_CW_MODEL = re.compile(r'<h3[^>]*class="table-model-name"[^>]*>(?P<m>[^<]+)</h3>')


def coreweave_single_table(page: str, at: datetime) -> list[ComputeListing]:
    from providers.coreweave import _COUNT, raw_name

    sec = _CW_SECTION.search(page)
    if sec is None:
        raise ValueError("coreweave(archive): 'On-demand GPU instances' section not found")
    out = []
    for row in re.split(r'role="listitem"', sec["body"])[1:]:
        models = {m["m"].strip() for m in _CW_MODEL.finditer(row)}
        meta = {m["label"].strip(): m["v"].strip() for m in _CW_META.finditer(row)}
        if "GPU Count" not in meta:
            continue  # a managed service row (SUNK, ...), not an instance shape
        prices = {p["v"] for p in _CW_PRICE.finditer(row)}
        if len(models) != 1 or len(prices) > 1:
            raise ValueError("coreweave(archive): a GPU row has several models or prices")
        if not prices:
            continue  # Contact sales
        cm = _COUNT.match(meta["GPU Count"])
        if cm is None or int(cm.group(1)) == 0:
            continue
        model, count, price = models.pop(), int(cm.group(1)), dec(prices.pop())
        vram = meta.get("VRAM")
        storage = dec(meta.get("Local Storage (TB)"))
        out.append(listing(
            "coreweave", sku=f"{model}:{count}x", listing_id=f"{slug(model)}:{count}x", raw=raw_name(model, vram),
            count=count, instance_price=price, at=at,
            vcpu=int(meta["vCPUs"]) if meta.get("vCPUs", "").isdigit() else None,
            ram_gb=float(meta["System RAM"].replace(",", "")) if dec(meta.get("System RAM")) else None,
            storage_gb=float(storage) * 1024 if storage else None))
    if not out:
        raise ValueError("coreweave(archive): no priced GPU rows")
    return out



# --------------------------------------------------------------------------------------------------------------
# A la carte GPU component prices (2021 - 2024).
# --------------------------------------------------------------------------------------------------------------

# (page model, VRAM GB) -> canonical GPU, where the page's name makes the product unambiguous.
COMPONENT_CANONICAL = {
    ("NVIDIA HGX H100", 80): "NVIDIA H100 80GB SXM5",
    ("NVIDIA H100 PCIe", 80): "NVIDIA H100 80GB PCIe",
    ("A100 80GB NVLINK", 80): "NVIDIA A100 80GB SXM4",
    ("A100 80GB PCIe", 80): "NVIDIA A100 80GB PCIe",
    ("A100 40GB NVLINK", 40): "NVIDIA A100 40GB SXM4",
    ("A100 40GB PCIe", 40): "NVIDIA A100 40GB PCIe",
    ("A40", 48): "NVIDIA A40 48GB",
    ("RTX A6000", 48): "NVIDIA RTX A6000 48GB",
    ("RTX A5000", 24): "NVIDIA RTX A5000 24GB",
    ("RTX A4000", 16): "NVIDIA RTX A4000 16GB",
    ("Tesla V100 NVLINK", 16): "NVIDIA Tesla V100 16GB SXM2",
    ("NVIDIA L40S", 48): "NVIDIA L40S 48GB",
    ("NVIDIA L40", 48): "NVIDIA L40 48GB",
    ("NVIDIA GH200", 96): "NVIDIA GH200 96GB",
}

COMPONENT_HEADER = ["GPU Model", "VRAM (GB)", "Max vCPUs per GPU ($0.01/hr)", "Max RAM (GB) per GPU ($0.005/hr)",
                    "GPU Component Cost Per Hour"]
_DESKTOP = re.compile(r'<div class="desktop">\s*<div class="table">(?P<t>.*?)(?=<div class="(?:mobile|desktop)"|$)', re.S)
_WCOL = re.compile(r'<div class="[^"]*\bw-col\b[^"]*">')
_BODY_ROW = re.compile(r'<div class="table-body-row[^"]*">')
_DOLLAR = re.compile(r"^\$(?P<v>\d+(?:\.\d+)?)(?:\s*Reserve Now)?$")


def _component(model: str, vram: str, price: str, at: datetime):
    m = _DOLLAR.match(price)
    if m is None:
        if re.search(r"\d", price):
            raise ValueError(f"coreweave(a la carte): {model} price cell {price!r} is not '$x'")
        return None  # "Reserve Now" alone: no on-demand price
    if not vram.isdigit():
        raise ValueError(f"coreweave(a la carte): {model} VRAM {vram!r}")
    o = listing("coreweave", sku=f"{model}:gpu-component", listing_id=f"{slug(model)}:gpu-component",
                raw=f"{model} {vram}GB", count=1, instance_price=dec(m["v"]), at=at, tier="a-la-carte")
    return o.model_copy(update={"canonical_gpu_name": COMPONENT_CANONICAL.get((model, int(vram)))})


def coreweave_component_table(page: str, at: datetime) -> list[ComputeListing]:
    """www.coreweave.com/gpu-cloud-pricing: the desktop table GPU Model | VRAM (GB) | Max vCPUs | Max RAM |
    GPU Component Cost Per Hour (the page's mobile cards repeat it, sometimes with different numbers: not read)."""
    out, tables = [], 0
    for tm in _DESKTOP.finditer(page):
        t = tm["t"]
        head_at = t.find('class="table-header"')
        body_at = t.find('class="table-body"')
        if head_at == -1 or body_at == -1:
            continue
        head = [text(c) for c in _WCOL.split(t[head_at:t.rfind("<div", 0, body_at)])[1:]]
        head = [h.replace("( $", "($").replace(" /hr", "/hr").replace("( ", "(") for h in head]
        if not head or head[0] != "GPU Model":
            continue
        if head != COMPONENT_HEADER:
            raise ValueError(f"coreweave(a la carte): GPU table header {head!r}")
        tables += 1
        for row in _BODY_ROW.split(t[body_at:])[1:]:
            cells = [text(c) for c in _WCOL.split(row)[1:]]
            if len(cells) != len(head):
                raise ValueError(f"coreweave(a la carte): row {cells!r} has {len(cells)} cells")
            o = _component(cells[0], cells[1], cells[4], at)
            if o is not None:
                out.append(o)
    if not tables:
        raise ValueError("coreweave(a la carte): no 'GPU Model ... GPU Component Cost Per Hour' table")
    if not out:
        raise ValueError("coreweave(a la carte): no priced GPU row")
    return out


# docs.coreweave.com resource labels -> (the gpu-cloud-pricing page's model name, VRAM GB), so a product priced on
# both pages is one listing id. Labels are compared without spaces ("H100_NVLINK _ 80GB").
LABEL_TO_MODEL = {
    "H100_NVLINK_80GB": ("NVIDIA HGX H100", 80), "H100_PCIE": ("NVIDIA H100 PCIe", 80),
    "A100_NVLINK_80GB": ("A100 80GB NVLINK", 80), "A100_NVLINK": ("A100 40GB NVLINK", 40),
    "A100_PCIE_80GB": ("A100 80GB PCIe", 80), "A100_PCIE_40GB": ("A100 40GB PCIe", 40),
    "Tesla_V100_NVLINK": ("Tesla V100 NVLINK", 16), "Tesla_V100": ("Tesla V100 PCIe", 16),
    "Tesla_V100_PCIE": ("Tesla V100 PCIe", 16), "A40": ("A40", 48), "RTX_A6000": ("RTX A6000", 48),
    "RTX_A5000": ("RTX A5000", 24), "RTX_A4000": ("RTX A4000", 16), "Quadro_RTX_6000": ("Quadro RTX 6000", 24),
    "Quadro_RTX_5000": ("Quadro RTX 5000", 16), "Quadro_RTX_4000": ("Quadro RTX 4000", 8),
    "Tesla_P100_NVLINK": ("Tesla P100 NVLINK", 16),
}
COMPONENT_CANONICAL[("Tesla V100 PCIe", 16)] = "NVIDIA Tesla V100 16GB PCIe"
COMPONENT_CANONICAL[("Quadro RTX 6000", 24)] = "NVIDIA Quadro RTX 6000 24GB"
_DOC_ROW = re.compile(r"GPU (?P<desc>NVIDIA .+?) (?P<label>[A-Za-z0-9]+(?:(?:_| _ )[A-Za-z0-9]+)*) "
                      r"\$(?P<p>\d+(?:\.\d+)?)(?= )")


def _doc_component(label: str, desc: str, price: str, at: datetime):
    key = label.replace(" ", "")
    if key not in LABEL_TO_MODEL:
        raise ValueError(f"coreweave(docs): unknown GPU resource label {label!r} ({desc})")
    model, vram = LABEL_TO_MODEL[key]
    return _component(model, str(vram), f"${price}", at)


def coreweave_docs_resource_pricing(page: str, at: datetime) -> list[ComputeListing]:
    """docs.coreweave.com/resources/resource-based-pricing (2021 - 2023): "GPU Instance Resource Pricing", rows
    Type | Description | (Resource) Label | Cost per Hour | VRAM; GPU rows are the GPU component per GPU-hour (the
    same a la carte price as www.coreweave.com/gpu-cloud-pricing, stored the same way); from May 2023 the GPU table
    drops the Type column (Description | Label | Cost per hour | VRAM). 2021 and 2023 print HTML tables; 2022 renders
    the same rows without one, read from the text in the same order (Type, Description,
    Label, $ cost). The resource label decides the product (LABEL_TO_MODEL); an unknown label raises."""
    out, seen = [], False
    for tm in re.finditer(r"<table[^>]*>(?P<t>.*?)</table>", page, re.S):
        rows = [[text(c) for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", r, re.S)]
                for r in re.findall(r"<tr[^>]*>(.*?)</tr>", tm["t"], re.S)]
        head = [h.lower() for h in rows[0]] if rows else []
        label_i = next((i for i, h in enumerate(head) if h in ("label", "resource label")), None)
        cost_i = head.index("cost per hour") if "cost per hour" in head else None
        if "description" not in head or label_i is None or cost_i is None:
            continue   # CPU / storage tables are headed "CPU Type" / "Node type", not "Description"
        type_i, desc_i = (head.index("type") if "type" in head else None), head.index("description")
        seen = True
        for cells in rows[1:]:
            if len(cells) != len(head):
                raise ValueError(f"coreweave(docs): row {cells!r} under {rows[0]!r}")
            if type_i is not None and cells[type_i] != "GPU":
                continue  # CPU / RAM rows of the 2021-22 combined table
            if not cells[cost_i].startswith("$"):
                raise ValueError(f"coreweave(docs): GPU row {cells!r}")
            o = _doc_component(cells[label_i], cells[desc_i], cells[cost_i][1:], at)
            if o is not None:
                out.append(o)
    if not seen:
        t = text(re.sub(r"<(script|style)\b.*?</\1>", " ", page, flags=re.S))
        i = t.find("Type Description Resource Label Cost per Hour")
        if i == -1:
            i = t.find("Type Description Label Cost per Hour")
        if i != -1:
            seen = True
            section = t[i: t.find("An example", i) if t.find("An example", i) != -1 else i + 4000]
            for m in _DOC_ROW.finditer(section):
                o = _doc_component(m["label"], m["desc"], m["p"], at)
                if o is not None:
                    out.append(o)
    if not seen:
        raise ValueError("coreweave(docs): no 'Type | Description | Label | Cost per Hour' GPU resource table")
    if not out:
        raise ValueError("coreweave(docs): no priced GPU resource")
    return out


SOURCES = [live_page("coreweave", "coreweave", "www.coreweave.com/pricing", coreweave_single_table),
           own_page("coreweave", "docs.coreweave.com/resources/resource-based-pricing", coreweave_docs_resource_pricing),
           own_page("coreweave", "www.coreweave.com/gpu-cloud-pricing", coreweave_component_table)]
