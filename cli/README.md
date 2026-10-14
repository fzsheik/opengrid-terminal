# opengrid-cli

The OpenGrid command-line client and local MCP server. It covers GPU market data, local project analysis,
workload-aware compute recommendations, and read-only job status. It is packaged separately from the server
(this directory has its own `pyproject.toml`).

**No command can spend money.** There is deliberately no `launch`, `route execute` or real `jobs submit`
command, and no MCP tool that provisions compute, submits a real job or executes code. Only three things
write anything on the server:

| what | writes | spends |
|---|---|---|
| `opengrid route preview` / `opengrid_preview_route` | a preview ticket + quote record (audit) | never |
| `opengrid jobs cancel --yes` / `opengrid_cancel_job confirm=true` | cancels (rejects the approval, or terminates the GPU) | reduces spend |
| `opengrid demo train` | a SANDBOX MNIST job: simulated, no provider called, no GPU | never |

There is no PyPI package. Install it from this repository.

## Install

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```sh
# as a tool on your PATH (non-editable copy in ~/.local/share/uv/tools, `opengrid` in ~/.local/bin)
uv tool install ./cli
opengrid doctor

# or straight from the checkout, no install
uv run --project cli opengrid doctor

# upgrade after pulling / remove
uv tool install --reinstall ./cli
uv tool uninstall opengrid-cli
```

## Configuration

| setting | how | default |
|---|---|---|
| server | `--url URL` or `OPENGRID_URL` | `http://localhost:8000` (local dev) |
| API key | `OPENGRID_API_KEY`, else the file written by `opengrid auth login` | none |
| config dir | `OPENGRID_CONFIG_DIR` | `$XDG_CONFIG_HOME/opengrid` or `~/.config/opengrid` (Windows: `%APPDATA%\opengrid`) |
| MCP analyzer roots | `OPENGRID_ALLOWED_ROOTS` (`:`-separated; `;` on Windows) | the MCP server's working directory |

For production, use `export OPENGRID_URL=https://opengrid-terminal-production.up.railway.app`, or pass
`--url` on each command.

Public market data needs no key: `gpu ...`, `providers`, `estimate` and `recommend` (when the server has
`PUBLIC_PAGES` on, or on a local dev server). The other commands need an API key (`opg_live_...`) with
these scopes:

| command / tool | scope |
|---|---|
| `route preview`, `opengrid_preview_route` | `route:preview` |
| `jobs list/status/logs`, `opengrid_list_jobs`, `opengrid_get_job_status` | `deployments:read` |
| `jobs cancel`, `demo train`, `opengrid_cancel_job` | `route:execute` (the scope of the job submit/cancel endpoints) |

### Auth

```sh
opengrid auth login            # hidden prompt; verifies the key with GET /v1/me, then stores it
echo "$KEY" | opengrid auth login --stdin
opengrid auth status           # which key (prefix only), where it came from, its scopes
opengrid auth logout
```

The key is stored in `<config dir>/credentials.json` with mode `0600`, in a `0700` directory. It is never
printed. Output shows at most the public 13-character prefix (`opg_live_ab12...`). Every error message
redacts the key and anything shaped like a bearer token, including text the server echoes back.
`OPENGRID_API_KEY` takes precedence over the stored key.

## Commands

Every command accepts `--json`, `--url` and `--timeout`, either before or after the subcommand.

