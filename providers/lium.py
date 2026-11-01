"""Lium: a marketplace of rentable GPU machines, public JSON, no API key.

Each executor is one host machine with its own price. The raw body carries live
GPU utilization, driver and port details that change every request, so the
stored payload keeps only the fields we read, plus (raw-first, read by no listing
field yet) the executor's network and interconnect: measured upload / download
Mbps, NVLink / P2P summary, InfiniBand / RoCE ports (rate, link layer, state) and
fabric flags, and any announced price change (pending_price_per_hour,
price_change_effective_date). Host identity (hotkeys, GUIDs, GIDs, port lists) and
the N x N link matrix are dropped.

Capacity: each executor reports `gpu_count` and `available_gpu_count`. Summed per
(machine type, city) they are true counts, the only provider here that gives them.
"""

import logging
from typing import TYPE_CHECKING

import httpx

import canonical
import changed_at
from models import CapacityObservation, ComputeListing, RawResponse
from providers.base import PollingPolicy, Provider, find_endpoint, reshaped, to_decimal

if TYPE_CHECKING:
    from tables import RawSnapshot

log = logging.getLogger(__name__)

BASE_URL = "https://lium.io"
ENDPOINT = "/api/executors"

KIB_PER_GIB = 1024 * 1024


def _keep(executor: dict) -> dict:
    """The fields normalize() reads, verbatim."""
    specs = executor.get("specs") or {}
    loc = executor.get("location") or {}
    return {
        "id": executor.get("id"),
        "machine_name": executor.get("machine_name"),
        "price_per_gpu": executor.get("price_per_gpu"),
        "gpu_count": executor.get("gpu_count"),
        "available_gpu_count": executor.get("available_gpu_count"),
        "tier": executor.get("tier"),
        "reliability_score": executor.get("reliability_score"),
        "country_code": loc.get("country_code"),
        "city": loc.get("city"),
        "is_spot": specs.get("is_spot"),
        "cpu_count": (specs.get("cpu") or {}).get("count"),
        "ram_total": (specs.get("ram") or {}).get("total"),
        "disk_total": (specs.get("hard_disk") or {}).get("total"),
        **_extras(executor, specs),
    }


# Top-level executor fields kept verbatim (raw only). Live counters that change on every request without meaning
# (uptime_in_minutes, last_verified_at, distance_mile, is_bookmarked) are left out.
_TOP = ("pending_price_per_hour", "price_change_effective_date", "effective_upload_speed_mbps",
        "effective_download_speed_mbps", "nvlink", "on_infiniband_fabric", "on_roce_fabric", "infiniband_fabric_rentable",
        "roce_fabric_rentable", "offers_bare_metal", "is_whole_host_free", "min_gpu_count_for_rental",
        "frequent_rent_failures", "max_cuda_version", "gpu_class", "operated_by_lium")
_INTERCONNECT = ("nvlink", "nvlink_links", "nvlink_active_links", "nvlink_pairs", "p2p", "p2p_pairs", "p2p_ok_pairs",
                 "gpu_pairs", "gpu_count", "pcie_class")
_NETWORK = ("ema_upload_speed", "ema_download_speed")
_PORT = ("device", "rate", "link_layer", "state", "phys_state")


def _extras(executor: dict, specs: dict) -> dict:
    """Network, interconnect and announced-price fields, summarised: no identifiers, no link matrix."""
    link = executor.get("interconnect") if isinstance(executor.get("interconnect"), dict) else specs.get("interconnect")
    net = specs.get("network") if isinstance(specs.get("network"), dict) else {}
    ports = specs.get("infiniband_ports") if isinstance(specs.get("infiniband_ports"), list) else None
    gpus = (specs.get("gpu") or {}).get("details") if isinstance(specs.get("gpu"), dict) else None
    first_gpu = gpus[0] if isinstance(gpus, list) and gpus and isinstance(gpus[0], dict) else {}
    return {
        **{k: executor.get(k) for k in _TOP},
        "interconnect": {k: link.get(k) for k in _INTERCONNECT} if isinstance(link, dict) else None,
        "network": {k: net.get(k) for k in _NETWORK} if net else None,
        "infiniband_ports": None if ports is None else [{k: p.get(k) for k in _PORT} for p in ports if isinstance(p, dict)],
        "gpu_memory_mib": first_gpu.get("capacity"),
        "cpu_model": (specs.get("cpu") or {}).get("model") if isinstance(specs.get("cpu"), dict) else None,
        "disk_type": (specs.get("hard_disk") or {}).get("disk_type") if isinstance(specs.get("hard_disk"), dict) else None,
    }


