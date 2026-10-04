"""AWS EC2: on-demand list prices for GPU instances, no credentials.

The public price file lists every EC2 instance type for one region and one
operating system. It carries vCPU, memory and price but NOT the GPU model or
count, so those come from GPU_INSTANCES below, hand-maintained like canonical.py.
An instance type missing from that table is not guessed: it is skipped and logged.

These are list prices. Large customers rarely pay them, which is why the
provider tier is "hyperscaler".
"""

import logging
import re
from typing import TYPE_CHECKING

import httpx

import canonical
from models import ComputeListing, RawResponse
from providers.base import PollingPolicy, Provider, find_endpoint, reshaped, to_decimal

if TYPE_CHECKING:
    from tables import RawSnapshot

log = logging.getLogger(__name__)

BASE_URL = "https://b0.p.awsstatic.com"
# us-east-1, Linux, shared tenancy, no extra software.
ENDPOINT = (
    "/pricing/2.0/meteredUnitMaps/ec2/USD/current/ec2-ondemand-without-sec-sel/"
    "US%20East%20%28N.%20Virginia%29/Linux/index.json"
)
REGION_NAME = "US East (N. Virginia)"
REGION = "us-east-1"

# Sizes of the small GPU families: GPUs per size.
_SMALL = {"xlarge": 1, "2xlarge": 1, "4xlarge": 1, "8xlarge": 1, "16xlarge": 1, "12xlarge": 4, "24xlarge": 4, "48xlarge": 8}


def _family(name: str, gpu: str, sizes: dict[str, int]) -> dict[str, tuple[int, str]]:
    return {f"{name}.{size}": (count, gpu) for size, count in sizes.items()}


# instance type -> (GPUs, GPU name as canonical.py knows it)
GPU_INSTANCES: dict[str, tuple[int, str]] = {
    # Training and inference flagships, 8 GPUs per full instance.
    "p3.2xlarge": (1, "Tesla V100-SXM2-16GB"),
    "p3.8xlarge": (4, "Tesla V100-SXM2-16GB"),
    "p3.16xlarge": (8, "Tesla V100-SXM2-16GB"),
    "p4d.24xlarge": (8, "NVIDIA A100-SXM4-40GB"),
    "p4de.24xlarge": (8, "NVIDIA A100-SXM4-80GB"),
    "p5.4xlarge": (1, "NVIDIA H100 80GB HBM3"),
    "p5.48xlarge": (8, "NVIDIA H100 80GB HBM3"),
    "p5e.48xlarge": (8, "NVIDIA H200"),
    "p5en.48xlarge": (8, "NVIDIA H200"),
    "p6-b200.48xlarge": (8, "NVIDIA B200"),
    "p6-b300.48xlarge": (8, "B300-SXM"),
    # Smaller GPUs; g4dn.metal is the only odd size.
    **_family("g4dn", "NVIDIA T4", {**_SMALL, "24xlarge": 0, "48xlarge": 0}),
    "g4dn.metal": (8, "NVIDIA T4"),
    **_family("g5", "NVIDIA A10G", _SMALL),
    **_family("g6", "NVIDIA L4", _SMALL),
    **_family("g6e", "NVIDIA L40S", _SMALL),
}
GPU_INSTANCES = {k: v for k, v in GPU_INSTANCES.items() if v[0] > 0}

_GIB = re.compile(r"^([\d.]+) GiB$")


def _keep(row: dict) -> dict:
    return {
        k: row.get(k)
        for k in ("Instance Type", "price", "vCPU", "Memory", "Operating System", "Pre Installed S/W", "License Model")
    }


class AwsProvider(Provider):
    name = "aws"
    # List prices change rarely and the file is large, so hourly is plenty.
    polling = PollingPolicy(interval_seconds=3600, timeout_seconds=60)

    def __init__(self, client: httpx.AsyncClient | None = None):
        super().__init__(
            client
            or httpx.AsyncClient(
                base_url=BASE_URL,
                headers={"accept": "application/json", "user-agent": "opengrid-terminal/0.1"},
                timeout=60,
            )
        )

    async def fetch(self) -> list[RawResponse]:
        raw = await self.get(ENDPOINT)
        regions = (raw.payload or {}).get("regions") if isinstance(raw.payload, dict) else None
        if not isinstance(regions, dict) or REGION_NAME not in regions:
            return [raw] if not raw.ok else [raw.model_copy(update={"ok": False, "error": "aws: region missing from price file", "payload": None})]
        # Keep the GPU instances only: the rest is 1,300 unrelated rows.
        rows = [_keep(r) for r in regions[REGION_NAME].values() if r.get("Instance Type") in GPU_INSTANCES]
        # An unlisted GPU family is a new product to add to the table, not to guess.
        for r in regions[REGION_NAME].values():
            t = r.get("Instance Type", "")
            if re.match(r"^(p\d|g\d|dl\d|trn|inf)", t) and t not in GPU_INSTANCES and t.split(".")[0] not in _SEEN_UNLISTED:
                _SEEN_UNLISTED.add(t.split(".")[0])
                log.info("aws: instance family %s is not in GPU_INSTANCES; skipped", t.split(".")[0])
        return [reshaped(raw, {"region": REGION_NAME, "rows": rows})]

    @staticmethod
    def normalize(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        """One GPU instance type becomes one listing."""
        rows = find_endpoint(by_endpoint, ENDPOINT)
        if not rows:
            return []
        snapshot = rows[0]
        listings = []
        for row in (snapshot.payload or {}).get("rows") or []:
            itype = row.get("Instance Type")
            spec = GPU_INSTANCES.get(itype)
            price = to_decimal(row.get("price"))
            if spec is None or price is None or price <= 0:
                continue
            count, raw_name = spec
            mem = _GIB.match(row.get("Memory") or "")
            listings.append(
                ComputeListing(
                    provider="aws",
                    sku=itype,
                    listing_id=f"{itype}:{REGION}",
                    raw_gpu_name=raw_name,
                    canonical_gpu_name=canonical.canonical_gpu_name(raw_name),
                    gpu_count=count,
                    region=REGION,
                    country="US",
                    price_per_gpu_hour=price / count,
                    price_per_instance_hour=price,
                    currency="USD",
                    market_type="on_demand",
                    provider_tier="hyperscaler",
                    interruptible=False,
                    # The price file says nothing about capacity.
                    available=None,
                    capacity=None,
                    capacity_unit=None,
                    vcpu=int(row["vCPU"]) if (row.get("vCPU") or "").isdigit() else None,
                    ram_gb=float(mem.group(1)) if mem else None,
                    storage_gb=None,
                    observed_at=snapshot.fetched_at,
                )
            )
        return listings


_SEEN_UNLISTED: set[str] = set()
