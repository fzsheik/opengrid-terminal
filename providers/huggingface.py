"""Hugging Face Inference Endpoints: its public catalogue of AWS/GCP/Azure instances, no key. Resold capacity.

GET https://api.endpoints.huggingface.cloud/v2/provider (no auth) returns vendors ("aws", "gcp", "azure"),
each with regions, each with "computes": id ("aws-us-east-1-nvidia-a100-x2"), accelerator ("gpu" | "cpu" |
"neuron"), numAccelerators, numCpus, memoryGb, gpuMemoryGb (all GPUs together), architecture ("Nvidia
A100"), status ("available" | "not_available" | "deprecated") and pricePerHour (USD, the whole instance:
A100 x2 = 5.0 = 2 x 2.5).

Read: every GPU compute that is not deprecated. Per-GPU = pricePerHour / numAccelerators. CPU and AWS
Inferentia ("neuron") computes are skipped. `available` is status == "available" (and the region's and
vendor's own status); the same word is the capacity row (boolean shape, one per compute).

Resale: these are the underlying cloud's GPUs (AWS, GCP, Azure: clouds OpenGrid reads directly) at Hugging
Face's managed-endpoint price. Each listing records the cloud in provider_tier "resold:<vendor>" (and in the
sku), so it is stored and shown but never votes in the market view, rollups, indices or routing validation
(market.RESOLD_PREFIX): counting it would count AWS's or Google's capacity a second time. GPU.ai is tagged
the same way. The GPU name carries the vendor ("aws Nvidia A100 80GB"), because the form factor follows
from the cloud's instance family (AWS's only 80 GB A100 is p4de, SXM4; GCP's A100 80GB is a2-ultragpu,
SXM4, its H100 a3, SXM5; both clouds' H200 are SXM).

A response without a `vendors` list, or no GPU compute with a price, fails the normalize.

Terms: huggingface.co's terms of service have no clause on automated access or scraping, and its robots.txt
allows everything; the endpoint host serves no robots.txt (404). The catalogue changes rarely: polled every
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

BASE_URL = "https://api.endpoints.huggingface.cloud"
PROVIDERS_EP = "/v2/provider"
REGION_COUNTRY = {"eu-west-1": "IE", "eu-west-2": "GB", "eu-west-3": "FR", "eu-central-1": "DE",
                  "eastus": "US", "eastus2": "US", "westus": "US", "westus2": "US", "westus3": "US",
                  "europe-west4": "NL", "europe-west1": "BE"}


def country_of(region: str | None) -> str | None:
    r = region or ""
    return "US" if r.startswith("us-") else REGION_COUNTRY.get(r)


def _computes(snapshot):
    """(vendor, region, compute, up) for every compute; up = vendor and region both say available."""
    body = snapshot.payload
    vendors = body.get("vendors") if isinstance(body, dict) else None
    if not isinstance(vendors, list):
        raise ValueError("huggingface: /v2/provider has no `vendors` list - response reshaped")
    for v in vendors:
        for r in (v.get("regions") or []) if isinstance(v, dict) else []:
            for c in (r.get("computes") or []) if isinstance(r, dict) else []:
                if isinstance(c, dict):
                    up = v.get("status") == "available" and r.get("status") == "available"
                    yield v.get("name"), r.get("name"), c, up


def _gpu(vendor, c) -> tuple[str, int] | None:
    n, mem, arch = c.get("numAccelerators"), c.get("gpuMemoryGb"), c.get("architecture")
    if c.get("accelerator") != "gpu" or c.get("status") == "deprecated" or not vendor or not c.get("id"):
        return None
    if not isinstance(n, int) or n < 1 or not isinstance(mem, (int, float)) or mem <= 0 or not arch:
        return None
    per = mem / n
    return f"{vendor} {arch} {int(per) if per == int(per) else per}GB", n


class HuggingFaceProvider(Provider):
    name = "huggingface"
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
        return [await self.get(PROVIDERS_EP)]  # ~20 KB, stored verbatim

    @staticmethod
    def normalize(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        rows = find_endpoint(by_endpoint, PROVIDERS_EP)
        if not rows:
            return []
        snapshot = rows[0]
        listings = []
        for vendor, region, c, up in _computes(snapshot):
            gpu = _gpu(vendor, c)
            price = to_decimal(c.get("pricePerHour"))
            if gpu is None or price is None or price <= 0:
                continue
            name, n = gpu
            cpus, mem = c.get("numCpus"), c.get("memoryGb")
            listings.append(
                ComputeListing(
                    provider="huggingface",
                    sku=c["id"],
                    listing_id=c["id"],
                    raw_gpu_name=name,
                    canonical_gpu_name=canonical.canonical_gpu_name(name),
                    gpu_count=n,
                    region=region,
                    country=country_of(region),
                    price_per_gpu_hour=price / n,
                    price_per_instance_hour=price,
                    currency="USD",
                    market_type="on_demand",
                    provider_tier=f"resold:{vendor}"[:24],
                    interruptible=False,
                    available=up and c.get("status") == "available",
                    capacity=None,
                    capacity_unit=None,
                    vcpu=cpus if isinstance(cpus, int) else None,
                    ram_gb=float(mem) if isinstance(mem, (int, float)) else None,
                    storage_gb=None,
                    observed_at=snapshot.fetched_at,
                )
            )
        if not listings:
            raise ValueError("huggingface: no priced GPU compute - not 'nothing for sale'")
        return listings

    @staticmethod
    def normalize_capacity(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[CapacityObservation]:
        rows = find_endpoint(by_endpoint, PROVIDERS_EP)
        if not rows:
            return []
        out = []
        for vendor, region, c, up in _computes(rows[0]):
            gpu = _gpu(vendor, c)
            if gpu is None:
                continue
            out.append(CapacityObservation(
                provider="huggingface", sku=c["id"], region=region, country=country_of(region), raw_gpu_name=gpu[0],
                canonical_gpu_name=canonical.canonical_gpu_name(gpu[0]), gpu_count=gpu[1],
                in_stock=up and c.get("status") == "available", raw_value=str(c.get("status")),
                source=PROVIDERS_EP, observed_at=rows[0].fetched_at))
        return out
