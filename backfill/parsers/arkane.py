"""Arkane Cloud: archived captures of arkanecloud.com/pricing/ (the live poller's page; the Archive files /pricing and
/pricing/ under one key).

Live (providers/arkane.py): "Cloud GPU pricing (per hour, billed per second)" with "Prices in USD." stated; each row
a 1-GPU listing at its "Price per GPU", listing id "<slug(GPU + VRAM)>:1x", raw name "NVIDIA H100 80GB", tier "vm".
No archived capture carries that wording yet. Three older designs, newest first:

  arkane_on_demand_table (Dec 2025 - Apr 2026): the same labelled table (<tr data-column-title>, <td data-row-title>)
      headed GPU type | Price per GPU | CPU | RAM | VRAM, without the USD line. The header names the unit (per GPU)
      and every cell prints "$x/hr"; rows go through the live extract-row shape and ArkaneProvider.normalize.
  arkane_gpu_cards (Jul 2024 - Sep 2025): one card per GPU: "H100 NVIDIA H100 80GB : <blurb> $ 2.1 /hr $ 1533 /month"
      followed by its action label. Only cards labelled "On demand" are read; "Reserve Cluster / Reserved Cloud"
      (reserved capacity) and "coming soon" cards are not. A card whose model line names another GPU than its
      heading ("B200 RTX A4000 180GB", Sep 2025) is a copy-paste on the page and is skipped.
  arkane_server_cards (Sep 2023 - Mar 2024): "$ 1 / hour/GPU [$8/hour per server] > 8-RTX A5000 24GB > 64vCPU ...":
      8-GPU servers priced per GPU-hour. One listing per server ("<slug>:8x", 8 GPUs, instance = 8 x the GPU price;
      checked against the "per server" figure where printed), same raw name and canonical GPU as live.
"""
from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

from backfill.parsers import dec, listing, slug, text
from backfill.sources import Source
from models import ComputeListing
from providers import PROVIDERS
from providers import arkane as live

_TABLE = re.compile(r"<table[^>]*>(?P<t>.*?)</table>", re.S)
_TH = re.compile(r"<th[^>]*>(?P<v>.*?)</th>", re.S)
_HEAD = ["GPU type", "Price per GPU", "CPU", "RAM", "VRAM"]


def _normalize(rows: list[dict], at: datetime) -> list[ComputeListing]:
    snap = SimpleNamespace(endpoint=live.ENDPOINT, payload={"rows": rows}, fetched_at=at, ok=True)
    return PROVIDERS["arkane"].normalize({live.ENDPOINT: [snap]})


def arkane_on_demand_table(page: str, at: datetime) -> list[ComputeListing]:
    for tm in _TABLE.finditer(page):
        thead = tm["t"].split("<tbody", 1)[0]
        if [text(h["v"]) for h in _TH.finditer(thead)] != _HEAD:
            continue
        rows = []
        for m in live._ROW.finditer(tm["t"]):
            cells = {c["label"].strip(): live._clean(c["v"]) for c in live._CELL.finditer(m["cells"])}
            if not live._PRICE.match(cells.get("Price per GPU", "")):
                raise ValueError(f"arkane(archive): row {m['gpu']!r} price {cells.get('Price per GPU')!r} is not '$x/hr'")
            rows.append({"gpu": live._html.unescape(m["gpu"]).strip(), "price": cells["Price per GPU"],
                         "cpu": cells.get("CPU"), "ram": cells.get("RAM"), "vram": cells.get("VRAM")})
        if not rows:
            raise ValueError("arkane(archive): the 'Price per GPU' table has no labelled rows")
        return _normalize(rows, at)
    raise ValueError(f"arkane(archive): no table headed {' | '.join(_HEAD)}")


_STRIP = re.compile(r"<(script|style)\b.*?</\1>", re.S)
_GPU_CARD = re.compile(
    r"(?P<head>[A-Z][A-Z0-9]{1,6}) (?P<model>(?:NVIDIA|RTX) [A-Za-z0-9 ]+?) (?P<vram>\d+)GB : [^$]*?"
    r"\$ (?P<p>\d+(?:\.\d+)?) /hr (?:\$ [\d,.]+ /month )?(?P<label>Get started On demand|Get started coming soon|"
    r"Reserve Cluster Reserved Cloud|Get started)", re.I)


def arkane_gpu_cards(page: str, at: datetime) -> list[ComputeListing]:
    t = text(_STRIP.sub(" ", page))
    if "GPU Instances:" not in t:
        raise ValueError("arkane(archive): no 'GPU Instances:' cards")
    cards = list(_GPU_CARD.finditer(t[t.index("GPU Instances:"):]))
    if not cards:
        raise ValueError("arkane(archive): no '<GPU> <model> <N>GB : ... $ x /hr' card")
    rows = []
    for m in cards:
        if m["label"].lower() != "get started on demand":
            continue  # reserved capacity or coming soon: not an on-demand price
        if m["head"].upper() not in m["model"].upper().replace(" ", ""):
            continue  # the card's model line is another GPU's (page copy-paste)
        rows.append({"gpu": m["model"], "price": f"${m['p']}/hr", "cpu": None, "ram": None, "vram": f"{m['vram']} GB"})
    if not rows:
        raise ValueError("arkane(archive): no card labelled 'On demand'")
    return _normalize(rows, at)


_SERVER = re.compile(r"\$ (?P<p>\d+(?:\.\d+)?) / hour/GPU (?:\$(?P<srv>\d+(?:\.\d+)?)/hour per server )?"
                     r"(?:1 Day Free Trial )?> (?P<n>\d+)-(?P<gpu>[A-Za-z0-9 ]+?) (?P<vram>\d+)GB > (?P<vcpu>\d+)vCPU "
                     r"> (?P<ram>\d+)GB Ram")


def arkane_server_cards(page: str, at: datetime) -> list[ComputeListing]:
    t = text(_STRIP.sub(" ", page))
    out = []
    for m in _SERVER.finditer(t):
        n, per_gpu = int(m["n"]), dec(m["p"])
        if m["srv"] is not None and dec(m["srv"]) != per_gpu * n:
            raise ValueError(f"arkane(archive): {m['gpu']} ${per_gpu}/GPU x {n} != ${m['srv']} per server")
        raw = live.raw_name(m["gpu"], f"{m['vram']} GB")
        out.append(listing("arkane", sku=f"{n}-{m['gpu']}", listing_id=f"{slug(raw)}:{n}x", raw=raw, count=n,
                           instance_price=per_gpu * Decimal(n), at=at, tier="vm", vcpu=int(m["vcpu"]),
                           ram_gb=float(m["ram"])))
    if not out:
        if "hour/GPU" in t:
            raise ValueError("arkane(archive): a '/ hour/GPU' price this parser cannot read")
        raise ValueError("arkane(archive): no '$ x / hour/GPU > N-<GPU>' server card")
    return out


def _live(body: bytes, at: datetime) -> list[ComputeListing]:
    return _normalize(live.extract(body.decode("utf-8", errors="replace"))["rows"], at)


def _text(fn):
    return f"backfill.parsers.arkane.{fn.__name__}", lambda body, at: fn(body.decode("utf-8", errors="replace"), at)


SOURCES = [Source("arkane", "arkanecloud.com/pricing/", (
    ("providers.arkane (live parser)", _live),
    _text(arkane_on_demand_table), _text(arkane_gpu_cards), _text(arkane_server_cards)))]
