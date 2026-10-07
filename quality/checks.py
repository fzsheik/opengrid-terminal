"""The bad-data rules: pure functions over one new listing and the state it would replace.

Nothing here touches the database; quality/quarantine.py decides what a finding does.
Every threshold is a named constant so methodology/data-quality.md can quote it.

A finding's action is either
    hold   the new value is kept out of compute_listings / listing_observations until it
           is resolved (accepted, auto-accepted after it persists, or rejected)
    flag   recorded as an incident and lowers trust, but nothing is held: there is no
           "last good value" to fall back to (the inconsistency is static), so holding
           would only blank the listing

Rules compare against the APPLIED state (compute_listings), not the provider's last
raw reading, so a held value keeps being compared with the last good one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

# Price jumps: the "$2.50 -> $250" unit error, or a cents/dollars swap.
JUMP_UP = 5.0      # new / previous >= 5x
JUMP_DOWN = 0.2    # new / previous <= 0.2x
# Absolute plausibility of a per-GPU hourly price (USD).
CEILING_PER_GPU = 60.0  # the dearest real listing seen is ~$18/GPU-h (AWS B300)
FLOORS = (  # datacenter parts nobody rents below these per GPU-hour
    (re.compile(r"\b(H100|H200|B200|B300|GB200|GB300|GH200|MI300X|MI325X|MI355X)\b"), 0.50),
    (re.compile(r"\bA100\b"), 0.20),
)
# Supply: a capacity reading 10x the previous and at least +50 units.
CAPACITY_SPIKE_X = 10
CAPACITY_SPIKE_MIN = 50
# Mapping: canonical VRAM vs a VRAM figure in the raw name, relative difference.
VRAM_TOLERANCE = 0.10  # 268GB (Verda's B300) vs 288GB canonical is a known 7% reporting gap
# Instance price vs per-GPU price x gpu_count.
INSTANCE_MISMATCH = 0.10
# Provider listing counts against the median of its recent polls.
EXPLOSION_X = 3.0
EXPLOSION_MIN = 20
COLLAPSE_X = 0.2
COLLAPSE_MIN_NORM = 10
EMPTY_MIN_PREVIOUS = 5
NORM_POLLS = 10     # how many recent polls make the norm
NORM_MIN_POLLS = 3  # fewer than this and there is no norm yet

PRICE, GPU_COUNT, REGION, CAPACITY, MAPPING, COUNT = (
    "price_per_gpu_hour", "gpu_count", "region", "capacity", "canonical_gpu_name", "listing_count",
)

# rule -> (field, action, auto-acceptable when it persists, severity)
RULES = {
    "price_nonpositive":       (PRICE, "hold", False, "major"),
    "price_jump":              (PRICE, "hold", True, "major"),  # auto only if the new value is plausible
    "price_below_floor":       (PRICE, "hold", False, "major"),
    "price_above_ceiling":     (PRICE, "hold", False, "major"),
    "gpu_count_invalid":       (GPU_COUNT, "hold", False, "major"),
    "gpu_count_changed":       (GPU_COUNT, "hold", True, "notable"),
    "region_disappeared":      (REGION, "hold", True, "notable"),
    "capacity_spike":          (CAPACITY, "hold", True, "notable"),
    "vram_conflict":           (MAPPING, "hold", False, "major"),
    "instance_price_mismatch": (PRICE, "flag", False, "notable"),
    "duplicate_explosion":     (COUNT, "hold", True, "major"),
    "listing_collapse":        (COUNT, "flag", False, "notable"),
    "empty_response":          (COUNT, "flag", False, "major"),
}


@dataclass
class Finding:
    rule: str
    field: str
    action: str
    previous: object
    new: object
    auto_acceptable: bool
    detail: dict = field(default_factory=dict)


def _f(v) -> float | None:
    return None if v is None else float(v)


def _finding(rule: str, previous, new, auto: bool | None = None, **detail) -> Finding:
    fld, action, default_auto, _ = RULES[rule]
    return Finding(rule, fld, action, previous, new, default_auto if auto is None else auto, detail)


def floor_for(canonical: str | None) -> float | None:
    if not canonical:
        return None
    for pattern, floor in FLOORS:
        if pattern.search(canonical):
            return floor
    return None


def plausible_price(price: float, canonical: str | None) -> bool:
    floor = floor_for(canonical)
    return 0 < price <= CEILING_PER_GPU and (floor is None or price >= floor)


_RAW_VRAM = re.compile(r"(?<![A-Za-z0-9.])(\d{2,3})\s?G(?:B|iB)?(?![A-Za-z0-9])", re.I)
_CANON_VRAM = re.compile(r"(\d+)GB\b")


def vram_conflict(raw_name: str, canonical: str | None) -> tuple[int, int] | None:
    """(raw VRAM, canonical VRAM) when both are stated and disagree by more than VRAM_TOLERANCE.

    Only the raw name is available (ComputeListing carries no VRAM field); a raw name
    with two different VRAM figures is ambiguous and never flagged.
    """
    if not canonical or not raw_name:
        return None
    raw = {int(m) for m in _RAW_VRAM.findall(raw_name)}
    canon = _CANON_VRAM.search(canonical)
    if len(raw) != 1 or not canon:
        return None
    r, c = raw.pop(), int(canon.group(1))
    if c and abs(r - c) / c > VRAM_TOLERANCE:
        return r, c
    return None


def same_value(a, b) -> bool:
    """Equal, with 1% slack for numbers so a marketplace median's wobble is "the same" value."""
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, (int, float, Decimal)) and isinstance(b, (int, float, Decimal)):
        a, b = float(a), float(b)
        return a == b or (max(abs(a), abs(b)) > 0 and abs(a - b) / max(abs(a), abs(b)) <= 0.01)
    return a == b


