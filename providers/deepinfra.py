"""DeepInfra GPU Instances: prices from the public price page; with a key, stock from the container API.

deepinfra.com sells GPU containers billed by the minute.

Prices, always: deepinfra.com/gpu-instances. Its "On-Demand Cloud Pricing" table has one row per GPU
configuration, each cell labelled for small screens ("GPU config" "1x NVIDIA B200", "GPU RAM" "180 GB",
"vCPU", "RAM", "Disk", "Price" "$4.89/hour"). GPU RAM is the instance total and the price is for the WHOLE
instance, so both are divided by the GPU count. Only that table is read, by its cell labels rather than column
position; the hero cards above it repeat some of the same prices and are not read (no double count). The page
names the card "NVIDIA B200 SXM6" in its spec block; the GPU name we store is "<config model> <per-GPU VRAM>GB",
e.g. "NVIDIA B200 180GB".

Stock, with DEEPINFRA_API_KEY: GET https://api.deepinfra.com/v1/containers/gpu_availability (`Authorization:
Bearer`), documented in DeepInfra's API reference ("Rent Gpu Availability", GPU Rentals). One row per GPU
configuration: `gpu_config` ("8xB200-180GB"), `usd_per_hour`, `available`. Only `available` is used, as
capacity in the boolean shape, joined to the page listings by sku ("8x NVIDIA B200"): NAMES maps the API's
codes to the page's model names, and a code not in NAMES keeps the API's own text (unmapped, never guessed).
The body is stored raw as received; a body with no readable rows is recorded as a failed fetch, never as
"nothing in stock". Capacity fails open: an API failure never touches prices. Read-only: this module never
calls POST /v1/containers (it starts a billed container).

Why the API's `usd_per_hour` is never published (it stays in the raw payload only): on 2026-11-01 it quoted a
B200 at $3.69 per GPU-hour while /gpu-instances and /pricing published $4.89, for every GPU a lower figure with
no stated reason, so it may be specific to our account. DeepInfra's terms (s.17, last modified 2026-10-15) count
non-public pricing as Confidential Information. So listings, indices and stats use the page price only.

Terms and robots: robots.txt allows /gpu-instances, and the terms do not mention automated reads of the API or
the pages. DeepInfra's per-token model API prices live on other pages and are never read.
"""

import html as _html
import logging
import re
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx

import canonical
from models import CapacityObservation, ComputeListing, RawResponse
from providers.base import PollingPolicy, Provider, find_endpoint, get_page

if TYPE_CHECKING:
    from tables import RawSnapshot

log = logging.getLogger(__name__)

BASE_URL = "https://deepinfra.com"
ENDPOINT = "/gpu-instances"
API_BASE_URL = "https://api.deepinfra.com"
API_ENDPOINT = "/v1/containers/gpu_availability"

# API gpu_config code -> the model name in the page's "GPU config" cell (checked 2026-11-01).
NAMES = {"B200": "NVIDIA B200"}
_API_CONFIG = re.compile(r"^(?P<count>\d+)x(?P<code>[A-Za-z0-9]+)-(?P<vram>\d+)GB$")

_SECTION = re.compile(r">On-Demand Cloud Pricing</h2>(?P<body>.*?)</table>", re.S)
_ROW = re.compile(r"<tr[^>]*>(?P<row>.*?)</tr>", re.S)
# <span aria-hidden="true">GPU RAM<!-- --> </span><span ...>180 GB</span>
_LABELLED = re.compile(
    r'<span[^>]*aria-hidden="true">(?P<label>[^<]+?)(?:<!-- -->)?\s*</span><span[^>]*>(?P<v>[^<]*)</span>'
)
_PRICE = re.compile(r"^\$(\d+(?:,\d{3})*(?:\.\d+)?)/hour$")
_CONFIG = re.compile(r"^(?P<count>\d+)x\s+(?P<model>\S.*)$")
_FIELDS = {"GPU config": "config", "GPU RAM": "gpu_ram", "vCPU": "vcpu", "RAM": "ram", "Disk": "disk", "Price": "price"}


