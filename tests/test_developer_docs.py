"""The developer pages say only what the code does.

    - /api's curated public catalogue (web/pages/api.js) == the routes the server really serves without a key
      (api/pages.is_public over the app's own route table), so a visitor's "Try it" never meets a 401 and no
      private route is advertised as public;
    - /openapi.json and /docs stay operator-only (the original security decision), and the curated page is the
      anonymous reference;
    - every `opengrid ...` command shown on /developers parses with the real CLI parser;
    - every example question on /assistant reaches a real intent of the deterministic engine;
    - the hosted MCP endpoint refuses an anonymous request, as /developers says.

Run:  .venv/bin/python tests/test_developer_docs.py   (needs node; no database)
"""

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

os.environ["OPENGRID_NO_JOBS"] = "1"
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from starlette.requests import Request  # noqa: E402

import main  # noqa: E402
from api import pages  # noqa: E402
from config import settings  # noqa: E402

DUMP = json.loads(subprocess.run(["node", str(ROOT / "tests" / "test_web_developers.js"), "--dump"], check=True,
                                 capture_output=True, text=True).stdout)


def _req(method: str, path: str) -> Request:
    return Request({"type": "http", "method": method, "path": path, "headers": [], "query_string": b""})


def server_public_ops() -> set[str]:
    """Every /v1 operation in the app's own schema (built in-process, never served to visitors) that the
    public-surface gate lets through without a key."""
    out = set()
    for path, item in main.app.openapi()["paths"].items():
        if not path.startswith("/v1/") or path == "/v1/events/track":  # the analytics beacon is not an API
            continue
        for m in item:
            if m.upper() in ("GET", "POST", "PUT", "PATCH", "DELETE") and pages.is_public(_req(m.upper(), re.sub(r"\{[^}]+\}", "x", path))):
                out.add(f"{m.upper()} {path}")
    return out


def test_public_catalogue_matches_server():
    doc, srv = set(DUMP["publicOps"]), server_public_ops()
    assert doc == srv, f"documented but not public: {sorted(doc - srv)}; public but undocumented: {sorted(srv - doc)}"


def test_schema_stays_operator_only():
    client = TestClient(main.app)
    old_pub, old_pw = settings.public_pages, settings.app_password
    settings.public_pages, settings.app_password = True, "test-pw"
    try:
        for p in ("/openapi.json", "/docs"):
            assert not pages.is_public(_req("GET", p)), p
            assert client.get(p).status_code == 401, p
        assert client.get("/v1/gpus").status_code != 401, "public market data stays public"
        r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert r.status_code == 401, "the hosted MCP endpoint needs a key, even with PUBLIC_PAGES on"
    finally:
        settings.public_pages, settings.app_password = old_pub, old_pw
    api = (ROOT / "web" / "pages" / "api.js").read_text(encoding="utf-8")
    assert "if (operator) fullList(" in api, "the full schema is requested only for the operator"


def _opengrid_commands():
    cmds = []
    for text in DUMP["snippets"].values():
        for line in text.replace("\\\n", " ").splitlines():
            line = re.split(r"\s+#\s", line, maxsplit=1)[0].strip()  # shell comments, not the #subdirectory in the git URL
            if not line or line.startswith(("export ", "curl ", "claude ", "codex ", "cd ", "{", "}", "\"")):
                continue
            m = re.search(r"(?:^|\s)(?:uvx --from \"[^\"]+\" )?opengrid (.+)$", line)
            if m and not line.startswith("uv tool"):
                cmds.append(m.group(1))
    return cmds


def test_cli_commands_parse():
    from opengrid_cli.cli import build_parser

    cmds = _opengrid_commands()
    assert len(cmds) >= 10, cmds
    # concrete forms of every row in the /developers command table
    cmds += ["providers", "gpu search", "gpu show h100", "analyze .", "estimate --model meta-llama/Llama-3.1-8B",
             "route preview h100-80gb-sxm5", "jobs list", "jobs status job-0123abcd", "jobs logs job-0123abcd", "demo train",
             "jobs cancel job-0123abcd --yes", "auth login", "auth status", "auth logout", "doctor",
             "mcp config claude", "mcp config cursor", "mcp config codex", "mcp config vscode"]
    for c in cmds:
        argv = shlex.split(c)
        try:
            ns = build_parser().parse_args(argv)
        except SystemExit as e:
            raise AssertionError(f"`opengrid {c}` does not parse (exit {e.code})")
        assert getattr(ns, "fn", None), f"`opengrid {c}` is not a command"
    # the MCP launch line every local config uses
    assert build_parser().parse_args(["mcp"]).fn


def test_assistant_examples_are_understood():
    from assistant import intents

    for title, _, example in DUMP["capabilities"]:
        got = intents.parse(example).intent
        assert got != "unknown", f"{title}: {example!r} is not understood"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok ", name)
    print("all passed")
