"""Our own GPU naming. Hand-maintained, one entry per name we have met.

Nothing here is derived or guessed: a provider name we have not mapped returns
None and shows up in the `unmapped` view, so new hardware is a visible to-do
rather than a silently invented model.

Naming: "<VENDOR> <MODEL> <VRAM> [<VARIANT>]".

Variants matter and are never collapsed. Three different products:
    NVIDIA H100 80GB PCIe
    NVIDIA H100 80GB PCIe NVLink
    NVIDIA H100 80GB SXM5
So do memory variants, which the same model number can hide:
    NVIDIA RTX 3060 8GB   vs   NVIDIA RTX 3060 12GB
"""

import re

# Market suffixes are stripped before lookup: they are `market_type`, not hardware.
_MARKET_SUFFIXES = ("-spot",)


def _key(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip().lower()


# raw provider name -> our canonical name.
# None means "seen, deliberately not mapped" (see NOT_A_SINGLE_GPU below).
_RAW_TO_CANONICAL: dict[str, str | None] = {}


def _add(canonical: str | None, *raw_names: str) -> None:
    for raw in raw_names:
        _RAW_TO_CANONICAL[_key(raw)] = canonical


# --------------------------------------------------------------------------
# NVIDIA datacenter
# --------------------------------------------------------------------------
_add("NVIDIA B300 288GB SXM", "B300-SXM", "NVIDIA B300 SXM6 AC")
# B200 ships 192GB of physical HBM3e but exposes 180GB to software. Lambda
# quotes the usable figure, Hyperstack's docs the physical one: same card, so
# one canonical name. We use the number providers actually report.
_add("NVIDIA B200 180GB SXM", "B200-SXM", "B200 (180 GB SXM6)", "NVIDIA B200")
_add("NVIDIA GH200 96GB", "GH200 (96 GB)")
_add("NVIDIA H200 141GB SXM5", "H200-141G-SXM5", "NVIDIA H200")
_add("NVIDIA H100 80GB SXM5", "H100-80G-SXM5", "H100 (80 GB SXM5)", "NVIDIA H100 80GB HBM3")
_add("NVIDIA H100 80GB PCIe", "H100-80G-PCIe", "H100 (80 GB PCIe)", "NVIDIA H100 PCIe")
_add("NVIDIA H100 80GB PCIe NVLink", "H100-80G-PCIe-NVLink")
_add("NVIDIA A100 80GB SXM4", "A100-80G-SXM4", "A100 (80 GB SXM4)", "NVIDIA A100-SXM4-80GB")
_add("NVIDIA A100 80GB PCIe", "A100-80G-PCIe", "NVIDIA A100 80GB PCIe")
_add("NVIDIA A100 80GB PCIe NVLink", "A100-80G-PCIe-NVLink")
# Lambda sells the 40GB A100s. Same model number, half the memory of the 80GB
# cards above: deliberately separate names.
_add("NVIDIA A100 40GB PCIe", "A100 (40 GB PCIe)")
_add("NVIDIA A100 40GB SXM4", "A100 (40 GB SXM4)", "NVIDIA A100-SXM4-40GB")
_add("NVIDIA L40 48GB", "L40", "NVIDIA L40")
_add("NVIDIA A40 48GB", "A40", "NVIDIA A40")
_add("NVIDIA A10 24GB PCIe", "A10 (24 GB PCIe)")
_add("NVIDIA Tesla V100 16GB", "Tesla V100 (16 GB)")

# --------------------------------------------------------------------------
# NVIDIA workstation
# --------------------------------------------------------------------------
_add("NVIDIA RTX PRO 6000 96GB SE", "RTX-PRO6000-SE", "NVIDIA RTX PRO 6000 Blackwell Server Edition")
_add("NVIDIA RTX 6000 Ada 48GB", "RTX-A6000-ada", "NVIDIA RTX 6000 Ada Generation")
# The original Turing Quadro RTX 6000, NOT the Ada 48GB card above.
_add("NVIDIA Quadro RTX 6000 24GB", "RTX 6000 (24 GB)")
_add("NVIDIA RTX A6000 48GB", "RTX-A6000", "A6000 (48 GB)", "NVIDIA RTX A6000")
_add("NVIDIA RTX A5000 24GB", "RTX-A5000", "RTX A5000 (24 GB)", "NVIDIA RTX A5000")
_add("NVIDIA RTX A4000 16GB", "RTX-A4000", "NVIDIA RTX A4000")

# --------------------------------------------------------------------------
# NVIDIA consumer
# Both spellings of the same card map to one canonical name: that is what
# makes prices comparable across providers.
# --------------------------------------------------------------------------
_add("NVIDIA RTX 5090 32GB", "RTX-5090", "RTX 5090 (32 GB)", "NVIDIA GeForce RTX 5090")
_add("NVIDIA RTX 5090 Laptop 24GB", "RTX 5090 Laptop (24 GB)")
_add("NVIDIA RTX 5080 16GB", "RTX-5080", "RTX 5080 (16 GB)", "NVIDIA GeForce RTX 5080")
_add("NVIDIA RTX 5070 Ti 16GB", "RTX 5070 Ti (16 GB)")
_add("NVIDIA RTX 5070 12GB", "RTX 5070 (12 GB)")
_add("NVIDIA RTX 5060 Ti 16GB", "RTX 5060 Ti (16 GB)")
_add("NVIDIA RTX 5060 8GB", "RTX 5060 (8 GB)")

_add("NVIDIA RTX 4090 24GB", "RTX-4090", "RTX 4090 (24 GB)", "NVIDIA GeForce RTX 4090")
_add("NVIDIA RTX 4080 16GB", "RTX-4080", "RTX 4080 (16 GB)", "NVIDIA GeForce RTX 4080")
_add("NVIDIA RTX 4070 Ti SUPER 16GB", "RTX 4070 Ti Super (16 GB)")
_add("NVIDIA RTX 4070 Ti 12GB", "RTX 4070 Ti (12 GB)", "NVIDIA GeForce RTX 4070 Ti")
_add("NVIDIA RTX 4070 12GB", "RTX 4070 (12 GB)")
_add("NVIDIA RTX 4070 Laptop 8GB", "RTX 4070 Laptop (8 GB)")
_add("NVIDIA RTX 4060 Ti 16GB", "RTX 4060 Ti (16 GB)")
_add("NVIDIA RTX 4060 8GB", "RTX 4060 (8 GB)")

_add("NVIDIA RTX 3090 Ti 24GB", "RTX 3090 Ti (24 GB)", "NVIDIA GeForce RTX 3090 Ti")
_add("NVIDIA RTX 3090 24GB", "RTX-3090", "RTX 3090 (24 GB)", "NVIDIA GeForce RTX 3090")
_add("NVIDIA RTX 3080 Ti 12GB", "RTX 3080 Ti (12 GB)", "NVIDIA GeForce RTX 3080 Ti")
_add("NVIDIA RTX 3080 10GB", "RTX 3080 (10 GB)", "NVIDIA GeForce RTX 3080")
_add("NVIDIA RTX 3070 Ti 8GB", "RTX 3070 Ti (8 GB)")
_add("NVIDIA RTX 3070 8GB", "RTX 3070 (8 GB)", "NVIDIA GeForce RTX 3070")
_add("NVIDIA RTX 3060 Ti 8GB", "RTX 3060 Ti (8 GB)")
# Same model number, two memory sizes: deliberately distinct.
_add("NVIDIA RTX 3060 12GB", "RTX 3060 (12 GB)")
_add("NVIDIA RTX 3060 8GB", "RTX 3060 (8 GB)")
_add("NVIDIA RTX 3050 8GB", "RTX 3050 (8 GB)")

_add("NVIDIA RTX 2080 Ti 11GB", "RTX 2080 Ti (11 GB)")
_add("NVIDIA RTX 2080 8GB", "RTX 2080 (8 GB)")
_add("NVIDIA RTX 2070 8GB", "RTX 2070 (8 GB)")
_add("NVIDIA RTX 2060 6GB", "RTX 2060 (6 GB)")

_add("NVIDIA GTX 1660 SUPER 6GB", "GTX 1660 Super (6 GB)")
_add("NVIDIA GTX 1660 6GB", "GTX 1660 (6 GB)")
_add("NVIDIA GTX 1650 4GB", "GTX 1650 (4 GB)")
_add("NVIDIA GTX 1060 6GB", "GTX 1060 (6 GB)")
_add("NVIDIA GTX 1050 Ti 4GB", "GTX 1050 Ti (4 GB)")

# --------------------------------------------------------------------------
# RunPod-only names
# RunPod ids carry VRAM and variant, so most map straight onto names we
# already have; these are the ones nothing else sells yet.
# --------------------------------------------------------------------------
_add("NVIDIA H100 94GB NVL", "NVIDIA H100 NVL")
_add("NVIDIA H200 143GB NVL", "NVIDIA H200 NVL")
_add("NVIDIA L40S 48GB", "NVIDIA L40S")
_add("NVIDIA L4 24GB", "NVIDIA L4")
# Tesla V100 comes in two boards. Lambda reports "Tesla V100 (16 GB)" with no
# variant, so it deliberately keeps its own variant-less name rather than being
# guessed into one of these.
_add("NVIDIA Tesla V100 16GB PCIe", "Tesla V100-PCIE-16GB")
_add("NVIDIA Tesla V100 16GB SXM2", "Tesla V100-SXM2-16GB")

_add("NVIDIA RTX 2000 Ada 16GB", "NVIDIA RTX 2000 Ada Generation")
_add("NVIDIA RTX 4000 Ada 20GB", "NVIDIA RTX 4000 Ada Generation")
_add("NVIDIA RTX 4000 SFF Ada 20GB", "NVIDIA RTX 4000 SFF Ada Generation")
_add("NVIDIA RTX 5000 Ada 32GB", "NVIDIA RTX 5000 Ada Generation")
_add("NVIDIA RTX A2000 6GB", "NVIDIA RTX A2000")
_add("NVIDIA RTX A4500 20GB", "NVIDIA RTX A4500")

_add("NVIDIA RTX PRO 4000 Blackwell 24GB", "NVIDIA RTX PRO 4000 Blackwell")
_add("NVIDIA RTX PRO 4500 Blackwell 32GB", "NVIDIA RTX PRO 4500 Blackwell")
_add("NVIDIA RTX PRO 4500 Blackwell 32GB SE", "NVIDIA RTX PRO 4500 Blackwell Server Edition")
_add("NVIDIA RTX PRO 5000 Blackwell 48GB", "NVIDIA RTX PRO 5000 Blackwell")
_add("NVIDIA RTX PRO 6000 Blackwell 96GB Max-Q", "NVIDIA RTX PRO 6000 Blackwell Max-Q Workstation Edition")
_add("NVIDIA RTX PRO 6000 Blackwell 96GB Workstation", "NVIDIA RTX PRO 6000 Blackwell Workstation Edition")

_add("NVIDIA RTX 4080 SUPER 16GB", "NVIDIA GeForce RTX 4080 SUPER")

# MIG slices are partitions of one physical card, not cards. Kept distinct so
# a 34GB slice is never priced as if it were a whole B300.
_add("NVIDIA B300 MIG 1g.34gb", "NVIDIA B300 SXM6 AC MIG 1g.34gb")
_add("NVIDIA RTX PRO 6000 SE MIG 1g.24gb", "NVIDIA RTX PRO 6000 Blackwell Server Edition MIG 1g.24gb")
_add("NVIDIA RTX PRO 6000 SE MIG 2g.48gb", "NVIDIA RTX PRO 6000 Blackwell Server Edition MIG 2g.48gb")

# --------------------------------------------------------------------------
# AMD datacenter
# --------------------------------------------------------------------------
_add("AMD Instinct MI300X 192GB", "AMD Instinct MI300X OAM")
_add("AMD Instinct MI350 294GB", "AMD Instinct MI350 OAM")

# --------------------------------------------------------------------------
# AMD consumer
# --------------------------------------------------------------------------
_add("AMD RX 9070 XT 16GB", "AMD RX 9070 XT (16GB)")
_add("AMD RX 9060 XT 16GB", "AMD RX 9060 XT (16GB)")
_add("AMD RX 7900 XTX 24GB", "AMD RX 7900 XTX (24GB)")
_add("AMD RX 7900 XT 20GB", "AMD RX 7900 XT (20GB)")
_add("AMD RX 7800 XT 16GB", "AMD RX 7800 XT (16GB)")

# --------------------------------------------------------------------------
# Hyperbolic, Voltage Park, Vast, Nebius, Massed Compute, Lium
# Only names whose silicon and memory are unambiguous. A name that hides a
# variant (Massed's "H100 (80GB)" is PCIe or SXM, Vast's "A100 SXM4" is 40 or
# 80GB) stays unmapped and shows up in `unmapped`.
# --------------------------------------------------------------------------
_add("NVIDIA H100 80GB SXM5", "h100 sxm5", "h100-sxm5-80gb", "H100 SXM", "NVIDIA HGX H100", "H100 SXM5 (80GB)")
_add("NVIDIA H100 80GB PCIe", "H100 PCIE")
_add("NVIDIA H100 94GB NVL", "H100 NVL")
_add("NVIDIA H200 141GB SXM5", "h200 sxm5", "H200", "NVIDIA HGX H200")
_add("NVIDIA H200 143GB NVL", "H200 NVL", "H200 NVL (141GB)")
_add("NVIDIA B200 180GB SXM", "B200", "NVIDIA HGX B200", "B200 SXM6")
_add("NVIDIA B300 288GB SXM", "NVIDIA HGX B300", "B300 SXM6")
_add("NVIDIA A100 80GB SXM4", "A100 SXM4 (80GB)")
_add("NVIDIA L40S 48GB", "L40S", "L40S (48GB)", "NVIDIA L40S with Intel CPU", "NVIDIA L40S with AMD CPU")
_add("NVIDIA L40 48GB", "L40 (48GB)")
_add("NVIDIA A40 48GB", "A40 (48GB)")
_add("NVIDIA RTX A6000 48GB", "RTX A6000 (48GB)")
_add("NVIDIA RTX A5000 24GB", "RTX A5000 (24GB)")
_add("NVIDIA RTX 6000 Ada 48GB", "RTX 6000Ada", "RTX 6000 ADA (48GB)")
_add("NVIDIA RTX PRO 4500 Blackwell 32GB", "RTX PRO 4500 Blackwell (32GB)")
_add("NVIDIA RTX PRO 6000 Blackwell 96GB Workstation", "RTX PRO 6000 WS")
_add("NVIDIA RTX PRO 6000 96GB SE", "RTX PRO 6000 S")
_add("NVIDIA RTX 5090 32GB", "RTX 5090")
_add("NVIDIA RTX 5080 16GB", "RTX 5080")
_add("NVIDIA RTX 4090 24GB", "RTX 4090")
_add("NVIDIA RTX 3090 24GB", "RTX 3090")

# --------------------------------------------------------------------------
# DigitalOcean (its gpu_info.model ids) and AWS (names from its instance table)
# DigitalOcean's MI350X stays unmapped: the 288GB figure conflicts with the
# MI350 name above and nothing else sells it yet.
# --------------------------------------------------------------------------
_add("NVIDIA H100 80GB SXM5", "nvidia_h100")
_add("NVIDIA H200 141GB SXM5", "nvidia_h200")
_add("NVIDIA B300 288GB SXM", "nvidia_b300")
_add("NVIDIA L40S 48GB", "nvidia_l40s")
_add("NVIDIA RTX 6000 Ada 48GB", "nvidia_rtx6000_ada")
_add("NVIDIA RTX 4000 Ada 20GB", "nvidia_rtx4000_ada")
_add("AMD Instinct MI300X 192GB", "amd_mi300x")
_add("AMD Instinct MI325X 256GB", "amd_mi325x")
_add("AMD Instinct MI355X 288GB", "amd_mi355x")
_add("NVIDIA T4 16GB", "NVIDIA T4")
_add("NVIDIA A10G 24GB", "NVIDIA A10G")

# --------------------------------------------------------------------------
# Verda, and Crusoe / Latitude / Denvr as listed by Shadeform.
# Shadeform names are "<gpu_type> <variant>" built in providers/shadeform.py.
# Left unmapped on purpose: every Verda confidential-computing ("CC") type, and
# Shadeform's "RTXPro6000 pcie": each hides a variant. Verda's "RTX PRO 6000 96GB"
# is NOT mapped here (Arkane, E2E print the same words for an unstated edition):
# providers/verda.py looks it up under the name Verda's product page gives it,
# "NVIDIA RTX PRO 6000 Blackwell Server Edition" (mapped above).
# Verda's B300 lists 268GB (usable) for the card others call 288GB, the same
# usable-versus-physical gap noted for the B200 above, so it shares a name.
# --------------------------------------------------------------------------
_add("NVIDIA GB300 288GB", "GB300 SXM6 288GB")
_add("NVIDIA B300 288GB SXM", "B300 SXM6 268GB")
_add("NVIDIA B200 180GB SXM", "B200 SXM6 180GB")
_add("NVIDIA H200 141GB SXM5", "H200 SXM5 141GB")
_add("NVIDIA H100 80GB SXM5", "H100 SXM5 80GB", "H100 sxm")
_add("NVIDIA A100 80GB SXM4", "A100 SXM4 80GB", "A100_80G sxm", "A100_80G sxm4")
_add("NVIDIA A100 40GB SXM4", "A100 SXM4 40GB", "A100 sxm")
_add("NVIDIA Tesla V100 16GB", "Tesla V100 16GB")
_add("NVIDIA L40S 48GB", "L40S 48GB", "L40S pcie")
_add("NVIDIA RTX 6000 Ada 48GB", "RTX 6000 Ada 48GB")
_add("NVIDIA RTX A6000 48GB", "RTX A6000 48GB")
_add("NVIDIA A100 80GB PCIe", "A100_80G pcie")
_add("NVIDIA A100 80GB PCIe NVLink", "A100_80G pcie nvlink")
_add("NVIDIA A100 40GB PCIe", "A100 pcie")
_add("NVIDIA A40 48GB", "A40 pcie")
_add("NVIDIA H100 80GB PCIe", "H100 pcie")
_add("NVIDIA H100 80GB PCIe NVLink", "H100 pcie nvlink")
_add("NVIDIA GH200 96GB", "GH200 pcie")
_add("Intel Gaudi 2 96GB", "GAUDI2 pcie")
# Paperspace via Shadeform. The RTX A4000/A5000/A6000 exist only as PCIe cards, so the tag cannot mislead.
# Left unmapped: "V100 pcie" (Paperspace's page says only "V100 16 GB"; Shadeform's interconnect tag is wrong
# on its H100), "V100_32G pcie" (no canonical 32 GB V100 yet) and the H100, relabelled in providers/shadeform.py
# because Paperspace's page says HGX (SXM) where Shadeform says pcie. "A100 pcie nvlink" (Paperspace's A100x8, 40 GB
# PCIe cards bridged with NVLink) has no canonical NVLink 40 GB variant yet.
_add("NVIDIA RTX A4000 16GB", "A4000 pcie")
_add("NVIDIA RTX A5000 24GB", "A5000 pcie")
_add("NVIDIA RTX A6000 48GB", "A6000 pcie")
_add(None, "A100 pcie nvlink", "V100 pcie", "V100_32G pcie", "H100 (Paperspace: HGX per its page, pcie per Shadeform)")
# Crusoe's own type catalogue (GET /projects/{id}/compute/vms/types, `gpu_type`), read for capacity only.
# Each names model, memory and form factor; H100 SXM is SXM5 only and A100 SXM SXM4 only.
_add("NVIDIA H100 80GB SXM5", "H100-SXM-80GB")
_add("NVIDIA A100 80GB SXM4", "A100-SXM-80GB")
_add("NVIDIA A100 80GB PCIe", "A100-PCIe-80GB")
_add("NVIDIA A100 40GB PCIe", "A100-PCIe-40GB")
_add("NVIDIA A40 48GB", "A40-PCIe-48GB")
_add("NVIDIA L40S 48GB", "L40S-48GB")
_add("AMD Instinct MI300X 192GB", "MI300X-192GB")
_add("AMD Instinct MI355X 288GB", "MI355X_288GB")
# Boost Run as listed by Shadeform: its H200/B200 types are named "..._sxm5x8"/"..._sxm6x8".
_add("NVIDIA H200 141GB SXM5", "H200 sxm")
_add("NVIDIA B200 180GB SXM", "B200 sxm")
# Phyntec (and Boost Run) as listed by Shadeform: "{gpu_type} {interconnect}". The
# GeForce cards have one variant each; Shadeform states 24GB / 32GB per GPU.
# Horizon's "H100 sxm5" / "H200 sxm5" are already mapped above.
_add("NVIDIA RTX 4090 24GB", "RTX4090 pcie")
_add("NVIDIA RTX 5090 32GB", "RTX5090 pcie")

# --------------------------------------------------------------------------
# CoreWeave, Jarvislabs, Hot Aisle (public price pages). The normalizers append
# the page's printed per-GPU VRAM, so a name here carries its memory figure.
# "HGX" is NVIDIA's SXM baseboard, so HGX H100/H200/B200/B300 are the SXM parts.
# CoreWeave's B300 prints 270GB (usable) for the 288GB card, as Verda's 268GB does.
# CoreWeave's "(High|Standard) Memory" RTX PRO 6000 rows differ in system RAM only;
# the suffix is stripped and the card is the Server Edition the name says.
# Left unmapped on purpose: "NVIDIA A100 80GB" (CoreWeave and Jarvislabs: SXM4 or
# PCIe not stated), "NVIDIA A100 40GB", "NVIDIA RTX Pro 6000 Blackwell 96GB"
# (Jarvislabs: Workstation, Max-Q or Server Edition not stated), "NVIDIA A30 24GB"
# (no canonical A30 yet) and "NVIDIA GB200 NVL72 186GB" (no canonical GB200 yet).
# --------------------------------------------------------------------------
_add("NVIDIA H100 80GB SXM5", "NVIDIA HGX H100 80GB", "NVIDIA H100 SXM 80GB")
_add("NVIDIA H200 141GB SXM5", "NVIDIA HGX H200 141GB", "NVIDIA H200 SXM 141GB")
_add("NVIDIA B200 180GB SXM", "NVIDIA HGX B200 180GB")
_add("NVIDIA B300 288GB SXM", "NVIDIA HGX B300 270GB")
_add("NVIDIA L40S 48GB", "NVIDIA L40S 48GB")
_add("NVIDIA L40 48GB", "NVIDIA L40 48GB")
_add("NVIDIA GH200 96GB", "NVIDIA GH200 96GB")
_add("NVIDIA RTX PRO 6000 96GB SE", "NVIDIA RTX PRO 6000 Blackwell Server Edition 96GB")
_add("NVIDIA L4 24GB", "NVIDIA L4 24GB")
_add("AMD Instinct MI300X 192GB", "AMD MI300X")

# --------------------------------------------------------------------------
# Google Cloud: Compute Engine accelerator-type ids, from the hand table in
# providers/gcp.py (the price page prints only "Nvidia H100", "NVIDIA L4", ...).
# A3 High and A3 Mega carry the same H100 SXM card (Mega adds network
# bandwidth), A3 Ultra the H200 SXM; A2 machines are HGX A100 boards (SXM4,
# NVSwitch; A2 Standard 40GB, A2 Ultra 80GB); G4 is the RTX PRO 6000 Server
# Edition. The V100 is printed as "16 GB HBM2" only, so it takes the
# variant-less V100 name (as Lambda's does).
# Left unmapped on purpose: nvidia-tesla-p4 and nvidia-tesla-p100 (no canonical
# P4 / P100 yet).
# --------------------------------------------------------------------------
_add("NVIDIA H100 80GB SXM5", "nvidia-h100-80gb", "nvidia-h100-mega-80gb")
_add("NVIDIA H200 141GB SXM5", "nvidia-h200-141gb")
_add("NVIDIA B200 180GB SXM", "nvidia-b200")
_add("NVIDIA A100 80GB SXM4", "nvidia-a100-80gb")
_add("NVIDIA A100 40GB SXM4", "nvidia-tesla-a100")
_add("NVIDIA L4 24GB", "nvidia-l4")
_add("NVIDIA RTX PRO 6000 96GB SE", "nvidia-rtx-pro-6000")
_add("NVIDIA T4 16GB", "nvidia-tesla-t4")
_add("NVIDIA Tesla V100 16GB", "nvidia-tesla-v100")

# --------------------------------------------------------------------------
# Azure. The Retail Prices API names no GPU; providers/azure.py GPU_SIZES carries
# the accelerator string of each size page on learn.microsoft.com, verbatim.
# NCv3's "Tesla V100 (16GB)" names no board, so it takes the variant-less V100.
# The only 94GB PCIe H100 is the H100 NVL. A10 and MI300X come in one form factor.
# The ND flagships (ND96asr_v4, ND96amsr_A100_v4, ND96isr_H100_v5, ND96isr_H200_v5)
# are only used for 8-GPU NVLink/NVSwitch sizes, i.e. NVIDIA HGX baseboards, which
# carry SXM parts: mapped the way AWS p4d/p5 and GCP A2/A3 are. These names are
# used by no other Azure size (the PCIe parts are named "PCIe" above).
# Left unmapped on purpose: "Nvidia Tesla V100 GPU (32GB)" (no canonical V100 32GB yet).
# --------------------------------------------------------------------------
_add("NVIDIA T4 16GB", "Nvidia Tesla T4 GPU (16GB)")
_add("NVIDIA Tesla V100 16GB", "Nvidia Tesla V100 GPU (16GB)")
_add("NVIDIA A100 80GB PCIe", "Nvidia PCIe A100 GPU (80GB)")
_add("NVIDIA H100 94GB NVL", "Nvidia PCIe H100 GPU (94GB)")
_add("NVIDIA A10 24GB PCIe", "Nvidia A10 GPU (24GB)")
_add("NVIDIA RTX PRO 6000 96GB SE", "Nvidia RTX PRO 6000 Blackwell Server Edition GPU (96 GB)")
_add("AMD Instinct MI300X 192GB", "AMD Instinct MI300X GPU (192GB)")
_add("NVIDIA A100 40GB SXM4", "Nvidia A100 GPU (40GB)")
_add("NVIDIA A100 80GB SXM4", "Nvidia A100 GPU (80GB)")
_add("NVIDIA H100 80GB SXM5", "Nvidia H100 GPU (80GB)")
_add("NVIDIA H200 141GB SXM5", "Nvidia H200 GPU (141GB)")

# --------------------------------------------------------------------------
# Akamai/Linode (GPU token from the plan label), OVHcloud ("<model> <VRAM>GB"
# from its catalogue), Scaleway as listed by Shadeform, Koyeb (gpu.name ids).
# Linode's "RTX4000 Ada" is read like DigitalOcean's and RunPod's unqualified
# RTX 4000 Ada (not the SFF card). Koyeb names append the per-GPU VRAM.
# Left unmapped on purpose: Linode "RTX6000" (Quadro RTX 6000 per Linode's docs,
# but the API name alone does not say Quadro or Ada); OVHcloud "H100 80GB" and
# "A100 80GB" (SXM or PCIe not stated), "H200 141GB" (SXM or NVL not stated),
# "Tesla V100S 32GB" (no canonical V100S yet); Koyeb "gpu-nvidia-h100 80GB",
# "gpu-nvidia-a100 80GB", "gpu-nvidia-h200 141GB" (variant not stated) and
# "gpu-nvidia-rtx-pro-6000 96GB" (Workstation, Max-Q or Server Edition not stated).
# --------------------------------------------------------------------------
_add("NVIDIA RTX 4000 Ada 20GB", "RTX4000 Ada")
_add("NVIDIA L4 24GB", "L4 24GB", "L4 pcie", "gpu-nvidia-l4 24GB")
_add("NVIDIA B300 288GB SXM", "B300 sxm")
# Scaleway's own catalogue (providers/scaleway.py): hardware.gpu.type + the VRAM in the offer id. Same products as
# the Shadeform names above ("H100 pcie", "L4 pcie", "L40S pcie", "B300 sxm"), so the canonical names continue.
# Scaleway's H100-SXM types are HGX H100 (SXM5). RENDER-S's "P100" stays unmapped (no canonical P100 yet).
_add("NVIDIA H100 80GB PCIe", "H100-PCIe 80GB")
_add("NVIDIA H100 80GB SXM5", "H100-SXM 80GB")
_add("NVIDIA B300 288GB SXM", "B300-SXM 288GB")
_add("NVIDIA L40S 48GB", "gpu-nvidia-l40s 48GB")
_add("NVIDIA RTX A6000 48GB", "gpu-nvidia-rtx-a6000 48GB")
_add("NVIDIA RTX 4000 SFF Ada 20GB", "gpu-nvidia-rtx-4000-sff-ada 20GB")
_add("NVIDIA A100 80GB SXM4", "gpu-nvidia-a100-sxm 80GB")

# --------------------------------------------------------------------------
# Oracle Cloud (providers/oracle.py SHAPES): Oracle's pricing-page wording plus
# its printed per-GPU VRAM. B200 and B300 exist only as SXM boards; B300 263GB and
# GB300 279GB are usable figures for the 288GB parts (as CoreWeave's 270GB). The
# bare "H200" is NVIDIA's SXM product; the PCIe card is always named "H200 NVL".
# Left unmapped on purpose: "NVIDIA H100 80GB", "NVIDIA A100 80GB", "NVIDIA A100
# 40GB" (Oracle never says SXM or PCIe; NVIDIA's blog calls OCI's H100 shape HGX,
# but the source we read does not), "NVIDIA RTX PRO 6000 96GB" (edition not
# stated) and "NVIDIA GB200 189GB (NVL72)" (no canonical GB200 yet).
# --------------------------------------------------------------------------
_add("NVIDIA H200 141GB SXM5", "NVIDIA H200 141GB")
_add("NVIDIA B200 180GB SXM", "NVIDIA B200 180GB")
_add("NVIDIA B300 288GB SXM", "NVIDIA B300 263GB")
_add("NVIDIA GB300 288GB", "NVIDIA GB300 279GB (NVL72)")
_add("AMD Instinct MI300X 192GB", "AMD MI300X 192GB")
_add("AMD Instinct MI355X 288GB", "AMD MI355X 288GB")
_add("NVIDIA A10 24GB PCIe", "NVIDIA A10 24GB")
_add("NVIDIA Tesla V100 16GB", "NVIDIA Tesla V100 16GB")

# IBM Cloud (providers/ibm.py PROFILES): IBM's VPC GPU profile docs say SXM5 for
# the H100/H200 profiles and "A100 PCIe (80 GB)" for the a100p ones (already
# mapped above). "Intel Gaudi 3 128GB" stays unmapped: no canonical Gaudi 3 yet.
_add("NVIDIA H100 80GB SXM5", "NVIDIA H100 SXM5 80GB")
_add("NVIDIA H200 141GB SXM5", "NVIDIA H200 SXM5 141GB")

# Archived price pages (backfill/parsers, methodology/archived-history.md): names older designs of provider pages
# printed. Only names whose card is unambiguous: "HGX" boards are SXM; A5000 / A6000 / RTX 6000 Ada exist only as
# those RTX workstation cards; "SXM" / "PCIe" stated. Still unmapped on purpose: bare "A100 80GB" / "H100 80GB"
# (form factor not stated), "NVIDIA HGX H100 Inference" (Together, one capture: what it is is not stated).
_add("NVIDIA H100 80GB SXM5", "NVIDIA HGX H100 SXM")                          # Together, Oct 2025 - Mar 2026
_add("NVIDIA L40S 48GB", "NVIDIA L40S GPU with AMD", "NVIDIA L40S GPU with Intel")  # Nebius, Nov 2025 - Mar 2026
_add("NVIDIA RTX A5000 24GB", "NVIDIA A5000 24GB")                            # Jarvislabs, Dec 2025 - Apr 2026
_add("NVIDIA RTX A6000 48GB", "NVIDIA A6000 48GB")                            # Jarvislabs, Hyperstack
_add("NVIDIA RTX 6000 Ada 48GB", "NVIDIA RTX6000 Ada 48GB", "NVIDIA RTX 6000 Ada Generation 48GB")
_add("NVIDIA RTX 4000 Ada 20GB", "NVIDIA RTX 4000 Ada Generation 20GB")       # DigitalOcean
_add("NVIDIA A100 80GB SXM4", "NVIDIA A100 SXM 80GB", "NVIDIA A100 80GB SXM", "NVIDIA HGX A100 80GB")
_add("NVIDIA A100 40GB SXM4", "NVIDIA A100 SXM 40GB")                         # Denvr
_add("NVIDIA A100 80GB PCIe", "NVIDIA PCIe A100 80GB")                        # Vultr
_add("NVIDIA A100 40GB PCIe", "NVIDIA A100 40GB PCIe")                        # Crusoe
_add("NVIDIA A40 48GB", "NVIDIA A40 48GB")                                    # Crusoe
_add("NVIDIA RTX PRO 6000 96GB SE", "NVIDIA RTX Pro 6000 SE 96GB")            # Hyperstack
_add("AMD Instinct MI300X 192GB", "AMD Instinct MI300X 192GB")                # DigitalOcean

# Nosana (providers/nosana.py) market names. Only names with a single product
# behind them are mapped. Unmapped: "NVIDIA H100", "NVIDIA A100 80GB/40GB" (no
# SXM or PCIe), "NVIDIA 3060" (8 or 12GB; the market's own vram says 8GB),
# "NVIDIA 4060" / "4070" / "4080" / "3090" (a Ti or SUPER host could sit in the
# same market), "4000/A4000" and "5000/A5000" (two cards), "Pro 6000 (SOC2)" and
# "Pro 6000 Server Edition (CC)" (a compliance / confidential-compute product).
_add("NVIDIA RTX 4090 24GB", "NVIDIA 4090")
_add("NVIDIA RTX 5090 32GB", "NVIDIA 5090")
_add("NVIDIA RTX A6000 48GB", "NVIDIA A6000")
_add("NVIDIA RTX 6000 Ada 48GB", "NVIDIA 6000 Ada")

# E2E Networks (providers/e2e.py): "<model> <vramGB>GB". The A40 and T4 come in
# one board each. "NVIDIA B200 180GB", "NVIDIA H200 141GB", "NVIDIA L40S 48GB"
# and "NVIDIA L4 24GB" are mapped above; the H100, A100, RTX PRO 6000, V100 32GB
# and A30 rows stay unmapped (variant not stated / no canonical name yet).
_add("NVIDIA A40 48GB", "NVIDIA A40 48GB")
_add("NVIDIA T4 16GB", "NVIDIA T4 16GB")
# SimplePod (providers/simplepod.py): "<gpuModel> (<VRAM> GB)". Its V100 name
# states the PCIe board; "Tesla V100-SXM2-32GB (32 GB)" has no canonical name yet.
_add("NVIDIA Tesla V100 16GB PCIe", "Tesla V100-PCIE-16GB (16 GB)")

# --------------------------------------------------------------------------
# Serverless: Modal (page names), Baseten ("NVIDIA <name> <VRAM>GB" from its
# specs), Beam ("<name> <interface> <VRAM>GB"), Replicate (page names; all its
# mappable names resolve above). Modal's GPU guide (modal.com/docs/guide/gpu)
# states every H100 and H200 on Modal is SXM and the H200 has 141 GB; the B300
# exists only as the SXM part. A10G is a single 24GB board.
# Left unmapped on purpose: Modal "Nvidia A100, 80 GB" / "Nvidia A100, 40 GB",
# Baseten "NVIDIA A100 80GB" / "NVIDIA H100 80GB", Replicate "Nvidia H100" /
# "Nvidia A100 (80GB)" (SXM or PCIe not stated); Modal "Nvidia RTX PRO 6000"
# (edition not stated); Modal "Nvidia A10" (A10 or AWS's A10G not stated).
# --------------------------------------------------------------------------
_add("NVIDIA B300 288GB SXM", "Nvidia B300")
_add("NVIDIA H200 141GB SXM5", "Nvidia H200 SXM")
_add("NVIDIA H100 80GB SXM5", "Nvidia H100 SXM5")
_add("NVIDIA A10G 24GB", "NVIDIA A10G 24GB")
_add("NVIDIA RTX 4090 24GB", "RTX 4090 PCIE 24GB")

# Arkane Cloud, VESSL AI and Mithril (public price pages). Arkane and VESSL names
# carry the printed per-GPU VRAM. B300 exists only as an SXM board (see Oracle
# above); the A100 SXM board is SXM4. Mithril's "H100 SXM5" / "H200 SXM5" are
# mapped above. Left unmapped on purpose: Arkane's "NVIDIA H100 80GB" and "NVIDIA
# A100 80GB" (SXM or PCIe not stated) and "RTX PRO 6000 96GB" (edition not stated).
_add("NVIDIA B300 288GB SXM", "NVIDIA B300 288GB")
_add("NVIDIA A100 80GB SXM4", "NVIDIA A100 SXM 80GB")

# Civo, Atlantic.Net, DeepInfra (public price pages). Civo's and DeepInfra's names
# ("NVIDIA H100 SXM 80GB", "NVIDIA L40S 48GB", "NVIDIA B200 180GB", "NVIDIA B300")
# are already mapped above. Atlantic.Net prints "H100NVL" for the card its page
# calls "NVIDIA H100 NVL", which exists only as the 94GB NVL board.
# Left unmapped on purpose: Civo "NVIDIA A100 40GB" / "NVIDIA A100 80GB" (SXM or
# PCIe not stated).
_add("NVIDIA H100 94GB NVL", "H100NVL")

# Clore.ai (keyed marketplace API). Names are the gpu_array model plus specs.gpuram,
# the per-GPU VRAM. Mapped only where the name and VRAM fix one product: GeForce
# cards at their standard memory, and boards whose name states the form factor or
# edition. Left unmapped on purpose: "H100 80GB" (SXM or PCIe not stated), "Tesla T4
# 15GB", "L4 23GB", "L40S 45GB", "A30 24GB", "A2 14GB" (no form factor in the name),
# "RTX 5060 Ti 8GB", "RTX 3080 12GB", "RTX 4070 SUPER 12GB" (no canonical variant
# yet), odd figures ("RTX 4090 23GB", "RTX 4060 Ti 7GB", "RTX 3090 20GB",
# "RTX 4090 D 48GB"), laptop parts other than the 5090, and CMP mining cards.
_add("NVIDIA RTX 5090 32GB", "RTX 5090 32GB")
_add("NVIDIA RTX 5090 Laptop 24GB", "RTX 5090 Laptop GPU 24GB")
_add("NVIDIA RTX 5080 16GB", "RTX 5080 16GB")
_add("NVIDIA RTX 5070 Ti 16GB", "RTX 5070 Ti 16GB")
_add("NVIDIA RTX 5070 12GB", "RTX 5070 12GB")
_add("NVIDIA RTX 5060 Ti 16GB", "RTX 5060 Ti 16GB")
_add("NVIDIA RTX 5060 8GB", "RTX 5060 8GB")
_add("NVIDIA RTX 4090 24GB", "RTX 4090 24GB")
_add("NVIDIA RTX 4080 SUPER 16GB", "RTX 4080 SUPER 16GB")
_add("NVIDIA RTX 4080 16GB", "RTX 4080 16GB")
_add("NVIDIA RTX 4070 Ti SUPER 16GB", "RTX 4070 Ti SUPER 16GB")
_add("NVIDIA RTX 4070 Ti 12GB", "RTX 4070 Ti 12GB")
_add("NVIDIA RTX 4070 12GB", "RTX 4070 12GB")
_add("NVIDIA RTX 3090 Ti 24GB", "RTX 3090 Ti 24GB")
_add("NVIDIA RTX 3090 24GB", "RTX 3090 24GB")
_add("NVIDIA RTX 3080 Ti 12GB", "RTX 3080 Ti 12GB")
_add("NVIDIA RTX 3080 10GB", "RTX 3080 10GB")
_add("NVIDIA RTX 3070 Ti 8GB", "RTX 3070 Ti 8GB")
_add("NVIDIA RTX 3070 8GB", "RTX 3070 8GB")
_add("NVIDIA RTX 3060 Ti 8GB", "RTX 3060 Ti 8GB")
_add("NVIDIA RTX 3060 12GB", "RTX 3060 12GB")
_add("NVIDIA RTX 3060 8GB", "RTX 3060 8GB")
_add("NVIDIA RTX 3050 8GB", "RTX 3050 8GB")
_add("NVIDIA RTX 2080 Ti 11GB", "RTX 2080 Ti 11GB")
_add("NVIDIA RTX 2080 8GB", "RTX 2080 8GB")
_add("NVIDIA RTX 2070 8GB", "RTX 2070 8GB")
_add("NVIDIA RTX 2060 6GB", "RTX 2060 6GB")
_add("NVIDIA GTX 1660 SUPER 6GB", "GTX 1660 SUPER 6GB")
_add("NVIDIA GTX 1650 4GB", "GTX 1650 4GB")
_add("NVIDIA GTX 1060 6GB", "GTX 1060 6GB 6GB")
_add("NVIDIA GTX 1050 Ti 4GB", "GTX 1050 Ti 4GB")
_add("NVIDIA RTX PRO 6000 Blackwell 96GB Workstation", "RTX PRO 6000 Blackwell Workstation Edition 96GB")
_add("NVIDIA RTX PRO 6000 Blackwell 96GB Max-Q", "RTX PRO 6000 Blackwell MaxQ Workstation Edition 96GB")
_add("NVIDIA RTX PRO 6000 96GB SE", "RTX PRO 6000 Blackwell Server Edition 96GB")
_add("NVIDIA RTX PRO 5000 Blackwell 48GB", "RTX PRO 5000 Blackwell 48GB")
_add("NVIDIA RTX PRO 4500 Blackwell 32GB", "RTX PRO 4500 Blackwell 32GB")
_add("NVIDIA RTX PRO 4000 Blackwell 24GB", "RTX PRO 4000 Blackwell 24GB")
_add("NVIDIA RTX A4000 16GB", "RTX A4000 16GB")
_add("NVIDIA RTX A2000 6GB", "RTX A2000 6GB")
_add("NVIDIA Quadro RTX 6000 24GB", "Quadro RTX 6000 24GB")
_add("NVIDIA A100 40GB SXM4", "A100SXM440GB 40GB")
_add("NVIDIA A100 80GB PCIe", "A100 80GB PCIe 80GB")
_add("NVIDIA Tesla V100 16GB SXM2", "Tesla V100SXM216GB 16GB")
_add("NVIDIA H100 94GB NVL", "H100 NVL 94GB")

# Akash Network (console-api /v1/gpu-prices): "<vendor> <model> <ram> <interface>", ram in GiB as Akash writes
# it. Mapped where model, memory and interface fix one product. Left unmapped: "rtx5000 16Gi" (Quadro RTX 5000,
# no canonical yet), "v100 32Gi" (no 32 GB V100 canonical), "gtx1050 2Gi", "m4000", "p4", "p40" (no canonical).
_add("NVIDIA A100 80GB SXM4", "nvidia a100 80Gi SXM4")
_add("NVIDIA A10G 24GB", "nvidia a10g 24Gi PCIe")
_add("NVIDIA B300 288GB SXM", "nvidia b300 288Gi SXM6")
_add("NVIDIA H100 80GB SXM5", "nvidia h100 80Gi SXM5")
_add("NVIDIA RTX PRO 6000 96GB SE", "nvidia pro6000se 96Gi PCIe")
_add("NVIDIA RTX PRO 6000 Blackwell 96GB Workstation", "nvidia pro6000we 96Gi PCIe")
_add("NVIDIA GTX 1050 Ti 4GB", "nvidia gtx1050ti 4Gi PCIe")
_add("NVIDIA RTX 3060 12GB", "nvidia rtx3060 12Gi PCIe")
_add("NVIDIA RTX 3060 Ti 8GB", "nvidia rtx3060ti 8Gi PCIe")
_add("NVIDIA RTX 3090 24GB", "nvidia rtx3090 24Gi PCIe")
_add("NVIDIA RTX 3090 Ti 24GB", "nvidia rtx3090ti 24Gi PCIe")
_add("NVIDIA RTX 4000 Ada 20GB", "nvidia rtx4000ada 20Gi PCIe")
_add("NVIDIA RTX 4000 SFF Ada 20GB", "nvidia rtx4000sada 20Gi PCIe")
_add("NVIDIA RTX 4080 16GB", "nvidia rtx4080 16Gi PCIe")
_add("NVIDIA RTX 4090 24GB", "nvidia rtx4090 24Gi PCIe")
_add("NVIDIA RTX 5090 32GB", "nvidia rtx5090 32Gi PCIe")
_add("NVIDIA T4 16GB", "nvidia t4 16Gi PCIe")
_add("NVIDIA Tesla V100 16GB SXM2", "nvidia v100 16Gi SXM2")
# CUDO Compute (rest.compute.cudo.org): gpuModelId. Its VM catalogue names nvidia-h100 "H100 SXM" (SXM5 80GB).
# Unmapped: "nvidia-a100-pcie" (40 or 80 GB not stated) and "nvidia-l40s-graphics" (an L40S in graphics
# mode, priced apart; kept apart rather than merged).
_add("NVIDIA H100 80GB SXM5", "nvidia-h100")
_add("NVIDIA H100 94GB NVL", "nvidia-h100-nvl-pcie")
_add("NVIDIA L40S 48GB", "nvidia-l40s")
_add(None, "nvidia-a100-pcie", "nvidia-l40s-graphics")
# GPU.ai (api.gpu.ai /v1/pricing): gpu_type slugs ("b200", "l40", "l40s" are already mapped above).
# Unmapped: "a100_80gb" (SXM or PCIe not stated), "rtx_pro_6000" and "rtx_pro_4500" (edition not stated).
_add("NVIDIA H100 80GB SXM5", "h100_sxm")
_add("NVIDIA H100 80GB PCIe", "h100_pcie")
_add("NVIDIA H200 141GB SXM5", "h200_sxm")
_add("NVIDIA B300 288GB SXM", "b300")
_add("NVIDIA L4 24GB", "l4")
_add("NVIDIA RTX 2000 Ada 16GB", "rtx_2000_ada")
_add("NVIDIA RTX 4000 Ada 20GB", "rtx_4000_ada")
_add("NVIDIA RTX 6000 Ada 48GB", "rtx_6000_ada")
_add("NVIDIA RTX A6000 48GB", "rtx_a6000")
_add("NVIDIA RTX A4000 16GB", "rtx_a4000")
_add("NVIDIA RTX 3090 24GB", "rtx_3090")
_add("NVIDIA RTX 4090 24GB", "rtx_4090")
_add("NVIDIA RTX 5090 32GB", "rtx_5090")
_add(None, "a100_80gb", "rtx_pro_6000", "rtx_pro_4500")
# Exoscale: "Exoscale <family> (<GPU from its pricing page>)". Unmapped: the A30 (no canonical yet) and the RTX PRO
# 6000 (the page says Blackwell 96GB, not which edition).
_add("NVIDIA Tesla V100 16GB", "Exoscale GPU2 (Tesla V100 16GB)")
_add("NVIDIA A40 48GB", "Exoscale GPU3 (NVIDIA A40 48GB)")
_add("NVIDIA RTX A5000 24GB", "Exoscale GPUA5000 (NVIDIA RTX A5000 24GB)")
_add("NVIDIA RTX 3080 Ti 12GB", "Exoscale GPU3080ti (NVIDIA RTX 3080 Ti 12GB)")
_add(None, "Exoscale GPUA30 (NVIDIA A30 24GB)", "Exoscale GPURTX6000PRO (NVIDIA RTX PRO 6000 Blackwell 96GB)")
# Hugging Face Inference Endpoints: "<cloud> <architecture> <GB per GPU>". The form factor follows from the cloud's
# instance family: AWS's only 80 GB A100 is p4de (SXM4); GCP's A100 80GB is a2-ultragpu (SXM4), its H100 a3 (SXM5);
# both clouds' H200 are SXM; the cloud RTX PRO 6000 is the Server Edition. Resold rows never vote (market.py).
_add("NVIDIA T4 16GB", "aws Nvidia T4 16GB", "gcp Nvidia T4 16GB")
_add("NVIDIA A10G 24GB", "aws Nvidia A10G 24GB")
_add("NVIDIA L4 24GB", "aws Nvidia L4 24GB", "gcp Nvidia L4 24GB")
_add("NVIDIA L40S 48GB", "aws Nvidia L40S 48GB")
_add("NVIDIA A100 80GB SXM4", "aws Nvidia A100 80GB", "gcp Nvidia A100 80GB")
_add("NVIDIA H100 80GB SXM5", "gcp Nvidia H100 80GB")
_add("NVIDIA H200 141GB SXM5", "aws Nvidia H200 141GB", "gcp Nvidia H200 141GB")
_add("NVIDIA RTX PRO 6000 96GB SE", "aws Nvidia RTX PRO 6000 Blackwell 96GB")
# Hydra Host inventory: nvidia-smi names, already mapped above, except Quadro RTX 8000 (no canonical yet).
_add(None, "Quadro RTX 8000")
_add(None, "nvidia rtx5000 16Gi PCIe", "nvidia v100 32Gi PCIe", "nvidia gtx1050 2Gi PCIe", "nvidia m4000 8Gi PCIe",
     "nvidia p4 8Gi PCIe", "nvidia p40 24Gi PCIe")

# Gcore (providers/gcore.py): bare-metal column model + per-GPU VRAM (the page prints SXM / PCIe). Its Virtual GPU
# rows say only "H100" and "80 GB": "H100 80GB" stays unmapped (SXM or PCIe not stated).
_add("NVIDIA H100 80GB SXM5", "H100 SXM 80GB")
_add("NVIDIA A100 80GB SXM4", "A100 SXM 80GB")
_add("NVIDIA L40S 48GB", "L40S PCIe 48GB")
_add("NVIDIA H200 141GB SXM5", "H200 SXM 141GB")
_add(None, "H100 80GB")

# --------------------------------------------------------------------------
# Seen, deliberately not mapped
# --------------------------------------------------------------------------
NOT_A_SINGLE_GPU = {
    # Salad sells one bundle covering three different cards, so no single
    # canonical name is truthful.
    "GTX 1070, 1080, 1080Ti (8 GB)": "bundle of three distinct GPUs",
    # A capability bundle, not a piece of hardware.
    "Stable Diffusion Compatible": "not a GPU model",
    # RunPod ships a placeholder row with zero price and zero VRAM.
    "unknown": "RunPod placeholder entry, not a GPU",
}
for _raw in NOT_A_SINGLE_GPU:
    _add(None, _raw)


def strip_market_suffix(raw_name: str) -> str:
    """Drop `-spot` and friends: those describe the market, not the silicon."""
    name = raw_name.strip()
    for suffix in _MARKET_SUFFIXES:
        if name.lower().endswith(suffix):
            return name[: -len(suffix)]
    return name


def canonical_gpu_name(raw_name: str) -> str | None:
    """Our name for a provider's GPU, or None if we have not mapped it yet."""
    return _RAW_TO_CANONICAL.get(_key(strip_market_suffix(raw_name)))


def is_known(raw_name: str) -> bool:
    """True when we have seen this name, even if we chose not to map it."""
    return _key(strip_market_suffix(raw_name)) in _RAW_TO_CANONICAL


def all_canonical_names() -> list[str]:
    return sorted({v for v in _RAW_TO_CANONICAL.values() if v})
