"""Microsoft Azure: pay-as-you-go list prices for GPU VMs, no credentials.

The Azure Retail Prices API (prices.azure.com/api/retail/prices) is anonymous and
documented by Microsoft for programmatic use (prices.azure.com has no robots.txt);
it answers ~10 requests a minute per caller (x-ms-ratelimit-* headers). One OData-filtered GET per region returns
every NC / ND / NV meter there: Consumption only (reserved and savings plans are
other priceTypes), Windows meters dropped by the filter. Today a region is ~190
rows on one page; NextPageLink is followed, but more than MAX_PAGES pages fails
the poll rather than recording a partial catalogue.

Each row prices a WHOLE VM size per hour. Rows carry the size (armSkuName) but
NOT the GPU model or count, so those come from GPU_SIZES below, copied from the
size pages on learn.microsoft.com, like AWS's GPU_INSTANCES. A size missing from
the table is logged and skipped, never guessed: that includes the fractional
sizes (1/6 of an A10, 1/4 of an RTX PRO 6000), which are not a whole GPU.

Meters ending "Spot" are a separate spot market (interruptible). "Low Priority"
meters are Azure Batch pricing and are never read.

Two regions: eastus (the AWS us-east-1 analogue) and eastus2, which carries the
ND H200 v5 and ND MI300X v5 sizes eastus does not.
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

BASE_URL = "https://prices.azure.com"
PATH = "/api/retail/prices"
REGIONS = ("eastus", "eastus2")
# Azure's own location label for these is "US East" / "US East 2".
COUNTRY = {"eastus": "US", "eastus2": "US"}
MAX_PAGES = 3


def odata_filter(region: str) -> str:
    return (
        f"serviceName eq 'Virtual Machines' and armRegionName eq '{region}' and priceType eq 'Consumption' "
        "and (startswith(armSkuName,'Standard_NC') or startswith(armSkuName,'Standard_ND') "
        "or startswith(armSkuName,'Standard_NV')) and contains(productName,'Windows') eq false"
    )


def endpoint(region: str) -> str:
    """The stored endpoint label. The encoded filter outgrows the 256-char endpoint
    column, so it is kept verbatim in the snapshot's `request` instead."""
    return f"{PATH}#{region}"


# The size pages' accelerator strings, verbatim. canonical.py maps the unambiguous ones.
T4 = "Nvidia Tesla T4 GPU (16GB)"
V100_16 = "Nvidia Tesla V100 GPU (16GB)"
V100_32 = "Nvidia Tesla V100 GPU (32GB)"
A100_PCIE = "Nvidia PCIe A100 GPU (80GB)"
H100_PCIE_94 = "Nvidia PCIe H100 GPU (94GB)"
A10 = "Nvidia A10 GPU (24GB)"
RTX_PRO_6000 = "Nvidia RTX PRO 6000 Blackwell Server Edition GPU (96 GB)"
A100_40 = "Nvidia A100 GPU (40GB)"
A100_80 = "Nvidia A100 GPU (80GB)"
H100_80 = "Nvidia H100 GPU (80GB)"
H200 = "Nvidia H200 GPU (141GB)"
MI300X = "AMD Instinct MI300X GPU (192GB)"

# VM size -> (GPUs, GPU name). Only sizes whose page lists a whole-GPU count.
GPU_SIZES: dict[str, tuple[int, str]] = {
    # NCasT4_v3
    "Standard_NC4as_T4_v3": (1, T4),
    "Standard_NC8as_T4_v3": (1, T4),
    "Standard_NC16as_T4_v3": (1, T4),
    "Standard_NC64as_T4_v3": (4, T4),
    # NCv3
    "Standard_NC6s_v3": (1, V100_16),
    "Standard_NC12s_v3": (2, V100_16),
    "Standard_NC24s_v3": (4, V100_16),
    "Standard_NC24rs_v3": (4, V100_16),
    # NC A100 v4
    "Standard_NC24ads_A100_v4": (1, A100_PCIE),
    "Standard_NC48ads_A100_v4": (2, A100_PCIE),
    "Standard_NC96ads_A100_v4": (4, A100_PCIE),
    # NCads H100 v5
    "Standard_NC40ads_H100_v5": (1, H100_PCIE_94),
    "Standard_NC80adis_H100_v5": (2, H100_PCIE_94),
    # NVadsA10 v5 (NV6/12/18ads are 1/6, 1/3, 1/2 of a GPU: not listed)
    "Standard_NV36ads_A10_v5": (1, A10),
    "Standard_NV36adms_A10_v5": (1, A10),
    "Standard_NV72ads_A10_v5": (2, A10),
    # NC RTX PRO 6000 BSE v6 (the 24/36/72 sizes are 1/4 and 1/2 GPU: not listed)
    "Standard_NC144ds_xl_RTXPRO6000BSE_v6": (1, RTX_PRO_6000),
    "Standard_NC288ds_xl_RTXPRO6000BSE_v6": (2, RTX_PRO_6000),
    "Standard_NC144lds_xl_RTXPRO6000BSE_v6": (1, RTX_PRO_6000),
    "Standard_NC288lds_xl_RTXPRO6000BSE_v6": (2, RTX_PRO_6000),
    # ND flagships, 8 GPUs per VM
    "Standard_ND40rs_v2": (8, V100_32),
    "Standard_ND96asr_v4": (8, A100_40),
    "Standard_ND96amsr_A100_v4": (8, A100_80),
    "Standard_ND96isr_H100_v5": (8, H100_80),
    "Standard_ND96isr_H200_v5": (8, H200),
    "Standard_ND96isr_MI300X_v5": (8, MI300X),
}

