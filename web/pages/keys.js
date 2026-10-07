/* /keys — API keys, usage, rate limits, BYO provider credentials, billing preview.
   GET/POST /v1/keys, DELETE /v1/keys/{id} (secret shown once), GET /v1/me, GET /v1/usage,
   GET/POST/DELETE /v1/credentials, GET /v1/billing/{policy,usage,invoices} (drafts only: no payments).
   The web UI acts as the operator account (site password / open dev). */
(() => {
  const { h, fmt } = OG;
  // Fallback mirror of accounts/auth.py SCOPES, used only when GET /v1/scopes (or /v1/me scopes_catalog) is absent.
  const FALLBACK_SCOPES = [
    ["data:read", "read market data, indices, events, news", ""],
    ["route:preview", "dry-run routing decisions", ""],
    ["route:execute", "provision compute through OpenGrid (spends money)", "money"],
    ["deployments:read", "see your deployments", ""],
    ["deployments:write", "stop / terminate your deployments", ""],
    ["watchlists", "manage watchlists and alerts", ""],
    ["billing:read", "see usage and invoices", ""],
    ["account:manage", "create / revoke this account's API keys and BYO provider credentials", "sensitive"],
    ["admin", "operator functions", "sensitive"],
  ];
  const confirm = o => OG.dialog(o);
  const money = v => OG.money(v);
  // Scope catalogue from the API: [{scope|name, description, spends_money|money, sensitive, tone}] (or {scopes: [...]})
  // -> [[scope, description, tone]]; null when the shape is not recognised.
  function scopeRows(x) {
    const list = Array.isArray(x) ? x : x && Array.isArray(x.scopes) ? x.scopes : x && Array.isArray(x.items) ? x.items : x && typeof x === "object" ? Object.entries(x).map(([k, v]) => (typeof v === "string" ? { scope: k, description: v } : Object.assign({ scope: k }, v))) : null;
    if (!list || !list.length) return null;
    const rows = list.map(s => {
      const name = s.scope || s.name || s.id;
      // the API flags money; "sensitive" (account / operator powers) is kept from the built-in list when the API does not say
      const known = FALLBACK_SCOPES.find(f => f[0] === (s.scope || s.name || s.id));
      const tone = s.tone || (s.spends_money || s.money || s.risk === "money" ? "money" : s.sensitive || s.risk === "sensitive" ? "sensitive" : known && known[2] === "sensitive" ? "sensitive" : "");
      return name ? [name, s.description || s.desc || s.summary || "", tone] : null;
    }).filter(Boolean);
    return rows.length ? rows : null;
  }
  let SCOPES = FALLBACK_SCOPES, scopeSource = "built-in list (the API did not publish a scope catalogue)";
  const when = v => (v ? h("span", { title: new Date(v).toString() }, fmt.dateTime(v)) : h("span", { class: "dim" }, "–"));
  const ago = v => (v ? h("span", { title: new Date(v).toString() }, fmt.age(v) + " ago") : h("span", { class: "dim" }, "never"));
  const STATE_TONE = { active: "good", revoked: "bad", expired: "warn" };

  OG.page("/keys", {
    title: "API keys",
    async mount(el, params, query, ctx) {
      const root = h("div", { class: "pg-keys" });
      el.append(root);
      const st = { keys: [], me: null, hours: Number(query.h) || 24 };
      const rateEl = h("div");
      root.append(OG.head("API keys", "One OpenGrid key for market data, routing and deployments · this page acts as the operator account",
        h("a", { class: "btn", href: "/api" }, "API docs →"), h("a", { class: "btn", href: "/methodology/billing" }, "Billing method")));
      const meBox = h("div", {}, OG.loading());
      const keysBox = h("div", {}, OG.loading("Loading keys…"));
      const reveal = h("div");
      const createBox = h("div");
      const usageBox = h("div", {}, OG.loading());
      const credBox = h("div", {}, OG.loading());
      const billBox = h("div", {}, OG.loading());
      root.append(meBox,
        h("div", { class: "ky-cols" },
          h("div", { class: "ky-main" }, reveal, OG.section("Keys", keysBox), OG.section("Create a key", createBox)),
          h("div", { class: "ky-side" }, OG.section("Rate limits", rateBox()), OG.section("Authentication", authBox()))),
        OG.section("API usage", usageBox),
        h("section", { class: "sec", id: "credentials" }, h("h2", { class: "sec-h" }, "Provider credentials · OpenGrid-managed vs bring-your-own"), credBox),
        h("section", { class: "sec", id: "billing" }, h("h2", { class: "sec-h" }, "Billing preview"), billBox));

      /* ----- who am I ----- */
      async function loadMe() {
        try {
          const me = st.me = await ctx.api("/v1/me", { nocache: true });
          const a = me.account || {};
          meBox.replaceChildren(OG.stats([
            { label: "Signed in as", value: me.principal === "operator" ? "operator" : me.key ? me.key.name : me.principal, sub: me.principal === "operator" ? "site password / open dev: every scope" : "API key " + (me.key && me.key.prefix) },
            { label: "Account", value: a.name || null, sub: `#${a.id} · ${a.plan || "–"} · ${a.status || "–"}`, reason: "no account" },
            { label: "Scopes", value: me.scopes && me.scopes[0] === "*" ? "all" : String((me.scopes || []).length), sub: me.scopes && me.scopes[0] === "*" ? "operator holds every scope" : (me.scopes || []).join(" ") },
            { label: "Active keys", value: fmt.num(st.keys.filter(k => k.state === "active").length), sub: `${st.keys.length} total` },
            { label: "Rate budget now", value: me.rate_limit ? `${me.rate_limit.read.remaining}/${me.rate_limit.read.limit}` : null, reason: "not rate limited: the operator is not an API key", sub: me.rate_limit ? "read requests left this minute" : "per API key, not per operator" },
          ]));
        } catch (e) { meBox.replaceChildren(OG.error(e, loadMe)); }
      }

      /* ----- keys ----- */
      const keysTbl = OG.table({
        columns: [
          { key: "name", label: "Name", cls: "strong" },
          { key: "prefix", label: "Key", fmt: v => h("span", { class: "mono" }, v + "…") },
          { key: "scopes", label: "Scopes", sort: false, value: r => r.scopes.join(" "), fmt: v => h("span", { class: "ky-scopes" }, (Array.isArray(v) ? v : String(v || "").split(" ")).filter(Boolean).map(s => OG.badge(s, s === "route:execute" ? "warn" : s === "admin" || s === "account:manage" ? "bad" : ""))) },
          { key: "state", label: "State", fmt: v => OG.badge(v, STATE_TONE[v]) },
          { key: "created_at", label: "Created", num: true, fmt: when },
          { key: "last_used_at", label: "Last used", num: true, fmt: (v, r) => h("span", { title: r.last_used_ip ? "from " + r.last_used_ip : null }, ago(v)) },
          { key: "expires_at", label: "Expires", num: true, fmt: v => v ? when(v) : h("span", { class: "dim" }, "never") },
          { key: "revoked_at", label: "Revoked", num: true, fmt: when },
          { key: "rate_limit_per_minute", label: "Limit/min", num: true, fmt: v => v != null ? fmt.num(v) : h("span", { class: "dim", title: "server default budgets" }, "default") },
          { key: "act", label: "", sort: false, csv: false, fmt: (v, r) => r.state === "active" ? h("button", { class: "btn sm ky-rev", type: "button", onclick: () => revoke(r) }, "Revoke") : "" },
        ],
        rows: [], sort: { key: "created_at", dir: "desc" }, compact: true, csv: "opengrid-keys.csv", rowKey: r => r.id, rowClass: r => r.state !== "active" ? "out" : "",
        empty: "No API keys yet. Create one below; the secret is shown once.",
      });
      async function loadKeys() {
        try { st.keys = await ctx.api("/v1/keys", { nocache: true }); keysTbl.update(st.keys); if (keysBox.firstChild !== keysTbl) keysBox.replaceChildren(keysTbl); }
        catch (e) { keysBox.replaceChildren(OG.error(e, loadKeys)); }
      }
      async function revoke(k) {
        const ok = await confirm({ title: `Revoke "${k.name}" (${k.prefix}…)?`, danger: true, confirm: "Revoke key",
          body: h("div", {}, h("p", {}, "Every request using this key fails with 401 from now on. Revoking cannot be undone; create a new key instead."),
            k.last_used_at ? h("p", { class: "dim" }, `Last used ${fmt.age(k.last_used_at)} ago${k.last_used_ip ? " from " + k.last_used_ip : ""}.`) : h("p", { class: "dim" }, "This key has never been used.")) });
        if (!ok) return;
        try { await ctx.api("/v1/keys/" + k.id, { method: "DELETE" }); await loadKeys(); loadMe(); }
        catch (e) { keysBox.append(OG.error(e)); }
      }

      /* ----- create ----- */
      function drawCreate() {
        const name = h("input", { class: "field", placeholder: "e.g. research-notebook", maxlength: 200, "aria-label": "Key name" });
        const exp = h("select", { class: "field", "aria-label": "Expires" }, [["", "never expires"], ["30", "30 days"], ["90", "90 days"], ["365", "1 year"]].map(([v, l]) => h("option", { value: v }, l)));
        const rl = h("input", { class: "field num", type: "number", min: 1, max: 100000, placeholder: "default", "aria-label": "Rate limit per minute" });
        const boxes = SCOPES.map(([s, desc, tone]) => {
          const cb = h("input", { type: "checkbox", value: s, checked: s === "data:read" ? true : null });
          return h("label", { class: "ky-scope" + (tone ? " t-" + tone : "") }, cb, h("span", { class: "mono ky-sn" }, s), h("span", { class: "ky-sd" }, desc),
            tone === "money" ? OG.badge("spends money", "warn") : tone === "sensitive" ? OG.badge("sensitive", "bad") : null);
        });
        const err = h("div");
        const go = h("button", { class: "btn pri", type: "submit" }, "Create key");
        const form = h("form", { class: "box ky-create", onsubmit: async e => {
          e.preventDefault();
          const scopes = boxes.map(b => b.querySelector("input")).filter(i => i.checked).map(i => i.value);
          if (!scopes.length) { err.replaceChildren(OG.error({ message: "pick at least one scope" })); return; }
          if (scopes.includes("route:execute") || scopes.includes("admin")) {
            const ok = await confirm({ title: "Create a key that can " + (scopes.includes("route:execute") ? "spend money" : "administer OpenGrid") + "?", danger: true, confirm: "Create key",
              ack: "I will store this key like a password",
              body: h("div", {}, scopes.includes("route:execute") ? h("p", {}, h("b", {}, "route:execute"), " lets anyone holding this key provision compute billed to this account (where live provisioning is enabled).") : null,
                scopes.includes("admin") ? h("p", {}, h("b", {}, "admin"), " grants operator functions: accounts, fee policies, credits, invoices.") : null) });
            if (!ok) return;
          }
          const body = { name: name.value.trim() || "default", scopes };
          if (exp.value) body.expires_in_days = Number(exp.value);
          if (rl.value) body.rate_limit_per_minute = Number(rl.value);
          go.disabled = true; err.replaceChildren();
          try {
            const k = await ctx.api("/v1/keys", { method: "POST", body });
            showSecret(k); name.value = ""; await loadKeys(); loadMe();
          } catch (e2) { err.replaceChildren(OG.error(e2)); }
          finally { go.disabled = false; }
        } },
          h("div", { class: "ky-row" }, h("label", { class: "ky-f" }, h("span", { class: "lbl" }, "Name"), name), h("label", { class: "ky-f" }, h("span", { class: "lbl" }, "Expires"), exp),
            h("label", { class: "ky-f" }, h("span", { class: "lbl" }, "Read limit / min"), rl), h("span", { class: "spacer" }), go),
          h("div", { class: "lbl", style: "margin-top:8px" }, "Scopes ", h("span", { class: "dimmer" }, "· data reads are separate from anything that spends money · from " + scopeSource)),
          h("div", { class: "ky-scopes-grid" }, boxes), err,
          h("p", { class: "note" }, "A key can never grant scopes its creator does not hold. A read-limit override replaces the read budget and can only lower the write and execute budgets."));
        createBox.replaceChildren(form);
      }
      function showSecret(k) {
        const cp = OG.copy(k.secret, { cls: "pri", title: "Copy the secret" });
        const base = (OG.boot && OG.boot.base_url) || location.origin;
        reveal.replaceChildren(h("div", { class: "ky-secret", role: "alert" },
          h("div", { class: "ky-secret-h" }, OG.badge("shown once", "warn"), h("b", {}, `New key "${k.name}" created`), h("span", { class: "spacer" }),
            h("button", { class: "btn sm", type: "button", onclick: async () => {
              const ok = await confirm({ title: "Hide the secret?", confirm: "I've stored it", body: h("p", {}, "OpenGrid keeps only a hash of this key. Once hidden it cannot be shown again; you would have to create a new key.") });
              if (ok) reveal.replaceChildren();
            } }, "Done")),
          h("p", { class: "ky-warn" }, "Copy this key now. OpenGrid stores only an HMAC of it and cannot show it again. Treat it like a password: anyone holding it acts as this account with these scopes: ", h("span", { class: "mono" }, k.scopes.join(", ")), "."),
          h("div", { class: "ky-secret-v" }, h("code", { class: "mono" }, k.secret), cp),
          h("pre", { class: "w4-code" }, `curl -H "Authorization: Bearer ${k.prefix}…" ${base}/v1/me`)));
        reveal.scrollIntoView({ block: "nearest" });
      }

      function rateBox() { drawRate(null); return rateEl; }
      // defaults: {read, write, execute} per minute from /v1/me (rate_limit_defaults | rate_limits.defaults), else not shown
      function drawRate(defs) {
        const cls = defs && (defs.classes || defs);
        const lim = k => {
          const x = cls && cls[k];
          const n = x == null ? null : typeof x === "object" ? x.limit_per_minute ?? x.per_minute ?? x.limit : x;
          return n != null ? h("span", { class: "mono" }, fmt.num(n) + "/min") : null;
        };
        rateEl.replaceChildren(h("div", { class: "box box-p ky-side-b" },
          h("table", { class: "dep-kv" },
            h("tr", {}, h("th", {}, "read"), h("td", {}, "GET / HEAD / OPTIONS"), h("td", { class: "n" }, lim("read"))),
            h("tr", {}, h("th", {}, "write"), h("td", {}, "other methods"), h("td", { class: "n" }, lim("write"))),
            h("tr", {}, h("th", {}, "execute"), h("td", {}, "non-GET under /v1/route, /v1/deployments"), h("td", { class: "n" }, lim("execute")))),
          defs ? h("p", { class: "note" }, "Default budgets per key, per minute (GET /v1/me). ", defs.override_rule ? defs.override_rule.replace(/^./, c => c.toUpperCase()) + "." : "A key's own limit replaces the read budget.") : null,
          h("p", { class: "note" }, "Per key, per class: a token bucket refilled continuously; a full minute's budget may burst. Every key response carries ",
            h("code", { class: "mono" }, "X-RateLimit-Limit / -Remaining / -Reset / -Class"), "; over budget is ", h("b", {}, "429"), " with ", h("code", { class: "mono" }, "Retry-After"), "."),
          h("p", { class: "note" }, "A key sees its live budget in ", h("code", { class: "mono" }, "GET /v1/me"), " → rate_limit. The operator (this UI) is not rate limited.")));
      }
      function authBox() {
        return h("div", { class: "box box-p ky-side-b" },
          h("pre", { class: "w4-code" }, "Authorization: Bearer opg_live_…"),
          h("p", { class: "note" }, "Keys are 256-bit random secrets prefixed ", h("code", { class: "mono" }, "opg_live_"), "; the first 13 characters are kept so you can tell keys apart. Only a keyed hash is stored."),
          h("p", { class: "note" }, h("a", { class: "lnk", href: "/api#auth" }, "Auth & scopes in the API docs →")));
      }

      /* ----- usage ----- */
      const usageSeg = OG.seg([[24, "24H"], [168, "7D"], [720, "30D"]], st.hours, v => { st.hours = Number(v); OG.qs.set({ h: v === 24 ? null : v }); loadUsage(); });
      async function loadUsage() {
        try {
          const u = await ctx.api("/v1/usage", { params: { hours: st.hours }, nocache: true, slot: "keys-usage" });
          const names = new Map(st.keys.map(k => [k.id, k]));
          const keyName = id => { const k = names.get(id); return k ? h("span", {}, k.name, " ", h("span", { class: "mono dim" }, k.prefix + "…")) : h("span", { class: "mono" }, "#" + id); };
          usageBox.replaceChildren(...[
            h("div", { class: "bar" }, usageSeg, h("span", { class: "dim", style: "font-size:11.5px" }, `since ${fmt.dateTime(u.since)} · rows kept ${u.retention_days} days · only API-key requests are logged (not this UI)`)),
            OG.stats([
              { label: "Requests", value: fmt.num(u.requests) }, { label: "OK", value: fmt.num(u.ok) },
              { label: "Rate limited", value: fmt.num(u.rate_limited), sub: "429" }, { label: "Errors", value: fmt.num(u.errors), sub: "4xx/5xx except 429" },
              { label: "Avg latency", value: u.avg_duration_ms != null ? fmt.num(u.avg_duration_ms, 1) + " ms" : null, reason: "no requests in the window" },
            ]),
            u.requests ? h("div", { class: "cols-2" },
              OG.table({ title: "By key", columns: [
                { key: "key_id", label: "Key", fmt: v => keyName(v), value: r => (names.get(r.key_id) || {}).name || r.key_id },
                { key: "requests", label: "Requests", num: true, desc: true }, { key: "last_ts", label: "Last", num: true, fmt: v => ago(v) }],
                rows: u.by_key, sort: { key: "requests", dir: "desc" }, compact: true, csv: false }),
              OG.table({ title: "Top paths", columns: [
                { key: "method", label: "", cls: "mono dim" }, { key: "path", label: "Path", cls: "mono" }, { key: "requests", label: "Requests", num: true, desc: true }],
                rows: u.top_paths, sort: { key: "requests", dir: "desc" }, compact: true, csv: "opengrid-usage-paths.csv" })) : OG.empty("No API-key requests in this window."),
            u.recent && u.recent.length ? OG.table({ title: "Latest requests", columns: [
              { key: "ts", label: "At", num: true, fmt: v => fmt.dateTime(v) }, { key: "key_id", label: "Key", fmt: v => keyName(v) },
              { key: "method", label: "", cls: "mono dim" }, { key: "path", label: "Path", cls: "mono" },
              { key: "status", label: "Status", num: true, fmt: v => h("span", { class: v >= 400 ? "down" : "" }, v) }, { key: "duration_ms", label: "ms", num: true }],
              rows: u.recent, sort: { key: "ts", dir: "desc" }, compact: true, csv: false, limit: 20 }) : null].filter(Boolean));
        } catch (e) { if (!e.stale) usageBox.replaceChildren(OG.error(e, loadUsage)); }
      }

      /* ----- credentials ----- */
      async function loadCreds() {
        let c, caps;
        try { [c, caps] = await Promise.all([ctx.api("/v1/credentials", { nocache: true }), ctx.api("/v1/capabilities").catch(() => [])]); }
        catch (e) { credBox.replaceChildren(OG.error(e, loadCreds)); return; }
        const capOf = new Map(caps.map(x => [x.provider, x]));
        const managed = c.opengrid_managed_providers || [];
        const provs = caps.length ? caps.map(x => x.provider) : OG.data.providers().map(p => p.name);
        const provSel = h("select", { class: "field", "aria-label": "Provider" },
          h("option", { value: "" }, "Provider…"),
          provs.slice().sort((a, b) => ((capOf.get(b) || {}).level_implemented >= 2) - ((capOf.get(a) || {}).level_implemented >= 2) || a.localeCompare(b)).map(p => {
            const lvl = (capOf.get(p) || {}).level_implemented;
            return h("option", { value: p }, OG.providerName(p) + (lvl >= 2 ? "  — OpenGrid can provision" : "  — no adapter: stored, not used"));
          }));
        const secret = h("input", { class: "field ky-secret-in", type: "password", autocomplete: "off", placeholder: "provider API key", "aria-label": "Secret" });
        const label = h("input", { class: "field", placeholder: "label (optional)", "aria-label": "Label" });
        const hint = h("span", { class: "note" });
        provSel.addEventListener("change", () => {
          const x = capOf.get(provSel.value);
          hint.textContent = !x ? "" : provSel.value === "verda" ? "Verda uses OAuth client credentials: enter client_id:client_secret." :
            x.via ? `Reached through ${OG.providerName(x.via)}: a BYO key here is a ${OG.providerName(x.via)} key.` : x.level_implemented >= 2 ? "" : "OpenGrid has no provisioning adapter for this provider yet; the key would be stored but never used.";
        });
        const err = h("div");
        const form = h("form", { class: "ky-row", onsubmit: async e => {
          e.preventDefault();
          if (!provSel.value || !secret.value) { err.replaceChildren(OG.error({ message: "provider and secret are required" })); return; }
          const existing = (c.byo || []).find(x => x.provider === provSel.value && x.state === "active");
          if (existing) {
            const ok = await confirm({ title: `Replace your ${OG.providerName(provSel.value)} credential?`, confirm: "Replace", body: h("p", {}, `An account holds one active credential per provider; ${existing.hint} will be revoked.`) });
            if (!ok) return;
          }
          try {
            await ctx.api("/v1/credentials", { method: "POST", body: { provider: provSel.value, secret: secret.value, label: label.value.trim() || null } });
            secret.value = ""; label.value = ""; err.replaceChildren(); loadCreds();
          } catch (e2) { err.replaceChildren(OG.error(e2)); }
        } }, provSel, secret, label, h("button", { class: "btn pri", type: "submit" }, "Store encrypted"), hint);
        const byoTbl = OG.table({
          columns: [
            { key: "provider", label: "Provider", fmt: v => OG.providerLink(v) },
            { key: "label", label: "Label", fmt: v => v || h("span", { class: "dim" }, "–") },
            { key: "hint", label: "Secret", cls: "mono", fmt: v => v + " (masked)" },
            { key: "state", label: "State", fmt: v => OG.badge(v, STATE_TONE[v]) },
            { key: "created_at", label: "Added", num: true, fmt: when },
            { key: "last_used_at", label: "Last used by routing", num: true, fmt: ago },
            { key: "act", label: "", sort: false, csv: false, fmt: (v, r) => r.state === "active" ? h("button", { class: "btn sm ky-rev", type: "button", onclick: () => delCred(r) }, "Delete") : "" },
          ], rows: c.byo || [], compact: true, csv: false, empty: "No BYO credentials: routing uses OpenGrid-managed credentials where configured.",
        });
        credBox.replaceChildren(
          h("div", { class: "ky-cred-modes" },
            h("div", { class: "box box-p" }, h("div", { class: "ky-mode-h" }, OG.badge("default", "good"), h("b", {}, "OpenGrid-managed")),
              h("p", {}, "OpenGrid provisions with its own provider accounts, pays the provider, and bills your usage: provider cost plus OpenGrid's fee lines (see billing below). One OpenGrid key, no provider accounts needed."),
              h("p", { class: "dim" }, "Configured on this server: ", managed.length ? managed.map(p => OG.providerName(p)).join(", ") : h("b", { class: "warn-t" }, "none — routing here has no managed provider credentials"), ".")),
            h("div", { class: "box box-p" }, h("div", { class: "ky-mode-h" }, OG.badge("optional"), h("b", {}, "Bring your own (BYO)")),
              h("p", {}, "Store your own key for a provider: routing uses it first for that provider, the provider bills you directly, and OpenGrid charges only its fee lines. Encrypted at rest (Fernet); never returned by any API — only the last 4 characters are shown. One active credential per provider."),
              st.me && st.me.principal === "operator" ? h("p", { class: "warn-t" }, "Note: routes started from this web UI run as the operator, which currently resolves OpenGrid-managed credentials only; BYO credentials apply to this account's API keys.") : null)),
          byoTbl, h("div", { class: "box box-p ky-add" }, h("div", { class: "lbl" }, "Add a BYO credential"), form, err));
      }
      async function delCred(r) {
        const ok = await confirm({ title: `Delete your ${OG.providerName(r.provider)} credential (${r.hint})?`, danger: true, confirm: "Delete",
          body: h("p", {}, "Routing falls back to OpenGrid-managed credentials for this provider (if configured). Running deployments started with it are not affected by OpenGrid, but OpenGrid can no longer manage them.") });
        if (!ok) return;
        try { await ctx.api("/v1/credentials/" + r.id, { method: "DELETE" }); loadCreds(); } catch (e) { credBox.append(OG.error(e)); }
      }

      /* ----- billing preview ----- */
      async function loadBilling() {
        const [pol, use, inv] = await Promise.all([
          ctx.api("/v1/billing/policy", { full: true }).catch(e => ({ error: e })),
          ctx.api("/v1/billing/usage").catch(e => ({ error: e })),
          ctx.api("/v1/billing/invoices").catch(e => ({ error: e }))]);
        const comp = c => {
          const k = c.kind;
          const txt = k === "buyer_fee_pct" ? `${c.pct}% of provider cost` : k === "flat_per_gpu_hour" ? `${money(c.usd)} per GPU-hour` :
            k === "spread" ? (c.pct != null ? `${c.pct}% markup` : `${money(c.usd_per_gpu_hour)}/GPU-h markup`) : k === "subscription" ? `${money(c.usd_per_month)} per month` :
            k === "data_api" ? `${money(c.usd_per_1k_requests)} per 1k requests after ${fmt.num(c.free_requests || 0)} free` : JSON.stringify(c);
          return h("li", {}, h("span", { class: "mono" }, k), " ", txt, c.label ? h("span", { class: "dim" }, " · " + c.label) : null, c.applies_to ? h("span", { class: "dim" }, " · applies to " + c.applies_to.join(", ")) : null);
        };
        const p = pol.error ? null : pol.data;
        const policyEl = pol.error ? OG.error(pol.error) : p ? h("div", {},
          h("div", {}, h("b", {}, p.name), h("span", { class: "dim" }, ` · v${p.version} · ${p.scope} · in force since ${fmt.date(p.effective_from)}`)),
          h("ul", { class: "ky-comp" }, (p.components || []).map(comp)), p.note ? h("p", { class: "note" }, p.note) : null)
          : OG.insufficient((pol.meta && pol.meta.note) || "no fee policy is configured: only provider cost passes through", "No fee policy");
        const usageEl = use.error ? OG.error(use.error) : (use.records || []).length ? h("div", {},
          OG.stats([{ label: "GPU-hours", value: fmt.num(use.gpu_hours, 2), kind: "transaction" },
            ...Object.entries(use.totals_by_kind || {}).map(([k, v]) => ({ label: k, value: money(v) }))]),
          OG.table({ columns: [
            { key: "period_start", label: "Period", num: true, fmt: (v, r) => fmt.dateTime(v) + " → " + fmt.time(r.period_end) },
            { key: "deployment_id", label: "Deployment", cls: "mono", href: r => "/deployments?id=" + r.deployment_id },
            { key: "kind", label: "Kind", fmt: v => OG.badge(v) }, { key: "provider", label: "Provider", fmt: v => OG.providerLink(v) },
            { key: "gpu", label: "GPU", fmt: v => OG.gpuLink(v) }, { key: "gpu_hours", label: "GPU-h", num: true, fmt: v => fmt.num(v, 2) },
            { key: "provider_cost_usd", label: "Provider cost", num: true, fmt: v => money(v) },
            { key: "charges", label: "Charge lines", sort: false, csv: false, cls: "wrap dim", fmt: v => (v || []).map(c => `${c.kind} ${money(c.amount_usd)}`).join(" · ") }],
            rows: use.records, compact: true, csv: "opengrid-billing-usage.csv", sort: { key: "period_start", dir: "desc" } })) : OG.empty("No metered compute: usage is recorded only for real deployments.");
        const invs = inv.error ? null : inv.invoices || [];
        const invEl = inv.error ? OG.error(inv.error) : invs.length ? h("div", {}, invs.map(i => h("details", { class: "box ky-inv" },
          h("summary", {}, h("b", { class: "mono" }, i.period), OG.badge(i.status, i.status === "draft" ? "warn" : ""), h("span", { class: "mono" }, "subtotal " + money(i.subtotal_usd)),
            h("span", { class: "mono" }, "credits " + money(-i.credits_usd)), h("b", { class: "mono" }, "total " + money(i.total_usd)), h("span", { class: "dim" }, "updated " + fmt.dateTime(i.updated_at))),
          OG.table({ columns: [{ key: "kind", label: "Kind", fmt: v => OG.badge(v) }, { key: "description", label: "Line", cls: "wrap" }, { key: "amount_usd", label: "USD", num: true, fmt: v => money(v) }],
            rows: i.lines || [], compact: true, csv: false })))) : OG.empty("No invoices. Drafts are built by the operator per month from recorded usage.");
        const credits = inv.error ? [] : inv.credits || [];
        billBox.replaceChildren(
          h("div", { class: "ky-nopay" }, OG.badge("preview", "warn"), h("b", {}, "No payments are processed."), " OpenGrid charges no cards and simulates no payment processor. Invoices are drafts generated from recorded usage; all figures are ", OG.kindBadge("transaction"), " data — provider cost and OpenGrid's fees are separate lines, never folded together. ", h("a", { class: "lnk", href: "/methodology/billing" }, "Billing method →")),
          h("div", { class: "cols-2" },
            h("div", {}, h("div", { class: "lbl" }, "Fee policy in force"), h("div", { class: "box box-p" }, policyEl)),
            h("div", {}, h("div", { class: "lbl" }, "Credits"), credits.length ? OG.table({ columns: [{ key: "amount_usd", label: "Amount", num: true, fmt: v => money(v) }, { key: "remaining_usd", label: "Remaining", num: true, fmt: v => money(v) }, { key: "reason", label: "Reason", cls: "wrap dim" }, { key: "expires_at", label: "Expires", num: true, fmt: v => v ? fmt.date(v) : "never" }], rows: credits, compact: true, csv: false }) : OG.empty("No credits."))),
          h("div", { class: "lbl", style: "margin-top:10px" }, "Metered usage"), usageEl,
          h("div", { class: "lbl", style: "margin-top:10px" }, "Invoices (drafts)"), invEl);
      }

      // scope catalogue: /v1/scopes, else /v1/me scopes_catalog, else the built-in fallback
      const [scopesApi, me0] = await Promise.all([ctx.api("/v1/scopes").catch(() => null), ctx.api("/v1/me", { nocache: true }).catch(() => null)]);
      if (!ctx.alive()) return;
      const fromApi = scopeRows(scopesApi), fromMe = me0 && scopeRows(me0.scopes_catalog);
      if (fromApi) { SCOPES = fromApi; scopeSource = "GET /v1/scopes"; }
      else if (fromMe) { SCOPES = fromMe; scopeSource = "GET /v1/me"; }
      const defs = me0 && (me0.rate_limit_defaults || (me0.rate_limits && me0.rate_limits.defaults) || null);
      if (defs) drawRate(defs);
      drawCreate();
      await loadKeys();
      if (!ctx.alive()) return;
      loadMe(); loadUsage(); loadCreds(); loadBilling();
    },
  });
})();
