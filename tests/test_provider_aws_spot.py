"""AWS Spot Instance Advisor feed: stored raw next to the on-demand price file, read only by spot_advice().

The feed fixture (tests/fixtures_providers/aws_spot_advisor.json) is a trimmed copy of the REAL
spot-advisor-data.json fetched 2026-11-01 (Last-Modified: Fri, 20 Mar 2026). No network.

Run:  .venv/bin/python tests/test_provider_aws_spot.py
"""

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from providers.aws import ENDPOINT, REGION_NAME, SPOT_ADVISOR, AwsProvider, spot_advice  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_providers"
NOW = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
LAST_MODIFIED = "Fri, 20 Mar 2026 23:38:55 GMT"
# A two-row stand-in for the on-demand price file (its real shape: regions -> {key: row}).
PRICES = {"regions": {REGION_NAME: {
    "a": {"Instance Type": "p5.48xlarge", "price": "55.04", "vCPU": "192", "Memory": "2048 GiB"},
    "b": {"Instance Type": "m5.large", "price": "0.096", "vCPU": "2", "Memory": "8 GiB"}}}}


def snap(payload):
    return SimpleNamespace(payload=payload, request=None, fetched_at=NOW)


def provider(spot_status=200, spot_body=None):
    spot_body = (FIX / "aws_spot_advisor.json").read_text() if spot_body is None else spot_body

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "spot-bid-advisor.s3.amazonaws.com":
            return httpx.Response(spot_status, text=spot_body,
                                  headers={"content-type": "application/json", "last-modified": LAST_MODIFIED})
        if request.url.raw_path.decode() == ENDPOINT:
            return httpx.Response(200, json=PRICES)
        return httpx.Response(404, text="not found")
    return AwsProvider(httpx.AsyncClient(base_url="https://b0.p.awsstatic.com", transport=httpx.MockTransport(handler)))


def fetch(prov):
    """(us-east-1's price file, the Spot Advisor) of one poll; the other regions' files are 404s here."""
    by = {r.endpoint: r for r in asyncio.run(prov.fetch())}
    return by[ENDPOINT], by[SPOT_ADVISOR]


def test_feed_stored_raw_gpu_rows_only():
    prices, spot = fetch(provider())
    assert prices.ok and prices.endpoint == ENDPOINT
    assert spot.ok and spot.endpoint == SPOT_ADVISOR and spot.payload["last_modified"] == LAST_MODIFIED
    adv = spot.payload["spot_advisor"]
    assert set(adv) == {"us-east-1", "eu-west-1"} and set(adv["us-east-1"]) == {"Linux", "Windows"}
    assert "m5.large" not in adv["us-east-1"]["Linux"], "non-GPU types dropped before storage"
    assert adv["us-east-1"]["Linux"]["p5.48xlarge"] == {"s": 57, "r": 0}, "rows kept verbatim"
    assert [r["label"] for r in spot.payload["ranges"]] == ["<5%", "5-10%", "10-15%", "15-20%", ">20%"]
    assert set(spot.payload["instance_types"]) <= {"p5.48xlarge", "p4d.24xlarge", "g5.xlarge", "g6e.12xlarge", "p3.2xlarge"}


def test_spot_advice_reader():
    _, spot = fetch(provider())
    rows = {(a.instance_type, a.region): a for a in spot_advice({SPOT_ADVISOR: [snap(spot.payload)]})}
    assert len(rows) == 8, sorted(rows)  # Linux only: 5 GPU types in us-east-1, 3 in eu-west-1
    p5 = rows[("p5.48xlarge", "us-east-1")]
    assert (p5.interruption_band, p5.interruption_label, p5.savings_pct) == (0, "<5%", 57)
    assert p5.gpu_count == 8 and p5.canonical_gpu_name == "NVIDIA H100 80GB SXM5" and p5.last_modified == LAST_MODIFIED
    p4d = rows[("p4d.24xlarge", "us-east-1")]
    assert (p4d.interruption_band, p4d.interruption_label) == (3, "15-20%")
    assert rows[("g5.xlarge", "eu-west-1")].interruption_band == 2
    # A row with a band outside the table or a non-integer saving is skipped, never guessed.
    odd = json.loads(json.dumps(spot.payload))
    odd["spot_advisor"]["us-east-1"]["Linux"]["p5.48xlarge"] = {"s": 57, "r": 7}
    odd["spot_advisor"]["us-east-1"]["Linux"]["p4d.24xlarge"] = {"s": "56", "r": 3}
    assert len(spot_advice({SPOT_ADVISOR: [snap(odd)]})) == 6


def test_listings_never_change():
    """The feed is never a listing: AWS listings are the on-demand price file's, with or without it."""
    prices, spot = fetch(provider())
    alone = AwsProvider.normalize({ENDPOINT: [snap(prices.payload)]})
    both = AwsProvider.normalize({ENDPOINT: [snap(prices.payload)], SPOT_ADVISOR: [snap(spot.payload)]})
    assert alone == both and [l.listing_id for l in both] == ["p5.48xlarge:us-east-1"]
    assert all(l.market_type == "on_demand" and not l.interruptible for l in both)


def test_feed_fails_open():
    """A down, reshaped, re-banded or GPU-less feed is a failed fetch; the price file is untouched."""
    bad = [(503, "slow down"), (200, "not json"), (200, json.dumps({"spot_advisor": []})),
           (200, json.dumps({"ranges": [{"index": i} for i in range(3)], "spot_advisor": {"us-east-1": {"Linux": {"g5.xlarge": {"s": 1, "r": 1}}}}})),
           (200, json.dumps({"ranges": [{"index": i} for i in range(5)], "spot_advisor": {"us-east-1": {"Linux": {"m5.large": {"s": 1, "r": 1}}}}}))]
    for status, body in bad:
        prices, spot = fetch(provider(status, body))
        assert not spot.ok and spot.payload is None and spot.error, (status, body)
        assert prices.ok and len(AwsProvider.normalize({ENDPOINT: [snap(prices.payload)]})) == 1


if __name__ == "__main__":
    for t in (test_feed_stored_raw_gpu_rows_only, test_spot_advice_reader, test_listings_never_change, test_feed_fails_open):
        t()
        print(t.__name__, "ok")
    print("all passed")
