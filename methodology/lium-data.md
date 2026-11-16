# Lium market data beyond prices

Besides its prices and fleet (the Lium listings and capacity, which are public), OpenGrid reads what Lium publishes
about its own market: completed rentals and their aggregates through Lium's data-partner feeds, several keyless
feeds about launches, demand, utilisation and what Lium pays providers, and the prices renters pay as Lium's public
Grafana dashboards show them. Every value is stored raw first and kept in
its own tables (`lium_*`). None of it is a listing, and none of it enters indices, fixings, spreads, percentiles,
routing or reliability scores.

Code: `lium_data/`, tables `store/lium.py` (migrations `0040_lium_data`, `0041_lium_grafana`,
`0043_lium_grafana_history`). Reads are admin-only, under
`/v1/ops/lium/`.

## Tier: internal

Every row is **internal** ([data visibility](/methodology/data-visibility)). The data-partner feeds need a key of an
account Lium has flagged as a data partner, and the terms for passing that data on are not agreed yet. The keyless
feeds are open to read, but Lium's Terms of Service (23 Oct 2026) grant only a limited licence to use the Service,
and Lium's partner page names only `pricing.json` and the public nodes feed as feeds to list. The Grafana
dashboards are open without an account, and the same terms apply to them. None of this allows republishing, so
OpenGrid keeps the data and does not show it. A feed moves to public only after Lium says it
may.

## Feeds

