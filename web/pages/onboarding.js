/* /onboarding: the design-partner path to a first real route.
   In a browser the only signed-in principal is the operator (site login); partners act with their API key. So:
     signed out  a static explanation of how onboarding works today (keys are issued by the OpenGrid team; no
                 self-service sign-up) with the access request link. No private call, no 401 stack.
     operator    the operator account's own checklist, or ?account=<id> to preview a partner's progress (read from
                 /v1/admin/partners; actions on this page still run as the operator, and say so).
   Steps come from GET /v1/onboarding (computed from evidence, never edited): account, key, provider credentials
   (optional), first route preview (with why the winner won), first supervised deployment, feedback. Keys are
   created on /keys (one place), not here. The preview runs only when its button is pressed; saving the profile
   never re-runs it (the old result is marked out of date instead).
   Profile form (GET/POST/PATCH /v1/partners/me, PATCH /v1/admin/partners/<id>): GPUs are typed as names
   ("H100 SXM5") and stored as canonical slugs.
   /admin/partners: every partner with onboarding status; create one (account + profile + first key in one
   step, key shown once); per-partner deployments and feedback. */
(() => {
  const { h, fmt } = OG;
  const PROFILE = [
    ["company", "Company (required)", "text"], ["technical_contact_name", "Technical contact", "text"], ["technical_contact_email", "Contact email", "text"],
    ["preferred_gpus", "Preferred GPUs (comma-separated, e.g. H100 SXM5, L40S)", "gpus"], ["regions", "Regions (US, Europe, …)", "list"], ["workload_type", "Workload type (training, fine-tuning, inference…)", "text"],
    ["normal_provider", "Provider you use today", "text"], ["normal_price_per_gpu_hour", "What you pay today ($/GPU·h)", "num"], ["max_price_per_gpu_hour", "Max price ($/GPU·h)", "num"],
    ["expected_gpu_count", "Expected GPU count", "int"], ["expected_duration_hours", "Expected duration (hours)", "num"],
    ["latency_requirements", "Latency requirements", "area"], ["storage_requirements", "Storage requirements", "area"], ["network_requirements", "Network requirements", "area"],
  ];
  const show = v => (v == null ? "" : Array.isArray(v) ? v.join(", ") : String(v));
  const norm = s => String(s || "").toLowerCase().replace(/^(nvidia|amd|intel)\s+/, "").replace(/[^a-z0-9]+/g, "");
  const noMem = s => norm(String(s || "").replace(/\b\d+\s*gb\b/ig, ""));
  // pure: typed GPU names -> {slugs, unknown, ambiguous}. A name matches a GPU's slug, full name or short name
  // (case, vendor prefix and punctuation ignored); without the memory size ("H100 PCIe") it is accepted only when
  // exactly one GPU fits. Nothing fuzzier: a typo is reported, never mapped to another GPU.
  function resolveGpuNames(text, gpus) {
    const slugs = [], unknown = [], ambiguous = [];
    for (const raw of String(text || "").split(",").map(x => x.trim()).filter(Boolean)) {
      const n = norm(raw), m = noMem(raw);
      let g = (gpus || []).find(x => [x.slug, x.name, x.short].some(v => v && norm(v) === n));
      if (!g) {
        const hits = (gpus || []).filter(x => [x.name, x.short].some(v => v && noMem(v) === m));
        if (hits.length === 1) g = hits[0];
        else if (hits.length > 1) { ambiguous.push(raw + " (" + hits.map(x => x.short || x.name).join(" or ") + ")"); continue; }
      }
      if (g) { if (!slugs.includes(g.slug)) slugs.push(g.slug); } else unknown.push(raw);
    }
    return { slugs, unknown, ambiguous };
  }
  const gpuLabel = (sl, gpus) => { const g = (gpus || []).find(x => x.slug === sl); return g ? (g.short || g.name) : sl; };
  // pure: the onboarding steps as this page shows them ([server step, title, optional]); "why the provider won"
  // is part of the preview step (the server has no separate step for it, so it could never show as done)
  const STEPS = [
    ["account_created", "Account created", false],
    ["key_created", "API key", false],
    ["credentials_connected", "Provider credentials", true],
    ["first_preview", "First route preview", false],
    ["first_supervised_deployment", "First supervised deployment", false],
    ["feedback_given", "Feedback on the deployment", false],
  ];
  OG.onboarding = { resolveGpuNames, gpuLabel, STEPS };
  if (typeof OG.page !== "function" || typeof document === "undefined") return;

  function profileForm(profile, onSave, opts) {
    opts = opts || {};
    const inputs = {};
    let gpus = [];
    const listId = "ob-gpu-names-" + Math.random().toString(36).slice(2, 8);
    const gpuList = h("datalist", { id: listId });
    const grid = h("div", { class: "ob-grid" }, PROFILE.map(([k, label, t]) => {
      const num = t === "num" || t === "int";
      const i = t === "area" ? h("textarea", { class: "field og-ask-ta", rows: 2 }) : h("input", { class: "field" + (num ? " num" : ""), type: num ? "number" : "text", step: t === "num" ? "0.01" : null, min: num ? 0 : null, list: t === "gpus" ? listId : null });
      i.value = show(profile && profile[k]);
      inputs[k] = i;
      return h("label", { class: "og-ask-f" + (t === "area" ? " wide" : "") }, h("span", { class: "og-ask-l" }, label), i);
    }));
    const shownGpus = () => ((profile && profile.preferred_gpus) || []).map(sl => gpuLabel(sl, gpus)).join(", ");
    OG.data.gpus().then(list => {
      gpus = list || [];
      gpuList.replaceChildren(...gpus.filter(g => g.live).sort((a, b) => b.weight - a.weight).map(g => h("option", { value: g.short || g.name })));
      if (inputs.preferred_gpus.value === show(profile && profile.preferred_gpus)) inputs.preferred_gpus.value = shownGpus();
    }).catch(() => {});
    const msg = h("span", { class: "dim" }), errBox = h("div");
    const read = () => {
      const out = {};
      for (const [k, , t] of PROFILE) {
        const raw = inputs[k].value.trim();
        if (t === "gpus") {
          if (raw === shownGpus() || raw === show(profile && profile[k])) continue;
          const r = resolveGpuNames(raw, gpus);
          if (r.unknown.length) throw new Error("unknown GPU: " + r.unknown.join(", ") + " (pick a name from the suggestions)");
          if (r.ambiguous.length) throw new Error("more than one GPU matches: " + r.ambiguous.join("; "));
          out[k] = r.slugs.length ? r.slugs : null;
          continue;
        }
        if (raw === show(profile && profile[k])) continue;
        out[k] = raw === "" ? null : t === "list" ? raw.split(",").map(x => x.trim()).filter(Boolean) : t === "int" ? parseInt(raw, 10) : t === "num" ? Number(raw) : raw;
      }
      return out;
    };
    const extra = opts.extra || [];
    return h("form", { class: "ob-form", onsubmit: async e => {
      e.preventDefault();
      msg.className = "dim"; msg.textContent = ""; errBox.replaceChildren();
      let body;
      try { body = read(); } catch (err) { errBox.replaceChildren(OG.dx.formError(err)); return; }
      for (const x of extra) Object.assign(body, x.read());
      if (!Object.keys(body).length && profile) { msg.textContent = "nothing changed"; return; }
      msg.textContent = "saving…";
      try { await onSave(body); msg.textContent = "saved"; msg.className = "up"; } catch (err) { msg.textContent = ""; errBox.replaceChildren(OG.dx.formError(err)); }
    } }, gpuList, grid, extra.map(x => x.el), h("div", { class: "bar" }, h("button", { class: "btn pri", type: "submit" }, opts.submit || (profile ? "Save profile" : "Create profile")), msg), errBox,
      h("p", { class: "note" }, "What you pay today is partner-reported, never shown as an OpenGrid observation."));
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
      el.append(root);
      root.append(OG.loading("Loading onboarding…"));
      const me = await OG.me();
      if (!ctx.alive()) return;
      const admin = OG.isAdmin(me);
      const D = OG.dx;

      /* ----- signed out: how onboarding works, no private calls ----- */
      if (!me) {
        root.replaceChildren(OG.head("Onboarding", "From a first look to a first supervised GPU run, with the OpenGrid team"),
          D.accessCall(null),
          h("ol", { class: "ob-steps" }, [
            ["Explore with no key", "Prices, project analysis and GPU recommendations.", h("a", { class: "lnk", href: "/developers#quickstart" }, "Developer quickstart →")],
            ["Get an account and a key", "The OpenGrid team creates them for design partners.", null],
            ["Preview routes", "See which listing OpenGrid would pick for your workload, and why. Free.", null],
            ["A first supervised run", "Not available yet: no real GPU runs today. When it opens, OpenGrid approves each launch.", null],
          ].map(([t, d, a], i) => h("li", { class: "ob-step" }, h("div", { class: "ob-n" }, String(i + 1)), h("div", { class: "ob-b" }, h("div", { class: "ob-t" }, h("b", {}, t)), h("p", { class: "dim ob-d" }, d), a)))));
        return;
      }

      let status = null, profile = null, partners = null, previewing = null, missing = null;
      async function load() {
        if (admin && query.account) {
          partners = partners || (await OG.api.soft("/v1/admin/partners", { nocache: true })) || [];
          const pp = partners.find(x => String(x.account_id) === String(query.account));
          if (pp) { previewing = pp; status = pp.onboarding; profile = pp; missing = null; return; }
          missing = query.account;
        }
        previewing = null;
        try { status = await ctx.api("/v1/onboarding", { nocache: true }); } catch (e) { root.replaceChildren(OG.head("Onboarding"), OG.error(e, () => load().then(draw))); throw e; }
        profile = status.profile || null;
        if (admin && !partners) partners = (await OG.api.soft("/v1/admin/partners", { nocache: true })) || [];
      }
      const stepOf = k => ((status && status.steps) || []).find(s => s.step === k) || {};

      function stepBox(n, key, title, body, opt) {
        const s = stepOf(key), ok = !!s.done, next = status && status.next_step && status.next_step.step === key;
        return h("li", { class: "ob-step" + (ok ? " done" : "") + (next ? " next" : "") },
          h("div", { class: "ob-n" }, ok ? "✓" : n),
          h("div", { class: "ob-b" },
            h("div", { class: "ob-t" }, h("b", {}, title), opt ? OG.badge("optional") : null, ok ? OG.badge("done", "good") : next ? OG.badge("next", "warn") : null,
              s.evidence ? h("span", { class: "dim mono ob-ev", title: s.evidence }, String(s.evidence).replace(/\b(\d{4}-\d\d-\d\dT\d\d:\d\d)(:\d\d(\.\d+)?)?(\+00:00|Z)?/g, (m, a) => fmt.dateTime(a + ":00Z"))) : null),
            body));
      }

      // the route preview (step 4): prefilled from the profile, run only on click
      const pvBox = h("div"), whyBox = h("div");
      let lastBody = null;
      function prefill() {
        const pf = profile || {};
        const b = { gpu: (pf.preferred_gpus || [])[0] || "h100-80gb-sxm5", count: Math.min(64, pf.expected_gpu_count || 1), mode: "BALANCED" };
        if ((pf.regions || [])[0]) b.region = pf.regions[0];
        if (pf.max_price_per_gpu_hour) b.max_price_per_gpu_hour = pf.max_price_per_gpu_hour;
        if (pf.expected_duration_hours) b.duration_hours = pf.expected_duration_hours;
        return b;
      }
      const routeHref = b => "/route" + OG.qs.stringify({ gpu: b.gpu, count: b.count > 1 ? b.count : null, region: b.region, max: b.max_price_per_gpu_hour, dur: b.duration_hours });
      const describe = b => `${b.count}× ${OG.data.gpuName(b.gpu) ? OG.shortGpu(OG.data.gpuName(b.gpu)) : b.gpu}${b.region ? " · " + b.region : ""}${b.max_price_per_gpu_hour ? " · ≤ $" + b.max_price_per_gpu_hour + "/GPU·h" : ""}${b.duration_hours ? " · " + b.duration_hours + " h" : ""}`;
      async function runPreview() {
        const body = prefill();
        lastBody = body;
        pvBox.replaceChildren(OG.loading("Ranking every eligible listing…"));
        whyBox.replaceChildren();
        try {
          const d = await ctx.api("/v1/route/preview", { method: "POST", body });
          const cc = d.selected;
          if (!cc) { pvBox.replaceChildren(OG.insufficient(d.reason || "no eligible listing", "No route")); return; }
          const q = d.quote || {}, m = d.market || {}, sv = q.savings_vs_median;
          pvBox.replaceChildren(h("div", { class: "ob-pv" },
            h("span", { class: "eyebrow" }, "Selected"), OG.providerLink(cc.provider), h("span", { class: "mono" }, `${cc.gpu_count}× ${OG.shortGpu(cc.gpu)}`),
            h("span", { class: "mono" }, fmt.price(cc.price_per_gpu_hour), "/GPU·h "), OG.conceptBadge("observed"),
            cc.provisionable ? OG.badge("OpenGrid can provision", "good") : OG.badge("market data only", "bad"),
            h("span", { class: "dim" }, m.median != null ? `market median ${fmt.price(m.median)} (${m.providers} providers)` : "no market median"),
            sv ? h("span", { class: sv.pct >= 0 ? "up" : "down" }, `${fmt.num(Math.abs(sv.pct) * 100, 1)}% ${sv.pct >= 0 ? "below" : "above"} median`) : null,
            h("a", { class: "lnk mono", href: "/v1/route/" + d.route_request_id, target: "_blank", rel: "external noopener" }, d.route_request_id, " ↗")));
          const factors = Object.entries(cc.factors || {}).filter(([, x]) => x && x.weight).sort((a, b) => (b[1].contribution || 0) - (a[1].contribution || 0));
          whyBox.replaceChildren(h("div", { class: "dx-ob-why" }, h("div", { class: "lbl" }, "Why this provider won"),
            h("ul", { class: "rt-why" }, factors.map(([k, x]) => h("li", { class: x.imputed ? "imp" : null }, h("b", {}, k.replace(/_/g, " ")), " ",
              x.note || "", x.imputed ? OG.badge("imputed 0.5", "warn") : null, h("span", { class: "rt-contrib mono" }, `${(x.weight * 100).toFixed(0)}% × ${x.value != null ? x.value.toFixed(2) : "-"} = +${(x.contribution || 0).toFixed(3)}`)))),
            (d.alternatives || []).length ? h("p", { class: "note" }, "Runner-up: ", OG.providerName(d.alternatives[0].provider), " at ", fmt.price(d.alternatives[0].price_per_gpu_hour), "/GPU·h, score ", (d.alternatives[0].score || 0).toFixed(3), " vs ", cc.score.toFixed(3), ".") : null,
            h("p", { class: "note" }, "Reliability and performance carry no weight until OpenGrid has real execution records; nothing is invented. ", h("a", { class: "lnk", href: routeHref(body) }, "Full breakdown on Route →"))));
          if (!previewing) OG.api.soft("/v1/onboarding", { nocache: true }).then(s => { if (s && !previewing && ctx.alive()) { status = s; profile = s.profile || profile; draw(); } });
        } catch (e) { pvBox.replaceChildren(OG.error(e, runPreview)); }
      }

      function draw() {
        const b = prefill();
        const stale = lastBody && JSON.stringify(lastBody) !== JSON.stringify(b);
        const pickP = admin && partners && partners.length ? h("select", { class: "field", "aria-label": "Whose onboarding", onchange: e => OG.go("/onboarding" + (e.target.value ? "?account=" + e.target.value : "")) },
          h("option", { value: "" }, "Operator account"), partners.map(pp => h("option", { value: pp.account_id, selected: previewing && previewing.account_id === pp.account_id ? true : null }, "Partner: " + pp.company))) : null;
        root.replaceChildren(
          OG.head("Onboarding", previewing ? `Progress of ${previewing.company} (account #${previewing.account_id})` : "From an account to a first supervised route",
            pickP, h("span", { class: "mono dim" }, status.progress || ""), status.complete ? OG.badge("complete", "good") : null),
          missing ? D.call("warn", h("b", {}, "No partner with account #" + missing + ". "), "Showing the operator account's own checklist.") : null,
          previewing ? D.call("warn", h("b", {}, "Previewing a partner. "), "Buttons here still act as you, the operator: a preview you run is recorded on the operator account.")
            : admin ? D.call("info", h("b", {}, "The operator account's own checklist. "), "Partners act with their API key. Pick one above, or onboard a new one on ", h("a", { class: "lnk", href: "/admin/partners" }, "Design partners"), ".") : null,
          h("ol", { class: "ob-steps" },
            stepBox(1, "account_created", "Account created", h("p", { class: "dim" }, previewing ? "Created with the partner on Design partners." : "The operator account exists from the first sign-in.")),
            stepBox(2, "key_created", "API key", previewing
              ? h("p", { class: "dim" }, "Created with the partner on ", h("a", { class: "lnk", href: "/admin/partners" }, "Design partners"), ".")
              : h("a", { class: "btn", href: "/keys" }, "API keys →")),
            stepBox(3, "credentials_connected", "Provider credentials", h("div", {},
              h("p", { class: "dim" }, "Managed by default: OpenGrid launches on its own provider accounts. Or bring your own key, and the provider bills you directly."),
              h("a", { class: "btn", href: "/keys#credentials" }, "Provider credentials →")), true),
            stepBox(4, "first_preview", "First route preview", h("div", {},
              h("p", { class: "dim" }, "From ", previewing ? previewing.company + "'s" : "the", " profile: ", h("span", { class: "mono" }, describe(b)), profile && (profile.preferred_gpus || []).length ? null : h("span", {}, " (no preferred GPU in the profile yet)")),
              h("div", { class: "bar" }, h("button", { class: "btn pri", type: "button", onclick: runPreview }, lastBody ? "Run again" : "Run preview"), h("span", { class: "dim" }, "free · never calls a provider · records an audit ticket"),
                stale ? h("span", { class: "dx-stale" }, "The profile changed since this result; run again to update it.") : null),
              pvBox, whyBox)),
            stepBox(5, "first_supervised_deployment", "First supervised deployment", h("div", {},
              h("p", { class: "dim" }, "A quote from Route, approved by OpenGrid before one instance launches. ", h("b", { class: "warn-t" }, "Not available yet: no real GPU runs today.")),
              h("a", { class: "btn", href: routeHref(b) }, "Open the Route page →"))),
            stepBox(6, "feedback_given", "Feedback on the deployment", h("p", { class: "dim" }, "After a deployment ends, its page asks four short questions."))),
          h("details", { class: "dx-fold ob-prof", open: profile ? null : true }, h("summary", {}, previewing ? `Design-partner profile · ${previewing.company}` : "Design-partner profile", h("span", { class: "dim" }, " · prefills the route preview")),
            h("div", { class: "dx-fold-b" }, profileForm(profile, async body => {
              if (previewing) await ctx.api("/v1/admin/partners/" + previewing.account_id, { method: "PATCH", body });
              else if (profile) await ctx.api("/v1/partners/me", { method: "PATCH", body });
              else await ctx.api("/v1/partners/me", { method: "POST", body });
              partners = null; await load(); draw();
            }))));
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
      if (!OG.isAdmin(me)) { root.append(OG.head("Design partners"), OG.accessNote("operator")); return; }
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
            { key: "status", label: "Status", fmt: v => OG.badge(v || "-", v === "active" ? "good" : "warn") },
            { key: "technical_contact_name", label: "Contact", cls: "dim", fmt: (v, r) => [v, r.technical_contact_email].filter(Boolean).join(" · ") },
            { key: "progress", label: "Onboarding", value: r => r.onboarding && r.onboarding.progress, fmt: (v, r) => h("span", {}, r.onboarding && r.onboarding.complete ? OG.badge("complete", "good") : v || "-") },
            { key: "next", label: "Next step", cls: "dim", value: r => r.onboarding && r.onboarding.next_step ? r.onboarding.next_step.label : "" },
            { key: "preferred_gpus", label: "GPUs", cls: "mono dim", fmt: v => show(v) },
            { key: "regions", label: "Regions", cls: "dim", fmt: v => show(v) },
            { key: "normal_price_per_gpu_hour", label: "Normal $/GPU·h", num: true, fmt: (v, r) => v != null ? h("span", { title: r.normal_price_kind || "partner-reported" }, fmt.price(v), " ", OG.badge("reported")) : "-" },
            { key: "max_price_per_gpu_hour", label: "Max $/GPU·h", num: true, fmt: v => v != null ? fmt.price(v) : "-" },
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
        const yn = v => v === true ? OG.badge("yes", "good") : v === false ? OG.badge("no", "bad") : h("span", { class: "dim" }, "-");
        detail.replaceChildren(OG.section(`${p.company} · account #${p.account_id}`,
          h("div", { class: "bar" }, h("a", { class: "btn", href: "/admin/execution?acct=" + p.account_id }, "Cost guards →"), h("a", { class: "btn", href: "/onboarding?account=" + p.account_id }, "Onboarding view →")),
          h("div", { class: "cols-2" },
            OG.section(`Deployments · ${mine.length}`, OG.table({
              columns: [{ key: "status", label: "State", fmt: v => OG.stateBadge(v) }, { key: "deployment_id", label: "Deployment", cls: "mono", href: r => "/deployments/" + r.deployment_id },
                { key: "provider", label: "Provider", fmt: v => v ? OG.providerLink(v) : "-" }, { key: "gpu", label: "GPU", fmt: (v, r) => `${r.gpu_count}× ${OG.shortGpu(v || "")}` },
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
