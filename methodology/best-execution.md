# Best execution: how OpenGrid ranks where a workload should run

Version `routing-1.0`. Code: `routing/scoring.py` (`rank_listings`). Endpoint: `GET /v1/best/{gpu}`
("Best Available Now"); the same ranking drives `POST /v1/route/preview` and `POST /v1/route`.

The ranking is a **transparent weighted model, not "cheapest wins"**. Every candidate is returned with
its total score, each factor's raw value, normalized value, weight and contribution, the data kind of
each factor, and a plain-language explanation. Every listing that was *not* a candidate is returned in
`exclusions` with a reason.

## Candidates

A candidate is a current `compute_listings` row for the requested canonical GPU that:

1. passes the market eligibility rules in `market.py` (canonical GPU, on-demand, not interruptible,
   not Vast's single-host "cheapest" row, not a "from $x" floor price);
2. has a price (> 0) and is not sold out (`available` is not false);
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

Exclusion codes: `not_on_demand`, `single_host_ask`, `floor_price`, `no_price`, `sold_out`, `stale`,
`wrong_count`, `over_max_price`, `wrong_region`, `region_unconfirmed` (strict region only), `excluded_by_preference`, `below_required_level`,
`availability_unknown`. Listings whose GPU name is unmapped have no canonical GPU and therefore never
appear for any GPU (see `/unmapped`); OpenGrid does not guess mappings.

## Market reference

`market.median` / `market.low`: for each provider, its lowest current eligible, fresh, priced,
not-sold-out per-GPU-hour price (any instance shape); then the median / minimum across providers —
the same rule as the market view. These are **observed market prices**.

## Factors

Each factor is normalized to 0..1.

| Factor | Kind | Definition |
|---|---|---|
| `price` | observed | `clamp(0.5 − (price − median) / median)`: 0.5 at the market median, 0.6 at 10% below, 1.0 at ≥50% below, 0 at ≥50% above. |
| `availability_now` | observed | Provider says available: 1.0 (labelled *explicit*, or *inferred* where the quality module marks the provider's flag as derived). Unknown: 0.5. Sold out is excluded, never scored. |
| `region_match` | observed | Only when a region group is requested: listing located in the group 1.0; location unknown 0.5 ("not confirmed"); known to be elsewhere → excluded. Region groups come from `regions.py`, never guessed. |
| `freshness` | observed | 1.0 while the listing's last sighting is within one polling interval, falling linearly to 0 at `stale_after`. |
| `availability_persistence` | inferred | Share of hourly samples in the last 30 days (from `market_hourly`) in which this provider had at least one priced, not-sold-out listing for this GPU. Denominator: hours from when OpenGrid first recorded the provider for this GPU (or 30 days ago, if later) to the newest rollup hour; an hour with no rollup row means no live listing and counts as unavailable. 7-day share is reported alongside. Per provider, collapsed over regions. Unknown-stock listings count as "priced availability". |
| `price_stability` | inferred | `clamp(1 − CV / 0.25)` where CV is the coefficient of variation (population stdev / mean) of the provider's hourly lowest price over 30 days. |
| `integration_level` | registry | OpenGrid adapter level / 3 (`routing/capabilities.py`): can OpenGrid actually provision there? |
| `reliability` | — | **No data — not used.** Weight 0, value null. |
| `performance` | — | **No data — not used.** Weight 0, value null. |

**Thin history.** Persistence and stability need at least 24 hourly samples. With less, the value is
null and the note says "insufficient". If a mode gives that factor weight, it scores a **neutral 0.5**
and is flagged `imputed: true` — never an invented number. Locally, history is hours deep, so expect
these factors to be imputed; production has weeks.

**Reliability and performance** will be computed only from OpenGrid's own transaction records
(`execution_records`: provisioning success, latency, uptime, interruptions) once enough exist. Until
then they are listed with weight 0 and cannot be weighted by users (a non-zero weight is rejected).

## Modes and weights

Weights are normalized to sum to 1. When a region group is requested, `region_match` gets 0.10
(before normalization) in every mode except CHEAPEST; when none is requested it is unused.

| Mode | Weights | Order |
|---|---|---|
| `CHEAPEST` | price 1.0 | Lowest price; ties by freshness. |
| `FASTEST_AVAILABLE` | availability_now .35, integration_level .30, freshness .20, availability_persistence .10, price .05 | Score. |
| `BALANCED` (default) | price .40, availability_now .20, price_stability .15, availability_persistence .15, freshness .05, integration_level .05 | Score. |
| `MOST_STABLE` | availability_persistence .45, price_stability .25, availability_now .15, price .10, freshness .05 | Score. |
| `USER_DEFINED` | from the request's `weights` | Score. |

`USER_DEFINED` validation: keys must be factor names; values finite and ≥ 0; not all zero;
`reliability` / `performance` must be 0; `region_match` needs a region. `weights` with any other mode
is rejected. Ties in score are broken by lower price, then fresher data.

Total score = Σ weight × value. Each factor's `contribution` is reported, and they sum to the score.

## Explanations

Each candidate carries `explanation`, assembled from the factor notes, e.g.
"9% below current market median ($2.45); available now (explicit); region matched (US); fresh API data
(4 min old); priced availability 94% of last 30 days; OpenGrid can provision via API (level 3)".
Each alternative carries `vs_selected`, e.g. "+4% more expensive but stronger availability history".
A factor counts as stronger/weaker when its normalized value differs by at least 0.1.

## What this is not

- Candidate prices are **observed market prices** (OpenGrid's normalized reading of list prices). A
  **quote** exists only for a specific route; an **execution price** only once a deployment runs.
- No reliability, uptime-SLA or benchmark performance data is used.
- The ranking does not imply OpenGrid can provision a candidate; `provisionable` and
  `integration_level` say whether it can.
