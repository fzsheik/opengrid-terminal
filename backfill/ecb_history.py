"""Store the ECB's full euro reference-rate history, so archived EUR price pages can be converted.

    python -m backfill.ecb_history            fetch eurofxref-hist.xml (every TARGET day since 1999), store it raw in
                                              fx_snapshots and every rate in fx_rates (fx.save: raw first, revisions
                                              logged)

The live FX job keeps only the daily file (and the 90-day file once), so fx_rates starts about 90 days before OpenGrid
first ran. Archived EUR pages (Scaleway, Hetzner, Gcore: backfill/parsers/) are converted exactly as their live pollers
convert: fx.usd_rate("EUR", capture time), the newest ECB rate already published at the capture, fail closed when it is
more than fx.MAX_AGE_DAYS old. Without this history every capture older than the stored rates stores nothing (fail
closed), it is never converted at a guess. The file is the ECB's own feed, free and unauthenticated; one request.
"""
from __future__ import annotations

import asyncio
import json
import sys

import fx

HIST_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.xml"


def run() -> dict:
    return fx.save(asyncio.run(fx.fetch(HIST_URL)))


if __name__ == "__main__":
    r = run()
    print(json.dumps(r, default=str))
    sys.exit(0 if r["ok"] else 1)
