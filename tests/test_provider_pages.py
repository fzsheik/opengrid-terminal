"""CoreWeave, Together, Jarvislabs, Hot Aisle, Vultr, Boost Run (via Shadeform) and Nebius.

Parsers run on trimmed copies of the REAL responses (tests/fixtures_providers/,
fetched 2026-10-13 with unauthenticated GETs). The last test pushes all six
through raw storage, normalize + quality screening, the hourly rollup and the
index vote in a scratch database.

Run:  .venv/Scripts/python tests/test_provider_pages.py
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

import mapping  # noqa: E402
import provider_meta  # noqa: E402
from providers import PROVIDERS  # noqa: E402
from providers import coreweave, hotaisle, nebius, together  # noqa: E402
from providers.coreweave import CoreWeaveProvider  # noqa: E402
from providers.hotaisle import HotAisleProvider  # noqa: E402
from providers.jarvislabs import JarvislabsProvider  # noqa: E402
from providers.nebius import NebiusProvider  # noqa: E402
from providers.shadeform import BoostRunProvider, ShadeformCloud  # noqa: E402
from providers.together import TogetherProvider  # noqa: E402
from providers.vultr import METAL_ENDPOINT, VM_ENDPOINT, VultrProvider  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_providers"
NOW = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
NEW = ["coreweave", "together", "jarvislabs", "hotaisle", "vultr", "boostrun"]


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


# --------------------------------------------------------------------------
# CoreWeave
# --------------------------------------------------------------------------


def test_coreweave():
    payload = coreweave.extract(text("coreweave_pricing.html"))
    assert len(payload["rows"]) == 24 and not any(r.get("conflict") for r in payload["rows"])
    out = by_id(CoreWeaveProvider.normalize({"/pricing": [snap(payload)]}))

    h100 = out["nvidia-hgx-h100:8x:north-america"]
    assert h100.canonical_gpu_name == "NVIDIA H100 80GB SXM5" and h100.raw_gpu_name == "NVIDIA HGX H100 80GB"
    assert h100.gpu_count == 8 and h100.price_per_instance_hour == D("49.24")
    assert h100.price_per_gpu_hour == D("49.24") / 8 == D("6.155"), "instance price / 8 GPUs"
    assert h100.market_type == "on_demand" and not h100.interruptible and h100.available is None
    assert h100.region == "North America" and h100.country is None, "a continent, never a guessed country"
    spot = out["nvidia-hgx-h100:8x:north-america:spot"]
    assert spot.market_type == "spot" and spot.interruptible and spot.price_per_instance_hour == D("19.71")
    assert out["nvidia-hgx-h100:8x:europe:spot"].price_per_instance_hour == D("19.51"), "regions priced separately"

    assert out["nvidia-hgx-h200:8x:north-america"].canonical_gpu_name == "NVIDIA H200 141GB SXM5"
    assert out["nvidia-hgx-h200:8x:north-america"].price_per_gpu_hour == D("6.305")
    b200 = out["nvidia-hgx-b200:8x:europe"]
    assert b200.canonical_gpu_name == "NVIDIA B200 180GB SXM" and b200.price_per_gpu_hour == D("8.60")
    l40s = out["nvidia-l40s:8x:north-america"]
    assert l40s.canonical_gpu_name == "NVIDIA L40S 48GB" and l40s.price_per_gpu_hour == D("2.25")
    gh = out["nvidia-gh200:1x:north-america"]
    assert gh.canonical_gpu_name == "NVIDIA GH200 96GB" and gh.gpu_count == 1 and gh.price_per_gpu_hour == D("6.50")
    # "(High Memory)" is system RAM; the card is the Server Edition.
    rtx = out["nvidia-rtx-pro-6000-blackwell-server-edition-high-memory:8x:north-america"]
    assert rtx.canonical_gpu_name == "NVIDIA RTX PRO 6000 96GB SE" and rtx.ram_gb == 1024

    # Contact-sales cells give no listing; a spot price alone gives a spot listing only.
    assert "nvidia-hgx-b300:8x:north-america" not in out and "nvidia-hgx-b300:8x:north-america:spot" in out
    assert out["nvidia-hgx-b300:8x:north-america:spot"].canonical_gpu_name == "NVIDIA B300 288GB SXM"
    assert not any(k.startswith("nvidia-gb300") for k in out)
    # Ambiguous or unknown hardware stays unmapped.
    assert out["nvidia-a100:8x:north-america"].canonical_gpu_name is None, "A100: SXM4 or PCIe not stated"
    gb = out["nvidia-gb200-nvl72:4x:north-america"]
    assert gb.gpu_count == 4 and gb.price_per_gpu_hour == D("10.50") and gb.canonical_gpu_name is None
    # The inference-only column is never read.
    assert all(l.price_per_instance_hour != D("6.16") for l in out.values())
    assert len(out) == 34

    # The two renderings of a row disagree: the row is skipped, not guessed.
    html = text("coreweave_pricing.html").replace("<div>$49.24</div>", "<div>$9.24</div>", 1)
    bad = coreweave.extract(html)
    assert [r["model"] for r in bad["rows"] if r.get("conflict")] == ["NVIDIA HGX H100"]
    assert "nvidia-hgx-h100:8x:north-america" not in by_id(CoreWeaveProvider.normalize({"/pricing": [snap(bad)]}))
    assert raises(coreweave.extract, "<html>redesigned</html>"), "a reshaped page fails the poll"


# --------------------------------------------------------------------------
# Together AI
# --------------------------------------------------------------------------


def test_together():
    payload = together.extract(text("together_pricing.html"))
    out = by_id(TogetherProvider.normalize({"/pricing": [snap(payload)]}))
    assert set(out) == {f"nvidia-hgx-{g}{s}" for g in ("h100", "h200", "b200", "b300") for s in ("", ":preemptible")}
    h100 = out["nvidia-hgx-h100"]
    assert h100.canonical_gpu_name == "NVIDIA H100 80GB SXM5" and h100.price_per_gpu_hour == D("3.99")
    assert h100.gpu_count == 1 and h100.price_per_instance_hour == D("3.99") and h100.market_type == "on_demand"
    assert not h100.interruptible and h100.region is None
    pre = out["nvidia-hgx-h100:preemptible"]
    assert pre.market_type == "spot" and pre.interruptible and pre.price_per_gpu_hour == D("1.99")
    assert out["nvidia-hgx-h200"].canonical_gpu_name == "NVIDIA H200 141GB SXM5"
    assert out["nvidia-hgx-h200"].price_per_gpu_hour == D("5.99")
    assert out["nvidia-hgx-b200"].canonical_gpu_name == "NVIDIA B200 180GB SXM"
    assert out["nvidia-hgx-b200"].price_per_gpu_hour == D("8.19")
    # Reserved terms ($3.69 / $3.45 / $3.19 for H100) are never read.
    assert {l.price_per_gpu_hour for l in out.values()}.isdisjoint({D("3.69"), D("3.45"), D("3.19")})
    # The table must still say it prices per GPU, and keep its column layout.
    assert raises(together.extract, text("together_pricing.html").replace("All prices are per GPU per hour.", ""))
    assert raises(together.extract, text("together_pricing.html").replace(">Preemptible Compute<", ">Spot<"))


def test_together_reserved_raw_only():
    """The reserved columns are stored raw under their own label and read by a pure helper; never a listing."""
    raws = asyncio.run(TogetherProvider(_client("https://www.together.ai", _serve(
        {"/pricing": text("together_pricing.html")}, "text/html"))).fetch())
    assert [r.endpoint for r in raws] == ["/pricing", together.RESERVED_ENDPOINT] and all(r.ok for r in raws)
    res = raws[1].payload
    assert res["terms"] == ["3-30 days", "31-90 days", "91-180 days", "181+ days"] and len(res["rows"]) == 6
    assert res["rows"][0]["reserved"] == {"3-30 days": "$3.69", "31-90 days": "$3.45", "91-180 days": "$3.19",
                                          "181+ days": "Contact us"}, "cells verbatim"
    assert set(res["rows"][3]["reserved"].values()) == {"Contact us"}, "a colspan cell covers every term"
    prices = {(p.hardware, p.term): p for p in together.reserved_prices({together.RESERVED_ENDPOINT: [snap(res)]})}
    assert len(prices) == 9, sorted(prices)  # H100 / H200 / B200 x three priced terms
    h100 = prices[("NVIDIA HGX H100", "31-90 days")]
    assert (h100.price_per_gpu_hour, h100.min_days, h100.max_days) == (D("3.45"), 31, 90)
    assert h100.canonical_gpu_name == "NVIDIA H100 80GB SXM5"
    assert prices[("NVIDIA HGX B200", "91-180 days")].price_per_gpu_hour == D("6.79")
    # Listings are the same with or without the reserved snapshot, and carry no reserved rate.
    both = {"/pricing": [snap(raws[0].payload)], together.RESERVED_ENDPOINT: [snap(res)]}
    assert TogetherProvider.normalize(both) == TogetherProvider.normalize({"/pricing": [snap(raws[0].payload)]})
    assert {l.price_per_gpu_hour for l in TogetherProvider.normalize(both)}.isdisjoint({D("3.69"), D("3.45"), D("3.19")})
    assert TogetherProvider.normalize({together.RESERVED_ENDPOINT: [snap(res)]}) == [], "never read as prices"
    # A reshaped reserved block fails that label alone; the on-demand prices are still stored.
    for broken in (text("together_pricing.html").replace(">31-90 days<", ">1-3 months<"),
                   text("together_pricing.html").replace(
                       '<td><div class="opacity-70"><p data-batch="" class="body-m text-weight-medium">$3.19</p></div></td>', "", 1)):
        page, reserved = asyncio.run(TogetherProvider(_client("https://x", _serve({"/pricing": broken}, "text/html"))).fetch())
        assert page.ok and page.payload == together.extract(text("together_pricing.html"))
        assert not reserved.ok and reserved.payload is None and "ValueError" in reserved.error, reserved.error


# --------------------------------------------------------------------------
# Jarvislabs
# --------------------------------------------------------------------------


def test_jarvislabs():
    """Read from the authenticated misc/server_meta API (it replaced the price-page scraper)."""
    body = json.loads(text("jarvislabs_server_meta.json"))
    out = by_id(JarvislabsProvider.normalize({"/misc/server_meta": [snap(body)]}))
    h100 = out["NVIDIA H100 SXM 80GB:1x:india-noida-01:vm:on_demand"]
    assert h100.canonical_gpu_name == "NVIDIA H100 80GB SXM5", "NAMES keeps the variant the page used to give"
    assert h100.price_per_gpu_hour == D("3.49") and h100.gpu_count == 1 and h100.provider_tier == "vm" and h100.country == "IN"
    spot = out["NVIDIA H100 SXM 80GB:1x:india-noida-01:vm:spot"]
    assert spot.market_type == "spot" and spot.interruptible and spot.price_per_gpu_hour == D("1.54")
    assert out["NVIDIA H200 SXM 141GB:1x:europe-01:any:on_demand"].canonical_gpu_name == "NVIDIA H200 141GB SXM5"
    assert not any(k.startswith("NVIDIA H200 SXM 141GB:1x:europe-01") and k.endswith(":spot") for k in out), "no spot price: no spot listing"
    assert {l.canonical_gpu_name for l in out.values() if l.raw_gpu_name == "NVIDIA L4 24GB"} == {"NVIDIA L4 24GB"}
    for ambiguous in ("NVIDIA A100 80GB", "NVIDIA RTX Pro 6000 Blackwell 96GB", "NVIDIA A100 40GB"):
        assert all(l.canonical_gpu_name is None for l in out.values() if l.raw_gpu_name == ambiguous), ambiguous
    assert all(l.available is None for l in out.values()), "stock lives in capacity, not on the price row"
    # An unknown code keeps its own text and stays unmapped, never guessed.
    odd = json.loads(json.dumps(body))
    odd["server_meta"][0]["gpu_type"] = "MYSTERY-9000"
    names = {l.raw_gpu_name for l in JarvislabsProvider.normalize({"/misc/server_meta": [snap(odd)]})}
    assert "MYSTERY-9000 80GB" in names
    assert raises(JarvislabsProvider.normalize, {"/misc/server_meta": [snap({"oops": []})]})
    cap = JarvislabsProvider.normalize_capacity({"/misc/server_meta": [snap(body)]})
    assert len(cap) == 13 and all(c.count_unit == "gpu" and c.sku.count(":") == 2 for c in cap)
    free = {(c.sku, c.region): c.available_count for c in cap}
    assert free[("NVIDIA H100 SXM 80GB:1x:vm", "india-noida-01")] == 4, "effective free GPUs, as the SDK routes on"
    # the provider's own client carries the key as a Bearer token; fetch stores the response
    assert JarvislabsProvider("k-test")._client.headers["authorization"] == "Bearer k-test"
    raw = asyncio.run(JarvislabsProvider("k-test", httpx.AsyncClient(base_url="https://backendn.jarvislabs.net",
                      transport=httpx.MockTransport(lambda req: httpx.Response(200, json=body)))).fetch())[0]
    assert raw.ok and raw.endpoint == "/misc/server_meta" and raw.payload == body


# --------------------------------------------------------------------------
# Hot Aisle
# --------------------------------------------------------------------------


def test_hotaisle():
    payload = hotaisle.extract(text("hotaisle_pricing.html"))
    assert [c["kind"] for c in payload["cards"]] == ["VM", "VM", "Bare metal"] and payload["michigan_facility"]
    out = by_id(HotAisleProvider.normalize({"/pricing": [snap(payload)]}))
    assert set(out) == {"mi300x:1x:vm", "mi300x:2x:vm", "mi300x:4x:vm"}, "bare metal (unclear terms) is skipped"
    four = out["mi300x:4x:vm"]
    assert four.canonical_gpu_name == "AMD Instinct MI300X 192GB" and four.gpu_count == 4
    assert four.price_per_gpu_hour == D("2.99") and four.price_per_instance_hour == D("11.96"), "per-GPU card x 4"
    assert four.country == "US" and four.market_type == "on_demand" and not four.interruptible
    assert raises(hotaisle.extract, text("hotaisle_pricing.html").replace("$2.99/GPU/hr", "$2.99/hr"))


# --------------------------------------------------------------------------
# Vultr (public API), fetched through a mock transport serving the real bodies
# --------------------------------------------------------------------------


def _serve(routes: dict[str, str], content_type="application/json"):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.raw_path.decode()
        for prefix, body in routes.items():
            if path.startswith(prefix):
                return httpx.Response(200, text=body, headers={"content-type": content_type})
        return httpx.Response(404, text="not found")
    return httpx.MockTransport(handler)


def _client(base, transport):
    return httpx.AsyncClient(base_url=base, transport=transport)


def test_vultr():
    routes = {"/v2/plans?type=vcg": text("vultr_plans_vcg.json"), "/v2/plans-metal": text("vultr_plans_metal.json")}
    prov = VultrProvider(_client("https://api.vultr.com", _serve(routes)))
    raws = asyncio.run(prov.fetch())
    assert [r.endpoint for r in raws] == [VM_ENDPOINT, METAL_ENDPOINT] and all(r.ok for r in raws)
    metal = raws[1].payload["plans_metal"]
    assert metal and all(p.get("gpu_count") for p in metal), "CPU servers are dropped before storage"
    out = by_id(VultrProvider.normalize({r.endpoint: [snap(r.payload)] for r in raws}))
    one, four = out["vcg-l40s-16c-180g-48vram"], out["vcg-l40s-64c-750g-192vram"]
    assert one.canonical_gpu_name == "NVIDIA L40S 48GB" and one.price_per_gpu_hour == D("1.671")
    assert four.gpu_count == 4 and four.price_per_instance_hour == D("6.684") and four.price_per_gpu_hour == D("1.671")
    assert one.market_type == "on_demand" and one.available is None and one.ram_gb == 180
    # Plans Vultr marks not deployable on demand give no listing at all.
    assert "vbm-112c-2048gb-8-h100-gpu" not in out and "vbm-256c-3072gb-8-b200-gpu" not in out
    assert out["vbm-48c-1024gb-4-a100-gpu"].canonical_gpu_name is None, "NVIDIA_A100 hides SXM/PCIe"
    assert set(out) == {"vcg-l40s-16c-180g-48vram", "vcg-l40s-32c-375g-96vram", "vcg-l40s-64c-750g-192vram",
                        "vbm-48c-1024gb-4-a100-gpu"}


# --------------------------------------------------------------------------
# Boost Run, as listed by Shadeform
# --------------------------------------------------------------------------


def test_boostrun():
    payload = json.loads(text("shadeform_boostrun.json"))
    prov = BoostRunProvider(_client("https://api.shadeform.ai", _serve({"/v1/instances/types": json.dumps(payload)})))
    raw = asyncio.run(prov.fetch())[0]
    assert {t["cloud"] for t in raw.payload["instance_types"]} == {"boostrun"}, "other clouds dropped before storage"
    out = by_id(BoostRunProvider.normalize({"/v1/instances/types": [snap(raw.payload)]}))
    h200, b200, l40s = out["H200_sxm5x8_NVLINK"], out["B200_sxm6x8_NVLINK"], out["L40Sx8"]
    assert h200.raw_gpu_name == "H200 sxm" and h200.canonical_gpu_name == "NVIDIA H200 141GB SXM5"
    assert h200.price_per_instance_hour == D("19.60") and h200.price_per_gpu_hour == D("2.45"), "cents -> $ / 8"
    assert b200.canonical_gpu_name == "NVIDIA B200 180GB SXM" and b200.price_per_gpu_hour == D("3.74")
    assert l40s.canonical_gpu_name == "NVIDIA L40S 48GB" and l40s.price_per_gpu_hour == D("0.69")
    assert h200.provider_tier == "baremetal/via-shadeform" and h200.country == "US"
    assert h200.available is False, "Shadeform lists it sold out today: False, kept out of prices"
    assert out["RTX6000Adax8"].canonical_gpu_name is None, "tagged sxm5 by Shadeform: stays unmapped"


# --------------------------------------------------------------------------
# Nebius (fixture trimmed to the page's __NEXT_DATA__ table blocks)
# --------------------------------------------------------------------------


def test_nebius():
    page = text("nebius_prices.html")
    payload = nebius.extract(page)
    assert payload["header"] == ["Item", "vCPUs", "RAM, GB", "GPU-hour"]
    assert len(payload["rows"]) == 9
    out = by_id(NebiusProvider.normalize({"/prices": [snap(payload)]}))
    assert len(out) == 7, sorted(out)

    h100 = out["NVIDIA HGX H100"]
    assert h100.canonical_gpu_name == "NVIDIA H100 80GB SXM5" and h100.price_per_gpu_hour == D("4.50")
    assert h100.gpu_count == 1 and h100.price_per_instance_hour == D("4.50") and h100.market_type == "on_demand"
    assert not h100.interruptible and h100.region is None and h100.provider_tier is None
    assert h100.vcpu == 16 and h100.ram_gb == 200
    assert out["NVIDIA HGX H200"].canonical_gpu_name == "NVIDIA H200 141GB SXM5"
    assert out["NVIDIA HGX H200"].price_per_gpu_hour == D("5.40")
    assert out["NVIDIA HGX B200"].canonical_gpu_name == "NVIDIA B200 180GB SXM"
    assert out["NVIDIA HGX B200"].price_per_gpu_hour == D("8.50")
    assert out["NVIDIA HGX B300"].canonical_gpu_name == "NVIDIA B300 288GB SXM"
    assert out["NVIDIA HGX B300"].price_per_gpu_hour == D("9.50") and out["NVIDIA HGX B300"].ram_gb == 346
    # Workstation or Server Edition is not stated: unmapped, never guessed.
    rtx = out["NVIDIA RTX PRO 6000"]
    assert rtx.canonical_gpu_name is None and rtx.price_per_gpu_hour == D("1.80")
    l40s = out["NVIDIA L40S with Intel CPU"]
    assert l40s.canonical_gpu_name == "NVIDIA L40S 48GB" and l40s.price_per_gpu_hour == D("1.55")
    assert l40s.provider_tier == "from_price" and l40s.vcpu is None and l40s.ram_gb is None, "ranges have no value"
    assert out["NVIDIA L40S with AMD CPU"].price_per_gpu_hour == D("1.82")
    # "Contact us" rows (GB200/GB300 NVL72) give no listing.
    assert not any("NVL72" in k for k in out)
    # The preemptible table ($0.99 / $0.79 / $0.74 / $0.90) is never read.
    assert {l.price_per_gpu_hour for l in out.values()}.isdisjoint({D("0.99"), D("0.79"), D("0.74"), D("0.90")})
    assert all(l.market_type == "on_demand" and not l.interruptible for l in out.values())

    # An announced price change: the dated column wins once its date arrives.
    dated = {"header": payload["header"] + ["GPU-hour (Effective November 1, 2026)"],
             "rows": [r + ["$4.95"] for r in payload["rows"]]}
    before = by_id(NebiusProvider.normalize({"/prices": [snap(dated)]}))
    after = by_id(NebiusProvider.normalize({"/prices": [snap(dated, datetime(2026, 11, 1, tzinfo=timezone.utc))]}))
    assert before["NVIDIA HGX H100"].price_per_gpu_hour == D("4.50")
    assert after["NVIDIA HGX H100"].price_per_gpu_hour == D("4.95") and "NVIDIA GB300 NVL72" in after
    # The pre-October header ("On-demand" plus a dated column) still normalizes.
    old = {"header": ["Item", "vCPUs", "RAM, GB", "On-demand", "GPU-hour (Effective October 1, 2026)"],
           "rows": [["NVIDIA HGX H100", "16", "200", "$2.95", "$4.50"]]}
    assert by_id(NebiusProvider.normalize({"/prices": [snap(old)]}))["NVIDIA HGX H100"].price_per_gpu_hour == D("4.50")

    # Fail closed on a reshaped page. The blocks are a JSON string inside
    # __NEXT_DATA__, so their quotes appear escaped in the HTML.
    def edit(old, new):
        q = lambda s: s.replace('"', '\\"')
        assert q(old) in page, old
        return page.replace(q(old), q(new))

    assert raises(nebius.extract, "<html>redesigned</html>"), "no __NEXT_DATA__"
    assert raises(nebius.extract, edit('"title":"NVIDIA GPU Instances"', '"title":"GPU Instances"')), "no table"
    assert raises(nebius.extract, edit('"GPU-hour "]', '"Reserved, GPU-hour"]')), "not an on-demand column"
    assert not raises(nebius.extract, edit('"Spot price, GPU-hour*"', '"x"')), "the spot table is not checked"
    assert raises(nebius.extract, edit('"title":"Preemptible NVIDIA GPU Instances"',
                                       '"title":"NVIDIA GPU Instances"')), "two candidate tables"
    assert raises(nebius.extract, edit('"16","200","$4.50"]', '"16","200"]')), "a short row"
    bad = nebius.extract(edit('"$4.50"', '"4.50 USD"'))
    assert raises(NebiusProvider.normalize, {"/prices": [snap(bad)]}), "an unparseable price cell fails"

    raw = asyncio.run(NebiusProvider(_client(nebius.BASE_URL, _serve({"/prices": page}, "text/html"))).fetch())[0]
    assert raw.ok and raw.endpoint == "/prices" and raw.payload == payload
    broken = asyncio.run(NebiusProvider(_client(nebius.BASE_URL, _serve({"/prices": "<html/>"}, "text/html"))).fetch())[0]
    assert not broken.ok and "ValueError" in broken.error


# --------------------------------------------------------------------------
# Fetch: the page providers store what extract() returns
# --------------------------------------------------------------------------


def test_fetch_pages():
    cases = [(CoreWeaveProvider, coreweave, "https://www.coreweave.com", "coreweave_pricing.html"),
             (TogetherProvider, together, "https://www.together.ai", "together_pricing.html"),
             (HotAisleProvider, hotaisle, "https://hotaisle.xyz", "hotaisle_pricing.html")]
    for cls, mod, base, fix in cases:
        raw = asyncio.run(cls(_client(base, _serve({"/pricing": text(fix)}, "text/html"))).fetch())[0]
        assert raw.ok and raw.endpoint == "/pricing" and raw.payload == mod.extract(text(fix)), cls.name
        broken = asyncio.run(cls(_client(base, _serve({"/pricing": "<html>new design</html>"}, "text/html"))).fetch())[0]
        assert not broken.ok and broken.payload is None and "ValueError" in broken.error, cls.name


# --------------------------------------------------------------------------
# Registry, classes, provenance
# --------------------------------------------------------------------------


def test_registry():
    assert set(NEW) <= set(PROVIDERS)
    assert mapping.check_mapping_coverage() == {}
    assert set(PROVIDERS) <= set(provider_meta.PROVIDER_META), "every provider is classified"
    cls = {p: provider_meta.meta(p).provider_class for p in NEW}
    assert cls == {"coreweave": "neocloud", "together": "neocloud", "jarvislabs": "neocloud",
                   "hotaisle": "neocloud", "boostrun": "neocloud", "vultr": "general_cloud"}
    assert provider_meta.meta("boostrun").source_type == "aggregator"
    assert "Shadeform" in provider_meta.meta("boostrun").note
    # No cloud is read both directly and through Shadeform (Vultr and CoreWeave are in its catalogue).
    via = {c.cloud for c in PROVIDERS.values() if isinstance(c, type) and issubclass(c, ShadeformCloud)}
    direct = {n for n, c in PROVIDERS.items() if not issubclass(c, ShadeformCloud)}
    assert not (via & direct), via & direct
    assert "vultr" not in via and "paperspace" not in via


# --------------------------------------------------------------------------
# Pipeline: raw storage -> normalize + quality -> rollup -> index votes
# --------------------------------------------------------------------------


def test_pipeline():
    import fetch
    import fixtures
    import normalize
    import scratchdb
    from sqlalchemy import text as sql
    from analytics import indices, rollups

    db = "og_test_provider_pages"
    url = scratchdb.create(db)
    try:
        Session = fixtures.session(url)
        fetch.SessionLocal = Session
        normalize.SessionLocal = Session

        pages = {"coreweave": "coreweave_pricing.html", "together": "together_pricing.html",
                 "hotaisle": "hotaisle_pricing.html"}
        vultr_routes = {"/v2/plans?type=vcg": text("vultr_plans_vcg.json"),
                        "/v2/plans-metal": text("vultr_plans_metal.json")}

        def provs():
            return [CoreWeaveProvider(_client("https://x", _serve({"/pricing": text(pages["coreweave"])}, "text/html"))),
                    TogetherProvider(_client("https://x", _serve({"/pricing": text(pages["together"])}, "text/html"))),
                    JarvislabsProvider("k-test", _client("https://x", _serve({"/misc/server_meta": text("jarvislabs_server_meta.json")}))),
                    HotAisleProvider(_client("https://x", _serve({"/pricing": text(pages["hotaisle"])}, "text/html"))),
                    VultrProvider(_client("https://x", _serve(vultr_routes))),
                    BoostRunProvider(_client("https://x", _serve({"/v1/instances/types": text("shadeform_boostrun.json")})))]

        now = datetime.now(timezone.utc).replace(microsecond=0)
        for when in (now - timedelta(hours=3), now - timedelta(minutes=5)):  # two polls, three hours apart
            responses = []
            for p in provs():
                responses += [r.model_copy(update={"fetched_at": when}) for r in asyncio.run(p.fetch())]
            assert all(r.ok for r in responses), [r.error for r in responses if not r.ok]
            fetch.save(responses)
            normalize.refresh(NEW)

        with Session() as s:
            stored = dict(s.execute(sql("SELECT provider, count(*) FROM raw_snapshots GROUP BY 1")).all())
            bodies = s.execute(sql("SELECT count(*) FROM raw_payloads")).scalar()
            listed = dict(s.execute(sql("SELECT provider, count(*) FROM compute_listings GROUP BY 1")).all())
            held = s.execute(sql("SELECT provider, listing_id, rule FROM quality_quarantine WHERE status = 'pending'")).all()
        # Together stores its reserved columns under their own label next to the price table (2 per poll).
        assert stored == {"coreweave": 2, "together": 4, "jarvislabs": 2, "hotaisle": 2, "vultr": 4, "boostrun": 2}
        assert bodies == 8, "identical bodies from the second poll are stored once"
        # Jarvislabs (API): 13 on-demand rows by region and workload + 11 with a spot price.
        assert listed == {"coreweave": 34, "together": 8, "jarvislabs": 24, "hotaisle": 3, "vultr": 4, "boostrun": 6}, listed
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

        assert voters("NVIDIA H100 80GB SXM5", ".neocloud") == {"coreweave", "together", "jarvislabs"}
        assert voters("NVIDIA H200 141GB SXM5", ".neocloud") == {"coreweave", "together", "jarvislabs"}, \
            "boostrun is sold out on Shadeform today, so it casts no vote"
        assert voters("NVIDIA B200 180GB SXM", ".neocloud") == {"coreweave", "together"}
        assert voters("NVIDIA L40S 48GB", ".neocloud") == {"coreweave"}
        assert voters("NVIDIA L40S 48GB", ".general-cloud") == {"vultr"}
        assert voters("NVIDIA A100 80GB SXM4", ".neocloud") == set(), "every new A100 is unmapped"
        # One vote per provider: CoreWeave's cheapest region, per GPU.
        h100 = votes[indices.gpu_index_id("NVIDIA H100 80GB SXM5") + ".neocloud"]
        assert abs(h100["coreweave"][0] - 6.155) < 1e-9 and abs(h100["together"][0] - 3.99) < 1e-9
        # Spot / preemptible prices land in the spot segment only.
        spot = set(votes.get(indices.gpu_index_id("NVIDIA H100 80GB SXM5", "spot"), {}))
        assert {"coreweave", "together"} <= spot
        assert "hotaisle" in votes.get(indices.gpu_index_id("AMD Instinct MI300X 192GB"), {})
    finally:
        scratchdb.drop(db)


if __name__ == "__main__":
    for t in (test_coreweave, test_together, test_together_reserved_raw_only, test_jarvislabs, test_hotaisle, test_vultr, test_boostrun,
              test_nebius, test_fetch_pages, test_registry, test_pipeline):
        t()
        print(t.__name__, "ok")
