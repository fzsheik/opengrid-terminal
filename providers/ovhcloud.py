"""OVHcloud Public Cloud: the public order catalogue, no API key.

GET /1.0/order/catalog/public/cloud?ovhSubsidiary=XX is anonymous. OVHcloud
prices each subsidiary in its own currency, and this app stores USD only, so
the two USD catalogues are read (both state currencyCode USD in `locale`):

    US  api.us.ovhcloud.com  OVHcloud US, whose regions are all in the US
    WS  ca.api.ovh.com       OVHcloud "World", the international USD price list
                             (the H100 / H200 / A100 flavors are only here)

Each GPU flavor is an addon "<flavor>.consumption" with product
"publiccloud-instance", one hourly price for the WHOLE instance in units of
1e-8 USD (180000000 = $1.80, as its formattedPrice says), and the GPU model,
count and VRAM under blobs.technical.gpu. Monthly addons ("<flavor>.monthly")
are never read or converted. Windows flavors ("win-...") bundle a licence and
AI Notebook/Training/Deploy addons are a different product: neither is read.

Only flavors OVHcloud tags "active" and not "coming_soon" are read; untagged
and "legacy" ones (GTX 10xx) are older flavors not offered for new instances.

OVHcloud is a general-purpose cloud, so it never counts toward neocloud indices.
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

_PATH = "/1.0/order/catalog/public/cloud?ovhSubsidiary="
# subsidiary -> (catalogue URL, country every one of its regions is in)
CATALOGUES = {
    "US": ("https://api.us.ovhcloud.com" + _PATH + "US", "US"),
    "WS": ("https://ca.api.ovh.com" + _PATH + "WS", None),
}
_MICRO = Decimal(100_000_000)  # catalogue prices are integers in 1e-8 of the currency


def is_gpu_flavor(addon: dict) -> bool:
    """An hourly-billed Public Cloud GPU instance (Linux), as opposed to everything else sold."""
    code = addon.get("planCode") or ""
    gpu = ((addon.get("blobs") or {}).get("technical") or {}).get("gpu")
    return bool(gpu) and addon.get("product") == "publiccloud-instance" and code.endswith(".consumption") \
        and not code.startswith("win-")


class OvhCloudProvider(Provider):
    name = "ovhcloud"
    # Two anonymous requests per poll (8 MB and 2 MB catalogues); list prices change rarely.
    polling = PollingPolicy(interval_seconds=1800, timeout_seconds=60)

    def __init__(self, client: httpx.AsyncClient | None = None):
        super().__init__(
            client
            or httpx.AsyncClient(
                headers={"accept": "application/json", "user-agent": "opengrid-terminal/0.1"},
                timeout=60,
            )
        )

    async def fetch(self) -> list[RawResponse]:
        out = []
        for url, _country in CATALOGUES.values():
            raw = await self.get(url)
            body = raw.payload
            if not isinstance(body, dict) or not isinstance(body.get("addons"), list):
                out.append(raw)  # stored as-is; normalize() refuses it
                continue
            # Keep the GPU flavors only, verbatim: the rest is storage, databases, CPU flavors...
            out.append(reshaped(raw, {"locale": body.get("locale"),
                                      "addons": [a for a in body["addons"] if is_gpu_flavor(a)]}))
        return out

    @staticmethod
    def normalize(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        """One active hourly GPU flavor in one USD catalogue becomes one listing."""
        listings = []
        for sub, (_url, country) in CATALOGUES.items():
            rows = find_endpoint(by_endpoint, _PATH + sub)
            if not rows:
                continue
            snapshot = rows[0]
            body = snapshot.payload
            if not isinstance(body, dict) or not isinstance(body.get("addons"), list):
                raise ValueError(f"ovhcloud {sub}: catalogue has no `addons` list - response reshaped")
            currency = ((body.get("locale") or {}).get("currencyCode") or "")
            if currency != "USD":
                raise ValueError(f"ovhcloud {sub}: catalogue is priced in {currency!r}, not USD")
            for a in body["addons"]:
                if not is_gpu_flavor(a):
                    continue
                blobs = a["blobs"]
                if "active" not in (blobs.get("tags") or []) or "coming_soon" in blobs["tags"]:
                    continue  # not offered for new instances
                if ((blobs.get("commercial") or {}).get("price") or {}).get("interval") != "PT1H":
                    raise ValueError(f"ovhcloud {sub}: {a['planCode']} is no longer priced per hour")
                hourly = [p for p in a.get("pricings") or [] if "consumption" in (p.get("capacities") or [])]
                if len(hourly) != 1 or not isinstance(hourly[0].get("price"), int):
                    raise ValueError(f"ovhcloud {sub}: {a['planCode']} has no single consumption price")
                price = Decimal(hourly[0]["price"]) / _MICRO
                # Fail closed if the unit ever changes: the integer must agree with the printed price.
                printed = (hourly[0].get("formattedPrice") or "").replace("$", "").replace("USD", "").strip()
                if printed and Decimal(printed) != price.quantize(Decimal("0.01")):
                    raise ValueError(f"ovhcloud {sub}: {a['planCode']} price {price} != printed {printed!r}")
                if price <= 0:
                    continue
                tech = blobs["technical"]
                gpu = tech["gpu"]
                count = gpu.get("number") or 0
                if not count:
                    continue
                vram = (gpu.get("memory") or {}).get("size")
                raw_name = f"{gpu.get('model') or ''} {vram}GB" if vram else (gpu.get("model") or "")
                flavor = a["planCode"].removesuffix(".consumption")
                disks = (tech.get("storage") or {}).get("disks") or []
                listings.append(
                    ComputeListing(
                        provider="ovhcloud",
                        sku=flavor,
                        listing_id=f"{sub}:{flavor}",
                        raw_gpu_name=raw_name,
                        canonical_gpu_name=canonical.canonical_gpu_name(raw_name),
                        gpu_count=count,
                        region=None,  # the catalogue is per subsidiary, not per region
                        country=country,
                        # the hourly price is for the whole instance.
                        price_per_gpu_hour=price / count,
                        price_per_instance_hour=price,
                        currency="USD",
                        market_type="on_demand",
                        provider_tier="vm",
                        interruptible=False,
                        available=None,  # a catalogue says nothing about stock
                        capacity=None,
                        capacity_unit=None,
                        vcpu=(tech.get("cpu") or {}).get("cores"),
                        ram_gb=(tech.get("memory") or {}).get("size"),
                        storage_gb=sum((d.get("capacity") or 0) * (d.get("number") or 1) for d in disks) or None,
                        observed_at=snapshot.fetched_at,
                    )
                )
        return listings
