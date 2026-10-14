"""Horizon Compute and Phyntec, read through Shadeform's keyless catalogue.

Parsers run on trimmed copies of the REAL response (tests/fixtures_providers/,
GET https://api.shadeform.ai/v1/instances/types, unauthenticated, 2026-10-13).
The last test pushes both through raw storage, normalize + quality screening,
the hourly rollup and the index vote in a scratch database.

Candidates checked and not added (TensorDock, Cudo, SF Compute, Akash, io.net,
Prime Intellect, Oblivus, Clore.ai, Nosana; Excess Supply and IMWT via
Shadeform) are asserted absent from the registry below.

Run:  .venv/bin/python tests/test_provider_markets.py
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

import canonical  # noqa: E402
import fetch  # noqa: E402
import mapping  # noqa: E402
import provider_meta  # noqa: E402
from providers import PROVIDERS  # noqa: E402
from providers.shadeform import ENDPOINT, BoostRunProvider, HorizonProvider, PhyntecProvider, ShadeformCloud  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_providers"
NOW = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
NEW = ["horizon", "phyntec"]


def load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def catalogue():
    """One Shadeform body holding three clouds' rows, as the live endpoint does."""
    rows = []
    for name in ("shadeform_horizon.json", "shadeform_phyntec.json", "shadeform_boostrun.json"):
        rows += load(name)["instance_types"]
    return {"instance_types": rows}


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


def _serve(body: str):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.raw_path.decode().startswith(ENDPOINT):
            return httpx.Response(200, text=body, headers={"content-type": "application/json"})
        return httpx.Response(404, text="not found")
    return httpx.MockTransport(handler)


def _provider(cls, body=None):
    body = json.dumps(catalogue()) if body is None else body
    return cls(httpx.AsyncClient(base_url="https://api.shadeform.ai", transport=_serve(body)))


def _fetched(cls):
    raw = asyncio.run(_provider(cls).fetch())
    assert len(raw) == 1 and raw[0].ok and raw[0].endpoint == ENDPOINT
    return raw[0]


# --------------------------------------------------------------------------
# Horizon Compute
# --------------------------------------------------------------------------


def test_horizon():
    raw = _fetched(HorizonProvider)
    assert {t["cloud"] for t in raw.payload["instance_types"]} == {"horizon"}, "other clouds dropped before storage"
    assert raw.payload["instance_types"] == load("shadeform_horizon.json")["instance_types"], "rows kept verbatim"
    out = by_id(HorizonProvider.normalize({ENDPOINT: [snap(raw.payload)]}))
    assert set(out) == {"H100x8", "H200x8"}

    h100, h200 = out["H100x8"], out["H200x8"]
    assert h100.provider == "horizon" and h100.sku == "H100-8"
    assert h100.raw_gpu_name == "H100 sxm5" and h100.canonical_gpu_name == "NVIDIA H100 80GB SXM5"
    assert h100.gpu_count == 8 and h100.price_per_instance_hour == D("15.60")
    assert h100.price_per_gpu_hour == D("1.95"), "1560 cents / 100 / 8 GPUs"
    assert h200.raw_gpu_name == "H200 sxm5" and h200.canonical_gpu_name == "NVIDIA H200 141GB SXM5"
    assert h200.price_per_instance_hour == D("24.00") and h200.price_per_gpu_hour == D("3.00")
    for l in (h100, h200):
        assert l.market_type == "on_demand" and not l.interruptible and l.currency == "USD"
        assert l.provider_tier == "vm/via-shadeform"
        assert l.available is False, "Shadeform lists both sold out today"
        assert l.country is None, "an Icelandic region is never given a guessed country"
        assert l.vcpu == 160 and l.ram_gb == 1536 and l.storage_gb == 17000
        assert l.observed_at == NOW
    assert h100.region == "IS, Blonduos 2" and h200.region == "IS, Blonduos 3"


# --------------------------------------------------------------------------
# Phyntec
# --------------------------------------------------------------------------


