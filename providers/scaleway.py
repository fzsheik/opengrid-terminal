"""Scaleway: its own public catalogue, EUR list prices converted to USD at the ECB reference rate (fx.py).

All anonymous:
    GET /product-catalog/v2alpha1/public-catalog/products?product_types=instance
        Scaleway's own list price per Instance type and zone, in EUR (per minute for GPU types), with its hardware
        and status. GPU rows only are stored (verbatim). PRICES come from here.
    GET /instance/v1/zones/{zone}/products/servers                 server types per zone (GPU ones kept)
    GET /instance/v1/zones/{zone}/products/servers/availability    per-zone stock word per server type
        CAPACITY (normalize_capacity) and each listing's `available` come from here.

Prices. One general_availability GPU offer becomes one listing across the zones it is priced in (Scaleway prices
an offer the same in every zone; an offer priced differently in two zones becomes one listing per zone). The EUR
instance-hour price is converted at the ECB EUR->USD rate in force when the catalogue was fetched; the listing is
USD and carries its provenance (original currency and amounts, rate, rate date). No usable rate (none stored, or
more than fx.MAX_AGE_DAYS old): no listing at all, logged (fail closed), so nothing is ever converted at a guess.

History. Until 2026-11-01 Scaleway's prices were Shadeform's USD listing of it (providers/shadeform.py), which sat
3-8% above the converted list price. The products Shadeform listed keep their listing ids (LEGACY_LISTING_IDS) and
canonical GPU names, so their series continue across the switch; the step at the switch is a source change, not a
market move (methodology/fx.md). Shadeform no longer reads Scaleway at all, so Scaleway is counted once.
"""

import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx

import canonical
import fx
from models import CapacityObservation, ComputeListing, RawResponse
from providers.base import PollingPolicy, Provider, find_endpoint, reshaped

if TYPE_CHECKING:
    from tables import RawSnapshot

log = logging.getLogger(__name__)

API = "https://api.scaleway.com"
SCALEWAY = API + "/instance/v1/zones/{zone}/products/servers"
SCALEWAY_ZONES = ("fr-par-1", "fr-par-2", "fr-par-3", "nl-ams-1", "nl-ams-2", "nl-ams-3", "pl-waw-1", "pl-waw-2", "pl-waw-3")
# Scaleway's stock words -> our tiers. "shortage" means creation fails: out of stock, no tier.
SCALEWAY_TIER = {"available": "high", "scarce": "low"}
SCALEWAY_CATALOG = API + "/product-catalog/v2alpha1/public-catalog/products"
SCALEWAY_CATALOG_PARAMS = {"product_types": "instance", "page_size": 1000}
LISTED_STATUSES = ("general_availability",)
REGION_MAX = 64  # compute_listings.region is varchar(64)
_NANO = Decimal(1_000_000_000)
_PER_HOUR = {"minute": 60, "hour": 1}
_VRAM = re.compile(r"-(\d+)G$")

# Scaleway offer id -> the listing_id Shadeform gave the same product (its shade_instance_type), checked against
# Shadeform's catalogue on 2026-11-01 (cloud_instance_type == offer_id, same GPU count). Keeping them keeps the
# price history of these listings continuous across the switch to Scaleway's own prices.
LEGACY_LISTING_IDS = {
    "H100-1-80G": "H100", "H100-2-80G": "H100x2",
    "L4-1-24G": "L4", "L4-2-24G": "L4x2", "L4-4-24G": "L4x4", "L4-8-24G": "L4x8",
    "L40S-1-48G": "L40S", "L40S-2-48G": "L40Sx2", "L40S-4-48G": "L40Sx4", "L40S-8-48G": "L40Sx8",
    "B300-SXM-8-288G": "B300x8",
}


def _failed(raw: RawResponse, why: str) -> RawResponse:
    """A response that arrived but cannot be read: stored as a failed fetch, never as an empty source."""
    return raw.model_copy(update={"ok": False, "error": why, "payload": None})


def is_scaleway_gpu_product(p: dict) -> bool:
    """A GPU Instance row in Scaleway's product catalogue (hardware.gpu present with a count)."""
    gpu = (((p.get("properties") or {}).get("hardware") or {}).get("gpu") or {}) if isinstance(p, dict) else {}
    return p.get("product_category") == "Instance" and isinstance(gpu.get("count"), int) and gpu["count"] > 0


@dataclass(frozen=True)
class ScalewayEurPrice:
    """One Scaleway GPU server type's own list price in one zone, in EUR."""

    offer_id: str          # e.g. "H100-1-80G": the listing sku
    zone: str
    gpu_type: str          # Scaleway's text, e.g. "H100-PCIe", "B300-SXM"
    gpu_count: int
    eur_per_instance_hour: Decimal
    eur_per_gpu_hour: Decimal
    unit: str              # the unit Scaleway priced it in ("minute" or "hour")
    status: str            # general_availability, preview, retired, ...
    hardware: dict         # properties.hardware, verbatim (cpu, ram, storage, network, gpu)


def scaleway_eur_prices(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ScalewayEurPrice]:
    """Scaleway's own EUR prices from the stored product catalogue. Pure, read-only.

    A row whose currency is not EUR or whose unit is not minute / hour is skipped, never guessed.
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
        hw = p["properties"]["hardware"]
        gpu = hw["gpu"]
        out.append(ScalewayEurPrice(offer_id=offer, zone=zone, gpu_type=str(gpu.get("type") or ""),
                                    gpu_count=gpu["count"], eur_per_instance_hour=hourly,
                                    eur_per_gpu_hour=hourly / gpu["count"], unit=unit["unit"],
                                    status=str(p.get("status") or ""), hardware=hw))
    return out


def raw_gpu_name(gpu_type: str, offer_id: str) -> str:
    """Scaleway's GPU text plus the per-GPU VRAM its offer id states: "H100-SXM" + "H100-SXM-8-80G" -> "H100-SXM 80GB"."""
    m = _VRAM.search(offer_id)
    return f"{gpu_type} {m.group(1)}GB" if m and gpu_type else (gpu_type or offer_id)


