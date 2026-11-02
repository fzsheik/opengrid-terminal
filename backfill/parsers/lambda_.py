"""Lambda: archived captures of lambda.ai/pricing (OpenGrid's live Lambda data comes from the instance-types API,
which needs a key, so this page is read only from the archive).

The page's "Instances pricing" block has one tab per instance size ("8x", "4x", "2x", "1x"), each a table with one
row per GPU: Plan, VRAM/GPU, vCPUs, RAM, STORAGE, PRICE/GPU/HR*. The price is per GPU, so the instance price is
derived (x GPU count), the inverse of the live API (cents per instance). Only tabs named "<N>x" are read: the
cluster tabs (DURATION, GPU COUNT columns) are multi-week commitments, not on-demand instances.

Page names are translated to the gpu_description the live API reports for the same product (PAGE_TO_API), so
archived and live listings share a canonical GPU. The page states SXM / PCIe and VRAM, which is what makes each
translation unambiguous; a (plan, VRAM) pair not in the table keeps the page's own name (and maps only if
canonical.py knows it).
"""
from __future__ import annotations

import re
from datetime import datetime

from backfill.parsers import dec, listing, slug, text
from backfill.sources import own_page
from models import ComputeListing

# (plan as printed, VRAM GB per GPU as printed) -> the live API's gpu_description for that product.
PAGE_TO_API = {
    ("NVIDIA B200 SXM6", 180): "B200 (180 GB SXM6)",
    ("NVIDIA H100 SXM", 80): "H100 (80 GB SXM5)",          # Lambda's only H100 SXM is SXM5
    ("NVIDIA H100 PCIe", 80): "H100 (80 GB PCIe)",
    ("NVIDIA A100 SXM", 80): "A100 (80 GB SXM4)",          # A100 SXM exists only as SXM4
    ("NVIDIA A100 SXM", 40): "A100 (40 GB SXM4)",
    ("NVIDIA A100 PCIe", 40): "A100 (40 GB PCIe)",
    ("NVIDIA GH200", 96): "GH200 (96 GB)",
    ("NVIDIA Tesla V100", 16): "Tesla V100 (16 GB)",
    ("NVIDIA A6000", 48): "A6000 (48 GB)",
    ("NVIDIA A10", 24): "A10 (24 GB PCIe)",
    ("NVIDIA Quadro RTX 6000", 24): "RTX 6000 (24 GB)",
}

_TAB = re.compile(r'<button[^>]*role="tab"[^>]*aria-controls="(?P<id>[^"]+)"[^>]*>(?P<label>[^<]+)</button>'
                  r'|<button[^>]*aria-controls="(?P<id2>[^"]+)"[^>]*role="tab"[^>]*>(?P<label2>[^<]+)</button>')
_ROW = re.compile(r"<tr[^>]*>(?P<r>.*?)</tr>", re.S)
_CELL = re.compile(r'<t[hd][^>]*data-label="(?P<label>[^"]+)"[^>]*>(?P<v>.*?)</t[hd]>', re.S)
_COUNT_TAB = re.compile(r"^(\d+)x$")
_VRAM = re.compile(r"^(\d+(?:\.\d+)?)\s*GB$")
_GIB = re.compile(r"^(\d+(?:\.\d+)?)\s*GiB$")
_STORAGE = re.compile(r"^(\d+(?:\.\d+)?)\s*(TiB|GiB)\b")
_PRICE_LABEL = "PRICE/GPU/HR*"


def _panel(page: str, panel_id: str, after: int) -> str:
    """The panel a tab controls: the first element with that id AFTER the tab button (some designs reuse panel
    ids between the cluster tab set and the instance tab set)."""
    at = page.find(f'id="{panel_id}"', after)
    if at == -1:
        raise ValueError(f"lambda(archive): tab panel {panel_id!r} missing")
    nxt = page.find('role="tabpanel"', at + 10)
    return page[at: nxt if nxt != -1 else len(page)]


