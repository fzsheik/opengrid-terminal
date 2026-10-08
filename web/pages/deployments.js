/* /deployments — every deployment this principal can see (GET /v1/deployments), uncertain ones first.
   /deployments/:id — one deployment, end to end: the state machine strip, the events timeline (actor, reason,
   evidence), provision attempts, the pinned credential source (never the secret), the four cost numbers kept
   apart (quote / expected / provider-reported / OpenGrid transaction) with quote error, effective rate and
   rounding, runtime and the auto-terminate deadline, Terminate / Stop (confirmed, one Idempotency-Key per
   intent), reconciliation evidence, the routing-quality row, the admin trace and the design-partner feedback
   form (after termination). Nothing here is inferred: missing numbers say why. */
(() => {
  const { h, fmt } = OG;
  const D = OG.dep;
  const TERMINAL = ["terminated", "rejected", "provision_failed", "provider_rejected", "quote_failed"];
  const PRE_LAUNCH = ["created", "quoted", "pending_approval", "approved", "quote_expired"];
  const dur = sec => {
    if (sec == null) return "–";
    const s = Math.max(0, Math.round(sec));
    if (s < 60) return s + "s";
    if (s < 3600) return Math.floor(s / 60) + "m " + String(s % 60).padStart(2, "0") + "s";
    if (s < 86400) return Math.floor(s / 3600) + "h " + String(Math.floor(s % 3600 / 60)).padStart(2, "0") + "m";
    return Math.floor(s / 86400) + "d " + Math.floor(s % 86400 / 3600) + "h";
  };
  /* ---- pure execution text (shared with route.js; tested in tests/test_web_exec.js) ---- */
  const p2 = n => String(n).padStart(2, "0");
  const stampOf = (dt, utc) => utc
    ? `${dt.getUTCFullYear()}-${p2(dt.getUTCMonth() + 1)}-${p2(dt.getUTCDate())} ${p2(dt.getUTCHours())}:${p2(dt.getUTCMinutes())}:${p2(dt.getUTCSeconds())}`
    : `${dt.getFullYear()}-${p2(dt.getMonth() + 1)}-${p2(dt.getDate())} ${p2(dt.getHours())}:${p2(dt.getMinutes())}:${p2(dt.getSeconds())}`;
  const CEILING_SOURCE = { request: "request", account: "account", system_default: "system default", system_hard_max: "hard max", validation_cap: "validation cap" };
  const X = {
    // "2026-10-07 09:05:00 local (UTC 2026-10-07 14:05:00)"; null when the time is unknown
    exactTime(iso) {
      if (!iso) return null;
      const dt = new Date(iso);
      if (isNaN(+dt)) return null;
      return `${stampOf(dt, false)} local (UTC ${stampOf(dt, true)})`;
    },
    ceilingSource: s => s ? (CEILING_SOURCE[s] || String(s).replace(/_/g, " ")) : "unknown",
    // "Auto-terminates: <exact> — max runtime N min, source <src>"
    autoTerminate(iso, minutes, source) {
      const t = X.exactTime(iso);
      return `Auto-terminates: ${t || "not set yet"} — max runtime ${minutes != null ? minutes + " min" : "unknown"}, source ${X.ceilingSource(source)}`;
    },
    // the backend's duration_warning, or the same check done here when the response did not carry it
    durationWarning(durationHours, minutes, backend) {
      if (backend) return backend;
      if (!durationHours || !minutes || +durationHours * 60 <= +minutes) return null;
      return `duration ${+durationHours} h is longer than the ${minutes} min runtime ceiling: the instance WILL be terminated at the deadline`;
    },
    // {past, seconds, text}: "in 1h 04m" / "PAST DEADLINE by 3m 10s"
    deadline(iso, now) {
      if (!iso) return { past: false, seconds: null, text: "no deadline" };
      const s = Math.round((+new Date(iso) - (now == null ? Date.now() : +now)) / 1000);
      return s > 0 ? { past: false, seconds: s, text: "in " + dur(s) } : { past: true, seconds: s, text: "PAST DEADLINE by " + dur(-s) };
    },
    // Who can log in, in the founder's format. o: {purpose, keyRef}. -> {lines:[{label,value,tone}], text, missingKey, needsOverride}
    sshAccess(sa, o) {
      o = o || {};
      const validation = o.purpose === "validation" || (sa && sa.operator_access === "validation_operator_key");
      const raw = sa ? String(sa.operator_access || "none") : null;
      let key, keyTone = null, missingKey = false;
      if (validation) key = "none (validation launch on OpenGrid's account)";
      else if (sa && sa.customer_key_fingerprint) key = sa.customer_key_fingerprint;
      else if (o.keyRef) key = `provider key name "${o.keyRef}" (your own BYO account; no fingerprint)`;
      else { key = "MISSING — no SSH public key on this request"; keyTone = "bad"; missingKey = true; }
      let op, opTone = "bad", needsOverride = false;
      if (raw == null) { op = "UNKNOWN (not reported)"; }
      else if (validation) op = "OpenGrid operator key (validation only)";
      else if (raw.toLowerCase() === "none") { op = "NONE"; opTone = null; }
      else if (raw.startsWith("blocked:")) { op = `${raw} — the provider may install its account-level keys; needs an admin override`; needsOverride = true; }
      else {
        const m = raw.match(/^provider_forced_account_key:override_by:(.+)$/);
        op = m ? `${raw} — provider-forced account key, override by ${m[1]}` : raw;
      }
      const lines = [{ label: "Customer supplied key", value: key, tone: keyTone }, { label: "OpenGrid operator access", value: op, tone: opTone }];
      return { lines, missingKey, needsOverride, validation, text: ["SSH access:"].concat(lines.map(l => `${l.label}: ${l.value}`)).join("\n") };
    },
    // the red banner: past the deadline while an instance may exist, or an uncertain state
    billingAlarm(d, now) {
      if (!d) return null;
      const live = OG.dep.LIVE.includes(d.status);
      const dl = X.deadline(d.terminate_deadline_at, now);
      if (live && d.terminate_deadline_at && dl.past) return { kind: "past_deadline", title: "PAST DEADLINE / POSSIBLY BILLING",
        text: `The auto-terminate deadline ${X.exactTime(d.terminate_deadline_at)} passed ${dur(-dl.seconds)} ago and the instance may still exist and bill. Terminate now and confirm in the provider console.` };
      if (d.uncertain || OG.dep.UNCERTAIN.includes(d.status) || d.status === "termination_failed") return { kind: "uncertain", title: "POSSIBLY BILLING",
        text: `State ${String(d.status).replace(/_/g, " ")}: OpenGrid cannot confirm whether the instance exists. Treat it as billing until reconciliation or the provider says otherwise.` };
      return null;
    },
  };
  OG.execText = X;
  // DOM: the SSH-access block (founder's format; red where operator access is not NONE or the key is missing)
  OG.sshAccessBlock = (sa, o) => {
    const r = X.sshAccess(sa, o);
    return h("div", { class: "x-ssh" + (r.lines.some(l => l.tone) ? " x-ssh-bad" : "") }, h("b", {}, "SSH access:"),
      r.lines.map(l => h("div", { class: "x-ssh-l" }, h("span", { class: "x-ssh-k" }, l.label + ": "), h("span", { class: "mono" + (l.tone ? " down x-ssh-red" : "") }, l.value))));
  };

  const money = v => (v == null ? null : OG.money(v));
  const kv = rows => h("table", { class: "dep-kv" }, rows.filter(Boolean).map(([k, v, title]) =>
    h("tr", { title: title || null }, h("th", {}, k), h("td", {}, v == null || v === "" ? h("span", { class: "dim" }, "–") : v))));
  function evText(o) {
    if (!o || typeof o !== "object") return o == null ? "" : String(o);
    return Object.entries(o).filter(([, v]) => v != null && v !== "").map(([k, v]) => `${k}: ${typeof v === "object" ? JSON.stringify(v).slice(0, 160) : v}`).join(" · ");
  }
  // Why each non-happy state matters, in one line (methodology/execution-safety.md)
  const STATE_NOTE = {
    provider_timeout: "The provision call timed out. An instance MAY exist and bill. Reconciliation looks it up by name (client name below) before anything is retried.",
    launch_unknown: "Ambiguous provider answer. An instance MAY exist and bill. No retry or failover until reconciliation resolves it by name.",
    orphan_suspected: "Reconciliation sees an instance OpenGrid cannot reconcile with this deployment. Check the evidence; terminate if it is still billing.",
    credentials_unavailable: "The credential pinned at launch can no longer be used. OpenGrid never switches credentials and keeps the state until it can confirm.",
    termination_failed: "The provider refused the delete: the instance is still there and billing. Retry terminate or remove it in the provider console.",
    degraded: "The provider reports an error on an instance that may still exist. Billing continues until it is terminated.",
    provision_failed: "The provider definitively created nothing (capacity / validation). Nothing bills.",
    provider_rejected: "The provider refused for an account reason (auth / quota). Nothing was created.",
    quote_expired: "The quote expired before approval. A new quote is needed.",
    rejected: "Rejected or cancelled before launch. Nothing was created.",
  };

  /* =============================== list =============================== */
  OG.page("/deployments", {
    title: "Deployments",
    mount(el, params, query, ctx) {
      if (query.id) { OG.go("/deployments/" + encodeURIComponent(query.id), { replace: true }); return; }
      const root = h("div", { class: "pg-dep" });
      el.append(root);
      const FILTERS = [["all", "All"], ["live", "Live"], ["uncertain", "Uncertain"], ["pending", "Pending approval"], ["failed", "Failed"], ["terminated", "Terminated"]];
      const st = { filter: FILTERS.some(f => f[0] === query.status) ? query.status : "all", rows: null };
      const stats = h("div"), holder = h("div", {}, OG.loading("Loading deployments…")), alarm = h("div");
      const seg = OG.seg(FILTERS, st.filter, v => { st.filter = v; OG.qs.set({ status: v === "all" ? null : v }); draw(); });
      root.append(OG.head("Deployments", "Compute OpenGrid launched or is about to launch · transaction data",
        OG.kindBadge("transaction"), seg, h("button", { class: "btn", type: "button", onclick: () => load() }, "Reload"),
        h("a", { class: "btn pri", href: "/route" }, "New route →")), alarm, stats, holder);
      const match = (r, f) => f === "all" || (f === "live" ? D.LIVE.includes(r.status) : f === "uncertain" ? D.UNCERTAIN.includes(r.status) || r.status === "termination_failed"
        : f === "pending" ? ["pending_approval", "approved", "quote_expired"].includes(r.status) : f === "failed" ? D.FAILED.includes(r.status) : r.status === f);
      const rank = r => (r.status === "termination_failed" || D.UNCERTAIN.includes(r.status) ? 0 : D.LIVE.includes(r.status) ? 1 : PRE_LAUNCH.includes(r.status) ? 2 : 3);
      const tbl = OG.table({
        columns: [
          { key: "attention", label: "", value: r => rank(r), width: "8px", fmt: (v, r) => h("i", { class: "dep-dot t-" + (D.tone(r.status) || "plain") }) },
          { key: "deployment_id", label: "Deployment", cls: "mono", fmt: v => h("span", { class: "dep-id" }, v) },
          { key: "status", label: "State", fmt: v => OG.stateBadge(v) },
          { key: "purpose", label: "Purpose", fmt: v => v === "validation" ? OG.badge("validation", "warn") : h("span", { class: "dim" }, v || "customer") },
          { key: "provider", label: "Provider", fmt: v => v ? OG.providerLink(v) : h("span", { class: "dim" }, "not placed") },
          { key: "gpu", label: "GPU", fmt: v => OG.gpuLink(v) },
          { key: "gpu_count", label: "GPUs", num: true },
          { key: "quote", label: "Quote $/GPU·h", num: true, value: r => r.prices && r.prices.quote, fmt: v => v != null ? fmt.price(v) : OG.na("no quote recorded") },
          { key: "exec", label: "Execution $/GPU·h", num: true, value: r => r.prices && r.prices.execution_price, fmt: v => v != null ? fmt.price(v) : OG.na("the provider has not reported an execution price") },
          { key: "created_at", label: "Created", num: true, fmt: v => fmt.dateTime(v) },
          { key: "uptime_seconds", label: "Runtime", num: true, fmt: v => dur(v), title: "OpenGrid-observed running time" },
          { key: "terminate_deadline_at", label: "Auto-terminate", num: true, fmt: (v, r) => v && D.LIVE.includes(r.status) ? OG.countdown(v) : h("span", { class: "dim" }, v ? fmt.dateTime(v) : "–") },
          { key: "failure_reason", label: "Failure", cls: "wrap dim", fmt: v => v || "" },
        ],
        rows: [], sort: { key: "attention", dir: "asc" }, rowKey: r => r.deployment_id, rowHref: r => "/deployments/" + encodeURIComponent(r.deployment_id),
        rowClass: r => (rank(r) === 0 ? "dep-hot" : ""), csv: "opengrid-deployments.csv", compact: true, empty: "No deployments match this filter.",
      });
      function draw() {
        const rows = st.rows;
        if (!rows) return;
        const hot = rows.filter(r => rank(r) === 0);
        alarm.replaceChildren(hot.length ? h("div", { class: "og-alarm" }, h("b", {}, `${hot.length} deployment${hot.length === 1 ? "" : "s"} in an uncertain or failed-termination state`),
          h("span", {}, " — an instance may exist and bill. Open each one; reconciliation evidence is on the detail page.")) : null);
        stats.replaceChildren(OG.stats([
          { label: "Deployments", value: fmt.num(rows.length) },
          { label: "Live", value: fmt.num(rows.filter(r => D.LIVE.includes(r.status)).length), sub: "an instance may exist" },
          { label: "Running", value: fmt.num(rows.filter(r => r.status === "running").length) },
          { label: "Uncertain", value: fmt.num(hot.length), sub: "needs evidence" },
          { label: "Pending approval", value: fmt.num(rows.filter(r => r.status === "pending_approval").length) },
          { label: "GPU-hours observed", value: rows.length ? fmt.num(rows.reduce((s, r) => s + (r.uptime_seconds || 0) * (r.gpu_count || 0) / 3600, 0), 1) : null, reason: "no deployments", kind: "transaction" },
        ]));
        if (!rows.length) {
          holder.replaceChildren(h("div", { class: "dep-empty" }, h("b", {}, "No deployments yet."),
            h("p", {}, "A deployment exists once POST /v1/route quotes a provisionable listing under SUPERVISED or LIVE execution. In PREVIEW_ONLY the route returns ", h("code", { class: "mono" }, "not_provisioned"), " and creates none."),
            h("p", {}, h("a", { class: "btn pri", href: "/route" }, "Preview a route →"), " ", h("a", { class: "btn", href: "/methodology/execution-safety" }, "Execution safety"))));
          return;
        }
        tbl.update(rows.filter(r => match(r, st.filter)));
        if (holder.firstChild !== tbl) holder.replaceChildren(tbl, h("p", { class: "note" }, "Quote = what the route quoted; execution price = what the provider reports charging. Never merged. Uptime is OpenGrid-observed. ",
          h("a", { class: "lnk", href: "/methodology/execution-safety" }, "State machine →")));
      }
      async function load() {
        try { st.rows = await ctx.api("/v1/deployments", { nocache: true }); draw(); }
        catch (e) { holder.replaceChildren(OG.error(e, load)); }
      }
      load();
      ctx.every(30000, load);
    },
  });

  /* =============================== detail =============================== */
  OG.page("/deployments/:id", {
    title: p => "Deployment " + p.id,
    nav: "deployments",
    async mount(el, params, query, ctx) {
      const id = params.id;
      const root = h("div", { class: "pg-dep pg-depd" });
      el.append(root);
      root.append(OG.loading("Loading deployment…"));
      const me = await OG.me();
      if (!ctx.alive()) return;
      const admin = OG.isAdmin(me);
      const intents = {};          // one Idempotency-Key per user intent (terminate / stop), reused only on an unknown outcome
      const flash = h("div", { class: "dep-flash" });
      let d = null, trace = null, quality = null, fb = null, val = null;

      async function load(refresh) {
        try {
          d = await ctx.api("/v1/deployments/" + encodeURIComponent(id), { params: { refresh: refresh ? "true" : "false" }, nocache: true });
        } catch (e) { root.replaceChildren(OG.head("Deployment " + id, null, h("a", { class: "btn", href: "/deployments" }, "← Deployments")), OG.error(e, () => load())); return; }
        const extra = await Promise.all([
          admin ? OG.api.soft("/v1/admin/trace/" + encodeURIComponent(id), { nocache: true }) : null,
          OG.api.soft(admin ? "/v1/admin/quality" : "/v1/quality", { params: { limit: 1000, days: 3650, include_validation: admin ? "true" : null }, nocache: true }),
          OG.api.soft("/v1/deployments/" + encodeURIComponent(id) + "/feedback", { nocache: true }),
          admin && d.purpose === "validation" ? OG.api("/v1/admin/validation/" + encodeURIComponent(id), { nocache: true }).catch(e => ({ error: e })) : null,
        ]);
        if (!ctx.alive()) return;
        [trace, quality, fb, val] = extra;
        draw();
      }

      function reconciliation() {
        if (d.reconciliation) return { rec: d.reconciliation, src: "GET /v1/deployments/{id}" };
        const step = trace && (trace.steps || []).filter(s => s.step === "cost_reconciliation" && s.reconciliation).pop();
        return step ? { rec: step.reconciliation, src: "admin trace (the deployment endpoint does not return it)" } : null;
      }

      function costBlock() {
        const R = reconciliation(), rec = R && R.rec;
        const p = d.prices || {};
        const q = rec && rec.quote;
        const tx = d.transaction;
        const why = d.reconciled_at ? null : TERMINAL.includes(d.status) ? "not reconciled yet (runs after confirmed termination)" : "reconciled after termination";
        const row = (label, concept, v, sub, reason) => h("div", { class: "dep-cost" },
          h("div", { class: "rt-cl" }, label, concept), h("div", { class: "dep-cv" }, v != null ? money(v) : OG.na(reason || why || "unavailable")), sub ? h("div", { class: "rt-cs" }, sub) : null);
        const quoteTotal = q && q.est_total_cost != null ? q.est_total_cost : null;
        const cells = h("div", { class: "dep-costs" },
          row("Quote", OG.conceptBadge("quote"), quoteTotal, q ? `${fmt.price(q.price_per_gpu_hour)}/GPU·h${q.duration_hours ? " × " + fmt.num(q.duration_hours) + " h quoted" : ""}${q.billing_unit ? " · billed " + q.billing_unit : ""}`
            : p.quote != null ? `${fmt.price(p.quote)}/GPU·h (basis ${p.quote_basis || "?"})` : null, p.quote != null ? "the quote had no duration: hourly only" : "no quote recorded"),
          row("Expected cost", OG.kindBadge("estimated"), rec && rec.expected_cost && rec.expected_cost.amount_usd, rec && rec.runtime ? `quote × ${fmt.num(rec.runtime.gpu_hours, 3)} metered GPU-h${rec.runtime.end_estimated ? " (end time estimated)" : ""}` : null),
          row("Provider-reported", OG.conceptBadge("execution"), rec ? rec.provider_reported_cost && rec.provider_reported_cost.amount_usd : d.provider_reported_cost,
            rec && rec.provider_reported_cost ? rec.provider_reported_cost.basis || rec.provider_reported_cost.reason : null, rec && rec.provider_reported_cost && rec.provider_reported_cost.reason),
          row("OpenGrid transaction", OG.kindBadge("transaction"), rec ? rec.opengrid_transaction_cost && rec.opengrid_transaction_cost.amount_usd : null,
            rec && rec.opengrid_transaction_cost ? `${rec.opengrid_transaction_cost.usage_records} usage record(s) · fees ${money(rec.opengrid_transaction_cost.fees_usd)} separate` : tx && tx.provider_cost_usd != null ? `metered so far ${money(tx.provider_cost_usd)} (${tx.cost_basis})` : null));
        const qe = rec && rec.quote_error, eff = rec && rec.effective_hourly_rate, rnd = rec && rec.billing_rounding, unx = rec && rec.unexpected_fees;
        const detail = rec ? kv([
          ["Quote error", qe && qe.amount_usd != null ? h("span", { class: Math.abs(qe.pct || 0) > 2 ? "warn-t" : "" }, `${money(qe.amount_usd)} (${qe.pct != null ? fmt.num(qe.pct, 2) + "%" : "–"})`) : OG.na("no expected cost"), qe && qe.basis],
          ["Effective rate", eff ? h("span", {}, eff.per_gpu_hour_transaction != null ? fmt.price(eff.per_gpu_hour_transaction) + "/GPU·h transaction" : "–",
            h("span", { class: "dim" }, " · provider " + (eff.per_gpu_hour_provider != null ? fmt.price(eff.per_gpu_hour_provider) + "/GPU·h" : "n/a"))) : null],
          ["Billing rounding", rnd ? (rnd.amount_usd != null ? `${money(rnd.amount_usd)} · unit ${rnd.billing_unit}` : h("span", { class: "dim" }, rnd.reason || "–")) : null, rnd && rnd.evidence],
          ["Unexpected fees", unx ? (unx.amount_usd != null ? money(unx.amount_usd) : h("span", { class: "dim" }, unx.reason)) : null, unx && unx.basis],
          ["Runtime billed", rec.runtime ? `${dur(rec.runtime.billable_seconds)} billable · ${dur(rec.runtime.running_seconds)} running · ${dur(rec.runtime.stopped_seconds)} stopped` : null],
          ["Reconciled", d.reconciled_at ? fmt.dateTime(d.reconciled_at) : null, "source: " + R.src],
        ]) : h("p", { class: "note" }, why ? "Cost reconciliation: " + why + "." : "");
        return OG.section("Cost — four numbers, never merged", cells, detail);
      }

      function pricesBlock() {
        const p = d.prices || {};
        const att = (d.provision_attempts || []).find(a => a.credential_ref);
        return OG.section("Prices per GPU-hour · credential",
          kv([
            [h("span", {}, "Observed market ", OG.conceptBadge("observed")), p.observed_market_price != null ? fmt.price(p.observed_market_price) : null, "OpenGrid's observed listing price when routed"],
            [h("span", {}, "List ", OG.conceptBadge("list")), p.list_price != null ? fmt.price(p.list_price) : null, "The provider's catalogue price read at the live check"],
            [h("span", {}, "Quote ", OG.conceptBadge("quote")), p.quote != null ? h("span", {}, fmt.price(p.quote), h("span", { class: "dim" }, " · " + (p.quote_basis || "?"))) : null],
            [h("span", {}, "Execution ", OG.conceptBadge("execution")), p.execution_price != null ? fmt.price(p.execution_price) : OG.na("the provider has not reported what it charges")],
            ["Credential (pinned)", d.credential_source ? h("span", {}, d.credential_source === "byo" ? "BYO — the customer's own provider account" : "OpenGrid-managed provider account",
              att ? h("span", { class: "dim" }, " · ref " + att.credential_ref) : null) : h("span", { class: "dim" }, "not pinned yet (pinned at launch)"), "status, stop, terminate and reconciliation use exactly this credential; the secret is never shown"],
            ["Instance", d.provider_instance_id ? h("span", { class: "mono" }, d.provider_instance_id) : h("span", { class: "dim" }, "not known")],
            ["Client name", h("span", { class: "mono" }, d.client_name || "–"), "the name OpenGrid sends the provider; reconciliation finds the instance by it"],
            ["Approval", d.approved_at ? `${d.approval_mode || ""} by ${d.approved_by} · ${fmt.dateTime(d.approved_at)}` : d.approval_mode ? d.approval_mode + ": not approved yet" : null],
            d.override_limits ? ["Limits overridden", h("span", { class: "warn-t" }, d.override_reason || "")] : null,
          ]));
      }

      function lifecycleBlock() {
        const maxRun = d.effective_max_runtime_minutes ?? d.max_runtime_minutes;
        const at = d.auto_termination || {};
        const live = D.LIVE.includes(d.status);
        const dl = X.deadline(d.terminate_deadline_at);
        const ts = v => v ? h("span", { title: v }, X.exactTime(v)) : null;
        const provBasis = d.billable_basis === "provider_running_at";
        const billBadge = d.billable_basis ? (provBasis ? OG.badge("provider timestamp", "good", "billable time from the provider's own running timestamp") : OG.kindBadge("estimated")) : null;
        return OG.section("Runtime ceiling · lifecycle timestamps · SSH access", h("div", { class: "cols-2" },
          kv([
            ["Max runtime", maxRun ? `${maxRun} min · source ${X.ceilingSource(d.runtime_ceiling_source)}` : null, "effective_max_runtime_minutes: OpenGrid terminates the instance at the deadline; never unlimited"],
            ["Auto-terminates", d.terminate_deadline_at ? h("span", {}, X.exactTime(d.terminate_deadline_at), " ",
              live ? h("b", { class: dl.past ? "down" : "" }, dl.text) : null) : h("span", { class: "dim" }, PRE_LAUNCH.includes(d.status) ? `set at approval: approval time + ${maxRun || "N"} min` : "no deadline"), at.basis ? "basis: " + at.basis : null],
            ["Terminate requested", ts(d.requested_termination_at || d.terminate_requested_at), "when OpenGrid (deadline, user or admin) asked the provider to terminate"],
            ["Provider created", ts(d.provider_created_at), "the provider's creation timestamp"],
            ["Provider running", ts(d.provider_running_at), "the provider's running timestamp (or first observed running)"],
            ["Provider terminated", ts(d.provider_terminated_at), "the provider confirmed the instance gone"],
            ["Billable start", d.billable_start ? h("span", {}, X.exactTime(d.billable_start), " ", billBadge) : null, d.billable_basis ? "basis: " + d.billable_basis : "not known yet"],
            ["Billable end", d.billable_end ? h("span", {}, X.exactTime(d.billable_end), " ", billBadge) : d.billable_start && !TERMINAL.includes(d.status) ? h("span", { class: "warn-t" }, "open: may still be billing") : null, d.billable_basis ? "basis: " + d.billable_basis : null],
            ["Billable basis", d.billable_basis ? h("span", {}, d.billable_basis, " ", billBadge) : null, "provider_running_at = provider timestamp; opengrid_observed_running / provider_created_at = estimate"],
          ]),
          h("div", {}, OG.sshAccessBlock(d.ssh_access, { purpose: d.purpose, keyRef: d.launch && d.launch.ssh_key }),
            d.ssh_access && d.ssh_access.note ? h("p", { class: "note" }, d.ssh_access.note) : null)));
      }

      function qualityRow() {
        const outs = quality && quality.outcomes;
        const o = Array.isArray(outs) ? outs.find(x => x.deployment_id === id) : null;
        if (!o) return OG.section("Routing quality", h("p", { class: "note" }, quality ? "No routing-quality row for this deployment yet (computed by the quality job)." : "Routing quality unavailable here."));
        const pct = v => v == null ? null : fmt.num(v, 2) + "%";
        return OG.section("Routing quality " , kv([
          ["Winner", h("span", {}, OG.providerName(o.winner_provider), " ", h("span", { class: "mono dim" }, o.winner_listing_id || ""), " · ", fmt.price(o.winner_price_per_gpu_hour), "/GPU·h ", OG.conceptBadge("observed"))],
          ["Runner-up", o.runner_up_provider ? `${OG.providerName(o.runner_up_provider)} · ${fmt.price(o.runner_up_price_per_gpu_hour)}/GPU·h` : h("span", { class: "dim" }, `none (${o.candidates_total} candidate${o.candidates_total === 1 ? "" : "s"})`)],
          ["Market median", o.market_median_per_gpu_hour != null ? `${fmt.price(o.market_median_per_gpu_hour)}/GPU·h across ${o.market_providers} providers` : null],
          ["Expected savings", pct(o.expected_savings_pct) || OG.na(o.comparison_reason || "no valid comparison"), "winner's observed price vs the market median at decision time"],
          ["Realized savings", pct(o.realized_savings_pct) || OG.na(o.comparison_reason || "needs the provider's execution price"), "execution price vs the market median at decision time"],
          ["Quote error", pct(o.quote_error_pct) || OG.na("needs an execution price")],
        ]));
      }

      function validationBlock() {
        if (d.purpose !== "validation") return null;
        if (!val) return OG.section("Validation evidence", h("p", { class: "note" }, "Admin only."));
        if (val.error) return OG.section("Validation evidence", val.error.status === 404 ? OG.insufficient("GET /v1/admin/validation/{id} is not available on this server yet.", "Not available yet") : OG.error(val.error));
        const steps = val.steps || {};
        return OG.section("Validation evidence " + (val.validated ? "· validated" : `· ${(val.missing || []).length} step(s) missing`),
          h("ul", { class: "ck-list" }, Object.entries(steps).map(([k, v]) => h("li", { class: "ck-" + (v && v.ok ? "green" : "red") },
            h("i", { class: "ck-dot" }), h("b", {}, k.replace(/_/g, " ")), h("span", { class: "dim" }, evText(Object.fromEntries(Object.entries(v || {}).filter(([kk]) => kk !== "ok"))))))),
          h("p", { class: "note" }, "A provider is marked validated only when every step has provider evidence: launch accepted → observed running → find/list by name → terminate accepted → termination confirmed → cost reconciled."));
      }

      function eventsBlock() {
        const ev = d.events || [];
        if (!ev.length) return OG.section("Events", OG.empty("No events recorded."));
        return OG.section("Events · " + ev.length, h("ol", { class: "dep-tl dep-tl2" }, ev.map(e => h("li", { class: "t-" + (D.tone(e.to) || "plain") + (e.from === e.to ? " note-ev" : "") },
          h("span", { class: "mono dim dep-at" }, fmt.dateTime(e.at) + ":" + String(new Date(e.at).getSeconds()).padStart(2, "0")),
          h("span", { class: "dep-tr" }, e.from === e.to ? h("span", { class: "dim" }, "note") : [h("span", { class: "dim" }, (e.from || "∅").replace(/_/g, " ") + " → "), OG.stateBadge(e.to)]),
          h("span", { class: "badge dep-actor a-" + e.actor }, e.actor + (e.actor_id ? " · " + e.actor_id : "")),
          h("span", { class: "dep-reason" }, e.reason || ""),
          e.evidence ? h("span", { class: "dep-det mono" }, evText(e.evidence)) : null))));
      }

      function attemptsBlock() {
        const a = d.provision_attempts || [];
        return OG.section("Provision attempts · at most one per deployment", a.length ? OG.table({
          columns: [
            { key: "provider", label: "Provider", fmt: v => OG.providerLink(v) },
            { key: "listing_id", label: "Listing", cls: "mono dim" },
            { key: "outcome", label: "Outcome", fmt: v => OG.badge(v || "–", v === "accepted" ? "good" : v === "rejected" ? "bad" : "warn", v === "unknown" ? "the provider may have created an instance" : null) },
            { key: "error_kind", label: "Error", cls: "dim", fmt: v => v || "" },
            { key: "status_code", label: "HTTP", num: true },
            { key: "latency_ms", label: "Latency ms", num: true },
            { key: "started_at", label: "Started", num: true, fmt: v => fmt.dateTime(v) },
            admin ? { key: "error", label: "Detail", cls: "wrap dim", fmt: v => v || "" } : null,
          ].filter(Boolean), rows: a, compact: true, csv: false,
        }) : h("p", { class: "note" }, PRE_LAUNCH.includes(d.status) ? "No provision call yet: it happens once, after approval." : "No provision call was made."));
      }

      function traceBlock() {
        if (!admin) return null;
        if (!trace) return OG.section("Trace", h("p", { class: "note" }, "GET /v1/admin/trace/" + id + " did not answer."));
        const steps = trace.steps || [];
        const KEYS = ["to_status", "status", "outcome", "provider", "reason", "quote_price_per_gpu_hour", "period_start", "period_end", "gpu_hours", "provider_cost_usd", "usage_record_id", "kind"];
        return h("details", { class: "sec dep-trace" }, h("summary", { class: "sec-h" }, `Trace · ${steps.length} steps (admin) · ${trace.route_request_id || ""}`),
          h("ol", { class: "dep-vt" }, steps.map(s => h("li", { class: "vt-" + s.step },
            h("span", { class: "mono dim" }, fmt.time(s.at)), h("b", { class: "mono" }, s.step.replace(/_/g, " ")),
            h("span", { class: "dim dep-det" }, KEYS.filter(k => s[k] != null && typeof s[k] !== "object").map(k => `${k}: ${s[k]}`).join(" · "))))),
          (trace.unavailable || []).length ? h("p", { class: "note" }, "Unavailable: " + trace.unavailable.join("; ")) : null);
      }

      function feedbackBlock() {
        if (d.status !== "terminated") return OG.section("Design-partner feedback", h("p", { class: "note" }, "The feedback form opens once the deployment is terminated."));
        const QS = [["would_have_chosen_provider", "Would you have chosen this provider yourself?"], ["price_better", "Was the price better than what you normally pay?"],
          ["setup_easier", "Was setup easier?"], ["would_route_next", "Would you route the next workload through OpenGrid?"]];
        const cur = fb || {};
        const vals = Object.fromEntries(QS.map(([k]) => [k, cur[k] === true ? "yes" : cur[k] === false ? "no" : ""]));
        const broke = h("textarea", { class: "field og-ask-ta", rows: 2, placeholder: "e.g. SSH key took minutes to attach", maxlength: 4000 }); broke.value = cur.what_broke || "";
        const notes = h("textarea", { class: "field og-ask-ta", rows: 2, maxlength: 4000 }); notes.value = cur.notes || "";
        const msg = h("span", { class: "dim" }, fb ? "saved " + fmt.dateTime(fb.updated_at || fb.created_at) + " · POST again to edit" : "");
        const form = h("form", { class: "dep-fb", onsubmit: async e => {
          e.preventDefault();
          const body = { what_broke: broke.value.trim() || null, notes: notes.value.trim() || null };
          for (const [k] of QS) body[k] = vals[k] === "yes" ? true : vals[k] === "no" ? false : null;
          msg.textContent = "saving…";
          try { fb = await ctx.api(`/v1/deployments/${encodeURIComponent(id)}/feedback`, { method: "POST", body }); msg.textContent = "saved · thank you"; msg.className = "up"; }
          catch (err) { msg.replaceChildren(OG.error(err)); }
        } },
          QS.map(([k, q]) => h("div", { class: "dep-fq" }, h("span", {}, q), OG.seg([["yes", "Yes"], ["no", "No"], ["", "Not sure"]], vals[k], v => { vals[k] = v; }))),
          h("label", { class: "og-ask-f" }, h("span", { class: "og-ask-l" }, "What broke?"), broke),
          h("label", { class: "og-ask-f" }, h("span", { class: "og-ask-l" }, "Notes"), notes),
          h("div", { class: "dep-acts" }, h("button", { class: "btn pri", type: "submit" }, fb ? "Update feedback" : "Send feedback"), msg));
        return OG.section("Design-partner feedback", form, h("p", { class: "note" }, "Opinions only: never used in reliability or savings figures."));
      }

      async function act(what) {
        const pre = PRE_LAUNCH.includes(d.status);
        const ok = await OG.dialog({
          title: what === "stop" ? `Stop ${id}?` : pre ? `Cancel ${id} before launch?` : `Terminate ${id}?`, danger: true,
          confirm: what === "stop" ? "Stop" : pre ? "Cancel request" : "Terminate",
          ack: what === "terminate" && !pre ? "I understand the instance and its local data are destroyed at the provider" : null,
          body: h("div", {},
            h("p", {}, pre ? "Nothing has been launched; this cancels the request (state → rejected)." : `${what === "stop" ? "Stops" : "Terminates"} the instance on ${OG.providerName(d.provider)} (${d.gpu_count}× ${OG.shortGpu(d.gpu || "")}).`),
            what === "stop" ? h("p", { class: "warn-t" }, "A stopped instance may keep billing (storage or full price, per provider). Only terminate is guaranteed to end spend.")
              : !pre ? h("p", {}, "OpenGrid marks it terminated only when the provider confirms (status read or instance list). One Idempotency-Key per click: a retry after a network error cannot terminate twice.") : null),
        });
        if (!ok) return;
        const intent = intents[what] = OG.intentFor(intents[what], what + ":" + id, {});
        flash.replaceChildren(OG.loading(what === "stop" ? "Stopping…" : "Terminating…"));
        try {
          const r = await OG.api.intent(intent, `/v1/deployments/${encodeURIComponent(id)}/${what}`, { method: "POST" });
          const note = (r && (r.terminate || r.stop)) || {};
          flash.replaceChildren(h("div", { class: "og-ok" }, h("b", {}, what === "stop" ? "Stop requested" : "Terminate requested"), " ", note.note || note.outcome || ""));
          load();
        } catch (e) {
          flash.replaceChildren(OG.error(e), intent.state === "unknown" ? h("p", { class: "warn-t" }, "Outcome unknown. Retrying reuses the same Idempotency-Key (" + intent.key.slice(0, 12) + "…), so the server replays instead of acting twice.") : null);
          if (e.body && e.body.detail && e.body.detail.deployment) load();
        }
      }

      function draw() {
        const live = D.LIVE.includes(d.status);
        const canTerm = !TERMINAL.includes(d.status);
        const canStop = d.supports_stop && ["running", "degraded"].includes(d.status) && d.provider_instance_id;
        const acts = [
          h("button", { class: "btn", type: "button", title: "GET ?refresh=true: asks the provider now (read-only)", onclick: () => { flash.replaceChildren(OG.loading("Asking the provider…")); load(true).then(() => flash.replaceChildren(d && d.refresh ? h("div", { class: "note" }, d.refresh.refreshed ? "Refreshed from the provider. " + (d.refresh.note || "") : "Not refreshed: " + (d.refresh.reason || d.refresh.note || "")) : "")); } }, "Refresh from provider"),
          canStop ? h("button", { class: "btn", type: "button", onclick: () => act("stop") }, "Stop…") : null,
          canTerm ? h("button", { class: "btn w4-danger", type: "button", onclick: () => act("terminate") }, PRE_LAUNCH.includes(d.status) ? "Cancel request…" : "Terminate…") : null,
          OG.copy(id, { title: "Copy the deployment id" }),
        ];
        const banner = STATE_NOTE[d.status] ? h("div", { class: "og-alarm t-" + (D.tone(d.status) || "plain") }, OG.stateBadge(d.status), h("span", {}, STATE_NOTE[d.status]),
          d.failure_reason ? h("span", { class: "mono dim" }, " · " + d.failure_reason) : null) : null;
        const est = (d.prices.execution_price ?? d.prices.quote);
        const alarm = X.billingAlarm(d);
        const billAlarm = alarm ? h("div", { class: "og-alarm t-bad x-billing" }, h("b", {}, alarm.title), h("span", {}, alarm.text),
          canTerm ? h("button", { class: "btn sm w4-danger", type: "button", onclick: () => act("terminate") }, "Terminate…") : null) : null;
        const maxRun = d.effective_max_runtime_minutes ?? d.max_runtime_minutes;
        root.replaceChildren(
          OG.head(h("span", { class: "dep-title" }, "Deployment ", h("span", { class: "mono" }, id)),
            h("span", {}, d.provider ? OG.providerLink(d.provider) : "not placed", " · ", `${d.gpu_count}× `, OG.gpuLink(d.gpu || ""), d.region ? " · " + d.region : "", " · ",
              d.purpose === "validation" ? OG.badge("validation", "warn") : "customer", d.route_request_id ? [" · ", h("a", { class: "lnk mono", href: "/route?rr=" + d.route_request_id }, d.route_request_id)] : null),
            OG.stateBadge(d.status), acts),
          flash, billAlarm, banner, OG.stateStrip(d.status, d.events),
          OG.stats([
            { label: "State since", value: d.state_changed_at ? fmt.age(d.state_changed_at) : null, sub: d.state_changed_at ? fmt.dateTime(d.state_changed_at) : null },
            { label: "Runtime", value: dur(d.uptime_seconds), sub: "OpenGrid-observed running", kind: "transaction" },
            { label: "Max runtime", value: maxRun ? maxRun + " min" : null, reason: "not reported", sub: "source " + X.ceilingSource(d.runtime_ceiling_source) },
            { label: "Auto-terminate", value: d.terminate_deadline_at ? (live ? OG.countdown(d.terminate_deadline_at, { expired: "PAST DEADLINE" }) : fmt.dateTime(d.terminate_deadline_at)) : null, reason: PRE_LAUNCH.includes(d.status) ? "set at approval: approval time + " + (maxRun || "N") + " min" : "no deadline", sub: d.terminate_deadline_at ? X.exactTime(d.terminate_deadline_at) : null },
            { label: "Accrued (est.)", value: est != null && d.uptime_seconds ? OG.money(est * d.gpu_count * d.uptime_seconds / 3600) : null, reason: "nothing ran", kind: "estimated", sub: "price × GPUs × observed runtime" },
            { label: "Last checked", value: d.last_checked_at ? fmt.age(d.last_checked_at) : null, reason: "never read from the provider", sub: d.last_checked_at ? fmt.dateTime(d.last_checked_at) : null },
            { label: "Reconciled", value: d.reconciled_at ? fmt.dateTime(d.reconciled_at) : null, reason: TERMINAL.includes(d.status) ? "pending" : "after termination" },
          ]),
          h("div", { class: "cols-2" }, costBlock(), pricesBlock()),
          lifecycleBlock(),
          (d.limit_violations || []).length ? OG.section("Limit violations at approval", violationTable(d.limit_violations)) : null,
          h("div", { class: "cols-2" }, h("div", {}, eventsBlock(), attemptsBlock()), h("div", {}, qualityRow(), validationBlock(), feedbackBlock())),
          traceBlock(),
          h("p", { class: "note" }, "Data kinds: quote / execution / transaction are never mixed. ", h("a", { class: "lnk", href: "/methodology/execution-safety" }, "Execution safety →")));
      }

      load();
      ctx.every(15000, () => { if (d && !TERMINAL.includes(d.status)) load(); });
    },
  });

  function violationTable(vs) {
    return OG.table({
      columns: [
        { key: "code", label: "Limit", cls: "mono" },
        { key: "value", label: "Value", num: true, fmt: v => typeof v === "number" ? fmt.num(v, v < 100 ? 4 : 2) : String(v) },
        { key: "limit", label: "Cap", num: true, fmt: v => typeof v === "number" ? fmt.num(v, v < 100 ? 4 : 2) : Array.isArray(v) ? v.join(", ") : String(v) },
        { key: "overridable", label: "Override", fmt: v => v ? OG.badge("admin may override", "warn") : OG.badge("never", "bad") },
        { key: "message", label: "Message", cls: "wrap dim" },
      ], rows: vs, compact: true, csv: false,
    });
  }
  OG.violationTable = violationTable;
})();
