"""Archived price history (backfill/, analytics/archived.py; methodology/archived-history.md).

Run:  .venv/bin/python tests/test_backfill.py
"""

import io
import sys
import tempfile
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import scratchdb  # noqa: E402  (first: isolates the process from real networks)
import httpx  # noqa: E402

import fixtures  # noqa: E402
import normalize  # noqa: E402
from analytics import archived  # noqa: E402
from backfill import aws_price_list, load, wayback
from backfill.parsers import coreweave as cw_archive  # noqa: E402
from backfill.sources import Source, live_page, sources  # noqa: E402
from models import ComputeListing  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_backfill"
PROV_FIX = Path(__file__).resolve().parent / "fixtures_providers"
AT = datetime(2026, 1, 18, 10, 55, tzinfo=timezone.utc)
DB = "og_test_backfill"


def _mock_wayback(handler, cache):
    return wayback.Wayback(cache_dir=cache, client=httpx.Client(transport=httpx.MockTransport(handler)),
                           min_interval=0, log=lambda m: None)


def test_wayback_captures_body_cache_and_backoff():
    calls = []
    cdx = [["original", "timestamp", "mimetype", "digest"],
           ["https://x.test/pricing", "20260101000000", "text/html", "AAA"],
           ["https://x.test/pricing", "20260108120000", "text/html", "BBB"]]

    def handler(req):
        calls.append(str(req.url))
        if req.url.path.startswith("/cdx"):
            assert req.url.params["filter"] == "statuscode:200" and req.url.params["collapse"] == "timestamp:8"
            return httpx.Response(200, json=cdx)
        if len([c for c in calls if "id_" in c]) == 1:
            return httpx.Response(429)          # first body request is rate-limited, then succeeds
        return httpx.Response(200, content=b"<html>ok</html>")

    real_sleep = wayback.time.sleep
    wayback.time.sleep = lambda s: None
    try:
        with tempfile.TemporaryDirectory() as d:
            w = _mock_wayback(handler, d)
            caps = w.captures("x.test/pricing", "20251021", "20261021")
            assert [c.timestamp for c in caps] == ["20260101000000", "20260108120000"]
            assert caps[1].captured_at == datetime(2026, 1, 8, 12, tzinfo=timezone.utc)
            assert caps[0].raw_url == "https://web.archive.org/web/20260101000000id_/https://x.test/pricing"
            assert caps[0].archive_url == "https://web.archive.org/web/20260101000000/https://x.test/pricing"
            assert w.body(caps[0]) == b"<html>ok</html>"
            n = len(calls)
            assert w.body(caps[0]) == b"<html>ok</html>" and len(calls) == n, "second read comes from the cache"
            w.captures("x.test/pricing", "20251021", "20261021")
            assert len(calls) == n, "the capture list is cached too"
    finally:
        wayback.time.sleep = real_sleep


def test_source_tries_parsers_in_order_and_reports_every_reason():
    def bad(body, at):
        raise ValueError("page reshaped")

    def empty(body, at):
        return []

    def good(body, at):
        return cw_archive.coreweave_single_table(body.decode(), at)

    s = Source("coreweave", "u", (("live", bad), ("empty", empty), ("old", good)))
    listings, used = s.parse((FIX / "coreweave_2026-01.html").read_bytes(), AT)
    assert used == "old" and listings
    try:
        Source("coreweave", "u", (("live", bad), ("empty", empty))).parse(b"", AT)
        raise AssertionError("no parser read it: must raise")
    except ValueError as exc:
        assert "live: ValueError: page reshaped" in str(exc) and "empty: no listings" in str(exc)


def test_coreweave_older_designs():
    for name in ("coreweave_2025-11.html", "coreweave_2026-01.html"):
        ls = cw_archive.coreweave_single_table((FIX / name).read_text(), AT)
        by = {l.raw_gpu_name: l for l in ls}
        h100 = by["NVIDIA HGX H100 80GB"]
        assert h100.gpu_count == 8 and h100.price_per_instance_hour == D("49.24"), name
        assert h100.canonical_gpu_name == "NVIDIA H100 80GB SXM5", "same canonical mapping as the live parser"
        assert h100.price_per_gpu_hour == D("6.155") and h100.market_type == "on_demand" and h100.observed_at == AT
        assert all(l.price_per_instance_hour > 0 for l in ls)
        # "Contact sales" rows carry no price and produce no listing
        assert "NVIDIA GB300 NVL72 279GB" not in by, name
        # GB200 has a canonical model since 2026-11-02 (one Blackwell GPU of the superchip)
        assert by["NVIDIA GB200 NVL72 186GB"].canonical_gpu_name == "NVIDIA GB200 186GB"


