"""GPU names the live modules of providers not merged yet (providers/batch-nokey: Paperspace, Akash, CUDO, GPU.ai,
Exoscale, Hugging Face, Hydra Host) teach canonical.py, so archived pages of those providers map to the SAME canonical
GPU the live rows will have. Copied verbatim from that branch's canonical.py additions on 2026-11-01.

canonical_name(raw) asks canonical.py first; ALIASES only fills names it does not know yet. Once the branch is
merged this module is redundant (every name here is then in canonical.py) and can be deleted; a re-import fills
`gpu` on rows stored unmapped (backfill/load.store).
"""
from __future__ import annotations

import canonical

ALIASES: dict[str, str | None] = {
    'A4000 pcie': 'NVIDIA RTX A4000 16GB',
    'A5000 pcie': 'NVIDIA RTX A5000 24GB',
    'A6000 pcie': 'NVIDIA RTX A6000 48GB',
    'A100 pcie nvlink': None,
    'V100 pcie': None,
    'V100_32G pcie': None,
    'H100 (Paperspace: HGX per its page, pcie per Shadeform)': None,
    'nvidia a100 80Gi SXM4': 'NVIDIA A100 80GB SXM4',
    'nvidia a10g 24Gi PCIe': 'NVIDIA A10G 24GB',
    'nvidia b300 288Gi SXM6': 'NVIDIA B300 288GB SXM',
    'nvidia h100 80Gi SXM5': 'NVIDIA H100 80GB SXM5',
    'nvidia pro6000se 96Gi PCIe': 'NVIDIA RTX PRO 6000 96GB SE',
    'nvidia pro6000we 96Gi PCIe': 'NVIDIA RTX PRO 6000 Blackwell 96GB Workstation',
    'nvidia gtx1050ti 4Gi PCIe': 'NVIDIA GTX 1050 Ti 4GB',
    'nvidia rtx3060 12Gi PCIe': 'NVIDIA RTX 3060 12GB',
    'nvidia rtx3060ti 8Gi PCIe': 'NVIDIA RTX 3060 Ti 8GB',
    'nvidia rtx3090 24Gi PCIe': 'NVIDIA RTX 3090 24GB',
    'nvidia rtx3090ti 24Gi PCIe': 'NVIDIA RTX 3090 Ti 24GB',
    'nvidia rtx4000ada 20Gi PCIe': 'NVIDIA RTX 4000 Ada 20GB',
    'nvidia rtx4000sada 20Gi PCIe': 'NVIDIA RTX 4000 SFF Ada 20GB',
    'nvidia rtx4080 16Gi PCIe': 'NVIDIA RTX 4080 16GB',
    'nvidia rtx4090 24Gi PCIe': 'NVIDIA RTX 4090 24GB',
    'nvidia rtx5090 32Gi PCIe': 'NVIDIA RTX 5090 32GB',
    'nvidia t4 16Gi PCIe': 'NVIDIA T4 16GB',
    'nvidia v100 16Gi SXM2': 'NVIDIA Tesla V100 16GB SXM2',
    'nvidia-h100': 'NVIDIA H100 80GB SXM5',
    'nvidia-h100-nvl-pcie': 'NVIDIA H100 94GB NVL',
    'nvidia-l40s': 'NVIDIA L40S 48GB',
    'nvidia-a100-pcie': None,
    'nvidia-l40s-graphics': None,
    'h100_sxm': 'NVIDIA H100 80GB SXM5',
    'h100_pcie': 'NVIDIA H100 80GB PCIe',
    'h200_sxm': 'NVIDIA H200 141GB SXM5',
    'b300': 'NVIDIA B300 288GB SXM',
    'l4': 'NVIDIA L4 24GB',
    'rtx_2000_ada': 'NVIDIA RTX 2000 Ada 16GB',
    'rtx_4000_ada': 'NVIDIA RTX 4000 Ada 20GB',
    'rtx_6000_ada': 'NVIDIA RTX 6000 Ada 48GB',
    'rtx_a6000': 'NVIDIA RTX A6000 48GB',
    'rtx_a4000': 'NVIDIA RTX A4000 16GB',
    'rtx_3090': 'NVIDIA RTX 3090 24GB',
    'rtx_4090': 'NVIDIA RTX 4090 24GB',
    'rtx_5090': 'NVIDIA RTX 5090 32GB',
    'a100_80gb': None,
    'rtx_pro_6000': None,
    'rtx_pro_4500': None,
    'Exoscale GPU2 (Tesla V100 16GB)': 'NVIDIA Tesla V100 16GB',
    'Exoscale GPU3 (NVIDIA A40 48GB)': 'NVIDIA A40 48GB',
    'Exoscale GPUA5000 (NVIDIA RTX A5000 24GB)': 'NVIDIA RTX A5000 24GB',
    'Exoscale GPU3080ti (NVIDIA RTX 3080 Ti 12GB)': 'NVIDIA RTX 3080 Ti 12GB',
    'Exoscale GPUA30 (NVIDIA A30 24GB)': None,
    'Exoscale GPURTX6000PRO (NVIDIA RTX PRO 6000 Blackwell 96GB)': None,
    'aws Nvidia T4 16GB': 'NVIDIA T4 16GB',
    'gcp Nvidia T4 16GB': 'NVIDIA T4 16GB',
    'aws Nvidia A10G 24GB': 'NVIDIA A10G 24GB',
    'aws Nvidia L4 24GB': 'NVIDIA L4 24GB',
    'gcp Nvidia L4 24GB': 'NVIDIA L4 24GB',
    'aws Nvidia L40S 48GB': 'NVIDIA L40S 48GB',
    'aws Nvidia A100 80GB': 'NVIDIA A100 80GB SXM4',
    'gcp Nvidia A100 80GB': 'NVIDIA A100 80GB SXM4',
    'gcp Nvidia H100 80GB': 'NVIDIA H100 80GB SXM5',
    'aws Nvidia H200 141GB': 'NVIDIA H200 141GB SXM5',
    'gcp Nvidia H200 141GB': 'NVIDIA H200 141GB SXM5',
    'aws Nvidia RTX PRO 6000 Blackwell 96GB': 'NVIDIA RTX PRO 6000 96GB SE',
    'Quadro RTX 8000': None,
    'nvidia rtx5000 16Gi PCIe': None,
    'nvidia v100 32Gi PCIe': None,
    'nvidia gtx1050 2Gi PCIe': None,
    'nvidia m4000 8Gi PCIe': None,
    'nvidia p4 8Gi PCIe': None,
    'nvidia p40 24Gi PCIe': None,
}


_LOWER = {" ".join(k.split()).lower(): v for k, v in ALIASES.items()}


def canonical_name(raw: str) -> str | None:
    known = canonical.canonical_gpu_name(raw)
    return known if known is not None else _LOWER.get(" ".join(raw.split()).lower())
