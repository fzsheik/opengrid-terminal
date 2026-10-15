# OpenGrid 2.0: run it locally, demo script, what is real

## Run locally

```sh
docker start opengrid-pg                       # local Postgres (see "Local database" below)
DATABASE_URL=postgresql+psycopg://postgres@localhost:5432/opengrid \
  .venv/bin/uvicorn main:app --port 8000       # migrations (incl. 0020_intelligence) run on start
```
Open http://localhost:8000. With no `APP_PASSWORD` on your own machine every request is the operator
(the free training simulation needs that, or a key with `route:execute`).
Add `POLLER_ENABLED=false OPENGRID_NO_JOBS=1` for a read-only server that doesn't poll providers.

### AI assistants (MCP), CLI
Hosted MCP, nothing to install: `<site>/mcp` (streamable HTTP, `Authorization: Bearer <API key>`; anonymous and
site-password requests are refused). Create a key from /developers (step 1 links to `/keys?preset=assistant`).
```sh
claude mcp add --scope user --transport http opengrid http://localhost:8000/mcp \
  --header 'Authorization: Bearer ${OPENGRID_API_KEY}'
```
Local MCP server (adds `opengrid_analyze_project`, reads your project folder on your machine), zero install with uv + git:
```sh
uvx --from "git+https://github.com/fzsheik/opengrid-terminal#subdirectory=cli" opengrid mcp
# or install from the repo: uv tool install ./cli   (after a pull: add --force --reinstall)
export OPENGRID_URL=http://localhost:8000
opengrid doctor
opengrid gpu search --model H100
opengrid analyze cli/tests/fixtures/projects/llama7b_lora --dry-run   # what would be read; reads nothing
opengrid recommend cli/tests/fixtures/projects/llama7b_lora           # shows what is sent, asks first
opengrid mcp config claude                     # hosted / uvx / installed snippets for claude, cursor, codex, vscode
```
Then ask "Find the cheapest H100 on OpenGrid" or "How are my OpenGrid jobs doing?". Tools: 12 hosted, 13 local
(no tool rents a paid GPU; `opengrid_start_demo` runs the free simulation). Full host setup: cli/README.md.

## Ten-minute demo

1. **/**: headline, three actions, live board of popular GPUs (observed prices).
2. **GPUs → H100 80GB SXM5**: the market terminal, with index level, providers, chart, regions, events (unchanged).
3. **Find Compute**: "Fine-tune", model "Llama 3.1 8B", LoRA, budget $50 → memory estimate with components and
   assumptions, three picks (cheapest advertised / best for your workload / best OpenGrid can run; the last is
   empty with the reason: only the MNIST demo is runnable), tiers, cost scenarios, "runtime: unknown".
4. **Ask in plain English** (on Find Compute): "What's the cheapest available H100?" → per-variant prices.
5. **Preview route** from an option → the existing order ticket (`/route`), preview only.
6. **My Jobs**: type "try a free demo" in the box → click "Start free simulation" (or New job → "Start free demo") → the job page walks Preparing → Running → Saving results →
   Complete, says it is simulated and that no money was spent; Advanced details hold logs and state history.
7. **My Jobs box**: "how are my jobs doing?", "train mnist on a real gpu" (check price, then confirm), "cancel my job" (asks to confirm).
8. **Terminal**: `opengrid analyze <project>` then `opengrid recommend <project>`: same picks as the website.
9. **Claude Code** (if connected): the two prompts above.

## Simulated vs real

| Capability | Status |
|---|---|
| Market data, GPU pages, indices, news | real (observed provider prices) |
| Memory estimate | real computation; theoretical (formulas in /methodology/recommendations), not measured |
| Recommendations | real market data; runtime and total cost are scenarios, never benchmarks |
| Project analysis | real, local only; only `requirements` is ever sent |
| Assistant (incl. the My Jobs box) | never changes anything itself: start-demo / cancel are buttons the user clicks; deterministic engine by default; optional LLM is off unless `ASSISTANT_LLM_PROVIDER` + `ANTHROPIC_API_KEY` are set |
| Training demo | SIMULATED: no GPU, no provider call, no spend |
| Route preview | real ranking; records a preview ticket; never provisions |
| Real provisioning | unchanged and gated: `ROUTING_LIVE_PROVISIONING`, execution mode, validated adapters, quotes, approval |
| Arbitrary user workloads | recommendation only; OpenGrid can't execute them |

## Local database
`opengrid-pg` is a Postgres 17 Docker container (`docker run -d --name opengrid-pg -e POSTGRES_HOST_AUTH_METHOD=trust
-e POSTGRES_DB=opengrid -p 5432:5432 -v opengrid-pgdata:/var/lib/postgresql/data postgres:17`) with a role named
after the OS user (tests connect that way). `deploy/pull_prod.sh` copies production history into it (read-only on
production).
