"""Google Cloud Compute Engine: the Cloud Billing Catalog API (it replaced the price-page scraper 2026-10-30).

Fixtures:
    gcp_catalog_skus.json            the API's Compute Engine SKUs as the provider stores them (filtered to GPU,
                                     the GPU families' core/RAM and Local SSD), fetched 2026-10-30
    gcp_page_prices_us_central1.json the Iowa prices printed on Google's accelerator-optimized pricing page
                                     (extracted from the saved page, fetched 2026-10-13). Every machine shape in
                                     providers/gcp.py is PROVEN by reproducing these from the API's components.
    gcp_page_prices_regions.json     the same page's per-region on-demand tables (its region-picker data, fetched
                                     2026-11-01): G2 in 41 regions, G4 in 24, A2/A3 Iowa only
    gcp_compute_machine_types.json   Compute Engine aggregated machineTypes as the provider stores them (GPU types,
                                     shape fields only), trimmed to the MACHINES types + a4-highgpu-8g, 2026-11-01
    gcp_compute_accelerator_types.json  aggregated acceleratorTypes as stored (nvidia-* only), 2026-11-01
    gcp_calendar_advice_unavailable.json  the calendar-mode advice answer for this new project, 2026-11-01

The last test pushes the catalogue through raw storage, normalize + quality screening, the hourly rollup and the
index vote in a scratch database.

Run:  .venv/bin/python tests/test_provider_gcp.py
"""

import asyncio
import json
import os
import sys
import time
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
    assert len(out) == 28 and {l.region for l in out.values()} == {"us-central1"}, "no zone lists: Iowa only"
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


# --------------------------------------------------------------------------- regions beyond Iowa (service account)

def compute_lists():
    return (json.loads((FIX / "gcp_compute_machine_types.json").read_text()),
            json.loads((FIX / "gcp_compute_accelerator_types.json").read_text()))


def with_zones(payload=None, mt=None, at=None, zones_at=NOW):
    m, a = compute_lists()
    return {ENDPOINT: [snap(payload or catalogue())], gcp.MACHINE_TYPES: [snap(mt or m, zones_at)],
            gcp.ACCELERATOR_TYPES: [snap(at or a, zones_at)]}


def test_regions_from_compute_engine():
    iowa = listings(catalogue())
    out = {l.listing_id: l for l in GcpProvider.normalize(with_zones())}
    regions = {l.region for l in out.values()}
    assert len(out) == 369 and len(regions) == 34, (len(out), len(regions))
    # Iowa keeps its ids and prices; only P100 goes: Compute Engine offers it in no zone at all any more.
    assert set(iowa) - set(out) == {"nvidia-tesla-p100:us-central1"}
    assert all(out[k].price_per_instance_hour == iowa[k].price_per_instance_hour for k in iowa if k in out)
    assert not any(l.sku == "nvidia-tesla-p100" for l in out.values())
    count = lambda sku: sum(1 for l in out.values() if l.sku == sku)
    assert (count("a3-highgpu-8g"), count("a3-megagpu-8g"), count("a3-ultragpu-8g"), count("g2-standard-4"),
            count("g4-standard-48"), count("nvidia-tesla-t4")) == (19, 11, 5, 18, 23, 23)
    nl = out["a3-highgpu-8g:europe-west4"]
    assert nl.country == "NL" and nl.gpu_count == 8 and nl.vcpu == 208 and abs(nl.price_per_instance_hour - D("112.54047917")) < D("0.000001")
    assert out["g2-standard-4:europe-west4"].price_per_instance_hour == D("0.742916046")
    assert all(l.country for l in out.values()), "every listed region has a country"
    # Priced is not sold: G2 is priced in us-west3 but no zone there offers it.
    assert "g2-standard-4:us-west3" not in out and gcp.by_region(catalogue()["skus"], gcp.FAMILY["G2"][0]).get("us-west3")
    # Offered but not priced on demand there (H200 in the Netherlands has only DWS / calendar SKUs): no listing.
    assert "europe-west4" in gcp.offered(with_zones()).machines["a3-ultragpu-8g"]
    assert "a3-ultragpu-8g:europe-west4" not in out
    # A type that is not a proven shape (a4-highgpu-8g) never becomes a listing.
    assert not any(l.sku.startswith("a4-") for l in out.values())


