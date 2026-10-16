/* / (homepage) for a first-time visitor: what OpenGrid is (headline), what works today (and what does not), three
   actions that each say what they need (free / needs an account / simulation), a compact live board of the
   most-covered GPUs and how OpenGrid works. A visitor who picks the demo gets the account explanation in place,
   never a sign-in error; a signed-in operator goes straight to /jobs/new.

   Data (all real, none invented): /v1/overview (board: lowest price, cheapest provider, provider count, matched
   24h change; capacity), /market?hours=24 (sparklines, shared with the tape), /v1/indices (soft), /polling (how
   many providers are polled, via OG.data.intervals()). Every block has its own loading / empty / error state.
   The full market dashboard that used to live here is /markets (pages/overview.js). */
(() => {
  const { h, fmt, Lib: L } = OG;
  const DEMO = "/jobs/new?preset=mnist&sandbox=1";
  const HOME_INDICES = ["gpu-compute", "h100-80gb-sxm5", "h200-141gb-sxm5", "a100-80gb-sxm4"];
  const BOARD_ROWS = 10;

  // Popular = the GPUs most providers sell now (ties: cheapest first); GPUs nobody prices are left out.
  function popular(items, n) {
    return (items || []).filter(b => b.lowest != null && b.providers > 0)
      .sort((a, b) => (b.providers - a.providers) || (a.lowest - b.lowest)).slice(0, n || BOARD_ROWS);
  }
  OG.home = { popular, DEMO };
  if (typeof OG.page !== "function" || typeof document === "undefined") return;   // node tests: pure part only

  function board() {
    return OG.table({
      columns: [
        { key: "gpu", label: "GPU", fmt: v => h("b", {}, L.shortGpu(v)), value: r => L.shortGpu(r.gpu), href: r => "/gpu/" + r.gpu_slug },
        { key: "lowest", label: "Low $/GPU·h", num: true, fmt: v => h("b", {}, fmt.price(v)), title: "lowest observed on-demand price per GPU-hour now" },
        { key: "lowest_provider", label: "Cheapest at", fmt: v => OG.providerLink(v) },
        { key: "providers", label: "Prov", num: true, desc: true, title: "providers with purchasable listings now" },
        { key: "change_24h_median", label: "24h", num: true, desc: true, title: "matched 24h change of the median: providers priced at both times only",
          fmt: (v, r) => OG.chg(v, { reason: r.providers_matched < 2 ? `needs >= 2 providers priced 24h ago and now (${r.providers_matched || 0} matched)` : "not enough history" }) },
        { key: "spark", label: "24h low", sort: false, csv: false, title: "lowest price over the last 24h (/market)",
          fmt: (v, r) => (r.spark ? OG.charts.sparkline(r.spark, { dir: fmt.dir(r.spark_chg), width: 84, height: 16 }) : h("span", { class: "dimmer" }, "-")) },
      ],
      rows: [], sort: { key: "providers", dir: "desc" }, compact: true, csv: false, limit: BOARD_ROWS,
      rowKey: r => r.gpu, rowHref: r => "/gpu/" + r.gpu_slug, empty: "No GPU is priced right now.",
    });
  }

  function indexRow(ix) {
    const usd = ix.unit === "usd_per_gpu_hour";
    const lvl = ix.level == null ? null : usd ? fmt.price(ix.level) : fmt.num(ix.level, 2);
    const name = String(ix.name || ix.id).replace(/^OpenGrid\s+/, "").replace(/\s+Index$/, "");
    const ch = ix.changes && ix.changes["24h"] != null ? OG.chg(ix.changes["24h"]) : OG.na((ix.change_reasons || {})["24h"] || ix.reason || "unavailable");
    return h("a", { class: "hm-ix", href: "/indices/" + encodeURIComponent(ix.id), title: ix.name + (ix.reason ? "\n" + ix.reason : "") },
      h("span", { class: "hm-ix-n" }, name),
      h("span", { class: "hm-ix-v mono" }, lvl == null ? h("span", { class: "dimmer" }, "not published") : [lvl, h("span", { class: "dimmer" }, usd ? "/GPU·h" : " pts")]),
      h("span", { class: "hm-ix-c mono" }, lvl == null ? "" : ch));
  }

  const step = (n, title, kind, body, link) => h("li", { class: "hm-step" },
    h("div", { class: "hm-step-h" }, h("span", { class: "hm-step-n mono" }, n), h("b", {}, title), kind),
    h("p", {}, body), link ? h("a", { class: "lnk hm-step-a", href: link[0] }, link[1] + " →") : null);

  OG.page("/", {
    title: "Find and run GPU compute",
    nav: "home",
    mount(el, params, query, ctx) {
      const root = h("div", { class: "pg-home" });
      el.append(root);

      const provN = h("span", {}, "GPU cloud providers");
      const stats = h("div", { class: "hm-stats" }, OG.loading("Loading live market…"));
      const tbl = board();
      const boardBox = h("div", { class: "hm-board-b" }, OG.loading("Loading prices…"));
      const asof = h("span", { class: "mono dimmer hm-asof" });
      const ixBox = h("div", { class: "hm-ixs" }, OG.loading("Loading indices…"));
      const observeN = h("span", {}, "every tracked provider");

      // Each action says up front what it needs: free and open, or an account / a simulation.
      const tag = (cls, text, lock) => h("span", { class: "hm-tag " + cls }, lock ? h("i", { class: "lock", "aria-hidden": "true" }) : null, text);
      const action = (el, title, line, tags, pri) => {
        el.className = "hm-act" + (pri ? " pri" : "");
        el.append(h("span", { class: "hm-act-t" }, title, h("span", { class: "hm-act-arr", "aria-hidden": "true" }, "→")),
          h("span", { class: "hm-act-l" }, line), h("span", { class: "hm-tags" }, tags));
        return el;
      };
      // The demo runs in a workspace, which needs an account: a visitor gets the explanation here instead of a
      // sign-in error one click later; a signed-in operator goes straight to it.
      const demoWhy = h("div", { class: "hm-why", id: "hm-demo-why", hidden: true },
        h("b", {}, "The demo needs an OpenGrid account"),
        h("p", {}, "It runs in your own workspace on OpenGrid, so it needs an account. Accounts are invite-only for now; there is no self-serve sign-up yet."),
        h("p", {}, "What it does: a small PyTorch model learns to read handwritten digits (MNIST) for a few rounds. It is a simulation: no GPU is rented and nothing is charged."),
        h("div", { class: "hm-why-a" },
          h("a", { class: "lnk", href: "/methodology/jobs" }, "How jobs work →"),
          h("a", { class: "lnk", href: "/developers" }, "Have an API key? Run it from your AI assistant →")));
      const demoBtn = h("button", { type: "button", "aria-expanded": "false", "aria-controls": "hm-demo-why",
        onclick: () => { demoWhy.hidden = !demoWhy.hidden; demoBtn.setAttribute("aria-expanded", String(!demoWhy.hidden)); } });
      const demoTags = () => [tag("sim", "Simulation · free"), OG.signedIn() ? null : tag("acct", "Needs an account", true)];
      const demoAct = action(demoBtn, "Try the Free Training Demo", "Watch a small AI model train, step by step.", demoTags());
      OG.me().then(me => {
        if (!me || !ctx.alive()) return;
        const a = action(h("a", { href: DEMO }), "Try the Free Training Demo", "Watch a small AI model train, step by step.", demoTags());
        demoAct.replaceWith(a); demoWhy.remove();
      });
      const today = (state, what, who) => h("li", { class: "hm-today-" + state }, h("span", { class: "hm-today-i", "aria-hidden": "true" }), h("span", {}, what), h("span", { class: "hm-today-w" }, who));

      root.append(
        h("section", { class: "hm-hero" },
          h("div", { class: "hm-hero-l" },
            h("div", { class: "eyebrow" }, "OpenGrid · live GPU cloud prices"),
            h("h1", { class: "hm-h1" }, "Compare GPU prices. Find the right compute. Run supported workloads."),
            h("p", { class: "hm-sub" }, "OpenGrid reads the public price lists of ", provN, " every few minutes. See what an hour of GPU time costs everywhere, find the right GPU for your AI project, and run supported jobs safely."),
            h("div", { class: "hm-today", "aria-label": "What works today" },
              h("div", { class: "eyebrow" }, "What works today"),
              h("ul", {},
                today("yes", "Live GPU prices and comparisons", "free, no account"),
                today("yes", "GPU recommendations for your project", "free, no account"),
                today("part", "Training demo (a simulation, no charge)", "needs an account"),
                today("part", "Real GPU runs, each approved by OpenGrid", "early access"),
                today("no", "Running your own code", "not yet")))),
          h("div", { class: "hm-acts" },
            action(h("a", { href: "/gpus" }), "Explore GPU Prices", "Every GPU, cheapest provider first, with price history.", [tag("free", "Free · no account")], true),
            action(h("a", { href: "/find" }), "Find Compute for My Project", "Describe your model and job; get the GPUs that fit, ranked by price.", [tag("free", "Free · no account")]),
            demoAct, demoWhy)),
        stats,
        h("div", { class: "hm-mkt" },
          h("section", { class: "hm-board" },
            h("header", { class: "hm-sh" }, h("h2", { class: "hm-h2" }, "GPU prices right now"), OG.kindBadge("observed"), asof,
              h("span", { class: "spacer" }), h("a", { class: "lnk hm-more", href: "/gpus" }, "All GPUs →"), h("a", { class: "lnk hm-more", href: "/markets" }, "Market dashboard →")),
            boardBox),
          h("section", { class: "hm-side" },
            h("header", { class: "hm-sh" }, h("h2", { class: "hm-h2" }, "Price indices"), h("span", { class: "spacer" }), h("a", { class: "lnk hm-more", href: "/indices" }, "All indices →")),
            ixBox,
            h("p", { class: "note" }, "An index is one number for the price of a GPU type across providers, like a stock index. These are list prices per GPU-hour, not quotes. ", h("a", { class: "lnk", href: "/methodology/data-kinds" }, "Data kinds"), " · ", h("a", { class: "lnk", href: "/methodology/indices" }, "Index methodology")))),
        h("section", { class: "hm-how" },
          h("h2", { class: "hm-h2" }, "How OpenGrid works"),
          h("ol", { class: "hm-steps" },
            step("01", ["Watch ", observeN], OG.kindBadge("observed"),
              "Every few minutes OpenGrid reads each provider's public prices and what is in stock. Every number shows where it came from and how old it is. When there is not enough data, it says n/a and why, instead of guessing.",
              ["/providers", "Providers and coverage"]),
            step("02", "Recommend for your project", OG.kindBadge("estimated"),
              "Tell it about your model and job. OpenGrid estimates how much GPU memory you need and lists the GPUs that fit, cheapest first. Estimates are always labelled as estimates.",
              ["/find", "Find Compute"]),
            step("03", "Run safely", h("span", { class: "badge warn" }, "approved"),
              "A price check is free and never rents anything. A real run starts only after OpenGrid approves the price, with a spending cap and proof the GPU was shut down afterwards. Today the one supported job is a small training example.",
              ["/methodology/execution-safety", "How runs are kept safe"]))),
        h("nav", { class: "hm-more-row", "aria-label": "More" },
          h("span", { class: "eyebrow" }, "Also"),
          [["/developers", "Use OpenGrid from your AI assistant, CLI or API"], ["/compare", "Compare two GPUs"], ["/events", "Market events"], ["/methodology", "How every number is computed"]]
            .map(([href, label]) => h("a", { class: "lnk", href }, label))));

      let spark = new Map(), last = null, lastMeta = null, polled = null;
      function drawBoard() {
        if (!last) return;
        const rows = popular(last.board && last.board.items).map(b => {
          const s = spark.get(b.gpu);
          return Object.assign({}, b, s ? { spark: s.spark, spark_chg: s.change_pct } : {});
        });
        tbl.update(rows);
        if (boardBox.firstChild !== tbl) boardBox.replaceChildren(tbl);
      }
      function drawStats(o, meta) {
        const c = o.capacity || {};
        const items = (o.board && o.board.items) || [];
        const priced = items.filter(b => b.lowest != null);
        const cheapest = priced.slice().sort((a, b) => a.lowest - b.lowest)[0];
        stats.replaceChildren(OG.stats([
          { label: "GPUs priced", value: priced.length ? fmt.num(priced.length) : null, reason: "no GPU is priced right now", sub: "on-demand, in stock or stock unknown" },
          { label: "Listings you can rent now", value: c.available === false ? null : fmt.num(c.available_listings_now), reason: c.reason || "capacity unavailable", kind: "observed" },
          { label: "Cheapest GPU-hour", value: cheapest ? fmt.price(cheapest.lowest) : null, reason: "no GPU is priced right now",
            sub: cheapest ? L.shortGpu(cheapest.gpu) + " at " + L.providerName(cheapest.lowest_provider) : null },
          { label: "Sold out everywhere", value: c.available === false ? null : fmt.num(c.gpus_sold_out_everywhere), reason: c.reason || "capacity unavailable", sub: "GPUs with listings, none purchasable" },
          { label: "Providers tracked", value: polled ? fmt.num(polled) : null, reason: "the provider list did not load", sub: "public prices and stock" },
        ]));
      }
      async function loadOverview() {
        let r;
        try { r = await ctx.api("/v1/overview", { full: true, nocache: true }); }
        catch (e) {
          boardBox.replaceChildren(OG.error(e, loadOverview));
          stats.replaceChildren();
          OG.status.live(false);
          return;
        }
        last = r.data || {};
        if (r.meta) OG.status.asOf(r.meta.as_of);
        asof.textContent = r.meta && r.meta.as_of ? "as of " + fmt.time(r.meta.as_of) : "";
        lastMeta = r.meta; drawStats(last, r.meta);
        if (last.board && last.board.available === false) { boardBox.replaceChildren(OG.insufficient(last.board.reason, "Market board unavailable")); return; }
        drawBoard();
      }
      async function loadSpark() {
        const m = await OG.data.market(24).catch(() => null);
        if (!m || !ctx.alive()) return;
        spark = new Map((m.gpus || []).map(g => [g.gpu, g]));
        drawBoard();
      }
      async function loadIndices() {
        const list = await OG.api.soft("/v1/indices", { nocache: true });
        if (!ctx.alive()) return;
        if (!Array.isArray(list)) { ixBox.replaceChildren(OG.empty("Indices did not load. Try again in a moment.")); return; }
        const byId = new Map(list.map(x => [x.id, x]));
        const rows = HOME_INDICES.map(id => byId.get(id)).filter(Boolean);
        ixBox.replaceChildren(rows.length ? h("div", { class: "hm-ixl" }, rows.map(indexRow)) : OG.empty(list.length ? "No benchmark index has data yet." : "No index has data yet."));
      }
      OG.data.intervals().then(m => {
        if (!ctx.alive() || !m || !m.size) return;
        polled = m.size;
        if (last) drawStats(last, lastMeta);
        provN.textContent = m.size + " GPU cloud providers";
        observeN.textContent = m.size + " providers";
      });
      const all = () => { loadOverview(); loadSpark(); loadIndices(); };
      all();
      ctx.every(60000, all);
    },
  });
})();
