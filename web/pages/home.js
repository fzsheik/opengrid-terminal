/* /get-started: where the landing page's "Learn more" goes. A plain headline, four numbered steps that each say
   what they need (free / simulation / invite only) and a compact live price board of the most-covered
   GPUs. A visitor who picks the demo gets the invite-only explanation in place, never a sign-in error; a
   signed-in operator goes straight to /jobs/new. Everything else (indices, the market dashboard, methodology)
   is one click away in the menu.

   Data (all real, none invented): /internal/overview (board: lowest price, cheapest provider, provider count, matched
   24h change), /market?hours=24 (sparklines, shared with the tape), /polling (how many providers are read, via
   OG.data.intervals()). The status bar says when prices were last read. */
(() => {
  const { h, fmt, Lib: L } = OG;
  const DEMO = "/jobs/new?preset=mnist&sandbox=1";
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
        { key: "lowest", label: "From $/GPU-hr", num: true, fmt: v => h("b", {}, fmt.price(v)), title: "lowest on-demand price per GPU-hour now" },
        { key: "lowest_provider", label: "Cheapest at", fmt: v => OG.providerLink(v) },
        { key: "providers", label: "Providers", num: true, desc: true, title: "providers selling this GPU now" },
        { key: "change_24h_median", label: "24h", num: true, desc: true, title: "change of the median provider price over 24 hours",
          fmt: (v, r) => OG.chg(v, { reason: r.providers_matched < 2 ? "needs 2 providers priced 24h ago and now" : "not enough history" }) },
        { key: "spark", label: "Last 24h", sort: false, csv: false, title: "lowest price over the last 24 hours",
          fmt: (v, r) => (r.spark ? OG.charts.sparkline(r.spark, { dir: fmt.dir(r.spark_chg), width: 84, height: 16 }) : h("span", { class: "dimmer" }, "-")) },
      ],
      rows: [], sort: { key: "providers", dir: "desc" }, compact: true, csv: false, limit: BOARD_ROWS,
      rowKey: r => r.gpu, rowHref: r => "/gpu/" + r.gpu_slug, empty: "No GPU is priced right now.",
    });
  }

  OG.page("/get-started", {
    title: "Get started",
    nav: "start",
    mount(el, params, query, ctx) {
      const root = h("div", { class: "pg-home" });
      el.append(root);

      const provN = h("span", {}, "GPU cloud providers");
      const tbl = board();
      const boardBox = h("div", { class: "hm-board-b" }, OG.loading("Loading prices…"));

      // One quiet row per step: number, what it is, one line, and what it needs (free / simulation / invite only).
      const step = (el, title, line, need) => {
        el.className = "hm-step";
        el.append(h("span", { class: "hm-step-n", "aria-hidden": "true" }),
          h("span", { class: "hm-step-b" }, h("span", { class: "hm-step-t" }, title), h("span", { class: "hm-step-l" }, line)),
          h("span", { class: "hm-step-need" }, need), h("span", { class: "hm-step-arr", "aria-hidden": "true" }, "→"));
        return el;
      };
      // The demo runs in an OpenGrid workspace (invite only): a visitor gets the explanation here instead of a
      // sign-in error one click later; a signed-in operator goes straight to it.
      const demoWhy = h("div", { class: "hm-why", id: "hm-demo-why", hidden: true },
        h("b", {}, "The demo is invite only"),
        h("p", {}, "It runs in an OpenGrid workspace, and workspaces are invite-only for now. A small model learns to read handwritten digits (MNIST). It is a simulation: no GPU is rented and nothing is charged."));
      const demoBtn = h("button", { type: "button", "aria-expanded": "false", "aria-controls": "hm-demo-why",
        onclick: () => { demoWhy.hidden = !demoWhy.hidden; demoBtn.setAttribute("aria-expanded", String(!demoWhy.hidden)); } });
      const DEMO_T = "Try the training demo", DEMO_L = "Watch a small AI model train, step by step. A simulation.";
      const demoStep = step(demoBtn, DEMO_T, DEMO_L, OG.signedIn() ? "Free" : "Invite only");
      OG.me().then(me => {
        if (!me || !ctx.alive()) return;
        const a = h("a", { href: DEMO });
        a.append(...demoStep.childNodes);
        a.className = demoStep.className;
        a.querySelector(".hm-step-need").textContent = "Free";
        demoStep.replaceWith(a);
        demoWhy.remove();
      });

      root.append(
        h("section", { class: "hm-hero" },
          h("h1", { class: "hm-h1" }, "Find the cheapest GPU for your job."),
          h("p", { class: "hm-sub" }, "Live prices from ", provN, ". Free to use, no account needed.")),
        h("nav", { class: "hm-steps", "aria-label": "Get started" },
          step(h("a", { href: "/", rel: "external" }), "Get an API key", "One click. Read every price from your own code.", "Free"),
          step(h("a", { href: "/gpus" }), "Explore GPU prices", "Every GPU, cheapest provider first, with price history.", "Free"),
          step(h("a", { href: "/find" }), "Find compute for my project", "Describe your model and job; get the GPUs that fit, ranked by price.", "Free"),
          demoStep, demoWhy),
        h("section", { class: "hm-board" },
          h("header", { class: "hm-sh" }, h("h2", { class: "hm-h2" }, "Cheapest right now"),
            h("span", { class: "spacer" }), h("a", { class: "lnk hm-more", href: "/gpus" }, "All GPUs →")),
          boardBox));

      let spark = new Map(), last = null;
      function drawBoard() {
        if (!last) return;
        const rows = popular(last.board && last.board.items).map(b => {
          const s = spark.get(b.gpu);
          return Object.assign({}, b, s ? { spark: s.spark, spark_chg: s.change_pct } : {});
        });
        tbl.update(rows);
        if (boardBox.firstChild !== tbl) boardBox.replaceChildren(tbl);
      }
      async function loadOverview() {
        let o;
        try { o = await ctx.api("/internal/overview", { nocache: true }); }
        catch (e) { boardBox.replaceChildren(OG.error(e, loadOverview)); return; }
        last = o || {};
        if (last.board && last.board.available === false) { boardBox.replaceChildren(OG.insufficient(last.board.reason, "Prices unavailable")); return; }
        drawBoard();
      }
      async function loadSpark() {
        const m = await OG.data.market(24).catch(() => null);
        if (!m || !ctx.alive()) return;
        spark = new Map((m.gpus || []).map(g => [g.gpu, g]));
        drawBoard();
      }
      OG.data.intervals().then(m => {
        if (ctx.alive() && m && m.size) provN.textContent = m.size + " GPU cloud providers";
      });
      const all = () => { loadOverview(); loadSpark(); };
      all();
      ctx.every(60000, all);
    },
  });
})();
