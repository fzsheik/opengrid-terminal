"""Same model without Docker: python serve_local.py (wraps the vLLM OpenAI server entrypoint)."""
import subprocess
import sys

MODEL = "hugging-quants/Meta-Llama-3.1-8B-Instruct-AWQ-INT4"

if __name__ == "__main__":
    subprocess.run([sys.executable, "-m", "vllm.entrypoints.openai.api_server",
                    "--model", MODEL, "--quantization", "awq",
                    "--max-model-len", "8192", "--max-num-seqs", "32"], check=True)
