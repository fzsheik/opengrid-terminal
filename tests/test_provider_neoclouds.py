"""Arkane Cloud, VESSL AI and Mithril (formerly ML Foundry): neocloud price pages.

Parsers run on trimmed copies of the REAL pages (tests/fixtures_providers/,
fetched 2026-10-21 with unauthenticated GETs). The last test pushes all three
through raw storage, normalize + quality screening, the hourly rollup and the
index vote in a scratch database.

Run:  .venv/bin/python tests/test_provider_neoclouds.py
"""

import asyncio
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
from providers import arkane, mithril, vessl  # noqa: E402
from providers.arkane import ArkaneProvider  # noqa: E402
from providers.mithril import MithrilProvider  # noqa: E402
from providers.vessl import VesslProvider  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_providers"
NOW = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
NEW = ["arkane", "vessl", "mithril"]


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


def _serve(routes: dict[str, str]):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.raw_path.decode()
        for prefix, body in routes.items():
            if path.startswith(prefix):
                return httpx.Response(200, text=body, headers={"content-type": "text/html"})
        return httpx.Response(404, text="not found")
    return httpx.MockTransport(handler)


def _client(base, transport):
    return httpx.AsyncClient(base_url=base, transport=transport)


def _fetch(prov):
    return asyncio.run(prov.fetch())


def arkane_prov():
    return ArkaneProvider(_client("https://arkanecloud.com", _serve({arkane.ENDPOINT: text("arkane_pricing.html")})))


def vessl_prov():
    return VesslProvider(_client("https://vessl.ai", _serve({vessl.ENDPOINT: text("vessl_pricing.html")})))


def mithril_prov():
    return MithrilProvider(_client("https://mithril.ai", _serve({mithril.ENDPOINT: text("mithril_pricing.html")})))


# --------------------------------------------------------------------------
# Arkane Cloud
# --------------------------------------------------------------------------


def test_arkane():
    raws = _fetch(arkane_prov())
    assert len(raws) == 1 and raws[0].ok and raws[0].endpoint == arkane.ENDPOINT
    rows = raws[0].payload["rows"]
    assert len(rows) == 9, "serverless rows are not read"
    out = by_id(ArkaneProvider.normalize({arkane.ENDPOINT: [snap(raws[0].payload)]}))
    assert len(out) == 9
    b300 = out["nvidia-b300-288gb:1x"]
    assert b300.raw_gpu_name == "NVIDIA B300 288GB" and b300.canonical_gpu_name == "NVIDIA B300 288GB SXM"
    assert b300.gpu_count == 1 and b300.price_per_gpu_hour == b300.price_per_instance_hour == D("8.99")
    assert b300.vcpu == 30 and b300.ram_gb == 275 and b300.market_type == "on_demand" and not b300.interruptible
    assert b300.available is None and b300.currency == "USD"
    assert out["nvidia-h200-141gb:1x"].canonical_gpu_name == "NVIDIA H200 141GB SXM5"
    assert out["nvidia-h200-141gb:1x"].price_per_gpu_hour == D("5.99")
    assert out["rtx-6000-ada-48gb:1x"].canonical_gpu_name == "NVIDIA RTX 6000 Ada 48GB"
    assert out["rtx-a6000-48gb:1x"].price_per_gpu_hour == D("0.99")
    h100 = out["nvidia-h100-80gb:1x"]
    assert h100.price_per_gpu_hour == D("3.99") and h100.canonical_gpu_name is None, "SXM or PCIe not stated"
    assert out["rtx-pro-6000-96gb:1x"].canonical_gpu_name is None, "edition not stated"

    # A row whose price is not "$x.xx/hr" (e.g. "Contact sales") gives no listing; others are kept.
    body = {"rows": [dict(rows[0], price="Contact sales")] + rows[1:]}
    out = by_id(ArkaneProvider.normalize({arkane.ENDPOINT: [snap(body)]}))
    assert len(out) == 8 and "nvidia-b300-288gb:1x" not in out

    # Reshaped page: fail closed.
    page = text("arkane_pricing.html")
    assert raises(arkane.extract, page.replace("Prices in USD.", "Prices in EUR."))
    assert raises(arkane.extract, page.replace("Cloud GPU pricing (per hour", "GPU pricing (per hour"))
    assert raises(arkane.extract, page.replace('data-row-title="Price per GPU"', 'data-row-title="Price"'))
    assert raises(arkane.extract, page.replace("data-column-title=", "data-col="))


# --------------------------------------------------------------------------
# VESSL AI
# --------------------------------------------------------------------------


