# RL cost: same cluster or split across clouds

An RL post-training job runs three kinds of work: a trainer that updates the policy, rollout workers that generate
attempts with the current weights, and sandboxes or graders that score them. The estimator prices that job two ways:
everything on one provider, or the trainer on one provider and the rollouts wherever eligible GPUs are cheapest. The
split plan also pays to move the weights to the rollout workers after every update. The estimator shows both totals,
the best plan it found, and the transfer price or sync rate at which splitting stops saving money.

Kind: **estimated**. Every input price is labelled **observed** (a current listing or a provider price API), **manual**
(a hand table with a source page and a date), **user_input**, **inferred** or **assumption**. Code: `cost/rl.py`,
built on the [full-cost estimator](/methodology/full-cost). API: `POST /v1/costs/rl-estimate`,
`GET /v1/costs/rl-presets`. Page: `/rl-cost`.

These are list prices, not quotes. Nothing here enters listings, indices, spreads, percentiles, settlement or routing.

## Inputs

| Field | Meaning | Default |
|---|---|---|
| `trainer.gpu`, `trainer.count`, `trainer.nodes` | trainer GPU, GPUs per node, nodes | 1 node |
| `rollouts.gpu` | a GPU, or `auto` (any GPU that passes the VRAM gate) | `auto` |
| `rollouts.count` | rollout GPUs in total | required |
| `rollouts.min_vram_gb` | memory each rollout GPU needs (model shard + KV cache) | 0 |
| `rollouts.interruptible` | spot / interruptible listings allowed for rollouts | false |
| `rollouts.interruption_overhead` | multiplier on interruptible rollout cost for lost and retried work | 1.15 |
| `rollouts.rank_by` | fill rollouts by `price` (per GPU-hour) or `bandwidth_per_dollar` | `price` |
| `sandboxes` | `{vcpu, ram_gb, count, price_per_hour}` per sandbox, or null | null |
| `weights_gb` | weights sent per sync, decimal GB | required |
| `syncs_per_hour` | weight syncs per hour | required |
| `sync_fanout` | copies of each sync that leave the trainer's provider | one per rollout provider |
| `hours` | job length | required |
| `storage_gb` / `storage_tb`, `storage_class` | checkpoints kept on the trainer's provider | 0, `auto` |
| `same_cluster_only` | price only the one-provider plan | false |
| `preset` | `RL1`, `RL2` or `RL3`, then any field overrides it | none |

Why these defaults:
- **Interruption overhead 1.15.** OpenGrid does not measure how often spot machines are reclaimed. 1.15 means 15% of
  interruptible rollout GPU time is lost or redone. It is a planning assumption, applied only to interruptible
  listings. Change it, and read the sensitivity table.
- **Fanout.** By default, each sync sends one copy to each rollout provider other than the trainer's, and that
  provider shares it internally. If your workers each pull their own copy, or you relay between regions, set
  `sync_fanout` to the real number of copies.

## Formula

For one plan (a trainer listing plus a set of rollout listings):

    trainer   = nodes x billed hours x price per instance-hour
                billed hours: the job's hours rounded up to the provider's billing increment (full-cost method)
    storage   = stored GB x price per GB-month x hours / 730, on the trainer's provider
    rollouts  = sum over rollout listings of machines x billed hours x price per instance-hour x overhead
                overhead = interruption_overhead for an interruptible listing, 1 for on-demand
    sandbox   = 0 if the rollout machines publish enough vCPU and RAM for every sandbox (bundled, not free)
                else your price_per_hour x hours, else UNKNOWN
    per_dest  = weights_gb x syncs_per_hour x hours
    transfer  = per_dest x fanout
    sync      = transfer through the trainer provider's internet egress tiers (allowance first)
                + each destination's share (transfer / destinations) through its ingress tiers
    total     = trainer + storage + rollouts + sandbox + sync

Rollouts on the trainer's own provider are assumed to sync over the cluster's private network at $0. This line is
labelled `assumption`. It holds when they share a region. Inter-region or inter-zone transfer inside one provider is
not priced.

Rules:

- **Unknown is never $0.** A component the plan uses that has no checked price is named in `unknown`, for example
  `sync_egress:verda`, `sync_ingress:aws` or `sandbox`. The total covers only the known lines, and
  `total_is_lower_bound` is true.
- **Whole machines.** Rollout GPUs are bought in whole machines. The last machine may carry spare GPUs, and you pay for them.
- **Listed capacity.** Where a listing publishes capacity (GPUs or instances), the fill never takes more. Where it
  does not, the plan assumes the provider can supply the GPUs and says so.
- **Eligible rollout listings:**
  - GPU memory at least `min_vram_gb`, from the hardware table;
  - on demand, or spot when allowed;
  - priced, and not sold out;
  - not a "from" price, and not Vast's median row;
  - seen within the provider's staleness window.
- **Eligible trainer listings** follow the full-cost rules: exactly `count` GPUs per machine, on demand, not
  interruptible, deployable on demand. With more than one node, a provider that does not sell the machine as a
  multi-node fabric is left out. A provider whose fabric is not published is kept, flagged and ranked after the known ones.

## Strategies

