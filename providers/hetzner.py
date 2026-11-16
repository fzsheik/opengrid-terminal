"""Hetzner: dedicated GPU servers (GEX line) from its public server matrix, no key; EUR converted at the ECB rate.

hetzner.com/dedicated-rootserver/matrix-gpu/ is server-rendered. One card per server model ("GEX45", "GEX63",
"GEX131", "GEX430") prints, each in a visually-hidden span:

    "€ 214.00 per month, excl. VAT"   monthly ("from monthly" when the model has upgrade options)
    "€ 0.3429 per hour, excl. VAT"    "or hourly"
    "€ 209.00 one-time setup fee"     "1x setup"

then its specs (cores and CPU, RAM, GPU: "4x NVIDIA RTX PRO™ 6000 Blackwell Max-Q ▪︎ 4x 96 GB GDDR7 vRam"), and a
details panel (joined by its details id) listing the locations ("HEL1", "FSN1", with a country flag).

Price read: the HOURLY rate Hetzner prints for hourly billing, the whole server, EUR excl. VAT, converted at the ECB
EUR->USD reference rate in force at fetch (fail closed without a fresh rate). Hetzner's hourly price is a real rate
(billing hourly, capped at the monthly price), so the 730-hour monthly convention is not applied: monthly / 730 would
be the cheaper full-month rate (GEX45: €214.00 / 730 = €0.2932 vs €0.3429 hourly). The monthly figure and the
one-time setup fee (as large as one month) are stored raw but not added to the hourly price.

A "from monthly" model (GEX131: RAM and disk upgrades) is priced at its base configuration, the one the card's specs
describe ("from 256 GB" RAM); the upgrades are not modelled. A card whose GPU line, hourly price or name cannot be read
is skipped (logged); a page with no readable card fails the normalize.

GPU names: "NVIDIA RTX PRO 6000 Blackwell Max-Q 96GB" (mapped); "NVIDIA RTX PRO 4000 Blackwell SFF Edition 24GB"
stays unmapped (no canonical SFF Blackwell card; the SFF Ada is a separate canonical model, never merged).

Region: the card's locations ("HEL1", "HEL1; FSN1"), country from the flag when all agree (FI, DE). The matrix itself
prints no stock.

Capacity (yes/no per location). Each card's price element carries the server's Robot product id
(product-key="ROBOT_1768+ROBOT_1266": the server, then a part every card shares). Hetzner's own order configurator
decides which locations can be ordered from one keyless file,

    GET /_resources/app/data/app/live_data_upfront.json     {"1747": ["FSN1", "HEL1"], "1768": ["HEL1"], ...}

a map of product number -> the datacenters where that product can be ordered now. The configurator script enables a
location only when the file lists it for the product, and labels the order button "Currently not available" when
none is enabled. That behaviour is the only definition there is: Hetzner documents neither the file nor its
fields, and its order-processing page says it "does not provide information about the current availability of
servers". Cross-checked 2026-11-15: GEX44 (product 1608) was missing from the file and Robot's own order page said
"The product GEX44 is currently not available."

One row per (server model, location the card lists): in stock exactly when the file lists that datacenter for the
card's product. A product the file does not name at all gets in_stock unknown, not false: the configurator skips the
check for products flagged CheckAvailability false (a flag kept in a per-product file this module does not read),
so absence alone cannot be told from "not checked". A "from" model (GEX131) is the base configuration the card
prices; its larger variants have other product numbers and are not read. A boolean: the file has no counts and no
delivery time. A card with no product id (a snapshot stored before it was kept) gets no row.

Terms (checked 2026-11-01): robots.txt allows everything except internal search; Hetzner's terms and conditions, legal
notice and system policies have no clause on automated reading of its public site. One request an hour.

Hetzner sells dedicated servers (and a general-purpose cloud), so it is classed general_cloud.
"""

import html as _html
import logging
import re
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx

import canonical
import fx
from models import CapacityObservation, ComputeListing, RawResponse
from providers.base import PollingPolicy, Provider, find_endpoint, get_page

if TYPE_CHECKING:
    from tables import RawSnapshot

log = logging.getLogger(__name__)

BASE_URL = "https://www.hetzner.com"
ENDPOINT = "/dedicated-rootserver/matrix-gpu/"
LIVE = "/_resources/app/data/app/live_data_upfront.json"

