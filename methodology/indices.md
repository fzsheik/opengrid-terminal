# OpenGrid market indices — methodology

Methodology version **1.0**. Code: `analytics/indices.py` (registry, math, storage). Stored in the
`index_levels` table, one row per index per hour. API: `/v1/indices`, `/v1/indices/{id}`,
`/v1/indices/{id}/history`.

Indices are built from **observed market prices** only: on-demand (or spot) list prices as
OpenGrid normalizes them, in USD per GPU-hour. They never include quotes, execution prices,
or any estimated number. See [data kinds](/methodology/data-kinds).

## 1. What the indices are

| Id pattern | Example | Unit | What it measures |
|---|---|---|---|
| `<gpu>` | `h100-80gb-sxm5` | USD / GPU-hr | On-demand price of one canonical GPU variant |
| `<gpu>.spot` | `h100-80gb-sxm5.spot` | USD / GPU-hr | Spot / **interruptible** capacity of that variant |
| `<gpu>.<region>` | `h100-80gb-sxm5.us` | USD / GPU-hr | On-demand, listings in one region group |
| `<gpu>.<class>` | `h100-80gb-sxm5.neocloud` | USD / GPU-hr | On-demand, providers of one class |
| `<family>-class` | `h100-class` | points (base 100) | Explicit composite of listed variants |
| `gpu-compute` | `gpu-compute` | points (base 100) | Composite of the benchmark GPU indices |

Ids are stable slugs. GPU slugs come from `api.common.gpu_slug` (`NVIDIA H100 80GB SXM5` →
`h100-80gb-sxm5`). Region slugs: `us, canada, europe, uk, apac, middle-east, latam, africa`.
Class slugs: `hyperscaler, neocloud, general-cloud, marketplace, decentralized`.

- **Per-variant indices are primary.** Every canonical GPU name gets an on-demand and a spot
  index. Materially different variants (SXM5 vs PCIe vs NVL, 40GB vs 80GB) are always separate
  indices and are never merged.
- **Regional and provider-class indices** exist for the benchmark GPUs: H100 80GB SXM5,
  H200 141GB SXM5, B200 180GB SXM, A100 80GB SXM4, L40S 48GB.
- **Spot indices** are labelled interruptible everywhere (`interruptible: true`): the provider
  can reclaim the capacity, so they are not comparable to on-demand prices.

## 2. Constituents (each hour)

