"""Conventions every /v1 endpoint follows.

Envelope
    {"data": ..., "meta": {"as_of": ..., "kind": ..., "methodology": ..., ...}}

Data kinds (never mixed in one number; see methodology/data-kinds.md)
    observed     read from a provider by OpenGrid (list price as normalized)
    inferred     derived by a stated rule from observed data (e.g. availability = capacity > 0)
    estimated    a model or assumption fills a gap (always labelled, never silent)
    transaction  what an OpenGrid execution actually quoted / paid

Price concepts (never mixed; see methodology/price-concepts.md)
    list_price             what the provider advertises
    observed_market_price  OpenGrid's normalized observation of that
    quote                  the price returned for one specific route request
    execution_price        the price actually paid / contracted

GPU slugs
    "NVIDIA H100 80GB SXM5" <-> "h100-80gb-sxm5". The slug drops the vendor
    prefix; `resolve_gpu` also accepts the full canonical name and loose case.
"""

import re
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

import canonical

OBSERVED, INFERRED, ESTIMATED, TRANSACTION = "observed", "inferred", "estimated", "transaction"
LIST_PRICE, OBSERVED_MARKET_PRICE, QUOTE, EXECUTION_PRICE = (
    "list_price", "observed_market_price", "quote", "execution_price",
)

DEFAULT_LIMIT, MAX_LIMIT = 100, 1000


def now() -> datetime:
    return datetime.now(timezone.utc)


def envelope(data: Any, *, kind: str | None = None, methodology: str | None = None, **meta) -> dict:
    """Wrap a payload. `methodology` names a doc under /methodology/<name>."""
    m = {"as_of": now().isoformat()}
    if kind:
        m["kind"] = kind
    if methodology:
        m["methodology"] = f"/methodology/{methodology}"
    m.update({k: v for k, v in meta.items() if v is not None})
    return {"data": data, "meta": m}


def page(items: list, limit: int, offset: int) -> tuple[list, dict]:
    """Slice an already-sorted list; returns (items, pagination meta)."""
    limit = max(1, min(limit, MAX_LIMIT))
    offset = max(0, offset)
    return items[offset: offset + limit], {"limit": limit, "offset": offset, "total": len(items)}


_VENDOR = re.compile(r"^(NVIDIA|AMD Instinct|AMD|Intel)\s+", re.I)


def gpu_slug(name: str) -> str:
    """'NVIDIA H100 80GB SXM5' -> 'h100-80gb-sxm5'. Stable: never change an existing slug."""
    short = _VENDOR.sub("", name)
    return re.sub(r"[^a-z0-9]+", "-", short.lower()).strip("-")


def provider_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def gpu_slugs() -> dict[str, str]:
    """slug -> canonical name, for every canonical name we know."""
    return {gpu_slug(n): n for n in canonical.all_canonical_names()}


def resolve_gpu(value: str, *, strict: bool = True) -> str | None:
    """A slug or canonical name -> canonical name. 404s when strict and unknown."""
    names = canonical.all_canonical_names()
    if value in names:
        return value
    low = value.strip().lower()
    for n in names:
        if n.lower() == low:
            return n
    hit = gpu_slugs().get(gpu_slug(value))
    if hit is None and strict:
        raise HTTPException(404, f"unknown GPU {value!r}; see /v1/gpus")
    return hit


def resolve_gpu_or_family(value: str) -> tuple[str, str]:
    """("gpu", canonical name) or ("family", family id); 404 when it is neither.

    A canonical GPU always wins: family slugs never collide with GPU slugs (tests/test_followup.py).
    A family is a set of distinct variants, never one market; see families.py.
    """
    hit = resolve_gpu(value, strict=False)
    if hit:
        return "gpu", hit
    import families  # lazy: families imports the news classifier

    fid = families.resolve_family(value)
    if fid:
        return "family", fid
    raise HTTPException(404, f"unknown GPU or GPU family {value!r}; see /v1/gpus and /v1/families")
