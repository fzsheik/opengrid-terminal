"""Audio / image / video detection in the local project analyzer (opengrid_cli.analyze): fixture projects under
fixtures/projects/ (whisper_batch, vlm_caption, video_generation, vllm_vision, asr_pipeline, mllama_config).

What is checked: the model, workload type and requirements.media (modality, task, images per request) are inferred
from code and configs (faster-whisper / openai-whisper loaders, transformers model classes and pipeline tasks,
diffusers video pipelines, vLLM --limit-mm-per-prompt, config.json model_type); projects without media keep the
exact requirements shape they had (no `media` key); only inferred values leave the machine (prompts, file contents
and secrets never appear in the output); the requirements validate against the CLI contract mirror.

Plain script: python cli/tests/test_analyze_media.py  -> "ok ..." lines and "all passed".
"""

from __future__ import annotations

import json
import pathlib
import shutil
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from opengrid_cli import workload  # noqa: E402
from opengrid_cli.analyze import analyze, facts  # noqa: E402

FIX = HERE / "fixtures" / "projects"


def ok(msg: str) -> None:
    print(f"ok {msg}")


def eq(actual, expected, what: str) -> None:
    assert actual == expected, f"{what}: expected {expected!r}, got {actual!r}"


def run(name: str) -> dict:
    out = analyze(FIX / name)
    workload.validate(out["requirements"])          # the contract mirror accepts what the analyzer emits
    return out


def ev(out: dict, field: str) -> dict:
    return next(e for e in out["requirements"]["evidence"] if e["field"] == field)


def test_whisper_batch() -> None:
    out = run("whisper_batch")
    r = out["requirements"]
    eq(r["model"]["name"], "openai/whisper-large-v3", "faster-whisper WhisperModel('large-v3') -> HF id")
    eq(r["workload_type"], "batch", "offline transcription script named *batch*")
    eq(r["media"], {"modality": ["audio"], "task": "transcription"}, "media")
    eq(r["frameworks"], ["faster-whisper"], "framework from requirements.txt")
    assert ev(out, "media.modality")["source"].startswith("transcribe_batch.py:"), ev(out, "media.modality")
    qs = [q["field"] for q in out["questions"]]
    assert "media.total_audio_hours" in qs and "model.parameters_b" not in qs, qs
    assert "inference.context_len" not in qs, "a text context length means nothing for Whisper"
    ok("faster-whisper batch transcription: model, batch workload, audio/transcription, audio-hours question")


def test_vlm_caption() -> None:
    out = run("vlm_caption")
    r = out["requirements"]
    eq(r["model"]["name"], "Qwen/Qwen2-VL-7B-Instruct", "Qwen2VLForConditionalGeneration.from_pretrained")
    eq(r["workload_type"], "inference", "generate() call")
    eq(r["media"]["modality"], ["image"], "image modality from the model class")
    assert "qwen-vl-utils" in r["frameworks"]
    assert "media.images_per_request" in [q["field"] for q in out["questions"]]
    ok("Qwen2-VL captioning: model class -> image modality, images-per-request question")


def test_video_generation() -> None:
    out = run("video_generation")
    r = out["requirements"]
    eq(r["model"]["name"], "THUDM/CogVideoX-5b", "CogVideoXPipeline.from_pretrained")
    eq(r["workload_type"], "inference", "a diffusers video pipeline is generation (inference)")
    eq(r["media"], {"modality": ["video"], "task": "generation"}, "media")
    assert "media.output_resolution" in [q["field"] for q in out["questions"]]
    ok("CogVideoX pipeline: video generation detected")


def test_vllm_vision() -> None:
    out = run("vllm_vision")
    r = out["requirements"]
    eq(r["model"]["name"], "llava-hf/llava-1.5-7b-hf", "vllm serve <model>")
    eq(r["inference"]["engine"], "vllm", "engine")
    eq(r["media"], {"modality": ["image"], "task": "understanding", "images_per_request": 4},
       "--limit-mm-per-prompt image=4")
    ok("vllm serve --limit-mm-per-prompt image=4 -> images per request")


def test_asr_pipeline_and_mllama_config() -> None:
    r = run("asr_pipeline")["requirements"]
    eq((r["model"]["name"], r["media"]["task"]), ("openai/whisper-large-v3-turbo", "transcription"),
       "pipeline('automatic-speech-recognition')")
    out = run("mllama_config")
    r = out["requirements"]
    eq((r["model"]["name"], r["model"]["architecture"], r["media"]["modality"]),
       ("meta-llama/Llama-3.2-11B-Vision-Instruct", "mllama", ["image"]), "config.json model_type mllama")
    eq(r["model"]["num_layers"], 40, "text_config dims")
    ok("transformers ASR pipeline task and an mllama config.json")


def test_unchanged_without_media() -> None:
    for name in ("vllm_inference", "llama7b_lora", "small_pytorch_training", "non_gpu_app", "full_finetune_70b"):
        r = analyze(FIX / name)["requirements"]
        assert "media" not in r, (name, r.get("media"))
    ok("projects without audio / image / video: no media key (requirements shape unchanged)")


def test_only_inferred_values_leave() -> None:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="og-media-"))
    try:
        for name in ("whisper_batch", "video_generation"):
            shutil.copytree(FIX / name, tmp / name)
        token = "hf_" + "Q" * 6 + "zX9" + "kP2mN7" * 4
        p = tmp / "whisper_batch" / "transcribe_batch.py"
        p.write_text(p.read_text() + f'\nHF_TOKEN = "{token}"\n')
        for name, private in (("whisper_batch", [token, "*.wav", "beam_size"]),
                              ("video_generation", ["a panda playing guitar", "out.mp4"])):
            out = analyze(tmp / name)
            blob = json.dumps(out)
            for x in private:
                assert x not in blob, (name, x)
            sent = workload.validate(out["requirements"])
            assert set(sent) <= set(workload.WorkloadRequirements.model_fields), sorted(sent)
            for x in private:
                assert x not in json.dumps(sent), (name, x)
        assert any("secret-looking" in w for w in analyze(tmp / "whisper_batch")["warnings"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    ok("redaction: prompts, code and a planted token never appear in the output or what is sent")


def test_vocabulary() -> None:
    eq(facts.whisper_model_id("large-v3"), "openai/whisper-large-v3", "size")
    eq(facts.whisper_model_id("turbo"), "openai/whisper-large-v3-turbo", "turbo alias")
    eq(facts.whisper_model_id("base.en"), "openai/whisper-base.en", ".en")
    eq(facts.whisper_model_id("my-model"), None, "not a size")
    eq(facts.media_of_name("Wan-AI/Wan2.1-T2V-14B"), ("video", "generation"), "wan")
    eq(facts.media_of_name("lmms-lab/LLaVA-Video-7B-Qwen2"), ("video", "understanding"), "llava-video before llava")
    eq(facts.media_of_name("meta-llama/Llama-3.1-8B"), None, "an LLM is not media")
    eq(facts.PACKAGES.get("whisper"), None, "pip 'whisper' is Graphite, not openai-whisper")
    eq(facts.IMPORT_ONLY["whisper"], ("ml", "openai-whisper"), "import whisper is openai-whisper")
    ok("media vocabulary: whisper sizes, model-name patterns, import-only names")


def main() -> None:
    for fn in (test_vocabulary, test_whisper_batch, test_vlm_caption, test_video_generation, test_vllm_vision,
               test_asr_pipeline_and_mllama_config, test_unchanged_without_media, test_only_inferred_values_leave):
        fn()
    print("all passed")


if __name__ == "__main__":
    main()
