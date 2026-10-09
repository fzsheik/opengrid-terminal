/* /news — News & signals. Story-clustered items from /v1/news, filters in the URL so provider-, GPU- and
   region-specific views are just links (/news?provider=vast, /news?gpu=h100, /news?region=Europe).
   Relevance components come from the list item (else /v1/news/{id}) on hover; family suggestions from /v1/news/families. Source health from /v1/news/sources, topic
   catalogue from /v1/news/topics. view=timeline overlays /v1/timeline for one GPU: prices, availability,
   market events and news on one axis — related in time, not necessarily causal. */
(() => {
  const { h, fmt, Lib: L } = OG;
  const REGIONS = ["US", "Canada", "Europe", "UK", "APAC", "Middle East", "LATAM", "Africa"];
  // Suggestions only; the server validates (GPU slug / canonical name, or a news GPU family)
  let FAMILIES = ["H100", "H200", "H20", "GH200", "B200", "B300", "GB200", "GB300", "Rubin", "A100", "L40S", "L4", "RTX 4090", "RTX 5090", "RTX PRO 6000", "MI300X", "MI325X", "MI355X", "Gaudi", "Blackwell", "Hopper"];
  const FILTERS = ["gpu", "provider", "region", "topic", "source", "min", "q"];
  const PAGE = 40, DEFAULT_MIN = 20;   // below 20 is mostly general semiconductor news; "all" shows it
  const relCls = r => (r >= 60 ? "r-hi" : r >= 35 ? "r-mid" : "r-lo");
  // Feed links are third-party data: only absolute http(s) URLs become links (no javascript:/data:).
  const safeHref = u => (typeof u === "string" && /^https?:\/\/[^\s]/i.test(u.trim()) ? u.trim() : null);

  OG.page("/news", {
    title: "News",
    async mount(el, params, query, ctx) {
      el.classList.add("nw-page");
      const f = {};
      for (const k of FILTERS) f[k] = OG.qs.get(k) || "";
      const st = { view: OG.qs.get("view", "feed"), items: [], total: 0, offset: 0, topics: null, sources: null, win: OG.qs.get("window", "7d") };
      if (!["7d", "30d", "24h"].includes(st.win)) st.win = "7d";

      const viewSeg = OG.seg([["feed", "Feed"], ["timeline", "Timeline"]], st.view, v => { st.view = v; OG.qs.set({ view: v === "feed" ? null : v }); drawView(); });
      const famLabel = new Map();
      const fbar = h("div", { class: "bar nw-f" }), chipsEl = h("div", { class: "nw-chips" }), activeEl = h("div", { class: "nw-active" });
      const feedEl = h("div", {}), moreEl = h("div", { class: "nw-more" }), countEl = h("span", { class: "dim nw-count" });
      const tlEl = h("div", {}), srcEl = h("div", {}, OG.loading()), topicEl = h("div", {}, OG.loading());
      const left = h("div", {}), right = h("div", {}, OG.section("Source health", srcEl), OG.section("Topics · 30d", topicEl));
      el.append(OG.head("News & signals", "GPU-compute news, story-clustered, tagged with the GPUs, providers and regions it mentions. Related to market moves by time and entity only — never presented as a cause.", viewSeg),
        fbar, chipsEl, activeEl, h("div", { class: "cols nw-cols" }, left, right));

      // ------------------------------------------------------------ filters
      const input = (k, ph, w, list) => h("input", { class: "field", value: f[k], placeholder: ph, style: `width:${w}px`, list: list || null, "aria-label": ph,
        onchange: e => setF({ [k]: e.target.value.trim() }), onkeydown: e => { if (e.key === "Enter") setF({ [k]: e.target.value.trim() }); } });
      const select = (k, label, opts) => h("select", { class: "field", "aria-label": label, onchange: e => setF({ [k]: e.target.value }) },
        h("option", { value: "" }, label), opts.map(([v, t]) => h("option", { value: v, selected: String(v) === String(f[k]) ? "" : null }, t)));
      function drawFilters() {
        const gpus = (OG.boot.gpus || []).map(([s, n]) => [s, L.shortGpu(n)]);
        const provs = OG.data.providers().map(p => [p.name, p.display_name]).sort((a, b) => a[1].localeCompare(b[1]));
        if (f.provider && !provs.some(p => p[0] === f.provider)) provs.unshift([f.provider, f.provider]);
        const srcs = (st.sources || []).filter(s => s.enabled).map(s => [s.id, s.name]).sort((a, b) => a[1].localeCompare(b[1]));
        fbar.replaceChildren(...[
          h("datalist", { id: "nw-gpus" }, [...FAMILIES.map(x => h("option", { value: x }, famLabel.get(x) || "family")), ...gpus.map(([s, t]) => h("option", { value: s }, t))]),
          h("span", { class: "flt" }, h("span", {}, "GPU"), input("gpu", "GPU or family", 130, "nw-gpus")),
          h("span", { class: "flt" }, h("span", {}, "Provider"), select("provider", "any", provs)),
          h("span", { class: "flt" }, h("span", {}, "Region"), select("region", "any", REGIONS.map(r => [r, r]))),
          h("span", { class: "flt" }, h("span", {}, "Source"), select("source", "any", srcs)),
          h("span", { class: "flt" }, h("span", {}, "Min rel."), OG.seg([["1", "all"], ["", "20"], ["40", "40"], ["60", "60"]], f.min, v => setF({ min: v }), "seg")),
          h("span", { class: "flt" }, h("span", {}, "Search"), input("q", "words in title / summary", 190)),
          h("span", { class: "spacer" }),
          FILTERS.some(k => f[k]) ? h("button", { class: "btn sm", onclick: () => setF(Object.fromEntries(FILTERS.map(k => [k, ""]))) }, "Clear") : null].filter(Boolean));
        chipsEl.replaceChildren(...(st.topics || []).filter(t => t.items_30d > 0 || t.id === f.topic).map(t => h("button", { class: "chip", "aria-pressed": String(f.topic === t.id), title: `${t.description} · weight ${t.weight} · ${t.items_30d} items in 30 days`,
          onclick: () => setF({ topic: f.topic === t.id ? "" : t.id }) }, t.id.replace(/_/g, " "), h("span", { class: "dimmer" }, " " + t.items_30d))));
        const act = FILTERS.filter(k => f[k]);
        activeEl.replaceChildren(...(act.length ? [h("span", { class: "dim" }, "Link to this view: "), h("a", { class: "lnk mono", href: location.pathname + location.search }, location.pathname + location.search)] : []));
      }
      function setF(patch) {
        Object.assign(f, patch);
        OG.qs.set(Object.fromEntries(Object.entries(patch).map(([k, v]) => [k, v || null])));
        drawFilters(); drawView();
      }

      // ------------------------------------------------------------ feed
      function apiParams(offset) {
        const p = { limit: PAGE, offset };
        if (f.gpu) p.gpu = f.gpu;
        if (f.provider) p.provider = f.provider;
        if (f.region) p.region = f.region;
        if (f.topic) p.topic = f.topic;
        if (f.source) p.source = f.source;
        if (f.q) p.q = f.q;
        p.min_relevance = f.min || DEFAULT_MIN;
        return p;
      }
      async function loadFeed(append) {
        if (!append) { st.offset = 0; feedEl.replaceChildren(OG.loading("Loading news…")); }
        let res;
        try { res = await ctx.api("/v1/news", { params: apiParams(st.offset), full: true, slot: "news" }); }
        catch (e) { if (!e.stale) feedEl.replaceChildren(OG.error(e, () => loadFeed())); moreEl.replaceChildren(); return; }
        const items = res.data || [];
        st.total = (res.meta && res.meta.pagination && res.meta.pagination.total) || items.length;
        st.items = append ? st.items.concat(items) : items;
        st.offset += items.length;
        if (!append) feedEl.replaceChildren();
        if (!st.items.length) feedEl.append(OG.empty("No stored news matches these filters."));
        feedEl.append(...items.map(item));
        countEl.textContent = `${fmt.num(st.items.length)} of ${fmt.num(st.total)} stories`;
        moreEl.replaceChildren(st.items.length < st.total ? h("button", { class: "btn sm", onclick: () => loadFeed(true) }, `Load ${Math.min(PAGE, st.total - st.items.length)} more`) : "");
      }
      const flink = (k, v, label, cls) => h("a", { class: "nw-ent " + (cls || ""), href: "/news" + OG.qs.stringify({ [k]: v }), title: `All news mentioning ${label}` }, label);
      function item(n) {
        // relevance reads at a glance: the score, a meter, and low-relevance stories set back (ranking is the API's)
        const rel = h("span", { class: "nw-rel " + relCls(n.relevance), tabindex: "0", "aria-label": "relevance " + n.relevance + " of 100" },
          h("b", {}, String(n.relevance)), h("i", { class: "nw-relm" }, h("i", { style: `width:${Math.max(4, Math.min(100, n.relevance))}%` })));
        const pop = h("div", { class: "nw-pop", hidden: true });
        let loaded = false;
        const show = async () => {
          pop.hidden = false;
          if (loaded) return;
          loaded = true;
          pop.replaceChildren(OG.loading("components…"));
          // the list item carries its components when the API includes them: no per-item request then
          const d = n.relevance_components ? n : await OG.api.soft("/v1/news/" + n.id);
          const c = d && d.relevance_components;
          if (!c) { pop.replaceChildren(h("div", { class: "dim" }, "components unavailable")); return; }
          const tb = (d.entities && d.entities.topic_basis) || {};
          pop.replaceChildren(h("div", { class: "ct-t" }, "Relevance ", h("b", {}, String(d.relevance)), " / 100 · inferred"),
            h("div", { class: "ct-r" }, h("span", {}, "entity points"), h("span", {}, fmt.num(c.entity_points, 1))),
            h("div", { class: "ct-r" }, h("span", {}, "topic points"), h("span", {}, fmt.num(c.topic_points, 1))),
            h("div", { class: "ct-r" }, h("span", {}, "source tier ×"), h("span", {}, fmt.num(c.tier_mult, 2) + " (" + d.trust_tier + ")")),
            h("div", { class: "ct-r" }, h("span", {}, "recency ×"), h("span", {}, fmt.num(c.recency_mult, 2))),
            h("div", { class: "ct-d nw-form" }, c.formula),
            Object.keys(tb).length ? h("div", { class: "ct-d" }, "topics from: " + Object.entries(tb).map(([k, v]) => `${k} (${v})`).join(", ")) : null,
            d.entities && (d.entities.compute_terms || []).length ? h("div", { class: "ct-d" }, "terms: " + d.entities.compute_terms.join(", ")) : null,
            h("a", { class: "lnk ct-d", href: "/methodology/news" }, "methodology →"));
        };
        const wrap = h("div", { class: "nw-relw", onmouseenter: show, onmouseleave: () => { pop.hidden = true; }, onfocusin: show, onfocusout: () => { pop.hidden = true; } }, rel, pop);
        const ents = [
          ...(n.gpus || []).map(g => OG.gpuLink(g)),
          ...(n.gpu_families || []).map(g => flink("gpu", g, g, "fam")),
          ...(n.providers || []).map(p => OG.providerMeta(p) || L.PROVIDER_NAMES[p] ? h("a", { class: "nw-ent prov", href: "/provider/" + encodeURIComponent(p) }, OG.providerName(p)) : flink("provider", p, p, "prov")),
          ...(n.regions || []).map(r => flink("region", r, r, "reg")),
          ...(n.topics || []).map(t => h("button", { class: "nw-ent top", onclick: () => setF({ topic: t }) }, t.replace(/_/g, " "))),
        ];
        const others = (n.sources || []).filter(s => s.item_id !== n.id);
        return h("article", { class: "nw-it " + relCls(n.relevance) }, wrap,
          h("div", { class: "nw-body" },
            h("a", { class: "nw-t", href: safeHref(n.url), target: "_blank", rel: "noopener external" }, n.title),
            n.summary ? h("div", { class: "nw-s" }, n.summary) : null,
            h("div", { class: "nw-m" },
              h("span", { class: "mono", title: (n.published_at_inferred ? "publication time inferred · " : "") + fmt.dateTime(n.published_at) }, fmt.age(n.published_at) + " ago"),
              h("a", { class: "lnk", href: "/news?source=" + encodeURIComponent(n.source_id) }, n.source_name),
              h("span", { class: "tier-t t-" + (n.trust_tier || "none"), title: "source tier" }, n.trust_tier || "–"),
              n.source_count > 1 ? h("span", { class: "nw-srcs", title: others.map(s => `${s.source_name}: ${s.title}`).join("\n") }, `+${n.source_count - 1} source${n.source_count > 2 ? "s" : ""}: `,
                others.slice(0, 4).map((s, i) => [i ? ", " : "", h("a", { class: "lnk", href: safeHref(s.url), target: "_blank", rel: "noopener external" }, s.source_name)])) : null,
              ents.length ? h("span", { class: "nw-ents" }, ents) : null)));
      }

      // ------------------------------------------------------------ timeline (one GPU variant)
      let charts = [];
      async function loadTimeline() {
        charts.forEach(c => c.destroy()); charts = [];
        const gsel = h("select", { class: "field", "aria-label": "GPU", onchange: e => setF({ gpu: e.target.value }) },
          h("option", { value: "" }, "choose a GPU…"), (OG.boot.gpus || []).map(([s, n]) => [s, L.shortGpu(n)]).sort((a, b) => a[1].localeCompare(b[1], undefined, { numeric: true }))
            .map(([s, t]) => h("option", { value: s, selected: s === f.gpu ? "" : null }, t)));
        const ctl = h("div", { class: "bar" }, h("span", { class: "flt" }, h("span", {}, "GPU variant"), gsel),
          h("span", { class: "flt" }, h("span", {}, "Window"), OG.seg([["24h", "24H"], ["7d", "7D"], ["30d", "30D"]], st.win, v => { st.win = v; OG.qs.set({ window: v === "7d" ? null : v }); loadTimeline(); })),
          f.provider ? h("span", { class: "dim" }, "provider: " + OG.providerName(f.provider)) : null);
        const body = h("div", {}, OG.loading());
        tlEl.replaceChildren(ctl, body);
        if (!f.gpu) { body.replaceChildren(OG.empty("Pick one GPU variant: prices are per canonical variant, so a family (e.g. “H100”) cannot be charted.")); return; }
        let d;
        try { d = await ctx.api("/v1/timeline", { params: { gpu: f.gpu, provider: f.provider || null, window: st.win }, slot: "nw-tl" }); }
        catch (e) { if (!e.stale) body.replaceChildren(OG.error(e)); return; }
        const pts = d.prices.points || [];
        const times = pts.map(p => p.hour);
        const evs = [
          ...((d.market_events && d.market_events.events) || []).map(e => ({ t: e.occurred_at, label: e.title, kind: "event · " + e.type.replace(/_/g, " "), severity: e.severity, href: "/events" })),
          ...((d.price_moves && d.price_moves.moves) || []).map(m => ({ t: m.at, label: `lowest ${fmt.price(m.from)} → ${fmt.price(m.to)} (${fmt.pct(m.change_pct / 100)})`, kind: "move · inferred", severity: "notable",
            detail: m.provider_set_changed ? "the set of priced providers changed this hour — may be coverage, not a market move" : null })),
          ...((d.availability_changes && d.availability_changes.changes) || []).map(c => ({ t: c.at || c.hour, label: `${OG.providerName(c.provider)}: ${c.from || "?"} → ${c.to || "?"}`, kind: "availability · inferred", color: "#c9b5ff" })),
          ...((d.news && d.news.items) || []).map(n => ({ t: n.published_at, label: n.title, kind: "news · " + n.source_name + " · rel " + n.relevance, color: "#8fc3ff", detail: "related by entity and time, not necessarily causal" })),
        ].filter(e => e.t);
        const priceBox = h("div", { class: "box box-p" }), availBox = h("div", { class: "box box-p nw-avail" });
        body.replaceChildren(h("p", { class: "nw-nc" }, h("b", {}, "Related, not necessarily causal. "), d.note || ""),
          h("div", { class: "lbl" }, `${L.shortGpu(d.gpu)} · ${d.prices.basis === "market_lowest" ? "market lowest and median provider price" : "provider lowest"} · ${pts.length} hourly point${pts.length === 1 ? "" : "s"}`),
          priceBox, h("div", { class: "lbl nw-gap" }, "Available listings (observed)"), availBox, legend(d, evs));
        if (times.length < 2) {
          priceBox.replaceChildren(OG.insufficient(`${pts.length} hourly sample${pts.length === 1 ? "" : "s"} in the last ${st.win}${pts[0] ? ` (lowest ${fmt.price(pts[0].lowest)}, median ${fmt.price(pts[0].median)} at ${fmt.dateTime(pts[0].hour)})` : ""}. The overlay needs at least two hours of the rollup. ${evs.length} marker${evs.length === 1 ? "" : "s"} in the window are listed below.`, "History building"));
          availBox.previousSibling.remove(); availBox.remove();
        } else {
          charts.push(OG.charts.timeseries(priceBox, { times, height: 260, events: evs, label: "price overlay", series: [
            { key: "low", label: "Lowest", values: pts.map(p => p.lowest), color: "#e7ebef", width: 2, strong: true },
            { key: "med", label: "Median provider", values: pts.map(p => p.median), color: "#7d8895", dash: "4 3" }] }));
          charts.push(OG.charts.timeseries(availBox, { times, height: 110, yFmt: v => fmt.num(v, 0), yZero: true, legend: true, label: "availability", series: [
            { key: "av", label: "In stock", values: pts.map(p => p.available_listings), color: "#2fbf71" },
            { key: "so", label: "Sold out", values: pts.map(p => p.sold_out_listings), color: "#ef5350", dash: "3 3" }] }));
        }
      }
      function legend(d, evs) {
        const rows = evs.slice().sort((a, b) => new Date(b.t) - new Date(a.t)).slice(0, 30);
        return OG.section(`Markers in this window (${evs.length})`, rows.length ? h("div", { class: "box nw-mk" }, rows.map(e => h("div", { class: "nw-mki" },
          h("i", { class: "sw", style: { background: e.color || ({ major: "#ef5350", notable: "#f5a524" })[e.severity] || "#7d8895" } }),
          h("span", { class: "mono dim" }, fmt.dateTime(e.t)), h("span", { class: "nw-mkk" }, e.kind), h("span", {}, e.label)))) : OG.empty("No events, inferred moves or related news in this window."));
      }

      // ------------------------------------------------------------ side panels
      function drawSources() {
        const rows = (st.sources || []).filter(s => s.enabled);
        const bad = rows.filter(s => s.health !== "ok");
        srcEl.replaceChildren(h("div", { class: "dim nw-sh" }, `${rows.length} enabled · ${rows.length - bad.length} ok · ${bad.length} not ok`),
          OG.table({ compact: true, limit: 60, rows, sort: { key: "health", dir: "asc" },
            rowClass: s => (s.id === f.source ? "hl" : ""), onRow: s => setF({ source: f.source === s.id ? "" : s.id }),
            columns: [
              { key: "name", label: "Source", cls: "wrap", fmt: (v, s) => h("span", { title: `${s.kind} · ${s.category} · ${s.trust_tier}${s.last_error ? "\n" + s.last_error : ""}` }, v) },
              { key: "health", label: "Health", fmt: v => v === "ok" ? h("span", { class: "dotx good" }, "ok") : OG.badge(v, v === "failing" || v === "error" ? "bad" : "warn") },
              { key: "last_ok_at", label: "Ok", num: true, value: s => s.last_ok_at ? -new Date(s.last_ok_at) : null, fmt: (v, s) => s.last_ok_at ? fmt.age(s.last_ok_at) : "never" },
              { key: "new_items_24h", label: "24h", num: true, desc: true, title: "new items in the last 24h" },
            ] }));
      }
      function drawTopics() {
        const t = (st.topics || []).slice().sort((a, b) => b.items_30d - a.items_30d);
        const max = Math.max(1, ...t.map(x => x.items_30d));
        topicEl.replaceChildren(h("div", { class: "box nw-tp" }, t.map(x => h("button", { class: "nw-tpi", "aria-pressed": String(f.topic === x.id), title: x.description, onclick: () => setF({ topic: f.topic === x.id ? "" : x.id }) },
          h("span", {}, x.id.replace(/_/g, " ")), h("span", { class: "nw-tpb" }, h("i", { style: { width: (x.items_30d / max) * 100 + "%" } })), h("span", { class: "n" }, String(x.items_30d))))),
          h("p", { class: "note" }, OG.kindBadge("inferred"), " topics and relevance by deterministic rules. ", h("a", { class: "lnk", href: "/methodology/news" }, "Methodology →")));
      }

      function drawView() {
        if (st.view === "timeline") { left.replaceChildren(tlEl); loadTimeline(); }
        else { left.replaceChildren(h("div", { class: "nw-hd" }, countEl), feedEl, moreEl); loadFeed(); }
        if (st.topics) drawTopics();
        if (st.sources) drawSources();
      }

      drawFilters(); drawView();
      const [topics, sources, fams] = await Promise.all([ctx.api("/v1/news/topics").catch(() => null), ctx.api("/v1/news/sources").catch(() => null), ctx.api("/v1/news/families").catch(() => null)]);
      // GPU family suggestions from the API (fallback: the built-in list)
      const famList = Array.isArray(fams) ? fams : fams && (fams.families || fams.items);
      if (Array.isArray(famList) && famList.length) {
        FAMILIES = famList.map(x => (typeof x === "string" ? x : x.name || x.family || x.id || x.slug)).filter(Boolean);
        for (const x of famList) if (x && typeof x === "object") { const k = x.name || x.family || x.id || x.slug, n = x.items ?? x.count ?? x.items_30d; if (k) famLabel.set(k, n != null ? `family · ${n} items` : "family"); }
      }
      st.topics = topics || []; st.sources = sources || [];
      if (!topics) topicEl.replaceChildren(OG.na("topics unavailable"));
      else drawTopics();
      if (!sources) srcEl.replaceChildren(OG.na("sources unavailable"));
      else drawSources();
      drawFilters();
      ctx.every(300000, () => { if (st.view === "feed" && st.offset <= PAGE) loadFeed(); });
    },
  });
})();
