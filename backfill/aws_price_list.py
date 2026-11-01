"""AWS's own archive of EC2 price lists: one version per month, each valid for a stated period.

    versions(since)          -> [Version]          monthly versions whose effective period ends after `since`
    gpu_rows(version)        -> [row dict]          us-east-1 Linux shared-tenancy on-demand GPU instance prices
    listings(version, rows)  -> [ComputeListing]    the same listings providers/aws.py builds from the live file

The live poller (providers/aws.py) reads the current us-east-1 Linux on-demand file; this reads AWS's published
historical versions of the same price list (https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/
index.json) and applies the same filter, the same GPU_INSTANCES table and the same per-GPU division. Each us-east-1
CSV is ~280 MB, so it is streamed and only the GPU rows are kept (cached on disk as JSON, a few KB).

A version's prices are AWS's list prices for its effective period [begin, end): observed_at is the period start,
valid_to the period end (null for the current version).
"""
from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx

import canonical
from backfill.wayback import DEFAULT_CACHE, USER_AGENT
from models import ComputeListing
from providers.aws import GPU_INSTANCES, REGION, REGION_NAME

BASE = "https://pricing.us-east-1.amazonaws.com"
VERSION_INDEX = "/offers/v1.0/aws/AmazonEC2/index.json"
PARSER = "backfill.aws_price_list (providers/aws.py GPU_INSTANCES)"

# The live file is "ec2-ondemand-without-sec-sel / Linux": on-demand, Linux, shared tenancy, no extra software.
_WANT = {"TermType": "OnDemand", "Unit": "Hrs", "Tenancy": "Shared", "Operating System": "Linux",
         "Pre Installed S/W": "NA", "CapacityStatus": "Used", "License Model": "No License required"}


@dataclass(frozen=True)
class Version:
    version: str
    begin: datetime
    end: datetime | None

    @property
    def csv_url(self) -> str:
        return f"{BASE}/offers/v1.0/aws/AmazonEC2/{self.version}/{REGION}/index.csv"


def _ts(s: str) -> datetime | None:
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def _client() -> httpx.Client:
    return httpx.Client(headers={"user-agent": USER_AGENT}, timeout=httpx.Timeout(60, read=300), follow_redirects=True)


def versions(since: datetime, client: httpx.Client | None = None) -> list[Version]:
    c = client or _client()
    r = c.get(BASE + VERSION_INDEX)
    r.raise_for_status()
    out = []
    for v, meta in r.json()["versions"].items():
        begin, end = _ts(meta["versionEffectiveBeginDate"]), _ts(meta.get("versionEffectiveEndDate") or "")
        if begin is not None and (end is None or end > since):
            out.append(Version(v, begin, end))
    return sorted(out, key=lambda x: x.begin)


def parse_csv(lines) -> list[dict]:
    """GPU rows from an EC2 price-list CSV (an iterable of text lines; the 5 metadata lines come first)."""
    it = iter(lines)
    for _ in range(5):
        next(it)
    rows = []
    for row in csv.DictReader(it):
        if row.get("Instance Type") not in GPU_INSTANCES or row.get("Location") != REGION_NAME:
            continue
        if any(row.get(k) != v for k, v in _WANT.items()):
            continue
        rows.append({k: row.get(k) for k in ("Instance Type", "PricePerUnit", "vCPU", "Memory", "EffectiveDate",
                                              "SKU", "RateCode")})
    return rows


def gpu_rows(version: Version, cache_dir: Path | None = None, client: httpx.Client | None = None) -> list[dict]:
    path = Path(cache_dir or DEFAULT_CACHE) / f"aws-ec2-{version.version}-{REGION}-gpu.json"
    if path.exists():
        return json.loads(path.read_text())
    c = client or _client()
    with c.stream("GET", version.csv_url) as r:
        r.raise_for_status()
        rows = parse_csv(io.TextIOWrapper(_Chunks(r.iter_bytes()), encoding="utf-8", newline=""))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows))
    return rows


class _Chunks(io.RawIOBase):
    """A readable stream over an iterator of byte chunks (lets csv read a streamed HTTP body line by line)."""

    def __init__(self, chunks):
        self._chunks, self._buf = iter(chunks), b""

    def readable(self):
        return True

    def readinto(self, b):
        while not self._buf:
            try:
                self._buf = next(self._chunks)
            except StopIteration:
                return 0
        n = min(len(b), len(self._buf))
        b[:n], self._buf = self._buf[:n], self._buf[n:]
        return n


def listings(version: Version, rows: list[dict]) -> tuple[list[ComputeListing], list[str]]:
    """One listing per instance type, priced as providers/aws.py prices the live file. An instance type with
    two different prices in one version is skipped (we cannot tell which applied) and reported."""
    by_type: dict[str, set[Decimal]] = {}
    spec_row: dict[str, dict] = {}
    for r in rows:
        try:
            p = Decimal(r["PricePerUnit"])
        except Exception:
            continue
        if p > 0:
            by_type.setdefault(r["Instance Type"], set()).add(p)
            spec_row[r["Instance Type"]] = r
    out, problems = [], []
    for itype, prices in sorted(by_type.items()):
        if len(prices) > 1:
            problems.append(f"{itype}: {len(prices)} different prices in version {version.version}; skipped")
            continue
        price = prices.pop()
        count, raw_name = GPU_INSTANCES[itype]
        r = spec_row[itype]
        mem = (r.get("Memory") or "").removesuffix(" GiB")
        out.append(ComputeListing(
            provider="aws", sku=itype, listing_id=f"{itype}:{REGION}", raw_gpu_name=raw_name,
            canonical_gpu_name=canonical.canonical_gpu_name(raw_name), gpu_count=count, region=REGION,
            country="US", price_per_gpu_hour=price / count, price_per_instance_hour=price, currency="USD",
            market_type="on_demand", provider_tier="hyperscaler", interruptible=False, available=None,
            capacity=None, capacity_unit=None, vcpu=int(r["vCPU"]) if (r.get("vCPU") or "").isdigit() else None,
            ram_gb=float(mem) if mem.replace(".", "", 1).isdigit() else None, storage_gb=None,
            observed_at=version.begin.astimezone(timezone.utc)))
    return out, problems
