"""Writes for the site tables (store/power.py, migration 0045). Every write is change-only, so re-applying the
same answer stores nothing new.

    record(source, dataset, f, start, end, applied)   a power_fetches row (url + sha256; the body is never kept)
    fetched_ok(dataset)                               {url: period_start} of answers already applied
    apply_generators(rows, period, via, scope, fid)   generator snapshot -> power_generators + history
    upsert_generation(year, plants, fid)              annual net MWh per plant
    replace_edition(source, edition, rows, vis, fid)  one queue edition, in place

Generators. A row holds the generator's state as of `period`, the inventory month of its last change. A snapshot
for month P updates a generator whose stored period is older (or the same month from the same or a lower-ranked
publication: the EIA-860M file outranks the API for the same month, since the two round coordinates differently).
A history row is written when the generator first appears, when a TRACKED field changes, or when it leaves:
    "api" snapshots list operable generators only: one stored as operable but absent from month P has left the
           operable inventory (retired, or removed by EIA): status RE, retirement month unknown until the file says
    "file" snapshots list every status: one absent from every sheet is status XX, group "removed"
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone

from power.registry import Fetched

UTC = timezone.utc
TRACKED = ("status", "nameplate_mw", "summer_mw", "planned_operation_ym", "planned_retirement_ym", "retirement_ym")
ATTRS = ("plant_name", "entity_id", "entity_name", "state", "county", "latitude", "longitude", "ba_code", "sector",
         "technology", "energy_source", "prime_mover", "nameplate_mw", "summer_mw", "winter_mw", "status",
         "status_group", "operating_ym", "planned_operation_ym", "planned_retirement_ym", "retirement_ym")
OPERABLE = ("operating", "standby", "out_of_service")
RANK = {"api": 0, "file": 1}


def _session():
    from power import storage

    return storage._session()


def record(source: str, dataset: str, f: Fetched, start: date | None = None, end: date | None = None,
           applied: bool = False, fetched_at: datetime | None = None) -> int:
    from store.power import PowerFetch

    with _session().begin() as s:
        row = PowerFetch(source=source, dataset=dataset, url=f.url[:1000], period_start=start, period_end=end,
                         fetched_at=fetched_at or datetime.now(UTC), status_code=f.status, duration_ms=f.ms,
                         ok=f.ok or f.empty, empty=f.empty, error=(f.error or None) and f.error[:2000],
                         bytes=None if f.body is None else len(f.body),
                         sha256=None if f.body is None else hashlib.sha256(f.body).hexdigest(),
                         content_type=(f.content_type or None) and f.content_type[:80], mode="hash", encoding=None,
                         body=None, days_stored=1 if applied else 0)
        s.add(row)
        s.flush()
        return row.id


def mark_applied(fetch_id: int) -> None:
    from sqlalchemy import update

    from store.power import PowerFetch

    with _session().begin() as s:
        s.execute(update(PowerFetch).where(PowerFetch.id == fetch_id).values(days_stored=1))


def applied(dataset: str) -> dict[str, date | None]:
    """{url: period_start} of the answers of `dataset` that were parsed and stored (days_stored = 1)."""
    from sqlalchemy import text

    with _session()() as s:
        return dict(s.execute(text("""SELECT url, max(period_start) FROM power_fetches
                                      WHERE dataset = :d AND ok AND days_stored > 0 GROUP BY url"""),
                              {"d": dataset}).all())


def applied_periods(dataset: str) -> set[date]:
    from sqlalchemy import text

    with _session()() as s:
        return {r[0] for r in s.execute(text("""SELECT DISTINCT period_start FROM power_fetches
                                                WHERE dataset = :d AND ok AND days_stored > 0
                                                AND period_start IS NOT NULL"""), {"d": dataset})}


# --------------------------------------------------------------------------- generators

def _same(a, b) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        return a is not None and b is not None and abs(float(a) - float(b)) < 1e-9
    return a == b


def apply_generators(rows: list[dict], period: date, via: str, scope: str, fetch_id: int | None,
                     now: datetime | None = None) -> dict:
    """scope "operable" (an API month) or "all" (an EIA-860M file). Returns counts."""
    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert

    from store.power import PowerGenerator as G, PowerGeneratorHistory as H

    now = now or datetime.now(UTC)
    out = {"rows": len(rows), "new": 0, "changed": 0, "updated": 0, "left": 0, "unchanged": 0, "stale": 0}
    cols = (G.plant_id, G.generator_id, G.period, G.via, G.first_period, *[getattr(G, a) for a in ATTRS])
    with _session()() as s:
        cur = {(r.plant_id, r.generator_id): r for r in s.execute(select(*cols))}
    upserts: list[dict] = []
    hist: list[dict] = []
    seen = set()
    for r in rows:
        k = (r["plant_id"], r["generator_id"])
        if k in seen:
            continue                     # the file lists a few generators twice (Operating and Retired): first wins
        seen.add(k)
        c = cur.get(k)
        if c is not None and (c.period > period or (c.period == period and RANK[c.via] > RANK[via])):
            out["stale"] += 1
            continue
        if c is not None and all(_same(getattr(c, a), r.get(a)) for a in ATTRS):
            out["unchanged"] += 1
            continue
        change = "new" if c is None else ("changed" if any(not _same(getattr(c, a), r.get(a)) for a in TRACKED)
                                          else None)
        upserts.append({**{a: r.get(a) for a in ATTRS}, "plant_id": k[0], "generator_id": k[1], "period": period,
                        "via": via, "first_period": c.first_period if c is not None else period,
                        "fetch_id": fetch_id, "updated_at": now})
        if change:
            out[change] += 1
            hist.append({"plant_id": k[0], "generator_id": k[1], "period": period, "change": change,
                         **{a: r.get(a) for a in TRACKED}, "status_group": r["status_group"], "via": via,
                         "fetch_id": fetch_id})
        else:
            out["updated"] += 1
    for k, c in cur.items():
        if k in seen or c.period >= period:
            continue
        if scope == "operable" and c.status_group in OPERABLE:
            status, group = "RE", "retired"
        elif scope == "all" and c.status_group not in ("removed",):
            status, group = "XX", "removed"
        else:
            continue
        out["left"] += 1
        upserts.append({**{a: getattr(c, a) for a in ATTRS}, "status": status, "status_group": group,
                        "plant_id": k[0], "generator_id": k[1], "period": period, "via": via,
                        "first_period": c.first_period, "fetch_id": fetch_id, "updated_at": now})
        hist.append({"plant_id": k[0], "generator_id": k[1], "period": period, "change": "left", "status": status,
                     "status_group": group, **{a: getattr(c, a) for a in TRACKED if a != "status"}, "via": via,
                     "fetch_id": fetch_id})
    with _session().begin() as s:
        for i in range(0, len(upserts), 2000):
            stmt = insert(G).values(upserts[i:i + 2000])
            ex = stmt.excluded
            s.execute(stmt.on_conflict_do_update(
                index_elements=[G.plant_id, G.generator_id],
                set_={c: getattr(ex, c) for c in (*ATTRS, "period", "via", "fetch_id", "updated_at")}))
        for i in range(0, len(hist), 2000):
            stmt = insert(H).values(hist[i:i + 2000])
            ex = stmt.excluded
            s.execute(stmt.on_conflict_do_update(
                constraint="uq_power_generator_history",
                set_={c: getattr(ex, c) for c in (*TRACKED, "status_group", "change", "via", "fetch_id")}))
    return out


# --------------------------------------------------------------------------- generation

def upsert_generation(year: int, plants: dict[int, tuple[float | None, str | None]], fetch_id: int | None,
                      now: datetime | None = None) -> dict:
    from sqlalchemy import literal_column
    from sqlalchemy.dialects.postgresql import insert

    from store.power import PowerPlantGeneration as P

    now = now or datetime.now(UTC)
    vals = [{"plant_id": p, "year": year, "net_mwh": v, "state": st, "fetch_id": fetch_id, "updated_at": now,
             "revisions": 0} for p, (v, st) in plants.items()]
    new = revised = 0
    with _session().begin() as s:
        for i in range(0, len(vals), 2000):
            stmt = insert(P).values(vals[i:i + 2000])
            ex = stmt.excluded
            stmt = stmt.on_conflict_do_update(
                index_elements=[P.plant_id, P.year],
                set_={"net_mwh": ex.net_mwh, "state": ex.state, "fetch_id": ex.fetch_id, "updated_at": ex.updated_at,
                      "revisions": P.revisions + 1},
                where=P.net_mwh.is_distinct_from(ex.net_mwh) | P.state.is_distinct_from(ex.state),
            ).returning(literal_column("(xmax = 0)").label("inserted"))
            for r in s.execute(stmt):
                new, revised = (new + 1, revised) if r.inserted else (new, revised + 1)
    return {"new": new, "revised": revised, "unchanged": len(vals) - new - revised}


# --------------------------------------------------------------------------- queues

QUEUE_COLS = ("iso", "entity", "state", "county", "fips", "kind", "type", "type_detail", "mw", "status", "phase",
              "queue_date", "proposed_cod", "on_date", "withdrawn_date", "ia_date", "poi", "project_name",
              "developer")


def replace_edition(source: str, edition: date, rows: list[dict], visibility: str, fetch_id: int | None,
                    now: datetime | None = None) -> dict:
    """Upsert one edition's projects where they differ and drop the edition's rows the new answer no longer has
    (a republished monthly report replaces its month)."""
    from sqlalchemy import delete, literal_column
    from sqlalchemy.dialects.postgresql import insert

    from store.power import PowerQueueProject as Q

    now = now or datetime.now(UTC)
    vals = [{**{c: r.get(c) for c in QUEUE_COLS}, "source": source, "edition": edition,
             "project_id": r["project_id"], "visibility": visibility, "fetch_id": fetch_id, "updated_at": now}
            for r in rows]
    new = changed = 0
    with _session().begin() as s:
        for i in range(0, len(vals), 2000):
            stmt = insert(Q).values(vals[i:i + 2000])
            ex = stmt.excluded
            differs = None
            for c in (*QUEUE_COLS, "visibility"):
                d = getattr(Q, c).is_distinct_from(getattr(ex, c))
                differs = d if differs is None else differs | d
            stmt = stmt.on_conflict_do_update(
                index_elements=[Q.source, Q.edition, Q.project_id],
                set_={c: getattr(ex, c) for c in (*QUEUE_COLS, "visibility", "fetch_id", "updated_at")},
                where=differs,
            ).returning(literal_column("(xmax = 0)").label("inserted"))
            for r in s.execute(stmt):
                new, changed = (new + 1, changed) if r.inserted else (new, changed + 1)
        ids = [v["project_id"] for v in vals]
        removed = s.execute(delete(Q).where(Q.source == source, Q.edition == edition,
                                            Q.project_id.not_in(ids) if ids else True)).rowcount
    return {"rows": len(vals), "new": new, "changed": changed, "unchanged": len(vals) - new - changed,
            "removed": removed}


def editions(source: str) -> set[date]:
    from sqlalchemy import text

    with _session()() as s:
        return {r[0] for r in s.execute(text("SELECT DISTINCT edition FROM power_queue_projects WHERE source = :s"),
                                        {"s": source})}
