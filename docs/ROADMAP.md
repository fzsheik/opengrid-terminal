# OpenGrid Terminal: roadmap

Unscheduled and unprioritised. Items are written down so they are not forgotten, not promised. Move an item to a dated
plan before starting it. Last edited 2026-10-23.

Why now: the terminal ingests a lot of data (32 providers, UptimeProject, status-page incidents, three probe
locations) and clients are starting to depend on the API. Clients touch the API first, so API quality comes before
internal tidiness.

---

## 1. API: structured, clean, easy to use (client-facing, do first)

The API is the product surface. Goal: a new client can read the docs, make a request, and understand every response
and every error without asking us.

- **One error shape.** Today errors come in several forms: FastAPI's `{"detail": ...}`, plain-text `401 Authentication
  required` from the site-login gate, and `{"status":"error","code":502,...}` from the host. Pick one JSON envelope for
  every `/v1` error (code, message, request_id, docs link) and make the gate return it for `/v1` paths.
- **One success shape, audited.** `{"data", "meta"}` is the convention (`api/common.py`). Audit every endpoint against
  it, including the newer `/v1/uptime/*` and `/v1/probes/*`, and the legacy routes below.
- **Retire or fence the legacy routes.** `/market`, `/market/detail`, `/listings`, `/polling`, `/changes` (and
  `web/classic.html`) are the first UI's JSON, outside `/v1`. Decide: keep as documented-deprecated, or remove.
- **Public surface list.** The set of public `/v1` areas is a hand-maintained tuple in `api/pages.py`
  (`PUBLIC_V1`); every new area has to be remembered there. Derive it from route metadata (a tag or a dependency) so a
  new endpoint is private by default and public only by declaration.
- **Naming and parameter consistency.** Same concept, same name everywhere (`provider` vs `name`, `days` vs `hours`
  vs `window`, `gpu` slug vs canonical name). Write the conventions down, then fix outliers behind `/v1` before a
  client pins to them; after clients depend on a shape, changes need versioning.
- **Pagination, filtering and sorting.** One convention (`limit`/`offset` exists on events and listings); apply it to
  every list endpoint and put totals in `meta`.
- **Versioning and deprecation policy.** Only `/v1` exists. Write the policy: what counts as a breaking change, how
  long a deprecated field lives, how clients are told (a `Deprecation` header and a changelog).
- **Docs that are generated, not hand-kept.** `/api` has a curated reference. Make the OpenAPI schema the source of
  truth: every endpoint has a summary, parameter descriptions, example responses and error cases. Add a short
  "getting started" with one working request per domain (market, uptime/probes, routing).
- **Contract tests.** A test that fails when a response shape changes without a version bump.
- **Rate-limit and auth clarity.** Document scopes (`data:read`, `probes:write`, ...) and limits in one place; return
  rate-limit headers consistently.
- **Examples and SDK.** `examples/` and `cli/` exist; decide whether a typed client (Python first) is worth shipping.

## 2. Architecture: stop one deploy taking everything down

Found 2026-10-23: a single deploy mistake (stale GitHub `main` rebuilt after a variable change) crash-looped the whole
site for about 10 minutes, because the web app, price pollers, reconciler, uptime poller and probe jobs are one process
and one deployment.

- **Web + worker split (the cheap step).** Same repo and codebase, two Railway services: a **web** service that serves
  the site and API, and a **worker** service that runs the background jobs (provider pollers, rollups, events, news,
  uptime, incident feeds, probe rollups, reconciliation). A bad worker deploy then cannot take the API down, and the
  reverse. Needs care: the pollers and jobs currently assume exactly one worker process (`--workers 1`), so
  scheduling, advisory locks and the in-memory job registry have to be reviewed first.
- **Independent services where the blast radius is real.** The probe agents are already their own services. Candidates
  to split later, in order of value: execution/reconciliation (money-touching), ingestion (pollers), then the rest. Full
  microservices is not the goal; split only where an independent failure or deploy cadence earns the cost.
- **Health that means something.** `/health` only runs `SELECT 1`. A wedged scheduler passes it. Add readiness
  checks for the job loop and the pollers' freshness, separate from liveness.
- **Deploy hygiene.**
  - Always push to `origin/main` before deploying or changing service variables (see the 2026-10-23 incident).
  - Add a pre-deploy check that fails when local `HEAD` is not on `origin/main`.
  - The app crash-loops if the database is ahead of the code (`init_db` cannot find the revision). Make that a clear
    error that does not restart-loop.
  - Railway deprecates `railway.toml` config-as-code on 2026-12-01 (existing files work until then). Migrate to its
    infrastructure-as-code format before that date.
- **Data safety.** Backups are a manual `deploy/restore.sh` seed, not a schedule. Add scheduled database backups.

## 3. Codebase cleanup ("clean and spiffy")

- **Private names used across modules.** `provider_meta._ALL`, `uptime.store._incident_dict` and `_region_ok` are
  imported by other packages (probes, api). Give them public names and a small interface.
- **Docs that go stale.** `CAPABILITIES.md` holds counts (endpoints, tables, jobs, tests) that drift; it does not yet
  mention the uptime or probes tabs. Generate the counts, or drop them.
- **Provider code paths.** Providers are polled in `providers/`, probed in `probes/targets.py`, given status feeds in
  `uptime/incidents.py`, and routed in `routing/adapters/`. One provider registry that lists what each provider
  supports (market data, probe target, status feed, adapter) would replace four separate lists.
- **Package layout.** `uptime/` (UptimeProject plus status-page incidents) and `probes/` (our own) overlap on scope
  logic and incident joining; consider one `health/` package with sources underneath.
- **Tests that need a migrated database.** `tests/test_pages.py` depends on a dev database that already has the
  schema; make every test build its own scratch database.
- **Frontend.** `web/` is vanilla JS with no build step; page files are long and share helpers by copy-paste (for
  example the safe-link and scope-badge helpers in `uptime.js` and `probes.js`). Extract shared helpers.
- **Type and lint baseline.** No enforced formatter, linter or type checker was seen. Add one and fix the baseline in a
  single pass so later diffs stay small.

## 4. Data

- **Raw-first for the new sources.** Probe results are stored raw. UptimeProject buckets and provider incidents are
  stored parsed. Store the raw response bodies (reuse the raw-snapshot mechanism) so rules can be replayed.
- **More status feeds.** Vast, CoreWeave, Together, Verda, RunPod and others publish status pages in other formats;
  each needs a small adapter.
- **More probe locations** (US East, a second Europe or Asia site) if three is not enough to separate regional from
  global for clients.
- **Interruptions per 1,000 GPU-hours.** No public source exists for the GPU clouds; options noted in the session
  notes: our own long-running probe instances, or provider-reported spot interruption data where it exists.

## Done

- 2026-10-23: `/uptime` (UptimeProject data, daily backfill, regional/global scope, provider incidents) and `/probes`
  (own probes from three locations, raw-first) deployed.
