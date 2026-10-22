"""Nosana: a decentralized GPU network on Solana, public market list, no API key.

GET /api/markets/ on api.nosana.com is anonymous (Nosana's docs name it as the
live price source). One market is one GPU class that independent hosts join;
a job posted to a market runs on whichever host in it picks the job up.

Price: each market states `usd_reward_per_hour`, what the host earns, and
`network_fee_percentage`, Nosana's fee on top. What a renter pays per hour is
usd_reward_per_hour x (1 + network_fee_percentage / 100): the formula Nosana's
own GPU Markets docs page uses to print its price table, and the ratio the
market's own on-chain fields carry (nos_job_price_per_second = nos_reward_per_
second x 1.10). nosana.com/pricing.md prints the bare host rate as "USD / hour";
that is the pre-fee figure. The USD figure is Nosana's; the NOS-per-second
fields are never read or converted. Nosana's terms allow access through a
documented interface, and its llms.txt / pricing.md name this endpoint as the
unauthenticated live price source.

Read: PREMIUM markets ("validated, high-performance") that are open to anyone.
Skipped:
  - COMMUNITY markets ("new or unvalidated"): the host's GPU is not validated,
    and Nosana's own price table leaves them out;
  - OTHER markets (enterprise multi-GPU nodes) and markets marked `client`
    (dedicated to one client, often with a TEE): not an open price;
  - markets with no positive USD rate.

Every market prices one GPU; a market whose name states a GPU count ("8xH100")
is never read as one GPU. Jobs on Nosana run to a market timeout on community
hosts that can go offline, so every listing is interruptible. The endpoint says
nothing about free hosts, so availability is unknown.
"""

import logging
import re
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx

import canonical
from models import ComputeListing, RawResponse
from providers.base import PollingPolicy, Provider, find_endpoint, to_decimal

if TYPE_CHECKING:
    from tables import RawSnapshot

log = logging.getLogger(__name__)

BASE_URL = "https://api.nosana.com"
ENDPOINT = "/api/markets/"
_MULTI = re.compile(r"\b\d+\s*x\s*\w", re.I)
_REQUIRED = ("slug", "name", "type", "usd_reward_per_hour", "network_fee_percentage")


class NosanaProvider(Provider):
    name = "nosana"
    # One anonymous request (about 45 KB); market rates change rarely.
    polling = PollingPolicy(interval_seconds=900, timeout_seconds=30)

    def __init__(self, client: httpx.AsyncClient | None = None):
        super().__init__(
            client
            or httpx.AsyncClient(
                base_url=BASE_URL,
                headers={"accept": "application/json", "user-agent": "opengrid-terminal/0.1"},
                timeout=30,
            )
        )

    async def fetch(self) -> list[RawResponse]:
        return [await self.get(ENDPOINT)]

    @staticmethod
    def normalize(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        """One open PREMIUM market becomes one single-GPU listing."""
        rows = find_endpoint(by_endpoint, ENDPOINT)
        if not rows:
            return []
        snapshot = rows[0]
        if not isinstance(snapshot.payload, list):
            raise ValueError("nosana: markets is not a list - response reshaped")
        listings = []
        for m in snapshot.payload:
            if not isinstance(m, dict) or any(k not in m for k in _REQUIRED):
                raise ValueError(f"nosana: market lost one of {_REQUIRED}: {str(m)[:200]}")
            if m["type"] != "PREMIUM" or m.get("client"):
                continue  # community / enterprise / client-dedicated: see the module docstring
            reward = to_decimal(m["usd_reward_per_hour"])
            fee = to_decimal(m["network_fee_percentage"])
            if reward is None or reward <= 0:
                continue
            if fee is None or fee < 0:
                raise ValueError(f"nosana: {m['slug']} network fee {m['network_fee_percentage']!r} is not a percentage")
            name = (m["name"] or "").strip()
            if not name or _MULTI.search(name):
                log.warning("nosana: market %s %r does not name one GPU; skipped", m["slug"], name)
                continue
            price = reward * (1 + fee / Decimal(100))
            listings.append(
                ComputeListing(
                    provider="nosana",
                    sku=m["slug"],
                    listing_id=m["slug"],
                    raw_gpu_name=name,
                    canonical_gpu_name=canonical.canonical_gpu_name(name),
                    gpu_count=1,
                    region=None,
                    country=None,
                    price_per_gpu_hour=price,
                    price_per_instance_hour=price,
                    currency="USD",
                    market_type="on_demand",
                    provider_tier="premium",
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
        return listings
