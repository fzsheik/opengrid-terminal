"""AWS's own archive of EC2 price lists: one version per month, each valid for a stated period.

    versions(since)          -> [Version]          monthly versions whose effective period ends after `since`
    gpu_rows(version)        -> [row dict]          us-east-1 Linux shared-tenancy GPU instance prices: on-demand
                                                    AND reserved (1 / 3-year) rows, cached on disk
    listings(version, rows)  -> [ComputeListing]    on-demand: the same listings providers/aws.py builds live
    reserved_listings(...)   -> [ComputeListing]    1 / 3-year reserved, market_type reserved_1y / reserved_3y

The live poller (providers/aws.py) reads the current us-east-1 Linux on-demand file; this reads AWS's published
historical versions of the same price list (https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/
index.json) and applies the same filter, the same GPU_INSTANCES table and the same per-GPU division. Each us-east-1
CSV is ~280 MB, so it is streamed and only the GPU rows are kept (cached on disk as JSON, a few KB).

A version's prices are AWS's list prices for its effective period [begin, end): observed_at is the period start,
valid_to the period end (null for the current version).

Older files (2015-12 to 2018) have fewer columns. A filter column the file does not have is not a reason to drop
the row: CapacityStatus (added in 2018; "Used" is the only status a normal instance-hour has) is treated as "Used"
when absent, OfferingClass (added 2016-09 with convertible RIs) as "standard". The 2017-11 version gives
p3.2xlarge $3.06, the price AWS announced at the P3 launch.

Reserved instances (data kind archived, never on-demand). Each 1 / 3-year offer is a pair of rows with the same
(term, purchase option, offering class): an upfront fee (Unit "Quantity") and a recurring hourly rate (Unit "Hrs").
The effective hourly price over the term is  hourly + upfront / (8,760 h x years)  (reserved.py's 8,760). Stored as
market_type reserved_1y / reserved_3y, one listing per (type, term, class, purchase option); analytics/archived.py
reads one market_type at a time and the API offers only on_demand / spot, so they can never mix with on-demand.

BACKFILL_INSTANCES adds instance types the live poller does not list (retired or not tracked: p2, g2, g3, p3dn).
Their GPUs have no canonical name yet, so they are stored with gpu = null (kept, not charted) like any unmapped GPU.
"""
from __future__ import annotations

import csv
import io
import json
import time
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
RESERVED_PARSER = "backfill.aws_price_list.reserved_listings"
LICENCE = "AWS public price list"
CACHE_VERSION = 2          # bump when the cached row selection changes; old caches are then re-downloaded
POLITE_SECONDS = 2.0       # between two CSV downloads (each is one streamed GET; AWS publishes them for this)
HOURS_PER_YEAR = Decimal(8760)

# Instance types the live poller does not track (older generations), for history only. GPU counts from AWS's
# instance-type pages; the names are not in canonical.py, so these rows are stored unmapped.
BACKFILL_INSTANCES: dict[str, tuple[int, str]] = {
    **GPU_INSTANCES,
    "p2.xlarge": (1, "NVIDIA K80"), "p2.8xlarge": (8, "NVIDIA K80"), "p2.16xlarge": (16, "NVIDIA K80"),
    "g2.2xlarge": (1, "NVIDIA GRID K520"), "g2.8xlarge": (4, "NVIDIA GRID K520"),
    "g3s.xlarge": (1, "NVIDIA Tesla M60"), "g3.4xlarge": (1, "NVIDIA Tesla M60"),
    "g3.8xlarge": (2, "NVIDIA Tesla M60"), "g3.16xlarge": (4, "NVIDIA Tesla M60"),
    "p3dn.24xlarge": (8, "NVIDIA Tesla V100-SXM2-32GB"),
}

# The live file is "ec2-ondemand-without-sec-sel / Linux": on-demand, Linux, shared tenancy, no extra software.
# Columns a file lacks take these defaults (see the module docstring), every other column must be present.
_COMMON = {"Tenancy": "Shared", "Operating System": "Linux", "Pre Installed S/W": "NA", "CapacityStatus": "Used",
           "License Model": "No License required"}
_DEFAULTS = {"CapacityStatus": "Used", "OfferingClass": "standard"}
_WANT = {"TermType": "OnDemand", "Unit": "Hrs", **_COMMON}
_KEEP = ("Instance Type", "TermType", "PricePerUnit", "Unit", "LeaseContractLength", "PurchaseOption",
         "OfferingClass", "vCPU", "Memory", "EffectiveDate", "SKU", "RateCode", "CapacityStatus")


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


def _value(row: dict, key: str) -> str | None:
    """A row's value; a column the file does not have takes its documented default (None = no default)."""
    v = row.get(key)
    return _DEFAULTS.get(key) if v is None else v


def parse_csv(lines, meta: dict | None = None) -> list[dict]:
    """GPU rows from an EC2 price-list CSV (an iterable of text lines; the 5 metadata lines come first): every
    us-east-1 Linux shared-tenancy row of a BACKFILL_INSTANCES type, on-demand or reserved. `meta`, when given,
    receives the metadata lines (Publication Date, Version)."""
    it = iter(lines)
    for _ in range(5):
        head = next(it)
        if meta is not None:
            parts = next(csv.reader([head]), [])
            if len(parts) >= 2:
                meta[parts[0]] = parts[1]
    rows = []
    for row in csv.DictReader(it):
        if row.get("Instance Type") not in BACKFILL_INSTANCES or row.get("Location") != REGION_NAME:
            continue
        if row.get("TermType") not in ("OnDemand", "Reserved"):
            continue
        if any(_value(row, k) != v for k, v in _COMMON.items()):
            continue
        rows.append({k: _value(row, k) for k in _KEEP})
    return rows


