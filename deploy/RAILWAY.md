# Deploying to Railway

One project, two services: **Postgres** (Railway's own) and **the app** (this repo, built from GitHub).
No Docker image: Railway builds from `pyproject.toml` + `uv.lock` and starts it with the start command in `.railway/railway.ts`
(Railway infrastructure-as-code; it replaced `railway.toml`, which Railway stops reading on 2026-12-01).

## Once

1. **GitHub.** Push this folder to a *private* repo. `.env` is gitignored; check `git status` shows no `.env`.
2. **Railway project.** New Project > *Provision PostgreSQL* first (leave the app for step 5).
3. **Variables file.** `.venv/bin/python deploy/make_env.py` writes `deploy/railway.env` and prints the site password. It also generates `API_KEY_PEPPER` (required: the app will not start without it) and `CREDENTIALS_ENCRYPTION_KEY`, and keeps both on later runs. Never change either once keys or credentials exist.
4. **Copy your data.** In the Postgres service > Variables, copy `DATABASE_PUBLIC_URL`, then
   `deploy/restore.sh "<that url>"`. It prints matching row counts for Railway and local.
5. **The app.** In the project: New > GitHub Repo > pick the repo. Open the service > Variables > *Raw Editor* >
   paste the contents of `deploy/railway.env` > Deploy. (Until `APP_PASSWORD` is set the app refuses to start, on purpose.)
6. **A URL.** App service > Settings > Networking > *Generate Domain*.

## Check it

- Open the URL: the browser asks for the password (user `opengrid`).
- `<url>/health` answers without a password (that is what Railway checks).
- App service > Logs: `polling started:` lists every provider being polled (60 in production on 2026-11-13; keyed providers drop out
  when their key is missing from Variables, and Alibaba Cloud stays dormant until its AccessKey is set).
- Then **stop the local server** so two copies are not both recording.

## Things to know

- **Exactly one instance.** The poller lives inside the web process; `.railway/railway.ts` pins one replica and one worker. Do not scale replicas.
- **Pick a paid plan.** A trial can run out of credit and stop the app, which stops the data collection.
- **Postgres version.** `restore.sh` dumps from local Postgres 16. If Railway's Postgres is older, restore may fail; pick 16 or newer.
- **Moving later.** Everything here is portable: dump with `pg_dump`, restore elsewhere, set the same variables.

## Service settings (infrastructure-as-code)

`.railway/railway.ts` declares every service's settings: `opengrid-terminal` (start command, `/health` healthcheck,
one replica in sfo, the two custom domains), the probe agents (`python -m probes.agent`, one replica each in
europe-west4 and asia-southeast1; no HTTP, so no healthcheck), Postgres and its volume. Variables are NOT managed by
the file (`variables: { managed: false }`): set them in the dashboard as before.

- Preview: `railway config plan` (read-only). Apply: `railway config apply`. The plan must show 0 to destroy.
- The SDK the file imports (`railway/iac`) comes with the Railway CLI's runner; if a plan asks for it, install it
  under `.railway/` (`cd .railway && npm install railway`), never at the repo root: a root `package.json` would make
  Railpack build this Python app as a Node app.
- A new probe vantage point: add a `service(...)` like the existing two (its region under `replicas`), apply, then set
  its variables (`OPENGRID_SERVER`, `OPENGRID_PROBE_KEY` with data:read + probes:write, `OPENGRID_PROBE_LOCATION`,
  optional `OPENGRID_PROBE_AGENT_ID`).
