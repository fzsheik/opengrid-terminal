# Currency conversion (FX)

OpenGrid publishes every price in US dollars. A provider that prices only in another currency is converted to USD
at the European Central Bank's euro foreign exchange reference rate, and every converted listing says so: it
carries the original currency and amount, the rate, and the rate's date. If no fresh rate is available, nothing is
converted.

Code: `fx.py` (rates, conversion), `store/fx.py` (tables), `providers/scaleway.py` (the first converted feed).
Endpoints: `GET /v1/fx/rates` (in force now: `eur_usd` with its conversion state, `to_usd` cross rates), `GET /v1/fx/rates/history` (`quote=` any ECB currency); each `GET /v1/listings` row carries its FX provenance.

## Source

The ECB's daily euro reference rates, the official XML feed
`https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml` (on first start, with no rate stored yet, the
90-day file `eurofxref-hist-90d.xml` once, so recent history can be converted too). The rate is quoted as units
of the foreign currency per 1 EUR (e.g. `USD 1.1172` on 30 Oct 2026 means 1 EUR = 1.1172 USD).

Raw first, like provider prices. Every fetch is stored verbatim in `fx_snapshots` (the XML body, status, time,
failures included). Every quote in it (USD and the other 28 currencies the ECB publishes) is parsed into `fx_rates`,
one row per rate date and currency. Bodies stored before 2 Nov 2026, when only EUR->USD was parsed, are re-parsed
once from `fx_snapshots`, so the other currencies have the same history. A body that is not the ECB's shape (no gesmes envelope, no dated rates, a rate that is not
a positive number, a DOCTYPE) is stored as a failed fetch and changes no rate. If the ECB revises a day already
stored, the new value replaces it and the revision is logged; the earlier body stays in `fx_snapshots`.

The feed is fetched every hour (`fx_rates` background job), and before normalizing on a manual `POST /fetch`.

## Timing: which rate applies

The ECB publishes reference rates on TARGET business days at about 16:00 CET. OpenGrid treats a rate dated D as
published at **16:00 Europe/Berlin on D**, and converts a price with the newest stored rate **already published
when the provider's price was fetched** (the raw snapshot's `fetched_at`, never the time of normalizing):

