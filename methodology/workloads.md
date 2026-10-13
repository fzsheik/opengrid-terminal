# Workloads: templates, requirements and workload-aware selection

Code: `workloads/` (`spec.py`, `templates.py`, `hardware_req.py`, `select.py`). Tests: `tests/test_workloads.py`.

A **job** is a user's workload and its outcome. This document covers what a job may ask for, which GPUs can
run it, how OpenGrid picks one, and how the cost bound is computed. Selection never calls a provider: it reads
OpenGrid's observed listings, the adapter capability matrix and the execution control plane.

## 1. Allowlisted templates

Only allowlisted templates run in this release. Nobody can send code, a command, a container image or a
dataset URL. The only template is **`pytorch-mnist`** ("Train Your First AI Model — PyTorch MNIST"):

| | |
|---|---|
| Framework | PyTorch 2.4.1, torchvision 0.19.1, CUDA 12.1 wheels from `download.pytorch.org/whl/cu121`, installed into a fresh venv. If that install fails, the bootstrap may fall back to the image's preinstalled PyTorch and records which one ran. |
| Dataset | MNIST (60k train / 10k test) from torchvision's default mirror, checked with torchvision's built-in MD5s. `dataset_ref` can only be omitted or `"mnist"`. |
| Model | a small CNN (2 conv + 2 fully-connected layers), Adam, cross-entropy |
| Config | `epochs` 1..10 (default 3), `batch_size` 32..512 (default 128), `learning_rate` 1e-5..1e-1 (default 1e-3), `seed` 0..2^31-1 (default 42). Unknown keys are refused. |
| Artifacts | model.pt, config.json, metrics.json, env.json, stored by OpenGrid (`artifact_destination` must be `"opengrid"`) |
| Limits | `max_runtime_minutes` 5..60, and never above the platform's `RUNTIME_HARD_MAX_MINUTES`; `max_price_per_gpu_hour_usd` and `max_total_cost_usd` must be > 0 (≤ 100). All three are required. |

**Founder preset** (`template.preset`): name `opengrid-first-training-test`, gpu `auto`, 1 GPU, region US
(non-strict), strategy BALANCED, no spot, $3/GPU-h max, $2 total max, 30 min max runtime.

**Expected runtime** (kind **estimated**, not a benchmark): setup 3–8 min (boot, venv, downloading and
installing the pinned wheels) + overhead 1.5–2.5 min (dataset, artifact upload, reporting) + 0.5–1.5 min per
epoch. With the default 3 epochs that is **6–15 min**. Environment setup dominates and depends on the
provider's image and network. If the top of the band is longer than `max_runtime_minutes`, validation adds a
note: the job may be stopped at the ceiling.

## 2. Request validation (`workloads.spec.validate`)

The request shape is in `WorkloadSpec` (unknown fields are refused). Every refusal is a `WorkloadError` with
a machine-readable `code`, a `message` and the offending `field`. The API returns these as HTTP 422
`{code, message}`.

