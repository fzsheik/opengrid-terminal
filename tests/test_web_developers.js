// Developer experience pages: /developers (setup snippets, access states, capability boundaries), /api (the curated
// public catalogue), /keys (presets), /onboarding (GPU names -> slugs) and the assistant's capability list.
// Pure parts only, under Node. The server-side cross-checks (public routes, CLI argument parsing, assistant
// intents) are in tests/test_developer_docs.py.
// Run: node tests/test_web_developers.js   (or with --dump: prints the snippets as JSON for the Python test)
const fs = require("fs");
const path = require("path");
const OG = require("../web/core.js");
global.OG = OG;
require("../web/pages/developers.js");
require("../web/pages/api.js");
require("../web/pages/keys.js");
require("../web/pages/onboarding.js");
require("../web/pages/assistant.js");

const S = OG.developers.snippets("https://og.example/");
if (process.argv.includes("--dump")) {
  process.stdout.write(JSON.stringify({ snippets: S, publicOps: OG.apiDocs.publicOps(), capabilities: OG.assistant.CAPABILITIES, presets: OG.keysPage.PRESETS }));
  process.exit(0);
}
let failures = 0;
const ok = (c, m) => { if (!c) { failures++; console.log("FAIL:", m); } else console.log("ok  :", m); };
const WEB = path.join(__dirname, "..", "web");
const src = f => fs.readFileSync(path.join(WEB, "pages", f), "utf8");
const GIT = "git+https://github.com/fzsheik/opengrid-terminal#subdirectory=cli";

// ---------- snippets: shapes verified in Claude Code 2.1.301 / Codex 0.159.0 (see developers.js VERIFIED) ----------
ok(S.base === "https://og.example" && S.url === "https://og.example/mcp", "trailing slash dropped; hosted MCP URL is <base>/mcp");
ok(S.localClaude.startsWith("claude mcp add opengrid ") && S.localClaude.includes("--transport stdio") && S.localClaude.includes("--env OPENGRID_URL=https://og.example")
  && S.localClaude.includes(`-- uvx --from "${GIT}" opengrid mcp`), "Claude Code local: name before the variadic --env, stdio, uvx from the public repo");
ok(S.localCodex.startsWith("codex mcp add opengrid --env OPENGRID_URL=https://og.example") && S.localCodex.includes(`-- uvx --from "${GIT}" opengrid mcp`), "Codex local: the registration command that was tested");
ok(S.hostedClaude.includes("--transport http https://og.example/mcp") && S.hostedClaude.includes("'Authorization: Bearer ${OPENGRID_API_KEY}'"), "Claude Code hosted: single-quoted header, expanded by Claude Code at connect time");
ok(S.hostedCodex.replace(/\\\n\s*/g, "") === "codex mcp add opengrid --url https://og.example/mcp --bearer-token-env-var OPENGRID_API_KEY", "Codex hosted: bearer token from the env var");
const J = k => { try { return JSON.parse(S[k]); } catch (e) { return null; } };
ok(["localCursor", "localVscode", "localDesktop", "hostedCursor", "hostedVscode"].every(k => J(k)), "every JSON config parses");
const local = [J("localCursor").mcpServers.opengrid, J("localVscode").servers.opengrid, J("localDesktop").mcpServers.opengrid];
ok(local.every(s => s.command === "uvx" && JSON.stringify(s.args) === JSON.stringify(["--from", GIT, "opengrid", "mcp"]) && s.env.OPENGRID_URL === "https://og.example"), "local JSON configs: uvx + OPENGRID_URL");
ok(local.every(s => !("OPENGRID_API_KEY" in s.env)), "local configs carry no key at all (anonymous, or the key stored by opengrid auth login)");
ok(J("localVscode").servers.opengrid.env.OPENGRID_ALLOWED_ROOTS === "${workspaceFolder}" && J("localDesktop").mcpServers.opengrid.env.OPENGRID_ALLOWED_ROOTS.startsWith("/"), "analysis roots: VS Code workspace folder; Desktop an absolute path to fill in");
ok(J("hostedCursor").mcpServers.opengrid.headers.Authorization === "Bearer ${env:OPENGRID_API_KEY}", "Cursor hosted: ${env:} interpolation");
const vs = J("hostedVscode");
ok(vs.servers.opengrid.type === "http" && vs.servers.opengrid.headers.Authorization === "Bearer ${input:opengrid-api-key}" && vs.inputs[0].password === true && vs.inputs[0].id === "opengrid-api-key", "VS Code hosted: secret promptString input (same id as `opengrid mcp config vscode`)");
const link = new URL(S.cursorLink.replace("cursor://", "https://"));
ok(link.searchParams.get("name") === "opengrid" && JSON.parse(Buffer.from(link.searchParams.get("config"), "base64").toString()).url === "https://og.example/mcp", "Add to Cursor deeplink: base64 of the server object");
ok(S.curlCheapest === 'curl -s "https://og.example/v1/best/h100?mode=CHEAPEST&limit=1"' && !S.curlCheapest.includes("Authorization"), "first success: a public call with no key");
ok(!S.curlRecommend.includes("Authorization") && S.curlKeyed.includes('Bearer $OPENGRID_API_KEY') && S.curlMint.includes("/v1/keys"), "recommend is public; previews and minting send the key from the env var");
ok(S.termTry.includes("export OPENGRID_URL=https://og.example") && S.authLogin.includes("export OPENGRID_URL=https://og.example"), "terminal snippets always set OPENGRID_URL (the client's default is a local dev server)");
ok(Object.values(S).every(v => !/opg_(live_)?[A-Za-z0-9]{6,}/.test(v)), "no literal key in any snippet");

