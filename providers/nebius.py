"""Nebius: on-demand prices from the public price page, spot prices from the public spot-price API. No key.

On-demand (GET nebius.com/prices). The page is a Next.js build. Its __NEXT_DATA__ script holds the page content as
a JSON string of blocks; the GPU prices are the "highlight-table-block" titled "NVIDIA GPU Instances", one price per
GPU-hour. Since late Oct 2026 (and from Jul to early Oct 2026) that table also carries a "Preemptible, GPU-hour"
column ("from $0.79": a floor of the spot prices) next to "On-demand, GPU-hour". The whole table is stored; the
preemptible column is never turned into listings (the spot API below is the spot source). When a price change is
announced the table gains a column headed "GPU-hour (Effective <date>)". Which column is current depends on the
date, so every price column is stored and normalize() picks by the fetch time.

Spot (GET api.nebius.cloud/billing/v1alpha1/spot-price, "The request doesn't require authentication",
docs.nebius.com/signup-billing/spot-prices.md; nebius/api billing/v1alpha1/spot_price*.proto). One item per SKU,
a SKU being a GPU platform in a region: skuId, productTitle, region, computeInstance.platform,
price {value, currency, unitTitle "GPU hour"}, effectiveTime; paged by pageToken / nextPageToken. Every page is
stored as one response, items verbatim. The API has spot prices only: no on-demand, preemptible-floor or reserved
fields. Its /list-series history (30 days) is not polled.

The two sources fail independently: a broken page never drops spot and a broken spot API never drops on-demand.
Either one parsing to zero rows is a failed poll, never "nothing for sale".
"""

import json
import logging
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

import httpx

import canonical
from models import ComputeListing, RawResponse
from providers.base import PollingPolicy, Provider, find_endpoint, get_page, reshaped

if TYPE_CHECKING:
    from tables import RawSnapshot

log = logging.getLogger(__name__)

BASE_URL = "https://nebius.com"
ENDPOINT = "/prices"
API_BASE_URL = "https://api.nebius.cloud"
SPOT_ENDPOINT = "/billing/v1alpha1/spot-price"
SPOT_PAGE_SIZE = 100  # the API's own default
SPOT_MAX_PAGES = 20

_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)
_GPU_TABLE = "NVIDIA GPU Instances"
_LEAD = ["Item", "vCPUs", "RAM, GB"]
_PRICE = re.compile(r"^(from )?\$(\d+(?:,\d{3})*(?:\.\d+)?)$")
_EFFECTIVE = re.compile(r"Effective ([A-Z][a-z]+ \d{1,2}, \d{4})")
# A preemptible (spot floor) column inside the on-demand table: stored, never a listing.
_PREEMPTIBLE = re.compile(r"^(Preemptible|Spot)\b", re.I)
# Nebius names that drop the card's edition -> the name Nebius's docs give it. Only the lookup uses it: raw_gpu_name
# keeps the page's text. docs.nebius.com/compute/virtual-machines/types (checked 2026-11-01), GPU types table:
# "NVIDIA RTX PRO 6000 Server Edition" for platforms gpu-rtx6000 and gpu-rtx6000-a. Scoped to Nebius on purpose:
# Modal prints the same words for an unstated edition, so canonical.py never maps the generic name.
DOC_NAMES = {"NVIDIA RTX PRO 6000": "NVIDIA RTX PRO 6000 Blackwell Server Edition"}


def canonical_name(raw_name: str) -> str | None:
    return canonical.canonical_gpu_name(DOC_NAMES.get(raw_name.strip(), raw_name))


# Words that mark a column as something other than the on-demand rate.
_NOT_ON_DEMAND = re.compile(r"spot|preemptible|reserv|commit|month|year", re.I)

