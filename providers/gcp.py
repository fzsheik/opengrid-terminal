"""Google Cloud Compute Engine: on-demand GPU list prices from the public price page, no key.

The Cloud Billing Catalog API needs an API key, and the old calculator price
file (cloudpricingcalculator.appspot.com/.../pricelist.json) is gone (404). The
public "Accelerator-optimized pricing" page is the keyless source; robots.txt
allows it and Google's terms only forbid automated access that robots.txt
disallows.

The page is server-rendered for one region, Iowa (us-central1), shown in its
region picker. Two sections are read:

  GPU-accelerated machines pricing   A4 / A3 / A2 / G4 / G2 machine types. The
      "Price (USD)" column is the WHOLE VM (GPUs + vCPUs + memory + bundled
      Local SSD); the per-GPU rate is that price / GPU count.
  GPU Attachment pricing             T4 / P4 / V100 / P100 attached to an N1 VM.
      "GPU price (USD)" is the GPU alone, per GPU; the host VM is billed on top.

The GPU model and count come from hand-kept tables (GPU_MACHINES, ATTACHABLE),
the way providers/aws.py does it, and are cross-checked against what the page
prints. A machine type missing from the table is logged and skipped, never
guessed; a row whose printed GPU or count disagrees with the table is skipped.

Not read: DWS Flex-start / Calendar Mode (a different consumption model), Spot
(the column mixes live and hand-typed figures with stray text, so it is not
trusted), every committed-use column, and the RTX Virtual Workstation table
(GPU plus a GRID licence).

These are list prices, which is why the provider class is "hyperscaler".
"""

import html as htmllib
import logging
import re
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx

import canonical
from models import ComputeListing, RawResponse
from providers.base import PollingPolicy, Provider, find_endpoint, get_page

if TYPE_CHECKING:
    from tables import RawSnapshot

log = logging.getLogger(__name__)

BASE_URL = "https://cloud.google.com"
ENDPOINT = "/products/compute/pricing/accelerator-optimized?hl=en"
REGION_LABEL = "Iowa (us-central1)"
REGION = "us-central1"

# machine type -> (GPUs, GCP accelerator type, GPU as the page prints it)
# Accelerator types are Compute Engine's own ids (gcloud compute accelerator-types list).
GPU_MACHINES: dict[str, tuple[int, str, str]] = {
    "a4-highgpu-8g": (8, "nvidia-b200", "Nvidia B200"),
    "a3-ultragpu-8g": (8, "nvidia-h200-141gb", "Nvidia H200"),
    "a3-megagpu-8g": (8, "nvidia-h100-mega-80gb", "Nvidia H100"),
    "a3-highgpu-8g": (8, "nvidia-h100-80gb", "Nvidia H100"),
    **{f"a2-ultragpu-{n}g": (n, "nvidia-a100-80gb", "Nvidia A100") for n in (1, 2, 4, 8)},
    **{f"a2-highgpu-{n}g": (n, "nvidia-tesla-a100", "Nvidia A100") for n in (1, 2, 4, 8)},
    "a2-megagpu-16g": (16, "nvidia-tesla-a100", "Nvidia A100"),
    # g4-standard-6/12/24 are 1/8, 1/4 and 1/2 of a card (fractional vGPU): not a
    # whole GPU, so deliberately left out.
    "g4-standard-48": (1, "nvidia-rtx-pro-6000", "NVIDIA RTX PRO 6000"),
    "g4-standard-96": (2, "nvidia-rtx-pro-6000", "NVIDIA RTX PRO 6000"),
    "g4-standard-192": (4, "nvidia-rtx-pro-6000", "NVIDIA RTX PRO 6000"),
    "g4-standard-384": (8, "nvidia-rtx-pro-6000", "NVIDIA RTX PRO 6000"),
    **{f"g2-standard-{v}": (1, "nvidia-l4", "NVIDIA L4") for v in (4, 8, 12, 16, 32)},
    "g2-standard-24": (2, "nvidia-l4", "NVIDIA L4"),
    "g2-standard-48": (4, "nvidia-l4", "NVIDIA L4"),
    "g2-standard-96": (8, "nvidia-l4", "NVIDIA L4"),
}
# attachable GPU as the page prints it -> (GCP accelerator type, GPU memory as printed)
ATTACHABLE: dict[str, tuple[str, str]] = {
    "NVIDIA T4": ("nvidia-tesla-t4", "16 GB GDDR6"),
    "NVIDIA P4": ("nvidia-tesla-p4", "8 GB GDDR5"),
    "NVIDIA V100": ("nvidia-tesla-v100", "16 GB HBM2"),
    "NVIDIA P100": ("nvidia-tesla-p100", "16 GB HBM2"),
}

