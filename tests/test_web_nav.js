// Navigation / information architecture: OG.navKeyFor (pure, web/core.js), the static nav in web/index.html
// (five primary groups, secondary links kept in the DOM for the command bar), that every client page highlights
// a nav link that exists, and the homepage's pure helper (web/pages/home.js → OG.home).
// Run: node tests/test_web_nav.js
const fs = require("fs");
const path = require("path");
const OG = require("../web/core.js");
global.OG = OG;
require("../web/pages/home.js");
let failures = 0;
const ok = (c, m) => { if (!c) { failures++; console.log("FAIL:", m); } else console.log("ok  :", m); };
const eq = (a, b, m) => ok(JSON.stringify(a) === JSON.stringify(b), m + " -> " + JSON.stringify(a));

const WEB = path.join(__dirname, "..", "web");
const html = fs.readFileSync(path.join(WEB, "index.html"), "utf8");
const navHtml = html.slice(html.indexOf('<nav class="nav"'), html.indexOf("</nav>"));

// ---------- navKeyFor ----------
const K = OG.navKeyFor;
ok(typeof K === "function", "navKeyFor is exported for Node");
eq(K("/"), "home", "/ is the homepage");
eq(K("/markets"), "markets", "/markets: the market dashboard");
eq(K("/developers"), "developers", "/developers");
eq(K("/find"), "find", "/find");
eq(K("/gpus"), "gpus", "/gpus");
eq(K("/gpu/h100-80gb-sxm5"), "gpus", "a GPU page highlights GPUs");
eq(K("/provider/lambda"), "providers", "a provider page highlights Providers");
eq(K("/compare/h100-80gb-sxm5-vs-h200-141gb-sxm5"), "compare", "compare pair");
eq(K("/indices/gpu-h100"), "indices", "one index");
eq(K("/methodology/data-kinds"), "methodology", "a methodology doc");
eq(K("/jobs/new"), "jobs", "new job");
eq(K("/jobs/abc"), "jobs", "one job");
eq(K("/deployments/42"), "deployments", "one deployment");
eq(K("/reserve/7"), "reserve", "one price-lock request");
eq(K("/admin/execution"), "admin-execution", "admin pages map to admin-<x>");
eq(K("/admin/value"), "admin-value", "admin value");
eq(K("/markets?x=1"), "markets", "a query string is ignored");
eq(K("/watchlists"), "watchlists", "other first segments map to themselves");

// ---------- the static nav ----------
const keys = [...navHtml.matchAll(/data-nav="([^"]+)"/g)].map(m => m[1]);
eq(keys.length, new Set(keys).size, "every data-nav is unique (one link per key)");
const primary = [...navHtml.matchAll(/<a href="([^"]+)" data-nav="[^"]+" class="nav-p"[^>]*>([^<]+)<\/a>/g)].map(m => [m[1], m[2]]);
eq(primary, [["/gpus", "GPUs"], ["/find", "Find Compute"], ["/jobs", "My Jobs"], ["/markets", "Markets"], ["/developers", "Developers"]], "five primary destinations, in order");
const group = g => {
  const i = navHtml.indexOf(`data-group="${g}"`);
  const ends = ["nav-sec", "nav-util"].map(t => navHtml.indexOf(t, i + 20)).filter(x => x > 0);
  const j = ends.length ? Math.min(...ends) : -1;
  return navHtml.slice(i, j < 0 ? undefined : j);
};
const hrefs = s => [...s.matchAll(/href="([^"]+)"/g)].map(m => m[1]);
eq(hrefs(group("gpus")), ["/gpus", "/compare", "/explorer", "/heatmaps", "/providers"], "GPUs group: Compare, Explorer, Heatmaps, Providers");
eq(hrefs(group("jobs")), ["/jobs", "/deployments", "/reserve", "/watchlists", "/route"], "My Jobs group: Deployments, Reserve, Watchlists, Route");
eq(hrefs(group("markets")), ["/markets", "/indices", "/opportunities", "/events", "/news"], "Markets group: Indices, Opportunities, Events, News");
eq(hrefs(group("developers")), ["/developers", "/api", "/keys", "/onboarding"], "Developers group: API docs, API keys, Onboarding");
ok(/<div class="nav-util">[\s\S]*href="\/methodology"[\s\S]*href="\/ops"/.test(navHtml), "utility links: Methodology, Ops");
ok(/class="nav-sec nav-admin"[^>]*hidden/.test(navHtml) && ["/admin/execution", "/admin/checklist", "/admin/partners", "/admin/value"].every(h => hrefs(group("admin")).includes(h)), "admin group: hidden until /v1/me says admin");
ok(html.includes('href="/reserve"') && html.includes("/static/pages/reserve.js"), "reserve link + script (test_reservations relies on it)");
ok(/id="nav-toggle"[^>]*aria-controls="main-nav"/.test(html) && html.includes('id="main-nav"'), "mobile menu button controls the nav");
// every page module has its script tag
for (const f of fs.readdirSync(path.join(WEB, "pages")).filter(f => f.endsWith(".js")))
  ok(html.includes(`/static/pages/${f}"`), `index.html loads pages/${f}`);
ok(/<script src="\/static\/pages\/find\.js">/.test(html), "/find (Find Compute) is loaded");

