/* / — Market overview. One /v1/overview request for the market sections, plus /v1/indices (soft),
   /v1/spreads, /v1/events?min_severity=notable, /v1/news (soft) and /market (sparklines only).
   Every section the API marks unavailable shows its reason in place, never zeros. Refreshes every 60 s. */
(() => {
  const { h, fmt, Lib: L } = OG;
  const BENCH = ["gpu-compute", "h100-80gb-sxm5", "h200-141gb-sxm5", "b200-180gb-sxm", "a100-80gb-sxm4", "l40s-48gb", "h100-class", "h200-class", "a100-class"];

  // ---- small building blocks ----
  // mini table: cols [{label, num, cell(row) -> node|string, title, cls}]
  function mini(cols, rows) {
    return h("table", { class: "grid-t compact ov-mt" },
      h("thead", {}, h("tr", {}, cols.map(c => h("th", { class: c.num ? "n" : null, title: c.title || null }, c.label)))),
      h("tbody", {}, rows.map(r => h("tr", {}, cols.map(c => h("td", { class: (c.num ? "n " : "") + (c.cls || "") }, c.cell(r)))))));
  }
  // Panel: header (title, kind badge, rule tooltip, optional link) + body that renders a /v1/overview section.
  function panel(title, opts) {
    opts = opts || {};
    const meta = h("span", { class: "ov-pm" });
    const body = h("div", { class: "ov-pb" }, h("div", { class: "ov-ld" }, "…"));
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
        body.replaceChildren(...[items.length ? render(items) : h("div", { class: "ov-none" }, emptyText || "none right now"),
          sec.note ? h("div", { class: "ov-note" }, sec.note) : null].filter(Boolean));
      },
      fail(reason) { meta.replaceChildren(); body.replaceChildren(unavail(reason)); },
      body,
    };
  }
  const unavail = reason => h("div", { class: "ov-un" }, h("b", {}, "unavailable"), " ", reason || "no reason given");
  const gpu = r => OG.gpuLink(r.gpu || r.gpu_slug);
  const delta = (v, title) => v == null ? OG.na(title) : h("span", { class: "chg " + (v > 0 ? "up" : v < 0 ? "down" : "flat") }, (v > 0 ? "+" : v < 0 ? L.MINUS : "") + Math.abs(v));
  const shortIndex = n => String(n || "").replace(/^OpenGrid\s+/, "").replace(/\s+Index$/, "").replace(/\s*\(interruptible\)/, "");

  // ---- indices strip ----
  function indexCell(ix) {
    const usd = ix.unit === "usd_per_gpu_hour";
    const lvl = ix.level == null ? null : usd ? fmt.price(ix.level) : fmt.num(ix.level, 2);
    const ch = w => ix.changes && ix.changes[w] != null ? OG.chg(ix.changes[w]) : OG.na((ix.change_reasons || {})[w] || ix.reason || "unavailable");
    return h("a", { class: "ov-ix" + (ix.level == null ? " off" : ""), href: "/indices/" + encodeURIComponent(ix.id), title: ix.name + (ix.reason ? "\n" + ix.reason : "") + (ix.constituents != null ? `\n${ix.constituents} constituent(s)` : "") },
      h("div", { class: "ov-ix-n" }, shortIndex(ix.name)),
      h("div", { class: "ov-ix-v" }, lvl == null ? OG.na(ix.reason) : lvl, h("span", { class: "ov-ix-u" }, usd ? "/GPU·h" : "pts")),
      ix.level == null
        ? h("div", { class: "ov-ix-r" }, ix.reason || "not published")
        : h("div", { class: "ov-ix-c" }, h("span", { class: "dimmer" }, "24h"), ch("24h"), h("span", { class: "dimmer" }, "7d"), ch("7d")));
  }
  function drawIndices(box, list) {
    if (!Array.isArray(list)) { box.replaceChildren(h("div", { class: "ov-ix-un" }, unavail("index service did not answer (/v1/indices)"))); return; }
    const byId = new Map(list.map(x => [x.id, x]));
    const cells = BENCH.map(id => byId.get(id) ? indexCell(byId.get(id)) : null).filter(Boolean);
    if (!cells.length) { box.replaceChildren(h("div", { class: "ov-ix-un" }, unavail(list.length ? "no benchmark index has data yet" : "no index has data yet: the index job has not produced levels"))); return; }
    box.replaceChildren(...cells, h("a", { class: "ov-ix ov-ix-all", href: "/indices" }, `All ${list.length} indices →`));
  }

  // ---- direction summary + capacity ----
  function summaryLine(o) {
    const parts = [];
    const ch = (o.board.items || []).map(b => b.change_24h_median).filter(v => v != null);
    if (o.gainers && o.gainers.available === false) parts.push(h("span", {}, h("b", {}, "24h direction "), h("span", { class: "warn" }, "unavailable"), h("span", { class: "dim" }, ": " + o.gainers.reason)));
    else if (!ch.length) parts.push(h("span", {}, h("b", {}, "Direction "), h("span", { class: "dim" }, "no GPU has >= 2 providers priced at both times")));
    else {
      const up = ch.filter(v => v > 0.0005).length, dn = ch.filter(v => v < -0.0005).length, med = ch.slice().sort((a, b) => a - b)[Math.floor(ch.length / 2)];
      parts.push(h("span", {}, h("b", {}, "24h "), h("span", { class: "up" }, `${up} up`), " · ", h("span", { class: "down" }, `${dn} down`), " · ", `${ch.length - up - dn} flat`,
        h("span", { class: "dim" }, " of " + ch.length + " GPUs · median move "), OG.chg(med)));
    }
    const c = o.capacity || {};
    if (c.available !== false) {
      parts.push(h("span", {}, h("b", {}, "Markets "), fmt.num(c.markets_live), h("span", { class: "dim" }, " live · "), h("span", { class: c.markets_sold_out_now ? "warn" : "" }, fmt.num(c.markets_sold_out_now)), h("span", { class: "dim" }, " sold out · "),
        fmt.num(c.gpus_sold_out_everywhere), h("span", { class: "dim" }, " GPUs sold out everywhere")));
      parts.push(h("span", {}, h("b", {}, "Listings "), fmt.num(c.available_listings_now), h("span", { class: "dim" }, " available "),
        c.available_listings_change_24h == null ? h("span", { class: "dim", title: "needs 24h of history" }, "(24h n/a)") : h("span", {}, "(", delta(c.available_listings_change_24h), " 24h)")));
      parts.push(h("span", {}, h("b", {}, "24h "), fmt.num(c.sold_out_events_24h), h("span", { class: "dim" }, " sell-outs · "), fmt.num(c.returned_events_24h), h("span", { class: "dim" }, " restocks")));
    }
    return parts;
  }

  // ---- section renderers ----
  const R = {
    movers: items => mini([
      { label: "GPU", cell: gpu },
      { label: "Median", num: true, cell: r => fmt.price(r.median_now) },
      { label: "24h", num: true, cell: r => OG.chg(r.median_pct) },
      { label: "Prov", num: true, title: "providers priced at both times (one vote each)", cell: r => String(r.providers_matched) },
    ], items.slice(0, 6)),
    spreads: rows => mini([
      { label: "GPU", cell: gpu },
      { label: "Low", num: true, cell: r => fmt.price(r.low) },
      { label: "High", num: true, cell: r => fmt.price(r.high) },
      { label: "Spread", num: true, title: "high / low − 1", cell: r => h("span", { title: r.label || "" }, fmt.pct(r.spread_pct_of_low).replace(/^\+/, "")) },
      { label: "Prov", num: true, cell: r => String(r.providers) },
    ], rows.slice(0, 6)),
    volatile: items => mini([
      { label: "GPU", cell: gpu },
      { label: "σ 1h", num: true, title: "stdev of matched hourly median change, 7 days", cell: r => fmt.pct(r.hourly_stdev).replace(/^\+/, "") },
      { label: "7d range", num: true, cell: r => fmt.pct(r.range_7d).replace(/^\+/, "") },
    ], items.slice(0, 6)),
    liquid: items => mini([
      { label: "GPU", cell: gpu },
      { label: "Priced", num: true, title: "providers with purchasable listings", cell: r => String(r.providers_priced) },
      { label: "Avail", num: true, title: "providers with available listings", cell: r => String(r.providers_available) },
      { label: "Listings", num: true, title: "available / live listings", cell: r => h("span", {}, String(r.available_listings), h("span", { class: "dimmer" }, "/" + r.live_listings)) },
    ], items.slice(0, 6)),
    avail: items => mini([
      { label: "GPU", cell: gpu },
      { label: "Prov", num: true, title: "providers with purchasable listings, 24h ago → now", cell: r => h("span", {}, h("span", { class: "dim" }, r.providers_priced_then + "→"), String(r.providers_priced_now)) },
      { label: "Δ", num: true, cell: r => delta(r.delta_providers) },
      { label: "Listings", num: true, cell: r => h("span", {}, h("span", { class: "dim" }, r.available_listings_then + "→"), String(r.available_listings_now)) },
      { label: "Δ", num: true, cell: r => delta(r.delta_listings) },
    ], items.slice(0, 6)),
    newly: items => mini([
      { label: "GPU", cell: gpu },
      { label: "First at", cell: r => OG.providerLink(r.first_provider, { logo: false }) },
      { label: "Since", num: true, cell: r => fmt.dateTime(r.first_priced) },
      { label: "Low", num: true, cell: r => OG.value(r.lowest_now, fmt.price, "not priced now") },
    ], items.slice(0, 6)),
    sold: items => mini([
      { label: "GPU", cell: gpu },
      { label: "Providers", cls: "ov-wrap", cell: r => h("span", {}, (r.providers || []).map((p, i) => [i ? ", " : "", OG.providerLink(p, { logo: false })])) },
      { label: "Last low", num: true, cell: r => r.last_lowest == null ? h("span", { class: "dimmer", title: "never priced since tracking began" }, "never") : h("span", { title: "last priced " + fmt.dateTime(r.last_priced_at) }, fmt.price(r.last_lowest)) },
    ], items.slice(0, 6)),
    unusual: items => mini([
      { label: "GPU", cell: gpu },
      { label: "24h", num: true, cell: r => OG.chg(r.change_24h_median) },
      { label: "z", num: true, title: "vs the same statistic on each of the previous 30 days", cell: r => fmt.num(r.z, 1) },
      { label: "n", num: true, cell: r => String(r.samples) },
    ], items.slice(0, 6)),
    changes: items => mini([
      { label: "Time", cell: r => h("span", { class: "mono dim" }, fmt.time(r.changed_at).slice(0, 5)) },
      { label: "GPU", cell: gpu },
      { label: "Provider", cell: r => OG.providerLink(r.provider, { logo: false }) },
      { label: "Was", num: true, cell: r => h("span", { class: "dim" }, fmt.price(r.previous_price)) },
      { label: "Now", num: true, cell: r => fmt.price(r.price) },
      { label: "Chg", num: true, cell: r => OG.chg(r.pct) },
    ], items.slice(0, 10)),
  };

  // ---- market board ----
  function boardTable() {
    return OG.table({
      columns: [
        { key: "gpu", label: "GPU", fmt: v => h("b", {}, L.shortGpu(v)), value: r => L.shortGpu(r.gpu), href: r => "/gpu/" + r.gpu_slug },
        { key: "providers", label: "Prov", num: true, desc: true, title: "providers with purchasable listings now" },
        { key: "lowest", label: "Low", num: true, fmt: v => fmt.price(v), title: "lowest observed on-demand $/GPU-hr now" },
        { key: "lowest_provider", label: "Cheapest at", fmt: v => OG.providerLink(v) },
        { key: "median", label: "Median", num: true, fmt: v => fmt.price(v), title: "median of provider prices (one vote each)" },
        { key: "highest", label: "High", num: true, fmt: v => fmt.price(v) },
        { key: "spread", label: "Spread", num: true, desc: true, value: r => (r.providers > 1 && r.lowest ? r.highest / r.lowest - 1 : null), fmt: v => v == null ? "–" : fmt.pct(v).replace(/^\+/, ""), title: "high / low − 1" },
        { key: "change_24h_median", label: "24h", num: true, desc: true, title: "matched 24h change of the median: providers priced at both times only",
          fmt: (v, r) => OG.chg(v, { reason: r.providers_matched < 2 ? `needs >= 2 providers priced 24h ago and now (${r.providers_matched || 0} matched)` : "not enough history" }) },
        { key: "spark", label: "24h low", sort: false, csv: false, title: "lowest price over the last 24h (/market)", fmt: (v, r) => r.spark ? OG.charts.sparkline(r.spark, { dir: fmt.dir(r.spark_chg), width: 84, height: 16 }) : h("span", { class: "dimmer" }, "–") },
      ],
      rows: [], sort: { key: "providers", dir: "desc" }, compact: true, rowKey: r => r.gpu, rowHref: r => "/gpu/" + r.gpu_slug,
      title: "Market board · on-demand", csv: "opengrid-board.csv", empty: "No GPU is priced right now.",
    });
  }

  OG.page("/", {
    title: "Overview",
    nav: "overview",
    mount(el, params, query, ctx) {
      el.classList.add("pg-overview");
      const sub = h("span", {}, "On-demand GPU market · observed list prices per GPU-hour");
      const asof = h("span", { class: "mono dim ov-asof" });
      const ixBox = h("div", { class: "ov-ixs" }, h("div", { class: "ov-ld" }, "loading indices…"));
      const sumBox = h("div", { class: "ov-sum" }, h("span", { class: "dim" }, "loading…"));
      const P = {
        gainers: panel("Top gainers · 24h", { rule: "matched 24h change of the median provider price" }),
        losers: panel("Top losers · 24h", { rule: "matched 24h change of the median provider price" }),
        unusual: panel("Unusual moves"),
        volatile: panel("Most volatile · 7d"),
        spreads: panel("Widest spreads", { href: "/heatmaps", rule: "high / low − 1 across providers now (>= 2 providers)" }),
        liquid: panel("Most liquid / covered"),
        gain: panel("Availability gaining · 24h"),
        lose: panel("Availability losing · 24h"),
        newly: panel("Newly available · 7d"),
        sold: panel("Sold out everywhere"),
        changes: panel("Recent provider price changes", { cls: "span2", rule: "per-listing changes in 24h above the 0.5% / $0.001 noise floor" }),
      };
      const grid = h("div", { class: "ov-grid" }, Object.values(P).map(p => p.el));
      const board = boardTable();
      const evBox = h("div", { class: "ov-evs" }, h("div", { class: "ov-ld" }, "…"));
      const newsBox = h("div", { class: "ov-news" }, h("div", { class: "ov-ld" }, "…"));
      el.append(
        OG.head("Market overview", sub, asof, h("a", { class: "btn", href: "/methodology/events#overview" }, "Methodology")),
        ixBox, sumBox, grid,
        h("div", { class: "ov-low" },
          h("div", { class: "ov-board" }, board),
          h("div", { class: "ov-side" },
            h("section", { class: "ov-p" }, h("header", { class: "ov-ph" }, h("a", { class: "lnk ov-pt", href: "/events?sev=notable" }, "Latest notable events"), h("a", { class: "lnk ov-more", href: "/events" }, "all →")), evBox),
            h("section", { class: "ov-p" }, h("header", { class: "ov-ph" }, h("a", { class: "lnk ov-pt", href: "/news" }, "Headlines"), h("a", { class: "lnk ov-more", href: "/news" }, "all →")), newsBox))));

      let spark = new Map(), lastO = null;
      let lastBoard = [];
      function drawBoard(items) {
        lastBoard = items;
        board.update(items.map(b => { const s = spark.get(b.gpu); return Object.assign({}, b, s ? { spark: s.spark, spark_chg: s.change_pct } : {}); }));
      }

      function tape(o) {
        const items = [];
        for (const b of (o.board.items || []).filter(b => b.providers >= 2).slice(0, 14))
          items.push(h("a", { class: "tk", href: "/gpu/" + b.gpu_slug, title: `${b.gpu}: lowest on-demand $/GPU-hr across ${b.providers} providers; 24h = matched median change` },
            h("b", {}, L.shortGpu(b.gpu)), " ", fmt.price(b.lowest), " ", OG.chg(b.change_24h_median, { reason: o.gainers.reason || "not enough matched history" })));
        for (const t of ((o.tape && o.tape.items) || []).slice(0, 8))
          items.push(h("a", { class: "tk tk-ev", href: t.gpu_slug ? "/gpu/" + t.gpu_slug : "/events", title: t.title }, h("i", { class: "ev-sev s-" + t.severity }), " ", t.title.length > 60 ? t.title.slice(0, 58) + "…" : t.title));
        if (items.length) OG.status.tape(items);
      }

      async function loadOverview() {
        let r;
        try { r = await ctx.api("/v1/overview", { full: true, nocache: true }); }
        catch (e) {
          for (const p of Object.values(P)) p.fail("overview request failed: " + e.message);
          sumBox.replaceChildren(OG.error(e, loadOverview)); OG.status.live(false, "api unreachable"); return;
        }
        const o = r.data;
        if (r.meta) OG.status.asOf(r.meta.as_of);
        asof.textContent = o.as_of_hour ? `hour ${fmt.dateTime(o.as_of_hour)} · ${o.history_hours}h recorded since ${fmt.dateTime(o.tracking_since)}` : "no market history";
        lastO = o;
        sumBox.replaceChildren(...summaryLine(o).flatMap((n, i) => i ? [h("span", { class: "ov-sep" }, "│"), n] : [n]));
        P.gainers.set(o.gainers, R.movers, "no GPU's matched median rose in 24h");
        P.losers.set(o.losers, R.movers, "no GPU's matched median fell in 24h");
        P.unusual.set(o.unusual, R.unusual, "no 24h move beyond |z| >= 2");
        P.volatile.set(o.volatile, R.volatile);
        P.liquid.set(o.liquid, R.liquid);
        P.gain.set(o.availability_gaining, R.avail, "no GPU gained availability vs 24h ago");
        P.lose.set(o.availability_losing, R.avail, "no GPU lost availability vs 24h ago");
        P.newly.set(o.newly_available, R.newly, "no GPU first went on sale in the last 7 days");
        P.sold.set(o.sold_out, R.sold, "every GPU with live listings is purchasable somewhere");
        P.changes.set(o.price_changes, R.changes, "no listing changed price in 24h");
        if (o.board.available === false) board.update([]);
        else drawBoard(o.board.items || []);
        tape(o);
      }
      async function loadSpreads() {
        const rows = await ctx.api("/v1/spreads", { params: { min_providers: 2 }, nocache: true }).catch(e => ({ error: e }));
        if (!Array.isArray(rows)) { P.spreads.fail("spreads request failed: " + (rows.error && rows.error.message)); return; }
        const list = rows.filter(r => r.spread_pct_of_low != null).sort((a, b) => b.spread_pct_of_low - a.spread_pct_of_low);
        P.spreads.set({ available: true, kind: "inferred", items: list }, R.spreads, "no GPU has 2+ priced providers");
      }
      async function loadEvents() {
        try {
          const evs = await ctx.api("/v1/events", { params: { min_severity: "notable", limit: 10 }, nocache: true });
          evBox.replaceChildren(evs.length ? h("div", { class: "ov-evl" }, evs.map(e => OG.views.eventRow(e, { compact: true })))
            : h("div", { class: "ov-none" }, "No notable or major events recorded yet."));
        } catch (e) { evBox.replaceChildren(unavail("events request failed: " + e.message)); }
      }
      async function loadNews() {
        const n = await OG.api.soft("/v1/news", { params: { limit: 8 } });
        if (!ctx.alive()) return;
        if (n == null) { newsBox.replaceChildren(unavail("news service did not answer (/v1/news)")); return; }
        if (!n.length) { newsBox.replaceChildren(h("div", { class: "ov-none" }, "No headlines ingested yet.")); return; }
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
        if (lastO) tape(lastO);   // after the shell's own /market tape fallback, which shares this request
      }
      const all = () => { loadOverview(); loadIndices(); loadSpreads(); loadEvents(); loadNews(); loadSpark(); };
      all();
      ctx.every(60000, all);
    },
  });
})();
