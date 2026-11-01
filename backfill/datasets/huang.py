"""Huang (2026), "GPU Rental Value Curves: replication data for 'Underwriting GPU Depreciation'", Zenodo
doi:10.5281/zenodo.22053861 (v1.6.1, CC BY 4.0 for everything under data/; the bundled Ornn OCPI file is third-party
data under other terms and is never read). Vast.ai marketplace, monthly, from 2022-10.

    download()        -> Path            the release zip, cached (one GET; Zenodo's md5 is checked)
    parse(csv_text)   -> [ComputeListing] one per (GPU, calendar month) from data/value_curves_realized.csv
    run(...)          -> report

The value read is `rental_usd_per_gpu_hour`: "Count-weighted median transacted rate for rented, verified machines,
USD per GPU-hour, renter-side and fee-inclusive" (the release's DATA_DICTIONARY.md). That is a monthly MEDIAN OF
TRANSACTED RATES, not a posted on-demand price, and it pools Vast's on-demand and interruptible rentals. So it is
stored as its own market_type, "transacted", with basis monthly_median: it is counted in coverage and can be read
alongside the archived series, but it never enters the on-demand (or spot) archived series. observed_at is the
month's first day and valid_to the next month's first day: the value describes the whole month.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx

from backfill.datasets import CACHE, dedupe, names
from backfill.wayback import USER_AGENT
from models import ComputeListing

SOURCE = "huang_vast_monthly"
RECORD = "22053861"
DOI = "10.5281/zenodo.22053861"
API = f"https://zenodo.org/api/records/{RECORD}"
MEMBER = "data/value_curves_realized.csv"
LICENCE = "CC-BY-4.0"
ATTRIBUTION = ("Huang, J. (2026). GPU Rental Value Curves: replication data for \"Underwriting GPU Depreciation.\" "
               f"Zenodo, doi:{DOI}. CC BY 4.0. Values used unchanged (rental_usd_per_gpu_hour); OpenGrid mapped "
               "the GPU labels to its own names.")
PARSER = "backfill.datasets.huang.parse"
MARKET = "transacted"


def download(cache_dir: Path | None = None, client: httpx.Client | None = None) -> Path:
    d = Path(cache_dir or CACHE) / "zenodo" / RECORD
    d.mkdir(parents=True, exist_ok=True)
    have = sorted(d.glob("*.zip"))
    if have:
        return have[0]
    c = client or httpx.Client(headers={"user-agent": USER_AGENT}, timeout=120, follow_redirects=True)
    meta = c.get(API)
    meta.raise_for_status()
    (d / "record.json").write_text(meta.text)
    f = next(x for x in meta.json()["files"] if x["key"].endswith(".zip"))
    r = c.get(f["links"]["self"])
    r.raise_for_status()
    algo, _, want = f.get("checksum", "").partition(":")
    if algo == "md5" and hashlib.md5(r.content).hexdigest() != want:
        raise ValueError(f"huang: {f['key']} md5 mismatch")
    path = d / f["key"]
    path.write_bytes(r.content)
    return path


def read_member(zip_path: Path) -> str:
    with zipfile.ZipFile(zip_path) as z:
        name = next(n for n in z.namelist() if n.endswith(MEMBER))
        return z.read(name).decode("utf-8")


def _month(s: str) -> tuple[datetime, datetime]:
    start = datetime.strptime(s.strip(), "%Y-%m").replace(tzinfo=timezone.utc)
    end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    return start, end


def parse(text: str) -> list[tuple[ComputeListing, datetime]]:
    """(listing, month end) per row; rows with no positive rate are skipped."""
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        label = (row.get("gpu_name") or "").strip()
        try:
            rate = Decimal(row.get("rental_usd_per_gpu_hour") or "")
        except InvalidOperation:
            continue
        if rate <= 0 or label not in names.HUANG_GPUS:
            continue
        start, end = _month(row["calendar_month"])
        out.append((ComputeListing(
            provider="vast", sku=label, listing_id=f"{label}:monthly_median:{start:%Y-%m}", raw_gpu_name=label,
            canonical_gpu_name=names.HUANG_GPUS[label], gpu_count=1, region=None, country=None,
            price_per_gpu_hour=rate.quantize(Decimal("0.000001")), price_per_instance_hour=None, currency="USD",
            market_type=MARKET, provider_tier=None, interruptible=None, available=None, capacity=None,
            capacity_unit=None, vcpu=None, ram_gb=None, storage_gb=None, observed_at=start), end))
    return out


def rows(text: str, zip_sha256: str) -> list[dict]:
    from backfill import load

    out = []
    for listing, end in parse(text):
        got, _ = load._rows([listing], source=SOURCE, source_url=f"https://doi.org/{DOI}#{MEMBER}",
                            sha256=zip_sha256, parser=PARSER, valid_to=end, basis="monthly_median",
                            licence=LICENCE, attribution=ATTRIBUTION,
                            note=f"Vast.ai count-weighted median transacted rate, {listing.observed_at:%Y-%m}, "
                                 "rented verified machines, renter-side incl. fees (on-demand and interruptible "
                                 "pooled); not a posted price")
        out += got
    return out


def run(*, zip_path: Path | None = None, dry_run: bool = False, log=print) -> dict:
    zip_path = zip_path or download()
    text = read_member(zip_path)
    got = rows(text, hashlib.sha256(zip_path.read_bytes()).hexdigest())
    result = dedupe.store(got, dry_run=dry_run)
    by_gpu: dict[str, int] = {}
    for r in got:
        by_gpu[r["raw_gpu_name"]] = by_gpu.get(r["raw_gpu_name"], 0) + 1
    log(f"huang: {len(got)} monthly points {json.dumps(by_gpu)}; {result}")
    return {"source": SOURCE, "rows": len(got), "by_gpu": by_gpu, **result,
            "first": min((r["observed_at"] for r in got), default=None)}
