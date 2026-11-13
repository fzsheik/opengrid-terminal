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

Rented machines (since 2026-11-13). /api/executors lists only machines with a free GPU; a fully rented one simply
vanishes from it. Lium's public nodes feed (NODES, `include_rented=true`, no key) lists the whole fleet with each
machine's price, the same ids as /api/executors. So:
    * a fully rented machine stays one listing at its current price, available false, capacity 0: the price a renter
      is paying right now, and a rental starts or ends as an availability change of the same listing (history);
    * capacity counts the whole fleet (free out of ALL GPUs, not out of the free machines' GPUs), so utilisation is
      real: about 77% rented on 2026-11-13, where the executors list alone read about 8%.
Only machines the nodes feed shows FULLY rented and that are absent from the (fresher) executors list are added; a
machine it shows free but the executors list has dropped was rented or went offline since, and is left to the next
poll. The nodes feed names a GPU "H200" or "RTX 5090"; the executors list names it the nvidia-smi way ("NVIDIA H200",
"NVIDIA GeForce RTX 5090"). A rented-only machine takes the name another machine of the same model carries in the
executors list of this round, else machine_name() (checked against every pair seen on 2026-11-13).

Lium's public market statistics (STATS, no key; refreshed every 5 min by Lium): per GPU model the GPUs listed and
rented, the cheapest ask, the 30-day median PAID price and utilisation, and 30 days of daily GMV, GPU-hours and
rental counts. Raw only (market_stats() reads it); `generated_at` is dropped so an unchanged answer is stored once.
"""

import logging
import re
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
NODES = "/api/public/v1/nodes"
STATS = "/api/public/v1/stats"
_NODE_FIELDS = ("id", "gpu_model", "gpu_count", "available_gpu_count", "available_now", "price_per_gpu_hour",
                "min_rentable_gpu_count", "tier", "country_code", "city", "cpu_count", "ram_gb", "disk_gb",
                "gpu_memory_gb", "gpu_interconnect")
_GEFORCE = re.compile(r"^RTX \d{4}(?: Ti| SUPER| Ti SUPER)?$")


def machine_name(gpu_model: str) -> str:
    """The executors list's (nvidia-smi) name for a nodes-feed GPU model: "RTX 5090" -> "NVIDIA GeForce RTX 5090",
    "H200" -> "NVIDIA H200", "RTX 6000 Ada Generation" -> "NVIDIA RTX 6000 Ada Generation"."""
    return "NVIDIA " + ("GeForce " if _GEFORCE.match(gpu_model) else "") + gpu_model

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
        nodes = await self.get(NODES, params={"include_rented": "true"})
        body = nodes.payload.get("nodes") if nodes.ok and isinstance(nodes.payload, dict) else None
        if nodes.ok:
            nodes = (reshaped(nodes, sorted(({k: n.get(k) for k in _NODE_FIELDS} for n in body if isinstance(n, dict)),
                                            key=lambda n: str(n.get("id"))))
                     if isinstance(body, list) else
                     nodes.model_copy(update={"ok": False, "payload": None, "error": "lium: nodes reshaped"}))
        stats = await self.get(STATS)
        if stats.ok:
            stats = (reshaped(stats, {k: v for k, v in stats.payload.items() if k != "generated_at"})
                     if isinstance(stats.payload, dict) and isinstance(stats.payload.get("models"), list) else
                     stats.model_copy(update={"ok": False, "payload": None, "error": "lium: stats reshaped"}))
        return [raw, nodes, stats]

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
        return listings + rented_listings(by_endpoint, snapshot.payload)

    @staticmethod
    def normalize_capacity(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[CapacityObservation]:
        """The whole fleet (executors + the machines the nodes feed shows fully rented) summed per (machine type,
        city): free GPUs out of all GPUs."""
        rows = find_endpoint(by_endpoint, ENDPOINT)
        if not rows or not isinstance(rows[0].payload, list):
            return []
        fleet = [*rows[0].payload, *(_as_executor(n, names) for n, names in _rented_nodes(by_endpoint, rows[0].payload))]
        sums: dict[tuple, list[int]] = {}
        for e in fleet:
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


def _rented_nodes(by_endpoint: "dict[str, list[RawSnapshot]]", executors: list) -> list[tuple[dict, dict]]:
    """(node, model -> executors name) for every machine the newest nodes snapshot shows fully rented and the
    executors list does not carry. Empty without a nodes snapshot or when it is reshaped."""
    snaps = by_endpoint.get(NODES) or []
    nodes = snaps[0].payload if snaps else None
    if not isinstance(nodes, list):
        return []
    listed = {str(e.get("id")) for e in executors if isinstance(e, dict)}
    by_id = {str(e.get("id")): e for e in executors if isinstance(e, dict)}
    names = {n.get("gpu_model"): by_id[str(n.get("id"))].get("machine_name") for n in nodes
             if isinstance(n, dict) and str(n.get("id")) in by_id and by_id[str(n.get("id"))].get("machine_name")}
    return [(n, names) for n in nodes if isinstance(n, dict) and n.get("id") and str(n["id"]) not in listed
            and n.get("gpu_model") and isinstance(n.get("gpu_count"), int) and n["gpu_count"] > 0
            and n.get("available_gpu_count") == 0]


def _as_executor(node: dict, names: dict) -> dict:
    """A rented node in the executors list's shape (the fields capacity and listings read)."""
    return {"id": node["id"], "machine_name": names.get(node["gpu_model"]) or machine_name(node["gpu_model"]),
            "gpu_count": node["gpu_count"], "available_gpu_count": 0, "city": node.get("city"),
            "country_code": node.get("country_code"), "tier": node.get("tier"), "price_per_gpu": node.get("price_per_gpu_hour"),
            "is_spot": node.get("tier") == "spot", "cpu_count": node.get("cpu_count"), "ram_gb": node.get("ram_gb"),
            "disk_gb": node.get("disk_gb")}


def rented_listings(by_endpoint: "dict[str, list[RawSnapshot]]", executors: list) -> list[ComputeListing]:
    """One listing per fully rented machine (module docstring): its current price, available false, capacity 0,
    observed when the nodes feed was read."""
    snaps = by_endpoint.get(NODES) or []
    out = []
    for node, names in _rented_nodes(by_endpoint, executors):
        e = _as_executor(node, names)
        per_gpu = to_decimal(e["price_per_gpu"])
        if per_gpu is None or per_gpu <= 0:
            continue
        out.append(ComputeListing(
            provider="lium", sku=e["machine_name"], listing_id=str(e["id"]), raw_gpu_name=e["machine_name"],
            canonical_gpu_name=canonical.canonical_gpu_name(e["machine_name"]), gpu_count=e["gpu_count"],
            region=e["city"], country=e["country_code"], price_per_gpu_hour=per_gpu,
            price_per_instance_hour=per_gpu * e["gpu_count"], currency="USD",
            market_type="spot" if e["is_spot"] else "on_demand", provider_tier=e["tier"], interruptible=e["is_spot"],
            available=False, capacity=0, capacity_unit="gpu", vcpu=e["cpu_count"],
            ram_gb=e["ram_gb"], storage_gb=e["disk_gb"], observed_at=snaps[0].fetched_at))
    return out


def market_stats(by_endpoint: "dict[str, list[RawSnapshot]]") -> dict | None:
    """Lium's own market statistics from the newest stored STATS snapshot (raw, as Lium publishes them), with the
    time OpenGrid read them. None without one."""
    snaps = by_endpoint.get(STATS) or []
    if not snaps or not isinstance(snaps[0].payload, dict):
        return None
    return {**snaps[0].payload, "observed_at": snaps[0].fetched_at}
