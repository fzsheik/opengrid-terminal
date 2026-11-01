"""Official / licensed price-history backfill (backfill/aws_price_list.py, azure_since.py, datasets/, gpuhunt.py,
coverage.py; methodology/archived-history.md). Fixtures only, no network.

Run:  .venv/bin/python tests/test_backfill_official.py
"""

import json
import sys
import tempfile
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import scratchdb  # noqa: E402  (first: isolates the process from real networks)
import httpx  # noqa: E402
from sqlalchemy import text  # noqa: E402

import fixtures  # noqa: E402
import normalize  # noqa: E402
from analytics import archived  # noqa: E402
from backfill import aws_price_list, azure_since, load  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_backfill"
DB = "og_test_backfill_official"
AWS_2017 = FIX / "aws_ec2_2017-11_us-east-1_p3.csv"
V2017 = aws_price_list.Version("20171128215034", datetime(2017, 11, 1, tzinfo=timezone.utc),
                               datetime(2017, 12, 1, tzinfo=timezone.utc))


def _db():
    url = scratchdb.create(DB)
    normalize.SessionLocal = fixtures.session(url)
    return url


def _q(sql, **kw):
    with normalize.SessionLocal() as s:
        return s.execute(text(sql), kw).all()


# ---------------------------------------------------------------------------------------------------------------
# 1. AWS price-list archive
# ---------------------------------------------------------------------------------------------------------------

def test_aws_2017_file_without_capacity_status_is_read():
    lines = AWS_2017.read_text().splitlines(keepends=True)
    assert "CapacityStatus" not in lines[5], "the fixture is a real pre-2018 header"
    meta = {}
    rows = aws_price_list.parse_csv(lines, meta)
    assert meta["Publication Date"] == "2017-11-28T21:50:34Z" and meta["Version"] == "20171128215034"
    assert rows and all(r["CapacityStatus"] == "Used" for r in rows), "an absent CapacityStatus reads as Used"
    assert not any(r["Instance Type"] == "c5.large" for r in rows), "non-GPU types are dropped"
    ls, problems = aws_price_list.listings(V2017, rows)
    assert not problems and [(l.sku, l.price_per_gpu_hour) for l in ls] == [("p3.2xlarge", D("3.06"))], ls
    assert ls[0].canonical_gpu_name == "NVIDIA Tesla V100 16GB SXM2" and ls[0].market_type == "on_demand"
    # Windows BYOL is also $3.06 and Dedicated $3.259: neither may leak in (one price, no "two prices" skip)
    res, rproblems = aws_price_list.reserved_listings(V2017, rows)
    assert not rproblems
    by = {l.listing_id: l for l in res}
    a1 = by["p3.2xlarge:us-east-1:1yr:standard:all_upfront"]
    assert a1.market_type == "reserved_1y" and a1.price_per_instance_hour == D(17075) / D(8760)
    p3 = by["p3.2xlarge:us-east-1:3yr:standard:partial_upfront"]
    assert p3.market_type == "reserved_3y" and p3.price_per_instance_hour == D("0.617") + D(16204) / D(26280)
    assert by["p3.2xlarge:us-east-1:3yr:standard:no_upfront"].price_per_instance_hour == D("1.332")
    assert {l.market_type for l in res} == {"reserved_1y", "reserved_3y"}
    note = aws_price_list.reserved_note(a1, rows)
    assert "upfront $17075" in note and "8760 h x 1" in note


def test_aws_offering_class_absent_reads_as_standard():
    lines = AWS_2017.read_text().splitlines(keepends=True)
    head = lines[5].replace('"OfferingClass",', "")
    i = lines[5].split(",").index('"OfferingClass"')
    body = [",".join(c for j, c in enumerate(l.split(",")) if j != i) for l in lines[6:]]
    rows = aws_price_list.parse_csv(lines[:5] + [head] + body)
    res, _ = aws_price_list.reserved_listings(V2017, rows)
    assert res and all(":standard:" in l.listing_id for l in res)


