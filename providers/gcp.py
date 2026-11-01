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
matched by a pattern. A region where two SKUs of one component disagree on the price is skipped, never guessed.
Attachable GPUs (T4, P4, V100, P100) are the GPU alone, per GPU; the N1 host is billed on top.

Regions. Many SKUs are priced for a whole region group ("running in Americas"), so "priced in a region" is not "sold
there" (Google's own page prices G2 in 41 regions; Compute Engine offers it in 18). So a machine type is listed in a
region only when Compute Engine itself offers it there AND every component is priced there:

    with GCP_SERVICE_ACCOUNT_JSON (+ GCP_PROJECT_ID): read-only Compute Engine lists, every 6 hours,
        GET compute/v1/projects/{p}/aggregated/machineTypes      which machine types each zone offers, with shape
        GET compute/v1/projects/{p}/aggregated/acceleratorTypes  which attachable GPUs each zone offers
      A zone counts for a machine type only when its shape (vCPUs, memory, GPU type and count, bundled Local SSD
      partitions x 375 GiB) is exactly the proven one in MACHINES; a region is offered when one of its zones is.
    without it: Iowa (us-central1) only, as before (FALLBACK_REGIONS).

Listing ids are `<machine type>:<region>` in every region, so Iowa's ids are unchanged.

Proof outside Iowa: Google's accelerator-optimized page embeds per-region on-demand tables for G2 (41 regions) and
G4 (24 regions); the assembled prices reproduce all of them exactly (tests/test_provider_gcp.py). A2 and A3 are
embedded for Iowa only, so outside Iowa they are the same proven shape assembled from that region's own component
SKUs, not checked against a page (methodology/gcp-regions.md).

Calendar-mode advice (capacity): see `calendar_advice` below. Raw only for now; no capacity rows.

