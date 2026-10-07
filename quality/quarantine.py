"""The screen between the normalizers and the database, and the quarantine it keeps.

normalize.refresh calls `screen(listings, only)` before anything is saved. Per provider:

  1. Compare each new listing with its APPLIED state in compute_listings (checks.py).
  2. A "hold" finding opens (or advances) a pending quarantine row and the listing goes
     on with the field's last good value instead (None if it never had one).
  3. A "flag" finding records an incident; nothing is held.
  4. Provider level: an empty result when the provider normally has listings is an
     incident and its previous state is kept untouched; a listing-count explosion holds
     back the NEW listing ids until it persists or an operator accepts it.

Lifecycle of a held value (see methodology/data-quality.md):
    pending        held; the listing keeps its last good value
    auto_accepted  the same value came back in K_POLLS consecutive sightings spanning
                   at least min_span(provider), and the rule allows auto-accept; it is then
                   applied by that very poll
    accepted       an operator accepted it (POST /v1/ops/quarantine/{id}/accept); applied now
    rejected       an operator rejected it; while the provider keeps sending that value
                   it stays held (an accepted / rejected value is remembered per field)
    superseded     the provider stopped sending it (went back, or sent another value)
                   before anyone resolved it; closed by the system, never applied

Freshness while a price is held: a listing whose price is held is still marked seen
(observed_at moves) for at most grace(provider) after the suspicious value first appeared.
After that, or once the value is rejected, the listing is withheld from the save
altogether, so it ages out through market.stale_after() like any listing we cannot
confirm. We never present an old price as current indefinitely, and never write a
fabricated "gone" or "sold out" row.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert

import normalize
from models import ComputeListing
from providers import PROVIDERS
from quality import checks, incidents
from quality.checks import CAPACITY, COUNT, GPU_COUNT, MAPPING, PRICE, REGION
from store.quality import QualityPoll, Quarantine
from tables import ComputeListingRow, ListingObservation

log = logging.getLogger(__name__)

K_POLLS = 4                         # sightings before a persistent value may auto-accept
MIN_SPAN_FLOOR = timedelta(minutes=30)
COUNT_TOLERANCE = 0.25              # listing counts within 25% are "the same" explosion
FLAG_KINDS = tuple(r for r, (_, action, _, _) in checks.RULES.items() if action == "flag" and r not in ("listing_collapse", "empty_response"))

# Which ComputeListing fields a held field covers.
FIELDS_OF = {
    PRICE: ("price_per_gpu_hour", "price_per_instance_hour"),
    GPU_COUNT: ("gpu_count",),
    REGION: ("region", "country"),
    CAPACITY: ("capacity",),
    MAPPING: ("canonical_gpu_name",),
}

_normalizer_failed_at: dict[str, float] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def interval(provider: str) -> float:
    cls = PROVIDERS.get(provider)
    return cls.polling.interval_seconds if cls else 900.0


def min_span(provider: str) -> timedelta:
    """K_POLLS sightings span (K_POLLS - 1) intervals; allow 10% jitter, never under 30 min."""
    return max(MIN_SPAN_FLOOR, timedelta(seconds=0.9 * (K_POLLS - 1) * interval(provider)))


def grace(provider: str) -> timedelta:
    """How long a held price may still be presented as the listing's current price."""
    return timedelta(seconds=max(3600.0, (K_POLLS + 1) * interval(provider)))


def _json(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, datetime):
        return v.isoformat()
    return v


def normalizer_failed(provider: str, exc: BaseException) -> None:
    """A normalizer raised: keep the previous state (nothing is saved) and record it."""
    _normalizer_failed_at[provider] = time.monotonic()
    incidents.record_now("normalizer_error", provider=provider, severity="major",
                         detail={"error": f"{type(exc).__name__}: {exc}"[:500]})


# --------------------------------------------------------------------------
# The screen
# --------------------------------------------------------------------------


def screen(listings: list[ComputeListing], only: list[str] | None = None) -> list[ComputeListing]:
    """The listings to save: held fields replaced by their last good values, some withheld.

    Fails open per provider: if screening one provider errors, its listings pass
    through unchanged and a quality_layer_error incident is recorded.
    """
    by: dict[str, list[ComputeListing]] = defaultdict(list)
    for listing in listings:
        by[listing.provider].append(listing)
    providers = set(by) | set(only or ())
    if only is None:
        with normalize.SessionLocal() as s:
            providers |= set(s.execute(select(ComputeListingRow.provider).distinct()).scalars())
    out: list[ComputeListing] = []
    for provider in sorted(providers):
        items = by.get(provider, [])
        try:
            out.extend(_screen_provider(provider, items))
        except Exception as exc:
            log.exception("quality screen failed for %s; passing its listings through", provider)
            incidents.record_now("quality_layer_error", provider=provider, severity="major",
                                 detail={"error": f"{type(exc).__name__}: {exc}"[:500]})
            out.extend(items)
    return out


