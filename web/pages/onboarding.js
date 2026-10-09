/* /onboarding — the design-partner path to a first real route, from GET /v1/onboarding (steps done, evidence,
   next): 1 account, 2 API key (inline, secret shown once), 3 provider credentials (optional: managed vs BYO),
   4 route preview prefilled from the partner profile, 5 why the provider won (the scoring explanation),
   6 request a supervised deployment (on /route, the one place money moves). Plus the partner profile form
   (GET/POST/PATCH /v1/partners/me). The operator can preview a partner: ?account=<id> reads that partner's
   status from /v1/admin/partners (actions on this page still run as the signed-in principal).
   /admin/partners — every partner with onboarding progress; create one (account + profile + first key in one
   step, key shown once); per-partner deployments and feedback. */
(() => {
  const { h, fmt } = OG;
  const PROFILE = [
    ["company", "Company", "text"], ["technical_contact_name", "Technical contact", "text"], ["technical_contact_email", "Contact email", "text"],
    ["preferred_gpus", "Preferred GPUs (slugs, comma-separated)", "list"], ["regions", "Regions (US, Europe, …)", "list"], ["workload_type", "Workload type", "text"],
    ["normal_provider", "Normal provider", "text"], ["normal_price_per_gpu_hour", "Normal $/GPU·h (what you pay today)", "num"], ["max_price_per_gpu_hour", "Max $/GPU·h", "num"],
    ["expected_gpu_count", "Expected GPU count", "int"], ["expected_duration_hours", "Expected duration (h)", "num"],
    ["latency_requirements", "Latency requirements", "area"], ["storage_requirements", "Storage requirements", "area"], ["network_requirements", "Network requirements", "area"],
  ];
  const show = v => (v == null ? "" : Array.isArray(v) ? v.join(", ") : String(v));
  function profileForm(profile, onSave, opts) {
    opts = opts || {};
    const inputs = {};
    const grid = h("div", { class: "ob-grid" }, PROFILE.map(([k, label, t]) => {
      const i = t === "area" ? h("textarea", { class: "field og-ask-ta", rows: 2 }) : h("input", { class: "field" + (t === "num" || t === "int" ? " num" : ""), type: t === "num" || t === "int" ? "number" : "text", step: t === "num" ? "0.01" : null, min: t === "num" || t === "int" ? 0 : null });
      i.value = show(profile && profile[k]);
      inputs[k] = i;
      return h("label", { class: "og-ask-f" + (t === "area" ? " wide" : "") }, h("span", { class: "og-ask-l" }, label), i);
    }));
    const msg = h("span", { class: "dim" });
    const read = () => {
      const out = {};
      for (const [k, , t] of PROFILE) {
        const raw = inputs[k].value.trim();
        if (raw === show(profile && profile[k])) continue;
        out[k] = raw === "" ? null : t === "list" ? raw.split(",").map(x => x.trim()).filter(Boolean) : t === "int" ? parseInt(raw, 10) : t === "num" ? Number(raw) : raw;
      }
      return out;
    };
    const extra = opts.extra || [];
    return h("form", { class: "ob-form", onsubmit: async e => {
      e.preventDefault();
      const body = read();
      for (const x of extra) Object.assign(body, x.read());
      if (!Object.keys(body).length && profile) { msg.textContent = "nothing changed"; return; }
      msg.textContent = "saving…";
      try { await onSave(body); msg.textContent = "saved"; msg.className = "up"; } catch (err) { msg.replaceChildren(OG.error(err)); }
    } }, grid, extra.map(x => x.el), h("div", { class: "bar" }, h("button", { class: "btn pri", type: "submit" }, opts.submit || (profile ? "Save profile" : "Create profile")), msg),
      h("p", { class: "note" }, "Normal price is partner-reported, never shown as an OpenGrid observation. Used to prefill routes and to compare savings honestly."));
  }
  function keyReveal(k) {
    return h("div", { class: "ob-key" }, h("div", { class: "ky-warn" }, h("b", {}, "Copy this key now. "), "OpenGrid stores only a hash and cannot show it again."),
      h("div", { class: "bar" }, h("code", { class: "mono ob-secret" }, k.secret), OG.copy(k.secret, { label: "Copy key" })),
      h("div", { class: "dim" }, "prefix ", h("span", { class: "mono" }, k.prefix || ""), " · scopes ", h("span", { class: "mono" }, (k.scopes || []).join(" "))));
  }

  /* =============================== /onboarding =============================== */
  OG.page("/onboarding", {
    title: "Onboarding",
    async mount(el, params, query, ctx) {
      const root = h("div", { class: "pg-ob" });
      el.append(root, OG.loading("Loading onboarding…"));
      const me = await OG.me();
      if (!ctx.alive()) return;
      const admin = OG.isAdmin(me);
      let status = null, profile = null, partners = null, previewing = null;
      async function load() {
        if (admin && query.account) {
          partners = partners || (await OG.api.soft("/v1/admin/partners", { nocache: true })) || [];
          const p = partners.find(x => String(x.account_id) === String(query.account));
          if (p) { previewing = p; status = p.onboarding; profile = p; return; }
        }
        previewing = null;
        try { status = await ctx.api("/v1/onboarding", { nocache: true }); } catch (e) { root.replaceChildren(OG.head("Onboarding"), OG.error(e, () => load().then(draw))); throw e; }
        profile = status.profile || null;
        if (admin && !partners) partners = (await OG.api.soft("/v1/admin/partners", { nocache: true })) || [];
      }
      const done = k => !!((status && status.steps) || []).find(s => s.step === k && s.done);
      const stepOf = k => ((status && status.steps) || []).find(s => s.step === k) || {};

      function stepBox(n, key, title, body, opt) {
        const s = stepOf(key), ok = key ? s.done : false, next = status && status.next_step && status.next_step.step === key;
        return h("li", { class: "ob-step" + (ok ? " done" : "") + (next ? " next" : "") },
          h("div", { class: "ob-n" }, ok ? "✓" : n),
          h("div", { class: "ob-b" },
            h("div", { class: "ob-t" }, h("b", {}, title), opt ? OG.badge("optional") : null, ok ? OG.badge("done", "good") : next ? OG.badge("next", "warn") : null,
              s.evidence ? h("span", { class: "dim mono ob-ev", title: s.evidence }, String(s.evidence).replace(/\b(\d{4}-\d\d-\d\dT\d\d:\d\d)(:\d\d(\.\d+)?)?(\+00:00|Z)?/g, (m, a) => fmt.dateTime(a + ":00Z"))) : null),
            body));
      }

      // step 2: create a key inline
      const keyBox = h("div");
      function keyForm() {
        if (previewing) return h("p", { class: "note" }, "A partner's first key is created with the partner (", h("a", { class: "lnk", href: "/admin/partners" }, "/admin/partners"), ") and shown once there.");
        const name = h("input", { class: "field", value: "first-route", "aria-label": "Key name" });
        const exec = h("input", { type: "checkbox" });
        return h("div", {}, h("div", { class: "bar" }, name, h("label", { class: "rt-chk" }, exec, "route:execute (request routes; launches still need OpenGrid approval)"),
          h("button", { class: "btn pri", type: "button", onclick: async () => {
            const scopes = ["data:read", "route:preview", "deployments:read", "deployments:write"].concat(exec.checked ? ["route:execute"] : []);
            try { const k = await ctx.api("/v1/keys", { method: "POST", body: { name: name.value.trim() || "first-route", scopes } }); keyBox.replaceChildren(keyReveal(k)); OG.api.soft("/v1/onboarding", { nocache: true }).then(s => { if (s && !previewing) { status = s; } }); }
            catch (e) { keyBox.replaceChildren(OG.error(e)); }
          } }, "Create API key")), keyBox);
      }

      // steps 4-5: a preview prefilled from the profile, and why the winner won
      const pvBox = h("div"), whyBox = h("div");
      let pvBody = null;
      function prefill() {
        const p = profile || {};
        const b = { gpu: (p.preferred_gpus || [])[0] || "h100-80gb-sxm5", count: Math.min(64, p.expected_gpu_count || 1), mode: "BALANCED" };
        if ((p.regions || [])[0]) b.region = p.regions[0];
        if (p.max_price_per_gpu_hour) b.max_price_per_gpu_hour = p.max_price_per_gpu_hour;
        if (p.expected_duration_hours) b.duration_hours = p.expected_duration_hours;
        return b;
      }
      const routeHref = b => "/route" + OG.qs.stringify({ gpu: b.gpu, count: b.count > 1 ? b.count : null, region: b.region, max: b.max_price_per_gpu_hour, dur: b.duration_hours });
      async function runPreview() {
        pvBody = prefill();
        pvBox.replaceChildren(OG.loading("Ranking every eligible listing…"));
        try {
          const d = await ctx.api("/v1/route/preview", { method: "POST", body: pvBody });
          const c = d.selected;
          if (!c) { pvBox.replaceChildren(OG.insufficient(d.reason || "no eligible listing", "No route")); whyBox.replaceChildren(); return; }
          const q = d.quote || {}, m = d.market || {}, sv = q.savings_vs_median;
          pvBox.replaceChildren(h("div", { class: "ob-pv" },
            h("span", { class: "eyebrow" }, "Winner"), OG.providerLink(c.provider), h("span", { class: "mono" }, `${c.gpu_count}× ${OG.shortGpu(c.gpu)}`),
            h("span", { class: "mono" }, fmt.price(c.price_per_gpu_hour), "/GPU·h "), OG.conceptBadge("observed"),
            c.provisionable ? OG.badge("OpenGrid can provision", "good") : OG.badge("market data only", "bad"),
            h("span", { class: "dim" }, m.median != null ? `market median ${fmt.price(m.median)} (${m.providers} providers)` : "no market median"),
            sv ? h("span", { class: sv.pct >= 0 ? "up" : "down" }, `${fmt.num(Math.abs(sv.pct) * 100, 1)}% ${sv.pct >= 0 ? "below" : "above"} median`) : null,
            h("a", { class: "lnk mono", href: "/v1/route/" + d.route_request_id, target: "_blank", rel: "external noopener" }, d.route_request_id, " ↗")));
          const factors = Object.entries(c.factors || {}).filter(([, x]) => x && x.weight).sort((a, b) => (b[1].contribution || 0) - (a[1].contribution || 0));
          whyBox.replaceChildren(h("ul", { class: "rt-why" }, factors.map(([k, x]) => h("li", { class: x.imputed ? "imp" : null }, h("b", {}, k.replace(/_/g, " ")), " ",
            x.note || "", x.imputed ? OG.badge("imputed 0.5", "warn") : null, h("span", { class: "rt-contrib mono" }, `${(x.weight * 100).toFixed(0)}% × ${x.value != null ? x.value.toFixed(2) : "–"} = +${(x.contribution || 0).toFixed(3)}`)))),
            (d.alternatives || []).length ? h("p", { class: "note" }, "Runner-up: ", OG.providerName(d.alternatives[0].provider), " at ", fmt.price(d.alternatives[0].price_per_gpu_hour), "/GPU·h, score ", (d.alternatives[0].score || 0).toFixed(3), " vs ", c.score.toFixed(3), ".") : null,
            h("p", { class: "note" }, "Reliability and performance carry no weight until OpenGrid has real execution records; nothing is invented. ", h("a", { class: "lnk", href: routeHref(pvBody) }, "Full breakdown on /route →")));
        } catch (e) { pvBox.replaceChildren(OG.error(e, runPreview)); }
      }

      function draw() {
        const b = prefill();
        const pickP = admin && partners && partners.length ? h("select", { class: "field", onchange: e => OG.go("/onboarding" + (e.target.value ? "?account=" + e.target.value : "")) },
          h("option", { value: "" }, "Signed-in account"), partners.map(p => h("option", { value: p.account_id, selected: previewing && previewing.account_id === p.account_id ? true : null }, "Preview: " + p.company))) : null;
        root.replaceChildren(
          OG.head("Onboarding", previewing ? `Previewing ${previewing.company} (account #${previewing.account_id}) · actions on this page run as you, not as the partner` : "From account to a first supervised route",
            pickP, h("span", { class: "mono dim" }, status.progress || ""), status.complete ? OG.badge("complete", "good") : null),
          h("ol", { class: "ob-steps" },
            stepBox(1, "account_created", "Account created", h("p", { class: "dim" }, "Your OpenGrid account holds keys, limits, deployments and invoices.")),
            stepBox(2, "key_created", "Create an API key", keyForm()),
            stepBox(3, "credentials_connected", "Connect provider credentials", h("div", {},
              h("p", {}, h("b", {}, "Managed (default): "), "OpenGrid launches on its own provider accounts and bills you the provider cost plus the published fee. ",
                h("b", {}, "BYO: "), "add your own provider API key; launches run on your account and the provider bills you directly. Keys are encrypted; a key that cannot be decrypted fails closed (never a fallback to OpenGrid's)."),
              h("a", { class: "btn", href: "/keys" }, "Manage credentials →")), true),
            stepBox(4, "first_preview", "Run a route preview", h("div", {},
              h("p", { class: "dim" }, "Prefilled from the profile: ", h("span", { class: "mono" }, `${b.count}× ${b.gpu}${b.region ? " · " + b.region : ""}${b.max_price_per_gpu_hour ? " · ≤ $" + b.max_price_per_gpu_hour : ""}${b.duration_hours ? " · " + b.duration_hours + " h" : ""}`)),
              h("div", { class: "bar" }, h("button", { class: "btn pri", type: "button", onclick: runPreview }, "Run preview"), h("span", { class: "dim" }, "free · never calls a provider")), pvBox)),
            stepBox(5, null, "Understand why the provider won", whyBox.childNodes.length ? whyBox : h("div", {}, whyBox, h("p", { class: "dim" }, "Run the preview: each factor's weight × value is shown, imputed factors flagged."))),
            stepBox(6, "first_supervised_deployment", "Request a supervised deployment", h("div", {},
              h("p", {}, "On /route, ", h("b", {}, "Request route"), " issues a quote. An OpenGrid admin reviews provider, price, fees and limits, then approves; only then is ONE instance launched (with an auto-terminate cap)."),
              h("a", { class: "btn pri", href: routeHref(b) }, "Open the order ticket →"))),
            stepBox(7, "feedback_given", "Tell us how it went", h("p", { class: "dim" }, "After termination, the deployment page asks five questions: provider choice, price, setup, next workload, what broke."))),
          OG.section(previewing ? `Design-partner profile · ${previewing.company}` : "Design-partner profile",
            profileForm(profile, async body => {
              if (previewing) await ctx.api("/v1/admin/partners/" + previewing.account_id, { method: "PATCH", body });
              else if (profile) await ctx.api("/v1/partners/me", { method: "PATCH", body });
              else await ctx.api("/v1/partners/me", { method: "POST", body });
              partners = null; await load(); draw();
            })));
        if (pvBody) runPreview();
      }
      try { await load(); } catch (e) { return; }
      if (ctx.alive()) draw();
    },
  });

  /* =============================== /admin/partners =============================== */
  OG.page("/admin/partners", {
    title: "Design partners",
    nav: "admin-partners",
    async mount(el, params, query, ctx) {
      const root = h("div", { class: "pg-ob" });
      el.append(root);
      const me = await OG.me();
      if (!ctx.alive()) return;
      if (!OG.isAdmin(me)) { root.append(OG.head("Design partners"), OG.error({ status: 403, message: "admin scope required" })); return; }
      const listBox = h("div", {}, OG.loading()), detail = h("div"), created = h("div");
      let sel = query.account || null, rows = [];
      root.append(OG.head("Design partners", "Accounts onboarding to a first real route",
        h("button", { class: "btn pri", type: "button", onclick: () => { formBox.hidden = !formBox.hidden; } }, "New partner…")), created);
      const grant = h("input", { type: "checkbox" });
      const formBox = h("div", { class: "box box-p", hidden: true },
        h("div", { class: "sec-h" }, "Create partner: account + profile + first key, in one step"),
        profileForm(null, async body => {
          if (!body.company) throw new Error("company is required");
          const r = await ctx.api("/v1/admin/partners", { method: "POST", body });
          formBox.hidden = true;
          created.replaceChildren(h("div", { class: "og-ok" }, h("b", {}, `${r.account.name} created (account #${r.account.id}). `), r.api_key ? keyReveal(r.api_key) : "No key created."));
          sel = r.account.id; load();
        }, { submit: "Create partner", extra: [{ el: h("label", { class: "rt-chk" }, grant, "Grant route:execute on the first key (every launch still needs OpenGrid approval in SUPERVISED)"), read: () => ({ grant_execute: grant.checked }) }] }));
      root.append(formBox, listBox, detail);

      async function load() {
        try { rows = await ctx.api("/v1/admin/partners", { nocache: true }); } catch (e) { listBox.replaceChildren(OG.error(e, load)); return; }
        listBox.replaceChildren(OG.table({
          columns: [
            { key: "company", label: "Company", fmt: (v, r) => h("b", {}, v) },
            { key: "account_id", label: "Acct", num: true },
            { key: "status", label: "Status", fmt: v => OG.badge(v || "–", v === "active" ? "good" : "warn") },
            { key: "technical_contact_name", label: "Contact", cls: "dim", fmt: (v, r) => [v, r.technical_contact_email].filter(Boolean).join(" · ") },
            { key: "progress", label: "Onboarding", value: r => r.onboarding && r.onboarding.progress, fmt: (v, r) => h("span", {}, r.onboarding && r.onboarding.complete ? OG.badge("complete", "good") : v || "–") },
            { key: "next", label: "Next step", cls: "dim", value: r => r.onboarding && r.onboarding.next_step ? r.onboarding.next_step.label : "" },
            { key: "preferred_gpus", label: "GPUs", cls: "mono dim", fmt: v => show(v) },
            { key: "regions", label: "Regions", cls: "dim", fmt: v => show(v) },
            { key: "normal_price_per_gpu_hour", label: "Normal $/GPU·h", num: true, fmt: (v, r) => v != null ? h("span", { title: r.normal_price_kind || "partner-reported" }, fmt.price(v), " ", OG.badge("reported")) : "–" },
            { key: "max_price_per_gpu_hour", label: "Max $/GPU·h", num: true, fmt: v => v != null ? fmt.price(v) : "–" },
            { key: "open", label: "", sort: false, fmt: (v, r) => h("a", { class: "btn sm", href: "/onboarding?account=" + r.account_id }, "Onboarding view") },
          ], rows, compact: true, csv: "opengrid-partners.csv", rowKey: r => r.account_id, onRow: r => { sel = r.account_id; OG.qs.set({ account: sel }); drawDetail(); },
          empty: "No design partners yet. Create one: it returns the first API key once.",
        }));
        drawDetail();
      }
      async function drawDetail() {
        const p = rows.find(r => String(r.account_id) === String(sel));
        if (!p) { detail.replaceChildren(); return; }
        detail.replaceChildren(OG.loading("Loading " + p.company + "…"));
        const [deps, fb] = await Promise.all([OG.api.soft("/v1/admin/deployments", { params: { limit: 2000 }, nocache: true }), OG.api.soft("/v1/admin/feedback", { params: { account_id: p.account_id }, nocache: true })]);
        if (!ctx.alive()) return;
        const mine = (deps || []).filter(d => d.account_id === p.account_id);
        const items = (fb && fb.items) || [];
        const yn = v => v === true ? OG.badge("yes", "good") : v === false ? OG.badge("no", "bad") : h("span", { class: "dim" }, "–");
        detail.replaceChildren(OG.section(`${p.company} · account #${p.account_id}`,
          h("div", { class: "bar" }, h("a", { class: "btn", href: "/admin/execution?acct=" + p.account_id }, "Cost guards →"), h("a", { class: "btn", href: "/onboarding?account=" + p.account_id }, "Onboarding view →")),
          h("div", { class: "cols-2" },
            OG.section(`Deployments · ${mine.length}`, OG.table({
              columns: [{ key: "status", label: "State", fmt: v => OG.stateBadge(v) }, { key: "deployment_id", label: "Deployment", cls: "mono", href: r => "/deployments/" + r.deployment_id },
                { key: "provider", label: "Provider", fmt: v => v ? OG.providerLink(v) : "–" }, { key: "gpu", label: "GPU", fmt: (v, r) => `${r.gpu_count}× ${OG.shortGpu(v || "")}` },
                { key: "quote", label: "Quote", num: true, value: r => r.prices.quote, fmt: v => fmt.price(v) }, { key: "created_at", label: "Created", num: true, fmt: v => fmt.dateTime(v) }],
              rows: mine, compact: true, csv: false, sort: { key: "created_at", dir: "desc" }, empty: "No deployments yet." })),
            OG.section(`Feedback · ${items.length}`, items.length ? OG.table({
              columns: [{ key: "deployment_id", label: "Deployment", cls: "mono", href: r => "/deployments/" + r.deployment_id },
                { key: "would_have_chosen_provider", label: "Own pick?", fmt: yn }, { key: "price_better", label: "Price better?", fmt: yn },
                { key: "setup_easier", label: "Easier?", fmt: yn }, { key: "would_route_next", label: "Route next?", fmt: yn },
                { key: "what_broke", label: "What broke", cls: "wrap" }, { key: "notes", label: "Notes", cls: "wrap dim" }],
              rows: items, compact: true, csv: false }) : h("p", { class: "dim" }, "No feedback yet (asked after each terminated deployment).")))));
      }
      load();
    },
  });
})();
