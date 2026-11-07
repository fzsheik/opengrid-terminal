"""Public GPU ids (gpu_ids.py; docs/API_CONTRACT.md section 6): complete, unique, well-formed, resolvable.

Run:  .venv/bin/python tests/test_gpu_ids.py   (no database)
"""

import os
import re
import sys
from pathlib import Path

os.environ["OPENGRID_NO_JOBS"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import canonical  # noqa: E402
import gpu_ids  # noqa: E402
import hardware  # noqa: E402
from api.common import gpu_slug, resolve_gpu  # noqa: E402

ID = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)+$")


def test_every_canonical_gpu_has_one_id():
    names = set(canonical.all_canonical_names())
    missing = sorted(names - set(gpu_ids.GPU_IDS))
    assert not missing, f"add a line to gpu_ids.py for: {missing}"
    stale = sorted(set(gpu_ids.GPU_IDS) - names)
    assert not stale, f"gpu_ids.py keys no longer canonical (rename the key, keep the id): {stale}"
    ids = list(gpu_ids.GPU_IDS.values())
    assert len(ids) == len(set(ids)), "duplicate id"


def test_id_form_and_vendor():
    for name, i in gpu_ids.GPU_IDS.items():
        assert ID.match(i), (name, i)
        vendor = ((hardware.spec(name) or {}).get("vendor") or name.split()[0]).lower()
        assert i.startswith(vendor + "-"), (name, i, vendor)


def test_resolves_id_name_and_old_slug():
    for name, i in gpu_ids.GPU_IDS.items():
        assert resolve_gpu(i) == name, i
        assert resolve_gpu(name) == name
        assert resolve_gpu(gpu_slug(name)) == name, "older short slugs stay accepted as input"
    assert resolve_gpu("NVIDIA-H100-80GB-SXM5".lower()) == "NVIDIA H100 80GB SXM5"
    assert resolve_gpu("no-such-gpu", strict=False) is None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok ", name)
    print("all passed")
