# Equity prices (INTERNAL research only)

This note is internal. It is not a public methodology page: `methodology/internal/` is outside the directory the
site serves (`api/pages.py` reads `methodology/*.md` only) and nothing in the public navigation links to it.

## What and why

OpenGrid tracks daily and hourly stock prices of GPU-relevant companies (NVDA, AMD, TSM, AVGO, MU, INTC, ARM, SMCI,
DELL, CRWV, NBIS, ORCL, MSFT, GOOGL, AMZN, META, VRT, ANET) and two semiconductor ETFs (SMH, SOXX) for its own
research and analysis: for example, to see whether GPU rental prices moved in a window when the chip and cloud
stocks did. The list is `context/equities.TICKERS`, overridable with `EQUITY_TICKERS`.

**The data is not displayed, not served by any public or keyed read, and not republished or resold.** It exists only
for internal analysis. Like every market-context series it is context, never cause, and never an input to listings,
indices, spreads, percentiles or routing.

## Source

Alpaca Market Data API v2 (`https://data.alpaca.markets/v2/stocks/bars`), market data only; the trading API is never
called. Keys: `ALPACA_DATA_KEY_ID`, `ALPACA_DATA_SECRET_KEY` (sent as `APCA-API-KEY-ID` / `APCA-API-SECRET-KEY`,
never logged or stored; error text is redacted before it is saved).

- **Feed: SIP only** (consolidated tape). Alpaca's free plan refuses SIP for the most recent 15 minutes, so every
  request ends at now − 16 minutes; if a 403 "recent SIP" still comes back, the request is retried once with the end
  an hour earlier. It never falls back to IEX: IEX volume is a small share of consolidated volume, and feeds are never
  mixed in one series.
- **Pace**: at most ~170 requests a minute (free plan: 200), `limit=10000` per page, `next_page_token` followed; a 429
  waits for `Retry-After` (or 60 s), up to three times. A dropped connection, timeout or 5xx is retried after 2, 10 and 30 s;
  a page that still fails is stored as a failed fetch and the next run resumes from the latest stored bar.
- **History**: daily bars from 2016-01-01 (SIP history starts 2016; IEX only from 2020-07), hourly bars for the last
  5 years. Companies listed later (ARM 2023, CRWV 2025, NBIS's relisting) start at their first trading day.

## Adjustment

| Series | Alpaca `adjustment` | Fields | Use |
|---|---|---|---|
| `alpaca.<sym>.<field>.split` | `split` | open, high, low, close, volume, vwap | price moves across splits |
| `alpaca.<sym>.<field>.raw` | `raw` | open, high, low, close, volume, vwap | the price actually traded |
| `alpaca.<sym>.close.all` | `all` (splits + dividends) | close | total-return research |
| `equity_bars` (1Hour) | `split` | OHLC, volume, vwap, trades | intraday timing |

Daily bars go to `context_series` (one row per series and session, `unit` usd or shares, `visibility = internal`);
hourly bars to `equity_bars`. Every response page is stored first, raw, in `context_snapshots` (source `alpaca`),
failures included, so the series can be rebuilt by re-parsing.

Adjusted history changes after a split or a dividend. The forward run re-reads 10 days (daily) / 3 days (hourly) of
overlap; when an adjusted close from before the latest stored bar comes back different, that symbol's adjusted
history is re-read in full and the changed rows count as revisions (`revisions`, `meta.previous_value`).

## Completeness

A daily bar is kept only once its session, extended hours included, is over (20:15 ET); an hourly bar only once its
hour closed more than 16 minutes ago. A partial bar is never stored.

## Schedule

`context_equities` runs hourly (only with both keys set). Daily bars for a session appear at the first run after
20:15 ET; hourly bars as each hour closes. On a fresh database the first run backfills everything.
`python -m context equities backfill` re-reads everything (idempotent); `forward` runs the job once; `status` prints
rows per ticker.

## Reads (admin scope only)

- `GET /v1/ops/context/equities`: coverage per ticker; `?symbol=NVDA&field=close&adjustment=split&since=&until=`
  daily points; `&timeframe=1Hour` hourly bars. `Cache-Control: private, no-store`; hidden from the OpenAPI schema.
- `GET /v1/ops/context/equities/moves?start=&end=` (`context.equities.moves`): each ticker's % move (split-adjusted
  and total return) from the last session on or before `start` to the last on or before `end`, beside each GPU's
  on-demand market median and lowest % move (`market_gpu_hourly`, the last hour on or before each date, at most 48 h
  stale). For internal context, for example the shifts / explanations work; never shown publicly.

`/v1/context/series` and `/v1/context/power` filter on `visibility = 'public'` for every caller, admins included;
`tests/test_equities.py` checks anonymous, keyed and operator callers.
