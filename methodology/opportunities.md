# Opportunity monitor

`GET /v1/opportunities` lists places where a buyer might currently get a better deal than usual. It is
computed live from the hourly rollup (`market_hourly`), the detector's cross-provider table
(`market_gpu_hourly`), current listings and recent events — nothing is stored — and cached 60 seconds.
Code: `analytics/opportunities.py`.

These are **observations about listed prices** (observed market prices), not advice, not quotes and
not execution prices. A listing can sell out or change before anyone acts on it, and nothing here
claims to know why a price moved.

## Item shape

`type`, `score` (0-100; higher = larger or more unusual; capped), `explanation` (one sentence), `numbers`
(everything the explanation uses), `gpu`, `gpu_slug`, `provider`, `region_group`, `kind`
(`observed` = a direct comparison of observed prices / counts; `inferred` = a statistic against a
baseline or rule), `links` (the events feed for the GPU and provider). Items are sorted by score.
`unavailable` lists types that could not be evaluated, with the reason (usually not enough history).

## Coverage rules

As for [market events](/methodology/events): comparisons over time use providers recorded at both
times; market-level figures (median, spread, cheapest) use only providers *established* for that GPU
(recorded >= 24h, or a genuine addition), so a provider we just started recording is never an
"opportunity" merely because it appeared.

## Types

| Type | Rule | Score | Kind |
|---|---|---|---|
| `wide_spread` | Spread (highest / lowest - 1, >= 3 established providers) more than 10% above the GPU's own 30-day p90 (needs >= 168 hourly values); without that baseline only spreads >= 100%, labelled "no baseline" | 50 x spread / p90 (or 25 x spread) | inferred |
| `price_cut` | A provider's lowest price is >= 5% (and >= $0.001) below its price 24h ago, recorded at both times | 200 x cut | observed |
| `new_cheap_inventory` | A listing first seen in the last 24h, still live, purchasable, priced below the GPU's current market median (>= 2 established providers); providers in their first day of coverage are excluded | 150 x discount | observed |
| `regional_dislocation` | A region group's median provider price >= 20% away from the global median (>= 2 providers inside and outside); groups from `regions.region_group`, never guessed | 150 x gap | inferred |
| `below_usual_premium` | Provider's premium now (price / market median - 1) is >= 10 points below its 30-day average premium (hours with >= 2 priced providers; needs >= 168 samples) | 200 x points | inferred |
| `scarcity` | Available listings now <= 50% of the GPU's 30-day hourly average (average >= 2; needs >= 168 samples) | 100 x (1 - ratio) | inferred |
| `new_cheapest_provider` | A `new_cheapest_provider` event in the last 24h whose provider is still the cheapest | 40 + 200 x drop | observed |
| `supply_change` | Providers with purchasable listings changed by >= 2, or available listings by >= 50% (from >= 4), vs 24h ago, over providers recorded at both times | 20 x providers + 50 x relative change | observed |
