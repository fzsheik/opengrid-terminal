"""Vast and Lium keep the cost, network and interconnect fields they already receive (raw-first).

Fixtures are trimmed copies of REAL responses fetched 2026-11-01 (identifiers replaced):
    tests/fixtures_providers/vast_offer_h100.json   one public /api/v0/bundles/ offer (H100 SXM)
    tests/fixtures_providers/lium_executor_8x.json  one /api/executors executor (8x B300, InfiniBand)
No network, no database.

Run:  .venv/bin/python tests/test_raw_depth.py
"""

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

import providers.vast as vast  # noqa: E402
from providers.lium import ENDPOINT as LIUM_ENDPOINT, LiumProvider  # noqa: E402
from providers.vast import ENDPOINT as VAST_ENDPOINT, VastProvider  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_providers"
NOW = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)


def _vast_raw():
    body = json.loads((FIX / "vast_offer_h100.json").read_text())

    def handler(request: httpx.Request) -> httpx.Response:
        q = json.loads(request.url.params["q"])
        return httpx.Response(200, json=body if q["gpu_name"]["eq"] == "H100 SXM" else {"offers": []})

    pause, vast.RATE_PAUSE_SECONDS = vast.RATE_PAUSE_SECONDS, 0
    try:
        prov = VastProvider(httpx.AsyncClient(base_url="https://x", transport=httpx.MockTransport(handler)))
        out = asyncio.run(prov.fetch())
    finally:
        vast.RATE_PAUSE_SECONDS = pause
    return next(r for r in out if r.request == {"gpu_name": "H100 SXM"}), body["offers"][0]


def test_vast_keeps_cost_and_network_fields():
    raw, offer = _vast_raw()
    kept = raw.payload[0]
    for k in ("storage_cost", "inet_up_cost", "inet_down_cost", "inet_up", "inet_down", "start_date", "end_date",
              "storage_total_cost", "dph_base", "min_bid", "reliability2", "bw_nvlink", "pcie_bw", "gpu_total_ram",
              "cpu_name", "disk_bw"):
        assert k in kept, k
        assert kept[k] == offer.get(k), (k, kept[k])  # verbatim
    assert kept["storage_cost"] == offer["storage_cost"] and kept["inet_up_cost"] > 0
    for k in ("public_ipaddr", "hostname", "score", "duration", "rn", "search", "instance"):
        assert k not in kept, f"{k} must not be stored"


def test_vast_listings_unchanged_by_the_wider_payload():
    raw, offer = _vast_raw()
    snap = SimpleNamespace(endpoint=VAST_ENDPOINT, payload=raw.payload, request=raw.request, fetched_at=NOW)
    listings = {l.provider_tier: l for l in VastProvider.normalize({VAST_ENDPOINT: [snap]})}
    assert set(listings) == {"median", "cheapest"}
    cheap = listings["cheapest"]
    assert float(cheap.price_per_gpu_hour) == offer["dph_total"] / offer["num_gpus"]
    assert cheap.vcpu == int(offer["cpu_cores_effective"]) and cheap.storage_gb == round(offer["disk_space"], 1)
    assert cheap.ram_gb == round(offer["cpu_ram"] / 1024, 1)


def test_full_cost_reads_vast_fields_as_stored():
    """Cross-branch (data/freshness x data/full-cost): the cost reader's Vast components come out of an offer
    exactly as the poller now stores it, so a Vast full-cost row is complete, not a lower bound."""
    from cost import estimate as est

    raw, offer = _vast_raw()
    snap = SimpleNamespace(endpoint=VAST_ENDPOINT, payload=raw.payload, request=raw.request, fetched_at=NOW)
    listings = [dict(provider=l.provider, listing_id=l.listing_id, sku=l.sku, provider_tier=l.provider_tier)
                for l in VastProvider.normalize({VAST_ENDPOINT: [snap]})]
    comps = {c["component"]: c for c in est.vast_components({"vast": {VAST_ENDPOINT: [snap]}}, listings)}
    assert set(comps) == {"block_storage", "egress_internet", "ingress_internet"}, comps  # ingress: the RL estimator
    cheapest = next(x["listing_id"] for x in listings if x["provider_tier"] == "cheapest")
    assert all(c["listing_id"] == cheapest for c in comps.values())
    assert float(comps["block_storage"]["tiers"][0]["price"]) == offer["storage_cost"], comps["block_storage"]
    assert float(comps["egress_internet"]["tiers"][0]["price"]) == offer["inet_up_cost"], comps["egress_internet"]
    assert str(offer["inet_down_cost"])[:6] in comps["egress_internet"]["notes"]
    assert float(comps["ingress_internet"]["tiers"][0]["price"]) == offer["inet_down_cost"], comps["ingress_internet"]


