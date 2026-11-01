"""Modal, Replicate, Baseten and Beam: serverless GPU platforms read from their price pages.

Parsers run on trimmed copies of the REAL pages (tests/fixtures_providers/,
fetched 2026-10-21 with unauthenticated GETs). The last test pushes all four
through raw storage, normalize + quality screening, the hourly rollup and the
index vote in a scratch database.

Run:  .venv/bin/python tests/test_provider_serverless.py
"""

import asyncio
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
from providers import baseten, beam, modal, replicate  # noqa: E402
from providers.baseten import BasetenProvider  # noqa: E402
from providers.beam import BeamProvider  # noqa: E402
from providers.modal import ModalProvider  # noqa: E402
from providers.replicate import ReplicateProvider  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_providers"
NOW = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
NEW = ["modal", "replicate", "baseten", "beam"]
PAGES = {  # provider class, base url, endpoint, fixture
    "modal": (ModalProvider, modal.BASE_URL, modal.ENDPOINT, "modal_pricing.html"),
    "replicate": (ReplicateProvider, replicate.BASE_URL, replicate.ENDPOINT, "replicate_pricing.html"),
    "baseten": (BasetenProvider, baseten.BASE_URL, baseten.ENDPOINT, "baseten_pricing.html"),
    "beam": (BeamProvider, beam.BASE_URL, beam.ENDPOINT, "beam_pricing.html"),
}


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


def prov(name, body=None, status=200):
    cls, base, endpoint, fixture = PAGES[name]
    page = text(fixture) if body is None else body

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == endpoint:
            return httpx.Response(status, text=page, headers={"content-type": "text/html"})
        return httpx.Response(404, text="not found")
    return cls(httpx.AsyncClient(base_url=base, transport=httpx.MockTransport(handler)))


def fetched(name, body=None):
    return asyncio.run(prov(name, body).fetch())[0]


def normalized(name):
    raw = fetched(name)
    assert raw.ok, raw.error
    cls, _, endpoint, _ = PAGES[name]
    return by_id(cls.normalize({endpoint: [snap(raw.payload)]}))


# --------------------------------------------------------------------------
# Modal
# --------------------------------------------------------------------------


def test_modal():
    out = normalized("modal")
    assert len(out) == 11
    h100 = out["nvidia-h100-sxm5"]
    # $0.001097/sec x 3600, exactly.
    assert h100.price_per_gpu_hour == D("3.9492") and h100.price_per_instance_hour == D("3.9492")
    assert h100.canonical_gpu_name == "NVIDIA H100 80GB SXM5" and h100.gpu_count == 1
    assert h100.provider_tier == "gpu-attachment" and h100.interruptible is True and h100.market_type == "on_demand"
    assert out["nvidia-h200-sxm"].canonical_gpu_name == "NVIDIA H200 141GB SXM5"
    assert out["nvidia-h200-sxm"].price_per_gpu_hour == D("4.5396")
    assert out["nvidia-b300"].canonical_gpu_name == "NVIDIA B300 288GB SXM"
    assert out["nvidia-b200"].canonical_gpu_name == "NVIDIA B200 180GB SXM"
    assert out["nvidia-b200"].price_per_gpu_hour == D("6.2496")
    assert out["nvidia-l40s"].canonical_gpu_name == "NVIDIA L40S 48GB"
    assert out["nvidia-t4"].price_per_gpu_hour == D("0.5904") and out["nvidia-t4"].canonical_gpu_name == "NVIDIA T4 16GB"
    for unmapped in ("nvidia-a100-80-gb", "nvidia-a100-40-gb", "nvidia-rtx-pro-6000", "nvidia-a10"):
        assert out[unmapped].canonical_gpu_name is None, unmapped
    assert out["nvidia-a100-80-gb"].raw_gpu_name == "Nvidia A100, 80 GB"

    # Reshaped: list gone, or a price unit we do not read.
    assert raises(modal.extract, "<html><body>Pricing</body></html>")
    assert raises(modal.extract, text("modal_pricing.html").replace(">/ sec<", ">/ min<", 1))


# --------------------------------------------------------------------------
# Replicate
# --------------------------------------------------------------------------


