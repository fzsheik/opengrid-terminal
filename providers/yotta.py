"""Yotta Shakti Cloud: on-demand GPU VMs from its public pricing page, no key; INR converted at the ECB cross rate.

shakticloud.ai/pricing/ is server-rendered (WordPress). The "AI Workspace - VM" section has two tables: Dedicated
(per month, 1 to 12-month terms) and On Demand. Only the On Demand table is read: the table whose note says
"*Unit Rate: Per Hour, Per Compute." Each row prints

    <td>VM – 1 x H100 On Demand</td>
    <td>... GPU : 1 x Nvidia H100 SXM (80 GB) <br>vCPU : 24 <br>RAM : 240 GB <br>Root Disk : 100 GB ...</td>
    <td class="dynamic-price" data-price1=" ₹ 356 " ...>

Price read: INR per hour for the whole VM (per compute), converted to USD at the ECB INR->USD CROSS rate in force at
fetch (fx.usd_rate: (EUR->USD) / (EUR->INR), both legs from one ECB date; fail closed without a fresh pair). The page
does not say whether GST is included; the INR figure is kept exactly as printed.

Not read: the Dedicated VM, Bare Metal (8x H100 ₹351, 8x B200 ₹473 per GPU-hour: monthly to 5-year commitments,
not on demand; the page prints no B300 price), SLURM/Kubernetes clusters, the AI Lab (per month) and Shakti Studio
serverless rows. Those are terms or other products, not on-demand GPU listings.

GPU names: "Nvidia H100 SXM (80 GB)" -> "NVIDIA H100 SXM 80GB" (H100 80GB SXM5); "Nvidia L40S (48 GB)" ->
"NVIDIA L40S 48GB". The memory printed is the VM's total ("2 x Nvidia H100 SXM (160 GB)"): divided by the GPUs. Region: Yotta's data centres are in India (Navi Mumbai, Greater Noida); the page names none per
row, so country IN and no region. No stock signal.

Terms (checked 2026-11-01): robots.txt allows everything except /wp-admin/. One request an hour.
"""

import html as _html
import logging
import re
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx

import canonical
import fx
from models import ComputeListing, RawResponse
from providers.base import PollingPolicy, Provider, find_endpoint, get_page

if TYPE_CHECKING:
    from tables import RawSnapshot

log = logging.getLogger(__name__)

BASE_URL = "https://shakticloud.ai"
ENDPOINT = "/pricing/"
CURRENCY = "INR"
ON_DEMAND_NOTE = "Per Hour, Per Compute"

_CONTAINER = '<div class="new-table-container"'
_NOTE = re.compile(r'<p class="note-table">(?P<v>.*?)</p>', re.S)
_ROW = re.compile(r"<tr>\s*(?P<cells>(?:\s*<td\b.*?</td>)+)\s*</tr>", re.S)
_TD = re.compile(r"<td\b(?P<attrs>[^>]*)>(?P<v>.*?)</td>", re.S)
_PRICE1 = re.compile(r'data-price1="(?P<v>[^"]*)"')
_INR = re.compile(r"₹\s*(?P<v>\d[\d,]*(?:\.\d+)?)")
_GPU = re.compile(r"GPU\s*:\s*(?P<n>\d+)\s*x\s*(?P<model>Nvidia [A-Za-z0-9 ]+?)\s*\((?P<mem>\d+)\s*GB\)", re.I)
_VCPU = re.compile(r"vCPU\s*:\s*(?P<v>\d+)")
_RAM = re.compile(r"RAM\s*:\s*(?P<v>\d+)\s*GB")
_DISK = re.compile(r"Root Disk\s*:\s*(?P<v>\d+)\s*GB")


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<br\s*/?>", " | ", re.sub(r"<(?!br)[^>]+>", " ", fragment)))).strip()


