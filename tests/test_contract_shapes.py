"""Contract test: every Data API GET route's response shape at each API version (docs/API_CONTRACT.md section 3).

The saved shape (tests/contracts/<version>.json) holds every field path and its JSON types, recorded from a seeded
scratch database. The test fails when a field disappears or changes type; new fields pass (additive). A breaking
change needs a new dated version, not an edit of the old snapshot.

Record deliberately:  UPDATE_CONTRACTS=1 .venv/bin/python tests/test_contract_shapes.py
Run:                  .venv/bin/python tests/test_contract_shapes.py
"""

import json
import os
import re
import sys
from pathlib import Path

os.environ["OPENGRID_NO_JOBS"] = "1"
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from fastapi.testclient import TestClient  # noqa: E402

import fixtures  # noqa: E402
import main  # noqa: E402
import normalize  # noqa: E402
import scratchdb  # noqa: E402
from api import contract  # noqa: E402

DB = "og_test_contract_shapes"
SAMPLE = {"gpu": "h100-80gb-sxm5", "provider": "syn_alpha", "index_id": "h100-80gb-sxm5", "family": "h100",
          "series": "x", "name": "indices", "fixing_id": "x", "shift_id": "1", "item_id": "1", "dataset": "listings"}
# A dict key that is data, not a field name (dates, GPU names / ids, provider ids, windows): collapsed to "*".
DYNAMIC = re.compile(r"^(\d|syn_)|[A-Z :-]|^[a-z0-9]+(-[a-z0-9]+)+$")


def shape(v):
    """{path: sorted JSON types} for every field in a body."""
    out: dict[str, set] = {}

    def walk(x, path):
        t = ("null" if x is None else "boolean" if isinstance(x, bool) else "number" if isinstance(x, (int, float))
             else "string" if isinstance(x, str) else "array" if isinstance(x, list) else "object")
        out.setdefault(path or "$", set()).add(t)
        if isinstance(x, dict):
            for k, y in x.items():
                walk(y, f"{path}.{'*' if DYNAMIC.search(k) else k}")
        elif isinstance(x, list):
            for y in x:
                walk(y, path + "[]")

    walk(v, "")
    return {p: sorted(t) for p, t in out.items()}


def paths() -> list[str]:
    out = []
    for path, item in main.app.openapi()["paths"].items():
        if "get" in item and contract.is_data_path(path):
            out.append(re.sub(r"\{([a-z_]+)\}", lambda m: SAMPLE.get(m.group(1), "x"), path))
    return sorted(set(out))


def current() -> dict:
    c = TestClient(main.app, headers={"X-OpenGrid-Request": "1"})
    got = {}
    for p in paths():
        r = c.get(p)
        if r.status_code == 200 and "json" in r.headers.get("content-type", ""):
            got[p] = shape(r.json()["data"]) if isinstance(r.json(), dict) and "data" in r.json() else shape(r.json())
    return got


def test_shapes_match_the_saved_contract():
    file = ROOT / "tests" / "contracts" / f"{contract.LATEST}.json"
    now = current()
    if os.environ.get("UPDATE_CONTRACTS"):
        file.parent.mkdir(exist_ok=True)
        file.write_text(json.dumps(now, indent=1, sort_keys=True) + "\n")
        print(f"recorded {len(now)} routes to {file}")
        return
    saved = json.loads(file.read_text())
    broken = []
    for route, fields in saved.items():
        if route not in now:
            broken.append(f"{route}: route gone or no longer 200")
            continue
        for path, types in fields.items():
            have = now[route].get(path)
            if have is None:
                # gone, unless it was only ever null, or a list above it is empty in this run (no items to show it)
                lists = [path[:i + 2] for i in range(len(path)) if path.startswith("[]", i)]
                if "null" not in types and all(l in now[route] for l in lists):
                    broken.append(f"{route}: field {path} gone")
            elif set(types) - {"null"} and not (set(types) - {"null"}) & set(have):
                broken.append(f"{route}: field {path} was {types}, now {have}")
    assert not broken, "breaking change without a new API version:\n" + "\n".join(broken)


if __name__ == "__main__":
    normalize.SessionLocal = fixtures.session(scratchdb.create(DB))
    try:
        fixtures.seed(normalize.SessionLocal, days=10)
        from analytics import rollups
        rollups.refresh()
        test_shapes_match_the_saved_contract()
        print("ok  test_shapes_match_the_saved_contract\nall passed")
    finally:
        normalize.SessionLocal.kw["bind"].dispose()
        scratchdb.drop(DB)
