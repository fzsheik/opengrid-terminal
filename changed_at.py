"""When a recorded change happened: provider effective times and the observation bracket (methodology/freshness.md).

A listing_observations row is written when a fetch shows a new value. `observed_at` is that fetch's time: when
OpenGrid knew. When it happened is a separate question, answered as well as the source allows:

    prev_observed_at     the last fetch that still showed the old value. The change happened in
                         (prev_observed_at, observed_at]. NULL on a listing's first row.
    provider_changed_at  the provider's own effective time for the new PRICE, when it publishes one:
                             aws      spot `timestamp` (DescribeSpotPriceHistory)        exact
                             nebius   spot `effectiveTime`                               exact
                             azure    `effectiveStartDate` (a date, midnight UTC)        date_only
                             gcp      `pricingInfo.effectiveTime`, latest component SKU   lower_bound
                             lium     live-stream frame `ts` (live/lium_stream.py)         exact
    changed_at_basis     exact | date_only | lower_bound (the provider's time, with that precision) or poll_window
                         (no provider time: only the bracket is known).

`lower_bound` (GCP): the price was already in force at that time; it may have taken effect earlier, because Google
reports effectiveTime clamped to the queried range. A provider time is attached only when the price itself moved
(or on a listing's first row); a row written because only stock moved gets poll_window. A provider time later than
observed_at (+ MAX_SKEW) is a scheduled change, not the current one: it is dropped (poll_window) and logged, never
trusted silently. observed_at is never replaced by a provider time.

Backfill (stored history, no network):

    .venv/bin/python -m changed_at [--provider aws] [--since 2026-10-01] [--dry-run]

    1. provider times: for each observation of AWS / Nebius / Azure / GCP with no basis yet, re-read the raw round it
       came from (normalize._latest_raw as of its observed_at), re-run the provider's pure normalize(), copy the
       listing's provider time.
    2. brackets: prev_observed_at = the newest earlier ok fetch of the endpoint(s) that produced the row (else the
       previous observation's time), for every provider. This assumes the listing was in that fetch; one that
       vanished and came back gets a bracket tighter than the truth (the live pipeline uses its last-seen time).
    3. every row still without a basis gets poll_window.

Only NULL columns are written, so it is idempotent: run it again and nothing changes. Raw payloads are kept
RAW_PAYLOAD_RETENTION_DAYS (30) and fetch metadata RAW_SNAPSHOT_RETENTION_DAYS (90), so provider times can be
recovered for the last 30 days and brackets for the last 90; older rows get poll_window with a NULL bracket.
"""

from __future__ import annotations

import argparse
import bisect
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal

log = logging.getLogger(__name__)

BASES = ("exact", "date_only", "lower_bound", "poll_window")
PROVIDER_TIMED = ("aws", "nebius", "azure", "gcp", "lium")  # lium: live-stream snapshots only
MAX_SKEW = timedelta(minutes=5)
PRICE_FIELDS = ("price_per_gpu_hour", "price_per_instance_hour")
_WARNED: set[tuple[str, str]] = set()


def parse_time(value) -> datetime | None:
    """An ISO 8601 provider time (Z or offset, optional fraction) as an aware UTC datetime; None if unreadable."""
    if not value or not isinstance(value, str):
        return None
    try:
        t = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def price_moved(listing, previous) -> bool:
    """True when a price field differs from the previous observation (None counts as a value)."""
    if previous is None:
        return True
    for f in PRICE_FIELDS:
        new, old = getattr(listing, f), getattr(previous, f)
        if (new is None) != (old is None):
            return True
        if new is not None and Decimal(str(new)) != Decimal(str(old)):
            return True
    return False


def provider_time(listing, previous) -> tuple[datetime | None, str]:
    """(provider_changed_at, changed_at_basis) for the row recording `listing` after `previous`."""
    at, basis = getattr(listing, "provider_changed_at", None), getattr(listing, "changed_at_basis", None)
    if at is None or basis is None or not price_moved(listing, previous):
        return None, "poll_window"
    if at > listing.observed_at + MAX_SKEW:
        key = (listing.provider, listing.listing_id)
        if key not in _WARNED:
            _WARNED.add(key)
            log.warning("%s %s: provider time %s is after the fetch (%s); scheduled change, not used",
                        listing.provider, listing.listing_id, at.isoformat(), listing.observed_at.isoformat())
        return None, "poll_window"
    return at, basis


def changed_at(row) -> datetime:
    """The best single time for a change: the provider's when it gave one, else the fetch that saw it."""
    return row.provider_changed_at or row.observed_at


# --------------------------------------------------------------------------
# Backfill
# --------------------------------------------------------------------------


