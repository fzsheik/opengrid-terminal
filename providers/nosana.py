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
hosts that can go offline, so every listing is interruptible. The market list
says nothing about free hosts (its `nodes` field is always empty), so a
listing's own availability stays unknown.

Capacity: GET /api/nodes/queued-nodes (anonymous; Nosana's API reference lists
it as "Queued nodes", learn.nosana.com/api/hosts.html) returns one row per host
waiting in a market's queue: nodeAddress, marketAddress, state "QUEUED". Nosana's
Jobs program docs say what that queue is: "When there a more nodes than jobs in
a given Market, the queue will fill up with nodes" (learn.nosana.com/programs/
jobs.html), and a queued node "will pick up new job postings". So the hosts
queued in a market are the hosts idle now and ready to take a job. One row per
market that has a listing (sku the market slug, provider-wide): the count of
distinct queued hosts, unit instances (one job, one GPU each). A market with no
queued host gets 0 and in_stock false: a job posted now waits in the queue until
a host frees up. No total: busy hosts are not in the list. The response is not
paged (limit and page parameters change nothing; 87 rows on 2026-11-15). Not
read: whether a PREMIUM market's job can fall through to its linked COMMUNITY
market (`premium_community_relation`); Nosana does not document it, so each
market's count is its own queue only. An empty list, a row without its market
or a state other than QUEUED fails the capacity read, never the prices.
"""

import logging
import re
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx

import canonical
from models import CapacityObservation, ComputeListing, RawResponse
from providers.base import PollingPolicy, Provider, find_endpoint, to_decimal

if TYPE_CHECKING:
    from tables import RawSnapshot

log = logging.getLogger(__name__)

BASE_URL = "https://api.nosana.com"
ENDPOINT = "/api/markets/"
QUEUED = "/api/nodes/queued-nodes"
_MULTI = re.compile(r"\b\d+\s*x\s*\w", re.I)
_REQUIRED = ("slug", "name", "type", "usd_reward_per_hour", "network_fee_percentage")


class NosanaProvider(Provider):
    name = "nosana"
    # Two anonymous requests (about 45 KB and 12 KB); market rates change rarely, the host queue moves all day.
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
        # Prices first; the queue is a separate response, so its failure is stored alone.
        return [await self.get(ENDPOINT), await self.get(QUEUED)]

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

    @staticmethod
    def normalize_capacity(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[CapacityObservation]:
        """One open PREMIUM market becomes one row: the hosts queued in it, idle and waiting for a job."""
        markets, queue = find_endpoint(by_endpoint, ENDPOINT), find_endpoint(by_endpoint, QUEUED)
        if not markets or not queue:
            return []
        rows = queue[0].payload
        if not isinstance(rows, list) or not rows:
            # An empty queue across 40-odd markets has never been seen; a broken answer must not read as sold out.
            raise ValueError("nosana: queued-nodes is not a non-empty list - response reshaped")
        hosts: dict[str, set[str]] = {}
        for r in rows:
            if not isinstance(r, dict) or not r.get("marketAddress") or not r.get("nodeAddress") or r.get("state") != "QUEUED":
                raise ValueError(f"nosana: queued node lost its market, address or QUEUED state: {str(r)[:160]}")
            hosts.setdefault(r["marketAddress"], set()).add(r["nodeAddress"])
        if not isinstance(markets[0].payload, list):
            raise ValueError("nosana: markets is not a list - response reshaped")
        out = []
        for m in markets[0].payload:
            if not isinstance(m, dict) or any(k not in m for k in _REQUIRED):
                raise ValueError(f"nosana: market lost one of {_REQUIRED}: {str(m)[:200]}")
            name = (m["name"] or "").strip()
            reward = to_decimal(m["usd_reward_per_hour"])
            # The same markets normalize() lists, so every row joins to a price.
            if m["type"] != "PREMIUM" or m.get("client") or reward is None or reward <= 0 or not name or _MULTI.search(name):
                continue
            if not m.get("address"):
                raise ValueError(f"nosana: market {m['slug']} has no address to join the queue on")
            free = len(hosts.get(m["address"], ()))
            out.append(CapacityObservation(
                provider="nosana", sku=m["slug"], region=None, raw_gpu_name=name,
                canonical_gpu_name=canonical.canonical_gpu_name(name), gpu_count=1,
                in_stock=free > 0, available_count=free, count_unit="instance",
                raw_value=f"{free} queued nodes", source=QUEUED, observed_at=queue[0].fetched_at))
        return out