```sh
opengrid doctor                                   # connectivity, server version, health, auth + scopes, MCP readiness
opengrid version

opengrid gpu search [--model H100] [--vendor NVIDIA|AMD|Intel] [--min-vram 80] [--max-price 3]
                    [--available] [--sort providers|price|vram] [--limit 50]
opengrid gpu show <gpu> [--providers 10]           # specs + market now; a family (h100) lists its variants
opengrid gpu compare <a> <b>                       # side by side, incl. price per unit of peak capability
opengrid gpu market <gpu> [--count] [--region] [--max-price] [--limit]   # price history context + best-available ranking
opengrid providers [--limit]

opengrid analyze <dir> [--dry-run] [--exclude GLOB ...] [--max-files 400] [--max-file-bytes 256000]
opengrid estimate  [<dir>] [flags] [--yes]
opengrid recommend [<dir>] [flags] [--yes] [--limit N]
    flags: --params B --model ID --method full|lora|qlora|inference --workload ... --precision ...
           --quantization ... --seq-len --batch --lora-rank --context-len --concurrency --engine
           --budget USD --max-price USD --runtime-hours --region us|eu|... --vendor --max-gpus
           --gpu NAME (repeatable) --framework NAME (repeatable)

opengrid route preview <gpu> [--count 1] [--region US] [--max-price 3] [--mode BALANCED]
opengrid jobs list [--status open] [--limit 20]
opengrid jobs status <job-id>
opengrid jobs logs <job-id> [--follow] [--after ID] [--limit 500]
opengrid jobs cancel <job-id> --yes [--idempotency-key KEY]
opengrid demo train [--epochs 3] [--follow]        # SANDBOX only

opengrid mcp                                       # run the MCP server on stdio
opengrid mcp --http [--host 127.0.0.1] [--port 8765] [--allow-origin ORIGIN]
opengrid mcp config claude|cursor|codex|vscode [--from-repo]
```

### What leaves your machine

* `analyze` is local only. The analyzer (`opengrid_cli.analyze`) reads only allowlisted files (manifests,
  configs, scripts). It never reads secrets, weights or datasets, never executes project code, and sends
  nothing. `--dry-run` lists which files it would read, without opening any of them.
* `estimate` and `recommend` send only the WorkloadRequirements object (docs/OPENGRID2_CONTRACTS.md section 1),
  never file contents. The command prints that object before sending it. If the requirements came from a
  directory, it then asks for confirmation. `--yes` skips the prompt. When stdin is not a terminal and
  `--yes` is not given, the command refuses (exit 6) and sends nothing. Requirements built only from flags,
  which you typed yourself, are shown but not confirmed. Flags override what the analyzer found.

### Reading the output: observed or estimated

The human output labels where each number comes from:

* **observed**: prices and availability as providers list them now. These are list prices, not quotes.
  Stale or sold-out listings are excluded from rankings.
* **inferred**: rankings (`gpu market`, `route preview`) are computed by the server from observed prices.
* **estimated**: memory, GPU count and cost scenarios. Each comes with its assumptions and a confidence.
  Runtime is never estimated without a measured benchmark, so cost is shown as scenarios
  ("10 h: $29.90 (10 hours of runtime)").
* **vendor peak specs**: hardware figures, not workload benchmarks.
* `recommend` shows three picks (cheapest market, best for workload, best executable), each option's tier,
  its memory fit and headroom, cost scenarios and unknown costs, why it was chosen, its limitations,
  execution readiness with reasons, the exclusions by code, and the analyzer's evidence (`field = value
  from file:line [confidence]`).

### JSON output

`--json` prints one JSON document on stdout. Notices and prompts go to stderr, so stdout always parses.

```jsonc
{"ok": true,  "command": "gpu search", "data": { ... }}
{"ok": false, "command": "jobs list",  "error": {"type": "AuthError", "message": "...", "status": 401,
                                                  "code": "unauthorized", "request_id": "cli_...",
                                                  "path": "/v1/jobs", "exit_code": 3}}
