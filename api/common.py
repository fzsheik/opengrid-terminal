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
# Recovered from a public archive by OpenGrid's parsers, not observed live (methodology/archived-history.md).
ARCHIVED = "archived"
LIST_PRICE, OBSERVED_MARKET_PRICE, QUOTE, EXECUTION_PRICE = (
    "list_price", "observed_market_price", "quote", "execution_price",
)

DEFAULT_LIMIT, MAX_LIMIT = 100, 1000


def now() -> datetime:
    return datetime.now(timezone.utc)


def envelope(data: Any, *, kind: str | None = None, methodology: str | None = None, page: tuple | None = None,
             **meta) -> dict:
    """Wrap a payload. `methodology` names a doc under /methodology/<name>. page=(limit, cursor) pages a list
    body by cursor (meta: limit, next_cursor, total), the one paging convention of every Data API list."""
    if page is not None and isinstance(data, list):
        data, pg = cursor_page(data, *page)
        meta = {**meta, **pg}
    import observability
    from api import contract  # lazy: contract imports config, which some importers of common load later

    m = {"as_of": now().isoformat(), "request_id": observability.current().get("request_id"),
         "api_version": contract.current_version()}
    m = {k: v for k, v in m.items() if v is not None}
    if kind:
        m["kind"] = kind
    if methodology:
        m["methodology"] = f"/methodology/{methodology}"
    m.update({k: v for k, v in meta.items() if v is not None or k == "next_cursor"})  # null = last page
    if m.get("api_version"):  # a Data API request: public ids and clean numbers (api/contract.public_shape)
        data, m = contract.public_shape(data), contract.public_shape(m)
    return {"data": data, "meta": m}


def low_high(v):
    """Time-series bodies use the Data API's low / high (the internals say lowest / highest)."""
    if isinstance(v, dict):
        return {{"lowest": "low", "highest": "high"}.get(k, k): low_high(x) for k, x in v.items()}
    if isinstance(v, list):
        return [low_high(x) for x in v]
    return v


def cursor_page(items: list, limit: int, cursor: str | None) -> tuple[list, dict]:
    """Slice an already-sorted list by an opaque cursor; returns (items, meta with next_cursor and total).

    shortcut: the cursor encodes an offset, right for small, slowly changing lists (GPUs, providers, markets);
    time-ordered tables (history, events, news) need keyset cursors (last timestamp + id) instead."""
    offset = cursor_offset(cursor)
    limit = max(1, min(limit, MAX_LIMIT))
    return items[offset: offset + limit], {"limit": limit, "next_cursor": next_cursor(offset, limit, len(items)),
                                           "total": len(items)}


def cursor_offset(cursor: str | None) -> int:
    """An offset cursor -> its offset (0 without one); a cursor this API did not issue is 400 invalid_cursor."""
    import base64
    import json

    if not cursor:
        return 0
    try:
        offset = int(json.loads(base64.urlsafe_b64decode(cursor.encode() + b"==").decode())["o"])
        assert offset >= 0
        return offset
    except Exception:
        raise HTTPException(400, {"code": "invalid_cursor", "param": "cursor",
                                  "message": "cursor is not one this API returned; omit it to start over"})


def keyset_cursor(*values) -> str:
    """An opaque cursor holding the sort key of a page's last row (time-ordered feeds: stable while rows arrive)."""
    import base64
    import json

    return base64.urlsafe_b64encode(json.dumps([v.isoformat() if hasattr(v, "isoformat") else v
                                                for v in values]).encode()).decode().rstrip("=")


def keyset_values(cursor: str, n: int) -> list:
    """keyset_cursor's values (the first one, a timestamp, parsed back); a foreign cursor is 400 invalid_cursor."""
    import base64
    import json

    try:
        vals = json.loads(base64.urlsafe_b64decode(cursor.encode() + b"==").decode())
        assert isinstance(vals, list) and len(vals) == n
        return [datetime.fromisoformat(vals[0]), *vals[1:]]
    except Exception:
        raise HTTPException(400, {"code": "invalid_cursor", "param": "cursor",
                                  "message": "cursor is not one this API returned; omit it to start over"})


def next_cursor(offset: int, limit: int, total: int) -> str | None:
    import base64
    import json

    if offset + limit >= total:
        return None
    return base64.urlsafe_b64encode(json.dumps({"o": offset + limit}).encode()).decode().rstrip("=")


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
    import gpu_ids

    if low in gpu_ids.BY_ID:  # the public id (docs/API_CONTRACT.md section 6)
        return gpu_ids.BY_ID[low]
    for n in names:
        if n.lower() == low:
            return n
    hit = gpu_slugs().get(gpu_slug(value))  # older short slugs stay accepted as input aliases
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
