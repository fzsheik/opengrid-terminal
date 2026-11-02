"""python -m context equities backfill|forward|status

    backfill   every ticker: daily bars from 2016-01-01 (split, raw, all) and hourly bars for 5 years; idempotent
    forward    what the hourly context_equities job does (from the latest stored bar, with an overlap)
    status     rows per ticker, earliest and latest session

INTERNAL research data (methodology/internal/equities.md). Needs ALPACA_DATA_KEY_ID and ALPACA_DATA_SECRET_KEY.
"""

import asyncio
import json
import sys


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[0] != "equities" or argv[1] not in ("backfill", "forward", "status"):
        print(__doc__)
        return 2
    import tables  # noqa: F401  (every model registered)
    from context import equities

    cmd = argv[1]
    if cmd in ("backfill", "forward"):
        if not equities.enabled():
            print("ALPACA_DATA_KEY_ID / ALPACA_DATA_SECRET_KEY not set", file=sys.stderr)
            return 1
        no_hourly = "--no-hourly" in argv
        out = asyncio.run(equities.refresh(full=cmd == "backfill", hourly=not no_hourly))
        print(json.dumps(out, default=str, indent=1))
    for r in equities.coverage():
        d, h = r["daily"]["split"], r["hourly"]["split"]
        print(f'{r["symbol"]:6} daily {d["sessions"]:5} {d.get("earliest") or "-"} .. {d.get("latest") or "-"}   '
              f'raw {r["daily"]["raw"]["sessions"]:5}  all {r["daily"]["all"]["sessions"]:5}   '
              f'hourly {h["bars"]:6} {h.get("earliest") or "-"}')
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
