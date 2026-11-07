/* /gpus: the full GPU market table.

Joins three reads on the canonical GPU name, each optional except the first:
  /v1/gpus?limit=1000        reference data: vendor specs per canonical GPU
  /v1/markets?limit=1000&trend=true  price (low / median / high), providers, listings, spread, efficiency score,
                             24h change of the lowest price and its sparkline
Filters (vendor, architecture, VRAM band, workload class, form factor, only-available, text) and sort live
in the URL so a filtered view is shareable. $/GB-VRAM-h = current low / VRAM: theoretical capability pricing.
*/
(() => {
  const { h, fmt, Lib: L } = OG;
  const VRAM_BANDS = [["", "Any VRAM"], ["0-24", "≤ 24 GB"], ["24-48", "25-48 GB"], ["48-96", "49-96 GB"], ["96-160", "97-160 GB"], ["160-9999", "> 160 GB"]];
  const inBand = (v, band) => { if (!band) return true; if (v == null) return false; const [a, b] = band.split("-").map(Number); return v > a && v <= b; };
  const small = v => (v == null || !isFinite(v) ? "-" : v >= 0.1 ? fmt.price(v) : "$" + Number(v).toPrecision(2));

  OG.page("/gpus", {
    title: "GPU markets",
    mount(el, params, query, ctx) {
      el.classList.add("gpx");
      ctx.onCleanup(() => el.classList.remove("gpx"));
      const q0 = OG.qs.all();
      const st = { q: q0.q || "", vendor: q0.vendor || "", arch: q0.arch || "", vram: q0.vram || "", wl: q0.wl || "", ff: q0.ff || "",
        avail: q0.avail === "1", all: q0.all === "1", rows: null };
      const strip = h("div", {});
      const holder = h("div", {}, OG.loading("Loading GPU markets…"));
      const sum = h("span", { class: "dim mono", style: "font-size:11px" });
      const tbl = OG.table({
        columns: [
          { key: "short", label: "GPU", fmt: (v, r) => h("b", {}, v), href: r => "/gpu/" + r.slug, ell: 300 },
          { key: "vendor", label: "Vendor", cls: "dim", hidden: true },
          { key: "arch", label: "Arch", cls: "dim", minor: 3 },
          { key: "vram", label: "VRAM", num: true, fmt: v => (v ? v + " GB" : "-") },
          { key: "ff", label: "Form", cls: "dim", minor: 2 },
          { key: "wl", label: "Class", cls: "dim", title: "workload class (editorial, from vendor positioning)", minor: 4 },
          { key: "low", label: "Low $/GPU·h", num: true, fmt: v => (v == null ? h("span", { class: "dimmer" }, "-") : h("b", {}, fmt.price(v))), title: "lowest current on-demand price per GPU-hour" },
          { key: "median", label: "Median", num: true, fmt: v => fmt.price(v), title: "median of each provider's lowest price" },
          { key: "high", label: "High", num: true, fmt: v => fmt.price(v) },
          { key: "chg", label: "24h", num: true, fmt: (v, r) => OG.chg(v, { reason: r.chgReason }), title: "24h change of the lowest price (the cheapest provider now vs 24 hours ago)" },
          { key: "spark", label: "24h trend", title: "lowest price over the last 24 hours", sort: false, csv: false, minor: 1, fmt: (v, r) => (v ? OG.charts.sparkline(v, { dir: fmt.dir(r.chg), width: 64, height: 16 }) : "") },
          { key: "providers", label: "Prov", num: true, desc: true, title: "Providers: how many providers price it now" },
          { key: "available", label: "Avail", num: true, desc: true, title: "Available: listings marked in stock now" },
          { key: "spread", label: "Spread", num: true, fmt: (v, r) => (r.providers > 1 ? fmt.pct(v) : "-"), title: "Spread: highest provider price vs the lowest" },
          { key: "eff", label: "Eff.", num: true, desc: true, fmt: (v, r) => (v == null ? OG.na(r.effReason) : h("span", { class: v >= 75 ? "up" : v < 50 ? "down" : "warn" }, v.toFixed(0))), title: "Efficiency: how tightly providers agree on price, 0 to 100 (higher = more consistent pricing). Not a hardware score. Needs 3+ providers" },
          { key: "perGb", label: "$/GB·h", num: true, fmt: v => small(v), title: "Lowest price per GB of VRAM per hour (theoretical)" },
          { key: "perTf", label: "$/BF16 PF·h", num: true, fmt: v => small(v), title: "Lowest price per dense BF16 petaFLOP-hour, from vendor peak specs (theoretical)" },
        ],
        rows: [], sort: { key: OG.qs.get("sort", "providers"), dir: OG.qs.get("dir", "desc") },
        onSort: s => OG.qs.set({ sort: s.key, dir: s.dir }),
        rowHref: r => "/gpu/" + r.slug, rowKey: r => r.slug, rowClass: r => (r.low == null ? "out" : ""),
        csv: "opengrid-gpu-markets.csv", title: "On-demand, per GPU-hour", toolbar: [sum], empty: "No GPUs match these filters.",
      });

      const sel = (key, options, label) => h("select", { class: "field", "aria-label": label, onchange: e => { st[key] = e.target.value; OG.qs.set({ [key]: st[key] || null }); draw(); } },
        options.map(([v, t]) => h("option", { value: v, selected: v === st[key] ? "" : null }, t)));
      const toggle = (key, label, title) => h("button", { class: "chip", "aria-pressed": String(st[key]), title,
        onclick: e => { st[key] = !st[key]; e.currentTarget.setAttribute("aria-pressed", String(st[key])); OG.qs.set({ [key]: st[key] ? 1 : null }); draw(); } }, label);
      const filters = h("div", { class: "bar" });
      el.append(OG.head("GPU markets", "On-demand price per GPU-hour across every provider OpenGrid reads. Click a row for the full market."),
        strip, filters, holder);

      function buildFilters(rows) {
        const uniq = k => [...new Set(rows.map(r => r[k]).filter(Boolean))].sort();
        const search = h("input", { class: "field", type: "search", placeholder: "Filter GPUs", value: st.q, "aria-label": "Filter GPUs", style: "width:150px",
          oninput: e => { st.q = e.target.value; OG.qs.set({ q: st.q || null }); draw(); } });
        filters.replaceChildren(search,
          sel("vendor", [["", "All vendors"], ...uniq("vendor").map(v => [v, v])], "Vendor"),
          sel("arch", [["", "All architectures"], ...uniq("arch").map(v => [v, v])], "Architecture"),
          sel("vram", VRAM_BANDS, "VRAM"),
          sel("wl", [["", "All workloads"], ...uniq("wl").map(v => [v, v])], "Workload class"),
          sel("ff", [["", "All form factors"], ...uniq("ff").map(v => [v, v])], "Form factor"),
          toggle("avail", "Available now", "only GPUs with at least one listing marked in stock"),
          toggle("all", "Include unpriced", "also list GPUs nobody prices right now"),
          h("span", { class: "spacer" }),
          h("button", { class: "btn sm", onclick: () => { Object.assign(st, { q: "", vendor: "", arch: "", vram: "", wl: "", ff: "", avail: false, all: false });
            OG.qs.set({ q: null, vendor: null, arch: null, vram: null, wl: null, ff: null, avail: null, all: null }); buildFilters(st.rows); draw(); } }, "Reset"));
      }

      function draw() {
        const rows = st.rows; if (!rows) return;
        const q = st.q.trim().toLowerCase();
        const shown = rows.filter(r => (st.all || r.low != null) && (!st.vendor || r.vendor === st.vendor) && (!st.arch || r.arch === st.arch) &&
          inBand(r.vram, st.vram) && (!st.wl || r.wl === st.wl) && (!st.ff || r.ff === st.ff) && (!st.avail || r.available > 0) &&
          (!q || r.name.toLowerCase().includes(q) || r.slug.includes(q)));
        sum.textContent = `${shown.length} of ${rows.length} GPUs`;
        tbl.update(shown);
        if (holder.firstChild !== tbl) holder.replaceChildren(tbl, h("p", { class: "note" }, OG.kindBadge("observed"),
          " On-demand list prices, one per provider (its lowest). $/GB and $/PF use vendor peak specs: theoretical, not benchmarks. ",
          h("a", { class: "lnk", href: "/methodology/dispersion" }, "Dispersion"), " · ", h("a", { class: "lnk", href: "/methodology/hardware" }, "Hardware")));
      }

      function drawStrip(rows, meta, mk) {
        const priced = rows.filter(r => r.low != null);
        const listings = priced.reduce((a, r) => a + (r.priced || 0), 0), avail = priced.reduce((a, r) => a + (r.available || 0), 0);
        const movers = priced.filter(r => r.chg != null && Math.abs(r.chg) >= 0.0005);
        const up = movers.filter(r => r.chg > 0).length, down = movers.filter(r => r.chg < 0).length;
        const eff = priced.filter(r => r.eff != null);
        const best = eff.slice().sort((a, b) => b.eff - a.eff)[0], worst = eff.slice().sort((a, b) => a.eff - b.eff)[0];
        strip.replaceChildren(OG.stats([
          { label: "GPUs priced now", value: String(priced.length), sub: `of ${rows.length} tracked` },
          { label: "Priced listings", value: fmt.num(listings), sub: `${fmt.num(avail)} explicitly available` },
          { label: "Lowest price, 24h", value: movers.length ? `${up}▲ ${down}▼` : null, reason: mk ? "no lowest price moved" : "24h data unavailable", sub: mk ? `${priced.length - movers.length} unchanged` : null, title: "GPUs whose lowest price rose or fell over the last 24 hours" },
          { label: "Most consistent price", value: best ? OG.shortGpu(best.name) : null, reason: "needs 3+ providers", sub: best ? `score ${best.eff.toFixed(0)} · ${best.providers} providers` : null, title: "providers agree most on price (market efficiency score, not a hardware score)" },
          { label: "Least consistent price", value: worst ? OG.shortGpu(worst.name) : null, reason: "needs 3+ providers", sub: worst ? `score ${worst.eff.toFixed(0)} · spread ${fmt.pct(worst.spread)}` : null, title: "providers disagree most on price (market efficiency score, not a hardware score)" },
          { label: "As of", value: meta && meta.as_of ? fmt.time(meta.as_of) : null, sub: meta && meta.as_of ? fmt.date(meta.as_of) : null },
        ]));
      }

      async function load() {
        let r;
        try { r = await ctx.api("/v1/gpus", { params: { limit: 1000 }, full: true, slot: "gpus-v1" }); }
        catch (e) { if (!e.stale) holder.replaceChildren(OG.error(e, load)); return; }
        // /v1/markets?trend=true: price, spread, score, 24h change and sparkline per GPU with a live listing
        const ms = await ctx.api("/v1/markets", { params: { limit: 1000, trend: true } }).catch(() => null), mk = ms;
        const byM = new Map((ms || []).map(g => [g.gpu, { change_pct: g.change ? g.change.pct : null, change_since: g.change ? g.change.since : null, spark: g.trend || null }])), byS = new Map((ms || []).map(s => [s.gpu, s]));
        const rows = r.data.map(g => {
          const m = byM.get(g.name), s = byS.get(g.name), p = s ? s.price : {};
          return { slug: OG.slug(g.name), name: g.name, short: OG.shortGpu(g.name), vendor: g.vendor || L.vendorOf(g.name), arch: g.architecture || null, vram: g.vram_gb || null,
            ff: g.form_factor || null, wl: g.workload_class || null, low: p.low ?? null, median: p.median ?? null, high: p.high ?? null, providers: s ? s.providers : 0,
            available: s ? s.listings.available : 0, priced: s ? s.listings.priced : 0,
            chg: m ? m.change_pct : null, chgReason: m ? (m.change_since ? null : "not enough history in 24h") : mk ? "no 24h data" : "24h data unavailable", spark: m ? m.spark : null,
            spread: s ? s.spread.pct_of_low : null, eff: s ? s.score.efficiency : null, effReason: s ? s.score.reason : ms ? "no price" : "markets unavailable",
            perGb: p.low != null && g.vram_gb ? p.low / g.vram_gb : null, perTf: p.low != null && g.bf16_tflops_dense ? p.low / (g.bf16_tflops_dense / 1000) : null };
        });
        st.rows = rows;
        OG.status.asOf(r.meta && r.meta.as_of);
        if (!filters.childNodes.length) buildFilters(rows);
        drawStrip(rows, r.meta, mk);
        draw();
      }
      load();
      ctx.every(60000, load);
    },
  });
})();
