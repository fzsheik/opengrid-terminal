"""Nebius: archived designs of nebius.com/prices the live parser (providers/nebius.py) rejects.

  with_preemptible   Jul 2026 on: the "NVIDIA GPU Instances" table gained a "Preemptible, GPU-hour" column next
                     to "On-demand, GPU-hour". The live parser reads only on-demand GPU-hour prices (preemptible is
                     never read), so this drops the preemptible column(s) and hands the rest to the live extract
                     checks and NebiusProvider.normalize unchanged: same per-GPU price, one-GPU listings, raw item
                     name as listing id, dated "(Effective ...)" columns picked by capture time.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from types import SimpleNamespace

from backfill.sources import live_page
from models import ComputeListing
from providers import nebius as N

_PREEMPTIBLE = re.compile(r"^Preemptible\b", re.I)


def with_preemptible(page: str, at: datetime) -> list[ComputeListing]:
    m = N._NEXT_DATA.search(page)
    if m is None:
        raise ValueError("nebius(archive): page does not embed __NEXT_DATA__")
    tables = []
    for b in N._blocks(json.loads(m.group(1)), []):
        if (b.get("title") or "").strip() != N._GPU_TABLE:
            continue
        content = (b.get("table") or {}).get("content")
        if not isinstance(content, list) or not content:
            raise ValueError("nebius(archive): GPU table block has no content")
        if content not in tables:
            tables.append(content)
    if len(tables) != 1:
        raise ValueError(f"nebius(archive): expected one on-demand GPU table, found {len(tables)}")
    header = [str(c).strip() for c in tables[0][0]]
    drop = {i for i, c in enumerate(header) if _PREEMPTIBLE.search(c)}
    if not drop:
        raise ValueError("nebius(archive): no preemptible column (not this design)")
    rows = tables[0][1:]
    if not rows or any(len(r) != len(header) or not all(isinstance(c, str) for c in r) for r in rows):
        raise ValueError("nebius(archive): GPU table rows no longer match its header")
    keep = [i for i in range(len(header)) if i not in drop]
    payload = {"header": [header[i] for i in keep], "rows": [[r[i] for i in keep] for r in rows]}
    N._price_columns(payload["header"])  # the live column checks: raises on anything but on-demand GPU-hour
    snap = SimpleNamespace(endpoint=N.ENDPOINT, payload=payload, fetched_at=at, ok=True)
    return N.NebiusProvider.normalize({N.ENDPOINT: [snap]})


SOURCES = [live_page("nebius", "nebius", "nebius.com/prices", with_preemptible)]
