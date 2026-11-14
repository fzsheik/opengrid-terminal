"""Audio, image + text, and video workloads: what the estimator adds for them. Pure: no database, no provider call.

    resolve(req, p) -> dict | None       the media inputs (request, catalog, or a stated assumption); memory.profile
    adjust(p, scenario, out, notes)      component bytes for media kinds; memory.components calls it
    work(req, p) -> dict                 the GPU work per item and the number of items (for a theoretical runtime)
    option_runtime(spec, n, n_min, prof, link_kind) -> dict | None   one option's theoretical runtime + throughput
    summary(p) -> dict                   the estimate's `media` block
    smart_resize / image_tokens / video_tokens / video_latent / whisper_windows      the token rules, exposed for tests

Formulas (every result is ESTIMATED; methodology/recommendations.md "Audio, image and video workloads"):
    VLM            KV cache over text + image (+ video) tokens: tokens per image by the model's documented rule
                   (fixed 576 for LLaVA-1.5; AnyRes tiles for LLaVA-1.6; one token per 28 x 28 pixels for Qwen2-VL;
                   1,601 per 560 x 560 tile into CROSS-attention for Llama 3.2 Vision); the vision tower stays 16-bit
                   when the LLM is quantized; one image's vision-encoder layer as transient activations
    Whisper        30 s windows; per window the decoder keeps a cross-attention cache over the 1500 encoder positions
                   (2 x decoder layers x d_model x 1500 x 2 B) and a self-attention cache of up to 448 tokens
    wav2vec2       50 frames per second of audio; the transformer activation formula over those frames
    TTS            Parler-TTS: KV cache over 86 codec frames per second of audio (+ the text prompt); Kokoro: weights
    video gen      weights (transformer + text encoder + VAE) + per-block activations over the latent video tokens
                   + the latent tensors + the VAE decode; activation and decode memory are only roughly modelled,
                   so the confidence is capped at low
    runtime        theoretical: compute at an assumed model-FLOPs utilization, plus memory-bandwidth-bound
                   autoregressive decode, divided by an assumed pipeline efficiency for audio pipelines; independent
                   items scale across GPUs as separate replicas. Never a benchmark: a consented measurement of the same
                   workload from a real OpenGrid job (intelligence/measured.py) takes precedence in the recommender.
"""

from __future__ import annotations

import math

from intelligence import memory as M
from intelligence import performance as P
from intelligence.models_catalog import MEDIA_KINDS
from intelligence.requirements import media_given, parse_resolution

GB = M.GB
SOURCES = {
    "whisper": "https://arxiv.org/abs/2212.04356",
    "wav2vec2": "https://arxiv.org/abs/2006.11477",
    "llava15": "https://arxiv.org/abs/2310.03744",
    "llava_next": "https://llava-vl.github.io/blog/2024-01-30-llava-next/",
    "qwen2vl": "https://arxiv.org/abs/2409.12191",
    "qwen_vl_utils": "https://pypi.org/project/qwen-vl-utils/0.0.8/",
    "llama32_vision": "https://huggingface.co/unsloth/Llama-3.2-11B-Vision-Instruct/raw/main/config.json",
    "paligemma": "https://arxiv.org/abs/2407.07726",
    "parler_codec": "https://huggingface.co/parler-tts/dac_44khZ_8kbps/raw/main/config.json",
    "cogvideox": "https://huggingface.co/THUDM/CogVideoX-5b",
    "wan": "https://github.com/Wan-Video/Wan2.1",
    "hunyuanvideo": "https://github.com/Tencent/HunyuanVideo",
    "activations": "https://arxiv.org/abs/2205.05198",
    "training_flops": "https://arxiv.org/abs/2001.08361",
}

WINDOW_S = 30                       # Whisper: 30-second windows (paper section 2.2)
ENC_POSITIONS = 1500                # 3000 mel frames, halved by the stride-2 conv stem
# OpenGrid planning assumptions (stated in every output), not measurements:
WHISPER_TOKENS = (100, 50, 150)     # decoder tokens per 30 s window (central, low, high): speech at ~100-300 words/min
BATCH = (16, 4, 32)                 # batch jobs: windows / clips / requests processed together on one GPU
INTERACTIVE = (1, 1, 8)
MFU = P.MFU_LARGE                   # (central, low, high) model-FLOPs utilization for compute-bound work
# Video diffusion transformers run large, dense matmuls and attention over tens of thousands of tokens. The range is
# chosen to contain the utilization implied by every publisher-reported generation time on record (CogVideoX-2b /
# 5b on A100 and H100, Wan2.1-T2V-14B 720p on A100 and H100: 30% to 66%, from the model cards / README and the FLOP
# formula below). Publisher figures, not OpenGrid measurements.
DIFFUSION_MFU = (0.45, 0.25, 0.70)
BW_UTIL = (0.60, 0.40, 0.80)        # achieved share of peak memory bandwidth in memory-bound decode
PIPELINE_EFF = (0.10, 0.04, 0.30)   # audio pipelines: share of that GPU roofline end to end (decode loops are
#                                     latency-bound on kernel launches; audio decoding and feature extraction run on
#                                     the CPU). Not calibrated against a measurement: low confidence.
REPLICA_EFF = (0.98, 0.90, 1.00)    # independent replicas, one per GPU: shard imbalance and stragglers only
VLM_IMAGES = (1, 1, 4)
VLM_RES = ("1024x1024", "512x512", "2048x2048")
VLM_PROMPT = (64, 16, 256)          # text prompt tokens around the images
VLM_OUTPUT = (128, 32, 512)         # generated tokens per request (a caption, an answer)
VIDEO_SECONDS = (30, 10, 120)
VIDEO_RES = "1280x720"
AUDIO_SECONDS = {"audio_encoder": (30, 10, 60), "tts": (10, 5, 30)}
TTS_PROMPT = 64                     # text tokens prefixed to the Parler-TTS decoder
VAE_CHUNK = 8                       # output frames decoded together by the video VAE (roughly modelled)
VIDEO_FPS_FALLBACK = 24             # when the model card states no frame rate


def _ix(scenario: int) -> int:
    """memory scenarios are 0 low / 1 central / 2 high; knobs here are (central, low, high)."""
    return {0: 1, 1: 0, 2: 2}[scenario]


# --------------------------------------------------------------------------------------------- token rules (pure)

def whisper_windows(seconds: float) -> int:
    return max(1, math.ceil(seconds / WINDOW_S))


def smart_resize(height: int, width: int, *, factor: int = 28, min_pixels: int = 3136,
                 max_pixels: int = 12845056) -> tuple[int, int]:
    """qwen_vl_utils smart_resize: sides rounded to multiples of `factor`, the area kept within [min, max] pixels."""
    hb = max(factor, round(height / factor) * factor)
    wb = max(factor, round(width / factor) * factor)
    if hb * wb > max_pixels:
        beta = math.sqrt(height * width / max_pixels)
        hb = max(factor, math.floor(height / beta / factor) * factor)
        wb = max(factor, math.floor(width / beta / factor) * factor)
    elif hb * wb < min_pixels:
        beta = math.sqrt(min_pixels / (height * width))
        hb = math.ceil(height * beta / factor) * factor
        wb = math.ceil(width * beta / factor) * factor
    return hb, wb


