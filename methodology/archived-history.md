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
2. **AWS price-list versions** (`source = aws_price_list`). AWS publishes every past version of the EC2
   price list with its effective period. For each version whose period overlaps the window, the us-east-1
   CSV is streamed and filtered exactly like the live AWS poller (on-demand, Linux, shared tenancy, no
   pre-installed software, the same `GPU_INSTANCES` table). `observed_at` is the period start and
   `valid_to` the period end: the price applied for the whole period. The current version reproduces the
   live poller's prices exactly (checked on import).

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
- **Component prices, said so.** CoreWeave (2021 - 24) and CUDO (2024 - 25) priced GPU instances a la carte and
  their pages print only the GPU component per GPU-hour (vCPUs and RAM extra). Those rows carry provider_tier
  `a-la-carte` / `vm-gpu-component` and their own listing ids: a lower bound on what the GPU cost, never the same
  listing as a whole-instance price.
- **Commitment prices are never read** (reserved, weekly / monthly / 6-month / 2-year columns, starred "3-year"
  rates): only the on-demand column or card, and spot where the page prints one.
- **Providers not merged yet.** Archived pages of providers whose live module is still on a branch
  (`backfill.sources.PENDING`) are read with that branch's listing ids and GPU names (`backfill/pending_names.py`),
  so the series join once it lands. Resellers keep their live tier (`resold:<vendor>`).
- A row first stored unmapped (`gpu = null`) takes its canonical GPU on a later re-import once canonical.py knows
  the name; a mapped row is never changed.
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
python -m backfill import --since 2016-01 --provider azure --no-aws   # one provider, everything archived since 2016
```

Downloads are one request at a time, at least 4.5 s apart, with back-off on 429/5xx, and are cached on
disk (`OPENGRID_ARCHIVE_CACHE`, default `~/.cache/opengrid/wayback`).
