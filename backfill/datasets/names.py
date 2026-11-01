"""Dataset labels -> OpenGrid names. Explicit tables only, like canonical.py: nothing is guessed.

A dataset GPU label that hides a variant (FSDL's "A100 (80 GB)" is SXM4 on some clouds and PCIe on others) is
resolved only from the row's own instance / plan name, through the providers' own tables (providers/aws.py
GPU_INSTANCES, providers/azure.py GPU_SIZES, providers/gcp.py machine families) or a form factor the name spells
out ("sxm", "pcie"). Otherwise it stays unmapped (gpu = null: kept, not charted), as in the live pipeline.
"""
from __future__ import annotations

import re

import canonical
from backfill.aws_price_list import BACKFILL_INSTANCES
from providers.azure import GPU_SIZES

# FSDL "Cloud" column -> OpenGrid provider id. Clouds OpenGrid does not track are skipped and counted.
FSDL_PROVIDERS = {
    "AWS": "aws", "GCP": "gcp", "Azure": "azure", "Lambda": "lambda", "Jarvislabs": "jarvislabs",
    "Datacrunch": "verda", "OVHcloud": "ovhcloud", "RunPod": "runpod", "RunPod.io": "runpod",
    "Latitude.sh": "latitude", "Oracle Cloud": "oracle", "Thunder Compute": "thundercompute", "Baseten": "baseten",
    # Added on branch providers/batch-nokey (2026-11-01): ids as in providers/paperspace (shadeform.py), cudo.py,
    # exoscale.py there, so archived history joins the live listings after merge.
    "Paperspace": "paperspace", "Cudo Compute": "cudo", "Exoscale": "exoscale",
}

# Live provider tiers that must travel with any archived row of these providers (providers/batch-nokey):
# resellers are "resold:*" and never vote in market statistics. No dataset read here has rows for them today;
# a row that ever arrives is stored with this tier in its note.
RESOLD_TIERS = {"huggingface": "resold:<cloud>", "gpuai": "resold:undisclosed"}

# Paperspace's live listing ids are Shadeform's shade_instance_type ("A6000", "A5000x4"); FSDL labels -> that base.
_PAPERSPACE_BASE = {"a4000 (16 gb)": "A4000", "a5000 (24 gb)": "A5000", "a6000 (48 gb)": "A6000",
                    "rtx a6000 (48 gb)": "A6000"}


def fsdl_listing_id(cloud: str, row: dict, count: int) -> str:
    """A listing id that joins the live one where the live id is derivable from the row; otherwise a stable id
    from the row's own shape (name or GPU label, GPU count, vCPUs, RAM: CUDO lists one GPU at several sizes)."""
    name = (row.get("Name") or "").strip()
    label = re.sub(r"\s+", " ", row.get("GPU Type") or "").strip().lower()
    if cloud == "Exoscale" and name:
        return re.sub(r"\s+", ".", name.lower())                 # live: "<family>.<size>", e.g. gpu3.small
    if cloud == "Paperspace" and label in _PAPERSPACE_BASE:
        return _PAPERSPACE_BASE[label] + (f"x{count}" if count > 1 else "")
    shape = f"{(row.get('vCPUs') or '').strip()}vcpu:{(row.get('RAM') or '').strip()}gb"
    return f"{name or row.get('GPU Type', '').strip()}:{count}:{shape}"

