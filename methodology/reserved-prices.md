# Reserved prices

Reserved and committed prices are what a provider charges when you commit to a term, from a few days to five years, instead of paying by the hour. OpenGrid shows them in their own section, on the `/reserved` page and at `GET /v1/reserved`. They are never mixed into on-demand listings, indices, spreads or market statistics.

## The hard rule

None of these prices is a listing. None of them enters an index, a spread, a percentile, a heatmap, routing, reliability or any market statistic. This is a founders' decision, and the code enforces it in three ways:

- The reserved model is computed when it is read, from raw responses OpenGrid already stores. It writes nothing: no listing, no observation, no rollup row, no table of its own.
- Each provider's on-demand normaliser reads its on-demand endpoints by exact label. The reserved responses are stored under other labels (for example `/api/retail/prices/reservation#eastus` for Azure and `/pricing#reserved` for Together), so a normaliser never sees them.
- Tests (`tests/test_reserved.py`) store reserved responses beside the on-demand ones. They then check that the listings, the hourly market rows and every index vote are identical to a run without the reserved responses. They also check that nothing in the price, analytics or index code imports the reserved model.

## What counts as reserved

A price counts as reserved when the provider publishes it for a stated commitment term, and that term is longer than pay-as-you-go:

| Provider | What OpenGrid reads | Terms |
|---|---|---|
| AWS | Compute Savings Plan rates per instance type from AWS's public price files (`compute-savings-plan-ec2`), Linux, shared tenancy, No / Partial / All Upfront, 15 regions; the effective hourly rate AWS states (upfront amortised by AWS) | 1, 3 years |
| Azure | Reserved-instance prices from the public Retail Prices API (`priceType eq 'Reservation'`), eastus and eastus2 | 1, 3, 5 years |
| GCP | Committed-use prices for the GPU SKU from the Cloud Billing Catalog (`Commit1Yr`, `Commit3Yr`), in every region Compute Engine offers the GPU (us-central1 without the service account) | 1, 3 years |
| Jarvislabs | `reserved_pricing` in the `misc/server_meta` catalogue, per GPU-hour | 1, 3, 6 months; 1, 2 years |
| Latitude | Each GPU plan's `month` and `year` price per region, from Latitude's plans API | 1 month, 1 year |
| Together AI | The GPU Clusters reserved columns of together.ai/pricing, per GPU-hour | 3-30, 31-90, 91-180, 181+ days |
| Verda | The commitment ladder `/v1/long-term/periods/instances`: a discount % per term | 1, 3, 6 months; 1, 2 years |

These are **not** reserved prices:

- Spot or preemptible prices. They are cheaper because the machine can be taken back, not because you commit to a term.
- Azure savings plans. A savings plan commits a dollar amount per hour across VM sizes, not one size.
  AWS Compute Savings Plans are the same kind of commitment, but they are included (since 2026-11-13) because AWS
  publishes the rate each instance type gets under each term and payment option, which is what buyers compare.
  This is a judgment call; Azure savings plans could be added the same way.
- GCP Calendar Mode and DWS. These reserve a fixed window of capacity, which is a different product.
- Latitude's `hour` price, which is pay-as-you-go. It is used as the on-demand comparison for the same plan.
- Any cell printed as "Contact us", or a dash.

## Term

Each row gives the provider's own label (`1 Year`, `reserved_6m`, `31-90 days`, `Commit3Yr`, `2 years`) and the term in months. Days convert at 365 / 12 days a month.

On the page, terms are columns. A term goes in the longest column its **minimum** commitment reaches, with a 0.1-month tolerance. So Together's "181+ days" goes under 6 months, "91-180 days" under 3 months, and "3-30 days" under "Under 1 month". The columns are: under 1 month, 1, 3 and 6 months, and 1, 2, 3 and 5 years. The API's `term` filter takes the column keys `lt1m`, `1m`, `3m`, `6m`, `1y`, `2y`, `3y` and `5y`.

## Effective price per GPU-hour

The effective price is what one GPU costs for one hour over the commitment.