| code | when |
|---|---|
| `missing_field` / `unknown_field` / `invalid_request` | the shape is wrong (pydantic) |
| `invalid_job_type` | job_type is not one of the five in the schema |
| `unsupported_workload_type` | `finetuning`, `inference`, `batch_compute` or `custom_container`: accepted by the schema, not supported in this release |
| `unknown_template`, `template_job_type_mismatch` | the template is not allowlisted, or is for a different job type |
| `invalid_config`, `config_out_of_bounds` | wrong type, unknown key or out of bounds |
| `gpu_count_out_of_bounds` | anything other than 1 GPU for pytorch-mnist |
| `limit_out_of_bounds` | runtime outside 5..min(60, hard max); a price or budget ≤ 0 |
| `spot_not_supported` | `allow_spot: true` (spot is refused in this release) |
| `cuda_required` | `cuda_required: false` for a CUDA template |
| `invalid_region` | a region that is not a region group, or `strict_region` without a region |
| `unknown_gpu`, `gpu_family_not_allowed` | an unknown GPU, or a family such as "h100" (its variants are different products: pick one, or `auto`) |
| `gpu_incompatible` | an explicit GPU that fails the requirements (raised by selection, with the reason) |
| `invalid_strategy` | an unknown strategy, USER_DEFINED without weights, or weights with another strategy (scoring's own rules) |
| `ssh_private_key_rejected`, `ssh_public_key_invalid` | the key is a private key or is not a valid OpenSSH public key |
| `unsupported_artifact_destination`, `dataset_not_allowed` | anything except `opengrid` / the template's own dataset |

Optional minimums (`min_vram_gb`, `min_vcpu`, `min_ram_gb`, `min_disk_gb`) can raise the template's minimums
but never lower them. A lower value gets a note and the template's value applies.

## 3. Hardware requirements → eligible GPUs (`workloads.hardware_req`)

pytorch-mnist requires:

- an **NVIDIA** GPU with CUDA
- **compute capability ≥ 7.0** (Volta or newer)
- **compute capability ≤ 9.0**: the pinned torch 2.4.1+cu121 build ships kernels for sm_50…sm_90 only. CUDA
  12.1 is older than Blackwell, which needs a CUDA 12.8+ build. Raising this limit means changing the
  framework pin in the runner, not just this number.
- **≥ 8 GB VRAM**. The real need is well under 1 GB; 8 GB is a deliberately generous margin for the CUDA
  context, cuDNN workspaces and large batch sizes.
- **1 GPU**
- an **x86_64 host**. Grace (ARM) superchips such as GH200 are excluded: the pinned wheels have not been
  verified there.
- **≥ 4 vCPU, ≥ 16 GB RAM, ≥ 30 GB disk**, checked only where the listing reports the value. An unreported
  value is shown as unknown and is not a reason to exclude.
- **on-demand** capacity only.

`gpu: auto` checks every canonical GPU with `hardware.spec()`. With an explicit GPU, only that one is checked,
and an incompatible choice is refused with the reason. Every variant is checked on its own facts; variants are
never treated as identical. A GPU with no hardware entry, an architecture missing from the table below, or
unknown VRAM is **excluded** with "hardware spec unknown". It is never assumed compatible.

Compute capability by architecture (sources: NVIDIA, *CUDA GPUs – Compute Capability*,
https://developer.nvidia.com/cuda-gpus; *CUDA C++ Programming Guide*, "Compute Capabilities"):

| architecture (hardware.py) | rule | CC | pytorch-mnist |
|---|---|---|---|
| Pascal | P100 6.0, other GP10x 6.1 | 6.0 / 6.1 | excluded (< 7.0) |
| Volta | GV100 (V100) | 7.0 | eligible |
| Turing | TU10x/TU11x (T4, RTX 20, GTX 16, Quadro RTX) | 7.5 | eligible if ≥ 8 GB |
| Ampere | A100 / A30 (GA100) 8.0; others (GA10x: A10, A10G, A40, RTX A-series, RTX 30) 8.6 | 8.0 / 8.6 | eligible if ≥ 8 GB |
| Ada Lovelace | AD10x (L4, L40, L40S, RTX Ada, RTX 40) | 8.9 | eligible if ≥ 8 GB |
| Hopper | GH100 (H100, H200; GH200 also needs an x86 host) | 9.0 | eligible (GH200 excluded: ARM host) |
| Blackwell | B200/GB200 10.0; RTX 50 / RTX PRO Blackwell 12.0 | 10.0 / 12.0 | excluded (> 9.0 for the cu121 build) |
| Blackwell Ultra | B300 / GB300 | 10.3 | excluded (> 9.0) |
| AMD (CDNA / RDNA), Intel Gaudi | not CUDA | — | excluded (wrong vendor) |

## 4. Selection (`workloads.select.select(spec, who, *, now=None)`)

1. Validate the request; derive the effective requirements.
2. Resolve the eligible GPUs (section 3). GPUs without any listing are reported with `listings: 0`.
3. For each eligible GPU, run the existing best-execution ranking `routing.scoring.rank_listings` with
   count = gpu_count, region, max price, mode = strategy, weights and strict_region. That function applies the
   market eligibility rules: on-demand only, not interruptible, priced, not sold out, not stale (seen within
   `market.stale_after(provider)`), the right GPU count, the region, the max price. Its exclusions are kept,
   with their codes and reasons. Multi-instance shapes are excluded (`multi_instance`).
4. Apply the workload filters: vCPU, RAM and disk where the listing reports them (`insufficient_vcpu`,
   `insufficient_ram`, `insufficient_disk`). Then the cost bound (section 5), which excludes with
   `over_budget`.
5. **Cross-GPU price factor.** `rank_listings` scores price against *one GPU's* market median. Merging those
   scores across GPUs as they are would let a popular, expensive GPU (an H100 sitting at the H100 median)
   tie a cheap compatible GPU. For a job any compatible GPU can run, that is wrong. So the merged list
   re-bases the price factor on the **median compatible-GPU price for this workload**: the median, over
   compatible (GPU, provider) pairs, of each pair's lowest candidate price. It uses the same
   `0.5 − pct vs median` rule, clamped to 0..1. Every other factor (availability, freshness, history, region,
   integration level) is `rank_listings`' own. The score is recomputed with the strategy's weights, and the
   explanation is scoring's own text. The GPU's own market median is kept in `factors.price.raw.gpu_market_median`.
6. Order the list. CHEAPEST: by price, then data age, GPU, provider, listing id. Every other strategy: by
   score, then price, age, GPU, provider, listing id. The order is deterministic.

## 5. Cost bound

```
billed_minutes  = max( ceil(runtime_ceiling / unit) × unit , minimum_commitment )
est_max_total   = price_per_gpu_hour × gpu_count × billed_minutes / 60
est_typical     = the same, using the template's expected runtime band (capped at the ceiling)
```

- `runtime_ceiling` is `guards.runtime_ceiling(account, max_runtime_minutes)`, the auto-terminate deadline
  every deployment gets (the request, clamped by the account and system maximums).
- `unit` and `minimum_commitment` come from the adapter's `CAPABILITIES.billing_unit` and
  `minimum_commitment`. Values in use today: per minute (Lambda, Hyperstack), per second (RunPod, Vast,
  Shadeform, DigitalOcean with a 60-second minimum charge), 10-minute prepaid blocks (Verda, rounded up as a
  conservative bound).
- A provider with **no adapter**, or an unrecognised unit, is assumed to bill **per hour**. A commitment that
  cannot be read is assumed to be one hour. Each candidate's `cost.billing_assumed` / `billing_basis` says so.
- The price is the **observed market price**, not a quote. The route issues a quote and re-validates it live
  before any launch.
- A candidate whose `est_max_total` exceeds `max_total_cost_usd` is excluded (`over_budget`). The reason shows
  the price, GPU count, billed hours and budget.

Example: a $2.50/GPU-h V100 with a 30 min ceiling costs at most $1.25 when billed per minute, but $2.50 on a
per-hour provider. The second is over a $2 budget and is excluded.

## 6. Market best vs execution ready

- **`market_best`**: the top-ranked compatible listing that passes every workload constraint (hardware,
  region, price, resources, budget), **whether or not OpenGrid can run it**. It is market intelligence.
- **`execution_ready`**: the top-ranked candidate OpenGrid can actually launch now, or `null`. A candidate is
  executable only if **all** of these hold. Each failure becomes a reason with a code:

| check | code when it fails |
|---|---|
| effective mode SUPERVISED or LIVE (`control.effective_mode`) | `mode_preview_only`, `mode_disabled` |
| env ceiling `ROUTING_LIVE_PROVISIONING` on | `env_ceiling` |
| an adapter exists, integration level ≥ 2 | `not_provisionable` |
| `adapter_status == validated` (only a recorded validation cycle sets it) | `adapter_not_validated` ("\<p\>: adapter simulated — run the supervised validation first") |
| `supervised_enabled` (or `live_enabled` / `supervised_enabled` in LIVE) | `supervised_not_enabled`, `provider_not_enabled` |
| provider not killed (global kill = mode DISABLED) | `provider_killed` |
| `CAPABILITIES.startup_script == YES` (the job bootstraps through it) | `no_startup_script` |
| `CAPABILITIES.forces_account_ssh_key == NO` | `forces_account_ssh_key` |
| credentials resolve (BYO first, fail closed; else OpenGrid-managed); checked without side effects | `no_credentials` |
| per-deployment SSH key registration on OpenGrid's account (the job's customer or ephemeral key) | `ssh_key_registration_unsupported` |
| the engine's own launch-spec rules (`engine.launch_spec_for` + the adapter's `missing_launch`) | `missing_launch_parameters`, ... |
| `control.launch_permission(provider, 'customer')` agrees | `launch_not_permitted` |

