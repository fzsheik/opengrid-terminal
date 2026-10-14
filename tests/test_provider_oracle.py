"""Oracle Cloud (OCI) list prices from the anonymous price-list API.

The parser runs on a trimmed copy of the REAL response
(tests/fixtures_providers/oracle_products.json, fetched 2026-10-13 with an
unauthenticated GET; every item with "GPU" in its name plus two CPU parts). The
last test pushes it through raw storage, normalize + quality screening, the
hourly rollup and the index vote in a scratch database.

Run:  .venv/bin/python tests/test_provider_oracle.py
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
from providers import PROVIDERS, OracleProvider  # noqa: E402
from providers import oracle  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_providers"
NOW = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
BODY = (FIX / "oracle_products.json").read_text(encoding="utf-8")


def snap(payload, at=NOW):
    return SimpleNamespace(payload=payload, request=None, fetched_at=at)


def raises(fn, *a):
    try:
        fn(*a)
    except ValueError:
        return True
    return False


def _serve(body: str, status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.raw_path.decode().startswith("/pls/apex/cetools/api/v1/products/"):
            return httpx.Response(status, text=body, headers={"content-type": "application/json"})
        return httpx.Response(404, text="not found")
    return httpx.MockTransport(handler)


def _provider(body=BODY, status=200):
    return OracleProvider(httpx.AsyncClient(base_url="https://apexapps.oracle.com", transport=_serve(body, status)))


def _normalize(payload):
    return {l.listing_id: l for l in OracleProvider.normalize({oracle.ENDPOINT: [snap(payload)]})}


def test_fetch_trims_to_known_parts():
    raw = asyncio.run(_provider().fetch())
    assert len(raw) == 1 and raw[0].ok and raw[0].endpoint == oracle.ENDPOINT
    parts = [i["partNumber"] for i in raw[0].payload["items"]]
    assert set(parts) == oracle.PARTS, set(parts) ^ oracle.PARTS
    assert raw[0].payload["lastUpdated"] == "2026-10-08T14:38:16.165Z"
    # Licences, Cloud@Customer, VMware, edge devices and CPU parts are dropped; items kept verbatim.
    full = {i["partNumber"]: i for i in json.loads(BODY)["items"]}
    assert all(i == full[i["partNumber"]] for i in raw[0].payload["items"])
    for dropped in ("B111824", "B110965", "B108806", "B109493", "B88517", "B88518", "B109480", "B93544"):
        assert dropped not in parts, dropped


def test_normalize():
    out = _normalize(oracle.extract(json.loads(BODY)))
    assert set(out) == set(oracle.SHAPES), set(out) ^ set(oracle.SHAPES)
    assert len(out) == 19

    h100 = out["BM.GPU.H100.8"]
    assert h100.provider == "oracle" and h100.sku == "BM.GPU.H100.8" and h100.gpu_count == 8
    assert h100.price_per_gpu_hour == D("10") and h100.price_per_instance_hour == D("80")
    assert isinstance(h100.price_per_gpu_hour, D)
    assert h100.market_type == "on_demand" and not h100.interruptible and h100.available is None
    assert h100.region is None and h100.country is None, "OCI list prices are global"
    assert h100.provider_tier == "bare-metal" and h100.currency == "USD" and h100.observed_at == NOW
    assert h100.raw_gpu_name == "NVIDIA H100 80GB" and h100.canonical_gpu_name is None, "SXM/PCIe not stated"

    expect = {
        # shape: (GPUs, per-GPU price, canonical name)
        "BM.GPU.H200.8": (8, D("10"), "NVIDIA H200 141GB SXM5"),
        "BM.GPU.B200.8": (8, D("14"), "NVIDIA B200 180GB SXM"),
        "BM.GPU.B300.8": (8, D("15"), "NVIDIA B300 288GB SXM"),
        "BM.GPU.GB200.4": (4, D("16"), None),
        "BM.GPU.GB300.4": (4, D("18"), "NVIDIA GB300 288GB"),
        "BM.GPU.MI300X.8": (8, D("6"), "AMD Instinct MI300X 192GB"),
        "BM.GPU.MI355X.8": (8, D("8.6"), "AMD Instinct MI355X 288GB"),
        "BM.GPU.A100-v2.8": (8, D("4"), None),
        "BM.GPU4.8": (8, D("3.05"), None),
        "BM.GPU.L40S.4": (4, D("3.5"), "NVIDIA L40S 48GB"),
        "BM.GPU.RTXPRO.8": (8, D("4.5"), None),
        "VM.GPU.A10.1": (1, D("2"), "NVIDIA A10 24GB PCIe"),
        "VM.GPU.A10.2": (2, D("2"), "NVIDIA A10 24GB PCIe"),
        "BM.GPU.A10.4": (4, D("2"), "NVIDIA A10 24GB PCIe"),
        "VM.GPU3.1": (1, D("2.95"), "NVIDIA Tesla V100 16GB"),
        "BM.GPU3.8": (8, D("2.95"), "NVIDIA Tesla V100 16GB"),
    }
    for shape, (count, price, canon) in expect.items():
        l = out[shape]
        assert (l.gpu_count, l.price_per_gpu_hour, l.canonical_gpu_name) == (count, price, canon), (shape, l)
        assert l.price_per_instance_hour == price * count, shape
    assert out["BM.GPU.MI355X.8"].price_per_instance_hour == D("68.8")
    assert out["VM.GPU.A10.2"].provider_tier == "vm" and out["BM.GPU.A10.4"].provider_tier == "bare-metal"
    assert all(l.market_type == "on_demand" for l in out.values()), "no preemptible listing exists"
    assert canonical.canonical_gpu_name("NVIDIA A100 80GB") is None
    assert canonical.canonical_gpu_name("NVIDIA RTX PRO 6000 96GB") is None


def test_fail_closed():
    good = json.loads(BODY)
    assert raises(oracle.extract, {"products": []}), "no items list"
    assert raises(oracle.extract, []), "not an object"
    assert raises(oracle.extract, {"items": [i for i in good["items"] if i["partNumber"] not in oracle.PARTS]})

    def broken(edit):
        d = copy.deepcopy(good)
        item = next(i for i in d["items"] if i["partNumber"] == "B98415")
        edit(item)
        return d

    tiered = broken(lambda i: i["currencyCodeLocalizations"][0]["prices"].append(
        {"model": "PAY_AS_YOU_GO", "value": 8, "rangeMin": 100, "rangeMax": 1000}))
    per_node = broken(lambda i: i.update(metricName="Node Per Hour"))
    no_usd = broken(lambda i: i["currencyCodeLocalizations"][0].update(currencyCode="EUR"))
    renamed = broken(lambda i: i.pop("currencyCodeLocalizations") and i.update(prices=[]))
    not_number = broken(lambda i: i["currencyCodeLocalizations"][0]["prices"][0].update(value="ten"))
    for bad in (tiered, per_node, no_usd, renamed, not_number):
        assert raises(oracle.extract, bad)
        assert raises(OracleProvider.normalize, {oracle.ENDPOINT: [snap(bad)]}), "normalize fails closed too"

    # Through fetch(): a reshaped body is a failed response, never a healthy empty one.
    r = asyncio.run(_provider(json.dumps(per_node)).fetch())[0]
    assert not r.ok and r.payload is None and "ValueError" in r.error
    r = asyncio.run(_provider('{"items": "gone"}').fetch())[0]
    assert not r.ok and r.payload is None
    r = asyncio.run(_provider("oops", status=503).fetch())[0]
    assert not r.ok and r.status_code == 503

    # A part withdrawn from the list just drops its shapes.
    d = copy.deepcopy(good)
    d["items"] = [i for i in d["items"] if i["partNumber"] != "B95909"]
    out = _normalize(oracle.extract(d))
    assert "VM.GPU.A10.1" not in out and "BM.GPU.H100.8" in out and len(out) == 16
    assert OracleProvider.normalize({}) == []


def test_registry():
    assert PROVIDERS["oracle"] is OracleProvider
    assert any(isinstance(p, OracleProvider) for p in fetch_mod.build_fetchers())
    assert mapping.check_mapping_coverage() == {}
    assert "oracle" in mapping.PROVIDER_MAPPINGS
    m = provider_meta.meta("oracle")
    assert m.provider_class == "hyperscaler" and m.source_type == "public_pricing_file"
    assert m.source == "apexapps.oracle.com"
    # Every shape's GPU name is either mapped or deliberately left unmapped (and documented).
    unmapped = {n for _, _, n in oracle.SHAPES.values() if canonical.canonical_gpu_name(n) is None}
    assert unmapped == {"NVIDIA H100 80GB", "NVIDIA A100 80GB", "NVIDIA A100 40GB",
                        "NVIDIA RTX PRO 6000 96GB", "NVIDIA GB200 189GB (NVL72)"}, unmapped


def test_pipeline():
    import fixtures
    import normalize
    import scratchdb
    from sqlalchemy import text as sql
    from analytics import indices, rollups

    db = "opengrid_test_oracle"
    url = scratchdb.create(db)
    try:
        Session = fixtures.session(url)
        fetch_mod.SessionLocal = Session
        normalize.SessionLocal = Session

        now = datetime.now(timezone.utc).replace(microsecond=0)
        for when in (now - timedelta(hours=3), now - timedelta(minutes=5)):
            responses = [r.model_copy(update={"fetched_at": when}) for r in asyncio.run(_provider().fetch())]
            assert all(r.ok for r in responses), [r.error for r in responses if not r.ok]
            fetch_mod.save(responses)
            normalize.refresh(["oracle"])

        with Session() as s:
            stored = s.execute(sql("SELECT count(*) FROM raw_snapshots WHERE provider = 'oracle'")).scalar()
            bodies = s.execute(sql("SELECT count(*) FROM raw_payloads")).scalar()
            listed = s.execute(sql("SELECT count(*) FROM compute_listings WHERE provider = 'oracle'")).scalar()
            held = s.execute(sql("SELECT listing_id, rule FROM quality_quarantine WHERE status = 'pending'")).all()
        assert stored == 2 and bodies == 1, (stored, bodies)
        assert listed == 19, listed
        assert not held, f"nothing held on clean data: {held}"

        rollups.rebuild()
        with Session() as s:
            last = s.execute(sql("SELECT max(hour) FROM market_hourly")).scalar()
            rows = [dict(r._mapping) for r in s.execute(sql(
                "SELECT segment, gpu, provider, region, country, hour, min_price FROM market_hourly "
                "WHERE hour = :h AND min_price IS NOT NULL"), {"h": last})]
        votes = indices.votes_by_index(rows)
        h200 = votes[indices.gpu_index_id("NVIDIA H200 141GB SXM5") + ".hyperscaler"]
        assert set(h200) == {"oracle"} and abs(h200["oracle"][0] - 10.0) < 1e-9
        assert "oracle" not in votes.get(indices.gpu_index_id("NVIDIA H200 141GB SXM5") + ".neocloud", {})
        assert "oracle" not in votes.get(indices.gpu_index_id("NVIDIA H200 141GB SXM5", "spot"), {})
    finally:
        scratchdb.drop(db)


if __name__ == "__main__":
    for t in (test_fetch_trims_to_known_parts, test_normalize, test_fail_closed, test_registry, test_pipeline):
        t()
        print(t.__name__, "ok")
    print("all passed")
