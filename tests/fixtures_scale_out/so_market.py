"""Shared by tests/test_scale_out.py and make_golden.py: the golden market (datacenter GPUs only, whose specs this
change does not touch), the golden requests (shapes that existed before scale-out), and the payload normalisation.
Imports only modules that exist at the golden's base commit (tables), so make_golden.py can run against it."""

from datetime import timedelta
from decimal import Decimal

T4, L4, L40S = "NVIDIA T4 16GB", "NVIDIA L4 24GB", "NVIDIA L40S 48GB"
A100, H100 = "NVIDIA A100 80GB SXM4", "NVIDIA H100 80GB SXM5"

GOLDEN_REQUESTS = {
    "lora8b_tokens": {"workload_type": "fine_tuning", "model": {"name": "meta-llama/Llama-3.1-8B"}, "precision": "bf16",
                      "training": {"method": "lora", "micro_batch_size": 1, "seq_len": 2048, "dataset_tokens": 2e8}},
    "vllm8b_serving": {"workload_type": "inference", "model": {"name": "meta-llama/Llama-3.1-8B"},
                       "inference": {"context_len": 8192, "concurrency": 4}},
    "whisper_10k": {"workload_type": "batch", "model": {"name": "openai/whisper-large-v3"}, "precision": "bf16",
                    "media": {"modality": "audio", "task": "transcription", "total_audio_hours": 10000}},
    "llava_batch": {"workload_type": "inference", "model": {"name": "llava-hf/llava-1.5-7b-hf"}, "precision": "bf16",
                    "inference": {"concurrency": 8, "context_len": 2048},
                    "media": {"images_per_request": 2, "total_items": 100000}},
}

VOLATILE = ("as_of", "observed_at", "newest_observed_at")
ADDED = ("scale_out",)          # keys this change adds; stripped before comparing with the golden


def golden_rows(Row, now):
    def L(prov, gpu, price, count=1):
        return Row(provider=prov, listing_id=f"{prov}:{gpu}:{count}", sku=f"{prov}-sku", raw_gpu_name=gpu,
                   canonical_gpu_name=gpu, gpu_count=count, region="us-east", country="US",
                   price_per_gpu_hour=Decimal(str(price)), price_per_instance_hour=Decimal(str(round(price * count, 6))),
                   currency="USD", market_type="on_demand", provider_tier=None, interruptible=False, available=True,
                   capacity=None, capacity_unit=None, vcpu=None, ram_gb=None, storage_gb=None, visibility="public",
                   observed_at=now - timedelta(minutes=1), first_seen_at=now - timedelta(days=3))
    return [L("syn_a", T4, 0.30), L("syn_b", T4, 0.35), L("syn_b", L4, 0.50), L("syn_c", L4, 0.55),
            L("syn_b", L40S, 0.90), L("syn_a", A100, 1.50), L("syn_c", A100, 1.40),
            L("syn_a", H100, 2.50), L("syn_b", H100, 2.40), L("syn_a", H100, 2.45, count=2),
            L("syn_b", H100, 2.30, count=8)]


def normalize(obj):
    """Drop the time-dependent keys and the keys this change adds (recursively)."""
    if isinstance(obj, dict):
        return {k: normalize(v) for k, v in obj.items() if k not in VOLATILE and k not in ADDED}
    if isinstance(obj, list):
        return [normalize(v) for v in obj]
    return obj
