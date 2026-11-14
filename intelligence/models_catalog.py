"""Well-known open model configurations, for filling what a request does not state.

    lookup(name) -> {"entry": dict, "match": "exact"|"loose", "candidates": [ids]} | None
    get(model_id) -> dict | None
    all_models() -> [dict]

Every number comes from the model's published config.json / model card / paper (the `source` URL). A value we
could not confirm is None, never guessed. parameters_b is the total parameter count in billions as stated by the
publisher (MoE: total, with active_parameters_b alongside).

kind
    decoder    decoder-only transformer (LLM): KV cache and the transformer activation formula apply
    encoder    encoder-only transformer (BERT): the activation formula applies, no KV cache
    moe        mixture-of-experts decoder: all expert weights are resident; active params drive compute only
    vit        vision transformer: the activation formula applies with seq = patches + 1
    cnn        convolutional network: activations from `activation_elements_per_sample` (see below)
    diffusion  latent diffusion pipeline: weights only; activation memory is not modelled
    vlm        vision-language model: vision encoder + projector + decoder LLM; the LLM dims are the top-level fields,
               image tokens join the KV cache (cross-attention models: their own cross-attention KV), see `media`
    asr        Whisper-style speech recognition encoder-decoder: 30 s windows, 1500 encoder positions, decoder KV
    audio_encoder  wav2vec2-style speech encoder: frames = audio seconds x samples per second / conv stride
    tts        text-to-speech: autoregressive codec-token decoder (KV over audio frames) or a non-autoregressive model
    video_diffusion  text-to-video diffusion transformer + text encoder + VAE: weights, latent video tokens,
               per-block activations and VAE decode (activation memory only roughly modelled: low confidence)

Audio / image / video entries carry a `media` dict with the architecture facts the media formulas use
(intelligence/media.py); each value is from the entry's `source` / `paper`, or is labelled "derived" (computed from
published dimensions or file sizes) in `media["derived"]`.

ResNet-50 activation elements are COMPUTED from the architecture (He et al. 2016, Table 1; torchvision v1.5
strides) at 224x224: the sum of the output elements of every conv, batch-norm, pooling and residual-add layer
(27,948,032 per image; ReLU is in place in torchvision and shares its input's storage). Counting the ReLU outputs
separately as well gives 37,556,736 (the `_high` figure). The same count reproduces torchvision's 25,557,032
parameters exactly, which checks the layer shapes. It is an architectural count, not a measurement.
"""

from __future__ import annotations

import re

HF = "https://huggingface.co/{}/blob/main/config.json"

_CATALOG: list[dict] = []


def _m(id_, family, version, size, kind, params, *, layers=None, hidden=None, heads=None, kv=None, head_dim=None,
       vocab=None, ctx=None, active=None, aliases=(), source=None, paper=None, note=None, **extra):
    _CATALOG.append({
        "id": id_, "family": family, "version": version, "size": size, "kind": kind, "parameters_b": params,
        "active_parameters_b": active, "num_layers": layers, "hidden_size": hidden, "num_attention_heads": heads,
        "num_kv_heads": kv, "head_dim": head_dim, "vocab_size": vocab, "max_context": ctx,
        "aliases": list(aliases), "source": source or HF.format(id_), "paper": paper, "note": note, **extra})


LLAMA3 = "https://arxiv.org/abs/2407.21783"
LLAMA2 = "https://arxiv.org/abs/2307.09288"
QWEN25 = "https://arxiv.org/abs/2412.15115"
GEMMA2 = "https://arxiv.org/abs/2408.00118"
PHI3 = "https://arxiv.org/abs/2404.14219"

# ---------------------------------------------------------------- Llama
_m("meta-llama/Llama-3.2-1B", "llama", "3.2", "1b", "decoder", 1.23, layers=16, hidden=2048, heads=32, kv=8,
   head_dim=64, vocab=128256, ctx=131072, paper=LLAMA3, aliases=["meta-llama/Llama-3.2-1B-Instruct"])
_m("meta-llama/Llama-3.2-3B", "llama", "3.2", "3b", "decoder", 3.21, layers=28, hidden=3072, heads=24, kv=8,
   head_dim=128, vocab=128256, ctx=131072, paper=LLAMA3, aliases=["meta-llama/Llama-3.2-3B-Instruct"])
_m("meta-llama/Llama-3.1-8B", "llama", "3.1", "8b", "decoder", 8.03, layers=32, hidden=4096, heads=32, kv=8,
   head_dim=128, vocab=128256, ctx=131072, paper=LLAMA3,
   aliases=["meta-llama/Llama-3.1-8B-Instruct", "meta-llama/Meta-Llama-3.1-8B", "meta-llama/Meta-Llama-3.1-8B-Instruct"])
_m("meta-llama/Llama-3.1-70B", "llama", "3.1", "70b", "decoder", 70.6, layers=80, hidden=8192, heads=64, kv=8,
   head_dim=128, vocab=128256, ctx=131072, paper=LLAMA3,
   aliases=["meta-llama/Llama-3.1-70B-Instruct", "meta-llama/Meta-Llama-3.1-70B", "meta-llama/Meta-Llama-3.1-70B-Instruct"])
