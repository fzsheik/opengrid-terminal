"""With PUBLIC_PAGES on, anonymous visitors read market data and nothing else.

Run:  .venv/bin/python tests/test_public_access.py
"""

import os
import sys
from pathlib import Path

os.environ["OPENGRID_NO_JOBS"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
from config import settings  # noqa: E402

PRIVATE_GETS = ["/v1/me", "/v1/keys", "/v1/usage", "/v1/watchlists", "/v1/alerts", "/v1/deployments",
                "/v1/ops/summary", "/v1/billing/usage", "/v1/credentials", "/v1/admin/accounts"]


def test_matrix():
    saved = settings.app_password, settings.public_pages
    try:
        settings.app_password = "pw"
        client = TestClient(main.app)
        settings.public_pages = True
        assert client.get("/v1/methodology").status_code == 200, "public data read"
        for path in PRIVATE_GETS:
            assert client.get(path).status_code in (401, 403), path
        assert client.post("/v1/route/preview", json={"gpu": "h100-80gb-sxm5", "count": 1}).status_code == 401
        assert client.post("/v1/route", json={"gpu": "h100-80gb-sxm5", "count": 1}).status_code == 401
        settings.public_pages = False
        assert client.get("/v1/methodology").status_code == 401, "closed when public_pages is off"
    finally:
        settings.app_password, settings.public_pages = saved


if __name__ == "__main__":
    test_matrix()
    print("test_matrix ok")