def extract(page: str) -> dict:
    """The On Demand VM table, row by row as printed. Raises when it cannot be found (page reshaped)."""
    parts = page.split(_CONTAINER)[1:]
    for part in parts:
        note = _NOTE.search(part)
        if not note or ON_DEMAND_NOTE not in _text(note["v"]):
            continue
        rows = []
        for r in _ROW.finditer(part[: note.start()]):
            tds = list(_TD.finditer(r["cells"]))
            if len(tds) < 3:
                continue
            price = _PRICE1.search(tds[2]["attrs"])
            rows.append({"name": _text(tds[0]["v"]), "description": _text(tds[1]["v"]),
                         "price": _html.unescape(price["v"]).strip() if price else _text(tds[2]["v"])})
        if not rows:
            raise ValueError("yotta: the on-demand table has no rows - page reshaped")
        return {"unit_note": _text(note["v"]), "rows": rows}
    raise ValueError("yotta: no table priced 'Per Hour, Per Compute' - page reshaped")


def read_row(r: dict) -> dict | None:
    """One on-demand VM, or None (logged) when its GPU line or INR price cannot be read."""
    gpu, inr = _GPU.search(r.get("description") or ""), _INR.search(r.get("price") or "")
    if "on demand" not in (r.get("name") or "").lower() or gpu is None or inr is None:
        log.warning("yotta: row %r unreadable (gpu %s, price %r); skipped", r.get("name"), bool(gpu), r.get("price"))
        return None
    model = re.sub(r"\s+", " ", gpu["model"]).strip()
    n, total = int(gpu["n"]), int(gpu["mem"])
    if n < 1 or total % n:
        log.warning("yotta: row %r: %s GB does not split over %s GPUs; skipped", r.get("name"), total, n)
        return None
    per_gpu = total // n   # the page prints the VM's total GPU memory: "2 x Nvidia H100 SXM (160 GB)"
    desc = r["description"]
    vcpu, ram, disk = _VCPU.search(desc), _RAM.search(desc), _DISK.search(desc)
    return {
        "name": r["name"],
        "gpu_count": n,
        "gpu_name": f"NVIDIA {model.split(' ', 1)[1]} {per_gpu}GB",
        "inr_per_hour": Decimal(inr["v"].replace(",", "")),
        "vcpu": int(vcpu["v"]) if vcpu else None,
        "ram_gb": float(ram["v"]) if ram else None,
        "disk_gb": float(disk["v"]) if disk else None,
    }


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


class YottaProvider(Provider):
    name = "yotta"
    polling = PollingPolicy(interval_seconds=3600, timeout_seconds=45)

    def __init__(self, client: httpx.AsyncClient | None = None):
        super().__init__(
            client
            or httpx.AsyncClient(base_url=BASE_URL, headers={"user-agent": "opengrid-terminal/0.1"}, timeout=45,
                                 follow_redirects=True)
        )

    async def fetch(self) -> list[RawResponse]:
        return [await get_page(self, ENDPOINT, extract)]

    @classmethod
    def normalize(cls, by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        """One on-demand VM size becomes one USD listing at its hourly rate."""
        rows = find_endpoint(by_endpoint, ENDPOINT)
        if not rows:
            return []
        snapshot = rows[0]
        body = snapshot.payload
        if not isinstance(body, dict) or not isinstance(body.get("rows"), list):
            raise ValueError("yotta: stored page has no rows list - reshaped")
        offers = [o for o in (read_row(r) for r in body["rows"]) if o]
        if not offers:
            raise ValueError("yotta: no readable on-demand VM - reshaped")
        rate = fx.usd_rate(CURRENCY, snapshot.fetched_at, context=f"yotta page fetched {snapshot.fetched_at:%Y-%m-%d %H:%M}Z")
        if rate is None:
            return []  # fail closed: nothing is converted without a fresh ECB cross rate (fx.usd_rate logged why)
        return [ComputeListing(
            provider=cls.name,
            sku=o["name"],
            listing_id=_slug(o["name"]),
            raw_gpu_name=o["gpu_name"],
            canonical_gpu_name=canonical.canonical_gpu_name(o["gpu_name"]),
            gpu_count=o["gpu_count"],
            region=None,
            country="IN",
            **fx.convert(o["inr_per_hour"], o["gpu_count"], CURRENCY, rate),
            market_type="on_demand",
            provider_tier="vm",
            interruptible=False,
            available=None,
            capacity=None,
            capacity_unit=None,
            vcpu=o["vcpu"],
            ram_gb=o["ram_gb"],
            storage_gb=o["disk_gb"],
            observed_at=snapshot.fetched_at,
        ) for o in offers]
