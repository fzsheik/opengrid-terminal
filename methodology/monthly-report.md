# Monthly market report

Once a month OpenGrid answers one question from its stored prices: over the calendar month, did the GPU clouds
move their prices proportionally? The report compares every cloud's price for each featured GPU at the start and
at the end of the month, counts who raised, cut or kept it, measures how unevenly they moved, and checks whether a
cloud that repriced several GPUs moved them all by the same percentage.

Code: `reports/monthly.py` (report, job, CLI), `api/reports.py`, page `/reports`. Version 1. Data kinds: **observed**
(live months) or **archived** (months before live recording), and **derived** (every statistic below).

The full report is **internal**: it names providers next to their prices, so it is shown only to signed-in readers
(the operator, the read-only viewer, a `data:read` key), never to anonymous visitors, never with `PUBLIC_PAGES`,
never on the Data API host. Each report carries a separate **public-safe summary** (below) written to be quoted.

## The month

A calendar month in UTC, `[YYYY-MM-01 00:00, first of the next month)`. A month is reported only once it has
ended. The `monthly_report` job runs daily and generates the previous month once, from 06:00 UTC on the 1st (the
hourly rollup and the index tail are final well before then); a stored month is never overwritten unless an admin
regenerates it explicitly (`POST /v1/reports/monthly/{month}/regenerate` or `--force`), which keeps the first
`generated_at`. Generation reads stored data only and contains no clock, so the same stored data gives the same
report.

## One price per cloud

A cloud's price for a GPU is its **lowest on-demand list price per GPU-hour**, one vote per cloud whatever its
listing count or regions (as in [dispersion](/methodology/dispersion)). Only the public data tier is read
([data visibility](/methodology/data-visibility)).

| Basis | Start price | End price |
|---|---|---|
| **live** (`market_hourly`) | median of the cloud's hourly lowest price over its first 24 priced hours inside the month's first 72 hours | median over its last 24 priced hours inside the month's last 72 hours |
| **archive** (`archived_prices`) | the price in force at the month's start: the last archived document before it, if at most 45 days old (an AWS price-list version counts while it is still valid) | the last archived document dated inside the month |

The archive basis is used only for a cloud and GPU that OpenGrid had not recorded live by the month's end; a cloud's
move is never part live, part archived. A cloud priced at only one end of the month (it joined, left, sold out, or
its feed failed) is **left out** and counted in the GPU's reasons; it is never filled in. The median over several
hours keeps a one-hour sell-out of a cloud's cheapest region from showing as a repricing.

**Marketplaces and decentralized networks** (provider classes `marketplace`, `decentralized`) are left out of every
cloud count: their prices are hosts' asks, not one company's list price (the same rule as
[shifts](/methodology/shifts)). The OpenGrid indices quoted in the headline still include them.

## How the clouds moved (per featured GPU)

Featured GPUs: H100 SXM, H200, B200, B300, A100 80GB SXM, L40S, MI300X, RTX 4090, RTX 5090.

- **move** = end price / start price - 1.
- **up / flat / down**: a move of at least **+1%** is up, at most **-1%** is down, anything smaller is flat. The
  threshold is a parameter (`generate(month, threshold=...)`), stated in every report's coverage block.
- **median move**, **middle half** (first and third quartiles, inclusive method, as in
  [dispersion](/methodology/dispersion)) and **full range** of the clouds' moves. These need at least **3 clouds**;
  below that they are null with the reason (the counts and the movers are still given).
- **biggest movers**: the five largest moves by size (provider, start → end price, %), ties by provider name.
- **matched median**: the median start price and median end price over the same clouds.
- **cheapest at month end** and **spread**: the cheapest cloud's end price, and how far it sits below the median
  cloud (`1 - cheapest / median`, median over every included cloud priced at month end, at least 3).
- **index**: the GPU's OpenGrid index level (see [indices](/methodology/indices)) at its first published hour in
  the month's first 72 hours and its last published hour in the last 72 hours. No change is given when the chain
  was rebased in between (levels in different chain segments are not comparable), or when it was not published.

### Dispersion and proportionality

