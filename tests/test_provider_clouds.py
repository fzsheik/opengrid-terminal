"""Akamai/Linode, OVHcloud, Koyeb and Scaleway (its own EUR catalogue, converted at the ECB rate): general clouds.

Parsers run on trimmed copies of the REAL responses (tests/fixtures_providers/,
fetched 2026-10-13 with unauthenticated GETs). The last test pushes all four
through raw storage, normalize + quality screening, the hourly rollup and the
index vote in a scratch database.

Run:  .venv/bin/python tests/test_provider_clouds.py
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("OPENGRID_NO_JOBS", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402

import canonical  # noqa: E402
import fetch as fetch_mod  # noqa: E402
import mapping  # noqa: E402
import provider_meta  # noqa: E402
from providers import PROVIDERS  # noqa: E402
from providers.koyeb import ENDPOINT as KOYEB_EP, KoyebProvider  # noqa: E402
from providers.linode import ENDPOINT as LINODE_EP, LinodeProvider  # noqa: E402
from providers.ovhcloud import AVAILABILITIES, AVAILABILITY_PATH as OVH_AVAIL_PATH, CATALOGUES, OvhCloudProvider, baremetal_availability  # noqa: E402
from contextlib import contextmanager  # noqa: E402

import fx  # noqa: E402
from providers.scaleway import LEGACY_LISTING_IDS, SCALEWAY_CATALOG, ScalewayProvider, scaleway_eur_prices  # noqa: E402
from providers.shadeform import ShadeformCloud, gpu_label  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_providers"
NOW = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
NEW = ["linode", "ovhcloud", "scaleway", "koyeb"]
OVH_US, OVH_WS = CATALOGUES["US"][0], CATALOGUES["WS"][0]
SCW_CATALOG_PATH = SCALEWAY_CATALOG.replace("https://api.scaleway.com", "")


def text(name):
    return (FIX / name).read_text(encoding="utf-8")


def snap(payload, at=NOW):
    return SimpleNamespace(payload=payload, request=None, fetched_at=at)


def by_id(listings):
    return {l.listing_id: l for l in listings}


def raises(fn, *a):
    try:
        fn(*a)
    except ValueError:
        return True
    return False


def _serve(routes: dict[str, str], content_type="application/json"):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.raw_path.decode()
        for prefix, body in routes.items():
            if path.startswith(prefix):
                return httpx.Response(200, text=body, headers={"content-type": content_type})
        return httpx.Response(404, text="not found")
    return httpx.MockTransport(handler)


def _client(base, transport):
    return httpx.AsyncClient(base_url=base, transport=transport)


def _fetch(prov):
    return asyncio.run(prov.fetch())


CAP = Path(__file__).resolve().parent / "fixtures_capacity"


def _cap(provider: str, fragment: str):
    """A capacity endpoint's saved body (tests/fixtures_capacity), as one unpaged JSON response."""
    saved = json.loads((CAP / f"{provider}.json").read_text())
    return next(v[0] for k, v in saved.items() if fragment in k)


def linode_prov():
    availability = {**_cap("linode", "/v4/regions/availability"), "page": 1, "pages": 1}
    return LinodeProvider(_client("https://api.linode.com", _serve({
        "/v4/linode/types": text("linode_types.json"), "/v4/regions/availability": json.dumps(availability)})))


def ovh_prov(availability=None):
    # Both catalogues share a path; the mock tells them apart by subsidiary. The two bare-metal availability lists
    # share a path too, and are told apart by host. `availability` overrides them: (status, body).
    catalogues = _serve({
        "/1.0/order/catalog/public/cloud?ovhSubsidiary=US": text("ovhcloud_catalog_us.json"),
        "/1.0/order/catalog/public/cloud?ovhSubsidiary=WS": text("ovhcloud_catalog_ws.json")})

    def handler(request):
        if request.url.path == OVH_AVAIL_PATH:
            status, body = availability or (200, text("ovhcloud_baremetal_availabilities_"
                                                      f"{'us' if request.url.host == 'api.us.ovhcloud.com' else 'ws'}.json"))
            return httpx.Response(status, text=body, headers={"content-type": "application/json"})
        return catalogues.handler(request)
    return OvhCloudProvider(_client("https://x", httpx.MockTransport(handler)))


def koyeb_prov():
    return KoyebProvider(_client("https://app.koyeb.com", _serve({
        "/v1/catalog/instances": text("koyeb_instances.json"), "/v1/catalog/usage": json.dumps(_cap("koyeb", "/v1/catalog/usage"))})))