# Spot API computeInstance.platform -> the item name the price page uses for that GPU, so spot and on-demand
# listings of one GPU share raw_gpu_name (and so canonical mapping). Platform ids: docs.nebius.com
# compute/virtual-machines/types.md (checked 2026-11-01). The "-a"/"-d" variants are the same GPU on another host
# CPU. A platform not here keeps the API's productTitle (minus "Preemptible ") and stays unmapped, never guessed.
PLATFORMS = {
    "gpu-h100-sxm": "NVIDIA HGX H100",
    "gpu-h200-sxm": "NVIDIA HGX H200",
    "gpu-b200-sxm": "NVIDIA HGX B200",
    "gpu-b200-sxm-a": "NVIDIA HGX B200",
    "gpu-b300-sxm": "NVIDIA HGX B300",
    "gpu-rtx6000": "NVIDIA RTX PRO 6000",
    "gpu-rtx6000-a": "NVIDIA RTX PRO 6000",
    "gpu-l40s-a": "NVIDIA L40S with Intel CPU",
    "gpu-l40s-d": "NVIDIA L40S with AMD CPU",
}
# Region -> ISO country, from docs.nebius.com/overview/regions.md (checked 2026-11-01). Unknown regions: None.
COUNTRY = {
    "eu-north1": "FI", "eu-north2": "IS", "eu-south1": "ES", "eu-west1": "FR", "eu-west2": "FR",
    "me-west1": "IL", "us-central1": "US", "us-north1": "US", "uk-south1": "GB", "uk-south2": "GB",
}


def _blocks(node, out: list) -> list:
    """Every highlight-table-block, including inside JSON-encoded strings."""
    if isinstance(node, dict):
        if node.get("type") == "highlight-table-block":
            out.append(node)
        for v in node.values():
            _blocks(v, out)
    elif isinstance(node, list):
        for v in node:
            _blocks(v, out)
    elif isinstance(node, str) and '"highlight-table-block"' in node:
        try:
            _blocks(json.loads(node), out)
        except ValueError:
            pass  # a string that merely mentions the block type
    return out


def _price_columns(header: list[str]) -> list[int]:
    """Indexes of the on-demand GPU-hour columns; raises on anything else.

    A "Preemptible, GPU-hour" column is recognised and skipped (spot comes from the API); any other column that
    is not an on-demand GPU-hour price (reserved, monthly, an unknown heading) fails the poll.
    """
    cols = [i for i in range(len(_LEAD), len(header)) if not _PREEMPTIBLE.search(header[i])]
    if header[: len(_LEAD)] != _LEAD or not cols:
        raise ValueError(f"nebius: GPU table header changed: {header!r}")
    for i in cols:
        c = header[i]
        if _NOT_ON_DEMAND.search(c) or not ("GPU-hour" in c or "On-demand" in c):
            raise ValueError(f"nebius: unexpected GPU table column: {c!r}")
    if sum(1 for i in cols if not _EFFECTIVE.search(header[i])) != 1:
        raise ValueError(f"nebius: expected one undated price column: {header!r}")
    return cols


def extract(html: str) -> dict:
    """The on-demand GPU price table, as the page prints it (preemptible column included).

    Raises when the table is missing or ambiguous, so a reshaped page fails the
    poll instead of recording nothing.
    """
    m = _NEXT_DATA.search(html)
    if m is None:
        raise ValueError("nebius: page no longer embeds __NEXT_DATA__")
    blocks = [b for b in _blocks(json.loads(m.group(1)), []) if (b.get("title") or "").strip() == _GPU_TABLE]
    # The page content can appear more than once; identical copies are one table.
    tables = []
    for b in blocks:
        content = (b.get("table") or {}).get("content")
        if not isinstance(content, list) or not content:
            raise ValueError("nebius: GPU table block has no content")
        if content not in tables:
            tables.append(content)
    if len(tables) != 1:
        raise ValueError(f"nebius: expected one on-demand GPU table, found {len(tables)}")
    header, rows = [str(c).strip() for c in tables[0][0]], tables[0][1:]
    _price_columns(header)
    if not rows or any(len(r) != len(header) or not all(isinstance(c, str) for c in r) for r in rows):
        raise ValueError("nebius: GPU table rows no longer match its header")
    return {"header": header, "rows": rows}


def _price(cell: str) -> tuple[Decimal, bool] | None:
    """(price, is_from_floor) for '$3.85' or 'from $1.55'; None when unpriced."""
    m = _PRICE.match(cell.strip())
    if m is None:
        if re.search(r"\d", cell):
            raise ValueError(f"nebius: price cell is not '$x.xx': {cell!r}")
        return None  # 'Contact us' and dashes
    return Decimal(m.group(2).replace(",", "")), bool(m.group(1))


