"""Bitdeer AI Cloud: on-demand GPU prices from the JSON feed behind its pricing page. INTERNAL ONLY.

Bitdeer is a Bitcoin miner that rents its own GPUs (docs/MINER_PROVIDERS.md). Its pricing page
(www.bitdeer.ai/en/pricing/gpu-compute) is drawn from one keyless feed:

    GET https://www.bitdeer.ai/api/gpu-pricing
    {"code": 2000, "data": [
      {"model": "NVIDIA H100", "spec": "80GB VRAM", "type": "gpu", "machineType": "vm", "price": 2.9,
       "details": [{"cards": 1, "price": 2.9, "year1": {"price": "2.46500", "save": "3810.60"},
                    "year3": {"price": "2.46500", "save": "11431.80"}}, {"cards": 2, ...}]},
      {"model": "General Compute", "type": "cpu", ...}]}

What the numbers mean:
    details[].price          the on-demand price of the WHOLE instance of that card count, USD per hour
    details[].year1 / year3  the 1-year / 3-year committed price of the same instance, USD per hour, as strings.
                             Not linear in the card count (B300: 1 card 7.65, 2 cards 16.74), so every row is read.
    save                     dollars saved over the term; derivable, not stored
    machineType              "vm" (H100, H200, B200, B300: 1 to 8 cards) or "bm" (GB200, GB300 NVL72: 4 to 72 cards,
                             so a price per node or rack)

Listings: one on-demand listing per (model, card count). Committed prices are never listings (founders' rule): they
are read by reserved_prices() for reserved.py, which keeps them out of every public read (see below).
The feed has no region, stock or availability, so listings are region-less and `available` is None, like a price
page. "NVIDIA H100 80GB VRAM" stays unmapped (SXM or PCIe not stated), as for Oracle and Fireworks.

Only GPU rows are stored (the CPU "General Compute" row is dropped). A `code` other than 2000, a missing `data` list
or a reshaped row fails the poll and raises in normalize: a reshape never reads as "no prices".

INTERNAL ONLY (provider_meta.INTERNAL_SOURCES, every source). Terms of Service at https://www.bitdeer.ai/en/terms
(no effective date printed; read 2026-11-15):
    5.8   "The User shall neither engage in any activities in the name of Bitdeer nor use the information/data on
          the Website without prior written authorization from Bitdeer."
    5.10  the User shall not "(f) copy, reproduce, distribute, republish, download, display, post or transmit in any
          form or by any means" any part of the Services.
    4.1   the licence is "solely for Your internal business purposes".
The terms say nothing about crawling or automated access, and robots.txt allows "/" (only /cdn-cgi/l/email-protection
and /en/docs/vms/user-data/ are disallowed). So the feed is read once an hour and used for routing and
recommendations, and never shown on a public page, index, fixing, statistic or export. OpenGrid holds no Bitdeer
account, so it has not accepted these terms as a "User"; clause 5.8 is nevertheless the reason to ask Bitdeer for
written permission before anything of theirs is republished.
"""

import logging
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

import httpx

import canonical
from models import ComputeListing, RawResponse
from providers.base import PollingPolicy, Provider, find_endpoint, reshaped

if TYPE_CHECKING:
    from tables import RawSnapshot

log = logging.getLogger(__name__)

BASE_URL = "https://www.bitdeer.ai"
ENDPOINT = "/api/gpu-pricing"
OK_CODE = 2000
TIERS = {"vm": "vm", "bm": "baremetal"}
# The committed terms the feed prices: (field, months, label).
TERMS = (("year1", 12, "1 year"), ("year3", 36, "3 years"))


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _dec(v, what: str) -> Decimal:
    """A positive USD amount from a JSON number or a numeric string. Booleans and anything else raise."""
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise ValueError(f"bitdeer: {what} is not a number: {v!r}")
    try:
        d = Decimal(str(v).strip())
    except InvalidOperation:
        raise ValueError(f"bitdeer: {what} is not a number: {v!r}") from None
    if not d.is_finite() or d <= 0:
        raise ValueError(f"bitdeer: {what} is not a positive price: {v!r}")
    return d


def gpu_rows(body) -> list[dict]:
    """The GPU rows of the feed's body, verbatim minus `save`. Raises when the body is not the known envelope."""
    if not isinstance(body, dict) or body.get("code") != OK_CODE:
        raise ValueError(f"bitdeer: code is not {OK_CODE}: {body.get('code') if isinstance(body, dict) else body!r}"[:200])
    data = body.get("data")
    if not isinstance(data, list):
        raise ValueError("bitdeer: no data list - response reshaped")
    rows = []
    for r in data:
        if not isinstance(r, dict) or not isinstance(r.get("type"), str):
            raise ValueError(f"bitdeer: a row has no type: {r!r}"[:200])
        if r["type"] != "gpu":
            continue  # "cpu": General Compute
        details = r.get("details")
        if not isinstance(details, list) or not all(isinstance(d, dict) for d in details):
            raise ValueError(f"bitdeer: {r.get('model')!r} has no details list")
        rows.append({
            "model": r.get("model"), "spec": r.get("spec"), "type": "gpu", "machineType": r.get("machineType"),
            "price": r.get("price"),
            "details": [{"cards": d.get("cards"), "price": d.get("price"),
                         **{k: {"price": d[k].get("price")} if isinstance(d.get(k), dict) else d.get(k)
                            for k, _, _ in TERMS if k in d}}
                        for d in details],
        })
    if not rows:
        raise ValueError("bitdeer: no GPU rows - response reshaped")
    return rows


