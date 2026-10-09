/* /provider/:name — one provider in depth.
   /v1/providers/{p}        catalog vs market, relative value, rank history, beats / expensive, feed health, facts
   /v1/capabilities/{p}     integration level: provider API vs implemented vs verified live (never merged)
   /v1/timeline?gpu&provider  this provider's hourly lowest for one GPU (+ market lowest / median, events, news)
   /v1/events?provider=     market events · /v1/news?provider= related news (never presented as a cause)
   /listings                current listings incl. previous price (recent changes)
   /mapping                 field-level provenance and quirks for this provider's feed
   soft: /v1/trust/providers, /v1/trust/listings?provider=, /v1/providers/{p}/gpus/{gpu}/context */
(() => {
  const { h, fmt, Lib: L } = OG;
  const V = () => OG.views || {};
  const share = v => (V().share ? V().share(v) : v == null ? "–" : Math.round(v * 100) + "%");
  const pctCls = v => (v == null ? "" : v < 0 ? "up" : v > 0 ? "down" : "");
  const prem = (v, reason) => (v == null ? OG.na(reason) : h("span", { class: pctCls(v) }, fmt.pct(v)));
  const kv = rows => h("table", { class: "pv-kv" }, h("tbody", {}, rows.filter(Boolean).map(([k, v, t]) => h("tr", { title: t || null }, h("td", {}, k), h("td", {}, v == null || v === "" ? h("span", { class: "dim" }, "–") : v)))));
  const box = (...kids) => h("div", { class: "box box-p" }, kids);
  const mins = s => (s == null ? null : s >= 3600 ? Math.round(s / 3600) + "h" : Math.round(s / 60) + "m");

  OG.page("/provider/:name", {
    title: params => OG.providerName(params.name),
    nav: "providers",
    async mount(el, params, query, ctx) {
      const p = params.name, m0 = OG.providerMeta(p);
      el.classList.add("pv-page", "pd-page");
      const st = { days: +OG.qs.get("days", 30), gpu: OG.qs.get("gpu"), win: OG.qs.get("window", "7d"), hist: OG.qs.get("hist", "premium"), d: null };
      if (![7, 30, 90].includes(st.days)) st.days = 30;
      if (!["24h", "7d", "30d"].includes(st.win)) st.win = "7d";

      const name = (m0 && m0.display_name) || OG.providerName(p);
      const others = OG.data.providers().filter(x => x.name !== p).sort((a, b) => a.display_name.localeCompare(b.display_name));
      const cmpSel = h("select", { class: "field", "aria-label": "Compare with provider", onchange: e => { if (e.target.value) OG.go(`/compare?a=${encodeURIComponent(p)}&b=${encodeURIComponent(e.target.value)}`); } },
        h("option", { value: "" }, "Compare with…"), others.map(o => h("option", { value: o.name }, o.display_name)));
      const daySeg = OG.seg([["7", "7D"], ["30", "30D"], ["90", "90D"]], String(st.days), v => { st.days = +v; OG.qs.set({ days: v === "30" ? null : v }); load(); });
      const subEl = h("span", {}, m0 ? `${(m0.provider_class || "").replace("_", " ")} · prices read from ${m0.source || "?"} (${V().providerSourceLabel ? V().providerSourceLabel(m0.source_type) : m0.source_type})` : "");
      el.append(OG.head(h("span", { class: "gpu-title" }, OG.logo(p, 18), name, m0 && m0.provider_class ? h("span", { class: "eyebrow" }, m0.provider_class.replace("_", " ")) : null), subEl,
        h("span", { class: "flt" }, h("span", {}, "Window"), daySeg), cmpSel,
        h("a", { class: "btn", href: "/explorer?provider=" + encodeURIComponent(p) }, "Listings →"),
        h("a", { class: "btn", href: "/news?provider=" + encodeURIComponent(p) }, "News →"),
        m0 && m0.website ? h("a", { class: "btn", href: m0.website, target: "_blank", rel: "noopener external" }, "Website ↗") : null));
      if (m0 && m0.note) el.append(h("p", { class: "note pd-metanote" }, m0.note));

      const statsEl = h("div", {}, OG.stats([{ label: "GPUs", value: null, reason: "loading" }]));
      const factsEl = h("ul", { class: "pv-list" });
      const catEl = h("div", {}, OG.loading("Loading catalog…"));
      const histEl = h("div", { class: "box box-p" }), histNote = h("p", { class: "note" });
      const histSeg = h("span", {});
      const priceEl = h("div", { class: "box box-p" }), priceNote = h("p", { class: "note" }), priceCtl = h("div", { class: "bar" }), ctxEl = h("ul", { class: "pv-list pd-ctx" });
      const chgEl = h("div", {}, OG.loading());
      const beatsEl = h("div", {}), dearEl = h("div", {});
      const intEl = box(OG.loading()), feedEl = box(OG.loading()), regEl = box(OG.loading());
      const evEl = h("div", {}, OG.loading()), newsEl = h("div", {}, OG.loading());
      const mapEl = h("div", {}, OG.loading()), listEl = h("div", {}, OG.loading());

      el.append(statsEl,
        h("div", { class: "cols pd-cols" },
          h("div", {},
            OG.section("Facts", box(factsEl)),
            OG.section("Catalog vs market", catEl),
            h("div", { class: "cols-2 pd-bx" }, OG.section("Where it beats the market", beatsEl), OG.section("Where it is expensive", dearEl)),
            OG.section(h("span", { class: "pd-sh" }, "Relative value history", histSeg), histEl, histNote),
            OG.section(h("span", { class: "pd-sh" }, "Price history"), priceCtl, priceEl, ctxEl, priceNote),
            OG.section("Recent price changes", chgEl)),
          h("div", {},
            OG.section("Routing integration", intEl),
            OG.section("Source & feed health", feedEl),
            OG.section("Regions covered", regEl),
            OG.section("Market events", evEl),
            OG.section(h("span", { class: "pd-sh" }, "Related news", h("a", { class: "lnk dim", href: "/news?provider=" + encodeURIComponent(p) }, "all →")), newsEl))),
        OG.section("Source field map", mapEl),
        OG.section("Current listings", listEl));

      // ------------------------------------------------------------ main payload
      async function load() {
        let res;
        try { res = await ctx.api(`/v1/providers/${encodeURIComponent(p)}`, { params: { days: st.days }, full: true, slot: "provider" }); }
        catch (e) {
          if (e.stale) return;
          statsEl.replaceChildren(); catEl.replaceChildren(OG.error(e, load));
          if (e.status === 404) { subEl.textContent = "Not a provider OpenGrid tracks"; ctx.setTitle("Unknown provider"); }
          return;
        }
        st.d = res.data; st.meta = res.meta;
        OG.status.asOf(res.meta && res.meta.as_of);
        drawStats(); drawCatalog(); drawBeats(); drawHist(); drawFacts(); drawRegions();
        if (!st.gpu || !st.d.catalog.some(c => c.slug === st.gpu)) {
          const pick = st.d.catalog.filter(c => c.best_price != null).sort((a, b) => b.market_providers - a.market_providers || a.best_price - b.best_price)[0];
          st.gpu = pick ? pick.slug : null;
        }
        drawPriceCtl(); loadPrice();
      }

      function drawStats() {
        const d = st.d, rv = d.relative_value || {}, rs = rv.reasons || {}, c = d.coverage, cat = d.catalog;
        const ranked = cat.filter(x => x.market_providers >= 2 && x.rank_now != null);
        const first = ranked.filter(x => x.rank_now === 1).length;
        const prems = cat.map(x => x.premium_now).filter(v => v != null);
        const medPrem = prems.length ? prems.slice().sort((a, b) => a - b)[Math.floor(prems.length / 2)] : null;
        statsEl.replaceChildren(OG.stats([
          { label: "GPUs", value: String(c.gpus), sub: (c.architectures || []).slice(0, 3).join(", ") || "none listed now" },
          { label: "Listings", value: fmt.num(c.live_listings), sub: `${fmt.num(c.priced_listings)} priced`, kind: "observed" },
          { label: "Regions", fold: true, value: (c.region_groups || []).length ? String(c.region_groups.length) : null, reason: c.live_listings ? "no listing location maps to a region group" : "no live listings", sub: (c.region_groups || []).join(" · ") },
          { label: "Cheapest now", value: ranked.length ? `${first}/${ranked.length}` : null, reason: "no GPU here has a second provider to compare with", sub: "GPUs where it is rank 1" },
          { label: "Median prem. now", value: medPrem == null ? null : prem(medPrem), reason: "no GPU with another provider priced", sub: `across ${prems.length} GPU${prems.length === 1 ? "" : "s"}`, kind: "inferred", title: "Median over its GPUs of: own lowest / median of other providers' lowest − 1, now" },
          { label: `Avg prem. ${st.days}d`, fold: true, value: rv.premium_avg == null ? null : prem(rv.premium_avg), reason: rs.premium_avg || "no rollup history", kind: "inferred" },
          { label: "% cheapest", fold: true, value: rv.cheapest_share == null ? null : share(rv.cheapest_share), reason: rs.cheapest_share || "no rollup history" },
          { label: "Availability", fold: true, value: rv.availability == null ? null : share(rv.availability), reason: rs.availability || "no rollup history", title: "Share of tracked hours with a priced listing" },
          { label: "Volatility", fold: true, value: rv.volatility_daily == null ? null : (rv.volatility_daily * 100).toFixed(1) + "%", reason: rs.volatility_daily || "no rollup history", title: "Median across its GPUs of the stdev of daily log price changes" },
        ]));
      }

      function drawFacts() {
        const d = st.d, out = [];
        for (const f of d.facts || []) out.push(h("li", {}, OG.kindBadge("inferred"), " ", f));
        const priced = d.catalog.filter(c => c.best_price != null).sort((a, b) => a.best_price - b.best_price);
        if (priced.length) {
          const top = priced.filter(c => c.market_providers >= 2).sort((a, b) => b.market_providers - a.market_providers)[0] || priced[0];
          out.push(h("li", {}, OG.kindBadge("observed"), ` ${name} lists ${d.coverage.gpus} GPU model${d.coverage.gpus === 1 ? "" : "s"} across ${fmt.num(d.coverage.live_listings)} live listings; its most widely traded is `, OG.gpuLink(top.gpu),
            ` at ${fmt.price(top.best_price)}/GPU-hr${top.rank_now ? `, rank ${top.rank_now} of ${top.market_providers}` : ""}.`));
          const one = d.catalog.filter(c => c.market_providers >= 2 && c.rank_now === 1);
          if (one.length) out.push(h("li", {}, OG.kindBadge("observed"), ` Cheapest provider right now for `, one.slice(0, 6).map((c, i) => [i ? ", " : "", OG.gpuLink(c.gpu)]), one.length > 6 ? ` and ${one.length - 6} more` : "", "."));
          const solo = d.catalog.filter(c => c.market_providers === 1 && c.best_price != null);
          if (solo.length) out.push(h("li", {}, OG.kindBadge("observed"), ` Only provider OpenGrid sees selling `, solo.slice(0, 5).map((c, i) => [i ? ", " : "", OG.gpuLink(c.gpu)]), solo.length > 5 ? ` and ${solo.length - 5} more` : "", " — no comparison possible."));
        } else out.push(h("li", { class: "dim" }, `No priced live listings from ${name} on this server.`));
        if (!(d.facts || []).length) out.push(h("li", { class: "dim" }, `Window statistics (cheapest share, average premium) need ≥ 24 compared hours in the last ${st.days} days.`));
        factsEl.replaceChildren(...out);
      }

      function drawCatalog() {
        const rows = st.d.catalog.map(c => ({ ...c, avail: c.listings ? c.available_listings / c.listings : null, w: c.window || {} }));
        if (!rows.length) { catEl.replaceChildren(OG.insufficient(`OpenGrid has no live listings from ${name} on this server. On servers without this provider's API key, its feed does not run.`, "No catalog")); return; }
        catEl.replaceChildren(OG.table({
          columns: [
            { key: "gpu", label: "GPU", fmt: v => OG.gpuLink(v), value: r => L.shortGpu(r.gpu), csv: r => r.gpu },
            { key: "best_price", label: "Best $/GPU·h", num: true, fmt: v => v == null ? OG.na("no priced in-stock listing") : h("b", {}, fmt.price(v)) },
            { key: "market_median", label: "Mkt median", num: true, fmt: v => fmt.price(v), title: "Median of every provider's lowest price for this GPU now" },
            { key: "premium_now", label: "Prem. now", num: true, fmt: (v, r) => prem(v, r.market_providers < 2 ? "only provider selling it: nothing to compare with" : "not priced now"), title: "Own lowest / median of the OTHER providers' lowest − 1. Negative = cheaper than the market" },
            { key: "rank_now", label: "Rank", num: true, fmt: (v, r) => v == null ? "–" : h("span", { class: v === 1 && r.market_providers > 1 ? "up" : "" }, `${v}/${r.market_providers}`), title: "1 = cheapest of the providers selling it now" },
            { key: "wprem", label: `Avg ${st.days}d`, num: true, value: r => r.w.premium_avg, fmt: (v, r) => prem(r.w.premium_avg, (r.w.reasons || {}).premium_avg) },
            { key: "wcheap", label: "% cheapest", num: true, desc: true, value: r => r.w.cheapest_share, fmt: (v, r) => OG.value(r.w.cheapest_share, share, (r.w.reasons || {}).cheapest_share) },
            { key: "avail", label: "In stock", num: true, desc: true, fmt: (v, r) => h("span", { title: `${r.available_listings} of ${r.listings} listings explicitly in stock (others sold out or unknown)` }, `${r.available_listings}/${r.listings}`) },
            { key: "region_groups", label: "Regions", value: r => r.region_groups.join(" "), fmt: v => h("span", { class: "dim" }, v.join(" · ")) },
          ],
          rows, sort: { key: "premium_now", dir: "asc" }, rowKey: r => r.slug, compact: true, csv: `opengrid-${p}-catalog.csv`,
          title: `${rows.length} GPUs · market = every provider's lowest live price now`,
          onRow: r => { st.gpu = r.slug; OG.qs.set({ gpu: r.slug }); drawPriceCtl(); loadPrice(); priceEl.scrollIntoView({ block: "center", behavior: "smooth" }); },
        }));
      }

      function drawBeats() {
        const list = (items, empty) => items.length ? h("div", { class: "box pd-bl" }, items.map(x => h("div", { class: "pd-bi" },
          OG.gpuLink(x.gpu), h("span", { class: "spacer" }), h("span", { class: "pd-bar" }, h("i", { class: x.premium < 0 ? "lo" : "hi", style: { width: Math.min(100, Math.abs(x.premium) * 100) + "%" } })),
          h("span", { class: "n " + pctCls(x.premium) }, fmt.pct(x.premium)),
          h("span", { class: "badge", title: x.basis === "current" ? "current premium (not enough history for a window average)" : `average over the last ${st.days} days` }, x.basis === "current" ? "now" : st.days + "d")))) : OG.empty(empty);
        beatsEl.replaceChildren(list(st.d.beats_the_market || [], "No GPU where it is below the market median."));
        dearEl.replaceChildren(list(st.d.expensive || [], "No GPU where it is above the market median."));
      }

      // ------------------------------------------------------------ history (daily, from the rollup)
      let histChart = null;
      function drawHist() {
        const pts = st.d.rank_history || [];
        histSeg.replaceChildren(OG.seg([["premium", "Avg premium"], ["rank", "Rank"], ["share", "Cheapest share"], ["avail", "Availability"]], st.hist, v => { st.hist = v; OG.qs.set({ hist: v === "premium" ? null : v }); drawHist(); }));
        if (histChart) { histChart.destroy(); histChart = null; }
        if (pts.length < 2) {
          histEl.replaceChildren(OG.insufficient(`${pts.length} day${pts.length === 1 ? "" : "s"} of rollup history for ${name} in this window; a history line needs at least 2.` +
            (pts.length ? ` Today so far: premium ${pts[0].premium_avg == null ? "n/a" : fmt.pct(pts[0].premium_avg)}, mean rank ${pts[0].mean_rank == null ? "n/a" : pts[0].mean_rank.toFixed(1)}, cheapest ${share(pts[0].cheapest_share)} of ${pts[0].hours_market} market hours.` : ""), "History building"));
          histNote.textContent = "";
          return;
        }
        const times = pts.map(x => x.day + "T12:00:00Z"), color = L.providerColor(p);
        const S = {
          premium: { series: [{ key: "premium", label: "Avg premium vs others' median", values: pts.map(x => x.premium_avg), color, width: 2, step: false }], yFmt: fmt.pct, yZero: true },
          rank: { series: [{ key: "rank", label: "Mean rank (1 = cheapest)", values: pts.map(x => x.mean_rank), color, width: 2, step: false }], yFmt: v => v.toFixed(1) },
          share: { series: [{ key: "c", label: "Cheapest share", values: pts.map(x => x.cheapest_share), color, width: 2, step: false }, { key: "t", label: "Top-3 share", values: pts.map(x => x.top3_share), color: "#7d8895", dash: "3 3", step: false }], yFmt: share, yZero: true },
          avail: { series: [{ key: "a", label: "Priced availability", values: pts.map(x => x.availability), color, width: 2, step: false }], yFmt: share, yZero: true },
        }[st.hist];
        histEl.textContent = "";
        histChart = OG.charts.timeseries(histEl, { times, height: 180, legend: true, ...S, label: `${name} relative value history` });
        histNote.replaceChildren(OG.kindBadge("inferred"), ` One point per day over that day's compared hours (hours where at least one other provider priced the same GPU), all GPUs pooled. Last ${st.days} days. `, h("a", { class: "lnk", href: "/methodology/provider-value" }, "Methodology →"));
      }

      // ------------------------------------------------------------ price history for one GPU
      function drawPriceCtl() {
        const cat = (st.d && st.d.catalog) || [];
        if (!cat.length) { priceCtl.replaceChildren(); return; }
        const sel = h("select", { class: "field", "aria-label": "GPU", onchange: e => { st.gpu = e.target.value; OG.qs.set({ gpu: st.gpu }); loadPrice(); } },
          cat.slice().sort((a, b) => L.shortGpu(a.gpu).localeCompare(L.shortGpu(b.gpu), undefined, { numeric: true })).map(c => h("option", { value: c.slug, selected: c.slug === st.gpu ? "" : null }, L.shortGpu(c.gpu) + (c.best_price != null ? "  " + fmt.price(c.best_price) : ""))));
        priceCtl.replaceChildren(h("span", { class: "flt" }, h("span", {}, "GPU"), sel),
          h("span", { class: "flt" }, h("span", {}, "Window"), OG.seg([["24h", "24H"], ["7d", "7D"], ["30d", "30D"]], st.win, v => { st.win = v; OG.qs.set({ window: v === "7d" ? null : v }); loadPrice(); })),
          st.gpu ? h("a", { class: "lnk dim", href: "/gpu/" + st.gpu }, "GPU market →") : null);
      }
      let priceChart = null;
      async function loadPrice() {
        if (!st.gpu) { priceEl.replaceChildren(OG.empty("No GPU to chart.")); return; }
        priceEl.replaceChildren(OG.loading());
        let mine, mkt, cx;
        try {
          [mine, mkt, cx] = await Promise.all([
            ctx.api("/v1/timeline", { params: { gpu: st.gpu, provider: p, window: st.win }, slot: "pd-tl" }),
            ctx.api("/v1/timeline", { params: { gpu: st.gpu, window: st.win } }).catch(() => null),
            ctx.api(`/v1/providers/${encodeURIComponent(p)}/gpus/${st.gpu}/context`).catch(() => null)]);
        } catch (e) { if (!e.stale) priceEl.replaceChildren(OG.error(e, loadPrice)); return; }
        const own = new Map(mine.prices.points.map(x => [x.hour, x.lowest]));
        const ml = new Map(((mkt && mkt.prices.points) || []).map(x => [x.hour, x]));
        const times = [...new Set([...own.keys(), ...ml.keys()])].sort();
        if (priceChart) { priceChart.destroy(); priceChart = null; }
        const gname = mine.gpu;
        if (times.length < 2) {
          const one = mine.prices.points[0];
          priceEl.replaceChildren(OG.insufficient(`${mine.prices.points.length} hourly sample${mine.prices.points.length === 1 ? "" : "s"} of ${name}'s ${L.shortGpu(gname)} price in the last ${st.win}` +
            (one ? ` (${fmt.price(one.lowest)} at ${fmt.dateTime(one.hour)})` : "") + ". A line needs at least two hours of the rollup.", "History building"));
        } else {
          const evs = [...(mine.market_events.events || []).map(e => ({ t: e.occurred_at, label: e.title, kind: e.type, severity: e.severity, href: "/events?provider=" + encodeURIComponent(p) })),
            ...(mine.price_moves.moves || []).map(mv => ({ t: mv.at, label: `${name} lowest ${fmt.price(mv.from)} → ${fmt.price(mv.to)} (${fmt.pct(mv.change_pct / 100)})`, kind: "price move · inferred", severity: "notable" })),
            ...(((mkt && mkt.news.items) || mine.news.items || []).map(n => ({ t: n.published_at, label: n.title, kind: "news · " + n.source_name, color: "#8fc3ff", detail: "related by entity, not necessarily causal", href: "/news?gpu=" + st.gpu })))];
          priceEl.textContent = "";
          priceChart = OG.charts.timeseries(priceEl, {
            times, height: 240, label: `${name} ${gname} price`, events: evs.filter(e => e.t),
            series: [
              { key: "mlow", label: "Market lowest", values: times.map(t => (ml.get(t) || {}).lowest ?? null), color: "#7d8895", dash: "2 3" },
              { key: "mmed", label: "Market median", values: times.map(t => (ml.get(t) || {}).median ?? null), color: "#4f5a66", dash: "6 3" },
              { key: "own", label: name + " lowest", values: times.map(t => own.get(t) ?? null), color: L.providerColor(p), width: 2, strong: true },
            ] });
        }
        const sums = (cx && cx.summaries) || [];
        ctxEl.replaceChildren(...sums.map(s => h("li", {}, OG.kindBadge("observed"), " ", s)),
          ...(cx ? Object.values(cx.windows || {}).filter(w => w.reason).slice(0, 1).map(w => h("li", { class: "dim" }, `Percentile vs its own history: ${w.reason}.`)) : []));
        priceNote.replaceChildren(OG.kindBadge("observed"), ` Hourly lowest eligible on-demand price per GPU-hour from the rollup; market lines are every provider's lowest. Markers: market events, inferred moves (≥ ${mine.price_moves.threshold_pct}%) and news mentioning this GPU — related in time, not necessarily causal.`);
      }

      // ------------------------------------------------------------ right column
      async function loadIntegration() {
        const c = await ctx.api(`/v1/capabilities/${encodeURIComponent(p)}`).catch(() => null);
        if (!c) { intEl.replaceChildren(OG.na("capability registry unavailable")); return; }
        const lv = n => n == null ? h("span", { class: "dim" }, "not established") : `L${n} ${({ 0: "market data only", 1: "availability check", 2: "provisioning", 3: "full lifecycle" })[n]}`;
        const can = c.level_implemented >= 3 ? `launch, check status and terminate${c.supports_stop ? ", stop" : ""} through the ${c.via || name} API`
          : c.level_implemented === 2 ? "launch through the API" : c.level_implemented === 1 ? "check availability live" : null;
        intEl.replaceChildren(
          h("div", { class: "pd-int" }, V().integration ? V().integration(c, { long: true }) : lv(c.level_implemented), c.verified_live ? OG.badge("verified live", "good") : OG.badge("not verified live", "warn", "no adapter has made a real call against a provider account yet")),
          h("p", { class: "pd-can" }, can ? `OpenGrid's adapter can ${can} — tested against mocked HTTP built from the documented API only; no real provisioning call has been made.`
            : `OpenGrid reads ${name}'s prices only. It cannot check stock live, quote or provision here${c.level_supported_by_provider_api ? ` (the provider's API documents L${c.level_supported_by_provider_api}; OpenGrid has no adapter)` : ""}.`),
          kv([["Provider API documents", lv(c.level_supported_by_provider_api), c.docs_checked],
            ["OpenGrid implements", lv(c.level_implemented)],
            ["Verified live", c.verified_live ? "yes" : h("span", { class: "pv-unv" }, "no")],
            ["Avail. check live-verified", c.availability_check_verified_live ? h("span", { class: "up" }, "yes · read-only") : "no"],
            c.via ? ["Routed via", `${c.via} (aggregator; it is the counterparty and its listing is the price)`] : null,
            ["Resource", c.resource], ["Credential", (c.credential_requirement || "–").replace(/_/g, " ")],
            ["Configured here", c.credentials_configured ? "yes" : "no", "whether OpenGrid-managed credentials are set on this server"],
            ["Stop supported", c.supports_stop ? "yes" : "no"],
            ["API docs", c.docs_url ? h("a", { class: "lnk", href: c.docs_url, target: "_blank", rel: "noopener external" }, (c.docs_checked || "docs") + " ↗") : null]]),
          c.notes ? h("p", { class: "note" }, c.notes) : null);
      }

      async function loadFeed() {
        const [trust, d] = [await ctx.api("/v1/trust/providers").catch(() => null), st.d];
        const t = (trust || []).find(x => x.provider === p), fh = d && d.feed_health, m = (d && d.meta) || m0 || {};
        const life = d && d.listing_lifetime;
        feedEl.replaceChildren(
          h("div", { class: "pd-int" }, V().feedStatus ? V().feedStatus(t, fh) : null, t || fh ? OG.freshBadge((t && t.last_ok_fetch) || (fh && fh.last_ok_fetch), { intervalSeconds: t && t.polling_interval_seconds, provider: p }) : null,
            h("span", { class: "dim" }, t && t.polling_interval_seconds ? `polled every ${mins(t.polling_interval_seconds)}` : "")),
          kv([["Source host", m.source ? h("span", { class: "mono" }, m.source) : null],
            ["Source type", V().providerSourceLabel ? V().providerSourceLabel(m.source_type) : m.source_type],
            ["Last ok fetch", (t && t.last_ok_fetch) || (fh && fh.last_ok_fetch) ? fmt.dateTime((t && t.last_ok_fetch) || fh.last_ok_fetch) + " · " + fmt.age((t && t.last_ok_fetch) || fh.last_ok_fetch) + " ago" : h("span", { class: "dim" }, "never on this server")],
            fh ? ["Fetches 24h", `${fh.fetches_24h}${fh.failure_rate_24h != null ? ` · ${share(fh.failure_rate_24h)} failed` : ""}`] : null,
            t ? ["Latency p50 / p95", `${fmt.num(t.latency_ms_p50_24h, 0)} / ${fmt.num(t.latency_ms_p95_24h, 0)} ms`] : fh && fh.avg_latency_ms_24h != null ? ["Latency avg", fmt.num(fh.avg_latency_ms_24h, 0) + " ms"] : null,
            t ? ["Listings now / 24h ago", [fmt.num(t.listings_now), " / ", t.listings_24h_ago == null ? OG.na(t.listings_24h_ago_note) : fmt.num(t.listings_24h_ago)]] : null,
            t ? ["Schema changes 7d", String(t.schema_changes_7d)] : null,
            t ? ["Quarantined values", String(t.quarantined)] : null,
            t && t.consecutive_failures ? ["Consecutive failures", h("span", { class: "down" }, String(t.consecutive_failures))] : null,
            life ? ["Listing lifetime (median)", OG.value(life.median_hours, x => x < 48 ? x.toFixed(1) + " h" : (x / 24).toFixed(1) + " d", life.reason), life.label] : null]),
          h("p", { class: "note" }, OG.kindBadge("observed"), " from OpenGrid's own fetch log. ", h("a", { class: "lnk", href: "#field-map" }, "Field map ↓"), " · ", h("a", { class: "lnk", href: "/methodology/data-trust" }, "Data trust →")));
      }

      function drawRegions() {
        const by = new Map();
        for (const c of st.d.catalog) for (const g of c.region_groups) { const a = by.get(g) || { gpus: 0, listings: 0 }; a.gpus++; a.listings += c.listings; by.set(g, a); }
        const rows = [...by.entries()].sort((a, b) => b[1].listings - a[1].listings);
        regEl.replaceChildren(rows.length ? h("table", { class: "pv-kv" }, h("tbody", {}, rows.map(([g, a]) => h("tr", {},
          h("td", {}, g === "Unassigned" ? h("span", { class: "dim", title: "no location given, or a listing spanning several groups" }, g) : g),
          h("td", { class: "mono" }, `${a.gpus} GPU${a.gpus === 1 ? "" : "s"}`), h("td", { class: "mono dim" }, `${a.listings} listings`))))) : OG.empty("No live listings."), rawRegions);
      }
      const rawRegions = h("div", { class: "pd-raw" });

      async function loadListings() {
        const [rows, tl] = await Promise.all([ctx.api("/listings").catch(e => e), ctx.api("/v1/trust/listings", { params: { provider: p, limit: 1000 } }).catch(() => null)]);
        if (rows instanceof Error) { listEl.replaceChildren(OG.error(rows)); chgEl.replaceChildren(OG.error(rows)); return; }
        const mine = rows.filter(r => r.provider === p);
        const trustBy = new Map((tl || []).map(r => [r.listing_id, r.trust]));
        // raw locations as the provider publishes them
        const locs = new Map();
        for (const r of mine) { const k = r.region || r.country; if (k) locs.set(k, (locs.get(k) || 0) + 1); }
        rawRegions.replaceChildren(locs.size ? h("p", { class: "note" }, h("b", {}, "As published: "), [...locs.entries()].sort((a, b) => b[1] - a[1]).slice(0, 24).map(([k, n], i) => [i ? " · " : "", k, h("span", { class: "dimmer" }, " " + n)])) : mine.length ? h("p", { class: "note" }, "This feed publishes no location for its listings.") : "");
        // recent changes: previous price differs
        const ch = mine.filter(r => r.previous_price_per_gpu_hour != null && r.price_per_gpu_hour != null && r.previous_price_per_gpu_hour !== r.price_per_gpu_hour)
          .map(r => ({ ...r, pct: r.price_per_gpu_hour / r.previous_price_per_gpu_hour - 1 }));
        chgEl.replaceChildren(ch.length ? OG.table({
          columns: [
            { key: "changed_at", label: "When", num: true, fmt: v => h("span", { title: fmt.dateTime(v) }, fmt.age(v) + " ago"), value: r => r.changed_at ? +new Date(r.changed_at) : null, csv: r => r.changed_at },
            { key: "canonical_gpu_name", label: "GPU", fmt: (v, r) => v ? OG.gpuLink(v) : h("span", { class: "dim" }, r.raw_gpu_name) },
            { key: "listing_id", label: "Listing", cls: "dim mono" },
            { key: "provider_tier", label: "Tier", cls: "dim" },
            { key: "previous_price_per_gpu_hour", label: "From", num: true, fmt: v => fmt.price(v) },
            { key: "price_per_gpu_hour", label: "To", num: true, fmt: v => h("b", {}, fmt.price(v)) },
            { key: "pct", label: "Change", num: true, fmt: v => OG.chg(v) },
          ], rows: ch, sort: { key: "changed_at", dir: "desc" }, compact: true, limit: 12, csv: `opengrid-${p}-changes.csv`,
          title: `${ch.length} listing${ch.length === 1 ? "" : "s"} whose price changed since first seen`,
        }) : OG.empty(mine.length ? "No listing has changed price since OpenGrid started recording it." : "No listings."));
        // all current listings with their trust block
        listEl.replaceChildren(mine.length ? OG.table({
          columns: [
            { key: "canonical_gpu_name", label: "GPU", fmt: (v, r) => v ? OG.gpuLink(v) : h("span", { class: "dim", title: "not mapped to a canonical GPU — never guessed" }, r.raw_gpu_name + " · unmapped") },
            { key: "raw_gpu_name", label: "Raw name", cls: "dim" },
            { key: "gpu_count", label: "GPUs", num: true },
            { key: "price_per_gpu_hour", label: "$/GPU·h", num: true, fmt: v => fmt.price(v) },
            { key: "price_per_instance_hour", label: "$/inst·h", num: true, fmt: v => fmt.price(v) },
            { key: "market_type", label: "Type", cls: "dim", fmt: (v, r) => v + (r.interruptible ? " · interruptible" : "") + (r.provider_tier ? " · " + r.provider_tier : "") },
            { key: "region", label: "Region", cls: "dim" },
            { key: "available", label: "Stock", fmt: (v, r) => { const b = (trustBy.get(r.listing_id) || {}).availability_basis; const t = b ? `availability ${b}` : null; return v === true ? h("span", { title: t }, "in stock") : v === false ? h("span", { class: "down", title: t }, "sold out") : h("span", { class: "dim", title: t }, "unknown"); } },
            { key: "fresh", label: "Fresh", value: r => (trustBy.get(r.listing_id) || {}).freshness || null, fmt: (v, r) => OG.freshBadge(v || r.observed_at, { provider: p }), csv: r => (trustBy.get(r.listing_id) || {}).freshness },
            { key: "conf", label: "Conf.", value: r => (trustBy.get(r.listing_id) || {}).confidence || null, cls: "dim", title: "Data-trust confidence (quality layer)", fmt: (v, r) => v ? h("span", { title: ((trustBy.get(r.listing_id) || {}).confidence_reasons || []).join("; ") || null }, v) : "–" },
          ],
          rows: mine, sort: { key: "price_per_gpu_hour", dir: "asc" }, compact: true, limit: 50, csv: `opengrid-${p}-listings.csv`, title: `${mine.length} listings incl. sold-out and ineligible rows`,
        }) : OG.empty("No current listings from this provider on this server."));
      }

      async function loadEvents() {
        const evs = await ctx.api("/v1/events", { params: { provider: p, limit: 12 } }).catch(() => null);
        if (!evs) { evEl.replaceChildren(OG.na("events unavailable")); return; }
        // the same one-line rows as the overview (full title on hover)
        evEl.replaceChildren(evs.length ? h("div", { class: "box pd-ev ov-evl" }, evs.map(e => OG.views.eventRow(e, { compact: true })),
          h("a", { class: "lnk dim pd-more", href: "/events?provider=" + encodeURIComponent(p) }, "All events →")) : OG.empty("No market events for this provider yet."));
      }

      async function loadNews() {
        const items = await ctx.api("/v1/news", { params: { provider: p, limit: 8 } }).catch(() => null);
        if (!items) { newsEl.replaceChildren(OG.na("news unavailable")); return; }
        newsEl.replaceChildren(items.length ? h("div", { class: "box pd-ev" }, items.map(n => h("div", { class: "pd-ni" },
          h("a", { class: "lnk", href: n.url, target: "_blank", rel: "noopener external" }, n.title),
          h("div", { class: "dim pd-nm" }, fmt.date(n.published_at), " · ", n.source_name, n.source_count > 1 ? ` +${n.source_count - 1}` : "", " · rel ", String(n.relevance), (n.topics || []).length ? " · " + n.topics.join(", ") : "")))) :
          OG.empty(`No stored news mentions ${name}.`), h("p", { class: "note" }, "Matched by entity rules; shown as related, never as a cause of a price move."));
      }

      async function loadMapping() {
        const m = await ctx.api("/mapping").catch(() => null);
        const entry = m && (m.providers || []).find(x => x.provider === p);
        if (!entry) { mapEl.replaceChildren(OG.empty("No field map for this provider.")); return; }
        mapEl.id = "field-map";
        mapEl.replaceChildren(h("div", { class: "cols-2" },
          h("div", {}, h("div", { class: "lbl" }, "Endpoints read"), box(kv(Object.entries(entry.endpoints || {}).map(([k, v]) => [h("span", { class: "mono" }, k), v])))),
          h("div", {}, h("div", { class: "lbl" }, "Quirks"), box(h("ul", { class: "pv-list" }, (entry.quirks || []).map(q => h("li", {}, q)))))),
          h("div", { class: "lbl pd-gap" }, "Field provenance"),
          OG.table({ columns: [
            { key: "field", label: "Field", cls: "mono" },
            { key: "kind", label: "Kind", fmt: v => OG.badge(v, v === "raw" ? "good" : v === "absent" ? "bad" : v === "derived" || v === "lookup" ? "" : "warn") },
            { key: "source", label: "Source", cls: "mono dim wrap" },
            { key: "note", label: "Note", cls: "dim wrap" }],
          rows: entry.fields || [], compact: true, csv: `opengrid-${p}-field-map.csv`, title: "Where each normalized field comes from (/mapping)" }));
      }

      await load();
      loadIntegration(); loadFeed(); loadListings(); loadEvents(); loadNews(); loadMapping();
      ctx.every(120000, () => { load().then(loadFeed); loadListings(); });
    },
  });
})();
