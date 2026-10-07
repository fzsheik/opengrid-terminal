// Pure parts of web/core.js and web/charts.js. Run: node tests/test_web_core.js
const OG = require("../web/core.js");
const C = require("../web/charts.js");
let failures = 0;
const ok = (c, m) => { if (!c) { failures++; console.log("FAIL:", m); } else console.log("ok  :", m); };
const eq = (a, b, m) => ok(JSON.stringify(a) === JSON.stringify(b), m + " -> " + JSON.stringify(a));

// ---------- router ----------
const routes = ["/", "/gpus", "/gpu/:slug", "/compare", "/compare/:a-vs-:b", "/methodology", "/methodology/:name", "/indices/:id", "/files/*"]
  .map(p => ({ pattern: p, compiled: OG.compile(p) }));
const m = p => { const r = OG.matchRoute(routes, p); return r ? [r.route.pattern, r.params] : null; };
eq(m("/"), ["/", {}], "root");
eq(m("/gpus"), ["/gpus", {}], "literal");
eq(m("/gpus/"), ["/gpus", {}], "trailing slash tolerated");
eq(m("/gpu/h100-80gb-sxm5"), ["/gpu/:slug", { slug: "h100-80gb-sxm5" }], "param");
eq(m("/gpu/a%20b"), ["/gpu/:slug", { slug: "a b" }], "params are URI-decoded");
eq(m("/gpu/%E0%A4%A"), ["/gpu/:slug", { slug: "%E0%A4%A" }], "a malformed escape does not throw");
eq(m("/compare/h100-80gb-sxm5-vs-h200-141gb-sxm5"), ["/compare/:a-vs-:b", { a: "h100-80gb-sxm5", b: "h200-141gb-sxm5" }], "compare pair with hyphenated slugs");
eq(m("/compare"), ["/compare", {}], "compare without pair");
eq(m("/methodology/data-kinds"), ["/methodology/:name", { name: "data-kinds" }], "methodology doc");
eq(m("/files/a/b/c"), ["/files/*", { rest: "a/b/c" }], "rest wildcard");
ok(m("/gpu") === null && m("/gpu/a/b") === null && m("/nope") === null, "non-matches return null");
ok(OG.compile("/a.b").re.test("/a.b") && !OG.compile("/a.b").re.test("/axb"), "literal dots are escaped");
// specificity: a literal beats a param
const r2 = [{ compiled: OG.compile("/x/:id") }, { compiled: OG.compile("/x/new") }];
ok(OG.matchRoute(r2, "/x/new").route === r2[1], "most literal pattern wins regardless of order");

// legacy hash URLs
ok(OG.legacyHash("#/market") === "/gpus", "#/market -> /gpus");
ok(OG.legacyHash("#/market/" + encodeURIComponent("NVIDIA H100 80GB SXM5")) === "/gpu/h100-80gb-sxm5", "#/market/<gpu> -> /gpu/<slug>");
ok(OG.legacyHash("#/routing") === "/route" && OG.legacyHash("#/deploy") === "/deployments" && OG.legacyHash("") === null && OG.legacyHash("#top") === null && OG.legacyHash("#routing") === null, "other legacy hashes");

// ---------- query strings ----------
eq(OG.qs.parse("?a=1&b=x%20y&c&d=a+b"), { a: "1", b: "x y", c: "", d: "a b" }, "parse");
eq(OG.qs.stringify({ a: 1, b: "x y", c: null, d: "", e: false, f: true, g: "1,2" }), "?a=1&b=x%20y&f=1&g=1,2", "stringify drops empty values, keeps commas readable");
ok(OG.qs.stringify({}) === "", "empty -> empty string");
const round = { gpu: "NVIDIA H100 80GB SXM5", q: "a&b=c?d", min: "1.5" };
eq(OG.qs.parse(OG.qs.stringify(round)), round, "round trip with reserved characters");
ok(OG.withParams("/x", { a: 1 }) === "/x?a=1" && OG.withParams("/x?z=1", { a: 2 }) === "/x?z=1&a=2" && OG.withParams("/x", null) === "/x", "withParams");

