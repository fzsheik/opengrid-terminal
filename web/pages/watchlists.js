/* /watchlists — watchlists (GPU / provider / GPU+provider / region / index items) with live values, and
   alert rules: templates for the founder's examples, rule state (ok / firing / unknown + reason),
   "test now" (POST /v1/alerts/{id}/test: dry, no state change, no delivery), firings feed, webhook channel.
   Email is an interface only: not implemented, and said so. ?add=<gpu> pre-fills an item (command "w <gpu>"). */
(() => {
  const { h, fmt } = OG;
  const REGIONS = ["US", "Canada", "Europe", "UK", "APAC", "Middle East", "LATAM", "Africa"];
  const KINDS = [["gpu", "GPU"], ["provider", "Provider"], ["gpu_provider", "GPU + provider"], ["region", "Region"], ["index", "Index"]];
  const METRICS = {
    market_low: { label: "Lowest price now", fields: ["gpu"], unit: "$/GPU·h", help: "Lowest observed on-demand price across providers now" },
    market_median: { label: "Median price now", fields: ["gpu"], unit: "$/GPU·h", help: "Median of each provider's lowest price now" },
    provider_price: { label: "Provider price now", fields: ["gpu", "provider"], unit: "$/GPU·h", help: "That provider's lowest observed price now" },
    available: { label: "Listings available", fields: ["gpu", "provider?"], unit: "listings", help: "Live listings explicitly reported available (default: > 0)" },
    provider_price_change_pct: { label: "Provider price change %", fields: ["gpu", "provider", "window_hours"], unit: "%", help: "% change of that provider's lowest price over the window" },
    region_availability_change_pct: { label: "Region availability change %", fields: ["gpu", "region_group", "window_hours"], unit: "%", help: "% change in available listings in a region group (hourly rollup)" },
    index_level: { label: "Index level", fields: ["index_id"], unit: "points", help: "Published OpenGrid index level" },
    index_new_low: { label: "Index new low", fields: ["index_id", "window"], unit: "1 = new low", help: "1 when the latest published level is the lowest of the window (default == 1)" },
  };
  const OPS = ["<", "<=", ">", ">=", "==", "!="];
  const confirm = o => OG.dialog(o);
  const shortG = g => (g ? OG.shortGpu(g) : "");

  function condText(p) {
    const m = METRICS[p.metric] || { label: p.metric };
    const subj = [p.gpu && shortG(p.gpu), p.provider && OG.providerName(p.provider), p.region_group, p.index_id].filter(Boolean).join(" · ");
    const op = p.op || (p.metric === "available" ? ">" : p.metric === "index_new_low" ? "==" : "?");
    const val = p.value != null ? p.value : p.metric === "available" ? 0 : p.metric === "index_new_low" ? 1 : "?";
    const win = p.window_hours ? ` over ${p.window_hours}h` : p.window ? ` over ${p.window}` : "";
    return `${m.label}${win} ${op} ${val}${subj ? " — " + subj : ""}`;
  }
  function stateBadge(r) {
    if (r.status === "paused") return OG.badge("paused");
    if (r.last_state === "true") return OG.badge("firing", "bad", "condition true at the last evaluation");
    if (r.last_state === "false") return OG.badge("ok", "good", "condition false at the last evaluation");
    if (r.last_state === "unknown") return OG.badge("unknown", "warn", r.last_detail || "no reading: never fires on unknown");
    return OG.badge("not evaluated", "", "the alerts job evaluates active rules every ~3 minutes");
  }

  OG.command(/^w\s+(.+)$/i, async m => { const g = await OG.resolveGpu(m[1]); OG.go("/watchlists?add=" + encodeURIComponent(g ? g.slug : m[1])); }, "w <gpu>          add a GPU to a watchlist");

  OG.page("/watchlists", {
    title: "Watchlists",
    async mount(el, params, query, ctx) {
      const root = h("div", { class: "pg-wl" });
      el.append(root);
      const st = { lists: [], sel: Number(query.wl) || null, rules: [], indices: [], gpus: [], market: null, tests: new Map() };
      root.append(OG.head("Watchlists & alerts", "Follow GPUs, providers, regions and indices; alert on price and availability · observed data, unknown never fires",
        h("a", { class: "btn", href: "/methodology/alerts" }, "Alerts method"), h("a", { class: "btn", href: "/api#ep-watchlists-alerts" }, "API")));
      const rail = h("div", { class: "wl-rail box" });
      const main = h("div", { class: "wl-main" }, OG.loading());
      const rulesBox = h("div", {}, OG.loading());
      const formBox = h("div");
      const firingsBox = h("div", {}, OG.loading());
      const secretBox = h("div");
      root.append(h("div", { class: "wl-grid" }, rail, main),
        h("section", { class: "sec", id: "alerts" }, h("h2", { class: "sec-h" }, "Alert rules"), secretBox, formBox, rulesBox),
        OG.section("Firings · last 7 days", firingsBox));

      // GPU datalist shared by the forms
      const dl = h("datalist", { id: "wl-gpus" });
      root.append(dl);
      OG.data.gpus().then(gs => { st.gpus = gs; dl.replaceChildren(...gs.sort((a, b) => b.weight - a.weight).map(g => h("option", { value: g.slug }, g.name))); }).catch(() => {});
      ctx.api("/v1/indices").then(ix => { st.indices = ix || []; drawMain(); drawForm(); }).catch(() => {});

      /* ----- lists ----- */
      async function loadLists() {
        try {
          st.lists = await ctx.api("/v1/watchlists", { params: { current: "true" }, nocache: true });
          if (!st.lists.some(w => w.id === st.sel)) st.sel = st.lists.length ? st.lists[0].id : null;
          drawRail(); drawMain();
        } catch (e) { main.replaceChildren(OG.error(e, loadLists)); }
      }
      function drawRail() {
        const name = h("input", { class: "field", placeholder: "New watchlist", maxlength: 200, "aria-label": "New watchlist name" });
        rail.replaceChildren(
          h("div", { class: "wl-rail-h" }, h("span", { class: "eyebrow" }, "Watchlists"), h("span", { class: "dim mono" }, st.lists.length)),
          h("div", { class: "wl-lists" }, st.lists.length ? st.lists.map(w => h("button", { type: "button", class: "wl-li" + (w.id === st.sel ? " on" : ""), onclick: () => { st.sel = w.id; OG.qs.set({ wl: w.id }); drawRail(); drawMain(); } },
            h("span", {}, w.name), h("span", { class: "dim mono" }, w.items.length))) : h("div", { class: "dim wl-none" }, "None yet.")),
          h("form", { class: "wl-new", onsubmit: async e => {
            e.preventDefault();
            if (!name.value.trim()) return;
            try { const w = await ctx.api("/v1/watchlists", { method: "POST", body: { name: name.value.trim() } }); st.sel = w.id; OG.qs.set({ wl: w.id }); await loadLists(); }
            catch (e2) { rail.append(OG.error(e2)); }
          } }, name, h("button", { class: "btn", type: "submit" }, "Create")));
      }

      /* live values: one /market read for all GPUs + soft per-item reads */
      const soft = new Map();
      const softGet = (path, opts) => { const k = path + JSON.stringify(opts || {}); if (!soft.has(k)) soft.set(k, ctx.api(path, opts).catch(() => null)); return soft.get(k); };
      async function live(i) {
        const m = st.market && st.market.gpus ? st.market.gpus.find(g => g.gpu === i.gpu) : null;
        const out = { low: null, median: null, chg: null, chgReason: null, avail: null, detail: null };
        if (i.kind === "gpu") {
          out.low = i.current ? i.current.lowest : m && m.lowest; out.median = m ? m.median : null;
          out.chg = m && m.change_since ? m.change_pct : null; out.chgReason = m ? "not enough coherent 24h history" : "no live market for this GPU";
          const mk = await softGet("/v1/markets/" + OG.slug(i.gpu));
          if (mk && mk.listings) out.avail = `${mk.listings.available} avail · ${mk.listings.availability_unknown} unknown · ${mk.listings.sold_out} sold out`;
          out.detail = i.current && i.current.lowest_provider ? h("span", {}, "low at ", OG.providerLink(i.current.lowest_provider), ` · ${i.current.providers} providers`) : null;
        } else if (i.kind === "gpu_provider") {
          out.low = i.current ? i.current.price_per_gpu_hour : null; out.median = m ? m.median : null;
          out.chgReason = "per-provider 24h change is not exposed by the API yet";
          const mk = await softGet("/v1/markets/" + OG.slug(i.gpu));
          const bp = mk && mk.by_provider ? mk.by_provider.find(b => b.provider === i.provider) : null;
          out.avail = bp ? (bp.available === true ? "available" : bp.available === false ? "sold out" : "not reported") : i.current && !i.current.listed_now ? "not listed now" : null;
          out.detail = bp && bp.premium_vs_others_median != null ? `${fmt.pct(bp.premium_vs_others_median)} vs others' median · rank ${bp.rank}` : i.current && !i.current.listed_now ? "no live priced listing" : null;
        } else if (i.kind === "provider") {
          const p = await softGet("/v1/providers/" + encodeURIComponent(i.provider));
          out.chgReason = "not applicable to a provider";
          if (p && p.coverage) { out.avail = `${p.coverage.live_listings} live listings · ${p.coverage.priced_listings} priced`; out.detail = `${p.coverage.gpus} GPUs · ${(p.coverage.region_groups || []).join(", ") || "no region groups"}`; }
        } else if (i.kind === "region") {
          const rs = await softGet("/v1/regions", i.gpu ? { params: { gpu: OG.slug(i.gpu) } } : undefined);
          const r = rs ? rs.find(x => x.region_group === i.region_group) : null;
          out.chgReason = "region change is available as an alert metric (hourly rollup)";
          if (r) { out.avail = `${r.live_listings} live · ${r.priced_listings} priced`; out.detail = `${r.providers.length} providers${i.gpu ? " · " + shortG(i.gpu) : ""} · ${r.gpus} GPUs`; }
        } else if (i.kind === "index") {
          const x = await softGet("/v1/indices/" + encodeURIComponent(i.index_id));
          if (x) {
            const lvl = x.level != null ? x.level : x.current && x.current.level;
            out.low = null;
            out.index = x.published === false || lvl == null ? null : lvl;
            out.indexReason = x.reason || (x.current && x.current.reason) || "index not published";
            const ch = x.changes || (x.current && x.current.changes) || {};
            out.chg = ch["24h"] != null ? ch["24h"] : null;
            out.chgReason = (x.change_reasons || {})["24h"] || "no 24h change";
            out.detail = x.name || (x.definition && x.definition.name);
          }
        }
        return out;
      }
      function itemLabel(i) {
        const kind = OG.badge((KINDS.find(k => k[0] === i.kind) || [0, i.kind])[1]);
        if (i.kind === "gpu") return h("span", { class: "wl-it" }, kind, OG.gpuLink(i.gpu));
        if (i.kind === "provider") return h("span", { class: "wl-it" }, kind, OG.providerLink(i.provider));
        if (i.kind === "gpu_provider") return h("span", { class: "wl-it" }, kind, OG.gpuLink(i.gpu), h("span", { class: "dim" }, "@"), OG.providerLink(i.provider));
        if (i.kind === "region") return h("span", { class: "wl-it" }, kind, h("b", {}, i.region_group), i.gpu ? OG.gpuLink(i.gpu) : null);
        return h("span", { class: "wl-it" }, kind, h("a", { class: "lnk mono", href: "/indices/" + encodeURIComponent(i.index_id) }, i.index_id));
      }
      async function drawMain() {
        const w = st.lists.find(x => x.id === st.sel);
        if (!w) {
          main.replaceChildren(h("div", { class: "wl-empty" }, h("b", {}, "No watchlist yet."), h("p", {}, "Create one on the left, then add GPUs, providers, a GPU at one provider, a region group or an OpenGrid index. Values are observed market data, read live."),
            query.add ? h("p", { class: "warn-t" }, `Create a watchlist to add ${query.add}.`) : null));
          return;
        }
        const tbl = OG.table({
          columns: [
            { key: "item", label: "Item", sort: false, fmt: (v, r) => itemLabel(r.i) },
            { key: "low", label: "Low $/GPU·h", num: true, fmt: (v, r) => r.i.kind === "index" ? (r.l.index != null ? h("span", {}, fmt.num(r.l.index, 2), h("small", { class: "dim" }, " pts")) : OG.na(r.l.indexReason)) : r.l.low != null ? fmt.price(r.l.low) : ["gpu", "gpu_provider"].includes(r.i.kind) ? OG.na("no live priced listing") : h("span", { class: "dim" }, "–") },
            { key: "median", label: "Median", num: true, fmt: (v, r) => r.l.median != null ? fmt.price(r.l.median) : h("span", { class: "dim" }, "–") },
            { key: "chg", label: "24h", num: true, fmt: (v, r) => r.l.chg != null ? OG.chg(r.l.chg) : OG.na(r.l.chgReason) },
            { key: "avail", label: "Availability", sort: false, cls: "dim", fmt: (v, r) => r.l.avail || h("span", { class: "dim" }, "–") },
            { key: "detail", label: "Detail", sort: false, cls: "dim wrap", fmt: (v, r) => r.l.detail || "" },
            { key: "x", label: "", sort: false, csv: false, fmt: (v, r) => h("span", { class: "wl-acts" },
              ["gpu", "gpu_provider"].includes(r.i.kind) ? h("button", { class: "btn sm", type: "button", title: "Start an alert rule for this item", onclick: () => prefill(r.i) }, "Alert…") : null,
              r.i.kind === "gpu" ? h("a", { class: "btn sm", href: "/route?gpu=" + OG.slug(r.i.gpu) }, "Route") : null,
              h("button", { class: "btn sm", type: "button", title: "Remove from watchlist", onclick: () => removeItem(w, r.i) }, "✕")) },
          ],
          rows: w.items.map(i => ({ i, l: {} })), compact: true, csv: false, empty: "Empty watchlist: add an item below.",
        });
        main.replaceChildren(
          h("div", { class: "wl-mh" }, h("b", { class: "wl-name" }, w.name), h("span", { class: "dim" }, `${w.items.length} items · created ${fmt.date(w.created_at)}`), h("span", { class: "spacer" }),
            OG.kindBadge("observed"),
            h("button", { class: "btn sm", type: "button", onclick: () => rename(w) }, "Rename"),
            h("button", { class: "btn sm ky-rev", type: "button", onclick: () => removeList(w) }, "Delete")),
          tbl, addItemForm(w));
        if (!st.market) st.market = await OG.data.market(24).catch(() => null);
        const rows = await Promise.all(w.items.map(async i => ({ i, l: await live(i) })));
        if (!ctx.alive() || st.sel !== w.id) return;
        tbl.update(rows);
      }
      function addItemForm(w) {
        const kind = h("select", { class: "field", "aria-label": "Item kind" }, KINDS.map(([v, l]) => h("option", { value: v }, l)));
        const gpu = h("input", { class: "field mono", list: "wl-gpus", placeholder: "gpu slug, e.g. h100-80gb-sxm5", "aria-label": "GPU", value: query.add || "" });
        const prov = h("select", { class: "field", "aria-label": "Provider" }, OG.data.providers().sort((a, b) => a.display_name.localeCompare(b.display_name)).map(p => h("option", { value: p.name }, p.display_name)));
        const reg = h("select", { class: "field", "aria-label": "Region group" }, REGIONS.map(g => h("option", { value: g }, g)));
        const idx = h("select", { class: "field", "aria-label": "Index" }, st.indices.length ? st.indices.map(x => h("option", { value: x.id }, x.name + (x.published ? "" : " (unpublished)"))) : h("option", { value: "" }, "no indices on this server"));
        const err = h("span");
        const wrap = (n, show) => { n.hidden = !show; return n; };
        const fg = h("span", { class: "wl-f" }, gpu), fp = h("span", { class: "wl-f" }, prov), fr = h("span", { class: "wl-f" }, reg), fi = h("span", { class: "wl-f" }, idx);
        const show = () => { const k = kind.value; wrap(fg, ["gpu", "gpu_provider", "region"].includes(k)); wrap(fp, ["provider", "gpu_provider"].includes(k)); wrap(fr, k === "region"); wrap(fi, k === "index"); gpu.placeholder = k === "region" ? "gpu (optional)" : "gpu slug, e.g. h100-80gb-sxm5"; };
        kind.addEventListener("change", show); show();
        return h("form", { class: "wl-add", onsubmit: async e => {
          e.preventDefault();
          const k = kind.value, b = { kind: k };
          if (["gpu", "gpu_provider", "region"].includes(k) && gpu.value.trim()) b.gpu = gpu.value.trim();
          if (["provider", "gpu_provider"].includes(k)) b.provider = prov.value;
          if (k === "region") b.region_group = reg.value;
          if (k === "index") b.index_id = idx.value;
          try { await ctx.api(`/v1/watchlists/${w.id}/items`, { method: "POST", body: b }); soft.clear(); if (query.add) { query.add = null; OG.qs.set({ add: null }); } await loadLists(); }
          catch (e2) { err.replaceChildren(OG.error(e2)); }
        } }, h("span", { class: "lbl" }, "Add"), kind, fg, fp, fr, fi, h("button", { class: "btn pri", type: "submit" }, "Add item"), err);
      }
      async function rename(w) {
        const inp = h("input", { class: "field", value: w.name, style: "width:100%" });
        const ok = await confirm({ title: "Rename watchlist", confirm: "Rename", body: inp });
        if (!ok || !inp.value.trim()) return;
        try { await ctx.api("/v1/watchlists/" + w.id, { method: "PATCH", body: { name: inp.value.trim() } }); loadLists(); } catch (e) { main.append(OG.error(e)); }
      }
      async function removeList(w) {
        if (!await confirm({ title: `Delete "${w.name}"?`, danger: true, confirm: "Delete", body: h("p", {}, `Removes the watchlist and its ${w.items.length} items. Alert rules are separate and are kept.`) })) return;
        try { await ctx.api("/v1/watchlists/" + w.id, { method: "DELETE" }); st.sel = null; OG.qs.set({ wl: null }); loadLists(); } catch (e) { main.append(OG.error(e)); }
      }
      async function removeItem(w, i) {
        try { await ctx.api(`/v1/watchlists/${w.id}/items/${i.id}`, { method: "DELETE" }); loadLists(); } catch (e) { main.append(OG.error(e)); }
      }

      /* ----- alert rule form ----- */
      const form = { metric: "market_low", gpu: "h100-80gb-sxm5", provider: "", region_group: "US", index_id: "", window_hours: 24, window: "30d", op: "<", value: 2, name: "", cooldown: 3600, webhook: "", email: false };
      const TEMPLATES = [
        ["H100 below $2/hr", () => ({ metric: "market_low", gpu: "h100-80gb-sxm5", op: "<", value: 2 })],
        ["B200 becomes available", async () => { const g = await OG.resolveGpu("b200"); return { metric: "available", gpu: g ? g.slug : "b200", provider: "", op: ">", value: 0 }; }],
        ["Lambda H100 drops 10%", () => ({ metric: "provider_price_change_pct", gpu: "h100-80gb-sxm5", provider: "lambda", window_hours: 24, op: "<=", value: -10 })],
        ["H100 US availability falls 20%", () => ({ metric: "region_availability_change_pct", gpu: "h100-80gb-sxm5", region_group: "US", window_hours: 24, op: "<=", value: -20 })],
        ["OpenGrid H100 Index hits new 30d low", () => {
          const ids = st.indices.map(x => x.id);
          return { metric: "index_new_low", index_id: ids.includes("h100-class") ? "h100-class" : ids.find(i => i.startsWith("h100")) || "h100-class", window: "30d", op: "==", value: 1 };
        }],
      ];
      async function applyTemplate(name, fn) { Object.assign(form, await fn(), { name }); drawForm(); }
      function prefill(i) { Object.assign(form, i.kind === "gpu_provider" ? { metric: "provider_price", gpu: OG.slug(i.gpu), provider: i.provider, op: "<" } : { metric: "market_low", gpu: OG.slug(i.gpu), op: "<" }, { name: "" }); drawForm(); formBox.scrollIntoView({ block: "nearest" }); }
      function drawForm() {
        const m = METRICS[form.metric];
        const need = f => m.fields.some(x => x.replace("?", "") === f);
        const inp = (key, attrs) => h("input", Object.assign({ class: "field", value: form[key] == null ? "" : form[key], oninput: e => { form[key] = e.target.type === "number" ? (e.target.value === "" ? null : Number(e.target.value)) : e.target.value; } }, attrs));
        const sel = (key, opts, attrs) => h("select", Object.assign({ class: "field", onchange: e => { form[key] = e.target.value; if (key === "metric") { const d = { available: [">", 0], index_new_low: ["==", 1] }[e.target.value]; if (d) [form.op, form.value] = d; drawForm(); } } }, attrs),
          opts.map(([v, l]) => h("option", { value: v, selected: String(form[key]) === String(v) ? true : null }, l)));
        const f = (label, node, show) => show === false ? null : h("label", { class: "wl-ff" }, h("span", { class: "lbl" }, label), node);
        const err = h("div");
        const provOpts = OG.data.providers().sort((a, b) => a.display_name.localeCompare(b.display_name)).map(p => [p.name, p.display_name]);
        formBox.replaceChildren(h("form", { class: "box wl-rule", onsubmit: async e => {
          e.preventDefault();
          const p = { metric: form.metric, op: form.op, value: Number(form.value) };
          if (need("gpu")) p.gpu = form.gpu;
          if (need("provider") && form.provider) p.provider = form.provider;
          if (need("region_group")) p.region_group = form.region_group;
          if (need("index_id")) p.index_id = form.index_id;
          if (need("window_hours")) p.window_hours = Number(form.window_hours) || 24;
          if (need("window")) p.window = form.window || "30d";
          const channels = [];
          if (form.webhook.trim()) channels.push({ type: "webhook", url: form.webhook.trim() });
          if (form.email) channels.push({ type: "email" });
          try {
            const r = await ctx.api("/v1/alerts", { method: "POST", body: { params: p, name: form.name || null, channels, cooldown_seconds: Number(form.cooldown) || 3600 } });
            if (r.webhook_secret) showSecret(r);
            err.replaceChildren(); loadRules();
          } catch (e2) { err.replaceChildren(OG.error(e2)); }
        } },
          h("div", { class: "wl-tpl" }, h("span", { class: "lbl" }, "Templates"), TEMPLATES.map(([n, fn]) => h("button", { type: "button", class: "chip", onclick: () => applyTemplate(n, fn) }, n))),
          h("div", { class: "wl-ffs" },
            f("Metric", sel("metric", Object.entries(METRICS).map(([k, v]) => [k, v.label]))),
            f("GPU", inp("gpu", { list: "wl-gpus", class: "field mono", placeholder: "h100-80gb-sxm5" }), need("gpu")),
            f(m.fields.includes("provider?") ? "Provider (optional)" : "Provider", sel("provider", [...(m.fields.includes("provider?") ? [["", "any"]] : []), ...provOpts]), need("provider")),
            f("Region group", sel("region_group", REGIONS.map(g => [g, g])), need("region_group")),
            f("Index", sel("index_id", st.indices.length ? st.indices.map(x => [x.id, x.name]) : [[form.index_id, form.index_id || "no indices on this server"]]), need("index_id")),
            f("Window h", inp("window_hours", { type: "number", min: 1, class: "field num" }), need("window_hours")),
            f("Window", inp("window", { class: "field num", placeholder: "30d" }), need("window")),
            f("Condition", h("span", { class: "wl-cond" }, sel("op", OPS.map(o => [o, o])), inp("value", { type: "number", step: "any", class: "field num" }), h("span", { class: "dim" }, m.unit))),
            f("Name", inp("name", { placeholder: "optional", maxlength: 200 })),
            f("Cooldown s", inp("cooldown", { type: "number", min: 60, class: "field num" }))),
          h("div", { class: "wl-ch" },
            h("span", { class: "lbl" }, "Channels"),
            h("span", { class: "wl-chi" }, h("input", { type: "checkbox", checked: true, disabled: true }), "in-app feed (always)"),
            h("label", { class: "wl-chi" }, "webhook ", inp("webhook", { type: "url", placeholder: "https://… (signed, HMAC-SHA256)", style: "width:280px" })),
            h("label", { class: "wl-chi dim", title: "Email delivery is an interface only: a firing records not_implemented" }, h("input", { type: "checkbox", checked: form.email ? true : null, onchange: e => { form.email = e.target.checked; } }), "email — ", h("b", { class: "warn-t" }, "not implemented yet"), " (recorded as not_implemented, never sent)"),
            h("span", { class: "spacer" }), h("button", { class: "btn pri", type: "submit" }, "Create rule")),
          h("p", { class: "note" }, m.help, ". Edge-triggered: fires on the transition to true, then waits for the cooldown; an unknown reading (thin history, provider not recorded) never fires. Webhook URLs must be https and public; the signing secret is shown once."),
          err));
      }
      function showSecret(r) {
        secretBox.replaceChildren(h("div", { class: "ky-secret", role: "alert" },
          h("div", { class: "ky-secret-h" }, OG.badge("shown once", "warn"), h("b", {}, `Webhook signing secret for rule #${r.id}`), h("span", { class: "spacer" }), h("button", { class: "btn sm", type: "button", onclick: () => secretBox.replaceChildren() }, "Done")),
          h("p", { class: "ky-warn" }, "Verify X-OpenGrid-Signature: v1=HMAC-SHA256(secret, \"<X-OpenGrid-Timestamp>.<raw body>\"); reject timestamps older than ~5 minutes. OpenGrid cannot show this secret again."),
          h("div", { class: "ky-secret-v" }, h("code", { class: "mono" }, r.webhook_secret), OG.copy(r.webhook_secret, { cls: "pri", title: "Copy the webhook signing secret" }))));
      }

      /* ----- rules ----- */
      const rulesTbl = OG.table({
        columns: [
          { key: "name", label: "Rule", fmt: (v, r) => h("span", {}, h("b", {}, v || "#" + r.id), h("div", { class: "dim wl-cnd" }, condText(r.params))) , value: r => r.name || "" },
          { key: "state", label: "State", value: r => r.status === "paused" ? "paused" : r.last_state || "", fmt: (v, r) => stateBadge(r) },
          { key: "last_value", label: "Last value", num: true, fmt: (v, r) => v != null ? fmt.num(v, Math.abs(v) < 10 ? 3 : 1) : r.last_state === "unknown" ? OG.na(r.last_detail) : h("span", { class: "dim" }, "–") },
          { key: "last_detail", label: "Reading", cls: "wrap dim", fmt: v => v || "" },
          { key: "last_evaluated_at", label: "Evaluated", num: true, fmt: v => v ? fmt.age(v) + " ago" : h("span", { class: "dim" }, "never") },
          { key: "last_fired_at", label: "Last fired", num: true, fmt: v => v ? fmt.dateTime(v) : h("span", { class: "dim" }, "never") },
          { key: "channels", label: "Channels", sort: false, fmt: v => h("span", { class: "wl-chs" }, (v || []).map(c => OG.badge(c.type, c.type === "email" ? "warn" : "", c.type === "email" ? "not implemented: never sent" : c.url || null))) },
          { key: "x", label: "", sort: false, csv: false, fmt: (v, r) => h("span", { class: "wl-acts" },
            h("button", { class: "btn sm", type: "button", title: "Evaluate now: no state change, no delivery", onclick: () => test(r) }, "Test now"),
            h("button", { class: "btn sm", type: "button", onclick: () => toggle(r) }, r.status === "paused" ? "Resume" : "Pause"),
            h("button", { class: "btn sm", type: "button", title: "Delete rule", onclick: () => delRule(r) }, "✕")) },
        ],
        rows: [], compact: true, csv: "opengrid-alert-rules.csv", rowKey: r => r.id, sort: { key: "name", dir: "asc" },
        empty: "No alert rules. Start from a template above.",
      });
      const testBox = h("div");
      async function loadRules() {
        try {
          st.rules = await ctx.api("/v1/alerts", { nocache: true });
          rulesTbl.update(st.rules);
          if (rulesBox.firstChild !== testBox) rulesBox.replaceChildren(testBox, rulesTbl, h("p", { class: "note" }, "State is the last evaluation by the alerts job (about every 3 minutes, on servers where background jobs run). ",
            h("b", {}, "ok"), " = condition false, ", h("b", {}, "firing"), " = true, ", h("b", {}, "unknown"), " = no honest reading (reason shown); unknown never fires."));
          loadFirings();
        } catch (e) { rulesBox.replaceChildren(OG.error(e, loadRules)); }
      }
      async function test(r) {
        testBox.replaceChildren(OG.loading("Evaluating rule #" + r.id + "…"));
        try {
          const t = await ctx.api(`/v1/alerts/${r.id}/test`, { method: "POST" });
          const tone = t.state === "true" ? "bad" : t.state === "false" ? "good" : "warn";
          testBox.replaceChildren(h("div", { class: "wl-test box" },
            h("span", { class: "eyebrow" }, "Test · dry run"), h("b", {}, r.name || "#" + r.id),
            OG.badge(t.state === "true" ? "condition true" : t.state === "false" ? "condition false" : "unknown", tone),
            h("span", { class: "mono" }, t.value != null ? "value " + fmt.num(t.value, Math.abs(t.value) < 10 ? 4 : 2) : "no value"),
            h("span", { class: "dim" }, t.detail || ""),
            h("span", {}, t.would_fire ? h("b", { class: "down" }, "would fire now") : "would not fire: " + t.reason),
            h("span", { class: "spacer" }), h("span", { class: "dim mono" }, fmt.time(t.evaluated_at) + " · nothing stored or sent"),
            h("button", { class: "btn sm", type: "button", onclick: () => testBox.replaceChildren() }, "✕")));
        } catch (e) { testBox.replaceChildren(OG.error(e)); }
      }
      async function toggle(r) {
        try { await ctx.api("/v1/alerts/" + r.id, { method: "PATCH", body: { status: r.status === "paused" ? "active" : "paused" } }); loadRules(); } catch (e) { rulesBox.append(OG.error(e)); }
      }
      async function delRule(r) {
        if (!await confirm({ title: `Delete rule "${r.name || "#" + r.id}"?`, danger: true, confirm: "Delete", body: h("p", {}, condText(r.params)) })) return;
        try { await ctx.api("/v1/alerts/" + r.id, { method: "DELETE" }); loadRules(); } catch (e) { rulesBox.append(OG.error(e)); }
      }
      async function loadFirings() {
        try {
          const fs = await ctx.api("/v1/alerts/firings", { params: { hours: 168 }, nocache: true });
          const names = new Map(st.rules.map(r => [r.id, r.name || "#" + r.id]));
          firingsBox.replaceChildren(OG.table({
            columns: [
              { key: "fired_at", label: "Fired", num: true, fmt: v => fmt.dateTime(v) },
              { key: "rule_id", label: "Rule", value: r => names.get(r.rule_id) || "#" + r.rule_id },
              { key: "value", label: "Value", num: true, fmt: v => v != null ? fmt.num(v, 3) : "–" },
              { key: "message", label: "Message", cls: "wrap" },
              { key: "delivery_status", label: "Delivery", sort: false, fmt: v => h("span", { class: "wl-chs" }, Object.entries(v || {}).map(([k, s]) => OG.badge(`${k}: ${s}`, s === "ok" ? "good" : s === "not_implemented" ? "warn" : "bad"))) },
            ], rows: fs, sort: { key: "fired_at", dir: "desc" }, compact: true, csv: "opengrid-alert-firings.csv", empty: "No alert has fired in the last 7 days.",
          }));
        } catch (e) { firingsBox.replaceChildren(OG.error(e, loadFirings)); }
      }

      drawForm();
      await loadLists();
      loadRules();
      ctx.every(60000, () => { soft.clear(); st.market = null; loadLists(); loadRules(); });
    },
  });
})();
