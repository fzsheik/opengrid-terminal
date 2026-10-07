"""Hardware facts for every canonical GPU name: what the silicon is, not what it costs.

    spec(canonical_gpu) -> dict | None
    all_specs() -> {canonical name: dict}

Hand-maintained from vendor datasheets, like canonical.py. Rules:
    - Every throughput figure is DENSE (no 2:4 structured sparsity). NVIDIA and
      AMD headline numbers are usually "with sparsity" = 2x dense; those were
      halved and the entry says so in `notes` where it matters.
    - fp16/bf16 are tensor/matrix-core peaks. For GeForce cards they are the
      FP32-accumulate rate (what mixed-precision training uses); GeForce runs
      FP16-accumulate at twice that. Consumer AMD cards carry fp32 only.
    - A field we could not confirm from a vendor document is None, never guessed.
      fp8 is None both where the chip lacks FP8 (`fp8_supported` False) and where
      the dense rate is not published (`fp8_supported` True).
    - Theoretical peaks, not benchmarks. Real workloads reach a fraction of them.

Keys: vendor, architecture, generation, released (year), segment
('datacenter'|'workstation'|'consumer'|'laptop'), datacenter (bool), vram_gb,
memory_type, memory_bandwidth_tbps, fp32_tflops, fp16_tflops_dense,
bf16_tflops_dense, fp8_tflops_dense, fp8_supported, interconnect (host link),
nvlink (GPU-GPU link text or None), form_factor ('SXM'|'PCIe'|'OAM'|'Superchip'|
'MIG slice'|'Laptop'), tdp_w, workload_class ('training'|'training+inference'|
'inference'|'graphics/inference'), notes, sources. MIG slices carry
`parent` and `fraction_of_parent` and no throughput of their own.

workload_class is an editorial classification from vendor positioning and
memory/interconnect, used only to group alternatives. See methodology/hardware.md.
"""

from __future__ import annotations

import canonical

NV_H100 = "https://www.nvidia.com/en-us/data-center/h100/"
NV_H200 = "https://www.nvidia.com/en-us/data-center/h200/"
NV_HGX = "https://www.nvidia.com/en-us/data-center/hgx/"
NV_GB300 = "https://www.nvidia.com/en-us/data-center/gb300-nvl72/"
NV_GH200 = "https://www.nvidia.com/en-us/data-center/grace-hopper-superchip/"
NV_A100 = "https://www.nvidia.com/content/dam/en-zz/Solutions/Data-Center/a100/pdf/nvidia-a100-datasheet-us-nvidia-1758950-r4-web.pdf"
NV_L40S = "https://www.nvidia.com/en-us/data-center/l40s/"
NV_L40 = "https://www.nvidia.com/en-us/data-center/l40/"
NV_L4 = "https://www.nvidia.com/en-us/data-center/l4/"
NV_A40 = "https://www.nvidia.com/en-us/data-center/a40/"
NV_A10 = "https://www.nvidia.com/en-us/data-center/products/a10-gpu/"
NV_T4 = "https://www.nvidia.com/en-us/data-center/tesla-t4/"
NV_V100 = "https://www.nvidia.com/en-us/data-center/v100/"
NV_ADA_WP = "https://images.nvidia.com/aem-dam/Solutions/geforce/ada/nvidia-ada-gpu-architecture.pdf"
NV_AMPERE_WP = "https://www.nvidia.com/content/PDF/nvidia-ampere-ga-102-gpu-architecture-whitepaper-v2.pdf"
NV_BLACKWELL_WP = "https://images.nvidia.com/aem-dam/Solutions/geforce/blackwell/nvidia-rtx-blackwell-gpu-architecture.pdf"
NV_RTXPRO6000_SE = "https://www.nvidia.com/en-us/data-center/rtx-pro-6000-blackwell-server-edition/"
NV_RTXPRO6000 = "https://www.nvidia.com/en-us/products/workstations/professional-desktop-gpus/rtx-pro-6000/"
NV_RTXPRO5000 = "https://www.nvidia.com/en-us/products/workstations/professional-desktop-gpus/rtx-pro-5000/"
NV_RTXPRO4500_SE = "https://www.nvidia.com/en-us/data-center/rtx-pro-4500-blackwell-server-edition/"
NV_RTXPRO4500 = "https://www.nvidia.com/en-us/products/workstations/professional-desktop-gpus/rtx-pro-4500/"
NV_RTXPRO4000 = "https://www.nvidia.com/en-us/products/workstations/professional-desktop-gpus/rtx-pro-4000/"
NV_WS = "https://www.nvidia.com/en-us/design-visualization/desktop-graphics/"
NV_GEFORCE = "https://www.nvidia.com/en-us/geforce/graphics-cards/compare/"
AMD_MI300X = "https://www.amd.com/en/products/accelerators/instinct/mi300/mi300x.html"
AMD_MI325X = "https://www.amd.com/en/products/accelerators/instinct/mi300/mi325x.html"
AMD_MI355X = "https://www.amd.com/en/products/accelerators/instinct/mi350/mi355x.html"
AMD_MI350X = "https://www.amd.com/en/products/accelerators/instinct/mi350/mi350x.html"
AMD_RADEON = "https://www.amd.com/en/products/graphics/desktops/radeon.html"
INTEL_GAUDI2 = "https://www.intel.com/content/www/us/en/products/details/processors/ai-accelerators/gaudi2.html"

