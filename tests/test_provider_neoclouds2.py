"""Civo, Atlantic.Net and DeepInfra GPU Instances: public price pages, and DeepInfra's container API.

Parsers run on trimmed copies of the REAL pages (tests/fixtures_providers/,
fetched 2026-10-21 with unauthenticated GETs) and on DeepInfra's REAL
GET /v1/containers/gpu_availability body (deepinfra_gpu_availability.json,
fetched 2026-11-01 with an account key; read-only, nothing was launched),
which supplies stock only: prices always come from the page. The last test pushes all three
through raw storage, normalize + quality screening, the hourly rollup and the
index vote in a scratch database.

Run:  .venv/bin/python tests/test_provider_neoclouds2.py
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
from providers import atlanticnet, civo, deepinfra  # noqa: E402
from providers.atlanticnet import AtlanticNetProvider  # noqa: E402
from providers.civo import CivoProvider  # noqa: E402
from providers.deepinfra import DeepInfraProvider  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_providers"
NOW = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
NEW = ["civo", "atlanticnet", "deepinfra"]
PAGES = {
    "civo": (CivoProvider, "https://www.civo.com", civo.ENDPOINT, "civo_pricing.html"),
    "atlanticnet": (AtlanticNetProvider, "https://www.atlantic.net", atlanticnet.ENDPOINT, "atlantic_gpu.html"),
    "deepinfra": (DeepInfraProvider, "https://deepinfra.com", deepinfra.ENDPOINT, "deepinfra_gpu_instances.html"),
}


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


def _page(status=200, body=""):
    return httpx.MockTransport(lambda r: httpx.Response(status, text=body, headers={"content-type": "text/html"}))


def prov(name, transport=None):
    cls, base, _, fixture = PAGES[name]
    return cls(httpx.AsyncClient(base_url=base, transport=transport or _page(body=text(fixture))))


def _fetch(p):
    return asyncio.run(p.fetch())


def normalized(name, payload=None):
    cls, _, ep, fixture = PAGES[name]
    if payload is None:
        payload = sys.modules[cls.__module__].extract(text(fixture))
    return by_id(cls.normalize({ep: [snap(payload)]}))


# --------------------------------------------------------------------------
# Civo
# --------------------------------------------------------------------------


def test_civo():
    raw = _fetch(prov("civo"))[0]
    assert raw.ok and raw.endpoint == civo.ENDPOINT, raw.error
    rows = raw.payload["rows"]
    assert len(rows) == 18 and {r["table"] for r in rows} == {
        "NVIDIA L40S 48GB", "NVIDIA A100 40GB", "NVIDIA A100 80GB", "NVIDIA H100 SXM", "NVIDIA H200 SXM",
        "NVIDIA B200", "NVIDIA B300"}, "CPU tables are outside the GPU section and never read"
    out = normalized("civo", raw.payload)
    assert len(out) == 17

    l40s = out["nvidia-l40s-48gb:4x"]
    assert l40s.raw_gpu_name == "NVIDIA L40S 48GB" and l40s.canonical_gpu_name == "NVIDIA L40S 48GB"
    assert l40s.gpu_count == 4 and l40s.price_per_instance_hour == D("5.16") and l40s.price_per_gpu_hour == D("1.29")
    assert l40s.market_type == "on_demand" and not l40s.interruptible and l40s.available is None
    assert l40s.vcpu == 48 and l40s.ram_gb == 384 and l40s.storage_gb == 800 and l40s.region is None
    h100 = out["nvidia-h100-sxm:8x"]
    assert h100.raw_gpu_name == "NVIDIA H100 SXM 80GB" and h100.canonical_gpu_name == "NVIDIA H100 80GB SXM5"
    assert h100.price_per_instance_hour == D("23.92") and h100.price_per_gpu_hour == D("2.99"), "instance / 8"
    h200 = out["nvidia-h200-sxm:1x"]
    assert h200.raw_gpu_name == "NVIDIA H200 SXM 141GB" and h200.canonical_gpu_name == "NVIDIA H200 141GB SXM5"
    assert h200.price_per_gpu_hour == D("3.49"), "on-demand, not the commitment price beside it"
    b300 = out["nvidia-b300:8x"]
    assert b300.raw_gpu_name == "NVIDIA B300" and b300.canonical_gpu_name == "NVIDIA B300 288GB SXM", "SXM-only part"
    assert b300.price_per_gpu_hour == D("59.92") / 8 == D("7.49") and b300.ram_gb == 1.9 * 1024
    for a100 in ("nvidia-a100-40gb:2x", "nvidia-a100-80gb:8x"):
        assert out[a100].canonical_gpu_name is None, "SXM or PCIe not stated"
    assert out["nvidia-a100-80gb:8x"].price_per_gpu_hour == D("1.79")
    assert "nvidia-b200:8x" not in out, "on-demand N/A (commitment-only): no listing"

    # A row filed under another model's table is skipped, never relabelled.
    moved = {"rows": [dict(rows[0], size="1 x NVIDIA A40 - 48GB")] + rows[1:]}
    assert "nvidia-l40s-48gb:1x" not in normalized("civo", moved)
    # Reshaped page: no GPU section, or a row without its on-demand cell.
    assert raises(civo.extract, text("civo_pricing.html").replace('id="nvidia-gpus"', 'id="gpus"'))
    assert raises(civo.extract, text("civo_pricing.html").replace('data-title="On-demand price"', 'data-title="x"'))
    assert raises(normalized, "civo", {"rows": [dict(rows[0], on_demand_hourly="£1.29")]}), "not '$x.xx'"


# --------------------------------------------------------------------------
# Atlantic.Net
# --------------------------------------------------------------------------


def test_atlanticnet():
    raw = _fetch(prov("atlanticnet"))[0]
    assert raw.ok, raw.error
    assert [p["plan"] for p in raw.payload["plans"]] == ["AL40S.192GB", "AH100NVL.240GB"]
    out = normalized("atlanticnet", raw.payload)
    assert len(out) == 2
    l40s = out["AL40S.192GB"]
    assert l40s.raw_gpu_name == "L40S" and l40s.canonical_gpu_name == "NVIDIA L40S 48GB"
    assert l40s.gpu_count == 1 and l40s.price_per_gpu_hour == l40s.price_per_instance_hour == D("1.668")
    assert l40s.vcpu == 32 and l40s.ram_gb == 192 and l40s.storage_gb == 1400
    assert l40s.market_type == "on_demand" and l40s.available is None
    nvl = out["AH100NVL.240GB"]
    assert nvl.raw_gpu_name == "H100NVL" and nvl.canonical_gpu_name == "NVIDIA H100 94GB NVL"
    assert nvl.price_per_gpu_hour == D("3.941"), "On-Demand, not the 1-year ($3.7898) or 3-year ($3.583) term"

    # Per-instance maths: a 2-GPU plan halves its price.
    two = {"plans": [dict(raw.payload["plans"][0], gpu="2 x L40S", on_demand_hourly="$3.336/hr")]}
    p = normalized("atlanticnet", two)["AL40S.192GB"]
    assert p.gpu_count == 2 and p.price_per_gpu_hour == D("1.668") and p.price_per_instance_hour == D("3.336")
    # An unreadable GPU cell is skipped, never guessed.
    assert not normalized("atlanticnet", {"plans": [dict(raw.payload["plans"][0], gpu="L40S")]})
    # Reshaped page: no On-Demand toggle, a changed header, a row without its od-price.
    page = text("atlantic_gpu.html")
    assert raises(atlanticnet.extract, page.replace('data-term="onDemand"', 'data-term="monthly"'))
    assert raises(atlanticnet.extract, page.replace(">vCPU</button>", ">Cores</button>"))
    assert raises(atlanticnet.extract, page.replace("od-price", "xx-price"))


# --------------------------------------------------------------------------
# DeepInfra
# --------------------------------------------------------------------------


def test_deepinfra():
    raw = _fetch(prov("deepinfra"))[0]
    assert raw.ok, raw.error
    assert [r["config"] for r in raw.payload["rows"]] == [f"{n}x NVIDIA B200" for n in (1, 2, 4, 8)]
    out = normalized("deepinfra", raw.payload)
    assert len(out) == 4, "the hero cards repeat prices but are not read"
    one, eight = out["nvidia-b200:1x"], out["nvidia-b200:8x"]
    assert one.raw_gpu_name == eight.raw_gpu_name == "NVIDIA B200 180GB"
    assert one.canonical_gpu_name == "NVIDIA B200 180GB SXM"
    assert one.price_per_gpu_hour == one.price_per_instance_hour == D("4.89")
    assert eight.gpu_count == 8 and eight.price_per_instance_hour == D("39.12") and eight.price_per_gpu_hour == D("4.89")
    assert eight.vcpu == 128 and eight.ram_gb == 1500 and eight.storage_gb == 15000
    assert one.provider_tier == "container" and one.market_type == "on_demand" and one.available is None

    # GPU RAM that does not divide evenly by the count is skipped, never guessed.
    odd = {"rows": [dict(raw.payload["rows"][1], gpu_ram="361 GB")]}
    assert not normalized("deepinfra", odd)
    # Reshaped page: no on-demand table, or a price that is no longer '$x/hour'.
    page = text("deepinfra_gpu_instances.html")
    assert raises(deepinfra.extract, page.replace("On-Demand Cloud Pricing", "Cloud Pricing"))
    assert raises(deepinfra.extract, page.replace("$4.89/hour", "$4.89/minute"))


def _api_body():
    return json.loads(text("deepinfra_gpu_availability.json"))


def _keyed(api_handler, page_transport=None, key="di-test-key"):
    """A keyed DeepInfra: the page through one mock, the container API through another."""
    seen = []

    def api(request):
        seen.append((request.method, request.url.path, request.headers.get("authorization")))
        return api_handler(request)
    p = DeepInfraProvider(
        httpx.AsyncClient(base_url="https://deepinfra.com",
                          transport=page_transport or _page(body=text("deepinfra_gpu_instances.html"))),
        api_key=key,
        api_client=httpx.AsyncClient(base_url=deepinfra.API_BASE_URL, transport=httpx.MockTransport(api),
                                     headers={"Authorization": f"Bearer {key}"}))
    return p, seen


def _by(raws):
    return {r.endpoint: [snap(r.payload, r.fetched_at)] for r in raws if r.ok}


def test_deepinfra_api_stock_page_prices():
    p, seen = _keyed(lambda r: httpx.Response(200, json=_api_body()))
    raws = _fetch(p)
    assert seen == [("GET", deepinfra.API_ENDPOINT, "Bearer di-test-key")], "one read-only GET; never POST /v1/containers"
    assert [r.endpoint for r in raws] == [deepinfra.ENDPOINT, deepinfra.API_ENDPOINT] and all(r.ok for r in raws)
    assert raws[1].payload == _api_body(), "raw first: the API body is stored as received (usd_per_hour included)"
    assert all("di-test-key" not in json.dumps(r.model_dump(mode="json")) for r in raws), "the key is never stored"

    # Prices are the page's, key or no key: the API's usd_per_hour never reaches a listing.
    by = _by(raws)
    keyed, page = by_id(DeepInfraProvider.normalize(by)), normalized("deepinfra")
    assert keyed == by_id(DeepInfraProvider.normalize({deepinfra.ENDPOINT: by[deepinfra.ENDPOINT]}))
    assert set(keyed) == set(page) == {f"nvidia-b200:{n}x" for n in (1, 2, 4, 8)}
    for lid, l in keyed.items():
        assert (l.price_per_gpu_hour, l.price_per_instance_hour, l.vcpu, l.ram_gb, l.storage_gb) == \
               (page[lid].price_per_gpu_hour, page[lid].price_per_instance_hour, page[lid].vcpu,
                page[lid].ram_gb, page[lid].storage_gb), lid
        assert l.price_per_gpu_hour == D("4.89") and l.available is None, lid
    assert keyed["nvidia-b200:8x"].vcpu == 128 and keyed["nvidia-b200:8x"].storage_gb == 15000, "page specs kept"
    published = {str(v) for l in keyed.values() for v in (l.price_per_gpu_hour, l.price_per_instance_hour)}
    assert not published & {"3.69", "7.38", "14.76", "29.52"}, published
    # Only the API stored (no page yet): no listings at all, rather than API prices.
    assert DeepInfraProvider.normalize({deepinfra.API_ENDPOINT: by[deepinfra.API_ENDPOINT]}) == []

    # API stock joins to page skus.
    rows = DeepInfraProvider.normalize_capacity(by)
    page_skus = {l.sku for l in keyed.values()}
    assert len(rows) == 4 and {r.sku for r in rows} == page_skus == {f"{n}x NVIDIA B200" for n in (1, 2, 4, 8)}
    for r in rows:
        l = next(l for l in keyed.values() if l.sku == r.sku)
        assert (r.gpu_count, r.raw_gpu_name, r.canonical_gpu_name) == (l.gpu_count, l.raw_gpu_name, l.canonical_gpu_name)
    assert all(r.in_stock is False and r.region is None and r.tier is None and r.available_count is None
               and r.source == deepinfra.API_ENDPOINT for r in rows), "all four sold out when the fixture was saved"
    assert rows[0].raw_value == "available=False; gpu_config=1xB200-180GB" and "3.69" not in rows[0].raw_value
    # No key: page only, no capacity.
    assert prov("deepinfra")._api is None and [r.endpoint for r in _fetch(prov("deepinfra"))] == [deepinfra.ENDPOINT]
    assert DeepInfraProvider.normalize_capacity({deepinfra.ENDPOINT: by[deepinfra.ENDPOINT]}) == []


def test_deepinfra_api_failures_never_touch_prices():
    """A stock body with no readable rows is a failed fetch, never "nothing in stock"; prices carry on."""
    for status, body in ((200, {"gpus": []}), (200, {"detail": "maintenance"}), (200, []),
                         (200, {"gpus": [{"usd_per_hour": 3.69, "available": True}]}),
                         (200, {"gpus": [{"gpu_config": "nonsense", "available": True}]}),
                         (401, {"detail": "User is not authorized to access this resource"}),
                         (503, {"detail": "unavailable"})):
        p, _ = _keyed(lambda r, s=status, b=body: httpx.Response(s, json=b))
        page, api = _fetch(p)
        assert page.ok and len(DeepInfraProvider.normalize(_by([page, api]))) == 4, (status, body)
        assert not api.ok and api.error, (status, body)
        if status == 200:
            assert api.payload == body, "kept for diagnosis"
            try:
                DeepInfraProvider.normalize_capacity({deepinfra.API_ENDPOINT: [snap(body)]})
                raise AssertionError(f"normalize_capacity must refuse {body!r}")
            except ValueError:
                pass
            assert len(DeepInfraProvider.normalize({deepinfra.ENDPOINT: _by([page])[deepinfra.ENDPOINT],
                                                    deepinfra.API_ENDPOINT: [snap(body)]})) == 4
    p, _ = _keyed(lambda r: (_ for _ in ()).throw(httpx.ConnectError("down")))
    page, api = _fetch(p)
    assert page.ok and not api.ok and api.payload is None
    # A broken page fails prices whatever the API says; the API never stands in.
    p, _ = _keyed(lambda r: httpx.Response(200, json=_api_body()), page_transport=_page(503, "maintenance"))
    page, api = _fetch(p)
    assert not page.ok and api.ok and DeepInfraProvider.normalize(_by([page, api])) == []
    assert DeepInfraProvider.normalize({}) == [] and DeepInfraProvider.normalize_capacity({}) == []


def test_deepinfra_capacity_flags():
    """Boolean shape; a missing or odd flag is unknown, never guessed; unknown codes keep the API's text."""
    body = _api_body()
    body["gpus"][0]["available"] = True
    del body["gpus"][1]["available"]
    body["gpus"][2]["available"] = "yes"
    body["gpus"].append({"gpu_config": "1xA100-80GB", "usd_per_hour": 0.89, "available": True})
    body["gpus"].append({"gpu_config": "8 x B200", "usd_per_hour": 29.52, "available": True})
    rows = {r.sku: r for r in DeepInfraProvider.normalize_capacity({deepinfra.API_ENDPOINT: [snap(body)]})}
    assert [rows[f"{n}x NVIDIA B200"].in_stock for n in (1, 2, 4, 8)] == [True, None, None, False]
    assert rows["1x A100"].raw_gpu_name == "A100 80GB" and len(rows) == 5, "unreadable config skipped"


