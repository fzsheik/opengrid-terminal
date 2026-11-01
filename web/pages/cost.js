/* /cost: "True cost". What a job costs per provider beyond the hourly price: compute rounded to the provider's billing
   increment, internet egress through its tiers, and storage, from POST /v1/cost/estimate (methodology/full-cost.md).
   A component the provider does not publish (or publishes only in a value OpenGrid could not verify) is shown as
   unknown and makes the total a lower bound ("≥ $X"); it is never drawn as $0. Pure helpers live on OG.trueCost
   (tests/test_web_cost.js). */
(() => {
  const { h } = OG;

  /* =============================== pure =============================== */
  const RULE = "List prices, not quotes. Estimates only: never part of listings, indices, spreads, market statistics or routing.";
  const EMPTY = gpu => `No current on-demand listing of ${gpu || "this GPU"} with exactly this many GPUs per machine. `
    + "Try another GPU count (most clouds sell 1 or 8 per machine) or another GPU.";
  const PRESETS = ["R0", "R1", "R2", "R3"];
  const STORAGE = [["auto", "Auto"], ["shared_fs", "Shared filesystem"],
    ["block_storage", "Block volume"], ["object_storage", "Object storage"]];
  const FABRIC = [["", "Any"], ["400", "≥ 400 Gbps"], ["1600", "≥ 1,600 Gbps"], ["3200", "≥ 3,200 Gbps (InfiniBand class)"]];
  const UNKNOWN_LABEL = { compute: "compute", egress_internet: "egress", storage: "storage" };
  const FABRIC_NAME = { infiniband: "InfiniBand", roce: "RoCE", efa: "EFA", tcpx: "GPUDirect-TCPX", ethernet: "Ethernet" };
  const DEFAULTS = { gpu: "NVIDIA H100 80GB SXM5", count: "8", hours: "72", egress_tb: "10", storage_tb: "20",
    storage_class: "auto", min_fabric_gbps: "", preset: "" };

  // Form state -> {body} or {errors: {field: message}}. TB are decimal (1 TB = 1,000 GB), as the API takes them.
  function buildBody(st) {
    if (st.preset) return { body: { preset: st.preset } };
    const errors = {}, body = {};
    if (!st.gpu) errors.gpu = "Pick a GPU";
    else body.gpu = st.gpu;
    const count = Number(st.count);
    if (!/^\d+$/.test(String(st.count || "")) || count < 1 || count > 1024) errors.count = "A whole number of GPUs, 1 to 1,024";
    else body.count = count;
    const hours = Number(st.hours);
    if (st.hours === "" || st.hours == null || !isFinite(hours) || hours < 1 / 60 || hours > 8760) errors.hours = "Hours between 1 minute (0.017) and 8,760";
    else body.hours = hours;
    for (const k of ["egress_tb", "storage_tb"]) {
      const v = st[k] === "" || st[k] == null ? 0 : Number(st[k]);
      if (!isFinite(v) || v < 0 || v > 10000) errors[k] = "Terabytes, 0 to 10,000";
      else body[k] = v;
    }
    body.storage_class = st.storage_class || "auto";
    if (st.min_fabric_gbps) body.min_fabric_gbps = Number(st.min_fabric_gbps);
    return Object.keys(errors).length ? { errors } : { body };
  }

  const money = v => (v == null || !isFinite(v) ? null : "$" + Number(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
  // "≥ $3,155.00" for a lower bound, "$5,011.69" otherwise, null when there is no total at all.
  function totalText(r) {
    const m = money(r.total);
    return m == null ? null : (r.total_is_lower_bound ? "≥ " + m : m);
  }
  function extraText(r) {
    const p = r.extras_vs_compute_pct;
    if (p == null || !isFinite(p)) return null;
    return (r.total_is_lower_bound ? "≥ +" : "+") + p.toFixed(1) + "%";
  }
  function unknownText(r) {
    const u = (r.unknown || []).map(k => UNKNOWN_LABEL[k] || k);
    return u.length ? "Not published, not counted: " + u.join(", ") : null;
  }
  // A component cell: {text, tone, title}. Unknown is a word, never a $0.
  function partView(r, key) {
    const p = (r.components || {})[key];
    if (!p) return { text: "-", tone: "dim", title: key === "storage" ? "no storage in this job" : "no egress in this job" };
    if (p.known === false || p.amount == null) {
      return { text: "unknown", tone: "warn", title: (key === "storage" ? "No checked storage price for this provider" : "No checked egress price for this provider")
        + ": left out of the total, which is therefore a lower bound" };
    }
    const bits = [p.product, p.class ? p.class.replace(/_/g, " ") : null, p.origin === "manual" ? `hand table, checked ${p.as_of || ""}`.trim() : p.origin === "api" ? "provider price API" : p.origin === "provider" ? "provider's own data" : null,
      p.included_gb ? `${Number(p.included_gb).toLocaleString("en-US")} GB included with the plan` : null,
      p.billed_when_stopped ? "billed while stopped too" : null].filter(Boolean);
    return { text: money(p.amount), tone: p.amount === 0 ? "good" : "", title: bits.join(" · ") };
  }
  function computeTitle(r) {
    const c = (r.components || {}).compute || {}, s = r.specs || {};
    const inc = s.billing_increment_s;
    const incText = inc == null ? "billing increment not published (per-second assumed)" : inc === 1 ? "billed per second" : inc === 60 ? "billed per minute" : inc === 3600 ? "billed per hour" : `billed in ${inc / 60}-minute steps`;
    const min = s.billing_minimum_s && s.billing_minimum_s > 60 ? `, minimum ${s.billing_minimum_s >= 3600 ? s.billing_minimum_s / 3600 + " h" : s.billing_minimum_s / 60 + " min"}` : "";
    return `${c.billed_hours != null ? c.billed_hours + " h billed" : ""} at ${money(c.price_per_instance_hour) || "?"}/h · ${incText}${min}`;
  }
  // Badges for the spec fields that make two listings different products. [{text, tone, title}]
  function badges(s, gpuCount) {
    s = s || {};
    const out = [];
    if (s.node_fabric) {
      const g = s.node_fabric_gbps;
      out.push({ text: (FABRIC_NAME[s.node_fabric] || s.node_fabric) + (g ? " " + (g >= 1000 ? (g / 1000).toLocaleString("en-US") + " Tbps" : g + " Gbps") : ""),
        tone: s.multi_node ? "good" : "", title: (s.multi_node ? "Node-to-node fabric for multi-node training" : "Node network; not sold as a multi-node training fabric") + (s.fabric_notes ? ": " + s.fabric_notes : "") });
    } else if ((gpuCount || 0) > 1) {   // one GPU: a node fabric is beside the point
      out.push({ text: "fabric n/a", tone: "dim", title: "The node-to-node network for this machine is not published (or not in OpenGrid's table)" });
    }
    if (s.on_demand_deployable === false) out.push({ text: "contract only", tone: "bad", title: "Not deployable on demand" });
    if (s.gpu_link === "pcie") out.push({ text: "PCIe only", tone: "warn", title: "GPUs talk over PCIe: no NVLink between them" });
    if (s.billing_minimum_s && s.billing_minimum_s >= 3600) out.push({ text: "1 h min", tone: "warn", title: "Billed for at least an hour" });
    return out;
  }
  function presetLabel(p) {
    return p ? `${p.preset}: ${p.label}` : "";
  }
  // 0.1666 h -> "10 min", 72 -> "72 h", 1.5 -> "1.5 h"
  function hoursText(hrs) {
    if (hrs == null || !isFinite(hrs)) return "?";
    if (hrs < 1) return Math.round(hrs * 60) + " min";
    return (Math.round(hrs * 100) / 100).toLocaleString("en-US") + " h";
  }

  OG.trueCost = { RULE, EMPTY, PRESETS, STORAGE, FABRIC, DEFAULTS, buildBody, money, totalText, extraText, unknownText, partView, computeTitle, badges, presetLabel, hoursText };
  if (typeof OG.page !== "function" || typeof document === "undefined") return;   // node tests: pure part only

  /* =============================== page =============================== */
  const cell = v => h("span", { class: "mono " + (v.tone === "warn" ? "tc-unk" : v.tone === "good" ? "up" : v.tone === "dim" ? "dim" : ""), title: v.title || null }, v.text);
  const badgeRow = (s, n) => h("span", { class: "tc-badges" }, badges(s, n).map(b => b.tone === "dim" ? h("span", { class: "dim", title: b.title }, b.text) : OG.badge(b.text, b.tone, b.title)));

  function costTable(rows, ranked) {
    return OG.table({ rows, csv: ranked ? "true-cost" : "true-cost-unranked", autoHide: false, sort: { key: ranked ? "rank" : "provider", dir: "asc" }, columns: [
      ranked ? { key: "rank", label: "#", num: true } : { key: "unranked_reason", label: "Why not ranked", cls: "dim" },
      { key: "provider", label: "Provider", fmt: v => OG.providerLink(v) },
      { key: "sku", label: "Machine", cls: "mono dim", ell: 220, fmt: (v, r) => [v || r.listing_id, r.region ? h("span", { class: "dimmer" }, " · " + r.region) : null] },
      { key: "compute", label: "Compute", num: true, value: r => r.components.compute.amount,
        fmt: (v, r) => h("span", { class: "mono", title: computeTitle(r) }, money(v) || "unknown") },
      { key: "egress", label: "Egress", num: true, value: r => (r.components.egress_internet || {}).amount, fmt: (v, r) => cell(partView(r, "egress_internet")) },
      { key: "storage", label: "Storage", num: true, value: r => (r.components.storage || {}).amount, fmt: (v, r) => cell(partView(r, "storage")) },
      { key: "total", label: "Total", num: true, title: "Compute + egress + storage. ≥ means a component is unknown and left out",
        fmt: (v, r) => h("span", { class: "tc-total", title: unknownText(r) || "every component this job uses is priced" },
          h("b", { class: "mono" }, totalText(r) || "unknown"), r.total_is_lower_bound ? h("span", {}, " ", OG.badge("lower bound", "warn", unknownText(r))) : null) },
      { key: "extras_vs_compute_pct", label: "Above compute", num: true, title: "How much egress and storage add to the compute cost", fmt: (v, r) => h("span", { class: "mono" }, extraText(r) || "-") },
      { key: "effective_per_gpu_hour", label: "$/GPU-hr", num: true, title: "Total ÷ (GPUs × hours)", fmt: (v, r) => h("span", { class: "mono", title: r.total_is_lower_bound ? "at least" : null }, v == null ? "-" : (r.total_is_lower_bound ? "≥ " : "") + "$" + v.toFixed(2)) },
      { key: "specs", label: "Network · terms", sort: false, fmt: (v, r) => badgeRow(v, r.gpu_count) },
    ] });
  }

  OG.page("/cost", {
    title: "True cost",
    async mount(el, params, query, ctx) {
      const st = {};
      for (const k of Object.keys(DEFAULTS)) st[k] = OG.qs.get(k, DEFAULTS[k]);
      const form = h("form", { class: "tc-form", onsubmit: ev => { ev.preventDefault(); run(); } });
      const out = h("div", {}), meta = h("div", {});
      el.append(OG.head("True cost", "What a job costs per provider once egress, storage and billing increments are added to the hourly price. ",
        OG.kindBadge("estimated")),
      h("div", { class: "access-note k-public" }, h("b", {}, "Estimate"), h("span", {}, " " + RULE)),
      form, out, meta,
      h("p", { class: "note" }, h("a", { class: "lnk", href: "/methodology/full-cost" }, "How the true cost is computed, and where each price comes from →")));

      let presets = [], gpus = [];
      const [pr, gl] = await Promise.all([ctx.api("/v1/cost/presets").catch(() => null), OG.data.gpus().catch(() => [])]);
      presets = Array.isArray(pr) ? pr : [];
      gpus = (gl || []).filter(g => g.live).sort((a, b) => b.weight - a.weight || a.name.localeCompare(b.name));
      if (st.gpu && !gpus.some(g => g.name === st.gpu)) gpus.unshift({ name: st.gpu, short: OG.shortGpu(st.gpu) });

      const errs = {};
      const err = k => h("em", { class: "tc-err", "data-err": k });
      const field = (k, label, input) => h("label", { class: "flt" }, h("span", {}, label), input, err(k));
      const num = (k, attrs) => h("input", Object.assign({ class: "field", type: "number", value: st[k], inputmode: "decimal", "aria-label": k,
        oninput: ev => { st[k] = ev.target.value.trim(); } }, attrs));
      function drawForm() {
        const custom = !st.preset;
        form.replaceChildren(
          h("div", { class: "bar" },
            h("span", { class: "flt" }, h("span", {}, "Job"), OG.seg([["", "Custom", "Set every input yourself"], ...presets.map(p => [p.preset, p.preset, p.label])],
              st.preset, v => { st.preset = v; drawForm(); run(); })),
            st.preset ? h("span", { class: "dim" }, presetLabel(presets.find(p => p.preset === st.preset))) : null),
          custom ? h("div", { class: "bar" },
            field("gpu", "GPU", h("select", { class: "field", "aria-label": "GPU", onchange: ev => { st.gpu = ev.target.value; } },
              gpus.map(g => h("option", { value: g.name, selected: g.name === st.gpu ? "" : null }, g.short || g.name)))),
            field("count", "GPUs", num("count", { min: 1, max: 1024, step: 1, style: "width:72px" })),
            field("hours", "Hours", num("hours", { min: 0.017, step: "any", style: "width:84px" })),
            field("egress_tb", "Egress TB", num("egress_tb", { min: 0, step: "any", style: "width:84px", title: "Data sent out to the internet over the job, decimal TB (1 TB = 1,000 GB)" })),
            field("storage_tb", "Stored TB", num("storage_tb", { min: 0, step: "any", style: "width:84px", title: "Data kept on storage for the length of the job, decimal TB" })),
            field("storage_class", "Storage", h("select", { class: "field", "aria-label": "storage class", onchange: ev => { st.storage_class = ev.target.value; } },
              STORAGE.map(([v, l]) => h("option", { value: v, selected: v === st.storage_class ? "" : null }, l)))),
            field("min_fabric_gbps", "Fabric", h("select", { class: "field", "aria-label": "minimum node fabric", onchange: ev => { st.min_fabric_gbps = ev.target.value; } },
              FABRIC.map(([v, l]) => h("option", { value: v, selected: v === st.min_fabric_gbps ? "" : null }, l)))),
            h("button", { class: "btn pri", type: "submit" }, "Estimate")) : null);
        for (const [k, m] of Object.entries(errs)) { const e = form.querySelector(`[data-err="${k}"]`); if (e) e.textContent = m; }
      }

      async function run() {
        for (const k of Object.keys(errs)) delete errs[k];
        const b = buildBody(st);
        OG.qs.set(Object.fromEntries(Object.keys(DEFAULTS).map(k => [k, st[k] === DEFAULTS[k] ? null : st[k] || null])));
        if (b.errors) { Object.assign(errs, b.errors); drawForm(); out.replaceChildren(); return; }
        drawForm();
        out.replaceChildren(OG.loading("Pricing the job on every provider…"));
        let res;
        try { res = await ctx.api("/v1/cost/estimate", { method: "POST", body: b.body, full: true, slot: "true-cost" }); }
        catch (e) { if (!e.stale) out.replaceChildren(OG.error(e, run)); meta.replaceChildren(); return; }
        const m = res.meta || {}, rows = res.data || [], unranked = m.unranked || [];
        OG.status.asOf(m.as_of);
        const job = m.job || {};
        const jobLine = `${job.count}× ${OG.shortGpu(job.gpu || "")} for ${hoursText(job.hours)} · ${(job.egress_gb / 1000).toLocaleString("en-US")} TB out · ${(job.storage_gb / 1000).toLocaleString("en-US")} TB stored`;
        if (!rows.length && !unranked.length) { out.replaceChildren(OG.empty(EMPTY(OG.shortGpu(job.gpu || st.gpu)))); meta.replaceChildren(); return; }
        out.replaceChildren(
          h("p", { class: "note" }, h("b", {}, jobLine), m.lower_bound ? h("span", { class: "dim" }, ` · ${m.lower_bound} of ${rows.length} totals are lower bounds (a component is not published); they rank after every complete total`) : null),
          rows.length ? costTable(rows, true) : OG.empty("Every matching listing is below the fabric requirement or not deployable on demand: see below."),
          unranked.length ? OG.section("Shown, not ranked", h("p", { class: "note" }, "These listings match the GPU and size but miss a requirement of the job."), costTable(unranked, false)) : null);
        meta.replaceChildren(OG.section("Notes", h("ul", { class: "note" }, (m.notes || []).map(n => h("li", { class: "dim" }, n)))));
      }
      drawForm();
      await run();
    },
  });
})();
