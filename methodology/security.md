# Security

How OpenGrid protects the operator, tenants and their money. Each rule below names the threat it
closes (the ids refer to the security audit). Tests: `tests/test_security.py`.

## Who is calling

| Principal | How | Ambient? | Notes |
|---|---|---|---|
| API key | `Authorization: Bearer opg_live_…` | no | verified by `accounts/keys.py`; scopes from the key |
| Operator | the site's HTTP basic auth (APP_PASSWORD), or nothing on a developer machine | **yes** | holds every scope |
| Public | nobody, when `PUBLIC_PAGES` is on | — | GET/HEAD of the public pages and market-data reads only |

"Ambient" means the browser attaches the credential on its own, including to requests a different
web site makes it send. That is what CSRF exploits.

## CSRF (SEC-P1-1)

Every request that the **operator** credential authenticates and that changes state (any method other
than GET, HEAD or OPTIONS, on every path: `/v1/**`, `/fetch`, `/normalize`, …) must:

1. carry the header `X-OpenGrid-Request: 1`. A cross-site page can only add a custom header with
   `fetch()`, and that forces a CORS preflight. OpenGrid configures no CORS, so the preflight fails
   and the request is never sent. A plain HTML `<form>` cannot set headers at all.
2. when the browser states where the request came from, come from OpenGrid itself: `Sec-Fetch-Site`
   must be `same-origin` (or `none`), and `Origin` must match the request's Host or `PUBLIC_BASE_URL`.

Otherwise: **403** with a message saying so. Bearer-key requests are exempt, because a page cannot
make the browser attach a key. The web client (`web/core.js`, `OG.api`) sends the header on every
non-GET request. Scripts that use the site password (curl) must send it too, or use an API key.
FastAPI's `strict_content_type` is pinned on, so JSON endpoints refuse form and text/plain bodies.

## Links and scripts from data (SEC-P1-2)

News feed links are third-party input that is rendered as `<a href>`.

- At ingest, `news/parse.py` keeps a link only if it is an absolute `http(s)://` URL. Anything else
  (`javascript:`, `data:`, `vbscript:`, relative links, or a scheme hidden behind whitespace or
  control characters) is dropped, and the item is stored without a link.
- At render time, `web/pages/news.js` links only http(s) URLs.
- `web/core.js` adds one guard for every page: `OG.h` and `OG.s` pass `href`, `src`, `action`,
  `formaction`, `xlink:href`, `poster` and `data` through `OG.safeUrl`. That function allows only
  http(s) and mailto URLs, relative paths, `#fragments` and `?queries`, and turns anything else into
  `#`. `OG.go` and row clicks refuse non-http(s) targets. String event handlers (`onclick: "…"`)
  are never set.
- The Content-Security-Policy (below) allows no inline script except each page's own boot script.

## Response headers (SEC-P2-6)

On every response: `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`,
`X-Frame-Options: DENY`. On HTML responses:

    default-src 'self'; script-src 'self' 'sha256-<each inline boot script>'; style-src 'self' 'unsafe-inline';
    img-src 'self' data:; font-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self';
    form-action 'self'; frame-ancestors 'none'

The hashes are computed from the page that is actually sent, so the page's own inline script runs.
An injected script has a different hash and does not run. JSON data blocks (`application/json`,
`ld+json`) are not scripts and need no hash. Inline *styles* are allowed because the UI sets style
attributes, and styles cannot run script. FastAPI's `/docs` and `/redoc` (operator only) also allow
`https://cdn.jsdelivr.net`, where Swagger UI and ReDoc are loaded from.

## API keys and rate limits (SEC-P1-3)

- A key with `account:manage` can create keys for its own account, but never one that exceeds itself:
  - its scopes must be a subset of the creating key's scopes;
  - its read rate limit is at most the creating key's effective limit, and defaults to it;
  - its expiry is no later than the creating key's expiry;
  - `platform_admin` cannot be set (only the operator can set it).
- Lineage (`api_key_lineage.parent_key_id`): revoking a key also revokes every key it created,
  transitively. Each revocation is written to `security_events`.
- At most `MAX_KEYS_PER_ACCOUNT` (default 20) active keys per account, on every creation path (API,
  admin API, CLI). The check runs under a lock on the account row.
- Rate limits are token buckets per (key, class), as before, **and** per (account, class):
  `ACCOUNT_RATE_LIMIT_{READ,WRITE,EXECUTE}_PER_MINUTE` (600 / 120 / 20). The operator can override
  them per account with the account setting `{"rate_limit_account": {"read": n, …}}`. A request
  spends one token from both buckets or from neither, so extra keys do not add to an account's
  budget. If a bucket's limit changes, the tokens already spent stay spent, so switching limits
  cannot reset a bucket.
- `GET /v1/keys` needs `keys:read` or `account:manage`.

## Cross-tenant admin (SEC-P2-8)

The `admin` scope gives access to every account. The operator always holds it. On an **API key**,
the scope counts only if the key is flagged `platform_admin`. Only the operator can set that flag:
with `POST /v1/admin/accounts/{id}/keys {"platform_admin": true}` or
`python admin.py create-key --platform-admin`. Without the flag, `admin` is removed from the key's
effective scopes, so every `require_scope("admin")` refuses it. Every key creation, including
whether it is a platform-admin key and which key created it, is recorded in `security_events`.

