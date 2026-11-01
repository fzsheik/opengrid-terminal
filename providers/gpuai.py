"""GPU.ai: its public pricing catalogue, no key. A reseller: kept out of market statistics.

GET https://api.gpu.ai/v1/pricing?limit=200 (docs: gpu.ai/docs, "the catalog endpoints are public: no API
key required"; list endpoints are cursor-paginated, ?limit 1-200, ?cursor from next_cursor) returns one row
per offering: gpu_type, gpu_count, region, tier ("on_demand"), price_per_hour, available, capacity_class
("secure" | "community"), deployment_type ("vm" | "container"), cpu_cores, ram_gb, storage_gb, offering_id.

Prices: price_per_hour is USD for the whole offering (a100_80gb x2 = 2.14 = 2 x 1.07; l40s x8 = 6.4), so the
per-GPU rate is price / gpu_count. `available` is a count of launchable offerings (instances), not GPUs.
Container offerings bill disk at disk_price_per_gb_hour on top (0.00014 $/GB-h; not added, their
instance_disk_gb is 0); VM offerings include storage_gb.

Why it never votes: GPU.ai's own terms (https://gpu.ai/terms) say "We aggregate GPU inventory from multiple
providers", and the API does not say whose. Its offerings are other clouds' GPUs at GPU.ai's price, possibly
clouds OpenGrid reads directly, so counting them would count a cloud twice. Every listing carries
provider_tier "resold:undisclosed" (market.RESOLD_PREFIX): stored, shown and searchable, but excluded from
the market view, rollups, indices and routing validation, like Hugging Face Endpoints' resold AWS/GCP/Azure
capacity (providers/huggingface.py).

Capacity (count shape, region = GPU.ai's region): `available` offerings per offering_id, the same figure as
the listing's capacity (unit "instance").

A response without a `data` list, or no priced GPU row, fails the normalize.

Terms: no clause on scraping or automated access in https://gpu.ai/terms; robots.txt disallows /api/ on
gpu.ai only (the API is api.gpu.ai, documented as public). Rate limits are per key; one or two pages every
15 minutes.
"""

import logging
from typing import TYPE_CHECKING

import httpx

import canonical
from models import CapacityObservation, ComputeListing, RawResponse
from providers.base import PollingPolicy, Provider, find_endpoint, to_decimal

if TYPE_CHECKING:
    from tables import RawSnapshot

log = logging.getLogger(__name__)

BASE_URL = "https://api.gpu.ai"
PRICING = "/v1/pricing"
LIMIT = 200       # the documented maximum
MAX_PAGES = 10
TIER = "resold:undisclosed"
_COUNTRY = {"us": "US", "ca": "CA"}


def country_of(region: str | None) -> str | None:
    """'us-east' -> US, 'ca-central' -> CA; 'eu-west' and 'asia-pacific' name no country."""
    return _COUNTRY.get((region or "").split("-")[0])


def _rows(snapshots) -> list[dict]:
    out = []
    for snap in snapshots:
        data = snap.payload.get("data") if isinstance(snap.payload, dict) else None
        if not isinstance(data, list):
            raise ValueError("gpuai: pricing has no `data` list - response reshaped")
        out.extend(r for r in data if isinstance(r, dict))
    return out


def _count(v) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


class GpuAiProvider(Provider):
    name = "gpuai"
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
        out, cursor = [], None
        for page in range(1, MAX_PAGES + 1):
            params = {"limit": LIMIT, **({"cursor": cursor} if cursor else {})}
            raw = await self.get(PRICING, params=params)
            raw = raw.model_copy(update={"endpoint": f"{PRICING}?limit={LIMIT}&page={page}"})
            out.append(raw)  # small (~40 KB for 66 rows), stored verbatim
            body = raw.payload
            cursor = body.get("next_cursor") if raw.ok and isinstance(body, dict) else None
            if not cursor:
                break
        return out

    @staticmethod
    def normalize(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        snaps = find_endpoint(by_endpoint, PRICING)
        if not snaps:
            return []
        fetched_at = max(s.fetched_at for s in snaps)
        listings = []
        for r in _rows(snaps):
            name, n, oid = r.get("gpu_type"), r.get("gpu_count"), r.get("offering_id")
            price = to_decimal(r.get("price_per_hour"))
            if not name or not oid or not isinstance(n, int) or n < 1 or price is None or price <= 0:
                continue
            if r.get("tier") != "on_demand":
                log.warning("gpuai: offering %s has tier %r; skipped", oid, r.get("tier"))
                continue
            free = _count(r.get("available"))
            kind = f"{r.get('deployment_type') or 'unknown'}/{r.get('capacity_class') or 'unknown'}"
            region = r.get("region") or None
            storage = r.get("storage_gb")
            listings.append(
                ComputeListing(
                    provider="gpuai",
                    sku=f"{name}x{n}:{kind}",
                    listing_id=str(oid),
                    raw_gpu_name=str(name),
                    canonical_gpu_name=canonical.canonical_gpu_name(str(name)),
                    gpu_count=n,
                    region=region,
                    country=country_of(region),
                    price_per_gpu_hour=price / n,
                    price_per_instance_hour=price,
                    currency="USD",
                    market_type="on_demand",
                    provider_tier=TIER,
                    interruptible=False,
                    available=None if free is None else free > 0,
                    capacity=free,
                    capacity_unit=None if free is None else "instance",
                    vcpu=r.get("cpu_cores") if isinstance(r.get("cpu_cores"), int) else None,
                    ram_gb=float(r["ram_gb"]) if isinstance(r.get("ram_gb"), (int, float)) else None,
                    storage_gb=float(storage) if isinstance(storage, (int, float)) and storage > 0 else None,
                    observed_at=fetched_at,
                )
            )
        if not listings:
            raise ValueError("gpuai: pricing has no priced GPU offering - not 'nothing for sale'")
        return listings

    @staticmethod
    def normalize_capacity(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[CapacityObservation]:
        snaps = find_endpoint(by_endpoint, PRICING)
        if not snaps:
            return []
        fetched_at = max(s.fetched_at for s in snaps)
        out = []
        for r in _rows(snaps):
            name, n, oid = r.get("gpu_type"), r.get("gpu_count"), r.get("offering_id")
            if not name or not oid or not isinstance(n, int) or n < 1:
                continue
            free = _count(r.get("available"))
            kind = f"{r.get('deployment_type') or 'unknown'}/{r.get('capacity_class') or 'unknown'}"
            out.append(CapacityObservation(
                provider="gpuai", sku=f"{name}x{n}:{kind}", region=r.get("region") or None,
                country=country_of(r.get("region")), raw_gpu_name=str(name),
                canonical_gpu_name=canonical.canonical_gpu_name(str(name)), gpu_count=n,
                in_stock=None if free is None else free > 0, available_count=free,
                count_unit=None if free is None else "instance",
                raw_value=f"available={r.get('available')}; offering_id={oid}", source=PRICING, observed_at=fetched_at))
        return out
