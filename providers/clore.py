"""Clore.ai: a peer-to-peer marketplace of independent hosts, read with an account key.

GET https://api.clore.ai/v1/marketplace (header `auth: <key>`, mandatory) lists
every public server, rented or not: about 2,800 servers and 3.8 MB per call.
GET /v1/renter_fees gives the fee a renter pays on top of the listed price.
Clore allows 1 request per second per key.
  API reference:  https://docs.clore.ai/for-hosts/api  (sections `marketplace`, `renter_fees`)
  Quick reference: https://docs.clore.ai/dev/reference/api-reference

What the price fields are (checked against the docs and a live snapshot, 2026-10-21):
- price.on_demand is "On demand price per day", one figure per accepted currency,
  for the WHOLE server (all of gpu_array). The host sets it; setting a per-GPU price
  makes the server price GPU count x that price (docs.clore.ai/for-hosts/server-settings).
  The marketplace guide says the same: "Prices in the feed are per server per day in
  USD - divide by 24 for hourly, and by the GPU count for per-GPU"
  (https://docs.clore.ai/guides/getting-started/pricing). A comment in the quick
  reference calls a usd field "USD per hour"; the API reference and the magnitudes
  (an RTX 4090 at $11.50 is $0.48/hour per day, $11.50/hour otherwise) say per day.
- "USD-Blockchain" is the USD stablecoin rail: USDT/USDC deposits credit one USD
  balance that pays for rentals (https://docs.clore.ai/for-renters/fee-structure,
  FAQ). The API documents `USD-Blockchain_on_demand` as "On-demand price in USD per
  day". It is the only field that is a USD price a renter actually pays, and it is
  the one read here. Servers that do not accept USD have no such key (they cannot be
  rented in USD) and give no price.
- price.usd.on_demand_usd repeats USD-Blockchain (equal on every server in the live
  snapshot); on_demand_btc / on_demand_clore are the BTC and CLORE prices converted to
  USD at an exchange rate, and original_usd is the host's USD autopricing target for
  those coins. None of them is read.
- The listed price is the host's base price without fees: "Your actual cost as a
  renter = listed price + your base fee + your extra fee" (fee-structure page). The
  renter's on-demand base and extra fee for USD-Blockchain come from /v1/renter_fees
  (5% and 0% today; Proof-of-Holding can halve the base fee, not assumed here), so a
  per-GPU hour = listed x (1 + (base + extra)/100) / 24 / GPUs, in exact Decimal.
- Spot is an auction: renters bid per day, are billed at their bid and can be
  outbid or displaced by an on-demand rental (https://docs.clore.ai/spot/spot-market).
  price.spot is the host's minimum bid, a floor rather than a price paid, and
  price.usd.spot is 0 on every server, so spot is not read: on-demand only.
- `rented` is "Is server rented on demand": a rented server cannot be taken, so, like
  Vast (which only asks for rentable offers), it gives no price.

Terms: the clore.ai Terms of Service (https://clore.ai/terms-and-conditions, effective
2026-08-19, the entire agreement) grant a license to access and use the Services and do
not restrict use of marketplace data; the API is documented for scripted use. An older
docs copy (https://docs.clore.ai/legal/terms-of-service, March 2026) limits use to
personal or internal business purposes and forbids republishing Platform content
without written consent. The newer site terms are taken to govern (CAPABILITIES.md 3.1).

Like Vast, a GPU model has many asks at very different prices, so each model becomes
two listings: the median ask (the typical price) and the cheapest ask (often one host;
excluded from market statistics by its provider_tier). A server whose "Nx" in
specs.gpu disagrees with gpu_array's length, or whose gpu_array mixes models, is
skipped, never guessed. The GPU name is the gpu_array model plus specs.gpuram, the
per-GPU VRAM in GB: "RTX 4090 24GB".

Capacity (normalize_capacity, count shape, provider-wide): per GPU name, free GPUs = GPUs
on servers with `rented: false` and total = GPUs on every server, both counted over the
servers the price book could hold (USD-priced, one model, count agreeing with gpu_array),
so the free count equals the listings' `capacity`. Sku is the listing sku, so stock joins
to price. A model whose servers are all rented gets `in_stock: false` and no price.
"Free" is free of an on-demand rental: a spot renter on a free server can be displaced.
"""

import asyncio
import logging
import re
import statistics
from collections import defaultdict
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx

import canonical
from models import CapacityObservation, ComputeListing, RawResponse
from providers.base import PollingPolicy, Provider, find_endpoint, reshaped, to_decimal

if TYPE_CHECKING:
    from tables import RawSnapshot

log = logging.getLogger(__name__)

