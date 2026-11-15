"""Every Lium feed this package reads (paths relative to https://lium.io/api, per https://lium.io/api/openapi.json).

kind / basis label how each number was obtained (methodology/data-kinds.md):
    transaction  what renters actually paid on Lium. basis trade_record: one completed rental as Lium reports it;
                 published_aggregate: Lium's own aggregate of its rentals (hourly mean, daily VWAP, 30-day median
                 paid price, daily GMV); derived_from_trades: OpenGrid's aggregate of stored trade records
    observed     what Lium states about its market that is not a price paid: launch success, renter requests,
                 rented/total counts, provider idle pay, reference prices
"""

from dataclasses import dataclass, field

BASE_URL = "https://lium.io"
API = "/api"
USER_AGENT = "opengrid-terminal/0.1"
TIMEOUT = 45
# Lium allows 60 requests a minute per IP on its public feeds (docs.lium.io/developers/public-stats-feed); every
# poll here is a handful of requests every 15 minutes, and the backfill waits this long between requests.
POLITE_GAP_SECONDS = 1.5

TRANSACTION, OBSERVED = "transaction", "observed"
TRADE_RECORD, PUBLISHED_AGGREGATE, DERIVED_FROM_TRADES = "trade_record", "published_aggregate", "derived_from_trades"


@dataclass(frozen=True)
class Feed:
    name: str
    path: str                  # under /api; {day} is filled for the dumps
    fmt: str                   # "csv" | "json"
    partner: bool              # needs the X-API-Key of an account Lium flags as a data partner
    kind: str
    basis: str
    what: str
    volatile: tuple[str, ...] = ("generated_at",)   # JSON keys left out of the change-only hash (kept in the body)
    keep: tuple[str, ...] = field(default=())        # JSON keys kept from a large object (empty: the whole body)


FEEDS: dict[str, Feed] = {f.name: f for f in (
    # data partners only (403 for any other account)
    Feed("trades", "/data-partners/trades.csv", "csv", True, TRANSACTION, TRADE_RECORD,
         "completed rentals: hashed renter and provider, price per GPU-hour, GPU count, start, end, GPU model, "
         "provider country; from/to (end days, at most 62 per call) or one closed day"),
    Feed("trades_dump", "/data-partners/dumps/{day}.csv", "csv", True, TRANSACTION, TRADE_RECORD,
         "the nightly (01:00 UTC) trades file of one closed day; 404 until written"),
    Feed("hourly_gpu", "/data-partners/hourly-gpu.csv", "csv", True, TRANSACTION, PUBLISHED_AGGREGATE,
         "per closed UTC hour and GPU model: rentals, GPU-hours, GPU-hour-weighted mean paid price; hours<=720"),
    Feed("daily_aggregate", "/data-partners/daily-aggregate.csv", "csv", True, TRANSACTION, PUBLISHED_AGGREGATE,
         "per end day, GPU model and provider country: deals, GPU-hours, VWAP, low, high"),
    Feed("daily_aggregate_dump", "/data-partners/dumps/{day}.daily-aggregate.csv", "csv", True, TRANSACTION,
         PUBLISHED_AGGREGATE, "the nightly daily-aggregate file of one closed day; 404 until written"),
    # public, no key
    Feed("launch_stats", "/public/v1/launch-stats", "json", False, OBSERVED, "provider_reported",
         "per GPU type and GPUs per pod, 14 days: share of launches reaching RUNNING, time to SSH p50 / p90; "
         "recomputed every 15 minutes"),
    Feed("requests", "/public/v1/requests", "json", False, OBSERVED, "provider_reported",
         "open renter requests per GPU class (demand): requests, GPUs asked, oldest; 404 while the feature is off"),
    Feed("executor_stats", "/executors/stats", "json", False, OBSERVED, "provider_reported",
         "per GPU type: GPUs rented and GPUs in total right now (live utilisation)"),
    Feed("spot_idle_rates", "/machines/spot-idle-rates", "json", False, OBSERVED, "provider_reported",
         "what Lium pays providers per idle GPU-hour of a spot node, per GPU model and count: a supply-side floor, "
         "not a renter price"),
    Feed("idle_capacity", "/machines/capacity", "json", False, OBSERVED, "provider_reported",
         "open earning capacity per GPU model, buckets of 1 and 8 GPUs: cap, unrented count, hourly rate, what one "
         "more idle node would be paid"),
    Feed("shared_config", "/v1/shared-config", "json", False, OBSERVED, "provider_reported",
         "Lium's reference price per GPU model (machine_prices) and its p90, and the price-rule rates; only these "
         "keys are kept (the rest is chain and driver configuration)",
         keep=("machine_prices", "machine_prices_p90", "machine_max_price_rate", "machine_min_price_rate",
               "soft_limit_price_rate", "rental_fees_rate", "volume_gb_hour_price_usd")),
)}

PUBLIC_FEEDS = tuple(f for f in FEEDS.values() if not f.partner)
PARTNER_FEEDS = tuple(f for f in FEEDS.values() if f.partner)
# Lium's public stats feed is fetched and stored raw by providers/lium.py (raw_snapshots, endpoint STATS); this
# package parses it (public.parse_stats). Same kinds as above.
STATS_KIND = (TRANSACTION, PUBLISHED_AGGREGATE)


def url(feed: Feed, **path_params) -> str:
    return BASE_URL + API + feed.path.format(**path_params)
