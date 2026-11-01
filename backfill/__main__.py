"""Backfill archived price history (methodology/archived-history.md). Uses DATABASE_URL like the server.

    python -m backfill survey                          captures per source, nothing downloaded or stored
    python -m backfill import [--since 2025-10-21] [--until 2026-10-21] [--provider coreweave ...]
    python -m backfill import --since 2016-01 --provider azure --no-aws     everything archived since Jan 2016

--since / --until take YYYY, YYYY-MM or YYYY-MM-DD (--since: the period's first day; --until: its last). The default
window is the last 365 days; parsers know designs back to 2016 (Azure), so a deeper --since is safe.
                              [--no-download] [--dry-run] [--no-aws] [--report report.json]

Downloads go one at a time through backfill/wayback.py (polite, cached under OPENGRID_ARCHIVE_CACHE,
default ~/.cache/opengrid/wayback), so the first full import takes about an hour and a re-run takes seconds.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

from backfill import load
from backfill.sources import sources
from backfill.wayback import Wayback


def _day(s: str, *, end: bool = False) -> str:
    """YYYY / YYYY-MM / YYYY-MM-DD (dashes optional) -> YYYYMMDD: the first day of the period, or with end=True
    its last day."""
    import calendar

    d = s.replace("-", "")
    if not d.isdigit() or len(d) not in (4, 6, 8):
        raise SystemExit(f"backfill: date {s!r} is not YYYY, YYYY-MM or YYYY-MM-DD")
    if len(d) == 8:
        datetime.strptime(d, "%Y%m%d")
        return d
    y, m = int(d[:4]), int(d[4:6]) if len(d) == 6 else (12 if end else 1)
    if not 2000 <= y <= 2100 or not 1 <= m <= 12:
        raise SystemExit(f"backfill: date {s!r} has month {m}")
    return f"{y:04d}{m:02d}{calendar.monthrange(y, m)[1] if end else 1:02d}"


def main(argv=None) -> int:
    today = datetime.now(timezone.utc).date()
    ap = argparse.ArgumentParser(prog="python -m backfill", description="archived GPU price history")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("survey", "import"):
        p = sub.add_parser(name)
        p.add_argument("--since", default=str(today - timedelta(days=365)))
        p.add_argument("--until", default=str(today))
        p.add_argument("--provider", action="append", help="only these providers (repeatable)")
    p.add_argument("--no-download", action="store_true", help="read only captures already in the cache")
    p.add_argument("--dry-run", action="store_true", help="parse and report, store nothing")
    p.add_argument("--no-aws", action="store_true", help="skip AWS's price-list archive")
    p.add_argument("--report", help="write the full JSON report (failures, unmapped GPUs) here")
    a = ap.parse_args(argv)

    since, until = _day(a.since), _day(a.until, end=True)
    srcs = [s for s in sources() if not a.provider or s.provider in a.provider]
    w = Wayback(log=lambda m: print(m, file=sys.stderr))
    if a.cmd == "survey":
        for s in srcs:
            caps = w.captures(s.url, since, until)
            print(f"{s.provider:14} {len(caps):4} captures  {s.url}"
                  + (f"  ({caps[0].timestamp[:8]} .. {caps[-1].timestamp[:8]})" if caps else ""))
        return 0

    report = load.run_wayback(srcs, since, until, w, download=not a.no_download, dry_run=a.dry_run)
    if not a.no_aws and (not a.provider or "aws" in a.provider):
        report.append(load.run_aws(datetime.strptime(since, "%Y%m%d").replace(tzinfo=timezone.utc),
                                   dry_run=a.dry_run))
    if a.report:
        with open(a.report, "w") as f:
            json.dump(report, f, indent=1, default=lambda o: dict(o) if isinstance(o, Counter) else str(o))
    unmapped = Counter()
    for r in report:
        unmapped.update(r["unmapped_gpus"])
    if unmapped:
        print("unmapped GPU names (stored with gpu = null, not charted):", dict(unmapped.most_common(20)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
