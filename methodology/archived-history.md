# Archived price history: methodology

OpenGrid's own price history starts when it began polling each provider. To show what GPUs cost before
that, OpenGrid reads prices out of public archives: the Internet Archive's captures of provider price pages
and public price APIs, and AWS's own archive of monthly EC2 price lists. These prices carry the data kind
**archived** and are kept apart from everything OpenGrid observes live.

Code: `backfill/` (sources, parsers, loader), `analytics/archived.py`. Table: `archived_prices`.
API: `GET /v1/history/{gpu}/extended` (one daily series, archived then observed), `GET /v1/history/{gpu}/archived`
(the archived samples themselves, with their documents).

## What "archived" means

| | Observed | Archived |
|---|---|---|
| Read by | OpenGrid's poller, live | OpenGrid's parsers, from an archived copy |
| Sampled | every poll (minutes) | only on days the archive captured the page (days to weeks apart; monthly for AWS) |
| Used in percentiles, indices, events, routing, alerts | yes | **never** |
| Shown | every price chart and statistic | the 1Y / ALL price chart before recording began (drawn lighter, labelled), its "Sources" table, the two endpoints above |

An archived price is what the provider's page or price list **displayed** at capture time: a list price,
not a quote and not what anyone paid. A change between two captures is invisible, so the archived series
can miss short price moves entirely. Stock is never inferred from an archived page.

## Sources

