from transformers import AutoProcessor, MllamaForConditionalGeneration

model = MllamaForConditionalGeneration.from_pretrained("./model", device_map="auto")
processor = AutoProcessor.from_pretrained("./model")


def answer(image, question):
    inputs = processor(image, question, return_tensors="pt").to(model.device)
    return processor.decode(model.generate(**inputs, max_new_tokens=64)[0])
