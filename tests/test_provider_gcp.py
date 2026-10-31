"""Google Cloud Compute Engine: the Cloud Billing Catalog API (it replaced the price-page scraper 2026-10-30).

Fixtures:
    gcp_catalog_skus.json            the API's Compute Engine SKUs as the provider stores them (filtered to GPU,
                                     the GPU families' core/RAM and Local SSD), fetched 2026-10-30
    gcp_page_prices_us_central1.json the Iowa prices printed on Google's accelerator-optimized pricing page
                                     (extracted from the saved page, fetched 2026-10-13). Every machine shape in
                                     providers/gcp.py is PROVEN by reproducing these from the API's components.

The last test pushes the catalogue through raw storage, normalize + quality screening, the hourly rollup and the
index vote in a scratch database.

Run:  .venv/bin/python tests/test_provider_gcp.py
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
import mapping  # noqa: E402
import provider_meta  # noqa: E402
from providers import PROVIDERS, gcp  # noqa: E402
from providers.gcp import ENDPOINT, GcpProvider  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_providers"
NOW = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)


def catalogue():
    return json.loads((FIX / "gcp_catalog_skus.json").read_text())


def page_prices():
    return json.loads((FIX / "gcp_page_prices_us_central1.json").read_text())


def snap(payload, at=NOW):
    return SimpleNamespace(payload=payload, request=None, fetched_at=at)


def listings(payload):
    return {l.listing_id: l for l in GcpProvider.normalize({ENDPOINT: [snap(payload)]})}


def raises(fn, *a):
    try:
        fn(*a)
    except ValueError:
        return True
    return False


def _serve(pages: list[dict], status=200):
    """Serve the catalogue in `pages`, chained by nextPageToken; remember the request headers."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path != ENDPOINT:
            return httpx.Response(404, text="not found")
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "nope"}})
        i = int(request.url.params.get("pageToken") or 0)
        body = {"skus": pages[i]["skus"], **({"nextPageToken": str(i + 1)} if i + 1 < len(pages) else {})}
        return httpx.Response(200, json=body)
    return GcpProvider("k-test", httpx.AsyncClient(base_url=gcp.BASE_URL, transport=httpx.MockTransport(handler))), seen


def test_shapes_reproduce_googles_page_prices():
    out = listings(catalogue())
    ref = page_prices()
    checked = 0
    for mtype in gcp.MACHINES:
        assert mtype in ref["machines"], f"{mtype}: no page price to prove its shape against"
        got = out[f"{mtype}:us-central1"].price_per_instance_hour
        assert abs(got - D(ref["machines"][mtype])) < D("0.000001"), (mtype, got, ref["machines"][mtype])
        checked += 1
    assert checked == len(gcp.MACHINES) == 24
    names = {"nvidia-tesla-t4": "NVIDIA T4", "nvidia-tesla-p4": "NVIDIA P4", "nvidia-tesla-v100": "NVIDIA V100",
             "nvidia-tesla-p100": "NVIDIA P100"}
    for accel, model in names.items():
        assert out[f"{accel}:us-central1"].price_per_gpu_hour == D(ref["attachable"][model]), accel
    # Shapes left out because they cannot be proven: no on-demand page price, or a fraction of a card.
    assert "a4-highgpu-8g" not in gcp.MACHINES and not any(k.startswith(("g4-standard-6", "g4-standard-12", "g4-standard-24"))
                                                            for k in gcp.MACHINES)


def test_normalize():
    out = listings(catalogue())
    assert len(out) == 28 and {l.region for l in out.values()} == {"us-central1"}, "Iowa only (see REGIONS)"
    a3 = out["a3-highgpu-8g:us-central1"]
    assert a3.canonical_gpu_name == "NVIDIA H100 80GB SXM5" and a3.gpu_count == 8 and a3.vcpu == 208 and a3.ram_gb == 1872
    assert abs(a3.price_per_gpu_hour - D("11.06125")) < D("0.000001") and a3.provider_tier == "hyperscaler"
    assert a3.market_type == "on_demand" and not a3.interruptible and a3.country == "US" and a3.available is None
    mega, high = out["a3-megagpu-8g:us-central1"], a3
    assert mega.price_per_instance_hour > high.price_per_instance_hour, "Mega (Plus SKUs) costs more than High"
    t4 = out["nvidia-tesla-t4:us-central1"]
    assert t4.gpu_count == 1 and t4.provider_tier == "gpu-attachment" and t4.price_per_gpu_hour == D("0.35")
    unmapped = {l.raw_gpu_name for l in out.values() if l.canonical_gpu_name is None}
    assert unmapped == {"nvidia-tesla-p4", "nvidia-tesla-p100"}, unmapped


def _sku(desc, region, units, nanos=0, usage="OnDemand", group="GPU", unit="h"):
    return {"description": desc, "serviceRegions": [region], "category": {"usageType": usage, "resourceGroup": group},
            "pricingInfo": [{"pricingExpression": {"usageUnit": unit, "tieredRates": [{"unitPrice": {"units": str(units), "nanos": nanos}}]}}]}


def test_conflicts_and_missing_components():
    base = catalogue()
    # A second, different on-demand H100 price for Iowa: the region is skipped for H100 machines, never guessed.
    clash = {"skus": base["skus"] + [_sku("Nvidia H100 80GB GPU running in Americas", "us-central1", 99)]}
    out = listings(clash)
    assert "a3-highgpu-8g:us-central1" not in out and "a2-highgpu-1g:us-central1" in out
    # Spot / commitment SKUs never leak into on-demand prices.
    spot = {"skus": base["skus"] + [_sku("Spot Preemptible Nvidia H100 80GB GPU running in Americas", "us-central1", 1)]}
    assert listings(spot)["a3-highgpu-8g:us-central1"].price_per_instance_hour == listings(base)["a3-highgpu-8g:us-central1"].price_per_instance_hour
    # Without the A3 RAM SKU in Iowa there is no A3 High listing there.
    no_ram = {"skus": [s for s in base["skus"] if not s["description"].startswith("A3 Instance Ram")]}
    assert "a3-highgpu-8g:us-central1" not in listings(no_ram)
    assert gcp.unit_price({"pricingInfo": []}) is None