# Unambiguous labels (any cloud). A100 / H100 / V100 32GB labels are NOT here: they need the instance name.
_PLAIN = {
    "v100 (16 gb)": "NVIDIA Tesla V100 16GB",
    "a6000 (48 gb)": "NVIDIA RTX A6000 48GB", "rtx a6000 (48 gb)": "NVIDIA RTX A6000 48GB",
    "a5000 (24 gb)": "NVIDIA RTX A5000 24GB", "a4000 (16 gb)": "NVIDIA RTX A4000 16GB",
    "t4 (16 gb)": "NVIDIA T4 16GB", "t4 (16)": "NVIDIA T4 16GB",
    "a10g (24 gb)": "NVIDIA A10G 24GB",
    "a10": "NVIDIA A10 24GB PCIe", "a10 (24 gb)": "NVIDIA A10 24GB PCIe",
    "a40 (48 gb)": "NVIDIA A40 48GB", "l4 (24 gb)": "NVIDIA L4 24GB", "l40s (48 gb)": "NVIDIA L40S 48GB",
    "l40": "NVIDIA L40 48GB", "quadro rtx 6000 (24 gb)": "NVIDIA Quadro RTX 6000 24GB",
    "3080ti (12 gb)": "NVIDIA RTX 3080 Ti 12GB", "gh200 (96 gb)": "NVIDIA GH200 96GB",
    "b200 (180 gb)": "NVIDIA B200 180GB SXM", "b300 (288 gb)": "NVIDIA B300 288GB SXM",
    "h100 hgx (80gb)": "NVIDIA H100 80GB SXM5", "h100 pcie (80gb)": "NVIDIA H100 80GB PCIe",
}
# Variant-hiding labels -> {form factor spelled in the name: canonical}
_BY_FORM = {
    "a100 (40 gb)": {"sxm": "NVIDIA A100 40GB SXM4", "pcie": "NVIDIA A100 40GB PCIe"},
    "a100 (80 gb)": {"sxm": "NVIDIA A100 80GB SXM4", "pcie": "NVIDIA A100 80GB PCIe"},
    "h100 (80 gb)": {"sxm": "NVIDIA H100 80GB SXM5", "pcie": "NVIDIA H100 80GB PCIe"},
    "h100 (80gb)": {"sxm": "NVIDIA H100 80GB SXM5", "pcie": "NVIDIA H100 80GB PCIe"},
}
# GCP machine families (providers/gcp.py MACHINES): A2 High = A100 40GB SXM4, A2 Ultra = A100 80GB SXM4,
# A3 High = H100 80GB SXM5, G2 = L4.
_GCP = ((re.compile(r"^a2-highgpu-|^a2-megagpu-"), "NVIDIA A100 40GB SXM4"),
        (re.compile(r"^a2-ultragpu-"), "NVIDIA A100 80GB SXM4"),
        (re.compile(r"^a3-highgpu-"), "NVIDIA H100 80GB SXM5"),
        (re.compile(r"^g2-standard-"), "NVIDIA L4 24GB"))


def fsdl_gpu(cloud: str, gpu_type: str, name: str) -> str | None:
    """Canonical GPU for one FSDL row, or None (kept unmapped)."""
    label = re.sub(r"\s+", " ", gpu_type or "").strip().lower()
    n = (name or "").strip()
    if cloud == "AWS" and n in BACKFILL_INSTANCES:
        return canonical.canonical_gpu_name(BACKFILL_INSTANCES[n][1])
    if cloud == "Azure":
        size = re.sub(r"\s+", "_", n)          # FSDL writes "NC24ads A100 v4" for Standard_NC24ads_A100_v4
        size = size if size.startswith("Standard_") else f"Standard_{size}"
        if size in GPU_SIZES:
            return canonical.canonical_gpu_name(GPU_SIZES[size][1])
    if cloud == "GCP":
        for pattern, gpu in _GCP:
            if pattern.match(n.lower()):
                return gpu
    if label in _PLAIN:
        return _PLAIN[label]
    if label in _BY_FORM:
        low = n.lower()
        forms = [f for f in ("sxm", "pcie") if f in low]
        return _BY_FORM[label][forms[0]] if len(forms) == 1 else None
    return None


# Huang (2026) labels are Vast's own gpu_name values. "A100 SXM4" pools Vast's 40GB and 80GB cards, so it stays
# unmapped; the others name one product.
HUANG_GPUS = {"H100 SXM": "NVIDIA H100 80GB SXM5", "RTX 3090": "NVIDIA RTX 3090 24GB",
              "RTX 4090": "NVIDIA RTX 4090 24GB", "RTX 5090": "NVIDIA RTX 5090 32GB", "A100 SXM4": None}
