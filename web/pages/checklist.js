/* /admin/checklist — the First Real Route checklist (GET /v1/admin/checklist?provider=&route_request_id=&probe=),
   computed live by the server: every item green / red / unknown with its evidence and the fix. Overall is
   green only when every required item is green ("unknown is not green"). This is the gate before SUPERVISED:
   the page links straight to the mode control only when the gate is green. ?provider= and ?rr= are URL-synced;
   "Probe" adds probe=true (a read-only credential check against the provider API). */
(() => {
  const { h, fmt } = OG;
  const TONE = { green: "good", red: "bad", unknown: "warn" };

  OG.page("/admin/checklist", {
    title: "First real route checklist",
    nav: "admin-checklist",
    async mount(el, params, query, ctx) {
      const root = h("div", { class: "pg-ck" });
      el.append(root);
      const me = await OG.me();
      if (!ctx.alive()) return;
      if (!OG.isAdmin(me)) { root.append(OG.head("First real route checklist"), OG.error({ status: 403, message: "admin scope required" })); return; }
      const st = { provider: query.provider || "", rr: query.rr || "", acct: query.acct || "" };
      const caps = (await OG.api.soft("/v1/capabilities")) || [];
      if (!ctx.alive()) return;
      const provs = caps.filter(c => (c.level_implemented || 0) >= 2).map(c => c.provider);
      if (!st.provider) st.provider = provs.includes("vast") ? "vast" : provs[0] || "";
      const sel = h("select", { class: "field", "aria-label": "Provider", onchange: e => { st.provider = e.target.value; sync(); load(false); } },
        provs.map(p => h("option", { value: p, selected: p === st.provider ? true : null }, OG.providerName(p))));
      const rrIn = h("input", { class: "field ck-rr", placeholder: "route_request_id (rr_…) optional", value: st.rr, onchange: e => { st.rr = e.target.value.trim(); sync(); load(false); } });
      const acctIn = h("input", { class: "field num", type: "number", min: 1, placeholder: "acct", value: st.acct, title: "account_id: check that account's limits", onchange: e => { st.acct = e.target.value.trim(); sync(); load(false); } });
      const body = h("div", {}, OG.loading("Computing the checklist…"));
      root.append(OG.head("First real route checklist", "The gate before SUPERVISED execution · computed live, nothing cached",
        sel, rrIn, acctIn,
        h("button", { class: "btn", type: "button", onclick: () => load(false) }, "Recheck"),
        h("button", { class: "btn pri", type: "button", title: "probe=true: one read-only API call with the configured credential", onclick: () => load(true) }, "Probe provider")), body);
      function sync() { OG.qs.set({ provider: st.provider || null, rr: st.rr || null, acct: st.acct || null }); }

      async function load(probe) {
        if (!st.provider) { body.replaceChildren(OG.empty("No provisionable provider on this server.")); return; }
        body.replaceChildren(OG.loading(probe ? "Probing " + OG.providerName(st.provider) + " (read-only)…" : "Computing…"));
        let d;
        try {
          d = await ctx.api("/v1/admin/checklist", { params: { provider: st.provider, route_request_id: st.rr || null, account_id: st.acct || null, probe: probe ? "true" : "false" }, nocache: true, slot: "ck" });
        } catch (e) { if (!e.stale) body.replaceChildren(OG.error(e, () => load(probe))); return; }
        const items = d.items || [];
        const green = d.overall === "green";
        body.replaceChildren(
          h("div", { class: "ck-overall ck-" + d.overall },
            h("b", {}, green ? "GREEN — ready for a supervised first route" : d.overall === "red" ? "RED — do not enable SUPERVISED" : "NOT GREEN — unknown items are not green"),
            h("span", { class: "mono" }, `${d.green} green · ${d.red} red · ${d.unknown} unknown`),
            h("span", { class: "dim" }, OG.providerName(d.provider) + (d.route_request_id ? " · " + d.route_request_id : "") + " · computed " + fmt.time(d.computed_at) + (probe ? " · probed" : "")),
            h("span", { class: "spacer" }),
            green ? h("a", { class: "btn pri", href: "/admin/execution" }, "Go to execution mode →") : h("a", { class: "btn", href: "/admin/execution", title: "the gate is not green: fix the red items first" }, "Execution control")),
          h("ol", { class: "ck-items" }, items.map(it => h("li", { class: "ck-" + it.status },
            h("i", { class: "ck-dot" }),
            h("div", { class: "ck-main" },
              h("div", { class: "ck-l" }, h("b", {}, it.label), OG.badge(it.status, TONE[it.status]), it.required ? null : OG.badge("optional")),
              it.evidence ? h("div", { class: "ck-ev mono" }, it.evidence) : null,
              it.status !== "green" && it.fix ? h("div", { class: "ck-fix" }, h("span", { class: "eyebrow" }, "How to fix"), " ", h("code", { class: "mono" }, it.fix)) : null)))),
          h("p", { class: "note" }, d.rule || "", " · ", h("a", { class: "lnk", href: "/methodology/first-live-route" }, "First live route guide →")));
      }
      load(false);
    },
  });
})();
