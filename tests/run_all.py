"""Run every test file: tests/test_*.py with this Python, tests/test_*.js with node. Exit 1 if any fails.

Run:  .venv/bin/python tests/run_all.py            all files, 6 at a time
      .venv/bin/python tests/run_all.py market fx   only files whose name contains "market" or "fx"
      JOBS=1 TIMEOUT=900 .venv/bin/python tests/run_all.py

Each test file is a standalone script (its own scratch database, see scratchdb.py), so files run in parallel.
"""

import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
JOBS = int(os.environ.get("JOBS", "6"))
TIMEOUT = int(os.environ.get("TIMEOUT", "600"))


def files(filters: list[str]) -> list[Path]:
    found = sorted(HERE.glob("test_*.py")) + sorted(HERE.glob("test_*.js"))
    return [f for f in found if not filters or any(s in f.name for s in filters)]


def run(f: Path) -> tuple[Path, str, float, str]:
    cmd = ["node", str(f)] if f.suffix == ".js" else [sys.executable, str(f)]
    t0 = time.monotonic()
    try:
        p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=TIMEOUT)
        status = "ok" if p.returncode == 0 else "FAIL"
        out = p.stdout + p.stderr
    except subprocess.TimeoutExpired as e:
        status, out = "TIMEOUT", f"{e.stdout or ''}{e.stderr or ''}"
        out = out.decode() if isinstance(out, bytes) else out
    return f, status, time.monotonic() - t0, out


def main() -> int:
    todo = files(sys.argv[1:])
    if not todo:
        print("no test files matched")
        return 1
    failed = []
    with ThreadPoolExecutor(JOBS) as pool:
        for f, status, secs, out in pool.map(run, todo):
            print(f"{status:7} {secs:6.1f}s  {f.name}", flush=True)
            if status != "ok":
                failed.append((f, status, out))
    for f, status, out in failed:
        print(f"\n===== {status}: {f.name} (last 40 lines) =====")
        print("\n".join(out.strip().splitlines()[-40:]))
    print(f"\n{len(todo) - len(failed)}/{len(todo)} files passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
