"""Clouds with no API of their own, read through Shadeform's public catalogue.

Shadeform resells other clouds' capacity and publishes every instance type, with
its real cloud named in `cloud`, from one anonymous endpoint. Crusoe, Latitude.sh
and Denvr have no usable public API of their own, so each is read from here and
labelled "via-shadeform". The prices are Shadeform's listing of them, not the
cloud's own page: where we can compare (Hyperstack, DigitalOcean, Voltage Park)
they match, and where a cloud has just repriced Shadeform can lag.

A cloud that has its own feed in this app must never also be read through here:
that would count one cloud's capacity twice.

Each subclass fetches the whole catalogue and keeps only its own cloud's rows.
"""

import logging
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx

import canonical
from models import ComputeListing, RawResponse
from providers.base import PollingPolicy, Provider, find_endpoint, reshaped

if TYPE_CHECKING:
    from tables import RawSnapshot

log = logging.getLogger(__name__)

BASE_URL = "https://api.shadeform.ai"
ENDPOINT = "/v1/instances/types"
REGION_MAX = 64  # compute_listings.region is varchar(64)


def gpu_label(row: dict) -> str:
    """A name that carries the variant, which Shadeform's gpu_type alone does not.

    Shadeform's interconnect field is sometimes wrong (a Crusoe `...sxm-ib` type
    is tagged pcie), so SXM in the instance name wins over the field.
    """
    names = (row.get("shade_instance_type") or "") + " " + (row.get("cloud_instance_type") or "")
    variant = "sxm" if "sxm" in names.lower() else (row.get("interconnect") or "").lower()
    parts = [row.get("gpu_type") or "", variant]
    if variant.startswith("pcie") and row.get("nvlink"):
        parts.append("nvlink")
    return " ".join(p for p in parts if p)


def regions_of(availability: list[dict], rental: str) -> tuple[bool | None, str | None, str | None]:
    """(in stock?, region text, country) for one rental type's availability entries."""
    entries = [a for a in availability if a.get("rental_type") == rental]
    if not entries:
        return None, None, None
    up = [a for a in entries if a.get("available")]
    shown = up or entries
    names = [a.get("display_name") or a.get("region") or "" for a in shown]
    text = "; ".join(n for n in names if n)
    if len(text) > REGION_MAX:
        text = text[: REGION_MAX - 1] + "…"
    country = "US" if names and all(n.startswith("US") for n in names) else None
    return bool(up), text or None, country


class ShadeformCloud(Provider):
    """One cloud, as listed by Shadeform. Subclasses set `name` and `cloud`."""

    cloud: str
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
        types = (raw.payload or {}).get("instance_types") if isinstance(raw.payload, dict) else None
        if types is None:
            return [raw]
        # Keep this cloud's rows only, verbatim: the rest is 18 other clouds.
        return [reshaped(raw, {"instance_types": [t for t in types if t.get("cloud") == self.cloud]})]

    @classmethod
    def normalize(cls, by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        """One instance type becomes one listing."""
        rows = find_endpoint(by_endpoint, ENDPOINT)
        if not rows:
            return []
        snapshot = rows[0]
        listings = []
        for t in (snapshot.payload or {}).get("instance_types") or []:
            count = t.get("num_gpus") or 0
            cents = t.get("hourly_price")
            if t.get("cloud") != cls.cloud or not count or cents is None:
                continue  # another cloud, a CPU-only type, or no price
            avail = t.get("availability") or []
            rentals = {a.get("rental_type") for a in avail}
            # A type offered only as spot is a spot listing.
            rental = "on_demand" if "on_demand" in rentals or not rentals else "spot"
            in_stock, region, country = regions_of(avail, rental)
            raw_name = gpu_label(t)
            price = Decimal(str(cents)) / 100  # integer cents per instance-hour
            listings.append(
                ComputeListing(
                    provider=cls.name,
                    sku=t.get("cloud_instance_type") or t["shade_instance_type"],
                    listing_id=t["shade_instance_type"],
                    raw_gpu_name=raw_name,
                    canonical_gpu_name=canonical.canonical_gpu_name(raw_name),
                    gpu_count=count,
                    region=region,
                    country=country,
                    price_per_gpu_hour=price / count,
                    price_per_instance_hour=price,
                    currency="USD",
                    market_type=rental,
                    provider_tier=f"{t.get('deployment_type') or 'vm'}/via-shadeform"[:24],
                    interruptible=rental == "spot",
                    available=in_stock,
                    capacity=None,
                    capacity_unit=None,
                    vcpu=t.get("vcpus"),
                    ram_gb=t.get("memory_in_gb"),
                    storage_gb=t.get("storage_in_gb"),
                    observed_at=snapshot.fetched_at,
                )
            )
        return listings


class CrusoeProvider(ShadeformCloud):
    name = "crusoe"
    cloud = "crusoe"


class LatitudeProvider(ShadeformCloud):
    name = "latitude"
    cloud = "latitude"


class DenvrProvider(ShadeformCloud):
    name = "denvr"
    cloud = "denvr"
