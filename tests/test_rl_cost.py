"""RL cost view (cost/rl.py, POST /v1/costs/rl-estimate, GET /v1/costs/rl-presets): the formula on hand-computed
fixtures (trainer + rollouts + sandbox + sync), unknown egress / ingress -> lower bound (never $0), the break-even
price and sync rate, the VRAM gate, interruptible rollouts and their overhead, listed capacity and fanout, presets,
input validation, the API's status codes and its public access. Components come from the same saved responses as
tests/test_cost.py; listings are hand-made. No network.

Run:  .venv/bin/python tests/test_rl_cost.py
"""

import os
import sys
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path

os.environ.setdefault("OPENGRID_NO_JOBS", "1")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import scratchdb  # noqa: E402  (isolates the process from real providers before config loads)
import test_cost as tc  # noqa: E402  (fixtures: stored cost sources, provider raw, the listing helper)
from cost import estimate as est  # noqa: E402
from cost import rl  # noqa: E402

H100, L40S, A10 = "NVIDIA H100 80GB SXM5", "NVIDIA L40S 48GB", "NVIDIA A10 24GB PCIe"
COMPS = tc.all_components()[0]
CTX = est.context(tc.provider_raw(), [])


def L(provider, lid, price, count=8, gpu=H100, **kw):
    lst = tc.L(provider, lid, price=price, count=count, gpu=gpu, **kw)
    for k in ("capacity", "capacity_unit", "vcpu", "ram_gb"):
        lst[k] = kw.get(k)
    return lst


def resolve(v):
    import api.common as common
    return common.resolve_gpu(v, strict=False)


def job(**over):
    body = {"trainer": {"gpu": H100, "count": 8, "nodes": 1},
            "rollouts": {"gpu": "auto", "count": 16, "min_vram_gb": 48},
            "weights_gb": 100, "syncs_per_hour": 2, "hours": 10, "storage_gb": 0}
    for k, v in over.items():
        body[k] = {**body[k], **v} if isinstance(v, dict) and isinstance(body.get(k), dict) else v
    return rl.parse(body, resolve)


CW = L("coreweave", "hgx-h100", 49.24)
AWS = L("aws", "p5.48xlarge", 55.04)
VERDA = L("verda", "8H100.80S.176V", 38.34)
RP = L("runpod", "L40S-1", 0.86, count=1, gpu=L40S)
A10L = L("lambda", "gpu_1x_a10", 0.30, count=1, gpu=A10)


def items(p, item):
    return [x for x in p["lines"] if x["item"] == item]


# --------------------------------------------------------------------------- formula

def test_same_cluster_vs_split_hand_computed():
    out = rl.estimate(job(), [CW, AWS, RP, A10L], COMPS, CTX)
    base, split, best = out["same_cluster"], out["split"], out["best"]
    # baseline: CoreWeave trainer 49.24 x 10 + two 8x H100 rollout machines 2 x 49.24 x 10
    assert base["trainer_provider"] == "coreweave" and base["total"] == 1477.20 and not base["total_is_lower_bound"]
    assert items(base, "rollout_compute")[0]["machines"] == 2 and items(base, "sync_intra_cluster")[0]["amount"] == 0
    assert items(base, "sync_intra_cluster")[0]["kind"] == "assumption" and not base["split"]
    # split: same trainer + 16 RunPod L40S x $0.86 x 10 h; 2,000 GB sync, CoreWeave egress $0, RunPod ingress $0
    assert split["trainer_provider"] == "coreweave" and split["rollout_providers"] == ["runpod"]
    assert items(split, "rollout_compute")[0]["amount"] == 137.60 and items(split, "rollout_compute")[0]["gpus"] == 16
    assert split["sync"]["gb_per_destination"] == 2000 and split["sync"]["fanout"] == 1
    assert items(split, "sync_egress")[0]["amount"] == 0 and items(split, "sync_ingress")[0]["amount"] == 0
    assert items(split, "sync_ingress")[0]["kind"] == "manual"
    assert split["total"] == 630.00 and not split["total_is_lower_bound"] and split["unknown"] == []
    assert split["parts"]["trainer"]["amount"] == 492.40 and split["parts"]["rollouts"]["amount"] == 137.60
    assert out["comparison"]["split_vs_same_cluster"] == {"saving": 847.20, "saving_pct": 57.4, "bound": "exact"}
    assert best["total"] == 630.00 and best["strategy"] == "best"
    # every line names its price concept, data kind and where the price came from
    for p in (base, split):
        for x in p["lines"]:
            assert x["price_concept"] == "list_price" and "kind" in x and "known" in x, x
    # break-even: free transfer on this route, so splitting saves at any sync volume
    be = out["break_even"]
    assert be["available"] and be["margin_before_transfer"] == 847.20 and be["break_even_price_per_gb"] == 0.4236
    assert be["splits_save"] is True and be["break_even_syncs_per_hour"] is None and "any sync volume" in be["summary"]
    # the A10 (24 GB) is gated out by min_vram_gb 48
    assert out["rollout_excluded"].get("below_min_vram") == 1
    assert all(r["gpu"] != A10 for r in out["rollout_ranking"]["rows"]) and "not measured" in out["rollout_ranking"]["basis"]
    print("test_same_cluster_vs_split_hand_computed ok")


