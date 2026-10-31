"""The capacity pipeline: raw snapshots -> CapacityObservation -> current state + change-only history.

It mirrors the listing pipeline (normalize.py) on purpose, so there is one way data moves through the system:

    fetch()               a provider pulls its capacity endpoint next to its price endpoints; stored raw
    normalize_capacity()  per provider, pure, re-runnable over stored raw (providers/<name>.py)
    save()                upsert capacity_current; append to capacity_changes only when the state moved

Adding a provider means two edits in its own module (fetch the endpoint, map it in normalize_capacity); nothing here
changes. Runs after the listing refresh and fails open: a capacity error never touches prices.
Methodology: methodology/capacity.md.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select, tuple_
from sqlalchemy.dialects.postgresql import insert

import normalize
from models import CapacityObservation
from providers import PROVIDERS, Provider
from store.capacity import CapacityChange, CapacityCurrent

log = logging.getLogger(__name__)

STATE = ("in_stock", "tier", "available_count", "total_count")


def supported() -> list[str]:
    """Providers whose module implements normalize_capacity."""
    return sorted(n for n, cls in PROVIDERS.items() if cls.normalize_capacity is not Provider.normalize_capacity)


def normalize_all(only: list[str] | None = None) -> list[CapacityObservation]:
    out: list[CapacityObservation] = []
    with normalize.SessionLocal() as s:
        for provider in supported():
            if only and provider not in only:
                continue
            try:
                out.extend(PROVIDERS[provider].normalize_capacity(normalize._latest_raw(s, provider)))
            except Exception:
                # shortcut: logged, not a quality incident yet; add one once capacity feeds are on the ops page
                log.exception("%s: capacity normalizer failed; keeping previous state", provider)
    return out


def _key(o) -> tuple[str, str, str]:
    return o.provider, o.sku, o.region or ""


def save(observations: list[CapacityObservation]) -> dict:
    """Upsert current state; record a change only when in_stock, tier or a count differs from what was stored."""
    if not observations:
        return {"current": 0, "changes": 0}
    latest = {_key(o): o for o in observations}  # one row per key; the last one wins
    now = datetime.now(timezone.utc)
    with normalize.SessionLocal.begin() as s:
        prev = {(r.provider, r.sku, r.region_key): r for r in s.scalars(select(CapacityCurrent).where(
            tuple_(CapacityCurrent.provider, CapacityCurrent.sku, CapacityCurrent.region_key).in_(list(latest))))}
        changes, rows = [], []
        for key, o in latest.items():
            old = prev.get(key)
            moved = old is None or any(getattr(old, f) != getattr(o, f) for f in STATE)
            if moved:
                changes.append(CapacityChange(provider=o.provider, sku=o.sku, region_key=key[2],
                                              canonical_gpu_name=o.canonical_gpu_name, observed_at=o.observed_at,
                                              raw_value=o.raw_value, **{f: getattr(o, f) for f in STATE}))
            rows.append({**o.model_dump(), "region_key": key[2],
                         "first_seen_at": old.first_seen_at if old else now,
                         "changed_at": o.observed_at if moved else old.changed_at})
        stmt = insert(CapacityCurrent).values(rows)
        s.execute(stmt.on_conflict_do_update(
            index_elements=["provider", "sku", "region_key"],
            set_={c: stmt.excluded[c] for c in rows[0] if c not in ("provider", "sku", "region_key", "first_seen_at")}))
        s.add_all(changes)
    return {"current": len(rows), "changes": len(changes)}


def refresh(only: list[str] | None = None) -> dict:
    return save(normalize_all(only))
