"""In-stock history from capacity_changes (capacity_history.py) and what it changes in routing and the listing reads.

Motivation (production, 2026-11-16, last 14 days): Verda had a price listed 100% of hours but was in stock 5% of the
time; routing scored its availability history from listed prices, so it looked fully reliable.

Covers, with shares worked out by hand:
    - a count provider's sequence with a closing "no longer reported" row (data ends at last seen + stale window,
      not out of stock) and a later return; a count without in_stock reads as in stock when above zero;
    - a tier provider's sequence with an unmapped word (unknown) and a stale current row (tail is no data);
    - combining keys: in stock if any key is, unknown before out;
    - the per-provider join rules (jarvislabs, hyperbolic, voltagepark, lium, list regions, Shadeform text, AWS,
      Massed Compute's GPU grain);
    - routing: a provider listed all month but mostly sold out ranks BELOW one in stock (the Verda case), with the
      factor's note; the fallback label and half credit for a price of unknown stock; a fresh out-of-stock feed row
      excludes a silent listing (sold_out_stock_feed), a stale one does not; require_available accepts the feed;
    - the listing read's stock fields; internal capacity rows never reach a public read or a public ranking;
    - GET /v1/ops/availability/quality.

Run:  .venv/bin/python tests/test_capacity_history.py
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("OPENGRID_NO_JOBS", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import scratchdb  # noqa: E402  (first: isolates the process from real providers)
import fixtures  # noqa: E402

import cache  # noqa: E402
import capacity_history as ch  # noqa: E402
import normalize  # noqa: E402
from accounts.auth import OPERATOR, Principal  # noqa: E402
from analytics import rollups  # noqa: E402
from routing import scoring  # noqa: E402
from store.capacity import CapacityChange, CapacityCurrent  # noqa: E402
from tables import ComputeListingRow, ListingObservation  # noqa: E402

DB = "og_test_capacity_history"
T = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
D, H = timedelta(days=1), timedelta(hours=1)
G = "NVIDIA RTX 4090 24GB"
NOW = datetime.now(timezone.utc).replace(microsecond=0)


def close(a, b, tol=0.01):
    return a is not None and abs(a - b) <= tol


def chg(at, in_stock=None, count=None, raw="x"):
    return {"observed_at": at, "in_stock": in_stock, "available_count": count, "raw_value": raw}


# --------------------------------------------------------------------------
# Pure
# --------------------------------------------------------------------------

def test_count_provider_with_closing_row():
    stale = ch.stale_window("lium")
    assert stale == timedelta(minutes=45), stale            # 3 x Lium's 900 s capacity interval
    rows = [chg(T - 31 * D, True, 5), chg(T - 20 * D, False, 0),
            chg(T - 10 * D, None, 2),                      # a count with no in_stock: above zero is in stock
            chg(T - 5 * D, raw=f"{ch.EXPIRED} (last seen {(T - 6 * D).isoformat()})"),
            chg(T - 2 * D, True, 4)]
    segs = ch.segments(rows, provider="lium", current_observed_at=T - H, now=T)
    assert segs == [(T - 31 * D, T - 20 * D, ch.IN), (T - 20 * D, T - 10 * D, ch.OUT),
                    (T - 10 * D, T - 6 * D + stale, ch.IN), (T - 2 * D, T - H + stale, ch.IN)], segs
    s = ch.summarize(segs, T)
    # 30 days: in 240 + 96.75 + 47.75 = 384.5 h, out 240 h, the closing gap and the last 15 min are no data
    d30 = s["30d"]
    assert close(d30["in_stock_hours"], 384.5) and close(d30["out_of_stock_hours"], 240), d30
    assert close(d30["no_data_hours"], 720 - 624.5) and d30["unknown_hours"] == 0, d30
    assert close(d30["in_stock_share"], 384.5 / 624.5, 1e-3), d30            # 0.6157
    assert close(d30["share_in_stock"], 384.5 / 720, 1e-3), d30
    # 7 days: in 24.75 + 47.75 = 72.5 h, nothing out
    assert close(s["7d"]["in_stock_hours"], 72.5) and s["7d"]["in_stock_share"] == 1.0, s["7d"]


def test_tier_provider_unknown_word_and_stale_tail():
    stale = ch.stale_window("runpod")
    assert stale == timedelta(minutes=6), stale              # 3 x 120 s
    rows = [chg(T - 10 * D, True), chg(T - 8 * D, False), chg(T - 7 * D, None), chg(T - 3 * D, True)]
    segs = ch.segments(rows, provider="runpod", current_observed_at=T - 2 * D, now=T)
    assert segs[-1] == (T - 3 * D, T - 2 * D + stale, ch.IN), "a stale row's state ends at last sighting + window"
    d30 = ch.summarize(segs, T)["30d"]
    assert close(d30["in_stock_hours"], 72.1) and close(d30["out_of_stock_hours"], 24), d30
    assert close(d30["unknown_hours"], 96) and close(d30["no_data_hours"], 720 - 192.1), d30
    assert close(d30["in_stock_share"], 72.1 / 96.1, 1e-3), d30                 # 0.7503
    d7 = ch.summarize(segs, T)["7d"]
    assert close(d7["unknown_hours"], 96) and close(d7["known_hours"], 24.1) and d7["in_stock_share"] == 1.0, d7
    # under 24 h of answers there is no share
    short = ch.summarize([(T - 20 * H, T, ch.OUT)], T)["30d"]
    assert short["in_stock_share"] is None and close(short["out_of_stock_hours"], 20), short


def test_no_current_row_ends_at_last_change():
    segs = ch.segments([chg(T - 5 * D, True), chg(T - 3 * D, False)], provider="lium", current_observed_at=None, now=T)
    assert segs == [(T - 5 * D, T - 3 * D, ch.IN)], "a pruned key: nothing says how long its last state held"


def test_state_of_reuses_the_providers_in_stock():
    assert ch.state_of(True) == ch.IN and ch.state_of(False) == ch.OUT and ch.state_of(None) == ch.UNKNOWN
    assert ch.state_of(None, 0) == ch.OUT and ch.state_of(None, 3) == ch.IN
    assert ch.state_of(False, 3) == ch.OUT, "the provider's own in_stock wins (Hyperbolic: disabled with a count)"


def test_combine_any_in_stock():
    a = [(T - 10 * H, T - 5 * H, ch.IN), (T - 5 * H, T, ch.OUT)]
    b = [(T - 8 * H, T - 2 * H, ch.UNKNOWN)]
    c = ch.combine([a, b])
    assert c == [(T - 10 * H, T - 5 * H, ch.IN), (T - 5 * H, T - 2 * H, ch.UNKNOWN), (T - 2 * H, T, ch.OUT)], c
    assert ch.combine([a]) == a and ch.combine([[], []]) == []


def test_join_rules():
    def keys(provider, *pairs, n=1):
        return {(provider, s, r): {"gpu_count": n} for s, r in pairs}

    def join(row, ks):
        got, grain = ch.listing_keys(row, ks)
        return sorted(k[1:] for k in got), grain

    # plain sku and region
    assert join({"provider": "lambda", "sku": "gpu_1x_h100", "listing_id": "gpu_1x_h100:us-east-1", "region": "us-east-1"},
                keys("lambda", ("gpu_1x_h100", "us-east-1"), ("gpu_1x_h100", "us-west-1"))) == \
        ([("gpu_1x_h100", "us-east-1")], "listing")
    # listing_id as sku (RunPod / Verda), listing priced for every location: every location row joins
    assert join({"provider": "verda", "sku": "1H100", "listing_id": "1H100", "region": None},
                keys("verda", ("1H100", "FIN-01"), ("1H100", "ICE-01"), ("1H100:spot", "FIN-01")))[0] == \
        [("1H100", "FIN-01"), ("1H100", "ICE-01")]
    # a listing priced for several regions (Koyeb, DigitalOcean, Scaleway, Hetzner)
    assert join({"provider": "scaleway", "sku": "H100-1", "listing_id": "H100-1", "region": "fr-par-1; fr-par-2"},
                keys("scaleway", ("H100-1", "fr-par-2"), ("H100-1", "nl-ams-1")))[0] == [("H100-1", "fr-par-2")]
    # Shadeform display text cannot be compared with region codes: the sku covers every region
    assert join({"provider": "denvr", "sku": "A100_80G", "listing_id": "x", "region": "Houston, US"},
                keys("denvr", ("A100_80G", "houston-1")))[0] == [("A100_80G", "houston-1")]
    # Jarvislabs: on-demand listings join the workload's row; spot listings do not
    jk = keys("jarvislabs", ("H100:1x:vm", "india-01"), ("H100:1x:container", "india-01"))
    assert join({"provider": "jarvislabs", "sku": "H100:1x", "listing_id": "H100:1x:india-01:vm:on_demand",
                 "region": "india-01", "market_type": "on_demand"}, jk)[0] == [("H100:1x:vm", "india-01")]
    assert join({"provider": "jarvislabs", "sku": "H100:1x", "listing_id": "H100:1x:india-01:vm:spot",
                 "region": "india-01", "market_type": "spot"}, jk) == ([], None)
    # Hyperbolic: the listing id carries the region between sku and machine
    assert join({"provider": "hyperbolic", "sku": "H100 SXM:8x", "listing_id": "H100 SXM:8x:us-central:bm:ib",
                 "region": "us-central"}, keys("hyperbolic", ("H100 SXM:8x:bm:ib", "us-central")))[1] == "listing"
    # Voltage Park bare metal: the location sits in the listing id
    vk = keys("voltagepark", ("H100:bare-metal:ib", "location:7"), ("H100:bare-metal:ib", "location:9"))
    assert join({"provider": "voltagepark", "sku": "H100:bare-metal:ib", "listing_id": "bm:9:ib", "region": None},
                vk)[0] == [("H100:bare-metal:ib", "location:9")]
    # Lium: no city is not provider-wide
    lk = keys("lium", ("8XH100", ""), ("8XH100", "Paris"))
    assert join({"provider": "lium", "sku": "8XH100", "listing_id": "e1", "region": "Paris"}, lk)[0] == [("8XH100", "Paris")]
    # AWS placement scores never join; Massed Compute's Shadeform types join by GPU count
    assert join({"provider": "aws", "sku": "p5.48xlarge", "listing_id": "p5.48xlarge:us-east-1a:spot", "region": "us-east-1a"},
                keys("aws", ("p5.48xlarge:spot", "us-east-1"))) == ([], None)
    mk = {("massedcompute", "H100_80Gx8", "us"): {"gpu_count": 8}, ("massedcompute", "H100_80Gx1", "us"): {"gpu_count": 1}}
    assert join({"provider": "massedcompute", "sku": "H100:8x", "listing_id": "H100:8x", "region": None,
                 "gpu_count": 8}, mk) == ([("H100_80Gx8", "us")], "gpu")
    # another provider's sku never joins at the GPU grain
    assert join({"provider": "lambda", "sku": "other", "listing_id": "o", "region": None, "gpu_count": 1},
                keys("lambda", ("gpu_1x_h100", "us-east-1"))) == ([], None)


# --------------------------------------------------------------------------
# Database: routing and reads
# --------------------------------------------------------------------------
# provider -> (price, listing available, capacity: list of (age, in_stock), current row age or None, visibility)
SPEC = {
    "syn_verda": (1.00, None, [(40 * D, False), (36 * H, True)], timedelta(minutes=1), "public"),  # in stock 5%
    "syn_instock": (1.05, None, [(40 * D, True)], timedelta(minutes=1), "public"),
    "syn_nofeed": (1.02, None, [], None, "public"),
    "syn_out": (0.90, None, [(40 * D, True), (5 * H, False)], timedelta(minutes=1), "public"),
    "syn_staleout": (0.95, None, [(40 * D, False)], 2 * D, "public"),
    "syn_secret": (1.01, None, [(40 * D, False)], timedelta(minutes=1), "internal"),  # internal row, public listing
}


def seed(Session):
    start = NOW - 40 * D
    with Session.begin() as s:
        for prov, (price, avail, caps, cur_age, vis) in SPEC.items():
            lid = f"{prov}:4090"
            s.add(ComputeListingRow(
                provider=prov, listing_id=lid, sku=f"{prov}-4090", raw_gpu_name="RTX 4090", canonical_gpu_name=G,
                gpu_count=1, region="r1", country="US", price_per_gpu_hour=Decimal(str(price)),
                price_per_instance_hour=Decimal(str(price)), currency="USD", market_type="on_demand",
                provider_tier=None, interruptible=False, available=avail, capacity=None, capacity_unit=None,
                vcpu=None, ram_gb=None, storage_gb=None, observed_at=NOW, first_seen_at=start))
            s.flush()
            s.add(ListingObservation(provider=prov, listing_id=lid, observed_at=start,
                                     price_per_gpu_hour=Decimal(str(price)), price_per_instance_hour=Decimal(str(price)),
                                     available=avail))
            for age, stock in caps:
                s.add(CapacityChange(provider=prov, sku=f"{prov}-4090", region_key="r1", canonical_gpu_name=G,
                                     observed_at=NOW - age, in_stock=stock, raw_value=str(stock), visibility=vis))
            if cur_age is not None:
                s.add(CapacityCurrent(provider=prov, sku=f"{prov}-4090", region_key="r1", region="r1", country="US",
                                      raw_gpu_name="RTX 4090", canonical_gpu_name=G, gpu_count=1,
                                      in_stock=caps[-1][1], raw_value=str(caps[-1][1]), source="/syn",
                                      observed_at=NOW - cur_age, first_seen_at=start, changed_at=NOW - caps[-1][0],
                                      visibility=vis))
    rollups.refresh()
    cache.clear_all()


def test_book_shares_and_tiers():
    pub = ch.load(G, "public", now=NOW)
    assert ("syn_secret", "syn_secret-4090", "r1") not in pub.keys, "a public book never reads an internal row"
    allt = ch.load(G, "all", now=NOW)
    assert ("syn_secret", "syn_secret-4090", "r1") in allt.keys
    v = pub.history(list(pub.provider_keys("syn_verda", G)))["30d"]
    assert close(v["in_stock_share"], 36 / 720 * (720 / 720), 0.002), v      # 36 h of 720: 5%
    assert close(pub.history(list(pub.provider_keys("syn_staleout", G)))["30d"]["known_hours"], 720 - 48 + 0.75), \
        "a stale row: its tail past last sighting + 45 min is no data"


def test_verda_case_ranks_below_in_stock():
    r = scoring.rank_listings(G, mode="BALANCED", now=NOW, limit=50)
    by = {c["provider"]: c for c in r["candidates"]}
    v, ok = by["syn_verda"], by["syn_instock"]
    pv, po = v["factors"]["availability_persistence"], ok["factors"]["availability_persistence"]
    assert pv["kind"] == "observed" and close(pv["value"], 0.05, 0.002), pv
    assert "in stock 5% of last 30 days (provider stock feed" in pv["note"], pv["note"]
    assert pv["raw"]["source"] == "capacity feed" and pv["raw"]["grain"] == "listing", pv["raw"]
    assert po["value"] == 1.0 and "in stock 100% of last 30 days" in po["note"]
    assert ok["score"] > v["score"] and ok["rank"] < v["rank"], (ok["score"], v["score"])
    assert v["factors"]["availability_now"]["value"] == 1.0 and \
        v["factors"]["availability_now"]["note"] == "in stock now (provider stock feed)"
    assert v["stock"] == "in_stock" and v["stock_source"] == "capacity feed" and close(v["in_stock_share_30d"], 0.05, 0.002)
    # before this change both scored persistence from listed prices (100% each): Verda was 5% cheaper and ranked first
    ms = scoring.rank_listings(G, mode="MOST_STABLE", now=NOW, limit=50)
    order = [c["provider"] for c in ms["candidates"]]
    assert order.index("syn_instock") < order.index("syn_verda"), order


def test_fallback_is_labelled_and_half_credit():
    r = scoring.rank_listings(G, mode="BALANCED", now=NOW, limit=50)
    f = {c["provider"]: c for c in r["candidates"]}["syn_nofeed"]["factors"]["availability_persistence"]
    assert f["kind"] == "inferred" and f["raw"]["source"] == "listed prices (market_hourly)", f
    assert "price listed 100% of last 30 days; no stock feed" in f["note"], f["note"]
    assert close(f["value"], 0.5, 0.01), "priced every hour, stock never stated: half credit"
    assert {c["provider"]: c for c in r["candidates"]}["syn_nofeed"]["stock"] == "unknown"


def test_out_of_stock_feed_excludes_and_stale_does_not():
    r = scoring.rank_listings(G, mode="BALANCED", now=NOW, limit=50)
    codes = {e["provider"]: e for e in r["exclusions"]}
    assert codes["syn_out"]["code"] == "sold_out_stock_feed" and "stock feed" in codes["syn_out"]["reason"], codes
    names = {c["provider"] for c in r["candidates"]}
    assert "syn_staleout" in names, "a stale feed row never excludes"
    st = {c["provider"]: c for c in r["candidates"]}["syn_staleout"]
    assert st["factors"]["availability_now"]["value"] == 0.5 and "stale" in st["factors"]["availability_now"]["note"]
    assert st["stock"] == "unknown" and st["stock_rows_stale"] == 1
    # the internal row is ignored on a public ranking and used on an internal one
    assert "syn_secret" in names and {c["provider"]: c for c in r["candidates"]}["syn_secret"]["stock"] == "unknown"
    allt = scoring.rank_listings(G, mode="BALANCED", now=NOW, limit=50, tier="all")
    assert {e["provider"]: e["code"] for e in allt["exclusions"]}.get("syn_secret") == "sold_out_stock_feed"
    # explicit availability required: the stock feed counts as explicit
    req = scoring.rank_listings(G, mode="BALANCED", now=NOW, limit=50, require_available=True)
    assert {c["provider"] for c in req["candidates"]} == {"syn_verda", "syn_instock"}, req["candidates"]


def _client(who):
    import main
    from accounts.auth import principal
    from fastapi.testclient import TestClient
    main.app.dependency_overrides[principal] = lambda: who
    return main, TestClient(main.app, headers={"X-OpenGrid-Request": "1"})


def test_listing_read_and_ops_read():
    main, c = _client(Principal(kind="api_key", account_id=7, scopes=frozenset({"data:read"})))
    try:
        r = c.get("/v1/listings?gpu=rtx-4090-24gb&limit=50")
        assert r.status_code == 200, r.text
        by = {x["provider"]: x for x in r.json()["data"]}
        assert by["syn_verda"]["stock"] == "in_stock" and by["syn_verda"]["stock_source"] == "capacity feed"
        assert by["syn_verda"]["available"] is None, "compute_listings.available is never overwritten"
        assert close(by["syn_verda"]["in_stock_share_30d"], 0.05, 0.002) and by["syn_verda"]["stock_observed_at"]
        assert by["syn_out"]["stock"] == "out_of_stock" and by["syn_nofeed"]["stock"] == "unknown"
        assert by["syn_nofeed"]["stock_feed"] is False and by["syn_staleout"]["stock"] == "unknown"
        s = by["syn_secret"]
        assert s["stock"] == "unknown" and s["stock_feed"] is False and s["in_stock_share_30d"] is None, \
            "an internal capacity row never reaches a public read"
        assert c.get("/v1/ops/availability/quality").status_code == 403
        main.app.dependency_overrides.clear()
        main, c = _client(OPERATOR)
        r = c.get("/v1/ops/availability/quality")
        assert r.status_code == 200, r.text
        rows = {x["provider"]: x for x in r.json()["data"]}
        v = rows["syn_verda"]
        assert v["stock_feed"] and v["listings_live"] == 1 and v["listings_joined"] == 1 and v["listings_joined_share"] == 1
        assert close(v["in_stock_share_30d"], 0.05, 0.002) and v["gpus"][0]["gpu"] == G and v["rows_stale"] == 0
        assert rows["syn_staleout"]["rows_stale"] == 1 and rows["syn_nofeed"]["stock_feed"] is False
        assert rows["syn_nofeed"]["listings_live"] == 1 and rows["syn_nofeed"]["listings_joined"] == 0
    finally:
        main.app.dependency_overrides.clear()


def main():
    pure = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)
            and n in ("test_count_provider_with_closing_row", "test_tier_provider_unknown_word_and_stale_tail",
                      "test_no_current_row_ends_at_last_change", "test_state_of_reuses_the_providers_in_stock",
                      "test_combine_any_in_stock", "test_join_rules")]
    db = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f) and (n, f) not in pure]
    url = scratchdb.create(DB)
    Session = fixtures.session(url)
    normalize.SessionLocal = Session
    failed = 0
    try:
        seed(Session)
        for name, fn in pure + db:
            try:
                fn()
                print(f"ok   {name}")
            except Exception as exc:  # noqa: BLE001
                failed += 1
                import traceback
                traceback.print_exc()
                print(f"FAIL {name}: {exc}")
    finally:
        Session.kw["bind"].dispose()
        scratchdb.drop(DB)
    print(f"\n{len(pure) + len(db) - failed}/{len(pure) + len(db)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
