"""Thunder Compute: the price map and GPU spec catalogue, read with an account key.

GET /v2/pricing returns a flat map of USD hourly rates ("h100_x2": 6.4, ...) and
GET /v2/specs the instance configs behind the `{gpu}_x{count}` keys (displayName,
vramGB, gpuCount, vCPU options, RAM per vCPU); the API docs name them the current,
mode-less catalogue (the legacy /pricing keeps old keys). Both answer without a key,
but Thunder's terms (updated 2026-09-28, s.28) allow automated access only through
your own account's credentials, so the account's API token is always sent and the
provider is not polled without one (THUNDER_API_KEY). With the key, GET /v2/status
gives live stock per size ("available" / "unavailable"); any other word, or a size
missing from it, leaves `available` unknown.

Read: every `{gpu}_x{count}` key present in both maps. The rate is for the whole
instance with its included vCPUs (the smallest vcpuOptions entry; the pricing page
bills extra vCPUs at `additional_vcpus` per hour on top) and the first 100 GB of
disk per GPU; instances are billed per minute and never preempted.

Skipped: the legacy mode keys ("h100", "a100xl_native": the old prototyping /
production modes, no longer a choice), "sandbox_*" (per-second resource rates for
a sales-gated sandbox product), and disk / snapshot / vCPU add-on rates. A spec
whose name states a VRAM figure other than vramGB is skipped, never guessed.

GPU names are the spec's displayName plus vramGB ("NVIDIA H100 80GB"). The specs
never say SXM or PCIe (the marketing page says "H100 SXM"), so the H100 and A100
stay unmapped. Thunder states only "North America" as a location: no region.
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

BASE_URL = "https://api.thundercompute.com:8443"
PRICING = "/v2/pricing"
SPECS = "/v2/specs"
STATUS = "/v2/status"
_STOCK = {"available": True, "unavailable": False}
_KEY = re.compile(r"^([a-z0-9]+)_x(\d+)$")
_VRAM_IN_NAME = re.compile(r"\s*\((\d+)\s*GB\)\s*", re.I)
DISK_PER_GPU_GB = 100  # "First 100 GB per GPU included" (technical-specs docs)


def _ram_gib(spec: dict, vcpu: int) -> int | None:
    """RAM at `vcpu` vCPUs, following the API's documented ramCapGiB rule."""
    per = spec.get("ramPerVCPUGiB")
    if not isinstance(per, int):
        return None
    cap = spec.get("ramCapGiB") or 0
    if cap > 0:
        # Capped at the max vCPU option, shrinking by ramPerVCPUGiB per vCPU below it.
        return cap - (max(spec["vcpuOptions"]) - vcpu) * per
    return vcpu * per


class ThunderComputeProvider(Provider):
    name = "thundercompute"
    # Three small requests per poll; list prices change rarely.
    polling = PollingPolicy(interval_seconds=900, timeout_seconds=30)

    def __init__(self, api_key: str, client: httpx.AsyncClient | None = None):
        super().__init__(
            client
            or httpx.AsyncClient(
                base_url=BASE_URL,
                headers={"Authorization": f"Bearer {api_key}", "accept": "application/json",
                         "user-agent": "opengrid-terminal/0.1"},
                timeout=30,
            )
        )

    async def fetch(self) -> list[RawResponse]:
        # All three bodies are small and stored verbatim.
        return [await self.get(PRICING), await self.get(SPECS), await self.get(STATUS)]

    @staticmethod
    def normalize(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        """One `{gpu}_x{count}` instance size becomes one listing."""
        prices, specs = find_endpoint(by_endpoint, PRICING), find_endpoint(by_endpoint, SPECS)
        if not prices or not specs:
            return []
        snapshot = prices[0]
        pricing = snapshot.payload.get("pricing") if isinstance(snapshot.payload, dict) else None
        spec_map = specs[0].payload.get("specs") if isinstance(specs[0].payload, dict) else None
        if not isinstance(pricing, dict) or not isinstance(spec_map, dict):
            raise ValueError("thundercompute: pricing or specs map missing - response reshaped")
        # Stock is optional: a failed or reshaped status call leaves `available` unknown.
        status = find_endpoint(by_endpoint, STATUS)
        stock = status[0].payload.get("specs") if status and isinstance(status[0].payload, dict) else None
        stock = stock if isinstance(stock, dict) else {}
        sizes = {k: v for k, v in pricing.items() if _KEY.match(k)}
        if not sizes:
            raise ValueError("thundercompute: no {gpu}_x{count} price keys - response reshaped")
        listings = []
        for key, value in sorted(sizes.items()):
            spec = spec_map.get(key)
            if not isinstance(spec, dict):
                log.warning("thundercompute: %s is priced but has no spec; skipped", key)
                continue
            count, vram, label = spec.get("gpuCount"), spec.get("vramGB"), spec.get("displayName")
            vcpus = spec.get("vcpuOptions")
            if not isinstance(count, int) or not isinstance(vram, int) or not label or not vcpus:
                raise ValueError(f"thundercompute: spec {key} lost its gpuCount, vramGB, name or vCPUs")
            if count != int(_KEY.match(key).group(2)):
                log.warning("thundercompute: %s says gpuCount %r; skipped", key, count)
                continue
            stated = _VRAM_IN_NAME.search(label)
            if stated and int(stated.group(1)) != vram:
                log.warning("thundercompute: %s name %r disagrees with vramGB %r; skipped", key, label, vram)
                continue
            price = to_decimal(value)
            if price is None or price <= 0:
                continue
            raw_name = f"{_VRAM_IN_NAME.sub(' ', label).strip()} {vram}GB"
            vcpu = min(vcpus)
            listings.append(
                ComputeListing(
                    provider="thundercompute",
                    sku=key,
                    listing_id=key,
                    raw_gpu_name=raw_name,
                    canonical_gpu_name=canonical.canonical_gpu_name(raw_name),
                    gpu_count=count,
                    region=None,
                    country=None,
                    # The rate is for the whole instance ("h100_x2" = 2 x "h100_x1").
                    price_per_gpu_hour=price / count,
                    price_per_instance_hour=price,
                    currency="USD",
                    market_type="on_demand",
                    provider_tier="vm",
                    interruptible=False,
                    available=_STOCK.get(stock.get(key)),
                    capacity=None,
                    capacity_unit=None,
                    vcpu=vcpu,
                    ram_gb=_ram_gib(spec, vcpu),
                    storage_gb=DISK_PER_GPU_GB * count,
                    observed_at=snapshot.fetched_at,
                )
            )
        return listings