def _aws_mock(csv_text: str):
    index = {"versions": {"20171128215034": {"versionEffectiveBeginDate": "2017-11-01T00:00:00Z",
                                             "versionEffectiveEndDate": "2017-12-01T00:00:00Z"}}}
    calls = []

    def handler(req):
        calls.append(req.url.path)
        if req.url.path.endswith("/AmazonEC2/index.json"):
            return httpx.Response(200, json=index)
        if req.url.path.endswith("/20171128215034/us-east-1/index.csv"):
            return httpx.Response(200, content=csv_text.encode())
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def test_aws_run_stores_on_demand_and_reserved_apart_with_provenance():
    _db()
    old = aws_price_list.POLITE_SECONDS
    aws_price_list.POLITE_SECONDS = 0
    try:
        client, calls = _aws_mock(AWS_2017.read_text())
        with tempfile.TemporaryDirectory() as cache:
            r = load.run_aws(datetime(2015, 12, 1, tzinfo=timezone.utc), cache_dir=Path(cache), client=client,
                             log=lambda m: None)
            assert r["read"] == 1 and r["inserted"] == 1 and r["reserved_inserted"] == 6, r
            n = len(calls)
            again = load.run_aws(datetime(2015, 12, 1, tzinfo=timezone.utc), cache_dir=Path(cache), client=client,
                                 log=lambda m: None)
            assert again["inserted"] == 0 and again["reserved_inserted"] == 0, "idempotent"
            assert sum(1 for c in calls[n:] if c.endswith(".csv")) == 0, "the second run reads the CSV from cache"
        rows = _q("SELECT market_type, price_per_gpu_hour, basis, licence, note, valid_to, source_url "
                  "FROM archived_prices ORDER BY market_type, price_per_gpu_hour")
        od = [x for x in rows if x.market_type == "on_demand"]
        assert len(od) == 1 and od[0].price_per_gpu_hour == D("3.06") and od[0].basis == "price_list_version"
        assert "version 20171128215034" in od[0].note and "published 2017-11-28T21:50:34Z" in od[0].note
        assert od[0].licence == aws_price_list.LICENCE and "/20171128215034/" in od[0].source_url
        assert od[0].valid_to == datetime(2017, 12, 1, tzinfo=timezone.utc)
        assert {x.market_type for x in rows} == {"on_demand", "reserved_1y", "reserved_3y"}
        assert all("upfront $" in x.note for x in rows if x.market_type != "on_demand")
        # The on-demand archived series never sees a reserved price
        a = archived.gpu_archived("NVIDIA Tesla V100 16GB SXM2")
        assert [p["price_per_gpu_hour"] for p in a["points"]] == [3.06], a["points"]
        assert archived.gpu_archived("NVIDIA Tesla V100 16GB SXM2", "spot")["points"] == []
    finally:
        aws_price_list.POLITE_SECONDS = old
        scratchdb.drop(DB)


# ---------------------------------------------------------------------------------------------------------------
# 2. Azure "effective since"
# ---------------------------------------------------------------------------------------------------------------

AZ = json.loads((FIX / "azure_retail_nd96asr_v4_2026-11.json").read_text())
READ = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)


def test_azure_effective_since_points():
    pts, problems = azure_since.points(AZ, READ)
    assert not problems
    by = {l.listing_id: (l, row) for l, row in pts}
    l, row = by["Standard_ND96asr_v4:westus2:since:2021-06-01"]
    assert l.price_per_instance_hour == D("27.197") and l.price_per_gpu_hour == D("27.197") / 8
    assert l.observed_at == datetime(2021, 6, 1, tzinfo=timezone.utc) and l.market_type == "on_demand"
    assert l.canonical_gpu_name == "NVIDIA A100 40GB SXM4"
    assert "Windows" not in row["productName"] and "Low Priority" not in row["skuName"]
    assert {l.market_type for l, _ in pts} == {"on_demand", "spot"}
    assert len(pts) == 4, "one per (size, region, market): Windows and Low Priority meters never read"


def test_azure_run_stores_one_labelled_point_each():
    _db()
    try:
        r = azure_since.run(items=AZ, read_at=READ, log=lambda m: None)
        assert r["inserted"] == 4 and azure_since.run(items=AZ, read_at=READ, log=lambda m: None)["inserted"] == 0
        rows = _q("SELECT * FROM archived_prices WHERE market_type = 'on_demand' ORDER BY observed_at")
        assert all(x.basis == "effective_since" and x.valid_to is None and x.source == "azure_retail" for x in rows)
        assert rows[0].source_url.startswith("https://prices.azure.com/api/retail/prices?%24filter=meterId%20eq%20")
        assert "effective since 2021-03-01" in rows[0].note and "read 2026-11-01" in rows[0].note
        a = archived.gpu_archived("NVIDIA A100 40GB SXM4")
        assert [p["t"][:10] for p in a["points"]] == ["2021-03-01", "2021-06-01"]
    finally:
        scratchdb.drop(DB)


