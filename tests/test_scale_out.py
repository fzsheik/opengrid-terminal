"""Consumer GPU tensor specs and scale-out options (hardware.py; intelligence/scale_out.py; methodology/hardware.md and
methodology/recommendations.md "Scale-out").

Pure (no database):
    specs          every listed GeForce row has dense FP16/BF16 (FP32 accumulate) and, Ada / Blackwell, FP8 from an NVIDIA
                   source (whitepaper table, or the product page's AI TOPS with the whitepaper ratio); the exact
                   whitepaper figures; fp8 = 2 x fp16 as on the datacenter rows; the unsourced rows stay null
    performance    the RTX 5070 Ti now gets a theoretical training runtime and a Whisper runtime, hand-computed
    sizing         wall time, replicas for a deadline / the default target, the capacity + diversification cap,
                   cheapest-first allocation, the whole block (costs, comparison text) hand-computed
    eligibility    never for training, latency-bound serving, a fixed GPU count or open-ended serving
    llm batch      total output / prompt tokens from the request
Database (scratch og_test_scale_out):
    recommend      Whisper 10k h and a Llama 8B AWQ batch get scale-out plans (replicas >= 2, spread under the cap,
                   batch body), the explanation compares them with the single machines; a card listed by one provider
                   only is skipped; never offered for training / a model that needs several GPUs / latency targets;
                   LLM batch runtime hand-checked; measured runtime wins over theoretical for a card's replicas
    golden         payloads of requests that existed before this change are byte-identical once the added
                   `scale_out` keys are removed (tests/fixtures_scale_out/golden_c89801f.json, from c89801f)
    visibility     public / non-admin answers are sized on public listings only and do not change with an internal
                   price; the operator sees the internal listing, labelled
Run:  .venv/bin/python tests/test_scale_out.py
"""

import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("OPENGRID_NO_JOBS", "1")
os.environ.setdefault("POLLER_ENABLED", "false")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures_scale_out"))

import scratchdb  # noqa: E402  (first: isolates the process from real providers)
import fixtures  # noqa: E402
import hardware  # noqa: E402
import normalize  # noqa: E402
import so_market  # noqa: E402
from accounts.auth import OPERATOR, PUBLIC, Principal  # noqa: E402
from config import settings  # noqa: E402
from intelligence import media as MD  # noqa: E402
from intelligence import performance as P  # noqa: E402
from intelligence import scale_out as SO  # noqa: E402
from intelligence.estimate import estimate  # noqa: E402
from intelligence.recommend import recommend  # noqa: E402
from intelligence.requirements import WorkloadRequirements as W  # noqa: E402
from routing import disclosure, scoring  # noqa: E402
from tables import ComputeListingRow  # noqa: E402

DB = os.environ.get("OG_TEST_SCALE_OUT_DB", "og_test_scale_out")
NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
R5090, R5080, R5070TI, R5070 = ("NVIDIA RTX 5090 32GB", "NVIDIA RTX 5080 16GB", "NVIDIA RTX 5070 Ti 16GB",
                                "NVIDIA RTX 5070 12GB")
R5060TI, R5060 = "NVIDIA RTX 5060 Ti 16GB", "NVIDIA RTX 5060 8GB"
R4090, R4080S, R4080, R4070TIS = ("NVIDIA RTX 4090 24GB", "NVIDIA RTX 4080 SUPER 16GB", "NVIDIA RTX 4080 16GB",
                                  "NVIDIA RTX 4070 Ti SUPER 16GB")
R4070TI, R4070, R4060TI, R4060 = ("NVIDIA RTX 4070 Ti 12GB", "NVIDIA RTX 4070 12GB", "NVIDIA RTX 4060 Ti 16GB",
                                  "NVIDIA RTX 4060 8GB")
R3090TI, R3090, R3080TI, R3080 = ("NVIDIA RTX 3090 Ti 24GB", "NVIDIA RTX 3090 24GB", "NVIDIA RTX 3080 Ti 12GB",
                                  "NVIDIA RTX 3080 10GB")
R3070TI, R3070 = "NVIDIA RTX 3070 Ti 8GB", "NVIDIA RTX 3070 8GB"
V100S, A4000, H100, L4 = ("NVIDIA Tesla V100S 32GB PCIe", "NVIDIA RTX A4000 16GB", "NVIDIA H100 80GB SXM5",
                          "NVIDIA L4 24GB")
