"""Full cost (cost/, api/cost.py): parsers over real saved responses, the hand table's rules, listing spec fields,
the estimator's arithmetic (billing increments, egress tiers and allowances, storage classes, unknowns never $0,
fabric and deployability gates), raw-first storage of fetches incl. failures, the public API, and the hard rule
that nothing statistical imports cost/. No network: every response is a fixture.

Fixtures (real responses, 2026-11-01, cut to a few dozen products where the original is large):
    fixtures_cost/aws_data_transfer_us-east-1.json   AWSDataTransfer offer file: every us-east-1 egress product + 6 others
    fixtures_cost/aws_s3_us-east-1.json              AmazonS3 offer file: S3 Standard storage + requests + 6 others
    fixtures_cost/azure_{bandwidth,storage}_eastus.json   Azure Retail Prices responses (Items, NextPageLink)
    fixtures_cost/oracle_price_list.json             cetools price list: the cost parts + 3 unrelated parts
    fixtures_cost/verda_volume_types.json            Verda /v1/volume-types, whole
    fixtures_capacity/hyperstack.json                Hyperstack pricebook (Cloud-SSD, ObjectStorage, PublicIP)
    fixtures_providers/vultr_plans_{vcg,metal}.json  Vultr plans (bandwidth, deploy_ondemand)

Run:  .venv/bin/python tests/test_cost.py
"""

import asyncio
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("OPENGRID_NO_JOBS", "1")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import scratchdb  # noqa: E402  (isolates the process from real providers before config loads)
import httpx  # noqa: E402
from cost import components as cc  # noqa: E402
from cost import estimate as est  # noqa: E402
from cost import manual, sources  # noqa: E402

FX = ROOT / "tests" / "fixtures_cost"
T = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
H100 = "NVIDIA H100 80GB SXM5"


def _j(name):
    return json.loads((FX / name).read_text(encoding="utf-8"))


def snapshots(at=T) -> dict:
    """sources.latest() shape, every source fetched OK at `at` and reshaped as the fetcher stores it."""
    bodies = {
        "aws_data_transfer": sources.reshape_aws_data_transfer(_j("aws_data_transfer_us-east-1.json")),
        "aws_s3": sources.reshape_aws_s3(_j("aws_s3_us-east-1.json")),
        "azure_bandwidth": sources.reshape_azure(_j("azure_bandwidth_eastus.json")["Items"]),
        "azure_storage": sources.reshape_azure(_j("azure_storage_eastus.json")["Items"]),
        "oracle_price_list": sources.reshape_oracle(_j("oracle_price_list.json")),
        "verda_volume_types": sources.reshape_verda(_j("verda_volume_types.json")),
    }
    return {k: {"payload": v, "fetched_at": at, "last_attempt_at": at, "last_ok": True, "last_error": None}
            for k, v in bodies.items()}


def provider_raw(at=T) -> dict:
    hs = json.loads((ROOT / "tests" / "fixtures_capacity" / "hyperstack.json").read_text())["/pricebook"][0]
    fp = ROOT / "tests" / "fixtures_providers"
    vultr = {ep: [SimpleNamespace(payload=json.loads((fp / f).read_text()), fetched_at=at, request=None)]
             for ep, f in (("/v2/plans?type=vcg&per_page=500", "vultr_plans_vcg.json"),
                           ("/v2/plans-metal?per_page=500", "vultr_plans_metal.json"))}
    vast_offers = [
        {"id": 1, "gpu_name": "H100 SXM", "num_gpus": 1, "dph_total": 2.10, "storage_cost": 0.133,
         "inet_up_cost": 0.005, "inet_down_cost": 0.001},
        {"id": 2, "gpu_name": "H100 SXM", "num_gpus": 2, "dph_total": 5.00, "storage_cost": 0.2},
    ]
    vast = {"/api/v0/bundles/": [SimpleNamespace(payload=vast_offers, fetched_at=at, request={"gpu_name": "H100 SXM"})]}
    return {"hyperstack": {"/pricebook": [SimpleNamespace(payload=hs, fetched_at=at, request=None)]},
            "vultr": vultr, "vast": vast}


def all_components():
    comps, notes = cc.build(snapshots(), provider_raw())
    return comps, notes


