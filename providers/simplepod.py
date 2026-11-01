"""SimplePod: the public rental market, no API key.

GET api.simplepod.ai/instances/market/list?rentalStatus=active lists every offer
open for rent right now: Docker containers by default, full VMs with mode=vm.
An offer is one host's GPU model at one GPU count (a host with 8 cards lists
1x..8x separately), with its own host-set price, data centre and country.
SimplePod runs its own racks (EU-PL-01, "enterprise") and lets third parties
list theirs ("community" data centres), so it is a marketplace.

Price: despite its name, an offer's `pricePerGpu` is the price of the WHOLE
offer per hour. The live book shows it (one host's V100 offers are 0.1, 0.2,
... 0.5 for 1..5 GPUs) and SimplePod's own dashboard prints it as
"{gpuCount}x {pricePerGpu / gpuCount} $/h". So the per-GPU rate is
pricePerGpu / gpuCount. Disk (`pricePerDiskSize`, $/month) is extra and not read.

Read: offers with isAvailableForDemand, a positive price, gpuCount >= 1 and no
host errors or maintenance. The list is paged; a page as large as the size we
ask for fails the poll rather than silently dropping offers. The GPU name is
gpuModel plus its VRAM ("RTX 3060 (12 GB)"), since some models come in more
than one memory size. A listing is a live rentable offer, so available=True;
no separate interruptible price is published, so only on-demand is read.
"""

import logging
from typing import TYPE_CHECKING

import httpx

import canonical
from models import ComputeListing, RawResponse
from providers.base import PollingPolicy, Provider, reshaped, to_decimal

if TYPE_CHECKING:
    from tables import RawSnapshot

log = logging.getLogger(__name__)

BASE_URL = "https://api.simplepod.ai"
PAGE_SIZE = 500
_PATH = f"/instances/market/list?rentalStatus=active&itemsPerPage={PAGE_SIZE}"
# market mode -> (endpoint, provider_tier)
MARKETS = {
    "docker": (_PATH, "container"),
    "vm": (_PATH + "&mode=vm", "vm"),
}
_KEEP = (
    "id", "gpuModel", "gpuCount", "gpuMemorySize", "pricePerGpu", "isAvailableForDemand",
    "errors", "rigRentalHostMaintenances", "rigRentalHostId", "cpuCoreCount", "systemMemory",
    "diskSize", "rig", "rigRentalDatacenter",
)  # fmt: skip


def _endpoint_mode(endpoint: str) -> str:
    return "vm" if "mode=vm" in endpoint else "docker"


class SimplePodProvider(Provider):
    name = "simplepod"
    # Two anonymous requests per poll; host prices can move at any time.
    polling = PollingPolicy(interval_seconds=900, timeout_seconds=30)

    def __init__(self, client: httpx.AsyncClient | None = None):
        super().__init__(
            client
            or httpx.AsyncClient(
                base_url=BASE_URL,
                headers={"accept": "application/json", "user-agent": "opengrid-terminal/0.1"},
                timeout=30,
            )
        )

    async def fetch(self) -> list[RawResponse]:
        out = []
        for endpoint, _tier in MARKETS.values():
            raw = await self.get(endpoint)
            if not isinstance(raw.payload, list):
                out.append(raw)  # stored as-is; normalize() refuses it
                continue
            # The fields normalize() reads, verbatim; speed tests and driver versions churn every poll.
            out.append(reshaped(raw, [{k: o.get(k) for k in _KEEP} for o in raw.payload if isinstance(o, dict)]))
        return out

    @staticmethod
    def normalize(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        """One rentable on-demand offer becomes one listing."""
        listings = []
        for endpoint, rows in by_endpoint.items():
            if "/instances/market/list" not in endpoint or not rows:
                continue
            snapshot, mode = rows[0], _endpoint_mode(endpoint)
            offers = snapshot.payload
            if not isinstance(offers, list):
                raise ValueError(f"simplepod: {mode} market is not a list of offers - response reshaped")
            if len(offers) >= PAGE_SIZE:
                raise ValueError(f"simplepod: {mode} market fills a whole page - some offers would be missed")
            tier = MARKETS[mode][1]
            for o in offers:
                if not all(k in o for k in ("id", "gpuModel", "gpuCount", "pricePerGpu")):
                    raise ValueError(f"simplepod: offer lost its id, model, count or price: {o.get('id')!r}")
                price = to_decimal(o["pricePerGpu"])
                count = o["gpuCount"]
                if price is None or price <= 0 or not isinstance(count, int) or count < 1:
                    log.warning("simplepod: offer %s has price %r for %r GPUs; skipped", o["id"], o["pricePerGpu"], count)
                    continue
                if not o.get("isAvailableForDemand") or o.get("errors") or o.get("rigRentalHostMaintenances"):
                    continue  # not rentable on demand right now
                mib = o.get("gpuMemorySize")
                raw_name = o["gpuModel"] + (f" ({mib / 1024:g} GB)" if isinstance(mib, int) and mib > 0 else "")
                rig = o.get("rig") or {}
                dc = o.get("rigRentalDatacenter") or {}
                cores = o.get("cpuCoreCount")
                ram = o.get("systemMemory")
                disk = o.get("diskSize")
                listings.append(
                    ComputeListing(
                        provider="simplepod",
                        sku=o["gpuModel"],
                        listing_id=f"{mode}:{o['id']}",
                        raw_gpu_name=raw_name,
                        canonical_gpu_name=canonical.canonical_gpu_name(raw_name),
                        gpu_count=count,
                        region=dc.get("name"),
                        country=rig.get("country"),
                        # pricePerGpu is the whole offer (see module docstring).
                        price_per_gpu_hour=price / count,
                        price_per_instance_hour=price,
                        currency="USD",
                        market_type="on_demand",
                        provider_tier=f"{tier}/{dc['type']}" if dc.get("type") else tier,
                        interruptible=False,
                        available=True,  # the market lists offers open for rent now
                        capacity=None,
                        capacity_unit=None,
                        vcpu=int(cores) if isinstance(cores, (int, float)) and cores > 0 else None,
                        ram_gb=ram / 1024 if isinstance(ram, (int, float)) and ram > 0 else None,
                        storage_gb=float(disk) if isinstance(disk, (int, float)) and disk > 0 else None,
                        observed_at=snapshot.fetched_at,
                    )
                )
        return listings

