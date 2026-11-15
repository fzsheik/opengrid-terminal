"""Google Cloud: archived designs of the Accelerator-optimized pricing page the retired page parser (backfill/retired/gcp_page.py) rejects.

    gcp_on_demand_column   Mar - Jul 2026: the machine table's price column is headed "On-Demand (USD)" (live:
                           "Price (USD)"), family heading rows ("A4 High") are one cell wide, and until Jul the
                           section is headed "Accelerator-optimized pricing" (live: "GPU-accelerated machines
                           pricing"). Everything else is the live parser's: the us-central1 region check on both
                           sections, the attachment table (backfill.retired.gcp_page._attachable), price-cell checks, and
                           GcpProvider.normalize with its GPU_MACHINES / ATTACHABLE tables and cross-checks.

Older pages (before the accelerator-optimized page existed, 2026). Every price is Iowa (us-central1) only, as the
page parser read it, and only GPUs / machine types in the live tables (ATTACHABLE, GPU_MACHINES); ids, raw names and
tiers are the live ones ("<accelerator>:us-central1", tier gpu-attachment; "<machine type>:us-central1", hyperscaler).

    gcp_cloud_pricer       cloud.google.com/compute/pricing (#gpus, Nov 2017 - Aug 2019) and compute/gpus-pricing (Aug
                           2019 - Mar 2020): one table Model | GPUs | GPU memory | GPU price | Preemptible GPU price
                           [| commitments], each price cell a <td cloud-pricer> carrying every region's price as an
                           attribute; io-hourly="$2.48 USD per GPU" is Iowa's. The model's "1 GPU" row is read: GPU
                           price as on-demand, Preemptible GPU price as spot ("<accelerator>:us-central1:spot", as live
                           Spot). Nov 2017 - Apr 2018 the table reads Model | GPUs (dies) | GPU boards | GPU memory |
                           GPU price | Preemptible GPU (Beta) price. Commitment columns are never read. Earlier 2017
                           tables (K80 only, per die, none in Iowa) have no ATTACHABLE GPU and raise.
    gcp_gpus_per_gpu       compute/gpus-pricing, Jan 2025 on: the "Other GPU models" table, headed "GPU price (USD)
                           per GPU" (the accelerator-optimized page's attachment table says "GPU price (USD)"), prices
                           as "$0.35 / 1 hour", region picker checked to show Iowa. Virtual-workstation rows (GPU plus
                           a licence) are not ATTACHABLE models and are skipped, as live.
    gcp_vm_instance_accel  compute/vm-instance-pricing, 2025 on: the "Accelerator-optimized machine type family"
                           section, one table per family headed Machine type | GPUs | vCPUs | Memory [| Local SSD] |
                           On-demand price (USD) | commitments. A table without an on-demand column (A4: commitments
                           only) has nothing to read; a row's GPU count must match GPU_MACHINES or it is skipped.
                           Aug - Nov 2025 the section holds only A4 (commitments): nothing to read. Dec 2025 - Mar
                           2026 it carries the accelerator-optimized page's own tables (Machine type | GPU |
                           Components | On-Demand (USD)), read as gcp_on_demand_column reads them.

Between Apr 2020 and Dec 2024 both pages loaded their prices from a separate frame / script the Archive did not keep
(no price in the HTML): those captures raise and store nothing.
"""
from __future__ import annotations

import html as htmllib
import logging
import re
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

from backfill.parsers import listing, text
from backfill.sources import live_page, own_page
from models import ComputeListing

log = logging.getLogger(__name__)


def _machines_old(body: str) -> list[dict]:
    from backfill.retired import gcp_page as G

    out = []
    for table in G._TABLE.findall(body):
        rows = G._rows(table)
        head = rows[0] if rows else []
        if head[:3] != ["Machine type", "GPU", "Components"] or len(head) < 4 or head[3] != "On-Demand (USD)":
            raise ValueError(f"gcp(archive): machine table header is not the 'On-Demand (USD)' design: {head[:4]}")
        for cells in rows[1:]:
            if len(cells) == 1 or (len(cells) == len(head) and not cells[1] and not cells[2]):
                continue  # a family heading row ("A3 High")
            if len(cells) != len(head):
                raise ValueError(f"gcp(archive): machine row has {len(cells)} cells, header has {len(head)}")
            if not G._MACHINE_TYPE.match(cells[0]):
                raise ValueError(f"gcp(archive): unreadable machine type {cells[0]!r}")
            parts = dict(G._COMPONENT.findall(cells[2]))
            out.append({"machine_type": cells[0], "gpu": cells[1], "gpus": parts.get("GPUs"),
                        "vcpus": parts.get("vCPUs"), "memory": parts.get("Memory"),
                        "price": G._check_price(cells[3], cells[0])})
    if not out:
        raise ValueError("gcp(archive): no GPU machine rows found")
    return out