// ---------- slug == api.common.gpu_slug ----------
ok(OG.slug("NVIDIA H100 80GB SXM5") === "h100-80gb-sxm5", "slug drops vendor");
ok(OG.slug("AMD Instinct MI300X 192GB") === "mi300x-192gb" && OG.slug("Intel Gaudi 2 96GB") === "gaudi-2-96gb", "AMD / Intel");
ok(OG.slug("NVIDIA RTX PRO 6000 Blackwell 96GB (Workstation)") === "rtx-pro-6000-blackwell-96gb-workstation", "punctuation collapses, no trailing dash");

// ---------- formatting ----------
ok(OG.fmt.price(1.3) === "$1.30" && OG.fmt.price("2.5") === "$2.50" && OG.fmt.price(null) === "–" && OG.fmt.price("") === "–", "price (numbers, strings, null)");
ok(OG.fmt.pct(0.021) === "+2.1%" && OG.fmt.pct(-0.004) === "−0.4%", "pct uses lib");
ok(OG.fmt.num(1234567.891, 2) === "1,234,567.89" && OG.fmt.num(-1234) === "−1,234" && OG.fmt.num(0.5) === "0.50" && OG.fmt.num(null) === "–", "num groups the integer part only: " + OG.fmt.num(1234567.891, 2));
ok(OG.fmt.compact(1530) === "1.5k" && OG.fmt.compact(2500000) === "2.5M" && OG.fmt.compact(12) === "12" && OG.fmt.compact(-15300) === "−15k", "compact");
const now = "2026-10-06T12:00:00Z";
ok(OG.fmt.age("2026-10-06T11:59:58Z", now) === "now" && OG.fmt.age("2026-10-06T11:59:20Z", now) === "40s" && OG.fmt.age("2026-10-06T11:55:00Z", now) === "5m"
  && OG.fmt.age("2026-10-06T09:00:00Z", now) === "3h" && OG.fmt.age("2026-10-01T12:00:00Z", now) === "5d" && OG.fmt.age(90) === "1m" && OG.fmt.age(null) === "–", "age");
ok(OG.fmt.age("2026-10-06T12:05:00Z", now) === "now", "a clock skewed into the future reads now, not negative");
ok(OG.fmt.cls(0.02) === "up" && OG.fmt.cls(-0.02) === "down" && OG.fmt.cls(0.00001) === "flat" && OG.fmt.cls(null) === "none" && OG.fmt.cls(0.02, true) === "down", "change classes (and invert)");
ok(/^\d\d:\d\d:\d\d$/.test(OG.fmt.time(now)) && OG.fmt.time(null) === "–" && OG.fmt.time("garbage") === "–", "time");
ok(OG.freshness(60) === "fresh" && OG.freshness(3600) === "aging" && OG.freshness(5 * 3600) === "stale" && OG.freshness(null) === "unknown" && OG.freshness("aging") === "aging", "freshness thresholds");

// ---------- sorting ----------
const rows = [{ p: 3, n: "b" }, { p: null, n: "a" }, { p: 1, n: "c" }, { p: 2, n: null }, { p: "", n: "d" }];
const col = { key: "p" };
eq(OG.sortRows(rows, col, "asc").map(r => r.p), [1, 2, 3, null, ""], "numbers ascending, blanks last");
eq(OG.sortRows(rows, col, "desc").map(r => r.p), [3, 2, 1, null, ""], "descending: blanks still last");
eq(OG.sortRows(rows, { key: "n" }, "asc").map(r => r.n), ["a", "b", "c", "d", null], "strings, null last");
eq(OG.sortRows([{ s: "gpu10" }, { s: "gpu9" }, { s: "GPU1" }], { key: "s" }, "asc").map(r => r.s), ["GPU1", "gpu9", "gpu10"], "natural, case-insensitive order");
eq(OG.sortRows([{ a: 1, i: 0 }, { a: 1, i: 1 }, { a: 0, i: 2 }], { key: "a" }, "desc").map(r => r.i), [0, 1, 2], "stable for ties");
eq(OG.sortRows([{ x: { v: 2 } }, { x: { v: 1 } }], { key: "x", value: r => r.x.v }, "asc").map(r => r.x.v), [1, 2], "custom value()");
ok(OG.sortRows(rows, null).length === rows.length, "no column: copy unchanged");

