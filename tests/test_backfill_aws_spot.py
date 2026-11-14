"""AWS EC2 spot price history backfill (backfill/aws_spot.py, `python -m backfill aws-spot`;
methodology/archived-history.md). Recorded fixtures only, no network.

Fixtures (tests/fixtures_providers/aws_ec2_spot_hist_*.xml) are REAL DescribeSpotPriceHistory responses captured
2026-11-14 with the read-only key, one UTC-day window each, Linux/UNIX, trimmed by the request itself to
p5.48xlarge (8 x H100):
    aws_ec2_spot_hist_us-east-1_2026-11-12_p{1,2,3}.xml   MaxResults=10, so three pages chained by nextToken
    aws_ec2_spot_hist_us-east-1_2026-11-13_p{1,2,3}.xml   the next day, also three pages
    aws_ec2_spot_hist_us-east-2_2026-11-12_p1.xml         one page
Each window holds the price in force at 00:00 (stamped with AWS's own earlier time, e.g. 2026-11-11T23:00) plus
every change that day.

Run:  .venv/bin/python tests/test_backfill_aws_spot.py
"""

import json
import re
import sys
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import scratchdb  # noqa: E402  (first: isolates the process from real networks)
import httpx  # noqa: E402
from sqlalchemy import text  # noqa: E402

import fixtures  # noqa: E402
import normalize  # noqa: E402
import providers.aws as aws  # noqa: E402
from analytics import archived  # noqa: E402
from backfill import aws_spot  # noqa: E402
from backfill.__main__ import main as backfill_main  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_providers"
DB = "og_test_backfill_aws_spot"
KEY_ID, SECRET = "AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
NOW = datetime(2026, 11, 14, 1, 30, tzinfo=timezone.utc)
H100 = "NVIDIA H100 80GB SXM5"
EMPTY = ('<DescribeSpotPriceHistoryResponse xmlns="http://ec2.amazonaws.com/doc/2016-11-15/">'
         '<requestId>x</requestId><spotPriceHistorySet/><nextToken/></DescribeSpotPriceHistoryResponse>')


def _pages() -> dict[tuple[str, str, str | None], str]:
    """(region, StartTime, NextToken or None) -> the recorded page, chained by each page's own nextToken."""
    out = {}
    for f in sorted(FIX.glob("aws_ec2_spot_hist_*_p1.xml")):
        region, day = re.match(r"aws_ec2_spot_hist_(.+)_(\d{4}-\d{2}-\d{2})_p1\.xml", f.name).groups()
        token, n = None, 1
        while (page := FIX / f"aws_ec2_spot_hist_{region}_{day}_p{n}.xml").exists():
            body = page.read_text()
            out[(region, f"{day}T00:00:00Z", token)] = body
            m = re.search(r"<nextToken>([^<]+)</nextToken>", body)
            token, n = (m.group(1) if m else None), n + 1
    return out


