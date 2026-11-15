"""Atlantic.Net: archived captures of www.atlantic.net/gpu-server-hosting/ (the live poller's page) and of
/gpu-server-hosting/gpu-cloud-hosting/, which prints the same "GPU Cloud Hosting Pricing Plans" table.

Live (providers/atlanticnet.py, Oct 2026 design): an On-Demand / 1 Year Term / 3 Year Term toggle of <button>s, a
<table class="main-pricing-table ..."> whose header cells are buttons, seven cells per plan and an od-price span
holding "$x/mo" and "$x/hr" for the whole plan. Those captures go through the live parser unchanged.

Mar 2025 - Sep 2026 (atlantic_table_2025): the same table one design earlier. The toggle entries are <a> or <button>
with data-term="onDemand" and the text "On-Demand" (required, as live); the table's class list holds
main-pricing-table; the header is plain <th>s, with a hidden "ID" column first (and from 2026 an "Action" column
last); each price cell holds an od-price <div> with a rate-hourly "$x/hr" span. Read exactly as live: the on-demand
hourly figure only (never the 1- / 3-year term prices or the monthly figure), then AtlanticNetProvider.normalize,
so listing id (the plan), GPU name ("L40S", "H100NVL", as printed) and tier are the live ones.

Before May 2025 the hub page prints no prices at all (the table is absent); nothing is read from those captures.
"""
from __future__ import annotations

import re
from datetime import datetime
from types import SimpleNamespace

from backfill.parsers import text
from backfill.sources import Source
from models import ComputeListing
from providers import PROVIDERS
from providers import atlanticnet as live

_ON_DEMAND = re.compile(r'<(?:a|button)\b[^>]*data-term="onDemand"[^>]*>\s*On-Demand\s*</(?:a|button)>')
_TABLE = re.compile(r'<table class="[^"]*\bmain-pricing-table\b[^"]*"[^>]*>(?P<t>.*?)</table>', re.S)
_TH = re.compile(r"<th\b[^>]*>(?P<v>.*?)</th>", re.S)
_TR = re.compile(r"<tr\b[^>]*>(?P<r>.*?)</tr>", re.S)
_TD = re.compile(r"<td\b[^>]*>(?P<v>.*?)</td>", re.S)
_OD = re.compile(r'<div class="od-price"[^>]*>.*?<span\s+class="rate-hourly[^"]*">(?P<hr>[^<]*)</span>', re.S)
_COLUMNS = ["Plan", "RAM", "vCPU", "GPU", "SSD", "Transfer", "Price"]


def atlantic_table_2025(page: str, at: datetime) -> list[ComputeListing]:
    if _ON_DEMAND.search(page) is None:
        raise ValueError("atlanticnet(archive): no 'On-Demand' billing-term toggle")
    tm = _TABLE.search(page)
    if tm is None:
        raise ValueError("atlanticnet(archive): no main-pricing-table")
    head = [text(h["v"]) for h in _TH.finditer(tm["t"])]
    lead = 1 if head[:1] == ["ID"] else 0
    cols = head[lead:]
    if cols and cols[-1] == "Action":
        cols = cols[:-1]
    if cols != _COLUMNS:
        raise ValueError(f"atlanticnet(archive): pricing table header {head}")
    plans = []
    for r in _TR.finditer(tm["t"]):
        cells = [c["v"] for c in _TD.finditer(r["r"])]
        if not cells or "<th" in r["r"]:
            continue  # the header row (its trailing empty <td> in 2025)
        cells = cells[lead: lead + len(_COLUMNS)]
        if len(cells) != len(_COLUMNS):
            raise ValueError(f"atlanticnet(archive): a plan row has {len(cells)} cells, not {len(_COLUMNS)}")
        od = _OD.search(cells[6])
        if od is None or live._HOURLY.match(od["hr"].strip()) is None:
            raise ValueError("atlanticnet(archive): a plan row lost its on-demand '$x/hr' price")
        plans.append(dict(zip(["plan", "ram", "vcpu", "gpu", "ssd", "transfer"],
                              [text(c) for c in cells[:6]]), on_demand_hourly=od["hr"].strip()))
    if not plans:
        raise ValueError("atlanticnet(archive): no plan rows")
    snap = SimpleNamespace(endpoint=live.ENDPOINT, payload={"plans": plans}, fetched_at=at, ok=True)
    return PROVIDERS["atlanticnet"].normalize({live.ENDPOINT: [snap]})


def _live(body: bytes, at: datetime) -> list[ComputeListing]:
    payload = live.extract(body.decode("utf-8", errors="replace"))
    snap = SimpleNamespace(endpoint=live.ENDPOINT, payload=payload, fetched_at=at, ok=True)
    return PROVIDERS["atlanticnet"].normalize({live.ENDPOINT: [snap]})


def _parsers():
    return (("providers.atlanticnet (live parser)", _live),
            ("backfill.parsers.atlanticnet.atlantic_table_2025",
             lambda body, at: atlantic_table_2025(body.decode("utf-8", errors="replace"), at)))


SOURCES = [Source("atlanticnet", "www.atlantic.net/gpu-server-hosting/", _parsers()),
           Source("atlanticnet", "www.atlantic.net/gpu-server-hosting/gpu-cloud-hosting/", _parsers())]