def test_priced_egress_and_break_even():
    out = rl.estimate(job(), [AWS, RP], COMPS, CTX)
    base, split = out["same_cluster"], out["split"]
    assert base["total"] == round(3 * 550.40, 2) == 1651.20
    # AWS egress: first 100 GB free, then $0.09; RunPod ingress free
    eg = items(split, "sync_egress")[0]
    assert eg["amount"] == round(1900 * 0.09, 2) == 171.00 and eg["kind"] == "observed"
    assert split["total"] == round(550.40 + 137.60 + 171.00, 2) == 859.00 and not split["total_is_lower_bound"]
    assert split["sync"]["egress_price_per_gb"] == round(171 / 2000, 6)
    be = out["break_even"]
    margin = 1651.20 - 688.00
    assert be["margin_before_transfer"] == round(margin, 2) and be["break_even_price_per_gb"] == round(margin / 2000, 6)
    # volume at which AWS egress eats the margin: 100 free, 10,140 GB at $0.09, then $0.085
    gb = 10240 + (margin - 10140 * 0.09) / 0.085
    assert abs(be["break_even_transfer_gb"] - gb) < 0.2, (be, gb)
    assert abs(be["break_even_syncs_per_hour"] - gb / (100 * 10)) < 0.001 and be["splits_save"] is True
    # check by recomputation: just under the break-even rate saves, just over does not
    for s, saves in ((be["break_even_syncs_per_hour"] - 0.01, True), (be["break_even_syncs_per_hour"] + 0.01, False)):
        o = rl.estimate(job(syncs_per_hour=s), [AWS, RP], COMPS, CTX, sensitivity=False)
        assert (o["comparison"]["split_vs_same_cluster"]["saving"] > 0) is saves, (s, o["comparison"])
    print("test_priced_egress_and_break_even ok")


