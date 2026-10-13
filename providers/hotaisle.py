"""Hot Aisle: the public price page, no API key.

hotaisle.xyz/pricing prints one card per plan: the deployment kind ("VM" or
"Bare metal"), the GPU counts it comes in ("1x", "2x & 4x", "8x"), the GPU
("MI300x") and a price "$x.xx/GPU/hr". A card's price is per GPU, so a 4x VM is
4 times the card price.

Only the VM cards are read. The page describes the 8x bare-metal card as
"billed by the minute" while its own meta description calls it "8x MI300X
bare-metal monthly terms": that contradiction makes it unclear whether the
bare-metal price is on-demand, so it is skipped rather than guessed.

All plans are in one Michigan, US facility (the page says so); the country is
set only when the page still says it.
"""

import html as _html
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

BASE_URL = "https://hotaisle.xyz"
ENDPOINT = "/pricing"

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


class HotAisleProvider(Provider):
    name = "hotaisle"
    # One page load per poll.
    polling = PollingPolicy(interval_seconds=900, timeout_seconds=45)

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
        return [await get_page(self, ENDPOINT, extract)]

    @staticmethod
    def normalize(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        """One VM card becomes one listing per GPU count it offers."""
        rows = find_endpoint(by_endpoint, ENDPOINT)
        if not rows:
            return []
        snapshot = rows[0]
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
                        available=None,  # the page says nothing about stock
                        capacity=None,
                        capacity_unit=None,
                        vcpu=None,
                        ram_gb=None,
                        storage_gb=None,
                        observed_at=snapshot.fetched_at,
                    )
                )
        return listings