_m("meta-llama/Llama-3.1-405B", "llama", "3.1", "405b", "decoder", 405.0, layers=126, hidden=16384, heads=128, kv=8,
   head_dim=128, vocab=128256, ctx=131072, paper=LLAMA3,
   aliases=["meta-llama/Llama-3.1-405B-Instruct", "meta-llama/Meta-Llama-3.1-405B"])
_m("meta-llama/Meta-Llama-3-8B", "llama", "3", "8b", "decoder", 8.03, layers=32, hidden=4096, heads=32, kv=8,
   head_dim=128, vocab=128256, ctx=8192, paper=LLAMA3, aliases=["meta-llama/Meta-Llama-3-8B-Instruct"])
_m("meta-llama/Meta-Llama-3-70B", "llama", "3", "70b", "decoder", 70.6, layers=80, hidden=8192, heads=64, kv=8,
   head_dim=128, vocab=128256, ctx=8192, paper=LLAMA3, aliases=["meta-llama/Meta-Llama-3-70B-Instruct"])
_m("meta-llama/Llama-2-7b-hf", "llama", "2", "7b", "decoder", 6.74, layers=32, hidden=4096, heads=32, kv=32,
   head_dim=128, vocab=32000, ctx=4096, paper=LLAMA2, aliases=["meta-llama/Llama-2-7b-chat-hf"])
_m("meta-llama/Llama-2-13b-hf", "llama", "2", "13b", "decoder", 13.0, layers=40, hidden=5120, heads=40, kv=40,
   head_dim=128, vocab=32000, ctx=4096, paper=LLAMA2, aliases=["meta-llama/Llama-2-13b-chat-hf"])
_m("meta-llama/Llama-2-70b-hf", "llama", "2", "70b", "decoder", 69.0, layers=80, hidden=8192, heads=64, kv=8,
   head_dim=128, vocab=32000, ctx=4096, paper=LLAMA2, aliases=["meta-llama/Llama-2-70b-chat-hf"])

# ---------------------------------------------------------------- Mistral
_m("mistralai/Mistral-7B-v0.1", "mistral", "0.1", "7b", "decoder", 7.24, layers=32, hidden=4096, heads=32, kv=8,
   head_dim=128, vocab=32000, ctx=32768, paper="https://arxiv.org/abs/2310.06825",
   aliases=["mistralai/Mistral-7B-Instruct-v0.1"])
_m("mistralai/Mixtral-8x7B-v0.1", "mixtral", "0.1", "8x7b", "moe", 46.7, active=12.9, layers=32, hidden=4096,
   heads=32, kv=8, head_dim=128, vocab=32000, ctx=32768, paper="https://arxiv.org/abs/2401.04088",
   aliases=["mistralai/Mixtral-8x7B-Instruct-v0.1"],
   note="all 8 experts are resident in memory (46.7B); 12.9B parameters are active per token")

