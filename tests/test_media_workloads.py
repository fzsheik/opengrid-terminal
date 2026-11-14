"""Audio, image + text and video workloads (intelligence/media.py; methodology/recommendations.md "Audio, image and
video workloads").

Pure (no database):
    requirements   `media` validation (bounds, WxH pattern, one modality or a list, extra fields rejected); an empty
                   `media` is the same as none; payloads without media give byte-identical estimates to the code before
                   this feature (tests/fixtures_media/old_payload_estimates.json, generated from c75316b)
    catalog        sourced numbers (config.json / model card / paper) for Whisper, wav2vec2, Parler-TTS, Kokoro, LLaVA,
                   Qwen2-VL, Llama 3.2 Vision, PaliGemma, CogVideoX, Wan2.1, HunyuanVideo; loose lookups
    token rules    LLaVA fixed / AnyRes, Qwen2-VL smart_resize and video budget, Llama 3.2 Vision tiles, Whisper windows,
                   video latents, all hand-computed
    memory         hand-computed components per modality (VLM KV with image tokens, cross-attention KV, Whisper decoder
                   caches, wav2vec2 frames, Parler-TTS codec-frame KV, video diffusion weights + activations + VAE decode,
                   Whisper LoRA activations, a quantized VLM's 16-bit vision tower); confidence low for video generation
    runtime        Whisper seconds per window hand-computed; replicas; every publisher-reported video generation time on
                   record falls inside the theoretical range
    questions, headline, intents (assistant), the contract mirror (server = CLI = /find schema)
Database (scratch og_test_media):
    recommend      10,000 h transcription: theoretical runtime / job cost per option, replicas, batch fan-out note,
                   no LLM decode roofline; video generation: only 80 GB-class GPUs fit, said in the explanation; VLM
                   images; old payloads carry no media blocks; visibility (internal listings never shown, a non-admin
                   answer does not change with the internal price); measured observations keep precedence;
                   the assistant answers a plain-language transcription ask with throughput and runtime

Run:  .venv/bin/python tests/test_media_workloads.py
"""

import json
import math
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("OPENGRID_NO_JOBS", "1")
os.environ.setdefault("POLLER_ENABLED", "false")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "cli" / "src"))

import scratchdb  # noqa: E402  (first: isolates the process from real providers)
import fixtures  # noqa: E402

import hardware  # noqa: E402
import normalize  # noqa: E402
from accounts.auth import OPERATOR, PUBLIC, Principal  # noqa: E402
from intelligence import media as MD  # noqa: E402
from intelligence import models_catalog as C  # noqa: E402
from intelligence.estimate import estimate  # noqa: E402
from intelligence.recommend import recommend  # noqa: E402
from intelligence.requirements import WorkloadRequirements as W  # noqa: E402
from routing import disclosure, scoring  # noqa: E402
from tables import ComputeListingRow  # noqa: E402

DB = os.environ.get("OG_TEST_MEDIA_DB", "og_test_media")
NOW = datetime.now(timezone.utc).replace(microsecond=0)
T4, L4, L40S, A100, H100 = ("NVIDIA T4 16GB", "NVIDIA L4 24GB", "NVIDIA L40S 48GB", "NVIDIA A100 80GB SXM4",
                            "NVIDIA H100 80GB SXM5")
GB = 1e9


def close(a, b, tol=1e-6):
    assert a is not None and abs(a - b) <= tol * max(1.0, abs(b)), f"{a} != {b}"


def est(body):
    return estimate(W.model_validate(body))


def overhead(other):
    return 1.5e9 + 0.05 * other


# ------------------------------------------------------------------ requirements

def test_requirements_validation():
    from pydantic import ValidationError

    r = W.model_validate({"media": {"modality": "audio", "image_resolution": "1280X720"}})
    assert r.media.modality == ["audio"] and r.media.image_resolution == "1280x720", "one value -> a list; WxH lowered"
    r = W.model_validate({"media": {"modality": ["image", "text", "image"]}})
    assert r.media.modality == ["image", "text"], "de-duplicated"
    bad = [{"media": {"modality": "smell"}}, {"media": {"images_per_request": 0}},
           {"media": {"images_per_request": 257}}, {"media": {"image_resolution": "1280*720"}},
           {"media": {"image_resolution": "99999x10"}}, {"media": {"audio_seconds": -1}},
           {"media": {"total_audio_hours": 2e7}}, {"media": {"output_video_seconds": 601}},
           {"media": {"diffusion_steps": 0}}, {"media": {"task": "dance"}}, {"media": {"file_contents": "x"}},
           {"media": {"fps": 0}}]
    for b in bad:
        try:
            W.model_validate(b)
        except ValidationError:
            continue
        raise AssertionError(f"accepted {b}")


def test_empty_media_is_no_media():
    for body in ({"workload_type": "inference", "model": {"name": "meta-llama/Llama-3.1-8B"}},
                 {"workload_type": "fine_tuning", "model": {"parameters_b": 7}, "training": {"method": "lora"}}, {}):
        a = json.dumps(est(body), sort_keys=True, default=str)
        b = json.dumps(est({**body, "media": {}}), sort_keys=True, default=str)
        c = json.dumps(est({**body, "media": {"modality": []}}), sort_keys=True, default=str)
        assert a == b == c, body
        assert '"media"' not in a


