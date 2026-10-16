# Developer onboarding: journey, verification, open items

Covers `/developers`, `/api`, `/keys`, `/onboarding`, `/assistant` and the shared assistant panel
(branch `ux/developer-onboarding`). The pages are built only on what the code does today; every claim below was
checked against the code or run on 2026-10-15 (macOS, local server in `PUBLIC_PAGES` mode and production reads).

## The journey

1. **/developers** opens on three choices, each with what it does, why it's useful, what you need, whether a key
   or an install is needed, and whether it's available now:
   - *Connect an AI coding assistant* (MCP): local server (no key, reads your project, needs uv + git) or hosted
     endpoint (key required, nothing to install, can't see your files).
   - *Use OpenGrid from your terminal* (the `opengrid` CLI; uv + git; no key for market data, analysis,
     recommendations).
   - *Integrate OpenGrid into your application* (REST; no key for public data and `POST /v1/recommend`).
2. **Quickstart, two first successes, no key:**
   - "Find the cheapest available H100": `GET /v1/best/h100?mode=CHEAPEST&limit=1`, `opengrid gpu search
     --model H100 --available --sort price`, or the assistant prompt.
   - "Analyze my project and recommend GPUs": `opengrid analyze . --dry-run` then `opengrid recommend .`, or
     the local MCP server (`opengrid_analyze_project` then `opengrid_recommend_compute`). The page says exactly
     which files are read, which never are, and that only the inferred requirements leave the machine.
   - The ladder analyze → recommend → preview (key: route:preview) → execute (not available yet).
3. **Access & API keys** states, shown by sign-in status:
   | state | what the page offers |
   |---|---|
   | anonymous, exploring | everything in the quickstart; no key needed |
   | needs a key | "keys are issued by the OpenGrid team; no self-service sign-up yet" + **Request API access** (a prefilled GitHub issue, marked public: no secrets) |
   | was issued a key | `opengrid auth login` (CLI + local MCP), `export OPENGRID_API_KEY` (hosted MCP + curl), `GET /v1/me`, minting narrower keys with `POST /v1/keys` (partner keys hold `account:manage`) |
   | operator | create keys on /keys (operator account); issue a developer their own account + key on /admin/partners |
4. **/api**: a curated reference everyone can use (quickstart, public vs key-protected, all 51 public operations
   with an anonymous "Try it", key-protected endpoints by scope, envelope, data kinds, rate limits). The full
   `/openapi.json` list and `/docs` appear only for the operator.
5. **/keys**: signed out, one access state and no private calls. Signed in: create a key first (Read-only /
   Assistant with previews and jobs / Custom; operator-level scopes set apart and confirmed), the secret once with
   how to use it, the key table, then usage, rate limits, provider credentials and billing in tabs that load only
   when opened. `?preset=assistant`, `#credentials` and `#billing` still work.
6. **/onboarding**: signed out, how onboarding works today. Operator: the operator account's checklist or a
   partner's (`?account=`), clearly labelled; six steps, each with a real completion state; keys created on /keys
   only; the preview runs only on click; GPUs typed as names and stored as slugs.
7. **/assistant**: one heading, what it answers (with example buttons), what it doesn't do, how to read observed
   vs estimated; recommendations as three pick cards; template cards rendered; examples come back after an
   unrecognised question; no engine labels; sign-in shown on actions that need it.

## Canonical configuration

- One environment variable: `OPENGRID_API_KEY` (read by `cli/src/opengrid_cli/client.py`). `OPENGRID_KEY` was a
  documentation typo on /api and is gone. `OPENGRID_URL` selects the server (the CLI default is
  `http://localhost:8000`, so every snippet sets it). `OPENGRID_ALLOWED_ROOTS` widens what the local MCP server
  may analyze.
- Snippets never contain a key: they reference the variable, VS Code's secret prompt, or the key file written by
  `opengrid auth login`. Local MCP configs carry no key at all.

## What was verified (2026-10-15)

| check | result |
|---|---|
| `uvx --from 'git+https://github.com/fzsheik/opengrid-terminal#subdirectory=cli' opengrid --version` | builds from the public repo; `opengrid 0.1.0` |
| `opengrid gpu search --model H100 --sort price`, no key, against production and a local `PUBLIC_PAGES` server | works |
| `opengrid analyze . --dry-run` on a sample project with `.env`, `model.safetensors`, `data/` | reads `requirements.txt`, `train.py`; skips the secret, the weights and the data dir |
| `opengrid recommend . --budget 50` without a terminal | refuses (exit 6), nothing sent; with `--yes` prints the requirements and the recommendation |
| MCP Inspector, uvx-launched local server, no key | 13 tools; `opengrid_analyze_project` works; home dir, a symlink pointing out, and `../` all refused |
| MCP Inspector, hosted `/mcp` with a key | 12 tools (no analyze); `opengrid_search_gpus` works; no key: 401 |
| Claude Code 2.1.301, `claude mcp add opengrid --scope user --transport stdio --env OPENGRID_URL=… -- uvx …` | Connected; a headless prompt called analyze + recommend with no key; user scope analyzes the folder Claude Code starts in |
| Claude Code, hosted `--header 'Authorization: Bearer ${OPENGRID_API_KEY}'` | Connected with the variable set; "Failed to connect" with it empty; no literal key in `~/.claude.json` |
| Codex CLI 0.159.0, local and hosted `codex mcp add …` in a temporary `CODEX_HOME` | writes the expected `config.toml`; no live Codex session was run |
| Cursor, VS Code, Claude Desktop | not installed here: snippets follow each client's documented format, and the page says "not yet tested" |
| claude.ai custom connectors | request headers are a limited beta; the page says so and points to Claude Desktop + local server |
| Production, anonymous | `/v1/gpus` 200, `/v1/scopes` 200, `/openapi.json` 401, `/v1/me` 401, `/mcp` 401; assistant engine `deterministic` |
| Browser (headless Chrome), anonymous | each page renders one h1, no "Sign-in required" box, no page errors; /api Try it on `/v1/best/{gpu}` returns 200 with no credentials; no horizontal overflow at 390 px |
| Browser, operator | read-only key created, shown once, works over the API (`data:read`), revoked → 401; elevated scope needs the acknowledgement; account tabs load without errors; full schema listed; onboarding preview on click only, profile save stores slugs and does not re-run it |

Tests: `node tests/test_web_developers.js` (snippets, access states, catalogue, presets, GPU names, assistant
capabilities) and `.venv/bin/python tests/test_developer_docs.py` (catalogue == the server's public routes;
`/openapi.json`, `/docs` and `/mcp` refuse anonymous callers; every `opengrid …` command on the page parses with the
real CLI parser; every assistant example reaches a real intent).

## The `/openapi.json` decision

Audited, kept: `api/pages.py` never lists `/openapi.json` or `/docs` as public, `tests/test_pages.py` asserts
both are denied, and `methodology/security.md` calls `/docs` operator-only. The schema includes admin and ops
routes. The curated reference on /api replaces it for everyone else, and `tests/test_developer_docs.py` keeps
the curated list identical to what the server serves without a key.

## Requests for the navigation / shell owner (Session 1)

1. **Load `web/devx.css` from `web/index.html`** (`<link rel="stylesheet" href="/static/devx.css">` after
   app.css). Today `web/pages/developers.js` injects it at load; that works (CSP allows `'self'` styles) but can
   flash unstyled for a frame. Or merge it into app.css. Afterwards drop the injection block in developers.js.
2. **Make the assistant discoverable**: a nav item or a command-bar entry for `/assistant` (it is reachable today
   only by URL, from /developers, and embedded on /find and /jobs). `OG.page("/assistant")` still sets
   `nav: "find"`; change it if the assistant gets its own nav item.
3. **Nav labels**: the Developers group says "API docs"; the page title is now "API reference".
4. **The shell's `GET /v1/me`** on every page logs a 401 in the console for anonymous visitors. Pages handle it
   (`OG.me()` → null); a quieter option is a public `/v1/me` that answers `{principal: "public"}`.
5. **`app.css` dead rules**: the `.pg-dev .dv-*` block belongs to an older /developers and is no longer used
   (the new page uses `.pg-dx` / `.dx-*` from devx.css).
6. **`api/pages.py` PAGES descriptions** (SEO text) for `/api` ("…with one key") and `/keys` could say that
   public data needs no key.

## Remaining obstacles to self-service adoption

- **No self-service accounts or keys.** Every key is issued by the operator. The only verified request channel is
  a public GitHub issue; a private channel (form, email, OAuth sign-up) needs a product decision.
  The link is one constant, `ACCESS_URL` in `web/pages/developers.js`.
- **The hosted MCP endpoint needs a key even for public data**, and claude.ai connectors can't send a header
  for most accounts. The keyless path is the local server (uv + git).
- **No PyPI package**: the CLI and local server install from GitHub through uv (git required).
- **Real execution is not available**: every provider adapter is simulated; real runs, when enabled, are the MNIST
  template only, with operator approval. No custom code.
- **The assistant is deterministic in production** (`ASSISTANT_LLM_PROVIDER` unset). It answers a fixed set of
  question types; the page says so. Mid-conversation, an unrelated message can be taken as the answer to the
  engine's pending follow-up question (backend behaviour in `assistant/engine.py`, not changed here).
- **Cursor, VS Code and Claude Desktop snippets are untested in those apps** (marked on the page).
