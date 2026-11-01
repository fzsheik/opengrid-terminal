"""Google Cloud Compute Engine: GPU list prices from the Cloud Billing Catalog API, every region.

GET https://cloudbilling.googleapis.com/v1/services/6F81-5844-456A/skus (Compute Engine), paged, with the API key in
the `X-Goog-Api-Key` header (GCP_API_KEY; restricted to the Cloud Billing API). The key never goes in a URL, so it
cannot end up in a stored endpoint or error message.

Google prices a GPU VM as separate SKUs: the GPUs, the vCPUs and the memory of that machine family, plus any bundled
Local SSD. A VM price is assembled from them:

    price = GPUs x GPU SKU + vCPUs x core SKU + GiB x RAM SKU + bundled SSD GiB x (SSD SKU per GiB-month / 730)

MACHINES holds each machine type's shape. Every shape is PROVEN, not looked up: assembled for Iowa (us-central1) it
reproduces the price on Google's own accelerator-optimized pricing page exactly (tests/test_provider_gcp.py checks
every one against a saved copy of the page). A machine type whose shape cannot be proven that way is left out:
a4-highgpu-8g (the page prints no on-demand price) and the fractional G4 shapes (1/8, 1/4, 1/2 of a card).

SKU names vary by region (the H100 Mega GPU is "Mega" in most regions and "Plus" in Americas), so each component is
matched by a pattern. Regions: only REGIONS (Iowa today, see the shortcut note there); within them, a region where two SKUs of one
component disagree on the price is skipped, never guessed. Attachable GPUs (T4, P4, V100, P100) are the GPU
alone, per GPU; the N1 host is billed on top.

Raw: only the SKUs that matter (GPU resource group, the machine families' core and RAM, Local SSD) are stored, with
every usage type, so Spot, 1- and 3-year commitments and Calendar Mode can be read later without refetching.
Listings: on-demand only for now.

These are list prices, which is why the provider class is "hyperscaler". Replaced the price-page scraper 2026-10-30.
"""

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

BASE_URL = "https://cloudbilling.googleapis.com"
ENDPOINT = "/v1/services/6F81-5844-456A/skus"  # Compute Engine
PAGE_SIZE = 5000
MAX_PAGES = 20
HOURS_PER_MONTH = Decimal(730)  # Google's own month for converting GiB-month storage prices
# shortcut: Iowa only. Many SKUs are priced for a whole region group ("running in Americas"), so "priced in a region"
# is not "sold in that region"; listing every priced region would invent availability. Opening more regions needs
# Compute Engine's accelerator-types-per-zone list, which needs a service account (OAuth), not an API key.
REGIONS = ("us-central1",)

# Component SKU patterns (on-demand descriptions). Region words come after "running in".
GPU = {
    "h100": r"^Nvidia H100 80GB GPU running in ",
    "h100-mega": r"^Nvidia H100 80GB (?:Mega|Plus) GPU running in ",
    "h200": r"^H200 141GB GPU running in ",
    "a100-80gb": r"^Nvidia Tesla A100 80GB GPU running in ",
    "a100": r"^Nvidia Tesla A100 GPU running in ",
    "l4": r"^Nvidia L4 GPU running in ",
    "rtx-pro-6000": r"^RTX 6000 96GB running in ",
    "t4": r"^Nvidia Tesla T4 GPU running in ",
    "p4": r"^Nvidia Tesla P4 GPU running in ",
    "v100": r"^Nvidia Tesla V100 GPU running in ",
    "p100": r"^Nvidia Tesla P100 GPU running in ",
}
FAMILY = {f: (rf"^{f} Instance Core running in ", rf"^{f} Instance Ram running in ")
          for f in ("A3", "A3Plus", "A3Ultra", "A2", "G2", "G4")}
SSD = r"^SSD backed Local Storage(?: in .+)?$"
_ONDEMAND_ONLY = re.compile(r"Spot|Preemptible|DWS|Calendar|Reserved|Commitment|Sole Tenancy|Custom", re.I)
_KEEP = re.compile("|".join([*GPU.values(), *(p for pair in FAMILY.values() for p in pair), SSD]))

