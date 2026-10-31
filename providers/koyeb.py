"""Koyeb: the public instance catalogue, no API key.

GET /v1/catalog/instances is anonymous and lists every instance type with
`price_hourly` for the WHOLE instance, `gpu` {count, name, memory} and the
regions it deploys in. Koyeb is a serverless platform billed per second; the
hourly figure is Koyeb's own (price_per_second x 3600, rounded).

Read: GPU instances with a positive hourly price, status AVAILABLE and at least
one region. RESTRICTED types (no regions; B200, 8x H100, ...) are sales-gated and
give no listing. Tenstorrent cards are listed at $0 (free preview): no listing.

A shape whose display name ("2x H100") disagrees with gpu.count is skipped,
never guessed: Koyeb's 4x Tenstorrent type says count 1. The GPU name is
gpu.name plus the per-GPU VRAM (gpu.memory / count), e.g. "gpu-nvidia-l40s 48GB".

Capacity: GET /v1/catalog/usage (also anonymous) gives an availability word per instance
type and region ("LOW", "MEDIUM"). It becomes a tier; an unknown word keeps in_stock unknown.

Koyeb is a general-purpose (serverless) cloud, so it never counts toward neocloud indices.
"""

import logging
import re
from typing import TYPE_CHECKING

import httpx

import canonical
from models import CapacityObservation, ComputeListing, RawResponse
from providers.base import PollingPolicy, Provider, find_endpoint, reshaped, to_decimal

if TYPE_CHECKING:
    from tables import RawSnapshot

log = logging.getLogger(__name__)

BASE_URL = "https://app.koyeb.com"
ENDPOINT = "/v1/catalog/instances?limit=100"
USAGE = "/v1/catalog/usage"
_MULTI = re.compile(r"^(\d+)x\s")
_TIER = {"LOW": "low", "MEDIUM": "medium", "HIGH": "high"}


def _gb(text) -> float | None:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(MB|GB)\s*", str(text or ""))
    if not m:
        return None
    return float(m.group(1)) / (1024 if m.group(2) == "MB" else 1)


def _gpu(i: dict) -> tuple[str, int] | None:
    """(GPU name with per-GPU VRAM, GPUs per instance), or None when the shape cannot be trusted."""
    gpu = i.get("gpu") or {}
    count = gpu.get("count")
    if not isinstance(count, int) or count < 1:
        return None
    m = _MULTI.match(i.get("display_name") or "")
    if (int(m.group(1)) if m else 1) != count:
        return None
    # gpu.memory is the instance total ("160GB" for 2x); the name carries per-GPU VRAM.
    total = _gb(gpu.get("memory"))
    vram = None if total is None else total / count
    return (gpu.get("name") or "") + (f" {vram:g}GB" if vram else ""), count


class KoyebProvider(Provider):
    name = "koyeb"
    # One anonymous request per poll; list prices change rarely.
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
        raw = await self.get(ENDPOINT)
        usage = await self.get(USAGE)
        body = raw.payload
        if not isinstance(body, dict) or not isinstance(body.get("instances"), list):
            return [raw, usage]  # stored as-is; normalize() refuses it
        # Keep the GPU instances only, verbatim, plus how many the catalogue holds.
        gpus = [i for i in body["instances"] if i.get("gpu")]
        out = [reshaped(raw, {"count": body.get("count"), "listed": len(body["instances"]), "instances": gpus})]
        ub = usage.payload
        if isinstance(ub, dict) and isinstance((ub.get("usage") or {}).get("instances"), dict):
            ids = {i.get("id") for i in gpus}
            usage = reshaped(usage, {"usage": {"instances": {k: v for k, v in ub["usage"]["instances"].items() if k in ids}}})
        return out + [usage]

    @staticmethod
    def normalize(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        """One available, priced GPU instance type becomes one listing."""
        rows = find_endpoint(by_endpoint, "/v1/catalog/instances")
        if not rows:
            return []
        snapshot = rows[0]
        body = snapshot.payload
        if not isinstance(body, dict) or not isinstance(body.get("instances"), list):
            raise ValueError("koyeb: catalogue has no `instances` list - response reshaped")
        if not isinstance(body.get("count"), int) or body["count"] > (body.get("listed") or 0):
            raise ValueError("koyeb: catalogue is paged beyond the first page - some types would be missed")
        listings = []
        for i in body["instances"]:
            gpu = i.get("gpu")
            if not gpu:
                continue
            if "id" not in i or "price_hourly" not in i or not isinstance(gpu.get("count"), int):
                raise ValueError(f"koyeb: GPU instance lost its id, price or count: {i.get('id')!r}")
            price = to_decimal(i["price_hourly"])
            regions = i.get("regions") or []
            if price is None or price <= 0 or i.get("status") != "AVAILABLE" or not regions:
                continue  # free preview, or sales-gated / not deployable
            shape = _gpu(i)
            if shape is None:
                log.warning("koyeb: %s says %r but gpu.count %r; skipped", i["id"], i.get("display_name"), gpu["count"])
                continue
            raw_name, count = shape
            listings.append(
                ComputeListing(
                    provider="koyeb",
                    sku=i["id"],
                    listing_id=i["id"],
                    raw_gpu_name=raw_name,
                    canonical_gpu_name=canonical.canonical_gpu_name(raw_name),
                    gpu_count=count,
                    region=",".join(regions)[:64],
                    country=None,
                    # price_hourly is for the whole instance.
                    price_per_gpu_hour=price / count,
                    price_per_instance_hour=price,
                    currency="USD",
                    market_type="on_demand",
                    provider_tier="serverless",
                    interruptible=False,
                    available=None,  # AVAILABLE is an offer status, not live stock
                    capacity=None,
                    capacity_unit=None,
                    vcpu=i.get("vcpu"),
                    ram_gb=_gb(i.get("memory")),
                    storage_gb=_gb(i.get("disk")),
                    observed_at=snapshot.fetched_at,
                )
            )
        return listings

    @staticmethod
    def normalize_capacity(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[CapacityObservation]:
        """One availability word per (GPU instance type, region) becomes one observation."""
        catalogue, usage = find_endpoint(by_endpoint, "/v1/catalog/instances"), find_endpoint(by_endpoint, USAGE)
        if not catalogue or not usage:
            return []
        instances = {i.get("id"): i for i in (catalogue[0].payload or {}).get("instances") or [] if isinstance(i, dict)}
        body = usage[0].payload
        block = body.get("usage") if isinstance(body, dict) else None
        per_type = block.get("instances") if isinstance(block, dict) else None
        if not isinstance(per_type, dict):
            raise ValueError("koyeb: /catalog/usage has no usage.instances map - response reshaped")
        out = []
        for sku, u in per_type.items():
            shape = _gpu(instances[sku]) if sku in instances else None
            if shape is None or not isinstance(u, dict):
                continue
            raw_name, count = shape
            # Per-region words when given; otherwise the type's provider-wide word.
            for region, r in ((u.get("regions") or {}) or {None: u}).items():
                word = str((r or {}).get("availability") or "")
                tier = _TIER.get(word.upper())
                out.append(CapacityObservation(
                    provider="koyeb", sku=sku, region=region, raw_gpu_name=raw_name,
                    canonical_gpu_name=canonical.canonical_gpu_name(raw_name), gpu_count=count,
                    in_stock=True if tier else None, tier=tier, raw_value=word or "(missing)",
                    source=USAGE, observed_at=usage[0].fetched_at))
        return out
