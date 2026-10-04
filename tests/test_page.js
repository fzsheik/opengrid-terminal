// Page logic against a fake DOM. Run: node tests/test_page.js
// Checks the change arrows, the time-frame selector, sorting and out-of-order responses.
const fs = require("fs");
const path = require("path");
const html = fs.readFileSync(path.join(__dirname, "../web/classic.html"), "utf8");
const script = html.match(/<script>([\s\S]*)<\/script>/)[1];

let failures = 0;
const ok = (cond, msg) => { if (!cond) { failures++; console.log("FAIL:", msg); } else console.log("ok  :", msg); };

function makeEnv(listings, changesFor) {
  class El {
    constructor(tag) { this.tag = tag; this.children = []; this._text = ""; this.className = ""; this.style = {}; this.dataset = {};
      this.checked = false; this.value = ""; this.options = []; this.title = ""; this.handlers = {};
      this.classList = { toggle: (c, on) => { const s = new Set(this.className.split(" ").filter(Boolean)); (on ? s.add(c) : s.delete(c)); this.className = [...s].join(" "); } }; }
    set textContent(v) { this._text = String(v); this.children = []; }
    get textContent() { return this._text + this.children.map(c => c.textContent).join(""); }
    append(...c) { this.children.push(...c.map(x => typeof x === "string" ? { textContent: x, tag: "#text", children: [] } : x)); }
    addEventListener(t, f) { (this.handlers[t] ||= []).push(f); }
    add(o) { this.options.push(o); }
    fire(t, ev) { (this.handlers[t] || []).forEach(f => f(ev)); }
  }
  const reg = {};
  const get = s => reg[s] ??= new El(s);
  const fetches = [];
  const document = {
    querySelector: get,
    querySelectorAll: s => { const m = s.match(/^#(\w+) (\w+)$/); return m ? get("#" + m[1]).children.filter(c => c.tag === m[2]) : []; },
    createElement: t => new El(t),
    createTextNode: t => ({ textContent: t, tag: "#text", children: [] }),
  };
  const ctx = {
    document, localStorage: { getItem: () => null, setItem() {} }, Option: class { constructor(t, v) { this.value = v; } },
    setInterval: () => {}, Number, Math, Date, Map, Set, Promise, console,
    fetch: url => {
      fetches.push(url);
      if (url === "/listings") return Promise.resolve({ ok: true, json: async () => listings });
      const h = Number(new URL(url, "http://x").searchParams.get("hours"));
      return changesFor(h);
    },
  };
  return { ctx, get, fetches, El };
}

const iso = (minAgo) => new Date(Date.now() - minAgo * 60000).toISOString();
const row = (provider, id, price, extra = {}) => ({
  provider, listing_id: id, raw_gpu_name: "x", canonical_gpu_name: "NVIDIA H100 80GB SXM5", gpu_count: 1,
  price_per_gpu_hour: price, price_per_instance_hour: price, region: null, country: null, market_type: "on_demand",
  provider_tier: null, interruptible: false, available: true, observed_at: iso(2), ...extra,
});
const item = (provider, id, status, now, then, pct) => ({ provider, listing_id: id, status, price_now: now, price_then: then, pct, then_at: iso(300) });

const listings = [
  row("a", "down", 2.0), row("b", "up", 3.0), row("c", "flat", 2.5), row("d", "new", 4.0), row("e", "nodata", 5.0),
  row("f", "stale-move", 1.0, { observed_at: iso(600) }),     // not seen for 10h: its arrow must not show
  row("g", "unlisted", 6.0),                                  // no entry in /changes at all
];
const payload = (h) => ({ hours: h, cutoff: iso(h * 60), tracking_since: iso(100000), items: [
  item("a", "down", "down", 2.0, 2.5, -0.2), item("b", "up", "up", 3.0, 2.0, 0.5), item("c", "flat", "flat", 2.5, 2.5, 0.0),
  item("d", "new", "new", 4.0, null, null), item("e", "nodata", "nodata", 5.0, null, null),
  item("f", "stale-move", "up", 1.0, 0.5, 1.0),
]});

(async () => {
  const env = makeEnv(listings, async (h) => ({ ok: true, json: async () => payload(h) }));
  const vm = require("vm");
  vm.createContext(env.ctx);
  vm.runInContext(script + "\n;globalThis.__t = { renderAll, load, get windowH() { return windowH; }, get changes() { return changes; } };", env.ctx);
  const t = env.ctx.__t, $ = env.get;
  await new Promise(r => setTimeout(r, 20));

  const rowsOf = () => $("#listings tbody").children;
  const cellsOf = tr => tr.children.map(c => c.textContent);
  // Listing columns: GPU, provider, GPUs, $/GPU-hr, change, $/instance, region, stock, type, seen
  const byProv = {}; for (const tr of rowsOf()) byProv[cellsOf(tr)[1]] = tr;
  const chgCell = p => byProv[p].children[4];

  ok(chgCell("a").textContent.startsWith("▼ 20%"), "price down shows ▼ 20%: " + chgCell("a").textContent);
  ok(chgCell("a").children[0].className.includes("chg-down"), "down arrow is the green class");
  ok(chgCell("b").textContent.startsWith("▲ 50%"), "price up shows ▲ 50%: " + chgCell("b").textContent);
  ok(chgCell("b").children[0].className.includes("chg-up"), "up arrow is the red class");
  ok(chgCell("c").textContent === "·", "flat shows a dim dot");
  ok(chgCell("d").textContent === "new", "newly listed shows 'new'");
  ok(chgCell("e").textContent === "–", "no history shows a dash");
  ok(chgCell("f").textContent === "", "stale listing shows no arrow even though /changes says up");
  ok(chgCell("g").textContent === "", "listing missing from /changes shows nothing, not a wrong arrow");
  ok(chgCell("a").children[0].title.includes("2.500") && chgCell("a").children[0].title.includes("2.000"), "tooltip names old and new price");

  const sum = $("#chgsum").textContent;
  ok(sum.startsWith("vs 24h ago: 1 cheaper ▼ · 1 pricier ▲ · 1 unchanged · 1 new · 1 without history"), "summary counts exclude the stale row: " + sum);
  ok($("#chg-head").textContent === "vs 24h ago", "column header names the window");
  ok($("#win").children.filter(c => c.tag === "button").length === 6, "six window buttons");
  ok($("#win").children.filter(c => c.tag === "button" && c.className.includes("on")).map(b => b.textContent).join() === "24h", "24h is the active button");

  // Charts: arrows only on up/down, and only for the displayed listing
  const chartRows = [];
  for (const c of $("#charts").children) for (const r of c.children.slice(1)) chartRows.push(r.children.map(x => x.textContent));
  const val = p => (chartRows.find(r => r[0] === p) || [])[2];
  ok(val("a") && val("a").includes("▼"), "chart shows ▼ beside a's price: " + val("a"));
  ok(val("b") && val("b").includes("▲"), "chart shows ▲ beside b's price: " + val("b"));
  ok(val("c") === "$2.50", "chart shows no mark for flat: " + val("c"));
  ok(val("d") === "$4.00" && val("e") === "$5.00", "chart shows no mark for new / no history");

  // Sorting by change: most negative first, empty cells last in BOTH directions
  const listHead = $("#listings thead");
  const click = k => listHead.fire("click", { target: { dataset: { k } } });
  click("chg");
  let order = rowsOf().map(tr => cellsOf(tr)[1]);
  ok(order.slice(0, 3).join() === "a,c,b", "sort by change ascending: cheaper first: " + order.join());
  click("chg");
  order = rowsOf().map(tr => cellsOf(tr)[1]);
  ok(order.slice(0, 3).join() === "b,c,a", "sort descending: pricier first: " + order.join());
  ok(["d", "e", "f", "g"].every(p => order.slice(3).includes(p)), "rows without a comparison stay at the bottom");

  // Switching window: asks for the right hours, drops the old arrows at once
  const winBtn = label => $("#win").children.find(c => c.tag === "button" && c.textContent === label);
  const clickWin = label => $("#win").fire("click", { target: winBtn(label) });
  env.fetches.length = 0;
  let release;
  const gate = new Promise(r => (release = r));
  // 6h answers only after 7d does: 7d is the later click, so it must win
  env.ctx.fetch = url => {
    env.fetches.push(url);
    if (url === "/listings") return Promise.resolve({ ok: true, json: async () => listings });
    const h = Number(new URL(url, "http://x").searchParams.get("hours"));
    if (h === 6) return gate.then(() => ({ ok: true, json: async () => payload(6) }));
    return Promise.resolve({ ok: true, json: async () => payload(h) });
  };
  clickWin("6h");
  ok(t.windowH === 6, "clicking 6h selects it");
  ok($("#chgsum").textContent === "price changes unavailable", "old arrows cleared the moment the window changes");
  const fresh = {}; for (const tr of rowsOf()) fresh[cellsOf(tr)[1]] = tr;     // rows were re-rendered
  ok(fresh["a"].children[4].textContent === "", "listings show no arrow while the new window loads");
  clickWin("7d");
  await new Promise(r => setTimeout(r, 20));
  ok(env.fetches.some(u => u === "/changes?hours=6") && env.fetches.some(u => u === "/changes?hours=168"), "requests carry hours=6 and hours=168: " + env.fetches.join(" "));
  ok($("#chgsum").textContent.startsWith("vs 7d ago"), "7d result shown: " + $("#chgsum").textContent.slice(0, 30));
  release();
  await new Promise(r => setTimeout(r, 20));
  ok($("#chgsum").textContent.startsWith("vs 7d ago"), "a late 6h answer does not overwrite the 7d view");
  ok(t.changes.get("b\u0000up") && t.windowH === 168, "state still on 7d");

  clickWin("30m");
  await new Promise(r => setTimeout(r, 20));
  ok(env.fetches.includes("/changes?hours=0.5"), "30m requests hours=0.5");

  // /changes down: the page still works, says so, and shows no arrows
  env.ctx.fetch = url => url === "/listings" ? Promise.resolve({ ok: true, json: async () => listings }) : Promise.resolve({ ok: false, status: 500 });
  await t.load();
  ok($("#chgsum").textContent === "price changes unavailable" && rowsOf().length === listings.length, "listings still render when /changes fails");

  console.log(failures ? `\n${failures} FAILED` : "\nall passed");
  process.exit(failures ? 1 : 0);
})();
