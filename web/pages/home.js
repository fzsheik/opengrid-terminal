/* / — homepage for a first-time visitor: what OpenGrid is (headline), three actions (Explore GPUs, Find Compute,
   the sandbox training demo), a compact live board of the most-covered GPUs and how OpenGrid works.

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
          fmt: (v, r) => (r.spark ? OG.charts.sparkline(r.spark, { dir: fmt.dir(r.spark_chg), width: 84, height: 16 }) : h("span", { class: "dimmer" }, "–")) },
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

      const provN = h("span", {}, "cloud providers");
      const stats = h("div", { class: "hm-stats" }, OG.loading("Loading live market…"));
      const tbl = board();
      const boardBox = h("div", { class: "hm-board-b" }, OG.loading("Loading prices…"));
      const asof = h("span", { class: "mono dimmer hm-asof" });
      const ixBox = h("div", { class: "hm-ixs" }, OG.loading("Loading indices…"));
      const observeN = h("span", {}, "every tracked provider");

      const action = (href, title, line, tag, pri) => h("a", { class: "hm-act" + (pri ? " pri" : ""), href },
        h("span", { class: "hm-act-t" }, title, h("span", { class: "hm-act-arr", "aria-hidden": "true" }, "→")),
        h("span", { class: "hm-act-l" }, line), tag || null);

      root.append(
        h("section", { class: "hm-hero" },
          h("div", { class: "hm-hero-l" },
            h("div", { class: "eyebrow" }, "OpenGrid · GPU compute market"),
            h("h1", { class: "hm-h1" }, "The smartest way to find and run GPU compute."),
            h("p", { class: "hm-sub" }, "Compare GPU prices across ", provN, ", find compute suited to your workload, and manage execution through one platform."),
            h("p", { class: "hm-honest" },
              "Prices are observed from provider price lists and APIs, labelled with their age. Real launches are supervised: each one waits for OpenGrid approval. The training demo is a free simulation — no GPU, no spend.")),
          h("div", { class: "hm-acts" },
            action("/gpus", "Explore GPUs", "Live prices, availability and history for every GPU across providers.", null, true),
            action("/find", "Find Compute", "Describe your workload; get GPUs that fit it, ranked by price and fit.", null),
            action(DEMO, "Try a Training Demo", "Train PyTorch MNIST end to end in the sandbox.", h("span", { class: "badge warn hm-act-b" }, "Simulated · free")))),
        stats,
        h("div", { class: "hm-mkt" },
          h("section", { class: "hm-board" },
            h("header", { class: "hm-sh" }, h("h2", { class: "sec-h" }, "Live market · most-covered GPUs"), OG.kindBadge("observed"), asof,
              h("span", { class: "spacer" }), h("a", { class: "lnk hm-more", href: "/gpus" }, "All GPUs →"), h("a", { class: "lnk hm-more", href: "/markets" }, "Market dashboard →")),
            boardBox),
          h("section", { class: "hm-side" },
            h("header", { class: "hm-sh" }, h("h2", { class: "sec-h" }, "Price indices"), h("span", { class: "spacer" }), h("a", { class: "lnk hm-more", href: "/indices" }, "All →")),
            ixBox,
            h("p", { class: "note" }, "On-demand list prices per GPU-hour, not quotes. ", h("a", { class: "lnk", href: "/methodology/data-kinds" }, "Data kinds"), " · ", h("a", { class: "lnk", href: "/methodology/indices" }, "Index methodology")))),
        h("section", { class: "hm-how" },
          h("h2", { class: "sec-h" }, "How OpenGrid works"),
          h("ol", { class: "hm-steps" },
            step("01", ["Observe ", observeN], OG.kindBadge("observed"),
              "OpenGrid polls each provider's public prices and availability on a schedule. Every number shows where it came from and how old it is; a statistic it cannot compute honestly reads n/a with the reason.",
              ["/providers", "Providers and coverage"]),
            step("02", "Recommend for your workload", OG.kindBadge("estimated"),
              "Describe a model and job (or let the CLI read your project locally). OpenGrid estimates the GPU memory it needs and ranks listings that fit. Estimates are labelled as estimates; runtime is unknown unless it has been measured.",
              ["/find", "Find Compute"]),
            step("03", "Run safely", h("span", { class: "badge warn" }, "supervised"),
              "Previewing a route is free and never calls a provider. A real launch runs only on a validated provider, after an OpenGrid admin approves the quote, with a cost ceiling and termination evidence. Today the runnable workload is the MNIST training template.",
              ["/methodology/execution-safety", "Execution safety"]))),
        h("nav", { class: "hm-more-row", "aria-label": "More" },
          h("span", { class: "eyebrow" }, "Also"),
          [["/developers", "Developers: CLI · MCP · API"], ["/compare", "Compare GPUs"], ["/reserve", "Reserve capacity"], ["/watchlists", "Watchlists & alerts"], ["/events", "Market events"], ["/methodology", "Methodology"]]
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
          { label: "Listings available", value: c.available === false ? null : fmt.num(c.available_listings_now), reason: c.reason || "capacity unavailable", kind: "observed" },
          { label: "Cheapest GPU-hour", value: cheapest ? fmt.price(cheapest.lowest) : null, reason: "no GPU is priced right now",
            sub: cheapest ? L.shortGpu(cheapest.gpu) + " at " + L.providerName(cheapest.lowest_provider) : null },
          { label: "Sold out everywhere", value: c.available === false ? null : fmt.num(c.gpus_sold_out_everywhere), reason: c.reason || "capacity unavailable", sub: "GPUs with listings, none purchasable" },
          { label: "Providers polled", value: polled ? fmt.num(polled) : null, reason: "the poll schedule (/polling) did not answer", sub: "public prices and availability" },
        ]));
      }
      async function loadOverview() {
        let r;
        try { r = await ctx.api("/v1/overview", { full: true, nocache: true }); }
        catch (e) {
          boardBox.replaceChildren(OG.error(e, loadOverview));
          stats.replaceChildren();
          OG.status.live(false, "api unreachable");
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
        if (!Array.isArray(list)) { ixBox.replaceChildren(OG.empty("The index service did not answer (/v1/indices).")); return; }
        const byId = new Map(list.map(x => [x.id, x]));
        const rows = HOME_INDICES.map(id => byId.get(id)).filter(Boolean);
        ixBox.replaceChildren(rows.length ? h("div", { class: "hm-ixl" }, rows.map(indexRow)) : OG.empty(list.length ? "No benchmark index has data yet." : "No index has data yet."));
      }
      OG.data.intervals().then(m => {
        if (!ctx.alive() || !m || !m.size) return;
        polled = m.size;
        if (last) drawStats(last, lastMeta);
        provN.textContent = m.size + " cloud providers";
        observeN.textContent = m.size + " providers";
      });
      const all = () => { loadOverview(); loadSpark(); loadIndices(); };
      all();
      ctx.every(60000, all);
    },
  });
})();