def test_backward_compat_golden():
    """Payloads without media: estimates byte-identical to the engine before this feature (c75316b)."""
    g = json.loads((Path(__file__).parent / "fixtures_media" / "old_payload_estimates.json").read_text())
    assert len(g["cases"]) >= 20
    for c in g["cases"]:
        now = json.loads(json.dumps(estimate(W.model_validate(c["payload"])), sort_keys=True, default=str))
        assert now == c["estimate"], f"estimate changed for an old payload: {c['payload']}"


# ------------------------------------------------------------------ catalog

def test_catalog_sourced_numbers():
    media = [e for e in C.all_models() if e["kind"] in C.MEDIA_KINDS]
    assert len(media) >= 25, len(media)
    for e in media:
        assert e["source"].startswith("https://"), e["id"]
        assert e.get("modality") and e.get("media") is not None, e["id"]
        assert e["parameters_b"] and e["parameters_b"] > 0, e["id"]
    w = C.get("openai/whisper-large-v3")["media"]
    assert (w["encoder_layers"], w["decoder_layers"], w["d_model"], w["mel_bins"], w["max_target_positions"]) == \
        (32, 32, 1280, 128, 448)
    t = C.get("openai/whisper-large-v3-turbo")
    assert t["parameters_b"] == 0.809 and t["media"]["decoder_layers"] == 4
    assert [C.get(f"openai/whisper-{s}")["parameters_b"] for s in ("tiny", "base", "small", "medium")] == \
        [0.039, 0.074, 0.244, 0.769]
    q = C.get("Qwen/Qwen2-VL-7B-Instruct")
    assert q["parameters_b"] == 8.291375616 and (q["num_layers"], q["hidden_size"], q["num_kv_heads"]) == (28, 3584, 4)
    assert q["media"]["max_pixels"] == 12845056 and q["media"]["video_total_pixels"] == 24576 * 784
    ll = C.get("meta-llama/Llama-3.2-11B-Vision")
    assert ll["media"]["cross_attention_layers"] == 8 and ll["num_layers"] == 40
    assert C.get("llava-hf/llava-1.5-7b-hf")["media"]["tokens_per_image"] == 576
    assert [C.get(f"google/paligemma-3b-pt-{r}")["media"]["tokens_per_image"] for r in (224, 448, 896)] == \
        [256, 1024, 4096]
    cog = C.get("THUDM/CogVideoX-5b")["media"]
    assert cog["transformer_params_b"] == 5.570283072 and cog["publisher"]["seconds"] == {"A100": 180, "H100": 90}
    wan = C.get("Wan-AI/Wan2.1-T2V-14B")
    assert (wan["num_layers"], wan["hidden_size"], wan["media"]["ffn_dim"]) == (40, 5120, 13824)
    hy = C.get("tencent/HunyuanVideo")["media"]
    assert hy["publisher"]["vram_gb"] == 60 and hy["cfg_forward_passes"] == 1
    assert C.get("parler-tts/parler-tts-large-v1")["parameters_b"] == 2.2
    # loose lookups pick the right family; LLM lookups unchanged
    for text, want in (("whisper large v3", "openai/whisper-large-v3"), ("whisper turbo", "openai/whisper-large-v3-turbo"),
                       ("qwen2 vl 72b", "Qwen/Qwen2-VL-72B-Instruct"), ("Llama 3.2 11B Vision",
                                                                         "meta-llama/Llama-3.2-11B-Vision"),
                       ("wan2.1 14b", "Wan-AI/Wan2.1-T2V-14B"), ("llama 3.1 8b", "meta-llama/Llama-3.1-8B"),
                       ("qwen2.5 7b", "Qwen/Qwen2.5-7B"), ("gemma 2 9b", "google/gemma-2-9b")):
        assert C.lookup(text)["entry"]["id"] == want, (text, C.lookup(text))


# ------------------------------------------------------------------ token rules

