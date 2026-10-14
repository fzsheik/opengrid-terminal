"""Oracle Cloud (OCI): on-demand list prices for GPU shapes, no credentials.

Oracle publishes its whole price list as anonymous JSON (documented under
"Accessing List Pricing" in the OCI cost-estimation docs, which shows the curl
call). Each item is a part number with a pay-as-you-go price; GPU parts are
priced "GPU Per Hour", so the price IS the per-GPU rate. OCI list prices are the
same in every commercial region, so a listing has no region.

The price list names a GPU part ("Compute - GPU - H100"), not a shape and not a
GPU count. Which shapes bill against which part comes from Oracle's own compute
pricing page (oracle.com/cloud/compute/pricing, checked 2026-10-13) and lives in
SHAPES below, hand-maintained like canonical.py. A GPU part missing from that
table is not guessed: it is skipped and logged.

These are list prices. Preemptible (50% off) and capacity-reservation prices are
derived discounts the list does not publish per part, and are not read.
"""

import logging
from typing import TYPE_CHECKING

import httpx

import canonical
from models import ComputeListing, RawResponse
from providers.base import PollingPolicy, Provider, find_endpoint, reshaped, to_decimal

if TYPE_CHECKING:
    from tables import RawSnapshot

log = logging.getLogger(__name__)

BASE_URL = "https://apexapps.oracle.com"
ENDPOINT = "/pls/apex/cetools/api/v1/products/?currencyCode=USD"

# shape -> (part number, GPUs, GPU name in Oracle's words + printed VRAM).
# From the "GPU - Accelerated Compute" table on Oracle's pricing page, where each
# shape's price cell is bound to a part number. Several shapes share one part.
SHAPES: dict[str, tuple[str, int, str]] = {
    "BM.GPU.H100.8": ("B98415", 8, "NVIDIA H100 80GB"),
    "BM.GPU.H200.8": ("B110519", 8, "NVIDIA H200 141GB"),
    "BM.GPU.B200.8": ("B110978", 8, "NVIDIA B200 180GB"),
    "BM.GPU.B300.8": ("B112237", 8, "NVIDIA B300 263GB"),
    # GB200 / GB300 NVL72: each instance is two Grace superchips, 4 GPUs.
    "BM.GPU.GB200.4": ("B110979", 4, "NVIDIA GB200 189GB (NVL72)"),
    "BM.GPU.GB300.4": ("B112140", 4, "NVIDIA GB300 279GB (NVL72)"),
    "BM.GPU.MI300X.8": ("B109485", 8, "AMD MI300X 192GB"),
    "BM.GPU.MI355X.8": ("B111758", 8, "AMD MI355X 288GB"),
    "BM.GPU.A100-v2.8": ("B95907", 8, "NVIDIA A100 80GB"),
    "BM.GPU4.8": ("B92740", 8, "NVIDIA A100 40GB"),
    "BM.GPU.L40S.4": ("B109479", 4, "NVIDIA L40S 48GB"),
    "BM.GPU.RTXPRO.8": ("B112613", 8, "NVIDIA RTX PRO 6000 96GB"),
    "VM.GPU.A10.1": ("B95909", 1, "NVIDIA A10 24GB"),
    "VM.GPU.A10.2": ("B95909", 2, "NVIDIA A10 24GB"),
    "BM.GPU.A10.4": ("B95909", 4, "NVIDIA A10 24GB"),
    "VM.GPU3.1": ("B89734", 1, "NVIDIA Tesla V100 16GB"),
    "VM.GPU3.2": ("B89734", 2, "NVIDIA Tesla V100 16GB"),
    "VM.GPU3.4": ("B89734", 4, "NVIDIA Tesla V100 16GB"),
    "BM.GPU3.8": ("B89734", 8, "NVIDIA Tesla V100 16GB"),
}
PARTS = {part for part, _, _ in SHAPES.values()}

# GPU parts read on purpose as NOT a GPU rental: software licences billed per GPU,
# on-premises Cloud@Customer, VMware commits, edge devices, and the P100 shapes
# (B88517/B88518) that Oracle lists as "no longer available for new deployments".
_NOT_RENTAL = ("NVIDIA AI Enterprise", "Cloud@Customer", "VMware", "Roving Edge")
_LEGACY = {"B88517", "B88518"}