def _row(cells: dict, count: int, at: datetime) -> ComputeListing | None:
    """One labelled row (Plan, VRAM/GPU, vCPUs, RAM, STORAGE, PRICE/GPU/HR*) of a <count>x instance; None when it
    publishes no price."""
    vm = _VRAM.match(cells["VRAM/GPU"])
    if vm is None:
        raise ValueError(f"lambda(archive): unreadable VRAM {cells['VRAM/GPU']!r}")
    price = dec(cells[_PRICE_LABEL])
    if price is None:
        if re.search(r"\d", cells[_PRICE_LABEL]):
            raise ValueError(f"lambda(archive): unreadable price {cells[_PRICE_LABEL]!r}")
        return None  # "—" / contact sales: no published price
    plan, vram = cells["Plan"], float(vm.group(1))
    raw = PAGE_TO_API.get((plan, int(vram)) if vram.is_integer() else None, f"{plan} {cells['VRAM/GPU']}")
    ram = _GIB.match(cells.get("RAM", ""))
    st = _STORAGE.match(cells.get("STORAGE", ""))
    storage = None if st is None else float(st.group(1)) * (1024 if st.group(2) == "TiB" else 1)
    return listing(
        "lambda", sku=f"{plan} {cells['VRAM/GPU']}:{count}x",
        listing_id=f"{slug(plan)}-{slug(cells['VRAM/GPU'])}:{count}x", raw=raw, count=count,
        instance_price=price * count, at=at,
        vcpu=int(cells["vCPUs"]) if cells.get("vCPUs", "").isdigit() else None,
        ram_gb=float(ram.group(1)) if ram else None, storage_gb=storage)


def instance_tabs(page: str, at: datetime) -> list[ComputeListing]:
    """The tabbed "Instances pricing" tables (one tab per instance size)."""
    tabs = [(m["id"] or m["id2"], text(m["label"] or m["label2"]), m.end()) for m in _TAB.finditer(page)]
    sized = [(pid, int(_COUNT_TAB.match(label).group(1)), end) for pid, label, end in tabs if _COUNT_TAB.match(label)]
    if not sized:
        raise ValueError("lambda(archive): no '<N>x' instance tabs")
    out = []
    for pid, count, end in sized:
        rows = 0
        for tr in _ROW.finditer(_panel(page, pid, end)):
            cells = {c["label"]: text(c["v"]) for c in _CELL.finditer(tr["r"])}
            if "Plan" not in cells:
                continue  # the header row
            if _PRICE_LABEL not in cells or "VRAM/GPU" not in cells:
                raise ValueError(f"lambda(archive): a {count}x row lacks its labelled price or VRAM: {cells}")
            row = _row(cells, count, at)
            if row is not None:
                rows += 1
                out.append(row)
        if not rows:
            raise ValueError(f"lambda(archive): the {count}x tab has no priced rows")
    return out


_TABLE = re.compile(r"<table[^>]*>(?P<t>.*?)</table>", re.S)
_TD = re.compile(r"<t[hd][^>]*>(?P<v>.*?)</t[hd]>", re.S)
_PLAN = re.compile(r"^On-demand (?P<n>\d+)x (?P<plan>.+)$")


def header_tables(page: str, at: datetime) -> list[ComputeListing]:
    """Oct - Nov 2025: tables whose first row is the column header (VRAM/GPU ... PRICE/GPU/HR*) and whose rows
    name their own size and plan: "On-demand 8x NVIDIA H100 SXM"."""
    out = []
    for t in _TABLE.finditer(page):
        trs = [[text(c["v"]) for c in _TD.finditer(r["r"])] for r in _ROW.finditer(t["t"])]
        if not trs or _PRICE_LABEL not in trs[0] or "VRAM/GPU" not in trs[0]:
            continue
        header = trs[0]
        for cells in trs[1:]:
            m = _PLAN.match(cells[0]) if cells else None
            if m is None:
                continue
            if len(cells) != len(header):
                raise ValueError(f"lambda(archive): row {cells[0]!r} has {len(cells)} cells, header {len(header)}")
            labelled = {**dict(zip(header[1:], cells[1:])), "Plan": m["plan"]}
            row = _row(labelled, int(m["n"]), at)
            if row is not None:
                out.append(row)
    if not out:
        raise ValueError("lambda(archive): no 'On-demand <N>x' rows under a PRICE/GPU/HR* header")
    return out


# --------------------------------------------------------------------------------------------------------------
# lambdalabs.com/service/gpu-cloud (2019 - 2025) and lambdalabs.com/service/gpu-cloud/pricing (2021 - 2023).
# --------------------------------------------------------------------------------------------------------------

