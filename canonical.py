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
# Left unmapped on purpose: Verda's "RTX PRO 6000 96GB" and every confidential-
# computing ("CC") type, and Shadeform's "RTXPro6000 pcie": each hides a variant.
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
