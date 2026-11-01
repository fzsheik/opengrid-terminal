"""Clouds with no API of their own, read through Shadeform's public catalogue.

Shadeform resells other clouds' capacity and publishes every instance type, with
its real cloud named in `cloud`, from one anonymous endpoint. Crusoe, Latitude.sh,
Denvr, Boost Run, Horizon Compute and Phyntec have no usable public price feed of
their own, and Scaleway's own catalogue prices in EUR only, so each is read from
here and labelled "via-shadeform". The prices are Shadeform's listing of them, not the
cloud's own page: where we can compare (Hyperstack, DigitalOcean, Voltage Park)
they match, and where a cloud has just repriced Shadeform can lag.

A cloud that has its own feed in this app must never also be read through here:
that would count one cloud's capacity twice.

Each subclass fetches the whole catalogue and keeps only its own cloud's rows.

Capacity: each instance type's `availability` list says, per region, whether Shadeform can launch it now
(a boolean). One row per (type, region) for the rental type the listing uses. Scaleway overrides this with
its own per-zone stock levels (available / scarce / shortage), read from Scaleway's public API.

Not read, on purpose: "excesssupply" (Excess Supply is Shadeform's own white-label
pool of other neoclouds' spare capacity; its types carry Lambda, Nebius and Phyntec
names and regions, so reading it would count those clouds twice) and "imwt" (no
public identity; its type names, vCPU/RAM shapes and regions are Massed Compute's,
which is read directly).
"""

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx

import canonical
from models import CapacityObservation, ComputeListing, RawResponse
from providers.base import PollingPolicy, Provider, find_endpoint, reshaped, to_decimal

if TYPE_CHECKING:
    from tables import RawSnapshot

log = logging.getLogger(__name__)

BASE_URL = "https://api.shadeform.ai"
ENDPOINT = "/v1/instances/types"
REGION_MAX = 64  # compute_listings.region is varchar(64)

SCALEWAY = "https://api.scaleway.com/instance/v1/zones/{zone}/products/servers"
SCALEWAY_ZONES = ("fr-par-1", "fr-par-2", "fr-par-3", "nl-ams-1", "nl-ams-2", "nl-ams-3", "pl-waw-1", "pl-waw-2", "pl-waw-3")
# Scaleway's stock words -> our tiers. "shortage" means creation fails: out of stock, no tier.
SCALEWAY_TIER = {"available": "high", "scarce": "low"}
# Scaleway's own price list: keyless, EUR only, one row per (server type, zone). Stored raw, never a listing (below).
SCALEWAY_CATALOG = "https://api.scaleway.com/product-catalog/v2alpha1/public-catalog/products"
SCALEWAY_CATALOG_PARAMS = {"product_types": "instance", "page_size": 1000}
_NANO = Decimal(1_000_000_000)
_PER_HOUR = {"minute": 60, "hour": 1}


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


def rental_of(availability: list[dict]) -> str:
    """The rental type a listing uses: on-demand when offered (or nothing is said), else spot."""
    rentals = {a.get("rental_type") for a in availability}
    return "on_demand" if "on_demand" in rentals or not rentals else "spot"


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


def _failed(raw: RawResponse, why: str) -> RawResponse:
    """A response that arrived but cannot be read: stored as a failed fetch, never as an empty source."""
    return raw.model_copy(update={"ok": False, "error": why, "payload": None})


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
        types = snapshot.payload.get("instance_types") if isinstance(snapshot.payload, dict) else None
        if not isinstance(types, list):
            # A reshaped catalogue fails the normalize (previous listings are kept),
            # never reads as a healthy cloud with nothing for sale.
            raise ValueError(f"{cls.name}: Shadeform response has no instance_types list - reshaped")
        listings = []
        for t in types:
            count = t.get("num_gpus") or 0
            cents = t.get("hourly_price")
            if t.get("cloud") != cls.cloud or not count or cents is None:
                continue  # another cloud, a CPU-only type, or no price
            if isinstance(cents, bool) or not isinstance(cents, int) or not isinstance(count, int):
                raise ValueError(f"{cls.name}: hourly_price is no longer integer cents: {cents!r}")
            avail = t.get("availability") or []
            rental = rental_of(avail)  # a type offered only as spot is a spot listing
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


    @classmethod
    def normalize_capacity(cls, by_endpoint: "dict[str, list[RawSnapshot]]") -> list[CapacityObservation]:
        return shadeform_capacity(cls.name, cls.cloud, by_endpoint)


