/* /rl-cost: "RL cost". An RL post-training job (trainer + rollout GPUs + sandboxes + weight syncs) priced with
   everything on one provider and split across providers, from POST /v1/costs/rl-estimate (methodology/rl-cost.md).
   Unknown lines are the word "unknown" and make a total a lower bound ("≥ $X"); never drawn as $0. The break-even
   and the sensitivity table say when splitting stops paying. Pure helpers live on OG.rlCost (tests/test_web_rl_cost.js). */
(() => {
  const { h } = OG;

  /* =============================== pure =============================== */
  const RULE = "List prices, not quotes. An estimate of a plan, not a measured run: rollout throughput is not benchmarked and spot reclaim rates are not measured.";
  const STRATS = [["same_cluster", "Same cluster", "Trainer and every rollout GPU on one provider (the baseline)"],
    ["split", "Split", "Cheapest trainer + cheapest eligible rollout GPUs anywhere, weights shipped across clouds"],
    ["best", "Best found", "The cheapest plan OpenGrid found across trainer providers and rollout mixes"]];
  const ITEM = { trainer_compute: "Trainer GPUs", trainer_storage: "Checkpoint storage", rollout_compute: "Rollout GPUs",
    sandbox: "Sandboxes (CPU)", sync_egress: "Weight sync: egress", sync_ingress: "Weight sync: ingress",
    sync_intra_cluster: "Weight sync: inside the cluster" };
  const KIND = { observed: "observed", manual: "hand table", user_input: "your input", inferred: "inferred", assumption: "assumption" };
  const PARTS = [["trainer", "Trainer"], ["storage", "Storage"], ["rollouts", "Rollouts"], ["sandbox", "Sandboxes"], ["sync", "Weight sync"]];
  const DEFAULTS = { preset: "RL1", trainer_gpu: "NVIDIA H100 80GB SXM5", trainer_count: "8", trainer_nodes: "1",
    rollout_gpu: "auto", rollout_count: "32", min_vram_gb: "80", interruptible: false, overhead: "1.15",
    weights_gb: "140", syncs_per_hour: "4", sync_fanout: "", hours: "72", storage_tb: "2",
    sb_vcpu: "", sb_ram_gb: "", sb_count: "", sb_price: "", same_cluster_only: false };

  // A preset (GET /v1/costs/rl-presets row) -> form state. The API's gpu fields may be public ids: keep gpu_name.
  function fromPreset(p) {
    const t = p.trainer || {}, r = p.rollouts || {}, s = p.sandboxes || {};
    const str = v => (v == null ? "" : String(v));
    return { preset: p.preset, trainer_gpu: t.gpu_name || t.gpu || DEFAULTS.trainer_gpu, trainer_count: str(t.count), trainer_nodes: str(t.nodes || 1),
      rollout_gpu: r.gpu === "auto" || !r.gpu ? "auto" : r.gpu_name || r.gpu, rollout_count: str(r.count), min_vram_gb: str(r.min_vram_gb),
      interruptible: !!r.interruptible, overhead: str(r.interruption_overhead), weights_gb: str(p.weights_gb), syncs_per_hour: str(p.syncs_per_hour),
      sync_fanout: str(p.sync_fanout), hours: str(p.hours), storage_tb: p.storage_gb == null ? "" : String(p.storage_gb / 1000),
      sb_vcpu: str(s.vcpu), sb_ram_gb: str(s.ram_gb), sb_count: str(s.count), sb_price: str(s.price_per_hour), same_cluster_only: !!p.same_cluster_only };
  }

  // Form state -> {body} or {errors: {field: message}}. Bounds mirror the API (cost/rl.py parse).
  function buildBody(st) {
    const errors = {};
    const blank = v => v === "" || v == null;
    const num = (k, lo, hi, msg, opt) => {
      if (blank(st[k])) { if (!opt) errors[k] = msg; return null; }
      const v = Number(st[k]);
      if (!isFinite(v) || v < lo || v > hi) { errors[k] = msg; return null; }
      return v;
    };
    const int = (k, lo, hi, msg, opt) => {
      const v = num(k, lo, hi, msg, opt);
      if (v != null && !Number.isInteger(v)) { errors[k] = msg; return null; }
      return v;
    };
    if (!st.trainer_gpu) errors.trainer_gpu = "Pick a GPU";
    const trainer = { gpu: st.trainer_gpu, count: int("trainer_count", 1, 64, "1 to 64 GPUs per node"), nodes: int("trainer_nodes", 1, 128, "1 to 128 nodes") };
    const rollouts = { gpu: st.rollout_gpu || "auto", count: int("rollout_count", 1, 4096, "1 to 4,096 GPUs"),
      min_vram_gb: num("min_vram_gb", 0, 1024, "0 to 1,024 GB", true) || 0, interruptible: !!st.interruptible,
      interruption_overhead: num("overhead", 1, 5, "1.0 to 5.0", true) };
    if (rollouts.interruption_overhead == null) delete rollouts.interruption_overhead;
    const body = { trainer, rollouts, weights_gb: num("weights_gb", 0.001, 100000, "GB per sync, above 0"),
      syncs_per_hour: num("syncs_per_hour", 0, 3600, "0 to 3,600"), hours: num("hours", 1 / 60, 8760, "1 minute to 8,760 hours"),
      storage_gb: (num("storage_tb", 0, 10000, "0 to 10,000 TB", true) || 0) * 1000, same_cluster_only: !!st.same_cluster_only };
    const fan = int("sync_fanout", 1, 1024, "1 to 1,024 copies, or blank", true);
    if (fan != null) body.sync_fanout = fan;
    const sbCount = int("sb_count", 0, 100000, "0 to 100,000", true);
    if (sbCount) {
      body.sandboxes = { count: sbCount, vcpu: num("sb_vcpu", 0, 1024, "0 to 1,024", true) || 0, ram_gb: num("sb_ram_gb", 0, 8192, "0 to 8,192", true) || 0 };
      const pr = num("sb_price", 0, 1000000, "$ per hour, 0 or more", true);
      if (pr != null) body.sandboxes.price_per_hour = pr;
      if (!body.sandboxes.vcpu && !body.sandboxes.ram_gb) errors.sb_vcpu = "vCPU or RAM per sandbox";
    }
    return Object.keys(errors).length ? { errors } : { body };
  }

  const money = v => (v == null || !isFinite(v) ? null : "$" + Number(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
  // "≥ $630.00" for a lower bound, "$630.00" otherwise, null when no total
  function totalText(p) {
    if (!p) return null;
    const m = money(p.total);
    return m == null ? null : (p.total_is_lower_bound ? "≥ " + m : m);
  }
  // A saving with its honesty bound: exact, at most (split is a lower bound), at least (baseline is), indeterminate.
  function savingText(s) {
    if (!s || s.saving == null) return null;
    const neg = s.saving < 0, m = money(Math.abs(s.saving));
    const base = neg ? `${m} more` : `${m} saved`;
    const pct = s.saving_pct == null ? "" : ` (${Math.abs(s.saving_pct).toFixed(1)}%)`;
    const pre = { exact: "", at_most: "at most ", at_least: "at least ", indeterminate: "about " }[s.bound] || "";
    return pre + base + pct + (s.bound === "indeterminate" ? ", both totals are lower bounds" : "");
  }
  function unknownText(p) {
    const u = (p && p.unknown) || [];
    if (!u.length) return null;
    return "Not published, not counted: " + u.map(x => {
      const [k, prov] = x.split(":");
      const name = { trainer_compute: "trainer price", trainer_storage: "storage", sync_egress: "egress", sync_ingress: "ingress", sandbox: "sandboxes" }[k] || k;
      return prov ? `${name} (${OG.providerName ? OG.providerName(prov) : prov})` : name;
    }).join(", ");
  }
  // A line's amount cell: {text, tone, title}. Unknown is a word, never $0.
  function lineAmount(ln) {
    const notes = (ln.notes || []).join("; ");
    if (!ln.known || ln.amount == null) return { text: "unknown", tone: "warn", title: (notes ? notes + ". " : "") + "Left out of the total, which is therefore a lower bound" };
    const bits = [KIND[ln.kind] || ln.kind, ln.product, notes].filter(Boolean);
    return { text: money(ln.amount), tone: ln.amount === 0 ? (ln.kind === "assumption" ? "dim" : "good") : "", title: bits.join(" · ") || null };
  }
  function lineWhat(ln) {
    const g = ln.gpu_name || ln.gpu;
    if (ln.item === "trainer_compute" || ln.item === "rollout_compute") {
      const head = `${ln.gpus}× ${g ? (OG.shortGpu ? OG.shortGpu(g) : g) : "GPU"}`;
      return head + (ln.machines ? ` on ${ln.machines} machine${ln.machines === 1 ? "" : "s"}` : "") + (ln.interruptible ? " · interruptible" : "");
    }
    if (ln.gb != null) return Number(ln.gb).toLocaleString("en-US", { maximumFractionDigits: 0 }) + " GB";
    if (ln.item === "sandbox") return `${ln.vcpu} vCPU · ${ln.ram_gb} GB RAM in total`;
    return "";
  }
  function lineRate(ln) {
    if (ln.unit_price != null) return `${ln.billed_hours} h × ${money(ln.unit_price)}/h` + (ln.interruption_overhead && ln.interruption_overhead !== 1 ? ` × ${ln.interruption_overhead}` : "");
    return "";
  }
  // Sensitivity -> grid rows: [{overhead, cells: [{syncs, text, tone, current, title}]}]
  function sensGrid(sens) {
    if (!sens || !sens.rows) return [];
    return (sens.interruption_overheads || []).map(o => ({ overhead: o, cells: (sens.syncs_per_hour || []).map(s => {
      const c = sens.rows.find(r => r.interruption_overhead === o && r.syncs_per_hour === s) || {};
      const t = c.saving == null ? "n/a" : (c.saving >= 0 ? "" : "−") + money(Math.abs(c.saving)).replace(/\.\d\d$/, "");
      return { syncs: s, text: (c.saving != null && c.saving_bound && c.saving_bound !== "exact" ? "~" : "") + t,
        tone: c.saving == null ? "dim" : c.saving > 0 ? "up" : "down", current: !!c.current,
        title: c.saving == null ? "No plan for this pair" : `Same cluster ${money(c.same_cluster_total)} vs split ${money(c.split_total)}${c.lower_bound ? " (a total is a lower bound)" : ""}` };
    }) }));
  }

  OG.rlCost = { RULE, STRATS, ITEM, KIND, PARTS, DEFAULTS, fromPreset, buildBody, money, totalText, savingText, unknownText, lineAmount, lineWhat, lineRate, sensGrid };
  if (typeof OG.page !== "function" || typeof document === "undefined") return;   // node tests: pure part only

  /* =============================== page =============================== */
  const cell = v => h("span", { class: "mono " + (v.tone === "warn" ? "tc-unk" : v.tone === "good" ? "up" : v.tone === "dim" ? "dim" : ""), title: v.title || null }, v.text);
  const totalEl = p => h("span", { class: "tc-total", title: unknownText(p) || "every line this plan uses is priced" },
    h("b", { class: "mono" }, totalText(p) || "unknown"), p && p.total_is_lower_bound ? h("span", {}, " ", OG.badge("lower bound", "warn", unknownText(p))) : null);
  const provs = list => h("span", {}, (list || []).map((p, i) => [i ? ", " : "", OG.providerLink(p)]));

  function compareTable(d) {
    const rows = STRATS.map(([k, label, title]) => ({ key: k, label, title, p: d[k], why: (d.unavailable || {})[k] }));
    return OG.table({ rows, csv: false, autoHide: false, columns: [
      { key: "label", label: "Plan", sort: false, fmt: (v, r) => h("span", { title: r.title }, h("b", {}, v), r.p && r.p.found_as ? h("span", { class: "dim" }, ` (${r.p.found_as.replace("_", " ")})`) : null) },
      { key: "trainer", label: "Trainer on", sort: false, fmt: (v, r) => r.p ? OG.providerLink(r.p.trainer_provider) : h("span", { class: "dim" }, r.why || "-") },
      { key: "rollouts", label: "Rollouts on", sort: false, fmt: (v, r) => r.p ? provs(r.p.rollout_providers) : "" },
      ...PARTS.map(([k, label]) => ({ key: k, label, num: true, sort: false, fmt: (v, r) => {
        const pt = r.p && r.p.parts[k];
        if (!pt || pt.amount == null) return h("span", { class: "dim" }, r.p ? "-" : "");
        return pt.complete ? h("span", { class: "mono" }, money(pt.amount)) : h("span", { class: "mono tc-unk", title: "part of this is unknown, so this is at least" }, "≥ " + money(pt.amount));
      } })),
      { key: "total", label: "Total", num: true, sort: false, fmt: (v, r) => r.p ? totalEl(r.p) : "" },
    ] });
  }

  function linesTable(p) {
    return OG.table({ rows: p.lines, csv: "rl-cost-" + p.strategy, autoHide: false, columns: [
      { key: "item", label: "Line", fmt: v => ITEM[v] || v },
      { key: "provider", label: "Provider", fmt: v => (v ? OG.providerLink(v) : h("span", { class: "dim" }, "-")) },
      { key: "what", label: "What", sort: false, value: r => lineWhat(r), fmt: (v, r) => h("span", { class: "mono dim" }, lineWhat(r), r.sku ? h("span", { class: "dimmer" }, " · " + r.sku) : null) },
      { key: "rate", label: "Rate", sort: false, value: r => lineRate(r), fmt: (v, r) => h("span", { class: "mono dim" }, lineRate(r)) },
      { key: "amount", label: "Cost", num: true, fmt: (v, r) => cell(lineAmount(r)) },
      { key: "kind", label: "Price from", fmt: (v, r) => (r.known ? h("span", { class: "dim", title: r.source || null }, KIND[v] || v || "-") : h("span", { class: "tc-unk" }, "not published")) },
    ] });
  }

  function breakEven(be, job) {
    if (!be || !be.available) return h("p", { class: "note dim" }, (be && be.reason) ? "No break-even: " + be.reason + "." : "No break-even.");
    return h("div", {},
      be.summary ? h("p", { class: "note" }, h("b", {}, be.summary)) : null,
      OG.stats([
        { label: "Saving before transfer", value: money(be.margin_before_transfer), title: "Same-cluster total minus the split total without moving the weights" },
        { label: "Weights moved", value: Number(be.transfer_gb).toLocaleString("en-US", { maximumFractionDigits: 0 }) + " GB", sub: job ? `${job.weights_gb} GB × ${job.syncs_per_hour}/h × ${job.hours} h × fanout` : null },
        { label: "Transfer now", value: be.current_price_per_gb == null ? null : "$" + be.current_price_per_gb.toFixed(4) + "/GB", reason: "an egress or ingress price on this route is not published" },
        { label: "Break-even price", value: be.break_even_price_per_gb == null ? null : "$" + be.break_even_price_per_gb.toFixed(4) + "/GB", title: "Above this transfer price (egress + ingress), splitting costs more" },
        { label: "Break-even syncs", value: be.break_even_syncs_per_hour == null ? null : be.break_even_syncs_per_hour.toLocaleString("en-US", { maximumFractionDigits: 2 }) + "/h",
          reason: be.splits_save === true ? "transfer is free on this route" : "needs a published transfer price" },
      ]),
      be.lower_bound_inputs ? h("p", { class: "note dim" }, "A line other than weight transfer is unknown in one of the plans, so the break-even is indicative.") : null);
  }

  function sensTable(sens) {
    const grid = sensGrid(sens);
    if (!grid.length) return null;
    return h("div", { class: "tbl" }, h("div", { class: "tbl-scroll" }, h("table", { class: "grid-t compact" },
      h("thead", {}, h("tr", {}, h("th", {}, "Overhead ↓ / syncs per hour →"), (sens.syncs_per_hour || []).map(s => h("th", { class: "num" }, String(s))))),
      h("tbody", {}, grid.map(r => h("tr", {}, h("td", { class: "mono" }, r.overhead + "×"),
        r.cells.map(c => h("td", { class: "num mono " + (c.tone || ""), title: c.title }, c.current ? h("b", {}, c.text + " ◆") : c.text))))))),
    h("p", { class: "note dim" }, "Split saving against the same-cluster plan (negative: splitting costs more). ◆ is your job; ~ means a total in that cell is a lower bound. " + (sens.note || "")));
  }

  function rankTable(rk) {
    if (!rk || !rk.rows || !rk.rows.length) return OG.empty("No eligible rollout GPU listing.");
    return h("div", {}, h("p", { class: "note" }, OG.badge("heuristic", "warn"), " ", rk.basis),
      OG.table({ rows: rk.rows, csv: "rl-rollout-ranking", autoHide: false, sort: { key: "bandwidth_per_dollar", dir: "desc" }, columns: [
        { key: "gpu", label: "GPU", fmt: (v, r) => OG.gpuLink ? OG.gpuLink(r.gpu_name || v) : (r.gpu_name || v) },
        { key: "vram_gb", label: "VRAM", num: true, fmt: v => v + " GB" },
        { key: "bandwidth_tbps", label: "Mem. bandwidth", num: true, fmt: v => (v == null ? "n/a" : v + " TB/s") },
        { key: "price_per_gpu_hour", label: "$/GPU-hr", num: true, title: "Cheapest eligible listing, with the interruption overhead on interruptible ones", fmt: v => "$" + v.toFixed(2) },
        { key: "provider", label: "Cheapest at", fmt: (v, r) => [OG.providerLink(v), r.interruptible ? h("span", { class: "dim" }, " · spot") : null] },
        { key: "bandwidth_per_dollar", label: "TB/s per $/hr", num: true, desc: true, fmt: v => (v == null ? "n/a" : v.toFixed(2)) },
      ] }));
  }

  OG.page("/rl-cost", {
    title: "RL cost",
    nav: "rl-cost",
    async mount(el, params, query, ctx) {
      const st = Object.assign({}, DEFAULTS);
      for (const k of Object.keys(DEFAULTS)) {
        const v = OG.qs.get(k, null);
        if (v != null) st[k] = typeof DEFAULTS[k] === "boolean" ? v === "1" || v === "true" : v;
      }
      const form = h("form", { class: "tc-form", onsubmit: ev => { ev.preventDefault(); run(); } });
      const out = h("div", {});
      el.append(OG.head("RL cost", "What an RL post-training job costs with the trainer and rollouts on one cloud, or split across clouds with the weights shipped after every update. ",
        OG.kindBadge("estimated")),
      h("div", { class: "access-note k-public" }, h("b", {}, "Estimate"), h("span", {}, " " + RULE)),
      form, out,
      h("p", { class: "note" }, h("a", { class: "lnk", href: "/methodology/rl-cost" }, "How the RL cost is computed, the defaults, and every known gap →")));

      const [pr, gl] = await Promise.all([OG.all(ctx.api, "/v1/costs/rl-presets").catch(() => null), OG.data.gpus().catch(() => [])]);
      const presets = Array.isArray(pr) ? pr : [];
      const gpus = (gl || []).filter(g => g.live).sort((a, b) => b.weight - a.weight || a.name.localeCompare(b.name));
      for (const g of [st.trainer_gpu, st.rollout_gpu]) if (g && g !== "auto" && !gpus.some(x => x.name === g)) gpus.unshift({ name: g, short: OG.shortGpu(g) });

      const errs = {};
      const err = k => h("em", { class: "tc-err", "data-err": k });
      const field = (k, label, input, title) => h("label", { class: "flt", title: title || null }, h("span", {}, label), input, err(k));
      const num = (k, attrs) => h("input", Object.assign({ class: "field", type: "number", value: st[k], inputmode: "decimal", "aria-label": k,
        oninput: ev => { st[k] = ev.target.value.trim(); st.preset = ""; } }, attrs));
      const check = (k, label, title) => h("label", { class: "flt", title }, h("input", { type: "checkbox", checked: st[k] ? true : null,
        onchange: ev => { st[k] = ev.target.checked; st.preset = ""; drawForm(); } }), h("span", {}, label));
      const gpuSel = (k, auto) => h("select", { class: "field", "aria-label": k, onchange: ev => { st[k] = ev.target.value; st.preset = ""; } },
        auto ? h("option", { value: "auto", selected: st[k] === "auto" ? "" : null }, "Auto (any GPU with enough VRAM)") : null,
        gpus.map(g => h("option", { value: g.name, selected: g.name === st[k] ? "" : null }, g.short || g.name)));
      function drawForm() {
        form.replaceChildren(
          h("div", { class: "bar" },
            h("span", { class: "flt" }, h("span", {}, "Job"), OG.seg([["", "Custom", "Set every input yourself"], ...presets.map(p => [p.preset, p.preset, p.label])],
              st.preset, v => { if (v) Object.assign(st, fromPreset(presets.find(p => p.preset === v))); else st.preset = ""; drawForm(); run(); })),
            st.preset ? h("span", { class: "dim" }, (presets.find(p => p.preset === st.preset) || {}).label || "") : null),
          h("div", { class: "bar" }, h("b", { class: "dim" }, "Trainer"),
            field("trainer_gpu", "GPU", gpuSel("trainer_gpu", false)),
            field("trainer_count", "GPUs / node", num("trainer_count", { min: 1, max: 64, step: 1, style: "width:72px" })),
            field("trainer_nodes", "Nodes", num("trainer_nodes", { min: 1, max: 128, step: 1, style: "width:64px" })),
            field("storage_tb", "Checkpoints TB", num("storage_tb", { min: 0, step: "any", style: "width:72px" }), "Kept on the trainer's provider for the whole job, decimal TB")),
          h("div", { class: "bar" }, h("b", { class: "dim" }, "Rollouts"),
            field("rollout_gpu", "GPU", gpuSel("rollout_gpu", true)),
            field("rollout_count", "GPUs", num("rollout_count", { min: 1, max: 4096, step: 1, style: "width:72px" })),
            field("min_vram_gb", "Min VRAM GB", num("min_vram_gb", { min: 0, step: "any", style: "width:72px" }), "Memory each rollout GPU needs for its model shard and KV cache"),
            check("interruptible", "Spot OK", "Allow interruptible (spot) rollout GPUs"),
            st.interruptible ? field("overhead", "Overhead ×", num("overhead", { min: 1, max: 5, step: 0.05, style: "width:64px" }), "Extra rollout cost from reclaimed spot machines: an assumption, not measured") : null),
          h("div", { class: "bar" }, h("b", { class: "dim" }, "Weight sync"),
            field("weights_gb", "GB per sync", num("weights_gb", { min: 0.001, step: "any", style: "width:72px" })),
            field("syncs_per_hour", "Syncs / h", num("syncs_per_hour", { min: 0, step: "any", style: "width:64px" })),
            field("sync_fanout", "Fanout", num("sync_fanout", { min: 1, step: 1, style: "width:64px", placeholder: "auto" }), "Copies of each sync that leave the trainer's cloud. Blank: one per rollout provider"),
            field("hours", "Hours", num("hours", { min: 0.017, step: "any", style: "width:72px" }))),
          h("div", { class: "bar" }, h("b", { class: "dim" }, "Sandboxes"),
            field("sb_count", "Count", num("sb_count", { min: 0, step: 1, style: "width:72px", placeholder: "none" })),
            field("sb_vcpu", "vCPU each", num("sb_vcpu", { min: 0, step: "any", style: "width:64px" })),
            field("sb_ram_gb", "RAM GB each", num("sb_ram_gb", { min: 0, step: "any", style: "width:64px" })),
            field("sb_price", "$ / h (all)", num("sb_price", { min: 0, step: "any", style: "width:72px", placeholder: "unknown" }), "Your price for the whole sandbox fleet per hour. OpenGrid has no CPU price dataset yet"),
            check("same_cluster_only", "Same cluster only", "Price only the one-provider plan (RL3)"),
            h("button", { class: "btn pri", type: "submit" }, "Estimate")));
        for (const [k, m] of Object.entries(errs)) { const e = form.querySelector(`[data-err="${k}"]`); if (e) e.textContent = m; }
      }

      async function run() {
        for (const k of Object.keys(errs)) delete errs[k];
        const b = buildBody(st);
        OG.qs.set(Object.fromEntries(Object.keys(DEFAULTS).map(k => [k, st[k] === DEFAULTS[k] ? null : typeof st[k] === "boolean" ? (st[k] ? "1" : "0") : st[k] || null])));
        if (b.errors) { Object.assign(errs, b.errors); drawForm(); out.replaceChildren(); return; }
        drawForm();
        out.replaceChildren(OG.loading("Pricing the trainer, rollouts and weight syncs on every provider…"));
        let res;
        try { res = await ctx.api("/v1/costs/rl-estimate", { method: "POST", body: b.body, full: true, slot: "rl-cost" }); }
        catch (e) { if (!e.stale) out.replaceChildren(OG.error(e, run)); return; }
        const d = res.data || {}, m = res.meta || {}, job = m.job || {};
        OG.status.asOf(m.as_of);
        const sc = d.same_cluster, sp = d.split, best = d.best, cmp = d.comparison || {};
        if (!sc && !sp && !best) { out.replaceChildren(OG.empty("No plan: " + Object.values(d.unavailable || {}).join("; ") + ". Try another trainer GPU or size, a lower VRAM minimum, or allow spot rollouts.")); return; }
        let tab = best ? "best" : sp ? "split" : "same_cluster";
        const linesBox = h("div", {});
        const drawLines = () => {
          const p = d[tab];
          linesBox.replaceChildren(OG.tabs(STRATS.filter(([k]) => d[k]).map(([k, l]) => [k, l]), tab, v => { tab = v; drawLines(); }),
            p ? h("div", {}, h("p", { class: "note" }, h("b", {}, totalText(p)), " · ", p.split ? `${Number(p.sync.transfer_gb).toLocaleString("en-US")} GB of weights leave ${OG.providerName(p.trainer_provider)} (${p.sync.fanout} cop${p.sync.fanout === 1 ? "y" : "ies"} per sync; ${p.sync.fanout_basis})` : "no cross-cloud transfer",
              unknownText(p) ? h("span", { class: "tc-unk" }, " · " + unknownText(p)) : null),
            linesTable(p), (p.notes || []).length ? h("ul", { class: "note" }, p.notes.map(n => h("li", { class: "dim" }, n))) : null) : null);
        };
        drawLines();
        const clb = d.cheapest_lower_bound;
        out.replaceChildren(
          OG.stats([
            { label: "Same cluster", value: sc ? totalEl(sc) : null, reason: (d.unavailable || {}).same_cluster, sub: sc ? OG.providerName(sc.trainer_provider) : null },
            { label: "Split", value: sp ? totalEl(sp) : null, reason: (d.unavailable || {}).split, sub: sp ? `${OG.providerName(sp.trainer_provider)} → ${sp.rollout_providers.map(OG.providerName).join(", ")}` : null },
            { label: "Best found", value: best ? totalEl(best) : null, reason: (d.unavailable || {}).best, sub: best ? `${d.plans_searched} plans compared` : null },
            { label: "Split vs same cluster", value: savingText(cmp.split_vs_same_cluster), reason: "needs both plans" },
          ]),
          OG.section("Plans side by side", compareTable(d),
            clb ? h("p", { class: "note dim" }, `A plan with unpublished prices shows ${totalText(clb)} (${OG.providerName(clb.trainer_provider)} trainer). It could cost more than it shows, so it is not ranked above a complete total.`) : null),
          OG.section("Line items", linesBox),
          d.break_even && !job.same_cluster_only ? OG.section("When does splitting stop paying?", breakEven(d.break_even, job)) : null,
          d.sensitivity ? OG.section("Sensitivity: interruption overhead × syncs per hour", sensTable(d.sensitivity)) : null,
          OG.section("Rollout GPUs by memory bandwidth per dollar", rankTable(d.rollout_ranking)),
          OG.section("What this does not know", h("ul", { class: "note" }, (d.caveats || []).map(c => h("li", {}, c)))),
          (m.notes || []).length ? OG.section("Notes", h("ul", { class: "note" }, m.notes.map(n => h("li", { class: "dim" }, n)))) : null);
      }
      drawForm();
      await run();
    },
  });
})();
