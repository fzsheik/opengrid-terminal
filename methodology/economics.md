# Economic value

OpenGrid reports what a customer saved against the market median only when there is a valid comparison,
and reports "no valid comparison: <reason>" in every other case.

Kind: **transaction** (the execution price and GPU-hours) compared with an **observed** market median.
Code: `routing/quality.py` (`economics`), built on `route_outcomes` ([routing quality](/methodology/routing-quality)).
API: `GET /v1/economics` (an API key sees its own account; the operator sees all, optionally `account_id=`).

## Per deployment

| field | definition |
|---|---|
| market median at decision time | from the routing decision's market snapshot: the median, across providers, of each provider's lowest current eligible on-demand price for that canonical GPU ($/GPU-hour), observed market price |
| OpenGrid selected price | the execution price the provider reports; if none was recorded, the quote (labelled `selected_price_basis: quote`) |
| savings % | (median − selected price) / median |
| savings $ | (median − selected price) × GPU-hours actually run |
| GPU-hours actually run | metered usage slices (running seconds × GPUs; stopped time excluded), else billing usage records, else observed uptime × GPUs |

### Valid comparison

All of the following must hold, or the deployment carries `comparison: "no valid comparison: <reason>"`
and no savings figure:

1. **Same canonical GPU.** The deployed GPU is the requested canonical GPU. A family route (for example
   "H100" across SXM5 and PCIe variants) has no single median: variants are different products.
2. **On-demand.** The median is built only from eligible on-demand, non-interruptible listings (the
   market rules in `market.py`), and the selected listing passed the same rules.
3. **Breadth.** The median came from at least **3 providers** at decision time.
4. **It ran.** The deployment was reported running, and a price (execution price or quote) is recorded.

Negative savings (OpenGrid's choice cost more than the median, for example a BALANCED route that paid
for availability) are reported as they are.

## Totals

Over deployments with a valid comparison:

- **total customer savings ($)** = Σ (median − selected) × GPU-hours run;
- **mean / median savings %**;
- the same **by GPU, by provider, by strategy** (routing mode), each with n;
- `excluded_no_valid_comparison`: how many deployments were left out, and why.

Validation deployments (OpenGrid testing its own adapters) are never counted as customer savings.

## Savings vs the customer's previous provider

A design partner may state what they normally pay (`normal_price_per_gpu_hour` on their partner
profile). Savings against it appear in [routing quality](/methodology/routing-quality) only when given.
It is partner-reported, not observed by OpenGrid, and is always labelled that way.

## Funnel

The product funnel (`GET /v1/admin/funnel`, `analytics/product.py`) counts, by ISO week:

**visitor** (an anonymous, cookie-less page id with any client event) → **market user** (2+ market page
views in the week) → **account** → **API key** → **route preview** → **real deployment** (a customer
deployment the provider reported running) → **second deployment**.

Visitors and market users are anonymous weekly counts. From "account" on, each week is the cohort of
accounts created that week and how many of them have reached each later step by now. So the
market-user → account conversion is a ratio of two counts, not a tracked path. The operator's own traffic
and account are excluded. No IP address or personal data is stored: event properties are sanitized
(short keys, scalar values, PII-looking keys dropped, e-mail addresses and secrets redacted, query strings
stripped, 2 KB cap). Server-side events (API calls per key per day, route previews, approvals and completed
routes) are derived from the database idempotently, so a client cannot forge them.