def test_replicate():
    raw = fetched("replicate")
    assert len(raw.payload["cards"]) == 8 and len(raw.payload["committed_spend"]) == 11
    out = normalized("replicate")
    assert sorted(out) == ["gpu-a100-large", "gpu-a100-large-2x", "gpu-h100", "gpu-l40s", "gpu-l40s-2x", "gpu-t4"]
    l40s2 = out["gpu-l40s-2x"]
    # $0.001950/sec x 3600 for the whole hardware, / 2 GPUs.
    assert l40s2.price_per_instance_hour == D("7.02") and l40s2.price_per_gpu_hour == D("3.51")
    assert l40s2.gpu_count == 2 and l40s2.vcpu == 20 and l40s2.ram_gb == 144
    assert l40s2.raw_gpu_name == "Nvidia L40S" and l40s2.canonical_gpu_name == "NVIDIA L40S 48GB"
    assert l40s2.provider_tier == "serverless" and l40s2.interruptible is False
    assert out["gpu-t4"].price_per_gpu_hour == D("0.81") and out["gpu-t4"].canonical_gpu_name == "NVIDIA T4 16GB"
    assert out["gpu-h100"].price_per_gpu_hour == D("5.49") and out["gpu-h100"].canonical_gpu_name is None
    assert out["gpu-a100-large"].raw_gpu_name == "Nvidia A100 (80GB)" and out["gpu-a100-large"].canonical_gpu_name is None
    # CPU cards and committed-spend cards give no listing.
    for gone in ("cpu", "cpu-small", "gpu-h200", "gpu-h100-8x", "gpu-l40s-4x"):
        assert gone not in out, gone

    # A card whose name disagrees with its GPU count is skipped, never guessed.
    body = json.loads(json.dumps(raw.payload))
    for c in body["cards"]:
        if c["hardware"] == "gpu-l40s-2x":
            c["gpu"] = "1x"
    assert "gpu-l40s-2x" not in by_id(ReplicateProvider.normalize({replicate.ENDPOINT: [snap(body)]}))
    # Reshaped: the divider or the price layout gone.
    assert raises(replicate.extract, text("replicate_pricing.html").replace("Additional hardware", "More"))
    assert raises(replicate.extract, text("replicate_pricing.html").replace("/sec<br/>", "/min<br/>", 1))


# --------------------------------------------------------------------------
# Baseten
# --------------------------------------------------------------------------


def test_baseten():
    raw = fetched("baseten")
    assert len(raw.payload["instances"]) == 13
    assert raw.payload["instances"][5] == {"name": "H100", "instanceType": "gpu", "specs": "80 GiB VRAM",
                                           "pricePerMinute": "0.10833", "pricePerHour": "6.5"}, "prices kept as printed"
    out = normalized("baseten")
    assert sorted(out) == ["a100", "a10g", "b200", "h100", "l4", "t4"], "MIG slice and CPU instances skipped"
    assert out["h100"].price_per_gpu_hour == D("6.5") and out["h100"].raw_gpu_name == "NVIDIA H100 80GB"
    assert out["h100"].canonical_gpu_name is None and out["a100"].canonical_gpu_name is None
    assert out["b200"].price_per_gpu_hour == D("9.98") and out["b200"].canonical_gpu_name == "NVIDIA B200 180GB SXM"
    assert out["t4"].raw_gpu_name == "NVIDIA T4 16GB" and out["t4"].canonical_gpu_name == "NVIDIA T4 16GB"
    assert out["a10g"].canonical_gpu_name == "NVIDIA A10G 24GB" and out["a10g"].price_per_gpu_hour == D("1.2072")
    assert out["l4"].provider_tier == "serverless" and out["l4"].gpu_count == 1

    # An hourly price that disagrees with the per-minute one is skipped.
    body = json.loads(json.dumps(raw.payload))
    body["instances"][5]["pricePerHour"] = "5.5"
    assert "h100" not in by_id(BasetenProvider.normalize({baseten.ENDPOINT: [snap(body)]}))
    # Reshaped: no data stream, or no Dedicated Deployments table.
    assert raises(baseten.extract, "<html><body>Pricing</body></html>")
    assert raises(baseten.extract, text("baseten_pricing.html").replace("Dedicated Deployments", "Deployments"))


# --------------------------------------------------------------------------
# Beam
# --------------------------------------------------------------------------


def test_beam():
    raw = fetched("beam")
    assert [r["group"] for r in raw.payload["serverless"]] == ["Start instantly", "With committed spend",
                                                              "With committed spend"]
    out = normalized("beam")
    assert list(out) == ["serverless:rtx-4090-pcie"], "committed-spend rows give no listing"
    l = out["serverless:rtx-4090-pcie"]
    # $0.000192/sec x 3600, exactly; the GPU alone.
    assert l.price_per_gpu_hour == D("0.6912") and l.provider_tier == "gpu-attachment"
    assert l.raw_gpu_name == "RTX 4090 PCIE 24GB" and l.canonical_gpu_name == "NVIDIA RTX 4090 24GB"
    # Reshaped: table gone, or a row with no group / an unread unit.
    assert raises(beam.extract, "<html><body>Pricing</body></html>")
    assert raises(beam.extract, text("beam_pricing.html").replace(">/sec<", ">/min<", 1))
    assert raises(beam.extract, text("beam_pricing.html").replace("Start instantly", "Soon"))


