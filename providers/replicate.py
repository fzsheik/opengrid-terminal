"""Replicate: the public price page, no API key.

The "Hardware pricing" section of replicate.com/pricing lists one card per
hardware type: a name ("2x Nvidia L40S GPU"), the hardware id ("gpu-l40s-2x"),
the price per second and per hour, and the GPU count, CPU count, total GPU RAM
and RAM. The page is read as text (tags dropped), from the narrow-screen card
list, which prints every field in the same order on every card.

The price is for the WHOLE hardware type (its CPUs and RAM included), billed per
second; per second x 3600 gives the hourly rate exactly. The per-GPU rate is
that / GPU count.

Read: the cards above the "Additional hardware" divider. Every card below it
(4x/8x A100 and H100, every H200, 4x/8x L40S) says its capacity "is available
with committed spend contracts", so it is not pay-as-you-go: no listing. CPU
cards have no GPU and give no listing.

The names carry no variant: "Nvidia H100" and "Nvidia A100 (80GB)" stay
unmapped (SXM or PCIe not stated). Replicate runs on other clouds' capacity, so
it is classed general_cloud like Koyeb.
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

BASE_URL = "https://replicate.com"
ENDPOINT = "/pricing"

_SECTION = re.compile(r'<section id="hardware".*?<div class="block md:hidden[^"]*">(?P<body>.*?)<div class="hidden md:block', re.S)
_DROP = re.compile(r"<!--.*?-->|<svg.*?</svg>", re.S)
_TAG = re.compile(r"<[^>]+>")
_ADDITIONAL = "Additional hardware"
_PER_SEC = re.compile(r"^\$(\d+(?:\.\d+)?)/sec$")
_PER_HR = re.compile(r"^\$(\d+(?:\.\d+)?)/hr$")
_SPECS = ("GPU", "CPU", "GPU RAM", "RAM")
_COUNT = re.compile(r"^(\d+)x$")
_GB = re.compile(r"^(\d+(?:\.\d+)?)GB$")
_MULTI = re.compile(r"^(\d+)x\s+")


def _tokens(fragment: str) -> list[str]:
    text = _TAG.sub("\n", _DROP.sub("", fragment))
    return [t for t in (_html.unescape(x).strip() for x in text.split("\n")) if t]


def extract(html: str) -> dict:
    """Every hardware card as printed, split into pay-as-you-go and committed-spend ones.

    Raises when the section, the divider or a card's price layout is missing.
    """
    sec = _SECTION.search(html)
    if sec is None:
        raise ValueError("replicate: 'Hardware pricing' card list not found - page reshaped")
    toks = _tokens(sec["body"])
    if _ADDITIONAL not in toks:
        raise ValueError(f"replicate: '{_ADDITIONAL}' divider not found - page reshaped")
    cut = toks.index(_ADDITIONAL)
    cards, committed = [], []
    # A card is: name, hardware id, "$x/sec", "$y/hr", then GPU/CPU/GPU RAM/RAM label-value pairs.
    i = 0
    while i < cut:
        name, hw, sec_price, hr_price = toks[i:i + 4]
        if not _PER_SEC.match(sec_price) or not _PER_HR.match(hr_price):
            raise ValueError(f"replicate: card {name!r} prices are not '$x/sec' '$y/hr': {toks[i:i + 4]!r}")
        specs = toks[i + 4:i + 12]
        if specs[0::2] != list(_SPECS):
            raise ValueError(f"replicate: card {name!r} spec labels changed: {specs!r}")
        cards.append({"name": name, "hardware": hw, "per_second": sec_price, "per_hour": hr_price,
                      **{k.lower().replace(" ", "_"): v for k, v in zip(specs[0::2], specs[1::2])}})
        i += 12
    # Below the divider: name, id, prices, then a "committed spend contracts" note.
    for j in range(cut + 1, len(toks)):
        if _PER_SEC.match(toks[j]) and j >= 2:
            note = toks[j + 2] if j + 2 < len(toks) else ""
            committed.append({"name": toks[j - 2], "hardware": toks[j - 1], "note": note})
    if not cards:
        raise ValueError("replicate: no hardware cards - page reshaped")
    return {"cards": cards, "committed_spend": committed}


class ReplicateProvider(Provider):
    name = "replicate"
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
        """One pay-as-you-go GPU card becomes one listing; committed-spend cards give none."""
        rows = find_endpoint(by_endpoint, ENDPOINT)
        if not rows:
            return []
        snapshot = rows[0]

        listings = []
        for c in snapshot.payload["cards"]:
            m = _COUNT.match(c["gpu"])
            if m is None:
                continue  # "-": a CPU-only card
            count = int(m.group(1))
            # The name repeats the count ("2x Nvidia L40S GPU"); a disagreement is skipped, never guessed.
            named = _MULTI.match(c["name"])
            if count < 1 or (int(named.group(1)) if named else 1) != count:
                log.warning("replicate: %s says %r but GPU %r; skipped", c["hardware"], c["name"], c["gpu"])
                continue
            instance = Decimal(_PER_SEC.match(c["per_second"]).group(1)) * 3600
            if instance <= 0:
                continue
            # "2x Nvidia A100 (80GB) GPU" -> "Nvidia A100 (80GB)".
            raw_name = re.sub(r"\s+GPU$", "", _MULTI.sub("", c["name"])).strip()
            cpu, ram = _COUNT.match(c["cpu"]), _GB.match(c["ram"])
            listings.append(
                ComputeListing(
                    provider="replicate",
                    sku=c["hardware"],
                    listing_id=c["hardware"],
                    raw_gpu_name=raw_name,
                    canonical_gpu_name=canonical.canonical_gpu_name(raw_name),
                    gpu_count=count,
                    region=None,
                    country=None,
                    price_per_gpu_hour=instance / count,
                    # The whole hardware type, CPUs and RAM included.
                    price_per_instance_hour=instance,
                    currency="USD",
                    market_type="on_demand",
                    provider_tier="serverless",
                    interruptible=False,
                    available=None,  # the page says nothing about stock
                    capacity=None,
                    capacity_unit=None,
                    vcpu=int(cpu.group(1)) if cpu else None,
                    ram_gb=float(ram.group(1)) if ram else None,
                    storage_gb=None,
                    observed_at=snapshot.fetched_at,
                )
            )
        return listings
