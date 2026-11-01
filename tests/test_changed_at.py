"""When a change happened: provider effective times, the observation bracket, and the backfill (changed_at.py).

Unit half: the rules (provider_time), and each provider's normalizer carrying its own time (AWS spot `timestamp`,
Nebius `effectiveTime`, Azure `effectiveStartDate`, GCP `effectiveTime`) from the saved REAL fixtures.
Database half (scratch database `og_test_changed_at`, dropped after): three Nebius polls through the real pipeline
(fetch.save -> normalize.refresh), then the backfill re-deriving the same values for rows that lack them.
No network.

Run:  .venv/bin/python tests/test_changed_at.py
"""

import copy
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("OPENGRID_NO_JOBS", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import scratchdb  # noqa: E402,F401  (blanks real credentials, refuses real network)

import changed_at  # noqa: E402
from models import ComputeListing  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_providers"
NOW = datetime(2026, 11, 1, 21, tzinfo=timezone.utc)
UTC = timezone.utc


def _listing(**kw) -> ComputeListing:
    base = dict(provider="p", sku="s", listing_id="l", raw_gpu_name="x", canonical_gpu_name=None, gpu_count=1,
                region=None, country=None, price_per_gpu_hour=Decimal("2"), price_per_instance_hour=Decimal("2"),
                market_type="spot", provider_tier=None, interruptible=True, available=None, capacity=None,
                capacity_unit=None, vcpu=None, ram_gb=None, storage_gb=None, observed_at=NOW)
    return ComputeListing(**{**base, **kw})


def test_parse_time():
    assert changed_at.parse_time("2026-11-01T22:00:00.000Z") == datetime(2026, 11, 1, 22, tzinfo=UTC)
    assert changed_at.parse_time("2026-10-31T00:26:00Z") == datetime(2026, 10, 31, 0, 26, tzinfo=UTC)
    assert changed_at.parse_time("2026-10-31T02:26:00+02:00") == datetime(2026, 10, 31, 0, 26, tzinfo=UTC)
    assert changed_at.parse_time("not a time") is None and changed_at.parse_time(None) is None


def test_provider_time_rules():
    t = NOW - timedelta(minutes=7)
    prev = SimpleNamespace(price_per_gpu_hour=Decimal("2.5"), price_per_instance_hour=Decimal("2.5"))
    same = SimpleNamespace(price_per_gpu_hour=Decimal("2.000000"), price_per_instance_hour=Decimal("2"))
    timed = _listing(provider_changed_at=t, changed_at_basis="exact")
    assert changed_at.provider_time(timed, prev) == (t, "exact"), "a price move keeps the provider's time"
    assert changed_at.provider_time(timed, None) == (t, "exact"), "a first row keeps it too"
    assert changed_at.provider_time(timed, same) == (None, "poll_window"), "only stock moved: the time is not its"
    assert changed_at.provider_time(_listing(), prev) == (None, "poll_window"), "no provider time"
    future = _listing(provider_changed_at=NOW + timedelta(days=3), changed_at_basis="date_only")
    assert changed_at.provider_time(future, prev) == (None, "poll_window"), "a scheduled change is not trusted"
    skew = _listing(provider_changed_at=NOW + timedelta(minutes=2), changed_at_basis="exact")
    assert changed_at.provider_time(skew, prev)[1] == "exact", "a clock a little ahead is tolerated"
    # the excluded fields never reach current state
    assert "provider_changed_at" not in timed.model_dump() and "changed_at_basis" not in timed.model_dump()


def test_aws_spot_exact():
    from providers.aws import SPOT_PRICES, spot_listings

    snap = SimpleNamespace(fetched_at=NOW, request=None, endpoint="x" + SPOT_PRICES, payload={
        "region": "us-east-1", "spotPriceHistorySet": [
            {"instanceType": "p5.48xlarge", "availabilityZone": "us-east-1a", "productDescription": "Linux/UNIX",
             "spotPrice": "30.10", "timestamp": "2026-11-01T18:00:00.000Z"},
            {"instanceType": "p5.48xlarge", "availabilityZone": "us-east-1a", "productDescription": "Linux/UNIX",
             "spotPrice": "29.00", "timestamp": "2026-11-01T20:15:00.000Z"}]})
    (l,) = spot_listings({snap.endpoint: [snap]})
    assert l.price_per_instance_hour == Decimal("29.00")
    assert l.provider_changed_at == datetime(2026, 11, 1, 20, 15, tzinfo=UTC) and l.changed_at_basis == "exact"


def test_nebius_spot_exact_page_none():
    from providers.nebius import ENDPOINT, SPOT_ENDPOINT, NebiusProvider, extract

    spot = json.loads((FIX / "nebius_spot_price.json").read_text())
    page = extract((FIX / "nebius_prices_2026-11.html").read_text(encoding="utf-8"))
    mk = lambda p, e: SimpleNamespace(payload=p, request=None, fetched_at=NOW, endpoint=e, ok=True)  # noqa: E731
    out = NebiusProvider.normalize({ENDPOINT: [mk(page, ENDPOINT)], SPOT_ENDPOINT: [mk(spot, SPOT_ENDPOINT)]})
    by = {l.listing_id: l for l in out}
    h200 = by["NVIDIA HGX H200:gpu-h200-sxm:eu-north1:spot"]
    assert h200.provider_changed_at == datetime(2026, 11, 1, 18, 41, tzinfo=UTC) and h200.changed_at_basis == "exact"
    on_demand = [l for l in out if l.market_type == "on_demand"]
    assert on_demand and all(l.provider_changed_at is None for l in on_demand), "the page carries no time"


def test_azure_date_only():
    from providers import azure

    body = json.loads((FIX / "azure_eastus.json").read_text())
    items, _ = azure.extract(body, "eastus")
    snap = SimpleNamespace(payload={"region": "eastus", "items": items}, request=None, fetched_at=NOW)
    out = azure.AzureProvider.normalize({azure.endpoint("eastus"): [snap]})
    assert out and all(l.changed_at_basis == "date_only" for l in out)
    for l in out:
        row = next(r for r in items if r["armSkuName"] == l.sku and azure.market(r) == l.market_type)
        assert l.provider_changed_at.date().isoformat() == row["effectiveStartDate"][:10]
        assert (l.provider_changed_at.hour, l.provider_changed_at.minute) == (0, 0)


def test_gcp_lower_bound_is_newest_component():
    from providers import gcp

    body = json.loads((FIX / "gcp_catalog_skus.json").read_text())
    snap = SimpleNamespace(payload=body, request=None, fetched_at=NOW, endpoint=gcp.ENDPOINT)
    base = {l.listing_id: l for l in gcp.GcpProvider.normalize({gcp.ENDPOINT: [snap]})}
    assert base and all(l.changed_at_basis == "lower_bound" for l in base.values())
    a3 = base["a3-highgpu-8g:us-central1"]
    # Move the A3 core SKU's effectiveTime later in Iowa: the assembled VM's time follows it.
    later = copy.deepcopy(body)
    for s in later["skus"]:
        if s["description"].startswith("A3 Instance Core running in") and "us-central1" in s["serviceRegions"] \
                and s["category"]["usageType"] == "OnDemand":
            s["pricingInfo"][0]["effectiveTime"] = "2026-10-31T09:30:00Z"
    snap2 = SimpleNamespace(payload=later, request=None, fetched_at=NOW, endpoint=gcp.ENDPOINT)
    moved = {l.listing_id: l for l in gcp.GcpProvider.normalize({gcp.ENDPOINT: [snap2]})}["a3-highgpu-8g:us-central1"]
    assert moved.provider_changed_at == max(a3.provider_changed_at, datetime(2026, 10, 31, 9, 30, tzinfo=UTC))
    assert moved.price_per_instance_hour == a3.price_per_instance_hour, "prices untouched"


# --------------------------------------------------------------------------
# Pipeline + backfill
# --------------------------------------------------------------------------


def _nebius_responses(at, spot_price=None, effective=None, page_row=None, page_price=None):
    from models import RawResponse
    from providers.nebius import ENDPOINT, SPOT_ENDPOINT, extract

    spot = json.loads((FIX / "nebius_spot_price.json").read_text())
    if spot_price:
        spot["items"][0]["price"]["value"] = spot_price
        spot["items"][0]["effectiveTime"] = effective
    html = (FIX / "nebius_prices_2026-11.html").read_text(encoding="utf-8")
    page = extract(html)
    if page_row is not None:
        page["rows"][page_row][-1] = page_price  # the on-demand column
    return [RawResponse(provider="nebius", endpoint=ENDPOINT, fetched_at=at, status_code=200, payload=page),
            RawResponse(provider="nebius", endpoint=SPOT_ENDPOINT, fetched_at=at, status_code=200, payload=spot)]


def test_pipeline_and_backfill():
    import fetch as fetch_mod
    import fixtures
    import normalize
    from sqlalchemy import select, text, update
    from tables import ListingObservation as O

    db = "og_test_changed_at"
    url = scratchdb.create(db)
    saved = fetch_mod.SessionLocal, normalize.SessionLocal
    try:
        Session = fixtures.session(url)
        fetch_mod.SessionLocal = normalize.SessionLocal = Session
        t0 = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=2)
        t1, t2 = t0 + timedelta(minutes=30), t0 + timedelta(minutes=60)
        spot0 = json.loads((FIX / "nebius_spot_price.json").read_text())["items"][0]
        spot_id = "NVIDIA HGX H200:gpu-h200-sxm:eu-north1:spot"
        assert spot0["computeInstance"]["platform"] == "gpu-h200-sxm" and spot0["region"] == "eu-north1"
        page = _nebius_responses(t0)[0].payload
        row = next(i for i, r in enumerate(page["rows"]) if r[-1].startswith("$"))
        item = page["rows"][row][0]
        for responses in (_nebius_responses(t0), _nebius_responses(t1),
                          _nebius_responses(t2, spot_price="2.95", effective=(t2 - timedelta(minutes=4)).isoformat(),
                                            page_row=row, page_price="$19.99")):
            fetch_mod.save(responses)
            normalize.refresh(["nebius"])

        def rows():
            with Session() as s:
                return {(r.listing_id, r.observed_at): r for r in s.scalars(select(O).where(O.provider == "nebius"))}

        live = rows()
        first = live[(spot_id, t0)]
        assert first.prev_observed_at is None, "a listing's first row has no bracket"
        assert first.provider_changed_at == datetime(2026, 11, 1, 18, 41, tzinfo=UTC)
        assert first.changed_at_basis == "exact"
        assert (spot_id, t1) not in live, "nothing moved at t1: no row"
        moved = live[(spot_id, t2)]
        assert moved.prev_observed_at == t1, "the bracket is the last fetch that showed the old price"
        assert moved.provider_changed_at == t2 - timedelta(minutes=4) and moved.changed_at_basis == "exact"
        page_moved = [r for (lid, t), r in live.items() if lid == item and t == t2]
        assert page_moved, f"the page price change for {item} is recorded"
        assert page_moved[0].prev_observed_at == t1 and page_moved[0].provider_changed_at is None
        assert page_moved[0].changed_at_basis == "poll_window"
        assert all(r.changed_at_basis for r in live.values())

        # Pretend the rows predate migration 0028; the backfill re-derives the same values from raw.
        with Session.begin() as s:
            s.execute(update(O).values(prev_observed_at=None, provider_changed_at=None, changed_at_basis=None))
        result = changed_at.backfill()["nebius"]
        assert result["provider_times"] > 0 and result["brackets"] == 2, result
        back = rows()
        for key, r in live.items():
            b = back[key]
            assert (b.prev_observed_at, b.provider_changed_at, b.changed_at_basis) == \
                   (r.prev_observed_at, r.provider_changed_at, r.changed_at_basis), (key, b.__dict__, r.__dict__)
        again = changed_at.backfill()["nebius"]
        assert again == {"provider_times": 0, "brackets": 0, "poll_window": 0}, again
        with Session() as s:
            assert s.execute(text("SELECT count(*) FROM listing_observations WHERE changed_at_basis IS NULL")).scalar() == 0
        hist = [h for h in normalize.history("nebius") if h["listing_id"] == spot_id]
        assert hist[0]["provider_changed_at"] == moved.provider_changed_at and hist[0]["changed_at_basis"] == "exact"
    finally:
        fetch_mod.SessionLocal, normalize.SessionLocal = saved
        scratchdb.drop(db)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(name, "ok")
    print("all passed")
