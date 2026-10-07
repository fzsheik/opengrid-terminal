# Hardware specs, capability pricing and alternatives

Used by `/v1/hardware`, `/v1/gpus`, `/v1/gpus/{gpu}`, `/v1/compare`. Code: `hardware.py`,
`analytics/capability.py`, `analytics/alternatives.py`. Hardware figures are **vendor peak
specifications**, not measurements.

## Specs (`hardware.py`)

One hand-maintained entry per canonical GPU name, with vendor datasheet URLs in `sources`.

- All throughput is **dense**: no 2:4 structured sparsity. Where a vendor headline figure is "with
  sparsity" it was halved; the entry's `notes` says so.
- `fp16_tflops_dense` / `bf16_tflops_dense` are tensor/matrix-core peaks. For GeForce cards they are the
  FP32-accumulate rate from NVIDIA's architecture whitepapers (FP16-accumulate runs at 2x); only flagship
  SKUs with a published figure carry one. Consumer AMD Radeon cards carry FP32 only.
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
