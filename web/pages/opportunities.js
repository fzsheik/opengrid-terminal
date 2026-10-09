/* /opportunities — the opportunity monitor (/v1/opportunities, computed live, cached 60 s).
   Tabs by type (with counts), sorted by score; each item: score, explanation, the numbers, observed/inferred,
   links. Filters (type, gpu, provider, min) live in the URL. Types the server could not evaluate are listed with
   the reason. These are observations about listed prices, not advice and not quotes. */
(() => {
  const { h, fmt, Lib: L } = OG;
  const LABEL = {
    wide_spread: "Wide spread", price_cut: "Price cut", new_cheap_inventory: "New cheap inventory",
    regional_dislocation: "Regional dislocation", below_usual_premium: "Below usual premium", scarcity: "Scarcity",
    new_cheapest_provider: "New cheapest provider", supply_change: "Supply change",
  };
  const label = t => LABEL[t] || String(t).replace(/_/g, " ");
  const pctAbs = v => v == null ? "–" : fmt.pct(v).replace(/^\+/, "");
  const MIN = [["0", "ANY"], ["25", "25+"], ["50", "50+"], ["75", "75+"]];

  // The numbers behind each type, as [label, value-node] pairs.
  function nums(it) {
    const n = it.numbers || {}, P = fmt.price, pv = p => p ? OG.providerLink(p, { logo: false }) : null;
    switch (it.type) {
      case "wide_spread": return [["spread", pctAbs(n.spread)], ["low", [P(n.lowest), " ", pv(n.lowest_provider)]], ["median", P(n.median)], ["high", [P(n.highest), " ", pv(n.highest_provider)]],
        n.p90_30d != null ? ["30d p90", pctAbs(n.p90_30d)] : ["baseline", h("span", { class: "dim" }, n.baseline || "none")], ["providers", String(n.providers)]];
      case "price_cut": return [["24h ago", P(n.price_24h_ago)], ["now", P(n.price)], ["chg", OG.chg(n.pct)]];
      case "new_cheap_inventory": return [["price", P(n.price)], ["mkt median", P(n.market_median)], ["vs median", OG.chg(n.discount)], ["region", n.region || "–"], ["GPUs", String(n.gpu_count ?? "–")], ["first seen", fmt.dateTime(n.first_seen)]];
      case "regional_dislocation": return [["group median", P(n.group_median)], ["global median", P(n.global_median)], ["gap", OG.chg(n.gap)], ["providers", `${n.group_providers}/${n.providers}`]];
      case "below_usual_premium": return [["price", P(n.price)], ["mkt median", P(n.market_median)], ["premium now", fmt.pct(n.premium_now)], ["30d avg", fmt.pct(n.premium_30d)], ["Δ pts", fmt.num((n.delta || 0) * 100, 1)], ["samples", String(n.samples)]];
      case "scarcity": return [["available", String(n.available_listings)], ["30d avg", fmt.num(n.average_30d, 1)], ["ratio", pctAbs(n.ratio)], ["providers avail", String(n.providers_available ?? "–")]];
      case "new_cheapest_provider": return [["price", P(n.price)], ["was", [P(n.previous_price), " ", pv(n.previous_provider)]], ["since", fmt.dateTime(n.since)]];
      case "supply_change": return [["providers", `${n.providers_priced_then} → ${n.providers_priced_now}`], ["listings", `${n.available_listings_then} → ${n.available_listings_now}`], ["matched", String(n.providers_matched)]];
      default: return Object.entries(n).filter(([, v]) => typeof v === "number" || typeof v === "string").slice(0, 6).map(([k, v]) => [k.replace(/_/g, " "), typeof v === "number" ? fmt.num(v, 3) : v]);
    }
  }
  const numsCell = it => h("span", { class: "op-nums" }, nums(it).map(([k, v]) => h("span", { class: "op-kv" }, h("i", {}, k), h("b", {}, v))));
  const scoreCell = s => h("span", { class: "op-sc", title: "score 0–100: larger or more unusual is higher (see methodology)" },
    h("span", { class: "op-sc-b" }, h("i", { style: { width: Math.max(2, Math.min(100, s)) + "%" } })), h("b", {}, fmt.num(s, 0)));

  function table(items, showType, title) {
    // one data kind for the whole block: say it once in the block title, not on every row
    const kinds = new Set(items.map(i => i.kind)), uniform = kinds.size === 1 && items.length > 0;
    if (uniform && title && title.nodeType) title.append(OG.kindBadge(items[0].kind));
    return OG.table({
      columns: [
        { key: "score", label: "Score", num: true, desc: true, fmt: v => scoreCell(v), width: "84px" },
        { key: "type", label: "Type", hidden: !showType, fmt: v => h("span", { class: "op-ty" }, label(v)) },
        { key: "gpu", label: "GPU", width: "136px", fmt: (v, r) => v ? OG.gpuLink(v) : "–", value: r => r.gpu ? L.shortGpu(r.gpu) : null },
        { key: "provider", label: "Provider / region", width: "128px", fmt: (v, r) => v ? OG.providerLink(v) : r.region_group ? h("span", { class: "op-rg" }, r.region_group) : h("span", { class: "dimmer" }, "market"), value: r => r.provider_name || r.region_group },
        { key: "explanation", label: "Why it is listed", cls: "wrap op-ex", sort: false },
        { key: "numbers", label: "Numbers", cls: "wrap", width: "30%", sort: false, fmt: (v, r) => numsCell(r), csv: r => JSON.stringify(r.numbers) },
        { key: "kind", label: "Kind", width: "92px", hidden: uniform, fmt: v => OG.kindBadge(v) },
        { key: "links", label: "", width: "92px", sort: false, csv: false, fmt: (v, r) => h("span", { class: "op-lk" },
          r.gpu_slug ? h("a", { class: "lnk", href: "/events?gpu=" + r.gpu_slug + (r.provider ? "&provider=" + encodeURIComponent(r.provider) : ""), title: "events for this market" }, "events") : null,
          r.gpu_slug ? h("a", { class: "lnk", href: "/route?gpu=" + r.gpu_slug, title: "preview routing for this GPU" }, "route") : null) },
      ],
      rows: items, sort: { key: "score", dir: "desc" }, compact: true, csv: "opengrid-opportunities.csv", title,
      empty: "Nothing of this type right now.",
    });
  }

  OG.page("/opportunities", {
    title: "Opportunities",
    async mount(el, params, query, ctx) {
      el.classList.add("pg-opps");
      const st = { type: OG.qs.get("type", ""), gpu: OG.qs.get("gpu", ""), provider: OG.qs.get("provider", ""), min: OG.qs.get("min", "0"), data: null, cat: null };
      if (!MIN.some(m => m[0] === st.min)) st.min = "0";
      const asof = h("span", { class: "mono dim" });
      const tabsBox = h("div", { class: "op-tabs" });
      const body = h("div", { class: "op-body" }, OG.loading("Evaluating the market…"));
      const unBox = h("div", { class: "op-un" });
      const gpuSel = h("select", { class: "field", "aria-label": "GPU", onchange: e => set({ gpu: e.target.value }) }, h("option", { value: "" }, "All GPUs"));
      const provSel = h("select", { class: "field", "aria-label": "Provider", onchange: e => set({ provider: e.target.value }) }, h("option", { value: "" }, "All providers"));
      const minSeg = OG.seg(MIN, st.min, v => set({ min: v }));
      el.append(
        OG.head("Opportunity monitor", "Where listed prices or supply look unusual right now, ranked by score. Observations about listed prices, not advice or quotes: a listing can change or sell out before you act.",
          asof, h("a", { class: "btn", href: "/methodology/opportunities" }, "Methodology")),
        h("div", { class: "bar" },
          h("label", { class: "flt" }, h("span", {}, "GPU"), gpuSel),
          h("label", { class: "flt" }, h("span", {}, "Provider"), provSel),
          h("span", { class: "flt" }, h("span", {}, "Min score"), minSeg),
          h("span", { class: "spacer" }),
          h("button", { class: "btn sm", onclick: () => set({ type: "", gpu: "", provider: "", min: "0" }) }, "Reset")),
        tabsBox, body, unBox);

      function set(patch) {
        const refetch = ["gpu", "provider", "min"].some(k => k in patch && patch[k] !== st[k]);
        Object.assign(st, patch);
        OG.qs.set({ type: st.type || null, gpu: st.gpu || null, provider: st.provider || null, min: st.min === "0" ? null : st.min });
        gpuSel.value = st.gpu; provSel.value = st.provider; minSeg.set(st.min);
        if (refetch) load(); else draw();
      }
      const desc = t => { const c = st.cat && st.cat.find(x => x.type === t); return c ? c.description : ""; };
      const allTypes = () => st.cat ? st.cat.map(c => c.type) : Object.keys(LABEL);

      const groupTitle = (t, n, link) => h("span", { class: "op-gh" },
        link ? h("button", { class: "lnk op-gt", title: "Show only this type", onclick: () => set({ type: t }) }, label(t)) : h("span", { class: "op-gt" }, label(t)),
        h("span", { class: "op-gn" }, String(n)), h("span", { class: "op-gd" }, desc(t)));
      function draw() {
        const d = st.data; if (!d) return;
        const items = d.items || [];
        const unav = new Map((d.unavailable || []).map(u => [u.type, u.reason]));
        const counts = new Map();
        for (const it of items) counts.set(it.type, (counts.get(it.type) || 0) + 1);
        const types = allTypes();
        if (st.type && !types.includes(st.type)) st.type = "";
        tabsBox.replaceChildren(OG.tabs([["", `All · ${items.length}`], ...types.map(t => [t, `${label(t)} · ${unav.has(t) ? "n/a" : counts.get(t) || 0}`, unav.get(t) || desc(t)])], st.type, v => set({ type: v })));
        if (st.type) {
          const list = items.filter(x => x.type === st.type);
          body.replaceChildren(unav.has(st.type) ? OG.insufficient(unav.get(st.type), "Could not evaluate " + label(st.type).toLowerCase()) : table(list, false, groupTitle(st.type, list.length)));
        } else if (!items.length) {
          body.replaceChildren(OG.empty(d.as_of_hour ? "No opportunities pass the rules right now" + (st.gpu || st.provider || st.min !== "0" ? " for these filters." : ".") : "No market history yet: the hourly rollup has not run."));
        } else {
          // grouped: one block per type that has items, in order of its best score
          const groups = types.filter(t => counts.get(t)).sort((a, b) => Math.max(...items.filter(x => x.type === b).map(x => x.score)) - Math.max(...items.filter(x => x.type === a).map(x => x.score)));
          body.replaceChildren(...groups.map(t => h("section", { class: "op-g" }, table(items.filter(x => x.type === t), false, groupTitle(t, counts.get(t), true)))));
        }
        const evaluatedEmpty = types.filter(t => !unav.has(t) && !counts.get(t));
        unBox.replaceChildren(...[
          d.unavailable && d.unavailable.length ? h("div", { class: "op-unb" }, h("h2", { class: "sec-h" }, "Could not evaluate"),
            h("table", { class: "grid-t compact" }, h("tbody", {}, d.unavailable.map(u => h("tr", {}, h("td", { class: "op-ty" }, label(u.type)), h("td", { class: "wrap dim" }, u.reason)))))) : null,
          evaluatedEmpty.length ? h("p", { class: "note" }, h("b", {}, "Evaluated, nothing found: "), evaluatedEmpty.map(label).join(", "), ".") : null].filter(Boolean));
      }

      async function load() {
        const p = { limit: 500 };
        if (st.gpu) p.gpu = st.gpu;
        if (st.provider) p.provider = st.provider;
        if (st.min !== "0") p.min_score = st.min;
        try {
          const r = await ctx.api("/v1/opportunities", { params: p, full: true, slot: "opps" });
          st.data = r.data;
          asof.textContent = r.data.as_of_hour ? `hour ${fmt.dateTime(r.data.as_of_hour)} · ${r.data.total} item${r.data.total === 1 ? "" : "s"}` : "no market history";
          if (r.meta) OG.status.asOf(r.meta.as_of);
          draw();
        } catch (e) { if (!e.stale) body.replaceChildren(OG.error(e, load)); }
      }

      const [cat, gpus] = await Promise.all([ctx.api("/v1/events/types").catch(() => null), OG.data.gpus().catch(() => [])]);
      st.cat = cat && cat.opportunities;
      gpuSel.append(...gpus.filter(g => g.live || g.slug === st.gpu).sort((a, b) => a.short.localeCompare(b.short)).map(g => h("option", { value: g.slug }, g.short)));
      provSel.append(...OG.data.providers().sort((a, b) => a.display_name.localeCompare(b.display_name)).map(p => h("option", { value: p.name }, p.display_name)));
      gpuSel.value = st.gpu; provSel.value = st.provider;
      load();
      ctx.every(60000, load);
    },
  });
})();
