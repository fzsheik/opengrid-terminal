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
from backfill import aws_price_list, load  # noqa: E402

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


TESTS = [test_aws_2017_file_without_capacity_status_is_read, test_aws_offering_class_absent_reads_as_standard,
         test_aws_run_stores_on_demand_and_reserved_apart_with_provenance]

if __name__ == "__main__":
    for t in TESTS:
        t()
        print(t.__name__, "ok")