- **Quoted per GPU-hour** (Jarvislabs, Together AI, GCP). The provider states the rate. It is used as it is.
- **Whole-term price** (Azure, Latitude). The provider states one price for the whole machine for the whole term:

      $/GPU-hour = whole-term price / hours in the term / GPUs in the machine

  A year is 8,760 hours (365 x 24), and a month is 730 hours (8,760 / 12, the month Azure uses). Leap days are ignored. Azure's `retailPrice` on a reservation row is the whole term, even though the row's unit says "1 Hour". Example: eastus `Standard_ND96isr_H100_v5`, 8 x H100, 3 years, $1,134,310. That is $1,134,310 / (8,760 x 3) / 8 = $5.40 per GPU-hour, against $12.29 on demand. The whole-term price is shown with the row (`term_price`, with `term_hours`).
- **GPU only** (GCP). Google prices a committed VM as separate SKUs for the GPUs, the vCPUs and the memory, and OpenGrid stores only the GPU commitment SKUs. A GCP row is therefore the GPU alone, marked `scope: "gpu_only"`. It is compared only with the same GPU SKU on demand, never with a whole-VM listing, and it is left out of the "cheapest per term" summary.

## Derived versus quoted

Every row says how its price was obtained:

- **Quoted** (`derived: false`, `kind: "observed"`). The provider publishes this price for this term. Dividing a whole-term price by hours and GPUs is a unit conversion of a quoted price, not a derivation.
- **Derived** (`derived: true`, `kind: "derived"`). The provider publishes only a discount. Verda's ladder says, for example, 8% off for 1 year. OpenGrid applies that % to each current Verda on-demand listing: `on-demand x (1 - discount / 100)`. The row carries `discount_pct`, so its saving is exactly that discount. A Verda instance type with no current on-demand listing gets no derived row.

## Saving against on-demand

Each row is compared with one on-demand price, from current listings only, in this order:

1. `same_listing`: the provider's own on-demand listing for the same SKU (and region).
2. `same_source`: the provider's own pay-as-you-go price for the same product, from the same response. This is the GCP GPU SKU on demand, or the same Latitude plan's hourly price in that region.
3. `same_size_region`: same provider, GPU, GPUs per machine and region.
4. `same_size`: same provider, GPU and GPUs per machine, with the cheapest region (the region differs).

If none of these matches, there is no comparison and no saving. A saving is never computed against another provider or another machine size.

    saving % = (on-demand - effective) / on-demand x 100

A negative saving means the commitment costs more than on demand, and it is shown as it is. A listing counts as current if its provider was read within its staleness window (2.5 poll intervals, at least 10 minutes), as on the market pages.

## Currency

Only USD prices are used. A price in any other currency is skipped and named in the response's `meta.notes`, for example Latitude's BRL prices. OpenGrid does not convert currencies yet.

## Freshness

The model reads the newest successful raw response for each provider: the same response the on-demand prices come from, or the reserved response stored beside it. No extra polling is done for reserved prices. A row is flagged `stale` when its source was not refreshed within 3 of its provider's poll intervals. `meta.source_as_of` gives the newest source time, and API answers are cached for 60 seconds. Reserved price lists change rarely: Azure's monthly at most, and Together's only when the page changes.

## API

- `GET /v1/reserved`: one row per provider, GPU, GPUs per machine, region and term. Filters: `gpu` (slug or canonical name), `provider`, `term` (column key).
- `GET /v1/reserved/summary`: per GPU, the cheapest whole-machine row in each term, plus the cheapest on-demand price compared. Filter: `gpu`.

Both endpoints are public with public pages on, like `/v1/capacity`, and keys need `data:read`. Code: `reserved.py`, `api/reserved.py`. The per-provider readers are `providers/aws.py` (`savings_plan_prices`), `providers/azure.py` (`reserved_prices`), `providers/gcp.py` (`commitment_prices`), `providers/jarvislabs.py` (`reserved_prices`), `providers/shadeform.py` (`latitude_plan_prices`), `providers/together.py` (`reserved_prices`) and `providers/verda.py` (`commitment_ladder`).