def test_zone_shape_must_match():
    m, a = compute_lists()
    # Every europe-west4 zone reports A3 High with a different memory size: the region is not counted for it.
    bent = {"machineTypes": [dict(x, memoryMb=1) if (x["name"], x["zone"][:12]) == ("a3-highgpu-8g", "europe-west4")
                             else x for x in m["machineTypes"]]}
    out = {l.listing_id for l in GcpProvider.normalize(with_zones(mt=bent))}
    assert "a3-highgpu-8g:europe-west4" not in out and "a3-highgpu-8g:us-central1" in out
    assert gcp._shape_ok("a3-highgpu-8g", next(x for x in m["machineTypes"] if x["name"] == "a3-highgpu-8g"))


def test_zone_lists_stale_or_reshaped_fall_back_to_iowa():
    iowa = set(listings(catalogue()))
    old = {l.listing_id for l in GcpProvider.normalize(with_zones(zones_at=NOW - timedelta(days=3)))}
    assert old == iowa, "zone lists older than COMPUTE_FRESH are not used"
    assert {l.listing_id for l in GcpProvider.normalize(with_zones(mt={"oops": 1}))} == iowa
    assert {l.listing_id for l in GcpProvider.normalize(with_zones(mt={"machineTypes": []}))} == iowa


def test_regions_proven_against_googles_page():
    """Every per-region on-demand price embedded in Google's page is reproduced exactly from the catalogue, and
    every listing in a region the page covers equals the page."""
    ref = json.loads((FIX / "gcp_page_prices_regions.json").read_text())["machines"]
    every = sorted({r for v in ref.values() for r in v})
    real = gcp.FALLBACK_REGIONS
    try:
        gcp.FALLBACK_REGIONS = tuple(every)  # assemble everywhere the page prices, offered or not
        anywhere = listings(catalogue())
    finally:
        gcp.FALLBACK_REGIONS = real
    checked = 0
    for mtype, regions in ref.items():
        for region, price in regions.items():
            got = anywhere[f"{mtype}:{region}"].price_per_instance_hour
            assert abs(got - D(price)) < D("0.000001"), (mtype, region, got, price)
            checked += 1
    assert checked == 436 and len(ref["g2-standard-4"]) == 41 and len(ref["g4-standard-48"]) == 24
    listed = {l.listing_id: l for l in GcpProvider.normalize(with_zones())}
    on_page = [(k, l) for k, l in listed.items() if l.region in ref.get(l.sku, {})]
    assert all(abs(l.price_per_instance_hour - D(ref[l.sku][l.region])) < D("0.000001") for _, l in on_page)
    outside_iowa = {(l.sku[:2], l.region) for _, l in on_page if l.region != "us-central1"}
    assert len(on_page) == 248 and {s for s, _ in outside_iowa} == {"g2", "g4"}, len(on_page)


def test_commitments_follow_offered_regions():
    iowa = gcp.commitment_prices({ENDPOINT: [snap(catalogue())]})
    out = gcp.commitment_prices(with_zones())
    where = gcp.offered(with_zones())
    assert {c.region for c in iowa} == {"us-central1"} and len(iowa) == 22
    assert len(out) == 258 and len({c.region for c in out}) == 34, (len(out), len({c.region for c in out}))
    assert all(c.region in where.gpu_regions(c.gpu_key) for c in out), "only where the GPU is offered"
    nl = [c for c in out if c.gpu_key == "h100" and c.region == "europe-west4"]
    assert {c.term_years for c in nl} == {1, 3} and all(c.on_demand_gpu_hour for c in nl)


def test_calendar_advice_raw_only():
    rec = json.loads((FIX / "gcp_calendar_advice_unavailable.json").read_text())
    raw = SimpleNamespace(status_code=rec["status_code"], payload=rec["payload"], request=rec["request"], fetched_at=NOW)
    assert gcp.advice_unavailable(raw) and gcp.calendar_advice([raw]) == []
    assert not gcp.advice_unavailable(SimpleNamespace(status_code=403, payload=rec["payload"]))
    body = gcp.advice_request("a3-highgpu-8g", NOW)
    spec = body["futureResourcesSpecs"]["spec"]
    assert spec["targetResources"] == {"specificSkuResources": {"machineType": "a3-highgpu-8g", "instanceCount": "1"}}
    assert spec["timeRangeSpec"] == {"startTimeNotEarlierThan": "2026-10-31T12:00:00Z",
                                     "startTimeNotLaterThan": "2026-11-13T12:00:00Z",
                                     "minDuration": "86400s", "maxDuration": "604800s"}
    # The documented answer shape is read back (no real answer seen yet, so this is the schema, not a fixture).
    ok = SimpleNamespace(status_code=200, request={"region": "us-central1", "machine_type": "a3-highgpu-8g"},
                         fetched_at=NOW, payload={"recommendations": [{"recommendationsPerSpec": {"spec": {
                             "recommendationType": "FUTURE_RESERVATION", "location": "zones/us-central1-a",
                             "startTime": "2026-11-03T00:00:00Z", "endTime": "2026-11-05T00:00:00Z",
                             "otherLocations": {"zones/us-central1-b": {"status": "NO_CAPACITY"}}}}}]})
    got = gcp.calendar_advice([ok])
    assert got[0]["recommended_zone"] == "us-central1-a" and got[0]["other_zones"] == {"us-central1-b": "NO_CAPACITY"}
    assert GcpProvider.normalize_capacity is gcp.Provider.normalize_capacity, "no capacity rows from advice yet"