def _load(s, provider: str):
    c = ComputeListingRow
    cols = [c.listing_id, c.price_per_gpu_hour, c.price_per_instance_hour, c.gpu_count, c.region,
            c.country, c.capacity, c.canonical_gpu_name, c.observed_at]
    prev = {r.listing_id: dict(r._mapping) for r in s.execute(select(*cols).where(c.provider == provider))}
    pending = {
        (q.listing_id, q.field): q
        for q in s.execute(select(Quarantine).where(Quarantine.provider == provider,
                                                    Quarantine.status == "pending")).scalars()
    }
    decided = {
        (r.listing_id, r.field): dict(r._mapping)
        for r in s.execute(text(
            """SELECT DISTINCT ON (listing_id, field) id, listing_id, field, status, new_value, last_seen
               FROM quality_quarantine
               WHERE provider = :p AND status IN ('accepted', 'auto_accepted', 'rejected')
               ORDER BY listing_id, field, resolved_at DESC, id DESC"""), {"p": provider})
    }
    recent = s.execute(text(
        "SELECT polled_at, listings FROM quality_polls WHERE provider = :p ORDER BY polled_at DESC LIMIT :n"),
        {"p": provider, "n": checks.NORM_POLLS + 1}).all()
    return prev, pending, decided, recent


def _hold(s, provider, lid, f: checks.Finding, polled_at, new_poll, pending, decided, snapshot) -> tuple[str, datetime]:
    """Advance the quarantine for one held field. Returns (state, first_seen) where state
    is 'pass' (apply the new value), 'pending' or 'rejected'."""
    key = (lid, f.field)
    tol = COUNT_TOLERANCE if f.field == COUNT else 0.01
    new = _json(f.new)
    now = _now()

    d = decided.get(key)
    if d and _same(d["new_value"], new, tol):
        if d["status"] in ("accepted", "auto_accepted"):
            return "pass", polled_at
        s.execute(update(Quarantine).where(Quarantine.id == d["id"]).values(
            last_seen=max(d["last_seen"], polled_at),
            seen_count=Quarantine.seen_count + (1 if polled_at > d["last_seen"] else 0)))
        return "rejected", polled_at

    detail = {"rules": f.detail.get("rules", [f.rule]), **{k: _json(v) for k, v in f.detail.items() if k != "rules"},
              "listing": snapshot}
    q = pending.get(key)
    if q is not None and not _same(q.new_value, new, tol):
        q.status, q.resolved_by, q.resolved_at, q.note = "superseded", "system", now, "provider sent another value"
        s.flush()  # the partial unique index allows one pending row per field
        q = None
    if q is None:
        q = Quarantine(provider=provider, listing_id=lid, field=f.field, rule=f.rule,
                       previous_value=_json(f.previous), new_value=new, detail=detail,
                       auto_acceptable=f.auto_acceptable, first_seen=polled_at, last_seen=polled_at,
                       seen_count=1, status="pending")
        s.add(q)
        s.flush()
        pending[key] = q
        return "pending", q.first_seen

    if new_poll and polled_at > q.last_seen:
        q.seen_count += 1
        q.last_seen = polled_at
    q.detail = detail
    q.auto_acceptable = f.auto_acceptable  # the latest reading decides (e.g. a jump that is now implausible)
    if q.auto_acceptable and q.seen_count >= K_POLLS and q.last_seen - q.first_seen >= min_span(provider):
        q.status, q.resolved_by, q.resolved_at = "auto_accepted", "system:auto", now
        q.note = f"seen in {q.seen_count} consecutive polls over {q.last_seen - q.first_seen}"
        return "pass", q.first_seen
    return "pending", q.first_seen


def _same(a, b, tol) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        m = max(abs(a), abs(b))
        return a == b or (m > 0 and abs(a - b) / m <= tol)
    return checks.same_value(a, b)


def _merge(findings: list[checks.Finding]) -> dict[str, checks.Finding]:
    """One hold per field; auto-accept only if every rule on that field allows it."""
    by_field: dict[str, checks.Finding] = {}
    for f in findings:
        cur = by_field.get(f.field)
        if cur is None:
            f.detail = {**f.detail, "rules": [f.rule]}
            by_field[f.field] = f
        else:
            cur.detail = {**f.detail, **cur.detail, "rules": cur.detail["rules"] + [f.rule]}
            cur.auto_acceptable = cur.auto_acceptable and f.auto_acceptable
    return by_field