| Strategy | Plan |
|---|---|
| `same_cluster` | For every trainer provider that can also host every rollout GPU: trainer and rollouts there. The cheapest one. This is the RL3 baseline. |
| `split` | The cheapest trainer (compute + storage) and the cheapest eligible rollout listings anywhere, filled in order. |
| `best` | The cheapest plan found over: every trainer provider x (its own cluster; the global rollout fill; its cluster plus one other provider; one other provider alone). |

Plans rank complete totals first, then lower bounds. A lower bound could be dearer than it shows, so it never
outranks a complete total. When a lower-bound plan shows a smaller total than `best`, it is returned as
`cheapest_lower_bound`, so you can see it without OpenGrid claiming it is cheaper.

The saving is `same_cluster.total - split.total`. Its `bound` says how far to trust it:
- `exact`: both totals are complete;
- `at_most`: the split total is a lower bound;
- `at_least`: the baseline total is a lower bound;
- `indeterminate`: both are lower bounds.

## Break-even

The break-even holds every line fixed except moving the weights:

    margin                = same_cluster.total - (split.total - transfer cost)
    break-even $/GB       = margin / transfer GB
    break-even sync rate  = the transfer volume at which egress + ingress, through the real tiers, equals the margin,
                            divided by weights_gb x hours x fanout

- **Margin of zero or less.** Splitting costs more even if transfer were free.
- **Unpublished egress or ingress price on the route.** Only the $/GB limit is given.
- **Free transfer.** When transfer is free at any volume, splitting saves at any sync rate.

The **sensitivity table** recomputes the same-cluster and split plans for each pair of interruption overhead
(1.0, 1.15, 1.3, 1.5 and yours) and syncs per hour (1, 2, 4, 8 and yours).

## Rollout GPU ranking

`rollout_ranking` lists the eligible rollout GPU models by **memory bandwidth (TB/s, vendor peak) per dollar per
GPU-hour**, with the cheapest eligible listing of each. Token generation at small batch is often limited by memory
bandwidth, so this is a reasonable first heuristic. It is **theoretical, not measured throughput**: model,
quantization, tensor parallelism, batch size, context length and software change the real ranking. The rollout GPU
count is your input; OpenGrid does not convert a tokens-per-hour target into GPUs.

## Worked example: RL1

RL1 is a 1 x 8 H100 SXM trainer, 32 rollout GPUs with at least 80 GB each (on demand), 72 hours, 140 GB of weights
synced 4 times an hour, and 2 TB of checkpoints. The prices below are illustrative list prices of the kind the
estimator reads; the live page uses current listings.

| Line | Same cluster (CoreWeave) | Split (CoreWeave trainer, RunPod rollouts) |
|---|---|---|
| Trainer | 72 h x $49.24 = $3,545.28 | $3,545.28 |
| Storage | 2,000 GB x $0.07 x 72/730 = $13.81 | $13.81 |
| Rollouts | 4 machines x 72 h x $49.24 = $14,181.12 | 32 x 72 h x $2.69 = $6,197.76 |
| Sync | inside the cluster, assumed $0 | 140 x 4 x 72 = 40,320 GB: CoreWeave egress $0, RunPod ingress $0 |
| **Total** | **$17,740.21** | **$9,756.85** |

The split saves $7,983.36 (45%), and the saving is exact, since every line is priced.

Moving the trainer to a provider that bills egress changes the picture. AWS, for example, charges $0.09 per GB after
the first 100 GB and less in its higher tiers. Then 40,320 GB costs a few thousand dollars, and the break-even shows
how many syncs an hour you can afford before the split stops paying. Moving the trainer to a provider that does not
publish egress (Verda) makes the split total a lower bound; only the break-even $/GB is given.

The guide's 40.32 TB is one copy per destination. Two rollout providers double it, and the estimator counts that.

## Gaps

These apply to every estimate, and each response lists the ones that apply to it in `caveats`:

1. **Sandbox CPU prices** have no dedicated dataset. Sandboxes are bundled only when the rollout machines publish
   enough vCPU and RAM. Otherwise they are unknown unless you give a price.
2. **Node fabric** is incomplete, so whether a multi-node trainer can run well on a provider is not always known.
3. **Spot reclaim rates** are not measured. The interruption overhead is an assumption. Provider API uptime does not
   measure how often spot machines are reclaimed.
4. **Cross-cloud ingress** is recorded per provider as `ingress_internet`: free where the provider's page says so
   (Azure, Nebius, Hyperstack, Crusoe, RunPod, CoreWeave, Lambda, Voltage Park, DigitalOcean), per offer on Vast, and
   unknown elsewhere. AWS, GCP and Oracle are listed but **unverified**: their pages were rendered by script or
   refused the read in this pass. Their ingress is unknown in an estimate.
5. **Tokens per second** per model and GPU are not benchmarked. The bandwidth ranking is an approximation.
6. **Engineering overhead** of splitting is not included: latency, cross-cloud reliability, where checkpoints live,
   and keeping rollout workers on the right weights.
7. **List prices** do not reflect enterprise discounts.
8. **Weight sync** is modelled as full copies. Compression, partial or delta updates, and relays between workers
   change the real volume.

Other limits:
- Egress tiers and free allowances are monthly and assumed fully available to this job.
- Rollout-side storage, inter-zone transfer and public IPs are not priced.
- Rollouts are priced for the whole job length.