@dataclass(frozen=True)
class Offer:
    """One (model, card count) row of the feed."""

    model: str
    spec: str
    machine_type: str
    cards: int
    price_per_instance_hour: Decimal
    committed: tuple[tuple[str, int, str, Decimal], ...]  # (field, months, label, USD per instance-hour)

    @property
    def raw_gpu_name(self) -> str:
        return f"{self.model} {self.spec}".strip()

    @property
    def listing_id(self) -> str:
        return f"{_slug(self.model)}:{self.cards}x:{self.machine_type}"

    @property
    def sku(self) -> str:
        return f"{self.model}:{self.cards}x:{self.machine_type}"


def offers(payload) -> list[Offer]:
    """Every (model, card count) of a stored payload. Raises on any row that does not read as documented."""
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ValueError("bitdeer: stored payload has no GPU rows")
    out, seen = [], set()
    for r in rows:
        model, spec, mt = r.get("model"), r.get("spec"), r.get("machineType")
        if not isinstance(model, str) or not model.strip() or not isinstance(spec, str) or mt not in TIERS:
            raise ValueError(f"bitdeer: unreadable row (model={model!r}, spec={spec!r}, machineType={mt!r})")
        details = r.get("details")
        if not isinstance(details, list) or not details:
            raise ValueError(f"bitdeer: {model} has no card rows")
        for d in details:
            cards = d.get("cards") if isinstance(d, dict) else None
            if isinstance(cards, bool) or not isinstance(cards, int) or cards < 1:
                raise ValueError(f"bitdeer: {model} has a row without a card count: {d!r}"[:200])
            key = (model, mt, cards)
            if key in seen:
                raise ValueError(f"bitdeer: {model} lists {cards} cards twice")
            seen.add(key)
            committed = []
            for field, months, label in TERMS:
                term = d.get(field)
                if term is None:
                    continue  # a row may lack a term; a present one must read
                if not isinstance(term, dict):
                    raise ValueError(f"bitdeer: {model} x{cards} {field} is not an object")
                committed.append((field, months, label, _dec(term.get("price"), f"{model} x{cards} {field}")))
            out.append(Offer(model.strip(), spec.strip(), mt, cards, _dec(d.get("price"), f"{model} x{cards} price"),
                             tuple(committed)))
    return out


@dataclass(frozen=True)
class ReservedPrice:
    """One committed price, per GPU-hour, for reserved.py."""

    canonical_gpu_name: str | None
    raw_gpu_name: str
    gpu_count: int
    sku: str
    listing_id: str  # the on-demand listing of the same model and card count
    term: str
    months: int
    price_per_gpu_hour: Decimal
    on_demand_per_gpu_hour: Decimal


def reserved_prices(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ReservedPrice]:
    """The 1-year and 3-year committed prices of the newest stored feed. Raises on a reshaped payload."""
    rows = find_endpoint(by_endpoint, ENDPOINT)
    if not rows:
        return []
    out = []
    for o in offers(rows[0].payload):
        for _, months, label, price in o.committed:
            out.append(ReservedPrice(
                canonical.canonical_gpu_name(o.raw_gpu_name), o.raw_gpu_name, o.cards, o.sku, o.listing_id, label,
                months, price / o.cards, o.price_per_instance_hour / o.cards))
    return out


class BitdeerProvider(Provider):
    name = "bitdeer"
    # One keyless request per poll; a list price, so hourly is plenty (and a human request rate).
    polling = PollingPolicy(interval_seconds=3600, timeout_seconds=30, tier="slow")

    def __init__(self, client: httpx.AsyncClient | None = None):
        super().__init__(
            client
            or httpx.AsyncClient(
                base_url=BASE_URL,
                headers={"accept": "application/json", "user-agent": "opengrid-terminal/0.1"},
                timeout=30,
                follow_redirects=True,
            )
        )

    async def fetch(self) -> list[RawResponse]:
        raw = await self.get(ENDPOINT)
        if not raw.ok:
            return [raw]
        try:
            kept = {"code": OK_CODE, "data": gpu_rows(raw.payload)}
            offers(kept)  # a row that does not read fails the poll, never lists nothing
        except ValueError as exc:
            return [raw.model_copy(update={"ok": False, "payload": None, "error": str(exc)[:500]})]
        return [reshaped(raw, kept)]

    @staticmethod
    def normalize(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        """One on-demand listing per (model, card count). Committed prices are not listings."""
        rows = find_endpoint(by_endpoint, ENDPOINT)
        if not rows:
            return []
        snapshot = rows[0]
        listings = []
        for o in offers(snapshot.payload):
            listings.append(
                ComputeListing(
                    provider="bitdeer",
                    sku=o.sku,
                    listing_id=o.listing_id,
                    raw_gpu_name=o.raw_gpu_name,
                    canonical_gpu_name=canonical.canonical_gpu_name(o.raw_gpu_name),
                    gpu_count=o.cards,
                    region=None,
                    country=None,
                    # details[].price is the whole instance of that card count.
                    price_per_gpu_hour=o.price_per_instance_hour / o.cards,
                    price_per_instance_hour=o.price_per_instance_hour,
                    currency="USD",
                    market_type="on_demand",
                    provider_tier=TIERS[o.machine_type],
                    interruptible=False,
                    available=None,  # the feed says nothing about stock
                    capacity=None,
                    capacity_unit=None,
                    vcpu=None,
                    ram_gb=None,
                    storage_gb=None,
                    observed_at=snapshot.fetched_at,
                )
            )
        return listings
