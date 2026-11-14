"""AWS EC2 spot price history from before OpenGrid recorded it (`python -m backfill aws-spot [--days 90] [--dry-run]`;
methodology/archived-history.md). AWS's DescribeSpotPriceHistory keeps 90 days, and they age out daily, so this is
a one-off read of whatever is still there.

Per spot region (providers/aws.SPOT_REGIONS) and per whole UTC day D, one DescribeSpotPriceHistory window
[D 00:00, D+1 00:00], every page, through the live poller's own reader (AwsProvider.spot_history: same request,
signing, parsing, GPU types and Linux/UNIX filter). Each window holds, per GPU type and AZ, the price in force at
00:00 plus every change that day with AWS's exact time. Two things are stored from it:

    raw          the window's GPU rows verbatim (XML -> JSON, as the live poller stores them) and the request's
                 parameters (never the key) in catalog_snapshots,
                 source "aws_spot_history", name = region, version = the day. Every change with its exact time
                 survives AWS's 90-day retention. Nothing reads it into prices.
    archived     one archived_prices row per (type, AZ) and day: the price AWS states was in force at D 00:00 UTC,
                 read by providers.aws.spot_listings (so listing ids, GPU counts and per-GPU prices are the live
                 spot listings' own: "<type>:<az>:spot", market spot, interruptible). source aws_spot_history,
                 basis provider_api_history, source_url = the window's query, sha256 = of the stored raw body, the
                 note names AWS's time for that price and the day's changes (count, low, high).

Why archived and not observed: OpenGrid did not see these prices when they applied; it read AWS's account of them
afterwards. Archived rows never enter percentiles, indices, events, routing or alerts, and the daily chart uses
them only before the GPU's first observed day.

Never double counted with the live change log: a region whose spot prices OpenGrid already records (its first
listing_observations row, `live_starts()`) is backfilled only for days that begin before that. Days already stored
raw are re-read from catalog_snapshots, not fetched again; archived rows are unique per (source, source_url,
provider, listing_id), so a re-run adds nothing.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

import changed_at
import normalize
from backfill import load
from providers import aws
from providers.base import gather_limited, to_decimal
from store.archive import CatalogSnapshot

SOURCE = "aws_spot_history"
BASIS = "provider_api_history"
PARSER = "backfill.aws_spot (providers.aws.spot_listings, price in force at 00:00 UTC)"
LICENCE = "AWS EC2 API (DescribeSpotPriceHistory)"
MAX_DAYS = 90   # AWS: "You can view the Spot Price history for the last 90 days"
NOTE = ("AWS DescribeSpotPriceHistory, one UTC day, GPU instance types, Linux/UNIX: the price in force at 00:00 plus "
        "every change that day with AWS's time (GPU rows verbatim, XML -> JSON). Read after the fact by "
        "python -m backfill aws-spot; AWS keeps 90 days.")

# The first live spot row per region: listing ids are "<type>:<az>:spot" and an AZ is its region plus a letter.
# (\: is a literal colon for SQLAlchemy's text(), not a bind parameter.)
_LIVE = text(r"""
    SELECT substring(listing_id from '\:([a-z]+(?:-[a-z]+)+-\d+)[a-z]\:spot$') AS region, min(observed_at) AS first
    FROM listing_observations
    WHERE provider = 'aws' AND listing_id LIKE '%\:spot'
    GROUP BY 1
