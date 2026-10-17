/* /markets: the on-demand GPU market at a glance. One /v1/overview request for the market sections, plus
   /v1/indices (soft), /v1/spreads, /v1/events?min_severity=notable, /v1/news (soft) and /market (sparklines only).
   First screen: benchmark indices, a one-line summary, the biggest movers and latest price changes, then the full
   board with events and headlines. The secondary signals (unusual moves, volatility, spreads, coverage,
   availability, new and sold-out GPUs) sit under "More market signals". A section the API marks unavailable shows
   its reason in place, never zeros. The ticker and the status bar belong to the shell (the same on every page).
   Refreshes every 60 s. */
(() => {
  const { h, fmt, Lib: L } = OG;
  const BENCH = ["gpu-compute", "h100-80gb-sxm5", "h200-141gb-sxm5", "b200-180gb-sxm", "a100-80gb-sxm4", "l40s-48gb"];

  // ---- small building blocks ----
  // mini table: cols [{label, num, cell(row) -> node|string, title, cls, w}]. Fixed layout: numeric columns get
  // their width (w, default 52px), the name column takes the rest and ellipsizes (full name on hover).
  function mini(cols, rows) {
    return h("table", { class: "grid-t compact ov-mt" },
      h("thead", {}, h("tr", {}, cols.map(c => h("th", { class: c.num ? "n" : null, title: c.title || null, style: c.w || c.num ? `width:${c.w || 52}px` : null }, c.label)))),
      h("tbody", {}, rows.map(r => h("tr", {}, cols.map(c => h("td", { class: (c.num ? "n " : "") + (c.cls || "") }, c.cell(r)))))));
  }
  const loading = () => h("div", { class: "ov-ld" }, "Loading…");
  // A section the API could not compute: its reason in plain words. A failed request: one calm line, detail on hover.
  const unavail = reason => h("div", { class: "ov-un" }, "Not available yet: " + (reason || "no reason given"));
  const failed = err => h("div", { class: "ov-un", title: err && err.message ? String(err.message) : "" }, "Could not load this. It retries every minute.");
  // Panel: header (title, kind badge, rule tooltip, optional link) + body that renders a /v1/overview section.
  function panel(title, opts) {
    opts = opts || {};
    const meta = h("span", { class: "ov-pm" });
    const body = h("div", { class: "ov-pb" }, loading());
    const el = h("section", { class: "ov-p" + (opts.cls ? " " + opts.cls : "") },
      h("header", { class: "ov-ph" }, opts.href ? h("a", { class: "lnk ov-pt", href: opts.href }, title) : h("span", { class: "ov-pt" }, title), meta),
      body);
    return {
      el,
      // sec: {available, reason, kind, items, rule, note}; render(items) -> node
      set(sec, render, emptyText) {
        meta.replaceChildren();
        if (!sec) { body.replaceChildren(unavail("not in this response")); return; }
        if (sec.kind) meta.append(OG.kindBadge(sec.kind));
        if (sec.rule || opts.rule) meta.append(h("span", { class: "ov-rule", title: sec.rule || opts.rule }, "?"));
        if (sec.available === false) { body.replaceChildren(unavail(sec.reason)); return; }
        const items = sec.items || [];
        body.replaceChildren(...[items.length ? render(items) : h("div", { class: "ov-none" }, emptyText || "None right now."),
          sec.note ? h("div", { class: "ov-note" }, sec.note) : null].filter(Boolean));
      },
      fail(err) { meta.replaceChildren(); body.replaceChildren(failed(err)); },
      body,
    };
  }
  const gpu = r => OG.gpuLink(r.gpu || r.gpu_slug);   // gpuLink carries the full name as its title
  const delta = (v, title) => v == null ? OG.na(title) : h("span", { class: "chg " + (v > 0 ? "up" : v < 0 ? "down" : "flat") }, (v > 0 ? "+" : v < 0 ? L.MINUS : "") + Math.abs(v));
  const shortIndex = n => String(n || "").replace(/^OpenGrid\s+/, "").replace(/\s+Index$/, "").replace(/\s*\(interruptible\)/, "");

  // ---- indices strip ----
  function indexCell(ix) {
    const usd = ix.unit === "usd_per_gpu_hour";
    const lvl = ix.level == null ? null : usd ? fmt.price(ix.level) : fmt.num(ix.level, 2);
    const ch = w => ix.changes && ix.changes[w] != null ? OG.chg(ix.changes[w]) : OG.na((ix.change_reasons || {})[w] || ix.reason || "unavailable");
    return h("a", { class: "ov-ix" + (ix.level == null ? " off" : ""), href: "/indices/" + encodeURIComponent(ix.id), title: ix.name + (ix.reason ? "\n" + ix.reason : "") },
      h("div", { class: "ov-ix-n" }, shortIndex(ix.name)),
      h("div", { class: "ov-ix-v" }, lvl == null ? h("span", { class: "ov-ix-off" }, "-") : [lvl, h("span", { class: "ov-ix-u" }, usd ? "/GPU-hr" : "pts")]),
      ix.level == null
        ? h("div", { class: "ov-ix-r" }, "not published yet")
        : h("div", { class: "ov-ix-c" }, h("span", { class: "dimmer" }, "24h"), ch("24h"),
          ix.changes && ix.changes["7d"] != null ? [h("span", { class: "dimmer" }, "7d"), ch("7d")] : null));
  }
  function drawIndices(box, list) {
    if (!Array.isArray(list)) { box.replaceChildren(h("div", { class: "ov-ix-un" }, failed())); return; }
    const byId = new Map(list.map(x => [x.id, x]));
    const cells = BENCH.map(id => byId.get(id) ? indexCell(byId.get(id)) : null).filter(Boolean);
    if (!cells.length) { box.replaceChildren(h("div", { class: "ov-ix-un" }, unavail(list.length ? "no benchmark index has data yet" : "no index has data yet"))); return; }
    box.replaceChildren(...cells, h("a", { class: "ov-ix ov-ix-all", href: "/indices" }, `All ${list.length} indices →`));
  }

  // ---- one-line summary: direction, stock, listings ----
  function summaryLine(o) {
    const parts = [];
    const ch = (o.board.items || []).map(b => b.change_24h_median).filter(v => v != null);
    if (o.gainers && o.gainers.available === false) parts.push(h("span", {}, h("b", {}, "24h "), h("span", { class: "dim" }, "not available yet: " + o.gainers.reason)));
    else if (ch.length) {
      const up = ch.filter(v => v > 0.0005).length, dn = ch.filter(v => v < -0.0005).length, med = ch.slice().sort((a, b) => a - b)[Math.floor(ch.length / 2)];
      parts.push(h("span", {}, h("b", {}, "24h "), h("span", { class: "up" }, `${up} up`), " · ", h("span", { class: "down" }, `${dn} down`), " · ", `${ch.length - up - dn} flat`,
        h("span", { class: "dim" }, " · typical move "), OG.chg(med)));
    }
    const c = o.capacity || {};
    if (c.available !== false) {
      parts.push(h("span", {}, h("b", {}, "In stock "), fmt.num(c.available_listings_now), h("span", { class: "dim" }, " listings"),
        c.available_listings_change_24h == null ? null : h("span", {}, " (", delta(c.available_listings_change_24h), " 24h)")));
      parts.push(h("span", {}, h("b", {}, "Sold out "), h("span", { class: c.gpus_sold_out_everywhere ? "warn" : "" }, fmt.num(c.gpus_sold_out_everywhere)), h("span", { class: "dim" }, " GPUs everywhere")));
    }
    return parts;
  }

  // ---- section renderers (plain column names; the exact rule is in each header's tooltip) ----
  const R = {
    movers: items => mini([
      { label: "GPU", cell: gpu },
      { label: "Median", num: true, w: 60, cell: r => fmt.price(r.median_now) },
      { label: "24h", num: true, w: 74, cell: r => OG.chg(r.median_pct) },
    ], items.slice(0, 6)),
    spreads: rows => mini([
      { label: "GPU", cell: gpu },
      { label: "Low", num: true, w: 58, cell: r => fmt.price(r.low) },
      { label: "High", num: true, w: 60, cell: r => fmt.price(r.high) },
      { label: "Gap", num: true, w: 54, title: "how much more the priciest provider asks than the cheapest", cell: r => h("span", { title: r.label || "" }, fmt.pct(r.spread_pct_of_low).replace(/^\+/, "")) },
    ], rows.slice(0, 6)),
    volatile: items => mini([
      { label: "GPU", cell: gpu },
      { label: "Hourly", num: true, w: 62, title: "typical hourly change of the median price over 7 days (standard deviation)", cell: r => fmt.pct(r.hourly_stdev).replace(/^\+/, "") },
      { label: "7d range", num: true, w: 74, cell: r => fmt.pct(r.range_7d).replace(/^\+/, "") },
    ], items.slice(0, 6)),
    liquid: items => mini([
      { label: "GPU", cell: gpu },
      { label: "Sellers", num: true, w: 64, title: "providers with purchasable listings", cell: r => String(r.providers_priced) },
      { label: "Listings", num: true, w: 72, title: "in stock / live listings", cell: r => h("span", {}, String(r.available_listings), h("span", { class: "dimmer" }, "/" + r.live_listings)) },
    ], items.slice(0, 6)),
    avail: items => mini([
      { label: "GPU", cell: gpu },
      { label: "Sellers", num: true, w: 64, title: "providers with purchasable listings, 24h ago → now", cell: r => h("span", {}, h("span", { class: "dim" }, r.providers_priced_then + "→"), String(r.providers_priced_now)) },
      { label: "Listings", num: true, w: 72, title: "available listings, 24h ago → now", cell: r => h("span", {}, h("span", { class: "dim" }, r.available_listings_then + "→"), String(r.available_listings_now)) },
      { label: "Change", num: true, w: 62, cell: r => delta(r.delta_listings) },
    ], items.slice(0, 6)),
    newly: items => mini([
      { label: "GPU", cell: gpu },
      { label: "First at", w: 76, cell: r => OG.providerLink(r.first_provider, { logo: false }) },
      { label: "Since", num: true, w: 58, cell: r => h("span", { title: fmt.dateTime(r.first_priced) }, fmt.date(r.first_priced)) },
      { label: "Low", num: true, w: 60, cell: r => OG.value(r.lowest_now, fmt.price, "not priced now") },
    ], items.slice(0, 6)),
    sold: items => mini([
      { label: "GPU", cell: gpu },
      { label: "Providers", cls: "ov-wrap", w: 120, cell: r => h("span", {}, (r.providers || []).map((p, i) => [i ? ", " : "", OG.providerLink(p, { logo: false })])) },
      { label: "Last low", num: true, w: 58, cell: r => r.last_lowest == null ? h("span", { class: "dimmer", title: "never priced since tracking began" }, "never") : h("span", { title: "last priced " + fmt.dateTime(r.last_priced_at) }, fmt.price(r.last_lowest)) },
    ], items.slice(0, 6)),
    unusual: items => mini([
      { label: "GPU", cell: gpu },
      { label: "24h", num: true, cell: r => OG.chg(r.change_24h_median) },
      { label: "vs usual", num: true, w: 64, title: "how many standard deviations from the same move on each of the previous 30 days (z-score)", cell: r => fmt.num(r.z, 1) + "×" },
    ], items.slice(0, 6)),
    changes: items => mini([
      { label: "Time", w: 58, cell: r => h("span", { class: "mono dim" }, fmt.time(r.changed_at).slice(0, 5)) },
      { label: "GPU", cell: gpu },
      { label: "Provider", w: 110, cell: r => OG.providerLink(r.provider, { logo: false }) },
      { label: "Was", num: true, w: 60, cell: r => h("span", { class: "dim" }, fmt.price(r.previous_price)) },
      { label: "Now", num: true, w: 60, cell: r => fmt.price(r.price) },
      { label: "Change", num: true, w: 74, cell: r => OG.chg(r.pct) },
    ], items.slice(0, 10)),
  };

  // ---- market board ----
  function boardTable() {
    return OG.table({
      columns: [
        { key: "gpu", label: "GPU", fmt: v => h("b", {}, L.shortGpu(v)), value: r => L.shortGpu(r.gpu), href: r => "/gpu/" + r.gpu_slug },
        { key: "providers", label: "Providers", num: true, desc: true, title: "providers with purchasable listings now" },
        { key: "lowest", label: "Low", num: true, fmt: v => fmt.price(v), title: "lowest on-demand price per GPU-hour now" },
        { key: "lowest_provider", label: "Cheapest at", fmt: v => OG.providerLink(v) },
        { key: "median", label: "Median", num: true, fmt: v => fmt.price(v), title: "median of provider prices (one vote each)" },
        { key: "highest", label: "High", num: true, fmt: v => fmt.price(v) },
        { key: "change_24h_median", label: "24h", num: true, desc: true, title: "change of the median provider price over 24 hours (providers priced at both times only)",
          fmt: (v, r) => OG.chg(v, { reason: r.providers_matched < 2 ? "needs 2 providers priced 24h ago and now" : "not enough history" }) },
        { key: "spark", label: "Last 24h", sort: false, csv: false, title: "lowest price over the last 24 hours", fmt: (v, r) => r.spark ? OG.charts.sparkline(r.spark, { dir: fmt.dir(r.spark_chg), width: 84, height: 16 }) : h("span", { class: "dimmer" }, "-") },
      ],
      rows: [], sort: { key: "providers", dir: "desc" }, compact: true, rowKey: r => r.gpu, rowHref: r => "/gpu/" + r.gpu_slug, limit: 24,
      title: "Every GPU", csv: "opengrid-board.csv", empty: "No GPU is priced right now.",
    });
  }

  OG.page("/markets", {
    title: "Markets",
    nav: "markets",
    mount(el, params, query, ctx) {
      el.classList.add("pg-overview");
      const ixBox = h("div", { class: "ov-ixs" }, loading());
      const sumBox = h("div", { class: "ov-sum" }, h("span", { class: "dim" }, "Loading…"));
      const P = {
        gainers: panel("Biggest rises · 24h", { rule: "change of the median provider price over 24 hours, providers priced at both times only" }),
        losers: panel("Biggest falls · 24h", { rule: "change of the median provider price over 24 hours, providers priced at both times only" }),
        changes: panel("Latest price changes", { cls: "span2", rule: "per-listing price changes in the last 24 hours (above a 0.5% noise floor)" }),
      };
      const M = {   // secondary signals, folded
        unusual: panel("Unusual moves"),
        volatile: panel("Most volatile · 7d"),
        spreads: panel("Widest price gaps", { href: "/heatmaps", rule: "highest / lowest provider price now (2 or more providers)" }),
        liquid: panel("Most providers"),
        gain: panel("More in stock · 24h"),
        lose: panel("Less in stock · 24h"),
        newly: panel("New on the market · 7d"),
        sold: panel("Sold out everywhere"),
      };
      const history = h("p", { class: "note ov-hist" });
      const board = boardTable();
      const evBox = h("div", { class: "ov-evs" }, loading());
      const newsBox = h("div", { class: "ov-news" }, loading());
      el.append(
        OG.head("Markets", "Observed on-demand list prices per GPU-hour across providers."),
        ixBox, sumBox,
        h("div", { class: "ov-grid" }, Object.values(P).map(p => p.el)),
        h("div", { class: "ov-low" },
          h("div", { class: "ov-board" }, board),
          h("div", { class: "ov-side" },
            h("section", { class: "ov-p" }, h("header", { class: "ov-ph" }, h("a", { class: "lnk ov-pt", href: "/events?sev=notable" }, "Notable events"), h("a", { class: "lnk ov-more", href: "/events" }, "all →")), evBox),
            h("section", { class: "ov-p" }, h("header", { class: "ov-ph" }, h("a", { class: "lnk ov-pt", href: "/news" }, "Headlines"), h("a", { class: "lnk ov-more", href: "/news" }, "all →")), newsBox))),
        h("details", { class: "ov-adv" },
          h("summary", {}, "More market signals", h("span", { class: "dim" }, " · unusual moves, volatility, price gaps, stock, new and sold-out GPUs")),
          h("div", { class: "ov-grid" }, Object.values(M).map(p => p.el)),
          history));

      let spark = new Map();
      let lastBoard = [];
      function drawBoard(items) {
        lastBoard = items;
        board.update(items.map(b => { const s = spark.get(b.gpu); return Object.assign({}, b, s ? { spark: s.spark, spark_chg: s.change_pct } : {}); }));
      }

      async function loadOverview() {
        let o;
        try { o = await ctx.api("/v1/overview", { nocache: true }); }
        catch (e) {
          for (const p of [...Object.values(P), ...Object.values(M)]) p.fail(e);
          sumBox.replaceChildren(OG.error(e, loadOverview)); return;
        }
        history.replaceChildren(o.as_of_hour ? `Market hour ${fmt.dateTime(o.as_of_hour)} · ${o.history_hours}h of history since ${fmt.dateTime(o.tracking_since)}. ` : "No market history yet. ",
          h("a", { class: "lnk", href: "/methodology/events#overview" }, "How these are computed"));
        sumBox.replaceChildren(...summaryLine(o).flatMap((n, i) => i ? [h("span", { class: "ov-sep" }, "│"), n] : [n]));
        P.gainers.set(o.gainers, R.movers, "No GPU's median price rose in 24h.");
        P.losers.set(o.losers, R.movers, "No GPU's median price fell in 24h.");
        P.changes.set(o.price_changes, R.changes, "No listing changed price in 24h.");
        M.unusual.set(o.unusual, R.unusual, "No unusual 24h move.");
        M.volatile.set(o.volatile, R.volatile);
        M.liquid.set(o.liquid, R.liquid);
        M.gain.set(o.availability_gaining, R.avail, "No GPU gained stock vs 24h ago.");
        M.lose.set(o.availability_losing, R.avail, "No GPU lost stock vs 24h ago.");
        M.newly.set(o.newly_available, R.newly, "No GPU first went on sale in the last 7 days.");
        M.sold.set(o.sold_out, R.sold, "Every GPU with live listings is in stock somewhere.");
        if (o.board.available === false) board.update([]);
        else drawBoard(o.board.items || []);
      }
      async function loadSpreads() {
        let rows;
        try { rows = await ctx.api("/v1/spreads", { params: { min_providers: 2 }, nocache: true }); }
        catch (e) { M.spreads.fail(e); return; }
        const list = (Array.isArray(rows) ? rows : []).filter(r => r.spread_pct_of_low != null).sort((a, b) => b.spread_pct_of_low - a.spread_pct_of_low);
        M.spreads.set({ available: true, items: list }, R.spreads, "No GPU has 2 or more priced providers.");
      }
      async function loadEvents() {
        try {
          const evs = await ctx.api("/v1/events", { params: { min_severity: "notable", limit: 10 }, nocache: true });
          evBox.replaceChildren(evs.length ? h("div", { class: "ov-evl" }, evs.map(e => OG.views.eventRow(e, { compact: true })))
            : h("div", { class: "ov-none" }, "No notable events yet."));
        } catch (e) { evBox.replaceChildren(failed(e)); }
      }
      async function loadNews() {
        const n = await OG.api.soft("/v1/news", { params: { limit: 8 } });
        if (!ctx.alive()) return;
        if (n == null) { newsBox.replaceChildren(failed()); return; }
        if (!n.length) { newsBox.replaceChildren(h("div", { class: "ov-none" }, "No headlines yet.")); return; }
        newsBox.replaceChildren(...n.map(s => h("div", { class: "ov-nw" },
          h("span", { class: "ov-nw-t mono dim" }, fmt.age(s.published_at || s.first_seen_at)),
          h("a", { class: "lnk ov-nw-h", href: s.url, target: "_blank", rel: "noopener external", title: s.summary || s.title }, s.title),
          h("span", { class: "ov-nw-s dim" }, s.source_name + (s.source_count > 1 ? ` +${s.source_count - 1}` : "")))));
      }
      async function loadIndices() {
        const list = await OG.api.soft("/v1/indices", { nocache: true });
        if (ctx.alive()) drawIndices(ixBox, list);
      }
      async function loadSpark() {
        const m = await OG.data.market(24).catch(() => null);
        if (!m || !ctx.alive()) return;
        spark = new Map(m.gpus.map(g => [g.gpu, g]));
        if (lastBoard.length) drawBoard(lastBoard);
      }
      const all = () => { loadOverview(); loadIndices(); loadSpreads(); loadEvents(); loadNews(); loadSpark(); };
      all();
      ctx.every(60000, all);
    },
  });
})();
