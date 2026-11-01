# Updates

One entry per shipped update, newest first. Every entry says what shipped, what was planned but did not ship (and why),
what is left, and what is next. Add an entry in the same commit as the change.

---

## 2026-10-31 — Azure reserved (1 / 3 / 5-year) prices, stored raw

### What shipped

**Azure reservations captured from now on.** The Azure provider makes a second filtered GET per region on the
same public Retail Prices API, with `priceType eq 'Reservation'`. There is no new auth and no new host.
- The filter is applied server-side: one region, Virtual Machines, `Standard_NC` / `ND` / `NV` sizes only, no
  Windows. That is about 100 rows a region (1, 3 and 5-year terms) on one page. `NextPageLink` is followed, up to
  the same 3-page cap as consumption, and more than that fails the call.
- A poll is now 4 requests (2 regions x consumption and reservation). Azure allows about 10 a minute. Raw bodies
  are deduplicated, so an unchanged price list adds no new payload.
- Stored raw like the GCP commitments and Jarvislabs' `reserved_pricing`: its own endpoint,
  `/api/retail/prices/reservation#<region>`, with payload `{region, price_type: "Reservation", items}`. The items
  are the consumption fields plus `reservationTerm`, verbatim.
- Fails closed like the consumption call: a missing field, a foreign region or type, or no known GPU size fails
  that snapshot only. Consumption prices are never affected.

**Effective $/GPU-hour, as a pure function.** `providers/azure.py: reservation_price(row)` returns the term,
region, VM size, GPU model (raw and canonical), GPU count, whole-term price, term hours and $/GPU-hour.
`retailPrice` is the whole-term price of the whole VM, even though the row says "1 Hour":
`retailPrice / (8,760 x years) / GPUs`. GPU counts come from the same `GPU_SIZES` table as the listings, so a size
not in the table (GB200, fractional A10 / RTX PRO, `ND96is_H100_v5`) is stored but never converted.
`reserved_prices(by_endpoint)` reads the current ones back out of stored snapshots.
- At ship time, eastus `Standard_ND96isr_H100_v5` (8 x H100) cost $12.29 per GPU-hour on demand. Reserved, it was
  $7.87 for 1 year ($551,221), $5.40 for 3 years ($1,134,310) and $4.92 for 5 years ($1,722,566).

**Kept out of every statistic.** `normalize()` now reads the consumption endpoints by exact label, and it skips any
payload tagged with another price type. Tests prove there are still 22 listings, only `on_demand` / `spot`
segments, and unchanged index votes (H100 SXM still $12.29) with reservation snapshots stored.

**Methodology:** `/methodology/data-kinds` has a new section, "Reserved and committed list prices".

### Planned but not shipped, and why

| Item | Why not |
|---|---|
| Reserved prices as listings, or a read-only API / page | There is no safe non-statistical price kind in the listing model: `market_type` feeds index segments. It still needs the reserved-price data model decision. |
| More Azure regions | Same two regions as the on-demand listings, so the reserved and on-demand prices stay comparable. |
| Azure savings plans | A different commitment: $/hour spent across VM sizes, not a reservation of one size. Not read. |

### Next
1. The reserved-price data model, then one read-only reserved view across Azure, GCP, Jarvislabs and Latitude.

---

## 2026-10-30 — Capacity tab, provider APIs instead of scraping, sign-in page

### What shipped

**Capacity (new data product).** Each provider's own stock signal, stored raw first, then normalised into one shape:
in stock yes/no (always), a tier (low / medium / high) when the provider gives one, and counts when it gives them.
Current state plus a change-only history.
- 19 providers report capacity:
  - **Free + total GPU counts:** Lium, Vast (new).
  - **Free counts:** Hyperstack (machines per size), Jarvislabs, Voltage Park, Latitude (new, servers per location).
  - **Tiers:** RunPod, Koyeb, Scaleway.
  - **Yes/no:** Lambda, DigitalOcean, Linode, Crusoe, Denvr, Boost Run, Horizon, Phyntec, Massed Compute, Hyperbolic.
- API: `GET /v1/capacity` and `GET /v1/capacity/history` (public, documented on /api).
- Page: `/capacity` (GPUs group in the nav). Methodology: `/methodology/capacity`.
- Database: migration `0025_capacity` (tables `capacity_current`, `capacity_changes`).
- Capacity runs after the price refresh and fails open: a capacity error never touches prices.

**Vast capacity census** (needs `VAST_API_KEY`). Free and total GPUs per GPU model, from Vast's own fields.
Free GPUs come from the `gpu_ids` in rentable offers. Each machine's size comes from `num_gpus` / `gpu_frac`.
Example at ship time: H100 SXM had 8 of 101 GPUs free.

**Latitude capacity from its own API** (needs `LATITUDE_API_KEY`). Stock level and in-stock server count per
location. This replaces Shadeform's yes/no. Latitude prices still come through Shadeform.

**Scraping replaced by official APIs:**
- **Jarvislabs** (`misc/server_meta`, needs `JARVIS_API_KEY`). Prices per region and workload, plus spot prices
  and live free-GPU counts. Reserved prices are stored raw.
- **GCP** (Cloud Billing Catalog API, needs `GCP_API_KEY`). VM prices are assembled from Google's GPU, vCPU, RAM
  and SSD SKUs. Each of the 24 machine shapes is proven against Google's published Iowa prices. Spot, commitment
  and Calendar Mode prices are stored raw.

**Sign-in page.** A styled `/login` form with a signed session cookie that lasts 7 days, plus `/logout`, replacing
the browser's password popup. Basic auth still works for scripts. Changing `APP_PASSWORD` signs everyone out.

