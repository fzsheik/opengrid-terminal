"""RunPod: pods from REST v2 (with a key) or GraphQL (without one, or when REST fails); serverless pools from GraphQL.

RunPod retires its GraphQL API "in early 2027" (docs.runpod.io/sdks/graphql/configurations, read 2026-11-13); REST v1
retires 2026-11-15. The documented successor for market data is REST v2's catalog
(https://api.runpod.io/v2/openapi.json 2.0.0, fetched 2026-11-13), which needs a Bearer key with the
`catalog:read` scope: an unauthenticated GET answers 401 "missing bearer token" (verified live 2026-11-13).

Pods, with RUNPOD_API_KEY set (REST v2, eight GETs, one per cloud x pod size, each stored under its own URL):

    GET https://api.runpod.io/v2/catalog/gpus?include=AVAILABILITY&product=POD&count=N&cloud=SECURE|COMMUNITY
        {gpus[{id, price{secure, community} (USD/h for a SINGLE GPU), availability NONE|LOW|MEDIUM|HIGH,
               minVcpuCount, minRam (pod totals for that count, omitted when NONE), maxCount, dataCenters}]}

    A listing is a (gpu, cloud, count) whose availability is LOW/MEDIUM/HIGH and whose price for that cloud is > 0;
    pod total = price[cloud] x count. This reproduces GraphQL exactly: on the live GraphQL body of 2026-11-13,
    lowestPrice(gpuCount N).uninterruptablePrice / N equalled securePrice / communityPrice in all 72 priced cells,
    and a price came back if and only if a stock word did (no "None" word ever carried a price), so an
    unavailable size was never a listing there either. minDisk has no REST field; it was null in all 384 GraphQL
    cells, so storage_gb stays None.

Pods, fallback (GraphQL, no key needed): `gpuTypes { lowestPrice(gpuCount, secureCloud) {uninterruptablePrice
stockStatus minVcpu minMemory minDisk} }`, every (cloud, size) as an alias in one POST to ENDPOINT. Used when no key
is set, and in any round where a REST call fails (401/403 scope, 5xx, 429, a body without a `gpus` list): REST stops
at the first failure and the GraphQL query runs in the same round. After a 401/403 REST is not retried for
AUTH_PAUSE_SECONDS (an hour), so a key without `catalog:read` does not fail one response in three every poll. normalize() reads whichever source holds the
newest complete round (_pod_round), so stale snapshots of the other source never mix in.

Serverless (GraphQL only, until REST exposes it): `gpuPools` (the query RunPod's own console runs; no key needed)
gives each pool's two list prices per GPU-hour: flex (scale-to-zero workers, the price on runpod.io/pricing) and
active (always-on workers). REST v2 has only `price.serverless`, one per-GPU figure from the GPU's pool, without
saying flex or active, and no pool list (label, isActive, members): no active price at all. Stored under
POOLS_ENDPOINT. Listings `serverless:<pool>:flex|active`, provider_tier serverless_flex | serverless_active: shown and
served, never in market math (a worker price on a provider whose pods are already counted would count RunPod twice).
A pool spans several GPU models (ADA_80_PRO is H100 SXM, NVL and PCIe), so it is canonical only when it holds exactly
one full GPU model (MIG slices aside); otherwise raw_gpu_name is the pool's own label and canonical_gpu_name stays None.

So until RunPod adds them to REST, these stay on GraphQL and stop at its retirement (early 2027): serverless pool
prices (always), and pods when no key is set or REST fails.

Capacity: the stock word per GPU type, cloud and pod size (GraphQL High/Medium/Low/None, REST HIGH/MEDIUM/LOW/NONE).
Pod size matters (8x can be Low while 1x is High), so a capacity row's sku is the listing_id "<gpu>:<cloud>:<count>".
"""

import logging
import time
from datetime import timedelta
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

CATALOG = "https://api.runpod.io/v2/catalog/gpus"

# Pod sizes RunPod prices. Anything larger comes back unpriced.
GPU_COUNTS = (1, 2, 4, 8)
# (our tier name, the GraphQL secureCloud flag it corresponds to)
CLOUDS = (("secure", "true"), ("community", "false"))

# RunPod's stock words -> our tiers. GraphQL capitalises them, REST v2 upper-cases them ("stockStatus -> availability",
# docs.runpod.io/api-reference-v2/migrate-from-v1). "None"/"NONE" means no stock; a missing value means not reported.
STOCK_TIER = {"High": "high", "Medium": "medium", "Low": "low", "HIGH": "high", "MEDIUM": "medium", "LOW": "low"}
NO_STOCK = ("None", "NONE")