- Friday before 16:00 CET: Thursday's rate. Friday after 16:00: Friday's.
- Saturday, Sunday and Monday before 16:00: Friday's rate. There is no weekend or holiday rate.
- TARGET holidays (New Year's Day, Good Friday, Easter Monday, 1 May, 25 and 26 December) carry the last
  published rate forward the same way.

Because the rule depends only on the stored rates and the snapshot's time, re-running a normalizer over stored
history converts each old snapshot at the rate that was in force when it was fetched.

## Fail-closed rule

A rate whose date is more than **5 calendar days** before the fetch date (in Frankfurt) is stale. With a stale
rate, or none at all, or a failing rate lookup, **no listing is converted**: the provider's converted listings are
not saved that poll, a warning is logged (`fx: ... conversion stopped (fail closed)`), and `GET /v1/fx/rates` reports
`eur_usd.state: stale` (or `missing`). The listings saved earlier then age out of statistics exactly like a feed that
stopped answering (`market.stale_after`), so an old conversion is never presented as current, and nothing is ever
converted at a guessed rate.

Why 5 and not 4: the longest normal gap between two publications is Easter (Thursday's rate is still the newest on
Easter Tuesday morning, 5 days later) and a Thursday Christmas (24 December's rate until Monday the 29th, 5 days).
A 4-day rule would stop conversion on those mornings; 6 days without a rate means the feed itself is broken.

## Cross rates (currencies other than EUR)

A price in any other currency the ECB publishes (JPY, INR, GBP, ...) is converted through the euro:

    X->USD = (EUR->USD) / (EUR->X)        rounded half-up to 8 decimals (the scale of compute_listings.fx_rate)

- **One date for both legs.** Both rates come from the same ECB reference date: the newest date already published
  when the price was fetched on which both are stored. A currency the ECB skipped that day falls back to the last
  date with both legs, and is then subject to the staleness rule.
- **Same fail-closed rule.** No pair, or a pair older than 5 days: nothing is converted.
- **Precision floor.** A currency worth less than 0.0005 USD (the Indonesian rupiah, about 0.000055) would keep
  fewer than 5 significant digits at 8 decimals, so it is refused, not converted imprecisely.
- **Source and legs.** `fx_source` is `ecb_cross`. The two legs (EUR->USD and EUR->X of `fx_rate_date`) are the stored
  ECB rates of that date. They are returned with the listing as `original.fx.legs` by `GET /v1/listings`, and every
  current cross rate with its legs in `to_usd` of `GET /v1/fx/rates`.

## Conversion and provenance

    USD per instance-hour = original instance-hour price x rate          (rounded half-up to 6 decimals)
    USD per GPU-hour      = original instance-hour price x rate / GPUs   (rounded half-up to 6 decimals)

Six decimals is the scale of the stored price columns, so the same raw price at the same rate always gives the
same stored value and does not write a new history row each poll.

A converted listing is an ordinary USD listing (`currency` is `USD`): indices, spreads, percentiles, routing and
every other statistic read it like any other and read none of the provenance fields. The provenance is stored on
`compute_listings` and is returned as each row's `original` by `GET /v1/listings`:

| Field | Meaning |
|---|---|
| `original_currency` | the currency the provider priced in (`EUR`) |
| `original_price_per_instance_hour`, `original_price_per_gpu_hour` | the provider's own prices, in that currency |
| `fx_rate` | USD per one unit of `original_currency` |
| `fx_rate_date` | the ECB reference date of that rate |
| `fx_source` | `ecb` (EUR), or `ecb_cross` (any other ECB currency, through the euro) |

All are empty on listings priced in USD. Each history row (`listing_observations`, `GET /history`) of a converted
listing also keeps the original instance price, the rate and the rate date, so a move in the USD series can be
told apart: a new `fx_rate` with the same original price is a currency move, a new original price is a list-price
move. Because the USD price follows the rate, a converted listing's history gains a row whenever the ECB rate
moves (at most once per TARGET business day); moves under the usual thresholds (0.5% and $0.001,
`normalize.CHANGE_MIN_PCT` / `CHANGE_MIN_USD`) read as flat in change reports.

## What is converted

| Provider | What | Since |
|---|---|---|
| Scaleway | Its own public catalogue (`api.scaleway.com/product-catalog`), EUR per minute, every general-availability GPU Instance type, one listing per type across the zones it is priced in. | 2026-11-01 |
| Verda | Any instance type the API prices in EUR only (a type priced in both EUR and USD is read in USD only, so it is never counted twice). Every Verda type was USD on 2026-11-01, so nothing is converted yet. | 2026-11-01 |
| Gcore | Its public AI pricing page (`gcore.com/pricing/ai`), EUR per hour excl. VAT: self-serve bare-metal GPU servers and Virtual GPU sizes. | 2026-11-01 |
| Hetzner | Its public GPU server matrix (`hetzner.com/dedicated-rootserver/matrix-gpu/`), EUR per hour excl. VAT (the hourly rate it prints), one listing per GEX model. | 2026-11-01 |
| Yotta Shakti Cloud | Its public pricing page (`shakticloud.ai/pricing/`), the On Demand VM table: INR per hour per VM, cross rate. The page does not say whether GST is included; the price is kept as printed. | 2026-11-02 |
| Sakura Internet | Koukaryoku DOK (`ai.sakura.ad.jp/gpu/koukaryoku-dok/`): JPY per hour per plan (the 8-GPU plan per box), cross rate. **Tax-inclusive**: the page says prices include tax and states no rate, so no pre-tax price is derived; listings are marked `provider_tier` `tax_inclusive`. | 2026-11-02 |

Not converted: OVHcloud's EU catalogues (EUR, GBP). OpenGrid reads OVHcloud's two USD catalogues (US and World),
which already list the same flavors; reading an EU price list as well would need a rule for which of two official
price lists of one flavor counts, so they stay unread.

## Scaleway: the switch from Shadeform's price (2026-11-01)

Until 2026-11-01 Scaleway's prices were Shadeform's USD listing of Scaleway, because Scaleway's own catalogue is
EUR-only. From 2026-11-01 they are Scaleway's own EUR list prices converted at the ECB rate, and Shadeform no longer
produces any Scaleway price row (Scaleway is counted once).

**The price step on 2026-11-01 is a source change, not a market move.** Shadeform's listing carried a margin over
Scaleway's list price. At the ECB rate of 30 Oct 2026 (1.1172):

| Listing | Shadeform (USD) | Scaleway list (EUR) | Converted (USD) | Step |
|---|---|---|---|---|
| `H100` (1x H100 PCIe) | 3.30 | 2.8665 | 3.2025 | -3.0% |
| `H100x2` | 6.60 | 5.733 | 6.4049 | -3.0% |
| `L4` (1x L4) | 0.95 | 0.7875 | 0.8798 | -7.4% |
| `L40S` (1x L40S) | 1.70 | 1.4699 | 1.6422 | -3.4% |
| `B300x8` (per GPU) | 9.0075 | 7.50 | 8.3790 | -7.0% |

The listings Shadeform carried keep their listing ids (`H100`, `H100x2`, `L4` ... `L40Sx8`, `B300x8`, mapped from
Scaleway's offer ids such as `H100-1-80G`), their sku and their canonical GPU names, so each series continues
across the switch with this one step in it. Read any Scaleway move on 2026-11-01 against this table. The H100-SXM
types (2, 4 and 8 GPUs) and RENDER-S (a P100, unmapped) were never in Shadeform's list and appear as new listings on
that day. Other changes at the switch: `provider_tier` is `vm` (was `vm/via-shadeform`), `region` lists Scaleway's
zones (`fr-par-2; pl-waw-2`), `available` comes from Scaleway's own per-zone stock words, and `storage_gb` is the
type's scratch storage. Scaleway capacity rows are unchanged (they already came from Scaleway's own API).