def scaleway_prov(catalog: str | None = None):
    # Prices from Scaleway's own EUR catalogue; capacity (and `available`) from its per-zone API (saved in
    # tests/fixtures_capacity). Availability paths come first: the catalogue path is a prefix of them.
    saved = json.loads((CAP / "scaleway.json").read_text())
    zones = {k: v[0] for k, v in saved.items() if "api.scaleway.com" in k}
    routes = {k.replace("https://api.scaleway.com", ""): json.dumps({"servers": v["servers"]})
              for k, v in sorted(zones.items(), key=lambda kv: not kv[0].endswith("/availability"))}
    return ScalewayProvider(_client("https://api.scaleway.com",
                                    _serve({SCW_CATALOG_PATH: catalog or text("scaleway_catalog_instance.json"), **routes})))


@contextmanager
def ecb_rate(rate="1.1172", day=None):
    """fx.rate_at answers with one ECB EUR->USD rate (None: no rate stored), so pure tests need no database."""
    real = fx.rate_at
    fixed = None if rate is None else fx.Rate("EUR", "USD", D(rate), day or (NOW - timedelta(days=1)).date())
    fx.rate_at = lambda at, base="EUR", quote="USD": fx.pick([fixed] if fixed else [], at)
    try:
        yield fixed
    finally:
        fx.rate_at = real


# --------------------------------------------------------------------------
# Akamai / Linode
# --------------------------------------------------------------------------


def test_linode():
    raws = _fetch(linode_prov())
    assert len(raws) == 2 and raws[0].ok and raws[0].endpoint == LINODE_EP
    assert raws[1].ok and raws[1].endpoint.startswith("/v4/regions/availability"), "capacity fetched next to prices"
    assert raws[1].payload["data"] and all("gpu" in r["plan"] for r in raws[1].payload["data"]), "GPU plans only"
    data = raws[0].payload["data"]
    assert len(data) == 13 and all(t["gpus"] for t in data), "CPU plans dropped before storage"
    assert raws[0].payload["pages"] == 1
    out = by_id(LinodeProvider.normalize({LINODE_EP: [snap(raws[0].payload)]}))
    assert len(out) == 13
    a4 = out["g2-gpu-rtx4000a4-m"]
    assert a4.raw_gpu_name == "RTX4000 Ada" and a4.canonical_gpu_name == "NVIDIA RTX 4000 Ada 20GB"
    assert a4.gpu_count == 4 and a4.price_per_instance_hour == D("3.57")
    assert a4.price_per_gpu_hour == D("3.57") / 4 == D("0.8925"), "plan price / 4 GPUs, exact"
    assert a4.market_type == "on_demand" and not a4.interruptible and a4.available is None
    assert a4.vcpu == 48 and a4.ram_gb == 196 and a4.storage_gb == 2048 and a4.region is None
    two = out["g2-gpu-rtx4000a2-s"]
    assert two.price_per_instance_hour == D("1.05") and two.price_per_gpu_hour == D("0.525")
    q = out["g1-gpu-rtx6000-2"]
    assert q.raw_gpu_name == "RTX6000" and q.canonical_gpu_name is None, "Quadro vs Ada not stated: unmapped"
    assert q.price_per_gpu_hour == D("1.5")

    # A region priced differently becomes its own listing; a monthly-only price is never used.
    body = json.loads(json.dumps(raws[0].payload))
    t = body["data"][0]
    t["region_prices"] = [{"id": "br-gru", "hourly": 1.8, "monthly": 1200}]
    body["data"][1]["price"] = {"hourly": None, "monthly": 1000}
    out = by_id(LinodeProvider.normalize({LINODE_EP: [snap(body)]}))
    assert out[f"{t['id']}:br-gru"].region == "br-gru" and out[f"{t['id']}:br-gru"].price_per_instance_hour == D("1.8")
    assert body["data"][1]["id"] not in out, "monthly-only price: no listing, never converted"

    # Fail closed.
    assert raises(LinodeProvider.normalize, {LINODE_EP: [snap({"types": []})]})
    assert raises(LinodeProvider.normalize, {LINODE_EP: [snap({**raws[0].payload, "pages": 2})]}), "paged"
    broken = {**raws[0].payload, "data": [{k: v for k, v in data[0].items() if k != "price"}]}
    assert raises(LinodeProvider.normalize, {LINODE_EP: [snap(broken)]})


# --------------------------------------------------------------------------
# OVHcloud
# --------------------------------------------------------------------------


