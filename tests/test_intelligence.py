"""Workload intelligence, pure parts: the VRAM formulas against hand-computed numbers, no double counting,
LoRA << full, QLoRA < LoRA, KV-cache scaling, sharding, the model catalog, needs_gpu, questions, request
validation. No database, no network.

Run:  .venv/bin/python tests/test_intelligence.py
"""

import os
import sys
from pathlib import Path

os.environ.setdefault("OPENGRID_NO_JOBS", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydantic import ValidationError  # noqa: E402

from intelligence import memory as M  # noqa: E402
from intelligence import models_catalog as C  # noqa: E402
from intelligence.estimate import estimate  # noqa: E402
from intelligence.requirements import WorkloadRequirements as W  # noqa: E402

GB = 1e9
# A small, fully specified transformer so every number below can be computed by hand.
TINY = {"name": "custom-1b", "parameters_b": 1.0, "num_layers": 16, "hidden_size": 2048, "num_attention_heads": 16,
        "num_kv_heads": 16, "head_dim": 128, "vocab_size": 32000, "max_context": 4096}
TRAIN = {"micro_batch_size": 2, "seq_len": 1024, "gradient_checkpointing": False, "optimizer": "adamw"}


def close(a, b, tol=0.011):
    assert a is not None and abs(a - b) <= tol, f"{a} != {b} (tol {tol})"


def est(**body):
    return estimate(W.model_validate(body))


def tiny(method, **tr):
    return est(workload_type="fine_tuning", model=TINY, precision="bf16", training={**TRAIN, "method": method, **tr})


# --------------------------------------------------------------------------
# formulas
# --------------------------------------------------------------------------

def test_bytes_per_param():
    assert M.weight_bytes_per_param("fp32", None) == 4.0
    assert M.weight_bytes_per_param("bf16", "none") == 2.0
    assert M.weight_bytes_per_param("fp16", None) == 2.0
    assert M.weight_bytes_per_param("bf16", "int8") == 1.0
    assert M.weight_bytes_per_param(None, "fp8") == 1.0
    assert M.weight_bytes_per_param(None, "nf4") == 0.5 + 0.127 / 8          # QLoRA double quantization
    assert M.weight_bytes_per_param(None, "awq") == 0.5 + (20 / 128) / 8      # group-128 scale + zero
    assert M.weight_bytes_per_param(None, "gptq") == M.weight_bytes_per_param(None, "int4")


def test_optimizer_bytes():
    assert M.optimizer_bytes_per_param("adamw", master_fp32=True) == 12.0     # m + v + fp32 master
    assert M.optimizer_bytes_per_param("adamw", master_fp32=False) == 8.0
    assert M.optimizer_bytes_per_param("adamw_8bit", master_fp32=True) == 6.0
    assert M.optimizer_bytes_per_param("sgd", master_fp32=True) == 8.0
    assert M.optimizer_bytes_per_param("adafactor", master_fp32=False, hidden_size=4096) == 4 * 2 / 4096


def test_lora_params_formula():
    # r * [(h+h) q + 2*(h+kv*d) k,v + (h+h) o] * L ; MHA: kv*d = h
    assert M.lora_trainable_params(8, layers=16, hidden_size=2048, num_heads=16, num_kv_heads=16, head_dim=128) \
        == 8 * (4096 + 2 * 4096 + 4096) * 16 == 2_097_152
    # Llama 3.1 8B (GQA, 8 KV heads of 128): r=16
    assert M.lora_trainable_params(16, layers=32, hidden_size=4096, num_heads=32, num_kv_heads=8, head_dim=128) \
        == 16 * ((4096 + 4096) + 2 * (4096 + 1024) + (4096 + 4096)) * 32 == 13_631_488


def test_activation_formula():
    s, b, h, a = 1024, 2, 2048, 16
    assert M.activation_bytes_per_layer(seq=s, batch=b, hidden=h, heads=a, flash=True) == s * b * h * 34 == 142_606_336
    # Korthikanti et al. 2022 eq. 1 with the attention-score term: 34 + 5*a*s/h = 34 + 40
    assert M.activation_bytes_per_layer(seq=s, batch=b, hidden=h, heads=a, flash=False) == s * b * h * 74
    full = M.transformer_activation_bytes(seq=s, batch=b, hidden=h, heads=a, layers=16, flash=True,
                                          checkpointing=False, vocab=32000)
    assert full == 16 * 142_606_336 + 4 * s * b * 32000 == 2_543_845_376
    ck = M.transformer_activation_bytes(seq=s, batch=b, hidden=h, heads=a, layers=16, flash=True,
                                        checkpointing=True, vocab=32000)
    assert ck == 16 * 2 * s * b * h + 142_606_336 + 4 * s * b * 32000 == 538_968_064
    assert ck < full / 4, "checkpointing cuts stored activations"


def test_kv_cache_formula_and_scaling():
    kv = M.kv_cache_bytes(layers=32, kv_heads=8, head_dim=128, context=8192, concurrency=16)
    assert kv == 2 * 32 * 8 * 128 * 8192 * 16 * 2 == 17_179_869_184
    assert M.kv_cache_bytes(layers=32, kv_heads=8, head_dim=128, context=8192, concurrency=32) == 2 * kv
    assert M.kv_cache_bytes(layers=32, kv_heads=8, head_dim=128, context=16384, concurrency=16) == 2 * kv


# --------------------------------------------------------------------------
# estimator
# --------------------------------------------------------------------------

def test_full_training_hand_computed():
    e = tiny("full")
    m = e["memory"]
    assert m["method"] == "training_full" and m["kind"] == "estimated"
    c = m["components_gb"]
    close(c["weights"], 2.0)          # 1e9 x 2 B (bf16)
    close(c["gradients"], 2.0)        # 1e9 x 2 B
    close(c["optimizer"], 12.0)       # 1e9 x (8 B m+v + 4 B fp32 master)
    close(c["activations"], 2.54)     # 2,543,845,376 B (above)
    assert c["kv_cache"] == 0.0
    other = 2e9 + 2e9 + 12e9 + 2_543_845_376
    close(c["framework_overhead"], (1.5e9 + 0.05 * other) / GB)   # 2.43
    total = (other + 1.5e9 + 0.05 * other) / GB                     # 20.97
    close(m["min_total_gb"], total)
    close(m["recommended_gb"], total * 1.2)                         # 25.17
    # no double counting: the total is exactly the sum of the components
    close(sum(v for v in c.values()), m["min_total_gb"], 0.03)
    assert m["confidence"] == "high", (m["confidence"], m["assumptions"])
    assert m["range_gb"] is None, "nothing uncertain was assumed"
    assert e["gpu_count"]["suggested"] == 1 and e["interconnect"]["needed"] is False
    assert e["runtime"]["known"] is False and "benchmark" in e["runtime"]["reason"]
    assert e["questions"] == [], e["questions"]
    assert m["trainable_parameters"] == 1_000_000_000


def test_fp32_training_has_no_master_copy():
    m = est(workload_type="training", model=TINY, precision="fp32", training={**TRAIN, "method": "full"})["memory"]
    close(m["components_gb"]["weights"], 4.0)
    close(m["components_gb"]["gradients"], 4.0)
    close(m["components_gb"]["optimizer"], 8.0)            # m + v only
    close(m["components_gb"]["activations"], 2 * 16 * 142_606_336 / GB + 4 * 1024 * 2 * 32000 / GB)


def test_lora_much_smaller_than_full_and_qlora_smaller_than_lora():
    full, lora, qlora = (tiny(x, lora_rank=8)["memory"] for x in ("full", "lora", "qlora"))
    T = 2_097_152
    assert lora["trainable_parameters"] == T
    close(lora["components_gb"]["weights"], (2e9 + 4 * T) / GB)          # frozen bf16 base + fp32 adapters
    close(lora["components_gb"]["gradients"], 4 * T / GB, 0.006)          # adapters only (GB rounded to 0.01)
    close(lora["components_gb"]["optimizer"], 8 * T / GB, 0.006)          # AdamW m+v on adapters only
    assert lora["components_gb"]["activations"] == full["components_gb"]["activations"], "same forward/backward"
    states_full = full["components_gb"]["gradients"] + full["components_gb"]["optimizer"]
    states_lora = lora["components_gb"]["gradients"] + lora["components_gb"]["optimizer"]
    assert states_lora < states_full / 100, (states_lora, states_full)
    assert lora["min_total_gb"] < full["min_total_gb"] / 3
    emb = 2 * 32000 * 2048                                                # embeddings + head stay 16-bit
    qw = (1e9 - emb) * (0.5 + 0.127 / 8) + emb * 2 + 4 * T
    close(qlora["components_gb"]["weights"], qw / GB)
    assert qlora["method"] == "training_qlora"
    assert qlora["min_total_gb"] < lora["min_total_gb"]
    assert any("NF4" in a for a in qlora["assumptions"])


def test_inference_kv_cache_scales_and_quantization():
    base = {"workload_type": "inference", "model": {"name": "meta-llama/Llama-3.1-8B"}, "quantization": "awq",
            "inference": {"engine": "vllm", "concurrency": 16, "context_len": 8192}}
    e = est(**base)
    m = e["memory"]
    assert m["method"] == "inference" and m["confidence"] == "high"
    close(m["components_gb"]["kv_cache"], 17.18)
    emb = 2 * 128256 * 4096
    close(m["components_gb"]["weights"], ((8.03e9 - emb) * (0.5 + 20 / 128 / 8) + emb * 2) / GB)   # 5.73
    assert m["components_gb"]["gradients"] == 0 and m["components_gb"]["optimizer"] == 0
    assert m["usable_vram_fraction"] == 0.9 and any("90%" in a for a in m["assumptions"])
    e2 = est(**{**base, "inference": {**base["inference"], "concurrency": 32}})
    close(e2["memory"]["components_gb"]["kv_cache"], 2 * 17.18, 0.02)
    bf16 = est(**{**base, "quantization": None})
    close(bf16["memory"]["components_gb"]["weights"], 16.06)
    assert bf16["memory"]["min_total_gb"] > m["min_total_gb"]


def test_unknown_inference_inputs_are_assumed_and_asked():
    e = est(workload_type="inference", model={"name": "meta-llama/Llama-3.1-8B"})
    m = e["memory"]
    assert m["confidence"] == "medium" and m["range_gb"] and m["range_gb"][0] < m["range_gb"][1]
    fields = {q["field"] for q in e["questions"]}
    assert {"inference.concurrency", "inference.context_len", "precision"} <= fields, fields
    assert any("context length not stated" in a for a in m["assumptions"])


def test_unknown_params_unavailable_with_questions():
    e = est(workload_type="fine_tuning", frameworks=["pytorch", "transformers"])
    m = e["memory"]
    assert e["needs_gpu"] is True
    assert m["method"] == "unavailable" and m["confidence"] == "none"
    assert m["min_total_gb"] is None and m["recommended_gb"] is None and m["components_gb"] is None
    assert e["gpu_count"]["suggested"] is None
    fields = [q["field"] for q in e["questions"]]
    assert "model.name" in fields and "training.method" in fields, fields
    assert all(q["why"] for q in e["questions"])
    e = est(model={"name": "my secret model"})
    assert e["memory"]["method"] == "unavailable" and "workload_type" in [q["field"] for q in e["questions"]]


def test_needs_gpu_on_evidence_only():
    assert est(workload_type="no_gpu")["needs_gpu"] is False
    e = est(frameworks=["flask", "sqlalchemy", "celery"])
    assert e["needs_gpu"] is False and "no ML framework" in e["needs_gpu_reason"]
    assert e["questions"] == [] and e["memory"]["method"] == "unavailable" and e["gpu_count"]["suggested"] == 0
    assert est()["needs_gpu"] is None
    assert est(frameworks=["flask", "torch"])["needs_gpu"] is True
    assert est(frameworks=["flask"], model={"name": "gpt2"})["needs_gpu"] is True


def test_sharding_and_multi_gpu():
    e = est(workload_type="fine_tuning", model={"name": "meta-llama/Llama-3.1-70B"},
            training={"method": "full", "micro_batch_size": 1, "seq_len": 2048, "gradient_checkpointing": True},
            precision="bf16")
    m = e["memory"]
    by = m["per_gpu_by_count"]
    one, four = by[1]["components_gb"], by[4]["components_gb"]
    close(one["optimizer"], 70.6 * 12)                               # 847.2 GB of AdamW + master
    close(four["optimizer"], one["optimizer"] / 4, 0.02)              # ZeRO-3 shards states ...
    close(four["gradients"], one["gradients"] / 4, 0.02)              # ... gradients ...
    close(four["weights"], one["weights"] / 4, 0.02)                  # ... and weights
    assert four["activations"] == one["activations"], "activations are per GPU (own micro-batch)"
    assert by[1]["min_gb"] > 1000
    assert e["gpu_count"]["suggested"] == 8 and e["interconnect"]["needed"] is True
    assert "ZeRO-3" in by[8]["parallelism"]
    assert m["per_gpu_gb"] == by[8]["recommended_gb"] and m["for_gpu_count"] == 8
    # ZeRO stage 0 (pure data parallel) never shrinks per-GPU memory
    z0 = est(workload_type="fine_tuning", model={"name": "meta-llama/Llama-3.1-70B"},
             training={"method": "full", "micro_batch_size": 1, "seq_len": 2048, "gradient_checkpointing": True,
                       "zero_stage": 0}, precision="bf16")
    assert z0["gpu_count"]["suggested"] is None and "ZeRO stage 0" in z0["gpu_count"]["reason"]
    # CPU offload moves optimizer states off the GPU
    off = tiny("full", cpu_offload=True)["memory"]
    assert off["components_gb"]["optimizer"] == 0.0


def test_resnet50_activation_from_catalog():
    m = est(workload_type="training", model={"name": "resnet50"}, precision="fp32",
            training={"micro_batch_size": 64, "optimizer": "sgd"})["memory"]
    close(m["components_gb"]["activations"], 27_948_032 * 64 * 4 / GB)       # 7.15
    close(m["components_gb"]["weights"], 0.025557e9 * 4 / GB)
    close(m["components_gb"]["optimizer"], 0.025557e9 * 4 / GB)              # SGD momentum, fp32 weights
    assert m["range_gb"][1] > m["range_gb"][0], "the high end counts ReLU outputs separately"
    assert m["recommended_gb"] < 16


def test_small_cnn_without_architecture_is_a_lower_bound():
    e = est(workload_type="training", frameworks=["pytorch"], model={"parameters_b": 0.0005})
    m = e["memory"]
    assert m["method"] == "training_full" and m["components_gb"]["activations"] is None
    assert m["lower_bound"] is True and m["confidence"] == "low"
    assert any("activation memory not estimated" in a for a in m["assumptions"])
    assert "model.name" in [q["field"] for q in e["questions"]]


def test_catalog():
    for e in C.all_models():
        assert e["source"].startswith("https://"), e["id"]
        assert e["parameters_b"] and e["parameters_b"] > 0, e["id"]
        if e["kind"] in ("decoder", "moe"):
            for k in ("num_layers", "hidden_size", "num_attention_heads", "num_kv_heads", "head_dim", "vocab_size",
                      "max_context"):
                assert e[k], (e["id"], k)
    ll = C.get("meta-llama/Llama-3.1-8B")
    assert (ll["parameters_b"], ll["num_layers"], ll["hidden_size"], ll["num_kv_heads"], ll["head_dim"],
            ll["vocab_size"], ll["max_context"]) == (8.03, 32, 4096, 8, 128, 128256, 131072)
    assert C.lookup("7B llama")["entry"]["id"] == "meta-llama/Llama-2-7b-hf"
    assert C.lookup("Llama-3-8B")["entry"]["id"] == "meta-llama/Meta-Llama-3-8B"
    assert C.lookup("llama 3.1 70b")["entry"]["id"] == "meta-llama/Llama-3.1-70B"
    assert C.lookup("mixtral 8x7b")["entry"]["active_parameters_b"] == 12.9
    assert C.lookup("Qwen/Qwen2.5-7B-Instruct")["match"] == "exact"
    assert C.lookup("some 7b model") is None and C.lookup("") is None and C.lookup(None) is None
    loose = C.lookup("llama 70b")
    assert loose["match"] == "loose" and len(loose["candidates"]) == 3
    # a loose match with several candidates lowers confidence
    m = est(workload_type="inference", model={"name": "llama 70b"},
            inference={"concurrency": 1, "context_len": 4096}, precision="bf16")["memory"]
    assert m["confidence"] == "low"
    # request values win over the catalog
    m = est(workload_type="inference", model={"name": "meta-llama/Llama-3.1-8B", "parameters_b": 9.0},
            precision="bf16", inference={"concurrency": 1, "context_len": 1024})["memory"]
    close(m["components_gb"]["weights"], 18.0)


def test_requirements_validation():
    W.model_validate({})
    for bad in ({"unknown": 1}, {"model": {"name": "x", "params": 7}}, {"precision": "fp64"},
                {"training": {"micro_batch_size": 0}}, {"model": {"parameters_b": -1}},
                {"constraints": {"vendor": "Apple"}}, {"hardware_hints": {"cuda_min": "twelve"}},
                {"frameworks": ["x" * 65]}, {"constraints": {"include_providers": [""]}},
                {"inference": {"engine": "custom"}}):
        try:
            W.model_validate(bad)
        except ValidationError:
            continue
        raise AssertionError(f"accepted {bad}")
    r = W.model_validate({"frameworks": ["PyTorch", "pytorch", " PEFT "],
                          "constraints": {"exclude_providers": ["Lambda"]}})
    assert r.frameworks == ["pytorch", "peft"] and r.constraints.exclude_providers == ["lambda"]
    assert r.constraints.allow_spot is None, "not stated: null (treated as on-demand only)"


def test_estimate_is_pure_and_deterministic():
    import json

    a = json.dumps(tiny("lora"), sort_keys=True)
    b = json.dumps(tiny("lora"), sort_keys=True)
    assert a == b


def main():
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


if __name__ == "__main__":
    main()