def gcp_on_demand_column(page: str, at: datetime) -> list[ComputeListing]:
    from backfill.retired import gcp_page as G

    start = re.compile(G._H.format("GPU-accelerated machines pricing"))
    if not start.search(page):
        start = re.compile(G._H.format("Accelerator-optimized pricing"))
    machines = G._section(page, start, G._ATTACH_HEAD, "GPU-accelerated machines pricing (archived design)")
    attach = G._section(page, G._ATTACH_HEAD, G._VWS_HEAD, "GPU Attachment pricing")
    payload = {"region": G.REGION_LABEL, "machines": _machines_old(machines), "attachable": G._attachable(attach)}
    ep = G.ENDPOINT.split("?")[0]
    return G.GcpProvider.normalize({ep: [SimpleNamespace(endpoint=ep, payload=payload, fetched_at=at)]})


REGION = "us-central1"
_TD = re.compile(r"<t([dh])\b([^>]*)>(.*?)</t\1>", re.S)
_PRICER = re.compile(r'\bio-hourly="([^"]*)"')
_PRICER_PRICE = re.compile(r"^\$(\d+(?:\.\d+)?) USD per GPU$")
_NO_PRICE = ("N/A", "Not available in this region", "")


def _model(cell: str) -> str:
    """'NVIDIA&reg; Tesla&reg; V100' -> 'NVIDIA V100' (the name the live ATTACHABLE table uses)."""
    return re.sub(r"\s+", " ", text(cell).replace("®", "").replace("Tesla ", "")).strip()


def _attachment(accel: str, price: Decimal, at: datetime, spot: bool = False) -> ComputeListing:
    return listing("gcp", sku=accel, listing_id=f"{accel}:{REGION}" + (":spot" if spot else ""), raw=accel, count=1,
                   instance_price=price, at=at, region=REGION, country="US", market="spot" if spot else "on_demand",
                   tier="gpu-attachment")


def _known(model: str, memory: str, where: str) -> str | None:
    """The live ATTACHABLE accelerator for a printed model; None when it is not one (K80, workstations, A100 ...)."""
    from backfill.retired import gcp_page as G

    spec = G.ATTACHABLE.get(model)
    if spec is None:
        return None
    accel, mem = spec
    if memory != mem:
        raise ValueError(f"gcp({where}): {model} prints memory {memory!r}, the live table says {mem!r}")
    return accel


def _pricer(attrs: str, where: str) -> Decimal | None:
    m = _PRICER.search(attrs)
    if "cloud-pricer" not in attrs or m is None:
        raise ValueError(f"gcp(cloud-pricer): {where} price cell has no io-hourly attribute")
    v = htmllib.unescape(m.group(1)).strip()
    if v in _NO_PRICE:
        return None
    p = _PRICER_PRICE.match(v)
    if p is None:
        raise ValueError(f"gcp(cloud-pricer): {where} Iowa price is not '$x USD per GPU': {v!r}")
    return Decimal(p.group(1))


