# Deploying to Railway

One project, two services: **Postgres** (Railway's own) and **the app** (this repo, built from GitHub).
No Docker image: Railway builds from `pyproject.toml` + `uv.lock` and starts it with `railway.toml`.

## Once

1. **GitHub.** Push this folder to a *private* repo. `.env` is gitignored; check `git status` shows no `.env`.
2. **Railway project.** New Project > *Provision PostgreSQL* first (leave the app for step 5).
3. **Variables file.** `.venv/bin/python deploy/make_env.py` writes `deploy/railway.env` and prints the site password.
4. **Copy your data.** In the Postgres service > Variables, copy `DATABASE_PUBLIC_URL`, then
   `deploy/restore.sh "<that url>"`. It prints matching row counts for Railway and local.
5. **The app.** In the project: New > GitHub Repo > pick the repo. Open the service > Variables > *Raw Editor* >
   paste the contents of `deploy/railway.env` > Deploy. (Until `APP_PASSWORD` is set the app refuses to start, on purpose.)
6. **A URL.** App service > Settings > Networking > *Generate Domain*.

## Check it

- Open the URL: the browser asks for the password (user `opengrid`).
- `<url>/health` answers without a password (that is what Railway checks).
- App service > Logs: `polling started:` should list **16** providers. Fewer means a key is missing from Variables.
- Then **stop the local server** so two copies are not both recording.

## Things to know

- **Exactly one instance.** The poller lives inside the web process; `railway.toml` pins one worker. Do not scale replicas.
- **Pick a paid plan.** A trial can run out of credit and stop the app, which stops the data collection.
- **Postgres version.** `restore.sh` dumps from local Postgres 16. If Railway's Postgres is older, restore may fail; pick 16 or newer.
- **Moving later.** Everything here is portable: dump with `pg_dump`, restore elsewhere, set the same variables.