_FIELDS = ("vendor", "architecture", "generation", "released", "segment", "datacenter", "vram_gb", "memory_type",
           "memory_bandwidth_tbps", "fp32_tflops", "fp16_tflops_dense", "bf16_tflops_dense", "fp8_tflops_dense",
           "fp8_supported", "interconnect", "nvlink", "form_factor", "tdp_w", "workload_class", "notes", "sources")

_SPECS: dict[str, dict] = {}


def _s(name, vendor, arch, gen, released, segment, vram, mem, bw, fp32, fp16, bf16, fp8, fp8_ok,
       interconnect, nvlink, form, tdp, workload, sources, notes="", **extra):
    d = dict(vendor=vendor, architecture=arch, generation=gen, released=released, segment=segment,
             datacenter=segment == "datacenter", vram_gb=vram, memory_type=mem, memory_bandwidth_tbps=bw,
             fp32_tflops=fp32, fp16_tflops_dense=fp16, bf16_tflops_dense=bf16, fp8_tflops_dense=fp8,
             fp8_supported=fp8_ok, interconnect=interconnect, nvlink=nvlink, form_factor=form, tdp_w=tdp,
             workload_class=workload, notes=notes, sources=list(sources), **extra)
    _SPECS[name] = d


DC, WS, CON, LAP = "datacenter", "workstation", "consumer", "laptop"
TR, TI, INF, GI = "training", "training+inference", "inference", "graphics/inference"

# ---------------------------------------------------------------- NVIDIA Blackwell datacenter
_s("NVIDIA B300 288GB SXM", "NVIDIA", "Blackwell Ultra", "Blackwell", 2025, DC, 288, "HBM3e", 8.0,
   75, 2250, 2250, 4500, True, "PCIe Gen6", "NVLink 5, 1.8 TB/s", "SXM", None, TR, [NV_HGX],
   "Per GPU from the HGX B300 8-GPU table (36 PF FP16/BF16, 72 PF FP8 sparse; dense is half). "
   "TDP varies by system (about 1,100 W HGX) and is not on the HGX page, so left empty. "
   "Bandwidth 8 TB/s per GPU per NVIDIA Blackwell Ultra material. Some providers list 268-270 GB usable.")
_s("NVIDIA B200 180GB SXM", "NVIDIA", "Blackwell", "Blackwell", 2024, DC, 180, "HBM3e", 8.0,
   75, 2250, 2250, 4500, True, "PCIe Gen5", "NVLink 5, 1.8 TB/s", "SXM", 1000, TR, [NV_HGX],
   "Per GPU from the HGX B200 table (36 PF FP16/BF16, 72 PF FP8 sparse; dense is half). 192 GB physical HBM3e, "
   "180 GB exposed; 8 TB/s per GPU on DGX B200 (some HGX material gives 7.7 TB/s). TDP up to 1,000 W in HGX.")
_s("NVIDIA GB300 288GB", "NVIDIA", "Blackwell Ultra", "Blackwell", 2025, DC, 288, "HBM3e", 8.0,
   None, 2500, 2500, 5000, True, "NVLink-C2C to Grace", "NVLink 5, 1.8 TB/s", "Superchip", None, TR, [NV_GB300],
   "Per GPU from GB300 NVL72 rack figures (360 PF FP16/BF16, 720 PF FP8, read as sparse, /72, halved). "
   "UNCERTAIN: NVIDIA's rack footnotes on sparsity were not re-verified; treat as approximate.")
_s("NVIDIA B300 MIG 1g.34gb", "NVIDIA", "Blackwell Ultra", "Blackwell", 2025, DC, 34, "HBM3e", None,
   None, None, None, None, True, None, None, "MIG slice", None, INF, [NV_HGX],
   "A Multi-Instance GPU partition of one B300 (1 of up to 7 compute slices), not a whole GPU.",
   parent="NVIDIA B300 288GB SXM", fraction_of_parent=1 / 7)

# ---------------------------------------------------------------- NVIDIA Hopper
_s("NVIDIA H200 141GB SXM5", "NVIDIA", "Hopper", "Hopper", 2024, DC, 141, "HBM3e", 4.8,
   67, 989.5, 989.5, 1979, True, "PCIe Gen5", "NVLink 4, 900 GB/s", "SXM", 700, TR, [NV_H200],
   "Datasheet gives 1,979 TFLOPS FP16 / 3,958 FP8 with sparsity; dense is half.")
_s("NVIDIA H200 143GB NVL", "NVIDIA", "Hopper", "Hopper", 2024, DC, 141, "HBM3e", 4.8,
   60, 835.5, 835.5, 1670.5, True, "PCIe Gen5", "NVLink bridge (2- or 4-way), 900 GB/s", "PCIe", 600, TI, [NV_H200],
   "NVIDIA specifies 141 GB; the canonical name keeps the 143 GB some providers report. TDP up to 600 W configurable.")
_s("NVIDIA H100 80GB SXM5", "NVIDIA", "Hopper", "Hopper", 2022, DC, 80, "HBM3", 3.35,
   67, 989.5, 989.5, 1979, True, "PCIe Gen5", "NVLink 4, 900 GB/s", "SXM", 700, TR, [NV_H100],
   "Datasheet gives 1,979 FP16 / 3,958 FP8 TFLOPS with sparsity; dense is half.")