# Older pages name some products differently from the 2025 page; the 2025 plan name is used so the listing id (and
# PAGE_TO_API's translation) is the same product's. A plan not stating SXM / PCIe ("NVIDIA A100", 2021 - 2023)
# keeps its own name and stays unmapped: the page does not say which A100 it was.
PLAN_ALIASES = {"NVIDIA RTX A6000": "NVIDIA A6000", "A6000": "NVIDIA A6000", "RTX A6000": "NVIDIA A6000",
                "NVIDIA V100": "NVIDIA Tesla V100", "NVIDIA Tesla V100s": "NVIDIA Tesla V100", "V100": "NVIDIA Tesla V100", "V100s": "NVIDIA Tesla V100",
                "NVIDIA® V100 Tensor Core": "NVIDIA Tesla V100", "Tesla V100": "NVIDIA Tesla V100",
                "RTX 6000": "NVIDIA Quadro RTX 6000", "Quadro RTX 6000": "NVIDIA Quadro RTX 6000",
                "A100": "NVIDIA A100", "A10": "NVIDIA A10"}

_ON_DEMAND_HEAD = re.compile(r"^(GPUs|GPUS)$")
_PRICE_CELL = re.compile(r"^\$(?P<v>\d+(?:,\d{3})*(?:\.\d+)?)\s*/\s*(?P<per>GPU\s*/\s*)?(?:hr|hour)$", re.I)
_SIZE_PLAN = re.compile(r"^(?P<n>\d+)x\s+(?P<plan>.+?)(?:\s+(?:New Price|New|NEW|Coming [Ss]oon|Sold [Oo]ut))?$")
_EFFECTIVE = re.compile(r"effective (?P<m>\d{1,2})/(?P<d>\d{1,2})/(?P<y>\d{4})")
_STRIP = re.compile(r"<(script|style)\b.*?</\1>", re.S)


def _old_listing(count: int, plan: str, vram_gb: int, price, per_gpu: bool, at: datetime, *, vcpu=None, ram=None,
                 storage=None) -> ComputeListing:
    plan = PLAN_ALIASES.get(plan, plan)
    vram = f"{vram_gb} GB"
    raw = PAGE_TO_API.get((plan, vram_gb), f"{plan} {vram}")
    ram_m = re.match(r"^(\d+(?:\.\d+)?)\s*(GiB|GB)$", ram or "")
    st = _STORAGE.match(storage or "")
    return listing(
        "lambda", sku=f"{plan} {vram}:{count}x", listing_id=f"{slug(plan)}-{slug(vram)}:{count}x", raw=raw,
        count=count, instance_price=price * count if per_gpu else price, at=at,
        vcpu=int(vcpu) if vcpu and str(vcpu).isdigit() else None, ram_gb=float(ram_m.group(1)) if ram_m else None,
        storage_gb=None if st is None else float(st.group(1)) * (1024 if st.group(2) == "TiB" else 1))


def _context_is_on_demand(page_text_before: str) -> bool:
    """True when the text just before a table names on-demand pricing after any mention of reserved pricing."""
    tail = page_text_before[-700:].lower()
    od = max(tail.rfind("on-demand"), tail.rfind("on demand"))
    rs = tail.rfind("reserved")
    return od != -1 and od > rs


