"""Full fine-tuning with the Hugging Face Trainer; DeepSpeed config is passed on the command line."""
from dataclasses import dataclass, field
from typing import Optional

from datasets import load_from_disk
from transformers import AutoModelForCausalLM, AutoTokenizer, HfArgumentParser, Trainer, TrainingArguments


@dataclass
class ModelArguments:
    model_name_or_path: Optional[str] = field(default=None)
    dataset_path: str = field(default="./corpus")
    max_seq_length: int = field(default=4096)


def main():
    parser = HfArgumentParser((ModelArguments, TrainingArguments))
    margs, targs = parser.parse_args_into_dataclasses()
    tokenizer = AutoTokenizer.from_pretrained(margs.model_name_or_path)
    model = AutoModelForCausalLM.from_pretrained(margs.model_name_or_path, torch_dtype="bfloat16",
                                                 attn_implementation="flash_attention_2")
    ds = load_from_disk(margs.dataset_path)
    trainer = Trainer(model=model, args=targs, train_dataset=ds["train"], tokenizer=tokenizer)
    trainer.train()
    trainer.save_model()


if __name__ == "__main__":
    main()
