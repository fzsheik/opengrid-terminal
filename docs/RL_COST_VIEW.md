# RL cost view: spec

Status: proposal (2026-11-06). Nothing here is built. Market figures were gathered on 2026-11-06 and are labelled by how
far they can be trusted. Sources are at the end.

## Summary

- **What:** a page and API that price a reinforcement-learning (RL) post-training job as the three things it actually
  buys: a trainer cluster, a fleet of rollout GPUs, and CPU sandboxes. It finds the cheapest place for each and compares
  the split with running everything on one cluster.
- **Why now:** RL post-training has gone from a few percent of frontier-lab compute in early 2024 to an estimated 40-55%
  in 2026. Most of an RL job's compute is rollouts: inference-type work that runs fine on cheaper, interruptible GPUs.
  That moves real spend onto the marketplace and spot capacity OpenGrid already tracks.
- **Why us:** the answer needs prices across many providers plus the hardware behind them (memory bandwidth, VRAM,
  interconnect, billing increments, egress). OpenGrid already collects all of this. Nobody publishes this view; teams
  work it out in private spreadsheets.
- **Who pays:** RL-environment companies, smaller labs and post-training teams, and neoclouds that want to sell rollout
  capacity.
- **First version:** can be built from existing data (the trainer and rollout split). Sandbox pricing and interruption
  rates come later.

## Plain-English explainer

**RL post-training.** Once a model is pretrained, labs improve it by letting it practise. It tries a task, gets a score,
and training nudges it towards whatever scored well. This is how the "reasoning" and coding-agent models are trained.

