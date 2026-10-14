"""IBM Cloud: VPC GPU virtual-server list prices from the Global Catalog, no key.

The Global Catalog's pricing endpoints may be called unauthenticated for public
services (IBM Platform Services SDK docs). GET /api/v1/{plan id}/pricing/deployment
returns every regional deployment of one instance-profile plan, each with its
metrics: INSTANCE_HOURS_MULTI_TENANT is the on-demand price of the WHOLE virtual
server per hour, INSTANCE_MULTI_TENANT_SPOT the spot price. Amounts are listed
per billing country; the "USA" (USD) entry is read.

The GPU model and count come from PROFILES below, hand-maintained from IBM's VPC
GPU profile documentation (which states SXM5 for H100/H200 and PCIe for A100).
A plan missing from that table is never guessed. Dedicated-host, reservation
and OS-licence metrics are not read.

IBM is a general-purpose cloud, so it never counts toward neocloud indices.
"""

import logging
from typing import TYPE_CHECKING

import httpx

import canonical
from models import ComputeListing, RawResponse
from providers.base import PollingPolicy, Provider, find_endpoint, gather_limited, reshaped, to_decimal

if TYPE_CHECKING:
    from tables import RawSnapshot

log = logging.getLogger(__name__)

BASE_URL = "https://globalcatalog.cloud.ibm.com"

# Global Catalog plan id -> (instance profile, GPUs, GPU name per IBM's profile docs).
PROFILES: dict[str, tuple[str, int, str]] = {
    "6bed29d7-67ba-43ca-83d0-9576a379a1ce": ("gx3-16x80x1l4", 1, "NVIDIA L4 24GB"),
    "fc628776-aec4-4049-8236-dbc1cac4baa8": ("gx3-32x160x2l4", 2, "NVIDIA L4 24GB"),
    "273e8cef-e5e9-4b7d-bd27-94acf8c24302": ("gx3-64x320x4l4", 4, "NVIDIA L4 24GB"),
    "aff9c001-4a97-468f-854e-50caaca168ae": ("gx3-24x120x1l40s", 1, "NVIDIA L40S 48GB"),
    "a5d31acf-ef30-411d-909a-377df771415c": ("gx3-48x240x2l40s", 2, "NVIDIA L40S 48GB"),
    "fb875111-9688-4b8e-8ae6-e5e74497662f": ("gx3d-24x120x1a100p", 1, "NVIDIA A100 80GB PCIe"),
    "54351d01-2e3f-4a0f-bcad-8e9328ac98cd": ("gx3d-48x240x2a100p", 2, "NVIDIA A100 80GB PCIe"),
    "99548819-fcbc-486c-8eeb-0355a56b035e": ("gx3d-160x1792x8h100", 8, "NVIDIA H100 SXM5 80GB"),
    "473375e7-f226-4deb-9214-dd68607b3d99": ("gx3d-160x1792x8h200", 8, "NVIDIA H200 SXM5 141GB"),
    "b029541d-cd56-42c5-a922-7288ed9b465f": ("gx3d-160x1792x8gaudi3", 8, "Intel Gaudi 3 128GB"),
}

ON_DEMAND = "INSTANCE_HOURS_MULTI_TENANT"
SPOT = "INSTANCE_MULTI_TENANT_SPOT"
COUNTRY = "USA"


def endpoint(plan_id: str) -> str:
    return f"/api/v1/{plan_id}/pricing/deployment"


def _metric_price(metric: dict, profile: str):
    """The single USD instance-hour price in a metric, or ValueError."""
    if metric.get("charge_unit") != "Instance-Hour":
        raise ValueError(f"ibm: {profile} {metric.get('charge_unit_name')} is not per Instance-Hour")
    if profile not in (metric.get("metric_id") or ""):
        raise ValueError(f"ibm: metric {metric.get('metric_id')!r} is not for profile {profile}")
    usa = [a for a in metric.get("amounts") or [] if a.get("country") == COUNTRY]
    if len(usa) != 1 or usa[0].get("currency") != "USD":
        raise ValueError(f"ibm: {profile} has no single USA/USD amount")
    prices = usa[0].get("prices") or []
    if len(prices) != 1 or prices[0].get("quantity_tier") != 1:
        raise ValueError(f"ibm: {profile} price is tiered: {prices!r}")
    price = to_decimal(prices[0].get("price"))
    if price is None:
        raise ValueError(f"ibm: {profile} price is not a number: {prices[0].get('price')!r}")
    return price


