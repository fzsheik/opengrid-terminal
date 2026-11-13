"""Price pages that changed layout in November 2026, read from real copies fetched 2026-11-13.

Each provider's older layout stays covered by its own test file (those fixtures are unchanged); these check the new
one gives the same listings shape, the same listing ids, and the prices the page printed that day.

    hotaisle_pricing_2026-11-13.html   card <p> classes reordered ("text-hot-orange-contrast mb-2 ... uppercase")
    baseten_pricing_2026-11-13.html    "Dedicated Deployments" instanceList replaced by a "Compute pricing" table
    fireworks_pricing_2026-11-13.html  "On demand deployments" table replaced by one "GPU rates (USD)" card
    together_pricing_2026-11-13.html   header "Preemptible" (was "Preemptible Compute"), three reserved terms, NVL72
                                       rows with one uncolspanned "Request Capacity" cell

Run:  .venv/bin/python tests/test_page_layouts_2026_11.py
"""

import os
import sys
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("OPENGRID_NO_JOBS", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from providers import baseten, fireworks, hotaisle, together  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_providers"
NOW = datetime(2026, 11, 13, 5, tzinfo=timezone.utc)


def page(name):
    return (FIX / f"{name}_pricing_2026-11-13.html").read_text(encoding="utf-8")


def listings(module, cls, payload, endpoint=None):
    snap = SimpleNamespace(payload=payload, fetched_at=NOW, request=None)
    return {l.listing_id: l for l in cls.normalize({endpoint or module.ENDPOINT: [snap]})}


def test_hotaisle():
    body = hotaisle.extract(page("hotaisle"))
    assert [(c["kind"], c["counts"], c["price"]) for c in body["cards"]] == [
        ("VM", "1x", "$2.99/GPU/hr"), ("VM", "2x & 4x", "$2.99/GPU/hr"), ("Bare metal", "8x", "$3.39/GPU/hr")]
    out = listings(hotaisle, hotaisle.HotAisleProvider, body)
    assert out and all(l.price_per_gpu_hour == D("2.99") for l in out.values()), "VM cards only, per GPU"


def test_baseten():
    body = baseten.extract(page("baseten"))
    out = listings(baseten, baseten.BasetenProvider, body)
    assert {k: v.price_per_gpu_hour for k, v in out.items()} == {
        "t4": D("0.6312"), "l4": D("0.8484"), "a10g": D("1.2072"), "a100": D("4.00"), "h100": D("6.50"),
        "b200": D("9.98")}, "the MIG slice and CPU-only rows are not GPU listings"
    assert out["b200"].canonical_gpu_name == "NVIDIA B200 180GB SXM"


def test_fireworks():
    out = listings(fireworks, fireworks.FireworksProvider, fireworks.extract(page("fireworks")))
    assert {k: v.price_per_gpu_hour for k, v in out.items()} == {
        "h100-80-gb-gpu": D("8.00"), "h200-141-gb-gpu": D("8.00"), "b200-180-gb-gpu": D("13.00"),
        "b300-288-gb-gpu": D("15.00"), "gb300-288-gb-gpu": D("20.00")}, "ids as before the layout change"


def test_together():
    html = page("together")
    out = listings(together, together.TogetherProvider, together.extract(html))
    assert out["nvidia-hgx-h100"].price_per_gpu_hour == D("3.99") and out["nvidia-hgx-h100"].market_type == "on_demand"
    assert out["nvidia-hgx-h100:preemptible"].price_per_gpu_hour == D("1.99") and out["nvidia-hgx-h100:preemptible"].interruptible
    assert len(out) == 8 and not any("nvl72" in k for k in out), "NVL72 rows print dashes: no price"
    reserved = together.extract_reserved(html)
    assert reserved["terms"] == ["3-30 days", "31-90 days", "91+ days"]
    nvl = next(r for r in reserved["rows"] if r["hardware"] == "NVIDIA GB200 NVL72")
    assert set(nvl["reserved"].values()) == {"Request Capacity"}
    snap = SimpleNamespace(payload=reserved, fetched_at=NOW, request=None)
    prices = {(r.hardware, r.term): r.price_per_gpu_hour for r in together.reserved_prices({together.RESERVED_ENDPOINT: [snap]})}
    assert prices[("NVIDIA HGX B300", "31-90 days")] == D("9.89") and len(prices) == 8


if __name__ == "__main__":
    for t in (test_hotaisle, test_baseten, test_fireworks, test_together):
        t()
        print(t.__name__, "ok")
    print("all passed")
