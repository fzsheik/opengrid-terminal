"""Hydra Host (Brokkr): a bare-metal GPU marketplace, its public inventory API, no key.

GET https://brokkr.hydrahost.com/api/v1/inventory?page=N&pageSize=100 (OpenAPI spec at
/api/v1/swagger-json, docs at /api/redoc; the old api.brokkr.hydrahost.com is retired) lists every
listed device: location, stockStatus ("on demand" | "reserve" | "preorder"), specs (CPU, GPU model
and count, RAM, storage) and listing prices per month, week and hour, each perGpu, perCpu and total,
for on-demand and (where the supplier allows it) interruptible use.

Units (checked 2026-11-01): the inventory's prices are US CENTS. An 8x L40S server shows
perHour.perGpu 59.375 and total 475 (a $4.75/hour server, $0.59/GPU-hour), and its interruptible
perGpu 42.5 is exactly the 0.425 USD that GET /inventory/category-prices states as the L40S start
price. The spec documents only the supplier-side listing body as "hourly price in USD"; the read
values are divided by 100 and checked: total must equal perGpu x GPUs within 1%, or the device is
skipped (logged), never guessed.

Read: every active GPU device. One listing per device for on-demand (unless the supplier set
isInterruptibleOnly) and one ":interruptible" listing where an interruptible price exists
(market_type "spot", interruptible). Devices without a GPU (CPU servers) are skipped. Hydra is a
marketplace of independent suppliers (93+ data centres), so each device is its own listing.

Stock: stockStatus "on demand" is deployable now (available True); "reserve" and "preorder" are not
(False: the price is real but not rentable this minute). The same word is the capacity row
(boolean shape, one row per device, sku = the device id = the listing sku).

Fetch keeps only the fields read (no IP addresses, disk serials or OS lists). A response without a
`data` list, or an inventory with no priced GPU device, fails the normalize.

Terms: hydrahost.com's robots.txt disallows /api/ on www.hydrahost.com only; the inventory API is on
brokkr.hydrahost.com and documented as public. The terms of service
(https://hydrahost.com/legal/terms-of-service/) forbid collecting information "in any unauthorized
manner"; this reads the documented, unauthenticated inventory endpoint, at most a few requests every
15 minutes (no rate limit is published).
"""

import logging
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx

import canonical
from models import CapacityObservation, ComputeListing, RawResponse
from providers.base import PollingPolicy, Provider, find_endpoint, reshaped, to_decimal

if TYPE_CHECKING:
    from tables import RawSnapshot

log = logging.getLogger(__name__)

BASE_URL = "https://brokkr.hydrahost.com"
INVENTORY = "/api/v1/inventory"
PAGE_SIZE = 100   # the documented maximum
MAX_PAGES = 10    # 1,000 devices; 33 were listed on 2026-11-01
REGION_MAX = 64
ON_DEMAND = "on demand"
STATUSES = (ON_DEMAND, "reserve", "preorder")

_US_STATES = {
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado", "Connecticut", "Delaware", "Florida",
    "Georgia", "Hawaii", "Idaho", "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana", "Maine",
    "Maryland", "Massachusetts", "Michigan", "Minnesota", "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada",
    "New Hampshire", "New Jersey", "New Mexico", "New York", "North Carolina", "North Dakota", "Ohio", "Oklahoma",
    "Oregon", "Pennsylvania", "Rhode Island", "South Carolina", "South Dakota", "Tennessee", "Texas", "Utah",
    "Vermont", "Virginia", "Washington", "West Virginia", "Wisconsin", "Wyoming", "United States",
}
_COUNTRIES = {
    "Singapore": "SG", "France": "FR", "Norway": "NO", "United Kingdom": "GB", "Malaysia": "MY", "India": "IN",
    "Germany": "DE", "Netherlands": "NL", "Sweden": "SE", "Finland": "FI", "Iceland": "IS", "Canada": "CA",
    "Japan": "JP", "Australia": "AU", "Spain": "ES", "Italy": "IT", "Poland": "PL", "Ireland": "IE",
}


def country_of(location: str | None) -> str | None:
    """ISO code for the location words Hydra uses ("Texas", "Singapore"); None when unknown, never guessed."""
    loc = (location or "").strip()
    return "US" if loc in _US_STATES else _COUNTRIES.get(loc)


def _keep(d: dict) -> dict:
    """The fields normalize() reads, verbatim."""
    specs, listing = d.get("specs") or {}, d.get("listing") or {}
    cpu = specs.get("cpu") or {}

    def hourly(key):
        p = listing.get(key)
        return {"perHour": (p or {}).get("perHour")} if isinstance(p, dict) else p

    return {
        "id": d.get("id"),
        "location": d.get("location"),
        "role": d.get("role"),
        "stockStatus": d.get("stockStatus"),
        "specs": {
            "cpu": {"model": cpu.get("model"), "totalCores": cpu.get("totalCores"), "totalThreads": cpu.get("totalThreads")},
            "gpu": specs.get("gpu"),
            "memory": specs.get("memory"),
            "storage": {"total": (specs.get("storage") or {}).get("total")},
        },
        "listing": {
            "isActive": listing.get("isActive"),
            "isInterruptibleOnly": listing.get("isInterruptibleOnly"),
            "onDemandPrice": hourly("onDemandPrice"),
            "interruptiblePrice": hourly("interruptiblePrice"),
        },
    }


