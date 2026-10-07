"""Watchlists: named sets of things an account follows (GPUs, providers, regions, indices).

Item kinds and the fields each needs:
    gpu           gpu (canonical name; slugs accepted at the API)
    provider      provider
    gpu_provider  gpu + provider
    region        region_group (one of regions.REGION_GROUPS), optionally gpu
    index         index_id
Reads attach a `current` block for gpu / gpu_provider items from the live market (observed data).
"""

from __future__ import annotations

from sqlalchemy import delete, select

import normalize
from store.accounts import Watchlist, WatchlistItem

KINDS = {"gpu": ("gpu",), "provider": ("provider",), "gpu_provider": ("gpu", "provider"),
         "region": ("region_group",), "index": ("index_id",)}


def _item(i: WatchlistItem) -> dict:
    return {"id": i.id, "kind": i.kind, "gpu": i.gpu, "provider": i.provider, "region_group": i.region_group,
            "index_id": i.index_id, "created_at": i.created_at}


def _list(w: Watchlist, items) -> dict:
    return {"id": w.id, "name": w.name, "created_at": w.created_at, "items": [_item(i) for i in items]}


def create(account_id: int, name: str) -> dict:
    if not name or not name.strip():
        raise ValueError("name is required")
    with normalize.SessionLocal.begin() as s:
        w = Watchlist(account_id=account_id, name=name.strip()[:200])
        s.add(w)
        s.flush()
        return _list(w, [])


def _owned(s, account_id: int, watchlist_id: int) -> Watchlist:
    w = s.get(Watchlist, watchlist_id)
    if w is None or w.account_id != account_id:
        raise KeyError(watchlist_id)
    return w


def all_for(account_id: int, with_current: bool = False) -> list[dict]:
    with normalize.SessionLocal() as s:
        lists = list(s.scalars(select(Watchlist).where(Watchlist.account_id == account_id).order_by(Watchlist.id)))
        items = {}
        if lists:
            for i in s.scalars(select(WatchlistItem).where(WatchlistItem.watchlist_id.in_([w.id for w in lists]))
                               .order_by(WatchlistItem.id)):
                items.setdefault(i.watchlist_id, []).append(i)
        out = [_list(w, items.get(w.id, [])) for w in lists]
    if with_current:
        _attach_current(out)
    return out


def get(account_id: int, watchlist_id: int, with_current: bool = True) -> dict:
    with normalize.SessionLocal() as s:
        w = _owned(s, account_id, watchlist_id)
        items = list(s.scalars(select(WatchlistItem).where(WatchlistItem.watchlist_id == w.id).order_by(WatchlistItem.id)))
        out = _list(w, items)
    if with_current:
        _attach_current([out])
    return out


def rename(account_id: int, watchlist_id: int, name: str) -> dict:
    with normalize.SessionLocal.begin() as s:
        w = _owned(s, account_id, watchlist_id)
        w.name = name.strip()[:200]
    return get(account_id, watchlist_id, with_current=False)


def remove(account_id: int, watchlist_id: int) -> None:
    with normalize.SessionLocal.begin() as s:
        w = _owned(s, account_id, watchlist_id)
        s.delete(w)


def add_item(account_id: int, watchlist_id: int, kind: str, gpu=None, provider=None, region_group=None, index_id=None) -> dict:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {sorted(KINDS)}")
    fields = {"gpu": gpu, "provider": provider, "region_group": region_group, "index_id": index_id}
    missing = [f for f in KINDS[kind] if not fields[f]]
    if missing:
        raise ValueError(f"a {kind} item needs {missing}")
    if region_group:
        try:
            from regions import REGION_GROUPS
        except ImportError:
            REGION_GROUPS = None
        if REGION_GROUPS and region_group not in REGION_GROUPS:
            raise ValueError(f"region_group must be one of {REGION_GROUPS}")
    with normalize.SessionLocal.begin() as s:
        _owned(s, account_id, watchlist_id)
        i = WatchlistItem(watchlist_id=watchlist_id, kind=kind, **fields)
        s.add(i)
        s.flush()
        return _item(i)


def remove_item(account_id: int, watchlist_id: int, item_id: int) -> None:
    with normalize.SessionLocal.begin() as s:
        _owned(s, account_id, watchlist_id)
        n = s.execute(delete(WatchlistItem).where(WatchlistItem.id == item_id, WatchlistItem.watchlist_id == watchlist_id)).rowcount
        if not n:
            raise KeyError(item_id)


def _attach_current(lists: list[dict]) -> None:
    from alerts import metrics

    ctx = metrics.Context()
    for w in lists:
        for i in w["items"]:
            if i["kind"] not in ("gpu", "gpu_provider"):
                continue
            lows = metrics._provider_lows(ctx, i["gpu"])
            if i["kind"] == "gpu_provider":
                p = lows.get(i["provider"])
                i["current"] = {"kind": "observed", "price_per_gpu_hour": p, "listed_now": p is not None}
            else:
                best = min(lows, key=lows.get) if lows else None
                i["current"] = {"kind": "observed", "lowest": lows.get(best) if best else None,
                                "lowest_provider": best, "providers": len(lows)}
