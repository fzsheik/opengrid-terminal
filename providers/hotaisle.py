"""Hot Aisle: prices from the public price page; with an API key, stock from Hot Aisle's own API.

Prices, always: hotaisle.xyz/pricing prints one card per plan: the deployment kind ("VM" or
"Bare metal"), the GPU counts it comes in ("1x", "2x & 4x", "8x"), the GPU
("MI300x") and a price "$x.xx/GPU/hr". A card's price is per GPU, so a 4x VM is
4 times the card price.

Only the VM cards are read. The page describes the 8x bare-metal card as
"billed by the minute" while its own meta description calls it "8x MI300X
bare-metal monthly terms": that contradiction makes it unclear whether the
bare-metal price is on-demand, so it is skipped rather than guessed.

All plans are in one Michigan, US facility (the page says so); the country is
set only when the page still says it.

Stock, with HOTAISLE_API_KEY: Hot Aisle's API (Swagger 2.0 at admin.hotaisle.app/api/docs/swagger.json, base
https://admin.hotaisle.app/api, header `Authorization: Token <key>`). Every call is a read-only GET:

    GET /teams/                                      the teams the key has a role on (`handle`, `name`); used only to
                                                     resolve the team handle, never stored (it is account data)
    GET /teams/{handle}/virtual_machines/available/  VM types that can be provisioned now: `Quantity` (how many of
                                                     that type), `OnDemandPrice` (US cents/hour), `Specs.gpus`
    GET /teams/{handle}/bare_metal/available/        the same for bare metal; stored raw only (no listing to join)

The path takes the team's `handle` (a slug such as "acme-corporation"), not its display name; any other value
answers 404. HOTAISLE_TEAM may be the handle or the display name (case-insensitive); when it is unset or matches
neither and the key has exactly one team, that team is used (with a warning). The resolved handle is never stored:
the raw endpoint keeps the literal "{team}" placeholder.

Capacity: `Quantity` per VM type (GPU model and count), in the count shape, provider-wide, unit = instances, keyed
by the page's sku ("MI300x:1x:vm") so stock joins to price. The endpoint lists only types that can be provisioned
now, so a VM size the page sells but the response does not list is out of stock (in_stock false, count unknown).
The body is stored raw as received. An empty list means every size is taken (the endpoint lists only what can be
provisioned now), so every page size reads out of stock; null, a non-list or a list with no readable VM type is a
failed fetch, never "nothing in stock". Capacity fails open: an API failure never touches prices.

Why prices stay the page's even with the key: the endpoint lists only the sizes in stock right now (on 2026-11-01
only the 1x VM), so API-sourced prices would make the 2x/4x listings vanish whenever they sell out. Each poll the
API's `OnDemandPrice` (read as per instance, per hour) is checked against the page price for the same size: a
difference is logged and never published, since it may be account-specific pricing. On 2026-11-01 they agreed
(1x MI300X VM: 299 cents = $2.99, page $2.99/GPU/hr).

Terms: robots.txt allows everything. Hot Aisle's Acceptable Use Policy (last modified May 25th, 2024) binds account
holders and prohibits "Monitoring or crawling of a System ... that impairs or disrupts the System ..., or other
harvesting or scraping of any content of Hot Aisle Services"; the documented API is the sanctioned route, and the
page is read once per poll (900 s). Read-only: this module never calls a POST (provisioning is billed).
"""

import html as _html
import logging
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx

import canonical
from models import CapacityObservation, ComputeListing, RawResponse
from providers.base import PollingPolicy, Provider, find_endpoint, get_page

if TYPE_CHECKING:
    from tables import RawSnapshot

log = logging.getLogger(__name__)

BASE_URL = "https://hotaisle.xyz"
ENDPOINT = "/pricing"
API_BASE_URL = "https://admin.hotaisle.app/api"
TEAMS_ENDPOINT = "/teams/"
# Stored with the literal "{team}": the account's handle never reaches the raw store.
VM_ENDPOINT = "/teams/{team}/virtual_machines/available/"
BAREMETAL_ENDPOINT = "/teams/{team}/bare_metal/available/"

_CARD = re.compile(
    r'<p class="mb-2 font-bold[^"]*uppercase[^"]*">(?P<kind>[^<]+)</p>\s*<h2[^>]*>(?P<plan>[^<]+)</h2>\s*'
    r'<div[^>]*><span>(?P<counts>[^<]+)</span><span[^>]*>(?P<gpu>[^<]+)</span></div>\s*'
    r'<p[^>]*>(?P<price>[^<]+)</p>'
)
_PRICE = re.compile(r"^\$(\d+(?:\.\d+)?)/GPU/hr$")
_COUNTS = re.compile(r"\d+")