def test_phyntec():
    raw = _fetched(PhyntecProvider)
    assert {t["cloud"] for t in raw.payload["instance_types"]} == {"phyntec"}
    out = by_id(PhyntecProvider.normalize({ENDPOINT: [snap(raw.payload)]}))
    assert set(out) == {"RTX4090x8", "RTX5090x8"}

    r4, r5 = out["RTX4090x8"], out["RTX5090x8"]
    assert r4.raw_gpu_name == "RTX4090 pcie" and r4.canonical_gpu_name == "NVIDIA RTX 4090 24GB"
    assert r4.price_per_instance_hour == D("3.20") and r4.price_per_gpu_hour == D("0.40")
    assert r5.raw_gpu_name == "RTX5090 pcie" and r5.canonical_gpu_name == "NVIDIA RTX 5090 32GB"
    assert r5.price_per_instance_hour == D("5.60") and r5.price_per_gpu_hour == D("0.70")
    for l in (r4, r5):
        assert l.gpu_count == 8 and l.market_type == "on_demand" and not l.interruptible
        assert l.provider_tier == "baremetal/via-shadeform"
        assert l.region == "US, Casper, WY" and l.country == "US"
        assert l.available is False
    assert (r4.vcpu, r4.ram_gb, r4.storage_gb) == (48, 256, 3000)
    assert (r5.vcpu, r5.ram_gb, r5.storage_gb) == (64, 256, 7600)

    # Each cloud reads only its own rows, even from the full catalogue.
    full = catalogue()
    assert set(by_id(PhyntecProvider.normalize({ENDPOINT: [snap(full)]}))) == {"RTX4090x8", "RTX5090x8"}
    assert set(by_id(HorizonProvider.normalize({ENDPOINT: [snap(full)]}))) == {"H100x8", "H200x8"}


def test_aliases():
    # The new aliases name single-variant GeForce cards; nothing else was merged.
    assert canonical.canonical_gpu_name("RTX4090 pcie") == "NVIDIA RTX 4090 24GB"
    assert canonical.canonical_gpu_name("RTX5090 pcie") == "NVIDIA RTX 5090 32GB"
    assert canonical.canonical_gpu_name("H100 sxm5") != canonical.canonical_gpu_name("H100 pcie")
    assert canonical.canonical_gpu_name("H100 pcie") == "NVIDIA H100 80GB PCIe"
    # Boost Run's RTX 4090 node now maps too; its RTX 6000 Ada (tagged sxm5) still does not.
    boost = by_id(BoostRunProvider.normalize({ENDPOINT: [snap(load("shadeform_boostrun.json"))]}))
    assert boost["RTX4090x8"].canonical_gpu_name == "NVIDIA RTX 4090 24GB"
    assert boost["RTX6000Adax8"].canonical_gpu_name is None


# --------------------------------------------------------------------------
# Fail closed
# --------------------------------------------------------------------------


def test_fail_closed():
    for cls in (HorizonProvider, PhyntecProvider):
        assert raises(cls.normalize, {ENDPOINT: [snap({"types": []})]}), "instance_types renamed"
        assert raises(cls.normalize, {ENDPOINT: [snap([])]}), "body no longer an object"
        assert raises(cls.normalize, {ENDPOINT: [snap({"instance_types": None})]})
        bad = copy.deepcopy(load(f"shadeform_{cls.cloud}.json"))
        bad["instance_types"][0]["hourly_price"] = "$15.60"
        assert raises(cls.normalize, {ENDPOINT: [snap(bad)]}), "price no longer integer cents"
        bad["instance_types"][0]["hourly_price"] = 15.6
        assert raises(cls.normalize, {ENDPOINT: [snap(bad)]})
        # An empty but well-formed catalogue is a real "nothing listed", not an error.
        assert cls.normalize({ENDPOINT: [snap({"instance_types": []})]}) == []
        assert cls.normalize({}) == []
        # A reshaped body is stored untouched by fetch(), so normalize sees it and fails.
        raw = asyncio.run(_provider(cls, json.dumps({"data": []})).fetch())[0]
        assert raw.payload == {"data": []} and raises(cls.normalize, {ENDPOINT: [snap(raw.payload)]})


# --------------------------------------------------------------------------
# Registry, classes, provenance
# --------------------------------------------------------------------------