def test_ovhcloud():
    raws = _fetch(ovh_prov())
    assert [r.endpoint for r in raws] == [OVH_US, OVH_WS, *AVAILABILITIES.values()] and all(r.ok for r in raws)
    raws = raws[:2]
    for r in raws:
        codes = [a["planCode"] for a in r.payload["addons"]]
        assert all(c.endswith(".consumption") and not c.startswith(("win-", "ai-")) for c in codes), codes
        assert "b3-8.consumption" not in codes and "l40s-90.monthly" not in codes, "kept GPU flavors only"
        assert r.payload["locale"]["currencyCode"] == "USD"
    out = by_id(OvhCloudProvider.normalize({r.endpoint: [snap(r.payload)] for r in raws}))
    assert sum(k.startswith("US:") for k in out) == 12 and sum(k.startswith("WS:") for k in out) == 19

    l40s = out["US:l40s-360"]
    assert l40s.raw_gpu_name == "L40S 48GB" and l40s.canonical_gpu_name == "NVIDIA L40S 48GB"
    assert l40s.gpu_count == 4 and l40s.price_per_instance_hour == D("7.2") and l40s.price_per_gpu_hour == D("1.8")
    assert l40s.country == "US" and l40s.region is None and l40s.available is None and l40s.market_type == "on_demand"
    assert l40s.vcpu == 60 and l40s.ram_gb == 360
    assert out["WS:l4-90"].canonical_gpu_name == "NVIDIA L4 24GB" and out["WS:l4-90"].price_per_gpu_hour == D("1")
    assert out["WS:l4-90"].country is None, "the World catalogue is not one country"
    h200 = out["WS:h200-1920"]
    assert h200.gpu_count == 8 and h200.price_per_instance_hour == D("49.56") and h200.price_per_gpu_hour == D("6.195")
    assert h200.canonical_gpu_name is None, "141GB H200 does not say SXM or NVL"
    a100 = out["WS:a100-720"]
    assert a100.price_per_gpu_hour == D("12.29") / 4 == D("3.0725") and a100.canonical_gpu_name is None
    assert out["WS:h100-380"].price_per_gpu_hour == D("2.99") and out["WS:h100-380"].canonical_gpu_name is None
    assert out["WS:t1-le-45"].canonical_gpu_name == "NVIDIA Tesla V100 16GB", "variant-less V100 name"
    assert out["US:t2-45"].canonical_gpu_name == "NVIDIA Tesla V100S 32GB PCIe", "the V100S is PCIe only"
    # Not offered for new instances: coming soon (RTX 5000, A10), legacy (GTX 10xx), untagged (WS t1-45).
    for gone in ("WS:rtx5000-28", "WS:a10-45", "WS:g1-15", "WS:t1-45", "WS:t2-45"):
        assert gone not in out, gone

    # Fail closed: a non-USD catalogue, a reshaped body, a price unit that no longer matches its label.
    us = json.loads(json.dumps(raws[0].payload))
    eur = {**us, "locale": {**us["locale"], "currencyCode": "EUR"}}
    assert raises(OvhCloudProvider.normalize, {OVH_US: [snap(eur)]})
    assert raises(OvhCloudProvider.normalize, {OVH_US: [snap({"plans": []})]})
    bad = json.loads(json.dumps(us))
    bad["addons"][0]["pricings"][0]["price"] *= 100
    assert raises(OvhCloudProvider.normalize, {OVH_US: [snap(bad)]}), "unit changed"
    minute = json.loads(json.dumps(us))
    minute["addons"][0]["blobs"]["commercial"]["price"]["interval"] = "PT1M"
    assert raises(OvhCloudProvider.normalize, {OVH_US: [snap(minute)]})


# --------------------------------------------------------------------------
# Koyeb
# --------------------------------------------------------------------------


def test_koyeb():
    raws = _fetch(koyeb_prov())
    raw = raws[0]
    assert raw.ok and raw.endpoint == KOYEB_EP
    assert len(raws) == 2 and raws[1].ok and raws[1].endpoint == "/v1/catalog/usage", "capacity fetched next to prices"
    assert raw.payload["count"] == raw.payload["listed"] == 44
    assert raw.payload["instances"] and all(i["gpu"] for i in raw.payload["instances"]), "CPU types dropped"
    out = by_id(KoyebProvider.normalize({KOYEB_EP: [snap(raw.payload)]}))
    assert len(out) == 15
    l40s = out["gpu-nvidia-l40s"]
    assert l40s.raw_gpu_name == "gpu-nvidia-l40s 48GB" and l40s.canonical_gpu_name == "NVIDIA L40S 48GB"
    assert l40s.price_per_gpu_hour == D("1.20") and l40s.region == "na" and l40s.available is None
    sxm = out["4-gpu-nvidia-a100-sxm"]
    assert sxm.raw_gpu_name == "gpu-nvidia-a100-sxm 80GB" and sxm.canonical_gpu_name == "NVIDIA A100 80GB SXM4"
    assert sxm.gpu_count == 4 and sxm.price_per_instance_hour == D("8.6") and sxm.price_per_gpu_hour == D("2.15")
    assert sxm.ram_gb == 720 and sxm.storage_gb == 1280 and sxm.provider_tier == "serverless"
    assert out["2-gpu-nvidia-h100"].price_per_gpu_hour == D("2.5")
    assert out["gpu-nvidia-rtx-4000-sff-ada"].canonical_gpu_name == "NVIDIA RTX 4000 SFF Ada 20GB"
    for unmapped in ("gpu-nvidia-h100", "gpu-nvidia-a100", "gpu-nvidia-h200", "gpu-nvidia-rtx-pro-6000"):
        assert out[unmapped].canonical_gpu_name is None, unmapped
    # Sales-gated (RESTRICTED, no regions) and $0 Tenstorrent types give no listing.
    for gone in ("gpu-nvidia-b200", "8-gpu-nvidia-h100", "gpu-nvidia-l4", "gpu-tenstorrent-n300s",
                 "4-gpu-tenstorrent-n300s"):
        assert gone not in out, gone

    # A display name that disagrees with gpu.count is skipped, never guessed.
    body = json.loads(json.dumps(raw.payload))
    for i in body["instances"]:
        if i["id"] == "2-gpu-nvidia-h100":
            i["gpu"]["count"] = 1
    assert "2-gpu-nvidia-h100" not in by_id(KoyebProvider.normalize({KOYEB_EP: [snap(body)]}))
    # Fail closed.
    assert raises(KoyebProvider.normalize, {KOYEB_EP: [snap({**raw.payload, "count": 120})]}), "paged"
    assert raises(KoyebProvider.normalize, {KOYEB_EP: [snap({"items": []})]})
    lost = {**raw.payload, "instances": [{k: v for k, v in raw.payload["instances"][0].items() if k != "price_hourly"}]}
    assert raises(KoyebProvider.normalize, {KOYEB_EP: [snap(lost)]})