def gcp_cloud_pricer(page: str, at: datetime) -> list[ComputeListing]:
    from backfill.retired import gcp_page as G

    out, tables = [], 0
    for table in G._TABLE.findall(page):
        rows = [_TD.findall(tr) for tr in G._TR.findall(table)]
        head = [text(c) for _, _, c in rows[0]] if rows else []
        if not head or head[0] not in ("Model", "GPU model") or "GPU price" not in head:
            continue
        tables += 1
        if head[:4] == ["Model", "GPUs", "GPU memory", "GPU price"]:
            n_col, mem_col, price_col = 1, 2, 3
        elif head[:5] == ["Model", "GPUs (dies)", "GPU boards", "GPU memory", "GPU price"]:
            n_col, mem_col, price_col = 1, 3, 4      # Nov 2017 - Apr 2018
        else:
            raise ValueError(f"gcp(cloud-pricer): GPU table header is not Model | GPUs | GPU memory | GPU price: {head}")
        spot_col = next((head.index(h) for h in ("Preemptible GPU price", "Preemptible GPU (Beta) price") if h in head),
                        None)
        for cells in rows[1:]:
            if len(cells) <= price_col:
                continue  # a continuation row ("2 GPUs | 32 GB HBM2") or a one-cell section label
            model, count, memory = _model(cells[0][2]), text(cells[n_col][2]), text(cells[mem_col][2])
            if len(cells) != len(head):
                raise ValueError(f"gcp(cloud-pricer): {model} row has {len(cells)} cells, header has {len(head)}")
            accel = _known(model, memory, "cloud-pricer")
            if accel is None:
                log.info("gcp(cloud-pricer): %s is not an ATTACHABLE GPU; skipped", model)
                continue
            if count != "1 GPU":
                raise ValueError(f"gcp(cloud-pricer): {model} row does not start at 1 GPU: {count!r}")
            od = _pricer(cells[price_col][1], model)
            if od is not None and od > 0:
                out.append(_attachment(accel, od, at))
            if spot_col is not None:
                sp = _pricer(cells[spot_col][1], f"{model} preemptible")
                if sp is not None and sp > 0:
                    out.append(_attachment(accel, sp, at, spot=True))
    if not tables:
        raise ValueError("gcp(cloud-pricer): no GPU price table (cloud-pricer design) on the page")
    if not out:
        raise ValueError("gcp(cloud-pricer): no ATTACHABLE GPU priced in Iowa")
    return out


def _section(page: str, title: str) -> str:
    """From the <h2> titled `title` to the next <h2>, checked to be priced for Iowa."""
    from backfill.retired import gcp_page as G

    a = re.compile(r"<h2\b[^>]*>(?:<div[^>]*>)?(?:<span[^>]*>)?\s*" + re.escape(title) + r"\s*<").search(page)
    if a is None:
        raise ValueError(f"gcp(archive): no '{title}' section")
    b = re.compile(r"<h2\b").search(page, a.end())
    body = page[a.end(): b.start() if b else len(page)]
    regions = set(G._REGION_SHOWN.findall(body)) | set(G._REGION_SELECTED.findall(body))
    if regions != {G.REGION_LABEL}:
        raise ValueError(f"gcp(archive): '{title}' is priced for {sorted(regions)}, not {G.REGION_LABEL}")
    return body


def gcp_gpus_per_gpu(page: str, at: datetime) -> list[ComputeListing]:
    from backfill.retired import gcp_page as G

    out = []
    for table in G._TABLE.findall(_section(page, "Other GPU models")):
        rows = G._rows(table)
        head = rows[0] if rows else []
        if head[:4] != ["Model", "GPUs", "GPU memory", "GPU price (USD) per GPU"]:
            raise ValueError(f"gcp(gpus-pricing): table header is not the 'per GPU' design: {head[:4]}")
        for cells in rows[1:]:
            if len(cells) == 2:
                continue  # "2 GPUs | 32 GB GDDR6"
            if len(cells) != len(head):
                raise ValueError(f"gcp(gpus-pricing): row has {len(cells)} cells, header has {len(head)}")
            if cells[1] != "1 GPU":
                raise ValueError(f"gcp(gpus-pricing): {cells[0]} row does not start at 1 GPU")
            accel = _known(cells[0], cells[2], "gpus-pricing")
            if accel is None:
                continue  # a virtual workstation (GPU + licence), not an ATTACHABLE model
            price = G._price(G._check_price(cells[3], cells[0]))
            if price is not None and price > 0:
                out.append(_attachment(accel, price, at))
    if not out:
        raise ValueError("gcp(gpus-pricing): no ATTACHABLE GPU priced in 'Other GPU models'")
    return out


