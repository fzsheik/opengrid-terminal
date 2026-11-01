"""Hugging Face Inference Endpoints: archived captures of its price tables, huggingface.co/docs/inference-endpoints/
pricing (Nov 2022 on) and huggingface.co/pricing (the Inference Endpoints section).

Live, OpenGrid reads the endpoints catalogue API (providers/huggingface.py on branch providers/batch-nokey): resold
AWS / GCP / Azure capacity, provider_tier "resold:<vendor>", raw name "<vendor> <architecture> <GB per GPU>GB"
("aws Nvidia A100 80GB"), never voting in market math. Archived rows keep exactly that tier and raw-name form.

The pages print GPU tables headed Provider | [Instance Type |] Instance Size (or no size) | Hourly rate | GPUs |
(GPU) Memory | Architecture, in some order: "aws | nvidia-a100 | x2 | $8 | 2 | 160 GB | NVIDIA A100". Memory is the
instance's total GPU memory as the page prints it; HF prints usable memory for some cards (T4 "14GB"), so the
per-GPU figure for a model whose memory has one size is the card's (T4 16, A10G 24, L4 24, L40S 48), as the API
reports it. The listing id is "<vendor>-<instance type>-<size>" ("aws-nvidia-t4-x1"; huggingface.co/pricing, which names no
instance type, gives the same id from its architecture and GPU count; 2022 - 23's named sizes give
"<vendor>-<size>-<architecture>"):
the page names no region, so it is not the live per-region compute id. CPU and Inferentia tables are not read.
In 2025 further sizes of a card leave Provider and Architecture blank (read from the row above).
A 2026 layout of huggingface.co/pricing merges the vendor into the first cell ("NVIDIA T4 aws") and continuation
rows: not read (reported).
"""
from __future__ import annotations

import re
from datetime import datetime

from backfill.parsers import dec, listing, slug, text
from backfill.pending_names import canonical_name
from backfill.sources import own_page
from models import ComputeListing

_TABLE = re.compile(r"<table[^>]*>(?P<t>.*?)</table>", re.S)
_TR = re.compile(r"<tr[^>]*>(?P<r>.*?)</tr>", re.S)
_CELL = re.compile(r"<t[hd][^>]*>(?P<c>.*?)</t[hd]>", re.S)
_GB = re.compile(r"^(?P<v>\d+(?:\.\d+)?)\s*GB$")
_RATE = re.compile(r"^\$(?P<v>\d+(?:\.\d+)?)$")
ONE_SIZE_GB = {"T4": 16, "A10G": 24, "L4": 24, "L40S": 48}
VENDORS = ("aws", "gcp", "azure")


def _raw(vendor: str, arch: str, total_gb: float, n: int) -> str:
    model = re.sub(r"^NVIDIA\s+", "", arch, flags=re.I)
    per = ONE_SIZE_GB.get(model, total_gb / n)
    per_s = int(per) if float(per).is_integer() else per
    return f"{vendor} Nvidia {model} {per_s}GB"


def hf_gpu_tables(page: str, at: datetime) -> list[ComputeListing]:
    out, tables = [], 0
    for tm in _TABLE.finditer(page):
        rows = [[text(c) for c in _CELL.findall(r)] for r in _TR.findall(tm["t"])]
        if not rows:
            continue
        head = [h.lower() for h in rows[0]]
        if "provider" not in head or "gpus" not in head or "architecture" not in head:
            continue
        mem = next((i for i, h in enumerate(head) if h in ("memory", "gpu memory")), None)
        rate = next((i for i, h in enumerate(head) if h == "hourly rate"), None)
        if mem is None or rate is None:
            raise ValueError(f"huggingface(archive): GPU table header {rows[0]}")
        typ = head.index("instance type") if "instance type" in head else None
        size = head.index("instance size") if "instance size" in head else None
        tables += 1
        prev = None
        for cells in rows[1:]:
            if len(cells) != len(head):
                raise ValueError(f"huggingface(archive): row {cells} under {rows[0]}")
            c = dict(zip(head, cells))
            if prev is not None and c["provider"] == "" and c["architecture"] == "":
                # 2025: further sizes of the same card leave Provider and Architecture blank under the first row
                c["provider"], c["architecture"] = prev["provider"], prev["architecture"]
                if typ is not None and cells[typ] == "":
                    cells[typ] = prev_cells[typ]
            prev, prev_cells = c, cells
            vendor = c["provider"].lower()
            if vendor not in VENDORS:
                raise ValueError(f"huggingface(archive): provider {c['provider']!r}")
            g, m, r = c["gpus"], _GB.match(cells[mem]), _RATE.match(cells[rate])
            if not g.isdigit() or m is None:
                raise ValueError(f"huggingface(archive): row {cells} lacks GPUs / memory")
            if r is None:
                if re.search(r"\d", cells[rate]):
                    raise ValueError(f"huggingface(archive): rate {cells[rate]!r}")
                continue
            n, arch = int(g), c["architecture"]
            raw = _raw(vendor, arch, float(m["v"]), n)
            if typ is not None:
                lid = f"{vendor}-{cells[typ]}-{cells[size] if size is not None else n}"
            else:
                lid = f"{vendor}-{cells[size]}-{slug(arch)}" if size is not None else f"{vendor}-{slug(arch)}-x{n}"
            o = listing("huggingface", sku=lid, listing_id=lid, raw=raw, count=n, instance_price=dec(r["v"]), at=at,
                        tier=f"resold:{vendor}"[:24])
            out.append(o.model_copy(update={"canonical_gpu_name": canonical_name(raw)}))
    if not tables:
        raise ValueError("huggingface(archive): no 'Provider | ... | GPUs | ... | Architecture' table")
    if not out:
        raise ValueError("huggingface(archive): GPU tables have no priced row")
    return out


SOURCES = [own_page("huggingface", "huggingface.co/docs/inference-endpoints/pricing", hf_gpu_tables),
           own_page("huggingface", "huggingface.co/pricing", hf_gpu_tables)]