def comp(comps, provider, component, **kw):
    got = [c for c in comps if c["provider"] == provider and c["component"] == component
           and all(c.get(k) == v for k, v in kw.items())]
    assert got, f"no {provider} {component} {kw}"
    return got[0]


def prices(c):
    return [(float(t["start_gb"]), None if t["end_gb"] is None else float(t["end_gb"]), round(float(t["price"]), 6))
            for t in c["tiers"]]


# --------------------------------------------------------------------------- parsers

def test_api_parsers():
    comps, notes = all_components()
    assert notes == [], notes
    aws = comp(comps, "aws", "egress_internet")
    assert prices(aws) == [(0, 100, 0), (100, 10240, 0.09), (10240, 51200, 0.085), (51200, 153600, 0.07),
                           (153600, None, 0.05)] and aws["origin"] == "api" and aws["region"] == "us-east-1"
    assert prices(comp(comps, "aws", "egress_inter_zone")) == [(0, None, 0.01)]
    assert prices(comp(comps, "aws", "object_storage")) == [(0, 51200, 0.023), (51200, 512000, 0.022), (512000, None, 0.021)]
    assert prices(comp(comps, "aws", "object_requests_write")) == [(0, None, 0.005)]
    assert prices(comp(comps, "aws", "object_requests_read")) == [(0, None, 0.0004)]
    assert comp(comps, "aws", "block_storage")["origin"] == "manual"  # EBS: not machine-readable
    az = comp(comps, "azure", "egress_internet")
    assert prices(az)[:4] == [(0, 100, 0), (100, 10335, 0.087), (10335, 51295, 0.083), (51295, 153695, 0.07)]
    # Premium SSD v2: $0.00011 per GiB-hour -> x730 / 1.073741824 per GB-month
    assert prices(comp(comps, "azure", "block_storage")) == [(0, None, round(0.00011 * 730 / 1.073741824, 6))]
    assert prices(comp(comps, "azure", "shared_fs")) == [(0, None, 0.16)]
    assert prices(comp(comps, "azure", "object_storage"))[0] == (0, 51200, 0.0208)
    assert prices(comp(comps, "oracle", "egress_internet")) == [(0, 10240, 0), (10240, None, 0.0085)]
    ob = comp(comps, "oracle", "block_storage")
    assert prices(ob) == [(0, None, 0.0425)] and ob["kind"] == "derived"
    assert prices(comp(comps, "oracle", "object_storage")) == [(0, 10, 0), (10, None, 0.0255)]
    v = [c for c in comps if c["provider"] == "verda"]
    assert [(c["component"], c["product"], prices(c)) for c in v] == [
        ("block_storage", "NVMe volume", [(0, None, 0.2)]), ("block_storage", "HDD volume", [(0, None, 0.05)]),
        ("shared_fs", "NVMe_Shared volume", [(0, None, 0.2)])]
    hs = comp(comps, "hyperstack", "block_storage")
    assert hs["origin"] == "provider" and prices(hs) == [(0, None, round(0.000096774 * 730, 6))]
    assert prices(comp(comps, "hyperstack", "public_ip")) == [(0, None, 0.00672)]
    print("test_api_parsers ok")


def test_reshape_rejects():
    for fn, body in ((sources.reshape_aws_data_transfer, {"products": {}}),
                     (sources.reshape_aws_data_transfer, {"products": {"x": {"attributes": {}}}, "terms": {}}),
                     (sources.reshape_oracle, {"items": [{"partNumber": "B1"}]}),
                     (sources.reshape_oracle, []),
                     (sources.reshape_azure, []),
                     (sources.reshape_verda, {})):
        try:
            fn(body)
        except ValueError:
            continue
        raise AssertionError(f"{fn.__name__} accepted {body!r}")
    # a stored body that no longer parses is a note, never a component and never a crash
    snaps = snapshots()
    snaps["azure_bandwidth"]["payload"] = {"items": [{"productName": "x"}]}
    snaps["oracle_price_list"] = {"payload": None, "fetched_at": None, "last_attempt_at": T, "last_ok": False,
                                  "last_error": "HTTPStatusError: 503"}
    comps, notes = cc.build(snaps, {})
    assert not [c for c in comps if c["provider"] == "azure" and c["component"] == "egress_internet"]
    assert not [c for c in comps if c["provider"] == "oracle" and c["origin"] == "api"]
    assert any("azure_bandwidth" in n and "could not be parsed" in n for n in notes)
    assert any("oracle_price_list" in n and "503" in n for n in notes)
    print("test_reshape_rejects ok")


