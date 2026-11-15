"""Mithril (Foundry until Aug 2025): archived captures of mithril.ai/pricing and of Foundry's mlfoundry.com/pricing.

Same semantics as the live parser (providers/mithril.py): the "Compute" table's "Spot (7-day avg)" column is a weekly
7-day average of Mithril's algorithmic spot price for the WHOLE instance ("$x instance/hr"), so every row is one spot,
interruptible listing, provider_tier "vm", listing id "<gpu slug>:<n>x:<vcpu>c-<ram>g-<disk>d" (the live id).

    two_columns   Jan 2025 - Mar 2026 (mlfoundry.com/pricing Jan - May 2025, mithril.ai/pricing Sep 2025 - Mar 2026):
                  Instance type | GPUs | vCPUs | RAM | Disk | Spot (7-day avg) | Reserved (7-day avg). Only the Spot
                  column is read; "Reserved" is Mithril's reserved capacity (a commitment) and is never read. The
                  "average of the last 7 days" sentence is required, as live. A row priced $0.00 is skipped (the live
                  parser skips it too).

From Apr 2026 the page prints the Spot column alone and the live parser reads it unchanged. foundrytechnologies.com
and mlfoundry.com's other pages were checked (CDX, 2018 - 2026) and print no prices.
"""
from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal

from backfill.parsers import listing, slug, text
from backfill.sources import live_page, own_page
from models import ComputeListing

_HEAD = "Instance type GPUs vCPUs RAM Disk Spot (7-day avg) Reserved (7-day avg)"
_AVERAGE = "represent an average of the last 7 days"
# What ends the table: Foundry's (2025) or Mithril's note on how prices are set.
_END = re.compile(r"(?:Foundry Cloud Platform uses market-based pricing|Mithril's omnicloud platform sets)")
_NUM = r"\d{1,3}(?:,\d{3})*(?:\.\d+)?"
_ROW = re.compile(rf"(?P<gpu>[A-Za-z][A-Za-z0-9 ]*?) (?P<n>\d+) (?P<vcpu>\d+) (?P<ram>{_NUM}) GB (?P<disk>{_NUM}) GB "
                  rf"\$(?P<spot>{_NUM}) instance/hr \$(?P<res>{_NUM}) instance/hr\s*")


def _n(s: str) -> str:
    """'1,750' -> '1750', '862.5' -> '862.5' (the live id prints integers without separators)."""
    return str(Decimal(s.replace(",", "")).normalize()) if "." in s else s.replace(",", "")


def two_columns(page: str, at: datetime) -> list[ComputeListing]:
    t = text(re.sub(r"<(script|style)\b.*?</\1>", " ", page.replace("&#x27;", "'"), flags=re.S))
    if _AVERAGE not in t:
        raise ValueError("mithril(archive): the '7-day average' note is gone - price meaning unknown")
    start = t.find(_HEAD)
    if start == -1:
        raise ValueError("mithril(archive): no 'Spot (7-day avg) | Reserved (7-day avg)' compute table")
    end = _END.search(t, start)
    if end is None:
        raise ValueError("mithril(archive): compute table has no closing pricing note")
    body, pos, out = t[start + len(_HEAD): end.start()].strip() + " ", 0, []
    while pos < len(body.rstrip()):
        m = _ROW.match(body, pos)
        if m is None:
            raise ValueError(f"mithril(archive): a compute row does not read as expected: {body[pos:pos + 120]!r}")
        pos = m.end()
        count, price = int(m["n"]), Decimal(m["spot"].replace(",", ""))
        if count < 1 or price <= 0:
            continue  # "$0.00": no price this week
        gpu, vcpu, ram, disk = m["gpu"].strip(), int(m["vcpu"]), _n(m["ram"]), _n(m["disk"])
        out.append(listing("mithril", sku=f"{gpu}:{count}x", listing_id=f"{slug(gpu)}:{count}x:{vcpu}c-{ram}g-{disk}d",
                           raw=gpu, count=count, instance_price=price, at=at, market="spot", tier="vm", vcpu=vcpu,
                           ram_gb=float(ram), storage_gb=float(disk)))
    if not out:
        raise ValueError("mithril(archive): no priced compute row")
    return out


SOURCES = [live_page("mithril", "mithril", "mithril.ai/pricing", two_columns),
           own_page("mithril", "mlfoundry.com/pricing", two_columns)]