def _best_pinpoint(w: int, h: int, pins) -> tuple[int, int]:
    """LLaVA-NeXT select_best_resolution: the grid that keeps the most of the image's pixels, then wastes the least."""
    best, best_eff, best_waste = None, -1, None
    for ph, pw in pins:
        s = min(pw / w, ph / h)
        eff = min(int(w * s) * int(h * s), w * h)
        waste = pw * ph - eff
        if eff > best_eff or (eff == best_eff and waste < best_waste):
            best, best_eff, best_waste = (pw, ph), eff, waste
    return best


def image_tokens(cm: dict, w: int | None, h: int | None) -> dict:
    """Tokens one image adds, by the model's documented rule. `w`/`h` None: the rule's fixed count when it has one."""
    rule = cm.get("rule")
    if rule == "fixed":
        t = cm["tokens_per_image"]
        return {"tokens": t, "patches": t, "rule": f"fixed {t} tokens per image (resized to {cm['image_size']} x "
                                                   f"{cm['image_size']})"}
    if w is None or h is None:
        return {"tokens": None, "patches": None, "rule": "resolution needed"}
    if rule == "anyres":
        pw, ph = _best_pinpoint(w, h, cm["grid_pinpoints"])
        tiles = (pw // cm["tile_size"]) * (ph // cm["tile_size"])
        t = cm["tokens_per_tile"] * (1 + tiles)
        return {"tokens": t, "patches": t, "tiles": tiles, "grid": f"{pw}x{ph}",
                "rule": f"AnyRes: {w}x{h} -> grid {pw}x{ph} = {tiles} tiles of {cm['tile_size']} + 1 base image, "
                        f"{cm['tokens_per_tile']} tokens each (upper bound: unpadding removes padded rows)"}
    if rule == "dynamic":
        hb, wb = smart_resize(h, w, min_pixels=cm["min_pixels"], max_pixels=cm["max_pixels"])
        t = hb * wb // cm["pixels_per_token"]
        return {"tokens": t, "patches": t * cm["merge"] ** 2, "resized": f"{wb}x{hb}",
                "rule": f"{w}x{h} resized to {wb}x{hb} (multiples of 28, within {cm['min_pixels']:,}-"
                        f"{cm['max_pixels']:,} pixels): one token per 28 x 28 pixels = {t:,}"}
    if rule == "tiles":
        tiles = min(cm["max_tiles"], math.ceil(w / cm["image_size"]) * math.ceil(h / cm["image_size"]))
        t = tiles * cm["tokens_per_tile"]
        return {"tokens": t, "patches": t, "tiles": tiles,
                "rule": f"{w}x{h} -> {tiles} tile(s) of {cm['image_size']} x {cm['image_size']} (at most "
                        f"{cm['max_tiles']}), {cm['tokens_per_tile']:,} vision tokens per tile, cross-attended"}
    return {"tokens": None, "patches": None, "rule": "no token rule on record for this model"}


def video_tokens(cm: dict, seconds: float, fps: float, w: int, h: int) -> dict:
    """Qwen2-VL video input (qwen_vl_utils 0.0.8): frames sampled at `fps`, an even count within 4-768; each frame
    resized within 128-768 x 28 x 28 pixels; two consecutive frames share one token grid (temporal patch 2); the whole
    video kept within 24,576 x 28 x 28 pixels' worth of tokens."""
    ff = cm.get("frame_factor", 2)
    frames = max(4, min(cm.get("max_frames", 768), round(seconds * fps / ff) * ff))
    hb, wb = smart_resize(h, w, min_pixels=cm["video_min_pixels"], max_pixels=cm["video_max_pixels"])
    per_pair = hb * wb // cm["pixels_per_token"]
    tokens = frames // cm["temporal_patch"] * per_pair
    cap = cm["video_total_pixels"] // cm["pixels_per_token"]
    capped = tokens > cap
    return {"frames": frames, "frame_resized": f"{wb}x{hb}", "tokens_per_frame_pair": per_pair,
            "tokens": min(tokens, cap), "capped": capped,
            "rule": f"{seconds:g} s x {fps:g} frames/s = {frames} frames, each resized to {wb}x{hb}; 2 frames per "
                    f"{per_pair:,}-token grid = {tokens:,} tokens" + (f", capped at the {cap:,}-token video budget"
                                                                       if capped else "")}


def video_latent(cm: dict, seconds: float | None, fps: float | None, w: int, h: int) -> dict:
    """Latent video tokens of a diffusion transformer: frames aligned to 4k + 1, VAE 4x in time and 8x in space,
    2 x 2 patches: tokens = latent frames x (H / 16) x (W / 16)."""
    native_fps = cm.get("native_fps")
    fps_eff = fps or native_fps or VIDEO_FPS_FALLBACK
    if seconds is None:
        frames = cm["native_frames"]
    else:
        frames = cm["vae_temporal"] * max(1, round(seconds * fps_eff / cm["vae_temporal"])) + 1
    lat_f = (frames - 1) // cm["vae_temporal"] + 1
    unit = cm["vae_spatial"] * cm["patch"]
    gh, gw = h // unit, w // unit
    tokens = lat_f * gh * gw
    return {"frames": frames, "fps": fps_eff, "seconds": round((frames - 1) / fps_eff, 3), "latent_frames": lat_f,
            "latent_hw": [h // cm["vae_spatial"], w // cm["vae_spatial"]], "tokens": tokens,
            "rule": f"{frames} frames -> {lat_f} latent frames (VAE 4x in time); {w}x{h} -> {gw} x {gh} patches (VAE "
                    f"8x in space, 2 x 2 patches) = {tokens:,} video tokens"}


# --------------------------------------------------------------------------------------------- resolve (profile)

def _g(mr, k):
    return None if mr is None else getattr(mr, k, None)


def _batch(md, req, p, A, known, batchy: bool):
    cc = _g(req.inference, "concurrency")
    if cc:
        b = (cc, cc, cc)
    elif batchy:
        b = BATCH
        A.append(f"batch size not stated: {BATCH[0]} items processed together per GPU assumed (range {BATCH[1]}-"
                 f"{BATCH[2]}; inference.concurrency sets it)")
        known["media.batch"] = "assumed"
    else:
        b = INTERACTIVE
        A.append("concurrency not stated: 1 item at a time assumed (range 1-8)")
        known["media.batch"] = "assumed"
    md["batch"] = list(b)
    return b


def resolve(req, p) -> dict | None:
    """The media block of a profile: None for a request without media fields on a non-media model (nothing changes)."""
    kind = p["kind"]
    entry = p.get("catalog") or {}
    cm = entry.get("media") or {}
    mr = req.media if media_given(req) else None
    if kind not in MEDIA_KINDS and mr is None:
        return None
    A, known = p["assumptions"], p["known"]
    req_mod = list(_g(mr, "modality") or [])
    modality = req_mod or list(entry.get("modality") or [])
    md = {"kind": kind, "modality": modality, "task": _g(mr, "task") or entry.get("task"), "model": entry.get("id"),
          "inputs": {}, "formulas": [], "notes": [], "confidence_cap": None, "publisher": cm.get("publisher")}
    if kind not in MEDIA_KINDS:
        if entry or p["model"].get("name"):
            md["notes"].append("the model is not an audio, image + text or video model in OpenGrid's catalog: the "
                               "media fields are recorded but do not change its memory estimate")
        else:
            md["notes"].append("no model named: name an audio, image + text or video model (e.g. a Whisper, LLaVA, "
                               "Qwen2-VL or CogVideoX checkpoint) to size these media inputs")
        return md
    batchy = req.workload_type == "batch" or bool(_g(mr, "total_audio_hours") or _g(mr, "total_items"))
    md["batch_job"] = batchy
    if not cm:
        md["notes"].append(f"architecture '{kind}' recognised but not in OpenGrid's catalog: the media formulas need "
                           "its published dimensions (name a catalog model): the estimate is a lower bound")
        md["confidence_cap"] = "low"
        md["lower_bound"] = True
        return md
    fn = {"vlm": _resolve_vlm, "asr": _resolve_asr, "audio_encoder": _resolve_audio_encoder, "tts": _resolve_tts,
          "video_diffusion": _resolve_video_gen}[kind]
    fn(md, req, p, mr, cm, A, known, batchy)
    return md


def _res_or(mr, field):
    r = parse_resolution(_g(mr, field))
    return r


def _resolve_vlm(md, req, p, mr, cm, A, known, batchy):
    req_mod = list(_g(mr, "modality") or [])
    video_req = _g(mr, "video_seconds") is not None or "video" in req_mod
    image_req = _g(mr, "images_per_request") is not None or "image" in req_mod or "multimodal" in req_mod \
        or _g(mr, "image_resolution") is not None or not video_req
    n_img = _g(mr, "images_per_request")
    if n_img is not None:
        imgs = (n_img, n_img, n_img)
    elif not image_req:
        imgs = (0, 0, 0)
    else:
        imgs = VLM_IMAGES
        A.append(f"images per request not stated: {VLM_IMAGES[0]} assumed (range {VLM_IMAGES[1]}-{VLM_IMAGES[2]})")
        known["media.images_per_request"] = "assumed"
    res = _res_or(mr, "image_resolution")
    per = []
    if cm["rule"] == "fixed":
        one = image_tokens(cm, None, None)
        per = [one, one, one]
        if res:
            A.append(f"{p['catalog']['id']} resizes every image to {cm['image_size']} x {cm['image_size']}: "
                     f"{one['tokens']} tokens per image whatever the input resolution")
    elif res:
        one = image_tokens(cm, *res)
        per = [one, one, one]
    elif imgs[0] or imgs[2]:
        per = [image_tokens(cm, *parse_resolution(r)) for r in VLM_RES]
        A.append(f"image resolution not stated: {VLM_RES[0]} assumed (range {VLM_RES[1]} to {VLM_RES[2]}); "
                 f"tokens per image follow the model's rule ({per[0]['tokens']:,}, range {per[1]['tokens']:,}-"
                 f"{per[2]['tokens']:,})")
        known["media.image_resolution"] = "assumed"
    tok_img = [((per[i]["tokens"] or 0) * imgs[i]) if per else 0 for i in range(3)]
    vid = None
    if video_req:
        if cm["rule"] != "dynamic":
            md["notes"].append(f"{p['catalog']['id']} takes images, not video: video input is not counted (send "
                               "sampled frames as images, or use a video-capable model such as Qwen2-VL)")
        else:
            sec = _g(mr, "video_seconds")
            if sec is None:
                secs = VIDEO_SECONDS
                A.append(f"video length not stated: {VIDEO_SECONDS[0]} s assumed (range {VIDEO_SECONDS[1]}-"
                         f"{VIDEO_SECONDS[2]} s)")
                known["media.video_seconds"] = "assumed"
            else:
                secs = (sec, sec, sec)
            fps = _g(mr, "fps") or cm["video_fps"]
            if _g(mr, "fps") is None:
                A.append(f"video frame sampling not stated: {cm['video_fps']:g} frames/s (the model's default)")
            vres = _res_or(mr, "video_resolution")
            if vres is None:
                vres = parse_resolution(VIDEO_RES)
                A.append(f"video resolution not stated: {VIDEO_RES} assumed (frames are resized within the model's "
                         "per-frame pixel budget, so larger inputs add no tokens)")
            vid = [video_tokens(cm, secs[i], fps, *vres) for i in range(3)]
    tok_vid = [v["tokens"] for v in vid] if vid else [0, 0, 0]
    extra = tuple(tok_img[i] + tok_vid[i] for i in range(3))
    md["inputs"] = {"images_per_request": list(imgs), "tokens_per_image": [x["tokens"] for x in per] if per else None,
                    "image_tokens_per_request": tok_img, "video": vid[0] if vid else None,
                    "video_tokens_per_request": tok_vid, "media_tokens_per_request": list(extra),
                    "vision_encoder": cm.get("encoder"), "token_rule": per[0]["rule"] if per else None}
    md["extra_tokens"] = extra
    md["formulas"].append("image tokens per request = images x tokens per image (" + (per[0]["rule"] if per else
                                                                                    "no images") + ")")
    if vid:
        md["formulas"].append("video tokens per request: " + vid[0]["rule"])
    wc = p["workload_class"]
    if cm.get("cross_attention"):
        md["cross_tokens"] = extra
        md["formulas"].append(f"cross-attention KV = 2 x {cm['cross_attention_layers']} cross-attention layers x "
                              f"{p['model']['num_kv_heads']} KV heads x {p['model']['head_dim']} x vision tokens x "
                              "concurrency x 2 B (vision tokens never enter the text KV cache)")
    elif wc == "inference" and p.get("context"):
        p["context"] = tuple(p["context"][j] + extra[_ix(j)] for j in range(3))
        md["formulas"].append("KV cache context = text context + image/video tokens per request")
        mc = p["model"].get("max_context")
        if mc and p["context"][1] > mc:
            md["notes"].append(f"text + media tokens ({p['context'][1]:,}) exceed the model's {mc:,}-token context: "
                               "fewer images or a lower resolution are needed")
    elif wc == "training" and p.get("seq") and p["seq"][1]:
        p["seq"] = tuple(p["seq"][j] + extra[_ix(j)] for j in range(3))
        md["formulas"].append("training sequence = text tokens + image/video tokens per sample")
    if wc == "inference" and batchy and req.inference is not None and req.inference.concurrency:
        pass
    elif wc == "inference" and batchy:
        msg = "concurrency not stated: 1 sequence at a time assumed (range 1-8)"
        if msg in A:
            A.remove(msg)
        p["concurrency"] = (BATCH[1], BATCH[0], BATCH[2])
        A.append(f"batch job, concurrency not stated: {BATCH[0]} requests at once assumed (range {BATCH[1]}-"
                 f"{BATCH[2]})")


def _resolve_asr(md, req, p, mr, cm, A, known, batchy):
    sec = _g(mr, "audio_seconds")
    hours = _g(mr, "total_audio_hours")
    items = _g(mr, "total_items")
    if p["workload_class"] == "inference":
        _batch(md, req, p, A, known, batchy)
        A.append("greedy decoding assumed: beam search with k beams multiplies the decoder caches by k")
    win_item = whisper_windows(sec) if sec else None
    total_windows = (hours * 3600 / WINDOW_S) if hours else (items * win_item if items and win_item else None)
    md["inputs"] = {"window_seconds": WINDOW_S, "encoder_positions": ENC_POSITIONS,
                    "decoder_max_tokens": cm["max_target_positions"], "encoder_layers": cm["encoder_layers"],
                    "decoder_layers": cm["decoder_layers"], "d_model": cm["d_model"], "mel_bins": cm["mel_bins"],
                    "audio_seconds_per_item": sec, "windows_per_item": win_item, "total_audio_hours": hours,
                    "total_windows": None if total_windows is None else math.ceil(total_windows)}
    md["formulas"] += [f"windows = ceil(audio seconds / {WINDOW_S}); {ENC_POSITIONS} encoder positions per window",
                       "decoder cross-attention KV = 2 x decoder layers x d_model x 1500 x windows in the batch x 2 B",
                       f"decoder self-attention KV = 2 x decoder layers x d_model x {cm['max_target_positions']} x "
                       "windows in the batch x 2 B"]
    tr = req.training
    if p["workload_class"] == "training" and p.get("method") in ("training_lora", "training_qlora") \
            and p["model"].get("num_layers") is None:
        p["model"]["num_layers"] = cm["encoder_layers"] + 2 * cm["decoder_layers"]
        p["known"]["num_layers"] = "derived"
        A.append(f"LoRA on Whisper: adapters on {cm['encoder_layers']} encoder self-attention + "
                 f"{cm['decoder_layers']} decoder self-attention + {cm['decoder_layers']} cross-attention blocks")
    if tr is not None and tr.seq_len:
        md["inputs"]["decoder_train_tokens"] = min(tr.seq_len, cm["max_target_positions"])


def _audio_seconds(md, mr, kind, A, known):
    sec = _g(mr, "audio_seconds")
    if sec is None:
        d = AUDIO_SECONDS[kind]
        A.append(f"audio length per item not stated: {d[0]} s assumed (range {d[1]}-{d[2]} s)")
        known["media.audio_seconds"] = "assumed"
        return d
    return (sec, sec, sec)


def _resolve_audio_encoder(md, req, p, mr, cm, A, known, batchy):
    secs = _audio_seconds(md, mr, "audio_encoder", A, known)
    if p["workload_class"] == "inference":
        _batch(md, req, p, A, known, batchy)
    frames = tuple(int(s * cm["frames_per_second"]) for s in secs)
    md["frames"] = frames
    md["inputs"] = {"audio_seconds_per_item": list(secs), "frames_per_item": list(frames),
                    "frames_per_second": cm["frames_per_second"], "total_audio_hours": _g(mr, "total_audio_hours"),
                    "total_items": _g(mr, "total_items")}
    md["formulas"].append(f"frames = audio seconds x {cm['sample_rate']:,} samples/s / {cm['conv_stride_product']} "
                          f"(conv stride) = {cm['frames_per_second']} per second; activations by the transformer "
                          "formula over those frames")


def _resolve_tts(md, req, p, mr, cm, A, known, batchy):
    secs = _audio_seconds(md, mr, "tts", A, known)
    if p["workload_class"] == "inference":
        _batch(md, req, p, A, known, batchy)
    md["inputs"] = {"audio_seconds_per_item": list(secs), "autoregressive": cm.get("autoregressive"),
                    "total_items": _g(mr, "total_items"), "total_audio_hours": _g(mr, "total_audio_hours")}
    if cm.get("autoregressive"):
        frames = tuple(int(math.ceil(s * cm["frame_rate"])) + cm["num_codebooks"] - 1 for s in secs)
        md["frames"] = frames
        md["inputs"].update(frame_rate=cm["frame_rate"], num_codebooks=cm["num_codebooks"],
                            decoder_steps_per_item=list(frames), prompt_tokens=TTS_PROMPT)
        md["formulas"].append(f"decoder steps = audio seconds x {cm['frame_rate']} codec frames/s + "
                              f"{cm['num_codebooks'] - 1} (delay pattern over {cm['num_codebooks']} codebooks); KV = 2 x "
                              f"layers x KV heads x head dim x (steps + {TTS_PROMPT} prompt tokens) x batch x 2 B")
        A.append(f"text prompt assumed {TTS_PROMPT} tokens per item")
    else:
        md["formulas"].append("not autoregressive: weights + framework overhead (activations of an 82M-parameter "
                              "model are negligible next to the fixed overhead)")


def _resolve_video_gen(md, req, p, mr, cm, A, known, batchy):
    res = _res_or(mr, "output_resolution")
    if res is None:
        res = parse_resolution(cm["native_resolution"])
        A.append(f"output resolution not stated: {cm['native_resolution']} assumed (the model's default)")
        known["media.output_resolution"] = "assumed"
    elif cm.get("fixed_resolution") and f"{res[0]}x{res[1]}" != cm["native_resolution"]:
        md["notes"].append(f"{p['catalog']['id']} generates {cm['native_resolution']} only (model card): estimated at "
                           f"the requested {res[0]}x{res[1]} anyway")
    sec, fps = _g(mr, "output_video_seconds"), _g(mr, "output_fps")
    if sec is None:
        A.append(f"video length not stated: the model's default {cm['native_frames']} frames assumed")
        known["media.output_video_seconds"] = "assumed"
    elif fps is None and cm.get("native_fps") is None:
        A.append(f"frame rate not stated and not on the model card: {VIDEO_FPS_FALLBACK} frames/s assumed")
    lat = video_latent(cm, sec, fps, *res)
    steps = _g(mr, "diffusion_steps") or cm["steps"]
    if _g(mr, "diffusion_steps") is None:
        A.append(f"denoising steps not stated: {cm['steps']} assumed (the model's default)")
    md["latent"] = lat
    md["resolution"] = list(res)
    md["steps"] = steps
    md["confidence_cap"] = "low"
    md["inputs"] = {"output_resolution": f"{res[0]}x{res[1]}", "frames": lat["frames"], "fps": lat["fps"],
                    "seconds_per_video": lat["seconds"], "latent_frames": lat["latent_frames"],
                    "video_tokens": lat["tokens"], "text_tokens_joint": cm["joint_text_tokens"],
                    "text_tokens_cross": cm["cross_text_tokens"], "steps": steps,
                    "forward_passes_per_step": cm["cfg_forward_passes"], "total_videos": _g(mr, "total_items")}
    md["formulas"] += ["video tokens: " + lat["rule"],
                       "weights = transformer x bytes/param + text encoder (16-bit) + VAE; low end: text encoder "
                       "offloaded after encoding the prompt (diffusers enable_model_cpu_offload)",
                       "transient activations per block (attention via SDPA / FlashAttention, scores never stored) = "
                       "batch x tokens x (6 x hidden + 2 x FFN) x 2 B (low: (3 x hidden + FFN); high: 2 x central); "
                       "batch 2 when classifier-free guidance batches its two passes",
                       f"VAE decode = {VAE_CHUNK} frames x H x W x top VAE channels x 2 B x 3 buffers (low: tiled "
                       "decode, 1/8; high: 2 x central)",
                       "ROUGHLY MODELLED: activation and VAE-decode memory depend on the implementation; confidence "
                       "low"]
    if p["workload_class"] == "training":
        md["notes"].append("video-diffusion fine-tuning memory is not modelled: weights, gradients and optimizer "
                           "states are counted, activations are not (the total is a lower bound)")


# --------------------------------------------------------------------------------------------- components (memory)

def adjust(p: dict, scenario: int, out: dict, notes: list) -> None:
    """Fill / correct component bytes for a media kind (memory.components calls this after its generic terms)."""
    md = p.get("media")
    if not md or md["kind"] not in MEDIA_KINDS:
        return
    kind, m, method = md["kind"], p["model"], p["method"]
    cm = ((p.get("catalog") or {}).get("media")) or {}
    i = _ix(scenario)
    act_b = 4 if p["precision"] == "fp32" else 2
    bd: dict[str, float] = {}
    training = bool(method) and method.startswith("training")
    if not cm:
        if training and out.get("activations") == 0.0 and kind != "vlm":
            out["activations"] = None
            notes.append("activation memory not estimated: the architecture's dimensions are not on record; the total "
                         "is a lower bound")
        md.setdefault("_bd", {})[scenario] = bd
        return
    bpp = M.weight_bytes_per_param(p["precision"], p["quantization"])
    if kind == "vlm":
        vp = cm.get("vision_params_b")
        if vp is None and cm.get("vision_hidden") and cm.get("vision_layers"):
            vp = 12 * cm["vision_layers"] * cm["vision_hidden"] ** 2 / 1e9
        quant = (p["quantization"] or "none") != "none"
        if quant and vp:
            out["weights"] += vp * 1e9 * (2.0 - bpp)
            bd["vision_tower_16bit_extra"] = vp * 1e9 * (2.0 - bpp)
        if md.get("cross_tokens") and method == "inference":
            kvh, hd = m.get("num_kv_heads") or 8, m.get("head_dim") or 128
            cc = (p.get("concurrency") or (1, 1, 1))[scenario]
            ck = 2 * cm["cross_attention_layers"] * kvh * hd * md["cross_tokens"][i] * cc * 2
            L = m.get("num_layers")
            if out.get("kv_cache") and L:
                # the text cache lives only in the self-attention layers (the cross-attention layers have none)
                out["kv_cache"] *= (L - cm["cross_attention_layers"]) / L
                bd["text_kv_self_attention_layers"] = out["kv_cache"]
            out["kv_cache"] = (out.get("kv_cache") or 0.0) + ck
            bd["cross_attention_kv"] = ck
        if method == "inference" and cm.get("vision_hidden"):
            per_img = [x for x in (md["inputs"].get("tokens_per_image") or [])]
            patches = None
            if per_img and per_img[i]:
                patches = per_img[i] * (cm.get("merge", 1) ** 2)
            elif md["inputs"].get("video"):
                patches = md["inputs"]["video"]["tokens_per_frame_pair"] * cm.get("merge", 1) ** 2 * 2
            if patches:
                va = patches * cm["vision_hidden"] * 34 * act_b / 2      # one vision layer, one image, SDPA
                out["activations"] = (out.get("activations") or 0.0) + va
                bd["vision_encoder_activations"] = va
    elif kind == "asr":
        L_enc, L_dec, d, a = cm["encoder_layers"], cm["decoder_layers"], cm["d_model"], cm["heads"]
        if method == "inference":
            b = md["batch"][i]
            cross = 2 * L_dec * d * ENC_POSITIONS * b * 2
            selfkv = 2 * L_dec * d * cm["max_target_positions"] * b * 2
            enc_act = b * ENC_POSITIONS * d * 34 * act_b / 2
            out["kv_cache"] = cross + selfkv
            out["activations"] = enc_act
            bd.update(decoder_cross_attention_kv=cross, decoder_self_attention_kv=selfkv,
                      encoder_layer_activations=enc_act)
        elif training:
            b = p["batch"][scenario]
            ck = p["ckpt"][scenario] if p.get("ckpt") else False
            dec_s = md["inputs"].get("decoder_train_tokens") or cm["max_target_positions"]
            enc = M.transformer_activation_bytes(seq=ENC_POSITIONS, batch=b, hidden=d, heads=a, layers=L_enc,
                                                 flash=True, checkpointing=ck, vocab=None, act_bytes=act_b)
            dec = M.transformer_activation_bytes(seq=dec_s, batch=b, hidden=d, heads=a, layers=L_dec, flash=True,
                                                 checkpointing=ck, vocab=m.get("vocab_size"), act_bytes=act_b)
            out["activations"] = enc + dec
            bd.update(encoder_activations=enc, decoder_activations=dec)
            if "Whisper decoder cross-attention activations are not counted (small next to the self-attention and "\
               "MLP terms)" not in notes:
                notes.append("Whisper decoder cross-attention activations are not counted (small next to the "
                             "self-attention and MLP terms)")
    elif kind == "audio_encoder":
        T = md["frames"][i]
        h, a, L = m["hidden_size"], m["num_attention_heads"], m["num_layers"]
        if method == "inference":
            b = md["batch"][i]
            va = b * T * h * 34 * act_b / 2
            out["activations"] = va
            bd["encoder_layer_activations"] = va
        elif training:
            b = p["batch"][scenario]
            ck = p["ckpt"][scenario] if p.get("ckpt") else False
            out["activations"] = M.transformer_activation_bytes(seq=T, batch=b, hidden=h, heads=a, layers=L,
                                                                flash=True, checkpointing=ck, vocab=None,
                                                                act_bytes=act_b)
            bd["encoder_activations"] = out["activations"]
    elif kind == "tts":
        if method == "inference" and cm.get("autoregressive"):
            b = md["batch"][i]
            L, kvh, hd, h = m["num_layers"], m["num_kv_heads"], m["head_dim"], m["hidden_size"]
            steps = md["frames"][i]
            kv = 2 * L * kvh * hd * (steps + TTS_PROMPT) * b * 2
            cross = 2 * L * h * TTS_PROMPT * b * 2
            out["kv_cache"] = kv + cross
            bd.update(decoder_kv=kv, description_cross_kv=cross)
        elif training:
            out["activations"] = None
            notes.append("text-to-speech fine-tuning activations are not modelled: the total is a lower bound")
    elif kind == "video_diffusion":
        lat, cmd = md["latent"], cm
        te = cmd["text_encoder"]["params_b"] * 1e9 * 2
        vae = (cmd["vae"].get("params_b") or 0) * 1e9 * 2 if cmd["vae"].get("params_b") else \
            float(cmd["vae"].get("bytes") or 0)
        dit = cmd["transformer_params_b"] * 1e9 * bpp
        te_now = 0.0 if scenario == 0 else te
        out["weights"] = dit + te_now + vae
        bd.update(transformer_weights=dit, text_encoder_weights=te_now, vae_weights=vae)
        if method == "inference":
            h, ffn = m["hidden_size"], cmd["ffn_dim"]
            T = lat["tokens"] + cmd["joint_text_tokens"]
            batch = 2 if (cmd["cfg_forward_passes"] == 2 and cmd["cfg_batched"]) else 1
            per_tok = {0: 3 * h + ffn, 1: 6 * h + 2 * ffn, 2: 2 * (6 * h + 2 * ffn)}[scenario]
            dit_act = batch * T * per_tok * 2
            lat_b = cmd["latent_channels"] * lat["latent_frames"] * lat["latent_hw"][0] * lat["latent_hw"][1] * 4 * 3
            w, hh = md["resolution"]
            vae_c = min(lat["frames"], VAE_CHUNK) * w * hh * cmd["vae"]["top_channels"] * 2 * 3
            vae_dec = {0: vae_c / 8, 1: vae_c, 2: 2 * vae_c}[scenario]
            out["activations"] = dit_act + lat_b + vae_dec
            bd.update(transformer_block_activations=dit_act, latents=lat_b, vae_decode=vae_dec)
        elif training:
            out["activations"] = None
    md.setdefault("_bd", {})[scenario] = bd


# --------------------------------------------------------------------------------------------- work (runtime)

def _no(reason: str) -> dict:
    return {"known": False, "reason": reason}


def work(req, p) -> dict:
    """GPU work per item, as (central, slow, fast) triples, and the number of items. Pure. `known` is True when the
    per-item work is computable; `items` None means the total is unknown (a rate is still shown)."""
    md = p.get("media")
    if not md or md["kind"] not in MEDIA_KINDS:
        return _no("not a media workload")
    if p.get("method") != "inference":
        return _no("runtime is estimated for media inference and batch jobs only (training: give dataset tokens)")
    cm = ((p.get("catalog") or {}).get("media")) or {}
    if not cm:
        return _no("the model's architecture is not in OpenGrid's catalog")
    kind, m, mr = md["kind"], p["model"], getattr(req, "media", None)
    bpp = M.weight_bytes_per_param(p["precision"], p["quantization"])
    P_tot = (m.get("parameters_b") or 0) * 1e9
    A: list[str] = []
    out = {"known": True, "kind": "estimated", "assumptions": A, "pipeline_eff": None, "decode": None}
    # slots: index 0 central, 1 slow end, 2 fast end
    if kind == "asr":
        L_enc, L_dec, d = cm["encoder_layers"], cm["decoder_layers"], cm["d_model"]
        P_enc = 12 * L_enc * d * d
        P_dec = max(P_tot - P_enc, 0.0)
        tau = (WHISPER_TOKENS[0], WHISPER_TOKENS[2], WHISPER_TOKENS[1])
        B = (md["batch"][0], md["batch"][1], md["batch"][2])
        enc = 2 * P_enc * ENC_POSITIONS + 4 * L_enc * ENC_POSITIONS ** 2 * d
        out.update(item="window", compute_flops=[enc + 2 * P_dec * t for t in tau],
                   decode={"steps": list(tau), "batch": list(B),
                           "bytes_per_step": [P_dec * bpp + B[j] * (2 * L_dec * d * ENC_POSITIONS * 2
                                                                    + 2 * L_dec * d * (tau[j] / 2) * 2)
                                              for j in range(3)],
                           "flops_per_step": [2 * P_dec * B[j] for j in range(3)]},
                   pipeline_eff=list(PIPELINE_EFF))
        A += [f"encoder parameters 12 x {L_enc} layers x {d}^2 = {P_enc / 1e9:.3g}B (from the published dimensions); "
              f"decoder = the rest, {P_dec / 1e9:.3g}B",
              f"{WHISPER_TOKENS[0]} decoder tokens per 30 s window (range {WHISPER_TOKENS[1]}-{WHISPER_TOKENS[2]}; "
              "speech density varies)",
              f"end-to-end pipeline efficiency {PIPELINE_EFF[0]:.0%} of the GPU roofline (range {PIPELINE_EFF[1]:.0%}-"
              f"{PIPELINE_EFF[2]:.0%}): decoding loops are latency-bound and audio decoding / feature extraction "
              "run on the CPU; an OpenGrid planning assumption, not a measurement"]
        hours = _g(mr, "total_audio_hours")
        inp = md["inputs"]
        out["items"] = inp.get("total_windows")
        out["unit"] = {"name": "audio hours", "per_item": WINDOW_S / 3600,
                       "total": hours if hours else (out["items"] * WINDOW_S / 3600 if out["items"] else None)}
        if out["items"] is None:
            out["items_reason"] = "the amount of audio is unknown (give media.total_audio_hours)"
    elif kind == "audio_encoder":
        L, h = m["num_layers"], m["hidden_size"]
        fr = (md["frames"][0], md["frames"][2], md["frames"][1])
        secs = md["inputs"]["audio_seconds_per_item"]
        out.update(item="clip", compute_flops=[2 * P_tot * T + 4 * L * T * T * h for T in fr])
        hours, items = _g(mr, "total_audio_hours"), _g(mr, "total_items")
        out["items"] = (math.ceil(hours * 3600 / secs[0]) if hours else items)
        out["unit"] = {"name": "audio hours", "per_item": secs[0] / 3600,
                       "total": hours if hours else (items * secs[0] / 3600 if items else None)}
        if out["items"] is None:
            out["items_reason"] = "the amount of audio is unknown (give media.total_audio_hours or total_items)"
        A.append("compute-bound encoder: 2 x parameters x frames + 4 x layers x frames^2 x hidden (attention) per clip")
    elif kind == "tts":
        if not cm.get("autoregressive"):
            return _no("no published compute figure per second of audio for this model: runtime not estimated")
        L, h = m["num_layers"], m["hidden_size"]
        kvh, hd = m["num_kv_heads"], m["head_dim"]
        P_dec = 16 * L * h * h
        fr = (md["frames"][0], md["frames"][2], md["frames"][1])
        B = tuple(md["batch"])
        secs = md["inputs"]["audio_seconds_per_item"]
        out.update(item="utterance", compute_flops=[2 * P_dec * f for f in fr],
                   decode={"steps": list(fr), "batch": list(B),
                           "bytes_per_step": [P_dec * bpp + B[j] * 2 * L * kvh * hd * (fr[j] / 2 + TTS_PROMPT) * 2
                                              for j in range(3)],
                           "flops_per_step": [2 * P_dec * B[j] for j in range(3)]},
                   pipeline_eff=list(PIPELINE_EFF))
        A += [f"decoder parameters 16 x {L} layers x {h}^2 = {P_dec / 1e9:.3g}B (self- and cross-attention + 4x MLP, "
              "from the published dimensions)",
              f"end-to-end pipeline efficiency {PIPELINE_EFF[0]:.0%} of the GPU roofline (range {PIPELINE_EFF[1]:.0%}-"
              f"{PIPELINE_EFF[2]:.0%}): an OpenGrid planning assumption, not a measurement"]
        hours, items = _g(mr, "total_audio_hours"), _g(mr, "total_items")
        out["items"] = math.ceil(hours * 3600 / secs[0]) if hours else items
        out["unit"] = {"name": "audio hours generated", "per_item": secs[0] / 3600,
                       "total": hours if hours else (items * secs[0] / 3600 if items else None)}
        if out["items"] is None:
            out["items_reason"] = "the amount of speech to generate is unknown (give media.total_items or " \
                                  "total_audio_hours)"
    elif kind == "vlm":
        vp = cm.get("vision_params_b")
        if vp is None:
            vp = 12 * cm["vision_layers"] * cm["vision_hidden"] ** 2 / 1e9
            A.append(f"vision encoder {vp:.3g}B parameters (12 x layers x hidden^2 from its dimensions)")
        P_v = vp * 1e9
        P_llm = max(P_tot - P_v, 0.0)
        inp = md["inputs"]
        media_tok = inp["media_tokens_per_request"]
        merge2 = cm.get("merge", 1) ** 2
        patches = [media_tok[j] * merge2 for j in range(3)]
        inf = req.inference
        outt = (inf.output_tokens,) * 3 if inf is not None and inf.output_tokens else VLM_OUTPUT
        if not (inf is not None and inf.output_tokens):
            A.append(f"output tokens per request not stated: {VLM_OUTPUT[0]} assumed (range {VLM_OUTPUT[1]}-"
                     f"{VLM_OUTPUT[2]})")
        A.append(f"text prompt {VLM_PROMPT[0]} tokens per request assumed (range {VLM_PROMPT[1]}-{VLM_PROMPT[2]})")
        sl = lambda t: (t[0], t[2], t[1])          # (central, low, high) -> (central, slow, fast)
        ot, pt = sl(outt), sl(VLM_PROMPT)
        mt = (media_tok[0], media_tok[2], media_tok[1])
        pa = (patches[0], patches[2], patches[1])
        cross = bool(cm.get("cross_attention"))
        prefill = [(0 if cross else mt[j]) + pt[j] for j in range(3)]
        cc = p.get("concurrency") or (1, 1, 1)
        B = (cc[1], cc[0], cc[2])
        kvh, hd, L = m.get("num_kv_heads") or 8, m.get("head_dim") or 128, m.get("num_layers") or 0
        out.update(item="request", compute_flops=[2 * P_v * pa[j] + 2 * P_llm * prefill[j] for j in range(3)],
                   decode={"steps": list(ot), "batch": list(B),
                           "bytes_per_step": [P_llm * bpp + B[j] * 2 * L * kvh * hd * (prefill[j] + ot[j] / 2) * 2
                                              for j in range(3)],
                           "flops_per_step": [2 * P_llm * B[j] for j in range(3)]})
        A.append("vision encoder 2 x parameters x patches; LLM prefill 2 x parameters x (media + prompt tokens); "
                 "decode memory-bandwidth bound (weights + KV read per step); vision attention not counted")
        out["items"] = _g(mr, "total_items")
        out["unit"] = {"name": "requests", "per_item": 1, "total": out["items"]}
        if out["items"] is None:
            out["items_reason"] = "the number of images / requests is unknown (give media.total_items)"
    elif kind == "video_diffusion":
        lat = md["latent"]
        h, L = m["hidden_size"], m["num_layers"]
        T = lat["tokens"] + cm["joint_text_tokens"]
        Pd = cm["transformer_params_b"] * 1e9
        per_pass = 2 * Pd * T + 4 * L * T * T * h + 4 * L * T * cm["cross_text_tokens"] * h
        flops = md["steps"] * cm["cfg_forward_passes"] * per_pass
        out.update(item="video", compute_flops=[flops] * 3)
        A += [f"per video: {md['steps']} steps x {cm['cfg_forward_passes']} forward pass(es) x (2 x "
              f"{cm['transformer_params_b']:.3g}B transformer parameters x {T:,} tokens + 4 x {L} blocks x tokens^2 x "
              f"{h} (attention)) = {flops:.3g} FLOPs; text encoder and VAE decode time not included"]
        items = _g(mr, "total_items")
        out["items"] = items or 1
        if not items:
            A.append("1 video assumed (media.total_items sets the batch)")
        out["unit"] = {"name": "videos", "per_item": 1, "total": out["items"],
                       "seconds_per_video": lat["seconds"]}
    else:
        return _no("not a media workload")
    out["mfu"] = list(DIFFUSION_MFU if kind == "video_diffusion" else MFU)
    out["bw_util"] = list(BW_UTIL)
    return out


def option_runtime(spec: dict, n: int, n_min: int, prof: dict, link_kind: str) -> dict | None:
    """Theoretical runtime and throughput of one option for the media work in `prof["media"]["work"]`. None when the
    workload is not a media one. Independent items run as n // n_min replicas (n_min GPUs each, tensor parallel
    within a replica when n_min > 1)."""
    md = (prof or {}).get("media") or {}
    wk = md.get("work") or {}
    if not wk.get("known"):
        return None if not md else {"known": False, "reason": wk.get("reason") or "media work unknown"}
    b = P.throughput_basis("training_full", prof.get("precision"), spec)
    bw = spec.get("memory_bandwidth_tbps")
    dec = wk.get("decode")
    if not b or (dec and not bw):
        return {"known": False, "reason": "no throughput / memory-bandwidth figure on record for this GPU"}
    peak, bwb = b["per_gpu"] * 1e12, (float(bw) * 1e12 if bw else None)
    mfu = tuple(wk.get("mfu") or MFU)
    mfu_t = (mfu[0], mfu[1], mfu[2])              # central, slow (low MFU), fast (high MFU)
    bw_t = (BW_UTIL[0], BW_UTIL[1], BW_UTIL[2])
    eff_t = wk.get("pipeline_eff")

    def seconds_per_item(j: int) -> float:
        t = wk["compute_flops"][j] / (peak * mfu_t[j])
        if dec:
            step = max(dec["bytes_per_step"][j] / (bwb * bw_t[j]), dec["flops_per_step"][j] / (peak * mfu_t[j]))
            t += dec["steps"][j] * step / max(1, dec["batch"][j])
        if eff_t:
            t /= eff_t[j]
        return t

    s = [seconds_per_item(j) for j in range(3)]
    n_min = max(1, n_min)
    replicas = max(1, n // n_min)
    if n_min > 1:
        sc = P.scaling(link_kind, n_min, "inference")
        inner = (sc["speedup"], sc["speedup_range"][0], sc["speedup_range"][1])
    else:
        inner = (1.0, 1.0, 1.0)
    rep = REPLICA_EFF if replicas > 1 else (1.0, 1.0, 1.0)
    speed = [replicas * rep[j] * inner[j] for j in range(3)]
    unit = wk["unit"]
    per_unit_item = unit["per_item"]
    rate = [3600 / s[j] * per_unit_item for j in range(3)]       # units per GPU-hour on one replica of n_min GPUs
    rate_gpu = [r / n_min for r in rate]
    thr = {"unit": f"{unit['name']} per GPU-hour", "value": round(rate_gpu[0], 4),
           "range": [round(rate_gpu[1], 4), round(rate_gpu[2], 4)], "kind": "estimated",
           "basis": "theoretical: compute at the assumed utilization + memory-bound decode"
                    + (" / pipeline efficiency" if eff_t else "") + "; not a benchmark"}
    if md.get("media_kind") == "video_diffusion":
        sv = unit.get("seconds_per_video") or 0
        thr["video_seconds_per_gpu_hour"] = round(rate_gpu[0] * sv, 3)
    par = (f"{replicas} independent replica(s), one per GPU, each on its own share of the items" if n_min == 1 else
           f"{replicas} replica(s) of {n_min} GPUs (tensor parallel within each replica)")
    out = {"kind": "estimated", "throughput": thr, "parallelism": par, "replicas": replicas,
           "speedup": [round(speed[0], 3), round(speed[1], 3), round(speed[2], 3)],
           "seconds_per_item": {"central": round(s[0], 4), "range": [round(s[2], 4), round(s[1], 4)],
                                "item": wk["item"]}}
    items = wk.get("items")
    if not items:
        out.update(known=False, hours=None, range_hours=None,
                   reason=wk.get("items_reason") or "the number of items is unknown")
        return out
    hrs = [items * s[j] / speed[j] / 3600 for j in range(3)]
    out.update(known=True, hours=round(hrs[0], 4), range_hours=[round(hrs[2], 4), round(hrs[1], 4)],
               basis=(f"{items:,} {wk['item']}(s) x {s[0]:.3g} s each on {n_min} GPU(s) / {speed[0]:.3g}x "
                      f"({par}); range {s[2]:.3g}-{s[1]:.3g} s per {wk['item']}. Theoretical, not a benchmark"),
               assumptions=list(wk.get("assumptions") or []) + [
                   f"model FLOPs utilization {mfu[0]:.0%} (range {mfu[1]:.0%}-{mfu[2]:.0%}"
                   + ("; contains every publisher-reported video generation time on record" if mfu == DIFFUSION_MFU
                      else "") + f"); decode at "
                   f"{BW_UTIL[0]:.0%} of peak memory bandwidth (range {BW_UTIL[1]:.0%}-{BW_UTIL[2]:.0%})",
                   "excludes startup, model download, data loading and writing results"],
               sources={"training_flops": SOURCES["training_flops"], "mfu_palm": P.SOURCES["mfu_palm"]})
    return out


def compute_bound(prof: dict) -> bool:
    """Video generation and speech encoders are compute bound (big matmuls), not bandwidth bound."""
    md = (prof or {}).get("media") or {}
    return md.get("media_kind") in ("video_diffusion", "audio_encoder")


# --------------------------------------------------------------------------------------------- summary / notes

def _gbs(d: dict) -> dict:
    return {k: round(v / GB, 3) for k, v in d.items() if v is not None}


def summary(p: dict) -> dict | None:
    md = p.get("media")
    if not md:
        return None
    out = {k: v for k, v in md.items() if not k.startswith("_") and k not in ("extra_tokens", "cross_tokens",
                                                                               "frames", "latent")}
    bd = (md.get("_bd") or {}).get(1)
    if bd:
        out["breakdown_gb"] = _gbs(bd)
    out["kind"] = "estimated"
    out["media_kind"] = md["kind"]
    out["sources"] = {k: v for k, v in SOURCES.items()}
    return out


def smallest_vram_fitting(need_gb: float, usable: float = 1.0) -> float | None:
    """The smallest whole-GPU VRAM tier on record (MIG slices and laptops excluded) that holds `need_gb`."""
    import hardware

    tiers = sorted({float(s["vram_gb"]) for s in hardware.all_specs().values()
                    if s.get("vram_gb") and s.get("form_factor") not in ("MIG slice", "Laptop")})
    return next((t for t in tiers if t * usable >= need_gb), None)


def headline(est: dict) -> str | None:
    """One plain sentence on what the media inputs cost in memory (for notes and the recommended option's why)."""
    mem = est.get("memory") or {}
    md = est.get("media") or {}
    kind = md.get("media_kind")
    inp = md.get("inputs") or {}
    bd = md.get("breakdown_gb") or {}
    if kind == "vlm" and inp.get("media_tokens_per_request"):
        t = inp["media_tokens_per_request"][0]
        if not t:
            return None
        imgs = (inp.get("images_per_request") or [0])[0]
        tpi = (inp.get("tokens_per_image") or [None])[0]
        what = (f"{imgs} image(s) x {tpi:,} tokens" if imgs and tpi else "") + \
               (" + " if imgs and tpi and inp.get("video") else "") + \
               (f"{inp['video']['tokens']:,} video tokens" if inp.get("video") else "")
        kv = (mem.get("components_gb") or {}).get("kv_cache")
        return (f"image/video input adds {t:,} tokens per request ({what}) to the "
                + ("cross-attention cache" if bd.get("cross_attention_kv") else "KV cache")
                + (f"; KV cache {kv:g} GB per GPU in total" if kv else "") + " (estimated)")
    if mem.get("method") != "inference":
        return None
    if kind == "asr":
        kv = bd.get("decoder_cross_attention_kv", 0) + bd.get("decoder_self_attention_kv", 0)
        b = (md.get("batch") or [1])[0]
        return (f"Whisper: {b} x 30 s window(s) decoded together keep {kv:.2g} GB of decoder caches next to the "
                f"weights (estimated)")
    if kind == "video_diffusion":
        tot = (mem.get("per_gpu_by_count") or {}).get(1) or (mem.get("per_gpu_by_count") or {}).get("1") or {}
        need = tot.get("recommended_gb")
        s = (f"video generation at {inp.get('output_resolution')}, {inp.get('frames')} frames "
             f"({inp.get('video_tokens'):,} latent tokens, {inp.get('steps')} steps)")
        if need:
            s += f" needs about {need:g} GB on one GPU with margin"
            smallest = smallest_vram_fitting(need, float(mem.get("usable_vram_fraction") or 1.0))
            if smallest and smallest >= 64:
                s += f": only GPUs with at least {smallest:g} GB hold it on one GPU"
        return s + " (estimated, low confidence: activation and VAE-decode memory are roughly modelled)"
    if kind == "tts":
        return "text-to-speech: weights dominate; the KV cache grows with seconds of audio per item (estimated)"
    if kind == "audio_encoder":
        return "speech encoder: activations grow with seconds of audio per clip and the batch (estimated)"
    return None
