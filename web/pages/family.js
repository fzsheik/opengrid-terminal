/* GPU family page, mounted by /gpu/:slug (pages/gpu.js) when the slug is a family (e.g. /gpu/h100,
   /gpu/blackwell) rather than one canonical GPU.

   A family groups materially different products (memory size, form factor, interconnect). OpenGrid never
   merges their prices: this page shows each variant side by side, each with its own price, and links to
   the variant's own market page. There is no family price, no family index and no family offer.

   Data: GET /v1/families/{family}  {id, slug, name, kind, architecture, note, variants: [{gpu, slug, low,
           median, high, providers, available_listings, index_id, index_level, change_24h}], cheapest_variant_now}
         GET /v1/history/{variant}?window=  per variant (soft: a variant without history is simply absent)
         GET /v1/news?gpu=<family>          (soft)
   Exposes OG.views.family(el, family, ctx). */
(() => {
  const { h, fmt, Lib: L } = OG;
  const WINDOWS = [["24h", "24H"], ["7d", "7D"], ["30d", "30D"], ["90d", "90D"]];
  const list = r => (Array.isArray(r) ? r : r && Array.isArray(r.items) ? r.items : []);
  const vslug = v => v.slug || OG.slug(v.gpu || "");
  // change_24h is {pct, reason, basis} (or a bare fraction from older servers)
  const chgPct = r => (r.change_24h && typeof r.change_24h === "object" ? r.change_24h.pct : r.change_24h);
  // Fixed per-variant colours by position in the family (never by price rank)
  const COLORS = ["#4d94ff", "#f5a524", "#2fbf71", "#e0729e", "#b39cff", "#4fc3d0", "#d7dde4", "#ef5350"];

  // A family object from /v1/families/{slug}, or from /v1/markets/{slug} when it resolves as a family
  OG.data.family = async (slug, api) => {
    const call = api || OG.api;
    const d = await call("/v1/families/" + encodeURIComponent(slug)).catch(() => null);
    if (d && Array.isArray(d.variants)) return d;
    const m = await call("/v1/markets/" + encodeURIComponent(slug), { full: true }).catch(() => null);
    if (m && m.meta && m.meta.resolved_as === "family" && m.data && Array.isArray(m.data.variants)) return m.data;
    return null;
  };

  function family(el, fam, ctx) {
    el.classList.add("fam");
    ctx.onCleanup(() => el.classList.remove("fam"));
    const name = fam.name || fam.slug;
    ctx.setTitle(name + " prices");
    const isArch = fam.kind === "architecture";
    const variants = fam.variants || [];
    const colorOf = new Map(variants.map((v, i) => [vslug(v), COLORS[i % COLORS.length]]));
    const st = { win: ["24h", "7d", "30d", "90d"].includes(OG.qs.get("w")) ? OG.qs.get("w") : "7d" };

    const chartEl = h("div", { class: "fam-chart" }, OG.loading("Loading each variant's history…"));
    const chartNote = h("p", { class: "note" });
    const newsBox = h("div", {}, OG.loading());
    const winSeg = OG.seg(WINDOWS, st.win, v => { st.win = v; OG.qs.set({ w: v === "7d" ? null : v }); loadChart(); });

    const badges = [isArch ? null : fam.architecture].filter(Boolean).map(t => OG.badge(String(t).replace(/_/g, " ")));
    const cheap = fam.cheapest_variant_now;
    const priced = variants.filter(v => v.low != null);
    el.append(
      OG.head(h("span", { class: "gpu-title" }, h("span", { class: "eyebrow" }, isArch ? "GPU architecture" : "GPU family"), name, h("span", { class: "gp-badges" }, badges),
        h("span", { class: "full" }, `${variants.length} variant${variants.length === 1 ? "" : "s"}`)), null,
        variants.length >= 2 ? h("a", { class: "btn", href: `/compare/${vslug(variants[0])}-vs-${vslug(variants[1])}` }, "Compare variants") : null,
        h("a", { class: "btn pri", href: "/route?gpu=" + encodeURIComponent(fam.slug || fam.id), title: "Route with this family: pick a variant or allow variants" }, "Route →")),
      h("div", { class: "fam-note", role: "note" }, h("b", {}, "Variants are different products. "),
        "Memory size, form factor and interconnect differ, so OpenGrid never merges their prices into one family price; each row below is its own market.",
        fam.note && !/never merged/i.test(fam.note) ? " " + fam.note : "",
        isArch && fam.member_families && fam.member_families.length ? h("span", { class: "dim" }, " Model families in this architecture: ", fam.member_families.map((m, i) => [i ? ", " : "", h("a", { class: "lnk", href: "/gpu/" + OG.slug(m) }, m)])) : null),
      OG.stats([
        { label: "Variants", value: String(variants.length), sub: `${priced.length} priced now` },
        { label: "Cheapest variant now", value: cheap && cheap.low != null ? fmt.price(cheap.low) : null, kind: "observed",
          reason: priced.length ? "not reported" : "no variant is priced now",
          sub: cheap ? OG.shortGpu(cheap.gpu || cheap.slug || "") + (cheap.provider ? " · " + OG.providerName(cheap.provider) : "") : null,
          title: "The lowest observed on-demand price of one variant, not a family price" },
        { label: "Indexed variants", value: String(variants.filter(v => v.index_level != null).length), sub: "each with its own index" },
      ]),
      OG.section("Variants side by side", variantsTable(variants)),
      h("section", { class: "sec" }, h("div", { class: "gp-sh" }, h("h2", { class: "sec-h" }, "Lowest price per variant"), h("span", { class: "spacer" }), winSeg), chartEl, chartNote),
      OG.section(`News mentioning the ${name}`, newsBox));

    function variantsTable(rows) {
      return OG.table({
        columns: [
          { key: "gpu", label: "Variant", value: r => OG.shortGpu(r.gpu || r.slug), fmt: (v, r) => h("span", { class: "lnk prov" }, h("i", { class: "sw", style: `background:${colorOf.get(vslug(r))}` }), h("a", { class: "lnk", href: "/gpu/" + vslug(r), title: r.gpu }, h("b", {}, OG.shortGpu(r.gpu || r.slug)))) },
          { key: "low", label: "Low $/GPU·h", num: true, fmt: (v, r) => (v == null ? OG.na(r.reason || "no in-stock on-demand listing now") : h("b", {}, fmt.price(v))), title: "Lowest observed on-demand price of this variant" },
          { key: "low_provider", label: "at", hidden: !rows.some(r => r.low_provider), fmt: v => (v ? OG.providerLink(v) : "–") },
          { key: "median", label: "Median", num: true, fmt: v => OG.value(v, fmt.price, "no priced providers") },
          { key: "high", label: "High", num: true, fmt: v => OG.value(v, fmt.price, "no priced providers") },
          { key: "change_24h", label: "24h", num: true, value: r => chgPct(r), fmt: (v, r) => OG.chg(chgPct(r), { reason: (r.change_24h && r.change_24h.reason) || "not enough history" }), title: "24-hour change of this variant's own index level" },
          { key: "providers", label: "Providers", num: true, desc: true },
          { key: "available_listings", label: "Available", num: true, desc: true, title: "Listings with explicit availability now" },
          { key: "index_level", label: "Index", num: true, fmt: (v, r) => (v != null && r.index_id ? h("a", { class: "lnk", href: "/indices/" + r.index_id }, fmt.price(v)) : OG.na("no published index for this variant")), title: "The variant's own OpenGrid index level" },
          { key: "open", label: "", sort: false, csv: false, fmt: (v, r) => h("a", { class: "lnk dim", href: "/gpu/" + vslug(r) }, "market →") },
        ],
        rows, sort: { key: "low", dir: "asc" }, rowKey: r => vslug(r), rowHref: r => "/gpu/" + vslug(r), compact: true,
        csv: `opengrid-${fam.slug || fam.id}-variants.csv`, empty: "This family has no variants listed.",
        onHover: r => chart && chart.highlight(r ? vslug(r) : null),
      });
    }

    let chart = null;
    ctx.onCleanup(() => chart && chart.destroy());
    async function loadChart() {
      const win = st.win;
      const hists = await Promise.all(variants.map(v => ctx.api("/v1/history/" + vslug(v), { params: { window: win } }).catch(() => null)));
      if (win !== st.win) return;
      const withData = variants.map((v, i) => ({ v, s: hists[i] && Array.isArray(hists[i].series) ? hists[i].series : [] })).filter(x => x.s.some(p => p.lowest != null));
      if (chart) { chart.destroy(); chart = null; }
      if (!withData.length) { chartEl.replaceChildren(OG.insufficient("No variant has hourly price history in this window yet.", "No history")); chartNote.replaceChildren(); return; }
      const times = [...new Set(withData.flatMap(x => x.s.map(p => p.t)))].sort();
      const series = withData.map(x => {
        const by = new Map(x.s.map(p => [p.t, p.lowest]));
        return { key: vslug(x.v), label: OG.shortGpu(x.v.gpu || x.v.slug), values: times.map(t => by.get(t) ?? null), color: colorOf.get(vslug(x.v)), width: 1.5 };
      });
      chartEl.textContent = "";
      chart = OG.charts.timeseries(chartEl, { times, series, height: 240, label: `Lowest price per ${name} variant`, emptyText: "No history in this window",
        gapReason: i => { const miss = series.filter(s => s.values[i] == null).map(s => s.label); return miss.length ? "no recorded price: " + miss.join(", ") : null; } });
      const missing = variants.length - withData.length;
      chartNote.replaceChildren(OG.kindBadge("observed"), " One line per variant: its own lowest observed on-demand price per GPU-hour, hourly. Lines are never averaged into a family line. ",
        missing ? `${missing} variant${missing === 1 ? "" : "s"} without history in this window. ` : "", h("a", { class: "lnk", href: "/methodology/indices" }, "Method"));
    }

    async function loadNews() {
      const r = await ctx.api("/v1/news", { params: { gpu: fam.slug || fam.id, limit: 12 } }).catch(e => ({ error: e }));
      if (r && r.error) { newsBox.replaceChildren(r.error.status === 404 ? OG.insufficient("News is not available on this server.", "Unavailable") : OG.error(r.error, loadNews)); return; }
      const items = list(r);
      if (!items.length) { newsBox.replaceChildren(OG.empty(`No news item mentioning the ${name} has been ingested yet.`)); return; }
      newsBox.replaceChildren(h("ul", { class: "gp-list gp-news" }, items.map(n => h("li", {},
        h("span", { class: "mono dim" }, fmt.date(n.published_at)), OG.badge(n.trust_tier || "source", n.trust_tier === "official" ? "good" : ""),
        h("span", {}, h("a", { class: "lnk", href: n.url, target: "_blank", rel: "external noopener" }, n.title),
          h("span", { class: "dim" }, " · " + (n.source_name || n.source_id || "") + (n.source_count > 1 ? ` +${n.source_count - 1}` : "")))))),
        h("p", { class: "note" }, "Linked by mention of the family or one of its variants (inferred by rules); not a claim about any price. ",
          h("a", { class: "lnk", href: "/news?gpu=" + encodeURIComponent(fam.slug || fam.id) }, "More →")));
    }

    loadChart(); loadNews();
    ctx.every(300000, loadChart);
  }

  OG.views = OG.views || {};
  OG.views.family = family;
})();