def test_manual_table_rules():
    seen = set()
    for m in manual.COMPONENTS:
        assert m["source"].startswith("https://"), m
        assert m.get("status", "checked") in ("checked", "unverified"), m
        assert m["component"] in cc.COMPONENTS and m["raw_unit"] in (
            "GB", "GiB", "GB-month", "GiB-month", "GB-hour", "1k requests", "hour"), m
        key = (m["provider"], m["component"], m["product"])
        assert key not in seen, f"duplicate manual row {key}"
        seen.add(key)
        starts = [t[0] for t in m["tiers"]]
        assert starts == sorted(starts) and starts[0] == 0, m
    # machine-readable sources are not duplicated by hand (except where the API lacks the component)
    api_keys = {(c["provider"], c["component"]) for c in all_components()[0] if c["origin"] in ("api", "provider")}
    dup = {(m["provider"], m["component"]) for m in manual.COMPONENTS} & api_keys
    assert dup == set(), f"hand rows repeat API data: {dup}"
    for p, b in manual.BILLING.items():
        assert b["source"].startswith("https://") and b["status"] in ("checked", "unverified"), p
    for f in manual.FABRIC:
        assert f["source"].startswith("https://") and f["status"] == "checked", f
    assert manual.V == "2026-11-01"
    # unit conversion: GiB price -> per GB, GiB bound -> GB
    c = cc.component("x", "egress_internet", "p", [(0, 1, 0), (1, None, 1.073741824)], "GiB", origin="manual", source="s")
    assert prices(c) == [(0, 1.073741824, 0), (1.073741824, None, 1.0)]
    try:
        cc.component("x", "bandwidth", "p", [(0, None, 1)], "GB", origin="manual", source="s")
        raise AssertionError("unknown component accepted")
    except ValueError:
        pass
    print("test_manual_table_rules ok")


# --------------------------------------------------------------------------- spec fields

def L(provider, listing_id, sku=None, price=None, count=8, gpu=H100, tier=None, available=True, **kw):
    return {"provider": provider, "listing_id": listing_id, "sku": sku or listing_id, "canonical_gpu_name": gpu,
            "gpu_count": count, "region": kw.get("region"), "price_per_gpu_hour": None if price is None else price / count,
            "price_per_instance_hour": price, "market_type": kw.get("market_type", "on_demand"),
            "interruptible": kw.get("interruptible", False), "provider_tier": tier, "available": available,
            "observed_at": T}


