"""Microsoft Azure (Retail Prices API).

The parser runs on trimmed copies of the REAL responses (tests/fixtures_providers/
azure_eastus*.json, fetched 2026-10-13 with the provider's own anonymous filtered
GET), served through a mock transport. The last test pushes both regions through
raw storage, normalize + quality screening, the hourly rollup and the index vote
in a scratch database.

Run:  .venv/bin/python tests/test_provider_azure.py
"""

import asyncio
import copy
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

os.environ.setdefault("OPENGRID_NO_JOBS", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402

import canonical  # noqa: E402
import fetch  # noqa: E402
import mapping  # noqa: E402
import provider_meta  # noqa: E402
from providers import PROVIDERS  # noqa: E402
from providers import azure  # noqa: E402
from providers.azure import AzureProvider  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_providers"
NOW = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)


def body(region):
    return json.loads((FIX / f"azure_{region}.json").read_text(encoding="utf-8"))


def snap(payload, at=NOW):
    return SimpleNamespace(payload=payload, request=None, fetched_at=at)


def by_id(listings):
    return {l.listing_id: l for l in listings}


def _serve(pages):
    """pages: {region: [page body, ...]}; page n>0 is reached via its NextPageLink."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        if request.url.path != azure.PATH:
            return httpx.Response(404, text="not found")
        q = parse_qs(urlsplit(str(request.url)).query)
        f = q.get("$filter", [""])[0]
        region = next((r for r in pages if f"armRegionName eq '{r}'" in f), None)
        if region is None:
            return httpx.Response(400, json={"Error": "bad filter"})
        n = int(q.get("$skip", ["0"])[0]) // 1000
        return httpx.Response(200, json=pages[region][n])

    return httpx.MockTransport(handler), seen


def provider(pages):
    transport, seen = _serve(pages)
    return AzureProvider(httpx.AsyncClient(base_url=azure.BASE_URL, transport=transport)), seen


def real_pages():
    return {"eastus": [body("eastus")], "eastus2": [body("eastus2")]}


def normalized(raws):
    return by_id(AzureProvider.normalize({r.endpoint: [snap(r.payload)] for r in raws}))


# --------------------------------------------------------------------------
# Fetch + normalize on today's responses
# --------------------------------------------------------------------------


def test_fetch_and_normalize():
    prov, seen = provider(real_pages())
    raws = asyncio.run(prov.fetch())
    assert [r.endpoint for r in raws] == ["/api/retail/prices#eastus", "/api/retail/prices#eastus2"]
    assert all(r.ok for r in raws), [r.error for r in raws]
    assert len(seen) == 2, "one request per region"
    assert all(len(r.endpoint) <= 256 for r in raws), "fits raw_snapshots.endpoint"
    assert "armRegionName eq 'eastus'" in raws[0].request["$filter"], "exact filter kept as provenance"
    assert "priceType eq 'Consumption'" in raws[0].request["$filter"]
    assert raws[0].payload["region"] == "eastus" and len(raws[0].payload["items"]) == 31
    assert set(raws[0].payload["items"][0]) == set(azure._FIELDS), "only the read fields are stored"

    out = normalized(raws)
    assert len(out) == 22, len(out)

    nvl = out["Standard_NC40ads_H100_v5:eastus"]
    assert nvl.raw_gpu_name == "Nvidia PCIe H100 GPU (94GB)" and nvl.canonical_gpu_name == "NVIDIA H100 94GB NVL"
    assert nvl.gpu_count == 1 and nvl.price_per_gpu_hour == D("6.98") and nvl.price_per_instance_hour == D("6.98")
    assert nvl.market_type == "on_demand" and not nvl.interruptible and nvl.available is None
    assert nvl.region == "eastus" and nvl.country == "US" and nvl.provider_tier == "hyperscaler"
    spot = out["Standard_NC40ads_H100_v5:eastus:spot"]
    assert spot.market_type == "spot" and spot.interruptible and spot.price_per_instance_hour == D("1.289904")

    a100 = out["Standard_NC24ads_A100_v4:eastus"]
    assert a100.canonical_gpu_name == "NVIDIA A100 80GB PCIe" and a100.price_per_gpu_hour == D("3.673")
    t4 = out["Standard_NC64as_T4_v3:eastus"]
    assert t4.canonical_gpu_name == "NVIDIA T4 16GB" and t4.gpu_count == 4
    assert t4.price_per_instance_hour == D("4.352") and t4.price_per_gpu_hour == D("1.088"), "VM price / 4 GPUs"
    v100 = out["Standard_NC6s_v3:eastus"]
    assert v100.canonical_gpu_name == "NVIDIA Tesla V100 16GB", "no board stated: the variant-less V100"
    assert v100.price_per_gpu_hour == D("3.06")
    a10 = out["Standard_NV36ads_A10_v5:eastus"]
    assert a10.canonical_gpu_name == "NVIDIA A10 24GB PCIe" and a10.price_per_gpu_hour == D("3.2")
    rtx = out["Standard_NC144ds_xl_RTXPRO6000BSE_v6:eastus"]
    assert rtx.canonical_gpu_name == "NVIDIA RTX PRO 6000 96GB SE" and rtx.price_per_gpu_hour == D("6.38")

    # ND flagships: 8 GPUs, exact division; HGX boards, so the SXM parts.
    h100 = out["Standard_ND96isr_H100_v5:eastus"]
    assert h100.gpu_count == 8 and h100.price_per_instance_hour == D("98.32")
    assert h100.price_per_gpu_hour == D("98.32") / 8 == D("12.29")
    assert h100.raw_gpu_name == "Nvidia H100 GPU (80GB)" and h100.canonical_gpu_name == "NVIDIA H100 80GB SXM5"
    assert out["Standard_ND40rs_v2:eastus"].canonical_gpu_name is None, "no canonical V100 32GB"
    h200 = out["Standard_ND96isr_H200_v5:eastus2"]
    assert h200.price_per_gpu_hour == D("10.6") and h200.canonical_gpu_name == "NVIDIA H200 141GB SXM5"
    mi = out["Standard_ND96isr_MI300X_v5:eastus2"]
    assert mi.canonical_gpu_name == "AMD Instinct MI300X 192GB" and mi.price_per_gpu_hour == D("6")
    assert out["Standard_ND96isr_MI300X_v5:eastus2:spot"].price_per_instance_hour == D("8.8704")
    assert out["Standard_NC24ads_A100_v4:eastus2"].region == "eastus2", "regions priced separately"

    # Never read: Low Priority meters, fractional / unlisted sizes, Windows ("Srs Win") products.
    assert not any("Low Priority" in l.sku for l in out.values())
    skipped = ("Standard_NV6ads_A10_v5", "Standard_NC36ds_xl_RTXPRO6000BSE_v6", "Standard_NC6_Promo",
               "Standard_ND128isr_NDR_GB200_v6", "Standard_NCC40ads_H100_v5")
    assert not any(l.sku in skipped for l in out.values())
    lp = {r["skuName"] for r in raws[0].payload["items"] if r["skuName"].endswith("Low Priority")}
    assert lp, "fixture keeps Low Priority rows to prove they are dropped"
    # Every listing is spot XOR on-demand, never mixed.
    assert all((l.market_type == "spot") == l.listing_id.endswith(":spot") == l.interruptible for l in out.values())


def test_market_rules():
    row = {"productName": "Virtual Machines NCadsH100v5 Series", "type": "Consumption", "unitOfMeasure": "1 Hour"}
    assert azure.market({**row, "skuName": "NC40adsH100v5"}) == "on_demand"
    assert azure.market({**row, "skuName": "NC40adsH100v5 Spot"}) == "spot"
    assert azure.market({**row, "skuName": "NC40adsH100v5 Low Priority"}) is None
    assert azure.market({**row, "skuName": "x", "productName": "Virtual Machines NCadsH100v5 Series Windows"}) is None
    assert azure.market({**row, "skuName": "x", "productName": "Virtual Machines NCCadsv5 Srs Win"}) is None
    assert azure.market({**row, "skuName": "x", "unitOfMeasure": "1/Month"}) is None
    assert azure.market({**row, "skuName": "x", "type": "Reservation"}) is None
    # A Windows row that reached storage never becomes a listing.
    p = body("eastus")
    win = copy.deepcopy(next(i for i in p["Items"] if i["armSkuName"] == "Standard_NC6s_v3" and "Spot" not in i["skuName"]))
    win["productName"] += " Win"
    win["retailPrice"] = 99.0
    rows = [{k: i.get(k) for k in azure._FIELDS} for i in p["Items"] + [win]]
    out = by_id(AzureProvider.normalize({"/api/retail/prices#eastus": [snap({"region": "eastus", "items": rows})]}))
    assert out["Standard_NC6s_v3:eastus"].price_per_instance_hour == D("3.06")


def test_pagination():
    full = body("eastus")
    one, two = copy.deepcopy(full), copy.deepcopy(full)
    one["Items"], two["Items"] = full["Items"][:10], full["Items"][10:]
    one["NextPageLink"] = "https://prices.azure.com:443/api/retail/prices?$filter=" + \
        "armRegionName eq 'eastus'&$skip=1000"
    prov, seen = provider({"eastus": [one, two], "eastus2": [body("eastus2")]})
    raws = asyncio.run(prov.fetch())
    assert all(r.ok for r in raws) and len(seen) == 3
    assert len(raws[0].payload["items"]) == 31, "both pages merged into one snapshot"
    assert normalized(raws) == normalized(asyncio.run(provider(real_pages())[0].fetch()))


# --------------------------------------------------------------------------
# Fail closed: a changed response fails the poll, never records nothing
# --------------------------------------------------------------------------


def _fails(pages, region="eastus"):
    raws = asyncio.run(provider(pages)[0].fetch())
    r = raws[0] if region == "eastus" else raws[1]
    assert not r.ok and r.payload is None and "ValueError" in r.error, r.error
    return r.error


def test_fail_closed():
    good = body("eastus2")

    renamed = body("eastus")
    renamed["Prices"] = renamed.pop("Items")
    assert "no Items list" in _fails({"eastus": [renamed], "eastus2": [good]})

    lost = body("eastus")
    del lost["Items"][0]["retailPrice"]
    assert "lost ['retailPrice']" in _fails({"eastus": [lost], "eastus2": [good]})

    foreign = body("eastus")
    foreign["Items"][0]["armRegionName"] = "westus"
    assert "filter not honoured" in _fails({"eastus": [foreign], "eastus2": [good]})

    eur = body("eastus")
    eur["BillingCurrency"] = "EUR"
    assert "currency" in _fails({"eastus": [eur], "eastus2": [good]})

    unknown = body("eastus")
    unknown["Items"] = [i for i in unknown["Items"] if i["armSkuName"] not in azure.GPU_SIZES]
    assert unknown["Items"], "fixture keeps rows for sizes outside the table"
    assert "no known GPU size" in _fails({"eastus": [unknown], "eastus2": [good]})

    endless = body("eastus")
    endless["NextPageLink"] = "https://prices.azure.com/api/retail/prices?$filter=armRegionName eq 'eastus'&$skip=0"
    assert "more than 3 pages" in _fails({"eastus": [endless], "eastus2": [good]})

    # One region failing does not hide the other.
    raws = asyncio.run(provider({"eastus": [renamed], "eastus2": [good]})[0].fetch())
    assert not raws[0].ok and raws[1].ok and normalized(raws[1:])


# --------------------------------------------------------------------------
# Registry, mapping, classes
# --------------------------------------------------------------------------


def test_registry():
    assert PROVIDERS["azure"] is AzureProvider
    assert any(isinstance(p, AzureProvider) for p in fetch.build_fetchers())
    assert mapping.check_mapping_coverage() == {}
    m = mapping.PROVIDER_MAPPINGS["azure"].by_field()
    assert m["gpu_count"].kind == mapping.LOOKUP and m["price_per_gpu_hour"].kind == mapping.DERIVED
    assert m["available"].kind == mapping.ABSENT and m["provider_tier"].kind == mapping.CONSTANT
    meta = provider_meta.meta("azure")
    assert meta.provider_class == "hyperscaler" and meta.source_type == "public_api"
    assert meta.source == "prices.azure.com"
    # Every table entry is a whole GPU count with a name canonical.py has seen or deliberately left.
    assert all(n >= 1 for n, _ in azure.GPU_SIZES.values())
    unmapped = {g for _, g in azure.GPU_SIZES.values() if canonical.canonical_gpu_name(g) is None}
    assert unmapped == {azure.V100_32}, unmapped
    for g in {g for _, g in azure.GPU_SIZES.values()} - unmapped:
        assert canonical.canonical_gpu_name(g) in canonical.all_canonical_names()


# --------------------------------------------------------------------------
# Pipeline: raw storage -> normalize + quality -> rollup -> index votes
# --------------------------------------------------------------------------


def test_pipeline():
    import fixtures
    import normalize
    import scratchdb
    from sqlalchemy import text as sql
    from analytics import indices, rollups

    db = "opengrid_test_azure"
    url = scratchdb.create(db)
    try:
        Session = fixtures.session(url)
        fetch.SessionLocal = Session
        normalize.SessionLocal = Session

        now = datetime.now(timezone.utc).replace(microsecond=0)
        for when in (now - timedelta(hours=3), now - timedelta(minutes=5)):  # two polls, three hours apart
            responses = [r.model_copy(update={"fetched_at": when})
                         for r in asyncio.run(provider(real_pages())[0].fetch())]
            assert all(r.ok for r in responses), [r.error for r in responses if not r.ok]
            fetch.save(responses)
            normalize.refresh(["azure"])

        with Session() as s:
            stored = s.execute(sql("SELECT count(*) FROM raw_snapshots WHERE provider = 'azure'")).scalar()
            bodies = s.execute(sql("SELECT count(*) FROM raw_payloads")).scalar()
            listed = s.execute(sql("SELECT count(*) FROM compute_listings WHERE provider = 'azure'")).scalar()
            held = s.execute(sql("SELECT listing_id, rule FROM quality_quarantine WHERE status = 'pending'")).all()
        assert stored == 4 and bodies == 2, (stored, bodies)
        assert listed == 22, listed
        assert not held, f"nothing held on clean data: {held}"

        rollups.rebuild()
        with Session() as s:
            last = s.execute(sql("SELECT max(hour) FROM market_hourly")).scalar()
            rows = [dict(r._mapping) for r in s.execute(sql(
                "SELECT segment, gpu, provider, region, country, hour, min_price FROM market_hourly "
                "WHERE hour = :h AND min_price IS NOT NULL"), {"h": last})]
        votes = indices.votes_by_index(rows)

        def vote(gpu, segment="on_demand"):
            return votes.get(indices.gpu_index_id(gpu, segment), {}).get("azure")

        assert abs(vote("AMD Instinct MI300X 192GB")[0] - 6.0) < 1e-9
        assert abs(vote("NVIDIA H100 94GB NVL")[0] - 6.98) < 1e-9
        assert abs(vote("NVIDIA T4 16GB")[0] - 1.088) < 1e-9, "cheapest per-GPU size: NC64as at $4.352/4"
        assert abs(vote("NVIDIA H100 94GB NVL", "spot")[0] - 1.289904) < 1e-9, "spot in the spot segment only"
        assert abs(vote("NVIDIA H100 80GB SXM5")[0] - 12.29) < 1e-9, "ND H100 v5: $98.32 / 8"
    finally:
        scratchdb.drop(db)


if __name__ == "__main__":
    for t in (test_fetch_and_normalize, test_market_rules, test_pagination, test_fail_closed, test_registry,
              test_pipeline):
        t()
        print(t.__name__, "ok")
    print("all passed")
