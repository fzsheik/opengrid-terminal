"""Massed Compute: the official GPU inventory API with a key; the public price page without one.

With MASSEDCOMPUTE_API_KEY (a read-only token from vm.massedcompute.com/settings/api):

    GET https://vm.massedcompute.com/api/v1/gpu-inventory   Authorization: Bearer <token>

returns `gpu_inventory`, one entry per product (e.g. "gpu_8x_a6000") with:

    instance_type.description          "8x RTX A6000 (48GB)": the GPU count, then the same model text the page heads
                                       its tables with, so names, skus and canonical mapping carry over unchanged
    instance_type.price_cents_per_hour per instance; the per-GPU rate is derived
    instance_type.specs                vcpu_count, memory_gib, storage_gb
    capacity_available                 instances of that shape that can be launched now (a count)
    regions_with_capacity_available    the regions with stock (name, description); empty when there is none

Documented at vm-docs.massedcompute.com/api/v1 (OpenAPI: /redocusaurus/plugin-redoc-0.yaml). The docs say not to
use api.massedcompute.com. Products named "..._spot" ("[Spot]" in the description) are spot listings; CPU-only
products ("cpu_...") are skipped. The body is stored verbatim. The API call never follows redirects: an unknown
path or a bad token answers with a redirect to /login, and that must be a failure, not an HTML page read as data.

Without the key: the public /pricing page. Each GPU model is a heading followed by a table of shapes (quantity,
vCPU, RAM, storage, instance price). With the key, the page is read only when the API call fails, so prices do
not go dark.

One source per poll: normalize() and normalize_capacity() read whichever source has the newest successful
snapshot (the API, else the page / Shadeform), never both.

Capacity: with the key, `capacity_available` per product, provider-wide (the API gives a count per product, not per
region; the in-stock regions are kept in raw_value). Capacity skus are the listing skus ("{model}:{n}x", plus
":spot" for spot products), so capacity joins to price. Without the key, capacity comes from Shadeform's public
catalogue, which lists Massed Compute's types with per-region availability (yes/no); those skus are Shadeform's
type names (e.g. "gpu_8x_a6000") and join to listings by GPU and count, not by sku. Shadeform is not fetched when
the key is set, so Massed Compute capacity is never counted from both.
"""

import logging
import re
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx

import canonical
from models import CapacityObservation, ComputeListing, RawResponse
from providers import shadeform
from providers.base import PollingPolicy, Provider, find_endpoint, get_page, reshaped

if TYPE_CHECKING:
    from tables import RawSnapshot

log = logging.getLogger(__name__)

BASE_URL = "https://vm.massedcompute.com"
ENDPOINT = "/pricing"
API_ENDPOINT = "/api/v1/gpu-inventory"

# A model heading, or a table; document order ties each table to its heading.
_BLOCK = re.compile(
    r'sm:text-\[15px\]">(?P<model>[^<]+)</span>|(?P<table><table.*?</table>)', re.S
)
_TAGS = re.compile(r"<[^>]+>")
_PRICE = re.compile(r"^\$(\d+(?:,\d{3})*(?:\.\d+)?)\s*/hr$")


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", _TAGS.sub(" ", html)).strip()


def extract(html: str) -> dict:
    """Every priced shape on the page, grouped under its model heading.

    Raises when no priced shape is found or a table has no heading, so a
    redesign fails the poll instead of recording nothing.
    """
    model, shapes = None, []
    for m in _BLOCK.finditer(html):
        if m["model"]:
            model = _text(m["model"])
            continue
        header = [_text(c) for c in re.findall(r"<th.*?</th>", m["table"], re.S)]
        if header[:5] != ["Qty", "vCPU", "RAM", "Storage", "Price"]:
            continue
        if model is None:
            raise ValueError("massedcompute: price table with no model heading before it")
        for tr in re.findall(r"<tr.*?</tr>", m["table"], re.S)[1:]:
            cells = [_text(c) for c in re.findall(r"<td.*?</td>", tr, re.S)]
            if len(cells) < 5:
                continue
            shapes.append(dict(zip(["model", "qty", "vcpu", "ram", "storage", "price"], [model, *cells[:5]])))
    if not shapes:
        raise ValueError("massedcompute: no priced shapes found - page reshaped")
    return {"shapes": shapes}


