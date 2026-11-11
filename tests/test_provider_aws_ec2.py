"""AWS keyed EC2 market calls (providers/aws.py): spot prices -> listings, placement scores -> capacity tiers,
Capacity Block offerings stored raw (and the new-account limit answer stored once, quietly). No network.

Fixtures (tests/fixtures_providers/aws_ec2_*.xml) are REAL responses captured 2026-11-01 with the read-only key,
trimmed by the request itself (five instance types, two regions); account ids replaced by 123456789012:
    aws_ec2_spot_us-east-{1,2}.xml              DescribeSpotPriceHistory (a change-log window), Linux/UNIX
                                                (p5.48xlarge, p5en.48xlarge, p6-b200.48xlarge, g6e.xlarge, m5.large)
    aws_ec2_placement_{p5,p6-b200}.48xlarge.xml GetSpotPlacementScores, 1 instance, six regions
    aws_ec2_capacity_blocks_limit.xml           DescribeCapacityBlockOfferings on this new account
    aws_ec2_unauthorized.xml                    an action the key may not call (DescribeInstances)
The Capacity Block offering in test_capacity_blocks_available is SYNTHETIC (no real one is reachable on this
account), shaped per the API reference's CapacityBlockOffering.

Run:  .venv/bin/python tests/test_provider_aws_ec2.py
"""

import asyncio
import logging
import re
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

import providers.aws as aws  # noqa: E402
from providers.aws import (CAPACITY_BLOCKS, ENDPOINT, PLACEMENT_REGIONS, PLACEMENT_TYPES, REGION_NAME, SPOT_REGIONS,  # noqa: E402
                           AwsProvider, placement_tier)

FIX = Path(__file__).resolve().parent / "fixtures_providers"
KEY_ID, SECRET = "AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
NOW = datetime(2026, 11, 1, 21, tzinfo=timezone.utc)
PRICES = {"regions": {REGION_NAME: {
    "a": {"Instance Type": "p5.48xlarge", "price": "55.04", "vCPU": "192", "Memory": "2048 GiB"},
    "b": {"Instance Type": "m5.large", "price": "0.096", "vCPU": "2", "Memory": "8 GiB"}}}}
EMPTY_SPOT = ('<DescribeSpotPriceHistoryResponse xmlns="http://ec2.amazonaws.com/doc/2016-11-15/">'
              '<requestId>x</requestId><spotPriceHistorySet/><nextToken/></DescribeSpotPriceHistoryResponse>')
CB_OFFER = """<DescribeCapacityBlockOfferingsResponse xmlns="http://ec2.amazonaws.com/doc/2016-11-15/">
  <requestId>synthetic</requestId><capacityBlockOfferingSet><item>
    <capacityBlockOfferingId>cbr-0123456789abcdef0</capacityBlockOfferingId><instanceType>{t}</instanceType>
    <availabilityZone>us-east-1a</availabilityZone><instanceCount>1</instanceCount>
    <startDate>2026-11-03T11:30:00.000Z</startDate><endDate>2026-11-04T11:30:00.000Z</endDate>
    <capacityBlockDurationHours>24</capacityBlockDurationHours><upfrontFee>750.00</upfrontFee>
    <currencyCode>USD</currencyCode><tenancy>default</tenancy></item></capacityBlockOfferingSet>
</DescribeCapacityBlockOfferingsResponse>"""


def fx(name: str) -> str:
    return (FIX / name).read_text()