// ---------- CSV ----------
ok(OG.csvCell('he said "hi", ok') === '"he said ""hi"", ok"', "quotes doubled, field quoted");
ok(OG.csvCell("a\nb") === '"a\nb"' && OG.csvCell("a\rb") === '"a\rb"', "newlines quoted");
ok(OG.csvCell("=HYPERLINK(1)") === "'=HYPERLINK(1)" && OG.csvCell("+1") === "'+1" && OG.csvCell("@x") === "'@x" && OG.csvCell("-2+3") === "'-2+3", "formula injection neutralised");
ok(OG.csvCell(-2.5) === "-2.5" && OG.csvCell(0) === "0" && OG.csvCell(NaN) === "" && OG.csvCell(null) === "" && OG.csvCell(true) === "true", "numbers stay numbers (negative not prefixed)");
const csv = OG.csvString([{ a: 1, b: "x,y", c: "hidden" }, { a: null, b: "z" }],
  [{ key: "a", label: "A" }, { key: "b", label: "B, quoted" }, { key: "c", label: "C", csv: false }, { key: "d", label: "D", csv: r => (r.a ? "has a" : "") }]);
ok(csv === 'A,"B, quoted",D\r\n1,"x,y",has a\r\n,z,\r\n', "csvString: header, escaping, csv:false skipped, csv() used: " + JSON.stringify(csv));

// ---------- commands & fuzzy ----------
eq(OG.parseCommand("  g   h100  "), { verb: "g", args: ["h100"] }, "parse command");
eq(OG.parseCommand("R h100 8 us"), { verb: "r", args: ["h100", "8", "us"] }, "verb lowercased, args kept");
eq(OG.parseCommand(""), { verb: "", args: [] }, "empty");
const gpus = [{ slug: "h100-80gb-pcie", weight: 2 }, { slug: "h100-80gb-sxm5", weight: 7 }, { slug: "h100-94gb-nvl", weight: 2 }, { slug: "gh200-96gb", weight: 1 }, { slug: "a100-80gb-sxm4", weight: 5 }];
const top = q => (OG.fuzzy(q, gpus, g => g.slug)[0] || {}).slug;
ok(top("h100") === "h100-80gb-sxm5", "h100 -> the most-traded H100 (weight breaks the prefix tie): " + top("h100"));
ok(top("h100 pcie") === "h100-80gb-pcie", "extra words narrow it: " + top("h100 pcie"));
ok(top("gh200") === "gh200-96gb" && top("a100") === "a100-80gb-sxm4", "prefix matches");
ok(top("h1sxm") === "h100-80gb-sxm5", "subsequence as a last resort");
ok(OG.fuzzy("zzz", gpus, g => g.slug).length === 0, "no match -> empty");
ok(OG.fuzzyScore("h100-80gb-sxm5", "h100-80gb-sxm5") > OG.fuzzyScore("h100", "h100-80gb-sxm5"), "exact beats prefix");
OG.command(/^g\s+(.+)$/i, () => {}, "g <gpu>");
OG.command(/^r\s+(\S+)(?:\s+(\d+))?(?:\s+(\S+))?$/i, () => {}, "r <gpu> [n] [region]");
OG.command(/^e$/i, () => {}, "e");
const fc = t => { const f = OG.findCommand(t); return f ? [f.cmd.usage, ...f.match.slice(1)] : null; };
eq(fc("g h100"), ["g <gpu>", "h100"], "g h100");
eq(fc("r h100 8 us"), ["r <gpu> [n] [region]", "h100", "8", "us"], "r h100 8 us");
eq(fc("r h100"), ["r <gpu> [n] [region]", "h100", undefined, undefined], "r with optional parts missing");
eq(fc("e"), ["e"], "single-letter command");
ok(fc("events") === null && fc("gx") === null, "no accidental matches");

