import torch
from transformers import AutoProcessor, Qwen2VLForConditionalGeneration

MODEL = "Qwen/Qwen2-VL-7B-Instruct"

model = Qwen2VLForConditionalGeneration.from_pretrained(MODEL, torch_dtype=torch.bfloat16, device_map="auto")
processor = AutoProcessor.from_pretrained(MODEL)


def caption(image):
    messages = [{"role": "user", "content": [{"type": "image"}, {"type": "text", "text": "Describe this image."}]}]
    prompt = processor.apply_chat_template(messages, add_generation_prompt=True)
    inputs = processor(text=[prompt], images=[image], return_tensors="pt").to(model.device)
    out = model.generate(**inputs, max_new_tokens=128)
    return processor.batch_decode(out, skip_special_tokens=True)[0]
