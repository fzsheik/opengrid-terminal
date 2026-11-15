"""DeepInfra: archived captures of its two PUBLIC price pages. Container-API prices are never read (they are
INTERNAL, provider_meta.INTERNAL_SOURCES: DeepInfra's terms treat non-public pricing as confidential), and the archive
holds no capture of that API anyway.

    deepinfra.com/gpu-instances   Aug 2025 on: the live parser (providers/deepinfra.py, "On-Demand Cloud Pricing"
                                  table) reads every capture unchanged: listings "<model>:<n>x", provider_tier
                                  "container", public.
    deepinfra.com/pricing         Apr 2024 on: the "Custom LLMs" table (custom_llm_table below).

custom_llm_table: "Custom LLMs - You can deploy your own model on our hardware and pay for uptime. You get dedicated
SXM-connected GPUs", a table GPU | Price ("Nvidia A100 GPU", "$2.00/GPU-hour"; from 2026: GPU | Memory | Price, "A100"
"80GB" "$1.49 / GPU-hour"). A dedicated model deployment billed per GPU-hour by the minute, no commitment: one GPU at
the printed rate, market on_demand, provider_tier "custom-llm", listing id "<model>-<vram>gb:custom-llm". It is a
different product from the GPU-instance containers above, so it never shares their listing ids.
  * VRAM: the Memory column, or (2024 - 2025, two columns) the section's own "Dedicated A100-80GB, H100-80GB &
    H200-141GB GPUs" sentence; a GPU the page gives no VRAM for keeps "<model>" alone (unmapped).
  * SXM: named only when the section says "SXM-connected" (it always has so far); without it the form factor is not
    stated and the name stays without it.
  * Not read: the 2023 "Execution Time Pricing" ($/second of inference on an A100: a per-request price, not a GPU
    rental) and every per-token price.
"""
from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal

import canonical
from backfill.parsers import listing, slug, text
from backfill.sources import live_page, own_page
from models import ComputeListing

_SECTION = re.compile(r"Custom LLMs(?P<s>.*?)(?:Dedicated Instances and Clusters|Embeddings Pricing|$)", re.S)
_TABLE = re.compile(r"<table[^>]*>(?P<t>.*?)</table>", re.S)
_TR = re.compile(r"<tr[^>]*>(?P<r>.*?)</tr>", re.S)
_THD = re.compile(r"<t[hd][^>]*>(?P<v>.*?)</t[hd]>", re.S)
_PRICE = re.compile(r"^\$(?P<v>\d+(?:\.\d+)?)\s*/\s*GPU-hour$")
_NAME = re.compile(r"^(?:Nvidia\s+)?(?P<m>[A-Z]+\d+[A-Za-z]*)(?:\s+GPU)?$", re.I)
_MEM = re.compile(r"^(?P<gb>\d+)\s*GB$")
_SENTENCE_VRAM = re.compile(r"\b(?P<m>[A-Z]\d{3}[A-Za-z]*)-(?P<gb>\d+)GB\b")


def custom_llm_table(page: str, at: datetime) -> list[ComputeListing]:
    sec = _SECTION.search(page)
    if sec is None:
        raise ValueError("deepinfra(archive): no 'Custom LLMs' section")
    s = sec["s"]
    plain = text(re.sub(r"<(script|style)\b.*?</\1>", " ", s, flags=re.S))
    sxm = "SXM-connected" in plain
    vram_said = {m["m"]: int(m["gb"]) for m in _SENTENCE_VRAM.finditer(plain)}
    out, tables = [], 0
    for tm in _TABLE.finditer(s):
        rows = [[text(c["v"]) for c in _THD.finditer(r["r"])] for r in _TR.finditer(tm["t"])]
        if not rows or rows[0] not in (["GPU", "Price"], ["GPU", "Memory", "Price"]):
            continue
        tables += 1
        head = rows[0]
        for cells in rows[1:]:
            if len(cells) != len(head):
                raise ValueError(f"deepinfra(archive): Custom LLMs row {cells!r} under {head!r}")
            nm, pm = _NAME.match(cells[0]), _PRICE.match(cells[-1])
            if nm is None or pm is None:
                raise ValueError(f"deepinfra(archive): Custom LLMs row {cells!r} is not '<GPU> ... $x/GPU-hour'")
            model = nm["m"].upper()
            if len(head) == 3:
                mm = _MEM.match(cells[1])
                if mm is None:
                    raise ValueError(f"deepinfra(archive): {model} memory {cells[1]!r}")
                vram = int(mm["gb"])
            else:
                vram = vram_said.get(model)
            raw = " ".join(x for x in ("NVIDIA", model, "SXM" if sxm else "", f"{vram}GB" if vram else "") if x)
            lid = f"{slug(model)}-{vram}gb:custom-llm" if vram else f"{slug(model)}:custom-llm"
            o = listing("deepinfra", sku=f"{model} {vram}GB custom LLM" if vram else f"{model} custom LLM",
                        listing_id=lid, raw=raw, count=1, instance_price=Decimal(pm["v"]), at=at, tier="custom-llm")
            if o.canonical_gpu_name is None and sxm and vram:
                # canonical.py knows some SXM-only parts by "<model> <vram>GB" alone (B200): take it only when the
                # name it gives is the SXM part the page states
                alt = canonical.canonical_gpu_name(f"NVIDIA {model} {vram}GB")
                if alt and "SXM" in alt and f"{vram}GB" in alt:
                    o = o.model_copy(update={"canonical_gpu_name": alt})
            out.append(o)
    if not tables:
        raise ValueError("deepinfra(archive): no 'GPU | (Memory |) Price' table under Custom LLMs")
    if not out:
        raise ValueError("deepinfra(archive): the Custom LLMs table has no priced GPU")
    return out


SOURCES = [live_page("deepinfra", "deepinfra", "deepinfra.com/gpu-instances"),
           own_page("deepinfra", "deepinfra.com/pricing", custom_llm_table)]
