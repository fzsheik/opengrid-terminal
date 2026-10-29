# Provider uptime (external)

Control-plane uptime and latency for the GPU cloud providers OpenGrid tracks, measured by a third party,
[UptimeProject](https://uptimeproject.org), and republished under its CC BY 4.0 licence.

Kind: **observed** (third-party). Code: `uptime/`. API: `GET /v1/uptime/providers`, `/v1/uptime/services`,
`/v1/uptime/services/{id}`, `/v1/uptime/health`. Page: `/uptime`.

This is a separate dataset on purpose. It is not OpenGrid's own record, it does not feed
[provider reliability](/methodology/reliability) or any routing score, and nothing here is blended into another
number.

## What is measured

UptimeProject runs five probes (New York, Falkenstein, Roubaix, Seattle, Singapore). No probe runs on the provider it
measures. For each service it makes unauthenticated requests to the provider's public API endpoint (for example AWS EC2
`DescribeRegions` every 60 seconds, plus an hourly TLS check). A response with the declared status code counts as up;
any other status or a network error is a failure.

Each minute, a service is **Operational** when a strict majority of reporting probes succeed, **Degraded** when some but
not a majority do, and **Down** when none do. With fewer than three probes reporting the minute is **Unknown** and is
left out of availability. Availability is up minutes over up + degraded + down minutes.

## What OpenGrid stores

| table | what | how often |
|---|---|---|
| `uptime_services` | each measured service of a tracked provider, headline availability (24h/7d/30d/90d), status | each poll |
| `uptime_hourly` | per service, per hour: availability, down and degraded minutes, **cross-probe p50 latency** | each poll rewrites the last 7 days |
| `uptime_daily` | the same fields per **day** (00:00 UTC). UptimeProject serves only daily buckets beyond 7 days, so this is the only way to backfill. | first poll backfills everything upstream has (up to a year), then refreshes the last 30 days |
| `uptime_probe_samples` | per probe status and latency at the moment of the poll | each poll, kept 90 days |

The poll (`uptime` job) runs hourly. UptimeProject caches data for five minutes, so polling faster adds nothing.
**Backfill.** UptimeProject keeps per-minute and per-day aggregates indefinitely (raw measurements for 90 days), so the
first poll of each service loads all its daily history, up to a year. Hourly buckets exist upstream only for the last 7
days, so hourly history builds from the day OpenGrid started polling, and 30- and 90-day views read the daily table.
Per-probe latency cannot be backfilled: UptimeProject's history endpoint exposes only the cross-probe median, so
per-probe history also starts when OpenGrid started polling.

## Reading the numbers

- **Provider availability** is the *lowest* availability across the provider's compute (`iaas`) services, so one bad
  region is not hidden by an average.
- **Latency** is the median of hourly cross-probe p50 values. The five probes sit on three continents, so a series
  moves with which probes reported as well as with the provider. Use the per-probe table to separate the two.
- A window with no data is `n/a`, never 100%.

## Regional or global

Two separate questions, never mixed, both labelled **inferred** with the reason on hover.

**Measured (a dip, from the probes).**
- **partial**: only *some* probes failed (degraded minutes, no full-failure minutes). That is a network path or
  vantage point as much as the provider, so it is never called regional or global.
- **regional**: every probe failed for one measured region while at least one other measured region of the same
  provider did not.
- **global**: every probe failed, and either all of the provider's measured regions dipped in the same window or the
  provider has a single measured endpoint (then "global" means all five probe locations; the region is not known).

**Reported (an incident, from the provider's status page).**
- Google Cloud lists affected locations itself: `global` among them means global, otherwise regional.
- Statuspage-style pages: the incident is **global** if its title or an affected component says global / all regions,
  if it touched 60% or more of a page with at least 5 components, or if it names 5 or more distinct regions;
  **regional** if it names a region (`us-east-1`, `eu-north1`, `nyc1`, `Canada 1`, `CA1`); otherwise
  **unspecified** (the provider did not say). Region names come from the title and the affected components.
- A reported incident is shown beside a dip when their times overlap (within 30 minutes). A regional incident is not
  attached to a dip in a different region. Overlap in time is not proof of cause, and providers under-report.

Incident feeds read today: Hyperstack, Nebius, Crusoe, DigitalOcean, Lambda (Statuspage-style) and Google Cloud.
Statuspage serves the 50 most recent incidents, so history reaches back as far as those 50 do. Other providers have no
feed in OpenGrid yet and show "n/a", not zero.

## What this does not show

- It measures reachability and response time of a **public API endpoint from outside**, with no credentials. It cannot
  show whether an authenticated operation such as launching an instance works, and it says nothing about GPU capacity,
  stock or interruptions.
- Every provider OpenGrid tracks has a row. Only the ones UptimeProject measures have numbers; the rest (most
  GPU-specialist clouds) read "not measured" with no data, never an estimate or a zero.
- This is a market-data page. It has no connection to which providers OpenGrid can launch on.
- Availability is near 100% for most providers over most windows, so latency and down minutes carry most of the
  information.

Attribution: UptimeProject (uptimeproject.org), CC BY 4.0.
