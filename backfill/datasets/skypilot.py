"""SkyPilot's catalogue git history (github.com/skypilot-org/skypilot-catalog) -> archived points.

DISABLED. The catalogue repository has no LICENSE file (the parent skypilot repo is Apache-2.0, the catalogue repo
states nothing), so nothing is ingested until SkyPilot confirm terms. run() refuses unless BOTH
settings.skypilot_catalog_enabled is true AND settings.skypilot_catalog_licence names the licence they confirmed
(recorded on every point). The loader is complete and tested on a small sample so it can be switched on then.

    revisions(repo, cloud, version)   -> [(sha, committed_at, path, csv text)]  one per UTC day (that day's last
                                                                                commit touching the cloud's vms.csv)
    listings(cloud, csv_text, at)     -> [ComputeListing]                       GPU rows only, on-demand per region,
                                                                                spot per availability zone
    run(repo, clouds, ...)            -> report

The catalogue is refreshed by GitHub Actions (AWS, GCP, Lambda every ~7 h) and committed only when content changes,
so a commit date is a change date. Some clouds are hand-edited snapshots (Azure, OCI, IBM ...): their commit dates
are when someone updated the file, which is recorded as basis catalog_commit with that caveat in the note.

vms.csv columns differ by cloud and version; read by name: InstanceType, AcceleratorName, AcceleratorCount, Price,
SpotPrice, Region, AvailabilityZone (absent for some clouds). GPU names: AWS through BACKFILL_INSTANCES (the same
instance table as the price-list archive), GCP through its machine families, otherwise SKYPILOT_GPUS below, plus a
form factor spelled in the instance name; anything else stays unmapped.
"""
from __future__ import annotations

import csv
import hashlib
import io
import subprocess
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import canonical
from backfill.aws_price_list import BACKFILL_INSTANCES
from backfill.datasets import CACHE, dedupe, names
from config import settings
from models import ComputeListing

SOURCE = "skypilot_catalog"
REPO_URL = "https://github.com/skypilot-org/skypilot-catalog"
VERSION = "v7"
PARSER = "backfill.datasets.skypilot"
ATTRIBUTION = "SkyPilot catalog (github.com/skypilot-org/skypilot-catalog), SkyPilot authors"

# SkyPilot cloud directory -> OpenGrid provider id (clouds OpenGrid does not track are not listed: skipped).
CLOUDS = {"aws": "aws", "gcp": "gcp", "azure": "azure", "lambda": "lambda", "runpod": "runpod", "vast": "vast",
          "do": "digitalocean", "oci": "oracle", "ibm": "ibm", "ovhcloud": "ovhcloud", "scaleway": "scaleway",
          "nebius": "nebius", "hyperbolic": "hyperbolic", "hyperstack": "hyperstack", "mithril": "mithril",
          "verda": "verda"}

# SkyPilot AcceleratorName values that name one product. A100 / A100-80GB / H100 hide SXM vs PCIe: resolved only
# from the instance name (_FORM) or a provider table.
SKYPILOT_GPUS = {
    "T4": "NVIDIA T4 16GB", "L4": "NVIDIA L4 24GB", "A10G": "NVIDIA A10G 24GB", "A10": "NVIDIA A10 24GB PCIe",
    "L40S": "NVIDIA L40S 48GB", "L40": "NVIDIA L40 48GB", "A40": "NVIDIA A40 48GB",
    "RTXA6000": "NVIDIA RTX A6000 48GB", "RTXA5000": "NVIDIA RTX A5000 24GB", "RTXA4000": "NVIDIA RTX A4000 16GB",
    "RTX4090": "NVIDIA RTX 4090 24GB", "RTX3090": "NVIDIA RTX 3090 24GB", "RTX6000-Ada": "NVIDIA RTX 6000 Ada 48GB",
    "H200": "NVIDIA H200 141GB SXM5", "B200": "NVIDIA B200 180GB SXM", "GH200": "NVIDIA GH200 96GB",
    "H100-SXM": "NVIDIA H100 80GB SXM5", "MI300X": "AMD Instinct MI300X 192GB",
}
_FORM = {"A100": {"sxm": "NVIDIA A100 40GB SXM4", "pcie": "NVIDIA A100 40GB PCIe"},
         "A100-80GB": {"sxm": "NVIDIA A100 80GB SXM4", "pcie": "NVIDIA A100 80GB PCIe"},
         "H100": {"sxm": "NVIDIA H100 80GB SXM5", "pcie": "NVIDIA H100 80GB PCIe"}}


class Disabled(RuntimeError):
    pass


def enabled() -> bool:
    return bool(settings.skypilot_catalog_enabled and (settings.skypilot_catalog_licence or "").strip())


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def clone(repo: Path | None = None) -> Path:
    repo = Path(repo or CACHE / "skypilot-catalog")
    if (repo / ".git").exists() or (repo / "HEAD").exists():
        _git(repo, "fetch", "-q", "origin")
    else:
        repo.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", "--filter=blob:none", "--no-checkout", REPO_URL, str(repo)], check=True)
    return repo


