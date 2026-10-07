"""GPU families in the public API: a family lists its variants side by side, never one merged price.

    GET /v1/families              every tracked family (model families, then architectures)
    GET /v1/families/{family}     one family: each variant's current market + index + 24h change,
                                  and the cheapest variant now (a cross-variant fact, not a family price)

Also the shared response used when a {gpu} path parameter names a family instead of a
canonical GPU (/v1/markets/h100, /v1/history/h100, /v1/gpus/h100, /v1/markets/h100/context,
/v1/markets/h100/regions): HTTP 200 with meta.kind="family", meta.resolved_as="family" and
each variant's own links. See methodology/families.md.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

import families as fam
from accounts.auth import Principal, require_scope
from analytics import dispersion, indices
from api.common import OBSERVED_MARKET_PRICE, envelope, gpu_slug
from cache import ttl_cache

router = APIRouter()
READ = require_scope("data:read")

CHEAPEST_LABEL = ("cross-variant fact: the variant with the lowest current observed price; "
                  "not a family price (variants are different products)")


def links(gpu: str) -> dict:
    s = gpu_slug(gpu)
    return {"market": f"/v1/markets/{s}", "history": f"/v1/history/{s}", "gpu": f"/v1/gpus/{s}",
            "context": f"/v1/markets/{s}/context", "regions": f"/v1/markets/{s}/regions", "best": f"/v1/best/{s}"}


def _composite(fid: str) -> str | None:
    """The indices module's equal-weight composite over the same variants, if one is defined."""
    cid = f"{fam.family_slug(fid)}-class"
    return cid if cid in indices.REGISTRY else None


def _variant(gpu: str, m: dict | None, with_index: bool) -> dict:
    row = {"gpu": gpu, "slug": gpu_slug(gpu),
           "low": m and m["low"], "median": m and m["median"], "high": m and m["high"],
           "low_provider": m["by_provider"][0]["provider"] if m and m["by_provider"] else None,
           "providers": m["providers"] if m else 0,
           "available_listings": m["listings"]["available"] if m else 0,
           "priced_listings": m["listings"]["priced"] if m else 0,
           "reason": None if m and m["providers"] else "no live priced listing now",
           "links": links(gpu)}
    if with_index:
        iid = indices.gpu_index_id(gpu)
        lv = indices.index_level(iid) or {}
        ch = (lv.get("changes") or {}).get("24h")
        row["index_id"] = iid
        row["index_level"] = lv.get("level")
        row["index_published"] = bool(lv.get("published"))
        row["change_24h"] = ({"pct": ch.get("pct"), "reason": ch.get("reason"), "basis": "index level, 24h"}
                             if ch else {"pct": None, "reason": lv.get("reason") or "index has no data",
                                         "basis": "index level, 24h"})
    return row


def cheapest_variant(rows: list[dict]) -> dict | None:
    priced = [r for r in rows if r["low"] is not None]
    if not priced:
        return None
    r = min(priced, key=lambda r: (r["low"], r["gpu"]))
    return {"gpu": r["gpu"], "slug": r["slug"], "low": r["low"], "provider": r["low_provider"],
            "label": CHEAPEST_LABEL}


@ttl_cache(60, maxsize=128)
def family_payload(fid: str) -> dict:
    d = fam.family(fid)
    if d is None:
        raise HTTPException(404, f"unknown GPU family {fid!r}; see /v1/families")
    markets = dispersion.all_markets()
    rows = [_variant(g, markets.get(g), True) for g in d["variants"]]
    return {"id": d["id"], "slug": d["slug"], "name": d["name"], "kind": d["kind"],
            "architecture": d["architecture"], "member_families": d["member_families"], "note": fam.NOTE,
            "variants": rows, "variants_total": len(rows),
            "variants_priced": sum(1 for r in rows if r["low"] is not None),
            "cheapest_variant_now": cheapest_variant(rows), "composite_index": _composite(d["id"])}


def family_response(fid: str, *, requested: str, endpoint: str) -> dict:
    """The 200 answer when a {gpu} path parameter is a family: the variants, each with its own links."""
    data = family_payload(fid)
    return envelope(data, kind="family", resolved_as="family", requested=requested, endpoint=endpoint,
                    methodology=fam.METHODOLOGY, price_concept=OBSERVED_MARKET_PRICE,
                    note=f"{requested!r} is a GPU family, not one product. {fam.NOTE} "
                         f"Follow a variant's links.{endpoint} for its {endpoint} data.")


@router.get("/v1/families", tags=["families"], summary="GPU families and their canonical variants")
def families_list(_: Principal = Depends(READ)):
    markets = dispersion.all_markets()
    out = []
    for d in fam.all_families():
        rows = [_variant(g, markets.get(g), False) for g in d["variants"]]
        out.append({"id": d["id"], "slug": d["slug"], "name": d["name"], "kind": d["kind"],
                    "architecture": d["architecture"], "member_families": d["member_families"],
                    "variants": [{"gpu": r["gpu"], "slug": r["slug"], "low": r["low"], "providers": r["providers"]}
                                 for r in rows],
                    "variants_priced": sum(1 for r in rows if r["low"] is not None),
                    "cheapest_variant_now": cheapest_variant(rows), "link": f"/v1/families/{d['slug']}"})
    return envelope(out, kind="family", methodology=fam.METHODOLOGY, price_concept=OBSERVED_MARKET_PRICE,
                    note=fam.NOTE, count=len(out))


@router.get("/v1/families/{family}", tags=["families"], summary="One GPU family: every variant side by side")
def family_detail(family: str, _: Principal = Depends(READ)):
    fid = fam.resolve_family(family)
    if fid is None:
        raise HTTPException(404, f"unknown GPU family {family!r}; see /v1/families")
    return envelope(family_payload(fid), kind="family", methodology=fam.METHODOLOGY,
                    price_concept=OBSERVED_MARKET_PRICE, note=fam.NOTE)
