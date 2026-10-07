# Billing: usage, fee policies, charges, credits, draft invoices

**Status: models only.** OpenGrid processes no payments, charges no cards and simulates no payment
processor. Invoices are generated only as **drafts** from recorded usage.

## Data kind and price concepts

Everything in billing is `transaction` data: it describes what an OpenGrid execution actually cost.
It never mixes with market data:

| concept | where it lives | billing uses it? |
|---|---|---|
| list price | provider catalogues (`compute_listings`) | no |
| observed market price | OpenGrid's normalized view (`/v1/...` market endpoints) | no |
| quote | the price a `/v1/route` request returned | no (routing stores it) |
| execution price | what the provider charged for a deployment period | **yes**: `usage_records.provider_cost_usd` |

OpenGrid's fees are separate charge lines on top of the execution price; they are never folded into
it, so a buyer can always see provider cost and OpenGrid's take apart.

## Usage records

`billing.usage.record_usage(...)` is called by routing for each metered deployment period:
account, deployment id, provider, canonical GPU, GPU count, period start/end and the provider cost.
GPU-hours = gpu_count x period length. It is idempotent on (deployment_id, period_start, period_end).

`kind="compute"` (OpenGrid-managed credentials): OpenGrid pays the provider, so the provider cost is
passed through as a `compute` line. `kind="byo"` (the account's own provider credentials): the
provider bills the account directly, so there is **no** compute line, only OpenGrid's fee lines.

## Fee policies (configurable, versioned, composable)

OpenGrid's economics are rows in `fee_policies`, not code. A policy is a list of components; any
number compose. No fee is hardcoded: with no policy configured, only provider cost passes through.

| component | parameters | priced |
|---|---|---|
| `buyer_fee_pct` | `pct` | per usage record: pct x provider cost |
| `flat_per_gpu_hour` | `usd` | per usage record: usd x GPU-hours |
| `spread` | `pct` or `usd_per_gpu_hour` | per usage record: markup over provider cost |
| `subscription` | `usd_per_month` | once per invoice period |
| `data_api` | `usd_per_1k_requests`, `free_requests` | per invoice period, over successful API-key requests |

Per-usage components accept `applies_to: ["compute", "byo"]` (default both) and an optional `label`.

Scope and time: a policy is either global (`account_id` null) or an override for one account, and is
in force over `[effective_from, effective_to)`. For an account at time t the override in force wins,
else the global policy in force, else none. A new version never edits an old one; it closes it.
Usage is priced by the policy in force at the usage `period_start`, and every charge line stores the
`policy_id` and the exact component that priced it, so a past invoice can always be explained.

## Charges, credits, draft invoices

Charge kinds: `compute`, `fee`, `subscription`, `data_api`, `credit` (negative). Credits have an
amount, a remaining balance, a reason and an optional expiry.

`POST /v1/admin/invoices/draft?period=YYYY-MM` builds one draft per account for that UTC month:
attach every un-invoiced usage charge whose usage period starts in the month; add subscription and
data-API lines from the policy in force at the month start; then apply credits unexpired at the
month start, soonest-expiring first, as negative lines up to the subtotal. Rebuilding a draft first
restores the credits it used, so it is idempotent. Issued or void invoices are never touched.
Amounts are stored to 6 decimal places (USD); presentation rounding is the caller's concern.

## API usage logging (what `data_api` bills)

Each request authenticated with an API key writes one `api_key_usage` row (key, account, time,
method, path, status, duration). Rows are buffered in memory and written every 10 s; at most ~10 s
can be lost in a crash, and the buffer is bounded (overflow drops the oldest and is counted).
Retention is `api_usage_retention_days` (default 90), so drafts must run within that window.
Operator (site-password) requests are not logged.

## Limits and honesty

- Single process: rate-limit buckets and the usage buffer are in memory (the app is one process by design).
- No tax, currency conversion, proration of subscriptions, dunning or payment state exists.
- Billing is only as correct as the provider cost routing reports; it is never estimated here.