def test_token_rules_hand_computed():
    q = C.get("Qwen/Qwen2-VL-7B-Instruct")["media"]
    assert MD.smart_resize(1024, 1024) == (1036, 1036)                    # round(1024 / 28) = 37 -> 37 x 28
    assert MD.image_tokens(q, 1024, 1024)["tokens"] == 37 * 37 == 1369
    assert MD.image_tokens(q, 1280, 720)["tokens"] == (1288 * 728) // 784 == 1196
    huge = MD.image_tokens(q, 8000, 8000)["tokens"]
    assert huge <= 16384, "max_pixels caps an image at 16,384 tokens"
    v = MD.video_tokens(q, 10, 2.0, 1280, 720)
    # 20 frames; a 1280x720 frame is above 768 x 784 pixels: scaled by sqrt(921600 / 602112), floored to 28 -> 1008x560
    assert v["frames"] == 20 and v["frame_resized"] == "1008x560" and v["tokens_per_frame_pair"] == 720
    assert v["tokens"] == 10 * 720 and not v["capped"]
    v = MD.video_tokens(q, 60, 2.0, 1280, 720)
    assert v["tokens"] == 24576 and v["capped"], "60 pairs x 720 = 43,200 > the 24,576-token video budget"
    lv = C.get("llava-hf/llava-v1.6-vicuna-7b-hf")["media"]
    assert MD.image_tokens(lv, 1024, 768)["tokens"] == 576 * (1 + 4)      # best grid 672x672: 4 tiles + base
    assert MD.image_tokens(lv, 336, 336)["tokens"] == 576 * (1 + 2)       # 336x672 grid (first of the tied grids)
    assert MD.image_tokens(C.get("llava-hf/llava-1.5-7b-hf")["media"], None, None)["tokens"] == 576
    ml = C.get("meta-llama/Llama-3.2-11B-Vision")["media"]
    assert MD.image_tokens(ml, 1120, 1120)["tokens"] == 4 * 1601 and MD.image_tokens(ml, 500, 400)["tokens"] == 1601
    assert MD.image_tokens(ml, 4000, 1000)["tiles"] == 4, "at most 4 tiles"
    assert MD.whisper_windows(600) == 20 and MD.whisper_windows(31) == 2 and MD.whisper_windows(1) == 1
    cog = C.get("THUDM/CogVideoX-2b")["media"]
    lat = MD.video_latent(cog, 6, None, 720, 480)
    assert (lat["frames"], lat["latent_frames"], lat["tokens"]) == (49, 13, 13 * 30 * 45)
    hy = C.get("tencent/HunyuanVideo")["media"]
    lat = MD.video_latent(hy, None, None, 1280, 720)
    assert (lat["frames"], lat["latent_frames"], lat["tokens"]) == (129, 33, 33 * 45 * 80)
    wan = C.get("Wan-AI/Wan2.1-T2V-1.3B")["media"]
    lat = MD.video_latent(wan, 5, None, 832, 480)
    assert (lat["frames"], lat["latent_frames"], lat["tokens"]) == (81, 21, 21 * 30 * 52)


# ------------------------------------------------------------------ memory (hand-computed)

def comps(e):
    return {k: v for k, v in e["memory"]["per_gpu_by_count"][1]["components_gb"].items()}


def test_memory_whisper_batch():
    e = est({"workload_type": "batch", "model": {"name": "openai/whisper-large-v3"}, "precision": "bf16",
             "inference": {"concurrency": 16}, "media": {"total_audio_hours": 10000}})
    weights = 1.55e9 * 2
    cross = 2 * 32 * 1280 * 1500 * 16 * 2           # decoder cross-attention cache over 1500 encoder positions
    selfkv = 2 * 32 * 1280 * 448 * 16 * 2           # decoder self-attention cache, 448 tokens
    act = 16 * 1500 * 1280 * 34                     # one encoder layer's activations (SDPA), 16 windows
    other = weights + cross + selfkv + act
    total = other + overhead(other)
    m = e["memory"]
    close(m["min_total_gb"], round(total / GB, 2), tol=1e-3)
    close(m["recommended_gb"], round(total * 1.2 / GB, 2), tol=1e-3)
    c = comps(e)
    close(c["kv_cache"], round((cross + selfkv) / GB, 2), tol=1e-3)
    close(c["activations"], round(act / GB, 2), tol=1e-3)
    assert m["confidence"] == "high", m["confidence"]
    md = e["media"]
    assert md["media_kind"] == "asr" and md["inputs"]["total_windows"] == 1_200_000
    assert md["work"]["known"] and md["work"]["items"] == 1_200_000
    assert e["runtime"]["estimable"] is True
    assert "30 s window" in md["headline"]


def test_memory_vlm_image_tokens():
    e = est({"workload_type": "inference", "model": {"name": "llava-hf/llava-1.5-7b-hf"}, "precision": "bf16",
             "inference": {"concurrency": 8, "context_len": 2048}, "media": {"images_per_request": 2}})
    weights = 7.063427072e9 * 2
    kv = 2 * 32 * 32 * 128 * (2048 + 2 * 576) * 8 * 2       # text context + 2 images x 576 tokens
    act = 576 * 1024 * 34                                    # one CLIP layer over one image's 576 patches
    other = weights + kv + act
    close(e["memory"]["min_total_gb"], round((other + overhead(other)) / GB, 2), tol=1e-3)
    close(comps(e)["kv_cache"], round(kv / GB, 2), tol=1e-3)
    assert e["media"]["inputs"]["media_tokens_per_request"][0] == 1152
    assert "1,152 tokens per request" in e["media"]["headline"]
    # Qwen2-VL: dynamic resolution
    e = est({"workload_type": "inference", "model": {"name": "Qwen/Qwen2-VL-7B-Instruct"}, "precision": "bf16",
             "inference": {"concurrency": 1, "context_len": 4096},
             "media": {"images_per_request": 1, "image_resolution": "1024x1024"}})
    kv = 2 * 28 * 4 * 128 * (4096 + 1369) * 1 * 2
    act = 1369 * 4 * 1280 * 34                               # 2x2 merge: 4 patches per token, ViT hidden 1280
    close(comps(e)["kv_cache"], round(kv / GB, 2), tol=1e-3)
    close(comps(e)["activations"], round(act / GB, 2), tol=1e-3)
    # Llama 3.2 Vision: images go to cross-attention, the text cache lives in the 32 self-attention layers only
    e = est({"workload_type": "inference", "model": {"name": "meta-llama/Llama-3.2-11B-Vision"}, "precision": "bf16",
             "inference": {"concurrency": 1, "context_len": 4096},
             "media": {"images_per_request": 1, "image_resolution": "1120x1120"}})
    text_kv = 2 * 40 * 8 * 128 * 4096 * 2 * 32 / 40
    cross = 2 * 8 * 8 * 128 * (4 * 1601) * 1 * 2
    close(comps(e)["kv_cache"], round((text_kv + cross) / GB, 2), tol=1e-3)
    assert e["media"]["breakdown_gb"]["cross_attention_kv"] == round(cross / GB, 3)