def test_listing_specs():
    ctx = est.context(provider_raw(), [])
    s = est.listing_specs(L("aws", "p5.48xlarge", price=55.04), ctx)
    assert s["gpu_form_factor"] == "sxm" and s["gpu_link"] == "nvlink" and "900" in s["gpu_link_detail"]
    assert (s["node_fabric"], s["node_fabric_gbps"], s["multi_node"]) == ("efa", 3200, True)
    assert (s["billing_increment_s"], s["billing_minimum_s"]) == (1, 60) and s["gpu_memory_gb"] == 80
    do = est.listing_specs(L("digitalocean", "gpu-h100x8-640gb", price=35.28), ctx)
    assert (do["node_fabric"], do["node_fabric_gbps"], do["multi_node"]) == ("ethernet", 25, False)
    assert "no RDMA" in do["fabric_notes"]
    ora = est.listing_specs(L("oracle", "BM.GPU.H100.8", price=80.0), ctx)
    assert (ora["billing_increment_s"], ora["billing_minimum_s"]) == (1, 3600)
    vm = est.listing_specs(L("oracle", "VM.GPU.A10.1", count=1, gpu="NVIDIA A10", price=2.0), ctx)
    assert (vm["billing_increment_s"], vm["billing_minimum_s"]) == (1, 60) and vm["gpu_link"] is None
    vp_ib = est.listing_specs(L("voltagepark", "bm:a:infiniband", sku="h100-sxm5-80gb:bare-metal:infiniband", price=19.92), ctx)
    vp_eth = est.listing_specs(L("voltagepark", "bm:a:ethernet", sku="h100-sxm5-80gb:bare-metal:ethernet", price=15.92), ctx)
    assert vp_ib["node_fabric_gbps"] == 3200 and vp_eth["node_fabric"] == "ethernet"
    crusoe = est.listing_specs(L("crusoe", "x", sku="h100-80gb-sxm-ib.8x", price=30.0), ctx)
    assert crusoe["node_fabric"] == "infiniband" and crusoe["billing_increment_s"] is None  # billing unverified
    pcie = est.listing_specs(L("lambda", "x", gpu="NVIDIA H100 80GB PCIe", count=2, price=6.0), ctx)
    assert pcie["gpu_form_factor"] == "pcie" and pcie["gpu_link"] == "pcie" and pcie["node_fabric"] is None
    plan = next(iter(cc.vultr_plans([provider_raw()["vultr"][k][0].payload for k in provider_raw()["vultr"]]).items()))
    vu = est.listing_specs(L("vultr", plan[0], price=10.0, count=1, gpu="NVIDIA L40S"), ctx)
    assert vu["on_demand_deployable"] == plan[1]["deploy_ondemand"]
    assert vu["included_egress_gb_month"] == float(plan[1]["bandwidth_gb"])
    print("test_listing_specs ok")


# --------------------------------------------------------------------------- estimator

R1 = {k: v for k, v in est.PRESETS["R1"].items() if k != "label"} | {"storage_class": "auto"}


def r1_listings():
    return [L("aws", "p5.48xlarge", price=55.04), L("oracle", "BM.GPU.H100.8", price=80.0),
            L("coreweave", "hgx-h100", price=49.24), L("verda", "8H100.80S.176V", price=38.34),
            L("lambda", "gpu_8x_h100_sxm5", price=31.92), L("digitalocean", "gpu-h100x8-640gb", price=35.28),
            L("hyperstack", "n3-H100-SXM5x8", price=25.60), L("azure", "Standard_ND96isr_H100_v5", price=98.32),
            L("aws", "p5.48xlarge:other", sku="p5.48xlarge", price=60.0),            # dearer: not the AWS row
            L("aws", "spot", sku="p5.48xlarge", price=20.0, interruptible=True),    # spot: never
            L("coreweave", "sold-out", price=10.0, available=False),                 # sold out: never
            L("nebius", "one-gpu", count=1, price=4.5)]                              # wrong size: never


def by(rows):
    return {r["provider"]: r for r in rows}