def _screen_provider(provider: str, items: list[ComputeListing]) -> list[ComputeListing]:
    with normalize.SessionLocal.begin() as s:
        # The poller and a manual /normalize may screen the same provider at once.
        s.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"quality:{provider}"})
        prev, pending, decided, recent = _load(s, provider)
        polled_at = max((x.observed_at for x in items), default=None)
        last_polled = recent[0].polled_at if recent else None
        new_poll = polled_at is not None and (last_polled is None or polled_at > last_polled)
        earlier = [r.listings for r in recent if polled_at is None or r.polled_at < polled_at][: checks.NORM_POLLS]
        norm = checks.norm_of(earlier)
        failed_recently = time.monotonic() - _normalizer_failed_at.get(provider, -1e9) < 300

        count_findings = {f.rule: f for f in checks.check_count(len(items), norm, earlier[0] if earlier else None)}

        # Malformed / empty response: keep the previous state, record it, save nothing.
        if not items:
            if "empty_response" in count_findings and not failed_recently:
                f = count_findings["empty_response"]
                incidents.record(s, "empty_response", provider=provider, severity="major",
                                 detail={"previous_listings": f.previous, "kept_previous_state": True})
            return []
        incidents.resolve(s, "empty_response", provider)
        if not failed_recently:
            incidents.resolve(s, "normalizer_error", provider)

        if "listing_collapse" in count_findings:
            f = count_findings["listing_collapse"]
            incidents.record(s, "listing_collapse", provider=provider, severity="notable", at=polled_at,
                             detail={"norm": f.previous, "listings": f.new})
        else:
            incidents.resolve(s, "listing_collapse", provider)

        hold_new_ids = False
        if "duplicate_explosion" in count_findings:
            f = count_findings["duplicate_explosion"]
            f.detail["rules"] = [f.rule]
            state, _ = _hold(s, provider, "*", f, polled_at, new_poll, pending, decided,
                             {"listings": len(items), "new_ids": sum(1 for x in items if x.listing_id not in prev)})
            hold_new_ids = state != "pass"
        elif new_poll and ("*", COUNT) in pending:
            _supersede(pending.pop(("*", COUNT)), "listing count returned to normal")

        out, held_n, withheld_n = [], 0, 0
        triggered, checked = set(), []
        for listing in items:
            lid = listing.listing_id
            p = prev.get(lid)
            if hold_new_ids and p is None:
                withheld_n += 1
                continue
            findings = checks.check_listing(listing, p)
            checked.append(lid)
            for f in findings:
                if f.action == "flag":
                    key = incidents.key_for(f.rule, provider, lid)
                    triggered.add(key)
                    incidents.record(s, f.rule, provider=provider, listing_id=lid, severity=checks.RULES[f.rule][3],
                                     key=key, at=listing.observed_at,
                                     detail={k: _json(v) for k, v in {"new": f.new, **f.detail}.items()})
            holds = _merge([f for f in findings if f.action == "hold"])
            snapshot = listing.model_dump(mode="json")
            upd: dict = {}
            withhold = False
            for fld, f in holds.items():
                state, first_seen = _hold(s, provider, lid, f, listing.observed_at, new_poll, pending, decided, snapshot)
                if state == "pass":
                    continue
                if fld == MAPPING:
                    upd["canonical_gpu_name"] = None  # a conflicting mapping is withheld, never guessed
                elif p is None:
                    if fld in (GPU_COUNT,):
                        withhold = True  # no shape to fall back to
                    elif fld == PRICE:
                        upd.update(price_per_gpu_hour=None, price_per_instance_hour=None)
                    elif fld == CAPACITY:
                        upd["capacity"] = None
                else:
                    for col in FIELDS_OF[fld]:
                        upd[col] = p[col]
                    if fld == PRICE and (state == "rejected" or listing.observed_at - first_seen > grace(provider)):
                        withhold = True  # do not keep presenting an old price as current
            if new_poll:
                for (plid, pfld), q in list(pending.items()):
                    if plid == lid and pfld not in holds and q.status == "pending":
                        _supersede(q, "provider went back to a normal value")
                        pending.pop((plid, pfld))
            if withhold:
                withheld_n += 1
                continue
            if upd:
                held_n += 1
                listing = listing.model_copy(update=upd)
            out.append(listing)

        for kind in FLAG_KINDS:
            incidents.resolve(s, kind, provider, listing_ids=checked, keep_keys=triggered)

        stmt = insert(QualityPoll).values(provider=provider, polled_at=polled_at, listings=len(items),
                                          held=held_n, withheld=withheld_n, recorded_at=_now())
        s.execute(stmt.on_conflict_do_update(index_elements=["provider", "polled_at"],
                                             set_={"held": stmt.excluded.held, "withheld": stmt.excluded.withheld}))
        return out


