# Major shifts: methodology

A major shift is a GPU price or capacity move that is large against its own history. For each one, OpenGrid
lists the news, provider announcements, SEC filings and its own market events published near it, and stores that
list so it reads the same next month. **The list is headed "Related news and events around this shift (not
necessarily causal)". Being near a shift in time and about the same GPU, provider or vendor is all an item shows.
OpenGrid never says an item caused a shift.**

Code: `analytics/shifts.py`. Tables: `market_shifts`, `market_shift_items`. API: `GET /v1/shifts`,
`GET /v1/shifts/{shift_id}`, `GET /v1/shifts/definition`. Shown on each GPU page (section "Major shifts" and a
"Shifts" chart overlay) and under the news timeline.

## What counts as a major shift

Every rule reads numbers OpenGrid already publishes, on the on-demand segment. Definition version 1.

| Type | Data kind | Fires when |
|---|---|---|
| `market_median` | observed | A GPU's coverage-matched 24h median change (`market_gpu_hourly.chg24_median`, only providers priced at both times; see [events](events)) is at least **max(10%, 3 × its trailing RMS 24h change)**, with at least 2 providers priced. |
| `index_move` | observed | A composite index (`gpu-compute`, `*-class`; see [indices](indices)) moves at least **max(5%, 3 × its trailing RMS)** in 24h within one chain segment. |
| `provider_repricing` | observed | One provider's own lowest price moved the same way on **at least 3 GPUs within 24h** (market events `price_move` with cause `price_change`). Marketplaces and decentralized networks are excluded: their rows are host asks, not one provider's price list. |
| `capacity_collapse` | observed | A market-wide `market_capacity` tightening event at severity major, or a GPU sold out at every tracked provider when at least 2 providers carried it. Repeats within 3 days are one episode. |
| `archived_price_change` | archived | A provider's lowest archived list price for a GPU differs by **at least 15%** between two consecutive archived documents. |
| `archived_provider_repricing` | archived | At least 3 GPUs of one provider changed at least 15% the same way between the same two documents. Their per-GPU changes are folded into this one shift. |

**Its own volatility.** The trailing RMS is the root mean square of the series' hourly 24h changes over the 30 days
before the move, excluding the move's own last 24 hours, so a jump never raises its own bar. A series needs at least
7 days of baseline (168 hourly values) before it can produce a shift. With less, nothing is judged. A quiet series
meets the floor (10% for a GPU median). A noisy one, such as a GPU priced mainly by marketplaces, must move further.

**Episodes.** Consecutive firing hours of the same sign, with gaps under 24 hours, are one shift. It is measured at
its largest hour and keyed on the episode's first hour (`shift_key`), so re-running the detector updates the shift
instead of adding another. Detection is a pure function of stored data: the same history gives the same shifts.

**Severity.** `major` at 2 × the threshold or 25% (for a repricing, 6 or more GPUs or a 25% median change). Otherwise `notable`.

## Archived shifts

Archived shifts compare list prices recovered from public archives (see [archived history](archived-history)). They are
labelled `archived` everywhere and differ from observed shifts in three ways:

- **Only before live coverage.** A (provider, GPU) pair is read from the archive only up to the first hour
  OpenGrid recorded it live. After that the observed shifts cover it, and archived samples are never mixed with
  observed data.
- **Placed between two captures.** The change happened somewhere between the two archived documents. The move is
  placed between them, capped at 60 days before the later one. Related items are read from 7 days before the
  earlier capture to 7 days after the later one.
- **Per provider.** There is no archived market median, because the set of providers captured changes from month
  to month (see archived history). Each archived shift compares one provider with itself.

Archived prices are never used in percentiles, indices, events, routing or alerts. Archived shifts are no exception.

## Related items

For each shift, OpenGrid reads candidates in its window. For observed shifts the window runs from **72 hours before
the move began** (the 24h change started a day before its first firing hour) **to 48 hours after its largest hour**:

- **news** about the GPU (its family or variant, as classified in [news](news)) or the provider, with relevance at
  least 20;
- **provider announcements**: official items from provider, hyperscaler and vendor sources;
- **SEC filings** (EDGAR): the GPU vendor's filings (NVIDIA, AMD, Intel) and the provider's own filings
  (CoreWeave, Microsoft, Alphabet, Amazon, Oracle, Nebius, DigitalOcean, and Form D filings of private GPU clouds);
- **market-wide** items: relevance at least 45 on export controls, price changes, pricing reports, supply deals,
  futures or capex, whatever entity they name;
- **OpenGrid market events** (notable or major) for the GPU or provider, or market-wide. The events that make up the
  shift itself are left out.

Backfilled news (recovered from archives; see [news history](news-history)) is used like live news and is marked
`backfilled` with where it was recovered from.

**Ranking.** `score = 0.5 × base + 0.3 × proximity + 0.2 × match weight`.

- The base is the item's relevance / 100 for news, or 1.0 / 0.7 for a major / notable market event.
- Proximity is 1 inside the move and falls linearly to 0 at the window edges.
- The match weight is 1.0 for the GPU or the provider, 0.7 for the vendor and 0.4 for a market-wide item.

At most 25 items are kept per shift, of which at most 6 are OpenGrid's own events, so outside items are never crowded out. Every
stored item keeps its score components, why it matched, and a snapshot of what was shown (title, link, source,
time, backfilled).

**Stable, and re-computable.** Items are recomputed while the window is still open and for 3 days after it closes
(late news arrives). After that they are frozen unless the shift itself changes, the definition or ranking version
changes, or an operator rebuilds them (`python -m analytics.shifts rebuild-items`). The same stored data always gives
the same ranking.

## Running it

The `market_shifts` job runs every 15 minutes and re-detects the last 10 days. `python -m analytics.shifts run`
detects over all history, archived included. `python -m analytics.shifts list` prints shifts with their top items.

## Limits

- A shift is a statistical description of OpenGrid's data. It is not a claim about what any buyer paid.
- An item near a shift is not necessarily causal. Unrelated news can sit next to a move by chance, and the real
  driver of a move may never be published.
- Thresholds are fixed and stated. A move just under a threshold is not a shift. Changing a threshold means a new definition version.
