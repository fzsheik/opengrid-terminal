# Market events and the market overview

OpenGrid turns its hourly market record into **structured events** ("Lambda cut NVIDIA H100 80GB SXM5
by 12.4% to $2.49/hr") and a homepage **overview** (movers, scarcity, unusual moves). Code:
`analytics/events.py`, `analytics/movers.py`. Endpoints: `GET /v1/events`, `/v1/events/types`,
`/v1/overview`, `/v1/tape`.

All prices are **observed market prices**: providers' list prices as normalized, per GPU-hour,
on-demand segment (market eligibility as in `market.py`: canonical GPU, on-demand, not interruptible,
not Vast's "cheapest" row, not a "from $x" floor; a sold-out listing is not a price). Events describe
what changed; they never say why. No event is a quote or an execution price.

## Source data

- `market_hourly` (the hourly rollup): each provider's listings sampled at the top of every hour,
  with market.py's rules. A provider's price for a GPU at an hour is the lowest price among its live,
  purchasable listings (one vote per provider).
- `market_gpu_hourly` (written by the detector): per GPU and hour, the cross-provider lowest / median /
  highest over every provider recorded that hour, listing counts, and **coverage-matched changes**
  (`chg1h_median`, `chg24_median`, `chg24_lowest`): computed only over providers priced at both
  times, so a provider we began recording in between cannot move them.
- `raw_snapshots` for provider feed health (fetch success/failure) and coverage start.
- `compute_listings.first_seen_at` to tell a genuinely new listing from a newly mapped one.

## Coverage rules (why a new provider is not a market move)

- **Coverage start** of a provider = the earlier of its first successful fetch and its first rollup hour.
- A (GPU, provider) pair whose first hour is within **2 hours** of the provider's coverage start was
  already on sale when we started looking. That is `coverage_started`, never `provider_added_gpu`.
- A pair is **established** at hour *h* once it has been recorded for **24 hours**, or immediately if it
  is a genuine addition (first hour > coverage start + 2h, and its listings were first seen at that time —
  not long before, which would mean a mapping fix or a rebuilt rollup).
- Market-level comparisons between two hours (cheapest provider, spread, market sold out, regional gaps)
  use **one provider set**: the providers established at the later hour, evaluated at both hours.
- 30-day and all-time records compare the current value (established providers) against history **as
  recorded** (every provider recorded at each past hour), so a provider entering the established set
  cannot create a false record: its own recorded hours are already in the history.

## Detection cadence, idempotency, backfill

- The detector runs after every rollup refresh (`rollups.AFTER_REFRESH`) and as job `market_events`
  every 15 minutes (a run already in progress is skipped, never doubled).
- A watermark per segment (`event_detector_state`) records the last processed hour. Each run redoes the
  last **3 hours** (the rollup rewrites them) and then the new ones.
- First run: the cross-provider table is backfilled over all rollup history; events over the last
  **90 days**. Coverage starts older than that are not emitted.
- Every event has a `dedupe_key` (type, segment, GPU, provider, region group, qualifier, hour); writes
  are insert-or-ignore, so running twice or over overlapping hours writes nothing new. Cooldowns are
  evaluated against events strictly before the hour, so a re-run makes the same decisions.
- Events are hourly: a change is reported at the first top-of-hour sample that shows it (latency up to
  ~1 hour plus the rollup's 10-minute refresh).

## Event catalogue

`kind` is the data kind (see [data kinds](/methodology/data-kinds)). Percentages are fractions in the API
(`pct = -0.124` is -12.4%).

| Type | Fires when | Severity | Kind |
|---|---|---|---|
| `price_move` (provider) | Provider's lowest price moved >= 10% and >= $0.001 vs the previous hour (`basis: hour`), or vs 24h ago (`basis: 24h`, at most once per 24h per provider+GPU, suppressed after an hourly move). **Marketplace providers** (see below): the move must hold for 2 consecutive hourly samples | info < 15%, notable >= 15%, major >= 25%; marketplace providers capped at notable unless > 40% | observed |
| `price_move` (market) | Coverage-matched market median moved >= 5% in 24h (>= 2 matched providers; once per 24h per GPU) | notable >= 5%, major >= 10% | observed |
| `sold_out` | Every live listing a provider has for a GPU went sold out (was purchasable the hour before). `provider = null`: every established provider live for that GPU is sold out | info; notable if it was the cheapest; major market-wide | observed |
| `capacity_returned` | A sold-out provider (or market) has purchasable listings again | info; notable if now cheapest, or market-wide | observed |
| `provider_added_gpu` | A provider tracked for >= 2h, whose feed was live the hour before, has live listings for a GPU it did not have, and one of them was first seen within the last ~3h | info; notable if it undercuts every established provider | inferred |
| `provider_removed_gpu` | A provider's listings for a GPU were gone for 2 consecutive hours while the provider kept reporting other listings or successful fetches, and the listings still exist in our records (not renamed by a mapping change) | info; notable if it was the cheapest or only priced provider | inferred |
| `new_cheapest_provider` | Among one fixed set of established providers, the cheapest changed and the new one is >= 0.5% below the old one's current price (or the old one is no longer purchasable: `reason`). At most once per 6h per GPU | info; notable if the new floor is >= 5% below the old | observed |
| `new_30d_low` / `new_30d_high` | Market lowest (metric `lowest`) or median (`median`, >= 2 providers) beyond every hour of the previous 30 days by >= 0.5%. Requires the GPU's history to start >= 30 days ago **and** >= 80% of the 720 hours priced. Once per 24h per GPU+metric | lowest: notable; median: info | observed |
| `new_all_time_low` / `new_all_time_high` | Market lowest beyond every hour **since tracking began** (date in the title) by >= 0.5%; requires >= 14 days of history. Supersedes the 30-day event on the same hour | major | observed |
| `spread_anomaly` | Spread = highest / lowest - 1 over >= 3 established providers rises above its own 30-day p95 x 1.25 (needs >= 14 days and >= 168 hourly values), or above 300% when there is no such baseline; must cross from at-or-below. Once per 24h per GPU | notable; major at >= 1.5x the threshold | inferred |
| `regional_dislocation` | A region group's median provider price (each provider's lowest in that group) crosses >= 20% away from the global median; >= 2 providers inside and >= 2 outside the group. Region groups come from `regions.region_group` and are never guessed (unknown regions are left out; if the module is unavailable the event is skipped) | info; notable at >= 35% | inferred |
| `market_capacity` | >= max(3, 20% of live (GPU, provider) markets) went sold out (`direction: tightening`) or came back (`loosening`) within 24h. Once per 24h per direction | notable; major at >= 40% | inferred |
| `provider_feed_down` | 3 consecutive poll rounds of a provider failed outright (no successful response in the round), after the feed had worked. A round = fetch rows within min(120s, half the poll interval) | notable | observed |
| `provider_feed_recovered` | First successful round after a `provider_feed_down` streak; `detail.outage_seconds` | info | observed |
| `coverage_started` | OpenGrid began recording a provider (list of GPUs it already sold). Not a market event | info | observed |

Each event's `detail` holds the numbers behind it (previous and new prices, listing counts, thresholds,
the history length used). `value_before`, `value_after` and `pct` are filled where meaningful.

### Marketplace providers: persistence before a provider price move

A provider whose `provider_meta` class is `marketplace` (Vast.ai, Hyperbolic) is many independent hosts
setting their own asks; its row is a statistic over those asks (Vast: the median of host asks), which
swings hour to hour as hosts come and go without anyone "changing a price". For these providers a
provider-level `price_move`:

- **hour basis** fires at hour H only when the price at both H-1 and H differs from the price at H-2 by
  >= 10% (and >= $0.001) **in the same direction** — the move held for 2 consecutive hourly samples. The
  event reports H-2 -> H (`since` = H-2) and `detail.persistence = {held_hours: 2, samples: [H-1, H]}`.
  The H-2 level must not itself be a fresh >= 10% jump from H-3, so the reversion of a one-hour spike
  is not reported as a held move either;
- **24h basis** fires only when both H-1 and H are >= 10% away from the 24h-ago price, same direction;
- the title ends with "(held 2h)" and `detail.provider_class = "marketplace"`;
- severity is capped at **notable** unless the move exceeds **40%**.

A one-hour swing that reverts never fires. Idempotency is unchanged: the event is a pure function of the
rollup rows, keyed by the same dedupe key. Market-level moves are unaffected (they are coverage-matched
medians across providers).

## Known limits

- A listing that disappears and later returns under the same id is treated as live across the gap
  (the pipeline keeps no gone-row; see `analytics/rollups.py`), so such gaps produce no removal/add.
- Price moves of a provider's *lowest* price can come from its listing mix changing (a cheaper listing
  sold out or appeared); `detail.cause` is `listing_mix` then, and the title says "lowest price".