KEY = Principal(kind="api_key", account_id=77, key_id=5, scopes=frozenset({"data:read"}))


def close(a, b, tol=1e-6):
    assert abs(a - b) <= tol * max(1.0, abs(b)), (a, b)


# ------------------------------------------------------------------ specs

# (fp16 = bf16 dense FP32-accumulate, fp8 dense FP32-accumulate) printed in NVIDIA's whitepaper tables
WHITEPAPER = {R5090: (209.5, 419), R5080: (112.6, 225.1), R5070TI: (87.9, 175.8), R5070: (61.7, 123.5),
              R4090: (165.2, 330.3), R4080: (97.5, 194.9), R4070TI: (80.2, 160.4), R4070: (58.3, 116.6),
              R3090TI: (80.0, None), R3090: (71, None), R3080TI: (68.2, None), R3080: (59.5, None),
              R3070TI: (43.5, None), R3070: (40.6, None)}
# NVIDIA product-page AI TOPS and the whitepaper ratio (Blackwell FP4 sparse / 16, Ada FP8 sparse / 8)
AI_TOPS = {R5060TI: (759, 16), R5060: (614, 16), R4080S: (836, 8), R4070TIS: (706, 8), R4060TI: (353, 8),
           R4060: (242, 8)}
UNSOURCED = ("NVIDIA RTX 3060 Ti 8GB", "NVIDIA RTX 3060 12GB", "NVIDIA RTX 3060 8GB", "NVIDIA RTX 3050 8GB")


def test_specs_consumer_rows_sourced():
    for g, (f16, f8) in WHITEPAPER.items():
        s = hardware.spec(g)
        assert s["fp16_tflops_dense"] == f16 and s["bf16_tflops_dense"] == f16, (g, s["fp16_tflops_dense"])
        assert s["fp8_tflops_dense"] == f8, (g, s["fp8_tflops_dense"])
        assert s["tensor_basis"] == "whitepaper" and any("nvidia.com" in u and u.endswith(".pdf") for u in s["sources"]), g
        assert "FP32-accumulate" in s["notes"], g
    for g, (tops, div) in AI_TOPS.items():
        s = hardware.spec(g)
        close(s["fp16_tflops_dense"], tops / div, tol=2e-3)
        close(s["fp8_tflops_dense"], 2 * tops / div, tol=2e-3)
        assert s["tensor_basis"] == "nvidia_ai_tops" and f"{tops} AI TOPS" in s["notes"], g
        assert any(u.startswith("https://www.nvidia.com/en-us/geforce/graphics-cards/") for u in s["sources"]), g
    # the ratio the derivation relies on holds for every whitepaper SKU (AI TOPS printed on the compare page)
    for g, tops, div in ((R5090, 3352, 16), (R5080, 1801, 16), (R5070TI, 1406, 16), (R5070, 988, 16),
                         (R4090, 1321, 8), (R4080, 780, 8), (R4070TI, 641, 8), (R4070, 466, 8)):
        close(hardware.spec(g)["fp16_tflops_dense"], tops / div, tol=2e-3)
    for g in list(WHITEPAPER) + list(AI_TOPS):
        s = hardware.spec(g)
        if s["fp8_tflops_dense"]:
            close(s["fp8_tflops_dense"], 2 * s["fp16_tflops_dense"], tol=2e-3)      # FP32-accumulate FP8 = 2x
            assert s["fp8_supported"] is True
    h = hardware.spec(H100)
    assert (h["bf16_tflops_dense"], h["fp8_tflops_dense"]) == (989.5, 1979), "datacenter rows untouched"
    for g in UNSOURCED:
        s = hardware.spec(g)
        assert s["fp16_tflops_dense"] is None and s["bf16_tflops_dense"] is None and "tensor_basis" not in s, g
        assert "no per-SKU tensor figure" in s["notes"], g