| Feed | What it holds | Kind ([data kinds](/methodology/data-kinds)) | Read |
|---|---|---|---|
| `trades.csv` (data partners) | One row per completed rental: hashed renter and provider ids, price per GPU-hour, GPU count, start, end, GPU model, provider country | transaction (trade record) | hourly, incrementally (`from` = the end day of the newest stored trade, `to` = today) |
| `dumps/{day}.csv` (data partners) | Lium's nightly file of one closed day's trades (01:00 UTC) | transaction (trade record) | each closed day once; the backfill walks back through them |
| `hourly-gpu.csv` (data partners) | Per closed UTC hour and GPU model: rentals, GPU-hours, GPU-hour-weighted mean paid price | transaction (published aggregate) | hourly |
| `daily-aggregate.csv` and its dump (data partners) | Per end day, GPU model and provider country: deals, GPU-hours, VWAP, low, high | transaction (published aggregate) | hourly, and each closed day's dump once |
| `/public/v1/stats` | Daily GMV billed, GPU-hours and rentals started; daily fleet utilisation; per model the 30-day median paid price | transaction (published aggregate) | already fetched by the Lium price poll; parsed from the stored copy |
| `/public/v1/launch-stats` | Per GPU type and GPUs per pod, over 14 days: share of launches that reached RUNNING, time to SSH p50 and p90 | observed (reported by Lium) | every 15 minutes |
| `/public/v1/requests` | Open renter requests per GPU class: requests, GPUs asked, oldest | observed (reported by Lium) | every 15 minutes; a 404 while Lium has the feature off is stored once |
| `/executors/stats` | GPUs rented and GPUs in total per GPU type, now | observed (reported by Lium) | every 15 minutes |
| `/machines/spot-idle-rates`, `/machines/capacity` | What Lium pays a provider per idle GPU-hour of a spot node, and its open idle-pay buckets: a supply-side floor, not a renter price | observed (reported by Lium) | every 15 minutes |
| `/v1/shared-config` | Lium's reference price per GPU model and its p90, and the price-rule rates (only these keys are kept) | observed (reported by Lium) | every 15 minutes |
| `/machines` | Lium's price rules per GPU model: reference price, the band a provider may ask (`p_min` to `p_max`), the soft price limit, the median asking price, the share rented now and over 30 days, GPUs in the fleet, and what a provider is paid per epoch for a rented and for an idle GPU | observed (reported by Lium) | every 15 minutes, since 2026-11-15 |
| `/machines/capacity/ineligible`, `/executors/count`, `/executors/total-count`, `/version` | GPU models outside idle pay; machines with a free GPU and machines in the whole fleet; the API build and feature flags | observed (reported by Lium) | every 15 minutes, since 2026-11-15 |
| `/provider-stats/{hotkey}` | Per provider: rentals that started and failed, unique renters, renter feedback, average uptime, machines reclaimed, machines and GPUs, measured network speed | observed (reported by Lium) | hourly, since 2026-11-15 |
| `/provider-stats/{hotkey}/executors` | Per provider, every machine it runs, rented ones included: rented or not, GPUs rented, revenue per hour, rented since, jobs finished, price, CPU, RAM, country | observed (reported by Lium) | hourly, since 2026-11-15 |
| Grafana, "Marketplace prices": *Rented-at price over time* | One point per rental at its start: start time, GPU model, price per GPU-hour paid, and its tier (spot or secure). No GPU count, end time, renter or provider | transaction (trade record) | hourly; history by backfill |
| Grafana, "Lium network status": *Credit spend per hour, by GPU type* | Renter spend in USD per hour, per UTC hour and GPU model | transaction (published aggregate) | hourly; history by backfill |
| Grafana, "Lium network status": *GPUs listed by type*, *Rented GPUs by type* | GPUs listed and GPUs rented per UTC hour and GPU model (hourly means of Lium's inventory samples) | observed (reported by Lium) | hourly; history by backfill |
| Grafana, "Marketplace prices": *Asking price vs paid price, daily medians, 30 days* | Per UTC day and GPU model: the median asking price of unrented nodes and the median price paid on rented nodes | observed (reported by Lium); the paid median is a published aggregate | about every six hours, since 2026-11-15; history by backfill (paid from 2025-12-03, asking from 2026-09-16) |
| Grafana, "Rental check": *Rentals created and node-side deploy failures* | Per UTC hour, whole network: rentals created, and deploys that failed on the node's side | observed (reported by Lium) | hourly, since 2026-11-15; history by backfill (rentals from 2025-09-04, failures from 2026-08-12) |
| Grafana, "Rental check": *Deploy failures per day, by cause* | Failed deploys per UTC day and cause, node-side and renter-side | observed (reported by Lium) | about every six hours, since 2026-11-15; history by backfill (from 2026-08-13) |
| Grafana, "Lium network status": *Penalty events per day, by reason* | Penalties applied to providers per UTC day and reason code | observed (reported by Lium) | about every six hours, since 2026-11-15; history by backfill (from 2026-04-28) |

**Trade record** is one rental as Lium reports it. **Published aggregate** is Lium's own figure computed from its
rentals. OpenGrid cannot check it against trade records until the partner feed is on, and then it can: the admin
check compares the two. When OpenGrid aggregates stored trade records itself, the basis is **derived from trades**.

## Storage

- **Raw first, change-only.** Every distinct answer of every feed is stored verbatim (`lium_feed_snapshots`, CSV or
  JSON text), and failures are stored too. When the newest answer for the same feed and parameters comes back with
  the same status and content, no new row is written: the stored row's last-seen time and count move instead.
  Volatile fields such as `generated_at` are left out of that comparison but kept in the body. A feed stuck at 403
  or 404 is therefore one row, however long it lasts.
- **Parsed rows** are rebuilt from the raw bodies (`python -m lium_data reparse`, `reparse-stats`). Each trade has a
  stable key: the feed's own trade id when it has one, otherwise a hash of the whole row. Re-reading an overlapping
  window or a dump therefore never counts a trade twice. Hourly and daily aggregate rows are updated in place when
  Lium revises them. A closed day of the published throughput that changes later is counted as a revision.
- **Column names.** Lium names the trade fields but not their header spelling. The parser accepts the usual
  spellings and keeps any other column verbatim. A row without price, GPU count, start, end or GPU model is counted
  as rejected and stays in the raw body.

## Lium's public Grafana

Lium publishes live market dashboards at `grafana.lium.io` and documents them as open without an account. Two of
them hold what the data-partner feeds would otherwise be needed for: the price of every rental at its start, and
renter spend per hour. Their panels also keep Lium's own history, which reaches back before OpenGrid began
collecting: daily asking and paid medians per GPU model, rentals created, failed deploys and penalties.

**OpenGrid never writes a query for it.** For every read, the collector fetches the dashboard, finds the panel by
its exact title, and takes the query the dashboard holds for that panel at that moment. It fills in the dashboard's
own variables with values the dashboard lists (GPU type "All"; tier "All", "spot" or "secure"), which is what the
page does when a visitor picks them, and asks for a time range, which is what the page's time picker does. A read
stops before anything is sent when the panel was renamed or removed, when its query uses a variable the collector
does not know, when the dashboard no longer lists a value, or when a tier read's query no longer filters by tier.
A panel that holds two queries is read one query at a time, each picked by the reference the dashboard gives it,
and an answer that lacks the value column that query is known for is a failed read too.
Each of those is stored as a failed read, once, and never guessed around. The job makes eleven requests an hour
(three dashboards, eight queries) and three more about every six hours for the daily panels.

**Rental starts.** The panel gives no rental id, so a rental's key is a hash of its start time to the nanosecond,
its GPU model and its price. Reading an overlapping window therefore never counts a rental twice. The panel is read
three times: the spot read and the secure read store each rental with its tier, and the unfiltered read then adds
the rentals neither returned, which keep an empty tier. Lium's tier labels begin in November 2026: every rental
from 11 November on carries one, 10 November is partly labelled, and almost none before it is (92,631 rentals read
back to September 2025, 8,795 of them with a tier). Spot and secure prices differ widely, so a statistic over
unlabelled rentals mixes the two. A rental is a start: the
panel does not say how many GPUs it took or when it ended.

**Hourly series.** One row per UTC hour and GPU model holds GPUs listed, GPUs rented and renter spend. The running
hour is partial, so each poll re-reads it and the hours just before it, and a value that moved is updated in place.
Only points on a whole hour are kept: the panels' step is one hour, and another step would be a different quantity.
The spend panel names about 100 GPU models, most of them never listed. A spend above zero is always stored. A zero
is stored only where the fleet panels made a row for that hour and model, so a model with no fleet and no spend has
no row.

**Daily asking and paid medians.** One row per UTC day and GPU model holds Lium's median asking price of unrented
nodes and its median price paid on rented nodes, both per GPU-hour. Lium computes them from the samples it takes
every six hours, one vote per node, and gives a median only for a model with three nodes or more that day; a
missing median is stored as empty, never as zero. The paid side reaches back to 2025-12-03 and the asking side to
2026-09-16, which is where Lium's own samples begin. This is the only history of asking prices per GPU model before
2026-11-15, when OpenGrid began reading `/machines`. The panel is read with tier "All" only: Lium's tier labels
begin on 2026-11-10, so a spot or secure read has no history to recover, and that query takes about 14 seconds
for 30 days against a 15-second limit on Lium's side.

**Rentals created and failed deploys.** One row per UTC hour for the whole network holds the rentals created and
the deploys that failed on the node's side. Lium does not count a rental that continues the previous one on the
same node, and leaves out the failures it puts down to the renter (an image that exits at start, a failed image
build, a rental deleted while starting). The panel has no row for an hour with nothing, so an empty count means
"no row", and before 2026-08-12, where Lium's failure log begins, an empty failure count means "not recorded".
Neither count is split by GPU model. A second panel gives the failed deploys per UTC day and cause, renter-side
causes included, in Lium's own classification.

**Penalties.** One row per UTC day and reason code holds the penalties Lium applied to providers, from 2026-04-28.

**Whole days only.** The daily panels group by UTC day, so they are only ever asked from a UTC midnight: a window
that began mid-day would return a partial first day and overwrite the full one. The running day is partial until
it closes; the collector re-reads it and the day before about every six hours, and a value that moved is updated
in place.

**History.** `python -m lium_data grafana-backfill --since YYYY-MM-DD` walks back from today in closed chunks
(three days of rentals, fourteen days of hourly series, so the hourly step stays one hour, thirty days of the daily
panels), newest first. A chunk read after it closed is skipped on the next run, so a stopped backfill resumes.
Without `--since` it stops after two empty chunks in a row. Three failed chunks in a row stop a panel. `--only`
names the groups to read, comma-separated: `rentals`, `series` (hourly spend and fleet per GPU model), `launches`
(hourly rentals created and failed deploys) and `daily` (daily medians, failure causes, penalties). The dashboards
filter with both ends of the range included, so a chunk of the `launches` and `daily` groups ends one millisecond
before its closing midnight; otherwise the bucket that begins on that midnight would come back holding a single
instant and overwrite the full value the newer chunk stored.

**Not read.** *Share of listed nodes rented, hourly* and *Hourly median rented-at price* repeat what the fleet
series and the rental starts already give, and the first is limited by its own query to the last 30 days. *Median
asking price and GPUs rented, daily, 30 days* ("Price your GPUs") fixes its 30 days in the query and gives one
network-wide line unless a GPU type is picked, and the dashboard lists no GPU type to pick from. The "GPU demand
analytics" panels are the same inventory as the fleet series. The "Penalty events" table and the "Node check logs"
are per node; OpenGrid stores no per-node log.

**Limits.** The dashboards are Lium's own account of its market and cannot be checked against trade records
without the partner feed. Lium can change or remove a panel at any time; the collector then records the failure
and stores nothing until the panel is back in a shape it can read.

## Per-provider feeds

The two per-provider feeds are read hourly for every miner hotkey in the newest stored executors list, and for every
provider that answered in the last 30 days, so a provider whose machines are all rented is still read. Requests go
one at a time, 1.5 seconds apart (Lium allows 60 a minute); a 429 ends the round.

- **What is kept.** Each answer is stored slimmed: no coordinates, GPU serials, validator keys or live counters
  (uptime minutes, free disk, open ports).
- **What counts as a change.** The jobs-finished counter and the average uptime move on almost every read. They
  stay in the stored body and are written as values whenever anything else about the provider changes, but they do
  not make a new row by themselves. A rental starting or ending, a price change, or a machine joining or leaving
  does.
- **One provider at a time.** An answer speaks only for its own provider: a machine missing from it is written
  once as absent for that provider, and never touches another provider's machines.
- **History starts 2026-11-15.** Lium publishes no history for these; `created_at` and `rented_since` on each
  machine are the only dates that reach further back.

Read: `/v1/ops/lium/providers` (admin). `operators.py` also uses the machine lists to add fully rented machines to
its Lium fleet.

## Statistics OpenGrid computes

From stored trades, per UTC end day and GPU model: rentals, GPU-hours (GPUs x duration), the volume-weighted average
price (sum of price x GPU-hours over GPU-hours), the lowest and highest price, and the notional value. A rental counts
whole on the day it ended, which is the axis Lium's windows use. Only USD rows count.

From the daily medians, where Lium gave both for a day and GPU model: the asking median over the paid median, minus
one, as a percentage. From the hourly counts: node-side failures per 100 rentals created, per hour or per UTC day.

From the Grafana rental starts, per UTC day or hour of start, GPU model and tier: the number of rentals, and the
median, mean, lowest and highest price per GPU-hour. Every rental counts once whatever its size or length, because
the panel gives neither; this is a count of deals, not a volume-weighted price. Rentals at a price of zero are left
out.

## Turning the partner feeds on

The `lium_partner` job is registered only when `LIUM_PARTNER_API_KEY` is set. Until Lium flags the account, every
call answers 403. That answer is stored once and logged once, and the job then checks again only every 6 hours.
History comes from the nightly dumps: `python -m lium_data backfill --since YYYY-MM-DD` walks back from yesterday,
one day at a time, and skips days already stored. When it is given no `--since`, it stops after three empty days in
a row. A 403 stops it at once.
