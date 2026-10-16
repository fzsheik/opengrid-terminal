/* /developers: three ways to use OpenGrid (AI coding assistant over MCP, the terminal CLI, the REST API), two
   first successes, honest access states, verified client setup, capability boundaries and troubleshooting.

   Facts this page relies on (checked against the code; keep in sync):
     - public without a key (PUBLIC_PAGES, api/pages.py): GET market data under /v1 (gpus, markets, best, ...),
       POST /v1/recommend, /v1/recommend/estimate, /v1/assistant; rate limited per IP.
     - the hosted MCP endpoint <base>/mcp ALWAYS needs `Authorization: Bearer <key>` (api/mcp_remote.py), and it
       cannot read local files (no opengrid_analyze_project).
     - the local MCP server and the CLI (cli/, run with uvx from the public GitHub repo; needs uv and git) work
       WITHOUT a key for public data, project analysis and recommendations; a key comes from OPENGRID_API_KEY or
       `opengrid auth login`. Default server is localhost, so OPENGRID_URL is always set in the snippets.
     - keys are issued by the operator (no self-service sign-up): /keys (operator account) or /admin/partners
       (a partner's own account + first key). A key holding account:manage can mint narrower keys (POST /v1/keys).
     - no tool, command or endpoint runs arbitrary code; real GPU runs are not available yet (every provider
       adapter is simulated); the free MNIST sandbox demo is simulated end to end.
   Snippets never contain a literal key: they reference OPENGRID_API_KEY, a VS Code secret prompt, or the key file
   written by `opengrid auth login`.

   Also defines OG.dx: small shared pieces for /api, /keys, /onboarding and /assistant (this file loads first),
   and injects web/devx.css (feature styles; web/index.html is owned by the navigation work). */
