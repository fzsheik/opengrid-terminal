/* /heatmaps — every /v1/heatmaps kind as a matrix. Null cells (no data — never zero) are hatched.
   Diverging scale (blue = below, red = above) for premiums and changes; sequential for availability,
   prices, counts and volatility. Kind and window are in the URL (?kind=…&days=…). */
(() => {
  const { h, fmt, Lib: L } = OG;
  const share = v => (v == null ? "–" : Math.round(v * 100) + "%");
  const GROUPS = [
    ["provider", "GPU × provider", [["gpu-provider-premium", "Premium vs market"], ["gpu-provider-availability", "Availability"], ["gpu-provider-change24h", "24h change"]]],
    ["region", "GPU × region", [["gpu-region-cheapest", "Cheapest"], ["gpu-region-premium", "Premium vs global"], ["gpu-region-availability", "Priced listings"]]],
    ["time", "GPU × day", [["gpu-time-volatility", "Volatility"], ["gpu-time-availability", "Availability"], ["gpu-time-change", "Daily change"]]],
  ];
  // per kind: scale, value format, domain rule, what a colour means
  const K = {
    "gpu-provider-premium": { div: true, f: fmt.pct, cap: 1, read: "provider's lowest vs the median of the other providers' lowest, now. Blue = cheaper than the rest of the market, red = dearer." },
    "gpu-provider-availability": { f: share, dom: [0, 1], days: true, read: "share of tracked hours the provider had a priced listing for that GPU." },
    "gpu-provider-change24h": { div: true, f: fmt.pct, cap: 0.25, read: "provider's lowest at the latest rollup hour vs 24 hours earlier." },
    "gpu-region-cheapest": { f: fmt.price, invert: true, units: "USD / GPU-hour", read: "cheapest priced listing in the region group, USD per GPU-hour. Brighter = cheaper." },
    "gpu-region-premium": { div: true, f: fmt.pct, cap: 1, read: "median provider lowest in the region vs the global median for that GPU." },
    "gpu-region-availability": { f: v => fmt.num(v), read: "number of priced listings in the region group now." },
    "gpu-time-volatility": { f: v => (v * 100).toFixed(2) + "%", days: true, read: "stdev of hourly log changes of the market median that day (≥ 12 changes, else empty)." },
    "gpu-time-availability": { f: share, dom: [0, 1], days: true, read: "provider-hours priced / provider-hours tracked that day." },
    "gpu-time-change": { div: true, f: fmt.pct, cap: 0.2, days: true, read: "market median close vs the previous day's close." },
  };
  const groupOf = kind => GROUPS.find(g => g[2].some(k => k[0] === kind)) || GROUPS[0];
  const pctl = (arr, q) => { const a = arr.slice().sort((x, y) => x - y); return a.length ? a[Math.min(a.length - 1, Math.floor(q * (a.length - 1) + 0.5))] : null; };

  OG.page("/heatmaps", {
    title: "Heatmaps",
    mount(el, params, query, ctx) {
      el.classList.add("hm-page");
      const st = { kind: K[OG.qs.get("kind")] ? OG.qs.get("kind") : "gpu-provider-premium", days: OG.qs.get("days"), order: OG.qs.get("order", "coverage"), empty: OG.qs.get("empty") === "1" };
      const grpHold = h("div", { class: "pv-hold" }), kindHold = h("div", { class: "pv-hold" }), dayHold = h("div", { class: "pv-hold" }), ordHold = h("div", { class: "pv-hold" });
      const statsEl = h("div", {}), chartEl = h("div", { class: "box box-p hm-box" }), legendEl = h("div", { class: "hm-legend" });
      el.append(OG.head("Heatmaps", "Where each GPU is cheap, dear, available or moving — across providers, regions and days. Hatched = no data, not zero.",
        h("span", { class: "flt" }, h("span", {}, "Order"), ordHold)),
        h("div", { class: "bar hm-ctl" }, grpHold, kindHold, h("span", { class: "spacer" }), dayHold),
        statsEl, legendEl, chartEl);

      function drawCtl() {
        const g = groupOf(st.kind), kd = K[st.kind];
        grpHold.replaceChildren(OG.tabs(GROUPS.map(x => [x[0], x[1]]), g[0], v => { setKind(GROUPS.find(x => x[0] === v)[2][0][0]); }));
        kindHold.replaceChildren(OG.seg(g[2], st.kind, setKind));
        const dflt = st.kind.startsWith("gpu-time-") ? "30" : "7";
        dayHold.replaceChildren(kd.days ? h("span", { class: "flt" }, h("span", {}, "Window"), OG.seg([["7", "7D"], ["14", "14D"], ["30", "30D"], ["90", "90D"]], st.days || dflt, v => { st.days = v === dflt ? null : v; OG.qs.set({ days: st.days }); load(); })) : h("span", { class: "dim hm-basis" }, "current snapshot"));
        ordHold.replaceChildren(h("button", { class: "chip", "aria-pressed": String(st.empty), title: "Show GPUs whose every cell is empty", onclick: () => { st.empty = !st.empty; OG.qs.set({ empty: st.empty ? "1" : null }); drawCtl(); draw(); } }, "Empty rows"), " ", OG.seg([["coverage", "Coverage"], ["name", "Name"], ["value", "Value"]], st.order, v => { st.order = v; OG.qs.set({ order: v === "coverage" ? null : v }); draw(); }));
      }
      function setKind(k) { st.kind = k; st.days = null; OG.qs.set({ kind: k === "gpu-provider-premium" ? null : k, days: null }); drawCtl(); load(); }

      let res = null, chart = null;
      async function load() {
        chartEl.replaceChildren(OG.loading("Loading matrix…"));
        try { res = await ctx.api("/v1/heatmaps/" + st.kind, { params: K[st.kind].days && st.days ? { days: st.days } : null, full: true, slot: "hm" }); }
        catch (e) { if (!e.stale) chartEl.replaceChildren(OG.error(e, load)); return; }
        OG.status.asOf(res.meta && res.meta.as_of);
        draw();
      }

      function draw() {
        if (!res) return;
        const d = res.data, kd = K[st.kind], meta = d.meta || {};
        const isTime = st.kind.startsWith("gpu-time-"), isProv = st.kind.startsWith("gpu-provider-");
        // reorder rows
        let idx = d.rows.map((_, i) => i);
        const filled = i => d.cells[i].filter(v => v != null).length;
        const mean = i => { const v = d.cells[i].filter(x => x != null); return v.length ? v.reduce((a, b) => a + b, 0) / v.length : null; };
        if (st.order === "name") idx.sort((a, b) => L.shortGpu(d.rows[a]).localeCompare(L.shortGpu(d.rows[b]), undefined, { numeric: true }));
        else if (st.order === "value") idx.sort((a, b) => (mean(a) ?? Infinity) - (mean(b) ?? Infinity));
        else idx.sort((a, b) => filled(b) - filled(a) || L.shortGpu(d.rows[a]).localeCompare(L.shortGpu(d.rows[b])));
        // time matrices: trim leading days where no GPU has data (keeps the axis on the data)
        let c0 = 0;
        if (isTime) { while (c0 < d.cols.length - 1 && d.rows.every((_, i) => d.cells[i][c0] == null)) c0++; }
        const hidden = st.empty ? [] : idx.filter(i => filled(i) === 0);
        if (!st.empty) idx = idx.filter(i => filled(i) > 0);
        const cols = d.cols.slice(c0), rows = idx.map(i => d.rows[i]), values = idx.map(i => d.cells[i].slice(c0));
        const all = values.flat().filter(v => v != null && isFinite(v));
        const total = rows.length * cols.length;

        // fragmentation, at a glance
        const perRow = values.map(r => r.filter(v => v != null).length);
        const single = d.cells.filter(r => r.filter(v => v != null).length <= 1).length;
        let widest = null;
        if (isProv || st.kind.startsWith("gpu-region-")) values.forEach((r, i) => { const v = r.filter(x => x != null); if (v.length >= 2) { const sp = Math.max(...v) - Math.min(...v); if (!widest || sp > widest.sp) widest = { i, sp, lo: Math.min(...v), hi: Math.max(...v) }; } });
        statsEl.replaceChildren(OG.stats([
          { label: "GPUs", value: String(rows.length) },
          { label: isProv ? "Providers" : isTime ? "Days" : "Region groups", value: String(cols.length) },
          { label: "Cells with data", value: total ? share(all.length / total) : null, reason: "empty matrix", sub: `${all.length} of ${total}` },
          isTime ? null : { label: "No comparison", value: String(single), sub: `GPUs with ≤ 1 ${isProv ? "provider" : "region"} cell`, title: "A GPU sold by one provider (or in one region) cannot be compared: its premium is undefined, not zero" },
          isTime ? null : { label: isProv ? "Median coverage" : "Median regions", value: perRow.length ? String(pctl(perRow, 0.5)) : null, reason: "no rows", sub: `${isProv ? "providers" : "regions"} per GPU` },
          widest ? { label: "Widest spread", value: h("span", {}, OG.gpuLink(rows[widest.i])), sub: `${kd.f(widest.lo)} → ${kd.f(widest.hi)}`, title: "GPU with the largest gap between its lowest and highest cell" } : null,
          { label: "Basis", value: meta.basis || (isTime ? `last ${st.days || 30} days` : "current"), kind: "inferred" },
        ]));

        // domain
        let domain = kd.dom;
        if (kd.div) { const m = Math.min(kd.cap, pctl(all.map(Math.abs), 0.95) || kd.cap) || kd.cap; domain = [-m, 0, m]; }
        else if (!domain) { const e = all.length ? [Math.min(...all), Math.max(...all)] : [0, 1]; domain = st.kind === "gpu-region-availability" ? [0, e[1] || 1] : e; }
        legendEl.replaceChildren(...[h("span", { class: "hm-k" }, kd.div ? "diverging" : "sequential"),
          h("span", {}, h("b", {}, groupOf(st.kind)[2].find(k => k[0] === st.kind)[1] + ": "), kd.read),
          h("span", { class: "dim" }, ` Unit: ${meta.unit || "–"}${meta.definition ? " · " + meta.definition : ""}${meta.min_hours ? ` · needs ≥ ${meta.min_hours} hours` : ""}${kd.div ? ` · colour clipped at ±${fmt.pct(domain[2]).replace("+", "")}` : ""}.`),
          meta.hour ? h("span", { class: "dim" }, ` Hour ${fmt.dateTime(meta.hour)}.`) : null,
          " ", h("a", { class: "lnk dim", href: "/methodology/" + (st.kind === "gpu-provider-availability" ? "provider-value" : "dispersion") }, "Methodology →"),
          hidden.length ? h("div", { class: "dim hm-hidden" }, `${hidden.length} GPU${hidden.length === 1 ? "" : "s"} with no cell hidden${st.kind.endsWith("-premium") ? " (a premium needs a second provider or region to compare with)" : ""}: `,
            hidden.slice(0, 14).map((i, k) => [k ? ", " : "", OG.gpuLink(d.rows[i])]), hidden.length > 14 ? " …" : "", ". ",
            h("a", { class: "lnk", href: "#", onclick: e => { e.preventDefault(); st.empty = true; OG.qs.set({ empty: "1" }); drawCtl(); draw(); } }, "Show them")) : null].filter(Boolean));

        if (chart) { chart.destroy(); chart = null; }
        if (!rows.length || !cols.length) {
          chartEl.replaceChildren(OG.insufficient(isTime ? `No GPU has a full day in the daily summaries for the last ${st.days || 30} days yet (they are built from the hourly rollup).` : "No cells: nothing is priced for this view right now.", "Empty matrix"));
          return;
        }
        const colLabel = c => isProv ? OG.providerName(c) : isTime ? fmt.date(c + "T12:00:00") : c;
        chartEl.textContent = "";
        chart = OG.charts.heatmap(chartEl, {
          rows: rows.map(r => L.shortGpu(r)), cols: cols.map(colLabel), values, scale: kd.div ? "diverging" : "sequential", domain, fmt: kd.f,
          nullText: "no data", invert: !!kd.invert, units: kd.units || meta.unit || null, labelMax: 260,
          cellTitle: (i, j, v) => v == null ? (isProv ? `${OG.providerName(cols[j])} has no ${st.kind === "gpu-provider-availability" ? "tracked hours" : "price"} for this GPU` : "no observation") : `${rows[i]} · ${isProv ? OG.providerName(cols[j]) : cols[j]}${meta.basis ? " · " + meta.basis : ""}`,
          rowHref: i => "/gpu/" + OG.slug(rows[i]),
          colHref: isProv ? j => "/provider/" + encodeURIComponent(cols[j]) : null,
          onCell: (i, j) => OG.go(isProv ? "/provider/" + encodeURIComponent(cols[j]) + "?gpu=" + OG.slug(rows[i]) : "/gpu/" + OG.slug(rows[i])),
          label: st.kind,
        });
      }

      drawCtl(); load();
      ctx.every(180000, load);
    },
  });
})();
