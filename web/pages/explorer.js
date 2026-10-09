/* /explorer — every current listing, filterable, sortable, shareable (all state lives in the URL), CSV export.
   Data: /listings (always). Extra columns appear when these answer (fail soft on 404):
     /v1/gpus            -> VRAM, architecture  (items: {name|gpu, vram_gb|spec.vram_gb, architecture|spec.architecture})
     /v1/trust/listings  -> freshness, availability basis  (items: {provider, listing_id, freshness, availability_basis, source_type})
   Source type always comes from provider_meta (embedded in the page). */
(() => {
  const { h, fmt, Lib: L } = OG;
  const KEYS = ["gpu", "provider", "region", "min", "max", "avail", "type", "tier", "count", "q", "sort", "dir"];

  OG.page("/explorer", {
    title: "Explorer",
    mount(el, params, query, ctx) {
      const f = {};
      for (const k of KEYS) f[k] = query[k] || "";
      let rows = null, specs = null, trust = null;

      const sum = h("span", { class: "dim mono", style: "font-size:11px" });
      const filters = h("div", { class: "bar" });
      const holder = h("div", {}, OG.loading("Loading listings…"));
      el.append(OG.head("Explorer", "Every current listing from every provider, as observed. Filter, sort, share the URL, export CSV.",
        h("button", { class: "btn", onclick: () => { for (const k of KEYS) f[k] = ""; sync(); buildFilters(); draw(); } }, "Reset")), filters, holder);

      const sync = () => { const patch = {}; for (const k of KEYS) patch[k] = f[k] || null; OG.qs.set(patch); };
      let tmr = 0;
      const later = () => { clearTimeout(tmr); tmr = setTimeout(() => { sync(); draw(); }, 140); };

      function select(key, label, options, allLabel) {
        const s = h("select", { class: "field", "aria-label": label, onchange: e => { f[key] = e.target.value; sync(); draw(); } },
          h("option", { value: "" }, allLabel || "All"), options.map(([v, t]) => h("option", { value: v, selected: String(v) === f[key] }, t)));
        return h("label", { class: "flt" }, h("span", {}, label), s);
      }
      function input(key, label, ph, cls) {
        return h("label", { class: "flt" }, h("span", {}, label), h("input", { class: "field " + (cls || ""), value: f[key], placeholder: ph || "", "aria-label": label, inputmode: cls === "num" ? "decimal" : null,
          oninput: e => { f[key] = e.target.value.trim(); later(); } }));
      }
      const distinct = (k, fmtv) => [...new Set(rows.map(r => r[k]).filter(v => v != null && v !== ""))].sort((a, b) => String(a).localeCompare(String(b), undefined, { numeric: true })).map(v => [v, fmtv ? fmtv(v) : v]);

      function buildFilters() {
        if (!rows) return;
        const gpus = distinct("canonical_gpu_name", L.shortGpu);
        filters.replaceChildren(
          input("q", "Text", "gpu, sku, region…"),
          select("gpu", "GPU", [...gpus, ["__unmapped", "(unmapped names)"]]),
          select("provider", "Provider", distinct("provider", OG.providerName)),
          select("region", "Region", distinct("region")),
          select("type", "Market", distinct("market_type")),
          select("tier", "Tier", distinct("provider_tier")),
          select("count", "GPUs", distinct("gpu_count", v => v + "×")),
          select("avail", "Stock", [["in", "in stock"], ["notout", "not sold out"], ["out", "sold out"], ["unknown", "unknown"]], "Any"),
          input("min", "$ min", "0.00", "num"), input("max", "$ max", "∞", "num"));
      }

      const num = v => (v === "" || v == null || isNaN(Number(v)) ? null : Number(v));
      function filtered() {
        const q = f.q.toLowerCase(), lo = num(f.min), hi = num(f.max);
        return rows.filter(r =>
          (!f.gpu || (f.gpu === "__unmapped" ? !r.canonical_gpu_name : r.canonical_gpu_name === f.gpu)) &&
          (!f.provider || r.provider === f.provider) &&
          (!f.region || r.region === f.region) &&
          (!f.type || r.market_type === f.type) &&
          (!f.tier || r.provider_tier === f.tier) &&
          (!f.count || String(r.gpu_count) === f.count) &&
          (!f.avail || (f.avail === "in" ? r.available === true : f.avail === "notout" ? r.available !== false : f.avail === "out" ? r.available === false : r.available == null)) &&
          (lo == null || (r.price_per_gpu_hour != null && r.price_per_gpu_hour >= lo)) &&
          (hi == null || (r.price_per_gpu_hour != null && r.price_per_gpu_hour <= hi)) &&
          (!q || [r.canonical_gpu_name, r.raw_gpu_name, r.provider, OG.providerName(r.provider), r.sku, r.region, r.country, r.market_type, r.provider_tier].some(v => v && String(v).toLowerCase().includes(q))));
      }

      const specOf = r => specs && r.canonical_gpu_name ? specs.get(r.canonical_gpu_name) : null;
      const trustOf = r => trust ? trust.get(r.provider + "\u0000" + r.listing_id) : null;
      const stock = v => v === true ? h("span", {}, "in stock") : v === false ? h("span", { class: "down" }, "sold out") : h("span", { class: "dim", title: "the provider does not report stock" }, "unknown");
      function columns() {
        return [
          { key: "canonical_gpu_name", label: "GPU", value: r => r.canonical_gpu_name ? L.shortGpu(r.canonical_gpu_name) : null,
            fmt: (v, r) => v ? OG.gpuLink(v) : h("span", { class: "dim", title: "raw name, not mapped to a canonical GPU" }, r.raw_gpu_name), csv: r => r.canonical_gpu_name, ell: 200 },
          { key: "raw_gpu_name", label: "Raw name", hidden: true },
          specs ? { key: "vram", label: "VRAM", num: true, value: r => (specOf(r) || {}).vram_gb ?? null, fmt: (v, r) => { const s = specOf(r); return s && s.vram_gb ? s.vram_gb + "G" : "–"; } } : null,
          specs ? { key: "arch", label: "Arch", value: r => (specOf(r) || {}).architecture ?? null, cls: "dim", minor: 3 } : null,
          { key: "provider", label: "Provider", value: r => OG.providerName(r.provider), fmt: (v, r) => OG.providerLink(r.provider), csv: r => r.provider },
          { key: "sku", label: "SKU", cls: "dim mono", ell: 170, minor: 2 },
          { key: "gpu_count", label: "GPUs", num: true },
          // a price of 0 is "not priced" (the provider published no rate), never "$0.000", and sorts last
          { key: "price_per_gpu_hour", label: "$/GPU·h", num: true, value: r => (r.price_per_gpu_hour > 0 ? r.price_per_gpu_hour : null), csv: r => r.price_per_gpu_hour,
            fmt: v => (v > 0 ? fmt.price(v) : OG.na("no price published for this listing")), cls: "strong" },
          { key: "last_change", label: "Last chg", num: true, title: "Change from the previous recorded price of this listing (observed)",
            value: r => (r.previous_price_per_gpu_hour && r.price_per_gpu_hour != null ? r.price_per_gpu_hour / r.previous_price_per_gpu_hour - 1 : null),
            fmt: (v, r) => (r.previous_price_per_gpu_hour && r.price_per_gpu_hour != null ? h("span", { title: `was ${fmt.price(r.previous_price_per_gpu_hour)}, changed ${fmt.dateTime(r.changed_at)}` }, OG.chg(r.price_per_gpu_hour / r.previous_price_per_gpu_hour - 1)) : h("span", { class: "dimmer" }, "–")) },
          { key: "price_per_instance_hour", label: "$/inst·h", num: true, value: r => (r.price_per_instance_hour > 0 ? r.price_per_instance_hour : null), csv: r => r.price_per_instance_hour, fmt: v => (v > 0 ? fmt.price(v) : "–"), minor: 4 },
          { key: "region", label: "Region", cls: "dim", ell: 140 },
          { key: "country", label: "Ctry", cls: "dim", minor: 5 },
          { key: "market_type", label: "Market", cls: "dim" },
          { key: "provider_tier", label: "Tier", cls: "dim", minor: 6 },
          { key: "interruptible", label: "Intr", title: "Interruptible (can be preempted)", fmt: v => v === true ? "yes" : v === false ? "no" : "–", cls: "dim", minor: 5 },
          { key: "available", label: "Stock", fmt: v => stock(v) },
          trust ? { key: "avail_basis", label: "Stock basis", value: r => (trustOf(r) || {}).availability_basis ?? null, cls: "dim", minor: 7 } : null,
          { key: "vcpu", label: "vCPU", num: true, minor: 9 },
          { key: "ram_gb", label: "RAM", num: true, fmt: v => v != null ? fmt.num(v, 0) + "G" : "–", minor: 9 },
          { key: "source_type", label: "Source", value: r => (trustOf(r) || {}).source_type || (OG.providerMeta(r.provider) || {}).source_type || null, cls: "dim", title: "How OpenGrid reads this provider (provider_meta)", minor: 8 },
          { key: "observed_at", label: "Seen", num: true, title: "Last observed (freshness: fresh ≤30m, aging ≤3h, else stale)",
            value: r => r.observed_at ? -new Date(r.observed_at) : null,
            fmt: (v, r) => OG.freshBadge((trustOf(r) || {}).freshness && (trustOf(r) || {}).age_seconds != null ? (trustOf(r)).age_seconds : r.observed_at, { provider: r.provider }), csv: r => r.observed_at },
          { key: "first_seen_at", label: "First seen", num: true, fmt: v => fmt.date(v), cls: "dim", minor: 7 },
        ].filter(Boolean);
      }

      let tbl = null;
      function draw() {
        if (!rows) return;
        const list = filtered();
        sum.textContent = `${list.length} of ${rows.length} listings · ${new Set(list.map(r => r.canonical_gpu_name).filter(Boolean)).size} GPUs · ${new Set(list.map(r => r.provider)).size} providers`;
        if (!tbl) {
          tbl = OG.table({
            columns: columns(), rows: list, sort: f.sort ? { key: f.sort, dir: f.dir === "desc" ? "desc" : "asc" } : { key: "price_per_gpu_hour", dir: "asc" },
            onSort: s => { f.sort = s.key; f.dir = s.dir; sync(); },
            rowKey: r => r.provider + ":" + r.listing_id, csv: "opengrid-listings.csv", title: "Listings", toolbar: [sum], compact: true, limit: 400,
            empty: "No listings match these filters.",
          });
          holder.replaceChildren(tbl, h("p", { class: "note" }, OG.kindBadge("observed"),
            " Prices and stock exactly as each provider reports them, normalized to USD per GPU-hour. Market lines elsewhere use a stricter subset (on-demand, not interruptible, in stock or unknown). ",
            h("a", { class: "lnk", href: "/methodology" }, "Methodology")));
        } else tbl.update(list);
      }
      function rebuildTable() { tbl = null; draw(); }

      async function load() {
        try {
          rows = await ctx.api("/listings");
          const newest = rows.reduce((m, r) => (r.observed_at && r.observed_at > m ? r.observed_at : m), "");
          if (newest) OG.status.asOf(newest);
          if (!filters.firstChild) buildFilters();
          draw();
        } catch (e) { holder.replaceChildren(OG.error(e, load)); }
      }
      load();
      ctx.every(120000, load);
      // Optional enrichments: add columns when the /v1 endpoints exist
      OG.api.soft("/v1/gpus", { params: { limit: 1000 } }).then(r => {
        const list = Array.isArray(r) ? r : r && Array.isArray(r.items) ? r.items : null;
        if (!ctx.alive() || !list) return;
        const m = new Map();
        for (const g of list) {
          const name = g.name || g.gpu || g.canonical_name, sp = g.spec || g.hardware || g;
          if (name && (sp.vram_gb != null || sp.architecture)) m.set(name, { vram_gb: sp.vram_gb, architecture: sp.architecture });
        }
        if (m.size) { specs = m; rebuildTable(); }
      });
      OG.api.soft("/v1/trust/listings", { params: { limit: 1000 } }).then(r => {
        const list = Array.isArray(r) ? r : r && Array.isArray(r.items) ? r.items : null;
        if (!ctx.alive() || !list || !list.length) return;
        trust = new Map(list.map(t => [t.provider + "\u0000" + t.listing_id, t]));
        rebuildTable();
      });
    },
  });
})();
