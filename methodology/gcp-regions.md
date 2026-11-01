# Google Cloud regions and calendar-mode advice

Google Cloud GPU VM prices are listed in every region where Compute Engine itself offers the machine type and every
price component is priced. Before the service account, they were listed in Iowa only. Kind: **observed** list
prices, assembled from Google's own SKUs.

Code: `providers/gcp.py`, `providers/gcp_auth.py`. Tests: `tests/test_provider_gcp.py`, `tests/test_gcp_auth.py`.

## Why a price SKU alone is not enough

Google prices a GPU VM as separate SKUs (GPUs, vCPUs, memory, bundled Local SSD), and many of them are priced for a
whole region group ("running in Americas"). So a region where a SKU is priced is not always a region where the
machine is sold. Google's own pricing page shows G2 (L4) prices in 41 regions, but Compute Engine offers G2 in 18.
Listing every priced region would make up availability.

## Where a machine type is offered

With a read-only service account (`GCP_SERVICE_ACCOUNT_JSON`, `GCP_PROJECT_ID`; roles `compute.viewer` plus a custom
role with `compute.advice.calendarMode`), every 6 hours the poller reads:

| Call | What it gives |
|---|---|
| `GET compute/v1/projects/{p}/aggregated/machineTypes` | each zone's machine types, with vCPUs, memory, GPU type and count, and bundled Local SSD partitions |
| `GET compute/v1/projects/{p}/aggregated/acceleratorTypes` | each zone's attachable GPUs (T4, P4, V100) |

A zone counts for a machine type only when its shape matches the proven shape exactly: the same vCPUs, memory (MiB =
GiB x 1024), GPU type and count, and bundled SSD (partitions x 375 GiB). A region is offered when at least one of its
zones is. A listing then needs every component priced on demand in that region. Where the catalogue has only DWS or
calendar-mode SKUs (for example H200 in europe-west4), there is no listing.

Without the service account, or when its zone lists are more than 2 days older than the newest catalogue, or when
they come back in an unexpected shape, Iowa (us-central1) is the only region listed, as before. A broken zone list
never stops prices. Listing ids are `<machine type>:<region>` everywhere, so Iowa's ids did not change.

On 2026-11-01 this gave **369 listings in 34 regions** (up from 28 listings in Iowa). The counts were A3 High 19,
A3 Mega 11, A3 Ultra 5, A2 High 10 per size, A2 Ultra 5 per size, A2 Mega 3, G2 18 per size, G4 23 per size, T4 23,
P4 7 and V100 5. Iowa lost exactly one listing: the P100, which Compute Engine no longer offers in any zone.

## How the regional prices are proven

As in Iowa, the check is Google's own accelerator-optimized pricing page. Its region picker carries per-region
on-demand tables for **G2 in 41 regions and G4 in 24**. Every one of those 436 page prices is reproduced exactly from
the catalogue's components with the proven shapes. A few of them:

| Machine type | Region | Google's page | Assembled |
|---|---|---|---|
| g2-standard-4 | europe-west4 (Netherlands) | $0.742916046 | $0.742916046 |
| g2-standard-4 | us-east4 (Virginia) | $0.704517824 | $0.704517824 |
| g2-standard-4 | europe-west10 (Berlin) | $1.03546552 | $1.03546552 |
| g4-standard-48 | europe-west4 (Netherlands) | $4.949923 | $4.949923 |
| g4-standard-48 | asia-south1 (Mumbai) | $5.84959 | $5.84959 |

The page embeds A2 and A3 tables for Iowa only. Outside Iowa, an A2 or A3 price is the same proven shape assembled
from that region's own GPU, core, RAM and SSD SKUs, and Compute Engine confirms that shape in that region's zones.
No page check is possible for these. For example, a3-highgpu-8g in europe-west4 is $112.540479 an hour, against
$88.490000 in Iowa.

## Commitments

The GCP rows in `/v1/reserved` (1- and 3-year GPU commitment SKUs, GPU only) follow the same rule. Each row is in a
region where Compute Engine offers that GPU, either in a proven machine type or as an attachable GPU. On 2026-11-01
that was 258 rows in 34 regions, up from 22 in Iowa (`methodology/reserved-prices.md`).

## Calendar-mode advice (capacity): stored raw, not published

Google has no public stock API. `POST compute/v1/projects/{p}/regions/{r}/advice/calendarMode` takes N VMs of a shape
and a time window. It answers where and when a **future reservation** would be recommended: a recommended zone with
start and end times, and for every other zone `RECOMMENDED`, `NO_CAPACITY`, `CONDITIONS_NOT_MET` or `NOT_SUPPORTED`.
That is calendar-mode reservation availability, not on-demand stock. It is advice only: the call creates nothing, and
this account cannot create a reservation.

The poller asks about 4 targets: a3-highgpu-8g and a3-megagpu-8g in us-central1, and a3-ultragpu-8g and
a3-highgpu-8g in europe-west4. Each asks for one VM, starting 1 to 14 days ahead, for 1 to 7 days. It asks every 6
hours, and each answer is stored raw with its request.

**Finding, 2026-11-01:** every advice call for this project returns HTTP 400 "The service is not available for this
project.". That held for every A3 shape and region tried, for a TPU request, in v1 and beta, and for the separate
capacity advisor (`advice/capacity`). It is a state of the account, not a capacity answer. A new project has no
high-end GPU quota (A100/H100 quota 0, Spot CPUs 0), and the service appears to need a project with GPU quota or
billing history. While that answer persists, the poller asks once a day with one target, so the failure is recorded
without degrading the provider's health. No capacity rows are produced. The documented answer shape is parsed
(`calendar_advice()`), but advice will not be mapped to `/v1/capacity` until real answers have been seen.

## Secrets

The key JSON is a `SecretStr`. The OAuth assertion is signed locally with `cryptography`, so no new dependency was
added, and is sent only to `https://oauth2.googleapis.com/token`. A key naming any other token URI is refused. Tokens
are held in memory only. Compute calls use their own HTTP client: the Billing API key never goes to Compute Engine,
and the bearer token never goes to the Billing API. Stored endpoints are templates (`{project}`), and zone URLs are
cut to the zone name, so the project id stays out of stored payloads. Errors carry only an HTTP status.
