"""GPU families in the public API: a family lists its variants side by side, never one merged price.

    GET /v1/families              every tracked family (model families, then architectures)
    GET /v1/families/{family}     one family: each variant's current market + index + 24h change,
                                  and the cheapest variant now (a cross-variant fact, not a family price)

A family's public id is its slug (h100, hopper). Variants carry the same price block as /v1/markets.
family_response is still used by /v1/history/{gpu} for a family slug (until the history routes move).
See methodology/families.md.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

import families as fam
from accounts.auth import Principal, require_scope
from analytics import dispersion, indices
import gpu_ids
from api.common import OBSERVED_MARKET_PRICE, envelope
from cache import ttl_cache

router = APIRouter()
READ = require_scope("data:read")

CHEAPEST_LABEL = ("cross-variant fact: the variant with the lowest current observed price; "
                  "not a family price (variants are different products)")


def links(gpu: str) -> dict:
    s = gpu_ids.gpu_id(gpu)
    return {"gpu": f"/v1/gpus/{s}", "market": f"/v1/markets/{s}", "regions": f"/v1/markets/{s}/regions",
            "history": f"/v1/history/{s}"}


def _composite(fid: str) -> str | None:
    """The indices module's equal-weight composite over the same variants, if one is defined."""
    cid = f"{fam.family_slug(fid)}-class"
    return cid if cid in indices.REGISTRY else None


def _variant(gpu: str, m: dict | None, with_index: bool) -> dict:
    from api.structure import price_block  # lazy: structure imports this module's siblings

    row = {"gpu": gpu, "price": price_block(m and m["low"], m and m["median"], m and m["high"]),
           "low_provider": m["by_provider"][0]["provider"] if m and m["by_provider"] else None,
           "providers": m["providers"] if m else 0,
           "listings": {"available": m["listings"]["available"] if m else 0,
                        "priced": m["listings"]["priced"] if m else 0},
           "reason": None if m and m["providers"] else "no live priced listing now",
           "links": links(gpu)}
    if with_index:
        iid = indices.gpu_index_id(gpu)
        lv = indices.index_level(iid) or {}
        ch = (lv.get("changes") or {}).get("24h")
        row["index"] = {"id": iid, "level": lv.get("level"), "published": bool(lv.get("published")),
                        "change_24h": ({"pct": ch.get("pct"), "reason": ch.get("reason")} if ch else
                                       {"pct": None, "reason": lv.get("reason") or "index has no data"})}
    return row


def cheapest_variant(rows: list[dict]) -> dict | None:
    priced = [r for r in rows if r["price"]["low"] is not None]
    if not priced:
        return None
    r = min(priced, key=lambda r: (r["price"]["low"], r["gpu"]))
    return {"gpu": r["gpu"], "price_per_gpu_hour": r["price"]["low"], "provider": r["low_provider"],
            "label": CHEAPEST_LABEL}


@ttl_cache(60, maxsize=128)
def family_payload(fid: str) -> dict:
    d = fam.family(fid)
    if d is None:
        raise HTTPException(404, f"unknown GPU family {fid!r}; see /v1/families")
    markets = dispersion.all_markets()
    rows = [_variant(g, markets.get(g), True) for g in d["variants"]]
    return {"id": d["slug"], "name": d["name"], "kind": d["kind"], "architecture": d["architecture"],
            "member_families": _member_ids(d["member_families"]), "note": fam.NOTE,
            "variants": rows, "variants_total": len(rows),
            "variants_priced": sum(1 for r in rows if r["price"]["low"] is not None),
            "cheapest_variant_now": cheapest_variant(rows), "composite_index": _composite(d["id"])}


def _member_ids(members):
    return [fam.family_slug(m) for m in members] if members else members


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
        out.append({"id": d["slug"], "name": d["name"], "kind": d["kind"],
                    "architecture": d["architecture"], "member_families": _member_ids(d["member_families"]),
                    "variants": [{"gpu": r["gpu"], "price": r["price"], "providers": r["providers"]} for r in rows],
                    "variants_priced": sum(1 for r in rows if r["price"]["low"] is not None),
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