# --------------------------------------------------------------------------
# Fetch failures, registry, classes
# --------------------------------------------------------------------------


def test_fetch_failures():
    """An error status or an unrelated page is a failed response, never an empty price list."""
    for name in NEW:
        for transport in (_page(503, "maintenance"), _page(200, "<html><body>Pricing</body></html>")):
            raws = _fetch(prov(name, transport))
            assert len(raws) == 1 and not raws[0].ok and raws[0].payload is None, name


def test_registry():
    assert set(NEW) <= set(PROVIDERS)
    names = {p.name for p in fetch_mod.build_fetchers()}
    assert set(NEW) <= names, names
    assert not {k: v for k, v in mapping.check_mapping_coverage().items() if k in NEW}
    assert set(NEW) <= set(mapping.PROVIDER_MAPPINGS)
    classes = {p: provider_meta.meta(p).provider_class for p in NEW}
    assert classes == {"civo": "general_cloud", "atlanticnet": "general_cloud", "deepinfra": "neocloud"}, classes
    assert {provider_meta.meta(p).source_type for p in NEW} == {"public_page"}, "DeepInfra prices stay from its page"
    from config import settings
    di = next(p for p in fetch_mod.build_fetchers() if p.name == "deepinfra")
    assert str(di._client.base_url).startswith(deepinfra.BASE_URL), "the price page, key or no key"
    assert (di._api is not None) == bool(settings.deepinfra_api_key), "a key adds the stock call"
    # Dropped on policy: Novita (terms forbid scraping), HPC-AI and GMI ('from' floors only), Cirrascale (monthly).
    assert not ({"novita", "hpcai", "gmicloud", "gmi", "cirrascale"} & set(PROVIDERS))
    # New aliases never merge variants.
    assert canonical.canonical_gpu_name("H100NVL") == "NVIDIA H100 94GB NVL"
    for unmapped in ("NVIDIA A100 40GB", "NVIDIA A100 80GB"):
        assert canonical.canonical_gpu_name(unmapped) is None, unmapped


