# Historical context and percentiles — methodology

Code: `analytics/stats.py`. API: `/v1/markets/{gpu}/context`,
`/v1/providers/{provider}/gpus/{gpu}/context`, `/v1/providers/{provider}/listings/context`,
`/v1/history/{gpu}`.

These statistics answer "is this price cheap or expensive **compared with its own recorded
history**". They are descriptive: they say nothing about where prices will go. All inputs are
observed market prices (normalized list prices, USD per GPU-hour).

## Samples

Every statistic uses the hourly rollup `market_hourly`: the state of each provider's eligible
listings at the top of each hour, sampled with the market view's rules (see
[indices](/methodology/indices) §2). A 30-day window is the 720 hourly samples in
`(now − 30 days, now]`, where "now" is the newest rollup hour; a 90-day window is 2,160.

| Subject | Series |
|---|---|
| GPU, market lowest | each hour, the lowest provider price (one vote per provider: its lowest eligible listing) |
| GPU, market median | each hour, the median of those provider prices |
| Provider + GPU | each hour, that provider's lowest eligible listing |
| Listing | each hour, that listing's price (live, priced, not sold out) |

An hour with no price (nobody selling, sold out, not yet recorded) is not a sample.

### The panel rule (market-level windows)

The market's lowest price changes when we start recording a new provider, not only when prices
move. Comparing today's lowest (which includes a provider added last week) with a 90-day history
that never included it would make the market look artificially cheap. So for each window, the
market series is computed only over providers **recorded since the window began** (the
window's *panel*); providers that started being recorded later are listed in
`excluded_recent_providers`, and the window's `current` is the panel's current value. The
headline `current` (all providers now) is reported separately.

## Percentile

Mid-rank percentile of the current value among the window's samples (the current hour included):

```
percentile = 100 × (samples below current + ½ × samples equal to current) / samples
```

Low = cheap relative to the window. A constant series puts every value at the 50th percentile.

## Coverage gate

`coverage = samples / hours in window`. A percentile, a distance from the window median and a
label are emitted **only when coverage ≥ 70%**. Otherwise all three are null and `reason`
states the shortfall (e.g. `insufficient history: 240 of 720 hourly samples (33%); 70% required`).
Samples and coverage are always reported so the gap is visible.

## Labels

| Percentile | Label |
|---|---|
| ≤ 10 | very cheap |
| ≤ 30 | cheap |
| < 70 | normal |
| < 90 | expensive |
| ≥ 90 | very expensive |

The headline label uses the 90-day window when it passes the coverage gate, else the 30-day
window, else none (with `label_reason`). For a GPU, the headline label is the market median's.

## Other figures

- **Distance from 30-day median**: `(current − median₃₀) / median₃₀`, coverage-gated as above.
- **Historical low / high**: the lowest and highest sample of the series over everything
  recorded (for a listing: the last 90 days), with the hour and `since` (the first sample).
  Distance: `(current − low) / low` and `(current − high) / high`. These are not
  coverage-gated because they are stated with `since`, but they need at least 24 hourly samples
  (otherwise null with the reason); for market series they use all providers, not a panel.
- **Provider vs market**: a provider's current price relative to the current market median.

## Summaries

Each response carries `summaries`: plain sentences generated only from the numbers in the same
response, e.g. "H100 80GB SXM5 market median price is in the 18th percentile of its 90-day range
(cheap)." or "Lambda H100 80GB SXM5 is 12% below its own 30-day median ($2.49/GPU-hr)." When
nothing passes the coverage gate the summary says so rather than characterising the price.

## History series (`/v1/history/{gpu}`)

Hourly (or daily) lowest / median / highest provider price and provider count — the plain
cross-section, one vote per provider — together with the chain-linked index for the same GPU.
The cross-section can move when a provider starts being recorded, so the response lists
`providers_joined` (first recorded hour of each provider in the window) and `coherent_from`
(the first hour from which every provider selling now was already recorded, as in the market
view). Daily points: lowest = min of the hourly lowest, median = median of the hourly medians,
highest = max of the hourly highest, providers = max hourly provider count.