Raw: only the SKUs that matter (GPU resource group, the machine families' core and RAM, Local SSD) are stored, with
every usage type, so Spot, 1- and 3-year commitments and Calendar Mode can be read later without refetching.
Listings: on-demand only for now.

These are list prices, which is why the provider class is "hyperscaler". Replaced the price-page scraper 2026-10-30.
"""

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx

import canonical
from models import ComputeListing, RawResponse
from providers import gcp_auth
from providers.base import PollingPolicy, Provider, find_endpoint, reshaped

if TYPE_CHECKING:
    from tables import RawSnapshot

log = logging.getLogger(__name__)

BASE_URL = "https://cloudbilling.googleapis.com"
ENDPOINT = "/v1/services/6F81-5844-456A/skus"  # Compute Engine
PAGE_SIZE = 5000
MAX_PAGES = 20
HOURS_PER_MONTH = Decimal(730)  # Google's own month for converting GiB-month storage prices
# Without the service account's Compute Engine lists there is no way to tell "priced in a region" from "sold there",
# so only Iowa (where every shape is proven against Google's page) is listed.
FALLBACK_REGIONS = ("us-central1",)

# Compute Engine (read-only, OAuth from the service account). Stored endpoints are these templates: the project id
# stays out of the endpoint (it is in the request record).
COMPUTE_URL = "https://compute.googleapis.com"
MACHINE_TYPES = "/compute/v1/projects/{project}/aggregated/machineTypes"
ACCELERATOR_TYPES = "/compute/v1/projects/{project}/aggregated/acceleratorTypes"
CALENDAR_ADVICE = "/compute/v1/projects/{project}/regions/{region}/advice/calendarMode"
COMPUTE_EVERY = 6 * 3600   # machine and accelerator types per zone change rarely
COMPUTE_FRESH = timedelta(days=2)  # zone lists older than this (vs the catalogue) are not used
LOCAL_SSD_PARTITION_GIB = 375

# Region -> ISO country, for the regions Compute Engine offers GPUs in (cloud.google.com/compute/docs/regions-zones).
COUNTRY = {
    "asia-east1": "TW", "asia-east2": "HK", "asia-northeast1": "JP", "asia-northeast2": "JP", "asia-northeast3": "KR",
    "asia-south1": "IN", "asia-south2": "IN", "asia-southeast1": "SG", "asia-southeast2": "ID",
    "australia-southeast1": "AU", "australia-southeast2": "AU", "africa-south1": "ZA",
    "europe-central2": "PL", "europe-north1": "FI", "europe-north2": "SE", "europe-southwest1": "ES",
    "europe-west1": "BE", "europe-west2": "GB", "europe-west3": "DE", "europe-west4": "NL", "europe-west6": "CH",
    "europe-west8": "IT", "europe-west9": "FR", "europe-west10": "DE", "europe-west12": "IT",
    "me-central1": "QA", "me-central2": "SA", "me-west1": "IL",
    "northamerica-northeast1": "CA", "northamerica-northeast2": "CA", "northamerica-south1": "MX",
    "southamerica-east1": "BR", "southamerica-west1": "CL",
}


def country(region: str) -> str | None:
    return "US" if region.startswith("us-") else COUNTRY.get(region)

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

# What is kept of each zone's machine / accelerator type (the rest is links, ids and timestamps).
_MACHINE_FIELDS = ("name", "zone", "guestCpus", "memoryMb", "accelerators", "bundledLocalSsds")
_ACCEL_FIELDS = ("name", "zone", "maximumCardsPerInstance")

# Calendar-mode advice (compute.advice.calendarMode, POST .../regions/{r}/advice/calendarMode). It answers "where and
# when would a future reservation of N VMs of this shape, in this time window, be recommended", per zone: a
# recommended zone and start/end, and for the other zones RECOMMENDED / NO_CAPACITY / CONDITIONS_NOT_MET /
# NOT_SUPPORTED. That is calendar-mode reservation availability, not on-demand stock. Nothing is reserved: the
# answer is advice only; creating a future reservation is a different call this account is not allowed to make.
# A handful of key shapes x regions, every ADVICE_EVERY; the window is fixed so answers compare over time:
# one VM, starting 1 to 14 days from now, for 1 to 7 days.
ADVICE_TARGETS = (
    ("a3-highgpu-8g", "us-central1"),
    ("a3-megagpu-8g", "us-central1"),
    ("a3-ultragpu-8g", "europe-west4"),
    ("a3-highgpu-8g", "europe-west4"),
)
ADVICE_EVERY = 6 * 3600
ADVICE_UNAVAILABLE_EVERY = 24 * 3600   # the project cannot use advice yet (see advice_unavailable): once a day
ADVICE_WINDOW = {"start_after": timedelta(days=1), "start_before": timedelta(days=14),
                 "min_duration": timedelta(days=1), "max_duration": timedelta(days=7)}
_UNAVAILABLE = "The service is not available for this project."


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def advice_request(machine_type: str, now: datetime, instances: int = 1) -> dict:
    w = ADVICE_WINDOW
    return {"futureResourcesSpecs": {"spec": {
        "targetResources": {"specificSkuResources": {"machineType": machine_type, "instanceCount": str(instances)}},
        "timeRangeSpec": {"startTimeNotEarlierThan": _iso(now + w["start_after"]),
                          "startTimeNotLaterThan": _iso(now + w["start_before"]),
                          "minDuration": f"{int(w['min_duration'].total_seconds())}s",
                          "maxDuration": f"{int(w['max_duration'].total_seconds())}s"},
        "deploymentType": "DENSE"}}}


def advice_unavailable(raw) -> bool:
    """Google's answer for a project that cannot use the advice service (seen 2026-11-01 for this new project, for
    calendar mode on GPUs and TPUs and for the capacity advisor alike): HTTP 400 "The service is not available for
    this project." It is an account state, not a capacity answer."""
    if raw.status_code != 400 or not isinstance(raw.payload, dict):
        return False
    return ((raw.payload.get("error") or {}).get("message") or "").strip() == _UNAVAILABLE