def test_memory_quantized_vlm_keeps_vision_16bit():
    e = est({"workload_type": "inference", "model": {"name": "Qwen/Qwen2-VL-7B-Instruct"}, "quantization": "awq",
             "inference": {"concurrency": 1, "context_len": 1024}, "media": {"images_per_request": 1,
                                                                            "image_resolution": "512x512"}})
    P, emb, bpp = 8.291375616e9, 2 * 152064 * 3584, 0.5 + (20 / 128) / 8
    weights = (P - emb) * bpp + emb * 2 + 0.675e9 * (2 - bpp)
    close(comps(e)["weights"], round(weights / GB, 2), tol=1e-3)


def test_memory_audio_encoder_tts_whisper_training():
    e = est({"workload_type": "inference", "model": {"name": "facebook/wav2vec2-large-960h-lv60-self"},
             "precision": "bf16", "inference": {"concurrency": 1}, "media": {"audio_seconds": 30}})
    close(comps(e)["activations"], round(1 * 1500 * 1024 * 34 / GB, 2), tol=1e-3)     # 30 s x 50 frames/s
    e = est({"workload_type": "inference", "model": {"name": "parler-tts/parler-tts-mini-v1"}, "precision": "bf16",
             "inference": {"concurrency": 4}, "media": {"audio_seconds": 20}})
    steps = math.ceil(20 * 86) + 8                                  # 86 codec frames/s + 8-step codebook delay
    kv = 2 * 24 * 16 * 64 * (steps + 64) * 4 * 2 + 2 * 24 * 1024 * 64 * 4 * 2
    close(comps(e)["kv_cache"], round(kv / GB, 2), tol=1e-3)
    close(comps(e)["weights"], 1.8, tol=1e-3)
    e = est({"workload_type": "inference", "model": {"name": "kokoro"}, "media": {"audio_seconds": 5}})
    assert comps(e)["kv_cache"] == 0.0 and e["media"]["work"]["known"] is False
    e = est({"workload_type": "fine_tuning", "model": {"name": "openai/whisper-small"}, "precision": "bf16",
             "training": {"method": "full", "micro_batch_size": 8}})
    enc = 12 * 1500 * 8 * 768 * 34                                   # 12 encoder layers over 1500 positions
    dec = 12 * 448 * 8 * 768 * 34 + 4 * 448 * 8 * 51865              # 12 decoder layers + fp32 logits
    close(comps(e)["activations"], round((enc + dec) / GB, 2), tol=1e-3)
    assert e["media"].get("headline") is None, "no inference headline for a training run"


def test_memory_video_generation_low_confidence():
    e = est({"model": {"name": "THUDM/CogVideoX-2b"}, "media": {"task": "generation"}})
    m = e["memory"]
    assert m["method"] == "inference" and m["confidence"] == "low"
    weights = (1.693783872 + 4.762310656 + 0.215583907) * 1e9 * 2
    dit = 2 * (17550 + 226) * (6 * 1920 + 2 * 7680) * 2       # CFG batch 2 x tokens x (6h + 2 FFN) x 2 B
    lat = 16 * 13 * 60 * 90 * 4 * 3
    vae = 8 * 720 * 480 * 128 * 2 * 3
    c = comps(e)
    close(c["weights"], round(weights / GB, 2), tol=1e-3)
    close(c["activations"], round((dit + lat + vae) / GB, 2), tol=1e-3)
    other = weights + dit + lat + vae
    close(m["min_total_gb"], round((other + overhead(other)) / GB, 2), tol=1e-3)
    # the publisher's own peak (SAT, FP16): 18 GB; the estimate is in the same range and states its uncertainty
    assert 15 <= m["min_total_gb"] <= 25 and m["range_gb"][0] < m["min_total_gb"] < m["range_gb"][1]
    assert any("ROUGHLY MODELLED" in f for f in e["media"]["formulas"])
    e = est({"model": {"name": "tencent/HunyuanVideo"}, "media": {"task": "generation"}})
    assert e["memory"]["per_gpu_by_count"][1]["recommended_gb"] > 48
    assert "only GPUs with at least 80 GB" in e["media"]["headline"], e["media"]["headline"]
    assert MD.smallest_vram_fitting(60) == 80 and MD.smallest_vram_fitting(10) is not None


