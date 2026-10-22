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
