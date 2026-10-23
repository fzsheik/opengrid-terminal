# Provider probes (OpenGrid's own)

OpenGrid's own measurements of whether each provider's public API answers, run from every **vantage point** that
reports, with every raw result stored and the rollups derived from it.

Kind: **observed**. Code: `probes/`. API: `GET /v1/probes/summary`, `/targets`, `/targets/{id}`, `/dips`, `/raw`,
`/agents`; `POST /v1/probes/ingest` (scope `probes:write`). Page: `/probes`.

This is a separate dataset from the [UptimeProject data](/methodology/uptime). It does not feed
[provider reliability](/methodology/reliability) or any routing score, and says nothing about GPU capacity.

## What is probed

One public, unauthenticated URL per target (see `probes/targets.py`; each was seen answering when added). Per probe:
one GET that stops at the response headers (some of these pages are hundreds of KB), a fresh connection each time.
A target declares exactly which HTTP statuses mean "up": a keyless API route returning 401, or AWS EC2 returning 400
to an unsigned request, proves the service answers. A target is one of three kinds, shown on the page:

| kind | meaning | strength |
|---|---|---|
| `api` | a real API route | strong: the API answers |
| `host` | the API host answers, no route does anything useful unauthenticated | medium |
| `website` | the provider's public site | weak: it can be up while the API is down |

A target may carry a **provider region** (AWS `us-east-1`, Oracle `eu-frankfurt-1`, OVH `ca`). Providers with several
regional targets let the page say *which of the provider's regions* failed.

## Raw first

Every result is stored untouched in `probe_results`: target, vantage point, time, ok, HTTP status, a classified
failure reason, and the DNS / TCP / TLS / first-byte / total timings in ms. Raw rows are kept 30 days
(`PROBES_RAW_RETENTION_DAYS`); the hourly rollups are kept for good and can be rebuilt from the raw. Results are
refused if the target is unknown or the time is more than 5 minutes in the future or more than 6 hours old; a
duplicate (same target, vantage point and time) is ignored.

**Failure reasons** (`error_class`): `dns`, `connect_timeout`, `connect_failed`, `connection_refused`, `unreachable`,
`tls`, `timeout`, `reset`, `protocol`, `http_5xx`, `http_4xx`, `http_status` (a status the target does not accept),
`other`. The timings show which phase was slow.

## Vantage points, and regional vs global

A probe's **location** is where it ran. The web process is one vantage point; `probes/agent.py` adds more (run it in
other regions with an API key holding `data:read` and `probes:write`; the server owns the target list). Each target
has a probe **slot** per interval (default 5 minutes). In a slot every reporting location either succeeded or failed:

- **ok**: every reporting location succeeded.
- **regional**: some failed and at least one succeeded. It names the failing vantage points. This means "failed from
  there", which is the provider's region *or* the network path to it; the provider-region context and the status
  page tell the two apart.
- **global**: every reporting location failed and at least two reported.
- **unconfirmed**: the only reporting location failed. One vantage point can never call global or regional, so the
  page says so instead of guessing.

An hour takes its worst slot. **Provider region** (a second, separate label): for a provider with at least two
regional targets, a dip is `all_regions` when every regional target dipped in the same hour and `some_regions` when
only some did (naming which). **Availability** is checks that succeeded over all checks, across every vantage point.

Provider-reported incidents (status pages, see the uptime methodology) are shown beside a dip when their times overlap
within 30 minutes; a regional incident is not attached to a dip in a different region. Overlap in time is not proof of
cause.

## What this does not show

- It measures that a public endpoint **answers**, not that an authenticated action such as launching an instance works,
  and not GPU capacity, stock or interruptions.
- Providers with only a website target are measured weakly (see the kind column).
- With one vantage point there is no regional or global call at all. Availability numbers are real; scope is
  `unconfirmed`.
- Latency is time to response headers, including a fresh TCP and TLS handshake from the vantage point's network.
  Compare a provider against itself over time or across locations, not against a provider on a different path.