# --------------------------------------------------------------------------
# Pipeline: raw storage -> normalize + quality -> rollup -> index votes
# --------------------------------------------------------------------------


def test_pipeline():
    import fixtures
    import normalize
    import scratchdb
    from sqlalchemy import text as sql
    from analytics import indices, rollups

    db = "opengrid_test_neoclouds2"
    url = scratchdb.create(db)
    try:
        Session = fixtures.session(url)
        fetch_mod.SessionLocal = Session
        normalize.SessionLocal = Session

        now = datetime.now(timezone.utc).replace(microsecond=0)
        for when in (now - timedelta(hours=3), now - timedelta(minutes=5)):  # two polls, three hours apart
            responses = []
            for name in NEW:
                responses += [r.model_copy(update={"fetched_at": when}) for r in _fetch(prov(name))]
            assert all(r.ok for r in responses), [r.error for r in responses if not r.ok]
            fetch_mod.save(responses)
            normalize.refresh(NEW)

        with Session() as s:
            stored = dict(s.execute(sql("SELECT provider, count(*) FROM raw_snapshots GROUP BY 1")).all())
            bodies = s.execute(sql("SELECT count(*) FROM raw_payloads")).scalar()
            listed = dict(s.execute(sql("SELECT provider, count(*) FROM compute_listings GROUP BY 1")).all())
            held = s.execute(sql("SELECT provider, listing_id, rule FROM quality_quarantine WHERE status = 'pending'")).all()
        assert stored == {"civo": 2, "atlanticnet": 2, "deepinfra": 2}, stored
        assert bodies == 3, "identical bodies from the second poll are stored once"
        assert listed == {"civo": 17, "atlanticnet": 2, "deepinfra": 4}, listed
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

        assert voters("NVIDIA L40S 48GB", ".general-cloud") == {"civo", "atlanticnet"}
        assert "deepinfra" in voters("NVIDIA B200 180GB SXM", ".neocloud")
        assert "civo" in voters("NVIDIA H100 80GB SXM5")
        assert not any({"civo", "atlanticnet"} & set(v) for k, v in votes.items() if k.endswith(".neocloud")), \
            "general clouds never vote in a neocloud index"
        l40s = votes[indices.gpu_index_id("NVIDIA L40S 48GB") + ".general-cloud"]
        assert abs(l40s["civo"][0] - 1.29) < 1e-9 and abs(l40s["atlanticnet"][0] - 1.668) < 1e-9
    finally:
        scratchdb.drop(db)


