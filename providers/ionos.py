"""IONOS Cloud: Cloud GPU VMs from IONOS's public USD price list, no key.

docs.ionos.com publishes IONOS CLOUD's official price lists, one page per contracting entity, each also served as
Markdown (append ".md"; the page itself says so). Read: the IONOS CLOUD Inc. list, which is in USD:

    GET docs.ionos.com/support/general-information/price-list/ionos-cloud-inc.md

Its "### Cloud GPU VM" table has one row per VM template: Template (H200-S/M/L/XL), GPU Model ("NVIDIA H200"),
GPU Type ("H200 PCIe"), Number of GPUs (1/2/4/8), Dedicated CPUs, RAM (GiB), NVMe Storage (GB), Price per hour
("$3.261", the whole VM, net of tax) and a price group. Every row becomes one on-demand listing; a row whose price is
not "$x.xxx" or whose GPU count is not a positive integer is skipped (logged), and a list without the table or with
no readable row fails the normalize.

Not read: the EUR list (IONOS CLOUD GmbH etc., 3,00 € for H200-S) and the GBP list (IONOS CLOUD Ltd., £2.554): the
same templates in other currencies. The USD list is IONOS's own USD price, so nothing is converted.

The GPU is "H200 PCIe": NVIDIA's PCIe H200 is the H200 NVL card (canonical.py), and IONOS's docs describe PCIe
passthrough. Region: IONOS's GPU VM docs (overview / limitations, read 2026-11-01) say Cloud GPU VMs run only in
Frankfurt de/fra/2 and Worcester gb/bhx, priced alike; that pair is the region, country None. No stock signal.

Terms (checked 2026-11-01): docs.ionos.com's robots.txt allows everything with Content-Signal ai-train=yes,
search=yes, ai-input=yes; IONOS's cloud terms forbid only spidering to harvest personal identifiers. A price list:
one request an hour.

IONOS is a general-purpose cloud, so it never counts toward neocloud indices.
"""

import logging
import re
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx

import canonical
from models import ComputeListing, RawResponse
from providers.base import PollingPolicy, Provider, find_endpoint, get_page

if TYPE_CHECKING:
    from tables import RawSnapshot

log = logging.getLogger(__name__)

BASE_URL = "https://docs.ionos.com"
ENDPOINT = "/support/general-information/price-list/ionos-cloud-inc.md"
REGION = "de/fra/2; gb/bhx"  # IONOS GPU VM docs: the only two data centres offering Cloud GPU VMs

_SECTION = re.compile(r"^###\s+Cloud GPU VM\s*$(?P<body>.*?)(?=^#{1,4}\s)", re.S | re.M)
_COLUMNS = ["Template", "GPU Model", "GPU Type", "Number of GPUs", "Dedicated CPUs", "RAM (GiB)",
            "NVMe Storage (GB)", "Price per hour", "Price group"]
_USD = re.compile(r"^\$(\d+(?:,\d{3})*(?:\.\d+)?)$")


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def extract(md: str) -> dict:
    """The Cloud GPU VM table's rows, keyed by its own column names. Raises when the table or a column is missing."""
    m = _SECTION.search(md)
    if m is None:
        raise ValueError("ionos: '### Cloud GPU VM' section not found - price list reshaped")
    lines = [l for l in m["body"].splitlines() if l.strip().startswith("|")]
    if len(lines) < 3:
        raise ValueError("ionos: Cloud GPU VM table has no rows - price list reshaped")
    header = _cells(lines[0])
    if header != _COLUMNS:
        raise ValueError(f"ionos: Cloud GPU VM columns changed: {header}")
    rows = [dict(zip(header, _cells(l))) for l in lines[2:]]
    if "Inc." not in md.split("\n## ", 1)[0]:
        raise ValueError("ionos: not the IONOS CLOUD Inc. (USD) price list")
    return {"rows": rows}


def raw_gpu_name(row: dict) -> str:
    """'NVIDIA H200' + 'H200 PCIe' -> 'NVIDIA H200 PCIe' (the type carries the form factor)."""
    model, kind = row.get("GPU Model", ""), row.get("GPU Type", "")
    vendor = model.split()[0] if model else ""
    return f"{vendor} {kind}".strip() if kind else model


def _int(cell: str | None) -> int | None:
    cell = (cell or "").strip().replace(",", "")
    return int(cell) if cell.isdigit() else None


class IonosProvider(Provider):
    name = "ionos"
    # One small Markdown page; a published price list.
    polling = PollingPolicy(interval_seconds=3600, timeout_seconds=30)

    def __init__(self, client: httpx.AsyncClient | None = None):
        super().__init__(
            client
            or httpx.AsyncClient(
                base_url=BASE_URL,
                headers={"user-agent": "opengrid-terminal/0.1"},
                timeout=30,
                follow_redirects=True,
            )
        )

    async def fetch(self) -> list[RawResponse]:
        return [await get_page(self, ENDPOINT, extract)]

    @classmethod
    def normalize(cls, by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        """One Cloud GPU VM template becomes one listing."""
        rows = find_endpoint(by_endpoint, ENDPOINT)
        if not rows:
            return []
        snapshot = rows[0]
        body = snapshot.payload
        if not isinstance(body, dict) or not isinstance(body.get("rows"), list):
            raise ValueError("ionos: stored price list has no rows - reshaped")
        listings = []
        for r in body["rows"]:
            price, count = _USD.match(r.get("Price per hour", "")), _int(r.get("Number of GPUs"))
            if price is None or not count:
                log.warning("ionos: template %r unreadable (%r, %r GPUs); skipped",
                            r.get("Template"), r.get("Price per hour"), r.get("Number of GPUs"))
                continue
            total = Decimal(price.group(1).replace(",", ""))
            name = raw_gpu_name(r)
            storage = _int(r.get("NVMe Storage (GB)"))
            listings.append(ComputeListing(
                provider=cls.name,
                sku=r["Template"],
                listing_id=r["Template"],
                raw_gpu_name=name,
                canonical_gpu_name=canonical.canonical_gpu_name(name),
                gpu_count=count,
                region=REGION,
                country=None,  # Germany and the UK
                price_per_gpu_hour=total / count,
                price_per_instance_hour=total,
                currency="USD",
                market_type="on_demand",
                provider_tier="vm",
                interruptible=False,
                available=None,
                capacity=None,
                capacity_unit=None,
                vcpu=_int(r.get("Dedicated CPUs")),
                ram_gb=float(_int(r.get("RAM (GiB)")) or 0) or None,
                storage_gb=float(storage) if storage else None,
                observed_at=snapshot.fetched_at,
            ))
        if not listings:
            raise ValueError("ionos: no readable Cloud GPU VM template - reshaped")
        return listings
