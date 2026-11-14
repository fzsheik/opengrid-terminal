# Hardware specs, capability pricing and alternatives

Used by `/v1/gpus`, `/v1/gpus`, `/v1/gpus/{gpu}`, `/internal/compare`. Code: `hardware.py`,
`analytics/capability.py`, `analytics/alternatives.py`. Hardware figures are **vendor peak
specifications**, not measurements.

## Specs (`hardware.py`)

One hand-maintained entry per canonical GPU name, with vendor datasheet URLs in `sources`.

- All throughput is **dense**: no 2:4 structured sparsity. Where a vendor headline figure is "with
  sparsity" it was halved; the entry's `notes` says so.
- `fp16_tflops_dense` / `bf16_tflops_dense` are tensor/matrix-core peaks. For GeForce cards they are the
  dense FP16/BF16 rate **with FP32 accumulate**: what mixed-precision training and ordinary inference GEMMs
  (PyTorch / cuBLAS half and bf16 matmuls accumulate in FP32) run at, and the same meaning as the datacenter
  rows (where FP32 accumulate is full rate). GeForce runs FP16-accumulate at 2x; that figure is not used.
  Consumer AMD Radeon cards carry FP32 only.
- GeForce `fp8_tflops_dense` is the dense FP8 rate with FP32 accumulate (2x the fp16 figure, the same 2x as
  H100 / L40S rows); Ampere and older GeForce have no FP8.
- GeForce tensor figures and where they come from (`tensor_basis` on the row):
  - `whitepaper`: printed in an NVIDIA architecture whitepaper appendix table. RTX Blackwell whitepaper:
    5090, 5080, 5070 Ti, 5070, and (comparison columns) 4070 Ti, 4070, 3070 Ti. Ada whitepaper (v2.02,
    which corrected the FP8 FP32-accumulate figures): 4090, 4080, 3090 Ti, 3080 Ti. GA102 whitepaper: 3090,
    3080, 3070.
  - `nvidia_ai_tops`: the SKU is in no whitepaper table; NVIDIA's product page prints only "AI TOPS"
    (sparse, lowest precision). Dense FP16/BF16 with FP32 accumulate = AI TOPS / 16 on Blackwell (FP4
    sparse) and AI TOPS / 8 on Ada (FP8 sparse with FP16 accumulate). The ratio is exact for every SKU the
    whitepapers print both for (5090 3352/16 = 209.5, 5080 1801/16 = 112.6, 5070 Ti 1406/16 = 87.9, 5070
    988/16 = 61.7; 4090 1321/8 = 165.2, 4080 780/8 = 97.5, 4070 Ti 641/8 = 80.2, 4070 466/8 = 58.3) and equals
    SMs x 512 dense FLOPs per clock x NVIDIA's boost clock. Used for 5060 Ti (759), 5060 (614), 4080 SUPER
    (836), 4070 Ti SUPER (706), 4060 Ti (353), 4060 (242).
  - none (null): RTX 3060 Ti, 3060 (12 and 8 GB), 3050 have no per-SKU tensor figure from NVIDIA (no
    whitepaper table, no AI TOPS on the product page); Turing GeForce (20-series) figures in NVIDIA material
    are at Founders Edition clocks, not the reference clocks of the FP32 field; RTX PRO Blackwell rows publish
    only sparse FP4 AI TOPS and the workstation (full-rate FP32-accumulate) ratio is not printed anywhere. All
    stay null and cannot get a theoretical runtime.
- `fp8_tflops_dense` is null where the chip has no FP8 (`fp8_supported: false`) and also where no dense
  figure is published (`fp8_supported: true`).
- A figure we could not confirm is null, never estimated. Variants are separate entries (H100 SXM5 vs
  PCIe vs NVL; A100 40 vs 80 GB). Where a provider's name hides the variant (Tesla V100 16GB with no form
  factor; "MI350 294GB" without X/355X) the ambiguous figures are null and `notes` explains.
- MIG slices are partitions of one card (`parent`, `fraction_of_parent`) and carry no throughput.
- `workload_class` (`training`, `training+inference`, `inference`, `graphics/inference`) is an editorial
  grouping from vendor positioning, memory and interconnect. It is used only to group alternatives.

## Capability pricing (`analytics/capability.py`)

    price per unit = observed market price per GPU-hour / vendor peak figure

at the current market low and median (one vote per provider, see dispersion.md), for VRAM (GB),
dense BF16, FP16 and FP8 TFLOPS, and memory bandwidth (TB/s). Every result carries the label
**"theoretical, from vendor peak specs; not a workload benchmark"**. Real workloads reach a fraction of
peak, differently per model, precision, kernel and interconnect.

## Alternatives (`analytics/alternatives.py`)

Different GPUs are never called equivalent. An alternative is any other GPU with a current market that
shares at least one dimension with the target, listed with the dimensions it shares and its deltas:

| dimension | rule |
|---|---|
| similar_vram | VRAM within +/-25% of the target |
| same_architecture | same architecture |
| same_generation | same generation family, different architecture (e.g. Blackwell vs Blackwell Ultra) |
| training_oriented | both classed training or training+inference |
| inference_oriented | both classed inference, training+inference or graphics/inference |
| similar_price_performance | current median $ per dense BF16 TFLOP-hour within +/-25% |

Deltas are fractions vs the target (median price, low price, VRAM, BF16 TFLOPS, bandwidth). Sorted by the
number of shared dimensions, then price. MIG slices and whole GPUs are never offered for each other.
