"""Build deploy/railway.env: the variables to paste into Railway's Raw Editor.

Run:  .venv/bin/python deploy/make_env.py

Takes your provider keys from .env, adds a site password, and writes deploy/railway.env
(gitignored, owner-only). It prints variable NAMES and the new password, never your keys.
DATABASE_URL is a reference to Railway's own Postgres service, so no database secret is copied.
"""

import secrets
import stat
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "deploy" / "railway.env"
# Only what the deployed app uses. PAPERSPACE_API_KEY and DATABASE_URL are deliberately left behind.
KEYS = ["SALAD_API_KEY", "SALAD_ORG", "HYPERSTACK_API_KEY", "LAMBDA_API_KEY", "RUNPOD_API_KEY", "DIGITALOCEAN_API_KEY"]

local = {k.strip(): v for k, v in dotenv_values(ROOT / ".env").items() if v}
existing = {k: v for k, v in dotenv_values(OUT).items()} if OUT.exists() else {}
password = existing.get("APP_PASSWORD") or secrets.token_urlsafe(15)   # keep the password if you run this twice

lines = ["DATABASE_URL=${{Postgres.DATABASE_URL}}", "APP_USER=opengrid", f"APP_PASSWORD={password}"]
missing = []
for k in KEYS:
    if k in local:
        lines.append(f"{k}={local[k]}")
    else:
        missing.append(k)
OUT.write_text("\n".join(lines) + "\n")
OUT.chmod(stat.S_IRUSR | stat.S_IWUSR)

print(f"wrote {OUT.relative_to(ROOT)}")
print("variables:", ", ".join(l.split("=")[0] for l in lines))
if missing:
    print("not found in .env (those providers will be skipped):", ", ".join(missing))
print(f"\nsite login:  user  opengrid   password  {password}")
