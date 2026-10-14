# Workload recommendations

OpenGrid estimates how much GPU memory a workload needs from published formulas, then matches that estimate against fresh, observed on-demand listings. Every default it assumes is listed in the answer, runtime is never guessed, and a GPU is called "executable" only when OpenGrid can actually launch the workload for you. One engine on the server (`intelligence/`) answers the website, the assistant, the CLI and the MCP server alike. The contract is `docs/OPENGRID2_CONTRACTS.md`.

Endpoints: `POST /v1/recommend/estimate` (memory, GPU count, questions; pure computation) and `POST /v1/recommend` (the estimate plus ranked market options). Both take a `WorkloadRequirements` body (every field optional; unknown is `null`). Both are read-only: the recommendation reads listings with the same ranking `/v1/best` uses and writes no audit, quote or route rows. With `PUBLIC_PAGES` on they can be called without a key, under the anonymous rate limit.

## Memory estimate

Units: GB = 10^9 bytes. Vendor VRAM figures are GiB-sized, so comparing against them leaves a little extra room. The estimate is a sum of separate components. Each byte is counted once.

| Component | Formula | Source |
|---|---|---|
| weights | parameters x bytes per parameter: fp32 4, bf16/fp16 2, fp8/int8 1, int4/GPTQ/AWQ 0.5 + 20 bits per 128 weights (group-128 scale and zero), NF4 0.5 + 0.127 bits (double quantization). When weights are quantized and the vocabulary and hidden size are known, the embedding and output head (2 x vocab x hidden) stay 16-bit | [QLoRA](https://arxiv.org/abs/2305.14314) |
| gradients | trainable parameters only. Full training: every parameter at the weight dtype (2 B mixed precision, 4 B fp32). LoRA/QLoRA: adapter parameters x 4 B | [ZeRO](https://arxiv.org/abs/1910.02054) |
| optimizer | trainable parameters only. AdamW m + v = 8 B. 8-bit AdamW = 2 B. SGD momentum = 4 B. Adafactor's factored second moment is about 4 B x 2/hidden. Plus a 4 B fp32 master copy when the trainable weights are 16-bit. Full AdamW in mixed precision is 2 + 2 + 12 = 16 B per parameter | [ZeRO §3](https://arxiv.org/abs/1910.02054), [8-bit optimizers](https://arxiv.org/abs/2110.02861), [Adafactor](https://arxiv.org/abs/1804.04235) |
| activations (training, transformers) | per layer `s·b·h·(34 + 5·a·s/h)` bytes for 16-bit activations. With FlashAttention / PyTorch SDPA (the assumed default) the `5·a·s/h` attention-score term is not stored. Gradient checkpointing (full recomputation) keeps only each layer's input, `2·s·b·h` per layer, plus one layer being recomputed. Plus fp32 logits for the loss, `4·s·b·vocab`. fp32 activations double the body | [Korthikanti et al. 2022, eq. 1](https://arxiv.org/abs/2205.05198), [FlashAttention](https://arxiv.org/abs/2205.14135) |
| activations (CNNs) | activation elements per sample x batch x activation bytes. For ResNet-50 at 224x224 the catalog value (27,948,032 elements) is counted from the architecture: every conv, batch-norm, pooling and residual-add output, with ReLU done in place. The high end (37,556,736) also counts ReLU outputs. The same count reproduces torchvision's 25,557,032 parameters exactly | [ResNet](https://arxiv.org/abs/1512.03385) |
| kv_cache (inference) | `2 x layers x kv_heads x head_dim x context x concurrency x 2 B` (K and V, 16-bit cache) | n/a |
| framework_overhead | 1.5 GB fixed (CUDA context, cuBLAS/cuDNN workspaces) + 5% of the other components (allocator fragmentation), per GPU | n/a |

LoRA adapters are assumed on the q, k, v and o attention projections. An adapter on a `d_in x d_out` matrix adds `r x (d_in + d_out)` parameters, so per layer the adapters add `r·[(h + a·d) + 2·(h + kv·d) + (a·d + h)]` with grouped-query attention ([LoRA](https://arxiv.org/abs/2106.09685)). Adapters are kept in fp32, the PEFT default, so they need 4 B weights + 4 B gradients + 8 B AdamW = 16 B per adapter parameter. With r = 16, the adapters for an 8B model are about 14M parameters (0.2 GB), compared with about 112 GB of gradients and optimizer states (14 B per parameter) for full fine-tuning.

Inference activations are short-lived and small next to the weights and KV cache. The framework overhead covers them. vLLM reserves memory up to `gpu_memory_utilization` (0.9 by default), so for vLLM a GPU fits only if the need is at most 0.9 x its VRAM.

`min_total_gb` is the sum of the components, the theoretical minimum. `recommended_gb` adds a **20% safety margin** for what the formulas leave out: kernels' temporary buffers, framework version differences, dataloader pinned memory and fragmentation peaks. A GPU must hold `recommended_gb` to be offered.

`range_gb` is reported when inputs had to be assumed. The low and high ends recompute the estimate with the low and high values of every assumed input: micro batch 1-8 against the central 4, sequence length 512-4096 against 2048, context 2048-8192 against 4096, concurrency 1-8 against 1. When nothing was assumed, the range is `null`.

`confidence` is `high` when every input the formulas use was given, `medium` when defaults filled gaps, `low` when a component could not be estimated at all (the total is then a lower bound, `lower_bound: true`), when a model name matched several catalog entries, or when the parameter count was approximated from dimensions (`12·L·h² + V·h`, [Kaplan et al. 2020](https://arxiv.org/abs/2001.08361)). It is `none` when the parameter count is unknown. Then `method` is `unavailable`, no memory number is given, and the questions say what is missing.

## Model catalog

`intelligence/models_catalog.py` holds the published configs of well-known open models: Llama 2/3/3.1/3.2, Mistral 7B, Mixtral 8x7B, Qwen2.5 0.5B-72B, Gemma 2, Phi-3, GPT-2, BERT, ResNet-50, ViT-B/16 and SDXL. Each entry links its source (the config.json, model card or paper). A request value always wins over the catalog, and every value filled from the catalog is listed in `assumptions`. A loose name ("7B llama") matches on family, size and version. Several matches are listed and lower the confidence. A value that could not be confirmed is left empty.

## GPU count and interconnect

Per-GPU need is computed for 1, 2, 4 and 8 GPUs, or up to `constraints.max_gpus`.

- **Training:** sharding follows ZeRO. Stage 1 splits optimizer states across the GPUs, stage 2 also gradients, stage 3 also weights. Activations stay per GPU because each GPU has its own micro-batch. When the stage is not given and more than one GPU is needed, ZeRO-3 / FSDP full sharding is assumed. ZeRO stage 0 (plain data parallel) keeps a full copy of the model on every GPU, so adding GPUs never helps it.
- **Inference:** tensor parallelism splits the weights and the KV cache N ways.
- **CPU offload:** moves the optimizer states off the GPU.

The suggested count is the smallest one whose per-GPU need, with the margin, fits the largest whole GPU on record (MIG slices and laptops excluded). In the recommendation each GPU model gets its own smallest fitting count. More than one GPU for sharded training or tensor parallelism means GPU-to-GPU traffic on every step. NVLink / SXM / Infinity Fabric parts are preferred, and PCIe-only multi-GPU options carry a limitation and a lower workload score.

## From estimate to options

1. **Hardware.** Every canonical GPU is checked with `workloads/hardware_req.check_gpu`:
   - **Vendor:** must match the vendor constraint. A stated CUDA version means NVIDIA only.
   - **Compute capability (NVIDIA):** 8.0 for *stated* bf16, 8.9 for fp8.
   - **FP8 on other vendors:** the GPU must support FP8.
   - **Assumed precision:** never excludes hardware. A T4 still runs fp16 mixed precision.
   - **VRAM:** the GPU must hold the per-GPU need at its fitting count.
2. **Market (read-only).** `routing.scoring.rank_listings(gpu, count=n)` runs for each fitting GPU. The same rules as `/v1/best` apply: canonical, on-demand, priced, not sold out, seen within the provider's freshness window (`market.stale_after`), and exactly `n` GPUs per instance. Listings with unknown availability are kept and labelled "availability not reported by provider". Spot capacity is never ranked, even with `allow_spot`. Region, maximum price and provider include/exclude lists come from the request.
3. **Exclusions.** Everything left out is listed with a code:
   - `insufficient_vram`: too little memory even at the largest count.
   - `wrong_vendor`, `compute_capability_too_low`, `precision_unsupported`: hardware checks.
   - `no_fresh_listing`: the GPU fits but no fresh listing of the needed shape exists. The reason explains why that GPU count is needed.
   - `stale`, `sold_out`, `not_on_demand`, `over_max_price`, `wrong_region`, `excluded_by_preference`, `multi_instance`: market rules.
   - `over_budget`: price x GPUs x billed runtime hours is over `budget_usd`. Applied only when both `budget_usd` and `runtime_hours` are given.

   Other instance shapes of the same GPU are not reported.
4. **Workload score.** Each (GPU, count, provider, region) keeps its best listing. Its score is 0.55 x price against the median compatible instance price, plus 0.25 x the listing's own best-execution score (availability, freshness, history, integration), plus 0.20 x memory headroom (full credit at 40%). PCIe-only multi-GPU options lose 0.10.
5. **Picks.**
   - `cheapest_market`: the lowest instance price that fits.
   - `best_for_workload`: the top workload score among options with at least 10% headroom.
   - `best_executable`: the first option OpenGrid can launch for this caller (see below), often `null`.
6. **Tiers.** Tiers are relative to the balanced pick.
   - `higher_performance`: more than 10% higher theoretical memory bandwidth, or more VRAM at equal bandwidth.
   - `lower_cost`: at least 10% cheaper per hour.
   - `balanced`: everything else.

   Comparisons cite spec-sheet numbers and say they are theoretical. OpenGrid never calls an option "fastest".

## Cost

Costs are **scenarios**, not predictions. Each one is price per GPU-hour x GPUs x billed hours for 1, 10 and 100 hours, plus the stated `runtime_hours` if given. Billed hours round the runtime up to the provider's billing unit and minimum commitment, taken from the adapter's declared capabilities (`workloads.select.billing`). A provider without an adapter is assumed to bill per hour, and the output says so. `budget_hours` is how many hours the budget buys at that price. `unknown_costs` lists what is not included: storage, egress, startup / provisioning time and idle time. These are not zero; they are unknown. Prices are observed list prices, not quotes. A route quotes and re-validates the price before any launch.

## Theoretical versus measured, and why runtime is unknown

Memory is **estimated** from formulas. Hardware comparisons use **theoretical** vendor peaks (see [hardware](/methodology/hardware)). Prices are **observed**. Runtime depends on achieved throughput: kernels, data loading, communication, the framework version, the provider's host. OpenGrid has no measured benchmark for arbitrary workloads, so runtime is reported as unknown (`runtime.known: false`) and never estimated from peak FLOPs. The `performance_observations` table (migration 0020) is the data model for future measured results. Each row records the GPU, workload class, model size, method, precision, runtime, peak VRAM, utilization, provisioning delay, cost, outcome, source kind (`theoretical`, `user_supplied`, `measured_benchmark` or `completed_job`) and the owner's consent. It is empty today. The recommender reads it only to report "no measured benchmark for this workload on this GPU". Only consented `measured_benchmark` or `completed_job` rows would ever count as measured.

## What "executable" means

`execution.executable` is true only when both of these hold:

1. **The workload is supported.** It must be one of the allowlisted job templates, which today is `pytorch-mnist` (named in `model.name`). Any other workload gets `workload_supported: false`. Its recommendation is market information only: you would run it yourself with the provider.
2. **The readiness checks pass for this caller.** These are the same checks a job preview runs (`workloads.select.provider_readiness` and the environment's execution mode). The provider needs an OpenGrid adapter, a validated adapter, supervised or live launches enabled, startup-script support, per-deployment SSH keys and credentials, and the API key needs the `route:execute` scope.

An anonymous caller is told that an API key or sign-in is required. Readiness is never assumed. Every reason not to execute is listed with a code. `route_preview_url` opens the route preview for that GPU and count.

## Questions

Questions are asked only for inputs that are unknown and change the answer:

- workload type
- model or parameter count
- fine-tuning method
- micro batch
- sequence length
- gradient checkpointing
- concurrency
- context length
- precision

Each question says why it matters. A workload is declared GPU-free (`needs_gpu: false`) only on evidence: `workload_type: "no_gpu"`, or a list of frameworks with no ML framework among them and no model. Such a recommendation says so plainly and returns no options.