def test_fail_closed():
    assert GcpProvider.normalize({}) == []
    assert raises(GcpProvider.normalize, {ENDPOINT: [snap({"oops": []})]})
    no_h100 = {"skus": [s for s in catalogue()["skus"] if "H100 80GB GPU" not in s["description"]]}
    assert raises(GcpProvider.normalize, {ENDPOINT: [snap(no_h100)]}), "a catalogue without H100 is a reshaped one"


def test_fetch():
    skus = catalogue()["skus"]
    noise = [_sku("N2 Instance Core running in Americas", "us-central1", 0, 31000000, group="CPU")]
    prov, seen = _serve([{"skus": skus[:1000] + noise}, {"skus": skus[1000:]}])
    raw = asyncio.run(prov.fetch())
    assert len(raw) == 1 and raw[0].ok and raw[0].endpoint == ENDPOINT
    assert len(raw[0].payload["skus"]) == len(skus), "both pages, and unrelated SKUs (N2) filtered out"
    assert [r.url.params.get("pageToken") for r in seen] == [None, "1"]
    assert GcpProvider("k-test")._client.headers["x-goog-api-key"] == "k-test", "the key travels in a header"
    assert all("k-test" not in str(r.url) for r in seen), "never in a URL"
    failing, _ = _serve([{"skus": skus}], status=403)
    bad = asyncio.run(failing.fetch())
    assert len(bad) == 1 and not bad[0].ok and "k-test" not in (bad[0].error or ""), "a failure is stored as a failure"


def test_registry():
    assert PROVIDERS["gcp"] is GcpProvider
    assert mapping.check_mapping_coverage() == {}
    assert "gcp" in mapping.PROVIDER_MAPPINGS
    m = provider_meta.meta("gcp")
    assert (m.provider_class, m.source_type, m.source) == ("hyperscaler", "authenticated_api", "cloudbilling.googleapis.com")
    import config
    import fetch
    real = config.settings.gcp_api_key
    try:
        config.settings.gcp_api_key = None
        assert not any(isinstance(p, GcpProvider) for p in fetch.build_fetchers()), "no key, no GCP"
        config.settings.gcp_api_key = "k-test"
        assert any(isinstance(p, GcpProvider) for p in fetch.build_fetchers())
    finally:
        config.settings.gcp_api_key = real
    unmapped = {a for _, _, a, *_ in gcp.MACHINES.values()} | set(gcp.ATTACHABLE.values())
    assert {a for a in unmapped if canonical.canonical_gpu_name(a) is None} == {"nvidia-tesla-p4", "nvidia-tesla-p100"}


def test_pipeline():
    import fetch
    import fixtures
    import normalize
    import scratchdb
    from sqlalchemy import text as sql
    from analytics import indices, rollups

    db = "opengrid_test_gcp"
    url = scratchdb.create(db)
    try:
        Session = fixtures.session(url)
        fetch.SessionLocal = Session
        normalize.SessionLocal = Session

        now = datetime.now(timezone.utc).replace(microsecond=0)
        for when in (now - timedelta(hours=3), now - timedelta(minutes=5)):  # two polls, three hours apart
            prov, _ = _serve([catalogue()])
            responses = [r.model_copy(update={"fetched_at": when}) for r in asyncio.run(prov.fetch())]
            assert all(r.ok for r in responses), [r.error for r in responses]
            fetch.save(responses)
            normalize.refresh(["gcp"])

        with Session() as s:
            stored = s.execute(sql("SELECT count(*) FROM raw_snapshots WHERE provider = 'gcp'")).scalar()
            bodies = s.execute(sql("SELECT count(*) FROM raw_payloads")).scalar()
            listed = s.execute(sql("SELECT count(*) FROM compute_listings WHERE provider = 'gcp'")).scalar()
            held = s.execute(sql("SELECT listing_id, rule FROM quality_quarantine WHERE status = 'pending'")).all()
        assert stored == 2 and bodies == 1, "the identical second body is stored once"
        assert listed == 28, listed
        assert not held, f"nothing held on clean data: {held}"

        rollups.rebuild()
        with Session() as s:
            last = s.execute(sql("SELECT max(hour) FROM market_hourly")).scalar()
            rows = [dict(r._mapping) for r in s.execute(sql(
                "SELECT segment, gpu, provider, region, country, hour, min_price FROM market_hourly "
                "WHERE hour = :h AND min_price IS NOT NULL"), {"h": last})]
        votes = indices.votes_by_index(rows)
        h100 = votes[indices.gpu_index_id("NVIDIA H100 80GB SXM5") + ".hyperscaler"]
        assert set(h100) == {"gcp"} and abs(h100["gcp"][0] - 11.06125) < 1e-6, "cheapest A3: High, not Mega"
        assert "gcp" not in votes.get(indices.gpu_index_id("NVIDIA H100 80GB SXM5") + ".neocloud", {})
        assert not any(k.endswith(".spot") and "gcp" in v for k, v in votes.items()), "no spot listings"
    finally:
        scratchdb.drop(db)


if __name__ == "__main__":
    for t in (test_shapes_reproduce_googles_page_prices, test_normalize, test_conflicts_and_missing_components,
              test_fail_closed, test_fetch, test_registry, test_pipeline):
        t()
        print(t.__name__, "ok")
    print("all passed")
