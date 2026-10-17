// The shared shell (web/core.js, web/app.css, web/pages/home.js): plain-language errors and freshness, the tape
// fit, command-bar parsing, the access vocabulary, the type scale (no raw px font sizes), control sizes, focus
// rings and WCAG AA contrast of the text tokens in both themes, and the homepage's first screen.
// Run: node tests/test_web_shell.js
const fs = require("fs");
const path = require("path");
const OG = require("../web/core.js");
let failures = 0;
const ok = (c, m) => { if (!c) { failures++; console.log("FAIL:", m); } else console.log("ok  :", m); };
const eq = (a, b, m) => ok(JSON.stringify(a) === JSON.stringify(b), m + " -> " + JSON.stringify(a));
const WEB = path.join(__dirname, "..", "web");

// ---------- errors read as sentences; HTTP and endpoints only in the technical detail ----------
const E = OG.explainError;
const e401 = E({ status: 401, message: "Authentication required", path: "http://x.example/v1/jobs" });
eq([e401.title, e401.action], ["Invite only", "signin"], "401: an access state (invite only), not a failure");
ok(/invite-only/.test(e401.text) && /API key/.test(e401.text) && !/HTTP|401|\/v1/.test(e401.title + e401.text), "401: explains invite-only accounts and API keys, no HTTP or endpoint in the words");
const core = fs.readFileSync(path.join(WEB, "core.js"), "utf8");
ok(core.includes('"/developers#access" }, "How to get an API key")') && core.includes('"OpenGrid staff: sign in"'), "401 state: how to get an API key, plus the small staff sign-in (the only browser login)");
ok(!/Sign-in required|Needs an account|needs an account/.test(core), "one access word in the shell: invite only (no 'needs an account' / 'sign-in required')");
ok(e401.tech.includes("HTTP 401") && e401.tech.includes("/v1/jobs") && !e401.tech.includes("x.example"), "401: status and path kept as technical detail (origin stripped)");
for (const [st, title] of [[403, "Not included in your access"], [404, "Not found"], [429, "Too many requests"], [0, "Could not reach OpenGrid"], [503, "OpenGrid had a problem"]]) {
  const x = E({ status: st, message: "m", path: "/v1/x" });
  ok(x.title === title && !/HTTP|\/v1/.test(x.title + x.text), `${st}: "${title}" in plain words`);
}
eq(E({ status: 404 }).action, null, "404: no retry offered (retrying will not help)");
eq(E({ status: 503 }).action, "retry", "5xx: try again");
ok(E(new Error("boom")).title === "Could not reach OpenGrid", "an error without a status is treated as a connection problem");

