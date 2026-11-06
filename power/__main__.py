"""python -m power backfill|forward|status|sources [--since YYYY-MM[-DD]] [--until YYYY-MM-DD]
                                                 [--source caiso ...] [--dataset caiso.da_lmp ...] [--dry-run]

    backfill   every missing market day from --since (default: each source's earliest) to its latest complete
               day; idempotent and resumable (stored days are skipped). Run it detached on the server, e.g.
               nohup .venv/bin/python -m power backfill --since 2021-01 > /tmp/power-backfill.log 2>&1 &
    forward    what the power_forward job does (the last stored days, with an overlap), then the cross-analysis
    status     per dataset: series, series-days, depth, raw bytes kept; table sizes
    sources    the registry: every source, its terms verdict and what it needs

--source may repeat (or be comma-separated); a named source runs even when POWER_SOURCES leaves it out, never
without its keys. Progress is logged one line per 10 requests.
"""

import asyncio
import json
import logging
import sys
from datetime import date


def _date(s: str) -> date:
    parts = [int(x) for x in s.split("-")]
    return date(parts[0], parts[1] if len(parts) > 1 else 1, parts[2] if len(parts) > 2 else 1)


def _multi(argv: list[str], flag: str) -> list[str]:
    out = []
    for i, a in enumerate(argv):
        if a == flag and i + 1 < len(argv):
            out += [x.strip() for x in argv[i + 1].split(",") if x.strip()]
    return out


def _one(argv: list[str], flag: str) -> str | None:
    v = _multi(argv, flag)
    return v[-1] if v else None


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("backfill", "forward", "status", "sources"):
        print(__doc__)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    import tables  # noqa: F401  (every model registered)
    from power import registry, runner

    cmd = argv[0]
    if cmd == "sources":
        print(json.dumps(registry.as_list(), indent=1, default=str))
        return 0
    if cmd in ("backfill", "forward"):
        sources = _multi(argv, "--source") or None
        known = {m.SOURCE for m in registry.modules()}
        bad = [s for s in sources or [] if s not in known]
        if bad:
            print(f"unknown source(s) {bad}; known: {sorted(known)}", file=sys.stderr)
            return 2
        since, until = _one(argv, "--since"), _one(argv, "--until")
        out = asyncio.run(runner.run(cmd, sources=sources, since=since and _date(since),
                                     until=until and _date(until), dry_run="--dry-run" in argv,
                                     datasets=_multi(argv, "--dataset") or None))
        if cmd == "forward" and "--dry-run" not in argv:
            try:
                from power import cross

                out["cross"] = cross.refresh()
            except ImportError:
                pass
        print(json.dumps(out, indent=1, default=str))
        for nc in out["not_configured"]:
            print(f"not configured: {nc['source']} needs {nc['requires']} ({nc['signup']})", file=sys.stderr)
    st = runner.status()
    for d in st["datasets"]:
        print(f'{d["dataset"]:24} {d["visibility"]:8} {"on " if d["selected"] else "off"} series {d["series"]:4} '
              f'days {d["series_days"]:7} ({d["complete"]} complete) {d["earliest"] or "-"} .. {d["latest"] or "-"} '
              f'vals {d["vals_bytes"] / 1e6:8.2f} MB raw kept {d["raw_bytes_kept"] / 1e6:7.2f} MB '
              f'fetches {d["fetches"]} failed {d["failed"]} empty {d["empty"]}')
    print(f'power tables total {st["total_bytes"] / 1e6:.1f} MB: '
          + ", ".join(f"{k} {v / 1e6:.1f}" for k, v in sorted(st["table_bytes"].items())))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
