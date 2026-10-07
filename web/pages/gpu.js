/* /gpu/:slug — one GPU market, laid out like a stock / commodity page.

Data (all in parallel, each section renders as soon as its own request lands):
  /v1/gpus/{slug}              hardware, current market (dispersion, score, by_provider), capability, alternatives
  /v1/indices/{slug}           OpenGrid index level, 24h/7d/30d/90d changes, volatility       (soft: reason shown)
  /v1/markets/{slug}/context   historical percentile, label, the context sentence             (soft)
  /v1/history/{slug}?window=   hourly lowest / median / highest + index line (chart, market view)
  /market/detail?gpu=&hours=   per-provider step lines (chart, providers view; fine grid for thin history)
  /v1/timeline?gpu=&window=    availability history, inferred price moves, events + news markers
  /v1/timeline/around          "related, not necessarily causal" panel when a move is clicked
  /v1/events?gpu=  /v1/news?gpu=  /v1/best/{slug}  /v1/trust/listings?gpu=  /v1/heatmaps/gpu-region-*
Nothing is computed here that the API does not state, except display arithmetic (spread, $ deltas).
*/
(() => {
  const { h, fmt, Lib: L } = OG;
  const WINDOWS = [["24h", "24H", 24], ["7d", "7D", 168], ["30d", "30D", 720], ["90d", "90D", 2160], ["all", "ALL", 0]];
  const SEV_RANK = { info: 0, notable: 1, major: 2 };
  const DIM_LABEL = { similar_vram: "similar VRAM", same_architecture: "same architecture", same_generation: "same generation",
    training_oriented: "training", inference_oriented: "inference", similar_price_performance: "similar $/TFLOP" };
  const list = r => (Array.isArray(r) ? r : r && Array.isArray(r.items) ? r.items : []);
  // $/unit figures are often fractions of a cent: keep three significant digits
  const small = v => (v == null || !isFinite(v) ? "–" : v >= 0.1 ? fmt.price(v) : "$" + Number(v).toPrecision(3));
  const pctTxt = v => (v == null ? "–" : fmt.pct(v));
  const signedCls = v => (v == null ? "" : v < -0.0005 ? "down" : v > 0.0005 ? "up" : "");
  const when = iso => (iso ? fmt.dateTime(iso) : "–");
  // A stat cell has room for a few words; the full reason stays in the tooltip
  const shortReason = r => !r ? null : /does not cover|insufficient history|not enough/i.test(r) ? "not enough history" : /not published/i.test(r) ? "not published" : /unavailable/i.test(r) ? "unavailable" : r.length > 28 ? r.slice(0, 26) + "…" : r;
  const ordinal = n => n + (n % 100 >= 11 && n % 100 <= 13 ? "th" : ["th", "st", "nd", "rd"][n % 10] || "th");
  const ext = (href, label) => h("a", { class: "lnk", href, target: "_blank", rel: "external noopener" }, label);

  function section(id, title, right, ...kids) {
    return h("section", { class: "sec gp-sec", id: "gp-" + id, "data-sec": id },
      h("div", { class: "gp-sh" }, h("h2", { class: "sec-h" }, title), h("span", { class: "spacer" }), right || null), kids);
  }
  const slot = (text) => h("div", { class: "gp-slot" }, OG.loading(text));

  OG.page("/gpu/:slug", {
    title: p => OG.shortGpu(OG.data.gpuName(p.slug) || p.slug),
    nav: "gpus",
    async mount(el, params, query, ctx) {
      el.classList.add("gp");
      ctx.onCleanup(() => el.classList.remove("gp"));
      const slug = params.slug;
      let name = OG.data.gpuName(slug);
      if (!name) { const g = await OG.resolveGpu(slug); if (g && g.slug === slug) name = g.name; }
      if (!ctx.alive()) return;
      if (!name) {
        // not one canonical GPU: a family slug (h100, blackwell) gets the side-by-side variants page
        el.append(OG.loading("Loading…"));
        const fam = OG.views.family ? await OG.data.family(slug, ctx.api) : null;
        if (!ctx.alive()) return;
        el.textContent = "";
        if (fam) { OG.views.family(el, fam, ctx); return; }
        el.append(OG.head("Unknown GPU", slug), OG.empty("No canonical GPU or GPU family has this slug."), h("a", { class: "lnk", href: "/gpus" }, "All GPU markets →"));
        return;
      }
      ctx.setTitle(L.shortGpu(name) + " price");
      const short = L.shortGpu(name);
      const wq = OG.qs.get("w", "7d");
      const st = {
        win: WINDOWS.some(w => w[0] === wq) ? wq : "7d",
        view: OG.qs.get("view") === "providers" ? "providers" : "market",
        overlays: new Set((OG.qs.get("ov", "events,news,moves")).split(",").filter(Boolean)),
        g: null, idx: undefined, ctxd: undefined, tl: null, chartData: null, count: Number(OG.qs.get("count", 1)) || 1,
      };

      /* ---------- skeleton ---------- */
      const hdrBadges = h("span", { class: "gp-badges" });
      const quote = h("div", { class: "gp-quote" }, OG.loading("Loading market…"));
      const ctxLine = h("div", { class: "gp-ctx" });
      const navEl = h("nav", { class: "gp-nav", "aria-label": "Sections" });
      const chartEl = h("div", { class: "gp-chart" });
      const chartNote = h("p", { class: "note" });
      const chartAround = h("div", { class: "gp-around gp-around-chart" });
      const availEl = h("div", { class: "gp-chart sm" });
      const availNote = h("p", { class: "note" });
      const provBox = slot("Loading providers…");
      const distBox = h("div", {}, OG.loading());
      const regionBox = slot();
      const movesBox = slot();
      const aroundBox0 = h("div", { class: "gp-around" });
      const newsBox = slot();
      const eventsBox = slot();
      const altBox = slot();
      const bestBox = slot("Ranking listings…");
      const dispBox = slot();
      const hwBox = slot();
      const capBox = slot();
      const listingsBox = h("div", {});

      const winSeg = OG.seg(WINDOWS.map(w => [w[0], w[1]]), st.win, v => { st.win = v; OG.qs.set({ w: v === "7d" ? null : v }); loadChart(); loadTimeline(); });
      const viewSeg = OG.seg([["market", "Market", "low–high band, median, lowest, index"], ["providers", "Providers", "one step line per provider"]], st.view,
        v => { st.view = v; OG.qs.set({ view: v === "market" ? null : v }); loadChart(); });
      const ovChip = (k, label, title) => h("button", { class: "chip sm", "aria-pressed": String(st.overlays.has(k)), title,
        onclick: e => { st.overlays.has(k) ? st.overlays.delete(k) : st.overlays.add(k); e.currentTarget.setAttribute("aria-pressed", String(st.overlays.has(k)));
          OG.qs.set({ ov: [...st.overlays].join(",") === "events,news,moves" ? null : [...st.overlays].join(",") || "none" }); drawChart(); } }, label);
      const chartCtl = h("div", { class: "gp-ctl" }, viewSeg, winSeg,
        h("span", { class: "flt" }, h("span", {}, "overlay"), ovChip("events", "Events", "notable and major market events"), ovChip("news", "News", "news mentioning this GPU"), ovChip("moves", "Moves", "inferred hour-over-hour moves of the lowest price ≥ 3%")));
      const evSevSeg = OG.seg([["info", "All"], ["notable", "Notable+"], ["major", "Major"]], OG.qs.get("sev", "notable"), v => { OG.qs.set({ sev: v === "notable" ? null : v }); loadEvents(v); });
      const countSeg = OG.seg([[1, "1×"], [2, "2×"], [4, "4×"], [8, "8×"]], st.count, v => { st.count = Number(v); OG.qs.set({ count: v == 1 ? null : v }); loadBest(); });

      const main = h("div", { class: "gp-main" },
        section("chart", "Price history", chartCtl, chartEl, chartNote, chartAround),
        section("availability", "Availability history", null, availEl, availNote),
        section("providers", "Provider ranking", null, provBox),
        section("regions", "Region pricing", null, regionBox),
        section("moves", "Price moves and related news", null, h("div", { class: "cols-2 gp-two" }, h("div", {}, h("div", { class: "lbl" }, "Inferred moves of the lowest price"), movesBox, aroundBox0), h("div", {}, h("div", { class: "lbl" }, "News mentioning this GPU"), newsBox))),
        section("events", "Market events", evSevSeg, eventsBox),
        section("alternatives", "Related and alternative GPUs", null, altBox),
        section("listings", "All current listings", null, listingsBox));
      const rail = h("aside", { class: "gp-rail" },
        section("best", "Best available now", countSeg, bestBox),
        section("dispersion", "Dispersion and efficiency", null, dispBox),
        section("distribution", "Price distribution now", null, distBox),
        section("capability", "Price per capability", OG.badge("theoretical", "warn", "vendor peak specs, dense; not a benchmark"), capBox),
        section("hardware", "Hardware", null, hwBox));

      el.append(
        OG.head(h("span", { class: "gpu-title" }, h("span", { class: "eyebrow" }, L.vendorOf(name)), short, hdrBadges, h("span", { class: "full" }, name)), null,
          h("a", { class: "btn pri", href: "/route?gpu=" + slug, title: "Preview where this would run (no provisioning without confirmation)" }, "Route this →"),
          h("a", { class: "btn", href: "/compare?a=" + slug }, "Compare"),
          h("a", { class: "btn", href: "/indices/" + slug }, "Index"),
          h("a", { class: "btn", href: "/explorer?gpu=" + encodeURIComponent(name) }, "Listings")),
        quote, ctxLine, navEl, h("div", { class: "gp-grid" }, main, rail));

      const NAV = [["chart", "Chart"], ["availability", "Availability"], ["providers", "Providers"], ["regions", "Regions"], ["moves", "Moves & news"],
        ["events", "Events"], ["alternatives", "Alternatives"], ["listings", "Listings"], ["best", "Best now"], ["dispersion", "Dispersion"],
        ["distribution", "Distribution"], ["capability", "$/capability"], ["hardware", "Hardware"]];
      navEl.append(...NAV.map(([id, label]) => h("button", { type: "button", "data-for": id, onclick: () => { const t = document.getElementById("gp-" + id); if (t) t.scrollIntoView({ block: "start" }); } }, label)));

      /* ---------- header quote strip ---------- */
      function drawQuote() {
        const g = st.g, m = g && g.market, idx = st.idx, cx = st.ctxd;
        if (!g) return;
        const hw = g.hardware || {};
        hdrBadges.replaceChildren(...[hw.architecture, hw.vram_gb ? hw.vram_gb + "GB " + (hw.memory_type || "") : null, hw.form_factor, hw.workload_class].filter(Boolean).map(t => OG.badge(t)));
        const ch = k => {
          if (idx === undefined) return { label: k, value: null, reason: "loading" };
          if (!idx) return { label: k, value: null, reason: "index unavailable" };
          const c = (idx.changes || {})[k] || {};
          return { label: k + " chg", value: c.pct == null ? null : OG.chg(c.pct), reason: c.reason || "no change data", sub: c.pct == null ? shortReason(c.reason) : "index",
            title: c.pct == null ? c.reason : `index ${fmt.price(c.from_level)} at ${when(c.from_hour)} → ${fmt.price(idx.level)}` };
        };
        const vol = idx && idx.volatility ? (idx.volatility["30d"] && idx.volatility["30d"].annualized != null ? ["30d", idx.volatility["30d"]] : ["7d", idx.volatility["7d"] || {}]) : null;
        const lowM = cx && cx.metrics && cx.metrics.lowest, hist = lowM && lowM.historical;
        const medW = cx && cx.metrics && cx.metrics.median && cx.metrics.median.windows;
        const pw = medW && (medW["30d"] && medW["30d"].percentile != null ? medW["30d"] : medW["90d"] && medW["90d"].percentile != null ? medW["90d"] : medW["30d"]);
        const live = m && m.providers > 0;
        const spread = live && m.providers > 1 ? m.stats.spread_pct_of_low : null;
        const big = h("div", { class: "gp-big" },
          h("div", { class: "gp-big-l" }, "OpenGrid index", idx ? OG.kindBadge("observed") : null),
          h("div", { class: "gp-big-v" }, idx && idx.published && idx.level != null ? fmt.price(idx.level) : OG.na(idx === undefined ? "loading" : idx ? idx.reason : "index endpoint unavailable"),
            idx && idx.published && idx.changes && idx.changes["24h"] ? OG.chg(idx.changes["24h"].pct, { reason: idx.changes["24h"].reason }) : null),
          h("div", { class: "gp-big-s" }, idx && idx.published ? `$/GPU·h · ${idx.constituents} providers · ${idx.method || "index"} · ${when(idx.hour)}` : idx ? (idx.reason || "not published") : idx === undefined ? "" : "index not available on this server"));
        quote.replaceChildren(big, OG.stats([
          { label: "Low", value: live ? fmt.price(m.low) : null, reason: "nobody is selling it in stock now", sub: live ? OG.providerName(m.by_provider[0].provider) : null, kind: "observed" },
          { label: "Median", value: live ? fmt.price(m.median) : null, reason: "no priced providers", sub: live ? "of provider lows" : null },
          { label: "High", value: live ? fmt.price(m.high) : null, reason: "no priced providers", sub: live ? OG.providerName(m.by_provider[m.by_provider.length - 1].provider) : null },
          { label: "Spread", value: spread != null ? fmt.pct(spread) : null, reason: live ? "one provider: no spread" : "no priced providers", sub: spread != null ? fmt.price(m.stats.spread_abs) + " high − low" : null },
          { label: "Providers", value: String(m ? m.providers : 0), sub: "priced now" },
          { label: "Available", value: m ? String(m.listings.available) : null, sub: m ? `${m.listings.availability_unknown} unknown · ${m.listings.sold_out} sold out` : null, title: "listings with explicit availability now" },
          ch("24h"), ch("7d"), ch("30d"), ch("90d"),
          { label: "Volatility", value: vol && vol[1].annualized != null ? fmt.pct(vol[1].annualized) : null, reason: vol ? vol[1].reason : idx === undefined ? "loading" : "index unavailable", sub: vol && vol[1].annualized != null ? vol[0] + " annualized" : shortReason(vol ? vol[1].reason : "index unavailable") },
          { label: "Hist. low", value: hist && hist.low ? fmt.price(hist.low.value) : null, reason: cx === undefined ? "loading" : cx ? "no history" : "context endpoint unavailable", sub: hist && hist.low ? "of lowest · " + fmt.date(hist.low.hour) : null, title: hist ? `lowest market price recorded since ${when(hist.since)} (${hist.samples} hourly samples)` : null },
          { label: "Hist. high", value: hist && hist.high ? fmt.price(hist.high.value) : null, reason: cx === undefined ? "loading" : cx ? "no history" : "context endpoint unavailable", sub: hist && hist.high ? "of lowest · " + fmt.date(hist.high.hour) : null, title: hist ? `highest market-lowest price recorded since ${when(hist.since)}` : null },
          { label: "Percentile", value: pw && pw.percentile != null ? ordinal(Math.round(pw.percentile)) : null, reason: pw ? pw.reason : cx ? cx.label_reason : "unavailable",
            sub: pw && pw.percentile != null ? `median vs ${pw.window}${cx.label ? " · " + cx.label : ""}` : shortReason(pw ? pw.reason : cx ? cx.label_reason : "unavailable"), title: pw && pw.reason ? pw.reason : null },
        ]));
        const sum = cx && cx.summaries && cx.summaries.length ? cx.summaries : null;
        ctxLine.replaceChildren(...(sum ? [h("span", { class: "eyebrow" }, "Context"), " ", sum.join(" "), " ", h("a", { class: "lnk dim", href: "/methodology/historical-context" }, "how")]
          : cx === null ? [h("span", { class: "dim" }, "Historical context unavailable on this server (/v1/markets/{gpu}/context).")]
          : cx && cx.label_reason ? [h("span", { class: "dim" }, "Historical context: " + cx.label_reason)] : []));
      }

      /* ---------- market summary (+ everything that hangs off /v1/gpus/{slug}) ---------- */
      async function loadSummary() {
        try {
          const r = await ctx.api("/v1/gpus/" + slug, { full: true, slot: "gp-sum" });
          st.g = r.data; OG.status.asOf(r.meta && r.meta.as_of);
          drawQuote(); drawProviders(); drawDispersion(); drawDistribution(); drawHardware(); drawCapability(); drawAlternatives();
        } catch (e) { if (!e.stale) quote.replaceChildren(OG.error(e, loadSummary)); }
      }
      // /v1/markets/{gpu}: per-provider 24h change when the API carries it (soft; the column is hidden otherwise)
      async function loadMarketChanges() {
        const mk = await ctx.api("/v1/markets/" + slug).catch(() => null);
        const rows = mk && (mk.by_provider || mk.providers || (mk.market && mk.market.by_provider)) || [];
        const m = new Map();
        for (const p of Array.isArray(rows) ? rows : []) { const x = p.change_24h ?? p.change_24h_pct; if (p.provider && x != null) m.set(p.provider, x); }
        st.chg = m;
        if (m.size && st.g) drawProviders();
      }
      async function loadIndex() {
        const [idx, cx] = await Promise.all([ctx.api("/v1/indices/" + slug).catch(() => null), ctx.api("/v1/markets/" + slug + "/context").catch(() => null)]);
        st.idx = idx; st.ctxd = cx; drawQuote();
      }

      /* ---------- providers ---------- */
      let trust = null, provTable = null;
      function drawProviders() {
        const m = st.g.market;
        if (!m.by_provider.length) { provBox.replaceChildren(OG.insufficient(`No provider is selling the ${short} in stock on demand right now (${m.listings.live} live listings, ${m.listings.sold_out} sold out).`, "No live price")); return; }
        const tByListing = new Map((trust || []).map(t => [t.provider + "|" + t.listing_id, t.trust]));
        // change_24h: {pct, status, reason} (or a bare fraction); null pct keeps its reason for the tooltip
        const chOf = p => { const x = p.change_24h ?? p.change_24h_pct ?? (st.chg && st.chg.get(p.provider)); return x == null ? null : typeof x === "object" ? x : { pct: x }; };
        const rows = m.by_provider.map(p => {
          const t = tByListing.get(p.provider + "|" + p.listing_id) || null, meta = OG.providerMeta(p.provider) || {};
          const c24 = chOf(p);
          return Object.assign({}, p, { trust: t, freshness: t ? t.freshness : null, age: t ? t.age_seconds : null, chg24: c24 ? c24.pct : null, chg24r: c24 ? c24.reason || c24.status : null, chg24k: !!c24,
            source_type: (t && (t.source_detail || t.source_type)) || meta.source_type || null, website: meta.website, pclass: meta.provider_class });
        });
        const cols = [
          { key: "rank", label: "#", num: true, width: "28px" },
          { key: "provider", label: "Provider", fmt: v => h("span", { class: "lnk prov" }, h("i", { class: "sw", style: `background:${L.providerColor(v)}` }), OG.providerLink(v)), value: r => OG.providerName(r.provider) },
          { key: "price", label: "Best $/GPU·h", num: true, fmt: v => h("b", {}, fmt.price(v)) },
          { key: "premium_vs_others_median", label: "vs median", num: true, title: "own lowest / median of the other providers' lowest − 1", fmt: v => h("span", { class: signedCls(-v) === "up" ? "up" : signedCls(v) === "up" ? "down" : "" }, pctTxt(v)) },
          { key: "chg24", label: "24h", num: true, hidden: !rows.some(r => r.chg24k), title: "this provider's lowest price vs 24 hours earlier", fmt: (v, r) => OG.chg(v, { reason: r.chg24r || "no price 24 hours ago" }) },
          { key: "listings", label: "Lst", num: true, title: "priced eligible listings" },
          { key: "available", label: "Stock", fmt: v => v === true ? h("span", { class: "up" }, "in stock") : v === false ? h("span", { class: "down" }, "sold out") : h("span", { class: "dim", title: "provider does not publish availability" }, "unknown") },
          { key: "region", label: "Region", cls: "dim", fmt: v => v || "–" },
          { key: "freshness", label: "Fresh", title: "from the trust endpoint: age of the observation", fmt: (v, r) => r.trust ? OG.freshBadge(r.age != null ? r.age : v, { provider: r.provider }) : trust === null ? h("span", { class: "dim" }, "…") : OG.na("trust data unavailable"), value: r => r.age },
          { key: "confidence", label: "Trust", value: r => r.trust && r.trust.confidence, fmt: (v, r) => r.trust ? OG.badge(r.trust.confidence || "?", r.trust.confidence === "high" ? "good" : r.trust.confidence === "low" ? "bad" : "warn", (r.trust.confidence_reasons || []).join("; ") || "no issues") : "–" },
          { key: "source_type", label: "Source", cls: "dim", fmt: v => (v || "–").replace(/_/g, " ") },
          { key: "website", label: "", sort: false, csv: false, fmt: v => v ? ext(v, "site ↗") : "" },
        ];
        if (provTable && provTable.cols24 !== rows.some(r => r.chg24k)) provTable = null;   // column set changed
        if (!provTable) {
          provTable = OG.table({ columns: cols, rows, sort: { key: "rank", dir: "asc" }, rowKey: r => r.provider, compact: true,
            onHover: r => chart && st.view === "providers" && chart.highlight(r ? r.provider : null), csv: `opengrid-${slug}-providers.csv`,
            title: `${rows.length} providers · one vote each (its lowest eligible price)` });
          provTable.cols24 = rows.some(r => r.chg24k);
          provBox.replaceChildren(provTable, h("p", { class: "note" }, OG.kindBadge("observed"), " Observed list prices, on-demand, not interruptible, in stock or stock unknown. ",
            m.listings.sold_out ? `${m.listings.sold_out} sold-out listing${m.listings.sold_out === 1 ? "" : "s"} excluded. ` : "", h("a", { class: "lnk", href: "/methodology/dispersion" }, "Method")));
        } else provTable.update(rows);
      }
      async function loadTrust() {
        const [r] = await Promise.all([ctx.api("/v1/trust/listings", { params: { gpu: slug, limit: 1000 } }).catch(() => undefined), OG.data.intervals()]);
        trust = r === undefined ? [] : list(r);
        if (r === undefined) trust.unavailable = true;
        if (st.g) { drawProviders(); drawDistribution(); }
      }

      /* ---------- distribution ---------- */
      function drawDistribution() {
        const m = st.g.market;
        if (!m.by_provider.length) { distBox.replaceChildren(OG.empty("No prices now.")); return; }
        const rows = [{ label: "Provider lows", low: m.low, median: m.median, high: m.high,
          points: m.by_provider.map(p => ({ key: p.provider, label: OG.providerName(p.provider), value: p.price, color: L.providerColor(p.provider), href: "/provider/" + p.provider })) }];
        const ls = m.listings.stats;
        const lst = (trust || []).filter(t => t.market_type === "on_demand" && !t.interruptible && t.price_per_gpu_hour > 0 && t.available !== false && t.trust && t.trust.freshness !== "stale");
        if (lst.length) rows.push({ label: "Listings", low: ls.low, median: ls.median, high: ls.high,
          points: lst.map(t => ({ key: t.provider + t.listing_id, label: OG.providerName(t.provider) + " · " + (t.gpu_count || "?") + "× · " + (t.region || "no region"), value: t.price_per_gpu_hour, color: L.providerColor(t.provider) })) });
        distBox.replaceChildren();
        const c = h("div", {}); distBox.append(c);
        OG.charts.dotplot(c, { rows });
        distBox.append(h("table", { class: "gp-kv" }, h("tbody", {},
          kv("Providers · listings", `${m.providers} · ${m.listings.priced} priced`),
          kv("Q1 – Q3 (providers)", m.stats.q1 != null ? `${fmt.price(m.stats.q1)} – ${fmt.price(m.stats.q3)}` : OG.na(m.stats.reasons.iqr)),
          kv("Listing median", fmt.price(ls.median)),
          kv("Mean (providers)", fmt.price(m.stats.mean)))),
          h("p", { class: "note" }, "Tick: median. Bar: low–high. Listing row: on-demand listings seen fresh, all GPU counts."));
      }
      const kv = (k, v, cls) => h("tr", {}, h("th", {}, k), h("td", { class: cls || "" }, v == null ? "–" : v));

      /* ---------- dispersion ---------- */
      function drawDispersion() {
        const m = st.g.market, sc = m.score, s = m.stats;
        if (sc.efficiency == null) { dispBox.replaceChildren(OG.insufficient(sc.reason, "No efficiency score"), h("table", { class: "gp-kv" }, h("tbody", {}, kv("Spread", s.spread_pct_of_low != null ? fmt.pct(s.spread_pct_of_low) : "–")))); return; }
        const comp = sc.components, w = sc.weights;
        const bar = (label, raw, c, wt, rawFmt) => h("tr", {}, h("th", {}, label), h("td", { class: "n" }, rawFmt(raw)),
          h("td", { class: "gp-barc" }, h("span", { class: "gp-bar", style: `width:${Math.round(c * 100)}%` })), h("td", { class: "n dim" }, "×" + wt), h("td", { class: "n" }, (100 * wt * c).toFixed(1)));
        dispBox.replaceChildren(
          h("div", { class: "gp-score" }, h("div", {}, h("div", { class: "gp-score-v" }, sc.efficiency.toFixed(0)), h("div", { class: "dim" }, "efficiency / 100")),
            h("div", {}, h("div", { class: "gp-score-l " + (sc.fragmentation >= 50 ? "down" : sc.fragmentation >= 25 ? "warn" : "up") }, sc.label),
              h("div", { class: "dim" }, `fragmentation ${sc.fragmentation} · confidence ${sc.confidence}`), OG.kindBadge("inferred"))),
          h("table", { class: "gp-kv gp-comp" }, h("thead", {}, h("tr", {}, h("th", {}, "component"), h("th", { class: "n" }, "raw"), h("th", {}, "saturation"), h("th", { class: "n" }, "w"), h("th", { class: "n" }, "pts"))),
            h("tbody", {}, bar("CV (adj.)", comp.cv_adj, comp.c_cv, w.cv, v => v.toFixed(3)), bar("IQR / median", comp.iqr_rel, comp.c_iqr, w.iqr, v => v.toFixed(3)), bar("High / low − 1", comp.range, comp.c_range, w.range, v => fmt.pct(v)))),
          h("table", { class: "gp-kv" }, h("tbody", {}, kv("Spread", `${fmt.price(s.spread_abs)} (${fmt.pct(s.spread_pct_of_low)} of low)`), kv("IQR", fmt.price(s.iqr)), kv("Std dev", fmt.price(s.stdev)))),
          h("p", { class: "note" }, "Fragmentation = 100 × Σ w·min(raw / saturation, 1); efficiency = 100 − fragmentation. ", h("a", { class: "lnk", href: "/methodology/dispersion" }, "Method")));
      }

      /* ---------- hardware ---------- */
      function drawHardware() {
        const hw = st.g.hardware;
        if (!hw) { hwBox.replaceChildren(OG.insufficient("No hardware entry for this GPU yet.", "No spec")); return; }
        const tf = v => (v == null ? null : fmt.num(v, v < 100 ? 1 : 0) + " TFLOPS");
        const rows = [
          ["Vendor · arch", [hw.vendor, hw.architecture, hw.generation && hw.generation !== hw.architecture ? hw.generation : null].filter(Boolean).join(" · ")],
          ["Released · segment", [hw.released, hw.segment].filter(Boolean).join(" · ")],
          ["VRAM", hw.vram_gb ? `${hw.vram_gb} GB ${hw.memory_type || ""}` : null],
          ["Memory bandwidth", hw.memory_bandwidth_tbps ? hw.memory_bandwidth_tbps + " TB/s" : null],
          ["BF16 (dense)", tf(hw.bf16_tflops_dense)], ["FP16 (dense)", tf(hw.fp16_tflops_dense)],
          ["FP8 (dense)", hw.fp8_tflops_dense != null ? tf(hw.fp8_tflops_dense) : hw.fp8_supported === false ? "not supported" : OG.na("dense FP8 rate not published")],
          ["FP32", tf(hw.fp32_tflops)], ["Form factor", hw.form_factor], ["Host link", hw.interconnect], ["GPU–GPU link", hw.nvlink || "none"],
          ["TDP", hw.tdp_w ? hw.tdp_w + " W" : null], ["Workload class", hw.workload_class],
          hw.parent ? ["Partition of", h("a", { class: "lnk", href: "/gpu/" + OG.slug(hw.parent) }, OG.shortGpu(hw.parent) + (hw.fraction_of_parent ? ` (${fmt.pct(hw.fraction_of_parent).replace("+", "")})` : ""))] : null,
        ].filter(Boolean);
        hwBox.replaceChildren(h("table", { class: "gp-kv" }, h("tbody", {}, rows.map(([k, v]) => kv(k, v == null ? OG.na("not published / not recorded") : v)))),
          hw.notes ? h("p", { class: "note" }, hw.notes) : null,
          h("p", { class: "note" }, "Vendor peak figures, dense (no sparsity), theoretical; not benchmarks. Sources: ",
            (hw.sources || []).map((s, i) => [i ? ", " : "", ext(s, (s.match(/^https?:\/\/([^/]+)/) || [0, s])[1])]), " · ", h("a", { class: "lnk", href: "/methodology/hardware" }, "Method")));
      }

      /* ---------- capability pricing ---------- */
      function drawCapability() {
        const c = st.g.capability, ms = c.metrics;
        const LBL = { per_gb_vram_hour: "per GB VRAM·h", per_bf16_tflop_hour: "per BF16 TFLOP·h", per_fp16_tflop_hour: "per FP16 TFLOP·h", per_fp8_tflop_hour: "per FP8 TFLOP·h", per_tbps_bandwidth_hour: "per TB/s·h" };
        capBox.replaceChildren(h("table", { class: "gp-kv gp-cap" },
          h("thead", {}, h("tr", {}, h("th", {}, "$"), h("th", { class: "n" }, "spec"), h("th", { class: "n" }, "at low"), h("th", { class: "n" }, "at median"))),
          h("tbody", {}, Object.entries(ms).map(([k, v]) => h("tr", { title: v.unit }, h("th", {}, LBL[k] || k), h("td", { class: "n dim" }, v.spec_value != null ? fmt.num(v.spec_value) : "–"),
            v.low == null ? h("td", { class: "n", colspan: 2 }, OG.na(v.reason)) : [h("td", { class: "n" }, small(v.low)), h("td", { class: "n" }, small(v.median))])))),
          h("p", { class: "note" }, c.label, ". Price / vendor peak figure. ", h("a", { class: "lnk", href: "/methodology/hardware" }, "Method")));
      }

      /* ---------- alternatives ---------- */
      function drawAlternatives() {
        const a = st.g.alternatives;
        if (a.reason && !a.alternatives.length) { altBox.replaceChildren(OG.insufficient(a.reason, "No alternatives")); return; }
        const d = v => h("span", { class: signedCls(v) }, pctTxt(v));
        altBox.replaceChildren(OG.table({
          columns: [
            { key: "gpu", label: "GPU", fmt: v => h("b", {}, OG.shortGpu(v)), href: r => "/gpu/" + OG.slug(r.gpu), value: r => OG.shortGpu(r.gpu) },
            { key: "shares", label: "Shares", sort: false, fmt: v => h("span", { class: "gp-dims" }, v.map(x => OG.badge(DIM_LABEL[x] || x))), csv: r => r.shares.join(" ") },
            { key: "low", label: "Low", num: true, fmt: v => fmt.price(v) },
            { key: "median", label: "Median", num: true, fmt: v => fmt.price(v) },
            { key: "dm", label: "Δ median $", num: true, value: r => r.delta.median_price, fmt: (v, r) => d(r.delta.median_price), title: "alternative's median price vs this GPU's" },
            { key: "dv", label: "Δ VRAM", num: true, value: r => r.delta.vram, fmt: (v, r) => d(r.delta.vram) },
            { key: "db", label: "Δ BF16", num: true, value: r => r.delta.bf16_tflops, fmt: (v, r) => d(r.delta.bf16_tflops) },
            { key: "dbw", label: "Δ mem BW", num: true, value: r => r.delta.memory_bandwidth, fmt: (v, r) => d(r.delta.memory_bandwidth) },
            { key: "providers", label: "Prov", num: true },
            { key: "available_listings", label: "Avail", num: true },
            { key: "cmp", label: "", sort: false, csv: false, fmt: (v, r) => h("a", { class: "lnk dim", href: `/compare/${slug}-vs-${OG.slug(r.gpu)}` }, "compare →") },
          ],
          rows: a.alternatives, compact: true, limit: 12, empty: "No priced GPU shares a dimension with this one.",
          title: "Shares the listed dimensions — not equivalent products", csv: `opengrid-${slug}-alternatives.csv`,
        }), h("p", { class: "note" }, a.note, ". Deltas are the alternative relative to the ", short, ".", " ", h("a", { class: "lnk", href: "/methodology/hardware" }, "Method")));
      }

      /* ---------- regions ---------- */
      async function loadRegions() {
        const rg = await ctx.api("/v1/markets/" + slug + "/regions", { full: true }).catch(() => null);
        const rl = rg && (Array.isArray(rg.data) ? rg.data : rg.data && (rg.data.regions || rg.data.items));
        if (Array.isArray(rl) && rl.length) { drawRegionRows(rl, rg.meta || {}); return; }
        const [ch, pr, av] = await Promise.all(["gpu-region-cheapest", "gpu-region-premium", "gpu-region-availability"].map(k => ctx.api("/v1/heatmaps/" + k).catch(() => null)));
        if (!ch) { regionBox.replaceChildren(OG.insufficient("Regional matrices unavailable on this server (/v1/heatmaps/gpu-region-*).", "No regional data")); return; }
        const row = (mx) => { if (!mx) return null; const i = mx.rows.indexOf(name); return i < 0 ? null : mx.cells[i]; };
        const c = row(ch), p = row(pr), a = row(av);
        if (!c || c.every(v => v == null)) { regionBox.replaceChildren(OG.empty("No priced listing for this GPU in any region now.")); return; }
        const rows = ch.cols.map((g, j) => ({ region: g, cheapest: c[j], premium: p ? p[j] : null, listings: a ? a[j] : null })).filter(r => r.cheapest != null || r.listings);
        const lo = Math.min(...rows.map(r => r.cheapest).filter(v => v != null)), hi = Math.max(...rows.map(r => r.cheapest).filter(v => v != null));
        regionBox.replaceChildren(OG.table({
          columns: [
            { key: "region", label: "Region group", fmt: v => v === "Unassigned" ? h("span", { class: "dim", title: "no location given, or a listing spanning several groups" }, "Unassigned") : h("b", {}, v) },
            { key: "cheapest", label: "Cheapest $/GPU·h", num: true, fmt: v => fmt.price(v) },
            { key: "bar", label: "", sort: false, csv: false, fmt: (v, r) => r.cheapest == null ? "" : h("span", { class: "gp-rbar" }, h("i", { style: `width:${hi > lo ? 8 + 92 * (r.cheapest - lo) / (hi - lo) : 50}%` })) },
            { key: "premium", label: "Regional median vs global", num: true, fmt: v => h("span", { class: signedCls(v) === "up" ? "down" : signedCls(v) === "down" ? "up" : "" }, pctTxt(v)), title: "median of provider lowest prices in region / global median − 1" },
            { key: "listings", label: "Priced listings", num: true },
            { key: "cmp", label: "", sort: false, csv: false, fmt: (v, r) => r.region === "Unassigned" ? "" : h("a", { class: "lnk dim", href: `/compare/${OG.slug(r.region)}-vs-${OG.slug(r.region === "US" ? "Europe" : "US")}?gpu=${slug}` }, "compare →") },
          ],
          rows, sort: { key: "cheapest", dir: "asc" }, compact: true, csv: `opengrid-${slug}-regions.csv`,
        }), h("p", { class: "note" }, OG.kindBadge("observed"), " Region groups from each listing's stated location; never guessed. ", h("a", { class: "lnk", href: "/heatmaps" }, "All regional heatmaps →")));
      }

      function drawRegionRows(rl, meta) {
        const num = (...v) => { for (const x of v) if (x != null && isFinite(x)) return Number(x); return null; };
        const rows = rl.map(r => ({
          region: r.region_group || r.region || r.group || "Unassigned",
          cheapest: num(r.cheapest, r.low, r.lowest, r.cheapest_price), provider: r.cheapest_provider || r.low_provider || null,
          median: num(r.median), premium: num(r.premium_vs_global, r.premium, r.median_vs_global),
          providers: num(r.providers, r.provider_count), listings: num(r.priced_listings, r.listings, r.listing_count), available: num(r.available_listings),
          reason: OG.reasonOf(r),
        }));
        const fin = rows.map(r => r.cheapest).filter(v => v != null), lo = Math.min(...fin), hi = Math.max(...fin);
        regionBox.replaceChildren(OG.table({
          columns: [
            { key: "region", label: "Region group", fmt: v => v === "Unassigned" ? h("span", { class: "dim", title: "no location given, or a listing spanning several groups" }, "Unassigned") : h("b", {}, v) },
            { key: "cheapest", label: "Cheapest $/GPU·h", num: true, fmt: (v, r) => OG.value(v, fmt.price, r.reason || "no priced listing") },
            { key: "provider", label: "at", hidden: !rows.some(r => r.provider), fmt: v => v ? OG.providerLink(v) : "–" },
            { key: "bar", label: "", sort: false, csv: false, fmt: (v, r) => r.cheapest == null || !fin.length ? "" : h("span", { class: "gp-rbar" }, h("i", { style: `width:${hi > lo ? 8 + 92 * (r.cheapest - lo) / (hi - lo) : 50}%` })) },
            { key: "median", label: "Median", num: true, hidden: !rows.some(r => r.median != null), fmt: v => OG.value(v, fmt.price, "–") },
            { key: "premium", label: "vs global", num: true, hidden: !rows.some(r => r.premium != null), fmt: v => h("span", { class: signedCls(v) === "up" ? "down" : signedCls(v) === "down" ? "up" : "" }, pctTxt(v)), title: "regional median vs global median − 1" },
            { key: "providers", label: "Providers", num: true, hidden: !rows.some(r => r.providers != null) },
            { key: "listings", label: "Priced listings", num: true, hidden: !rows.some(r => r.listings != null) },
            { key: "available", label: "Available", num: true, hidden: !rows.some(r => r.available != null) },
            { key: "cmp", label: "", sort: false, csv: false, fmt: (v, r) => r.region === "Unassigned" ? "" : h("a", { class: "lnk dim", href: `/compare/${OG.slug(r.region)}-vs-${OG.slug(r.region === "US" ? "Europe" : "US")}?gpu=${slug}` }, "compare →") },
          ],
          rows, sort: { key: "cheapest", dir: "asc" }, compact: true, csv: `opengrid-${slug}-regions.csv`,
        }), h("p", { class: "note" }, OG.kindBadge("observed"), " Region groups from each listing's stated location; never guessed.", meta.as_of ? ` As of ${when(meta.as_of)}. ` : " ",
          h("a", { class: "lnk", href: "/heatmaps?kind=gpu-region-cheapest" }, "All regional heatmaps →")));
      }

      /* ---------- best available now ---------- */
      async function loadBest() {
        bestBox.replaceChildren(OG.loading("Ranking listings…"));
        let b;
        try { b = await ctx.api("/v1/best/" + slug, { params: { count: st.count, limit: 6 }, slot: "gp-best" }); }
        catch (e) { if (!e.stale) bestBox.replaceChildren(e.status === 404 ? OG.insufficient("Best-execution ranking is not available on this server.", "Unavailable") : OG.error(e, loadBest)); return; }
        const c0 = b.candidates[0];
        const routeHref = "/route?gpu=" + slug + (st.count > 1 ? "&count=" + st.count : "");
        if (!c0) {
          const why = Object.entries(b.exclusions_by_code || {}).map(([k, v]) => `${v} ${k.replace(/_/g, " ")}`).join(", ");
          bestBox.replaceChildren(OG.insufficient(`No eligible ${st.count}-GPU listing now${why ? " (" + why + ")" : ""}.`, "Nothing to route"),
            b.multi_instance_alternatives && b.multi_instance_alternatives.length ? h("p", { class: "note" }, `${b.multi_instance_alternatives.length} multi-instance shape(s) exist; see Route.`) : null,
            h("a", { class: "btn", href: routeHref }, "Open route preview →"));
          return;
        }
        const factors = Object.entries(c0.factors).filter(([, f]) => f.weight > 0);
        bestBox.replaceChildren(
          h("div", { class: "gp-best" },
            h("div", { class: "gp-best-h" }, OG.logo(c0.provider, 18), h("b", {}, c0.provider_display || OG.providerName(c0.provider)), h("span", { class: "dim" }, [c0.region, c0.country].filter(Boolean).join(", ") || "location not given"),
              h("span", { class: "spacer" }), h("span", { class: "gp-best-p" }, fmt.price(c0.price_per_gpu_hour)), h("span", { class: "dim" }, "/GPU·h")),
            h("div", { class: "gp-best-s" }, `score ${c0.score.toFixed(2)} · ${b.mode.toLowerCase()} · ${c0.gpu_count}× ${c0.sku || ""}`, " ", c0.provisionable ? OG.badge("provisionable", "good") : OG.badge("market data only", "", "OpenGrid cannot provision at this provider yet")),
            h("table", { class: "gp-kv gp-fac" }, h("tbody", {}, factors.map(([k, f]) => h("tr", { title: f.note },
              h("th", {}, k.replace(/_/g, " ")), h("td", { class: "gp-barc" }, h("span", { class: "gp-bar" + (f.imputed ? " imp" : ""), style: `width:${Math.round((f.value == null ? 0.5 : f.value) * 100)}%` })),
              h("td", { class: "n dim" }, "×" + f.weight), h("td", { class: "n" }, f.contribution.toFixed(3)), h("td", {}, f.imputed ? OG.badge("neutral", "warn", "no data: scored 0.5") : f.kind ? h("span", { class: "dim" }, f.kind) : ""))))),
            h("p", { class: "note" }, c0.explanation),
            h("div", { class: "gp-best-a" }, h("a", { class: "btn pri", href: routeHref }, "Route this →"), h("a", { class: "lnk dim", href: "/methodology/best-execution" }, "how it is scored"))),
          b.candidates.length > 1 ? h("table", { class: "gp-kv gp-runners" }, h("tbody", {}, b.candidates.slice(1, 6).map(c => h("tr", {},
            h("td", { class: "n dim" }, c.rank), h("td", {}, OG.providerLink(c.provider)), h("td", { class: "dim" }, c.region || "–"), h("td", { class: "n" }, fmt.price(c.price_per_gpu_hour)), h("td", { class: "n dim" }, c.score.toFixed(2)))))) : null,
          h("p", { class: "note" }, `${b.candidates_total} candidates, ${b.exclusions_total} excluded (`, Object.entries(b.exclusions_by_code || {}).map(([k, v]) => `${v} ${k.replace(/_/g, " ")}`).join(", "), "). ",
            "Prices are observed market prices, not quotes. Reliability and performance are not used: no execution history yet."));
      }

      /* ---------- chart ---------- */
      let chart = null;
      const winOf = () => WINDOWS.find(w => w[0] === st.win);
      async function loadChart() {
        const w = winOf(), view = st.view, win = st.win;
        if (!chart) chartEl.replaceChildren(OG.loading("Loading history…"));
        let data = null;
        try {
          if (view === "market") {
            const hist = await ctx.api("/v1/history/" + slug, { params: { window: w[0] }, slot: "gp-chart" }).catch(e => { if (e.stale) throw e; return null; });
            const pts = hist && hist.series ? hist.series.filter(p => p.lowest != null) : [];
            if (pts.length >= 6) data = { kind: "hourly", hist };
          }
          if (!data) {
            const d = await ctx.api("/market/detail", { params: { gpu: name, hours: w[2] }, slot: "gp-chart" });
            data = { kind: "detail", d, thin: view === "market" };
          }
        } catch (e) { if (!e.stale) { chartEl.replaceChildren(OG.error(e, loadChart)); chart = null; } return; }
        if (win !== st.win || view !== st.view) return;
        st.chartData = data; drawChart();
      }
      function overlayEvents() {
        const t = st.tl, out = [];
        if (!t) return out;
        if (st.overlays.has("events") && t.market_events && t.market_events.events) {
          const evs = t.market_events.events.filter(e => SEV_RANK[e.severity] >= 1).sort((a, b) => SEV_RANK[b.severity] - SEV_RANK[a.severity]).slice(0, 80);
          for (const e of evs) out.push({ t: e.occurred_at, label: e.title, kind: (e.type || "").replace(/_/g, " "), severity: e.severity, detail: [e.provider && OG.providerName(e.provider), e.region_group, e.kind].filter(Boolean).join(" · ") });
        }
        if (st.overlays.has("news") && t.news && t.news.items) {
          for (const n of t.news.items.slice(0, 40)) out.push({ t: n.published_at, label: n.title, kind: "news · " + (n.source_name || n.source_id), color: "#4d94ff", detail: "Related by mention, not necessarily causal" });
        }
        if (st.overlays.has("moves") && t.price_moves && t.price_moves.moves) {
          for (const mv of t.price_moves.moves.slice(-60)) out.push({ t: mv.at, label: `Lowest ${mv.direction} ${mv.change_pct > 0 ? "+" : ""}${mv.change_pct}% (${fmt.price(mv.from)} → ${fmt.price(mv.to)})`,
            kind: "move · inferred", color: mv.direction === "up" ? "#2fbf71" : "#ef5350", detail: mv.provider_set_changed ? "provider set changed this hour: " + describeSet(mv.provider_set_changed) : "same providers before and after",
            onClick: () => around(mv, chartAround), hint: "click: related news and events around this move" });
        }
        return out;
      }
      function drawChart() {
        const data = st.chartData;
        if (!data) return;
        let opts;
        const events = overlayEvents();
        if (data.kind === "hourly") {
          const hst = data.hist, s = hst.series;
          const idxPts = hst.index && hst.index.points ? new Map(hst.index.points.filter(p => p.published && p.level != null).map(p => [p.t, p.level])) : new Map();
          const times = s.map(p => p.t);
          const fin = L.finite(s.map(p => p.median)), dir = fin.length >= 2 ? L.direction((fin[fin.length - 1] - fin[0]) / fin[0]) : "none";
          const series = [
            { key: "median", label: "Median", values: s.map(p => p.median), color: "#d7dde4", width: 1.75, strong: true },
            { key: "lowest", label: "Lowest", values: s.map(p => p.lowest), color: dir === "down" ? "#ef5350" : "#2fbf71", width: 1.5 },
          ];
          if (idxPts.size) series.push({ key: "index", label: "OG index", values: times.map(t => idxPts.get(t) ?? null), color: "#f5a524", width: 1.25, dash: "4 3" });
          opts = { times, series, band: { lo: s.map(p => p.lowest), hi: s.map(p => p.highest), label: "low–high" }, hatchBefore: hst.coherent_from };
          chartNote.replaceChildren(OG.kindBadge("observed"), ` Hourly cross-section of provider prices (one vote per provider), ${s.length} hours. `,
            hst.coherent_from ? `Hatched: before ${when(hst.coherent_from)} not every current provider was being recorded, so a level change there can be a provider joining, not a market move. ` : "",
            idxPts.size ? "Dashed: OpenGrid index. " : "", events.length ? `${events.length} markers on the time axis (hover). ` : "", h("a", { class: "lnk", href: "/methodology/indices" }, "Method"));
        } else {
          const d = data.d;
          if (!d.times || !d.providers.length) { chartEl.replaceChildren(OG.empty("No price history for this GPU in this window.")); chart = null; chartNote.replaceChildren(); return; }
          const first = Math.min(...[d.lowest, ...d.providers.map(p => p.series)].map(a => { const i = a.findIndex(v => v != null); return i < 0 ? Infinity : i; }));
          const i0 = isFinite(first) && d.times.length - first >= 3 ? first : 0;
          const sl = a => a.slice(i0);
          if (st.view === "market") {
            opts = { series: [{ key: "median", label: "Median", values: sl(d.median), color: "#d7dde4", width: 1.75, strong: true }, { key: "lowest", label: "Lowest", values: sl(d.lowest), color: "#2fbf71", width: 1.5 }],
              band: { lo: sl(d.lowest), hi: sl(d.highest), label: "low–high" } };
          } else {
            opts = { series: [{ key: "__low", label: "Best price", values: sl(d.lowest), color: "#ffffff", halo: true },
              ...d.providers.filter(p => p.series.some(v => v != null)).map(p => ({ key: p.provider, label: OG.providerName(p.provider), values: sl(p.series), color: L.providerColor(p.provider) }))] };
          }
          Object.assign(opts, { times: sl(d.times), hatchBefore: d.market_from });
          const began = i0 > 0 ? d.times[i0] : null;
          chartNote.replaceChildren(OG.kindBadge("observed"), " Step lines: a posted price holds until it changes. ",
            data.thin ? "Hourly history is too short for this window, so this is the fine-grained replay of recorded listings. " : "",
            began ? `History begins ${when(began)}, when recording started. ` : "",
            d.market_from ? `Hatched: before ${when(d.market_from)} not every provider shown was recorded yet. ` : "",
            events.length ? `${events.length} markers on the time axis (hover). ` : "");
        }
        Object.assign(opts, { events, height: 340, label: `Price history for ${name}`, emptyText: "No price history in this window" });
        if (chart) chart.destroy();
        chartEl.textContent = "";
        chart = OG.charts.timeseries(chartEl, opts);
      }
      ctx.onCleanup(() => chart && chart.destroy());
      const describeSet = s => [s.added && s.added.length ? "+" + s.added.map(OG.providerName).join(", +") : "", s.removed && s.removed.length ? "−" + s.removed.map(OG.providerName).join(", −") : "",
        s.newly_recorded && s.newly_recorded.length ? "newly recorded " + s.newly_recorded.map(OG.providerName).join(", ") : ""].filter(Boolean).join(" ");

      /* ---------- timeline: availability, moves, overlay ---------- */
      let availChart = null;
      async function loadTimeline() {
        const win = st.win, w = win === "all" ? "180d" : win;
        let t;
        try { t = await ctx.api("/v1/timeline", { params: { gpu: slug, window: w }, slot: "gp-tl" }); }
        catch (e) {
          if (e.stale) return;
          st.tl = null; drawChart();
          availEl.replaceChildren(OG.insufficient(e.message, "Availability history unavailable")); movesBox.replaceChildren(OG.insufficient(e.message, "Moves unavailable"));
          return;
        }
        if (win !== st.win) return;
        st.tl = t; drawChart();
        // availability history
        const pts = (t.prices && t.prices.points) || [];
        if (availChart) { availChart.destroy(); availChart = null; }
        if (pts.length < 2) availEl.replaceChildren(OG.insufficient(t.prices && t.prices.unavailable ? t.prices.unavailable : `only ${pts.length} hour${pts.length === 1 ? "" : "s"} recorded in this window`, "Not enough availability history"));
        else {
          availEl.textContent = "";
          availChart = OG.charts.timeseries(availEl, { times: pts.map(p => p.hour), height: 150, yFmt: v => fmt.num(v, 0), yZero: true, label: "availability history",
            series: [
              { key: "avail", label: "Available listings", values: pts.map(p => p.available_listings), color: "#2fbf71" },
              { key: "prov", label: "Priced providers", values: pts.map(p => p.priced_providers), color: "#4d94ff" },
              { key: "sold", label: "Sold-out listings", values: pts.map(p => p.sold_out_listings), color: "#ef5350", width: 1.25 },
            ] });
          ctx.onCleanup(() => availChart && availChart.destroy());
        }
        availNote.replaceChildren(OG.kindBadge("observed"), " Hourly samples from the market rollup. Available = explicit availability; many providers publish none (counted as unknown, not available). ",
          (t.availability_changes && t.availability_changes.changes || []).length ? `${t.availability_changes.changes.length} inferred provider availability transitions in this window.` : "");
        drawMoves(t);
      }
      function drawMoves(t) {
        const mv = (t.price_moves && t.price_moves.moves || []).slice().reverse();
        if (!mv.length) { movesBox.replaceChildren(OG.empty(`No hour-over-hour move of the lowest price ≥ ${(t.price_moves && t.price_moves.threshold_pct) || 3}% in this window.`)); return; }
        movesBox.replaceChildren(OG.table({
          columns: [
            { key: "at", label: "Hour", fmt: v => fmt.dateTime(v) },
            { key: "change_pct", label: "Move", num: true, fmt: v => h("span", { class: v > 0 ? "up" : "down" }, (v > 0 ? "+" : "") + v.toFixed(1) + "%") },
            { key: "from", label: "From → to", num: true, fmt: (v, r) => `${fmt.price(r.from)} → ${fmt.price(r.to)}` },
            { key: "provider_set_changed", label: "Providers", value: r => r.provider_set_changed ? 1 : 0, fmt: v => v ? h("span", { class: "warn", title: describeSet(v) }, "set changed") : h("span", { class: "dim" }, "same") },
          ],
          rows: mv, compact: true, limit: 12, rowKey: r => r.at, onRow: r => around(r),
          title: "Click a move: related news and events", empty: "No moves.",
        }));
      }
      // "Related news and events around this move (not necessarily causal)", in place: under the chart when a
      // marker is clicked, under the moves table when a row is clicked
      async function around(mv, box) {
        const aroundBox = box || aroundBox0;
        (aroundBox === chartAround ? aroundBox0 : chartAround).replaceChildren();
        aroundBox.replaceChildren(OG.loading("Looking around " + fmt.dateTime(mv.at) + "…"));
        if (aroundBox === chartAround) aroundBox.scrollIntoView({ block: "nearest" });
        let a;
        try { a = await ctx.api("/v1/timeline/around", { params: { gpu: slug, at: mv.at, window_hours: 48 }, slot: "gp-around" }); }
        catch (e) { if (!e.stale) aroundBox.replaceChildren(OG.error(e)); return; }
        const rel = a.related || [];
        aroundBox.replaceChildren(h("div", { class: "gp-around-h" }, h("b", {}, a.heading || "Related news and events around this move (not necessarily causal)"),
          h("button", { class: "btn sm", title: "Close", onclick: () => aroundBox.replaceChildren() }, "×")),
          h("div", { class: "dim" }, `${fmt.dateTime(mv.at)} · lowest ${fmt.price(mv.from)} → ${fmt.price(mv.to)} · ±${a.window_hours}h`, a.move && a.move.unavailable ? " · " + a.move.unavailable : ""),
          rel.length ? h("ul", { class: "gp-list" }, rel.slice(0, 12).map(r => h("li", {},
            h("span", { class: "mono dim", title: r.position + " the move" }, (r.hours_from_move < 0 ? "−" : "+") + Math.abs(r.hours_from_move).toFixed(0) + "h"),
            r.type === "news" ? OG.badge("news") : OG.badge((r.event && r.event.severity) || "event", r.event && r.event.severity === "major" ? "bad" : r.event && r.event.severity === "notable" ? "warn" : ""),
            r.item && r.item.url ? ext(r.item.url, r.title) : h("span", {}, r.title))))
            : OG.empty(a.market_events_available === false ? "Market events are not recorded on this server; no related news in the window." : "Nothing recorded within the window."));
      }

      /* ---------- events, news, listings ---------- */
      async function loadEvents(minSev) {
        minSev = minSev || OG.qs.get("sev", "notable");
        let r;
        try { r = await ctx.api("/v1/events", { params: { gpu: slug, limit: 60, min_severity: minSev === "info" ? null : minSev }, slot: "gp-ev" }); }
        catch (e) { if (!e.stale) eventsBox.replaceChildren(e.status === 404 ? OG.insufficient("Market events are not available on this server.", "Unavailable") : OG.error(e, () => loadEvents(minSev))); return; }
        const rows = list(r);
        eventsBox.replaceChildren(OG.table({
          columns: [
            { key: "occurred_at", label: "When", fmt: v => fmt.dateTime(v) },
            { key: "severity", label: "Sev", fmt: v => OG.badge(v, v === "major" ? "bad" : v === "notable" ? "warn" : "") , value: r => SEV_RANK[r.severity] },
            { key: "type", label: "Type", cls: "dim", fmt: v => (v || "").replace(/_/g, " ") },
            { key: "title", label: "Event", cls: "wrap" },
            { key: "provider", label: "Provider", fmt: v => v ? OG.providerLink(v) : h("span", { class: "dim" }, "market") },
            { key: "kind", label: "Kind", fmt: v => v ? OG.kindBadge(v) : "–" },
          ],
          rows, compact: true, limit: 15, empty: "No market events for this GPU at this severity.", csv: `opengrid-${slug}-events.csv`,
        }), h("p", { class: "note" }, h("a", { class: "lnk", href: "/events?gpu=" + slug }, "All events for this GPU →"), " · ", h("a", { class: "lnk", href: "/methodology/events" }, "How events are detected")));
      }
      async function loadNews() {
        let r;
        try { r = await ctx.api("/v1/news", { params: { gpu: slug, limit: 12 } }); }
        catch (e) { newsBox.replaceChildren(e.status === 404 ? OG.insufficient("News is not available on this server.", "Unavailable") : OG.error(e, loadNews)); return; }
        const items = list(r);
        if (!items.length) { newsBox.replaceChildren(OG.empty("No news item mentioning this GPU has been ingested yet.")); return; }
        newsBox.replaceChildren(h("ul", { class: "gp-list gp-news" }, items.map(n => h("li", {},
          h("span", { class: "mono dim" }, fmt.date(n.published_at)), OG.badge(n.trust_tier || "source", n.trust_tier === "official" ? "good" : ""),
          h("span", {}, ext(n.url, n.title), h("span", { class: "dim" }, " · " + (n.source_name || n.source_id) + (n.source_count > 1 ? ` +${n.source_count - 1}` : "")))))),
          h("p", { class: "note" }, "Linked by mention of this GPU (inferred by rules). Appearing near a price move does not mean it caused it. ", h("a", { class: "lnk", href: "/news?gpu=" + slug }, "More →")));
      }
      async function loadListings() {
        try {
          const rows = (await ctx.api("/listings", { params: { q: name } })).filter(r => r.canonical_gpu_name === name);
          listingsBox.replaceChildren(OG.table({
            columns: [
              { key: "provider", label: "Provider", fmt: v => OG.providerLink(v), value: r => OG.providerName(r.provider) },
              { key: "sku", label: "SKU", cls: "dim mono" },
              { key: "gpu_count", label: "GPUs", num: true },
              { key: "price_per_gpu_hour", label: "$/GPU·h", num: true, fmt: v => fmt.price(v) },
              { key: "price_per_instance_hour", label: "$/inst·h", num: true, fmt: v => fmt.price(v) },
              { key: "market_type", label: "Type", cls: "dim" },
              { key: "provider_tier", label: "Tier", cls: "dim" },
              { key: "region", label: "Region", cls: "dim" },
              { key: "available", label: "Stock", fmt: v => v === true ? "in stock" : v === false ? h("span", { class: "down" }, "sold out") : h("span", { class: "dim" }, "unknown") },
              { key: "observed_at", label: "Seen", num: true, fmt: (v, r) => OG.freshBadge(v, { provider: r.provider }) },
            ],
            rows, sort: { key: "price_per_gpu_hour", dir: "asc" }, compact: true, limit: 25, csv: `opengrid-${slug}-listings.csv`,
            title: `${rows.length} listings (all market types, incl. spot and sold out)`, empty: "No current listings.",
          }));
        } catch (e) { listingsBox.replaceChildren(OG.error(e, loadListings)); }
      }

      // section nav: mark the section in view
      const scroller = document.querySelector(".main");
      const onScroll = () => {
        const top = navEl.getBoundingClientRect().bottom + 8;
        let cur = null;
        for (const s of el.querySelectorAll(".gp-main > .gp-sec")) if (s.getBoundingClientRect().top <= top + 40) cur = s.dataset.sec;
        navEl.querySelectorAll("button").forEach(b => b.classList.toggle("on", b.dataset.for === cur));
      };
      if (scroller) { scroller.addEventListener("scroll", onScroll, { passive: true }); ctx.onCleanup(() => scroller.removeEventListener("scroll", onScroll)); }

      loadSummary(); loadIndex(); loadMarketChanges(); loadChart(); loadTimeline(); loadTrust(); loadRegions(); loadBest(); loadEvents(); loadNews(); loadListings();
      ctx.every(60000, () => { loadSummary(); loadBest(); });
      ctx.every(300000, () => { loadIndex(); loadChart(); loadTimeline(); loadTrust(); loadRegions(); loadEvents(); });
      const esc = e => { const hp = document.getElementById("help");
        if (e.key === "Escape" && !/INPUT|SELECT|TEXTAREA/.test(e.target.tagName) && (!hp || hp.hidden)) OG.go("/gpus"); };
      document.addEventListener("keydown", esc);
      ctx.onCleanup(() => document.removeEventListener("keydown", esc));
    },
  });
})();
