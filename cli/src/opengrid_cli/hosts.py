"""`opengrid mcp config <host>`: the exact config snippets for each MCP host, three ways to connect.

  hosted     nothing to install: the OpenGrid server's own MCP endpoint (<url>/mcp) with your API key as a
             Bearer header. No local tools (project analysis needs a local server).
  uvx        zero-install local server: `uvx --from git+<repo>#subdirectory=cli opengrid mcp` (needs uv).
  installed  the `opengrid` command on your PATH (`uv tool install ./cli`), or `--from-repo` for a checkout.

Snippets never contain a literal API key. They reference the OPENGRID_API_KEY environment variable with each
host's own syntax (VS Code: a password prompt kept in its secret storage). The local server also falls back
to the key stored by `opengrid auth login`, so a local config can omit the key entirely.
"""

from __future__ import annotations

import json
from pathlib import Path

HOSTS = ("claude", "cursor", "codex", "vscode")
MODES = ("hosted", "uvx", "installed")
GIT_SPEC = "git+https://github.com/fzsheik/opengrid-terminal#subdirectory=cli"
ENV = "OPENGRID_API_KEY"


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


def launch(mode: str, from_repo: bool = False) -> tuple[str, list[str]]:
    if mode == "uvx":
        return "uvx", ["--from", GIT_SPEC, "opengrid", "mcp"]
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


def _hosted(host: str, url: str) -> dict:
    mcp_url = url.rstrip("/") + "/mcp"
    notes = [f"set {ENV} to an OpenGrid API key (opg_live_..., from {url}/keys) in the environment the host "
             f"starts from; the endpoint refuses requests without it",
             "hosted tools: market data, recommendations from a requirements object, route preview, jobs, the "
             "free sandbox demo, cancel; project analysis needs the local server (uvx or installed)"]
    if host == "claude":
        cfg = {"mcpServers": {"opengrid": {"type": "http", "url": mcp_url,
                                           "headers": {"Authorization": f"Bearer ${{{ENV}}}"}}}}
        return {"config": cfg, "file": ".mcp.json (project root)",
                # single quotes: the shell must NOT expand the variable; Claude Code expands it at startup.
                # The name goes before the variadic --header.
                "cli": "claude mcp add --transport http --scope local opengrid " + _sh([mcp_url])
                       + f" --header 'Authorization: Bearer ${{{ENV}}}'",
                "notes": notes + ["Claude Code expands ${OPENGRID_API_KEY} from your environment when it connects and "
                                  "stores only the reference; `--scope user` makes it available in every project; "
                                  "verify with `claude mcp get opengrid`, then /mcp"]}
    if host == "cursor":
        cfg = {"mcpServers": {"opengrid": {"url": mcp_url,
                                           "headers": {"Authorization": f"Bearer ${{env:{ENV}}}"}}}}
        return {"config": cfg, "file": ".cursor/mcp.json (project) or ~/.cursor/mcp.json (global)", "cli": None,
                "notes": notes + ["${env:OPENGRID_API_KEY} is read from the environment Cursor was started from"]}
    if host == "codex":
        toml = "\n".join(["[mcp_servers.opengrid]", f"url = {json.dumps(mcp_url)}",
                          "# Codex sends `Authorization: Bearer <value of this variable>`; never write the key here",
                          f'bearer_token_env_var = "{ENV}"'])
        return {"config": toml, "format": "toml", "file": "~/.codex/config.toml",
                "cli": _sh(["codex", "mcp", "add", "opengrid", "--url", mcp_url, "--bearer-token-env-var", ENV]),
                "notes": notes + ["verify: `codex mcp get opengrid`"]}
    if host == "vscode":
        cfg = {"inputs": [_vscode_input(required=True)],
               "servers": {"opengrid": {"type": "http", "url": mcp_url,
                                        "headers": {"Authorization": "Bearer ${input:opengrid-api-key}"}}}}
        return {"config": cfg, "file": ".vscode/mcp.json", "cli": None,
                "notes": notes[1:] + ["VS Code prompts for the key once (password input) and keeps it in its "
                                      "secret storage", "run 'MCP: List Servers' to start and check the server"]}
    raise ValueError(host)


def _vscode_input(required: bool = False) -> dict:
    hint = "" if required else "; leave empty for public data only"
    return {"type": "promptString", "id": "opengrid-api-key", "description": f"OpenGrid API key (opg_live_...){hint}",
            "password": True}