1. **Internet Archive captures** (`source = wayback`). For each source URL, the archive's CDX index is
   asked for successful (HTTP 200) captures, at most one per UTC day (the day's first). The capture's
   original bytes (`/web/<timestamp>id_/<url>`, without the archive's toolbar) are parsed. Every row keeps
   the capture's viewable URL (`source_url`) so any number can be checked against the page it came from,
   and the sha256 of the bytes that were parsed.
2. **AWS price-list versions** (`source = aws_price_list`, `basis = price_list_version`). AWS publishes every
   past version of the EC2 price list with its effective period: 123 versions, from 2015-12 to now
   (`python -m backfill aws`). For each version the us-east-1 CSV is streamed (never saved; its GPU rows are
   cached) and filtered exactly like the live AWS poller (on-demand, Linux, shared tenancy, no pre-installed
   software, the same `GPU_INSTANCES` table, plus older types the poller no longer lists: p2, g2, g3, p3dn, stored
   unmapped). `observed_at` is the period start and `valid_to` the period end: the price applied for the whole
   period. Every row's note names the version id and its publication date. The current version reproduces the
   live poller's prices exactly (checked on import).
   - Files before 2018 lack some columns. A missing `CapacityStatus` reads as "Used" (the only status of a normal
     instance-hour) and a missing `OfferingClass` as "standard" (convertible RIs began in 2016-09). The 2017-11
     version gives p3.2xlarge $3.06/h, AWS's launch price.
   - **Reserved instances** from the same files are stored with `market_type` `reserved_1y` / `reserved_3y`,
     one row per (instance type, term, offering class, purchase option), at the effective hourly price over the
     term: hourly + upfront / (8,760 h x years). The note keeps the upfront fee and hourly rate. They are never
     read as on-demand or spot: the archived reader takes one market at a time and the API offers only
     `on_demand` and `spot`.

3. **Azure "effective since" anchors** (`source = azure_retail`, `basis = effective_since`;
   `python -m backfill azure-since`). The Retail Prices API returns only current prices, but every row states
   the date that price took effect (`effectiveStartDate`). One point is stored per GPU size, region and market
   (on-demand, spot), dated at that start date: ND96asr_v4 (8 x A100 40GB) at $27.197/h since 2021-03-01 in
   eastus and since 2021-06-01 in westus2. It is **one point, not a series**: the price before that date is
   unknown, and `valid_to` stays empty so the point is never stretched to today. Rows are read with the live
   poller's rules (`GPU_SIZES`, Windows and Low Priority meters dropped, whole-VM price / GPU count). Every
   point links to its meter's own query (`$filter=meterId eq '...'`) and its note says when OpenGrid read it.
   A later run that finds a new price stores a new point with the new start date; the old one stays.

4. **Open datasets with a licence** (`backfill/datasets/`; `python -m backfill datasets`). Each point records its
   licence and attribution (`licence`, `attribution` columns) and the exact revision it came from.
   - **Full Stack Deep Learning, cloud-gpus.csv** (`source = fsdl_cloud_gpus`, `basis = dataset_snapshot`,
     MIT, from 2022-08). Every revision of the file on the default branch (git history, first parent). The table
     is edited by hand, so a commit date is when a row was *published*, not when the price was checked. A row is
     stored once per version: where it first appears or a price changes; later commits that carry it along add
     nothing (otherwise a row nobody updated would read as a fresh price for years).
   - **Huang (2026), GPU Rental Value Curves**, Zenodo doi:10.5281/zenodo.22053861 (`source = huang_vast_monthly`,
     `basis = monthly_median`, CC BY 4.0, Vast.ai from 2022-10). The value is the month's count-weighted median
     *transacted* rate of rented, verified machines (renter-side, fees included, on-demand and interruptible
     pooled), so it is stored as its own market, `transacted`, with `valid_to` the month's end. It is counted in
     coverage but never enters the on-demand or spot series. The release's third-party Ornn file is never read.
   - Names: explicit tables (`backfill/datasets/names.py`). Providers OpenGrid does not track are skipped and
     counted. A GPU label that hides a variant ("A100 (80 GB)") is resolved only from the row's own instance
     name through the providers' tables (AWS `GPU_INSTANCES`, Azure `GPU_SIZES`, GCP machine families) or a form
     factor the name spells out; otherwise it stays unmapped. Vast's "A100 SXM4" pools 40 and 80 GB cards and
     stays unmapped. Listing ids join the live ones where the live id can be derived (Exoscale `gpu3.small`,
     Paperspace `A6000x2`).
   - **Never double counted.** A dataset point that repeats a primary source (`aws_price_list`, `azure_retail`,
     `wayback`) for the same provider, listing (provider, market, GPU count, and GPU or sku) and date (inside an
     AWS version's period; else the same UTC day) is not stored again: it is appended to the primary row's
     `corroboration` (source, document, date, its own stated price, licence), once.
5. **Third-party catalogues kept raw.** dstack's gpuhunt publishes per-provider catalogues to a public S3 bucket
   and old versions expire after about 90 days. The `gpuhunt_snapshot` job (daily; `gpuhunt_snapshot_enabled`)
   stores each provider's current `catalog.zip` verbatim in `catalog_snapshots` (sha256, size, Last-Modified),
   once per (provider, version). Nothing reads these into prices. Licence recorded: gpuhunt's code is MPL-2.0;
   the bucket states no licence for the data.
6. **SkyPilot catalogue history** (`backfill/datasets/skypilot.py`): **not ingested.** The catalogue repository
   has no licence file, so the loader refuses to run unless `skypilot_catalog_enabled` is set *and*
   `skypilot_catalog_licence` names the licence SkyPilot confirm (recorded on every point). When enabled: one
   revision per cloud per UTC day (that day's last commit), GPU rows only, on-demand per region and spot per
   zone, `basis = catalog_commit`, each point linked to its commit.

## Parsing

- Where OpenGrid scrapes a page live, the archived capture goes through **the live parser unchanged**
  (`providers/<provider>.py`: `extract` then `normalize`), with the capture time as the observation time.
- Older page designs the live parser rejects go to a parser written for that design (`backfill/pages.py`).
  These read only labelled values (a price next to its own "/ Hour" or column header, a count next to
  its "GPU Count" label) and raise on a row they recognise but cannot read fully.
- A capture no parser reads is reported (with every parser's reason) and stores nothing. A design nobody
  has written a parser for yet therefore shows up as a gap, never as wrong numbers.
- GPU names go through `canonical.py` like live ones. A name the live pipeline leaves unmapped on purpose
  (e.g. "NVIDIA A100 80GB", SXM or PCIe not stated) stays unmapped: stored with `gpu = null`, not charted.
- The same sanity bound applies as for live data: a price per GPU-hour must be positive and at most $200.
- Imports are idempotent: `(source, source_url, provider, listing_id)` is unique (one archived document, e.g. a
  Shadeform catalogue, can list several providers).

## The daily series (1Y / ALL charts)

`GET /v1/history/{gpu}/extended?days=365` returns one value per provider per UTC day, and the lowest / median /
highest across providers:

- Before `archived_until` (the first UTC day OpenGrid recorded the GPU live) a provider's value is its archived
  price, **held from one capture until its next**, at most 45 days (an AWS price-list version: until its period
  end). This is the same "a posted price holds until it changes" rule the observed step lines use, applied to
  sparser samples. After 45 days without a capture the provider has no value (a gap), not a stale one.
- From `archived_until` on, a provider's value is its lowest observed price that day; archived data is not used.
- `kind[i]` is `archived` or `observed` per day, and every archived value names the capture it was held from.

The chart draws one continuous line through both: the archived stretch lighter on a faint tint, with a note
under the chart, the capture behind each archived value in the tooltip, and the source documents in "Sources".
The provider set is usually smaller before recording (only providers whose pages were archived and parsed),
so the cross-provider lowest / median can step at `archived_until` without any price moving; the providers
view shows each provider separately.

## The monthly series

`GET /v1/history/{gpu}/archived` returns:

- `points`: one per provider per source document: that provider's lowest priced on-demand listing for the
  GPU in the document, with the document's URL.
- `providers`: per provider, the number of samples, first and last date, lowest and highest price.
- `monthly`: per UTC month, one vote per provider (its lowest archived price that month), the lowest and
  the median across the providers that voted, and how many voted. A provider with no capture that month
  does not vote, so month-to-month changes in `lowest` / `median` can come from which providers were
  captured, not from prices moving. Compare a provider with itself, or read `by_provider`.
- `observed_from`: when OpenGrid started recording the GPU live.

## Running it

```
python -m backfill survey                      # captures per source; nothing downloaded or stored
python -m backfill import --report report.json # download (polite, cached), parse, store
python -m backfill import --no-download        # re-parse only what is cached (seconds)
```

Downloads are one request at a time, at least 4.5 s apart, with back-off on 429/5xx, and are cached on
disk (`OPENGRID_ARCHIVE_CACHE`, default `~/.cache/opengrid/wayback`).