(() => {
  const { h } = OG;
  const KEY_VAR = "OPENGRID_API_KEY";
  const REPO = "https://github.com/fzsheik/opengrid-terminal";
  const GIT = "git+" + REPO + "#subdirectory=cli";
  // The one verified public contact channel: the repository's GitHub issues (public; no secrets).
  const ACCESS_TITLE = "API key request";
  const ACCESS_BODY = ["What I want to build with OpenGrid:", "", "Which access I need (hosted MCP endpoint, route previews, jobs):", "",
    "How to reach me (optional):", "", "(This issue is public. Do not paste secrets, keys or confidential details.)"].join("\n");
  const ACCESS_URL = REPO + "/issues/new?title=" + encodeURIComponent(ACCESS_TITLE) + "&body=" + encodeURIComponent(ACCESS_BODY);

  // What was run on 2026-10-15 (macOS) before this page was written; "untested" means the snippet follows the
  // client's documented format but OpenGrid has not run it in that client.
  const VERIFIED = {
    claude: [true, "Tested 2026-10-15 with Claude Code 2.1.301: the local server connected with no key and answered “analyze this project and recommend a GPU”; the hosted endpoint connected with a key, and failed to connect with the variable unset."],
    codex: [true, "Tested 2026-10-15 with Codex CLI 0.159.0: both commands write the expected entry to config.toml (registration checked with codex mcp get; no live Codex session was run)."],
    cursor: [false, "Format from Cursor's MCP documentation; not yet tested by OpenGrid in Cursor."],
    vscode: [false, "Format from VS Code's MCP documentation; not yet tested by OpenGrid in VS Code."],
    desktop: [false, "Format from Claude Desktop's MCP documentation; not yet tested by OpenGrid in Claude Desktop."],
  };

  // ------------------------------------------------------------ pure: snippets (tests/test_web_developers.js)
  const pretty = obj => JSON.stringify(obj, null, 2).replace(/\[\s*([^\[\]{}]*?)\s*\]/g, (m, inner) => "[" + inner.split(/,\s*/).join(", ") + "]");
  const b64 = s => (typeof btoa === "function" ? btoa(unescape(encodeURIComponent(s))) : Buffer.from(s, "utf8").toString("base64"));
  function snippets(base) {
    base = String(base || "").replace(/\/$/, "");
    const url = base + "/mcp";
    const uvx = ["--from", GIT, "opengrid", "mcp"];
    const run = `uvx --from "${GIT}" opengrid`;
    return {
      url, base,
      // ---- local server (stdio, launched by the client through uvx): no key needed for public data + analysis
      localClaude: `claude mcp add opengrid --scope user --transport stdio \\
  --env OPENGRID_URL=${base} \\
  -- uvx --from "${GIT}" opengrid mcp`,
      localCursor: pretty({ mcpServers: { opengrid: { command: "uvx", args: uvx, env: { OPENGRID_URL: base } } } }),
      localVscode: pretty({ servers: { opengrid: { type: "stdio", command: "uvx", args: uvx, env: { OPENGRID_URL: base, OPENGRID_ALLOWED_ROOTS: "${workspaceFolder}" } } } }),
      localCodex: `codex mcp add opengrid --env OPENGRID_URL=${base} \\
  -- uvx --from "${GIT}" opengrid mcp`,
      localDesktop: pretty({ mcpServers: { opengrid: { command: "uvx", args: uvx, env: { OPENGRID_URL: base, OPENGRID_ALLOWED_ROOTS: "/absolute/path/to/your/project" } } } }),
      // a key for the local server (optional): stored once, read by the CLI and the local MCP server
      authLogin: `export OPENGRID_URL=${base}
${run} auth login      # paste the key at the hidden prompt
${run} auth status     # shows the key prefix and its scopes`,
      // ---- hosted endpoint (streamable HTTP): always needs a key
      hostedClaude: `claude mcp add opengrid --scope user --transport http ${url} \\
  --header 'Authorization: Bearer \${${KEY_VAR}}'`,
      hostedCursor: pretty({ mcpServers: { opengrid: { url, headers: { Authorization: "Bearer ${env:" + KEY_VAR + "}" } } } }),
      cursorLink: "cursor://anysphere.cursor-deeplink/mcp/install?name=opengrid&config=" + encodeURIComponent(b64(JSON.stringify({ url, headers: { Authorization: "Bearer ${env:" + KEY_VAR + "}" } }))),
      hostedVscode: pretty({
        inputs: [{ type: "promptString", id: "opengrid-api-key", description: "OpenGrid API key", password: true }],
        servers: { opengrid: { type: "http", url, headers: { Authorization: "Bearer ${input:opengrid-api-key}" } } },
      }),
      hostedCodex: `codex mcp add opengrid --url ${url} \\
  --bearer-token-env-var ${KEY_VAR}`,
      keyEnv: `export ${KEY_VAR}=<paste your key here>     # e.g. in ~/.zshrc or ~/.bashrc, then open a new terminal`,
      // ---- terminal
      installUv: `curl -LsSf https://astral.sh/uv/install.sh | sh     # macOS / Linux (or: brew install uv)`,
      termTry: `export OPENGRID_URL=${base}
${run} gpu search --model H100 --available --sort price --limit 5`,
      termInstall: `uv tool install "${GIT}"
opengrid doctor          # checks the server, your key (if any) and the MCP server`,
      termAnalyze: `cd path/to/your/project
opengrid analyze . --dry-run      # lists the files it would read; opens none
opengrid recommend . --budget 50  # prints what it will send, asks before sending`,
      termExamples: `opengrid gpu show h100                      # specs and market; a family lists its variants
opengrid gpu compare h100-80gb-sxm5 h200-141gb-sxm5
opengrid gpu market h100-80gb-sxm5 --count 8 --region US
opengrid recommend --model meta-llama/Llama-3.1-8B --method lora --budget 50
opengrid providers --limit 10`,
      // ---- REST
      curlCheapest: `curl -s "${base}/v1/best/h100?mode=CHEAPEST&limit=1"`,
      curlRecommend: `curl -s -X POST "${base}/v1/recommend" \\
  -H "Content-Type: application/json" \\
  -d '{"workload_type": "fine_tuning", "model": {"name": "meta-llama/Llama-3.1-8B"}, "training": {"method": "lora"}, "constraints": {"budget_usd": 50}}'`,
      curlKeyed: `curl -s -X POST "${base}/v1/route/preview" \\
  -H "Authorization: Bearer $${KEY_VAR}" \\
  -H "Content-Type: application/json" \\
  -d '{"gpu": "h100-80gb-sxm5", "count": 1}'`,
      curlMe: `curl -s "${base}/v1/me" -H "Authorization: Bearer $${KEY_VAR}"`,
      curlMint: `curl -s -X POST "${base}/v1/keys" \\
  -H "Authorization: Bearer $${KEY_VAR}" \\
  -H "Content-Type: application/json" \\
  -d '{"name": "assistant", "scopes": ["data:read"]}'`,
    };
  }

  // The developer's access situation, from GET /v1/me (null for an anonymous visitor). In the browser the only
  // signed-in principal is the operator (site login); a key holder uses the key from the CLI, MCP or curl.
  function accessState(me) {
    if (!me) return "anonymous";
    if (me.principal === "operator" || (me.scopes || []).includes("*")) return "operator";
    return "key";
  }

  // [capability, CLI, local MCP, hosted MCP, REST, key needed]
  const BOUNDARIES = [
    ["GPU prices, specs, comparisons, price history", "yes", "yes", "yes", "yes", "No (hosted MCP: yes)"],
    ["Analyze a project folder on your computer", "yes", "yes", "no: it cannot see your files", "no: send the requirements instead", "No"],
    ["Estimate memory and recommend GPUs for a workload", "yes", "yes", "yes", "yes", "No (hosted MCP: yes)"],
    ["Preview a route: what would be picked, at what price (records an audit ticket; never launches)", "yes", "yes", "yes", "yes", "Yes: route:preview"],
    ["See your jobs, their status and logs", "yes", "yes", "yes", "yes", "Yes: deployments:read"],
    ["Free demo job: PyTorch MNIST, simulated end to end (no GPU, no spend)", "yes", "yes", "yes", "yes", "Yes: route:execute"],
    ["Cancel one of your jobs", "yes", "yes", "yes", "yes", "Yes: route:execute"],
    ["Run on a real GPU", "no", "no", "no", "no", "Not available yet"],
    ["Run your own code, container or model", "no", "no", "no", "no", "Not available"],
  ];
  // [tool, what it does, key scope, local only]
  const TOOLS = [
    ["opengrid_search_gpus", "Search GPUs by model, vendor, memory or observed price", "none (hosted: any key)"],
    ["opengrid_get_gpu_specs", "Specs and current market of one GPU (a family lists its variants)", "none (hosted: any key)"],
    ["opengrid_compare_gpus", "Two GPUs side by side: specs, prices, price per unit of capability", "none (hosted: any key)"],
    ["opengrid_get_market_context", "Price now vs its history, plus the best-available-now ranking", "none (hosted: any key)"],
    ["opengrid_analyze_project", "Read allowlisted files in a project folder and infer the workload (local server only)", "none", true],
    ["opengrid_estimate_requirements", "GPU memory and GPU count a workload needs", "none (hosted: any key)"],
    ["opengrid_recommend_compute", "Three picks and ranked options that fit, with cost scenarios", "none (hosted: any key)"],
    ["opengrid_preview_route", "What OpenGrid would select and its quote; records a preview, never launches", "route:preview"],
    ["opengrid_list_jobs", "Your jobs and their state", "deployments:read"],
    ["opengrid_get_job_status", "One job: progress, cost, state", "deployments:read"],
    ["opengrid_get_job_logs", "A job's log lines, in pages", "deployments:read"],
    ["opengrid_start_demo", "Start the free, simulated MNIST demo (no GPU, no spend)", "route:execute"],
    ["opengrid_cancel_job", "Cancel one of your jobs (only after you confirm)", "route:execute"],
  ];

  OG.developers = { snippets, accessState, GIT, KEY_VAR, ACCESS_URL, VERIFIED, BOUNDARIES, TOOLS };
  if (typeof OG.page !== "function" || typeof document === "undefined") return;

  // ------------------------------------------------------------ feature stylesheet (once per document)
  if (!document.querySelector('link[data-dx-css]')) {
    const l = document.createElement("link");
    l.rel = "stylesheet"; l.href = "/static/devx.css"; l.dataset.dxCss = "1";
    document.head.append(l);
  }

  // ------------------------------------------------------------ shared DOM pieces (OG.dx)
  const code = (text, label, where) => h("div", { class: "dx-code" },
    h("div", { class: "dx-code-h" }, h("span", { class: "dx-code-l" }, label || "shell"), where ? h("span", { class: "dx-code-w" }, where) : null,
      OG.copy(text, { title: "Copy " + (label || "code") })),
    h("pre", {}, text));
  const p = (...kids) => h("p", { class: "dx-p" }, kids);
  const c = t => h("code", {}, t);
  const ext = (href, text) => h("a", { class: "lnk", href, target: "_blank", rel: "noopener noreferrer external" }, text, " ↗");
  const call = (tone, ...kids) => h("div", { class: "dx-call " + tone, role: tone === "bad" ? "alert" : "note" }, kids);
  const yn = v => (v === "yes" ? h("span", { class: "dx-yes" }, "Yes") : v === "no" ? h("span", { class: "dx-no" }, "No") : /^no:/.test(v) ? h("span", { class: "dx-no" }, "No", h("span", { class: "dim" }, v.slice(2))) : v);
  // One honest statement of how to get access, for every page that needs a key.
  function accessCall(me, opts) {
    opts = opts || {};
    const st = accessState(me);
    if (st === "operator") return call("ok", h("b", {}, "Signed in as the operator. "),
      "Keys you create on API keys belong to the operator account. To give a developer their own account and key, use Design partners.",
      h("div", { class: "dx-row" }, h("a", { class: "btn pri", href: "/keys" }, "Create a key"), h("a", { class: "btn", href: "/admin/partners" }, "Issue a key to a developer")));
    return call("info", h("b", {}, opts.title || "API keys are issued by the OpenGrid team."),
      " There is no self-service sign-up yet. ", opts.lead || "You can start without one: public market data, project analysis and GPU recommendations work with no key.",
      " To ask for a key, open a request on OpenGrid's GitHub (it is public: don't include secrets).",
      h("div", { class: "dx-row" }, h("a", { class: "btn pri", href: ACCESS_URL, target: "_blank", rel: "noopener noreferrer external" }, "Request API access ↗"),
        opts.noStart ? null : h("a", { class: "btn", href: "/developers#quickstart" }, "Start without a key"),
        h("a", { class: "btn", href: OG.loginHref() }, "Operator sign in")));
  }
  OG.dx = { code, p, c, ext, call, accessCall, accessState, ACCESS_URL };

  // ------------------------------------------------------------ page
  OG.page("/developers", {
    title: "Developers",
    async mount(el, params, query, ctx) {
      const root = h("div", { class: "pg-dx" });
      el.append(root);
      const base = ((OG.boot && OG.boot.base_url) || location.origin).replace(/\/$/, "");
      const S = snippets(base);
      const accessBox = h("div");
      const sec = (id, title, sub, ...kids) => h("section", { class: "dx-sec", id }, h("div", { class: "dx-sec-h" }, h("h2", { class: "dx-h2" }, title), sub ? h("span", { class: "dx-sec-sub" }, sub) : null), kids);

      root.append(OG.head("Developers", "Use OpenGrid's live GPU market data and workload-aware recommendations from your AI coding assistant, your terminal or your own code.",
        h("a", { class: "btn", href: "/api" }, "API reference"), h("a", { class: "btn", href: "/assistant" }, "Ask the assistant")));

      // ---------- three paths ----------
      const path = (id, title, desc, facts, cta) => h("article", { class: "dx-path" },
        h("h2", { class: "dx-path-t" }, title), h("p", { class: "dx-path-d" }, desc),
        h("dl", { class: "dx-facts" }, facts.map(([k, v]) => [h("dt", {}, k), h("dd", {}, v)])),
        h("a", { class: "btn pri", href: "#" + id }, cta));
      root.append(h("div", { class: "dx-paths" },
        path("assistant", "Connect an AI coding assistant",
          "Claude Code, Cursor, Codex, VS Code or Claude Desktop calls OpenGrid's tools for you: live prices, GPU sizing and recommendations, and (on the local server) your project's own requirements.",
          [["Useful for", "asking in plain English inside your editor; answers come from live data"], ["You need", "an MCP client; for the local server also uv and git"],
            ["API key", h("span", {}, h("span", { class: "dx-yes" }, "Not needed"), " for the local server · needed for the hosted endpoint")], ["Install", "local server: uv runs it, nothing else · hosted: nothing"], ["Status", h("span", { class: "dx-yes" }, "Available now")]],
          "Set up your assistant"),
        path("terminal", "Use OpenGrid from your terminal",
          "The opengrid command: search and compare GPUs, analyze a project folder, get recommendations, preview routes and follow jobs. Every command has --json for scripts.",
          [["Useful for", "scripts, CI and quick lookups without a browser"], ["You need", "uv and git (uv fetches Python 3.11+ if needed)"],
            ["API key", h("span", {}, h("span", { class: "dx-yes" }, "Not needed"), " for market data, analysis and recommendations")], ["Install", "none to try (uvx); optional uv tool install"], ["Status", h("span", { class: "dx-yes" }, "Available now")]],
          "Set up the CLI"),
        path("rest", "Integrate OpenGrid into your application",
          "Plain JSON over HTTPS: market data, GPU recommendations for a workload, and (with a key) route previews and jobs.",
          [["Useful for", "dashboards, schedulers and your own tools"], ["You need", "any HTTP client"],
            ["API key", h("span", {}, h("span", { class: "dx-yes" }, "Not needed"), " for public market data and recommendations (rate limited per IP)")], ["Install", "nothing"], ["Status", h("span", { class: "dx-yes" }, "Available now")]],
          "Call the API")));
      root.append(accessBox);
      root.append(h("nav", { class: "dx-toc", "aria-label": "On this page" }, [["quickstart", "Quickstart"], ["assistant", "AI assistant (MCP)"], ["terminal", "Terminal (CLI)"], ["rest", "REST API"],
        ["access", "Access & API keys"], ["boundaries", "What OpenGrid can and can't do"], ["tools", "MCP tools"], ["troubleshooting", "Troubleshooting"]].map(([a, l]) => h("a", { href: "#" + a }, l))));

      // ---------- quickstart: two first successes ----------
      const win = (n, title, sub, tabsDef) => {
        const box = h("div");
        const tabs = OG.tabs(tabsDef.map(([k, l]) => [k, l]), tabsDef[0][0], v => box.replaceChildren(tabsDef.find(t => t[0] === v)[2]()));
        box.replaceChildren(tabsDef[0][2]());
        return h("div", { class: "dx-win" }, h("div", { class: "eyebrow" }, "First success " + n), h("h3", { class: "dx-h3", style: "margin-top:4px" }, title), sub ? p(sub) : null, tabs, box);
      };
      root.append(sec("quickstart", "Quickstart", "two results in a few minutes, no key needed",
        h("div", { class: "dx-wins" },
          win(1, "Find the cheapest available H100", "Uses public market data: no account, no key.", [
            ["rest", "curl", () => h("div", {}, code(S.curlCheapest, "curl", "any terminal"),
              p("Each H100 variant (PCIe, SXM5, NVL…) is listed separately in ", c("data.by_variant"), "; ", c("top[0]"), " is its cheapest fresh, in-stock listing: ", c("provider"), ", ", c("region"), ", ", c("price_per_gpu_hour"), ". Prices are observed list prices, not quotes."))],
            ["cli", "CLI", () => h("div", {}, code(S.termTry, "shell", "needs uv and git"), p("The first run downloads the client (cached afterwards). ", h("a", { class: "lnk", href: "#terminal" }, "Terminal setup →")))],
            ["ai", "AI assistant", () => h("div", {}, p("After ", h("a", { class: "lnk", href: "#assistant" }, "connecting your assistant"), ", ask:"),
              h("div", { class: "dx-asks" }, h("div", { class: "dx-ask" }, h("span", {}, "“Use OpenGrid to find the cheapest available H100.”"), OG.copy("Use OpenGrid to find the cheapest available H100."))),
              p("Or right here, without any setup: ", h("a", { class: "lnk", href: "/assistant?q=" + encodeURIComponent("Cheapest available H100") }, "ask the OpenGrid Assistant →")))],
          ]),
          win(2, "Analyze my project and recommend suitable GPUs", "Runs on your computer: the CLI or the local MCP server reads your project, OpenGrid recommends.", [
            ["cli", "CLI", () => h("div", {}, code(S.termAnalyze, "shell", "after installing the CLI"),
              p(c("recommend"), " prints the exact requirements it will send and asks before sending. Add ", c("--yes"), " in scripts."))],
            ["ai", "AI assistant", () => h("div", {}, p("With the ", h("b", {}, "local"), " server connected (the hosted one can't see your files), open your project and ask:"),
              h("div", { class: "dx-asks" }, h("div", { class: "dx-ask" }, h("span", {}, "“Use OpenGrid to analyze this project and recommend GPUs.”"), OG.copy("Use OpenGrid to analyze this project and recommend GPUs."))),
              p("The assistant calls ", c("opengrid_analyze_project"), " then ", c("opengrid_recommend_compute"), "."))],
            ["rest", "REST", () => h("div", {}, p("No files over the API: describe the workload yourself (or paste the ", c("requirements"), " that ", c("opengrid analyze . --json"), " prints)."),
              code(S.curlRecommend, "curl", "POST /v1/recommend · no key"))],
          ])),
        h("h3", { class: "dx-h3" }, "What project analysis reads, and what leaves your computer"),
        h("div", { class: "dx-scroll" }, h("table", { class: "dx-tbl" }, h("tbody", {},
          h("tr", {}, h("th", {}, "Reads"), h("td", {}, "Only allowlisted files inside the folder you name: dependency manifests (requirements.txt, pyproject.toml…), container and compose files, Hugging Face / PEFT / DeepSpeed / Accelerate and training configs, training or serving Python scripts and the local modules they import, notebook code cells, launch scripts, and the README (for the model name). Capped per file and in total; ", c(".gitignore"), " is respected.")),
          h("tr", {}, h("th", {}, "Never reads"), h("td", {}, "Secrets and credentials (", c(".env"), ", keys, ", c(".ssh"), ", ", c(".aws"), "…), model weights, datasets and ", c("data/"), " folders, checkpoints, logs, media, archives and binaries. Folders outside the allowed roots and symlinks pointing outside are refused. It never runs your code.")),
          h("tr", {}, h("th", {}, "Sends to OpenGrid"), h("td", {}, "Only the inferred workload requirements (workload type, frameworks, model name and size, method, precision, batch size, budget). Never file contents. The CLI prints them first and asks; ", c("opengrid analyze . --dry-run"), " lists the files without opening any.")),
          h("tr", {}, h("th", {}, "Allowed folders (MCP)"), h("td", {}, "The local MCP server only analyzes folders inside the directory the client started it in (never your home directory or /) plus any listed in ", c("OPENGRID_ALLOWED_ROOTS"), "."))))),
        h("h3", { class: "dx-h3" }, "From a requirement to a run: what each step does"),
        h("div", { class: "dx-ladder" },
          h("div", { class: "dx-step" }, OG.badge("no key", "good"), h("b", {}, "1 · Analyze requirements"), "Infer what your workload needs from your files or your description. Local; nothing is sent."),
          h("div", { class: "dx-step" }, OG.badge("no key", "good"), h("b", {}, "2 · Get a recommendation"), "Estimated memory and GPU count, three picks and ranked options from live prices. Read-only."),
          h("div", { class: "dx-step" }, OG.badge("key: route:preview", "warn"), h("b", {}, "3 · Preview a route"), "Which listing OpenGrid would select and its quote, with an audit id. Never calls a provider, never launches."),
          h("div", { class: "dx-step" }, OG.badge("not available yet", "bad"), h("b", {}, "4 · Execute"), "Real GPU runs are not available yet, and OpenGrid does not run your own code. The free MNIST demo is a simulation."))));

      // ---------- AI assistant ----------
      const verified = k => { const v = VERIFIED[k]; return h("p", { class: "dx-verified" + (v[0] ? "" : " untested") }, h("b", {}, v[0] ? "Verified · " : "Not yet tested · "), v[1]); };
      const mode = (kind, ...kids) => h("div", { class: "dx-mode" },
        h("div", { class: "dx-mode-h" }, h("b", {}, kind === "local" ? "Local server" : "Hosted endpoint"),
          kind === "local" ? [OG.badge("no key needed", "good"), OG.badge("reads your project"), OG.badge("needs uv + git")] : [OG.badge("API key required", "warn"), OG.badge("nothing to install"), OG.badge("no project files")]),
        kids);
      const localKeyNote = () => p(h("span", { class: "dim" }, "Have a key? Run "), c("opengrid auth login"), h("span", { class: "dim" }, " once (", h("a", { class: "lnk", href: "#access" }, "how"), "): the local server uses the stored key for route previews and jobs. No key in any config file."));
      const clients = {
        claude: () => h("div", {}, verified("claude"),
          mode("local", p("Run once in a terminal. ", c("--scope user"), " makes it available in every project; the server analyzes the project you start Claude Code in."), code(S.localClaude, "shell", "Claude Code · local"), localKeyNote()),
          mode("hosted", p("Set ", c(KEY_VAR), " in your shell profile first (", c(S.keyEnv.split("     ")[0]), "), then run once. The single quotes keep the key itself out of the config: Claude Code reads the variable when it connects."),
            code(S.hostedClaude, "shell", "Claude Code · hosted")),
          h("details", { class: "dx-fold" }, h("summary", {}, "Check it and troubleshoot"), h("div", { class: "dx-fold-b" }, h("dl", { class: "dx-dl" },
            h("dt", {}, "Check"), h("dd", {}, c("claude mcp get opengrid"), " should say ", h("b", {}, "Connected"), "; inside Claude Code, ", c("/mcp"), " lists the tools."),
            h("dt", {}, "“Failed to connect” on the hosted endpoint"), h("dd", {}, c(KEY_VAR), " is empty where Claude Code started (", c("echo $" + KEY_VAR), "), or the key was revoked. Open a new terminal after editing your profile."),
            h("dt", {}, "“outside the allowed roots”"), h("dd", {}, "Start Claude Code in the project folder, or add ", c("--env OPENGRID_ALLOWED_ROOTS=/path/to/project"), " to the add command."),
            h("dt", {}, "Replace an existing entry"), h("dd", {}, c("claude mcp remove opengrid -s user"), ", then add again."))))),
        cursor: () => h("div", {}, verified("cursor"),
          mode("local", p("Paste into ", c("~/.cursor/mcp.json"), " (every project) or ", c(".cursor/mcp.json"), " in one project, then restart Cursor."), code(S.localCursor, "json", "~/.cursor/mcp.json · local"),
            p(h("span", { class: "dim" }, "If analysis says a folder is outside the allowed roots, add "), c('"OPENGRID_ALLOWED_ROOTS": "/path/to/project"'), h("span", { class: "dim" }, " to env.")), localKeyNote()),
          mode("hosted", h("div", { class: "dx-row" }, h("a", { class: "btn pri", href: S.cursorLink }, "Add to Cursor"), h("span", { class: "dim" }, "or paste:")),
            code(S.hostedCursor, "json", "~/.cursor/mcp.json · hosted"), p("Cursor reads ", c(KEY_VAR), " from the environment it was started from: launch it from a terminal where the variable is set, or set it system-wide.")),
          h("details", { class: "dx-fold" }, h("summary", {}, "Check it and troubleshoot"), h("div", { class: "dx-fold-b" }, h("dl", { class: "dx-dl" },
            h("dt", {}, "Check"), h("dd", {}, "In Cursor Settings, the MCP section should show ", c("opengrid"), " as enabled with its tools listed."),
            h("dt", {}, "The server fails to start (uvx not found)"), h("dd", {}, "Apps opened from the Dock may not see your shell's PATH. Use the full path from ", c("which uvx"), " as ", c('"command"'), "."),
            h("dt", {}, "401 on the hosted endpoint"), h("dd", {}, "The key variable is empty in Cursor's environment, or the key was revoked."))))),
        vscode: () => h("div", {}, verified("vscode"),
          mode("local", p("Save as ", c(".vscode/mcp.json"), " in your project. ", c("${workspaceFolder}"), " lets the server analyze the open project."), code(S.localVscode, "json", ".vscode/mcp.json · local"), localKeyNote()),
          mode("hosted", p("VS Code asks for the key once (a password field) and keeps it in its secret storage."), code(S.hostedVscode, "json", ".vscode/mcp.json · hosted")),
          h("details", { class: "dx-fold" }, h("summary", {}, "Check it and troubleshoot"), h("div", { class: "dx-fold-b" }, h("dl", { class: "dx-dl" },
            h("dt", {}, "Check"), h("dd", {}, "Command palette → ", h("b", {}, "MCP: List Servers"), " → opengrid → Start; then use Copilot Chat in agent mode."),
            h("dt", {}, "uvx not found"), h("dd", {}, "Use the full path from ", c("which uvx"), " as ", c('"command"'), "."))))),
        codex: () => h("div", {}, verified("codex"),
          mode("local", p("Run once. It writes ", c("[mcp_servers.opengrid]"), " to ", c("~/.codex/config.toml"), "."), code(S.localCodex, "shell", "Codex CLI · local"), localKeyNote()),
          mode("hosted", p("Codex sends ", c("Authorization: Bearer <value of " + KEY_VAR + ">"), " on each request; the key is never written to the config."), code(S.hostedCodex, "shell", "Codex CLI · hosted")),
          h("details", { class: "dx-fold" }, h("summary", {}, "Check it and troubleshoot"), h("div", { class: "dx-fold-b" }, h("dl", { class: "dx-dl" },
            h("dt", {}, "Check"), h("dd", {}, c("codex mcp get opengrid"), " shows the entry; Codex connects when a session starts."),
            h("dt", {}, "401 on the hosted endpoint"), h("dd", {}, c(KEY_VAR), " is not set in the shell that runs Codex."),
            h("dt", {}, "Replace an entry"), h("dd", {}, c("codex mcp remove opengrid"), ", then add again."))))),
        desktop: () => h("div", {}, verified("desktop"),
          mode("local", p("Claude Desktop → Settings → Developer → Edit Config opens ", c("claude_desktop_config.json"), " (macOS: ", c("~/Library/Application Support/Claude/"), ", Windows: ", c("%APPDATA%\\Claude\\"), "). Add the ", c("opengrid"), " entry, put your project's absolute path in ", c("OPENGRID_ALLOWED_ROOTS"), ", and restart Claude Desktop."),
            code(S.localDesktop, "json", "claude_desktop_config.json · local"), p("Desktop apps often don't see your shell's PATH: if it fails to start, use the full path from ", c("which uvx"), " as ", c('"command"'), "."), localKeyNote()),
          mode("hosted", p("claude.ai and Claude Desktop add remote servers under Settings → Connectors → Add custom connector, with URL ", c(S.url), " ", OG.copy(S.url), "."),
            p("OpenGrid's hosted endpoint needs an ", c("Authorization: Bearer <key>"), " request header. Claude offers custom request headers only as a limited beta: if the dialog has no ", h("b", {}, "Request headers"), " section, your account can't use the hosted endpoint yet. Use the local server above instead. OpenGrid has no OAuth sign-in."))),
      };
      const clientBox = h("div", { class: "dx-client" });
      const clientTabs = OG.tabs([["claude", "Claude Code"], ["cursor", "Cursor"], ["vscode", "VS Code"], ["codex", "Codex"], ["desktop", "Claude Desktop / claude.ai"]], "claude", v => clientBox.replaceChildren(clients[v]()));
      clientBox.replaceChildren(clients.claude());
      root.append(sec("assistant", "Connect an AI coding assistant", "MCP: the Model Context Protocol",
        h("div", { class: "dx-scroll" }, h("table", { class: "dx-tbl" },
          h("thead", {}, h("tr", {}, h("th", {}, ""), h("th", {}, "Local server (recommended to start)"), h("th", {}, "Hosted endpoint"))),
          h("tbody", {},
            h("tr", {}, h("th", {}, "What it is"), h("td", {}, "Your client starts OpenGrid's MCP server on your computer (", c("uvx … opengrid mcp"), "). It talks to OpenGrid over HTTPS."), h("td", {}, "OpenGrid's own server at ", c(S.url), " (streamable HTTP). Nothing runs on your computer.")),
            h("tr", {}, h("th", {}, "Your project files"), h("td", {}, h("span", { class: "dx-yes" }, "Can analyze"), " the allowed project folder, on your machine; only the inferred requirements are sent."), h("td", {}, h("span", { class: "dx-no" }, "Cannot see them."), " A remote server has no access to your computer; describe the workload instead.")),
            h("tr", {}, h("th", {}, "API key"), h("td", {}, h("span", { class: "dx-yes" }, "Not needed"), " for prices, analysis and recommendations; a key adds route previews and jobs."), h("td", {}, h("span", { class: "dx-need" }, "Always required"), ", even for market data.")),
            h("tr", {}, h("th", {}, "You need"), h("td", {}, ext("https://docs.astral.sh/uv/getting-started/installation/", "uv"), " and git (uv fetches the client from GitHub and a Python if needed)."), h("td", {}, "An API key and a client that can send a Bearer header.")),
            h("tr", {}, h("th", {}, "Tools"), h("td", {}, "13 (", h("a", { class: "lnk", href: "#tools" }, "list"), ")"), h("td", {}, "12: everything except project analysis"))))),
        p("Pick one per client (both use the name ", c("opengrid"), "). Your assistant can never rent a paid GPU through either."),
        h("div", { class: "dx-row" }, clientTabs), clientBox,
        h("h3", { class: "dx-h3" }, "Then ask"),
        h("div", { class: "dx-asks" }, ["Use OpenGrid to find the cheapest available H100.", "Use OpenGrid to analyze this project and recommend GPUs. (local server)", "Compare the H100 SXM and H200 on OpenGrid. Which numbers are observed prices and which are vendor specs?"]
          .map(q => h("div", { class: "dx-ask" }, h("span", {}, "“" + q + "”"), OG.copy(q.replace(/ \(local server\)$/, ""))))),
        p(h("span", { class: "dim" }, "A working setup answers with observed prices and an as-of time. Prompts are also available in your client's prompt menu: cheapest_gpu, try_free_demo and size_my_project (local)."))));

      // ---------- terminal ----------
      root.append(sec("terminal", "Use OpenGrid from your terminal", "the opengrid CLI",
        p("Needs ", ext("https://docs.astral.sh/uv/getting-started/installation/", "uv"), " and git. There is no PyPI package: uv builds the client from OpenGrid's public GitHub repository."),
        h("h3", { class: "dx-h3" }, "1 · Install uv (skip if you have it)"), code(S.installUv, "shell", "official uv installer"),
        h("h3", { class: "dx-h3" }, "2 · Try it with nothing installed"), p(c("OPENGRID_URL"), " points the client at this server (its default is a local development server)."), code(S.termTry, "shell", "no key needed"),
        h("h3", { class: "dx-h3" }, "3 · Install the opengrid command (optional)"), code(S.termInstall, "shell", "puts opengrid on your PATH"),
        p("Keep ", c("OPENGRID_URL"), " in your shell profile. Upgrade later with ", c(`uv tool install --reinstall "${GIT}"`), "."),
        h("h3", { class: "dx-h3" }, "More commands"), code(S.termExamples, "shell", "no key needed"),
        h("div", { class: "dx-scroll" }, h("table", { class: "dx-tbl" },
          h("thead", {}, h("tr", {}, h("th", {}, "Command"), h("th", {}, "What it does"), h("th", {}, "Key"))),
          h("tbody", {}, [
            ["opengrid gpu search | show | compare | market", "GPU catalog, specs, observed prices, history and best available now", "no"],
            ["opengrid providers", "every provider with coverage and relative value", "no"],
            ["opengrid analyze <dir>", "local project analysis; --dry-run opens nothing", "no"],
            ["opengrid estimate | recommend [<dir>]", "memory, GPU count and ranked picks; flags such as --model, --method, --budget", "no"],
            ["opengrid route preview <gpu>", "what OpenGrid would select and its quote; never launches", "route:preview"],
            ["opengrid jobs list | status | logs", "your jobs", "deployments:read"],
            ["opengrid demo train", "the free simulated MNIST demo", "route:execute"],
            ["opengrid jobs cancel <id> --yes", "cancel one of your jobs", "route:execute"],
            ["opengrid auth login | status | logout", "store, check or remove your key (0600 file)", "-"],
            ["opengrid doctor", "connectivity, server version, key scopes, MCP readiness", "-"],
            ["opengrid mcp config claude|cursor|codex|vscode", "print every setup snippet for that client", "-"],
          ].map(([a, b, k]) => h("tr", {}, h("td", { class: "mono" }, a), h("td", {}, b), h("td", { class: k === "no" ? "dx-no" : "" }, k === "no" ? "not needed" : k)))))),
        p(h("span", { class: "dim" }, "Exit codes: 0 ok · 3 key missing or lacks a scope (the message names it) · 4 not found · 5 OpenGrid unreachable · 6 refused by a safety rule (e.g. no --yes without a terminal). Full reference: "), ext(REPO + "/tree/main/cli#readme", "cli/README.md"))));

      // ---------- REST ----------
      root.append(sec("rest", "Integrate OpenGrid into your application", "REST API, JSON over HTTPS",
        p("Base URL ", c(base), ". Every response is ", c("{data, meta}"), "; ", c("meta.as_of"), " says when, ", c("meta.kind"), " says whether a number is observed or estimated."),
        h("h3", { class: "dx-h3" }, "Without a key: public market data and recommendations"), code(S.curlCheapest, "curl", "GET /v1/best/{gpu} · cheapest fresh listing per variant"), code(S.curlRecommend, "curl", "POST /v1/recommend · GPUs that fit a workload"),
        p("Anonymous requests are rate limited per IP address. A key gives you your own, larger budget."),
        h("h3", { class: "dx-h3" }, "With a key: route previews, jobs and your account"), code(S.curlKeyed, "curl", "POST /v1/route/preview · scope route:preview"),
        p("Send the key as ", c("Authorization: Bearer $" + KEY_VAR), ". Never put a key in a URL. ", h("b", {}, "401"), " missing or invalid key · ", h("b", {}, "403"), " the key lacks the scope (the message names it) · ", h("b", {}, "429"), " over the rate limit (see ", c("Retry-After"), ")."),
        h("div", { class: "dx-row" }, h("a", { class: "btn pri", href: "/api" }, "API reference: every public endpoint, with Try it"), h("a", { class: "btn", href: "/methodology/data-kinds" }, "Data kinds"))));

      // ---------- access ----------
      const accessSec = h("div");
      root.append(sec("access", "Access & API keys", null, accessSec));
      function drawAccess(me) {
        const st = accessState(me);
        accessBox.replaceChildren(st === "operator" ? accessCall(me) : call("info", h("b", {}, "No account needed to start. "),
          "Market data, project analysis and GPU recommendations work without a key. A key (issued by the OpenGrid team; no self-service sign-up yet) is needed for the hosted MCP endpoint, route previews and jobs. ",
          h("a", { class: "lnk", href: "#access" }, "How to get one ↓")));
        accessSec.replaceChildren(
          h("div", { class: "dx-scroll" }, h("table", { class: "dx-tbl" },
            h("thead", {}, h("tr", {}, h("th", {}, "Your situation"), h("th", {}, "What you can do"), h("th", {}, "Next step"))),
            h("tbody", {},
              h("tr", { class: st === "anonymous" ? "on" : null }, h("th", {}, "Exploring, no key"), h("td", {}, "Everything in the Quickstart: public market data (REST, CLI, local MCP), project analysis, recommendations, the OpenGrid Assistant."), h("td", {}, h("a", { class: "lnk", href: "#quickstart" }, "Quickstart →"))),
              h("tr", {}, h("th", {}, "You need a key"), h("td", {}, "Keys are issued by the OpenGrid team to design partners. There is no self-service sign-up or payment yet."),
                h("td", {}, h("a", { class: "lnk", href: ACCESS_URL, target: "_blank", rel: "noopener noreferrer external" }, "Request API access on GitHub ↗"), h("div", { class: "dim" }, "Public issue: no secrets."))),
              h("tr", {}, h("th", {}, "You were issued a key"), h("td", {}, "Store it once and check it:", code(S.authLogin, "shell", "CLI and local MCP server"), code(S.keyEnv, "shell", "hosted MCP endpoint and curl"),
                p("A key with ", c("account:manage"), " (design-partner keys have it) can create narrower keys for each tool, e.g. a read-only key for your assistant:"), code(S.curlMint, "curl", "POST /v1/keys · secret returned once")),
                h("td", {}, code(S.curlMe, "curl", "who am I"))),
              h("tr", { class: st === "operator" ? "on" : null }, h("th", {}, "You are the OpenGrid operator"), h("td", {}, "Create keys for the operator account, or issue a developer their own account and first key."),
                h("td", {}, h("a", { class: "lnk", href: "/keys" }, "API keys →"), h("br"), h("a", { class: "lnk", href: "/admin/partners" }, "Design partners →")))))),
          p(h("span", { class: "dim" }, "Keys are shown once and stored by OpenGrid only as a hash. Treat a key like a password: keep it in an environment variable or the CLI's key file, never in a URL, a screenshot or a committed file.")));
      }
      drawAccess(null);

      // ---------- boundaries ----------
      root.append(sec("boundaries", "What OpenGrid can and can't do", "today",
        h("div", { class: "dx-scroll" }, h("table", { class: "dx-tbl" },
          h("thead", {}, h("tr", {}, ["Capability", "CLI", "Local MCP", "Hosted MCP", "REST", "API key"].map(t => h("th", {}, t)))),
          h("tbody", {}, BOUNDARIES.map(r => h("tr", {}, h("th", {}, r[0]), r.slice(1, 5).map(v => h("td", {}, yn(v))), h("td", {}, r[5])))))),
        call("warn", h("b", {}, "Execution. "), "OpenGrid's execution path is built but every provider adapter is still simulated, and no real GPU job has run yet. When real runs open, only the allowlisted PyTorch MNIST template will run, started on this website and approved by the operator before any GPU is launched. No tool, command or endpoint runs your own code. Recommendations are market advice: prices are observed list prices (not quotes), memory is estimated, and runtime is never guessed without a benchmark.")));

      // ---------- tools ----------
      root.append(sec("tools", "MCP tools", "what your assistant can call",
        h("div", { class: "dx-scroll" }, h("table", { class: "dx-tbl" },
          h("thead", {}, h("tr", {}, h("th", {}, "Tool"), h("th", {}, "What it does"), h("th", {}, "Key scope"))),
          h("tbody", {}, TOOLS.map(([n, d, k, local]) => h("tr", {}, h("td", { class: "mono" }, n, local ? [" ", OG.badge("local only")] : null), h("td", {}, d), h("td", { class: k === "route:execute" ? "dx-need" : "" }, k)))))),
        p(h("span", { class: "dim" }, "Only three tools write anything: a route preview records an audit ticket, the demo creates a simulated job, cancel stops one of your jobs. None can spend money."))));

      // ---------- troubleshooting ----------
      root.append(sec("troubleshooting", "Troubleshooting", null,
        h("dl", { class: "dx-dl" },
          h("dt", {}, "uvx: command not found, or a git error"), h("dd", {}, "Install ", ext("https://docs.astral.sh/uv/getting-started/installation/", "uv"), " and git. Desktop apps may need the full path from ", c("which uvx"), " as the command."),
          h("dt", {}, "“cannot reach OpenGrid at http://localhost:8000”"), h("dd", {}, c("OPENGRID_URL"), " is not set, so the client tried a local development server. ", c("export OPENGRID_URL=" + base), "."),
          h("dt", {}, "My assistant doesn't list OpenGrid's tools"), h("dd", {}, "Restart the client after adding the server, then use its MCP status view (per-client checks above). ", c("opengrid doctor"), " shows whether the MCP server starts."),
          h("dt", {}, "“outside the allowed roots”"), h("dd", {}, "The local server only analyzes the folder the client started it in. Start the client in your project, or set ", c("OPENGRID_ALLOWED_ROOTS"), " to the project path in the server's env."),
          h("dt", {}, "401 Unauthorized"), h("dd", {}, "The key is missing, mistyped, revoked or expired (the hosted endpoint always needs one). Check ", c("echo $" + KEY_VAR), " in the environment the client starts from, or ", c("opengrid auth status"), "."),
          h("dt", {}, "403 Forbidden"), h("dd", {}, "The key lacks a scope; the message names it (e.g. ", c("route:preview"), "). Ask for a key with that scope, or mint one with ", c("POST /v1/keys"), " if your key has account:manage."),
          h("dt", {}, "429 Too Many Requests"), h("dd", {}, "Over the rate limit. Wait for ", c("Retry-After"), " seconds. Anonymous requests share a per-IP budget."),
          h("dt", {}, "ChatGPT"), h("dd", {}, "ChatGPT connects only to servers with OAuth sign-in, which OpenGrid doesn't offer yet. Use one of the clients above."))));

      const me = await OG.me();
      if (!ctx.alive()) return;
      drawAccess(me);
      if (location.hash) { const t = document.getElementById(location.hash.slice(1)); if (t) t.scrollIntoView(); }
    },
  });
})();
