/* /deployments — workloads OpenGrid actually provisioned (transaction data), from GET /v1/deployments.
   A deployment exists only when live provisioning ran: a route with provisioning disabled creates none.
   Row → detail drawer (GET /v1/deployments/{id}?refresh=false; "Refresh from provider" asks the provider):
   the four price concepts kept apart, lifecycle, events timeline, provision attempts, audit link, and
   stop / terminate behind a confirmation (stop only where the provider API has it). ?id=<dep> opens the drawer. */
(() => {
  const { h, fmt } = OG;
  const LIVE = ["provisioning", "running", "stopped", "terminating"];
  const TONE = { running: "good", provisioning: "warn", routing: "warn", pending: "warn", terminating: "warn", stopped: "", failed: "bad", terminated: "" };
  const statusBadge = s => OG.badge(s || "–", TONE[s] || "", s === "stopped" ? "Stopped: may still bill at some providers (see provider notes)" : null);
  const dur = sec => {
    if (sec == null) return "–";
    const s = Math.max(0, Math.round(sec));
    if (s < 60) return s + "s";
    if (s < 3600) return Math.floor(s / 60) + "m";
    if (s < 86400) return Math.floor(s / 3600) + "h " + String(Math.floor(s % 3600 / 60)).padStart(2, "0") + "m";
    return Math.floor(s / 86400) + "d " + Math.floor(s % 86400 / 3600) + "h";
  };
  const concept = c => OG.conceptBadge(c);
  const confirm = o => OG.dialog(o);

  OG.page("/deployments", {
    title: "Deployments",
    mount(el, params, query, ctx) {
      const root = h("div", { class: "pg-dep" });
      el.append(root);
      const st = { filter: ["all", "live", "failed", "terminated"].includes(query.status) ? query.status : "all", rows: null, open: query.id || null };
      const stats = h("div");
      const liveEl = h("span", { class: "dep-live" });
      const holder = h("div", {}, OG.loading("Loading deployments…"));
      const seg = OG.seg([["all", "All"], ["live", "Live"], ["failed", "Failed"], ["terminated", "Terminated"]], st.filter, v => { st.filter = v; OG.qs.set({ status: v === "all" ? null : v }); draw(); });
      root.append(OG.head("Deployments", "Compute OpenGrid actually provisioned · transaction data",
        liveEl, OG.kindBadge("transaction"), seg,
        h("button", { class: "btn", type: "button", onclick: () => load() }, "Reload"),
        h("a", { class: "btn pri", href: "/route" }, "New route →")), stats, holder);

      const tbl = OG.table({
        columns: [
          { key: "deployment_id", label: "Deployment", cls: "mono", fmt: v => h("span", { class: "dep-id" }, v) },
          { key: "status", label: "Status", fmt: v => statusBadge(v) },
          { key: "provider", label: "Provider", fmt: v => v ? OG.providerLink(v) : h("span", { class: "dim" }, "not placed") },
          { key: "gpu", label: "GPU", fmt: v => OG.gpuLink(v) },
          { key: "gpu_count", label: "GPUs", num: true },
          { key: "region", label: "Region", cls: "dim" },
          { key: "quote", label: "Quote $/GPU·h", num: true, value: r => r.prices && r.prices.quote, fmt: v => v != null ? fmt.price(v) : OG.na("no quote recorded"), title: "The price this route quoted" },
          { key: "exec", label: "Execution $/GPU·h", num: true, value: r => r.prices && r.prices.execution_price, fmt: v => v != null ? fmt.price(v) : OG.na("the provider has not reported an execution price"), title: "What the provider reports charging" },
          { key: "created_at", label: "Created", num: true, fmt: v => fmt.dateTime(v) },
          { key: "provisioned_at", label: "Provisioned", num: true, fmt: v => v ? fmt.dateTime(v) : "–" },
          { key: "terminated_at", label: "Terminated", num: true, fmt: v => v ? fmt.dateTime(v) : "–" },
          { key: "uptime_seconds", label: "Uptime", num: true, fmt: v => dur(v), title: "OpenGrid-observed running time (accurate to the status-check interval, not the provider's billing clock)" },
          { key: "interruptions", label: "Intr", num: true, title: "Times it left running without OpenGrid asking" },
          { key: "failure_reason", label: "Failure reason", cls: "wrap dim", fmt: v => v || "" },
        ],
        rows: [], sort: { key: "created_at", dir: "desc" }, rowKey: r => r.deployment_id, onRow: r => openDrawer(r.deployment_id),
        csv: "opengrid-deployments.csv", compact: true, empty: "No deployments match this filter.",
      });

      function draw() {
        const rows = st.rows;
        if (!rows) return;
        const live = rows.filter(r => LIVE.includes(r.status));
        const up = rows.reduce((s, r) => s + (r.uptime_seconds || 0), 0);
        const gpuh = rows.reduce((s, r) => s + (r.uptime_seconds || 0) * (r.gpu_count || 0) / 3600, 0);
        stats.replaceChildren(OG.stats([
          { label: "Deployments", value: fmt.num(rows.length) },
          { label: "Live", value: fmt.num(live.length), sub: "provisioning · running · stopped · terminating" },
          { label: "Running", value: fmt.num(rows.filter(r => r.status === "running").length) },
          { label: "Failed", value: fmt.num(rows.filter(r => r.status === "failed").length) },
          { label: "Observed uptime", value: rows.length ? dur(up) : null, reason: "no deployments", sub: "OpenGrid-observed, all deployments" },
          { label: "GPU-hours observed", value: rows.length ? fmt.num(gpuh, 1) : null, reason: "no deployments", kind: "transaction", sub: "uptime × GPUs; billing uses provider periods" },
        ]));
        if (!rows.length) {
          holder.replaceChildren(h("div", { class: "dep-empty" },
            h("b", {}, "No deployments yet."),
            h("p", {}, "A deployment is created only when ", h("code", { class: "mono" }, "POST /v1/route"), " actually provisions an instance: it ranks candidates, checks live availability, quotes, and — only where live provisioning is enabled on the server — launches on the first provisionable provider."),
            h("p", {}, "With live provisioning disabled (the default), a route returns ", h("code", { class: "mono" }, "status: not_provisioned"), " with the decision and quote, and creates no deployment, so this list can never show compute that was not launched."),
            h("p", {}, h("a", { class: "btn pri", href: "/route" }, "Preview a route →"), " ", h("a", { class: "btn", href: "/methodology/routing" }, "How routing works"), " ", h("a", { class: "btn", href: "/api#ep-routing" }, "API: /v1/route"))));
          return;
        }
        const f = st.filter;
        tbl.update(rows.filter(r => f === "all" || (f === "live" ? LIVE.includes(r.status) : r.status === f)));
        if (holder.firstChild !== tbl) holder.replaceChildren(tbl, h("p", { class: "note" }, "Quote = what the route quoted; execution price = what the provider reports charging. They are never merged. Uptime is OpenGrid-observed. ",
          h("a", { class: "lnk", href: "/methodology/routing" }, "Method →")));
        if (st.open) tbl.highlight(st.open);
      }

      async function load() {
        try { st.rows = await ctx.api("/v1/deployments", { nocache: true }); draw(); }
        catch (e) { holder.replaceChildren(OG.error(e, load)); }
      }

      /* ----- live provisioning in this environment (GET /v1/capabilities meta) ----- */
      ctx.api("/v1/capabilities", { full: true }).then(r => {
        const on = r && r.meta ? r.meta.live_provisioning_enabled : undefined;
        liveEl.replaceChildren(on === true ? OG.badge("Live provisioning: on in this environment", "warn", "POST /v1/route on this server can launch paid instances")
          : on === false ? OG.badge("Live provisioning: off in this environment", "", "POST /v1/route here ranks, checks and quotes but never launches, so no deployment is created")
          : OG.badge("Live provisioning: unknown", "", "this server does not report it (GET /v1/capabilities meta.live_provisioning_enabled)"));
      }, () => liveEl.replaceChildren());

      /* ----- detail drawer (OG.drawer) ----- */
      let drawer = null;
      ctx.onCleanup(() => { if (drawer) drawer.close(true); });

      async function openDrawer(id, refresh) {
        st.open = id; OG.qs.set({ id }); tbl.highlight(id);
        if (!drawer || !drawer.open) drawer = OG.drawer({ label: "Deployment " + id, cls: "dep-drawer", onClose: () => { drawer = null; st.open = null; OG.qs.set({ id: null }); tbl.highlight(null); } });
        drawer.head(h("b", { class: "mono" }, id)).body(OG.loading(refresh ? "Asking the provider…" : "Loading…"));
        try {
          const d = await ctx.api("/v1/deployments/" + encodeURIComponent(id), { params: { refresh: refresh ? "true" : "false" }, nocache: true });
          if (st.open !== id || !drawer) return;
          renderDrawer(d);
          if (refresh) load();
        } catch (e) {
          if (drawer) drawer.head(h("b", { class: "mono" }, id)).body(OG.error(e));
        }
      }

      function kv(rows) {
        return h("table", { class: "dep-kv" }, rows.filter(Boolean).map(([k, v, title]) => h("tr", { title: title || null }, h("th", {}, k), h("td", {}, v == null || v === "" ? h("span", { class: "dim" }, "–") : v))));
      }

      function renderDrawer(d) {
        const p = d.prices || {};
        const canStop = d.supports_stop && ["running", "provisioning"].includes(d.status);
        const canTerm = LIVE.includes(d.status) && d.provider_instance_id;
        const acts = h("div", { class: "dep-acts" },
          h("button", { class: "btn", type: "button", onclick: () => openDrawer(d.deployment_id, true), title: "GET /v1/deployments/{id}?refresh=true: asks the provider for the current state" }, "Refresh from provider"),
          canStop ? h("button", { class: "btn", type: "button", onclick: () => act(d, "stop") }, "Stop…") : null,
          canTerm ? h("button", { class: "btn w4-danger", type: "button", onclick: () => act(d, "terminate") }, "Terminate…") : null,
          !canStop && LIVE.includes(d.status) && !d.supports_stop ? h("span", { class: "dim dep-why", title: "This provider's API has no stop; only terminate ends it" }, "no stop at " + OG.providerName(d.provider)) : null);
        const tx = d.transaction;
        drawer.head(h("b", { class: "mono" }, d.deployment_id), statusBadge(d.status), OG.copy(d.deployment_id, { title: "Copy the deployment id" }));
        drawer.body(...[
          h("div", { class: "dep-sub" }, d.provider ? OG.providerLink(d.provider) : h("span", { class: "dim" }, "never placed"), h("span", { class: "mono" }, `${d.gpu_count}× `, OG.gpuLink(d.gpu)), d.region ? h("span", { class: "dim" }, d.region) : null,
            h("span", { class: "spacer" }), d.route_request_id ? h("a", { class: "lnk mono dep-audit", href: "/v1/route/" + d.route_request_id, target: "_blank", rel: "external noopener", title: "The routing decision this deployment came from" }, "audit " + d.route_request_id + " ↗") : null),
          d.refresh ? h("div", { class: "note dep-ref" }, d.refresh.refreshed ? "Refreshed from the provider" + (d.refresh.note ? ": " + d.refresh.note : ".") : "Not refreshed: " + (d.refresh.reason || "")) : null,
          acts,
          h("h3", { class: "sec-h" }, "Prices (USD per GPU-hour) — four concepts, never merged"),
          kv([
            [h("span", {}, "Observed market ", concept("observed")), p.observed_market_price != null ? fmt.price(p.observed_market_price) : null, "OpenGrid's observed listing price when routed"],
            [h("span", {}, "List ", concept("list")), p.list_price != null ? fmt.price(p.list_price) : null, "The provider's catalogue price read at the availability check"],
            [h("span", {}, "Quote ", concept("quote")), p.quote != null ? h("span", {}, fmt.price(p.quote), h("span", { class: "dim" }, " · basis " + (p.quote_basis || "?"))) : null],
            [h("span", {}, "Execution ", concept("execution")), p.execution_price != null ? fmt.price(p.execution_price) : OG.na("the provider has not reported what it charges"), "What the provider reports charging"],
          ]),
          h("h3", { class: "sec-h" }, "Lifecycle"),
          kv([
            ["Created", fmt.dateTime(d.created_at)], ["Provisioned", d.provisioned_at ? fmt.dateTime(d.provisioned_at) : null],
            ["Terminated", d.terminated_at ? fmt.dateTime(d.terminated_at) : null], ["Last checked", d.last_checked_at ? fmt.dateTime(d.last_checked_at) + " (" + fmt.age(d.last_checked_at) + " ago)" : null],
            ["Uptime", dur(d.uptime_seconds), "OpenGrid-observed"], ["Interruptions", String(d.interruptions || 0)],
            ["Failure", d.failure_reason ? h("span", { class: "down" }, d.failure_reason) : null], ["Termination", d.termination_reason],
            ["Credentials", d.credential_source ? (d.credential_source === "byo" ? "your own (BYO) — the provider bills you" : "OpenGrid-managed — OpenGrid pays the provider") : null],
            ["Instance", d.provider_instance_id ? h("span", { class: "mono" }, d.provider_instance_id) : null], ["IP", d.ip ? h("span", { class: "mono" }, d.ip) : null],
          ]),
          tx ? h("div", {}, h("h3", { class: "sec-h" }, "Execution record ", OG.kindBadge("transaction")), kv(Object.entries(tx).filter(([k, v]) => v != null && typeof v !== "object").map(([k, v]) => [k.replace(/_/g, " "), h("span", { class: "mono" }, String(v))]))) : null,
          h("h3", { class: "sec-h" }, "Events"),
          (d.events || []).length ? h("ol", { class: "dep-tl" }, d.events.map(e => h("li", { class: "t-" + e.to },
            h("span", { class: "mono dim" }, fmt.dateTime(e.at)), h("span", { class: "mono" }, (e.from || "∅") + " → ", h("b", {}, e.to)),
            e.detail && Object.keys(e.detail).length ? h("span", { class: "dim dep-det" }, Object.entries(e.detail).map(([k, v]) => `${k}: ${typeof v === "object" ? JSON.stringify(v) : v}`).join(" · ")) : null))) : OG.empty("No events recorded."),
          h("h3", { class: "sec-h" }, "Provision attempts"),
          (d.provision_attempts || []).length ? OG.table({
            columns: [
              { key: "rank", label: "#", num: true }, { key: "provider", label: "Provider", fmt: v => OG.providerLink(v) },
              { key: "ok", label: "Result", fmt: (v, r) => v ? OG.badge("ok", "good") : OG.badge(r.error_kind || "failed", "bad") },
              { key: "latency_ms", label: "ms", num: true }, { key: "started_at", label: "At", num: true, fmt: v => fmt.time(v) },
              { key: "error", label: "Error", cls: "wrap dim" },
            ], rows: d.provision_attempts, compact: true, csv: false,
          }) : OG.empty("No provision attempts recorded."),
          h("h3", { class: "sec-h" }, "Report outcome"),
          h("div", { class: "dep-acts" }, h("span", { class: "dim" }, "Did the workload complete? (feeds future reliability data)"),
            h("button", { class: "btn sm", type: "button", onclick: () => outcome(d, true) }, "Completed"),
            h("button", { class: "btn sm", type: "button", onclick: () => outcome(d, false) }, "Did not complete"))].filter(Boolean));
      }

      async function outcome(d, completed) {
        try { await ctx.api(`/v1/deployments/${encodeURIComponent(d.deployment_id)}/outcome`, { method: "POST", body: { workload_completed: completed } }); openDrawer(d.deployment_id); }
        catch (e) { drawer && drawer.append(OG.error(e)); }
      }

      async function act(d, what) {
        const cost = d.prices && (d.prices.execution_price ?? d.prices.quote);
        const ok = await confirm({
          title: what === "terminate" ? `Terminate ${d.deployment_id}?` : `Stop ${d.deployment_id}?`,
          danger: true, confirm: what === "terminate" ? "Terminate" : "Stop",
          ack: what === "terminate" ? "I understand the instance and its local data are destroyed at the provider" : null,
          body: h("div", {},
            h("p", {}, `${what === "terminate" ? "Terminates" : "Stops"} the instance on ${OG.providerName(d.provider)} (${d.gpu_count}× ${OG.shortGpu(d.gpu)}${cost != null ? ", " + fmt.price(cost) + "/GPU·h" : ""}).`),
            what === "stop" ? h("p", { class: "warn-t" }, "A stopped instance may still bill at some providers (e.g. powered-off DigitalOcean Droplets). Only terminate is guaranteed to end spend.") :
              h("p", {}, "This cannot be undone. Billing stops when the provider confirms termination.")),
        });
        if (!ok) return;
        try {
          await ctx.api(`/v1/deployments/${encodeURIComponent(d.deployment_id)}/${what}`, { method: "POST" });
          openDrawer(d.deployment_id); load();
        } catch (e) { if (drawer) drawer.append(OG.error(e)); }
      }

      load().then(() => { if (st.open) openDrawer(st.open); });
      ctx.every(30000, load);
    },
  });
})();
