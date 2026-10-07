# Price dispersion and market fragmentation

Used by `/v1/markets/{gpu}`, `/v1/spreads`, `/v1/gpus`, `/v1/gpus/{gpu}`, region heatmaps and
`/v1/heatmaps/gpu-time-*`. Code: `analytics/dispersion.py`, `analytics/providers.py` (daily sums),
`analytics/heatmaps.py`. Data kind: **inferred** (derived by stated rules from observed market prices).
Price concept: **observed market price** (normalized list prices), never quotes or execution prices.

## Which listings count

The same rules as the market view (`market.py`): canonical GPU, on-demand, not interruptible, not
Vast's "cheapest" row, not a "from $x" price; still live (seen within `stale_after`, about 2.5 polls);
priced (> 0) and not sold out. A listing with unknown availability counts as priced.

## One vote per provider

Each provider contributes **one** price per GPU: its lowest eligible price. A provider with 40 listings
does not outvote one with a single listing. Listing-level counts and stats are reported next to these,
separately (`listings`).

## Statistics (N = number of priced providers)

| field | definition | needs |
|---|---|---|
| low, median, high | of provider votes | N >= 1 |
| spread_abs | high - low (USD / GPU-hour) | N >= 1 |
| spread_pct_of_low | high / low - 1 | N >= 1 |
| spread_pct_of_median | (high - low) / median | N >= 1 |
| q1, q3, iqr | quartiles, inclusive method (Python `statistics.quantiles(method="inclusive")`); iqr = q3 - q1 | N >= 3 |
| iqr_rel | iqr / median | N >= 3 |
| stdev | sample standard deviation | N >= 3 |
| cv | stdev / mean | N >= 3 |

Below the threshold the field is `null` and `reasons` says why.

## Fragmentation / efficiency score (0-100)

Only when N >= 3. Higher fragmentation = providers disagree more on the price of the same GPU.

    cv_adj        = cv * (1 + 1 / (4N))            small-sample correction (CV is biased low for small N)
    c_cv          = min(cv_adj / 0.60, 1)
    c_iqr         = min(iqr_rel / 0.60, 1)
    c_range       = min(spread_pct_of_low / 3.00, 1)   saturates when the highest price is 4x the lowest
    fragmentation = 100 * (0.4 * c_cv + 0.4 * c_iqr + 0.2 * c_range)
    efficiency    = 100 - fragmentation

Labels: fragmentation < 25 **efficient**; < 50 **moderately fragmented**; otherwise **highly fragmented**.
`confidence` is `low` for N = 3-5 and `normal` for N >= 6. Every response carries the components,
weights and saturation points, so the number can be recomputed by hand.

The saturation points (0.60, 0.60, 3.00) and weights are editorial choices, set so that markets observed
in October 2026 spread across the range instead of all reading 100. They are constants in the code and
will be changed only with a note here.

Premium of one provider (current): `own lowest / median(other providers' lowest) - 1`, needs at least
one other priced provider. Rank: 1 + number of providers strictly cheaper (ties share a rank).

## Over time

From the hourly rollup (`market_hourly`, sampled at the top of each hour with the same rules):

- `resolution=1h` (<= 14 days): the statistics above recomputed for each hour.
- `resolution=1d`: per UTC day, from `structure_gpu_daily`: mean of hourly CV and IQR/median over hours
  with >= 3 providers (`cv_hours` says how many), mean of hourly high/low - 1 over hours with >= 2
  providers, and the closing low/median/high (last hour of the day with any priced provider).
- Daily volatility (`gpu-time-volatility`): sample stdev of hour-to-hour log changes of the market median
  within the day, only when >= 12 consecutive-hour changes exist.
- Daily availability (`gpu-time-availability`): provider-hours priced / provider-hours tracked, where a
  provider is tracked from its first recorded hour for that GPU onward.
- Daily change (`gpu-time-change`): closing median / previous day's closing median - 1 (consecutive days).

The number of providers changes over time; a provider joining changes dispersion, which is why every
point reports its provider count.

## Regions

Region groups come from `regions.region_group` (UN M49 based; see the module docstring). A listing
with no location, or one spanning several groups, is **Unassigned**, never guessed.
Regional premium = median of provider lowest prices within the group / global median - 1.