def extract(html: str) -> dict:
    """Every plan card, as printed, plus whether the page still names its US facility.

    Raises when no card is found or a card's price is not "$x.xx/GPU/hr".
    """
    cards = []
    for m in _CARD.finditer(html):
        price = _html.unescape(m["price"]).strip()
        if _PRICE.match(price) is None:
            raise ValueError(f"hotaisle: card price is not '$x.xx/GPU/hr': {price!r}")
        cards.append({
            "kind": _html.unescape(m["kind"]).strip(),
            "plan": _html.unescape(m["plan"]).strip(),
            "counts": _html.unescape(m["counts"]).strip(),
            "gpu": _html.unescape(m["gpu"]).strip(),
            "price": price,
        })
    if not cards:
        raise ValueError("hotaisle: no plan cards found - page reshaped")
    return {"cards": cards, "michigan_facility": "Michigan facility" in html}


def resolve_team(teams, configured: str | None) -> str:
    """The team handle to put in the API path, from GET /teams/ and HOTAISLE_TEAM.

    Exact handle first, then handle or display name case-insensitively; failing that, the key's only team. Raises
    when nothing fits (no team, or several and none named), so the stock call is recorded as failed. Error texts
    never include a handle or name.
    """
    if not isinstance(teams, list):
        raise ValueError("hotaisle: GET /teams/ did not return a list - response reshaped")
    teams = [t for t in teams if isinstance(t, dict) and isinstance(t.get("handle"), str) and t["handle"]
             and not t.get("invitation")]
    if not teams:
        raise ValueError("hotaisle: the API key has no (accepted) team")
    want = (configured or "").strip()
    if want:
        for t in teams:
            if t["handle"] == want:
                return t["handle"]
        for t in teams:
            if want.lower() in (t["handle"].lower(), str(t.get("name") or "").strip().lower()):
                return t["handle"]
    if len(teams) == 1:
        log.warning("hotaisle: HOTAISLE_TEAM %s; using the key's only team (set HOTAISLE_TEAM to its handle)",
                    "is unset" if not want else "matches no team handle or name")
        return teams[0]["handle"]
    raise ValueError(f"hotaisle: HOTAISLE_TEAM matches none of the key's {len(teams)} teams; set it to a team handle")


def vm_rows(payload) -> list[dict]:
    """The readable VM types of a virtual_machines/available body.

    One row per type with exactly one GPU model: `model` (upper-case, "MI300X"), `count`, `quantity`, `cents`
    (OnDemandPrice, None when absent) and the specs. Raises when the body is not a non-empty list or no type is
    readable, so a reshaped response fails the poll instead of reading as "nothing in stock". An empty list is
    valid: the endpoint lists only what can be provisioned now, so [] means every size is taken. A type with no GPU,
    several GPU models, or an unreadable Quantity is skipped with a warning.
    """
    if payload == []:
        return []  # sold out: every page size reads out of stock
    if not isinstance(payload, list):
        raise ValueError("hotaisle: virtual_machines/available is not a list - response reshaped")
    rows = []
    for t in payload:
        if not isinstance(t, dict) or not isinstance(t.get("Specs"), dict):
            raise ValueError(f"hotaisle: a VM type lost its Specs: {t!r}"[:300])
        specs = t["Specs"]
        gpus = [g for g in (specs.get("gpus") or []) if isinstance(g, dict)]
        models = {str(g.get("model") or "").strip().upper() for g in gpus}
        count = sum(g.get("count") or 0 for g in gpus if isinstance(g.get("count"), int))
        quantity = t.get("Quantity")
        if len(models) != 1 or "" in models or count <= 0 or not isinstance(quantity, int) or quantity < 0:
            log.warning("hotaisle: unreadable VM type (gpus=%r, Quantity=%r); skipped", gpus, quantity)
            continue
        cents = t.get("OnDemandPrice")
        rows.append({
            "model": models.pop(), "count": count, "quantity": quantity,
            "cents": cents if isinstance(cents, int) else None,
            "manufacturer": str(gpus[0].get("manufacturer") or "").strip(),
            "vcpu": specs.get("cpu_cores"), "ram_bytes": specs.get("ram_capacity"),
            "disk_bytes": specs.get("disk_capacity"),
        })
    if not rows:
        raise ValueError("hotaisle: no readable VM type in virtual_machines/available - response reshaped")
    return rows


