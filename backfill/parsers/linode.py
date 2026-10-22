"""Akamai Cloud (Linode): archived captures of GET api.linode.com/v4/linode/types.

The archived request has no page_size, but both captures say page 1 of 1 (every type on one page), so the body is
the same catalogue the live poller reads (providers/linode.py, page_size=500). It goes through the live path
unchanged: GPU types only (gpus > 0), then LinodeProvider.normalize.
"""
from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

from backfill.sources import Source
from models import ComputeListing
from providers.linode import ENDPOINT, LinodeProvider


def linode_types(body: bytes, at: datetime) -> list[ComputeListing]:
    d = json.loads(body)
    if not isinstance(d, dict) or not isinstance(d.get("data"), list):
        raise ValueError("linode(archive): response has no data list")
    if (d.get("pages") or 1) != 1:
        raise ValueError(f"linode(archive): page {d.get('page')} of {d.get('pages')}: a partial catalogue")
    payload = {**{k: v for k, v in d.items() if k != "data"}, "data": [t for t in d["data"] if t.get("gpus")]}
    return LinodeProvider.normalize({ENDPOINT: [SimpleNamespace(endpoint=ENDPOINT, payload=payload, fetched_at=at, ok=True)]})


SOURCES = [Source("linode", "api.linode.com/v4/linode/types",
                  (("backfill.parsers.linode.linode_types (live normalizer)", linode_types),))]
