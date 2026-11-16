"""python -m power.sites sync|status|sources [--source eia860m,eia923,lbnl_queues,ercot_gis] [--since YYYY-MM]
                                           [--dry-run]

    sync      fetch what is not stored yet: the first run is the backfill, later runs refresh. Idempotent and
              resumable (an interrupted run continues from the last month / year / edition stored). Run it detached
              on the server, e.g. nohup .venv/bin/python -m power.sites sync > /tmp/power-sites.log 2>&1 &
    status    rows and bytes per table, generators per status, queue editions, fetches per dataset
    sources   the registry: every source, its terms verdict and what it needs

--since limits the first run (EIA-860M months, EIA-923 years, ERCOT GIS editions); --dry-run plans and asks only
the cheap listing / metadata requests.
"""

import asyncio
import json
import logging
import sys
from datetime import date


def _one(argv: list[str], flag: str) -> str | None:
    for i, a in enumerate(argv):
        if a == flag and i + 1 < len(argv):
            return argv[i + 1]
    return None


def _date(s: str) -> date:
    parts = [int(x) for x in s.split("-")]
    return date(parts[0], parts[1] if len(parts) > 1 else 1, parts[2] if len(parts) > 2 else 1)


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("sync", "status", "sources"):
        print(__doc__)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    import tables  # noqa: F401  (every model registered)
    from power.sites import registry, runner

    if argv[0] == "sources":
        print(json.dumps(registry.as_list(), indent=1, default=str))
        return 0
    if argv[0] == "sync":
        src = _one(argv, "--source")
        names = [s.strip() for s in src.split(",") if s.strip()] if src else None
        known = {s.key for s in registry.SOURCES}
        if names and set(names) - known:
            print(f"unknown source(s) {sorted(set(names) - known)}; known: {sorted(known)}", file=sys.stderr)
            return 2
        since = _one(argv, "--since")
        out = asyncio.run(runner.sync(names, since and _date(since), "--dry-run" in argv))
        print(json.dumps(out, indent=1, default=str))
        for nc in out["not_configured"]:
            print(f"not configured: {nc['source']} needs {nc['requires']} ({nc['signup']})", file=sys.stderr)
    st = runner.status()
    for t, v in st["tables"].items():
        print(f"{t:26} rows {v['rows']:9} {v['bytes'] / 1e6:8.2f} MB")
    for g in st["generators"]:
        print(f"  generators {g['status_group']:15} {g['n']:7} {float(g['mw']):12.1f} MW  {g['first']} .. {g['last']}")
    for q in st["queue_editions"]:
        print(f"  queue {q['source']:12} {q['edition']} {q['visibility']:8} {q['n']:6} projects, "
              f"{float(q['active_mw']):10.1f} MW active")
    print(f"site tables total {st['total_bytes'] / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