def _zone_maps(by_endpoint) -> tuple[dict[str, dict], dict[str, dict]]:
    """(zone -> server types, zone -> stock words) from the stored per-zone responses."""
    catalogues, stock = {}, {}
    for ep, rows in by_endpoint.items():
        if "/products/servers" not in ep or not rows or not isinstance(rows[0].payload, dict):
            continue
        body = rows[0].payload
        target = stock if ep.endswith("/availability") else catalogues
        if isinstance(body.get("servers"), dict) and body.get("zone"):
            target[body["zone"]] = body["servers"]
    return catalogues, stock


def _available(offer: str, zones: list[str], stock: dict[str, dict]) -> bool | None:
    words = [str(((stock.get(z) or {}).get(offer) or {}).get("availability") or "") for z in zones]
    if any(w in SCALEWAY_TIER for w in words):
        return True
    if words and all(w == "shortage" for w in words):
        return False
    return None  # no stock word for any priced zone


def _region(zones: list[str]) -> tuple[str, str | None]:
    text = "; ".join(zones)
    if len(text) > REGION_MAX:
        text = text[: REGION_MAX - 1] + "…"
    countries = {z[:2].upper() for z in zones}
    return text, (countries.pop() if len(countries) == 1 else None)


class ScalewayProvider(Provider):
    name = "scaleway"
    polling = PollingPolicy(interval_seconds=900, timeout_seconds=60)

    def __init__(self, client: httpx.AsyncClient | None = None):
        super().__init__(
            client
            or httpx.AsyncClient(
                base_url=API,
                headers={"accept": "application/json", "user-agent": "opengrid-terminal/0.1"},
                timeout=45,
            )
        )

    async def fetch(self) -> list[RawResponse]:
        out = [await self._catalog()]  # prices
        for zone in SCALEWAY_ZONES:  # stock
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
        page set, or no GPU row at all is stored as a failure, so it never reads as "nothing for sale"."""
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
    def normalize(cls, by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        """One general_availability GPU offer (all its zones at one price) becomes one USD listing."""
        rows = find_endpoint(by_endpoint, SCALEWAY_CATALOG)
        if not rows:
            return []
        snapshot = rows[0]
        if not isinstance(snapshot.payload, dict) or not isinstance(snapshot.payload.get("products"), list):
            raise ValueError("scaleway: catalogue has no products list - reshaped")
        prices = [p for p in scaleway_eur_prices(by_endpoint) if p.status in LISTED_STATUSES]
        if not prices:
            return []
        rate = fx.usd_rate("EUR", snapshot.fetched_at, context=f"scaleway catalogue fetched {snapshot.fetched_at:%Y-%m-%d %H:%M}Z")
        if rate is None:
            return []  # fail closed: nothing is converted without a fresh ECB rate (fx.usd_rate logged why)
        catalogues, stock = _zone_maps(by_endpoint)

        by_offer: dict[str, list[ScalewayEurPrice]] = defaultdict(list)
        for p in prices:
            by_offer[p.offer_id].append(p)
        listings = []
        for offer, zone_prices in sorted(by_offer.items()):
            groups: dict[Decimal, list[ScalewayEurPrice]] = defaultdict(list)
            for p in zone_prices:
                groups[p.eur_per_instance_hour].append(p)
            base_id = LEGACY_LISTING_IDS.get(offer, offer)
            for eur, ps in sorted(groups.items()):
                zones = sorted({p.zone for p in ps})
                listing_id = base_id if len(groups) == 1 else f"{base_id}@{zones[0]}"
                listings.append(cls._listing(offer, listing_id, ps[0], eur, zones, rate, catalogues, stock,
                                             snapshot.fetched_at))
        return listings

    @classmethod
    def _listing(cls, offer, listing_id, p: ScalewayEurPrice, eur, zones, rate, catalogues, stock, fetched_at):
        hw = p.hardware or {}
        cpu, ram = hw.get("cpu") or {}, hw.get("ram") or {}
        scratch = [((catalogues.get(z) or {}).get(offer) or {}).get("scratch_storage_max_size") for z in zones]
        scratch = [s for s in scratch if isinstance(s, int) and s > 0]
        region, country = _region(zones)
        name = raw_gpu_name(p.gpu_type, offer)
        vcpu = (cpu.get("virtual") or {}).get("count") or cpu.get("threads")
        return ComputeListing(
            provider=cls.name,
            sku=offer,
            listing_id=listing_id,
            raw_gpu_name=name,
            canonical_gpu_name=canonical.canonical_gpu_name(name),
            gpu_count=p.gpu_count,
            region=region,
            country=country,
            **fx.convert(eur, p.gpu_count, "EUR", rate),
            market_type="on_demand",
            provider_tier="vm",
            interruptible=False,
            available=_available(offer, zones, stock),
            capacity=None,
            capacity_unit=None,
            vcpu=vcpu if isinstance(vcpu, int) else None,
            ram_gb=round(ram["size"] / 2**30, 2) if isinstance(ram.get("size"), int) else None,
            storage_gb=max(scratch) / 1e9 if scratch else None,
            observed_at=fetched_at,
        )

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
