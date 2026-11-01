"""Exoscale: the public price feed behind its pricing page, no key.

GET https://portal.exoscale.com/api/pricing/opencompute returns three flat maps, "chf", "eur" and "usd",
of hourly rates keyed "running_<family>_<size>" (e.g. "running_gpu3_large": "4.18130000"). The USD map is
read (no conversion). On 2026-11-01 every key had the same figure in all three currencies: Exoscale
prices nominally alike in CHF, EUR and USD; the USD map is what a USD-billed account pays.

The feed names no GPU, so the GPU model, GPU count, vCPUs and RAM per (family, size) come from the table
on Exoscale's own pricing page (https://www.exoscale.com/pricing/, read 2026-11-01), held below in FAMILIES,
with the zones the page lists for each family. A key whose family or size is not in that table is skipped
(logged), never guessed: the legacy "gpu" family (no longer on the page) is such a key. GPU B300 is "on
request" and absent from the feed.

The rate is the whole instance per hour. Local storage is billed on top (minimum 100 GB at the "volume"
rate) and is not added. Exoscale screens accounts before granting GPU access ("priority to established
businesses"): a list price, not a self-serve guarantee. No stock signal is published: available and
capacity stay unknown.

A response without a "usd" map, or with no GPU key the table can read, fails the normalize.

Terms: www.exoscale.com's robots.txt allows everything except /register/; portal.exoscale.com publishes
none (its SPA answers); the terms (https://www.exoscale.com/terms/) have no clause on automated access.
List prices change rarely: one request an hour.
"""

import logging
import re
from typing import TYPE_CHECKING

import httpx

import canonical
from models import ComputeListing, RawResponse
from providers.base import PollingPolicy, Provider, find_endpoint, to_decimal

if TYPE_CHECKING:
    from tables import RawSnapshot

log = logging.getLogger(__name__)

BASE_URL = "https://portal.exoscale.com"
PRICING = "/api/pricing/opencompute"
_KEY = re.compile(r"^running_(gpu[a-z0-9]*)_(small|medium|large|huge)$")

# family -> (raw GPU name, zones from the pricing page, {size: (GPUs, vCPUs, RAM GB)}), pricing page 2026-11-01.
FAMILIES: dict[str, tuple[str, tuple[str, ...], dict[str, tuple[int, int, int]]]] = {
    "gpua30": ("Exoscale GPUA30 (NVIDIA A30 24GB)", ("CH-GVA-2",),
               {"small": (1, 12, 56), "medium": (2, 16, 90), "large": (3, 24, 120), "huge": (4, 48, 225)}),
    "gpu2": ("Exoscale GPU2 (Tesla V100 16GB)", ("AT-VIE-1",),
             {"small": (1, 12, 56), "medium": (2, 16, 90), "large": (3, 24, 120), "huge": (4, 48, 225)}),
    "gpu3": ("Exoscale GPU3 (NVIDIA A40 48GB)", ("DE-FRA-1",),
             {"small": (1, 12, 56), "medium": (2, 24, 120), "large": (4, 48, 224), "huge": (8, 96, 448)}),
    "gpua5000": ("Exoscale GPUA5000 (NVIDIA RTX A5000 24GB)", ("AT-VIE-2",),
                 {"small": (1, 12, 56), "medium": (2, 24, 112), "large": (4, 48, 224)}),
    "gpu3080ti": ("Exoscale GPU3080ti (NVIDIA RTX 3080 Ti 12GB)", ("AT-VIE-2",),
                  {"small": (1, 12, 56), "medium": (2, 24, 112), "large": (4, 48, 224)}),
    "gpurtx6000pro": ("Exoscale GPURTX6000PRO (NVIDIA RTX PRO 6000 Blackwell 96GB)",
                      ("DE-FRA-1", "HR-ZAG-1", "CH-DK-2", "AT-VIE-2"),
                      {"small": (1, 36, 120), "medium": (2, 72, 240), "large": (4, 144, 480), "huge": (8, 248, 960)}),
}


def _country(zones: tuple[str, ...]) -> str | None:
    codes = {z.split("-")[0] for z in zones}
    return codes.pop() if len(codes) == 1 else None


class ExoscaleProvider(Provider):
    name = "exoscale"
    # One small public GET; list prices.
    polling = PollingPolicy(interval_seconds=3600, timeout_seconds=30)

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
        return [await self.get(PRICING)]  # ~6 KB, stored verbatim

    @staticmethod
    def normalize(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        rows = find_endpoint(by_endpoint, PRICING)
        if not rows:
            return []
        snapshot = rows[0]
        usd = snapshot.payload.get("usd") if isinstance(snapshot.payload, dict) else None
        if not isinstance(usd, dict):
            raise ValueError("exoscale: pricing has no `usd` map - response reshaped")
        listings = []
        for key, value in sorted(usd.items()):
            m = _KEY.match(key)
            if not m:
                continue  # CPU instances, storage, network
            family, size = m.groups()
            spec = FAMILIES.get(family)
            if spec is None or size not in spec[2]:
                log.warning("exoscale: %s is not in the pricing page's GPU table; skipped", key)
                continue
            price = to_decimal(value)
            if price is None or price <= 0:
                continue
            name, zones, sizes = spec
            gpus, vcpu, ram = sizes[size]
            sku = f"{family}.{size}"
            listings.append(
                ComputeListing(
                    provider="exoscale",
                    sku=sku,
                    listing_id=sku,
                    raw_gpu_name=name,
                    canonical_gpu_name=canonical.canonical_gpu_name(name),
                    gpu_count=gpus,
                    region="; ".join(zones),
                    country=_country(zones),
                    price_per_gpu_hour=price / gpus,
                    price_per_instance_hour=price,
                    currency="USD",
                    market_type="on_demand",
                    provider_tier="vm",
                    interruptible=False,
                    available=None,
                    capacity=None,
                    capacity_unit=None,
                    vcpu=vcpu,
                    ram_gb=float(ram),
                    storage_gb=None,
                    observed_at=snapshot.fetched_at,
                )
            )
        if not listings:
            raise ValueError("exoscale: no GPU key the pricing table can read - not 'nothing for sale'")
        return listings
