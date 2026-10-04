(() => {
  const L = Lib;
  const WINDOWS = [[1, "1H"], [6, "6H"], [24, "24H"], [168, "1W"], [0, "ALL"]];
  const KNOWN_LOGOS = new Set(Object.keys(L.PROVIDER_NAMES));
  const SVGNS = "http://www.w3.org/2000/svg";
  const REFRESH_MS = 30000;

  const state = {
    hours: 24, overview: null, detail: null, detailGpu: null,
    q: "", vendor: "All", sort: "providers", singles: false,
    show: { median: false }, hover: null,
    polling: null, seq: 0,
  };
  try {
    const saved = localStorage.getItem("og.hours");           // null means never chosen: Number(null) would be 0, which is ALL
    if (saved !== null && WINDOWS.some(w => w[0] === Number(saved))) state.hours = Number(saved);
  } catch (e) {}

  /* ---------- tiny DOM helpers ---------- */
  function h(tag, props, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v == null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid.nodeType ? kid : document.createTextNode(kid));
    return el;
  }
  function s(tag, attrs, ...kids) {
    const el = document.createElementNS(SVGNS, tag);
    for (const [k, v] of Object.entries(attrs || {})) if (v != null) el.setAttribute(k, v);
    for (const kid of kids.flat()) if (kid) el.append(kid);
    return el;
  }
  const $ = sel => document.querySelector(sel);

  function monoLogo(p, size) {
    return h("span", { class: "mono-logo", style: `width:${size}px;height:${size}px` }, (L.providerName(p)[0] || "?").toUpperCase());
  }
  function logo(p, size) {
    if (!KNOWN_LOGOS.has(p)) return monoLogo(p, size);
    const img = h("img", { class: "logo", src: `/static/logos/${p}.png`, alt: "", width: size, height: size, title: L.providerName(p) });
    img.addEventListener("error", () => img.replaceWith(monoLogo(p, size)));
    return img;
  }
  const arrowOf = dir => dir === "up" ? "▲ " : dir === "down" ? "▼ " : "";
  const sinceLabel = iso => iso ? L.timeLabel(iso, state.hours === 0 || state.hours > 36 ? 100 : 6) + (state.hours > 36 || state.hours === 0 ? "" : "") : "";

  /* ---------- data ---------- */
  async function getJSON(url) {
    const r = await fetch(url);
    if (!r.ok) throw new Error(url + " " + r.status);
    return r.json();
  }
  async function loadPolling() {
    try { state.polling = await getJSON("/polling"); } catch (e) { state.polling = null; }
    renderFoot();
  }
  async function loadOverview() {
    const seq = ++state.seq, hours = state.hours;
    try {
      const data = await getJSON(`/market?hours=${hours}`);
      if (seq !== state.seq || hours !== state.hours) return;
      state.overview = data; renderFoot(); if (route().tab === "market" && !route().gpu) renderGrid();
    } catch (e) { if (seq === state.seq) setLive(false, "api unreachable"); }
  }
  async function loadDetail() {
    const gpu = route().gpu; if (!gpu) return;
    const seq = ++state.seq, hours = state.hours;
    try {
      const data = await getJSON(`/market/detail?gpu=${encodeURIComponent(gpu)}&hours=${hours}`);
      if (seq !== state.seq || hours !== state.hours || gpu !== route().gpu) return;
      state.detail = data; state.detailGpu = gpu; renderFoot(); renderDetail();
    } catch (e) { if (seq === state.seq) setLive(false, "api unreachable"); }
  }
  const reload = () => (route().gpu ? loadDetail() : route().tab === "market" ? loadOverview() : null);

  /* ---------- routing ---------- */
  function route() {
    const parts = location.hash.replace(/^#\/?/, "").split("/");
    const tab = ["market", "routing", "deploy"].includes(parts[0]) ? parts[0] : "market";
    const gpu = tab === "market" && parts[1] ? decodeURIComponent(parts.slice(1).join("/")) : null;
    return { tab, gpu };
  }
  function go(hash) { location.hash = hash; }

  function render() {
    const { tab, gpu } = route();
    document.querySelectorAll(".nav a").forEach(a => a.toggleAttribute("aria-current", a.dataset.tab === tab) || a.removeAttribute("aria-current"));
    document.querySelectorAll(".nav a").forEach(a => { if (a.dataset.tab === tab) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current"); });
    const page = $("#page"); page.textContent = ""; state.hover = null;
    if (tab === "market" && !gpu) { mountMarket(page); loadOverview(); }
    else if (tab === "market") { mountDetail(page); loadDetail(); }
    else mountPlaceholder(page, tab);
  }

  /* ---------- sidebar status ---------- */
  function setLive(ok, text) {
    $("#live-dot").className = "dot " + (ok ? "live" : "stale");
    $("#live-text").textContent = text;
  }
  function renderFoot() {
    const p = state.polling, o = state.detail && route().gpu ? state.detail : state.overview;
    const n = p ? p.providers.length : null;
    const age = o && o.t1 ? (Date.now() - new Date(o.t1)) / 1000 : null;
    if (age != null && age > 300) setLive(false, "stale data");
    else if (o) setLive(true, "live");
    const d = $("#foot-detail"); d.textContent = "";
    if (n) d.append(`${n} providers`, h("br"), `polled every ${Math.round(Math.min(...p.providers.map(x => x.interval_seconds)) / 60)}m`);
    if (o && o.t1) d.append(h("br"), "as of " + new Date(o.t1).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }));
  }

  /* ---------- shared controls ---------- */
  function windowSeg() {
    return h("div", { class: "seg", role: "group", "aria-label": "Time range" }, WINDOWS.map(([hrs, label]) =>
      h("button", { "aria-pressed": hrs === state.hours ? "true" : "false", onclick: () => setHours(hrs) }, label)));
  }
  function setHours(hrs) {
    if (hrs === state.hours) return;
    state.hours = hrs; state.overview = null; state.detail = null;
    try { localStorage.setItem("og.hours", String(hrs)); } catch (e) {}
    document.querySelectorAll(".seg button").forEach(b => b.setAttribute("aria-pressed", String(b.textContent === WINDOWS.find(w => w[0] === hrs)[1])));
    if (route().gpu) { const c = $("#chart"); if (c) c.textContent = ""; }
    else renderGrid();
    reload();
  }

  /* ---------- market ---------- */
  function mountMarket(page) {
    page.append(
      h("div", { class: "head" },
        h("div", {}, h("h1", {}, "Market"), h("div", { class: "sub" }, "Lowest on-demand price per GPU-hour, across providers")),
        windowSeg()),
      h("div", { class: "bar" },
        h("input", { class: "field", type: "search", placeholder: "Search GPUs", "aria-label": "Search GPUs", value: state.q,
          oninput: e => { state.q = e.target.value; renderGrid(); } }),
        h("div", { id: "vendors", style: "display:flex;gap:6px" }),
        h("span", { class: "spacer" }),
        h("select", { class: "field", "aria-label": "Sort", onchange: e => { state.sort = e.target.value; renderGrid(); } },
          [["providers", "Most providers"], ["price", "Price: low to high"], ["priceDesc", "Price: high to low"], ["change", "Biggest move"]]
            .map(([v, t]) => h("option", { value: v, selected: v === state.sort }, t))),
        h("button", { class: "chip", id: "singles", "aria-pressed": String(state.singles), title: "GPUs only one provider sells have no market to compare",
          onclick: e => { state.singles = !state.singles; e.currentTarget.setAttribute("aria-pressed", String(state.singles)); renderGrid(); } }, "Single-provider GPUs")),
      h("div", { class: "grid", id: "grid" }, h("div", { class: "empty" }, "Loading market…")));
  }

  function renderGrid() {
    const grid = $("#grid"); if (!grid) return;
    const o = state.overview;
    if (!o) { grid.replaceChildren(h("div", { class: "empty" }, "Loading market…")); return; }
    const vendors = ["All", ...[...new Set(o.gpus.map(g => L.vendorOf(g.gpu)))].sort()];
    const vbox = $("#vendors");
    if (vbox) vbox.replaceChildren(...vendors.map(v => h("button", { class: "chip", "aria-pressed": String(v === state.vendor), onclick: () => { state.vendor = v; renderGrid(); } }, v)));

    const q = state.q.trim().toLowerCase();
    let list = o.gpus.filter(g =>
      (state.singles || g.providers >= 2) &&
      (state.vendor === "All" || L.vendorOf(g.gpu) === state.vendor) &&
      (!q || g.gpu.toLowerCase().includes(q)));
    const by = {
      providers: (a, b) => b.providers - a.providers || a.gpu.localeCompare(b.gpu),
      price: (a, b) => a.lowest - b.lowest,
      priceDesc: (a, b) => b.lowest - a.lowest,
      change: (a, b) => Math.abs(b.change_pct ?? 0) - Math.abs(a.change_pct ?? 0),
    }[state.sort];
    list = list.sort(by);
    grid.replaceChildren(...(list.length ? list.map(card) : [h("div", { class: "empty" }, "No GPUs match.")]));
  }

  function card(g) {
    const dir = L.direction(g.change_pct);
    const since = g.change_since ? " since " + L.timeLabel(g.change_since, state.hours === 0 ? 100 : state.hours) : "";
    return h("button", { class: "card", onclick: () => go("#/market/" + encodeURIComponent(g.gpu)),
      title: dir === "none" ? "Not enough history yet to show a change" : `Lowest price${since}` },
      h("div", { class: "card-top" }, h("span", { class: "gpu" }, L.shortGpu(g.gpu)), h("span", { class: "vendor" }, L.vendorOf(g.gpu))),
      h("div", { class: "price-row" },
        h("span", { class: "price" }, L.fmtPrice(g.lowest)), h("span", { class: "unit" }, "/GPU·HR"),
        h("span", { class: "chg " + dir }, arrowOf(dir) + (dir === "none" ? "–" : L.fmtPct(g.change_pct)))),
      sparkline(g.spark, dir),
      h("div", { class: "card-foot" }, logo(g.lowest_provider, 16), h("span", { class: "cut" }, L.providerName(g.lowest_provider), h("span", { class: "dim" }, " \u00B7 lowest")),
        h("span", { class: "spacer" }), h("span", { class: "mono" }, g.providers + (g.providers === 1 ? " provider" : " providers"))));
  }

  function sparkline(values, dir) {
    const W = 240, H = 46, pad = 4;
    values = values.slice(Math.max(0, values.findIndex(v => v != null)));   // start where the data starts
    const fin = L.finite(values);
    const color = dir === "up" ? "var(--up)" : dir === "down" ? "var(--down)" : "var(--muted)";
    const svg = s("svg", { class: "spark", viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none", "aria-hidden": "true" });
    if (!fin.length) { svg.append(s("line", { x1: 0, x2: W, y1: H / 2, y2: H / 2, stroke: "var(--line-2)", "stroke-dasharray": "2 4", "vector-effect": "non-scaling-stroke" })); return svg; }
    if (fin.length < 2) {                      // one sample is a price, not a trend
      svg.append(s("line", { x1: 0, x2: W, y1: H / 2, y2: H / 2, stroke: "var(--line-2)", "stroke-dasharray": "2 4", "vector-effect": "non-scaling-stroke" }),
        s("circle", { cx: W - 4, cy: H / 2, r: 2.5, fill: color }));
      return svg;
    }
    const lo = Math.min(...fin), hi = Math.max(...fin), span = hi - lo || 1;
    const n = values.length;
    const xAt = i => L.xFrac(i, n) * W;
    const yAt = v => hi === lo ? H / 2 : H - pad - ((v - lo) / span) * (H - 2 * pad);
    svg.append(
      hi === lo ? null : s("path", { d: L.areaPath(values, xAt, yAt, H), fill: color, opacity: ".09" }),
      s("path", { d: L.linePath(values, xAt, yAt), fill: "none", stroke: color, "stroke-width": "1.5", "stroke-linejoin": "round", "vector-effect": "non-scaling-stroke" }));
    return svg;
  }

  /* ---------- detail ---------- */
  function mountDetail(page) {
    const gpu = route().gpu;
    page.append(
      h("a", { class: "back", href: "#/market" }, "← Market"),
      h("div", { class: "detail", onpointerleave: () => setHover(null) },
        h("div", { class: "panel" },
          h("div", { class: "hero", id: "hero" }, h("div", { class: "gpu" }, L.shortGpu(gpu))),
          h("div", { class: "legend", id: "legend" }),
          h("div", { class: "chart-wrap", id: "wrap" }, s("svg", { id: "chart", role: "img", "aria-label": `Price history for ${gpu}` }), h("div", { class: "tip", id: "tip", hidden: true })),
          h("div", { class: "note", id: "note" })),
        h("div", { class: "panel plist", id: "plist" }, h("h2", {}, "Providers"))));
    const wrap = $("#wrap");
    if (window.ResizeObserver) { let raf; new ResizeObserver(() => { cancelAnimationFrame(raf); raf = requestAnimationFrame(drawChart); }).observe(wrap); }
  }

  function renderDetail() {
    const d = state.detail; if (!d || !$("#hero")) return;
    const gpu = d.gpu, live = d.providers.filter(p => p.now != null);
    const hero = $("#hero"); hero.textContent = "";
    if (!live.length) {
      hero.append(h("div", {}, h("div", { class: "vendor" }, L.vendorOf(gpu)), h("div", { class: "gpu" }, L.shortGpu(gpu))), windowSeg());
      $("#legend").textContent = ""; $("#note").textContent = "Nobody is selling this GPU in stock right now.";
      $("#chart").textContent = ""; $("#plist").replaceChildren(h("h2", {}, "Providers"));
      return;
    }
    const low = live[0], high = live[live.length - 1];
    const dir = L.direction(d.change_pct), med = d.median[d.median.length - 1];
    hero.append(
      h("div", {},
        h("div", { class: "vendor" }, L.vendorOf(gpu)),
        h("div", { class: "gpu" }, L.shortGpu(gpu)),
        h("div", { class: "price-row", style: "margin-top:6px" },
          h("span", { class: "price" }, L.fmtPrice(low.now)), h("span", { class: "unit" }, "/GPU·HR"),
          h("span", { class: "chg " + dir, style: "margin-left:10px", title: d.change_since ? "Lowest price since " + L.timeLabel(d.change_since, state.hours === 0 ? 100 : state.hours) : "Not enough history yet" },
            arrowOf(dir) + (dir === "none" ? "–" : L.fmtPct(d.change_pct)))),
        h("div", { class: "hero-meta" },
          h("span", {}, logo(low.provider, 14), " ", L.providerName(low.provider), " lowest"),
          h("span", {}, "median " + L.fmtPrice(med)),
          h("span", {}, "high " + L.fmtPrice(high.now) + " " + L.providerName(high.provider)),
          h("span", {}, live.length + " selling"))),
      windowSeg());
    const legend = $("#legend"); legend.textContent = "";
    const mi = state.show.median ? medianInfo(d.median) : null;
    legend.append(...[
      state.show.median
        ? h("span", { class: "key" }, h("i", { class: "k-line", style: `background:${mi ? mi.color : "var(--muted)"}` }), "Median of providers", mi ? h("span", { style: `color:${mi.color}` }, `${arrowOf(mi.dir)}${L.fmtPct(mi.pct)}`) : null)
        : h("span", { class: "key" }, h("i", { class: "k-best" }), "Best price"),
      h("span", { class: "spacer" }),
      h("button", { class: "chip", "aria-pressed": String(state.show.median), title: "A smooth curve of the median price across providers; everything else greys out", onclick: () => { state.show.median = !state.show.median; state.hover = null; renderDetail(); } }, "Median"),
    ].filter(Boolean));        // append(null) would print the word "null"
    const note = $("#note");
    const startIdx = Math.min(...[d.lowest, ...d.providers.map(p => p.series)].map(a => { const i = a.findIndex(v => v != null); return i < 0 ? Infinity : i; }));
    const began = isFinite(startIdx) && startIdx > 0 ? d.times[startIdx] : null;
    const spanH = L.spanHours(d.times[0], d.times[d.times.length - 1]);
    const when = iso => L.timeLabel(iso, Math.min(spanH, 36)) + (spanH <= 36 ? " today" : "");
    const from = d.market_from && d.market_from > (began || d.t0) ? d.market_from : null;
    note.textContent = [
      began ? `History begins ${when(began)}, when recording started.` : "",
      from ? `Hatched area: not every provider listed here was being recorded yet, so lowest and median start ${when(from)} to avoid a false jump as each one joined.` : "",
    ].filter(Boolean).join(" ");
    renderProviderList(d);
    drawChart();
  }

  function renderProviderList(d) {
    const box = $("#plist"); box.textContent = ""; box.append(h("h2", {}, "Providers"));
    for (const p of d.providers) {
      const dir = L.direction(p.change_pct), out = p.now == null;
      const sub = out ? "not selling now" : [p.gpu_count ? p.gpu_count + "\u00D7" : null, p.region, p.available === true ? "in stock" : null].filter(Boolean).join(" \u00B7 ");
      // Hover only: leaving the row clears it. Nothing is ever "selected".
      box.append(h("div", { class: "prow" + (out ? " out" : ""), "data-p": p.provider, tabindex: "0",
        style: out ? null : `box-shadow: inset 3px 0 0 ${L.providerColor(p.provider)}`,
        onmouseenter: () => setHover(p.provider), onmouseleave: () => setHover(null),
        onfocus: () => setHover(p.provider), onblur: () => setHover(null) },
        logo(p.provider, 22),
        h("div", { style: "min-width:0" }, h("div", { class: "nm" }, L.providerName(p.provider)), h("div", { class: "sub2" }, sub)),
        h("span", { class: "pv" }, L.fmtPrice(p.now)),
        h("span", { class: "pc chg " + (out ? "none" : dir) }, out || dir === "none" || dir === "flat" ? "" : arrowOf(dir) + L.fmtPct(p.change_pct))));
    }
  }

  /* ---------- the chart ---------- */
  // Normal view: each provider is a step line in its own colour; "best price" is a pale halo under them.
  // Median view: one smooth curve, green if the median rose and red if it fell, everything else greyed out.
  const UP = "#2fbf71", DOWN = "#ef5350", FLAT = "#98a4b1";
  let chartGeom = null;

  // The median over the window: first and last real value, and what that says.
  function medianInfo(median) {
    const fin = L.finite(median);
    if (fin.length < 2) return null;
    const pct = (fin[fin.length - 1] - fin[0]) / fin[0], dir = L.direction(pct);
    return { pct, dir, color: dir === "up" ? UP : dir === "down" ? DOWN : FLAT, first: fin[0], last: fin[fin.length - 1] };
  }

  // Hover changes styles on the elements already there: nothing is redrawn, so there is nothing to get stuck.
  function setHover(p) {
    if (state.show.median) p = null;
    if (state.hover === p) return;
    state.hover = p; applyFocus();
  }
  function applyFocus() {
    document.querySelectorAll(".prow").forEach(r => r.classList.toggle("hl", !!state.hover && r.dataset.p === state.hover));
    const g = chartGeom; if (!g || !g.refs) return;
    const f = state.show.median ? null : state.hover;
    for (const [p, el] of g.refs.lines) {
      const on = f === p;
      el.setAttribute("stroke-width", on ? 2.6 : 1.6);
      el.setAttribute("opacity", f ? (on ? 1 : 0.16) : 0.95);
      if (on) el.parentNode.appendChild(el);                      // draw the focused line on top
    }
    for (const [p, r] of g.refs.logos) {
      const on = f === p;
      r.g.setAttribute("opacity", f && !on ? 0.3 : 1);
      r.ring.setAttribute("stroke-width", on ? 2.4 : 1.6);
      r.label.style.fill = on ? "#fff" : "var(--muted)";
    }
    if (g.refs.halo) g.refs.halo.setAttribute("stroke-opacity", f ? ".08" : ".2");
  }

  function drawChart() {
    const full = state.detail, svg = $("#chart");
    if (!full || !svg || !full.times || !full.providers.length) return;
    // Start the x-axis at the earliest data any line has, so the chart is not mostly blank
    const firstAny = Math.min(...[full.lowest, ...full.providers.map(p => p.series)].map(a => { const i = a.findIndex(v => v != null); return i < 0 ? Infinity : i; }));
    const i0 = isFinite(firstAny) && full.times.length - firstAny >= 3 ? firstAny : 0;
    const sl = a => a.slice(i0);
    const d = { ...full, times: sl(full.times), lowest: sl(full.lowest), median: sl(full.median), highest: sl(full.highest),
      providers: full.providers.map(p => ({ ...p, series: sl(p.series) })) };

    const W = svg.clientWidth || 800, H = 400, Lm = 10, Rm = 104, Tm = 14, Bm = 28;
    const pw = W - Lm - Rm, ph = H - Tm - Bm, n = d.times.length;
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.textContent = "";

    const medianView = state.show.median, med = medianView ? medianInfo(d.median) : null;
    const refs = { lines: new Map(), logos: new Map(), halo: null };

    // vertical range
    let lo, hi;
    if (medianView && med) {                                      // frame the median curve itself
      const fin = L.finite(d.median); lo = Math.min(...fin); hi = Math.max(...fin);
      const pad = Math.max((hi - lo) * 0.45, hi * 0.04); lo = Math.max(0, lo - pad); hi += pad;
    } else {
      const vals = [...L.finite(d.lowest)];
      for (const p of d.providers) vals.push(...L.finite(p.series));
      if (!vals.length) return;
      lo = Math.min(...vals); hi = Math.max(...vals);
      if (hi === lo) { lo *= 0.95; hi *= 1.05; }
      const pad = (hi - lo) * 0.1; lo = Math.max(0, lo - pad); hi += pad;
    }
    const xAt = i => Lm + L.xFrac(i, n) * pw;
    const yAt = v => Tm + ph - ((v - lo) / (hi - lo)) * ph;
    chartGeom = { xAt, yAt, n, Lm, pw, Tm, ph, W, H, D: d, refs, med };

    const id = "g" + Math.random().toString(36).slice(2, 7);
    svg.append(s("defs", {},
      s("clipPath", { id: id + "p" }, s("rect", { x: Lm, y: Tm - 2, width: pw + 2, height: ph + 4 })),
      s("linearGradient", { id: id + "m", x1: 0, x2: 0, y1: 0, y2: 1 }, s("stop", { offset: "0", "stop-color": med ? med.color : FLAT, "stop-opacity": ".28" }), s("stop", { offset: "1", "stop-color": med ? med.color : FLAT, "stop-opacity": "0" })),
      s("pattern", { id: id + "h", width: 6, height: 6, patternUnits: "userSpaceOnUse", patternTransform: "rotate(45)" }, s("line", { x1: 0, y1: 0, x2: 0, y2: 6, stroke: "var(--line)", "stroke-width": 1.5 }))));

    // grid and axis labels
    for (const t of L.niceTicks(lo, hi, 5)) {
      const y = yAt(t);
      svg.append(s("line", { class: "axis-line", x1: Lm, x2: Lm + pw, y1: y, y2: y }), s("text", { x: Lm + 2, y: y - 4 }, L.fmtPrice(t)));
    }
    const span = L.spanHours(d.times[0], d.times[n - 1]);
    const xi = [0, 0.25, 0.5, 0.75, 1].map(f => Math.round(f * (n - 1)));
    xi.forEach((i, k) => svg.append(s("text", { x: xAt(i), y: H - 8, "text-anchor": k === 0 ? "start" : k === xi.length - 1 ? "end" : "middle" }, L.timeLabel(d.times[i], span))));

    // everything below is clipped to the plot
    const plot = s("g", { "clip-path": `url(#${id}p)` });
    svg.append(plot);
    const mf = d.market_from ? Math.max(0, d.times.findIndex(t => t >= d.market_from)) : 0;
    if (mf > 0) plot.append(s("rect", { x: Lm, y: Tm, width: xAt(mf) - Lm, height: ph, fill: `url(#${id}h)`, opacity: ".7" }));

    if (medianView) {
      // everything else greys out
      for (const p of d.providers) plot.append(s("path", { d: L.stepPath(p.series, xAt, yAt), fill: "none", stroke: "#5b6672", "stroke-width": 1, opacity: ".16", "stroke-linejoin": "round" }));
      if (med) {
        plot.append(s("path", { d: L.smoothArea(d.median, xAt, yAt, Tm + ph), fill: `url(#${id}m)` }),
          s("path", { d: L.smoothPath(d.median, xAt, yAt), fill: "none", stroke: med.color, "stroke-width": 2.6, "stroke-linejoin": "round", "stroke-linecap": "round" }));
        const k = d.median.length - 1 - [...d.median].reverse().findIndex(v => v != null), y = yAt(d.median[k]);
        svg.append(s("circle", { cx: xAt(k), cy: y, r: 8, fill: med.color, opacity: ".2" }), s("circle", { cx: xAt(k), cy: y, r: 3.6, fill: med.color }));
        const t1 = s("text", { x: Lm + pw + 14, y: y - 6, style: "fill:var(--dim);letter-spacing:.1em" }); t1.textContent = "MEDIAN"; svg.append(t1);
        const t2 = s("text", { x: Lm + pw + 14, y: y + 10, style: "fill:#fff;font-weight:600;font-size:12px" }); t2.textContent = L.fmtPrice(d.median[k]); svg.append(t2);
      } else {
        const t = s("text", { x: Lm + pw / 2, y: Tm + ph / 2, "text-anchor": "middle" }); t.textContent = "Not enough history for a median curve yet"; svg.append(t);
      }
    } else {
      // best price: a pale halo under the lines (only from where the market line is trustworthy)
      refs.halo = s("path", { d: L.stepPath(d.lowest, xAt, yAt), fill: "none", stroke: "#fff", "stroke-opacity": ".2", "stroke-width": 6, "stroke-linejoin": "round" });
      plot.append(refs.halo);
      for (const p of d.providers) {
        const line = s("path", { d: L.stepPath(p.series, xAt, yAt), fill: "none", stroke: L.providerColor(p.provider), "stroke-width": 1.6, opacity: ".95", "stroke-linejoin": "round", "stroke-linecap": "round" });
        refs.lines.set(p.provider, line); plot.append(line);
      }

      // provider logos at their current price, spread so none overlap
      const rightX = Lm + pw, top = Tm + 10, bottom = Tm + ph - 10;
      const live = d.providers.filter(p => p.now != null);
      const ys = L.spread(live.map(p => yAt(p.now)), 24, top, bottom);
      live.forEach((p, k) => {
        const color = L.providerColor(p.provider), y0 = yAt(p.now), y1 = ys[k];
        const g = s("g", { style: "cursor:default" });
        g.append(s("path", { d: `M${rightX} ${y0.toFixed(1)}L${rightX + 12} ${y1.toFixed(1)}`, stroke: color, "stroke-opacity": ".7", fill: "none" }));
        g.append(s("circle", { cx: rightX, cy: y0, r: 3, fill: color }));
        const cx = rightX + 24, r = 10.5;
        g.append(s("circle", { cx, cy: y1, r, fill: "#fff" }));
        if (KNOWN_LOGOS.has(p.provider)) {
          const cid = id + "c" + k;
          svg.querySelector("defs").append(s("clipPath", { id: cid }, s("circle", { cx, cy: y1, r })));
          const img = s("image", { href: `/static/logos/${p.provider}.png`, x: cx - 13, y: y1 - 13, width: 26, height: 26, "clip-path": `url(#${cid})`, preserveAspectRatio: "xMidYMid slice" });
          img.addEventListener("error", () => img.remove());
          g.append(img);
        } else {
          const t = s("text", { x: cx, y: y1 + 3.5, "text-anchor": "middle", style: "fill:#333;font-weight:600" }); t.textContent = (L.providerName(p.provider)[0] || "?").toUpperCase(); g.append(t);
        }
        const ring = s("circle", { cx, cy: y1, r, fill: "none", stroke: color, "stroke-width": 1.6 }); g.append(ring);
        const label = s("text", { x: cx + 16, y: y1 + 3.5, style: "fill:var(--muted)" }); label.textContent = L.fmtPrice(p.now); g.append(label);
        const title = s("title"); title.textContent = `${L.providerName(p.provider)} ${L.fmtPrice(p.now)}`; g.append(title);
        g.addEventListener("mouseenter", () => setHover(p.provider));
        g.addEventListener("mouseleave", () => setHover(null));
        refs.logos.set(p.provider, { g, ring, label });
        svg.append(g);
      });
    }

    // hover crosshair
    const cross = s("g", { style: "pointer-events:none" }); svg.append(cross);
    const hit = s("rect", { x: Lm, y: Tm, width: pw, height: ph, fill: "transparent" });
    hit.addEventListener("mouseenter", () => setHover(null));
    hit.addEventListener("mousemove", e => {
      const r = svg.getBoundingClientRect(), x = (e.clientX - r.left) * (W / r.width);
      showTip(cross, L.nearestIndex(x, n, Lm, Lm + pw));
    });
    hit.addEventListener("mouseleave", () => { cross.textContent = ""; $("#tip").hidden = true; });
    svg.append(hit);
    applyFocus();
  }

  function showTip(cross, i) {
    const g = chartGeom, d = g.D, tip = $("#tip"), svg = $("#chart"), med = g.med;
    cross.textContent = "";
    const x = g.xAt(i);
    cross.append(s("line", { x1: x, x2: x, y1: g.Tm, y2: g.Tm + g.ph, stroke: "var(--muted)", "stroke-dasharray": "3 3" }));
    const rows = d.providers.map(p => [p.provider, p.series[i]]).filter(r => r[1] != null).sort((a, b) => a[1] - b[1]);
    if (state.show.median) {
      if (med && d.median[i] != null) cross.append(s("circle", { cx: x, cy: g.yAt(d.median[i]), r: 4.5, fill: med.color, stroke: "var(--bg)", "stroke-width": 2 }));
    } else for (const [p, v] of rows) if (g.yAt(v) >= g.Tm - 1) cross.append(s("circle", { cx: x, cy: g.yAt(v), r: 3.5, fill: L.providerColor(p), stroke: "var(--bg)", "stroke-width": 1.5 }));
    const span = L.spanHours(d.times[0], d.times[d.times.length - 1]);
    tip.textContent = "";
    tip.append(h("div", { class: "t" }, new Date(d.times[i]).toLocaleString([], span > 36 ? { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false } : { hour: "2-digit", minute: "2-digit", hour12: false })));
    if (state.show.median && d.median[i] != null) tip.append(h("div", { class: "r best", style: med ? `color:${med.color}` : null }, h("span", { style: med ? `color:${med.color}` : null }, "Median"), h("span", {}, L.fmtPrice(d.median[i]))));
    if (d.lowest[i] != null) tip.append(h("div", { class: state.show.median ? "r" : "r best" }, h("span", {}, "Best price"), h("span", {}, L.fmtPrice(d.lowest[i]))));
    for (const [p, v] of rows.slice(0, 10)) tip.append(h("div", { class: "r" }, h("span", {}, h("i", { class: "sw", style: `background:${L.providerColor(p)}` }), L.providerName(p)), h("span", {}, L.fmtPrice(v))));
    if (rows.length > 10) tip.append(h("div", { class: "t" }, `+${rows.length - 10} more`));
    if (!rows.length) tip.append(h("div", { class: "t" }, "no listings"));
    tip.hidden = false;
    const scale = svg.getBoundingClientRect().width / g.W, wrapW = svg.getBoundingClientRect().width;
    const left = x * scale + 14;
    tip.style.left = (left + tip.offsetWidth > wrapW - 110 ? Math.max(8, x * scale - tip.offsetWidth - 14) : left) + "px";
    tip.style.top = "12px";
  }

  /* ---------- placeholders ---------- */
  function mountPlaceholder(page, tab) {
    const copy = {
      routing: ["Routing", "Describe a workload and get the cheapest provider that can run it, with the reasoning."],
      deploy: ["Deploy", "Launch on the provider routing picked. Not built yet."],
    }[tab];
    page.append(h("div", { class: "ph" }, h("div", {}, h("h1", {}, copy[0]), h("div", { class: "sub" }, copy[1]), h("span", { class: "tag" }, "Coming soon"))));
  }

  /* ---------- go ---------- */
  document.addEventListener("keydown", e => { if (e.key === "Escape" && route().gpu) go("#/market"); });
  window.addEventListener("hashchange", render);
  if (!location.hash) history.replaceState(null, "", "#/market");
  render(); loadPolling();
  setInterval(() => { reload(); loadPolling(); }, REFRESH_MS);
})();