class LiumProvider(Provider):
    name = "lium"
    # One request per poll against an anonymous endpoint (about 1 MB raw).
    polling = PollingPolicy(interval_seconds=900, timeout_seconds=45)

    def __init__(self, client: httpx.AsyncClient | None = None):
        super().__init__(
            client
            or httpx.AsyncClient(
                base_url=BASE_URL,
                headers={"accept": "application/json", "user-agent": "opengrid-terminal/0.1"},
                timeout=45,
            )
        )

    async def fetch(self) -> list[RawResponse]:
        raw = await self.get(ENDPOINT)
        if isinstance(raw.payload, list):
            raw = reshaped(raw, [_keep(e) for e in raw.payload])
        return [raw]

    @staticmethod
    def normalize(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        """One executor (a whole host) becomes one listing."""
        rows = find_endpoint(by_endpoint, ENDPOINT)
        if not rows:
            return []
        snapshot = rows[0]
        if not isinstance(snapshot.payload, list):
            log.warning("lium: unexpected payload shape %s", type(snapshot.payload).__name__)
            return []

        # A live-stream snapshot (live/lium_stream.py) says when each changed executor changed: Lium's frame `ts`.
        # Handed to the freshness pipeline (changed_at.provider_time keeps it only when the price moved).
        request = getattr(snapshot, "request", None)
        stream_times = (request.get("changed_at") or {}) if isinstance(request, dict) else {}
        listings = []
        for e in snapshot.payload:
            per_gpu = to_decimal(e.get("price_per_gpu"))
            gpu_count = e.get("gpu_count") or 0
            raw_name = e.get("machine_name")
            if per_gpu is None or not gpu_count or not raw_name:
                continue
            free = e.get("available_gpu_count")
            ram, disk = e.get("ram_total"), e.get("disk_total")
            listings.append(
                ComputeListing(
                    provider="lium",
                    sku=raw_name,
                    listing_id=str(e["id"]),
                    raw_gpu_name=raw_name,
                    canonical_gpu_name=canonical.canonical_gpu_name(raw_name),
                    gpu_count=gpu_count,
                    region=e.get("city"),
                    country=e.get("country_code"),
                    price_per_gpu_hour=per_gpu,
                    price_per_instance_hour=per_gpu * gpu_count,
                    currency="USD",
                    market_type="spot" if e.get("is_spot") else "on_demand",
                    provider_tier=e.get("tier"),
                    interruptible=bool(e.get("is_spot")),
                    available=None if free is None else free > 0,
                    capacity=free,
                    capacity_unit=None if free is None else "gpu",
                    vcpu=e.get("cpu_count"),
                    ram_gb=None if not ram else round(ram / KIB_PER_GIB, 1),
                    storage_gb=None if not disk else round(disk / KIB_PER_GIB, 1),
                    observed_at=snapshot.fetched_at,
                    provider_changed_at=changed_at.parse_time(stream_times.get(str(e["id"]))),
                    changed_at_basis="exact" if stream_times.get(str(e["id"])) else None,
                )
            )
        return listings

    @staticmethod
    def normalize_capacity(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[CapacityObservation]:
        """Executors summed per (machine type, city): free GPUs out of all GPUs."""
        rows = find_endpoint(by_endpoint, ENDPOINT)
        if not rows or not isinstance(rows[0].payload, list):
            return []
        sums: dict[tuple, list[int]] = {}
        for e in rows[0].payload:
            name, total, free = e.get("machine_name"), e.get("gpu_count"), e.get("available_gpu_count")
            if not name or not isinstance(total, int) or total < 1 or not isinstance(free, int):
                continue
            s = sums.setdefault((name, e.get("city"), e.get("country_code")), [0, 0, total])
            s[0] += free
            s[1] += total
            s[2] = min(s[2], total)  # GPUs per instance: the smallest host of this type
        return [CapacityObservation(
            provider="lium", sku=name, region=city, country=country, raw_gpu_name=name,
            canonical_gpu_name=canonical.canonical_gpu_name(name), gpu_count=per_host,
            in_stock=free > 0, available_count=free, total_count=total, count_unit="gpu",
            raw_value=f"{free}/{total}", source=ENDPOINT, observed_at=rows[0].fetched_at)
            for (name, city, country), (free, total, per_host) in sums.items()]