// ---------- envelope ----------
eq(OG.unwrap({ data: [1], meta: { as_of: "t" } }), { data: [1], meta: { as_of: "t" } }, "envelope unwrapped");
eq(OG.unwrap({ data: null, meta: { reason: "thin" } }), { data: null, meta: { reason: "thin" } }, "null data keeps meta (insufficient coverage)");
eq(OG.unwrap({ gpus: [], data: 1, meta: {} }), { data: { gpus: [], data: 1, meta: {} }, meta: null }, "object with extra keys is not an envelope");
eq(OG.unwrap([1, 2]), { data: [1, 2], meta: null }, "plain arrays pass through");
ok(OG.reasonOf({ value: null, reason: "coverage below 3 providers" }) === "coverage below 3 providers" && OG.reasonOf({ unavailable_reason: "x" }) === "x" && OG.reasonOf(null) === null, "reasonOf");

// ---------- API client: dedupe + out-of-order ----------
(async () => {
  let calls = 0;
  const pending = {};
  global.fetch = (url) => { calls++; return new Promise(res => { pending[url] = () => res({ ok: true, status: 200, headers: { get: () => "application/json" }, json: async () => ({ data: url, meta: { as_of: "t" } }) }); }); };
  const a = OG.api("/v1/x"), b = OG.api("/v1/x");
  pending["/v1/x"]();
  const [ra, rb] = await Promise.all([a, b]);
  ok(calls === 1 && ra === "/v1/x" && rb === "/v1/x", "identical concurrent GETs share one request and unwrap data");
  const full = OG.api("/v1/y", { full: true }); pending["/v1/y"]();
  eq(await full, { data: "/v1/y", meta: { as_of: "t" } }, "full: data and meta");
  // newer request in the same slot wins even when the older answers last
  const old = OG.api("/v1/slow", { slot: "s" }), fresh = OG.api("/v1/fast", { slot: "s" });
  pending["/v1/fast"]();
  ok(await fresh === "/v1/fast", "newest in slot resolves");
  pending["/v1/slow"]();
  let stale = null; try { await old; } catch (e) { stale = e; }
  ok(stale && stale.stale === true, "older request in the slot rejects as stale");
  global.fetch = async () => ({ ok: false, status: 404, headers: { get: () => "application/json" }, json: async () => ({ detail: "unknown GPU 'x'" }) });
  let err = null; try { await OG.api("/v1/gpus/x"); } catch (e) { err = e; }
  ok(err && err.status === 404 && err.message === "unknown GPU 'x'", "errors carry status and the API's detail");
  ok(await OG.api.soft("/v1/missing") === null, "soft() returns null on error");
  global.fetch = async () => { throw new TypeError("offline"); };
  err = null; try { await OG.api("/v1/z"); } catch (e) { err = e; }
  ok(err && err.status === 0, "network failure -> status 0");

  // ---------- charts math ----------
  const sc = C.scale(0, 10, 100, 200);
  ok(sc(0) === 100 && sc(10) === 200 && sc(5) === 150 && sc.invert(150) === 5, "linear scale and invert");
  ok(C.scale(3, 3, 0, 100)(3) === 50, "zero-width domain centres instead of NaN");
  eq(C.extent([3, null, 1], [NaN, 7, undefined]), [1, 7], "extent skips nulls and NaN");
  ok(C.extent([null], []) === null, "extent of nothing is null");
  const pe = C.padExtent([1, 2], 0.1);
  ok(Math.abs(pe[0] - 0.9) < 1e-9 && Math.abs(pe[1] - 2.1) < 1e-9, "padExtent pads both ends");
  ok(C.padExtent([0.05, 0.1], 2)[0] === 0, "prices never pad below zero");
  const flat = C.padExtent([2, 2]); ok(flat[0] < 2 && flat[1] > 2, "flat range opens up");
  const t0 = Date.UTC(2026, 9, 6, 0, 7), t1 = t0 + 24 * 36e5;
  const tt = C.timeTicks(t0, t1, 6);
  ok(tt.length >= 4 && tt.length <= 9 && tt.every(t => t >= t0 && t <= t1), "time ticks inside the span: " + tt.length);
  ok(tt.every((t, i) => i === 0 || t - tt[i - 1] === tt[1] - tt[0]), "time ticks evenly stepped");
  ok(new Date(tt[0]).getMinutes() === 0, "time ticks land on round local times");
  const bn = C.bins([1, 1.1, 1.2, 2, 3.9, 4], 4);
  ok(bn.reduce((s, x) => s + x.count, 0) === 6 && bn[0].x0 <= 1 && bn[bn.length - 1].x1 >= 4, "bins cover every value: " + JSON.stringify(bn));
  ok(bn.every((x, i) => i === 0 || Math.abs(x.x0 - bn[i - 1].x1) < 1e-9), "bins are contiguous");
  eq(C.bins([5, 5, 5]), [{ x0: 5, x1: 5, count: 3 }], "single-value bins");
  ok(C.bins([null, NaN]).length === 0, "no values, no bins");
  ok(C.lerpColor("#000000", "#ffffff", 0.5) === "#808080" && C.lerpColor("#000000", "#ffffff", 2) === "#ffffff", "colour lerp clamps");
  ok(C.colorSeq(0) === C.SEQ[0] && C.colorSeq(1) === C.SEQ[C.SEQ.length - 1], "sequential ramp endpoints");
  ok(C.colorDiv(0) === "#3a3f46" && C.colorDiv(null) === null && C.colorDiv(-1) !== C.colorDiv(1), "diverging: grey midpoint, two distinct poles, null stays null");
  const xAt = i => i * 10, yAt = v => 100 - v;
  ok(C.bandPath([1, 2], [3, 4], xAt, yAt, false) === "M0.0 97.0L10.0 96.0L10.0 98.0L0.0 99.0Z", "band polygon: top then bottom reversed: " + C.bandPath([1, 2], [3, 4], xAt, yAt, false));
  ok((C.bandPath([1, null, 1, 1], [2, null, 2, 2], xAt, yAt).match(/Z/g) || []).length === 2, "a gap splits the band");
  ok(!/NaN/.test(C.bandPath([1], [2], xAt, yAt)), "single-point band has no NaN");
  ok(C.bandPath([1, 2], [3, 4], xAt, yAt, true).includes("L10.0 97.0L10.0 96.0"), "step band holds the previous value until the next sample");
  ok(C.nearest([0, 10, 20, 30], 14) === 1 && C.nearest([0, 10, 20, 30], 16) === 2 && C.nearest([0, 10], -5) === 0 && C.nearest([0, 10], 99) === 1 && C.nearest([], 1) === -1, "nearest index");

  // ---------- follow-up helpers ----------
  // freshness relative to the poll interval: fresh <= 2x, aging <= 6x, stale beyond
  ok(OG.freshness(70 * 60, null, 3600) === "fresh" && OG.freshness(2 * 3600, null, 3600) === "fresh" && OG.freshness(2 * 3600 + 1, null, 3600) === "aging"
    && OG.freshness(6 * 3600, null, 3600) === "aging" && OG.freshness(6 * 3600 + 1, null, 3600) === "stale", "hourly feed thresholds (AWS seen 70 min ago is fresh)");
  ok(OG.freshness(70 * 60) === "aging" && OG.freshness(25 * 60, null, 900) === "fresh" && OG.freshness(31 * 60, null, 900) === "aging" && OG.freshness(91 * 60, null, 900) === "stale", "default and 15-min thresholds");
  ok(OG.freshness(100, null, 0) === "fresh" && OG.freshness(100, null, null) === "fresh" && OG.freshness("stale", null, 3600) === "stale", "no interval falls back; labels pass through");
  eq(OG.freshLimits(600), [1200, 3600], "freshLimits");
  // null children skipped, arrays flattened
  eq(OG.cleanKids(["a", null, undefined, false, true, ["b", [null, "c"]], 0, ""]), ["a", "b", "c", 0, ""], "cleanKids drops null/undefined/booleans, keeps 0 and empty string");
  eq(OG.cleanKids(null), [], "cleanKids(null)");
  // family command routing
  const fams = [
    { slug: "h100", id: "H100", name: "H100 family", variants: [{ gpu: "NVIDIA H100 80GB SXM5", slug: "h100-80gb-sxm5" }, { gpu: "NVIDIA H100 80GB PCIe", slug: "h100-80gb-pcie" }, { gpu: "NVIDIA H100 NVL 94GB", slug: "h100-nvl-94gb" }] },
    { slug: "a10", id: "A10", name: "A10 family", variants: [{ gpu: "NVIDIA A10 24GB", slug: "a10-24gb" }] },
    { slug: "blackwell", id: "Blackwell", name: "Blackwell architecture", variant_count: 5 },
  ];
  eq(OG.familyTarget("h100", fams), { kind: "family", slug: "h100" }, "g h100 -> family page");
  eq(OG.familyTarget("H100", fams), { kind: "family", slug: "h100" }, "case-insensitive");
  eq(OG.familyTarget("h100 sxm", fams), { kind: "variant", slug: "h100-80gb-sxm5", family: "h100" }, "g h100 sxm -> the SXM5 variant");
  eq(OG.familyTarget("h100 pcie", fams), { kind: "variant", slug: "h100-80gb-pcie", family: "h100" }, "g h100 pcie -> PCIe");
  eq(OG.familyTarget("h100 80gb", fams), { kind: "family", slug: "h100" }, "ambiguous words -> family page, never a guess");
  ok(OG.familyTarget("a10", fams) === null, "a one-variant family keeps the plain GPU match");
  ok(OG.familyTarget("rtx 4090", fams) === null && OG.familyTarget("", fams) === null && OG.familyTarget("h100", null) === null, "non-family queries fall through");
  eq(OG.familyTarget("blackwell", fams), { kind: "family", slug: "blackwell" }, "variant_count counts");
  ok(OG.familyTarget("blackwell b200", fams).kind === "family-detail", "variants not loaded -> caller fetches the family");
  // stat-cell reasons
  ok(OG.shortReason("short") === "short" && OG.shortReason(null) === null && OG.shortReason("the index does not cover enough hours in this window") === "not enough history", "shortReason patterns");
  ok(OG.shortReason("alpha beta gamma delta epsilon zeta eta theta").length <= 32 && OG.shortReason("alpha beta gamma delta epsilon zeta eta theta").endsWith("…"), "shortReason truncates on a word");
  // chart helpers
  eq(C.isolated([null, 1, null, 2, 3, null, 4]), [1, 6], "isolated points (both neighbours missing)");
  eq(C.isolated([1, null]), [0], "isolated at the start");
  eq(C.thinLabels([{ x: 0, w: 20 }, { x: 15, w: 20 }, { x: 40, w: 20 }], 6), [true, false, true], "thinLabels drops colliding labels");
  ok(C.fitLabel("abcdefghijklmnop", 40, 6) === "abcde…" && C.fitLabel("short", 100, 6) === "short", "fitLabel");

  console.log(failures ? `\n${failures} FAILED` : "\nall passed");
  process.exit(failures ? 1 : 0);
})();