def test_media_without_a_media_model():
    e = est({"media": {"modality": "audio", "total_audio_hours": 100}})
    assert e["memory"]["method"] == "unavailable" and any(q["field"] == "model.name" for q in e["questions"])
    assert "name an audio" in e["media"]["notes"][0]
    e = est({"workload_type": "inference", "model": {"name": "meta-llama/Llama-3.1-8B"},
             "media": {"images_per_request": 2}})
    assert "do not change its memory estimate" in e["media"]["notes"][0]
    base = est({"workload_type": "inference", "model": {"name": "meta-llama/Llama-3.1-8B"}})
    assert e["memory"]["recommended_gb"] == base["memory"]["recommended_gb"]


def test_questions():
    e = est({"workload_type": "inference", "model": {"name": "Qwen/Qwen2-VL-7B-Instruct"}})
    f = [q["field"] for q in e["questions"]]
    assert "media.images_per_request" in f and "media.image_resolution" in f, f
    e = est({"workload_type": "batch", "model": {"name": "openai/whisper-large-v3"}, "media": {"audio_seconds": 60}})
    assert "media.total_audio_hours" in [q["field"] for q in e["questions"]]
    e = est({"model": {"name": "Wan-AI/Wan2.1-T2V-1.3B"}, "media": {"task": "generation"}})
    assert "media.output_resolution" in [q["field"] for q in e["questions"]]


# ------------------------------------------------------------------ runtime (theoretical)

def test_runtime_whisper_hand_computed():
    e = est({"workload_type": "batch", "model": {"name": "openai/whisper-large-v3"}, "precision": "bf16",
             "inference": {"concurrency": 16}, "media": {"total_audio_hours": 10000}})
    prof = e["memory"]["profile"]
    spec = hardware.spec(H100)
    r1 = MD.option_runtime(spec, 1, 1, prof, "single_gpu")
    P_enc = 12 * 32 * 1280 ** 2
    P_dec = 1.55e9 - P_enc
    peak, bw = 989.5e12 * 0.35, 3.35e12 * 0.60
    compute = 2 * P_enc * 1500 + 4 * 32 * 1500 ** 2 * 1280 + 2 * P_dec * 100
    step_bytes = P_dec * 2 + 16 * (2 * 32 * 1280 * 1500 * 2 + 2 * 32 * 1280 * 50 * 2)
    step = max(step_bytes / bw, 2 * P_dec * 16 / peak)
    per_window = (compute / peak + 100 * step / 16) / 0.10            # / pipeline efficiency
    close(r1["seconds_per_item"]["central"], round(per_window, 4), tol=1e-3)
    close(r1["hours"], round(1_200_000 * per_window / 3600, 4), tol=1e-3)
    assert r1["known"] and r1["range_hours"][0] < r1["hours"] < r1["range_hours"][1]
    assert r1["throughput"]["unit"] == "audio hours per GPU-hour"
    close(r1["throughput"]["value"], round(3600 / per_window * 30 / 3600, 4), tol=1e-3)
    r2 = MD.option_runtime(spec, 2, 1, prof, "nvlink")
    assert r2["replicas"] == 2 and "independent replica" in r2["parallelism"]
    close(r2["hours"], round(r1["hours"] / (2 * MD.REPLICA_EFF[0]), 4), tol=1e-3)
    assert any("pipeline efficiency" in a for a in r1["assumptions"])


def test_runtime_publisher_times_within_range():
    """Every publisher-reported generation time on record lies inside the theoretical range (never claimed exact)."""
    cases = [("THUDM/CogVideoX-2b", {}, {A100: 90, H100: 45}), ("THUDM/CogVideoX-5b", {}, {A100: 180, H100: 90}),
             ("Wan-AI/Wan2.1-T2V-14B", {"output_resolution": "1280x720", "output_video_seconds": 5},
              {A100: 3342.6, H100: 1837.9})]
    for name, media, times in cases:
        e = est({"model": {"name": name}, "media": {"task": "generation", **media}})
        prof = e["memory"]["profile"]
        for gpu, secs in times.items():
            r = MD.option_runtime(hardware.spec(gpu), 1, 1, prof, "single_gpu")
            fast, slow = r["seconds_per_item"]["range"]
            assert fast <= secs <= slow, (name, gpu, secs, fast, slow)
            assert r["throughput"]["unit"] == "videos per GPU-hour" and "video_seconds_per_gpu_hour" in r["throughput"]
    # FLOPs per CogVideoX-2b video, by hand: 50 steps x 2 passes x (2 x P x T + 4 x L x T^2 x h), T = 17550 + 226
    T = 17550 + 226
    flops = 50 * 2 * (2 * 1.693783872e9 * T + 4 * 30 * T * T * 1920)
    prof = est({"model": {"name": "THUDM/CogVideoX-2b"}, "media": {"task": "generation"}})["memory"]["profile"]
    assert abs(prof["media"]["work"]["compute_flops"][0] - flops) / flops < 1e-9