def read_deployments(plan_id: str, payload) -> list[dict]:
    """[{region, on_demand, spot}] for one plan, or ValueError when the shape changed."""
    profile = PROFILES[plan_id][0]
    if not isinstance(payload, dict) or not isinstance(payload.get("resources"), list):
        raise ValueError(f"ibm: {profile} pricing has no 'resources' list - API reshaped")
    out = []
    for r in payload["resources"]:
        region = r.get("deployment_location")
        if not region or not str(r.get("deployment_id", "")).startswith(plan_id):
            raise ValueError(f"ibm: {profile} deployment lost its id or location")
        metrics = {m.get("charge_unit_name"): m for m in r.get("metrics") or []}
        if ON_DEMAND not in metrics:
            raise ValueError(f"ibm: {profile} {region} has no {ON_DEMAND} metric")
        out.append({
            "region": region,
            "on_demand": _metric_price(metrics[ON_DEMAND], profile),
            "spot": _metric_price(metrics[SPOT], profile) if SPOT in metrics else None,
        })
    if not out:
        raise ValueError(f"ibm: {profile} has no deployments")
    return out


def extract(plan_id: str, payload) -> dict:
    """The deployments of one plan with only the metrics read, verbatim except
    for the amounts of other billing countries. ValueError on reshape."""
    read_deployments(plan_id, payload)
    if (payload.get("count") or 0) > len(payload["resources"]):
        raise ValueError(f"ibm: {PROFILES[plan_id][0]} deployments are paginated - not all were returned")
    keep = []
    for r in payload["resources"]:
        metrics = [
            {**m, "amounts": [a for a in m.get("amounts") or [] if a.get("country") == COUNTRY]}
            for m in r.get("metrics") or []
            if m.get("charge_unit_name") in (ON_DEMAND, SPOT)
        ]
        keep.append({k: r.get(k) for k in ("deployment_id", "deployment_location", "effective_from",
                                           "effective_until")} | {"metrics": metrics})
    return {"count": payload.get("count"), "resources": keep}


class IbmProvider(Provider):
    name = "ibm"
    # Ten anonymous requests per poll; list prices change monthly.
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
        plan_ids = list(PROFILES)
        raws = await gather_limited([lambda p=p: self.get(endpoint(p)) for p in plan_ids], limit=4)
        out = []
        for plan_id, raw in zip(plan_ids, raws):
            if not raw.ok:
                out.append(raw)
                continue
            try:
                out.append(reshaped(raw, extract(plan_id, raw.payload)))
            except ValueError as exc:
                out.append(raw.model_copy(update={"ok": False, "payload": None, "error": f"ValueError: {exc}"[:500]}))
        return out

    @staticmethod
    def normalize(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        """One listing per profile, region and market (on-demand, spot)."""
        listings = []
        for plan_id, (profile, count, raw_name) in PROFILES.items():
            rows = find_endpoint(by_endpoint, endpoint(plan_id))
            if not rows:
                continue
            snapshot = rows[0]
            by_region: dict[str, list[dict]] = {}
            for d in read_deployments(plan_id, snapshot.payload):
                by_region.setdefault(d["region"], []).append(d)
            for region, deps in by_region.items():
                # A region can list several deployments (zones); they must agree.
                if len({(d["on_demand"], d["spot"]) for d in deps}) != 1:
                    log.warning("ibm: %s %s deployments disagree on price; skipped", profile, region)
                    continue
                d = deps[0]
                for market, price in (("on_demand", d["on_demand"]), ("spot", d["spot"])):
                    if price is None or price <= 0:
                        continue
                    spot = market == "spot"
                    listings.append(
                        ComputeListing(
                            provider="ibm",
                            sku=profile,
                            listing_id=f"{profile}:{region}" + (":spot" if spot else ""),
                            raw_gpu_name=raw_name,
                            canonical_gpu_name=canonical.canonical_gpu_name(raw_name),
                            gpu_count=count,
                            region=region,
                            country=None,
                            price_per_gpu_hour=price / count,
                            price_per_instance_hour=price,
                            currency="USD",
                            market_type=market,
                            provider_tier="vm",
                            interruptible=spot,
                            # The catalog lists where a profile is sold, not live stock.
                            available=None,
                            capacity=None,
                            capacity_unit=None,
                            vcpu=int(profile.split("-")[1].split("x")[0]),
                            ram_gb=float(profile.split("-")[1].split("x")[1]),
                            storage_gb=None,
                            observed_at=snapshot.fetched_at,
                        )
                    )
        return listings