def test_vessl():
    raws = _fetch(vessl_prov())
    assert len(raws) == 1 and raws[0].ok
    rows = raws[0].payload["rows"]
    assert [r["gpu"] for r in rows] == ["NVIDIA H100 SXM", "NVIDIA A100 SXM", "NVIDIA L40S"]
    assert not any(r.get("conflict") for r in rows), "desktop and mobile copies agree"
    out = by_id(VesslProvider.normalize({vessl.ENDPOINT: [snap(raws[0].payload)]}))
    assert len(out) == 3
    h100 = out["nvidia-h100-sxm-80gb:1x"]
    assert h100.raw_gpu_name == "NVIDIA H100 SXM 80GB" and h100.canonical_gpu_name == "NVIDIA H100 80GB SXM5"
    assert h100.price_per_gpu_hour == h100.price_per_instance_hour == D("2.98") and h100.gpu_count == 1
    assert h100.provider_tier == "container" and h100.market_type == "on_demand" and h100.available is None
    a100 = out["nvidia-a100-sxm-80gb:1x"]
    assert a100.canonical_gpu_name == "NVIDIA A100 80GB SXM4" and a100.price_per_gpu_hour == D("1.48")
    assert out["nvidia-l40s-48gb:1x"].canonical_gpu_name == "NVIDIA L40S 48GB"
    assert out["nvidia-l40s-48gb:1x"].price_per_gpu_hour == D("1.80")

    # A desktop/mobile disagreement is skipped, not guessed; a sales-quoted row gives no listing.
    page = text("vessl_pricing.html")
    mobile_off = page.replace('<span class="text-2xl font-semibold text-foreground">$1.48</span>',
                              '<span class="text-2xl font-semibold text-foreground">$1.58</span>')
    payload = vessl.extract(mobile_off)
    assert [r["gpu"] for r in payload["rows"] if r.get("conflict")] == ["NVIDIA A100 SXM"]
    assert set(by_id(VesslProvider.normalize({vessl.ENDPOINT: [snap(payload)]}))) == \
        {"nvidia-h100-sxm-80gb:1x", "nvidia-l40s-48gb:1x"}
    quoted = {"rows": [dict(rows[0], price="Quoted with sales")] + rows[1:]}
    assert len(VesslProvider.normalize({vessl.ENDPOINT: [snap(quoted)]})) == 2

    # Reshaped page, or prices no longer stated per GPU: fail closed.
    assert raises(vessl.extract, page.replace("per GPU, per hour", "per node, per hour"))
    assert raises(vessl.extract, page.replace(">Container Compute</h2>", ">Compute</h2>"))
    assert raises(vessl.extract, page.replace("<tbody>", "<tbody hidden>").replace("</tbody>", ""))


# --------------------------------------------------------------------------
# Mithril (formerly ML Foundry)
# --------------------------------------------------------------------------


def test_mithril():
    raws = _fetch(mithril_prov())
    assert len(raws) == 1 and raws[0].ok
    rows = raws[0].payload["rows"]
    assert len(rows) == 6
    out = by_id(MithrilProvider.normalize({mithril.ENDPOINT: [snap(raws[0].payload)]}))
    assert len(out) == 6
    h200 = out["h200-sxm5:8x:160c-1825g-15500d"]
    assert h200.raw_gpu_name == "H200 SXM5" and h200.canonical_gpu_name == "NVIDIA H200 141GB SXM5"
    assert h200.gpu_count == 8 and h200.price_per_instance_hour == D("18.59")
    assert h200.price_per_gpu_hour == D("18.59") / 8 == D("2.32375"), "instance price / 8 GPUs, exact"
    assert h200.market_type == "spot" and h200.interruptible is True and h200.available is None
    assert h200.vcpu == 160 and h200.ram_gb == 1825 and h200.storage_gb == 15500
    # Two 1x H200 types with different specs stay two listings.
    one = out["h200-sxm5:1x:20c-227g-3300d"], out["h200-sxm5:1x:20c-200g-1500d"]
    assert [l.price_per_gpu_hour for l in one] == [D("3.93"), D("2.15")]
    h100 = out["h100-sxm5:8x:168c-1814g-12500d"]
    assert h100.canonical_gpu_name == "NVIDIA H100 80GB SXM5" and h100.price_per_gpu_hour == D("2.625")
    assert out["h100-sxm5:1x:20c-226g-1774d"].price_per_instance_hour == D("1.51")

    # A row with zero GPUs is skipped.
    body = {"rows": [dict(rows[0], gpus="0")] + rows[1:]}
    assert len(MithrilProvider.normalize({mithril.ENDPOINT: [snap(body)]})) == 5

    # Reshaped page, or the price no longer a stated 7-day average: fail closed.
    page = text("mithril_pricing.html")
    assert raises(mithril.extract, page.replace("represent an average of the last 7 days", "are live"))
    assert raises(mithril.extract, page.replace("Spot (7-day avg)", "Spot"))
    assert raises(mithril.extract, page.replace(">instance/hr<", ">GPU/hr<"))
    assert raises(mithril.extract, page.replace(">$18.59<", ">Contact us<"))


# --------------------------------------------------------------------------
# Failures and registry
# --------------------------------------------------------------------------


