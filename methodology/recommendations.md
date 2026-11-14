# Workload recommendations

OpenGrid estimates how much GPU memory a workload needs from published formulas, then matches that estimate against fresh, observed on-demand listings. It compares options by their total theoretical throughput across all their GPUs, and, for training when you say how much data you train on, estimates each option's runtime and whole-job cost as a range. Every default it assumes is listed in the answer, nothing is presented as a benchmark, and a GPU is called "executable" only when OpenGrid can actually launch the workload for you. One engine on the server (`intelligence/`) answers the website, the assistant, the CLI and the MCP server alike. The contract is `docs/OPENGRID2_CONTRACTS.md`.

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

`intelligence/models_catalog.py` holds the published configs of well-known open models: Llama 2/3/3.1/3.2, Mistral 7B, Mixtral 8x7B, Qwen2.5 0.5B-72B, Gemma 2, Phi-3, GPT-2, BERT, ResNet-50, ViT-B/16 and SDXL, plus the audio, image + text and video models of [the section below](#audio-image-and-video-workloads). Each entry links its source (the config.json, model card or paper). A request value always wins over the catalog, and every value filled from the catalog is listed in `assumptions`. A loose name ("7B llama") matches on family, size and version. Several matches are listed and lower the confidence. A value that could not be confirmed is left empty.

## GPU count and interconnect

Per-GPU need is computed for 1, 2, 4 and 8 GPUs, or up to `constraints.max_gpus`.

- **Training:** sharding follows ZeRO. Stage 1 splits optimizer states across the GPUs, stage 2 also gradients, stage 3 also weights. Activations stay per GPU because each GPU has its own micro-batch. When the stage is not given and more than one GPU is needed, ZeRO-3 / FSDP full sharding is assumed. ZeRO stage 0 (plain data parallel) keeps a full copy of the model on every GPU, so adding GPUs never helps it.
- **Inference:** tensor parallelism splits the weights and the KV cache N ways.
- **CPU offload:** moves the optimizer states off the GPU.

The suggested count is the smallest one whose per-GPU need, with the margin, fits the largest whole GPU on record (MIG slices and laptops excluded). In the recommendation each GPU model gets its own smallest fitting count, so "2 x H100" (two 80 GB GPUs) and "1 x H200" (one 141 GB GPU) can both appear for the same workload. More than one GPU for sharded training or tensor parallelism means GPU-to-GPU traffic on every step. NVLink / SXM / Infinity Fabric parts are preferred, and PCIe-only multi-GPU options carry a limitation and a lower workload score.

**Scale-up options.** For each GPU model that fits, OpenGrid also looks at its next count (2n GPUs, within `max_gpus`), so more GPUs of one model can compete with fewer, larger GPUs. Each GPU model gets at most one scale-up option (its best-valued listing), marked `count_choice: "scale_up"`. There are none when `hardware_hints.gpu_count` fixes the count, or for models under 1B parameters, where communication outweighs the extra compute.

## Performance across GPUs (theoretical)

Two GPUs are not "twice one GPU", and a bigger GPU is not "faster" just because it has more memory. An H200 has the same chip and the same compute as an H100 (989.5 dense BF16 TFLOPS each), with more and faster memory (141 GB at 4.8 TB/s against 80 GB at 3.35 TB/s). Two H100s have twice the compute and 160 GB in total, but the model is split across them and they exchange data on every step. Each option's `performance` block (`intelligence/performance.py`) says this in numbers:

- **Basis.** Training and fine-tuning are compute bound: dense tensor throughput at the training precision (BF16/FP16; dense FP8 for an FP8 run when the GPU publishes it; FP32 for FP32). Inference token generation is memory-bandwidth bound at small batch (every weight is read once per token): memory bandwidth. Vendor peak figures from [hardware](/methodology/hardware), never benchmarks.
- **Totals.** `total`: GPUs, VRAM, dense BF16 TFLOPS and memory bandwidth summed over the option's GPUs. A multi-GPU option also says its memory is split across the GPUs, not held in one.
- **Interconnect.** `interconnect.kind`: `single_gpu`; `nvlink` (SXM, OAM or Superchip parts: NVLink / NVSwitch / Infinity Fabric between every GPU, or an NVLink bridge spanning all the cards); `nvlink_bridge` (bridged pairs of PCIe cards, more cards than one bridge spans); `pcie` (no NVLink on record).
- **Scaling assumption.** `scaling`: speedup = GPUs x efficiency, with a range. OpenGrid planning assumptions, not measurements (communication-overhead context: [FSDP](https://arxiv.org/abs/2304.11277), [Megatron-LM](https://arxiv.org/abs/2104.04473)):

  | Efficiency per GPU | 2 GPUs | 4 GPUs | 8 GPUs |
  |---|---|---|---|
  | training, NVLink | 93% (85-98) | 90% (80-96) | 87% (75-94) |
  | training, bridged pairs | 90% (80-96) | 75% (55-88) | 65% (45-82) |
  | training, PCIe | 80% (60-92) | 70% (50-86) | 60% (40-80) |
  | inference (tensor parallel), NVLink | 85% (70-95) | 75% (60-90) | 65% (50-85) |
  | inference, bridged pairs | 80% (65-92) | 60% (40-80) | 50% (30-70) |
  | inference, PCIe | 65% (45-85) | 50% (30-75) | 40% (25-65) |

  Beyond 8 GPUs the efficiency drops another 5 points per doubling (floor 30%).
- **Effective throughput.** `effective` = per-GPU figure x speedup. `relative_to_balanced` divides it by the balanced pick's: "2 x H100: about 1.86x the balanced 1 x H200".

## Runtime and whole-job cost (training, when the data size is known)

Runtime is estimated only for training and fine-tuning, and only when the amount of data is given: `training.dataset_tokens` (or `dataset_samples` x `seq_len`), times `epochs` (1 is assumed and stated when epochs are not given). Without it, `runtime.known` is false, no job cost is shown, and a question asks for the dataset size.

- **Work.** FLOPs = k x N x T, N = parameters (active parameters for a mixture of experts), T = tokens trained. k = 6 for full training (forward 2N + backward 4N per token, [Kaplan et al. 2020](https://arxiv.org/abs/2001.08361) section 2.1). k = 4 for LoRA / QLoRA: frozen base weights get no weight gradients, but activation gradients still flow through every layer ([LoRA](https://arxiv.org/abs/2106.09685)). +2 with gradient checkpointing (one extra forward pass, [Chen et al. 2016](https://arxiv.org/abs/1604.06174)). QLoRA's dequantization overhead is not modelled and the output says its real runtime is usually longer.
- **Runtime.** hours = FLOPs / (effective throughput x utilization) / 3600. Utilization (model FLOPs utilization): 35%, range 20-50%, for models of 1B+ parameters. Well-tuned pretraining reaches 46-52% ([PaLM](https://arxiv.org/abs/2204.02311) 46.2%, [Megatron-LM](https://arxiv.org/abs/2104.04473) 52%); typical fine-tuning stacks land lower, so the central figure is conservative. Smaller models: 20%, range 5-40%. The range pairs the low utilization with the low scaling efficiency (slow end) and the high with the high (fast end). Startup, data-loading stalls, evaluation and checkpoint saving are not included.
- **Job cost.** `performance.job_cost` (also `cost.estimated_job`) = runtime x instance price, each end of the range rounded up to the provider's billing unit. Storage, egress and startup time are not included.
- **Pick.** `lowest_estimated_job_cost` is the option with the lowest central job-cost estimate. It can be a multi-GPU option that costs more per hour. For example, at illustrative prices of $4.90/h for two H100s and $3.60/h for one H200, a full fine-tune of an 8B model on 200M tokens is estimated at about 5.5 h (about $27) on the two H100s and about 10 h (about $37) on the H200. With a budget, an option whose estimated job cost exceeds it carries a limitation; it is not dropped, because the estimate is theoretical.

These figures are estimates in the open: every input, assumption and source is in the response. A consented measured benchmark for the workload and GPU (`performance_observations`, below) is the only thing that would replace them.

## From estimate to options

1. **Hardware.** Every canonical GPU is checked with `workloads/hardware_req.check_gpu`:
   - **Vendor:** must match the vendor constraint. A stated CUDA version means NVIDIA only.
   - **Compute capability (NVIDIA):** 8.0 for *stated* bf16, 8.9 for fp8.
   - **FP8 on other vendors:** the GPU must support FP8.
   - **Assumed precision:** never excludes hardware. A T4 still runs fp16 mixed precision.
   - **VRAM:** the GPU must hold the per-GPU need at its fitting count.
2. **Market (read-only).** `routing.scoring.rank_listings(gpu, count=n)` runs for each fitting GPU. The same rules as `/v1/best` apply: canonical, on-demand, priced, not sold out, seen within the provider's freshness window (`market.stale_after`), and exactly `n` GPUs per instance. Listings with unknown availability are kept and labelled "stock not published by the provider". Spot capacity is never ranked, even with `allow_spot`. Region, maximum price and provider include/exclude lists come from the request.
3. **Exclusions.** Everything left out is listed with a code:
   - `insufficient_vram`: too little memory even at the largest count.
   - `mig_single_slice`: a MIG slice that would be needed more than once. A slice is part of one GPU, so slices are never combined for sharding or tensor parallelism, and get no scale-up option.
   - `wrong_vendor`, `compute_capability_too_low`, `precision_unsupported`: hardware checks.
   - `no_fresh_listing`: the GPU fits but no fresh listing of the needed shape exists. The reason explains why that GPU count is needed.
   - `stale`, `sold_out`, `not_on_demand`, `over_max_price`, `wrong_region`, `excluded_by_preference`, `multi_instance`: market rules.
   - `over_budget`: price x GPUs x billed runtime hours is over `budget_usd`. Applied only when both `budget_usd` and `runtime_hours` are given.

   Other instance shapes of the same GPU are not reported.
4. **Workload score** (orders the list). Each (GPU, count, provider, region) keeps its best listing. Its score is 0.60 x price (the cheapest option's cost divided by this option's: never capped, so a cheaper option always scores better on price; whole-job cost when the option has one, else price per hour), plus 0.25 x the listing's own best-execution score (availability, freshness, history, integration), plus 0.15 x memory headroom (full credit at 10% over the recommended need, which already carries a 20% margin; more headroom buys nothing). PCIe-only multi-GPU options lose 0.10; options whose provider does not report stock lose 0.08.
5. **Picks.**
   - `cheapest_market`: the lowest instance price that fits.
   - `best_for_workload` (the **recommended** option): the cheapest option that is clearly safe: at least 10% headroom, in stock now (the provider reports it), a market score of at least 0.6, and NVLink when it uses more than one GPU, on the fewest GPUs that work. "Cheapest" is the whole-job cost when most safe options have one, else the price per hour. If nothing is that safe, the conditions relax in turn (any GPU count; then stock unconfirmed; then any fit), and the reason says which applied. Every other option carries a one-line `summary` comparing it with the recommended one: cheaper options say what is wrong with them ("$0.12/h cheaper, but stock not reported by the provider"), pricier ones what the extra buys ("$0.20/h more, for about 2.4x the throughput (estimated)", "Same GPU, $0.15/h more at Massed Compute", "with no advantage for this workload").
   - `best_executable`: the first option OpenGrid can launch for this caller (see below), often `null`.
   - `lowest_estimated_job_cost`: the lowest estimated whole-job cost (training with a known dataset size; see "Runtime and whole-job cost"), else `null` with the reason in `picks_unavailable`. Measured where an option has a measured runtime, theoretical otherwise; the reason says which.
   - `shortest_estimated_runtime`: the option estimated to finish soonest (a range, never called "fastest"), under the same conditions.
   - Every pick is a listed option and `limit` caps the list: the picks come first, in priority order (recommended, executable, cheapest, lowest job cost, shortest runtime), and the best-ranked other options fill the rest. With a `limit` smaller than the number of distinct picks, the lower-priority picks are `null` and `picks_unavailable` says the limit left them out.
   - A stated target the option misses on its own estimate (`constraints.deadline_hours`, `inference.target_tokens_per_s`, `inference.target_latency_ms`) keeps it out of the recommended pick while another option meets it: the rules are simple, safe, roomy, then `meets_targets`, then any.
6. **Tiers.** Tiers are relative to the balanced pick.
   - `higher_performance`: at least 10% more effective theoretical throughput in total over the option's GPUs (see "Performance across GPUs"). When a GPU has no throughput figure on record, more than 10% higher memory bandwidth, or more VRAM at equal bandwidth.
   - `lower_cost`: at least 10% cheaper per hour.
   - `balanced`: everything else.

   Comparisons cite spec-sheet numbers and say they are theoretical. OpenGrid never calls an option "fastest".

## Cost

Costs are **scenarios**, not predictions. Each one is price per GPU-hour x GPUs x billed hours for 1, 10 and 100 hours, plus the stated `runtime_hours` if given. Billed hours round the runtime up to the provider's billing unit and minimum commitment, taken from the adapter's declared capabilities (`workloads.select.billing`). A provider without an adapter is assumed to bill per hour, and the output says so. `budget_hours` is how many hours the budget buys at that price. `unknown_costs` lists what is not included: storage, egress, startup / provisioning time and idle time. These are not zero; they are unknown. Prices are observed list prices, not quotes. A route quotes and re-validates the price before any launch.

## Theoretical versus measured

Memory is **estimated** from formulas. Hardware comparisons use **theoretical** vendor peaks (see [hardware](/methodology/hardware)). Prices are **observed**. Real runtime depends on achieved throughput: kernels, data loading, communication, the framework version, the provider's host. Without a measurement the runtimes above are **estimated** from peak throughput with stated utilization and scaling assumptions, always as a range and always labelled theoretical; the workload-level `estimate.runtime.known` stays false.

## Measured performance

A completed **real** OpenGrid job becomes a measured observation in `performance_observations` (`intelligence/measured.py`; columns added by migration 0037). The rules, all required:

- not a sandbox job, outcome `succeeded`, the `real_metrics` check passed, and every metric row came from the runner (one `simulated` row and the job is skipped);
- the job's selection names the provider and canonical GPU; when the runner reported the GPU name, the GPU's model token must appear in it as a whole word;
- at least one epoch reports both its wall-clock (`epoch_seconds`) and its work (`train_samples` or tokens).

Throughput is the work over the timed epochs divided by their seconds, dropping the first epoch as warm-up when there are two or more. Setup overhead is the time from `initializing` to `training_completed` minus the timed epochs (bootstrap, data download, evaluation, artifact upload; not provisioning). Each row is labelled data kind `measured`, source kind `completed_job`, and a job is recorded once. A run by OpenGrid itself is consented; a customer's run is recorded with `consent=false` unless the account opted in, and only consented rows are read back.

The recommender matches measurements by **workload signature** (`template:<id>` for an allowlisted template; `model:<catalog id>|<method>|<precision>` otherwise), GPU and GPU count, no older than 180 days, and never borrows a measurement from another workload. An option with a measurement carries `performance.measured` (number of runs, median and range of throughput, age, confidence: high with 3+ runs within 30 days, else medium, and only a count of providers, never their names) and, when the request states its data size in the measured unit, a measured runtime (work / median throughput + median setup overhead; the range from the fastest and slowest run) and `performance.job_cost_measured`. Ranking and the job-cost picks then use the measured figure and say so; the theoretical figures stay alongside. With no measurement nothing changes, and nothing is labelled measured.

## Whole-job cost and completion time

Each option with a runtime (measured, else theoretical) gets `cost.estimated_total`: the runtime plus, when OpenGrid has at least the reliability minimum of its own launches on that provider, the provider's median time to capacity (approval to running), billed in the provider's unit at the instance price. It states the completion hours and range, the runtime kind, whether provisioning and setup are included, and what is still unknown (storage, egress, provisioning, setup). The total is always a lower bound. Options are ranked on this figure when most have one. `constraints.deadline_hours` labels each option `meets_deadline`: `yes` (finishes in time even at the slow end), `likely` (the central estimate does), `no`, or `unknown` (no runtime). `training.max_steps` with `global_batch_size` and `seq_len` gives the tokens trained (sequences assumed packed to the full length, and the output says so).

## Hardware suitability

`intelligence/workload_fit.py` adds, per option (all estimated, never benchmarks):

- **Interconnect evidence** (`performance.interconnect.evidence`): the GPU link and where it comes from (`listing` when the listing names SXM / HGX / DGX / NVSwitch / NVLink / OAM, else `spec_sheet`; "PCIe" in a listing names the form factor and changes nothing), the per-direction link bandwidth (half of the published NVLink total; PCI-SIG x16 rates for PCIe), and the node fabric (InfiniBand / RoCE / EFA / TCPX) from the listing text or provider documentation, `null` when unknown. A PCIe card that supports NVLink bridges, on a listing that does not say they are fitted, carries a limitation: without the bridges the GPUs talk over PCIe only. The scaling table above still uses the spec-sheet class.
- **Parallelism** (`performance.parallelism`): DDP, ZeRO-1/2, FSDP / ZeRO-3 or tensor parallel, with how to launch it.
- **Communication versus compute** (`performance.communication`, training on 2+ GPUs): bytes per GPU per step from ring-collective volumes ([NCCL](https://github.com/NVIDIA/nccl-tests/blob/master/doc/PERFORMANCE.md): all-reduce 2(n-1)/n, all-gather and reduce-scatter (n-1)/n each; DDP all-reduces gradients, FSDP gathers weights twice and reduce-scatters gradients), a lower bound on its time at the peak link bandwidth, and the step's compute time at the central utilization. A ratio of 1 or more adds a "communication-bound on paper" limitation. Overlap is not assumed.
- **Inference throughput** (`performance.inference`): a memory-bandwidth roofline for decode. Each step reads the per-GPU weights and the KV cache at 60% of peak bandwidth (range 40-80%, a planning assumption) with the tensor-parallel efficiency above, capped by compute (2 x active parameters FLOPs per token at the central utilization). Aggregate and per-stream tokens/s, checked against `target_tokens_per_s` (read as total output tokens/s) and `target_latency_ms` (with `output_tokens`), and the cost per million output tokens at full utilization. Prefill is not modelled.
- **Multi-GPU advice** (top-level `multi_gpu`): `single_gpu`, `scale_up_optional` or `multi_gpu_required`; the reasons not to go multi-GPU for this workload; and each scale-up compared with the same GPU and provider at its minimum count (speedup range, link, strategy, whole-job cost ratio).

## Explanations and confidence

Every option has `decision.confidence` (high / medium / low): the weakest of the memory fit (the estimate's own confidence, at most medium under 10% headroom), the runtime (measured: medium or high; theoretical: low), the price freshness and the reported stock. Provider reliability is shown, and lowers the level only once a real score exists. Each component names its evidence (measured, observed, estimated, transaction). `decision.vs_recommended` compares the option with the recommended one factor by factor (whole-job cost, completion time, price per hour, headroom, throughput, stock, listing score, GPU link, executable), each saying which side is better. The top-level `explanation` names the recommended option, the rule and basis that chose it, its confidence, and what each of the first alternatives wins and loses on. When an internal-tier option is involved, every factor that could reveal its price, stock or score is removed for non-admin callers (`routing/disclosure.py`).

The full review, its limits and the validation checklist: [workload intelligence review](/methodology/workload-intelligence-review).

## Audio, image and video workloads

Speech recognition, text-to-speech, vision-language models (images, video) and video generation size differently from a text LLM: an image adds hundreds or thousands of tokens to every request, Whisper keeps a cache over 1,500 encoder positions per 30-second window, and a video diffusion model holds a latent video of up to ~120,000 tokens in attention. `intelligence/media.py` adds these terms; everything else (sharding, margin, market, ranking, disclosure) is the pipeline above. Every figure is **estimated**, with its formula in the response (`estimate.media.formulas`) and its inputs (`estimate.media.inputs`).

**Request.** An optional `media` object (all fields optional, bounded, `extra="forbid"`): `modality` (`text`, `image`, `audio`, `video`, `multimodal`, one value or a list), `task` (`understanding`, `captioning`, `transcription`, `speech_synthesis`, `generation`), `images_per_request` (1-256), `image_resolution` (`WIDTHxHEIGHT`), `audio_seconds` (per item), `total_audio_hours` (the batch), `video_seconds`, `fps` and `video_resolution` (input video), `output_video_seconds`, `output_fps`, `output_resolution` and `diffusion_steps` (generation), `total_items` (images, clips, files or videos in a batch). A request without `media`, or with an empty one, is estimated exactly as before (a test compares 24 such payloads byte for byte with the engine before this feature). The CLI mirror (`cli/src/opengrid_cli/workload.py`) and the /find schema carry the same fields.

**Models** (each value from the linked config.json, model card or paper; a value derived from published dimensions or file sizes is labelled `derived`):

| Family | Entries | Facts used | Source |
|---|---|---|---|
| Whisper | tiny, base, small, medium, large-v3, large-v3-turbo | encoder / decoder layers, d_model, mel bins, 1,500 encoder positions, 448 decoder tokens; 39M-1,550M parameters, turbo 809M with a 4-layer decoder | [paper](https://arxiv.org/abs/2212.04356) Table 1, config.json, the turbo model card |
| wav2vec2 | base-960h, large-960h-lv60-self | 320-sample conv stride = 50 frames/s at 16 kHz; 95M / 317M | [paper](https://arxiv.org/abs/2006.11477), config.json |
| Parler-TTS, Kokoro | mini-v1 (0.9B), large-v1 (2.2B); Kokoro-82M | decoder dims, DAC codec at 86 frames/s with 9 codebooks; Kokoro is not autoregressive | model cards, config.json, codec config |
| LLaVA | 1.5 7B/13B, 1.6 Vicuna 7B/13B, 1.6 Mistral 7B | CLIP ViT-L/14-336: 576 tokens per image; 1.6 AnyRes grid pinpoints | [LLaVA-1.5](https://arxiv.org/abs/2310.03744), [LLaVA-NeXT](https://llava-vl.github.io/blog/2024-01-30-llava-next/), config.json |
| Qwen2-VL | 2B, 7B, 72B | 675M ViT, one token per 28 x 28 pixels within 3,136-12,845,056 pixels; video at 2 frames/s, 2 frames per token grid, per-frame 128-768 x 784 pixels, 24,576-token video budget | [paper](https://arxiv.org/abs/2409.12191), config.json, preprocessor config, qwen-vl-utils 0.0.8 |
| Llama 3.2 Vision | 11B, 90B | 8 / 20 cross-attention layers; up to 4 tiles of 560 x 560, 1,601 vision tokens per tile | config.json (public mirror), Hugging Face totals |
| PaliGemma | 3B at 224 / 448 / 896 | 256 / 1,024 / 4,096 image tokens; Gemma-2B dims | [paper](https://arxiv.org/abs/2407.07726) |
| CogVideoX | 2B, 5B | transformer, T5-XXL (4.76B) and VAE parameters; 720x480, 49 frames at 8 fps, 50 steps, classifier-free guidance; the publisher's memory and time | config.json files, model card |
| Wan2.1 T2V | 1.3B, 14B | DiT dims and FFN, umT5-XXL, 81 frames at 16 fps, 480p / 720p; the publisher's memory and time table | config.json, [GitHub](https://github.com/Wan-Video/Wan2.1) |
| HunyuanVideo | 13B | 20 dual + 40 single-stream blocks, hidden 3,072, guidance distilled; 1280x720 x 129 frames: 60 GB published peak | [GitHub](https://github.com/Tencent/HunyuanVideo) |

**Memory** (added to the components of [Memory estimate](#memory-estimate)):

- **Image + text (VLM):** tokens per image by the model's documented rule (fixed 576 for LLaVA-1.5; AnyRes: 576 x (1 + tiles of the best-fitting grid), an upper bound because unpadding removes padded rows; Qwen2-VL: the image resized to multiples of 28 within its pixel limits, one token per 28 x 28 pixels; PaliGemma: fixed by checkpoint). The KV cache uses context = text tokens + images x tokens per image (+ video tokens). Llama 3.2 Vision instead keeps `2 x cross-attention layers x KV heads x head dim x vision tokens x concurrency x 2 B` and its text cache only in the self-attention layers. One vision-encoder layer over one image is added as transient activations. With quantized weights the vision tower stays 16-bit.
- **Video understanding (Qwen2-VL):** frames = seconds x fps (2 by default), an even count within 4-768; each frame resized within 128-768 x 784 pixels; tokens = frames / 2 x tokens per frame grid, capped at the 24,576-token video budget.
- **Speech recognition (Whisper):** windows = ceil(audio seconds / 30). Per window decoded together, a cross-attention cache `2 x decoder layers x d_model x 1500 x 2 B` and a self-attention cache over up to 448 tokens; one encoder layer's activations over 1,500 positions. Greedy decoding (beam search multiplies the decoder caches by the beam count). Fine-tuning: the activation formula over the encoder (1,500 positions) and the decoder (448 tokens + fp32 logits).
- **Speech encoders (wav2vec2):** frames = seconds x 50; the activation formula over those frames.
- **Text-to-speech:** Parler-TTS: KV over `seconds x 86 + 8` codec steps plus a 64-token prompt; Kokoro: weights only.
- **Video generation:** weights = transformer (at the run's precision) + text encoder (16-bit; the low end assumes it is offloaded after encoding the prompt) + VAE. Latent tokens = latent frames x (H / 16) x (W / 16), latent frames = (frames - 1) / 4 + 1, frames = seconds x fps + 1 aligned to 4k + 1. Transient activations per block = batch x tokens x (6 x hidden + 2 x FFN) x 2 B, with the low end at (3 x hidden + FFN) and the high end at twice the central figure, and batch 2 when classifier-free guidance batches its passes. The VAE decode adds 8 frames x H x W x top VAE channels x 2 B x 3, with 1/8 of that for a tiled decode at the low end and twice it at the high end. **These two terms are roughly modelled**, so video generation is always `confidence: low`. Several GPUs are assumed to run sequence parallel (xDiT / Ulysses): weights replicated, activations split. Some repositories shard weights too (FSDP); that is not assumed. Cross-checks against the publishers' own peaks: CogVideoX-2b about 19.8 GB before margin vs 18 GB (SAT, FP16); Wan2.1-14B 720p 57.8 GB vs 69.1 GB; HunyuanVideo 720p x 129 frames 62.7 GB vs 60 GB.

**Throughput and runtime (theoretical, per option).** Per item (a 30-second window, a clip, an utterance, a request, a video), the GPU time is the compute at an assumed model-FLOPs utilization plus, for autoregressive decoders, `steps x max(bytes read per step / (bandwidth x 60%), step FLOPs / (peak x utilization)) / batch`:

- **Whisper:** encoder `2 x encoder parameters x 1500 + 4 x layers x 1500^2 x d_model` plus decoder `2 x decoder parameters x 100 tokens` per window. The token count ranges 50-150, and the encoder parameters are `12 x layers x d_model^2`. The total is divided by an end-to-end **pipeline efficiency of 10% (range 4-30%)**: decode loops are latency-bound and audio decoding runs on the CPU. This is an OpenGrid planning assumption, not calibrated against a measurement.
- **Parler-TTS:** one decoder step per codec frame, with the same pipeline efficiency. Kokoro: not estimated (no published compute per second of audio).
- **VLM:** vision encoder `2 x parameters x patches` + LLM prefill `2 x parameters x (media + 64 prompt tokens)` + memory-bound decode of 128 output tokens (range 32-512).
- **Video generation:** `steps x passes x (2 x transformer parameters x tokens + 4 x blocks x tokens^2 x hidden [+ cross-attention])` per video at **45% utilization (range 25-70%)**. The range is chosen to contain the utilization implied by every publisher-reported time on record: CogVideoX-2b and -5b on A100 and H100, and Wan2.1-14B 720p on A100 and H100, which imply 30% to 66%. A test checks that each of those six times falls inside the range. They are publisher figures, never OpenGrid measurements.

Throughput is shown per option in `performance.media.throughput` (audio hours, requests or videos per GPU-hour, with a range). With the amount of work (`total_audio_hours`, or `total_items`; a video job defaults to 1 video and says so), each option gets a runtime and a whole-job cost range, which feed the same picks, deadlines and explanations as training. **Measured first:** a consented measurement of the same workload signature from a real OpenGrid job (`samples_per_second`, with `total_items` as samples) replaces the theoretical runtime for that GPU and is labelled.

**Several GPUs.** Transcription windows, captions and videos are independent, so a multi-GPU option of a model that fits one GPU runs one replica per GPU (`performance.parallelism.strategy: "replicas"`). The assumed scaling is 98% of linear (range 90-100%) for shard imbalance, with no GPU-to-GPU link needed. `multi_gpu.independent_items` says so and points to batch fan-out ([batches](/methodology/batches)), which runs allowlisted templates only today. Tensor parallelism is used only when one copy does not fit.

**Explanations and questions.** The recommended option's `why` names what the media inputs cost (e.g. "image/video input adds 1,152 tokens per request ...", "video generation at 1280x720, 129 frames ... only GPUs with at least 80 GB hold it on one GPU"), and a `Media:` note repeats it. Questions ask for images per request, image resolution, video length, output resolution and length, and total audio hours or items, but only when they are unknown and change the answer.

**Detection.** The local analyzer reads Python scripts whose names suggest media workloads (transcribe, caption, generate, asr, tts, speech, video, vision, whisper, diffusion), using the same file allowlist and redaction as for other scripts. It recognises these sources:

- **Model classes and pipelines:** transformers model classes (`WhisperForConditionalGeneration`, `LlavaForConditionalGeneration`, `Qwen2VLForConditionalGeneration`, `MllamaForConditionalGeneration`, `PaliGemmaForConditionalGeneration` and others), `pipeline("automatic-speech-recognition" | "image-to-text" | "text-to-speech" ...)`, and diffusers video pipelines (`CogVideoXPipeline`, `WanPipeline`, `HunyuanVideoPipeline`, `LTXPipeline` and others).
- **Whisper loaders:** `faster_whisper.WhisperModel("large-v3")`, `whisper.load_model` and `whisperx.load_model`, with the size mapped to the Hugging Face id.
- **Other sources:** Kokoro's `KPipeline`, vLLM `limit_mm_per_prompt` / `--limit-mm-per-prompt`, `config.json` `model_type` and the model name.

It fills `media.modality`, `media.task` and (from vLLM) `images_per_request`. A project without any of these keeps its exact previous output (no `media` key). The assistant recognises plain-language asks ("transcribe 10k hours of audio", "caption images with LLaVA", "generate 5-second videos", "summarize 60-second videos") and, when no model is named, offers catalog models for the modality instead of guessing one.

**Limits.**

- **Whisper and Parler-TTS runtimes** rest on an uncalibrated pipeline efficiency, so treat them as order-of-magnitude.
- **AnyRes and Llama 3.2 Vision tile counts** follow the documented rules approximately: AnyRes counts are an upper bound, and a 4000x1000 image counts 4 tiles.
- **Video understanding** is modelled for Qwen2-VL only. An image-only VLM given video says so and counts no video tokens.
- **Video generation:** activation and VAE-decode memory are rough. Text-encoder and VAE time are not in the runtime. Training memory for video and TTS models is a lower bound.
- **HunyuanVideo's transformer** is counted at the "over 13 billion" lower bound.

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
- for audio, image and video workloads: images per request, image resolution, video length, output resolution and length, total audio hours or items (see [Audio, image and video workloads](#audio-image-and-video-workloads))

Each question says why it matters. A workload is declared GPU-free (`needs_gpu: false`) only on evidence: `workload_type: "no_gpu"`, or a list of frameworks with no ML framework among them and no model. Such a recommendation says so plainly and returns no options.