**RL environment.** The practice world: a task, a place to act, and an automatic grader. Examples: "fix this failing
test in this repo" (a container with a codebase and a terminal), "book a flight" (a simulated website), or a math
problem with a checkable answer. The grader is the hard part. If it can be gamed, the model learns to game it ("reward
hacking").

**Why the compute is different.** One RL job is three workloads with different hardware needs:

| Part | Like... | Needs | Can be interrupted? |
|---|---|---|---|
| Rollouts (the model attempting tasks) | inference | memory bandwidth, VRAM for long contexts | yes: just retry |
| Grading (run the code, check the answer) | ordinary servers | CPU and RAM, fast container start | yes |
| Training update (the gradient step) | classic training | tightly connected GPUs (NVLink, InfiniBand) | no |

So the cheapest way to run RL is usually *not* one big premium cluster. It's a small premium trainer plus as much cheap
rollout and CPU capacity as you can find. Finding it means comparing many providers, which is OpenGrid's job.

## Why it matters: the market

### RL's share of compute is large and rising

No lab publishes its compute split, so every figure here is an outside estimate. They agree on the direction:

| Estimate | Figure | Trust |
|---|---|---|
| SemiAnalysis (as reported by OfficeChai) | early 2024: ~67% pre-training, ~5% post-training/RL, ~29% inference; more than half of combined OpenAI + Anthropic compute projected for post-training/RL | medium: secondary report of a paid analysis |
| Exobrain | RL ~4% of lab compute in early 2024 to ~53% by Q3 2026; inference about a third throughout | low-medium: modelled |
| Attributed to Epoch AI (via Startup Fortune) | post-training/RL ~42% of frontier *training* compute in Q1 2026, against 23% for pre-training | low: could not find it on Epoch's site |
| Epoch AI, March 2026 | final training runs are under a quarter of total AI R&D compute; most goes to experiments and data generation | medium |
| DeepSeek R1 (2025, via an arXiv RLHF book) | ~5% of compute in post-training | medium: older baseline |

**Inside an RL job, rollouts dominate.** One open-model report put rollouts at 43.8% of the bill, gradient steps at
43.5% and graders at 12.7%. Other reviews put rollout generation at over 90% of RL *runtime*. Either way, a large share
of RL spend is inference-type work that doesn't need the premium cluster.

### The environment business is a market of its own

| Claim | Figure | Trust |
|---|---|---|
| Lab budget for environments (The Information, via TechCrunch, Sept 2025) | Anthropic leaders discussed spending **over $1B on RL environments** in the following year | medium: reported, not confirmed |
| Price per environment (vendor blogs) | ~$200-2,000 per standard task; ~$300K to build a full "company replica" environment; top vendors on seven-figure quarterly contracts, smaller labs ~$300-500K a quarter | low |
| Surge AI | ~$1.2B revenue in 2024 (Bloomberg, via TechCrunch); has set up an RL-environment team | medium |
| Mercor | valued at $10B (TechCrunch, 2025); pitching environments for coding, healthcare and law; reportedly bought Sepal AI (Feb 2026) | medium for valuation, low for the rest |
| Mechanize | builds coding environments; offering $500K salaries to environment engineers; reported to have worked with Anthropic | medium |
| Prime Intellect | open "Environments Hub" (2,500+ environments, per secondary sources); also sells compute | medium |
| Whole sector (job-board tally, July 2026) | ~$8.5B combined revenue, ~$100B combined valuation | low: method unknown |

### The sandbox layer (the CPU side of RL)

| Company | Figure | Trust |
|---|---|---|
| Modal | $355M Series C at a $4.65B valuation (May 2026), ~$300M annualised revenue growing ~5x; later reported in talks for ~$750M at ~$15B | medium (round), low (later talks) |
| Daytona | $24M Series A (Feb 2026); one report of $60M at $1B (Oct 2026); ~850K sandboxes a day by May 2026 | medium (A), low (rest) |
| E2B | $21M Series A (July 2025); passed one billion sandboxes | medium |
| OpenAI Agents SDK (April 2026) | lists Daytona, E2B and Modal as sandbox providers | medium |

Not all sandbox demand is RL training (much of it is agents running in production), but RL graders use the same
product.

### The GPU rental market it sits on

- The neocloud market is estimated at ~$35B in 2026, up from ~$24B in 2025 (Mordor Intelligence, via secondary coverage). Forecasts for 2031
  range from ~$236B to over $400B. Low-medium trust: research-firm forecasts.
- CoreWeave: Q2 2026 revenue ~$2.6B, more than double a year earlier. Nebius: Q1 2026 revenue $399M. Check both
  against their SEC filings before quoting them externally.

### What this means for OpenGrid

The figures above are what customers spend on compute and environments. OpenGrid doesn't sell compute. Its opportunity
is the pricing and decision data around that spend:

- **It's big enough to matter.** If even a few percent of RL spend can move from premium clusters to cheaper rollout
  capacity, the saving at a single customer is large compared with the price of a data subscription. For comparison,
  Synoptic is talking about $20K/month for a GPU price feed.
- **Nobody publishes this view.** The cost of a split RL job across providers isn't public. It's worked out privately,
  and often badly, because egress, billing increments and interconnect are missed.
- **We already have most of the inputs.** Prices, hardware specs (bandwidth, VRAM), interruptible flags, CPU and RAM per
  listing, egress and storage components, billing increments, and node fabric for some providers.

We don't put a dollar figure on OpenGrid's addressable market here. The inputs are too uncertain to multiply together
honestly.

## Who would use it

| Customer | What they want from it | How they'd pay |
|---|---|---|
| RL-environment companies (Mechanize, Prime Intellect, the data-labelling firms' environment teams) | what it costs to run their environments at scale; quoting labs a price for "environment + compute" | data subscription or API |
| Smaller labs and post-training teams (startups, open-model teams, enterprise fine-tuning) | where to put the trainer and the rollouts; the cheapest split | API, true-cost page, alerts |
| Neoclouds and GPU marketplaces | how their spot and interruptible capacity compares as rollout capacity; a reason to market to RL teams | provider-side data product |
| Investors and analysts | how RL demand affects the price of non-premium GPUs | market-context data, reports |

Frontier labs build this internally, so they aren't the first target.

## Competition and alternatives

- **In-house spreadsheets.** The real competitor. Free, but stale, and usually missing egress, billing increments and
  interconnect.
- **GPU price trackers and indices** (Silicon Data and others) publish prices per GPU type. None that we know of price a
  multi-part job or rank GPUs by bandwidth per dollar for rollouts.
- **Compute brokers and marketplaces** (Prime Intellect, SF Compute, Vast and others) will quote their own capacity, not
  a neutral comparison across providers.
- **Sandbox vendors** publish their own prices only.

We have not done a full competitor review. That is a next step.

## Detailed design: the question it answers

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

## Next steps

Sizes are rough (S is about a day, M a few days, L a week or more). Each step ships on its own.

### Phase 0: check the demand (before building much)
1. **Talk to 5-10 likely users** (RL-environment startups, small post-training teams, one or two neoclouds). Ask how
   they work out RL compute cost today, what they get wrong, and whether they'd pay for this. Size S, ongoing.
2. **Price one real RL job by hand** with today's data (e.g. preset RL1) as a worked example for those conversations,
   and to check the method. Size S.
3. **Full competitor review:** GPU indices, brokers, sandbox vendors and any RL-cost calculators. Size S.

### Phase 1: first version from existing data
4. **Rollout ranking and split total** in `cost/` (pure functions, like `estimate()`): bandwidth-per-dollar ranking,
   capacity filling, trainer via the existing estimator, sync egress. Sandbox shortfall shown as UNKNOWN. Fixture tests
   for ranking, capacity filling, lower-bound ordering and tax-inclusive flagging. Size M.
5. **API routes and presets:** `POST /v1/costs/rl-estimate`, `GET /v1/costs/rl-presets`, gated in `api/pages.py`,
   added to the `web/pages/api.js` catalogue. Methodology page `methodology/rl-cost.md`. Size S.
6. **Web page:** the split shown next to the single-cluster baseline, with UNKNOWN and lower-bound badges. Size M.

### Phase 2: fill the gaps
7. **Sandbox/CPU providers:** Modal, E2B and Daytona public pricing, plus plain CPU VMs from providers we already
   poll. Terms check for each, like any new provider. Size M-L.
8. **Node fabric coverage:** record interconnect for more multi-GPU listings, so multi-node trainers can be priced.
   Size M.
9. **Ingress per provider:** record it rather than assume $0. Size S.

### Phase 3: make it precise
10. **Interruption rates:** measure how often spot and interruptible capacity is taken back (from our own probes or
    provider data). Until then the output says "interruption cost not included". Size L.
11. **Throughput option:** accept tokens per hour instead of GPU count, only once measured per-GPU, per-model benchmarks
    exist. Size L.

### Decisions needed
- Is this public (free page, lead generation) or a paid API feature, or both?
- Which customer group to aim at first (recommended: RL-environment companies and small post-training teams).
- Whether to add sandbox providers as a general data product (they're useful beyond RL).

## Risks

- **Estimates, not quotes.** List prices miss negotiated discounts, which big RL buyers get. Say so on the page.
- **The interruption cost is missing** until it's measured. Interruptible rollouts may look cheaper than they are.
- **Thin interconnect data** limits multi-node trainer pricing at first.
- **The market figures are mostly secondary.** Check them against primary sources before using them in a pitch.
- **Splitting across providers costs engineering time.** That isn't priced. The page should say the saving is
  before engineering cost.

## Sources

Gathered 2026-11-06. Trust labels in the tables above.

- TechCrunch, "Silicon Valley bets big on 'environments' to train AI agents" (2025-09-21):
  https://techcrunch.com/2025/09/21/silicon-valley-bets-big-on-environments-to-train-ai-agents/
- OfficeChai, on SemiAnalysis's compute split:
  https://officechai.com/ai/while-share-of-inference-has-held-steady-over-the-last-2-years-pre-training-compute-has-moved-to-post-training-semi-analysis/
- SemiAnalysis, "RL Environments and RL for Science": https://newsletter.semianalysis.com/p/rl-environments-and-rl-for-science
- Exobrain, "The rise of post-training": https://www.exobrain.co.uk/insights/the-rise-of-post-training
- Startup Fortune (Epoch AI figures):
  https://startupfortune.com/ai-labs-are-now-spending-more-compute-teaching-models-to-think-than-to-learn/
- Epoch AI on R&D compute (secondary):
  https://www.pulse.bot/ai/blogs/final-training-runs-account-for-a-minority-of-rd-compute-spending-fbbad151-db55-475e-98ec-b46b4fd0df71/
- "Reinforcement Learning from Human Feedback" (arXiv book) (DeepSeek R1 figure): https://arxiv.org/pdf/2504.12501
- "Where Should RL Post-Training Compute Go?" (rollout/gradient/grader split): https://arxiv.org/html/2607.13389v1
- Wing VC, "Who Will Win the RL Environment Market": https://www.wing.vc/content/who-will-win-the-rl-environment-market--and-why
- CleverX, environment pricing: https://cleverx.com/blog/companies-building-rl-environments-2026/
- Patronus AI, RL environment companies: https://www.patronus.ai/resources/rl-environment-companies
- Sector tally (low trust): https://aitraining.jobs/roles/agentic-eval
- Sandbox funding: https://bex.co/blog/2026/09/11/ai-sandbox-funding-modal-daytona-e2b and
  https://alleywatch.com/2026/02/daytona-ai-agent-infrastructure-sandbox-computing-developer-tools-ivan-burazin/ and
  https://techaiwire.com/articles/daytona-60-million-series-b-1-billion-ai-agent-sandboxes/
- Neocloud market: https://www.nextplatform.com/cloud/2026/08/14/the-neoclouds-build-cash-hoards-faster-than-revenues/5288040
  and https://www.century.ae/en/investment-insights/neocloud-buildout-scaling-ais-compute-frontier/