_s("NVIDIA H100 80GB PCIe", "NVIDIA", "Hopper", "Hopper", 2022, DC, 80, "HBM2e", 2.0,
   51, 756, 756, 1513, True, "PCIe Gen5", None, "PCIe", 350, TI, [NV_H100],
   "Fewer SMs, lower clocks and HBM2e versus SXM5: about 76% of SXM5 dense throughput. TDP 300-350 W.")
_s("NVIDIA H100 80GB PCIe NVLink", "NVIDIA", "Hopper", "Hopper", 2022, DC, 80, "HBM2e", 2.0,
   51, 756, 756, 1513, True, "PCIe Gen5", "NVLink bridge (pairs), 600 GB/s", "PCIe", 350, TI, [NV_H100],
   "Same card as H100 PCIe, sold with an NVLink bridge between pairs.")
_s("NVIDIA H100 94GB NVL", "NVIDIA", "Hopper", "Hopper", 2023, DC, 94, "HBM3", 3.9,
   60, 835.5, 835.5, 1670.5, True, "PCIe Gen5", "NVLink bridge (pairs), 600 GB/s", "PCIe", 400, TI, [NV_H100],
   "Datasheet figures with sparsity halved. TDP 350-400 W configurable.")
_s("NVIDIA GH200 96GB", "NVIDIA", "Hopper", "Hopper", 2023, DC, 96, "HBM3", 4.0,
   67, 989.5, 989.5, 1979, True, "NVLink-C2C 900 GB/s to Grace CPU", None, "Superchip", None, TR, [NV_GH200],
   "Grace CPU + H100-class GPU. The 144 GB HBM3e variant has 4.9 TB/s: different product. Module power "
   "450-1,000 W covers CPU and GPU, so no GPU TDP is given.")

# ---------------------------------------------------------------- NVIDIA Ampere datacenter
_s("NVIDIA A100 80GB SXM4", "NVIDIA", "Ampere", "Ampere", 2020, DC, 80, "HBM2e", 2.039,
   19.5, 312, 312, None, False, "PCIe Gen4", "NVLink 3, 600 GB/s", "SXM", 400, TR, [NV_A100])
_s("NVIDIA A100 80GB PCIe", "NVIDIA", "Ampere", "Ampere", 2021, DC, 80, "HBM2e", 1.935,
   19.5, 312, 312, None, False, "PCIe Gen4", None, "PCIe", 300, TI, [NV_A100])
_s("NVIDIA A100 80GB PCIe NVLink", "NVIDIA", "Ampere", "Ampere", 2021, DC, 80, "HBM2e", 1.935,
   19.5, 312, 312, None, False, "PCIe Gen4", "NVLink bridge (pairs), 600 GB/s", "PCIe", 300, TI, [NV_A100])
_s("NVIDIA A100 40GB SXM4", "NVIDIA", "Ampere", "Ampere", 2020, DC, 40, "HBM2", 1.555,
   19.5, 312, 312, None, False, "PCIe Gen4", "NVLink 3, 600 GB/s", "SXM", 400, TR, [NV_A100])
_s("NVIDIA A100 40GB PCIe", "NVIDIA", "Ampere", "Ampere", 2020, DC, 40, "HBM2", 1.555,
   19.5, 312, 312, None, False, "PCIe Gen4", None, "PCIe", 250, TI, [NV_A100])
_s("NVIDIA A40 48GB", "NVIDIA", "Ampere", "Ampere", 2020, DC, 48, "GDDR6", 0.696,
   37.4, 149.7, 149.7, None, False, "PCIe Gen4", "NVLink bridge (pairs), 112.5 GB/s", "PCIe", 300, GI, [NV_A40],
   "Datasheet tensor figures with sparsity halved.")
_s("NVIDIA A10 24GB PCIe", "NVIDIA", "Ampere", "Ampere", 2021, DC, 24, "GDDR6", 0.6,
   31.2, 125, 125, None, False, "PCIe Gen4", None, "PCIe", 150, INF, [NV_A10],
   "Datasheet: 250 TFLOPS FP16/BF16 tensor with sparsity; dense is half.")
_s("NVIDIA A10G 24GB", "NVIDIA", "Ampere", "Ampere", 2021, DC, 24, "GDDR6", None,
   None, None, None, None, False, "PCIe Gen4", None, "PCIe", None, INF,
   ["https://aws.amazon.com/ec2/instance-types/g5/"],
   "AWS-only part related to the A10; NVIDIA publishes no datasheet, so throughput and bandwidth are left empty.")

# ---------------------------------------------------------------- NVIDIA Ada / Turing / Volta datacenter
_s("NVIDIA L40S 48GB", "NVIDIA", "Ada Lovelace", "Ada", 2023, DC, 48, "GDDR6", 0.864,
   91.6, 362.05, 362.05, 733, True, "PCIe Gen4", None, "PCIe", 350, TI, [NV_L40S],
   "Datasheet: 733 FP16 / 1,466 FP8 TFLOPS with sparsity; dense is half.")
_s("NVIDIA L40 48GB", "NVIDIA", "Ada Lovelace", "Ada", 2022, DC, 48, "GDDR6", 0.864,
   90.5, 181.05, 181.05, 362, True, "PCIe Gen4", None, "PCIe", 300, GI, [NV_L40],
   "Datasheet: 362 FP16 / 724 FP8 TFLOPS with sparsity; dense is half.")
