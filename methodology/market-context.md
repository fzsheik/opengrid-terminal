# Market context

Numbers about the world around GPU prices, shown beside them: what power costs where the GPUs run, how much the
chipmakers sell into data centres, and how much the largest buyers spend on capacity. They are context, never
cause. Nothing on OpenGrid says one of these series moved a GPU price, and none of them feeds listings, indices,
spreads, percentiles or routing.

Kind: **observed**. Code: `context/`. API: `GET /v1/context/series` (the catalogue, or the points of up to 20 series
with `id=`), `GET /v1/context/power` (US listings by state, beside that state's electricity price).

Only sources whose terms allow republishing are used:

| Source | What | Terms | Off until |
|---|---|---|---|
| **EIA API v2** | Average retail electricity price by US state, industrial and commercial, monthly (cents per kWh) | US government data, public domain; EIA asks for an acknowledgment with the publication date | `EIA_API_KEY` is set (free) |
| **SEC EDGAR XBRL** | NVIDIA and AMD total and Data Center revenue; capital expenditure of Microsoft, Alphabet, Amazon, Meta, Oracle, CoreWeave, NVIDIA and AMD; quarterly (USD) | Public filings. SEC fair access: a User-Agent naming the company and a contact email, at most 10 requests a second | `SEC_USER_AGENT` is set |

Considered and **not** used:

- **ENTSO-E** (European day-ahead power prices): no clear reuse licence on the platform, and day-ahead prices
  belong to the power exchanges.
- **US market operators' wholesale prices (LMPs)**: each operator has its own terms, and some restrict
  redistribution.

The `market_area` field names a state's wholesale market only as a label. No wholesale price is read.

## Raw first

Every fetch is stored in `context_snapshots`, failures too, with the body cut to what is read:
- EIA: the state rows for the two sectors;
- EDGAR company facts: the concepts used;
- a filing's XBRL instance: the data-centre revenue facts.

Parsed values go to `context_series`, one row per series and period. That table is rebuilt from the snapshots by
re-parsing.

## When a value was knowable

Each point has `published_at` (when the source made it public) and `first_seen_at` (when OpenGrid first read it):

- **EDGAR:** `published_at` is the filing date of the first filing that made the value knowable. For a derived
  quarter it is the later of the two filings used. A value reported again in a later filing (as a prior-year
  comparative) keeps its first filing. A different later value is a revision: the row takes the new value, counts the
  revision, and keeps the previous value in `meta`.
- **EIA:** it publishes no date per value, so `published_at` is empty and `first_seen_at` is the time to use. A month
  EIA leaves blank is skipped, never stored as 0.

## EDGAR quarters

| What | How |
|---|---|
| Total revenue | `Revenues` or `RevenueFromContractWithCustomerExcludingAssessedTax`, whichever reaches the latest period |
| Data Center revenue | Not in EDGAR's company facts, which carry no dimensions. Read from each 10-Q / 10-K XBRL instance: NVIDIA `srt:ProductOrServiceAxis = nvda:DataCenterMember`; AMD `us-gaap:StatementBusinessSegmentsAxis = amd:DataCenterMember` (operating segments). A fact counts only with exactly those dimensions. |
| Capital expenditure | Cash paid for property and equipment: `PaymentsToAcquirePropertyPlantAndEquipment` or `PaymentsToAcquireProductiveAssets`, whichever the company uses for the latest period (Amazon and, since 2020, NVIDIA use the second; NVIDIA's includes intangible assets). Gross of incentives and finance leases. |

A quarter is **reported** when a filing gives that three-month period. Otherwise it is **derived** from two facts
one quarter apart:
- with the same start, as year-to-date minus the previous year-to-date (Q4 = full year minus nine months);
- with the same end, as six months minus its last quarter.

Cash-flow figures such as capex are only ever year-to-date, so most capex quarters are derived. Fiscal calendars
differ (NVIDIA's year ends in late January), so periods are kept as dates, never relabelled as calendar quarters.

## Power by region

`GET /v1/context/power` maps current listings to a US state only where the region names one unambiguously:
- a documented cloud region (AWS, Google Cloud, Azure and Oracle tables in `context/geo.py`);
- Shadeform's "US, city, ST";
- Vast's "State, US".

Each state is shown with its latest industrial and commercial price. These are state averages, not any provider's
tariff. A provider's own power cost is unknown.

## Freshness

The `context_series` job runs once a day when a source is configured. EIA publishes monthly with a lag of about two
months. Companies file quarterly.