def _devices(rows) -> list[dict]:
    out = []
    for snap in rows:
        data = snap.payload.get("data") if isinstance(snap.payload, dict) else None
        if not isinstance(data, list):
            raise ValueError("hydrahost: inventory has no `data` list - response reshaped")
        out.extend(data)
    for d in out:
        if not isinstance(d, dict) or not d.get("id") or not isinstance(d.get("specs"), dict):
            raise ValueError(f"hydrahost: device lost its id or specs: {d!r}"[:200])
    return out


def _gpu(d: dict) -> tuple[str, int] | None:
    gpu = d["specs"].get("gpu") or {}
    model, count = gpu.get("model"), gpu.get("count")
    if not model or not isinstance(count, int) or isinstance(count, bool) or count < 1:
        return None
    return str(model).strip(), count


def _hourly(d: dict, key: str, count: int) -> tuple[Decimal, Decimal] | None:
    """(per-GPU, per-server) USD per hour from one price block in cents; None when absent or inconsistent."""
    block = (d.get("listing") or {}).get(key)
    per_hour = (block or {}).get("perHour") if isinstance(block, dict) else None
    if not isinstance(per_hour, dict):
        return None
    per_gpu, total = to_decimal(per_hour.get("perGpu")), to_decimal(per_hour.get("total"))
    if per_gpu is None or total is None or per_gpu <= 0 or total <= 0:
        return None
    if abs(per_gpu * count - total) > total / 100:
        log.warning("hydrahost: %s %s perGpu %s x %d != total %s; skipped", d["id"], key, per_gpu, count, total)
        return None
    return per_gpu / 100, total / 100


class HydraHostProvider(Provider):
    name = "hydrahost"
    # One page of 100 covers today's inventory; prices are set by suppliers and change slowly.
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
        out = []
        for page in range(1, MAX_PAGES + 1):
            raw = await self.get(INVENTORY, params={"page": page, "pageSize": PAGE_SIZE})
            raw = raw.model_copy(update={"endpoint": f"{INVENTORY}?page={page}&pageSize={PAGE_SIZE}"})
            body = raw.payload
            if not raw.ok or not isinstance(body, dict) or not isinstance(body.get("data"), list):
                out.append(raw)  # stored as-is; normalize() refuses it
                break
            out.append(reshaped(raw, {"data": [_keep(d) for d in body["data"] if isinstance(d, dict)],
                                      "meta": body.get("meta")}))
            pages = (body.get("meta") or {}).get("totalPages")
            if not isinstance(pages, int) or page >= pages:
                break
        return out

    @staticmethod
    def normalize(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        rows = find_endpoint(by_endpoint, INVENTORY)
        if not rows:
            return []
        fetched_at = max(r.fetched_at for r in rows)
        listings = []
        for d in _devices(rows):
            gpu = _gpu(d)
            listing = d.get("listing") or {}
            if gpu is None or listing.get("isActive") is False:
                continue
            model, count = gpu
            status = d.get("stockStatus")
            location = (d.get("location") or "").strip() or None
            specs = d["specs"]
            threads = (specs.get("cpu") or {}).get("totalThreads")
            ram = (specs.get("memory") or {}).get("total")
            disk = (specs.get("storage") or {}).get("total")
            kinds = [("onDemandPrice", "", "on_demand")] if not listing.get("isInterruptibleOnly") else []
            kinds.append(("interruptiblePrice", ":interruptible", "spot"))
            for key, suffix, market in kinds:
                prices = _hourly(d, key, count)
                if prices is None:
                    continue
                per_gpu, total = prices
                listings.append(
                    ComputeListing(
                        provider="hydrahost",
                        sku=d["id"],
                        listing_id=f"{d['id']}{suffix}",
                        raw_gpu_name=model,
                        canonical_gpu_name=canonical.canonical_gpu_name(model),
                        gpu_count=count,
                        region=location[:REGION_MAX] if location else None,
                        country=country_of(location),
                        price_per_gpu_hour=per_gpu,
                        price_per_instance_hour=total,
                        currency="USD",
                        market_type=market,
                        provider_tier="baremetal",
                        interruptible=market == "spot",
                        available=status == ON_DEMAND if status in STATUSES else None,
                        capacity=None,
                        capacity_unit=None,
                        vcpu=threads if isinstance(threads, int) and threads > 0 else None,
                        ram_gb=float(ram) if isinstance(ram, (int, float)) and ram > 0 else None,
                        storage_gb=float(disk) if isinstance(disk, (int, float)) and disk > 0 else None,
                        observed_at=fetched_at,
                    )
                )
        if not listings:
            raise ValueError("hydrahost: inventory has no priced GPU device - not 'nothing for sale'")
        return listings

    @staticmethod
    def normalize_capacity(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[CapacityObservation]:
        """One row per active GPU device: is it deployable on demand now."""
        rows = find_endpoint(by_endpoint, INVENTORY)
        if not rows:
            return []
        fetched_at = max(r.fetched_at for r in rows)
        out = []
        for d in _devices(rows):
            gpu = _gpu(d)
            if gpu is None or (d.get("listing") or {}).get("isActive") is False:
                continue
            status, location = d.get("stockStatus"), (d.get("location") or "").strip() or None
            out.append(CapacityObservation(
                provider="hydrahost", sku=d["id"], region=location, country=country_of(location),
                raw_gpu_name=gpu[0], canonical_gpu_name=canonical.canonical_gpu_name(gpu[0]), gpu_count=gpu[1],
                in_stock=status == ON_DEMAND if status in STATUSES else None, raw_value=str(status),
                source=INVENTORY, observed_at=fetched_at))
        return out