_CARD = '<div class="baremetal-product">'
_NAME = re.compile(r'<span class="text-display-8[^"]*">(?P<v>.*?)</span>\s*<span', re.S)
_HIDDEN = re.compile(r'<span class="visually-hidden">(?P<v>[^<]+)</span>')
_LABEL = re.compile(r'<div class="baremetal-month">.*?</ho-localization-provider>\s*<span[^>]*>(?P<v>[^<]*)</span>', re.S)
_SPEC = re.compile(r'<div class="server-card-element-content-specs-item">.*?<p class="text-copy-3">(?P<v>.*?)</p>', re.S)
_DETAILS_ID = re.compile(r'id="details-(?P<id>\d+)"')
_PRODUCT_KEY = re.compile(r'product-key="(?P<v>ROBOT_\d+[^"]*)"')
_LOCATION = re.compile(r'<span data-location="(?P<cc>[a-z]{2})">.*?<span class="text-copy-3">\s*(?P<dc>[A-Z0-9]+)\s*</span>', re.S)
_EUR = r"€\s*(?P<eur>\d+(?:,\d{3})*(?:\.\d+)?)"
_MONTH = re.compile(rf"^{_EUR} per month, excl\. VAT$")
_HOUR = re.compile(rf"^{_EUR} per hour, excl\. VAT$")
_SETUP = re.compile(rf"^{_EUR} one-time setup fee, excl\. VAT$")
_GPU = re.compile(r"^(?:(?P<n>\d+)x\s+)?(?P<model>NVIDIA .+?)\s*▪︎?\s*(?:\d+x\s+)?(?P<vram>\d+)\s*GB\b")
_RAM = re.compile(r"(?P<gb>\d+)\s*GB\b.*RAM")
_CORES = re.compile(r"^(?P<n>\d+)\s*Cores\b")


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def extract(page: str) -> dict:
    """Every GEX card as printed (name, price spans, monthly label, spec lines, locations).

    Raises when no card is found or a card lost its name or price spans, so a redesigned page fails the poll.
    """
    starts = [m.start() for m in re.finditer(re.escape(_CARD), page)]
    if not starts:
        raise ValueError("hetzner: no server card on the GPU matrix - page reshaped")
    stop = page.find("baremetal-details-container", starts[-1])
    bounds = starts[1:] + [stop if stop > 0 else len(page)]
    locations: dict[str, list[list[str]]] = {}
    for m in re.finditer(r'id="baremetal-container-details-(?P<id>\d+)"', page):
        end = page.find("baremetal-details-power", m.end())
        chunk = page[m.end(): end if end > 0 else m.end() + 20000]
        locations[m["id"]] = [[l["dc"], l["cc"].upper()] for l in _LOCATION.finditer(chunk)]
    cards = []
    for s, e in zip(starts, bounds):
        card = page[s:e]
        name, details = _NAME.search(card), _DETAILS_ID.search(card)
        prices = [_html.unescape(h["v"]).strip() for h in _HIDDEN.finditer(card)]
        label = _LABEL.search(card)
        if name is None or len(prices) < 2:
            raise ValueError("hetzner: a server card lost its name or prices - page reshaped")
        cards.append({
            "name": _text(name["v"]),
            "prices": prices,
            "monthly_label": _text(label["v"]) if label else None,
            "specs": [_text(sp["v"]) for sp in _SPEC.finditer(card)],
            "locations": locations.get(details["id"], []) if details else [],
            # The Robot product id, for the stock file; None on a layout without it (capacity then has no row).
            "product_key": (lambda m: m["v"] if m else None)(_PRODUCT_KEY.search(card)),
        })
    return {"cards": cards}


def _eur(s: str) -> Decimal:
    return Decimal(s.replace(",", ""))


def _price(prices: list[str], pattern: re.Pattern) -> Decimal | None:
    for p in prices:
        m = pattern.match(p)
        if m:
            return _eur(m["eur"])
    return None


def read_card(c: dict) -> dict | None:
    """One card's offer, or None (logged) when its model, GPU line or hourly price cannot be read."""
    model = re.search(r"\bGEX\d+\b", c.get("name") or "")
    hourly = _price(c.get("prices") or [], _HOUR)
    gpu = next((m for m in (_GPU.match(s.replace("™", "")) for s in c.get("specs") or []) if m), None)
    if model is None or hourly is None or gpu is None:
        log.warning("hetzner: card %r unreadable (hourly %s, GPU line %s); skipped", c.get("name"), hourly, bool(gpu))
        return None
    specs = c["specs"]
    ram = next((m for m in (_RAM.search(s) for s in specs) if m), None)
    cores = next((m for m in (_CORES.match(s) for s in specs) if m), None)
    return {
        "model": model.group(0),
        "gpu_count": int(gpu["n"] or 1),
        "gpu_name": f"{re.sub(r'\s+', ' ', gpu['model']).strip()} {gpu['vram']}GB",
        "eur_per_hour": hourly,
        "eur_per_month": _price(c["prices"], _MONTH),
        "eur_setup": _price(c["prices"], _SETUP),
        "from_price": (c.get("monthly_label") or "").lower().startswith("from"),
        "ram_gb": int(ram["gb"]) if ram else None,
        "cores": int(cores["n"]) if cores else None,
        "locations": c.get("locations") or [],
        "product": (lambda m: m.group(1) if m else None)(re.match(r"ROBOT_(\d+)", c.get("product_key") or "")),
    }


