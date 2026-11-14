# Workload intelligence review (2026-11-13)

A review of how OpenGrid works out what a customer is running and which GPU configuration and provider suit it,
followed by the improvements made in this pass. Nothing that already worked was rebuilt or renamed. Every API
change adds fields. The internal-only data tier is still never exposed. No performance figure is presented as a
benchmark unless it was measured on a real OpenGrid job.

Status: code and tests written. The tests were **not run** in this pass, and nothing was launched, benchmarked or
deployed. The [validation checklist](#validation-checklist) at the end lists what must be run and checked.

## What existed

| Area | Where | What it did |
|---|---|---|
| Request contract | `intelligence/requirements.py` | `WorkloadRequirements`. Every field is optional and typos are rejected (`extra="forbid"`): model, training, inference, constraints, hardware hints, evidence. |
| Memory estimator | `intelligence/memory.py` | Weights, gradients, optimizer, activations (Korthikanti 2022), KV cache and framework overhead. ZeRO 0-3 sharding, CPU offload, tensor parallelism for inference, a 20% margin, per-GPU need at 1/2/4/8 GPUs, a confidence level and every assumption. |
| Model catalog | `intelligence/models_catalog.py` | Published configs for Llama, Mistral, Qwen, Gemma, Phi, BERT, ResNet, ViT and SDXL, each with a source URL. |
| Estimate | `intelligence/estimate.py` | `needs_gpu` only on evidence, questions only for unknowns that change the answer, training work (FLOPs). |
| Theoretical performance | `intelligence/performance.py` | Interconnect class from the spec sheet. Scaling-efficiency table per class. Work = 6/4/8 x N x T. MFU 35% (20-50%). Runtime and whole-job cost ranges. |
| Recommender | `intelligence/recommend.py`, `api/recommend.py` | Hardware filter, smallest fitting count plus one 2n scale-up per GPU model, read-only market ranking (`routing.scoring.rank_listings`), value score, tiers, the picks `cheapest_market` / `best_for_workload` / `best_executable` / `lowest_estimated_job_cost`, a one-line `summary` per option, exclusions with codes. |
| Market factors | `routing/scoring.py`, `routing/reliability.py` | Price against the median, availability, freshness, 30-day persistence, price stability, integration level. A transaction-based reliability score once a provider has 10+ OpenGrid launches. |
| Full cost | `cost/estimate.py`, `cost/manual.py` | Compute + egress + storage per listing for reference jobs. Node fabric (InfiniBand / RoCE / EFA) from provider documentation. Not connected to recommendations. |
| Local analyzer | `cli/src/opengrid_cli/analyze/` | Reads allowlisted files only (never executes code), redacts secrets, flags prompt injection. Extracts model, batch, sequence, ZeRO, LoRA, epochs, GPU count and dataset samples. |
| Assistant / MCP | `assistant/tools.py`, `cli/src/opengrid_cli/mcp_server.py` | The same engine, public tier only. |
| Jobs and runner | `runner/mnist_train.py`, `jobs/sync.py`, `store/jobs.py` | Per-epoch `epoch_seconds`, `train_samples` and `samples_per_second` from the runner, stored in `job_metrics`. The source is `runner` or `simulated`. |
| Measurements table | `store/intelligence.py` (migration 0020) | `performance_observations`, which was empty. Nothing wrote to it. |
| UI | `web/pages/find.js` | One recommended option, alternatives, performance across GPUs, and theoretical runtime and job cost. |

## What was missing or wrong

1. **No measured feedback loop.** Completed real jobs recorded their timings, but nothing turned them into
   performance data. The recommender's `_benchmarks` lookup matched observations by *workload class* only. Once the
   first MNIST run was recorded, every `training_full` recommendation on that GPU (a 70B fine-tune included) would
   have lost its "no measured benchmark" warning.
2. **Whole-job cost was theoretical only.** Provider startup time was never counted, and there was no deadline.
3. **Inference performance constraints were ignored.** `target_tokens_per_s` and `target_latency_ms` were accepted
   and then unused. There was no throughput estimate, so inference was ranked by $/hour alone.
4. **Multi-GPU used the spec sheet only.**
   - PCIe cards that support NVLink bridges were assumed to have them fitted.
   - The listing's own evidence (an SXM / NVSwitch board, InfiniBand) was ignored.
   - No parallelism strategy, no communication estimate, and no "do not go multi-GPU because...".
5. **Explanations were one sentence.** There was no confidence level, and no factor-by-factor comparison with the
   recommended option.
6. **Workload analysis.** A run bounded by `max_steps` (the common HF Trainer setup) could not be costed. The
   runnable MNIST template was unknown to the catalog, so its memory estimate was a low-confidence lower bound.
7. **Picks could disappear.** With a small `limit`, a pick could push another pick out of `options`.

## What changed

### Measured performance (`intelligence/measured.py`, migration `0038_measured_performance`)

- **Hook.** `jobs/sync.complete()` calls `measured.record_safely(job_id)` after a real (non-sandbox) job succeeds.
  It never raises. For past jobs, run `python -m intelligence.measured backfill`. The hook lives in `jobs/sync.py`
  because `jobs/orchestrator.py` is reserved for the batch fan-out branch.
- **What counts as a measurement.**
  - The job is not sandbox, its outcome is `succeeded`, and its `real_metrics` check passed.
  - Every metric row has source `runner`; one `simulated` row disqualifies the job.
  - The selection names the provider and canonical GPU.
  - If the runner reported the GPU name, the model token must match as a whole word (an L40 run is never recorded
    as an L40S).
  - At least one epoch has both a duration and its work.
  - Anything else is skipped with a reason code.
- **What is recorded.** Throughput over the timed epochs, dropping the first epoch (warm-up) when there are two or
  more. Setup overhead = time from initializing to `training_completed`, minus the timed epochs. Data kind
  `measured`, source kind `completed_job`. The partial unique index allows one row per job.
- **Consent.**
  - Runs by OpenGrid itself (operator) are consented.
  - Customer runs are recorded with `consent=false` unless the account opted in (`accounts.settings
    ["share_performance_measurements"] = true`).
  - Only consented rows are ever read back.
  - The response never names an account, a job id or a provider: the provider list becomes a count, plus whether
    this option's provider is one of them.
- **Use in recommendations.**
  - Measurements are matched by *workload signature*: `template:pytorch-mnist`, or
    `model:<catalog id>|<method>|<precision>`, plus GPU and count. A measurement is never borrowed from another
    workload.
  - With a measurement, an option gets `performance.measured` (n, median and range of throughput, age, confidence)
    and, when the request states its data size in the measured unit, a measured runtime and
    `performance.job_cost_measured`.
  - Ranking then uses the measured figure, and the notes and pick reasons say "measured". The theoretical figures
    stay beside it.
  - Measurements older than 180 days are not used.
- **Confidence.** High needs 3 or more runs, the newest within 30 days. Otherwise medium.
- **Zero data.** With no data (production today), nothing changes, nothing is labelled measured and nothing is
  invented.

### Completion-time-aware whole-job cost (`recommend._job_view`)

- **What options are ranked on.** One whole-job figure per option:
  - runtime (measured, else theoretical);
  - plus the provider's median time to capacity, when `routing/reliability` has the minimum sample of OpenGrid
    launches;
  - billed in the provider's unit at the instance price.
- **`cost.estimated_total`.** Shows the total, its range, completion hours, the runtime kind, whether startup and
  setup are included, and what is still unknown (storage, egress, provisioning or setup when not measured).
  `total_is_lower_bound` is always true.
- **New pick.** `shortest_estimated_runtime`, never called "fastest".
- **New constraint.** `constraints.deadline_hours` gives each option `meets_deadline: yes | likely | no | unknown`.
  An option that misses a stated target (deadline, tokens/s, latency) on its own estimate is not recommended while
  another option meets it (new rule `meets_targets`).
- **New request field.** `training.max_steps`: with `global_batch_size` and `seq_len` it gives the tokens trained.

### Hardware suitability (`intelligence/workload_fit.py`)

- **Interconnect evidence** (`performance.interconnect.evidence`).
  - The GPU link comes from the listing when the listing says so (SXM / HGX / DGX / NVSwitch / NVLink / OAM), else
    from the spec sheet. "PCIe" in a listing never changes it: it names the form factor.
  - Per-direction link bandwidth: half the published NVLink total, or PCI-SIG x16 rates.
  - Node fabric (InfiniBand / RoCE / EFA / TCPX) from listing text (whole words) or `cost/manual.FABRIC`. Unknown
    stays `null`.
  - A bridge-capable PCIe card whose listing does not mention NVLink gets the limitation "bridges are optional...
    without them the GPUs talk over PCIe only".
- **Parallelism** (`performance.parallelism`): `ddp`, `zero1`, `zero2`, `fsdp` or `tensor_parallel`, with the
  launch flags (torchrun / DeepSpeed / vLLM `--tensor-parallel-size`).
- **Communication vs compute per step** (`performance.communication`, training on 2+ GPUs).
  - Ring-collective volumes: DDP all-reduce; FSDP all-gather x2 + reduce-scatter.
  - Peak link bandwidth gives a lower bound on communication time, compared with the step's compute time at the
    central MFU.
  - A ratio of 1 or more adds a "communication-bound on paper" limitation.
- **Inference roofline** (`performance.inference`).
  - Decode tokens/s per stream and in aggregate: per-GPU weights plus KV reads at 60% of peak memory bandwidth
    (range 40-80%, a stated planning assumption), times tensor-parallel efficiency, capped by compute.
  - Checked against `target_tokens_per_s` (read as aggregate output tokens/s) and `target_latency_ms` (with
    `output_tokens`).
  - Cost per million output tokens at full utilization.
  - Prefill is not modelled, and the output says so.
- **Multi-GPU advice** (top-level `multi_gpu`).
  - Verdict: `single_gpu`, `scale_up_optional` or `multi_gpu_required`.
  - `when_not_to_go_multi_gpu`: fits on one GPU, small model, ZeRO-0, serving, PCIe-only.
  - The scale-up pairs: the same GPU and provider at n and 2n, with speedup range, link, strategy and whole-job cost
    ratio.

### Explanations and confidence (`intelligence/explain.py`)

- **`options[].decision.confidence`.** high / medium / low. It is the weakest of:
  - **memory fit:** the estimate's own confidence, at most medium under 10% headroom;
  - **runtime:** measured = medium or high, theoretical = low, unknown = not applicable;
  - **price freshness;**
  - **reported stock.**

  Reliability is shown but only lowers the level once a real score exists. Every component names its evidence kind:
  measured, observed, estimated or transaction.
- **`options[].decision.vs_recommended`.** Factor by factor: whole-job cost, completion hours, $/h, headroom,
  effective throughput, decode tokens/s, reported stock, listing score, GPU link and executable, each with
  `better: this | recommended | same | unknown`.
- **Top-level `explanation`.** The recommended option, the rule and basis that chose it, its confidence, and up to
  three alternatives with what each wins and loses on.
- **Data visibility.** `routing/disclosure.py` strips every comparison factor that could reveal an internal
  option's price, stock or score, rebuilds the explanation when the recommended option is internal, and drops
  multi-GPU cost ratios that involve an internal option.

### Smaller fixes

- The catalog has the MNIST template network (1,199,882 parameters, activation elements computed from the layer
  shapes) under the alias `pytorch-mnist`.
- A pick never pushes another pick out of `options`.
- `/find` shows "Measured runtime" with a `measured` badge when one exists, plus a Confidence fact. The schema
  mirror accepts `max_steps` / `deadline_hours`.

## What remains limited

- **No measured data exists yet.** Every runtime in production stays theoretical until real jobs complete. The
  only runnable workload is the MNIST template, so for now measurements will only ever describe that signature.
  LLM signatures need a runnable LLM template whose runner emits `train_tokens` / `tokens_per_second`.
- **No opt-in control for customers.** Customers have no UI or API to opt in to sharing; it is an account setting
  only the operator can change today.
- **The planning assumptions are still assumptions.**
  - Scaling efficiencies, MFU and decode bandwidth share are not measured.
  - The communication estimate ignores compute/communication overlap and protocol overhead.
  - MoE communication uses total parameters.
  - Prefill and batching gaps are not modelled.
- **Totals are lower bounds.**
  - Storage and egress are not in recommendation totals: the request has no storage or egress fields, though
    `cost/estimate.py` could price them.
  - Provisioning time is counted only once a provider reaches the reliability minimum sample, and no provider has
    yet.
- **Not covered.** Spot capacity is not ranked. Multi-node training is not planned. Node fabric is known only for
  the SKUs in `cost/manual.FABRIC`.
- **The local analyzer does not emit `training.max_steps` yet.** The server must ship first, because
  `extra="forbid"` rejects unknown fields on an older server. The CLI's human-readable output does not show the new
  pick.
- **Migration 0038** (renumbered at merge) is chained after 0037_job_resume_move.

## Competitors

| | How it picks hardware (per its public docs) | What OpenGrid does differently |
|---|---|---|
| **SkyPilot** | The user names the accelerator (`accelerators: A100:8`), or a set or ordered list (`any_of` / `ordered`). The optimizer minimises cost (default) or time (`OptimizeTarget.COST` / `TIME`) across clouds and regions, with automatic failover on capacity or quota errors. Without a user-supplied time estimator the optimizer assumes 1 hour of runtime (`sky/optimizer.py`), so "time" has no workload model behind it. A separate benchmark tool reports estimated hours and cost from trial runs the user starts. Sources: [API reference](https://docs.skypilot.ai/en/latest/reference/api.html), [auto-failover](https://docs.skypilot.ai/en/latest/examples/auto-failover.html), [optimizer source](https://github.com/skypilot-org/skypilot/blob/master/sky/optimizer.py), [benchmark](https://skypilot.readthedocs.io/en/latest/reference/benchmark/index.html). | OpenGrid infers the GPU and count from the model: a VRAM estimate with sharding, a hardware filter and a scale-up comparison. It estimates runtime per GPU from the workload (theoretical, or measured from real jobs, labelled), ranks by whole-job cost, and explains every alternative with a confidence level. SkyPilot's strength is execution (launch, failover, managed jobs) across many clouds. |
| **Shadeform** | `GET /v1/instances/types` filters by cloud, region, `gpu_type`, `num_gpus` and availability. `sort=price` sorts by hourly price and "removes nothing". Each type carries `interconnect` (pcie / sxm), an `nvlink` flag and `vram_per_gpu_in_gb`. No InfiniBand field was seen, and the instance-types reference takes no workload or model input. Source: [instance types API](https://docs.shadeform.ai/api-reference/instances/instances-types). | OpenGrid takes the workload as input, not the GPU. It checks memory fit at each count and models the interconnect's effect on scaling and communication, including whether NVLink bridges are confirmed. It adds node fabric where documented, ranks by whole-job cost and completion time instead of $/h, and covers 59 monitored clouds with freshness and reliability factors. Only the instance-types page was reviewed, so a Shadeform recommendation feature elsewhere in its product is not ruled out. |
| **"CIQ"** | **Unverified which company is meant.** The likeliest is CIQ (Ctrl IQ, the Rocky Linux company) and its Fuzzball platform. It runs AI / HPC workflows across AWS, GCP, OCI, CoreWeave, Azure and on-prem. Fuzzball 4.3 (October 2026) deploys validated models from a catalog and does health-aware scheduling across NVIDIA and AMD fleets the customer already has. Sources: [HPCwire, Fuzzball 4.3](https://www.hpcwire.com/off-the-wire/ciqs-fuzzball-4-3-runs-validated-open-weight-models-on-a-customers-gpus-with-one-command/), [ITBrief, five clouds](https://itbrief.co.uk/story/ciq-expands-fuzzball-to-span-five-clouds-on-prem). Nothing found shows cross-provider price comparison, VRAM sizing or provider selection by cost; one trade summary mentions planned cost optimization, which could not be confirmed. | If Fuzzball is meant: it orchestrates work on GPUs a customer already has. OpenGrid chooses which GPUs and provider to rent, and what that will cost. **Confirm with the founder which "CIQ" was meant before citing this externally.** |

What would make OpenGrid clearly better than all three, in order:

1. Real measured runs feeding recommendations. The loop above is ready, and it needs live MNIST runs and then an
   LLM fine-tune template.
2. Storage and egress fields in the request, priced through `cost/estimate.py`.
3. A customer opt-in for sharing measurements.
4. Measured provider time to capacity once launches accumulate.

## Validation checklist

Owner: Fawwaaz. Nothing below was run in this pass.

**1. Static checks.**

```
.venv/bin/python -m py_compile intelligence/*.py routing/disclosure.py jobs/sync.py store/intelligence.py migrations/versions/0038_measured_performance.py
node --check web/pages/find.js && node --check web/core.js
```

**2. New test file.** `.venv/bin/python tests/test_workload_intelligence.py` runs pure checks, then a scratch
database.

Pure checks:

| Test | Expected |
|---|---|
| `test_link_bandwidth` | H100 SXM 450 GB/s per direction; PCIe Gen4 31.5 GB/s; unknown PCIe generation stays `None`. |
| `test_interconnect_evidence` | NVSwitch text gives a listing-sourced NVLink link. "PCIe" text never changes a bridge. `bare-metal-infiniband` gives fabric `infiniband`. "processor" is not RoCE. |
| `test_communication_hand_computed` | FSDP on 2 x H100 moves 24e9 bytes per GPU per step, 0.0533 s at 450 GB/s, against about 1.51 s of compute: compute-bound. DDP moves 16e9 bytes. PCIe gives a worse ratio. Missing batch or sequence gives `known: false`. |
| `test_inference_roofline_and_targets` | 16 GB of weights on 3.35 TB/s at 60% gives about 125.6 tok/s per stream. Concurrency raises the aggregate. An impossible target gives `meets_target: false`. |
| `test_observation_rules` | Each skip reason: `sandbox`, `not_succeeded`, `no_real_metrics`, `simulated_metrics`, `no_selection`, `gpu_mismatch`, `no_timed_epochs`, `no_metrics`. Throughput is 2000 samples/s with the 45 s warm-up epoch dropped. Overhead is 400 - 105 s. |
| `test_disclosure_of_new_blocks` | With an internal recommended option, only safe factors remain. The explanation has no rule and no confidence. Cost ratios involving it are nulled. |

Database checks:

| Test | Expected |
|---|---|
| `test_db_zero_data_is_graceful` | Nothing is labelled measured, no estimated total exists, and both job picks are `null` with reasons. |
| `test_db_record_jobs` | Only `job-real`, `job-cust` and `job-cust-ok` are recorded. Backfill skips the simulated-metrics and wrong-GPU jobs and never scans sandbox jobs. A second `record_job` gives `already_recorded`. Lookup returns n=2 (consented only). |
| `test_db_recommend_prefers_measured` | The L4 option carries `performance.measured` (no provider names), a measured runtime, `job_cost_measured` and the `lowest_estimated_job_cost` and `shortest_estimated_runtime` picks, with "measured" in the reason and notes. The T4 option still says "no measured benchmark". |
| `test_db_deadline_and_explanation` | A tiny deadline flags L4 `meets_deadline: "no"`. Others are `"unknown"`. The explanation is consistent with `best_for_workload`. |
| `test_db_llm_scale_up_blocks` | An 8B LoRA run gives an H100 1 -> 2 pair with `fsdp`, a known communication block and a listing-sourced NVLink link. |
| `test_migration_0038` | Upgrade adds the columns and the unique index. Downgrade to 0037 removes them and keeps the 0020 columns. |

**3. Regression files that must stay green.** These files touch changed code:

```
.venv/bin/python tests/run_all.py recommend intelligence visibility assistant jobs web_find mcp_remote workloads
```

Watch especially:

- `test_recommend.test_j_two_h100_against_one_h200`: the job-cost pick now goes through `x["job"]`, which is
  identical with no startup data.
- `test_recommend` picks / limit 2: a pick never displaces another pick, so `options` stays at 3 or fewer.
- `test_visibility.test_customers_never_see_internal_values`: the new blocks must not change when internal prices
  change.
- `test_jobs*`: the completion hook must never break completion. It now writes `performance_observations` rows for
  real succeeded jobs in those scratch databases.
- `tests/test_web_find.js`: the existing runtime-label assertions.

**4. Staging migration.** Run `alembic upgrade head`, check
`\d performance_observations`, then `alembic downgrade -1` and `upgrade head` again.

**5. Live checks, on the first real (non-sandbox) MNIST job:**

1. After `training_completed`, `SELECT * FROM performance_observations WHERE job_id = '<job>'` has exactly one row:
   - `source_kind = completed_job`, `data_kind = measured`, `consent = true` (an operator run);
   - `gpu` and `provider` match the job's selection, and `gpu_detected` is the nvidia-smi name;
   - `throughput` is about the mean of `samples_per_second` over epochs 2..n in `/v1/jobs/<id>/metrics`;
   - `overhead_s` is plausible (setup + dataset + upload, minutes).
2. A sandbox job gives no row. Re-running `python -m intelligence.measured backfill` records nothing new
   (idempotent).
3. `POST /v1/recommend` with `{"workload_type":"training","model":{"name":"pytorch-mnist"},"precision":"fp32",
   "training":{"epochs":3}}`:
   - the measured GPU's option shows `performance.measured.n = 1`, confidence `medium` and a measured runtime;
   - the notes say MEASURED;
   - `/find` shows "Measured runtime" with the `measured` badge.
4. Compare the measured runtime with the next real run's wall-clock (initializing -> training_completed). Record the
   error in the job notes; this is the first calibration point for the theoretical model.
5. Never seed `performance_observations` by hand in production. Test rows must stay in scratch databases.

## Addendum (2026-11): audio, image and video workloads

The same engine now sizes speech recognition (Whisper, wav2vec2), text-to-speech (Parler-TTS, Kokoro),
vision-language models (LLaVA, Qwen2-VL incl. video input, Llama 3.2 Vision, PaliGemma) and video generation
(CogVideoX, Wan2.1, HunyuanVideo) through an optional `media` object. The formulas, sources and limits are in
[recommendations](/methodology/recommendations#audio-image-and-video-workloads). Requests without `media` are
unchanged: `tests/test_media_workloads.py` compares 24 such payloads byte for byte with the earlier engine.

Validation checklist:

1. **Whisper transcription:** `POST /v1/recommend/estimate` with `{"workload_type":"batch","model":{"name":
   "openai/whisper-large-v3"},"media":{"total_audio_hours":10000}}`.
   - `media.inputs.total_windows` is 1,200,000.
   - The decoder caches are in `kv_cache`.
   - `runtime.estimable` is true.
2. **Video generation:** the same call for `tencent/HunyuanVideo` with `"media":{"task":"generation"}`.
   - `memory.confidence` is `low`.
   - About 75 GB with margin, which keeps only 80 GB-class GPUs.
3. **Recommendation:** `POST /v1/recommend` with the first body.
   - Every option shows `performance.media.throughput` (audio hours per GPU-hour, a range) and a theoretical
     runtime and job cost.
   - A 2-GPU option is `replicas`, and a note points to batch fan-out.
4. **Measured first:** once real measurements of a media workload exist, the measured runtime replaces the
   theoretical one for that GPU, exactly as for training.
