"""Paperspace: archived captures of www.paperspace.com/pricing (2016 on).

Live, OpenGrid reads Paperspace through Shadeform's catalogue (providers/shadeform.py PaperspaceProvider, on branch
providers/batch-nokey): listing id = Shadeform's type ("A6000x2", "A100_sxm4_80Gx8"), raw names "A6000 pcie",
"A100_80G sxm4", ... Archived rows are Paperspace's OWN page, so they carry provider_tier "paperspace-page" and its
own list price (Shadeform's live rate is that price + $0.04/GPU-h, $0.10 on the A100s), but the same listing id and
the same canonical GPU as the live row for the same machine, so the two series join.

The page prints one card per machine: "A4000 $ 0.76 / hour NVIDIA A4000 GPU 45GB RAM 8 vCPU" (2021 on) or
"V100 The most powerful GPU ... $ 2.30 / hour 8 vCPU 30 GB RAM 16 GB GPU" (2020), "P5000 $ 0.78 / hour 456 / month
..." (2017 - 19), and multi-GPU cards "P5000 x2 $ 1.56 / hour".
Read: a known machine name directly followed by "$ <price> / hour" (hourly, whole machine) and then its own spec. A
price marked "*" / "**" is a commitment rate ("$2.24* / hour ... 3-year commitment") and is never read; a card whose
spec names another GPU ("H100 New $ 3.09 / hour NVIDIA A100 GPU", a 2023 - 24 copy-paste on the page) is skipped.
Monthly prices and the Gradient notebook price list (2018) are not read.
"""
from __future__ import annotations

import re
from datetime import datetime

from backfill.parsers import dec, listing, text
from backfill.sources import own_page
from models import ComputeListing

NAMES = ("M4000", "P4000", "P5000", "P6000", "V100-32G", "V100", "RTX4000", "RTX5000", "A4000", "A5000", "A6000",
         "A100-80G", "A100", "H100")
# page machine -> Shadeform's shade_instance_type base (the live listing id), where it differs
TYPE_ID = {"A100-80G": "A100_sxm4_80G", "V100-32G": "V100_32G"}
# page machine -> the live raw GPU name (providers/shadeform.py gpu_label + PaperspaceProvider.labels)
LIVE_RAW = {"A4000": "A4000 pcie", "A5000": "A5000 pcie", "A6000": "A6000 pcie", "V100": "V100 pcie",
            "V100-32G": "V100_32G pcie", "A100": "A100 pcie", "A100-80G": "A100_80G sxm4",
            "H100": "H100 (Paperspace: HGX per its page, pcie per Shadeform)"}
# The canonical GPU the live pipeline gives each raw name (canonical.py on providers/batch-nokey); set here so rows
# imported before that branch is merged already carry it. Names live leaves unmapped stay unmapped.
CANONICAL = {"A4000 pcie": "NVIDIA RTX A4000 16GB", "A5000 pcie": "NVIDIA RTX A5000 24GB",
             "A6000 pcie": "NVIDIA RTX A6000 48GB", "A100 pcie": "NVIDIA A100 40GB PCIe",
             "A100_80G sxm4": "NVIDIA A100 80GB SXM4"}
# what each card's own spec must name ("NVIDIA <X> GPU"), when it names one
SPEC_GPU = {"A100-80G": ("A100",), "H100": ("HGX H100", "H100"), "RTX4000": ("RTX4000",), "RTX5000": ("RTX5000",)}

_NAME_ALT = "|".join(re.escape(n) for n in NAMES)
# Between a card's name and its price: nothing, a "New" tag, or (2020) a one-line description that names no other
# machine and holds no price and no card boundary ("Create", "Sign up").
_GAP = r"(?:(?!\$|Create|Sign up|(?<![\w-])(?:" + _NAME_ALT + r")(?![\w-])).){0,120}?"
_CARD = re.compile(r"(?<![\w-])(?P<name>" + _NAME_ALT + r")(?![\w-])(?: x(?P<n>[248]))?"
                   r"(?P<gap> " + _GAP + r")?\$ (?P<p>\d+(?:\.\d+)?)(?P<star>\**) / hour (?P<spec>.{0,40})")
_SPEC_NAMED = re.compile(r"^NVIDIA (?P<g>.+?) GPU\b")
_SPEC_PLAIN = re.compile(r"^(?:\d+ vCPU|\d+ GB GPU|\d+ / month)\b")
_STRIP = re.compile(r"<(script|style)\b.*?</\1>", re.S)


def paperspace_cards(page: str, at: datetime) -> list[ComputeListing]:
    t = text(_STRIP.sub(" ", page))
    for stop in ("Additional Gradient", "Gradient° GPU Types"):
        if stop in t:
            t = t[: t.index(stop)]
    out = []
    for m in _CARD.finditer(t):
        if m["star"]:
            continue  # a commitment rate
        name, count, spec = m["name"], int(m["n"] or 1), m["spec"]
        named = _SPEC_NAMED.match(spec)
        if named:
            if named["g"] not in SPEC_GPU.get(name, (name,)):
                continue  # the card's spec is another GPU's (page copy-paste): its price is not this machine's
        elif not _SPEC_PLAIN.match(spec):
            raise ValueError(f"paperspace(archive): card {m.group(0)[:80]!r} has no spec after its price")
        type_id = TYPE_ID.get(name, name) + (f"x{count}" if count > 1 else "")
        raw = LIVE_RAW.get(name, f"Paperspace {name}")
        if name == "A100" and count == 8:
            raw = "A100 pcie nvlink"  # live: Paperspace's A100x8 is NVLink-bridged PCIe (unmapped)
        o = listing("paperspace", sku=name + (f"x{count}" if count > 1 else ""), listing_id=type_id, raw=raw,
                    count=count, instance_price=dec(m["p"]), at=at, tier="paperspace-page")
        out.append(o.model_copy(update={"canonical_gpu_name": CANONICAL.get(raw)}))
    if not out:
        raise ValueError("paperspace(archive): no '<machine> $ x / hour' card")
    return out


SOURCES = [own_page("paperspace", "www.paperspace.com/pricing", paperspace_cards)]