**Also:**
- `deploy/restore.sh` can prompt for the Postgres values instead of needing a full URL.
- `docs/PROVIDER_API_SURVEY.md`: what each of the 32 providers exposes (capacity, reserved pricing, capacity blocks).
- The /api reference now lists the capacity, uptime and probe endpoints. They were public but undocumented.

### Planned but not shipped, and why

| Item | Why not |
|---|---|
| Nebius capacity (tier plus count per region and machine size) | Works with a personal login. The read-only service account the server needs was not created: the step that creates the account and its key file was blocked for approval. Commands are ready. |
| Massed Compute counts (`capacity_available`) | The key in `.env` gets a 401. Massed Compute issues API tokens by email (techadmin@massedcompute.com). |
| GCP regions beyond Iowa, and GCP capacity (`advice capacity`) | Both need a GCP service account. The API key only reads the price catalogue. |
| Crusoe counts (`/capacities` returns `quantity`) | Needs a Crusoe account. |
| Reserved / committed prices as listings | Already stored raw for Jarvislabs, GCP and Latitude. It needs a data model decision first (term, upfront, per-GPU-month vs hour). |
| A shared tier standard across providers | Waiting on a decision. Nebius publishes the only written tier definitions so far, which makes it a good template. |
| A "market vs account" flag on counts | Needed before Nebius goes live: its counts are capped at our quota (8 GPUs), so they must show as "8+". Not built yet. |
| Latitude prices from Latitude's own API | Out of scope for this update; still via Shadeform. |
| Akash (public free + total GPU counts) | Not one of our 32 providers; waiting on a decision to add it. |

### Known issues at ship time
- 6 test suites fail on this commit and on the commit before it, so they predate this work: chaos, jobs_e2e,
  limits, reconcile, stats, structure. Most failures show New York times, which suggests they depend on the
  timezone or the date. Not investigated yet.
- At startup the analytics rollup can log a duplicate-key error on `structure_provider_daily` when two refreshes
  run at once. It is logged and skipped, and prices and capacity are unaffected. Not fixed yet.
- Most counts cover the whole market. Exceptions: Nebius's counts will be capped at our quota, and Crusoe's may be.
  See the methodology page.
- Found after shipping:
  - **Jarvislabs had no prices from Oct 25 to Oct 30.** The old pricing-page scraper broke around midday on
    Oct 25 and failed every poll until today's switch to the API. The gap cannot be backfilled: the failed polls
    stored errors, and the API has no history.
  - **Nebius has had no prices since Oct 19** (the last good fetch was Oct 19 15:55 UTC). Every poll of its
    pricing page has failed since, and it has 0 listings now.
  - Neither break raised an alert. The health view marked them "degraded" / "down", but nothing notifies anyone.
- Railway's `railway.toml` config format stops working on 2026-12-01. Run `railway config migrate` before then.

### To do: treat every non-API source as likely to break
Any price or capacity feed that isn't read from an official API or CLI should be assumed to fail eventually
and silently, whether it's a scraped page or a third party's listing. Jarvislabs and Nebius both broke this way.
1. **Alert on silence.** Raise an ops alert when any provider has had no successful poll for more than a few
   hours, and again when its listings drop to 0. Highest priority: it would have caught both breaks within hours.
2. **Label the source on every row.** Put each listing's and capacity row's source type in the API and on the
   page: official API / CLI, published pricing file, scraped page, or via an aggregator. Then readers can weigh
   scraped data accordingly.
3. **Move every scraped provider to an official source, or flag it as fragile.**
   - **Scraped pages:** Nebius (down since Oct 19; has an API, needs the service account), CoreWeave, Together,
     Hot Aisle, Massed Compute (the API needs the emailed token).
   - **Via Shadeform (secondhand):** Crusoe, Latitude prices, Denvr, Boost Run, Horizon, Phyntec, Scaleway
     prices. These are not broken today, but a third party's listing can lag or be wrong.
   - **Pricing files:** AWS, Oracle. Official, but parsed from files, so a format change breaks them.
4. **Fail loudly on shape changes.** A page that parses to zero rows should count as a failure, never as a
   provider with nothing for sale. Most scrapers already do this; check that all of them do.

### Next
1. Alerts on silence (to-do 1), then Nebius prices back via its API (service account), plus Nebius capacity
   with the market vs account flag.
2. Fix the 6 date-dependent test suites so a red test means a real problem again.
3. Reserved pricing data model, then turn on the stored reserved and commitment prices.
4. Accounts to request: Massed Compute token, GCP service account, Crusoe, Oracle, AWS.
5. Agree the tier standard.

### Deploy notes
- Production needs these keys in Railway (web service `opengrid-terminal`): `JARVIS_API_KEY`, `GCP_API_KEY`,
  `VAST_API_KEY`, `LATITUDE_API_KEY`. Without the first two, Jarvislabs and GCP drop out of production, because
  their scrapers are gone.
- `0025_capacity` runs automatically at startup. It was checked on a clean database: up from 0024, down, and up
  again.

---

## 2026-10-23 — Uptime and probes tabs, roadmap

- **Shipped:**
  - Provider uptime from UptimeProject, with a 90-day backfill.
  - OpenGrid's own probes, which store raw results and tell regional outages apart from global ones. Probe agents
    run in EU-West and Southeast Asia.
  - `docs/ROADMAP.md`.
- **Incident:** a Railway variable change rebuilt production from an out-of-date GitHub `main`, which caused about
  10 minutes of 502 errors. Rule since then: push to `main` before any deploy or variable change.
