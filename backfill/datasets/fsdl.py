"""Full Stack Deep Learning's cloud GPU table (github.com/full-stack-deep-learning/website,
docs/cloud-gpus/cloud-gpus.csv): every revision in its git history, from 2022-08-04. MIT licence
(docs/cloud-gpus/LICENSE.txt, Copyright (c) 2023 Full Stack Deep Learning, LLC); recorded on every point.

    revisions(repo)          -> [(sha, committed_at, csv text)]   first-parent commits that changed the file
    row_versions(revisions)  -> [(sha, committed_at, row)]        a row only where it was added or its prices changed
    listings(row, at)        -> [ComputeListing]                   on-demand, and spot where the row states one
    run(...)                 -> report

The table is maintained by hand through pull requests, so a commit date is when a row was PUBLISHED, not when the
price was checked. A row is stored once per version (where it appeared or a price changed); later commits that
merely carry it along are not new observations and add nothing (otherwise a row nobody updated would read as a
fresh price for years). Date = the commit's committer date on the default branch (--first-parent: when it landed).

Columns: Cloud, GPU Type, GPU Arch, GPUs, GPU RAM, vCPUs, RAM, On-demand (whole instance $/h), Per-GPU, Spot, Name.
Providers OpenGrid does not track are skipped and counted; GPU labels go through datasets/names.py.
"""
from __future__ import annotations

import csv
import hashlib
import io
import subprocess
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from backfill.datasets import CACHE, dedupe, names
from models import ComputeListing

SOURCE = "fsdl_cloud_gpus"
REPO_URL = "https://github.com/full-stack-deep-learning/website"
PATH = "docs/cloud-gpus/cloud-gpus.csv"
LICENCE = "MIT"
ATTRIBUTION = ("Full Stack Deep Learning, cloud-gpus.csv (github.com/full-stack-deep-learning/website), "
               "MIT License, Copyright (c) 2023 Full Stack Deep Learning, LLC")
PARSER = "backfill.datasets.fsdl"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def clone(repo: Path | None = None) -> Path:
    """A blobless clone in the cache (one download; later runs fetch only what is new)."""
    repo = Path(repo or CACHE / "fsdl-website")
    if (repo / ".git").exists() or (repo / "HEAD").exists():
        _git(repo, "fetch", "-q", "origin")
    else:
        repo.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", "--filter=blob:none", "--no-checkout", REPO_URL, str(repo)],
                       check=True)
    return repo


def revisions(repo: Path, ref: str = "origin/HEAD") -> list[tuple[str, datetime, str]]:
    try:
        _git(repo, "rev-parse", "--verify", "-q", ref)
    except subprocess.CalledProcessError:
        ref = "HEAD"
    log = _git(repo, "log", "--first-parent", "--format=%H%x09%cI", ref, "--", PATH)
    out = []
    for line in reversed(log.strip().splitlines()):
        sha, when = line.split("\t")
        try:
            body = _git(repo, "show", f"{sha}:{PATH}")
        except subprocess.CalledProcessError:
            continue   # the commit deleted the file
        out.append((sha, datetime.fromisoformat(when).astimezone(timezone.utc), body))
    return out


def _price(s: str | None) -> Decimal | None:
    s = (s or "").strip().replace("$", "").replace(",", "")
    if not s:
        return None
    try:
        p = Decimal(s)
    except InvalidOperation:
        return None
    return p if p > 0 else None


def _key(row: dict) -> tuple:
    # vCPUs and RAM too: some clouds (CUDO) list one GPU type and count at several machine sizes.
    return tuple(row.get(k, "").strip() for k in ("Cloud", "GPU Type", "GPUs", "Name", "vCPUs", "RAM"))


def _values(row: dict) -> tuple:
    return (_price(row.get("On-demand")), _price(row.get("Per-GPU")), _price(row.get("Spot")))


