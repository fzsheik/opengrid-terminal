"""Source schema change detection: a fingerprint of each raw response's JSON shape.

A job reads raw_snapshots newer than its cursor (by id, read-only; payloads are never
modified) and fingerprints each ok payload per (provider, endpoint):

    shape   the set of "path:type" strings. Lists are merged: every element's keys
            count under "path[]", so an optional key present in any element is part of
            the shape. A dict with more than WIDE_DICT keys, or whose keys look like
            data (digits, spaces, dots), is treated as a map: its keys collapse to "*"
            (AWS keys prices by instance name, Hyperstack stocks by "8x").
            A path seen only as null is recorded as "path:null"; when comparing shapes,
            null is compatible with any type, so a field that is sometimes null is not a
            schema change (the new fingerprint is remembered without an incident).
    bounds  at most MAX_ELEMENTS elements per list and MAX_NODES nodes per payload are
            visited, and at most MAX_PATHS paths are kept (truncated=True beyond that).

A fingerprint never seen before for that endpoint, when the endpoint already had one,
is a schema_change incident when paths were added or removed or a path's (non-null)
types changed; removals and type changes are "major", pure additions "notable". Returning to a shape seen
before is not a new change, so an endpoint flipping between two known shapes (an
optional block present on some polls) does not raise an incident on every poll.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

import normalize
from jobs import job
from quality import incidents
from store.quality import QualityCursor, SourceSchema

log = logging.getLogger(__name__)

MAX_ELEMENTS = 500
MAX_NODES = 50_000
MAX_PATHS = 2_000
WIDE_DICT = 40
BATCH = 200
CURSOR = "schema_watch"
_DATA_KEY = re.compile(r"[0-9\s./:]")


def _type(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    return type(v).__name__


def shape(payload) -> tuple[list[str], bool]:
    """Sorted "path:type" strings for a JSON value, and whether the walk was truncated."""
    paths: set[str] = set()
    nullable: set[str] = set()
    budget = [MAX_NODES]
    truncated = False

    def walk(v, path):
        nonlocal truncated
        if budget[0] <= 0:
            truncated = True
            return
        budget[0] -= 1
        t = _type(v)
        if t is None:
            nullable.add(path or "$")
        else:
            paths.add(f"{path or '$'}:{t}")
        if isinstance(v, dict):
            as_map = len(v) > WIDE_DICT or (v and all(_DATA_KEY.search(str(k)) for k in v))
            for k, child in v.items():
                walk(child, f"{path}.{'*' if as_map else k}")
        elif isinstance(v, list):
            if len(v) > MAX_ELEMENTS:
                truncated = True
            for child in v[:MAX_ELEMENTS]:
                walk(child, f"{path}[]")

    walk(payload, "")
    typed = {p.rsplit(":", 1)[0] for p in paths}
    paths |= {f"{p}:null" for p in nullable - typed}  # present, but only ever null here
    out = sorted(paths)
    if len(out) > MAX_PATHS:
        out, truncated = out[:MAX_PATHS], True
    return out, truncated


def _parse(paths: list[str]) -> dict[str, set[str]]:
    """{path: non-null types}; a path seen only as null has an empty set."""
    out: dict[str, set[str]] = {}
    for entry in paths:
        path, _, t = entry.rpartition(":")
        out.setdefault(path, set())
        if t != "null":
            out[path].add(t)
    return out


def fingerprint(paths: list[str]) -> str:
    return hashlib.sha256("\n".join(paths).encode()).hexdigest()


def scan(batch: int = BATCH, max_batches: int = 50) -> dict:
    """Fingerprint raw snapshots newer than the cursor. Returns counts."""
    seen = changes = 0
    with normalize.SessionLocal.begin() as s:
        if s.get(QualityCursor, CURSOR) is None:
            # First run: baseline each endpoint from its newest ok snapshot, then follow
            # new rows only, instead of replaying years of history through the watcher.
            rows = s.execute(text(
                """SELECT r.id, r.provider, r.endpoint, r.fetched_at, r.ok, r.payload FROM raw_snapshots r
                   JOIN (SELECT DISTINCT ON (provider, endpoint) id FROM raw_snapshots WHERE ok
                         ORDER BY provider, endpoint, fetched_at DESC, id DESC) n ON n.id = r.id""")).all()
            for r in rows:
                if r.payload is not None:
                    seen += 1
                    _observe(s, r)
            top = s.execute(text("SELECT coalesce(max(id), 0) FROM raw_snapshots")).scalar()
            s.add(QualityCursor(name=CURSOR, position=top, updated_at=datetime.now(timezone.utc)))
    for _ in range(max_batches):
        with normalize.SessionLocal.begin() as s:
            cur = s.get(QualityCursor, CURSOR)
            rows = s.execute(text(
                """SELECT id, provider, endpoint, fetched_at, ok, payload FROM raw_snapshots
                   WHERE id > :pos ORDER BY id LIMIT :n"""), {"pos": cur.position, "n": batch}).all()
            if not rows:
                break
            for r in rows:
                if r.ok and r.payload is not None:
                    seen += 1
                    changes += _observe(s, r)
            cur.position = rows[-1].id
            cur.updated_at = datetime.now(timezone.utc)
        if len(rows) < batch:
            break
    return {"snapshots": seen, "schema_changes": changes}


def _observe(s, r) -> int:
    paths, truncated = shape(r.payload)
    fp = fingerprint(paths)
    known = s.execute(
        select(SourceSchema).where(SourceSchema.provider == r.provider, SourceSchema.endpoint == r.endpoint)
        .order_by(SourceSchema.last_seen.desc())
    ).scalars().all()
    hit = next((k for k in known if k.fingerprint == fp), None)
    if hit is not None:
        if r.fetched_at >= hit.last_seen:
            hit.last_seen, hit.last_raw_id = r.fetched_at, r.id
        hit.seen_count += 1
        return 0
    s.execute(insert(SourceSchema).values(
        provider=r.provider, endpoint=r.endpoint, fingerprint=fp, paths=paths, path_count=len(paths),
        truncated=truncated, first_seen=r.fetched_at, last_seen=r.fetched_at, seen_count=1,
        first_raw_id=r.id, last_raw_id=r.id,
    ).on_conflict_do_nothing())
    if not known:
        return 0  # the first shape we ever saw is a baseline, not a change
    before, now = _parse(known[0].paths), _parse(paths)
    added, removed = sorted(set(now) - set(before)), sorted(set(before) - set(now))
    retyped = sorted(f"{p}: {'|'.join(sorted(before[p]))} -> {'|'.join(sorted(now[p]))}"
                     for p in set(now) & set(before) if now[p] and before[p] and now[p] != before[p])
    if not (added or removed or retyped):
        return 0  # only null-vs-value differences: a compatible shape, remembered silently
    incidents.record(
        s, "schema_change", provider=r.provider, endpoint=r.endpoint,
        severity="major" if removed or retyped else "notable",
        key=incidents.key_for("schema_change", r.provider, None, f"{r.endpoint}:{fp[:16]}"),
        at=r.fetched_at,
        detail={"fingerprint": fp, "previous_fingerprint": known[0].fingerprint, "raw_snapshot_id": r.id,
                "added": added[:50], "removed": removed[:50], "retyped": retyped[:50], "added_count": len(added),
                "removed_count": len(removed), "truncated": truncated},
    )
    return 1


@job("quality_schema_watch", every_seconds=120, initial_delay_seconds=60)
def _job():
    return scan()


def changes(provider: str | None = None, limit: int = 200) -> list[dict]:
    return incidents.recent(provider=provider, kinds=["schema_change"], limit=limit)