def _components_machines(body: str) -> list[dict]:
    """vm-instance-pricing Dec 2025 - Mar 2026: Machine type | GPU | Components | On-Demand (USD) | CUD columns, family
    heading rows ("A3 High") with the other cells empty. The attachment table printed next to them ("Model | GPUs
    Configuration | GPU Memory Configuration | Price per GPU") states memory ranges that disagree with the GPUs (P4
    "16, 32 or 64 GB GDDR6"), so it is not read."""
    from backfill.retired import gcp_page as G

    out = []
    for table in G._TABLE.findall(body):
        rows = G._rows(table)
        head = rows[0] if rows else []
        if head[:1] == ["Model"]:
            continue
        if head[:4] != ["Machine type", "GPU", "Components", "On-Demand (USD)"]:
            raise ValueError(f"gcp(vm-instance-pricing): machine table header changed: {head[:4]}")
        for cells in rows[1:]:
            if not any(cells[1:]):
                continue  # a family heading row
            if len(cells) != len(head):
                raise ValueError(f"gcp(vm-instance-pricing): machine row has {len(cells)} cells, header has {len(head)}")
            mtype = cells[0].replace("\ufeff", "")  # a stray byte-order mark before some names (g4-standard-192)
            if not G._MACHINE_TYPE.match(mtype):
                raise ValueError(f"gcp(vm-instance-pricing): unreadable machine type {cells[0]!r}")
            parts = dict(G._COMPONENT.findall(cells[2]))
            out.append({"machine_type": mtype, "gpu": cells[1], "gpus": parts.get("GPUs"), "vcpus": parts.get("vCPUs"),
                        "memory": parts.get("Memory"), "price": G._check_price(cells[3], cells[0])})
    if not out:
        raise ValueError("gcp(vm-instance-pricing): no machine rows")
    return out


def gcp_vm_instance_accel(page: str, at: datetime) -> list[ComputeListing]:
    from backfill.retired import gcp_page as G

    body = _section(page, "Accelerator-optimized machine type family")
    if re.search(r">\s*Components\s*<", body):
        # Dec 2025 - Mar 2026: the accelerator-optimized page's own tables (Machine type | GPU | Components |
        # On-Demand (USD)), read exactly as gcp_on_demand_column reads them; this page has no attachment table.
        payload = {"region": G.REGION_LABEL, "machines": _components_machines(body), "attachable": []}
        ep = G.ENDPOINT.split("?")[0]
        return G.GcpProvider.normalize({ep: [SimpleNamespace(endpoint=ep, payload=payload, fetched_at=at)]})
    out = []
    for table in G._TABLE.findall(body):
        rows = G._rows(table)
        head = rows[0] if rows else []
        if head[:4] != ["Machine type", "GPUs", "vCPUs", "Memory"]:
            raise ValueError(f"gcp(vm-instance-pricing): machine table header changed: {head[:4]}")
        if "On-demand price (USD)" not in head:
            continue  # commitments only (a4-highgpu-8g): no on-demand price printed
        col = head.index("On-demand price (USD)")
        for cells in rows[1:]:
            if len(cells) != len(head):
                raise ValueError(f"gcp(vm-instance-pricing): row has {len(cells)} cells, header has {len(head)}")
            mtype = cells[0]
            if not G._MACHINE_TYPE.match(mtype):
                raise ValueError(f"gcp(vm-instance-pricing): unreadable machine type {mtype!r}")
            spec = G.GPU_MACHINES.get(mtype)
            if spec is None:
                log.info("gcp(vm-instance-pricing): %s is not in GPU_MACHINES; skipped", mtype)
                continue
            count, accel, _ = spec
            if G._int(cells[1]) != count:
                log.warning("gcp(vm-instance-pricing): %s prints %r GPUs, table says %d; skipped", mtype, cells[1], count)
                continue
            price = G._price(G._check_price(cells[col], mtype))
            if price is None or price <= 0:
                continue
            out.append(G._listing(mtype, accel, count, price, at, "hyperscaler", vcpu=G._int(cells[2]),
                                  ram=G._memory_gb(cells[3])))
    if not out:
        raise ValueError("gcp(vm-instance-pricing): no GPU_MACHINES machine type priced on demand")
    return out


SOURCES = [live_page("gcp", "gcp", "cloud.google.com/products/compute/pricing/accelerator-optimized",
                     gcp_on_demand_column),
           own_page("gcp", "cloud.google.com/compute/pricing", gcp_cloud_pricer),
           own_page("gcp", "cloud.google.com/compute/gpus-pricing", gcp_cloud_pricer, gcp_gpus_per_gpu),
           own_page("gcp", "cloud.google.com/compute/vm-instance-pricing", gcp_vm_instance_accel)]
