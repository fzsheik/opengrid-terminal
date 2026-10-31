"""Jarvislabs: the authenticated `misc/server_meta` catalogue (the same call the official SDK makes).

GET https://backendn.jarvislabs.net/misc/server_meta with `Authorization: Bearer <JARVIS_API_KEY>` returns one row per
(region, GPU type, workload type: vm or container) with:

    price_per_hour            on-demand, per GPU-hour
    spot_price                spot, per GPU-hour (null when not offered)
    reserved_pricing          1-month / 1-year / 2-year per GPU-hour (kept in raw; not a listing yet)
    effective_num_free_devices  free GPUs for on-demand (what the SDK's own launch routing reads)
    spot_num_free_devices     free GPUs for spot
    num_gpus                  GPUs per server

Every regional host returns the same catalogue, so one call covers all regions.

GPU names: the API's `gpu_type` codes drop the form factor ("H100"), while Jarvislabs' own pricing page names the
part ("NVIDIA H100 SXM"). NAMES maps codes to the names Jarvislabs publishes, so mapping matches what the page gave;
a code not in NAMES keeps the API's own text and goes to the unmapped queue, never guessed.

Replaced the pricing-page scraper (2026-10-30): the API adds regions, spot prices, reserved prices and live capacity.
"""

import logging
import re
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx

import canonical
from models import CapacityObservation, ComputeListing, RawResponse
from providers.base import PollingPolicy, Provider, find_endpoint

if TYPE_CHECKING:
    from tables import RawSnapshot

log = logging.getLogger(__name__)

BASE_URL = "https://backendn.jarvislabs.net"
ENDPOINT = "/misc/server_meta"

# API gpu_type -> the GPU name on jarvislabs.ai/pricing (checked 2026-10-30).
NAMES = {
    "H100": "NVIDIA H100 SXM",
    "H200": "NVIDIA H200 SXM",
    "A100-80GB": "NVIDIA A100 80GB",
    "A100": "NVIDIA A100",
    "A30": "NVIDIA A30",
    "L4": "NVIDIA L4",
    "RTX-PRO6000": "NVIDIA RTX Pro 6000 Blackwell",
}


def raw_name(gpu: str, vram) -> str:
    """The GPU name with its VRAM, unless the name already carries one:
    "NVIDIA H200 SXM" + 141 -> "NVIDIA H200 SXM 141GB"; "NVIDIA A100 80GB" stays."""
    return gpu if re.search(r"\d+\s?GB\b", gpu, re.I) or not vram else f"{gpu} {vram}GB"


def _rows(by_endpoint: "dict[str, list[RawSnapshot]]"):
    """(snapshot, server_meta rows), or (None, []) before the first fetch. A reshaped body raises."""
    snaps = find_endpoint(by_endpoint, ENDPOINT)
    if not snaps:
        return None, []
    meta = snaps[0].payload.get("server_meta") if isinstance(snaps[0].payload, dict) else None
    if not isinstance(meta, list):
        raise ValueError("jarvislabs: server_meta has no server_meta list - response reshaped")
    return snaps[0], meta


def _name(row: dict) -> str:
    code = str(row.get("gpu_type") or "")
    return raw_name(NAMES.get(code, code), row.get("vram"))


def _int(v) -> int | None:
    try:
        return None if v is None or v == "" else int(v)
    except (TypeError, ValueError):
        return None


class JarvislabsProvider(Provider):
    name = "jarvislabs"
    # One authenticated request per poll; every regional host returns the same catalogue.
    polling = PollingPolicy(interval_seconds=900, timeout_seconds=30)

    def __init__(self, api_key: str, client: httpx.AsyncClient | None = None):
        super().__init__(
            client
            or httpx.AsyncClient(
                base_url=BASE_URL,
                headers={"Authorization": f"Bearer {api_key}", "accept": "application/json"},
                timeout=30,
            )
        )

    async def fetch(self) -> list[RawResponse]:
        return [await self.get(ENDPOINT)]

    @staticmethod
    def normalize(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        """One listing per (GPU, region, workload type) and market: on-demand always, spot when priced."""
        snapshot, rows = _rows(by_endpoint)
        listings = []
        for r in rows:
            name, region, workload = _name(r), r.get("region"), r.get("workload_type") or "any"
            if not r.get("gpu_type") or not region:
                continue
            per_gpu = _int(r.get("num_gpus"))
            for market, price in (("on_demand", r.get("price_per_hour")), ("spot", r.get("spot_price"))):
                if not isinstance(price, (int, float)) or price <= 0 or (market == "on_demand" and r.get("spot_only_server")):
                    continue
                listings.append(ComputeListing(
                    provider="jarvislabs",
                    sku=f"{name}:1x",
                    listing_id=f"{name}:1x:{region}:{workload}:{market}",
                    raw_gpu_name=name,
                    canonical_gpu_name=canonical.canonical_gpu_name(name),
                    gpu_count=1,  # prices are per GPU-hour; a 1-GPU instance is the unit Jarvislabs sells
                    region=region,
                    country="IN" if region.startswith("india") else None,
                    price_per_gpu_hour=Decimal(str(price)),
                    price_per_instance_hour=Decimal(str(price)),
                    currency="USD",
                    market_type=market,
                    provider_tier=workload,
                    interruptible=market == "spot",
                    available=None,  # stock lives in capacity (normalize_capacity), not on the price row
                    capacity=None,
                    capacity_unit=None,
                    vcpu=r.get("cpus_per_gpu"),
                    ram_gb=r.get("ram_per_gpu"),
                    storage_gb=None,
                    observed_at=snapshot.fetched_at,
                ))
        if rows:
            unknown = sorted({str(r.get("gpu_type")) for r in rows if r.get("gpu_type") not in NAMES})
            if unknown:
                log.warning("jarvislabs: gpu types not in NAMES (left unmapped): %s", unknown)
        return listings

    @staticmethod
    def normalize_capacity(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[CapacityObservation]:
        """Free on-demand GPUs per (GPU, region, workload type). Sku carries the workload, like the listing id."""
        snapshot, rows = _rows(by_endpoint)
        out = []
        for r in rows:
            name, region, workload = _name(r), r.get("region"), r.get("workload_type") or "any"
            if not r.get("gpu_type") or not region:
                continue
            free = _int(r.get("effective_num_free_devices"))
            if free is None:
                free = _int(r.get("num_free_devices"))
            out.append(CapacityObservation(
                provider="jarvislabs", sku=f"{name}:1x:{workload}", region=region,
                country="IN" if region.startswith("india") else None, raw_gpu_name=name,
                canonical_gpu_name=canonical.canonical_gpu_name(name), gpu_count=1,
                in_stock=None if free is None else free > 0, available_count=free,
                count_unit=None if free is None else "gpu",
                raw_value=(f"effective_free={r.get('effective_num_free_devices')}; free={r.get('num_free_devices')}; "
                           f"spot_free={r.get('spot_num_free_devices')}; gpus_per_server={r.get('num_gpus')}"),
                source=ENDPOINT, observed_at=snapshot.fetched_at))
        return out