// ---------- every client page highlights a link that exists ----------
const navSet = new Set(keys);
for (const f of fs.readdirSync(path.join(WEB, "pages")).filter(f => f.endsWith(".js"))) {
  const src = fs.readFileSync(path.join(WEB, "pages", f), "utf8");
  for (const m of src.matchAll(/OG\.page\("([^"]+)",\s*\{([\s\S]*?)mount/g)) {
    const pattern = m[1];
    const explicit = /nav:\s*"([^"]+)"/.exec(m[2]);
    const sample = pattern.replace(/:a-vs-:b/, "a-vs-b").replace(/:\w+/g, "x");
    const key = explicit ? explicit[1] : K(sample);
    ok(key === "home" || navSet.has(key), `${pattern} highlights nav "${key}"`);
  }
}

// ---------- homepage helper ----------
const P = OG.home.popular;
const rows = [
  { gpu: "A", providers: 3, lowest: 2 }, { gpu: "B", providers: 9, lowest: 4 }, { gpu: "C", providers: 9, lowest: 1 },
  { gpu: "D", providers: 0, lowest: null }, { gpu: "E", providers: 2, lowest: null },
];
eq(P(rows, 10).map(r => r.gpu), ["C", "B", "A"], "popular: most providers first, cheapest breaks ties, unpriced left out");
eq(P(rows, 2).map(r => r.gpu), ["C", "B"], "popular: limited to n");
eq(P(null), [], "popular: no board -> empty");
ok(OG.home.DEMO === "/jobs/new?preset=mnist&sandbox=1", "demo link opens the MNIST preset in the sandbox");
const jobs = fs.readFileSync(path.join(WEB, "pages", "jobs.js"), "utf8");
ok(jobs.includes('query.sandbox === "1"') && jobs.includes('const simple = query.advanced !== "1"'), "jobs.js: /jobs/new opens the simple three-choice view (the demo link lands there); ?advanced=1 opens the full form");

// ---------- every page module parses (a syntax error blanks the whole SPA) ----------
const vm = require("vm");
for (const f of fs.readdirSync(path.join(WEB, "pages")).filter(f => f.endsWith(".js"))) {
  let err = null;
  try { new vm.Script(fs.readFileSync(path.join(WEB, "pages", f), "utf8"), { filename: f }); } catch (e) { err = e; }
  ok(!err, `pages/${f} parses${err ? ": " + err.message : ""}`);
}

// ---------- developers page: config snippets reference env vars / inputs, never a literal key ----------
const dev = fs.readFileSync(path.join(WEB, "pages", "developers.js"), "utf8");
require("../web/pages/developers.js");
const S = OG.developers.snippets("https://og.example");
ok(S.url === "https://og.example/mcp", "hosted MCP URL is <base>/mcp");
ok(S.claude.includes("--transport http opengrid https://og.example/mcp") && S.claude.includes("'Authorization: Bearer ${OPENGRID_API_KEY}'"),
  "Claude Code: http transport, key expanded at runtime from single-quoted ${OPENGRID_API_KEY}");
ok(JSON.parse(S.cursorJson).mcpServers.opengrid.headers.Authorization === "Bearer ${env:OPENGRID_API_KEY}", "Cursor: ${env:} interpolation in headers");
const link = new URL(S.cursorLink.replace("cursor://", "https://"));
ok(link.searchParams.get("name") === "opengrid" && JSON.parse(Buffer.from(link.searchParams.get("config"), "base64").toString()).url === "https://og.example/mcp",
  "Add to Cursor deeplink: base64 of the inner server object");
const vs = JSON.parse(S.vscodeJson);
ok(vs.servers.opengrid.type === "http" && vs.servers.opengrid.headers.Authorization === "Bearer ${input:opengrid-key}" && vs.inputs[0].password === true, "VS Code: secure promptString input");
ok(S.codex === "codex mcp add opengrid --url https://og.example/mcp --bearer-token-env-var OPENGRID_API_KEY", "Codex: bearer token from env var");
ok(S.localClaude.includes('uvx --from "git+https://github.com/fzsheik/opengrid-terminal#subdirectory=cli" opengrid mcp') && S.localClaude.includes("'OPENGRID_API_KEY=${OPENGRID_API_KEY}'"),
  "local zero-install server via uvx + git URL, key indirect");
ok(JSON.parse(S.localJson).mcpServers.opengrid.env.OPENGRID_URL === "https://og.example", "local server is told the OpenGrid URL");
ok(Object.values(S).every(v => !/opg_[A-Za-z0-9]{6,}/.test(v)) && !/opg_live_[A-Za-z0-9]/.test(dev), "no literal key in any snippet or the page");
ok(["opengrid_search_gpus", "opengrid_compare_gpus", "opengrid_get_gpu_specs", "opengrid_get_market_context", "opengrid_analyze_project", "opengrid_estimate_requirements",
  "opengrid_recommend_compute", "opengrid_preview_route", "opengrid_list_jobs", "opengrid_get_job_status", "opengrid_get_job_logs", "opengrid_start_demo", "opengrid_cancel_job"].every(t => dev.includes(`"${t}"`)), "every MCP tool listed");
ok(dev.includes('href: "/keys?preset=assistant"'), "step 1 links to the assistant key preset");
ok(S.curl.includes("/v1/gpus") && S.curlRec.includes("/v1/recommend") && dev.includes('href: "/api"'), "API examples + link to the full docs");

if (failures) { console.log(`\n${failures} FAILED`); process.exit(1); }
console.log("\nall nav tests passed");
