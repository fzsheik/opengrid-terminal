"""LoRA fine-tune of Llama-2-7B on an instruction dataset."""
import torch
from datasets import load_dataset
from peft import LoraConfig, get_peft_model
from transformers import (AutoModelForCausalLM, AutoTokenizer, DataCollatorForLanguageModeling, Trainer,
                          TrainingArguments)

MODEL_NAME = "meta-llama/Llama-2-7b-hf"
MAX_SEQ_LEN = 2048
OUTPUT_DIR = "out/llama2-7b-lora"


def tokenize(batch, tokenizer):
    return tokenizer(batch["text"], truncation=True, max_length=MAX_SEQ_LEN)


def main():
    tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
    tokenizer.pad_token = tokenizer.eos_token
    model = AutoModelForCausalLM.from_pretrained(MODEL_NAME, torch_dtype=torch.bfloat16)
    model.gradient_checkpointing_enable()

    lora_config = LoraConfig(
        r=16,
        lora_alpha=32,
        lora_dropout=0.05,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
        task_type="CAUSAL_LM",
    )
    model = get_peft_model(model, lora_config)

    ds = load_dataset("json", data_files="train.jsonl")["train"]
    ds = ds.map(lambda b: tokenize(b, tokenizer), batched=True, remove_columns=ds.column_names)

    args = TrainingArguments(
        output_dir=OUTPUT_DIR,
        per_device_train_batch_size=4,
        per_device_eval_batch_size=8,
        gradient_accumulation_steps=4,
        num_train_epochs=3,
        learning_rate=2e-4,
        bf16=True,
        gradient_checkpointing=True,
        optim="adamw_torch",
        logging_steps=10,
    )
    trainer = Trainer(model=model, args=args, train_dataset=ds,
                      data_collator=DataCollatorForLanguageModeling(tokenizer, mlm=False))
    trainer.train()
    model.save_pretrained(OUTPUT_DIR)


if __name__ == "__main__":
    main()
