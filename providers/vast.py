"""Vast.ai: a marketplace of third-party host offers, public JSON, no API key.

Offers are queried one GPU model at a time. A model can have hundreds of
offers at wildly different prices, and the cheapest is often one unverified
host, so each model becomes two listings: the median ask (the typical price)
and the cheapest ask (what a router would actually grab).

Capacity (only with VAST_API_KEY): a census per GPU model, two searches of verified on-demand machines, one for
rentable offers and one for offers that are not rentable now. A keyed search returns up to CENSUS_CAP offers (the
public one stops at 64 per query and is read in pages, see API_CAP), and only a keyed search returns machines that
are not rentable. An offer is a slice of one
machine: `gpu_ids` names its GPUs and `gpu_frac` its share of the machine, so

    free GPUs  = distinct gpu_ids across the rentable offers
    total GPUs = per machine, num_gpus / gpu_frac, summed over every machine either search returned

A non-rentable offer can contain free GPUs (an 8x slice of a machine with 2 GPUs busy), so busy is total - free,
never a count of non-rentable offers. "Total" means GPUs on verified machines listed on Vast now; a host that has
delisted its machine is in neither search. Prices are untouched by the census (normalize skips census rows).

Stored offers keep KEPT_FIELDS: what normalize() reads plus, raw-first, each offer's storage / egress / ingress prices,
measured network speeds, offer window and hardware detail (see COST_FIELDS), so full-cost history accrues before any
listing field reads it.
"""

import asyncio
import json
import logging
import re
import statistics
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx

import canonical
from models import CapacityObservation, ComputeListing, RawResponse
from providers.base import PollingPolicy, Provider, find_endpoint, reshaped

if TYPE_CHECKING:
    from tables import RawSnapshot

log = logging.getLogger(__name__)

BASE_URL = "https://console.vast.ai"
ENDPOINT = "/api/v0/bundles/"

# Vast's own gpu_name values, each checked against the live API.
GPU_NAMES = [
    "H100 SXM", "H100 PCIE", "H100 NVL", "H200", "H200 NVL", "B200",
    "A100 SXM4", "A100 PCIE", "L40S", "A40", "RTX A6000", "RTX 6000Ada",
    "RTX PRO 6000 WS", "RTX PRO 6000 S",
    "RTX 5090", "RTX 5080", "RTX 4090", "RTX 3090",
]  # fmt: skip

LIMIT = 300
# Vast's documented limit is 1 request/second.
RATE_PAUSE_SECONDS = 1.2
# The public search returns at most this many offers per query however large the limit, and takes no offset. So a
# model's book is read in pages (since 2026-11-15): offers come cheapest first, and the next query asks for
# dph_total >= the last price seen (offers already read are dropped by id). A page shorter than the cap ends the book.
# On 2026-11-15 only RTX 5090 (208 offers), RTX 4090 (126) and RTX 3090 (89) needed more than one page: 23 requests a
# round instead of 18. Before that day the stored book of those models was the 64 cheapest offers, so their median
# read low (RTX 5090: 0.749 over the 64 cheapest, 0.868 over the whole book) and their capacity was withheld.
API_CAP = 64
# A book longer than this many pages is stored as far as read and treated as cut off (capacity withheld).
MAX_PAGES = 12
# request["book"] of a search that read the whole book. Absent: one page only (every snapshot before 2026-11-15), or
# a book that could not be read to its end.
FULL_BOOK = "full"
# A keyed search stops at this many offers (checked 2026-10-30, "truncated": false regardless); a census that hits it
# is incomplete, so its counts are withheld.
CENSUS_CAP = 512
# Vast caps keyed searches at 20,000 result rows a day (429 "search_quota_exceeded", seen 2026-11-01 after a census
# every poll exhausted it). The census spends at most this many rows a day: after each census the next waits
# rows_used / budget of a day (at least an hour, at most CENSUS_MAX_GAP). Prices use the public search, not the key.
CENSUS_DAILY_ROW_BUDGET = 16_000
CENSUS_MIN_GAP = timedelta(hours=1)
CENSUS_MAX_GAP = timedelta(hours=12)
# host_id, cpu_name and cpu_cores (since 2026-11-15) say who runs each machine of the WHOLE fleet and what it is built
# on; the price search below carries them only for the cheapest offers. INTERNAL: read only by operators.py
# (methodology/internal/operators.md), never by a listing or a capacity row. The host's IP address and hostname stay out.
CENSUS_FIELDS = ("machine_id", "num_gpus", "gpu_frac", "gpu_ids", "geolocation", "rentable", "host_id", "cpu_name",
                 "cpu_cores")


