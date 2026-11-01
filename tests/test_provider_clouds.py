"""Akamai/Linode, OVHcloud, Koyeb and Scaleway (via Shadeform): general-purpose clouds.

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
from providers.ovhcloud import CATALOGUES, OvhCloudProvider  # noqa: E402
from providers.shadeform import SCALEWAY_CATALOG, ScalewayProvider, ShadeformCloud, scaleway_eur_prices  # noqa: E402

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


def ovh_prov():
    # Both catalogues share a path; the mock tells them apart by subsidiary.
    return OvhCloudProvider(_client("https://x", _serve({
        "/1.0/order/catalog/public/cloud?ovhSubsidiary=US": text("ovhcloud_catalog_us.json"),
        "/1.0/order/catalog/public/cloud?ovhSubsidiary=WS": text("ovhcloud_catalog_ws.json")})))


def koyeb_prov():
    return KoyebProvider(_client("https://app.koyeb.com", _serve({
        "/v1/catalog/instances": text("koyeb_instances.json"), "/v1/catalog/usage": json.dumps(_cap("koyeb", "/v1/catalog/usage"))})))


def scaleway_prov():
    # Prices from Shadeform; capacity from Scaleway's own per-zone API (saved in tests/fixtures_capacity).
    # Availability paths come first: the catalogue path is a prefix of them.
    saved = json.loads((CAP / "scaleway.json").read_text())
    zones = {k: v[0] for k, v in saved.items() if "api.scaleway.com" in k}
    routes = {k.replace("https://api.scaleway.com", ""): json.dumps({"servers": v["servers"]})
              for k, v in sorted(zones.items(), key=lambda kv: not kv[0].endswith("/availability"))}
    return ScalewayProvider(_client("https://api.shadeform.ai",
                                    _serve({"/v1/instances/types": text("shadeform_scaleway.json"),
                                            SCW_CATALOG_PATH: text("scaleway_catalog_instance.json"), **routes})))


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
    assert [r.endpoint for r in raws] == [OVH_US, OVH_WS] and all(r.ok for r in raws)
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
    assert out["US:t2-45"].canonical_gpu_name is None, "no canonical V100S"
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
# Scaleway, as listed by Shadeform
# --------------------------------------------------------------------------


def test_scaleway():
    raw = _fetch(scaleway_prov())[0]
    assert {t["cloud"] for t in raw.payload["instance_types"]} == {"scaleway"}, "other clouds dropped"
    out = by_id(ScalewayProvider.normalize({"/v1/instances/types": [snap(raw.payload)]}))
    assert len(out) == 11
    h100 = out["H100x2"]
    assert h100.sku == "H100-2-80G" and h100.canonical_gpu_name == "NVIDIA H100 80GB PCIe"
    assert h100.price_per_instance_hour == D("6.60") and h100.price_per_gpu_hour == D("3.30"), "cents -> $ / 2"
    b300 = out["B300x8"]
    assert b300.raw_gpu_name == "B300 sxm" and b300.canonical_gpu_name == "NVIDIA B300 288GB SXM"
    assert b300.price_per_gpu_hour == D("72.06") / 8 and b300.available is False
    assert out["L4"].canonical_gpu_name == "NVIDIA L4 24GB" and out["L4"].price_per_gpu_hour == D("0.95")
    assert out["L40Sx8"].canonical_gpu_name == "NVIDIA L40S 48GB" and out["L40Sx8"].price_per_gpu_hour == D("1.70")
    assert out["H100"].available is True and out["H100"].provider_tier == "vm/via-shadeform"


def test_scaleway_own_catalogue_raw_only():
    """Scaleway's own EUR catalogue is stored raw (GPU rows only) and read by a pure helper; never a listing."""
    raws = _fetch(scaleway_prov())
    cat = [r for r in raws if r.endpoint == SCALEWAY_CATALOG]
    assert len(cat) == 1 and cat[0].ok, [r.error for r in cat]
    products = cat[0].payload["products"]
    assert len(products) == 6 and all(p["properties"]["hardware"]["gpu"]["count"] for p in products), "GPU rows only (the retired GPU-3070 rows carry no hardware block)"
    by_ep = {SCALEWAY_CATALOG: [snap(cat[0].payload)]}
    prices = {(p.offer_id, p.zone): p for p in scaleway_eur_prices(by_ep)}
    assert len(prices) == 6
    h100 = prices[("H100-1-80G", "fr-par-2")]  # 0.047775 EUR per minute
    assert h100.eur_per_instance_hour == D("2.8665") and h100.gpu_count == 1 and h100.gpu_type == "H100-PCIe"
    sxm = prices[("H100-SXM-8-80G", "fr-par-2")]  # a type Shadeform does not list
    assert sxm.eur_per_instance_hour == D("25.3308") and sxm.eur_per_gpu_hour == D("3.16635") and sxm.gpu_count == 8
    assert prices[("RENDER-S", "fr-par-1")].eur_per_instance_hour == D("1.221"), "per-hour rows are not x60"
    assert prices[("RENDER-S", "fr-par-1")].unit == "hour" and h100.status == "general_availability"
    # Listings still come from Shadeform only, in USD: the catalogue never changes them and never adds one.
    shade = {"/v1/instances/types": [snap(raws[0].payload)]}
    assert ScalewayProvider.normalize({**shade, **by_ep}) == ScalewayProvider.normalize(shade)
    assert {l.currency for l in ScalewayProvider.normalize({**shade, **by_ep})} == {"USD"}
    # A non-EUR or oddly-measured row is skipped, never guessed.
    odd = json.loads(json.dumps(cat[0].payload))
    odd["products"][0]["price"]["retail_price"]["currency_code"] = "USD"
    odd["products"][1]["unit_of_measure"]["unit"] = "month"
    assert len(scaleway_eur_prices({SCALEWAY_CATALOG: [snap(odd)]})) == 4