def test_estimate_r1():
    comps, _ = all_components()
    out = est.estimate(R1, r1_listings(), comps, est.context(provider_raw(), []))
    rows = by(out["rows"])
    assert set(rows) == {"aws", "oracle", "coreweave", "verda", "lambda", "digitalocean", "hyperstack", "azure"}
    aws = rows["aws"]
    # compute 72 x 55.04; egress 9,900 GB x $0.09 (first 100 GB free); EBS gp3 20,000 GB x $0.08 x 72/730
    assert aws["components"]["compute"]["amount"] == 3962.88 and aws["components"]["egress_internet"]["amount"] == 891.0
    assert aws["components"]["storage"]["amount"] == round(20000 * 0.08 * 72 / 730, 2) == 157.81
    assert aws["components"]["storage"]["class"] == "block_storage"  # AWS publishes no shared FS here
    assert aws["total"] == 5011.69 and aws["unknown"] == [] and not aws["total_is_lower_bound"]
    assert aws["sku"] == "p5.48xlarge" and aws["listing_id"] == "p5.48xlarge"
    assert aws["extras_vs_compute_pct"] == 26.5 and abs(aws["effective_per_gpu_hour"] - 5011.69 / 576) < 1e-4
    ora = rows["oracle"]
    assert ora["components"]["egress_internet"]["amount"] == 0.0  # under the 10 TB free tier
    assert ora["total"] == round(5760 + 20000 * 0.0425 * 72 / 730, 2)
    az = rows["azure"]
    assert az["components"]["egress_internet"]["amount"] == round(9900 * 0.087, 2)
    assert az["components"]["storage"]["class"] == "shared_fs" and az["components"]["storage"]["amount"] == round(
        20000 * 0.16 * 72 / 730, 2)
    cw = rows["coreweave"]
    assert cw["components"]["egress_internet"]["amount"] == 0 and cw["components"]["storage"]["class"] == "shared_fs"
    assert cw["total"] == round(49.24 * 72 + 20000 * 0.07 * 72 / 730, 2)
    # unknowns: never $0, always named, total marked as a lower bound
    ve = rows["verda"]
    assert ve["unknown"] == ["egress_internet"] and ve["total_is_lower_bound"]
    assert ve["components"]["egress_internet"] == {"amount": None, "known": False}
    assert ve["total"] == round(38.34 * 72 + 20000 * 0.2 * 72 / 730, 2)  # compute + storage only
    la = rows["lambda"]
    assert la["unknown"] == ["storage"] and la["components"]["storage"]["amount"] is None  # $0.20 is unverified
    assert la["components"]["egress_internet"]["amount"] == 0.0
    do = rows["digitalocean"]
    assert do["components"]["egress_internet"]["amount"] == 0.0  # inside the 60 TiB pool
    hs = rows["hyperstack"]
    assert hs["components"]["storage"]["class"] == "block_storage" and hs["components"]["storage"]["origin"] == "provider"
    # ranking: known totals ascending; ranks 1..n
    assert [r["rank"] for r in out["rows"]] == list(range(1, len(out["rows"]) + 1))
    flags = [r["total_is_lower_bound"] for r in out["rows"]]
    assert flags == sorted(flags), "complete totals rank before every lower bound"
    for lb in (False, True):
        tot = [r["total"] for r in out["rows"] if r["total_is_lower_bound"] is lb]
        assert tot == sorted(tot)
    assert by(out["rows"])["lambda"]["rank"] > by(out["rows"])["aws"]["rank"], "a cheap lower bound never outranks a complete total"
    assert out["unranked"] == []
    print("test_estimate_r1 ok")


def test_billing_increments_and_unknown_compute():
    comps, _ = all_components()
    job = {"gpu": H100, "count": 8, "hours": 1 / 6, "egress_gb": 0, "storage_gb": 0}
    lst = [L("oracle", "BM.GPU.H100.8", price=80.0), L("verda", "8H100", price=38.34), L("aws", "p5.48xlarge", price=55.04),
           L("crusoe", "h100-80gb-sxm-ib.8x", price=30.0), L("together", "8x", price=31.92)]
    rows = by(est.estimate(job, lst, comps)["rows"])
    assert rows["oracle"]["components"]["compute"]["billed_hours"] == 1.0  # bare metal: 1-hour minimum
    assert rows["oracle"]["total"] == 80.0
    assert rows["verda"]["components"]["compute"]["billed_hours"] == round(600 / 3600, 6)
    assert rows["aws"]["total"] == round(55.04 / 6, 2)
    assert rows["together"]["components"]["compute"]["billed_hours"] == 1.0
    cr = rows["crusoe"]  # increment unverified: per-second assumed, flagged not exact
    assert cr["components"]["compute"]["exact"] is False and any("billing increment" in n for n in cr["notes"])
    assert est.billed_hours(D(72), None, None) == (D(72), True)
    assert est.billed_hours(D("0.5"), 3600, 3600) == (D(1), True)
    assert est.billed_hours(D("1.01"), 60, 60)[0] == D(61) / 60
    # a listing with no price: compute unknown, no total
    nop = est.estimate_listing(L("aws", "x", price=None), job, comps)
    assert nop["total"] is None and "compute" in nop["unknown"]
    print("test_billing_increments_and_unknown_compute ok")


