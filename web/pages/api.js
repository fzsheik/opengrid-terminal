/* /api — public API docs, generated from the live /openapi.json and grouped by domain, plus auth (Bearer opg_
   keys, scopes), the {data, meta} envelope, data kinds / price concepts, rate limits, an example
   /v1/route/preview call, and "try it" for GET endpoints (runs in this browser with the page's own session). */
(() => {
  const { h, fmt } = OG;
  // [group, anchor, path prefixes, default scope]
  const GROUPS = [
    ["Market data", "ep-market-data", ["/v1/overview", "/v1/gpus", "/v1/markets", "/v1/providers", "/v1/hardware", "/v1/compare", "/v1/history", "/v1/heatmaps", "/v1/regions", "/v1/spreads", "/v1/opportunities", "/v1/trust"], "data:read"],
    ["Indices", "ep-indices", ["/v1/indices"], "data:read"],
    ["Events", "ep-events", ["/v1/events", "/v1/timeline", "/v1/tape"], "data:read"],
    ["News", "ep-news", ["/v1/news"], "data:read"],
    ["Routing", "ep-routing", ["/v1/capabilities", "/v1/best", "/v1/route"], null],
    ["Deployments", "ep-deployments", ["/v1/deployments"], null],
    ["Accounts", "ep-accounts", ["/v1/me", "/v1/keys", "/v1/usage", "/v1/credentials", "/v1/admin"], null],
    ["Billing", "ep-billing", ["/v1/billing"], "billing:read"],
    ["Watchlists / Alerts", "ep-watchlists-alerts", ["/v1/watchlists", "/v1/alerts"], "watchlists"],
    ["Ops & reference", "ep-ops", ["/v1/ops", "/v1/methodology"], null],
  ];
  // Scopes as enforced in api/*.py (the OpenAPI schema does not carry them).
  function scopeOf(method, path, dflt) {
    if (path.startsWith("/v1/admin")) return "admin";
    if (path === "/v1/route/preview" || (path.startsWith("/v1/route/") && method === "GET")) return "route:preview";
    if (path === "/v1/route") return "route:execute";
    if (path.startsWith("/v1/capabilities") || path.startsWith("/v1/best")) return "data:read";
    if (path.startsWith("/v1/deployments")) return method === "GET" ? "deployments:read" : "deployments:write";
    if (path.startsWith("/v1/credentials") || ((path.startsWith("/v1/keys")) && method !== "GET")) return "account:manage";
    if (["/v1/me", "/v1/keys", "/v1/usage"].includes(path)) return "any key";
    return dflt;
  }
  const groupOf = path => GROUPS.find(g => g[2].some(p => path === p || path.startsWith(p + "/") || path.startsWith(p + "?"))) || null;
  const EXAMPLE = { gpu: "h100-80gb-sxm5", count: 8, region: "US", max_price_per_gpu_hour: 2.5, duration_hours: 24, mode: "BALANCED" };

  OG.page("/api", {
    title: "API docs",
    async mount(el, params, query, ctx) {
      const root = h("div", { class: "pg-api" });
      el.append(root);
      const base = (OG.boot && OG.boot.base_url) || location.origin;
      root.append(OG.head("API", "One OpenGrid API key for market data, indices, routing, deployments and alerts · JSON over HTTPS",
        h("a", { class: "btn", href: "/docs", rel: "external" }, "Interactive docs ↗"), h("a", { class: "btn", href: "/openapi.json", rel: "external" }, "openapi.json"), h("a", { class: "btn pri", href: "/keys" }, "Get a key →")));
      const toc = h("nav", { class: "api-toc box" });
      const body = h("div", { class: "api-body" });
      root.append(h("div", { class: "api-grid" }, toc, body));

      const curl = `curl -X POST ${base}/v1/route/preview \\
  -H "Authorization: Bearer $OPENGRID_KEY" \\
  -H "Content-Type: application/json" \\
  -d '${JSON.stringify(EXAMPLE)}'`;
      const copyBtn = text => OG.copy(text);
      const sec = (id, title, ...kids) => h("section", { class: "sec api-sec", id }, h("h2", { class: "sec-h" }, title), kids);

      body.append(
        sec("auth", "Authentication & scopes",
          h("div", { class: "cols-2" },
            h("div", {},
              h("pre", { class: "w4-code" }, `Authorization: Bearer opg_live_…`),
              h("p", { class: "note" }, "Every /v1 endpoint needs a key (or the site's own session, which acts as the operator). Create and revoke keys on ", h("a", { class: "lnk", href: "/keys" }, "API keys"), ". The secret is shown once; OpenGrid stores only a keyed hash."),
              h("p", { class: "note" }, h("b", {}, "401"), " missing / invalid / revoked / expired key · ", h("b", {}, "403"), " key lacks the scope, or account suspended · ", h("b", {}, "429"), " over the rate limit.")),
            h("table", { class: "dep-kv api-scopes" }, [
              ["data:read", "market data, indices, events, news, capabilities, best execution"], ["route:preview", "dry-run routing decisions and audit records"],
              ["route:execute", "provision compute through OpenGrid — spends money"], ["deployments:read", "your deployments"], ["deployments:write", "stop / terminate deployments"],
              ["watchlists", "watchlists and alerts"], ["billing:read", "usage and draft invoices"], ["account:manage", "keys and BYO provider credentials"], ["admin", "operator functions"],
            ].map(([s, d]) => h("tr", {}, h("th", { class: "mono" }, s), h("td", { class: s === "route:execute" ? "warn-t" : "" }, d)))))),
        sec("envelope", "Response envelope",
          h("div", { class: "cols-2" },
            h("pre", { class: "w4-code" }, JSON.stringify({ data: { "…": "the payload" }, meta: { as_of: "2026-10-07T01:08:25Z", kind: "inferred", methodology: "/methodology/routing", price_concept: "observed_market_price", note: "…" } }, null, 2)),
            h("div", {},
              h("p", { class: "note" }, h("code", { class: "mono" }, "meta.as_of"), " is when the response was built; ", h("code", { class: "mono" }, "meta.kind"), " is the data kind; ", h("code", { class: "mono" }, "meta.methodology"), " links the method behind every number. Lists page with ", h("code", { class: "mono" }, "limit / offset"), " where offered."),
              h("p", { class: "note" }, "A statistic OpenGrid cannot compute honestly is ", h("code", { class: "mono" }, "null"), " with a ", h("code", { class: "mono" }, "reason"), " — never a placeholder number."),
              h("p", { class: "note" }, "Errors: ", h("code", { class: "mono" }, '{"detail": "…"}'), " with the HTTP status.")))),
        sec("kinds", "Data kinds & price concepts",
          h("div", { class: "cols-2" },
            h("table", { class: "dep-kv" },
              h("tr", {}, h("th", {}, OG.kindBadge("observed")), h("td", {}, "read directly from a provider and normalized")),
              h("tr", {}, h("th", {}, OG.kindBadge("inferred")), h("td", {}, "derived from observed data by a stated rule (rankings, indices)")),
              h("tr", {}, h("th", {}, OG.kindBadge("estimated")), h("td", {}, "a model or assumption fills a gap")),
              h("tr", {}, h("th", {}, OG.kindBadge("transaction")), h("td", {}, "what an OpenGrid execution actually quoted or paid"))),
            h("table", { class: "dep-kv" },
              h("tr", {}, h("th", {}, OG.conceptBadge("list")), h("td", {}, "the provider's published catalogue price")),
              h("tr", {}, h("th", {}, OG.conceptBadge("observed")), h("td", {}, "OpenGrid's normalized reading of a live listing")),
              h("tr", {}, h("th", {}, OG.conceptBadge("quote")), h("td", {}, "the price a route would transact at (preview: from the observed listing; route: from the provider API where available)")),
              h("tr", {}, h("th", {}, OG.conceptBadge("execution")), h("td", {}, "what the provider charged a real deployment")))),
          h("p", { class: "note" }, "The four price concepts are never mixed in one field. ", h("a", { class: "lnk", href: "/methodology/data-kinds" }, "Data kinds methodology →"))),
        sec("limits", "Rate limits",
          h("p", { class: "note" }, "Per key and per request class — ", h("b", {}, "read"), " (GET/HEAD/OPTIONS), ", h("b", {}, "write"), " (other methods), ", h("b", {}, "execute"),
            " (non-GET under /v1/route and /v1/deployments) — as a token bucket refilled continuously, so a full minute's budget can burst. Each response carries ",
            h("code", { class: "mono" }, "X-RateLimit-Limit"), ", ", h("code", { class: "mono" }, "-Remaining"), ", ", h("code", { class: "mono" }, "-Reset"), ", ", h("code", { class: "mono" }, "-Class"),
            "; a 429 adds ", h("code", { class: "mono" }, "Retry-After"), ". ", h("code", { class: "mono" }, "GET /v1/me"), " shows the key's current budget.")),
        sec("example", "Example: route preview",
          h("p", { class: "note" }, "“I need 8 H100s in the US under $2.50/hr for a day.” Preview is free, never calls a provider, and returns the selected listing, its quote, alternatives with per-factor scores, exclusions with reasons, and an audit id (", h("code", { class: "mono" }, "GET /v1/route/{route_request_id}"), ")."),
          h("div", { class: "api-ex" }, h("pre", { class: "w4-code" }, curl), copyBtn(curl)),
          h("p", { class: "note" }, h("a", { class: "lnk", href: "/route" + OG.qs.stringify({ gpu: EXAMPLE.gpu, count: 8, region: "US", max: 2.5, dur: 24 }) }, "Run this preview in the terminal →"),
            " · ", h("code", { class: "mono" }, "POST /v1/route"), " with the same body (scope route:execute) checks, quotes and — only where live provisioning is enabled — provisions; otherwise it returns ", h("code", { class: "mono" }, "status: not_provisioned"), ".")));

      const epHolder = h("div", {}, OG.loading("Reading /openapi.json…"));
      body.append(epHolder);
      let spec;
      try { spec = await ctx.api("/openapi.json"); } catch (e) { epHolder.replaceChildren(OG.error(e)); return; }
      const ops = [];
      for (const [path, item] of Object.entries(spec.paths || {})) {
        if (!path.startsWith("/v1")) continue;
        for (const [method, op] of Object.entries(item)) {
          if (!["get", "post", "put", "patch", "delete"].includes(method)) continue;
          const g = groupOf(path);
          ops.push({ method: method.toUpperCase(), path, op, group: g ? g[0] : "Other", anchor: g ? g[1] : "ep-other", scope: scopeOf(method.toUpperCase(), path, g ? g[3] : null) });
        }
      }
      const order = [...GROUPS.map(g => g[0]), "Other"];
      const groups = order.map(name => ({ name, anchor: (GROUPS.find(g => g[0] === name) || [0, "ep-other"])[1], ops: ops.filter(o => o.group === name).sort((a, b) => a.path.localeCompare(b.path) || a.method.localeCompare(b.method)) })).filter(g => g.ops.length);
      const filter = h("input", { class: "field", type: "search", placeholder: "Filter endpoints", "aria-label": "Filter endpoints", value: query.q || "" });
      toc.replaceChildren(h("div", { class: "eyebrow" }, "Guide"),
        ...[["auth", "Auth & scopes"], ["envelope", "Envelope"], ["kinds", "Data kinds"], ["limits", "Rate limits"], ["example", "Example"]].map(([a, l]) => h("a", { href: "#" + a, class: "api-tl" }, l)),
        h("div", { class: "eyebrow", style: "margin-top:10px" }, `Endpoints · ${ops.length}`),
        ...groups.map(g => h("a", { href: "#" + g.anchor, class: "api-tl" }, g.name, h("span", { class: "dim mono" }, g.ops.length))));
      const list = h("div");
      epHolder.replaceChildren(h("div", { class: "bar api-fbar" }, h("span", { class: "sec-h", style: "margin:0" }, "Endpoints"), filter, h("span", { class: "dim", style: "font-size:11.5px" }, `${ops.length} operations in ${spec.info ? spec.info.title + " " + (spec.info.version || "") : "the schema"} · GET endpoints have Try it`)), list);
      function draw() {
        const q = filter.value.trim().toLowerCase();
        list.replaceChildren(...groups.map(g => {
          const rows = g.ops.filter(o => !q || o.path.toLowerCase().includes(q) || (o.op.summary || "").toLowerCase().includes(q));
          if (!rows.length) return null;
          return h("section", { class: "sec api-sec", id: g.anchor }, h("h2", { class: "sec-h" }, g.name, " ", h("span", { class: "dimmer" }, rows.length)), h("div", { class: "api-ops box" }, rows.map(opRow)));
        }).filter(Boolean));
        if (!list.children.length) list.append(OG.empty("No endpoint matches."));
      }
      filter.addEventListener("input", () => { OG.qs.set({ q: filter.value || null }); draw(); });

      function opRow(o) {
        const prm = (o.op.parameters || []);
        const det = h("details", { class: "api-op" },
          h("summary", {}, h("span", { class: "api-m m-" + o.method.toLowerCase() }, o.method), h("span", { class: "mono api-p" }, o.path),
            h("span", { class: "api-s" }, o.op.summary || ""), h("span", { class: "spacer" }), o.scope ? h("span", { class: "badge" + (o.scope === "route:execute" ? " warn" : o.scope === "admin" ? " bad" : "") }, o.scope) : null));
        det.addEventListener("toggle", () => { if (det.open && det.children.length === 1) det.append(opBody(o, prm)); }, { once: false });
        return det;
      }
      function opBody(o, prm) {
        const desc = o.op.description && o.op.description !== o.op.summary ? h("p", { class: "note" }, o.op.description) : null;
        const ptbl = prm.length ? h("table", { class: "dep-kv api-params" }, prm.map(p => h("tr", {}, h("th", { class: "mono" }, p.name, p.required ? h("span", { class: "down" }, " *") : null),
          h("td", { class: "dim" }, `${p.in} · ${schemaText(p.schema)}${p.schema && p.schema.default != null ? " · default " + p.schema.default : ""}`)))) : h("p", { class: "note" }, "No parameters.");
        const bodyRef = o.op.requestBody && o.op.requestBody.content && o.op.requestBody.content["application/json"] && o.op.requestBody.content["application/json"].schema;
        const bodyEl = bodyRef ? h("div", {}, h("div", { class: "lbl" }, "JSON body"), h("pre", { class: "w4-code" }, bodySketch(bodyRef))) : null;
        return h("div", { class: "api-ob" }, desc, ptbl, bodyEl, o.method === "GET" ? tryIt(o, prm) : h("p", { class: "note" }, "Try it is offered for GET endpoints only; use curl with your key for writes."));
      }
      const resolveRef = s => { let n = 0; while (s && s.$ref && n++ < 5) s = s.$ref.split("/").slice(1).reduce((a, k) => a && a[k], spec); return s; };
      function schemaText(s) {
        s = resolveRef(s) || {};
        if (s.anyOf) return s.anyOf.map(schemaText).filter(t => t !== "null").join(" | ");
        if (s.enum) return s.enum.join(" | ");
        let t = s.type || "any";
        if (s.minimum != null || s.maximum != null) t += ` [${s.minimum ?? ""}..${s.maximum ?? ""}]`;
        if (s.pattern) t += " " + s.pattern;
        return t;
      }
      function bodySketch(s) {
        s = resolveRef(s) || {};
        const props = s.properties || {};
        const req = new Set(s.required || []);
        const lines = Object.entries(props).map(([k, v]) => `  "${k}"${req.has(k) ? "*" : ""}: ${schemaText(v)}${(resolveRef(v) || {}).description ? "  // " + resolveRef(v).description : ""}`);
        return lines.length ? "{\n" + lines.join(",\n") + "\n}\n* required" : schemaText(s);
      }
      function tryIt(o, prm) {
        const inputs = prm.map(p => {
          const sample = p.name === "gpu" ? "h100-80gb-sxm5" : p.name === "provider" ? "lambda" : p.name === "index_id" ? "h100-class" : "";
          const i = h("input", { class: "field", placeholder: p.schema && p.schema.default != null ? String(p.schema.default) : p.name, value: p.in === "path" ? sample : "", "aria-label": p.name });
          return [p, i];
        });
        const out = h("div");
        const urlEl = h("code", { class: "mono api-url" });
        const build = () => {
          let path = o.path; const q = {};
          for (const [p, i] of inputs) { const v = i.value.trim(); if (p.in === "path") path = path.replace("{" + p.name + "}", encodeURIComponent(v || "{" + p.name + "}")); else if (v) q[p.name] = v; }
          return path + OG.qs.stringify(q);
        };
        const upd = () => { urlEl.textContent = "GET " + build(); };
        inputs.forEach(([, i]) => i.addEventListener("input", upd)); upd();
        const send = async () => {
          const url = build();
          if (/\{[^}]+\}/.test(url)) { out.replaceChildren(OG.error({ message: "fill every path parameter" })); return; }
          out.replaceChildren(OG.loading("GET " + url));
          const t0 = performance.now();
          try {
            const r = await fetch(url, { credentials: "same-origin", headers: { accept: "application/json" } });
            const txt = await r.text();
            let pretty = txt; try { pretty = JSON.stringify(JSON.parse(txt), null, 2); } catch (e) {}
            const ms = Math.round(performance.now() - t0);
            const cut = pretty.length > 60000;
            out.replaceChildren(h("div", { class: "api-res-h" }, OG.badge("HTTP " + r.status, r.ok ? "good" : "bad"), h("span", { class: "dim mono" }, `${ms} ms · ${fmt.compact(txt.length)} bytes${cut ? " · first 60k shown" : ""}`),
              h("span", { class: "spacer" }), copyBtn(`curl -H "Authorization: Bearer $OPENGRID_KEY" "${base}${url}"`)),
              h("pre", { class: "w4-code api-res" }, cut ? pretty.slice(0, 60000) + "\n…" : pretty));
          } catch (e) { out.replaceChildren(OG.error({ message: "network error" })); }
        };
        return h("div", { class: "api-try" }, h("div", { class: "lbl" }, "Try it ", h("span", { class: "dimmer" }, "· runs in this browser as the signed-in operator; your key is not needed here")),
          inputs.length ? h("div", { class: "api-ti" }, inputs.map(([p, i]) => h("label", { class: "api-tf" }, h("span", { class: "mono dim" }, p.name + (p.required ? "*" : "")), i))) : null,
          h("div", { class: "bar" }, h("button", { class: "btn pri", type: "button", onclick: send }, "Send"), urlEl), out);
      }
      draw();
      if (location.hash) { const t = document.getElementById(location.hash.slice(1)); if (t) t.scrollIntoView(); }
    },
  });
})();