def test_performance_runtime_consumer():
    s = hardware.spec(R5070TI)
    wk = {"flops": 4 * 8e9 * 1e8, "formula": "4 x N x T", "assumptions": []}
    perf = P.option_performance(s, 1, method="training_lora", precision="bf16", wk=wk, params_b=8.0)
    assert perf["runtime"]["known"], perf["runtime"]
    close(perf["runtime"]["hours"], 4 * 8e9 * 1e8 / (87.9e12 * 0.35) / 3600, tol=1e-3)
    e = estimate(W.model_validate(so_market.GOLDEN_REQUESTS["whisper_10k"]))
    prof = e["memory"]["profile"]
    for g in (R5070TI, R5060TI, R4060):
        r = MD.option_runtime(hardware.spec(g), 1, 1, prof, "single_gpu")
        assert r["known"] and r["hours"] > 0, (g, r)


# ------------------------------------------------------------------ sizing, hand-computed

T1 = (100.0, 50.0, 300.0)


def test_sizing_hand_computed():
    w = SO.wall(T1, 10)
    close(w[0], 0.25 + 0.05 + 100 * 1.10 / (10 * 0.98))
    close(w[1], 0.10 + 0.02 + 50 * 1.05 / (10 * 1.00))
    close(w[2], 0.50 + 0.15 + 300 * 1.25 / (10 * 0.90))
    b = 100 * 1.10 / 0.98                                   # 112.245 single-GPU hours with the allowances
    assert SO.replicas_for(T1, 12.0) == math.ceil(b / 11.7) == 10
    assert SO.replicas_for(T1, 0.3) is None, "the fixed overhead alone exceeds the target"
    units = {"a": 6, "b": 5}
    d = SO.choose_n(T1, units, max_n=64, deadline=12.0, target_h=None)
    assert (d["n"], d["needed"], d["rule"], d["capped_by_capacity"]) == (10, 10, "deadline", False), d
    d = SO.choose_n(T1, units, max_n=64, deadline=11.0, target_h=None)
    assert d["needed"] == math.ceil(b / 10.7) == 11 and d["n"] == 10 and d["capped_by_capacity"], d
    d = SO.choose_n(T1, units, max_n=64, deadline=None, target_h=20.0)
    assert (d["n"], d["needed"], d["rule"]) == (6, math.ceil(b / 19.7), "match_fastest_single_node"), d
    d = SO.choose_n(T1, units, max_n=4, deadline=None, target_h=20.0)
    assert d["n"] == 4 and d["capped_by_capacity"], "BATCH_MAX_SHARDS / max_gpus cap"
    d = SO.choose_n((1.0, 0.5, 3.0), units, max_n=64, deadline=None, target_h=50.0)
    assert d["n"] == SO.MIN_REPLICAS, "never fewer than 2 replicas: one is not a scale-out"


def test_capacity_cap():
    assert SO.provider_cap(10) == 5 and SO.provider_cap(3) == 1 and SO.provider_cap(1) == 1
    assert SO.capacity_cap({"a": 3, "b": 3, "c": 3}, 64)["max"] == 9          # n=10: cap 5, 3+3+3 < 10
    assert SO.capacity_cap({"a": 3, "b": 3, "c": 3}, 4)["max"] == 4
    assert SO.capacity_cap({"a": 50}, 64)["max"] == 1, "one provider: the 50% cap allows a single replica"
    r = SO.choose_n(T1, {"a": 50}, max_n=64, deadline=None, target_h=20.0)
    assert r["n"] is None and "diversification cap" in r["reason"]
    li = [{"provider": p, "listing_id": p, "price": pr, "units": 5} for p, pr in (("a", 0.1), ("b", 0.2), ("c", 0.3))]
    al = SO.allocate(li, 4)
    assert [x["provider"] for x in al] == ["a", "a", "b", "b"], "cheapest first, at most 2 per provider for 4"
    assert SO.allocate(li[:1], 4) is None


def _card(gpu, listings, t1=T1, kind="theoretical"):
    return {"gpu": gpu, "spec": hardware.spec(gpu), "T1": t1, "runtime_kind": kind, "measured_confidence": None,
            "listings": [{"provider": p, "provider_display": p.title(), "listing_id": f"{p}:{i}", "price": pr,
                          "units": u, "units_reported": u > 1, "available": True, "visibility": "public", "bill": None}
                         for i, (p, pr, u) in enumerate(listings)]}