# ---------------------------------------------------------------------------------------------------------------
# 3. Open datasets: FSDL (MIT) and Huang (CC BY)
# ---------------------------------------------------------------------------------------------------------------

_FSDL_HEAD = "Cloud,GPU Type,GPU Arch,GPUs,GPU RAM,vCPUs,RAM,On-demand,Per-GPU,Spot,Name\n"
_FSDL_1 = _FSDL_HEAD + (
    "AWS,V100 (16 GB),Volta,1,16,8,61,$3.06,$3.06,$0.92,p3.2xlarge\n"
    "Lambda,A100 (40 GB),Ampere,8,320,124,1800,$8.80,$1.10,,\n"
    "Oblivus Cloud,A100 (80 GB),Ampere,1,80,8,64,$2.55,$2.55,,\n")
_FSDL_2 = _FSDL_HEAD + (
    "AWS,V100 (16 GB),Volta,1,16,8,61,3.06,3.06,0.92,p3.2xlarge\n"          # "$" dropped: not a change
    "Lambda,A100 (40 GB),Ampere,8,320,124,1800,$10.32,$1.29,,\n"            # re-priced: a new version
    "Exoscale,A40 (48 GB),Ampere,1,48,12,56,,1.88,,GPU3 SMALL\n"
    "Cudo Compute,A4000 (16 GB),Ampere,1,16,2,4,0.30,0.29,,\n"
    "Cudo Compute,A4000 (16 GB),Ampere,1,16,4,8,0.32,0.30,,\n"              # same GPU, another machine size
    "Paperspace,A6000 (48 GB),Ampere,2,96,16,90,3.78,1.89,,\n"
    "Azure,A100 (80 GB),Ampere,1,80,24,220,3.67,3.67,,NC24ads A100 v4\n")
REVS = [("a" * 40, datetime(2022, 8, 5, 4, tzinfo=timezone.utc), _FSDL_1),
        ("b" * 40, datetime(2023, 5, 24, 9, tzinfo=timezone.utc), _FSDL_2)]


def test_fsdl_row_versions_names_and_listing_ids():
    from backfill.datasets import fsdl
    vs = fsdl.row_versions(REVS)
    assert [(sha[0], row["Cloud"]) for sha, _, row in vs] == [
        ("a", "AWS"), ("a", "Lambda"), ("a", "Oblivus Cloud"), ("b", "Lambda"), ("b", "Exoscale"),
        ("b", "Cudo Compute"), ("b", "Cudo Compute"), ("b", "Paperspace"), ("b", "Azure")], \
        "a row is stored where it appears or changes, never again for carrying it along"
    rows, info = fsdl.rows(REVS)
    by = {(r["provider"], r["listing_id"]): r for r in rows}
    assert info["skipped_untracked_clouds"] == {"Oblivus Cloud": 1}
    assert by[("aws", "p3.2xlarge:1:8vcpu:61gb")]["gpu"] == "NVIDIA Tesla V100 16GB SXM2", "AWS via its table"
    assert by[("aws", "p3.2xlarge:1:8vcpu:61gb:spot")]["market_type"] == "spot"
    assert by[("exoscale", "gpu3.small")]["gpu"] == "NVIDIA A40 48GB", "joins Exoscale's live listing id"
    assert by[("exoscale", "gpu3.small")]["price_per_gpu_hour"] == D("1.88"), "Per-GPU x GPUs when no On-demand"
    assert by[("paperspace", "A6000x2")]["gpu"] == "NVIDIA RTX A6000 48GB", "joins Paperspace's live id"
    assert len([k for k in by if k[0] == "cudo"]) == 2, "two CUDO machine sizes stay two listings"
    assert by[("azure", "NC24ads A100 v4:1:24vcpu:220gb")]["gpu"] == "NVIDIA A100 80GB PCIe", "Azure via GPU_SIZES"
    lam = [r for r in rows if r["provider"] == "lambda"]
    assert [r["gpu"] for r in lam] == [None, None], "A100 (40 GB) with no form factor stays unmapped"
    r = by[("aws", "p3.2xlarge:1:8vcpu:61gb")]
    assert r["licence"] == "MIT" and "Full Stack Deep Learning" in r["attribution"] and r["basis"] == "dataset_snapshot"
    assert r["source_url"].endswith(f"/blob/{'a' * 40}/docs/cloud-gpus/cloud-gpus.csv")


