"""Akamai Cloud (Linode): archived captures of GET api.linode.com/v4/linode/types, and of www.linode.com/pricing/.

The archived request has no page_size, but both captures say page 1 of 1 (every type on one page), so the body is
the same catalogue the live poller reads (providers/linode.py, page_size=500). It goes through the live path
unchanged: GPU types only (gpus > 0), then LinodeProvider.normalize.

The API was captured only 14 times (2020 - 2026), so the pricing page fills the gaps. Its GPU tables name no type id,
so a row is matched to one of PLANS, taken verbatim from Linode's own archived /linode/types responses (label, id,
GPUs, vCPUs, memory), and kept only when the page's GPU count, CPUs and RAM all agree with it:

    linode_pricing_page   o-table (2020 - 22): "GPU Plans ... with RTX 6000 GPUs", columns RAM | CPU | Storage | GPU
                          Cards | ... | Price, price "$1000 / mo ($1.5 / hr)": the hourly figure in brackets is read
                          (Aug 2021 - Jun 2022: separate Monthly | Hourly columns; Hourly is read).
                          c-table (2023 on): Plan | $/Mo | $/Hr | RAM | [GPUs | CPUs] ..., the plan named
                          ("Dedicated 32 GB + RTX6000 GPU x1", "RTX4000 Ada GPU x1 Small"); $/Hr is read. From Sep
                          2025 the same columns carry no col-- classes and are named by their header text.
    linode_gpu_cards      Jun 2019 - Jan 2020: one card per plan under "GPU Plans": the plan label, "$1000/mo",
                          "($1.5/hr)" in an h6.hourly, and a spec table ("1x RTX6000 GPU", "32 GB RAM", "8 CPU Cores").
Monthly prices are never read. Listing ids, raw GPU names (providers.linode.gpu_name of the API label) and tier are
the live ones; the page's price is the plan's base price (the live "no region" listing).
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

from backfill.parsers import listing, text
from backfill.sources import Source
from models import ComputeListing
from providers.linode import ENDPOINT, LinodeProvider, gpu_name


def linode_types(body: bytes, at: datetime) -> list[ComputeListing]:
    d = json.loads(body)
    if not isinstance(d, dict) or not isinstance(d.get("data"), list):
        raise ValueError("linode(archive): response has no data list")
    if (d.get("pages") or 1) != 1:
        raise ValueError(f"linode(archive): page {d.get('page')} of {d.get('pages')}: a partial catalogue")
    payload = {**{k: v for k, v in d.items() if k != "data"}, "data": [t for t in d["data"] if t.get("gpus")]}
    return LinodeProvider.normalize({ENDPOINT: [SimpleNamespace(endpoint=ENDPOINT, payload=payload, fetched_at=at, ok=True)]})


# API label -> (type id, GPUs, vCPUs, memory GB), from the archived /v4/linode/types captures (2020 - 2026).
PLANS: dict[str, tuple[str, int, int, int]] = {
    "Dedicated 32GB + RTX6000 GPU x1": ("g1-gpu-rtx6000-1", 1, 8, 32),
    "Dedicated 64GB + RTX6000 GPU x2": ("g1-gpu-rtx6000-2", 2, 16, 64),
    "Dedicated 96GB + RTX6000 GPU x3": ("g1-gpu-rtx6000-3", 3, 20, 96),
    "Dedicated 128GB + RTX6000 GPU x4": ("g1-gpu-rtx6000-4", 4, 24, 128),
    "RTX4000 Ada x1 Small": ("g2-gpu-rtx4000a1-s", 1, 4, 16),
    "RTX4000 Ada x1 Medium": ("g2-gpu-rtx4000a1-m", 1, 8, 32),
    "RTX4000 Ada x1 Large": ("g2-gpu-rtx4000a1-l", 1, 16, 64),
    "RTX4000 Ada x1 X-Large": ("g2-gpu-rtx4000a1-xl", 1, 32, 128),
    "RTX4000 Ada x2 Small": ("g2-gpu-rtx4000a2-s", 2, 8, 32),
    "RTX4000 Ada x2 Medium": ("g2-gpu-rtx4000a2-m", 2, 16, 64),
    "RTX4000 Ada x2 Medium High Storage": ("g2-gpu-rtx4000a2-hs", 2, 16, 64),
    "RTX4000 Ada x4 Small": ("g2-gpu-rtx4000a4-s", 4, 32, 128),
    "RTX4000 Ada x4 Medium": ("g2-gpu-rtx4000a4-m", 4, 48, 196),
}
_RTX6000_BY_GPUS = {n: label for label, (_, n, _, _) in PLANS.items() if "RTX6000" in label}

_TABLE = re.compile(r"<table\b.*?</table>", re.S)
_TR = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.S)
_CELL = re.compile(r"<t[hd]\b([^>]*)>(.*?)</t[hd]>", re.S)
_COL_CLASS = re.compile(r'class="[^"]*\bcol--([a-z0-9-]+)')
# Sep 2025 on the tables carry no col-- classes: the header row's text names the columns instead.
_HEADER_KEYS = {"Plan": "plan", "$/Mo": "mo", "$/Hr": "hr", "RAM": "ram", "GPUs": "gpus", "CPUs": "cpus"}
_HOURLY_IN_BRACKETS = re.compile(r"^\$[\d,]+(?:\.\d+)? / mo \(\$(\d+(?:\.\d+)?) / hr\)$")


def _label(printed: str) -> str:
    """'Dedicated 32 GB + RTX6000 GPU x1' -> 'Dedicated 32GB + RTX6000 GPU x1'; 'RTX4000 Ada GPU x1 Small' ->
    'RTX4000 Ada x1 Small' (the API's spelling)."""
    return re.sub(r"(\d+) GB\b", r"\1GB", printed).replace("Ada GPU x", "Ada x")


def _int(cell: str) -> int | None:
    m = re.fullmatch(r"(\d+)(?: Cores)?", cell)
    return int(m.group(1)) if m else None


def _gb(cell: str) -> int | None:
    m = re.fullmatch(r"([\d,]+) GB", cell)
    return int(m.group(1).replace(",", "")) if m else None


def linode_pricing_page(page: str, at: datetime) -> list[ComputeListing]:
    out: dict[str, ComputeListing] = {}
    gpu_tables = 0
    for t in _TABLE.finditer(page):
        trs = [_CELL.findall(tr) for tr in _TR.findall(t.group(0))]
        if not trs:
            continue
        keys = [(m.group(1) if (m := _COL_CLASS.search(a)) else _HEADER_KEYS.get(text(v))) for a, v in trs[0]]
        cols = {k for k in keys if k}
        rows = [{k: text(v) for k, (_, v) in zip(keys, tr) if k} for tr in trs[1:] if len(tr) == len(keys)]
        gpus_col = "gpus" if "gpus" in cols else "gpu-cards" if "gpu-cards" in cols else None
        if gpus_col is None:
            continue  # not a GPU plan table (CPU plans, GPU backups priced per plan)
        gpu_tables += 1
        if "plan" not in cols:
            # o-table: no plan names; the section says which GPU ("... with RTX 6000 GPUs")
            if "RTX 6000 GPUs" not in text(page[max(0, t.start() - 3000):t.start()]):
                raise ValueError("linode(page): a GPU table without plan names, not under an 'RTX 6000 GPUs' heading")
        for r in rows:
            if not r:
                continue
            count, cpus, ram = _int(r.get(gpus_col, "")), _int(r.get("cpus", r.get("cpu", ""))), _gb(r.get("ram", ""))
            if "plan" in cols:
                label = _label(r.get("plan", ""))
                m = re.fullmatch(r"\$(\d+(?:\.\d+)?)", r.get("hr", "").replace(",", ""))
                hourly = Decimal(m.group(1)) if m else None
            else:
                label = _RTX6000_BY_GPUS.get(count or 0, "")
                if "hourly" in cols:   # Aug 2021 - Jun 2022: separate Monthly and Hourly columns
                    m = re.fullmatch(r"\$(\d+(?:\.\d+)?)", r.get("hourly", "").replace(",", ""))
                else:
                    m = _HOURLY_IN_BRACKETS.match(r.get("price", ""))
                hourly = Decimal(m.group(1)) if m else None
            if label not in PLANS:
                raise ValueError(f"linode(page): GPU plan {r.get('plan') or r} is not one of PLANS")
            if hourly is None:
                raise ValueError(f"linode(page): {label}: no hourly price "
                                 f"({r.get('hr') or r.get('hourly') or r.get('price')!r})")
            tid, n, vcpu, mem = PLANS[label]
            if (count, cpus, ram) != (n, vcpu, mem):
                raise ValueError(f"linode(page): {label} prints {count} GPUs / {cpus} CPUs / {ram} GB, "
                                 f"the API said {n} / {vcpu} / {mem}")
            if hourly <= 0:
                continue
            seen = out.get(tid)
            if seen is not None and seen.price_per_instance_hour != hourly:
                raise ValueError(f"linode(page): {label} printed at ${seen.price_per_instance_hour} and ${hourly}")
            out[tid] = listing("linode", sku=tid, listing_id=tid, raw=gpu_name(label), count=n, instance_price=hourly,
                               at=at, tier="vm", vcpu=vcpu, ram_gb=float(mem))
    if not gpu_tables:
        raise ValueError("linode(page): no GPU plan table (a 'GPUs' / 'GPU Cards' column)")
    return list(out.values())


_CARD = re.compile(r'<div class="gpu plan">(?P<label>.*?)</div>(?P<body>.*?)</table>', re.S)
_CARD_HOURLY = re.compile(r'<h6 class="hourly">(.*?)</h6>', re.S)
_SPEC = re.compile(r'<td class="plan-value">(.*?)</td>\s*<td class="plan-key">(.*?)</td>', re.S)


def linode_gpu_cards(page: str, at: datetime) -> list[ComputeListing]:
    out = []
    for m in _CARD.finditer(page):
        label = text(m.group("label"))
        if label not in PLANS:
            raise ValueError(f"linode(cards): GPU plan {label!r} is not one of PLANS")
        h = _CARD_HOURLY.search(m.group("body"))
        hm = re.fullmatch(r"\(\s*\$\s*(\d+(?:\.\d+)?)\s*/hr\s*\)", text(h.group(1)) if h else "")
        if hm is None:
            raise ValueError(f"linode(cards): {label}: no '($x/hr)' price")
        spec = {text(k): text(v) for v, k in _SPEC.findall(m.group("body"))}
        tid, n, vcpu, mem = PLANS[label]
        gpus = next((v for k, v in spec.items() if k.endswith("GPU")), "")
        if (gpus, spec.get("GB RAM"), spec.get("CPU Cores")) != (f"{n}x", str(mem), str(vcpu)):
            raise ValueError(f"linode(cards): {label} specs {spec} disagree with the API ({n} GPUs, {mem} GB, {vcpu} CPUs)")
        hourly = Decimal(hm.group(1))
        if hourly > 0:
            out.append(listing("linode", sku=tid, listing_id=tid, raw=gpu_name(label), count=n, instance_price=hourly,
                               at=at, tier="vm", vcpu=vcpu, ram_gb=float(mem)))
    if not out:
        raise ValueError("linode(cards): no GPU plan card")
    return out


def _page(fn):
    return f"backfill.parsers.linode.{fn.__name__}", lambda body, at: fn(body.decode("utf-8", errors="replace"), at)


SOURCES = [Source("linode", "api.linode.com/v4/linode/types",
                  (("backfill.parsers.linode.linode_types (live normalizer)", linode_types),)),
           Source("linode", "www.linode.com/pricing/", (_page(linode_pricing_page), _page(linode_gpu_cards)))]
