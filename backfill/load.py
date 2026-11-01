"""Read archived sources and store what they said in archived_prices (data kind 'archived').

    run_wayback(sources, since, until, wayback, download=True) -> report
    run_aws(since)                                              -> report
    store(rows)                                                 -> rows inserted (idempotent)

A capture becomes rows only when a parser reads it AND every listing passes the same sanity bounds the
live pipeline applies to a price (positive, at most MAX_PER_GPU_HOUR). A capture no parser reads is reported with
every parser's reason and stored nowhere. Re-running is safe: (source, source_url, provider, listing_id) is unique.
"""
from __future__ import annotations

import hashlib
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import literal_column
from sqlalchemy.dialects.postgresql import insert

import normalize
from models import ComputeListing
from store.archive import ArchivedPrice

MAX_PER_GPU_HOUR = Decimal(200)


def _rows(listings: list[ComputeListing], *, source: str, source_url: str, sha256: str, parser: str,
          valid_to: datetime | None = None) -> tuple[list[dict], list[str]]:
    now = datetime.now(timezone.utc)
    rows, rejected = [], []
    for l in listings:
        p = l.price_per_gpu_hour
        if p is None or p <= 0 or p > MAX_PER_GPU_HOUR or l.gpu_count <= 0:
            rejected.append(f"{l.listing_id}: price per GPU-hour {p} outside (0, {MAX_PER_GPU_HOUR}]")
            continue
        rows.append({
            "source": source, "source_url": source_url, "source_sha256": sha256, "parser": parser[:160],
            "provider": l.provider, "listing_id": l.listing_id[:256], "sku": (l.sku or "")[:256] or None,
            "raw_gpu_name": l.raw_gpu_name[:256], "gpu": l.canonical_gpu_name, "gpu_count": l.gpu_count,
            "region": (l.region or None) and l.region[:64], "country": l.country, "price_per_gpu_hour": p,
            "price_per_instance_hour": l.price_per_instance_hour, "currency": l.currency,
            "market_type": l.market_type, "interruptible": l.interruptible, "observed_at": l.observed_at,
            "valid_to": valid_to, "imported_at": now,
        })
    # One listing id once per document: a page that prints the same shape twice keeps the first.
    seen, unique = set(), []
    for r in rows:
        if r["listing_id"] not in seen:
            seen.add(r["listing_id"])
            unique.append(r)
    return unique, rejected


def store(rows: list[dict]) -> int:
    """Insert new rows; returns how many were inserted. A row already stored is left as it was, except that an
    unmapped one (gpu null) takes the canonical GPU a re-import now gives it: canonical.py learns names after a
    page was first imported (or before a pending provider's branch is merged), and a re-run should not need a purge."""
    if not rows:
        return 0
    n = 0
    with normalize.SessionLocal.begin() as s:
        for i in range(0, len(rows), 1000):
            stmt = insert(ArchivedPrice).values(rows[i:i + 1000])
            stmt = stmt.on_conflict_do_update(
                constraint="uq_archived_doc_listing", set_={"gpu": stmt.excluded.gpu},
                where=ArchivedPrice.gpu.is_(None) & stmt.excluded.gpu.isnot(None),
            ).returning(literal_column("(xmax = 0)"))
            n += sum(1 for (inserted,) in s.execute(stmt).all() if inserted)
    return n


def run_wayback(sources, since: str, until: str, wayback, *, download: bool = True, dry_run: bool = False,
                log=print) -> list[dict]:
    report = []
    for src in sources:
        r = {"provider": src.provider, "url": src.url, "captures": 0, "read": 0, "not_downloaded": 0,
             "failed": 0, "rows": 0, "inserted": 0, "unmapped_gpus": Counter(), "parsers": Counter(),
             "failures": [], "rejected": [], "first": None, "last": None}
        try:
            caps = wayback.captures(src.url, since, until)
        except Exception as exc:
            r["error"] = f"capture list: {type(exc).__name__}: {exc}"
            report.append(r)
            log(f"{src.provider}: {r['error']}")
            continue
        r["captures"] = len(caps)
        for c in caps:
            if not download and not wayback._cached(c.raw_url, ".body").exists():
                r["not_downloaded"] += 1
                continue
            try:
                body = wayback.body(c)
            except Exception as exc:
                r["failed"] += 1
                r["failures"].append({"capture": c.archive_url, "reason": f"download: {exc}"})
                continue
            try:
                listings, parser = src.parse(body, c.captured_at)
            except Exception as exc:
                r["failed"] += 1
                r["failures"].append({"capture": c.archive_url, "reason": str(exc)[:600]})
                continue
            rows, rejected = _rows(listings, source="wayback", source_url=c.archive_url,
                                   sha256=hashlib.sha256(body).hexdigest(), parser=parser)
            r["rejected"] += [f"{c.timestamp} {x}" for x in rejected]
            r["read"] += 1
            r["parsers"][parser] += 1
            r["rows"] += len(rows)
            r["unmapped_gpus"].update(x["raw_gpu_name"] for x in rows if x["gpu"] is None)
            r["first"] = r["first"] or c.captured_at.isoformat()
            r["last"] = c.captured_at.isoformat()
            if not dry_run:
                r["inserted"] += store(rows)
        log(f"{src.provider}: {r['captures']} captures, {r['read']} read, {r['failed']} failed, "
            f"{r['not_downloaded']} not downloaded, {r['rows']} rows ({r['inserted']} new)")
        report.append(r)
    return report


def run_aws(since: datetime, *, dry_run: bool = False, log=print) -> dict:
    from backfill import aws_price_list as A

    r = {"provider": "aws", "url": A.BASE + A.VERSION_INDEX, "captures": 0, "read": 0, "failed": 0, "rows": 0,
         "inserted": 0, "failures": [], "rejected": [], "first": None, "last": None,
         "parsers": Counter(), "unmapped_gpus": Counter()}
    for v in A.versions(since):
        r["captures"] += 1
        try:
            raw = A.gpu_rows(v)
            listings, problems = A.listings(v, raw)
        except Exception as exc:
            r["failed"] += 1
            r["failures"].append({"capture": v.csv_url, "reason": f"{type(exc).__name__}: {exc}"})
            continue
        sha = hashlib.sha256(repr(sorted(map(sorted, (x.items() for x in raw)))).encode()).hexdigest()
        rows, rejected = _rows(listings, source="aws_price_list", source_url=v.csv_url, sha256=sha,
                               parser=A.PARSER, valid_to=v.end)
        r["rejected"] += problems + rejected
        r["read"] += 1
        r["parsers"][A.PARSER] += 1
        r["rows"] += len(rows)
        r["unmapped_gpus"].update(x["raw_gpu_name"] for x in rows if x["gpu"] is None)
        r["first"] = r["first"] or v.begin.isoformat()
        r["last"] = v.begin.isoformat()
        if not dry_run:
            r["inserted"] += store(rows)
    log(f"aws: {r['captures']} price-list versions, {r['read']} read, {r['rows']} rows ({r['inserted']} new)")
    return r