def test_unknown_egress_and_ingress_are_lower_bounds():
    out = rl.estimate(job(), [VERDA, RP], COMPS, CTX)
    split = out["split"]
    eg = items(split, "sync_egress")[0]
    assert eg["amount"] is None and eg["known"] is False, "unknown egress is never $0"
    assert split["unknown"] == ["sync_egress:verda"] and split["total_is_lower_bound"]
    assert split["total"] == round(383.40 + 137.60, 2)
    assert out["comparison"]["split_vs_same_cluster"]["bound"] == "at_most"
    be = out["break_even"]
    assert be["break_even_price_per_gb"] == round((1150.20 - 521.00) / 2000, 6) and be["splits_save"] is None
    assert "not published" in be["summary"]
    assert any("egress price is not published" in c for c in out["caveats"])
    # ingress: RunPod capped at 8 listed GPUs, the rest on AWS (ingress unverified -> unknown); fanout 2 by default
    rp8 = L("runpod", "L40S-1", 0.86, count=1, gpu=L40S, capacity=8, capacity_unit="gpu")
    aws_l40s = L("aws", "g6e.xlarge", 1.00, count=1, gpu=L40S)
    out = rl.estimate(job(), [CW, rp8, aws_l40s], COMPS, CTX)
    split = out["split"]
    ro = items(split, "rollout_compute")
    assert [(r["provider"], r["gpus"]) for r in ro] == [("runpod", 8), ("aws", 8)]
    assert ro[0]["capacity_published"] is True and ro[1]["capacity_published"] is False
    assert split["sync"]["fanout"] == 2 and split["sync"]["transfer_gb"] == 4000 and split["sync"]["destinations"] == ["aws", "runpod"]
    ing = {x["provider"]: x for x in items(split, "sync_ingress")}
    assert ing["aws"]["amount"] is None and "unverified" in ing["aws"]["notes"][0] and ing["runpod"]["amount"] == 0
    assert ing["aws"]["gb"] == 2000 and split["unknown"] == ["sync_ingress:aws"] and split["total_is_lower_bound"]
    assert any("Ingress" in c and "aws" in c for c in out["caveats"])
    assert any("capacity" in n for n in split["notes"])
    # explicit fanout: 3 copies a sync, spread over the two destinations
    s3 = rl.estimate(job(sync_fanout=3), [CW, rp8, aws_l40s], COMPS, CTX, sensitivity=False)["split"]
    assert s3["sync"]["fanout"] == 3 and s3["sync"]["transfer_gb"] == 6000 and s3["sync"]["fanout_basis"] == "given"
    print("test_unknown_egress_and_ingress_are_lower_bounds ok")


def test_vram_gate_interruptible_and_sandbox():
    spot = L("runpod", "L40S-spot", 0.40, count=1, gpu=L40S, market_type="spot", interruptible=True)
    out = rl.estimate(job(), [CW, RP, spot, A10L], COMPS, CTX)
    assert out["rollout_excluded"]["interruptible_not_allowed"] == 1
    assert items(out["split"], "rollout_compute")[0]["listing_id"] == "L40S-1"
    out = rl.estimate(job(rollouts={"interruptible": True}), [CW, RP, spot, A10L], COMPS, CTX)
    r = items(out["split"], "rollout_compute")[0]
    assert r["listing_id"] == "L40S-spot" and r["interruption_overhead"] == 1.15
    assert r["amount"] == round(16 * 0.40 * 10 * 1.15, 2) == 73.60
    assert any("1.15x interruption overhead" in c and "not measured" in c for c in out["caveats"])
    sens = out["sensitivity"]
    assert sens["interruption_overheads"] == [1.0, 1.15, 1.3, 1.5] and sens["syncs_per_hour"] == [1, 2, 4, 8]
    cell = next(c for c in sens["rows"] if c["current"])
    assert cell["split_total"] == out["split"]["total"] and cell["same_cluster_total"] == out["same_cluster"]["total"]
    hi = next(c for c in sens["rows"] if c["interruption_overhead"] == 1.5 and c["syncs_per_hour"] == 2)
    assert hi["split_total"] == round(492.40 + 16 * 0.40 * 10 * 1.5, 2)
    # the overhead never touches on-demand rollouts
    o2 = rl.estimate(job(rollouts={"interruption_overhead": 2.0}), [CW, RP], COMPS, CTX, sensitivity=False)
    assert items(o2["split"], "rollout_compute")[0]["amount"] == 137.60
    # a smaller model: the A10 passes a 16 GB gate and is the cheapest rollout GPU
    o3 = rl.estimate(job(rollouts={"min_vram_gb": 16}), [CW, RP, A10L], COMPS, CTX, sensitivity=False)
    assert items(o3["split"], "rollout_compute")[0]["gpu"] == A10
    # a named rollout GPU only uses that GPU
    o4 = rl.estimate(job(rollouts={"gpu": H100}), [CW, AWS, RP], COMPS, CTX, sensitivity=False)
    assert {x["gpu"] for x in items(o4["split"], "rollout_compute")} == {H100}
    # sandboxes: unknown without a price (lower bound), priced by the user's price, bundled when vCPU/RAM cover it
    sb = {"vcpu": 2, "ram_gb": 4, "count": 8}
    o5 = rl.estimate(job(sandboxes=sb), [CW, RP], COMPS, CTX, sensitivity=False)
    assert items(o5["split"], "sandbox")[0]["amount"] is None and "sandbox" in o5["split"]["unknown"]
    assert any("CPU sandbox prices" in c for c in o5["caveats"])
    o6 = rl.estimate(job(sandboxes={**sb, "price_per_hour": 1.5}), [CW, RP], COMPS, CTX, sensitivity=False)
    s6 = items(o6["split"], "sandbox")[0]
    assert s6["amount"] == 15.0 and s6["kind"] == "user_input" and o6["split"]["total"] == 645.00
    big = L("runpod", "L40S-1", 0.86, count=1, gpu=L40S, vcpu=8, ram_gb=64)
    o7 = rl.estimate(job(sandboxes=sb), [CW, big], COMPS, CTX, sensitivity=False)
    s7 = items(o7["split"], "sandbox")[0]
    assert s7["amount"] == 0 and s7["kind"] == "inferred" and "not free" in s7["notes"][0]
    print("test_vram_gate_interruptible_and_sandbox ok")