def test_tiers_allowance_and_gates():
    tiers = [{"start_gb": D(0), "end_gb": D(100), "price": D(0)}, {"start_gb": D(100), "end_gb": D(1000), "price": D(1)},
             {"start_gb": D(1000), "end_gb": None, "price": D("0.5")}]
    assert est.tiered(D(50), tiers) == 0 and est.tiered(D(150), tiers) == 50
    assert est.tiered(D(2000), tiers) == 900 + 500
    assert est.tiered(D(2000), tiers, allowance_gb=D(1500)) == 400  # plan allowance first, then the tiers
    comps, _ = all_components()
    job = dict(R1, min_fabric_gbps=3200)
    out = est.estimate(job, r1_listings(), comps)
    ranked, unranked = by(out["rows"]), by(out["unranked"])
    assert set(ranked) == {"aws", "oracle", "coreweave"} | {"azure"}
    assert unranked["digitalocean"]["unranked_reason"] == "node fabric 25 Gbps < 3200"
    assert unranked["hyperstack"]["unranked_reason"] == "node fabric 350 Gbps < 3200"
    assert unranked["lambda"]["unranked_reason"] == "node fabric not published"
    assert unranked["verda"]["unranked_reason"] == "node fabric not published"
    assert all("rank" not in r for r in out["unranked"])
    # Vultr's included bandwidth comes off before its $0.01/GB overage
    ctx = est.context(provider_raw(), [])
    pid, plan = next(iter(ctx["vultr_plans"].items()))
    vjob = {"gpu": "NVIDIA L40S", "count": 1, "hours": 10, "egress_gb": float(plan["bandwidth_gb"]) + 1000, "storage_gb": 0}
    vr = est.estimate_listing(L("vultr", pid, price=1.671, count=1, gpu="NVIDIA L40S"), vjob, comps, ctx)
    assert vr["components"]["egress_internet"]["amount"] == 10.0 and vr["components"]["egress_internet"]["included_gb"]
    assert vr["components"]["compute"]["billed_hours"] == 10.0
    print("test_tiers_allowance_and_gates ok")


def test_vast_offer_components():
    raw = provider_raw()
    listings = [L("vast", "H100 SXM:cheapest", sku="H100 SXM", count=1, price=2.10, tier="cheapest"),
                L("vast", "H100 SXM:median", sku="H100 SXM", count=1, price=2.30, tier="median")]
    comps = all_components()[0] + est.vast_components(raw, listings)
    job = {"gpu": H100, "count": 1, "hours": 10, "egress_gb": 100, "storage_gb": 500}
    rows = est.estimate(job, listings, comps)["rows"]
    assert [r["listing_id"] for r in rows] == ["H100 SXM:cheapest"]  # the median row is not a rentable machine
    r = rows[0]
    assert r["components"]["egress_internet"]["amount"] == 0.5  # 100 GB x the offer's $0.005 upload
    assert r["components"]["storage"]["amount"] == round(500 * 0.133 * 10 / 730, 2)
    assert r["components"]["storage"]["billed_when_stopped"] is True
    # an offer whose fields were not kept (Vast `_keep` today) is unknown, not $0
    bare = [{"id": 9, "gpu_name": "H100 SXM", "num_gpus": 1, "dph_total": 2.0}]
    raw["vast"]["/api/v0/bundles/"][0].payload = bare
    comps = all_components()[0] + est.vast_components(raw, listings)
    r = est.estimate(job, listings, comps)["rows"][0]
    assert set(r["unknown"]) == {"egress_internet", "storage"} and r["total"] == 21.0
    print("test_vast_offer_components ok")


def test_never_imported_by_statistics():
    pat = re.compile(r"^\s*(import cost\b|from cost\b)", re.M)
    users = sorted(str(p.relative_to(ROOT)) for p in ROOT.rglob("*.py")
                   if ".venv" not in p.parts and "tests" not in p.parts and p.parts[len(ROOT.parts)] != "cost"
                   and pat.search(p.read_text(encoding="utf-8", errors="ignore")))
    assert users == ["api/cost.py"], f"only the read-only API imports cost/: {users}"
    print("test_never_imported_by_statistics ok")


# --------------------------------------------------------------------------- database + API

DB = "og_test_cost"


