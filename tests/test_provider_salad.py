"""SaladCloud capacity: free nodes per GPU class and priority, from the availability POST already fetched for prices.

Runs on trimmed copies of REAL responses (tests/fixtures_providers/salad_gpu_classes.json: 4 of 60 classes from
GET /organizations/{org}/gpu-classes, and salad_availability.json: the matching
POST .../availability/sce-gpu-availability bodies with the request each answered; 2026-11-01). No network, no key.

Run:  .venv/bin/python tests/test_provider_salad.py
"""

import copy
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from providers import PROVIDERS  # noqa: E402
from providers.salad import SaladProvider  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_providers"
NOW = datetime(2026, 11, 1, 21, tzinfo=timezone.utc)
CLASSES = "/organizations/opengrid/gpu-classes"
AVAIL = "/organizations/opengrid/availability/sce-gpu-availability"
RTX4090 = "ed563892-aacd-40f5-80b7-90c9be6c759b"
RTX2080 = "ffc51032-64d2-4df3-855a-f3a649895c0f"


def load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def by_endpoint(classes=None, avail=None):
    classes = load("salad_gpu_classes.json") if classes is None else classes
    avail = load("salad_availability.json") if avail is None else avail
    return {CLASSES: [SimpleNamespace(payload=classes, request=None, fetched_at=NOW)],
            AVAIL: [SimpleNamespace(payload=a["payload"], request=a["request"], fetched_at=NOW) for a in avail]}


def rows(**kw):
    return {r.sku: r for r in SaladProvider.normalize_capacity(by_endpoint(**kw))}


def test_capacity():
    out = rows()
    listings = {l.listing_id: l for l in SaladProvider.normalize(by_endpoint())}
    assert set(out) == set(listings) and len(out) == 16, "one row per class and priority, sku = listing_id"
    hi, med, low = out[f"{RTX4090}:high"], out[f"{RTX4090}:medium"], out[f"{RTX4090}:low"]
    assert (hi.available_count, med.available_count, low.available_count) == (345, 180, 0)
    assert hi.in_stock is True and low.in_stock is False, "zero free at low priority is out of stock there"
    assert hi.count_unit == "instance" and hi.total_count is None and hi.tier is None, "no total, no tier"
    assert hi.raw_value == "available_gpu_high=345; on_call_gpu=0; is_high_demand=False"
    assert hi.canonical_gpu_name == "NVIDIA RTX 4090 24GB" and hi.gpu_count == 1 and hi.region is None
    assert hi.provider == "salad" and hi.observed_at == NOW and hi.source == "sce-gpu-availability"
    for sku, r in out.items():
        assert r.available_count == listings[sku].capacity and r.in_stock == listings[sku].available, sku
    assert out[f"{RTX2080}:batch"].in_stock is False and out[f"{RTX2080}:high"].available_count == 18


def test_high_demand_is_not_stock():
    """is_high_demand is kept in raw_value only: it never sets a tier or in_stock."""
    classes = load("salad_gpu_classes.json")
    for c in classes["items"]:
        c["is_high_demand"] = True
    changed, base = rows(classes=classes), rows()
    for sku, r in changed.items():
        assert r.raw_value.endswith("is_high_demand=True")
        assert (r.in_stock, r.tier, r.available_count) == (base[sku].in_stock, None, base[sku].available_count)


def test_gaps_and_failures():
    avail = load("salad_availability.json")
    # A class whose availability call is missing gets no rows (no statement), the others still do.
    out = rows(avail=[a for a in avail if a["request"]["gpu_classes"][0] != RTX4090])
    assert not any(k.startswith(RTX4090) for k in out) and len(out) == 12
    # A non-numeric count is unknown, never guessed; the word stays in raw_value.
    odd = copy.deepcopy(avail)
    next(a for a in odd if a["request"]["gpu_classes"][0] == RTX4090)["payload"]["available_gpu_high"] = "lots"
    r = rows(avail=odd)[f"{RTX4090}:high"]
    assert r.in_stock is None and r.available_count is None and r.count_unit is None and "lots" in r.raw_value
    # Reshaped classes raise; no raw yet is nothing.
    try:
        rows(classes={"data": []})
        raise AssertionError("a reshaped gpu-classes body must raise")
    except ValueError:
        pass
    assert SaladProvider.normalize_capacity({}) == []
    assert "salad" in PROVIDERS and PROVIDERS["salad"] is SaladProvider


if __name__ == "__main__":
    for t in (test_capacity, test_high_demand_is_not_stock, test_gaps_and_failures):
        t()
        print(t.__name__, "ok")
    print("all passed")
