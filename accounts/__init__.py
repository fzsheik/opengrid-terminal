"""OpenGrid accounts: users/orgs, API keys, scopes, usage, rate limits, BYO provider credentials.

    auth         Principal / principal / require_scope: the gate every /v1 handler depends on
    keys         key generation, HMAC hashing, verify() (401 / 403 / 429), create / revoke / list
    accounts     account rows and the implicit operator account
    ratelimit    in-process token buckets per key and request class
    usage        buffered per-request usage rows + the ASGI middleware that records them
    credentials  BYO provider credentials, Fernet-encrypted; resolve() for routing

Sessions come from `normalize.SessionLocal` (one patch point for tests, as in analytics.rollups).
"""
