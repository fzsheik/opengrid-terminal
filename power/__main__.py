"""python -m power backfill|forward|status|sources [--since YYYY-MM[-DD]] [--until YYYY-MM-DD]
                                                 [--source caiso ...] [--dataset caiso.da_lmp ...] [--dry-run]
       python -m power flaring [--source worldbank_ggfd|ndic] [--since YYYY-MM] [--refresh] [--dry-run]
       python -m power nodal backfill|forward [--since ..] [--until ..] [--source spp] [--dataset spp.rt_nodal]
       python -m power nodal status
       python -m power nodal cheap [--days 90] [--market ercot|spp] [--series rt|da] [--zone ..] [--type ..]

    backfill   every missing market day from --since (default: each source's earliest) to its latest complete
               day; idempotent and resumable (stored days are skipped). Run it detached on the server, e.g.
               nohup .venv/bin/python -m power backfill --since 2021-01 > /tmp/power-backfill.log 2>&1 &
    forward    what the power_forward job does (the last stored days, with an overlap), then the cross-analysis
    status     per dataset: series, series-days, depth, raw bytes kept; table sizes
    sources    the registry: every source, its terms verdict and what it needs (and the datasets declined)
    flaring    gas flaring (power/flaring.py): the World Bank CC BY files once, North Dakota's monthly reports from
               --since (default 2015-05) to the latest posted; idempotent and resumable; --refresh re-reads all
    sources    the registry: every source, its terms verdict and what it needs
    nodal      per-node daily summaries (power/nodal.py): backfill (default: the last 366 days the source still
               holds; one request a day, ERCOT RT 96), forward (what the power_forward job runs after the hubs),
               status (nodes, rows, depth, table bytes), cheap (the stranded-power ranking)

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


def nodal_main(argv: list[str]) -> int:
    from power import nodal, registry

    cmd = argv[0] if argv else ""
    if cmd in ("backfill", "forward"):
        sources = _multi(argv, "--source") or None
        known = {d.source for d in registry.nodal_datasets()}
        bad = [s for s in sources or [] if s not in known]
        if bad:
            print(f"no nodal datasets for {bad}; known: {sorted(known)}", file=sys.stderr)
            return 2
        since, until = _one(argv, "--since"), _one(argv, "--until")
        out = asyncio.run(nodal.run(cmd, sources=sources, since=since and _date(since), until=until and _date(until),
                                    dry_run="--dry-run" in argv, datasets=_multi(argv, "--dataset") or None))
        print(json.dumps(out, indent=1, default=str))
    elif cmd == "cheap":
        rows = nodal.cheap_nodes(int(_one(argv, "--days") or 90), _one(argv, "--market"),
                                 series=_one(argv, "--series") or "rt", zone=_one(argv, "--zone"),
                                 node_type=_one(argv, "--type"), min_days=int(_one(argv, "--min-days") or 7),
                                 limit=int(_one(argv, "--limit") or 25))
        for r in rows:
            print(f'{r["market"]:5} {r["node"]:24} {r["node_type"] or "-":19} {r["zone"] or "-":10} '
                  f'<$20 {r["share_below_20"]:6.1%}  <$0 {r["share_below_0"]:6.1%}  {r["hours_below_0"]:7.1f} h<0  '
                  f'median {r["median"]:7.2f}  days {r["days"]}')
        return 0
    elif cmd != "status":
        print(__doc__)
        return 2
    st = nodal.status()
    for d in st["datasets"]:
        print(f'{d["dataset"]:16} {d["visibility"]:8} nodes {d["nodes"]:5} rows {d["rows"]:8} days {d["days"]:4} '
              f'{d["earliest"] or "-"} .. {d["latest"] or "-"} (catalogue {d["catalogue_nodes"]})')
    print(f'nodal tables total {st["total_bytes"] / 1e6:.1f} MB: '
          + ", ".join(f"{k} {v / 1e6:.1f}" for k, v in sorted(st["table_bytes"].items())))
    return 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("backfill", "forward", "status", "sources", "flaring", "nodal"):
        print(__doc__)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if argv[0] == "nodal":
        import tables  # noqa: F401  (every model registered)
        return nodal_main(argv[1:])
    import tables  # noqa: F401  (every model registered)
    from power import registry, runner

    cmd = argv[0]
    if cmd == "sources":
        from power import flaring

        print(json.dumps({"sources": registry.as_list(), "flaring": flaring.verdicts(),
                          "declined": registry.declined_list()}, indent=1, default=str))
        return 0
    if cmd == "flaring":
        from power import flaring

        bad = [s for s in _multi(argv, "--source") if s not in flaring.SOURCES]
        if bad:
            print(f"unknown flaring source(s) {bad}; known: {sorted(flaring.SOURCES)}", file=sys.stderr)
            return 2
        since = _one(argv, "--since")
        out = asyncio.run(flaring.run(_multi(argv, "--source") or None, since=since and _date(since),
                                      refresh="--refresh" in argv, dry_run="--dry-run" in argv))
        print(json.dumps(out, indent=1, default=str))
        print(json.dumps(flaring.status(), indent=1, default=str))
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
            from config import settings

            if settings.power_nodal and not _multi(argv, "--dataset"):
                from power import nodal

                out["nodal"] = asyncio.run(nodal.run("forward", sources=sources))   # as the job: after the hubs
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
