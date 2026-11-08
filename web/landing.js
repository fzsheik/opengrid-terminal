/* The homepage at "/" (web/landing.html). Everything shown is live, read-only, public data:
   1. The grid: GPU x cloud, each square a cloud's lowest listed price vs the other clouds' (GET
      /internal/heatmaps/gpu-provider-premium). Hover or focus shows the price (GET /v1/markets/{gpu}, by_provider).
      The find box filters the rows; Enter opens the GPU's market page.
   2. The spread sentence and the index table: GET /v1/markets (low/median/high now) and /v1/indices?kind=gpu.
   3. The composite (GPU Compute Index) with its history, and the H100 SXM index's constituents for the worked example.
   4. What moved: GET /v1/events. 5. API key: POST /v1/instant-key, kept for this tab in sessionStorage. */
(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const HDR = { "X-OpenGrid-Request": "1", "Accept": "application/json" };
  const KEY_STORE = "og-instant-key";
  const HERO_GPU = "h100-80gb-sxm5";          // the GPU the spread sentence and the worked example use
  const FEATURED = ["h100-80gb-sxm5", "h200-141gb-sxm5", "b200-180gb-sxm", "b300-288gb-sxm", "a100-80gb-sxm4",
    "h100-80gb-pcie", "l40s-48gb", "mi300x-192gb"];
  const ROWS = 20;                            // GPUs in the grid
  const SVGNS = "http://www.w3.org/2000/svg";

  // ------------------------------------------------------------------ helpers
  function el(tag, attrs, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === "class") n.className = v;
      else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else n.setAttribute(k, v === true ? "" : v);
    }
    for (const k of kids.flat()) if (k != null && k !== false) n.append(k instanceof Node ? k : String(k));
    return n;
  }
  function sv(tag, attrs, ...kids) {
    const n = document.createElementNS(SVGNS, tag);
    for (const [k, v] of Object.entries(attrs || {})) if (v != null) n.setAttribute(k, v);
    for (const k of kids) if (k != null) n.append(k instanceof Node ? k : String(k));
    return n;
  }
  const num = x => (x == null || !isFinite(x)) ? null : Number(x);
  const usd = p => num(p) == null ? "-" : "$" + Number(p).toFixed(p >= 0.1 ? 2 : 3);
  const pct = (f, d = 1) => num(f) == null ? "-" : (f > 0 ? "+" : f < 0 ? "−" : "") + Math.abs(f * 100).toFixed(d) + "%";
  const shortGpu = name => String(name || "").replace(/^(NVIDIA|AMD Instinct|AMD|Intel)\s+/i, "");
  const slugOf = gpu => String(gpu || "").replace(/^(nvidia|amd|intel)-/, "");
  const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
  const dateFmt = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
  const timeFmt = new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit", timeZone: "UTC", hour12: false });
  const dayFmt = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", timeZone: "UTC" });
  function ago(iso) {
    const s = (Date.now() - Date.parse(iso)) / 1000;
    if (!isFinite(s)) return null;
    if (s < 90) return "just now";
    const m = Math.round(s / 60);
    if (m < 90) return m + " min ago";
    const h = Math.round(m / 60);
    return h < 36 ? h + " h ago" : Math.round(h / 24) + " days ago";
  }
  function errText(body, fallback) {
    return (body && (body.detail || (body.error && body.error.message) || body.message)) || fallback;
  }
  async function getJSON(url, opts) {
    const r = await fetch(url, Object.assign({ credentials: "same-origin", headers: { Accept: "application/json" } }, opts));
    const body = await r.json().catch(() => ({}));
    if (!r.ok) {
      const e = new Error(typeof errText(body) === "string" ? errText(body) : "Something went wrong. Try again.");
      e.status = r.status; e.retryAfter = r.headers.get("Retry-After");
      throw e;
    }
    return body;
  }
  const soft = p => p.catch(() => null);      // a section without data hides; the page never breaks

  // ------------------------------------------------------------------ data
  const load = {
    heat: soft(getJSON("/internal/heatmaps/gpu-provider-premium")),
    markets: soft(getJSON("/v1/markets?limit=200")),
    indices: soft(getJSON("/v1/indices?kind=gpu&limit=100")),
    composite: soft(getJSON("/v1/indices/gpu-compute")),
    compHist: soft(getJSON("/v1/indices/gpu-compute/history?window=all&interval=1d")),
    providers: soft(getJSON("/v1/providers?limit=200")),
    gpus: soft(getJSON("/v1/gpus?limit=1")),
  };

  // ------------------------------------------------------------------ 1. the grid
  const grid = $("grid"), tip = $("tip"), board = tip.parentElement, scroller = $("board-scroll");
  const rowsByName = new Map();               // gpu name -> {tr, slug, market}
  const byProviderCache = new Map();          // market slug -> Promise<Map(provider -> by_provider row)>
  let provName = id => id;

  function band(p) {
    if (p == null) return "co";
    if (p <= -0.25) return "c4";
    if (p <= -0.08) return "c3";
    if (p < 0.08) return "c2";
    if (p < 0.6) return "c1";
    return "c0";
  }
  function premiumText(p) {
    if (p == null) return "Not listed";
    if (Math.abs(p) < 0.03) return "About the same as the other clouds";
    if (p < 0) return `${Math.round(-p * 100)}% below the other clouds' median`;
    if (p >= 1) return `${(1 + p).toFixed(1)}× the other clouds' median`;
    return `${Math.round(p * 100)}% above the other clouds' median`;
  }

  function buildGrid(heat, markets) {
    const mk = new Map((markets || []).map(m => [m.gpu_name, m]));
    const filled = r => r.filter(x => x != null).length;
    let rows = heat.rows.map((name, i) => ({ name, i, n: filled(heat.cells[i]), m: mk.get(name) }))
      .filter(r => r.n >= 3)
      .sort((a, b) => b.n - a.n || a.name.localeCompare(b.name))
      .slice(0, ROWS);
    // order rows by family so like sits with like (H100 SXM by H100 PCIe), most-listed family first
    rows.sort((a, b) => b.n - a.n);
    const cols = heat.cols.map((id, j) => ({ id, j, n: rows.filter(r => heat.cells[r.i][j] != null).length }))
      .filter(c => c.n > 0)
      .sort((a, b) => b.n - a.n || provName(a.id).localeCompare(provName(b.id)));

    const head = el("tr", {}, el("th", { scope: "col" }, el("span", { class: "sr" }, "GPU")));
    cols.forEach((c, ci) => head.append(el("th", { scope: "col", "data-col": c.id, "data-ci": ci }, el("span", { title: provName(c.id) }, provName(c.id)))));
    const tbody = el("tbody");
    rows.forEach((r, ri) => {
      const slug = r.m ? slugOf(r.m.gpu) : null;
      const label = shortGpu(r.name);
      const tr = el("tr", { "data-gpu": r.name },
        el("th", { scope: "row" }, slug ? el("a", { href: "/gpu/" + slug }, label) : label));
      cols.forEach((c, ci) => {
        const p = heat.cells[r.i][c.j];
        const td = el("td", { class: band(p), "data-p": p == null ? "" : p, "data-col": c.id, "data-ci": ci, style: `--col:${ci};--row:${ri}` },
          el("i"), el("span", { class: "sr" }, `${provName(c.id)}: ${premiumText(p)}`));
        tr.append(td);
      });
      tbody.append(tr);
      rowsByName.set(r.name, { tr, slug, label, market: r.m, mslug: r.m ? r.m.gpu : null });
    });
    grid.append(el("thead", {}, head), tbody);
    fitCells(cols.length);
    $("grid-wait").remove();
    if (!still) {
      grid.classList.add("boot");
      requestAnimationFrame(() => requestAnimationFrame(() => {
        grid.classList.add("lit");
        grid.classList.remove("boot");
        setTimeout(() => grid.classList.add("done"), 1600);
      }));
    }
    return { rows, cols };
  }

  let shownCols = 0;
  function fitCells(n) {
    const fit = () => {
      grid.style.removeProperty("--cell");
      grid.querySelectorAll(".xc").forEach(x => x.classList.remove("xc"));
      const label = grid.querySelector("tbody th");
      const lw = label ? label.getBoundingClientRect().width : 140;
      const gap = parseFloat(getComputedStyle(grid).borderSpacing) || 3;
      const room = scroller.clientWidth - lw - gap * 2;
      let cell = Math.floor(room / n - gap), show = n;
      if (cell < 13) { cell = 13; show = Math.max(6, Math.floor(room / (cell + gap))); }
      grid.style.setProperty("--cell", Math.min(24, cell) + "px");
      if (show < n) grid.querySelectorAll("[data-ci]").forEach(x => { if (+x.dataset.ci >= show) x.classList.add("xc"); });
      shownCols = show;
      if (asofText) asofText();
    };
    fit();
    let t;
    addEventListener("resize", () => { clearTimeout(t); t = setTimeout(fit, 120); });
  }

  function byProvider(mslug) {
    if (!byProviderCache.has(mslug)) {
      byProviderCache.set(mslug, getJSON("/v1/markets/" + encodeURIComponent(mslug))
        .then(b => new Map(((b.data && b.data.by_provider) || []).map(x => [x.provider, x])))
        .catch(() => new Map()));
    }
    return byProviderCache.get(mslug);
  }

  let asofText = null;
  let tipFor = null;
  function placeTip(anchor) {
    const b = board.getBoundingClientRect(), a = anchor.getBoundingClientRect();
    tip.hidden = false;
    const tw = tip.offsetWidth, th = tip.offsetHeight;
    let x = a.left - b.left + a.width / 2 - tw / 2;
    x = Math.max(0, Math.min(x, b.width - tw));
    let y = a.top - b.top - th - 10;
    if (y < 0) y = a.bottom - b.top + 10;
    tip.style.left = x + "px";
    tip.style.top = y + "px";
  }
  function clearHot() {
    grid.querySelectorAll(".hot, .on").forEach(n => n.classList.remove("hot", "on"));
  }
  function showCell(td) {
    const tr = td.parentElement, gpu = tr.dataset.gpu, info = rowsByName.get(gpu), prov = td.dataset.col;
    clearHot();
    tr.classList.add("hot");
    td.classList.add("on");
    const colHead = grid.querySelector(`thead th[data-col="${CSS.escape(prov)}"]`);
    if (colHead) colHead.classList.add("hot");
    const p = td.dataset.p === "" ? null : Number(td.dataset.p);
    tipFor = td;
    const priceLine = el("span", { class: "tp" + (p != null && p < -0.08 ? " lo" : "") }, p == null ? "" : "…");
    tip.replaceChildren(...[el("b", {}, `${provName(prov)} · ${info.label}`), p == null ? null : priceLine, premiumText(p)].filter(Boolean));
    placeTip(td);
    if (p != null && info.mslug) {
      byProvider(info.mslug).then(m => {
        if (tipFor !== td) return;
        const row = m.get(prov);
        priceLine.textContent = row ? `${usd(row.price)}/GPU-hr` : "";
        if (!row) priceLine.remove();
        placeTip(td);
      });
    }
  }
  function showRow(tr) {
    const info = rowsByName.get(tr.dataset.gpu), m = info.market;
    clearHot();
    tr.classList.add("hot");
    tipFor = tr;
    tip.replaceChildren(...[el("b", {}, info.label),
      m ? el("span", { class: "tp" }, `${usd(m.price.low)} – ${usd(m.price.high)}`) : null,
      m ? `Listed now at ${plural(m.providers, "cloud")}. Median ${usd(m.price.median)}/GPU-hr.` : "Open its market"].filter(Boolean));
    placeTip(tr.querySelector("th a") || tr.querySelector("th"));
  }
  function hideTip() { tipFor = null; tip.hidden = true; clearHot(); }

  grid.addEventListener("pointerover", e => {
    const td = e.target.closest("td");
    if (td && grid.contains(td)) showCell(td);
    else {
      const th = e.target.closest("tbody th");
      if (th) showRow(th.parentElement);
    }
  });
  grid.addEventListener("pointerleave", hideTip);
  grid.addEventListener("focusin", e => { const tr = e.target.closest("tbody tr"); if (tr) showRow(tr); });
  grid.addEventListener("focusout", hideTip);
  scroller.addEventListener("scroll", () => { if (!tip.hidden) hideTip(); }, { passive: true });

  // find a GPU: filter the grid as you type; Enter opens the first match's market
  const findQ = $("find-q"), findMsg = $("find-msg");
  const norm = s => String(s || "").toLowerCase().replace(/[^a-z0-9]/g, "");
  function matches() {
    const q = norm(findQ.value);
    const out = [];
    rowsByName.forEach((info, name) => { if (!q || norm(name).includes(q)) out.push(info); });
    return { q, out };
  }
  findQ.addEventListener("input", () => {
    const { q, out } = matches();
    rowsByName.forEach(info => info.tr.classList.toggle("dim", !!q && !out.includes(info)));
    if (!q) { findMsg.textContent = ""; return; }
    if (out.length) {
      findMsg.replaceChildren(`${plural(out.length, "match")} in the grid. Press Enter to open `, el("b", {}, out[0].label), ".");
    } else {
      findMsg.replaceChildren("Not in this grid. ", el("a", { href: "/gpus?q=" + encodeURIComponent(findQ.value.trim()) }, "Search all 100 GPUs"));
    }
  });
  $("find").addEventListener("submit", e => {
    e.preventDefault();
    const { q, out } = matches();
    if (!q) { findQ.focus(); return; }
    const hit = out.find(i => i.slug);
    location.href = hit ? "/gpu/" + hit.slug : "/gpus?q=" + encodeURIComponent(findQ.value.trim());
  });

  // ------------------------------------------------------------------ 2. spread sentence + index table
  function spread(market, idx) {
    if (!market || market.price.low == null) return;
    const s = $("spread");
    const ratio = market.price.high / market.price.low;
    const k = Math.max(1, Math.min(2.1, 1 + (ratio - 1) / 3.5));   // the high price is set larger, by how much larger it is
    const hi = el("span", { class: "hi" }, usd(market.price.high));
    hi.style.fontSize = k.toFixed(2) + "em";
    s.replaceChildren(...[
      `Right now one ${shortGpu(market.gpu_name)} is listed from`,
      el("span", { class: "big" }, el("span", { class: "lo" }, usd(market.price.low)), el("span", { class: "to" }, "to"), hi),
      `an hour, ${isFinite(ratio) && ratio >= 1.5 ? `${ratio.toFixed(1)}\u00a0times apart, ` : ""}on ${plural(market.providers, "cloud")} with live\u00a0listings.`,
      idx && idx.level != null ? el("small", {}, `OpenGrid's ${shortGpu(idx.gpu_name)} index: `, el("b", {}, usd(idx.level) + "/GPU-hr"),
        ` from ${plural(idx.constituents, "cloud")}, ${dateFmt.format(new Date(idx.hour))} ${timeFmt.format(new Date(idx.hour))} UTC.`) : null].filter(Boolean));
    s.hidden = false;
    $("lede").textContent = "The same GPU costs very different amounts on different clouds. OpenGrid reads every listed price and puts the whole market on one grid.";
  }

  function spark(values) {
    const v = (values || []).map(num).filter(x => x != null);
    if (v.length < 2) return null;
    const lo = Math.min(...v), hi = Math.max(...v), span = hi - lo || 1;
    const d = v.map((y, i) => `${i ? "L" : "M"}${(i / (v.length - 1) * 120).toFixed(1)},${(26 - (y - lo) / span * 24).toFixed(1)}`).join("");
    return sv("svg", { viewBox: "0 0 120 28", preserveAspectRatio: "none", "aria-hidden": "true" }, sv("path", { d }));
  }

  let outside = 0;
  function rangeCell(r, m) {
    if (!m || m.price.low == null) return "-";
    const lo = m.price.low, hi = m.price.high;
    const out = r.level < lo * 0.995 || r.level > hi * 1.005;
    if (out) outside++;
    const body = Math.abs(hi - lo) < 0.005 ? [usd(lo), el("span", { class: "sub-i" }, " one cloud")] : [el("span", { class: "lo" }, usd(lo)), " – ", usd(hi)];
    return out ? [...body, el("sup", { class: "mk", title: "Index outside today's listed range: see the note under the table" }, "*")] : body;
  }

  function indexTable(indices, markets) {
    const mk = new Map((markets || []).map(m => [slugOf(m.gpu), m]));
    const pub = indices.filter(r => r.published && r.level != null);
    const pick = FEATURED.map(id => pub.find(r => r.id === id)).filter(Boolean);
    for (const r of pub.sort((a, b) => b.constituents - a.constituents)) if (pick.length < 8 && !pick.includes(r)) pick.push(r);
    const tb = $("itbl-b");
    tb.replaceChildren(...pick.map(r => {
      const m = mk.get(r.id), ch = num(r.changes && r.changes["7d"]);
      return el("tr", {},
        el("td", {}, el("a", { href: "/indices/" + r.id }, shortGpu(r.gpu_name))),
        el("td", { class: "n px" }, usd(r.level)),
        el("td", { class: "n" + (ch != null && ch < 0 ? " dn" : "") }, ch == null ? el("span", { class: "sub", title: "Under 7 days of index history" }, "new") : pct(ch)),
        el("td", { class: "n wide" }, String(r.constituents)),
        el("td", { class: "n rng wide" }, rangeCell(r, m)),
        el("td", { class: "spk" }, spark(r.spark_7d)));
    }));
    if (outside) $("itbl-note").hidden = false;
  }

  // ------------------------------------------------------------------ 3. composite + worked example
  function composite(c, hist) {
    if (!c || c.level == null) return;
    $("comp-level").textContent = Number(c.level).toFixed(2);
    const all = num(c.changes && c.changes.all && (c.changes.all.pct != null ? c.changes.all.pct : c.changes.all));
    const wk = c.changes && c.changes["7d"];
    const w = num(wk && (wk.pct != null ? wk.pct : wk));
    $("comp-chg").textContent = w != null ? `${pct(w)} this week` : "";
    const since = c.coverage && c.coverage.first_published;
    const names = (c.components || []).map(x => shortGpu(x.name.replace(/^OpenGrid\s+/, "").replace(/\s+Index$/, "")).replace(/\s+\d+GB.*$/, ""));
    $("comp-note").textContent = `Base 100 on ${since ? dateFmt.format(new Date(since)) : "the first hour"}${all != null ? `, ${pct(all)} since` : ""}. ` +
      (names.length ? `An equal-weight blend of the ${names.slice(0, -1).join(", ")} and ${names.slice(-1)} indices.` : "");
    $("composite").hidden = false;
    const pts = ((hist && hist.points) || []).map(p => num(p.level)).filter(x => x != null);
    const svg = $("comp-chart");
    if (pts.length > 1) {
      const lo = Math.min(100, ...pts) - 1, hi = Math.max(100, ...pts) + 1;
      const raw = hist.points.filter(p => num(p.level) != null);
      const W = Math.max(300, Math.round(svg.getBoundingClientRect().width)) || 600, H = 170, top = 14, bot = 26, right = 64;
      const y = v => top + (1 - (v - lo) / (hi - lo)) * (H - top - bot);
      const x = i => i / (pts.length - 1) * (W - right);
      const d = pts.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");
      svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
      svg.setAttribute("aria-label", `GPU Compute Index, daily, from ${pts[0].toFixed(1)} to ${pts[pts.length - 1].toFixed(1)}`);
      const last = pts[pts.length - 1];
      const ticks = [0, Math.floor((pts.length - 1) / 2), pts.length - 1].map(i => sv("text", { class: "dt", x: x(i), y: H - 6, "text-anchor": i === 0 ? "start" : i === pts.length - 1 ? "end" : "middle" }, dayFmt.format(new Date(raw[i].t))));
      svg.replaceChildren(
        sv("line", { class: "base", x1: 0, x2: W - right, y1: y(100), y2: y(100) }),
        sv("text", { class: "bl", x: W - right + 8, y: y(100) + 4 }, "100 base"),
        sv("path", { class: "ar", d: `${d}L${x(pts.length - 1)},${H - bot}L0,${H - bot}Z` }),
        sv("path", { class: "ln", d }),
        sv("rect", { class: "end", x: x(pts.length - 1) - 4, y: y(last) - 4, width: 8, height: 8 }),
        sv("text", { class: "ev", x: W - right + 8, y: y(last) + 4 }, last.toFixed(2)),
        ...ticks);
    }
    $("composite").hidden = false;
  }

  function workedExample(d) {
    const cs = (d.constituents_now && d.constituents_now.constituents) || [];
    if (cs.length < 3 || d.level == null) return;
    const inc = cs.filter(c => c.status === "included").map(c => c.price).sort((a, b) => a - b);
    const k = inc.length >= 5 ? Math.floor(inc.length * 0.2) : 0;
    const keep = inc.slice(k, inc.length - k);
    const lowCut = inc[k - 1], highCut = inc[inc.length - k];
    const mean = d.raw_level != null ? Number(d.raw_level) : keep.reduce((a, b) => a + b, 0) / keep.length;
    const svg = $("strip-svg");
    $("strip").hidden = false;
    const W = Math.max(320, Math.round(svg.getBoundingClientRect().width)) || 1000, H = 150, padL = 12, padR = 12, base = 104;
    const max = Math.max(...cs.map(c => c.price), d.level) * 1.04;
    const x = v => padL + v / max * (W - padL - padR);
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.setAttribute("aria-label", `${cs.length} clouds' H100 SXM prices from ${usd(inc[0])} to ${usd(inc[inc.length - 1])}; this hour's trimmed average ${usd(mean)}; published index ${usd(d.level)}.`);
    const parts = [];
    if (k) parts.push(sv("rect", { class: "trim", x: x(keep[0]), y: 20, width: x(keep[keep.length - 1]) - x(keep[0]), height: base - 20 + 10 }));
    parts.push(sv("line", { class: "ax", x1: padL, x2: W - padR, y1: base + 12, y2: base + 12 }));
    const step = max > 12 ? (W < 600 ? 4 : 2) : (W < 600 ? 2 : 1);
    for (let t = 0; t <= max; t += step) {
      parts.push(sv("line", { class: "ax", x1: x(t), x2: x(t), y1: base + 12, y2: base + 17 }));
      parts.push(sv("text", { x: x(t), y: base + 32, "text-anchor": "middle" }, "$" + t));
    }
    // stack dots that would overlap
    const placed = [];
    cs.slice().sort((a, b) => a.price - b.price).forEach(c => {
      const cx = x(c.price);
      let level = 0;
      while (placed.some(p => p.level === level && Math.abs(p.cx - cx) < 12)) level++;
      placed.push({ cx, level });
      const cy = base - level * 13;
      const cls = c.status !== "included" ? "out" : (k && (c.price < keep[0] || c.price > keep[keep.length - 1])) ? "cut" : "in";
      const dot = sv("rect", { class: cls, x: cx - 5, y: cy - 5, width: 10, height: 10 });
      dot.append(sv("title", {}, `${c.provider_name}: ${usd(c.price)}/GPU-hr${cls === "cut" ? " (trimmed)" : cls === "out" ? " (held out)" : ""}`));
      parts.push(dot);
    });
    parts.push(sv("line", { class: "lvl", x1: x(mean), x2: x(mean), y1: 8, y2: base + 12, "stroke-dasharray": "4 4" }));
    parts.push(sv("text", { class: "lvl-t", x: x(mean) + 8, y: 18 }, W < 600 ? `Average ${usd(mean)}` : `This hour's trimmed average ${usd(mean)}`));
    parts.push(sv("line", { class: "lvl", x1: x(d.level), x2: x(d.level), y1: 26, y2: base + 12 }));
    parts.push(sv("text", { class: "lvl-t", x: x(d.level) - 8, y: 36, "text-anchor": "end" }, `Index ${usd(d.level)}`));
    svg.replaceChildren(...parts);
    const key = (cls, text) => el("span", { class: "k" }, (() => { const s = sv("svg", { viewBox: "0 0 12 12", "aria-hidden": "true" }); s.append(sv("rect", { class: cls, x: 1.5, y: 1.5, width: 9, height: 9 })); return s; })(), text);
    const cap = $("strip-cap");
    cap.replaceChildren(...[
      key("in", `Averaged (${keep.length})`),
      k ? key("cut", `Trimmed, top and bottom fifth (${inc.length - keep.length})`) : null,
      cs.some(c => c.status !== "included") ? key("out", "Held out") : null,
      el("span", {}, `${plural(cs.length, "cloud")}, ${dateFmt.format(new Date(d.constituents_now.hour))} ${timeFmt.format(new Date(d.constituents_now.hour))} UTC.`)].filter(Boolean));
    $("how-num").textContent = usd(d.level);
    $("strip").hidden = false;
  }

  // ------------------------------------------------------------------ 4. what moved
  const TYPE = {
    price_move: e => (e.pct != null && e.pct < 0) ? ["Price cut", true] : ["Price rise", false],
    new_cheapest_provider: () => ["New cheapest", true],
    capacity_returned: () => ["Back in stock", false],
    sold_out: () => ["Sold out", false],
    new_low_30d: () => ["30-day low", true], new_all_time_low: () => ["All-time low", true],
    new_high_30d: () => ["30-day high", false], new_all_time_high: () => ["All-time high", false],
    spread_anomaly: () => ["Wide spread", false], regional_dislocation: () => ["Regional gap", false],
  };
  function moves(events, gridNames) {
    const want = new Set(gridNames);
    const seen = new Set();
    const pick = [];
    for (const e of events) {
      if (!want.has(e.gpu_name) || !TYPE[e.type]) continue;
      const k = e.gpu + e.type + e.provider;
      if (seen.has(k)) continue;
      seen.add(k);
      pick.push(e);
      if (pick.length === 6) break;
    }
    if (!pick.length) return;
    $("moves").replaceChildren(...pick.map(e => {
      const [label, good] = TYPE[e.type](e);
      const t = new Date(e.occurred_at);
      const same = new Date().toDateString() === t.toDateString();
      return el("li", {},
        el("time", { datetime: e.occurred_at }, same ? timeFmt.format(t) + " UTC" : dayFmt.format(t) + " " + timeFmt.format(t)),
        el("span", { class: "ty" + (good ? " lo" : "") }, label),
        el("span", { class: "tt" }, e.title));
    }));
    $("tape-sec").hidden = false;
  }

  // ------------------------------------------------------------------ run
  (async () => {
    const [heat, markets, indices, comp, compHist, providers, gpus] = await Promise.all([
      load.heat, load.markets, load.indices, load.composite, load.compHist, load.providers, load.gpus]);
    const mrows = markets ? markets.data : [];
    if (providers && providers.data) {
      const names = new Map(providers.data.map(p => [p.provider, p.provider_name || p.provider]));
      provName = id => names.get(id) || id;
    }
    const nProv = providers && providers.meta && providers.meta.total;
    const nGpu = gpus && gpus.meta && gpus.meta.total;
    const since = comp && comp.data && comp.data.coverage && comp.data.coverage.first_published;
    if (nProv && nGpu) {
      $("facts").replaceChildren("Tracking ", el("b", {}, String(nProv)), " GPU clouds and ", el("b", {}, String(nGpu)),
        " exact GPU models", ...(since ? [", hour by hour since ", el("b", {}, dateFmt.format(new Date(since)))] : []),
        ". An H100 SXM and an H100 PCIe are different products here, so their prices are never averaged together.");
    }

    let built = null;
    if (heat && heat.data && heat.data.rows && heat.data.rows.length) {
      built = buildGrid(heat.data, mrows);
      const asof = markets && markets.meta && markets.meta.as_of;
      asofText = () => {
        const n = built.cols.length, part = shownCols && shownCols < n ? `${shownCols} of ${n} clouds (the most-listed)` : `${n} clouds`;
        $("asof").textContent = `${built.rows.length} most-listed GPUs × ${part}` + (asof ? ` · listed prices at ${timeFmt.format(new Date(asof))} UTC` : "");
      };
      asofText();
    } else {
      $("grid-wait").textContent = "The market grid is unavailable right now. Open the markets for every price.";
    }

    const idxRows = indices ? indices.data : [];
    const heroMarket = mrows.find(m => slugOf(m.gpu) === HERO_GPU);
    const heroIdx = idxRows.find(r => r.id === HERO_GPU);
    spread(heroMarket, heroIdx);
    if (idxRows.length) indexTable(idxRows, mrows);
    else $("itbl").closest(".tbl-wrap").hidden = true;
    composite(comp && comp.data, compHist && compHist.data);

    const later = () => {
      soft(getJSON("/v1/indices/" + HERO_GPU)).then(b => b && b.data && workedExample(b.data));
      if (built) soft(getJSON("/v1/events?min_severity=notable&limit=100")).then(b => b && moves(b.data || [], built.rows.map(r => r.name)));
    };
    if ("IntersectionObserver" in window) {
      const io = new IntersectionObserver(es => { if (es.some(e => e.isIntersecting)) { io.disconnect(); later(); } }, { rootMargin: "600px" });
      io.observe($("facts"));
    } else later();
  })();

  // ------------------------------------------------------------------ 5. API key + code
  const gen = $("gen"), err = $("key-err"), ask = $("key-ask"), out = $("key-out");
  const secretEl = $("secret"), copyBtn = $("copy"), saved = $("key-saved"), liveMsg = $("live");
  let secret = null;

  function codeFor(key) {
    const k = key || "$OPENGRID_API_KEY", o = location.origin;
    const auth = key ? `"Authorization: Bearer ${k}"` : `"Authorization: Bearer $OPENGRID_API_KEY"`;
    $("pane-curl").replaceChildren(el("span", { class: "c" }, "# Every cloud's H100 SXM price, cheapest first\n"),
      `curl ${o}/v1/best/${HERO_GPU} \\\n  -H ${auth}\n\n`,
      el("span", { class: "c" }, "# Today's GPU indices\n"),
      `curl "${o}/v1/indices?kind=gpu" \\\n  -H ${auth}`);
    $("pane-mcp").replaceChildren(el("span", { class: "c" }, "# Give Claude Code the market as a tool (read-only)\n"),
      `claude mcp add --transport http opengrid \\\n  ${o}/mcp \\\n  --header ${auth}\n\n`,
      el("span", { class: "c" }, "# Then ask it: "), el("span", { class: "k" }, "\"cheapest H100 that's in stock?\""));
    $("pane-py").replaceChildren(`import requests\n\n`,
      `r = requests.get(\n    "${o}/v1/best/${HERO_GPU}",\n    headers={"Authorization": "Bearer ${key || "<your key>"}"},\n)\n`,
      `for c in r.json()["data"]["candidates"][:5]:\n    print(c["provider"], c["price_per_gpu_hour"])`);
  }
  codeFor(null);

  const tabs = Array.from(document.querySelectorAll('.tabs [role="tab"]'));
  function selectTab(t, focus) {
    tabs.forEach(b => {
      const on = b === t;
      b.setAttribute("aria-selected", on ? "true" : "false");
      b.tabIndex = on ? 0 : -1;
      $(b.getAttribute("aria-controls")).hidden = !on;
    });
    if (focus) t.focus();
  }
  tabs.forEach((t, i) => {
    t.addEventListener("click", () => selectTab(t));
    t.addEventListener("keydown", e => {
      const d = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
      if (d) { e.preventDefault(); selectTab(tabs[(i + d + tabs.length) % tabs.length], true); }
    });
  });

  function selectKey() {
    const sel = getSelection(), range = document.createRange();
    range.selectNodeContents(secretEl);
    sel.removeAllRanges();
    sel.addRange(range);
  }
  async function copy() {
    try { await navigator.clipboard.writeText(secret); return true; } catch (_) { selectKey(); return false; }
  }
  function savedLine(copied) {
    saved.replaceChildren(copied ? el("b", {}, "Copied to clipboard.") : "Select the key above and copy it.",
      " It can't be shown again, so keep it somewhere safe. The examples on the right now use it.");
  }
  function showKey(s, { fresh }) {
    secret = s;
    secretEl.textContent = s;
    ask.hidden = true;
    out.hidden = false;
    codeFor(s);
    $("code-note").textContent = "Paste any of these into a terminal. The key reads market data only.";
    if (fresh) {
      copy().then(ok => {
        savedLine(ok);
        liveMsg.textContent = ok ? "Your API key is ready and copied to the clipboard." : "Your API key is ready. Select it and copy it.";
      });
      copyBtn.focus({ preventScroll: true });
    } else {
      saved.textContent = "Your key from earlier in this tab. Once you close the tab it can't be shown again.";
    }
  }
  function hours(sec) {
    const h = Math.max(1, Math.ceil(Number(sec) / 3600));
    return h === 1 ? "1 hour" : h + " hours";
  }
  async function generate() {
    if (gen.getAttribute("aria-busy") === "true" || gen.disabled) return;
    gen.setAttribute("aria-busy", "true");
    gen.textContent = "Generating…";
    err.hidden = true;
    let done = false;
    try {
      const b = await getJSON("/v1/instant-key", { method: "POST", headers: HDR });
      const d = b && b.data;
      if (!d || !d.secret) throw new Error("Couldn't make a key right now. Try again.");
      try { sessionStorage.setItem(KEY_STORE, d.secret); } catch (_) { /* storage blocked: the key still shows */ }
      showKey(d.secret, { fresh: true });
    } catch (e) {
      const limited = e.status === 429 || e.status === 503;
      err.replaceChildren(e.message || "Couldn't make a key right now. Try again.",
        ...(limited ? [" ", el("a", { href: "/markets" }, "Browse every price without a key")] : []));
      err.hidden = false;
      if (limited) {
        done = true;
        gen.disabled = true;
        gen.textContent = e.status === 429 && e.retryAfter ? `Try again in ${hours(e.retryAfter)}` : "No keys left today";
      }
    } finally {
      gen.removeAttribute("aria-busy");
      if (!done) gen.textContent = "Generate API key";
    }
  }
  gen.addEventListener("click", generate);
  copyBtn.addEventListener("click", async () => {
    const ok = await copy();
    copyBtn.textContent = ok ? "Copied" : "Selected";
    savedLine(ok);
    liveMsg.textContent = ok ? "Copied." : "Key selected. Press Command or Control C to copy.";
    clearTimeout(copyBtn.t);
    copyBtn.t = setTimeout(() => { copyBtn.textContent = "Copy"; }, 1600);
  });
  // "Get API key" links land on the key box with focus on its button
  document.querySelectorAll('a[href="#key"]').forEach(a => a.addEventListener("click", () => {
    setTimeout(() => (out.hidden ? gen : copyBtn).focus({ preventScroll: true }), still ? 0 : 450);
  }));
  try {
    const kept = sessionStorage.getItem(KEY_STORE);
    if (kept) showKey(kept, { fresh: false });
  } catch (_) { /* storage blocked */ }
})();