_H = r"<h[23][^>]*>(?:<div[^>]*>)?(?:<p>)?(?:<span[^>]*>)?\s*{}\s*(?:</span>)?(?:</p>)?(?:</div>)?</h[23]>"
_MACHINES_HEAD = re.compile(_H.format("GPU-accelerated machines pricing"))
_ATTACH_HEAD = re.compile(_H.format("GPU Attachment pricing"))
_VWS_HEAD = re.compile(_H.format("NVIDIA RTX virtual workstations"))
# The region picker's displayed value, and its selected option.
_REGION_SHOWN = re.compile(r'jsname="Fb0Bif">([^<]*)<')
_REGION_SELECTED = re.compile(r'aria-selected="true" data-value="([^"]*)"')
_TABLE = re.compile(r"<table\b.*?</table>", re.S)
_TR = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.S)
_CELL = re.compile(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", re.S)
_TAG = re.compile(r"<[^>]+>")
_PRICE = re.compile(r"^\$(\d+(?:,\d{3})*(?:\.\d+)?) ?/ ?1 hour$")
_NO_PRICE = ("N/A", "-", "")
_COMPONENT = re.compile(r"(GPUs|vCPUs|Memory|SSD):\s*([^:]+?)(?=\s+(?:GPUs|vCPUs|Memory|SSD):|$)")
_MACHINE_TYPE = re.compile(r"^[a-z]\d[a-z]?-[a-z]+-\d+g?$")


def _text(cell: str) -> str:
    return re.sub(r"\s+", " ", htmllib.unescape(_TAG.sub(" ", cell))).strip()


def _rows(table: str) -> list[list[str]]:
    return [[_text(c) for c in _CELL.findall(tr)] for tr in _TR.findall(table)]


def _check_price(cell: str, where: str) -> str:
    if cell not in _NO_PRICE and _PRICE.match(cell) is None:
        raise ValueError(f"gcp: {where} price cell is not '$x / 1 hour': {cell!r}")
    return cell


def _section(page: str, start: re.Pattern, end: re.Pattern, name: str) -> str:
    a = start.search(page)
    b = end.search(page, a.end()) if a else None
    if a is None or b is None:
        raise ValueError(f"gcp: '{name}' section not found - page reshaped")
    body = page[a.end() : b.start()]
    regions = set(_REGION_SHOWN.findall(body)) | set(_REGION_SELECTED.findall(body))
    if regions != {REGION_LABEL}:
        raise ValueError(f"gcp: '{name}' is priced for {sorted(regions)}, not {REGION_LABEL}")
    return body


def _machines(body: str) -> list[dict]:
    """Every machine-type row of the GPU-accelerated tables, cells kept as printed."""
    out = []
    for table in _TABLE.findall(body):
        rows = _rows(table)
        head = rows[0] if rows else []
        if head[:3] != ["Machine type", "GPU", "Components"] or len(head) < 4 or not head[3].startswith("Price (USD)"):
            raise ValueError(f"gcp: machine table header changed: {head[:4]}")
        for cells in rows[1:]:
            if len(cells) != len(head):
                raise ValueError(f"gcp: machine row has {len(cells)} cells, header has {len(head)}")
            if not cells[1] and not cells[2]:
                continue  # a family heading row ("A3 High")
            if not _MACHINE_TYPE.match(cells[0]):
                raise ValueError(f"gcp: unreadable machine type {cells[0]!r}")
            parts = dict(_COMPONENT.findall(cells[2]))
            out.append({
                "machine_type": cells[0],
                "gpu": cells[1],
                "gpus": parts.get("GPUs"),
                "vcpus": parts.get("vCPUs"),
                "memory": parts.get("Memory"),
                "price": _check_price(cells[3], cells[0]),
            })
    if not out:
        raise ValueError("gcp: no GPU machine rows found - page reshaped")
    return out


def _attachable(body: str) -> list[dict]:
    """The attachable-GPU table: one priced row per model ("1 GPU"); larger counts repeat the per-GPU price."""
    tables = _TABLE.findall(body)
    if len(tables) != 1:
        raise ValueError(f"gcp: expected one GPU attachment table, found {len(tables)}")
    rows = _rows(tables[0])
    head = rows[0] if rows else []
    if head[:4] != ["Model", "GPUs", "GPU memory", "GPU price (USD)"]:
        raise ValueError(f"gcp: GPU attachment header changed: {head[:4]}")
    out = []
    for cells in rows[1:]:
        if len(cells) == len(head):
            if cells[1] != "1 GPU":
                raise ValueError(f"gcp: attachment row for {cells[0]} does not start at 1 GPU")
            out.append({"model": cells[0], "memory": cells[2], "price": _check_price(cells[3], cells[0])})
        elif len(cells) != 2:  # the "2 GPUs | 32 GB" continuation rows
            raise ValueError(f"gcp: attachment row has {len(cells)} cells")
    if not out:
        raise ValueError("gcp: no attachable GPU rows found - page reshaped")
    return out


def extract(page: str) -> dict:
    """The priced GPU rows of both sections, for us-central1. Raises when the page changed shape."""
    machines = _section(page, _MACHINES_HEAD, _ATTACH_HEAD, "GPU-accelerated machines pricing")
    attach = _section(page, _ATTACH_HEAD, _VWS_HEAD, "GPU Attachment pricing")
    return {"region": REGION_LABEL, "machines": _machines(machines), "attachable": _attachable(attach)}


def _price(cell: str) -> Decimal | None:
    m = _PRICE.match(cell)
    return Decimal(m.group(1).replace(",", "")) if m else None


def _int(cell: str | None) -> int | None:
    return int(cell.replace(",", "")) if cell and cell.replace(",", "").isdigit() else None


def _memory_gb(cell: str | None) -> float | None:
    m = re.match(r"^([\d,]+)\s*Gi?B$", cell or "")
    return float(m.group(1).replace(",", "")) if m else None


def _listing(sku, raw, count, price, observed_at, tier, vcpu=None, ram=None) -> ComputeListing:
    return ComputeListing(
        provider="gcp",
        sku=sku,
        listing_id=f"{sku}:{REGION}",
        raw_gpu_name=raw,
        canonical_gpu_name=canonical.canonical_gpu_name(raw),
        gpu_count=count,
        region=REGION,
        country="US",
        price_per_gpu_hour=price / count,
        price_per_instance_hour=price,
        currency="USD",
        market_type="on_demand",
        provider_tier=tier,
        interruptible=False,
        # A price page says nothing about capacity.
        available=None,
        capacity=None,
        capacity_unit=None,
        vcpu=vcpu,
        ram_gb=ram,
        storage_gb=None,
        observed_at=observed_at,
    )


class GcpProvider(Provider):
    name = "gcp"
    # List prices change rarely and the page is ~6 MB, so hourly is plenty.
    polling = PollingPolicy(interval_seconds=3600, timeout_seconds=60)

    def __init__(self, client: httpx.AsyncClient | None = None):
        super().__init__(
            client
            or httpx.AsyncClient(
                base_url=BASE_URL,
                headers={"user-agent": "opengrid-terminal/0.1", "accept-language": "en"},
                timeout=60,
                follow_redirects=True,
            )
        )

    async def fetch(self) -> list[RawResponse]:
        return [await get_page(self, ENDPOINT, extract)]

    @staticmethod
    def normalize(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        """One machine type, or one attachable GPU, becomes one on-demand listing."""
        rows = find_endpoint(by_endpoint, ENDPOINT.split("?")[0])
        if not rows:
            return []
        snapshot = rows[0]
        payload = snapshot.payload or {}
        listings: dict[str, ComputeListing] = {}

        for r in payload.get("machines") or []:
            mtype = r["machine_type"]
            spec = GPU_MACHINES.get(mtype)
            if spec is None:
                if not mtype.startswith("g4-standard-"):  # fractional G4 shapes are left out on purpose
                    log.info("gcp: machine type %s is not in GPU_MACHINES; skipped", mtype)
                continue
            count, accel, label = spec
            if _int(r["gpus"]) != count or r["gpu"].lower() != label.lower():
                log.warning("gcp: %s prints %r x %r, table says %d x %r; skipped", mtype, r["gpus"], r["gpu"], count, label)
                continue
            price = _price(r["price"])
            if price is None or price <= 0:
                continue  # N/A: not sold on demand (a4-highgpu-8g today)
            listing = _listing(mtype, accel, count, price, snapshot.fetched_at, "hyperscaler",
                               vcpu=_int(r["vcpus"]), ram=_memory_gb(r["memory"]))
            seen = listings.get(listing.listing_id)
            if seen is not None and seen.price_per_instance_hour != price:
                log.warning("gcp: %s printed with two prices; skipped", mtype)
                listings[listing.listing_id] = None
                continue
            listings.setdefault(listing.listing_id, listing)

        for r in payload.get("attachable") or []:
            spec = ATTACHABLE.get(r["model"])
            if spec is None:
                log.info("gcp: attachable GPU %r is not in ATTACHABLE; skipped", r["model"])
                continue
            accel, memory = spec
            if r["memory"] != memory:
                log.warning("gcp: %s prints %r, table says %r; skipped", r["model"], r["memory"], memory)
                continue
            price = _price(r["price"])
            if price is None or price <= 0:
                continue
            # The GPU alone, per GPU; the N1 host VM is billed on top.
            listing = _listing(accel, accel, 1, price, snapshot.fetched_at, "gpu-attachment")
            listings[listing.listing_id] = listing

        return [l for l in listings.values() if l is not None]