def test_coreweave_live_parser_still_reads_current_design_via_source():
    src = next(s for s in sources() if s.provider == "coreweave")
    page = next(PROV_FIX.glob("coreweave*"), None)
    if page is None:
        print("  (no live coreweave fixture; skipped)")
        return
    listings, used = src.parse(page.read_bytes(), AT)
    assert used.startswith("providers.coreweave") and listings and all(l.observed_at == AT for l in listings)


_CSV_HEAD = ('"FormatVersion","v1.0"\n"Disclaimer","x"\n"Publication Date","2026-03-26T15:44:55Z"\n'
             '"Version","20260326154455"\n"OfferCode","AmazonEC2"\n')
_COLS = ["SKU", "RateCode", "TermType", "Unit", "PricePerUnit", "Location", "Instance Type", "vCPU", "Memory",
         "Tenancy", "Operating System", "License Model", "Pre Installed S/W", "CapacityStatus", "EffectiveDate"]


def _csv(rows):
    out = io.StringIO()
    out.write(_CSV_HEAD + ",".join(f'"{c}"' for c in _COLS) + "\n")
    base = {"TermType": "OnDemand", "Unit": "Hrs", "Location": "US East (N. Virginia)", "Tenancy": "Shared",
            "Operating System": "Linux", "License Model": "No License required", "Pre Installed S/W": "NA",
            "CapacityStatus": "Used", "vCPU": "192", "Memory": "2048 GiB", "EffectiveDate": "2026-03-01"}
    for r in rows:
        r = {**base, **r}
        out.write(",".join(f'"{r.get(c, "")}"' for c in _COLS) + "\n")
    return out.getvalue().splitlines(keepends=True)


def test_aws_price_list_filter_and_listings():
    rows = aws_price_list.parse_csv(_csv([
        {"SKU": "A", "Instance Type": "p5.48xlarge", "PricePerUnit": "55.04"},
        {"SKU": "B", "Instance Type": "p5.48xlarge", "PricePerUnit": "60.00", "Operating System": "Windows"},
        {"SKU": "C", "Instance Type": "p5.48xlarge", "PricePerUnit": "70.00", "Tenancy": "Dedicated"},
        {"SKU": "D", "Instance Type": "p5.48xlarge", "PricePerUnit": "0.00", "CapacityStatus": "UnusedCapacityReservation"},
        {"SKU": "E", "Instance Type": "c5.large", "PricePerUnit": "0.085"},
        {"SKU": "F", "Instance Type": "g6.xlarge", "PricePerUnit": "0.8048"},
        {"SKU": "G", "Instance Type": "g6.xlarge", "PricePerUnit": "0.9000"},   # two prices for one type
    ]))
    assert sorted(r["SKU"] for r in rows) == ["A", "F", "G"], rows
    v = aws_price_list.Version("20260326154455", datetime(2026, 3, 1, tzinfo=timezone.utc),
                               datetime(2026, 4, 1, tzinfo=timezone.utc))
    ls, problems = aws_price_list.listings(v, rows)
    assert [l.sku for l in ls] == ["p5.48xlarge"] and ls[0].price_per_gpu_hour == D("6.88")
    assert ls[0].observed_at == v.begin and ls[0].canonical_gpu_name and ls[0].ram_gb == 2048.0
    assert problems and "g6.xlarge" in problems[0], "an ambiguous type is skipped and reported, not guessed"


def _listing(provider, gpu_raw, price, at, lid="l1", count=1):
    import canonical
    return ComputeListing(provider=provider, sku=lid, listing_id=lid, raw_gpu_name=gpu_raw,
                          canonical_gpu_name=canonical.canonical_gpu_name(gpu_raw), gpu_count=count, region=None,
                          country=None, price_per_gpu_hour=D(price) / count, price_per_instance_hour=D(price),
                          market_type="on_demand", provider_tier=None, interruptible=False, available=None,
                          capacity=None, capacity_unit=None, vcpu=None, ram_gb=None, storage_gb=None, observed_at=at)


