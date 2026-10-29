/* /probes: OpenGrid's own probes of provider APIs, from every vantage point that reports.
   Raw results are stored (probe_results); this page shows the rollups and says, for every dip, WHO could not reach the
   provider (regional vs global), WHICH provider region it was (when the provider has regional endpoints), and WHY
   (the classified failure: dns, timeout, tls, http_5xx ...). Separate from /uptime (UptimeProject's data).
   Sources: /v1/probes/summary, /v1/probes/targets/{id}, /v1/probes/dips, /v1/probes/raw, /v1/probes/agents (in summary meta). */
(() => {
  const { h, fmt } = OG;
  const REFRESH_MS = 120000;
  const COLORS = ["#7aa2f7", "#9ece6a", "#e0af68", "#bb9af7", "#f7768e", "#73daca", "#ff9e64", "#2ac3de"];
  const STATUS_TONE = { up: "good", degraded: "warn", down: "bad", unconfirmed: "warn", unknown: "" };
  const statusBadge = s => (s === "up" ? h("span", { class: "dotx good" }, "up") : OG.badge(s || "unknown", STATUS_TONE[s] ?? "", s === "unknown" ? "no recent probe result" : s === "unconfirmed" ? "one vantage point failed and no other reported, so global vs local is unknown" : null));
  const ms = v => (v == null ? OG.na("no successful probe") : v >= 1000 ? (v / 1000).toFixed(2) + " s" : Math.round(v) + " ms");
  const avail = v => (v == null ? OG.na("no probe results in this window") : v >= 99.995 ? "100%" : v.toFixed(v >= 99 ? 3 : 2) + "%");
  const availCls = v => (v == null ? "" : v < 99 ? "t-bad" : v < 99.9 ? "t-warn" : "");
  const SCOPE_TONE = { global: "bad", regional: "warn", unconfirmed: "" };
  const SCOPE_TEXT = { global: "global", regional: "regional", unconfirmed: "unconfirmed" };
  const scopeBadge = (s, why) => OG.badge(SCOPE_TEXT[s] || s || "ok", SCOPE_TONE[s] ?? "", (why || "") + " (inferred)");
  const REGION_TEXT = { all_regions: "all regions", some_regions: "some regions" };
  const KIND_TIP = { api: "a real API route", host: "the API host answers; no route does anything unauthenticated", website: "the public website only: it can be up while the API is down" };
  const safeHref = u => (typeof u === "string" && /^https?:\/\/[^\s]/i.test(u.trim()) ? u.trim() : null);
  const reasons = e => Object.entries(e || {}).sort((a, b) => b[1] - a[1]).map(([k, n]) => `${k.replace(/_/g, " ")} ×${n}`).join(", ");
  const ago = v => (v ? fmt.age(v) + " ago" : "never");

  OG.page("/probes", {
    title: "Provider probes",
    async mount(el, params, query, ctx) {
      el.classList.add("up-page");
      const st = { provider: OG.qs.get("provider", ""), target: OG.qs.get("target", ""), hours: +OG.qs.get("hours", 24), rows: [], meta: null };
      const vantageEl = h("div", {}), tblBox = h("div", {}, OG.loading("Loading probes…")), detail = h("div", {});
      const hoursSeg = OG.seg([["6", "6H"], ["24", "24H"], ["168", "7D"], ["720", "30D"]], String(st.hours), v => {
        st.hours = +v; OG.qs.set({ hours: v === "24" ? null : v }); load();
      });
      el.append(OG.head("Provider probes",
        "OpenGrid's own probes of each provider's public API, run from every vantage point that reports. Every raw result is stored; this view is the rollup. Separate from the UptimeProject data on /uptime, and it says nothing about GPU capacity.",
        h("span", { class: "flt" }, h("span", {}, "Window"), hoursSeg)),
        vantageEl, tblBox, detail,
        h("p", { class: "note" }, h("a", { class: "lnk", href: "/methodology/probes" }, "Methodology →"), " · ", h("a", { class: "lnk", href: "/uptime" }, "UptimeProject data →")));

      // ------------------------------------------------------------ providers
      async function load() {
        try {
          const res = await ctx.api("/v1/probes/summary", { params: { hours: st.hours }, full: true, slot: "probes" });
          st.rows = res.data; st.meta = res.meta;
          OG.status.asOf(res.meta && res.meta.as_of);
        } catch (e) { if (!e.stale && !st.rows.length) tblBox.replaceChildren(OG.error(e, load)); return; }
        drawVantage(); drawTable();
        if (!st.provider || !st.rows.some(r => r.provider === st.provider)) st.provider = (st.rows.find(r => r.availability != null) || st.rows[0] || {}).provider || "";
        loadDetail();
      }

      function drawVantage() {
        const ag = (st.meta && st.meta.agents) || [];
        const items = ag.map(a => h("span", { class: "chip", title: `agent ${a.agent_id} · ${a.results_total} results stored · first seen ${fmt.dateTime(a.first_seen_at)}` }, a.location, h("span", { class: "dimmer" }, " " + ago(a.last_seen_at))));
        const locs = new Set(ag.map(a => a.location));
        vantageEl.replaceChildren(h("div", { class: "nw-chips" }, h("span", { class: "dim" }, `${locs.size} vantage point${locs.size === 1 ? "" : "s"}: `), ...items),
          locs.size < 2 ? h("p", { class: "note" }, locs.size ? "Only one location reports, so OpenGrid cannot tell a provider outage from a problem on the path to it: a failed check shows as \"unconfirmed\", never global or regional. Run probes/agent.py in other regions to add vantage points." : "No probe has reported yet. The first results arrive within a few minutes of the server starting.") : null);
      }

      function drawTable() {
        if (!st.rows.length) { tblBox.replaceChildren(OG.empty("No probe targets are configured.")); return; }
        const rows = st.rows.map(r => ({ ...r, display_name: OG.providerName(r.provider), g: r.targets.reduce((a, t) => a + t.slots.global, 0), rg: r.targets.reduce((a, t) => a + t.slots.regional, 0),
          errs: (() => { const e = {}; r.targets.forEach(t => Object.entries(t.error_classes || {}).forEach(([k, n]) => { e[k] = (e[k] || 0) + n; })); return e; })() }));
        const cols = [
          { key: "display_name", label: "Provider", fmt: (v, r) => OG.providerLink(r.provider), value: r => r.display_name },
          { key: "status", label: "Now", fmt: v => statusBadge(v), title: "From the latest probe of each target in the last 20 minutes" },
          { key: "availability", label: "Availability", num: true, desc: true, fmt: v => avail(v), cls: r => availCls(r.availability), title: "Share of checks that succeeded across every vantage point, the lowest across the provider's targets" },
          { key: "latency_p50_ms", label: "Latency p50", num: true, fmt: v => ms(v), title: "Median of hourly p50 time-to-response-headers, across vantage points" },
          { key: "kinds", label: "Probes", sort: false, fmt: (v, r) => h("span", {}, v.map((k, i) => [i ? " " : "", h("span", { class: "dim", title: KIND_TIP[k] }, k)])) },
          { key: "locations", label: "Vantage", num: true, value: r => r.locations.length, fmt: (v, r) => (r.locations.length ? String(r.locations.length) : OG.na("no results yet")) },
          { key: "g", label: "Global slots", num: true, desc: true, title: "Probe intervals where every reporting vantage point failed (at least two reported)" },
          { key: "rg", label: "Regional slots", num: true, desc: true, title: "Probe intervals where some vantage points failed and others succeeded" },
          { key: "errs", label: "Failure reasons", sort: false, fmt: v => (Object.keys(v).length ? reasons(v) : h("span", { class: "dim" }, "none")) },
        ];
        const tbl = OG.table({ columns: cols, rows, sort: { key: "availability", dir: "asc" }, csv: "provider-probes", rowKey: r => r.provider,
          rowClass: r => (r.provider === st.provider ? "sel" : ""),
          onRow: r => { st.provider = r.provider; st.target = ""; OG.qs.set({ provider: r.provider, target: null }); drawTable(); loadDetail(); } });
        tblBox.replaceChildren(tbl);
      }

      // ------------------------------------------------------------ one provider
      let chart = null;
      async function loadDetail() {
        const row = st.rows.find(r => r.provider === st.provider);
        if (!row) { detail.replaceChildren(); return; }
        if (!row.targets.some(t => t.id === st.target)) st.target = row.targets[0].id;
        OG.qs.set({ provider: st.provider, target: st.target });
        const tSeg = row.targets.length > 1 ? OG.seg(row.targets.map(t => [t.id, t.region || t.name]), st.target, v => { st.target = v; loadDetail(); }) : null;
        detail.replaceChildren(OG.section(OG.providerName(row.provider), tSeg, OG.loading("Loading probe history…")));
        let d, raw = [];
        try {
          [d, raw] = await Promise.all([
            ctx.api("/v1/probes/targets/" + encodeURIComponent(st.target), { params: { hours: st.hours }, full: true, slot: "probe-t" }).then(r => r.data),
            ctx.api("/v1/probes/raw", { params: { target: st.target, limit: 60 }, full: true, slot: "probe-raw" }).then(r => r.data)]);
        } catch (e) { if (!e.stale) detail.replaceChildren(OG.section(OG.providerName(row.provider), OG.error(e, loadDetail))); return; }
        drawDetail(row, d, raw, tSeg);
      }

      function drawDetail(row, d, raw, tSeg) {
        const locs = Object.keys(d.by_location).sort(), chartEl = h("div", {});
        const latest = {};
        raw.forEach(r => { if (!latest[r.location]) latest[r.location] = r; });   // raw is newest first
        const latestRows = Object.values(latest);
        const phase = v => (v == null ? OG.na("phase did not happen") : Math.round(v) + " ms");
        const latTbl = latestRows.length ? OG.table({ compact: true, rows: latestRows, sort: { key: "location", dir: "asc" }, columns: [
          { key: "location", label: "Vantage point" },
          { key: "ok", label: "Latest", fmt: (v, r) => (v ? h("span", { class: "dotx good" }, "up · " + r.status_code) : OG.badge(r.error_class || "failed", "bad", r.error || "")) },
          { key: "observed_at", label: "At", fmt: v => fmt.dateTime(v) },
          { key: "dns_ms", label: "DNS", num: true, fmt: phase }, { key: "connect_ms", label: "TCP", num: true, fmt: phase },
          { key: "tls_ms", label: "TLS", num: true, fmt: phase }, { key: "ttfb_ms", label: "First byte", num: true, fmt: phase },
          { key: "total_ms", label: "Total", num: true, fmt: phase },
        ] }) : OG.empty("No raw results for this target yet.");
        const t = d;
        detail.replaceChildren(OG.section(`${OG.providerName(row.provider)}: ${t.name}${t.region ? " · " + t.region : ""}`, tSeg,
          h("div", { class: "dim" }, `${t.kind} probe · counts ${t.accept.join(" / ")} as up · ${t.hourly.length} hourly buckets · ${locs.length} vantage point${locs.length === 1 ? "" : "s"}`),
          h("p", { class: "note", title: KIND_TIP[t.kind] }, t.kind === "website" ? "This is the provider's website only: it can stay up while the API is down." : t.kind === "host" ? "Only the API host's web tier is checked: no unauthenticated route does anything useful." : "A real API route; the declared status codes prove the service is answering." + (t.note ? " " + t.note : "")),
          locs.length ? chartEl : OG.empty("No hourly rollups in this window yet. Rollups run every 5 minutes."),
          h("div", { class: "lbl" }, "Latest probe by vantage point"), latTbl,
          h("div", { class: "lbl", title: "Every hour with at least one failed check" }, "Dips"), dipsEl(),
          h("div", { class: "lbl" }, "Recent failed checks (raw)"), failTbl(d.recent_failures),
          h("p", { class: "note" }, "Target URL: ", h("span", { class: "mono" }, t.url))));
        if (chart) { chart.destroy && chart.destroy(); chart = null; }
        if (locs.length) {
          const times = [...new Set(locs.flatMap(l => d.by_location[l].map(p => p.bucket)))].sort();
          chart = OG.charts.timeseries(chartEl, { times, height: 200, yZero: true, yFmt: v => Math.round(v) + " ms", label: `${t.name} p50 latency by vantage point`,
            series: locs.map((l, i) => { const m = new Map(d.by_location[l].map(p => [p.bucket, p.latency_p50_ms])); return { key: l, label: l, values: times.map(x => m.get(x) ?? null), color: COLORS[i % COLORS.length], step: false, width: 1.6 }; }),
            events: t.hourly.filter(p => p.scope === "global" || p.scope === "regional").map(p => ({ t: p.bucket, label: `${p.scope}: ${reasons(p.error_classes) || "failed checks"}`, kind: p.scope, severity: p.scope === "global" ? "major" : "notable" })) });
        }
        fillDips(row);
      }

      const dipsBox = h("div", {});
      function dipsEl() { dipsBox.replaceChildren(OG.loading("Loading dips…")); return dipsBox; }
      function failTbl(rows) {
        if (!rows || !rows.length) return OG.empty("No failed checks stored for this target.");
        return OG.table({ compact: true, rows, sort: { key: "observed_at", dir: "desc" }, limit: 20, columns: [
          { key: "observed_at", label: "When", fmt: v => fmt.dateTime(v), desc: true }, { key: "location", label: "Vantage point" },
          { key: "error_class", label: "Reason", fmt: v => OG.badge((v || "failed").replace(/_/g, " "), "bad") },
          { key: "status_code", label: "HTTP", num: true }, { key: "error", label: "Detail", ell: 320 },
        ] });
      }

      async function fillDips(row) {
        let res;
        const days = Math.max(1, Math.min(30, Math.ceil(st.hours / 24)));
        try { res = await ctx.api("/v1/probes/dips", { params: { provider: row.provider, days }, full: true, slot: "probe-dips" }); }
        catch (e) { if (!e.stale) dipsBox.replaceChildren(OG.error(e, () => fillDips(row))); return; }
        const rows = res.data.filter(r => r.target_id === st.target);
        if (!rows.length) { dipsBox.replaceChildren(OG.empty("No failed checks for this target in the last " + days + " day" + (days === 1 ? "" : "s") + ".")); return; }
        dipsBox.replaceChildren(
          h("p", { class: "note" }, "Scope is by vantage point: ", h("b", {}, "global"), " every reporting location failed (at least two reported); ", h("b", {}, "regional"), " some failed while others succeeded; ",
            h("b", {}, "unconfirmed"), " only one location reported. For providers with several regional endpoints, ", h("b", {}, "provider region"), " says whether other regions dipped in the same hour."),
          OG.table({ compact: true, rows, sort: { key: "bucket", dir: "desc" }, limit: 200, csv: "probe-dips", columns: [
            { key: "bucket", label: "Hour", desc: true, fmt: v => fmt.dateTime(v) },
            { key: "scope", label: "Scope", fmt: (v, r) => scopeBadge(v, r.scope_basis) },
            { key: "region_scope", label: "Provider region", sort: false, fmt: (v, r) => (v ? OG.badge(REGION_TEXT[v] || v, v === "all_regions" ? "bad" : "warn", (r.region_basis || "") + " (inferred)") : h("span", { class: "dim", title: "this provider has fewer than two regional endpoints, or this target is not regional" }, "-")) },
            { key: "failed_checks", label: "Failed", num: true, fmt: (v, r) => `${v} of ${r.checks}` },
            { key: "availability", label: "Availability", num: true, fmt: v => avail(v) },
            { key: "failing_locations", label: "Failing from", sort: false, fmt: v => Object.entries(v || {}).map(([l, n]) => `${l} ×${n}`).join(", ") || "-" },
            { key: "error_classes", label: "Reason", sort: false, fmt: v => reasons(v) || "-" },
            { key: "reported", label: "Provider reported", sort: false, fmt: v => (v && v.length ? h("span", {}, v.map((i, k) => [k ? h("br") : null,
              safeHref(i.url) ? h("a", { class: "lnk", href: safeHref(i.url), target: "_blank", rel: "noopener noreferrer", title: i.title }, i.title.length > 44 ? i.title.slice(0, 43) + "…" : i.title) : h("span", {}, i.title), " ", OG.badge(i.scope, SCOPE_TONE[i.scope] ?? "", "reported by the provider (inferred scope)")])) : h("span", { class: "dim", title: "No incident on the provider's status page overlaps this hour" }, "none")) },
          ] }));
      }

      await load();
      ctx.every(REFRESH_MS, load);
    },
  });
})();