def test_trainer_gates_storage_and_best():
    # two nodes: DigitalOcean (no multi-node fabric) is not a trainer; CoreWeave (InfiniBand) is
    do = L("digitalocean", "gpu-h100x8-640gb", 20.0)
    out = rl.estimate(job(trainer={"nodes": 2}), [CW, do, RP], COMPS, CTX, sensitivity=False)
    assert out["split"]["trainer_provider"] == "coreweave" and out["split"]["trainer"]["nodes"] == 2
    assert items(out["split"], "trainer_compute")[0]["amount"] == 984.80
    assert {u["provider"]: u["reason"] for u in out["trainer_unranked"]} == {"digitalocean": "not sold as a multi-node training fabric"}
    assert any("Node-to-node fabric" in c for c in out["caveats"])
    # storage on the trainer's provider: CoreWeave shared FS $0.07/GB-month
    o = rl.estimate(job(storage_gb=1000), [CW, RP], COMPS, CTX, sensitivity=False)
    assert items(o["split"], "trainer_storage")[0]["amount"] == round(1000 * 0.07 * 10 / 730, 2)
    # best can beat both: trainer on Verda is cheapest but its egress is unknown; CoreWeave + RunPod is complete
    o = rl.estimate(job(), [CW, VERDA, RP], COMPS, CTX, sensitivity=False)
    assert o["split"]["trainer_provider"] == "verda" and o["split"]["total_is_lower_bound"]
    assert o["best"]["total"] == 630.00 and not o["best"]["total_is_lower_bound"] and o["best"]["trainer_provider"] == "coreweave"
    clb = o["cheapest_lower_bound"]
    assert clb and clb["total"] < 630.00 and clb["total_is_lower_bound"], "a cheaper lower bound is shown, not ranked first"
    assert o["plans_searched"] > 3
    # same-cluster only (RL3 shape): no split, best is the baseline
    o = rl.estimate(job(same_cluster_only=True), [CW, AWS, RP], COMPS, CTX)
    assert o["split"] is None and o["best"]["total"] == o["same_cluster"]["total"] and "sensitivity" not in o
    # nothing hosts the trainer
    o = rl.estimate(job(), [RP], COMPS, CTX)
    assert o["same_cluster"] is None and o["split"] is None and o["unavailable"]["split"]
    # not enough rollout GPUs anywhere
    o = rl.estimate(job(rollouts={"count": 4096, "gpu": L40S}), [CW, L("runpod", "x", 0.86, count=1, gpu=L40S, capacity=10, capacity_unit="gpu")],
                    COMPS, CTX, sensitivity=False)
    assert o["split"] is None and "of 4096" in o["unavailable"]["split"]
    print("test_trainer_gates_storage_and_best ok")


