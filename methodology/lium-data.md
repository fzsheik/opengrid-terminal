# Lium market data beyond prices

Besides its prices and fleet (the Lium listings and capacity, which are public), OpenGrid reads what Lium publishes
about its own market: completed rentals and their aggregates through Lium's data-partner feeds, and several keyless
feeds about launches, demand, utilisation and what Lium pays providers. Every value is stored raw first and kept in
its own tables (`lium_*`). None of it is a listing, and none of it enters indices, fixings, spreads, percentiles,
routing or reliability scores.

Code: `lium_data/`, tables `store/lium.py` (migration `0040_lium_data`). Reads are admin-only, under
`/v1/ops/lium/`.

## Tier: internal

Every row is **internal** ([data visibility](/methodology/data-visibility)). The data-partner feeds need a key of an
account Lium has flagged as a data partner, and the terms for passing that data on are not agreed yet. The keyless
feeds are open to read, but Lium's Terms of Service (23 Oct 2026) grant only a limited licence to use the Service,
and Lium's partner page names only `pricing.json` and the public nodes feed as feeds to list. Neither allows
republishing these, so OpenGrid keeps them and does not show them. A feed moves to public only after Lium says it
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

## Statistics OpenGrid computes

From stored trades, per UTC end day and GPU model: rentals, GPU-hours (GPUs x duration), the volume-weighted average
price (sum of price x GPU-hours over GPU-hours), the lowest and highest price, and the notional value. A rental counts
whole on the day it ended, which is the axis Lium's windows use. Only USD rows count.

## Turning the partner feeds on

The `lium_partner` job is registered only when `LIUM_PARTNER_API_KEY` is set. Until Lium flags the account, every
call answers 403. That answer is stored once and logged once, and the job then checks again only every 6 hours.
History comes from the nightly dumps: `python -m lium_data backfill --since YYYY-MM-DD` walks back from yesterday,
one day at a time, and skips days already stored. When it is given no `--since`, it stops after three empty days in
a row. A 403 stops it at once.
