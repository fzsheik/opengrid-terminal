/* /providers — provider league table.
   Sources: /v1/providers (coverage, relative value over a window, feed health, facts — all from the
   hourly rollup, so a figure is null + reason until it has enough compared hours), /v1/capabilities
   (integration level: what OpenGrid implements vs what is verified live — verified_live is false
   everywhere and the page says so), /v1/trust/providers (soft: feed status from the quality layer).
   Also exports OG.views.integration / OG.views.share / OG.views.feedStatus for pages/provider.js. */
(() => {
  const { h, fmt } = OG;
  const CLASSES = ["hyperscaler", "neocloud", "general_cloud", "marketplace", "decentralized", "unclassified"];
  const SRC = { authenticated_api: "auth API", public_api: "public API", public_page: "public page", aggregator: "aggregator", public_pricing_file: "price file" };

  // Unsigned share of hours / listings: 0.234 -> "23%"
  const share = v => (v == null || !isFinite(v) ? "–" : (v * 100 >= 9.95 || v === 0 ? Math.round(v * 100) : (v * 100).toFixed(1)) + "%");
  const LEVEL_SHORT = { 0: "data only", 1: "avail. check", 2: "provision", 3: "lifecycle" };

  // Integration cell: the implemented level, with the three separate claims on hover. Never "live".
  function integration(cap, opts) {
    if (!cap) return OG.na("capability registry unavailable");
    const impl = cap.level_implemented, api = cap.level_supported_by_provider_api;
    const lines = [
      `OpenGrid implements: L${impl} ${cap.level_implemented_label}`,
      `Provider API documents: ${api == null ? "not established" : "L" + api + " " + cap.level_supported_label}${cap.docs_checked ? " (" + cap.docs_checked + ")" : ""}`,
      "Verified live against a real account: no (adapters tested against mocked HTTP)",
      `Availability check verified live: ${cap.availability_check_verified_live ? "yes (read-only, public endpoint)" : "no"}`,
      cap.via ? `Routed via ${cap.via} (aggregator; ${cap.via} is the counterparty)` : null,
      `OpenGrid credentials configured on this server: ${cap.credentials_configured ? "yes" : "no"}`,
    ].filter(Boolean).join("\n");
    return h("span", { class: "pv-int l" + impl, title: lines },
      h("b", {}, "L" + impl), " ", LEVEL_SHORT[impl] || "",
      impl > 0 ? h("span", { class: "pv-unv" }, " ·mock") : null,
      opts && opts.long && cap.via ? h("span", { class: "dim" }, " via " + cap.via) : null);
  }
  // Feed status: the quality layer's word if we have it, else derived from the last 24h of fetches.
  function feedStatus(trust, fh) {
    if (trust && trust.status) {
      const tone = trust.status === "healthy" ? "good" : trust.status === "degraded" ? "warn" : "bad";
      return OG.badge(trust.status, tone, (trust.status_reasons || []).join("; ") || `p50 ${fmt.num(trust.latency_ms_p50_24h, 0)} ms · ${trust.fetches_24h} fetches / 24h`);
    }
    if (!fh) return h("span", { class: "dim", title: "OpenGrid has no fetch recorded for this provider on this server" }, "no feed");
    if (fh.failure_rate_24h == null) return OG.badge("idle", "warn", fh.reason || "no fetches in the last 24h");
    return fh.failure_rate_24h === 0 ? OG.badge("ok", "good", `${fh.fetches_24h} fetches / 24h, none failed`)
      : OG.badge(share(fh.failure_rate_24h) + " fail", fh.failure_rate_24h > 0.2 ? "bad" : "warn", `${fh.fetches_24h} fetches / 24h`);
  }
  OG.views = OG.views || {};
  Object.assign(OG.views, { integration, share, feedStatus, providerSourceLabel: s => SRC[s] || s || "–" });

  OG.page("/providers", {
    title: "Providers",
    async mount(el, params, query, ctx) {
      const st = { days: +OG.qs.get("days", 30), cls: OG.qs.get("class", "all"), sort: OG.qs.get("sort", "listings"), dir: OG.qs.get("dir", "desc"), data: null, caps: null, trust: null };
      if (![7, 30, 90].includes(st.days)) st.days = 30;
      el.classList.add("pv-page");
      const statsEl = h("div", {}, OG.stats([{ label: "Providers", value: null, reason: "loading" }]));
      const tblBox = h("div", {}, OG.loading("Loading providers…"));
      const factsEl = h("div", { class: "box box-p pv-facts" });
      const capsNote = h("p", { class: "note" });
      const clsSeg = h("div", { class: "pv-hold" });
      const daySeg = OG.seg([["7", "7D"], ["30", "30D"], ["90", "90D"]], String(st.days), v => { st.days = +v; OG.qs.set({ days: v === "30" ? null : v }); load(); });
      el.append(OG.head("Providers", "Every GPU cloud OpenGrid reads: coverage, pricing vs the rest of the market, feed health and how far OpenGrid can route to it",
        h("span", { class: "flt" }, h("span", {}, "Class"), clsSeg), h("span", { class: "flt" }, h("span", {}, "Window"), daySeg)),
        statsEl, tblBox, capsNote,
        h("div", { class: "cols-2 pv-lower" }, OG.section("Facts", factsEl), OG.section("Integration levels", levelsBox())));

      function levelsBox() {
        return h("div", { class: "box box-p pv-levels" },
          h("table", { class: "pv-kv" }, h("tbody", {}, [[0, "market data only", "OpenGrid reads prices; cannot check stock live, quote live or launch"],
            [1, "availability check", "a live per-request stock check"], [2, "provisioning", "OpenGrid can launch an instance through the provider API"],
            [3, "full lifecycle", "launch, inspect status, terminate (stop where the API has it)"]].map(([n, l, d]) =>
            h("tr", {}, h("td", { class: "mono" }, "L" + n), h("td", {}, l), h("td", { class: "dim" }, d))))),
          h("p", { class: "note" }, h("b", {}, "·mock"), " = implemented in OpenGrid's adapter and tested against mocked HTTP built from the provider's documented API. ",
            "No adapter has been run against a real provider account yet (verified_live = false for every provider), so no provisioning claim here is verified. ",
            h("a", { class: "lnk", href: "/methodology/routing" }, "Routing methodology →")));
      }

      function drawClassSeg(rows) {
        const present = CLASSES.filter(c => rows.some(r => r.meta && r.meta.provider_class === c));
        const opts = [["all", "All"], ...present.map(c => [c, c.replace("_", " ")])];
        if (!opts.some(o => o[0] === st.cls)) st.cls = "all";
        clsSeg.replaceChildren(OG.seg(opts, st.cls, v => { st.cls = v; OG.qs.set({ class: v === "all" ? null : v }); draw(); }));
      }

      async function load() {
        tblBox.replaceChildren(OG.loading("Loading providers…"));
        try {
          const [res, caps, trust] = await Promise.all([
            ctx.api("/v1/providers", { params: { days: st.days }, full: true, slot: "providers" }),
            st.caps ? Promise.resolve(st.caps) : ctx.api("/v1/capabilities").catch(() => null),
            ctx.api("/v1/trust/providers").catch(() => null), OG.data.intervals()]);
          st.data = res.data; st.meta = res.meta; st.caps = caps; st.trust = trust;
          OG.status.asOf(res.meta && res.meta.as_of);
        } catch (e) { if (!e.stale) tblBox.replaceChildren(OG.error(e, load)); return; }
        draw();
      }

      function rowsOf() {
        const capBy = new Map((st.caps || []).map(c => [c.provider, c]));
        const trBy = new Map((st.trust || []).map(t => [t.provider, t]));
        return st.data.map(p => {
          const rv = p.relative_value || {}, rs = rv.reasons || {}, fh = p.feed_health, tr = trBy.get(p.provider), cap = capBy.get(p.provider);
          return { provider: p.provider, name: (p.meta && p.meta.display_name) || OG.providerName(p.provider), meta: p.meta || {}, cls: (p.meta || {}).provider_class,
            source_type: (p.meta || {}).source_type, cap, level: cap ? cap.level_implemented : null,
            gpus: p.coverage.gpus, listings: p.coverage.live_listings, priced: p.coverage.priced_listings, regions: p.coverage.region_groups || [],
            premium: rv.premium_avg ?? null, premium_r: rs.premium_avg, cheapest: rv.cheapest_share ?? null, cheapest_r: rs.cheapest_share,
            avail: rv.availability ?? null, avail_r: rs.availability, vol: rv.volatility_daily ?? null, vol_r: rs.volatility_daily,
            samples: rv.samples, lifetime: p.listing_lifetime, fh, tr, last_ok: (tr && tr.last_ok_fetch) || (fh && fh.last_ok_fetch) || null,
            facts: p.facts || [] };
        });
      }

      function draw() {
        const all = rowsOf();
        drawClassSeg(all);
        const rows = st.cls === "all" ? all : all.filter(r => r.cls === st.cls);
        const live = all.filter(r => r.listings > 0);
        const gpuSet = new Set(st.data.flatMap(p => p.coverage.gpu_names || []));
        const healthy = all.filter(r => r.tr ? r.tr.status === "healthy" : r.fh && r.fh.failure_rate_24h === 0).length;
        const withPremium = all.filter(r => r.premium != null).length;
        statsEl.replaceChildren(OG.stats([
          { label: "Providers", value: String(all.length), sub: `${live.length} with live listings here` },
          { label: "Live listings", value: fmt.num(all.reduce((a, r) => a + r.listings, 0)), sub: fmt.num(all.reduce((a, r) => a + r.priced, 0)) + " priced", kind: "observed" },
          { label: "GPU models", value: String(gpuSet.size), sub: "canonical, across providers" },
          { label: "Feeds healthy", value: `${healthy}/${all.filter(r => r.fh || r.tr).length}`, sub: "last 24h of fetches" },
          { label: "Rel. value", value: withPremium ? `${withPremium} rated` : null, reason: `needs ≥ 24 compared hours in the last ${st.days}d; no provider has them yet`, sub: withPremium ? `of ${all.length}, last ${st.days}d` : null, kind: "inferred" },
          { label: "Routable (impl.)", value: String(all.filter(r => r.level >= 2).length), sub: "L≥2 implemented · 0 verified live" },
        ]));
        const cols = [
          { key: "name", label: "Provider", fmt: (v, r) => OG.providerLink(r.provider), width: "150px" },
          { key: "cls", label: "Class", cls: "dim", fmt: v => (v || "–").replace("_", " ") },
          { key: "source_type", label: "Source", cls: "dim", fmt: v => SRC[v] || v || "–", title: "How OpenGrid reads this provider's prices" },
          { key: "level", label: "Integration", value: r => r.level, fmt: (v, r) => integration(r.cap), title: "Integration level OpenGrid implements (0–3). Hover for API vs implemented vs verified. ·mock = adapter tested against mocked HTTP only", desc: true },
          { key: "gpus", label: "GPUs", num: true, desc: true, title: "Canonical GPU models listed now" },
          { key: "listings", label: "Listings", num: true, desc: true, title: "Live listings now (priced in title)", fmt: (v, r) => h("span", { title: `${r.priced} priced` }, fmt.num(v)) },
          { key: "regions", label: "Regions", value: r => r.regions.length, desc: true, fmt: (v, r) => r.regions.length ? h("span", { class: "pv-regs", title: r.regions.join(", ") }, r.regions.join(" · ")) : r.listings ? h("span", { class: "dim", title: "listings carry no location OpenGrid can assign to a region group" }, "unassigned") : "–" },
          { key: "premium", label: "Avg prem.", num: true, title: `Mean premium vs the median of other providers, same GPU, same hour (last ${st.days}d). Negative = cheaper.`,
            fmt: (v, r) => v == null ? OG.na(r.premium_r) : h("span", { class: v < 0 ? "up" : v > 0 ? "down" : "" }, fmt.pct(v)) },
          { key: "cheapest", label: "% cheapest", num: true, desc: true, title: "Share of market hours this provider was the cheapest", fmt: (v, r) => OG.value(v, share, r.cheapest_r) },
          { key: "avail", label: "Avail.", num: true, desc: true, title: "Share of tracked hours with a priced listing", fmt: (v, r) => OG.value(v, share, r.avail_r) },
          { key: "vol", label: "Vol (1d)", num: true, title: "Median across its GPUs of the stdev of daily log price changes", fmt: (v, r) => OG.value(v, x => (x * 100).toFixed(1) + "%", r.vol_r) },
          { key: "life", label: "Lifetime", num: true, value: r => r.lifetime && r.lifetime.median_hours, desc: true, title: "Median observed listing lifetime (first seen → last seen by OpenGrid; understates true lifetime)",
            fmt: (v, r) => r.lifetime ? OG.value(r.lifetime.median_hours, x => x < 48 ? x.toFixed(1) + "h" : (x / 24).toFixed(0) + "d", r.lifetime.reason) : "–" },
          { key: "feed", label: "Feed", value: r => r.tr ? r.tr.status : r.fh ? (r.fh.failure_rate_24h === 0 ? "ok" : "x") : null, fmt: (v, r) => feedStatus(r.tr, r.fh) },
          { key: "last_ok", label: "Last ok", num: true, value: r => r.last_ok ? -new Date(r.last_ok) : null, title: "Age of the last successful fetch", fmt: (v, r) => r.last_ok ? OG.freshBadge(r.last_ok, { intervalSeconds: r.tr && r.tr.polling_interval_seconds, provider: r.provider }) : h("span", { class: "dim" }, "never") },
        ];
        const t = OG.table({ columns: cols, rows, sort: { key: st.sort, dir: st.dir }, stickyLast: 2, rowHref: r => "/provider/" + encodeURIComponent(r.provider), rowKey: r => r.provider,
          rowClass: r => (r.listings ? "" : "out"), csv: `opengrid-providers-${st.days}d.csv`, title: `${rows.length} providers · window ${st.days}d`,
          onSort: s => { st.sort = s.key; st.dir = s.dir; OG.qs.set({ sort: s.key === "listings" ? null : s.key, dir: s.dir === "desc" ? null : s.dir }); } });
        // CSV gets plain values, not display nodes
        const csvOf = { level: r => r.cap ? `L${r.level} ${r.cap.level_implemented_label}; verified_live=false` : "", regions: r => r.regions.join("; "),
          feed: r => r.tr ? r.tr.status : r.fh ? `failure_rate_24h=${r.fh.failure_rate_24h}` : "", last_ok: r => r.last_ok, name: r => r.name,
          life: r => r.lifetime && r.lifetime.median_hours };
        for (const c of cols) if (csvOf[c.key]) c.csv = csvOf[c.key];
        tblBox.replaceChildren(t);
        capsNote.replaceChildren(OG.kindBadge("observed"), " coverage and feed health ", OG.kindBadge("inferred"),
          ` relative value: each provider vs the median of the other providers' lowest price for the same GPU in the same hour, from the hourly rollup. Figures stay n/a (hover for the reason) until there are ≥ 24 compared hours. Greyed rows: no live listings on this server. `,
          h("a", { class: "lnk", href: "/methodology/provider-value" }, "Methodology →"));
        drawFacts(all);
      }

      function drawFacts(all) {
        const out = [];
        for (const r of all) for (const f of r.facts) out.push(h("li", {}, OG.kindBadge("inferred"), " ", f));
        // Observed counts only: no statistic that needs history
        const live = all.filter(r => r.listings > 0);
        if (live.length) {
          const byG = live.slice().sort((a, b) => b.gpus - a.gpus)[0], byL = live.slice().sort((a, b) => b.listings - a.listings)[0];
          const multi = live.filter(r => r.regions.length >= 2).sort((a, b) => b.regions.length - a.regions.length);
          out.push(h("li", {}, OG.kindBadge("observed"), " ", OG.providerLink(byG.provider), ` lists the most GPU models right now (${byG.gpus}).`));
          if (byL !== byG) out.push(h("li", {}, OG.kindBadge("observed"), " ", OG.providerLink(byL.provider), ` has the most live listings (${fmt.num(byL.listings)}).`));
          if (multi.length) out.push(h("li", {}, OG.kindBadge("observed"), " ", `${multi.length} provider${multi.length === 1 ? "" : "s"} list in two or more region groups; `, OG.providerLink(multi[0].provider), ` spans ${multi[0].regions.length} (${multi[0].regions.join(", ")}).`));
          const noLoc = live.filter(r => !r.regions.length);
          if (noLoc.length) out.push(h("li", {}, OG.kindBadge("observed"), ` ${noLoc.length} of ${live.length} live providers publish no location OpenGrid can map to a region group.`));
        }
        const dead = all.filter(r => !r.listings);
        if (dead.length) out.push(h("li", { class: "dim" }, `No live listings on this server from: ${dead.map(r => r.name).join(", ")} (no API key configured here, or the feed has not run).`));
        if (!all.some(r => r.facts.length)) out.push(h("li", { class: "dim" }, `Relative-value facts (cheapest share, average premium) appear once a provider has ≥ 24 compared hours in the last ${st.days} days.`));
        factsEl.replaceChildren(h("ul", { class: "pv-list" }, out));
      }

      load();
      ctx.every(120000, load);
    },
  });
})();