def gpu_cloud_tables(page: str, at: datetime) -> list[ComputeListing]:
    """Jul 2022 - Nov 2024: the "On-demand (GPU cloud) pricing" table, headed GPUs | VRAM per GPU (VRAM / GPU,
    VRAM/GPU) | vCPUs | RAM | Storage | Price, one row per instance ("8x NVIDIA H100 SXM").
      * until mid-2024 the price is the whole instance's ("$27.92 / hr"); from Aug 2024 it is per GPU
        ("PRICE / GPU / HR", "$2.99 / GPU / hr"), and each row says "On-demand" or "Reserved" (its own first column,
        or a prefix of the GPUs cell): reserved rows are never read.
      * Dec 2023 printed "2023 Price" and "2024 Price (effective 1/1/2024)": the one in force on the capture day.
      * 2022 put a reserved table with the same header under it: told apart by the text before each table."""
    out, tables = [], 0
    for tm in _TABLE.finditer(page):
        trs = [[text(c["v"]) for c in _TD.finditer(r["r"])] for r in _ROW.finditer(tm["t"])]
        if not trs or not trs[0]:
            continue
        head = [re.sub(r"\s*/\s*", "/", h.rstrip("*").strip().upper()) for h in trs[0]]
        lead = 1 if head[0] == "" and len(head) > 1 else 0          # Aug 2024: an unlabelled market column
        if head[lead] != "GPUS" or not {"VRAM PER GPU", "VRAM/GPU"} & set(head):
            continue
        if not _context_is_on_demand(text(_STRIP.sub(" ", page[max(0, tm.start() - 20000): tm.start()]))):
            continue  # the reserved table (same header, 2022)
        price_cols = [i for i, h in enumerate(head) if "PRICE" in h]
        if not price_cols:
            raise ValueError(f"lambda(archive): on-demand table without a price column: {trs[0]}")
        col = price_cols[0]
        if len(price_cols) > 1:
            # "2023 Price" | "2024 Price (effective 1/1/2024)": the later column applies from its effective date.
            eff = _EFFECTIVE.search(" ".join(trs[0]))
            if len(price_cols) != 2 or eff is None:
                raise ValueError(f"lambda(archive): several price columns {trs[0]}")
            starts = datetime(int(eff["y"]), int(eff["m"]), int(eff["d"]), tzinfo=at.tzinfo)
            col = price_cols[1] if at >= starts else price_cols[0]
        idx = {h: i for i, h in enumerate(head)}
        vram_i = idx.get("VRAM PER GPU", idx.get("VRAM/GPU"))
        tables += 1
        for cells in trs[1:]:
            if not cells:
                continue
            if len(cells) != len(head):
                raise ValueError(f"lambda(archive): row {cells[:2]} has {len(cells)} cells, header {len(head)}")
            gpus = cells[lead]
            market = cells[0] if lead else ""
            mm = re.match(r"^(On-demand|Reserved)\s+(.*)$", gpus)
            if mm:
                market, gpus = mm.group(1), mm.group(2)
            if market and market != "On-demand":
                if market != "Reserved":
                    raise ValueError(f"lambda(archive): row market {market!r}")
                continue
            sm = _SIZE_PLAN.match(gpus)
            vm = _VRAM.match(cells[vram_i])
            if sm is None or vm is None:
                raise ValueError(f"lambda(archive): row {gpus!r} / VRAM {cells[vram_i]!r} unreadable")
            pc = _PRICE_CELL.match(cells[col])
            if pc is None:
                if re.search(r"\d", cells[col]):
                    raise ValueError(f"lambda(archive): price {cells[col]!r} is not '$x / hr' or '$x / GPU / hr'")
                continue  # Coming soon / sold out / contact sales
            if bool(pc["per"]) != ("GPU/HR" in head[col]):
                raise ValueError(f"lambda(archive): price {cells[col]!r} under header {trs[0][col]!r}")
            out.append(_old_listing(int(sm["n"]), sm["plan"], int(float(vm.group(1))), dec(pc["v"]), bool(pc["per"]),
                                    at, vcpu=cells[idx["VCPUS"]] if "VCPUS" in idx else None,
                                    ram=cells[idx["RAM"]] if "RAM" in idx else None,
                                    storage=cells[idx["STORAGE"]] if "STORAGE" in idx else None))
    if not tables:
        raise ValueError("lambda(archive): no on-demand 'GPUs | VRAM per GPU | ... | Price' table")
    if not out:
        raise ValueError("lambda(archive): the on-demand table has no priced row")
    return out


_CARD = re.compile(r"(?P<size>\d+)-GPU Instance .{0,200}?GPU Details (?P<n>\d+)x (?P<gpu>[^()]{2,60}?) \((?P<vram>\d+) GB\)"
                   r"(?: \+ NVLink™)? VCPUS (?P<vcpu>\d+) SYSTEM RAM (?P<ram>\d+ GB) TEMPORARY STORAGE (?P<st>[\d.]+ [TG]B)"
                   r".{0,120}?instance pricing \$(?P<p>\d+(?:\.\d+)?) / hr")


