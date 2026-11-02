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
    test_new_models_have_sourced_hardware()
    test_thunder_scoped_h100()
    test_reasons_recorded()
    print("all passed")
