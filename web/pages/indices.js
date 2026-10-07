/* /indices — the OpenGrid index board; /indices/:id — one index.
   Sources: /v1/indices (level + changes for every index with data in 90 days), /v1/indices/{id}
   (definition, level, changes with reasons, high/low, volatility, coverage, constituents),
   /v1/indices/{id}/history (stored levels; unpublished hours carry their reason).
   Board rows lazily fetch /v1/indices/{id} (high/low/vol) and 7d history (sparkline) — the list
   endpoint does not carry them. Nothing is computed here except constituent weights in the
   statistic, which follow the stated method exactly (median / 20% trimmed mean; methodology/indices.md). */
(() => {
  const { h, fmt } = OG;
  const FAMILIES = [
    ["composite", "Composites", "Base-100 composites: the overall GPU Compute Index and class indices. Not the price of any single SKU."],
    ["gpu", "Per-GPU · on-demand", "One canonical GPU, on-demand, USD per GPU-hour. One vote per provider, chain-linked."],
    ["spot", "Spot · interruptible", "Spot / interruptible capacity: can be reclaimed by the provider."],
    ["regional", "Regional", "On-demand, listings whose region maps to one region group."],
    ["provider_class", "Provider class", "On-demand, providers of one class (hyperscaler, neocloud, marketplace…)."],
  ];
  const FAM_LABEL = Object.fromEntries(FAMILIES.map(f => [f[0], f[1]]));
  const WINDOWS = [["24h", "24H"], ["7d", "7D"], ["30d", "30D"], ["90d", "90D"], ["ytd", "YTD"], ["all", "ALL"]];
  const CHG = [["24h", "24h"], ["7d", "7d"], ["30d", "30d"], ["90d", "90d"], ["ytd", "YTD"], ["all", "All"]];
  const levelFmt = (v, unit) => (v == null ? "–" : unit === "points" ? fmt.num(v, 2) : fmt.price(v));
  const unitLabel = u => (u === "points" ? "points (base 100)" : "USD / GPU-hour");
  const volFmt = v => (v == null ? "–" : (v * 100).toFixed(1) + "%");
  const STATUS_TXT = { included: "included", excluded_feed_down: "excluded: feed down", excluded_outlier: "excluded: outlier (> 3× from median)", unpublished: "unpublished this hour", no_data: "no data" };

  // Short form of an API reason for tight cells; the full reason always stays in the tooltip.
  function shortReason(r) {
    if (!r) return "n/a";
    let m;
    if (/^history does not cover/.test(r)) return "history shorter than window";
    if ((m = /insufficient history: (\d+) hourly returns, (\d+) required/.exec(r))) return `${m[1]}/${m[2]} hourly returns`;
    if ((m = /insufficient coverage: (\d+) eligible provider\(s\), (\d+) required/.exec(r))) return `${m[1]} of ${m[2]} providers needed`;
    if ((m = /insufficient coverage: (\d+%) of component weight published, (\d+%) required/.exec(r))) return `${m[1]} of weight (needs ${m[2]})`;
    if ((m = /insufficient coverage: (\d+) published component/.exec(r))) return `${m[1]} component published`;
    if ((m = /insufficient coverage: (\d+) component\(s\) \/ (\d+%) of component weight published; (\d+) and (\d+%) required/.exec(r))) return `${m[1]} comp. / ${m[2]} weight (needs ${m[3]} / ${m[4]})`;
    if (/rebased/.test(r)) return "chain rebased in window";
    if (/not published within/.test(r)) return "unpublished at window start";
    if (/^index not published now/.test(r)) return "not published now";
    if ((m = /^no eligible constituents at .*\(last data (\S+ \S+)\)/.exec(r))) return "no constituents since " + m[1];
    if (/^no eligible constituents at/.test(r)) return "no constituents this hour";
    if (/regions\.py/.test(r)) return "region grouping unavailable";
    return r.length > 44 ? r.slice(0, 42) + "…" : r;
  }

  /* lazy per-index extras for the board: detail (high/low/vol) + 7d hourly levels, cached 5 min */
  const extras = new Map();
  function extra(id) {
    const hit = extras.get(id);
    if (hit && Date.now() - hit.t < 300000) return hit.p;
    const p = Promise.all([OG.api.soft("/v1/indices/" + encodeURIComponent(id)), OG.api.soft(`/v1/indices/${encodeURIComponent(id)}/history`, { params: { window: "7d", resolution: "1h" } })])
      .then(([d, hst]) => ({ detail: d, spark: hst && hst.points ? hst.points.map(x => (x.published ? x.level : null)) : null }));
    extras.set(id, { t: Date.now(), p });
    return p;
  }
  // run fns with at most n in flight
  async function pool(items, n, fn, alive) {
    let i = 0;
    const next = async () => { while (i < items.length && alive()) { const it = items[i++]; await fn(it); } };
    await Promise.all(Array.from({ length: Math.min(n, items.length) }, next));
  }

  function statusCell(r) {
    if (r.published) return h("span", { class: "ix-pub", title: r.hour ? "published for " + fmt.dateTime(r.hour) : "" }, h("i"), "published");
    return h("span", { class: "ix-unpub", title: r.reason || "not published" }, h("i"), "not published · ", h("span", { class: "dim" }, shortReason(r.reason)));
  }
  function levelCell(r) {
    if (r.published) return h("b", {}, levelFmt(r.level, r.unit));
    if (r.last_published) return h("span", { class: "dimmer", title: `not published now; last published ${levelFmt(r.last_published.level, r.unit)} at ${fmt.dateTime(r.last_published.hour)}` }, levelFmt(r.last_published.level, r.unit) + "*");
    return OG.na(r.reason);
  }

  /* ================= board ================= */
  OG.page("/indices", {
    title: "Indices",
    async mount(el, params, query, ctx) {
      const root = h("div", { class: "ix" });
      el.append(root);
      const st = { fam: OG.qs.get("family", "all"), pub: OG.qs.get("status", "all"), q: OG.qs.get("q", ""), rows: null, meta: null };
      const famSeg = OG.seg([["all", "All"], ...FAMILIES.map(f => [f[0], f[1].split(" ·")[0]])], st.fam, v => { st.fam = v; OG.qs.set({ family: v === "all" ? null : v }); draw(); });
      const pubSeg = OG.seg([["all", "All"], ["published", "Published"], ["unpublished", "Unpublished"]], st.pub, v => { st.pub = v; OG.qs.set({ status: v === "all" ? null : v }); draw(); });
      const search = h("input", { class: "field ix-q", type: "search", placeholder: "filter: h100, us, spot…", value: st.q, "aria-label": "Filter indices",
        oninput: e => { st.q = e.target.value; OG.qs.set({ q: st.q || null }); draw(); } });
      const statsEl = h("div", {}, OG.stats([{ label: "GPU Compute Index", value: null, reason: "loading" }]));
      const heroEl = h("div", { class: "ix-hero" });
      const body = h("div", {}, OG.loading("Loading indices…"));
      const noteEl = h("p", { class: "note" });
      root.append(OG.head("Indices", "Every OpenGrid index: chain-linked, one vote per provider, published only with enough constituents",
        h("span", { class: "flt" }, h("span", {}, "Family"), famSeg), h("span", { class: "flt" }, h("span", {}, "Status"), pubSeg), search,
        h("a", { class: "btn", href: "/methodology/indices" }, "Methodology")),
        statsEl, heroEl, body, noteEl);

      const tables = new Map();
      async function load() {
        let res;
        try { res = await ctx.api("/v1/indices", { full: true, slot: "ix-list" }); }
        catch (e) { if (!e.stale) body.replaceChildren(OG.error(e, load)); return; }
        st.rows = res.data.map(r => Object.assign({}, r, (st.rows || []).find(o => o.id === r.id) && { _x: (st.rows || []).find(o => o.id === r.id)._x }));
        st.meta = res.meta;
        OG.status.asOf(res.meta && res.meta.as_of);
        tables.clear();
        draw();
        fillExtras();
      }

      let redrawT = 0;
      const soon = () => { clearTimeout(redrawT); redrawT = setTimeout(() => { if (ctx.alive()) { for (const t of tables.values()) t.update(t._rows()); drawHero(); } }, 200); };
      ctx.onCleanup(() => clearTimeout(redrawT));
      async function fillExtras() {
        // /v1/indices now carries the board columns: use them, and fetch per index only for old servers.
        if (st.rows.length && "spark_7d" in st.rows[0]) {
          for (const r of st.rows) {
            const v = r.volatility || {};
            r._x = { spark: r.spark_7d, high: r.high, low: r.low, vol30: v["30d"], vol7: v["7d"], cov: r.coverage };
          }
          soon();
          return;
        }
        // composites + published first: they matter most at a glance
        const order = st.rows.slice().sort((a, b) => (b.kind === "composite") - (a.kind === "composite") || b.published - a.published);
        await pool(order, 6, async r => {
          const x = await extra(r.id);
          if (!ctx.alive()) return;
          const d = x.detail;
          r._x = { spark: x.spark, high: d && d.high, low: d && d.low, vol30: d && d.volatility && d.volatility["30d"], vol7: d && d.volatility && d.volatility["7d"], cov: d && d.coverage };
          soon();
        }, ctx.alive);
      }

      function visible() {
        const q = st.q.trim().toLowerCase();
        return st.rows.filter(r => (st.fam === "all" || r.kind === st.fam) && (st.pub === "all" || (st.pub === "published") === !!r.published) &&
          (!q || (r.id + " " + r.name + " " + (r.gpu || "") + " " + (r.region_group || "") + " " + (r.provider_class || "")).toLowerCase().includes(q)));
      }

      function drawStats() {
        const all = st.rows, by = id => all.find(r => r.id === id);
        const gc = by("gpu-compute"), pub = all.filter(r => r.published).length;
        const latest = all.map(r => r.hour).filter(Boolean).sort().pop();
        statsEl.replaceChildren(OG.stats([
          gc ? { label: "GPU Compute Index", value: gc.published ? fmt.num(gc.level, 2) : null, reason: gc.reason, change: gc.published ? gc.changes["24h"] : undefined, sub: gc.published ? "24h" + (gc.changes["24h"] == null ? ": " + shortReason(gc.change_reasons["24h"]) : " change") : "not published", kind: "observed", title: gc.change_reasons && gc.change_reasons["24h"] }
            : { label: "GPU Compute Index", value: null, reason: "no data for the composite yet" },
          gc && gc.published ? { label: "7d", value: h("span", {}, OG.chg(gc.changes["7d"], { reason: gc.change_reasons["7d"] })), title: gc.change_reasons["7d"] || null, sub: gc.change_reasons["7d"] ? "insufficient history" : "GPU Compute" } : null,
          { label: "Indices", value: String(all.length), sub: `${pub} published · ${all.length - pub} not` },
          ...FAMILIES.map(([k, l]) => { const rs = all.filter(r => r.kind === k); return rs.length ? { label: l.split(" ·")[0], value: `${rs.filter(r => r.published).length}/${rs.length}`, sub: "published" } : null; }),
          { label: "Index hour", value: latest ? fmt.time(latest).slice(0, 5) : null, reason: "no index hour stored", sub: latest ? fmt.date(latest) + " · v" + ((st.meta && st.meta.methodology_version) || "?") : null },
        ]));
      }

      function drawHero() {
        const comps = st.rows.filter(r => r.kind === "composite");
        if (!comps.length) { heroEl.replaceChildren(); return; }
        heroEl.replaceChildren(...comps.map(r => {
          const sp = r._x && r._x.spark;
          return h("a", { class: "ix-card" + (r.published ? "" : " off") + (r.id === "gpu-compute" ? " main" : ""), href: "/indices/" + encodeURIComponent(r.id), title: r.published ? r.name : "not published: " + r.reason },
            h("div", { class: "ix-card-h" }, h("span", { class: "ix-card-n" }, r.name.replace(/^OpenGrid /, "")), h("span", { class: "mono dimmer" }, r.id)),
            h("div", { class: "ix-card-v" }, r.published ? h("b", {}, fmt.num(r.level, 2)) : h("span", { class: "dimmer" }, "n/a"),
              r.published ? OG.chg(r.changes["24h"], { reason: r.change_reasons["24h"] }) : null, h("span", { class: "spacer" }),
              sp ? OG.charts.sparkline(sp, { width: 110, height: 26, dir: fmt.dir(r.changes["7d"]) }) : null),
            h("div", { class: "ix-card-s", title: r.reason || null }, r.published ? `${r.constituents} component${r.constituents === 1 ? "" : "s"} · 7d ` : "not published · " + shortReason(r.reason), r.published ? OG.chg(r.changes["7d"], { reason: r.change_reasons["7d"] }) : null));
        }));
      }

      function columns(fam) {
        const unit = fam === "composite" ? "points" : "usd";
        return [
          { key: "name", label: "Index", width: "250px", fmt: (v, r) => h("span", { class: "ix-name" }, h("a", { class: "lnk", href: "/indices/" + encodeURIComponent(r.id) }, v.replace(/^OpenGrid /, "")), h("span", { class: "ix-id" }, r.id)) },
          { key: "level", label: unit === "points" ? "Level" : "$/GPU·h", num: true, desc: true, value: r => (r.published ? r.level : null), csv: r => r.level, fmt: (v, r) => levelCell(r) },
          ...CHG.map(([w, l]) => ({ key: "c_" + w, label: l, num: true, desc: true, value: r => r.changes[w], csv: r => r.changes[w],
            fmt: (v, r) => OG.chg(r.changes[w], { reason: r.change_reasons[w] }) })),
          { key: "high", label: "High", num: true, value: r => r._x && r._x.high ? r._x.high.level : null, title: "Highest published level since the current chain segment began",
            fmt: (v, r) => !r._x ? h("span", { class: "dimmer" }, "·") : r._x.high ? h("span", { title: "at " + fmt.dateTime(r._x.high.hour) + " · since " + fmt.dateTime(r._x.high.since) }, levelFmt(r._x.high.level, r.unit)) : OG.na("never published") },
          { key: "low", label: "Low", num: true, value: r => r._x && r._x.low ? r._x.low.level : null,
            fmt: (v, r) => !r._x ? h("span", { class: "dimmer" }, "·") : r._x.low ? h("span", { title: "at " + fmt.dateTime(r._x.low.hour) }, levelFmt(r._x.low.level, r.unit)) : OG.na("never published") },
          { key: "vol", label: "Vol 30d", num: true, desc: true, value: r => r._x && r._x.vol30 ? r._x.vol30.annualized : null, title: "Annualized realized volatility of hourly log returns, 30 days",
            fmt: (v, r) => !r._x ? h("span", { class: "dimmer" }, "·") : OG.value(v, volFmt, r._x.vol30 ? r._x.vol30.reason : "unavailable") },
          { key: "constituents", label: fam === "composite" ? "Comp." : "Prov.", num: true, desc: true, title: fam === "composite" ? "Published components this hour" : "Included providers this hour (3 needed)",
            fmt: (v, r) => h("span", { class: r.published ? "" : "ix-low" }, String(v)) },
          { key: "spark", label: "7d", sort: false, csv: false, fmt: (v, r) => r._x && r._x.spark ? OG.charts.sparkline(r._x.spark, { width: 84, height: 18, dir: fmt.dir(r.changes["7d"]) }) : h("span", { class: "dimmer" }, r._x ? "–" : "·") },
          { key: "published", label: "Status", value: r => (r.published ? 1 : 0), desc: true, csv: r => (r.published ? "published" : "not published: " + r.reason), cls: "ix-st", fmt: (v, r) => statusCell(r) },
        ];
      }

      function draw() {
        if (!st.rows) return;
        drawStats(); drawHero();
        const rows = visible();
        const fams = FAMILIES.filter(([k]) => st.fam === "all" || st.fam === k);
        const parts = [];
        for (const [k, label, desc] of fams) {
          const rs = rows.filter(r => r.kind === k);
          if (!rs.length) continue;
          let t = tables.get(k);
          if (!t) {
            t = OG.table({ columns: columns(k), rows: rs, compact: true, rowKey: r => r.id, rowHref: r => "/indices/" + encodeURIComponent(r.id),
              rowClass: r => (r.published ? "" : "out"), csv: `opengrid-indices-${k}.csv`, limit: 200,
              title: `${label} · ${rs.filter(r => r.published).length}/${rs.length} published` });
            tables.set(k, t);
          }
          const cur = rs;
          t._rows = () => visible().filter(r => r.kind === k);
          t.update(cur);
          const tt = t.querySelector(".tbl-title"); if (tt) tt.textContent = `${label} · ${rs.filter(r => r.published).length}/${rs.length} published`;
          parts.push(h("section", { class: "sec ix-fam" }, h("div", { class: "ix-fam-d" }, desc), t));
        }
        body.replaceChildren(...(parts.length ? parts : [OG.empty(st.rows.length ? "No index matches these filters." : "No index has data yet: indices compute hourly from the market rollup once listings are recorded.")]));
        noteEl.replaceChildren(OG.kindBadge("observed"), " observed market prices (list prices as normalized), never quotes or execution prices. ",
          "Changes are measured from the current published level; a change is n/a (hover) when history does not cover the window or the chain was rebased. ",
          "* = last published level, shown dim when the index is not published now. ",
          h("a", { class: "lnk", href: "/methodology/indices" }, "How indices are computed →"));
      }

      load();
      ctx.every(300000, load);
    },
  });

  /* ================= detail ================= */
  OG.page("/indices/:id", {
    title: p => "Index " + p.id,
    nav: "indices",
    async mount(el, params, query, ctx) {
      const id = params.id;
      const root = h("div", { class: "ix ix-d" });
      el.append(root);
      root.append(OG.loading("Loading index…"));
      let d;
      try { d = await ctx.api("/v1/indices/" + encodeURIComponent(id)); }
      catch (e) {
        root.replaceChildren(OG.head("Index " + id, null), OG.error(e), h("p", {}, h("a", { class: "lnk", href: "/indices" }, "All indices →")));
        return;
      }
      ctx.setTitle(d.name);
      const def = d.definition || {};
      const st = { win: OG.qs.get("window", "30d"), res: OG.qs.get("res", "auto"), raw: OG.qs.get("raw") === "1", pts: null };
      if (!WINDOWS.some(w => w[0] === st.win)) st.win = "30d";
      const isPts = d.unit === "points";
      const yf = v => levelFmt(v, d.unit);

      const winSeg = OG.seg(WINDOWS, st.win, v => { st.win = v; OG.qs.set({ window: v === "30d" ? null : v }); loadHistory(); });
      const resSeg = OG.seg([["auto", "AUTO"], ["1h", "1H"], ["1d", "1D"]], st.res, v => { st.res = v; OG.qs.set({ res: v === "auto" ? null : v }); loadHistory(); });
      const rawBtn = h("button", { class: "chip", "aria-pressed": String(st.raw), title: "Overlay the unchained cross-sectional statistic (moves when providers join/leave)", onclick: () => { st.raw = !st.raw; rawBtn.setAttribute("aria-pressed", String(st.raw)); OG.qs.set({ raw: st.raw ? "1" : null }); drawChart(); } }, "raw cross-section");
      const gpuSlug = d.gpu ? OG.slug(d.gpu) : null;
      const chartEl = h("div", { class: "box box-p ix-chart" });
      const stripEl = h("div", { class: "ix-strip" });
      const hoverEl = h("div", { class: "ix-hover mono" }, "hover the chart for each hour's publication status");
      const chartNote = h("p", { class: "note" });
      const consEl = h("div", {});
      const defEl = h("div", { class: "box box-p ix-def" });
      const relEl = h("div", { class: "box box-p ix-rel" });

      root.replaceChildren(...[
        OG.head(h("span", { class: "ix-title" }, h("span", { class: "eyebrow" }, FAM_LABEL[d.kind] || d.kind), d.name, h("span", { class: "ix-id" }, d.id)),
          d.interruptible ? "Interruptible capacity: spot instances can be reclaimed by the provider." : def.description,
          h("span", { class: "flt" }, h("span", {}, "Window"), winSeg), h("span", { class: "flt" }, h("span", {}, "Res"), resSeg), d.kind !== "composite" ? rawBtn : null,
          gpuSlug ? h("a", { class: "btn", href: "/gpu/" + gpuSlug }, OG.shortGpu(d.gpu) + " market →") : null,
          h("a", { class: "btn", href: "/methodology/indices" }, "Methodology")),
        d.published ? null : OG.insufficient(d.reason + (d.last_published ? ` · last published ${yf(d.last_published.level)} at ${fmt.dateTime(d.last_published.hour)}` : ""), "Not published"),
        statsStrip(),
        h("div", { class: "cols ix-cols" },
          h("div", {}, chartEl, stripEl, hoverEl, chartNote),
          h("div", {}, OG.section("Definition", defEl), OG.section("Related", relEl))),
        OG.section(d.kind === "composite" ? "Components now" : "Constituents now", consEl)].filter(Boolean));

      function statsStrip() {
        const c = d.changes || {}, v = d.volatility || {}, cov = d.coverage || {};
        const ch = (w, l) => ({ label: l, value: c[w] && c[w].pct != null ? OG.chg(c[w].pct) : null, reason: c[w] ? c[w].reason : "n/a",
          sub: c[w] && c[w].pct != null ? "from " + yf(c[w].from_level) : shortReason(c[w] && c[w].reason), title: c[w] && c[w].pct != null ? "from " + fmt.dateTime(c[w].from_hour) + (c[w].note ? " · " + c[w].note : "") : c[w] && c[w].reason });
        return OG.stats([
          { label: "Level", value: d.published ? yf(d.level) : null, reason: d.reason, sub: d.hour ? (isPts ? "points · " : "$/GPU·h · ") + fmt.dateTime(d.hour) : null, kind: "observed" },
          ch("24h", "24h"), ch("7d", "7d"), ch("30d", "30d"), ch("90d", "90d"), ch("ytd", "YTD"), ch("all", "All-time"),
          { label: "High", value: d.high ? yf(d.high.level) : null, reason: "never published", sub: d.high ? fmt.dateTime(d.high.hour) : null, title: d.high ? "since chain segment start " + fmt.dateTime(d.high.since) : null },
          { label: "Low", value: d.low ? yf(d.low.level) : null, reason: "never published", sub: d.low ? fmt.dateTime(d.low.hour) : null },
          { label: "Vol 7d", value: v["7d"] && v["7d"].annualized != null ? volFmt(v["7d"].annualized) : null, reason: v["7d"] && v["7d"].reason, sub: v["7d"] && v["7d"].annualized != null ? "annualized" : shortReason(v["7d"] && v["7d"].reason), title: v["7d"] && v["7d"].reason },
          { label: "Vol 30d", value: v["30d"] && v["30d"].annualized != null ? volFmt(v["30d"].annualized) : null, reason: v["30d"] && v["30d"].reason, sub: v["30d"] && v["30d"].annualized != null ? "annualized" : shortReason(v["30d"] && v["30d"].reason), title: v["30d"] && v["30d"].reason },
          { label: isPts ? "Components" : "Constituents", value: String(d.constituents ?? 0), sub: d.method ? d.method.replace(/_/g, " ") : null },
          { label: "Coverage 30d", value: cov.hours_30d ? Math.round(100 * (cov.published_hours_30d || 0) / cov.hours_30d) + "%" : null, reason: "no coverage data", sub: cov.hours_30d ? `${cov.published_hours_30d}/${cov.hours_30d} h published` : null, title: cov.first_published ? "first published " + fmt.dateTime(cov.first_published) + " · chain segment " + cov.chain_segment + " since " + fmt.dateTime(cov.chain_segment_start) : null },
        ].filter(Boolean));
      }

      /* ---- history ---- */
      let chart = null;
      async function loadHistory() {
        chartEl.replaceChildren(OG.loading("Loading history…")); chart = null;
        let hst;
        try { hst = await ctx.api(`/v1/indices/${encodeURIComponent(id)}/history`, { params: { window: st.win, resolution: st.res === "auto" ? null : st.res }, slot: "ix-hist" }); }
        catch (e) { if (!e.stale) chartEl.replaceChildren(OG.error(e, loadHistory)); return; }
        st.resolved = hst.resolution;
        st.pts = fillGrid(hst.points || [], hst.resolution);
        drawChart();
      }
      // Stored rows only exist for hours with any data: fill the grid so missing hours are gaps, not joined lines.
      function fillGrid(points, res) {
        if (points.length < 2) return points;
        const step = res === "1d" ? 864e5 : 36e5, out = [], by = new Map(points.map(p => [+new Date(p.t), p]));
        const t0 = +new Date(points[0].t), t1 = +new Date(points[points.length - 1].t);
        if ((t1 - t0) / step > 20000) return points;
        for (let t = t0; t <= t1; t += step) out.push(by.get(t) || { t: new Date(t).toISOString(), published: false, level: null, reason: "no row stored: no eligible listing recorded", _missing: true });
        return out;
      }
      function drawChart() {
        const pts = st.pts || [];
        if (!pts.some(p => p.published)) {
          chartEl.replaceChildren(OG.insufficient(pts.length ? `none of the ${pts.length} stored ${st.resolved === "1d" ? "days" : "hours"} in this window is published` : "no stored level in this window", "No published level"));
          stripEl.replaceChildren(); drawStrip(pts); chart = null; return;
        }
        const times = pts.map(p => p.t), lv = pts.map(p => (p.published ? p.level : null));
        const series = [{ key: "level", label: d.name, values: lv, color: "#4d94ff", width: 1.75, strong: true }];
        if (st.raw && d.kind !== "composite") series.push({ key: "raw", label: "raw cross-section (unchained)", values: pts.map(p => (p.published ? p.raw_level : null)), color: "#7d8895", dash: "3 3", width: 1 });
        const events = [];
        for (let i = 1; i < pts.length; i++) {
          const a = pts[i - 1], b = pts[i];
          if (b.published && b.segment_no != null && a.segment_no != null && b.segment_no !== a.segment_no) events.push({ t: b.t, label: "Chain rebased", kind: "rebase", severity: "notable", detail: b.reason || "levels before this are not comparable" });
        }
        const opts = { times, series, height: 300, yFmt: yf, events, legend: series.length > 1, label: "Index level history", emptyText: "no published level",
          gapReason: i => { const p = pts[i]; return !p || p.published ? null : "not published: " + (p.reason || (st.resolved === "1d" ? "no published hour this day" : "no reason stored")); },
          onHover: i => {
            if (i == null) { hoverEl.replaceChildren("hover the chart for each hour's publication status"); return; }
            const p = pts[i];
            hoverEl.replaceChildren(h("span", { class: "dim" }, fmt.dateTime(p.t) + (st.resolved === "1d" ? " (day)" : "") + "  "),
              p.published ? [h("span", { class: "ix-pub" }, h("i"), "published "), h("b", {}, yf(p.level)),
                st.resolved === "1d" ? h("span", { class: "dim" }, `  ${p.published_hours}/24 h published · range ${yf(p.low)}–${yf(p.high)}`) : h("span", { class: "dim" }, `  ${p.constituents} constituent${p.constituents === 1 ? "" : "s"}${p.link_constituents != null ? ", " + p.link_constituents + " linked" : ""}${p.reason ? " · " + p.reason : ""}`)]
                : [h("span", { class: "ix-unpub" }, h("i"), "not published: "), h("span", {}, p.reason || (st.resolved === "1d" ? "no published hour this day" : "no reason stored"))]);
          } };
        chartEl.textContent = "";
        if (chart) chart.destroy();
        chart = OG.charts.timeseries(chartEl, opts);
        ctx.onCleanup(() => chart && chart.destroy());
        drawStrip(pts);
        const gaps = pts.filter(p => !p.published).length;
        const isolated = lv.filter((v, i) => v != null && lv[i - 1] == null && lv[i + 1] == null).length;
        chartNote.replaceChildren(OG.kindBadge("observed"), ` Chain-linked level, ${st.resolved === "1d" ? "daily close (last published hour)" : "hourly"}. Each hour's move is measured only on constituents present in both hours, so a provider joining or leaving cannot move it. `,
          gaps ? `${gaps} unpublished ${st.resolved === "1d" ? "day" : "hour"}${gaps === 1 ? "" : "s"} in this window are gaps in the line and red in the strip below (hover for the reason). ` : "Published for every stored hour in this window. ",
          events.length ? `${events.length} chain rebase${events.length === 1 ? "" : "s"} marked. ` : "",
          isolated ? `${isolated} published ${st.resolved === "1d" ? "day" : "hour"}${isolated === 1 ? " stands" : "s stand"} alone between gaps, so ${isolated === 1 ? "it has" : "they have"} no line segment; hover the chart to read ${isolated === 1 ? "it" : "them"}.` : "");
      }
      // Publication strip under the chart: runs of published / unpublished periods, aligned to the plot area.
      function drawStrip(pts) {
        if (!pts.length) { stripEl.replaceChildren(); return; }
        const runs = [];
        for (const p of pts) {
          const key = p.published ? "pub" : "gap:" + (p.reason || "");
          const last = runs[runs.length - 1];
          if (last && last.key === key) { last.n++; last.t1 = p.t; } else runs.push({ key, n: 1, t0: p.t, t1: p.t, pub: p.published, reason: p.reason });
        }
        const total = pts.length;
        stripEl.replaceChildren(h("div", { class: "ix-strip-bar" }, runs.map(r => h("span", { class: r.pub ? "on" : "off", style: `flex:${r.n} 0 0`,
          title: `${fmt.dateTime(r.t0)} → ${fmt.dateTime(r.t1)} · ${r.n} ${st.resolved === "1d" ? "day" : "hour"}${r.n === 1 ? "" : "s"}\n${r.pub ? "published" : "not published: " + (r.reason || "no reason stored")}` }))),
        h("div", { class: "ix-strip-l" }, h("span", {}, h("i", { class: "on" }), "published"), h("span", {}, h("i", { class: "off" }), "not published"),
          h("span", { class: "dim" }, `${total - runs.filter(r => !r.pub).reduce((a, r) => a + r.n, 0)}/${total} ${st.resolved === "1d" ? "days" : "hours"} published`)));
      }

      /* ---- constituents ---- */
      function weights(items, method) {
        const inc = items.filter(c => c.status === "included").sort((a, b) => a.price - b.price);
        const w = new Map(), n = inc.length;
        if (!n) return w;
        if (method === "trimmed_mean_20") {
          const k = Math.floor(n * 0.2);
          inc.forEach((c, i) => w.set(c.provider, i < k || i >= n - k ? { w: 0, note: "trimmed (20% each end)" } : { w: 1 / (n - 2 * k) }));
        } else {
          const mid = [Math.floor((n - 1) / 2), Math.ceil((n - 1) / 2)];
          inc.forEach((c, i) => w.set(c.provider, mid.includes(i) ? { w: mid[0] === mid[1] ? 1 : 0.5, note: "median" } : { w: 0, note: "not the median" }));
        }
        return w;
      }
      function drawConstituents() {
        const cn = d.constituents_now || { constituents: [] };
        const items = cn.constituents || [];
        if (!items.length) { consEl.replaceChildren(OG.empty("No constituent recorded for the latest stored hour.")); return; }
        const when = cn.hour ? `as of ${fmt.dateTime(cn.hour)}` : "";
        if (d.kind === "composite") {
          consEl.replaceChildren(OG.table({
            rows: items, compact: true, rowKey: r => r.index_id, rowHref: r => "/indices/" + encodeURIComponent(r.index_id), csv: `opengrid-${id}-components.csv`,
            title: `${items.filter(c => c.status === "included").length}/${items.length} components published · ${when}`, rowClass: r => (r.status === "included" ? "" : "out"), sort: { key: "weight", dir: "desc" },
            columns: [
              { key: "name", label: "Component index", value: r => r.name || r.index_id, fmt: (v, r) => h("span", { class: "ix-name" }, h("a", { class: "lnk", href: "/indices/" + encodeURIComponent(r.index_id) }, String(v).replace(/^OpenGrid /, "")), h("span", { class: "ix-id" }, r.index_id)) },
              { key: "weight", label: "Weight", num: true, desc: true, fmt: v => fmt.pct(v).replace(/^\+/, "") },
              { key: "level", label: "Level $/GPU·h", num: true, fmt: v => (v == null ? "–" : fmt.price(v)) },
              { key: "status", label: "Status", fmt: (v, r) => h("span", { class: v === "included" ? "ix-pub" : "ix-unpub" }, h("i"), STATUS_TXT[v] || v, r.linked === false ? h("span", { class: "dim" }, " · not linked this hour (rebased child)") : null) },
              { key: "segment_no", label: "Chain seg.", num: true, cls: "dim" },
            ] }),
          h("p", { class: "note" }, "Weights are the definition's equal weights. The composite moves by the weighted mean of its published components' hourly relatives; it is published only when ≥ 50% of weight is published."));
          return;
        }
        const wt = weights(items, d.method);
        const rows = items.map(c => Object.assign({}, c, { _w: wt.get(c.provider) }));
        consEl.replaceChildren(OG.table({
          rows, compact: true, rowKey: r => r.provider, csv: `opengrid-${id}-constituents.csv`, sort: { key: "price", dir: "asc" },
          title: `${items.filter(c => c.status === "included").length}/${items.length} providers included · ${when} · statistic: ${(d.method || "–").replace(/_/g, " ")}`,
          rowClass: r => (r.status === "included" ? "" : "out"),
          columns: [
            { key: "provider", label: "Provider", fmt: v => OG.providerLink(v) },
            { key: "provider_class", label: "Class", cls: "dim", fmt: v => (v || "–").replace("_", " ") },
            { key: "region", label: "Region", cls: "dim", fmt: v => v || "–" },
            { key: "price", label: "Vote $/GPU·h", num: true, title: "The provider's lowest eligible, not-sold-out listing price this hour", fmt: v => fmt.price(v) },
            { key: "status", label: "Included", value: r => (r.status === "included" ? 0 : 1), csv: r => r.status, fmt: (v, r) => h("span", { class: r.status === "included" ? "ix-pub" : "ix-unpub" }, h("i"), STATUS_TXT[r.status] || r.status) },
            { key: "w", label: "Weight in stat", num: true, desc: true, value: r => (r._w ? r._w.w : null), title: "Share of this hour's statistic, from the stated method (median or 20% trimmed mean over included votes). Derived, not stored.",
              csv: r => (r._w ? r._w.w : null), fmt: (v, r) => (r._w ? h("span", { class: r._w.w ? "" : "dimmer", title: r._w.note || "" }, r._w.w ? (r._w.w * 100).toFixed(r._w.w === 1 ? 0 : 1) + "%" : r._w.note === "trimmed (20% each end)" ? "trimmed" : "0%") : h("span", { class: "dimmer" }, "–")) },
            { key: "recorded_since", label: "Recorded since", num: true, value: r => (r.recorded_since ? +new Date(r.recorded_since) : null), csv: r => r.recorded_since, fmt: v => (v ? fmt.dateTime(v) : "–"), title: "First hour OpenGrid recorded this provider for this GPU" },
          ] }),
        h("p", { class: "note" }, "One vote per provider: its lowest eligible listing (on-demand, canonical, not sold out). Excluded when its feed was failing that hour or the vote is more than 3× above / below the median. Weight column is inferred from the method; the level is chained on the providers common to consecutive hours."));
      }

      function drawDef() {
        const kv = [
          ["Index id", h("span", { class: "mono" }, d.id)], ["Family", FAM_LABEL[d.kind] || d.kind], ["Segment", d.segment === "spot" ? "spot / interruptible" : "on-demand"],
          ["Unit", unitLabel(d.unit)], d.gpu ? ["GPU", OG.gpuLink(d.gpu, d.gpu)] : null, d.region_group ? ["Region group", d.region_group] : null,
          d.provider_class ? ["Provider class", d.provider_class.replace("_", " ")] : null, ["Price concept", (def.price_concept || "observed_market_price").replace(/_/g, " ")],
          ["Statistic", d.kind === "composite" ? "chained weighted relatives (base 100)" : "median (< 5 providers) / 20% trimmed mean, chain-linked"],
          ["Methodology", h("span", {}, h("a", { class: "lnk", href: "/methodology/indices" }, "indices"), h("span", { class: "dim" }, " · version " + (d.methodology_version || def.methodology_version || "?")))],
          d.coverage && d.coverage.first_published ? ["First published", fmt.dateTime(d.coverage.first_published)] : null,
          d.coverage && d.coverage.chain_segment ? ["Chain segment", `#${d.coverage.chain_segment} since ${fmt.dateTime(d.coverage.chain_segment_start)}`] : null,
        ].filter(Boolean);
        defEl.replaceChildren(...[def.description ? h("p", { class: "ix-desc" }, def.description) : null,
          h("table", { class: "ix-kv" }, h("tbody", {}, kv.map(([k, v]) => h("tr", {}, h("th", {}, k), h("td", {}, v))))),
          d.note ? h("p", { class: "note" }, "Latest hour: " + d.note) : null].filter(Boolean));
      }
      async function drawRelated() {
        const links = [];
        if (gpuSlug) links.push(h("a", { class: "lnk", href: "/gpu/" + gpuSlug }, OG.shortGpu(d.gpu) + " market: prices, listings, history →"));
        if (d.kind === "composite") for (const c of def.components || []) links.push(h("a", { class: "lnk", href: "/indices/" + encodeURIComponent(c.index_id) }, (c.name || c.index_id).replace(/^OpenGrid /, "")));
        relEl.replaceChildren(h("div", { class: "ix-rel-l" }, links.length ? links : h("span", { class: "dim" }, "–")));
        if (!d.gpu) return;
        const all = await OG.api.soft("/v1/indices");
        if (!ctx.alive() || !all) return;
        const sib = all.filter(r => r.gpu === d.gpu && r.id !== d.id);
        if (sib.length) relEl.append(h("div", { class: "ix-rel-h" }, "Other indices on this GPU"), h("table", { class: "ix-kv ix-sib" }, h("tbody", {}, sib.map(r =>
          h("tr", {}, h("td", {}, h("a", { class: "lnk", href: "/indices/" + encodeURIComponent(r.id) }, r.name.replace(/^OpenGrid /, ""))),
            h("td", { class: "n" }, r.published ? levelFmt(r.level, r.unit) : h("span", { class: "dimmer", title: r.reason }, "n/p")),
            h("td", { class: "n" }, r.published ? OG.chg(r.changes["24h"], { reason: r.change_reasons["24h"] }) : null))))));
      }

      drawConstituents(); drawDef(); drawRelated();
      loadHistory();
      ctx.every(300000, async () => { const nd = await OG.api.soft("/v1/indices/" + encodeURIComponent(id)); if (nd && ctx.alive()) { d = nd; drawConstituents(); } loadHistory(); });
    },
  });
})();