def shadeform_capacity(provider: str, cloud: str, by_endpoint: "dict[str, list[RawSnapshot]]") -> list[CapacityObservation]:
    """One (instance type, region) availability boolean in Shadeform's catalogue becomes one observation. Shared by
    the clouds read through Shadeform and by providers that use Shadeform only for capacity (Massed Compute)."""
    rows = find_endpoint(by_endpoint, ENDPOINT)
    types = rows[0].payload.get("instance_types") if rows and isinstance(rows[0].payload, dict) else None
    if not isinstance(types, list):
        return []
    out = []
    for t in types:
        count = t.get("num_gpus")
        if t.get("cloud") != cloud or not isinstance(count, int) or count < 1:
            continue
        avail = t.get("availability") or []
        rental, raw_name = rental_of(avail), gpu_label(t)
        for a in avail:
            if a.get("rental_type") not in (rental, None) or not a.get("region"):
                continue
            flag = a.get("available")
            out.append(CapacityObservation(
                provider=provider, sku=t.get("cloud_instance_type") or t["shade_instance_type"], region=a["region"],
                country="US" if str(a.get("display_name") or "").startswith("US") else None,
                raw_gpu_name=raw_name, canonical_gpu_name=canonical.canonical_gpu_name(raw_name), gpu_count=count,
                in_stock=flag if isinstance(flag, bool) else None, raw_value=str(flag).lower(),
                source=ENDPOINT, observed_at=rows[0].fetched_at))
    return out


class CrusoeProvider(ShadeformCloud):
    name = "crusoe"
    cloud = "crusoe"


LATITUDE_PLANS = "https://api.latitude.sh/plans"
# Latitude's region-level stock word -> our tiers; "unavailable" is out of stock, no tier.
LATITUDE_TIER = {"high": "high", "medium": "medium", "low": "low"}
LATITUDE_COUNTRY = {"United States": "US", "Australia": "AU", "Japan": "JP", "United Kingdom": "GB",
                    "Netherlands": "NL", "Germany": "DE", "Brazil": "BR", "Mexico": "MX", "Chile": "CL",
                    "Argentina": "AR", "Colombia": "CO", "Singapore": "SG"}


class LatitudeProvider(ShadeformCloud):
    """Prices through Shadeform (unchanged). Capacity from Latitude's own plans API when LATITUDE_API_KEY is set:
    per plan and region a stock word (`stock_level`) and per location whether it is in stock (`locations.in_stock`)
    and how many servers (`locations.in_stock_count`). Without the key, Shadeform's yes/no per region."""

    name = "latitude"
    cloud = "latitude"

    def __init__(self, client: httpx.AsyncClient | None = None, api_key: str | None = None):
        super().__init__(client)
        self._api_key = api_key

    async def fetch(self) -> list[RawResponse]:
        out = await super().fetch()
        if self._api_key:
            plans = await self.get(LATITUDE_PLANS, headers={"Authorization": f"Bearer {self._api_key}"})
            data = (plans.payload or {}).get("data") if isinstance(plans.payload, dict) else None
            if isinstance(data, list):  # GPU plans only, verbatim
                plans = reshaped(plans, {"data": [p for p in data if ((p.get("attributes") or {}).get("specs") or {}).get("gpu")]})
            out.append(plans)
        return out

    @classmethod
    def normalize_capacity(cls, by_endpoint: "dict[str, list[RawSnapshot]]") -> list[CapacityObservation]:
        rows = find_endpoint(by_endpoint, LATITUDE_PLANS)
        if not rows:
            return super().normalize_capacity(by_endpoint)
        snap = rows[0]
        data = snap.payload.get("data") if isinstance(snap.payload, dict) else None
        if not isinstance(data, list):
            raise ValueError("latitude: plans response has no data list - reshaped")
        labels = _latitude_labels(by_endpoint)
        out = []
        for p in data:
            a = p.get("attributes") or {}
            gpu = (a.get("specs") or {}).get("gpu") or {}
            if not gpu.get("count") or not a.get("slug"):
                continue
            raw_name = labels.get(a["slug"]) or f"{gpu.get('type') or ''}".strip()
            for r in a.get("regions") or []:
                locs = r.get("locations") or {}
                in_stock, counts, word = set(locs.get("in_stock") or []), locs.get("in_stock_count") or {}, str(r.get("stock_level") or "")
                for loc in locs.get("available") or []:
                    stocked = loc in in_stock
                    n = counts.get(loc) if isinstance(counts, dict) else None
                    count = n if isinstance(n, int) else (0 if not stocked else None)
                    out.append(CapacityObservation(
                        provider="latitude", sku=a["slug"], region=loc, country=LATITUDE_COUNTRY.get(r.get("name")),
                        raw_gpu_name=raw_name, canonical_gpu_name=canonical.canonical_gpu_name(raw_name),
                        gpu_count=gpu["count"], in_stock=stocked, tier=LATITUDE_TIER.get(word) if stocked else None,
                        available_count=count, count_unit=None if count is None else "instance",
                        raw_value=f"stock_level={word}; in_stock={stocked}; in_stock_count={n}",
                        source=LATITUDE_PLANS, observed_at=snap.fetched_at))
        return out