def calendar_advice(snapshots) -> list[dict]:
    """Stored advice answers read back, one per target: the recommended zone and window, and every other zone's
    status. Pure. Raw only for now: not mapped to capacity rows until a real answer has been seen and documented."""
    out = []
    for r in snapshots:
        req = r.request if isinstance(r.request, dict) else {}
        recs = (r.payload or {}).get("recommendations") if isinstance(r.payload, dict) else None
        if not isinstance(recs, list):
            continue
        for rec in recs:
            spec = ((rec or {}).get("recommendationsPerSpec") or {}).get("spec") or {}
            out.append({"machine_type": req.get("machine_type"), "region": req.get("region"),
                        "recommended_zone": (spec.get("location") or "").removeprefix("zones/") or None,
                        "start": spec.get("startTime"), "end": spec.get("endTime"),
                        "other_zones": {z.removeprefix("zones/"): (v or {}).get("status")
                                        for z, v in (spec.get("otherLocations") or {}).items()},
                        "observed_at": r.fetched_at})
    return out


def _region(zone: str) -> str:
    return zone.rsplit("/", 1)[-1].rsplit("-", 1)[0]


def _shape_ok(mtype: str, m: dict) -> bool:
    """The zone's machine type is exactly the proven shape (so the proven price assembly applies there)."""
    count, _, accel, _, vcpu, ram, lssd = MACHINES[mtype]
    accels = [(a.get("guestAcceleratorType"), a.get("guestAcceleratorCount")) for a in m.get("accelerators") or []]
    partitions = (m.get("bundledLocalSsds") or {}).get("partitionCount") or 0
    return (m.get("guestCpus") == vcpu and m.get("memoryMb") == ram * 1024 and accels == [(accel, count)]
            and partitions * LOCAL_SSD_PARTITION_GIB == lssd)


@dataclass(frozen=True)
class Offered:
    """Where Compute Engine offers each machine type and attachable GPU, from the service account's zone lists."""

    machines: dict[str, set[str]]      # machine type -> regions
    accelerators: dict[str, set[str]]  # accelerator type -> regions
    observed_at: object = None

    def gpu_regions(self, key: str) -> set[str]:
        """Regions where the GPU behind a GPU key is offered: in a proven machine type, or attachable."""
        out = set(self.accelerators.get(ATTACHABLE[key], set())) if key in ATTACHABLE else set()
        for mtype, (_, k, *_rest) in MACHINES.items():
            if k == key:
                out |= self.machines.get(mtype, set())
        return out


def offered(by_endpoint: "dict[str, list[RawSnapshot]]") -> Offered | None:
    """Offered regions from the newest stored Compute Engine lists. None (so: Iowa only) without both lists, when
    they are more than COMPUTE_FRESH older than the newest catalogue (the service account was removed or has been
    failing), or when they are reshaped (logged: prices must not stop because the region lists broke).
    A zone whose shape differs from the proven one is not counted, and is logged."""
    mt, at = find_endpoint(by_endpoint, MACHINE_TYPES), find_endpoint(by_endpoint, ACCELERATOR_TYPES)
    if not mt or not at:
        return None
    cat = find_endpoint(by_endpoint, ENDPOINT)
    oldest = min(mt[0].fetched_at, at[0].fetched_at)
    if cat and cat[0].fetched_at and oldest and cat[0].fetched_at - oldest > COMPUTE_FRESH:
        log.warning("gcp: Compute Engine zone lists are older than %s; Iowa only", COMPUTE_FRESH)
        return None
    machines_raw = mt[0].payload.get("machineTypes") if isinstance(mt[0].payload, dict) else None
    accels_raw = at[0].payload.get("acceleratorTypes") if isinstance(at[0].payload, dict) else None
    if not isinstance(machines_raw, list) or not isinstance(accels_raw, list):
        log.error("gcp: Compute Engine machine or accelerator types reshaped; Iowa only")
        return None
    machines: dict[str, set[str]] = {}
    for m in machines_raw:
        name, zone = m.get("name"), m.get("zone")
        if name not in MACHINES or not zone:
            continue
        if _shape_ok(name, m):
            machines.setdefault(name, set()).add(_region(zone))
        else:
            log.warning("gcp: %s in %s is not the proven shape; zone not counted", name, zone)
    accelerators: dict[str, set[str]] = {}
    for a in accels_raw:
        if a.get("name") and a.get("zone"):
            accelerators.setdefault(a["name"], set()).add(_region(a["zone"]))
    if not machines.get("a3-highgpu-8g"):
        log.error("gcp: Compute Engine lists no proven a3-highgpu-8g anywhere (reshaped?); Iowa only")
        return None
    return Offered(machines, accelerators, mt[0].fetched_at)