def _mock_client(fail: set[str]) -> httpx.AsyncClient:
    bodies = {
        sources.AWS_DT_URL: _j("aws_data_transfer_us-east-1.json"), sources.AWS_S3_URL: _j("aws_s3_us-east-1.json"),
        sources.ORACLE_URL.split("?")[0]: _j("oracle_price_list.json"), sources.VERDA_URL: _j("verda_volume_types.json"),
    }
    bw, st = _j("azure_bandwidth_eastus.json"), _j("azure_storage_eastus.json")
    half = len(bw["Items"]) // 2
    pages = {"bw1": {"Items": bw["Items"][:half], "NextPageLink": "https://prices.azure.com/api/retail/prices?page=bw2"},
             "bw2": {"Items": bw["Items"][half:], "NextPageLink": None}}

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url).split("?")[0]
        key = next((k for k in fail if k in str(req.url)), None)
        if key:
            return httpx.Response(503, text="unavailable")
        if url == sources.AZURE_URL:
            if req.url.params.get("page") == "bw2":
                return httpx.Response(200, json=pages["bw2"])
            f = req.url.params.get("$filter") or ""
            return httpx.Response(200, json=pages["bw1"] if "Bandwidth" in f else st)
        if url in bodies:
            return httpx.Response(200, json=bodies[url])
        return httpx.Response(404, text="unknown")

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_db_fetch_and_api():
    import config
    import fixtures
    import normalize
    from models import ComputeListing

    url = scratchdb.create(DB)
    try:
        Session = fixtures.session(url)
        normalize.SessionLocal = Session
        # first refresh: Verda down -> stored as a failure, the rest OK; Azure bandwidth spans two pages
        r1 = asyncio.run(sources.refresh(_mock_client({"api.verda.com"})))
        assert r1["failed"] == 1 and [f["source"] for f in r1["fetches"] if not f["ok"]] == ["verda_volume_types"]
        with Session() as s:
            snaps = sources.latest(s)
        assert snaps["verda_volume_types"]["payload"] is None and "503" in snaps["verda_volume_types"]["last_error"]
        assert len(snaps["azure_bandwidth"]["payload"]["items"]) == len(_j("azure_bandwidth_eastus.json")["Items"])
        assert set(snaps["aws_data_transfer"]["payload"]["products"]) < set(_j("aws_data_transfer_us-east-1.json")["products"])
        comps, notes, _ = est.load_components()
        assert any("verda_volume_types" in n for n in notes)
        assert not [c for c in comps if c["provider"] == "verda"]
        # second refresh: everything OK; the newest OK snapshot wins
        r2 = asyncio.run(sources.refresh(_mock_client(set())))
        assert r2["failed"] == 0
        comps, notes, _ = est.load_components()
        assert notes == [] and [c for c in comps if c["provider"] == "verda"]

        now = datetime.now(timezone.utc)
        rows = []
        for p, lid, sku, price in (("aws", "p5.48xlarge", "p5.48xlarge", 55.04), ("verda", "8H100.80S.176V", "8H100.80S.176V", 38.34),
                                   ("oracle", "BM.GPU.H100.8", "BM.GPU.H100.8", 80.0)):
            rows.append(ComputeListing(provider=p, sku=sku, listing_id=lid, raw_gpu_name="H100", canonical_gpu_name=H100,
                                       gpu_count=8, region=None, country=None, price_per_gpu_hour=D(str(price)) / 8,
                                       price_per_instance_hour=D(str(price)), currency="USD", market_type="on_demand",
                                       provider_tier=None, interruptible=False, available=True, capacity=None,
                                       capacity_unit=None, vcpu=None, ram_gb=None, storage_gb=None, observed_at=now))
        stale = rows[0].model_copy(update={"provider": "coreweave", "listing_id": "old", "observed_at": now - timedelta(days=2)})
        normalize.save(rows + [stale])
        with Session() as s:
            counts = {t: s.execute(__import__("sqlalchemy").text(f"SELECT count(*) FROM {t}")).scalar()
                      for t in ("compute_listings", "listing_observations", "cost_snapshots")}

        config.settings.app_password = None
        import main
        from api import cost as cost_api
        from api import pages
        from fastapi.testclient import TestClient

        assert "cost" in pages.PUBLIC_V1 and "/v1/cost/estimate" in pages.PUBLIC_POST
        cost_api._components.cache_clear()
        client = TestClient(main.app, headers={"X-OpenGrid-Request": "1"})
        resp = client.post("/v1/cost/estimate", json={"preset": "R1"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        m = body["meta"]
        assert m["kind"] == "estimated" and m["methodology"] == "/methodology/full-cost" and "never" in m["rule"]
        assert m["job"]["egress_gb"] == 10000 and m["job"]["gpu"] == H100 and m["lower_bound"] == 1
        got = {r["provider"]: r for r in body["data"]}
        assert set(got) == {"aws", "verda", "oracle"}  # the 2-day-old CoreWeave listing is gone, not priced
        assert got["aws"]["total"] == 5011.69 and got["verda"]["unknown"] == ["egress_internet"]
        assert got["verda"]["components"]["egress_internet"]["amount"] is None
        # the same job by fields, in TB; storage_class narrows the choice
        b2 = client.post("/v1/cost/estimate", json={"gpu": "h100-80gb-sxm5", "count": 8, "hours": 72, "egress_tb": 10,
                                                    "storage_tb": 20, "storage_class": "object_storage"}).json()
        a2 = {r["provider"]: r for r in b2["data"]}["aws"]
        assert a2["components"]["storage"]["class"] == "object_storage"
        assert a2["components"]["storage"]["amount"] == round(20000 * 0.023 * 72 / 730, 2)
        assert client.post("/v1/cost/estimate", json={"gpu": "nope", "count": 1, "hours": 1}).status_code == 404
        for bad, code in (({"count": 1, "hours": 1}, "invalid_gpu"),
                          ({"gpu": H100, "count": 0, "hours": 1}, "invalid_count"),
                          ({"gpu": H100, "count": 1}, "missing_hours"),
                          ({"gpu": H100, "count": 1, "hours": 1, "egress_tb": 1, "egress_gb": 5}, "invalid_egress"),
                          ({"gpu": H100, "count": 1, "hours": 1, "colour": "red"}, "unknown_field"),
                          ({"preset": "R9"}, "invalid_preset"),
                          ({"gpu": H100, "count": 1, "hours": 1, "storage_class": "tape"}, "invalid_storage_class")):
            r = client.post("/v1/cost/estimate", json=bad)
            assert r.status_code == 422 and code in r.text, (bad, r.status_code, r.text)
        assert client.post("/v1/cost/estimate", content="x", headers={"content-type": "text/plain"}).status_code == 415
        cj = client.get("/v1/cost/components?provider=aws").json()
        assert {c["component"] for c in cj["data"]} >= {"egress_internet", "block_storage", "object_storage"}
        assert all(isinstance(t["price"], float) for c in cj["data"] for t in c["tiers"])
        assert client.get("/v1/cost/components?component=bandwidth").status_code == 422
        src = client.get("/v1/cost/sources").json()["data"]
        assert {s["source"] for s in src} == set(sources.SOURCES) and all(s["last_attempt_ok"] for s in src)
        bill = {b["provider"]: b for b in client.get("/v1/cost/billing").json()["data"]}
        assert bill["oracle"]["products"]["BM."]["minimum_s"] == 3600
        assert [p["preset"] for p in client.get("/v1/cost/presets").json()["data"]] == ["R0", "R1", "R2", "R3"]
        assert client.get("/methodology/full-cost").status_code == 200
        # reads wrote nothing
        with Session() as s:
            after = {t: s.execute(__import__("sqlalchemy").text(f"SELECT count(*) FROM {t}")).scalar() for t in counts}
        assert after == counts
        # public: with a site password set, the estimate and the reads stay open (PUBLIC_PAGES), keys stay private
        config.settings.app_password = "pw"
        old = config.settings.public_pages
        config.settings.public_pages = True
        try:
            anon = TestClient(main.app)
            assert anon.post("/v1/cost/estimate", json={"preset": "R3"}, headers={"X-OpenGrid-Request": "1"}).status_code == 200
            assert anon.get("/v1/cost/components").status_code == 200
        finally:
            config.settings.app_password = None
            config.settings.public_pages = old
    finally:
        scratchdb.drop(DB)
    print("test_db_fetch_and_api ok")


if __name__ == "__main__":
    test_api_parsers()
    test_reshape_rejects()
    test_manual_table_rules()
    test_listing_specs()
    test_estimate_r1()
    test_billing_increments_and_unknown_compute()
    test_tiers_allowance_and_gates()
    test_vast_offer_components()
    test_never_imported_by_statistics()
    test_db_fetch_and_api()
    print("all passed")