def _latitude_labels(by_endpoint: "dict[str, list[RawSnapshot]]") -> dict[str, str]:
    """Plan slug -> Shadeform's GPU label. Latitude's own GPU text ("NVIDIA H100 80GB") omits the variant; Shadeform's
    label for the same plan carries it, and is what the price listings map, so it wins where Shadeform lists the plan."""
    shade = find_endpoint(by_endpoint, ENDPOINT)
    types = shade[0].payload.get("instance_types") if shade and isinstance(shade[0].payload, dict) else None
    return {t.get("cloud_instance_type"): gpu_label(t) for t in types or [] if isinstance(t, dict)}


# Latitude plan price periods that are commitments -> (months, hours in the period). "hour" is pay-as-you-go.
# 730 h is a twelfth of 8,760 (365 x 24), the same month Azure uses; leap days are ignored.
LATITUDE_TERMS = {"month": (1, Decimal(730)), "year": (12, Decimal(8760))}


@dataclass(frozen=True)
class LatitudePlanPrice:
    """A Latitude plan's monthly or yearly price in one region, per server, made comparable. Never a listing."""

    slug: str
    raw_gpu_name: str
    canonical_gpu_name: str | None
    gpu_count: int
    region: str                 # Latitude's region name, e.g. "United States"
    term: str                   # "month" or "year", Latitude's own key
    months: int
    term_price: Decimal         # the whole server for the whole period, as Latitude states it
    term_hours: Decimal
    price_per_gpu_hour: Decimal
    hourly_per_gpu: Decimal | None  # the same plan's own "hour" price, per GPU (pay-as-you-go)


def latitude_plan_prices(by_endpoint: "dict[str, list[RawSnapshot]]") -> tuple[list[LatitudePlanPrice], list[str]]:
    """Monthly and yearly USD plan prices from the stored plans response, and the non-USD currencies skipped
    (OpenGrid converts no currencies). Pure, read-only; nothing here becomes a listing or a statistic."""
    rows = find_endpoint(by_endpoint, LATITUDE_PLANS)
    data = rows[0].payload.get("data") if rows and isinstance(rows[0].payload, dict) else None
    if not isinstance(data, list):
        return [], []
    labels = _latitude_labels(by_endpoint)
    out, skipped = [], set()
    for p in data:
        a = p.get("attributes") or {}
        gpu = (a.get("specs") or {}).get("gpu") or {}
        count = gpu.get("count")
        if not isinstance(count, int) or count <= 0 or not a.get("slug"):
            continue
        raw_name = labels.get(a["slug"]) or f"{gpu.get('type') or ''}".strip()
        for r in a.get("regions") or []:
            pricing = r.get("pricing") or {}
            skipped.update(c for c in pricing if c != "USD")
            usd = pricing.get("USD") if isinstance(pricing.get("USD"), dict) else {}
            hourly = to_decimal(usd.get("hour"))
            for term, (months, hours) in LATITUDE_TERMS.items():
                price = to_decimal(usd.get(term))
                if price is None or price <= 0 or not r.get("name"):
                    continue
                out.append(LatitudePlanPrice(
                    slug=a["slug"], raw_gpu_name=raw_name, canonical_gpu_name=canonical.canonical_gpu_name(raw_name),
                    gpu_count=count, region=r["name"], term=term, months=months, term_price=price, term_hours=hours,
                    price_per_gpu_hour=price / hours / count,
                    hourly_per_gpu=hourly / count if hourly is not None and hourly > 0 else None))
    return out, sorted(skipped)


class DenvrProvider(ShadeformCloud):
    name = "denvr"
    cloud = "denvr"


