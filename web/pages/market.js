/* Shared market views (interim, ported from the first web UI's market grid).
   OG.views.windowSeg(hours, onChange)       time-range control, remembers the choice
   OG.views.marketBoard(el, ctx, opts)       dense board of every canonical GPU from /market
   Used by / (until the Overview page lands) and /gpus. */
(() => {
  const { h, fmt, Lib: L } = OG;
  const WINDOWS = [[1, "1H"], [6, "6H"], [24, "24H"], [168, "1W"], [0, "ALL"]];
  OG.views = OG.views || {};
  OG.views.WINDOWS = WINDOWS;

  // ?h= wins (shareable), then the last choice, then 24h. hours=0 means all history.
  OG.views.hours = () => {
    const q = OG.qs.get("h");
    if (q != null && WINDOWS.some(w => String(w[0]) === q)) return Number(q);
    try { const s = localStorage.getItem("og.hours"); if (s !== null && WINDOWS.some(w => w[0] === Number(s))) return Number(s); } catch (e) {}
    return 24;
  };
  OG.views.windowSeg = (hours, onChange) => OG.seg(WINDOWS, hours, v => {
    try { localStorage.setItem("og.hours", String(v)); } catch (e) {}
    OG.qs.set({ h: v === 24 ? null : v });
    onChange(Number(v));
  });

  OG.views.marketBoard = (el, ctx, opts) => {
    opts = opts || {};
    const st = { hours: OG.views.hours(), q: OG.qs.get("q", ""), vendor: OG.qs.get("vendor", "All"), singles: OG.qs.get("singles") === "1", data: null };
    const vendorBox = h("div", { class: "flt" });
    const holder = h("div", {}, OG.loading("Loading market…"));
    const sum = h("span", { class: "dim mono", style: "font-size:11px" });
    const tbl = OG.table({
      columns: [
        { key: "gpu", label: "GPU", fmt: (v) => h("span", {}, h("b", {}, L.shortGpu(v))), value: r => L.shortGpu(r.gpu), href: r => "/gpu/" + OG.slug(r.gpu) },
        { key: "vendor", label: "Vendor", value: r => L.vendorOf(r.gpu), cls: "dim" },
        { key: "lowest", label: "Lowest $/GPU·h", num: true, fmt: v => fmt.price(v), title: "Lowest in-stock on-demand price per GPU-hour now" },
        { key: "change_pct", label: "Chg", num: true, fmt: (v, r) => OG.chg(v, { reason: r.change_since ? null : "not enough coherent history in this window" }), title: "Change of the lowest price over the window, from when every current provider was recorded" },
        { key: "spark", label: "Trend", sort: false, csv: false, fmt: (v, r) => OG.charts.sparkline(v, { dir: fmt.dir(r.change_pct), width: 90, height: 18 }) },
        { key: "median", label: "Median", num: true, fmt: v => fmt.price(v) },
        { key: "highest", label: "High", num: true, fmt: v => fmt.price(v) },
        { key: "spread", label: "Spread", num: true, value: r => (r.lowest ? r.highest / r.lowest - 1 : null), fmt: (v, r) => r.providers > 1 ? fmt.pct(r.lowest ? r.highest / r.lowest - 1 : null) : "–", title: "Highest / lowest − 1 across providers now" },
        { key: "providers", label: "Prov", num: true, desc: true, title: "Providers selling in stock now" },
        { key: "lowest_provider", label: "Cheapest at", fmt: v => OG.providerLink(v) },
      ],
      rows: [], sort: { key: OG.qs.get("sort", "providers"), dir: OG.qs.get("dir", "desc") },
      onSort: s => OG.qs.set({ sort: s.key, dir: s.dir }),
      rowHref: r => "/gpu/" + OG.slug(r.gpu), rowKey: r => r.gpu,
      csv: "opengrid-market.csv", title: "On-demand market", toolbar: [sum],
      empty: "No GPUs match.",
    });
    const win = OG.views.windowSeg(st.hours, v => { st.hours = v; st.data = null; holder.replaceChildren(OG.loading("Loading market…")); load(); });
    const search = h("input", { class: "field", type: "search", placeholder: "Filter GPUs", value: st.q, "aria-label": "Filter GPUs",
      oninput: e => { st.q = e.target.value; OG.qs.set({ q: st.q || null }); draw(); } });
    const singles = h("button", { class: "chip", "aria-pressed": String(st.singles), title: "GPUs only one provider sells have no market to compare",
      onclick: e => { st.singles = !st.singles; e.currentTarget.setAttribute("aria-pressed", String(st.singles)); OG.qs.set({ singles: st.singles ? 1 : null }); draw(); } }, "Single-provider");
    el.append(h("div", { class: "bar" }, search, vendorBox, singles, h("span", { class: "spacer" }), win), holder);

    function draw() {
      const o = st.data; if (!o) return;
      const vendors = ["All", ...[...new Set(o.gpus.map(g => L.vendorOf(g.gpu)))].sort()];
      vendorBox.replaceChildren(...vendors.map(v => h("button", { class: "chip", "aria-pressed": String(v === st.vendor), onclick: () => { st.vendor = v; OG.qs.set({ vendor: v === "All" ? null : v }); draw(); } }, v)));
      const q = st.q.trim().toLowerCase();
      const list = o.gpus.filter(g => (st.singles || g.providers >= 2) && (st.vendor === "All" || L.vendorOf(g.gpu) === st.vendor) && (!q || g.gpu.toLowerCase().includes(q)));
      sum.textContent = `${list.length} of ${o.gpus.length} GPUs · ${o.hours ? "window " + (WINDOWS.find(w => w[0] === o.hours) || [0, o.hours + "h"])[1] : "all history"}`;
      tbl.update(list);
      if (holder.firstChild !== tbl) holder.replaceChildren(tbl, h("p", { class: "note" },
        "Observed list prices, on-demand, not interruptible, in stock or stock unknown. Change starts once every provider selling now was being recorded. ",
        h("a", { class: "lnk", href: "/methodology/data-kinds" }, "Data kinds"), " ", OG.kindBadge("observed")));
    }
    async function load() {
      const hours = st.hours;
      try {
        const d = await ctx.api("/market", { params: { hours }, slot: "market-board" });
        if (hours !== st.hours) return;
        st.data = d; OG.status.asOf(d.t1); draw();
        opts.onData && opts.onData(d);
      } catch (e) { if (!e.stale) { holder.replaceChildren(OG.error(e, load)); OG.status.live(false, "api unreachable"); } }
    }
    load();
    ctx.every(30000, load);
  };
})();
