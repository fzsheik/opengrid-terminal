"""Unmapped-queue cleanup (2026-11-02): new canonical models with vendor-sourced hardware, aliases only for names that
state the variant, Thunder Compute's provider-scoped H100, and names that must stay unmapped (with a recorded reason).

Run:  .venv/bin/python tests/test_unmapped_cleanup.py
"""

import os
import sys
from pathlib import Path

os.environ.setdefault("OPENGRID_NO_JOBS", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import canonical  # noqa: E402
import hardware  # noqa: E402
import mapping  # noqa: E402
from providers import thundercompute  # noqa: E402

MAPPED = {
    # raw name -> canonical
    "NVIDIA A30 24GB": "NVIDIA A30 24GB", "A30 (24GB)": "NVIDIA A30 24GB",
    "Exoscale GPUA30 (NVIDIA A30 24GB)": "NVIDIA A30 24GB",
    "NVIDIA GB200 NVL72 186GB": "NVIDIA GB200 186GB", "NVIDIA GB200 189GB (NVL72)": "NVIDIA GB200 186GB",
    "H100 NVL (94GB) NVLink": "NVIDIA H100 94GB NVL", "H200 NVL (141GB) NVLink": "NVIDIA H200 143GB NVL",
    "Quadro RTX 8000": "NVIDIA Quadro RTX 8000 48GB",
    "NVIDIA RTX PRO 4000 Blackwell SFF Edition 24GB": "NVIDIA RTX PRO 4000 Blackwell SFF 24GB",
    "Tesla V100-SXM2-32GB (32 GB)": "NVIDIA Tesla V100 32GB SXM2", "Tesla V100SXM232GB 32GB": "NVIDIA Tesla V100 32GB SXM2",
    "NVIDIA Tesla V100-SXM2-32GB": "NVIDIA Tesla V100 32GB SXM2",
    "Tesla V100PCIE32GB 32GB": "NVIDIA Tesla V100 32GB PCIe", "nvidia v100 32Gi PCIe": "NVIDIA Tesla V100 32GB PCIe",
    "Tesla V100SPCIE32GB 32GB": "NVIDIA Tesla V100S 32GB PCIe", "Tesla V100S 32GB": "NVIDIA Tesla V100S 32GB PCIe",
    "NVIDIA K80": "NVIDIA Tesla K80 12GB", "NVIDIA Tesla M60": "NVIDIA Tesla M60 8GB",
    "A100-SXM4-40GB (40 GB)": "NVIDIA A100 40GB SXM4", "NVIDIA T4 Tensor Core GPU": "NVIDIA T4 16GB",
    "RTX A6000": "NVIDIA RTX A6000 48GB", "RTX A6000 (48GB) [Premium]": "NVIDIA RTX A6000 48GB",
    "NVIDIA GeForce RTX 5070 Ti": "NVIDIA RTX 5070 Ti 16GB",
}
# Variant not stated anywhere: must stay unmapped (never guessed, never merged).
UNMAPPED = ["NVIDIA H100 80GB", "NVIDIA A100 80GB", "NVIDIA V100 32GB", "V100_32G pcie", "H100 80GB", "A100 SXM4",
            "NVIDIA GeForce RTX 5060 Ti", "NVIDIA B300 SXM6 PC", "NVIDIA GRID K520", "H100 80 GB GPU",
            "NVIDIA Tesla V100 Tensor Core GPU", "RTX PRO 6000 96GB"]


# Capacity-queue cleanup (2026-11-15): names that state one existing model. No new canonical model.
MAPPED.update({
    # Clore: one-product boards, at the nominal or the usable (ECC) memory figure
    "Tesla T4 16GB": "NVIDIA T4 16GB", "Tesla T4 15GB": "NVIDIA T4 16GB", "L4 23GB": "NVIDIA L4 24GB",
    "L40S 45GB": "NVIDIA L40S 48GB", "A10 24GB": "NVIDIA A10 24GB PCIe", "A10 23GB": "NVIDIA A10 24GB PCIe",
    "A30 24GB": "NVIDIA A30 24GB", "A40 48GB": "NVIDIA A40 48GB",
    # Clore: the name spells the board
    "A100PCIE40GB 40GB": "NVIDIA A100 40GB PCIe", "A100SXM480GB 80GB": "NVIDIA A100 80GB SXM4",
    "H100 PCIe 80GB": "NVIDIA H100 80GB PCIe", "H100 PCIe 79GB": "NVIDIA H100 80GB PCIe",
    "H100 80GB HBM3 80GB": "NVIDIA H100 80GB SXM5",
    "RTX 6000 Ada Generation 48GB": "NVIDIA RTX 6000 Ada 48GB", "RTX A5000 24GB": "NVIDIA RTX A5000 24GB",
    "RTX A4500 20GB": "NVIDIA RTX A4500 20GB", "GTX 1660 6GB": "NVIDIA GTX 1660 6GB",
    "RTX PRO 4000 Blackwell SFF Edition 24GB": "NVIDIA RTX PRO 4000 Blackwell SFF 24GB",
    # Lium: one memory size each
    "NVIDIA GeForce RTX 3070 Ti": "NVIDIA RTX 3070 Ti 8GB", "NVIDIA GeForce RTX 5070": "NVIDIA RTX 5070 12GB",
    "NVIDIA GeForce RTX 4070 Ti SUPER": "NVIDIA RTX 4070 Ti SUPER 16GB",
    # GPU.ai slugs
    "h100_nvl": "NVIDIA H100 94GB NVL", "h200_nvl": "NVIDIA H200 143GB NVL", "rtx_4080": "NVIDIA RTX 4080 16GB",
    "rtx_5080": "NVIDIA RTX 5080 16GB", "rtx_a4500": "NVIDIA RTX A4500 20GB", "rtx_a5000": "NVIDIA RTX A5000 24GB",
    # Akash, Koyeb, Massed Compute
    "nvidia rtx4060 8Gi PCIe": "NVIDIA RTX 4060 8GB", "nvidia rtxa2000 6Gi PCIe": "NVIDIA RTX A2000 6GB",
    "nvidia v100 32Gi SXM2": "NVIDIA Tesla V100 32GB SXM2", "gpu-nvidia-b200 180GB": "NVIDIA B200 180GB SXM",
    "DGX A100 (80GB)": "NVIDIA A100 80GB SXM4",
})
# Still unmapped: variant not stated, sources in conflict, an odd memory figure on a GeForce / workstation card, a
# confidential-computing type, a card with no canonical model, or not a GPU model at all.
UNMAPPED += [
    "H200 141GB", "B300 SXM6 PC 192GB", "RTX 4090 23GB", "RTX 3090 20GB", "RTX 4070 11GB", "RTX 4070 Ti SUPER 15GB",
    "RTX 4060 Ti 15GB", "RTX 4060 7GB", "RTX 5070 Ti 15GB", "RTX 5060 Ti 15GB", "RTX A4500 19GB",
    "NVIDIA GeForce RTX 3060", "NVIDIA GeForce RTX 4060 Ti", "a100_40gb", "v100", "rtx_3080", "a16",
    "RTX6000-Blackwell 96GB", "RTX6000Ada sxm5", "RTXPro6000 pcie", "RTX6000", "A100 (80GB)", "H100 (80GB)",
    "B200 CC SXM6 180GB", "B300 CC SXM6 268GB", "RTX PRO 6000 CC 96GB", "gpu-nvidia-h200 141GB",
    # no canonical model (none was added): variants, laptop parts, older and mining cards
    "RTX 4060 Ti 7GB", "RTX 5060 Ti 8GB", "RTX 3080 12GB", "RTX 4090 D 24GB", "RTX 4090 D 48GB", "RTX 5090 D 32GB",
    "RTX 4070 SUPER 12GB", "RTX 4090 Laptop GPU 16GB", "RTX PRO 5000 72GB Blackwell 72GB", "GTX 1660 Ti 6GB",
    "Tesla P40 24GB", "CMP 170HX 40GB", "A2 14GB", "nvidia gtx1660ti 6Gi PCIe", "amd_mi350x", "P100 16GB",
    "gpu-tenstorrent-n300s 24GB", "RTX 4060 Ti (8 GB)", "RTX 5080 Laptop (16 GB)", "NVIDIA RTX 6000D",
]
NOT_GPU_MODELS = ["NVIDIA Graphics Device", "Graphics Device 64GB", "Graphics Device 8GB", "NVxx 16GB",
                  "nvidia CANARYI16622GPUMODEL 24Gi PCIe", "Stable Diffusion Compatible",
                  "GTX 1070, 1080, 1080Ti (8 GB)"]


def test_capacity_queue_cleanup():
    assert len(canonical.all_canonical_names()) == 100, "aliases only: the model count does not move"
    assert set(MAPPED.values()) <= set(canonical.all_canonical_names())
    for raw in NOT_GPU_MODELS:
        assert canonical.canonical_gpu_name(raw) is None and canonical.NOT_A_SINGLE_GPU.get(raw), raw
    # never merged: PCIe is not SXM, 40 GB is not 80 GB, the NVL card is not the SXM part
    assert canonical.canonical_gpu_name("H100 PCIe 80GB") != canonical.canonical_gpu_name("H100 80GB HBM3 80GB")
    assert canonical.canonical_gpu_name("A100PCIE40GB 40GB") != canonical.canonical_gpu_name("A100 80GB PCIe 80GB")
    assert canonical.canonical_gpu_name("h200_nvl") != canonical.canonical_gpu_name("h200_sxm")
    # a deliberate non-mapping is registered, so it reads as "seen", not as new hardware
    for raw in ("H200 141GB", "RTX 4090 23GB", "rtx_3080", "RTX6000", "B200 CC SXM6 180GB", "gpu-nvidia-h100 80GB"):
        assert canonical.is_known(raw), raw
    print("test_capacity_queue_cleanup ok")


def test_aliases():
    for raw, want in MAPPED.items():
        assert canonical.canonical_gpu_name(raw) == want, (raw, canonical.canonical_gpu_name(raw), want)
    for raw in UNMAPPED:
        assert canonical.canonical_gpu_name(raw) is None, raw
    print("test_aliases ok")


def test_new_models_have_sourced_hardware():
    assert hardware.missing() == []
    new = {"NVIDIA A30 24GB": (24, "PCIe", 0.933), "NVIDIA GB200 186GB": (186, "Superchip", 8.0),
           "NVIDIA Quadro RTX 8000 48GB": (48, "PCIe", 0.672), "NVIDIA RTX PRO 4000 Blackwell SFF 24GB": (24, "PCIe", 0.432),
           "NVIDIA Tesla V100 32GB SXM2": (32, "SXM", 0.9), "NVIDIA Tesla V100 32GB PCIe": (32, "PCIe", 0.9),
           "NVIDIA Tesla V100S 32GB PCIe": (32, "PCIe", 1.134), "NVIDIA Tesla K80 12GB": (12, "PCIe", 0.24),
           "NVIDIA Tesla M60 8GB": (8, "PCIe", 0.16)}
    for name, (vram, form, bw) in new.items():
        s = hardware.spec(name)
        assert (s["vram_gb"], s["form_factor"], s["memory_bandwidth_tbps"]) == (vram, form, bw), name
        assert s["sources"] and all(u.startswith("https://") for u in s["sources"]), name
    # never merged: SFF is not the full-height card, V100S is not V100, a K80 GPU is half a board
    assert hardware.spec("NVIDIA RTX PRO 4000 Blackwell 24GB")["tdp_w"] == 145
    assert hardware.spec("NVIDIA RTX PRO 4000 Blackwell SFF 24GB")["tdp_w"] == 70
    assert hardware.spec("NVIDIA Tesla K80 12GB")["fp32_tflops"] is None, "the board spec gives no FLOPS: not guessed"
    gb = hardware.spec("NVIDIA GB200 186GB")
    assert gb["fp8_tflops_dense"] == 2 * gb["bf16_tflops_dense"] == 5000
    print("test_new_models_have_sourced_hardware ok")


def test_thunder_scoped_h100():
    assert thundercompute.canonical_name("NVIDIA H100 80GB") == "NVIDIA H100 80GB SXM5"
    assert thundercompute.canonical_name("NVIDIA A100 80GB") is None
    assert canonical.canonical_gpu_name("NVIDIA H100 80GB") is None, "scoped to Thunder, never global"
    print("test_thunder_scoped_h100 ok")


def test_reasons_recorded():
    r = mapping.UNMAPPED_REASONS
    for key in (("oracle", "NVIDIA H100 80GB"), ("gcore", "H100 80GB"), ("sakura", "NVIDIA H100 80GB"),
                ("vast", "A100 SXM4"), ("aws", "NVIDIA GRID K520"), ("thundercompute", "NVIDIA A100 80GB")):
        assert r.get(key), key
    # a reason is only for names that really are unmapped
    mapped = [k for k in r if canonical.canonical_gpu_name(k[1])]
    assert mapped == [], f"reasons recorded for mapped names: {mapped}"
    print("test_reasons_recorded ok")


if __name__ == "__main__":
    test_aliases()
    test_capacity_queue_cleanup()
    test_new_models_have_sourced_hardware()
    test_thunder_scoped_h100()
    test_reasons_recorded()
    print("all passed")
