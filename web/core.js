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
API          OG.api(path, {params, method, body, full, slot, nocache, idempotencyKey, keepalive}) -> Promise<data>
             money-moving POSTs: intent = OG.intentFor(prev, action, body); OG.api.intent(intent, path, opts)
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
Badges       OG.kindBadge("observed"|"estimated"|"transaction"; "inferred" renders nothing) · OG.freshBadge(isoOrSeconds|label)
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

  // Which left-nav link (data-nav) a path highlights when its page sets no `nav` of its own.
  // "/" is the homepage (no nav link: the brand is home); /admin/<x> -> "admin-<x>"; else the first segment,
  // with the singular detail paths folded into their list page.
  function navKeyFor(path) {
    const parts = String(path || "/").split("?")[0].split("/");
    const seg0 = parts[1] || "";
    if (seg0 === "admin") return parts[2] ? "admin-" + parts[2] : "admin-execution";
    return { "": "home", gpu: "gpus", gpus: "gpus", provider: "providers", indices: "indices", compare: "compare", methodology: "methodology",
      markets: "markets", overview: "markets", job: "jobs", jobs: "jobs", deployment: "deployments", developers: "developers" }[seg0] ?? seg0;
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
      if (v == null || !isFinite(v)) return "-";
      const d = digits == null ? (Number.isInteger(v) ? 0 : 2) : digits;
      const [ip, fp] = Math.abs(v).toFixed(d).split(".");          // group the integer part only
      const grouped = ip.replace(/\B(?=(\d{3})+(?!\d))/g, ",") + (fp ? "." + fp : "");
      return (v < 0 ? L.MINUS : "") + grouped;
    },
    compact(v) {
      v = toNum(v);
      if (v == null || !isFinite(v)) return "-";
      const a = Math.abs(v), sign = v < 0 ? L.MINUS : "";
      for (const [n, u] of [[1e9, "B"], [1e6, "M"], [1e3, "k"]]) if (a >= n) return sign + (a / n).toFixed(a / n < 10 ? 1 : 0) + u;
      return sign + (Number.isInteger(a) ? a : a.toFixed(1));
    },
    // Seconds (number) or a time (ISO / Date) -> "now", "42s", "5m", "3h", "2d"
    age(x, now) {
      if (x == null) return "-";
      const sec = typeof x === "number" ? x : ((now != null ? +new Date(now) : Date.now()) - +new Date(x)) / 1000;
      if (!isFinite(sec)) return "-";
      const a = Math.max(0, sec);
      if (a < 5) return "now";
      if (a < 60) return Math.round(a) + "s";
      if (a < 3600) return Math.floor(a / 60) + "m";
      if (a < 86400 * 2) return Math.floor(a / 3600) + "h";
      return Math.floor(a / 86400) + "d";
    },
    time(iso) { if (!iso) return "-"; const d = new Date(iso); return isNaN(d) ? "-" : p2(d.getHours()) + ":" + p2(d.getMinutes()) + ":" + p2(d.getSeconds()); },
    date(iso) { if (!iso) return "-"; const d = new Date(iso); return isNaN(d) ? "-" : MONTHS[d.getMonth()] + " " + d.getDate(); },
    dateTime(iso) { if (!iso) return "-"; const d = new Date(iso); return isNaN(d) ? "-" : MONTHS[d.getMonth()] + " " + d.getDate() + " " + p2(d.getHours()) + ":" + p2(d.getMinutes()); },
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
    if (/insufficient coverage|eligible provider|component weight/i.test(s)) return "too few constituents";
    if (/does not cover|insufficient history|not enough (history|hours|data)|needs? ≥|fewer than/i.test(s)) return "not enough history";
    if (/not published|unpublished/i.test(s)) return "not published";
    if (/unavailable|not available/i.test(s)) return "unavailable";
    if (/one provider|single provider|only provider/i.test(s)) return "one provider only";
    const cut = s.slice(0, n - 1), sp = cut.lastIndexOf(" ");
    return (sp > n * 0.5 ? cut.slice(0, sp) : cut).replace(/[\s,;:.(\u2013\u2014-]+$/, "") + "…";
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

  // A usage line "g <gpu>          open a GPU market" -> { syntax, desc, insert }: `insert` is what a click on the
  // suggestion types into the bar (the text before the first <argument>); null when the command needs no argument.
  function commandParts(usage) {
    const [syntax, ...rest] = String(usage || "").split(/\s{2,}/);
    const i = syntax.search(/[<[]/);
    return { syntax: syntax.trim(), desc: rest.join(" ").trim(), insert: i > 0 ? syntax.slice(0, i) : null };
  }

  /* ================= pure: plain-language shell copy ================= */
  // Who can use what. One vocabulary for the access indicator, nav locks and sign-in states (shared with the
  // Jobs, Keys and Developers pages: OG.accessNote(kind)).
  // The browser sign-in is the OpenGrid staff login (/login); customers use an API key issued by OpenGrid.
  const ACCESS = {
    public: { label: "Free, no account", text: "Anyone can use this. No account needed." },
    account: { label: "Invite only", text: "This needs an OpenGrid account, and accounts are invite-only for now (no self-serve sign-up). The OpenGrid team issues API keys for code, the CLI and AI assistants." },
    operator: { label: "OpenGrid staff only", text: "These are OpenGrid's internal operator tools." },
    simulated: { label: "Simulation", text: "A simulation: no GPU is rented and nothing is charged." },
  };
  // An API error -> what to tell a person: a short title, one sentence, the next step, and the technical detail
  // (kept for a "Technical details" fold, never the headline).
  function explainError(err) {
    const st = err && typeof err.status === "number" ? err.status : null;
    const tech = [st ? "HTTP " + st : null, err && err.message, err && err.path ? String(err.path).replace(/^https?:\/\/[^/]+/, "") : null].filter(Boolean).join(" · ");
    const base = { tech: tech || String(err || ""), action: "retry" };
    if (st === 401) return Object.assign(base, { title: "Invite only", text: ACCESS.account.text, action: "signin" });
    if (st === 403) return Object.assign(base, { title: "Not included in your access", text: "You are signed in, but your account or API key does not cover this. An OpenGrid admin can add it.", action: null });
    if (st === 404) return Object.assign(base, { title: "Not found", text: "We could not find this. It may have been removed, or the link is wrong.", action: null });
    if (st === 429) return Object.assign(base, { title: "Too many requests", text: "You are going a little fast. Wait a few seconds, then try again." });
    if (st === 0 || st == null) return Object.assign(base, { title: "Could not reach OpenGrid", text: "Check your connection, then try again." });
    if (st >= 500) return Object.assign(base, { title: "OpenGrid had a problem", text: "Something went wrong on our side. Try again in a moment." });
    return Object.assign(base, { title: "Could not load this", text: (err && err.message) || "The request was not accepted." });
  }
  // The status bar's freshness line: when prices were last read, in words, with the exact time in `title`.
  // Prices are read every few minutes, so older than 10 minutes is "may be out of date".
  function ago(sec) {
    if (sec < 45) return "just now";
    if (sec < 90) return "1 min ago";
    if (sec < 3600) return Math.round(sec / 60) + " min ago";
    if (sec < 5400) return "1 hour ago";
    if (sec < 86400) return Math.round(sec / 3600) + " hours ago";
    return Math.round(sec / 86400) + " days ago";
  }
  function freshnessText(asOf, nowMs, ok, failText) {
    if (ok === false) return { tone: "down", text: failText || "Can't reach OpenGrid", title: "The price service did not answer. Prices on this page may be out of date." };
    const t = asOf ? Date.parse(asOf) : NaN;
    if (!isFinite(t) && ok === true) return { tone: "", text: "Prices loaded", title: "OpenGrid could not tell when providers were last read." };
    if (!isFinite(t)) return { tone: "", text: "Connecting…", title: "" };
    const sec = Math.max(0, ((nowMs == null ? Date.now() : nowMs) - t) / 1000);
    const when = new Date(t).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "medium" });
    if (sec > 600) return { tone: "stale", text: "Prices may be out of date: last read " + ago(sec), title: "Prices were last read from providers at " + when + " (your time). They are normally refreshed every few minutes." };
    return { tone: "live", text: "Prices updated " + ago(sec), title: "Prices were last read from providers at " + when + " (your time)." };
  }
  // When prices were last read: the newest successful provider fetch in /v1/trust/providers (null if none).
  function lastReadOf(providers) {
    let best = null, bt = -Infinity;
    for (const p of Array.isArray(providers) ? providers : []) {
      const t = p && p.last_ok_fetch ? Date.parse(p.last_ok_fetch) : NaN;
      if (isFinite(t) && t > bt) { bt = t; best = p.last_ok_fetch; }
    }
    return best;
  }
  // How many tape items fit whole in `avail` px (gap between items; `more` px reserved for "+N more" when not all fit).
  function tapeFit(widths, avail, gap, more) {
    let used = 0;
    const all = widths.reduce((a, w, i) => a + w + (i ? gap : 0), 0);
    if (all <= avail) return widths.length;
    for (let i = 0; i < widths.length; i++) {
      const next = used + (i ? gap : 0) + widths[i];
      if (next + gap + more > avail) return i;
      used = next;
    }
    return widths.length;
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

  /* ================= pure: idempotency intents ================= */
  // Money-moving POSTs (route, approve, terminate, stop) carry an Idempotency-Key. ONE key per user intent:
  // a retry of the SAME action after an unknown outcome (network error, 5xx, 429, 409 idempotency_in_progress)
  // reuses the key, so the server replays instead of acting twice; any definitive answer settles the intent
  // and the next click is a new intent with a new key. The client never retries on its own.
  function randomId(n, rand) {
    const abc = "abcdefghijklmnopqrstuvwxyz0123456789";
    let out = "";
    const c = !rand && typeof crypto !== "undefined" && crypto.getRandomValues ? crypto.getRandomValues(new Uint8Array(n)) : null;
    for (let i = 0; i < n; i++) out += abc[(c ? c[i] : Math.floor((rand || Math.random)() * 256)) % abc.length];
    return out;
  }
  function stableJson(v) {
    if (v === undefined) return "null";
    if (v === null || typeof v !== "object") return JSON.stringify(v);
    if (Array.isArray(v)) return "[" + v.map(stableJson).join(",") + "]";
    return "{" + Object.keys(v).sort().filter(k => v[k] !== undefined).map(k => JSON.stringify(k) + ":" + stableJson(v[k])).join(",") + "}";
  }
  // Did the request possibly happen without us learning the answer? Then the same key must be reused.
  function outcomeUnknown(err) {
    if (!err) return false;
    const s = err.status, d = err.body && err.body.detail;
    if (s === 0 || s === 429 || s >= 500) return true;
    return s === 409 && !!d && typeof d === "object" && d.code === "idempotency_in_progress";
  }
  // intentFor(previous, action, body): reuse `previous` only when it is the same action + body and its last
  // attempt ended with an unknown outcome; else a new intent with a fresh key.
  function intentFor(prev, action, body, rand) {
    const b = stableJson(body == null ? {} : body);
    if (prev && prev.state === "unknown" && prev.action === action && prev.body === b) return prev;
    return { key: "ogui-" + randomId(26, rand), action, body: b, state: "new", attempts: 0 };
  }
  function settleIntent(intent, err) {
    intent.attempts++;
    intent.state = !err ? "done" : outcomeUnknown(err) ? "unknown" : "failed";
    return intent;
  }

  /* ================= pure: deployment state machine (mirrors routing/deployments.py) ================= */
  const DEP_FLOW = ["created", "quoted", "pending_approval", "approved", "provisioning", "running", "stopping", "stopped", "terminating", "terminated"];
  const DEP_UNCERTAIN = ["provider_timeout", "launch_unknown", "orphan_suspected", "credentials_unavailable", "degraded"];
  const DEP_FAILED = ["quote_failed", "quote_expired", "rejected", "provision_failed", "provider_rejected", "termination_failed"];
  // Every state in which an instance may exist at the provider (may bill).
  const DEP_LIVE = ["provisioning", "running", "degraded", "stopping", "stopped", "terminating", "termination_failed", "provider_timeout",
    "launch_unknown", "orphan_suspected", "credentials_unavailable"];
  function depTone(s) {
    if (DEP_FAILED.includes(s)) return "bad";
    if (DEP_UNCERTAIN.includes(s)) return "warn";
    if (s === "running") return "good";
    if (["pending_approval", "approved", "provisioning", "stopping", "terminating"].includes(s)) return "busy";
    return "";
  }
  // The strip: the main flow with done / current / todo / skipped marks; a failure or uncertain current state
  // is inserted right after the furthest main-flow state the deployment reached (from its event history).
  function depStrip(status, history) {
    const seen = new Set((history || []).filter(Boolean));
    const known = seen.size > 0;
    let cur = DEP_FLOW.indexOf(status);
    let after = cur;
    if (cur < 0) { after = -1; DEP_FLOW.forEach((s, i) => { if (seen.has(s)) after = i; }); if (!known) after = 0; }
    const steps = DEP_FLOW.map((s, i) => {
      let mark = i < (cur >= 0 ? cur : after + 1) ? "done" : "todo";
      if (i === cur) mark = "current";
      if (mark === "done" && known && !seen.has(s)) mark = "skipped";
      return { state: s, mark, tone: mark === "current" ? depTone(s) : "" };
    });
    if (cur < 0 && status) steps.splice(after + 1, 0, { state: status, mark: "current", tone: depTone(status) || "bad", off: true });
    return steps;
  }

  /* ================= pure: product analytics tracker ================= */
  // Anonymous, cookie-less: a random id in sessionStorage, batched, max 50 events per POST, failures dropped,
  // nothing tracked when Do Not Track is on. No PII: props are page paths, GPU slugs, provider names, queries.
  const TRACK_EVENTS = ["page_view", "search", "gpu_view", "provider_view", "compare", "watchlist_create"];
  function makeTracker(o) {
    o = o || {};
    const max = o.max || 50, q = [];
    let anon = null;
    function anonId() {
      if (anon) return anon;
      try { anon = o.storage && o.storage.getItem("og-anon"); } catch (e) { anon = null; }
      if (!anon || !/^[A-Za-z0-9_-]{8,64}$/.test(anon)) {
        anon = "a" + randomId(19, o.rand);
        try { o.storage && o.storage.setItem("og-anon", anon); } catch (e) { /* private mode: per page load */ }
      }
      return anon;
    }
    return {
      queue: q,
      enabled: !o.dnt,
      track(event, props) {
        if (o.dnt || !TRACK_EVENTS.includes(event)) return false;
        q.push({ event, anon_id: anonId(), props: props || {}, ts: new Date(o.now ? o.now() : Date.now()).toISOString() });
        if (q.length > 500) q.splice(0, q.length - 500);
        return true;
      },
      // Sends everything queued in batches of `max`; returns the number of batches. Never throws, never retries.
      flush(opts) {
        let n = 0;
        while (q.length) {
          const batch = q.splice(0, max);
          n++;
          try { Promise.resolve(o.send(batch, opts || {})).catch(() => {}); } catch (e) { /* dropped */ }
        }
        return n;
      },
      anonId,
    };
  }

  Object.assign(OG, { compile, matchRoute, legacyHash, navKeyFor, slug, fmt, freshness, freshLimits, cleanKids, familyTarget, shortReason, sortRows, csvCell, csvString,
    fuzzy, fuzzyScore, parseCommand, command, findCommand, COMMANDS, commandParts, ACCESS, explainError, freshnessText, lastReadOf, tapeFit, unwrap, withParams, reasonOf,
    randomId, stableJson, outcomeUnknown, intentFor, settleIntent, makeTracker, TRACK_EVENTS,
    dep: { FLOW: DEP_FLOW, UNCERTAIN: DEP_UNCERTAIN, FAILED: DEP_FAILED, LIVE: DEP_LIVE, tone: depTone, strip: depStrip },
    qs: { parse: qsParse, stringify: qsStringify } });

  /* ================= API ================= */
  class ApiError extends Error {
    constructor(status, message, path, body) { super(message); this.status = status; this.path = path; this.body = body; }
  }
  OG.ApiError = ApiError;
  const inflight = new Map(), slots = new Map();
  async function doFetch(url, method, opts) {
    const init = { method, credentials: "same-origin", headers: { accept: "application/json" } };
    // CSRF: the server refuses state changes under the site login without this header (main.py).
    if (method !== "GET" && method !== "HEAD") init.headers["x-opengrid-request"] = "1";
    if (opts.body !== undefined) { init.body = typeof opts.body === "string" ? opts.body : JSON.stringify(opts.body); init.headers["content-type"] = "application/json"; }
    if (opts.headers) Object.assign(init.headers, opts.headers);
    // one key per user intent (see intentFor); never generated here, so a retry can reuse it
    if (opts.idempotencyKey) init.headers["idempotency-key"] = String(opts.idempotencyKey);
    if (opts.keepalive) init.keepalive = true;
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
  // api.intent(intent, path, opts): one attempt of a user intent with its Idempotency-Key; settles the intent
  // (done / failed / unknown). Never retries by itself: the user retries, and an unknown outcome reuses the key.
  api.intent = async (intent, path, opts) => {
    try { const r = await api(path, Object.assign({}, opts, { idempotencyKey: intent.key })); settleIntent(intent); return r; }
    catch (e) { settleIntent(intent, e); throw e; }
  };
  OG.api = api;

  /* ================= pure: URL guard ================= */
  // One central guard for every URL-valued attribute OG.h / OG.s set, and for OG.go: only
  // http(s)/mailto, relative paths, fragments and queries pass. javascript: / data: / vbscript:
  // (any case, with embedded whitespace or control characters) become "#": data (e.g. a news
  // feed link) must never run script on this origin.
  const URL_ATTRS = new Set(["href", "src", "action", "formaction", "xlink:href", "srcdoc", "poster", "data"]);
  function safeUrl(v) {
    const raw = String(v).trim();
    const m = /^([a-z][a-z0-9+.-]*):/i.exec(raw.replace(/[\x00-\x20\x7f]+/g, ""));
    if (!m) return raw;  // no scheme: a relative path, #fragment or ?query
    return /^(https?|mailto)$/i.test(m[1]) ? raw : "#";
  }
  OG.safeUrl = safeUrl;

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
      if (URL_ATTRS.has(k.toLowerCase())) { el.setAttribute(k, k.toLowerCase() === "srcdoc" ? "" : safeUrl(v)); continue; }
      if (k.startsWith("on") && typeof v !== "function") continue;  // never inline handler strings
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
      else if (k.startsWith("on")) continue;
      else el.setAttribute(k, URL_ATTRS.has(k.toLowerCase()) ? safeUrl(v) : v);
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
    if (d === "none") return h("span", { class: "chg none", title: (opts && opts.reason) || "not enough history" }, "-");
    const arrow = L.direction(toNum(v)) === "up" ? "▲" : L.direction(toNum(v)) === "down" ? "▼" : "";
    return h("span", { class: "chg " + d }, arrow ? arrow + " " : "", fmt.pct(v));
  };

  // /login always asks the browser for the site login (main.py), then returns here.
  OG.loginHref = () => "/login?next=" + encodeURIComponent((location.pathname || "/") + (location.search || ""));
  OG.loading = text => h("div", { class: "state loading", role: "status" }, h("i", { class: "spin" }), text || "Loading…");
  OG.empty = text => h("div", { class: "state empty" }, text || "Nothing to show.");
  // Errors read as a sentence and a next step; the HTTP status and endpoint stay in "Technical details".
  // A 401 is an access state, not a failure: invite only, how to get an API key, and (small) the staff sign-in,
  // the only browser login that exists.
  OG.error = (err, retry) => {
    const x = explainError(err);
    const signin = x.action === "signin";
    return h("div", { class: "state error" + (signin ? " gate" : ""), role: signin ? "status" : "alert" },
      h("div", { class: "st-body" },
        h("b", {}, signin ? [h("i", { class: "lock", "aria-hidden": "true" }), " ", x.title] : x.title),
        h("p", {}, x.text),
        h("div", { class: "st-acts" },
          signin ? h("a", { class: "btn sm", href: "/developers#access" }, "How to get an API key") : null,
          signin ? h("a", { class: "btn sm", href: "/" }, "Explore free pages") : null,
          signin ? h("a", { class: "lnk st-op", href: OG.loginHref() }, "OpenGrid staff: sign in") : null,
          retry && x.action === "retry" ? h("button", { class: "btn sm", type: "button", onclick: retry }, "Try again") : null),
        x.tech ? h("details", { class: "st-tech" }, h("summary", {}, "Technical details"), h("code", { class: "mono" }, x.tech)) : null));
  };
  // A standard access explanation for pages that need an account (Jobs, Keys, Watchlists...): call before the
  // data loads so a visitor is never surprised by an error. kind: "account" | "operator" | "simulated" | "public".
  OG.accessNote = (kind, extra) => {
    const a = ACCESS[kind] || ACCESS.account;
    return h("div", { class: "access-note k-" + (ACCESS[kind] ? kind : "account") },
      h("b", {}, kind === "account" || kind === "operator" ? h("i", { class: "lock", "aria-hidden": "true" }) : null, a.label),
      h("span", {}, " " + a.text + (extra ? " " + extra : "")));
  };
  OG.signedIn = () => document.body && document.body.classList.contains("og-signed-in");
  // The API returns null + a reason when coverage is thin: show the reason, never a number.
  OG.insufficient = (reason, title) => h("div", { class: "state insufficient" },
    h("b", {}, title || "Insufficient data"), h("span", {}, " " + (reason || "not enough observed history to compute this honestly")));
  OG.na = reason => h("span", { class: "na", title: reason || "unavailable" }, "n/a");
  OG.value = (v, f, reason) => (v == null || (typeof v === "number" && !isFinite(v)) ? OG.na(reason) : (f || fmt.num)(v));
  OG.reasonOf = reasonOf;

  const KIND_HELP = {
    observed: "Read directly from a provider and normalized",
    estimated: "A model or assumption fills a gap",
    transaction: "What an OpenGrid execution actually quoted or paid",
  };
  OG.badge = (text, tone, title) => h("span", { class: "badge " + (tone || ""), title: title || null }, text);
  // "inferred" is a data kind in the API but is not shown as a badge in the UI
  OG.kindBadge = kind => kind === "inferred" ? null : h("a", { class: "badge kind k-" + kind, href: "/methodology/data-kinds", title: KIND_HELP[kind] || kind }, kind);
  // Poll interval per provider (seconds), from /polling, loaded at start: freshBadge(..., {provider}) uses it.
  const INTERVALS = new Map();
  OG.data_intervals = INTERVALS;
  OG.pollInterval = p => INTERVALS.get(p) || null;
  // OG.freshBadge(isoOrSeconds | label, {intervalSeconds, provider, now}): thresholds relative to the
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
  // OG.ask({title, body, fields, confirm, danger}) -> Promise<values | null>. fields: [{key, label, type:
  // "text" | "textarea" | "checkbox" | "select", required, minLength, placeholder, options, match (exact text the
  // user must type, e.g. the provider name), show(values) (visible only when true), hint}]. Confirm stays
  // disabled until every visible required field is valid. For approvals, overrides, kill switches, reasons.
  OG.ask = o => new Promise(resolve => {
    const prev = document.activeElement;
    const inputs = {};
    const vals = () => Object.fromEntries(Object.entries(inputs).map(([k, i]) => [k, i.type === "checkbox" ? i.checked : i.value.trim()]));
    const rows = (o.fields || []).map(f => {
      let i;
      if (f.type === "textarea") i = h("textarea", { class: "field og-ask-ta", rows: 3, placeholder: f.placeholder || "", "aria-label": f.label });
      else if (f.type === "select") i = h("select", { class: "field", "aria-label": f.label }, (f.options || []).map(([v, l]) => h("option", { value: v }, l)));
      else i = h("input", { class: f.type === "checkbox" ? "" : "field og-ask-in", type: f.type === "checkbox" ? "checkbox" : "text", placeholder: f.placeholder || "", autocomplete: "off", spellcheck: "false", "aria-label": f.label });
      if (f.value != null) { if (f.type === "checkbox") i.checked = !!f.value; else i.value = f.value; }
      inputs[f.key] = i;
      const row = h("label", { class: "og-ask-f" + (f.type === "checkbox" ? " chk" : "") }, f.type === "checkbox" ? [i, h("span", {}, f.label)] : [h("span", { class: "og-ask-l" }, f.label), i], f.hint ? h("span", { class: "og-ask-h" }, f.hint) : null);
      return { f, row, i };
    });
    const ok = h("button", { class: "btn " + (o.danger ? "w4-danger" : "pri"), type: "button", disabled: true, onclick: () => close(true) }, o.confirm || "Confirm");
    function valid() {
      const v = vals();
      let good = true;
      for (const { f, row } of rows) {
        const shown = !f.show || f.show(v);
        row.hidden = !shown;
        if (!shown) continue;
        const x = v[f.key];
        if (f.match != null && String(x).toLowerCase() !== String(f.match).toLowerCase()) good = false;
        if (f.required && f.type !== "checkbox" && String(x).length < (f.minLength || 1)) good = false;
        if (f.required && f.type === "checkbox" && !x) good = false;
      }
      ok.disabled = !good;
    }
    rows.forEach(r => { r.i.addEventListener("input", valid); r.i.addEventListener("change", valid); });
    const box = h("div", { class: "w4-dlg og-ask", role: "dialog", "aria-modal": "true", "aria-label": o.title },
      h("div", { class: "w4-dlg-h" }, o.title), h("div", { class: "w4-dlg-b" }, o.body, rows.map(r => r.row)),
      h("div", { class: "w4-dlg-f" }, h("button", { class: "btn", type: "button", onclick: () => close(false) }, o.cancel || "Cancel"), ok));
    const ov = h("div", { class: "w4-ov", onmousedown: e => { if (e.target === ov) close(false); } }, box);
    const key = e => {
      if (e.key === "Escape") { e.preventDefault(); e.stopImmediatePropagation(); close(false); }
      else if (e.key === "Enter" && e.target.tagName === "INPUT" && !ok.disabled) { e.preventDefault(); close(true); }
    };
    let done = false;
    function close(v) {
      if (done) return; done = true;
      document.removeEventListener("keydown", key, true); ov.remove();
      if (prev && prev.focus) prev.focus();
      resolve(v ? vals() : null);
    }
    document.addEventListener("keydown", key, true);
    document.body.append(ov);
    valid();
    const first = rows.find(r => !r.row.hidden);
    (first ? first.i : ok).focus();
  });

  /* ---- shared: execution (deployment state, quote countdown, execution mode) ---- */
  OG.stateBadge = s => h("span", { class: "badge og-st st-" + (depTone(s) || "plain"), title: DEP_UNCERTAIN.includes(s) ? "uncertain: an instance may exist and bill" : DEP_FAILED.includes(s) ? "failure state" : null }, String(s || "-").replace(/_/g, " "));
  OG.stateStrip = (status, events) => h("ol", { class: "og-strip", "aria-label": "deployment state machine" },
    depStrip(status, (events || []).map(e => e.to)).map(st => h("li", { class: "m-" + st.mark + (st.tone ? " t-" + st.tone : "") + (st.off ? " off" : ""), title: st.mark === "skipped" ? st.state + ": not passed through" : st.state, "aria-current": st.mark === "current" ? "step" : null }, st.state.replace(/_/g, " "))));
  // A live countdown to `iso` ("4m 12s"); "expired" (or o.expired) after. Stops when removed from the page.
  OG.countdown = (iso, o) => {
    o = o || {};
    const el = h("span", { class: "og-cd mono" });
    const t = +new Date(iso);
    let fired = false;
    const tick = () => {
      if (!iso || isNaN(t)) { el.textContent = "-"; return false; }
      const s = Math.round((t - Date.now()) / 1000);
      if (s <= 0) {
        el.textContent = o.expired || "expired"; el.classList.add("over");
        if (!fired && o.onExpire) { fired = true; o.onExpire(); }
        return false;
      }
      const d = Math.floor(s / 86400), hh = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60), ss = s % 60;
      el.textContent = (o.prefix || "") + (d ? d + "d " + hh + "h" : hh ? hh + "h " + p2(m) + "m" : m + "m " + p2(ss) + "s");
      el.classList.toggle("soon", s < (o.soon || 60));
      return true;
    };
    tick();
    let mounted = false;
    const id = setInterval(() => {
      if (el.isConnected) mounted = true;
      else if (mounted) { clearInterval(id); return; }   // removed from the page
      if (!tick()) clearInterval(id);
    }, 1000);
    return el;
  };
  const MODE_HELP = {
    DISABLED: "Kill switch: no new launch anywhere. Monitoring, reconciliation, stop and terminate keep working.",
    PREVIEW_ONLY: "Routes rank, check and quote; nothing is ever launched.",
    SUPERVISED: "Launches happen only after an OpenGrid admin approves each quote, on validated providers with supervised launches enabled.",
    LIVE: "Validated, live-enabled providers launch without per-launch approval (within cost guards).",
  };
  OG.MODE_HELP = MODE_HELP;
  // ms = GET /v1/admin/execution/mode (or {effective_mode} from a route response); null -> unknown
  OG.modeBanner = ms => {
    const eff = ms && (ms.effective_mode || ms.execution_mode);
    if (!eff) return h("div", { class: "og-mode m-unknown" }, h("b", {}, "EXECUTION MODE UNKNOWN"), h("span", {}, "This page cannot read the execution mode (admin only). The server enforces it on every request."));
    const capped = ms.stored_mode && ms.stored_mode !== eff;
    return h("div", { class: "og-mode m-" + eff.toLowerCase() }, h("b", {}, eff.replace("_", " ")), h("span", {}, MODE_HELP[eff] || ""),
      capped ? h("span", { class: "og-mode-cap" }, `stored mode ${ms.stored_mode}, capped by the deploy-level env flag (ROUTING_LIVE_PROVISIONING=false)`) : null);
  };

  // Price-concept badge: the four concepts are never mixed (methodology/data-kinds).
  const CONCEPT = {
    list: ["list", "List price: the provider's published catalogue price"],
    observed: ["observed", "Observed market price: OpenGrid's normalized reading of a live listing"],
    quote: ["quote", "Quote: the price this route would transact at, from the observed listing (preview) or the provider API (route)"],
    execution: ["execution", "Execution price: what the provider actually charged a real deployment"],
  };
  OG.conceptBadge = c => h("a", { class: "badge w4-pc pc-" + c, href: "/methodology/data-kinds", title: (CONCEPT[c] || [c, c])[1] }, (CONCEPT[c] || [c])[0]);
  OG.money = v => (v == null || !isFinite(v) ? "-" : (v < 0 ? "−$" : "$") + fmt.num(Math.abs(v), 2));

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
  // item.fold: when its value is missing it is not drawn as its own n/a cell; every folded item is named in one
  // trailing cell ("Not enough history: 7d · 30d · Vol 30d", reasons on hover) so a strip is never a row of n/a
  OG.stats = items => {
    items = items.filter(Boolean);
    const folded = items.filter(it => it.fold && (it.value == null || it.value === ""));
    if (folded.length) {
      items = items.filter(it => !folded.includes(it));
      items.push({ label: folded.every(it => /history|window|returns|cover/i.test(it.reason || "")) ? "Not enough history yet" : "Not available yet",
        value: h("span", { class: "stat-fold" }, folded.map(it => it.label).join(" · ")),
        title: folded.map(it => it.label + ": " + (it.reason || "unavailable")).join("\n") });
    }
    return statsEl(items);
  };
  const statsEl = items => h("div", { class: "stats" }, items.map(it => {
    const missing = it.value == null || it.value === "";
    const line = it.sub != null && it.sub !== "" ? it.sub : missing && it.reason ? shortReason(it.reason) : null;
    const full = [it.title, missing && it.reason && it.reason !== line ? it.reason : null, typeof line === "string" && line.length > 30 ? line : null].filter(Boolean);
    return h("div", { class: "stat", title: full.length ? [...new Set(full)].join("; ") : null },
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
    const thead = h("thead"), tbody = h("tbody"), more = h("div", { class: "tbl-more" }), foot = h("div", { class: "tbl-foot" });
    tbl.append(thead, tbody); scroll.append(tbl);
    if (spec.title || spec.csv || spec.toolbar) wrap.append(bar);
    wrap.append(scroll, more, foot);
    // Column visibility: a column whose every cell is empty ("-", "n/a", blank) is hidden and named once in the
    // footer (its reason on hover) instead of repeating n/a down the page (spec.autoHide: false or column.keep
    // to opt out); columns with `minor: n` are dropped highest-n first when the table is wider than its box.
    // Anything still too wide scrolls sideways with a visible fade at the clipped edge.
    let autoHidden = new Map(), fitHidden = new Set(), lastW = 0;
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
      let out = c.fmt ? c.fmt(c.key in r ? r[c.key] : v, r) : v == null || v === "" ? "-" : c.num && typeof v === "number" ? fmt.num(v) : String(v);
      if (c.href) { const href = c.href(r); if (href) out = h("a", { class: "lnk", href }, out); }
      if (c.ell) { out = h("span", { class: "ell", style: `max-width:${typeof c.ell === "number" ? c.ell + "px" : c.ell}` }, out); if (!out.querySelector("[title]")) out.title = out.textContent; }
      return h("td", { class: (c.num ? "n " : "") + (c.cls ? (typeof c.cls === "function" ? c.cls(r) || "" : c.cls) : "") + stickCls(i) }, out);
    }
    const EMPTY_TXT = new Set(["", "-", "\u2013", "\u2014", "n/a"]);
    // empty = only "-" (or a legacy en/em dash) / "n/a" / blank text, and no purely visual element (a bar, a swatch, a sparkline)
    const emptyCell = td => EMPTY_TXT.has(td.textContent.trim()) && !td.querySelector("svg,img,input,button,select,canvas") &&
      [...td.querySelectorAll("*")].every(e => e.textContent.trim() !== "");
    function setCol(i, hide) {
      const th = thead.firstChild && thead.firstChild.children[i];
      if (th) th.hidden = hide;
      for (const tr of tbody.children) if (tr.children.length === cols.length) tr.children[i].hidden = hide;
    }
    // which columns carry no value at all in the rows drawn (needs >= 2 rows: one empty cell is information)
    function findEmpty() {
      autoHidden = new Map();
      const trs = [...tbody.children].filter(tr => tr.children.length === cols.length);
      if (spec.autoHide === false || trs.length < 2) return;
      cols.forEach((c, i) => {
        if (c.keep) return;
        if (!trs.every(tr => emptyCell(tr.children[i]))) return;
        const why = trs.map(tr => tr.children[i].querySelector("[title]")).find(Boolean);
        autoHidden.set(i, why ? why.getAttribute("title") : "");
      });
    }
    function layout() {
      cols.forEach((c, i) => setCol(i, autoHidden.has(i)));
      fitHidden = new Set();
      if (wrap.isConnected && scroll.clientWidth > 0) {
        const minors = cols.map((c, i) => [c.minor || 0, i]).filter(x => x[0] > 0 && !autoHidden.has(x[1])).sort((a, b) => b[0] - a[0] || b[1] - a[1]);
        for (const [, i] of minors) { if (scroll.scrollWidth <= scroll.clientWidth + 1) break; fitHidden.add(i); setCol(i, true); }
      }
      edges();
      const lab = i => cols[i].label || cols[i].key;
      const parts = [];
      if (autoHidden.size) {
        const tip = [...autoHidden].map(([i, why]) => lab(i) + (why ? ": " + why : "")).join("\n");
        parts.push(h("span", { title: tip }, h("span", { class: "dimmer" }, "No data yet: "), [...autoHidden.keys()].map(lab).join(" · ")));
      }
      if (fitHidden.size) parts.push(h("span", { title: "Widen the window to show them; the CSV download has every column" }, h("span", { class: "dimmer" }, "Hidden at this width: "), [...fitHidden].sort((a, b) => a - b).map(lab).join(" · ")));
      foot.replaceChildren(...parts);
      if (nStick) placeSticky();
    }
    // fade at whichever edge has more table to scroll to
    function edges() {
      const over = scroll.scrollWidth > scroll.clientWidth + 1;
      scroll.classList.toggle("ovf", over);
      scroll.classList.toggle("ovf-r", over && scroll.scrollLeft + scroll.clientWidth < scroll.scrollWidth - 2);
      scroll.classList.toggle("ovf-l", over && scroll.scrollLeft > 2);
    }
    scroll.addEventListener("scroll", edges, { passive: true });
    function drawBody() {
      const list = sorted();
      if (!list.length) { tbody.replaceChildren(h("tr", {}, h("td", { colspan: cols.length, class: "tbl-empty" }, spec.empty || "No rows."))); more.textContent = ""; findEmpty(); layout(); return; }
      const shown = list.slice(0, limit);
      tbody.replaceChildren(...shown.map(r => {
        const href = spec.rowHref ? spec.rowHref(r) : null, act = href || spec.onRow;
        const tr = h("tr", { class: (act ? "act " : "") + (spec.rowClass ? spec.rowClass(r) || "" : ""), tabindex: act ? "0" : null, "data-k": spec.rowKey ? spec.rowKey(r) : null },
          cols.map((c, i) => cell(c, r, i)));
        if (act) {
          tr.addEventListener("click", e => { if (e.target.closest("a,button,input,select")) return; if (href) (e.metaKey || e.ctrlKey ? window.open(safeUrl(href)) : OG.go(href)); else spec.onRow(r, e); });
          tr.addEventListener("keydown", e => { if (e.key === "Enter" && e.target === tr) { if (href) OG.go(href); else spec.onRow(r, e); } });
        }
        if (spec.onHover) { tr.addEventListener("mouseenter", () => spec.onHover(r)); tr.addEventListener("mouseleave", () => spec.onHover(null)); }
        return tr;
      }));
      more.replaceChildren(list.length > limit ? h("button", { class: "btn sm", onclick: () => { limit += spec.limit || 500; drawBody(); } }, list.length - limit <= (spec.limit || 500) ? `Show ${list.length - limit} more` : `Show ${spec.limit || 500} more (${list.length - limit} hidden)`) : "");
      findEmpty(); layout();
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
    // re-fit when the box changes width (first paint, window resize, a drawer opening)
    if (window.ResizeObserver) new ResizeObserver(() => { const w = scroll.clientWidth; if (Math.abs(w - lastW) < 2) return; lastW = w; layout(); }).observe(scroll);
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
    if (!/^https?:$/.test(u.protocol)) return;  // never navigate to javascript: / data: (see safeUrl)
    if (u.origin !== location.origin || !OG.match(u.pathname)) { location.href = u.href; return; }
    history[opts && opts.replace ? "replaceState" : "pushState"](null, "", u.pathname + u.search + u.hash);
    render();
  };

  // Highlight the current nav link and open its group (other groups stay as the user left them). A link whose
  // href is exactly this page wins over the page's nav key (/assistant sets nav "find" but has its own link).
  function markNav(nav) {
    let group = null;
    const links = [...document.querySelectorAll(".nav a[data-nav]")];
    const exact = links.find(a => a.getAttribute("href") === location.pathname);
    const cur = exact || links.find(a => a.dataset.nav === nav) || null;
    links.forEach(a => {
      if (a === cur) { a.setAttribute("aria-current", "page"); group = a.closest(".nav-sec"); }
      else a.removeAttribute("aria-current");
    });
    document.querySelectorAll(".nav-sec").forEach(g => {
      const cur = g === group;
      g.classList.toggle("has-cur", cur);
      if (cur) setGroupOpen(g, true);
      else if (!g.dataset.pinned) setGroupOpen(g, false);
    });
    setMenuOpen(false);   // the mobile menu closes on every navigation
  }
  function setGroupOpen(g, open) {
    g.classList.toggle("open", open);
    const b = g.querySelector(".nav-tg");
    if (b) b.setAttribute("aria-expanded", String(open));
  }
  function setMenuOpen(open) {
    document.body.classList.toggle("nav-open", open);
    const b = $("#nav-toggle");
    if (b) { b.setAttribute("aria-expanded", String(open)); b.setAttribute("aria-label", open ? "Close menu" : "Open menu"); }
  }
  function setupNav() {
    document.querySelectorAll(".nav-sec .nav-tg").forEach(b => b.addEventListener("click", () => {
      const g = b.closest(".nav-sec"), open = !g.classList.contains("open");
      setGroupOpen(g, open);
      if (open) g.dataset.pinned = "1"; else delete g.dataset.pinned;
    }));
    const t = $("#nav-toggle");
    if (t) t.addEventListener("click", () => setMenuOpen(!document.body.classList.contains("nav-open")));
    document.addEventListener("keydown", e => { if (e.key === "Escape" && document.body.classList.contains("nav-open")) setMenuOpen(false); });
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
    markNav(nav);
    if (!m) {
      ctx.setTitle("Not found");
      page.append(OG.head("Page not found", "There is no page at " + path),
        h("div", { class: "state empty" }, h("div", { class: "st-body" },
          h("p", {}, "The link may be old or mistyped. Try one of these, or press ", h("kbd", {}, "/"), " to search."),
          h("div", { class: "st-acts" }, h("a", { class: "btn sm pri", href: "/" }, "Home"), h("a", { class: "btn sm", href: "/gpus" }, "GPU prices"), h("a", { class: "btn sm", href: "/find" }, "Find Compute")))));
      return;
    }
    const t = typeof m.route.title === "function" ? m.route.title(m.params, ctx.query) : m.route.title;
    ctx.setTitle(t);
    trackView(path, m.route.pattern, m.params);
    try {
      const ret = m.route.mount(page, m.params, ctx.query, ctx);
      if (typeof ret === "function") ctx._cleanups.push(ret);
      else if (ret && typeof ret.then === "function") ret.catch(e => { console.error(e); if (!ctx._dead) page.append(OG.error(e)); });
    } catch (e) { console.error(e); page.append(OG.error(e)); }
  }
  OG.render = render;

  /* ================= product analytics (anonymous; see makeTracker) ================= */
  let store = null;
  try { store = root.sessionStorage; } catch (e) { store = null; }
  const tracker = makeTracker({
    dnt: (root.navigator && root.navigator.doNotTrack === "1") || root.doNotTrack === "1",
    storage: store,
    send: (batch, o) => api("/v1/events/track", { method: "POST", body: { events: batch }, keepalive: !!o.keepalive }),
  });
  OG.tracker = tracker;
  OG.track = (event, props) => tracker.track(event, props);
  function trackView(path, pattern, params) {
    tracker.track("page_view", { page: path });
    if (pattern === "/gpu/:slug") tracker.track("gpu_view", { gpu: params.slug });
    else if (pattern === "/provider/:name") tracker.track("provider_view", { provider: params.name });
    else if (pattern === "/compare/:a-vs-:b") tracker.track("compare", { a: params.a, b: params.b });
  }

  /* ================= who is this (GET /v1/me, cached) ================= */
  let meP = null;
  OG.me = () => { if (!meP) meP = api.soft("/v1/me", { nocache: true }); return meP; };
  // Operator (site login / open dev) or a key with the admin scope.
  OG.isAdmin = me => !!me && (me.principal === "operator" || (me.scopes || []).includes("*") || (me.scopes || []).includes("admin"));

  // Fetch the server-rendered summary block of a page path (for SPA navigation to SSR-heavy pages)
  OG.fetchSSR = async path => {
    const r = await fetch(path, { credentials: "same-origin", headers: { accept: "text/html" } });
    if (!r.ok) throw new ApiError(r.status, r.status === 404 ? "not found" : "HTTP " + r.status, path);
    const doc = new DOMParser().parseFromString(await r.text(), "text/html");
    const n = doc.getElementById("ssr");
    return n ? document.importNode(n, true) : null;
  };

  /* ================= status bar ================= */
  // "Prices updated 2 min ago" (exact time in the tooltip), redrawn every 30s so the words stay true.
  const statusState = { asOf: null, ok: null, text: null };
  function drawStatus() {
    const dot = $("#live-dot"), txt = $("#live-text"), wrap = $("#live");
    if (!dot) return;
    const f = freshnessText(statusState.asOf, Date.now(), statusState.ok, statusState.text);
    dot.className = "dot " + f.tone;
    txt.textContent = f.text;
    if (wrap) wrap.title = f.title || "When OpenGrid last read provider prices";
  }
  OG.status = {
    asOf(iso) { if (iso) { statusState.asOf = iso; statusState.ok = true; statusState.text = null; } drawStatus(); },
    live(ok, text) { statusState.ok = ok; statusState.text = ok ? null : (text && !/unreachable|offline/i.test(text) ? text : null); drawStatus(); },
    tape(items) { const el = $("#tape"); if (el) { el.replaceChildren(...items); fitTape(); } },
  };
  // Only whole tape items are shown; the rest are counted in "+N more" (links to /gpus). Refits on resize.
  function fitTape() {
    const el = $("#tape");
    if (!el || !el.clientWidth) return;
    const items = [...el.querySelectorAll(".tk")];
    let more = el.querySelector(".tk-more");
    if (more) more.remove();
    items.forEach(i => { i.hidden = false; });
    const gap = parseFloat(getComputedStyle(el).columnGap) || 18;
    const avail = el.clientWidth - (parseFloat(getComputedStyle(el).paddingLeft) || 0);
    const n = tapeFit(items.map(i => i.getBoundingClientRect().width), avail, gap, 78);
    items.forEach((i, k) => { i.hidden = k >= n; });
    if (n < items.length) {
      more = h("a", { class: "tk-more", href: "/gpus", title: "See every GPU price" }, "+" + (items.length - n) + " more");
      el.append(more);
    }
  }
  // When prices were last read from providers (the newest successful fetch), shared for 30 s. Never rejects.
  let readP = null, readT = 0;
  OG.data.lastRead = () => {
    if (!readP || Date.now() - readT > 30000) { readT = Date.now(); readP = api.soft("/v1/trust/providers", { nocache: true }).then(lastReadOf); }
    return readP;
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
      el.replaceChildren(h("span", { class: "st-sep", "aria-hidden": "true" }, "·"), ` ${p.providers.length} providers tracked`);
      el.title = `OpenGrid reads prices from ${p.providers.length} cloud providers, the busiest every ${mins} min${p.running ? "" : " (price checks are paused on this server)"}`;
    }
  }
  // Tape (the same on every page): GPU prices from /market (lowest on-demand $/GPU-hr), then the latest notable
  // moves from /v1/tape. The status bar's "Prices updated" is the last provider read, not the response time.
  const EVENT_WORDS = { sold_out: "sold out", capacity_returned: "back in stock", provider_added_gpu: "now listed", provider_removed_gpu: "delisted",
    new_cheapest_provider: "new cheapest", new_30d_low: "30-day low", new_30d_high: "30-day high", new_all_time_low: "all-time low", new_all_time_high: "all-time high" };
  async function loadTape() {
    const [m, v1, read] = await Promise.all([OG.data.market(24).catch(() => null), api.soft("/v1/tape"), OG.data.lastRead()]);
    if (!m) { OG.status.live(false); return; }
    if (read) OG.status.asOf(read); else OG.status.live(true);
    const top = m.gpus.filter(g => g.providers >= 2).slice(0, 16);
    const prices = top.map(g => h("a", { class: "tk", href: "/gpu/" + slug(g.gpu), title: `${g.gpu}: lowest on-demand price per GPU-hour across ${g.providers} providers, change over 24 hours` },
      h("b", {}, L.shortGpu(g.gpu)), " ", fmt.price(g.lowest), " ", OG.chg(g.change_pct)));
    const events = (Array.isArray(v1) ? v1 : []).slice(0, 12).map(e => h("a", {
      class: "tk tk-ev", href: e.gpu_slug ? "/gpu/" + e.gpu_slug : "/events", title: e.title || "",
    }, h("b", {}, e.provider ? L.providerName(e.provider) : "Market"), " ", e.gpu ? L.shortGpu(e.gpu) : "", " ",
      e.pct != null ? OG.chg(e.pct) : h("span", { class: "dim" }, EVENT_WORDS[e.type] || String(e.type || "event").replace(/_/g, " "))));
    OG.status.tape([...prices, ...events]);
  }

  /* ================= command bar ================= */
  // Every nav link, primary or secondary (collapsed groups stay in the DOM), plus its search aliases.
  function navItems() {
    return [...document.querySelectorAll(".nav a[data-nav]")].filter(a => !a.closest("[hidden]"))
      .map(a => ({ label: a.textContent.trim(), href: a.getAttribute("href"), alias: a.dataset.alias || "", lock: !!a.dataset.access && !OG.signedIn() }));
  }
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
    // the main places by name (also reachable by typing part of the name: page suggestions)
    for (const [word, href, label] of [["home", "/", "home page"], ["gpus", "/gpus", "every GPU price"], ["find", "/find", "Find Compute for a project"],
      ["markets", "/markets", "market overview"], ["developers", "/developers", "connect an AI assistant, CLI or API"]])
      command(new RegExp("^" + word + "$", "i"), () => go(href), `${word.padEnd(16)} ${label}`);
    command(/^ask\s+(.+)$/i, m => go("/assistant?q=" + encodeURIComponent(m[1].trim())), "ask <question>   ask the assistant in plain English");
    command(/^theme(?:\s+(light|dark))?$/i, m => OG.setTheme(m[1] ? m[1].toLowerCase() : OG.theme() === "light" ? "dark" : "light"), "theme [light|dark]  switch colour theme");
    command(/^\?$|^help$/i, () => showHelp(), "?                this help");
  }

  // The places a first-time visitor needs, shown when the bar is empty (all clickable).
  const START = [["Home", "/"], ["GPUs: every price", "/gpus"], ["Find Compute for my project", "/find"], ["Ask the assistant", "/assistant"],
    ["Markets", "/markets"], ["Developers", "/developers"], ["My Jobs", "/jobs", true]];
  const LOCKED = "invite only";
  const EXAMPLES = [["h100", "open the H100 market"], ["c h100 h200", "compare two GPUs"], ["ask cheapest gpu for fine-tuning llama 8b", "ask in plain English"]];
  async function suggestions(text) {
    const t = text.trim();
    if (!t) {
      return [
        ...START.map(([label, href, lock], i) => ({ group: i ? null : "Go to", label, hint: lock ? LOCKED : "page", lock: !!lock, run: () => OG.go(href) })),
        ...EXAMPLES.map(([q, hint], i) => ({ group: i ? null : "Try typing", label: q, hint, code: true, insert: q })),
      ];
    }
    const out = [];
    const hit = findCommand(t);
    if (hit) out.push({ label: "↵ " + t, hint: commandParts(hit.cmd.usage).desc || "command", run: () => hit.cmd.handler(hit.match, t) });
    const q = t.replace(/^[gpc]\s+/i, "");
    const [gpus, provs, fams] = [await gpusForSearch(), OG.data.providers(), await OG.data.families()];
    for (const n of fuzzy(t, navItems(), n => [n.label, n.alias].filter(Boolean)).slice(0, 3)) out.push({ label: n.label, hint: n.lock ? "page · " + LOCKED : "page", lock: n.lock, run: () => OG.go(n.href) });
    for (const f of fuzzy(q, fams, f => [f.slug, f.id, f.name].filter(Boolean)).slice(0, 2)) {
      const n = Array.isArray(f.variants) ? f.variants.length : Number(f.variant_count ?? f.variants_count) || null;
      out.push({ label: f.name || f.slug, hint: "GPU family" + (n ? ` · ${n} variant${n === 1 ? "" : "s"}` : ""), run: () => OG.go("/gpu/" + (f.slug || f.id)) });
    }
    for (const g of fuzzy(q, gpus, g => [g.slug, g.short, g.name]).slice(0, 6)) out.push({ label: g.short, hint: g.live ? `GPU · ${g.weight} provider${g.weight === 1 ? "" : "s"}` : "GPU · none priced now", run: () => OG.go("/gpu/" + g.slug) });
    for (const p of fuzzy(q, provs, p => [p.name, p.display_name]).slice(0, 4)) out.push({ label: p.display_name, hint: "provider", run: () => OG.go("/provider/" + p.name) });
    // commands whose word starts with what was typed ("ma" -> markets); clicking one with an argument fills it in
    if (!hit) for (const c of COMMANDS) {
      const cp = commandParts(c.usage);
      if (t.length >= 2 && cp.syntax.toLowerCase().startsWith(t.toLowerCase()) && out.length < 11)
        out.push({ label: cp.syntax, hint: cp.desc, code: true, insert: cp.insert, run: cp.insert ? null : () => { const x = findCommand(cp.syntax); if (x) return x.cmd.handler(x.match, cp.syntax); } });
    }
    // more than one word and nothing exact: offer the assistant
    if (!hit && /\s/.test(t) && !/^[gpcr]\s/i.test(t)) out.push({ label: "Ask the assistant: \u201c" + t.slice(0, 60) + "\u201d", hint: "plain English", run: () => OG.go("/assistant?q=" + encodeURIComponent(t)) });
    return out.slice(0, 12);
  }
  function setupCommandBar() {
    const input = $("#cmd"), box = $("#cmd-sug");
    if (!input) return;
    let items = [], sel = 0, seq = 0;
    const close = () => { box.hidden = true; items = []; input.setAttribute("aria-expanded", "false"); input.removeAttribute("aria-activedescendant"); };
    const draw = () => {
      const rows = [];
      items.forEach((it, i) => {
        if (it.group) rows.push(h("div", { class: "sug-g", role: "presentation" }, it.group));
        rows.push(h("div", { class: "sug" + (i === sel ? " on" : ""), id: "sug-" + i, role: "option", "aria-selected": String(i === sel), onmousedown: e => { e.preventDefault(); exec(i); } },
          h("span", { class: "sug-l" }, it.lock ? h("i", { class: "lock", "aria-hidden": "true" }) : null, it.lock ? " " : null, it.code ? h("code", {}, it.label) : it.label),
          h("span", { class: "sug-h" }, it.hint)));
      });
      if (items.length && !input.value.trim()) rows.push(h("div", { class: "sug-foot" }, "Type a GPU, provider or page. Press ", h("kbd", {}, "?"), " for every shortcut."));
      box.replaceChildren(...rows);
      box.hidden = !items.length;
      input.setAttribute("aria-expanded", String(!!items.length));
      if (items.length) input.setAttribute("aria-activedescendant", "sug-" + sel); else input.removeAttribute("aria-activedescendant");
    };
    let itemsFor = null;   // the text `items` were computed for: Enter on newer text recomputes first (no stale pick)
    const refresh = async () => { const my = ++seq, text = input.value; const r = await suggestions(text); if (my !== seq) return; items = r; itemsFor = text; sel = 0; draw(); };
    async function exec(i) {
      const it = items[i];
      const q = input.value.trim();
      if (it && !it.run && it.insert) { input.value = it.insert; input.focus(); refresh(); return; }   // fill in, let them finish typing
      if (q) tracker.track("search", { q: q.slice(0, 100) });
      if (it && it.run) { input.value = ""; close(); input.blur(); await it.run(); return; }
      const t = input.value.trim(), hit = findCommand(t);
      if (hit) { input.value = ""; close(); input.blur(); await hit.cmd.handler(hit.match, t); }
    }
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-expanded", "false");
    input.addEventListener("input", refresh);
    input.addEventListener("focus", refresh);
    input.addEventListener("blur", () => setTimeout(close, 100));
    input.addEventListener("keydown", e => {
      if (e.key === "ArrowDown") { e.preventDefault(); sel = Math.min(items.length - 1, sel + 1); draw(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); sel = Math.max(0, sel - 1); draw(); }
      else if (e.key === "Enter") {
        e.preventDefault();
        if (itemsFor !== input.value) refresh().then(() => exec(items.length ? sel : -1));
        else exec(items.length ? sel : -1);
      }
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
    if (!hp) { hp = h("div", { id: "help", class: "help", role: "dialog", "aria-modal": "true", "aria-label": "Keyboard shortcuts", onclick: e => { if (e.target === hp) hp.hidden = true; } }); document.body.append(hp); }
    const row = (k, d) => h("tr", {}, h("td", {}, k), h("td", {}, d));
    const close = h("button", { class: "btn sm", type: "button", onclick: () => { hp.hidden = true; } }, "Close");
    const parts = COMMANDS.map(c => commandParts(c.usage));
    const withArg = parts.filter(cp => /[<[]/.test(cp.syntax)), noArg = parts.filter(cp => !/[<[]/.test(cp.syntax) && cp.syntax !== "?").sort((a, b) => a.syntax.localeCompare(b.syntax));
    hp.replaceChildren(h("div", { class: "help-box" },
      h("div", { class: "help-h" }, h("b", {}, "Search and shortcuts"), h("span", { class: "dim" }, "Esc to close")),
      h("table", { class: "help-t" }, h("tbody", {},
        h("tr", { class: "hg" }, h("td", { colspan: "2" }, "Keys")),
        row("/", "search: type a GPU, provider or page name"), row("?", "this list"), row("↑ ↓ ↵", "pick a suggestion"), row("Esc", "close"),
        h("tr", { class: "hg" }, h("td", { colspan: "2" }, "Find something (type in the search box)")),
        ...withArg.map(cp => row(cp.syntax, cp.desc)),
        h("tr", { class: "hg" }, h("td", { colspan: "2" }, "Jump to a page")),
        ...noArg.map(cp => row(cp.syntax, cp.desc)))),
      close));
    hp.hidden = false;
    close.focus({ preventScroll: true });
  }

  /* ================= theme ================= */
  // index.html sets <html data-theme> before first paint (saved choice, else the OS preference); this switches it
  OG.theme = () => (document.documentElement.dataset.theme === "light" ? "light" : "dark");
  OG.setTheme = t => {
    document.documentElement.dataset.theme = t;
    try { localStorage.setItem("og-theme", t); } catch (e) { /* private mode: the choice lasts this page only */ }
    drawThemeToggle();
    root.dispatchEvent(new Event("og-theme"));
  };
  function drawThemeToggle() {
    const b = $("#theme-toggle");
    if (!b) return;
    const label = OG.theme() === "light" ? "Switch to dark mode" : "Switch to light mode";
    b.setAttribute("aria-label", label); b.title = label;
  }

  /* ================= access indicator ================= */
  // "Public access" for a visitor, "Operator" after the site login. The browser login is OpenGrid's operator
  // login, not a customer account (customers use API keys), so the popover never offers a customer sign-in.
  function drawAccess(me) {
    const wrap = $("#acc"), btn = $("#acc-b"), t = $("#acc-t"), pop = $("#acc-p");
    if (!wrap || !btn || !pop) return;
    const op = OG.isAdmin(me), signed = !!me;
    document.body.classList.toggle("og-signed-in", signed);
    wrap.classList.toggle("op", op);
    t.textContent = op ? "Operator" : signed ? "Signed in" : (root.matchMedia && root.matchMedia("(max-width: 560px)").matches ? "Public" : "Public access");
    btn.title = op ? "Signed in with the OpenGrid site login" : signed ? "Signed in" : "You are browsing as a visitor: what you can use";
    const li = (k, v) => h("li", {}, h("b", {}, k), h("span", {}, v));
    pop.replaceChildren(...(op || signed ? [
      h("h2", {}, op ? "Signed in as operator" : "Signed in"),
      h("p", {}, op ? "You are using the OpenGrid site login: every page is open to you, including the Operator tools at the bottom of the menu." : "Your account's pages are open to you."),
      h("p", { class: "dim" }, "To sign out, close this browser window: the site login is remembered until then."),
    ] : [
      h("h2", {}, "You are browsing as a visitor"),
      h("ul", {},
        li("Free", "GPU prices, comparisons, market data, Find Compute and the assistant. No account needed."),
        li("Invite only", "My Jobs (including the free simulated demo), API keys and watchlists (pages with a lock). The OpenGrid team issues accounts and API keys; there is no self-serve sign-up.")),
      h("div", { class: "acc-act" },
        h("a", { class: "btn sm pri", href: "/developers#access" }, "How to get an API key")),
      h("p", { class: "note" }, "OpenGrid staff: ", h("a", { class: "lnk", href: OG.loginHref() }, "sign in")),
    ]));
  }
  function setupAccess() {
    const btn = $("#acc-b"), pop = $("#acc-p");
    if (!btn || !pop) return;
    const set = open => { pop.hidden = !open; btn.setAttribute("aria-expanded", String(open)); };
    btn.addEventListener("click", e => { e.stopPropagation(); set(pop.hidden); });
    document.addEventListener("click", e => { if (!pop.hidden && !e.target.closest("#acc")) set(false); });
    document.addEventListener("keydown", e => { if (e.key === "Escape" && !pop.hidden) { set(false); btn.focus(); } });
    pop.addEventListener("click", e => { if (e.target.closest("a")) set(false); });
    drawAccess(null);
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
    setupNav();
    const tt = $("#theme-toggle");
    if (tt) tt.addEventListener("click", () => OG.setTheme(OG.theme() === "light" ? "dark" : "light"));
    drawThemeToggle();
    setupAccess();
    OG.data.intervals();
    render();
    loadPolling(); loadTape();
    setInterval(() => { if (!document.hidden) loadTape(); }, 60000);
    setInterval(() => { if (!document.hidden) drawStatus(); }, 30000);
    let fitT = null;
    root.addEventListener("resize", () => { clearTimeout(fitT); fitT = setTimeout(fitTape, 120); });
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(fitTape);
    setInterval(() => tracker.flush(), 10000);
    root.addEventListener("pagehide", () => tracker.flush({ keepalive: true }));
    // Admin nav section: only for the operator / admin-scoped principals (the API enforces it regardless)
    OG.me().then(me => { document.querySelectorAll(".nav-admin").forEach(n => { n.hidden = !OG.isAdmin(me); }); drawAccess(me); });
  };
  return OG;
});
