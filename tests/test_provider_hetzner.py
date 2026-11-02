"""Hetzner: dedicated GEX GPU servers from its public server matrix, EUR converted at the ECB rate.

Parser runs on the REAL page (tests/fixtures_providers/hetzner_matrix_gpu.html,
hetzner.com/dedicated-rootserver/matrix-gpu/ fetched 2026-11-01 with an anonymous GET, trimmed to the server cards
and their details panels). No network, no database (fx.rate_at is stubbed).

Run:  .venv/bin/python tests/test_provider_hetzner.py
"""

import asyncio
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
from providers.hetzner import ENDPOINT, HetznerProvider, extract  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_providers" / "hetzner_matrix_gpu.html"
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

    def handler(request):
        assert request.url.path == ENDPOINT
        return httpx.Response(200, text=page(), headers={"content-type": "text/html"})
    raws = asyncio.run(HetznerProvider(httpx.AsyncClient(base_url="https://www.hetzner.com",
                                                         transport=httpx.MockTransport(handler))).fetch())
    assert [r.endpoint for r in raws] == [ENDPOINT] and raws[0].ok and raws[0].payload == extract(page())


def test_registry():
    assert PROVIDERS["hetzner"] is HetznerProvider and HetznerProvider.polling.interval_seconds == 3600
    assert "HetznerProvider" in {type(f).__name__ for f in fetch_mod.build_fetchers()}
    assert "hetzner" in mapping.PROVIDER_MAPPINGS and mapping.check_mapping_coverage() == {}
    m = provider_meta.meta("hetzner")
    assert (m.provider_class, m.source_type, m.source) == ("general_cloud", "public_page", "www.hetzner.com")


if __name__ == "__main__":
    for t in (test_extract, test_normalize, test_failures, test_registry):
        t()
        print(t.__name__, "ok")