class HotAisleProvider(Provider):
    name = "hotaisle"
    # The price page every poll, plus three read-only GETs with a key.
    polling = PollingPolicy(interval_seconds=900, timeout_seconds=45)

    def __init__(self, client: httpx.AsyncClient | None = None, api_key: str | None = None,
                 team: str | None = None, api_client: httpx.AsyncClient | None = None):
        super().__init__(
            client
            or httpx.AsyncClient(
                base_url=BASE_URL,
                headers={"user-agent": "opengrid-terminal/0.1"},
                timeout=45,
                follow_redirects=True,
            )
        )
        self._team = team
        self._handle: str | None = None  # resolved once per process, never stored
        self._api = None
        if api_key:
            self._api = api_client or httpx.AsyncClient(
                base_url=API_BASE_URL,
                headers={"Authorization": f"Token {api_key}", "accept": "application/json",
                         "user-agent": "opengrid-terminal/0.1"},
                timeout=45,
            )

    async def aclose(self) -> None:
        await super().aclose()
        if self._api is not None:
            await self._api.aclose()

    async def _api_get(self, path: str, stored_as: str) -> RawResponse:
        page_client, self._client = self._client, self._api
        try:
            raw = await self.get(path)
        finally:
            self._client = page_client
        update = {"endpoint": stored_as}
        if raw.error and self._handle:
            update["error"] = raw.error.replace(self._handle, "{team}")
        return raw.model_copy(update=update)

    def _failed(self, endpoint: str, error: str) -> RawResponse:
        return RawResponse(provider=self.name, endpoint=endpoint, fetched_at=datetime.now(timezone.utc),
                           ok=False, error=error[:500])

    async def _stock(self) -> list[RawResponse]:
        """The API's availability calls, stored raw; never raises, so prices are never at stake."""
        if self._handle is None:
            teams = await self._api_get(TEAMS_ENDPOINT, TEAMS_ENDPOINT)
            if not teams.ok:
                return [self._failed(VM_ENDPOINT, f"team lookup failed: HTTP {teams.status_code}: {teams.error}")]
            try:
                self._handle = resolve_team(teams.payload, self._team)
            except ValueError as exc:
                return [self._failed(VM_ENDPOINT, str(exc))]
        vm = await self._api_get(VM_ENDPOINT.format(team=self._handle), VM_ENDPOINT)
        if vm.ok:
            try:
                vm_rows(vm.payload)
            except ValueError as exc:
                # Stored as received, but failed: zero readable types is not "nothing in stock".
                vm = vm.model_copy(update={"ok": False, "error": str(exc)[:500]})
        bm = await self._api_get(BAREMETAL_ENDPOINT.format(team=self._handle), BAREMETAL_ENDPOINT)
        # Bare metal null/[] is a normal answer (nothing reservable now, as on 2026-11-01); it feeds no listing.
        if bm.ok and bm.payload is not None and not isinstance(bm.payload, list):
            bm = bm.model_copy(update={"ok": False, "error": "hotaisle: bare_metal/available is not a list"})
        if vm.status_code in (401, 403, 404):
            self._handle = None  # team renamed or key re-scoped: look it up again next poll
        return [vm, bm]

    async def fetch(self) -> list[RawResponse]:
        responses = [await get_page(self, ENDPOINT, extract)]
        if self._api is not None:
            responses.extend(await self._stock())
        return responses

    @staticmethod
    def normalize(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        """One VM card becomes one listing per GPU count it offers. The API is only cross-checked, never published."""
        rows = find_endpoint(by_endpoint, ENDPOINT)
        if not rows:
            return []
        listings = _page_listings(rows[0])
        api = find_endpoint(by_endpoint, VM_ENDPOINT)
        if api:
            try:
                check_prices(listings, vm_rows(api[0].payload))
            except ValueError:
                pass  # an unreadable API body never touches prices
        return listings

    @staticmethod
    def normalize_capacity(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[CapacityObservation]:
        """`Quantity` per page VM listing (count shape, instances), keyed by the page's sku."""
        api = find_endpoint(by_endpoint, VM_ENDPOINT)
        page = find_endpoint(by_endpoint, ENDPOINT)
        if not api or not page:
            return []  # no key, or nothing to join stock to
        snapshot = api[0]
        types = {(r["model"], r["count"]): r for r in vm_rows(snapshot.payload)}
        listings = _page_listings(page[0])
        out, seen = [], set()
        for l in listings:
            model = l.sku.split(":", 1)[0].upper()
            key = (model, l.gpu_count)
            seen.add(key)
            r = types.get(key)
            if r is None:
                out.append(_obs(l.sku, l.raw_gpu_name, l.canonical_gpu_name, l.gpu_count, l.country, None, False,
                                f"not listed in virtual_machines/available ({model} x{l.gpu_count})", snapshot))
            else:
                out.append(_obs(l.sku, l.raw_gpu_name, l.canonical_gpu_name, l.gpu_count, l.country, r["quantity"],
                                r["quantity"] > 0, _raw_value(r), snapshot))
        # A size the API offers but the page does not price: stock with no listing to join, kept visible.
        page_gpu = {l.sku.split(":", 1)[0].upper(): l.sku.split(":", 1)[0] for l in listings}
        for key, r in types.items():
            if key in seen:
                continue
            name = f"AMD {r['model']}" if r["manufacturer"].upper() in ("AMD", "") else f"{r['manufacturer']} {r['model']}"
            sku = f"{page_gpu.get(r['model'], r['model'])}:{r['count']}x:vm"
            out.append(_obs(sku, name, canonical.canonical_gpu_name(name), r["count"], listings[0].country
                            if listings else None, r["quantity"], r["quantity"] > 0, _raw_value(r), snapshot))
        return out


def check_prices(listings: list[ComputeListing], rows: list[dict]) -> list[str]:
    """Each API VM type's OnDemandPrice against the page price for the same size; differences logged, returned."""
    by_key = {(l.sku.split(":", 1)[0].upper(), l.gpu_count): l for l in listings}
    diffs = []
    for r in rows:
        l = by_key.get((r["model"], r["count"]))
        if l is None or r["cents"] is None:
            continue
        api_price = Decimal(r["cents"]) / 100
        if api_price != l.price_per_instance_hour:
            diffs.append(f"{l.sku}: API {api_price}/h, page {l.price_per_instance_hour}/h")
    if diffs:
        log.warning("hotaisle: API OnDemandPrice differs from the public page (page prices kept): %s", "; ".join(diffs))
    return diffs


def _raw_value(r: dict) -> str:
    return f"Quantity={r['quantity']}; {r['model']} x{r['count']}"


def _obs(sku, raw_name, canon, count, country, quantity, in_stock, raw_value, snapshot) -> CapacityObservation:
    return CapacityObservation(
        provider="hotaisle", sku=sku, region=None, country=country, raw_gpu_name=raw_name,
        canonical_gpu_name=canon, gpu_count=count, in_stock=in_stock, available_count=quantity,
        count_unit="instance" if quantity is not None else None, raw_value=raw_value, source=VM_ENDPOINT,
        observed_at=snapshot.fetched_at)


def _page_listings(snapshot) -> list[ComputeListing]:
    """One VM card becomes one listing per GPU count it offers."""
    payload = snapshot.payload
    country = "US" if payload.get("michigan_facility") else None

    listings = []
    for c in payload["cards"]:
        if c["kind"].upper() != "VM":
            continue  # bare metal: on-demand status unclear (see module docstring)
        per_gpu = Decimal(_PRICE.match(c["price"]).group(1))
        # "MI300x" -> "AMD MI300X": the page sells only AMD Instinct parts.
        raw_name = f"AMD {c['gpu'].upper()}"
        for n in sorted({int(x) for x in _COUNTS.findall(c["counts"])}):
            if n <= 0:
                continue
            listings.append(
                ComputeListing(
                    provider="hotaisle",
                    sku=f"{c['gpu']}:{n}x:vm",
                    listing_id=f"{c['gpu'].lower()}:{n}x:vm",
                    raw_gpu_name=raw_name,
                    canonical_gpu_name=canonical.canonical_gpu_name(raw_name),
                    gpu_count=n,
                    region=None,
                    country=country,
                    price_per_gpu_hour=per_gpu,
                    # The card prices one GPU; the instance is n of them.
                    price_per_instance_hour=per_gpu * n,
                    currency="USD",
                    market_type="on_demand",
                    provider_tier="vm",
                    interruptible=False,
                    available=None,  # stock lives in capacity (normalize_capacity), not on the price row
                    capacity=None,
                    capacity_unit=None,
                    vcpu=None,
                    ram_gb=None,
                    storage_gb=None,
                    observed_at=snapshot.fetched_at,
                )
            )
    return listings
