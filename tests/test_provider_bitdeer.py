"""Bitdeer AI: on-demand prices per card count from its keyless feed (internal-only source); committed prices
(1 / 3 years) only through reserved.py's internal reader.

The fixture is the REAL, unauthenticated body of GET https://www.bitdeer.ai/api/gpu-pricing fetched 2026-11-15
(tests/fixtures_providers/bitdeer_gpu_pricing_2026-11-15.json). No network: every call goes through MockTransport.

Run:  .venv/bin/python tests/test_provider_bitdeer.py
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

os.environ.setdefault("OPENGRID_NO_JOBS", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402

import fetch as fetch_mod  # noqa: E402
import mapping  # noqa: E402
import provider_meta  # noqa: E402
import reserved  # noqa: E402
from providers import PROVIDERS, bitdeer  # noqa: E402
from providers.bitdeer import ENDPOINT, BitdeerProvider  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_providers" / "bitdeer_gpu_pricing_2026-11-15.json"
NOW = datetime(2026, 11, 15, 7, tzinfo=timezone.utc)


def body():
    return json.loads(FIX.read_text(encoding="utf-8"))


def stored(b=None):
    return {"code": 2000, "data": bitdeer.gpu_rows(body() if b is None else b)}


def snap(payload, at=NOW):
    return SimpleNamespace(payload=payload, request=None, fetched_at=at)


def listings(payload=None):
    return {l.listing_id: l for l in BitdeerProvider.normalize({ENDPOINT: [snap(stored() if payload is None else payload)]})}


def raises(fn, *a):
    try:
        fn(*a)
    except ValueError:
        return True
    return False


def poll(response=None, status=200):
    def handler(request):
        assert request.method == "GET" and request.url.path == ENDPOINT
        return httpx.Response(status, json=body() if response is None else response)
    p = BitdeerProvider(httpx.AsyncClient(base_url=bitdeer.BASE_URL, transport=httpx.MockTransport(handler)))
    return asyncio.run(p.fetch())


def test_fetch_stores_gpu_rows_only():
    raws = poll()
    assert [r.endpoint for r in raws] == [ENDPOINT] and raws[0].ok
    kept = raws[0].payload
    assert kept == stored() and kept["code"] == 2000
    assert {r["type"] for r in kept["data"]} == {"gpu"}, "the CPU 'General Compute' row is dropped"
    assert [r["model"] for r in kept["data"]] == ["NVIDIA H100", "NVIDIA H200", "NVIDIA B200", "NVIDIA B300",
                                                  "NVIDIA GB200", "NVIDIA GB300"]
    assert "save" not in json.dumps(kept), "the derivable 'save' amount is not stored"
    assert kept["data"][0]["details"][0] == {"cards": 1, "price": 2.9, "year1": {"price": "2.46500"},
                                             "year3": {"price": "2.46500"}}


def test_every_card_count_is_a_listing():
    out = listings()
    assert len(out) == 4 * 5 + 2 * 10 == 40
    for model, per_gpu in (("nvidia-h100", "2.9"), ("nvidia-h200", "3.56"), ("nvidia-b200", "5.5"), ("nvidia-b300", "9")):
        for n in (1, 2, 4, 6, 8):
            l = out[f"{model}:{n}x:vm"]
            assert l.gpu_count == n and l.price_per_instance_hour == D(per_gpu) * n, (model, n, l.price_per_instance_hour)
            assert l.price_per_gpu_hour == D(per_gpu) and l.provider_tier == "vm"
    h = out["nvidia-h100:1x:vm"]
    assert (h.provider, h.sku, h.raw_gpu_name, h.price_per_gpu_hour) == ("bitdeer", "NVIDIA H100:1x:vm",
                                                                        "NVIDIA H100 80GB VRAM", D("2.9"))
    assert (h.market_type, h.interruptible, h.currency, h.region, h.country) == ("on_demand", False, "USD", None, None)
    assert h.available is None and h.capacity is None and h.observed_at == NOW, "the feed says nothing about stock"
    assert all(l.market_type == "on_demand" for l in out.values()), "committed prices are never listings"


def test_gb_rows_are_per_node():
    out = listings()
    assert sorted(l.gpu_count for l in out.values() if l.listing_id.startswith("nvidia-gb200:")) == \
        [4, 8, 16, 24, 32, 40, 48, 56, 64, 72]
    g = out["nvidia-gb200:4x:bm"]
    assert g.price_per_instance_hour == D("27.764") and g.price_per_gpu_hour == D("6.941") and g.provider_tier == "baremetal"
    assert out["nvidia-gb300:4x:bm"].price_per_gpu_hour == D("10.4115")
    # Read every row: the feed's own figures, not a multiple of the first row.
    assert out["nvidia-gb200:56x:bm"].price_per_instance_hour == D("388.69") != D("27.764") * 14
    assert out["nvidia-gb200:72x:bm"].price_per_instance_hour == D("499.75")


def test_canonical_names():
    names = {l.raw_gpu_name: l.canonical_gpu_name for l in listings().values()}
    assert names == {
        "NVIDIA H100 80GB VRAM": None,  # SXM or PCIe not stated
        "NVIDIA H200 141GB VRAM": "NVIDIA H200 141GB SXM5", "NVIDIA B200 180GB VRAM": "NVIDIA B200 180GB SXM",
        "NVIDIA B300 288GB VRAM": "NVIDIA B300 288GB SXM", "NVIDIA GB200 192GB VRAM": "NVIDIA GB200 186GB",
        "NVIDIA GB300 288GB VRAM": "NVIDIA GB300 288GB"}


def test_reserved_rows_are_read_per_row_not_scaled():
    prices = bitdeer.reserved_prices({ENDPOINT: [snap(stored())]})
    assert len(prices) == 80, "40 card rows x (1 year, 3 years)"
    by = {(p.listing_id, p.months): p for p in prices}
    assert by[("nvidia-h100:1x:vm", 12)].price_per_gpu_hour == D("2.465") == by[("nvidia-h100:1x:vm", 36)].price_per_gpu_hour
    assert by[("nvidia-b300:1x:vm", 12)].price_per_gpu_hour == D("7.65")
    # Not linear in the card count: 2 cards cost 16.74 for a year (8.37 per GPU), not 2 x 7.65.
    assert by[("nvidia-b300:2x:vm", 12)].price_per_gpu_hour == D("8.37")
    assert by[("nvidia-b300:2x:vm", 36)].price_per_gpu_hour == D("7.83")
    assert by[("nvidia-gb200:4x:bm", 36)].price_per_gpu_hour == D("19.99008") / 4
    assert by[("nvidia-b300:2x:vm", 12)].on_demand_per_gpu_hour == D("9") and by[("nvidia-b300:2x:vm", 12)].term == "1 year"

    rows, notes = reserved.build({"bitdeer": {ENDPOINT: [snap(stored())]}},
                                 [l.model_dump() for l in listings().values()], NOW, {"bitdeer": 3600},
                                 reserved.INTERNAL_READERS)
    assert not notes and len(rows) == 80 and {r["provider"] for r in rows} == {"bitdeer"}
    r = next(x for x in rows if x["sku"] == "NVIDIA B200:1x:vm" and x["term_bucket"] == "1y")
    assert (r["price_per_gpu_hour"], r["on_demand_per_gpu_hour"], r["saving_pct"], r["derived"]) == (4.675, 5.5, 15.0, False)
    assert {x["term_bucket"] for x in rows} == {"1y", "3y"} and not any(x["stale"] for x in rows)


def test_internal_only():
    assert provider_meta.provider_internal("bitdeer") and not provider_meta.is_public_provider("bitdeer")
    assert provider_meta.enforce("bitdeer", "public") == "internal", "stored internal whatever a row claims"
    # The public reserved model never reads it: only the internal readers do, and the two sets are disjoint.
    assert "bitdeer" not in reserved.READERS and "bitdeer" not in reserved.PROVIDERS and "bitdeer" not in reserved.SOURCES
    assert set(reserved.INTERNAL_READERS) == {"bitdeer"} == set(reserved.INTERNAL_PROVIDERS)
    assert all(provider_meta.provider_internal(p) for p in reserved.INTERNAL_READERS)
    assert not any(provider_meta.provider_internal(p) for p in reserved.READERS), "no internal provider on the public page"
    rows, _ = reserved.build({"bitdeer": {ENDPOINT: [snap(stored())]}}, [], NOW)
    assert rows == [], "the default (public) build has no Bitdeer row even when its raw is present"


def test_failures_are_loud():
    for bad in ({"code": 5000, "data": body()["data"]}, {"code": 2000}, {"code": 2000, "data": {}}, [],
                {"code": 2000, "data": [r for r in body()["data"] if r["type"] == "cpu"]}):
        raws = poll(bad)
        assert not raws[0].ok and raws[0].payload is None and "bitdeer" in raws[0].error, bad
    assert not poll(status=503)[0].ok

    def reshape(fn):
        b = body()
        fn(b["data"])
        return b
    cases = [
        lambda d: d[0]["details"][0].update(price="free"),          # a price that is not a number
        lambda d: d[0]["details"][0].update(price=0),               # or not positive
        lambda d: d[0]["details"][0].pop("cards"),                  # a row without its card count
        lambda d: d[0]["details"][1].update(cards=1),               # the same card count twice
        lambda d: d[0].update(machineType="container"),             # an unknown machine type
        lambda d: d[0].update(details=[]),                          # a model with no rows
        lambda d: d[3]["details"][1]["year1"].update(price="n/a"),  # a committed price that does not read
        lambda d: d[0].pop("model"),
    ]
    for i, fn in enumerate(cases):
        b = reshape(fn)
        raws = poll(b)
        assert not raws[0].ok and raws[0].payload is None, f"case {i}: a reshaped row fails the poll"
        kept = {"code": 2000, "data": b["data"][:6]}
        assert raises(BitdeerProvider.normalize, {ENDPOINT: [snap(kept)]}), f"case {i}: normalize raises"
    assert raises(BitdeerProvider.normalize, {ENDPOINT: [snap({"rows": []})]})
    assert raises(bitdeer.reserved_prices, {ENDPOINT: [snap({"code": 2000, "data": []})]})
    assert BitdeerProvider.normalize({}) == [] and bitdeer.reserved_prices({}) == []
    # A row that only lacks a committed term still lists on demand.
    b = copy.deepcopy(body())
    del b["data"][0]["details"][0]["year3"]
    assert len(listings(stored(b))) == 40 and len(bitdeer.reserved_prices({ENDPOINT: [snap(stored(b))]})) == 79


def test_registry():
    assert PROVIDERS["bitdeer"] is BitdeerProvider and BitdeerProvider.polling.interval_seconds == 3600
    assert "BitdeerProvider" in {type(f).__name__ for f in fetch_mod.build_fetchers()}
    assert "bitdeer" in mapping.PROVIDER_MAPPINGS and mapping.check_mapping_coverage() == {}
    m = provider_meta.meta("bitdeer")
    assert (m.provider_class, m.source_type, m.source) == ("neocloud", "public_api", "www.bitdeer.ai")
    assert "INTERNAL" in m.note and "INTERNAL ONLY" in (sys.modules["providers.bitdeer"].__doc__ or "")


def test_pipeline():
    """Two polls through storage, normalize + screening: 40 listings, all stored internal, nothing held, and
    none of it in the public rollup."""
    import fixtures
    import normalize
    import scratchdb
    from sqlalchemy import text as sql

    db = "opengrid_test_bitdeer"
    url = scratchdb.create(db)
    try:
        Session = fixtures.session(url)
        fetch_mod.SessionLocal = Session
        normalize.SessionLocal = Session
        now = datetime.now(timezone.utc).replace(microsecond=0)
        for when in (now - timedelta(hours=2), now - timedelta(minutes=5)):
            fetch_mod.save([r.model_copy(update={"fetched_at": when}) for r in poll()])
            normalize.refresh(["bitdeer"])
        with Session() as s:
            listed = dict(s.execute(sql("SELECT visibility, count(*) FROM compute_listings WHERE provider = 'bitdeer' "
                                        "GROUP BY 1")).all())
            h100 = s.execute(sql("SELECT price_per_gpu_hour, market_type FROM compute_listings WHERE provider = "
                                 "'bitdeer' AND listing_id = 'nvidia-h100:1x:vm'")).one()
            held = s.execute(sql("SELECT listing_id, rule FROM quality_quarantine WHERE status = 'pending'")).all()
        assert listed == {"internal": 40} and not held, (listed, held)
        assert (h100[0], h100[1]) == (D("2.900000"), "on_demand")
        rows, notes, newest = reserved.load(now)
        assert not any(r["provider"] == "bitdeer" for r in rows), "the public reserved model has no Bitdeer row"
        rows, notes, newest = reserved.load(now, internal=True)
        assert len(rows) == 80 and newest is not None and all(r["on_demand_per_gpu_hour"] is not None for r in rows)

        # The API: committed prices only behind the admin read; the public reserved endpoints never name Bitdeer.
        import base64

        from fastapi.testclient import TestClient

        import main
        from accounts import security
        from api import reserved as reserved_api
        from config import settings

        def basic(user, pw):
            return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}
        client = TestClient(main.app, headers={"X-OpenGrid-Request": "1"})
        saved = (settings.app_password, settings.public_pages, settings.viewer_user, settings.viewer_password,
                 settings.environment)
        try:
            security.reset()
            reserved_api._model.cache_clear() if hasattr(reserved_api._model, "cache_clear") else None
            settings.app_password, settings.environment = "pw", "development"
            settings.viewer_user, settings.viewer_password = "vc", "look-only"
            settings.public_pages = True
            op, viewer = basic("opengrid", "pw"), basic("vc", "look-only")
            path = "/v1/ops/reserved/internal"
            assert client.get(path).status_code == 401 and client.get(path, headers=viewer).status_code == 403
            got = client.get(path + "?provider=bitdeer&term=3y", headers=op)
            assert got.status_code == 200, got.text
            body = got.json()
            assert body["meta"]["visibility"] == "internal" and body["meta"]["count"] == 40 == len(body["data"])
            assert {r["term_bucket"] for r in body["data"]} == {"3y"}
            assert client.get(path + "?provider=aws", headers=op).status_code == 404
            for public in ("/v1/reserved?limit=1000", "/v1/reserved/summary?limit=1000"):
                r = client.get(public, headers=op)
                assert r.status_code == 200 and "bitdeer" not in r.text.lower(), public
            assert client.get("/v1/reserved?provider=bitdeer", headers=op).status_code == 404
            assert path not in main.app.openapi()["paths"]
        finally:
            (settings.app_password, settings.public_pages, settings.viewer_user, settings.viewer_password,
             settings.environment) = saved
    finally:
        scratchdb.drop(db)


if __name__ == "__main__":
    for t in (test_fetch_stores_gpu_rows_only, test_every_card_count_is_a_listing, test_gb_rows_are_per_node,
              test_canonical_names, test_reserved_rows_are_read_per_row_not_scaled, test_internal_only,
              test_failures_are_loud, test_registry, test_pipeline):
        t()
        print(t.__name__, "ok")
    print("all passed")