# --------------------------------------------------------------------------
# Scaleway: its own EUR catalogue, converted at the ECB rate
# --------------------------------------------------------------------------


def _scw_by_endpoint(raws):
    return {r.endpoint: [snap(r.payload)] for r in raws if r.ok}


def test_scaleway_own_prices_converted():
    raws = _fetch(scaleway_prov())
    assert raws[0].endpoint == SCALEWAY_CATALOG and raws[0].ok and len(raws) == 1 + 2 * 9, "catalogue + 9 zones x 2"
    assert not any("shadeform" in r.endpoint or r.endpoint == "/v1/instances/types" for r in raws), "no Shadeform call"
    with ecb_rate("1.1172") as rate:
        out = by_id(ScalewayProvider.normalize(_scw_by_endpoint(raws)))
    # 6 GPU rows in the trimmed catalogue: 4 products Shadeform listed (their old ids) + 2 it never listed.
    assert set(out) == {"H100", "B300x8", "L40Sx8", "L4", "H100-SXM-8-80G", "RENDER-S"}, set(out)
    h100 = out["H100"]
    assert h100.sku == "H100-1-80G" and h100.raw_gpu_name == "H100-PCIe 80GB"
    assert h100.canonical_gpu_name == "NVIDIA H100 80GB PCIe"
    # 0.047775 EUR per minute = 2.8665 EUR per hour; x 1.1172 = 3.2024538 -> 3.202454 USD (6 decimals, the column)
    assert h100.original_currency == "EUR" and h100.original_price_per_instance_hour == D("2.866500")
    assert h100.price_per_instance_hour == D("3.202454") and h100.price_per_gpu_hour == D("3.202454")
    assert h100.currency == "USD" and h100.fx_rate == D("1.1172") and h100.fx_rate_date == rate.rate_date
    assert h100.fx_source == "ecb" and h100.provider_tier == "vm" and h100.market_type == "on_demand"
    assert h100.region == "fr-par-2" and h100.country == "FR" and h100.available is True, "scarce in fr-par-2: in stock"
    assert h100.vcpu == 24 and h100.ram_gb == 240.0
    sxm = out["H100-SXM-8-80G"]  # a type Shadeform never listed
    assert sxm.canonical_gpu_name == "NVIDIA H100 80GB SXM5" and sxm.gpu_count == 8
    assert sxm.original_price_per_gpu_hour == D("3.166350") and sxm.price_per_instance_hour == D("28.299570")
    assert out["B300x8"].canonical_gpu_name == "NVIDIA B300 288GB SXM" and out["B300x8"].available is False, "shortage"
    assert out["B300x8"].price_per_instance_hour == D("67.032000"), "1 EUR per minute x 60 x 1.1172"
    assert out["RENDER-S"].canonical_gpu_name is None and out["RENDER-S"].original_price_per_instance_hour == D("1.221"), \
        "per-hour rows are not x60; P100 stays unmapped"
    assert out["L4"].region == "pl-waw-2" and out["L4"].canonical_gpu_name == "NVIDIA L4 24GB"
    for l in out.values():
        assert l.price_per_gpu_hour == l.price_per_gpu_hour.quantize(D("0.000001")), "stored scale: re-reads compare equal"
    with ecb_rate("1.1172"):
        assert ScalewayProvider.normalize(_scw_by_endpoint(raws)) == list(out.values()), "deterministic"