def unit_price(sku: dict) -> Decimal | None:
    """The SKU's highest-tier unit price (GPU and core SKUs have one tier), or None when it has none."""
    info = (sku.get("pricingInfo") or [{}])[0]
    rates = (info.get("pricingExpression") or {}).get("tieredRates") or []
    if not rates:
        return None
    p = rates[-1].get("unitPrice") or {}
    return Decimal(str(p.get("units") or 0)) + Decimal(p.get("nanos") or 0) / Decimal(10**9)


def _on_demand_skus(skus: list[dict], pattern: str):
    """The priced on-demand SKUs of one component: (sku, unit price)."""
    rx = re.compile(pattern)
    for s in skus:
        d = s.get("description") or ""
        if (s.get("category") or {}).get("usageType") != "OnDemand" or not rx.search(d) or _ONDEMAND_ONLY.search(d):
            continue
        price = unit_price(s)
        if price is not None:
            yield s, price


def effective_by_region(skus: list[dict], pattern: str) -> dict[str, datetime]:
    """Per region, the newest `pricingInfo.effectiveTime` among one component's on-demand SKUs (changed_at.py).
    Google reports it within the queried range, so it is a lower bound: the price was in force by then."""
    import changed_at

    out: dict[str, datetime] = {}
    for s, _price in _on_demand_skus(skus, pattern):
        t = changed_at.parse_time(((s.get("pricingInfo") or [{}])[0] or {}).get("effectiveTime"))
        if t is None:
            continue
        for region in s.get("serviceRegions") or []:
            out[region] = max(out.get(region, t), t)
    return out


def by_region(skus: list[dict], pattern: str) -> dict[str, Decimal]:
    """On-demand price per region for one component. A region priced twice with different values is dropped."""
    out, conflict = {}, set()
    for s, price in _on_demand_skus(skus, pattern):
        for region in s.get("serviceRegions") or []:
            if region in out and out[region] != price:
                conflict.add(region)
            out[region] = price
    for region in conflict:
        log.warning("gcp: %r priced twice in %s; region skipped", pattern, region)
        out.pop(region)
    return out