def _local(host: str, url: str, mode: str, from_repo: bool) -> dict:
    command, args = launch(mode, from_repo)
    first = (["uvx fetches and runs the client from GitHub on each start (cached after the first); needs uv: "
              "https://docs.astral.sh/uv/"] if mode == "uvx" else
             ["needs `opengrid` on the PATH the host uses (`uv tool install ./cli`); GUI apps may need the "
              "absolute path from `which opengrid`"] if not from_repo else [])
    if host == "claude":
        cfg = {"mcpServers": {"opengrid": {"type": "stdio", "command": command, "args": args,
                                           "env": {"OPENGRID_URL": url, ENV: f"${{{ENV}:-}}"}}}}
        return {"config": cfg, "file": ".mcp.json (project root, shared with the repo)",
                # the name goes first: --env is variadic and would swallow it
                "cli": _sh(["claude", "mcp", "add", "opengrid", "--transport", "stdio", "--scope", "local",
                            "--env", f"OPENGRID_URL={url}", "--", command, *args]),
                "notes": first + [
                    "${OPENGRID_API_KEY:-} is expanded by Claude Code from your environment when the server starts; "
                    "empty means: use the key stored by `opengrid auth login`, or anonymous public reads",
                    "the `claude mcp add` form stores no key at all: run `opengrid auth login` once instead",
                    "verify: `claude mcp get opengrid`, then /mcp inside Claude Code"]}
    if host == "cursor":
        cfg = {"mcpServers": {"opengrid": {"type": "stdio", "command": command, "args": args,
                                           "env": {"OPENGRID_URL": url, ENV: f"${{env:{ENV}}}"}}}}
        return {"config": cfg, "file": ".cursor/mcp.json (project) or ~/.cursor/mcp.json (global)", "cli": None,
                "notes": first + ["${env:OPENGRID_API_KEY} is read from the environment Cursor was started from",
                                  "check Cursor Settings > MCP: the opengrid server should list its tools"]}
    if host == "codex":
        toml = "\n".join([
            "[mcp_servers.opengrid]",
            f"command = {json.dumps(command)}",
            f"args = {json.dumps(args)}",
            f"env = {{ OPENGRID_URL = {json.dumps(url)} }}",
            "# forwarded from your shell environment; never write the key itself here",
            f'env_vars = ["{ENV}"]',
        ])
        return {"config": toml, "format": "toml", "file": "~/.codex/config.toml",
                "cli": _sh(["codex", "mcp", "add", "opengrid", "--env", f"OPENGRID_URL={url}", "--", command, *args]),
                "notes": first + ["`codex mcp add` cannot forward a variable by name: either add the env_vars line "
                                  "to config.toml, or run `opengrid auth login` so the server reads the stored key",
                                  "verify: `codex mcp get opengrid`"]}
    if host == "vscode":
        cfg = {"inputs": [_vscode_input()],
               "servers": {"opengrid": {"type": "stdio", "command": command, "args": args,
                                        "env": {"OPENGRID_URL": url, ENV: "${input:opengrid-api-key}"}}}}
        return {"config": cfg, "file": ".vscode/mcp.json", "cli": None,
                "notes": first + ["VS Code prompts for the key once (password input) and stores it in its secret "
                                  "storage", "run 'MCP: List Servers' from the command palette to start and check "
                                  "the server"]}
    raise ValueError(host)


TITLES = {"hosted": "hosted endpoint: nothing to install, just the URL and your API key",
          "uvx": "local server, zero install (uvx runs it from GitHub)",
          "installed": "local server, installed `opengrid` command"}
NAMES = {"claude": "Claude Code", "cursor": "Cursor", "codex": "Codex", "vscode": "VS Code"}


def option(host: str, mode: str, *, url: str, from_repo: bool = False) -> dict:
    if host not in HOSTS:
        raise ValueError(f"unknown host {host!r}; choose one of {', '.join(HOSTS)}")
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; choose one of {', '.join(MODES)}")
    o = _hosted(host, url) if mode == "hosted" else _local(host, url, mode, from_repo and mode == "installed")
    fmt = o.pop("format", "json")
    cfg = o["config"]
    return {"mode": mode, "title": TITLES[mode] + (" (from this checkout)" if from_repo and mode == "installed"
                                                     else ""),
            "format": fmt, "file": o["file"], "config": cfg if fmt == "toml" else json.dumps(cfg, indent=2),
            "cli": o["cli"], "notes": o["notes"]}


def snippet(host: str, *, url: str, from_repo: bool = False, mode: str | None = None) -> dict:
    """All three options (or one, with `mode`). The top-level config/cli/notes are the first option shown:
    the hosted endpoint by default, the local server with --from-repo."""
    if host not in HOSTS:
        raise ValueError(f"unknown host {host!r}; choose one of {', '.join(HOSTS)}")
    modes = [mode] if mode else (["installed", "hosted", "uvx"] if from_repo else list(MODES))
    opts = [option(host, m, url=url, from_repo=from_repo) for m in modes]
    first = opts[0]
    return {"host": NAMES[host], "url": url, "options": opts, "mode": first["mode"], "file": first["file"],
            "format": first["format"], "config": first["config"], "cli": first["cli"], "notes": first["notes"]}