def _census_query(gpu_name: str, rentable: bool) -> dict:
    return {"verified": {"eq": True}, "rentable": {"eq": rentable}, "type": "on-demand",
            "gpu_name": {"eq": gpu_name}, "limit": 5000}


def _query(gpu_name: str, price_from: float | None = None, strict: bool = False) -> str:
    """The public search for one model, cheapest first; `price_from` continues a book past the 64-offer cap."""
    q = {
        "verified": {"eq": True},
        "rentable": {"eq": True},
        "type": "on-demand",
        "gpu_name": {"eq": gpu_name},
        "order": [["dph_total", "asc"]],
        "limit": LIMIT,
    }
    if price_from is not None:
        q["dph_total"] = {"gt" if strict else "gte": price_from}
    return json.dumps(q)


# The fields normalize() reads.
LISTING_FIELDS = (
    "id", "machine_id", "host_id", "gpu_name", "num_gpus", "dph_total",
    "gpu_ram", "geolocation", "reliability", "cpu_cores_effective",
    "cpu_ram", "disk_space",
)  # fmt: skip
# Kept raw for the full cost of an offer and its hardware, though no listing field reads them yet (raw-first: history
# that is not stored cannot be backfilled). Units as Vast publishes them (checked against a live offer 2026-11-01):
#   storage_cost $/GB-month of disk (billed while the instance exists, even stopped); storage_total_cost $/h of the
#   offer's disk; inet_up_cost / inet_down_cost $/GB of egress / ingress; inet_up / inet_down measured Mbps;
#   start_date / end_date epoch seconds of the offer's window; dph_base $/h before storage; min_bid $/h interruptible.
# Left out on purpose: public_ipaddr and hostname (host identity), and fields that change on every request without
# meaning (score, duration, rn, time_remaining).
COST_FIELDS = (
    "dph_base", "storage_cost", "storage_total_cost", "inet_up_cost", "inet_down_cost", "inet_up", "inet_down",
    "start_date", "end_date", "min_bid", "discount_rate", "credit_discount_max",
)  # fmt: skip
HARDWARE_FIELDS = (
    "reliability2", "verification", "gpu_frac", "gpu_total_ram", "gpu_mem_bw", "bw_nvlink", "pcie_bw", "pci_gen",
    "gpu_lanes", "compute_cap", "cuda_max_good", "gpu_max_power", "dlperf", "total_flops", "cpu_name", "cpu_cores",
    "cpu_ghz", "cpu_arch", "disk_name", "disk_bw", "direct_port_count", "static_ip", "hosting_type", "cluster_id",
    "vms_enabled", "os_version", "driver_version",
)  # fmt: skip
KEPT_FIELDS = LISTING_FIELDS + COST_FIELDS + HARDWARE_FIELDS


def _keep(offer: dict) -> dict:
    """The stored offer: every field in KEPT_FIELDS, verbatim (None when the offer lacks it)."""
    return {k: offer.get(k) for k in KEPT_FIELDS}


def _quota_exhausted(raw: RawResponse) -> bool:
    return raw.status_code == 429 and "search_quota_exceeded" in str(raw.error or raw.payload or "")


def _retry_after(raw: RawResponse) -> int | None:
    m = re.search(r'"retry_after"\s*:\s*(\d+)', str(raw.error or raw.payload or ""))
    return int(m.group(1)) if m else None


