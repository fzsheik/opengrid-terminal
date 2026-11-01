/* /reserved: reserved and committed GPU prices by term, from /v1/reserved and /v1/reserved/summary.
   Their own section on purpose: none of these prices is an on-demand listing, an index input or a market statistic
   (methodology/reserved-prices.md). Per GPU, terms are columns; a cell is the effective $/GPU-hr and the saving
   against the matching on-demand price. Pure helpers live on OG.reservedPrices (tests/test_web_reserved.js). */
(() => {
  const { h, fmt } = OG;

  /* =============================== pure =============================== */
  const RULE = "Kept apart: reserved and committed prices never enter on-demand prices, indices, spreads or market statistics.";
  const EMPTY = "No reserved prices are stored on this server yet. They are read from the raw responses OpenGrid already "
    + "stores for Azure, GCP, Jarvislabs, Latitude, Together AI and Verda, so they appear after those providers' next successful poll.";
  const FILTERED = "No reserved prices match these filters.";
  const MATCH = {
    same_listing: "the provider's own on-demand listing for this SKU",
    same_source: "the provider's own pay-as-you-go price for this product, from the same response",
    same_size_region: "same provider, GPU, size and region",
    same_size: "same provider, GPU and size; cheapest region (the region differs)",
  };

  // Terms present in a set of rows, in bucket order (meta.terms).
  function termsIn(rows, terms) {
    const have = new Set(rows.map(r => r.term_bucket));
    return (terms || []).filter(t => have.has(t.key));
  }
  // One line per offer (provider, size, region, SKU); terms become columns.
  function pivot(rows) {
    const lines = new Map();
    for (const r of rows) {
      const key = [r.provider, r.gpu_count, r.region || "", r.sku || "", r.scope].join("|");
      let ln = lines.get(key);
      if (!ln) {
        ln = { key, provider: r.provider, gpu: r.gpu, raw_gpu_name: r.raw_gpu_name, gpu_count: r.gpu_count, region: r.region,
          sku: r.sku, scope: r.scope, derived: r.derived, method: r.method, source: r.source, stale: r.stale,
          on_demand: r.on_demand_per_gpu_hour, on_demand_match: r.on_demand_match, observed_at: r.observed_at, cells: {} };
        lines.set(key, ln);
      }
      const cur = ln.cells[r.term_bucket];
      // two rows of one offer in one bucket (Together 3-30 and 31-90 never share one): keep the cheaper
      if (!cur || r.price_per_gpu_hour < cur.price_per_gpu_hour) ln.cells[r.term_bucket] = r;
      if (ln.on_demand == null && r.on_demand_per_gpu_hour != null) { ln.on_demand = r.on_demand_per_gpu_hour; ln.on_demand_match = r.on_demand_match; }
      ln.stale = ln.stale || r.stale;
    }
    return [...lines.values()];
  }
  function groupByGpu(rows) {
    const out = new Map();
    for (const r of rows) {
      const g = r.gpu || "";
      if (!out.has(g)) out.set(g, []);
      out.get(g).push(r);
    }
    return [...out.entries()].sort((a, b) => (a[0] === "") - (b[0] === "") || a[0].localeCompare(b[0]));
  }
  function savingText(pct) {
    if (pct == null || !isFinite(pct)) return null;
    if (Math.abs(pct) < 0.05) return "same as on-demand";
    return pct > 0 ? `save ${pct.toFixed(1)}%` : `${Math.abs(pct).toFixed(1)}% more`;
  }

  OG.reservedPrices = { RULE, EMPTY, FILTERED, MATCH, termsIn, pivot, groupByGpu, savingText };
  if (typeof OG.page !== "function" || typeof document === "undefined") return;   // node tests: pure part only

  /* =============================== page =============================== */
  const priceCell = (r, opts) => {
    if (!r) return h("span", { class: "dim", title: "no published price for this term" }, "-");
    const s = savingText(r.saving_pct);
    const tip = [r.term_label + (r.term_price != null ? ` · whole term ${fmt.price(r.term_price).replace(/\.\d+$/, "")} for ${r.gpu_count} GPU${r.gpu_count > 1 ? "s" : ""}` : ""),
      r.derived ? `derived: ${r.discount_pct}% off the on-demand price` : "quoted by the provider",
      r.on_demand_per_gpu_hour != null ? `on-demand ${fmt.price(r.on_demand_per_gpu_hour)}/GPU-hr` : "no current on-demand price to compare"].join("\n");
    return h("span", { class: "rv-cell", title: tip },
      h("b", { class: "mono" }, fmt.price(r.price_per_gpu_hour)),
      opts && opts.provider ? h("span", { class: "dim" }, " ", OG.providerName(r.provider)) : null,
      s ? h("span", { class: "rv-save " + (r.saving_pct > 0 ? "up" : "down") }, " ", s) : null,
      r.derived ? h("span", { class: "dim" }, " ", OG.badge("derived", "", "Provider's published discount % applied to its on-demand price")) : null);
  };
  const onDemandCell = ln => (ln.on_demand == null
    ? OG.na(ln.scope === "gpu_only" ? "no on-demand price for this GPU SKU" : "no current on-demand listing for this machine size")
    : h("span", { class: "mono", title: "compared with " + (MATCH[ln.on_demand_match] || "-") }, fmt.price(ln.on_demand),
      ln.on_demand_match === "same_size" ? h("span", { class: "dim" }, " *") : null));

  function summaryTable(groups, terms) {
    const cols = termsIn(groups.flatMap(g => Object.keys(g.terms).map(k => ({ term_bucket: k }))), terms);
    return OG.table({ rows: groups, csv: "reserved-summary", sort: { key: "gpu", dir: "asc" }, columns: [
      { key: "gpu", label: "GPU", fmt: v => OG.gpuLink(v) },
      { key: "on_demand_min", label: "On-demand", num: true, title: "Cheapest on-demand price these reserved prices were compared with (same provider and size)", fmt: v => OG.value(v, fmt.price, "no matching on-demand price") },
      ...cols.map(t => ({ key: "t_" + t.key, label: t.label, num: true, title: `Cheapest effective $/GPU-hr for a commitment of ${t.label.toLowerCase()} (minimum term)`,
        value: g => (g.terms[t.key] ? g.terms[t.key].price_per_gpu_hour : null),
        fmt: (v, g) => priceCell(g.terms[t.key] ? Object.assign({ term_label: g.terms[t.key].term_label, gpu_count: g.terms[t.key].gpu_count }, g.terms[t.key]) : null, { provider: true }) })),
      { key: "providers", label: "Providers", fmt: v => v.map(p => OG.providerName(p)).join(", "), sort: false },
    ] });
  }

  function gpuTable(gpu, rows, terms) {
    const lines = pivot(rows), cols = termsIn(rows, terms);
    return OG.section(null,
      h("h2", { class: "sec-h" }, gpu ? OG.gpuLink(gpu, gpu) : "Not mapped to a canonical GPU yet"),
      OG.table({ rows: lines, compact: true, sort: { key: "provider", dir: "asc" }, columns: [
        { key: "provider", label: "Provider", fmt: v => OG.providerLink(v) },
        { key: "gpu_count", label: "Size", num: true, fmt: (v, r) => [`${v}× GPU`, r.scope === "gpu_only" ? h("span", {}, " ", OG.badge("GPU only", "warn", "GPU SKU alone: vCPUs, memory and disk are billed on top")) : null] },
        { key: "region", label: "Region", fmt: v => v || h("span", { class: "dim" }, "provider-wide") },
        { key: "sku", label: "SKU", cls: "mono dim", ell: 200, fmt: (v, r) => (gpu ? v : `${r.raw_gpu_name} · ${v}`) },
        { key: "on_demand", label: "On-demand", num: true, title: "The matching current on-demand $/GPU-hr (hover for how it was matched; * = other region)", fmt: (v, r) => onDemandCell(r) },
        ...cols.map(t => ({ key: "t_" + t.key, label: t.label, num: true, value: r => (r.cells[t.key] ? r.cells[t.key].price_per_gpu_hour : null), fmt: (v, r) => priceCell(r.cells[t.key]) })),
        { key: "method", label: "Source", cls: "dim", ell: 260, fmt: (v, r) => [r.stale ? OG.badge("stale", "warn", "source not refreshed within 3 polls") : null, r.stale ? " " : null, v], title: "How the effective price is obtained" },
      ] }));
  }

  OG.page("/reserved", {
    title: "Reserved prices",
    async mount(el, params, query, ctx) {
      const st = { provider: OG.qs.get("provider", ""), term: OG.qs.get("term", ""), gpu: OG.qs.get("gpu", "") };
      const bar = h("div", { class: "bar" }), top = h("div", {}), box = h("div", {}, OG.loading("Loading reserved prices…")), foot = h("div", {});
      el.append(OG.head("Reserved prices", "Committed prices per GPU-hour by term, from 1 month to 5 years, and the saving against the same provider's on-demand price. "),
        h("div", { class: "access-note k-public" }, h("b", {}, "Their own section"), h("span", {}, " " + RULE)),
        bar, top, box, foot,
        h("p", { class: "note" }, h("a", { class: "lnk", href: "/methodology/reserved-prices" }, "How reserved prices are computed →")));

      async function load() {
        let res, sum;
        try {
          [res, sum] = await Promise.all([
            ctx.api("/v1/reserved", { params: { provider: st.provider || null, term: st.term || null, gpu: st.gpu || null }, full: true, slot: "reserved" }),
            ctx.api("/v1/reserved/summary", { params: { gpu: st.gpu || null }, full: true, slot: "reserved-summary" })]);
        } catch (e) { if (!e.stale) box.replaceChildren(OG.error(e, load)); return; }
        const meta = res.meta || {}, terms = meta.terms || [];
        OG.status.asOf(meta.as_of);
        const gpus = [...new Set((sum.data || []).map(g => g.gpu))].sort();
        bar.replaceChildren(
          h("span", { class: "flt" }, h("span", {}, "GPU"), h("select", { class: "field", "aria-label": "GPU",
            onchange: e => { st.gpu = e.target.value; OG.qs.set({ gpu: st.gpu || null }); load(); } },
            h("option", { value: "" }, "all"), gpus.map(g => h("option", { value: g, selected: g === st.gpu ? "" : null }, OG.shortGpu ? OG.shortGpu(g) : g)))),
          h("span", { class: "flt" }, h("span", {}, "Provider"), h("select", { class: "field", "aria-label": "provider",
            onchange: e => { st.provider = e.target.value; OG.qs.set({ provider: st.provider || null }); load(); } },
            h("option", { value: "" }, "all"), (meta.providers || []).map(p => h("option", { value: p, selected: p === st.provider ? "" : null }, OG.providerName(p))))),
          h("span", { class: "flt" }, h("span", {}, "Term"), h("select", { class: "field", "aria-label": "term",
            onchange: e => { st.term = e.target.value; OG.qs.set({ term: st.term || null }); load(); } },
            h("option", { value: "" }, "all"), terms.map(t => h("option", { value: t.key, selected: t.key === st.term ? "" : null }, t.label)))),
          h("span", { class: "dim" }, `${res.data.length} prices${meta.derived ? ` (${meta.derived} derived)` : ""}`
            + (meta.source_as_of ? ` · sources read ${fmt.age(meta.source_as_of)} ago` : "")));

        const any = !st.provider && !st.term && !st.gpu;
        if (!res.data.length) {
          top.replaceChildren();
          box.replaceChildren(OG.empty(any ? EMPTY : FILTERED));
        } else {
          top.replaceChildren(!st.provider && !st.term && (sum.data || []).length
            ? OG.section("Cheapest per term", h("p", { class: "note" }, "Per GPU, the lowest effective price in each term across providers (GPU-only commitments left out: they exclude CPU, memory and disk). Columns are minimum commitments."), summaryTable(sum.data, terms))
            : null);
          box.replaceChildren(h("h2", { class: "sec-h" }, "Every reserved price, by GPU"),
            ...groupByGpu(res.data).map(([gpu, rows]) => gpuTable(gpu, rows, terms)));
        }
        const notes = meta.notes || [];
        foot.replaceChildren(OG.section("Sources", h("ul", { class: "note" },
          Object.entries(meta.sources || {}).map(([p, s]) => h("li", {}, h("b", {}, OG.providerName(p)), `: ${s.source}. ${s.method}.`)),
          notes.map(n => h("li", { class: "dim" }, n)))));
      }
      await load();
      ctx.every(300000, load);
    },
  });
})();