class Ec2:
    """MockTransport for ec2.<region>.amazonaws.com: serves the recorded pages, an empty set for anything else."""

    def __init__(self):
        self.pages = _pages()
        self.calls: list[tuple[str, dict]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        region = request.url.host.split(".")[1]
        form = dict(urllib.parse.parse_qsl(request.content.decode()))
        assert form["Action"] == "DescribeSpotPriceHistory", form
        assert request.headers["authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/")
        self.calls.append((region, form))
        body = self.pages.get((region, form["StartTime"], form.get("NextToken")), EMPTY)
        return httpx.Response(200, text=body, headers={"content-type": "text/xml;charset=UTF-8"})

    def provider(self) -> aws.AwsProvider:
        aws.RETRY_DELAY = 0
        return aws.AwsProvider(httpx.AsyncClient(transport=httpx.MockTransport(self.handler)), KEY_ID, SECRET)


def _db():
    url = scratchdb.create(DB)
    normalize.SessionLocal = fixtures.session(url)
    return url


def _q(sql, **kw):
    with normalize.SessionLocal() as s:
        return s.execute(text(sql), kw).all()


def _payload(region: str, day: str) -> dict:
    """One recorded day through the live reader (all pages), as stored raw."""
    import asyncio

    t0 = datetime.fromisoformat(f"{day}T00:00:00+00:00")
    raw = asyncio.run(Ec2().provider().spot_history(region, t0, t0 + timedelta(days=1)))
    assert raw.ok, raw.error
    return raw.payload


# ---------------------------------------------------------------------------------------------------------------

def test_spot_history_pages_through_the_live_reader():
    import asyncio

    m = Ec2()
    t0 = datetime(2026, 11, 12, tzinfo=timezone.utc)
    raw = asyncio.run(m.provider().spot_history("us-east-1", t0, t0 + timedelta(days=1)))
    assert raw.ok and len(m.calls) == 3, (raw.error, m.calls)
    assert [c[1].get("NextToken") is None for c in m.calls] == [True, False, False], "pages chained by nextToken"
    form = m.calls[0][1]
    assert form["StartTime"] == "2026-11-12T00:00:00Z" and form["EndTime"] == "2026-11-13T00:00:00Z"
    assert form["ProductDescription.1"] == "Linux/UNIX" and form["MaxResults"] == "1000"
    assert set(v for k, v in form.items() if k.startswith("InstanceType.")) == set(aws.GPU_INSTANCES)
    rows = raw.payload["spotPriceHistorySet"]
    assert len(rows) == 26 and raw.payload["region"] == "us-east-1", len(rows)
    assert rows == sorted(rows, key=lambda r: (r["instanceType"], r["availabilityZone"], r["timestamp"]))
    # the live poller's window bookkeeping is untouched by a backfill read
    p = m.provider()
    asyncio.run(p.spot_history("us-east-1", t0, t0 + timedelta(days=1)))
    assert p._spot_since == {}


def test_day_rows_price_in_force_at_midnight():
    payload = _payload("us-east-1", "2026-11-12")
    rows = aws_spot.day_rows("us-east-1", date(2026, 11, 12), payload, "a" * 64)
    by = {r["listing_id"]: r for r in rows}
    assert sorted(by) == [f"p5.48xlarge:us-east-1{z}:spot" for z in "abcdef"], sorted(by)
    d = by["p5.48xlarge:us-east-1d:spot"]
    # in force at 00:00: 21.085900 set 2026-11-11T23:00 (not 21.1627 at 07:00, nor 21.5684 at 23:00 that day)
    assert d["price_per_instance_hour"] == D("21.085900") and d["price_per_gpu_hour"] == D("21.085900") / 8
    assert d["observed_at"] == datetime(2026, 11, 12, tzinfo=timezone.utc) and d["valid_to"] is None
    assert "set by AWS at 2026-11-11T23:00:00Z" in d["note"] and "4 changes later that day" in d["note"], d["note"]
    assert "low $21.162700, high $21.568400" in d["note"]
    # a change stamped exactly 00:00 is the price in force at 00:00
    assert by["p5.48xlarge:us-east-1f:spot"]["price_per_instance_hour"] == D("31.532200")
    assert {r["gpu"] for r in rows} == {H100} and {r["gpu_count"] for r in rows} == {8}
    for r in rows:
        assert r["source"] == "aws_spot_history" and r["basis"] == "provider_api_history"
        assert r["market_type"] == "spot" and r["interruptible"] is True and r["provider"] == "aws"
        assert r["visibility"] == "public" and r["country"] == "US" and r["licence"] == aws_spot.LICENCE
        assert r["source_url"] == ("https://ec2.us-east-1.amazonaws.com/?Action=DescribeSpotPriceHistory"
                                   "&ProductDescription=Linux/UNIX&StartTime=2026-11-12T00:00:00Z"
                                   "&EndTime=2026-11-13T00:00:00Z")


def test_listing_ids_and_visibility_match_the_live_spot_listings():
    payload = _payload("us-east-1", "2026-11-12")
    snap = SimpleNamespace(payload=payload, fetched_at=NOW, endpoint=aws.ec2_endpoint("us-east-1",
                                                                                      "DescribeSpotPriceHistory"))
    live = aws.spot_listings({snap.endpoint: [snap]})
    rows = aws_spot.day_rows("us-east-1", date(2026, 11, 12), payload, "a" * 64)
    assert {l.listing_id for l in live} == {r["listing_id"] for r in rows}
    assert {l.visibility for l in live} == {r["visibility"] for r in rows} == {"public"}
    assert {l.provider_tier for l in live} == {"hyperscaler"}


def test_days_to_read():
    now = datetime(2026, 11, 14, 17, 33, tzinfo=timezone.utc)
    days = aws_spot.days_to_read(now, 90, None)
    assert days[0] == date(2026, 8, 17) and days[-1] == date(2026, 11, 13) and len(days) == 89
    # a region recorded live from 2026-11-01 21:00: its 00:00 sample that day predates the first live row
    live = datetime(2026, 11, 1, 21, tzinfo=timezone.utc)
    assert aws_spot.days_to_read(now, 90, live)[-1] == date(2026, 11, 1)
    assert aws_spot.days_to_read(now, 90, datetime(2026, 11, 2, tzinfo=timezone.utc))[-1] == date(2026, 11, 1)
    assert aws_spot.days_to_read(now, 2, None) == [date(2026, 11, 13)], "only whole days that have ended"


def test_run_stores_raw_and_archived_idempotently_without_double_counting():
    _db()
    try:
        # us-east-2 has been recorded live since 2026-11-13 00:00 (seeded through the live path): only its 11-12
        # sample predates that; us-east-1 has no live row, so both whole days are read.
        snap = SimpleNamespace(payload=_payload("us-east-2", "2026-11-12"), endpoint=aws.ec2_endpoint(
            "us-east-2", "DescribeSpotPriceHistory"), fetched_at=datetime(2026, 11, 13, tzinfo=timezone.utc))
        live = aws.spot_listings({snap.endpoint: [snap]})
        normalize.save(live)
        normalize.record_observations(live)
        assert aws_spot.live_starts() == {"us-east-2": datetime(2026, 11, 13, tzinfo=timezone.utc)}
        obs_before = _q("SELECT count(*) FROM listing_observations")[0][0]

        m = Ec2()
        rep = aws_spot.run(3, provider=m.provider(), now=NOW, regions=["us-east-1", "us-east-2"], log=lambda *_: 0)
        assert rep["failed"] == 0 and rep["earliest"] == "2026-11-12", rep
        by = {x["region"]: x for x in rep["regions"]}
        assert by["us-east-1"]["days"] == 2 and by["us-east-2"]["days"] == 1, by
        assert {r for r, f in m.calls if f["StartTime"].startswith("2026-11-13")} == {"us-east-1"}
        n_e2 = len({i["availabilityZone"] for i in snap.payload["spotPriceHistorySet"]})
        assert rep["rows"] == rep["inserted"] == 6 + 6 + n_e2, rep
        assert rep["by_gpu"] == {H100: rep["rows"]}

        raws = _q("SELECT name, version, sha256, body FROM catalog_snapshots WHERE source = 'aws_spot_history' "
                  "ORDER BY name, version")
        assert [(r.name, r.version) for r in raws] == [("us-east-1", "2026-11-12"), ("us-east-1", "2026-11-13"),
                                                       ("us-east-2", "2026-11-12")]
        e1 = raws[0]
        body = json.loads(bytes(e1.body))
        assert len(body["spotPriceHistorySet"]) == 26 and body["request"]["StartTime"] == "2026-11-12T00:00:00Z"
        assert "AKIDEXAMPLE" not in bytes(e1.body).decode() and SECRET not in bytes(e1.body).decode()
        import hashlib
        assert hashlib.sha256(bytes(e1.body)).hexdigest() == e1.sha256
        shas = {r.source_sha256 for r in _q("SELECT DISTINCT source_sha256 FROM archived_prices")}
        assert shas == {r.sha256 for r in raws}, "every archived row names the raw body it was read from"

        # Observed tables are untouched: no listing, observation or hourly row comes from the backfill.
        assert _q("SELECT count(*) FROM listing_observations")[0][0] == obs_before
        assert _q("SELECT count(*) FROM compute_listings")[0][0] == len(live)

        # Re-run: every day is re-read from the raw store (no AWS call), nothing new is inserted.
        m2 = Ec2()
        rep2 = aws_spot.run(3, provider=m2.provider(), now=NOW, regions=["us-east-1", "us-east-2"],
                            log=lambda *_: 0)
        assert m2.calls == [] and rep2["inserted"] == 0 and rep2["rows"] == rep["rows"], rep2
        assert _q("SELECT count(*) FROM archived_prices")[0][0] == rep["rows"]
        assert _q("SELECT count(*) FROM catalog_snapshots")[0][0] == 3

        # The archived readers: spot only (never on_demand), one point per region-day document.
        assert archived.gpu_archived(H100, "on_demand")["points"] == []
        pts = archived.gpu_archived(H100, "spot")["points"]
        assert len(pts) == 3 and {p["source"] for p in pts} == {"aws_spot_history"}
        e1_1112 = min(r["price_per_gpu_hour"] for r in aws_spot.day_rows(
            "us-east-1", date(2026, 11, 12), _payload("us-east-1", "2026-11-12"), "x"))
        e2_1112 = min(r["price_per_gpu_hour"] for r in aws_spot.day_rows(
            "us-east-2", date(2026, 11, 12), snap.payload, "x"))
        # The daily series takes the provider's LOWEST across that day's documents (regions), not the last one.
        # Nothing observed for H100 in segment spot -> market_hourly is empty, so every day is archived.
        ext = archived.extended(H100, "spot", None)
        (prov,) = ext["providers"]
        day = ext["days"].index("2026-11-12T00:00:00+00:00")
        assert e1_1112 < e2_1112 and abs(prov["values"][day] - float(e1_1112)) < 1e-6, (prov["values"], e1_1112, e2_1112)
        assert ext["kind"][day] == "archived"

        # Dry run stores nothing.
        with normalize.SessionLocal.begin() as s:
            s.execute(text("DELETE FROM archived_prices"))
            s.execute(text("DELETE FROM catalog_snapshots"))
        rep3 = aws_spot.run(3, provider=Ec2().provider(), now=NOW, regions=["us-east-1"], dry_run=True,
                            log=lambda *_: 0)
        assert rep3["rows"] == 12 and rep3["inserted"] == 0
        assert _q("SELECT count(*) FROM archived_prices")[0][0] == 0
        assert _q("SELECT count(*) FROM catalog_snapshots")[0][0] == 0
    finally:
        scratchdb.drop(DB)


def test_failed_day_is_reported_and_not_stored():
    _db()
    try:
        # us-west-2 has no recorded page: the mock answers an empty set, which is a failed fetch (zero GPU rows).
        rep = aws_spot.run(2, provider=Ec2().provider(), now=NOW, regions=["us-west-2"], live={}, log=lambda *_: 0)
        assert rep["failed"] == 1 and rep["rows"] == 0 and rep["earliest"] is None
        assert "no GPU rows" in rep["regions"][0]["failures"][0]
        assert _q("SELECT count(*) FROM catalog_snapshots")[0][0] == 0
    finally:
        scratchdb.drop(DB)


def test_cli_wiring():
    assert "aws-spot" in sys.modules["backfill.__main__"].__doc__
    try:
        backfill_main(["aws-spot", "--region", "mars-1", "--dry-run"])
    except SystemExit as exc:
        assert "mars-1" in str(exc)
    else:
        raise AssertionError("an unknown region is refused")
    seen = {}
    orig = aws_spot.run
    aws_spot.run = lambda days, **kw: seen.update(days=days, **kw) or {
        "regions": [], "rows": 0, "inserted": 0, "failed": 0, "earliest": None, "by_gpu": {}}
    try:
        assert backfill_main(["aws-spot", "--days", "30", "--dry-run", "--region", "us-east-1"]) == 0
    finally:
        aws_spot.run = orig
    assert seen == {"days": 30, "dry_run": True, "regions": ["us-east-1"]}, seen


TESTS = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]

if __name__ == "__main__":
    for t in TESTS:
        t()
        print(t.__name__, "ok")
    print("all passed")