# machine type -> (GPUs, GPU key, accelerator type, core/RAM family, vCPUs, RAM GiB, bundled Local SSD GiB)
# Proven against Google's Iowa page prices (see the module docstring). A3 High's page prints 1,871 GB of memory;
# 1,872 GiB is its documented size and the only value that reproduces the printed price.
MACHINES: dict[str, tuple[int, str, str, str, int, int, int]] = {
    "a3-ultragpu-8g": (8, "h200", "nvidia-h200-141gb", "A3Ultra", 224, 2952, 12000),
    "a3-megagpu-8g": (8, "h100-mega", "nvidia-h100-mega-80gb", "A3Plus", 208, 1872, 6000),
    "a3-highgpu-8g": (8, "h100", "nvidia-h100-80gb", "A3", 208, 1872, 6000),
    **{f"a2-ultragpu-{n}g": (n, "a100-80gb", "nvidia-a100-80gb", "A2", 12 * n, 170 * n, 375 * n) for n in (1, 2, 4, 8)},
    **{f"a2-highgpu-{n}g": (n, "a100", "nvidia-tesla-a100", "A2", 12 * n, 85 * n, 0) for n in (1, 2, 4, 8)},
    "a2-megagpu-16g": (16, "a100", "nvidia-tesla-a100", "A2", 96, 1360, 0),
    **{f"g4-standard-{48 * n}": (n, "rtx-pro-6000", "nvidia-rtx-pro-6000", "G4", 48 * n, 180 * n, 0) for n in (1, 2, 4, 8)},
    **{f"g2-standard-{v}": (1, "l4", "nvidia-l4", "G2", v, 4 * v, 0) for v in (4, 8, 12, 16, 32)},
    "g2-standard-24": (2, "l4", "nvidia-l4", "G2", 24, 96, 0),
    "g2-standard-48": (4, "l4", "nvidia-l4", "G2", 48, 192, 0),
    "g2-standard-96": (8, "l4", "nvidia-l4", "G2", 96, 384, 0),
}
# attachable GPU key -> accelerator type (the GPU alone, per GPU)
ATTACHABLE = {"t4": "nvidia-tesla-t4", "p4": "nvidia-tesla-p4", "v100": "nvidia-tesla-v100", "p100": "nvidia-tesla-p100"}


def unit_price(sku: dict) -> Decimal | None:
    """The SKU's highest-tier unit price (GPU and core SKUs have one tier), or None when it has none."""
    info = (sku.get("pricingInfo") or [{}])[0]
    rates = (info.get("pricingExpression") or {}).get("tieredRates") or []
    if not rates:
        return None
    p = rates[-1].get("unitPrice") or {}
    return Decimal(str(p.get("units") or 0)) + Decimal(p.get("nanos") or 0) / Decimal(10**9)


def by_region(skus: list[dict], pattern: str) -> dict[str, Decimal]:
    """On-demand price per region for one component. A region priced twice with different values is dropped."""
    rx, out, conflict = re.compile(pattern), {}, set()
    for s in skus:
        d = s.get("description") or ""
        if (s.get("category") or {}).get("usageType") != "OnDemand" or not rx.search(d) or _ONDEMAND_ONLY.search(d):
            continue
        price = unit_price(s)
        if price is None:
            continue
        for region in s.get("serviceRegions") or []:
            if region in out and out[region] != price:
                conflict.add(region)
            out[region] = price
    for region in conflict:
        log.warning("gcp: %r priced twice in %s; region skipped", pattern, region)
        out.pop(region)
    return out


def _listing(sku, accel, count, price, region, observed_at, tier, vcpu=None, ram=None) -> ComputeListing:
    return ComputeListing(
        provider="gcp",
        sku=sku,
        listing_id=f"{sku}:{region}",
        raw_gpu_name=accel,
        canonical_gpu_name=canonical.canonical_gpu_name(accel),
        gpu_count=count,
        region=region,
        country="US" if region.startswith("us-") else None,
        price_per_gpu_hour=price / count,
        price_per_instance_hour=price,
        currency="USD",
        market_type="on_demand",
        provider_tier=tier,
        interruptible=False,
        available=None,  # a price list says nothing about capacity
        capacity=None,
        capacity_unit=None,
        vcpu=vcpu,
        ram_gb=ram,
        storage_gb=None,
        observed_at=observed_at,
    )


