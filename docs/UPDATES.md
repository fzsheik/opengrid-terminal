# Updates

One entry per shipped update, newest first. Every entry says what shipped, what was planned but did not ship (and why),
what is left, and what is next. Add an entry in the same commit as the change.

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

### Next
1. Nebius service account, then Nebius capacity with the market vs account flag.
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
