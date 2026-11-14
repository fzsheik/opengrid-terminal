from transformers import pipeline

asr = pipeline("automatic-speech-recognition", model="openai/whisper-large-v3-turbo", device=0)


def handler(path):
    return asr(path, chunk_length_s=30, batch_size=16)["text"]