class GcpProvider(Provider):
    name = "gcp"
    # List prices change rarely; the catalogue is ~7 pages.
    polling = PollingPolicy(interval_seconds=3600, timeout_seconds=90)

    def __init__(self, api_key: str, client: httpx.AsyncClient | None = None):
        super().__init__(
            client
            or httpx.AsyncClient(
                base_url=BASE_URL,
                headers={"X-Goog-Api-Key": api_key, "accept": "application/json"},
                timeout=90,
            )
        )

    async def fetch(self) -> list[RawResponse]:
        """Every page as one response, filtered to the SKUs that matter. A failed page is returned as the failure,
        so a partial catalogue is never stored as a complete one."""
        kept, first, token = [], None, None
        for _ in range(MAX_PAGES):
            resp = await self.get(ENDPOINT, params={"pageSize": PAGE_SIZE, **({"pageToken": token} if token else {})})
            if not resp.ok or not isinstance(resp.payload, dict) or not isinstance(resp.payload.get("skus"), list):
                return [resp]
            first = first or resp
            kept += [s for s in resp.payload["skus"]
                     if (s.get("category") or {}).get("resourceGroup") == "GPU" or _KEEP.search(s.get("description") or "")]
            token = resp.payload.get("nextPageToken")
            if not token:
                return [reshaped(first, {"skus": kept})]
        return [first.model_copy(update={"ok": False, "error": f"more than {MAX_PAGES} pages; catalogue not stored"})]

    @staticmethod
    def normalize(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        """One on-demand listing per (machine type, region) and per (attachable GPU, region)."""
        rows = find_endpoint(by_endpoint, ENDPOINT)
        if not rows:
            return []
        snapshot = rows[0]
        skus = snapshot.payload.get("skus") if isinstance(snapshot.payload, dict) else None
        if not isinstance(skus, list):
            raise ValueError("gcp: catalogue has no skus list - response reshaped")
        gpu = {k: by_region(skus, p) for k, p in GPU.items()}
        fam = {f: (by_region(skus, c), by_region(skus, r)) for f, (c, r) in FAMILY.items()}
        ssd = {r: p / HOURS_PER_MONTH for r, p in by_region(skus, SSD).items()}
        if not gpu["h100"] or not fam["A3"][0]:
            raise ValueError("gcp: no H100 or A3 core SKU found - catalogue reshaped")

        listings = []
        for mtype, (count, key, accel, family, vcpu, ram, lssd) in MACHINES.items():
            cores, mem = fam[family]
            for region, g in gpu[key].items():
                if region not in REGIONS or region not in cores or region not in mem or (lssd and region not in ssd):
                    continue  # a component is not priced there: the machine is not sold there
                price = count * g + vcpu * cores[region] + ram * mem[region] + (lssd * ssd[region] if lssd else 0)
                listings.append(_listing(mtype, accel, count, price, region, snapshot.fetched_at, "hyperscaler",
                                         vcpu=vcpu, ram=float(ram)))
        for key, accel in ATTACHABLE.items():
            for region, g in gpu[key].items():
                if region not in REGIONS:
                    continue
                listings.append(_listing(accel, accel, 1, g, region, snapshot.fetched_at, "gpu-attachment"))
        return listings


# --------------------------------------------------------------------------- commitments (read on demand, never a listing)

# GPU key -> accelerator type, for every key that has one (machine GPUs and attachable GPUs).
ACCELERATOR = {**{key: accel for _, key, accel, *_ in MACHINES.values()}, **ATTACHABLE}
COMMIT_TERMS = {"Commit1Yr": 1, "Commit3Yr": 3}
_COMMIT_DESC = re.compile(r"^Commitment v1: (?P<body>.+?) for (?P<years>\d+) Years?$")


@dataclass(frozen=True)
class GcpCommitPrice:
    """A 1- or 3-year committed-use price of ONE GPU (the GPU SKU alone: vCPUs, memory and Local SSD are billed on
    top and their commitment SKUs are not stored). Never a ComputeListing."""

    gpu_key: str
    accelerator: str
    canonical_gpu_name: str | None
    region: str
    term_years: int
    usage_type: str             # Google's own label, e.g. "Commit1Yr"
    description: str            # the SKU description, verbatim
    price_per_gpu_hour: Decimal
    on_demand_gpu_hour: Decimal | None  # the same GPU SKU on demand, same region (same basis)
    currency: str


def commitment_prices(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[GcpCommitPrice]:
    """Every GPU commitment SKU priced in REGIONS, one per (GPU, region, term). Pure, read-only.

    A region priced twice with different values for one GPU and term is skipped, never guessed (as by_region does).
    Not called by normalize(): nothing here becomes a listing, an index vote or a market statistic.
    """
    rows = find_endpoint(by_endpoint, ENDPOINT)
    skus = rows[0].payload.get("skus") if rows and isinstance(rows[0].payload, dict) else None
    if not isinstance(skus, list):
        return []
    patterns = {k: re.compile(p) for k, p in GPU.items() if k in ACCELERATOR}
    found: dict[tuple[str, str, int], tuple[dict, Decimal, str]] = {}
    conflict: set[tuple[str, str, int]] = set()
    for s in skus:
        years = COMMIT_TERMS.get((s.get("category") or {}).get("usageType"))
        m = _COMMIT_DESC.match(s.get("description") or "")
        if years is None or m is None or int(m["years"]) != years:
            continue
        key = next((k for k, rx in patterns.items() if rx.search(m["body"])), None)
        price = unit_price(s)
        if key is None or price is None or price <= 0:
            continue
        rates = ((s.get("pricingInfo") or [{}])[0].get("pricingExpression") or {})
        if rates.get("usageUnit") != "h":
            continue  # a GPU commitment is priced per hour; anything else is kept raw, never converted
        tiers = rates.get("tieredRates") or [{}]
        currency = (tiers[-1].get("unitPrice") or {}).get("currencyCode") or "USD"
        for region in s.get("serviceRegions") or []:
            if region not in REGIONS:
                continue
            k = (key, region, years)
            if k in found and found[k][1] != price:
                conflict.add(k)
            found[k] = (s, price, currency)
    on_demand = {k: by_region(skus, GPU[k]) for k in {k for k, _, _ in found}}
    out = []
    for (key, region, years), (s, price, currency) in sorted(found.items()):
        if (key, region, years) in conflict:
            log.warning("gcp: %s %d-year commitment priced twice in %s; skipped", key, years, region)
            continue
        out.append(GcpCommitPrice(
            gpu_key=key, accelerator=ACCELERATOR[key], canonical_gpu_name=canonical.canonical_gpu_name(ACCELERATOR[key]),
            region=region, term_years=years, usage_type=s["category"]["usageType"], description=s["description"],
            price_per_gpu_hour=price, on_demand_gpu_hour=on_demand[key].get(region), currency=currency))
    return out
