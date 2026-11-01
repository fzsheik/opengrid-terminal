"""OVHcloud Public Cloud: the public order catalogue, no API key.

GET /1.0/order/catalog/public/cloud?ovhSubsidiary=XX is anonymous. OVHcloud
prices each subsidiary in its own currency, and the two USD catalogues are read
(both state currencyCode USD in `locale`). The EU catalogues are not converted
(fx.py): they price the same flavors as World, a second official price list:

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

Bare-metal stock (raw only). The same poll stores the anonymous dedicated-server
availability list, GET /v1/dedicated/server/datacenter/availabilities, from the
World (ca.api.ovh.com) and US (api.us.ovhcloud.com) APIs, GPU configurations only
(rows with a `gpu` option, about 169 of 9,017 World rows). Per configuration
(planCode + memory + storage + gpu) and datacenter it gives OVHcloud's delivery
word: "1H-high", "1H-low", "24H", "72H", "unavailable", "unknown". These are
dedicated servers (Scale-GPU, HGR-AI), not the Public Cloud flavors listed
above, so they join to no listing: no capacity row is made from them until the
servers are priced as listings. baremetal_availability() reads them.
A failed or GPU-less response is stored as a failed fetch; prices never depend on it.
"""

import asyncio
import logging
import re
from dataclasses import dataclass
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
_MICRO = Decimal(100_000_000)
AVAILABILITY_PATH = "/v1/dedicated/server/datacenter/availabilities"
# subsidiary -> bare-metal availability URL (the EU API is not used: it did not answer from here, and the World
# list carries the same EU datacenters).
AVAILABILITIES = {
    "WS": "https://ca.api.ovh.com" + AVAILABILITY_PATH,
    "US": "https://api.us.ovhcloud.com" + AVAILABILITY_PATH,
}
_AVAILABILITY_TIMEOUT = 30  # each list is 4-7 MB; a slow one fails alone, after the prices are already fetched
_GPU_OPTION = re.compile(r"^gpu-(\d+)x(.+)$")  # catalogue prices are integers in 1e-8 of the currency


def is_gpu_flavor(addon: dict) -> bool:
    """An hourly-billed Public Cloud GPU instance (Linux), as opposed to everything else sold."""
    code = addon.get("planCode") or ""
    gpu = ((addon.get("blobs") or {}).get("technical") or {}).get("gpu")
    return bool(gpu) and addon.get("product") == "publiccloud-instance" and code.endswith(".consumption") \
        and not code.startswith("win-")


class OvhCloudProvider(Provider):
    name = "ovhcloud"
    # Two anonymous requests per poll (8 MB and 2 MB catalogues); list prices change rarely.
    # Plus two bare-metal availability lists (4-7 MB each, fetched together, 30 s each) after the prices.
    polling = PollingPolicy(interval_seconds=1800, timeout_seconds=150)

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
        out += await asyncio.gather(*(self._availability(sub, url) for sub, url in AVAILABILITIES.items()))
        return out

    async def _availability(self, sub: str, url: str) -> RawResponse:
        """One bare-metal availability list, GPU configurations only, verbatim. Fails open."""
        raw = await self.get(url, timeout=_AVAILABILITY_TIMEOUT)
        if not raw.ok:
            return raw
        if not isinstance(raw.payload, list):
            return raw.model_copy(update={"ok": False, "payload": None,
                                          "error": f"ovhcloud {sub}: availabilities is not a list - reshaped"})
        gpus = [r for r in raw.payload if isinstance(r, dict) and r.get("gpu")]
        if not gpus:
            return raw.model_copy(update={"ok": False, "payload": None,
                                          "error": f"ovhcloud {sub}: no GPU row in {len(raw.payload)} availabilities"})
        return reshaped(raw, {"subsidiary": sub, "total_rows": len(raw.payload), "rows": gpus})

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


@dataclass(frozen=True)
class BareMetalAvailability:
    """One OVHcloud dedicated GPU server configuration in one datacenter. Never a listing or capacity row."""

    subsidiary: str     # the API it came from: WS (World) or US
    plan_code: str      # e.g. "24hgrai02-v2", "23scalegpu01-v2-eu"
    server: str         # e.g. "24hgrai02"
    fqn: str            # the full configuration name
    gpu_option: str     # OVHcloud's own text, e.g. "gpu-4xnvidia-l40s-48g"
    gpu_count: int | None
    gpu_model: str | None   # the option text after the count, e.g. "nvidia-l40s-48g"; not mapped to a canonical GPU
    datacenter: str
    availability: str   # OVHcloud's word, verbatim: 1H-high, 1H-low, 24H, 72H, unavailable, unknown


def baremetal_availability(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[BareMetalAvailability]:
    """Stored bare-metal GPU availability rows, one per (configuration, datacenter). Pure, read-only."""
    out = []
    for sub, url in AVAILABILITIES.items():
        rows = find_endpoint(by_endpoint, url)
        body = rows[0].payload if rows and isinstance(rows[0].payload, dict) else {}
        for r in body.get("rows") or []:
            if not isinstance(r, dict) or not r.get("gpu") or not r.get("planCode"):
                continue
            m = _GPU_OPTION.match(str(r["gpu"]))
            for dc in r.get("datacenters") or []:
                if not isinstance(dc, dict) or not dc.get("datacenter") or not isinstance(dc.get("availability"), str):
                    continue
                out.append(BareMetalAvailability(
                    subsidiary=sub, plan_code=r["planCode"], server=str(r.get("server") or ""), fqn=str(r.get("fqn") or ""),
                    gpu_option=str(r["gpu"]), gpu_count=int(m.group(1)) if m else None, gpu_model=m.group(2) if m else None,
                    datacenter=dc["datacenter"], availability=dc["availability"]))
    return out
