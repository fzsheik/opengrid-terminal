"""Shadeform's public catalogue: archived captures of GET api.shadeform.ai/v1/instances/types.

The live poller reads this one anonymous endpoint for every cloud that has no feed of its own (providers/shadeform.py:
Crusoe, Latitude.sh, Denvr, Boost Run, Horizon, Phyntec) and keeps each cloud's rows. Scaleway left this list on
2026-11-01 (its own EUR catalogue, converted: providers/scaleway.py), so Shadeform captures no longer give it archived
rows either: they carried Shadeform's margin, not Scaleway's list price. An archived capture is
the same whole catalogue, so each of those clouds gets one Source over the same URL whose parser is the live one:
keep that cloud's rows, then <Cloud>Provider.normalize. The download is shared through the archive cache.

These are the live semantics exactly (unlike the clouds' own pages, e.g. backfill/parsers/crusoe.py, which print the
cloud's own price); only two captures exist (Nov 2025, Jan 2026).
"""
from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

from backfill.sources import Source
from providers import PROVIDERS
from providers.shadeform import ENDPOINT, ShadeformCloud

URL = "api.shadeform.ai/v1/instances/types"


def _parser(cls: type[ShadeformCloud]):
    def parse(body: bytes, at: datetime):
        d = json.loads(body)
        types = d.get("instance_types") if isinstance(d, dict) else None
        if not isinstance(types, list):
            raise ValueError("shadeform(archive): response has no instance_types list")
        payload = {"instance_types": [t for t in types if t.get("cloud") == cls.cloud]}
        return cls.normalize({ENDPOINT: [SimpleNamespace(endpoint=ENDPOINT, payload=payload, fetched_at=at, ok=True)]})
    return parse


# Denvr's own page (backfill/parsers/denvr.py, 8 captures) prints its own price, ~5% below Shadeform's; mixing the two
# sources would make the held daily series alternate between them, so Denvr is read from its own page only.
OWN_PAGE = {"denvr"}
CLOUDS = sorted((c for c in PROVIDERS.values() if isinstance(c, type) and issubclass(c, ShadeformCloud)
                 and c is not ShadeformCloud and c.name not in OWN_PAGE), key=lambda c: c.name)
SOURCES = [Source(c.name, URL, ((f"backfill.parsers.shadeform ({c.__name__}.normalize, live parser)", _parser(c)),))
           for c in CLOUDS]