_COUNT_MODEL = re.compile(r"^(\d+)\s*x\s+(.+)$", re.I)
_NAME_COUNT = re.compile(r"^gpu_(\d+)x_", re.I)
_SPOT = re.compile(r"\s*\[spot\]\s*", re.I)


def products(payload) -> list[dict]:
    """The GPU products in a gpu-inventory body, each as {name, model, count, market, cents, specs, entry}.

    Raises when the body is not the documented shape. CPU-only products and rows without a positive price are
    skipped; the caller treats zero products as a failure.
    """
    inv = payload.get("gpu_inventory") if isinstance(payload, dict) else None
    if not isinstance(inv, dict):
        raise ValueError("massedcompute: gpu-inventory has no gpu_inventory object - response reshaped")
    out = []
    for key, entry in inv.items():
        it = entry.get("instance_type") if isinstance(entry, dict) else None
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or key)
        if not name.lower().startswith("gpu_"):
            continue  # CPU-only VMs
        cents = it.get("price_cents_per_hour")
        if isinstance(cents, bool) or not isinstance(cents, (int, float)) or cents <= 0:
            continue
        desc = re.sub(r"\s+", " ", str(it.get("description") or "")).strip()
        m, n = _COUNT_MODEL.match(desc), _NAME_COUNT.match(name)
        count = int(m.group(1)) if m else int(n.group(1)) if n else 0
        model = m.group(2) if m else desc
        if count <= 0 or not model:
            continue
        spot = bool(_SPOT.search(model)) or name.lower().endswith("_spot")
        model = _SPOT.sub(" ", model).strip()
        out.append({"name": name, "model": model, "count": count, "market": "spot" if spot else "on_demand",
                    "cents": cents, "specs": it.get("specs") if isinstance(it.get("specs"), dict) else {},
                    "entry": entry})
    return out


def _sku(p: dict) -> str:
    """The page's "{model}:{n}x", so listings continue from the scraper; spot products add ":spot"."""
    return f"{p['model']}:{p['count']}x" + (":spot" if p["market"] == "spot" else "")


def _newest(by_endpoint: "dict[str, list[RawSnapshot]]", *fragments: str) -> tuple[str | None, "list[RawSnapshot]"]:
    """(fragment, snapshots) for whichever source has the newest snapshot; (None, []) when none has any."""
    best, rows = None, []
    for f in fragments:
        found = find_endpoint(by_endpoint, f)
        if found and (not rows or found[0].fetched_at > rows[0].fetched_at):
            best, rows = f, found
    return best, rows


def _number(s: str) -> float | None:
    m = re.search(r"\d+(?:\.\d+)?", s.replace(",", ""))
    return float(m.group()) if m else None


