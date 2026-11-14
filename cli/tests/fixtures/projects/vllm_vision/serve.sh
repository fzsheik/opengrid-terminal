#!/usr/bin/env bash
# OpenAI-compatible endpoint for image questions
vllm serve llava-hf/llava-1.5-7b-hf --max-model-len 4096 --limit-mm-per-prompt image=4
