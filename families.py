"""GPU families: the single source of truth for "H100" vs its canonical variants.

A family ("H100") is a NAME FOR A SET of canonical GPUs ("NVIDIA H100 80GB SXM5",
"NVIDIA H100 80GB PCIe", ...). The variants are different products (memory, form factor,
interconnect) and are never merged: a family has no price of its own. Every number the
API shows for a family is a per-variant number listed side by side.

Built from news/classify.py's FAMILIES / FAMILY_VARIANTS / FAMILY_ARCH / ARCHITECTURES so the
news classifier and the market API agree on what "H100" means; no regex is repeated here.

    all_families()          families with >= 1 canonical variant (models, then architectures)
    family(id_or_slug)      one family dict, or None
    variants(id)            its canonical variant names (sorted), [] if unknown
    family_slug(id)         'H100' -> 'h100', 'RTX PRO 6000' -> 'rtx-pro-6000'
    resolve_family(value)   id / slug / loose case -> family id, or None (only listed families)
    classifier_families()   every family the news classifier knows, tracked or not

kind is 'model' (one GPU model, e.g. H100) or 'architecture' (a generation, e.g. Blackwell:
the datacenter / cloud parts of that generation). See methodology/families.md.
"""

from __future__ import annotations

import re

from news import classify as _c

NOTE = "Variants are different products and are never merged; each is listed separately."
METHODOLOGY = "families"


def family_slug(fid: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", fid.lower()).strip("-")


def _kind(fid: str) -> str:
    return "architecture" if fid in _c.ARCHITECTURES else "model"


def _dict(fid: str) -> dict:
    kind = _kind(fid)
    return {"id": fid, "slug": family_slug(fid), "name": f"{fid} {'architecture' if kind == 'architecture' else 'family'}",
            "kind": kind, "architecture": _c.FAMILY_ARCH.get(fid),
            "variants": list(_c.FAMILY_VARIANTS.get(fid, [])),
            "member_families": list(_c.ARCHITECTURES[fid][1]) if kind == "architecture" else None}


def _order(fid: str):
    return (_kind(fid) == "architecture", fid)


def classifier_families() -> list[dict]:
    """Every family the news classifier recognises; tracked=False when no canonical variant exists yet."""
    out = []
    for fid in sorted(_c.FAMILY_VARIANTS, key=_order):
        d = _dict(fid)
        d["tracked"] = bool(d["variants"])
        out.append(d)
    return out


def all_families() -> list[dict]:
    """Families with at least one canonical variant: model families first, then architectures."""
    return [d for d in classifier_families() if d["tracked"]]


def resolve_family(value: str | None) -> str | None:
    """'h100' / 'H100' / 'rtx-pro-6000' / 'RTX PRO 6000' / 'blackwell' -> family id; None if not a listed family."""
    if not value:
        return None
    v = value.strip()
    s = family_slug(v)
    for fid, vs in _c.FAMILY_VARIANTS.items():
        if vs and (fid == v or family_slug(fid) == s):
            return fid
    return None


def family(id_or_slug: str) -> dict | None:
    fid = resolve_family(id_or_slug)
    return _dict(fid) if fid else None


def variants(fid: str) -> list[str]:
    fid = resolve_family(fid) or fid
    return list(_c.FAMILY_VARIANTS.get(fid, []))


def families_for_gpu(gpu: str) -> list[str]:
    """Family ids (model and architecture) that list this canonical GPU."""
    return _c.families_for_gpu(gpu)
