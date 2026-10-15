"""Grounding check: every number in a reply must come from the cards (tool results) or the user's own words.

    allowed(cards, *texts) -> Allowed     numbers present anywhere in the card data (values and strings)
    numbers(text)          -> [(token, value, decimals, is_money, is_pct)]
    check(reply, allowed, cards) -> list of problems ([] = grounded)

A reply number with d decimals is grounded when some allowed value rounds to it at d decimals (prices shown as
$2.99 from 2.9900, 29.2 GB from 29.2), or, for a percentage, when value x 100 does (fractions shown as %).
Bare integers 0..12 not followed by % and not money are allowed (counts such as "3 options", "top 5") -- a
price, a percentage or any larger number must be backed by data. Links must be relative paths present in the
cards, and the reply may not claim an action (there are no action tools): neither "I launched / cancelled ..." nor
"your job has been cancelled / was started / is now running". Job ids, epochs and costs are allowed like any other
number: they are in the `job` / `jobs` / `action` card data. An ACTION card is a button the user still has to click
and confirm, so a reply may describe it ("click Cancel ... and confirm") but never as done.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

_NUM_IN_REPLY = re.compile(r"(?<![A-Za-z0-9_.])(\$\s*)?([-−]?\d[\d,]*(?:\.\d+)?)(\s*%)?")
_NUM_IN_STRING = re.compile(r"\d+(?:\.\d+)?")
_ACTION = re.compile(r"\bI(?:'ve| have| just|'ll| will)?\s+(?:launched|routed|booked|reserved|purchased|bought|"
                     r"started|deployed|provisioned|submitted|approved|placed|quoted|cancelled|canceled|terminated|"
                     r"stopped|killed|created|scheduled|queued|ran|"
                     r"launch|route|book|reserve|purchase|buy|deploy|provision|submit|approve|cancel|terminate|stop|"
                     r"kill|start|create|schedule|queue|run)\b", re.I)
# "your job has been cancelled", "the simulation was started", "it is now running": a claim that something
# happened. Deterministic job replies use the recorded state words instead ("Cancelled (the GPU was shut down)").
_CLAIM = re.compile(r"\b(?:has|have|had|was|were|is|are|got)\s+(?:now\s+|just\s+|already\s+|successfully\s+)*"
                    r"(?:been\s+)?(?:cancell?ed|started|launched|submitted|approved|terminated|stopped|killed|"
                    r"created|deployed|queued|scheduled|provisioned|kicked off)\b|\bis now (?:running|training|live)\b",
                    re.I)
_URL = re.compile(r"(https?://|www\.)\S+", re.I)


@dataclass
class Allowed:
    values: set = field(default_factory=set)   # floats (absolute values)
    links: set = field(default_factory=set)

    def ok(self, value: float, decimals: int, pct: bool) -> bool:
        v = abs(value)
        for a in self.values:
            if round(a, decimals) == round(v, decimals) or math.isclose(a, v, rel_tol=1e-9):
                return True
            if pct and round(a * 100, decimals) == round(v, decimals):
                return True
        return False


def _walk(x, out: Allowed, depth=0):
    if depth > 12:
        return
    if isinstance(x, bool) or x is None:
        return
    if isinstance(x, (int, float)):
        if math.isfinite(x):
            out.values.add(abs(float(x)))
        return
    if isinstance(x, str):
        for m in _NUM_IN_STRING.finditer(x):
            out.values.add(float(m.group(0)))
        if x.startswith("/") or x.startswith("http"):
            out.links.add(x)
        return
    if isinstance(x, dict):
        for k, v in x.items():
            _walk(v, out, depth + 1)
        return
    if isinstance(x, (list, tuple)):
        for v in x:
            _walk(v, out, depth + 1)


def allowed(cards: list, *texts) -> Allowed:
    out = Allowed()
    _walk(cards, out)
    for t in texts:
        _walk(t, out)
    return out


def numbers(text: str) -> list[tuple[str, float, int, bool, bool]]:
    out = []
    for m in _NUM_IN_REPLY.finditer(text or ""):
        raw = m.group(2).replace(",", "").replace("−", "-")
        try:
            v = float(raw)
        except ValueError:
            continue
        dec = len(raw.split(".", 1)[1]) if "." in raw else 0
        out.append((m.group(0).strip(), v, dec, bool(m.group(1)), bool(m.group(3))))
    return out


def check(reply: str, ok: Allowed) -> list[str]:
    problems = []
    for tok, v, dec, money, pct in numbers(reply):
        if not money and not pct and dec == 0 and 0 <= v <= 12:
            continue
        if not ok.ok(v, dec, pct):
            problems.append(f"ungrounded number {tok!r}")
    for m in _URL.finditer(reply or ""):
        if m.group(0).rstrip(".,)") not in ok.links:
            problems.append(f"link not in the cards: {m.group(0)!r}")
    if _ACTION.search(reply or ""):
        problems.append("claims an action (the assistant has no action tools)")
    elif _CLAIM.search(reply or ""):
        problems.append("claims that something happened (the assistant only offers actions for the user to confirm)")
    return problems
