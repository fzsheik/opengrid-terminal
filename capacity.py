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
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, tuple_
from sqlalchemy.dialects.postgresql import insert

import normalize
import provider_meta
from models import CapacityObservation
from providers import PROVIDERS, Provider
from store.capacity import CapacityChange, CapacityCurrent

log = logging.getLogger(__name__)

STATE = ("in_stock", "tier", "available_count", "total_count")
# A current row its provider has stopped reporting is removed once it has not been seen for this long: a day, or six
# of the provider's capacity reads when those are further apart (Vast's census runs up to 12 hours apart).
EXPIRE_AFTER = timedelta(hours=24)
EXPIRE_AFTER_READS = 6
EXPIRED = "no longer reported"


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
            tier = provider_meta.enforce(o.provider, o.visibility)  # stamped here, failing closed
            if moved:
                changes.append(CapacityChange(provider=o.provider, sku=o.sku, region_key=key[2],
                                              canonical_gpu_name=o.canonical_gpu_name, observed_at=o.observed_at,
                                              raw_value=o.raw_value, visibility=tier,
                                              **{f: getattr(o, f) for f in STATE}))
            rows.append({**o.model_dump(), "visibility": tier, "region_key": key[2],
                         "first_seen_at": old.first_seen_at if old else now,
                         "changed_at": o.observed_at if moved else old.changed_at})
        stmt = insert(CapacityCurrent).values(rows)
        s.execute(stmt.on_conflict_do_update(
            index_elements=["provider", "sku", "region_key"],
            set_={c: stmt.excluded[c] for c in rows[0] if c not in ("provider", "sku", "region_key", "first_seen_at")}))
        s.add_all(changes)
    return {"current": len(rows), "changes": len(changes)}


def prune(observations: list[CapacityObservation]) -> int:
    """Drop current rows that came from a source a provider no longer uses, for every provider in this run.

    A provider that moved to its own API (Massed Compute off Shadeform) would otherwise keep the old source's rows
    forever beside the new ones. Rows from a source still in use are kept even when this run lacks them: those age
    into `stale: true` (methodology/capacity.md) and are removed by expire() after a day. A provider whose normalizer failed or returned nothing is absent
    here and keeps its previous state. History stays in capacity_changes.
    """
    sources: dict[str, set[str]] = {}
    for o in observations:
        sources.setdefault(o.provider, set()).add(o.source)
    dropped = 0
    with normalize.SessionLocal.begin() as s:
        for provider, used in sources.items():
            old = [r for r in s.scalars(select(CapacityCurrent).where(CapacityCurrent.provider == provider))
                   if r.source not in used]
            for r in old:
                s.delete(r)
            dropped += len(old)
    return dropped


def expire(observations: list[CapacityObservation], now: datetime | None = None) -> int:
    """Remove current rows a provider has stopped reporting, for every provider in this run.

    A sku and region the provider's answer no longer carries (a machine type that left a marketplace, a plan that
    was withdrawn) used to stay in capacity_current for ever with its last state, so "in stock" could describe a
    row nobody had seen for weeks. Until EXPIRE_AFTER it only ages into `stale: true`, which covers a provider
    leaving a row out for a few reads. After that the row is deleted, and one last capacity_changes row with no
    state and raw_value "no longer reported (last seen ...)" marks the end in the history.

    Only providers that answered in this run are touched, and only rows this run did not report: a provider whose
    read failed keeps everything, however old.
    """
    now = now or datetime.now(timezone.utc)
    reported: dict[str, set[tuple[str, str, str]]] = {}
    for o in observations:
        reported.setdefault(o.provider, set()).add(_key(o))
    gone = 0
    with normalize.SessionLocal.begin() as s:
        for provider, keys in reported.items():
            cadence = PROVIDERS[provider].polling.capacity_seconds if provider in PROVIDERS else 0
            cutoff = now - max(EXPIRE_AFTER, timedelta(seconds=EXPIRE_AFTER_READS * cadence))
            old = [r for r in s.scalars(select(CapacityCurrent).where(CapacityCurrent.provider == provider,
                                                                       CapacityCurrent.observed_at < cutoff))
                   if (r.provider, r.sku, r.region_key) not in keys]
            for r in old:
                s.add(CapacityChange(provider=r.provider, sku=r.sku, region_key=r.region_key,
                                     canonical_gpu_name=r.canonical_gpu_name, observed_at=now, in_stock=None, tier=None,
                                     available_count=None, total_count=None,
                                     raw_value=f"{EXPIRED} (last seen {r.observed_at.isoformat()})",
                                     visibility=r.visibility))
                s.delete(r)
            gone += len(old)
    return gone


def refresh(only: list[str] | None = None) -> dict:
    observations = normalize_all(only)
    out = save(observations)
    out["pruned"] = prune(observations)
    out["expired"] = expire(observations)
    return out