_s("NVIDIA L4 24GB", "NVIDIA", "Ada Lovelace", "Ada", 2023, DC, 24, "GDDR6", 0.3,
   30.3, 121, 121, 242.5, True, "PCIe Gen4", None, "PCIe", 72, INF, [NV_L4],
   "Datasheet: 242 FP16 / 485 FP8 TFLOPS with sparsity; dense is half.")
_s("NVIDIA T4 16GB", "NVIDIA", "Turing", "Turing", 2018, DC, 16, "GDDR6", 0.32,
   8.1, 65, None, None, False, "PCIe Gen3", None, "PCIe", 70, INF, [NV_T4],
   "Turing has no BF16 or FP8 and no sparsity: 65 TFLOPS mixed FP16/FP32 is dense.")
_s("NVIDIA Tesla V100 16GB SXM2", "NVIDIA", "Volta", "Volta", 2017, DC, 16, "HBM2", 0.9,
   15.7, 125, None, None, False, "PCIe Gen3", "NVLink 2, 300 GB/s", "SXM", 300, TI, [NV_V100],
   "Volta has no BF16 or FP8; tensor figure is dense (no sparsity on Volta).")
_s("NVIDIA Tesla V100 16GB PCIe", "NVIDIA", "Volta", "Volta", 2017, DC, 16, "HBM2", 0.9,
   14, 112, None, None, False, "PCIe Gen3", None, "PCIe", 250, TI, [NV_V100])
_s("NVIDIA Tesla V100 16GB", "NVIDIA", "Volta", "Volta", 2017, DC, 16, "HBM2", 0.9,
   None, None, None, None, False, None, None, None, None, TI, [NV_V100],
   "The provider does not say PCIe (112 TFLOPS, 250 W) or SXM2 (125 TFLOPS, 300 W), so those fields are empty.")

# ---------------------------------------------------------------- NVIDIA RTX PRO Blackwell
_rtxpro = "No dense FP16/FP8 tensor rate is published for RTX PRO Blackwell (only FP4 'AI TOPS' with sparsity), so left empty."
_s("NVIDIA RTX PRO 6000 96GB SE", "NVIDIA", "Blackwell", "Blackwell", 2025, DC, 96, "GDDR7", 1.597,
   120, None, None, None, True, "PCIe Gen5", None, "PCIe", 600, GI, [NV_RTXPRO6000_SE],
   "Server Edition, passively cooled, up to 600 W configurable; MIG up to 4 instances. " + _rtxpro)
_s("NVIDIA RTX PRO 6000 Blackwell 96GB Workstation", "NVIDIA", "Blackwell", "Blackwell", 2025, WS, 96, "GDDR7",
   1.792, 125, None, None, None, True, "PCIe Gen5", None, "PCIe", 600, GI, [NV_RTXPRO6000], _rtxpro)
_s("NVIDIA RTX PRO 6000 Blackwell 96GB Max-Q", "NVIDIA", "Blackwell", "Blackwell", 2025, WS, 96, "GDDR7",
   1.792, None, None, None, None, True, "PCIe Gen5", None, "PCIe", 300, GI, [NV_RTXPRO6000],
   "300 W variant of the workstation card; lower clocks, FP32 not confirmed. " + _rtxpro)
_s("NVIDIA RTX PRO 6000 SE MIG 1g.24gb", "NVIDIA", "Blackwell", "Blackwell", 2025, DC, 24, "GDDR7", None,
   None, None, None, None, True, None, None, "MIG slice", None, INF, [NV_RTXPRO6000_SE],
   "A quarter of one RTX PRO 6000 Server Edition (MIG, up to 4 instances), not a whole GPU.",
   parent="NVIDIA RTX PRO 6000 96GB SE", fraction_of_parent=1 / 4)
_s("NVIDIA RTX PRO 6000 SE MIG 2g.48gb", "NVIDIA", "Blackwell", "Blackwell", 2025, DC, 48, "GDDR7", None,
   None, None, None, None, True, None, None, "MIG slice", None, INF, [NV_RTXPRO6000_SE],
   "Half of one RTX PRO 6000 Server Edition (MIG), not a whole GPU.",
   parent="NVIDIA RTX PRO 6000 96GB SE", fraction_of_parent=1 / 2)
_s("NVIDIA RTX PRO 5000 Blackwell 48GB", "NVIDIA", "Blackwell", "Blackwell", 2025, WS, 48, "GDDR7", 1.344,
   None, None, None, None, True, "PCIe Gen5", None, "PCIe", 300, GI, [NV_RTXPRO5000], _rtxpro)
_s("NVIDIA RTX PRO 4500 Blackwell 32GB", "NVIDIA", "Blackwell", "Blackwell", 2025, WS, 32, "GDDR7", None,
   None, None, None, None, True, "PCIe Gen5", None, "PCIe", 200, GI, [NV_RTXPRO4500],
   "Bandwidth not confirmed from NVIDIA; left empty. " + _rtxpro)
_s("NVIDIA RTX PRO 4500 Blackwell 32GB SE", "NVIDIA", "Blackwell", "Blackwell", 2025, DC, 32, "GDDR7", 0.8,
   51, None, None, None, True, "PCIe Gen5", None, "PCIe", 165, GI, [NV_RTXPRO4500_SE], _rtxpro)
