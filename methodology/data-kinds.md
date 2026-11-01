# Data kinds and price concepts

OpenGrid labels every number by how it was obtained, and never mixes kinds in one figure.

| Kind | Meaning |
|---|---|
| **observed** | Read directly from a provider (API, public price page, or aggregator) and normalized. |
| **derived** | Computed from observed data by a stated rule, e.g. `available = capacity > 0`. |
| **estimated** | A model or assumption fills a gap. Always labelled; never silent. |
| **transaction** | What an OpenGrid execution actually quoted or paid. |

## Price concepts

| Concept | Meaning |
|---|---|
| **List price** | What the provider advertises. |
| **Observed market price** | OpenGrid's normalized observation of list prices (per GPU-hour, on-demand). |
| **Quote** | The price returned for one specific route request at one moment. |
| **Execution price** | What was actually paid or contracted for a deployment. |

Market indices, spreads and percentiles are built from **observed market prices** only.
Quotes and execution prices live in routing and transaction records and are reported separately.

## Reserved and committed list prices

Some providers publish prices for a 1 to 5-year commitment. OpenGrid **stores these raw only**. They are not
listings and never enter on-demand or spot prices, indices, spreads or market statistics. They wait on a
reserved-price data model (term, upfront, per GPU-month vs hour).

| Provider | What is stored |
|---|---|
| Azure | 1, 3 and 5-year reserved-instance prices from the public Retail Prices API (`priceType eq 'Reservation'`), eastus and eastus2. |
| GCP | 1 and 3-year commitment SKUs from the Cloud Billing Catalog. |
| Jarvislabs | `reserved_pricing`: 1-month, 1-year and 2-year per GPU-hour. |
| Latitude | GPU plans from Latitude's own plans API, stored verbatim (needs `LATITUDE_API_KEY`). |

**Azure conversion (derived).** Azure states a reservation as one price for the whole VM for the whole term,
even though the row says "1 Hour". The comparable figure is:

    $/GPU-hour = term price / (8,760 hours x years) / GPUs per VM

8,760 is 365 x 24 (Azure's own 730-hour month x 12). Leap days are ignored. GPU counts come from the same size
table as Azure's on-demand listings. A size missing from that table is stored but never converted. Example
(eastus, Standard_ND96isr_H100_v5, 8 x H100, Oct 2026): 1 year $551,221 = $7.87 per GPU-hour, 3 years
$1,134,310 = $5.40, against $12.29 on demand. Code: `providers/azure.py` (`reservation_price`).
