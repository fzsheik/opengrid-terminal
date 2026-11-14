/* /reports, /reports/:month: the INTERNAL monthly market report (reports/monthly.py, methodology/monthly-report).
   Reads /v1/reports/monthly (list) and /v1/reports/monthly/{month} (data:read; private, never public pages).
   Tables first: how the clouds moved per featured GPU, biggest movers, within-cloud proportionality, generations,
   supply, notable events, coverage. "Copy public summary" copies the only part meant to leave OpenGrid.
   An admin also gets "Regenerate" (POST .../regenerate). */
(() => {
  const { h, fmt } = OG;
  const PATTERN = {
    no_moves: ["no moves", "no cloud moved its price"],
    proportional: ["proportional", "every cloud within 2 points of the median move"],
    few_movers: ["few movers", "fewer than half of the clouds moved"],
    broad_uneven: ["broad, uneven", "half or more moved, by different amounts"],
  };
  const CLASS = { uniform: "uniform", same_direction: "same direction", mixed: "mixed" };
  const pct = v => (v == null ? OG.na() : OG.chg(v));
  const pp = v => (v == null ? "-" : fmt.num(v, 1) + " pp");
  const price = v => (v == null ? "-" : fmt.price(v));
  const range = r => (r ? fmt.pct(r[0]) + " … " + fmt.pct(r[1]) : "-");

  function mountReport(el, params, ctx) {
    const root = h("div", { class: "pg-reports" });
    el.append(root);
    const picker = h("select", { class: "field", "aria-label": "Month", onchange: e => OG.go("/reports/" + e.target.value) });
    const actions = h("span", { class: "rp-act" });
    root.append(OG.head("Monthly market report",
      "Internal. How GPU clouds moved their prices over one calendar month (UTC). It names providers next to their prices: share only the public summary.",
      picker, actions, h("a", { class: "btn", href: "/methodology/monthly-report" }, "Methodology")));
    const body = h("div", {}, OG.loading("Loading the report…"));
    root.append(body);

    (async () => {
      const me = await OG.me();
      if (!ctx.alive()) return;
      if (!me) {
        body.replaceChildren(h("section", { class: "cx-access" }, OG.accessNote("account", "The monthly report is internal market analysis."),
          h("div", {}, h("a", { class: "btn", href: "/gpus" }, "Browse GPU prices"))));
        return;
      }
      let list;
      try { list = await ctx.api("/v1/reports/monthly", { params: { limit: 1000 } }); }
      catch (e) { body.replaceChildren(OG.error(e)); return; }
      const month = params.month || (list[0] && list[0].month);
      picker.replaceChildren(...list.map(r => h("option", { value: r.month, selected: r.month === month ? "" : null }, r.label + (r.basis && r.basis !== "live" ? " · " + r.basis : ""))));
      if (month && !list.some(r => r.month === month)) picker.prepend(h("option", { value: month, selected: "" }, month + " (not generated)"));
      if (OG.isAdmin(me) && month) actions.replaceChildren(h("button", { class: "btn", type: "button", title: "Generate this month again from stored data (generated_at is kept)", onclick: async ev => {
        ev.target.disabled = true;
        try { await ctx.api(`/v1/reports/monthly/${month}/regenerate`, { method: "POST" }); load(month, true); }
        catch (e) { body.prepend(OG.error(e)); }
        ev.target.disabled = false;
      } }, "Regenerate"));
      if (!month) { body.replaceChildren(OG.empty("No monthly report yet. The monthly_report job writes the previous month after the 1st.")); return; }
      load(month);
    })();

    async function load(month, nocache) {
      body.replaceChildren(OG.loading("Loading the report…"));
      let row;
      try { row = await ctx.api(`/v1/reports/monthly/${month}`, { nocache: !!nocache }); }
      catch (e) { body.replaceChildren(OG.error(e)); return; }
      if (!ctx.alive()) return;
      body.replaceChildren(...draw(row));
    }
  }

  function draw(row) {
    const r = row.report, cov = r.coverage, ci = r.headline.compute_index;
    const out = [];
    // public-safe summary first: the one block meant to be pasted outside OpenGrid
    out.push(OG.section("Public-safe summary",
      h("div", { class: "box box-p rp-sum" }, h("ul", {}, row.public_summary.split("\n").filter(l => l.startsWith("- ")).map(l => h("li", {}, l.slice(2)))),
        h("p", { class: "note" }, row.public_summary.split("\n").filter(l => l.startsWith("Methodology:")).join(" ")),
        h("div", {}, OG.copy(row.public_summary, { label: "Copy public summary", cls: "", title: "Rounded headline numbers, no provider names: safe for a blog post" }))),
      h("p", { class: "note" }, `Generated ${fmt.dateTime(row.generated_at)}` + (row.regenerated_at ? `, regenerated ${fmt.dateTime(row.regenerated_at)}` : "") +
        ` · version ${row.version} · basis ${cov.basis}` + (row.posted_at ? " · posted to #opengrid-market" : ""))));

    out.push(OG.stats([
      { label: "GPU Compute Index", value: ci.end == null ? null : fmt.num(ci.end, 1), change: ci.change == null ? undefined : ci.change, reason: ci.reason, sub: ci.start == null ? null : "from " + fmt.num(ci.start, 1) },
      { label: "Clouds counted", value: fmt.num(cov.providers_counted), sub: cov.pairs + " cloud × GPU prices" },
      { label: "Basis", value: cov.basis, sub: cov.pairs_live + " live · " + cov.pairs_archive + " archived" },
      { label: "Notable events", value: fmt.num(r.events.notable_total), sub: r.events.by_severity.major + " major" },
    ]));

    out.push(OG.section("How the clouds moved", OG.table({
      csv: `opengrid-report-${r.month}-gpus.csv`, compact: true, rows: r.featured,
      columns: [
        { key: "short", label: "GPU", href: x => "/gpu/" + x.slug },
        { key: "index_change", label: "Index", num: true, value: x => x.index.change, fmt: (v, x) => (x.index.change == null ? OG.na(x.index.reason) : OG.chg(x.index.change)), title: "OpenGrid index, month start to end" },
        { key: "clouds", label: "Clouds", num: true },
        { key: "up", label: "Up", num: true }, { key: "flat", label: "Flat", num: true }, { key: "down", label: "Down", num: true },
        { key: "median_move", label: "Median", num: true, fmt: (v, x) => (v == null ? OG.na(x.reasons.median_move) : pct(v)) },
        { key: "middle_half", label: "Middle half", num: true, fmt: range, sort: false },
        { key: "range", label: "Full range", num: true, fmt: range, sort: false },
        { key: "mad", label: "Dispersion", num: true, value: x => x.dispersion.mad_pp, fmt: (v, x) => pp(x.dispersion.mad_pp), title: "median absolute deviation of the moves from the median move, in percentage points" },
        { key: "pattern", label: "Pattern", fmt: v => (v ? h("span", { title: PATTERN[v][1] }, PATTERN[v][0]) : "-") },
        { key: "cheapest", label: "Cheapest at end", value: x => x.cheapest_end && x.cheapest_end.price, fmt: (v, x) => (x.cheapest_end ? [OG.providerLink(x.cheapest_end.provider), " ", price(x.cheapest_end.price)] : "-") },
        { key: "cheapest_below_median", label: "vs median", num: true, fmt: (v, x) => (v == null ? OG.na(x.reasons.spread) : "−" + fmt.num(v * 100, 0) + "%"), title: "how far the cheapest cloud sits below the median cloud at month end" },
      ] })));

    const movers = [];
    for (const f of r.featured) for (const m of f.biggest_movers) movers.push(Object.assign({ gpu: f.short, slug: f.slug }, m));
    out.push(OG.section("Biggest movers", movers.length ? OG.table({
      csv: `opengrid-report-${r.month}-movers.csv`, compact: true, rows: movers,
      columns: [
        { key: "gpu", label: "GPU", href: x => "/gpu/" + x.slug },
        { key: "provider", label: "Provider", fmt: v => OG.providerLink(v) },
        { key: "from", label: "Start", num: true, fmt: price }, { key: "to", label: "End", num: true, fmt: price },
        { key: "change", label: "Change", num: true, fmt: pct },
        { key: "basis", label: "Basis" },
      ] }) : OG.empty("No cloud moved a featured GPU's price by the threshold this month.")));

    const wp = r.within_provider;
    out.push(OG.section("Within one cloud: did its GPUs move together?", wp.providers_repricing_2plus ? [
      h("p", { class: "note" }, `${wp.providers_repricing_2plus} clouds repriced two or more GPUs: ${wp.counts.uniform} uniform (all within ${wp.band_pp} pp), ` +
        `${wp.counts.same_direction} same direction by different amounts, ${wp.counts.mixed} mixed.`),
      OG.table({ csv: `opengrid-report-${r.month}-within.csv`, compact: true, rows: wp.providers, columns: [
        { key: "provider", label: "Provider", fmt: v => OG.providerLink(v) },
        { key: "class", label: "Class", fmt: v => CLASS[v] || v },
        { key: "gpus_repriced", label: "Repriced", num: true, fmt: (v, x) => v + " of " + x.gpus_tracked },
        { key: "spread_pp", label: "Spread", num: true, fmt: pp },
        { key: "moves", label: "Moves", sort: false, value: x => x.moves.map(m => OG.shortGpu(m.gpu) + " " + fmt.pct(m.change)).join("; "),
          fmt: (v, x) => x.moves.map(m => OG.shortGpu(m.gpu) + " " + fmt.pct(m.change)).join(" · ") },
      ] })] : OG.insufficient(wp.reason, "No multi-GPU repricing")));

    out.push(OG.section("By generation", r.generations.length ? OG.table({ compact: true, rows: r.generations, columns: [
      { key: "generation", label: "Generation" },
      { key: "gpus", label: "GPUs", sort: false, fmt: v => v.map(g => g.short).join(", ") },
      { key: "clouds", label: "Clouds", num: true },
      { key: "up", label: "Up", num: true }, { key: "flat", label: "Flat", num: true }, { key: "down", label: "Down", num: true },
      { key: "median_gpu_move", label: "Median GPU move", num: true, fmt: pct },
    ] }) : OG.insufficient(`No GPU had at least ${cov.thresholds.min_clouds} clouds priced at both ends of the month.`)));

    out.push(OG.section("Supply", r.supply.reason ? OG.insufficient(r.supply.reason, "No supply data") : OG.table({ compact: true, rows: r.supply.gpus, columns: [
      { key: "short", label: "GPU" },
      { key: "sold_out", label: "Sold out", num: true, title: "a cloud's SKU / region went from in stock (or unknown) to sold out" },
      { key: "back_in_stock", label: "Back in stock", num: true },
      { key: "providers_listed", label: "Started listing", sort: false, fmt: v => (v.length ? v.map(p => OG.providerName(p)).join(", ") : "-") },
      { key: "providers_delisted", label: "Stopped listing", sort: false, fmt: v => (v.length ? v.map(p => OG.providerName(p)).join(", ") : "-") },
    ] })));

    const ev = r.events;
    out.push(OG.section(`Notable events (${ev.notable_total})`, ev.notable.length ? OG.table({ compact: true, rows: ev.notable, columns: [
      { key: "occurred_at", label: "When", fmt: v => fmt.dateTime(v) },
      { key: "severity", label: "Severity" },
      { key: "title", label: "Event", ell: 520 },
      { key: "pct", label: "Change", num: true, fmt: v => (v == null ? "-" : pct(v)) },
    ] }) : OG.insufficient(ev.reason || "No notable or major events this month.", "No notable events")));

    out.push(OG.section("Coverage and caveats", h("div", { class: "box box-p" },
      h("p", { class: "note" }, `Basis: ${cov.basis} (${cov.pairs_live} live, ${cov.pairs_archive} archived cloud × GPU prices) · ${cov.providers_counted} clouds counted · data kinds: ${cov.data_kinds.join(", ")} · ` +
        `move threshold ${fmt.num(cov.thresholds.move * 100, 0)}% · live recording since ${cov.live_recording_start ? fmt.date(cov.live_recording_start) + " " + new Date(cov.live_recording_start).getFullYear() : "-"}`),
      cov.providers_excluded_class.length ? h("p", { class: "note" }, "Left out (marketplace / decentralized): " + cov.providers_excluded_class.map(p => OG.providerName(p)).join(", ")) : null,
      cov.reason ? OG.insufficient(cov.reason) : null,
      h("ul", { class: "note" }, cov.caveats.map(c => h("li", {}, c))))));
    return out;
  }

  OG.page("/reports", { title: "Monthly report", mount(el, params, query, ctx) { mountReport(el, params, ctx); } });
  OG.page("/reports/:month", { title: "Monthly report", nav: "reports", mount(el, params, query, ctx) { mountReport(el, params, ctx); } });
})();