def test_registry():
    assert PROVIDERS["horizon"] is HorizonProvider and PROVIDERS["phyntec"] is PhyntecProvider
    assert {type(f).__name__ for f in fetch.build_fetchers()} >= {"HorizonProvider", "PhyntecProvider"}
    assert mapping.check_mapping_coverage() == {}
    assert {"horizon", "phyntec"} <= set(mapping.PROVIDER_MAPPINGS)
    assert set(PROVIDERS) <= set(provider_meta.PROVIDER_META), "every provider is classified"
    for name, site in (("horizon", "https://horizoncompute.com"), ("phyntec", "https://phyntec.net")):
        m = provider_meta.meta(name)
        assert m.provider_class == "neocloud" and m.source_type == "aggregator"
        assert m.website == site and "Shadeform" in m.note
    # No cloud is read both directly and through Shadeform.
    via = {c.cloud for c in PROVIDERS.values() if issubclass(c, ShadeformCloud)}
    direct = {n for n, c in PROVIDERS.items() if not issubclass(c, ShadeformCloud)}
    assert not (via & direct), via & direct
    # Declined candidates stay out: resellers, unidentified clouds, and sources that failed policy.
    declined = {"excesssupply", "imwt", "paperspace", "tensordock", "cudo", "sfcompute", "akash",
                "ionet", "io.net", "primeintellect", "oblivus", "clore", "nosana"}
    assert not (declined & (set(PROVIDERS) | via)), declined & (set(PROVIDERS) | via)


# --------------------------------------------------------------------------
# Pipeline: raw storage -> normalize + quality -> rollup -> index votes
# --------------------------------------------------------------------------


def test_pipeline():
    import fixtures
    import normalize
    import scratchdb
    from sqlalchemy import text as sql
    from analytics import indices, rollups

    db = "opengrid_test_markets"
    url = scratchdb.create(db)
    try:
        Session = fixtures.session(url)
        fetch.SessionLocal = Session
        normalize.SessionLocal = Session

        # Mark Horizon's H100 node in stock so a price reaches the vote.
        body = catalogue()
        for t in body["instance_types"]:
            if t["cloud"] == "horizon" and t["shade_instance_type"] == "H100x8":
                t["availability"][0]["available"] = True
        text_body = json.dumps(body)

        now = datetime.now(timezone.utc).replace(microsecond=0)
        for when in (now - timedelta(hours=3), now - timedelta(minutes=5)):  # two polls, three hours apart
            responses = []
            for cls in (HorizonProvider, PhyntecProvider):
                responses += [r.model_copy(update={"fetched_at": when})
                              for r in asyncio.run(_provider(cls, text_body).fetch())]
            assert all(r.ok for r in responses), [r.error for r in responses if not r.ok]
            fetch.save(responses)
            normalize.refresh(NEW)

        with Session() as s:
            stored = dict(s.execute(sql("SELECT provider, count(*) FROM raw_snapshots GROUP BY 1")).all())
            bodies = s.execute(sql("SELECT count(*) FROM raw_payloads")).scalar()
            listed = dict(s.execute(sql("SELECT provider, count(*) FROM compute_listings GROUP BY 1")).all())
            held = s.execute(sql("SELECT provider, listing_id, rule FROM quality_quarantine WHERE status = 'pending'")).all()
        assert stored == {"horizon": 2, "phyntec": 2}, stored
        assert bodies == 2, "identical bodies from the second poll are stored once"
        assert listed == {"horizon": 2, "phyntec": 2}, listed
        assert not held, f"nothing held on clean data: {held}"

        rollups.rebuild()
        with Session() as s:
            last = s.execute(sql("SELECT max(hour) FROM market_hourly")).scalar()
            rows = [dict(r._mapping) for r in s.execute(sql(
                "SELECT segment, gpu, provider, region, country, hour, min_price FROM market_hourly "
                "WHERE hour = :h AND min_price IS NOT NULL"), {"h": last})]
        votes = indices.votes_by_index(rows)
        h100 = votes.get(indices.gpu_index_id("NVIDIA H100 80GB SXM5") + ".neocloud", {})
        assert set(h100) == {"horizon"} and abs(h100["horizon"][0] - 1.95) < 1e-9, h100
        assert "horizon" not in votes.get(indices.gpu_index_id("NVIDIA H200 141GB SXM5") + ".neocloud", {}), \
            "a sold-out type casts no vote"
        assert not any("phyntec" in v for v in votes.values()), "Phyntec is sold out on Shadeform today"
    finally:
        scratchdb.drop(db)


if __name__ == "__main__":
    for t in (test_horizon, test_phyntec, test_aliases, test_fail_closed, test_registry, test_pipeline):
        t()
        print(t.__name__, "ok")
    print("all passed")