SINGLES = [{"option_id": "v100s:clore:1", "gpu": V100S, "n": 1, "provider_display": "Clore.ai", "hours": 360.0,
            "range_hours": [100.0, 900.0], "usd": 21.6, "range_usd": [6.0, 54.0]},
           {"option_id": "h100:vast:2", "gpu": H100, "n": 2, "provider_display": "Vast.ai", "hours": 43.2,
            "range_hours": [10.0, 120.0], "usd": 159.88, "range_usd": [40.0, 444.0]}]


def test_build_hand_computed():
    card = _card(R5070TI, [("clore", 0.14, 1), ("clore", 0.15, 1), ("clore", 0.25, 1),
                           ("lium", 0.17, 1), ("lium", 0.19, 1)])
    blk = SO.build(kind="media_batch", cards=[card], deadline=None, max_n=64, singles=SINGLES,
                   billed=lambda li, h: h, template=None, supported=False, req_name="openai/whisper-large-v3",
                   budget=None, work_label="1,200,000 windows")
    assert blk["offered"] and len(blk["options"]) == 1
    o = blk["options"][0]
    # target 43.2 h: need ceil(112.245 / 42.9) = 3; n=3 has cap 1 -> 2 placed < 3; n=4: cap 2 -> 2 + 2
    assert o["sizing"]["replicas_needed"] == 3 and o["replicas"] == 4 and o["sizing"]["in_reach"], o["sizing"]
    assert [(p["provider"], p["replicas"]) for p in o["providers"]] == [("clore", 2), ("lium", 2)]
    per_hour = 0.14 + 0.15 + 0.17 + 0.19
    close(o["price_per_hour_total"], per_hour)
    w = SO.wall(T1, 4)
    close(o["completion_hours"], round(w[0], 3))
    close(o["job_cost_usd"], round(per_hour * w[0], 2))
    assert o["job_cost_range_usd"] == [round(per_hour * w[1], 2), round(per_hour * w[2], 2)]
    assert o["runtime_kind"] == "theoretical" and o["confidence"]["level"] == "low" and o["kind"] == "estimated"
    for k in ("replica_setup_hours", "data_sharding_hours", "straggler_allowance", "replica_efficiency"):
        assert k in o["overheads"], k
    hb = o["batch_hint"]
    assert hb["endpoint"] == "POST /v1/batches/preview" and not hb["runnable_today"]
    assert len(hb["body"]["shards"]) == 4 and hb["body"]["concurrency"]["max_running"] == 4
    assert hb["body"]["placement"] == {"max_share_per_provider": 0.5, "include_providers": ["clore", "lium"]}
    assert hb["body"]["limits"]["max_price_per_gpu_hour_usd"] == 0.19
    assert blk["picks"]["best_scale_out"]["scale_out_id"] == o["scale_out_id"]
    t = blk["comparison"]["text"]
    assert t.startswith(f"4 x RTX 5070 Ti 16GB across 2 provider(s): ~{w[0]:.3g} h, ~${per_hour * w[0]:,.2f} vs "
                        "1 x Tesla V100S 32GB PCIe on Clore.ai (lowest-cost single machine): ~360 h, ~$21.60; vs "
                        "2 x H100 80GB SXM5 on Vast.ai (fastest single machine): ~43.2 h, ~$159.88"), t
    dc = per_hour * w[0] - 21.6
    word = f"${-dc:,.2f} cheaper" if dc < 0 else f"${dc:,.2f} more"
    assert f"about {360 / w[0]:.2g}x sooner and {word} than the lowest-cost single machine" in t, t
    assert f"about {43.2 / w[0]:.2g}x sooner and ${159.88 - per_hour * w[0]:,.2f} cheaper than the fastest" in t, t
    # with a deadline the plan is sized to it and flagged
    blk = SO.build(kind="media_batch", cards=[card], deadline=12.0, max_n=64, singles=SINGLES,
                   billed=lambda li, h: h, template=None, supported=False, req_name=None, budget=None,
                   work_label="x")
    o = blk["options"][0]
    assert o["replicas"] == 4 and o["meets_deadline"] == "no" and o["sizing"]["capped_by_capacity"], o["sizing"]
    assert any("deadline" in x for x in o["limitations"])