class MassedComputeProvider(Provider):
    name = "massedcompute"
    # One page load per poll.
    polling = PollingPolicy(interval_seconds=900, timeout_seconds=45)

    def __init__(self, client: httpx.AsyncClient | None = None, api_key: str | None = None):
        # The token goes only on the API request (fetch), never on the page or the Shadeform call.
        self._api_key = api_key
        super().__init__(
            client
            or httpx.AsyncClient(
                base_url=BASE_URL,
                headers={"user-agent": "opengrid-terminal/0.1"},
                timeout=45,
                follow_redirects=True,
            )
        )

    async def fetch(self) -> list[RawResponse]:
        if self._api_key:
            api = await self.get(API_ENDPOINT, follow_redirects=False,
                                 headers={"Authorization": f"Bearer {self._api_key}", "accept": "application/json"})
            if api.ok:
                try:
                    if not products(api.payload):
                        raise ValueError("massedcompute: gpu-inventory has no priced GPU products")
                except ValueError as exc:
                    api = api.model_copy(update={"ok": False, "error": str(exc)})
            if api.ok:
                return [api]
            # Recorded as a failure; the page keeps prices flowing. Capacity keeps its last API state.
            return [api, await get_page(self, ENDPOINT, extract)]
        catalogue = await self.get(shadeform.BASE_URL + shadeform.ENDPOINT)
        types = (catalogue.payload or {}).get("instance_types") if isinstance(catalogue.payload, dict) else None
        if isinstance(types, list):  # keep Massed Compute's rows only, verbatim
            catalogue = reshaped(catalogue, {"instance_types": [t for t in types if t.get("cloud") == "massedcompute"]})
        return [await get_page(self, ENDPOINT, extract), catalogue]

    @staticmethod
    def normalize_capacity(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[CapacityObservation]:
        """The API's count per product when its snapshot is the newest source, else Shadeform's yes/no."""
        source, rows = _newest(by_endpoint, API_ENDPOINT, shadeform.ENDPOINT)
        if source != API_ENDPOINT:
            return shadeform.shadeform_capacity("massedcompute", "massedcompute", by_endpoint)
        snapshot, out = rows[0], []
        for p in products(snapshot.payload):
            entry = p["entry"]
            count = entry.get("capacity_available")
            count = count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else None
            regions = [r.get("name") for r in entry.get("regions_with_capacity_available") or [] if isinstance(r, dict)]
            in_stock = count > 0 if count is not None else (True if regions else None)
            out.append(CapacityObservation(
                provider="massedcompute", sku=_sku(p), region=None, country=None, raw_gpu_name=p["model"],
                canonical_gpu_name=canonical.canonical_gpu_name(p["model"]), gpu_count=p["count"],
                in_stock=in_stock, available_count=count, count_unit=None if count is None else "instance",
                raw_value=f"{p['name']}: capacity_available={entry.get('capacity_available')}; "
                          f"regions={','.join(r for r in regions if r) or '-'}",
                source=API_ENDPOINT, observed_at=snapshot.fetched_at))
        return out

    @staticmethod
    def _normalize_api(snapshot: "RawSnapshot") -> list[ComputeListing]:
        listings = []
        for p in products(snapshot.payload):
            per_instance = Decimal(str(p["cents"])) / 100
            specs, spot = p["specs"], p["market"] == "spot"
            ram, storage = specs.get("memory_gib"), specs.get("storage_gb")
            listings.append(ComputeListing(
                provider="massedcompute",
                sku=_sku(p),
                listing_id=_sku(p),
                raw_gpu_name=p["model"],
                canonical_gpu_name=canonical.canonical_gpu_name(p["model"]),
                gpu_count=p["count"],
                region=None,  # one price list; the in-stock regions are capacity, not price
                country=None,
                price_per_gpu_hour=per_instance / p["count"],
                price_per_instance_hour=per_instance,
                currency="USD",
                market_type="spot" if spot else "on_demand",
                provider_tier=None,
                interruptible=spot,
                available=None,  # stock lives in capacity (normalize_capacity), not on the price row
                capacity=None,
                capacity_unit=None,
                vcpu=specs.get("vcpu_count") if isinstance(specs.get("vcpu_count"), int) else None,
                ram_gb=float(ram) if isinstance(ram, (int, float)) else None,
                storage_gb=float(storage) if isinstance(storage, (int, float)) else None,
                observed_at=snapshot.fetched_at,
            ))
        if not listings:
            raise ValueError("massedcompute: gpu-inventory has no priced GPU products")
        return listings

    @staticmethod
    def normalize(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        """The API's products when its snapshot is the newest source; else one page table row per listing."""
        source, rows = _newest(by_endpoint, API_ENDPOINT, ENDPOINT)
        if source == API_ENDPOINT:
            return MassedComputeProvider._normalize_api(rows[0])
        if not rows:
            return []
        snapshot = rows[0]

        listings = []
        for s in snapshot.payload["shapes"]:
            qty = re.sub(r"\D", "", s["qty"])
            price = _PRICE.match(s["price"])
            if not qty or int(qty) == 0 or price is None:
                continue
            count = int(qty)
            per_instance = Decimal(price.group(1).replace(",", ""))
            vcpu, ram, storage = _number(s["vcpu"]), _number(s["ram"]), _number(s["storage"])
            listings.append(
                ComputeListing(
                    provider="massedcompute",
                    sku=f"{s['model']}:{count}x",
                    listing_id=f"{s['model']}:{count}x",
                    raw_gpu_name=s["model"],
                    canonical_gpu_name=canonical.canonical_gpu_name(s["model"]),
                    gpu_count=count,
                    region=None,
                    country=None,
                    price_per_gpu_hour=per_instance / count,
                    price_per_instance_hour=per_instance,
                    currency="USD",
                    market_type="on_demand",
                    provider_tier=None,
                    interruptible=False,
                    available=None,
                    capacity=None,
                    capacity_unit=None,
                    vcpu=None if vcpu is None else int(vcpu),
                    ram_gb=ram,
                    storage_gb=storage,
                    observed_at=snapshot.fetched_at,
                )
            )
        return listings
