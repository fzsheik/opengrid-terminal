/* /compare/:a-vs-:b and /compare?a=&b=[&gpu=] — side by side.

Each side resolves to a GPU (canonical slug), a provider, or a region group:
  GPU vs GPU            /v1/compare + /v1/gpus/{a,b} (providers selling both) + /v1/history/{a,b} (overlay)
  provider vs provider  /v1/compare (coverage, relative value, common GPUs) + /market/detail for one common GPU
                        (?gpu= picks it: "the same GPU across providers")
  region vs region      /v1/heatmaps/gpu-region-* ("the same GPU across regions"; ?gpu= highlights one)
Mixed pairs: the API's 400 is shown as is. Different GPUs are different products: side by side, never "equivalent".
*/
(() => {
  const { h, fmt, Lib: L } = OG;
  const REGIONS = ["US", "Canada", "Europe", "UK", "APAC", "Middle East", "LATAM", "Africa"];
  const CA = "#4d94ff", CB = "#f5a524";
  const small = v => (v == null || !isFinite(v) ? "–" : v >= 0.1 ? fmt.price(v) : "$" + Number(v).toPrecision(3));
  const rel = (a, b) => (a == null || b == null || !a ? null : b / a - 1);
  const pctCell = (v, invert) => h("span", { class: v == null || Math.abs(v) < 0.0005 ? "dim" : (v > 0) !== !!invert ? "up" : "down" }, v == null ? "–" : fmt.pct(v));
  const priceDelta = v => h("span", { class: v == null || Math.abs(v) < 0.0005 ? "dim" : v > 0 ? "down" : "up", title: "B relative to A; green = B cheaper" }, v == null ? "–" : fmt.pct(v));
  const WINDOWS = [["7d", "7D", 168], ["30d", "30D", 720], ["90d", "90D", 2160]];

  function resolve(x) {
    if (!x) return null;
    const s = String(x).trim(), sl = s.toLowerCase();
    const name = OG.data.gpuName(sl) || OG.data.gpuName(OG.slug(s));
    if (name) return { type: "gpu", id: OG.slug(name), name, label: OG.shortGpu(name) };
    const p = OG.data.providers().find(p => p.name === sl || OG.slug(p.display_name || "") === sl || (p.display_name || "").toLowerCase() === sl);
    if (p) return { type: "provider", id: p.name, name: p.name, label: OG.providerName(p.name) };
    const r = REGIONS.find(g => OG.slug(g) === OG.slug(s));
    if (r) return { type: "region", id: OG.slug(r), name: r, label: r };
    return { type: "unknown", id: sl, name: s, label: s };
  }
  const pairHref = (a, b, gpu) => `/compare/${a}-vs-${b}` + (gpu ? "?gpu=" + gpu : "");

  function picker(a, b, gpu) {
    const dl = h("datalist", { id: "cmp-opts" },
      (OG.boot.gpus || []).map(([s, n]) => h("option", { value: s }, OG.shortGpu(n))),
      OG.data.providers().map(p => h("option", { value: p.name }, "provider · " + (p.display_name || p.name))),
      REGIONS.map(r => h("option", { value: OG.slug(r) }, "region · " + r)));
    const ia = h("input", { class: "field", list: "cmp-opts", value: a || "", placeholder: "GPU, provider or region", "aria-label": "Left side", style: "width:210px" });
    const ib = h("input", { class: "field", list: "cmp-opts", value: b || "", placeholder: "GPU, provider or region", "aria-label": "Right side", style: "width:210px" });
    const go = () => {
      const ra = resolve(ia.value), rb = resolve(ib.value);
      if (!ra || !rb) return;
      OG.go(pairHref(ra.id, rb.id, gpu && (ra.type !== "gpu") ? gpu : null));
    };
    const keyGo = e => { if (e.key === "Enter") go(); };
    ia.addEventListener("keydown", keyGo); ib.addEventListener("keydown", keyGo);
    const presets = [["h100-80gb-sxm5", "h200-141gb-sxm5", "H100 vs H200"], ["h100-80gb-pcie", "h100-80gb-sxm5", "H100 PCIe vs SXM"], ["a100-80gb-sxm4", "h100-80gb-sxm5", "A100 vs H100"],
      ["b200-180gb-sxm", "h200-141gb-sxm5", "B200 vs H200"], ["runpod", "lambda", "RunPod vs Lambda"], ["lium", "aws", "Lium vs AWS"], ["us", "europe", "US vs Europe"]];
    return h("div", { class: "bar cmp-pick" }, dl, h("span", { class: "flt" }, h("span", {}, "A"), ia),
      h("button", { class: "btn sm", title: "swap", onclick: () => { const t = ia.value; ia.value = ib.value; ib.value = t; go(); } }, "⇄"),
      h("span", { class: "flt" }, h("span", {}, "B"), ib), h("button", { class: "btn pri", onclick: go }, "Compare"),
      h("span", { class: "spacer" }), h("span", { class: "cmp-presets" }, presets.map(([x, y, t]) => h("a", { class: "lnk dim", href: pairHref(x, y) }, t))));
  }

  // Overlay chart. lines: [{key,label,color,dash,points:[{t,v}]}] on a merged time axis
  function overlay(el, lines, opts) {
    const ts = [...new Set(lines.flatMap(l => l.points.map(p => p.t)))].sort((x, y) => new Date(x) - new Date(y));
    if (ts.length < 2) { el.replaceChildren(OG.insufficient(opts.thin || "not enough recorded history to draw a comparison", "No history to overlay")); return null; }
    const series = lines.map(l => { const m = new Map(l.points.map(p => [p.t, p.v])); let last = null;
      return { key: l.key, label: l.label, color: l.color, dash: l.dash, width: l.width || 1.6, values: ts.map(t => { if (m.has(t)) last = m.get(t); return m.has(t) ? m.get(t) : last; }) }; });
    el.textContent = "";
    return OG.charts.timeseries(el, { times: ts, series, height: opts.height || 280, label: opts.label, hatchBefore: opts.hatchBefore });
  }
  async function gpuLines(api, side, win) {
    const hist = await api("/v1/history/" + side.id, { params: { window: win[0] } }).catch(() => null);
    const pts = hist && hist.series ? hist.series.filter(p => p.median != null) : [];
    if (pts.length >= 6) return { hourly: true, coherent: hist.coherent_from, median: pts.map(p => ({ t: p.t, v: p.median })), low: pts.map(p => ({ t: p.t, v: p.lowest })) };
    const d = await api("/market/detail", { params: { gpu: side.name, hours: win[2] } }).catch(() => null);
    if (!d || !d.times) return { hourly: false, median: [], low: [] };
    const pick = arr => d.times.map((t, i) => ({ t, v: arr[i] })).filter(p => p.v != null);
    return { hourly: false, coherent: d.market_from, median: pick(d.median), low: pick(d.lowest) };
  }

  /* ================= GPU vs GPU ================= */
  async function gpuVsGpu(el, A, B, ctx) {
    const body = h("div", {}, OG.loading("Comparing…"));
    el.append(body);
    let c;
    try { c = await ctx.api("/v1/compare", { params: { a: A.id, b: B.id }, full: true }); }
    catch (e) { body.replaceChildren(e.status === 400 ? OG.insufficient(e.message, "These two cannot be compared") : OG.error(e)); return; }
    OG.status.asOf(c.meta && c.meta.as_of);
    const d = c.data, a = d.a, b = d.b;
    const ha = a.hardware || {}, hb = b.hardware || {};
    const ma = a.market, mb = b.market, ca = a.capability || {}, cb = b.capability || {};
    const sa = OG.shortGpu(a.name), sb = OG.shortGpu(b.name);

    // verdicts: only where both sides have the number
    const verdict = (label, va, vb, unit, theoretical) => {
      if (va == null || vb == null) return h("li", {}, h("span", { class: "dim" }, label + ": "), OG.na("one side has no value"), " ", h("span", { class: "dim" }, "(" + [va == null ? sa : null, vb == null ? sb : null].filter(Boolean).join(", ") + " missing)"));
      if (Math.abs(va - vb) / Math.max(va, vb) < 0.005) return h("li", {}, h("span", { class: "dim" }, label + ": "), "about the same (", small(va), " vs ", small(vb), unit, ")");
      const cheaper = va < vb ? sa : sb, r = va < vb ? 1 - va / vb : 1 - vb / va;
      return h("li", {}, h("span", { class: "dim" }, label + ": "), h("b", { style: `color:${va < vb ? CA : CB}` }, cheaper), ` is ${fmt.pct(r).replace("+", "")} cheaper (`, small(va), " vs ", small(vb), unit, ")",
        theoretical ? [" ", OG.badge("theoretical", "warn", "vendor peak spec, dense; not a benchmark")] : null);
    };
    const cap = (side, k) => side[k] && side[k].median;
    const verdicts = h("ul", { class: "cmp-verdicts" },
      verdict("Lowest price now", ma.low, mb.low, "/GPU·h"),
      verdict("Median price now", ma.median, mb.median, "/GPU·h"),
      verdict("Per dense BF16 TFLOP (median)", cap(ca, "per_bf16_tflop_hour"), cap(cb, "per_bf16_tflop_hour"), "/TFLOP·h", true),
      verdict("Per dense FP8 TFLOP (median)", cap(ca, "per_fp8_tflop_hour"), cap(cb, "per_fp8_tflop_hour"), "/TFLOP·h", true),
      verdict("Per GB of VRAM (median)", cap(ca, "per_gb_vram_hour"), cap(cb, "per_gb_vram_hour"), "/GB·h", true),
      verdict("Per TB/s of memory bandwidth (median)", cap(ca, "per_tbps_bandwidth_hour"), cap(cb, "per_tbps_bandwidth_hour"), "/(TB/s)·h", true));

    // side-by-side market table
    const row = (label, va, vb, f, deltaFn, title) => ({ label, va, vb, f: f || (v => v), delta: deltaFn ? deltaFn(va, vb) : null, title });
    const mrows = [
      row("Lowest $/GPU·h", ma.low, mb.low, fmt.price, (x, y) => priceDelta(rel(x, y))),
      row("Median $/GPU·h", ma.median, mb.median, fmt.price, (x, y) => priceDelta(rel(x, y))),
      row("Highest $/GPU·h", ma.high, mb.high, fmt.price, (x, y) => priceDelta(rel(x, y))),
      row("Providers pricing", ma.providers, mb.providers, String, (x, y) => pctCell(rel(x, y))),
      row("Priced listings", a.listings.priced, b.listings.priced, String, (x, y) => pctCell(rel(x, y))),
      row("Available listings", a.listings.available, b.listings.available, String, (x, y) => pctCell(rel(x, y)), "explicit availability"),
      row("Sold-out listings", a.listings.sold_out, b.listings.sold_out, String),
      row("Efficiency score", a.score.efficiency, b.score.efficiency, v => v.toFixed(0) , null, "100 − fragmentation; needs 3+ providers"),
      row("Market label", a.score.label, b.score.label, v => v),
      ...[["per_gb_vram_hour", "$ per GB VRAM·h"], ["per_bf16_tflop_hour", "$ per BF16 TFLOP·h"], ["per_fp16_tflop_hour", "$ per FP16 TFLOP·h"], ["per_fp8_tflop_hour", "$ per FP8 TFLOP·h"], ["per_tbps_bandwidth_hour", "$ per TB/s·h"]]
        .map(([k, l]) => row(l + " (median)", cap(ca, k), cap(cb, k), small, (x, y) => priceDelta(rel(x, y)), "theoretical: median price / vendor peak spec")),
    ];
    const sideTable = (rows, headA, headB) => h("table", { class: "cmp-t" },
      h("thead", {}, h("tr", {}, h("th", {}, ""), h("th", { class: "n", style: `color:${CA}` }, headA), h("th", { class: "n", style: `color:${CB}` }, headB), h("th", { class: "n" }, "B vs A"))),
      h("tbody", {}, rows.map(r => h("tr", { title: r.title || null }, h("th", {}, r.label),
        h("td", { class: "n" }, r.va == null ? OG.na("no value") : r.f(r.va)), h("td", { class: "n" }, r.vb == null ? OG.na("no value") : r.f(r.vb)), h("td", { class: "n" }, r.delta || "")))));

    // spec diff
    const SPEC = [["vendor", "Vendor"], ["architecture", "Architecture"], ["generation", "Generation"], ["released", "Released"], ["segment", "Segment"], ["vram_gb", "VRAM (GB)", 1], ["memory_type", "Memory"],
      ["memory_bandwidth_tbps", "Bandwidth (TB/s)", 1], ["bf16_tflops_dense", "BF16 TFLOPS (dense)", 1], ["fp16_tflops_dense", "FP16 TFLOPS (dense)", 1], ["fp8_tflops_dense", "FP8 TFLOPS (dense)", 1],
      ["fp32_tflops", "FP32 TFLOPS", 1], ["form_factor", "Form factor"], ["interconnect", "Host link"], ["nvlink", "GPU–GPU link"], ["tdp_w", "TDP (W)", 1], ["workload_class", "Workload class"]];
    const specT = h("table", { class: "cmp-t cmp-spec" },
      h("thead", {}, h("tr", {}, h("th", {}, ""), h("th", { class: "n", style: `color:${CA}` }, sa), h("th", { class: "n", style: `color:${CB}` }, sb), h("th", { class: "n" }, "B / A"))),
      h("tbody", {}, SPEC.map(([k, l, num]) => {
        const va = ha[k], vb = hb[k], diff = String(va ?? "") !== String(vb ?? "");
        const f = v => v == null ? OG.na("not published / not recorded") : num ? fmt.num(v, v < 100 && !Number.isInteger(v) ? 2 : 0) : String(v);
        return h("tr", { class: diff ? "diff" : "" }, h("th", {}, l), h("td", { class: "n" }, f(va)), h("td", { class: "n" }, f(vb)),
          h("td", { class: "n" }, num && va && vb ? h("span", { class: vb > va ? "up" : vb < va ? "down" : "dim" }, "×" + (vb / va).toFixed(2)) : diff ? h("span", { class: "dim" }, "differs") : ""));
      })));

    // providers that sell both
    const [ga, gb] = await Promise.all([ctx.api("/v1/gpus/" + a.slug).catch(() => null), ctx.api("/v1/gpus/" + b.slug).catch(() => null)]);
    const pa = new Map((ga ? ga.market.by_provider : []).map(p => [p.provider, p])), pb = new Map((gb ? gb.market.by_provider : []).map(p => [p.provider, p]));
    const provRows = [...new Set([...pa.keys(), ...pb.keys()])].map(p => ({ provider: p, a: pa.has(p) ? pa.get(p).price : null, b: pb.has(p) ? pb.get(p).price : null,
      ra: pa.has(p) ? pa.get(p).rank : null, rb: pb.has(p) ? pb.get(p).rank : null })).map(r => Object.assign(r, { d: rel(r.a, r.b), both: r.a != null && r.b != null }));
    const provT = OG.table({
      columns: [
        { key: "provider", label: "Provider", fmt: v => OG.providerLink(v), value: r => OG.providerName(r.provider) },
        { key: "a", label: sa, num: true, fmt: (v, r) => v == null ? h("span", { class: "dimmer" }, "not sold") : [fmt.price(v), h("span", { class: "dim" }, " #" + r.ra)] },
        { key: "b", label: sb, num: true, fmt: (v, r) => v == null ? h("span", { class: "dimmer" }, "not sold") : [fmt.price(v), h("span", { class: "dim" }, " #" + r.rb)] },
        { key: "d", label: "B vs A", num: true, fmt: v => priceDelta(v) },
      ],
      rows: provRows, sort: { key: "d", dir: "asc" }, compact: true, rowClass: r => r.both ? "" : "out",
      title: `${provRows.filter(r => r.both).length} providers price both`, csv: `opengrid-${a.slug}-vs-${b.slug}-providers.csv`,
    });

    const chartEl = h("div", { class: "cmp-chart" }), chartNote = h("p", { class: "note" });
    let chart = null, win = WINDOWS.find(w => w[0] === OG.qs.get("w", "30d")) || WINDOWS[1];
    const winSeg = OG.seg(WINDOWS.map(w => [w[0], w[1]]), win[0], v => { win = WINDOWS.find(w => w[0] === v); OG.qs.set({ w: v === "30d" ? null : v }); drawChart(); });
    async function drawChart() {
      chartEl.replaceChildren(OG.loading("Loading history…"));
      const w = win;
      const [la, lb] = await Promise.all([gpuLines(ctx.api, A, w), gpuLines(ctx.api, B, w)]);
      if (w !== win) return;
      if (chart) chart.destroy();
      chart = overlay(chartEl, [
        { key: "am", label: sa + " median", color: CA, points: la.median, width: 2 }, { key: "al", label: sa + " lowest", color: CA, dash: "4 3", points: la.low },
        { key: "bm", label: sb + " median", color: CB, points: lb.median, width: 2 }, { key: "bl", label: sb + " lowest", color: CB, dash: "4 3", points: lb.low },
      ], { label: `${sa} vs ${sb} price history`, thin: "Not enough recorded history for either GPU in this window." });
      chartNote.replaceChildren(OG.kindBadge("observed"), (la.hourly && lb.hourly) ? " Hourly cross-section of provider prices (one vote per provider)." : " Fine-grained replay of recorded listings (hourly history is short).",
        " Solid: median of provider lows. Dashed: lowest. A line starts when recording of that GPU began; a provider joining can move it.");
    }
    ctx.onCleanup(() => chart && chart.destroy());

    body.replaceChildren(
      h("div", { class: "cmp-heads" }, sideHead(a.name, "/gpu/" + a.slug, CA, `${ma.providers} providers · ${a.listings.priced} priced listings`, ma.low),
        sideHead(b.name, "/gpu/" + b.slug, CB, `${mb.providers} providers · ${b.listings.priced} priced listings`, mb.low)),
      h("p", { class: "note cmp-caveat" }, d.note, ". Shared dimensions: ", d.shared_dimensions && d.shared_dimensions.length ? d.shared_dimensions.map(x => OG.badge(x.replace(/_/g, " "))) : h("span", { class: "dim" }, "none")),
      h("div", { class: "cmp-grid" },
        h("div", {}, OG.section("Verdicts", verdicts), h("section", { class: "sec" }, h("div", { class: "cmp-sh" }, h("h2", { class: "sec-h" }, "Price history"), h("span", { class: "spacer" }), winSeg), chartEl, chartNote),
          OG.section("Same provider, both GPUs", provT)),
        h("div", {}, OG.section("Market and capability pricing", sideTable(mrows, sa, sb), h("p", { class: "note" }, "B vs A on prices: green means B is cheaper. $ per capability is theoretical (vendor peak, dense).")),
          OG.section("Spec diff", specT, h("p", { class: "note" }, "Highlighted rows differ. Vendor peak figures, dense (no sparsity); not benchmarks. ", h("a", { class: "lnk", href: "/methodology/hardware" }, "Method"))))));
    drawChart();
  }
  function sideHead(name, href, color, sub, low) {
    return h("div", { class: "cmp-side", style: `border-top-color:${color}` },
      h("div", {}, h("a", { class: "lnk cmp-name", href }, OG.shortGpu(name)), h("div", { class: "dim" }, sub)),
      h("div", { class: "cmp-px" }, low != null ? fmt.price(low) : OG.na("no live price"), h("span", { class: "dim" }, low != null ? " low /GPU·h" : "")));
  }

  /* ================= provider vs provider ================= */
  async function providerVsProvider(el, A, B, ctx, query) {
    const body = h("div", {}, OG.loading("Comparing…"));
    el.append(body);
    let c;
    try { c = await ctx.api("/v1/compare", { params: { a: A.id, b: B.id }, full: true }); }
    catch (e) { body.replaceChildren(e.status === 400 ? OG.insufficient(e.message, "These two cannot be compared") : OG.error(e)); return; }
    OG.status.asOf(c.meta && c.meta.as_of);
    const d = c.data, a = d.a, b = d.b, na = OG.providerName(a.name), nb = OG.providerName(b.name);
    const rv = (s, k) => s.relative_value ? s.relative_value[k] : null;
    const rr = (s, k) => s.relative_value && s.relative_value.reasons ? s.relative_value.reasons[k] : "no relative-value data";
    const fh = (s, k) => s.feed_health ? s.feed_health[k] : null;
    const cell = (v, f, reason) => v == null || (Array.isArray(v) && !v.length) ? OG.na(reason || "no value") : f ? f(v) : String(v);
    const rows = [
      ["Class", a.meta.provider_class, b.meta.provider_class], ["Source", (a.meta.source_type || "").replace(/_/g, " ") + " · " + (a.meta.source || ""), (b.meta.source_type || "").replace(/_/g, " ") + " · " + (b.meta.source || "")],
      ["GPUs listed", a.coverage.gpus, b.coverage.gpus], ["Live listings", a.coverage.live_listings, b.coverage.live_listings], ["Priced listings", a.coverage.priced_listings, b.coverage.priced_listings],
      ["Architectures", a.coverage.architectures.join(", "), b.coverage.architectures.join(", ")], ["Region groups", a.coverage.region_groups.join(", "), b.coverage.region_groups.join(", ")],
      ["Avg premium vs market", [rv(a, "premium_avg"), rr(a, "premium_avg")], [rv(b, "premium_avg"), rr(b, "premium_avg")], fmt.pct],
      ["Cheapest-provider share", [rv(a, "cheapest_share"), rr(a, "cheapest_share")], [rv(b, "cheapest_share"), rr(b, "cheapest_share")], v => fmt.pct(v).replace("+", "")],
      ["Availability (hours)", [rv(a, "availability"), rr(a, "availability")], [rv(b, "availability"), rr(b, "availability")], v => fmt.pct(v).replace("+", "")],
      ["Feed failure rate 24h", [fh(a, "failure_rate_24h"), "no feed data"], [fh(b, "failure_rate_24h"), "no feed data"], v => fmt.pct(v).replace("+", "")],
      ["Feed latency 24h", [fh(a, "avg_latency_ms_24h"), "no feed data"], [fh(b, "avg_latency_ms_24h"), "no feed data"], v => fmt.num(v, 0) + " ms"],
      ["Last good fetch", [fh(a, "last_ok_fetch") ? OG.freshBadge(fh(a, "last_ok_fetch"), { provider: a.name }) : null, "never"], [fh(b, "last_ok_fetch") ? OG.freshBadge(fh(b, "last_ok_fetch"), { provider: b.name }) : null, "never"], v => v],
    ];
    const tbl = h("table", { class: "cmp-t" },
      h("thead", {}, h("tr", {}, h("th", {}, ""), h("th", { style: `color:${CA}` }, na), h("th", { style: `color:${CB}` }, nb))),
      h("tbody", {}, rows.map(([l, x, y, f]) => h("tr", {}, h("th", {}, l),
        h("td", {}, Array.isArray(x) ? cell(x[0], f, x[1]) : cell(x)), h("td", {}, Array.isArray(y) ? cell(y[0], f, y[1]) : cell(y))))));
    const common = d.common_gpus.map(g => Object.assign({}, g, { slug: OG.slug(g.gpu), ra: a.prices[g.gpu].rank_now, rb: b.prices[g.gpu].rank_now }));
    const onlyA = Object.keys(a.prices).filter(g => !b.prices[g]), onlyB = Object.keys(b.prices).filter(g => !a.prices[g]);
    const aWins = common.filter(g => g.b_vs_a > 0.0005).length, bWins = common.filter(g => g.b_vs_a < -0.0005).length;
    const med = arr => { const s = arr.slice().sort((x, y) => x - y); return s.length ? (s.length % 2 ? s[(s.length - 1) / 2] : (s[s.length / 2 - 1] + s[s.length / 2]) / 2) : null; };
    const mdiff = med(common.map(g => g.b_vs_a));
    let focus = common.find(g => g.slug === (query.gpu || OG.qs.get("gpu"))) || common[0] || null;
    const chartEl = h("div", { class: "cmp-chart" }), chartNote = h("p", { class: "note" }), chartHead = h("h2", { class: "sec-h" });
    let chart = null, win = WINDOWS.find(w => w[0] === OG.qs.get("w", "7d")) || WINDOWS[0];
    const winSeg = OG.seg(WINDOWS.map(w => [w[0], w[1]]), win[0], v => { win = WINDOWS.find(w => w[0] === v); OG.qs.set({ w: v === "7d" ? null : v }); drawChart(); });
    const commonT = OG.table({
      columns: [
        { key: "gpu", label: "GPU", fmt: v => h("b", {}, OG.shortGpu(v)), value: r => OG.shortGpu(r.gpu) },
        { key: "a", label: na, num: true, fmt: (v, r) => [fmt.price(v), h("span", { class: "dim" }, " #" + r.ra)] },
        { key: "b", label: nb, num: true, fmt: (v, r) => [fmt.price(v), h("span", { class: "dim" }, " #" + r.rb)] },
        { key: "b_vs_a", label: "B vs A", num: true, fmt: v => priceDelta(v) },
        { key: "lnk", label: "", sort: false, csv: false, fmt: (v, r) => h("a", { class: "lnk dim", href: "/gpu/" + r.slug }, "market →") },
      ],
      rows: common, sort: { key: "b_vs_a", dir: "asc" }, compact: true, rowKey: r => r.slug,
      onRow: r => { focus = r; OG.qs.set({ gpu: r.slug }); commonT.highlight(r.slug); drawChart(); },
      title: "Common GPUs · click one to chart it", empty: "These providers have no GPU in common right now.", csv: `opengrid-${A.id}-vs-${B.id}.csv`,
    });
    async function drawChart() {
      if (!focus) { chartHead.textContent = "Same GPU across both providers"; chartEl.replaceChildren(OG.empty("No common GPU to chart.")); return; }
      chartHead.textContent = OG.shortGpu(focus.gpu) + " at both providers";
      commonT.highlight(focus.slug);
      chartEl.replaceChildren(OG.loading());
      const w = win, f = focus;
      let det;
      try { det = await ctx.api("/market/detail", { params: { gpu: f.gpu, hours: w[2] }, slot: "cmp-pp" }); } catch (e) { if (!e.stale) chartEl.replaceChildren(OG.error(e)); return; }
      if (w !== win || f !== focus) return;
      const line = (p, color, label) => { const s = det.providers.find(x => x.provider === p); return { key: p, label, color, width: 2, points: s ? det.times.map((t, i) => ({ t, v: s.series[i] })).filter(x => x.v != null) : [] }; };
      if (chart) chart.destroy();
      chart = overlay(chartEl, [line(a.name, CA, na), line(b.name, CB, nb), { key: "mkt", label: "market lowest", color: "#7d8895", dash: "2 3", width: 1, points: det.times.map((t, i) => ({ t, v: det.lowest[i] })).filter(x => x.v != null) }],
        { label: "provider price history", height: 240 });
      chartNote.replaceChildren(OG.kindBadge("observed"), " Each provider's lowest eligible price for this GPU; step-held between recorded changes. Dotted: market lowest across all providers.");
    }
    ctx.onCleanup(() => chart && chart.destroy());
    body.replaceChildren(
      h("div", { class: "cmp-heads" }, provHead(a, CA), provHead(b, CB)),
      OG.stats([
        { label: "Common GPUs", value: String(common.length), sub: `${onlyA.length} only ${na} · ${onlyB.length} only ${nb}` },
        { label: `${na} cheaper on`, value: common.length ? `${aWins} of ${common.length}` : null, reason: "no common GPU" },
        { label: `${nb} cheaper on`, value: common.length ? `${bWins} of ${common.length}` : null, reason: "no common GPU" },
        { label: "Median B vs A", value: mdiff != null ? fmt.pct(mdiff) : null, reason: "no common GPU", sub: mdiff == null ? null : mdiff < 0 ? nb + " cheaper" : mdiff > 0 ? na + " cheaper" : "same", kind: "inferred" },
      ]),
      h("div", { class: "cmp-grid" },
        h("div", {}, OG.section(null, commonT), h("section", { class: "sec" }, h("div", { class: "cmp-sh" }, chartHead, h("span", { class: "spacer" }), winSeg), chartEl, chartNote)),
        h("div", {}, OG.section("Side by side", tbl, h("p", { class: "note" }, "Relative value needs at least 24 recorded hours; until then it shows the reason. ", h("a", { class: "lnk", href: "/methodology/provider-value" }, "Method"))),
          onlyA.length || onlyB.length ? OG.section("Only one sells", h("table", { class: "cmp-t" }, h("tbody", {},
            h("tr", {}, h("th", { style: `color:${CA}` }, "only " + na), h("td", { class: "wrap" }, onlyA.map(g => OG.gpuLink(g)).flatMap((x, i) => i ? [", ", x] : [x]))),
            h("tr", {}, h("th", { style: `color:${CB}` }, "only " + nb), h("td", { class: "wrap" }, onlyB.map(g => OG.gpuLink(g)).flatMap((x, i) => i ? [", ", x] : [x])))))) : null)));
    drawChart();
  }
  function provHead(s, color) {
    return h("div", { class: "cmp-side", style: `border-top-color:${color}` },
      h("div", {}, h("a", { class: "lnk cmp-name prov", href: "/provider/" + s.name }, OG.logo(s.name, 16), " ", OG.providerName(s.name)),
        h("div", { class: "dim" }, [s.meta.provider_class, s.meta.note].filter(Boolean).join(" · "))),
      h("div", { class: "cmp-px" }, String(s.coverage.gpus), h("span", { class: "dim" }, " GPUs · " + s.coverage.priced_listings + " priced")));
  }

  /* ================= region vs region ================= */
  async function regionVsRegion(el, A, B, ctx, query) {
    const body = h("div", {}, OG.loading("Comparing regions…"));
    el.append(body);
    const [ch, pr, av] = await Promise.all(["gpu-region-cheapest", "gpu-region-premium", "gpu-region-availability"].map(k => ctx.api("/v1/heatmaps/" + k).catch(e => e)));
    if (ch instanceof Error) { body.replaceChildren(OG.insufficient(ch.message, "Regional data unavailable")); return; }
    const ia = ch.cols.indexOf(A.name), ib = ch.cols.indexOf(B.name);
    const val = (mx, i, j) => (mx instanceof Error || !mx || j < 0 ? null : (mx.cells[mx.rows.indexOf(ch.rows[i])] || [])[j] ?? null);
    const focusSlug = query.gpu || OG.qs.get("gpu");
    const rows = ch.rows.map((g, i) => ({ gpu: g, slug: OG.slug(g), a: ia < 0 ? null : ch.cells[i][ia], b: ib < 0 ? null : ch.cells[i][ib],
      pa: val(pr, i, ia), pb: val(pr, i, ib), la: val(av, i, ia), lb: val(av, i, ib) })).filter(r => r.a != null || r.b != null)
      .map(r => Object.assign(r, { d: rel(r.a, r.b), both: r.a != null && r.b != null }));
    const both = rows.filter(r => r.both), aw = both.filter(r => r.d > 0.0005).length, bw = both.filter(r => r.d < -0.0005).length;
    const focus = rows.find(r => r.slug === focusSlug);
    body.replaceChildren(
      h("div", { class: "cmp-heads" }, regHead(A, CA, ia < 0, rows.filter(r => r.a != null).length), regHead(B, CB, ib < 0, rows.filter(r => r.b != null).length)),
      focus ? h("div", { class: "cmp-focus" }, h("b", {}, OG.shortGpu(focus.gpu)), ": cheapest ", h("span", { style: `color:${CA}` }, A.label), " ", focus.a != null ? fmt.price(focus.a) : OG.na("not listed there"),
        " · ", h("span", { style: `color:${CB}` }, B.label), " ", focus.b != null ? fmt.price(focus.b) : OG.na("not listed there"), focus.both ? [" · B vs A ", priceDelta(focus.d)] : null,
        " ", h("a", { class: "lnk dim", href: "/gpu/" + focus.slug + "#gp-regions" }, "all regions →")) : null,
      OG.stats([{ label: "GPUs in both", value: String(both.length) }, { label: A.label + " cheaper on", value: both.length ? `${aw} of ${both.length}` : null, reason: "no GPU listed in both" },
        { label: B.label + " cheaper on", value: both.length ? `${bw} of ${both.length}` : null, reason: "no GPU listed in both" }]),
      OG.table({
        columns: [
          { key: "gpu", label: "GPU", fmt: v => h("b", {}, OG.shortGpu(v)), href: r => "/gpu/" + r.slug, value: r => OG.shortGpu(r.gpu) },
          { key: "a", label: A.label + " cheapest", num: true, fmt: v => v == null ? h("span", { class: "dimmer" }, "not listed") : fmt.price(v) },
          { key: "b", label: B.label + " cheapest", num: true, fmt: v => v == null ? h("span", { class: "dimmer" }, "not listed") : fmt.price(v) },
          { key: "d", label: "B vs A", num: true, fmt: v => priceDelta(v) },
          { key: "pa", label: A.label + " vs global", num: true, fmt: v => pctCell(v, true), title: "regional median of provider lows / global median − 1" },
          { key: "pb", label: B.label + " vs global", num: true, fmt: v => pctCell(v, true) },
          { key: "la", label: A.label + " listings", num: true }, { key: "lb", label: B.label + " listings", num: true },
        ],
        rows, sort: { key: "d", dir: "asc" }, compact: true, rowKey: r => r.slug, rowClass: r => (r.slug === focusSlug ? "hl" : r.both ? "" : "out"),
        title: "Same GPU across two region groups", csv: `opengrid-${A.id}-vs-${B.id}.csv`,
      }),
      h("p", { class: "note" }, OG.kindBadge("observed"), " Region groups come from each listing's stated location, never guessed; listings without a location are not in either group. ", h("a", { class: "lnk", href: "/heatmaps" }, "Regional heatmaps →")));
  }
  function regHead(R, color, missing, n) {
    return h("div", { class: "cmp-side", style: `border-top-color:${color}` }, h("div", {}, h("span", { class: "cmp-name" }, R.label), h("div", { class: "dim" }, "region group")),
      h("div", { class: "cmp-px" }, missing ? OG.na("no current listing in this group") : String(n), h("span", { class: "dim" }, missing ? "" : " GPUs priced")));
  }

  /* ================= page ================= */
  async function mount(el, a, b, query, ctx) {
    el.classList.add("cmpx");
    ctx.onCleanup(() => el.classList.remove("cmpx"));
    const A = resolve(a), B = resolve(b);
    const title = A && B ? `${A.label} vs ${B.label}` : "Compare";
    ctx.setTitle(title);
    el.append(OG.head(title, A && B ? (A.type === "gpu" && B.type === "gpu" ? "GPU vs GPU: price, availability, capability pricing and specs, side by side"
      : A.type === "provider" && B.type === "provider" ? "Provider vs provider: coverage, pricing on common GPUs, relative value and feed health"
      : A.type === "region" && B.type === "region" ? "Region vs region: the cheapest price of each GPU in two region groups" : "Pick two GPUs, two providers or two regions")
      : "Two GPUs (H100 vs H200), two providers (RunPod vs Lambda), or two regions (US vs Europe)"), picker(A && A.id, B && B.id, query.gpu));
    if (!A || !B) {
      el.append(OG.section("Popular comparisons", h("div", { class: "cmp-start" },
        [["h100-80gb-sxm5", "h200-141gb-sxm5"], ["h100-80gb-pcie", "h100-80gb-sxm5"], ["a100-80gb-sxm4", "h100-80gb-sxm5"], ["h200-141gb-sxm5", "b200-180gb-sxm"], ["l40s-48gb", "a100-80gb-pcie"], ["rtx-4090-24gb", "rtx-5090-32gb"]]
          .filter(([x, y]) => OG.data.gpuName(x) && OG.data.gpuName(y)).map(([x, y]) => h("a", { class: "btn", href: pairHref(x, y) }, OG.shortGpu(OG.data.gpuName(x)) + " vs " + OG.shortGpu(OG.data.gpuName(y)))))),
        h("p", { class: "note" }, "Or type ", h("kbd", {}, "c h100 h200"), " in the command bar."));
      return;
    }
    const bad = [A, B].filter(x => x.type === "unknown");
    if (bad.length) { el.append(OG.insufficient(bad.map(x => `"${x.name}"`).join(" and ") + " is not a canonical GPU slug, a provider or a region group.", "Unknown")); return; }
    if (A.type === "gpu" && B.type === "gpu") return gpuVsGpu(el, A, B, ctx);
    if (A.type === "provider" && B.type === "provider") return providerVsProvider(el, A, B, ctx, query);
    if (A.type === "region" && B.type === "region") return regionVsRegion(el, A, B, ctx, query);
    // mixed: ask the API so the message is the API's own (regions are not API compare targets)
    if (A.type !== "region" && B.type !== "region") {
      try { await ctx.api("/v1/compare", { params: { a: A.id, b: B.id } }); }
      catch (e) { el.append(OG.insufficient(e.message, "These two cannot be compared")); }
    } else el.append(OG.insufficient(`${A.label} is a ${A.type} and ${B.label} is a ${B.type}: compare two GPUs, two providers or two region groups.`, "These two cannot be compared"));
    el.append(h("p", { class: "note" }, "Try ", h("a", { class: "lnk", href: pairHref(A.type === "gpu" ? A.id : "h100-80gb-sxm5", B.type === "gpu" ? B.id : "h200-141gb-sxm5") }, "two GPUs"),
      " or ", h("a", { class: "lnk", href: pairHref(A.type === "provider" ? A.id : "lium", B.type === "provider" ? B.id : "aws") }, "two providers"), "."));
  }
  OG.page("/compare/:a-vs-:b", { title: p => `${p.a} vs ${p.b}`, nav: "compare", mount: (el, p, q, ctx) => mount(el, p.a, p.b, q, ctx) });
  OG.page("/compare", { title: "Compare", mount: (el, p, q, ctx) => mount(el, q.a || null, q.b || null, q, ctx) });
})();