class BoostRunProvider(ShadeformCloud):
    # Boost Run publishes no prices of its own; Shadeform's cloud directory names it
    # ("Boost Run", boostrun.com, a GPU cloud selling its own NVIDIA servers).
    name = "boostrun"
    cloud = "boostrun"


class HorizonProvider(ShadeformCloud):
    # Horizon Compute (horizoncompute.com) owns its HGX H100 / H200 clusters in
    # Iceland (65.7 N: Blonduos, the region Shadeform names) and sells hourly
    # on-demand instances from them. Its site prints no prices.
    name = "horizon"
    cloud = "horizon"


class PhyntecProvider(ShadeformCloud):
    # Phyntec (phyntec.net, formerly Evergreen Compute; "evergreen" in Shadeform's
    # cloud directory, "phyntec" on its instance types) runs its own 8x RTX 4090 /
    # 5090 bare-metal servers in its WY-1 facility (Casper, WY). Its site prints
    # monthly reservations only; the hourly rate is Shadeform's.
    name = "phyntec"
    cloud = "phyntec"


def is_scaleway_gpu_product(p: dict) -> bool:
    """A GPU Instance row in Scaleway's product catalogue (hardware.gpu present with a count)."""
    gpu = (((p.get("properties") or {}).get("hardware") or {}).get("gpu") or {}) if isinstance(p, dict) else {}
    return p.get("product_category") == "Instance" and isinstance(gpu.get("count"), int) and gpu["count"] > 0


@dataclass(frozen=True)
class ScalewayEurPrice:
    """One Scaleway GPU server type's own list price in one zone, in EUR. Never a ComputeListing."""

    offer_id: str          # e.g. "H100-1-80G": the same name as the listing sku (Shadeform's cloud_instance_type)
    zone: str
    gpu_type: str          # Scaleway's text, e.g. "H100-PCIe", "B300-SXM"
    gpu_count: int
    eur_per_instance_hour: Decimal
    eur_per_gpu_hour: Decimal
    unit: str              # the unit Scaleway priced it in ("minute" or "hour")
    status: str            # general_availability, preview, retired, ...


def scaleway_eur_prices(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ScalewayEurPrice]:
    """Scaleway's own EUR prices from the stored product catalogue. Pure, read-only, for audit and later use.

    Never called by normalize(): this app stores USD and converts no currency (the same rule as OVHcloud's EUR
    catalogues), so Scaleway listings keep Shadeform's USD price until an FX policy exists. A row whose currency
    is not EUR or whose unit is not minute / hour is skipped, never guessed.
    """
    rows = find_endpoint(by_endpoint, SCALEWAY_CATALOG)
    body = rows[0].payload if rows else None
    out = []
    for p in (body or {}).get("products") or [] if isinstance(body, dict) else []:
        if not is_scaleway_gpu_product(p):
            continue
        retail = (p.get("price") or {}).get("retail_price") or {}
        unit = (p.get("unit_of_measure") or {})
        per_hour = _PER_HOUR.get(unit.get("unit"))
        units, nanos = retail.get("units"), retail.get("nanos")
        offer = ((p.get("properties") or {}).get("instance") or {}).get("offer_id") or p.get("product")
        zone = (p.get("locality") or {}).get("zone")
        if retail.get("currency_code") != "EUR" or per_hour is None or unit.get("size") != 1 \
                or not isinstance(units, int) or not isinstance(nanos, int) or not offer or not zone:
            continue
        hourly = (Decimal(units) + Decimal(nanos) / _NANO) * per_hour
        gpu = p["properties"]["hardware"]["gpu"]
        out.append(ScalewayEurPrice(offer_id=offer, zone=zone, gpu_type=str(gpu.get("type") or ""),
                                    gpu_count=gpu["count"], eur_per_instance_hour=hourly,
                                    eur_per_gpu_hour=hourly / gpu["count"], unit=unit["unit"],
                                    status=str(p.get("status") or "")))
    return out