def _provider_times(s, provider: str, since: datetime | None, dry_run: bool) -> int:
    import normalize
    from providers import PROVIDERS
    from sqlalchemy import select, update
    from tables import ListingObservation as O

    cls = PROVIDERS.get(provider)
    if cls is None:
        return 0
    q = select(O).where(O.provider == provider).order_by(O.listing_id, O.observed_at)
    if since is not None:
        q = q.where(O.observed_at >= since - timedelta(days=90))  # the previous row may be older than `since`
    by_listing: dict[str, list] = defaultdict(list)
    for row in s.scalars(q):
        by_listing[row.listing_id].append(row)
    todo: dict[datetime, list[tuple]] = defaultdict(list)  # observed_at -> [(row, previous row)]
    for rows in by_listing.values():
        for i, row in enumerate(rows):
            if row.changed_at_basis is None and (since is None or row.observed_at >= since):
                todo[row.observed_at].append((row, rows[i - 1] if i else None))
    updates, cache = [], {}
    for t in sorted(todo):
        raw = normalize._latest_raw(s, provider, as_of=t)
        key = tuple(sorted(r.id for rs in raw.values() for r in rs))
        if key not in cache:
            try:
                cache[key] = {l.listing_id: l for l in cls.normalize(raw) if l.observed_at == t}
            except Exception as exc:  # an expired or reshaped payload: leave these rows to poll_window
                log.info("%s @ %s: raw round not readable (%s)", provider, t.isoformat(), type(exc).__name__)
                cache[key] = {}
        listings = cache[key]
        for row, prev in todo[t]:
            listing = listings.get(row.listing_id)
            if listing is None or listing.provider_changed_at is None:
                continue
            at, basis = provider_time(listing, prev)
            if at is not None:
                updates.append({"p": provider, "l": row.listing_id, "t": t, "at": at, "basis": basis})
    if updates and not dry_run:
        from sqlalchemy import bindparam

        c = O.__table__.c  # Core executemany: one UPDATE per row, keyed on the primary key
        s.execute(update(O.__table__).where(c.provider == bindparam("p"), c.listing_id == bindparam("l"),
                                            c.observed_at == bindparam("t"), c.changed_at_basis.is_(None))
                  .values(provider_changed_at=bindparam("at"), changed_at_basis=bindparam("basis")), updates)
    return len(updates)


def _brackets(s, provider: str, since: datetime | None, dry_run: bool) -> int:
    from sqlalchemy import bindparam, select, text, update
    from tables import ListingObservation as O

    fetches: dict[str, list[datetime]] = defaultdict(list)   # endpoint -> ok fetch times, ascending
    at_time: dict[datetime, set[str]] = defaultdict(set)     # fetch time -> endpoints fetched then
    for ep, t in s.execute(text("SELECT endpoint, fetched_at FROM raw_snapshots WHERE provider = :p AND ok "
                                "ORDER BY fetched_at"), {"p": provider}):
        fetches[ep].append(t)
        at_time[t].add(ep)
    q = select(O.listing_id, O.observed_at, O.prev_observed_at).where(O.provider == provider) \
        .order_by(O.listing_id, O.observed_at)
    updates, last = [], {}
    for lid, t, prev_at in s.execute(q):
        before = last.get(lid)
        last[lid] = t
        if prev_at is not None or before is None or (since is not None and t < since):
            continue
        candidates = [fetches[ep][i - 1] for ep in at_time.get(t, ())
                      if (i := bisect.bisect_left(fetches[ep], t)) > 0]
        bracket = max([c for c in candidates if c >= before] or [before])
        updates.append({"p": provider, "l": lid, "t": t, "v": bracket})
    if updates and not dry_run:
        c = O.__table__.c
        s.execute(update(O.__table__).where(c.provider == bindparam("p"), c.listing_id == bindparam("l"),
                                            c.observed_at == bindparam("t"), c.prev_observed_at.is_(None))
                  .values(prev_observed_at=bindparam("v")), updates)
    return len(updates)


def backfill(provider: str | None = None, since: datetime | None = None, dry_run: bool = False) -> dict:
    """Fill prev_observed_at / provider_changed_at / changed_at_basis on stored history. Idempotent."""
    import normalize
    from sqlalchemy import func, select, update
    from tables import ListingObservation as O

    out: dict[str, dict] = {}
    with normalize.SessionLocal() as s:
        providers = [provider] if provider else sorted(s.execute(select(O.provider).distinct()).scalars())
    for p in providers:
        with normalize.SessionLocal.begin() as s:
            timed = _provider_times(s, p, since, dry_run) if p in PROVIDER_TIMED or provider else 0
            brackets = _brackets(s, p, since, dry_run)
            cond = [O.provider == p, O.changed_at_basis.is_(None)] + ([O.observed_at >= since] if since else [])
            if dry_run:  # what step 3 would label: the rows step 1 could not time
                window = s.execute(select(func.count()).select_from(O).where(*cond)).scalar() - timed
            else:
                window = s.execute(update(O).where(*cond).values(changed_at_basis="poll_window")).rowcount
        out[p] = {"provider_times": timed, "brackets": brackets, "poll_window": window}
        log.info("changed_at backfill %s: %s", p, out[p])
    return out


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m changed_at", description=__doc__.split("\n\n")[0])
    ap.add_argument("command", nargs="?", default="backfill", choices=["backfill"])
    ap.add_argument("--provider")
    ap.add_argument("--since", help="only rows observed at or after this ISO date/time (UTC)")
    ap.add_argument("--dry-run", action="store_true", help="count what would be written; write nothing")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    since = parse_time(args.since) if args.since else None
    if args.since and since is None:
        ap.error(f"--since: not an ISO date/time: {args.since!r}")
    for p, r in backfill(args.provider, since, args.dry_run).items():
        print(f"{p}: {r['provider_times']} provider times, {r['brackets']} brackets, {r['poll_window']} poll_window")


if __name__ == "__main__":
    main()