""")


def live_starts() -> dict[str, datetime]:
    """Per spot region, when OpenGrid first recorded its spot prices live (no entry = never)."""
    with normalize.SessionLocal() as s:
        return {r.region: r.first for r in s.execute(_LIVE) if r.region}


def window_url(region: str, day: date) -> str:
    """The checkable query behind one day: re-running it (within 90 days) returns the same rows."""
    start = datetime.combine(day, time(), timezone.utc)
    return aws.ec2_endpoint(region, "DescribeSpotPriceHistory", ProductDescription=aws.SPOT_PRODUCT,
                            StartTime=f"{start:%Y-%m-%dT%H:%M:%SZ}",
                            EndTime=f"{start + timedelta(days=1):%Y-%m-%dT%H:%M:%SZ}")


def days_to_read(now: datetime, days: int, live_start: datetime | None) -> list[date]:
    """Whole UTC days whose window AWS still holds (starting at or after now - days) and that have ended, and, for
    a region already recorded live, that begin before its first live row."""
    first = (now - timedelta(days=days)).date() + timedelta(days=1)
    out, d = [], first
    while datetime.combine(d + timedelta(days=1), time(), timezone.utc) <= now:
        if live_start is None or datetime.combine(d, time(), timezone.utc) < live_start:
            out.append(d)
        d += timedelta(days=1)
    return out


def body_bytes(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def _changes_note(items: list[dict], t0: datetime) -> dict[tuple[str, str], str]:
    """Per (type, AZ): the day's changes after 00:00, as "n changes that day (low, high per instance-hour)"."""
    per: dict[tuple[str, str], list[Decimal]] = {}
    for i in items:
        t, p = changed_at.parse_time(i.get("timestamp")), to_decimal(i.get("spotPrice"))
        if t is not None and t > t0 and p is not None:
            per.setdefault((i.get("instanceType"), i.get("availabilityZone")), []).append(p)
    return {k: f"{len(v)} change{'s' if len(v) != 1 else ''} later that day (instance-hour low ${min(v)}, high "
               f"${max(v)})" for k, v in per.items()}


def day_rows(region: str, day: date, payload: dict, sha256: str) -> list[dict]:
    """archived_prices rows for one region-day: per (type, AZ) the price in force at 00:00 UTC, read by the live
    spot normalizer from the rows dated at or before 00:00 (AWS's own "effective price as of the start time")."""
    t0 = datetime.combine(day, time(), timezone.utc)
    items = [i for i in payload.get("spotPriceHistorySet") or [] if isinstance(i, dict)]
    at_start = [i for i in items if (t := changed_at.parse_time(i.get("timestamp"))) is not None and t <= t0]
    snap = SimpleNamespace(payload={**payload, "spotPriceHistorySet": at_start}, fetched_at=t0,
                           endpoint=aws.ec2_endpoint(region, "DescribeSpotPriceHistory"))
    listings = aws.spot_listings({snap.endpoint: [snap]})
    later = _changes_note(items, t0)
    read = datetime.now(timezone.utc)

    def note(l) -> str:
        set_at = f"{l.provider_changed_at:%Y-%m-%dT%H:%M:%SZ}" if l.provider_changed_at else "?"
        extra = later.get((l.sku, l.region), "no change later that day")
        return (f"AWS DescribeSpotPriceHistory {region}, {aws.SPOT_PRODUCT}: price in force at {t0:%Y-%m-%d} 00:00 "
                f"UTC, set by AWS at {set_at}; {extra}; read {read:%Y-%m-%d}")

    rows, _rejected = load._rows(listings, source=SOURCE, source_url=window_url(region, day), sha256=sha256,
                                 parser=PARSER, basis=BASIS, licence=LICENCE, note=note)
    return rows


def _stored_raw(region: str, day: date) -> CatalogSnapshot | None:
    with normalize.SessionLocal() as s:
        return s.execute(select(CatalogSnapshot).where(
            CatalogSnapshot.source == SOURCE, CatalogSnapshot.name == region,
            CatalogSnapshot.version == day.isoformat())).scalar_one_or_none()


def _store_raw(region: str, day: date, body: bytes, sha256: str, fetched_at: datetime) -> int:
    row = {"source": SOURCE, "name": region, "version": day.isoformat(), "url": window_url(region, day),
           "fetched_at": fetched_at, "published_at": None, "sha256": sha256, "size": len(body),
           "content_type": "application/json", "licence": LICENCE, "note": NOTE, "body": body}
    with normalize.SessionLocal.begin() as s:
        res = s.execute(insert(CatalogSnapshot).values(row).on_conflict_do_nothing(
            constraint="uq_catalog_snapshot_version"))
        return res.rowcount or 0