_s("NVIDIA RTX PRO 4000 Blackwell 24GB", "NVIDIA", "Blackwell", "Blackwell", 2025, WS, 24, "GDDR7", 0.672,
   40, None, None, None, True, "PCIe Gen5", None, "PCIe", 145, GI, [NV_RTXPRO4000], _rtxpro)

# ---------------------------------------------------------------- NVIDIA workstation (Ada, Ampere, Turing)
_ada_ws = "Datasheet 'Tensor Performance' is FP8 with sparsity; dense FP8 is half, dense FP16 a quarter."
_s("NVIDIA RTX 6000 Ada 48GB", "NVIDIA", "Ada Lovelace", "Ada", 2022, WS, 48, "GDDR6", 0.96,
   91.1, 364.25, 364.25, 728.5, True, "PCIe Gen4", None, "PCIe", 300, GI, [NV_WS], _ada_ws)
_s("NVIDIA RTX 5000 Ada 32GB", "NVIDIA", "Ada Lovelace", "Ada", 2023, WS, 32, "GDDR6", 0.576,
   65.3, 261.1, 261.1, 522.2, True, "PCIe Gen4", None, "PCIe", 250, GI, [NV_WS], _ada_ws)
_s("NVIDIA RTX 4000 Ada 20GB", "NVIDIA", "Ada Lovelace", "Ada", 2023, WS, 20, "GDDR6", 0.36,
   26.7, 81.9, 81.9, 163.8, True, "PCIe Gen4", None, "PCIe", 130, GI, [NV_WS], _ada_ws)
_s("NVIDIA RTX 4000 SFF Ada 20GB", "NVIDIA", "Ada Lovelace", "Ada", 2023, WS, 20, "GDDR6", 0.28,
   19.2, 76.7, 76.7, 153.4, True, "PCIe Gen4", None, "PCIe", 70, GI, [NV_WS], _ada_ws)
_s("NVIDIA RTX 2000 Ada 16GB", "NVIDIA", "Ada Lovelace", "Ada", 2024, WS, 16, "GDDR6", 0.224,
   12.0, 48.0, 48.0, 96.0, True, "PCIe Gen4", None, "PCIe", 70, GI, [NV_WS], _ada_ws)
_amp_ws = "Datasheet 'Tensor Performance' is with sparsity; dense FP16/BF16 is half."
_s("NVIDIA RTX A6000 48GB", "NVIDIA", "Ampere", "Ampere", 2020, WS, 48, "GDDR6", 0.768,
   38.7, 154.8, 154.8, None, False, "PCIe Gen4", "NVLink bridge (pairs), 112.5 GB/s", "PCIe", 300, GI, [NV_WS], _amp_ws)
_s("NVIDIA RTX A5000 24GB", "NVIDIA", "Ampere", "Ampere", 2021, WS, 24, "GDDR6", 0.768,
   27.8, 111.1, 111.1, None, False, "PCIe Gen4", "NVLink bridge (pairs), 112.5 GB/s", "PCIe", 230, GI, [NV_WS], _amp_ws)
_s("NVIDIA RTX A4500 20GB", "NVIDIA", "Ampere", "Ampere", 2021, WS, 20, "GDDR6", 0.64,
   23.7, 94.6, 94.6, None, False, "PCIe Gen4", "NVLink bridge (pairs), 112.5 GB/s", "PCIe", 200, GI, [NV_WS], _amp_ws)
_s("NVIDIA RTX A4000 16GB", "NVIDIA", "Ampere", "Ampere", 2021, WS, 16, "GDDR6", 0.448,
   19.2, 76.7, 76.7, None, False, "PCIe Gen4", None, "PCIe", 140, GI, [NV_WS], _amp_ws)
_s("NVIDIA RTX A2000 6GB", "NVIDIA", "Ampere", "Ampere", 2021, WS, 6, "GDDR6", 0.288,
   8.0, 32.0, 32.0, None, False, "PCIe Gen4", None, "PCIe", 70, GI, [NV_WS],
   _amp_ws + " A 12 GB A2000 also exists: different product.")
_s("NVIDIA Quadro RTX 6000 24GB", "NVIDIA", "Turing", "Turing", 2018, WS, 24, "GDDR6", 0.672,
   16.3, 130.5, None, None, False, "PCIe Gen3", "NVLink bridge (pairs), 100 GB/s", "PCIe", 295, GI, [NV_WS],
   "Turing: no BF16/FP8, no sparsity; 130.5 TFLOPS tensor is the datasheet figure.")

# ---------------------------------------------------------------- NVIDIA GeForce (consumer)
_gf = "GeForce: fp16/bf16 is the FP32-accumulate tensor rate from NVIDIA's architecture whitepaper (FP16-accumulate is 2x)."
_gf_none = "Tensor rate not recorded (no per-SKU dense figure confirmed); FP32 is the boost-clock shader peak."
_s("NVIDIA RTX 5090 32GB", "NVIDIA", "Blackwell", "Blackwell", 2025, CON, 32, "GDDR7", 1.792,
   104.8, 209.5, 209.5, None, True, "PCIe Gen5", None, "PCIe", 575, GI, [NV_GEFORCE, NV_BLACKWELL_WP], _gf)
