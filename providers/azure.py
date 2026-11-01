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

Reservations (raw only, never a listing). A second filtered GET per region asks the
same API for priceType 'Reservation': the 1, 3 and 5-year reserved-instance prices
of the same NC / ND / NV sizes (~100 rows a region, one page). They are stored as
their own endpoint, RESERVATION_PATH#region, so they can be read later without
refetching, the way GCP's commitments and Jarvislabs' reserved_pricing are kept in
raw. normalize() never reads them: reserved prices must not enter on-demand
listings, indices or market statistics until a reserved-price data model exists.
reservation_price() turns one stored row into an effective $/GPU-hour for that
later use. A row's retailPrice is the WHOLE-TERM price of the whole VM, even though
its unitOfMeasure says "1 Hour":

    $/GPU-hour = retailPrice / (8,760 hours x term years) / GPUs per VM

8,760 is 365 x 24 (Azure's own 730-hour month x 12); leap days are ignored.
"""

import logging
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx

import canonical
import changed_at
from models import ComputeListing, RawResponse
from providers.base import PollingPolicy, Provider, reshaped, to_decimal

if TYPE_CHECKING:
    from tables import RawSnapshot

log = logging.getLogger(__name__)

BASE_URL = "https://prices.azure.com"
PATH = "/api/retail/prices"
REGIONS = ("eastus", "eastus2")
# Azure's own location label for these is "US East" / "US East 2".
COUNTRY = {"eastus": "US", "eastus2": "US"}
MAX_PAGES = 3


CONSUMPTION, RESERVATION = "Consumption", "Reservation"
# Reservations get their own endpoint label, so normalize() (which reads only
# endpoint(region), by exact key) can never pick them up, and history keeps them apart.
RESERVATION_PATH = f"{PATH}/reservation"


def odata_filter(region: str, price_type: str = CONSUMPTION) -> str:
    """Server-side filter: one region, one price type, GPU VM families only, no Windows."""
    return (
        f"serviceName eq 'Virtual Machines' and armRegionName eq '{region}' and priceType eq '{price_type}' "
        "and (startswith(armSkuName,'Standard_NC') or startswith(armSkuName,'Standard_ND') "
        "or startswith(armSkuName,'Standard_NV')) and contains(productName,'Windows') eq false"
    )


def endpoint(region: str, price_type: str = CONSUMPTION) -> str:
    """The stored endpoint label. The encoded filter outgrows the 256-char endpoint
    column, so it is kept verbatim in the snapshot's `request` instead."""
    return f"{PATH if price_type == CONSUMPTION else RESERVATION_PATH}#{region}"


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
# A reservation row also states its term ("1 Year", "3 Years", "5 Years").
_RES_FIELDS = (*_FIELDS, "reservationTerm")
_RES_REQUIRED = (*_REQUIRED, "reservationTerm")


def extract(body, region: str, price_type: str = CONSUMPTION) -> tuple[list[dict], str | None]:
    """The rows of one response page, and the next page's link. Raises on a new shape."""
    if not isinstance(body, dict) or not isinstance(body.get("Items"), list):
        raise ValueError("azure: response has no Items list - API reshaped")
    if body.get("BillingCurrency", "USD") != "USD":
        raise ValueError(f"azure: billing currency {body.get('BillingCurrency')!r}, expected USD")
    fields, required = (_FIELDS, _REQUIRED) if price_type == CONSUMPTION else (_RES_FIELDS, _RES_REQUIRED)
    rows = []
    for item in body["Items"]:
        missing = [k for k in required if k not in item]
        if missing:
            raise ValueError(f"azure: price row lost {missing} - API reshaped")
        if item["armRegionName"] != region or item["type"] != price_type:
            raise ValueError(f"azure: filter not honoured ({item['armRegionName']}, {item['type']})")
        rows.append({k: item.get(k) for k in fields})
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


HOURS_PER_YEAR = Decimal(8760)  # 365 x 24; Azure's 730-hour month x 12
_TERM = re.compile(r"^(\d+) Years?$")


@dataclass(frozen=True)
class ReservedPrice:
    """One Azure reserved-instance price, made comparable. Never a ComputeListing."""

    sku: str  # VM size, e.g. Standard_ND96isr_H100_v5
    region: str
    term_years: int  # 1, 3 or 5
    raw_gpu_name: str
    canonical_gpu_name: str | None
    gpu_count: int
    term_price: Decimal  # the whole VM for the whole term, as Azure states it
    term_hours: Decimal
    price_per_instance_hour: Decimal
    price_per_gpu_hour: Decimal
    currency: str
    effective_start: str | None


def reservation_price(row: dict) -> ReservedPrice | None:
    """A stored reservation row -> its effective per-GPU-hour price. Pure.

    None for anything that is not a priced reservation of a whole-GPU size in
    GPU_SIZES (fractional, retired or unlisted sizes, an unknown term, no price).
    """
    if row.get("type") != RESERVATION or row.get("armSkuName") not in GPU_SIZES:
        return None
    m = _TERM.match(str(row.get("reservationTerm") or "").strip())
    price = to_decimal(row.get("retailPrice"))
    if not m or int(m.group(1)) <= 0 or price is None or price <= 0:
        return None
    years = int(m.group(1))
    count, raw_name = GPU_SIZES[row["armSkuName"]]
    hours = HOURS_PER_YEAR * years
    per_vm_hour = price / hours
    return ReservedPrice(
        sku=row["armSkuName"],
        region=row.get("armRegionName") or "",
        term_years=years,
        raw_gpu_name=raw_name,
        canonical_gpu_name=canonical.canonical_gpu_name(raw_name),
        gpu_count=count,
        term_price=price,
        term_hours=hours,
        price_per_instance_hour=per_vm_hour,
        price_per_gpu_hour=per_vm_hour / count,
        currency=row.get("currencyCode") or "USD",
        effective_start=row.get("effectiveStartDate"),
    )


def reserved_prices(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ReservedPrice]:
    """Every current reserved price in stored snapshots, one per (region, size, term). Pure, read-only.

    Not called by normalize() or anything downstream of it: nothing here becomes a
    listing, an index vote or a market statistic.
    """
    out: dict[tuple[str, str, int], ReservedPrice] = {}
    for region in REGIONS:
        for snapshot in by_endpoint.get(endpoint(region, RESERVATION)) or []:
            payload = snapshot.payload
            if not isinstance(payload, dict) or payload.get("region") != region:
                continue
            if payload.get("price_type") != RESERVATION:
                continue
            for row in payload.get("items") or []:
                rp = reservation_price(row)
                if rp is None or rp.region != region:
                    continue
                key = (region, rp.sku, rp.term_years)
                # A re-priced meter can appear twice; the later effectiveStartDate is current.
                if key not in out or (rp.effective_start or "") > (out[key].effective_start or ""):
                    out[key] = rp
    return [out[k] for k in sorted(out)]


class AzureProvider(Provider):
    name = "azure"
    # Four requests per poll (consumption and reservations, two regions) against
    # ~10/min; list prices change monthly at most.
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
        # Consumption first; the reservation calls are raw capture only.
        return [await self._region(r) for r in REGIONS] + [await self._region(r, RESERVATION) for r in REGIONS]

    async def _region(self, region: str, price_type: str = CONSUMPTION) -> RawResponse:
        params = {"$filter": odata_filter(region, price_type)}
        first = (await self.get(PATH, params=params)).model_copy(
            update={"endpoint": endpoint(region, price_type), "request": params})
        if not first.ok:
            return first
        rows: list[dict] = []
        page = first
        try:
            for _ in range(MAX_PAGES):
                got, link = extract(page.payload, region, price_type)
                rows += got
                if not link:
                    break
                page = await self.get(link)
                if not page.ok:
                    return page.model_copy(update={"endpoint": first.endpoint})
            else:
                raise ValueError(f"azure: more than {MAX_PAGES} pages for {region} ({price_type})")
            if not any(r["armSkuName"] in GPU_SIZES for r in rows):
                raise ValueError(f"azure: no known GPU size priced in {region} ({price_type})")
        except ValueError as exc:
            return first.model_copy(update={"ok": False, "error": f"ValueError: {exc}"[:500], "payload": None})
        # A new size is a table entry to add from its docs page, not a count to guess.
        new = sorted({r["armSkuName"] for r in rows} - set(GPU_SIZES) - _SEEN_UNLISTED)
        if new:
            _SEEN_UNLISTED.update(new)
            log.info("azure: %d VM sizes not in GPU_SIZES (fractional, retired or unlisted); skipped: %s",
                     len(new), ", ".join(new))
        if price_type != CONSUMPTION:
            # Tagged, so no reader can mistake these rows for consumption prices.
            return reshaped(first, {"region": region, "price_type": price_type, "items": rows}, request=params)
        return reshaped(first, {"region": region, "items": rows}, request=params)

    @staticmethod
    def normalize(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        """One GPU VM size per region and market becomes one listing.

        Reads the consumption endpoints only, by exact label: the reservation
        endpoints share the path prefix and must never become listings.
        """
        listings = []
        for snapshot in (s for r in REGIONS for s in by_endpoint.get(endpoint(r)) or []):
            payload = snapshot.payload
            if not isinstance(payload, dict) or payload.get("region") not in REGIONS:
                continue
            if payload.get("price_type", CONSUMPTION) != CONSUMPTION:
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
                # effectiveStartDate is a date (always midnight UTC): when this retail price took effect.
                since = changed_at.parse_time(row.get("effectiveStartDate"))
                listings.append(
                    ComputeListing(
                        provider_changed_at=since and since.replace(hour=0, minute=0, second=0, microsecond=0),
                        changed_at_basis="date_only" if since else None,
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