def test_build_ranking_in_reach_first():
    slow_cheap = _card(R5060TI, [("clore", 0.09, 1), ("vast", 0.12, 1)], t1=(400.0, 200.0, 1200.0))
    fast = _card(R3090, [("clore", 0.10, 4), ("vast", 0.15, 4), ("lium", 0.16, 4)], t1=(150.0, 75.0, 450.0))
    blk = SO.build(kind="media_batch", cards=[slow_cheap, fast], deadline=None, max_n=64, singles=SINGLES,
                   billed=lambda li, h: h, template=None, supported=False, req_name=None, budget=None,
                   work_label="x")
    ids = [o["gpu"] for o in blk["options"]]
    assert ids[0] == R3090, "a plan that reaches the target beats a cheaper one capped at 2 replicas"
    assert blk["options"][0]["sizing"]["in_reach"] and not blk["options"][1]["sizing"]["in_reach"]


def test_eligibility():
    e = lambda **k: SO.eligibility(**{**dict(method="inference", media_batch=False, llm_known=True,  # noqa: E731
                                             latency_target=False, fixed=False, has_pool=True), **k})
    assert e() == (True, None, "llm_batch") and e(media_batch=True, llm_known=False)[2] == "media_batch"
    assert not e(method="training_lora")[0] and "training run" in e(method="training_full")[1]
    assert not e(latency_target=True)[0] and "latency" in e(latency_target=True)[1]
    assert not e(fixed=True)[0] and not e(has_pool=False)[0]
    assert not e(llm_known=False)[0] and "total amount of work" in e(llm_known=False)[1]


def test_llm_batch_work():
    body = {"workload_type": "batch", "model": {"name": "meta-llama/Llama-3.1-8B-Instruct"}, "quantization": "awq",
            "inference": {"concurrency": 16, "context_len": 2048, "output_tokens": 512, "total_requests": 1000}}
    req = W.model_validate(body)
    prof = estimate(req)["memory"]["profile"]
    w = SO.llm_batch_work(req, prof)
    assert w["known"] and w["output_tokens"] == 512_000 and w["input_tokens"] == 1000 * (2048 - 512)
    req = W.model_validate({**body, "inference": {"total_output_tokens": 1e9}})
    w = SO.llm_batch_work(req, estimate(req)["memory"]["profile"])
    assert w["known"] and w["output_tokens"] == 1e9 and w["input_tokens"] is None and w["assumptions"]
    req = W.model_validate({**body, "inference": {"total_requests": 10}})
    assert not SO.llm_batch_work(req, estimate(req)["memory"]["profile"])["known"]
    req = W.model_validate({**body, "inference": {"concurrency": 4}})
    assert not SO.llm_batch_work(req, estimate(req)["memory"]["profile"])["known"]
    e = estimate(W.model_validate(body))
    assert e["runtime"]["estimable"] is True, e["runtime"]


# ------------------------------------------------------------------ database

def L(prov, gpu, price, *, count=1, cap=None, vis="public", lid=None):
    return ComputeListingRow(
        provider=prov, listing_id=lid or f"{prov}:{gpu}:{count}:{price}", sku=f"{prov}-sku-{gpu}-{price}",
        raw_gpu_name=gpu, canonical_gpu_name=gpu, gpu_count=count, region="us-east", country="US",
        price_per_gpu_hour=Decimal(str(price)), price_per_instance_hour=Decimal(str(round(price * count, 6))),
        currency="USD", market_type="on_demand", provider_tier=None, interruptible=False, available=True,
        capacity=cap, capacity_unit="gpu" if cap else None, vcpu=None, ram_gb=None, storage_gb=None, visibility=vis,
        observed_at=NOW - timedelta(minutes=2), first_seen_at=NOW - timedelta(days=5))


INTERNAL_ID = "syn_i:internal-5070ti"
R_ONE = R4090          # listed by one provider only: no scale-out under the 50% cap


