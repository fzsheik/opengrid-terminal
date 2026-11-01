"""Daily raw copy of dstack's gpuhunt catalogues (catalog_snapshots, source "gpuhunt"). Raw only: nothing reads
these into prices; they are kept because the publisher's old versions expire.

dstack's gpuhunt (github.com/dstackai/gpuhunt) publishes one catalogue per "offline" provider to a public-read S3
bucket, refreshed several times a day:

    {CATALOG_URL}/{provider}/version              -> e.g. "20261101-20691"
    {CATALOG_URL}/{provider}/{version}/catalog.zip -> {provider}.csv (instance, GPU, region, price, spot ...)

Versions older than about 90 days are no longer retrievable (research 2026-11-01: the oldest found was 2026-08-01),
so the only way to have this history later is to keep a copy now. Once a day this job asks each provider's current
version and downloads the zip only if that (provider, version) is not stored yet: at most PROVIDERS + PROVIDERS
small requests a day, one at a time, MIN_INTERVAL apart. The bytes are stored verbatim with their sha256, size and
Last-Modified.

Licence: gpuhunt's code is MPL-2.0 (its LICENSE file). The bucket states no separate licence for the catalogue
data, which is mostly the providers' own public list prices. Recorded as LICENCE on every snapshot; treat the
data as reference material with attribution to dstack until its terms are clearer.
"""
from __future__ import annotations

import hashlib
import logging
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

import normalize
from backfill.wayback import USER_AGENT
from config import settings
from jobs import job
from store.archive import CatalogSnapshot

log = logging.getLogger(__name__)

SOURCE = "gpuhunt"
CATALOG_URL = "https://dstack-gpu-pricing.s3.eu-west-1.amazonaws.com/v3"
# gpuhunt's OFFLINE_PROVIDERS (src/gpuhunt/_internal/catalog.py, 2026-10-29): the ones published to the bucket.
PROVIDERS = ("aws", "azure", "verda", "gcp", "lambdalabs", "nebius", "oci", "runpod", "cloudrift")
LICENCE = "gpuhunt code MPL-2.0; data: no licence stated"
NOTE = "dstack gpuhunt offline catalogue (github.com/dstackai/gpuhunt), stored verbatim; raw only"
MIN_INTERVAL = 2.0
MAX_BYTES = 50 * 1024 * 1024


def _client() -> httpx.Client:
    return httpx.Client(headers={"user-agent": USER_AGENT}, timeout=httpx.Timeout(30, read=120),
                        follow_redirects=True)


def _stored(provider: str, version: str) -> bool:
    with normalize.SessionLocal() as s:
        return s.execute(select(CatalogSnapshot.id).where(
            CatalogSnapshot.source == SOURCE, CatalogSnapshot.name == provider,
            CatalogSnapshot.version == version)).first() is not None


def snapshot(*, client: httpx.Client | None = None, providers=PROVIDERS, min_interval: float = MIN_INTERVAL) -> dict:
    """One pass over every provider. Returns {provider: "stored <version>" | "unchanged <version>" | "error: ..."}."""
    c = client or _client()
    out, last = {}, 0.0

    def get(url: str) -> httpx.Response:
        nonlocal last
        wait = last + min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        last = time.monotonic()
        r = c.get(url)
        r.raise_for_status()
        return r

    for p in providers:
        try:
            version = get(f"{CATALOG_URL}/{p}/version").text.strip()
            if not version or len(version) > 64 or "/" in version:
                raise ValueError(f"unexpected version {version[:80]!r}")
            if _stored(p, version):
                out[p] = f"unchanged {version}"
                continue
            url = f"{CATALOG_URL}/{p}/{version}/catalog.zip"
            r = get(url)
            body = r.content
            if not body.startswith(b"PK") or len(body) > MAX_BYTES:
                raise ValueError(f"not a zip of sane size ({len(body)} bytes)")
            modified = r.headers.get("last-modified")
            row = {"source": SOURCE, "name": p, "version": version, "url": url,
                   "fetched_at": datetime.now(timezone.utc),
                   "published_at": parsedate_to_datetime(modified) if modified else None,
                   "sha256": hashlib.sha256(body).hexdigest(), "size": len(body),
                   "content_type": r.headers.get("content-type"), "licence": LICENCE, "note": NOTE, "body": body}
            with normalize.SessionLocal.begin() as s:
                s.execute(insert(CatalogSnapshot).values(row).on_conflict_do_nothing(
                    constraint="uq_catalog_snapshot_version"))
            out[p] = f"stored {version}"
        except Exception as exc:
            out[p] = f"error: {type(exc).__name__}: {exc}"[:300]
            log.warning("gpuhunt snapshot %s: %s", p, out[p])
    return out


@job("gpuhunt_snapshot", every_seconds=86400, initial_delay_seconds=900)
def _gpuhunt_job():
    if not settings.gpuhunt_snapshot_enabled:
        return {"skipped": "gpuhunt_snapshot_enabled is False"}
    result = snapshot()
    errors = {k: v for k, v in result.items() if v.startswith("error")}
    if errors and len(errors) == len(result):
        raise RuntimeError(f"gpuhunt: every provider failed: {errors}")
    return result
