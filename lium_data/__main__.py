"""python -m lium_data status | poll-public | poll-partner | backfill [--since YYYY-MM-DD] [--until YYYY-MM-DD]
                                                                       [--force] | reparse | reparse-stats
                      | poll-grafana | grafana-backfill [--since YYYY-MM-DD] [--until YYYY-MM-DD]
                                                         [--only rentals|series] [--force] | grafana-reparse

    status          every feed's newest answer and the row counts
    poll-public     one run of the lium_public job (keyless feeds + published throughput)
    poll-partner    one run of the lium_partner job (needs LIUM_PARTNER_API_KEY)
    backfill        data-partner history from Lium's nightly dumps, newest day first, back to --since (default:
                    until 3 empty days in a row); days already stored are skipped unless --force. Resumable. Run it
                    detached on the server, e.g.
                    nohup .venv/bin/python -m lium_data backfill --since 2026-01-01 > /tmp/lium-backfill.log 2>&1 &
    reparse         re-parse every stored partner CSV body (after a parser change)
    reparse-stats   re-parse every stored Lium stats snapshot (raw_snapshots) into the throughput tables
    poll-grafana    one run of the lium_grafana job (Lium's public Grafana: rental starts, hourly spend and fleet)
    grafana-backfill  that history, newest first, back to --since (default: until 2 empty chunks in a row); a
                    chunk read after it closed is skipped unless --force. Resumable. Run it detached on the server:
                    nohup python -m lium_data grafana-backfill --since 2025-10-01 > /tmp/lium-grafana.log 2>&1 &
    grafana-reparse re-parse every stored Grafana answer (after a parser change)
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


def main(argv: list[str]) -> int:
    cmds = ("status", "poll-public", "poll-partner", "backfill", "reparse", "reparse-stats", "poll-grafana",
            "grafana-backfill", "grafana-reparse")
    if not argv or argv[0] not in cmds:
        print(__doc__)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    import tables  # noqa: F401  (every model registered)
    from lium_data import grafana, partner, public, reads

    cmd = argv[0]
    if cmd == "status":
        out = reads.status()
    elif cmd == "poll-public":
        out = asyncio.run(public.poll())
    elif cmd == "poll-partner":
        out = asyncio.run(partner.poll())
    elif cmd == "backfill":
        since, until = _one(argv, "--since"), _one(argv, "--until")
        if not partner.api_key():
            print("LIUM_PARTNER_API_KEY is not set", file=sys.stderr)
            return 2
        out = asyncio.run(partner.backfill(date.fromisoformat(since) if since else None,
                                           date.fromisoformat(until) if until else None, force="--force" in argv))
    elif cmd == "reparse":
        out = partner.reparse()
    elif cmd == "poll-grafana":
        out = asyncio.run(grafana.poll())
    elif cmd == "grafana-backfill":
        since, until, only = _one(argv, "--since"), _one(argv, "--until"), _one(argv, "--only")
        if only not in (None, "rentals", "series"):
            print("--only takes rentals or series", file=sys.stderr)
            return 2
        out = asyncio.run(grafana.backfill(date.fromisoformat(since) if since else None,
                                           date.fromisoformat(until) if until else None, only=only,
                                           force="--force" in argv))
    elif cmd == "grafana-reparse":
        out = grafana.reparse()
    else:
        out = public.reparse_stats()
    print(json.dumps(out, indent=1, default=str))
    return 0 if not isinstance(out, dict) or out.get("state") in (None, "ok", "off") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