# ------------------------------------------------------------------ assistant intents

def test_intents():
    from assistant import intents

    cases = {
        "transcribe 10k hours of audio": ("recommend", {"modality": ["audio"], "task": "transcription",
                                                         "total_audio_hours": 10000.0}, "batch"),
        "caption images with LLaVA": ("recommend", {"modality": ["image"], "task": "captioning"}, "inference"),
        "generate 5-second videos": ("recommend", {"modality": ["video"], "task": "generation",
                                                   "output_video_seconds": 5.0}, "inference"),
        "generate 100 5-second 720p videos with Wan2.1 14B": ("recommend", {
            "modality": ["video"], "task": "generation", "output_video_seconds": 5.0, "total_items": 100,
            "output_resolution": "1280x720"}, "inference"),
        "caption 50,000 product photos with Qwen2-VL 7B at 1024x768": ("recommend", {
            "modality": ["image"], "task": "captioning", "total_items": 50000, "image_resolution": "1024x768"},
            "batch"),
        "summarize 60-second videos with qwen2-vl": ("recommend", {"modality": ["video"], "task": "understanding",
                                                                   "video_seconds": 60.0}, "inference"),
    }
    for msg, (intent, media, wt) in cases.items():
        p = intents.parse(msg)
        assert p.intent == intent and p.slots.get("media") == media and p.slots.get("workload_type") == wt, \
            (msg, p.intent, p.slots)
    p = intents.parse("Which GPU for Qwen2-VL 7B with 2 images per request?")
    assert p.slots["model"] == {"name": "Qwen2-VL 7B"}, "a VLM's name size is the LLM's: the catalog total is used"
    assert p.slots["media"]["images_per_request"] == 2
    # unchanged asks stay unchanged
    for msg, want in (("Cheapest available H100", {}), ("Compare H100 SXM and H200", {}),
                      ("What GPU should I use to fine-tune Llama 3 8B with LoRA?",
                       {"workload_type": "fine_tuning", "training": {"method": "lora"},
                        "model": {"name": "Llama 3 8B", "parameters_b": 8.0}})):
        assert intents.parse(msg).slots == want, (msg, intents.parse(msg).slots)
    W.model_validate(intents.parse("generate 100 5-second 720p videos with Wan2.1 14B").slots)


# ------------------------------------------------------------------ contract mirror (server = CLI = /find)

def _props(model) -> dict:
    sch = model.model_json_schema()
    out = {"WorkloadRequirements": set(sch["properties"])}
    for name, d in sch.get("$defs", {}).items():
        out[name] = set(d.get("properties", {}))
    return out


def test_contract_mirror():
    from opengrid_cli import workload as cli_w

    from intelligence import requirements as srv

    s, c = _props(srv.WorkloadRequirements), _props(cli_w.WorkloadRequirements)
    assert s == c, {k: (s.get(k, set()) ^ c.get(k, set())) for k in set(s) | set(c) if s.get(k) != c.get(k)}
    ok = {"media": {"modality": ["image", "video"], "task": "understanding", "images_per_request": 2,
                    "image_resolution": "1024x768", "video_seconds": 30, "fps": 2, "total_items": 10}}
    srv.WorkloadRequirements.model_validate(ok)
    assert cli_w.validate(ok)["media"]["image_resolution"] == "1024x768"
    for bad in ({"media": {"images_per_request": 0}}, {"media": {"image_resolution": "big"}},
                {"media": {"oops": 1}}):
        for model in (srv.WorkloadRequirements, cli_w.WorkloadRequirements):
            try:
                model.model_validate(bad)
            except Exception:  # noqa: BLE001
                continue
            raise AssertionError(f"{model.__module__} accepted {bad}")
    js = (ROOT / "web" / "pages" / "find.js").read_text()
    block = re.search(r"media: O\(\{(.*?)\}\),\n", js, re.S).group(1)
    js_fields = set(re.findall(r"(\w+):", block))
    assert js_fields == s["Media"], s["Media"] ^ js_fields


# ------------------------------------------------------------------ database

def L(prov, gpu, price, *, count=1, available=True, vis="public", lid=None):
    return ComputeListingRow(
        provider=prov, listing_id=lid or f"{prov}:{gpu}:{count}", sku=f"{prov}-sku", raw_gpu_name=gpu,
        canonical_gpu_name=gpu, gpu_count=count, region="us-east", country="US",
        price_per_gpu_hour=Decimal(str(price)), price_per_instance_hour=Decimal(str(round(price * count, 6))),
        currency="USD", market_type="on_demand", provider_tier=None, interruptible=False, available=available,
        capacity=None, capacity_unit=None, vcpu=None, ram_gb=None, storage_gb=None, visibility=vis,
        observed_at=NOW - timedelta(minutes=1), first_seen_at=NOW - timedelta(days=3))