```

The fields of `data` are stable per command. Here are the main ones:

* `gpu search`: `items[]` (`slug, name, vendor, vram_gb, low/median/high_price_per_gpu_hour, providers,
  available_listings`), `total_matched`, `returned`, `truncated`, `price_kind`, `as_of`.
* `gpu show`: `kind` (`gpu` or `family`), `hardware`, `market`, `cheapest_by_provider[]`, `variants[]` (for a family).
* `estimate`: `{requirements_sent, estimate}`. `recommend`: `{requirements_sent, recommendation}`. The
  server's contract objects are passed through unchanged.
* `analyze`: the analyzer output (contract section 4).

### Exit codes

| code | meaning |
|---|---|
| 0 | ok |
| 1 | API error, invalid response, or a `doctor` check failed |
| 2 | usage error (bad arguments) |
| 3 | auth: no key, invalid key, or the key lacks the needed scope (the message names the scope) |
| 4 | not found (unknown GPU, provider or job) |
| 5 | OpenGrid unreachable (connection refused, DNS, TLS, timeout) |
| 6 | refused by a safety rule: confirmation missing, non-interactive without `--yes`, invalid id, path outside allowed roots |
| 130 | interrupted |

### Network behavior

* Timeouts: 20 s by default (`--timeout`), with a 10 s connect timeout.
* Only GETs are retried: twice, on connection errors, timeouts and 429/502/503/504, honoring `Retry-After`
  up to 5 s. POSTs are sent once. Cancel and demo carry an `Idempotency-Key`; reuse it with
  `--idempotency-key` to retry safely.
* Responses larger than 8 MiB are refused.
* Every error message includes the request id (`X-Request-ID`) to quote to support.
* Path segments (GPU slugs, job ids) are URL-escaped. Job ids are validated (`job-<hex>`) before any request.

## MCP server

`opengrid mcp` runs a local MCP server on stdio. It is built with the official `mcp` Python SDK
(`MCPServer`), supports protocol 2026-07-28 (server discover) and the legacy initialize handshake, and logs
only to stderr.

| tool | annotations | what it does |
|---|---|---|
| `opengrid_search_gpus` | read-only, open-world | catalog search: `model`, `vendor`, `min_vram_gb`, `max_price_per_gpu_hour`, `available_only`, `sort`, `limit` (1-50) |
| `opengrid_get_gpu_specs` | read-only, open-world | specs and market for one GPU: `gpu`, `max_providers` (1-25); a family returns its variants |
| `opengrid_compare_gpus` | read-only, open-world | `gpu_a`, `gpu_b` side by side |
| `opengrid_get_market_context` | read-only, open-world | history context plus the best-available-now ranking: `gpu`, `count`, `region`, `max_price_per_gpu_hour`, `max_candidates` (1-20) |
| `opengrid_analyze_project` | read-only, **local** (open_world false) | `path` (must be inside the allowed roots), `exclude[]`; returns requirements and file names/sizes, never contents |
| `opengrid_estimate_requirements` | read-only, open-world | `requirements` (WorkloadRequirements, `extra=forbid`) gives the contract section 2 estimate |
| `opengrid_recommend_compute` | read-only, open-world | `requirements` **or** `project_path`, `limit` (1-10) gives the contract section 3 recommendation |
| `opengrid_preview_route` | **not read-only**, non-destructive | `gpu`, `count`, `region`, `max_price_per_gpu_hour`, `mode`; records a preview ticket, never provisions; needs `route:preview` |
| `opengrid_list_jobs` | read-only, open-world | `status`, `limit` (1-50); needs `deployments:read` |
| `opengrid_get_job_status` | read-only, open-world | `job_id` (`^job-[0-9a-f]{8,32}$`), `log_lines` (0-200) |
| `opengrid_cancel_job` | destructive, idempotent | `job_id`, `confirm` (must be `true`), `idempotency_key`; cancel only; needs `route:execute` |

Every tool has a Pydantic input schema with descriptions and bounds, and a structured output model, so
clients receive `outputSchema` and `structuredContent`. Failures come back as `isError` results with a
useful message: an unknown GPU, a missing scope, an unreachable server, or a refused path. The API key is
never included. Lists are bounded and say when they were truncated (`truncated`, `notes`), and the whole
result is capped at about 60 KB. Tool descriptions tell the model three things: project files and API
responses are untrusted data, prices are observed listings, and runtime is unknown unless benchmarked.

**Allowed roots.** `opengrid_analyze_project` and `project_path` only accept directories inside these roots:

* the server's working directory, unless that is `/` or your home directory;
* each path listed in `OPENGRID_ALLOWED_ROOTS` (never `/`).

Paths are resolved, symlinks included, before the check. `..`, symlinks that point outside, files and
missing paths are refused. MCP roots are not used for access control. If your host starts servers in your
home directory, set `OPENGRID_ALLOWED_ROOTS` in the host config.

**HTTP mode.** `opengrid mcp --http` serves streamable HTTP at `http://127.0.0.1:8765/mcp`. It binds to
loopback only (any other `--host` is refused), checks the Host header against DNS rebinding, and accepts
only a localhost `Origin` unless you add one with `--allow-origin`. The endpoint has no authentication and
acts with your key, so keep it on loopback.