# --------------------------------------------------------------------------
# Fetch failures, registry, classes
# --------------------------------------------------------------------------


def test_fetch_failures():
    """An error status, or a page with no prices, is a failed response, never 'nothing for sale'."""
    for name in NEW:
        down = asyncio.run(prov(name, "maintenance", status=503).fetch())[0]
        assert not down.ok and down.payload is None, name
        blank = fetched(name, "<html><body><h1>Pricing</h1></body></html>")
        assert not blank.ok and blank.payload is None and "reshaped" in blank.error, (name, blank.error)


def test_registry():
    assert set(NEW) <= set(PROVIDERS)
    names = {p.name for p in fetch_mod.build_fetchers()}
    assert set(NEW) <= names, names
    problems = mapping.check_mapping_coverage()
    assert not (set(NEW) & set(problems)), {n: problems[n] for n in NEW if n in problems}
    assert set(NEW) <= set(mapping.PROVIDER_MAPPINGS)
    assert {p: provider_meta.meta(p).provider_class for p in NEW} == dict.fromkeys(NEW, "general_cloud")
    assert {provider_meta.meta(p).source_type for p in NEW} == {"public_page"}
    # Variants are never merged.
    assert canonical.canonical_gpu_name("Nvidia H100 SXM5") == "NVIDIA H100 80GB SXM5"
    for unmapped in ("Nvidia H100", "NVIDIA H100 80GB", "Nvidia A100, 80 GB", "Nvidia A10", "Nvidia RTX PRO 6000"):
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

    db = "opengrid_test_serverless"
    url = scratchdb.create(db)
    try:
        Session = fixtures.session(url)
        fetch_mod.SessionLocal = Session
        normalize.SessionLocal = Session

        now = datetime.now(timezone.utc).replace(microsecond=0)
        for when in (now - timedelta(hours=3), now - timedelta(minutes=5)):  # two polls, three hours apart
            responses = [r.model_copy(update={"fetched_at": when}) for n in NEW for r in asyncio.run(prov(n).fetch())]
            assert all(r.ok for r in responses), [r.error for r in responses if not r.ok]
            fetch_mod.save(responses)
            normalize.refresh(NEW)

        with Session() as s:
            stored = dict(s.execute(sql("SELECT provider, count(*) FROM raw_snapshots GROUP BY 1")).all())
            bodies = s.execute(sql("SELECT count(*) FROM raw_payloads")).scalar()
            listed = dict(s.execute(sql("SELECT provider, count(*) FROM compute_listings GROUP BY 1")).all())
            held = s.execute(sql("SELECT provider, listing_id, rule FROM quality_quarantine WHERE status = 'pending'")).all()
        assert stored == dict.fromkeys(NEW, 2), stored
        assert bodies == 4, "identical bodies from the second poll are stored once"
        assert listed == {"modal": 11, "replicate": 6, "baseten": 6, "beam": 1}, listed
        assert not held, f"nothing held on clean data: {held}"

        rollups.rebuild()
        with Session() as s:
            last = s.execute(sql("SELECT max(hour) FROM market_hourly")).scalar()
            rows = [dict(r._mapping) for r in s.execute(sql(
                "SELECT segment, gpu, provider, region, country, hour, min_price FROM market_hourly "
                "WHERE hour = :h AND min_price IS NOT NULL"), {"h": last})]
        votes = indices.votes_by_index(rows)
        voting = {p for v in votes.values() for p in v}
        # GPU-only rates (Modal, Beam) are not like-for-like with whole instances: they never vote.
        assert voting == {"replicate", "baseten"}, voting
        l40s = votes[indices.gpu_index_id("NVIDIA L40S 48GB") + ".general-cloud"]
        assert set(l40s) == {"replicate"} and abs(l40s["replicate"][0] - 3.51) < 1e-9
        assert not any(set(NEW) & set(v) for k, v in votes.items() if k.endswith(".neocloud")), \
            "general clouds never vote in a neocloud index"
    finally:
        scratchdb.drop(db)


if __name__ == "__main__":
    for t in (test_modal, test_replicate, test_baseten, test_beam, test_fetch_failures, test_registry,
              test_pipeline):
        t()
        print(t.__name__, "ok")
    print("all passed")
