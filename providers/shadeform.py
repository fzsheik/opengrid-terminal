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
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx

import canonical
from models import CapacityObservation, ComputeListing, RawResponse
from providers.base import PollingPolicy, Provider, find_endpoint, reshaped

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
        # Latitude's own GPU text ("NVIDIA H100 80GB") omits the variant; Shadeform's label for the same plan carries
        # it, and is what the price listings map, so it wins where Shadeform lists the plan.
        shade = find_endpoint(by_endpoint, ENDPOINT)
        types = shade[0].payload.get("instance_types") if shade and isinstance(shade[0].payload, dict) else None
        labels = {t.get("cloud_instance_type"): gpu_label(t) for t in types or [] if isinstance(t, dict)}
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


class ScalewayProvider(ShadeformCloud):
    # Scaleway's own product catalogue is keyless but prices only in EUR (per minute), and
    # this app stores USD with no FX conversion; Shadeform lists Scaleway's GPU VMs in USD.
    # Capacity comes from Scaleway itself: its per-zone stock levels beat Shadeform's yes/no.
    name = "scaleway"
    cloud = "scaleway"

    async def fetch(self) -> list[RawResponse]:
        out = await super().fetch()  # prices, from Shadeform
        for zone in SCALEWAY_ZONES:
            catalogue = await self._all_pages(zone, "")
            availability = await self._all_pages(zone, "/availability")
            if catalogue.ok and availability.ok:
                gpus = catalogue.payload["servers"]
                availability = reshaped(availability, {"zone": zone, "servers": {
                    k: v for k, v in availability.payload["servers"].items() if k in gpus}})
            out += [catalogue, availability]
        return out

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