class VastProvider(Provider):
    name = "vast"
    # Fast tier: the public search (one request per GPU model and per page of its book, ~23, paced to Vast's
    # documented 1/second: ~35 s) every 5 minutes. Capacity is the keyed census, on its own daily row budget (1-12 h apart, unchanged): its cadence is
    # the longest gap, CENSUS_MAX_GAP, for the stale flag and the capacity-silence alert.
    polling = PollingPolicy(interval_seconds=300, timeout_seconds=180, tier="fast",
                            capacity_interval_seconds=int(CENSUS_MAX_GAP.total_seconds()), rate_limit=(1, 1.0))

    def __init__(self, client: httpx.AsyncClient | None = None, api_key: str | None = None):
        super().__init__(
            client
            or httpx.AsyncClient(
                base_url=BASE_URL,
                headers={"accept": "application/json", "user-agent": "opengrid-terminal/0.1"},
                timeout=40,
            )
        )
        self._api_key = api_key
        self._census_next: datetime | None = None  # in memory: a restart runs one census, then the budget applies

    async def _census(self, gpu_name: str, rentable: bool) -> RawResponse:
        query = _census_query(gpu_name, rentable)
        # backoff=False: a census quota refusal limits the key, not the public price search.
        raw = await self.post(ENDPOINT, json=query, headers={"Authorization": f"Bearer {self._api_key}"},
                              backoff=False)
        offers = (raw.payload or {}).get("offers") if isinstance(raw.payload, dict) else None
        payload = None if offers is None else [{k: o.get(k) for k in CENSUS_FIELDS} for o in offers]
        return reshaped(raw, payload, request={"census": True, "gpu_name": gpu_name, "rentable": rentable})

    async def _one(self, gpu_name: str) -> RawResponse:
        """One model's whole book, as one stored response: page after page until a page is shorter than the cap.

        The first page decides the response (status, timing, failure). A later page that fails, a book longer than
        MAX_PAGES, or more than a page of offers at one price leaves the book cut off: stored as far as read, without
        the FULL_BOOK mark, so normalize withholds its capacity as it always did for a capped book.
        """
        first = await self._paced(lambda: self.get(ENDPOINT, params={"q": _query(gpu_name)}))
        offers = (first.payload or {}).get("offers") if isinstance(first.payload, dict) else None
        # The model asked for goes in `request`: the body does not repeat it when the book is empty.
        if offers is None:
            return reshaped(first, None, request={"gpu_name": gpu_name})
        book = {o.get("id"): o for o in offers if isinstance(o, dict)}
        page, full, pages = offers, len(offers) < API_CAP, 1
        while not full and pages < MAX_PAGES:
            last = page[-1].get("dph_total") if isinstance(page[-1], dict) else None
            if not isinstance(last, (int, float)):
                break
            raw = await self._paced(lambda: self.get(ENDPOINT, params={"q": _query(gpu_name, last)}))
            page = (raw.payload or {}).get("offers") if raw.ok and isinstance(raw.payload, dict) else None
            if not isinstance(page, list):
                break  # a failed page: keep what was read, cut off
            pages += 1
            new = [o for o in page if isinstance(o, dict) and o.get("id") not in book]
            book.update((o.get("id"), o) for o in new)
            if len(page) < API_CAP:
                full = True
            elif not new:
                break  # a whole page of offers at one price, all read already: the rest cannot be reached
        request = {"gpu_name": gpu_name, "book": FULL_BOOK} if full else {"gpu_name": gpu_name}
        return reshaped(first, [_keep(o) for o in book.values()], request=request)

    async def fetch(self) -> list[RawResponse]:
        # The API allows one request per second and answers 429 beyond it, so
        # go one model at a time (each request paced, with one retry after the stated wait).
        responses = []
        for n in GPU_NAMES:
            responses.append(await self._one(n))
        now = datetime.now(timezone.utc)
        if self._api_key and (self._census_next is None or now >= self._census_next):
            responses += await self._census_round(now)
        return responses

    async def _paced(self, call) -> RawResponse:
        raw = await call()
        if raw.status_code == 429 and not _quota_exhausted(raw):
            await asyncio.sleep(2)
            raw = await call()
        await asyncio.sleep(RATE_PAUSE_SECONDS)
        return raw

    async def _census_round(self, now: datetime) -> list[RawResponse]:
        """Both census halves for every model, within the daily row budget; stops at the first quota refusal."""
        out, rows = [], 0
        for n in GPU_NAMES:
            for r in (True, False):
                raw = await self._paced(lambda n=n, r=r: self._census(n, r))
                out.append(raw)
                if _quota_exhausted(raw):
                    wait = _retry_after(raw) or 3600
                    self._census_next = now + min(timedelta(seconds=wait), timedelta(hours=24))
                    log.warning("vast: keyed search quota exhausted; next census after %s", self._census_next.isoformat())
                    return out
                rows += max(1, len(raw.payload) if isinstance(raw.payload, list) else 1)
        gap = timedelta(days=1) * (rows / CENSUS_DAILY_ROW_BUDGET)
        self._census_next = now + min(max(gap, CENSUS_MIN_GAP), CENSUS_MAX_GAP)
        log.info("vast: census used %d rows; next census after %s", rows, self._census_next.isoformat())
        return out

    @staticmethod
    def normalize_capacity(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[CapacityObservation]:
        """One provider-wide row per GPU model whose census has both halves: free and total GPUs."""
        halves: dict[str, dict[bool, "RawSnapshot"]] = {}
        for snap in find_endpoint(by_endpoint, ENDPOINT):
            req = snap.request or {}
            if req.get("census") and isinstance(req.get("rentable"), bool):
                halves.setdefault(req["gpu_name"], {})[req["rentable"]] = snap
        out = []
        for gpu_name, pair in halves.items():
            if set(pair) != {True, False}:
                continue  # half a census would read as all-free or all-busy
            free_offers, busy_offers = pair[True].payload, pair[False].payload
            if not isinstance(free_offers, list) or not isinstance(busy_offers, list):
                raise ValueError(f"vast: census for {gpu_name} is not an offer list - response reshaped")
            free_ids, per_machine = set(), {}
            for o in free_offers + busy_offers:
                n, frac = o.get("num_gpus"), o.get("gpu_frac")
                if isinstance(n, int) and n > 0 and isinstance(frac, (int, float)) and frac > 0:
                    per_machine[o.get("machine_id")] = max(per_machine.get(o.get("machine_id"), 0), round(n / frac))
            for o in free_offers:
                free_ids.update(o.get("gpu_ids") or [])
            capped = max(len(free_offers), len(busy_offers)) >= CENSUS_CAP
            free, total = len(free_ids), sum(per_machine.values())
            out.append(CapacityObservation(
                provider="vast", sku=gpu_name, region=None, country=None, raw_gpu_name=gpu_name,
                canonical_gpu_name=canonical.canonical_gpu_name(gpu_name), gpu_count=1,
                in_stock=bool(free_offers),
                available_count=None if capped else free, total_count=None if capped else total,
                count_unit=None if capped else "gpu",
                raw_value=(f"free_gpus={free}; total_gpus={total}; machines={len(per_machine)}; "
                           f"rentable_offers={len(free_offers)}; other_offers={len(busy_offers)}"
                           + ("; capped" if capped else "")),
                source=ENDPOINT, observed_at=pair[True].fetched_at))
        return out

    @staticmethod
    def normalize(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        listings, done = [], set()
        for snapshot in find_endpoint(by_endpoint, ENDPOINT):
            if (snapshot.request or {}).get("census"):
                continue  # capacity census rows (keyed, verified-only); prices come from the public search
            gpu_name = (snapshot.request or {}).get("gpu_name")
            if gpu_name in done:
                continue  # two rounds within the read window (a deploy): the newest book of a model wins
            done.add(gpu_name)
            offers = [
                o
                for o in snapshot.payload or []
                if o.get("dph_total") and (o.get("num_gpus") or 0) > 0
            ]
            if not gpu_name or not offers:
                continue

            # Per-GPU ask for every offer, cheapest first.
            asks = sorted(
                Decimal(str(o["dph_total"])) / o["num_gpus"] for o in offers
            )
            cheapest = min(offers, key=lambda o: o["dph_total"] / o["num_gpus"])
            median = Decimal(str(statistics.median(asks)))
            total_gpus = sum(o["num_gpus"] for o in offers)
            # A book cut off at the cap undercounts supply, so say nothing rather than a low number. A book read
            # to its end (FULL_BOOK) is whole however long it is.
            capped = (snapshot.request or {}).get("book") != FULL_BOOK and len(offers) >= API_CAP

            def listing(kind: str, per_gpu: Decimal, **extra) -> ComputeListing:
                return ComputeListing(
                    provider="vast",
                    sku=gpu_name,
                    listing_id=f"{gpu_name}:{kind}",
                    raw_gpu_name=gpu_name,
                    canonical_gpu_name=canonical.canonical_gpu_name(gpu_name),
                    gpu_count=1,
                    region=extra.get("region"),
                    country=None,
                    price_per_gpu_hour=per_gpu,
                    price_per_instance_hour=per_gpu,
                    currency="USD",
                    market_type="on_demand",
                    provider_tier=kind,
                    interruptible=False,
                    available=True,
                    capacity=None if capped else total_gpus,
                    capacity_unit=None if capped else "gpu",
                    vcpu=extra.get("vcpu"),
                    ram_gb=extra.get("ram_gb"),
                    storage_gb=extra.get("storage_gb"),
                    observed_at=snapshot.fetched_at,
                )

            listings.append(listing("median", median))
            listings.append(
                listing(
                    "cheapest",
                    asks[0],
                    region=cheapest.get("geolocation"),
                    vcpu=None if not cheapest.get("cpu_cores_effective") else int(cheapest["cpu_cores_effective"]),
                    ram_gb=None if not cheapest.get("cpu_ram") else round(cheapest["cpu_ram"] / 1024, 1),
                    storage_gb=None if not cheapest.get("disk_space") else round(cheapest["disk_space"], 1),
                )
            )
        return listings
