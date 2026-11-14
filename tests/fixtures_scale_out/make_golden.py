"""Write golden_c89801f.json: recommend() payloads for the golden requests on the golden market, produced by the code
BEFORE scale-out (base commit c89801f). Regenerate only when an intended change alters these payloads:

    git archive c89801f | tar -x -C /tmp/og-base
    .venv/bin/python tests/fixtures_scale_out/make_golden.py /tmp/og-base

The ROOT argument is the code that produces the payloads; this directory supplies only the market and requests.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(sys.argv[1]).resolve()
os.environ.setdefault("OPENGRID_NO_JOBS", "1")
os.environ.setdefault("POLLER_ENABLED", "false")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(HERE))

import scratchdb  # noqa: E402
import fixtures  # noqa: E402
import so_market  # noqa: E402
import normalize as N  # noqa: E402
from accounts.auth import PUBLIC  # noqa: E402
from intelligence.recommend import recommend  # noqa: E402
from intelligence.requirements import WorkloadRequirements as W  # noqa: E402
from routing import scoring  # noqa: E402
from tables import ComputeListingRow  # noqa: E402

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
DB = "og_golden_scale_out"


def main():
    S = fixtures.session(scratchdb.create(DB))
    N.SessionLocal = S
    with S.begin() as s:
        s.add_all(so_market.golden_rows(ComputeListingRow, NOW))
    scoring.history.cache_clear()
    try:
        out = {k: so_market.normalize(recommend(W.model_validate(b), PUBLIC, now=NOW, limit=20))
               for k, b in so_market.GOLDEN_REQUESTS.items()}
    finally:
        S.kw["bind"].dispose()
        scratchdb.drop(DB)
    (HERE / "golden_c89801f.json").write_text(json.dumps(out, sort_keys=True, default=str) + "\n")
    print("wrote", HERE / "golden_c89801f.json")


main()
