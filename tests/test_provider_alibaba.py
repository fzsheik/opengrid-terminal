"""Alibaba Cloud ECS spot: signed RPC calls, the GPU catalogue, spot per zone and on-demand per region.

No real Alibaba answer has been seen yet (no AccessKey on 2026-11-13). The bodies below follow Alibaba's documented
response shapes (DescribeInstanceTypes, DescribeAvailableResource, DescribeSpotPriceHistory, API 2014-05-26); the
spot numbers are a competitor feed's real Tokyo gn7e events (1.833 -> 1.887 at 19:00 UTC on 2026-10-29). The mock
checks every request's signature with providers.alibaba.sign, so a request it accepts is one Alibaba would.

Run:  .venv/bin/python tests/test_provider_alibaba.py
"""

import asyncio
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("OPENGRID_NO_JOBS", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402

from providers import alibaba  # noqa: E402
from providers.alibaba import AlibabaProvider  # noqa: E402

KID, SECRET = "LTAI-test-key-id", "test-secret"
NOW = datetime(2026, 10, 29, 19, 20, tzinfo=timezone.utc)

TYPES = {"InstanceTypes": {"InstanceType": [
    {"InstanceTypeId": "ecs.gn7e-c16g1.4xlarge", "InstanceTypeFamily": "ecs.gn7e", "GPUSpec": "NVIDIA A100",
     "GPUAmount": 1, "GPUMemorySize": 80.0, "CpuCoreCount": 16, "MemorySize": 125.0, "LocalStorageCategory": "x"},
    {"InstanceTypeId": "ecs.gn7e-c16g1.8xlarge", "InstanceTypeFamily": "ecs.gn7e", "GPUSpec": "NVIDIA A100",
     "GPUAmount": 2, "GPUMemorySize": 80.0, "CpuCoreCount": 32, "MemorySize": 250.0},
    {"InstanceTypeId": "ecs.sgn7i-vws-m2.xlarge", "InstanceTypeFamily": "ecs.sgn7i-vws", "GPUSpec": "NVIDIA A10*1/12",
     "GPUAmount": 1, "GPUMemorySize": 2.0, "CpuCoreCount": 4, "MemorySize": 15.5}]}, "NextToken": ""}
AVAILABLE = {"AvailableZones": {"AvailableZone": [{"ZoneId": "ap-northeast-1c", "AvailableResources": {"AvailableResource": [
    {"Type": "InstanceType", "SupportedResources": {"SupportedResource": [
        {"Value": "ecs.gn7e-c16g1.4xlarge", "Status": "Available"},
        {"Value": "ecs.gn7e-c16g1.8xlarge", "Status": "Available"},
        {"Value": "ecs.g7.large", "Status": "Available"}]}}]}}]}}


def spot_rows(itype, n):
    rows = []
    for zone, prices in (("ap-northeast-1c", ("1.833", "1.887")), ("ap-northeast-1d", ("1.901", "1.901"))):
        for hour, p in zip((18, 19), prices):
            rows.append({"InstanceType": itype, "ZoneId": zone, "SpotPrice": float(D(p) * n),
                         "OriginPrice": float(D("6.288") * n), "Timestamp": f"2026-10-29T{hour}:00:00Z",
                         "NetworkType": "vpc", "IoOptimized": "optimized"})
    return {"RequestId": "r", "Currency": "USD", "SpotPrices": {"SpotPriceType": rows}}


class Ecs:
    def __init__(self, currency="USD", fail=None):
        self.calls, self.currency, self.fail = [], currency, fail

    def handler(self, request):
        q = dict(request.url.params)
        sig = q.pop("Signature")
        assert sig == alibaba.sign(q, SECRET), "the request is signed over every other parameter"
        assert q["AccessKeyId"] == KID and q["SignatureMethod"] == "HMAC-SHA1" and q["Version"] == alibaba.VERSION
        region, action = request.url.host.split(".")[1], q["Action"]
        self.calls.append((region, action, q))
        if self.fail == action:
            return httpx.Response(400, json={"Code": "Throttling.User", "Message": f"slow down {KID}"})
        if action == "DescribeInstanceTypes":
            return httpx.Response(200, json=TYPES)
        if action == "DescribeAvailableResource":
            return httpx.Response(200, json=AVAILABLE if region == "ap-northeast-1" else {"AvailableZones": {}})
        if action == "DescribeSpotPriceHistory":
            n = 2 if q["InstanceType"].endswith("8xlarge") else 1
            return httpx.Response(200, json={**spot_rows(q["InstanceType"], n), "Currency": self.currency})
        return httpx.Response(404, json={"Code": "InvalidAction"})

    def provider(self):
        return AlibabaProvider(KID, SECRET, client=httpx.AsyncClient(transport=httpx.MockTransport(self.handler)))


def by_endpoint(responses):
    out = {}
    for r in responses:
        if r.ok:
            out.setdefault(r.endpoint, []).append(SimpleNamespace(payload=r.payload, request=r.request,
                                                                  fetched_at=NOW, endpoint=r.endpoint))
    return out


def test_fetch():
    ecs = Ecs()
    out = asyncio.run(ecs.provider().fetch())
    actions = [a for _, a, _ in ecs.calls]
    assert actions.count("DescribeInstanceTypes") == 1
    assert actions.count("DescribeAvailableResource") == len(alibaba.REGIONS)
    spot = [(r, q["InstanceType"]) for r, a, q in ecs.calls if a == "DescribeSpotPriceHistory"]
    assert sorted(spot) == [("ap-northeast-1", "ecs.gn7e-c16g1.4xlarge"), ("ap-northeast-1", "ecs.gn7e-c16g1.8xlarge")], \
        "only GPU types sold as spot in that region; the vGPU slice and the non-GPU g7 never asked"
    q = next(q for _, a, q in ecs.calls if a == "DescribeSpotPriceHistory")
    assert (q["NetworkType"], q["OSType"], q["SpotDuration"]) == ("vpc", "linux", "1")
    assert all(r.ok for r in out)
    for r in out:  # the key, signature and nonce never reach storage
        blob = f"{r.endpoint} {r.request} {r.payload} {r.error}"
        assert KID not in blob and SECRET not in blob and "Signature" not in blob and "RequestId" not in blob
    eps = {r.endpoint for r in out}
    assert alibaba.endpoint("ap-northeast-1", "DescribeSpotPriceHistory", InstanceType="ecs.gn7e-c16g1.4xlarge") in eps
    # The catalogue is daily: a second poll asks spot prices only.
    ecs.calls.clear()
    prov = ecs.provider()
    asyncio.run(prov.fetch())
    ecs.calls.clear()
    asyncio.run(prov.fetch())
    assert {a for _, a, _ in ecs.calls} == {"DescribeSpotPriceHistory"}


def test_listings():
    out = asyncio.run(Ecs().provider().fetch())
    ls = {l.listing_id: l for l in AlibabaProvider.normalize(by_endpoint(out))}
    one = ls["ecs.gn7e-c16g1.4xlarge:ap-northeast-1c:spot"]
    assert one.price_per_instance_hour == D("1.887") and one.gpu_count == 1 and one.market_type == "spot"
    assert one.interruptible and one.region == "ap-northeast-1c" and one.country == "JP"
    assert one.raw_gpu_name == "NVIDIA A100 80GB" and one.vcpu == 16 and one.ram_gb == 125.0
    assert one.provider_changed_at == datetime(2026, 10, 29, 19, tzinfo=timezone.utc) and one.changed_at_basis == "exact"
    assert [(c.price_per_instance_hour, c.provider_changed_at.hour) for c in one.superseded] == [(D("1.833"), 18)]
    two = ls["ecs.gn7e-c16g1.8xlarge:ap-northeast-1c:spot"]
    assert two.price_per_instance_hour == D("3.774") and two.price_per_gpu_hour == D("1.887")
    od = ls["ecs.gn7e-c16g1.4xlarge:ap-northeast-1"]
    assert od.market_type == "on_demand" and not od.interruptible and od.price_per_instance_hour == D("6.288")
    assert len(ls) == 6, sorted(ls)  # 2 types x (2 zones spot + 1 region on demand)


def test_failures_and_currency():
    ecs = Ecs(currency="CNY")
    out = asyncio.run(ecs.provider().fetch())
    assert AlibabaProvider.normalize(by_endpoint(out)) == [], "a CNY answer is skipped, never converted"
    bad = asyncio.run(Ecs(fail="DescribeInstanceTypes").provider().fetch())
    assert len(bad) == 1 and not bad[0].ok and bad[0].throttled and KID not in bad[0].error, "the key id is masked"
    thr = asyncio.run(Ecs(fail="DescribeSpotPriceHistory").provider().fetch())
    assert any(r.throttled for r in thr) and AlibabaProvider.normalize(by_endpoint(thr)) == []


def test_sign_known_vector():
    """Alibaba's documented signature example (ECS DescribeRegions, AccessKey testid / testsecret)."""
    params = {"AccessKeyId": "testid", "Action": "DescribeRegions", "Format": "XML", "SignatureMethod": "HMAC-SHA1",
              "SignatureNonce": "3ee8c1b8-83d3-44af-a94f-4e0ad82fd6cf", "SignatureVersion": "1.0",
              "Timestamp": "2016-02-23T12:46:24Z", "Version": "2014-05-26"}
    assert alibaba.sign(params, "testsecret") == "OLeaidS1JvxuMvnyHOwuJ+uX5qY="


def test_not_built_without_a_key():
    import config
    import fetch
    config.settings.alibaba_access_key_id = config.settings.alibaba_access_key_secret = None
    assert not any(p.name == "alibaba" for p in fetch.build_fetchers())


if __name__ == "__main__":
    for t in (test_fetch, test_listings, test_failures_and_currency, test_sign_known_vector, test_not_built_without_a_key):
        t()
        print(t.__name__, "ok")
    print("all passed")