def extract(html: str) -> dict:
    """Every row of the on-demand table, keyed by its cell labels.

    Raises when the table is gone or a row lost its config or price, so a
    redesigned page fails the poll.
    """
    sec = _SECTION.search(html)
    if sec is None:
        raise ValueError("deepinfra: 'On-Demand Cloud Pricing' table not found - page reshaped")
    rows = []
    for r in _ROW.finditer(sec["body"]):
        cells = {_FIELDS[m["label"].strip()]: _html.unescape(m["v"]).strip()
                 for m in _LABELLED.finditer(r["row"]) if m["label"].strip() in _FIELDS}
        if not cells:
            continue  # the header row
        if "config" not in cells or "price" not in cells or _PRICE.match(cells["price"]) is None:
            raise ValueError(f"deepinfra: a row lost its GPU config or '$x/hour' price: {cells!r}")
        rows.append(cells)
    if not rows:
        raise ValueError("deepinfra: no rows in the on-demand table - page reshaped")
    return {"rows": rows}


def _num(cell: str | None, unit: str) -> float | None:
    m = re.match(rf"^\s*(\d+(?:\.\d+)?)\s*{unit}\b", cell or "")
    return float(m.group(1)) if m else None


def _storage_gb(cell: str | None) -> float | None:
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*(TB|GB)\b", cell or "")
    if m is None:
        return None
    return float(m.group(1)) * (1000 if m.group(2) == "TB" else 1)


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def api_rows(payload) -> list[dict]:
    """The readable rows of a gpu_availability body: config parsed, `available` passed through.

    `usd_per_hour` is deliberately not read (see the module docstring). Raises when the body lost its `gpus`
    list, a row lost its gpu_config, or no config is readable, so a reshaped response fails the poll instead
    of reading as "nothing in stock". A config in an unknown format is skipped with a warning.
    """
    gpus = payload.get("gpus") if isinstance(payload, dict) else None
    if not isinstance(gpus, list) or not gpus:
        raise ValueError("deepinfra: gpu_availability has no `gpus` rows - response reshaped")
    rows = []
    for g in gpus:
        if not isinstance(g, dict) or not isinstance(g.get("gpu_config"), str):
            raise ValueError(f"deepinfra: a gpu_availability row lost its gpu_config: {g!r}"[:300])
        m = _API_CONFIG.match(g["gpu_config"].strip())
        if m is None or int(m["count"]) <= 0:
            log.warning("deepinfra: unreadable gpu_config %r; skipped", g["gpu_config"])
            continue
        count, code = int(m["count"]), m["code"]
        model = NAMES.get(code, code)
        rows.append({"config": g["gpu_config"], "count": count, "model": model, "sku": f"{count}x {model}",
                     "name": f"{model} {int(m['vram'])}GB", "available": g.get("available")})
    if not rows:
        raise ValueError("deepinfra: no readable gpu_config in gpu_availability - response reshaped")
    unknown = sorted({r["model"] for r in rows if r["model"] not in NAMES.values()})
    if unknown:
        log.warning("deepinfra: gpu codes not in NAMES (left unmapped): %s", unknown)
    return rows