Keys issued before this change have no lineage row. They are treated as having no parent and as not
platform admin. So an existing admin-scoped key must be re-issued with the flag.

## Alert webhooks (SEC-P1-5)

At every delivery:

1. The hostname is resolved once.
2. **Every** address it resolves to must be public unicast. Refused: private, loopback, link-local,
   CGNAT `100.64.0.0/10`, multicast, reserved, unspecified, IPv6 ULA and link-local, NAT64, and
   IPv4-mapped, 6to4 or Teredo wrappers around any of these.
3. The connection goes to that validated IP, while the `Host` header and TLS SNI stay the hostname.
   The certificate is therefore still checked against the hostname, but a DNS answer that changes
   between the check and the connect (DNS rebinding) cannot redirect the request.

Other rules: no redirects, a 3 s connect and 5 s total timeout, and the response body is never read
or stored. Tenants see only `ok` or `delivery_failed`, never status codes or error types, so a
webhook cannot be used as a port or host scanner. The details go to the server log. Rule creation
applies the same address checks. `ALERTS_WEBHOOK_ALLOW_PRIVATE` (development only) turns them off.

## Deployment detection (SEC-P2-1)

The server counts as **deployed** if any one of these is true:

- `ENVIRONMENT=production`;
- `RAILWAY_ENVIRONMENT` is set;
- `DATABASE_URL` points at a host other than localhost, 127.0.0.1 or ::1;
- `ENVIRONMENT` has a value other than `development` (unknown values fail closed).

When deployed:

- `APP_PASSWORD` is required. Startup refuses without it, and every request gets 503.
- `API_KEY_PEPPER` is required.
- `CREDENTIALS_ENCRYPTION_KEY` must be a real Fernet key.

Only on a developer machine (no signal at all) are a missing password (open site), a derived
pepper and a derived or passphrase encryption key allowed, each with a warning.
`deploy/make_env.py` writes `ENVIRONMENT=production` and `TRUST_PROXY_HEADERS=true`.

## Credential encryption (SEC-P2-2)

BYO provider credentials and webhook signing secrets are Fernet-encrypted.

- **Deployed:** a passphrase or a malformed key is refused (503 at use). Startup logs the reason.
- **Rotation:** set the new key as `CREDENTIALS_ENCRYPTION_KEY` and put the old key or keys in
  `CREDENTIALS_ENCRYPTION_KEYS_OLD` (comma-separated). Decryption accepts any of them
  (`MultiFernet`); encryption always uses the new key. Then run `python admin.py rotate-credentials`,
  which re-encrypts every stored secret under the new key and lists any row it could not decrypt,
  without dropping it. After that, remove the old keys.
- **No silent fallback:** if an account has a BYO credential that does not decrypt,
  `accounts.credentials.resolve` raises `CredentialUnavailable`. It never substitutes OpenGrid's
  managed key. `routing/credentials.py` does the same at launch and pins the credential to each
  deployment.

## Client IP (SEC-P2-3)

`X-Forwarded-For` is ignored unless `TRUST_PROXY_HEADERS=true`. When it is trusted, the client is
the entry `TRUSTED_PROXY_COUNT` (default 1) hops from the **right**. That is the entry our own proxy
appended; everything to its left is supplied by the client. The client IP is used for
`api_keys.last_used_ip`, login throttling and the anonymous rate limit.

## Throttling (SEC-P2-7)

- **Failed basic-auth logins:** after `LOGIN_MAX_FAILURES` (10) failures from one IP within
  `LOGIN_FAILURE_WINDOW_SECONDS` (300), that IP gets 429 with `Retry-After` until the window passes,
  even with the right password. The counter is in-process and bounded in memory.
- **Anonymous visitors (PUBLIC_PAGES on):** a token bucket per client IP of
  `PUBLIC_RATE_LIMIT_PER_MINUTE` (240). Static assets are not counted, and the signed-in operator is
  not limited.

These limits are in-process, which is correct for the single-process deployment. Running several
replicas multiplies them.

## Not covered here

- Provider error text reaching tenants (SEC-P2-4) and the default `refresh` of
  `GET /v1/deployments/{id}` (SEC-P2-9) belong to the routing modules.
- Supply chain (SEC-P2-10): build with `uv sync --locked`.
- HSTS is left to the TLS-terminating edge.

## Re-audit hardening (before the first real launch)

- **CSP inline scripts.** The only inline scripts allowed are the ones in OpenGrid's own `web/*.html`
  templates, by hash (CRLF and LF forms). A script injected into a response is never hashed into the policy.
- **Key lineage under concurrency.** Creating a child key locks the parent row and refuses when the parent or
  any ancestor is revoked or expired; revoking re-reads descendants until none are left. The locks are in
  Postgres, so this holds with several workers.
- **Private webhook targets.** `ALERTS_WEBHOOK_ALLOW_PRIVATE` is ignored (with a warning) whenever the app is
  deployed; it exists only for local development.
- **Open dev servers.** With no `APP_PASSWORD` (local development only), the app answers only when the Host is
  an IP literal, `localhost` / `*.localhost`, `testserver` or the public base host, so a DNS-rebinding page
  cannot reach it as same-origin.
- **Rate-limit classes.** Validation launches are in the strict execute class. Terminate and stop are in the
  write class: they only ever reduce spend, so an emergency shutdown is never throttled like a launch.