def gpu_cloud_cards_2020(page: str, at: datetime) -> list[ComputeListing]:
    """May 2020 - Apr 2021: one card per instance ("8-GPU Instance ... GPU Details 8x NVIDIA® V100 Tensor Core (16 GB)
    + NVLink™ ... Dedicated 8-GPU instance pricing $12.00 / hr"), the whole instance's price. The 4-GPU card's
    "GTX 1080 Ti" / "Pascal Based GPUs (11 GB)" keeps its own name (no canonical GPU)."""
    t = text(_STRIP.sub(" ", page))
    out = []
    for m in _CARD.finditer(t):
        if int(m["size"]) != int(m["n"]):
            raise ValueError(f"lambda(archive): {m['size']}-GPU card lists {m['n']}x")
        gpu = m["gpu"].replace("NVIDIA® ", "NVIDIA ").strip()
        plan = PLAN_ALIASES.get(m["gpu"].strip(), gpu)
        st = m["st"].replace("TB", "TiB").replace("GB", "GiB")
        out.append(_old_listing(int(m["n"]), plan, int(m["vram"]), dec(m["p"]), False, at, vcpu=m["vcpu"],
                                ram=m["ram"], storage=st))
    if not out:
        raise ValueError("lambda(archive): no '<N>-GPU Instance ... $x / hr' card")
    return out


_VRAM_EACH = re.compile(r"\((?P<gb>\d+) GiB VRAM each\)")
_PLAN_VRAM = re.compile(r"^(?P<plan>.+?) \((?P<gb>\d+) GB\)$")


def pricing_tables_2021(page: str, at: datetime) -> list[ComputeListing]:
    """lambdalabs.com/service/gpu-cloud/pricing, 2021 - 2022: tables headed GPUs | [NVLink™ |] vCPUs | RAM |
    (Temporary) Storage | On-demand Price, one row per instance ("1x A6000", "$2.25 / hr": the whole instance).
      * 2021: one table per GPU; VRAM is read from its section's "(48 GiB VRAM each)", never assumed.
      * 2022: one table; the GPUs cell states the VRAM: "1x NVIDIA RTX A6000 (48 GB)".
    Reserved tables (headed "1-year Reserved Price" ...) have no "On-demand Price" column and are never read."""
    out, tables, prev = [], 0, 0
    for tm in _TABLE.finditer(page):
        trs = [[text(c["v"]) for c in _TD.finditer(r["r"])] for r in _ROW.finditer(tm["t"])]
        start, prev = prev, tm.end()
        if not trs or not trs[0] or trs[0][0] != "GPUs" or trs[0][-1] != "On-demand Price":
            continue
        head = trs[0]
        idx = {h: i for i, h in enumerate(head)}
        storage_i = idx.get("Temporary Storage", idx.get("Storage"))
        if "vCPUs" not in idx or "RAM" not in idx or storage_i is None:
            raise ValueError(f"lambda(archive): on-demand table header {head}")
        tables += 1
        section = _VRAM_EACH.findall(text(_STRIP.sub(" ", page[start: tm.start()])))
        for cells in trs[1:]:
            if len(cells) != len(head):
                raise ValueError(f"lambda(archive): row {cells[:1]} has {len(cells)} cells")
            sm = _SIZE_PLAN.match(cells[0])
            if sm is None:
                raise ValueError(f"lambda(archive): GPUs cell {cells[0]!r} is not '<N>x <GPU>'")
            plan, pv = sm["plan"], _PLAN_VRAM.match(sm["plan"])
            if pv:
                plan, vram = pv["plan"], int(pv["gb"])
            elif section:
                vram = int(section[-1])
            else:
                raise ValueError(f"lambda(archive): no VRAM for {cells[0]!r} (cell or '(N GiB VRAM each)')")
            pc = _PRICE_CELL.match(cells[-1])
            if pc is None:
                if re.search(r"\d", cells[-1]):
                    raise ValueError(f"lambda(archive): price {cells[-1]!r} is not '$x / hr'")
                continue  # Coming Soon
            out.append(_old_listing(int(sm["n"]), plan, vram, dec(pc["v"]), bool(pc["per"]), at,
                                    vcpu=cells[idx["vCPUs"]], ram=cells[idx["RAM"]], storage=cells[storage_i]))
    if not tables:
        raise ValueError("lambda(archive): no 'GPUs | ... | On-demand Price' table")
    if not out:
        raise ValueError("lambda(archive): the pricing tables have no priced row")
    return out


SOURCES = [own_page("lambda", "lambda.ai/pricing", instance_tabs, header_tables),
           own_page("lambda", "lambdalabs.com/service/gpu-cloud/pricing", gpu_cloud_tables, pricing_tables_2021),
           own_page("lambda", "lambdalabs.com/service/gpu-cloud", gpu_cloud_tables, gpu_cloud_cards_2020)]