class Aws:
    """A MockTransport standing in for awsstatic, the Spot Advisor bucket and ec2.<region>.amazonaws.com."""

    def __init__(self, cb=None, spot=None, placement=None):
        self.calls: list[tuple[str, dict, httpx.Request]] = []
        self.cb = cb or (lambda form: (400, fx("aws_ec2_capacity_blocks_limit.xml")))
        self.spot = spot or self._spot
        self.placement = placement or (lambda form: (200, fx(f"aws_ec2_placement_{form['InstanceType.1']}.xml"))
                                       if form["InstanceType.1"] in ("p5.48xlarge", "p6-b200.48xlarge")
                                       else (200, fx("aws_ec2_placement_p6-b200.48xlarge.xml")))

    @staticmethod
    def _spot(region, form):
        name = f"aws_ec2_spot_{region}.xml"
        return (200, fx(name)) if (FIX / name).exists() else (200, EMPTY_SPOT)

    def handler(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "spot-bid-advisor.s3.amazonaws.com":
            return httpx.Response(200, text=fx("aws_spot_advisor.json"), headers={"content-type": "application/json"})
        if host == "b0.p.awsstatic.com":
            return httpx.Response(200, json=PRICES)
        region = host.split(".")[1]
        form = dict(urllib.parse.parse_qsl(request.content.decode()))
        self.calls.append((region, form, request))
        action = form["Action"]
        status, body = (self.spot(region, form) if action == "DescribeSpotPriceHistory" else
                        self.placement(form) if action == "GetSpotPlacementScores" else
                        self.cb(form) if action == "DescribeCapacityBlockOfferings" else
                        (403, fx("aws_ec2_unauthorized.xml")))
        return httpx.Response(status, text=body, headers={"content-type": "text/xml;charset=UTF-8"})

    def provider(self, keys=True) -> AwsProvider:
        client = httpx.AsyncClient(base_url="https://b0.p.awsstatic.com", transport=httpx.MockTransport(self.handler))
        return AwsProvider(client, *((KEY_ID, SECRET) if keys else (None, None)))

    def actions(self) -> list[str]:
        return [f["Action"] for _, f, _ in self.calls]


def run(coro):
    return asyncio.run(coro)


def by_endpoint(responses):
    out = {}
    for r in responses:
        if r.ok:
            out.setdefault(r.endpoint, []).append(SimpleNamespace(payload=r.payload, request=r.request,
                                                                  fetched_at=NOW, endpoint=r.endpoint))
    return out


def fresh():
    aws._CAPACITY_BLOCK_LOGGED = False
    aws.RETRY_DELAY = 0


def test_no_key_no_calls():
    fresh()
    m = Aws()
    out = run(m.provider(keys=False).fetch())
    assert m.calls == [] and len(out) == 2, "without the key: the price file and the Spot Advisor only"
    eps = by_endpoint(out)
    assert AwsProvider.normalize_capacity(eps) == []
    assert [l.listing_id for l in AwsProvider.normalize(eps)] == ["p5.48xlarge:us-east-1"]


def test_requests_are_signed_read_only_calls():
    fresh()
    m = Aws()
    run(m.provider().fetch())
    assert set(m.actions()) == {"DescribeSpotPriceHistory", "GetSpotPlacementScores", "DescribeCapacityBlockOfferings"}
    for region, form, req in m.calls:
        auth = req.headers["authorization"]
        assert re.fullmatch(rf"AWS4-HMAC-SHA256 Credential={KEY_ID}/\d{{8}}/{region}/ec2/aws4_request, "
                            r"SignedHeaders=content-type;host;x-amz-date, Signature=[0-9a-f]{64}", auth), auth
        assert req.method == "POST" and req.url.host == f"ec2.{region}.amazonaws.com" and form["Version"] == "2016-11-15"
        assert SECRET not in auth and SECRET not in req.content.decode()
    spot = [f for _, f, _ in m.calls if f["Action"] == "DescribeSpotPriceHistory"]
    assert sorted(r for r, f, _ in m.calls if f["Action"] == "DescribeSpotPriceHistory") == sorted(SPOT_REGIONS)
    # A change log: a fresh process with no stored poll looks back SPOT_COLD_WINDOW (test_spot_change_log.py has more).
    t = lambda v: datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    assert all(t(f["EndTime"]) - t(f["StartTime"]) == aws.SPOT_COLD_WINDOW and f["ProductDescription.1"] == "Linux/UNIX"
               for f in spot)
    assert {v for k, v in spot[0].items() if k.startswith("InstanceType.")} == set(aws.GPU_INSTANCES)


def test_spot_stored_raw_gpu_rows_only():
    fresh()
    m = Aws()
    out = {r.endpoint: r for r in run(m.provider().fetch())}
    east1 = out["https://ec2.us-east-1.amazonaws.com/?Action=DescribeSpotPriceHistory"]
    assert east1.ok and east1.payload["region"] == "us-east-1"
    rows = east1.payload["spotPriceHistorySet"]
    assert rows and all(r["instanceType"] != "m5.large" for r in rows), "non-GPU rows dropped before storage"
    assert {"instanceType", "productDescription", "spotPrice", "timestamp", "availabilityZone",
            "availabilityZoneId"} == set(rows[0]), "rows kept verbatim"
    assert "StartTime" in east1.request and "NextToken" not in east1.request
    # A region whose answer has no GPU row is a failed fetch, never an empty source.
    west = out["https://ec2.us-west-2.amazonaws.com/?Action=DescribeSpotPriceHistory"]
    assert not west.ok and west.payload is None and "no GPU rows" in west.error


def test_spot_listings():
    fresh()
    m = Aws()
    eps = by_endpoint(run(m.provider().fetch()))
    listings = AwsProvider.normalize(eps)
    od = [l for l in listings if l.market_type == "on_demand"]
    spot = {l.listing_id: l for l in listings if l.market_type == "spot"}
    assert [l.listing_id for l in od] == ["p5.48xlarge:us-east-1"], "on-demand unchanged"
    assert len(spot) == 25, len(spot)  # 13 + 12 GPU rows in the two fixtures (m5.large dropped), one per (type, AZ)
    p5 = spot["p5.48xlarge:us-east-1d:spot"]
    assert p5.sku == "p5.48xlarge" and p5.region == "us-east-1d" and p5.country == "US"
    assert p5.interruptible and p5.provider_tier == "hyperscaler" and p5.available is None
    assert p5.canonical_gpu_name == "NVIDIA H100 80GB SXM5" and p5.gpu_count == 8
    assert p5.price_per_gpu_hour * 8 == p5.price_per_instance_hour
    assert (p5.vcpu, p5.ram_gb) == (192, 2048.0), "instance specs from the price file"
    b200 = [l for l in spot.values() if l.sku == "p6-b200.48xlarge"]
    assert b200 and all(l.canonical_gpu_name == "NVIDIA B200 180GB SXM" for l in b200)
    assert all(l.vcpu is None for l in b200), "no on-demand row in the stand-in price file: never guessed"
    assert not any(l.sku == "m5.large" for l in spot.values())


def test_spot_latest_per_az_and_product():
    """Only the newest Linux/UNIX row per (type, AZ) is priced; other OSes, non-GPU types, bad prices are not."""
    snap = SimpleNamespace(fetched_at=NOW, request=None, endpoint="x?Action=DescribeSpotPriceHistory", payload={
        "region": "us-east-1", "spotPriceHistorySet": [
            {"instanceType": "p5.48xlarge", "productDescription": "Linux/UNIX", "spotPrice": "30.0",
             "timestamp": "2026-11-01T17:00:00.000Z", "availabilityZone": "us-east-1a"},
            {"instanceType": "p5.48xlarge", "productDescription": "Linux/UNIX", "spotPrice": "32.0",
             "timestamp": "2026-11-01T20:00:00.000Z", "availabilityZone": "us-east-1a"},
            {"instanceType": "p5.48xlarge", "productDescription": "Windows", "spotPrice": "1.0",
             "timestamp": "2026-11-01T21:00:00.000Z", "availabilityZone": "us-east-1b"},
            {"instanceType": "g6e.xlarge", "productDescription": "Linux/UNIX", "spotPrice": "n/a",
             "timestamp": "2026-11-01T21:00:00.000Z", "availabilityZone": "us-east-1b"}]})
    ls = aws.spot_listings({snap.endpoint: [snap]})
    assert [(l.listing_id, str(l.price_per_instance_hour)) for l in ls] == [("p5.48xlarge:us-east-1a:spot", "32.0")]


def test_placement_scores_as_tiers():
    fresh()
    m = Aws()
    out = run(m.provider().fetch())
    assert [r.ok for r in out if "GetSpotPlacementScores" in r.endpoint] == [True] * len(PLACEMENT_TYPES)
    caps = {(c.sku, c.region): c for c in AwsProvider.normalize_capacity(by_endpoint(out))}
    p5 = {r: c.tier for (s, r), c in caps.items() if s == "p5.48xlarge:spot"}
    # Real scores: us-west-2 8, us-east-2 9, ap-northeast-1 6, us-east-1 / eu-west-2 / eu-north-1 3.
    assert p5 == {"us-west-2": "high", "us-east-2": "high", "ap-northeast-1": "medium", "us-east-1": "low",
                  "eu-west-2": "low", "eu-north-1": "low"}, p5
    c = caps[("p5.48xlarge:spot", "us-west-2")]
    assert c.in_stock is None, "a score is a likelihood, not stock"
    assert c.raw_value == "placement score 8/10 (target 1 instance, regional)" and c.country == "US"
    assert c.canonical_gpu_name == "NVIDIA H100 80GB SXM5" and c.gpu_count == 8 and c.available_count is None
    assert c.source == "https://ec2.us-east-1.amazonaws.com/?Action=GetSpotPlacementScores&InstanceType=p5.48xlarge"
    assert caps[("p6-b200.48xlarge:spot", "us-west-2")].tier == "low"
    assert ("p6-b200.48xlarge:spot", "eu-west-2") not in caps, "a region AWS did not score gets no row"
    forms = [f for _, f, _ in m.calls if f["Action"] == "GetSpotPlacementScores"]
    assert all(f["TargetCapacity"] == "1" and f["TargetCapacityUnitType"] == "units" for f in forms)
    assert {v for k, v in forms[0].items() if k.startswith("RegionName.")} == set(PLACEMENT_REGIONS)


def test_placement_tier_mapping():
    assert [placement_tier(s) for s in range(1, 11)] == ["low"] * 3 + ["medium"] * 3 + ["high"] * 4
    assert [placement_tier(s) for s in (0, 11, None)] == [None, None, None]


def test_slow_calls_are_cached():
    """Placement scores every 2 h and Capacity Blocks every 24 h, not every hourly poll; spot prices every poll."""
    fresh()
    m = Aws()
    p = m.provider()
    run(p.fetch())
    first = m.actions()
    m.calls.clear()
    run(p.fetch())
    assert m.actions() == ["DescribeSpotPriceHistory"] * len(SPOT_REGIONS), m.actions()
    assert first.count("GetSpotPlacementScores") == len(PLACEMENT_TYPES)
    m.calls.clear()
    p._last = {k: v.replace(year=2000) for k, v in p._last.items()}  # both windows have passed
    run(p.fetch())
    assert m.actions().count("GetSpotPlacementScores") == len(PLACEMENT_TYPES)
    assert m.actions().count("DescribeCapacityBlockOfferings") == 1


def test_capacity_blocks_unavailable_recorded_once_quietly():
    fresh()
    m = Aws()
    records = []
    h = logging.Handler()
    h.emit = records.append
    logging.getLogger("providers.aws").addHandler(h)
    try:
        out = {r.endpoint: r for r in run(m.provider().fetch())}
        run(Aws().provider().fetch())  # another poll (a fresh process cache would call again): no second log line
    finally:
        logging.getLogger("providers.aws").removeHandler(h)
    cb = out[CAPACITY_BLOCKS]
    assert cb.ok and cb.error is None, "the account's state, not a failed feed"
    assert cb.payload["status"] == "not_available_for_account"
    assert cb.payload["error_code"] == "CapacityBlockDescribeLimitExceeded"
    assert "not sufficient to use EC2 Capacity Blocks" in cb.payload["error_message"]
    assert m.actions().count("DescribeCapacityBlockOfferings") == 1, "stops at the first limit answer"
    assert len([r for r in records if "Capacity Block" in r.getMessage()]) == 1


def test_capacity_blocks_available():
    fresh()
    m = Aws(cb=lambda form: (200, CB_OFFER.format(t=form["InstanceType"])))
    cb = {r.endpoint: r for r in run(m.provider().fetch())}[CAPACITY_BLOCKS]
    assert cb.ok and cb.payload["status"] == "ok"
    assert [o["instanceType"] for o in cb.payload["capacityBlockOfferingSet"]] == list(aws.CAPACITY_BLOCK_TYPES)
    assert cb.payload["capacityBlockOfferingSet"][0]["upfrontFee"] == "750.00"
    assert [q["CapacityDurationHours"] for q in cb.request["queries"]] == ["24"] * 3
    assert len(AwsProvider.normalize(by_endpoint([cb]))) == 0, "stored raw only"


def test_errors_redacted_and_failed():
    fresh()
    m = Aws(placement=lambda form: (403, fx("aws_ec2_unauthorized.xml")))
    out = [r for r in run(m.provider().fetch()) if "GetSpotPlacementScores" in r.endpoint]
    assert out and all(not r.ok and r.payload is None for r in out)
    err = out[0].error
    assert err.startswith("UnauthorizedOperation: ") and "<redacted>" in err
    assert not re.search(r"\d{12}|arn:aws", err), err
    assert aws.redact("User: arn:aws:iam::123456789012:user/x is not") == "User: <redacted> is not"


def test_throttle_retried_once_and_pagination():
    fresh()
    seen = {"n": 0}
    page1, page2 = fx("aws_ec2_spot_us-east-1.xml"), fx("aws_ec2_spot_us-east-2.xml")
    split = page1.index("</spotPriceHistorySet>")

    def spot(region, form):
        if region != "us-east-1":
            return 200, EMPTY_SPOT
        seen["n"] += 1
        if seen["n"] == 1:
            return 503, ('<Response><Errors><Error><Code>RequestLimitExceeded</Code><Message>Request limit exceeded.'
                         '</Message></Error></Errors><RequestID>x</RequestID></Response>')
        if "NextToken" not in form:
            return 200, page1[:split] + "</spotPriceHistorySet><nextToken>tok2</nextToken>" + \
                page1[split + len("</spotPriceHistorySet>"):].replace("<nextToken></nextToken>", "")
        assert form["NextToken"] == "tok2"
        return 200, page2

    m = Aws(spot=spot)
    r = {x.endpoint: x for x in run(m.provider().fetch())}["https://ec2.us-east-1.amazonaws.com/?Action=DescribeSpotPriceHistory"]
    assert r.ok and seen["n"] == 3, seen
    assert len(r.payload["spotPriceHistorySet"]) == 13 + 12, "both pages, GPU rows only"


def test_keys_never_stored():
    fresh()
    m = Aws(placement=lambda form: (403, fx("aws_ec2_unauthorized.xml")))
    dumped = str([r.model_dump() for r in run(m.provider().fetch())])
    assert SECRET not in dumped and KEY_ID not in dumped


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(t.__name__, "ok")
    print("all passed")