async def _region(provider: aws.AwsProvider, region: str, wanted: list[date], *, dry_run: bool, log) -> dict:
    r = {"region": region, "days": len(wanted), "fetched": 0, "from_raw_store": 0, "failed": 0, "raw_stored": 0,
         "rows": 0, "inserted": 0, "by_gpu": Counter(), "first": None, "last": None, "failures": []}
    for day in wanted:
        stored = None if dry_run else _stored_raw(region, day)
        if stored is not None:
            body, sha = stored.body, stored.sha256
            r["from_raw_store"] += 1
        else:
            t0 = datetime.combine(day, time(), timezone.utc)
            raw = await provider.spot_history(region, t0, t0 + timedelta(days=1))
            if not raw.ok:
                r["failed"] += 1
                r["failures"].append(f"{day}: {raw.error}")
                continue
            r["fetched"] += 1
            body = body_bytes({**raw.payload, "request": raw.request})   # the window and filters asked for
            sha = hashlib.sha256(body).hexdigest()
            if not dry_run:
                r["raw_stored"] += _store_raw(region, day, body, sha, raw.fetched_at)
        rows = day_rows(region, day, json.loads(body), sha)
        r["rows"] += len(rows)
        r["by_gpu"].update(x["gpu"] or f"unmapped:{x['raw_gpu_name']}" for x in rows)
        if rows:
            r["first"] = r["first"] or day.isoformat()
            r["last"] = day.isoformat()
        if not dry_run:
            r["inserted"] += load.store(rows)
    log(f"aws-spot {region}: {r['days']} days ({r['fetched']} fetched, {r['from_raw_store']} from the raw store, "
        f"{r['failed']} failed), {r['rows']} rows ({r['inserted']} new), {r['first']} .. {r['last']}")
    return r


def run(days: int = MAX_DAYS, *, dry_run: bool = False, provider: aws.AwsProvider | None = None,
        now: datetime | None = None, live: dict[str, datetime] | None = None, regions=None, log=print) -> dict:
    """Backfill every spot region (or `regions`). `live` overrides live_starts() (tests); a dry run that cannot
    reach the database reads every day and says so."""
    from config import settings

    if days > MAX_DAYS:
        log(f"aws-spot: AWS keeps {MAX_DAYS} days of spot history; reading {MAX_DAYS}, not {days}")
        days = MAX_DAYS
    now = now or datetime.now(timezone.utc)
    if provider is None:
        if not (settings.aws_access_key_id and settings.aws_secret_access_key):
            raise SystemExit("aws-spot: AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY are required "
                             "(the read-only key the live poller uses)")
        provider = aws.AwsProvider(None, settings.aws_access_key_id, settings.aws_secret_access_key)
    report = {"source": SOURCE, "days": days, "now": now.isoformat(), "dry_run": dry_run, "live_start": {},
              "regions": []}
    if live is None:
        try:
            live = live_starts()
        except Exception as exc:  # noqa: BLE001 - only a dry run may go on without knowing where live data begins
            if not dry_run:
                raise
            log(f"aws-spot: live start unknown ({type(exc).__name__}); dry run reads every day")
            report["live_start_error"] = f"{type(exc).__name__}: {exc}"[:300]
            live = {}
    report["live_start"] = {k: v.isoformat() for k, v in sorted(live.items())}
    todo = {r: days_to_read(now, days, live.get(r)) for r in (regions or aws.SPOT_REGIONS)}

    async def go():
        return await gather_limited([lambda r=r: _region(provider, r, todo[r], dry_run=dry_run, log=log)
                                     for r in todo], limit=aws.SPOT_CONCURRENCY)

    report["regions"] = asyncio.run(go())
    by_gpu, firsts = Counter(), [x["first"] for x in report["regions"] if x["first"]]
    for x in report["regions"]:
        by_gpu.update(x["by_gpu"])
    report.update(rows=sum(x["rows"] for x in report["regions"]),
                  inserted=sum(x["inserted"] for x in report["regions"]),
                  failed=sum(x["failed"] for x in report["regions"]),
                  earliest=min(firsts) if firsts else None, by_gpu=dict(by_gpu.most_common()))
    log(f"aws-spot: {report['rows']} rows ({report['inserted']} new), {report['failed']} region-days failed, "
        f"earliest {report['earliest']}")
    return report
