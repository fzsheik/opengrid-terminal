"""RunPod: GraphQL only, two clouds, one request for the whole catalogue.

`lowestPrice` takes a `secureCloud` flag, so each cloud reports its own price,
stock and pod minimums. Asking for every (cloud, gpuCount) pair as aliases
keeps the whole poll to a single HTTP request.
"""

import logging
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx

import canonical
from models import ComputeListing, RawResponse
from providers.base import PollingPolicy, Provider, find_endpoint

if TYPE_CHECKING:
    from tables import RawSnapshot

log = logging.getLogger(__name__)

ENDPOINT = "https://api.runpod.io/graphql"

# Pod sizes RunPod prices. Anything larger comes back unpriced.
GPU_COUNTS = (1, 2, 4, 8)
# (our tier name, the secureCloud flag it corresponds to)
CLOUDS = (("secure", "true"), ("community", "false"))

# Fields of LowestPrice we actually use. rentedCount, totalCount,
# rentalPercentage and availableGpuCounts also exist but are null in every
# combination tried, so there is no capacity number to be had.
_PRICE_FIELDS = "uninterruptablePrice stockStatus minVcpu minMemory minDisk"


def _alias(cloud: str, count: int) -> str:
    return f"{cloud}_{count}"


def build_query() -> str:
    """One document asking every (cloud, pod size) pair as a separate alias."""
    parts = [
        f"{_alias(cloud, n)}: lowestPrice(input: {{gpuCount: {n}, secureCloud: {flag}}}) "
        f"{{ {_PRICE_FIELDS} }}"
        for cloud, flag in CLOUDS
        for n in GPU_COUNTS
    ]
    return (
        "{ gpuTypes { id displayName manufacturer memoryInGb "
        "securePrice communityPrice maxGpuCount minPodGpuCount "
        + " ".join(parts)
        + " } }"
    )


class RunPodProvider(Provider):
    name = "runpod"
    # One request per poll. RunPod publishes no rate limit for GraphQL.
    polling = PollingPolicy(interval_seconds=900, timeout_seconds=45)

    def __init__(self, api_key: str, client: httpx.AsyncClient | None = None):
        super().__init__(
            client
            or httpx.AsyncClient(
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "content-type": "application/json",
                    "accept": "application/json",
                },
                timeout=40,
            )
        )

    async def fetch(self) -> list[RawResponse]:
        response = await self.post(ENDPOINT, json={"query": build_query()})
        # GraphQL answers 200 even when it refuses the query, so surface that.
        if response.ok and isinstance(response.payload, dict) and response.payload.get("errors"):
            message = str(response.payload["errors"])[:500]
            log.error("runpod: graphql errors: %s", message)
            response = response.model_copy(update={"ok": False, "error": message})
        return [response]

    @staticmethod
    def normalize(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        rows = find_endpoint(by_endpoint, "graphql")
        if not rows:
            return []
        snapshot = rows[0]
        payload = snapshot.payload or {}
        types = (payload.get("data") or {}).get("gpuTypes") or []

        listings = []
        for gpu in types:
            gpu_id = gpu.get("id")
            if not gpu_id:
                continue
            for cloud, _flag in CLOUDS:
                for count in GPU_COUNTS:
                    price_block = gpu.get(_alias(cloud, count)) or {}
                    total = price_block.get("uninterruptablePrice")
                    if total is None:
                        # Not offered in this cloud at this pod size.
                        continue
                    per_instance = Decimal(str(total))
                    stock = price_block.get("stockStatus")
                    listings.append(
                        ComputeListing(
                            provider="runpod",
                            sku=f"{gpu_id}:{cloud}",
                            listing_id=f"{gpu_id}:{cloud}:{count}",
                            raw_gpu_name=gpu_id,
                            canonical_gpu_name=canonical.canonical_gpu_name(gpu_id),
                            gpu_count=count,
                            # Datacenter is knowable but costs ~50x the requests;
                            # community cloud has no datacenter at all.
                            region=None,
                            country=None,
                            price_per_gpu_hour=per_instance / count,
                            price_per_instance_hour=per_instance,
                            currency="USD",
                            # The spot fields duplicate on-demand exactly, so
                            # there is no second market to model.
                            market_type="on_demand",
                            provider_tier=cloud,
                            interruptible=cloud == "community",
                            available=stock not in (None, "None"),
                            # stockStatus is a High/Medium/Low bucket, not a count.
                            capacity=None,
                            capacity_unit=None,
                            vcpu=price_block.get("minVcpu"),
                            ram_gb=price_block.get("minMemory"),
                            storage_gb=price_block.get("minDisk"),
                            observed_at=snapshot.fetched_at,
                        )
                    )
        return listings
