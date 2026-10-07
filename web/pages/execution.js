/* /admin/execution — the operator's execution control surface (admin scope; the API enforces it).
   Top, because they can bill: uncertain deployments (launch_unknown / provider_timeout / termination_failed /
   orphan_suspected / credentials_unavailable / degraded) and ORPHANS (instances that may bill with no live
   deployment), each with its evidence and an action. Then the global STOP ALL LIVE PROVISIONING kill switch
   and the execution mode (4 modes, consequences, reason required, env ceiling), the approval queue, live
   deployments with force-terminate, provider flags (validated before live), the capability matrix, the
   validation launcher (1 GPU, <= $3/h, <= 30 min) and its evidence, reconciliation runs, account limits and the
   control audit log. Every change needs a reason; every money-moving POST carries one Idempotency-Key per intent.
   Endpoints still being built answer 404: shown as "not available yet", never as empty. */
(() => {
  const { h, fmt } = OG;
  const D = OG.dep;
  const MODES = ["DISABLED", "PREVIEW_ONLY", "SUPERVISED", "LIVE"];
  const HOT = D.UNCERTAIN.concat(["termination_failed"]);
  const MATRIX_ROWS = ["quote", "live_availability", "launch", "ssh_key_injection", "startup_script", "status", "stop", "terminate", "region_selection",
    "gpu_count_selection", "price_known_before_launch", "billing_unit", "minimum_commitment", "interruptible", "name_tag_at_launch", "list_instances",
    "find_by_name", "idempotency_token", "reported_cost", "stopped_billing", "error_semantics"];
  const notYet = (what, e) => e && e.status === 404 ? OG.insufficient(what + " is not available on this server yet.", "Not available yet") : OG.error(e);
  const evText = o => !o || typeof o !== "object" ? String(o ?? "") : Object.entries(o).filter(([, v]) => v != null && v !== "").map(([k, v]) => `${k}: ${typeof v === "object" ? JSON.stringify(v).slice(0, 140) : v}`).join(" · ");

  OG.page("/admin/execution", {
    title: "Execution control",
    nav: "admin-execution",
    async mount(el, params, query, ctx) {
      const root = h("div", { class: "pg-ex" });
      el.append(root);
      const me = await OG.me();
      if (!ctx.alive()) return;
      if (!OG.isAdmin(me)) { root.append(OG.head("Execution control"), OG.error({ status: 403, message: "admin scope required" })); return; }
      const I = {};   // intents: one Idempotency-Key per user intent
      const B = {};
      for (const k of ["alarm", "orphans", "kill", "mode", "queue", "live", "flags", "matrix", "validation", "recon", "limits", "log"]) B[k] = h("div", { class: "ex-b ex-" + k });
      const modeBadge = h("span");
      root.append(
        OG.head("Execution control", "Mode, kill switches, providers, orphans and reconciliation · every change needs a reason and is logged",
          modeBadge, h("a", { class: "btn", href: "/admin/checklist" }, "First-route checklist →"), h("button", { class: "btn", type: "button", onclick: () => loadAll() }, "Reload")),
        B.alarm, B.orphans,
        h("div", { class: "cols-2 ex-ctl" }, B.kill, B.mode),
        B.queue, B.live,
        OG.section("Provider execution flags", B.flags),
        h("details", { class: "sec ex-mx", open: query.matrix === "1" ? true : null }, h("summary", { class: "sec-h" }, "Capability matrix · what each adapter can really do (evidence on hover)"), B.matrix),
        h("div", { class: "cols-2" }, OG.section("Validation launcher", B.validation), OG.section("Reconciliation", B.recon)),
        OG.section("Account limits (cost guards)", B.limits),
        OG.section("Control audit log", B.log));

      let modeSt = null, all = [], flags = [], caps = [];

      /* ---------- uncertain deployments + orphans (top: they may bill) ---------- */
      function drawAlarm() {
        const hot = all.filter(r => HOT.includes(r.status));
        if (!hot.length) { B.alarm.replaceChildren(h("div", { class: "og-ok ex-allclear" }, h("b", {}, "No uncertain deployments."), " Every deployment's state is backed by provider evidence.")); return; }
        B.alarm.replaceChildren(h("div", { class: "ex-hot" },
          h("div", { class: "ex-hot-h" }, h("b", {}, `${hot.length} UNCERTAIN DEPLOYMENT${hot.length === 1 ? "" : "S"}`), h("span", {}, "an instance may exist and bill · reconciliation must resolve these before any retry")),
          OG.table({
            columns: [
              { key: "status", label: "State", fmt: v => OG.stateBadge(v) },
              { key: "deployment_id", label: "Deployment", cls: "mono", href: r => "/deployments/" + r.deployment_id },
              { key: "account_id", label: "Acct", num: true },
              { key: "provider", label: "Provider", fmt: v => v ? OG.providerLink(v) : "–" },
              { key: "gpu", label: "GPU", fmt: (v, r) => h("span", {}, `${r.gpu_count}× `, OG.gpuLink(v || "")) },
              { key: "client_name", label: "Client name", cls: "mono dim" },
              { key: "provider_instance_id", label: "Instance", cls: "mono", fmt: v => v || h("span", { class: "warn-t" }, "unknown") },
              { key: "state_changed_at", label: "Since", num: true, fmt: v => fmt.age(v) },
              { key: "accrued_cost_estimate_usd", label: "Accrued est.", num: true, fmt: v => v != null ? OG.money(v) : OG.na("no running time observed") },
              { key: "failure_reason", label: "Reason", cls: "wrap dim", fmt: v => v || "" },
              { key: "act", label: "", sort: false, csv: false, fmt: (v, r) => forceBtn(r) },
            ], rows: hot, compact: true, csv: false, sort: { key: "state_changed_at", dir: "asc" }, rowClass: r => "t-" + (D.tone(r.status) || "plain"),
          })));
      }
      async function loadOrphans() {
        let rows, open = null, src = "GET /v1/admin/orphans";
        try { const r = await ctx.api("/v1/admin/orphans", { full: true, nocache: true }); rows = r.data; open = r.meta && r.meta.open; }
        catch (e) {
          const ov = await OG.api.soft("/v1/admin/execution/overview", { nocache: true });
          if (!ov || !Array.isArray(ov.orphans)) { B.orphans.replaceChildren(OG.section("Orphans", notYet("GET /v1/admin/orphans", e))); return; }
          rows = ov.orphans; src = "execution overview (resolve endpoint unavailable)";
        }
        const live = rows.filter(o => ["open", "terminating"].includes(o.status));
        const perHour = live.reduce((s, o) => s + (o.price_per_hour || 0), 0);
        const unknownPrice = live.filter(o => o.price_per_hour == null).length;
        B.orphans.replaceChildren(h("div", { class: "ex-orph" + (live.length ? " hot" : "") },
          h("div", { class: "ex-hot-h" }, h("b", {}, live.length ? `${open ?? live.length} OPEN ORPHAN${live.length === 1 ? "" : "S"}` : "No open orphans"),
            live.length ? h("span", {}, `≈ ${OG.money(perHour)}/h at the provider's listed price${unknownPrice ? ` + ${unknownPrice} with unknown price` : ""} · OpenGrid never auto-terminates an instance it cannot prove it owns`) : h("span", { class: "dim" }, "every instance the providers list is accounted for"),
            h("span", { class: "spacer" }), h("span", { class: "dim mono" }, src)),
          rows.length ? OG.table({
            columns: [
              { key: "status", label: "Status", fmt: v => OG.badge(v, v === "open" ? "bad" : v === "terminating" ? "warn" : "") },
              { key: "provider", label: "Provider", fmt: v => OG.providerLink(v) },
              { key: "instance_id", label: "Instance", cls: "mono" },
              { key: "instance_name", label: "Name", cls: "mono" },
              { key: "kind", label: "Kind", cls: "mono dim", title: "og_no_deployment: named og-* but no OpenGrid deployment; terminated_still_alive: OpenGrid terminated it, the provider still lists it" },
              { key: "deployment_id", label: "Deployment", cls: "mono", fmt: v => v ? h("a", { class: "lnk", href: "/deployments/" + v }, v) : h("span", { class: "dim" }, "none") },
              { key: "provider_state", label: "Provider state" },
              { key: "price_per_hour", label: "$/h", num: true, fmt: v => v != null ? fmt.price(v) : OG.na("the provider list gave no price") },
              { key: "first_seen_at", label: "First seen", num: true, fmt: v => fmt.age(v) + " ago" },
              { key: "seen_count", label: "Seen", num: true },
              { key: "provably_ours", label: "Ours?", fmt: v => v ? OG.badge("provably", "good", "og-* name AND a terminal deployment") : OG.badge("unproven", "warn") },
              { key: "credential_ref", label: "Credential", cls: "mono dim" },
              { key: "act", label: "Resolve", sort: false, csv: false, fmt: (v, r) => ["open", "terminating"].includes(r.status) ? h("span", { class: "ex-acts" },
                ["terminate", "adopt", "ignore"].map(a => h("button", { class: "btn sm" + (a === "terminate" ? " w4-danger" : ""), type: "button", disabled: a === "adopt" && !r.deployment_id ? true : null, onclick: () => resolveOrphan(r, a) }, a))) : h("span", { class: "dim" }, r.action ? `${r.action} by ${r.resolved_by}` : "") },
            ], rows, compact: true, csv: "opengrid-orphans.csv", sort: { key: "status", dir: "asc" }, stickyLast: 1,
          }) : null));
      }
      async function resolveOrphan(o, action) {
        const help = { terminate: "Calls the provider's delete with the credential that listed it. Billing stops when the provider confirms.", adopt: "Links the instance to its unresolved deployment (launch_unknown / provider_timeout / orphan_suspected); tracking and billing resume on that deployment.", ignore: "Stops alerting. The instance stays at the provider: someone outside OpenGrid owns it." };
        const v = await OG.ask({ title: `${action} orphan ${o.instance_id} on ${OG.providerName(o.provider)}`, danger: action !== "adopt", confirm: action,
          body: h("p", {}, help[action]), fields: [
            action === "terminate" ? { key: "id", label: `Type the instance id to confirm: ${o.instance_id}`, match: o.instance_id, required: true } : null,
            { key: "reason", type: "textarea", label: "Reason (required, logged)", required: true, minLength: 3 }].filter(Boolean) });
        if (!v) return;
        const body = { action, reason: v.reason };
        const k = "orphan:" + o.id + ":" + action;
        I[k] = OG.intentFor(I[k], k, body);
        try { await OG.api.intent(I[k], `/v1/admin/orphans/${o.id}/resolve`, { method: "POST", body }); loadOrphans(); loadLog(); }
        catch (e) { B.orphans.append(notYet("POST /v1/admin/orphans/{id}/resolve", e)); }
      }

      /* ---------- kill switch + mode ---------- */
      function drawKill() {
        const eff = modeSt && modeSt.effective_mode;
        const killed = eff === "DISABLED";
        B.kill.replaceChildren(h("div", { class: "ex-kill" + (killed ? " on" : "") },
          h("div", { class: "eyebrow" }, "Global kill switch"),
          killed ? h("div", { class: "ex-kill-on" }, h("b", {}, "ALL LIVE PROVISIONING IS STOPPED"), h("span", {}, `since ${fmt.dateTime(modeSt.updated_at)} by ${modeSt.updated_by}: ${modeSt.reason || ""}`),
            h("span", { class: "dim" }, "Restore by choosing a mode on the right (reason required)."))
            : h("button", { class: "btn ex-kill-btn", type: "button", onclick: killAll }, "STOP ALL LIVE PROVISIONING"),
          h("p", { class: "note" }, "Sets the mode to DISABLED: no new launch anywhere. Monitoring, status polling, reconciliation, stop and terminate keep working.")));
      }
      async function killAll() {
        const v = await OG.ask({ title: "STOP ALL LIVE PROVISIONING", danger: true, confirm: "Stop all launches",
          body: h("p", {}, "Every new launch is refused until an admin restores a mode. Running deployments keep running and can still be terminated."),
          fields: [{ key: "word", label: "Type STOP to confirm", match: "STOP", required: true }, { key: "reason", type: "textarea", label: "Reason (required, logged, alerts ops)", required: true, minLength: 3 }] });
        if (!v) return;
        try { modeSt = await ctx.api("/v1/admin/execution/kill", { method: "POST", body: { reason: v.reason } }); drawMode(); drawKill(); loadLog(); }
        catch (e) { B.kill.append(OG.error(e)); }
      }
      function drawMode() {
        if (!modeSt) { B.mode.replaceChildren(OG.loading()); return; }
        const eff = modeSt.effective_mode, stored = modeSt.stored_mode, env = modeSt.env_ceiling || {};
        modeBadge.replaceChildren(OG.badge("mode " + eff, eff === "LIVE" ? "bad" : eff === "SUPERVISED" ? "warn" : eff === "DISABLED" ? "bad" : ""));
        B.mode.replaceChildren(h("div", { class: "ex-mode" },
          h("div", { class: "eyebrow" }, "Execution mode"),
          OG.modeBanner(modeSt),
          h("div", { class: "ex-modes" }, MODES.map(m => h("button", { type: "button", class: "ex-m" + (m === stored ? " on" : "") + (m === eff ? " eff" : ""), "aria-pressed": String(m === stored), onclick: () => setMode(m) },
            h("b", {}, m.replace("_", " ")), h("span", {}, OG.MODE_HELP[m]),
            !env.routing_live_provisioning && (m === "SUPERVISED" || m === "LIVE") ? h("i", { class: "warn-t" }, "capped to PREVIEW ONLY by the env ceiling") : null))),
          h("table", { class: "dep-kv" },
            h("tr", {}, h("th", {}, "Env ceiling"), h("td", {}, env.routing_live_provisioning ? h("span", { class: "warn-t" }, "ROUTING_LIVE_PROVISIONING=true · the stored mode applies") : "ROUTING_LIVE_PROVISIONING=false · capped at PREVIEW_ONLY")),
            h("tr", {}, h("th", {}, "Stored"), h("td", {}, stored, modeSt.default ? h("span", { class: "dim" }, " (default: no row)") : h("span", { class: "dim" }, ` · ${modeSt.updated_by} · ${fmt.dateTime(modeSt.updated_at)} · ${modeSt.reason || ""}`))))));
      }
      async function setMode(m) {
        if (modeSt && m === modeSt.stored_mode) return;
        const v = await OG.ask({ title: "Set execution mode " + m, danger: m === "LIVE" || m === "SUPERVISED", confirm: "Set " + m,
          body: h("div", {}, h("p", {}, OG.MODE_HELP[m]), m !== "DISABLED" && m !== "PREVIEW_ONLY" ? h("p", { class: "warn-t" }, "Check the first-route checklist is green before enabling launches.") : null),
          fields: [m === "LIVE" ? { key: "word", label: "Type LIVE to confirm", match: "LIVE", required: true } : null, { key: "reason", type: "textarea", label: "Reason (required, logged)", required: true, minLength: 3 }].filter(Boolean) });
        if (!v) return;
        try { modeSt = await ctx.api("/v1/admin/execution/mode", { method: "POST", body: { mode: m, reason: v.reason } }); drawMode(); drawKill(); loadLog(); }
        catch (e) { B.mode.append(OG.error(e)); }
      }

      /* ---------- approval queue, live deployments ---------- */
      function forceBtn(r) {
        if (["terminated", "rejected", "provision_failed", "provider_rejected", "quote_failed"].includes(r.status)) return null;
        return h("button", { class: "btn sm w4-danger", type: "button", onclick: () => forceTerminate(r) }, "Force-terminate");
      }
      async function forceTerminate(r) {
        const v = await OG.ask({ title: "Force-terminate " + r.deployment_id, danger: true, confirm: "Force-terminate",
          body: h("p", {}, `Admin terminate on ${OG.providerName(r.provider)} with the deployment's pinned credential (any account, any mode). OpenGrid marks it terminated only when the provider confirms.`),
          fields: [{ key: "id", label: "Type the deployment id to confirm", match: r.deployment_id, required: true }, { key: "reason", type: "textarea", label: "Reason (required, logged)", required: true, minLength: 3 }] });
        if (!v) return;
        const body = { reason: v.reason }, k = "force:" + r.deployment_id;
        I[k] = OG.intentFor(I[k], k, body);
        try { await OG.api.intent(I[k], `/v1/admin/deployments/${encodeURIComponent(r.deployment_id)}/terminate`, { method: "POST", body }); loadDeps(); loadLog(); }
        catch (e) { B.live.append(OG.error(e)); }
      }
      function depTable(rows, extra, empty) {
        return OG.table({
          columns: [
            { key: "status", label: "State", fmt: v => OG.stateBadge(v) },
            { key: "deployment_id", label: "Deployment", cls: "mono", href: r => "/deployments/" + r.deployment_id },
            { key: "purpose", label: "Purpose", fmt: v => v === "validation" ? OG.badge("validation", "warn") : h("span", { class: "dim" }, v) },
            { key: "account_id", label: "Acct", num: true },
            { key: "provider", label: "Provider", fmt: v => v ? OG.providerLink(v) : "–" },
            { key: "gpu", label: "GPU", fmt: (v, r) => h("span", {}, `${r.gpu_count}× `, OG.gpuLink(v || "")) },
            { key: "quote", label: "Quote $/GPU·h", num: true, value: r => r.prices && r.prices.quote, fmt: v => fmt.price(v) },
            ...extra,
          ], rows, compact: true, csv: false, empty, sort: { key: "deployment_id", dir: "asc" },
        });
      }
      function drawDeps() {
        const pend = all.filter(r => ["pending_approval", "quote_expired", "approved"].includes(r.status));
        B.queue.replaceChildren(OG.section(`Approval queue · ${pend.length}`, depTable(pend, [
          { key: "limit_violations", label: "Limits", value: r => (r.limit_violations || []).length, fmt: (v, r) => (r.limit_violations || []).length ? OG.badge(r.limit_violations.map(x => x.code).join(", "), "bad") : OG.badge("within limits", "good") },
          { key: "created_at", label: "Requested", num: true, fmt: v => fmt.age(v) + " ago" },
          { key: "open", label: "", sort: false, fmt: (v, r) => h("a", { class: "btn sm pri", href: "/route?rr=" + r.route_request_id }, "Review ticket →") },
        ], "Nothing waiting for approval.")));
        const live = all.filter(r => D.LIVE.includes(r.status) && !HOT.includes(r.status));
        const perH = live.reduce((s, r) => s + ((r.prices.execution_price ?? r.prices.quote) || 0) * (r.gpu_count || 0), 0);
        B.live.replaceChildren(OG.section(`Live deployments · ${live.length} · ≈ ${OG.money(perH)}/h at quote`, depTable(live, [
          { key: "uptime_seconds", label: "Runtime", num: true, fmt: v => fmt.age(v || 0) },
          { key: "terminate_deadline_at", label: "Auto-terminate", num: true, fmt: v => v ? OG.countdown(v, { expired: "past deadline" }) : OG.na("no deadline") },
          { key: "accrued_cost_estimate_usd", label: "Accrued est.", num: true, fmt: v => v != null ? OG.money(v) : "–" },
          { key: "act", label: "", sort: false, csv: false, fmt: (v, r) => forceBtn(r) },
        ], "Nothing is running.")));
      }
      async function loadDeps() {
        try { all = await ctx.api("/v1/admin/deployments", { params: { limit: 2000 }, nocache: true }); drawAlarm(); drawDeps(); drawValidation(); }
        catch (e) { B.alarm.replaceChildren(OG.error(e, loadDeps)); }
      }

      /* ---------- provider flags, capability matrix ---------- */
      function drawFlags() {
        const capOf = p => caps.find(c => c.provider === p) || {};
        const rows = flags.map(f => Object.assign({}, f, { level: capOf(f.provider).level_implemented, creds: capOf(f.provider).credentials_configured }));
        B.flags.replaceChildren(OG.table({
          columns: [
            { key: "provider", label: "Provider", fmt: v => OG.providerLink(v) },
            { key: "level", label: "Lvl", num: true, title: "OpenGrid integration level (2+ = can provision)" },
            { key: "adapter_status", label: "Adapter", fmt: v => v === "validated" ? OG.badge("validated", "good") : OG.badge("simulated", "warn", "never run against a real provider account: customer launches refused") },
            { key: "validated_at", label: "Validated", num: true, fmt: (v, r) => v ? h("span", {}, fmt.dateTime(v), r.validation_deployment_id ? [" ", h("a", { class: "lnk mono", href: "/deployments/" + r.validation_deployment_id }, "evidence")] : null) : "–" },
            { key: "creds", label: "Credentials", fmt: v => v ? OG.badge("configured", "good") : h("span", { class: "dim" }, "none") },
            { key: "supervised_enabled", label: "Supervised", sort: false, fmt: (v, r) => toggle(r, "supervised_enabled", r.adapter_status !== "validated" ? "validate the adapter first (validation launches do not need this flag)" : null) },
            { key: "live_enabled", label: "Live", sort: false, fmt: (v, r) => toggle(r, "live_enabled", r.adapter_status !== "validated" ? "live needs a validated adapter" : null) },
            { key: "killed", label: "Kill switch", sort: false, fmt: (v, r) => v ? h("span", { class: "ex-acts" }, OG.badge("killed", "bad", r.kill_reason), h("button", { class: "btn sm", type: "button", onclick: () => killProvider(r, false) }, "Unkill"))
              : h("button", { class: "btn sm w4-danger", type: "button", onclick: () => killProvider(r, true) }, "Kill") },
            { key: "kill_reason", label: "Note", cls: "wrap dim", fmt: (v, r) => v ? `${v} (${r.killed_by})` : r.updated_by ? `${r.updated_by} · ${fmt.age(r.updated_at)} ago` : "defaults" },
          ], rows, compact: true, csv: false, sort: { key: "level", dir: "desc" },
        }), h("p", { class: "note" }, "Customer launches need a validated adapter AND the supervised (admin approves each) or live flag. ", h("b", {}, "validated"), " is set only by a complete, provider-confirmed validation cycle; it can be demoted, never typed in."));
      }
      function toggle(r, key, block) {
        const on = !!r[key];
        return h("button", { type: "button", class: "ex-tg" + (on ? " on" : ""), disabled: block && !on ? true : null, title: block && !on ? block : on ? "on: click to turn off" : "off: click to turn on", "aria-pressed": String(on),
          onclick: () => setFlag(r, key, !on) }, on ? "ON" : "off");
      }
      async function setFlag(r, key, val) {
        const label = key === "live_enabled" ? "live (no per-launch approval)" : "supervised";
        const v = await OG.ask({ title: `${val ? "Enable" : "Disable"} ${label} launches on ${OG.providerName(r.provider)}`, danger: val, confirm: val ? "Enable" : "Disable",
          fields: [key === "live_enabled" && val ? { key: "word", label: `Type ${r.provider} to confirm`, match: r.provider, required: true } : null, { key: "reason", type: "textarea", label: "Reason (required, logged)", required: true, minLength: 3 }].filter(Boolean) });
        if (!v) return;
        try { await ctx.api("/v1/admin/execution/providers/" + encodeURIComponent(r.provider), { method: "POST", body: { [key]: val, reason: v.reason } }); loadFlags(); loadLog(); }
        catch (e) { B.flags.append(OG.error(e)); }
      }
      async function killProvider(r, kill) {
        const v = await OG.ask({ title: `${kill ? "Kill" : "Unkill"} ${OG.providerName(r.provider)}`, danger: kill, confirm: kill ? "Kill provider" : "Lift kill switch",
          body: h("p", {}, kill ? "No new launch on this provider. Running deployments keep being tracked and can be terminated." : "Launches on this provider follow its flags again."),
          fields: [{ key: "reason", type: "textarea", label: "Reason (required, logged)", required: true, minLength: 3 }] });
        if (!v) return;
        try { await ctx.api(`/v1/admin/execution/providers/${encodeURIComponent(r.provider)}/${kill ? "kill" : "unkill"}`, { method: "POST", body: { reason: v.reason } }); loadFlags(); loadLog(); }
        catch (e) { B.flags.append(OG.error(e)); }
      }
      function drawMatrix() {
        const provs = caps.filter(c => c.matrix);
        if (!provs.length) { B.matrix.replaceChildren(OG.insufficient("No adapter publishes a capability matrix on this server.", "No matrix")); return; }
        const cellOf = (c, k) => {
          const x = c.matrix[k];
          if (!x) return h("td", { class: "mx-c mx-na" }, "–");
          const val = String(x.value ?? x);
          const t = /^yes/i.test(val) ? "yes" : /^no/i.test(val) ? "no" : /^partial/i.test(val) ? "partial" : "info";
          return h("td", { class: "mx-c mx-" + t, title: x.evidence || "" }, val);
        };
        B.matrix.replaceChildren(h("div", { class: "tbl" }, h("div", { class: "tbl-scroll" }, h("table", { class: "grid-t compact mx" },
          h("thead", {}, h("tr", {}, h("th", {}, "Capability"), provs.map(c => h("th", {}, OG.providerName(c.provider), h("div", { class: "dim mx-st" }, c.validation_status || c.adapter_status || ""))))),
          h("tbody", {}, MATRIX_ROWS.filter(k => provs.some(c => c.matrix[k])).map(k => h("tr", {}, h("th", { class: "mx-k" }, k.replace(/_/g, " ")), provs.map(c => cellOf(c, k)))),
            h("tr", {}, h("th", { class: "mx-k" }, "provider-specific risks"), provs.map(c => h("td", { class: "mx-c mx-risk" }, (c.matrix.risks || []).map(r => h("div", {}, "• " + r))))))))),
          h("p", { class: "note" }, "From each adapter's CAPABILITIES (GET /v1/capabilities). YES / NO / PARTIAL as documented; hover a cell for the evidence (docs URL or observed behaviour)."));
      }
      async function loadFlags() {
        try {
          [flags, caps] = await Promise.all([ctx.api("/v1/admin/execution/providers", { nocache: true }), ctx.api("/v1/capabilities", { nocache: true })]);
          drawFlags(); drawMatrix(); drawValidation();
        } catch (e) { B.flags.replaceChildren(OG.error(e, loadFlags)); }
      }

      /* ---------- validation launcher ---------- */
      let valFollow = query.vdep || null, valEv = null;
      function drawValidation() {
        const provs = caps.filter(c => (c.level_implemented || 0) >= 2).map(c => c.provider);
        if (!provs.length) { B.validation.replaceChildren(OG.loading()); return; }
        const sel = h("select", { class: "field" }, provs.map(p => h("option", { value: p }, OG.providerName(p) + " · " + ((flags.find(f => f.provider === p) || {}).adapter_status || "simulated"))));
        const gpu = h("input", { class: "field", placeholder: "GPU slug (optional)" });
        const vdeps = all.filter(r => r.purpose === "validation");
        B.validation.replaceChildren(
          h("div", { class: "ex-cap" }, h("b", {}, "Validation cap (not overridable): "), "1 instance · 1 GPU · ≤ $3.00/h total · ≤ 30 min runtime · auto-terminate · admin approval"),
          h("div", { class: "bar" }, sel, gpu, h("button", { class: "btn pri", type: "button", onclick: () => startValidation(sel.value, gpu.value.trim()) }, "Validate provider…")),
          h("p", { class: "note" }, "Creates a purpose=validation route pending approval. Approve it on its ticket, watch it run, terminate it (or let the deadline do it); the evidence below must be complete before the adapter becomes validated."),
          vdeps.length ? OG.table({
            columns: [{ key: "status", label: "State", fmt: v => OG.stateBadge(v) }, { key: "deployment_id", label: "Validation deployment", cls: "mono", fmt: v => h("button", { class: "lnk mono", type: "button", onclick: () => follow(v) }, v) },
              { key: "provider", label: "Provider", fmt: v => OG.providerLink(v) }, { key: "gpu", label: "GPU", fmt: v => OG.shortGpu(v || "") }, { key: "quote", label: "$/GPU·h", num: true, value: r => r.prices.quote, fmt: v => fmt.price(v) },
              { key: "rr", label: "", sort: false, fmt: (v, r) => ["pending_approval", "quote_expired"].includes(r.status) ? h("a", { class: "btn sm pri", href: "/route?rr=" + r.route_request_id }, "Approve on ticket →") : h("a", { class: "btn sm", href: "/deployments/" + r.deployment_id }, "Open →") }],
            rows: vdeps, compact: true, csv: false,
          }) : h("p", { class: "dim" }, "No validation deployments yet."),
          valFollow ? h("div", { class: "ex-vf" }, h("div", { class: "sec-h" }, "Evidence · ", h("span", { class: "mono" }, valFollow)), valEv ? valEv : OG.loading("Reading evidence…")) : null);
      }
      async function follow(dep) {
        valFollow = dep; OG.qs.set({ vdep: dep }); valEv = null; drawValidation();
        try {
          const r = await ctx.api("/v1/admin/validation/" + encodeURIComponent(dep), { nocache: true });
          valEv = h("div", {}, h("ul", { class: "ck-list" }, Object.entries(r.steps || {}).map(([k, v]) => h("li", { class: "ck-" + (v && v.ok ? "green" : "red") }, h("i", { class: "ck-dot" }), h("b", {}, k.replace(/_/g, " ")),
            h("span", { class: "dim" }, evText(Object.fromEntries(Object.entries(v || {}).filter(([kk]) => kk !== "ok"))))))),
            h("div", { class: "bar" }, r.validated ? OG.badge("validated", "good") : OG.badge(`${(r.missing || []).length} step(s) missing`, "warn"),
              h("button", { class: "btn sm", type: "button", disabled: (r.missing || []).length ? true : null, title: (r.missing || []).length ? "every step needs evidence first" : null, onclick: () => markValidated(dep) }, "Re-check & mark validated")));
        } catch (e) { valEv = notYet("GET /v1/admin/validation/{id}", e); }
        drawValidation();
      }
      async function markValidated(dep) {
        const k = "mark:" + dep;
        I[k] = OG.intentFor(I[k], k, {});
        try { await OG.api.intent(I[k], `/v1/admin/validation/${encodeURIComponent(dep)}/mark`, { method: "POST" }); loadFlags(); follow(dep); loadLog(); }
        catch (e) { valEv = h("div", {}, valEv, notYet("POST /v1/admin/validation/{id}/mark", e)); drawValidation(); }
      }
      async function startValidation(provider, gpuSlug) {
        const ok = await OG.dialog({ title: "Validate " + OG.providerName(provider), confirm: "Create validation route",
          body: h("div", {}, h("p", {}, "Picks a 1-GPU on-demand listing at or under $3.00/h, quotes it live and creates a validation deployment ", h("b", {}, "pending your approval"), " (max 30 min, auto-terminated)."),
            h("p", { class: "dim" }, "Nothing launches until you approve the ticket.")) });
        if (!ok) return;
        const body = { provider }; if (gpuSlug) body.gpu = gpuSlug;
        I.val = OG.intentFor(I.val, "validation_start", body);
        try {
          const r = await OG.api.intent(I.val, "/v1/admin/validation/start", { method: "POST", body });
          await loadDeps();
          B.validation.prepend(h("div", { class: "og-ok" }, h("b", {}, "Validation route created: "), h("span", { class: "mono" }, r.deployment_id || ""), " ",
            r.route_request_id ? h("a", { class: "btn sm pri", href: "/route?rr=" + r.route_request_id }, "Approve on ticket →") : null));
          if (r.deployment_id) follow(r.deployment_id);
        } catch (e) { B.validation.prepend(notYet("POST /v1/admin/validation/start", e)); }
      }

      /* ---------- reconciliation runs ---------- */
      async function loadRecon() {
        let runs, last = null, src = "GET /v1/admin/reconcile/runs";
        try { const r = await ctx.api("/v1/admin/reconcile/runs", { params: { limit: 20 }, full: true, nocache: true }); runs = r.data; last = r.meta && r.meta.last_run; }
        catch (e) {
          const ov = await OG.api.soft("/v1/admin/execution/overview", { nocache: true });
          if (!ov || !Array.isArray(ov.reconciliation_runs)) { B.recon.replaceChildren(notYet("GET /v1/admin/reconcile/runs", e)); return; }
          runs = ov.reconciliation_runs; src = "execution overview";
        }
        const lr = last || runs[0];
        B.recon.replaceChildren(
          h("div", { class: "bar" }, lr ? h("span", {}, "Last run ", h("b", {}, fmt.age(lr.started_at) + " ago"), " · ", OG.badge(lr.status, lr.status === "ok" ? "good" : lr.status === "failed" ? "bad" : "warn")) : OG.badge("never run", "bad"),
            h("span", { class: "spacer" }), h("span", { class: "dim mono" }, src), h("button", { class: "btn sm", type: "button", onclick: runRecon }, "Run now")),
          runs.length ? OG.table({
            columns: [
              { key: "id", label: "#", num: true }, { key: "started_at", label: "Started", num: true, fmt: v => fmt.dateTime(v) },
              { key: "trigger", label: "Trigger", cls: "dim" }, { key: "provider", label: "Provider", fmt: v => v || h("span", { class: "dim" }, "all") },
              { key: "status", label: "Status", fmt: v => OG.badge(v, v === "ok" ? "good" : v === "failed" ? "bad" : "warn") },
              { key: "counts", label: "Findings", sort: false, cls: "wrap", fmt: v => v && Object.keys(v).length ? Object.entries(v).map(([k, n]) => h("span", { class: "badge " + (/orphan|fail|unresolved/.test(k) ? "warn" : "") }, k.replace(/_/g, " ") + " " + n)) : h("span", { class: "dim" }, "none") },
              { key: "error", label: "Error", cls: "wrap dim", fmt: v => v || "" },
            ], rows: runs, compact: true, csv: false, sort: { key: "id", dir: "desc" },
            onRow: r => B.recon.append(h("pre", { class: "w4-code ex-find" }, JSON.stringify(r.findings || [], null, 1).slice(0, 4000))),
          }) : h("p", { class: "dim" }, "No reconciliation run recorded. The job runs every few minutes in a deployed server (not with OPENGRID_NO_JOBS)."));
      }
      async function runRecon() {
        B.recon.prepend(OG.loading("Running one reconciliation pass (read-only list calls; may terminate provably-ours orphans)…"));
        try { await ctx.api("/v1/admin/reconcile/run", { method: "POST" }); } catch (e) { B.recon.prepend(notYet("POST /v1/admin/reconcile/run", e)); return; }
        loadRecon(); loadOrphans(); loadDeps();
      }

      /* ---------- account limits ---------- */
      const LIM = [["max_price_per_gpu_hour", "Max $/GPU·h", "num"], ["max_hourly_cost", "Max $/h", "num"], ["max_total_cost", "Max total $", "num"], ["max_gpus", "Max GPUs", "int"],
        ["max_active_deployments", "Max active", "int"], ["monthly_spend_limit", "Monthly $", "num"], ["provider_allowlist", "Provider allowlist", "list"], ["region_allowlist", "Region allowlist", "list"]];
      let limAcct = query.acct || null, partners = null;
      async function loadLimits() {
        if (!partners) partners = (await OG.api.soft("/v1/admin/partners", { nocache: true })) || [];
        const pick = h("select", { class: "field", onchange: e => { limAcct = e.target.value; OG.qs.set({ acct: limAcct }); loadLimits(); } },
          h("option", { value: "" }, "Choose an account…"), partners.map(p => h("option", { value: p.account_id, selected: String(p.account_id) === String(limAcct) ? true : null }, `${p.company} · #${p.account_id}`)));
        const idIn = h("input", { class: "field num", type: "number", min: 1, placeholder: "id", value: limAcct || "" });
        const head = h("div", { class: "bar" }, pick, h("span", { class: "dim" }, "or account id"), idIn, h("button", { class: "btn sm", type: "button", onclick: () => { limAcct = idIn.value; OG.qs.set({ acct: limAcct }); loadLimits(); } }, "Load"));
        if (!limAcct) { B.limits.replaceChildren(head); return; }
        let r;
        try { r = await ctx.api("/v1/admin/execution/limits/" + encodeURIComponent(limAcct), { nocache: true }); } catch (e) { B.limits.replaceChildren(head, OG.error(e)); return; }
        const L = r.limits, U = r.usage, inputs = {};
        const show = v => (v == null ? "" : Array.isArray(v) ? v.join(", ") : v);
        const grid = h("div", { class: "ex-lim" }, LIM.map(([k, label, t]) => {
          const i = h("input", { class: "field" + (t === "list" ? "" : " num"), value: show(L[k]), placeholder: "default" });
          inputs[k] = i;
          return h("label", { class: "og-ask-f" }, h("span", { class: "og-ask-l" }, label, " ", h("span", { class: "badge" + (L.source && L.source[k] === "account" ? " warn" : "") }, (L.source && L.source[k]) || "")), i);
        }));
        const save = async () => {
          const body = {};
          for (const [k, , t] of LIM) {
            const raw = inputs[k].value.trim();
            if (raw === show(L[k]).toString()) continue;
            body[k] = raw === "" ? null : t === "list" ? raw.split(",").map(x => x.trim()).filter(Boolean) : t === "int" ? parseInt(raw, 10) : Number(raw);
          }
          if (!Object.keys(body).length) return;
          const v = await OG.ask({ title: "Change cost guards for account #" + limAcct, confirm: "Save limits", body: h("pre", { class: "w4-code" }, JSON.stringify(body, null, 1)),
            fields: [{ key: "reason", type: "textarea", label: "Reason (required, logged)", required: true, minLength: 3 }] });
          if (!v) return;
          try { await ctx.api("/v1/admin/execution/limits/" + encodeURIComponent(limAcct), { method: "POST", body: Object.assign(body, { reason: v.reason }) }); loadLimits(); loadLog(); }
          catch (e) { B.limits.append(OG.error(e)); }
        };
        B.limits.replaceChildren(head, OG.stats([
          { label: "Active deployments", value: fmt.num(U.active_deployments), sub: "cap " + (L.max_active_deployments ?? "–") },
          { label: "Active GPUs", value: fmt.num(U.active_gpus), sub: "cap " + (L.max_gpus ?? "–") },
          { label: "Month spend", value: OG.money(U.month_spend), sub: `finished ${OG.money(U.month_finished_cost)} + active est. ${OG.money(U.month_active_estimated_cost)}`, kind: "estimated" },
          { label: "Monthly limit", value: L.monthly_spend_limit != null ? OG.money(L.monthly_spend_limit) : null, reason: "none" },
        ]), grid, h("div", { class: "bar" }, h("button", { class: "btn pri", type: "button", onclick: save }, "Save changes…"), h("span", { class: "dim" }, "blank = settings default · checked at quote AND again at launch; exceeding blocks the launch unless an admin overrides with a reason")));
      }

      /* ---------- control log ---------- */
      async function loadLog() {
        try {
          const rows = await ctx.api("/v1/admin/execution/log", { params: { limit: 200 }, nocache: true });
          B.log.replaceChildren(OG.table({
            columns: [
              { key: "at", label: "At", num: true, fmt: v => fmt.dateTime(v) }, { key: "action", label: "Action", cls: "mono", fmt: v => OG.badge(v, /kill|force|override/.test(v) ? "bad" : /mode|flags|approve/.test(v) ? "warn" : "") },
              { key: "target", label: "Target", cls: "mono dim" }, { key: "actor", label: "Actor", cls: "mono" }, { key: "reason", label: "Reason", cls: "wrap" },
              { key: "after", label: "After", cls: "wrap dim mono", sort: false, fmt: v => evText(v) },
            ], rows, compact: true, csv: "opengrid-control-log.csv", sort: { key: "at", dir: "desc" }, limit: 60, empty: "No control changes recorded.",
          }));
        } catch (e) { B.log.replaceChildren(OG.error(e, loadLog)); }
      }

      async function loadMode() {
        try { modeSt = await ctx.api("/v1/admin/execution/mode", { nocache: true }); drawMode(); drawKill(); } catch (e) { B.mode.replaceChildren(OG.error(e, loadMode)); }
      }
      function loadAll() { loadMode(); loadDeps(); loadOrphans(); loadFlags(); loadRecon(); loadLimits(); loadLog(); }
      drawValidation();
      loadAll();
      if (valFollow) follow(valFollow);
      ctx.every(20000, () => { loadDeps(); loadOrphans(); });
    },
  });
})();
