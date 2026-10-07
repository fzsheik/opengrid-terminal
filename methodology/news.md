# News and external signals

OpenGrid watches public sources for events that can move GPU compute markets: provider and
hyperscaler announcements, GPU vendor newsrooms, US export-control rules, power and datacenter
constraints, and trade press. This page covers how items are collected, deduplicated,
classified and scored, and how they appear next to market data. **Being near a price move in
time does not mean a news item explains it. OpenGrid never says it does.**

## Sources

The registry (`news/sources.py`) lists official feeds and APIs only: RSS, Atom, JSON Feed, and
the [Federal Register API](https://www.federalregister.gov/developers/documentation/api/v1) for
Bureau of Industry and Security (BIS) and FERC rules. We do not scrape HTML. Before a source is
enabled, it has to work in a real fetch. A source we could not fetch, or whose site forbids
automated access (CME, for one), stays in the registry as disabled with a note saying why, so
the gap is visible. `GET /v1/news/sources` lists every source with its fetch health.

Each source carries:

- **category**: `provider`, `vendor`, `hyperscaler`, `trade_press`, `regulation` or `markets`.
- **trust tier**: `official` (the company or agency itself), `analysis` or `press`.
- **default topics**: for example, every BIS rule carries `export_controls`.
- **entity hints**: a provider's own blog implies that provider.

## Collection

- Each source is polled on its own interval. At most 4 fetches run at once, and each has a
  20-second timeout. Responses over 12 MB are refused. A failing source is logged and never
  blocks or rolls back another.
- Conditional GET: the source's `ETag` and `Last-Modified` are sent back on the next fetch,
  and a `304 Not Modified` counts as a healthy fetch with nothing new.
- Every attempt is written to `news_fetch_log` with: ok, HTTP status, error, items, new items,
  bytes and duration.
- **Raw first.** Every distinct version of a feed item goes into `news_raw_items` untouched,
  as its XML element or JSON object. Classification can always be re-run from stored data
  (`POST /v1/news/reclassify`).
- Only the newest 100 items of a feed are read on each fetch. Some feeds return their whole
  archive.

Parsing is stdlib only. XML feeds are recognized by their root element, so a feed that
switches between RSS and Atom keeps working. Malformed XML gets one repair attempt: control
characters are removed, bare `&` is escaped, and mislabelled Windows-1252 is decoded. If the
repair fails, each `<item>`/`<entry>` is parsed on its own, so one broken item does not lose
the rest. Documents that declare XML entities are refused.

## Publish time

- RFC 822, ISO 8601 and plain dates are converted to UTC. A plain date (as in the Federal
  Register) is read as 00:00 UTC on that date. A time with no timezone is read as UTC.
- If the publish time is missing, unreadable, or more than 24 hours in the future, OpenGrid
  uses the time it first saw the item (`first_seen_at`) and sets
  `published_at_inferred = true`.

## Deduplication and stories

**Same article, different URLs.** URLs are canonicalized before hashing:

- `http` becomes `https`.
- The host is lowercased, and a leading `www.`, `m.` or `amp.` is dropped.
- Default ports, fragments, trailing slashes and `/amp` suffixes are dropped.
- Tracking parameters are dropped: `utm_*`, `fbclid`, `gclid`, `ref`, `source` and similar.
- The remaining query parameters are sorted.

The SHA-256 of the canonical URL is unique in `news_items`, so an article is stored once. If
two sources carry the same URL, the first one seen keeps it. Some feeds point every entry at
one page, such as release notes with a fragment per day. Those sources key on the entry's id
instead (`key="guid"` in the registry).

**Same story, different outlets.** Titles are normalized first:

- The outlet suffix is removed (" - The Register", " | DCD").
- Text is lowercased and punctuation removed.
- Money amounts get one spelling ("$14 billion" and "$14bn" both become `$14bn`).
- Stop words are dropped.

Two items are the same story when their publish times are within 72 hours and either:

- the normalized titles are identical, or
- both titles have at least 4 tokens, and word-bigram Jaccard is at least 0.6 or token-set
  Jaccard is at least 0.8.

Matches share a `story_id` (the id of the story's first item). `/v1/news` lists each story
once, with every source that carried it. The thresholds are strict on purpose. Merging two
different stories would hide one of them. Missing a syndication only shows the same story
twice.

## Classification (inferred, deterministic)

All classification comes from hand-maintained tables matched against the title and summary
(`news/classify.py`). The same text always gives the same result, and each entity keeps the
text that matched it.

**GPUs.** A model mention maps to its **family**, never to a guessed variant. "H100" becomes
`gpu_family = H100`, listing every canonical H100 variant: SXM5, PCIe, PCIe NVLink and 94GB
NVL. A specific canonical GPU is named only when the text states the form factor, as in
"H100 SXM", "HGX H100", "H100 PCIe", "H100 NVL" or "A100 80GB SXM4". "A100 SXM" without a
memory size stays at family level, because it could be 40GB or 80GB. Architecture words such
as "Blackwell" and "Hopper" are families too, listing that generation's datacenter parts.
"GH200" is not "H200", and "GB200" is not "B200".

When an item is matched to a GPU:

- An item that names a specific variant matches that variant, and also matches a search for
  its family.
- A family-level item (one that says "H100" and nothing more specific) matches every variant
  in that family.
- An "H100 SXM" item does not match H100 PCIe.

**Providers.** The provider ids from `provider_meta`, plus compute companies OpenGrid does not
poll (CoreWeave, Together AI, Fluidstack, Nscale and others), each with aliases:

- "Lambda Labs" maps to `lambda`.
- "DataCrunch" maps to `verda`.

Ambiguous names ("Lambda", "Crusoe", "Oracle", "OCI", "Verda", "Lium", "Rubin") count only
when the item also has compute context. "AWS Lambda" never counts as Lambda.

**Regions.** Country and place keywords are mapped to ISO codes, then to the region groups
defined in `regions.py` (US, Canada, Europe, UK, APAC, Middle East, LATAM, Africa).

**Topics.** Topics come from keyword patterns. Their weights are listed at `GET /v1/news/topics`.

Two topics are derived:

- `gpu_launch`: a launch word plus a GPU mention.
- `neocloud_expansion`: a neocloud provider plus a capacity, region, datacenter, supply-deal
  or contract topic.

Some topics count only when the item has compute context: `availability`, `region_launch`,
`outage`, `price_change`, `capacity`, `funding`, `earnings` and `contracts`. Compute context is
a GPU mention, a compute term (GPU, AI accelerator, HPC, datacenter, TPU, GPU instance
families...), or a neocloud, marketplace or decentralized provider. This is why "a database
feature is now available in more regions" is not tagged as an availability signal.

## Relevance (0 to 100)

```
entity_points (<= 40)  specific GPU 20 (+5 per extra), family only 15 (+4 per extra),
                       provider named in the text 12 (+4 per extra), provider from a source hint 6,
                       region 4, compute context 8
topic_points  (<= 40)  sum of topic weights; a topic matched only in the summary counts at 0.6x
tier_mult              official 1.0, analysis 0.95, press 0.85
recency_mult           by the lag from publication to first sight: <= 2 days 1.0, <= 7 days 0.9,
                       <= 30 days 0.75, older 0.6 (an archive item found later is old news)
relevance = round(min(100, (entity_points + topic_points) * 1.25 * tier_mult * recency_mult))
```

Every item stores its components (`relevance_components`), so any score can be checked by
hand. `GET /v1/news` defaults to `min_relevance=1`, which hides items with no compute signal at
all. Pass `min_relevance=0` to see everything.

## News next to market data

`GET /v1/timeline` puts these on one time axis:

- **Observed prices** from the hourly rollup (`market_hourly`): the market's lowest and median
  provider price for a GPU, or one provider's lowest.
- **Inferred price moves**: hour-over-hour changes of at least 3% (configurable). A move is
  flagged with `provider_set_changed` when providers entered or left that hour. A provider
  OpenGrid has only just started recording moves the lowest price without the market moving.
- **Inferred availability changes** for each provider: priced, sold out or absent.
- **Market events** from the events engine (`market_events`), when that table exists.
  Otherwise the response says it is unavailable.
- **Related news**: items with the same GPU (or family) or provider in the same window.

A family is never priced as one line: the timeline needs a canonical GPU. With no rollup rows,
prices are returned as unavailable with the reason "insufficient coverage".

`GET /v1/timeline/around?gpu=&at=&window_hours=` returns, under the heading **"Related news and
events around this move (not necessarily causal)"**, the news and market events within the
window. They are ranked as follows:

```
news:    rank = 0.6 * relevance/100 + 0.4 * proximity
events:  rank = 0.6 * severity_weight + 0.4 * proximity      (major 1.0, notable 0.7, info 0.4)
proximity = max(0, 1 - |t - at| / window)
```

Each entry says whether it came before or after the moment and by how many hours. Nothing in
these responses says one thing caused another.

## Data kinds

- News items are **observed** text from public sources.
- Their entities, topics, relevance and story clusters are **inferred** by the rules above.
- Prices in the timeline are **observed market prices**. Moves and availability changes drawn
  from them are **inferred**.

No figure here is estimated.