# ---------------------------------------------------------------- Qwen2.5 (Q heads / KV heads per the model cards)
for _size, _p, _l, _h, _a, _kv, _v, _c in (("0.5B", 0.49, 24, 896, 14, 2, 151936, 32768),
                                          ("1.5B", 1.54, 28, 1536, 12, 2, 151936, 32768),
                                          ("3B", 3.09, 36, 2048, 16, 2, 151936, 32768),
                                          ("7B", 7.61, 28, 3584, 28, 4, 152064, 131072),
                                          ("14B", 14.7, 48, 5120, 40, 8, 152064, 131072),
                                          ("32B", 32.5, 64, 5120, 40, 8, 152064, 131072),
                                          ("72B", 72.7, 80, 8192, 64, 8, 152064, 131072)):
    _m(f"Qwen/Qwen2.5-{_size}", "qwen", "2.5", _size.lower(), "decoder", _p, layers=_l, hidden=_h, heads=_a, kv=_kv,
       head_dim=_h // _a, vocab=_v, ctx=_c, paper=QWEN25, aliases=[f"Qwen/Qwen2.5-{_size}-Instruct"])

# ---------------------------------------------------------------- Gemma 2
_m("google/gemma-2-2b", "gemma", "2", "2b", "decoder", 2.61, layers=26, hidden=2304, heads=8, kv=4, head_dim=256,
   vocab=256000, ctx=8192, paper=GEMMA2, aliases=["google/gemma-2-2b-it"])
_m("google/gemma-2-9b", "gemma", "2", "9b", "decoder", 9.24, layers=42, hidden=3584, heads=16, kv=8, head_dim=256,
   vocab=256000, ctx=8192, paper=GEMMA2, aliases=["google/gemma-2-9b-it"])
_m("google/gemma-2-27b", "gemma", "2", "27b", "decoder", 27.2, layers=46, hidden=4608, heads=32, kv=16, head_dim=128,
   vocab=256000, ctx=8192, paper=GEMMA2, aliases=["google/gemma-2-27b-it"])

# ---------------------------------------------------------------- Phi-3
_m("microsoft/Phi-3-mini-4k-instruct", "phi", "3", "mini", "decoder", 3.82, layers=32, hidden=3072, heads=32, kv=32,
   head_dim=96, vocab=32064, ctx=4096, paper=PHI3, aliases=["phi-3-mini"])
_m("microsoft/Phi-3-medium-4k-instruct", "phi", "3", "medium", "decoder", 14.0, layers=40, hidden=5120, heads=40,
   kv=10, head_dim=128, vocab=32064, ctx=4096, paper=PHI3, aliases=["phi-3-medium"])

# ---------------------------------------------------------------- GPT-2, BERT
_GPT2 = "https://cdn.openai.com/better-language-models/language_models_are_unsupervised_multitask_learners.pdf"
_m("openai-community/gpt2", "gpt2", None, "124m", "decoder", 0.124, layers=12, hidden=768, heads=12, kv=12,
   head_dim=64, vocab=50257, ctx=1024, paper=_GPT2, aliases=["gpt2"])
_m("openai-community/gpt2-xl", "gpt2", None, "1.5b", "decoder", 1.56, layers=48, hidden=1600, heads=25, kv=25,
   head_dim=64, vocab=50257, ctx=1024, paper=_GPT2, aliases=["gpt2-xl"])
_BERT = "https://arxiv.org/abs/1810.04805"
_m("google-bert/bert-base-uncased", "bert", None, "base", "encoder", 0.110, layers=12, hidden=768, heads=12, kv=12,
   head_dim=64, vocab=30522, ctx=512, paper=_BERT, aliases=["bert-base-uncased", "bert-base-cased", "bert-base"])
_m("google-bert/bert-large-uncased", "bert", None, "large", "encoder", 0.340, layers=24, hidden=1024, heads=16, kv=16,
   head_dim=64, vocab=30522, ctx=512, paper=_BERT, aliases=["bert-large-uncased", "bert-large-cased", "bert-large"])

# ---------------------------------------------------------------- vision / diffusion
_m("microsoft/resnet-50", "resnet", None, "50", "cnn", 0.025557, paper="https://arxiv.org/abs/1512.03385",
   source="https://pytorch.org/vision/stable/models/generated/torchvision.models.resnet50.html",
   aliases=["resnet50", "resnet-50", "torchvision/resnet50", "timm/resnet50.a1_in1k"],
   input_resolution=224, activation_elements_per_sample=27_948_032, activation_elements_per_sample_high=37_556_736,
   note="25,557,032 parameters (torchvision); activation elements computed from the architecture at 224x224")
# OpenGrid's allowlisted job template (workloads/templates.py, runner/mnist_train.py build_model): the classic PyTorch
# MNIST example network. Parameters: conv1 320 + conv2 18,496 + fc1 1,179,776 + fc2 1,290 = 1,199,882. Activation
# elements per 28x28 sample, computed from the layer shapes: conv1 + ReLU 2 x 21,632, conv2 + ReLU 2 x 36,864,
# max-pool 9,216, dropout 9,216, fc1 + ReLU + dropout 3 x 128, fc2 10 = 135,818; the _high figure adds the input
# (784), the max-pool indices (9,216) and the dropout masks (9,344). Architectural counts, not measurements.
_m("opengrid/pytorch-mnist-cnn", "mnist-cnn", None, "small", "cnn", 0.001199882,
   source="https://github.com/pytorch/examples/blob/main/mnist/main.py",
   aliases=["pytorch-mnist", "mnist-cnn", "pytorch mnist"],
   input_resolution=28, activation_elements_per_sample=135_818, activation_elements_per_sample_high=155_162,
   note="the network OpenGrid's pytorch-mnist template trains (2 conv + 2 fully-connected layers)")
_m("google/vit-base-patch16-224", "vit", None, "base", "vit", 0.0866, layers=12, hidden=768, heads=12, kv=12,
   head_dim=64, ctx=197, paper="https://arxiv.org/abs/2010.11929", aliases=["vit-base", "vit-b/16", "vit_b_16"],
   input_resolution=224, note="sequence = 196 patches of 16x16 at 224x224 + 1 class token = 197")
_m("stabilityai/stable-diffusion-xl-base-1.0", "sdxl", "1.0", "base", "diffusion", 3.5,
   paper="https://arxiv.org/abs/2307.01952",
   source="https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0",
   aliases=["sdxl", "stable-diffusion-xl", "sdxl-base"],
   note="2.6B UNet + 0.817B text encoders (paper) + VAE, about 3.5B total; activation memory not modelled")

# ================================================================ audio, image + text, video (intelligence/media.py)
RAW = "https://huggingface.co/{}/raw/main/config.json"

# ---------------------------------------------------------------- Whisper (speech recognition, encoder-decoder)
# Dimensions: each checkpoint's config.json. Parameters: Radford et al. 2022, Table 1 (39M ... 1550M); large-v3 and
# large-v3-turbo from the large-v3-turbo model card (1550M / 809M; turbo cuts the decoder from 32 layers to 4).
# Audio (paper section 2.2): 16 kHz, 30-second segments, log-mel on 25 ms windows with a 10 ms stride (3000 frames),
# a conv stem whose second layer has stride 2 -> 1500 encoder positions per window (max_source_positions 1500);
# the decoder holds up to 448 tokens (max_target_positions).
WHISPER = "https://arxiv.org/abs/2212.04356"
for _size, _p, _d, _le, _ld, _a, _ffn, _mel, _v, _ver in (
        ("tiny", 0.039, 384, 4, 4, 6, 1536, 80, 51865, None), ("base", 0.074, 512, 6, 6, 8, 2048, 80, 51865, None),
        ("small", 0.244, 768, 12, 12, 12, 3072, 80, 51865, None),
        ("medium", 0.769, 1024, 24, 24, 16, 4096, 80, 51865, None),
        ("large-v3", 1.55, 1280, 32, 32, 20, 5120, 128, 51866, "3"),
        ("large-v3-turbo", 0.809, 1280, 32, 4, 20, 5120, 128, 51866, "3")):
    _id = f"openai/whisper-{_size}"
    _m(_id, "whisper", _ver, "turbo" if _size.endswith("turbo") else _size.split("-")[0], "asr", _p, hidden=_d,
       heads=_a, kv=_a, head_dim=_d // _a, vocab=_v, ctx=448, paper=WHISPER, source=RAW.format(_id),
       aliases=[f"whisper-{_size}"] + ([f"openai/whisper-{_size}.en"] if _ver is None else [])
       + (["whisper-turbo", "openai/whisper-turbo", "large-v3-turbo"] if "turbo" in _size else []),
       modality=["audio"], task="transcription",
       media={"encoder_layers": _le, "decoder_layers": _ld, "d_model": _d, "heads": _a, "ffn_dim": _ffn,
              "mel_bins": _mel, "sample_rate": 16000, "window_seconds": 30, "encoder_positions": 1500,
              "max_target_positions": 448,
              "card": "https://huggingface.co/openai/whisper-large-v3-turbo" if _size.startswith("large") else None},
       note=f"{_le}-layer encoder + {_ld}-layer decoder, d_model {_d}; 30 s windows of {_mel}-bin log-mel")

# ---------------------------------------------------------------- wav2vec2 (speech encoder, CTC)
W2V = "https://arxiv.org/abs/2006.11477"
for _id, _size, _p, _l, _h, _a, _ffn in (("facebook/wav2vec2-base-960h", "base", 0.095, 12, 768, 12, 3072),
                                        ("facebook/wav2vec2-large-960h-lv60-self", "large", 0.317, 24, 1024, 16, 4096)):
    _m(_id, "wav2vec2", None, _size, "audio_encoder", _p, layers=_l, hidden=_h, heads=_a, kv=_a, head_dim=_h // _a,
       vocab=32, paper=W2V, source=RAW.format(_id), aliases=[f"wav2vec2-{_size}", f"wav2vec2-{_size}-960h"],
       modality=["audio"], task="transcription",
       media={"sample_rate": 16000, "conv_stride_product": 320, "frames_per_second": 50, "ffn_dim": _ffn},
       note="conv feature encoder strides (5,2,2,2,2,2,2) = 320 samples per frame: 50 frames per second at 16 kHz "
            "(about 20 ms)")

# ---------------------------------------------------------------- text-to-speech
# Parler-TTS: decoder dims and codec from config.json; the DAC codec runs at 86 frames per second with 9 codebooks
# (codec config.json); 44.1 kHz audio. Parameters: large-v1 model card ("2.2B-parameters"); mini-v1: 0.9B is the
# Hugging Face safetensors total (rounded to 0.1B by Hugging Face; the card states no count).
for _id, _size, _p, _l, _h, _a, _ffn, _te in (("parler-tts/parler-tts-mini-v1", "mini", 0.9, 24, 1024, 16, 4096,
                                               "google/flan-t5-large"),
                                              ("parler-tts/parler-tts-large-v1", "large", 2.2, 25, 1536, 24, 6144,
                                               "google/flan-t5-xl")):
    _m(_id, "parler-tts", "1", _size, "tts", _p, layers=_l, hidden=_h, heads=_a, kv=_a, head_dim=_h // _a,
       vocab=1088, ctx=4096, source=f"https://huggingface.co/{_id}", aliases=[f"parler-tts-{_size}-v1", f"parler-{_size}"],
       modality=["audio", "text"], task="speech_synthesis",
       media={"autoregressive": True, "frame_rate": 86, "num_codebooks": 9, "sample_rate": 44100, "text_encoder": _te,
              "ffn_dim": _ffn, "config": RAW.format(_id),
              "codec_config": "https://huggingface.co/parler-tts/dac_44khZ_8kbps/raw/main/config.json"},
       note=f"{_l}-layer decoder over DAC codec frames (86 per second of audio, 9 codebooks predicted per step); "
            f"text encoder {_te}")
_m("hexgrad/Kokoro-82M", "kokoro", None, "82m", "tts", 0.082, source="https://huggingface.co/hexgrad/Kokoro-82M",
   aliases=["kokoro", "kokoro-82m", "kokoro-tts"], modality=["audio", "text"], task="speech_synthesis",
   media={"autoregressive": False, "sample_rate": 24000},
   note="82 million parameters (model card); not autoregressive: no KV cache; 24 kHz output (the card's code)")

# ---------------------------------------------------------------- vision-language models (image + text, video)
# LLaVA-1.5 (Liu et al. 2023): CLIP ViT-L/14 at 336 px -> 24 x 24 = 576 image tokens (vision_feature_select_strategy
# "default" drops the class token). Text dims of the 7B from its base lmsys/vicuna-7b-v1.5 config (the llava config
# leaves them to the defaults); parameter totals are the Hugging Face safetensors totals.
LLAVA15 = "https://arxiv.org/abs/2310.03744"
LLAVA_NEXT = "https://llava-vl.github.io/blog/2024-01-30-llava-next/"
_CLIP336 = {"encoder": "CLIP ViT-L/14-336", "image_size": 336, "patch_size": 14, "vision_hidden": 1024,
            "vision_layers": 24, "vision_params_b": 0.302}
for _size, _p, _l, _h, _a in (("7b", 7.063427072, 32, 4096, 32), ("13b", 13.351494656, 40, 5120, 40)):
    _id = f"llava-hf/llava-1.5-{_size}-hf"
    _m(_id, "llava", "1.5", _size, "vlm", _p, layers=_l, hidden=_h, heads=_a, kv=_a, head_dim=128, vocab=32064,
       ctx=4096, paper=LLAVA15, source=RAW.format(_id), aliases=[f"liuhaotian/llava-v1.5-{_size}", f"llava-1.5-{_size}"],
       modality=["image", "text"], task="understanding",
       media={**_CLIP336, "rule": "fixed", "tokens_per_image": 576, "derived": ["vision_params_b"]},
       note="CLIP ViT-L/14-336 vision tower + 2-layer MLP projector + Vicuna; 576 tokens per image")
# LLaVA-1.6 (LLaVA-NeXT) AnyRes: the image is resized to the best-fitting grid in image_grid_pinpoints and split into
# 336 x 336 tiles, plus one downscaled base image; each tile and the base give 576 tokens (unpadding then removes the
# padded rows, newline tokens add one per row: the count below is an upper bound; the blog states no token count).
_PIN = [[336, 672], [672, 336], [672, 672], [1008, 336], [336, 1008]]
for _id, _size, _p, _l, _h, _a, _kv, _c, _base in (
        ("llava-hf/llava-v1.6-vicuna-7b-hf", "7b", 7.063431168, 32, 4096, 32, 32, 4096, "vicuna"),
        ("llava-hf/llava-v1.6-vicuna-13b-hf", "13b", 13.351499776, 40, 5120, 40, 40, 4096, "vicuna"),
        ("llava-hf/llava-v1.6-mistral-7b-hf", "7b", 7.566747648, 32, 4096, 32, 8, 32768, "mistral")):
    _m(_id, "llava", "1.6", _size, "vlm", _p, layers=_l, hidden=_h, heads=_a, kv=_kv, head_dim=128, vocab=32064,
       ctx=_c, paper=LLAVA_NEXT, source=RAW.format(_id),
       aliases=[f"liuhaotian/llava-v1.6-{_base}-{_size}", f"llava-v1.6-{_base}-{_size}", f"llava-next-{_base}-{_size}"],
       modality=["image", "text"], task="understanding",
       media={**_CLIP336, "rule": "anyres", "tokens_per_tile": 576, "tile_size": 336, "grid_pinpoints": _PIN,
              "derived": ["vision_params_b"]},
       note="AnyRes: up to 4 tiles of 336 x 336 + 1 base image, 576 tokens each (at most 2,880 per image)")
# Qwen2-VL (Wang et al. 2024): a 675M-parameter ViT (patch 14) shared by every size; 2 x 2 adjacent patches merge
# into one token, so one token per 28 x 28 pixels at the image's own resolution (qwen_vl_utils smart_resize: sides
# rounded to multiples of 28, total pixels kept within min_pixels 3,136 and max_pixels 12,845,056 = 16,384 tokens).
# Video: 2 frames per second by default, two consecutive frames merged (temporal patch 2); per-frame pixels within
# 128-768 x 28 x 28 (VIDEO_MIN/MAX_PIXELS) and the whole video within 24,576 x 28 x 28 (qwen_vl_utils 0.0.8).
QWEN2VL = "https://arxiv.org/abs/2409.12191"
for _size, _p, _l, _h, _a, _kv, _v in (("2B", 2.2089856, 28, 1536, 12, 2, 151936),
                                       ("7B", 8.291375616, 28, 3584, 28, 4, 152064),
                                       ("72B", 73.40556032, 80, 8192, 64, 8, 152064)):
    _id = f"Qwen/Qwen2-VL-{_size}-Instruct"
    _m(_id, "qwen2-vl", "2", _size.lower(), "vlm", _p, layers=_l, hidden=_h, heads=_a, kv=_kv, head_dim=_h // _a,
       vocab=_v, ctx=32768, paper=QWEN2VL, source=RAW.format(_id), aliases=[f"Qwen/Qwen2-VL-{_size}", f"qwen2-vl-{_size}"],
       modality=["image", "video", "text"], task="understanding",
       media={"encoder": "ViT (675M, patch 14, 2x2 merge)", "patch_size": 14, "merge": 2, "temporal_patch": 2,
              "vision_hidden": 1280, "vision_layers": 32, "vision_params_b": 0.675, "rule": "dynamic",
              "pixels_per_token": 784, "min_pixels": 3136, "max_pixels": 12845056, "video_fps": 2.0,
              "video_min_pixels": 128 * 784, "video_max_pixels": 768 * 784, "video_total_pixels": 24576 * 784,
              "frame_factor": 2, "max_frames": 768,
              "utils": "https://pypi.org/project/qwen-vl-utils/0.0.8/"},
       note="dynamic resolution: one token per 28 x 28 pixels; video at 2 frames/s, 2 frames per token group")
# Llama 3.2 Vision: images enter through CROSS-attention layers (cross_attention_layers), not the text KV cache. Each
# image is split into up to max_num_tiles 4 tiles of 560 x 560; patch 14 -> 40 x 40 = 1,600 patches + 1 class token
# = 1,601 vision tokens per tile. Configs from the public unsloth Instruct mirrors (the meta-llama repos are gated);
# parameter totals from the Hugging Face safetensors totals of the meta-llama repos.
for _size, _p, _l, _h, _a, _cross in (("11B", 10.642941475, 40, 4096, 32, 8), ("90B", 88.593355323, 100, 8192, 64, 20)):
    _id = f"meta-llama/Llama-3.2-{_size}-Vision"
    _m(_id, "mllama", "3.2", _size.lower(), "vlm", _p, layers=_l, hidden=_h, heads=_a, kv=8, head_dim=128,
       vocab=128256, ctx=131072, paper=LLAMA3,
       source=f"https://huggingface.co/unsloth/Llama-3.2-{_size}-Vision-Instruct/raw/main/config.json",
       aliases=[f"meta-llama/Llama-3.2-{_size}-Vision-Instruct", f"llama-3.2-{_size.lower()}-vision"],
       modality=["image", "text"], task="understanding",
       media={"encoder": "ViT-H/14 (32 + 8 global layers, hidden 1280)", "image_size": 560, "patch_size": 14,
              "vision_hidden": 1280, "vision_layers": 40, "rule": "tiles", "max_tiles": 4, "tokens_per_tile": 1601,
              "cross_attention": True, "cross_attention_layers": _cross, "derived": ["tokens_per_tile"]},
       note=f"{_cross} of the {_l} text layers are cross-attention layers over up to 4 x 1,601 vision tokens per image")
# PaliGemma (Beyer et al. 2024): SigLIP-So400m (400M) + Gemma 2B; 256 / 1,024 / 4,096 image tokens at 224 / 448 / 896.
PALIGEMMA = "https://arxiv.org/abs/2407.07726"
for _res, _p, _t in ((224, 2.92346648, 256), (448, 2.924351216, 1024), (896, 2.92789016, 4096)):
    _id = f"google/paligemma-3b-pt-{_res}"
    _m(_id, "paligemma", str(_res), "3b", "vlm", _p, layers=18, hidden=2048, heads=8, kv=1, head_dim=256,
       vocab=257216, paper=PALIGEMMA, source=f"https://huggingface.co/{_id}",
       aliases=[f"google/paligemma-3b-mix-{_res}", f"paligemma-3b-{_res}"], modality=["image", "text"],
       task="understanding",
       media={"encoder": "SigLIP-So400m/14", "image_size": _res, "patch_size": 14, "vision_hidden": 1152,
              "vision_layers": 27, "vision_params_b": 0.4, "rule": "fixed", "tokens_per_image": _t},
       note=f"{_t} image tokens at {_res} x {_res}")

# ---------------------------------------------------------------- text-to-video diffusion
# Components (weights): transformer, text encoder, VAE, each with its parameter count (or bytes on disk when only
# that is published). Latent video: VAE compression 4x in time and 8x in space, 16 channels, then 2 x 2 patches:
# tokens = latent frames x (H/16) x (W/16); latent frames = (frames - 1) / 4 + 1. Frames = seconds x fps + 1
# (CogVideoX 6 s x 8 fps + 1 = 49; Wan2.1 5 s x 16 fps + 1 = 81). `publisher` = the publisher's own memory / time
# figures, used only to cross-check the estimate, never as an OpenGrid measurement.
COGVIDEOX = "https://arxiv.org/abs/2408.06072"
_COG_TE = {"name": "T5 v1.1 XXL encoder", "params_b": 4.762310656}
_COG_VAE = {"params_b": 0.215583907, "top_channels": 128}
for _size, _p_dit, _l, _heads, _dt, _pub in (
        ("2b", 1.693783872, 30, 30, "fp16", {"vram_gb": 18, "condition": "SAT, FP16, 720x480, 49 frames",
                                             "seconds": {"A100": 90, "H100": 45}}),
        ("5b", 5.570283072, 42, 48, "bf16", {"vram_gb": 26, "condition": "SAT, BF16, 720x480, 49 frames",
                                             "seconds": {"A100": 180, "H100": 90}})):
    _id = f"THUDM/CogVideoX-{_size}"
    _h = _heads * 64
    _m(_id, "cogvideox", None, _size, "video_diffusion", round(_p_dit + _COG_TE["params_b"] + _COG_VAE["params_b"], 6),
       layers=_l, hidden=_h, heads=_heads, head_dim=64, paper=COGVIDEOX,
       source=f"https://huggingface.co/{_id}/raw/main/transformer/config.json",
       aliases=[f"zai-org/CogVideoX-{_size}", f"cogvideox-{_size}"], modality=["video", "text"], task="generation",
       media={"transformer_params_b": _p_dit, "text_encoder": _COG_TE, "vae": _COG_VAE, "ffn_dim": 4 * _h,
              "joint_text_tokens": 226, "cross_text_tokens": 0, "latent_channels": 16, "vae_temporal": 4,
              "vae_spatial": 8, "patch": 2, "native_resolution": "720x480", "native_frames": 49, "native_fps": 8,
              "fixed_resolution": True, "steps": 50, "cfg_forward_passes": 2, "cfg_batched": True,
              "dtype": _dt, "card": f"https://huggingface.co/{_id}", "publisher": _pub,
              "derived": ["ffn_dim (diffusers FeedForward default 4 x hidden)", "parameter counts summed from the "
                          "safetensors headers"]},
       note=f"{_l}-block expert transformer (hidden {_h}) with full attention over 226 text + video tokens; 720x480, "
            "6 s at 8 fps; classifier-free guidance batches 2 forward passes")
WAN = "https://arxiv.org/abs/2503.20314"
_WAN_TE = {"name": "umT5-XXL encoder (bf16)", "params_b": round(11_361_920_418 / 2 / 1e9, 6)}
_WAN_VAE = {"bytes": 507_609_880, "top_channels": 96}
for _size, _p_dit, _l, _h, _heads, _ffn, _res, _pub in (
        ("1.3B", 1.4189968, 30, 1536, 12, 8960, "832x480",
         {"vram_gb": 8.19, "condition": "RTX 4090, 480p, 81 frames, --offload_model True --t5_cpu",
          "seconds": {"RTX 4090": 261.4}}),
        ("14B", 14.288491584, 40, 5120, 40, 13824, "1280x720",
         {"vram_gb": 69.1, "condition": "720p, 81 frames, --offload_model True",
          "seconds": {"A100": 3342.6, "H100": 1837.9}})):
    _id = f"Wan-AI/Wan2.1-T2V-{_size}"
    _m(_id, "wan", "2.1", _size.lower(), "video_diffusion",
       round(_p_dit + _WAN_TE["params_b"] + _WAN_VAE["bytes"] / 4 / 1e9, 6), layers=_l, hidden=_h, heads=_heads,
       head_dim=_h // _heads, paper=WAN, source=RAW.format(_id),
       aliases=[f"Wan-AI/Wan2.1-T2V-{_size}-Diffusers", f"wan2.1-t2v-{_size.lower()}", f"wan2.1-{_size.lower()}"],
       modality=["video", "text"], task="generation",
       media={"transformer_params_b": _p_dit, "text_encoder": _WAN_TE, "vae": _WAN_VAE, "ffn_dim": _ffn,
              "joint_text_tokens": 0, "cross_text_tokens": 512, "latent_channels": 16, "vae_temporal": 4,
              "vae_spatial": 8, "patch": 2, "native_resolution": _res, "native_frames": 81, "native_fps": 16,
              "fixed_resolution": False, "steps": 50, "cfg_forward_passes": 2, "cfg_batched": False, "dtype": "bf16",
              "card": "https://github.com/Wan-Video/Wan2.1", "publisher": _pub,
              "derived": ["text encoder parameters = its bf16 checkpoint bytes / 2",
                          "VAE parameters = its checkpoint bytes / 4 (fp32 assumed); its weights count the file size"]},
       note=f"{_l}-block DiT (hidden {_h}, FFN {_ffn}) with cross-attention to 512 umT5 tokens; 81 frames at 16 fps; "
            "classifier-free guidance runs 2 forward passes in sequence")
_m("tencent/HunyuanVideo", "hunyuanvideo", None, "13b", "video_diffusion", round(13.0 + 8.03 + 0.123, 3), hidden=3072,
   heads=24, head_dim=128, layers=60, paper="https://arxiv.org/abs/2412.03603",
   source="https://github.com/Tencent/HunyuanVideo", aliases=["hunyuanvideo", "hunyuan-video", "HunyuanVideo"],
   modality=["video", "text"], task="generation",
   media={"transformer_params_b": 13.0, "text_encoder": {"name": "llava-llama-3-8b (language model) + CLIP-L",
                                                         "params_b": 8.153},
          "vae": {"params_b": None, "top_channels": 128}, "ffn_dim": 4 * 3072, "joint_text_tokens": 256,
          "cross_text_tokens": 0, "latent_channels": 16, "vae_temporal": 4, "vae_spatial": 8, "patch": 2,
          "native_resolution": "1280x720", "native_frames": 129, "native_fps": None, "fixed_resolution": False,
          "steps": 50, "cfg_forward_passes": 1, "cfg_batched": False, "dtype": "bf16",
          "card": "https://github.com/Tencent/HunyuanVideo",
          "publisher": {"vram_gb": 60, "condition": "1280x720, 129 frames, batch 1 (544x960: 45 GB); 80 GB GPU "
                                                    "recommended", "seconds": {}},
          "derived": ["transformer 13.0B: the README says 'over 13 billion' (a lower bound)",
                      "text encoders 8.153B: Llama-3-8B language model (8.03B) + CLIP-L text tower (about 0.123B, "
                      "from its dimensions)", "VAE size not published: not counted", "top VAE channels 128 assumed"]},
   note="20 dual-stream + 40 single-stream blocks (hidden 3072, 24 heads), full attention over video + 256 text "
        "tokens; guidance distilled into the model (one forward pass per step)")

MEDIA_KINDS = ("vlm", "asr", "audio_encoder", "tts", "video_diffusion")

_FAMILY_WORDS = {
    "llama": "llama", "mistral": "mistral", "mixtral": "mixtral", "qwen": "qwen", "gemma": "gemma", "phi": "phi",
    "gpt2": "gpt2", "gpt-2": "gpt2", "bert": "bert", "resnet": "resnet", "vit": "vit", "sdxl": "sdxl",
    "whisper": "whisper", "wav2vec": "wav2vec2", "parler": "parler-tts", "kokoro": "kokoro", "llava": "llava",
    "qwen2-vl": "qwen2-vl", "qwen2 vl": "qwen2-vl", "qwen-vl": "qwen2-vl", "paligemma": "paligemma",
    "mllama": "mllama", "cogvideo": "cogvideox", "wan2": "wan", "wan-ai": "wan", "wan 2": "wan",
    "hunyuan": "hunyuanvideo",
}
# a more specific family wins over the generic word it contains ("paligemma" is not Gemma, "llava ... mistral" is
# LLaVA, "Llama 3.2 Vision" is the vision model)
_SUPERSEDES = {"mixtral": ("mistral",), "qwen2-vl": ("qwen",), "paligemma": ("gemma",), "llava": ("mistral", "llama"),
               "mllama": ("llama",)}
_MEDIA_SIZE_WORDS = ("turbo", "tiny", "small", "mini", "medium", "base", "large", "xl")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def get(model_id: str) -> dict | None:
    want = _norm(model_id)
    for e in _CATALOG:
        if want == e["id"].lower() or want in (a.lower() for a in e["aliases"]):
            return _copy(e)
    return None


def all_models() -> list[dict]:
    return [_copy(e) for e in _CATALOG]


def _copy(e: dict) -> dict:
    out = {**e, "aliases": list(e["aliases"])}
    if isinstance(e.get("media"), dict):
        import copy

        out["media"] = copy.deepcopy(e["media"])
    return out


_SIZE = re.compile(r"(?<![\w.])(\d+x)?(\d+(?:\.\d+)?)\s*([bm])(?![a-z])")


def lookup(name: str | None) -> dict | None:
    """An exact HF id / alias match, else a loose one ("7B llama", "llama 3.1 70b", "qwen2.5 7b instruct").

    Loose matching needs the family word and, when the text has one, the size; a version number in the text
    must match too. Several survivors: the newest version wins and every candidate is listed (match "loose").
    """
    if not name or not name.strip():
        return None
    hit = get(name)
    if hit is not None:
        return {"entry": hit, "match": "exact", "candidates": [hit["id"]]}
    text = _norm(name)
    tail = text.rsplit("/", 1)[-1]
    families = {fam for word, fam in _FAMILY_WORDS.items() if word in text}
    if "llama" in families and re.search(r"\bvision\b", text):
        families.add("mllama")
    for fam, generic in _SUPERSEDES.items():
        if fam in families:
            families.difference_update(generic)
    if not families:
        return None
    media_family = any(e["family"] in families and e["kind"] in MEDIA_KINDS for e in _CATALOG)
    size = None
    flat = tail.replace("-", " ").replace("_", " ")
    m = _SIZE.search(flat)
    if m:
        size = f"{m.group(1) or ''}{m.group(2)}{m.group(3)}"
        flat = flat[:m.start()] + " " + flat[m.end():]
    else:
        for word in (_MEDIA_SIZE_WORDS if media_family else ("mini", "medium", "base", "large", "xl")):
            if re.search(rf"\b{word}\b", flat):
                size = word
                break
    versions = set(re.findall(r"(?<![\d.])(\d+(?:\.\d+)?)(?![\d.])", flat))
    pool = []
    for e in _CATALOG:
        if e["family"] not in families:
            continue
        if size and e["size"] != size:
            continue
        if versions and e["version"] and not any(v == e["version"] or e["version"].startswith(v + ".")
                                                 for v in versions):
            continue
        pool.append(e)
    if not pool:
        return None

    def key(e):
        try:
            v = tuple(int(x) for x in (e["version"] or "0").split("."))
        except ValueError:
            v = (0,)
        return (e["version"] in versions, v)

    pool.sort(key=key, reverse=True)
    return {"entry": _copy(pool[0]), "match": "loose", "candidates": [e["id"] for e in pool]}
