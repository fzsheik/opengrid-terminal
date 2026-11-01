"""Akash Network: the Akash Console's public GPU market summary, one call, no key.

GET https://console-api.akash.network/v1/gpu-prices (security: none; served with a 120 s cache and
memoized 15 minutes server-side) returns, per GPU (vendor, model, ram, interface):
    availability          {total, available}   GPUs on the network (allocatable, and not yet allocated)
    providerAvailability  {total, available}   providers hosting that GPU
    price                 {currency: "USD", min, max, avg, weightedAverage, med}   per GPU-hour, or null
    priceUakt             the same in micro-AKT (not read)

What the price is (the Console source, apps/api/src/gpu/services/gpu-price/gpu-price.service.ts,
read 2026-11-01; its own comment: "derived from recent bids made on the network. This is a
temporary solution"): Akash has no price list. For every provider that hosts the GPU the Console
picks ONE bid from the last 31 days: the newest bid on its pricing bot's tiny single-GPU order
(0.1 CPU), else the cheapest single-GPU bid with the same RAM and interface in the last 14 days,
else with the same RAM, else the cheapest bid for any variant of the model, then the same steps
over 31 days. uakt bids are converted at that day's AKT price. min/max/avg/med are over those
per-provider bids; weightedAverage weights each by the provider's allocatable GPUs.

So this is a market summary, modelled as one: each GPU row becomes ONE listing at weightedAverage
(the price a GPU drawn at random from the network's supply would cost), provider_tier "weighted",
gpu_count 1; min, max, avg and med are not stored as listings (a single provider's bid, like Vast's
cheapest row, is not the market) but stay in the stored raw response. Known limits, stated rather
than hidden: a provider's bid can be up to 31 days old, and when it has no bid on the exact variant
the cheapest bid on another RAM/interface of the same model stands in (Akash's rule, which OpenGrid
cannot undo from the summary). That is why Akash was declined on 2026-10-13 and 2026-10-21; it is
added on 2026-11-01 with those limits documented here and in mapping.py. Bids include the bot
order's tiny CPU/RAM; a lease runs until either side closes it (not preempted by a higher bid).

Capacity (normalize_capacity, count shape, provider-wide): availability.available of
availability.total GPUs per row, the same figure as the listing's `capacity`. A row with no price
(no bid in 31 days) gets a capacity row and no listing.

A response without a `models` list, or with no priced row at all, fails the normalize: never
"nothing for sale".

Terms: akash.network's robots.txt allows all crawlers (and names AI crawlers as welcome); no terms of
service page governs the Console API, which is open source and documented as unauthenticated
(security: none). Polled every 15 minutes, the server-side memo period.
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

BASE_URL = "https://console-api.akash.network"
PRICES = "/v1/gpu-prices"


def raw_name(m: dict) -> str:
    """The GPU as Akash names it: 'nvidia h100 80Gi SXM5'."""
    return " ".join(str(m.get(k) or "").strip() for k in ("vendor", "model", "ram", "interface")).strip()


def sku(m: dict) -> str:
    return "-".join(str(m.get(k) or "").strip() for k in ("vendor", "model", "ram", "interface"))


def _models(snapshot) -> list[dict]:
    body = snapshot.payload
    models = body.get("models") if isinstance(body, dict) else None
    if not isinstance(models, list):
        raise ValueError("akash: gpu-prices has no `models` list - response reshaped")
    for m in models:
        if not isinstance(m, dict) or not m.get("model") or not isinstance(m.get("availability"), dict):
            raise ValueError(f"akash: model row lost its name or availability: {m!r}"[:200])
    return models


def _counts(m: dict) -> tuple[int | None, int | None]:
    a = m["availability"]
    total, free = a.get("total"), a.get("available")
    ok = all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in (total, free))
    return (free, total) if ok and free <= total else (None, None)


class AkashProvider(Provider):
    name = "akash"
    # One small GET; the Console memoizes the answer for 15 minutes, so polling faster reads the same body.
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
        # ~9 KB, stored verbatim (min/max/med/avg and the uakt prices stay in raw).
        return [await self.get(PRICES)]

    @staticmethod
    def normalize(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        """One listing per GPU row: the allocatable-weighted average of each provider's chosen bid."""
        rows = find_endpoint(by_endpoint, PRICES)
        if not rows:
            return []
        snapshot = rows[0]
        listings = []
        for m in _models(snapshot):
            price = m.get("price")
            if price is None:
                continue  # no bid in 31 days: capacity only
            if not isinstance(price, dict) or price.get("currency") != "USD":
                raise ValueError(f"akash: price for {sku(m)} is not a USD summary: {price!r}"[:200])
            weighted = to_decimal(price.get("weightedAverage"))
            if weighted is None or weighted <= 0:
                log.warning("akash: %s has no positive weightedAverage (%r); skipped", sku(m), price)
                continue
            free, total = _counts(m)
            name = raw_name(m)
            listings.append(
                ComputeListing(
                    provider="akash",
                    sku=sku(m),
                    listing_id=sku(m),
                    raw_gpu_name=name,
                    canonical_gpu_name=canonical.canonical_gpu_name(name),
                    gpu_count=1,
                    region=None,
                    country=None,
                    price_per_gpu_hour=weighted,
                    price_per_instance_hour=weighted,
                    currency="USD",
                    market_type="on_demand",
                    provider_tier="weighted",
                    interruptible=False,
                    available=None if free is None else free > 0,
                    capacity=free,
                    capacity_unit=None if free is None else "gpu",
                    vcpu=None,
                    ram_gb=None,
                    storage_gb=None,
                    observed_at=snapshot.fetched_at,
                )
            )
        if not listings:
            raise ValueError("akash: gpu-prices has no priced GPU row - not 'nothing for sale'")
        return listings

    @staticmethod
    def normalize_capacity(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[CapacityObservation]:
        """One provider-wide row per GPU: free (allocatable - allocated) of allocatable GPUs."""
        rows = find_endpoint(by_endpoint, PRICES)
        if not rows:
            return []
        snapshot = rows[0]
        out = []
        for m in _models(snapshot):
            free, total = _counts(m)
            a, p = m["availability"], m.get("providerAvailability") or {}
            name = raw_name(m)
            out.append(CapacityObservation(
                provider="akash", sku=sku(m), region=None, raw_gpu_name=name,
                canonical_gpu_name=canonical.canonical_gpu_name(name), gpu_count=1,
                in_stock=None if free is None else free > 0, available_count=free, total_count=total,
                count_unit=None if free is None else "gpu",
                raw_value=f"available={a.get('available')}; total={a.get('total')}; "
                          f"providers_available={p.get('available')}; providers={p.get('total')}",
                source=PRICES, observed_at=snapshot.fetched_at))
        return out

