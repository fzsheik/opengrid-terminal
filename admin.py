"""Operator CLI for accounts and API keys. Uses DATABASE_URL / API_KEY_PEPPER like the server.

    python admin.py create-account --name "Acme" [--email ops@acme.com] [--plan free]
    python admin.py list-accounts
    python admin.py create-key --account 3 [--name ci] [--scopes data:read,route:preview]
                               [--expires-days 90] [--rate-limit 300]
    python admin.py list-keys [--account 3]
    python admin.py revoke-key 12
    python admin.py suspend-account 3 | activate-account 3

The full key is printed ONCE by create-key; only its HMAC is stored. The pepper must match
the server's (API_KEY_PEPPER), or the key will not verify there.
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

from accounts import accounts, keys
from accounts.auth import SCOPES


def _print(obj):
    print(json.dumps(obj, indent=2, default=str))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="admin.py", description="OpenGrid accounts and API keys")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("create-account")
    p.add_argument("--name", required=True)
    p.add_argument("--email")
    p.add_argument("--plan", default="free")
    sub.add_parser("list-accounts")
    p = sub.add_parser("create-key")
    p.add_argument("--account", type=int, required=True)
    p.add_argument("--name", default="default")
    p.add_argument("--scopes", default="data:read", help=f"comma-separated, from: {', '.join(SCOPES)}")
    p.add_argument("--expires-days", type=float)
    p.add_argument("--rate-limit", type=int, help="requests per minute (read class) for this key")
    p = sub.add_parser("list-keys")
    p.add_argument("--account", type=int)
    p = sub.add_parser("revoke-key")
    p.add_argument("key_id", type=int)
    for name in ("suspend-account", "activate-account"):
        sub.add_parser(name).add_argument("account_id", type=int)
    a = ap.parse_args(argv)

    try:
        if a.cmd == "create-account":
            _print(accounts.create_account(a.name, a.email, a.plan))
        elif a.cmd == "list-accounts":
            _print(accounts.list_accounts())
        elif a.cmd == "create-key":
            expires = datetime.now(timezone.utc) + timedelta(days=a.expires_days) if a.expires_days else None
            k = keys.create_key(a.account, a.name, [s.strip() for s in a.scopes.split(",") if s.strip()], expires, a.rate_limit)
            secret = k.pop("secret")
            _print(k)
            print(f"\nAPI key (shown once, store it now):\n{secret}", file=sys.stderr)
        elif a.cmd == "list-keys":
            _print(keys.list_keys(a.account))
        elif a.cmd == "revoke-key":
            _print(keys.revoke_key(a.key_id))
        elif a.cmd in ("suspend-account", "activate-account"):
            _print(accounts.set_status(a.account_id, "suspended" if a.cmd == "suspend-account" else "active"))
    except (ValueError, KeyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
