# Full cost: egress, storage and billing beside the hourly price

The hourly price is not the cost of a job. OpenGrid publishes the other charges a GPU job runs into: internet egress,
storage and billing increments. It also publishes the spec fields that decide whether two listings are the same
product (GPU link, node fabric, deployable on demand), and an estimator that adds it all up for a reference job.
Unpublished components are named, never counted as $0.

Kind: **estimated** for the estimate; components are **observed** (from a provider API or price file) or **manual**
(a hand-maintained table with a source page and a verified-at date). Code: `cost/`. API: `POST /v1/costs/estimate`,
`GET /v1/costs/components`, `/v1/costs/billing`, `/v1/costs/sources`, `/v1/costs/presets`.

Nothing here enters listings, indices, spreads, percentiles, settlement or routing. Like
[reserved prices](/methodology/reserved-prices), it is computed on read and a test checks that nothing statistical
imports it.

## Where each number comes from

| Origin | Providers | How |
|---|---|---|
| Price API or file (`api`) | AWS (Data Transfer and S3 offer files, us-east-1), Azure (Retail Prices: Bandwidth and Storage, eastus), Oracle (the cetools price list, parts B88327, B91961/B91962, B91628), Verda (`/v1/volume-types`) | Fetched once a day, stored raw in `cost_snapshots` (failures too), cut to the products read. Parsed on read from the newest successful fetch. |
| Provider raw already stored (`provider`) | Hyperstack (pricebook: Cloud-SSD, ObjectStorage, PublicIP), Vultr (plan `bandwidth`, `deploy_ondemand`), Vast (each offer's `storage_cost`, `inet_up_cost`) | Read from the provider's own newest raw snapshot. No extra request. |
| Hand table (`manual`) | AWS EBS gp3 (its public price map hashes its keys), GCP egress and disks (the Catalog API's networking SKUs are not read yet), DigitalOcean, Vultr overage, CoreWeave, Lambda, RunPod, Nebius, Together, Crusoe, Hyperstack egress, Voltage Park, Lium | `cost/manual.py`. Every row has the page it was read from and the day (`verified_at`). |

A hand-table value is **checked** when it was read on the provider's own page, and **unverified** otherwise: a
search snippet, a docs example, or a unit (GB or GiB) the page leaves open. **Only checked values are used in an
estimate.** An unverified value is still listed in `/v1/costs/components`, but in an estimate its component is
unknown.

## Units

- 1 GB = 10^9 bytes. A price per GiB is divided by 1.073741824; a tier bound in GiB is multiplied by it. The published
  unit is kept as `raw_unit`.
- A month is 730 hours. A storage price per GB-hour is multiplied by 730.
- Request prices are per 1,000 requests.
- Transfer tiers are per month, as the providers bill them. A free allowance is a tier priced 0, and
  `allowance_scope` says whose it is:
  - `account_month`: AWS's 100 GB across all services, Azure's 100 GB, Oracle's 10 TB;
  - `team_pool_month`: DigitalOcean's pooled transfer.
- "Free" is an explicit 0 with its source. A component the provider does not publish is **absent**.

## Spec fields

Each listing in an estimate carries the following fields:

| Field | Meaning | From |
|---|---|---|
| `gpu_form_factor` | sxm / pcie / oam / superchip | [hardware](/methodology/hardware), by canonical GPU |
| `gpu_link` | GPU-to-GPU inside the machine: nvlink, nvlink_bridge, pcie, infinity_fabric (null for 1 GPU) | hardware + GPU count |
| `node_fabric`, `node_fabric_gbps`, `frontend_gbps`, `multi_node` | node-to-node network: infiniband, roce, efa, tcpx, ethernet | hand table, per SKU |
| `on_demand_deployable` | can be launched without a contract | Vultr `deploy_ondemand`; hand table |
| `included_egress_gb_month` | transfer included with the plan | Vultr `bandwidth` |
| `billing_increment_s`, `billing_minimum_s` | how compute time is rounded | hand table, per provider, with per-product overrides (Oracle bare metal: 1-hour minimum) |

These fields keep unlike products from being compared as if they were the same. Examples:
- DigitalOcean's 8x H100 has no RDMA fabric (25 Gbps private).
- Hyperstack's on-demand flavors use Ethernet up to 350 Gbps.
- An AWS p5 node has 3,200 Gbps EFA.

Vultr plans that are not deployable on demand are already left out of listings by the Vultr reader. The field still
shows for the plans that are listed.

## The estimate

Request: a GPU, a GPU count, hours, egress (TB or GB, decimal) and stored data (TB or GB), plus optionally:
- `storage_class`: `auto` (shared filesystem, then block, then object), or one class;
- `min_fabric_gbps`;
- a `preset`.

For every provider, OpenGrid prices one listing: its cheapest current listing of that GPU, with exactly that many GPUs
per machine. A listing qualifies if it is:
- on demand and not interruptible;
- not a "from" price;
- in stock, or stock unknown;
- seen within the provider's staleness window.

Vast's median row is not a machine anyone can rent, so Vast is priced from its cheapest row, which is one real offer,
using that offer's own storage and transfer prices.

    compute  = billed hours x price per instance-hour
               billed hours = hours rounded up to the billing increment, at least the minimum
    egress   = the job's egress GB through the provider's monthly tiers, after the plan's included transfer
    storage  = stored GB x price per GB-month x hours / 730, with the first known class in storage_class
    total    = compute + egress + storage
    effective_per_gpu_hour = total / (GPUs x hours)

Rules:

- **Unknown is never $0.** A component the job uses (egress > 0, storage > 0) without a checked price is listed in
  `unknown`. The total then covers only the known components, and `total_is_lower_bound` is true.
- **Billing increments.** If a provider's increment is not published or is unverified, compute uses the job's own
  hours and says so in `notes`. Every published increment divides an hour, so whole hours are exact anyway.
- **Unranked listings.** A listing that is not deployable on demand, or whose node fabric is below `min_fabric_gbps`
  or not published when a minimum is asked for, is returned in `unranked` with the reason. It is not ranked.
- **Ranking.** Ranked rows are sorted by total, complete totals first and lower bounds after them. A lower bound could
  be dearer than it shows, so it never outranks a complete total. The response counts how many rows are lower bounds.
- **Allowances.** Free allowances are assumed fully available to this job, as if nothing else on the account used them
  that month. So the estimate is the best case for the providers that have allowances.
- **Not a quote.** These are list prices. Nothing in the estimate is a quote or an execution price.

## Reference jobs

| Preset | Job |
|---|---|
| R0 | Inference box: 1x H100 SXM for 730 h, 1 TB egress, 0.5 TB stored |
| R1 | Training run: 8x H100 SXM for 72 h, 10 TB egress, 20 TB stored |
| R2 | Egress-heavy: 8x H100 SXM for 72 h, 50 TB egress, 20 TB stored |
| R3 | Short benchmark: 1x H100 SXM for 10 minutes (shows billing minimums) |

For R1, AWS p5.48xlarge at $55.04/h breaks down as:

| Part | Calculation | Cost |
|---|---|---|
| Compute | 72 h x $55.04 | $3,962.88 |
| Egress | 9,900 GB after the free 100 GB, x $0.09 | $891.00 |
| EBS gp3 storage | 20,000 GB x $0.08 x 72/730 | $157.81 |
| **Total** | | **$5,011.69** |

The extras add 26% to compute. At CoreWeave, egress is free, so the same extras add about 4%.

## Freshness and failures

- The machine-readable sources are fetched daily. Their lists change monthly at most.
- `GET /v1/costs/sources` shows each source's last attempt, last success and last error.
- A failed fetch is stored and changes nothing: the newest successful fetch is still used. A stored body that no
  longer parses produces no components and a note, never a guess.
- Hand-table rows carry the day they were read. Re-check them monthly.

## Known gaps

- Vast's storage and egress come from each offer's own `storage_cost` / `inet_up_cost` / `inet_down_cost`, which
  the Vast poller stores from 2026-11-02. Until the first poll after that deploy, and for any older snapshot, Vast's
  storage and egress read as unknown.
- GCP's networking and storage prices come from the hand table. The Cloud Billing Catalog API has them, but under
  service ids OpenGrid does not read yet.
- Verda does not publish egress prices, so Verda's egress is always unknown.
- Lambda shows its filesystem price only at creation (the docs' $0.20 is an example), so Lambda's filesystem cost is
  unknown.
- Inter-zone transfer, request charges and public IPs are listed but not used by the estimator, because a job does not
  declare them.
