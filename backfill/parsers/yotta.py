"""Yotta Shakti Cloud: archived captures of shakticloud.ai/pricing/ (the live poller's page).

Live (providers/yotta.py): the AI Workspace VM "On Demand" table, the one whose note says "*Unit Rate: Per Hour,
Per Compute"; INR per hour for the whole VM, converted at the ECB INR->USD cross rate in force at the capture
(fx.usd_rate with the capture time, fail closed: a capture with no ECB pair published within the staleness limit
gives no row). Captures from Nov 2026 go through the live parser unchanged.

Nov 2025 (yotta_hourly_tab): the same table one design earlier, with no note. The section's tabs are <li class=
"t-tab" tid="tN">; the "On Demand GPU Compute" tab's price dropdown (dropid="tN") offers exactly one option, "Price
Hourly" (the "Dedicated GPU Compute" tab's offers 1 / 3 / 6 / 12-month terms). That label is the unit statement; the
rows of container id="tN" are then read exactly as live (read_row, YottaProvider.normalize).

Not read (no row, reported):
  - Jan - Mar 2026: the On Demand tab's dropdown is empty and no note states a unit;
  - Apr - May 2026: the On Demand table's note says "*Unit Rate: Per Month, per Compute." (the Dedicated table's
    note, repeated), contradicting an hourly price; the page states a unit OpenGrid will not override;
  - before Nov 2025: no on-demand table at all (dedicated VMs per month, "MRC"), and pricing.html (2024 - May 2025)
    priced containers with no unit.
"""
from __future__ import annotations

import re
from datetime import datetime
from types import SimpleNamespace

from backfill.sources import Source
from models import ComputeListing
from providers import PROVIDERS
from providers import yotta as live

_TAB = re.compile(r'<li class="t-tab[^"]*" tid="(?P<id>t\d+)">\s*On Demand GPU Compute\s*</li>')
_SELECT = re.compile(r'<div class="price-dropdown-wrap" dropid="(?P<id>t\d+)">\s*<select[^>]*>(?P<opts>.*?)</select>', re.S)
_OPTION = re.compile(r"<option[^>]*>(?P<v>.*?)</option>", re.S)


def _normalize(rows: list[dict], note: str, at: datetime) -> list[ComputeListing]:
    snap = SimpleNamespace(endpoint=live.ENDPOINT, payload={"unit_note": note, "rows": rows}, fetched_at=at, ok=True)
    out = PROVIDERS["yotta"].normalize({live.ENDPOINT: [snap]})
    if not out:
        raise ValueError(f"yotta(archive): no ECB INR->USD rate in force at {at:%Y-%m-%d} (fail closed)")
    return out


def yotta_hourly_tab(page: str, at: datetime) -> list[ComputeListing]:
    tab = _TAB.search(page)
    if tab is None:
        raise ValueError("yotta(archive): no 'On Demand GPU Compute' tab")
    tid = tab["id"]
    sel = next((m for m in _SELECT.finditer(page, tab.end()) if m["id"] == tid), None)
    opts = [live._text(o["v"]) for o in _OPTION.finditer(sel["opts"])] if sel else []
    if opts != ["Price Hourly"]:
        raise ValueError(f"yotta(archive): the On Demand tab's price unit is {opts or 'not stated'}, not ['Price Hourly']")
    at_c = page.find(f'<div class="new-table-container" id="{tid}"', sel.end())
    if at_c == -1:
        raise ValueError(f"yotta(archive): no table container {tid!r} for the On Demand tab")
    end = page.find('<div class="new-table-container"', at_c + 10)
    part = page[at_c: end if end != -1 else at_c + 30000]
    note = live._NOTE.search(part)
    if note and live.ON_DEMAND_NOTE not in live._text(note["v"]) and live._text(note["v"]):
        raise ValueError(f"yotta(archive): the On Demand table's note says {live._text(note['v'])!r}")
    rows = []
    for r in live._ROW.finditer(part):
        tds = list(live._TD.finditer(r["cells"]))
        if len(tds) < 3:
            continue
        price = live._PRICE1.search(tds[2]["attrs"])
        rows.append({"name": live._text(tds[0]["v"]), "description": live._text(tds[1]["v"]),
                     "price": live._html.unescape(price["v"]).strip() if price else live._text(tds[2]["v"])})
    if not any("on demand" in r["name"].lower() for r in rows):
        raise ValueError("yotta(archive): the On Demand tab's table has no 'On Demand' VM rows")
    return _normalize(rows, "Price Hourly (tab dropdown)", at)


def _live(body: bytes, at: datetime) -> list[ComputeListing]:
    payload = live.extract(body.decode("utf-8", errors="replace"))
    return _normalize(payload["rows"], payload["unit_note"], at)


SOURCES = [Source("yotta", "shakticloud.ai/pricing/", (
    ("providers.yotta (live parser)", _live),
    ("backfill.parsers.yotta.yotta_hourly_tab",
     lambda body, at: yotta_hourly_tab(body.decode("utf-8", errors="replace"), at)),
))]
