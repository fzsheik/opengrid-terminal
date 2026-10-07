/* /ops — internal ops / data-quality terminal. Operator only: every read is an admin endpoint.
   Sources: /v1/ops/summary (everything broken in one view), /v1/ops/providers (full feed health),
   /v1/ops/quarantine (+ POST accept|reject), /v1/ops/incidents, /v1/ops/schema-changes,
   /v1/ops/unmapped, /v1/ops/stale, /v1/ops/jobs, /v1/news/sources (soft).
   Nothing here is computed client-side beyond counting and colouring what the API returned; the
   matrix tones follow the quality layer's own rules (methodology/data-quality.md, data-trust.md). */
(() => {
  const { h, fmt } = OG;
  const REFRESH_MS = 30000;
  const ms = v => (v == null ? "–" : v >= 1000 ? (v / 1000).toFixed(v >= 10000 ? 0 : 2) + "s" : Math.round(v) + "ms");
  const pct = v => (v == null ? "–" : (v * 100 >= 9.95 || v === 0 ? Math.round(v * 100) : (v * 100).toFixed(1)) + "%");
  const SEV = { major: "bad", notable: "warn", info: "" };
  const sevBadge = s => OG.badge(s || "–", SEV[s] || "");
  const STATUS_TONE = { healthy: "good", degraded: "warn", down: "bad", ok: "good", failing: "bad", overdue: "warn", never_run: "", never_fetched: "", disabled: "" };
  const statusBadge = (s, title) => OG.badge((s || "–").replace("_", " "), STATUS_TONE[s] ?? "warn", title);
  const shortId = id => { const s = String(id || ""); return s === "*" ? "* (provider)" : s.length > 28 ? s.slice(0, 12) + "…" + s.slice(-12) : s; };
  const jsonish = v => (v == null ? "∅" : typeof v === "object" ? JSON.stringify(v) : String(v));
  function detailText(d) {
    if (!d || typeof d !== "object") return "";
    if (d.error) return d.error;
    const parts = [];
    for (const [k, v] of Object.entries(d)) {
      if (k === "listing" || k === "fingerprint" || k === "previous_fingerprint") continue;
      if (Array.isArray(v) && !v.length) continue;
      parts.push(k + "=" + (Array.isArray(v) ? v.slice(0, 4).join(", ") + (v.length > 4 ? ` +${v.length - 4}` : "") : jsonish(v)));
    }
    return parts.join(" · ");
  }

  /* Status matrix: provider x check. Each cell -> {tone: ok|warn|bad|na, text, title}. */
  const CHECKS = [
    ["feed", "Feed", "Quality layer status: healthy / degraded / down (data-trust.md)"],
    ["fresh", "Last OK", "Age of the last successful fetch vs the provider's polling interval"],
    ["consec", "Consec. fail", "Failed responses since the last good one (2+ = degraded)"],
    ["rate", "Fail 24h", "Share of fetches that failed in 24h (>20% = degraded)"],
    ["errors", "Errors 24h", "Failed raw snapshots in 24h (any endpoint)"],
    ["p95", "p95 lat.", "95th-percentile fetch latency, 24h (informational, no threshold)"],
    ["count", "Listings Δ24h", "Listings now vs the screened poll nearest 24h ago"],
    ["quar", "Quarantine", "Values held back awaiting a decision"],
    ["schema", "Schema 7d", "Source JSON shape changes in 7 days"],
    ["stale", "Stale", "Listings not seen within their provider's stale window"],
    ["inc", "Incidents", "Open quality incidents for this provider"],
  ];
  function cells(p, ctx) {
    const c = {};
    const reasons = (p.status_reasons || []).join("; ");
    c.feed = { tone: p.status === "healthy" ? "ok" : p.status === "degraded" ? "warn" : "bad", text: p.status, title: reasons || "no problems found" };
    if (!p.last_ok_fetch) c.fresh = { tone: "bad", text: "never", title: "no successful fetch recorded" };
    else {
      const age = (Date.now() - new Date(p.last_ok_fetch)) / 1000, iv = p.polling_interval_seconds || 900;
      c.fresh = { tone: p.status === "down" ? "bad" : age > 2 * iv ? "warn" : "ok", text: fmt.age(p.last_ok_fetch), title: `last ok ${fmt.dateTime(p.last_ok_fetch)} · polled every ${Math.round(iv / 60)}m` };
    }
    const cf = p.consecutive_failures || 0;
    c.consec = { tone: cf >= 2 ? "bad" : cf === 1 ? "warn" : "ok", text: String(cf) };
    c.rate = p.failure_rate_24h == null ? { tone: "na", text: "n/a", title: "no fetches in 24h" }
      : { tone: p.failure_rate_24h > 0.2 ? "bad" : p.failure_rate_24h > 0 ? "warn" : "ok", text: pct(p.failure_rate_24h), title: `${p.failures_24h ?? "?"} of ${p.fetches_24h ?? "?"} fetches failed` };
    const er = ctx.errorsBy.get(p.provider) || 0;
    c.errors = { tone: er ? "warn" : "ok", text: String(er) };
    c.p95 = p.latency_ms_p95_24h == null ? { tone: "na", text: "n/a", title: "no fetch timings in 24h" } : { tone: "info", text: ms(p.latency_ms_p95_24h), title: `p50 ${ms(p.latency_ms_p50_24h)} · p95 ${ms(p.latency_ms_p95_24h)}` };
    if (p.listings_24h_ago == null) c.count = { tone: "na", text: fmt.num(p.listings_now) + " / ?", title: p.listings_24h_ago_note || "no poll near 24h ago" };
    else {
      const was = p.listings_24h_ago, now = p.listings_now || 0, r = was ? now / was : null;
      c.count = { tone: now === 0 && was > 0 ? "bad" : r != null && (r < 0.5 || r > 3) ? "warn" : "ok", text: `${fmt.num(now)} / ${fmt.num(was)}`, title: `now ${now}, 24h ago ${was}${r != null ? " (" + (r >= 1 ? "+" : "") + pct(r - 1) + ")" : ""}` };
    }
    c.quar = { tone: p.quarantined ? "warn" : "ok", text: String(p.quarantined || 0) };
    c.schema = { tone: p.schema_changes_7d ? "warn" : "ok", text: String(p.schema_changes_7d || 0) };
    const sl = ctx.staleBy[p.provider] || 0;
    c.stale = { tone: sl ? "warn" : "ok", text: String(sl) };
    const oi = Object.entries(p.open_incidents || {}), n = oi.reduce((a, [, v]) => a + v, 0);
    c.inc = { tone: n ? "warn" : "ok", text: String(n), title: oi.map(([k, v]) => `${k}: ${v}`).join("\n") || "none open" };
    return c;
  }

  OG.page("/ops", {
    title: "Ops",
    async mount(el, params, query, ctx) {
      const root = h("div", { class: "ops" });
      el.append(root);
      const st = { provider: OG.qs.get("provider", null), qStatus: OG.qs.get("q", "pending"), incStatus: OG.qs.get("inc", "open"), incKind: OG.qs.get("kind", null),
        confirming: null, flash: null, sum: null, health: null, lastAt: null };
      const upd = h("span", { class: "ops-upd mono" }, "loading…");
      const provPick = h("select", { class: "field", "aria-label": "Filter by provider", onchange: e => setProvider(e.target.value || null) });
      root.append(OG.head("Ops · data quality", "Internal: feed health, held values, incidents and pipeline jobs. Every figure is read from the quality layer; nothing is estimated.",
        h("span", { class: "flt" }, h("span", {}, "Provider"), provPick), upd,
        h("button", { class: "btn", onclick: () => refresh(true) }, "Refresh")));
      const box = id => h("div", { class: "ops-b", id });
      const B = {
        stats: box("ops-stats"), matrix: box("ops-matrix"), health: box(), latency: box(), errors: box(), jobs: box(), quar: box(), inc: box(),
        schema: box(), unmapped: box(), stale: box(), anomalies: box(), routing: box(), news: box(),
      };
      const sec = (title, sub, body, right) => h("section", { class: "sec ops-sec" },
        h("div", { class: "ops-sh" }, h("h2", { class: "sec-h" }, title), sub ? h("span", { class: "ops-sub" }, sub) : null, h("span", { class: "spacer" }), right || null), body);
      const flashEl = h("div", { class: "ops-flash", hidden: true, role: "status" });
      root.append(B.stats, flashEl,
        sec("Status matrix", "provider × check · hover a cell for detail · click a row to filter the page", B.matrix),
        h("div", { class: "ops-grid2" },
          sec("Feed health", "per provider, last 24h", B.health),
          h("div", {}, sec("Source latency", "fetch latency p50 → p95, last 24h", B.latency), sec("Provider API failures", "failed raw snapshots, 24h", B.errors),
            sec("Anomalies", "held / flagged by the screen", B.anomalies))),
        sec("Quarantine queue", "held values: the listing keeps its last good value until a decision", B.quar),
        sec("Incidents", "schema changes, empty responses, collapses, normalizer errors, static flags", B.inc),
        h("div", { class: "ops-grid2 even" },
          sec("Unmapped GPUs", "the mapping to-do", B.unmapped, h("a", { class: "lnk ops-a", href: "/v1/ops/unmapped", target: "_blank" }, "JSON →")),
          sec("Stale listings", "not seen within the stale window", B.stale)),
        sec("Schema changes", "source JSON shape changes per endpoint", B.schema),
        h("div", { class: "ops-grid2" },
          sec("Background jobs", "jobs.JOBS on this server process", B.jobs),
          sec("Routing & provisioning", "probes of the routing tables", B.routing)),
        sec("News ingestion", "per source, last 24h", B.news),
        h("p", { class: "note" }, "Rules and thresholds: ", h("a", { class: "lnk", href: "/methodology/data-quality" }, "data quality"), " · ",
          h("a", { class: "lnk", href: "/methodology/data-trust" }, "data trust"), ". Auto-refreshes every 30 s while this tab is visible."));
      for (const k of ["matrix", "health", "quar", "inc"]) B[k].append(OG.loading());

      function setProvider(p) {
        st.provider = p; OG.qs.set({ provider: p });
        provPick.value = p || "";
        drawAll(); loadQuarantine(); loadIncidents(); loadStale(); loadSchema();
      }
      function flash(text, tone) {
        flashEl.hidden = false; flashEl.className = "ops-flash " + (tone || "");
        flashEl.replaceChildren(h("span", {}, text), h("button", { class: "btn sm", onclick: () => { flashEl.hidden = true; } }, "×"));
      }

      /* ---------- summary + providers ---------- */
      async function loadCore(force) {
        try {
          const [sumR, health] = await Promise.all([
            ctx.api("/v1/ops/summary", { full: true, nocache: force }),
            ctx.api("/v1/ops/providers", { nocache: force })]);
          st.sum = sumR.data; st.health = health; st.lastAt = new Date();
          OG.status.asOf(sumR.meta && sumR.meta.as_of);
          upd.textContent = "updated " + fmt.time(st.lastAt) + " · auto 30s";
          upd.classList.remove("bad");
        } catch (e) {
          upd.textContent = "refresh failed " + fmt.time(new Date()); upd.classList.add("bad");
          if (!st.sum) { for (const k of ["matrix", "health"]) B[k].replaceChildren(OG.error(e, () => refresh(true))); B.stats.replaceChildren(); }
          return;
        }
        const names = st.health.map(p => p.provider);
        provPick.replaceChildren(h("option", { value: "" }, "All providers"), ...names.map(n => h("option", { value: n, selected: n === st.provider ? "" : null }, OG.providerName(n))));
        drawAll();
      }
      function filt(rows) { return st.provider ? rows.filter(r => r.provider === st.provider) : rows; }

      function drawAll() {
        if (!st.sum || !st.health) return;
        const s = st.sum, hs = st.health;
        const by = s.providers.by_status || {};
        const jobsBad = (s.jobs || []).filter(j => j.status === "failing" || j.status === "overdue").length;
        const errN = (s.provider_errors_24h || []).reduce((a, e) => a + e.failures, 0);
        const incOpen = Object.values(s.incidents_open || {}).reduce((a, b) => a + b, 0);
        B.stats.replaceChildren(OG.stats([
          { label: "Feeds", value: h("span", {}, h("span", { class: "up" }, by.healthy || 0), h("span", { class: "dimmer" }, " / "), h("span", { class: by.degraded ? "ops-warn" : "dimmer" }, by.degraded || 0), h("span", { class: "dimmer" }, " / "), h("span", { class: by.down ? "down" : "dimmer" }, by.down || 0)), sub: "healthy / degraded / down" },
          { label: "Quarantine", value: h("span", { class: s.quarantine.pending ? "ops-warn" : "" }, String(s.quarantine.pending)), sub: "values held, pending" },
          { label: "Open incidents", value: h("span", { class: incOpen ? "ops-warn" : "" }, String(incOpen)), sub: Object.keys(s.incidents_open || {}).slice(0, 3).join(", ") || "none" },
          { label: "Fetch errors 24h", value: h("span", { class: errN ? "ops-warn" : "" }, String(errN)), sub: `${(s.provider_errors_24h || []).length} provider·endpoint` },
          { label: "Schema changes", value: String(s.schema_changes_7d.count), sub: "last 7 days" },
          { label: "Stale listings", value: String(s.stale_listings.count), sub: Object.keys(s.stale_listings.by_provider || {}).length + " providers" },
          { label: "Unmapped GPUs", value: String(s.unmapped_gpus.count), sub: "raw names, no canonical" },
          { label: "Jobs", value: h("span", { class: jobsBad ? "down" : "" }, `${(s.jobs || []).length - jobsBad}/${(s.jobs || []).length}`), sub: jobsBad ? `${jobsBad} failing/overdue` : "ok or not yet run" },
        ]));
        drawMatrix(hs, s); drawHealth(hs); drawLatency(hs); drawErrors(s); drawJobs(s.jobs || []); drawAnomalies(s); drawRouting(s); drawUnmapped();
      }

      function drawMatrix(hs, s) {
        const ctxM = { errorsBy: new Map(), staleBy: (s.stale_listings && s.stale_listings.by_provider) || {} };
        for (const e of s.provider_errors_24h || []) ctxM.errorsBy.set(e.provider, (ctxM.errorsBy.get(e.provider) || 0) + e.failures);
        const rank = { down: 0, degraded: 1, healthy: 2 };
        const rows = hs.slice().sort((a, b) => (rank[a.status] ?? 3) - (rank[b.status] ?? 3) || a.provider.localeCompare(b.provider));
        if (!rows.length) { B.matrix.replaceChildren(OG.empty("No provider has any raw data or listings on this server.")); return; }
        const tbl = h("table", { class: "ops-mx" },
          h("thead", {}, h("tr", {}, h("th", { class: "pn" }, "Provider"), CHECKS.map(([, l, t]) => h("th", { title: t }, l)))),
          h("tbody", {}, rows.map(p => {
            const c = cells(p, ctxM), on = st.provider === p.provider;
            return h("tr", { class: (on ? "on " : "") + (st.provider && !on ? "dimrow" : ""), tabindex: "0", title: "click: filter this page to " + p.provider,
              onclick: () => setProvider(on ? null : p.provider), onkeydown: e => { if (e.key === "Enter") setProvider(on ? null : p.provider); } },
            h("td", { class: "pn" }, OG.logo(p.provider, 13), " ", OG.providerName(p.provider)),
            CHECKS.map(([k]) => h("td", { class: "mx " + c[k].tone, title: c[k].title || null }, c[k].text)));
          })));
        const worst = rows.filter(p => p.status !== "healthy");
        B.matrix.replaceChildren(h("div", { class: "ops-mxwrap" }, tbl),
          h("div", { class: "ops-legend" }, h("span", { class: "mx ok" }, "ok"), h("span", { class: "mx warn" }, "attention"), h("span", { class: "mx bad" }, "failing"), h("span", { class: "mx na" }, "no data"),
            h("span", { class: "dim" }, worst.length ? ` ${worst.length} provider${worst.length === 1 ? "" : "s"} not healthy: ` + (worst.length <= 3 ? worst.map(p => OG.providerName(p.provider) + " (" + ((p.status_reasons || [])[0] || p.status) + ")").join("; ") : worst.map(p => OG.providerName(p.provider)).join(", ") + " · hover a Feed cell for the reason") : " every provider feed is healthy")));
      }

      function drawHealth(hs) {
        B.health.replaceChildren(OG.table({
          rows: filt(hs), compact: true, csv: "opengrid-ops-feeds.csv", rowKey: r => r.provider,
          sort: { key: "status", dir: "asc" },
          columns: [
            { key: "provider", label: "Provider", fmt: v => OG.providerLink(v) },
            { key: "status", label: "Status", value: r => ({ down: 0, degraded: 1, healthy: 2 })[r.status], csv: r => r.status, fmt: (v, r) => statusBadge(r.status, (r.status_reasons || []).join("; ") || null) },
            { key: "source_type", label: "Source", cls: "dim", fmt: v => (v || "–").replace(/_/g, " ") },
            { key: "last_ok_fetch", label: "Last OK", num: true, value: r => r.last_ok_fetch ? -new Date(r.last_ok_fetch) : null, csv: r => r.last_ok_fetch, fmt: (v, r) => r.last_ok_fetch ? h("span", { title: fmt.dateTime(r.last_ok_fetch) }, fmt.age(r.last_ok_fetch)) : h("span", { class: "down" }, "never") },
            { key: "consecutive_failures", label: "Consec", num: true, desc: true, fmt: v => h("span", { class: v >= 2 ? "down" : v ? "ops-warn" : "dimmer" }, String(v ?? "–")) },
            { key: "failure_rate_24h", label: "Fail%", num: true, desc: true, fmt: (v, r) => v == null ? OG.na("no fetches in 24h") : h("span", { class: v > 0.2 ? "down" : v > 0 ? "ops-warn" : "dimmer", title: `${r.failures_24h}/${r.fetches_24h}` }, pct(v)) },
            { key: "latency_ms_p50_24h", label: "p50", num: true, desc: true, fmt: v => v == null ? OG.na("no timings") : ms(v) },
            { key: "latency_ms_p95_24h", label: "p95", num: true, desc: true, fmt: v => v == null ? OG.na("no timings") : ms(v) },
            { key: "listings_now", label: "Now", num: true, desc: true, title: "Live listings now" },
            { key: "listings_24h_ago", label: "24h ago", num: true, desc: true, fmt: (v, r) => v == null ? OG.na(r.listings_24h_ago_note) : fmt.num(v) },
            { key: "last_failure", label: "Last failure", sort: false, csv: r => r.last_failure && r.last_failure.error, cls: "wrap ops-err",
              fmt: v => v ? h("span", { title: v.error || "" }, h("span", { class: "dim" }, fmt.age(v.at) + " "), v.status_code ? h("b", {}, v.status_code + " ") : null, (v.error || "").slice(0, 60)) : h("span", { class: "dimmer" }, "–") },
          ],
        }));
      }

      function drawLatency(hs) {
        const rows = filt(hs).filter(p => p.latency_ms_p50_24h != null).sort((a, b) => b.latency_ms_p95_24h - a.latency_ms_p95_24h);
        B.latency.replaceChildren();
        if (!rows.length) { B.latency.append(OG.empty("No fetch timings recorded in the last 24h.")); return; }
        const box2 = h("div", { class: "box box-p" });
        B.latency.append(box2, h("p", { class: "note" }, "Dot = p50 and p95 of fetch latency per provider over 24h (raw_snapshots timings). Only the two percentiles are exposed by the API, so this is not a time series."));
        const c = OG.charts.dotplot(box2, { fmt: ms, zero: true, axisMin: 0, label: "fetch latency by provider",
          rows: rows.map(p => ({ label: OG.providerName(p.provider), href: null, low: p.latency_ms_p50_24h, high: p.latency_ms_p95_24h,
            points: [{ key: "p50", label: "p50", value: p.latency_ms_p50_24h, color: "#4d94ff" }, { key: "p95", label: "p95", value: p.latency_ms_p95_24h, color: p.status === "healthy" ? "#98a4b1" : "#f5a524" }] })) });
        ctx.onCleanup(() => c.destroy());
      }

      function drawErrors(s) {
        const rows = filt(s.provider_errors_24h || []);
        B.errors.replaceChildren(rows.length ? OG.table({
          rows, compact: true, limit: 12, csv: "opengrid-ops-errors.csv",
          columns: [
            { key: "provider", label: "Provider", fmt: v => OG.providerLink(v, { logo: false }) },
            { key: "endpoint", label: "Endpoint", cls: "dim mono ops-ep", fmt: v => h("span", { title: v }, String(v || "").slice(0, 34)) },
            { key: "failures", label: "Fails", num: true, desc: true },
            { key: "last_status", label: "HTTP", num: true },
            { key: "last_at", label: "Last", num: true, value: r => -new Date(r.last_at), csv: r => r.last_at, fmt: (v, r) => fmt.age(r.last_at) },
            { key: "last_error", label: "Last error", cls: "wrap ops-err", fmt: v => h("span", { title: v || "" }, String(v || "–").slice(0, 80)) },
          ] }) : OG.empty("No failed fetch in the last 24h" + (st.provider ? " for " + OG.providerName(st.provider) : "") + "."));
      }

      function drawJobs(jobs) {
        B.jobs.replaceChildren(jobs.length ? OG.table({
          rows: jobs, compact: true, rowKey: r => r.name, sort: { key: "status", dir: "asc" },
          columns: [
            { key: "name", label: "Job", cls: "mono" },
            { key: "status", label: "Status", value: r => ({ failing: 0, overdue: 1, never_run: 2, ok: 3 })[r.status], csv: r => r.status, fmt: (v, r) => statusBadge(r.status, r.last_error || null) },
            { key: "every_seconds", label: "Every", num: true, fmt: v => v >= 3600 ? v / 3600 + "h" : v >= 60 ? v / 60 + "m" : v + "s" },
            { key: "runs", label: "Runs", num: true },
            { key: "failures", label: "Fails", num: true, fmt: v => h("span", { class: v ? "down" : "dimmer" }, String(v)) },
            { key: "last_finished", label: "Last run", num: true, value: r => r.last_finished ? -new Date(r.last_finished) : null, fmt: (v, r) => r.running ? OG.badge("running", "good") : r.last_finished ? fmt.age(r.last_finished) : h("span", { class: "dimmer" }, "never") },
            { key: "last_duration_ms", label: "Took", num: true, fmt: v => ms(v) },
            { key: "last_error", label: "Last error", cls: "wrap ops-err", fmt: v => v ? h("span", { title: v }, String(v).slice(0, 70)) : h("span", { class: "dimmer" }, "–") },
          ] }) : OG.empty("No background jobs registered."),
        jobs.length && jobs.every(j => j.runs === 0) ? h("p", { class: "note" }, "No job has run in this server process (jobs disabled with OPENGRID_NO_JOBS, or just started).") : null);
      }

      function drawAnomalies(s) {
        const de = s.duplicate_explosions, sp = s.suspicious_price_moves;
        const rowsA = [
          ["Duplicate explosions", de.pending, "listing-count explosions held (rule duplicate_explosion)", "duplicate_explosion"],
          ["Listing collapses", de.collapses_open, "open listing_collapse incidents", null, "listing_collapse"],
          ["Missing regions", s.missing_regions.pending, "region_disappeared holds", "region_disappeared"],
          ["Suspicious price moves", sp.pending, Object.entries(sp.by_rule || {}).map(([k, v]) => `${k}: ${v}`).join(", ") || "price_* holds", "price"],
          ["Instance price mismatch", sp.instance_price_mismatch_open, "open flags (nothing held)", null, "instance_price_mismatch"],
        ];
        B.anomalies.replaceChildren(h("table", { class: "ops-kv" }, h("tbody", {}, rowsA.map(([l, n, t, rule, kind]) =>
          h("tr", {}, h("td", {}, l), h("td", { class: "n " + (n ? "ops-warn" : "dimmer") }, String(n ?? 0)), h("td", { class: "dim" }, t),
            h("td", {}, n && kind ? h("a", { class: "lnk", href: "#ops-inc", onclick: e => { e.preventDefault(); st.incKind = kind; st.incStatus = "open"; OG.qs.set({ kind, inc: null }); loadIncidents(); B.inc.scrollIntoView({ block: "start" }); } }, "show →")
              : n && rule ? h("a", { class: "lnk", href: "#ops-q", onclick: e => { e.preventDefault(); st.qRule = rule; drawQuarantine(); B.quar.scrollIntoView({ block: "start" }); } }, "show →") : null))))),
        h("p", { class: "note" }, "Counts of pending quarantine rows and open incidents by rule (quality/checks.py)."));
      }

      function drawRouting(s) {
        const probes = Object.values(s.routing || {});
        B.routing.replaceChildren(probeTable(probes),
          h("p", { class: "note" }, "Routing errors and provisioning failures are counted from the routing tables by their error/status columns (rule in the row tooltip). Provider API failures are the fetch errors above. ",
            h("a", { class: "lnk", href: "/deployments" }, "Deployments →"), " ", h("a", { class: "lnk", href: "/methodology/routing" }, "Routing methodology →")));
      }
      function probeTable(probes) {
        if (!probes.length) return OG.empty("No probes.");
        return h("table", { class: "ops-kv" }, h("thead", {}, h("tr", {}, ["Table", "Rows", "24h", "Failures 24h", "Recent errors"].map((t, i) => h("th", { class: i && i < 4 ? "n" : "" }, t)))),
          h("tbody", {}, probes.map(p => p.available ? h("tr", { title: "failure rule: " + p.failure_rule },
            h("td", { class: "mono" }, p.table), h("td", { class: "n" }, fmt.num(p.rows)), h("td", { class: "n" }, p.rows_24h == null ? OG.na("no time column") : fmt.num(p.rows_24h)),
            h("td", { class: "n " + (p.failures_24h ? "down" : "dimmer") }, String(p.failures_24h)),
            h("td", { class: "wrap ops-err" }, (p.recent_errors || []).length ? p.recent_errors.slice(0, 3).map(e => h("div", { title: e }, e.slice(0, 90))) : h("span", { class: "dimmer" }, "–")))
            : h("tr", {}, h("td", { class: "mono" }, p.table), h("td", { colspan: 4, class: "dim" }, p.reason)))));
      }

      /* ---------- quarantine ---------- */
      let quarRows = [];
      async function loadQuarantine(force) {
        if (st.confirming) return;                      // never wipe an open confirmation
        try {
          quarRows = await ctx.api("/v1/ops/quarantine", { params: { status: st.qStatus, provider: st.provider, limit: 300 }, slot: "ops-q", nocache: force });
        } catch (e) { if (!e.stale) B.quar.replaceChildren(OG.error(e, () => loadQuarantine(true))); return; }
        drawQuarantine();
      }
      function drawQuarantine() {
        const seg = OG.seg([["pending", "Pending"], ["accepted", "Accepted"], ["rejected", "Rejected"], ["auto_accepted", "Auto"], ["superseded", "Superseded"], ["all", "All"]], st.qStatus,
          v => { st.qStatus = v; st.qRule = null; OG.qs.set({ q: v === "pending" ? null : v }); loadQuarantine(true); });
        const rows = st.qRule ? quarRows.filter(r => r.rule === st.qRule || (st.qRule === "price" && /^price_/.test(r.rule))) : quarRows;
        const ruleChip = st.qRule ? h("button", { class: "chip", "aria-pressed": "true", onclick: () => { st.qRule = null; drawQuarantine(); } }, "rule: " + st.qRule + " ×") : null;
        B.quar.replaceChildren(OG.table({
          rows, compact: true, rowKey: r => r.id, title: `${rows.length} ${st.qStatus === "all" ? "" : st.qStatus.replace("_", " ")} hold${rows.length === 1 ? "" : "s"}`,
          toolbar: [ruleChip, seg].filter(Boolean), csv: "opengrid-quarantine.csv", empty: st.qStatus === "pending" ? "Nothing held: no value is awaiting a decision." : "No rows.",
          rowClass: r => (st.confirming && st.confirming.id === r.id ? "hl" : ""),
          columns: [
            { key: "id", label: "#", num: true, cls: "dim" },
            { key: "provider", label: "Provider", fmt: v => OG.providerLink(v, { logo: false }) },
            { key: "listing_id", label: "Listing", cls: "mono dim", fmt: (v, r) => h("span", { title: v + ((r.detail && r.detail.listing && r.detail.listing.raw_gpu_name) ? "\n" + r.detail.listing.raw_gpu_name : "") }, shortId(v)) },
            { key: "field", label: "Field", cls: "mono" },
            { key: "rule", label: "Rule", fmt: (v, r) => h("span", { title: detailText(r.detail) }, ((r.detail && r.detail.rules) || [v]).join(" + ")) },
            { key: "previous_value", label: "Last good → held", sort: false, csv: r => jsonish(r.previous_value) + " -> " + jsonish(r.new_value),
              fmt: (v, r) => h("span", { class: "mono ops-chgv" }, h("span", { class: "dim" }, jsonish(r.previous_value)), " → ", h("b", {}, jsonish(r.new_value))) },
            { key: "seen_count", label: "Seen", num: true, title: "Consecutive sightings of the held value" },
            { key: "auto_acceptable", label: "Auto", title: "May auto-accept once it persists (K polls)", fmt: v => v ? h("span", { class: "dim" }, "yes") : h("span", { class: "ops-warn" }, "no") },
            { key: "first_seen", label: "First", num: true, value: r => -new Date(r.first_seen), csv: r => r.first_seen, fmt: (v, r) => h("span", { title: fmt.dateTime(r.first_seen) }, fmt.age(r.first_seen)) },
            { key: "last_seen", label: "Last", num: true, value: r => -new Date(r.last_seen), csv: r => r.last_seen, fmt: (v, r) => fmt.age(r.last_seen) },
            { key: "status", label: "Status", fmt: (v, r) => v === "pending" ? OG.badge("pending", "warn") : h("span", { class: "dim", title: [r.resolved_by, r.note].filter(Boolean).join(" · ") }, v.replace("_", " ")) },
            { key: "_act", label: "Decision", sort: false, csv: false, fmt: (v, r) => r.status === "pending" ? actions(r) : h("span", { class: "dimmer" }, r.resolved_at ? fmt.age(r.resolved_at) + " ago" : "–") },
          ] }),
        h("p", { class: "note" }, "Accept applies the held value to current state now (and history if a tracked value moved). Reject keeps it held while the provider keeps sending it. Both are recorded with your identity. ",
          h("a", { class: "lnk", href: "/methodology/data-quality" }, "Lifecycle →")));
      }
      function actions(r) {
        const c = st.confirming;
        if (c && c.id === r.id) {
          const note = h("input", { class: "field ops-note", placeholder: "note (optional)", "aria-label": "Decision note", value: c.note || "", oninput: e => { c.note = e.target.value; } });
          setTimeout(() => note.focus(), 0);
          return h("span", { class: "ops-confirm" }, h("b", { class: c.decision === "accept" ? "up" : "down" }, c.decision === "accept" ? "Apply this value?" : "Reject this value?"), note,
            h("button", { class: "btn sm " + (c.decision === "accept" ? "ops-yes" : "ops-no"), disabled: c.busy ? "" : null, onclick: () => decide(r, c) }, c.busy ? "…" : "Confirm " + c.decision),
            h("button", { class: "btn sm", onclick: () => { st.confirming = null; drawQuarantine(); } }, "Cancel"));
        }
        return h("span", { class: "ops-acts" },
          h("button", { class: "btn sm ops-yes", title: "Accept: apply the held value now", onclick: () => { st.confirming = { id: r.id, decision: "accept" }; drawQuarantine(); } }, "Accept"),
          h("button", { class: "btn sm ops-no", title: "Reject: keep holding it", onclick: () => { st.confirming = { id: r.id, decision: "reject" }; drawQuarantine(); } }, "Reject"));
      }
      async function decide(r, c) {
        c.busy = true; drawQuarantine();
        try {
          const out = await ctx.api(`/v1/ops/quarantine/${r.id}/${c.decision}`, { method: "POST", body: { note: c.note || null } });
          flash(`#${r.id} ${out && out.status ? out.status : c.decision + "ed"}${c.decision === "accept" ? (out && out.applied ? " · applied to current state" : " · nothing to apply") : ""} (${r.provider} ${r.field}).`, "good");
        } catch (e) {
          flash(`#${r.id}: ${e.status === 409 ? e.message : "could not " + c.decision + ": " + e.message}`, "bad");
        }
        st.confirming = null;
        loadQuarantine(true); loadCore(true);
      }

      /* ---------- incidents ---------- */
      let incRows = [], incKinds = new Set();
      async function loadIncidents(force) {
        try {
          incRows = await ctx.api("/v1/ops/incidents", { params: { status: st.incStatus === "all" ? null : st.incStatus, kind: st.incKind, provider: st.provider, limit: 500 }, slot: "ops-inc", nocache: force });
        } catch (e) { if (!e.stale) B.inc.replaceChildren(OG.error(e, () => loadIncidents(true))); return; }
        for (const r of incRows) incKinds.add(r.kind);
        drawIncidents();
      }
      function drawIncidents() {
        const seg = OG.seg([["open", "Open"], ["resolved", "Resolved"], ["all", "All"]], st.incStatus, v => { st.incStatus = v; OG.qs.set({ inc: v === "open" ? null : v }); loadIncidents(true); });
        const kinds = [...new Set([...incKinds, "schema_change", "empty_response", "listing_collapse", "normalizer_error", "instance_price_mismatch", "vram_conflict", "quality_layer_error"])].sort();
        const kindSel = h("select", { class: "field", "aria-label": "Incident kind", onchange: e => { st.incKind = e.target.value || null; OG.qs.set({ kind: st.incKind }); loadIncidents(true); } },
          h("option", { value: "" }, "All kinds"), kinds.map(k => h("option", { value: k, selected: k === st.incKind ? "" : null }, k)));
        const sevN = { major: 0, notable: 0, info: 0 };
        for (const r of incRows) sevN[r.severity] = (sevN[r.severity] || 0) + 1;
        B.inc.id = "ops-inc";
        B.inc.replaceChildren(OG.table({
          rows: incRows, compact: true, rowKey: r => r.id, limit: 100, csv: "opengrid-incidents.csv",
          title: `${incRows.length} incident${incRows.length === 1 ? "" : "s"} · ${sevN.major} major · ${sevN.notable} notable`,
          toolbar: [kindSel, seg], empty: "No incidents match.", sort: { key: "last_seen", dir: "desc" },
          rowClass: r => (r.status === "resolved" ? "out" : ""),
          columns: [
            { key: "severity", label: "Sev", value: r => ({ major: 0, notable: 1, info: 2 })[r.severity], csv: r => r.severity, fmt: (v, r) => sevBadge(r.severity) },
            { key: "kind", label: "Kind", cls: "mono" },
            { key: "provider", label: "Provider", fmt: v => v ? OG.providerLink(v, { logo: false }) : h("span", { class: "dimmer" }, "system") },
            { key: "listing_id", label: "Listing / endpoint", cls: "mono dim", value: r => r.listing_id || r.endpoint, fmt: (v, r) => h("span", { title: (r.listing_id || "") + " " + (r.endpoint || "") }, shortId(r.listing_id || r.endpoint || "–")) },
            { key: "detail", label: "Detail", sort: false, cls: "wrap ops-det", csv: r => JSON.stringify(r.detail), fmt: v => h("span", { title: JSON.stringify(v, null, 1) }, detailText(v).slice(0, 160)) },
            { key: "count", label: "Count", num: true, desc: true },
            { key: "first_seen", label: "First", num: true, value: r => +new Date(r.first_seen), csv: r => r.first_seen, fmt: (v, r) => h("span", { title: fmt.dateTime(r.first_seen) }, fmt.age(r.first_seen)) },
            { key: "last_seen", label: "Last", num: true, value: r => +new Date(r.last_seen), csv: r => r.last_seen, desc: true, fmt: (v, r) => h("span", { title: fmt.dateTime(r.last_seen) }, fmt.age(r.last_seen)) },
            { key: "status", label: "Status", fmt: (v, r) => v === "open" ? OG.badge("open", SEV[r.severity] || "") : h("span", { class: "dim", title: r.resolved_at ? "resolved " + fmt.dateTime(r.resolved_at) : "" }, "resolved") },
          ] }));
      }

      /* ---------- schema, unmapped, stale ---------- */
      async function loadSchema(force) {
        let rows;
        try { rows = await ctx.api("/v1/ops/schema-changes", { params: { provider: st.provider, limit: 100 }, slot: "ops-schema", nocache: force }); }
        catch (e) { if (!e.stale) B.schema.replaceChildren(OG.error(e)); return; }
        B.schema.replaceChildren(rows.length ? OG.table({
          rows, compact: true, limit: 20, csv: "opengrid-schema-changes.csv", sort: { key: "last_seen", dir: "desc" },
          columns: [
            { key: "severity", label: "Sev", fmt: v => sevBadge(v), title: "major: a path removed or retyped; notable: paths added" },
            { key: "provider", label: "Provider", fmt: v => OG.providerLink(v, { logo: false }) },
            { key: "endpoint", label: "Endpoint", cls: "mono dim", fmt: v => h("span", { title: v }, String(v || "").slice(0, 40)) },
            { key: "added", label: "Added", value: r => (r.detail && r.detail.added_count) ?? ((r.detail && r.detail.added) || []).length, num: true, fmt: (v, r) => h("span", { title: ((r.detail && r.detail.added) || []).join("\n") }, String(v)) },
            { key: "removed", label: "Removed", value: r => (r.detail && r.detail.removed_count) ?? ((r.detail && r.detail.removed) || []).length, num: true, fmt: (v, r) => h("span", { class: v ? "down" : "dimmer", title: ((r.detail && r.detail.removed) || []).join("\n") }, String(v)) },
            { key: "retyped", label: "Retyped", value: r => ((r.detail && r.detail.retyped) || []).length, num: true, fmt: (v, r) => h("span", { class: v ? "down" : "dimmer", title: ((r.detail && r.detail.retyped) || []).join("\n") }, String(v)) },
            { key: "paths", label: "Paths", sort: false, cls: "wrap mono ops-det", csv: r => detailText(r.detail), fmt: (v, r) => [...((r.detail && r.detail.removed) || []).map(p => "− " + p), ...((r.detail && r.detail.retyped) || []).map(p => "~ " + p), ...((r.detail && r.detail.added) || []).map(p => "+ " + p)].slice(0, 3).join("  ") || "–" },
            { key: "last_seen", label: "When", num: true, value: r => +new Date(r.last_seen), csv: r => r.last_seen, fmt: (v, r) => h("span", { title: fmt.dateTime(r.last_seen) }, fmt.age(r.last_seen)) },
            { key: "status", label: "Status", cls: "dim" },
          ] }) : OG.empty("No source schema change recorded" + (st.provider ? " for " + OG.providerName(st.provider) : "") + "."));
      }
      let unmapped = null;
      async function loadUnmapped(force) {
        try { unmapped = await ctx.api("/v1/ops/unmapped", { nocache: force }); } catch (e) { B.unmapped.replaceChildren(OG.error(e)); return; }
        drawUnmapped();
      }
      function drawUnmapped() {
        if (!unmapped) return;
        const rows = filt(unmapped);
        B.unmapped.replaceChildren(rows.length ? OG.table({
          rows, compact: true, limit: 15, csv: "opengrid-unmapped.csv", title: `${rows.length} raw name${rows.length === 1 ? "" : "s"}`,
          columns: [
            { key: "provider", label: "Provider", fmt: v => OG.providerLink(v, { logo: false }) },
            { key: "raw_gpu_name", label: "Raw GPU name", cls: "mono" },
            { key: "reason", label: "Why", cls: "dim" },
          ] }) : OG.empty("Every raw GPU name maps to a canonical GPU."),
        h("p", { class: "note" }, "Mappings are hand-maintained in canonical.py; OpenGrid never guesses one. These listings are excluded from every market figure until mapped."));
      }
      async function loadStale(force) {
        let res;
        try { res = await ctx.api("/v1/ops/stale", { params: { provider: st.provider, include_aging: true, limit: 200 }, full: true, slot: "ops-stale", nocache: force }); }
        catch (e) { if (!e.stale) B.stale.replaceChildren(OG.error(e)); return; }
        const rows = res.data, total = res.meta && res.meta.pagination ? res.meta.pagination.total : rows.length;
        const nStale = rows.filter(r => r.freshness === "stale").length;
        B.stale.replaceChildren(rows.length ? OG.table({
          rows, compact: true, limit: 15, csv: "opengrid-stale.csv", title: `${total} aging/stale${total > rows.length ? " · first " + rows.length + " shown" : ""} · ${nStale} stale`,
          columns: [
            { key: "provider", label: "Provider", fmt: v => OG.providerLink(v, { logo: false }) },
            { key: "canonical_gpu_name", label: "GPU", value: r => r.canonical_gpu_name || r.raw_gpu_name, fmt: (v, r) => r.canonical_gpu_name ? OG.gpuLink(r.canonical_gpu_name) : h("span", { class: "dim" }, r.raw_gpu_name) },
            { key: "region", label: "Region", cls: "dim", fmt: v => v || "–" },
            { key: "age_seconds", label: "Age", num: true, desc: true, fmt: (v, r) => h("span", { class: r.freshness === "stale" ? "down" : "ops-warn" }, fmt.age(v)) },
          ] }) : OG.empty("No aging or stale listing" + (st.provider ? " for " + OG.providerName(st.provider) : "") + "."));
      }

      /* ---------- news ---------- */
      async function loadNews(force) {
        const rows = await OG.api.soft("/v1/news/sources", { nocache: force });
        if (!ctx.alive()) return;
        if (!rows) { B.news.replaceChildren(OG.insufficient("/v1/news/sources did not answer on this server.", "News health unavailable")); return; }
        const rank = { failing: 0, degraded: 1, never_fetched: 2, ok: 3, disabled: 4 };
        const bad = rows.filter(r => r.health === "failing" || r.health === "degraded").length, never = rows.filter(r => r.health === "never_fetched").length;
        B.news.replaceChildren(OG.table({
          rows, compact: true, limit: 15, csv: "opengrid-news-sources.csv", sort: { key: "health", dir: "asc" }, rowKey: r => r.id,
          title: `${rows.length} sources · ${bad} failing/degraded · ${never} never fetched`, rowClass: r => (r.enabled ? "" : "out"),
          columns: [
            { key: "name", label: "Source", fmt: (v, r) => h("a", { class: "lnk", href: r.url, target: "_blank", rel: "noopener external", title: r.url }, v) },
            { key: "category", label: "Category", cls: "dim" },
            { key: "trust_tier", label: "Tier", cls: "dim" },
            { key: "health", label: "Health", value: r => rank[r.health] ?? 5, csv: r => r.health, fmt: (v, r) => statusBadge(r.health, r.last_error || null) },
            { key: "last_ok_at", label: "Last OK", num: true, value: r => r.last_ok_at ? -new Date(r.last_ok_at) : null, csv: r => r.last_ok_at, fmt: (v, r) => r.last_ok_at ? fmt.age(r.last_ok_at) : h("span", { class: "dimmer" }, "never") },
            { key: "consecutive_failures", label: "Consec", num: true, desc: true, fmt: v => h("span", { class: v >= 3 ? "down" : v ? "ops-warn" : "dimmer" }, String(v)) },
            { key: "fetches_24h", label: "Fetch 24h", num: true, fmt: (v, r) => `${r.ok_fetches_24h}/${v}` },
            { key: "new_items_24h", label: "New 24h", num: true, desc: true },
            { key: "items_stored", label: "Stored", num: true, desc: true },
            { key: "avg_fetch_ms_24h", label: "Avg", num: true, fmt: v => v == null ? "–" : ms(v) },
            { key: "last_error", label: "Last error", cls: "wrap ops-err", fmt: v => v ? h("span", { title: v }, String(v).slice(0, 70)) : h("span", { class: "dimmer" }, "–") },
          ] }),
        h("p", { class: "note" }, "Ingestion failures from news_sources / news_fetch_log. ", h("a", { class: "lnk", href: "/methodology/news" }, "News methodology →")));
      }

      function refresh(force) {
        loadCore(force); loadQuarantine(force); loadIncidents(force); loadSchema(force); loadUnmapped(force); loadStale(force); loadNews(force);
      }
      refresh(false);
      ctx.every(REFRESH_MS, () => refresh(true));
    },
  });
})();
