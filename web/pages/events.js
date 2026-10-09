/* /events — Market Events feed from /v1/events (+ catalogue /v1/events/types).
   Filters (type, sev, gpu, provider, since) live in the URL. Newest first, day separators,
   "load more" paging, a per-day count histogram of the filtered set.
   Also exports OG.views.eventRow / OG.views.EVENT_TYPES, used by the overview page. */
(() => {
  const { h, fmt, Lib: L } = OG;
  OG.views = OG.views || {};

  // Short tags for the feed. Unknown types fall back to their raw name.
  const TYPE = {
    price_move: "PRICE", sold_out: "SOLD OUT", capacity_returned: "RESTOCK", provider_added_gpu: "ADDED",
    provider_removed_gpu: "REMOVED", new_cheapest_provider: "CHEAPEST", new_30d_low: "30D LOW", new_30d_high: "30D HIGH",
    new_all_time_low: "ATL", new_all_time_high: "ATH", spread_anomaly: "SPREAD", regional_dislocation: "REGIONAL",
    market_capacity: "CAPACITY", provider_feed_down: "FEED DOWN", provider_feed_recovered: "FEED UP", coverage_started: "COVERAGE",
  };
  const typeTag = t => TYPE[t] || String(t || "").replace(/_/g, " ").toUpperCase();
  const typeName = t => String(t || "").replace(/_/g, " ");
  OG.views.EVENT_TYPES = TYPE;
  // pct is a price change for these; a share / spread level for the others.
  const LEVEL_PCT = { spread_anomaly: "spread", market_capacity: "of markets", regional_dislocation: "vs global" };

  const sevMark = sev => h("i", { class: "ev-sev s-" + (sev || "info"), title: sev || "info" });

  function numbers(e, compact) {
    const out = [];
    const b = e.value_before, a = e.value_after;
    if (b != null && a != null) out.push(compact ? h("span", { class: "ev-px", title: fmt.price(b) + " → " + fmt.price(a) }, fmt.price(a)) : h("span", { class: "ev-px" }, h("span", { class: "dim" }, fmt.price(b)), " → ", fmt.price(a)));
    else if (a != null) out.push(h("span", { class: "ev-px" }, fmt.price(a)));
    else if (b != null) out.push(h("span", { class: "ev-px dim", title: "last price before" }, "was " + fmt.price(b)));
    if (e.pct != null) {
      if (LEVEL_PCT[e.type] && e.type !== "regional_dislocation") out.push(h("span", { class: "ev-lvl" }, fmt.pct(e.pct).replace(/^\+/, "") + " " + LEVEL_PCT[e.type]));
      else out.push(OG.chg(e.pct));
    }
    return out;
  }

  // One feed row. opts.compact: overview sidebar (no links column, date+time).
  OG.views.eventRow = (e, opts) => {
    opts = opts || {};
    const links = [];
    if (e.gpu) links.push(OG.gpuLink(e.gpu));
    if (e.provider) links.push(OG.providerLink(e.provider, { logo: !opts.compact }));
    if (e.region_group) links.push(h("span", { class: "ev-rg" }, e.region_group));
    const d = e.detail || {};
    const tip = [e.title, d.note, d.cause ? "cause: " + d.cause : null, d.reason ? "reason: " + d.reason : null, "detected " + fmt.dateTime(e.detected_at)].filter(Boolean).join("\n");
    // compact (overview side panel): who + what only, the type tag and the arrow say how; full title on hover
    const short = e.gpu ? (e.provider ? L.providerName(e.provider) + " · " : "") + L.shortGpu(e.gpu) + (e.provider ? "" : " · market") : null;
    const today = new Date(e.occurred_at).toDateString() === new Date().toDateString();
    return h("div", { class: "ev-row s-" + (e.severity || "info") + (e.type === "coverage_started" ? " ev-cov" : "") + (opts.compact ? "" : " ev-full"), title: tip },
      h("span", { class: "ev-t mono" }, opts.compact && !today ? fmt.date(e.occurred_at) : fmt.time(e.occurred_at).slice(0, 5)),
      sevMark(e.severity),
      h("span", { class: "ev-ty" }, typeTag(e.type)),
      h("span", { class: "ev-ti" }, opts.compact ? short || String(e.title || "").replace(/(NVIDIA|AMD Instinct|AMD|Intel) /g, "") : e.title),
      h("span", { class: "ev-n mono" }, numbers(e, opts.compact)),
      opts.compact ? null : h("span", { class: "ev-l" }, links),
      opts.compact ? null : h("span", { class: "ev-k" }, e.kind && e.kind !== "observed" ? h("a", { class: "kind-t", href: "/methodology/data-kinds", title: "data kind: " + e.kind }, e.kind) : null));
  };

  const SINCE = [["1", "24H"], ["7", "7D"], ["30", "30D"], ["90", "90D"], ["all", "ALL"]];
  const SEV = [["", "ALL"], ["notable", "NOTABLE+"], ["major", "MAJOR"]];
  const PAGE = 100;
  const dayKey = iso => { const d = new Date(iso); return d.getFullYear() + "-" + d.getMonth() + "-" + d.getDate(); };
  const DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const dayLabel = iso => { const d = new Date(iso); return DOW[d.getDay()] + " " + fmt.date(iso) + " " + d.getFullYear(); };

  OG.page("/events", {
    title: "Events",
    async mount(el, params, query, ctx) {
      const st = {
        type: OG.qs.get("type", ""), sev: OG.qs.get("sev", ""), gpu: OG.qs.get("gpu", ""), provider: OG.qs.get("provider", ""),
        since: OG.qs.get("since", "7"), cov: OG.qs.get("cov") === "1", items: [], total: 0, cat: null,
      };
      if (!SINCE.some(s => s[0] === st.since)) st.since = "7";
      const count = h("span", { class: "mono dim ev-count" });
      const typeSel = h("select", { class: "field", "aria-label": "Event type", onchange: e => set({ type: e.target.value }) }, h("option", { value: "" }, "All types"));
      const gpuSel = h("select", { class: "field", "aria-label": "GPU", onchange: e => set({ gpu: e.target.value }) }, h("option", { value: "" }, "All GPUs"));
      const provSel = h("select", { class: "field", "aria-label": "Provider", onchange: e => set({ provider: e.target.value }) }, h("option", { value: "" }, "All providers"));
      const sevSeg = OG.seg(SEV, st.sev, v => set({ sev: v }));
      const sinceSeg = OG.seg(SINCE, st.since, v => set({ since: v }));
      const covBtn = h("button", { class: "chip", "aria-pressed": String(st.cov), title: "coverage_started marks when OpenGrid began recording a provider: not a market change",
        onclick: () => set({ cov: !st.cov }) }, "Coverage starts");
      const clear = h("button", { class: "btn sm", onclick: () => set({ type: "", sev: "", gpu: "", provider: "", since: "7", cov: false }) }, "Reset");
      const histEl = h("div", { class: "ev-hist" });
      const histNote = h("div", { class: "ev-hist-n mono dim" });
      const feed = h("div", { class: "ev-feed" }, OG.loading("Loading events…"));
      const more = h("div", { class: "ev-more" });
      const legend = h("div", { class: "ev-legend" }, OG.loading());

      el.classList.add("pg-events");
      el.append(
        OG.head("Market events", "Structured changes detected in the hourly on-demand market record. Events say what changed, never why.",
          count, h("a", { class: "btn", href: "/methodology/events" }, "Methodology")),
        h("div", { class: "bar" },
          h("label", { class: "flt" }, h("span", {}, "Type"), typeSel),
          h("label", { class: "flt" }, h("span", {}, "GPU"), gpuSel),
          h("label", { class: "flt" }, h("span", {}, "Provider"), provSel),
          h("span", { class: "flt" }, h("span", {}, "Severity"), sevSeg),
          h("span", { class: "flt" }, h("span", {}, "Since"), sinceSeg),
          covBtn, h("span", { class: "spacer" }), clear),
        h("div", { class: "ev-main" },
          h("div", { class: "ev-left" },
            h("div", { class: "box ev-histbox" }, h("div", { class: "ev-hh" }, h("span", { class: "lbl" }, "Events per day"), histNote), histEl),
            feed, more),
          h("aside", { class: "ev-side" }, h("h2", { class: "sec-h" }, "Event types"), legend)));

      let hist = null;
      function set(patch) {
        Object.assign(st, patch);
        OG.qs.set({ type: st.type || null, sev: st.sev || null, gpu: st.gpu || null, provider: st.provider || null,
          since: st.since === "7" ? null : st.since, cov: st.cov ? 1 : null });
        syncControls();
        reload();
      }
      function syncControls() {
        typeSel.value = st.type; gpuSel.value = st.gpu; provSel.value = st.provider;
        sevSeg.set(st.sev); sinceSeg.set(st.since); covBtn.setAttribute("aria-pressed", String(st.cov));
        legend.querySelectorAll(".ev-lg").forEach(n => n.classList.toggle("on", n.dataset.t === st.type));
      }
      function params(offset, limit) {
        const p = { limit, offset };
        if (st.type) p.type = st.type;
        else if (!st.cov && st.cat) p.type = st.cat.events.map(t => t.type).filter(t => t !== "coverage_started").join(",");
        if (st.sev) p.min_severity = st.sev;
        if (st.gpu) p.gpu = st.gpu;
        if (st.provider) p.provider = st.provider;
        if (st.since !== "all") p.since = new Date(Date.now() - Number(st.since) * 86400e3).toISOString();
        return p;
      }

      function drawFeed() {
        if (!st.items.length) {
          const filtered = st.type || st.sev || st.gpu || st.provider;
          feed.replaceChildren(OG.empty(filtered ? "No events match these filters in this window." : "No market events in this window. The detector runs hourly on the rollup; with little recorded history few events can fire."));
          more.replaceChildren(); return;
        }
        const out = []; let day = null;
        const perDay = new Map();
        for (const e of st.items) perDay.set(dayKey(e.occurred_at), (perDay.get(dayKey(e.occurred_at)) || 0) + 1);
        for (const e of st.items) {
          const k = dayKey(e.occurred_at);
          if (k !== day) { day = k; out.push(h("div", { class: "ev-day" }, h("b", {}, dayLabel(e.occurred_at)), h("span", { class: "dim mono" }, perDay.get(k) + " shown"))); }
          out.push(OG.views.eventRow(e));
        }
        feed.replaceChildren(...out);
        const left = st.total - st.items.length;
        more.replaceChildren(left > 0 ? h("button", { class: "btn", onclick: loadMore }, `Load ${Math.min(PAGE, left)} more · ${fmt.num(left)} older`) : h("span", { class: "dim mono" }, "end of feed"));
      }

      async function loadHist() {
        // Day counts over the filtered window: newest 1000 events (says so when truncated).
        const days = st.since === "all" ? null : Number(st.since);
        let r;
        try { r = await ctx.api("/v1/events", { params: params(0, 1000), full: true, slot: "ev-hist" }); }
        catch (e) { if (!e.stale) histEl.replaceChildren(OG.error(e)); return; }
        const items = r.data || [], total = (r.meta && r.meta.pagination && r.meta.pagination.total) || items.length;
        const counts = new Map();
        for (const e of items) counts.set(dayKey(e.occurred_at), (counts.get(dayKey(e.occurred_at)) || 0) + 1);
        const end = new Date(); end.setHours(0, 0, 0, 0);
        let start;
        if (days) { start = new Date(end); start.setDate(start.getDate() - Math.min(days, 90) + 1); }
        else if (items.length) { start = new Date(items[items.length - 1].occurred_at); start.setHours(0, 0, 0, 0); }
        else start = new Date(end);
        const bars = [];
        for (let d = new Date(start); d <= end; d.setDate(d.getDate() + 1)) {
          const iso = d.toISOString(), k = dayKey(d);
          bars.push({ label: fmt.date(iso), short: String(d.getDate()), value: counts.get(k) || 0, color: "#22528c" });
        }
        const truncated = total > items.length;
        histNote.textContent = (truncated ? `newest ${fmt.num(items.length)} of ${fmt.num(total)} events counted · ` : "") + `${fmt.num(total)} events · ${bars.length} day${bars.length === 1 ? "" : "s"}`;
        const opts = { items: bars, horizontal: false, height: 72, fmt: v => (Number.isInteger(v) ? fmt.num(v, 0) : ""), label: "events per day" };
        if (hist) hist.update(opts); else hist = OG.charts.bars(histEl, opts);
      }

      async function reload() {
        feed.replaceChildren(OG.loading("Loading events…")); more.replaceChildren();
        try {
          const r = await ctx.api("/v1/events", { params: params(0, PAGE), full: true, slot: "ev-feed" });
          st.items = r.data || []; st.total = (r.meta && r.meta.pagination && r.meta.pagination.total) || st.items.length;
          count.textContent = `${fmt.num(st.total)} event${st.total === 1 ? "" : "s"}`;
          if (r.meta) OG.status.asOf(r.meta.as_of);
          drawFeed();
        } catch (e) { if (!e.stale) feed.replaceChildren(OG.error(e, reload)); }
        loadHist();
      }
      async function loadMore() {
        more.replaceChildren(OG.loading());
        try {
          const r = await ctx.api("/v1/events", { params: params(st.items.length, PAGE), full: true });
          st.items = st.items.concat(r.data || []);
          drawFeed();
        } catch (e) { more.replaceChildren(OG.error(e, loadMore)); }
      }
      // Auto-refresh only the first page while the user has not paged further.
      async function refresh() {
        if (st.items.length > PAGE) return;
        try {
          const r = await ctx.api("/v1/events", { params: params(0, PAGE), full: true, slot: "ev-feed", nocache: true });
          st.items = r.data || []; st.total = (r.meta && r.meta.pagination && r.meta.pagination.total) || st.items.length;
          count.textContent = `${fmt.num(st.total)} event${st.total === 1 ? "" : "s"}`;
          drawFeed(); loadHist();
        } catch (e) { /* keep the current feed */ }
      }

      // Catalogue + selector options, then the feed.
      const [cat, gpus] = await Promise.all([ctx.api("/v1/events/types").catch(() => null), OG.data.gpus().catch(() => [])]);
      st.cat = cat;
      if (cat) {
        typeSel.append(...cat.events.map(t => h("option", { value: t.type }, typeName(t.type))));
        legend.replaceChildren(...cat.events.map(t => h("button", { class: "ev-lg", "data-t": t.type, title: "Show only " + typeName(t.type), onclick: () => set({ type: st.type === t.type ? "" : t.type }) },
          h("div", { class: "ev-lg-h" }, h("span", { class: "ev-ty" }, typeTag(t.type)), h("span", { class: "ev-lg-n" }, typeName(t.type)), OG.kindBadge(t.kind)),
          h("div", { class: "ev-lg-d" }, t.description),
          h("div", { class: "ev-lg-s" }, h("span", { class: "dimmer" }, "severity "), t.severity))),
          h("div", { class: "ev-lg-k" }, h("span", {}, sevMark("info"), " info"), h("span", {}, sevMark("notable"), " notable"), h("span", {}, sevMark("major"), " major")));
      } else legend.replaceChildren(OG.insufficient("the event catalogue (/v1/events/types) could not be loaded", "Unavailable"));
      gpuSel.append(...gpus.filter(g => g.live || g.slug === st.gpu).sort((a, b) => a.short.localeCompare(b.short)).map(g => h("option", { value: g.slug }, g.short)));
      provSel.append(...OG.data.providers().sort((a, b) => a.display_name.localeCompare(b.display_name)).map(p => h("option", { value: p.name }, p.display_name)));
      syncControls();
      reload();
      ctx.every(60000, refresh);
    },
  });
})();
