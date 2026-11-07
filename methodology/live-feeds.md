# Live market feeds

Some sources push changes as they happen instead of waiting to be polled. OpenGrid ingests them as they change.
Every change is recorded once, with the provider's own timestamp, and the provider's normal poller keeps running as
the fallback. Today there is one such source: Lium's public executor WebSocket.

Kind: **observed**. Code: `live/`. API (operator): `GET /internal/live/status`, `GET /internal/live/events`.

## Lium executor WebSocket

The socket is `wss://lium.io/api/executors/ws`. It is public and needs no key, but a `User-Agent` is required. It
sends four kinds of frame:

| Frame | Meaning |
|---|---|
| `snapshot` | Always first: every executor. It replaces the local state. |
| `upsert` | One executor added or replaced. In practice the whole record is sent. |
| `delete` | One executor removed. |
| `heartbeat` | The connection is alive. It carries the current `seq` and does not advance it. |

Every frame carries `seq` and `ts` (server time, Unix seconds).

**Change-only.** Each executor is reduced to the fields the Lium price reader uses: price per GPU, GPUs and free GPUs,
tier, location, spot flag, CPU, RAM and disk. These are the same fields the poller keeps. A frame that changes none of
them is counted and not stored; Lium re-sends executors as their uptime counter ticks. A real change is one row in
`stream_events`:
- **kind:** `add`, `change` (with the changed fields) or `remove`;
- **provider time:** the frame's `ts` and `seq`;
- **data:** the reduced view, and the executor exactly as sent (raw).

**Batched writes.** Changes are written at most once a minute. Each write also stores the whole book as one raw
snapshot, in exactly the shape the poller stores, and re-normalizes Lium. Listings and capacity then follow a change
within about a minute instead of up to fifteen. Price history (`listing_observations`) stays change-only, because the
normalizer records an observation only when a listing moved. When nothing changed, the book is still written every
five minutes, so freshness shows the feed is alive.

**Never a stale book.** If no frame (heartbeats included) arrives for three minutes, the connection is treated as dead
and reopened. Changes already learned are still stored, but no snapshot is written from a book OpenGrid cannot vouch
for.

**Gaps and reconnects.** A missed frame shows up as either:
- an upsert or delete whose `seq` is not the last one plus one;
- a heartbeat ahead of the book.

Either one closes the connection, and the next connection's snapshot is diffed against the book. After a restart, the
book is reseeded from the newest stored Lium snapshot, so the first snapshot is diffed too. Differences found this way
have **basis** `resnapshot`: the change happened after `prev_seen_at` and no later than `provider_ts`. Changes that
arrived live have basis `stream`, and `provider_ts` is when Lium sent them. Errors and close code 1013 back off from
1 second to 5 minutes, with jitter.

**Change times in price history.** Each stream snapshot carries, per executor, the `ts` of its newest live (`stream`)
change. The price-history row that records that change gets it as `provider_changed_at`, with `changed_at_basis`
`exact` (methodology/freshness.md), so the detection lag of the live feed is measured. As for every provider, the time
is attached only when the price moved; a stock-only change gets `poll_window`. `resnapshot` differences carry no
provider time: they are recorded as `poll_window`, bracketed by the last fetch that still showed the old value.

**Fallback.** The Lium REST poller runs every 15 minutes whatever the socket does. The newest raw snapshot, from the
poller or the stream, is the one that is read. A dropped socket therefore costs freshness, never data. The silence
watch counts the stream's snapshots as successful fetches. The poller and the stream never normalize Lium at the
same time.

## SF Compute order book (not ingested)

SF Compute publishes an anonymized order book and trade record (`/preview/v2/orderbook/`: `windows`, `quote`,
`depth`, `fills`, `orders`), priced in dollars per node-hour for a delivery window. It is the closest public
thing to transaction data for GPU time. Every endpoint needs a Bearer token: without one, the answer is
`401 missing authentication token` (checked 2026-11-01). Getting a token needs:
- an SF Compute account and organization;
- `sf tokens create`;
- a grant to a workspace role (`sf grants create`).

No read-only role is documented.

Once a token exists, the proposed design is:
- **Raw first.** Each poll of `windows` and of `depth` / `quote` for the standard windows (1 day, 1 week, 1 month
  ahead; per GPU type) is stored as received. `fills` is read since the last fill seen, so every trade is kept once,
  with its `filled_at`.
- **Market-depth series.** Per GPU type and window length (term), every poll records:
  - best bid and best ask;
  - the spread;
  - bid and ask depth (node-hours within 2% and 5% of the top);
  - order counts.
- **Into indices, later, and only as a separate input.** Fills are executed prices, the input regulators prefer
  ([futures path](/methodology/settlement)). The proposal is a transaction-weighted index per GPU type and term: the
  volume-weighted mean of fills over a window, published only when it has at least N fills from at least M distinct
  windows. It would sit beside the list-price indices, never blended into them without a published rule. Bid/ask
  midpoints would be shown as indicative and never used as index inputs on their own.
