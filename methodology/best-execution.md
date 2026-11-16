# Best execution: how OpenGrid ranks where a workload should run

Version `routing-1.1` (1.1, 2026-11-16: availability reads the provider stock feeds; see "Availability and the
stock feeds" below). Code: `routing/scoring.py` (`rank_listings`). Endpoint: `GET /v1/best/{gpu}`
("Best Available Now"); the same ranking drives `POST /v1/route/preview` and `POST /v1/route`.

The ranking is a **transparent weighted model, not "cheapest wins"**. Every candidate is returned with
its total score, each factor's raw value, normalized value, weight and contribution, the data kind of
each factor, and a plain-language explanation. Every listing that was *not* a candidate is returned in
`exclusions` with a reason.

## Candidates

A candidate is a current `compute_listings` row for the requested canonical GPU that:

1. passes the market eligibility rules in `market.py` (canonical GPU, on-demand, not interruptible,
   not Vast's single-host "cheapest" row, not a "from $x" floor price);
2. has a price (> 0) and is not sold out (`available` is not false, and, when the listing does not state its
   stock, no fresh joined stock-feed row says out of stock: code `sold_out_stock_feed`);
3. was seen within `stale_after(provider)` (2.5 polling intervals, at least 10 minutes);
4. has exactly `count` GPUs per instance (see below);
5. is at or under `max_price_per_gpu_hour`, if given;
6. is not known to be outside the requested region group (if given); with `strict_region` it must
   also be confirmed inside it (see [routing](/methodology/routing));
7. satisfies request preferences (excluded/included providers, minimum integration level,
   explicit availability required).

**`count` is GPUs per instance.** A route launches exactly one instance. Shapes whose GPU count
divides `count` (e.g. 1-GPU instances for a request of 8) are scored and reported separately as
`multi_instance_alternatives` with the number of instances needed; they are never provisioned
automatically. Shapes that cannot make up `count` are excluded (`wrong_count`).

Exclusion codes: `not_on_demand`, `single_host_ask`, `floor_price`, `no_price`, `sold_out`, `sold_out_stock_feed`, `stale`,
`wrong_count`, `over_max_price`, `wrong_region`, `region_unconfirmed` (strict region only), `excluded_by_preference`, `below_required_level`,
`availability_unknown`. Listings whose GPU name is unmapped have no canonical GPU and therefore never
appear for any GPU (see `/unmapped`); OpenGrid does not guess mappings.

## Market reference

`market.median` / `market.low`: for each provider, its lowest current eligible, fresh, priced,
not-sold-out per-GPU-hour price (any instance shape); then the median / minimum across providers,
the same rule as the market view. These are **observed market prices**.

## Factors

Each factor is normalized to 0..1.

| Factor | Kind | Definition |
|---|---|---|
| `price` | observed | `clamp(0.5 − (price − median) / median)`: 0.5 at the market median, 0.6 at 10% below, 1.0 at ≥50% below, 0 at ≥50% above. |
| `availability_now` | observed | Listing says available: 1.0 (labelled *explicit*, or *derived* where the quality module marks the provider's flag as derived). Listing silent but a fresh stock-feed row joined to it says in stock: 1.0 ("in stock now (provider stock feed)"). Unknown: 0.5 (the note says when the joined feed row is stale). Sold out, by the listing or by a fresh joined feed row, is excluded, never scored. |
| `region_match` | observed | Only when a region group is requested: listing located in the group 1.0; location unknown 0.5 ("not confirmed"); known to be elsewhere → excluded. Region groups come from `regions.py`, never guessed. |
| `freshness` | observed | 1.0 while the listing's last sighting is within one polling interval, falling linearly to 0 at `stale_after`. |
| `availability_persistence` | observed (stock feed) or inferred (fallback) | **Stock feed** (the provider reports capacity for this GPU, `methodology/capacity`): share of the last 30 days the provider was IN STOCK, = in-stock hours / hours with a stock answer (in + out), from `capacity_changes`. Uses the capacity rows joined to this listing when they join, else all of the provider's rows for this GPU (in stock when any is). Unknown hours (a row with no stock answer) and no-data hours (before the first row, after a "no longer reported" row, past the stale rule) are evidence of neither and are left out. Raw: the 7- and 30-day shares in stock, out, unknown and no data, hours covered, source and grain. Note: "in stock 11% of last 30 days (provider stock feed, ...)". **No stock feed** for this GPU: from `market_hourly`, hours from when OpenGrid first recorded the provider for this GPU (or 30 days ago) to the newest rollup hour; an hour in which a listing said available counts 1, an hour with only a priced listing of unknown stock counts 0.5, an hour with no live, not-sold-out listing counts 0. Note: "price listed 98% of last 30 days; no stock feed (a listed price of unknown stock counts half)". |
| `price_stability` | derived | `clamp(1 − CV / 0.25)` where CV is the coefficient of variation (population stdev / mean) of the provider's hourly lowest price over 30 days. |
| `integration_level` | registry | OpenGrid adapter level / 3 (`routing/capabilities.py`): can OpenGrid actually provision there? |
| `reliability` | transaction | The provider's reliability score / 100 (`methodology/reliability.md`): launch success × (1 − unexpected-termination share) × termination success, from OpenGrid's own customer launches. A provider below `reliability_min_samples` (10) launches has no score: null, imputed 0.5 when weighted, note "reliability unproven". |
| `performance` | n/a | **No data, not used.** Weight 0, value null. |

**Thin history.** Persistence and stability need at least 24 hourly samples. With less, the value is
null and the note says "insufficient". If a mode gives that factor weight, it scores a **neutral 0.5**
and is flagged `imputed: true`, never an invented number. Locally, history is hours deep, so expect
these factors to be imputed; production has weeks.

**Reliability** comes only from OpenGrid's own launches, never provider marketing. It is weighted only
once **at least one** provider has a score; until then it has weight 0 in every mode, `not_used` lists
it, a user weight on it is rejected, and rankings are exactly what they were without it. Once active it
is added (before normalization) at 0.10 in `FASTEST_AVAILABLE` and `BALANCED` and 0.20 in `MOST_STABLE`;
`CHEAPEST` stays price-only. Every ranking in a period uses the same weights (the switch is global, not
per GPU), so merged family rankings stay comparable. A provider without a score scores a neutral 0.5:
a proven provider above 50/100 ranks ahead of an unproven one, one with frequent interruptions behind it.
Scores are cached for 5 minutes. **Performance** has no data yet: weight 0, cannot be weighted.

## Availability and the stock feeds

Until 2026-11-16 (`routing-1.0`) availability history counted the hours a provider had a **price listed**. Many
providers keep their prices listed while sold out, so that measured the price page, not stock. Production, last 14
days, provider-wide: Verda had a price listed 100% of hours and was in stock 5% of the time; Hyperstack 99% vs 22%;
Lambda 100% vs 61%; Lium 100% vs 89%; RunPod 100% vs 100%. All of them scored about 1.0.

Since `routing-1.1` the provider stock feeds OpenGrid already stores (`capacity_current`, `capacity_changes`;
`/methodology/capacity`) drive both availability factors:

- **`availability_persistence`** is the in-stock share above whenever the provider reports capacity for the GPU
  (kind `observed`). Weights are unchanged.
- **Fallback, no stock feed** (kind `inferred`): a listed price is not stock, so an hour with only a price of unknown
  stock counts 0.5, the same credit `availability_now` gives unknown stock now, and an hour whose listing said
  available counts 1. Full credit for a listed price would let every provider without a stock feed outrank providers
  whose feeds honestly report sell-outs. A provider that publishes stock in its price feed keeps full credit.
- **`availability_now`**: a listing that does not state its stock takes the answer of a fresh joined stock-feed
  row: in stock scores 1.0, out of stock is excluded (`sold_out_stock_feed`). A listing's own `available` always
  wins. With `require_available`, a stock-feed "in stock" counts as explicit.
- **Stale rows** (not refreshed within 3 of the provider's capacity intervals, the `/v1/capacity` rule) never
  decide either factor now; their history past that point is no data.
- **Thin history**: under 24 hours with a stock answer, null, imputed 0.5 when weighted, as before.
- **Data tiers**: a public ranking reads public capacity rows only; `tier="all"` also reads internal ones.
- Rankings also carry `stock`, `stock_source`, `stock_observed_at`, `stock_feed`, `stock_rows_stale` and
  `in_stock_share_30d` per candidate, the same fields as `/v1/listings`.

What moves: a provider that is listed but mostly sold out loses most of its persistence credit (BALANCED: up to
-0.14 of score, MOST_STABLE: up to -0.43) and is excluded while its feed says sold out; one with a feed that says in
stock gains on `availability_now` (BALANCED +0.10 where its listing was silent); one with no stock feed and silent
listings drops from about 1.0 to about 0.5 on persistence (BALANCED -0.075). The market median (`market`) is
unchanged: it still follows the market view's rules.

## Modes and weights

Weights are normalized to sum to 1. When a region group is requested, `region_match` gets 0.10
(before normalization) in every mode except CHEAPEST; when none is requested it is unused.

| Mode | Weights | Order |
|---|---|---|
| `CHEAPEST` | price 1.0 | Lowest price; ties by freshness. |
| `FASTEST_AVAILABLE` | availability_now .35, integration_level .30, freshness .20, availability_persistence .10, price .05 (+ reliability .10 once active) | Score. |
| `BALANCED` (default) | price .40, availability_now .20, price_stability .15, availability_persistence .15, freshness .05, integration_level .05 (+ reliability .10 once active) | Score. |
| `MOST_STABLE` | availability_persistence .45, price_stability .25, availability_now .15, price .10, freshness .05 (+ reliability .20 once active) | Score. |
| `USER_DEFINED` | from the request's `weights` | Score. |

`USER_DEFINED` validation: keys must be factor names; values finite and ≥ 0; not all zero;
`performance` must be 0, and `reliability` too until it is active; `region_match` needs a region. `weights` with any other mode
is rejected. Ties in score are broken by lower price, then fresher data. A complete tie (same price and
data age; for CHEAPEST, same price and age) resolves to the alphabetically first provider, then listing id,
so the same market always produces the same decision (tests/test_routing_benchmark.py).

Total score = Σ weight × value. Each factor's `contribution` is reported, and they sum to the score.

## Explanations

Each candidate carries `explanation`, assembled from the factor notes, e.g.
"9% below current market median ($2.45); available now (explicit); region matched (US); fresh API data
(4 min old); in stock 94% of last 30 days (provider stock feed, this listing's rows, 712 h observed); OpenGrid can
provision via API (level 3)".
Each alternative carries `vs_selected`, e.g. "+4% more expensive but stronger availability history".
A factor counts as stronger/weaker when its normalized value differs by at least 0.1.

## What this is not

- Candidate prices are **observed market prices** (OpenGrid's normalized reading of list prices). A
  **quote** exists only for a specific route; an **execution price** only once a deployment runs.
- No provider uptime SLA or benchmark performance data is used; reliability is OpenGrid's own record only.
- The ranking does not imply OpenGrid can provision a candidate; `provisionable` and
  `integration_level` say whether it can.