def test_scaleway_catalogue_fails_open():
    """A broken or GPU-less catalogue is stored as a failed fetch; the Shadeform prices are untouched."""
    for body, status in (("maintenance", 503), (json.dumps({"products": "?"}), 200),
                         (json.dumps({"products": [], "total_count": 0}), 200),
                         (json.dumps({"products": [{"product_category": "Instance"}], "total_count": 5}), 200)):
        prov = ScalewayProvider(_client("https://api.shadeform.ai", httpx.MockTransport(
            lambda r, body=body, status=status: httpx.Response(status, text=body, headers={"content-type": "application/json"})
            if r.url.path.startswith(SCW_CATALOG_PATH) else
            httpx.Response(200, text=text("shadeform_scaleway.json"), headers={"content-type": "application/json"})
            if r.url.path == "/v1/instances/types" else httpx.Response(404, text="nf"))))
        raws = _fetch(prov)
        cat = [r for r in raws if r.endpoint == SCALEWAY_CATALOG]
        assert len(cat) == 1 and not cat[0].ok and cat[0].payload is None, (body, cat)
        assert raws[0].ok and len(ScalewayProvider.normalize({"/v1/instances/types": [snap(raws[0].payload)]})) == 11


# --------------------------------------------------------------------------
# Fetch failures, registry, classes
# --------------------------------------------------------------------------


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
    assert provider_meta.meta("scaleway").source_type == "aggregator"
    assert {provider_meta.meta(p).source_type for p in ("linode", "ovhcloud", "koyeb")} == {"public_api"}
    # Scaleway is read through Shadeform only; Linode / OVH / Koyeb only directly.
    via = {c.cloud for c in PROVIDERS.values() if isinstance(c, type) and issubclass(c, ShadeformCloud)}
    direct = {n for n, c in PROVIDERS.items() if not issubclass(c, ShadeformCloud)}
    assert not (via & direct), via & direct
    assert "scaleway" in via and not ({"linode", "ovhcloud", "koyeb"} & via)
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
        # Scaleway adds a catalogue + availability response for each of its 9 zones and its own EUR product
        # catalogue (20 per poll).
        assert stored == {"linode": 4, "ovhcloud": 4, "scaleway": 40, "koyeb": 4}, stored
        # 7 as before + Scaleway capacity: 3 GPU zones x 2 bodies, plus 6 GPU-less zones whose filtered catalogue and
        # availability are the same empty body (stored once each) = 19, plus the EUR catalogue = 20. The second,
        # identical poll adds none.
        assert bodies == 20, f"identical bodies are stored once: {bodies}"
        assert listed == {"linode": 13, "ovhcloud": 31, "scaleway": 11, "koyeb": 15}, listed
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

        assert voters("NVIDIA L40S 48GB", ".general-cloud") == {"ovhcloud", "scaleway", "koyeb"}
        assert voters("NVIDIA A100 80GB SXM4", ".general-cloud") == {"koyeb"}
        # GPUs with no class index yet vote in the all-provider index only.
        assert voters("NVIDIA L4 24GB") == {"ovhcloud", "scaleway"}
        assert voters("NVIDIA RTX 4000 Ada 20GB") == {"linode"}
        assert voters("NVIDIA H100 80GB PCIe") == {"scaleway"}
        assert voters("NVIDIA H100 80GB SXM5") == set(), "no new H100 is labelled SXM"
        assert not any(set(NEW) & set(v) for k, v in votes.items() if k.endswith(".neocloud")), \
            "general clouds never vote in a neocloud index"
        l40s = votes[indices.gpu_index_id("NVIDIA L40S 48GB") + ".general-cloud"]
        assert abs(l40s["ovhcloud"][0] - 1.8) < 1e-9 and abs(l40s["koyeb"][0] - 1.2) < 1e-9
    finally:
        scratchdb.drop(db)


if __name__ == "__main__":
    for t in (test_linode, test_ovhcloud, test_koyeb, test_scaleway, test_scaleway_own_catalogue_raw_only,
              test_scaleway_catalogue_fails_open, test_fetch_failures, test_registry,
              test_pipeline):
        t()
        print(t.__name__, "ok")
    print("all passed")
