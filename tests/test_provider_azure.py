"""Microsoft Azure (Retail Prices API).

The parser runs on trimmed copies of the REAL responses (tests/fixtures_providers/
azure_eastus*.json, fetched 2026-10-13 with the provider's own anonymous filtered
GET), served through a mock transport. The reservation tests use trimmed real
priceType 'Reservation' responses (azure_reservation_eastus*.json, fetched
2026-10-31 with the provider's own filter; rows verbatim). The last test pushes
both regions through raw storage, normalize + quality screening, the hourly rollup
and the index vote in a scratch database, and proves reserved prices never reach them.

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


def res_body(region):
    return json.loads((FIX / f"azure_reservation_{region}.json").read_text(encoding="utf-8"))


def snap(payload, at=NOW):
    return SimpleNamespace(payload=payload, request=None, fetched_at=at)


def by_id(listings):
    return {l.listing_id: l for l in listings}


def _serve(pages, res_pages=None):
    """pages / res_pages: {region: [page body, ...]} for priceType Consumption / Reservation;
    page n>0 is reached via its NextPageLink."""
    seen = []
    res_pages = real_res_pages() if res_pages is None else res_pages

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        if request.url.path != azure.PATH:
            return httpx.Response(404, text="not found")
        q = parse_qs(urlsplit(str(request.url)).query)
        f = q.get("$filter", [""])[0]
        book = res_pages if "priceType eq 'Reservation'" in f else pages
        region = next((r for r in book if f"armRegionName eq '{r}'" in f), None)
        if region is None:
            return httpx.Response(400, json={"Error": "bad filter"})
        n = int(q.get("$skip", ["0"])[0]) // 1000
        return httpx.Response(200, json=book[region][n])

    return httpx.MockTransport(handler), seen


def provider(pages, res_pages=None):
    transport, seen = _serve(pages, res_pages)
    return AzureProvider(httpx.AsyncClient(base_url=azure.BASE_URL, transport=transport)), seen


def real_pages():
    return {"eastus": [body("eastus")], "eastus2": [body("eastus2")]}


def real_res_pages():
    return {"eastus": [res_body("eastus")], "eastus2": [res_body("eastus2")]}


CONSUMPTION_EPS = ["/api/retail/prices#eastus", "/api/retail/prices#eastus2"]
RESERVATION_EPS = ["/api/retail/prices/reservation#eastus", "/api/retail/prices/reservation#eastus2"]


def normalized(raws):
    return by_id(AzureProvider.normalize({r.endpoint: [snap(r.payload)] for r in raws}))


# --------------------------------------------------------------------------
# Fetch + normalize on today's responses
# --------------------------------------------------------------------------


def test_fetch_and_normalize():
    prov, seen = provider(real_pages())
    raws = asyncio.run(prov.fetch())
    assert [r.endpoint for r in raws] == CONSUMPTION_EPS + RESERVATION_EPS
    assert all(r.ok for r in raws), [r.error for r in raws]
    assert len(seen) == 4, "one consumption and one reservation request per region"
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
    assert all(r.ok for r in raws) and len(seen) == 5
    assert len(raws[0].payload["items"]) == 31, "both pages merged into one snapshot"
    assert normalized(raws) == normalized(asyncio.run(provider(real_pages())[0].fetch()))


def test_reservation_pagination():
    full = res_body("eastus")
    one, two = copy.deepcopy(full), copy.deepcopy(full)
    one["Items"], two["Items"] = full["Items"][:7], full["Items"][7:]
    one["NextPageLink"] = "https://prices.azure.com:443/api/retail/prices?$filter=" + \
        "armRegionName eq 'eastus' and priceType eq 'Reservation'&$skip=1000"
    prov, seen = provider(real_pages(), {"eastus": [one, two], "eastus2": [res_body("eastus2")]})
    raws = asyncio.run(prov.fetch())
    assert all(r.ok for r in raws) and len(seen) == 5, "the reservation NextPageLink is followed"
    res = raws[2]
    assert res.endpoint == RESERVATION_EPS[0] and len(res.payload["items"]) == len(full["Items"]) == 20
    assert "priceType eq 'Reservation'" in res.request["$filter"], "the first page's filter is the provenance"
    whole = asyncio.run(provider(real_pages())[0].fetch())
    assert azure.reserved_prices(raw_map(raws)) == azure.reserved_prices(raw_map(whole))


# --------------------------------------------------------------------------
# Reservations: raw capture, the per-GPU-hour conversion, never a listing
# --------------------------------------------------------------------------


def raw_map(raws):
    return {r.endpoint: [snap(r.payload)] for r in raws if r.ok}


def test_reservations_raw_capture():
    raws = asyncio.run(provider(real_pages())[0].fetch())
    res = {r.endpoint: r for r in raws[2:]}
    assert list(res) == RESERVATION_EPS and all(r.ok for r in res.values())
    assert all(len(ep) <= 256 for ep in res), "fits raw_snapshots.endpoint"
    p = res[RESERVATION_EPS[0]]
    f = p.request["$filter"]
    assert "priceType eq 'Reservation'" in f and "armRegionName eq 'eastus'" in f
    assert "startswith(armSkuName,'Standard_ND')" in f, "GPU families filtered server-side"
    assert p.payload["region"] == "eastus" and p.payload["price_type"] == "Reservation"
    assert len(p.payload["items"]) == 20 and set(p.payload["items"][0]) == set(azure._RES_FIELDS)
    # Every row is stored, mapped or not: unlisted (GB200) and fractional (NV6ads) sizes too.
    skus = {i["armSkuName"] for i in p.payload["items"]}
    assert {"Standard_ND128isr_NDR_GB200_v6", "Standard_NV6ads_A10_v5"} <= skus
    assert {i["reservationTerm"] for i in p.payload["items"]} == {"1 Year", "3 Years", "5 Years"}

    # Fail closed like the consumption call: a row without its term fails that snapshot only.
    lost = res_body("eastus")
    del lost["Items"][0]["reservationTerm"]
    raws = asyncio.run(provider(real_pages(), {"eastus": [lost], "eastus2": [res_body("eastus2")]})[0].fetch())
    assert [r.ok for r in raws] == [True, True, False, True]
    assert "lost ['reservationTerm']" in raws[2].error and raws[2].payload is None
    assert normalized(raws) == normalized(asyncio.run(provider(real_pages())[0].fetch())), \
        "a failed reservation call never touches consumption listings"
    mixed = res_body("eastus")
    mixed["Items"][0]["type"] = "Consumption"
    raws = asyncio.run(provider(real_pages(), {"eastus": [mixed], "eastus2": [res_body("eastus2")]})[0].fetch())
    assert not raws[2].ok and "filter not honoured" in raws[2].error


def test_reservation_price():
    row = next(i for i in res_body("eastus")["Items"]
               if i["armSkuName"] == "Standard_ND96isr_H100_v5" and i["reservationTerm"] == "1 Year")
    assert row["retailPrice"] == 551221.0 and row["unitOfMeasure"] == "1 Hour", "a whole-term price, labelled '1 Hour'"
    rp = azure.reservation_price(row)
    assert rp.sku == "Standard_ND96isr_H100_v5" and rp.region == "eastus" and rp.term_years == 1
    assert rp.gpu_count == 8 and rp.raw_gpu_name == azure.H100_80
    assert rp.canonical_gpu_name == "NVIDIA H100 80GB SXM5"
    assert rp.term_price == D("551221") and rp.term_hours == D(8760)
    assert rp.price_per_instance_hour == D("551221") / D(8760)
    assert rp.price_per_gpu_hour == D("551221") / D(8760) / 8
    assert round(rp.price_per_gpu_hour, 4) == D("7.8656") and rp.currency == "USD"

    got = {(r.region, r.sku, r.term_years): r for r in azure.reserved_prices(
        raw_map(asyncio.run(provider(real_pages())[0].fetch())))}
    h100 = {t: round(got[("eastus", "Standard_ND96isr_H100_v5", t)].price_per_gpu_hour, 4) for t in (1, 3, 5)}
    assert h100 == {1: D("7.8656"), 3: D("5.3953"), 5: D("4.9160")}, h100
    assert got[("eastus", "Standard_ND96isr_H100_v5", 3)].term_hours == D(26280)
    assert round(got[("eastus", "Standard_NC64as_T4_v3", 1)].price_per_gpu_hour, 4) == D("0.6398"), "22417/8760/4"
    assert got[("eastus2", "Standard_ND96isr_MI300X_v5", 1)].canonical_gpu_name == "AMD Instinct MI300X 192GB"
    # Sizes outside GPU_SIZES are stored raw but never converted (no guessed GPU count).
    assert not any(r.sku in ("Standard_ND128isr_NDR_GB200_v6", "Standard_NV6ads_A10_v5", "Standard_ND96is_H100_v5",
                             "Standard_NCC40ads_H100_v5") for r in got.values())
    assert len(got) == 22, "31 stored rows minus 9 for unmapped sizes"

    # Pure rules: only priced reservations of a known whole-GPU size and a stated term.
    assert azure.reservation_price({**row, "type": "Consumption"}) is None
    assert azure.reservation_price({**row, "armSkuName": "Standard_NV6ads_A10_v5"}) is None
    assert azure.reservation_price({**row, "reservationTerm": "1 Month"}) is None
    assert azure.reservation_price({**row, "reservationTerm": None}) is None
    assert azure.reservation_price({**row, "retailPrice": 0}) is None
    assert azure.reservation_price({**row, "retailPrice": "n/a"}) is None
    assert azure.reservation_price({**row, "reservationTerm": "5 Years"}).term_hours == D(43800)
    # A re-priced meter: the later effectiveStartDate wins.
    newer = {**row, "retailPrice": 500000.0, "effectiveStartDate": "2026-11-01T00:00:00Z"}
    payload = {"region": "eastus", "price_type": "Reservation", "items": [newer, row]}
    only = azure.reserved_prices({RESERVATION_EPS[0]: [snap(payload)]})
    assert len(only) == 1 and only[0].term_price == D("500000")
    # Reservation rows under the consumption label are not reserved prices (and not listings: see below).
    assert azure.reserved_prices({CONSUMPTION_EPS[0]: [snap(payload)]}) == []


def test_reservations_never_listings():
    raws = asyncio.run(provider(real_pages())[0].fetch())
    with_res, without = normalized(raws), normalized(raws[:2])
    assert with_res == without and len(with_res) == 22
    assert {l.market_type for l in with_res.values()} == {"on_demand", "spot"}
    assert with_res["Standard_ND96isr_H100_v5:eastus"].price_per_gpu_hour == D("12.29"), "still on-demand"
    # Only the reservation snapshots: no listings at all.
    assert normalized(raws[2:]) == {}
    # Even reservation rows stored under a consumption label, or tagged in its payload, never list.
    rows = [{k: i.get(k) for k in azure._RES_FIELDS} for i in res_body("eastus")["Items"]]
    assert AzureProvider.normalize({CONSUMPTION_EPS[0]: [snap({"region": "eastus", "items": rows})]}) == []
    good = raws[0].payload
    assert AzureProvider.normalize({CONSUMPTION_EPS[0]: [snap({**good, "price_type": "Reservation"})]}) == []


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
        assert stored == 8 and bodies == 4, "consumption + reservation, 2 regions, 2 polls; bodies deduplicated"
        assert listed == 22, "reservation snapshots add no listings"
        assert not held, f"nothing held on clean data: {held}"
        with Session() as s:
            kept = azure.reserved_prices(normalize._latest_raw(s, "azure"))
        assert len(kept) == 22 and any(r.sku == "Standard_ND96isr_H100_v5" and r.term_years == 3 for r in kept), \
            "reserved prices are readable back from raw storage"

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
        # Reserved prices reach no segment and no statistic.
        assert {r["segment"] for r in rows if r["provider"] == "azure"} <= {"on_demand", "spot"}
        reserved = {round(float(r.price_per_gpu_hour), 6) for r in kept}
        assert not any(round(float(r["min_price"]), 6) in reserved for r in rows if r["provider"] == "azure")
        assert not any(i.endswith(":reserved") or "reserv" in i for i in votes)
    finally:
        scratchdb.drop(db)


if __name__ == "__main__":
    for t in (test_fetch_and_normalize, test_market_rules, test_pagination, test_reservation_pagination,
              test_reservations_raw_capture, test_reservation_price, test_reservations_never_listings,
              test_fail_closed, test_registry, test_pipeline):
        t()
        print(t.__name__, "ok")
    print("all passed")
