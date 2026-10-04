"""Verda and the Shadeform-backed clouds, on small hand-built payloads.

Run:  .venv/bin/python tests/test_providers.py
"""

import sys
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mapping
from providers import PROVIDERS
from providers.shadeform import CrusoeProvider, DenvrProvider, LatitudeProvider, gpu_label, regions_of
from providers.verda import VerdaProvider

NOW = datetime(2026, 10, 4, tzinfo=timezone.utc)


def snap(payload):
    return SimpleNamespace(payload=payload, request=None, fetched_at=NOW)


def verda_type(itype, n, price, spot, name="H100 SXM5 80GB", currency="usd", gpus=True):
    return {"instance_type": itype, "name": name, "currency": currency, "price_per_hour": price, "spot_price": spot,
            "gpu": {"number_of_gpus": n if gpus else 0}, "cpu": {"number_of_cores": 30}, "memory": {"size_in_gigabytes": 120}}


def test_verda():
    payload = [
        verda_type("1H100.80S.30V", 1, "3.737", "1.869"),
        verda_type("8H100.80S.176V", 8, "29.90", "14.95"),
        verda_type("CPU.8V", 0, "0.10", "0.05", gpus=False),
        verda_type("8B200.240V.CC", 8, "57.87", "28.94", name="B200 CC SXM6 180GB"),
        verda_type("EUR.1", 1, "2.00", "1.00", currency="eur"),
        verda_type("NOSPOT.1", 1, "2.00", None),
    ]
    out = {l.listing_id: l for l in VerdaProvider.normalize({"/v1/instance-types": [snap(payload)]})}
    assert "CPU.8V" not in out, "CPU-only types are not GPU listings"
    assert not any(k.startswith("EUR") for k in out), "a non-USD price is skipped, never read as dollars"
    assert out["8H100.80S.176V"].price_per_gpu_hour == D("29.90") / 8, "per-instance price divides by GPU count"
    assert out["8H100.80S.176V"].price_per_instance_hour == D("29.90")
    spot = out["8H100.80S.176V:spot"]
    assert spot.market_type == "spot" and spot.interruptible and spot.price_per_instance_hour == D("14.95")
    assert out["1H100.80S.30V"].market_type == "on_demand" and not out["1H100.80S.30V"].interruptible
    assert out["1H100.80S.30V"].canonical_gpu_name == "NVIDIA H100 80GB SXM5"
    assert out["8B200.240V.CC"].provider_tier == "confidential" and out["8B200.240V.CC"].canonical_gpu_name is None
    assert "NOSPOT.1" in out and "NOSPOT.1:spot" not in out, "no spot price, no spot listing"
    assert all(l.available is None for l in out.values()), "Verda stock is unknown, not sold out"


def sf_type(cloud, shade, cloud_type, gpu_type, n, cents, interconnect="pcie", nvlink=False, avail=None, dep="vm"):
    return {"cloud": cloud, "shade_instance_type": shade, "cloud_instance_type": cloud_type, "gpu_type": gpu_type,
            "num_gpus": n, "hourly_price": cents, "interconnect": interconnect, "nvlink": nvlink, "vcpus": 8,
            "memory_in_gb": 64, "storage_in_gb": 500, "deployment_type": dep,
            "availability": avail if avail is not None else [{"region": "r", "available": True, "display_name": "US, Dallas, TX", "rental_type": "on_demand"}]}


def test_shadeform():
    # The SXM8 type is tagged pcie by Shadeform; its own name says sxm, and that wins.
    mislabeled = sf_type("crusoe", "A100_80G_sxm4x8", "a100-80gb-sxm-ib.8x", "A100_80G", 8, 1840)
    assert gpu_label(mislabeled) == "A100_80G sxm"
    assert gpu_label(sf_type("x", "H100x1", "h", "H100", 1, 1, nvlink=True)) == "H100 pcie nvlink"
    assert gpu_label(sf_type("x", "H100x1", "h", "H100", 1, 1, nvlink=False)) == "H100 pcie"

    payload = {"instance_types": [
        mislabeled,
        sf_type("crusoe", "L40Sx1", "l40s.1x", "L40S", 1, 150),
        sf_type("crusoe", "CPU", "cpu", "CPU", 0, 50),                                    # no GPUs
        sf_type("latitude", "H100x1", "vm-h100", "H100", 1, 199),                         # another cloud
        sf_type("crusoe", "NOPRICE", "np", "A40", 1, None),
    ]}
    out = {l.listing_id: l for l in CrusoeProvider.normalize({"/v1/instances/types": [snap(payload)]})}
    assert set(out) == {"A100_80G_sxm4x8", "L40Sx1"}, "only this cloud's priced GPU types: " + str(set(out))
    a = out["A100_80G_sxm4x8"]
    assert a.price_per_instance_hour == D("18.40") and a.price_per_gpu_hour == D("2.30"), "cents to dollars, then per GPU"
    assert a.canonical_gpu_name == "NVIDIA A100 80GB SXM4", "the SXM name, not the wrong pcie tag"
    assert a.provider == "crusoe" and a.provider_tier == "vm/via-shadeform" and a.available is True
    assert LatitudeProvider.normalize({"/v1/instances/types": [snap(payload)]})[0].price_per_gpu_hour == D("1.99")
    assert DenvrProvider.normalize({"/v1/instances/types": [snap(payload)]}) == []


def test_regions():
    up = {"region": "a", "available": True, "display_name": "US, Dallas, TX", "rental_type": "on_demand"}
    down = {"region": "b", "available": False, "display_name": "DE, Frankfurt", "rental_type": "on_demand"}
    spot = {"region": "c", "available": True, "display_name": "US, Austin, TX", "rental_type": "spot"}
    assert regions_of([up, down], "on_demand") == (True, "US, Dallas, TX", "US"), "available regions are shown first"
    assert regions_of([down], "on_demand") == (False, "DE, Frankfurt", None), "sold out is False, with its regions"
    assert regions_of([up, spot], "on_demand")[1] == "US, Dallas, TX", "spot entries do not leak into on-demand"
    assert regions_of([], "on_demand") == (None, None, None), "no entries: unknown, not sold out"
    many = [{"region": str(i), "available": True, "display_name": f"US, City Number {i}, TX", "rental_type": "on_demand"} for i in range(20)]
    text = regions_of(many, "on_demand")[1]
    assert len(text) <= 64 and text.endswith("…"), "long region lists fit a varchar(64)"
    # A type offered only as spot becomes a spot listing, interruptible
    only_spot = {"instance_types": [sf_type("denvr", "S1", "s1", "H100", 1, 100, avail=[spot])]}
    l = DenvrProvider.normalize({"/v1/instances/types": [snap(only_spot)]})[0]
    assert l.market_type == "spot" and l.interruptible is True


def test_registry():
    assert {"verda", "crusoe", "latitude", "denvr"} <= set(PROVIDERS)
    assert mapping.check_mapping_coverage() == {}
    # A cloud with its own feed must not also be read through Shadeform
    shadeform_clouds = {cls.cloud for cls in (CrusoeProvider, LatitudeProvider, DenvrProvider)}
    direct = {name for name in PROVIDERS if PROVIDERS[name] not in (CrusoeProvider, LatitudeProvider, DenvrProvider)}
    assert not (shadeform_clouds & direct), f"double-counted: {shadeform_clouds & direct}"


if __name__ == "__main__":
    for t in (test_verda, test_shadeform, test_regions, test_registry):
        t(); print(t.__name__, "ok")