def _supersede(q: Quarantine, note: str) -> None:
    q.status, q.resolved_by, q.resolved_at, q.note = "superseded", "system", _now(), note


# --------------------------------------------------------------------------
# Operator decisions
# --------------------------------------------------------------------------


class NotPending(Exception):
    pass


def resolve(qid: int, decision: str, by: str, note: str | None = None) -> dict:
    """Accept (apply now) or reject a pending hold."""
    if decision not in ("accept", "reject"):
        raise ValueError(decision)
    with normalize.SessionLocal.begin() as s:
        q = s.get(Quarantine, qid, with_for_update=True)
        if q is None:
            raise KeyError(qid)
        if q.status != "pending":
            raise NotPending(q.status)
        applied = _apply(s, q) if decision == "accept" else False
        q.status = "accepted" if decision == "accept" else "rejected"
        q.resolved_by, q.resolved_at, q.note = by[:64], _now(), note
        s.flush()
        out = as_dict(q)
    out["applied"] = applied
    return out


def _apply(s, q: Quarantine) -> bool:
    """Write an accepted value into current state, and history when a tracked value moved.

    Provider-level holds (listing_id '*') need no write: the next poll's new listings
    flow in because the accepted count is remembered.
    """
    if q.listing_id == "*":
        return False
    snap = (q.detail or {}).get("listing")
    if not snap:
        return False  # no reported listing stored with the hold: there is no value to apply
    cur = s.get(ComputeListingRow, (q.provider, q.listing_id))
    if cur is None:
        listing = ComputeListing(**snap)
    else:
        base = {f: getattr(cur, f) for f in ComputeListing.model_fields}
        reported = ComputeListing(**snap).model_dump()
        for col in FIELDS_OF.get(q.field, ()):
            if col in reported:
                base[col] = reported[col]
        base["observed_at"] = max(cur.observed_at, q.last_seen)
        listing = ComputeListing(**base)

    row = listing.model_dump()
    stmt = insert(ComputeListingRow).values({**row, "first_seen_at": listing.observed_at})
    s.execute(stmt.on_conflict_do_update(
        index_elements=["provider", "listing_id"],
        set_={c: getattr(stmt.excluded, c) for c in row if c not in ("provider", "listing_id")},
    ))
    last = s.execute(
        select(ListingObservation)
        .where(ListingObservation.provider == q.provider, ListingObservation.listing_id == q.listing_id)
        .order_by(ListingObservation.observed_at.desc()).limit(1)
    ).scalar_one_or_none()
    if normalize._moved(listing, last):
        vals = {c: row[c] for c in normalize.TRACKED}
        ins = insert(ListingObservation).values(provider=q.provider, listing_id=q.listing_id,
                                                observed_at=listing.observed_at, **vals)
        s.execute(ins.on_conflict_do_update(index_elements=["provider", "listing_id", "observed_at"], set_=vals))
    return True


def as_dict(q: Quarantine) -> dict:
    d = {c.name: getattr(q, c.name) for c in Quarantine.__table__.columns}
    return d


def queue(status: str | None = "pending", provider: str | None = None, limit: int = 200) -> list[dict]:
    stmt = select(Quarantine).order_by(Quarantine.first_seen.desc(), Quarantine.id.desc()).limit(limit)
    if status:
        stmt = stmt.where(Quarantine.status == status)
    if provider:
        stmt = stmt.where(Quarantine.provider == provider)
    with normalize.SessionLocal() as s:
        return [as_dict(q) for q in s.execute(stmt).scalars()]


def open_holds(providers: list[str] | None = None) -> dict[tuple[str, str], list[dict]]:
    """(provider, listing_id) -> pending holds, for trust labels."""
    q = "SELECT provider, listing_id, field, rule, first_seen FROM quality_quarantine WHERE status = 'pending'"
    params = {}
    if providers:
        q += " AND provider = ANY(:p)"
        params["p"] = list(providers)
    out: dict[tuple[str, str], list[dict]] = defaultdict(list)
    with normalize.SessionLocal() as s:
        for r in s.execute(text(q), params):
            out[(r.provider, r.listing_id)].append({"field": r.field, "rule": r.rule, "since": r.first_seen})
    return out
