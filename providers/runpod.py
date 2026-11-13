"""RunPod: GraphQL only, two clouds, one request for the whole catalogue.

`lowestPrice` takes a `secureCloud` flag, so each cloud reports its own price,
stock and pod minimums. Asking for every (cloud, gpuCount) pair as aliases
keeps the whole poll to a single HTTP request.

Capacity: `stockStatus` (High / Medium / Low / None) per GPU type, cloud and pod size. Pod size matters (8x can
be Low while 1x is High), so a capacity row's sku is the listing_id "<gpu>:<cloud>:<count>".

Serverless (since 2026-11-13): a second query, `gpuPools` (the one RunPod's own console runs; no key needed), gives
each serverless GPU pool's two list prices per GPU-hour: flex (scale-to-zero workers, the price on
runpod.io/pricing) and active (always-on workers). Stored under POOLS_ENDPOINT. Listings
`serverless:<pool>:flex|active`, provider_tier serverless_flex | serverless_active: shown and served, never in market
math (a worker price on a provider whose pods are already counted would count RunPod twice). A pool spans several
GPU models (ADA_80_PRO is H100 SXM, NVL and PCIe), so it is canonical only when it holds exactly one full GPU model
(MIG slices aside); otherwise raw_gpu_name is the pool's own label and canonical_gpu_name stays None.
"""

import logging
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx

import canonical
from models import CapacityObservation, ComputeListing, RawResponse
from providers.base import PollingPolicy, Provider

if TYPE_CHECKING:
    from tables import RawSnapshot

log = logging.getLogger(__name__)

ENDPOINT = "https://api.runpod.io/graphql"
POOLS_ENDPOINT = ENDPOINT + "#gpuPools"  # same URL; a distinct stored endpoint so each body is read on its own
POOLS_QUERY = ("{ gpuPools { id label title flexHourlyCost activeHourlyCost gpuTypeIds isActive isPro maxGpuCount "
               "minGpuCount ramGb vcpus vramGb } }")
SERVERLESS_TIERS = {"flex": ("flexHourlyCost", "serverless_flex"), "active": ("activeHourlyCost", "serverless_active")}

# Pod sizes RunPod prices. Anything larger comes back unpriced.
GPU_COUNTS = (1, 2, 4, 8)
# (our tier name, the secureCloud flag it corresponds to)
CLOUDS = (("secure", "true"), ("community", "false"))

# RunPod's stock words -> our tiers. "None" (the word) means no stock; a missing value means not reported.
STOCK_TIER = {"High": "high", "Medium": "medium", "Low": "low"}

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
    # Fast tier: one GraphQL query every 2 min (stock moves in minutes; REST v2 documents 60/min per user).
    polling = PollingPolicy(interval_seconds=120, timeout_seconds=45, tier="fast", rate_limit=(30, 60.0))

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
        out = [_graphql_errors(await self.post(ENDPOINT, json={"query": build_query()}))]
        pools = _graphql_errors(await self.post(ENDPOINT, json={"query": POOLS_QUERY}))
        return out + [pools.model_copy(update={"endpoint": POOLS_ENDPOINT})]

    @staticmethod
    def normalize(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        return _pod_listings(by_endpoint) + serverless_listings(by_endpoint)

    @staticmethod
    def normalize_capacity(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[CapacityObservation]:
        rows = by_endpoint.get(ENDPOINT) or []
        if not rows:
            return []
        out = []
        for gpu in ((rows[0].payload or {}).get("data") or {}).get("gpuTypes") or []:
            gpu_id = gpu.get("id")
            if not gpu_id:
                continue
            for cloud, _flag in CLOUDS:
                for count in GPU_COUNTS:
                    block = gpu.get(_alias(cloud, count)) or {}
                    if block.get("uninterruptablePrice") is None:
                        continue  # not offered at this size in this cloud
                    word = block.get("stockStatus")
                    tier = STOCK_TIER.get(word)
                    out.append(CapacityObservation(
                        provider="runpod", sku=f"{gpu_id}:{cloud}:{count}", region=None, raw_gpu_name=gpu_id,
                        canonical_gpu_name=canonical.canonical_gpu_name(gpu_id), gpu_count=count,
                        in_stock=True if tier else (False if word == "None" else None), tier=tier,
                        raw_value=str(word), source=ENDPOINT, observed_at=rows[0].fetched_at))
        return out


def _graphql_errors(response: RawResponse) -> RawResponse:
    """GraphQL answers 200 even when it refuses the query, so surface that."""
    if response.ok and isinstance(response.payload, dict) and response.payload.get("errors"):
        message = str(response.payload["errors"])[:500]
        log.error("runpod: graphql errors: %s", message)
        response = response.model_copy(update={"ok": False, "error": message})
    return response


def _pool_gpu(pool: dict) -> tuple[str, str | None]:
    """(raw_gpu_name, canonical) of a serverless pool: the one full GPU model it holds, else its label and None."""
    full = [g for g in pool.get("gpuTypeIds") or [] if isinstance(g, str) and " MIG " not in g]
    if len(full) == 1:
        return full[0], canonical.canonical_gpu_name(full[0])
    return str(pool.get("label") or pool.get("id")), None


def serverless_listings(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
    """Two listings per active serverless pool (flex, active), per GPU-hour. A reshaped body raises."""
    rows = by_endpoint.get(POOLS_ENDPOINT) or []
    if not rows:
        return []
    snapshot = rows[0]
    pools = ((snapshot.payload or {}).get("data") or {}).get("gpuPools") if isinstance(snapshot.payload, dict) else None
    if not isinstance(pools, list):
        raise ValueError("runpod: gpuPools is not a list - response reshaped")
    out = []
    for pool in pools:
        pid = pool.get("id") if isinstance(pool, dict) else None
        if not pid or pool.get("isActive") is False:
            continue
        raw, canon = _pool_gpu(pool)
        for kind, (field, tier) in SERVERLESS_TIERS.items():
            value = pool.get(field)
            if value is None or Decimal(str(value)) <= 0:
                continue
            price = Decimal(str(value))
            out.append(ComputeListing(
                provider="runpod", sku=f"serverless:{pid}", listing_id=f"serverless:{pid}:{kind}", raw_gpu_name=raw,
                canonical_gpu_name=canon, gpu_count=1, region=None, country=None,  # pools carry no location
                price_per_gpu_hour=price, price_per_instance_hour=price, currency="USD", market_type="on_demand",
                provider_tier=tier, interruptible=False, available=None, capacity=None, capacity_unit=None,
                vcpu=None, ram_gb=None, storage_gb=None, observed_at=snapshot.fetched_at))
    return out


def _pod_listings(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
    """Pod listings: one per (GPU type, cloud, pod size) RunPod prices."""
    rows = by_endpoint.get(ENDPOINT) or []
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
