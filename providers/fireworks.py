"""Fireworks AI: on-demand GPU deployments from its public pricing page, no key.

INTERNAL ONLY (terms allow reading, forbid republishing). Fireworks' Terms of Service (fireworks.ai/terms-of-service,
last updated 2026-07-10, read 2026-11-01) cover its websites as part of the "Service". Clause (b) allows automated
access only at a human request rate (one request an hour is well inside that); clause (e) forbids copying,
distributing or creating derivative works of "any part of the Service ... including ... by any automated or
non-automated scraping". So these prices may be read and used inside OpenGrid but must not be republished: register
"fireworks" in provider_meta.INTERNAL_SOURCES (data/visibility-tier) when merging. robots.txt allows every path.

fireworks.ai/pricing is server-rendered (Next.js). Its "On demand deployments" table (under "On-Demand Pricing:
pay per GPU second, with no extra charges for start-up times") lists one row per GPU: "H100 80 GB GPU", a price per
minute and a price per hour, in USD, per GPU. Read: every row, at the hourly price; the per-minute figure x 60 must
agree within 1% (Fireworks rounds the per-minute figure: H100 $0.134 x 60 = $8.04 vs $8.00) or the row is skipped.

Since 2026-11 the page prints one shared rate card instead: "GPU rates (USD)", header "GPU Per minute Per hour",
rows "H100 80 GB $0.134 $8.00" ("On Demand bills per GPU-second ... from the same rates"). extract() reads it into the
same rows, the name with " GPU" appended as before ("H100 80 GB GPU"), so listing ids are unchanged.

A dedicated inference deployment billed per GPU, host included: one listing per GPU model, gpu_count 1, tier
"serverless". Region-restricted deployments cost 1.5x (not modelled: the list price is the unrestricted one).
Fireworks runs on other clouds' capacity and publishes no region or stock: region, available and capacity stay None.

GPU names are kept as printed. "H100 80 GB GPU" stays unmapped (SXM or PCIe not stated); a bare H200, B200, B300 or
GB300 exists only as the SXM / superchip part (canonical.py).
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

BASE_URL = "https://fireworks.ai"
ENDPOINT = "/pricing"

_SECTION = re.compile(r">On demand deployments<(?P<body>.*?)(?:SOC 2 Type 2|</section>)", re.S)
_HEADER = "GPU Type Price ($) per minute Price ($) per hour"
_ROW = re.compile(r"(?P<name>[A-Z][A-Za-z0-9]* \d+ GB GPU) \$(?P<minute>\d+\.\d+) \$(?P<hour>\d+(?:,\d{3})*\.\d+)")


def _text(fragment: str) -> str:
    s = re.sub(r"<script.*?</script>|<style.*?</style>", " ", fragment, flags=re.S)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", _html.unescape(s)).strip()


def extract(page: str) -> dict:
    """The on-demand GPU table's rows, as printed. Raises when the table or its header is missing or has no row."""
    m = _SECTION.search(page)
    if m is None:
        return _rate_card(_text(page))
    text = _text(m["body"])
    if _HEADER not in text:
        raise ValueError("fireworks: on-demand table header not found - page reshaped")
    table = text[text.index(_HEADER) + len(_HEADER):]
    rows = [{"gpu": r["name"], "per_minute": r["minute"], "per_hour": r["hour"]} for r in _ROW.finditer(table)]
    if not rows:
        raise ValueError("fireworks: no GPU row in the on-demand table - page reshaped")
    return {"rows": rows}


_CARD_HEADER = "GPU rates (USD) GPU Per minute Per hour"
_CARD_ROW = re.compile(r"\s*(?P<name>[A-Z][A-Za-z0-9]* \d+ GB) \$(?P<minute>\d+\.\d+) \$(?P<hour>\d+(?:,\d{3})*\.\d+)")


def _rate_card(text: str) -> dict:
    """The "GPU rates (USD)" card (the page's layout since 2026-11): consecutive rows right after its header."""
    if _CARD_HEADER not in text:
        raise ValueError("fireworks: neither the 'On demand deployments' table nor the 'GPU rates' card found - "
                         "page reshaped")
    rest, rows = text[text.index(_CARD_HEADER) + len(_CARD_HEADER):], []
    while (r := _CARD_ROW.match(rest)) is not None:
        rows.append({"gpu": f"{r['name']} GPU", "per_minute": r["minute"], "per_hour": r["hour"]})
        rest = rest[r.end():]
    if not rows:
        raise ValueError("fireworks: no GPU row in the 'GPU rates' card - page reshaped")
    return {"rows": rows}


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


class FireworksProvider(Provider):
    name = "fireworks"
    # One page load; list prices change rarely (and the terms allow only human-rate automated access).
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
        return [await get_page(self, ENDPOINT, extract)]

    @classmethod
    def normalize(cls, by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        """One on-demand GPU row becomes one per-GPU listing."""
        rows = find_endpoint(by_endpoint, ENDPOINT)
        if not rows:
            return []
        snapshot = rows[0]
        body = snapshot.payload
        if not isinstance(body, dict) or not isinstance(body.get("rows"), list):
            raise ValueError("fireworks: stored page has no rows list - reshaped")
        listings = []
        for r in body["rows"]:
            hour = Decimal(str(r["per_hour"]).replace(",", ""))
            minute = Decimal(str(r["per_minute"]))
            if hour <= 0 or abs(minute * 60 - hour) > hour / 100:
                log.warning("fireworks: %s $%s/min x 60 disagrees with $%s/h; skipped", r["gpu"], minute, hour)
                continue
            name = r["gpu"]
            listings.append(ComputeListing(
                provider=cls.name,
                sku=f"on-demand:{name}",
                listing_id=_slug(name),
                raw_gpu_name=name,
                canonical_gpu_name=canonical.canonical_gpu_name(name),
                gpu_count=1,  # priced per GPU
                region=None,
                country=None,
                price_per_gpu_hour=hour,
                price_per_instance_hour=hour,
                currency="USD",
                market_type="on_demand",
                provider_tier="serverless",
                interruptible=False,
                available=None,
                capacity=None,
                capacity_unit=None,
                vcpu=None,
                ram_gb=None,
                storage_gb=None,
                observed_at=snapshot.fetched_at,
            ))
        if not listings:
            raise ValueError("fireworks: no readable on-demand row - reshaped")
        return listings
