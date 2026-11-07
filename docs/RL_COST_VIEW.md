# RL cost view: spec

Status: proposal (2026-11-06). Nothing here is built.

## The question it answers

"What does one reinforcement-learning (RL) post-training job cost, end to end, and where should each piece run?"

The true-cost estimator (`cost/estimate.py`, methodology/full-cost.md) prices one GPU type at one provider. An RL job is
three different purchases with different hardware needs. The RL view prices each one separately, picks the cheapest
place for each, and adds them up.

## Background: the shape of an RL job

One RL step has three parts:

1. **Rollouts.** The model makes many attempts at tasks inside RL environments (a sandbox with a task and a grader).
   This is inference: limited by memory bandwidth and context length, not FLOPS. It is often most of the compute, and
   it survives interruption, because a lost rollout is just generated again.
2. **Grading.** Graders run code and tests in sandboxes, check answers, or call a reward model. Mostly CPU and RAM.
3. **Training update.** The gradient step on a tightly connected trainer (NVLink within a node, InfiniBand/RoCE across
   nodes). New weights are then sent back out to the rollout workers.

Teams increasingly split these parts: a small premium trainer, plus many cheap or interruptible rollout GPUs, sometimes
at other providers. No one publishes what that split costs. OpenGrid already has the prices and hardware specs to work
it out.

## Inputs (the "RL job")

| Field | Meaning | Example |
|---|---|---|
| `trainer_gpu`, `trainer_count` | Canonical GPU and GPUs per trainer node | `NVIDIA H100 80GB SXM5`, 8 |
| `trainer_nodes` | Trainer nodes (more than 1 requires a node fabric) | 1 |
| `rollout_gpus` | Rollout GPUs wanted, or `null` with `rollout_tokens_per_hour` | 32 |
| `rollout_gpu` | Canonical GPU, or `"any"` (ranked by bandwidth per dollar, see below) | `any` |
| `min_rollout_vram_gb` | Must hold the policy weights plus KV cache | 80 |
| `sandbox_vcpu_per_rollout_gpu` | CPU the environments need per rollout GPU | 16 |
| `sandbox_ram_gb_per_rollout_gpu` | RAM the environments need per rollout GPU | 64 |
| `weights_gb` | Size of one weight sync | 140 |
| `syncs_per_hour` | How often weights are sent to rollout workers | 4 |
| `hours` | Length of the run | 72 |
| `storage_gb` | Checkpoints and data, at the trainer | 5000 |
| `allow_interruptible_rollouts` | Rollouts may use spot/interruptible listings | true |

Reference presets, as with R0 to R3:
- **RL1, small lab:** 1×8 H100 trainer, 32 rollout GPUs, 72 h.
- **RL2, rollout-heavy:** 1×8 H100 trainer, 128 rollout GPUs, any GPU type with at least 48 GB.
- **RL3, single cluster:** everything on one 8-GPU node (the baseline the split is compared with).

## Formula

```
trainer   = existing estimate() for (trainer_gpu, trainer_count, hours, storage_gb, egress of the weight syncs)
            on demand, not interruptible; trainer_nodes > 1 requires node_fabric_gbps >= min_fabric_gbps
rollouts  = sum over the chosen rollout listings of billed_hours x price per GPU-hour x GPUs taken
            x interruption_overhead          (1.0 until measured; see open questions)
sandbox   = 0 when the rollout listings' own vcpu/ram_gb meet the per-GPU need,
            else the shortfall in vCPU-hours and GB-hours x sandbox price (UNKNOWN until sandbox providers exist)
sync      = weights_gb x syncs_per_hour x hours, as egress at the trainer's provider (tiered, as in full-cost)
            + ingress at the rollout providers (normally $0, stated per provider)
total     = trainer + rollouts + sandbox + sync
```

The existing honesty rules apply unchanged:
- A component the job uses that a provider does not publish is UNKNOWN, never $0. The total becomes a lower bound
  (`total_is_lower_bound`), and lower bounds rank after complete totals.
- List prices only. This is an estimate, not a quote.
- The output never feeds listings, indices, spreads, percentiles or routing.
- Listings marked `tax_inclusive` (e.g. Sakura) are flagged, not compared like for like.

## Choosing the rollout GPUs

Rollouts are bandwidth-bound, so candidates are ranked by **memory bandwidth per dollar**:

```
score = hardware.spec(gpu).memory_bandwidth_tbps / price per GPU-hour
```

Rules:
- Only canonical GPUs whose `hardware.spec` states bandwidth. Unmapped names are never ranked.
- VRAM must be at least `min_rollout_vram_gb`.
- In stock or stock unknown, seen within the provider's staleness window (as in full-cost).
- Interruptible listings are allowed only when `allow_interruptible_rollouts` is true, and are shown as interruptible.
- Fill greedily from the best score until `rollout_gpus` are taken, using the capacity each listing states. When
  capacity is unknown, take at most one listing's `gpu_count` from it and mark the row "capacity not stated".
- Vast is priced from real offers (the "cheapest" row), never the median row, as in full-cost.

## Output

```
{
  "job": {...},
  "split": {
    "trainer":  {provider, listing, specs (gpu_link, node_fabric), cost, unknown: []},
    "rollouts": [{provider, listing, gpus_taken, interruptible, bw_per_dollar, cost}],
    "sandbox":  {covered_by_rollout_hosts: bool, shortfall_vcpu, shortfall_ram_gb, cost | null, unknown: []},
    "sync":     {gb_total, egress_cost, unknown: []},
    "total", "total_is_lower_bound", "per_hour"
  },
  "single_cluster": {... RL3 priced the same way ...},
  "saving_vs_single_cluster": {usd, pct} | null,      // null when either side is a lower bound
  "notes": [...]
}
```

The web page puts the split next to the single-cluster baseline: one bar per part, with the same UNKNOWN and lower-bound
badges as the true-cost page.

API: `POST /v1/costs/rl-estimate` and `GET /v1/costs/rl-presets`, gated like the existing cost routes in
`api/pages.py`, and added to the `web/pages/api.js` catalogue (enforced by `test_developer_docs`).

## What it needs that we don't have yet

1. **Sandbox and CPU pricing.** A new provider type for CPU sandbox platforms (e.g. Modal, E2B, Daytona) and plain CPU
   VMs: price per vCPU-hour and per GB-hour, billing increment, cold-start. Same terms check as any new provider. Until
   then, a sandbox shortfall is UNKNOWN.
2. **Node fabric coverage.** `listing_specs` already returns `node_fabric` / `node_fabric_gbps`, but only for some
   providers. Multi-node trainers can only be priced where it is stated.
3. **Interruption rates.** The uptime tab (UptimeProject) measures control-plane APIs, not spot reclaims, so it can't be
   used. Until there is a measured reclaim rate, `interruption_overhead` is 1.0 and the output says "interruption cost
   not included".
4. **Cross-provider ingress.** Most clouds price ingress at $0, but this has to be recorded per provider, not assumed.

## Out of scope

- Throughput modelling (tokens per second per GPU, per model). The user gives `rollout_gpus`. A tokens-per-hour input
  would need measured benchmarks per GPU and model, which OpenGrid doesn't have.
- Pricing RL environments themselves (the environment and dataset vendors).
- Booking or routing anything. This is a read-only estimate.

## Build order

1. Rollout ranking and the split total from existing data (no sandbox pricing; shortfall shown as UNKNOWN). Fixture
   tests for the ranking, capacity filling, and lower-bound ordering.
2. API routes and presets, then the web page with the single-cluster comparison.
3. Sandbox/CPU providers.
4. Interruption overhead, once a measured reclaim rate exists.
