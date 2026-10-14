"""Recommendations over a crafted market: the controlled suite
    A small PyTorch training -> modest GPU        B 7B LoRA -> estimate with assumptions, GPU comparison, tiers
    C 70B full fine-tune -> multi-GPU only        D vLLM 8B inference -> quantization + concurrency -> KV cache
    E non-GPU app -> no options                   F missing info -> questions, no numbers made up
    H stale / sold-out / spot never deployable    I unsupported execution -> executable false, still recommended
plus constraints (budget, max price, region, vendor, providers), cost scenarios, determinism, no route / quote
rows, and the HTTP API (operator, API key, anonymous with PUBLIC_PAGES).

Scratch database og_test_recommend with synthetic `syn_*` providers backed by fake adapters (never called).
Run:  .venv/bin/python tests/test_recommend.py
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("OPENGRID_NO_JOBS", "1")
os.environ.setdefault("POLLER_ENABLED", "false")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fixtures  # noqa: E402
import scratchdb  # noqa: E402

import normalize  # noqa: E402
from accounts.auth import PUBLIC, Principal  # noqa: E402
from config import settings  # noqa: E402
from intelligence.recommend import RecommendError, recommend  # noqa: E402
from intelligence.requirements import WorkloadRequirements as W  # noqa: E402
from routing import adapters, control, scoring  # noqa: E402
from routing.adapters.base import Adapter  # noqa: E402
from routing.adapters.results import PER_DEPLOYMENT, Capabilities  # noqa: E402
from sqlalchemy import text  # noqa: E402
from tables import ComputeListingRow  # noqa: E402

DB = os.environ.get("OG_TEST_RECOMMEND_DB", "og_test_recommend")
NOW = datetime.now(timezone.utc).replace(microsecond=0)
FULL = frozenset({"data:read", "route:preview", "route:execute", "deployments:read"})
WHO = Principal(kind="api_key", account_id=7, key_id=70, scopes=FULL)
READER = Principal(kind="api_key", account_id=8, key_id=80, scopes=frozenset({"data:read"}))

T4, L4, R4090, A40, L40S = ("NVIDIA T4 16GB", "NVIDIA L4 24GB", "NVIDIA RTX 4090 24GB", "NVIDIA A40 48GB",
                            "NVIDIA L40S 48GB")
A100, H100, H100P, H200 = ("NVIDIA A100 80GB SXM4", "NVIDIA H100 80GB SXM5", "NVIDIA H100 80GB PCIe",
                           "NVIDIA H200 141GB SXM5")
MI300X, B200 = "AMD Instinct MI300X 192GB", "NVIDIA B200 180GB SXM"


def caps() -> Capabilities:
    c = Capabilities()
    c.startup_script = ("YES", "test fake")
    c.forces_account_ssh_key = ("NO", "test fake")
    c.billing_unit = ("per minute", "test")
    c.minimum_commitment = ("NO", "test")
    return c


CALLS: list = []


class Fake(Adapter):
    """Never called: recommending must not touch a provider."""
    LEVEL = 3
    CREDENTIALS = ()
    CHECK_NEEDS_CREDENTIALS = False
    SSH_KEY_REGISTRATION = PER_DEPLOYMENT
    CAPABILITIES = caps()

    def _no(self, *a, **k):
        CALLS.append((self.provider, a))
        raise AssertionError("recommend made a provider call")

    check_availability = provision = status = terminate = list_instances = _no


FAKES = ("syn_a", "syn_b", "syn_c")


def L(prov, gpu, price, *, lid=None, region="us-east", country="US", market="on_demand", interruptible=False,
      available=True, age_min=1, count=1):
    return ComputeListingRow(
        provider=prov, listing_id=lid or f"{prov}:{gpu}:{count}:{market}", sku=f"{prov}-sku", raw_gpu_name=gpu,
        canonical_gpu_name=gpu, gpu_count=count, region=region, country=country,
        price_per_gpu_hour=Decimal(str(price)), price_per_instance_hour=Decimal(str(round(price * count, 6))),
        currency="USD", market_type=market, provider_tier=None, interruptible=interruptible, available=available,
        capacity=None, capacity_unit=None, vcpu=None, ram_gb=None, storage_gb=None,
        observed_at=NOW - timedelta(minutes=age_min), first_seen_at=NOW - timedelta(days=3))


STALE_ID, SOLD_ID, SPOT_ID = "syn_b:h100:stale", "syn_c:h100:soldout", "syn_a:h100:spot"


def seed(Session):
    rows = [
        L("syn_a", T4, 0.30), L("syn_a", L4, 0.55), L("syn_b", L4, 0.50),
        L("syn_a", R4090, 0.45, region=None, country=None),
        L("syn_a", A40, 0.60), L("syn_c", A40, 0.65, available=None),
        L("syn_a", L40S, 0.95), L("syn_b", L40S, 0.90), L("syn_c", L40S, 0.70, region="eu-west", country="DE"),
        L("syn_a", A100, 1.50), L("syn_mkt", A100, 1.20),
        L("syn_a", H100, 2.50), L("syn_b", H100, 2.40), L("syn_mkt", H100P, 2.00),
        L("syn_b", H100, 2.30, count=8),
        L("syn_b", H100, 1.00, lid=STALE_ID, age_min=180),                       # stale
        L("syn_c", H100, 0.90, lid=SOLD_ID, available=False),                     # sold out
        L("syn_a", H100, 0.80, lid=SPOT_ID, market="spot", interruptible=True),  # spot
        L("syn_a", H200, 3.60), L("syn_a", H200, 3.20, count=8),
        L("syn_b", MI300X, 2.10, count=8),
        L("syn_a", B200, 5.00, count=8),
    ]
    from store.accounts import Account
    with Session.begin() as s:
        s.add_all(rows)
        for aid in (7, 8, 9):
            s.add(Account(id=aid, name=f"acct{aid}", status="active", plan="free", settings={}, is_operator=False))


def reset():
    settings.routing_live_provisioning = False
    with normalize.SessionLocal.begin() as s:
        s.execute(text("DELETE FROM provider_execution_flags"))
        s.execute(text("DELETE FROM execution_controls"))
    scoring.history.cache_clear()


def supervised(*providers):
    settings.routing_live_provisioning = True
    control.set_mode("SUPERVISED", reason="test", by="test")
    for p in providers:
        control.mark_validated(p, "dep-test", {"test": "synthetic"}, "test")
        control.set_provider_flags(p, reason="test", by="test", supervised_enabled=True)


def rec(body, who=WHO, **kw):
    return recommend(W.model_validate(body), who, now=NOW, **kw)


def codes(out, gpu=None, provider=None):
    return {e["code"] for e in out["excluded"] if (gpu is None or e["gpu"] == gpu)
            and (provider is None or e.get("provider") == provider)}


def opt(out, key):
    ref = out["picks"][key]
    return None if ref is None else next(o for o in out["options"] if o["option_id"] == ref["option_id"])


RESNET = {"workload_type": "training", "frameworks": ["pytorch", "torchvision"], "model": {"name": "resnet50"},
          "precision": "fp32", "training": {"micro_batch_size": 64, "optimizer": "sgd"}}
LORA7 = {"workload_type": "fine_tuning", "frameworks": ["pytorch", "transformers", "peft"],
         "model": {"name": "mistralai/Mistral-7B-v0.1"}, "training": {"method": "lora"}}
FULL70 = {"workload_type": "fine_tuning", "model": {"name": "meta-llama/Llama-3.1-70B"}, "precision": "bf16",
          "training": {"method": "full", "micro_batch_size": 1, "seq_len": 2048, "gradient_checkpointing": True}}
VLLM8 = {"workload_type": "inference", "frameworks": ["vllm"], "model": {"name": "meta-llama/Llama-3.1-8B"},
         "quantization": "awq", "inference": {"engine": "vllm", "concurrency": 16, "context_len": 8192}}


# --------------------------------------------------------------------------
# A. small PyTorch training
# --------------------------------------------------------------------------

def test_a_small_training_gets_a_modest_gpu():
    reset()
    out = rec(RESNET)
    need = out["estimate"]["memory"]["recommended_gb"]
    assert 9 < need < 16, need
    ch, bw = opt(out, "cheapest_market"), opt(out, "best_for_workload")
    assert ch["gpu"] == T4 and ch["price_per_gpu_hour"] == 0.30 and ch["gpu_count"] == 1
    assert bw["vram_gb"] <= 48 and bw["price_per_instance_hour"] <= 1.0, bw
    assert all(o["memory_fit"]["fits"] and o["memory_fit"]["needed_per_gpu_gb"] == need for o in out["options"])
    assert all(o["vram_gb"] >= need for o in out["options"])
    assert all(o["gpu_count"] == 1 for o in out["options"])
    assert out["estimate"]["interconnect"]["needed"] is False
    # an unknown small CNN: a lower bound, low confidence, still a modest GPU
    out = rec({"workload_type": "training", "frameworks": ["pytorch"], "model": {"parameters_b": 0.0005}})
    m = out["estimate"]["memory"]
    assert m["confidence"] == "low" and m["lower_bound"] is True and m["recommended_gb"] < 3
    assert opt(out, "cheapest_market")["gpu"] == T4, "an ASSUMED bf16 never excludes the T4"
    assert out["notes"][0].startswith("This is a small model (0.5M parameters): it can likely train on a CPU"), \
        "a tiny model is not pushed towards renting a big GPU"
    assert not any("small model" in n for n in rec(RESNET)["notes"]), "ResNet-50 (25.6M) gets no CPU note"


# --------------------------------------------------------------------------
# B. 7B LoRA
# --------------------------------------------------------------------------

def test_b_7b_lora_compares_gpus_with_tradeoffs():
    reset()
    out = rec(LORA7)
    est = out["estimate"]
    m = est["memory"]
    assert m["method"] == "training_lora" and m["confidence"] == "medium"
    assert any("micro batch size not stated" in a for a in m["assumptions"])
    assert any("Mistral-7B-v0.1" in a for a in m["assumptions"]), "catalog fill is stated"
    need = m["recommended_gb"]
    assert 24 < need < 40, need
    assert {"training.micro_batch_size", "training.seq_len"} <= {q["field"] for q in out["questions"]}
    # 24 GB cards are excluded with the numbers
    for g in (L4, R4090):     # 24 GB: never one GPU; 2-GPU ZeRO-3 would fit but nobody lists that shape
        e = next(e for e in out["excluded"] if e["gpu"] == g)
        assert e["code"] in ("insufficient_vram", "no_fresh_listing") and f"{need:g} GB needed" in e["reason"], e
        assert not any(o["gpu"] == g for o in out["options"])
    gpus = {o["gpu"] for o in out["options"]}
    assert len(gpus) >= 3 and {A40, L40S} <= gpus, gpus
    tiers = {o["tier"] for o in out["options"]}
    assert "balanced" in tiers and "higher_performance" in tiers, tiers
    bw = opt(out, "best_for_workload")
    assert bw["tier"] == "balanced" and bw["memory_fit"]["headroom_pct"] >= 10
    hp = [o for o in out["options"] if o["tier"] == "higher_performance"]
    assert all(any("spec sheet" in w or "more VRAM" in w for w in o["why"]) for o in hp), [o["why"] for o in hp]
    assert all(any("vs the balanced pick" in w for w in o["why"]) for o in hp
               if o["price_per_instance_hour"] > bw["price_per_instance_hour"] * 1.005)
    for o in out["options"]:
        assert o["memory_fit"]["headroom_pct"] == round((o["vram_gb"] - need) / o["vram_gb"] * 100, 1)
        assert o["theoretical"]["kind"] == "theoretical"
        assert "runtime not benchmarked: no measured benchmark for this workload on this GPU" in o["limitations"]
    assert "fastest" not in json.dumps(out).lower()
    assert est["runtime"]["known"] is False


def test_cost_scenarios_and_billing():
    reset()
    out = rec(LORA7)
    for o in out["options"]:
        k = o["cost"]
        assert [s["hours"] for s in k["scenarios"]] == [1, 10, 100]
        for s in k["scenarios"]:
            assert s["total_usd"] == round(o["price_per_instance_hour"] * s["billed_hours"], 2)
        assert {"storage", "egress"} <= set(k["unknown_costs"]) and k["kind"] == "estimated"
        if o["provider"] == "syn_mkt":
            assert k["billing_unit"] == "unknown" and any("billing granularity" in u for u in k["unknown_costs"])
            assert "market data only: OpenGrid cannot provision this provider" in o["limitations"]
        else:
            assert k["billing_unit"] == "per_minute", k
    assert all(o["cost"]["budget_hours"] is None for o in out["options"])
    # a budget alone: hours it buys, nothing excluded for it
    out = rec({**LORA7, "constraints": {"budget_usd": 7}})
    a40 = next(o for o in out["options"] if o["gpu"] == A40 and o["provider"] == "syn_a")
    assert a40["cost"]["budget_hours"] == 11.6        # floor(7 / 0.60 * 10) / 10
    assert "over_budget" not in codes(out)
    # budget + runtime: anything over $7 for 10 h is excluded with the numbers
    out = rec({**LORA7, "constraints": {"budget_usd": 7, "runtime_hours": 10}})
    assert out["options"] and all(o["cost"]["runtime_total_usd"] <= 7 for o in out["options"])
    assert all(any(s["hours"] == 10 and "your runtime" in s["assumption"] for s in o["cost"]["scenarios"])
               for o in out["options"])
    ob = [e for e in out["excluded"] if e["code"] == "over_budget"]
    assert ob and all("$7.00 budget" in e["reason"] for e in ob)
    assert any(e["gpu"] == H100 for e in ob)


# --------------------------------------------------------------------------
# C. 70B full fine-tune
# --------------------------------------------------------------------------

def test_c_70b_full_is_multi_gpu_and_never_a_24gb_card():
    reset()
    out = rec(FULL70)
    est = out["estimate"]
    assert est["gpu_count"]["suggested"] == 8 and est["interconnect"]["needed"] is True
    per8 = est["memory"]["per_gpu_by_count"][8]["recommended_gb"]
    assert out["options"], out["excluded"]
    for o in out["options"]:
        assert o["gpu_count"] == 8 and o["vram_gb"] >= per8 and o["vram_gb"] > 24, o
        assert o["price_per_instance_hour"] == round(o["price_per_gpu_hour"] * 8, 6)
        assert "ZeRO-3" in o["why"][0]
    assert {o["gpu"] for o in out["options"]} == {MI300X}
    for g in (L4, R4090, A40, L40S, H100, H200, B200):
        assert "insufficient_vram" in codes(out, g), (g, codes(out, g))
    assert codes(out, T4) == {"compute_capability_too_low"}, "bf16 needs compute capability 8.0"
    h100 = next(e for e in out["excluded"] if e["gpu"] == H100 and e["code"] == "insufficient_vram")
    assert "even at 8 GPUs" in h100["reason"]
    # with max_gpus 4 nothing fits at all
    out = rec({**FULL70, "constraints": {"max_gpus": 4}})
    assert out["options"] == [] and out["picks"]["cheapest_market"] is None
    assert out["estimate"]["gpu_count"]["suggested"] is None


# --------------------------------------------------------------------------
# D. inference
# --------------------------------------------------------------------------

def test_d_vllm_inference_kv_cache():
    reset()
    out = rec(VLLM8)
    m = out["estimate"]["memory"]
    assert m["components_gb"]["kv_cache"] == 17.18 and m["components_gb"]["weights"] == 5.73
    need = m["recommended_gb"]
    for g in (L4, R4090):     # 21.6 GB usable < need: one GPU never; tensor-parallel x2 has no listing
        e = next(e for e in out["excluded"] if e["gpu"] == g)
        assert e["code"] in ("insufficient_vram", "no_fresh_listing") and "90% usable by vLLM" in e["reason"], e
        assert f"{need:g} GB needed" in e["reason"]
    for o in out["options"]:
        assert o["memory_fit"]["usable_vram_gb"] == round(o["vram_gb"] * 0.9, 2) >= need
    assert opt(out, "cheapest_market")["gpu"] == A40
    # 4x the concurrency: 4x the KV cache, an 80 GB card no longer fits on one GPU
    big = rec({**VLLM8, "inference": {**VLLM8["inference"], "concurrency": 64}})
    bm = big["estimate"]["memory"]
    assert abs(bm["per_gpu_by_count"][1]["components_gb"]["kv_cache"] - 4 * 17.18) < 0.03
    one_gpu = [o for o in big["options"] if o["gpu_count"] == 1]
    assert one_gpu and all(o["vram_gb"] * 0.9 >= bm["per_gpu_by_count"][1]["recommended_gb"] for o in one_gpu)
    assert all(o["gpu"] not in (A100, H100, A40, L40S) for o in one_gpu)


# --------------------------------------------------------------------------
# E / F
# --------------------------------------------------------------------------

def test_e_non_gpu_app():
    reset()
    out = rec({"frameworks": ["flask", "sqlalchemy", "redis"]})
    assert out["estimate"]["needs_gpu"] is False
    assert out["options"] == [] and all(v is None for v in out["picks"].values()) and out["excluded"] == []
    assert out["notes"] and out["notes"][0].startswith("This workload does not need a GPU")
    out = rec({"workload_type": "no_gpu"})
    assert out["options"] == [] and out["estimate"]["needs_gpu"] is False


def test_f_missing_info_asks_and_invents_nothing():
    reset()
    out = rec({"workload_type": "fine_tuning"})
    m = out["estimate"]["memory"]
    assert m["method"] == "unavailable" and m["confidence"] == "none" and m["min_total_gb"] is None
    assert out["options"] == [] and all(v is None for v in out["picks"].values())
    f = {q["field"] for q in out["questions"]}
    assert {"model.name", "training.method"} <= f, f
    assert "could not be estimated" in out["notes"][0]


# --------------------------------------------------------------------------
# H. stale / sold out / spot
# --------------------------------------------------------------------------

def test_h_stale_sold_out_and_spot_never_deployable():
    reset()
    out = rec({**LORA7, "constraints": {"max_gpus": 1}}, limit=20)
    ids = {o["listing_id"] for o in out["options"]}
    assert not ids & {STALE_ID, SOLD_ID, SPOT_ID}
    assert "stale" in codes(out, H100, "syn_b") and "sold_out" in codes(out, H100, "syn_c")
    assert "not_on_demand" in codes(out, H100, "syn_a")
    assert all(o["fresh"] and o["available"] is not False for o in out["options"])
    assert all(o["age_seconds"] < 3600 for o in out["options"])
    unk = next(o for o in out["options"] if o["provider"] == "syn_c" and o["gpu"] == A40)
    assert unk["available"] is None and "availability not reported by provider" in unk["limitations"]
    assert opt(out, "cheapest_market")["listing_id"] not in (STALE_ID, SOLD_ID, SPOT_ID)
    # allow_spot does not smuggle spot in
    out = rec({**LORA7, "constraints": {"allow_spot": True}}, limit=20)
    assert SPOT_ID not in {o["listing_id"] for o in out["options"]}
    assert any("spot" in n for n in out["notes"])


# --------------------------------------------------------------------------
# I. execution
# --------------------------------------------------------------------------

def test_i_unsupported_execution_still_recommends():
    reset()
    supervised(*FAKES)
    try:
        out = rec(LORA7)
        assert out["options"] and out["picks"]["cheapest_market"] and out["picks"]["best_for_workload"]
        assert out["picks"]["best_executable"] is None
        assert out["picks_unavailable"]["best_executable"] == "the workload is not a supported template", \
            "a null pick always says why"
        tiers = [o["tier"] for o in out["options"]]
        rank = {"lower_cost": 0, "balanced": 1, "higher_performance": 2}
        assert [rank[t] for t in tiers] == sorted(rank[t] for t in tiers), f"options ordered by tier: {tiers}"
        for o in out["options"]:
            ex = o["execution"]
            assert ex["executable"] is False and ex["workload_supported"] is False
            assert ex["reasons"][0]["code"] == "workload_not_supported" and "pytorch-mnist" in ex["reasons"][0]["reason"]
            assert ex["route_preview_url"] == f"/route?gpu={o['gpu_slug']}&count={o['gpu_count']}"
        assert any("not a supported template" in n for n in out["notes"])
        # the allowlisted template: executable on a ready provider
        mn = rec({"workload_type": "training", "model": {"name": "pytorch-mnist", "parameters_b": 0.0004},
                  "precision": "fp32", "frameworks": ["pytorch"]})
        exe = opt(mn, "best_executable")
        assert exe is not None and exe["execution"]["executable"] and exe["execution"]["reasons"] == []
        assert exe["provider"] in FAKES and exe["gpu_count"] == 1
        mk = [o for o in mn["options"] if o["provider"] == "syn_mkt"]
        assert all(not o["execution"]["executable"] and "not_provisionable" in {r["code"] for r in o["execution"]["reasons"]}
                   for o in mk)
        # anonymous: never assumed ready
        an = rec({"workload_type": "training", "model": {"name": "pytorch-mnist", "parameters_b": 0.0004},
                  "precision": "fp32"}, who=PUBLIC)
        assert an["picks"]["best_executable"] is None
        assert all("sign_in_required" in {r["code"] for r in o["execution"]["reasons"]} for o in an["options"])
        assert all("API key" in next(r["reason"] for r in o["execution"]["reasons"] if r["code"] == "sign_in_required")
                   for o in an["options"])
        # a read-only key: says which scope is missing
        rd = rec({"workload_type": "training", "model": {"name": "pytorch-mnist", "parameters_b": 0.0004},
                  "precision": "fp32"}, who=READER)
        assert rd["picks"]["best_executable"] is None
        assert all("scope_missing" in {r["code"] for r in o["execution"]["reasons"]} for o in rd["options"])
    finally:
        reset()
    # preview-only environment: readiness blockers are reported, not assumed away
    mn = rec({"workload_type": "training", "model": {"name": "pytorch-mnist", "parameters_b": 0.0004},
              "precision": "fp32"})
    assert mn["picks"]["best_executable"] is None
    assert all("env_ceiling" in {r["code"] for r in o["execution"]["reasons"]}
               for o in mn["options"] if o["provider"] in FAKES)
    assert CALLS == []


# --------------------------------------------------------------------------
# constraints
# --------------------------------------------------------------------------

def test_constraints():
    reset()
    out = rec({**LORA7, "constraints": {"max_price_per_gpu_hour": 1.0}}, limit=20)
    assert out["options"] and all(o["price_per_gpu_hour"] <= 1.0 for o in out["options"])
    assert "over_max_price" in codes(out, H100)
    out = rec({**LORA7, "constraints": {"region": "eu"}}, limit=20)
    assert any(o["region"] == "eu-west" for o in out["options"])
    assert all(o["region"] in ("eu-west", None) for o in out["options"]), [o["region"] for o in out["options"]]
    assert "wrong_region" in codes(out, L40S)
    out = rec({**LORA7, "constraints": {"exclude_providers": ["syn_a"]}}, limit=20)
    assert out["options"] and all(o["provider"] != "syn_a" for o in out["options"])
    assert "excluded_by_preference" in codes(out, provider="syn_a")
    out = rec({**LORA7, "constraints": {"include_providers": ["syn_b"]}}, limit=20)
    assert out["options"] and {o["provider"] for o in out["options"]} == {"syn_b"}
    out = rec({**LORA7, "constraints": {"vendor": "AMD"}})
    assert "wrong_vendor" in codes(out, L40S) and "wrong_vendor" in codes(out, H100)
    assert out["options"] == [] and "no_fresh_listing" in codes(out, MI300X)
    out = rec({**FULL70, "constraints": {"vendor": "NVIDIA"}})
    assert out["options"] == [] and "wrong_vendor" in codes(out, MI300X)
    out = rec({**LORA7, "hardware_hints": {"cuda_min": "12.1"}})
    assert out["options"] and all(o["gpu"].startswith("NVIDIA") for o in out["options"])
    out = rec({**LORA7, "precision": "fp8"}, limit=20)
    assert "compute_capability_too_low" in codes(out, A100) and "compute_capability_too_low" in codes(out, A40)
    assert {o["gpu"] for o in out["options"]} <= {L40S, H100, H100P, H200, B200, MI300X, L4, R4090}, "only FP8-capable GPUs"
    try:
        rec({**LORA7, "constraints": {"region": "mars"}})
    except RecommendError as e:
        assert e.code == "unknown_region"
    else:
        raise AssertionError("unknown region accepted")
    out = rec(LORA7, limit=2)
    assert len(out["options"]) <= 3
    picked = {r["option_id"] for r in out["picks"].values() if r}
    assert picked <= {o["option_id"] for o in out["options"]}, "picks always reference a listed option"


def test_deterministic_and_read_only():
    reset()
    a, b = rec(LORA7), rec(LORA7)
    assert json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)
    json.dumps(a)
    ids = [o["option_id"] for o in a["options"]]
    assert len(ids) == len(set(ids))
    with normalize.SessionLocal() as s:
        for t in ("route_requests", "routing_decisions", "quotes", "deployments", "performance_observations"):
            assert s.execute(text(f"SELECT count(*) FROM {t}")).scalar() == 0, t
    assert CALLS == []


# --------------------------------------------------------------------------
# HTTP API
# --------------------------------------------------------------------------

def test_api():
    reset()
    import main
    from fastapi.testclient import TestClient

    from accounts import keys, ratelimit
    saved = (settings.app_password, settings.public_pages, settings.environment, settings.api_key_pepper)
    try:
        settings.app_password, settings.public_pages, settings.environment = None, False, "development"
        settings.api_key_pepper = "test-pepper-recommend"
        keys.reset_cache()
        ratelimit.reset()
        c = TestClient(main.app)
        H = {"X-OpenGrid-Request": "1"}
        r = c.post("/v1/recommend/estimate", json=LORA7, headers=H)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["meta"]["kind"] == "estimated" and d["meta"]["methodology"] == "/methodology/recommendations"
        assert d["data"]["memory"]["method"] == "training_lora" and d["data"]["runtime"]["known"] is False
        r = c.post("/v1/recommend", json={**LORA7, "limit": 5}, headers=H)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["meta"]["kind"] == "mixed" and len(d["data"]["options"]) <= 6 and d["data"]["picks"]["cheapest_market"]
        assert c.post("/v1/recommend", json=LORA7).status_code == 403, "operator POST without the CSRF header"
        r = c.post("/v1/recommend", json={**LORA7, "limit": 0}, headers=H)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_limit"
        r = c.post("/v1/recommend/estimate", json={**LORA7, "command": "rm -rf /"}, headers=H)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_requirements"
        assert r.json()["detail"]["errors"][0]["field"] == "command"
        r = c.post("/v1/recommend", json={**LORA7, "constraints": {"region": "mars"}}, headers=H)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "unknown_region"
        r = c.post("/v1/recommend/estimate", content=json.dumps(LORA7), headers={**H, "Content-Type": "text/plain"})
        assert r.status_code == 415
        r = c.post("/v1/recommend/estimate", json={"evidence": [{"field": "x" * 150, "value": "y" * 150}] * 200}, headers=H)
        assert r.status_code == 413, r.status_code
        # an API key with data:read
        k = keys.create_key(9, "rec-test", ["data:read"], rate_limit_per_minute=1000)
        r = c.post("/v1/recommend", json=LORA7, headers={"Authorization": f"Bearer {k['secret']}"})
        assert r.status_code == 200, r.text
        assert all("scope_missing" in {x["code"] for x in o["execution"]["reasons"]} for o in r.json()["data"]["options"])
        # anonymous with PUBLIC_PAGES: both endpoints open, execution says sign in
        settings.app_password, settings.public_pages = "pw", True
        anon = TestClient(main.app)
        r = anon.post("/v1/recommend/estimate", json=VLLM8)
        assert r.status_code == 200, r.text
        r = anon.post("/v1/recommend", json=VLLM8)
        assert r.status_code == 200, r.text
        opts = r.json()["data"]["options"]
        assert opts and all(o["execution"]["executable"] is False and
                            "sign_in_required" in {x["code"] for x in o["execution"]["reasons"]} for o in opts)
        assert anon.put("/v1/recommend", json=VLLM8).status_code in (401, 405)
        assert anon.post("/v1/recommend/other", json=VLLM8).status_code == 401
        assert anon.post("/v1/route/preview", json={"gpu": "h100-80gb-sxm5", "count": 1}).status_code == 401
        settings.public_pages = False
        assert anon.post("/v1/recommend", json=VLLM8).status_code == 401, "closed when PUBLIC_PAGES is off"
    finally:
        settings.app_password, settings.public_pages, settings.environment, settings.api_key_pepper = saved
        keys.reset_cache()
        ratelimit.reset()
    with normalize.SessionLocal() as s:
        for t in ("route_requests", "routing_decisions", "quotes"):
            assert s.execute(text(f"SELECT count(*) FROM {t}")).scalar() == 0, t
    assert CALLS == []


def test_migration_roundtrip():
    """0020 applies on top of 0019 and downgrades cleanly (additive only)."""
    from alembic import command
    from sqlalchemy import create_engine

    import db as dbmod
    name = DB + "_mig"
    url = scratchdb.create(name)
    old = settings.database_url
    settings.database_url = url
    try:
        cfg = dbmod._alembic_config()
        command.upgrade(cfg, "0020_intelligence")
        eng = create_engine(url)
        with eng.connect() as c:
            assert c.execute(text("SELECT to_regclass('performance_observations')")).scalar() is not None
            assert c.execute(text("SELECT count(*) FROM pg_constraint WHERE conname = 'ck_perfobs_source_kind'")).scalar() == 1
        command.downgrade(cfg, "0019_reservations")
        with eng.connect() as c:
            assert c.execute(text("SELECT to_regclass('performance_observations')")).scalar() is None
            assert c.execute(text("SELECT to_regclass('reservation_requests')")).scalar() is not None, "0019 untouched"
        eng.dispose()
    finally:
        settings.database_url = old
        scratchdb.drop(name)


# --------------------------------------------------------------------------

def main():
    url = scratchdb.create(DB)
    Session = fixtures.session(url)
    normalize.SessionLocal = Session
    seed(Session)
    scoring.history.cache_clear()
    try:
        from accounts import accounts as acc
        acc.reset_cache()
    except Exception:  # noqa: BLE001
        pass
    for p in FAKES:
        adapters.register(p, Fake)
    try:
        tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
        failed = 0
        for name, fn in tests:
            try:
                fn()
                print(f"ok   {name}")
            except Exception as exc:  # noqa: BLE001
                failed += 1
                import traceback
                traceback.print_exc()
                print(f"FAIL {name}: {exc}")
        print(f"\n{len(tests) - failed}/{len(tests)} passed")
        if failed:
            sys.exit(1)
    finally:
        for p in FAKES:
            adapters.unregister(p)
        settings.routing_live_provisioning = False
        Session.kw["bind"].dispose()
        scratchdb.drop(DB)


if __name__ == "__main__":
    main()

