/* OpenGrid Terminal client core. No build step: plain script, global `OG`.

PAGE-MODULE CONTRACT (wave-2 agents: edit only web/pages/<yours>.js)
--------------------------------------------------------------------
Each file in web/pages/ registers one or more pages; index.html already loads every file.

  OG.page("/gpu/:slug", {
    title: "GPU",                         // string, or (params, query) => string. Shown as "<title> · OpenGrid"
    nav: "gpus",                          // optional: which left-nav item to highlight (defaults by path prefix)
    mount(el, params, query, ctx) {       // el is empty; params from the pattern; query = OG.qs.all()
      el.append(OG.head("H100", "sub line", controls...));
      ctx.api("/v1/gpus/" + params.slug).then(d => ...);   // resolves only while this page is mounted
      ctx.every(30000, refresh);           // auto-refresh, cleared on navigation, paused while tab hidden
      ctx.onCleanup(() => ...);            // anything else to undo
      ctx.ssr                              // the server-rendered #ssr node for this path (first load only) or null
      return optionalCleanupFn;
    },
  });

Patterns: literal text plus ":name" params (match up to the next "/" non-greedily) and "*" (rest).
"/compare/:a-vs-:b" matches "/compare/h100-80gb-sxm5-vs-h200-141gb" -> {a:"h100-80gb-sxm5", b:"h200-141gb"}.
The most specific (most literal characters) matching pattern wins.

Navigation   OG.go(url, {replace})  ·  plain <a href="/x"> links are intercepted when a page matches
Query state  OG.qs.get(k, dflt) · OG.qs.all() · OG.qs.set({k: v|null}, {push}) (updates URL, no remount)
API          OG.api(path, {params, method, body, full, slot, nocache}) -> Promise<data>
               unwraps {data, meta}; {full:true} -> {data, meta}; throws OG.ApiError {status, message, path}
               identical concurrent GETs share one request; {slot:"x"}: only the newest request in a slot
               resolves, older ones reject with err.stale = true.
             OG.api.soft(path, opts) -> data | null on any error (use for endpoints that may not exist yet)
             ctx.api(...) same as OG.api but never settles after the page unmounts.
DOM          OG.h(tag, props, ...kids)  (props: class, style, on<event>, any attribute; kids: nodes/strings/arrays)
             OG.s(tag, attrs, ...kids)   (SVG)
Formatting   OG.fmt.price/pct/num/compact/age/time/date/dateTime/dir/cls, OG.chg(fraction) (▲/▼ span)
States       OG.loading(text) · OG.empty(text) · OG.error(err, retry) · OG.insufficient(reason, title)
             OG.na(reason) (inline "n/a" with the reason) · OG.value(v, fmtFn, reason) · OG.reasonOf(obj)
Badges       OG.kindBadge("observed"|"inferred"|"estimated"|"transaction") · OG.freshBadge(isoOrSeconds|label)
             OG.badge(text, tone)
Components   OG.head(title, sub, ...right) · OG.section(title, ...kids) · OG.stats(items) · OG.seg(options, value, onChange)
             OG.tabs(options, value, onChange) · OG.table(spec) · OG.csv(rows, columns, filename) · OG.logo(p, size)
             OG.gpuLink(nameOrSlug) · OG.providerLink(name) · OG.stub(el, {title, line, links})
Data         OG.slug(gpuName) (== api.common.gpu_slug) · OG.data.gpuName(slug) · OG.data.gpus() · OG.data.providers()
             OG.data.market(hours) (cached /market)
Commands     OG.command(/^g\s+(.+)$/i, (match, text) => ..., "g <gpu>  open a GPU market")
Status bar   OG.status.asOf(iso) · OG.status.live(ok, text)
Charts       OG.charts.* (web/charts.js)
Full docs: web/README.md.
*/
(function (root, factory) {
  const lib = root.Lib || (typeof require === "function" ? require("./lib.js") : null);
  const OG = factory(lib, root);
  if (typeof module === "object" && module.exports) module.exports = OG;
  else root.OG = OG;
})(typeof self !== "undefined" ? self : this, function (L, root) {
  const HAS_DOM = typeof document !== "undefined";
  const OG = { Lib: L, version: 1 };

  /* ================= pure: router ================= */
  function compile(pattern) {
    const keys = [];
    let re = "", literal = 0;
    for (const tok of pattern.split(/(:[A-Za-z_]\w*|\*)/)) {
      if (!tok) continue;
      if (tok === "*") { keys.push("rest"); re += "(.*)"; }
      else if (tok[0] === ":") { keys.push(tok.slice(1)); re += "([^/]+?)"; }
      else { re += tok.replace(/[.+?^${}()|[\]\\]/g, "\\$&"); literal += tok.length; }
    }
    const body = pattern === "/" ? "/" : re.replace(/\/$/, "") + "/?";       // "/x" and "/x/" both match
    return { pattern, keys, literal, re: new RegExp("^" + body + "$") };
  }
  function dec(s) { try { return decodeURIComponent(s); } catch (e) { return s; } }
  // routes: [{compiled, ...}] -> best {route, params} or null
  function matchRoute(routes, path) {
    let best = null;
    for (const r of routes) {
      const m = r.compiled.re.exec(path);
      if (!m) continue;
      if (best && best.route.compiled.literal >= r.compiled.literal) continue;
      const params = {};
      r.compiled.keys.forEach((k, i) => { params[k] = dec(m[i + 1]); });
      best = { route: r, params };
    }
    return best;
  }

  // Old hash URLs from the first web UI -> new paths
  function legacyHash(hash, slugify) {
    // Only the old app's "#/market..." form: a plain "#routing" is an in-page anchor, not a route.
    if (!/^#\//.test(String(hash || ""))) return null;
    const h = String(hash).replace(/^#\//, "");
    if (!h) return null;
    const parts = h.split("/");
    if (parts[0] === "market") return parts[1] ? "/gpu/" + (slugify || slug)(dec(parts.slice(1).join("/"))) : "/gpus";
    if (parts[0] === "routing") return "/route";
    if (parts[0] === "deploy") return "/deployments";
    return null;
  }

  /* ================= pure: query strings ================= */
  function qsParse(str) {
    const out = {};
    String(str || "").replace(/^\?/, "").split("&").forEach(kv => {
      if (!kv) return;
      const i = kv.indexOf("=");
      const k = dec((i < 0 ? kv : kv.slice(0, i)).replace(/\+/g, " "));
      out[k] = i < 0 ? "" : dec(kv.slice(i + 1).replace(/\+/g, " "));
    });
    return out;
  }
  function qsStringify(obj) {
    const parts = [];
    for (const [k, v] of Object.entries(obj || {})) {
      if (v == null || v === "" || v === false) continue;
      parts.push(encodeURIComponent(k) + "=" + encodeURIComponent(v === true ? "1" : String(v)).replace(/%2C/gi, ","));
    }
    return parts.length ? "?" + parts.join("&") : "";
  }

  /* ================= pure: slugs, formatting ================= */
  // Must equal api.common.gpu_slug: never change, links depend on it.
  function slug(name) {
    return String(name || "").replace(/^(NVIDIA|AMD Instinct|AMD|Intel)\s+/i, "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
  }
  const toNum = v => (v == null || v === "" ? null : typeof v === "number" ? v : Number(v));
  const fmt = {
    price: v => L.fmtPrice(toNum(v)),
    pct: v => L.fmtPct(toNum(v)),
    num(v, digits) {
      v = toNum(v);
      if (v == null || !isFinite(v)) return "–";
      const d = digits == null ? (Number.isInteger(v) ? 0 : 2) : digits;
      const [ip, fp] = Math.abs(v).toFixed(d).split(".");          // group the integer part only
      const grouped = ip.replace(/\B(?=(\d{3})+(?!\d))/g, ",") + (fp ? "." + fp : "");
      return (v < 0 ? L.MINUS : "") + grouped;
    },
    compact(v) {
      v = toNum(v);
      if (v == null || !isFinite(v)) return "–";
      const a = Math.abs(v), sign = v < 0 ? L.MINUS : "";
      for (const [n, u] of [[1e9, "B"], [1e6, "M"], [1e3, "k"]]) if (a >= n) return sign + (a / n).toFixed(a / n < 10 ? 1 : 0) + u;
      return sign + (Number.isInteger(a) ? a : a.toFixed(1));
    },
    // Seconds (number) or a time (ISO / Date) -> "now", "42s", "5m", "3h", "2d"
    age(x, now) {
      if (x == null) return "–";
      const sec = typeof x === "number" ? x : ((now != null ? +new Date(now) : Date.now()) - +new Date(x)) / 1000;
      if (!isFinite(sec)) return "–";
      const a = Math.max(0, sec);
      if (a < 5) return "now";
      if (a < 60) return Math.round(a) + "s";
      if (a < 3600) return Math.floor(a / 60) + "m";
      if (a < 86400 * 2) return Math.floor(a / 3600) + "h";
      return Math.floor(a / 86400) + "d";
    },
    time(iso) { if (!iso) return "–"; const d = new Date(iso); return isNaN(d) ? "–" : p2(d.getHours()) + ":" + p2(d.getMinutes()) + ":" + p2(d.getSeconds()); },
    date(iso) { if (!iso) return "–"; const d = new Date(iso); return isNaN(d) ? "–" : MONTHS[d.getMonth()] + " " + d.getDate(); },
    dateTime(iso) { if (!iso) return "–"; const d = new Date(iso); return isNaN(d) ? "–" : MONTHS[d.getMonth()] + " " + d.getDate() + " " + p2(d.getHours()) + ":" + p2(d.getMinutes()); },
    dir: v => L.direction(toNum(v)),
    // CSS class for a change. invert: true when a rise is bad news (unused by default: up is green, as elsewhere in the UI)
    cls: (v, invert) => { const d = L.direction(toNum(v)); return invert && (d === "up" || d === "down") ? (d === "up" ? "down" : "up") : d; },
  };
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  function p2(n) { return String(n).padStart(2, "0"); }

  // Freshness of an observation by age. With the provider's poll interval, thresholds are relative to it
  // (fresh <= 2 polls, aging <= 6 polls): an hourly feed seen 70 minutes ago is healthy, not "aging".
  // Without one, the same fixed thresholds everywhere (fresh <= 30 min, aging <= 3 h).
  const FRESH_S = 30 * 60, AGING_S = 3 * 3600;
  function freshLimits(intervalSeconds) {
    const iv = Number(intervalSeconds);
    return iv > 0 && isFinite(iv) ? [2 * iv, 6 * iv] : [FRESH_S, AGING_S];
  }
  function freshness(x, now, intervalSeconds) {
    if (x == null) return "unknown";
    if (x === "fresh" || x === "aging" || x === "stale" || x === "unknown") return x;
    const sec = typeof x === "number" ? x : ((now != null ? +new Date(now) : Date.now()) - +new Date(x)) / 1000;
    if (!isFinite(sec)) return "unknown";
    const [f, a] = freshLimits(intervalSeconds);
    return sec <= f ? "fresh" : sec <= a ? "aging" : "stale";
  }

  // Children for append / replaceChildren: nested arrays flattened; null, undefined and booleans dropped
  // (the DOM would print them as the text "null" / "false").
  function cleanKids(kids) {
    const out = [];
    for (const k of [].concat(kids == null ? [] : kids).flat(Infinity)) if (k != null && k !== false && k !== true) out.push(k);
    return out;
  }

  // A reason short enough for a stat cell; the caller keeps the full text for the tooltip.
  function shortReason(r, max) {
    if (r == null || r === "") return null;
    const s = String(r).trim(), n = max || 32;
    if (s.length <= n) return s;
    if (/does not cover|insufficient (history|coverage)|not enough (history|hours|data)|needs? ≥|fewer than/i.test(s)) return "not enough history";
    if (/not published|unpublished/i.test(s)) return "not published";
    if (/unavailable|not available/i.test(s)) return "unavailable";
    if (/one provider|single provider|only provider/i.test(s)) return "one provider only";
    const cut = s.slice(0, n - 1), sp = cut.lastIndexOf(" ");
    return (sp > n * 0.5 ? cut.slice(0, sp) : cut).replace(/[\s,;:.(–-]+$/, "") + "…";
  }

  // Family-aware "g <query>" routing. families: [{slug, id, name, variants: [{gpu, slug}] | variant_count}].
  // Returns {kind: "family", slug} when the query is exactly a family with more than one variant,
  // {kind: "variant", slug, family} when it is a family name plus words that pick one variant ("h100 sxm"),
  // {kind: "family-detail", family} when the family matched but its variant list is not loaded yet, else null
  // (the caller keeps the plain GPU fuzzy match).
  function familyTarget(q, families) {
    const raw = String(q || "").trim().toLowerCase().replace(/\s+/g, " ");
    if (!raw || !Array.isArray(families)) return null;
    const keys = f => [f.slug, f.id, f.name].filter(Boolean).map(norm);
    const nVar = f => (Array.isArray(f.variants) ? f.variants.length : Number(f.variant_count ?? f.variants_count ?? f.variants) || 0);
    const whole = norm(raw);
    const exact = families.find(f => keys(f).includes(whole));
    if (exact) return nVar(exact) > 1 ? { kind: "family", slug: exact.slug || exact.id } : null;
    // longest family key that prefixes the query on a word boundary
    const words = raw.split(" ");
    for (let n = words.length - 1; n >= 1; n--) {
      const head = norm(words.slice(0, n).join(" ")), rest = words.slice(n).join(" ");
      const fam = families.find(f => keys(f).includes(head));
      if (!fam) continue;
      if (!Array.isArray(fam.variants)) return { kind: "family-detail", family: fam, rest };
      const vs = fam.variants.map(v => ({ v, slug: v.slug || slug(v.gpu || v.name || ""), name: String(v.gpu || v.name || v.slug || "") }));
      // every word of the rest must match the variant; an ambiguous rest ("h100 80gb") opens the family page
      const score = x => { let s = 0; for (const w of rest.split(" ")) { const k = Math.max(fuzzyScore(w, x.slug), fuzzyScore(w, x.name)); if (!k) return 0; s += k; } return s; };
      const ranked = vs.map(x => [x, score(x)]).filter(x => x[1] > 0).sort((a, b) => b[1] - a[1]);
      if (ranked.length && (ranked.length === 1 || ranked[0][1] > ranked[1][1])) return { kind: "variant", slug: ranked[0][0].slug, family: fam.slug || fam.id };
      return { kind: "family", slug: fam.slug || fam.id };
    }
    return null;
  }

  /* ================= pure: sorting, CSV ================= */
  function colValue(col, row) { return col.value ? col.value(row) : row[col.key]; }
  // Nulls and blanks last in BOTH directions: a missing number is not the cheapest.
  function sortRows(rows, col, dir) {
    if (!col) return rows.slice();
    const sgn = dir === "desc" ? -1 : 1;
    return rows.map((r, i) => [r, colValue(col, r), i]).sort((a, b) => {
      const x = a[1], y = b[1];
      const xn = x == null || x === "" || (typeof x === "number" && isNaN(x)), yn = y == null || y === "" || (typeof y === "number" && isNaN(y));
      if (xn || yn) return xn && yn ? a[2] - b[2] : xn ? 1 : -1;
      let c;
      if (typeof x === "number" && typeof y === "number") c = x - y;
      else if (typeof x === "boolean" || typeof y === "boolean") c = (x ? 1 : 0) - (y ? 1 : 0);
      else c = String(x).localeCompare(String(y), undefined, { numeric: true, sensitivity: "base" });
      return c * sgn || a[2] - b[2];
    }).map(t => t[0]);
  }
  function csvCell(v) {
    if (v == null) return "";
    if (typeof v === "number") return isFinite(v) ? String(v) : "";
    if (typeof v === "boolean") return v ? "true" : "false";
    let s = v instanceof Date ? v.toISOString() : String(v);
    if (/^[=+\-@\t\r]/.test(s)) s = "'" + s;          // spreadsheet formula injection
    return /[",\r\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  }
  function csvString(rows, columns) {
    const cols = columns.filter(c => c.csv !== false);
    const head = cols.map(c => csvCell(c.csvLabel || c.label || c.key)).join(",");
    const body = rows.map(r => cols.map(c => csvCell(typeof c.csv === "function" ? c.csv(r) : colValue(c, r))).join(","));
    return [head, ...body].join("\r\n") + "\r\n";
  }

  /* ================= pure: commands, fuzzy ================= */
  const norm = s => String(s || "").toLowerCase().replace(/[^a-z0-9]+/g, "");
  // 0 = no match. Exact > prefix > word-prefix > substring > subsequence; `weight` breaks ties.
  function fuzzyScore(q, text) {
    const a = norm(q), b = norm(text);
    if (!a || !b) return 0;
    if (a === b) return 1000;
    if (b.startsWith(a)) return 800 - (b.length - a.length);
    const words = String(text).toLowerCase().split(/[^a-z0-9]+/).filter(Boolean);
    if (words.some(w => w.startsWith(a))) return 600 - (b.length - a.length);
    const i = b.indexOf(a);
    if (i >= 0) return 400 - i - (b.length - a.length) * 0.5;
    let j = 0;
    for (let k = 0; k < b.length && j < a.length; k++) if (b[k] === a[j]) j++;
    return j === a.length ? 100 - (b.length - a.length) * 0.5 : 0;
  }
  // items: any[]; texts(item) -> string | string[]; returns items sorted best-first (score > 0 only)
  function fuzzy(q, items, texts) {
    return items.map(it => {
      const t = texts ? texts(it) : it;
      const s = Math.max(...(Array.isArray(t) ? t : [t]).map(x => fuzzyScore(q, x)));
      return [it, s + (s > 0 && it && it.weight ? Math.min(50, it.weight) : 0)];
    }).filter(x => x[1] > 0).sort((a, b) => b[1] - a[1]).map(x => x[0]);
  }
  function parseCommand(text) {
    const t = String(text || "").trim().replace(/\s+/g, " ");
    if (!t) return { verb: "", args: [] };
    const [verb, ...args] = t.split(" ");
    return { verb: verb.toLowerCase(), args };
  }
  const COMMANDS = [];
  function command(re, handler, usage) { COMMANDS.push({ re, handler, usage: usage || String(re) }); }
  function findCommand(text) {
    const t = String(text || "").trim();
    for (const c of COMMANDS) { const m = c.re.exec(t); if (m) return { cmd: c, match: m }; }
    return null;
  }

  /* ================= pure: API envelope ================= */
  function unwrap(j) {
    if (j && typeof j === "object" && !Array.isArray(j) && "data" in j && j.meta && typeof j.meta === "object" &&
        Object.keys(j).every(k => k === "data" || k === "meta")) return { data: j.data, meta: j.meta };
    return { data: j, meta: null };
  }
  function withParams(path, params) {
    if (!params) return path;
    const q = qsStringify(params);
    return q ? path + (path.includes("?") ? "&" + q.slice(1) : q) : path;
  }
  function reasonOf(o) {
    if (!o || typeof o !== "object") return null;
    return o.reason || o.unavailable_reason || o.insufficient_reason || o.coverage_reason || null;
  }

  Object.assign(OG, { compile, matchRoute, legacyHash, slug, fmt, freshness, freshLimits, cleanKids, familyTarget, shortReason, sortRows, csvCell, csvString,
    fuzzy, fuzzyScore, parseCommand, command, findCommand, COMMANDS, unwrap, withParams, reasonOf,
    qs: { parse: qsParse, stringify: qsStringify } });

  /* ================= API ================= */
  class ApiError extends Error {
    constructor(status, message, path, body) { super(message); this.status = status; this.path = path; this.body = body; }
  }
  OG.ApiError = ApiError;
  const inflight = new Map(), slots = new Map();
  async function doFetch(url, method, opts) {
    const init = { method, credentials: "same-origin", headers: { accept: "application/json" } };
    if (opts.body !== undefined) { init.body = typeof opts.body === "string" ? opts.body : JSON.stringify(opts.body); init.headers["content-type"] = "application/json"; }
    if (opts.headers) Object.assign(init.headers, opts.headers);
    if (opts.signal) init.signal = opts.signal;
    let r;
    try { r = await fetch(url, init); } catch (e) { throw new ApiError(0, "network error", url); }
    const ct = (r.headers && r.headers.get && r.headers.get("content-type")) || "";
    let body = null;
    if (r.status !== 204) body = ct.includes("json") ? await r.json().catch(() => null) : await r.text().catch(() => null);
    if (!r.ok) {
      const d = body && typeof body === "object" ? body.detail : body;
      const msg = typeof d === "string" ? d : d && d.message ? d.message : Array.isArray(d) ? d.map(x => x.msg).join("; ") : `HTTP ${r.status}`;
      throw new ApiError(r.status, msg, url, body);
    }
    return typeof body === "object" ? unwrap(body) : { data: body, meta: null };
  }
  async function api(path, opts) {
    opts = opts || {};
    const url = withParams(path, opts.params), method = (opts.method || "GET").toUpperCase();
    let p;
    if (method === "GET" && !opts.nocache && inflight.has(url)) p = inflight.get(url);
    else {
      p = doFetch(url, method, opts);
      if (method === "GET") { inflight.set(url, p); const clear = () => { if (inflight.get(url) === p) inflight.delete(url); }; p.then(clear, clear); }
    }
    let token;
    if (opts.slot) { token = {}; slots.set(opts.slot, token); }
    let r;
    try { r = await p; }
    catch (e) { if (opts.slot && slots.get(opts.slot) !== token) { const s = new ApiError(0, "superseded", url); s.stale = true; throw s; } throw e; }
    if (opts.slot && slots.get(opts.slot) !== token) { const s = new ApiError(0, "superseded", url); s.stale = true; throw s; }
    return opts.full ? r : r.data;
  }
  api.soft = (path, opts) => api(path, opts).catch(() => null);
  api.full = (path, opts) => api(path, Object.assign({}, opts, { full: true }));
  OG.api = api;

  if (!HAS_DOM) return OG;

  /* ================= DOM helpers ================= */
  const SVGNS = "http://www.w3.org/2000/svg";
  function addKids(el, kids) {
    for (const kid of cleanKids(kids)) el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  // el.replaceChildren(a, null, [b, c]) / el.append(...) / el.prepend(...) anywhere in the app: drop null,
  // undefined and booleans and flatten arrays, instead of printing "null" / "false" / "[object HTMLElement]".
  // Patched once on the prototypes (pages call these hundreds of times; a wrapper at every call site rots).
  for (const P of [root.Element, root.DocumentFragment].filter(Boolean).map(C => C.prototype)) {
    for (const m of ["append", "prepend", "replaceChildren"]) {
      const orig = P[m];
      if (!orig || orig.__ogSafe) continue;
      const safe = function (...kids) {
        let clean = true;
        for (const k of kids) if (k == null || typeof k === "boolean" || Array.isArray(k)) { clean = false; break; }
        return orig.apply(this, clean ? kids : cleanKids(kids));
      };
      safe.__ogSafe = true;
      P[m] = safe;
    }
  }
  OG.fill = (el, ...kids) => { el.replaceChildren(...cleanKids(kids)); return el; };
  function h(tag, props, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v == null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
      else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
      else if (k === "value" && "value" in el) el.value = v;
      else el.setAttribute(k, v === true ? "" : v);
    }
    addKids(el, kids);
    return el;
  }
  function s(tag, attrs, ...kids) {
    const el = document.createElementNS(SVGNS, tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v);
    }
    for (const kid of kids.flat(Infinity)) if (kid != null && kid !== false) el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    return el;
  }
  OG.h = h; OG.s = s;
  const $ = sel => document.querySelector(sel);

  /* ================= boot data (embedded by api/pages.py) ================= */
  let BOOT = { gpus: [], providers: [], methodology: [] };
  try { const b = document.getElementById("og-boot"); if (b && b.textContent.trim().startsWith("{")) BOOT = Object.assign(BOOT, JSON.parse(b.textContent)); } catch (e) {}
  OG.boot = BOOT;
  const GPU_BY_SLUG = new Map((BOOT.gpus || []).map(([sl, name]) => [sl, name]));
  const PROVIDER_META = new Map((BOOT.providers || []).map(p => [p.name, p]));

  const providerName = p => (PROVIDER_META.get(p) || {}).display_name || L.providerName(p);
  OG.providerName = providerName;
  OG.providerMeta = p => PROVIDER_META.get(p) || null;
  OG.shortGpu = L.shortGpu;

  const marketCache = new Map();
  let familiesP = null;
  OG.data = {
    gpuName(sl) { return GPU_BY_SLUG.get(sl) || null; },
    // Every canonical GPU (from the server), weighted by how many providers sell it now (from /market).
    async gpus() {
      let live = null;
      const v1 = await api.soft("/v1/gpus", { params: { limit: 1000 } });
      const list = Array.isArray(v1) ? v1 : v1 && Array.isArray(v1.items) ? v1.items : null;
      if (list) live = list.map(g => ({ name: g.name || g.gpu || g.canonical_name, providers: g.providers || g.provider_count || 0 }));
      else { const m = await OG.data.market(24).catch(() => null); live = m ? m.gpus.map(g => ({ name: g.gpu, providers: g.providers })) : []; }
      const weight = new Map(live.filter(g => g.name).map(g => [g.name, typeof g.providers === "number" ? g.providers : Array.isArray(g.providers) ? g.providers.length : 0]));
      const names = new Set([...GPU_BY_SLUG.values(), ...weight.keys()]);
      return [...names].map(n => ({ slug: slug(n), name: n, short: L.shortGpu(n), vendor: L.vendorOf(n), weight: weight.get(n) || 0, live: weight.has(n) }));
    },
    providers() {
      const names = new Set([...PROVIDER_META.keys(), ...Object.keys(L.PROVIDER_NAMES)]);
      return [...names].map(n => Object.assign({ name: n, display_name: providerName(n) }, PROVIDER_META.get(n) || {}));
    },
    // GPU families (/v1/families): [{slug, id, name, kind, variants?|variant_count}] or [] when unavailable
    families() {
      if (!familiesP) familiesP = api.soft("/v1/families").then(r => (Array.isArray(r) ? r : r && Array.isArray(r.items) ? r.items : r && Array.isArray(r.families) ? r.families : []));
      return familiesP;
    },
    // /market overview, shared by every page for 20 s
    market(hours) {
      const k = String(hours == null ? 24 : hours), hit = marketCache.get(k);
      if (hit && Date.now() - hit.t < 20000) return hit.p;
      const p = api("/market", { params: { hours: k } });
      marketCache.set(k, { t: Date.now(), p });
      p.catch(() => marketCache.delete(k));
      return p;
    },
  };

  /* ================= small components ================= */
  const KNOWN_LOGOS = new Set(Object.keys(L.PROVIDER_NAMES));
  function monoLogo(p, size) { return h("span", { class: "mono-logo", style: `width:${size}px;height:${size}px;font-size:${Math.max(8, size * 0.5)}px` }, (providerName(p)[0] || "?").toUpperCase()); }
  function logo(p, size) {
    size = size || 14;
    if (!KNOWN_LOGOS.has(p)) return monoLogo(p, size);
    const img = h("img", { class: "logo", src: `/static/logos/${p}.png`, alt: "", width: size, height: size, loading: "lazy" });
    img.addEventListener("error", () => img.replaceWith(monoLogo(p, size)));
    return img;
  }
  OG.logo = logo;
  OG.gpuLink = (nameOrSlug, label) => {
    const name = GPU_BY_SLUG.get(nameOrSlug) || nameOrSlug;
    return h("a", { class: "lnk", href: "/gpu/" + slug(name), title: name }, label || L.shortGpu(name));
  };
  OG.providerLink = (p, opts) => h("a", { class: "lnk prov", href: "/provider/" + encodeURIComponent(p) },
    (opts && opts.logo === false) ? null : logo(p, 14), providerName(p));

  OG.chg = (v, opts) => {
    const d = fmt.cls(v, opts && opts.invert);
    if (d === "none") return h("span", { class: "chg none", title: (opts && opts.reason) || "not enough history" }, "–");
    const arrow = L.direction(toNum(v)) === "up" ? "▲" : L.direction(toNum(v)) === "down" ? "▼" : "";
    return h("span", { class: "chg " + d }, arrow ? arrow + " " : "", fmt.pct(v));
  };

  OG.loading = text => h("div", { class: "state loading", role: "status" }, h("i", { class: "spin" }), text || "Loading…");
  OG.empty = text => h("div", { class: "state empty" }, text || "Nothing to show.");
  OG.error = (err, retry) => h("div", { class: "state error", role: "alert" },
    h("b", {}, err && err.status === 404 ? "Not found" : err && err.status === 401 ? "Sign-in required" : err && err.status === 403 ? "Not allowed" : "Could not load"),
    h("span", {}, " " + ((err && err.message) || String(err || ""))),
    retry ? h("button", { class: "btn sm", onclick: retry }, "Retry") : null);
  // The API returns null + a reason when coverage is thin: show the reason, never a number.
  OG.insufficient = (reason, title) => h("div", { class: "state insufficient" },
    h("b", {}, title || "Insufficient data"), h("span", {}, " " + (reason || "not enough observed history to compute this honestly")));
  OG.na = reason => h("span", { class: "na", title: reason || "unavailable" }, "n/a");
  OG.value = (v, f, reason) => (v == null || (typeof v === "number" && !isFinite(v)) ? OG.na(reason) : (f || fmt.num)(v));
  OG.reasonOf = reasonOf;

  const KIND_HELP = {
    observed: "Read directly from a provider and normalized",
    inferred: "Derived from observed data by a stated rule",
    estimated: "A model or assumption fills a gap",
    transaction: "What an OpenGrid execution actually quoted or paid",
  };
  OG.badge = (text, tone, title) => h("span", { class: "badge " + (tone || ""), title: title || null }, text);
  OG.kindBadge = kind => h("a", { class: "badge kind k-" + kind, href: "/methodology/data-kinds", title: KIND_HELP[kind] || kind }, kind);
  // Poll interval per provider (seconds), from /polling, loaded at start: freshBadge(..., {provider}) uses it.
  const INTERVALS = new Map();
  OG.data_intervals = INTERVALS;
  OG.pollInterval = p => INTERVALS.get(p) || null;
  // OG.freshBadge(isoOrSeconds | label, {intervalSeconds, provider, now}) — thresholds relative to the
  // provider's poll interval when known (fresh <= 2x, aging <= 6x). A second argument that is not an
  // object is the legacy `now`.
  OG.freshBadge = (x, opts) => {
    const o = opts && typeof opts === "object" && !(opts instanceof Date) ? opts : { now: opts };
    const iv = o.intervalSeconds || (o.provider ? INTERVALS.get(o.provider) : null) || null;
    const f = freshness(x, o.now, iv);
    const age = typeof x === "number" || (x && !["fresh", "aging", "stale", "unknown"].includes(x)) ? fmt.age(x, o.now) : null;
    const basis = iv ? ` (polled every ${fmt.age(iv)}: fresh ≤ ${fmt.age(2 * iv)}, aging ≤ ${fmt.age(6 * iv)})` : "";
    return h("span", { class: "badge fresh f-" + f, title: f === "unknown" ? "freshness unknown" : age ? `${f}: last observed ${age === "now" ? "just now" : age + " ago"}${basis}` : f + basis }, h("i"), age || f);
  };

  /* ---- shared: confirmation dialog, price-concept badge, money, drawer, copy button ---- */
  // OG.dialog({title, body, confirm, cancel, danger, ack}) -> Promise<boolean>. `ack`: a checkbox the user
  // must tick before Confirm enables (for anything that can spend money or destroy something).
  OG.dialog = o => new Promise(resolve => {
    const prev = document.activeElement;
    const ackBox = o.ack ? h("input", { type: "checkbox" }) : null;
    const ok = h("button", { class: "btn " + (o.danger ? "w4-danger" : "pri"), type: "button", disabled: o.ack ? true : null, onclick: () => close(true) }, o.confirm || "Confirm");
    if (ackBox) ackBox.addEventListener("change", () => { ok.disabled = !ackBox.checked; });
    const box = h("div", { class: "w4-dlg", role: "dialog", "aria-modal": "true", "aria-label": o.title },
      h("div", { class: "w4-dlg-h" }, o.title),
      h("div", { class: "w4-dlg-b" }, o.body),
      ackBox ? h("label", { class: "w4-ack" }, ackBox, h("span", {}, o.ack)) : null,
      h("div", { class: "w4-dlg-f" }, h("button", { class: "btn", type: "button", onclick: () => close(false) }, o.cancel || "Cancel"), ok));
    const ov = h("div", { class: "w4-ov", onmousedown: e => { if (e.target === ov) close(false); } }, box);
    const key = e => { if (e.key === "Escape") { e.preventDefault(); e.stopImmediatePropagation(); close(false); } };
    let done = false;
    function close(v) {
      if (done) return; done = true;
      document.removeEventListener("keydown", key, true); ov.remove();
      if (prev && prev.focus) prev.focus();
      resolve(v);
    }
    document.addEventListener("keydown", key, true);
    document.body.append(ov);
    (ackBox || ok).focus();
  });
  // Price-concept badge: the four concepts are never mixed (methodology/data-kinds).
  const CONCEPT = {
    list: ["list", "List price: the provider's published catalogue price"],
    observed: ["observed", "Observed market price: OpenGrid's normalized reading of a live listing"],
    quote: ["quote", "Quote: the price this route would transact at, from the observed listing (preview) or the provider API (route)"],
    execution: ["execution", "Execution price: what the provider actually charged a real deployment"],
  };
  OG.conceptBadge = c => h("a", { class: "badge w4-pc pc-" + c, href: "/methodology/data-kinds", title: (CONCEPT[c] || [c, c])[1] }, (CONCEPT[c] || [c])[0]);
  OG.money = v => (v == null || !isFinite(v) ? "–" : (v < 0 ? "−$" : "$") + fmt.num(Math.abs(v), 2));

  // OG.drawer({label, onClose}) -> {el, head(...nodes), body(...nodes), append(node), close(), open}
  // A right-hand panel over the page. Close button, Esc (unless a dialog is open) and close() all call
  // onClose once. One drawer per call; call close() from ctx.onCleanup.
  OG.drawer = o => {
    o = o || {};
    const headEl = h("div", { class: "og-dh" }), bodyEl = h("div", { class: "og-db" });
    const el = h("aside", { class: "og-drawer " + (o.cls || ""), role: "dialog", "aria-label": o.label || "Details" }, headEl, bodyEl);
    const closeBtn = h("button", { class: "btn sm", type: "button", onclick: () => d.close() }, "Close ✕");
    const esc = e => { if (e.key === "Escape" && !document.querySelector(".w4-ov")) { e.stopImmediatePropagation(); d.close(); } };
    const d = {
      el, open: true,
      head(...nodes) { headEl.replaceChildren(...cleanKids(nodes), h("span", { class: "spacer" }), closeBtn); return d; },
      body(...nodes) { bodyEl.replaceChildren(...cleanKids(nodes)); return d; },
      append(...nodes) { bodyEl.append(...cleanKids(nodes)); return d; },
      close(silent) {
        if (!d.open) return; d.open = false;
        document.removeEventListener("keydown", esc); el.remove();
        if (!silent && o.onClose) o.onClose();
      },
    };
    d.head(o.title ? h("b", {}, o.title) : null);
    document.addEventListener("keydown", esc);
    document.body.append(el);
    return d;
  };

  // OG.copy(text, {label, cls, title}) -> a button that copies `text` (or text()) to the clipboard
  async function copyText(text) {
    try { await navigator.clipboard.writeText(text); return true; }
    catch (e) {
      const t = h("textarea", { style: "position:fixed;left:-999px" }); t.value = text; document.body.append(t); t.select();
      let ok = false; try { ok = document.execCommand("copy"); } catch (e2) {} t.remove(); return ok;
    }
  }
  OG.copyText = copyText;
  OG.copy = (text, o) => {
    o = o || {};
    const label = o.label || "Copy";
    const b = h("button", { class: "btn og-copy " + (o.cls || "sm"), type: "button", title: o.title || "Copy to clipboard" }, label);
    b.addEventListener("click", async e => {
      e.preventDefault(); e.stopPropagation();
      const ok = await copyText(typeof text === "function" ? text() : String(text));
      b.textContent = ok ? "Copied" : "Copy failed"; b.classList.toggle("done", ok);
      setTimeout(() => { b.textContent = label; b.classList.remove("done"); }, 1400);
    });
    return b;
  };

  OG.head = (title, sub, ...right) => h("div", { class: "phead" },
    h("div", { class: "phead-l" }, h("h1", {}, title), sub ? h("div", { class: "sub" }, sub) : null),
    right.length ? h("div", { class: "phead-r" }, right) : null);
  OG.section = (title, ...kids) => h("section", { class: "sec" }, title ? h("h2", { class: "sec-h" }, title) : null, kids);

  // Stat strip: compact label / value / change rows. item: {label, value, sub, change, invert, reason, kind, title}
  // `sub` is a short line under the value; `title` the full text on hover. A long reason never widens the
  // cell: the line shows a short form and the full reason moves to the tooltip.
  OG.shortReason = shortReason;
  OG.stats = items => h("div", { class: "stats" }, items.filter(Boolean).map(it => {
    const missing = it.value == null || it.value === "";
    const line = it.sub != null && it.sub !== "" ? it.sub : missing && it.reason ? shortReason(it.reason) : null;
    const full = [it.title, missing && it.reason && it.reason !== line ? it.reason : null, typeof line === "string" && line.length > 30 ? line : null].filter(Boolean);
    return h("div", { class: "stat", title: full.length ? [...new Set(full)].join(" — ") : null },
      h("div", { class: "stat-l" }, it.label, it.kind ? OG.kindBadge(it.kind) : null),
      h("div", { class: "stat-v" }, missing ? OG.na(it.reason) : it.value, it.change !== undefined ? OG.chg(it.change, { invert: it.invert }) : null),
      line != null ? h("div", { class: "stat-s" }, line) : null);
  }));

  // Segmented control. options: [[value, label, title?]] or [{value, label, title}]
  function seg(options, value, onChange, cls) {
    const opts = options.map(o => Array.isArray(o) ? { value: o[0], label: o[1], title: o[2] } : o);
    const el = h("div", { class: cls || "seg", role: "group" });
    const draw = () => el.replaceChildren(...opts.map(o => h("button", { type: "button", "aria-pressed": String(String(o.value) === String(value)), title: o.title || null,
      onclick: () => { if (String(o.value) === String(value)) return; value = o.value; draw(); onChange && onChange(o.value); } }, o.label)));
    draw();
    el.set = v => { value = v; draw(); };
    return el;
  }
  OG.seg = seg;
  OG.tabs = (options, value, onChange) => seg(options, value, onChange, "tabs");

  OG.stub = (el, o) => {
    el.append(OG.head(o.title, null), h("p", { class: "stub" }, h("span", { class: "badge" }, "coming online"), " ", o.line || ""),
      o.links && o.links.length ? h("p", { class: "stub-links" }, o.links.map(([href, label]) => h("a", { class: "lnk", href }, label + " →"))) : null);
  };

  /* ================= table ================= */
  /* spec: {columns, rows, sort:{key,dir}, onSort(sort), onRow(row, ev), rowHref(row), rowKey(row), rowClass(row),
           empty, csv: filename|false, limit (default 500), title, toolbar: [nodes], onHover(row|null)}
     column: {key, label, num, fmt(v,row) -> string|Node, value(row) (sort/CSV), href(row), sort:false,
              title, cls, width, csv:false|fn, desc (first click sorts descending)}
     returns element with .update(rows), .rows(), .sorted() */
  function table(spec) {
    let rows = spec.rows || [], sort = spec.sort || null, limit = spec.limit || 500;
    const cols = spec.columns.filter(c => !c.hidden);
    // stickyLast: n -> the last n columns stay visible when the table scrolls sideways (narrow screens)
    const nStick = Math.max(0, Math.min(cols.length - 1, spec.stickyLast | 0));
    const stickCls = i => (nStick && i >= cols.length - nStick ? " stk" + (i === cols.length - nStick ? " stk-first" : "") : "");
    const wrap = h("div", { class: "tbl" + (nStick ? " tbl-stick" : "") });
    const bar = h("div", { class: "tbl-bar" });
    const scroll = h("div", { class: "tbl-scroll" });
    const tbl = h("table", { class: "grid-t" + (spec.compact ? " compact" : "") });
    const thead = h("thead"), tbody = h("tbody"), more = h("div", { class: "tbl-more" });
    tbl.append(thead, tbody); scroll.append(tbl);
    if (spec.title || spec.csv || spec.toolbar) wrap.append(bar);
    wrap.append(scroll, more);
    const colOf = k => cols.find(c => c.key === k);
    const sorted = () => (sort && colOf(sort.key) ? sortRows(rows, colOf(sort.key), sort.dir) : rows.slice());
    function drawHead() {
      thead.replaceChildren(h("tr", {}, cols.map(c => {
        const on = sort && sort.key === c.key, can = c.sort !== false;
        return h("th", { class: (c.num ? "n " : "") + (can ? "srt " : "") + (on ? "on " + sort.dir : "") + stickCls(cols.indexOf(c)), style: c.width ? `width:${c.width}` : null,
          title: c.title || null, scope: "col", tabindex: can ? "0" : null, "aria-sort": on ? (sort.dir === "desc" ? "descending" : "ascending") : null,
          onclick: can ? () => clickSort(c) : null, onkeydown: can ? e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); clickSort(c); } } : null },
        c.label, on ? h("i", { class: "car" }, sort.dir === "desc" ? "▼" : "▲") : null);
      })));
    }
    function clickSort(c) {
      sort = sort && sort.key === c.key ? { key: c.key, dir: sort.dir === "asc" ? "desc" : "asc" } : { key: c.key, dir: c.desc ? "desc" : "asc" };
      drawHead(); drawBody(); spec.onSort && spec.onSort(sort);
    }
    function cell(c, r, i) {
      const v = colValue(c, r);
      let out = c.fmt ? c.fmt(c.key in r ? r[c.key] : v, r) : v == null || v === "" ? "–" : c.num && typeof v === "number" ? fmt.num(v) : String(v);
      if (c.href) { const href = c.href(r); if (href) out = h("a", { class: "lnk", href }, out); }
      return h("td", { class: (c.num ? "n " : "") + (c.cls ? (typeof c.cls === "function" ? c.cls(r) || "" : c.cls) : "") + stickCls(i) }, out);
    }
    function drawBody() {
      const list = sorted();
      if (!list.length) { tbody.replaceChildren(h("tr", {}, h("td", { colspan: cols.length, class: "tbl-empty" }, spec.empty || "No rows."))); more.textContent = ""; return; }
      const shown = list.slice(0, limit);
      tbody.replaceChildren(...shown.map(r => {
        const href = spec.rowHref ? spec.rowHref(r) : null, act = href || spec.onRow;
        const tr = h("tr", { class: (act ? "act " : "") + (spec.rowClass ? spec.rowClass(r) || "" : ""), tabindex: act ? "0" : null, "data-k": spec.rowKey ? spec.rowKey(r) : null },
          cols.map((c, i) => cell(c, r, i)));
        if (act) {
          tr.addEventListener("click", e => { if (e.target.closest("a,button,input,select")) return; if (href) (e.metaKey || e.ctrlKey ? window.open(href) : OG.go(href)); else spec.onRow(r, e); });
          tr.addEventListener("keydown", e => { if (e.key === "Enter" && e.target === tr) { if (href) OG.go(href); else spec.onRow(r, e); } });
        }
        if (spec.onHover) { tr.addEventListener("mouseenter", () => spec.onHover(r)); tr.addEventListener("mouseleave", () => spec.onHover(null)); }
        return tr;
      }));
      more.replaceChildren(list.length > limit ? h("button", { class: "btn sm", onclick: () => { limit += spec.limit || 500; drawBody(); } }, `Show more (${list.length - limit} hidden)`) : "");
      if (nStick) requestAnimationFrame(placeSticky);
    }
    function drawBar() {
      bar.replaceChildren(...[spec.title ? h("span", { class: "tbl-title" }, spec.title) : null, h("span", { class: "spacer" }), ...(spec.toolbar || []),
        spec.csv ? h("button", { class: "btn sm", title: "Download these rows as CSV", onclick: () => OG.csv(sorted(), spec.columns, typeof spec.csv === "string" ? spec.csv : "opengrid.csv") }, "CSV") : null].filter(Boolean));
    }
    // right offset of each sticky column = total width of the sticky columns to its right
    function placeSticky() {
      if (!nStick) return;
      const ths = thead.querySelectorAll("th"), trs = tbody.querySelectorAll("tr");
      let right = 0;
      for (let i = cols.length - 1; i >= cols.length - nStick; i--) {
        const off = right + "px";
        if (ths[i]) ths[i].style.right = off;
        trs.forEach(tr => { const td = tr.children[i]; if (td && td.classList.contains("stk")) td.style.right = off; });
        right += ths[i] ? ths[i].getBoundingClientRect().width : 0;
      }
    }
    if (nStick && window.ResizeObserver) new ResizeObserver(() => placeSticky()).observe(tbl);
    drawBar(); drawHead(); drawBody();
    wrap.update = r => { rows = r || []; drawBody(); return wrap; };
    wrap.rows = () => rows;
    wrap.sorted = sorted;
    wrap.setSort = s2 => { sort = s2; drawHead(); drawBody(); };
    wrap.highlight = key => tbody.querySelectorAll("tr[data-k]").forEach(tr => tr.classList.toggle("hl", key != null && tr.dataset.k === String(key)));
    return wrap;
  }
  OG.table = table;
  OG.csv = (rows, columns, filename) => {
    const blob = new Blob(["﻿" + csvString(rows, columns)], { type: "text/csv;charset=utf-8" });
    const a = h("a", { href: URL.createObjectURL(blob), download: filename || "opengrid.csv" });
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  };

  /* ================= auto refresh ================= */
  OG.every = (ms, fn) => {
    const id = setInterval(() => { if (!document.hidden) fn(); }, ms);
    return () => clearInterval(id);
  };

  /* ================= router ================= */
  const ROUTES = [];
  OG.routes = ROUTES;
  OG.page = (pattern, def) => { ROUTES.push(Object.assign({ pattern, compiled: compile(pattern) }, def)); };
  OG.match = path => matchRoute(ROUTES, path);
  let current = null, firstMount = true;

  OG.qs.all = () => qsParse(location.search);
  OG.qs.get = (k, d) => { const v = qsParse(location.search)[k]; return v == null || v === "" ? (d === undefined ? null : d) : v; };
  OG.qs.set = (patch, opts) => {
    const q = Object.assign(qsParse(location.search), patch);
    const url = location.pathname + qsStringify(q) + location.hash;
    if (url === location.pathname + location.search + location.hash) return;
    history[opts && opts.push ? "pushState" : "replaceState"](history.state, "", url);
  };

  OG.go = (url, opts) => {
    const u = new URL(url, location.href);
    if (u.origin !== location.origin || !OG.match(u.pathname)) { location.href = u.href; return; }
    history[opts && opts.replace ? "replaceState" : "pushState"](null, "", u.pathname + u.search + u.hash);
    render();
  };

  function navKeyFor(path) {
    const seg0 = path.split("/")[1] || "";
    return { "": "overview", gpu: "gpus", gpus: "gpus", provider: "providers", indices: "indices", compare: "compare", methodology: "methodology" }[seg0] ?? seg0;
  }
  function render() {
    const path = location.pathname;
    if (current) { current.ctx._dead = true; for (const f of current.ctx._cleanups.splice(0)) { try { f(); } catch (e) { console.error(e); } } }
    const m = OG.match(path);
    const page = $("#page");
    if (!page) return;
    const ssrNode = firstMount ? document.getElementById("ssr") : null;
    const ssr = ssrNode && ssrNode.dataset.path === path && ssrNode.innerHTML.trim() ? ssrNode : null;
    if (ssrNode) ssrNode.remove();
    firstMount = false;
    page.textContent = "";
    page.className = "page";   // pages may add their own class to the container; never let it leak
    page.scrollTop = 0;
    const main = $(".main"); if (main) main.scrollTop = 0;
    const ctx = {
      path, params: m ? m.params : {}, query: OG.qs.all(), ssr, _dead: false, _cleanups: [],
      alive() { return !ctx._dead; },
      onCleanup(f) { if (ctx._dead) f(); else ctx._cleanups.push(f); },   // registered after unmount: undo at once
      every(ms, fn) { if (!ctx._dead) ctx._cleanups.push(OG.every(ms, fn)); },
      api(p, o) { return api(p, o).then(d => ctx._dead ? new Promise(() => {}) : d, e => ctx._dead ? new Promise(() => {}) : Promise.reject(e)); },
      setTitle(t) { document.title = t ? t + " · OpenGrid" : "OpenGrid Terminal"; },
    };
    current = { ctx, route: m && m.route };
    const nav = m ? (m.route.nav || navKeyFor(path)) : null;
    document.querySelectorAll(".nav a[data-nav]").forEach(a => { if (a.dataset.nav === nav) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current"); });
    if (!m) { ctx.setTitle("Not found"); page.append(OG.head("Not found", path), OG.empty("No page at this address.")); return; }
    const t = typeof m.route.title === "function" ? m.route.title(m.params, ctx.query) : m.route.title;
    ctx.setTitle(t);
    try {
      const ret = m.route.mount(page, m.params, ctx.query, ctx);
      if (typeof ret === "function") ctx._cleanups.push(ret);
      else if (ret && typeof ret.then === "function") ret.catch(e => { console.error(e); if (!ctx._dead) page.append(OG.error(e)); });
    } catch (e) { console.error(e); page.append(OG.error(e)); }
  }
  OG.render = render;

  // Fetch the server-rendered summary block of a page path (for SPA navigation to SSR-heavy pages)
  OG.fetchSSR = async path => {
    const r = await fetch(path, { credentials: "same-origin", headers: { accept: "text/html" } });
    if (!r.ok) throw new ApiError(r.status, r.status === 404 ? "not found" : "HTTP " + r.status, path);
    const doc = new DOMParser().parseFromString(await r.text(), "text/html");
    const n = doc.getElementById("ssr");
    return n ? document.importNode(n, true) : null;
  };

  /* ================= status bar ================= */
  const statusState = { asOf: null, ok: null, text: "connecting" };
  function drawStatus() {
    const dot = $("#live-dot"), txt = $("#live-text"), asof = $("#asof");
    if (!dot) return;
    const age = statusState.asOf ? (Date.now() - new Date(statusState.asOf)) / 1000 : null;
    const stale = statusState.ok === false || (age != null && age > 600);
    dot.className = "dot " + (statusState.ok == null ? "" : stale ? "stale" : "live");
    txt.textContent = statusState.ok === false ? statusState.text : age != null && age > 600 ? "stale" : statusState.ok ? "live" : statusState.text;
    asof.textContent = statusState.asOf ? fmt.time(statusState.asOf) : "–";
    asof.title = statusState.asOf ? "data as of " + new Date(statusState.asOf).toString() : "";
  }
  OG.status = {
    asOf(iso) { if (iso) { statusState.asOf = iso; statusState.ok = true; } drawStatus(); },
    live(ok, text) { statusState.ok = ok; statusState.text = text || (ok ? "live" : "offline"); drawStatus(); },
    tape(items) { const el = $("#tape"); if (el) el.replaceChildren(...items); },
  };
  let pollingP = null;
  // Poll intervals per provider: resolves to the Map used by freshBadge({provider}). Never rejects.
  OG.data.intervals = () => {
    if (!pollingP) pollingP = api.soft("/polling").then(p => {
      for (const x of (p && p.providers) || []) if (x.provider && x.interval_seconds) INTERVALS.set(x.provider, x.interval_seconds);
      return p;
    });
    return pollingP.then(() => INTERVALS);
  };
  async function loadPolling() {
    await OG.data.intervals();
    const p = await pollingP;
    const el = $("#polled");
    if (el && p && p.providers) {
      const mins = Math.round(Math.min(...p.providers.map(x => x.interval_seconds)) / 60);
      el.textContent = `${p.providers.length} providers`;
      el.title = `${p.providers.length} providers polled, fastest every ${mins}m${p.running ? "" : " (poller not running on this server)"}`;
    }
  }
  // Tape: GPU prices from /market (lowest on-demand $/GPU-hr), then the latest notable moves from
  // /v1/tape (events: title, pct, value). Real prices only; the two are never mixed in one item.
  async function loadTape() {
    const [m, v1] = await Promise.all([OG.data.market(24).catch(() => null), api.soft("/v1/tape")]);
    if (!m) { OG.status.live(false, "api unreachable"); return; }
    OG.status.asOf(m.t1);
    const top = m.gpus.filter(g => g.providers >= 2).slice(0, 16);
    const prices = top.map(g => h("a", { class: "tk", href: "/gpu/" + slug(g.gpu), title: `${g.gpu}: lowest on-demand $/GPU-hr across ${g.providers} providers` },
      h("b", {}, L.shortGpu(g.gpu)), " ", fmt.price(g.lowest), " ", OG.chg(g.change_pct)));
    const events = (Array.isArray(v1) ? v1 : []).slice(0, 12).map(e => h("a", {
      class: "tk tk-ev", href: e.gpu_slug ? "/gpu/" + e.gpu_slug : "/events", title: e.title || "",
    }, h("b", {}, e.provider ? L.providerName(e.provider) : "Market"), " ", e.gpu ? L.shortGpu(e.gpu) : (e.type || ""), " ", OG.chg(e.pct)));
    OG.status.tape([...prices, ...events]);
  }

  /* ================= command bar ================= */
  function navItems() { return [...document.querySelectorAll(".nav a[data-nav]")].map(a => ({ label: a.textContent.trim(), href: a.getAttribute("href") })); }
  let gpuList = null;
  async function gpusForSearch() { if (!gpuList) gpuList = await OG.data.gpus().catch(() => []); return gpuList; }
  OG.resolveGpu = async q => {
    const list = await gpusForSearch();
    const exact = list.find(g => g.slug === slug(q) || g.slug === String(q).toLowerCase());
    return exact || fuzzy(q, list, g => [g.slug, g.short, g.name])[0] || null;
  };
  OG.resolveProvider = q => fuzzy(q, OG.data.providers(), p => [p.name, p.display_name])[0] || null;

  function builtinCommands() {
    const go = OG.go;
    command(/^g\s+(.+)$/i, async m => {
      const q = m[1].trim();
      // an exact canonical slug always wins; then families ("h100" -> family page, "h100 sxm" -> the SXM5 variant)
      const exactGpu = (await gpusForSearch()).find(g => g.slug === slug(q) || g.slug === q.toLowerCase());
      if (!exactGpu) {
        const fams = await OG.data.families();
        let t = OG.familyTarget(q, fams);
        if (t && t.kind === "family-detail") {
          const d = await api.soft("/v1/families/" + encodeURIComponent(t.family.slug || t.family.id));
          t = d && Array.isArray(d.variants) ? OG.familyTarget(q, [Object.assign({}, t.family, { variants: d.variants })]) : null;
        }
        if (t && (t.kind === "family" || t.kind === "variant")) { go("/gpu/" + t.slug); return; }
      }
      const g = exactGpu || await OG.resolveGpu(q);
      if (g) go("/gpu/" + g.slug); else go("/gpus?q=" + encodeURIComponent(q));
    }, "g <gpu>          open a GPU market (g h100 = family, g h100 sxm = variant)");
    command(/^p\s+(.+)$/i, m => { const p = OG.resolveProvider(m[1]); go(p ? "/provider/" + p.name : "/providers"); }, "p <provider>     open a provider (p lambda)");
    command(/^c\s+(\S+)\s+(\S+)$/i, async m => { const [a, b] = await Promise.all([OG.resolveGpu(m[1]), OG.resolveGpu(m[2])]); if (a && b) go(`/compare/${a.slug}-vs-${b.slug}`); else go(`/compare?a=${encodeURIComponent(m[1])}&b=${encodeURIComponent(m[2])}`); }, "c <gpu> <gpu>    compare two GPUs (c h100 h200)");
    command(/^r\s+(\S+)(?:\s+(\d+))?(?:\s+(\S+))?$/i, async m => { const g = await OG.resolveGpu(m[1]); go("/route" + qsStringify({ gpu: g ? g.slug : m[1], count: m[2], region: m[3] })); }, "r <gpu> [n] [region]  route preview (r h100 8 us)");
    const single = { e: ["/events", "events"], n: ["/news", "news"], o: ["/opportunities", "opportunities"], x: ["/explorer", "explorer"], i: ["/indices", "indices"], h: ["/heatmaps", "heatmaps"], m: ["/methodology", "methodology"], d: ["/deployments", "deployments"] };
    for (const [k, [href, label]] of Object.entries(single)) command(new RegExp("^" + k + "$", "i"), () => go(href), `${k.padEnd(16)} ${label}`);
    command(/^\?$|^help$/i, () => showHelp(), "?                this help");
  }

  async function suggestions(text) {
    const t = text.trim();
    if (!t) return COMMANDS.map(c => ({ label: c.usage, hint: "command", run: null }));
    const out = [];
    const hit = findCommand(t);
    if (hit) out.push({ label: "↵ " + t, hint: hit.cmd.usage.split(/\s{2,}/)[1] || "command", run: () => hit.cmd.handler(hit.match, t) });
    const q = t.replace(/^[gpc]\s+/i, "");
    const [gpus, provs, fams] = [await gpusForSearch(), OG.data.providers(), await OG.data.families()];
    for (const f of fuzzy(q, fams, f => [f.slug, f.id, f.name].filter(Boolean)).slice(0, 2)) {
      const n = Array.isArray(f.variants) ? f.variants.length : Number(f.variant_count ?? f.variants_count) || null;
      out.push({ label: f.name || f.slug, hint: "GPU family" + (n ? ` · ${n} variant${n === 1 ? "" : "s"}` : ""), run: () => OG.go("/gpu/" + (f.slug || f.id)) });
    }
    for (const g of fuzzy(q, gpus, g => [g.slug, g.short, g.name]).slice(0, 6)) out.push({ label: g.short, hint: g.live ? `GPU · ${g.weight} provider${g.weight === 1 ? "" : "s"}` : "GPU · none live", run: () => OG.go("/gpu/" + g.slug) });
    for (const p of fuzzy(q, provs, p => [p.name, p.display_name]).slice(0, 4)) out.push({ label: p.display_name, hint: "provider", run: () => OG.go("/provider/" + p.name) });
    for (const n of fuzzy(q, navItems(), n => n.label).slice(0, 3)) out.push({ label: n.label, hint: "page", run: () => OG.go(n.href) });
    return out.slice(0, 12);
  }
  function setupCommandBar() {
    const input = $("#cmd"), box = $("#cmd-sug");
    if (!input) return;
    let items = [], sel = 0, seq = 0;
    const close = () => { box.hidden = true; items = []; };
    const draw = () => {
      box.replaceChildren(...items.map((it, i) => h("div", { class: "sug" + (i === sel ? " on" : ""), role: "option", onmousedown: e => { e.preventDefault(); exec(i); } },
        h("span", { class: "sug-l" }, it.label), h("span", { class: "sug-h" }, it.hint))));
      box.hidden = !items.length;
    };
    const refresh = async () => { const my = ++seq; const r = await suggestions(input.value); if (my !== seq) return; items = r; sel = 0; draw(); };
    async function exec(i) {
      const it = items[i];
      if (it && it.run) { input.value = ""; close(); input.blur(); await it.run(); return; }
      const t = input.value.trim(), hit = findCommand(t);
      if (hit) { input.value = ""; close(); input.blur(); await hit.cmd.handler(hit.match, t); }
    }
    input.addEventListener("input", refresh);
    input.addEventListener("focus", refresh);
    input.addEventListener("blur", () => setTimeout(close, 100));
    input.addEventListener("keydown", e => {
      if (e.key === "ArrowDown") { e.preventDefault(); sel = Math.min(items.length - 1, sel + 1); draw(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); sel = Math.max(0, sel - 1); draw(); }
      else if (e.key === "Enter") { e.preventDefault(); exec(items.length ? sel : -1); }
      else if (e.key === "Escape") { input.value = ""; close(); input.blur(); }
    });
    document.addEventListener("keydown", e => {
      const tag = (e.target && e.target.tagName) || "", typing = /INPUT|TEXTAREA|SELECT/.test(tag) || (e.target && e.target.isContentEditable);
      if (typing || e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === "/") { e.preventDefault(); input.focus(); input.select(); }
      else if (e.key === "?") { e.preventDefault(); showHelp(); }
      else if (e.key === "Escape") { const hp = $("#help"); if (hp && !hp.hidden) { hp.hidden = true; e.stopImmediatePropagation(); } }
    });
  }
  function showHelp() {
    let hp = $("#help");
    if (!hp) { hp = h("div", { id: "help", class: "help", role: "dialog", "aria-label": "Keyboard commands", onclick: e => { if (e.target === hp) hp.hidden = true; } }); document.body.append(hp); }
    hp.replaceChildren(h("div", { class: "help-box" },
      h("div", { class: "help-h" }, h("b", {}, "Commands"), h("span", { class: "dim" }, "press / to type · Esc to close")),
      h("pre", { class: "help-pre" }, COMMANDS.map(c => c.usage).join("\n")),
      h("button", { class: "btn sm", onclick: () => { hp.hidden = true; } }, "Close")));
    hp.hidden = false;
  }

  /* ================= boot ================= */
  OG.start = () => {
    // old hash URLs (#/market, #/market/<gpu>) -> new paths
    const legacy = legacyHash(location.hash);
    if (legacy) history.replaceState(null, "", legacy);
    document.addEventListener("click", e => {
      if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      const a = e.target.closest && e.target.closest("a[href]");
      if (!a || a.target || a.hasAttribute("download") || a.getAttribute("rel") === "external") return;
      const u = new URL(a.href, location.href);
      if (u.origin !== location.origin || /^\/(static|v1|docs|openapi|classic|redoc)/.test(u.pathname)) return;
      if (u.pathname === location.pathname && u.search === location.search && u.hash) return;   // in-page anchor
      if (!OG.match(u.pathname)) return;
      e.preventDefault();
      OG.go(u.pathname + u.search + u.hash);
    });
    window.addEventListener("popstate", render);
    builtinCommands();
    setupCommandBar();
    OG.data.intervals();
    render();
    loadPolling(); loadTape();
    setInterval(() => { if (!document.hidden) { loadTape(); drawStatus(); } }, 60000);
  };
  return OG;
});