def test_store_is_idempotent_bounded_and_monthly_votes():
    url = scratchdb.create(DB)
    try:
        normalize.SessionLocal = fixtures.session(url)
        d = lambda m, day: datetime(2026, m, day, tzinfo=timezone.utc)  # noqa: E731
        gpu_raw = "NVIDIA HGX H100 80GB"
        docs = [("coreweave", d(1, 5), "6.0"), ("coreweave", d(1, 20), "5.5"), ("coreweave", d(2, 3), "5.5"),
                ("lambda", d(1, 9), "2.99")]
        for prov, at, price in docs:
            rows, rejected = load._rows([_listing(prov, gpu_raw, price, at)], source="wayback",
                                        source_url=f"https://web.archive.org/web/{at:%Y%m%d}/{prov}", sha256="0" * 64,
                                        parser="test")
            assert not rejected and load.store(rows) == 1
            assert load.store(rows) == 0, "re-import stores nothing twice"
        rows, rejected = load._rows([_listing("x", gpu_raw, "999", d(1, 1)), _listing("x", gpu_raw, "0", d(1, 1), "l2")],
                                    source="wayback", source_url="u", sha256="0" * 64, parser="t")
        assert not rows and len(rejected) == 2, "out-of-bounds prices are rejected, never stored"

        a = archived.gpu_archived("NVIDIA H100 80GB SXM5")
        assert a["kind"] == "archived" and len(a["points"]) == 4
        jan, feb = a["monthly"]
        assert jan["month"] == "2026-01" and jan["by_provider"] == {"coreweave": 5.5, "lambda": 2.99}
        assert jan["lowest"] == 2.99 and jan["providers"] == 2 and jan["median"] == (5.5 + 2.99) / 2
        assert feb["providers"] == 1 and feb["lowest"] == 5.5, "a provider not captured that month does not vote"
        cw = next(p for p in a["providers"] if p["provider"] == "coreweave")
        assert cw["samples"] == 3 and cw["lowest"] == 5.5 and cw["highest"] == 6.0
        assert all(p["source_url"].startswith("https://web.archive.org/") for p in a["points"])

        # extended(): archived days hold each capture until the next (<= HOLD_DAYS), observed days take over
        from sqlalchemy import text as sqltext
        with normalize.SessionLocal.begin() as s:
            s.execute(sqltext("INSERT INTO market_hourly (segment, gpu, provider, region, hour, min_price, max_price, "
                              "avg_price, live_listings, priced_listings, available_listings, unknown_listings, "
                              "sold_out_listings) VALUES ('on_demand', 'NVIDIA H100 80GB SXM5', 'vast', '', :h, 1.5, "
                              "1.5, 1.5, 1, 1, 1, 0, 0)"), {"h": d(2, 10)})
        x = archived.extended("NVIDIA H100 80GB SXM5", days=None)
        days = [t[:10] for t in x["days"]]
        assert x["archived_until"].startswith("2026-02-10") and days[0] == "2026-01-05" and days[-1] == "2026-02-10"
        at = lambda iso: days.index(iso)  # noqa: E731
        cw = next(p for p in x["providers"] if p["provider"] == "coreweave")
        assert cw["values"][at("2026-01-05")] == 6.0 and cw["values"][at("2026-01-19")] == 6.0, "held until next capture"
        assert cw["values"][at("2026-01-20")] == 5.5 and cw["captures"][cw["capture"][at("2026-01-20")]]["url"].endswith("coreweave")
        assert cw["values"][at("2026-02-10")] is None, "from the first observed day, archived data is not used"
        assert x["kind"][at("2026-02-09")] == "archived" and x["kind"][at("2026-02-10")] == "observed"
        assert x["market"]["lowest"][at("2026-01-10")] == 2.99 and x["market"]["providers"][at("2026-01-10")] == 2
        assert x["market"]["lowest"][at("2026-02-10")] == 1.5 and x["market"]["providers"][at("2026-02-10")] == 1
        lam = next(p for p in x["providers"] if p["provider"] == "lambda")
        assert lam["values"][at("2026-01-09")] == 2.99 and lam["values"][at("2026-02-09")] == 2.99, "held <= HOLD_DAYS"
        with normalize.SessionLocal.begin() as s:
            s.execute(sqltext("DELETE FROM market_hourly"))

        # archived rows never reach the observed rollup
        from sqlalchemy import text
        with normalize.SessionLocal() as s:
            assert s.execute(text("SELECT count(*) FROM market_hourly")).scalar() == 0
    finally:
        scratchdb.drop(DB)


def test_every_source_names_a_known_provider_and_parser():
    from providers import PROVIDERS
    srcs = sources()
    assert len({(s.provider, s.url) for s in srcs}) == len(srcs), "no duplicate sources"
    for s in srcs:
        assert s.provider in PROVIDERS, s.provider
        assert s.parsers and all(name for name, _ in s.parsers)
    assert isinstance(live_page("coreweave", "coreweave", "u"), Source)


if __name__ == "__main__":
    for t in (test_wayback_captures_body_cache_and_backoff, test_source_tries_parsers_in_order_and_reports_every_reason,
              test_coreweave_older_designs, test_coreweave_live_parser_still_reads_current_design_via_source,
              test_aws_price_list_filter_and_listings, test_store_is_idempotent_bounded_and_monthly_votes,
              test_every_source_names_a_known_provider_and_parser):
        t()
        print(t.__name__, "ok")
