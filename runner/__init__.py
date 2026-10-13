"""OpenGrid job runner: the code that runs ON the rented GPU instance.

- mnist_train.py  the allowlisted `pytorch-mnist` training workload (imports torch lazily, so this package is
                  importable on the app server, which has no torch).
- reporter.py     stdlib-only client for the runner callback API (events / artifacts / complete) with seq
                  numbers, retries and an offline spool; also the process supervisor on the instance.
- bootstrap.py    renders the cloud-init user_data bash script that installs pinned PyTorch and runs the above.
                  Runs on the APP side only; it is not shipped to the GPU.

Wire format and verification: methodology/runner.md.
"""
from pathlib import Path

_HERE = Path(__file__).resolve().parent
RUNNER_VERSION = (_HERE / "VERSION").read_text(encoding="utf-8").strip()
