# Data visibility: public and internal data

OpenGrid keeps two tiers of data. **Public** data may be republished and sold: it is what the site, the public
API, the indices, the fixings, exports and settlement are built from. **Internal** data is data OpenGrid may read
and use but may not republish: it is pulled and kept current like everything else, and it helps OpenGrid choose
where to run a workload, but it never appears on a public page, in a public API answer or in any published
statistic.

## The two tiers

| Tier | May be used for | Never used for |
|---|---|---|
| **public** | Everything: pages, the public API, indices and index votes, fixings and settlement, market statistics (percentiles, spreads, dispersion, movers, events), exports and CSV downloads, archived history, reserved prices, currency-converted listings, assistant and MCP answers about the market. | (nothing) |
| **internal** | Choosing and ordering candidates in routing (route previews and routes), job placement, and recommendations made for an OpenGrid account (an API key or a signed-in operator). | Any public page or public API answer, any statistic, index vote, fixing, export or archived series, and the assistant; and any value shown to a customer (see below). Anonymous recommendations use public data only. |

A source is internal when OpenGrid has a right to read it but not to pass it on: for example prices a provider
shows only to an account holder, prices the provider's terms treat as confidential, or a feed whose owner's
terms forbid republishing. Sources whose terms forbid automated access altogether are not read at all, so they
are in neither tier.

Every source is **public unless it is listed below**. The list lives in one place in the code
(`provider_meta.INTERNAL_SOURCES`), each entry with its reason.

## Internal sources

| Provider | Source | What it feeds | Why it is internal | Used by |
|---|---|---|---|---|
| DeepInfra | Container API (`/v1/containers/gpu_availability`), its hourly prices | Internal listings, ids ending `:api` | The API's prices are not the prices DeepInfra publishes on its price page and may be specific to OpenGrid's account; DeepInfra's terms treat non-public pricing as confidential information. DeepInfra's public listings stay the `/gpu-instances` price-page prices, and the same response's in-stock flag stays public capacity. | Routing, job placement, keyed recommendations |
| AWS | Spot Instance Advisor (`spot-advisor-data.json`): interruption bands and average savings | Internal signals | AWS's site terms restrict gathering and republishing data from aws.amazon.com. | Available to routing (spot capacity is not ranked yet, so no route uses it today) |
| Fireworks AI | Every source: the on-demand deployments table of fireworks.ai/pricing | Internal listings (the whole provider) | Its Terms of Service cover the website as part of the Service and, in clause (e), forbid copying or distributing any part of it. OpenGrid reads the page at a human rate (hourly) and may use the prices; it does not republish them, and the provider is not named on public provider lists. | Routing, recommendations and job placement |

## What a customer is shown

Internal data may decide which candidate OpenGrid recommends or routes to, and in what order, but its values are
never shown to a customer, including customers with an API key, through the API, the hosted MCP tools or a route
preview. An internal candidate is shown with:

- the **public** list price of the same product (same provider and instance) when the provider publishes one, marked
  `price_basis: "public_listing"`; otherwise **no price** (`price_per_gpu_hour: null`, `price_basis: "quote"`):
  OpenGrid quotes it when the workload is executed;
- the label "available via OpenGrid";
- nothing computed from its internal price: no cost estimate, fee, saving, score, or comparison ("12% cheaper than
  ...") against it, and no internal stock count or raw provider field.

Comparisons and scores of other candidates that would be measured against an internal value are left out of that
response. Only OpenGrid's operators and platform-admin keys see internal values, labelled `internal`.

## How it is enforced

- **One stamp per row.** When a listing, a capacity row or an archived price is saved, its tier is looked up in
  the registry and stored with the row (`visibility`, `public` or `internal`). The lookup fails closed: a value
  that is not exactly `public`, or any row of a provider listed as wholly internal, is stored as internal.
  The database refuses any other value. A listing's price history belongs to the listing, so it has the same
  tier.
- **Public readers read public rows only.** The market eligibility rule shared by every statistic includes the
  public tier, the hourly market rollup (from which indices, fixings, provider statistics and market events are
  built) samples public listings only, and every public endpoint that reads listings or capacity directly
  filters on the tier.
- **Routing reads both tiers, by asking for them.** The best-execution ranking reads public listings unless the
  caller explicitly asks for both. Only OpenGrid's own execution paths do: route previews and routes, job
  placement, and recommendations for an account. Each candidate says which tier it comes from. The market
  reference a candidate is scored against (the median of providers' prices) is always computed from public
  listings only.
- **Wholly internal providers are not named publicly.** If every source of a provider is internal, the provider
  is also left out of public provider lists and feed-health reports.
- **Customer responses are masked in one place.** Recommendations, route previews and routes (with their audit
  records and quotes), deployments and job previews pass through one disclosure step before they are returned to a
  non-admin caller (`routing/disclosure.py`). A test runs every such call twice with different internal prices: the
  two responses must be identical.
- **Operators see internal data, labelled.** The admin view (`GET /v1/ops/internal-data`, admin only) lists the
  registry, the internal listings and capacity rows, and internal signals, each marked `internal`.
- **Tested on every public route.** A test seeds internal listings, capacity and archived prices that would be
  impossible to miss (an internal provider far cheaper than every public one, a GPU sold only by internal
  listings), builds every derived table, and calls every public route of the application, enumerated from the
  application itself so that a new endpoint is covered without anyone remembering to add it. It fails if an
  internal name, price or count appears in any response, rollup, index vote or fixing, and it checks that
  routing and keyed recommendations do see the internal rows.

## What this means for published numbers

Public statistics are computed exactly as if internal data did not exist. Adding or removing an internal source
never moves an index, a fixing, a percentile or a published price. A provider can have both public and internal
listings for the same product (DeepInfra does); only the public one is ever shown.

See also [data kinds](/methodology/data-kinds) (how each number was obtained) and
[best execution](/methodology/best-execution) (how routing ranks listings).

## Not for AI input

Separate from the two tiers, a provider can ask that its content not be fed to AI systems. Gcore's robots.txt sets
`Content-Signal: ai-input=no`, so Gcore stays public on the site and API, but its rows are removed from everything
the OpenGrid Assistant is grounded on (its cards, and so any language-model phrasing of them) and from every answer
of the MCP tools (local `opengrid mcp` and the hosted `/mcp`). Market aggregates computed over all providers (a
median, a provider count) are left as they are. The list is `provider_meta.AI_INPUT_EXCLUDED`, mirrored in the CLI.