- Only the on-demand segment is scanned for events.

<a id="overview"></a>
## Market overview (`GET /v1/overview`)

One payload for the homepage, cached 60 seconds; every section is `{available, reason, kind, items}`.
An unavailable section carries the reason (e.g. "needs 24h of history; 6h recorded"), never zeros.
"Now" is the latest rollup hour.

| Section | Definition |
|---|---|
| `board` | Every GPU priced now: providers, lowest (and who), median, highest, matched 24h changes |
| `gainers` / `losers` | Matched 24h change of the median provider price; >= 2 providers priced at both times; one vote per provider. `excluded_providers` lists those not in both samples |
| `volatile` | Population stdev of the matched hourly median change over 7 days; needs >= 72 hourly values. Plus the 7-day median range |
| `liquid` | Providers with purchasable listings, then available listings (score = providers available x available listings) |
| `availability_gaining` / `_losing` | Available listings and providers with purchasable listings, now vs 24h ago, over providers recorded at both times |
| `newly_available` | GPUs first priced in the last 7 days whose first (GPU, provider) pair is a genuine addition (not the start of our coverage) |
| `sold_out` | GPUs with live listings, at least one sold out, and nothing purchasable at any provider |
| `unusual` | z = (today's matched 24h median change - mean) / stdev of the same statistic on each of the previous 30 days (same hour); needs >= 14 daily samples; flagged at abs(z) >= 2 |
| `price_changes` | Per-listing price changes in the last 24h from `listing_observations`, above `normalize.classify_change`'s noise floor (0.5% and $0.001) |
| `capacity` | Live markets, sold-out markets now, GPUs sold out everywhere, sold-out / returned events in 24h, available listings and their 24h change |
| `tape` | Latest price moves and notable / major events (same as `GET /v1/tape`) |