// ---------- freshness in words ----------
const F = OG.freshnessText, now = Date.parse("2026-10-15T12:00:00Z");
const at = s => new Date(now - s * 1000).toISOString();
eq(F(at(10), now, true).text, "Prices updated just now", "fresh: just now");
eq(F(at(180), now, true).text, "Prices updated 3 min ago", "fresh: minutes");
ok(F(at(180), now, true).title.includes("(your time)"), "the exact time is in the tooltip");
eq([F(at(1500), now, true).tone, F(at(1500), now, true).text], ["stale", "Prices may be out of date: last read 25 min ago"], "older than 10 min: may be out of date");
eq(F(at(7200), now, true).text, "Prices may be out of date: last read 2 hours ago", "hours");
eq([F(null, now, false).tone, F(null, now, false).text], ["down", "Can't reach OpenGrid"], "no answer: can't reach, never jargon");
eq(F(null, now, null).text, "Connecting…", "before the first answer");
eq(F(null, now, true).text, "Prices loaded", "prices answered but the read time is unknown: no made-up freshness");
// the freshness is the last provider read (newest successful fetch), never the response time
const R = OG.lastReadOf;
eq(R([{ last_ok_fetch: "2026-10-15T11:50:00Z" }, { last_ok_fetch: null }, { last_ok_fetch: "2026-10-15T11:58:00Z" }, {}]), "2026-10-15T11:58:00Z", "last read: the newest successful fetch");
eq(R([{ last_ok_fetch: null }]), null, "no successful fetch: unknown");
eq(R(null), null, "no provider list: unknown");
ok(/OG\.status\.asOf\(read\)/.test(core) && !/OG\.status\.asOf\(m\.t1\)/.test(core), "the shell drives 'Prices updated' from the last provider read, not /market's response time");
for (const f of ["home.js", "overview.js"]) {
  const src = fs.readFileSync(path.join(WEB, "pages", f), "utf8");
  ok(!/OG\.status\.(asOf|tape|live)\(/.test(src), `${f}: leaves the status bar and the ticker to the shell (one meaning, one format on every page)`);
}
const ov = fs.readFileSync(path.join(WEB, "pages", "overview.js"), "utf8");
ok(/h\("details", \{ class: "ov-adv" \}/.test(ov) && !/"σ 1h"|label: "z"|label: "n"|label: "Δ"|label: "Prov"/.test(ov), "/markets: secondary signals folded, no symbol-only column names");
ok(!/request failed|service did not answer|"…"/.test(ov), "/markets: no internal error text or bare '…' placeholders");

// ---------- tape: whole items only, room kept for "+N more" ----------
const T = OG.tapeFit;
eq(T([100, 100, 100], 320, 10, 40), 3, "everything fits: no +N");
eq(T([100, 100, 100], 250, 10, 40), 1, "two would fit but not with +N: one shown");
eq(T([100, 100, 100], 270, 10, 40), 2, "two plus +N");
eq(T([300], 100, 10, 40), 0, "nothing fits whole: only +N");
eq(T([], 100, 10, 40), 0, "empty tape");

// ---------- command parsing ----------
const C = OG.commandParts;
eq(C("g <gpu>          open a GPU market"), { syntax: "g <gpu>", desc: "open a GPU market", insert: "g " }, "argument command: click fills in the prefix");
eq(C("markets          market overview").insert, null, "no-argument command runs on click");
eq(C("theme [light|dark]  switch colour theme").insert, "theme ", "optional argument");

// ---------- access vocabulary ----------
ok(/invite-only/.test(OG.ACCESS.account.text) && /no self-serve sign-up/.test(OG.ACCESS.account.text), "account: honest about invite-only, no sign-up promised");
ok(/no GPU is rented and nothing is charged/.test(OG.ACCESS.simulated.text), "simulation: no GPU, no charge");

// ---------- CSS: one type scale, real controls, visible focus ----------
const css = fs.readFileSync(path.join(WEB, "app.css"), "utf8");
const raw = css.match(/font(?:-size)?:[^;{}]*\b\d+(?:\.\d+)?px/g) || [];
eq(raw, [], "no raw px font sizes: every size is a --fs-* token");
for (const f of ["compute.css", "devx.css"]) if (fs.existsSync(path.join(WEB, f))) {
  const other = fs.readFileSync(path.join(WEB, f), "utf8").match(/font(?:-size)?:[^;{}]*\b\d+(?:\.\d+)?px/g) || [];
  eq(other, [], `${f}: page stylesheets use the same --fs-* tokens`);
}
const tok = n => { const m = new RegExp(`--${n}:\\s*([\\d.]+)px`).exec(css); return m ? +m[1] : null; };
ok(tok("fs-base") >= 14 && tok("fs-2xs") >= 11 && tok("fs-data") >= 12.5, `type floor: body ${tok("fs-base")}px, smallest ${tok("fs-2xs")}px, tables ${tok("fs-data")}px`);
ok(tok("ctl-h") >= 32 && tok("ctl-h-sm") >= 26, `controls: ${tok("ctl-h")}px, compact ${tok("ctl-h-sm")}px`);
ok(/\.btn \{[^}]*height: var\(--ctl-h\)/.test(css) && /\.field \{[^}]*height: var\(--ctl-h\)/.test(css) && /#cmd \{[^}]*height: var\(--ctl-h\)/.test(css), "buttons, inputs and the search box use --ctl-h");
ok(/@media \(pointer: coarse\) \{[^}]*min-height: 44px/.test(css), "touch screens: 44px targets");
ok(/:focus-visible \{ outline: 2px solid var\(--accent\)/.test(css), "focus ring: 2px accent");
// WCAG AA (4.5:1) for every text token on the page and panel backgrounds, both themes
const block = sel => css.slice(css.indexOf(sel + " {"), css.indexOf("}", css.indexOf(sel + " {")));
const col = (b, n) => (new RegExp(`--${n}:\\s*(#[0-9a-fA-F]{6})`).exec(b) || [])[1];
const lum = hex => { const c = [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16) / 255).map(x => x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4); return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]; };
const ratio = (a, b) => { const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p); return (x + 0.05) / (y + 0.05); };
for (const [theme, b] of [["dark", block(":root")], ["light", block(':root[data-theme="light"]')]]) {
  for (const fg of ["text", "text-2", "muted", "dim", "accent", "up", "down", "warn"]) for (const bg of ["bg", "surface"]) {
    const r = ratio(col(b, fg), col(b, bg));
    ok(r >= 4.5, `${theme}: --${fg} on --${bg} ${r.toFixed(2)}:1 >= 4.5`);
  }
}

// ---------- homepage: the first screen ----------
const home = fs.readFileSync(path.join(WEB, "pages", "home.js"), "utf8");
ok(home.includes('"Compare GPU prices. Find the right compute. Run supported workloads."'), "headline says what OpenGrid does");
ok(["Explore GPU prices", "Find compute for my project", "Try the free training demo"].every(t => home.includes(`"${t}"`)), "the three primary actions (sentence case)");
ok(/demoAct = action\(demoBtn/.test(home) && /OG\.me\(\)\.then\(me => \{\s*if \(!me/.test(home), "a visitor's demo action explains invite-only access in place; only a signed-in user gets the /jobs/new link");
ok(home.includes('"Invite only"') && home.includes('"Simulation · free"') && (home.match(/"Free · no account"/g) || []).length === 2, "every action is tagged: free / simulation / invite only");
ok(home.includes('"Running your own code", "not yet"'), "what works today: says plainly what does not work yet");
ok(home.includes('"GPU prices right now"') && !/hm-how|hm-stats|\/v1\/indices|hm-more-row/.test(home), "homepage: headline, what works today, three actions and the price board; nothing else");
ok(!/[\u2013\u2014]/.test(home + ov), "no en or em dashes in the homepage or /markets copy");
ok(!/arbitrary|any workload|run anything/i.test(home), "no claim that arbitrary workloads run today");

console.log(failures ? `\n${failures} FAILED` : "\nall shell tests passed");
process.exit(failures ? 1 : 0);
