"""Which archived documents are read for which provider, and how.

A Source is one URL the Internet Archive has captures of, plus a parser that turns one capture's bytes into
ComputeListings. Where OpenGrid already scrapes that page live, the parser IS the live one: the provider module's
own extract(html) followed by its Provider.normalize(), fed a stand-in snapshot whose fetched_at is the capture
time. A capture the live parser rejects ("page reshaped") is reported and skipped, never guessed at.

Older designs and pages OpenGrid does not scrape live (e.g. lambda.ai/pricing, whose live data comes from an API
that needs a key) have their own parsers, one module per provider in backfill/parsers/, each defining SOURCES.
A live page with no module there is registered in LIVE_ONLY below.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from typing import Callable

from models import ComputeListing


Parser = Callable[[bytes, datetime], list[ComputeListing]]


@dataclass(frozen=True)
class Source:
    provider: str
    url: str                                   # as given to the Archive's CDX server
    parsers: tuple[tuple[str, Parser], ...]    # (name recorded on every row, parser), tried in order

    def parse(self, body: bytes, at: datetime) -> tuple[list[ComputeListing], str]:
        """The first parser that reads this capture wins; when none does, every parser's reason is raised."""
        reasons = []
        for name, fn in self.parsers:
            try:
                listings = fn(body, at)
            except Exception as exc:
                reasons.append(f"{name}: {type(exc).__name__}: {exc}")
                continue
            if listings:
                return listings, name
            reasons.append(f"{name}: no listings")
        raise ValueError(" | ".join(reasons))


def _snapshot(endpoint: str, payload, at: datetime):
    return SimpleNamespace(endpoint=endpoint, payload=payload, fetched_at=at, ok=True)


def _text_parser(fn: Callable[[str, datetime], list[ComputeListing]]) -> tuple[str, Parser]:
    return f"{fn.__module__}.{fn.__name__}", lambda body, at: fn(body.decode("utf-8", errors="replace"), at)


RETIRED = {"jarvislabs", "gcp"}  # live pollers now on official APIs; page scrapers kept in backfill/retired/


def live_page(provider: str, module: str, url: str, *older) -> Source:
    """A page the live poller scrapes: reuse providers/<module>.extract + <Provider>.normalize unchanged, then
    the parsers for its older designs (backfill/parsers/<provider>.py), in order."""
    from providers import PROVIDERS

    # A provider whose live poller moved to an API keeps its old page scraper in backfill/retired/.
    path = f"backfill.retired.{module}_page" if module in RETIRED else f"providers.{module}"
    mod = importlib.import_module(path)
    normalize = mod.normalize if module in RETIRED else PROVIDERS[provider].normalize

    def parse(body: bytes, at: datetime) -> list[ComputeListing]:
        payload = mod.extract(body.decode("utf-8", errors="replace"))
        return normalize({mod.ENDPOINT: [_snapshot(mod.ENDPOINT, payload, at)]})

    live = (f"providers.{module} (live parser)", parse)
    return Source(provider, url, (live, *(_text_parser(f) for f in older)))


def own_page(provider: str, url: str, *fns) -> Source:
    """A page OpenGrid does not scrape live: only backfill/parsers/ parsers."""
    return Source(provider, url, tuple(_text_parser(f) for f in fns))


# Live page scrapers whose archived captures need no parser beyond the live one (yet).
LIVE_ONLY = [
    ("together", "together", "www.together.ai/pricing"),
    ("nebius", "nebius", "nebius.com/prices"),
    ("hotaisle", "hotaisle", "hotaisle.xyz/pricing"),
    ("jarvislabs", "jarvislabs", "jarvislabs.ai/pricing"),
    ("massedcompute", "massedcompute", "vm.massedcompute.com/pricing"),
    ("gcp", "gcp", "cloud.google.com/products/compute/pricing/accelerator-optimized"),
]


def sources() -> list[Source]:
    """Every module in backfill/parsers/ contributes its SOURCES; LIVE_ONLY pages not claimed by one are added."""
    import pkgutil

    from backfill import parsers

    out: list[Source] = []
    for m in sorted(pkgutil.iter_modules(parsers.__path__), key=lambda m: m.name):
        out += importlib.import_module(f"backfill.parsers.{m.name}").SOURCES
    claimed = {s.url for s in out}
    out += [live_page(p, mod, url) for p, mod, url in LIVE_ONLY if url not in claimed]
    return out