def test_deepinfra_keyed_pipeline():
    """A keyed poll through storage: page prices listed, API stock in capacity, joined by sku."""
    import capacity
    import fixtures
    import normalize
    import scratchdb
    from sqlalchemy import text as sql

    db = "opengrid_test_deepinfra_keyed"
    url = scratchdb.create(db)
    try:
        Session = fixtures.session(url)
        fetch_mod.SessionLocal = Session
        normalize.SessionLocal = Session
        now = datetime.now(timezone.utc).replace(microsecond=0)
        p, _ = _keyed(lambda r: httpx.Response(200, json=_api_body()))
        fetch_mod.save([r.model_copy(update={"fetched_at": now - timedelta(minutes=5)}) for r in _fetch(p)])
        normalize.refresh(["deepinfra"])
        stored = capacity.save(capacity.normalize_all(["deepinfra"]))
        with Session() as s:
            listed = dict(s.execute(sql("SELECT sku, price_per_gpu_hour FROM compute_listings "
                                        "WHERE provider = 'deepinfra'")).all())
            cap = dict(s.execute(sql("SELECT sku, in_stock FROM capacity_current WHERE provider = 'deepinfra'")).all())
        assert listed == {f"{n}x NVIDIA B200": D("4.89") for n in (1, 2, 4, 8)}, listed
        assert stored["current"] == 4 and set(cap) == set(listed) and not any(cap.values()), cap
    finally:
        scratchdb.drop(db)

if __name__ == "__main__":
    for t in (test_civo, test_atlanticnet, test_deepinfra, test_deepinfra_api_stock_page_prices,
              test_deepinfra_api_failures_never_touch_prices, test_deepinfra_capacity_flags, test_fetch_failures,
              test_registry, test_pipeline, test_deepinfra_keyed_pipeline):
        t()
        print(t.__name__, "ok")
    print("all passed")
