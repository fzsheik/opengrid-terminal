"""Hyperstack (NexGen Cloud Infrahub): VMs priced per GPU-hour, on-demand and spot.

Capacity: /core/stocks gives, per region and GPU model, `configurations` (deployable VMs per size, e.g.
{"1x": 4, "2x": 2, "4x": 1} for 4 free GPUs) and `available` (GPUs, a floor estimate such as "10+"). Each
GPU flavor gets one row: in_stock from the flavor's own `stock_available` flag, available_count from the
configurations count for its size, in VMs.
"""

import asyncio
import logging
from typing import TYPE_CHECKING

import httpx

import canonical
from models import CapacityObservation, ComputeListing, RawResponse
from providers.base import Provider, PollingPolicy, find_endpoint, to_decimal

if TYPE_CHECKING:
    from tables import RawSnapshot

log = logging.getLogger(__name__)

BASE_URL = "https://infrahub-api.nexgencloud.com/v1"


class HyperstackProvider(Provider):
    name = "hyperstack"
    # 5 requests per poll against 500/min per IP.
    polling = PollingPolicy(interval_seconds=900, timeout_seconds=30)

    def __init__(self, api_key: str, client: httpx.AsyncClient | None = None):
        super().__init__(
            client
            or httpx.AsyncClient(
                base_url=BASE_URL,
                headers={"api_key": api_key, "accept": "application/json"},
                timeout=30,
            )
        )

    async def fetch(self) -> list[RawResponse]:
        # Rate limit is 500/min per IP, so five parallel calls is comfortable.
        return list(
            await asyncio.gather(
                self.get("/core/flavors"),
                self.get("/pricebook"),
                self.get("/core/stocks"),
                self.get("/core/regions"),
                self.get("/core/gpus"),
            )
        )

    @staticmethod
    def normalize(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        """Listings come from /core/flavors, NOT from the stock configurations
        map: that map reports sizes such as 10x H100 for which no flavor exists.
        """
        flavor_rows = find_endpoint(by_endpoint, "/core/flavors")
        if not flavor_rows:
            return []
        snapshot = flavor_rows[0]

        price_rows = find_endpoint(by_endpoint, "/pricebook")
        pricebook = price_rows[0].payload if price_rows else []
        prices = {
            entry["name"]: to_decimal(entry.get("value"))
            for entry in (pricebook if isinstance(pricebook, list) else [])
            if entry.get("name")
        }

        # (region, gpu api name, gpu count) -> deployable VMs
        capacity: dict[tuple[str, str, int], int] = {}
        stock_rows = find_endpoint(by_endpoint, "/core/stocks")
        if stock_rows:
            for region_stock in (stock_rows[0].payload or {}).get("stocks") or []:
                region = region_stock.get("region")
                for model in region_stock.get("models") or []:
                    for size, count in (model.get("configurations") or {}).items():
                        try:
                            capacity[(region, model["model"], int(size.rstrip("xX")))] = int(count)
                        except (ValueError, KeyError):
                            continue

        country_by_region: dict[str, str] = {}
        region_rows = find_endpoint(by_endpoint, "/core/regions")
        if region_rows:
            for region in (region_rows[0].payload or {}).get("regions") or []:
                if region.get("name"):
                    country_by_region[region["name"]] = region.get("country")

        listings = []
        unpriced: set[str] = set()
        for group in (snapshot.payload or {}).get("data") or []:
            for flavor in group.get("flavors") or []:
                gpu_count = flavor.get("gpu_count") or 0
                if not gpu_count:
                    continue  # CPU-only flavor
                api_name = flavor.get("gpu") or ""
                region = flavor.get("region_name")
                price = prices.get(api_name)
                if price is None:
                    unpriced.add(api_name)

                spot = api_name.lower().endswith("-spot")
                listings.append(
                    ComputeListing(
                        provider="hyperstack",
                        sku=flavor["name"],
                        listing_id=f"{region}:{flavor['name']}",
                        raw_gpu_name=api_name,
                        canonical_gpu_name=canonical.canonical_gpu_name(api_name),
                        gpu_count=gpu_count,
                        region=region,
                        country=country_by_region.get(region),
                        price_per_gpu_hour=price,
                        # GPUs bill per GPU; vCPU, RAM and root disk are included.
                        price_per_instance_hour=None if price is None else price * gpu_count,
                        currency="USD",
                        market_type="spot" if spot else "on_demand",
                        provider_tier=None,
                        interruptible=spot,
                        # The flavor's own flag beats the coarser stock count.
                        available=flavor.get("stock_available"),
                        # Sibling flavors differing only by disk share this number.
                        capacity=capacity.get((region, api_name, gpu_count)),
                        capacity_unit="instance",
                        vcpu=flavor.get("cpu"),
                        ram_gb=flavor.get("ram"),
                        storage_gb=flavor.get("disk"),
                        observed_at=snapshot.fetched_at,
                    )
                )
        if unpriced:
            log.warning("hyperstack: no pricebook entry for %s", sorted(unpriced))
        return listings

    @staticmethod
    def normalize_capacity(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[CapacityObservation]:
        flavor_rows, stock_rows = find_endpoint(by_endpoint, "/core/flavors"), find_endpoint(by_endpoint, "/core/stocks")
        if not flavor_rows or not stock_rows:
            return []
        stocks = (stock_rows[0].payload or {}).get("stocks")
        if not isinstance(stocks, list):
            raise ValueError("hyperstack: /core/stocks has no `stocks` list - response reshaped")
        vms: dict[tuple[str, str, int], int] = {}
        model_word: dict[tuple[str, str], str] = {}
        for region_stock in stocks:
            region = region_stock.get("region")
            for model in region_stock.get("models") or []:
                model_word[(region, model.get("model"))] = str(model.get("available"))
                for size, count in (model.get("configurations") or {}).items():
                    try:
                        vms[(region, model["model"], int(size.rstrip("xX")))] = int(count)
                    except (ValueError, KeyError, TypeError):
                        continue
        region_rows = find_endpoint(by_endpoint, "/core/regions")
        country = {r.get("name"): r.get("country")
                   for r in ((region_rows[0].payload or {}).get("regions") or [] if region_rows else [])}
        out = []
        for group in (flavor_rows[0].payload or {}).get("data") or []:
            for f in group.get("flavors") or []:
                n, api_name, region = f.get("gpu_count") or 0, f.get("gpu") or "", f.get("region_name")
                if not n or not f.get("name"):
                    continue
                flag, count = f.get("stock_available"), vms.get((region, api_name, n))
                out.append(CapacityObservation(
                    provider="hyperstack", sku=f["name"], region=region, country=country.get(region),
                    raw_gpu_name=api_name, canonical_gpu_name=canonical.canonical_gpu_name(api_name), gpu_count=n,
                    in_stock=flag if isinstance(flag, bool) else None, available_count=count,
                    count_unit=None if count is None else "instance",
                    raw_value=f"stock_available={flag}; {n}x VMs={count}; model GPUs={model_word.get((region, api_name))}",
                    source="/core/stocks", observed_at=stock_rows[0].fetched_at))
        return out
