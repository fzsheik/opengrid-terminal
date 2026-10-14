# OpenGrid 2.0 shared contracts

Every interface (website Find Compute, the assistant, the `opengrid` CLI, the MCP server) uses ONE
recommendation engine on the server (`intelligence/`). Frontends never estimate memory or rank GPUs
themselves. This file is the contract they are built against; change it only together with its tests.

```
local project ──(opengrid_cli.analyze, local only, allowlisted files)──► WorkloadRequirements (JSON)
                                                                              │
website form / assistant / CLI flags / MCP tool args ─────────────────────────┤
                                                                              ▼
                        POST /v1/recommend/estimate   ──► Estimate (memory, GPU count, questions)
                        POST /v1/recommend            ──► Recommendation (Estimate + ranked market options)
```

## 1. WorkloadRequirements (request body)

Pydantic model `intelligence.requirements.WorkloadRequirements` (`extra="forbid"`). Every field is
optional: unknown is `null`, never a guess. The analyzer fills what it has evidence for.

```jsonc
{
  "workload_type": "training" | "fine_tuning" | "inference" | "batch" | "no_gpu" | null,
  "frameworks": ["pytorch", "transformers", "peft", "vllm", "tensorflow", "jax", "deepspeed", "accelerate", ...],
  "model": {
    "name": "meta-llama/Llama-3.1-8B" | null,        // HF id or free text
    "architecture": "llama" | null,                  // HF model_type
    "parameters_b": 8.03 | null,                     // billions; total params (MoE: total)
    "active_parameters_b": null,                     // MoE only
    "num_layers": 32, "hidden_size": 4096, "num_attention_heads": 32,
    "num_kv_heads": 8, "head_dim": 128, "vocab_size": 128256,
    "max_context": 131072
  },
  "precision": "fp32" | "bf16" | "fp16" | "fp8" | "int8" | "int4" | null,   // compute / weight dtype
  "quantization": "none" | "int8" | "int4" | "nf4" | "gptq" | "awq" | "fp8" | null,
  "training": {
    "method": "full" | "lora" | "qlora" | null,
    "optimizer": "adamw" | "adamw_8bit" | "adafactor" | "sgd" | null,
    "micro_batch_size": 4, "global_batch_size": null, "seq_len": 2048,
    "gradient_checkpointing": true | null,
    "lora_rank": 16 | null,
    "zero_stage": 0 | 1 | 2 | 3 | null, "cpu_offload": false | null,
    "epochs": null, "dataset_tokens": null, "dataset_samples": null
  },
  "inference": {
    "concurrency": 8 | null, "context_len": 8192 | null, "output_tokens": null,
    "target_latency_ms": null, "target_tokens_per_s": null,
    "engine": "vllm" | "tgi" | "transformers" | "llama.cpp" | null
  },
  "constraints": {
    "budget_usd": 50 | null, "max_price_per_gpu_hour": null, "runtime_hours": null,
    "region": "us" | "eu" | null, "max_gpus": null, "allow_spot": false,
    "vendor": "NVIDIA" | "AMD" | null, "include_providers": [], "exclude_providers": []
  },
  "hardware_hints": { "gpus_mentioned": ["H100"], "gpu_count": null, "cuda_min": "12.1" | null },
  "evidence": [ { "field": "model.parameters_b", "value": 8.03, "source": "config.json:12",
                  "confidence": "high" | "medium" | "low" } ],
  "source": "analyzer" | "form" | "assistant" | "api"
}
```

## 2. Estimate — `POST /v1/recommend/estimate`

Body: WorkloadRequirements. Pure computation (no DB writes, no provider calls). Public (anonymous allowed).

```jsonc
{
  "needs_gpu": true | false | null,           // false only on evidence (no ML framework, no model)
  "needs_gpu_reason": "...",
  "workload_type": "fine_tuning",
  "memory": {
    "method": "training_full" | "training_lora" | "training_qlora" | "inference" | "unavailable",
    "components_gb": { "weights": 16.06, "gradients": 0.08, "optimizer": 0.5, "activations": 6.2,
                       "kv_cache": 0, "framework_overhead": 1.5 },
    "min_total_gb": 24.3,                      // sum of components: theoretical minimum
    "recommended_gb": 29.2,                    // with headroom (documented margin)
    "range_gb": [21.0, 34.0],                  // when inputs are uncertain
    "per_gpu_gb": 29.2,                        // for the suggested gpu_count (sharding applied)
    "confidence": "high" | "medium" | "low" | "none",
    "assumptions": ["micro batch 4 assumed (not found in project)", ...],
    "kind": "estimated"
  },
  "gpu_count": { "min": 1, "suggested": 1, "reason": "..." },
  "interconnect": { "needed": false, "reason": "..." },
  "runtime": { "known": false, "reason": "no benchmark for this workload; runtime not estimated" },
  "questions": [ { "field": "training.method", "question": "Full fine-tuning, LoRA or QLoRA?",
                   "why": "changes optimizer memory by ~10x" } ],
  "methodology": "/methodology/recommendations"
}
```