# The fields normalize() reads, kept verbatim; the rest of each row is ids and dates.
_FIELDS = ("armSkuName", "skuName", "productName", "retailPrice", "unitOfMeasure", "type",
           "currencyCode", "armRegionName", "location", "effectiveStartDate")
_REQUIRED = ("armSkuName", "skuName", "productName", "retailPrice", "unitOfMeasure", "type", "armRegionName")


def extract(body, region: str) -> tuple[list[dict], str | None]:
    """The rows of one response page, and the next page's link. Raises on a new shape."""
    if not isinstance(body, dict) or not isinstance(body.get("Items"), list):
        raise ValueError("azure: response has no Items list - API reshaped")
    if body.get("BillingCurrency", "USD") != "USD":
        raise ValueError(f"azure: billing currency {body.get('BillingCurrency')!r}, expected USD")
    rows = []
    for item in body["Items"]:
        missing = [k for k in _REQUIRED if k not in item]
        if missing:
            raise ValueError(f"azure: price row lost {missing} - API reshaped")
        if item["armRegionName"] != region or item["type"] != "Consumption":
            raise ValueError(f"azure: filter not honoured ({item['armRegionName']}, {item['type']})")
        rows.append({k: item.get(k) for k in _FIELDS})
    return rows, body.get("NextPageLink") or None


def market(row: dict) -> str | None:
    """on_demand, spot, or None for a meter we never read."""
    product = row.get("productName") or ""
    if "Windows" in product or product.endswith(" Win"):
        return None  # Azure abbreviates some Windows products to "... Srs Win"
    if row.get("type") != "Consumption" or row.get("unitOfMeasure") != "1 Hour":
        return None
    sku = row.get("skuName") or ""
    if sku.endswith("Low Priority"):
        return None  # Azure Batch low-priority pricing: neither on-demand nor the spot market
    return "spot" if sku.endswith(" Spot") else "on_demand"


class AzureProvider(Provider):
    name = "azure"
    # Two requests per poll against ~10/min; list prices change monthly at most.
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
        return [await self._region(r) for r in REGIONS]

    async def _region(self, region: str) -> RawResponse:
        params = {"$filter": odata_filter(region)}
        first = (await self.get(PATH, params=params)).model_copy(update={"endpoint": endpoint(region), "request": params})
        if not first.ok:
            return first
        rows: list[dict] = []
        page = first
        try:
            for _ in range(MAX_PAGES):
                got, link = extract(page.payload, region)
                rows += got
                if not link:
                    break
                page = await self.get(link)
                if not page.ok:
                    return page.model_copy(update={"endpoint": first.endpoint})
            else:
                raise ValueError(f"azure: more than {MAX_PAGES} pages for {region}")
            if not any(r["armSkuName"] in GPU_SIZES for r in rows):
                raise ValueError(f"azure: no known GPU size priced in {region}")
        except ValueError as exc:
            return first.model_copy(update={"ok": False, "error": f"ValueError: {exc}"[:500], "payload": None})
        # A new size is a table entry to add from its docs page, not a count to guess.
        new = sorted({r["armSkuName"] for r in rows} - set(GPU_SIZES) - _SEEN_UNLISTED)
        if new:
            _SEEN_UNLISTED.update(new)
            log.info("azure: %d VM sizes not in GPU_SIZES (fractional, retired or unlisted); skipped: %s",
                     len(new), ", ".join(new))
        return reshaped(first, {"region": region, "items": rows}, request=params)

    @staticmethod
    def normalize(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        """One GPU VM size per region and market becomes one listing."""
        listings = []
        for snapshot in find_endpoint(by_endpoint, PATH):
            payload = snapshot.payload
            if not isinstance(payload, dict) or payload.get("region") not in REGIONS:
                continue
            region = payload["region"]
            chosen: dict[tuple[str, str], dict] = {}
            for row in payload.get("items") or []:
                size, mkt = row.get("armSkuName"), market(row)
                if size not in GPU_SIZES or mkt is None:
                    continue
                # A re-priced meter can appear twice; the later effectiveStartDate is current.
                prev = chosen.get((size, mkt))
                if prev is None or (row.get("effectiveStartDate") or "") > (prev.get("effectiveStartDate") or ""):
                    chosen[(size, mkt)] = row
            for (size, mkt), row in sorted(chosen.items()):
                price = to_decimal(row.get("retailPrice"))
                if price is None or price <= 0:
                    continue
                count, raw_name = GPU_SIZES[size]
                listings.append(
                    ComputeListing(
                        provider="azure",
                        sku=size,
                        listing_id=f"{size}:{region}" + (":spot" if mkt == "spot" else ""),
                        raw_gpu_name=raw_name,
                        canonical_gpu_name=canonical.canonical_gpu_name(raw_name),
                        gpu_count=count,
                        region=region,
                        country=COUNTRY.get(region),
                        # retailPrice is for the whole VM.
                        price_per_gpu_hour=price / count,
                        price_per_instance_hour=price,
                        currency=row.get("currencyCode") or "USD",
                        market_type=mkt,
                        provider_tier="hyperscaler",
                        interruptible=mkt == "spot",
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