def revisions(repo: Path, cloud: str, version: str = VERSION, ref: str = "origin/HEAD"):
    try:
        _git(repo, "rev-parse", "--verify", "-q", ref)
    except subprocess.CalledProcessError:
        ref = "HEAD"
    path = f"catalogs/{version}/{cloud}/vms.csv"
    log = _git(repo, "log", "--first-parent", "--format=%H%x09%cI", ref, "--", path)
    per_day: dict = {}
    for line in log.strip().splitlines():            # newest first: the first seen per day is that day's last
        sha, when = line.split("\t")
        at = datetime.fromisoformat(when).astimezone(timezone.utc)
        per_day.setdefault(at.date(), (sha, at))
    out = []
    for day in sorted(per_day):
        sha, at = per_day[day]
        try:
            out.append((sha, at, path, _git(repo, "show", f"{sha}:{path}")))
        except subprocess.CalledProcessError:
            continue
    return out


def _dec(s) -> Decimal | None:
    try:
        d = Decimal(str(s).strip())
    except (InvalidOperation, ValueError):
        return None
    return d if d > 0 else None


def gpu_name(cloud: str, instance: str, accel: str) -> str | None:
    if cloud == "aws" and instance in BACKFILL_INSTANCES:
        return canonical.canonical_gpu_name(BACKFILL_INSTANCES[instance][1])
    if cloud == "gcp":
        for pattern, gpu in names._GCP:
            if pattern.match(instance.lower()):
                return gpu
    if accel in SKYPILOT_GPUS:
        return SKYPILOT_GPUS[accel]
    if accel in _FORM:
        low = instance.lower()
        forms = [f for f in ("sxm", "pcie") if f in low]
        return _FORM[accel][forms[0]] if len(forms) == 1 else None
    return None


def listings(cloud: str, text: str, at: datetime) -> list[ComputeListing]:
    provider = CLOUDS.get(cloud)
    if provider is None:
        return []
    out, seen = [], set()
    for row in csv.DictReader(io.StringIO(text)):
        accel, instance = (row.get("AcceleratorName") or "").strip(), (row.get("InstanceType") or "").strip()
        count_d = _dec(row.get("AcceleratorCount"))
        if not accel or not instance or count_d is None or count_d != count_d.to_integral_value():
            continue   # CPU rows and fractional GPUs are not GPU listings
        count = int(count_d)
        region, zone = (row.get("Region") or "").strip(), (row.get("AvailabilityZone") or "").strip()
        gpu = gpu_name(cloud, instance, accel)
        for market, price, lid in (("on_demand", _dec(row.get("Price")), f"{instance}:{region}"),
                                   ("spot", _dec(row.get("SpotPrice")), f"{instance}:{zone or region}:spot")):
            if price is None or lid in seen:
                continue
            seen.add(lid)
            out.append(ComputeListing(
                provider=provider, sku=instance, listing_id=lid, raw_gpu_name=accel, canonical_gpu_name=gpu,
                gpu_count=count, region=region or None, country=None, price_per_gpu_hour=price / count,
                price_per_instance_hour=price, currency="USD", market_type=market, provider_tier=None,
                interruptible=market == "spot", available=None, capacity=None, capacity_unit=None, vcpu=None,
                ram_gb=None, storage_gb=None, observed_at=at))
    return out


def rows(cloud: str, revs, licence: str) -> list[dict]:
    from backfill import load

    out = []
    for sha, at, path, text in revs:
        got, _ = load._rows(listings(cloud, text, at), source=SOURCE, source_url=f"{REPO_URL}/blob/{sha}/{path}",
                            sha256=hashlib.sha256(text.encode()).hexdigest(), parser=PARSER, basis="catalog_commit",
                            licence=licence, attribution=ATTRIBUTION,
                            note=f"SkyPilot catalogue {path} at commit {sha[:12]} ({at:%Y-%m-%d}); a commit date is "
                                 "when the file changed (hand-edited clouds: when someone updated it)")
        out += got
    return out


def run(*, repo: Path | None = None, clouds=None, version: str = VERSION, dry_run: bool = False, log=print,
        force_for_tests: bool = False) -> dict:
    if not (enabled() or force_for_tests):
        raise Disabled("SkyPilot catalogue ingestion is disabled: no licence confirmed for skypilot-catalog "
                       "(set skypilot_catalog_enabled and skypilot_catalog_licence once SkyPilot reply)")
    licence = (settings.skypilot_catalog_licence or "test-only").strip()
    repo = clone(repo) if repo is None or not Path(repo).exists() else Path(repo)
    report = {"source": SOURCE, "clouds": {}}
    for cloud in clouds or sorted(CLOUDS):
        revs = revisions(repo, cloud, version)
        got = rows(cloud, revs, licence)
        result = dedupe.store(got, dry_run=dry_run)
        report["clouds"][cloud] = {"revisions": len(revs), "rows": len(got), **result,
                                   "first": revs[0][1].isoformat() if revs else None}
        log(f"skypilot: {cloud}: {len(revs)} days -> {len(got)} rows; {result}")
    return report