An unvalidated adapter is never `execution_ready`, but it can be `market_best`. `alternatives` are the next
three executable candidates. If there are none, they are the next market candidates, labelled
`not executable`. When nothing is executable, `blockers` lists why: deduplicated by (code, provider), global
reasons first, market-only providers collapsed into one entry. Each blocker is actionable. When no candidate
exists at all, a single `no_eligible_listing` blocker summarises the exclusions by code and gives hints that
match them (budget, price, region, stale data, sold out).

`execution_ready.route_spec` is the request to hand to the existing route path: gpu, count, region group,
strict_region, max price, mode, weights, `preferences.include_providers=[provider]`, max_runtime_minutes,
duration_hours = billed hours, and the listing id. The route re-ranks within that provider and GPU, so it may
pick a different listing of the same provider and GPU (another region, say). Its quote and live
re-validation are authoritative. `execution_ready.limit_violations` is the account cost-guard check, for
information only; the route re-checks it atomically.

## 7. Output (stable keys)

`template{id, version, title, expected_runtime_minutes, expected_runtime_kind}`, `request` (normalised),
`requirements`, `eligible_gpus[{gpu, slug, vendor, architecture, compute_capability, compute_capability_basis,
vram_gb, form_factor, listings, candidates, market}]`, `ineligible_gpus[{gpu, slug, code, reason}]`,
`gpu_selection` (auto | explicit), `candidates_total`, `market_best`, `execution_ready`, `alternatives`,
`exclusions[{provider, gpu, gpu_slug, listing_id, region, price_per_gpu_hour, code, reason}]` (sorted, capped
at 300), `exclusions_total`, `exclusions_by_code`, `blockers[{code, reason, provider? | providers?}]`,
`estimate{for, provider, gpu, price_per_gpu_hour, price_kind, gpu_count, billed_minutes, est_max_total,
est_typical_total, est_typical_range, typical_minutes, billing_unit, billing_assumed, max_total_cost_usd, kind,
basis}`, `strategy`, `weights`, `workload_market{median, pairs, rule}`, `runtime` (the guards ceiling),
`execution_mode`, `live_provisioning_enabled`, `sandbox`, `sandbox_note`, `notes`, `as_of`,
`market_data_freshness{newest_observed_at, oldest_observed_at, selected_age_seconds,
max_candidate_age_seconds, stale_excluded, rule}`, `price_concepts`, `methodology`.

A candidate has: rank, provider, provider_display, listing_id, sku, gpu, gpu_slug, gpu_count, region,
country, price_per_gpu_hour, price_per_instance_hour, price_kind, available, observed_at, age_seconds,
integration_level, provisionable, score, factors, explanation, `resources{vcpu, ram_gb, disk_gb, unknown,
note}`, `cost{...}` (section 5), `executable`, `readiness{ready, reasons[{code, reason, provider?}],
approval_mode, approval_note, provider_flags, credential_source}`, plus `why` on market_best /
execution_ready, `vs_selected` + `label` on alternatives, and `route_spec` on executable candidates.

Data kinds: candidate prices are observed market prices, `cost` / `estimate` are **estimated**, and the
expected runtime is **estimated**. A quote exists only after the route issues one.
