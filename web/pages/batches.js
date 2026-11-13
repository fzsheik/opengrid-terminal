/* Batches (jobs/batches.py, api/batches.py; methodology/batches.md).
   /batches       your batches (GET /v1/batches) and "Run as a batch": a sweep form -> POST /v1/batches/preview (the
                  placement plan: per-shard provider, price, estimate, the total vs all on one cloud) -> POST /v1/batches
                  (one Idempotency-Key per intent; a real batch is confirmed with its budget first). Sandbox by default.
   /batch/:id     one batch (GET /v1/batches/{id}, polled while not finished): status counts, budget (committed /
                  spent / remaining), the ONE batch approval (admins: POST /v1/batches/{id}/approve with every waiting
                  shard's exact quote), per-provider breakdown, per-shard rows linking to their jobs, the best result
                  for the chosen metric, events, Cancel (POST /v1/batches/{id}/cancel, confirmed).
   A SIMULATED batch carries the same banner as a sandbox job. Every shard is an ordinary job (/jobs/<id>). */
(() => {
  const OG = typeof window !== "undefined" ? window.OG : globalThis.OG;
  if (!OG) return;
  const Bt = OG.batches = OG.batches || {};

  /* ---------------- pure parts ---------------- */
  const STATE_TONE = { queued: "", submitting: "", pending_approval: "warn", needs_reapproval: "warn", running: "up",
    relaunching: "warn", succeeded: "up", failed: "bad", cancelled: "", skipped: "", refused: "bad" };
  const STATE_TEXT = { queued: "queued", submitting: "submitting", pending_approval: "waiting for approval",
    needs_reapproval: "needs re-approval", running: "running", relaunching: "relaunching", succeeded: "succeeded",
    failed: "failed", cancelled: "cancelled", skipped: "skipped", refused: "refused" };
  Bt.STATE_TEXT = STATE_TEXT;
  // "1, 2, 3" -> [1, 2, 3]; empty -> null; a non-number -> throws with the field name
  Bt.numList = (s, field) => {
    const parts = String(s || "").split(/[\s,]+/).map(x => x.trim()).filter(Boolean);
    if (!parts.length) return null;
    return parts.map(x => { const v = Number(x); if (!Number.isFinite(v)) throw new Error(field + ": " + x + " is not a number"); return v; });
  };
  // The POST /v1/batches body from the form's values (validated again by the server).
  Bt.bodyFrom = f => {
    const grid = {};
    for (const [k, label] of [["learning_rate", "learning rates"], ["batch_size", "batch sizes"], ["epochs", "epochs"]]) {
      const v = Bt.numList(f[k], label);
      if (v) grid[k] = v;
    }
    if (!Object.keys(grid).length) throw new Error("give at least one list of values to sweep");
    return {
      name: (f.name || "mnist-sweep").trim(), job_type: "training", template: "pytorch-mnist",
      config: { epochs: 2, batch_size: 128, learning_rate: 0.001, seed: 42 },
      sweep: { grid },
      resources: { gpu: "auto", gpu_count: 1, region: f.region || "US" },
      routing: { strategy: f.strategy || "CHEAPEST" },
      limits: { max_price_per_gpu_hour_usd: Number(f.max_price || 3), max_runtime_minutes: Number(f.runtime || 30) },
      budget: { max_total_usd: Number(f.budget || 20) },
      placement: { max_share_per_provider: Number(f.share || 0.5) },
      concurrency: { max_running: Number(f.max_running || 4) },
      policy: { on_failure: f.on_failure || "continue", max_attempts: Number(f.max_attempts || 1) },
      metric: { name: "test_accuracy", goal: "max" },
      sandbox: f.sandbox !== false,
    };
  };
  Bt.countsText = counts => Object.entries(counts || {}).filter(([, n]) => n).map(([k, n]) => `${n} ${STATE_TEXT[k] || k}`).join(" · ") || "no shards";
  Bt.isDone = b => !!(b && b.finished);

  if (typeof document === "undefined" || typeof OG.page !== "function") return;
  const { h, fmt } = OG;
  const usd = v => v == null ? "-" : "$" + Number(v).toFixed(2);
  const badge = st => OG.badge(STATE_TEXT[st] || st, STATE_TONE[st] || "");
  const kv = rows => h("table", { class: "dep-kv" }, rows.filter(Boolean).map(([k, v]) => h("tr", {}, h("th", {}, k), h("td", {}, v == null || v === "" ? h("span", { class: "dim" }, "-") : v))));
  const errBox = (e, what) => h("div", { class: "og-alarm t-warn" }, h("b", {}, what + " failed"),
    h("span", {}, (e && e.body && e.body.detail && (e.body.detail.message || e.body.detail.code)) || (e && e.message) || "network error"));
  const simBanner = () => h("div", { class: "jb-simban", role: "note" }, h("b", {}, "SIMULATED"), h("span", {}, "Every shard is a sandbox job: no GPU was used and nothing was charged. Numbers are synthetic."));

  function planBlock(pv) {
    const p = pv.plan || {}, t = p.totals || {}, alt = p.alternatives || {};
    const tbl = OG.table({
      columns: [
        { key: "shard_index", label: "Shard", num: true },
        { key: "config", label: "Settings", value: r => JSON.stringify(r.config || {}), fmt: (v, r) => h("span", { class: "mono" }, Object.entries(r.config || {}).map(([k, x]) => `${k}=${x}`).join(" ")) },
        { key: "provider", label: "Provider", fmt: (v, r) => v ? OG.providerLink(v) : h("span", { class: "dim" }, r.reason || "not placed") },
        { key: "price_per_gpu_hour", label: "$/GPU-h", num: true, fmt: v => v == null ? "-" : "$" + Number(v).toFixed(2) },
        { key: "est_max_total_usd", label: "Est. max", num: true, fmt: v => usd(v) },
        { key: "reservation_usd", label: "Reserved", num: true, fmt: v => usd(v) },
      ],
      rows: p.shards || [], rowKey: r => String(r.shard_index), compact: true, csv: "opengrid-batch-plan.csv",
    });
    tbl.update(p.shards || []);
    const sub = pv.submission || {};
    return h("section", { class: "sec" },
      h("h2", { class: "sec-h" }, "Placement plan"),
      kv([["Shards", `${p.placed} of ${p.shards_total} placed`], ["Per provider", `at most ${(p.cap || {}).max_shards_per_provider}`],
        ["Estimated max", usd(t.est_max_total_usd)], ["Reserved", `${usd(t.reservation_total_usd)} of ${usd(t.budget_usd)} budget`],
        ["Headroom for relaunches", usd(t.relaunch_headroom_usd)],
        alt.all_on_one ? ["All on " + OG.providerName(alt.all_on_one.provider), usd(alt.all_on_one.est_max_total_usd) + (alt.savings_vs_all_on_one_usd == null ? "" :
          alt.savings_vs_all_on_one_usd >= 0 ? ` (the spread plan saves ${usd(alt.savings_vs_all_on_one_usd)})` : ` (spreading costs ${usd(-alt.savings_vs_all_on_one_usd)} more)`)] : null]),
      (p.warnings || []).map(w => h("p", { class: "note t-warn" }, w)),
      sub.can_submit === false ? h("p", { class: "note t-warn" }, h("b", {}, "Cannot submit: "), (sub.refusal || {}).message || (sub.refusal || {}).code) : null,
      tbl, h("p", { class: "note" }, alt.note || ""));
  }

  /* =============================== /batches =============================== */
  OG.page("/batches", {
    title: "Batches",
    nav: "batches",
    async mount(el, params, query, ctx) {
      if (OG.computeStyles) OG.computeStyles();
      const root = h("div", { class: "pg-jobs pg-dep pg-route" });
      el.append(root);
      const me = await OG.me();
      if (!ctx.alive()) return;
      if (!me) {
        root.replaceChildren(OG.head("Batches", "Many jobs at once, spread across GPU clouds under one budget."),
          h("section", { class: "cx-access" }, OG.accessNote("account", "Batches are private to their account.")));
        return;
      }
      const listBox = h("div", {}, OG.loading("Loading your batches…"));
      const formBox = h("div"), planBox = h("div"), flash = h("div", { class: "dep-flash" });
      const newSec = h("details", { class: "cx-adv", open: query.new === "1" ? true : null },
        h("summary", {}, h("b", {}, "Run as a batch"), h("span", { class: "dim" }, " (a sweep of the MNIST template: each combination is one job; free simulation by default)")),
        formBox, flash, planBox);
      root.replaceChildren(OG.head("Batches", "Many independent jobs at once, placed on the cheapest suitable clouds under one budget, one approval and one status.",
        h("a", { class: "btn sm", href: "/jobs" }, "My Jobs"), h("button", { class: "btn sm", type: "button", onclick: () => load() }, "Refresh")),
        newSec, listBox);

      const f = { name: "mnist-sweep", learning_rate: "0.001, 0.003, 0.01", batch_size: "64, 128", epochs: "", budget: "20",
        max_running: "4", share: "0.5", on_failure: "continue", max_attempts: "1", max_price: "3", runtime: "30", region: "US",
        strategy: "CHEAPEST", sandbox: true };
      const input = (k, label, attrs) => h("label", { class: "fld" }, h("span", {}, label),
        h("input", Object.assign({ class: "field", value: f[k], oninput: e => { f[k] = e.target.value; } }, attrs || {})));
      const sel = (k, label, opts) => h("label", { class: "fld" }, h("span", {}, label),
        h("select", { class: "field", onchange: e => { f[k] = e.target.value; } }, opts.map(([v, t]) => h("option", { value: v, selected: f[k] === v ? true : null }, t))));
      let intent = null, lastBody = null;
      formBox.replaceChildren(h("div", { class: "rt-form" },
        input("name", "Batch name"), input("learning_rate", "Learning rates"), input("batch_size", "Batch sizes"),
        input("epochs", "Epochs (optional)"), input("budget", "Budget, all shards (USD)", { type: "number", min: "1", step: "1" }),
        input("max_running", "Run at once", { type: "number", min: "1", max: "64" }),
        input("share", "Max share per provider (0-1)", { type: "number", min: "0.05", max: "1", step: "0.05" }),
        sel("on_failure", "If a shard fails", [["continue", "keep going"], ["stop_on_first_failure", "stop the batch"]]),
        sel("max_attempts", "Attempts per shard", [["1", "1 (no relaunch)"], ["2", "2"], ["3", "3"]]),
        sel("strategy", "Placement", [["CHEAPEST", "cheapest"], ["BALANCED", "balanced"], ["MOST_STABLE", "most stable"]]),
        h("label", { class: "fld chk" }, h("input", { type: "checkbox", checked: true, onchange: e => { f.sandbox = e.target.checked; } }), h("span", {}, "Free simulation (no GPU, no charge)"))),
        h("div", { class: "cx-acts" }, h("button", { class: "btn", type: "button", onclick: () => preview() }, "Preview plan"),
          h("button", { class: "btn pri", type: "button", onclick: () => submit() }, "Run batch")));

      async function preview() {
        let body;
        try { body = Bt.bodyFrom(f); } catch (e) { flash.replaceChildren(errBox(e, "Check the form")); return; }
        flash.replaceChildren(OG.loading("Planning…"));
        try {
          const pv = await ctx.api("/v1/batches/preview", { method: "POST", body, slot: "batch-preview" });
          lastBody = body;
          flash.replaceChildren();
          planBox.replaceChildren(planBlock(pv));
        } catch (e) { if (!e.stale) flash.replaceChildren(errBox(e, "Preview")); }
      }
      async function submit() {
        let body;
        try { body = Bt.bodyFrom(f); } catch (e) { flash.replaceChildren(errBox(e, "Check the form")); return; }
        if (!body.sandbox) {
          const ok = await OG.dialog({ title: "Run a real batch?", confirm: "Submit for approval", cancel: "Not now",
            body: h("div", {}, h("p", {}, `Up to ${usd(body.budget.max_total_usd)} across every shard and every relaunch. Nothing launches until an OpenGrid admin approves the batch.`),
              h("p", { class: "dim" }, "Safe to retry: a second click after a network error can't submit twice.")) });
          if (!ok) return;
        }
        intent = OG.intentFor(intent, "batch:submit", body);
        flash.replaceChildren(OG.loading("Submitting…"));
        try {
          const r = await OG.api.intent(intent, "/v1/batches", { method: "POST", body });
          OG.go("/batch/" + encodeURIComponent(r.batch_id));
        } catch (e) { flash.replaceChildren(errBox(e, "Submit")); if (lastBody !== body) planBox.replaceChildren(); }
      }

      async function load() {
        let rows;
        try { rows = await ctx.api("/v1/batches", { nocache: true }); }
        catch (e) {
          listBox.replaceChildren(e && (e.status === 401 || e.status === 403) ? h("section", { class: "cx-access" }, OG.accessNote("account")) : errBox(e, "Loading batches"));
          return;
        }
        rows = Array.isArray(rows) ? rows : [];
        if (!rows.length) { listBox.replaceChildren(h("section", { class: "cx-empty" }, h("h2", {}, "No batches yet"), h("p", {}, "A batch runs many jobs (for example a learning-rate sweep) at once. Open Run as a batch above: the free simulation shows the plan and the flow without renting a GPU."))); return; }
        const tbl = OG.table({
          columns: [
            { key: "name", label: "Name", fmt: v => h("span", { class: "jb-nm" }, v) },
            { key: "batch_id", label: "Batch", cls: "mono dim" },
            { key: "status", label: "State", value: r => r.finished ? r.outcome : r.status, fmt: (v, r) => OG.badge(v, r.outcome === "succeeded" || r.outcome === "simulated" ? "up" : r.outcome === "failed" ? "bad" : "") },
            { key: "shards_total", label: "Shards", num: true },
            { key: "counts", label: "Progress", value: r => Bt.countsText(r.counts), fmt: v => h("span", { class: "dim" }, v) },
            { key: "spent", label: "Committed / budget", num: true, value: r => (r.budget || {}).committed_usd, fmt: (v, r) => r.sandbox ? "free" : `${usd(v)} / ${usd((r.budget || {}).max_total_usd)}` },
            { key: "sandbox", label: "Run", value: r => r.sandbox ? 1 : 0, fmt: (v, r) => OG.jobs && OG.jobs.sandboxBadge ? OG.jobs.sandboxBadge(r.sandbox) : (r.sandbox ? "SIMULATED" : "real") },
            { key: "created_at", label: "Created", num: true, fmt: v => fmt.dateTime(v) },
          ],
          rows, sort: { key: "created_at", dir: "desc" }, rowKey: r => r.batch_id, rowHref: r => "/batch/" + encodeURIComponent(r.batch_id), compact: true, csv: "opengrid-batches.csv",
        });
        tbl.update(rows);
        listBox.replaceChildren(tbl);
      }
      load();
      ctx.every(15000, () => load());
    },
  });

  /* =============================== /batch/:id =============================== */
  OG.page("/batch/:id", {
    title: p => "Batch " + p.id,
    nav: "batches",
    async mount(el, params, query, ctx) {
      if (OG.computeStyles) OG.computeStyles();
      const id = params.id, enc = encodeURIComponent(id);
      const root = h("div", { class: "pg-jobs pg-dep pg-depd" });
      el.append(root);
      root.append(OG.loading("Loading batch…"));
      const me = await OG.me();
      if (!ctx.alive()) return;
      const admin = OG.isAdmin(me);
      const flash = h("div", { class: "dep-flash" });
      let b = null, I = { approve: null, cancel: null };

      async function load() {
        try { b = await ctx.api("/v1/batches/" + enc, { nocache: true }); }
        catch (e) {
          if (!b) root.replaceChildren(h("a", { class: "lnk jb-back", href: "/batches" }, "← Batches"), OG.head("Batch " + id, null),
            h("section", { class: "cx-access" }, e && e.status === 401 ? OG.accessNote("account", "Batches are private to their account.") : h("h2", {}, e && e.status === 404 ? "Batch not found" : "Could not load the batch")));
          return;
        }
        draw();
      }
      function draw() {
        const bu = b.budget || {}, ap = b.approval || {};
        const shardsTbl = OG.table({
          columns: [
            { key: "shard_index", label: "Shard", num: true },
            { key: "config", label: "Settings", value: r => JSON.stringify(r.config || {}), fmt: (v, r) => h("span", { class: "mono" }, Object.entries(r.config || {}).map(([k, x]) => `${k}=${x}`).join(" ") || "-") },
            { key: "status", label: "State", fmt: v => badge(v) },
            { key: "provider", label: "Provider", fmt: (v, r) => v ? h("span", {}, OG.providerLink(v), r.provider_planned ? h("span", { class: "dim" }, " (planned)") : null) : h("span", { class: "dim" }, "-") },
            { key: "attempts", label: "Attempts", num: true },
            { key: "metric", label: (b.metric || {}).name || "metric", num: true, value: r => r.metric ? r.metric.value : null, fmt: (v, r) => v == null ? "-" : Number(v).toFixed(4) + (r.metric.simulated ? " (sim)" : "") },
            { key: "job_id", label: "Job", fmt: v => v ? h("a", { class: "lnk mono", href: "/jobs/" + encodeURIComponent(v) }, v) : h("span", { class: "dim" }, "not submitted") },
            { key: "reason", label: "Note", fmt: v => v ? h("span", { class: "dim" }, String(v).slice(0, 140)) : "" },
          ],
          rows: b.shards || [], rowKey: r => String(r.shard_index), compact: true, csv: "opengrid-batch-shards.csv",
        });
        shardsTbl.update(b.shards || []);
        const provRows = (b.providers || []).map(p => h("tr", {}, h("td", {}, OG.providerLink(p.provider)), h("td", { class: "num" }, String(p.shards)),
          h("td", { class: "dim" }, Bt.countsText(p.by_state)), h("td", { class: "num" }, usd(p.committed_usd)), h("td", { class: "num" }, usd(p.spent_usd))));
        const canCancel = !b.finished && !b.cancel_requested_at;
        root.replaceChildren(
          h("a", { class: "lnk jb-back", href: "/batches" }, "← Batches"),
          OG.head(h("span", { class: "dep-title" }, b.name), h("span", {}, `${b.shards_total} shards · ${b.template}`),
            canCancel ? h("button", { class: "btn w4-danger", type: "button", onclick: () => cancel() }, "Cancel batch") : null),
          h("div", { class: "jb-bigline" }, OG.badge(b.finished ? b.outcome : b.status, b.outcome === "failed" ? "bad" : b.finished ? "up" : ""),
            h("span", {}, Bt.countsText(b.counts)), b.finished ? null : h("span", { class: "dim" }, "updates every few seconds"),
            h("button", { class: "btn sm", type: "button", onclick: () => load() }, "Refresh")),
          b.sandbox ? simBanner() : null,
          flash,
          ap.required ? h("section", { class: "sec jb-appr" }, h("h2", { class: "sec-h" }, "Waiting for the batch approval"),
            h("p", {}, `${ap.waiting.length} shard(s) wait. One approval covers every shard's exact quote; each shard is still re-checked live before it launches, and a price that moved needs approving again.`),
            h("ul", {}, ap.waiting.map(w => h("li", {}, `Shard ${w.shard_index} on ${OG.providerName(w.provider)} at $${Number(w.quote_price_per_gpu_hour || 0).toFixed(2)}/GPU-h`, w.needs_reapproval ? h("b", { class: "warn-t" }, " · needs re-approval") : null))),
            admin ? h("button", { class: "btn pri", type: "button", onclick: () => approve() }, "Approve batch") : h("p", { class: "dim" }, "An OpenGrid admin approves it; nothing is billed until then.")) : null,
          h("section", { class: "sec" }, h("h2", { class: "sec-h" }, "Budget"),
            kv([["Budget (all shards, all relaunches)", usd(bu.max_total_usd)], ["Committed (upper bound)", usd(bu.committed_usd)], ["Spent so far", usd(bu.spent_usd)], ["Remaining", usd(bu.remaining_usd)],
              ["Running at once", `${(b.concurrency || {}).busy} of ${(b.concurrency || {}).max_running}`], ["If a shard fails", (b.policy || {}).on_failure === "stop_on_first_failure" ? "stop the batch" : "keep going"],
              (b.policy || {}).stop_reason ? ["Stopped because", b.policy.stop_reason] : null]),
            h("p", { class: "note" }, bu.note || "")),
          b.best ? h("section", { class: "sec" }, h("h2", { class: "sec-h" }, "Best result"),
            h("p", {}, `Shard ${b.best.shard_index}: ${b.best.metric} ${Number(b.best.value).toFixed(4)} with `, h("span", { class: "mono" }, Object.entries(b.best.config || {}).map(([k, x]) => `${k}=${x}`).join(" ")), " · ",
              h("a", { class: "lnk", href: "/jobs/" + encodeURIComponent(b.best.job_id) }, "open the job")), b.best.simulated ? h("p", { class: "note" }, b.best.note) : null) : null,
          provRows.length ? h("section", { class: "sec" }, h("h2", { class: "sec-h" }, "Providers"),
            h("table", { class: "tbl compact" }, h("thead", {}, h("tr", {}, ["Provider", "Shards", "States", "Committed", "Spent"].map(x => h("th", {}, x)))), h("tbody", {}, provRows))) : null,
          h("section", { class: "sec" }, h("h2", { class: "sec-h" }, "Shards"), shardsTbl),
          h("details", { class: "jb-adv" }, h("summary", {}, h("b", {}, "Events"), h("span", { class: "dim" }, ` (${(b.events || []).length})`)),
            h("ul", { class: "dim" }, (b.events || []).slice(-200).map(e => h("li", {}, fmt.dateTime(e.at), " · ", e.kind, " · ", e.reason || "")))),
        );
      }
      async function approve() {
        const quotes = (b.approval || {}).body ? b.approval.body.quotes : {};
        const ok = await OG.dialog({ title: "Approve the batch?", confirm: "Approve", cancel: "Not now",
          body: h("p", {}, `Approves ${Object.keys(quotes).length} waiting shard(s) at their exact quotes, and the planned prices of the queued ones (within the quote tolerance). Budget ${usd((b.budget || {}).max_total_usd)}.`) });
        if (!ok) return;
        I.approve = OG.intentFor(I.approve, "batch:approve:" + id, { quotes });
        flash.replaceChildren(OG.loading("Approving…"));
        try { await OG.api.intent(I.approve, `/v1/batches/${enc}/approve`, { method: "POST", body: { quotes } }); flash.replaceChildren(h("div", { class: "og-ok" }, h("b", {}, "Batch approved"))); await load(); }
        catch (e) { flash.replaceChildren(errBox(e, "Approve")); }
      }
      async function cancel() {
        const ok = await OG.dialog({ title: "Cancel the batch?", danger: true, confirm: "Cancel batch", cancel: "Keep it",
          body: h("p", {}, "Queued shards are cancelled; submitted ones are cancelled like any job (a running GPU is terminated). Finished shards keep their results.") });
        if (!ok) return;
        I.cancel = OG.intentFor(I.cancel, "batch:cancel:" + id, {});
        try { await OG.api.intent(I.cancel, `/v1/batches/${enc}/cancel`, { method: "POST", body: {} }); flash.replaceChildren(h("div", { class: "og-ok" }, h("b", {}, "Cancel requested"))); await load(); }
        catch (e) { flash.replaceChildren(errBox(e, "Cancel")); }
      }
      await load();
      ctx.every(5000, () => { if (b && !b.finished && !document.hidden) load(); });
    },
  });
})();
