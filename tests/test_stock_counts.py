"""Stock counts per GPU type (api/stock_counts.py) and Hydra Host's per-device counts.

Covers: unit conversion (instances x GPUs per instance, GPUs as stated, no unit = no number); the overlap rule (one
pool's sizes are never added, SimplePod per host, different machines are); resellers kept out of the total; yes/no
and tiers never turned into numbers; providers that price a GPU with no stock row shown as gaps; the H100 SXM and
RTX 4090 tables built from the saved REAL provider answers, values checked by hand against the raw files; Hydra
Host's counts from its real inventory and a reshaped inventory raising; the admin read (401 anonymous, 403 viewer,
internal rows shown to the admin, out of the public schema).

Run:  .venv/bin/python tests/test_stock_counts.py
"""

import base64
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("OPENGRID_NO_JOBS", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import scratchdb  # noqa: E402  (isolates the process from real providers and the network)
import fixtures  # noqa: E402
import capacity  # noqa: E402
import normalize  # noqa: E402
from api import stock_counts as sc  # noqa: E402
from config import settings  # noqa: E402
from models import CapacityObservation, ComputeListing  # noqa: E402
from providers import PROVIDERS  # noqa: E402

HERE = Path(__file__).resolve().parent
CAP, PRO = HERE / "fixtures_capacity", HERE / "fixtures_providers"
NOW = datetime(2026, 11, 16, 12, 5, tzinfo=timezone.utc)
T0 = NOW - timedelta(minutes=5)
H100, R4090 = "NVIDIA H100 80GB SXM5", "NVIDIA RTX 4090 24GB"
DB = "og_test_stock_counts"


def o(provider, sku, *, region="r1", n=1, gpu=H100, in_stock=None, tier=None, free=None, total=None, unit=None,
      raw="x", at=T0, visibility="public"):
    return CapacityObservation(provider=provider, sku=sku, region=region, raw_gpu_name="raw", canonical_gpu_name=gpu,
                               gpu_count=n, in_stock=in_stock, tier=tier, available_count=free, total_count=total,
                               count_unit=unit, raw_value=raw, source="/x", observed_at=at, visibility=visibility)


def line(t, provider, region="r1"):
    return next(l for l in t["lines"] if l["provider"] == provider and l["region"] == region)


# --------------------------------------------------------------------------- by hand

def test_conversion_overlap_and_unknowns_by_hand():
    rows = [
        # Hyperstack's sizes of one region draw on the same 4 free GPUs: 1x:4, 2x:2, 4x:1, 8x:0 -> 4, never 4+4+4.
        o("hyperstack", "x1", n=1, free=4, unit="instance", in_stock=True),
        o("hyperstack", "x2", n=2, free=2, unit="instance", in_stock=True),
        o("hyperstack", "x4", n=4, free=1, unit="instance", in_stock=True),
        o("hyperstack", "x8", n=8, free=0, unit="instance", in_stock=False),
        o("hyperstack", "x8-other", n=8, in_stock=False),  # a flavor /core/stocks does not count: yes/no beside counts
        # SimplePod host 7: 5 GPUs free, offered as 5 x 1, 2 x 2, 1 x 5 -> 5; host 9: one 2-GPU instance -> 2.
        o("simplepod", "docker:1", n=1, free=5, unit="instance", in_stock=True, raw="numOfInstances=5; host=7"),
        o("simplepod", "docker:2", n=2, free=2, unit="instance", in_stock=True, raw="numOfInstances=2; host=7"),
        o("simplepod", "docker:3", n=5, free=1, unit="instance", in_stock=True, raw="numOfInstances=1; host=7"),
        o("simplepod", "docker:4", n=2, free=1, unit="instance", in_stock=True, raw="numOfInstances=1; host=9"),
        # Lium: two machine types in one city are different machines: 3 of 8 + 2 of 4 GPUs.
        o("lium", "8xH100", n=8, free=3, total=8, unit="gpu", in_stock=True),
        o("lium", "4xH100", n=4, free=2, total=4, unit="gpu", in_stock=True),
        # Latitude: 2 servers of 8 GPUs.
        o("latitude", "g3.h100.large", n=8, free=2, unit="instance", in_stock=True),
        # Salad: cumulative priorities (high >= medium) on 1-GPU nodes: the largest, an estimate.
        o("salad", "c:high", free=10, unit="instance", in_stock=True),
        o("salad", "c:medium", free=7, unit="instance", in_stock=True),
        # GPU.ai resells other clouds: 3 instances of 8 GPUs, shown but not in the total.
        o("gpuai", "h100x8:vm", n=8, free=3, unit="instance", in_stock=True),
        # A count with no stated unit is not converted.
        o("cudo", "vm:odd", free=6, in_stock=True),
        o("lambda", "gpu_8x_h100", region=None, n=8, in_stock=True),     # yes/no, in stock
        o("runpod", "H100:secure:1", region=None, tier="low", in_stock=True),
        o("koyeb", "1-gpu-h100", in_stock=False),                         # yes/no, out
        o("aws", "p5.48xlarge:spot", region="us-east-1", n=8, tier="high", in_stock=None, raw="placement score 8/10"),
        o("hyperbolic", "h100 sxm5:1x", in_stock=None, raw="totalAvailable=None"),
        o("lium", "other", gpu=R4090, free=99, unit="gpu", in_stock=True),  # another GPU: not in this table
    ]
    listed = {"nebius": {"eu-north1"}, "verda": {"FIN-01", None}, "lambda": {"us-east-1"}}
    supported = sorted(set(capacity.supported()) | {"verda"})
    t = sc.table(H100, rows, listed, NOW, supported)

    hs = line(t, "hyperstack")
    assert hs["shape"] == "count" and hs["unit"] == "instance" and hs["sizes"] == [1, 2, 4, 8]
    assert hs["gpus_free"] == 4 and hs["at_least"] and hs["combine"].startswith("largest"), hs
    assert [r["gpus_free"] for r in hs["rows"]] == [4, 4, 4, 0, None], "instances x GPUs per instance, row by row"
    sp = line(t, "simplepod")
    assert sp["gpus_free"] == 5 + 2 and sp["at_least"] and "per host" in sp["combine"]
    lium = line(t, "lium")
    assert (lium["gpus_free"], lium["gpus_total"], lium["at_least"]) == (5, 12, False) and lium["combine"].startswith("sum")
    assert line(t, "latitude")["gpus_free"] == 16 and line(t, "latitude")["combine"] is None
    salad = line(t, "salad")
    assert salad["gpus_free"] == 10 and salad["figure"] == "estimate"
    assert line(t, "gpuai")["gpus_free"] == 24 and line(t, "gpuai")["resold"]
    cudo = line(t, "cudo")
    assert cudo["shape"] == "count" and cudo["gpus_free"] is None, "no unit: the count is shown raw, not converted"
    assert line(t, "lambda", None)["shape"] == "yes-no" and line(t, "lambda", None)["gpus_free"] is None
    rp = line(t, "runpod", None)
    assert (rp["shape"], rp["tier"], rp["gpus_free"], rp["figure"]) == ("tier", "low", None, "level")
    aws = line(t, "aws", "us-east-1")
    assert (aws["shape"], aws["figure"], aws["in_stock"], aws["gpus_free"]) == ("tier", "likelihood", None, None)
    hb = line(t, "hyperbolic")
    assert hb["shape"] == "none" and hb["in_stock"] is None and hb["gpus_free"] is None
    assert not [l for l in t["lines"] if l["gpus_free"] == 99], "only this GPU's rows"

    s = t["summary"]
    assert s["gpus_free_known"] == 4 + 7 + 5 + 16 + 10 and s["at_least"], s
    assert s["resold_gpus_free_known"] == 24 and s["resold_providers_with_counts"] == ["gpuai"]
    assert s["providers_with_counts"] == ["cudo", "hyperstack", "latitude", "lium", "salad", "simplepod"]
    assert s["providers_in_stock_no_count"] == ["lambda", "runpod"]
    assert s["providers_out_of_stock_no_count"] == ["koyeb"]
    assert s["providers_unknown"] == ["aws", "hyperbolic", "nebius", "verda"], "AWS's score is not stock"
    gaps = {g["provider"]: g for g in t["no_stock_feed"]}
    assert set(gaps) == {"nebius", "verda"}, "lambda has a row, so it is not a gap"
    assert gaps["nebius"]["reason"] == "no stock feed" and gaps["verda"]["reason"].startswith("its stock feed")
    assert gaps["verda"]["regions"] == ["FIN-01"]
    assert s["line"].startswith(f"{H100}: at least 42 GPUs known free at 6 provider(s) with counts (+24 resold); "
                                "2 more in stock by yes/no or tier, 1 out of stock; 4 unknown (2 with no stock row)"), s["line"]

    # Age and staleness: RunPod reads capacity every 2 minutes, so a row 10 minutes old is stale; Akash's is not.
    old = sc.table(H100, [o("runpod", "a", tier="low", in_stock=True, at=NOW - timedelta(minutes=10)),
                          o("lium", "b", free=1, unit="gpu", in_stock=True, at=NOW - timedelta(minutes=10))], {}, NOW)
    assert line(old, "runpod")["age_seconds"] == 600 and line(old, "runpod")["stale"]
    assert not line(old, "lium")["stale"]

    many = sc.tables(rows, {"NVIDIA A100 80GB SXM4": {"nebius": {"x"}}}, NOW, supported)
    assert [x["gpu"] for x in many] == [R4090, H100, "NVIDIA A100 80GB SXM4"], "most GPUs free first; listed-only too"
    assert many[2]["summary"]["providers_unknown"] == ["nebius"] and not many[2]["lines"]


# --------------------------------------------------------------------------- from the saved real answers

def _snap(payload, request=None):
    return SimpleNamespace(payload=payload, fetched_at=T0, endpoint="", request=request)


def _capfix(name):
    raw = json.loads((CAP / f"{name}.json").read_text())
    return {ep: [_snap(p) for p in ps] for ep, ps in raw.items()}


def _pro(name):
    return json.loads((PRO / name).read_text())


def fixture_rows() -> list[CapacityObservation]:
    from providers import akash, clore, gpuai, hydrahost, vast
    loads = {n: _capfix(n) for n in ("hyperbolic", "hyperstack", "koyeb", "lambda", "linode", "lium", "massedcompute",
                                     "runpod", "scaleway", "voltagepark", "crusoe", "denvr", "boostrun", "horizon",
                                     "phyntec")}
    loads["vast"] = {vast.ENDPOINT: [_snap(c["payload"], c["request"])
                                     for c in json.loads((CAP / "vast_census.json").read_text())]}
    loads["hydrahost"] = {f"{hydrahost.INVENTORY}?page=1&pageSize=100": [_snap(_pro("hydrahost_inventory.json"))]}
    loads["akash"] = {akash.PRICES: [_snap(_pro("akash_gpu_prices.json"))]}
    loads["clore"] = {clore.MARKET: [_snap(_pro("clore_marketplace.json"))]}
    loads["gpuai"] = {gpuai.PRICING: [_snap(_pro("gpuai_pricing.json"))]}
    out = []
    for name, by_endpoint in loads.items():
        out += PROVIDERS[name].normalize_capacity(by_endpoint)
    return out


def test_h100_sxm_and_rtx_4090_from_real_answers():
    rows = fixture_rows()
    t = sc.table(H100, rows, {"nebius": {"eu-north1"}}, NOW)
    s = t["summary"]
    # By hand from the raw files: Akash h100 SXM5 availability 16 of 69; Vast's census 9 of 101; Lium's Nizhniy
    # Novgorod 2x machines 8 of 10; Hydra Host's one H100 server (Norway) is `preorder`: 0 of 1 server x 8 GPUs;
    # Hyperstack CANADA-1 8x H100 configurations 0; Voltage Park's bare-metal location 0 on both fabrics.
    assert s["gpus_free_known"] == 16 + 9 + 8 and not s["at_least"], s
    assert s["providers_with_counts"] == ["akash", "hydrahost", "hyperstack", "lium", "vast", "voltagepark"]
    assert (line(t, "akash", None)["gpus_free"], line(t, "akash", None)["gpus_total"]) == (16, 69)
    assert (line(t, "vast", None)["gpus_free"], line(t, "vast", None)["gpus_total"]) == (9, 101)
    hh = line(t, "hydrahost", "Norway")
    assert (hh["gpus_free"], hh["gpus_total"], hh["unit"], hh["sizes"]) == (0, 8, "instance", [8])
    assert line(t, "hyperstack", "CANADA-1")["gpus_free"] == 0 and not line(t, "hyperstack", "CANADA-1")["at_least"]
    assert line(t, "hyperstack", "CANADA-2")["shape"] == "yes-no", "a flavor /core/stocks omits stays yes/no"
    assert s["resold_gpus_free_known"] == 12 + 12 + 4 + 4, "GPU.ai's four regions, kept out of the total"
    assert s["providers_in_stock_no_count"] == ["runpod", "scaleway"]
    assert s["providers_out_of_stock_no_count"] == ["denvr", "horizon", "lambda", "massedcompute"]
    assert s["providers_unknown"] == ["hyperbolic", "nebius"], "Hyperbolic's options without totalAvailable"
    assert line(t, "runpod", None)["tier"] == "high" and line(t, "runpod", None)["gpus_free"] is None

    t = sc.table(R4090, rows, {}, NOW)
    s = t["summary"]
    lium = sum(l["gpus_free"] for l in t["lines"] if l["provider"] == "lium")
    # Lium: 14 cities, 65 free GPUs; Clore: 9 of 14; Akash rtx4090: 2 of 4.
    assert lium == 65 and len([l for l in t["lines"] if l["provider"] == "lium"]) == 14
    assert (line(t, "clore", None)["gpus_free"], line(t, "clore", None)["gpus_total"]) == (9, 14)
    assert (line(t, "akash", None)["gpus_free"], line(t, "akash", None)["gpus_total"]) == (2, 4)
    assert s["gpus_free_known"] == 65 + 9 + 2 and s["resold_gpus_free_known"] == 20
    assert s["providers_in_stock_no_count"] == ["runpod"]
    assert s["providers_out_of_stock_no_count"] == ["boostrun", "phyntec"]


# --------------------------------------------------------------------------- Hydra Host's per-device counts

def test_hydrahost_counts_one_server_per_device():
    from providers.hydrahost import INVENTORY, HydraHostProvider
    ep = f"{INVENTORY}?page=1&pageSize=100"
    body = _pro("hydrahost_inventory.json")
    rows = HydraHostProvider.normalize_capacity({ep: [_snap(body)]})
    assert rows and all(r.count_unit == "instance" and r.total_count == 1 for r in rows)
    assert all(r.available_count == (1 if r.raw_value == "on demand" else 0) for r in rows)
    assert sum(r.available_count for r in rows) == sum(d["stockStatus"] == "on demand" for d in body["data"]
                                                          if (d["specs"].get("gpu") or {}).get("model")) == 4
    body["data"][0]["stockStatus"] = "soon"
    odd = next(r for r in HydraHostProvider.normalize_capacity({ep: [_snap(body)]}) if r.sku == body["data"][0]["id"])
    assert (odd.in_stock, odd.available_count, odd.total_count, odd.count_unit) == (None, None, None, None), \
        "an unknown word leaves the counts unknown"
    try:
        HydraHostProvider.normalize_capacity({ep: [_snap({"items": []})]})
        raise AssertionError("a reshaped inventory must raise")
    except ValueError:
        pass


# --------------------------------------------------------------------------- the admin read

def _basic(user, pw):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


def _listing(provider, region, gpu=H100):
    return ComputeListing(provider=provider, sku="s", listing_id=f"{provider}:{region}", raw_gpu_name="H100",
                          canonical_gpu_name=gpu, gpu_count=8, region=region, country=None,
                          price_per_gpu_hour=Decimal("2"), price_per_instance_hour=Decimal("16"), market_type="on_demand",
                          provider_tier=None, interruptible=False, available=None, capacity=None, capacity_unit=None,
                          vcpu=None, ram_gb=None, storage_gb=None, observed_at=datetime.now(timezone.utc))


def test_admin_read():
    from fastapi.testclient import TestClient

    url = scratchdb.create(DB)
    try:
        Session = fixtures.session(url)
        normalize.SessionLocal = Session
        at = datetime.now(timezone.utc) - timedelta(minutes=1)
        capacity.save([o("lium", "8xH100", n=8, free=3, total=8, unit="gpu", in_stock=True, at=at),
                       o("latitude", "plan", n=8, free=2, unit="instance", in_stock=True, at=at, visibility="internal"),
                       o("lambda", "gpu_8x_h100", region=None, n=8, in_stock=True, at=at)])
        normalize.save([_listing("nebius", "eu-north1"), _listing("lium", "r1")])

        import main
        from accounts import security

        client = TestClient(main.app, headers={"X-OpenGrid-Request": "1"})
        saved = (settings.app_password, settings.public_pages, settings.viewer_user, settings.viewer_password,
                 settings.environment)
        try:
            security.reset()
            settings.app_password, settings.environment = "pw", "development"
            settings.viewer_user, settings.viewer_password = "vc", "look-only"
            settings.public_pages = True
            op, viewer = _basic("opengrid", "pw"), _basic("vc", "look-only")
            path = "/v1/ops/availability/counts"
            assert client.get(path).status_code == 401
            assert client.get(path, headers=viewer).status_code == 403
            got = client.get(path, params={"gpu": H100}, headers=op)
            assert got.status_code == 200, got.text
            b = got.json()
            assert b["meta"]["visibility"] == "internal" and b["meta"]["count"] == 1
            t = b["data"][0]
            assert t["gpu"] == H100 and t["summary"]["gpus_free_known"] == 3 + 16
            lat = line(t, "latitude")
            assert lat["visibility"] == "internal" and lat["gpus_free"] == 16, "the admin sees internal rows"
            assert [g["provider"] for g in t["no_stock_feed"]] == ["nebius"]
            assert t["summary"]["providers_in_stock_no_count"] == ["lambda"]
            every = client.get(path, headers=op).json()
            assert [x["gpu"] for x in every["data"]] == [H100]
            assert client.get(path, params={"gpu": "no-such-gpu"}, headers=op).status_code == 404
            assert not [p for p in main.app.openapi()["paths"] if "availability/counts" in p], "out of the public schema"
        finally:
            (settings.app_password, settings.public_pages, settings.viewer_user, settings.viewer_password,
             settings.environment) = saved
    finally:
        scratchdb.drop(DB)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
