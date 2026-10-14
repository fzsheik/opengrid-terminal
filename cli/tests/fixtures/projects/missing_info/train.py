"""Generic fine-tuning entry point: everything comes from the environment or the command line."""
import argparse
import os

from datasets import load_dataset
from peft import LoraConfig, get_peft_model
from transformers import AutoModelForCausalLM, AutoTokenizer, Trainer, TrainingArguments

model_id = os.environ["MODEL_ID"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--use_lora", action="store_true")
    parser.add_argument("--lora_r", type=int)
    parser.add_argument("--data", required=True)
    args = parser.parse_args()

    tokenizer = AutoTokenizer.from_pretrained(model_id)
    model = AutoModelForCausalLM.from_pretrained(model_id)
    if args.use_lora:
        model = get_peft_model(model, LoraConfig(r=args.lora_r, task_type="CAUSAL_LM"))

    ds = load_dataset("json", data_files=args.data)["train"]
    targs = TrainingArguments(output_dir="out", per_device_train_batch_size=int(os.environ["BATCH_SIZE"]))
    Trainer(model=model, args=targs, train_dataset=ds, tokenizer=tokenizer).train()


if __name__ == "__main__":
    main()