def on_demand(rows: list[dict]) -> list[dict]:
    """The rows the live poller would read (parse_csv already applied the shared filters)."""
    return [r for r in rows if r.get("TermType") == _WANT["TermType"] and r.get("Unit") == _WANT["Unit"]]


def _cache_path(version: Version, cache_dir: Path | None) -> Path:
    return Path(cache_dir or DEFAULT_CACHE) / f"aws-ec2-{version.version}-{REGION}-gpu-v{CACHE_VERSION}.json"


def cached(version: Version, cache_dir: Path | None = None) -> dict | None:
    path = _cache_path(version, cache_dir)
    return json.loads(path.read_text()) if path.exists() else None


def gpu_document(version: Version, cache_dir: Path | None = None, client: httpx.Client | None = None) -> dict:
    """{"version", "publication_date", "rows"} for one version: from the disk cache, else one streamed download
    (the CSV itself is never written to disk; only its GPU rows are kept)."""
    doc = cached(version, cache_dir)
    if doc is not None:
        return doc
    c = client or _client()
    meta: dict = {}
    for attempt in range(4):
        try:
            with c.stream("GET", version.csv_url) as r:
                if r.status_code in (429, 500, 502, 503, 504):
                    raise httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
                r.raise_for_status()
                rows = parse_csv(io.TextIOWrapper(_Chunks(r.iter_bytes()), encoding="utf-8", newline=""), meta)
            break
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if attempt == 3 or (status is not None and status not in (429, 500, 502, 503, 504)):
                raise
            time.sleep(10 * 2 ** attempt)
    doc = {"version": version.version, "publication_date": meta.get("Publication Date"), "rows": rows}
    path = _cache_path(version, cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc))
    time.sleep(POLITE_SECONDS)
    return doc


def gpu_rows(version: Version, cache_dir: Path | None = None, client: httpx.Client | None = None) -> list[dict]:
    return gpu_document(version, cache_dir, client)["rows"]


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
    for r in on_demand(rows):
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
        count, raw_name = BACKFILL_INSTANCES[itype]
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


_TERM_YEARS = {"1yr": 1, "3yr": 3}


def reserved_listings(version: Version, rows: list[dict]) -> tuple[list[ComputeListing], list[str]]:
    """One listing per (instance type, term, offering class, purchase option): the effective hourly price over the
    term, hourly + upfront / (8,760 h x years). market_type reserved_1y / reserved_3y. An offer whose upfront or
    hourly part appears with two different prices is skipped and reported, never guessed."""
    parts: dict[tuple, dict[str, set[Decimal]]] = {}
    spec_row: dict[str, dict] = {}
    for r in rows:
        if r.get("TermType") != "Reserved" or r.get("LeaseContractLength") not in _TERM_YEARS:
            continue
        try:
            p = Decimal(r["PricePerUnit"])
        except Exception:
            continue
        unit = {"Hrs": "hourly", "Quantity": "upfront"}.get(r.get("Unit") or "")
        if unit is None:
            continue
        key = (r["Instance Type"], r["LeaseContractLength"], (r.get("OfferingClass") or "standard").lower(),
               r.get("PurchaseOption") or "")
        parts.setdefault(key, {}).setdefault(unit, set()).add(p)
        spec_row[r["Instance Type"]] = r
    out, problems = [], []
    for (itype, term, cls, option), got in sorted(parts.items()):
        if any(len(v) > 1 for v in got.values()):
            problems.append(f"{itype} {term} {cls} {option}: several prices in version {version.version}; skipped")
            continue
        years = _TERM_YEARS[term]
        hourly = next(iter(got.get("hourly", {Decimal(0)})))
        upfront = next(iter(got.get("upfront", {Decimal(0)})))
        if "hourly" not in got and "upfront" not in got:
            continue
        price = hourly + upfront / (HOURS_PER_YEAR * years)
        if price <= 0:
            continue
        count, raw_name = BACKFILL_INSTANCES[itype]
        r = spec_row[itype]
        mem = (r.get("Memory") or "").removesuffix(" GiB")
        slug = option.lower().replace(" ", "_")
        out.append(ComputeListing(
            provider="aws", sku=itype, listing_id=f"{itype}:{REGION}:{term}:{cls}:{slug}", raw_gpu_name=raw_name,
            canonical_gpu_name=canonical.canonical_gpu_name(raw_name), gpu_count=count, region=REGION,
            country="US", price_per_gpu_hour=price / count, price_per_instance_hour=price, currency="USD",
            market_type=f"reserved_{years}y", provider_tier="hyperscaler", interruptible=False, available=None,
            capacity=None, capacity_unit=None, vcpu=int(r["vCPU"]) if (r.get("vCPU") or "").isdigit() else None,
            ram_gb=float(mem) if mem.replace(".", "", 1).isdigit() else None, storage_gb=None,
            observed_at=version.begin.astimezone(timezone.utc)))
    return out, problems


def reserved_note(listing: ComputeListing, rows: list[dict]) -> str:
    """The offer's own terms, so the effective price can be re-derived from the row."""
    itype, _, term, cls, slug = listing.listing_id.split(":")
    got = {}
    for r in rows:
        if (r.get("TermType") == "Reserved" and r["Instance Type"] == itype and r.get("LeaseContractLength") == term
                and (r.get("OfferingClass") or "standard").lower() == cls
                and (r.get("PurchaseOption") or "").lower().replace(" ", "_") == slug):
            got[r.get("Unit")] = r["PricePerUnit"]
    return (f"reserved {term} {cls} {slug.replace('_', ' ')}: upfront ${got.get('Quantity', '0')} + "
            f"${got.get('Hrs', '0')}/h; effective = hourly + upfront / (8760 h x {_TERM_YEARS[term]})")
