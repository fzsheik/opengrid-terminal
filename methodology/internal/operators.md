# Marketplace operators (INTERNAL)

This note is internal. It is not a public methodology page: `methodology/internal/` is outside the directory the
site serves, and nothing in the public navigation links to it.

## What and why

A GPU marketplace lists machines, but a machine is run by an operator, and one operator often runs many. Knowing
which machines share an operator answers two questions prices alone cannot:

1. **How concentrated is a marketplace's supply?** If a handful of operators hold most of the GPUs, the "many
   independent hosts" behind a marketplace price is really a few sellers.
2. **Is the same hardware offered in more than one place?** An operator may list the same machine, or identical
   builds, on two marketplaces at different prices. Those listings are one supply counted twice, and the price gap
   between them is what the second venue adds.

Code: `operators.py`, admin read `api/operators.py` (`/v1/ops/operators`, `/v1/ops/operators/overlap`).

## What is kept, since 2026-11-15

| Marketplace | Field | Where | Covers |
|---|---|---|---|
| Lium | `provider_key`, `miner_hotkey` | the stored copy of `/api/executors` | machines with a free GPU |
| Vast.ai | `host_id` | price-search offers (already kept), and now the keyed census with `cpu_name`, `cpu_cores` | machines seen among the 64 cheapest offers per GPU model in the last 3 hours; the whole fleet when the census runs |

Not kept, on purpose: IP addresses, hostnames, coordinates, GPU serials and Clore's owner ids. Lium's per-provider
endpoints (`/provider-stats/{hotkey}`, billing history) are still not read.

History starts on 2026-11-15: snapshots stored before that day have no operator key for Lium and no host or CPU
for Vast's census, and they cannot be backfilled.

## Tier: internal

Operator keys identify a business or a person. Neither marketplace licenses them for republishing: Lium's terms
grant a limited licence to use the Service, and its partner page names only `pricing.json` and the public nodes
feed as feeds to list. So:

- the keys live only in stored raw copies, which no public read serves;
- no listing field, capacity field or listing id carries them (`tests/test_operators.py` checks the normalized rows);
- nothing in normalize, analytics, indices, market statistics or routing imports `operators.py` (same test);
- the only read is admin-only and marks its answer `visibility: internal`.

## Operators of one marketplace

Per operator: machines, GPUs, GPU models, countries, and the lowest and highest price per GPU-hour. For the
marketplace: the largest operator's share of GPUs, the top five's share, and the Herfindahl-Hirschman index on GPU
shares (0 to 10,000; 10,000 means one operator has everything). A machine without an operator key is counted as
unidentified and left out of the shares, never guessed.

## Matches across marketplaces

A machine has no identity that two marketplaces share, so matches are by hardware fingerprint, in two tiers:

| Tier | Rule |
|---|---|
| strong | same GPU model, GPU count, CPU model and country, and RAM within 3% |
| build | same GPU model, CPU model and country (any GPU count, RAM not compared) |

A fingerprint is a candidate, not proof. Common builds repeat: on 2026-11-15 about 40% of machines shared their
strong fingerprint with another machine of the same marketplace. Every answer therefore carries `fingerprint_unique`
(how many machines of each side have a strong fingerprint nobody else there shares), and each match states its tier.
`price_gap_pct` is the second marketplace's ask over the first's, in percent.

Operator pairs: when both machines of a match have an operator key, the pair (operator on Lium, operator on Vast)
is counted. A pair with several strong matches is the useful signal: one business selling through both venues.

## Limits

- **Lium:** only machines with a free GPU carry an operator key and a CPU model; fully rented machines do not.
- **Vast:** census rows have no RAM and no price, so they can match on the build tier only.
- **Same moment only:** the read compares the newest stored copies. A machine that leaves one marketplace and
  appears on another later is not detected yet; the stored history allows it.
- **Two marketplaces:** Clore and SimplePod publish no CPU model, and Hydra Host no operator.

First measurement (2026-11-15, from stored data before operator keys were kept): Vast 1,456 machines on 626 hosts,
Lium 212 machines; one strong match and five build matches.