class ScalewayProvider(ShadeformCloud):
    # Prices: Shadeform's USD listing. Scaleway's own product catalogue is keyless but prices only in EUR (per
    # minute), and this app stores USD with no FX conversion (as for OVHcloud's EUR catalogues), so it is stored
    # raw next to the Shadeform response and read only by scaleway_eur_prices(), never into listings or statistics.
    # Capacity comes from Scaleway itself: its per-zone stock levels beat Shadeform's yes/no.
    name = "scaleway"
    cloud = "scaleway"

    async def fetch(self) -> list[RawResponse]:
        out = await super().fetch()  # prices, from Shadeform
        out.append(await self._catalog())  # Scaleway's own EUR prices, raw only; fails open
        for zone in SCALEWAY_ZONES:
            catalogue = await self._all_pages(zone, "")
            availability = await self._all_pages(zone, "/availability")
            if catalogue.ok and availability.ok:
                gpus = catalogue.payload["servers"]
                availability = reshaped(availability, {"zone": zone, "servers": {
                    k: v for k, v in availability.payload["servers"].items() if k in gpus}})
            out += [catalogue, availability]
        return out

    async def _catalog(self) -> RawResponse:
        """Scaleway's own Instance catalogue, GPU rows only, verbatim. A response without a products list, a partial
        page set, or no GPU row at all is stored as a failure: it never reaches listings, so it cannot break prices."""
        merged, first, total = [], None, None
        for page in range(1, 4):
            resp = await self.get(SCALEWAY_CATALOG, params={**SCALEWAY_CATALOG_PARAMS, "page": page})
            body = resp.payload
            if not resp.ok or not isinstance(body, dict) or not isinstance(body.get("products"), list):
                return resp if not resp.ok else _failed(resp, "scaleway catalogue: no products list - reshaped")
            first, total = first or resp, body.get("total_count")
            merged += body["products"]
            if not body["products"] or not isinstance(total, int) or len(merged) >= total:
                break
        if isinstance(total, int) and len(merged) < total:
            return _failed(first, f"scaleway catalogue: {len(merged)} of {total} products after 3 pages")
        gpus = [p for p in merged if is_scaleway_gpu_product(p)]
        if not gpus:
            return _failed(first, "scaleway catalogue: no GPU Instance rows - reshaped")
        return reshaped(first, {"total_count": total, "products": gpus})

    async def _all_pages(self, zone: str, suffix: str) -> RawResponse:
        """Every page of one Scaleway listing (100 per page) as one response; the catalogue keeps GPU types only.
        A failed page is returned as the failure, so a partial set is never stored as complete."""
        url, merged, first = SCALEWAY.format(zone=zone) + suffix, {}, None
        for page in range(1, 6):
            resp = await self.get(url, params={"per_page": 100, "page": page})
            if not resp.ok or not isinstance(resp.payload, dict) or not isinstance(resp.payload.get("servers"), dict):
                return resp
            first = first or resp
            merged.update(resp.payload["servers"])
            if len(resp.payload["servers"]) < 100:
                break
        if not suffix:
            merged = {k: v for k, v in merged.items() if v.get("gpu")}
        return reshaped(first, {"zone": zone, "servers": merged})

    @classmethod
    def normalize_capacity(cls, by_endpoint: "dict[str, list[RawSnapshot]]") -> list[CapacityObservation]:
        """One (GPU server type, zone) stock word becomes one observation."""
        catalogues = {s.payload.get("zone"): s.payload.get("servers") or {}
                      for ep, rows in by_endpoint.items() if "/products/servers" in ep and "/availability" not in ep
                      for s in rows if isinstance(s.payload, dict)}
        out = []
        for ep, rows in by_endpoint.items():
            if "/products/servers/availability" not in ep:
                continue
            snap = rows[0]
            body = snap.payload if isinstance(snap.payload, dict) else {}
            zone, stock = body.get("zone"), body.get("servers")
            if not isinstance(stock, dict):
                raise ValueError(f"scaleway: {ep} has no servers map - response reshaped")
            for sku, a in stock.items():
                info = catalogues.get(zone, {}).get(sku) or {}
                if not info.get("gpu"):
                    continue  # catalogue missing for this zone, or not a GPU type
                g = info.get("gpu_info") or {}
                mem = g.get("gpu_memory")
                raw_name = (g.get("gpu_name") or sku) + (f" {round(mem / 2**30)}GB" if mem else "")
                word = str((a or {}).get("availability") or "")
                tier = SCALEWAY_TIER.get(word)
                out.append(CapacityObservation(
                    provider="scaleway", sku=sku, region=zone, country=(zone or "")[:2].upper() or None,
                    raw_gpu_name=raw_name, canonical_gpu_name=canonical.canonical_gpu_name(raw_name),
                    gpu_count=info["gpu"], in_stock=True if tier else (False if word == "shortage" else None),
                    tier=tier, raw_value=word or "(missing)", source=ep, observed_at=snap.fetched_at))
        return out