BASE_URL = "https://api.clore.ai"
MARKET = "/v1/marketplace"
FEES = "/v1/renter_fees"
COIN = "USD-Blockchain"
# Clore's documented limit is 1 request/second per key.
RATE_PAUSE_SECONDS = 1.2
_COUNT = re.compile(r"^(\d+)x\s")
_DISK_GB = re.compile(r"(\d+(?:\.\d+)?)GB\s*$")


def _keep(server: dict) -> dict:
    """The fields normalize() reads, verbatim. Owner ids, ratings and telemetry are dropped."""
    specs = server.get("specs") or {}
    price = server.get("price") or {}
    on_demand = price.get("on_demand") if isinstance(price, dict) else None
    return {
        "id": server.get("id"),
        "rented": server.get("rented"),
        "price": {"on_demand": {COIN: on_demand[COIN]} if isinstance(on_demand, dict) and COIN in on_demand else {}},
        "specs": {
            "gpu": specs.get("gpu"),
            "gpuram": specs.get("gpuram"),
            "cpus": specs.get("cpus"),
            "ram": specs.get("ram"),
            "disk": specs.get("disk"),
            "net": {"cc": (specs.get("net") or {}).get("cc")},
        },
        "gpu_array": server.get("gpu_array"),
    }


def _fee_multiplier(body) -> Decimal:
    """1 + the renter's on-demand base and extra fee for USD, in percent."""
    try:
        on_demand = body["renter_fees"]["on_demand"]
        base = to_decimal(on_demand["base"][COIN])
        extra = to_decimal((on_demand.get("extra") or {}).get(COIN, 0))
    except (KeyError, TypeError) as exc:
        raise ValueError(f"clore: renter_fees lost the on-demand {COIN} fee: {exc!r}") from None
    if base is None or extra is None or not (0 <= base + extra < 100):
        raise ValueError(f"clore: renter fee for {COIN} unreadable: {base!r} + {extra!r}")
    return 1 + (base + extra) / 100


def _threads(cpus) -> int | None:
    m = re.fullmatch(r"\s*\d+\s*/\s*(\d+)\s*", str(cpus or ""))
    return int(m.group(1)) if m else None


def _disk_gb(disk) -> float | None:
    m = _DISK_GB.search(str(disk or ""))
    return round(float(m.group(1)), 1) if m else None


def _servers(snapshot) -> list[dict]:
    body = snapshot.payload
    if not isinstance(body, dict) or not isinstance(body.get("servers"), list) or body.get("code") != 0:
        raise ValueError("clore: marketplace has no `servers` list or a non-zero code - response reshaped")
    for s in body["servers"]:
        if not isinstance(s, dict) or "rented" not in s or not isinstance(s.get("specs"), dict):
            raise ValueError(f"clore: server lost its rented flag or specs: {s!r}"[:200])
    return body["servers"]


def _book_entry(s: dict, warn: bool = True) -> tuple[str, Decimal] | None:
    """(GPU name, listed USD per server-day) for a server the book can hold; None when it cannot."""
    arr = s.get("gpu_array") or []
    m = _COUNT.match(s["specs"].get("gpu") or "")
    if not arr or not m or int(m.group(1)) != len(arr):
        if warn:  # once per poll, from normalize(); capacity skips the same servers quietly
            log.warning("clore: server %s says %r but gpu_array has %d; skipped",
                        s.get("id"), s["specs"].get("gpu"), len(arr))
        return None
    if len(set(arr)) != 1:
        return None  # mixed models: no single GPU to price
    listed = to_decimal(((s.get("price") or {}).get("on_demand") or {}).get(COIN))
    if listed is None or listed <= 0:
        return None  # not rentable in USD
    vram = s["specs"].get("gpuram")
    if not isinstance(vram, int) or isinstance(vram, bool) or vram <= 0:
        return None
    return f"{arr[0]} {vram}GB", listed


