"""Google Cloud Compute Engine: the public accelerator-optimized price page.

The parser runs on a trimmed copy of the REAL page (tests/fixtures_providers/
gcp_accelerator_optimized.html, fetched 2026-10-13 with an unauthenticated GET):
the two sections read, with scripts, unselected region options and behaviour
attributes removed. The last test pushes it through raw storage, normalize +
quality screening, the hourly rollup and the index vote in a scratch database.

Run:  .venv/bin/python tests/test_provider_gcp.py
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
import mapping  # noqa: E402
import provider_meta  # noqa: E402
from providers import PROVIDERS, gcp  # noqa: E402
from providers.gcp import ENDPOINT, GcpProvider  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_providers" / "gcp_accelerator_optimized.html"
NOW = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)


def page():
    return FIX.read_text(encoding="utf-8")


def snap(payload, at=NOW):
    return SimpleNamespace(payload=payload, request=None, fetched_at=at)


def listings(payload):
    return {l.listing_id: l for l in GcpProvider.normalize({ENDPOINT: [snap(payload)]})}


def raises(fn, *a):
    try:
        fn(*a)
    except ValueError:
        return True
    return False


def _serve(body: str):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/products/compute/pricing/accelerator-optimized":
            return httpx.Response(200, text=body, headers={"content-type": "text/html"})
        return httpx.Response(404, text="not found")
    return httpx.AsyncClient(base_url="https://cloud.google.com", transport=httpx.MockTransport(handler))


def test_extract():
    payload = gcp.extract(page())
    assert payload["region"] == "Iowa (us-central1)"
    assert len(payload["machines"]) == 28 and len(payload["attachable"]) == 4
    a3 = next(r for r in payload["machines"] if r["machine_type"] == "a3-highgpu-8g")
    assert a3 == {"machine_type": "a3-highgpu-8g", "gpu": "Nvidia H100", "gpus": "8", "vcpus": "208",
                  "memory": "1,871GB", "price": "$88.490000119 / 1 hour"}, "the on-demand column, as printed"
    assert [r["model"] for r in payload["attachable"]] == ["NVIDIA T4", "NVIDIA P4", "NVIDIA V100", "NVIDIA P100"]


def test_normalize():
    out = listings(gcp.extract(page()))

    h100 = out["a3-highgpu-8g:us-central1"]
    assert h100.raw_gpu_name == "nvidia-h100-80gb" and h100.canonical_gpu_name == "NVIDIA H100 80GB SXM5"
    assert h100.gpu_count == 8 and h100.price_per_instance_hour == D("88.490000119")
    assert h100.price_per_gpu_hour == D("88.490000119") / 8 == D("11.061250014875"), "VM price / 8 GPUs, exact"
    assert h100.market_type == "on_demand" and not h100.interruptible and h100.available is None
    assert h100.region == "us-central1" and h100.country == "US" and h100.provider_tier == "hyperscaler"
    assert h100.vcpu == 208 and h100.ram_gb == 1871
    mega = out["a3-megagpu-8g:us-central1"]
    assert mega.canonical_gpu_name == "NVIDIA H100 80GB SXM5" and mega.price_per_gpu_hour == D("11.675089100875")
    h200 = out["a3-ultragpu-8g:us-central1"]
    assert h200.canonical_gpu_name == "NVIDIA H200 141GB SXM5" and h200.price_per_gpu_hour == D("10.600863561625")
    a100_80 = out["a2-ultragpu-8g:us-central1"]
    assert a100_80.canonical_gpu_name == "NVIDIA A100 80GB SXM4" and a100_80.price_per_gpu_hour == D("5.068797890375")
    a100_40 = out["a2-highgpu-1g:us-central1"]
    assert a100_40.canonical_gpu_name == "NVIDIA A100 40GB SXM4" and a100_40.price_per_gpu_hour == D("3.673385")
    assert out["a2-megagpu-16g:us-central1"].gpu_count == 16
    assert out["a2-megagpu-16g:us-central1"].price_per_gpu_hour == D("3.483719")
    rtx = out["g4-standard-384:us-central1"]
    assert rtx.canonical_gpu_name == "NVIDIA RTX PRO 6000 96GB SE" and rtx.price_per_gpu_hour == D("4.49993")
    l4 = out["g2-standard-4:us-central1"]
    assert l4.canonical_gpu_name == "NVIDIA L4 24GB" and l4.price_per_gpu_hour == D("0.706832276") and l4.ram_gb == 16
    assert out["g2-standard-96:us-central1"].price_per_gpu_hour == D("1.000416348")

    # Attachable GPUs: the GPU alone, per GPU.
    t4 = out["nvidia-tesla-t4:us-central1"]
    assert t4.canonical_gpu_name == "NVIDIA T4 16GB" and t4.gpu_count == 1 and t4.price_per_gpu_hour == D("0.35")
    assert t4.provider_tier == "gpu-attachment" and t4.vcpu is None
    assert out["nvidia-tesla-v100:us-central1"].canonical_gpu_name == "NVIDIA Tesla V100 16GB", "variant-less V100"
    assert out["nvidia-tesla-v100:us-central1"].price_per_gpu_hour == D("2.48")
    assert out["nvidia-tesla-p4:us-central1"].canonical_gpu_name is None, "no canonical P4"
    assert out["nvidia-tesla-p100:us-central1"].canonical_gpu_name is None, "no canonical P100"

    # B200 is N/A on demand; fractional G4 shapes are not whole GPUs; spot / DWS / CUDs never read.
    assert "a4-highgpu-8g:us-central1" not in out
    assert not {"g4-standard-6:us-central1", "g4-standard-12:us-central1", "g4-standard-24:us-central1"} & set(out)
    prices = {l.price_per_instance_hour for l in out.values()}
    assert not prices & {D("52.794210411"), D("38.3200"), D("41.6000"), D("61.383674231"), D("0.22")}
    assert all(l.market_type == "on_demand" for l in out.values())
    assert len(out) == 28


def test_hand_table_wins():
    payload = gcp.extract(page())
    # The page printing a different count or GPU than the table: skipped, not guessed.
    for r in payload["machines"]:
        if r["machine_type"] == "a3-highgpu-8g":
            r["gpus"] = "4"
        if r["machine_type"] == "g2-standard-4":
            r["gpu"] = "NVIDIA L40S"
    # A machine type nobody put in the table: skipped.
    payload["machines"].append({"machine_type": "a5-highgpu-8g", "gpu": "Nvidia B300", "gpus": "8",
                                "vcpus": "224", "memory": "4,000GB", "price": "$120.00 / 1 hour"})
    out = listings(payload)
    assert "a3-highgpu-8g:us-central1" not in out and "g2-standard-4:us-central1" not in out
    assert "a5-highgpu-8g:us-central1" not in out and len(out) == 26
    assert GcpProvider.normalize({}) == []


def test_fail_closed():
    html = page()
    assert raises(gcp.extract, "<html>new design</html>")
    # Another region rendered: not us-central1, so the poll fails.
    assert raises(gcp.extract, html.replace("Iowa (us-central1)", "Belgium (europe-west1)", 1))
    assert raises(gcp.extract, html.replace(">Machine type<", ">Instance<", 1))
    assert raises(gcp.extract, html.replace(">GPU price (USD)<", ">Price<", 1))
    assert raises(gcp.extract, html.replace("$88.490000119 / 1 hour", "$88.49 per hour", 1))
    assert raises(gcp.extract, html.replace(">GPU Attachment pricing<", ">Attach a GPU<", 1))
    assert not raises(gcp.extract, html)


def test_fetch():
    raw = asyncio.run(GcpProvider(_serve(page())).fetch())[0]
    assert raw.ok and raw.endpoint == ENDPOINT and raw.payload == gcp.extract(page())
    broken = asyncio.run(GcpProvider(_serve("<html>new design</html>")).fetch())[0]
    assert not broken.ok and broken.payload is None and "ValueError" in broken.error


def test_registry():
    assert PROVIDERS["gcp"] is GcpProvider
    assert mapping.check_mapping_coverage() == {}
    assert "gcp" in mapping.PROVIDER_MAPPINGS
    m = provider_meta.meta("gcp")
    assert (m.provider_class, m.source_type, m.source) == ("hyperscaler", "public_page", "cloud.google.com")
    import fetch
    assert any(isinstance(p, GcpProvider) for p in fetch.build_fetchers())
    # Every table entry resolves to a canonical name or is a known, deliberate gap.
    unmapped = {a for _, a, _ in gcp.GPU_MACHINES.values()} | {a for a, _ in gcp.ATTACHABLE.values()}
    unmapped = {a for a in unmapped if canonical.canonical_gpu_name(a) is None}
    assert unmapped == {"nvidia-tesla-p4", "nvidia-tesla-p100"}, unmapped


def test_pipeline():
    import fetch
    import fixtures
    import normalize
    import scratchdb
    from sqlalchemy import text as sql
    from analytics import indices, rollups

    db = "opengrid_test_gcp"
    url = scratchdb.create(db)
    try:
        Session = fixtures.session(url)
        fetch.SessionLocal = Session
        normalize.SessionLocal = Session

        now = datetime.now(timezone.utc).replace(microsecond=0)
        for when in (now - timedelta(hours=3), now - timedelta(minutes=5)):  # two polls, three hours apart
            responses = [r.model_copy(update={"fetched_at": when}) for r in asyncio.run(GcpProvider(_serve(page())).fetch())]
            assert all(r.ok for r in responses), [r.error for r in responses]
            fetch.save(responses)
            normalize.refresh(["gcp"])

        with Session() as s:
            stored = s.execute(sql("SELECT count(*) FROM raw_snapshots WHERE provider = 'gcp'")).scalar()
            bodies = s.execute(sql("SELECT count(*) FROM raw_payloads")).scalar()
            listed = s.execute(sql("SELECT count(*) FROM compute_listings WHERE provider = 'gcp'")).scalar()
            held = s.execute(sql("SELECT listing_id, rule FROM quality_quarantine WHERE status = 'pending'")).all()
        assert stored == 2 and bodies == 1, "the identical second body is stored once"
        assert listed == 28, listed
        assert not held, f"nothing held on clean data: {held}"

        rollups.rebuild()
        with Session() as s:
            last = s.execute(sql("SELECT max(hour) FROM market_hourly")).scalar()
            rows = [dict(r._mapping) for r in s.execute(sql(
                "SELECT segment, gpu, provider, region, country, hour, min_price FROM market_hourly "
                "WHERE hour = :h AND min_price IS NOT NULL"), {"h": last})]
        votes = indices.votes_by_index(rows)
        h100 = votes[indices.gpu_index_id("NVIDIA H100 80GB SXM5") + ".hyperscaler"]
        assert set(h100) == {"gcp"} and abs(h100["gcp"][0] - 11.06125) < 1e-6, "cheapest A3: High, not Mega"
        assert "gcp" not in votes.get(indices.gpu_index_id("NVIDIA H100 80GB SXM5") + ".neocloud", {})
        assert not any(k.endswith(".spot") and "gcp" in v for k, v in votes.items()), "no spot listings"
    finally:
        scratchdb.drop(db)


if __name__ == "__main__":
    for t in (test_extract, test_normalize, test_hand_table_wins, test_fail_closed, test_fetch, test_registry,
              test_pipeline):
        t()
        print(t.__name__, "ok")
    print("all passed")