def test_scaleway_continuity_with_shadeform_history():
    """Every product Shadeform listed keeps its listing id, sku, GPU count and canonical GPU name."""
    shade = json.loads(text("shadeform_scaleway.json"))["instance_types"]
    old = {t["cloud_instance_type"]: t for t in shade if t["cloud"] == "scaleway"}
    assert set(LEGACY_LISTING_IDS) == set(old), "every Shadeform Scaleway type is mapped, nothing invented"
    catalog = json.loads(text("scaleway_catalog_instance.json"))
    # Put every legacy offer in the catalogue (the trimmed fixture has 4 of the 11), at its Shadeform hardware.
    have = {p["properties"]["instance"]["offer_id"] for p in catalog["products"] if "hardware" in p.get("properties", {})}
    template = next(p for p in catalog["products"] if p["product"] == "L40S-8-48G")
    for offer, t in old.items():
        if offer not in have:
            p = json.loads(json.dumps(template))
            p["product"] = p["properties"]["instance"]["offer_id"] = offer
            gtype = {"H100": "H100-PCIe", "L4": "L4", "L40S": "L40S"}[t["gpu_type"]]
            p["properties"]["hardware"]["gpu"] = {"count": t["num_gpus"], "type": gtype}
            catalog["products"].append(p)
    with ecb_rate():
        out = {l.sku: l for l in ScalewayProvider.normalize({SCALEWAY_CATALOG: [snap(catalog)]})}
    for offer, t in old.items():
        new = out[offer]
        assert new.listing_id == t["shade_instance_type"] == LEGACY_LISTING_IDS[offer], offer
        assert new.gpu_count == t["num_gpus"], offer
        assert new.canonical_gpu_name == canonical.canonical_gpu_name(gpu_label(t)) is not None, \
            (offer, new.canonical_gpu_name, gpu_label(t))
    assert {"H100-SXM-8-80G", "RENDER-S"} <= set(out) and not ({"H100-SXM-8-80G", "RENDER-S"} & set(old))


def test_scaleway_counted_once():
    """Shadeform never produces a Scaleway price row; Shadeform rows next to the catalogue change nothing."""
    raws = _fetch(scaleway_prov())
    by_ep = _scw_by_endpoint(raws)
    shade = json.loads(text("shadeform_scaleway.json"))
    with ecb_rate():
        assert ScalewayProvider.normalize({**by_ep, "/v1/instances/types": [snap(shade)]}) == ScalewayProvider.normalize(by_ep)
        assert ScalewayProvider.normalize({"/v1/instances/types": [snap(shade)]}) == [], "a Shadeform body alone: nothing"
    for cls in PROVIDERS.values():
        if issubclass(cls, ShadeformCloud):
            assert cls.cloud != "scaleway", cls.name
            try:
                assert cls.normalize({"/v1/instances/types": [snap(shade)]}) == [], cls.name
            except ValueError:
                assert cls.name == "paperspace", cls.name  # no Paperspace row is a failed read, never Scaleway rows
    try:
        type("ScalewayAgain", (ShadeformCloud,), {"name": "scw2", "cloud": "scaleway"})
        raise AssertionError("a Shadeform reader for Scaleway must be refused")
    except TypeError:
        pass
    assert not issubclass(PROVIDERS["scaleway"], ShadeformCloud)


def test_scaleway_fails_closed_without_a_fresh_rate():
    by_ep = _scw_by_endpoint(_fetch(scaleway_prov()))
    with ecb_rate(None):
        assert ScalewayProvider.normalize(by_ep) == [], "no ECB rate stored: nothing converted"
    with ecb_rate("1.1172", (NOW - timedelta(days=fx.MAX_AGE_DAYS + 1)).date()):
        assert ScalewayProvider.normalize(by_ep) == [], "a rate older than MAX_AGE_DAYS: nothing converted"
    with ecb_rate("1.1172", (NOW - timedelta(days=fx.MAX_AGE_DAYS)).date()):
        assert len(ScalewayProvider.normalize(by_ep)) == 6, "exactly MAX_AGE_DAYS old is still in force"
    real = fx.rate_at
    fx.rate_at = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down"))
    try:
        assert ScalewayProvider.normalize(by_ep) == [], "a failing lookup converts nothing and never raises"
    finally:
        fx.rate_at = real