# A 401/403 (no key scope catalog:read, or a revoked key) will not fix itself between polls: REST is left alone this
# long, so the poll is not one failed response in three (quality.trust marks a provider degraded above 20%).
AUTH_PAUSE_SECONDS = 3600

# One fetch round: normalize's own window (normalize._latest_raw keeps rows within 120 s of an endpoint's newest).
ROUND = timedelta(seconds=120)

# Fields of LowestPrice we actually use. rentedCount, totalCount,
# rentalPercentage and availableGpuCounts also exist but are null in every
# combination tried, so there is no capacity number to be had.
_PRICE_FIELDS = "uninterruptablePrice stockStatus minVcpu minMemory minDisk"


def _alias(cloud: str, count: int) -> str:
    return f"{cloud}_{count}"


def catalog_url(cloud: str, count: int) -> str:
    """The REST v2 catalog GET for one cloud and pod size; also its stored endpoint key."""
    return f"{CATALOG}?include=AVAILABILITY&product=POD&count={count}&cloud={cloud.upper()}"


CATALOG_KEYS = {catalog_url(cloud, n): (cloud, n) for cloud, _ in CLOUDS for n in GPU_COUNTS}


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
    # Fast tier: every 2 min (stock moves in minutes). With a key: 8 REST GETs + 1 GraphQL (pools); REST v2 documents
    # 60/min per user. Without one: 2 GraphQL POSTs.
    polling = PollingPolicy(interval_seconds=120, timeout_seconds=45, tier="fast", rate_limit=(30, 60.0))

    def __init__(self, api_key: str | None = None, client: httpx.AsyncClient | None = None):
        self._api_key = api_key or None
        self._rest_paused_until = 0.0
        headers = {"content-type": "application/json", "accept": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        super().__init__(client or httpx.AsyncClient(headers=headers, timeout=40))

    async def fetch(self) -> list[RawResponse]:
        out: list[RawResponse] = []
        rest_complete = False
        if self._api_key and time.time() >= self._rest_paused_until:
            for url in CATALOG_KEYS:
                r = _catalog_shape(await self.get(url))
                out.append(r)
                if not r.ok:
                    # A failed REST round is replaced whole by GraphQL below; the rest of it would be wasted calls.
                    if r.status_code in (401, 403):
                        self._rest_paused_until = time.time() + AUTH_PAUSE_SECONDS
                    log.warning("runpod: REST catalog failed (%s); pods from GraphQL this round%s",
                                r.error or r.status_code,
                                f", REST paused {AUTH_PAUSE_SECONDS}s" if r.status_code in (401, 403) else "")
                    break
            else:
                rest_complete = True
        if not rest_complete:
            out.append(_graphql_errors(await self.post(ENDPOINT, json={"query": build_query()})))
        pools = _graphql_errors(await self.post(ENDPOINT, json={"query": POOLS_QUERY}))
        return out + [pools.model_copy(update={"endpoint": POOLS_ENDPOINT})]

    @staticmethod
    def normalize(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        return _pod_listings(by_endpoint) + serverless_listings(by_endpoint)

    @staticmethod
    def normalize_capacity(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[CapacityObservation]:
        out = []
        for c in _pod_cells(by_endpoint):
            word = c["word"]
            tier = STOCK_TIER.get(word)
            out.append(CapacityObservation(
                provider="runpod", sku=f"{c['gpu']}:{c['cloud']}:{c['count']}", region=None, raw_gpu_name=c["gpu"],
                canonical_gpu_name=canonical.canonical_gpu_name(c["gpu"]), gpu_count=c["count"],
                in_stock=True if tier else (False if word in NO_STOCK else None), tier=tier,
                raw_value=str(word), source=c["source"], observed_at=c["at"]))
        return out


def _graphql_errors(response: RawResponse) -> RawResponse:
    """GraphQL answers 200 even when it refuses the query, so surface that."""
    if response.ok and isinstance(response.payload, dict) and response.payload.get("errors"):
        message = str(response.payload["errors"])[:500]
        log.error("runpod: graphql errors: %s", message)
        response = response.model_copy(update={"ok": False, "error": message})
    return response


def _catalog_shape(response: RawResponse) -> RawResponse:
    """A 200 REST catalog body without a `gpus` list is a reshape: stored as a failure so GraphQL covers the round."""
    if response.ok and not (isinstance(response.payload, dict) and isinstance(response.payload.get("gpus"), list)):
        response = response.model_copy(update={"ok": False, "error": "runpod: catalog body has no gpus list"})
    return response


def _pod_round(by_endpoint: "dict[str, list[RawSnapshot]]") -> tuple[str, dict]:
    """Which pod source to read: ("rest", {url: snapshot}), ("graphql", {ENDPOINT: snapshot}) or ("none", {}).

    by_endpoint holds each endpoint's newest ok snapshot of all time, so after a switch the other source's last
    snapshot (hours or months old) is still there. The newer source wins; within one round (ROUND), GraphQL wins over
    an incomplete REST round, because fetch() only queries GraphQL when REST failed. REST snapshots older than the
    newest REST round are dropped, never mixed in."""
    gql = (by_endpoint.get(ENDPOINT) or [None])[0]
    rest = {url: rows[0] for url in CATALOG_KEYS if (rows := by_endpoint.get(url))}
    if rest:
        newest = max(s.fetched_at for s in rest.values())
        rest = {url: s for url, s in rest.items() if newest - s.fetched_at <= ROUND}
        complete = len(rest) == len(CATALOG_KEYS)
        gql_later_round = gql is not None and gql.fetched_at > newest + ROUND
        gql_same_round = gql is not None and gql.fetched_at >= newest - ROUND
        if not gql_later_round and (complete or not gql_same_round):
            return "rest", rest
    return ("graphql", {ENDPOINT: gql}) if gql is not None else ("none", {})


def _pod_cells(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[dict]:
    """Every priced (gpu, cloud, pod size) in the round's source, as one shape whichever API it came from:
    gpu, cloud, count, total (pod USD/h, Decimal), per_gpu, word (raw stock word), vcpu, ram, disk, source, at."""
    kind, snaps = _pod_round(by_endpoint)
    cells = []
    if kind == "graphql":
        snapshot = snaps[ENDPOINT]
        for gpu in ((snapshot.payload or {}).get("data") or {}).get("gpuTypes") or []:
            gpu_id = gpu.get("id")
            if not gpu_id:
                continue
            for cloud, _flag in CLOUDS:
                for count in GPU_COUNTS:
                    block = gpu.get(_alias(cloud, count)) or {}
                    total = block.get("uninterruptablePrice")
                    if total is None:
                        continue  # not offered at this size in this cloud
                    total = Decimal(str(total))
                    cells.append(dict(gpu=gpu_id, cloud=cloud, count=count, total=total, per_gpu=total / count,
                                      word=block.get("stockStatus"), vcpu=block.get("minVcpu"),
                                      ram=block.get("minMemory"), disk=block.get("minDisk"), source=ENDPOINT,
                                      at=snapshot.fetched_at))
    elif kind == "rest":
        for url, snapshot in snaps.items():
            cloud, count = CATALOG_KEYS[url]
            gpus = snapshot.payload.get("gpus") if isinstance(snapshot.payload, dict) else None
            if not isinstance(gpus, list):
                raise ValueError("runpod: REST catalog gpus is not a list - response reshaped")
            for gpu in gpus:
                gpu_id = gpu.get("id") if isinstance(gpu, dict) else None
                if not gpu_id:
                    continue
                word = gpu.get("availability")
                price = (gpu.get("price") or {}).get(cloud)
                # GraphQL prices a size only when a machine has it free; REST prices every type, so the stock word
                # decides, as it did there. A zero price is RunPod's "not sold in this cloud", never a price.
                if word not in STOCK_TIER or price is None or Decimal(str(price)) <= 0:
                    continue
                per_gpu = Decimal(str(price))
                cells.append(dict(gpu=gpu_id, cloud=cloud, count=count, total=per_gpu * count, per_gpu=per_gpu,
                                  word=word, vcpu=gpu.get("minVcpuCount"), ram=gpu.get("minRam"), disk=None,
                                  source=url, at=snapshot.fetched_at))
    return cells


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
    return [
        ComputeListing(
            provider="runpod",
            sku=f"{c['gpu']}:{c['cloud']}",
            listing_id=f"{c['gpu']}:{c['cloud']}:{c['count']}",
            raw_gpu_name=c["gpu"],
            canonical_gpu_name=canonical.canonical_gpu_name(c["gpu"]),
            gpu_count=c["count"],
            # REST lists per-datacenter stock (dataCenters) but not per-datacenter prices; GraphQL needed one request
            # per datacenter. Community cloud has no datacenter at all. Not modelled yet.
            region=None,
            country=None,
            price_per_gpu_hour=c["per_gpu"],
            price_per_instance_hour=c["total"],
            currency="USD",
            # The GraphQL spot fields duplicate on-demand exactly and REST v2 has no spot pods: one market.
            market_type="on_demand",
            provider_tier=c["cloud"],
            interruptible=c["cloud"] == "community",
            available=c["word"] not in (None, *NO_STOCK),
            # The stock word is a High/Medium/Low bucket, not a count.
            capacity=None,
            capacity_unit=None,
            vcpu=c["vcpu"],
            ram_gb=c["ram"],
            storage_gb=c["disk"],
            observed_at=c["at"],
        )
        for c in _pod_cells(by_endpoint)
    ]
