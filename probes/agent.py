"""Standalone probe agent: run it in another region and it adds a vantage point.

    OPENGRID_PROBE_KEY=opg_live_... python -m probes.agent --server https://your-opengrid --location eu-west \
        [--agent-id eu-west-1]

The key needs the `data:read` and `probes:write` scopes. The agent fetches the target list from the server (so the
server owns what is probed), probes whatever is due, and posts raw results to /v1/probes/ingest. Results that cannot be
posted are kept (up to MAX_BUFFER, oldest dropped) and retried, so a short server outage loses nothing. The server
refuses results older than 6 hours.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import time

import httpx

from probes import measure, targets

log = logging.getLogger("probes.agent")
MAX_BUFFER = 5000
BATCH = 400
TARGET_REFRESH_S = 600
TICK_S = 10
LOCATION_RE = re.compile(r"^[a-z0-9-]{2,32}$")


def parse_targets(rows: list[dict]) -> list[targets.Target]:
    return [targets.Target(id=r["id"], provider=r["provider"], name=r["name"], url=r["url"], kind=r.get("kind", "api"),
                           accept=tuple(r.get("accept") or (200,)), region=r.get("region"),
                           interval_s=int(r.get("interval_s") or 300), note=r.get("note", "")) for r in rows]


def wire(result: dict) -> dict:
    return {**result, "observed_at": result["observed_at"].isoformat()}


async def run(server: str, key: str, location: str, agent_id: str, once: bool = False, transport=None) -> None:
    headers = {"Authorization": f"Bearer {key}"}
    pool: list[targets.Target] = list(targets.TARGETS)
    refreshed = 0.0
    last: dict[str, float] = {}
    buffer: list[dict] = []
    async with httpx.AsyncClient(base_url=server.rstrip("/"), headers=headers, timeout=20.0, transport=transport) as api, measure.client() as c:
        while True:
            now = time.time()
            if now - refreshed >= TARGET_REFRESH_S:
                try:
                    r = await api.get("/v1/probes/targets")
                    r.raise_for_status()
                    pool = parse_targets(r.json()["data"]) or pool
                except Exception as exc:  # noqa: BLE001 - keep the last known list
                    log.warning("target list refresh failed (%s); using %d known targets", exc, len(pool))
                refreshed = now
            batch = [t for t in pool if last.get(t.id) is None or now - last[t.id] >= t.interval_s]
            if batch:
                sem = asyncio.Semaphore(8)

                async def one(t):
                    async with sem:
                        return await measure.measure(c, t, location)
                for t, res in zip(batch, await asyncio.gather(*(one(t) for t in batch))):
                    last[t.id] = now
                    buffer.append(res)
                buffer = buffer[-MAX_BUFFER:]
            while buffer:
                chunk = buffer[:BATCH]
                try:
                    r = await api.post("/v1/probes/ingest", json={"agent_id": agent_id, "location": location,
                                                                  "results": [wire(x) for x in chunk]})
                    r.raise_for_status()
                    del buffer[:len(chunk)]
                    log.info("posted %d results: %s", len(chunk), r.json().get("data"))
                except Exception as exc:  # noqa: BLE001
                    log.warning("post failed (%s); %d results buffered", exc, len(buffer))
                    break
            if once:
                return
            await asyncio.sleep(TICK_S)


def main() -> None:
    ap = argparse.ArgumentParser(description="OpenGrid probe agent (one vantage point)")
    ap.add_argument("--server", default=os.environ.get("OPENGRID_SERVER"), required=not os.environ.get("OPENGRID_SERVER"))
    ap.add_argument("--location", default=os.environ.get("OPENGRID_PROBE_LOCATION"), required=not os.environ.get("OPENGRID_PROBE_LOCATION"),
                    help="vantage-point name, lowercase letters, digits and dashes (e.g. eu-west)")
    ap.add_argument("--agent-id", default=os.environ.get("OPENGRID_PROBE_AGENT_ID"))
    ap.add_argument("--once", action="store_true", help="probe everything once, post, and exit")
    a = ap.parse_args()
    key = os.environ.get("OPENGRID_PROBE_KEY")
    if not key:
        raise SystemExit("set OPENGRID_PROBE_KEY (an API key with data:read and probes:write)")
    if not LOCATION_RE.match(a.location):
        raise SystemExit("--location must be 2-32 lowercase letters, digits or dashes")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(run(a.server, key, a.location, a.agent_id or a.location, a.once))


if __name__ == "__main__":
    main()