_s("NVIDIA RTX 5090 Laptop 24GB", "NVIDIA", "Blackwell", "Blackwell", 2025, LAP, 24, "GDDR7", 0.896,
   None, None, None, None, True, "PCIe Gen5", None, "Laptop", None, GI, [NV_GEFORCE],
   "Laptop part: clocks and power (95-150 W+) depend on the laptop, so throughput and TDP are left empty.")
_s("NVIDIA RTX 5080 16GB", "NVIDIA", "Blackwell", "Blackwell", 2025, CON, 16, "GDDR7", 0.96,
   56.3, None, None, None, True, "PCIe Gen5", None, "PCIe", 360, GI, [NV_GEFORCE], _gf_none)
_s("NVIDIA RTX 5070 Ti 16GB", "NVIDIA", "Blackwell", "Blackwell", 2025, CON, 16, "GDDR7", 0.896,
   43.9, None, None, None, True, "PCIe Gen5", None, "PCIe", 300, GI, [NV_GEFORCE], _gf_none)
_s("NVIDIA RTX 5070 12GB", "NVIDIA", "Blackwell", "Blackwell", 2025, CON, 12, "GDDR7", 0.672,
   30.9, None, None, None, True, "PCIe Gen5", None, "PCIe", 250, GI, [NV_GEFORCE], _gf_none)
_s("NVIDIA RTX 5060 Ti 16GB", "NVIDIA", "Blackwell", "Blackwell", 2025, CON, 16, "GDDR7", 0.448,
   23.7, None, None, None, True, "PCIe Gen5", None, "PCIe", 180, GI, [NV_GEFORCE], _gf_none)
_s("NVIDIA RTX 5060 8GB", "NVIDIA", "Blackwell", "Blackwell", 2025, CON, 8, "GDDR7", 0.448,
   19.2, None, None, None, True, "PCIe Gen5", None, "PCIe", 145, GI, [NV_GEFORCE], _gf_none)
_s("NVIDIA RTX 4090 24GB", "NVIDIA", "Ada Lovelace", "Ada", 2022, CON, 24, "GDDR6X", 1.008,
   82.6, 165.2, 165.2, None, True, "PCIe Gen4", None, "PCIe", 450, GI, [NV_GEFORCE, NV_ADA_WP], _gf)
_s("NVIDIA RTX 4080 SUPER 16GB", "NVIDIA", "Ada Lovelace", "Ada", 2024, CON, 16, "GDDR6X", 0.736,
   52.2, None, None, None, True, "PCIe Gen4", None, "PCIe", 320, GI, [NV_GEFORCE], _gf_none)
_s("NVIDIA RTX 4080 16GB", "NVIDIA", "Ada Lovelace", "Ada", 2022, CON, 16, "GDDR6X", 0.717,
   48.7, 97.5, 97.5, None, True, "PCIe Gen4", None, "PCIe", 320, GI, [NV_GEFORCE, NV_ADA_WP], _gf)
_s("NVIDIA RTX 4070 Ti SUPER 16GB", "NVIDIA", "Ada Lovelace", "Ada", 2024, CON, 16, "GDDR6X", 0.672,
   44.1, None, None, None, True, "PCIe Gen4", None, "PCIe", 285, GI, [NV_GEFORCE], _gf_none)
_s("NVIDIA RTX 4070 Ti 12GB", "NVIDIA", "Ada Lovelace", "Ada", 2023, CON, 12, "GDDR6X", 0.504,
   40.1, None, None, None, True, "PCIe Gen4", None, "PCIe", 285, GI, [NV_GEFORCE], _gf_none)
_s("NVIDIA RTX 4070 12GB", "NVIDIA", "Ada Lovelace", "Ada", 2023, CON, 12, "GDDR6X", 0.504,
   29.1, None, None, None, True, "PCIe Gen4", None, "PCIe", 200, GI, [NV_GEFORCE], _gf_none)
_s("NVIDIA RTX 4070 Laptop 8GB", "NVIDIA", "Ada Lovelace", "Ada", 2023, LAP, 8, "GDDR6", 0.256,
   None, None, None, None, True, "PCIe Gen4", None, "Laptop", None, GI, [NV_GEFORCE],
   "Laptop part: throughput depends on the laptop's power limit (35-115 W).")
_s("NVIDIA RTX 4060 Ti 16GB", "NVIDIA", "Ada Lovelace", "Ada", 2023, CON, 16, "GDDR6", 0.288,
   22.1, None, None, None, True, "PCIe Gen4", None, "PCIe", 165, GI, [NV_GEFORCE], _gf_none)
_s("NVIDIA RTX 4060 8GB", "NVIDIA", "Ada Lovelace", "Ada", 2023, CON, 8, "GDDR6", 0.272,
   15.1, None, None, None, True, "PCIe Gen4", None, "PCIe", 115, GI, [NV_GEFORCE], _gf_none)
_s("NVIDIA RTX 3090 Ti 24GB", "NVIDIA", "Ampere", "Ampere", 2022, CON, 24, "GDDR6X", 1.008,
   40.0, None, None, None, False, "PCIe Gen4", "NVLink bridge (pairs)", "PCIe", 450, GI, [NV_GEFORCE], _gf_none)
