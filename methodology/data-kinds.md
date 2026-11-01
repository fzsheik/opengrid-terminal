# Data kinds and price concepts

OpenGrid labels every number by how it was obtained, and never mixes kinds in one figure.

| Kind | Meaning |
|---|---|
| **observed** | Read directly from a provider (API, public price page, or aggregator) and normalized. |
| **derived** | Computed from observed data by a stated rule, e.g. `available = capacity > 0`. |
| **estimated** | A model or assumption fills a gap. Always labelled; never silent. |
| **transaction** | What an OpenGrid execution actually quoted or paid. |
| **archived** | Read by OpenGrid's parsers from a public archive (an Internet Archive capture of a provider's price page, or a past version of AWS's price list), not observed live. Sampled only on capture days; never used in indices, percentiles or routing. See [archived history](/methodology/archived-history). |

## Price concepts

| Concept | Meaning |
|---|---|
| **List price** | What the provider advertises. |
| **Observed market price** | OpenGrid's normalized observation of list prices (per GPU-hour, on-demand). |
| **Quote** | The price returned for one specific route request at one moment. |
| **Execution price** | What was actually paid or contracted for a deployment. |

Market indices, spreads and percentiles are built from **observed market prices** only (never archived ones).
Quotes and execution prices live in routing and transaction records and are reported separately.

## Reserved and committed list prices

Some providers publish prices for a commitment, from a few days to 5 years. OpenGrid stores the responses raw and
computes the reserved prices from them when they are read, in their own section: the `/reserved` page and
`GET /v1/reserved`. They are not listings and never enter on-demand or spot prices, indices, spreads or market
statistics. See [reserved prices](/methodology/reserved-prices) for terms, the effective price, derived versus
quoted, and the on-demand comparison.

| Provider | What is stored |
|---|---|
| Azure | 1, 3 and 5-year reserved-instance prices from the public Retail Prices API (`priceType eq 'Reservation'`), eastus and eastus2. |
| GCP | 1 and 3-year commitment SKUs from the Cloud Billing Catalog. |
| Jarvislabs | `reserved_pricing`: 1, 3 and 6-month and 1 and 2-year per GPU-hour. |
| Together AI | GPU Clusters reserved columns from the public price page, per GPU-hour for 3-30, 31-90, 91-180 and 181+ day commitments, cells as printed ("Contact us" kept). Stored under `/pricing#reserved`; code: `providers/together.py` (`reserved_prices`). |
| Latitude | GPU plans from Latitude's own plans API, stored verbatim (needs `LATITUDE_API_KEY`): per region, `hour`, `month` and `year` prices in USD and BRL. |
| Verda | The commitment ladder (`/v1/long-term/periods/instances`): a discount % per term, 1 month to 2 years. |

**Azure conversion (derived).** Azure states a reservation as one price for the whole VM for the whole term,
even though the row says "1 Hour". The comparable figure is:

    $/GPU-hour = term price / (8,760 hours x years) / GPUs per VM

8,760 is 365 x 24 (Azure's own 730-hour month x 12). Leap days are ignored. GPU counts come from the same size
table as Azure's on-demand listings. A size missing from that table is stored but never converted. Example
(eastus, Standard_ND96isr_H100_v5, 8 x H100, Oct 2026): 1 year $551,221 = $7.87 per GPU-hour, 3 years
$1,134,310 = $5.40, against $12.29 on demand. Code: `providers/azure.py` (`reservation_price`).

## Stored raw, not yet used

Public feeds OpenGrid fetches and keeps raw because a later data model needs them. None of them is a listing, a
capacity row or a score, and none enters indices, spreads, percentiles, routing or reliability.

| Source | What is stored | Why it is not used yet |
|---|---|---|
| Scaleway product catalogue (`/product-catalog/v2alpha1/public-catalog/products`) | Scaleway's own list price per GPU Instance type and zone, in EUR (per minute or hour as Scaleway states it), with its status (`general_availability`, `retired`, ...). Code: `providers/shadeform.py` (`scaleway_eur_prices`). | It is EUR-only and OpenGrid converts no currency, so Scaleway listings keep Shadeform's USD price. |
| AWS Spot Instance Advisor (`spot-advisor-data.json`) | Per GPU instance type, region and OS: interruption band `r` (0-4, the feed's own labels `<5%` to `>20%` over the trailing month) and average spot saving `s` (%), with the feed's Last-Modified time. Code: `providers/aws.py` (`spot_advice`). | No AWS spot listings exist (spot prices need credentials); provider reliability counts only OpenGrid's own launches; a band is not stock. The feed is coarse and slow-moving: it was last rewritten on 20 Mar 2026. |
| OVHcloud dedicated-server availabilities (`/v1/dedicated/server/datacenter/availabilities`, World and US APIs) | Per GPU server configuration (Scale-GPU, HGR-AI: L4, L40S, V100S, Radeon) and datacenter, OVHcloud's delivery word verbatim (`1H-high`, `1H-low`, `24H`, `72H`, `unavailable`, `unknown`). Code: `providers/ovhcloud.py` (`baremetal_availability`). | These bare-metal servers are not listings (OVHcloud listings are Public Cloud flavors), so there is no price for a capacity row to join to. |