## 3. Recommendation — `POST /v1/recommend`

Body: WorkloadRequirements (+ optional `"limit": 1-20`). Read-only (uses `scoring.rank_listings`, never
`engine.preview`, so no audit/quote rows). Public (anonymous allowed); execution readiness for an
anonymous caller is reported as "sign in / API key required", never assumed.

```jsonc
{
  "estimate": { ...Estimate... },
  "picks": {
    "cheapest_market":  <option-ref> | null,   // cheapest fresh eligible listing that FITS memory
    "best_for_workload": <option-ref> | null,  // fits with headroom, balanced score, fresh
    "best_executable":  <option-ref> | null    // first option OpenGrid can actually launch for this caller
  },                                            // option-ref = {"option_id": "...", "reason": "..."}
  "options": [ {
    "option_id": "h100-80gb-sxm5:lambda:us-east-1:1",
    "tier": "lower_cost" | "balanced" | "higher_performance",
    "gpu": "NVIDIA H100 80GB SXM5", "gpu_slug": "h100-80gb-sxm5", "vram_gb": 80, "form_factor": "SXM",
    "gpu_count": 1, "provider": "lambda", "provider_display": "Lambda", "region": "us-east-1",
    "price_per_gpu_hour": 2.99, "price_per_instance_hour": 2.99, "price_kind": "observed",
    "available": true | false | null, "observed_at": "...", "age_seconds": 420, "fresh": true,
    "memory_fit": { "needed_per_gpu_gb": 29.2, "vram_gb": 80, "headroom_pct": 63.5, "fits": true },
    "cost": { "billing_unit": "per_minute", "scenarios": [
                { "hours": 1, "total_usd": 2.99, "assumption": "1 hour of runtime" },
                { "hours": 10, "total_usd": 29.9, "assumption": "..." } ],
              "budget_hours": 16.7,            // hours the budget buys, when budget given
              "unknown_costs": ["storage", "egress", "startup time"], "kind": "estimated" },
    "why": ["fits 29 GB estimate with 64% headroom", "cheapest fresh H100 SXM listing"],
    "limitations": ["runtime not benchmarked", "availability not reported by provider"],
    "execution": { "executable": false, "reasons": [ {"code": "...", "reason": "..."} ],
                   "workload_supported": false,   // only the allowlisted MNIST template is runnable
                   "route_preview_url": "/route?gpu=h100-80gb-sxm5&count=1" }
  } ],
  "excluded": [ { "gpu": "NVIDIA L4 24GB", "code": "insufficient_vram", "reason": "24 GB < 29.2 GB needed" } ],
  "questions": [ ...same as estimate.questions... ],
  "notes": ["Prices are observed list prices, not quotes.", ...],
  "as_of": "...", "methodology": "/methodology/recommendations"
}
```

Rules every interface must keep:
- Prices are observed market data; never invented. Stale or sold-out listings are not presented as deployable.
- Runtime is unknown unless a measured benchmark exists; cost is shown as scenarios with stated assumptions.
- "Executable" requires the existing readiness checks AND a supported workload (today: the MNIST template).
- `needs_gpu=false` recommendations must say so plainly and return no options.

## 4. Local analyzer output — `opengrid analyze <dir> --json`

```jsonc
{
  "root": "/abs/path", "requirements": { ...WorkloadRequirements... },
  "files_inspected": [ { "path": "train.py", "bytes": 4210, "reason": "training script" } ],
  "files_skipped": [ { "path": ".env", "reason": "secret file" } ],
  "signals": [ { "kind": "framework", "value": "peft", "source": "requirements.txt:4" } ],
  "questions": [ ... ], "warnings": [ ... ], "analyzer_version": "1"
}
```
The analyzer never executes project code, never installs anything, never reads secrets, weights or datasets,
and never sends file contents anywhere: only `requirements` leaves the machine (and only when the user asks
for a recommendation).

## 5. Assistant — `POST /v1/assistant`

```jsonc
// request
{ "message": "I want to fine-tune a 7B model under $50", "context": { ...previous "context" echo... } }
// response
{ "reply": "plain-language answer grounded in the cards", "cards": [
    { "type": "recommendation", "data": <Recommendation> } |
    { "type": "gpu_prices", "data": { "gpu": ..., "market": ... } } |
    { "type": "compare", "data": ... } | { "type": "questions", "data": [ ...questions ] } |
    { "type": "link", "data": { "label": "...", "href": "/route?..." } } ],
  "context": { "requirements": { ... } },      // client echoes it back next turn
  "engine": "deterministic" | "llm:<model>", "grounded": true }
```
The deterministic engine (no LLM) must handle the core intents; an LLM, when configured, only phrases and
routes — every number in a reply comes from a tool result in `cards`.