def row_versions(revs: list[tuple[str, datetime, str]]) -> list[tuple[str, datetime, dict]]:
    """Rows where they first appear or a price changes, in commit order."""
    last: dict[tuple, tuple] = {}
    out = []
    for sha, at, body in revs:
        for row in csv.DictReader(io.StringIO(body)):
            row = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
            k, v = _key(row), _values(row)
            if not k[0] or last.get(k) == v:
                continue
            last[k] = v
            out.append((sha, at, row))
    return out


def listings(row: dict, at: datetime) -> list[ComputeListing]:
    cloud, gpu_type, name = row.get("Cloud", ""), row.get("GPU Type", ""), row.get("Name", "")
    provider = names.FSDL_PROVIDERS.get(cloud)
    try:
        count = int(row.get("GPUs") or "")
    except ValueError:
        return []
    if provider is None or count <= 0:
        return []
    on_demand, per_gpu, spot = _values(row)
    instance = on_demand if on_demand is not None else (per_gpu * count if per_gpu is not None else None)
    gpu = names.fsdl_gpu(cloud, gpu_type, name)
    base = names.fsdl_listing_id(cloud, row, count)
    out = []
    for market, price in (("on_demand", instance), ("spot", spot)):
        if price is None:
            continue
        out.append(ComputeListing(
            provider=provider, sku=name or base, listing_id=base + (":spot" if market == "spot" else ""),
            raw_gpu_name=gpu_type, canonical_gpu_name=gpu, gpu_count=count, region=None, country=None,
            price_per_gpu_hour=price / count, price_per_instance_hour=price, currency="USD", market_type=market,
            provider_tier=None, interruptible=market == "spot", available=None, capacity=None, capacity_unit=None,
            vcpu=None, ram_gb=None, storage_gb=None, observed_at=at))
    return out


def rows(revs: list[tuple[str, datetime, str]]) -> tuple[list[dict], dict]:
    from backfill import load

    out, skipped, unmapped = [], {}, {}
    by_doc: dict[str, list] = {}
    for sha, at, row in row_versions(revs):
        ls = listings(row, at)
        if not ls and row.get("Cloud") not in names.FSDL_PROVIDERS:
            skipped[row.get("Cloud")] = skipped.get(row.get("Cloud"), 0) + 1
        by_doc.setdefault(sha, []).extend(ls)
    when = {sha: at for sha, at, _ in revs}
    body_sha = {sha: hashlib.sha256(body.encode()).hexdigest() for sha, _, body in revs}
    for sha, ls in by_doc.items():
        got, _ = load._rows(ls, source=SOURCE, source_url=f"{REPO_URL}/blob/{sha}/{PATH}", sha256=body_sha[sha],
                            parser=PARSER, basis="dataset_snapshot", licence=LICENCE, attribution=ATTRIBUTION,
                            note=lambda l, sha=sha: (
                                f"row added or re-priced in commit {sha[:12]} ({when[sha]:%Y-%m-%d}); the date is "
                                "when the row was published in the table, not when it was checked"
                                + (f"; provider tier {names.RESOLD_TIERS[l.provider]}"
                                   if l.provider in names.RESOLD_TIERS else "")))
        for r in got:
            if r["gpu"] is None:
                unmapped[r["raw_gpu_name"]] = unmapped.get(r["raw_gpu_name"], 0) + 1
        out += got
    return out, {"skipped_untracked_clouds": skipped, "unmapped_gpus": unmapped}


def run(*, repo: Path | None = None, revs=None, dry_run: bool = False, log=print) -> dict:
    if revs is None:
        revs = revisions(clone(repo))
    got, info = rows(revs)
    result = dedupe.store(got, dry_run=dry_run)
    report = {"source": SOURCE, "revisions": len(revs), "rows": len(got), **result, **info,
              "first": revs[0][1].isoformat() if revs else None}
    log(f"fsdl: {len(revs)} revisions -> {len(got)} row versions; {result}")
    return report
