"""Verda (formerly DataCrunch): the public instance-type catalogue, no API key.

One anonymous endpoint lists every instance type with an on-demand price and a
spot price, both per whole instance. Availability needs a login, so stock is
unknown here.
"""

import logging
from typing import TYPE_CHECKING

import httpx

import canonical
from models import ComputeListing, RawResponse
from providers.base import PollingPolicy, Provider, find_endpoint, to_decimal

if TYPE_CHECKING:
    from tables import RawSnapshot

log = logging.getLogger(__name__)

BASE_URL = "https://api.verda.com"
ENDPOINT = "/v1/instance-types"


class VerdaProvider(Provider):
    name = "verda"
    # One request per poll against an anonymous endpoint.
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
        return [await self.get(ENDPOINT)]

    @staticmethod
    def normalize(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        """One instance type becomes an on-demand listing and a spot listing."""
        rows = find_endpoint(by_endpoint, ENDPOINT)
        if not rows:
            return []
        snapshot = rows[0]
        if not isinstance(snapshot.payload, list):
            log.warning("verda: unexpected payload shape %s", type(snapshot.payload).__name__)
            return []

        listings = []
        for it in snapshot.payload:
            count = (it.get("gpu") or {}).get("number_of_gpus") or 0
            if not count:
                continue  # CPU-only instance type
            if (it.get("currency") or "").lower() != "usd":
                log.warning("verda: %s is priced in %r, not USD; skipped", it.get("instance_type"), it.get("currency"))
                continue
            raw_name = it.get("name")
            itype = it["instance_type"]
            storage = it.get("storage") or {}
            for market, price_key, suffix in (("on_demand", "price_per_hour", ""), ("spot", "spot_price", ":spot")):
                price = to_decimal(it.get(price_key))
                if price is None or price <= 0:
                    continue
                listings.append(
                    ComputeListing(
                        provider="verda",
                        sku=itype,
                        listing_id=itype + suffix,
                        raw_gpu_name=raw_name,
                        canonical_gpu_name=canonical.canonical_gpu_name(raw_name),
                        gpu_count=count,
                        region=None,
                        country=None,
                        # Both prices are for the whole instance.
                        price_per_gpu_hour=price / count,
                        price_per_instance_hour=price,
                        currency="USD",
                        market_type=market,
                        # ".CC" types run in confidential-computing mode.
                        provider_tier="confidential" if itype.endswith(".CC") else None,
                        interruptible=market == "spot",
                        # Availability needs an authenticated call: unknown, not sold out.
                        available=None,
                        capacity=None,
                        capacity_unit=None,
                        vcpu=(it.get("cpu") or {}).get("number_of_cores"),
                        ram_gb=(it.get("memory") or {}).get("size_in_gigabytes"),
                        storage_gb=storage.get("size_in_gigabytes") if isinstance(storage, dict) else None,
                        observed_at=snapshot.fetched_at,
                    )
                )
        return listings