def _listing(sku, accel, count, price, region, observed_at, tier, vcpu=None, ram=None, **changed) -> ComputeListing:
    return ComputeListing(
        **changed,
        provider="gcp",
        sku=sku,
        listing_id=f"{sku}:{region}",
        raw_gpu_name=accel,
        canonical_gpu_name=canonical.canonical_gpu_name(accel),
        gpu_count=count,
        region=region,
        country=country(region),
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
    # List prices change rarely; the catalogue is ~7 pages, the Compute Engine zone lists ~5 more every 6 hours.
    # Slow tier: the Catalog is 'up to 12 h old' by Google's own account, so every 6 h loses nothing.
    polling = PollingPolicy(interval_seconds=21600, timeout_seconds=180, tier="slow")

    def __init__(self, api_key: str, client: httpx.AsyncClient | None = None,
                 service_account: "gcp_auth.ServiceAccount | None" = None, project_id: str | None = None,
                 compute_client: httpx.AsyncClient | None = None):
        super().__init__(
            client
            or httpx.AsyncClient(
                base_url=BASE_URL,
                headers={"X-Goog-Api-Key": api_key, "accept": "application/json"},
                timeout=90,
            )
        )
        # Compute Engine: a separate client (the Billing API key never goes to it), bearer token per request.
        self._project = project_id or (service_account.project_id if service_account else None)
        self._sa = service_account if self._project else None
        self._compute = None
        if self._sa:
            self._compute = compute_client or httpx.AsyncClient(base_url=COMPUTE_URL, timeout=60,
                                                                headers={"accept": "application/json"})
            self._tokens = gcp_auth.TokenSource(self._sa, self._compute)
            # calendar-mode advice is a POST, which compute.readonly does not cover; IAM keeps the account read-only
            self._advice_tokens = gcp_auth.TokenSource(self._sa, self._compute, scope=gcp_auth.ADVICE_SCOPE)
        self._next_compute = 0.0
        self._next_advice = 0.0

    async def aclose(self) -> None:
        await super().aclose()
        if self._compute is not None:
            await self._compute.aclose()

    async def fetch(self) -> list[RawResponse]:
        out = await self._catalogue()
        if self._sa:
            try:
                out += await self._compute_lists()
                out += await self._advice()
            except Exception as exc:  # fail open: prices above are already in hand
                log.warning("gcp: Compute Engine fetch failed (%s); prices unaffected", type(exc).__name__)
        return out

    async def _catalogue(self) -> list[RawResponse]:
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

    # -- Compute Engine (service account, read-only) --

    async def _auth(self, tokens: "gcp_auth.TokenSource") -> dict | RawResponse:
        """The bearer header, or a failed response to store (status only: no key, assertion or token in it)."""
        started = datetime.now(timezone.utc)
        try:
            return {"Authorization": f"Bearer {await tokens.token()}"}
        except gcp_auth.GcpAuthError as exc:
            return RawResponse(provider=self.name, endpoint=gcp_auth.TOKEN_URI, method="POST", fetched_at=started,
                               ok=False, error=str(exc))

    async def _compute_lists(self) -> list[RawResponse]:
        """Machine types (GPU families) and accelerator types per zone, each stored as one compact response."""
        if time.monotonic() < self._next_compute:
            return []
        auth = await self._auth(self._tokens)
        if isinstance(auth, RawResponse):
            return [auth]
        out = [
            await self._aggregated(MACHINE_TYPES, "machineTypes", auth, _MACHINE_FIELDS,
                                   keep=lambda m: bool(m.get("accelerators")),
                                   params={"filter": 'name = "a*" OR name = "g*"'}),
            await self._aggregated(ACCELERATOR_TYPES, "acceleratorTypes", auth, _ACCEL_FIELDS,
                                   keep=lambda a: str(a.get("name", "")).startswith("nvidia-")),
        ]
        if all(r.ok for r in out):
            self._next_compute = time.monotonic() + COMPUTE_EVERY
        return out

    async def _aggregated(self, template: str, key: str, auth: dict, fields: tuple[str, ...], keep, params=None
                          ) -> RawResponse:
        path = template.format(project=self._project)
        kept, first, token, unreachable = [], None, None, []
        for _ in range(MAX_PAGES):
            resp = await self.get(path, client=self._compute, headers=auth, follow_redirects=False,
                                  params={**(params or {}), "maxResults": 500, **({"pageToken": token} if token else {})})
            resp = resp.model_copy(update={"endpoint": template})
            items = resp.payload.get("items") if resp.ok and isinstance(resp.payload, dict) else None
            if not resp.ok or not isinstance(items, dict):
                return resp if not resp.ok else resp.model_copy(
                    update={"ok": False, "payload": None, "error": f"gcp: {key} has no items map - response reshaped"})
            first = first or resp
            for scope in items.values():
                for x in (scope or {}).get(key) or []:
                    if isinstance(x, dict) and keep(x):
                        y = {f: x[f] for f in fields if f in x}
                        if isinstance(y.get("zone"), str):  # accelerator types give a URL naming the project
                            y["zone"] = y["zone"].rsplit("/", 1)[-1]
                        kept.append(y)
            unreachable += resp.payload.get("unreachables") or []
            token = resp.payload.get("nextPageToken")
            if not token:
                kept.sort(key=lambda x: (str(x.get("name")), str(x.get("zone"))))
                return reshaped(first, {key: kept, **({"unreachables": sorted(unreachable)} if unreachable else {})})
        return first.model_copy(update={"ok": False, "payload": None, "error": f"more than {MAX_PAGES} pages"})

    async def _advice(self) -> list[RawResponse]:
        """Calendar-mode advice for ADVICE_TARGETS (see calendar_advice). Never creates a reservation."""
        if time.monotonic() < self._next_advice:
            return []
        auth = await self._auth(self._advice_tokens)
        if isinstance(auth, RawResponse):
            return [auth]
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        out = []
        for mtype, region in ADVICE_TARGETS:
            body = advice_request(mtype, now)
            raw = await self.post(CALENDAR_ADVICE.format(project=self._project, region=region), client=self._compute,
                                  headers=auth, json=body, follow_redirects=False)
            raw = raw.model_copy(update={"endpoint": CALENDAR_ADVICE,
                                         "request": {"region": region, "machine_type": mtype, "body": body}})
            out.append(raw)
            if advice_unavailable(raw):
                break  # the project cannot use advice at all: one call is enough to record that
        unavailable = any(advice_unavailable(r) for r in out)
        self._next_advice = time.monotonic() + (ADVICE_UNAVAILABLE_EVERY if unavailable else ADVICE_EVERY)
        return out

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

        where = offered(by_endpoint)
        # When each component's price took effect (a lower bound); an assembled VM price is in force once all are.
        eff = {p: effective_by_region(skus, p) for p in {*GPU.values(), *(x for cr in FAMILY.values() for x in cr), SSD}}

        def since(region: str, *patterns: str) -> dict:
            times = [eff[p].get(region) for p in patterns]
            if not times or any(t is None for t in times):
                return {}
            return {"provider_changed_at": max(times), "changed_at_basis": "lower_bound"}

        listings = []
        for mtype, (count, key, accel, family, vcpu, ram, lssd) in MACHINES.items():
            cores, mem = fam[family]
            sold = where.machines.get(mtype, set()) if where else set(FALLBACK_REGIONS)
            for region, g in sorted(gpu[key].items()):
                if region not in sold or region not in cores or region not in mem or (lssd and region not in ssd):
                    continue  # not offered there, or a component is not priced there
                price = count * g + vcpu * cores[region] + ram * mem[region] + (lssd * ssd[region] if lssd else 0)
                parts = (GPU[key], *FAMILY[family]) + ((SSD,) if lssd else ())
                listings.append(_listing(mtype, accel, count, price, region, snapshot.fetched_at, "hyperscaler",
                                         vcpu=vcpu, ram=float(ram), **since(region, *parts)))
        for key, accel in ATTACHABLE.items():
            sold = where.accelerators.get(accel, set()) if where else set(FALLBACK_REGIONS)
            for region, g in sorted(gpu[key].items()):
                if region in sold:
                    listings.append(_listing(accel, accel, 1, g, region, snapshot.fetched_at, "gpu-attachment",
                                             **since(region, GPU[key])))
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
    """Every GPU commitment SKU priced where Compute Engine offers that GPU (Iowa only without the service account's
    zone lists, as for listings), one per (GPU, region, term). Pure, read-only.

    A region priced twice with different values for one GPU and term is skipped, never guessed (as by_region does).
    Not called by normalize(): nothing here becomes a listing, an index vote or a market statistic.
    """
    rows = find_endpoint(by_endpoint, ENDPOINT)
    skus = rows[0].payload.get("skus") if rows and isinstance(rows[0].payload, dict) else None
    if not isinstance(skus, list):
        return []
    where = offered(by_endpoint)
    sold = {k: (where.gpu_regions(k) if where else set(FALLBACK_REGIONS)) for k in ACCELERATOR}
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
            if region not in sold[key]:
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