INTERNAL_ID = "syn_a:internal-h100-pool:1"


def seed(Session):
    with Session.begin() as s:
        s.add_all([L("syn_a", T4, 0.30), L("syn_b", L4, 0.50), L("syn_b", L40S, 0.90), L("syn_a", A100, 1.50),
                   L("syn_a", H100, 2.50), L("syn_a", H100, 2.45, count=2),
                   L("syn_a", H100, 1.99, vis="internal", lid=INTERNAL_ID)])


WHISPER_10K = {"workload_type": "batch", "model": {"name": "openai/whisper-large-v3"}, "precision": "bf16",
               "media": {"modality": "audio", "task": "transcription", "total_audio_hours": 10000}}


def test_db_whisper_10k_hours():
    out = recommend(W.model_validate(WHISPER_10K), PUBLIC, now=NOW, limit=20)
    assert out["options"], out["notes"]
    for o in out["options"]:
        p = o["performance"]
        assert p["media"]["throughput"]["unit"] == "audio hours per GPU-hour" and p["media"]["throughput"]["value"] > 0
        assert p["runtime"]["known"] and p["runtime"]["kind"] == "estimated"
        assert o["cost"]["estimated_total"]["runtime_kind"] == "theoretical"
        assert "inference" not in p, "no LLM token-decode roofline for Whisper"
        rt = o["decision"]["confidence"]["components"]["runtime"]
        assert rt["level"] == "low" and rt["evidence"] == "estimated"
    assert out["picks"]["lowest_estimated_job_cost"] and out["picks"]["shortest_estimated_runtime"]
    notes = " ".join(out["notes"])
    assert "independent" in notes and "batch fan-out" in notes and "Media: Whisper" in notes
    two = [o for o in out["options"] if o["gpu"] == H100 and o["gpu_count"] == 2]
    assert two and two[0]["performance"]["parallelism"]["strategy"] == "replicas", [
        (o["gpu"], o["gpu_count"]) for o in out["options"]]
    assert "independent items" in two[0]["performance"]["scaling"]["basis"]
    one = next(o for o in out["options"] if o["gpu"] == H100 and o["gpu_count"] == 1)
    close(two[0]["performance"]["runtime"]["hours"], one["performance"]["runtime"]["hours"] / (2 * MD.REPLICA_EFF[0]),
          tol=1e-3)
    assert out["multi_gpu"]["independent_items"]["items"] == 1_200_000
    assert out["estimate"]["media"]["work"]["items"] == 1_200_000
    best = next(o for o in out["options"] if o["option_id"] == out["picks"]["best_for_workload"]["option_id"])
    assert any("Whisper" in w for w in best["why"]), best["why"]


def test_db_video_generation_needs_80gb():
    out = recommend(W.model_validate({"model": {"name": "tencent/HunyuanVideo"},
                                      "media": {"task": "generation", "total_items": 20}}), PUBLIC, now=NOW)
    gpus = {o["gpu"] for o in out["options"]}
    assert gpus and gpus <= {A100, H100}, gpus
    small = {e["gpu"] for e in out["excluded"] if e["code"] == "insufficient_vram"}
    assert {T4, L4, L40S} <= small, small
    best = next(o for o in out["options"] if o["option_id"] == out["picks"]["best_for_workload"]["option_id"])
    assert any("only GPUs with at least 80 GB" in w for w in best["why"]), best["why"]
    for o in out["options"]:
        p = o["performance"]
        assert p["basis"]["metric"] == "tflops", "video diffusion is compute bound"
        assert p["media"]["throughput"]["unit"] == "videos per GPU-hour" and p["runtime"]["known"]
        assert o["decision"]["confidence"]["level"] == "low"


def test_db_vlm_images():
    body = {"workload_type": "inference", "model": {"name": "llava-hf/llava-1.5-7b-hf"}, "precision": "bf16",
            "inference": {"concurrency": 8, "context_len": 2048}, "media": {"images_per_request": 2}}
    out = recommend(W.model_validate(body), PUBLIC, now=NOW)
    gpus = {o["gpu"] for o in out["options"]}
    assert gpus and not gpus & {T4, L4}, gpus            # 36.5 GB with margin
    assert out["picks"]["lowest_estimated_job_cost"] is None
    assert "total_items" in out["picks_unavailable"]["lowest_estimated_job_cost"]
    assert all(o["performance"]["media"]["throughput"]["unit"] == "requests per GPU-hour" for o in out["options"])
    body["media"]["total_items"] = 100_000
    out = recommend(W.model_validate(body), PUBLIC, now=NOW)
    assert out["picks"]["lowest_estimated_job_cost"] is not None
    assert all(o["performance"]["runtime"]["known"] for o in out["options"])


def test_db_old_payloads_have_no_media_blocks():
    for body in ({"workload_type": "inference", "model": {"name": "meta-llama/Llama-3.1-8B"},
                  "inference": {"context_len": 8192, "concurrency": 4}},
                 {"workload_type": "fine_tuning", "model": {"name": "meta-llama/Llama-3.1-8B"}, "precision": "bf16",
                  "training": {"method": "lora", "micro_batch_size": 1, "seq_len": 2048, "dataset_tokens": 2e8}}):
        out = recommend(W.model_validate(body), OPERATOR, now=NOW)
        s = json.dumps(out, default=str)
        assert '"media"' not in s and "Media:" not in s and "independent_items" not in s, body