def test_presets_and_caveats():
    assert list(rl.PRESETS) == ["RL1", "RL2", "RL3"]
    j1 = rl.parse({"preset": "RL1"}, resolve)
    assert j1["trainer"] == {"gpu": H100, "count": 8, "nodes": 1} and j1["rollouts"]["count"] == 32
    assert j1["hours"] == 72 and j1["weights_gb"] == 140 and j1["syncs_per_hour"] == 4
    assert rl.parse({"preset": "RL2"}, resolve)["rollouts"]["min_vram_gb"] == 48
    assert rl.parse({"preset": "RL2"}, resolve)["rollouts"]["count"] == 128
    assert rl.parse({"preset": "RL3"}, resolve)["same_cluster_only"] is True
    # preset fields can be overridden one by one, nested too
    j = rl.parse({"preset": "RL1", "rollouts": {"interruptible": True}, "hours": 24}, resolve)
    assert j["rollouts"]["interruptible"] and j["rollouts"]["count"] == 32 and j["hours"] == 24
    # the sync volume of the guide's example: 140 GB x 4 x 72 = 40,320 GB per destination
    # the worked example in methodology/rl-cost.md
    o = rl.estimate(j1, [CW, RP, L("runpod", "h100-1", 2.69, count=1, gpu=H100)], COMPS, CTX, sensitivity=False)
    assert o["split"]["sync"]["gb_per_destination"] == 40320
    sc, sp = o["same_cluster"], o["split"]
    assert (sc["parts"]["trainer"]["amount"], sc["parts"]["storage"]["amount"], sc["parts"]["rollouts"]["amount"]) == (3545.28, 13.81, 14181.12)
    assert sc["total"] == 17740.21 and sp["parts"]["rollouts"]["amount"] == 6197.76 and sp["total"] == 9756.85
    assert o["comparison"]["split_vs_same_cluster"] == {"saving": 7983.36, "saving_pct": 45.0, "bound": "exact"}
    assert items(sc, "rollout_compute")[0]["machines"] == 4 and items(sp, "sync_egress")[0]["amount"] == 0
    text = " ".join(o["caveats"])
    assert "40" + "-55" not in text and "40–55" not in text and "% of" not in text
    for gap in ("List prices", "throughput", "engineering cost", "full copies"):
        assert gap in text, gap
    print("test_presets_and_caveats ok")


