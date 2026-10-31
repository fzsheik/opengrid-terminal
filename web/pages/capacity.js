/* /capacity: provider-reported capacity, one row per (provider, SKU, region), from /v1/capacity.
   Bare minimum on purpose: a provider filter, an in-stock toggle, and the shared table (sort, CSV). */
(() => {
  const { h, fmt } = OG;
  const stockBadge = v => (v === true ? h("span", { class: "dotx good" }, "in stock") : v === false ? OG.badge("out", "bad") : OG.na("not reported"));

  OG.page("/capacity", {
    title: "Capacity",
    async mount(el, params, query, ctx) {
      const st = { provider: OG.qs.get("provider", ""), stock: OG.qs.get("stock", "") };
      const bar = h("div", { class: "bar" }), box = h("div", {}, OG.loading("Loading capacity…"));
      el.append(OG.head("Capacity", "What each provider says it can supply right now, by SKU and region. The provider's own word is kept in the last column. "), bar, box,
        h("p", { class: "note" }, h("a", { class: "lnk", href: "/methodology/capacity" }, "Methodology →")));

      async function load() {
        let res;
        try {
          res = await ctx.api("/v1/capacity", { params: { provider: st.provider || null, in_stock: st.stock || null }, full: true, slot: "capacity" });
        } catch (e) { if (!e.stale) box.replaceChildren(OG.error(e, load)); return; }
        OG.status.asOf(res.meta && res.meta.as_of);
        bar.replaceChildren(
          h("span", { class: "flt" }, h("span", {}, "Provider"), h("select", { class: "field", "aria-label": "provider",
            onchange: e => { st.provider = e.target.value; OG.qs.set({ provider: st.provider || null }); load(); } },
            h("option", { value: "" }, "all"), (res.meta.providers || []).map(p => h("option", { value: p, selected: p === st.provider ? "" : null }, OG.providerName(p))))),
          h("span", { class: "flt" }, h("span", {}, "Stock"), OG.seg([["", "All"], ["true", "In stock"], ["false", "Out"]], st.stock,
            v => { st.stock = v; OG.qs.set({ stock: v || null }); load(); })),
          h("span", { class: "dim" }, `${res.data.length} rows`));
        box.replaceChildren(OG.table({ rows: res.data, csv: "capacity", sort: { key: "provider", dir: "asc" }, columns: [
          { key: "provider", label: "Provider", fmt: v => OG.providerLink(v) },
          { key: "gpu", label: "GPU", fmt: (v, r) => v || h("span", { class: "dim", title: "not mapped to a canonical GPU yet" }, r.raw_gpu_name) },
          { key: "gpu_count", label: "Per machine", num: true, fmt: v => `${v}× GPU`, title: "Machine size: GPUs in one instance of this SKU. Not how many the provider has; that is the Free column, where reported." },
          { key: "sku", label: "SKU", cls: "mono" },
          { key: "region", label: "Region", fmt: v => v || h("span", { class: "dim" }, "provider-wide") },
          { key: "in_stock", label: "Stock", fmt: v => stockBadge(v), value: r => (r.in_stock === true ? 2 : r.in_stock === false ? 0 : 1) },
          { key: "tier", label: "Tier", fmt: v => v || "-" },
          { key: "available_count", label: "Free", num: true, title: "How much is free right now. Only some providers report a number (Hyperstack: VMs; Lium: GPUs free / total); the rest only say in stock or out.", fmt: (v, r) => (v == null ? "-" : r.total_count != null ? `${v} / ${r.total_count}` : `${v} ${r.count_unit === "instance" ? "VMs" : ""}`) },
          { key: "raw_value", label: "Provider's word", cls: "mono dim", ell: 220 },
          { key: "observed_at", label: "Updated", fmt: (v, r) => (r.stale ? OG.badge("stale", "warn", "not refreshed within 3 polls") : fmt.age(v) + " ago") },
        ] }));
      }
      await load();
      ctx.every(120000, load);
    },
  });
})();
