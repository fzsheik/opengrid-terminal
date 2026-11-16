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
2026-10-21 with an account key (tests/test_provider_clore.py). Akash was added on
2026-11-01 as a market summary, with those limits documented (tests/test_provider_akash.py).

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
from providers.nosana import ENDPOINT, QUEUED, NosanaProvider  # noqa: E402

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


def queued_text():
    return (FIX / "nosana_queued_nodes.json").read_text(encoding="utf-8")


def _serve(body: str, status: int = 200, queued: str | None = None, queued_status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.raw_path.decode().split("?")[0]
        if path == ENDPOINT:
            return httpx.Response(status, text=body, headers={"content-type": "application/json"})
        if path == QUEUED:
            return httpx.Response(queued_status, text=queued_text() if queued is None else queued,
                                  headers={"content-type": "application/json"})
        return httpx.Response(404, text="not found")
    return httpx.MockTransport(handler)


def prov(body=None, status=200, **queue):
    body = text() if body is None else body
    return NosanaProvider(httpx.AsyncClient(base_url="https://api.nosana.com", transport=_serve(body, status, **queue)))


def _fetch(p):
    return asyncio.run(p.fetch())


def test_nosana():
    raw = _fetch(prov())
    assert [r.endpoint for r in raw] == [ENDPOINT, QUEUED] and all(r.ok for r in raw)
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
    assert len(raw) == 2 and not raw[0].ok and raw[0].status_code == 503 and raw[0].endpoint == ENDPOINT

    def boom(request):
        raise httpx.ConnectError("no route")
    p = NosanaProvider(httpx.AsyncClient(base_url="https://api.nosana.com", transport=httpx.MockTransport(boom)))
    raw = _fetch(p)
    assert len(raw) == 2 and not any(r.ok for r in raw) and "ConnectError" in raw[0].error


def cap(markets, queued):
    return {o.sku: o for o in NosanaProvider.normalize_capacity({ENDPOINT: [snap(markets)], QUEUED: [snap(queued)]})}


def test_capacity_queued_hosts():
    """nosana_queued_nodes.json: the whole answer of GET /api/nodes/queued-nodes on 2026-11-15 (87 hosts, untrimmed)."""
    raw = _fetch(prov())
    queued = json.loads(queued_text())
    assert raw[1].ok and raw[1].payload == queued and len(queued) == 87, "stored untouched"
    out = cap(raw[0].payload, raw[1].payload)
    # One row per market that has a listing; hosts counted by hand from the fixture.
    assert set(out) == set(by_id(NosanaProvider.normalize({ENDPOINT: [snap(raw[0].payload)]})))
    assert {k: v.available_count for k, v in out.items()} == {
        "nvidia-3060": 5, "nvidia-a6000": 1, "nvidia-pro6000": 1, "nvidia-4090": 0, "nvidia-h100": 0, "nvidia-a100-80gb": 0}
    a = out["nvidia-3060"]
    assert a.in_stock is True and a.count_unit == "instance" and a.total_count is None and a.tier is None
    assert a.region is None and a.gpu_count == 1 and a.raw_value == "5 queued nodes" and a.source == QUEUED
    assert out["nvidia-4090"].canonical_gpu_name == "NVIDIA RTX 4090 24GB"
    # No host waiting: a job posted now queues. The linked community market's 5 hosts are not counted in.
    assert out["nvidia-4090"].in_stock is False and out["nvidia-h100"].in_stock is False
    assert sum(1 for r in queued if r["marketAddress"] == "Dcwz62TisNbWuto6KJM2EGYGVKnHbdZGVGmgLASzsXy8") == 5

    twice = queued + [r for r in queued if r["marketAddress"] == "EjryZ6XEthz3z7nnLfjXBYafyn7VyHgChfbfM47LfAao"]
    assert cap(load(), twice)["nvidia-a6000"].available_count == 1, "a host listed twice is one host"

    # Reshaped answers raise; none reads as "nothing in stock".
    assert raises(cap, load(), []), "an empty queue list"
    assert raises(cap, load(), {"data": queued})
    assert raises(cap, load(), [{**queued[0], "state": "RUNNING"}] + queued[1:]), "an unknown state"
    assert raises(cap, load(), [{k: v for k, v in queued[0].items() if k != "marketAddress"}])
    assert raises(cap, {"markets": load()}, queued)
    lost = load()
    del lost[3]["address"]
    assert raises(cap, lost, queued), "nothing to join the queue on"
    # No queue snapshot: no rows (unknown), and prices are untouched either way.
    assert NosanaProvider.normalize_capacity({ENDPOINT: [snap(load())]}) == []
    assert NosanaProvider.normalize_capacity({}) == []


def test_capacity_failure_leaves_prices():
    raw = _fetch(prov(queued_status=503))
    assert raw[0].ok and not raw[1].ok and raw[1].endpoint == QUEUED
    by = {r.endpoint: [snap(r.payload)] for r in raw if r.ok}
    assert len(NosanaProvider.normalize(by)) == 6 and NosanaProvider.normalize_capacity(by) == []
    raw = _fetch(prov(queued="[]"))
    by = {r.endpoint: [snap(r.payload)] for r in raw if r.ok}
    assert len(NosanaProvider.normalize(by)) == 6 and raises(NosanaProvider.normalize_capacity, by)


def test_registry():
    assert PROVIDERS["nosana"] is NosanaProvider
    assert "NosanaProvider" in {type(f).__name__ for f in fetch_mod.build_fetchers()}
    coverage = mapping.check_mapping_coverage()
    assert "nosana" not in coverage, coverage.get("nosana")
    assert "nosana" in mapping.PROVIDER_MAPPINGS
    m = provider_meta.meta("nosana")
    assert m.provider_class == "decentralized" and m.source_type == "public_api" and m.source == "api.nosana.com"
    # Re-checked and still declined.
    # Akash was added on 2026-11-01 with its bid-derived limits documented (tests/test_provider_akash.py).
    assert not ({"tensordock", "ionet", "io.net", "sfcompute"} & set(PROVIDERS))


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
        assert stored == {"nosana": 4}, stored  # two polls x (markets, queued nodes)
        assert bodies == 2, "identical bodies from the second poll are stored once"
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
    for t in (test_nosana, test_fee_matches_chain, test_fail_closed, test_fetch_failures, test_capacity_queued_hosts,
              test_capacity_failure_leaves_prices, test_registry,
              test_aliases, test_pipeline):
        t()
        print(t.__name__, "ok")
    print("all passed")