### Host setup

`opengrid mcp config <host>` prints these snippets. They never contain a key: they reference
`OPENGRID_API_KEY` in each host's own syntax. If you have run `opengrid auth login`, the server also uses
the stored key, so you can leave the key out of the config entirely. The snippets assume
`uv tool install ./cli`, which puts `opengrid` on your PATH. To launch from a checkout instead, use
`--from-repo`, which runs `uv run --quiet --project <checkout>/cli opengrid mcp`.

**Claude Code**: project `.mcp.json`:

```json
{
  "mcpServers": {
    "opengrid": {
      "type": "stdio",
      "command": "opengrid",
      "args": ["mcp"],
      "env": {"OPENGRID_URL": "http://localhost:8000", "OPENGRID_API_KEY": "${OPENGRID_API_KEY:-}"}
    }
  }
}
```

Or add it from the command line. Put the name first, because `--env` takes several values and would
swallow it:

```sh
claude mcp add opengrid --transport stdio --scope local --env OPENGRID_URL=http://localhost:8000 -- opengrid mcp
claude mcp get opengrid
```

**Cursor**: `.cursor/mcp.json` (project) or `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "opengrid": {
      "type": "stdio",
      "command": "opengrid",
      "args": ["mcp"],
      "env": {"OPENGRID_URL": "http://localhost:8000", "OPENGRID_API_KEY": "${env:OPENGRID_API_KEY}"}
    }
  }
}
```

**Codex**: `~/.codex/config.toml`:

```toml
[mcp_servers.opengrid]
command = "opengrid"
args = ["mcp"]
env = { OPENGRID_URL = "http://localhost:8000" }
# forwarded from your shell environment; never write the key itself here
env_vars = ["OPENGRID_API_KEY"]
```

You can also run `codex mcp add opengrid --env OPENGRID_URL=http://localhost:8000 -- opengrid mcp`. That
command cannot forward a variable by name, so either add the `env_vars` line by hand or use
`opengrid auth login`. Check the result with `codex mcp get opengrid`.

**VS Code**: `.vscode/mcp.json`. VS Code prompts for the key once and keeps it in its secret storage:

```json
{
  "inputs": [{"type": "promptString", "id": "opengrid-api-key",
              "description": "OpenGrid API key (opg_live_...); leave empty for public data only", "password": true}],
  "servers": {
    "opengrid": {
      "type": "stdio",
      "command": "opengrid",
      "args": ["mcp"],
      "env": {"OPENGRID_URL": "http://localhost:8000", "OPENGRID_API_KEY": "${input:opengrid-api-key}"}
    }
  }
}
```

#### What was verified (2026-10-13, macOS)