def seed(Session):
    rows = ([L("syn_a", R5070TI, p) for p in (0.14, 0.15, 0.16, 0.18, 0.20, 0.25)]
            + [L("syn_b", R5070TI, p) for p in (0.17, 0.19, 0.21, 0.22, 0.24)]
            + [L("syn_a", R3090, 0.07), L("syn_b", R3090, 0.15), L("syn_c", R3090, 0.22, cap=8)]
            + [L("syn_a", V100S, 0.06), L("syn_b", V100S, 0.30)]
            + [L("syn_a", A4000, 0.08), L("syn_c", A4000, 0.15, cap=6)]
            + [L("syn_a", R_ONE, 0.05), L("syn_a", R_ONE, 0.06)]
            + [L("syn_a", L4, 0.50), L("syn_b", L4, 0.55)]
            + [L("syn_a", H100, 2.40), L("syn_b", H100, 2.69), L("syn_c", H100, 1.85, count=2),
               L("syn_b", H100, 2.30, count=8)]
            + [L("syn_i", R5070TI, 0.11, vis="internal", lid=INTERNAL_ID, cap=4)])
    with Session.begin() as s:
        s.add_all(rows)


WHISPER = {"workload_type": "batch", "model": {"name": "openai/whisper-large-v3"},
           "media": {"modality": "audio", "task": "transcription", "total_audio_hours": 10000}}
AWQ = {"workload_type": "batch", "model": {"name": "meta-llama/Llama-3.1-8B-Instruct"}, "quantization": "awq",
       "inference": {"concurrency": 16, "context_len": 2048, "output_tokens": 512, "total_requests": 1_953_125}}


def rec(body, who=PUBLIC, **kw):
    return recommend(W.model_validate(body), who, now=NOW, limit=kw.pop("limit", 20), **kw)