class CloreProvider(Provider):
    name = "clore"
    # Two keyed requests per poll, paced to 1/second; the marketplace body is ~4 MB.
    polling = PollingPolicy(interval_seconds=900, timeout_seconds=90)

    def __init__(self, api_key: str, client: httpx.AsyncClient | None = None):
        super().__init__(
            client
            or httpx.AsyncClient(
                base_url=BASE_URL,
                headers={"auth": api_key, "accept": "application/json", "user-agent": "opengrid-terminal/0.1"},
                timeout=60,
            )
        )

    async def fetch(self) -> list[RawResponse]:
        market = await self.get(MARKET)
        await asyncio.sleep(RATE_PAUSE_SECONDS)
        fees = await self.get(FEES)

        body = market.payload
        if isinstance(body, dict) and isinstance(body.get("servers"), list):
            # my_servers (the account's own hosts) and everything not read is dropped.
            market = reshaped(market, {"code": body.get("code"), "servers": [_keep(s) for s in body["servers"]]})
        # Otherwise stored as-is; normalize() refuses it.

        fb = fees.payload
        if isinstance(fb, dict) and isinstance(fb.get("renter_fees"), dict):
            # Only the on-demand renter fee; the account's PoH balance is dropped.
            fees = reshaped(fees, {"code": fb.get("code"), "renter_fees": {"on_demand": fb["renter_fees"].get("on_demand")}})
        return [market, fees]

    @staticmethod
    def normalize_capacity(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[CapacityObservation]:
        """One provider-wide row per GPU name: free GPUs (not rented) out of all GPUs listed."""
        markets = find_endpoint(by_endpoint, MARKET)
        if not markets:
            return []
        free: dict[str, list[int]] = defaultdict(lambda: [0, 0])   # gpus, servers
        total: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for s in _servers(markets[0]):
            entry = _book_entry(s, warn=False)
            if entry is None or not isinstance(s["rented"], bool):
                continue
            n = len(s["gpu_array"])
            for tally in ((total,) if s["rented"] else (total, free)):
                tally[entry[0]][0] += n
                tally[entry[0]][1] += 1
        out = []
        for gpu_name, (gpus, servers) in sorted(total.items()):
            free_gpus, free_servers = free.get(gpu_name, [0, 0])
            out.append(CapacityObservation(
                provider="clore", sku=gpu_name, region=None, raw_gpu_name=gpu_name,
                canonical_gpu_name=canonical.canonical_gpu_name(gpu_name), gpu_count=1,
                in_stock=free_gpus > 0, available_count=free_gpus, total_count=gpus, count_unit="gpu",
                raw_value=f"free_gpus={free_gpus}; total_gpus={gpus}; free_servers={free_servers}; servers={servers}",
                source=MARKET, observed_at=markets[0].fetched_at))
        return out

    @staticmethod
    def normalize(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        markets = find_endpoint(by_endpoint, MARKET)
        if not markets:
            return []
        snapshot = markets[0]
        servers = _servers(snapshot)
        fee_rows = find_endpoint(by_endpoint, FEES)
        if not fee_rows:
            raise ValueError("clore: no renter_fees snapshot - the renter's price cannot be stated")
        multiplier = _fee_multiplier(fee_rows[0].payload)

        books: dict[str, list[tuple[Decimal, dict]]] = defaultdict(list)
        for s in servers:
            if s["rented"] is not False:
                continue  # rented on demand: not an ask anyone can take
            entry = _book_entry(s)
            if entry is None:
                continue
            gpu_name, listed = entry
            books[gpu_name].append((listed * multiplier / 24 / len(s["gpu_array"]), s))

        listings = []
        for gpu_name, book in books.items():
            book.sort(key=lambda b: (b[0], b[1].get("id") or 0))
            asks = [p for p, _ in book]
            cheapest = book[0][1]
            total_gpus = sum(len(s["gpu_array"]) for _, s in book)
            cc = (cheapest["specs"].get("net") or {}).get("cc")

            def listing(kind: str, per_gpu: Decimal, **extra) -> ComputeListing:
                return ComputeListing(
                    provider="clore",
                    sku=gpu_name,
                    listing_id=f"{gpu_name}:{kind}",
                    raw_gpu_name=gpu_name,
                    canonical_gpu_name=canonical.canonical_gpu_name(gpu_name),
                    gpu_count=1,
                    region=None,
                    country=extra.get("country"),
                    price_per_gpu_hour=per_gpu,
                    price_per_instance_hour=per_gpu,
                    currency="USD",
                    market_type="on_demand",
                    provider_tier=kind,
                    interruptible=False,
                    available=True,
                    capacity=total_gpus,
                    capacity_unit="gpu",
                    vcpu=extra.get("vcpu"),
                    ram_gb=extra.get("ram_gb"),
                    storage_gb=extra.get("storage_gb"),
                    observed_at=snapshot.fetched_at,
                )

            ram = cheapest["specs"].get("ram")
            listings.append(listing("median", statistics.median(asks)))
            listings.append(
                listing(
                    "cheapest",
                    asks[0],
                    country=cc if isinstance(cc, str) and re.fullmatch(r"[A-Z]{2}", cc) else None,
                    vcpu=_threads(cheapest["specs"].get("cpus")),
                    ram_gb=float(ram) if isinstance(ram, (int, float)) and ram > 0 else None,
                    storage_gb=_disk_gb(cheapest["specs"].get("disk")),
                )
            )
        return listings