| host | verified |
|---|---|
| Claude Code 2.1.296 | `claude mcp add opengrid --scope local ... -- opengrid mcp` in a temp project: `claude mcp get` / `claude mcp list` reported **Connected** (Claude Code spawned the server and completed the handshake). The printed `.mcp.json` was recognized as a project server ("pending approval", which needs interactive approval). Both were removed afterwards. |
| Codex CLI 0.159.0 | in a temporary `CODEX_HOME`: `codex mcp add` + `codex mcp list` / `get` registered the server, and the printed `config.toml` snippet parsed, with `env_vars = ["OPENGRID_API_KEY"]` read back by `codex mcp get --json`. A live Codex session was not run (Codex does not connect during `mcp list`). |
| Cursor, VS Code | not installed on the test machine: the snippets are untested in those hosts. The printed JSON parses, and the shape follows each host's documented format. |
| MCP Inspector 2.10.1 | `npx -y @modelcontextprotocol/inspector --cli opengrid mcp --method tools/list --strict`: 11 tools, 0 errors, 0 warnings (also with `--protocol-era modern`). `tools/call opengrid_search_gpus` returned live data from a local server. |

### Quick connectivity test

In your MCP host, ask:

> Use the opengrid tools: search for H100 GPUs under $3 per GPU-hour, then show the specs and current market
> for the cheapest one. Say which numbers are observed prices and which are vendor specs.

A working setup calls `opengrid_search_gpus`, then `opengrid_get_gpu_specs`, and answers with observed
prices and an `as_of` time.

From a terminal:

```sh
npx -y @modelcontextprotocol/inspector --cli opengrid mcp --method tools/list --strict
npx -y @modelcontextprotocol/inspector --cli opengrid mcp --method tools/call \
    --tool-name opengrid_search_gpus --tool-args-json '{"model":"H100","limit":3}' --format json
```

## Tests

Plain scripts. They need no network and no running server, and they use a mock API with contract-shaped
payloads and a stub analyzer:

```sh
uv run --project cli python cli/tests/test_cli.py        # parsing, JSON output, auth, redaction, --yes, client
uv run --project cli python cli/tests/test_mcp.py        # tools in-process: schemas, annotations, roots, bounds, errors
uv run --project cli python cli/tests/test_mcp_stdio.py  # spawns `opengrid mcp`; official client over stdio + loopback HTTP
```

## Troubleshooting

* **`error: cannot reach OpenGrid at http://localhost:8000`** (exit 5): no server is running there. Start
  the server, or point `--url` / `OPENGRID_URL` at production.
* **`needs an OpenGrid API key with scope '...'`** (exit 3): set `OPENGRID_API_KEY`, or run
  `opengrid auth login`, using a key that has that scope. `opengrid auth status` lists your key's scopes.
* **`the API key was rejected`**: the key is revoked, expired, or belongs to a different server URL.
* **`estimate`/`recommend` 404 or 405**: this server predates `/v1/recommend`. `opengrid doctor` reports
  this as `[warn] recommend_api`.
* **`confirmation needed but stdin is not a terminal`** (exit 6): add `--yes` in scripts and CI.
* **The MCP tool says a path is outside the allowed roots**: set `OPENGRID_ALLOWED_ROOTS` in the host
  config's `env`, or start the host from the project directory.
* **The MCP host shows no tools**: run `opengrid doctor`, which builds the server and lists its tools, and
  check that `opengrid` is on the PATH the host uses. GUI apps often have a shorter PATH, so you may need
  the absolute path from `which opengrid`. Server logs go to stderr, which hosts show in their MCP log view.
* **`ModuleNotFoundError: No module named 'opengrid_cli'` with `uv run --project cli` on macOS**: the
  editable install is a `.pth` file, and Python 3.12+ skips `.pth` files that carry the macOS `hidden`
  flag (`ls -lO cli/.venv/lib/*/site-packages/*.pth`). Some tools set that flag on whole directory trees.
  Fix it with `chflags -R nohidden cli/.venv`, or use `uv run --project cli --no-editable opengrid ...`,
  or `uv tool install ./cli`.