# Throwaway key for the fetch tests (made per run; nothing secret in the repository).
def _fake_sa():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    from providers import gcp_auth
    pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    return gcp_auth.load(json.dumps({"type": "service_account", "project_id": "proj-x", "private_key": pem,
                                     "client_email": "ro@proj-x.iam.gserviceaccount.com"}))


def _serve_all(advice_status=400, compute_status=200, token_status=200):
    """One transport for every host: the catalogue, Google's token endpoint, Compute Engine. Two pages per
    aggregated list; accelerator zones come back as URLs naming the project, as the real API sends them."""
    m, a = compute_lists()
    unavailable = json.loads((FIX / "gcp_calendar_advice_unavailable.json").read_text())["payload"]
    seen = []

    def pages(rows, key, url_zone=False):
        half = len(rows) // 2
        out = []
        for chunk in (rows[:half], rows[half:]):
            items = {}
            for x in chunk:
                z = x["zone"]
                items.setdefault(f"zones/{z}", {key: []})[key].append(
                    dict(x, zone=f"https://www.googleapis.com/compute/v1/projects/proj-x/zones/{z}") if url_zone else x)
            out.append({"items": items})
        return out
    lists = {"machineTypes": pages(m["machineTypes"], "machineTypes"),
             "acceleratorTypes": pages(a["acceleratorTypes"], "acceleratorTypes", url_zone=True)}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        host, path = request.url.host, request.url.path
        if host == "oauth2.googleapis.com":
            return httpx.Response(token_status, json={"access_token": "ya29.fake", "expires_in": 3599}
                                  if token_status == 200 else {"error": "invalid_grant"})
        if host == "cloudbilling.googleapis.com":
            return httpx.Response(200, json={"skus": catalogue()["skus"]})
        if host == "compute.googleapis.com":
            if request.headers.get("authorization") != "Bearer ya29.fake":
                return httpx.Response(401, json={"error": {"code": 401}})
            if path.endswith("/advice/calendarMode"):
                return httpx.Response(advice_status, json=unavailable if advice_status == 400 else {"recommendations": []})
            key = path.rsplit("/", 1)[-1]
            if compute_status != 200 or key not in lists:
                return httpx.Response(compute_status if compute_status != 200 else 404, json={"error": {"code": 500}})
            i = int(request.url.params.get("pageToken") or 0)
            body = {**lists[key][i], **({"nextPageToken": str(i + 1)} if i + 1 < len(lists[key]) else {})}
            return httpx.Response(200, json=body)
        return httpx.Response(404)
    t = httpx.MockTransport(handler)
    prov = GcpProvider("k-test", httpx.AsyncClient(base_url=gcp.BASE_URL, headers={"X-Goog-Api-Key": "k-test"}, transport=t),
                       service_account=_fake_sa(), compute_client=httpx.AsyncClient(base_url=gcp.COMPUTE_URL, transport=t))
    return prov, seen


