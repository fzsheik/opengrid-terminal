/* /developers: one first step (connect an AI assistant to the local MCP server, or one keyless curl call), the
   per-client setup, what needs a key (said once), and everything else folded: CLI, REST, local vs hosted, what
   works where, the MCP tools and troubleshooting.

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

  // What OpenGrid has run itself (2026-10-15, macOS; details in docs/DEVELOPER_ONBOARDING.md). false: the snippet
  // follows the client's documented format but OpenGrid has not run it in that client.
  const VERIFIED = {
    claude: [true, "Tested by OpenGrid in Claude Code."],
    codex: [true, "Tested by OpenGrid in Codex CLI (registration only)."],
    cursor: [false, "Follows Cursor's documented format; not yet tested by OpenGrid."],
    vscode: [false, "Follows VS Code's documented format; not yet tested by OpenGrid."],
    desktop: [false, "Follows Claude Desktop's documented format; not yet tested by OpenGrid."],
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
  const fold = (id, title, ...kids) => h("details", { class: "dx-fold", id }, h("summary", {}, title), h("div", { class: "dx-fold-b" }, kids));
  const yn = v => (v === "yes" ? h("span", { class: "dx-yes" }, "Yes") : v === "no" ? h("span", { class: "dx-no" }, "No") : /^no:/.test(v) ? h("span", { class: "dx-no" }, "No", h("span", { class: "dim" }, v.slice(2))) : v);
  const requestBtn = () => h("a", { class: "btn pri", href: ACCESS_URL, target: "_blank", rel: "noopener noreferrer external", title: "Opens a public GitHub issue: include no secrets" }, "Request API access ↗");
  // How to get access, said the same way on every developer page: the shared account note, the one request
  // channel, and the operator's own shortcuts when the operator is signed in.
  function accessCall(me, opts) {
    opts = opts || {};
    if (accessState(me) === "operator") return call("ok", h("b", {}, "Signed in as the operator. "),
      "Keys you create on API keys belong to the operator account. Design partners gives a developer their own account and key.",
      h("div", { class: "dx-row" }, h("a", { class: "btn pri", href: "/keys" }, "Create a key"), h("a", { class: "btn", href: "/admin/partners" }, "Issue a key to a developer")));
    return h("div", { class: "dx-access" },
      OG.accessNote("account", "Keys are issued by the OpenGrid team. Public data, project analysis and GPU recommendations work without one."),
      h("div", { class: "dx-row" }, requestBtn(), h("span", { class: "dim" }, "a public GitHub issue: include no secrets"),
        opts.noStart ? null : h("a", { class: "btn", href: "/developers#quickstart" }, "Start without a key"),
        h("a", { class: "lnk dim dx-staff", href: OG.loginHref() }, "OpenGrid staff: operator sign-in")));
  }
  // A form problem in plain words. A client-side check (an Error with no HTTP status) or a 400 / 409 / 422 from
  // the server is a validation message: show it as the headline (FastAPI's field list included), never as
  // "Could not reach OpenGrid". Anything else (network, 401, 5xx) goes to the shared OG.error.
  function formError(err) {
    const st = err && typeof err.status === "number" ? err.status : null;
    if (st !== null && ![400, 409, 422].includes(st)) return OG.error(err);
    const d = err && err.body && err.body.detail;
    let msg = Array.isArray(d) ? d.map(x => {
      const field = (x.loc || []).filter(l => l !== "body" && typeof l === "string").join(" ").replace(/_/g, " ");
      return (field ? field + ": " : "") + String(x.msg || "").replace(/^Value error, /, "");
    }).join("; ") : (err && err.message) || "Check the highlighted fields.";
    if (/^HTTP \d+$/.test(msg)) msg = "The server did not accept these values.";
    msg = msg.charAt(0).toUpperCase() + msg.slice(1);
    return h("div", { class: "state error dx-invalid", role: "alert" }, h("div", { class: "st-body" }, h("b", {}, "Please check the form"), h("p", {}, msg)));
  }
  // In-page anchors that point into a fold open it first (also used on load for /page#anchor).
  function openTarget(id) {
    const t = id && document.getElementById(id);
    if (!t) return null;
    const d = t.tagName === "DETAILS" ? t : t.closest("details");
    if (d) d.open = true;
    return t;
  }
  function wireAnchors(root) {
    root.addEventListener("click", e => {
      const a = e.target.closest && e.target.closest('a[href^="#"]');
      if (a) openTarget(a.getAttribute("href").slice(1));
    });
    if (location.hash) { const t = openTarget(location.hash.slice(1)); if (t) t.scrollIntoView(); }
  }
  OG.dx = { code, p, c, ext, call, fold, accessCall, accessState, formError, wireAnchors, requestBtn, ACCESS_URL };

  // ------------------------------------------------------------ page
  OG.page("/developers", {
    title: "Developers",
    async mount(el, params, query, ctx) {
      const root = h("div", { class: "pg-dx" });
      el.append(root);
      const base = ((OG.boot && OG.boot.base_url) || location.origin).replace(/\/$/, "");
      const S = snippets(base);
      const sec = (id, title, ...kids) => h("section", { class: "dx-sec", id }, h("div", { class: "dx-sec-h" }, h("h2", { class: "dx-h2" }, title)), kids);
      const uv = () => ext("https://docs.astral.sh/uv/getting-started/installation/", "uv");

      root.append(OG.head("Developers", "Live GPU prices and GPU recommendations in your AI assistant, your terminal or your own code. No key needed to start.",
        h("a", { class: "btn", href: "/api" }, "API reference")));

      // ---------- start here: one obvious first step ----------
      root.append(h("section", { class: "dx-start", id: "quickstart", "aria-label": "Start here" },
        h("div", { class: "dx-win dx-first" }, h("div", { class: "eyebrow" }, "Start here"),
          h("h2", { class: "dx-h2" }, "Connect your AI assistant"),
          p("Claude Code, Cursor, VS Code, Codex or Claude Desktop can then look up live GPU prices and size your own project. No key, no account."),
          h("a", { class: "btn pri dx-big", href: "#assistant" }, "Set it up")),
        h("div", { class: "dx-win" }, h("div", { class: "eyebrow" }, "Or try one call"),
          code(S.curlCheapest, "curl", "no key"),
          p("The cheapest in-stock listing of each H100 variant right now. Or ", h("a", { class: "lnk", href: "/assistant?q=" + encodeURIComponent("Cheapest available H100") }, "ask the OpenGrid Assistant"), "."))));

      // ---------- connect an assistant ----------
      const verified = k => { const v = VERIFIED[k]; return h("p", { class: "dx-verified" + (v[0] ? "" : " untested") }, h("b", {}, v[0] ? "Tested" : "Not yet tested"), " · ", v[1]); };
      const check = (...kids) => p(h("span", { class: "dim" }, "Check: "), kids);
      const hosted = (...kids) => h("details", { class: "dx-fold dx-hosted" },
        h("summary", {}, "Use the hosted endpoint instead ", h("span", { class: "dim" }, "· nothing to install, needs an API key, cannot read your project")),
        h("div", { class: "dx-fold-b" }, kids));
      const clients = {
        claude: () => [p("Run once in a terminal. It works in every project and analyzes the folder you start Claude Code in."),
          code(S.localClaude, "shell", "Claude Code"), check(c("claude mcp get opengrid"), " says Connected."), verified("claude"),
          hosted(p("Set ", c(KEY_VAR), " in your shell profile first. The single quotes keep the key out of the config: Claude Code reads the variable when it connects."),
            code(S.keyEnv, "shell", "your shell profile"), code(S.hostedClaude, "shell", "Claude Code · hosted"))],
        cursor: () => [p("Paste into ", c("~/.cursor/mcp.json"), " (or ", c(".cursor/mcp.json"), " in one project), then restart Cursor."),
          code(S.localCursor, "json", "~/.cursor/mcp.json"), check("Cursor Settings, MCP: ", c("opengrid"), " is enabled and lists its tools."), verified("cursor"),
          hosted(h("div", { class: "dx-row" }, h("a", { class: "btn", href: S.cursorLink }, "Add to Cursor"), h("span", { class: "dim" }, "or paste:")),
            code(S.hostedCursor, "json", "~/.cursor/mcp.json · hosted"), p("Cursor reads ", c(KEY_VAR), " from the environment it was started from."))],
        vscode: () => [p("Save as ", c(".vscode/mcp.json"), " in your project."),
          code(S.localVscode, "json", ".vscode/mcp.json"), check("Command palette, ", h("b", {}, "MCP: List Servers"), ", opengrid, Start. Then use Copilot Chat in agent mode."), verified("vscode"),
          hosted(p("VS Code asks for the key once, in a password field, and keeps it in its secret storage."), code(S.hostedVscode, "json", ".vscode/mcp.json · hosted"))],
        codex: () => [p("Run once. It adds ", c("opengrid"), " to ", c("~/.codex/config.toml"), "."),
          code(S.localCodex, "shell", "Codex CLI"), check(c("codex mcp get opengrid"), " shows the entry."), verified("codex"),
          hosted(p("Codex sends the value of ", c(KEY_VAR), " on each request; the key is never written to the config."), code(S.hostedCodex, "shell", "Codex CLI · hosted"))],
        desktop: () => [p("Settings, Developer, Edit Config opens ", c("claude_desktop_config.json"), ". Add this entry, set ", c("OPENGRID_ALLOWED_ROOTS"), " to your project's absolute path, and restart Claude Desktop."),
          code(S.localDesktop, "json", "claude_desktop_config.json"), check("after the restart, a new chat lists OpenGrid's tools."), verified("desktop"),
          h("details", { class: "dx-fold dx-hosted" }, h("summary", {}, "claude.ai and the hosted endpoint"), h("div", { class: "dx-fold-b" },
            p("Custom connectors take the URL ", c(S.url), " ", OG.copy(S.url), ", but the endpoint needs an ", c("Authorization"), " header, which Claude offers only as a limited beta. If your connector dialog has no Request headers section, use the local server above. OpenGrid has no OAuth sign-in.")))],
      };
      const clientBox = h("div", { class: "dx-client" });
      const clientTabs = OG.tabs([["claude", "Claude Code"], ["cursor", "Cursor"], ["vscode", "VS Code"], ["codex", "Codex"], ["desktop", "Claude Desktop"]], "claude", v => clientBox.replaceChildren(...clients[v]()));
      clientBox.replaceChildren(...clients.claude());
      root.append(sec("assistant", "Connect your AI assistant",
        p("Your assistant starts OpenGrid's MCP server on your computer. Needs ", uv(), " and git; nothing else to install, no key."),
        h("div", { class: "dx-row" }, clientTabs), clientBox,
        h("h3", { class: "dx-h3" }, "Then ask"),
        h("div", { class: "dx-asks" }, ["Use OpenGrid to find the cheapest available H100.", "Use OpenGrid to analyze this project and recommend GPUs.", "Compare the H100 SXM and H200 on OpenGrid."]
          .map(q => h("div", { class: "dx-ask" }, h("span", {}, "“" + q + "”"), OG.copy(q))))));

      // ---------- access: said once ----------
      const accessBox = h("div");
      const drawAccess = me => accessBox.replaceChildren(accessState(me) === "operator" ? accessCall(me) : call("info", h("b", {}, "What needs a key. "),
        "Prices, project analysis and recommendations work without one. A key adds route previews, your jobs and the hosted endpoint. Keys are issued by the OpenGrid team (no self-service sign-up yet). OpenGrid does not run real GPU jobs yet and never runs your own code.",
        h("div", { class: "dx-row" }, requestBtn(), h("span", { class: "dim" }, "a public GitHub issue: include no secrets"))));
      drawAccess(null);
      root.append(sec("access", "API keys", accessBox,
        fold("have-key", "Already have a key? Use it",
          p("For the CLI and the local MCP server, store it once at a hidden prompt:"), code(S.authLogin, "shell", "CLI and local MCP server"),
          p("For the hosted endpoint and curl, keep it in an environment variable (never in a URL or a committed file):"), code(S.keyEnv, "shell", "hosted endpoint and curl"),
          code(S.curlMe, "curl", "who am I"),
          p("A key with ", c("account:manage"), " can create narrower keys, for example a read-only key for your assistant:"), code(S.curlMint, "curl", "the new secret is shown once"))));

      // ---------- everything else, folded ----------
      root.append(sec("more", "More",
        fold("terminal", "Terminal (CLI)",
          p("No PyPI package: uv builds the client from OpenGrid's public GitHub repository."),
          code(S.installUv, "shell", "install uv (skip if you have it)"),
          code(S.termTry, "shell", "try it with nothing installed"),
          code(S.termInstall, "shell", "install the opengrid command (optional)"),
          code(S.termAnalyze, "shell", "analyze your project and get GPU picks"),
          code(S.termExamples, "shell", "more commands, no key"),
          p(h("span", { class: "dim" }, "Every command has --json. Full reference: "), ext(REPO + "/tree/main/cli#readme", "cli/README.md"))),
        fold("rest", "REST API",
          p("Base URL ", c(base), ". Every response is ", c("{data, meta}"), "; ", c("meta.as_of"), " says when and ", c("meta.kind"), " whether a number is observed or estimated."),
          code(S.curlCheapest, "curl", "GET /v1/best/{gpu} · no key"), code(S.curlRecommend, "curl", "POST /v1/recommend · no key"),
          code(S.curlKeyed, "curl", "POST /v1/route/preview · key with route:preview"),
          p("Anonymous calls are rate limited per IP. ", h("b", {}, "401"), " missing or invalid key · ", h("b", {}, "403"), " the key lacks a scope (the message names it) · ", h("b", {}, "429"), " over the limit (see ", c("Retry-After"), ")."),
          h("a", { class: "btn", href: "/api" }, "API reference with Try it")),
        fold("local-hosted", "Local server or hosted endpoint, and what analysis reads",
          h("div", { class: "dx-scroll" }, h("table", { class: "dx-tbl" },
            h("thead", {}, h("tr", {}, h("th", {}, ""), h("th", {}, "Local server"), h("th", {}, "Hosted endpoint"))),
            h("tbody", {},
              h("tr", {}, h("th", {}, "Runs"), h("td", {}, "on your computer (needs uv + git)"), h("td", {}, "at ", c(S.url), ", nothing to install")),
              h("tr", {}, h("th", {}, "Your project"), h("td", {}, h("span", { class: "dx-yes" }, "Can analyze it"), "; only the inferred requirements are sent"), h("td", {}, h("span", { class: "dx-no" }, "Cannot see it"))),
              h("tr", {}, h("th", {}, "API key"), h("td", {}, "not needed (a key adds previews and jobs)"), h("td", {}, h("span", { class: "dx-need" }, "always required"))),
              h("tr", {}, h("th", {}, "Tools"), h("td", {}, "13"), h("td", {}, "12 (no project analysis)"))))),
          h("dl", { class: "dx-dl" },
            h("dt", {}, "Reads"), h("dd", {}, "Only allowlisted files in the folder: dependency manifests, container files, training and model configs, training or serving scripts, notebook code cells and the README. ", c(".gitignore"), " is respected."),
            h("dt", {}, "Never reads"), h("dd", {}, "Secrets (", c(".env"), ", keys, ", c(".ssh"), "), model weights, datasets, checkpoints, logs and binaries. It never runs your code."),
            h("dt", {}, "Sends"), h("dd", {}, "Only the inferred requirements (workload type, model, method, precision, budget), never file contents. ", c("opengrid analyze . --dry-run"), " lists the files without opening any."),
            h("dt", {}, "Allowed folders"), h("dd", {}, "The folder the client started in, plus any listed in ", c("OPENGRID_ALLOWED_ROOTS"), ". Never your home directory."))),
        fold("boundaries", "What works where",
          h("div", { class: "dx-scroll" }, h("table", { class: "dx-tbl" },
            h("thead", {}, h("tr", {}, ["", "CLI", "Local MCP", "Hosted MCP", "REST", "Key"].map(t => h("th", {}, t)))),
            h("tbody", {}, BOUNDARIES.map(r => h("tr", {}, h("th", {}, r[0]), r.slice(1, 5).map(v => h("td", {}, yn(v))), h("td", {}, r[5]))))))),
        fold("tools", "All 13 MCP tools",
          h("div", { class: "dx-scroll" }, h("table", { class: "dx-tbl" },
            h("thead", {}, h("tr", {}, h("th", {}, "Tool"), h("th", {}, "What it does"), h("th", {}, "Key scope"))),
            h("tbody", {}, TOOLS.map(([n, d, k, local]) => h("tr", {}, h("td", { class: "mono" }, n, local ? [" ", OG.badge("local only")] : null), h("td", {}, d), h("td", { class: k === "route:execute" ? "dx-need" : "" }, k)))))),
          p(h("span", { class: "dim" }, "None can spend money. Your client's prompt menu also offers cheapest_gpu, try_free_demo and size_my_project."))),
        fold("troubleshooting", "Troubleshooting",
          h("dl", { class: "dx-dl" },
            h("dt", {}, "uvx: command not found, or a git error"), h("dd", {}, "Install ", uv(), " and git. Desktop apps may not see your shell's PATH: use the full path from ", c("which uvx"), " as the command."),
            h("dt", {}, "“cannot reach OpenGrid at http://localhost:8000”"), h("dd", {}, c("OPENGRID_URL"), " is not set: ", c("export OPENGRID_URL=" + base), "."),
            h("dt", {}, "The assistant doesn't list OpenGrid's tools"), h("dd", {}, "Restart the client after adding the server. ", c("opengrid doctor"), " shows whether the MCP server starts."),
            h("dt", {}, "“outside the allowed roots”"), h("dd", {}, "Start the client in your project, or set ", c("OPENGRID_ALLOWED_ROOTS"), " to the project path in the server's env."),
            h("dt", {}, "401 on the hosted endpoint"), h("dd", {}, c(KEY_VAR), " is empty where the client started (", c("echo $" + KEY_VAR), "), or the key was revoked or expired."),
            h("dt", {}, "403"), h("dd", {}, "The key lacks a scope; the message names it."),
            h("dt", {}, "429"), h("dd", {}, "Over the rate limit. Wait ", c("Retry-After"), " seconds."),
            h("dt", {}, "Replace an entry"), h("dd", {}, c("claude mcp remove opengrid -s user"), " or ", c("codex mcp remove opengrid"), ", then add again."),
            h("dt", {}, "ChatGPT"), h("dd", {}, "ChatGPT connects only to servers with OAuth sign-in, which OpenGrid doesn't offer yet.")))));

      wireAnchors(root);
      const me = await OG.me();
      if (!ctx.alive()) return;
      drawAccess(me);
    },
  });
})();
