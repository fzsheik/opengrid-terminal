"""Nosana: a decentralized GPU network, read through its keyless market list.

The parser runs on a trimmed copy of the REAL response (tests/fixtures_providers/
nosana_markets.json: 10 of 47 markets from GET https://api.nosana.com/api/markets/,
unauthenticated, 2026-10-21). The last test pushes it through raw storage,
normalize + quality screening, the hourly rollup and the index vote in a scratch
database.

Re-checked on 2026-10-21 and still declined (CAPABILITIES.md 3.1), asserted absent
below: Akash (the USD figure is a summary of per-provider bids up to 31 days old,
with a fallback across RAM/interface variants), TensorDock (keyless endpoints
answer 404 or an empty list), io.net (pricing endpoints answer 401) and SF Compute
(orderbook needs a Bearer token). Clore.ai, declined then for its key, was added on
2026-10-21 with an account key (tests/test_provider_clore.py).

Run:  .venv/bin/python tests/test_provider_decentralized.py
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
from providers.nosana import ENDPOINT, NosanaProvider  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_providers"
NOW = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
NEW = ["nosana"]


def text():
    return (FIX / "nosana_markets.json").read_text(encoding="utf-8")


def load():
    return json.loads(text())


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


def _serve(body: str, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.raw_path.decode().split("?")[0] == ENDPOINT:
            return httpx.Response(status, text=body, headers={"content-type": "application/json"})
        return httpx.Response(404, text="not found")
    return httpx.MockTransport(handler)


def prov(body=None, status=200):
    body = text() if body is None else body
    return NosanaProvider(httpx.AsyncClient(base_url="https://api.nosana.com", transport=_serve(body, status)))


def _fetch(p):
    return asyncio.run(p.fetch())


def test_nosana():
    raw = _fetch(prov())
    assert len(raw) == 1 and raw[0].ok and raw[0].endpoint == ENDPOINT
    assert raw[0].payload == load(), "stored untouched"
    out = by_id(NosanaProvider.normalize({ENDPOINT: [snap(raw[0].payload)]}))
    # Read: open PREMIUM markets. Skipped: COMMUNITY (nvidia-4090-community), client-dedicated
    # (zephyr-skyline-4090), OTHER (nvidia-8x-h100; Enterprise 8xA5000 with no USD rate).
    assert set(out) == {"nvidia-h100", "nvidia-4090", "nvidia-a6000", "nvidia-3060", "nvidia-a100-80gb",
                        "nvidia-pro6000"}
    # Renter price = host USD rate x (1 + 10% network fee), exactly.
    assert out["nvidia-h100"].price_per_gpu_hour == D("1.3636") * D("1.1") == D("1.49996")
    assert out["nvidia-4090"].price_per_gpu_hour == D("0.39996")
    assert out["nvidia-a6000"].price_per_gpu_hour == D("0.39996")
    assert out["nvidia-3060"].price_per_gpu_hour == D("0.04796")
    assert out["nvidia-a100-80gb"].price_per_gpu_hour == D("0.89991")
    assert out["nvidia-pro6000"].price_per_gpu_hour == D("0.9999")

    assert out["nvidia-4090"].raw_gpu_name == "NVIDIA 4090"
    assert out["nvidia-4090"].canonical_gpu_name == "NVIDIA RTX 4090 24GB"
    assert out["nvidia-a6000"].canonical_gpu_name == "NVIDIA RTX A6000 48GB"
    # No variant stated, or two products possible: unmapped, never guessed.
    for lid in ("nvidia-h100", "nvidia-3060", "nvidia-a100-80gb", "nvidia-pro6000"):
        assert out[lid].canonical_gpu_name is None, lid
    for l in out.values():
        assert l.provider == "nosana" and l.gpu_count == 1 and l.price_per_instance_hour == l.price_per_gpu_hour
        assert l.provider_tier == "premium" and l.market_type == "on_demand" and l.interruptible is True
        assert l.available is None and l.capacity is None and l.capacity_unit is None
        assert l.region is None and l.country is None
        assert (l.vcpu, l.ram_gb, l.storage_gb) == (None, None, None)
        assert l.sku == l.listing_id and l.currency == "USD" and l.observed_at == NOW


def test_fee_matches_chain():
    # The fee we add is the one the market's own on-chain job price carries:
    # nos_job_price_per_second = nos_reward_per_second x (1 + fee / 100).
    for m in load():
        if m["nos_reward_per_second"]:
            fee = 1 + D(str(m["network_fee_percentage"])) / 100
            ratio = D(str(m["nos_job_price_per_second"])) / D(str(m["nos_reward_per_second"]))
            assert abs(ratio - fee) < D("0.001"), (m["slug"], ratio)


def test_fail_closed():
    body = load()
    assert raises(NosanaProvider.normalize, {ENDPOINT: [snap({"markets": body})]}), "no longer a list"
    bad = copy.deepcopy(body)
    del bad[0]["usd_reward_per_hour"]
    assert raises(NosanaProvider.normalize, {ENDPOINT: [snap(bad)]}), "USD rate field renamed"
    bad = copy.deepcopy(body)
    next(m for m in bad if m["slug"] == "nvidia-h100")["network_fee_percentage"] = None
    assert raises(NosanaProvider.normalize, {ENDPOINT: [snap(bad)]}), "fee unknown: price unknown"
    # A PREMIUM market naming a GPU count is skipped, never read as one GPU.
    odd = copy.deepcopy(body)
    next(m for m in odd if m["slug"] == "nvidia-4090")["name"] = "NVIDIA 2x4090"
    assert "nvidia-4090" not in by_id(NosanaProvider.normalize({ENDPOINT: [snap(odd)]}))
    # A zero USD rate gives no listing.
    zero = copy.deepcopy(body)
    next(m for m in zero if m["slug"] == "nvidia-4090")["usd_reward_per_hour"] = 0
    assert "nvidia-4090" not in by_id(NosanaProvider.normalize({ENDPOINT: [snap(zero)]}))
    # A body that changed shape is stored untouched by fetch(), so normalize sees it and fails.
    raw = _fetch(prov(json.dumps({"data": []})))[0]
    assert raw.payload == {"data": []} and raises(NosanaProvider.normalize, {ENDPOINT: [snap(raw.payload)]})
    assert NosanaProvider.normalize({ENDPOINT: [snap([])]}) == []
    assert NosanaProvider.normalize({}) == []


def test_fetch_failures():
    raw = _fetch(prov("upstream down", status=503))
    assert len(raw) == 1 and not raw[0].ok and raw[0].status_code == 503 and raw[0].endpoint == ENDPOINT

    def boom(request):
        raise httpx.ConnectError("no route")
    p = NosanaProvider(httpx.AsyncClient(base_url="https://api.nosana.com", transport=httpx.MockTransport(boom)))
    raw = _fetch(p)
    assert len(raw) == 1 and not raw[0].ok and "ConnectError" in raw[0].error


def test_registry():
    assert PROVIDERS["nosana"] is NosanaProvider
    assert "NosanaProvider" in {type(f).__name__ for f in fetch_mod.build_fetchers()}
    coverage = mapping.check_mapping_coverage()
    assert "nosana" not in coverage, coverage.get("nosana")
    assert "nosana" in mapping.PROVIDER_MAPPINGS
    m = provider_meta.meta("nosana")
    assert m.provider_class == "decentralized" and m.source_type == "public_api" and m.source == "api.nosana.com"
    # Re-checked and still declined.
    assert not ({"akash", "tensordock", "ionet", "io.net", "sfcompute"} & set(PROVIDERS))


def test_aliases():
    assert canonical.canonical_gpu_name("NVIDIA 4090") == "NVIDIA RTX 4090 24GB"
    assert canonical.canonical_gpu_name("NVIDIA 5090") == "NVIDIA RTX 5090 32GB"
    assert canonical.canonical_gpu_name("NVIDIA 6000 Ada") == "NVIDIA RTX 6000 Ada 48GB"
    for name in ("NVIDIA 3090", "NVIDIA H100", "NVIDIA 3060", "NVIDIA 4080", "NVIDIA 4000/A4000"):
        assert canonical.canonical_gpu_name(name) is None, name


def test_pipeline():
    import fixtures
    import normalize
    import scratchdb
    from sqlalchemy import text as sql
    from analytics import indices, rollups

    db = "opengrid_test_decentralized"
    url = scratchdb.create(db)
    try:
        Session = fixtures.session(url)
        fetch_mod.SessionLocal = Session
        normalize.SessionLocal = Session

        now = datetime.now(timezone.utc).replace(microsecond=0)
        for when in (now - timedelta(hours=3), now - timedelta(minutes=5)):  # two polls, three hours apart
            responses = [r.model_copy(update={"fetched_at": when}) for r in _fetch(prov())]
            assert all(r.ok for r in responses), [r.error for r in responses if not r.ok]
            fetch_mod.save(responses)
            normalize.refresh(NEW)

        with Session() as s:
            stored = dict(s.execute(sql("SELECT provider, count(*) FROM raw_snapshots GROUP BY 1")).all())
            bodies = s.execute(sql("SELECT count(*) FROM raw_payloads")).scalar()
            listed = dict(s.execute(sql("SELECT provider, count(*) FROM compute_listings GROUP BY 1")).all())
            held = s.execute(sql("SELECT provider, listing_id, rule FROM quality_quarantine WHERE status = 'pending'")).all()
        assert stored == {"nosana": 2}, stored
        assert bodies == 1, "identical bodies from the second poll are stored once"
        assert listed == {"nosana": 6}, listed
        assert not held, f"nothing held on clean data: {held}"

        rollups.rebuild()
        with Session() as s:
            last = s.execute(sql("SELECT max(hour) FROM market_hourly")).scalar()
            rows = [dict(r._mapping) for r in s.execute(sql(
                "SELECT segment, gpu, provider, region, country, hour, min_price FROM market_hourly "
                "WHERE hour = :h AND min_price IS NOT NULL"), {"h": last})]
        votes = indices.votes_by_index(rows)
        rtx4090 = indices.gpu_index_id("NVIDIA RTX 4090 24GB")
        # Interruptible: Nosana votes in the spot index, never on-demand or a class index.
        assert "nosana" not in votes.get(rtx4090, {}), "never an on-demand price"
        assert abs(votes[rtx4090 + ".spot"]["nosana"][0] - 0.39996) < 1e-9
        assert not any("nosana" in v for k, v in votes.items() if not k.endswith(".spot")), \
            {k: v for k, v in votes.items() if "nosana" in v}
    finally:
        scratchdb.drop(db)


if __name__ == "__main__":
    for t in (test_nosana, test_fee_matches_chain, test_fail_closed, test_fetch_failures, test_registry,
              test_aliases, test_pipeline):
        t()
        print(t.__name__, "ok")
    print("all passed")
