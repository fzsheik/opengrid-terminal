"""Hetzner: dedicated GEX GPU servers from its public server matrix, EUR converted at the ECB rate.

Parser runs on the REAL page (tests/fixtures_providers/hetzner_matrix_gpu.html,
hetzner.com/dedicated-rootserver/matrix-gpu/ fetched 2026-11-01 with an anonymous GET, trimmed to the server cards
and their details panels). No network, no database (fx.rate_at is stubbed).

Run:  .venv/bin/python tests/test_provider_hetzner.py
"""

import asyncio
import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("OPENGRID_NO_JOBS", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

import fetch as fetch_mod  # noqa: E402
import fx  # noqa: E402
import mapping  # noqa: E402
import provider_meta  # noqa: E402
from providers import PROVIDERS  # noqa: E402
from providers.hetzner import ENDPOINT, LIVE, HetznerProvider, extract  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_providers" / "hetzner_matrix_gpu.html"
LIVE_FIX = FIX.with_name("hetzner_live_data_upfront.json")
NOW = datetime(2026, 11, 1, 17, tzinfo=timezone.utc)


def page():
    return FIX.read_text(encoding="utf-8")


def snap(payload):
    return SimpleNamespace(payload=payload, request=None, fetched_at=NOW)


@contextmanager
def ecb_rate(rate="1.1500", day=None):
    real = fx.rate_at
    fixed = None if rate is None else fx.Rate("EUR", "USD", D(rate), day or (NOW - timedelta(days=1)).date())
    fx.rate_at = lambda at, base="EUR", quote="USD": fx.pick([fixed] if fixed else [], at)
    try:
        yield
    finally:
        fx.rate_at = real


def raises(fn, *a):
    try:
        fn(*a)
    except ValueError:
        return True
    return False


def test_extract():
    cards = extract(page())["cards"]
    assert [c["name"] for c in cards] == ["GEX45", "GEX63", "GEX131", "New GEX430"]
    assert cards[0]["prices"] == ["€ 214.00 per month, excl. VAT", "€ 0.3429 per hour, excl. VAT",
                                  "€ 209.00 one-time setup fee, excl. VAT"]
    assert cards[2]["monthly_label"] == "from monthly" and cards[2]["locations"] == [["HEL1", "FI"], ["FSN1", "DE"]]


def test_normalize():
    with ecb_rate("1.1500"):
        out = {l.listing_id: l for l in HetznerProvider.normalize({ENDPOINT: [snap(extract(page()))]})}
    assert sorted(out) == ["gex131", "gex430", "gex45", "gex63"]
    a = out["gex45"]
    assert (a.gpu_count, a.raw_gpu_name, a.canonical_gpu_name) == (1, "NVIDIA RTX PRO 4000 Blackwell SFF Edition 24GB", "NVIDIA RTX PRO 4000 Blackwell SFF 24GB")
    assert a.original_price_per_instance_hour == D("0.3429") and a.price_per_instance_hour == D("0.394335")
    assert (a.region, a.country, a.ram_gb, a.provider_tier, a.fx_source) == ("HEL1", "FI", 64.0, "dedicated", "ecb")
    q = out["gex430"]
    assert q.gpu_count == 4 and q.sku == "GEX430" and q.canonical_gpu_name == "NVIDIA RTX PRO 6000 Blackwell 96GB Max-Q"
    assert q.original_price_per_gpu_hour == D("1.441900") and q.price_per_gpu_hour == D("1.658185")
    g = out["gex131"]
    assert (g.region, g.country, g.ram_gb, g.original_price_per_instance_hour) == ("HEL1; FSN1", None, 256.0, D("2.4022"))
    assert all(l.currency == "USD" and l.available is None and l.vcpu is None for l in out.values())


def test_failures():
    with ecb_rate(None):
        assert HetznerProvider.normalize({ENDPOINT: [snap(extract(page()))]}) == [], "no ECB rate: fail closed"
    assert raises(extract, "<html>no cards</html>")
    assert raises(HetznerProvider.normalize, {ENDPOINT: [snap({"rows": []})]})
    p = extract(page())
    p["cards"][1]["specs"] = ["20 Cores", "128 GB DDR5 RAM"]  # GPU line gone
    with ecb_rate():
        assert "gex63" not in {l.listing_id for l in HetznerProvider.normalize({ENDPOINT: [snap(p)]})}
    for c in p["cards"]:
        c["prices"] = ["€ 999.00 per month, excl. VAT", "price on request"]
    with ecb_rate():
        assert raises(HetznerProvider.normalize, {ENDPOINT: [snap(p)]}), "nothing readable is a failure"
    assert HetznerProvider.normalize({}) == []

    raws = asyncio.run(prov().fetch())
    assert [r.endpoint for r in raws] == [ENDPOINT, LIVE] and raws[0].ok and raws[0].payload == extract(page())


def prov(live_status=200, live_text=None):
    def handler(request):
        if request.url.path == LIVE:
            return httpx.Response(live_status, text=LIVE_FIX.read_text() if live_text is None else live_text,
                                  headers={"content-type": "application/json"})
        assert request.url.path == ENDPOINT
        return httpx.Response(200, text=page(), headers={"content-type": "text/html"})
    return HetznerProvider(httpx.AsyncClient(base_url="https://www.hetzner.com", transport=httpx.MockTransport(handler)))


def cap(cards, live):
    out = HetznerProvider.normalize_capacity({ENDPOINT: [snap(cards)], LIVE: [snap(live)]})
    return {(o.sku, o.region): o for o in out}


def test_capacity():
    """hetzner_live_data_upfront.json: the whole file as served on 2026-11-15 (783 bytes, untrimmed)."""
    raws = asyncio.run(prov().fetch())
    live = json.loads(LIVE_FIX.read_text())
    assert raws[1].ok and raws[1].payload == live, "stored untouched"
    cards = raws[0].payload
    assert [c["product_key"] for c in cards["cards"]] == [
        "ROBOT_1768+ROBOT_1266", "ROBOT_1767+ROBOT_1266", "ROBOT_1747+ROBOT_1266", "ROBOT_1769+ROBOT_1266"]
    out = cap(cards, live)
    # The file: 1768, 1767 and 1769 -> ["HEL1"]; 1747 -> ["FSN1", "HEL1"]. One row per location a card lists.
    assert {k: v.in_stock for k, v in out.items()} == {
        ("GEX45", "HEL1"): True, ("GEX63", "HEL1"): True, ("GEX131", "HEL1"): True, ("GEX131", "FSN1"): True,
        ("GEX430", "HEL1"): True}
    a = out[("GEX131", "FSN1")]
    assert (a.country, a.gpu_count, a.source, a.raw_value) == ("DE", 1, LIVE, "product 1747 orderable in FSN1, HEL1")
    assert a.tier is None and a.available_count is None and a.total_count is None and a.count_unit is None, "a boolean"
    assert out[("GEX430", "HEL1")].gpu_count == 4
    assert out[("GEX430", "HEL1")].canonical_gpu_name == "NVIDIA RTX PRO 6000 Blackwell 96GB Max-Q"

    # A location the card lists but the file does not: out of stock there, in stock elsewhere.
    gone = cap(cards, {**live, "1747": ["HEL1", "NBG1"]})
    assert gone[("GEX131", "FSN1")].in_stock is False and gone[("GEX131", "HEL1")].in_stock is True
    assert ("GEX131", "NBG1") not in gone, "a datacenter the card does not offer gets no row"
    assert cap(cards, {**live, "1768": []})[("GEX45", "HEL1")].in_stock is False
    # A product the file does not name: unknown, never in stock and never sold out.
    missing = cap(cards, {k: v for k, v in live.items() if k != "1769"})[("GEX430", "HEL1")]
    assert missing.in_stock is None and missing.raw_value == "product 1769 not in the availability file"
    # Whole product numbers only: 17690 is not 1769.
    assert cap(cards, {**{k: v for k, v in live.items() if k != "1769"}, "17690": ["HEL1"]})[("GEX430", "HEL1")].in_stock is None
    assert cap(cards, {**{k: v for k, v in live.items() if k != "1769"}, "1769+1266": ["HEL1"]})[("GEX430", "HEL1")].in_stock is True

    # A card without its product id (stored before it was kept) has no row.
    old = json.loads(json.dumps(cards))
    old["cards"][0].pop("product_key")
    assert ("GEX45", "HEL1") not in cap(old, live) and len(cap(old, live)) == 4

    # Reshaped answers raise; none reads as "nothing in stock".
    for bad in ({}, [], {"1747": "HEL1"}, {"1747": [1, 2]}, {"error": {"code": 500}}):
        assert raises(cap, cards, bad), bad
    assert raises(cap, {"rows": []}, live)
    assert HetznerProvider.normalize_capacity({ENDPOINT: [snap(cards)]}) == [], "no file: no rows"
    assert HetznerProvider.normalize_capacity({}) == []


def test_capacity_failure_leaves_prices():
    raws = asyncio.run(prov(live_status=503, live_text="down").fetch())
    assert raws[0].ok and not raws[1].ok and raws[1].endpoint == LIVE
    by = {r.endpoint: [snap(r.payload)] for r in raws if r.ok}
    with ecb_rate():
        assert len(HetznerProvider.normalize(by)) == 4
    assert HetznerProvider.normalize_capacity(by) == []
    raws = asyncio.run(prov(live_text="{}").fetch())
    by = {r.endpoint: [snap(r.payload)] for r in raws if r.ok}
    with ecb_rate():
        assert len(HetznerProvider.normalize(by)) == 4 and raises(HetznerProvider.normalize_capacity, by)


def test_registry():
    assert PROVIDERS["hetzner"] is HetznerProvider and HetznerProvider.polling.interval_seconds == 3600
    assert "HetznerProvider" in {type(f).__name__ for f in fetch_mod.build_fetchers()}
    assert "hetzner" in mapping.PROVIDER_MAPPINGS and mapping.check_mapping_coverage() == {}
    m = provider_meta.meta("hetzner")
    assert (m.provider_class, m.source_type, m.source) == ("general_cloud", "public_page", "www.hetzner.com")


if __name__ == "__main__":
    for t in (test_extract, test_normalize, test_failures, test_capacity, test_capacity_failure_leaves_prices,
              test_registry):
        t()
        print(t.__name__, "ok")
