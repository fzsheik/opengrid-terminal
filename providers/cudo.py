"""CUDO Compute: its public machine-type catalogue, VMs and bare metal, no key.

Two anonymous GETs on rest.compute.cudo.org (the API its console and CLI use):
    GET /v1/vms/machine-types   per (data centre, VM machine type): gpuPriceHr, vcpuPriceHr,
                                memoryGibPriceHr, the allowed vCPU-per-GPU and vCPU-per-GiB ranges,
                                and free capacity (totalGpuFree, maxGpuFree)
    GET /v1/machines-types      per (data centre, bare-metal type): gpus, priceHr for the whole server per
                                commitmentTerm, and machinesFree
Both list only what is in stock or recently stocked (2-6 types on 2026-11-01, Norway and Sweden),
not a full catalogue.

Units (checked 2026-11-01): plain USD per hour, not cents. H100 SXM VM gpuPriceHr "1.790000"; the
8x H100 bare-metal server priceHr "14.320000" = 1.79 per GPU; L40S 0.87 per GPU. Decimal strings.

VMs are priced a la carte, so a listing is the smallest VM the API allows for ONE GPU:
    vCPU = max(1, minVcpuPerGpu), RAM = ceil(vCPU / maxVcpuPerMemoryGib) GiB,
    price = gpuPriceHr + vCPU x vcpuPriceHr + RAM x memoryGibPriceHr   (H100: 1.79 + 0.0059 + 5 x 0.0049)
Boot storage (minStorageGibPriceHr per GiB) and the IPv4 address are extra and not added (the minimum
disk size is not published). Bare metal: the COMMITMENT_TERM_NONE priceHr / gpus; other terms are
commitments and are not read.

Capacity (count shapes, region = data centre): VMs totalGpuFree GPUs; bare metal machinesFree servers.
The same figures are the listings' `capacity`. CPU-only types are skipped. No priced GPU type at all
fails the normalize.

Availability caveat: CAPABILITIES.md recorded in October 2026 that CUDO's public on-demand closed on
2026-03-31. That could not be confirmed on 2026-11-01 (no notice found; its site still advertises
on-demand VMs), and the API still states no-commitment prices with free capacity, so these are read as
its on-demand list prices. If that changes, listings go sold out or the feed fails closed.

Terms: www.cudocompute.com's robots.txt disallows only /wp-admin/; its terms of service
(https://www.cudocompute.com/terms-of-service) contain no clause on automated access, and the API is
documented for programmatic use. Two requests every 15 minutes.
"""

import logging
import math
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx

import canonical
from models import CapacityObservation, ComputeListing, RawResponse
from providers.base import PollingPolicy, Provider, find_endpoint, to_decimal

if TYPE_CHECKING:
    from tables import RawSnapshot

log = logging.getLogger(__name__)

BASE_URL = "https://rest.compute.cudo.org"
VMS = "/v1/vms/machine-types"
METAL = "/v1/machines-types"
NO_COMMITMENT = "COMMITMENT_TERM_NONE"
COUNTRY = {"no": "NO", "se": "SE", "gb": "GB", "us": "US", "fi": "FI", "de": "DE", "nl": "NL", "is": "IS",
           "ca": "CA", "fr": "FR", "es": "ES"}


def _types(snapshot, endpoint: str) -> list[dict]:
    body = snapshot.payload
    types = body.get("machineTypes") if isinstance(body, dict) else None
    if not isinstance(types, list):
        raise ValueError(f"cudo: {endpoint} has no machineTypes list - response reshaped")
    return [t for t in types if isinstance(t, dict)]


def _usd(v) -> Decimal | None:
    return to_decimal((v or {}).get("value")) if isinstance(v, dict) else None


def country_of(dc: str | None) -> str | None:
    """'no-kristiansand-1' -> 'NO'."""
    return COUNTRY.get((dc or "").split("-")[0])


def _free(v) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def _vm(t: dict) -> tuple[Decimal, int, int] | None:
    """(1-GPU VM USD/hour, vCPU, RAM GiB) at the smallest allowed config; None when the type has no GPU price."""
    gpu, cpu, mem = _usd(t.get("gpuPriceHr")), _usd(t.get("vcpuPriceHr")), _usd(t.get("memoryGibPriceHr"))
    ratio = t.get("maxVcpuPerMemoryGib")
    if gpu is None or gpu <= 0 or cpu is None or mem is None or not isinstance(ratio, (int, float)) or ratio <= 0:
        return None
    vcpu = max(1, int(t.get("minVcpuPerGpu") or 1))
    ram = math.ceil(vcpu / ratio - 1e-9)
    return gpu + vcpu * cpu + ram * mem, vcpu, ram