class DeepInfraProvider(Provider):
    name = "deepinfra"
    # The price page every poll, plus one stock call with a key.
    polling = PollingPolicy(interval_seconds=900, timeout_seconds=45)

    def __init__(self, client: httpx.AsyncClient | None = None, api_key: str | None = None,
                 api_client: httpx.AsyncClient | None = None):
        super().__init__(
            client
            or httpx.AsyncClient(
                base_url=BASE_URL,
                headers={"user-agent": "opengrid-terminal/0.1"},
                timeout=45,
                follow_redirects=True,
            )
        )
        self._api_key = api_key
        self._api = None
        if api_key:
            self._api = api_client or httpx.AsyncClient(
                base_url=API_BASE_URL,
                headers={"Authorization": f"Bearer {api_key}", "accept": "application/json",
                         "user-agent": "opengrid-terminal/0.1"},
                timeout=45,
            )

    async def aclose(self) -> None:
        await super().aclose()
        if self._api is not None:
            await self._api.aclose()

    async def _stock(self) -> RawResponse:
        """The container API's stock call, stored raw; never raises, so prices are never at stake."""
        page_client, self._client = self._client, self._api
        try:
            raw = await self.get(API_ENDPOINT)
        finally:
            self._client = page_client
        if raw.ok:
            try:
                api_rows(raw.payload)
            except ValueError as exc:
                # Stored as received, but failed: zero readable rows is not "nothing in stock".
                raw = raw.model_copy(update={"ok": False, "error": str(exc)[:500]})
        return raw

    async def fetch(self) -> list[RawResponse]:
        responses = [await get_page(self, ENDPOINT, extract)]
        if self._api is not None:
            responses.append(await self._stock())
        return responses

    @staticmethod
    def normalize(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        """One on-demand row of the price page becomes one listing. The API is never read here."""
        rows = find_endpoint(by_endpoint, ENDPOINT)
        return _page_listings(rows[0]) if rows else []

    @staticmethod
    def normalize_capacity(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[CapacityObservation]:
        """The API's `available` flag per configuration (boolean shape), keyed by the page's sku."""
        snaps = find_endpoint(by_endpoint, API_ENDPOINT)
        if not snaps:
            return []  # no key, or no stock call stored yet: the page has no stock
        snapshot = snaps[0]
        out = []
        for r in api_rows(snapshot.payload):
            stock = r["available"] if isinstance(r["available"], bool) else None
            out.append(CapacityObservation(
                provider="deepinfra", sku=r["sku"], region=None, raw_gpu_name=r["name"],
                canonical_gpu_name=canonical.canonical_gpu_name(r["name"]), gpu_count=r["count"],
                in_stock=stock, raw_value=f"available={r['available']!r}; gpu_config={r['config']}",
                source=API_ENDPOINT, observed_at=snapshot.fetched_at))
        return out


def _page_listings(snapshot) -> list[ComputeListing]:
    """One on-demand row of the price page becomes one listing."""
    listings = []
    for r in snapshot.payload["rows"]:
        m = _CONFIG.match(r["config"])
        if m is None or int(m["count"]) <= 0:
            log.warning("deepinfra: unreadable GPU config %r; skipped", r["config"])
            continue
        count, model = int(m["count"]), m["model"].strip()
        total_vram = _num(r.get("gpu_ram"), "GB")
        if total_vram is None or total_vram % count:
            log.warning("deepinfra: %s has GPU RAM %r, not a whole per-GPU figure; skipped",
                        r["config"], r.get("gpu_ram"))
            continue
        name = f"{model} {int(total_vram // count)}GB"
        price = Decimal(_PRICE.match(r["price"]).group(1).replace(",", ""))
        vcpu = _num(r.get("vcpu"), "cores")
        listings.append(
            ComputeListing(
                provider="deepinfra",
                sku=r["config"],
                listing_id=f"{_slug(model)}:{count}x",
                raw_gpu_name=name,
                canonical_gpu_name=canonical.canonical_gpu_name(name),
                gpu_count=count,
                region=None,  # no region on the page
                country=None,
                # The printed price is for the whole instance.
                price_per_gpu_hour=price / count,
                price_per_instance_hour=price,
                currency="USD",
                market_type="on_demand",
                provider_tier="container",
                interruptible=False,
                available=None,  # the page says nothing about stock
                capacity=None,
                capacity_unit=None,
                vcpu=None if vcpu is None else int(vcpu),
                ram_gb=_num(r.get("ram"), "GB"),
                storage_gb=_storage_gb(r.get("disk")),
                observed_at=snapshot.fetched_at,
            )
        )
    return listings