All in percentage points (pp) of the moves:

| Measure | Definition |
|---|---|
| `mad_pp` | median absolute deviation of the clouds' moves from the median move |
| `iqr_pp` | width of the middle half (q3 - q1) |
| `movers_spread_pp` | largest minus smallest move among the clouds that moved (2 movers or more) |

**Pattern** (at least 3 clouds):

| Pattern | When |
|---|---|
| `no_moves` | no cloud moved by the threshold |
| `proportional` | at least one cloud moved and **every** cloud's move is within **2 pp** of the median move (a market-wide repricing by about the same percentage) |
| `few_movers` | not proportional, and fewer than half of the clouds moved |
| `broad_uneven` | not proportional, and half or more moved, by different amounts |

A cloud that stayed flat while others moved breaks proportionality: "proportional" means the whole market moved
together.

## Within one cloud

For every cloud that repriced (moved by the threshold) **two or more GPUs** in the month, over every GPU it is
priced for (not only the featured ones):

| Class | When |
|---|---|
| `uniform` | every repriced GPU moved in the same direction, and the largest and smallest moves are within **2 pp** |
| `same_direction` | same direction, by different amounts (more than 2 pp apart) |
| `mixed` | some up, some down |

The report gives the counts and shares of each class. GPUs the cloud left unchanged are not repricings and do not
enter its class.

## Generations

Every GPU with at least 3 clouds is grouped by its hardware generation (`hardware.py`; a non-datacenter part is its
own group, e.g. "Blackwell (consumer)"), newest first. Per group: the GPUs, the pooled up / flat / down counts, the
**median of the GPUs' median moves** (each GPU counts once) and the pooled median over every cloud move. This shows
whether older GPUs moved differently from flagships.

## Supply

From the providers' own capacity feeds ([capacity](/methodology/capacity); public tier) for the featured GPUs:

- **sold out**: a cloud's SKU and region went from in stock (or a known non-sold-out state) to sold out in the month;
- **back in stock**: from sold out to in stock.

A SKU seen for the first time is not a transition. Only about 32 providers report capacity, so these are counts of
reported changes, not of all supply. **Started / stopped listing**: the month's `provider_added_gpu` /
`provider_removed_gpu` [events](/methodology/events) (real catalogue changes, never coverage artefacts). Neither
exists before live recording.

## Events

The month's [market events](/methodology/events) (`coverage_started` excluded): totals by type and severity, and
the notable and major ones (up to 25, major first). Events say what changed, never why.

## Coverage and caveats

Every report states its basis (`live`, `archive`, `mixed` or `none`), the clouds counted, the cloud × GPU prices per
basis, the providers left out as marketplaces, the thresholds, the data kinds and when live recording began.
Missing data stays missing, with the reason.

- These are **list prices**, not transactions.
- Only the start and the end of the month are compared: a cut and a restore inside the month do not show.
- Archived prices exist only on the days a capture or a price-list version exists; clouds without a document at the
  month's start and inside the month are not counted, so archive months cover fewer clouds.
- OpenGrid indices start with live recording; archive months have no index levels.

## Public-safe summary

Four to eight plain sentences for a blog post, plus one methodology line. Only headline numbers, **rounded**: index
points to a whole number, the per-GPU index in dollars to 10 cents, percentages to a whole number, counts ("2 of 6
clouds raised H100 prices"). It never names a provider and never gives one cloud's price. A month without data says
so in one sentence. Tests check every provider name and every number in it.

## Where it goes

- Stored once per month in `monthly_reports` (migration `0039_monthly_reports`).
- `GET /v1/reports/monthly`, `GET /v1/reports/monthly/{YYYY-MM}` (`data:read`, not anonymous);
  `POST /v1/reports/monthly/{YYYY-MM}/regenerate` (admin).
- Page `/reports` (not indexed, not in the sitemap) with a "Copy public summary" button.
- A compact embed in the internal `#opengrid-market` Discord channel, posted once per month.
- CLI: `python -m reports.monthly generate --month 2026-10 [--force]`, `backfill --from 2024-01`, `show --month`.