def spot_name(item: dict) -> str:
    """The page's item name for this platform, else the API's own product title."""
    platform = str((item.get("computeInstance") or {}).get("platform") or "")
    if platform in PLATFORMS:
        return PLATFORMS[platform]
    title = re.sub(r"^Preemptible\s+", "", str(item.get("productTitle") or ""), flags=re.I)
    return title.replace("®", "").replace("™", "").strip() or platform


def spot_rows(payload) -> list[dict]:
    """The spot items, checked. Raises on a reshaped body, an item that is not a USD price per GPU hour, a
    duplicate (platform, region), or no items at all: zero rows is a failure, not an empty market."""
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise ValueError("nebius: spot-price response has no items list - response reshaped")
    if not items:
        raise ValueError("nebius: spot-price response has no items")
    out, seen = [], set()
    for it in items:
        if not isinstance(it, dict):
            raise ValueError(f"nebius: spot item is not an object: {it!r}"[:300])
        price = it.get("price") or {}
        platform = (it.get("computeInstance") or {}).get("platform")
        region = it.get("region")
        if not isinstance(platform, str) or not platform or not isinstance(region, str) or not region:
            raise ValueError(f"nebius: spot item without platform or region: {it!r}"[:300])
        if str(price.get("currency") or "").lower() != "usd" or str(price.get("unitTitle") or "").lower() != "gpu hour":
            raise ValueError(f"nebius: spot price is not USD per GPU hour: {price!r}"[:300])
        try:
            value = Decimal(str(price.get("value")))
        except (InvalidOperation, ValueError):
            raise ValueError(f"nebius: spot price value is not a decimal: {price!r}"[:300]) from None
        if not value.is_finite() or value <= 0:
            raise ValueError(f"nebius: spot price is not positive: {price!r}"[:300])
        if (platform, region) in seen:
            raise ValueError(f"nebius: two spot prices for {platform} in {region}")
        seen.add((platform, region))
        out.append({"name": spot_name(it), "platform": platform, "region": region, "price": value})
    return out


def _page_listings(snapshot) -> list[ComputeListing]:
    """One table row becomes one single-GPU on-demand listing (prices are per GPU)."""
    header, table = snapshot.payload["header"], snapshot.payload["rows"]

    cols = _price_columns(header)
    col = next(i for i in cols if not _EFFECTIVE.search(header[i]))
    # A dated column replaces the plain one once its date has arrived.
    for i in cols:
        m = _EFFECTIVE.search(header[i])
        if m and datetime.strptime(m.group(1), "%B %d, %Y").date() <= snapshot.fetched_at.date():
            col = i

    listings = []
    for row in table:
        item = row[0].strip()
        parsed = _price(row[col])
        if parsed is None:
            continue  # published without a price
        per_gpu, floor = parsed
        vcpu = row[1].strip()
        ram = row[2].strip()
        listings.append(
            ComputeListing(
                provider="nebius",
                sku=item,
                listing_id=item,
                raw_gpu_name=item,
                canonical_gpu_name=canonical_name(item),
                gpu_count=1,
                region=None,
                country=None,
                price_per_gpu_hour=per_gpu,
                price_per_instance_hour=per_gpu,
                currency="USD",
                market_type="on_demand",
                # 'from $x' is a floor over several configurations, not a quote.
                provider_tier="from_price" if floor else None,
                interruptible=False,
                available=None,
                capacity=None,
                capacity_unit=None,
                # Per-GPU resource share; a range ('8-40') has no single value.
                vcpu=int(vcpu) if vcpu.isdigit() else None,
                ram_gb=float(ram) if ram.replace(".", "", 1).isdigit() else None,
                storage_gb=None,
                observed_at=snapshot.fetched_at,
            )
        )
    return listings