def test_validation():
    bad = [({"colour": 1}, "unknown_field"), ({"preset": "RL9"}, "invalid_preset"),
           ({"trainer": {"gpu": "nope", "count": 8}}, "invalid_trainer_gpu"),
           ({"trainer": {"gpu": H100}}, "missing_trainer_count"),
           ({"trainer": {"gpu": H100, "count": 8.5}}, "invalid_trainer_count"),
           ({"trainer": {"gpu": H100, "count": 8, "nodes": 0}}, "invalid_trainer_nodes"),
           ({"trainer": {"gpu": H100, "count": 8}, "rollouts": "x"}, "invalid_rollouts"),
           ({"trainer": {"gpu": H100, "count": 8}, "rollouts": {"count": 8, "zz": 1}}, "unknown_field"),
           ({"trainer": {"gpu": H100, "count": 8}}, "missing_rollouts_count"),
           ({"preset": "RL1", "rollouts": {"count": 5000}}, "invalid_rollouts_count"),
           ({"preset": "RL1", "rollouts": {"interruption_overhead": 0.9}}, "invalid_rollouts_interruption_overhead"),
           ({"preset": "RL1", "rollouts": {"interruptible": "yes"}}, "invalid_rollouts_interruptible"),
           ({"preset": "RL1", "rollouts": {"rank_by": "vibes"}}, "invalid_rollouts_rank_by"),
           ({"preset": "RL1", "rollouts": {"gpu": "nope"}}, "invalid_rollouts_gpu"),
           ({"preset": "RL1", "hours": 0}, "invalid_hours"), ({"preset": "RL1", "weights_gb": -1}, "invalid_weights_gb"),
           ({"preset": "RL1", "syncs_per_hour": True}, "invalid_syncs_per_hour"),
           ({"preset": "RL1", "sync_fanout": 0}, "invalid_sync_fanout"),
           ({"preset": "RL1", "storage_tb": 1, "storage_gb": 1}, "invalid_storage"),
           ({"preset": "RL1", "storage_class": "tape"}, "invalid_storage_class"),
           ({"preset": "RL1", "sandboxes": {"vcpu": -1, "count": 1}}, "invalid_sandboxes_vcpu"),
           ({"preset": "RL1", "same_cluster_only": 1}, "invalid_same_cluster_only"),
           ({"trainer": {"gpu": H100, "count": 8}, "rollouts": {"count": 8}}, "missing_weights_gb")]
    for body, code in bad:
        try:
            rl.parse(body, resolve)
        except rl.RLInputError as e:
            assert e.code == code, (body, e.code, code)
            continue
        raise AssertionError(f"accepted {body}")
    j = rl.parse({"preset": "RL1", "storage_tb": 3, "sandboxes": None}, resolve)
    assert j["storage_gb"] == 3000 and j["sandboxes"] is None
    print("test_validation ok")


# --------------------------------------------------------------------------- API

DB = "og_test_rl_cost"


