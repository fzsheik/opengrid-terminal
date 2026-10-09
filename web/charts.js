/* OG.charts: pure-SVG charts. No dependencies beyond lib.js (and core.js in the browser).

Every renderer takes a container element and an options object, draws into it, redraws on resize,
and returns a handle {el, update(opts), destroy()} (timeseries also has highlight(key|null)).

  OG.charts.timeseries(el, {times, series:[{key,label,values,color,step=true,width,dash,opacity,halo}],
                            band:{lo:[], hi:[], label, color}, events:[{t, label, kind, severity, detail, href, onClick(event), hint, color}],
                            yFmt, height=260, hatchBefore: iso, yZero=false, endLabels=true, legend=true, onHover(i|null),
                            gapReason(i) -> string|null (tooltip text where a series has no value)})
                            A value with null on both sides is drawn as a dot (a line cannot show it).
  OG.charts.sparkline(values, {dir, width=96, height=22, color}) -> <svg> (not a container renderer)
  OG.charts.bars(el, {items:[{label, value, color, href, title}], fmt, horizontal=true, height})
  OG.charts.histogram(el, {values, bins=20, fmt, marks:[{value, label}], height})
  OG.charts.heatmap(el, {rows:[label], cols:[label], values:[[v|null]], scale:"sequential"|"diverging",
                         domain:[lo,hi] | [lo,mid,hi], fmt, cellTitle(r,c,v), onCell(r,c,v), rowHref(r), colHref(c),
                         labelWidth (px; default fits the longest row label up to labelMax=260), units (legend text),
                         invert (sequential: low values get the strong colour), nullText})
  OG.charts.dotplot(el, {rows:[{label, href, points:[{key, value, color, label}], low, median, high}], fmt})
                         (narrow: row labels truncate with the full text on hover; axis labels thinned)
Pure math (also exported in Node for tests): scale, extent, padExtent, timeTicks, bins, lerpColor,
  colorSeq, colorDiv, bandPath, nearest, isolated, thinLabels, fitLabel.
*/
(function (root, factory) {
  const lib = root.Lib || (typeof require === "function" ? require("./lib.js") : null);
  const C = factory(lib);
  if (typeof module === "object" && module.exports) module.exports = C;
  else { root.OG = root.OG || {}; root.OG.charts = C; }
})(typeof self !== "undefined" ? self : this, function (L) {
  /* ---------------- pure math ---------------- */
  function scale(d0, d1, r0, r1) {
    const span = d1 - d0;
    const f = v => (span === 0 ? (r0 + r1) / 2 : r0 + ((v - d0) / span) * (r1 - r0));
    f.invert = p => (r1 === r0 ? d0 : d0 + ((p - r0) / (r1 - r0)) * span);
    f.domain = [d0, d1]; f.range = [r0, r1];
    return f;
  }
  function extent(...arrays) {
    let lo = Infinity, hi = -Infinity;
    for (const a of arrays) for (const v of a || []) if (v != null && isFinite(v)) { if (v < lo) lo = v; if (v > hi) hi = v; }
    return lo === Infinity ? null : [lo, hi];
  }
  // Pad a [lo, hi] range by a fraction; a flat range gets +-5%; zero: start at 0
  function padExtent(ext, frac, zero) {
    if (!ext) return null;
    let [lo, hi] = ext;
    if (hi === lo) { const d = Math.abs(lo) * 0.05 || 1; lo -= d; hi += d; }
    const p = (hi - lo) * (frac == null ? 0.08 : frac);
    lo = zero ? Math.min(0, lo) : lo - p; hi += p;
    if (!zero && ext[0] >= 0 && lo < 0) lo = 0;
    return [lo, hi];
  }
  const MIN = 60e3, HR = 36e5, DAY = 864e5;
  const STEPS = [MIN, 5 * MIN, 15 * MIN, 30 * MIN, HR, 3 * HR, 6 * HR, 12 * HR, DAY, 2 * DAY, 7 * DAY, 14 * DAY, 30 * DAY, 91 * DAY, 365 * DAY];
  // Round local-time ticks across [t0, t1] (ms), about `count` of them
  function timeTicks(t0, t1, count) {
    if (!(t1 > t0)) return [t0];
    const raw = (t1 - t0) / Math.max(1, count || 5);
    const step = STEPS.find(s => s >= raw) || STEPS[STEPS.length - 1];
    const off = new Date(t0).getTimezoneOffset() * MIN;            // align to local clock / midnight
    const out = [];
    for (let t = Math.ceil((t0 - off) / step) * step + off; t <= t1; t += step) out.push(t);
    return out;
  }
  // Equal-width bins on a nice step. Returns [{x0, x1, count}]
  function bins(values, n) {
    const fin = values.filter(v => v != null && isFinite(v));
    if (!fin.length) return [];
    let lo = Math.min(...fin), hi = Math.max(...fin);
    if (lo === hi) return [{ x0: lo, x1: hi, count: fin.length }];
    const ticks = L.niceTicks(lo, hi, n || 20);
    const step = ticks.length > 1 ? ticks[1] - ticks[0] : (hi - lo) / (n || 20);
    const start = Math.floor(lo / step) * step;
    const k = Math.max(1, Math.ceil((hi - start) / step + 1e-9));
    const out = Array.from({ length: k }, (_, i) => ({ x0: +(start + i * step).toFixed(10), x1: +(start + (i + 1) * step).toFixed(10), count: 0 }));
    for (const v of fin) out[Math.min(k - 1, Math.floor((v - start) / step + 1e-9))].count++;
    return out;
  }
  const hex = c => [1, 3, 5].map(i => parseInt(c.slice(i, i + 2), 16));
  function lerpColor(a, b, t) {
    const x = hex(a), y = hex(b), k = Math.max(0, Math.min(1, t));
    return "#" + x.map((v, i) => Math.round(v + (y[i] - v) * k).toString(16).padStart(2, "0")).join("");
  }
  // Sequential (one hue, dark -> light on the dark surface: near-zero recedes into the background)
  const SEQ = ["#122a4a", "#184f95", "#2a78d6", "#6da7ec", "#cde2fb"];
  // Diverging: blue (below) <- neutral grey -> red-orange (above)
  const DIV_LO = ["#6da7ec", "#2a78d6", "#1c5cab"], DIV_MID = "#3a3f46", DIV_HI = ["#f0a07a", "#e0603a", "#b5401f"];
  function ramp(stops, t) {
    t = Math.max(0, Math.min(1, t));
    const f = t * (stops.length - 1), i = Math.min(stops.length - 2, Math.floor(f));
    return lerpColor(stops[i], stops[i + 1], f - i);
  }
  const colorSeq = t => ramp(SEQ, t);
  // t in [-1, 1]; 0 is the neutral midpoint
  const colorDiv = t => (t == null || !isFinite(t) ? null : t < 0 ? ramp([DIV_MID, ...DIV_LO], -t) : ramp([DIV_MID, ...DIV_HI], t));
  // Closed polygons between lo[] and hi[] over each run where both exist
  function bandPath(lo, hi, xAt, yAt, step) {
    let d = "", run = [];
    const f = n => n.toFixed(1);
    const flush = () => {
      if (run.length) {
        const top = [], bot = [];
        run.forEach((i, k) => {
          const x = xAt(i);
          if (step && k > 0) { const xp = x; top.push([xp, yAt(hi[run[k - 1]])]); bot.push([xp, yAt(lo[run[k - 1]])]); }
          top.push([x, yAt(hi[i])]); bot.push([x, yAt(lo[i])]);
        });
        if (top.length === 1) { const [x, y] = top[0]; top.push([x + 1, y]); bot.push([x + 1, bot[0][1]]); }
        d += "M" + top.map(p => f(p[0]) + " " + f(p[1])).join("L") + "L" + bot.reverse().map(p => f(p[0]) + " " + f(p[1])).join("L") + "Z";
      }
      run = [];
    };
    for (let i = 0; i < lo.length; i++) { if (lo[i] == null || hi[i] == null) flush(); else run.push(i); }
    flush();
    return d;
  }
  // Index in ascending `arr` closest to v
  function nearest(arr, v) {
    if (!arr.length) return -1;
    let lo = 0, hi = arr.length - 1;
    while (hi - lo > 1) { const m = (lo + hi) >> 1; if (arr[m] <= v) lo = m; else hi = m; }
    return Math.abs(arr[hi] - v) < Math.abs(arr[lo] - v) ? hi : lo;
  }
  // Indices of values with no neighbour on either side (a lone published point a line cannot show)
  function isolated(values) {
    const out = [];
    for (let i = 0; i < (values || []).length; i++) if (values[i] != null && isFinite(values[i]) && values[i - 1] == null && values[i + 1] == null) out.push(i);
    return out;
  }
  // Labels that fit: keep a label only if it starts at least `gap` px after the previous kept one ends.
  // items: [{x, w}] (centre x, width) in drawing order -> boolean[] keep
  function thinLabels(items, gap) {
    let end = -Infinity;
    return items.map(it => { const x0 = it.x - it.w / 2; if (x0 >= end + (gap == null ? 6 : gap)) { end = it.x + it.w / 2; return true; } return false; });
  }
  // Truncate a label to about `px` pixels at `cw` px per character
  function fitLabel(s, px, cw) {
    s = String(s == null ? "" : s);
    const n = Math.max(3, Math.floor(px / (cw || 6.2)));
    return s.length <= n ? s : s.slice(0, n - 1) + "…";
  }
  const C = { scale, extent, padExtent, timeTicks, bins, lerpColor, colorSeq, colorDiv, bandPath, nearest, isolated, thinLabels, fitLabel, SEQ };
  if (typeof document === "undefined") return C;

  /* ---------------- DOM plumbing ---------------- */
  const NS = "http://www.w3.org/2000/svg";
  function s(tag, attrs, ...kids) {
    const el = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) if (v != null && v !== false) el.setAttribute(k, v);
    for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    return el;
  }
  function hd(tag, cls, ...kids) { const el = document.createElement(tag); if (cls) el.className = cls; for (const k of kids.flat()) if (k != null && k !== false) el.append(k.nodeType ? k : document.createTextNode(String(k))); return el; }
  const priceFmt = v => L.fmtPrice(v);
  let uid = 0;
  // A container that redraws on resize and owns a tooltip
  function frame(el, draw) {
    el.classList.add("chart");
    const tip = hd("div", "ctip"); tip.hidden = true;
    let raf = 0, ro = null, lastW = 0;
    const redraw = () => { const w = el.clientWidth || 600; lastW = w; draw(w, tip); el.append(tip); };
    if (window.ResizeObserver) { ro = new ResizeObserver(() => { if (Math.abs((el.clientWidth || 0) - lastW) < 2) return; cancelAnimationFrame(raf); raf = requestAnimationFrame(redraw); }); ro.observe(el); }
    redraw();
    return {
      redraw,
      destroy() { if (ro) ro.disconnect(); el.textContent = ""; },
      tip(x, y, nodes) {
        tip.replaceChildren(...nodes); tip.hidden = false;
        const w = el.clientWidth, tw = tip.offsetWidth, th = tip.offsetHeight;
        tip.style.left = Math.max(2, x + 14 + tw > w ? x - tw - 14 : x + 14) + "px";
        tip.style.top = Math.max(2, Math.min(y - 8, el.clientHeight - th - 2)) + "px";
      },
      hide() { tip.hidden = true; },
    };
  }
  const row = (label, value, color, strong) => hd("div", "ct-r" + (strong ? " b" : ""), hd("span", "", color ? swatch(color) : null, label), hd("span", "", value));
  function swatch(color) { const i = document.createElement("i"); i.className = "sw"; i.style.background = color; return i; }
  function svgText(attrs, text) { const t = s("text", attrs); t.textContent = text; return t; }

  /* ---------------- time series ---------------- */
  function timeseries(el, opts) {
    let o = opts, f = null, refs = null, hl = null;
    const SEV = { major: "#ef5350", notable: "#f5a524", info: "#7d8895" };
    function draw(W, tip) {
      el.replaceChildren();
      let legendEl = null;
      const series = o.series || [], times = (o.times || []).map(t => +new Date(t)), n = times.length;
      const H = o.height || 260, ev = o.events || [];
      const showEnd = o.endLabels !== false && series.filter(x => !x.halo).length <= 12;
      const Lm = 6, Rm = showEnd ? 92 : 52, Tm = 10, Bm = ev.length ? 34 : 22;
      const pw = Math.max(40, W - Lm - Rm), ph = H - Tm - Bm;
      const svg = s("svg", { class: "ts", width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": o.label || "time series" });
      el.append(svg);
      if (o.legend !== false && series.filter(x => !x.halo && x.label).length >= 2) {
        el.prepend(legendEl = hd("div", "c-legend", series.filter(x => !x.halo && x.label).map(x => {
          const k = hd("span", "c-key", swatch(x.color || "#94a3b8"), x.label); k.dataset.k = x.key;
          k.addEventListener("mouseenter", () => api.highlight(x.key)); k.addEventListener("mouseleave", () => api.highlight(null));
          return k;
        }), o.band ? hd("span", "c-key", hd("i", "sw band"), o.band.label || "range") : null));
      }
      if (!n) { svg.append(svgText({ x: W / 2, y: H / 2, "text-anchor": "middle", class: "c-mute" }, o.emptyText || "no data")); return; }
      // Robust scale: the lines (median, lowest…) set the range; the low–high band may add headroom up to
      // its 75th percentile (at most one line-range above the lines). A band high beyond that is clipped and
      // flagged at the top edge, so one outlier listing cannot flatten every line into the floor.
      const core = extent(...series.filter(x => !x.halo).map(x => x.values));
      let cap = null;
      if (o.band && o.band.hi && core && o.robust !== false) {
        const his = o.band.hi.filter(v => v != null && isFinite(v)).sort((a, b) => a - b);
        const bandMax = his.length ? his[his.length - 1] : null;
        const p75 = his.length ? his[Math.floor(0.75 * (his.length - 1))] : null;
        const room = Math.max(core[1] - core[0], core[1] * 0.15);
        const lim = Math.max(core[1], Math.min(p75 == null ? core[1] : p75, core[1] + room));
        if (bandMax != null && bandMax > lim * 1.02) cap = lim;
      }
      const bandHi = o.band && o.band.hi ? (cap == null ? o.band.hi : o.band.hi.map(v => (v == null ? v : Math.min(v, cap)))) : null;
      const ext = padExtent(extent(...series.map(x => x.values), o.band && o.band.lo, bandHi), 0.08, o.yZero);
      if (!ext) { svg.append(svgText({ x: W / 2, y: H / 2, "text-anchor": "middle", class: "c-mute" }, o.emptyText || "no data in this window")); return; }
      const t0 = times[0], t1 = times[n - 1] === t0 ? t0 + 1 : times[n - 1];
      const xs = scale(t0, t1, Lm, Lm + pw), ys = scale(ext[0], ext[1], Tm + ph, Tm);
      const xAt = i => xs(times[i]);
      const yf = o.yFmt || priceFmt;
      const id = "c" + (++uid);
      svg.append(s("defs", {},
        s("clipPath", { id: id + "p" }, s("rect", { x: Lm, y: Tm - 3, width: pw + 1, height: ph + 6 })),
        s("pattern", { id: id + "h", width: 7, height: 7, patternUnits: "userSpaceOnUse", patternTransform: "rotate(45)" }, s("line", { x1: 0, y1: 0, x2: 0, y2: 7, class: "c-hatch" }))));
      const yTicks = [];
      for (const t of L.niceTicks(ext[0], ext[1], Math.max(3, Math.round(ph / 38)))) {
        const y = ys(t), lab = svgText({ x: Lm + pw + 6, y: y + 3.5, class: "c-ax" }, yf(t));
        svg.append(s("line", { class: "c-grid", x1: Lm, x2: Lm + pw, y1: y, y2: y }), lab);
        yTicks.push([y, lab]);
      }
      const tt = timeTicks(t0, t1, Math.max(2, Math.round(pw / 110)));
      const span = (t1 - t0) / HR;
      for (const t of tt) {
        const x = xs(t);
        if (x < Lm + 16 || x > Lm + pw - 16) continue;
        // date-only labels (spans > 4 days): label midnights only, so a 12-hour grid never prints a date twice
        const dt = new Date(t), dateOnly = span > 96 && !(dt.getHours() === 0 && dt.getMinutes() === 0);
        svg.append(s("line", { class: "c-tick", x1: x, x2: x, y1: Tm + ph, y2: Tm + ph + (dateOnly ? 2 : 4) }), dateOnly ? null : svgText({ x, y: Tm + ph + 15, "text-anchor": "middle", class: "c-ax" }, L.timeLabel(dt.toISOString(), span)));
      }
      svg.append(s("line", { class: "c-base", x1: Lm, x2: Lm + pw, y1: Tm + ph, y2: Tm + ph }));
      const plot = s("g", { "clip-path": `url(#${id}p)` });
      svg.append(plot);
      if (o.hatchBefore) {
        const hx = xs(+new Date(o.hatchBefore));
        if (hx > Lm) {
          const hw = Math.min(pw, hx - Lm);
          plot.append(s("rect", { x: Lm, y: Tm, width: hw, height: ph, fill: `url(#${id}h)`, class: "c-hatchrect" }),
            s("line", { x1: Lm + hw, x2: Lm + hw, y1: Tm, y2: Tm + ph, class: "c-hatchedge" }));
          if (hw > 96) plot.append(svgText({ x: Lm + hw - 5, y: Tm + ph - 6, "text-anchor": "end", class: "c-hatchl" }, "partial coverage"));
        }
      }
      if (o.band && o.band.lo && bandHi) {
        plot.append(s("path", { d: bandPath(o.band.lo, bandHi, xAt, ys, o.band.step !== false), fill: o.band.color || "#7d8895", "fill-opacity": ".09" }));
        if (cap != null) {
          // band highs off the scale: a tick on the top edge per clipped hour + one label with the true max
          let d = "", maxV = -Infinity;
          o.band.hi.forEach((v, i) => { if (v != null && v > cap) { const x = xAt(i); const yc = ys(cap); d += `M${x.toFixed(1)} ${yc.toFixed(1)}L${x.toFixed(1)} ${(yc - 5).toFixed(1)}`; if (v > maxV) maxV = v; } });
          plot.append(s("path", { d, class: "c-clip" }));
          const txt = `▲ ${o.band.label || "high"} clipped above ${yf(cap)} · max ${yf(maxV)}`;
          if (legendEl) legendEl.append(hd("span", "c-key c-clipk", txt));
          else svg.append(svgText({ x: Lm + pw - 4, y: Tm + ph - 6, "text-anchor": "end", class: "c-clipl" }, txt));
        }
      }
      refs = new Map();
      for (const x of series) {
        const d = x.step === false ? L.linePath(x.values, xAt, ys) : L.stepPath(x.values, xAt, ys);
        const p = s("path", { d, fill: "none", stroke: x.color || "#94a3b8", "stroke-width": x.halo ? 6 : x.width || 1.5, "stroke-opacity": x.halo ? 0.18 : x.opacity || 0.95,
          "stroke-dasharray": x.dash || null, "stroke-linejoin": "round", "stroke-linecap": "round" });
        plot.append(p);
        // a published value with nothing on either side is invisible as a line: draw it as a dot
        const dots = x.halo ? [] : isolated(x.values).map(i => s("circle", { cx: xAt(i), cy: ys(x.values[i]), r: (x.width || 1.5) + 1, fill: x.color || "#94a3b8", class: "c-iso" }));
        plot.append(...dots);
        if (!x.halo) refs.set(x.key, { p, x, dots });
      }
      // direct labels at the right edge, spread so they never overlap
      if (showEnd) {
        const lastOf = v => { for (let i = v.length - 1; i >= 0; i--) if (v[i] != null) return i; return -1; };
        const ends = series.filter(x => !x.halo).map(x => ({ x, i: lastOf(x.values) })).filter(e => e.i === n - 1);
        const pos = L.spread(ends.map(e => ys(e.x.values[e.i])), 12, Tm + 4, Tm + ph);
        ends.forEach((e, k) => {
          const y0 = ys(e.x.values[e.i]), y1 = pos[k], xr = Lm + pw;
          const g = s("g", { class: "c-end" });
          g.append(s("circle", { cx: xr, cy: y0, r: 2.5, fill: e.x.color || "#94a3b8" }),
            svgText({ x: xr + 6, y: y1 + 3.5, class: "c-endl" }, yf(e.x.values[e.i])));
          svg.append(g);
          const r = refs.get(e.x.key); if (r) r.end = g;
        });
        // an axis label under an end label is unreadable: the end label (the data) wins
        for (const [y, lab] of yTicks) if (pos.some(p => Math.abs(p - y) < 11)) lab.remove();
      }
      // event markers on the time axis
      const evY = Tm + ph + 24;
      const evG = s("g", { class: "c-events" });
      // markers closer than 7px merge into one (the most severe wins), so a busy week reads as marks, not a fence
      const RANK = { major: 2, notable: 1 };
      const inRange = ev.map(e => ({ e, x: xs(+new Date(e.t)) })).filter(m => { const t = +new Date(m.e.t); return t >= t0 && t <= t1; })
        .sort((a, b) => (RANK[b.e.severity] || 0) - (RANK[a.e.severity] || 0));
      const kept = [];
      for (const m of inRange) if (!kept.some(k => Math.abs(k.x - m.x) < 7)) kept.push(m);
      kept.sort((a, b) => a.x - b.x);
      for (const { e } of kept) {
        const t = +new Date(e.t);
        const x = xs(t), color = e.color || SEV[e.severity] || SEV.info;
        const g = s("g", { class: "c-ev", tabindex: "0", "aria-label": e.label });
        g.append(s("line", { x1: x, x2: x, y1: Tm, y2: Tm + ph, class: "c-evline", stroke: color }),
          s("path", { d: `M${x - 4} ${evY + 4}L${x} ${evY - 3}L${x + 4} ${evY + 4}Z`, fill: color }),
          s("rect", { x: x - 6, y: evY - 6, width: 12, height: 14, fill: "transparent" }));
        const act = e.onClick || e.href;
        const show = () => { g.classList.add("on"); f.tip(x, evY - 60, [hd("div", "ct-t", L.timeLabel(e.t, 100) + " " + new Date(e.t).toTimeString().slice(0, 5) + (e.kind ? " · " + e.kind : "")),
          hd("div", "ct-ev", e.label || ""), e.detail ? hd("div", "ct-d", e.detail) : null, e.onClick && e.hint !== false ? hd("div", "ct-d", e.hint || "click for details") : null]); };
        const hide = () => { g.classList.remove("on"); f.hide(); };
        g.addEventListener("mouseenter", show); g.addEventListener("focus", show);
        g.addEventListener("mouseleave", hide); g.addEventListener("blur", hide);
        if (act) {
          g.setAttribute("role", "button");
          const run = () => (e.onClick ? e.onClick(e) : window.OG && OG.go ? OG.go(e.href) : (location.href = e.href));
          g.addEventListener("click", run);
          g.addEventListener("keydown", k => { if (k.key === "Enter" || k.key === " ") { k.preventDefault(); run(); } });
        } else g.classList.add("noact");
        evG.append(g);
      }
      // crosshair
      const cross = s("g", { class: "c-cross" });
      const hit = s("rect", { x: Lm, y: Tm, width: pw, height: ph, fill: "transparent" });
      hit.addEventListener("mousemove", e => {
        const r = svg.getBoundingClientRect(), px = (e.clientX - r.left) * (W / r.width);
        const i = nearest(times, xs.invert(px));
        cross.replaceChildren();
        const x = xAt(i);
        cross.append(s("line", { x1: x, x2: x, y1: Tm, y2: Tm + ph, class: "c-xh" }));
        const vals = series.filter(x2 => !x2.halo || x2.label).map(x2 => [x2, x2.values[i]]).filter(v => v[1] != null).sort((a, b) => a[1] - b[1]);
        for (const [x2, v] of vals) if (!x2.halo) cross.append(s("circle", { cx: x, cy: ys(v), r: 3, fill: x2.color || "#94a3b8", class: "c-dot" }));
        const nodes = [hd("div", "ct-t", new Date(times[i]).toLocaleString([], span > 36 ? { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false } : { hour: "2-digit", minute: "2-digit", hour12: false }))];
        if (o.band && o.band.lo[i] != null) nodes.push(row(o.band.label || "range", yf(o.band.lo[i]) + "–" + yf(o.band.hi[i])));
        vals.slice(0, 12).forEach(([x2, v]) => nodes.push(row(x2.label || x2.key, yf(v), x2.halo ? null : x2.color, x2.halo || x2.strong)));
        if (vals.length > 12) nodes.push(hd("div", "ct-t", `+${vals.length - 12} more`));
        if (!vals.length) nodes.push(hd("div", "ct-t", (o.gapReason && o.gapReason(i)) || "no data at this time"));
        else if (o.gapReason && vals.length < series.filter(x2 => !x2.halo).length) { const why = o.gapReason(i); if (why) nodes.push(hd("div", "ct-d", why)); }
        f.tip(x * (r.width / W), 4, nodes);
        o.onHover && o.onHover(i);
      });
      hit.addEventListener("mouseleave", () => { cross.replaceChildren(); f.hide(); o.onHover && o.onHover(null); });
      svg.append(cross, hit, evG);
      applyHl();
    }
    function applyHl() {
      if (!refs) return;
      for (const [k, r] of refs) {
        const on = hl === k;
        r.p.setAttribute("stroke-opacity", hl == null ? r.x.opacity || 0.95 : on ? 1 : 0.15);
        r.p.setAttribute("stroke-width", on ? (r.x.width || 1.5) + 1 : r.x.width || 1.5);
        if (on) r.p.parentNode.appendChild(r.p);
        for (const d of r.dots || []) d.setAttribute("opacity", hl == null || on ? 1 : 0.2);
        if (r.end) r.end.setAttribute("opacity", hl == null || on ? 1 : 0.3);
      }
      el.querySelectorAll(".c-key[data-k]").forEach(k => k.classList.toggle("dimmed", hl != null && k.dataset.k !== String(hl)));
    }
    f = frame(el, draw);
    const api = { el, update(n2) { o = Object.assign({}, o, n2); f.redraw(); }, highlight(k) { hl = k; applyHl(); }, destroy: () => f.destroy() };
    return api;
  }

  /* ---------------- sparkline ---------------- */
  function sparkline(values, opts) {
    opts = opts || {};
    const W = opts.width || 96, H = opts.height || 22, pad = 2;
    values = (values || []).slice(Math.max(0, (values || []).findIndex(v => v != null)));
    const fin = L.finite(values);
    const color = opts.color || (opts.dir === "up" ? "var(--up)" : opts.dir === "down" ? "var(--down)" : "var(--muted)");
    const svg = s("svg", { class: "spark", width: W, height: H, viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none", "aria-hidden": "true" });
    if (fin.length < 2) {
      svg.append(s("line", { x1: 0, x2: W, y1: H / 2, y2: H / 2, stroke: "var(--line-2)", "stroke-dasharray": "2 3" }));
      if (fin.length) svg.append(s("circle", { cx: W - 3, cy: H / 2, r: 2, fill: color }));
      return svg;
    }
    const lo = Math.min(...fin), hi = Math.max(...fin), n = values.length;
    const xAt = i => L.xFrac(i, n) * (W - 2) + 1;
    const yAt = v => (hi === lo ? H / 2 : H - pad - ((v - lo) / (hi - lo)) * (H - 2 * pad));
    svg.append(s("path", { d: L.stepPath(values, xAt, yAt), fill: "none", stroke: color, "stroke-width": 1.25, "stroke-linejoin": "round" }));
    return svg;
  }

  /* ---------------- bars ---------------- */
  function bars(el, opts) {
    let o = opts;
    const f = frame(el, (W) => {
      el.replaceChildren();
      const items = o.items || [], vf = o.fmt || (v => L.fmtPrice(v));
      if (!items.length) { el.append(hd("div", "c-mute", o.emptyText || "no data")); return; }
      const ext = extent(items.map(i => i.value)) || [0, 1];
      const lo = Math.min(0, ext[0]), hi = Math.max(0, ext[1]) || 1;
      if (o.horizontal !== false) {
        const rh = 18, Lw = Math.min(160, W * 0.32), Rw = 64, H = items.length * rh + 4;
        const xs = scale(lo, hi, Lw, W - Rw);
        const svg = s("svg", { class: "bars", width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": o.label || "bar chart" });
        items.forEach((it, k) => {
          const y = k * rh + 2, x0 = xs(0), x1 = it.value == null ? x0 : xs(it.value);
          const g = s("g", { class: "c-bar" });
          g.append(svgText({ x: Lw - 6, y: y + 12, "text-anchor": "end", class: "c-lab" }, it.label),
            it.value == null ? svgText({ x: x0 + 4, y: y + 12, class: "c-mute" }, it.reason || "n/a")
              : s("rect", { x: Math.min(x0, x1), y: y + 3, width: Math.max(1, Math.abs(x1 - x0)), height: rh - 6, rx: 1.5, fill: it.color || "var(--accent)" }),
            it.value == null ? null : svgText({ x: W - Rw + 6, y: y + 12, class: "c-val" }, vf(it.value)));
          if (it.title) g.append(s("title", {}, it.title));
          if (it.href) { g.style.cursor = "pointer"; g.addEventListener("click", () => OG.go(it.href)); }
          svg.append(g);
        });
        el.append(svg);
      } else {
        const H = o.height || 160, Bm = 18, Tm = 8, Rm = 44, pw = W - Rm, bw = pw / items.length;
        const ys = scale(lo, hi, H - Bm, Tm);
        const svg = s("svg", { class: "bars", width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": o.label || "column chart" });
        for (const t of L.niceTicks(lo, hi, 4)) svg.append(s("line", { class: "c-grid", x1: 0, x2: pw, y1: ys(t), y2: ys(t) }), svgText({ x: pw + 4, y: ys(t) + 3.5, class: "c-ax" }, vf(t)));
        items.forEach((it, k) => {
          if (it.value == null) return;
          const x = k * bw + 1, y0 = ys(0), y1 = ys(it.value);
          const r = s("rect", { x, y: Math.min(y0, y1), width: Math.max(1, bw - 2), height: Math.max(1, Math.abs(y1 - y0)), fill: it.color || "var(--accent)", rx: 1 });
          r.addEventListener("mousemove", e => f.tip(e.offsetX, e.offsetY, [hd("div", "ct-t", it.label), row("", vf(it.value))]));
          r.addEventListener("mouseleave", () => f.hide());
          svg.append(r);
          if (items.length <= 24) svg.append(svgText({ x: x + bw / 2 - 1, y: H - 5, "text-anchor": "middle", class: "c-ax" }, it.short || it.label));
        });
        el.append(svg);
      }
    });
    return { el, update(n2) { o = Object.assign({}, o, n2); f.redraw(); }, destroy: () => f.destroy() };
  }

  /* ---------------- histogram ---------------- */
  function histogram(el, opts) {
    let o = opts;
    const f = frame(el, (W) => {
      el.replaceChildren();
      const b = bins(o.values || [], o.bins || 20), vf = o.fmt || (v => L.fmtPrice(v));
      if (!b.length) { el.append(hd("div", "c-mute", o.emptyText || "no data")); return; }
      const H = o.height || 140, Bm = 18, Tm = 8;
      const x0 = b[0].x0, x1 = b[b.length - 1].x1 === x0 ? x0 + 1 : b[b.length - 1].x1;
      const xs = scale(x0, x1, 4, W - 4), ys = scale(0, Math.max(...b.map(x => x.count)), H - Bm, Tm);
      const svg = s("svg", { class: "hist", width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": o.label || "distribution" });
      for (const bin of b) {
        const x = xs(bin.x0), w = Math.max(1, (b.length === 1 ? 12 : xs(bin.x1) - xs(bin.x0)) - 2);
        const r = s("rect", { x: x + 1, y: ys(bin.count), width: w, height: H - Bm - ys(bin.count), fill: o.color || "var(--accent)", "fill-opacity": ".75", rx: 1 });
        r.addEventListener("mousemove", e => f.tip(e.offsetX, e.offsetY, [hd("div", "ct-t", vf(bin.x0) + "–" + vf(bin.x1)), row("count", String(bin.count))]));
        r.addEventListener("mouseleave", () => f.hide());
        svg.append(r);
      }
      for (const t of L.niceTicks(x0, x1, Math.max(2, Math.round(W / 90)))) svg.append(svgText({ x: xs(t), y: H - 4, "text-anchor": "middle", class: "c-ax" }, vf(t)));
      for (const m of o.marks || []) {
        const x = xs(m.value);
        svg.append(s("line", { x1: x, x2: x, y1: Tm, y2: H - Bm, class: "c-mark" }), svgText({ x: x + 3, y: Tm + 9, class: "c-markl" }, m.label || vf(m.value)));
      }
      el.append(svg);
    });
    return { el, update(n2) { o = Object.assign({}, o, n2); f.redraw(); }, destroy: () => f.destroy() };
  }

  /* ---------------- heatmap ---------------- */
  function heatmap(el, opts) {
    let o = opts;
    const f = frame(el, (W) => {
      el.replaceChildren();
      const rows = o.rows || [], cols = o.cols || [], V = o.values || [], vf = o.fmt || (v => L.fmtPrice(v));
      if (!rows.length || !cols.length) { el.append(hd("div", "c-mute", o.emptyText || "no data")); return; }
      const all = V.flat().filter(v => v != null && isFinite(v));
      const div = o.scale === "diverging";
      let dom = o.domain;
      if (!dom) { const e = extent(all) || [0, 1]; dom = div ? [-Math.max(Math.abs(e[0]), Math.abs(e[1])) || 1, 0, Math.max(Math.abs(e[0]), Math.abs(e[1])) || 1] : e; }
      // invert (sequential only): low values get the strong colour, e.g. a price scale where cheap stands out
      const seq = t => colorSeq(o.invert ? 1 - t : t);
      const color = v => {
        if (v == null || !isFinite(v)) return null;
        if (div) { const [a, m, b] = dom.length === 3 ? dom : [dom[0], (dom[0] + dom[1]) / 2, dom[1]]; return colorDiv(v < m ? -(m - v) / ((m - a) || 1) : (v - m) / ((b - m) || 1)); }
        return seq((v - dom[0]) / ((dom[dom.length - 1] - dom[0]) || 1));
      };
      // row label column: fixed (labelWidth) or fitted to the longest label, up to labelMax (260) and 40% of the width
      const CW = 6.3;
      const Lw = o.labelWidth || Math.round(Math.min(o.labelMax || 260, Math.max(120, W * 0.4), Math.max(60, ...rows.map(r => String(r).length * CW + 12)))), Th = 64;
      const cw = Math.max(14, Math.min(64, (W - Lw - 4) / cols.length)), ch = 18;
      const w = Lw + cw * cols.length + 2, H = Th + ch * rows.length + 30;
      const id = "hm" + (++uid);
      const svg = s("svg", { class: "heat", width: w, height: H, viewBox: `0 0 ${w} ${H}`, role: "img", "aria-label": o.label || "heatmap" });
      svg.append(s("defs", {}, s("pattern", { id, width: 5, height: 5, patternUnits: "userSpaceOnUse", patternTransform: "rotate(45)" }, s("rect", { width: 5, height: 5, fill: "#11161c" }), s("line", { x1: 0, y1: 0, x2: 0, y2: 5, class: "c-hatch" }))));
      cols.forEach((c, j) => {
        const t = svgText({ x: 0, y: 0, class: "c-ax", transform: `translate(${Lw + j * cw + cw / 2 + 3},${Th - 4}) rotate(-50)` }, String(c).length > 14 ? String(c).slice(0, 13) + "…" : c);
        if (o.colHref) { t.style.cursor = "pointer"; t.addEventListener("click", () => OG.go(o.colHref(j))); }
        svg.append(t);
      });
      // hover: an outline drawn on top of the neighbours (a stroke on the cell itself is half hidden) + row/col labels lit
      const hov = s("rect", { class: "c-hov", width: cw, height: ch, rx: 2, fill: "none", visibility: "hidden", "pointer-events": "none" });
      const rowLabs = [];
      rows.forEach((r, i) => {
        const shown = fitLabel(r, Lw - 10, CW);
        const t = svgText({ x: Lw - 6, y: Th + i * ch + 12.5, "text-anchor": "end", class: "c-lab" }, shown);
        if (shown !== String(r)) t.append(s("title", {}, String(r)));
        if (o.rowHref) { t.style.cursor = "pointer"; t.addEventListener("click", () => OG.go(o.rowHref(i))); }
        svg.append(t); rowLabs.push(t);
        cols.forEach((c, j) => {
          const v = V[i] ? V[i][j] : null, fill = color(v);
          const rect = s("rect", { x: Lw + j * cw + 1, y: Th + i * ch + 1, width: cw - 2, height: ch - 2, rx: 1.5, fill: fill || `url(#${id})`, class: fill ? "c-cell" : "c-cell null" });
          rect.addEventListener("mouseenter", () => { hov.setAttribute("x", Lw + j * cw); hov.setAttribute("y", Th + i * ch); hov.setAttribute("visibility", "visible"); t.classList.add("on"); });
          rect.addEventListener("mousemove", e => f.tip(e.offsetX, e.offsetY, [hd("div", "ct-t", `${r} · ${c}`), hd("div", "ct-ev", v == null ? (o.nullText || "no data") : vf(v) + (o.units ? " " + o.units : "")),
            o.cellTitle ? hd("div", "ct-d", o.cellTitle(i, j, v) || "") : null]));
          rect.addEventListener("mouseleave", () => { f.hide(); hov.setAttribute("visibility", "hidden"); t.classList.remove("on"); });
          if (o.onCell) { rect.style.cursor = "pointer"; rect.addEventListener("click", () => o.onCell(i, j, v)); }
          svg.append(rect);
        });
      });
      svg.append(hov);
      // legend
      const ly = Th + ch * rows.length + 12, lw = Math.min(200, w - Lw - 10);
      for (let k = 0; k < 40; k++) {
        const t = k / 39;
        svg.append(s("rect", { x: Lw + (k * lw) / 40, y: ly, width: lw / 40 + 0.5, height: 7, fill: div ? colorDiv(t * 2 - 1) : seq(t) }));
      }
      const nullTxt = o.nullText || "no data";
      const over = all.some(v => v > dom[dom.length - 1]), under = all.some(v => v < dom[0]);
      svg.append(svgText({ x: Lw, y: ly + 18, class: "c-ax" }, (under ? "≤ " : "") + vf(dom[0])), svgText({ x: Lw + lw, y: ly + 18, "text-anchor": "end", class: "c-ax" }, (over ? "≥ " : "") + vf(dom[dom.length - 1])),
        s("rect", { x: Lw + lw + 14, y: ly, width: 10, height: 7, fill: `url(#${id})`, class: "c-nullsw" }), svgText({ x: Lw + lw + 28, y: ly + 7, class: "c-ax" }, nullTxt));
      // units slot: what the colour measures, next to the legend
      if (o.units) svg.append(svgText({ x: Lw + lw + 28 + nullTxt.length * CW + 14, y: ly + 7, class: "c-ax c-units" }, o.units));
      if (div && dom.length === 3) svg.append(svgText({ x: Lw + lw / 2, y: ly + 18, "text-anchor": "middle", class: "c-ax" }, vf(dom[1])));
      el.append(svg);
    });
    return { el, update(n2) { o = Object.assign({}, o, n2); f.redraw(); }, destroy: () => f.destroy() };
  }

  /* ---------------- dot plot (provider dispersion) ---------------- */
  function dotplot(el, opts) {
    let o = opts;
    const f = frame(el, (W) => {
      el.replaceChildren();
      const rows = o.rows || [], vf = o.fmt || (v => L.fmtPrice(v));
      if (!rows.length) { el.append(hd("div", "c-mute", o.emptyText || "no data")); return; }
      const single = rows.length === 1 && !rows[0].label;
      // narrow columns: the label column shrinks (labels truncate, full text on hover) before the plot does
      const Lw = single ? 8 : Math.round(Math.min(150, Math.max(54, W * 0.3), Math.max(70, ...rows.map(r => String(r.label).length * 6.4)))), Rm = 14, rh = single ? 34 : 24, Tm = 4, Bm = 18;
      const H = Tm + rows.length * rh + Bm;
      const ext = padExtent(extent(...rows.map(r => r.points.map(p => p.value)), o.axisMin != null ? [o.axisMin] : [], o.axisMax != null ? [o.axisMax] : []), 0.06, o.zero);
      const svg = s("svg", { class: "dots", width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": o.label || "price dispersion" });
      if (!ext) { el.append(hd("div", "c-mute", "no prices")); return; }
      const xs = scale(ext[0], ext[1], Lw, W - Rm);
      // axis labels: drop any that would touch its neighbour or run off either edge
      const ticks = L.niceTicks(ext[0], ext[1], Math.max(2, Math.round((W - Lw) / 80)));
      const tl = ticks.map(t => ({ t, x: xs(t), text: vf(t), w: vf(t).length * 6.2 }));
      const keep = thinLabels(tl, 8);
      tl.forEach((k, i) => {
        svg.append(s("line", { class: "c-grid", x1: k.x, x2: k.x, y1: Tm, y2: H - Bm }));
        if (keep[i] && k.x - k.w / 2 >= (single ? 0 : Lw - 4) && k.x + k.w / 2 <= W + 2) svg.append(svgText({ x: k.x, y: H - 4, "text-anchor": "middle", class: "c-ax" }, k.text));
      });
      rows.forEach((r, i) => {
        const cy = Tm + i * rh + rh / 2;
        if (!single) {
          const shown = fitLabel(r.label, Lw - 12, 6.2);
          const t = svgText({ x: Lw - 8, y: cy + 3.5, "text-anchor": "end", class: "c-lab" }, shown);
          if (shown !== String(r.label)) t.append(s("title", {}, String(r.label)));
          if (r.href) { t.style.cursor = "pointer"; t.addEventListener("click", () => OG.go(r.href)); }
          svg.append(t);
        }
        if (r.low != null && r.high != null) svg.append(s("line", { x1: xs(r.low), x2: xs(r.high), y1: cy, y2: cy, class: "c-range" }));
        if (r.median != null) svg.append(s("line", { x1: xs(r.median), x2: xs(r.median), y1: cy - 7, y2: cy + 7, class: "c-med" }));
        const pts = r.points.filter(p => p.value != null).sort((a, b) => a.value - b.value);
        let lastX = -99, stack = 0;
        for (const p of pts) {
          const x = xs(p.value);
          stack = x - lastX < 5 ? stack + 1 : 0; lastX = x;
          const cyy = cy + (stack % 2 ? 1 : -1) * Math.ceil(stack / 2) * 5;
          const c = s("circle", { cx: x, cy: cyy, r: 4, fill: p.color || "var(--accent)", class: "c-pt" });
          const tipNodes = () => [hd("div", "ct-t", r.label || ""), row(p.label || p.key, vf(p.value), p.color), r.median != null ? row("median", vf(r.median)) : null].filter(Boolean);
          c.addEventListener("mousemove", e => f.tip(e.offsetX, e.offsetY, tipNodes()));
          c.addEventListener("mouseleave", () => f.hide());
          if (p.href) { c.style.cursor = "pointer"; c.addEventListener("click", () => OG.go(p.href)); }
          svg.append(c);
        }
      });
      el.append(svg);
    });
    return { el, update(n2) { o = Object.assign({}, o, n2); f.redraw(); }, destroy: () => f.destroy() };
  }

  return Object.assign(C, { timeseries, sparkline, bars, histogram, heatmap, dotplot });
});