_s("NVIDIA RTX 3090 24GB", "NVIDIA", "Ampere", "Ampere", 2020, CON, 24, "GDDR6X", 0.936,
   35.6, 71, 71, None, False, "PCIe Gen4", "NVLink bridge (pairs)", "PCIe", 350, GI, [NV_GEFORCE, NV_AMPERE_WP], _gf)
_s("NVIDIA RTX 3080 Ti 12GB", "NVIDIA", "Ampere", "Ampere", 2021, CON, 12, "GDDR6X", 0.912,
   34.1, None, None, None, False, "PCIe Gen4", None, "PCIe", 350, GI, [NV_GEFORCE], _gf_none)
_s("NVIDIA RTX 3080 10GB", "NVIDIA", "Ampere", "Ampere", 2020, CON, 10, "GDDR6X", 0.76,
   29.8, 59.5, 59.5, None, False, "PCIe Gen4", None, "PCIe", 320, GI, [NV_GEFORCE, NV_AMPERE_WP], _gf)
_s("NVIDIA RTX 3070 Ti 8GB", "NVIDIA", "Ampere", "Ampere", 2021, CON, 8, "GDDR6X", 0.608,
   21.7, None, None, None, False, "PCIe Gen4", None, "PCIe", 290, GI, [NV_GEFORCE], _gf_none)
_s("NVIDIA RTX 3070 8GB", "NVIDIA", "Ampere", "Ampere", 2020, CON, 8, "GDDR6", 0.448,
   20.3, 40.6, 40.6, None, False, "PCIe Gen4", None, "PCIe", 220, GI, [NV_GEFORCE, NV_AMPERE_WP], _gf)
_s("NVIDIA RTX 3060 Ti 8GB", "NVIDIA", "Ampere", "Ampere", 2020, CON, 8, "GDDR6", 0.448,
   16.2, None, None, None, False, "PCIe Gen4", None, "PCIe", 200, GI, [NV_GEFORCE], _gf_none)
_s("NVIDIA RTX 3060 12GB", "NVIDIA", "Ampere", "Ampere", 2021, CON, 12, "GDDR6", 0.36,
   12.7, None, None, None, False, "PCIe Gen4", None, "PCIe", 170, GI, [NV_GEFORCE], _gf_none)
_s("NVIDIA RTX 3060 8GB", "NVIDIA", "Ampere", "Ampere", 2022, CON, 8, "GDDR6", 0.24,
   12.7, None, None, None, False, "PCIe Gen4", None, "PCIe", 170, GI, [NV_GEFORCE],
   "128-bit bus: two-thirds of the 12 GB card's bandwidth. " + _gf_none)
_s("NVIDIA RTX 3050 8GB", "NVIDIA", "Ampere", "Ampere", 2022, CON, 8, "GDDR6", 0.224,
   9.1, None, None, None, False, "PCIe Gen4", None, "PCIe", 130, GI, [NV_GEFORCE], _gf_none)
_turing = "Turing GeForce: no BF16/FP8. " + _gf_none
_s("NVIDIA RTX 2080 Ti 11GB", "NVIDIA", "Turing", "Turing", 2018, CON, 11, "GDDR6", 0.616,
   13.4, None, None, None, False, "PCIe Gen3", "NVLink bridge (pairs)", "PCIe", 250, GI, [NV_GEFORCE], _turing)
_s("NVIDIA RTX 2080 8GB", "NVIDIA", "Turing", "Turing", 2018, CON, 8, "GDDR6", 0.448,
   10.1, None, None, None, False, "PCIe Gen3", None, "PCIe", 215, GI, [NV_GEFORCE], _turing)
_s("NVIDIA RTX 2070 8GB", "NVIDIA", "Turing", "Turing", 2018, CON, 8, "GDDR6", 0.448,
   7.5, None, None, None, False, "PCIe Gen3", None, "PCIe", 175, GI, [NV_GEFORCE], _turing)
_s("NVIDIA RTX 2060 6GB", "NVIDIA", "Turing", "Turing", 2019, CON, 6, "GDDR6", 0.336,
   6.5, None, None, None, False, "PCIe Gen3", None, "PCIe", 160, GI, [NV_GEFORCE], _turing)
_gtx = "GTX: no tensor cores, no BF16/FP8."
_s("NVIDIA GTX 1660 SUPER 6GB", "NVIDIA", "Turing", "Turing", 2019, CON, 6, "GDDR6", 0.336,
   5.0, None, None, None, False, "PCIe Gen3", None, "PCIe", 125, GI, [NV_GEFORCE], _gtx)
_s("NVIDIA GTX 1660 6GB", "NVIDIA", "Turing", "Turing", 2019, CON, 6, "GDDR5", 0.192,
   5.0, None, None, None, False, "PCIe Gen3", None, "PCIe", 120, GI, [NV_GEFORCE], _gtx)
_s("NVIDIA GTX 1650 4GB", "NVIDIA", "Turing", "Turing", 2019, CON, 4, None, None,
   2.9, None, None, None, False, "PCIe Gen3", None, "PCIe", 75, GI, [NV_GEFORCE],
   _gtx + " Sold with GDDR5 (128 GB/s) or GDDR6 (192 GB/s); the provider does not say which.")
_s("NVIDIA GTX 1060 6GB", "NVIDIA", "Pascal", "Pascal", 2016, CON, 6, "GDDR5", 0.192,
   4.4, None, None, None, False, "PCIe Gen3", None, "PCIe", 120, GI, [NV_GEFORCE], _gtx)