// ---------- one canonical env var, no stale copy ----------
for (const f of ["developers.js", "api.js", "keys.js", "onboarding.js", "assistant.js"]) {
  const t = src(f);
  ok(!/\bOPENGRID_KEY\b/.test(t), `${f}: no OPENGRID_KEY (the CLI and MCP read OPENGRID_API_KEY)`);
  ok(!/opg_live_[A-Za-z0-9]{4,}/.test(t), `${f}: no literal key`);
}
const dev = src("developers.js"), api = src("api.js");
ok(!/Three steps, nothing to install/.test(dev) && /needs uv \+ git/.test(dev), "no blanket 'nothing to install': the local server and CLI say they need uv and git");
ok(!/Three steps/.test(dev) && !dev.includes('href: "/keys?preset=assistant"'), "no 'create a key' step for visitors who cannot create one");
ok(/no self-service sign-up/.test(dev) && dev.includes("issues/new"), "access: honest about operator-issued keys, with the real request channel");
ok(!/Every \/v1 endpoint needs a key/i.test(api) && !/signed-in operator; your key is not needed/.test(api), "api: no 'every endpoint needs a key', no 'runs as the operator' for visitors");
ok(/if \(operator\) fullList\(/.test(api) && !/ctx\.api\("\/openapi\.json"\)[^;]*;\s*\n\s*const ops/.test(api.split("async function fullList")[0]), "api: /openapi.json is read only for the operator");

// ---------- plain-words form errors and one access state (no "Could not reach OpenGrid" for a validation message) ----------
const keysSrc = src("keys.js"), obSrc = src("onboarding.js");
for (const [f, t] of [["api.js", api], ["keys.js", keysSrc], ["onboarding.js", obSrc]])
  ok(!/OG\.error\(\{\s*message/.test(t), `${f}: no OG.error({message}) (it has no status, so it reads as a network failure)`);
ok(/function formError/.test(dev) && /\[400, 409, 422\]/.test(dev), "OG.dx.formError: client checks and 400/409/422 render the server's message");
ok(obSrc.includes("formError(err)") && /OG\.accessNote\("operator"\)/.test(obSrc) && !/status: 403/.test(obSrc), "/onboarding and /admin/partners: validation in plain words; non-operators get one access note");
ok(/OG\.accessNote\("account"/.test(dev), "visitor access uses the shared account note");
ok(/missing\.length/.test(api), "api Try it: empty required parameters are asked for, not sent");
for (const f of ["developers.js", "api.js", "keys.js", "onboarding.js"]) ok(!/[–—]/.test(src(f)), `${f}: no en or em dashes`);
ok(!/[–—]/.test(fs.readFileSync(path.join(WEB, "devx.css"), "utf8")), "devx.css: no en or em dashes");

// ---------- access states ----------
const A = OG.developers.accessState;
ok(A(null) === "anonymous" && A({ principal: "operator", scopes: ["*"] }) === "operator" && A({ principal: "api_key", scopes: ["data:read"] }) === "key", "accessState: anonymous / operator / key");

// ---------- capability boundaries ----------
const B = OG.developers.BOUNDARIES;
const row = re => B.find(r => re.test(r[0]));
ok(row(/real GPU/).slice(1, 5).every(v => v === "no") && row(/own code/).slice(1, 5).every(v => v === "no"), "real GPU runs and own code: no on every surface");
ok(/^no/.test(row(/Analyze a project/)[3]) && row(/Analyze a project/)[2] === "yes", "project analysis: local MCP yes, hosted no");
ok(OG.developers.TOOLS.length === 13 && OG.developers.TOOLS.filter(t => t[3]).map(t => t[0]).join() === "opengrid_analyze_project", "13 MCP tools; only analyze is local-only");

// ---------- /api catalogue ----------
const ops = OG.apiDocs.publicOps();
ok(ops.length === new Set(ops).size, "public catalogue: no duplicates");
const PRIVATE_AREAS = ["route", "deployments", "keys", "admin", "ops", "me", "usage", "billing", "watchlists", "alerts", "credentials", "jobs", "quotes", "reservations"];
ok(ops.every(o => !PRIVATE_AREAS.includes(o.split(" ")[1].slice(4).split("/")[0])), "public catalogue lists no private area");
ok(ops.includes("GET /v1/best/{gpu}") && ops.includes("POST /v1/recommend"), "the quickstart endpoints are in the catalogue");

// ---------- /keys presets ----------
const P = OG.keysPage;
ok(P.PRESETS.filter(p => p[3]).every(p => p[3].every(s => !P.ELEVATED.includes(s))), "presets never include route:execute, account:manage or admin");
ok(JSON.stringify(P.presetScopes("read")) === '["data:read"]' && P.presetScopes("custom") === null, "read-only preset; custom has no preset scopes");
ok(P.FALLBACK_SCOPES.some(s => s[0] === "keys:read"), "fallback scope list matches accounts/auth.py (keys:read)");
ok(JSON.stringify(P.scopeRows([{ scope: "data:read", description: "d" }])) === '[["data:read","d",""]]', "scopeRows reads the /v1/scopes shape");

// ---------- /onboarding: GPU names ----------
const L = require("../web/lib.js");
const gpus = ["NVIDIA H100 80GB SXM5", "NVIDIA H100 80GB PCIe", "NVIDIA A100 80GB SXM4", "NVIDIA A100 40GB SXM4", "NVIDIA L40S 48GB"].map(n => ({ slug: OG.slug(n), name: n, short: L.shortGpu(n) }));
const r = OG.onboarding.resolveGpuNames("H100 SXM5, nvidia a100 80gb sxm4, h100-80gb-sxm5, H100 PCIe, L40S, H1OO, A100 SXM4", gpus);
ok(JSON.stringify(r.slugs) === '["h100-80gb-sxm5","a100-80gb-sxm4","h100-80gb-pcie","l40s-48gb"]', "names with or without memory size, full names and slugs resolve; duplicates collapse -> " + JSON.stringify(r.slugs));
ok(JSON.stringify(r.unknown) === '["H1OO"]' && r.ambiguous.length === 1 && /A100 40GB SXM4/.test(r.ambiguous[0]), "a typo is reported, and a name that fits two GPUs is called ambiguous, never guessed");
ok(OG.onboarding.gpuLabel("h100-80gb-sxm5", gpus) === "H100 80GB SXM5" && OG.onboarding.gpuLabel("x", gpus) === "x", "stored slugs are shown as names");
ok(OG.onboarding.STEPS.length === 6 && !OG.onboarding.STEPS.some(s => /why/i.test(s[1])), "six steps, each with a server-side completion state");

// ---------- assistant ----------
const AS = OG.assistant;
ok(AS.CAPABILITIES.length >= 6 && AS.CAPABILITIES.every(c => c[2] && c[2].length < 120), "capability list with an example each");
ok(AS.isUnknown({ intent: "unknown", cards: [] }) && !AS.isUnknown({ intent: "cheapest", cards: [{}] }), "an unrecognised question brings the examples back");
ok(AS.cardKind({ type: "template" }) === "template", "template cards are rendered (were dropped)");
const asrc = src("assistant.js");
ok(!/deterministic answer|phrased by|check the cards/.test(asrc), "no engine labels in normal output");
ok(!/It cannot launch or buy anything/.test(asrc), "panel note no longer contradicts its own demo / cancel buttons");

if (failures) { console.log(`\n${failures} FAILED`); process.exit(1); }
console.log("\nall developer page tests passed");