class HetznerProvider(Provider):
    name = "hetzner"
    # One page load and one 1 KB stock file; list prices change rarely.
    polling = PollingPolicy(interval_seconds=3600, timeout_seconds=45)

    def __init__(self, client: httpx.AsyncClient | None = None):
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
        # Prices first; the stock file is its own response, so its failure is stored alone.
        return [await get_page(self, ENDPOINT, extract), await self.get(LIVE)]

    @classmethod
    def normalize(cls, by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        """One GEX server model becomes one USD listing at its hourly rate."""
        rows = find_endpoint(by_endpoint, ENDPOINT)
        if not rows:
            return []
        snapshot = rows[0]
        body = snapshot.payload
        if not isinstance(body, dict) or not isinstance(body.get("cards"), list):
            raise ValueError("hetzner: stored page has no cards list - reshaped")
        offers = [o for o in (read_card(c) for c in body["cards"]) if o]
        if not offers:
            raise ValueError("hetzner: no readable GPU server card - reshaped")
        rate = fx.usd_rate("EUR", snapshot.fetched_at,
                           context=f"hetzner page fetched {snapshot.fetched_at:%Y-%m-%d %H:%M}Z")
        if rate is None:
            return []  # fail closed: nothing is converted without a fresh ECB rate (fx.usd_rate logged why)
        listings = []
        for o in offers:
            dcs = [dc for dc, _ in o["locations"]]
            countries = {cc for _, cc in o["locations"]}
            listings.append(ComputeListing(
                provider=cls.name,
                sku=o["model"],
                listing_id=o["model"].lower(),
                raw_gpu_name=o["gpu_name"],
                canonical_gpu_name=canonical.canonical_gpu_name(o["gpu_name"]),
                gpu_count=o["gpu_count"],
                region="; ".join(dcs) or None,
                country=countries.pop() if len(countries) == 1 else None,
                **fx.convert(o["eur_per_hour"], o["gpu_count"], "EUR", rate),
                market_type="on_demand",
                provider_tier="dedicated",
                interruptible=False,
                available=None,  # the matrix says nothing about stock
                capacity=None,
                capacity_unit=None,
                vcpu=None,  # physical cores, not vCPUs (kept raw in the card specs)
                ram_gb=float(o["ram_gb"]) if o["ram_gb"] is not None else None,
                storage_gb=None,
                observed_at=snapshot.fetched_at,
            ))
        return listings

    @staticmethod
    def normalize_capacity(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[CapacityObservation]:
        """One (GEX model, listed location) becomes one yes/no row from Hetzner's order-availability file."""
        pages, live = find_endpoint(by_endpoint, ENDPOINT), find_endpoint(by_endpoint, LIVE)
        if not pages or not live:
            return []
        body, stock = pages[0].payload, live[0].payload
        if not isinstance(body, dict) or not isinstance(body.get("cards"), list):
            raise ValueError("hetzner: stored page has no cards list - reshaped")
        if not isinstance(stock, dict) or not stock or not all(
                isinstance(v, list) and all(isinstance(dc, str) for dc in v) for v in stock.values()):
            raise ValueError("hetzner: availability file is not a map of product -> datacenters - reshaped")
        # A key can name several products; the site matches by substring, whole numbers are matched here so
        # product 174 never reads 1747's stock.
        where: dict[str, set[str]] = {}
        for key, dcs in stock.items():
            for number in re.findall(r"\d+", str(key)):
                where.setdefault(number, set()).update(dcs)
        out = []
        for o in (o for o in (read_card(c) for c in body["cards"]) if o):
            if not o["product"]:
                continue  # no product id on the card: nothing to join the file on
            dcs = where.get(o["product"])
            for dc, cc in o["locations"] or [[None, None]]:
                if dcs is None:
                    in_stock, raw = None, f"product {o['product']} not in the availability file"
                else:
                    in_stock = bool(dcs) if dc is None else dc in dcs
                    raw = f"product {o['product']} orderable in {', '.join(sorted(dcs)) or 'no location'}"
                out.append(CapacityObservation(
                    provider="hetzner", sku=o["model"], region=dc, country=cc, raw_gpu_name=o["gpu_name"],
                    canonical_gpu_name=canonical.canonical_gpu_name(o["gpu_name"]), gpu_count=o["gpu_count"],
                    in_stock=in_stock, raw_value=raw, source=LIVE, observed_at=live[0].fetched_at))
        return out