def test_api():
    import config
    import fixtures
    import normalize
    from fastapi.testclient import TestClient
    from models import ComputeListing

    url = scratchdb.create(DB)
    try:
        Session = fixtures.session(url)
        normalize.SessionLocal = Session
        now = datetime.now(timezone.utc)

        def row(p, lid, gpu, n, price, **kw):
            return ComputeListing(provider=p, sku=lid, listing_id=lid, raw_gpu_name=gpu, canonical_gpu_name=gpu,
                                  gpu_count=n, region=None, country=None, price_per_gpu_hour=D(str(price)) / n,
                                  price_per_instance_hour=D(str(price)), currency="USD",
                                  market_type=kw.get("market_type", "on_demand"), provider_tier=None,
                                  interruptible=kw.get("interruptible", False), available=True, capacity=None,
                                  capacity_unit=None, vcpu=None, ram_gb=None, storage_gb=None, observed_at=now)
        normalize.save([row("coreweave", "hgx-h100", H100, 8, 49.24), row("runpod", "L40S-1", L40S, 1, 0.86),
                        row("lambda", "a10", A10, 1, 0.30)])
        config.settings.app_password = None
        import main
        from api import pages

        assert "/v1/costs/rl-estimate" in pages.PUBLIC_POST
        client = TestClient(main.app, headers={"X-OpenGrid-Request": "1"})
        body = {"trainer": {"gpu": "h100-80gb-sxm5", "count": 8}, "rollouts": {"count": 16, "min_vram_gb": 48},
                "weights_gb": 100, "syncs_per_hour": 2, "hours": 10}
        r = client.post("/v1/costs/rl-estimate", json=body)
        assert r.status_code == 200, r.text
        b = r.json()
        m, d = b["meta"], b["data"]
        assert m["kind"] == "estimated" and m["methodology"] == "/methodology/rl-cost" and m["price_concept"] == "list_price"
        assert d["same_cluster"]["total"] == 1477.2 and d["split"]["total"] == 630.0
        assert d["comparison"]["split_vs_same_cluster"]["saving"] == 847.2 and d["caveats"]
        assert d["split"]["trainer"]["gpu"] != H100 and d["split"]["trainer"]["gpu_name"] == H100  # public id + name
        assert m["job"]["rollouts"]["interruption_overhead"] == 1.15
        assert client.post("/v1/costs/rl-estimate", json={"preset": "RL1"}).status_code == 200
        # public + CPU-heavy: computed off the event loop (a threadpool worker has no running loop), the listings /
        # components read once per TTL for every job, the result cached per normalized job (never mutated by a reply)
        import asyncio

        from api import cost as api_cost
        calls = {"inputs": 0, "on_loop": []}
        real_inputs, real_load = rl.load_inputs, rl.load

        def counting():
            calls["inputs"] += 1
            return real_inputs()

        def probing(j, inputs=None):
            try:
                asyncio.get_running_loop()
                calls["on_loop"].append(True)
            except RuntimeError:
                calls["on_loop"].append(False)
            return real_load(j, inputs)

        api_cost._rl_inputs.cache_clear()
        api_cost._rl_cached.cache_clear()
        rl.load_inputs, rl.load = counting, probing
        try:
            r1 = client.post("/v1/costs/rl-estimate", json=body).json()
            r2 = client.post("/v1/costs/rl-estimate", json=body).json()
            r3 = client.post("/v1/costs/rl-estimate", json={"preset": "RL2"})
        finally:
            rl.load_inputs, rl.load = real_inputs, real_load
        assert r3.status_code == 200 and r1["data"] == r2["data"] and r1["meta"]["notes"] == r2["meta"]["notes"]
        assert calls == {"inputs": 1, "on_loop": [False, False]}, calls
        for bad, code in (({"preset": "RL9"}, "invalid_preset"), ({"preset": "RL1", "hours": -1}, "invalid_hours"),
                          ({"x": 1}, "unknown_field")):
            r = client.post("/v1/costs/rl-estimate", json=bad)
            assert r.status_code == 422 and code in r.text, (bad, r.status_code, r.text)
        assert client.post("/v1/costs/rl-estimate", content="x", headers={"content-type": "text/plain"}).status_code == 415
        assert client.post("/v1/costs/rl-estimate", content=b"{" + b" " * 20000 + b"}",
                           headers={"content-type": "application/json"}).status_code == 413
        assert client.post("/v1/costs/rl-estimate", content="[1]", headers={"content-type": "application/json"}).status_code == 422
        pr = client.get("/v1/costs/rl-presets").json()
        assert [p["preset"] for p in pr["data"]] == ["RL1", "RL2", "RL3"] and pr["meta"]["methodology"] == "/methodology/rl-cost"
        assert client.get("/methodology/rl-cost").status_code == 200
        assert "RL1" in client.get("/v1/methodology/rl-cost").text
        assert client.get("/rl-cost").status_code == 200 and "/static/pages/rl_cost.js" in client.get("/rl-cost").text
        # public: with a site password set and PUBLIC_PAGES on, the estimate and presets stay open
        config.settings.app_password = "pw"
        old = config.settings.public_pages
        config.settings.public_pages = True
        try:
            anon = TestClient(main.app)
            assert anon.post("/v1/costs/rl-estimate", json={"preset": "RL3"}, headers={"X-OpenGrid-Request": "1"}).status_code == 200
            assert anon.get("/v1/costs/rl-presets").status_code == 200
            config.settings.public_pages = False
            assert anon.post("/v1/costs/rl-estimate", json={"preset": "RL3"}, headers={"X-OpenGrid-Request": "1"}).status_code in (401, 403)
        finally:
            config.settings.app_password = None
            config.settings.public_pages = old
    finally:
        scratchdb.drop(DB)
    print("test_api ok")


if __name__ == "__main__":
    test_same_cluster_vs_split_hand_computed()
    test_priced_egress_and_break_even()
    test_unknown_egress_and_ingress_are_lower_bounds()
    test_vram_gate_interruptible_and_sandbox()
    test_trainer_gates_storage_and_best()
    test_presets_and_caveats()
    test_validation()
    test_api()
    print("all passed")