def test_fetch_with_service_account():
    prov, seen = _serve_all()
    raw = asyncio.run(prov.fetch())
    eps = [r.endpoint for r in raw]
    assert eps == [ENDPOINT, gcp.MACHINE_TYPES, gcp.ACCELERATOR_TYPES, gcp.CALENDAR_ADVICE], eps
    cat, mt, at, adv = raw
    assert cat.ok and mt.ok and at.ok and not adv.ok and adv.status_code == 400
    m, a = compute_lists()
    assert mt.payload == {"machineTypes": sorted(m["machineTypes"], key=lambda x: (x["name"], x["zone"]))}
    assert at.payload["acceleratorTypes"][0]["zone"].count("/") == 0, "zone URLs (naming the project) cut to the zone"
    assert adv.request["region"] == "us-central1" and adv.request["machine_type"] == "a3-highgpu-8g"
    # One advice call: the project cannot use the service, so the rest are not asked; retried after a day.
    assert sum(1 for r in seen if r.url.path.endswith("calendarMode")) == 1
    assert prov._next_advice - time.monotonic() > gcp.ADVICE_EVERY
    # Secrets and the project id stay out of what is stored, and each credential only goes to its own host.
    stored = json.dumps([r.model_dump(mode="json") for r in raw])
    assert "proj-x" not in stored and "ya29" not in stored and "PRIVATE" not in stored and "k-test" not in stored
    for r in seen:
        if r.url.host == "compute.googleapis.com":
            assert "x-goog-api-key" not in r.headers and r.headers["authorization"] == "Bearer ya29.fake"
        elif r.url.host == "cloudbilling.googleapis.com":
            assert "authorization" not in r.headers
    # The stored zone lists make the normalizer open the regions.
    by_endpoint = {r.endpoint: [snap(r.payload, r.fetched_at)] for r in raw if r.ok}
    assert len(GcpProvider.normalize(by_endpoint)) == 369
    # Within 6 hours, the next poll is the catalogue only (one token, cached; zone lists and advice not due).
    again = asyncio.run(prov.fetch())
    assert [r.endpoint for r in again] == [ENDPOINT]
    assert sum(1 for r in seen if r.url.host == "oauth2.googleapis.com") == 2, "one token per scope"


def test_fetch_failures_fail_open():
    prov, _ = _serve_all(token_status=401)
    raw = asyncio.run(prov.fetch())
    assert raw[0].ok and raw[0].endpoint == ENDPOINT, "prices are fetched whatever the service account does"
    tok = raw[1]
    assert not tok.ok and tok.endpoint == "https://oauth2.googleapis.com/token" and "HTTP 401" in tok.error
    prov, _ = _serve_all(compute_status=500)
    raw = asyncio.run(prov.fetch())
    assert [r.ok for r in raw[:3]] == [True, False, False] and prov._next_compute == 0.0, "retried next poll"
    # No service account, or no project: exactly the old behaviour.
    assert GcpProvider("k")._sa is None
    import fetch
    import config
    real = (config.settings.gcp_api_key, config.settings.gcp_service_account_json, config.settings.gcp_project_id)
    try:
        from pydantic import SecretStr
        config.settings.gcp_api_key = "k-test"
        config.settings.gcp_service_account_json = SecretStr("{not json")
        config.settings.gcp_project_id = "proj-x"
        g = next(p for p in fetch.build_fetchers() if isinstance(p, GcpProvider))
        assert g._sa is None, "an unreadable key is skipped; prices still polled"
    finally:
        config.settings.gcp_api_key, config.settings.gcp_service_account_json, config.settings.gcp_project_id = real


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

        # A third poll with the service account: the zone lists open 34 regions; nothing is held by screening.
        prov, _ = _serve_all()
        responses = asyncio.run(prov.fetch())
        fetch.save(responses)
        normalize.refresh(["gcp"])
        with Session() as s:
            regions = s.execute(sql("SELECT count(DISTINCT region) FROM compute_listings WHERE provider = 'gcp' "
                                    "AND observed_at >= :t"), {"t": responses[0].fetched_at}).scalar()
            fresh = s.execute(sql("SELECT count(*) FROM compute_listings WHERE provider = 'gcp' AND observed_at >= :t"),
                              {"t": responses[0].fetched_at}).scalar()
            held = s.execute(sql("SELECT listing_id, rule FROM quality_quarantine WHERE status = 'pending'")).all()
            failed = s.execute(sql("SELECT endpoint FROM raw_snapshots WHERE provider = 'gcp' AND NOT ok")).scalars().all()
        assert (fresh, regions) == (369, 34), (fresh, regions)
        assert not held, f"nothing held: {held[:5]}"
        assert failed == [gcp.CALENDAR_ADVICE], "the advice answer is stored raw, as a failed call"
    finally:
        scratchdb.drop(db)


if __name__ == "__main__":
    for t in (test_shapes_reproduce_googles_page_prices, test_normalize, test_conflicts_and_missing_components,
              test_fail_closed, test_fetch, test_registry, test_regions_from_compute_engine, test_zone_shape_must_match,
              test_zone_lists_stale_or_reshaped_fall_back_to_iowa, test_regions_proven_against_googles_page,
              test_commitments_follow_offered_regions, test_calendar_advice_raw_only, test_fetch_with_service_account,
              test_fetch_failures_fail_open, test_pipeline):
        t()
        print(t.__name__, "ok")
    print("all passed")
