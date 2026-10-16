/* /keys: create and manage API keys.
   Signed out (no site login): one access state (how keys are issued today, what works without one); no private
   API call is made, so there is no stack of 401 boxes. Signed in (the operator, the only browser principal):
     1. create a key (presets first; the full scope grid is one click away; operator-level scopes are set apart)
        -> the secret is shown once, with how to use it;
     2. the keys table (revoke);
     3. secondary, loaded only when opened: API usage, rate limits & authentication, provider credentials (BYO vs
        managed), billing preview.
   GET/POST /v1/keys, DELETE /v1/keys/{id}, GET /v1/me, GET /v1/scopes, GET /v1/usage, GET/POST/DELETE
   /v1/credentials, GET /v1/billing/{policy,usage,invoices} (drafts only: no payments). Keys created here belong to
   the operator account; a developer's own account and key are created on /admin/partners. */
(() => {
  const { h, fmt } = OG;
  const KEY_VAR = "OPENGRID_API_KEY";
  // Fallback mirror of accounts/auth.py SCOPES, used only when GET /v1/scopes is unavailable.
  const FALLBACK_SCOPES = [
    ["data:read", "read market data, indices, events, news", ""],
    ["route:preview", "dry-run routing decisions", ""],
    ["route:execute", "provision compute through OpenGrid (spends money)", "money"],
    ["deployments:read", "see your deployments", ""],
    ["deployments:write", "stop / terminate your deployments", ""],
    ["watchlists", "manage watchlists and alerts", ""],
    ["billing:read", "see usage and invoices", ""],
    ["keys:read", "list this account's API keys (metadata only)", ""],
    ["account:manage", "create / revoke this account's API keys and BYO provider credentials", "sensitive"],
    ["admin", "operator functions", "sensitive"],
  ];
  // Scopes that are not part of a normal integration: they spend money, manage the account or act as operator.
  const ELEVATED = ["route:execute", "account:manage", "admin"];
  const PRESETS = [
    ["read", "Read-only", "Prices, specs and recommendations through the hosted MCP endpoint or the API. Right for most assistants and scripts.", ["data:read"]],
    ["assistant", "Assistant with previews and jobs", "Also previews routes and reads your jobs. Cannot start or cancel anything.", ["data:read", "route:preview", "deployments:read"]],
    ["custom", "Custom", "Pick each scope yourself.", null],
  ];
  // Scope catalogue from the API: [{scope|name, description, spends_money|money, sensitive, tone}] (or {scopes: [...]})
  // -> [[scope, description, tone]]; null when the shape is not recognised.
  function scopeRows(x) {
    const list = Array.isArray(x) ? x : x && Array.isArray(x.scopes) ? x.scopes : x && Array.isArray(x.items) ? x.items : x && typeof x === "object" ? Object.entries(x).map(([k, v]) => (typeof v === "string" ? { scope: k, description: v } : Object.assign({ scope: k }, v))) : null;
    if (!list || !list.length) return null;
    const rows = list.map(s => {
      const name = s.scope || s.name || s.id;
      const known = FALLBACK_SCOPES.find(f => f[0] === name);
      const tone = s.tone || (s.spends_money || s.money || s.risk === "money" ? "money" : s.sensitive || s.risk === "sensitive" ? "sensitive" : known && known[2] === "sensitive" ? "sensitive" : "");
      return name ? [name, s.description || s.desc || s.summary || "", tone] : null;
    }).filter(Boolean);
    return rows.length ? rows : null;
  }
  const presetScopes = id => { const p = PRESETS.find(x => x[0] === id); return p && p[3] ? p[3].slice() : null; };
  OG.keysPage = { scopeRows, PRESETS, ELEVATED, presetScopes, FALLBACK_SCOPES };
  if (typeof OG.page !== "function" || typeof document === "undefined") return;

  const confirm = o => OG.dialog(o);
  const money = v => OG.money(v);
  const when = v => (v ? h("span", { title: new Date(v).toString() }, fmt.dateTime(v)) : h("span", { class: "dim" }, "-"));
  const ago = v => (v ? h("span", { title: new Date(v).toString() }, fmt.age(v) + " ago") : h("span", { class: "dim" }, "never"));
  const STATE_TONE = { active: "good", revoked: "bad", expired: "warn" };

  OG.page("/keys", {
    title: "API keys",
    async mount(el, params, query, ctx) {
      const root = h("div", { class: "pg-dx pg-dxk pg-keys" });
      el.append(root, OG.loading());
      const me0 = await OG.me();
      if (!ctx.alive()) return;
      el.lastChild && el.lastChild !== root && el.lastChild.remove();
      const D = OG.dx;
      const { code, p, c } = D;
      const base = ((OG.boot && OG.boot.base_url) || location.origin).replace(/\/$/, "");

      /* ----- signed out: one intentional state ----- */
      if (!me0) {
        root.append(OG.head("API keys", "Keys let your tools use OpenGrid as your account", h("a", { class: "btn", href: "/developers" }, "Developer guide")),
          D.accessCall(null, { title: "API keys are issued by the OpenGrid team." }),
          h("div", { class: "dx-wins" },
            h("div", { class: "dx-win" }, h("h2", { class: "dx-h3", style: "margin-top:0" }, "Works without a key"),
              h("ul", { class: "dx-ul" }, h("li", {}, "Public market data over REST, the CLI and the local MCP server"), h("li", {}, "Analyzing a project on your computer"), h("li", {}, "GPU recommendations for a workload"), h("li", {}, "The OpenGrid Assistant on this site")),
              h("a", { class: "btn", href: "/developers#quickstart" }, "Quickstart →")),
            h("div", { class: "dx-win" }, h("h2", { class: "dx-h3", style: "margin-top:0" }, "Already have a key?"),
              p("Manage it from your terminal; this page is for the operator. Check it, or create a narrower key for one tool (your key needs ", c("account:manage"), "):"),
              code(`curl -s "${base}/v1/me" -H "Authorization: Bearer $${KEY_VAR}"
curl -s "${base}/v1/keys" -H "Authorization: Bearer $${KEY_VAR}"`, "curl", "your account's keys"),
              h("a", { class: "lnk", href: "/developers#access" }, "Using your key with the CLI, MCP and curl →"))));
        return;
      }

      /* ----- signed in ----- */
      const st = { keys: [], me: me0, hours: Number(query.h) || 24 };
      const isOperator = OG.isAdmin(me0);
      root.append(OG.head("API keys", isOperator ? "Keys for the operator account. A key holds only the scopes you pick; the secret is shown once." : "Your account's keys",
        isOperator ? h("a", { class: "btn", href: "/admin/partners", title: "Create a design partner: their own account and first key" }, "Issue a key to a developer →") : null,
        h("a", { class: "btn", href: "/api#auth" }, "API reference")));
      const reveal = h("div"), createBox = h("div"), keysBox = h("div", {}, OG.loading("Loading keys…")), meLine = h("div", { class: "dim", style: "font-size:11.5px;margin:-4px 0 10px" });
      root.append(meLine, reveal, h("section", { class: "sec", id: "create" }, h("h2", { class: "sec-h" }, "Create a key"), createBox), OG.section("Your keys", keysBox));

      function drawMe() {
        const a = (st.me && st.me.account) || {};
        meLine.replaceChildren(st.me.principal === "operator" ? "Signed in as the operator (site login): every scope · " : "Signed in with a key · ",
          `account #${a.id != null ? a.id : "-"} ${a.name ? "· " + a.name : ""} · ${st.keys.filter(k => k.state === "active").length} active key(s)`);
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
          { key: "rate_limit_per_minute", label: "Read/min", num: true, fmt: v => v != null ? fmt.num(v) : h("span", { class: "dim", title: "server default budgets" }, "default") },
          { key: "act", label: "", sort: false, csv: false, fmt: (v, r) => r.state === "active" ? h("button", { class: "btn sm ky-rev", type: "button", onclick: () => revoke(r) }, "Revoke") : "" },
        ],
        rows: [], sort: { key: "created_at", dir: "desc" }, compact: true, csv: "opengrid-keys.csv", rowKey: r => r.id, rowClass: r => r.state !== "active" ? "out" : "",
        empty: "No keys yet. Create one above; the secret is shown once.",
      });
      async function loadKeys() {
        try { st.keys = await ctx.api("/v1/keys", { nocache: true }); keysTbl.update(st.keys); if (keysBox.firstChild !== keysTbl) keysBox.replaceChildren(keysTbl); drawMe(); }
        catch (e) { keysBox.replaceChildren(OG.error(e, loadKeys)); }
      }
      async function revoke(k) {
        const ok = await confirm({ title: `Revoke "${k.name}" (${k.prefix}…)?`, danger: true, confirm: "Revoke key",
          body: h("div", {}, h("p", {}, "Every request using this key fails with 401 from now on, and keys it created are revoked too. This cannot be undone; create a new key instead."),
            k.last_used_at ? h("p", { class: "dim" }, `Last used ${fmt.age(k.last_used_at)} ago${k.last_used_ip ? " from " + k.last_used_ip : ""}.`) : h("p", { class: "dim" }, "This key has never been used.")) });
        if (!ok) return;
        try { await ctx.api("/v1/keys/" + k.id, { method: "DELETE" }); await loadKeys(); }
        catch (e) { keysBox.append(OG.error(e)); }
      }

      /* ----- create ----- */
      let SCOPES = OG.keysPage.FALLBACK_SCOPES;
      function drawCreate() {
        let preset = PRESETS.some(x => x[0] === query.preset) ? query.preset : "read";
        const name = h("input", { class: "field", placeholder: "e.g. claude-code-laptop", maxlength: 200, "aria-label": "Key name", value: preset === "assistant" ? "ai-assistant" : null });
        const exp = h("select", { class: "field", "aria-label": "Expires" }, [["90", "90 days"], ["30", "30 days"], ["365", "1 year"], ["", "never"]].map(([v, l]) => h("option", { value: v }, l)));
        const rl = h("input", { class: "field num", type: "number", min: 1, max: 100000, placeholder: "default", "aria-label": "Read limit per minute", style: "width:100px" });
        const boxes = new Map();
        const scopeEl = ([s, desc, tone]) => {
          const cb = h("input", { type: "checkbox", value: s });
          boxes.set(s, cb);
          return h("label", { class: "dx-scope" }, cb, h("span", {}, h("code", {}, s), " ", desc, " ",
            tone === "money" ? OG.badge("spends money", "warn") : tone === "sensitive" ? OG.badge("sensitive", "bad") : null));
        };
        const normal = SCOPES.filter(s => !ELEVATED.includes(s[0])), elevated = SCOPES.filter(s => ELEVATED.includes(s[0]));
        const grid = h("details", { class: "dx-fold" }, h("summary", {}, "Scopes ", h("span", { class: "dim" }, "· the exact permissions this key gets")),
          h("div", { class: "dx-fold-b" },
            h("div", { class: "dx-scopes" }, normal.map(scopeEl)),
            elevated.length ? [h("div", { class: "lbl", style: "margin-top:8px" }, "Operator-level: not for assistants or scripts"), h("div", { class: "dx-scopes" }, elevated.map(scopeEl))] : null,
            h("p", { class: "note" }, "A key can never grant scopes its creator does not hold. ", h("a", { class: "lnk", href: "/api#private" }, "What each scope allows →"))));
        const setPreset = id => {
          preset = id;
          const sc = presetScopes(id);
          if (sc) boxes.forEach((cb, s) => { cb.checked = sc.includes(s); });
          if (id === "custom") grid.open = true;
          radios.forEach(r => { r.checked = r.value === id; });
        };
        const radios = [];
        const presetEls = PRESETS.map(([id, title, desc]) => {
          const r = h("input", { type: "radio", name: "dx-preset", value: id });
          r.addEventListener("change", () => setPreset(id));
          radios.push(r);
          return h("label", { class: "dx-preset" }, r, h("span", {}, h("b", {}, title), desc));
        });
        boxes.forEach(cb => cb.addEventListener("change", () => { if (preset !== "custom") { preset = "custom"; radios.forEach(r => { r.checked = r.value === "custom"; }); } }));
        const err = h("div");
        const go = h("button", { class: "btn pri", type: "submit" }, "Create key");
        const form = h("form", { class: "dx-form", onsubmit: async e => {
          e.preventDefault();
          const scopes = [...boxes].filter(([, cb]) => cb.checked).map(([s]) => s);
          if (!scopes.length) { err.replaceChildren(OG.error({ message: "pick at least one scope" })); return; }
          const risky = scopes.filter(s => ELEVATED.includes(s));
          if (risky.length) {
            const ok = await confirm({ title: "Create a key with " + risky.join(", ") + "?", danger: true, confirm: "Create key", ack: "I will store this key like a password",
              body: h("div", {},
                scopes.includes("route:execute") ? h("p", {}, h("b", {}, "route:execute"), " lets anyone holding this key submit jobs and request compute billed to this account (launches still need operator approval). Not needed for an assistant that only reads.") : null,
                scopes.includes("account:manage") ? h("p", {}, h("b", {}, "account:manage"), " lets the key create and revoke keys and store provider credentials for this account.") : null,
                scopes.includes("admin") ? h("p", {}, h("b", {}, "admin"), " is honoured on a key only when the operator flags it platform_admin; it grants operator functions.") : null) });
            if (!ok) return;
          }
          const body = { name: name.value.trim() || "default", scopes };
          if (exp.value) body.expires_in_days = Number(exp.value);
          if (rl.value) body.rate_limit_per_minute = Number(rl.value);
          go.disabled = true; err.replaceChildren();
          try {
            const k = await ctx.api("/v1/keys", { method: "POST", body });
            showSecret(k); name.value = ""; await loadKeys();
          } catch (e2) { err.replaceChildren(OG.error(e2)); }
          finally { go.disabled = false; }
        } },
          h("div", { class: "dx-presets", role: "radiogroup", "aria-label": "Key type" }, presetEls),
          h("div", { class: "dx-form-r" },
            h("label", {}, "Name", name), h("label", {}, "Expires", exp),
            h("label", { title: "Requests per minute for reads; also caps writes and executes" }, "Read limit / min", rl), go),
          grid, err);
        createBox.replaceChildren(form);
        setPreset(preset);
      }
      function showSecret(k) {
        const cp = OG.copy(k.secret, { cls: "pri", title: "Copy the key", label: "Copy key" });
        reveal.replaceChildren(h("div", { class: "dx-secret", role: "alert" },
          h("div", { class: "dx-row", style: "margin:0" }, OG.badge("shown once", "warn"), h("b", {}, `Key "${k.name}" created`), h("span", { class: "spacer" }),
            h("button", { class: "btn sm", type: "button", onclick: async () => {
              const ok = await confirm({ title: "Hide the key?", confirm: "I've stored it", body: h("p", {}, "OpenGrid keeps only a hash of this key. Once hidden it cannot be shown again; you would have to create a new key.") });
              if (ok) reveal.replaceChildren();
            } }, "Done")),
          p("Copy it now: OpenGrid cannot show it again. Anyone holding it acts as this account with ", h("b", {}, k.scopes.join(", ")), "."),
          h("div", { class: "dx-secret-v" }, h("code", {}, k.secret), cp),
          h("div", { class: "lbl", style: "margin-top:8px" }, "Use it"),
          code(`export ${KEY_VAR}=<paste the key>      # in ~/.zshrc or ~/.bashrc; for the hosted MCP endpoint and curl
opengrid auth login                     # or store it for the CLI and the local MCP server (hidden prompt)`, "shell", "never in a URL or a committed file"),
          code(`curl -s "${base}/v1/me" -H "Authorization: Bearer $${KEY_VAR}"`, "curl", "check it"),
          p(h("a", { class: "lnk", href: "/developers#assistant" }, "Connect an assistant with it →"))));
        reveal.scrollIntoView({ block: "nearest" });
      }

      /* ----- secondary: account details, each loaded only when opened ----- */
      const panes = {
        usage: { label: "API usage", load: loadUsage },
        limits: { label: "Rate limits & authentication", load: drawLimits },
        credentials: { label: "Provider credentials", load: loadCreds },
        billing: { label: "Billing preview", load: loadBilling },
      };
      const paneBox = h("div");
      const pick = k => { OG.qs.set({ pane: k === "usage" ? null : k }); paneBox.replaceChildren(OG.loading()); panes[k].load(paneBox); };
      const initial = panes[(location.hash || "").slice(1)] ? location.hash.slice(1) : panes[query.pane] ? query.pane : "usage";
      const paneTabs = OG.tabs(Object.entries(panes).map(([k, v]) => [k, v.label]), initial, pick);
      const acct = h("section", { class: "sec", id: "account", style: "margin-top:22px" }, h("h2", { class: "sec-h" }, "Account details"),
        h("p", { class: "note", style: "margin:0 0 6px" }, "Usage, limits, provider credentials and billing. Not needed to create your first key."), paneTabs, paneBox);
      // old anchors (/keys#credentials, /keys#billing) still land here
      acct.append(h("span", { id: "credentials" }), h("span", { id: "billing" }));
      root.append(acct);

      async function loadUsage(box) {
        const seg = OG.seg([[24, "24H"], [168, "7D"], [720, "30D"]], st.hours, v => { st.hours = Number(v); OG.qs.set({ h: v === 24 ? null : v }); loadUsage(box); });
        try {
          const u = await ctx.api("/v1/usage", { params: { hours: st.hours }, nocache: true, slot: "keys-usage" });
          const names = new Map(st.keys.map(k => [k.id, k]));
          const keyName = id => { const k = names.get(id); return k ? h("span", {}, k.name, " ", h("span", { class: "mono dim" }, k.prefix + "…")) : h("span", { class: "mono" }, "#" + id); };
          box.replaceChildren(...[
            h("div", { class: "bar" }, seg, h("span", { class: "dim", style: "font-size:11.5px" }, `since ${fmt.dateTime(u.since)} · kept ${u.retention_days} days · only API-key requests are counted (not this website)`)),
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
        } catch (e) { if (!e.stale) box.replaceChildren(OG.error(e, () => loadUsage(box))); }
      }

      function drawLimits(box) {
        const me = st.me || {};
        const defs = me.rate_limit_defaults || (me.rate_limits && me.rate_limits.defaults) || null;
        const cls = defs && (defs.classes || defs);
        const lim = k => {
          const x = cls && cls[k];
          const n = x == null ? null : typeof x === "object" ? x.limit_per_minute ?? x.per_minute ?? x.limit : x;
          return n != null ? h("span", { class: "mono" }, fmt.num(n) + "/min") : h("span", { class: "dim" }, "-");
        };
        box.replaceChildren(h("div", { class: "dx-wins" },
          h("div", { class: "dx-win" }, h("div", { class: "lbl" }, "Rate limits per key"),
            h("table", { class: "dx-tbl" }, h("tbody", {},
              h("tr", {}, h("th", {}, "read"), h("td", {}, "GET / HEAD / OPTIONS"), h("td", {}, lim("read"))),
              h("tr", {}, h("th", {}, "write"), h("td", {}, "other methods"), h("td", {}, lim("write"))),
              h("tr", {}, h("th", {}, "execute"), h("td", {}, "non-GET under /v1/route, /v1/deployments"), h("td", {}, lim("execute"))))),
            p("Each class refills continuously, so a full minute's budget can burst. Responses carry ", c("X-RateLimit-Remaining"), "; over budget is ", h("b", {}, "429"), " with ", c("Retry-After"), ". A key sees its live budget in ", c("GET /v1/me"), ". This website (the operator) is not rate limited.")),
          h("div", { class: "dx-win" }, h("div", { class: "lbl" }, "Authentication"),
            code("Authorization: Bearer opg_live_…", "http header"),
            p("Keys are 256-bit random secrets prefixed ", c("opg_live_"), "; the first 13 characters are kept so you can tell keys apart. OpenGrid stores only a keyed hash, so a lost key cannot be recovered, only replaced."),
            p(h("a", { class: "lnk", href: "/api#auth" }, "Authentication in the API reference →")))));
      }

      async function loadCreds(box) {
        let cr, caps;
        try { [cr, caps] = await Promise.all([ctx.api("/v1/credentials", { nocache: true }), ctx.api("/v1/capabilities").catch(() => [])]); }
        catch (e) { box.replaceChildren(OG.error(e, () => loadCreds(box))); return; }
        const capOf = new Map(caps.map(x => [x.provider, x]));
        const managed = cr.opengrid_managed_providers || [];
        const provs = caps.length ? caps.map(x => x.provider) : OG.data.providers().map(x => x.name);
        const provSel = h("select", { class: "field", "aria-label": "Provider" },
          h("option", { value: "" }, "Provider…"),
          provs.slice().sort((a, b) => ((capOf.get(b) || {}).level_implemented >= 2) - ((capOf.get(a) || {}).level_implemented >= 2) || a.localeCompare(b)).map(pv => {
            const lvl = (capOf.get(pv) || {}).level_implemented;
            return h("option", { value: pv }, OG.providerName(pv) + (lvl >= 2 ? " (OpenGrid can provision)" : " (no adapter: stored, not used)"));
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
          const existing = (cr.byo || []).find(x => x.provider === provSel.value && x.state === "active");
          if (existing) {
            const ok = await confirm({ title: `Replace your ${OG.providerName(provSel.value)} credential?`, confirm: "Replace", body: h("p", {}, `An account holds one active credential per provider; ${existing.hint} will be revoked.`) });
            if (!ok) return;
          }
          try {
            await ctx.api("/v1/credentials", { method: "POST", body: { provider: provSel.value, secret: secret.value, label: label.value.trim() || null } });
            secret.value = ""; label.value = ""; err.replaceChildren(); loadCreds(box);
          } catch (e2) { err.replaceChildren(OG.error(e2)); }
        } }, provSel, secret, label, h("button", { class: "btn pri", type: "submit" }, "Store encrypted"), hint);
        const byoTbl = OG.table({
          columns: [
            { key: "provider", label: "Provider", fmt: v => OG.providerLink(v) },
            { key: "label", label: "Label", fmt: v => v || h("span", { class: "dim" }, "-") },
            { key: "hint", label: "Secret", cls: "mono", fmt: v => v + " (masked)" },
            { key: "state", label: "State", fmt: v => OG.badge(v, STATE_TONE[v]) },
            { key: "created_at", label: "Added", num: true, fmt: when },
            { key: "last_used_at", label: "Last used by routing", num: true, fmt: ago },
            { key: "act", label: "", sort: false, csv: false, fmt: (v, r) => r.state === "active" ? h("button", { class: "btn sm ky-rev", type: "button", onclick: () => delCred(r, box) }, "Delete") : "" },
          ], rows: cr.byo || [], compact: true, csv: false, empty: "No bring-your-own credentials: routing uses OpenGrid-managed credentials where configured.",
        });
        box.replaceChildren(
          p("Only relevant once real provisioning is enabled. ", h("b", {}, "OpenGrid-managed"), " (default): OpenGrid launches on its own provider accounts and bills provider cost plus its fee lines. ",
            h("b", {}, "Bring your own"), ": store your provider key; the provider bills you directly and OpenGrid charges only its fee lines. Encrypted at rest; never returned by any API; only the last 4 characters are shown."),
          p(h("span", { class: "dim" }, "Managed credentials configured on this server: "), managed.length ? managed.map(x => OG.providerName(x)).join(", ") : h("b", { class: "warn-t" }, "none"), "."),
          st.me && st.me.principal === "operator" ? p(h("span", { class: "warn-t" }, "Routes started from this website run as the operator and use OpenGrid-managed credentials; BYO credentials apply to this account's API keys.")) : null,
          byoTbl, h("div", { class: "box box-p ky-add" }, h("div", { class: "lbl" }, "Add a credential"), form, err));
      }
      async function delCred(r, box) {
        const ok = await confirm({ title: `Delete your ${OG.providerName(r.provider)} credential (${r.hint})?`, danger: true, confirm: "Delete",
          body: h("p", {}, "Routing falls back to OpenGrid-managed credentials for this provider (if configured). Deployments already started with it keep running, but OpenGrid can no longer manage them.") });
        if (!ok) return;
        try { await ctx.api("/v1/credentials/" + r.id, { method: "DELETE" }); loadCreds(box); } catch (e) { box.append(OG.error(e)); }
      }

      async function loadBilling(box) {
        const [pol, use, inv] = await Promise.all([
          ctx.api("/v1/billing/policy", { full: true }).catch(e => ({ error: e })),
          ctx.api("/v1/billing/usage").catch(e => ({ error: e })),
          ctx.api("/v1/billing/invoices").catch(e => ({ error: e }))]);
        if (pol.error && use.error && inv.error) { box.replaceChildren(OG.error(pol.error, () => loadBilling(box))); return; }
        const comp = cc => {
          const k = cc.kind;
          const txt = k === "buyer_fee_pct" ? `${cc.pct}% of provider cost` : k === "flat_per_gpu_hour" ? `${money(cc.usd)} per GPU-hour` :
            k === "spread" ? (cc.pct != null ? `${cc.pct}% markup` : `${money(cc.usd_per_gpu_hour)}/GPU-h markup`) : k === "subscription" ? `${money(cc.usd_per_month)} per month` :
            k === "data_api" ? `${money(cc.usd_per_1k_requests)} per 1k requests after ${fmt.num(cc.free_requests || 0)} free` : JSON.stringify(cc);
          return h("li", {}, h("span", { class: "mono" }, k), " ", txt, cc.label ? h("span", { class: "dim" }, " · " + cc.label) : null, cc.applies_to ? h("span", { class: "dim" }, " · applies to " + cc.applies_to.join(", ")) : null);
        };
        const pd = pol.error ? null : pol.data;
        const policyEl = pol.error ? OG.error(pol.error) : pd ? h("div", {},
          h("div", {}, h("b", {}, pd.name), h("span", { class: "dim" }, ` · v${pd.version} · ${pd.scope} · in force since ${fmt.date(pd.effective_from)}`)),
          h("ul", { class: "ky-comp" }, (pd.components || []).map(comp)), pd.note ? h("p", { class: "note" }, pd.note) : null)
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
            { key: "charges", label: "Charge lines", sort: false, csv: false, cls: "wrap dim", fmt: v => (v || []).map(x => `${x.kind} ${money(x.amount_usd)}`).join(" · ") }],
            rows: use.records, compact: true, csv: "opengrid-billing-usage.csv", sort: { key: "period_start", dir: "desc" } })) : OG.empty("No metered compute: usage is recorded only for real deployments.");
        const invs = inv.error ? null : inv.invoices || [];
        const invEl = inv.error ? OG.error(inv.error) : invs.length ? h("div", {}, invs.map(i => h("details", { class: "box ky-inv" },
          h("summary", {}, h("b", { class: "mono" }, i.period), OG.badge(i.status, i.status === "draft" ? "warn" : ""), h("span", { class: "mono" }, "subtotal " + money(i.subtotal_usd)),
            h("span", { class: "mono" }, "credits " + money(-i.credits_usd)), h("b", { class: "mono" }, "total " + money(i.total_usd)), h("span", { class: "dim" }, "updated " + fmt.dateTime(i.updated_at))),
          OG.table({ columns: [{ key: "kind", label: "Kind", fmt: v => OG.badge(v) }, { key: "description", label: "Line", cls: "wrap" }, { key: "amount_usd", label: "USD", num: true, fmt: v => money(v) }],
            rows: i.lines || [], compact: true, csv: false })))) : OG.empty("No invoices. Drafts are built by the operator per month from recorded usage.");
        const credits = inv.error ? [] : inv.credits || [];
        box.replaceChildren(
          p(OG.badge("preview", "warn"), " ", h("b", {}, "No payments are processed."), " Invoices are drafts generated from recorded usage (", OG.kindBadge("transaction"), " data); provider cost and OpenGrid's fees are separate lines. ", h("a", { class: "lnk", href: "/methodology/billing" }, "Billing method →")),
          h("div", { class: "cols-2" },
            h("div", {}, h("div", { class: "lbl" }, "Fee policy in force"), h("div", { class: "box box-p" }, policyEl)),
            h("div", {}, h("div", { class: "lbl" }, "Credits"), credits.length ? OG.table({ columns: [{ key: "amount_usd", label: "Amount", num: true, fmt: v => money(v) }, { key: "remaining_usd", label: "Remaining", num: true, fmt: v => money(v) }, { key: "reason", label: "Reason", cls: "wrap dim" }, { key: "expires_at", label: "Expires", num: true, fmt: v => v ? fmt.date(v) : "never" }], rows: credits, compact: true, csv: false }) : OG.empty("No credits."))),
          h("div", { class: "lbl", style: "margin-top:10px" }, "Metered usage"), usageEl,
          h("div", { class: "lbl", style: "margin-top:10px" }, "Invoices (drafts)"), invEl);
      }

      // scope catalogue: GET /v1/scopes (public), else /v1/me scopes_catalog, else the built-in fallback
      const scopesApi = await ctx.api("/v1/scopes").catch(() => null);
      if (!ctx.alive()) return;
      SCOPES = scopeRows(scopesApi) || scopeRows(me0.scopes_catalog) || FALLBACK_SCOPES;
      drawCreate();
      await loadKeys();
      if (!ctx.alive()) return;
      pick(initial);
      if (panes[(location.hash || "").slice(1)]) acct.scrollIntoView();
    },
  });
})();