_s("NVIDIA GTX 1050 Ti 4GB", "NVIDIA", "Pascal", "Pascal", 2016, CON, 4, "GDDR5", 0.112,
   2.1, None, None, None, False, "PCIe Gen3", None, "PCIe", 75, GI, [NV_GEFORCE], _gtx)

# ---------------------------------------------------------------- AMD Instinct
_s("AMD Instinct MI355X 288GB", "AMD", "CDNA 4", "CDNA 4", 2025, DC, 288, "HBM3E", 8.0,
   157.3, 2500, 2500, 5000, True, "PCIe Gen5", "Infinity Fabric", "OAM", 1400, TR, [AMD_MI355X],
   "AMD dense matrix figures (2.5 PF FP16/BF16, 5.0 PF FP8; 2x with sparsity). Liquid-cooled, 1,400 W TBP.")
_s("AMD Instinct MI350 294GB", "AMD", "CDNA 4", "CDNA 4", 2025, DC, 288, "HBM3E", 8.0,
   None, None, None, None, True, "PCIe Gen5", "Infinity Fabric", "OAM", None, TR, [AMD_MI350X, AMD_MI355X],
   "The provider says 'MI350' with 294 GB. AMD sells MI350X (2.3 PF FP16, 1,000 W) and MI355X (2.5 PF, 1,400 W), "
   "both 288 GB HBM3E at 8 TB/s; which one is not stated, so throughput and power are left empty.")
_s("AMD Instinct MI325X 256GB", "AMD", "CDNA 3", "CDNA 3", 2024, DC, 256, "HBM3E", 6.0,
   163.4, 1307.4, 1307.4, 2614.9, True, "PCIe Gen5", "Infinity Fabric, 896 GB/s aggregate", "OAM", 1000, TR,
   [AMD_MI325X], "AMD dense matrix figures; 2x with sparsity.")
_s("AMD Instinct MI300X 192GB", "AMD", "CDNA 3", "CDNA 3", 2023, DC, 192, "HBM3", 5.3,
   163.4, 1307.4, 1307.4, 2614.9, True, "PCIe Gen5", "Infinity Fabric, 896 GB/s aggregate", "OAM", 750, TR,
   [AMD_MI300X], "AMD dense matrix figures; 2x with sparsity.")

# ---------------------------------------------------------------- AMD Radeon (consumer)
_rdna = "Consumer Radeon: FP32 only (AMD's FP16 figures mix shader and matrix rates; not recorded)."
_s("AMD RX 9070 XT 16GB", "AMD", "RDNA 4", "RDNA 4", 2025, CON, 16, "GDDR6", 0.6446,
   48.7, None, None, None, True, "PCIe Gen5", None, "PCIe", 304, GI, [AMD_RADEON], _rdna)
_s("AMD RX 9060 XT 16GB", "AMD", "RDNA 4", "RDNA 4", 2025, CON, 16, "GDDR6", 0.3223,
   25.6, None, None, None, True, "PCIe Gen5", None, "PCIe", 160, GI, [AMD_RADEON], _rdna)
_s("AMD RX 7900 XTX 24GB", "AMD", "RDNA 3", "RDNA 3", 2022, CON, 24, "GDDR6", 0.96,
   61.4, None, None, None, False, "PCIe Gen4", None, "PCIe", 355, GI, [AMD_RADEON], _rdna)
_s("AMD RX 7900 XT 20GB", "AMD", "RDNA 3", "RDNA 3", 2022, CON, 20, "GDDR6", 0.8,
   51.6, None, None, None, False, "PCIe Gen4", None, "PCIe", 315, GI, [AMD_RADEON], _rdna)
_s("AMD RX 7800 XT 16GB", "AMD", "RDNA 3", "RDNA 3", 2023, CON, 16, "GDDR6", 0.624,
   37.3, None, None, None, False, "PCIe Gen4", None, "PCIe", 263, GI, [AMD_RADEON], _rdna)

# ---------------------------------------------------------------- Intel
_s("Intel Gaudi 2 96GB", "Intel", "Gaudi 2", "Gaudi 2", 2022, DC, 96, "HBM2E", 2.45,
   None, None, None, None, True, "PCIe Gen4", "24x 100 GbE RoCE on-chip", "OAM", 600, TR, [INTEL_GAUDI2],
   "Intel does not publish a single dense BF16/FP8 matrix peak comparable to the others; left empty.")


def spec(name: str) -> dict | None:
    """Hardware facts for one canonical GPU name (a copy), or None if we have no entry."""
    d = _SPECS.get(name)
    return None if d is None else {"name": name, **d, "sources": list(d["sources"])}


def all_specs() -> dict[str, dict]:
    return {n: spec(n) for n in sorted(_SPECS)}


def summary(name: str) -> dict | None:
    """The handful of fields a list view needs."""
    d = _SPECS.get(name)
    if d is None:
        return None
    keys = ("vendor", "architecture", "vram_gb", "memory_type", "memory_bandwidth_tbps", "bf16_tflops_dense",
            "fp8_tflops_dense", "form_factor", "workload_class", "segment", "released")
    return {k: d[k] for k in keys}


def missing() -> list[str]:
    """Canonical names with no hardware entry (should be empty; tests enforce it)."""
    return [n for n in canonical.all_canonical_names() if n not in _SPECS]
