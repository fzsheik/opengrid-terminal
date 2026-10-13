# Reservations: price-lock requests for reserved compute

A **price-lock request** is a customer's request for reserved GPU compute at or below a price they name: "lock
H100 SXM at ≤ $2.25/GPU-h for 5,376 GPU-hours, starting between Nov 1 and Nov 8, in the US". OpenGrid reviews it
and answers with an **offer** only when provider capacity backs it. It is a request / offer workflow (an RFQ),
nothing more.

What it is not, stated plainly because OpenGrid also publishes price indices:

- **Not a quote and not a binding price.** OpenGrid has no provider capacity commitments of its own. Nothing a
  request returns is a price anyone has committed to.
- **Not a guaranteed reservation.** Submitting reserves nothing.
- **Not a financial contract.** A price-lock request is a commercial order for compute delivered by OpenGrid —
  not a derivative, not tradable, no cash settlement. It does not reference or settle against an index, even
  though the indicative reference shows one.
- **No money moves.** No endpoint here launches compute, charges a card, issues an invoice or takes a payment.
  An accepted offer becomes a commitment only in an order form signed outside the API.

Code: `reservations/` (states, service), `api/reservations.py`, `store/reservations.py` (migration
`0019_reservations`), page `/reserve` (`web/pages/reserve.js`).

## 1. The request

`POST /v1/reservations/requests` (scope `route:preview`, `Idempotency-Key` required):

| field | rule |
|---|---|
| `gpu` | one GPU variant: slug or canonical name (`/v1/gpus`). Families and `auto` are refused: a price lock is for one hardware variant |
| `gpu_count` | integer 1 – 1,024: GPUs used at the same time |
| `total_gpu_hours` | 1 – 10,000,000, at most 2 decimals, at least one hour on each GPU (`≥ gpu_count`) |
| `start_after`, `start_before` | ISO 8601 with a timezone. The start window: `start_after` not in the past (5-minute clock allowance), window ≥ 1 hour, `start_before` ≤ 731 days ahead |
| `region_group` (alias `region`) | optional: US, Canada, Europe, UK, APAC, Middle East, LATAM, Africa |
| `max_price_per_gpu_hour` | required, > $0 and ≤ $1,000: the customer's **limit**, not a price |
| `notes` | optional, ≤ 2,000 characters, no control characters |

Unknown fields are refused (`unknown_field`). Errors are HTTP 422 `{code, message, field}`. An account may hold at
most 20 open requests (`too_many_open_requests`, 409). Same key + same body replays the first answer; same key +
different body is 422 `idempotency_key_reused` (methodology/execution-safety.md).

## 2. The indicative reference

Every request stores, and `GET /v1/reservations/reference?gpu=&gpu_count=&region_group=` returns now, an
**indicative** reference. Every block carries `label: "indicative"`; the whole carries `is_quote: false` and
`binding: false`:

