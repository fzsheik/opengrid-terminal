"""`opengrid mcp config <host>`: the exact config snippet for each MCP host.

Snippets never contain a literal API key. They reference the OPENGRID_API_KEY environment variable with each
host's own syntax, and the server also falls back to the key stored by `opengrid auth login`, so a host
config can omit the key entirely.
"""

from __future__ import annotations

import json
from pathlib import Path

HOSTS = ("claude", "cursor", "codex", "vscode")


def repo_cli_dir() -> Path | None:
    """The cli/ project directory when running from a repo checkout (editable install / uv run)."""
    d = Path(__file__).resolve().parents[2]
    p = d / "pyproject.toml"
    try:
        if p.exists() and 'name = "opengrid-cli"' in p.read_text(encoding="utf-8"):
            return d
    except OSError:
        pass
    return None


def launch(from_repo: bool) -> tuple[str, list[str]]:
    if from_repo:
        d = repo_cli_dir()
        if d is None:
            raise ValueError("--from-repo: this opengrid is not running from a repo checkout; use the installed "
                             "`opengrid` command instead")
        return "uv", ["run", "--quiet", "--project", str(d), "opengrid", "mcp"]
    return "opengrid", ["mcp"]


def _sh(parts: list[str]) -> str:
    import shlex

    return " ".join(shlex.quote(p) for p in parts)


def snippet(host: str, *, url: str, from_repo: bool = False) -> dict:
    command, args = launch(from_repo)
    if host == "claude":
        cfg = {"mcpServers": {"opengrid": {"type": "stdio", "command": command, "args": args,
                                           "env": {"OPENGRID_URL": url, "OPENGRID_API_KEY": "${OPENGRID_API_KEY:-}"}}}}
        return {
            "host": "Claude Code", "file": ".mcp.json (project root, shared with the repo)",
            "format": "json", "config": json.dumps(cfg, indent=2),
            # the name goes first: --env is variadic and would swallow it
            "cli": _sh(["claude", "mcp", "add", "opengrid", "--transport", "stdio", "--scope", "local",
                        "--env", f"OPENGRID_URL={url}", "--", command, *args]),
            "notes": ["${OPENGRID_API_KEY:-} is expanded by Claude Code from your environment when the server starts; "
                      "empty means: use the key stored by `opengrid auth login`, or anonymous public reads",
                      "the `claude mcp add` form stores no key at all: run `opengrid auth login` once instead",
                      "verify: `claude mcp get opengrid`, then /mcp inside Claude Code"]}
    if host == "cursor":
        cfg = {"mcpServers": {"opengrid": {"type": "stdio", "command": command, "args": args,
                                           "env": {"OPENGRID_URL": url, "OPENGRID_API_KEY": "${env:OPENGRID_API_KEY}"}}}}
        return {"host": "Cursor", "file": ".cursor/mcp.json (project) or ~/.cursor/mcp.json (global)",
                "format": "json", "config": json.dumps(cfg, indent=2), "cli": None,
                "notes": ["${env:OPENGRID_API_KEY} is read from the environment Cursor was started from",
                          "check Cursor Settings > MCP: the opengrid server should list its tools"]}
    if host == "codex":
        toml = "\n".join([
            "[mcp_servers.opengrid]",
            f"command = {json.dumps(command)}",
            f"args = {json.dumps(args)}",
            f"env = {{ OPENGRID_URL = {json.dumps(url)} }}",
            "# forwarded from your shell environment; never write the key itself here",
            'env_vars = ["OPENGRID_API_KEY"]',
        ])
        return {"host": "Codex", "file": "~/.codex/config.toml", "format": "toml", "config": toml,
                "cli": _sh(["codex", "mcp", "add", "opengrid", "--env", f"OPENGRID_URL={url}", "--", command, *args]),
                "notes": ["`codex mcp add` cannot forward a variable by name: either add the env_vars line to "
                          "config.toml, or run `opengrid auth login` so the server reads the stored key",
                          "verify: `codex mcp get opengrid`"]}
    if host == "vscode":
        cfg = {"inputs": [{"type": "promptString", "id": "opengrid-api-key",
                           "description": "OpenGrid API key (opg_live_...); leave empty for public data only",
                           "password": True}],
               "servers": {"opengrid": {"type": "stdio", "command": command, "args": args,
                                        "env": {"OPENGRID_URL": url, "OPENGRID_API_KEY": "${input:opengrid-api-key}"}}}}
        return {"host": "VS Code", "file": ".vscode/mcp.json", "format": "json", "config": json.dumps(cfg, indent=2),
                "cli": None,
                "notes": ["VS Code prompts for the key once (password input) and stores it in its secret storage",
                          "run 'MCP: List Servers' from the command palette to start and check the server"]}
    raise ValueError(f"unknown host {host!r}; choose one of {', '.join(HOSTS)}")