def _check_items(payload) -> list[dict]:
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise ValueError("oracle: price list has no 'items' list - API reshaped")
    return payload["items"]


def unit_price(item: dict):
    """The single pay-as-you-go USD price of a GPU part, or ValueError.

    Fails closed: another unit, a tiered price or a missing USD entry means the
    list changed under us, and a wrong price is worse than none.
    """
    part = item.get("partNumber")
    if item.get("metricName") != "GPU Per Hour":
        raise ValueError(f"oracle: {part} is no longer priced 'GPU Per Hour': {item.get('metricName')!r}")
    usd = [c for c in item.get("currencyCodeLocalizations") or [] if c.get("currencyCode") == "USD"]
    if len(usd) != 1:
        raise ValueError(f"oracle: {part} has no single USD price entry")
    prices = usd[0].get("prices") or []
    if len(prices) != 1 or prices[0].get("model") != "PAY_AS_YOU_GO" or "rangeMin" in prices[0]:
        raise ValueError(f"oracle: {part} is not one flat pay-as-you-go price: {prices!r}")
    price = to_decimal(prices[0].get("value"))
    if price is None:
        raise ValueError(f"oracle: {part} price is not a number: {prices[0].get('value')!r}")
    return price


def extract(payload) -> dict:
    """The price-list items for the parts in SHAPES, verbatim. ValueError on reshape."""
    items = _check_items(payload)
    kept = [i for i in items if i.get("partNumber") in PARTS]
    if not kept:
        raise ValueError("oracle: none of the known GPU parts are in the price list")
    for item in kept:
        unit_price(item)
    for i in items:
        name = i.get("displayName") or ""
        part = i.get("partNumber")
        if ("GPU" in name and part not in PARTS and part not in _LEGACY and part not in _SEEN_UNLISTED
                and not any(s in name for s in _NOT_RENTAL)):
            _SEEN_UNLISTED.add(part)
            log.info("oracle: GPU part %s (%s) is not in SHAPES; skipped", part, name)
    return {"lastUpdated": payload.get("lastUpdated"), "items": kept}


class OracleProvider(Provider):
    name = "oracle"
    # One request per poll; list prices change rarely.
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
        if not raw.ok:
            return [raw]
        try:
            payload = extract(raw.payload)
        except ValueError as exc:
            return [raw.model_copy(update={"ok": False, "payload": None, "error": f"ValueError: {exc}"[:500]})]
        # Keep the GPU parts only: the rest is ~650 unrelated services.
        return [reshaped(raw, payload)]

    @staticmethod
    def normalize(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        """One GPU shape becomes one listing, at its part's per-GPU price."""
        rows = find_endpoint(by_endpoint, "/cetools/api/v1/products/")
        if not rows:
            return []
        snapshot = rows[0]
        prices = {i.get("partNumber"): unit_price(i) for i in _check_items(snapshot.payload) if i.get("partNumber") in PARTS}
        listings = []
        for shape, (part, count, raw_name) in SHAPES.items():
            price = prices.get(part)
            if price is None or price <= 0:
                continue  # part withdrawn from the list, or free
            listings.append(
                ComputeListing(
                    provider="oracle",
                    sku=shape,
                    listing_id=shape,
                    raw_gpu_name=raw_name,
                    canonical_gpu_name=canonical.canonical_gpu_name(raw_name),
                    gpu_count=count,
                    # OCI list prices are global: one price in every commercial region.
                    region=None,
                    country=None,
                    price_per_gpu_hour=price,
                    price_per_instance_hour=price * count,
                    currency="USD",
                    market_type="on_demand",
                    provider_tier="bare-metal" if shape.startswith("BM.") else "vm",
                    interruptible=False,
                    # A price list says nothing about capacity.
                    available=None,
                    capacity=None,
                    capacity_unit=None,
                    vcpu=None,
                    ram_gb=None,
                    storage_gb=None,
                    observed_at=snapshot.fetched_at,
                )
            )
        return listings


_SEEN_UNLISTED: set[str] = set()