def _check_block(out):
    so = out["scale_out"]
    assert so["offered"] and so["options"], so.get("reason")
    assert set(out["picks"]) == {"cheapest_market", "best_for_workload", "best_executable",
                                 "lowest_estimated_job_cost", "shortest_estimated_runtime"}, "picks unchanged"
    best = so["picks"]["best_scale_out"]
    assert best and best["scale_out_id"] in {o["scale_out_id"] for o in so["options"]}
    for o in so["options"]:
        n = o["replicas"]
        assert 2 <= n <= settings.batch_max_shards and o["gpu_count_per_replica"] == 1
        assert o["providers_count"] >= 2 and sum(p["replicas"] for p in o["providers"]) == n
        assert all(p["replicas"] <= max(1, n // 2) for p in o["providers"]), "diversification cap"
        lo, hi = o["completion_range_hours"]
        assert lo <= o["completion_hours"] <= hi and o["job_cost_range_usd"][0] <= o["job_cost_usd"] <= o["job_cost_range_usd"][1]
        assert len(o["batch_hint"]["body"]["shards"]) == n
        assert o["kind"] == "estimated" and o["confidence"]["level"] in ("low", "medium", "high")
    assert out["explanation"]["scale_out"] == so["comparison"]["text"] and " vs " in so["comparison"]["text"]
    return so


def test_db_whisper_scale_out():
    out = rec(WHISPER)
    so = _check_block(out)
    assert all(o["gpu"] != R_ONE for o in so["options"])
    assert any(s["gpu"] == R_ONE and "diversification cap" in s["reason"] for s in so["skipped"]), so["skipped"]
    best = next(o for o in so["options"] if o["scale_out_id"] == so["picks"]["best_scale_out"]["scale_out_id"])
    assert best["gpu"] == R5070TI, [(o["gpu"], o["replicas"], o["job_cost_usd"]) for o in so["options"]]
    # consumer rows now have a runtime: every option, the 5070 Ti included, has a whole-job estimate
    assert all((o["cost"]["estimated_total"] or {}).get("usd") for o in out["options"])
    # the assistant and the CLI say it (the MCP tool returns the payload itself)
    from assistant import engine
    reply = engine._rec_reply(out, WHISPER, "recommend")
    assert f"Or split it across {best['replicas']} separate RTX 5070 Ti" in reply and "Compared with one machine" in reply, reply
    import contextlib
    import io
    sys.path.insert(0, str(ROOT / "cli" / "src"))
    from opengrid_cli import cli as ogcli
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ogcli.show_recommendation(out)
    assert "scale-out (independent one-GPU replicas; theoretical):" in buf.getvalue() and "compare: " in buf.getvalue()
    # a deadline sizes the plan to it
    d = rec({**WHISPER, "constraints": {"deadline_hours": 400}})
    for o in d["scale_out"]["options"]:
        assert o["sizing"]["rule"] == "deadline" and o["meets_deadline"] in ("yes", "likely", "no")
    # limit does not touch scale-out (a separate list) and options keep their limit
    one = rec(WHISPER, limit=1)
    assert len(one["options"]) == 1 and one["scale_out"]["options"] == out["scale_out"]["options"]


def test_db_llm_batch():
    out = rec(AWQ)
    _check_block(out)
    assert out["picks"]["lowest_estimated_job_cost"] and out["picks"]["shortest_estimated_runtime"]
    o = next(o for o in out["options"] if o["gpu"] == R3090 and o["gpu_count"] == 1)
    inf, rt = o["performance"]["inference"], o["performance"]["runtime"]
    params = out["estimate"]["memory"]["profile"]["model"]["parameters_b"]
    peak = hardware.spec(R3090)["bf16_tflops_dense"]
    prefill = 2 * params * 1e9 * 1_953_125 * (2048 - 512) / (peak * 1e12 * P.MFU_LARGE[0]) / 3600
    decode = 1_953_125 * 512 / inf["aggregate_tokens_per_s"] / 3600
    close(rt["hours"], round(decode + prefill, 4), tol=2e-3)
    assert rt["known"] and "output tokens" in rt["basis"]
    assert o["cost"]["estimated_total"]["runtime_kind"] == "theoretical"


def test_db_never_offered():
    lora = rec(so_market.GOLDEN_REQUESTS["lora8b_tokens"])
    assert not lora["scale_out"]["offered"] and "training run" in lora["scale_out"]["reason"]
    serve = rec({"workload_type": "inference", "model": {"name": "meta-llama/Llama-3.1-8B-Instruct"},
                 "quantization": "awq", "inference": {"concurrency": 4, "context_len": 4096}})
    assert not serve["scale_out"]["offered"] and "total amount of work" in serve["scale_out"]["reason"]
    lat = rec({**AWQ, "inference": {**AWQ["inference"], "target_latency_ms": 500}})
    assert not lat["scale_out"]["offered"] and "latency" in lat["scale_out"]["reason"]
    fixed = rec({**AWQ, "hardware_hints": {"gpu_count": 1}})
    assert not fixed["scale_out"]["offered"]
    big = rec({"workload_type": "batch", "model": {"name": "meta-llama/Llama-3.1-70B-Instruct"}, "precision": "bf16",
               "inference": {"concurrency": 8, "context_len": 4096, "output_tokens": 256, "total_requests": 10000}})
    if big["options"]:
        assert not big["scale_out"]["offered"] and "one GPU" in big["scale_out"]["reason"], big["scale_out"]
        assert all(o["gpu_count"] >= 2 for o in big["options"])


def test_db_golden_backward_compat():
    S = normalize.SessionLocal
    gold = json.loads((Path(__file__).resolve().parent / "fixtures_scale_out" / "golden_c89801f.json").read_text())
    name = DB + "_golden"
    Sg = fixtures.session(scratchdb.create(name))
    try:
        normalize.SessionLocal = Sg
        with Sg.begin() as s:
            s.add_all(so_market.golden_rows(ComputeListingRow, NOW))
        scoring.history.cache_clear()
        for k, body in so_market.GOLDEN_REQUESTS.items():
            out = recommend(W.model_validate(body), PUBLIC, now=NOW, limit=20)
            assert "scale_out" in out and isinstance(out["scale_out"], dict)
            got = json.loads(json.dumps(so_market.normalize(out), sort_keys=True, default=str))
            assert got == gold[k], (k, _first_diff(got, gold[k]))
    finally:
        normalize.SessionLocal = S
        scoring.history.cache_clear()
        Sg.kw["bind"].dispose()
        scratchdb.drop(name)


def _first_diff(a, b, path="$"):
    if type(a) is not type(b):
        return path, a, b
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                return f"{path}.{k}", a.get(k, "<missing>"), b.get(k, "<missing>")
            d = _first_diff(a[k], b[k], f"{path}.{k}")
            if d:
                return d
        return None
    if isinstance(a, list):
        if len(a) != len(b):
            return path, len(a), len(b)
        for i, (x, y) in enumerate(zip(a, b)):
            d = _first_diff(x, y, f"{path}[{i}]")
            if d:
                return d
        return None
    return None if a == b else (path, a, b)


def test_db_visibility():
    anon = rec(WHISPER)
    s = json.dumps(anon["scale_out"])
    assert INTERNAL_ID not in s and "syn_i" not in s and "internal" not in s

    def shown(price):
        with normalize.SessionLocal.begin() as ss:
            ss.query(ComputeListingRow).filter(ComputeListingRow.listing_id == INTERNAL_ID).update(
                {"price_per_gpu_hour": Decimal(str(price)), "price_per_instance_hour": Decimal(str(price))})
        scoring.history.cache_clear()
        out = disclosure.redact(rec(WHISPER, KEY), KEY)
        return json.dumps(out["scale_out"], sort_keys=True, default=str)

    a, b = shown(0.11), shown(0.12)
    assert a == b, "a non-admin scale-out answer must not change with an internal price"
    assert "syn_i" not in a and json.loads(a) == json.loads(json.dumps(anon["scale_out"], default=str))
    op = rec(WHISPER, OPERATOR)
    so = op["scale_out"]
    used = [p for o in so["options"] for p in o["providers"] if p["provider"] == "syn_i"]
    assert used and all(p["visibility"] == "internal" for p in used), "the operator sees the internal listing"
    assert any(o["visibility"] == "internal" for o in so["options"] if any(p["provider"] == "syn_i" for p in o["providers"]))
    shown(0.11)


def test_db_measured_precedence():
    """A consented measurement of the same workload on a card replaces its theoretical single-GPU runtime."""
    from store.intelligence import PerformanceObservation as PO

    sig = "model:openai/whisper-large-v3|inference|bf16"
    body = {"workload_type": "batch", "model": {"name": "openai/whisper-large-v3"}, "precision": "bf16",
            "media": {"audio_seconds": 600, "total_items": 36000}}
    with normalize.SessionLocal.begin() as s:
        s.add(PO(gpu=L4, gpu_count=1, workload_class="inference", model_name="openai/whisper-large-v3",
                 method="inference", precision="bf16", source_kind="completed_job", consent=True, job_id="job-so1",
                 created_at=NOW, observed_at=NOW, provider="syn_a", workload_signature=sig, throughput=2.0,
                 throughput_unit="samples_per_second", data_kind="measured"))
    out = rec(body)
    l4 = next(o for o in out["options"] if o["gpu"] == L4 and o["gpu_count"] == 1)
    mh = l4["performance"]["measured"]["runtime"]["hours"]
    close(mh, 36000 / 2.0 / 3600, tol=1e-3)
    assert out["scale_out"]["offered"], out["scale_out"].get("reason")
    keep = SO.MAX_CARDS
    SO.MAX_CARDS = 50           # show every card's plan, so the L4 one is there to inspect
    try:
        so = rec(body)["scale_out"]
    finally:
        SO.MAX_CARDS = keep
    l4p = next((o for o in so["options"] if o["gpu"] == L4), None)
    assert l4p is not None, [o["gpu"] for o in so["options"]] + [x["gpu"] for x in so["skipped"]]
    assert l4p["runtime_kind"] == "measured" and l4p["single_gpu_hours"]["kind"] == "measured"
    close(l4p["single_gpu_hours"]["central"], round(mh, 3), tol=1e-3)


PURE = [test_specs_consumer_rows_sourced, test_performance_runtime_consumer, test_sizing_hand_computed,
        test_capacity_cap, test_build_hand_computed, test_build_ranking_in_reach_first, test_eligibility,
        test_llm_batch_work]
DBT = [test_db_whisper_scale_out, test_db_llm_batch, test_db_never_offered, test_db_golden_backward_compat,
       test_db_visibility, test_db_measured_precedence]


def main():
    failed = 0

    def run(fn):
        nonlocal failed
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL {fn.__name__}: {exc}")
    for fn in PURE:
        run(fn)
    url = scratchdb.create(DB)
    Session = fixtures.session(url)
    normalize.SessionLocal = Session
    seed(Session)
    scoring.history.cache_clear()
    try:
        for fn in DBT:
            run(fn)
    finally:
        Session.kw["bind"].dispose()
        scratchdb.drop(DB)
    total = len(PURE) + len(DBT)
    print(f"\n{total - failed}/{total} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