def test_scaleway_zone_prices_that_differ_split():
    catalog = json.loads(text("scaleway_catalog_instance.json"))
    h100 = next(p for p in catalog["products"] if p["product"] == "H100-1-80G")
    other = json.loads(json.dumps(h100))
    other["locality"]["zone"] = "pl-waw-2"
    catalog["products"].append(json.loads(json.dumps(h100)) | {"locality": {"zone": "fr-par-1"}})  # same price
    other["price"]["retail_price"]["nanos"] = 50000000
    catalog["products"].append(other)
    with ecb_rate():
        out = by_id(ScalewayProvider.normalize({SCALEWAY_CATALOG: [snap(catalog)]}))
    assert "H100" not in out and out["H100@fr-par-1"].region == "fr-par-1; fr-par-2" and out["H100@fr-par-1"].country == "FR"
    assert out["H100@pl-waw-2"].original_price_per_instance_hour == D("3.000000"), "0.05 EUR/min, its own listing"


def test_scaleway_catalogue_rows_and_failures():
    """Odd rows are skipped, never guessed; a broken or GPU-less catalogue is a failed fetch and lists nothing."""
    raws = _fetch(scaleway_prov())
    cat = raws[0]
    products = cat.payload["products"]
    assert len(products) == 6 and all(p["properties"]["hardware"]["gpu"]["count"] for p in products), "GPU rows only (the retired GPU-3070 rows carry no hardware block)"
    prices = {(p.offer_id, p.zone): p for p in scaleway_eur_prices({SCALEWAY_CATALOG: [snap(cat.payload)]})}
    assert prices[("H100-SXM-8-80G", "fr-par-2")].eur_per_gpu_hour == D("3.16635")
    assert prices[("RENDER-S", "fr-par-1")].unit == "hour"
    odd = json.loads(json.dumps(cat.payload))
    odd["products"][0]["price"]["retail_price"]["currency_code"] = "USD"
    odd["products"][1]["unit_of_measure"]["unit"] = "month"
    odd["products"][2]["status"] = "preview"
    assert len(scaleway_eur_prices({SCALEWAY_CATALOG: [snap(odd)]})) == 4
    with ecb_rate():
        assert len(ScalewayProvider.normalize({SCALEWAY_CATALOG: [snap(odd)]})) == 3, "only general_availability is listed"
        assert raises(ScalewayProvider.normalize, {SCALEWAY_CATALOG: [snap({"products": "?"})]})
    for body, status in (("maintenance", 503), (json.dumps({"products": "?"}), 200),
                         (json.dumps({"products": [], "total_count": 0}), 200),
                         (json.dumps({"products": [{"product_category": "Instance"}], "total_count": 5}), 200)):
        prov = ScalewayProvider(_client("https://api.scaleway.com", httpx.MockTransport(
            lambda r, body=body, status=status: httpx.Response(status, text=body, headers={"content-type": "application/json"})
            if r.url.path.startswith(SCW_CATALOG_PATH) else httpx.Response(404, text="nf"))))
        raws = _fetch(prov)
        cat = [r for r in raws if r.endpoint == SCALEWAY_CATALOG]
        assert len(cat) == 1 and not cat[0].ok and cat[0].payload is None, (body, cat)
        with ecb_rate():
            assert ScalewayProvider.normalize(_scw_by_endpoint(raws)) == [], "no catalogue, no prices"


# --------------------------------------------------------------------------
# Fetch failures, registry, classes
# --------------------------------------------------------------------------


def test_ovhcloud_baremetal_raw_only():
    """Dedicated-server GPU stock is stored raw (GPU rows only) and read by a pure helper; never a listing."""
    raws = _fetch(ovh_prov())
    avail = {r.payload["subsidiary"]: r for r in raws[2:]}
    assert set(avail) == {"WS", "US"} and all(r.ok for r in avail.values())
    assert avail["US"].payload["total_rows"] == 7 and len(avail["US"].payload["rows"]) == 5, "non-GPU rows dropped"
    assert len(avail["WS"].payload["rows"]) == 3
    by_ep = {r.endpoint: [snap(r.payload)] for r in raws}
    rows = baremetal_availability(by_ep)
    assert len(rows) == 10 + 10, len(rows)  # (configuration, datacenter) pairs: US 5 configs x 2, World 6 + 2 + 2
    v100 = [r for r in rows if r.plan_code == "21hgrai012-us" and r.datacenter == "vin"]
    assert {r.availability for r in v100} == {"1H-low", "72H"} and all(r.gpu_count == 4 for r in v100)
    assert v100[0].gpu_model == "nvidia-tesla-v100s" and v100[0].subsidiary == "US"
    l40s = next(r for r in rows if r.subsidiary == "WS" and r.gpu_option == "gpu-4xnvidia-l40s-48g")
    assert l40s.server == "24hgrai02" and l40s.availability == "unavailable"
    assert {r.availability for r in rows} == {"1H-low", "72H", "unavailable", "unknown"}, "words kept verbatim"
    # Listings are the Public Cloud catalogues' only: the same with or without the bare-metal lists.
    prices = {r.endpoint: [snap(r.payload)] for r in raws[:2]}
    assert OvhCloudProvider.normalize(by_ep) == OvhCloudProvider.normalize(prices)
    assert OvhCloudProvider.normalize_capacity(by_ep) == [], "no capacity rows: bare metal joins to no listing"


