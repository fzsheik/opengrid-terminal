"""E2E Networks (published price feed) and SimplePod (public rental market).

Parsers run on trimmed copies of the REAL responses (tests/fixtures_providers/,
fetched 2026-10-21 with unauthenticated GETs). The last test pushes both through
raw storage, normalize + quality screening, the hourly rollup and the index vote
in a scratch database.

Run:  .venv/bin/python tests/test_provider_neoclouds3.py
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
import fetch as fetch_mod  # noqa: E402
import mapping  # noqa: E402
import provider_meta  # noqa: E402
from providers import PROVIDERS  # noqa: E402
from providers.e2e import ENDPOINT as E2E_EP, E2EProvider  # noqa: E402
from providers.simplepod import MARKETS, PAGE_SIZE, SimplePodProvider  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_providers"
NOW = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
NEW = ["e2e", "simplepod"]
SP_DOCKER, SP_VM = MARKETS["docker"][0], MARKETS["vm"][0]


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


def _client(base, handler):
    return httpx.AsyncClient(base_url=base, transport=httpx.MockTransport(handler))


def _fetch(prov):
    return asyncio.run(prov.fetch())


def e2e_prov():
    def handler(request):
        if request.url.path == "/pricing.json":
            return httpx.Response(200, text=text("e2e_pricing.json"), headers={"content-type": "application/json"})
        return httpx.Response(404, text="not found")
    return E2EProvider(_client("https://www.e2enetworks.com", handler))


def simplepod_prov():
    def handler(request):
        if request.url.path != "/instances/market/list" or request.url.params.get("rentalStatus") != "active":
            return httpx.Response(404, text="not found")
        name = "simplepod_market_vm.json" if request.url.params.get("mode") == "vm" else "simplepod_market_docker.json"
        return httpx.Response(200, text=text(name), headers={"content-type": "application/json"})
    return SimplePodProvider(_client("https://api.simplepod.ai", handler))


def e2e_normalize(payload):
    return E2EProvider.normalize({E2E_EP: [snap(payload)]})


def sp_normalize(docker, vm):
    return SimplePodProvider.normalize({SP_DOCKER: [snap(docker)], SP_VM: [snap(vm)]})


# --------------------------------------------------------------------------
# E2E Networks
# --------------------------------------------------------------------------


def test_e2e():
    raw = _fetch(e2e_prov())[0]
    assert raw.ok and raw.endpoint == E2E_EP
    assert set(raw.payload) == {"generatedAt", "notes", "gpuInstances"}, "CPU plans and storage dropped"
    out = by_id(e2e_normalize(raw.payload))
    assert len(out) == 12
    h200 = out["h200:1x"]
    assert h200.raw_gpu_name == "NVIDIA H200 141GB" and h200.canonical_gpu_name == "NVIDIA H200 141GB SXM5"
    assert h200.price_per_gpu_hour == h200.price_per_instance_hour == D("5.29"), "USD, not the INR 502.55"
    assert h200.gpu_count == 1 and h200.vcpu == 30 and h200.ram_gb == 375
    assert h200.market_type == "on_demand" and not h200.interruptible and h200.available is None
    assert h200.region is None and h200.country is None and h200.provider_tier == "vm"
    assert out["b200:1x"].price_per_gpu_hour == D("7.99") and out["b200:1x"].canonical_gpu_name == "NVIDIA B200 180GB SXM"
    assert out["a100-80gb:1x"].price_per_gpu_hour == D("2.1") and out["a100-80gb:1x"].raw_gpu_name == "NVIDIA A100 80GB"
    assert out["t4:1x"].canonical_gpu_name == "NVIDIA T4 16GB" and out["a40:1x"].canonical_gpu_name == "NVIDIA A40 48GB"
    # Variant not stated: never guessed.
    for lid in ("h100:1x", "a100-80gb:1x", "a100-40gb:1x", "rtx-pro-6000:1x", "v100:1x", "a30:1x"):
        assert out[lid].canonical_gpu_name is None, lid
    # Spot and committed rates are INR-only: never a listing.
    assert all(l.market_type == "on_demand" for l in out.values())


def test_e2e_skips_and_reshape():
    body = json.loads(text("e2e_pricing.json"))
    no_usd = copy.deepcopy(body)
    no_usd["gpuInstances"][0]["pricing"].pop("usd")
    assert "b200:1x" not in by_id(e2e_normalize(no_usd)) and len(e2e_normalize(no_usd)) == 11
    monthly = copy.deepcopy(body)
    monthly["gpuInstances"][2]["unit"] = "per month"
    assert raises(e2e_normalize, monthly), "a row priced per month fails the poll"
    assert raises(e2e_normalize, {"gpuInstances": []})
    assert raises(e2e_normalize, {"products": []})
    lost = copy.deepcopy(body)
    del lost["gpuInstances"][1]["model"]
    assert raises(e2e_normalize, lost)


# --------------------------------------------------------------------------
# SimplePod
# --------------------------------------------------------------------------


def test_simplepod():
    raws = _fetch(simplepod_prov())
    assert [r.endpoint for r in raws] == [SP_DOCKER, SP_VM] and all(r.ok for r in raws)
    assert "downloadSpeedtest" not in raws[0].payload[0], "churny fields dropped before storage"
    out = by_id(SimplePodProvider.normalize({r.endpoint: [snap(r.payload)] for r in raws}))
    assert len(out) == 37
    # pricePerGpu is the whole offer: one host's V100 at 1..5 GPUs is 0.1 per GPU throughout.
    for n, lid in enumerate(("docker:887", "docker:888", "docker:889", "docker:890", "docker:891"), 1):
        v = out[lid]
        assert v.gpu_count == n and v.price_per_instance_hour == D("0.1") * n and v.price_per_gpu_hour == D("0.1"), lid
    v100 = out["docker:887"]
    assert v100.raw_gpu_name == "Tesla V100-PCIE-16GB (16 GB)" and v100.canonical_gpu_name == "NVIDIA Tesla V100 16GB PCIe"
    assert v100.region == "CA-QB-02" and v100.country == "CA" and v100.provider_tier == "container/community"
    assert v100.vcpu == 11 and v100.ram_gb == 30 and v100.storage_gb == 461
    assert v100.available is True and v100.market_type == "on_demand" and v100.interruptible is False
    six = out["docker:86"]
    assert six.gpu_count == 6 and six.price_per_instance_hour == D("1.266") and six.price_per_gpu_hour == D("0.211")
    assert six.canonical_gpu_name == "NVIDIA RTX 3060 12GB"
    vm = out["vm:300"]
    assert vm.provider_tier == "vm/enterprise" and vm.country == "PL" and vm.canonical_gpu_name == "NVIDIA RTX 5090 32GB"
    assert vm.gpu_count == 4 and vm.price_per_instance_hour == D("3.992") and vm.price_per_gpu_hour == D("0.998")
    assert out["vm:30"].canonical_gpu_name == "NVIDIA RTX 4090 24GB" and out["vm:30"].price_per_gpu_hour == D("0.691")
    # Unmapped on purpose: no canonical V100 32GB, RTX PRO 6000 edition unstated, a MIG slice.
    assert out["docker:582"].canonical_gpu_name is None
    assert out["vm:434"].raw_gpu_name == "RTX PRO 6000 Blackwell (96 GB)" and out["vm:434"].canonical_gpu_name is None
    assert out["docker:289"].canonical_gpu_name is None


def test_simplepod_skips_and_reshape():
    docker, vm = json.loads(text("simplepod_market_docker.json")), json.loads(text("simplepod_market_vm.json"))
    d = copy.deepcopy(docker)
    d[0]["isAvailableForDemand"] = False
    d[1]["errors"] = ["gpu fell off the bus"]
    d[2]["rigRentalHostMaintenances"] = [{"id": 1}]
    d[3]["pricePerGpu"] = 0
    d[4]["gpuCount"] = 0
    out = by_id(sp_normalize(d, vm))
    assert len(out) == 32
    assert not {f"docker:{o['id']}" for o in d[:5]} & set(out)
    assert raises(sp_normalize, {"error": "maintenance"}, vm), "a non-list market fails the poll"
    assert raises(sp_normalize, docker * (PAGE_SIZE // len(docker) + 1), vm), "a full page could hide offers"
    lost = copy.deepcopy(vm)
    del lost[0]["pricePerGpu"]
    assert raises(sp_normalize, docker, lost)


# --------------------------------------------------------------------------
# Fetch failures, registry, classes
# --------------------------------------------------------------------------


def test_fetch_failures():
    """An error page or a refused connection is stored as a failed response, never as prices."""
    def down(request):
        return httpx.Response(503, text="maintenance")

    def refused(request):
        raise httpx.ConnectError("refused")
    for handler in (down, refused):
        for prov in (E2EProvider(_client("https://x", handler)), SimplePodProvider(_client("https://x", handler))):
            raws = _fetch(prov)
            assert raws and all(not r.ok and r.payload is None for r in raws), prov.name


def test_failed_snapshots_never_become_prices():
    assert raises(e2e_normalize, None)
    assert raises(sp_normalize, None, None)


def test_registry():
    assert set(NEW) <= set(PROVIDERS)
    names = {p.name for p in fetch_mod.build_fetchers()}
    assert set(NEW) <= names, names
    assert not {k: v for k, v in mapping.check_mapping_coverage().items() if k in NEW}
    assert set(NEW) <= set(mapping.PROVIDER_MAPPINGS)
    assert provider_meta.meta("e2e").provider_class == "general_cloud"
    assert provider_meta.meta("e2e").source_type == "public_pricing_file"
    assert provider_meta.meta("simplepod").provider_class == "marketplace"
    assert provider_meta.meta("simplepod").source_type == "public_api"
    # New aliases never merge variants.
    assert canonical.canonical_gpu_name("NVIDIA H100 80GB") is None
    assert canonical.canonical_gpu_name("NVIDIA A100 80GB") is None
    assert canonical.canonical_gpu_name("Tesla V100-SXM2-32GB (32 GB)") is None


# --------------------------------------------------------------------------
# Pipeline: raw storage -> normalize + quality -> rollup -> index votes
# --------------------------------------------------------------------------


def test_pipeline():
    import fixtures
    import normalize
    import scratchdb
    from sqlalchemy import text as sql
    from analytics import indices, rollups

    db = "opengrid_test_neoclouds3"
    url = scratchdb.create(db)
    try:
        Session = fixtures.session(url)
        fetch_mod.SessionLocal = Session
        normalize.SessionLocal = Session

        now = datetime.now(timezone.utc).replace(microsecond=0)
        for when in (now - timedelta(hours=3), now - timedelta(minutes=5)):  # two polls, three hours apart
            responses = []
            for p in (e2e_prov(), simplepod_prov()):
                responses += [r.model_copy(update={"fetched_at": when}) for r in _fetch(p)]
            assert all(r.ok for r in responses), [r.error for r in responses if not r.ok]
            fetch_mod.save(responses)
            normalize.refresh(NEW)

        with Session() as s:
            stored = dict(s.execute(sql("SELECT provider, count(*) FROM raw_snapshots GROUP BY 1")).all())
            bodies = s.execute(sql("SELECT count(*) FROM raw_payloads")).scalar()
            listed = dict(s.execute(sql("SELECT provider, count(*) FROM compute_listings GROUP BY 1")).all())
            held = s.execute(sql("SELECT provider, listing_id, rule FROM quality_quarantine WHERE status = 'pending'")).all()
        assert stored == {"e2e": 2, "simplepod": 4}, stored
        assert bodies == 3, "identical bodies from the second poll are stored once"
        assert listed == {"e2e": 12, "simplepod": 37}, listed
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

        assert voters("NVIDIA L40S 48GB", ".general-cloud") == {"e2e"}
        assert "e2e" in voters("NVIDIA H200 141GB SXM5") and "simplepod" in voters("NVIDIA RTX 5090 32GB")
        assert voters("NVIDIA H100 80GB SXM5") == set(), "E2E's H100 is not labelled SXM"
        assert not any("e2e" in v for k, v in votes.items() if k.endswith(".neocloud")), \
            "a general cloud never votes in a neocloud index"
    finally:
        scratchdb.drop(db)


if __name__ == "__main__":
    for t in (test_e2e, test_e2e_skips_and_reshape, test_simplepod, test_simplepod_skips_and_reshape,
              test_fetch_failures, test_failed_snapshots_never_become_prices, test_registry, test_pipeline):
        t()
        print(t.__name__, "ok")
    print("all passed")
