/* /uptime: external control-plane uptime and latency, measured by UptimeProject (CC BY 4.0).
   A separate dataset on purpose: these are third-party probes of each provider's public API, not OpenGrid's own
   launches, and nothing here feeds a score or a route.
   Sources: /v1/uptime/providers (one row per tracked provider that UptimeProject measures),
   /v1/uptime/services/{id} (checks, hourly history, per-probe latency). The selected provider/service live in the
   URL (/uptime?provider=aws&service=aws-ec2-us-east-1). */
(() => {
  const { h, fmt } = OG;
  const REFRESH_MS = 300000;
  // UptimeProject's fixed probe fleet (uptimeproject.org/methodology): no probe runs on a provider it measures.
  const PROBE_LOC = { "do-nyc3": "New York (DigitalOcean)", "hzr-fsn1": "Falkenstein (Hetzner)", "ovh-rbx": "Roubaix (OVH)",
    "vlr-sea": "Seattle (Vultr)", "vlr-sgp": "Singapore (Vultr)" };
  const STATUS_TONE = { up: "good", degraded: "warn", down: "bad", unknown: "" };
  const statusBadge = s => (s === "up" ? h("span", { class: "dotx good" }, "up") : OG.badge(s || "n/a", STATUS_TONE[s] ?? "", s ? null : "no status from UptimeProject"));
  const ms = v => (v == null ? OG.na("no latency samples") : v >= 1000 ? (v / 1000).toFixed(2) + " s" : Math.round(v) + " ms");
  // availability is already a percentage (99.97), unlike OpenGrid's other fractions
  const avail = v => (v == null ? OG.na("no data in this window") : v >= 99.995 ? "100%" : v.toFixed(v >= 99 ? 3 : 2) + "%");
  const availCls = v => (v == null ? "" : v < 99 ? "t-bad" : v < 99.9 ? "t-warn" : "");

  // Two kinds of scope, never mixed: MEASURED (from the probes, on a dip) and REPORTED (the provider's own words, on an incident).
  const SCOPE_TONE = { global: "bad", regional: "warn", partial: "", unspecified: "" };
  const scopeBadge = (scope, why, regions) => OG.badge(scope + (regions && regions.length ? ": " + regions.slice(0, 3).join(", ") + (regions.length > 3 ? " +" + (regions.length - 3) : "") : ""),
    SCOPE_TONE[scope] ?? "", (why || "") + " (inferred)");
  const IMPACT_TONE = { critical: "bad", major: "bad", service_outage: "bad", minor: "warn", service_disruption: "warn" };
  const impactBadge = i => (i ? OG.badge(String(i).replace(/_/g, " "), IMPACT_TONE[i] ?? "", "impact the provider assigned") : null);
  const dur = (a, b) => { if (!b) return "ongoing"; const m = Math.round((new Date(b) - new Date(a)) / 60000); return m < 60 ? m + " min" : m < 2880 ? (m / 60).toFixed(m % 60 ? 1 : 0) + " h" : Math.round(m / 1440) + " d"; };
  const daysFor = hours => (hours <= 24 ? 1 : hours <= 168 ? 7 : hours <= 720 ? 30 : 90);
  // Feed and status-page links are third-party data: only absolute http(s) URLs become links (no javascript:/data:).
  const safeHref = u => (typeof u === "string" && /^https?:\/\/[^\s]/i.test(u.trim()) ? u.trim() : null);
  const probeName = id => (PROBE_LOC[id] || id).replace(/ \(.*\)$/, "");

  OG.page("/uptime", {
    title: "Provider uptime",
    async mount(el, params, query, ctx) {
      el.classList.add("up-page");
      const st = { provider: OG.qs.get("provider", ""), service: OG.qs.get("service", ""), hours: +OG.qs.get("hours", 168), rows: [], meta: null };
      const tblBox = h("div", {}, OG.loading("Loading uptime…")), detail = h("div", {}), noteEl = h("div", {});
      const hoursSeg = OG.seg([["24", "24H"], ["168", "7D"], ["720", "30D"], ["2160", "90D"]], String(st.hours), v => {
        st.hours = +v; OG.qs.set({ hours: v === "168" ? null : v }); loadDetail();
      });
      el.append(OG.head("Provider uptime",
        "Third-party probes of each provider's public API from five fixed locations. Separate from OpenGrid's own launch data, and it says nothing about GPU capacity or whether a launch would succeed.",
        h("span", { class: "flt" }, h("span", {}, "Window"), hoursSeg)),
        noteEl, tblBox, detail,
        h("p", { class: "note" }, "Source: ", h("a", { class: "lnk", href: "https://uptimeproject.org", target: "_blank", rel: "noopener noreferrer" }, "UptimeProject"),
          ", licensed CC BY 4.0. ", h("a", { class: "lnk", href: "/methodology/uptime" }, "Methodology →")));

      // ------------------------------------------------------------ providers table
      let tbl = null;
      async function load() {
        try {
          const res = await ctx.api("/v1/uptime/providers", { full: true, slot: "uptime" });
          st.rows = res.data; st.meta = res.meta;
          OG.status.asOf(res.meta && res.meta.as_of);
        } catch (e) { if (!e.stale && !st.rows.length) tblBox.replaceChildren(OG.error(e, load)); return; }
        drawNote(); drawTable();
        if (!st.provider || !st.rows.some(r => r.provider === st.provider)) st.provider = (st.rows.find(r => r.measured !== false) || st.rows[0] || {}).provider || "";
        loadDetail();
      }

      function drawNote() {
        const m = st.meta || {}, hl = m.health || {}, bits = [];
        bits.push(`${m.covered ?? st.rows.length} of ${m.tracked_providers ?? "?"} tracked providers are measured.`);
        if (hl.last_ok_at) bits.push(`Last polled ${fmt.age(hl.last_ok_at)} ago.`);
        else bits.push("Not polled yet.");
        if (hl.last_ok === false) bits.push("The last poll failed: " + (hl.last_error || "no detail") + ".");
        if (hl.history_since) bits.push(`Hourly history since ${fmt.date(hl.history_since)}.`);
        const miss = (m.not_covered || []).length;
        if (miss) bits.push(`${miss} have no UptimeProject probe and show "not measured" rather than a guess.`);
        noteEl.replaceChildren(h("p", { class: "note" }, bits.join(" ")));
      }

      function drawTable() {
        const rows = st.rows.map(r => ({ ...r, measured: r.measured !== false, a24: r.availability["24h"], a7: r.availability["7d"], a30: r.availability["30d"], a90: r.availability["90d"] }));
        const cols = [
          { key: "measured", label: "Probe", hidden: true, value: r => (r.measured ? 1 : 0) },
          { key: "display_name", label: "Provider", fmt: (v, r) => OG.providerLink(r.provider), value: r => r.display_name },
          { key: "status", label: "Status", fmt: (v, r) => (r.measured === false ? h("span", { class: "dim", title: "UptimeProject has no probe for this provider, so there is nothing to show" }, "not measured") : statusBadge(v)), title: "Worst current status across the provider's compute services" },
          ...[["a24", "24h"], ["a7", "7d"], ["a30", "30d"], ["a90", "90d"]].map(([k, l]) => ({ key: k, label: l, num: true, desc: true,
            fmt: v => avail(v), cls: r => availCls(r[k]), title: `Availability over ${l}: the lowest across the provider's compute services, so one bad region is not averaged away` })),
          { key: "latency_p50_ms_24h", label: "Latency p50 24h", num: true, fmt: v => ms(v), title: "Median of hourly cross-probe p50 response time, averaged across the provider's compute services. Five locations on three continents are blended." },
          { key: "down_minutes_24h", label: "Down min 24h", num: true, fmt: v => (v == null ? OG.na() : String(v)) },
          { key: "inc", label: "Reported incidents 30d", num: true, desc: true, value: r => (r.incidents_30d ? r.incidents_30d.total : -1),
            fmt: (v, r) => (!r.incidents_30d ? OG.na("OpenGrid reads no status feed for this provider") : h("span", { title: `${r.incidents_30d.global} global · ${r.incidents_30d.regional} regional · ${r.incidents_30d.unspecified} region not stated (as reported by the provider)` }, String(r.incidents_30d.total))),
            title: "Incidents the provider reported on its own status page. Reported, not measured; providers with no feed show n/a." },
          { key: "n", label: "Services", num: true, value: r => r.services.length, fmt: (v, r) => (r.services.length ? String(r.services.length) : OG.na("no probe")) },
        ];
        if (!rows.length) { tblBox.replaceChildren(OG.empty("No data yet. The first poll runs shortly after the server starts.")); return; }
        tbl = OG.table({ columns: cols, rows, sort: { key: "measured", dir: "desc" }, csv: "provider-uptime",
          rowKey: r => r.provider, rowClass: r => (r.provider === st.provider ? "sel" : ""),
          onRow: r => { st.provider = r.provider; st.service = ""; OG.qs.set({ provider: r.provider, service: null }); drawTable(); loadDetail(); } });
        tblBox.replaceChildren(tbl);
      }

      // ------------------------------------------------------------ one provider / service
      let chart = null;
      async function loadDetail() {
        const row = st.rows.find(r => r.provider === st.provider);
        if (!row) { detail.replaceChildren(); return; }
        if (!row.services.length) {
          OG.qs.set({ provider: st.provider, service: null });
          const box = h("div", {});
          detail.replaceChildren(OG.section(row.display_name, OG.empty("UptimeProject has no probe for " + row.display_name + ", so there is no measured uptime or latency to show."),
            row.incidents_30d ? h("div", {}, h("div", { class: "lbl" }, "Reported incidents"), box) : h("p", { class: "note" }, "OpenGrid reads no status feed for this provider either.")));
          if (row.incidents_30d) fillIncidents(box, row);
          return;
        }
        if (!row.services.some(s => s.id === st.service)) st.service = row.services[0].id;
        OG.qs.set({ provider: st.provider, service: st.service });
        const svcSeg = row.services.length > 1
          ? OG.seg(row.services.map(s => [s.id, s.region || s.name]), st.service, v => { st.service = v; loadDetail(); }) : null;
        detail.replaceChildren(OG.section(`${row.display_name}: ${row.services.find(s => s.id === st.service).name}`, svcSeg, OG.loading("Loading history…")));
        let d;
        try { d = (await ctx.api("/v1/uptime/services/" + encodeURIComponent(st.service), { params: { hours: st.hours }, full: true, slot: "uptime-svc" })).data; }
        catch (e) { if (!e.stale) detail.replaceChildren(OG.section(row.display_name, OG.error(e, loadDetail))); return; }
        drawDetail(row, d, svcSeg);
      }

      function drawDetail(row, d, svcSeg) {
        const daily = st.hours >= 720, hist = (daily ? d.daily : d.history) || [], chartEl = h("div", {});
        const dipsEl = h("div", {}, OG.loading("Loading dips…")), incEl = h("div", {}, OG.loading("Loading incidents…"));
        const lat = hist.filter(p => p.latency_p50_ms != null).length;
        const down = hist.reduce((a, p) => a + (p.down_minutes || 0), 0), deg = hist.reduce((a, p) => a + (p.degraded_minutes || 0), 0);
        const probes = d.probes || [];
        const probeTbl = probes.length ? OG.table({ compact: true, rows: probes, sort: { key: "latency_ms", dir: "desc" }, limit: 20,
          columns: [
            { key: "probe_id", label: "Probe", fmt: v => PROBE_LOC[v] || v, title: "Fixed UptimeProject probe location", value: r => PROBE_LOC[r.probe_id] || r.probe_id },
            { key: "status", label: "Status", fmt: v => statusBadge(v) },
            { key: "latency_ms", label: "Latest", num: true, desc: true, fmt: v => ms(v) },
            { key: "latency_min_ms", label: "Min (our polls)", num: true, fmt: v => ms(v) },
            { key: "latency_max_ms", label: "Max (our polls)", num: true, fmt: v => ms(v) },
            { key: "samples", label: "Polls, 24h", num: true },
          ] }) : OG.empty("No per-probe samples yet. They accumulate from OpenGrid's first poll onward; UptimeProject's history endpoint only has the cross-probe median.");
        const checks = (d.checks || []).map(c => h("tr", {}, h("td", { class: "mono" }, c.check_type), h("td", { class: "mono ell" }, c.target || "-"),
          h("td", { class: "dim" }, c.interval_sec ? `every ${c.interval_sec >= 3600 ? c.interval_sec / 3600 + "h" : c.interval_sec >= 60 ? c.interval_sec / 60 + "m" : c.interval_sec + "s"}` : "-")));
        detail.replaceChildren(OG.section(`${row.display_name}: ${d.name}${d.region ? " · " + d.region : ""}`,
          svcSeg,
          h("div", { class: "dim" }, `${d.status || "status unknown"} now · ${hist.length} ${daily ? "daily" : "hourly"} buckets · ${down} min down, ${deg} min degraded in the window`),
          lat ? chartEl : OG.empty("No latency buckets in this window yet."),
          h("div", { class: "lbl" }, "Per-probe latency"), probeTbl,
          h("div", { class: "lbl", title: "Every hour (or day, for 30D and 90D) below 100%" }, "Dips below 100%"), dipsEl,
          h("div", { class: "lbl" }, "Incidents the provider reported"), incEl,
          h("details", { class: "sec" }, h("summary", { class: "sec-h" }, "What is probed"),
            checks.length ? h("table", { class: "pv-kv" }, h("tbody", {}, checks)) : h("p", { class: "note" }, "No check details yet."),
            h("p", { class: "note" }, "Unauthenticated requests to the provider's public API. A response with the declared status code counts as up; this cannot show whether authenticated operations such as launching an instance work.")),
          safeHref(d.status_page_url) ? h("p", { class: "note" }, "Provider status page: ", h("a", { class: "lnk", href: safeHref(d.status_page_url), target: "_blank", rel: "noopener noreferrer" }, d.status_page_url)) : null));
        fillDips(dipsEl, row);
        fillIncidents(incEl, row);
        if (chart) { chart.destroy && chart.destroy(); chart = null; }
        if (lat) {
          chart = OG.charts.timeseries(chartEl, { times: hist.map(p => p.bucket), height: 200, yZero: true, yFmt: v => Math.round(v) + " ms",
            label: `${d.name} p50 latency`, legend: false,
            series: [{ key: "lat", label: "p50 latency (all probes)", values: hist.map(p => p.latency_p50_ms), color: "#7aa2f7", step: false, width: 1.6 }],
            events: hist.filter(p => (p.down_minutes || 0) > 0).map(p => ({ t: p.bucket, label: `${p.down_minutes} min down`, kind: "down", severity: "major" })) });
        }
      }

      // MEASURED dips: scope comes from how many probes failed and how many of the provider's measured regions dipped
      async function fillDips(box, row) {
        let res;
        try { res = await ctx.api("/v1/uptime/dips", { params: { provider: row.provider, days: daysFor(st.hours), service: st.service }, full: true, slot: "uptime-dips" }); }
        catch (e) { if (!e.stale) box.replaceChildren(OG.error(e, () => fillDips(box, row))); return; }
        const rows = res.data;
        if (!rows.length) { box.replaceChildren(OG.empty("No dips in this window: every bucket was 100%.")); return; }
        const hourly = res.meta.resolution === "hour";
        const counts = rows.reduce((a, r) => (a[r.scope] = (a[r.scope] || 0) + 1, a), {});
        box.replaceChildren(
          h("p", { class: "note" }, `${rows.length} ${hourly ? "hours" : "days"} below 100%: ` + ["global", "regional", "partial"].filter(k => counts[k]).map(k => `${counts[k]} ${k}`).join(" · ") + ". ",
            "Scope is inferred from the probes: ", h("b", {}, "global"), " every probe failed (and every measured region dipped, or the provider has one measured endpoint); ",
            h("b", {}, "regional"), " every probe failed for some measured regions only; ", h("b", {}, "partial"), " only some probes failed, which can be a network path rather than the provider."),
          OG.table({ compact: true, rows, sort: { key: "bucket", dir: "desc" }, limit: 200, csv: "uptime-dips", columns: [
            { key: "bucket", label: hourly ? "Hour (UTC)" : "Day", desc: true, fmt: v => (hourly ? fmt.dateTime(v) : fmt.date(v)) },
            { key: "scope", label: "Scope", fmt: (v, r) => scopeBadge(v, r.scope_basis), title: "Measured by the probes, inferred by rule. Hover a badge for the reason." },
            { key: "kind", label: "Kind", fmt: v => (v === "down" ? OG.badge("down", "bad", "every probe failed") : v === "mixed" ? OG.badge("mixed", "warn") : OG.badge("degraded", "warn", "only some probes failed")) },
            { key: "down_minutes", label: "Down min", num: true },
            { key: "degraded_minutes", label: "Degraded min", num: true },
            { key: "availability_pct", label: "Availability", num: true, fmt: v => avail(v) },
            { key: "probes_failing", label: "Probes failing", sort: false, fmt: v => (v && v.length ? v.map(probeName).join(", ") : h("span", { class: "dim", title: "Per-probe detail is recorded only while OpenGrid is polling" }, "-")) },
            { key: "reported", label: "Provider reported", sort: false, fmt: v => (v && v.length ? h("span", {}, v.map((i, k) => [k ? h("br") : null,
              (safeHref(i.url) ? h("a", { class: "lnk", href: safeHref(i.url), target: "_blank", rel: "noopener noreferrer", title: i.title }, (i.title.length > 48 ? i.title.slice(0, 47) + "…" : i.title)) : h("span", { title: i.title }, i.title.length > 48 ? i.title.slice(0, 47) + "…" : i.title)), " ", scopeBadge(i.scope, "reported by the provider", i.regions)])) : h("span", { class: "dim", title: "No incident on the provider's status page overlaps this dip" }, "none")) },
          ] }),
          h("p", { class: "note" }, "A reported incident that overlaps a dip is related in time, not proven to be its cause. Providers under-report, and a regional incident is not attached to a dip in a different region."));
      }

      // REPORTED incidents: the provider's own status page, with its update text
      async function fillIncidents(box, row) {
        let res;
        try { res = await ctx.api("/v1/uptime/incidents", { params: { provider: row.provider, days: Math.max(30, daysFor(st.hours)) }, full: true, slot: "uptime-inc-" + row.provider }); }
        catch (e) { if (!e.stale) box.replaceChildren(OG.error(e, () => fillIncidents(box, row))); return; }
        const rows = res.data;
        if (!row.incidents_30d && !rows.length) { box.replaceChildren(h("p", { class: "note" }, "OpenGrid reads no status feed for " + row.display_name + ", so there is nothing reported to show.")); return; }
        if (!rows.length) { box.replaceChildren(OG.empty("The provider reported no incidents in this window.")); return; }
        box.replaceChildren(h("p", { class: "note" }, "Reported by the provider, not measured. Scope is inferred from the regions and components the incident names."),
          ...rows.slice(0, 40).map(i => h("details", { class: "sec" },
            h("summary", { class: "sec-h" }, fmt.date(i.started_at) + "  ", scopeBadge(i.scope, i.scope_basis, i.regions), " ", impactBadge(i.impact), "  " + i.title,
              h("span", { class: "dim" }, "  · " + dur(i.started_at, i.resolved_at))),
            i.components.length ? h("p", { class: "note" }, "Affected: " + i.components.slice(0, 8).join(", ") + (i.components.length > 8 ? ` +${i.components.length - 8} more` : "")) : null,
            ...(i.updates || []).map(u => h("p", { class: "note" }, h("span", { class: "mono" }, fmt.dateTime(u.at) + " "), h("b", {}, (u.status || "") + " "), String(u.body || "").replace(/<[^>]+>/g, " ").replace(/\*\*|#{2,}/g, "").replace(/\s+/g, " ").trim().slice(0, 600))),
            safeHref(i.url) ? h("p", { class: "note" }, h("a", { class: "lnk", href: safeHref(i.url), target: "_blank", rel: "noopener noreferrer" }, "Open on the provider's status page →")) : null)));
      }

      await load();
      ctx.every(REFRESH_MS, load);
    },
  });
})();