def test_ovhcloud_baremetal_fails_open():
    """A down, reshaped or GPU-less availability list is a failed fetch; the price catalogues are untouched."""
    for status, body in ((503, "maintenance"), (200, json.dumps({"rows": []})),
                         (200, json.dumps([{"planCode": "x", "datacenters": []}]))):
        raws = _fetch(ovh_prov((status, body)))
        assert all(not r.ok and r.payload is None and r.error for r in raws[2:]), (status, body)
        assert all(r.ok for r in raws[:2])
        assert len(OvhCloudProvider.normalize({r.endpoint: [snap(r.payload)] for r in raws[:2]})) == 31


def test_fetch_failures():
    """A non-JSON or error response is stored as a failed / unreshaped response, never as prices."""
    def failing():
        return httpx.MockTransport(lambda r: httpx.Response(503, text="maintenance"))
    for prov in (LinodeProvider(_client("https://x", failing())), OvhCloudProvider(_client("https://x", failing())),
                 KoyebProvider(_client("https://x", failing()))):
        raws = _fetch(prov)
        assert raws and all(not r.ok and r.payload is None for r in raws), prov.name


def test_registry():
    assert set(NEW) <= set(PROVIDERS)
    names = {p.name for p in fetch_mod.build_fetchers()}
    assert set(NEW) <= names, names
    assert mapping.check_mapping_coverage() == {}
    assert set(NEW) <= set(mapping.PROVIDER_MAPPINGS)
    assert set(PROVIDERS) <= set(provider_meta.PROVIDER_META), "every provider is classified"
    assert {p: provider_meta.meta(p).provider_class for p in NEW} == dict.fromkeys(NEW, "general_cloud")
    assert {provider_meta.meta(p).source_type for p in ("linode", "ovhcloud", "koyeb", "scaleway")} == {"public_api"}
    # All four are read directly; none through Shadeform (Scaleway was, until its own prices were converted).
    via = {c.cloud for c in PROVIDERS.values() if isinstance(c, type) and issubclass(c, ShadeformCloud)}
    direct = {n for n, c in PROVIDERS.items() if not issubclass(c, ShadeformCloud)}
    assert not (via & direct), via & direct
    assert not (set(NEW) & via) and set(NEW) <= direct
    # New aliases never merge variants.
    assert canonical.canonical_gpu_name("H100 pcie") == "NVIDIA H100 80GB PCIe"
    assert canonical.canonical_gpu_name("H100 80GB") is None and canonical.canonical_gpu_name("RTX6000") is None


# --------------------------------------------------------------------------
# Pipeline: raw storage -> normalize + quality -> rollup -> index votes
# --------------------------------------------------------------------------


