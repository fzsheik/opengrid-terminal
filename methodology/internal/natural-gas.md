# Natural gas (INTERNAL by choice)

This note is internal. It is not a public methodology page: `methodology/internal/` is outside the directory the
site serves.

## What and why

OpenGrid reads natural gas prices and storage from the EIA API v2 as slow inputs to its own models: gas sets the
marginal power price in most US markets, so it is a lagged driver candidate beside power prices. EIA data is
U.S.-government public domain, but by the founder's decision (2026-11-05) these series are **for internal algorithm
use only**: no public page, no public or keyed endpoint, admin-only reads (`/v1/ops/power/series?source=eia_gas`).
The tier is set in `power/sources/eia_gas.py` TERMS (visibility "internal", the reason says "internal by choice").

| Dataset | EIA route | Series | Unit | Depth |
|---|---|---|---|---|
| `eia_gas.daily` | natural-gas/pri/fut, daily | Henry Hub spot (RNGWHHD) | $/MMBtu | 1997-01-07 onward |
| | | NYMEX futures, contracts 1-4 (RNGC1..RNGC4) | $/MMBtu | 1993-12 to **2024-04-05**: EIA stopped publishing them (measured 2026-11-05: the last period of all four). History only |
| `eia_gas.storage_weekly` | natural-gas/stor/wkly | working gas in underground storage: Lower 48 (R48), East, Midwest, South Central, Mountain, Pacific (R31-R35), South Central salt / non-salt | BCF, week ending Friday | 2010-01-01 onward |
| `eia_gas.electric_price_monthly` | natural-gas/pri/sum, process PEU | price sold to electric power consumers, per state and U.S. | $/MCF | U.S. from 2002-01; EIA withholds many state-months (no row, never a zero) |

Not read, deliberately: regional hub dailies (ICE-licensed), pipeline flows, LNG, rig counts.

## Storage

Periodic series: one value per `power_days` row, dated by EIA's own period label (trading day, week-ending Friday,
first of the month); `power_series.commodity = 'natural_gas'`. The full backfill measured 54,470 rows, 8.6 MB of
`power_days` including indexes (158 bytes per row); growth is about 3,000 rows (0.5 MB) a year. The forward job keeps
each answer's JSON gzipped (a few KB) when it carried something new.

## Use

`power/cross.py`: Henry Hub against each ISO's hub price (`power_price_vs_henry_hub`) and against GPU median prices
by region (`gpu_price_vs_henry_hub`), lags -14..+30 days, on day-on-day changes, internal rows. Correlation, never
cause.

Backfill: `python -m power backfill --source eia_gas` with NO `--since` (from each dataset's earliest).
