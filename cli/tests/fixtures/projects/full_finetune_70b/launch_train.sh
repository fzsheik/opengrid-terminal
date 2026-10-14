#!/usr/bin/env bash
# Full-parameter fine-tune of Llama-3-70B on one 8-GPU node (ZeRO-3, optimizer offloaded to CPU).
set -euo pipefail

accelerate launch --config_file accelerate_config.yaml train.py \
  --model_name_or_path meta-llama/Meta-Llama-3-70B \
  --dataset_path ./corpus \
  --per_device_train_batch_size 1 \
  --gradient_accumulation_steps 16 \
  --max_seq_length 4096 \
  --num_train_epochs 2 \
  --learning_rate 1e-5 \
  --bf16 \
  --gradient_checkpointing \
  --deepspeed ds_config_zero3.json \
  --output_dir out/llama3-70b-full
