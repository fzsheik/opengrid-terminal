/* /developers — three ways to use OpenGrid outside the browser, kept apart on purpose:
     1. the terminal (the `opengrid` CLI from the repository's cli/ package)
     2. an AI coding assistant (the same package's MCP server: `opengrid mcp`, stdio)
     3. an application (the REST API; the full reference stays at /api)
   Each says what it enables, what access it needs and whether a key is required. Config snippets reference
   environment variables / prompted inputs only: never a literal key. Static page, no API calls. */
(() => {
  const { h } = OG;
  const KEY_VAR = "OPENGRID_API_KEY";
  const TEST_PROMPT = "Show me the cheapest available H100 options through OpenGrid";

  // [tool, what it does, credential needed]
  const TOOLS = [
    ["opengrid_search_gpus", "Search GPU markets by model, VRAM or price: lowest observed price, providers, availability", "none on a public deployment (else data:read)"],
    ["opengrid_compare_gpus", "Compare GPUs side by side: price, availability, hardware", "none on a public deployment (else data:read)"],
    ["opengrid_get_gpu_specs", "Hardware specs of one GPU: VRAM, bandwidth, interconnect, peak FLOPS", "none on a public deployment (else data:read)"],
    ["opengrid_get_market_context", "Price history, spread across providers and recent market events for a GPU", "none on a public deployment (else data:read)"],
    ["opengrid_analyze_project", "Read the local project (allowlisted files only) to infer framework, model and training setup", "none: runs on your machine, sends nothing"],
    ["opengrid_estimate_requirements", "Estimate GPU memory and GPU count for a workload (POST /v1/recommend/estimate)", "none by design"],
    ["opengrid_recommend_compute", "Ranked GPU options that fit the workload, with cost scenarios (POST /v1/recommend)", "none by design"],
    ["opengrid_preview_route", "Dry-run a route: what OpenGrid would launch, at what price, and what blocks it. Free; no provider is called", "key with route:preview"],
    ["opengrid_list_jobs", "Your training jobs and their state", "key with deployments:read"],
    ["opengrid_get_job_status", "One job: lifecycle, progress, cost, deployment state", "key with deployments:read"],
    ["opengrid_cancel_job", "Cancel one of your jobs (asks you to confirm)", "key with route:execute"],
  ];

  function code(text, label) {
    return h("div", { class: "dv-code" },
      label ? h("div", { class: "dv-code-h" }, h("span", { class: "mono dim" }, label), OG.copy(text)) : null,
      h("pre", { class: "w4-code" }, text),
      label ? null : h("div", { class: "dv-code-c" }, OG.copy(text)));
  }
  // JSON with arrays of scalars on one line ("args": ["mcp"]), as people write config files by hand
  const pretty = obj => JSON.stringify(obj, null, 2).replace(/\[\s*([^\[\]{}]*?)\s*\]/g, (m, inner) => "[" + inner.split(/,\s*/).join(", ") + "]");
  const kv = rows => h("table", { class: "dep-kv dv-kv" }, rows.map(([k, v]) => h("tr", {}, h("th", {}, k), h("td", {}, v))));
  const sec = (id, n, title, badge, ...kids) => h("section", { class: "dv-sec", id },
    h("header", { class: "dv-sec-h" }, h("span", { class: "dv-n mono" }, n), h("h2", {}, title), badge), kids);

  OG.page("/developers", {
    title: "Developers",
    mount(el, params, query, ctx) {
      const root = h("div", { class: "pg-dev" });
      el.append(root);
      const base = ((OG.boot && OG.boot.base_url) || location.origin).replace(/\/$/, "");
      const fromRepo = () => h("span", { class: "badge warn", title: "Installed from the repository's cli/ package; not yet published to PyPI" }, "from the repo · cli/");

      root.append(OG.head("Developers", "Use OpenGrid where you already work: your terminal, your AI coding assistant, or your own application",
        h("a", { class: "btn", href: "/api" }, "API reference"), h("a", { class: "btn pri", href: "/keys" }, "Get an API key →")));

      const choice = (href, n, title, line, enables, key) => h("a", { class: "dv-ch", href },
        h("span", { class: "dv-ch-t" }, h("span", { class: "dv-n mono" }, n), title),
        h("span", { class: "dv-ch-l" }, line),
        h("span", { class: "dv-ch-k" }, h("span", { class: "dimmer" }, "Enables "), enables),
        h("span", { class: "dv-ch-k" }, h("span", { class: "dimmer" }, "Key "), key));
      root.append(h("nav", { class: "dv-choices", "aria-label": "Choose an integration" },
        choice("#cli", "1", "Use OpenGrid in your terminal", "The opengrid CLI: search GPUs, analyze a project, get a recommendation.",
          "search · analyze · recommend · preview · jobs", "not for market data or local analysis"),
        choice("#mcp", "2", "Connect your AI coding assistant", "MCP server for Claude Code, Cursor, Codex or VS Code.",
          "your assistant calls OpenGrid tools", "only for previews and jobs"),
        choice("#api", "3", "Integrate OpenGrid into your application", "REST API: JSON over HTTPS with a {data, meta} envelope.",
          "market data, recommendations, routing, jobs", "recommended; required for private data")));

      // ---------- 1. CLI ----------
      root.append(sec("cli", "1", "Use OpenGrid in your terminal", fromRepo(),
        h("div", { class: "dv-cols" },
          h("div", {},
            kv([
              ["Enables", "GPU price search and comparison, a local analysis of your project (framework, model, training setup), and a workload recommendation from the same engine as the website."],
              ["Access", h("span", {}, h("code", { class: "mono" }, "opengrid analyze"), " reads allowlisted files in the directory you name, on your machine. It never runs project code, never reads secrets, weights or datasets, and sends nothing. Other commands call ", h("code", { class: "mono" }, base), ".")],
              ["Credentials", h("span", {}, "None for market data on a deployment with public pages on, or for local analysis. Previews and jobs need ", h("code", { class: "mono" }, KEY_VAR), " with the right scopes (", h("a", { class: "lnk", href: "/keys" }, "API keys"), ").")],
              ["Install", h("span", {}, "From the repository with ", h("a", { class: "lnk", href: "https://docs.astral.sh/uv/", target: "_blank", rel: "noopener external" }, "uv"), ". ", h("b", {}, "Not yet published to PyPI."))],
            ])),
          h("div", {},
            code("uv tool install ./cli", "install · at the root of an OpenGrid repository checkout"),
            code(`export OPENGRID_URL=${base}
export ${KEY_VAR}=…   # optional: only for previews and jobs

opengrid doctor                    # checks the URL, the key and connectivity
opengrid gpu search --model H100   # live prices across providers
opengrid analyze .                 # local only: what this project needs
opengrid recommend .               # GPUs that fit it, ranked`, "quickstart")))));

      // ---------- 2. MCP ----------
      const claudeAdd = `claude mcp add --transport stdio \\
  --env OPENGRID_URL=${base} \\
  opengrid -- opengrid mcp`;
      const mcpJson = pretty({ mcpServers: { opengrid: { command: "opengrid", args: ["mcp"], env: { OPENGRID_URL: base, [KEY_VAR]: "${" + KEY_VAR + "}" } } } });
      const cursorJson = pretty({ mcpServers: { opengrid: { command: "opengrid", args: ["mcp"], env: { OPENGRID_URL: base, [KEY_VAR]: "${env:" + KEY_VAR + "}" } } } });
      const codexAdd = `codex mcp add opengrid \\
  --env OPENGRID_URL=${base} \\
  -- opengrid mcp`;
      const codexToml = `[mcp_servers.opengrid]
command = "opengrid"
args = ["mcp"]
env = { OPENGRID_URL = "${base}" }
env_vars = ["${KEY_VAR}"]`;
      const vscodeJson = pretty({
        servers: { opengrid: { type: "stdio", command: "opengrid", args: ["mcp"], env: { OPENGRID_URL: base, [KEY_VAR]: "${input:opengrid-key}" } } },
        inputs: [{ type: "promptString", id: "opengrid-key", description: "OpenGrid API key", password: true }],
      });
      const clients = {
        claude: h("div", {}, code(claudeAdd, "Claude Code · command (market tools; no key)"),
          code(mcpJson, ".mcp.json · project-level, with your key from the environment"),
          h("p", { class: "note" }, "Claude Code expands ", h("code", { class: "mono" }, "${" + KEY_VAR + "}"), " from the environment it was started in; the key is never written into the file.")),
        cursor: h("div", {}, code(cursorJson, ".cursor/mcp.json"),
          h("p", { class: "note" }, "Cursor resolves ", h("code", { class: "mono" }, "${env:" + KEY_VAR + "}"), " from the environment Cursor was launched from.")),
        codex: h("div", {}, code(codexAdd, "Codex CLI"), code(codexToml, "~/.codex/config.toml"),
          h("p", { class: "note" }, h("code", { class: "mono" }, "env_vars"), " forwards ", h("code", { class: "mono" }, KEY_VAR), " from your shell to the server; nothing secret is stored in the file.")),
        vscode: h("div", {}, code(vscodeJson, ".vscode/mcp.json"),
          h("p", { class: "note" }, "VS Code prompts for the key once and stores it in its secret storage (", h("code", { class: "mono" }, "password: true"), ").")),
      };
      const clientBox = h("div", { class: "dv-client" }, clients.claude);
      const tabs = OG.seg([["claude", "Claude Code"], ["cursor", "Cursor"], ["codex", "Codex"], ["vscode", "VS Code"]], "claude", v => clientBox.replaceChildren(clients[v]));

      root.append(sec("mcp", "2", "Connect your AI coding assistant", fromRepo(),
        h("div", { class: "dv-cols" },
          h("div", {},
            kv([
              ["Enables", "Your assistant answers GPU questions with live OpenGrid data and can size your project: “what does this repo need, and where is it cheapest?”. Every number comes from an OpenGrid tool result."],
              ["Access", h("span", {}, "A local stdio process (", h("code", { class: "mono" }, "opengrid mcp"), ") started by your assistant. It talks only to ", h("code", { class: "mono" }, base), ". Project analysis reads allowlisted files on your machine; only the derived requirements are sent, and only when you ask for a recommendation.")],
              ["Credentials", "None for the market and recommendation tools on a deployment with public pages on. Previews and job tools need a key with the scopes listed below. There is no tool that launches paid compute."],
              ["Requires", h("span", {}, "The CLI installed (section 1) and ", h("code", { class: "mono" }, "opengrid"), " on the PATH your assistant sees.")],
            ]),
            h("h3", { class: "sec-h dv-h3" }, "Tools"),
            h("table", { class: "grid-t compact dv-tools" },
              h("thead", {}, h("tr", {}, h("th", {}, "Tool"), h("th", {}, "What it does"), h("th", {}, "Needs"))),
              h("tbody", {}, TOOLS.map(([n, d, k]) => h("tr", {}, h("td", { class: "mono" }, n), h("td", {}, d), h("td", { class: /^none/.test(k) ? "dim" : "warn-t" }, k)))))),
          h("div", {},
            h("div", { class: "bar dv-tabs" }, tabs),
            clientBox,
            h("h3", { class: "sec-h dv-h3" }, "Test the connection"),
            h("p", { class: "note" }, "After restarting the assistant, ask:"),
            code(TEST_PROMPT, "prompt"),
            h("p", { class: "note" }, "A working setup answers with current H100 prices and provider names from ", h("code", { class: "mono" }, "opengrid_search_gpus"), "; compare them with ", h("a", { class: "lnk", href: "/gpu/h100" }, "the H100 page"), ".")))));

      // ---------- 3. API ----------
      const auth = `-H "Authorization: Bearer $${KEY_VAR}"`;
      const recBody = { workload_type: "fine_tuning", model: { name: "meta-llama/Llama-3.1-8B", parameters_b: 8.03 }, precision: "bf16",
        training: { method: "lora", seq_len: 2048, micro_batch_size: 4 }, constraints: { budget_usd: 50 }, source: "api", limit: 5 };
      root.append(sec("api", "3", "Integrate OpenGrid into your application", h("span", { class: "badge" }, "REST · JSON"),
        h("div", { class: "dv-cols" },
          h("div", {},
            kv([
              ["Enables", "Market data (GPUs, providers, history, indices, events), best-execution rankings, workload recommendations, route previews, jobs and deployments, from your own code."],
              ["Access", h("span", {}, "HTTPS to ", h("code", { class: "mono" }, base + "/v1"), ". Responses are ", h("code", { class: "mono" }, "{data, meta}"), "; ", h("code", { class: "mono" }, "meta.kind"), " says whether a number is observed or estimated, and a value OpenGrid cannot compute is ", h("code", { class: "mono" }, "null"), " with a reason.")],
              ["Credentials", h("span", {}, "Read-only market data needs no key when the deployment has public pages on (rate-limited per IP); otherwise a key with ", h("code", { class: "mono" }, "data:read"), ". Previews need ", h("code", { class: "mono" }, "route:preview"), ", jobs ", h("code", { class: "mono" }, "deployments:read"), " / ", h("code", { class: "mono" }, "route:execute"), ". Send ", h("code", { class: "mono" }, "Authorization: Bearer opg_…"), ".")],
              ["Reference", h("span", {}, h("a", { class: "lnk", href: "/api" }, "Full API docs"), " (every endpoint, scopes, try-it) · ", h("a", { class: "lnk", href: "/docs", rel: "external" }, "OpenAPI explorer"), " · ", h("a", { class: "lnk", href: "/openapi.json", rel: "external" }, "openapi.json"))],
            ])),
          h("div", {},
            code(`curl -s "${base}/v1/gpus?priced_only=true&limit=5"`, "GET /v1/gpus · every GPU with its current market"),
            code(`curl -s "${base}/v1/best/h100-80gb-sxm5?count=1"`, "GET /v1/best/{gpu} · transparent best-execution ranking"),
            code(`curl -s -X POST "${base}/v1/recommend" \\
  ${auth} \\
  -H "Content-Type: application/json" \\
  -d '${pretty(recBody)}'`, "POST /v1/recommend · GPUs that fit a workload"),
            h("p", { class: "note" }, "Recommendations are read-only: they never call a provider or reserve anything. Prices are observed list prices, not quotes; runtime is unknown unless measured. ",
              h("a", { class: "lnk", href: "/methodology/data-kinds" }, "Data kinds"), ".")))));

      // ---------- troubleshooting ----------
      const tr = (q, a) => h("details", { class: "dv-tr" }, h("summary", {}, q), h("div", { class: "dv-tr-b" }, a));
      root.append(h("section", { class: "dv-sec", id: "troubleshooting" },
        h("header", { class: "dv-sec-h" }, h("h2", {}, "Troubleshooting")),
        h("div", { class: "dv-trs" },
          tr(h("span", {}, h("code", { class: "mono" }, "opengrid: command not found")), h("p", {}, "uv installs tools into its bin directory (usually ", h("code", { class: "mono" }, "~/.local/bin"), "). Run ", h("code", { class: "mono" }, "uv tool update-shell"), " and open a new terminal.")),
          tr("The assistant does not list the OpenGrid tools", h("p", {}, "Restart the assistant after changing its config. Desktop editors often do not inherit your shell's PATH: put the absolute path from ", h("code", { class: "mono" }, "which opengrid"), " in ", h("code", { class: "mono" }, "command"), ". In Claude Code, ", h("code", { class: "mono" }, "claude mcp list"), " shows the server's status.")),
          tr("401 Unauthorized", h("p", {}, "No key, or the key is invalid, revoked or expired. If market data also returns 401, this deployment is private (public pages off): every call needs a key with ", h("code", { class: "mono" }, "data:read"), ". Check that ", h("code", { class: "mono" }, KEY_VAR), " is set in the environment that starts the CLI or editor (", h("code", { class: "mono" }, "opengrid doctor"), " reports it).")),
          tr("403 Forbidden", h("p", {}, "The key lacks the scope (for example ", h("code", { class: "mono" }, "route:preview"), " for previews). Create a key with the scopes you need on ", h("a", { class: "lnk", href: "/keys" }, "API keys"), "; the secret is shown once.")),
          tr("429 Too Many Requests", h("p", {}, "Over the rate limit. Anonymous callers are limited per IP; a key has its own limit. Back off and retry later.")),
          tr("A POST from a browser session is refused (cross-site request refused)", h("p", {}, "State-changing requests authenticated by the site login must come from the OpenGrid web app. From scripts and servers, use an API key: ", h("code", { class: "mono" }, "Authorization: Bearer opg_…"), ".")),
          tr("Prices look different from a provider's site", h("p", {}, "OpenGrid shows observed list prices per GPU-hour, labelled with their age; a provider can change prices between polls. ", h("a", { class: "lnk", href: "/methodology/data-kinds" }, "How prices are observed"), ".")))));
    },
  });
})();
