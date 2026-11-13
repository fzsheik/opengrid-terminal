"""Workload intelligence review (2026-11; methodology/workload-intelligence-review.md): the additions on top of the
estimator and recommender.

Pure (no database):
    workload_fit   link bandwidth, interconnect evidence (listing beats spec sheet only when explicit; "PCIe" text
                   never downgrades; fabric words are whole words), parallelism, communication vs compute against
                   hand-computed numbers, the decode roofline and its targets, cost per million tokens, multi-GPU advice
    measured       signatures, GPU-name verification, job -> observation rules (sandbox / simulated / failed /
                   mismatched GPU never count; warm-up epoch dropped), measured runtime arithmetic, confidence
    explain        confidence levels and their limiting component, factor deltas
    performance    max_steps -> tokens; requirements accept max_steps / deadline_hours; the MNIST catalog entry
    disclosure     the new blocks never compare against an internal option's price, stock or score
Database (scratch og_test_workload_intel):
    record_job / backfill: only consented, completed REAL jobs become observations; idempotent
    lookup: zero data -> {} ; consent=false rows never read
    recommend: zero data -> nothing labelled measured; with a measurement -> measured runtime and job cost, labelled,
               preferred for that option; explanation / decision / multi_gpu blocks present; deadline flags
    migration 0037 upgrades and downgrades cleanly

Run:  .venv/bin/python tests/test_workload_intelligence.py
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace as NS

os.environ.setdefault("OPENGRID_NO_JOBS", "1")
os.environ.setdefault("POLLER_ENABLED", "false")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import scratchdb  # noqa: E402  (first: isolates the process from real providers)
import fixtures  # noqa: E402

import normalize  # noqa: E402
from accounts.auth import OPERATOR, PUBLIC  # noqa: E402
from config import settings  # noqa: E402
from intelligence import explain as X  # noqa: E402
from intelligence import measured as MS  # noqa: E402
from intelligence import models_catalog as C  # noqa: E402
from intelligence import performance as P  # noqa: E402
from intelligence import workload_fit as F  # noqa: E402
from intelligence.estimate import estimate  # noqa: E402
from intelligence.recommend import recommend  # noqa: E402
from intelligence.requirements import WorkloadRequirements as W  # noqa: E402
from routing import scoring  # noqa: E402
from tables import ComputeListingRow  # noqa: E402

DB = os.environ.get("OG_TEST_WORKLOAD_INTEL_DB", "og_test_workload_intel")
NOW = datetime.now(timezone.utc).replace(microsecond=0)
T4, L4, A100, H100 = "NVIDIA T4 16GB", "NVIDIA L4 24GB", "NVIDIA A100 80GB SXM4", "NVIDIA H100 80GB SXM5"
SXM = {"vram_gb": 80, "memory_bandwidth_tbps": 3.35, "bf16_tflops_dense": 989.5, "nvlink": "NVLink 4, 900 GB/s",
       "interconnect": "PCIe Gen5", "form_factor": "SXM"}
PCIE = {"vram_gb": 48, "memory_bandwidth_tbps": 0.864, "bf16_tflops_dense": 181.0, "nvlink": None,
        "interconnect": "PCIe Gen4", "form_factor": "PCIe"}
BRIDGED = {"vram_gb": 80, "memory_bandwidth_tbps": 2.0, "bf16_tflops_dense": 756, "nvlink": "NVLink bridge (pairs), 600 GB/s",
           "interconnect": "PCIe Gen5", "form_factor": "PCIe"}


def close(a, b, tol=1e-6):
    assert a is not None and abs(a - b) <= tol * max(1.0, abs(b)), f"{a} != {b}"


# ------------------------------------------------------------------ workload_fit (pure)

def test_link_bandwidth():
    close(F.link_bandwidth(SXM, "nvlink")["gbps"], 450.0)                    # 900 GB/s total -> 450 per direction
    close(F.link_bandwidth(PCIE, "pcie")["gbps"], 31.5)                      # Gen4 x16
    close(F.link_bandwidth(BRIDGED, "nvlink_bridge")["gbps"], 300.0)
    assert F.link_bandwidth({"nvlink": "NVLink 5, 1.8 TB/s"}, "nvlink")["gbps"] == 900.0
    assert F.link_bandwidth({}, "pcie")["gbps"] is None, "unknown PCIe generation stays unknown"
    assert F.link_bandwidth(SXM, "single_gpu")["gbps"] is None


def test_interconnect_evidence():
    e = F.interconnect_evidence(SXM, 8, {"raw_gpu_name": "H100 SXM5 80GB", "sku": "8x-h100-nvswitch"})
    assert e["gpu_link"] == "nvlink" and e["gpu_link_source"] == "listing" and "NVSwitch" in e["gpu_link_note"]
    e = F.interconnect_evidence(BRIDGED, 2, {"raw_gpu_name": "H100 PCIe"})
    assert e["gpu_link_source"] == "spec_sheet" and e["gpu_link"] == P.interconnect(BRIDGED, 2)["kind"], \
        "'PCIe' in a listing is the form factor: it never downgrades (or confirms) a bridge"
    e = F.interconnect_evidence(BRIDGED, 2, {"raw_gpu_name": "H100 PCIe with NVLink bridge"})
    assert e["gpu_link_source"] == "listing"
    e = F.interconnect_evidence(PCIE, 2, {"raw_gpu_name": "L40S", "provider_tier": "bare-metal-infiniband"})
    assert e["gpu_link"] == "pcie" and e["node_fabric"] == "infiniband" and e["node_fabric_source"] == "listing"
    e = F.interconnect_evidence(PCIE, 2, {"raw_gpu_name": "L40S", "sku": "amd-epyc-processor-box"})
    assert e["node_fabric"] is None, "'processor' is not RoCE: fabric words match whole words only"
    e = F.interconnect_evidence(PCIE, 1, {})
    assert e["gpu_link"] == "single_gpu" and e["per_direction_gbps"] is None and e["node_fabric"] is None


def test_parallelism():
    assert F.parallelism("training_full", 1, zero_stage=None, link_kind="single_gpu")["strategy"] == "single_gpu"
    assert F.parallelism("training_full", 4, zero_stage=None, link_kind="nvlink")["strategy"] == "fsdp"
    assert F.parallelism("training_full", 4, zero_stage=0, link_kind="nvlink")["strategy"] == "ddp"
    assert F.parallelism("training_full", 4, zero_stage=2, link_kind="nvlink")["strategy"] == "zero2"
    p = F.parallelism("inference", 2, zero_stage=None, link_kind="pcie")
    assert p["strategy"] == "tensor_parallel" and "--tensor-parallel-size 2" in p["configure"] and p.get("warning")
    assert "frozen base weights" in F.parallelism("training_lora", 2, zero_stage=3, link_kind="nvlink", lora=True)["configure"]


def test_communication_hand_computed():
    prof = {"batch": (1, 4, 4), "seq": (2048, 2048, 2048), "ckpt": (True, True, True)}
    c = F.communication("training_full", 2, params_b=8.0, trainable_params=8e9, zero_stage=None, profile=prof,
                        spec=SXM, link_kind="nvlink", precision="bf16")
    # FSDP, n=2, f=1/2: weights gathered twice (2 x 0.5 x 8e9 x 2 B) + grads reduce-scattered (0.5 x 8e9 x 2 B) = 24e9 B
    assert c["known"] and c["bytes_per_gpu_per_step"] == 24_000_000_000
    close(c["comm_seconds_lower_bound"], round(24e9 / 450e9, 4))
    # compute: (6 + 2 ckpt) x 8e9 x (4 x 2048 tokens) / (989.5e12 x 35%)
    close(c["compute_seconds"], round(8 * 8e9 * 4 * 2048 / (989.5e12 * 0.35), 4))
    assert c["comm_to_compute"] < 0.3 and c["verdict"].startswith("compute-bound")
    pc = F.communication("training_full", 2, params_b=8.0, trainable_params=8e9, zero_stage=None, profile=prof,
                         spec=PCIE, link_kind="pcie", precision="bf16")
    assert pc["comm_to_compute"] > c["comm_to_compute"], "PCIe moves the same bytes slower"
    ddp = F.communication("training_full", 2, params_b=8.0, trainable_params=8e9, zero_stage=0, profile=prof,
                          spec=SXM, link_kind="nvlink", precision="bf16")
    assert ddp["bytes_per_gpu_per_step"] == 16_000_000_000 and "DDP" in ddp["pattern"]
    assert F.communication("training_full", 1, params_b=8.0, trainable_params=None, zero_stage=None, profile=prof,
                           spec=SXM, link_kind="single_gpu", precision="bf16") is None
    assert F.communication("inference", 2, params_b=8.0, trainable_params=None, zero_stage=None, profile=prof,
                           spec=SXM, link_kind="nvlink", precision="bf16") is None
    unk = F.communication("training_full", 2, params_b=8.0, trainable_params=None, zero_stage=None,
                          profile={"batch": None, "seq": None}, spec=SXM, link_kind="nvlink", precision="bf16")
    assert unk["known"] is False, "no micro batch / sequence: unknown, not guessed"


def test_inference_roofline_and_targets():
    prof = {"concurrency": (1, 1, 1), "precision": "bf16"}
    inf = F.inference_throughput(SXM, 1, per_gpu_gb={"weights": 16.0, "kv_cache": 0.0}, profile=prof, params_b=8.0,
                                 active_params_b=None, ic_kind="single_gpu")
    close(inf["per_stream_tokens_per_s"], 3.35e12 * 0.60 / 16e9, tol=1e-3)       # about 125.6 tok/s
    assert inf["kind"] == "estimated" and "not a benchmark" in inf["basis"]
    lo, hi = inf["aggregate_range"]
    assert lo <= inf["aggregate_tokens_per_s"] <= hi
    # 16 concurrent sequences with a KV cache: aggregate rises, per stream falls
    many = F.inference_throughput(SXM, 1, per_gpu_gb={"weights": 16.0, "kv_cache": 8.0},
                                  profile={"concurrency": (16, 16, 16), "precision": "bf16"}, params_b=8.0,
                                  active_params_b=None, ic_kind="single_gpu", target_tps=100_000)
    assert many["aggregate_tokens_per_s"] > inf["aggregate_tokens_per_s"]
    assert many["per_stream_tokens_per_s"] < inf["per_stream_tokens_per_s"]
    assert many["meets_target"] is False and "fast end" in many["target_note"]
    easy = F.inference_throughput(SXM, 1, per_gpu_gb={"weights": 16.0, "kv_cache": 0.0}, profile=prof, params_b=8.0,
                                  active_params_b=None, ic_kind="single_gpu", target_tps=10, target_latency_ms=60_000,
                                  output_tokens=256)
    assert easy["meets_target"] is True and easy["meets_latency"] is True
    cpm = F.cost_per_million_tokens(2.0, inf)
    close(cpm["usd"], round(2.0 / (inf["aggregate_tokens_per_s"] * 3600) * 1e6, 4))
    assert cpm["range_usd"][0] <= cpm["usd"] <= cpm["range_usd"][1]
    assert F.inference_throughput(SXM, 1, per_gpu_gb=None, profile=prof, params_b=8.0, active_params_b=None,
                                  ic_kind="single_gpu") is None, "no memory components: nothing invented"


def test_multi_gpu_advice():
    a = F.multi_gpu_advice(method="training_full", params_b=8.0, suggested=1, zero_stage=None, fits_one=True,
                           scale_pairs=[])
    assert a["verdict"] == "single_gpu" and any("fits on one GPU" in r for r in a["when_not_to_go_multi_gpu"])
    a = F.multi_gpu_advice(method="training_full", params_b=8.0, suggested=1, zero_stage=None, fits_one=True,
                           scale_pairs=[{"gpu": H100, "link": "nvlink", "job_cost_ratio": 0.95}])
    assert a["verdict"] == "scale_up_optional"
    a = F.multi_gpu_advice(method="training_full", params_b=70.0, suggested=4, zero_stage=None, fits_one=False,
                           scale_pairs=[{"gpu": "NVIDIA L40S 48GB", "link": "pcie", "job_cost_ratio": None}])
    assert a["verdict"] == "multi_gpu_required" and any("PCIe-only" in r for r in a["when_not_to_go_multi_gpu"])
    assert "not benchmarks" in a["note"]


# ------------------------------------------------------------------ measured (pure)

def _job(**kw):
    t0 = NOW - timedelta(hours=2)
    base = dict(job_id="job-x", sandbox=False, outcome="succeeded", job_type="training", template="pytorch-mnist",
                evidence={"real_metrics": {"status": "pass"}}, selection={"provider": "syn_b", "gpu": L4},
                spec={"resources": {"gpu_count": 1}}, config={"batch_size": 128, "epochs": 3}, account_id=None,
                principal_kind="operator", started_at=t0, finished_at=None)
    base.update(kw)
    return NS(**base)


def _metrics(epochs=3, secs=30.0, samples=60000, src="runner", first_secs=None):
    out = []
    for e in range(1, epochs + 1):
        s = first_secs if (e == 1 and first_secs) else secs
        out += [{"epoch": e, "name": "epoch_seconds", "value": s, "source": src},
                {"epoch": e, "name": "train_samples", "value": samples, "source": src},
                {"epoch": e, "name": "samples_per_second", "value": samples / s, "source": src},
                {"epoch": e, "name": "train_loss", "value": 0.1, "source": src}]
    return out


def test_signatures_and_gpu_tokens():
    assert MS.signature(template="pytorch-mnist") == "template:pytorch-mnist"
    assert MS.signature(model="meta-llama/Llama-3.1-8B", method="training_lora", precision="bf16") == \
        "model:meta-llama/llama-3.1-8b|training_lora|bf16"
    assert MS.signature() is None
    assert MS._gpu_token(A100) == "A100" and MS._gpu_token("NVIDIA RTX 4090 24GB") == "4090"
    assert MS._gpu_token("NVIDIA L40S 48GB") == "L40S" and MS._gpu_token("AMD Instinct MI300X 192GB") == "MI300X"
    assert MS.gpu_matches(A100, "NVIDIA A100-SXM4-80GB") is True
    assert MS.gpu_matches("NVIDIA L40 48GB", "NVIDIA L40S") is False, "L40 is not L40S"
    assert MS.gpu_matches(L4, None) is None
    req = W.model_validate({"workload_type": "training", "model": {"name": "pytorch-mnist"}})
    assert MS.request_signature(req, {"method": "training_full"}) == "template:pytorch-mnist"


def test_observation_rules():
    row, why = MS.observation_from_job(_job(), _metrics(first_secs=45.0), consent=True, now=NOW,
                                       completed_at=NOW - timedelta(hours=2) + timedelta(seconds=400))
    assert why == "ok" and row["data_kind"] == "measured" and row["source_kind"] == "completed_job"
    close(row["throughput"], 2000.0)                    # epochs 2-3 only: the 45 s warm-up epoch is dropped
    assert row["epochs_measured"] == 2 and "warm-up" in row["notes"]
    close(row["overhead_s"], 400 - (45 + 30 + 30))       # initializing -> training_completed minus timed epochs
    assert row["throughput_unit"] == "samples_per_second" and row["workload_signature"] == "template:pytorch-mnist"
    assert row["gpu"] == L4 and row["provider"] == "syn_b" and row["consent"] is True
    for job, metrics, reason in (
            (_job(sandbox=True), _metrics(), "sandbox"),
            (_job(outcome="simulated"), _metrics(), "not_succeeded"),
            (_job(outcome="failed"), _metrics(), "not_succeeded"),
            (_job(evidence={"real_metrics": {"status": "simulated"}}), _metrics(), "no_real_metrics"),
            (_job(), _metrics(src="simulated"), "simulated_metrics"),
            (_job(), _metrics()[:-1] + [{"epoch": 3, "name": "train_loss", "value": 0.1, "source": "simulated"}],
             "simulated_metrics"),
            (_job(selection={}), _metrics(), "no_selection"),
            (_job(evidence={"real_metrics": {"status": "pass"},
                            "gpu_detected": {"status": "pass", "evidence": {"gpu_name": "NVIDIA A10G"}}}),
             _metrics(), "gpu_mismatch"),
            (_job(), [{"epoch": 1, "name": "train_loss", "value": 0.2, "source": "runner"}], "no_timed_epochs"),
            (_job(), [], "no_metrics")):
        r, w = MS.observation_from_job(job, metrics, consent=True, now=NOW)
        assert r is None and w == reason, (reason, w)
    one, _ = MS.observation_from_job(_job(), _metrics(epochs=1), consent=False, now=NOW)
    assert one["epochs_measured"] == 1 and "includes warm-up" in one["notes"] and one["consent"] is False
    ok, _ = MS.observation_from_job(
        _job(evidence={"real_metrics": {"status": "pass"},
                       "gpu_detected": {"status": "pass", "evidence": {"gpu_name": "NVIDIA L4"}}}),
        _metrics(), consent=True, now=NOW)
    assert ok is not None and ok["gpu_detected"] == "NVIDIA L4"


def test_measured_runtime_and_confidence():
    s = {"n": 1, "unit": "samples_per_second", "throughput_median": 2000.0, "throughput_range": [1600.0, 2500.0],
         "overhead_s_median": 300.0, "age_days": 2.0}
    rt = MS.runtime(s, {"samples": 180000, "basis": "60,000 samples x 3 epochs", "assumptions": []})
    close(rt["hours"], round((90 + 300) / 3600, 4))
    close(rt["range_hours"][0], round((180000 / 2500 + 300) / 3600, 4))
    close(rt["range_hours"][1], round((180000 / 1600 + 300) / 3600, 4))
    assert rt["kind"] == "measured" and rt["includes_setup"] is True
    assert MS.runtime(s, {"tokens": 1e9, "basis": "x"}) is None, "a token count never meets a samples/s measurement"
    assert MS.runtime(None, {"samples": 1}) is None
    assert MS.confidence(s)[0] == "medium"
    assert MS.confidence({**s, "n": 3, "age_days": 5})[0] == "high"
    assert MS.confidence({**s, "n": 5, "age_days": 90})[0] == "medium", "old measurements are not high confidence"
    req = W.model_validate({"workload_type": "training", "model": {"name": "pytorch-mnist"}})
    w = MS.request_work(req, {"method": "training_full"})
    assert w["samples"] == 60000 * 3 and any("template" in a for a in w["assumptions"])


# ------------------------------------------------------------------ explain (pure)

def _x(price=1.0, headroom=50.0, avail=True, fresh=1.0, rel=None, job=None, n=1, gpu=L4, executable=False):
    return {"price_inst": price, "headroom": headroom, "n": n, "gpu": gpu, "job": job,
            "c": {"available": avail, "score": 0.7, "age_seconds": 120,
                  "factors": {"freshness": {"value": fresh}, "reliability": {"value": rel, "note": None}}},
            "perf": {"effective": None, "basis": None, "interconnect": {"kind": "single_gpu"}},
            "option": {"option_id": f"{gpu}:{price}", "provider_display": "P", "tier": "balanced",
                       "execution": {"executable": executable}, "why": ["fits"]}}


def test_confidence_levels():
    mem = {"confidence": "high", "assumptions": []}
    c = X.confidence(_x(job={"usd": 1, "range_usd": [1, 2], "hours": 1, "range_hours": [1, 2],
                             "runtime_kind": "theoretical"}), mem)
    assert c["level"] == "low" and c["limited_by"] == ["runtime"], "a theoretical runtime is low confidence"
    c = X.confidence(_x(job={"usd": 1, "range_usd": [1, 2], "hours": 1, "range_hours": [1, 2],
                             "runtime_kind": "measured", "measured_confidence": ("high", "measured on 4 runs")}), mem)
    assert c["level"] == "high" and c["components"]["runtime"]["evidence"] == "measured"
    c = X.confidence(_x(avail=None), mem)
    assert c["level"] == "medium" and "availability" in c["limited_by"]
    c = X.confidence(_x(headroom=5.0), mem)
    assert c["components"]["memory_fit"]["level"] == "medium", "a tight fit is never high"
    c = X.confidence(_x(), {"confidence": "low", "assumptions": ["a", "b"]})
    assert c["level"] == "low"
    c = X.confidence(_x(), mem)
    assert c["components"]["reliability"]["level"] is None and c["level"] == "high", \
        "unproven reliability is reported, not counted against the option"
    assert X.confidence(_x(rel=0.5), mem)["level"] == "low"


def test_deltas():
    ref = _x(price=2.0, headroom=40.0)
    alt = _x(price=1.5, headroom=8.0, avail=None)
    d = {e["factor"]: e for e in X.deltas(alt, ref)}
    assert d["price_per_instance_hour"]["better"] == "this"
    assert d["memory_headroom_pct"]["better"] == "recommended"
    assert d["in_stock_reported"]["better"] == "recommended"
    assert "estimated_job_cost_usd" not in d, "no job estimate: no job delta invented"
    j = lambda usd, h, k="theoretical": {"usd": usd, "range_usd": [usd, usd], "hours": h, "range_hours": [h, h],  # noqa: E731
                                         "runtime_kind": k}
    d = {e["factor"]: e for e in X.deltas(_x(price=3.0, job=j(10.0, 2.0)), _x(price=2.0, job=j(12.0, 4.0)))}
    assert d["estimated_job_cost_usd"]["better"] == "this" and d["estimated_completion_hours"]["better"] == "this"
    assert d["price_per_instance_hour"]["better"] == "recommended"


# ------------------------------------------------------------------ requirements / work / catalog (pure)

def test_max_steps_and_deadline():
    req = W.model_validate({"workload_type": "fine_tuning", "model": {"name": "meta-llama/Llama-3.1-8B"},
                            "precision": "bf16", "training": {"method": "full", "max_steps": 1000,
                                                              "global_batch_size": 64, "seq_len": 2048},
                            "constraints": {"deadline_hours": 12}})
    e = estimate(req)
    assert e["work"]["tokens"] == 1000 * 64 * 2048 and "max_steps" in e["work"]["tokens_basis"]
    assert "training.dataset_tokens" not in [q["field"] for q in e["questions"]], "max_steps answers the data question"
    try:
        W.model_validate({"training": {"max_steps": 0}})
        raise AssertionError("max_steps 0 accepted")
    except ValueError:
        pass
    # without global batch size, max_steps alone is not enough: still unknown, never guessed
    e = estimate(W.model_validate({"workload_type": "fine_tuning", "model": {"name": "meta-llama/Llama-3.1-8B"},
                                   "training": {"method": "lora", "max_steps": 1000, "seq_len": 2048}}))
    assert e["work"]["tokens"] is None


def test_mnist_catalog_entry():
    m = C.get("pytorch-mnist")
    assert m and m["kind"] == "cnn" and m["parameters_b"] == 0.001199882 and m["source"].startswith("https://")
    assert C.lookup("mnist") is None, "no loose 'mnist' match: only the exact template alias"
    e = estimate(W.model_validate({"workload_type": "training", "model": {"name": "pytorch-mnist"},
                                   "precision": "fp32", "training": {"micro_batch_size": 128}}))
    assert e["memory"]["components_gb"]["activations"] is not None and e["memory"]["recommended_gb"] < 4


# ------------------------------------------------------------------ disclosure (pure)

def test_disclosure_of_new_blocks():
    from routing import disclosure as D
    out = {"options": [{"option_id": "pub", "visibility": "public", "summary": "x",
                        "decision": {"confidence": {"level": "low"}, "vs_recommended": [
                            {"factor": "price_per_instance_hour", "better": "this"},
                            {"factor": "estimated_job_cost_usd", "better": "this"},
                            {"factor": "in_stock_reported", "better": "recommended"},
                            {"factor": "memory_headroom_pct", "better": "same"}]}},
                       {"option_id": "int", "visibility": "internal"}],
           "explanation": {"recommended_option_id": "int", "label": "1 x H100 on Hidden", "rule": "simple",
                           "why": ["Recommended: the cheapest per hour ... $0.01/h"], "confidence": {"level": "high"},
                           "alternatives": [{"option_id": "pub", "label": "L4", "summary": "$0.10/h more",
                                             "recommended_wins_on": ["price_per_instance_hour", "memory_headroom_pct"],
                                             "this_wins_on": ["in_stock_reported"]}]},
           "multi_gpu": {"verdict": "scale_up_optional", "summary": "...", "scale_up": [
               {"gpu": H100, "option_ids": ["int", "int2"], "job_cost_ratio": 0.8}]}}
    D._recommendation_explanations(out, {"int", "int2"}, True)
    assert [e["factor"] for e in out["options"][0]["decision"]["vs_recommended"]] == ["memory_headroom_pct"]
    ex = out["explanation"]
    assert set(ex) == {"recommended_option_id", "label", "summary", "alternatives", "kind"}, sorted(ex)
    assert "rule" not in ex and "confidence" not in ex, "the rule and confidence reveal an internal listing's stock"
    assert ex["alternatives"][0]["recommended_wins_on"] == ["memory_headroom_pct"]
    assert ex["alternatives"][0]["this_wins_on"] == []
    assert out["multi_gpu"]["scale_up"][0]["job_cost_ratio"] is None and out["multi_gpu"]["verdict"] == "single_gpu"


# ------------------------------------------------------------------ database

def L(prov, gpu, price, *, count=1, available=True):
    return ComputeListingRow(
        provider=prov, listing_id=f"{prov}:{gpu}:{count}", sku=f"{prov}-sku", raw_gpu_name=gpu,
        canonical_gpu_name=gpu, gpu_count=count, region="us-east", country="US",
        price_per_gpu_hour=Decimal(str(price)), price_per_instance_hour=Decimal(str(round(price * count, 6))),
        currency="USD", market_type="on_demand", provider_tier=None, interruptible=False, available=available,
        capacity=None, capacity_unit=None, vcpu=None, ram_gb=None, storage_gb=None,
        observed_at=NOW - timedelta(minutes=1), first_seen_at=NOW - timedelta(days=3))


def _seed_job(s, jid, *, sandbox=False, outcome="succeeded", gpu=L4, provider="syn_b", account=None,
              principal="operator", src="runner", real="pass", detected=None, epochs=3, secs=30.0):
    from store.jobs import Job, JobEvent, JobMetric
    t0 = NOW - timedelta(hours=3)
    ev = {"real_metrics": {"status": real}}
    if detected:
        ev["gpu_detected"] = {"status": "pass", "evidence": {"gpu_name": detected}}
    s.add(Job(job_id=jid, account_id=account, principal_kind=principal, name=jid, job_type="training",
              template="pytorch-mnist", config={"batch_size": 128, "epochs": epochs},
              spec={"resources": {"gpu_count": 1}}, status="reconciled", outcome=outcome, sandbox=sandbox,
              selection={"provider": provider, "gpu": gpu}, evidence=ev, created_at=t0, updated_at=NOW,
              started_at=t0, finished_at=t0 + timedelta(seconds=900)))
    s.add(JobEvent(job_id=jid, at=t0 + timedelta(seconds=400), from_status="saving_artifacts",
                   to_status="training_completed", actor="runner"))
    for e in range(1, epochs + 1):
        for name, v in (("epoch_seconds", secs), ("train_samples", 60000.0), ("samples_per_second", 60000 / secs)):
            s.add(JobMetric(job_id=jid, seq=e, epoch=e, step=None, name=name, value=v, at=t0, source=src))


def seed(Session):
    from store.accounts import Account
    with Session.begin() as s:
        s.add_all([L("syn_a", T4, 0.30), L("syn_b", L4, 0.50), L("syn_a", A100, 1.50), L("syn_a", H100, 2.50),
                   L("syn_a", H100, 2.45, count=2)])
        s.add(Account(id=9, name="no-consent", status="active", plan="free", settings={}, is_operator=False))
        s.add(Account(id=10, name="consent", status="active", plan="free",
                      settings={MS.CONSENT_SETTING: True}, is_operator=False))


MNIST = {"workload_type": "training", "frameworks": ["pytorch"], "model": {"name": "pytorch-mnist"},
         "precision": "fp32", "training": {"epochs": 3, "micro_batch_size": 128}}


def test_db_zero_data_is_graceful():
    """No measurement exists (today's production state): nothing is labelled measured, nothing is invented."""
    assert MS.lookup([L4, T4], "template:pytorch-mnist", now=NOW) == {}
    out = recommend(W.model_validate(MNIST), PUBLIC, now=NOW)
    assert out["options"], "options still offered"
    for o in out["options"]:
        assert "measured" not in o["performance"] and "job_cost_measured" not in o["performance"]
        assert o["cost"]["estimated_total"] is None, "MNIST has no token count: no theoretical runtime either"
        assert o["decision"]["confidence"]["level"] in ("high", "medium", "low")
    assert out["picks"]["lowest_estimated_job_cost"] is None and out["picks"]["shortest_estimated_runtime"] is None
    assert out["picks_unavailable"]["shortest_estimated_runtime"]
    assert out["explanation"]["recommended_option_id"] == out["picks"]["best_for_workload"]["option_id"]
    assert out["multi_gpu"]["verdict"] == "single_gpu"


def test_db_record_jobs():
    with normalize.SessionLocal.begin() as s:
        _seed_job(s, "job-real")                                            # operator: consented
        _seed_job(s, "job-sim", sandbox=True, outcome="simulated", src="simulated", real="simulated")
        _seed_job(s, "job-fail", outcome="failed")
        _seed_job(s, "job-mixed", src="simulated")
        _seed_job(s, "job-wronggpu", detected="NVIDIA A10G")
        _seed_job(s, "job-cust", account=9, principal="api_key")            # recorded, consent false
        _seed_job(s, "job-cust-ok", account=10, principal="api_key", secs=40.0)   # opted in
    r = MS.record_job("job-real", now=NOW)
    assert r["recorded"] and r["reason"] == "ok"
    assert MS.record_job("job-real", now=NOW) == {"recorded": False, "reason": "already_recorded"}, "idempotent"
    assert MS.record_job("job-sim", now=NOW)["reason"] == "sandbox"
    assert MS.record_job("job-nope", now=NOW)["reason"] == "job_not_found"
    b = MS.backfill()
    assert b["recorded"] == 2, b                                            # job-cust (no consent) and job-cust-ok
    assert b["skipped"].get("simulated_metrics") == 1 and b["skipped"].get("gpu_mismatch") == 1, b
    assert "sandbox" not in b["skipped"], "sandbox jobs are never even scanned"
    from sqlalchemy import select

    from store.intelligence import PerformanceObservation as PO
    with normalize.SessionLocal() as s:
        rows = {r.job_id: r for r in s.scalars(select(PO))}
    assert set(rows) == {"job-real", "job-cust", "job-cust-ok"}
    assert rows["job-real"].consent is True and rows["job-cust"].consent is False and rows["job-cust-ok"].consent
    close(rows["job-real"].throughput, 2000.0)
    close(rows["job-real"].overhead_s, 400 - 90)
    summ = MS.lookup([L4], "template:pytorch-mnist", now=NOW)[L4][1]
    assert summ["n"] == 2, "the two consented runs, never the unconsented one"
    assert summ["throughput_range"] == [1500.0, 2000.0] and summ["kind"] == "measured"
    assert MS.lookup([L4], "model:something|training_full|bf16", now=NOW) == {}


def test_db_recommend_prefers_measured():
    out = recommend(W.model_validate(MNIST), PUBLIC, now=NOW)
    l4 = next(o for o in out["options"] if o["gpu"] == L4)
    m = l4["performance"]["measured"]
    assert m["kind"] == "measured" and m["n"] == 2 and m["includes_this_provider"] is True
    assert "providers" not in m, "provider names of measured runs are not exposed, only a count"
    rt = m["runtime"]
    assert rt["kind"] == "measured" and rt["hours"] > 0 and rt["range_hours"][0] <= rt["hours"] <= rt["range_hours"][1]
    jc = l4["performance"]["job_cost_measured"]
    assert jc["runtime_kind"] == "measured" and jc["usd"] > 0
    tot = l4["cost"]["estimated_total"]
    assert tot["runtime_kind"] == "measured" and tot["total_is_lower_bound"] is True and "storage" in tot["unknown_costs"]
    assert any("measured on 2 real OpenGrid runs" in w for w in l4["why"])
    assert not any("no measured benchmark" in x for x in l4["limitations"])
    assert l4["decision"]["confidence"]["components"]["runtime"]["evidence"] == "measured"
    # options on other GPUs stay theoretical / unknown and say so
    t4 = next(o for o in out["options"] if o["gpu"] == T4)
    assert "measured" not in t4["performance"] and any("no measured benchmark" in x for x in t4["limitations"])
    assert out["picks"]["lowest_estimated_job_cost"]["option_id"] == l4["option_id"]
    assert "measured runtime" in out["picks"]["lowest_estimated_job_cost"]["reason"]
    assert any("MEASURED" in n for n in out["notes"])
    assert out["picks"]["shortest_estimated_runtime"]["option_id"] == l4["option_id"]


def test_db_deadline_and_explanation():
    body = {**MNIST, "constraints": {"deadline_hours": 0.0001}}
    out = recommend(W.model_validate(body), PUBLIC, now=NOW)
    l4 = next(o for o in out["options"] if o["gpu"] == L4)
    assert l4["meets_deadline"] == "no" and any("deadline" in x for x in l4["limitations"])
    assert any(o["meets_deadline"] == "unknown" for o in out["options"] if o["gpu"] != L4), \
        "no runtime: the deadline is unknown, not met or missed"
    assert any("deadline" in n for n in out["notes"])
    ex = out["explanation"]
    best = out["picks"]["best_for_workload"]["option_id"]
    assert ex["recommended_option_id"] == best and ex["confidence"]["level"] in ("high", "medium", "low")
    for a in ex["alternatives"]:
        assert a["option_id"] != best and isinstance(a["recommended_wins_on"], list)
    for o in out["options"]:
        assert (o["decision"]["vs_recommended"] is None) == (o["option_id"] == best)


def test_db_llm_scale_up_blocks():
    """An 8B LoRA fine-tune with its dataset size (fits one H100, so 2 x H100 is a scale-up at the same provider):
    the 1 -> 2 pair, parallelism and communication blocks, theoretical whole-job totals."""
    body = {"workload_type": "fine_tuning", "model": {"name": "meta-llama/Llama-3.1-8B"}, "precision": "bf16",
            "training": {"method": "lora", "micro_batch_size": 1, "seq_len": 2048, "gradient_checkpointing": True,
                         "dataset_tokens": 2e8, "epochs": 1}}
    out = recommend(W.model_validate(body), OPERATOR, now=NOW)
    two = [o for o in out["options"] if o["gpu"] == H100 and o["gpu_count"] == 2]
    assert two, [(o["gpu"], o["gpu_count"]) for o in out["options"]]
    p = two[0]["performance"]
    assert p["parallelism"]["strategy"] == "fsdp" and p["communication"]["known"] is True
    assert p["interconnect"]["evidence"]["gpu_link"] == "nvlink"
    assert two[0]["cost"]["estimated_total"]["runtime_kind"] == "theoretical"
    pairs = out["multi_gpu"]["scale_up"]
    assert any(pr["gpu"] == H100 and pr["from"] == 1 and pr["to"] == 2 and pr["job_cost_ratio"] for pr in pairs), pairs


def test_migration_0037():
    """0037 applies on top of 0035 and downgrades cleanly (additive only)."""
    from alembic import command
    from sqlalchemy import create_engine, text

    import db as dbmod
    name = DB + "_mig"
    url = scratchdb.create(name)
    old = settings.database_url
    settings.database_url = url
    try:
        cfg = dbmod._alembic_config()
        command.upgrade(cfg, "0037_measured_performance")
        eng = create_engine(url)
        cols = "SELECT column_name FROM information_schema.columns WHERE table_name = 'performance_observations'"
        with eng.connect() as c:
            have = {r[0] for r in c.execute(text(cols))}
            assert {"workload_signature", "throughput", "throughput_unit", "overhead_s", "data_kind",
                    "provider", "observed_at"} <= have, have
            assert c.execute(text("SELECT count(*) FROM pg_indexes WHERE indexname = 'ux_perfobs_completed_job'")).scalar() == 1
        command.downgrade(cfg, "0035_power_markets")
        with eng.connect() as c:
            have = {r[0] for r in c.execute(text(cols))}
            assert "workload_signature" not in have and "gpu" in have, "0020 columns untouched"
        eng.dispose()
    finally:
        settings.database_url = old
        scratchdb.drop(name)


PURE = [test_link_bandwidth, test_interconnect_evidence, test_parallelism, test_communication_hand_computed,
        test_inference_roofline_and_targets, test_multi_gpu_advice, test_signatures_and_gpu_tokens,
        test_observation_rules, test_measured_runtime_and_confidence, test_confidence_levels, test_deltas,
        test_max_steps_and_deadline, test_mnist_catalog_entry, test_disclosure_of_new_blocks]
DBT = [test_db_zero_data_is_graceful, test_db_record_jobs, test_db_recommend_prefers_measured,
       test_db_deadline_and_explanation, test_db_llm_scale_up_blocks, test_migration_0037]


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
        for fn in DBT:          # in order: zero data first, then the recorded jobs are read back
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