def test_db_visibility():
    anon = recommend(W.model_validate(WHISPER_10K), PUBLIC, now=NOW, limit=20)
    assert all(o["visibility"] == "public" for o in anon["options"]) and INTERNAL_ID not in json.dumps(anon)
    op = recommend(W.model_validate(WHISPER_10K), OPERATOR, now=NOW, limit=20)
    assert any(o["listing_id"] == INTERNAL_ID and o["visibility"] == "internal" for o in op["options"])
    key = Principal(kind="api_key", account_id=77, key_id=5, scopes=frozenset({"data:read"}))

    def shown(price):
        with normalize.SessionLocal.begin() as s:
            s.query(ComputeListingRow).filter(ComputeListingRow.listing_id == INTERNAL_ID).update(
                {"price_per_gpu_hour": Decimal(str(price)), "price_per_instance_hour": Decimal(str(price))})
        scoring.history.cache_clear()
        out = disclosure.redact(recommend(W.model_validate(WHISPER_10K), key, now=NOW, limit=20), key)
        out.pop("as_of", None)
        return json.dumps(out, sort_keys=True, default=str)

    a, b = shown(1.99), shown(2.01)
    assert a == b, "a non-admin answer must not change with an internal price"
    assert "1.99" not in a and "2.01" not in a
    io = next(o for o in json.loads(a)["options"] if o.get("listing_id") == INTERNAL_ID)
    assert "performance" not in io and io["price_basis"] in ("public_listing", "quote")


def test_db_assistant_transcription_ask():
    from assistant import engine

    r = engine.respond("Transcribe 10,000 hours of audio with Whisper large-v3", None, PUBLIC, provider=None)
    reply = r["reply"]
    assert "audio hours per GPU-hour" in reply and "theoretical" in reply, reply
    rec = next(c["data"] for c in r["cards"] if c["type"] == "recommendation")
    assert rec["estimate"]["media"]["media_kind"] == "asr" and rec["tier"] == "public"
    r = engine.respond("transcribe 10k hours of audio", None, PUBLIC, provider=None)
    q = next(c["data"] for c in r["cards"] if c["type"] == "questions")
    assert q[0]["field"] == "model.name" and "openai/whisper-large-v3" in [o["message"] for o in q[0]["options"]]


def test_db_measured_keeps_precedence():
    """A consented measurement of the same workload from a real OpenGrid job beats the theoretical runtime."""
    from store.intelligence import PerformanceObservation as PO

    sig = "model:openai/whisper-large-v3|inference|bf16"
    with normalize.SessionLocal.begin() as s:
        s.add(PO(gpu=L4, gpu_count=1, workload_class="inference", model_name="openai/whisper-large-v3",
                 method="inference", precision="bf16", source_kind="completed_job", consent=True, job_id="job-media1",
                 created_at=NOW, observed_at=NOW, provider="syn_b", workload_signature=sig, throughput=2.0,
                 throughput_unit="samples_per_second", data_kind="measured"))
    body = {"workload_type": "batch", "model": {"name": "openai/whisper-large-v3"}, "precision": "bf16",
            "media": {"audio_seconds": 600, "total_items": 3600}}
    out = recommend(W.model_validate(body), PUBLIC, now=NOW, limit=20)
    l4 = next(o for o in out["options"] if o["gpu"] == L4 and o["gpu_count"] == 1)
    assert l4["performance"]["measured"]["kind"] == "measured"
    assert l4["cost"]["estimated_total"]["runtime_kind"] == "measured"
    close(l4["performance"]["measured"]["runtime"]["hours"], 3600 / 2.0 / 3600, tol=1e-3)
    assert l4["performance"]["runtime"]["kind"] == "estimated", "the theoretical figure stays alongside, labelled"
    others = [o for o in out["options"] if o["gpu"] != L4]
    assert all(o["cost"]["estimated_total"]["runtime_kind"] == "theoretical" for o in others)


PURE = [test_requirements_validation, test_empty_media_is_no_media, test_backward_compat_golden,
        test_catalog_sourced_numbers, test_token_rules_hand_computed, test_memory_whisper_batch,
        test_memory_vlm_image_tokens, test_memory_quantized_vlm_keeps_vision_16bit,
        test_memory_audio_encoder_tts_whisper_training, test_memory_video_generation_low_confidence,
        test_media_without_a_media_model, test_questions, test_runtime_whisper_hand_computed,
        test_runtime_publisher_times_within_range, test_intents, test_contract_mirror]
DBT = [test_db_whisper_10k_hours, test_db_video_generation_needs_80gb, test_db_vlm_images,
       test_db_old_payloads_have_no_media_blocks, test_db_visibility, test_db_assistant_transcription_ask,
       test_db_measured_keeps_precedence]


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