def _lium_raw():
    body = json.loads((FIX / "lium_executor_8x.json").read_text())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body) if request.url.path == "/api/executors" else httpx.Response(404)

    prov = LiumProvider(httpx.AsyncClient(base_url="https://lium.io", transport=httpx.MockTransport(handler)))
    raw = asyncio.run(prov.fetch())[0]  # the executors list (the nodes and stats feeds are 404s here)
    return raw, body[0]


def test_lium_keeps_network_interconnect_and_announced_price():
    raw, e = _lium_raw()
    kept = raw.payload[0]
    assert kept["effective_upload_speed_mbps"] == e["effective_upload_speed_mbps"]
    assert kept["effective_download_speed_mbps"] == e["effective_download_speed_mbps"]
    assert kept["on_infiniband_fabric"] is True and kept["nvlink"] is True
    assert "pending_price_per_hour" in kept and "price_change_effective_date" in kept
    assert kept["interconnect"]["nvlink_links"] == e["interconnect"]["nvlink_links"]
    assert "matrix" not in kept["interconnect"], "the N x N link matrix is dropped"
    assert kept["network"] == {"ema_upload_speed": e["specs"]["network"]["ema_upload_speed"],
                               "ema_download_speed": e["specs"]["network"]["ema_download_speed"]}
    ports = kept["infiniband_ports"]
    assert len(ports) == len(e["specs"]["infiniband_ports"])
    assert {p["link_layer"] for p in ports} == {"InfiniBand", "Ethernet"}
    assert any(p["rate"].startswith("800 Gb/sec") for p in ports)
    assert all(set(p) == {"device", "rate", "link_layer", "state", "phys_state"} for p in ports), "no GUIDs / GIDs"
    assert kept["gpu_memory_mib"] == e["specs"]["gpu"]["details"][0]["capacity"]
    text = json.dumps(raw.payload)
    for secret in ("5FAKEvalidatorHOTKEY", "5FAKEminerHOTKEY", "verified_ports", "md5_checksums", "boot_id"):
        assert secret not in text, secret


def test_lium_listing_unchanged():
    raw, e = _lium_raw()
    snap = SimpleNamespace(endpoint=LIUM_ENDPOINT, payload=raw.payload, request=None, fetched_at=NOW)
    (listing,) = LiumProvider.normalize({LIUM_ENDPOINT: [snap]})
    assert listing.gpu_count == 8 and float(listing.price_per_gpu_hour) == e["price_per_gpu"]
    assert listing.vcpu == e["specs"]["cpu"]["count"] and listing.region == e["location"]["city"]
    (cap,) = LiumProvider.normalize_capacity({LIUM_ENDPOINT: [snap]})
    assert cap.available_count == e["available_gpu_count"] and cap.total_count == 8


def test_lium_old_payload_still_reads():
    """Snapshots stored before the wider _keep (no extra keys) normalize exactly as before."""
    raw, _ = _lium_raw()
    old = [{k: v for k, v in raw.payload[0].items() if k in (
        "id", "machine_name", "price_per_gpu", "gpu_count", "available_gpu_count", "tier", "reliability_score",
        "country_code", "city", "is_spot", "cpu_count", "ram_total", "disk_total")}]
    a = LiumProvider.normalize({LIUM_ENDPOINT: [SimpleNamespace(payload=old, fetched_at=NOW)]})
    b = LiumProvider.normalize({LIUM_ENDPOINT: [SimpleNamespace(payload=raw.payload, fetched_at=NOW)]})
    assert [x.model_dump() for x in a] == [x.model_dump() for x in b]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(name, "ok")
    print("all passed")