def check_listing(listing, prev: dict | None) -> list[Finding]:
    """Findings for one new listing against its applied state (None when it is new).

    Price rules run only when the price differs from the applied one (or the listing is
    new): an unchanged value has nothing new to hold, and holding it would be a no-op.
    """
    out: list[Finding] = []
    new_p, new_i = _f(listing.price_per_gpu_hour), _f(listing.price_per_instance_hour)
    old_p = _f(prev.get("price_per_gpu_hour")) if prev else None
    price_changed = prev is None or not same_value(new_p, old_p) or not same_value(new_i, _f(prev.get("price_per_instance_hour")))

    if price_changed:
        nonpositive = (new_p is not None and new_p <= 0) or (new_i is not None and new_i <= 0)
        if nonpositive:
            out.append(_finding("price_nonpositive", old_p, new_p, instance=new_i))
        elif new_p is not None:
            if old_p and old_p > 0 and (new_p / old_p >= JUMP_UP or new_p / old_p <= JUMP_DOWN):
                out.append(_finding("price_jump", old_p, new_p,
                                    auto=plausible_price(new_p, listing.canonical_gpu_name),
                                    ratio=round(new_p / old_p, 4), instance=new_i))
            floor = floor_for(listing.canonical_gpu_name)
            if floor is not None and new_p < floor:
                out.append(_finding("price_below_floor", old_p, new_p, floor=floor, instance=new_i))
            if new_p > CEILING_PER_GPU:
                out.append(_finding("price_above_ceiling", old_p, new_p, ceiling=CEILING_PER_GPU, instance=new_i))

    if listing.gpu_count is None or listing.gpu_count <= 0:
        out.append(_finding("gpu_count_invalid", prev and prev.get("gpu_count"), listing.gpu_count))
    elif prev and prev.get("gpu_count") and prev["gpu_count"] != listing.gpu_count:
        out.append(_finding("gpu_count_changed", prev["gpu_count"], listing.gpu_count))

    if prev and prev.get("region") and not listing.region:
        out.append(_finding("region_disappeared", prev["region"], None, country=prev.get("country")))

    old_c = prev.get("capacity") if prev else None
    if old_c and old_c >= 1 and listing.capacity is not None and listing.capacity != old_c:
        if listing.capacity >= CAPACITY_SPIKE_X * old_c and listing.capacity - old_c >= CAPACITY_SPIKE_MIN:
            out.append(_finding("capacity_spike", old_c, listing.capacity))

    conflict = vram_conflict(listing.raw_gpu_name, listing.canonical_gpu_name)
    if conflict:
        out.append(_finding("vram_conflict", None, listing.canonical_gpu_name,
                            raw_vram_gb=conflict[0], canonical_vram_gb=conflict[1], raw_gpu_name=listing.raw_gpu_name))

    if new_p and new_i and listing.gpu_count and listing.gpu_count > 0:
        expected = new_p * listing.gpu_count
        if expected > 0 and abs(new_i / expected - 1) > INSTANCE_MISMATCH:
            out.append(_finding("instance_price_mismatch", None, new_i,
                                expected=round(expected, 6), gpu_count=listing.gpu_count, per_gpu=new_p))
    return out


def norm_of(counts: list[int]) -> float | None:
    """Median of recent poll counts; None until there are NORM_MIN_POLLS of them."""
    if len(counts) < NORM_MIN_POLLS:
        return None
    s = sorted(counts)
    n = len(s)
    return float(s[n // 2]) if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def check_count(count: int, norm: float | None, previous_count: int | None) -> list[Finding]:
    """Provider-level findings on how many listings one poll produced."""
    out = []
    if count == 0:
        base = max(norm or 0, previous_count or 0)
        if base >= EMPTY_MIN_PREVIOUS:
            out.append(_finding("empty_response", base, 0))
        return out
    if norm is None:
        return out
    if count > EXPLOSION_X * norm and count - norm >= EXPLOSION_MIN:
        out.append(_finding("duplicate_explosion", norm, count, ratio=round(count / norm, 2)))
    elif norm >= COLLAPSE_MIN_NORM and count < COLLAPSE_X * norm:
        out.append(_finding("listing_collapse", norm, count))
    return out