def test_pipeline():
    import fixtures
    import normalize
    import scratchdb
    from sqlalchemy import text as sql
    from analytics import indices, rollups

    db = "opengrid_test_clouds"
    url = scratchdb.create(db)
    try:
        Session = fixtures.session(url)
        fetch_mod.SessionLocal = Session
        normalize.SessionLocal = Session

        now = datetime.now(timezone.utc).replace(microsecond=0)
        from store.fx import FxRate
        with Session.begin() as s:  # yesterday's ECB rate (published at 16:00 Frankfurt, before both polls)
            day = (now - timedelta(days=2)).date()
            s.add(FxRate(rate_date=day, base="EUR", quote="USD", rate=D("1.1172"), source="ecb",
                         first_seen_at=now, updated_at=now))
        for when in (now - timedelta(hours=3), now - timedelta(minutes=5)):  # two polls, three hours apart
            responses = []
            for p in (linode_prov(), ovh_prov(), scaleway_prov(), koyeb_prov()):
                responses += [r.model_copy(update={"fetched_at": when}) for r in _fetch(p)]
            assert all(r.ok for r in responses), [r.error for r in responses if not r.ok]
            fetch_mod.save(responses)
            normalize.refresh(NEW)

        with Session() as s:
            stored = dict(s.execute(sql("SELECT provider, count(*) FROM raw_snapshots GROUP BY 1")).all())
            bodies = s.execute(sql("SELECT count(*) FROM raw_payloads")).scalar()
            listed = dict(s.execute(sql("SELECT provider, count(*) FROM compute_listings GROUP BY 1")).all())
            held = s.execute(sql("SELECT provider, listing_id, rule FROM quality_quarantine WHERE status = 'pending'")).all()
            cap = dict(s.execute(sql("SELECT provider, count(*) FROM capacity_current GROUP BY 1")).all())
            cap_changes = s.execute(sql("SELECT count(*) FROM capacity_changes")).scalar()
        assert cap == {"linode": 429, "koyeb": 25, "scaleway": 29}, f"the poll fills capacity through the same refresh: {cap}"
        assert cap_changes == 429 + 25 + 29, "the second identical poll adds no capacity history"
        # Linode and Koyeb store a capacity response next to the price response each poll (capacity.py).
        # Scaleway: its own EUR product catalogue (prices) + a catalogue and availability response for each of its 9
        # zones (19 per poll; no Shadeform call any more).
        # OVHcloud adds its two bare-metal availability lists (4 per poll).
        assert stored == {"linode": 4, "ovhcloud": 8, "scaleway": 38, "koyeb": 4}, stored
        # Linode 2 + OVHcloud 4 + Koyeb 2, + Scaleway: 3 GPU zones x 2 bodies, plus 6 GPU-less zones whose filtered
        # catalogue and availability are the same empty body (stored once each), plus the EUR catalogue = 7.
        # The second, identical poll adds none.
        assert bodies == 21, f"identical bodies (plus OVHcloud's 2 bare-metal lists) are stored once: {bodies}"
        assert listed == {"linode": 13, "ovhcloud": 31, "scaleway": 6, "koyeb": 15}, listed
        with Session() as s:
            prov = s.execute(sql("SELECT listing_id, original_currency, fx_rate, fx_rate_date, price_per_instance_hour "
                                 "FROM compute_listings WHERE provider = 'scaleway' AND listing_id = 'H100'")).one()
            obs = s.execute(sql("SELECT count(*), min(original_currency), min(fx_rate) FROM listing_observations "
                                "WHERE provider = 'scaleway' AND listing_id = 'H100'")).one()
            others = s.execute(sql("SELECT count(*) FROM compute_listings WHERE provider <> 'scaleway' "
                                   "AND original_currency IS NOT NULL")).scalar()
        assert prov.original_currency == "EUR" and prov.fx_rate == D("1.1172") and prov.fx_rate_date == day
        assert prov.price_per_instance_hour == D("3.202454")
        assert tuple(obs) == (1, "EUR", D("1.1172")), f"same rate, same EUR price: one observation, with provenance {obs}"
        assert others == 0, "USD providers carry no FX provenance"
        assert not held, f"nothing held on clean data: {held}"

        rollups.rebuild()
        with Session() as s:
            last = s.execute(sql("SELECT max(hour) FROM market_hourly")).scalar()
            rows = [dict(r._mapping) for r in s.execute(sql(
                "SELECT segment, gpu, provider, region, country, hour, min_price FROM market_hourly "
                "WHERE hour = :h AND min_price IS NOT NULL"), {"h": last})]
        votes = indices.votes_by_index(rows)

        def voters(gpu, suffix=""):
            return set(votes.get(indices.gpu_index_id(gpu) + suffix, {}))

        # Scaleway's only L40S row in the trimmed catalogue (L40S-8-48G) is in shortage in both its zones: no vote.
        assert voters("NVIDIA L40S 48GB", ".general-cloud") == {"ovhcloud", "koyeb"}
        assert voters("NVIDIA A100 80GB SXM4", ".general-cloud") == {"koyeb"}
        # GPUs with no class index yet vote in the all-provider index only.
        assert voters("NVIDIA L4 24GB") == {"ovhcloud", "scaleway"}
        assert voters("NVIDIA RTX 4000 Ada 20GB") == {"linode"}
        assert voters("NVIDIA H100 80GB PCIe") == {"scaleway"}
        # Scaleway's H100-SXM-8 (a type Shadeform lacked) is listed but in shortage at fr-par-2: listed, no vote.
        assert voters("NVIDIA H100 80GB SXM5") == set()
        assert not any(set(NEW) & set(v) for k, v in votes.items() if k.endswith(".neocloud")), \
            "general clouds never vote in a neocloud index"
        l40s = votes[indices.gpu_index_id("NVIDIA L40S 48GB") + ".general-cloud"]
        assert abs(l40s["ovhcloud"][0] - 1.8) < 1e-9 and abs(l40s["koyeb"][0] - 1.2) < 1e-9
    finally:
        scratchdb.drop(db)


if __name__ == "__main__":
    for t in (test_linode, test_ovhcloud, test_koyeb, test_scaleway_own_prices_converted,
              test_scaleway_continuity_with_shadeform_history, test_scaleway_counted_once,
              test_scaleway_fails_closed_without_a_fresh_rate, test_scaleway_zone_prices_that_differ_split,
              test_scaleway_catalogue_rows_and_failures, test_ovhcloud_baremetal_raw_only, test_ovhcloud_baremetal_fails_open,
              test_fetch_failures, test_registry,
              test_pipeline):
        t()
        print(t.__name__, "ok")
    print("all passed")