The input is the hourly rollup `market_hourly` (see `analytics/rollups.py`): each provider's
eligible listings sampled at the top of each hour with exactly the market view's rules
(canonical GPU, on-demand and not interruptible for the on-demand segment, not Vast's
"cheapest" row, not a "from $x" floor, not sold out, and only while the listing was still being
seen — a listing that vanished stops counting after its provider's stale window).

1. **One vote per provider.** A provider's vote is its *lowest* priced eligible listing for the
   GPU that hour, across all its regions (for a regional index, across its regions in that
   group). A provider with 40 listings counts once, exactly like a provider with one.
   We use the lowest listing because it is the price a buyer can actually get from that
   provider; averaging a provider's listings would reward providers that list many expensive
   configurations.
2. **Failing feeds are excluded.** If every raw snapshot OpenGrid fetched from a provider in the
   hour before the sample failed (`raw_snapshots.ok = false`, no successful fetch), that
   provider's vote is excluded for that hour (`excluded_feed_down`). This complements the
   rollup's stale rule, which already drops listings that are no longer being seen.
   A provider with no snapshot at all in that hour is not treated as failing.
3. **Outliers are excluded.** With at least 3 candidate votes, a vote above 3× or below 1/3 of
   the candidates' median is excluded (`excluded_outlier`). The band is deliberately wide:
   prices for the same GPU legitimately differ by 2–3× between a marketplace and a hyperscaler,
   and the index should reflect that market. The band only removes values that are almost
   certainly data errors (a mis-normalized per-instance price, a placeholder price).
   Below 3 votes there is no meaningful median to screen against, and nothing is screened.
4. **Minimum coverage: 3 providers.** If fewer than 3 votes remain, the index is **not
   published** for that hour. The row is still stored with `published = false` and a reason
   such as `insufficient coverage: 2 eligible provider(s), 3 required`. We never publish a level
   computed from one or two providers.
5. **No availability or capacity weighting.** Every included provider has equal weight.
   Capacity is reported by only a few providers (and in different units), so weighting by it
   would silently weight by "who reports capacity". Sold-out listings are already not votes.

Each stored row keeps every candidate in `detail` with its price and status
(`included`, `excluded_outlier`, `excluded_feed_down`), so every published level can be audited.

## 3. The statistic

- Fewer than 5 constituents: the **median**.
- 5 or more: the **20% trimmed mean** — sort, drop `floor(0.2 × N)` from each end, average the
  rest (N = 5 drops one from each end; N = 10 drops two).

Why: with 3–4 providers a mean is dominated by any single provider, and the median is the only
robust choice. From 5 providers the trimmed mean uses more of the information (it moves when
the middle of the market moves, not only when the single middle provider moves) while still
ignoring the extremes. The method used each hour is stored in `method`.

`raw_level` is this statistic over all of the hour's constituents: the plain cross-section.

## 4. Chain-linking (why a provider joining does not move the index)

The set of providers changes: we start recording a new provider, a provider stops listing a GPU,
a feed breaks, a listing sells out. A plain cross-sectional median jumps whenever that set
changes, which would publish a market move that never happened (for example, the day a cheap
marketplace is added, the "H100 price" would appear to fall 10%).

So each index is **chain-linked**, the standard technique for indices whose constituents change:

```
common  = providers included at hour t AND at the previous published hour p
level_t = level_p × stat(prices_t over common) / stat(prices_p over common)
```

- The move from `p` to `t` is measured **only on providers present in both hours**, with the
  same statistic. A provider joining (or leaving) is not in the common set for that step and
  cannot move the index; from the next hour on it is part of the common set like any other.
  This is also why the index does not need `coherent_start`-style truncation: coherence is
  enforced at every step. (`rollups.first_hours` is reported as each constituent's
  `recorded_since` for transparency.)
- `p` is the previous **published** hour, so the chain links across unpublished gaps.
- **Base:** at the first published hour, the level equals the cross-sectional statistic, so
  per-GPU indices are in **USD per GPU-hour**.
- **Rebase:** if no constituent is in common with `p` (complete turnover), the chain cannot be
  linked. The level restarts at the cross-sectional value and `segment_no` increments, with the
  reason recorded. Levels in different segments are not comparable: changes, highs and lows are
  only computed within the current segment, and a change whose reference falls before a rebase
  is reported as null with the reason.
- `link_constituents` records how many providers each step was measured on.

**Level vs raw level.** Over time the chained level can drift away from the plain cross-section
(e.g. if cheaper providers join, the cross-section falls but the index does not, because no
price fell). The index answers "how have prices changed, like for like"; `raw_level` answers
"what is the middle of today's market". Both are published; neither is relabelled as the other.

## 5. Composite indices (base 100)

Composites combine child indices, never raw prices, and are published in **points, base 100**,
never as a price — a composite is not the price of any single SKU.

```
level_t = level_p × Σ w_i (child_i,t / child_i,p) / Σ w_i      over linkable children
```

- A child is linkable for the step if it is published at both `p` and `t` in the same chain
  segment (a child that rebased between them is skipped for that step).
- Published only if **at least 2 children** are published at `t` and they carry at least
  **50% of the total weight**; otherwise unpublished with the reason. (A "composite" of one
  published child would just be that child's index under another name.)
- Base 100 at the first published hour; rebased to 100 (new segment) if no child links.

**Family composites** (each lists its variants explicitly; each variant also remains its own index):

| Id | Name | Components (equal weights) |
|---|---|---|
| `h100-class` | OpenGrid H100 Class Index | H100 80GB SXM5, H100 80GB PCIe, H100 80GB PCIe NVLink, H100 94GB NVL |
| `h200-class` | OpenGrid H200 Class Index | H200 141GB SXM5, H200 143GB NVL |
| `a100-class` | OpenGrid A100 Class Index | A100 80GB SXM4, A100 80GB PCIe, A100 80GB PCIe NVLink, A100 40GB SXM4, A100 40GB PCIe |

**OpenGrid GPU Compute Index** (`gpu-compute`): the benchmark on-demand indices H100 80GB SXM5,
H200 141GB SXM5, B200 180GB SXM, A100 80GB SXM4 and L40S 48GB, **equally weighted (20% each)**.

Why equal, fixed weights: the honest alternative would be weights by traded volume or deployed
capacity, and OpenGrid does not observe either (most providers publish no capacity; none publish
volume). Any other weights would be a judgement presented as data. Equal weights make the
composite "the average price move across the benchmark GPUs", which is easy to state and
verify. Weights are part of the methodology version and change only with a new version.

## 6. Statistics published per index

All are computed from stored `index_levels` rows; "now" is the newest hour in the rollup.

- **Current level**: the level at the newest hour, if published. Otherwise null with the
  reason, plus `last_published` (hour and level).
- **Changes** 24h / 7d / 30d / 90d / YTD / all: `level_now / level_ref − 1`, where `level_ref`
  is the last published level at or before the window's start, at most a tolerance earlier
  (24h: 1h, 7d: 3h, 30d: 12h, 90d and YTD: 24h). YTD starts 1 January 00:00 UTC. "All" is
  measured from the first published hour of the current chain segment. A change is **null with
  a reason** when history does not reach the window start, when the index was not published
  near it, or when the chain was rebased in between. We never substitute a shorter window.
  "All" is null while the current segment covers a single published hour.
- **High / low**: the highest and lowest published level in the current chain segment, with
  their hours.
- **Realized volatility** 7d and 30d: the sample standard deviation of hourly log returns
  `ln(level_t / level_t−1)` over consecutive published hours in the same segment, annualized by
  `√8760`. Null with a reason unless returns exist for at least 70% of the window's hours.
- **Coverage**: first published hour, current chain segment and its start, constituents now,
  published hours in the last 30 days.

## 7. Computation and storage

- `index_levels` (index_id, hour) holds: `published`, `level`, `raw_level`, `constituents`,
  `link_constituents`, `method`, `segment_no`, `reason`, `detail` (each candidate and its
  status), `methodology_version`, `computed_at`. A row exists only for hours in which the index
  had at least one candidate.
- After each rollup refresh (`rollups.AFTER_REFRESH`), indices are recomputed incrementally
  from the last stored hour minus 3 hours (the rollup may revise its recent tail), continuing
  each chain from its last published row. The result is identical to a full recomputation.
- A full recomputation happens when the stored methodology version differs from the code's
  (the stored version is suffixed `+regions` when regional indices were computable), so a
  methodology change or the arrival of region grouping never leaves mixed-method history.
- Regional indices use `regions.region_group(provider, region, country)`. A region that cannot
  be placed in a group is not guessed; the listing simply does not vote in any regional index.
  If the region module is unavailable, regional indices report that reason.
- Provider classes come from `provider_meta.py`; unclassified providers are in no class index.

## 8. Known limits

- History is only as long as OpenGrid's recording. Early on, most windows report
  "history does not cover the window" — by design.
- A listing that disappears and later returns is treated as live across the gap by the rollup
  (the pipeline keeps no record of the gap). The feed-down rule covers the common cause
  (a failing feed); it cannot detect a provider silently delisting and relisting.
- The outlier band is fixed (3×) and does not adapt to dispersion.
- Indices describe advertised list prices, not what buyers paid.
