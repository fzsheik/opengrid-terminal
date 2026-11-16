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
    per_provider: bool = False   # one answer per provider: the path takes {hotkey}; read by lium_providers, hourly


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
    # public, no key, read since 2026-11-15
    Feed("machines", "/machines", "json", False, OBSERVED, "provider_reported",
         "Lium's price rules per GPU model: reference price, the band a provider may ask (p_min to p_max), the soft "
         "price limit, the median asking price, the share rented now and over 30 days, GPUs in the fleet, and what "
         "a provider is paid per epoch for a rented and for an idle GPU"),
    Feed("idle_ineligible", "/machines/capacity/ineligible", "json", False, OBSERVED, "provider_reported",
         "GPU models outside the idle-pay programme, with Lium's reason"),
    Feed("executors_count", "/executors/count", "json", False, OBSERVED, "provider_reported",
         "machines with a free GPU right now (the rentable list)"),
    Feed("executors_total", "/executors/total-count", "json", False, OBSERVED, "provider_reported",
         "active machines in the whole fleet right now, rented ones included"),
    Feed("version", "/version", "json", False, OBSERVED, "provider_reported",
         "Lium's API build and feature flags: a change here dates a platform release",
         volatile=("uptime_seconds",)),
    # public, no key, one answer per provider (lium_providers, hourly). The provider pages show these to every
    # visitor; the hotkeys come from the stored /api/executors copy (providers/lium.py OPERATOR_FIELDS).
    Feed("provider_stats", "/provider-stats/{hotkey}", "json", False, OBSERVED, "provider_reported",
         "per provider: rentals that started and failed, unique renters, renter feedback, average uptime, machines "
         "reclaimed, machines and GPUs, measured network speed; coordinates are not kept", per_provider=True),
    Feed("provider_executors", "/provider-stats/{hotkey}/executors", "json", False, OBSERVED, "provider_reported",
         "per provider, every machine it runs, rented ones included: rented or not, GPUs rented, revenue per hour, "
         "rented since, jobs finished, price, CPU, RAM, country; only these fields are kept", per_provider=True),
)}

PUBLIC_FEEDS = tuple(f for f in FEEDS.values() if not f.partner and not f.per_provider)
PROVIDER_FEEDS = tuple(f for f in FEEDS.values() if f.per_provider)
PARTNER_FEEDS = tuple(f for f in FEEDS.values() if f.partner)
# Lium's public stats feed is fetched and stored raw by providers/lium.py (raw_snapshots, endpoint STATS); this
# package parses it (public.parse_stats). Same kinds as above.
STATS_KIND = (TRANSACTION, PUBLISHED_AGGREGATE)


def url(feed: Feed, **path_params) -> str:
    return BASE_URL + API + feed.path.format(**path_params)


# --------------------------------------------------------------------------
# Lium's public Grafana (grafana.lium.io; docs.lium.io/providers/grafana: "No account is needed")
# --------------------------------------------------------------------------
# lium_data/grafana.py reads a panel by running the query the DASHBOARD holds for it at that moment, with the
# dashboard's own variables at one of their listed values. OpenGrid never writes a query of its own.

GRAFANA_URL = "https://grafana.lium.io"
# The options of the dashboards' `tier` variable, in the order they are read: the two tiers first, so a rental is
# stored with its tier at once, then "All" (no filter), which adds the rentals neither tier read returned.
TIERS = ("spot", "secure", "All")


@dataclass(frozen=True)
class Panel:
    name: str                  # the feed name in lium_feed_snapshots
    dashboard: str             # dashboard uid
    title: str                 # the panel's title exactly as the dashboard prints it
    kind: str
    basis: str
    what: str
    metric: str | None = None  # the column this panel fills in its table; None: one point per rental
    # Where the parsed rows go: "model_hourly" (lium_grafana_hourly; the rentals panel has no metric and its own
    # table), "network_hourly" (lium_grafana_network_hourly), "price_daily" (lium_grafana_price_daily) or
    # "daily_counts" (lium_grafana_daily_counts, where `metric` is the series name).
    table: str = "model_hourly"
    ref: str | None = None     # a panel that holds several queries: the refId of the one to read
    column: str | None = None  # the value column that query's answer must carry; any other answer is a failed read
    # what raw.store reads from a Feed
    fmt: str = "json"
    partner: bool = False
    volatile: tuple[str, ...] = ("from", "to")     # the window slides; only the cells decide "changed"
    keep: tuple[str, ...] = field(default=())

    @property
    def path(self) -> str:                         # the source provider_meta's registry names
        return f"grafana.lium.io/d/{self.dashboard}"