def test_fsdl_never_double_counts_a_primary_point():
    from backfill.datasets import fsdl
    _db()
    old = aws_price_list.POLITE_SECONDS
    aws_price_list.POLITE_SECONDS = 0
    try:
        # A primary AWS price-list row for p3.2xlarge covering 2022-08 (the FSDL commit date)
        v = aws_price_list.Version("20220801000000", datetime(2022, 8, 1, tzinfo=timezone.utc),
                                   datetime(2022, 9, 1, tzinfo=timezone.utc))
        rows = aws_price_list.parse_csv(AWS_2017.read_text().splitlines(keepends=True))
        ls, _ = aws_price_list.listings(v, rows)
        got, _ = load._rows(ls, source="aws_price_list", source_url="u-aws", sha256="0" * 64, parser="t",
                            valid_to=v.end, basis="price_list_version")
        assert load.store(got) == 1
        r = fsdl.run(revs=REVS, log=lambda m: None)
        assert r["matched_primary"] == 1 and r["corroborated"] == 1, r
        assert fsdl.run(revs=REVS, log=lambda m: None)["corroborated"] == 0, "corroboration is added once"
        aws = _q("SELECT source, corroboration FROM archived_prices WHERE provider = 'aws' AND market_type = 'on_demand'")
        assert len(aws) == 1 and aws[0].source == "aws_price_list", "one point per provider / listing / date"
        (c,) = aws[0].corroboration
        assert c["source"] == "fsdl_cloud_gpus" and c["price_per_gpu_hour"] == "3.06" and c["licence"] == "MIT"
        assert _q("SELECT count(*) FROM archived_prices WHERE provider = 'aws' AND market_type = 'spot'")[0][0] == 1, \
            "FSDL's spot row has no primary twin: stored"
    finally:
        aws_price_list.POLITE_SECONDS = old
        scratchdb.drop(DB)


def test_huang_monthly_medians_are_their_own_market():
    import zipfile
    from backfill.datasets import huang
    _db()
    try:
        with tempfile.TemporaryDirectory() as d:
            z = Path(d) / "gpu-rental-value-curves-v1.6.1.zip"
            with zipfile.ZipFile(z, "w") as f:
                f.writestr("release/data/value_curves_realized.csv",
                           (FIX / "huang_value_curves_realized_sample.csv").read_text())
                f.writestr("release/data/ocpi_daily_2026-05-12_to_2026-08-17.csv", "never,read\n")
            r = huang.run(zip_path=z, log=lambda m: None)
            assert r["inserted"] == 5 and huang.run(zip_path=z, log=lambda m: None)["inserted"] == 0
        rows = _q("SELECT * FROM archived_prices ORDER BY raw_gpu_name, observed_at")
        assert {x.market_type for x in rows} == {"transacted"} and {x.provider for x in rows} == {"vast"}
        a100 = [x for x in rows if x.raw_gpu_name == "A100 SXM4"]
        assert all(x.gpu is None for x in a100), "Vast pools 40/80GB A100 SXM4: unmapped, not guessed"
        assert a100[0].observed_at == datetime(2022, 10, 1, tzinfo=timezone.utc)
        assert a100[0].valid_to == datetime(2022, 11, 1, tzinfo=timezone.utc)
        assert a100[0].price_per_gpu_hour == D("1.434389")
        h = [x for x in rows if x.raw_gpu_name == "H100 SXM"]
        assert h[0].gpu == "NVIDIA H100 80GB SXM5" and h[0].licence == "CC-BY-4.0"
        assert "Huang" in h[0].attribution and "10.5281/zenodo.22053861" in h[0].attribution
        assert h[0].basis == "monthly_median" and "not a posted price" in h[0].note
        assert archived.gpu_archived("NVIDIA H100 80GB SXM5")["points"] == [], "never in the on-demand series"
    finally:
        scratchdb.drop(DB)


TESTS = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]

if __name__ == "__main__":
    for t in TESTS:
        t()
        print(t.__name__, "ok")
