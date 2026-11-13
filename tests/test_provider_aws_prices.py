"""AWS public price files beyond us-east-1: on-demand in every SPOT_REGIONS region, and Compute Savings Plans.

Fixtures (real files from b0.p.awsstatic.com, 2026-11-13, trimmed to the GPU types plus one m5.large as noise):
    aws_ondemand_ap-southeast-2.json                    the Sydney on-demand price file
    aws_savings_plan_3y_{no,partial,all}_ap-southeast-2.json  the Sydney 3-year Compute Savings Plan files

Run:  .venv/bin/python tests/test_provider_aws_prices.py
"""

import asyncio
import json
import os
import sys
import urllib.parse
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("OPENGRID_NO_JOBS", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402

import providers.aws as aws  # noqa: E402
import reserved  # noqa: E402
from providers.aws import AwsProvider  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_providers"
NOW = datetime(2026, 11, 13, 4, tzinfo=timezone.utc)
SYD = aws.REGION_NAMES["ap-southeast-2"]


def fx(name):
    return json.loads((FIX / name).read_text())


def serve(fail_region=None):
    """awsstatic for every region: Sydney's real files, every other region an empty (GPU-less) file."""
    seen = []

    def handler(request):
        path = urllib.parse.unquote(request.url.raw_path.decode())
        seen.append(path)
        if request.url.host == "spot-bid-advisor.s3.amazonaws.com":
            return httpx.Response(200, text=(FIX / "aws_spot_advisor.json").read_text())
        name = next(x for x in path.split("/") if x in aws.REGION_NAMES.values())
        if fail_region and name == aws.REGION_NAMES[fail_region]:
            return httpx.Response(503, text="slow down")
        if name == SYD and aws.ON_DEMAND in path:
            return httpx.Response(200, json=fx("aws_ondemand_ap-southeast-2.json"))
        if name == SYD and "/3 year/" in path:
            pay = path.split("/3 year/")[1].split("/")[0].split()[0].lower()
            return httpx.Response(200, json=fx(f"aws_savings_plan_3y_{pay}_ap-southeast-2.json"))
        return httpx.Response(200, json={"regions": {name: {}}})

    client = httpx.AsyncClient(base_url=aws.BASE_URL, transport=httpx.MockTransport(handler))
    return AwsProvider(client), seen


def by_endpoint(responses):
    out = {}
    for r in responses:
        if r.ok:
            out.setdefault(r.endpoint, []).append(SimpleNamespace(payload=r.payload, request=r.request,
                                                                  fetched_at=NOW, endpoint=r.endpoint))
    return out


def test_every_region_on_demand():
    prov, seen = serve()
    out = asyncio.run(prov.fetch())
    assert sum(aws.ON_DEMAND in p for p in seen) == len(aws.SPOT_REGIONS) == 15
    assert all(r.ok for r in out), [r.error for r in out if not r.ok]
    listings = {l.listing_id: l for l in AwsProvider.normalize(by_endpoint(out))}
    p5e = listings["p5e.48xlarge:ap-southeast-2"]
    assert p5e.price_per_instance_hour == D("82.2848") and p5e.region == "ap-southeast-2" and p5e.country == "AU"
    assert p5e.gpu_count == 8 and p5e.price_per_gpu_hour == D("82.2848") / 8 and p5e.market_type == "on_demand"
    assert not any(l.sku == "m5.large" for l in listings.values()), "non-GPU rows never stored"
    # The stored body keeps GPU rows only, under the region's own name.
    syd = next(r for r in out if r.endpoint == aws.price_endpoint("ap-southeast-2"))
    assert syd.payload["region"] == SYD and all(r["Instance Type"] in aws.GPU_INSTANCES for r in syd.payload["rows"])
    assert aws.price_endpoint("us-east-1") == aws.ENDPOINT, "us-east-1's stored endpoint (and its ids) unchanged"


def test_one_region_failing_never_hides_the_others():
    prov, _ = serve(fail_region="eu-west-2")
    out = asyncio.run(prov.fetch())
    failed = [r.endpoint for r in out if not r.ok]
    assert failed and all("London" in urllib.parse.unquote(e) for e in failed), failed
    assert any(l.region == "ap-southeast-2" for l in AwsProvider.normalize(by_endpoint(out)))


def test_savings_plans():
    prov, seen = serve()
    out = asyncio.run(prov.fetch())
    assert sum(aws.SAVINGS_PLANS in p for p in seen) == 6 * 15, "1/3 years x 3 payment options x 15 regions"
    sp = {(x.instance_type, x.years, x.payment): x for x in aws.savings_plan_prices(by_endpoint(out))}
    # The rates AWS prints for p5e (H200 x8) in Sydney, 3 years.
    p5e = {pay: sp[("p5e.48xlarge", 3, pay)].price_per_instance_hour for pay in ("No Upfront", "Partial Upfront", "All Upfront")}
    assert p5e == {"No Upfront": D("60.48239"), "Partial Upfront": D("56.00221"), "All Upfront": D("54.88217")}, p5e
    assert sp[("p5e.48xlarge", 3, "No Upfront")].on_demand_per_instance_hour == D("82.2848")
    assert all(x.region == "ap-southeast-2" for x in sp.values()) and not any(k[0] == "m5.large" for k in sp)
    # Never a listing: normalize() is the on-demand files (+ spot with the key), whatever else is stored.
    assert all(l.market_type == "on_demand" for l in AwsProvider.normalize(by_endpoint(out)))
    # Fetched every SAVINGS_PLAN_TTL, not every price poll.
    again = asyncio.run(prov.fetch())
    assert not any(aws.SAVINGS_PLANS in r.endpoint for r in again)


def test_reserved_rows():
    prov, _ = serve()
    out = asyncio.run(prov.fetch())
    be = by_endpoint(out)
    listings = [l.model_dump() for l in AwsProvider.normalize(be)]
    rows, notes = reserved.build({"aws": be}, listings, NOW, {"aws": 3600})
    rows = [r for r in rows if r["provider"] == "aws" and r["sku"] == "p5e.48xlarge"]
    assert len(rows) == 3 and not notes
    partial = next(r for r in rows if "Partial" in r["term_label"])
    assert partial["term_label"] == "3 Years Compute Savings Plan, Partial Upfront" and partial["term_bucket"] == "3y"
    assert abs(partial["price_per_gpu_hour"] - 56.00221 / 8) < 1e-6 and partial["gpu"] == "NVIDIA H200 141GB SXM5"
    assert partial["on_demand_match"] == "same_listing" and abs(partial["on_demand_per_gpu_hour"] - 82.2848 / 8) < 1e-6
    assert partial["saving_pct"] == round((1 - 56.00221 / 82.2848) * 100, 1) == 31.9, partial["saving_pct"]


if __name__ == "__main__":
    for t in (test_every_region_on_demand, test_one_region_failing_never_hides_the_others, test_savings_plans,
              test_reserved_rows):
        t()
        print(t.__name__, "ok")
    print("all passed")