def _metal_price(t: dict) -> Decimal | None:
    for p in t.get("prices") or []:
        if isinstance(p, dict) and p.get("commitmentTerm") == NO_COMMITMENT:
            price = _usd(p.get("priceHr"))
            return price if price is not None and price > 0 else None
    return None


class CudoProvider(Provider):
    name = "cudo"
    # Two small GETs; no rate limit is published.
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
        # Both bodies are small (2-3 KB) and stored verbatim.
        return [await self.get(VMS), await self.get(METAL)]

    @staticmethod
    def normalize(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        vms, metal = find_endpoint(by_endpoint, VMS), find_endpoint(by_endpoint, METAL)
        if not vms and not metal:
            return []
        listings = []
        for t in _types(vms[0], VMS) if vms else []:
            name, dc = t.get("gpuModelId"), t.get("dataCenterId")
            vm = _vm(t) if name and dc and t.get("machineType") else None
            if vm is None:
                continue
            price, vcpu, ram = vm
            free = _free(t.get("totalGpuFree"))
            sku = f"vm:{t['machineType']}"
            listings.append(CudoProvider._listing(sku, f"{sku}@{dc}", name, 1, dc, price, price, "vm",
                                                  free, "gpu", vcpu, ram, None, vms[0].fetched_at))
        for t in _types(metal[0], METAL) if metal else []:
            name, dc, n = t.get("gpuModelId"), t.get("dataCenterId"), t.get("gpus")
            price = _metal_price(t)
            if not name or not dc or not t.get("id") or not isinstance(n, int) or n < 1 or price is None:
                continue
            free = _free(t.get("machinesFree"))
            sku = f"baremetal:{t['id']}"
            cores, ram, disk = t.get("cpuCores"), t.get("memoryGib"), t.get("diskSizeGib")
            listings.append(CudoProvider._listing(
                sku, f"{sku}@{dc}", name, n, dc, price / n, price, "baremetal", free, "instance",
                cores if isinstance(cores, int) else None, ram if isinstance(ram, (int, float)) else None,
                disk if isinstance(disk, (int, float)) else None, metal[0].fetched_at))
        if not listings:
            raise ValueError("cudo: no priced GPU machine type in either catalogue - not 'nothing for sale'")
        return listings

    @staticmethod
    def _listing(sku, listing_id, name, count, dc, per_gpu, total, tier, free, unit, vcpu, ram, disk, at):
        return ComputeListing(
            provider="cudo", sku=sku, listing_id=listing_id, raw_gpu_name=name,
            canonical_gpu_name=canonical.canonical_gpu_name(name), gpu_count=count, region=dc,
            country=country_of(dc), price_per_gpu_hour=per_gpu, price_per_instance_hour=total, currency="USD",
            market_type="on_demand", provider_tier=tier, interruptible=False,
            available=None if free is None else free > 0, capacity=free, capacity_unit=None if free is None else unit,
            vcpu=vcpu, ram_gb=float(ram) if ram is not None else None, storage_gb=float(disk) if disk else None,
            observed_at=at)

    @staticmethod
    def normalize_capacity(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[CapacityObservation]:
        """VMs: free GPUs per (type, data centre). Bare metal: free servers per (type, data centre)."""
        out = []
        for endpoint, key, prefix, unit in ((VMS, "totalGpuFree", "vm", "gpu"), (METAL, "machinesFree", "baremetal", "instance")):
            rows = find_endpoint(by_endpoint, endpoint)
            if not rows:
                continue
            for t in _types(rows[0], endpoint):
                name, dc = t.get("gpuModelId"), t.get("dataCenterId")
                ident = t.get("machineType") if prefix == "vm" else t.get("id")
                count = 1 if prefix == "vm" else t.get("gpus")
                if not name or not dc or not ident or not isinstance(count, int) or count < 1:
                    continue
                free = _free(t.get(key))
                out.append(CapacityObservation(
                    provider="cudo", sku=f"{prefix}:{ident}", region=dc, country=country_of(dc), raw_gpu_name=name,
                    canonical_gpu_name=canonical.canonical_gpu_name(name), gpu_count=count,
                    in_stock=None if free is None else free > 0, available_count=free,
                    count_unit=None if free is None else unit,
                    raw_value=f"{key}={t.get(key)}" + (f"; maxGpuFree={t.get('maxGpuFree')}" if prefix == "vm" else ""),
                    source=endpoint, observed_at=rows[0].fetched_at))
        return out