def test_fetch_failures():
    """An error page is stored as a failed response with no payload, never as prices."""
    def failing():
        return httpx.MockTransport(lambda r: httpx.Response(503, text="maintenance"))

    def down():
        def handler(request):
            raise httpx.ConnectError("unreachable")
        return httpx.MockTransport(handler)

    for make in (failing, down):
        for prov in (ArkaneProvider(_client("https://x", make())), VesslProvider(_client("https://x", make())),
                     MithrilProvider(_client("https://x", make()))):
            raws = _fetch(prov)
            assert len(raws) == 1 and not raws[0].ok and raws[0].payload is None and raws[0].error, prov.name
    # A 200 page that lost its table is a failed poll too.
    prov = VesslProvider(_client("https://vessl.ai", _serve({vessl.ENDPOINT: "<html>maintenance</html>"})))
    raws = _fetch(prov)
    assert not raws[0].ok and "per GPU" in raws[0].error
    # No snapshot: no listings, no error.
    assert ArkaneProvider.normalize({}) == [] and MithrilProvider.normalize({}) == []


def test_registry():
    assert set(NEW) <= set(PROVIDERS)
    names = {p.name for p in fetch_mod.build_fetchers()}
    assert set(NEW) <= names, names
    assert {n: mapping.check_mapping_coverage().get(n) for n in NEW} == dict.fromkeys(NEW), "every field documented"
    assert set(NEW) <= set(mapping.PROVIDER_MAPPINGS)
    assert {p: provider_meta.meta(p).provider_class for p in NEW} == dict.fromkeys(NEW, "neocloud")
    assert {provider_meta.meta(p).source_type for p in NEW} == {"public_page"}
    # Declined in this round: resellers of clouds already read, and terms that forbid automated access.
    assert not ({"lightning", "lightningai", "saturn", "saturncloud", "cloudrift", "oblivus"} & set(PROVIDERS))
    # New aliases never merge variants.
    assert canonical.canonical_gpu_name("NVIDIA B300 288GB") == "NVIDIA B300 288GB SXM"
    assert canonical.canonical_gpu_name("NVIDIA A100 SXM 80GB") == "NVIDIA A100 80GB SXM4"
    for unmapped in ("NVIDIA H100 80GB", "NVIDIA A100 80GB", "RTX PRO 6000 96GB"):
        assert canonical.canonical_gpu_name(unmapped) is None, unmapped


# --------------------------------------------------------------------------
# Pipeline: raw storage -> normalize + quality -> rollup -> index votes
# --------------------------------------------------------------------------


def test_pipeline():
    import fixtures
    import normalize
    import scratchdb
    from sqlalchemy import text as sql
    from analytics import indices, rollups

    db = "opengrid_test_neoclouds"
    url = scratchdb.create(db)
    try:
        Session = fixtures.session(url)
        fetch_mod.SessionLocal = Session
        normalize.SessionLocal = Session

        now = datetime.now(timezone.utc).replace(microsecond=0)
        for when in (now - timedelta(hours=3), now - timedelta(minutes=5)):  # two polls, three hours apart
            responses = []
            for p in (arkane_prov(), vessl_prov(), mithril_prov()):
                responses += [r.model_copy(update={"fetched_at": when}) for r in _fetch(p)]
            assert all(r.ok for r in responses), [r.error for r in responses if not r.ok]
            fetch_mod.save(responses)
            normalize.refresh(NEW)

        with Session() as s:
            stored = dict(s.execute(sql("SELECT provider, count(*) FROM raw_snapshots GROUP BY 1")).all())
            bodies = s.execute(sql("SELECT count(*) FROM raw_payloads")).scalar()
            listed = dict(s.execute(sql("SELECT provider, count(*) FROM compute_listings GROUP BY 1")).all())
            held = s.execute(sql("SELECT provider, listing_id, rule FROM quality_quarantine WHERE status = 'pending'")).all()
        assert stored == {"arkane": 2, "vessl": 2, "mithril": 2}, stored
        assert bodies == 3, "identical bodies from the second poll are stored once"
        assert listed == {"arkane": 9, "vessl": 3, "mithril": 6}, listed
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

        assert voters("NVIDIA L40S 48GB", ".neocloud") == {"arkane", "vessl"}
        assert voters("NVIDIA H100 80GB SXM5", ".neocloud") == {"vessl"}, "Mithril is spot; Arkane's H100 is unmapped"
        assert "mithril" not in set().union(*(v for k, v in votes.items() if k.endswith(".neocloud"))), \
            "spot prices never vote in an on-demand class index"
        l40s = votes[indices.gpu_index_id("NVIDIA L40S 48GB") + ".neocloud"]
        assert abs(l40s["arkane"][0] - 1.79) < 1e-9 and abs(l40s["vessl"][0] - 1.80) < 1e-9
    finally:
        scratchdb.drop(db)


if __name__ == "__main__":
    for t in (test_arkane, test_vessl, test_mithril, test_fetch_failures, test_registry, test_pipeline):
        t()
        print(t.__name__, "ok")
    print("all passed")
