/* /admin/value — is OpenGrid creating value? Tables, not decoration, and honest about thin samples.
   Economic value (GET /v1/economics): per deployment market median vs selected vs savings, or "no valid
   comparison: <reason>"; totals and by GPU / provider / strategy. Routing quality (/v1/admin/quality),
   reliability with sample sizes (/v1/admin/reliability: n and "insufficient sample" as the API says),
   the activation funnel (/v1/admin/funnel) and product usage (/v1/admin/product). */
(() => {
  const { h, fmt } = OG;
  const pct = v => (v == null ? null : fmt.num(v, 2) + "%");
  // {value|mean, n, status} metric -> cell
  const metric = (m, f) => {
    if (!m) return OG.na("not reported");
    const v = m.value ?? m.mean ?? null;
    if (v == null) return h("span", { class: "dim", title: m.status || "" }, OG.na(m.status || "no data"), m.n != null ? h("span", { class: "v-n" }, " n=" + m.n) : null);
    return h("span", {}, f ? f(v) : String(v), m.n != null ? h("span", { class: "v-n" }, " n=" + m.n) : null);
  };
  const share = v => fmt.num(v * 100, 1) + "%";

  OG.page("/admin/value", {
    title: "Value",
    nav: "admin-value",
    async mount(el, params, query, ctx) {
      const root = h("div", { class: "pg-val" });
      el.append(root);
      const me = await OG.me();
      if (!ctx.alive()) return;
      if (!OG.isAdmin(me)) { root.append(OG.head("Value"), OG.error({ status: 403, message: "admin scope required" })); return; }
      const days = ["7", "30", "90", "3650"].includes(query.days) ? query.days : "30";
      const B = { econ: h("div", {}, OG.loading()), qual: h("div", {}, OG.loading()), rel: h("div", {}, OG.loading()), fun: h("div", {}, OG.loading()), prod: h("div", {}, OG.loading()) };
      root.append(OG.head("Value", "Savings, routing quality, reliability, funnel · real transactions only",
        OG.kindBadge("transaction"), OG.seg([["7", "7D"], ["30", "30D"], ["90", "90D"], ["3650", "ALL"]], days, v => { OG.qs.set({ days: v }); OG.render(); })),
        OG.section("Economic value vs the market median", B.econ),
        OG.section("Routing quality", B.qual),
        OG.section("Provider reliability (OpenGrid's own launches)", B.rel),
        h("div", { class: "cols-2" }, OG.section("Activation funnel", B.fun), OG.section("Product usage", B.prod)));
      const groupTable = (obj, label) => OG.table({
        columns: [{ key: "k", label }, { key: "n", label: "n", num: true }, { key: "total_savings_usd", label: "Savings $", num: true, fmt: v => OG.money(v) },
          { key: "mean_savings_pct", label: "Mean %", num: true, fmt: v => pct(v) }, { key: "median_savings_pct", label: "Median %", num: true, fmt: v => pct(v) }],
        rows: Object.entries(obj || {}).map(([k, v]) => Object.assign({ k: label === "GPU" ? OG.shortGpu(k) : label === "Provider" ? OG.providerName(k) : k }, v)), compact: true, csv: false, empty: "No valid comparisons yet." });

      ctx.api("/v1/economics", { params: { days: days === "3650" ? null : days }, nocache: true }).then(e => {
        const t = e.totals || {};
        B.econ.replaceChildren(
          OG.stats([
            { label: "Deployments", value: fmt.num(t.deployments) },
            { label: "With valid comparison", value: fmt.num(t.with_valid_comparison), sub: "same GPU, on-demand, median of ≥ 3 providers" },
            { label: "Customer savings", value: t.total_customer_savings_usd != null ? OG.money(t.total_customer_savings_usd) : null, reason: t.status, kind: "transaction" },
            { label: "Mean savings", value: pct(t.mean_savings_pct), reason: t.status },
            { label: "Median savings", value: pct(t.median_savings_pct), reason: t.status },
          ]),
          h("div", { class: "cols-3" }, groupTable(e.by_gpu, "GPU"), groupTable(e.by_provider, "Provider"), groupTable(e.by_strategy, "Strategy")),
          Object.keys(e.excluded_no_valid_comparison || {}).length ? h("p", { class: "note" }, "Excluded (no valid comparison): ", Object.entries(e.excluded_no_valid_comparison).map(([k, n]) => `${k} × ${n}`).join(" · ")) : null,
          OG.table({
            title: "Per deployment",
            columns: [
              { key: "deployment_id", label: "Deployment", cls: "mono", href: r => "/deployments/" + r.deployment_id },
              { key: "account_id", label: "Acct", num: true },
              { key: "gpu", label: "GPU", fmt: v => OG.shortGpu(v || "") }, { key: "provider", label: "Provider", fmt: v => OG.providerName(v) }, { key: "strategy", label: "Strategy", cls: "dim" },
              { key: "market_median_per_gpu_hour", label: "Market median", num: true, fmt: (v, r) => v != null ? h("span", { title: `${r.market_median_kind} · ${r.market_providers} providers` }, fmt.price(v)) : "–" },
              { key: "selected_price_per_gpu_hour", label: "Selected", num: true, fmt: (v, r) => v != null ? h("span", { title: "basis: " + r.selected_price_basis }, fmt.price(v), " ", OG.badge(r.selected_price_basis === "quote" ? "quote" : "exec")) : "–" },
              { key: "gpu_hours", label: "GPU-h", num: true, fmt: v => v != null ? fmt.num(v, 2) : "–" },
              { key: "savings_pct", label: "Savings %", num: true, fmt: (v, r) => v != null ? h("span", { class: v >= 0 ? "up" : "down" }, pct(v)) : h("span", { class: "dim" }, r.comparison || "no valid comparison") },
              { key: "savings_usd", label: "Savings $", num: true, fmt: v => v != null ? OG.money(v) : "–" },
            ], rows: e.deployments || [], compact: true, csv: "opengrid-economics.csv", empty: "No deployments in this window.",
          }));
      }, err => B.econ.replaceChildren(OG.error(err)));

      ctx.api("/v1/admin/quality", { params: { days: days === "3650" ? null : days, limit: 200 }, nocache: true }).then(q => {
        const s = q.summary || {};
        const qa = s.quote_accuracy || {};
        B.qual.replaceChildren(
          h("table", { class: "dep-kv val-kv" },
            [["Route requests", `${s.route_requests ?? 0} (${s.previews ?? 0} previews · ${s.routes ?? 0} routes)`],
              ["Routing success", metric(s.routing_success_rate, share), s.routing_success_rate && s.routing_success_rate.definition],
              ["Provisioning success", metric(s.provisioning_success_rate, share), s.provisioning_success_rate && s.provisioning_success_rate.definition],
              ["Expected savings vs median", metric(s.expected_savings_vs_median_pct, v => pct(v)), s.expected_savings_vs_median_pct && s.expected_savings_vs_median_pct.basis],
              ["Realized savings vs median", metric(s.realized_savings_vs_median_pct, v => pct(v)), s.realized_savings_vs_median_pct && s.realized_savings_vs_median_pct.basis],
              ["Savings vs previous provider", metric(s.savings_vs_previous_provider_pct, v => pct(v)), s.savings_vs_previous_provider_pct && s.savings_vs_previous_provider_pct.basis],
              ["Provisioning latency", metric(s.provisioning_latency_ms, v => fmt.num(v, 0) + " ms")],
              ["Quote accuracy (mean |error|)", metric(qa.mean_abs_error_pct, v => pct(v)), "tolerance " + (qa.tolerance_pct ?? "–") + "%"],
              ["Within quote tolerance", metric(qa.within_tolerance, share)],
              ["Provider failure rate", Object.keys(s.provider_failure_rate || {}).length ? h("span", {}, Object.entries(s.provider_failure_rate).map(([p, m]) => h("span", { class: "val-pf" }, OG.providerName(p), " ", metric(m, share)))) : h("span", { class: "dim" }, "no launches")],
            ].map(([k, v, t]) => h("tr", { title: t || null }, h("th", {}, k), h("td", {}, v)))),
          OG.table({
            title: "Per route (newest first)",
            columns: [
              { key: "created_at", label: "At", num: true, fmt: v => fmt.dateTime(v) }, { key: "gpu", label: "GPU", fmt: v => OG.shortGpu(v || "") }, { key: "strategy", label: "Mode", cls: "dim" },
              { key: "winner_provider", label: "Winner", fmt: (v, r) => v ? `${OG.providerName(v)} ${fmt.price(r.winner_price_per_gpu_hour)}` : "–" },
              { key: "runner_up_provider", label: "Runner-up", cls: "dim", fmt: (v, r) => v ? `${OG.providerName(v)} ${fmt.price(r.runner_up_price_per_gpu_hour)}` : `none (${r.candidates_total})` },
              { key: "market_median_per_gpu_hour", label: "Median", num: true, fmt: v => fmt.price(v) },
              { key: "expected_savings_pct", label: "Expected %", num: true, fmt: v => pct(v) || "–" }, { key: "realized_savings_pct", label: "Realized %", num: true, fmt: (v, r) => pct(v) || h("span", { class: "dim" }, r.comparison_reason || "–") },
              { key: "deployment_status", label: "Outcome", fmt: (v, r) => v ? h("a", { href: "/deployments/" + r.deployment_id }, OG.stateBadge(v)) : h("span", { class: "dim" }, r.request_status || "") },
            ], rows: q.outcomes || [], compact: true, csv: "opengrid-route-quality.csv", sort: { key: "created_at", dir: "desc" }, empty: "No routes in this window.", limit: 50,
          }));
      }, err => B.qual.replaceChildren(OG.error(err)));

      ctx.api("/v1/admin/reliability", { nocache: true }).then(r => {
        const M = [["successful_launches", "Launch success", share], ["provisioning_latency_ms", "Provisioning ms", v => fmt.num(v, 0)], ["time_to_capacity_seconds", "Time to capacity", v => fmt.num(v, 0) + " s"],
          ["unexpected_terminations", "Unexpected terminations", share], ["api_error_rate", "API errors", share], ["termination_success", "Termination success", share], ["quote_accuracy", "Quote accuracy", v => pct(v)], ["score", "Score", v => fmt.num(v, 2)]];
        const rows = [];
        for (const [kind, by] of [["customer", r.customer], ["validation", r.validation]]) for (const [p, x] of Object.entries(by || {})) rows.push(Object.assign({ provider: p, kind }, x));
        B.rel.replaceChildren(rows.length ? OG.table({
          columns: [{ key: "provider", label: "Provider", fmt: v => OG.providerLink(v) }, { key: "kind", label: "Launches of", fmt: v => OG.badge(v, v === "validation" ? "warn" : "") }, { key: "launches", label: "Launches", num: true },
            ...M.map(([k, label, f]) => ({ key: k, label, sort: false, fmt: (v, row) => metric(row[k], f) })),
            { key: "failed_launches", label: "Failed (rej / timeout / unknown)", sort: false, fmt: v => v ? `${v.rejected} / ${v.timeout} / ${v.unknown}` : "–" }],
          rows, compact: true, csv: false,
        }) : OG.insufficient("No launches recorded yet: reliability needs OpenGrid's own transactions.", "No reliability data"),
          h("p", { class: "note" }, `A value appears only with at least ${r.min_samples} samples; below that the API says "insufficient sample" and shows n. Validation launches are reported apart and never in customer scores.`));
      }, err => B.rel.replaceChildren(OG.error(err)));

      ctx.api("/v1/admin/funnel", { params: { weeks: 8 }, nocache: true }).then(f => {
        const stages = f.stages || [];
        const conv = (c, a, b) => { const v = c && c[a + "->" + b]; return v == null ? h("span", { class: "dim" }, "–") : h("span", { class: v > 1 ? "warn-t" : "", title: v > 1 ? "over 100%: later stages are not a strict subset of earlier ones in this window" : null }, share(v)); };
        B.fun.replaceChildren(OG.table({
          columns: [{ key: "week", label: "Week of", fmt: v => v === "total" ? h("b", {}, "total") : fmt.date(v) }, ...stages.map(s => ({ key: s, label: s.replace(/_/g, " "), num: true }))],
          rows: [...(f.weeks || []), Object.assign({ week: "total" }, f.total || {})], compact: true, csv: false,
        }), f.total ? h("div", { class: "val-conv" }, stages.slice(1).map((s, i) => h("span", {}, stages[i].replace(/_/g, " "), " → ", s.replace(/_/g, " "), " ", conv(f.total.conversion, stages[i], s)))) : null,
          f.note ? h("p", { class: "note" }, f.note) : null);
      }, err => B.fun.replaceChildren(OG.error(err)));

      ctx.api("/v1/admin/product", { params: { days: Math.min(365, Number(days)) }, nocache: true }).then(p => {
        const small = (rows, cols) => OG.table({ columns: cols, rows: rows || [], compact: true, csv: false, empty: "none" });
        B.prod.replaceChildren(OG.stats([
          { label: "Visitors", value: fmt.num(p.visitors) }, { label: "Repeat", value: fmt.num(p.repeat_visitors), sub: p.repeat_rate != null ? share(p.repeat_rate) + " repeat rate" : null },
          ...Object.entries(p.events || {}).slice(0, 4).map(([k, n]) => ({ label: k.replace(/_/g, " "), value: fmt.num(n) })),
        ]), h("div", { class: "cols-2" },
          small(p.top_pages, [{ key: "page", label: "Page", cls: "mono" }, { key: "views", label: "Views", num: true }, { key: "visitors", label: "Visitors", num: true }]),
          h("div", {}, small(p.top_searches, [{ key: "q", label: "Search", cls: "mono" }, { key: "count", label: "n", num: true }]), small(p.top_gpus, [{ key: "gpu", label: "GPU viewed", cls: "mono" }, { key: "views", label: "n", num: true }]))),
          h("p", { class: "note" }, p.note || "", " · anonymous ids only (sessionStorage), Do Not Track respected."));
      }, err => B.prod.replaceChildren(OG.error(err)));
    },
  });
})();
