# News history: methodology

A news feed shows only its newest items. To have news around price moves from before OpenGrid started polling, OpenGrid
recovers past items from legitimate public archives. Each recovered item is stored raw first with where it came
from and is marked **backfilled**. It is deduplicated against live items and classified by the same rules as live
news ([news](news)). Backfilled items appear next to shifts ([shifts](shifts)) under the same "not necessarily causal" heading.

Code: `news/history.py`. Columns: `news_raw_items.origin`, `archive_url`, `captured_at`;
`news_items.backfilled`, `provenance`. Command: `python -m news.history run`.

## Where past items come from

| Method (`origin`) | What it reads | Applies to |
|---|---|---|
| `feed_archive` | The feed's own archive pages. WordPress `?paged=N` counts only if page 2 is strictly older than page 1, which is checked per source; many feeds ignore the parameter. The Federal Register API is read by publication-date range. | WordPress feeds (NVIDIA blog, AWS blogs, Microsoft, Meta, TechCrunch, ServeTheHome, EE Times, SemiAnalysis, Azure blog...), the Federal Register |
| `wayback_feed` | The Internet Archive's captures of the feed URL (CDX API). Each capture is the feed as it was that day. | every enabled RSS / Atom source |
| `wayback_index` | Captures of blog and newsroom index pages, for providers that publish no feed. Only items with a machine-readable date are kept: JSON-LD `datePublished`, a Next.js page's embedded data, or `<time datetime>` inside an `<article>`. An undated link is not stored. | Crusoe, Nebius, Vast, Verda, Fluidstack, Hyperbolic, Silicon Data |
| `edgar` | The SEC's submissions API, including the older-filings pages. | SEC EDGAR sources |
| `gdelt` | The GDELT DOC 2.0 API, month by month, for two GPU-market queries. | GDELT sources |

**Walking the Internet Archive.** Captures are read newest to oldest. After each capture, the next one read is the
newest capture at or before the oldest item just seen, plus one day of overlap. The feed's whole window is covered
with as few downloads as possible. If a capture has no usable dates, the walk steps back 30 days. When two captures
are further apart than the feed's window, the gap stays a gap: it is reported, never filled.

## Licences and terms

- **Internet Archive**: public captures, read one request at a time at least 4.5 seconds apart, as for archived
  prices.
- **SEC EDGAR**: US government data in the public domain. Requests declare a User-Agent as the SEC's fair-access
  policy asks (`SEC_USER_AGENT`; set it to the operator's contact), at most 4 a second, far under the 10 a second limit.
- **GDELT**: "available for unlimited and unrestricted use for any academic, commercial, or governmental use of any
  kind without fee", with a citation of the GDELT Project and a link to https://www.gdeltproject.org/ required
  (checked on gdeltproject.org/about.html, 2026-11-01). The citation is in the source note and in each item's
  provenance. Requests are 5.5 seconds apart, inside GDELT's one-per-5-seconds limit. GDELT gives titles and links
  only, and its time is GDELT's first-seen time for the article.
- **Publishers' own pages** (`?paged=`): at most one request every 2 seconds.

Every body is cached on disk (`OPENGRID_NEWS_ARCHIVE_CACHE`, default `~/.cache/opengrid/news`), so a re-run
downloads nothing it already has. Back-off on HTTP 429 and 5xx.

## Storage and dedupe

- **Raw first.** Every recovered version of an item is stored in `news_raw_items` with `origin`, the `archive_url`
  it was read from (the capture's viewable URL, the archive page or the API query) and `captured_at` (the capture
  time; empty for dated APIs).
- **Dedupe.** Items key on the same canonical-URL hash as live items (see news). **Live always wins.** An archive copy
  never rewrites an item already stored, live or backfilled. A live poll that later sees a backfilled item
  keeps one row. Re-running the backfill stores nothing new.
- **Story clustering** is the same as for live items, so a recovered article joins the story its syndicated copies
  belong to.

## Publish times

The live rules apply, with the capture time standing in for "now":

- A date more than 24 hours after the capture that held it is not a publish time. The item is flagged.
- **Single-date feeds**: when 5 or more items in one capture share one timestamp, that is the feed's build time,
  not publish times. Those items are flagged, excluded from time windows, and not used to steer the walk. Sources
  dated by day (release notes, the Federal Register) are exempt, because sharing a date is normal there.
- Undated items from index pages are not stored at all.

## Relevance

The relevance formula is the live one. One difference: the recency multiplier measures the lag from publication
to the **capture** that held the item, not to the day OpenGrid read the archive. An article captured a day after
publication scores like live news. One first captured a month later scores like old news. For EDGAR, GDELT and
the feeds' own archive pages, the archive itself dates the item, and the lag is taken as none.

## How far back it reaches

`python -m news.history reach` prints, per source, the oldest item with a real publish time, the oldest live
item, and the counts of items and backfilled items. `GET /v1/news/sources` carries `oldest_dated_item_at` and
`backfilled_items` for every source. Reach depends on how often the Internet Archive captured each feed and on
the walk's budget (`--max-captures`, `--max-pages`).

## New live sources (2026-11-01)

- **SEC EDGAR**: NVIDIA, AMD, Intel, CoreWeave, Microsoft, Alphabet, Amazon, Meta, Oracle, Nebius, DigitalOcean
  and Super Micro. The filings kept are 8-K items 1.01, 1.02, 2.01, 2.02 (earnings), 2.03, 2.05, 2.06, 3.02, 7.01
  and 8.01, plus 10-Q, 10-K, 20-F, 6-K, S-1 and 424B4. Form D funding notices are kept for Lambda, Crusoe and
  Fluidstack. The filing's item codes set its topics (2.02 means earnings, Form D means funding).
- **Changelogs and status**: DigitalOcean release notes, the Together AI changelog, and Lambda's status-page history.
- **GDELT**: two GPU-market queries over the last day, every 6 hours.

All of them have the same health tracking as every other source.