- `index`: the OpenGrid on-demand index for that GPU (methodology/indices.md): published `level` and `raw_level`
  (this hour's plain statistic), constituents, hour, methodology version. When the index is not published the
  level is null with the reason; a GPU without an OpenGrid index says so. A level is never invented.
- `best_on_demand`: the cheapest current on-demand listings passing the best-execution eligibility rules
  (methodology/best-execution.md; `CHEAPEST` ranking, region applied when given, instances of up to 8 GPUs),
  with each listing's observed price, availability and age, plus the market median and low. These are observed
  list prices (`price_kind: observed_market_price`).
- `settlement_fixing`: the latest daily and hourly official fixing of that index from `index_fixings`
  (methodology/settlement.md), when that table exists; a restatement supersedes its original. When the table
  does not exist or holds no fixing the block says so; the request never depends on it.
- `limit_vs_index`: the customer's limit relative to the index level, when one is published.

These are reference points for the conversation, not inputs to a price: an offer's price is whatever the backing
provider commitment supports.

## 3. Status machine

```
requested -> under_review -> offered -> accepted -> fulfilled
                                     +-> declined | expired | withdrawn
                            accepted -> cancelled
```

| from | to | who | when |
|---|---|---|---|
| requested | under_review | operator | review started (an offer straight from `requested` records `under_review` first) |
| requested, under_review | offered | operator | an offer with a supply basis (section 4) |
| offered | offered | operator | revised offer: `offer_seq` + 1, the previous offer kept in the event evidence |
| offered | accepted | the requesting account | naming the current `offer_seq`, while the offer is valid |
| requested, under_review, offered | declined | operator | reason required (also retracts an open offer) |
| requested, under_review, offered | withdrawn | the requesting account | reason optional (also declines an offer) |
| offered | expired | system | `valid_until` passed |
| requested, under_review | expired | system | `start_before` passed with no offer |
| accepted | fulfilled | operator | the compute was delivered under a signed order form (reason = its reference) |
| accepted | cancelled | operator | the order form was not signed or was cancelled (reason required) |

`declined`, `expired`, `withdrawn`, `fulfilled` and `cancelled` are terminal. Every transition writes a
`reservation_events` row: from, to, actor (`user` / `admin` / `system`), actor id (`key:<id>` or `operator`),
reason and evidence. Status moves only through one function (`reservations.service.transition`), under a row
lock. Expiry is applied on every read and by the `reservation_expiry` job (every 5 minutes); it is idempotent.

## 4. Offers require a supply basis

`POST /v1/admin/reservations/{id}/offer` (scope `admin`) takes `price_per_gpu_hour` (> 0, ≤ 1,000, ≤ 6
decimals), `valid_until` (15 minutes to 30 days ahead), `terms` (≥ 10 characters) and **`supply_basis`** —
the provider capacity commitment that backs the offer (e.g. "Provider X written capacity commitment ref
PX-2026-114, 64× H100 Nov 1–30"), at least 10 characters. Without it the offer is refused
(`supply_basis_required`). The database enforces the same rule (`ck_rsv_offer_has_supply_basis`): no row can hold
an offer price without a supply basis, whichever code path writes it.

An offer above the customer's limit is allowed (a counter-offer) and flagged `above_customer_limit`. The offer
shows `total_usd` = price × GPU-hours, labelled "offer (not billed; binds only in a signed order form)".

## 5. Acceptance records intent only

`POST /v1/reservations/{id}/accept` (scope `route:execute`, `Idempotency-Key` required, body `{offer_seq}`).
Only the requesting account can accept (the operator cannot accept for a customer: `not_owner`). A stale
`offer_seq` is refused (`offer_changed`, 409, with `current_offer_seq`), so a customer never accepts terms the
operator has since revised. An expired offer is refused (`offer_expired`).

The response says: **"Accepted — OpenGrid will contact you to sign the order form; nothing is billed by this
action."** and carries `launched: false`, `billed: false`, `deployment_id: null`, `charge: null`. Acceptance
creates no route request, quote, deployment, usage record or job (tests/test_reservations.py checks those
tables).

## 6. Access, isolation, limits

- An API key sees only its own account's requests (`GET /v1/reservations`, `GET /v1/reservations/{id}`; scope
  `route:preview`); another account's request is 404. The operator (site login) sees all; the admin endpoints
  (`/v1/admin/reservations*`, scope `admin`, honoured on a key only when the operator flagged it platform_admin)
  list every account's.
- Rate limits: `POST /v1/reservations*` is in the same class as `POST /v1/route/preview` (the execute class,
  per key and per account; accounts/ratelimit.py). Reads are the read class.
- A suspended account is read-only: it can list and read its requests; creating, accepting and withdrawing are
  refused (403).
- `/v1/reservations` is never public, also with `PUBLIC_PAGES` on.
- Discord: new requests, offers and acceptances post to the deployments channel feed (alerts/channels.py,
  `reservations`). Customer notes are never posted.

## 7. Known gaps

- OpenGrid has no capacity commitments of its own: every offer depends on a provider commitment the operator
  records by reference; the API does not verify that document.
- The order form, signature, invoicing and delivery are outside the API; `fulfilled` / `cancelled` are recorded
  by the operator with a reference.
- There is no partial fill, no counter-proposal from the customer other than a new request, and no customer
  e-mail notification (the UI and the API are the channel).
