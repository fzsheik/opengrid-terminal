/* /developers: connect OpenGrid to an AI assistant in three steps (key, app, ask), simplest path first.
     Hosted MCP: <base>/mcp (streamable HTTP, Bearer API key). Nothing to install.
     Local MCP:  `uvx --from git+…#subdirectory=cli opengrid mcp` (stdio; adds project analysis on your machine).
     Terminal and REST API are folded below. Static page, no API calls.
   Snippets never contain a literal key: they reference the OPENGRID_API_KEY environment variable or a secure
   prompt. Client syntax verified against each client's docs (Claude Code, Cursor, VS Code, Codex, claude.ai). */
(() => {
  const { h } = OG;
  const KEY_VAR = "OPENGRID_API_KEY";
  const GIT = "git+https://github.com/fzsheik/opengrid-terminal#subdirectory=cli";
  const ASK = ["Find the cheapest H100 on OpenGrid", "Which GPU should I use to fine-tune Llama 3 8B? Use OpenGrid", "How are my OpenGrid jobs doing?"];

  // plain-language capabilities (the tools behind them are listed under "All tools")
  const CAN = [
    ["Find GPU prices", "live prices across 32 clouds, compare GPUs, specs, why a price moved"],
    ["Size your workload", "how much GPU memory a model needs and which GPUs fit, with cost scenarios"],
    ["Run the free demo", "start the simulated MNIST training job and follow it (no GPU, no spend)"],
    ["Check your jobs", "status, progress and logs of your jobs; cancel one when you ask"],
  ];
  // [tool, what it does, needs]
  const TOOLS = [
    ["opengrid_search_gpus", "Search GPUs by model, VRAM or price", "data:read"],
    ["opengrid_compare_gpus", "Compare two GPUs: price, availability, hardware", "data:read"],
    ["opengrid_get_gpu_specs", "Specs of one GPU", "data:read"],
    ["opengrid_get_market_context", "Price history, spread and recent events for a GPU", "data:read"],
    ["opengrid_estimate_requirements", "GPU memory and GPU count for a workload", "none"],
    ["opengrid_recommend_compute", "Ranked GPU options that fit a workload, with cost scenarios", "none"],
    ["opengrid_preview_route", "What OpenGrid would launch and at what price (records a preview; never launches)", "route:preview"],
    ["opengrid_list_jobs", "Your jobs and their state", "deployments:read"],
    ["opengrid_get_job_status", "One job: progress, cost, state", "deployments:read"],
    ["opengrid_get_job_logs", "A job's log lines", "deployments:read"],
    ["opengrid_start_demo", "Start the free MNIST simulation (no GPU, no spend)", "route:execute"],
    ["opengrid_cancel_job", "Cancel one of your jobs (only when you ask)", "route:execute"],
    ["opengrid_analyze_project", "Read your project folder to infer the model and setup (local server only; reads on your machine)", "none"],
  ];

  function code(text, label) {
    return h("div", { class: "dv-code" },
      h("div", { class: "dv-code-h" }, h("span", { class: "mono dim" }, label || ""), OG.copy(text)),
      h("pre", { class: "w4-code" }, text));
  }
  // JSON with arrays of scalars on one line ("args": ["mcp"]), as people write config files by hand
  const pretty = obj => JSON.stringify(obj, null, 2).replace(/\[\s*([^\[\]{}]*?)\s*\]/g, (m, inner) => "[" + inner.split(/,\s*/).join(", ") + "]");
  const b64 = s => (typeof btoa === "function" ? btoa(unescape(encodeURIComponent(s))) : "");
  const step = (n, title, ...kids) => h("section", { class: "dv-step" }, h("div", { class: "dv-step-n mono", "aria-hidden": "true" }, String(n)),
    h("div", { class: "dv-step-b" }, h("h2", {}, title), kids));
  const p = (...kids) => h("p", { class: "dv-p" }, kids);
  const c = t => h("code", { class: "mono" }, t);
  const more = (title, sub, ...kids) => h("details", { class: "dv-more" }, h("summary", {}, h("b", {}, title), sub ? h("span", { class: "dim" }, " " + sub) : null), h("div", { class: "dv-more-b" }, kids));

  // Each pure snippet builder is exported for tests (tests/test_web_nav.js loads this file under Node).
  function snippets(base) {
    const url = base + "/mcp";
    const bearerEnv = "Bearer ${" + KEY_VAR + "}";
    const local = { command: "uvx", args: ["--from", GIT, "opengrid", "mcp"] };
    return {
      url,
      claude: `claude mcp add --scope user --transport http opengrid ${url} \\
  --header 'Authorization: ${bearerEnv}'`,
      cursorJson: pretty({ mcpServers: { opengrid: { url, headers: { Authorization: "Bearer ${env:" + KEY_VAR + "}" } } } }),
      cursorLink: "cursor://anysphere.cursor-deeplink/mcp/install?name=opengrid&config=" + encodeURIComponent(b64(JSON.stringify({ url, headers: { Authorization: "Bearer ${env:" + KEY_VAR + "}" } }))),
      vscodeJson: pretty({
        inputs: [{ type: "promptString", id: "opengrid-key", description: "OpenGrid API key", password: true }],
        servers: { opengrid: { type: "http", url, headers: { Authorization: "Bearer ${input:opengrid-key}" } } },
      }),
      codex: `codex mcp add opengrid --url ${url} --bearer-token-env-var ${KEY_VAR}`,
      terminal: `export OPENGRID_URL=${base}
export ${KEY_VAR}=<paste your key>

uvx --from "${GIT}" opengrid gpu search --model H100`,
      install: `uv tool install "${GIT}"     # then just: opengrid …`,
      localClaude: `claude mcp add --scope user --transport stdio \\
  --env OPENGRID_URL=${base} \\
  --env '${KEY_VAR}=\${${KEY_VAR}}' \\
  opengrid -- uvx --from "${GIT}" opengrid mcp`,
      localJson: pretty({ mcpServers: { opengrid: Object.assign({}, local, { env: { OPENGRID_URL: base, [KEY_VAR]: "${env:" + KEY_VAR + "}" } }) } }),
      localCodex: `codex mcp add opengrid --env OPENGRID_URL=${base} -- uvx --from "${GIT}" opengrid mcp`,
      curl: `curl -s "${base}/v1/gpus?priced_only=true&limit=5" \\
  -H "Authorization: Bearer $${KEY_VAR}"`,
      curlRec: `curl -s -X POST "${base}/v1/recommend" \\
  -H "Authorization: Bearer $${KEY_VAR}" -H "Content-Type: application/json" \\
  -d '{"workload_type": "fine_tuning", "model": {"name": "meta-llama/Llama-3.1-8B"}, "training": {"method": "lora"}, "constraints": {"budget_usd": 50}}'`,
    };
  }
  OG.developers = { snippets, GIT, KEY_VAR };
  if (typeof OG.page !== "function" || typeof document === "undefined") return;

  OG.page("/developers", {
    title: "Developers",
    mount(el) {
      const root = h("div", { class: "pg-dev" });
      el.append(root);
      const base = ((OG.boot && OG.boot.base_url) || location.origin).replace(/\/$/, "");
      const S = snippets(base);

      root.append(OG.head("Connect your AI assistant", "Ask Claude, Cursor, VS Code or Codex about GPU prices, what your model needs, and your jobs. Three steps, nothing to install.",
        h("a", { class: "btn", href: "/api" }, "API reference")));

      // ---------- 1. key ----------
      root.append(step(1, "Get an API key",
        p("Create a key for your assistant. It can read prices and your jobs; it cannot rent a GPU."),
        h("div", { class: "dv-row" }, h("a", { class: "btn pri dv-big", href: "/keys?preset=assistant" }, "Create an assistant key →")),
        p("The key is shown once. Save it in your terminal profile (", c("~/.zshrc"), " or ", c("~/.bashrc"), "), then open a new terminal:"),
        code(`export ${KEY_VAR}=<paste your key>`, "~/.zshrc")));

      // ---------- 2. app ----------
      const apps = {
        claude: h("div", {}, p("Run this once in a terminal:"), code(S.claude, "Claude Code"),
          p("Check it with ", c("claude mcp list"), ". The single quotes keep the key out of the config file.")),
        cursor: h("div", {}, h("div", { class: "dv-row" }, h("a", { class: "btn pri", href: S.cursorLink }, "Add to Cursor"), h("span", { class: "dim" }, "or paste this into ", c("~/.cursor/mcp.json"), ":")),
          code(S.cursorJson, "~/.cursor/mcp.json"), p("Cursor reads ", c(KEY_VAR), " from the environment it was started in. Restart Cursor after saving.")),
        vscode: h("div", {}, p("Put this in ", c(".vscode/mcp.json"), " in your project. VS Code asks for the key once and keeps it in its secret storage."),
          code(S.vscodeJson, ".vscode/mcp.json")),
        codex: h("div", {}, p("Run this once in a terminal:"), code(S.codex, "Codex CLI"), p("Codex reads the key from ", c(KEY_VAR), " each time it starts.")),
        claudeai: h("div", {}, h("ol", { class: "dv-ol" },
          h("li", {}, "Open ", h("b", {}, "Settings → Connectors → Add custom connector"), " in claude.ai or the Claude desktop app."),
          h("li", {}, "URL: ", c(S.url), " ", OG.copy(S.url)),
          h("li", {}, "Add a request header ", c("Authorization"), " with the value ", c("Bearer <your key>"), ".")),
          p("Calls come from Anthropic's servers, so this works on any device you use Claude on.")),
        terminal: h("div", {}, p("No assistant? Use OpenGrid straight from your terminal. Needs ", h("a", { class: "lnk", href: "https://docs.astral.sh/uv/", target: "_blank", rel: "noopener external" }, "uv"), " and git."),
          code(S.terminal, "terminal"), code(S.install, "optional: install it")),
      };
      const box = h("div", { class: "dv-client" }, apps.claude);
      const tabs = OG.seg([["claude", "Claude Code"], ["cursor", "Cursor"], ["vscode", "VS Code"], ["codex", "Codex"], ["claudeai", "claude.ai / Desktop"], ["terminal", "Terminal"]], "claude",
        v => box.replaceChildren(apps[v]));
      root.append(step(2, "Add OpenGrid to your app", h("div", { class: "bar dv-tabs" }, tabs), box));

      // ---------- 3. ask ----------
      root.append(step(3, "Ask",
        p("Restart your app, then try:"),
        h("div", { class: "dv-asks" }, ASK.map(q => h("div", { class: "dv-ask" }, h("span", {}, "“" + q + "”"), OG.copy(q)))),
        h("ul", { class: "dv-can" }, CAN.map(([t, d]) => h("li", {}, h("b", {}, t), h("span", { class: "dim" }, " " + d)))),
        p(h("span", { class: "dim" }, "Every number comes from OpenGrid's live data. Your assistant can never rent a paid GPU: real runs are started and approved on this website."))));

      // ---------- more ----------
      root.append(h("div", { class: "dv-mores" },
        more("Analyze a project on your computer", "(local server: reads your project folder on your machine, sends only the derived requirements)",
          p("The hosted server can't see your files. To let your assistant size a project in a folder, run OpenGrid's MCP server locally instead. Needs uv and git."),
          code(S.localClaude, "Claude Code"), code(S.localJson, "Cursor (~/.cursor/mcp.json) and other JSON configs"), code(S.localCodex, "Codex CLI"),
          p("It reads allowlisted files only (code, configs, requirements), never secrets, weights or datasets, and never runs your code.")),
        more("Use it from your own code", "(REST API, JSON over HTTPS)",
          code(S.curl, "GET /v1/gpus · every GPU with its current market"), code(S.curlRec, "POST /v1/recommend · GPUs that fit a workload"),
          p("Responses are ", c("{data, meta}"), ". ", h("a", { class: "lnk", href: "/api" }, "Full API reference"), " · ", h("a", { class: "lnk", href: "/openapi.json", rel: "external" }, "openapi.json"))),
        more("All tools", "(what your assistant can call)",
          h("table", { class: "grid-t compact dv-tools" },
            h("thead", {}, h("tr", {}, h("th", {}, "Tool"), h("th", {}, "What it does"), h("th", {}, "Key permission"))),
            h("tbody", {}, TOOLS.map(([n, d, k]) => h("tr", {}, h("td", { class: "mono" }, n), h("td", {}, d), h("td", { class: k === "none" ? "dim" : k === "route:execute" ? "warn-t" : "" }, k)))))),
        more("Troubleshooting", null,
          h("dl", { class: "dv-faq" },
            h("dt", {}, "The assistant doesn't list OpenGrid's tools"), h("dd", {}, "Restart the app after adding the server. In Claude Code, ", c("claude mcp list"), " shows its status."),
            h("dt", {}, "401 Unauthorized"), h("dd", {}, "The key is missing, mistyped, revoked or expired. Check that ", c(KEY_VAR), " is set where the app starts (", c("echo $" + KEY_VAR), "), or create a new key."),
            h("dt", {}, "403 Forbidden"), h("dd", {}, "The key lacks a permission, e.g. ", c("route:execute"), " to start the demo. ", h("a", { class: "lnk", href: "/keys" }, "Create a key"), " with it."),
            h("dt", {}, "uvx: command not found, or a git error"), h("dd", {}, "Install ", h("a", { class: "lnk", href: "https://docs.astral.sh/uv/getting-started/installation/", target: "_blank", rel: "noopener external" }, "uv"), " and git. Desktop apps may need the full path from ", c("which uvx"), " as the command."),
            h("dt", {}, "ChatGPT"), h("dd", {}, "ChatGPT connects only to servers with OAuth sign-in, which OpenGrid doesn't offer yet. Use one of the apps above."),
            h("dt", {}, "429 Too Many Requests"), h("dd", {}, "Over the rate limit. Wait a minute and try again.")))));
    },
  });
})();