def _spot_listings(snapshot) -> list[ComputeListing]:
    """One spot item (a GPU platform in a region) becomes one single-GPU, interruptible listing."""
    rows, listings = spot_rows(snapshot.payload), []
    for r in rows:
        name, platform, region = r["name"], r["platform"], r["region"]
        listings.append(
            ComputeListing(
                provider="nebius",
                sku=f"{name}:{platform}",
                listing_id=f"{name}:{platform}:{region}:spot",
                raw_gpu_name=name,
                canonical_gpu_name=canonical_name(name),
                gpu_count=1,  # priced per GPU hour
                region=region,
                country=COUNTRY.get(region),
                price_per_gpu_hour=r["price"],
                price_per_instance_hour=r["price"],
                currency="USD",
                market_type="spot",
                provider_tier=None,
                interruptible=True,
                available=None,
                capacity=None,
                capacity_unit=None,
                vcpu=None,
                ram_gb=None,
                storage_gb=None,
                observed_at=snapshot.fetched_at,
            )
        )
    unknown = sorted({r["platform"] for r in rows} - set(PLATFORMS))
    if unknown:
        log.warning("nebius: spot platforms not in PLATFORMS (left unmapped): %s", unknown)
    return listings


class NebiusProvider(Provider):
    name = "nebius"
    # One page load plus one spot-price call (a page per 100 SKUs) per poll.
    polling = PollingPolicy(interval_seconds=900, timeout_seconds=45)

    def __init__(self, client: httpx.AsyncClient | None = None, api_client: httpx.AsyncClient | None = None):
        super().__init__(
            client
            or httpx.AsyncClient(
                base_url=BASE_URL,
                headers={"user-agent": "opengrid-terminal/0.1"},
                timeout=45,
                follow_redirects=True,
            )
        )
        self._api = api_client or httpx.AsyncClient(
            base_url=API_BASE_URL,
            headers={"user-agent": "opengrid-terminal/0.1", "accept": "application/json"},
            timeout=30,
        )

    async def aclose(self) -> None:
        await super().aclose()
        await self._api.aclose()

    async def _spot(self) -> RawResponse:
        """Every spot-price page as one stored response. Never raises: a failure is a failed response, so the
        on-demand page is never at stake. A failed page fails the whole call (a partial list is not stored)."""
        page_client, self._client = self._client, self._api
        try:
            items, first, token = [], None, None
            for _ in range(SPOT_MAX_PAGES):
                params = {"pageSize": SPOT_PAGE_SIZE, **({"pageToken": token} if token else {})}
                resp = await self.get(SPOT_ENDPOINT, params=params)
                if not resp.ok:
                    return resp
                if not isinstance(resp.payload, dict) or not isinstance(resp.payload.get("items"), list):
                    return resp.model_copy(update={"ok": False, "error": "nebius: spot-price response has no items list"})
                first = first or resp
                items += resp.payload["items"]
                token = resp.payload.get("nextPageToken")
                if not token:
                    break
            else:
                return first.model_copy(update={"ok": False, "error": f"nebius: more than {SPOT_MAX_PAGES} spot pages"})
        finally:
            self._client = page_client
        raw = reshaped(first, {"items": items, "snapshotTime": first.payload.get("snapshotTime")})
        try:
            spot_rows(raw.payload)
        except ValueError as exc:
            # Stored as received, but failed: zero readable rows is not "nothing for sale".
            raw = raw.model_copy(update={"ok": False, "error": str(exc)[:500]})
        return raw

    async def fetch(self) -> list[RawResponse]:
        return [await get_page(self, ENDPOINT, extract), await self._spot()]

    @staticmethod
    def normalize(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        """On-demand listings from the page plus spot listings from the API.

        Each half is read on its own: if one raises, the other's listings still come back (and the failed half
        keeps its previous state). Only when every stored half fails does normalize raise.
        """
        # Exact keys: "/prices" is not a substring of the spot endpoint, but exact is clearer than fragments.
        halves = [
            ("page", by_endpoint.get(ENDPOINT) or [], _page_listings),
            ("spot", by_endpoint.get(SPOT_ENDPOINT) or [], _spot_listings),
        ]
        listings, errors = [], []
        for label, snaps, read in halves:
            if not snaps:
                continue
            try:
                listings += read(snaps[0])
            except Exception as exc:
                log.exception("nebius: %s normalizer failed; keeping its previous listings", label)
                errors.append(exc)
        if errors and len(errors) == sum(1 for _, snaps, _ in halves if snaps):
            raise errors[0]
        return listings
