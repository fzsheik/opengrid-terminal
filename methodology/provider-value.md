# Provider relative value

Used by `/v1/providers`, `/v1/providers/{provider}`, `/v1/compare` (providers) and
`/v1/heatmaps/gpu-provider-*`. Code: `analytics/providers.py`; tables `structure_provider_daily`,
`structure_gpu_daily`. Data kind: **inferred**, from observed market prices in the hourly rollup.

## The comparison is always within the same hour

For a GPU and an hour H (top of the hour, from `market_hourly`, market.py's eligibility rules):

- `own(p, H)`: provider p's lowest eligible, priced, not-sold-out price that hour.
- `premium(p, H) = own(p, H) / median{ own(q, H) : q != p } - 1`, defined when at least one other
  provider is priced in H. The provider is excluded from its own benchmark.
- `rank(p, H) = 1 + number of providers strictly cheaper` (ties share the rank).

Window figures (default 30 days, by UTC day):

| figure | definition | minimum sample |
|---|---|---|
| premium_avg | mean of premium(p, H) over compared hours (p priced and >= 1 other priced) | 24 compared hours |
| cheapest_share | hours with rank 1 / market hours | 24 market hours |
| top3_share | hours with rank <= 3 / market hours | 24 market hours |
| rank_distribution | share of compared hours at each rank (10 = 10th or worse) | 24 compared hours |
| availability | hours with >= 1 priced listing / hours tracked | 24 tracked hours |
| explicit_availability | hours with >= 1 listing reported available (not unknown) / hours tracked | 24 tracked hours |
| volatility_daily | sample stdev of daily log changes of the provider's closing lowest price (consecutive days) | 7 changes |

- **Hours tracked** begin at the provider's first recorded hour for that GPU (`rollups.first_hours`). A
  provider OpenGrid began recording late is never penalised for the time before.
- **Market hours** are tracked hours in which at least one OTHER provider was priced. An hour in which the
  provider was absent (sold out, gone) while others sold counts against its cheapest share. Hours in which it
  was the only seller are not counted (no competition, nothing to win).
- Across all GPUs (`*`): premium, cheapest and rank sums add over (GPU, hour) pairs; availability counts an
  hour once if any GPU was priced; volatility is the median of the per-GPU volatilities.

Below a minimum the figure is `null` with a reason in `reasons`; nothing is extrapolated.

## Other provider facts

- **Listing lifetime** (`listing_lifetime`): median of `observed_at - first_seen_at` over the provider's
  eligible listings in `compute_listings`, labelled *observed lifetime*: first seen to last seen by OpenGrid.
  Listings still live are counted up to now, so it understates true lifetime. Needs >= 5 listings.
- **Coverage**: canonical GPUs live now, their architectures (from `hardware.py`), region groups.
- **Feed health** (from `raw_snapshots`, last 24 h): fetches, failure rate (`ok = false` / fetches), mean
  `duration_ms`, last successful fetch. It measures OpenGrid's reading of the provider, not the provider's uptime.
- **Beats the market / expensive**: GPUs sorted by window premium_avg where it meets the minimum, otherwise by
  the current premium; each item says which (`basis`).
- **Facts**: sentences are generated only from non-null figures above, e.g. "X has been the cheapest
  H100 80GB SXM5 provider 37% of recorded hours in the last 30 days."

Daily sums are recomputed from `market_hourly` after each rollup refresh (last two days incrementally),
so they cannot disagree with the rollup.