MARKETPLACE_PRICES, NETWORK_STATUS, RENTAL_CHECK = "marketplace-prices-v1", "bejrh6ldw5lhcc", "fejrp6bf9f5s0e"
# How the daily-medians panel names its two series per GPU model (the end of each column name).
ASKING_SUFFIX, PAID_SUFFIX = " - asking, unrented", " - paid, rented"

GRAFANA_PANELS: dict[str, Panel] = {p.name: p for p in (
    Panel("grafana_rentals", MARKETPLACE_PRICES, "Rented-at price over time ($gpu_type)", TRANSACTION, TRADE_RECORD,
          "one point per rental at its start: start time, GPU model, price per GPU-hour paid; also read once per "
          "tier (spot, secure). No GPU count, end time, renter or provider"),
    # the fleet before the spend: a zero spend is written only where the fleet read already made the hour's row
    Panel("grafana_listed", NETWORK_STATUS, "GPUs listed by type", OBSERVED, "provider_reported",
          "GPUs listed per UTC hour and GPU model (hourly mean of Lium's inventory samples)", metric="gpus_listed"),
    Panel("grafana_rented", NETWORK_STATUS, "Rented GPUs by type", OBSERVED, "provider_reported",
          "GPUs rented per UTC hour and GPU model (hourly mean of Lium's inventory samples)", metric="gpus_rented"),
    Panel("grafana_spend", NETWORK_STATUS, "Credit spend per hour, by GPU type", TRANSACTION, PUBLISHED_AGGREGATE,
          "renter spend in USD per hour, per UTC hour and GPU model (Lium's hourly mean of its own samples)",
          metric="spend_usd_per_hour"),
    # read since 2026-11-15 (migration 0043): Lium's own history of these reaches back before OpenGrid's collection
    Panel("grafana_launch_created", RENTAL_CHECK, "Rentals created and node-side deploy failures", OBSERVED,
          "provider_reported", "rentals created per UTC hour on the whole network (a rental that continues the "
          "previous one on the same node is not counted); history from 2025-09-04",
          metric="rentals_created", table="network_hourly", ref="A", column="Rentals created"),
    Panel("grafana_launch_failures", RENTAL_CHECK, "Rentals created and node-side deploy failures", OBSERVED,
          "provider_reported", "deploys that failed on the node's side per UTC hour on the whole network (failures "
          "Lium puts down to the renter's image are left out); history from 2026-08-12",
          metric="node_deploy_failures", table="network_hourly", ref="B", column="Node-side deploy failures"),
    Panel("grafana_price_daily", MARKETPLACE_PRICES, "Asking price vs paid price, daily medians, 30 days ($gpu_type)",
          OBSERVED, "provider_reported",
          "per UTC day and GPU model: the median asking price of unrented nodes and the median price paid on rented "
          "nodes (Lium's medians over its six-hourly samples, models with three nodes or more); paid from "
          "2025-12-03, asking from 2026-09-16", table="price_daily"),
    Panel("grafana_deploy_causes", RENTAL_CHECK, "Deploy failures per day, by cause", OBSERVED, "provider_reported",
          "failed deploys per UTC day and cause, node-side and renter-side, as Lium classifies them; history from "
          "2026-08-13", metric="deploy_failures", table="daily_counts"),
    Panel("grafana_penalties", NETWORK_STATUS, "Penalty events per day, by reason", OBSERVED, "provider_reported",
          "penalties applied to providers per UTC day and reason code; history from 2026-04-28",
          metric="penalties", table="daily_counts"),
)}
SERIES_PANELS = tuple(p for p in GRAFANA_PANELS.values() if p.metric and p.table == "model_hourly")
RENTALS_PANEL = GRAFANA_PANELS["grafana_rentals"]
NETWORK_PANELS = tuple(p for p in GRAFANA_PANELS.values() if p.table == "network_hourly")
DAILY_PANELS = tuple(p for p in GRAFANA_PANELS.values() if p.table in ("price_daily", "daily_counts"))
COUNT_SERIES = {p.metric: p for p in GRAFANA_PANELS.values() if p.table == "daily_counts"}
