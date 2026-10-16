/* /api: the API reference.

   Everyone sees a curated reference: quickstart, authentication (which endpoints are public and which need a key),
   every PUBLIC endpoint (PUBLIC catalogue below, checked against the server's real public routes by
   tests/test_api_docs.py) with a "Try it" that runs in the browser without a key, the authenticated endpoints by
   scope (summaries only), the {data, meta} envelope, data kinds and rate limits.
   The full machine-readable schema (/openapi.json, /docs) is deliberately operator-only (api/pages.py denies
   both; it lists admin and ops routes), so it is loaded and rendered only when the viewer is the operator. */
(() => {
  const { h, fmt } = OG;
  const KEY_VAR = "OPENGRID_API_KEY";
  // [group, anchor, [[method, path, summary, params ("name*" = required), sample path values]]]
  const PUBLIC = [
    ["GPUs & hardware", "pub-gpus", [
      ["GET", "/v1/gpus", "Every canonical GPU: hardware summary and current market", "priced_only, limit, offset"],
      ["GET", "/v1/gpus/{gpu}", "One GPU: full spec, dispersion, capability pricing, alternatives", "gpu*, days"],
      ["GET", "/v1/families", "GPU families and their canonical variants", ""],
      ["GET", "/v1/families/{family}", "One GPU family: every variant side by side", "family*"],
      ["GET", "/v1/hardware", "Vendor peak specs for every canonical GPU (dense figures)", ""],
      ["GET", "/v1/compare", "Side by side: two GPUs or two providers", "a*, b*, days"],
    ]],
    ["Market prices", "pub-markets", [
      ["GET", "/v1/overview", "Homepage market overview in one payload", ""],
      ["GET", "/v1/markets/{gpu}", "Current market for one GPU: dispersion, efficiency score, providers", "gpu*"],
      ["GET", "/v1/markets/{gpu}/context", "Where the GPU's price sits in its 30d / 90d history", "gpu*, segment"],
      ["GET", "/v1/markets/{gpu}/dispersion", "Dispersion of one GPU over time", "gpu*, days, resolution"],
      ["GET", "/v1/markets/{gpu}/regions", "One GPU by region group: cheapest, median, providers, premium", "gpu*"],
      ["GET", "/v1/history/{gpu}", "One GPU over time: lowest / median / highest / provider count + index", "gpu*, window, resolution, segment"],
      ["GET", "/v1/spreads", "Every GPU ranked by fragmentation and spread", "min_providers"],
      ["GET", "/v1/heatmaps/{kind}", "Heatmap matrix: rows, cols, cells (null = no data)", "kind*, days"],
      ["GET", "/v1/regions", "Region groups and what is listed in each now", ""],
      ["GET", "/v1/opportunities", "Live opportunity monitor", "type, gpu, provider, min_score, limit"],
      ["GET", "/v1/tape", "Ticker: latest price moves and notable events", "gpu, limit"],
      ["GET", "/v1/timeline", "Price timeline for a GPU or provider", "gpu, provider, window, end, segment, move_pct"],
      ["GET", "/v1/timeline/around", "What happened around one moment", "gpu*, at*, window_hours, provider, segment"],
    ]],
    ["Best execution & recommendations", "pub-best", [
      ["GET", "/v1/best/{gpu}", "Best Available Now: transparent best-execution ranking for one GPU", "gpu*, count, region, mode, max_price, limit, strict_region, per_variant"],
      ["POST", "/v1/recommend", "Recommend GPUs for a workload: estimate + ranked fresh market options (read-only)", "JSON body: WorkloadRequirements"],
      ["POST", "/v1/recommend/estimate", "Estimate GPU memory, GPU count and open questions for a workload (pure computation)", "JSON body: WorkloadRequirements"],
      ["POST", "/v1/assistant", "Ask the OpenGrid Assistant (read-only, grounded)", "JSON body: {message, context}"],
      ["GET", "/v1/capabilities", "What each provider's API allows vs what OpenGrid implements", ""],
      ["GET", "/v1/capabilities/{provider}", "One provider's integration capabilities", "provider*"],
    ]],
    ["Providers & data quality", "pub-providers", [
      ["GET", "/v1/providers", "Every provider: meta, coverage, relative value, feed health", "days"],
      ["GET", "/v1/providers/{provider}", "One provider: catalog, pricing vs market, rank history, health", "provider*, days"],
      ["GET", "/v1/providers/{provider}/gpus/{gpu}/context", "One provider's price for a GPU against its own recorded history", "provider*, gpu*, segment"],
      ["GET", "/v1/providers/{provider}/listings/context", "One listing's price against its own recorded history (90 days)", "provider*, listing_id*"],
      ["GET", "/v1/trust/listings", "Listings with their trust block (source, freshness, availability basis)", "gpu, provider, limit, offset"],
      ["GET", "/v1/trust/providers", "Provider feed health (no internal error text)", ""],
    ]],
    ["Indices & fixings", "pub-indices", [
      ["GET", "/v1/indices", "OpenGrid indices: current level and changes", "kind"],
      ["GET", "/v1/indices/{index_id}", "One index: definition, level, constituents, statistics", "index_id*"],
      ["GET", "/v1/indices/{index_id}/history", "Stored index levels over a window", "index_id*, window, resolution"],
      ["GET", "/v1/indices/{index_id}/fixings", "Official fixing history of one index", "index_id*, type, status, from, to, effective, limit, offset"],
      ["GET", "/v1/fixings", "Latest official fixings of every settlement index", ""],
      ["GET", "/v1/fixings/methodology", "Fixing schedule, thresholds, correction windows, change notices", ""],
      ["GET", "/v1/fixings/restatements", "Every restatement: what was corrected, by whom, why", "limit, offset"],
      ["GET", "/v1/fixings/chain/verify", "Check the hash chain over the whole fixings ledger (paged)", "from_seq, limit"],
      ["GET", "/v1/fixings/{fixing_id}", "One fixing: value, flags, full inputs and raw evidence", "fixing_id*"],
      ["GET", "/v1/fixings/{fixing_id}/verify", "Recompute a fixing from its stored inputs; check hashes and links", "fixing_id*"],
    ]],
    ["Events & news", "pub-events", [
      ["GET", "/v1/events", "Market events, newest first", "gpu, provider, type, severity, min_severity, since, until, limit, offset"],
      ["GET", "/v1/events/types", "Event catalogue: triggers, severity rules, data kind", ""],
      ["GET", "/v1/news", "News and signals, with filters", "gpu, provider, region, topic, source, q, since, min_relevance, limit, offset"],
      ["GET", "/v1/news/{item_id}", "One news item", "item_id*"],
      ["GET", "/v1/news/sources", "The news sources OpenGrid reads", ""],
      ["GET", "/v1/news/topics", "News topics", ""],
      ["GET", "/v1/news/families", "News grouped by GPU family", ""],
    ]],
    ["Reference", "pub-ref", [
      ["GET", "/v1/methodology", "Methodology documents: name, title, links", ""],
      ["GET", "/v1/methodology/{name}", "One methodology document as raw markdown", "name*"],
      ["GET", "/v1/scopes", "Every scope an API key can hold, with what it allows", ""],
    ]],
  ];
  const SAMPLE = { gpu: "h100-80gb-sxm5", family: "h100", provider: "lambda", index_id: "h100-class", kind: "gpu-provider-premium", name: "data-kinds", a: "h100-80gb-sxm5", b: "h200-141gb-sxm5" };
  // Authenticated (needs a key): [area, scope, what] (summaries; the operator's full schema lists every route)
  const PRIVATE = [
    ["POST /v1/route/preview · GET /v1/route/{id} · GET /v1/quotes/{id}", "route:preview", "Dry-run routing: the selected listing, its quote, alternatives with per-factor scores and exclusions, plus the audit record. Never calls a provider."],
    ["POST /v1/jobs/preview · GET /v1/jobs/readiness", "route:preview", "Preview a training job and see what blocks a real run today."],
    ["GET /v1/jobs · /v1/jobs/{id} · /logs · /metrics · /artifacts", "deployments:read", "Your jobs: state, progress, logs, metrics and artifacts."],
    ["POST /v1/jobs · POST /v1/jobs/{id}/cancel", "route:execute", "Submit a job from the allowlisted template (sandbox demo, or a real run once enabled, always operator-approved) and cancel one. Idempotency-Key required."],
    ["GET /v1/deployments · /v1/deployments/{id}", "deployments:read", "Your deployments."],
    ["POST /v1/deployments/{id}/stop · /terminate", "deployments:write", "Stop or terminate one of your deployments."],
    ["POST /v1/route", "route:execute", "Route a workload: check, quote, then wait for operator approval. Real provisioning is not enabled yet; otherwise it returns status not_provisioned."],
    ["GET /v1/me · GET /v1/usage", "any key", "Who am I: account, key, scopes and live rate-limit budget; recent usage."],
    ["GET/POST /v1/keys · DELETE /v1/keys/{id}", "account:manage", "Your account's keys: create narrower keys (secret shown once), revoke."],
    ["GET/POST/DELETE /v1/credentials", "account:manage", "Bring-your-own provider credentials (encrypted; never returned)."],
    ["/v1/watchlists · /v1/alerts", "watchlists", "Watchlists and alert rules."],
    ["/v1/billing/usage · /invoices · /policy", "billing:read", "Metered usage and draft invoices (no payments are processed)."],
    ["/v1/reservations", "route:preview / route:execute", "Price-lock requests (records intent only; launches and bills nothing)."],
  ];
  const publicOps = () => PUBLIC.flatMap(([g, , ops]) => ops.map(([m, p]) => m + " " + p));
  OG.apiDocs = { PUBLIC, PRIVATE, publicOps, SAMPLE };
  if (typeof OG.page !== "function" || typeof document === "undefined") return;

  OG.page("/api", {
    title: "API reference",
    async mount(el, params, query, ctx) {
      const root = h("div", { class: "pg-dx pg-api" });
      el.append(root);
      const base = ((OG.boot && OG.boot.base_url) || location.origin).replace(/\/$/, "");
      const D = OG.dx || { code: (t, l) => h("pre", { class: "w4-code" }, t), p: (...k) => h("p", { class: "note" }, k), c: t => h("code", { class: "mono" }, t), call: (t, ...k) => h("div", { class: "note" }, k), accessCall: () => null };
      const { code, p, c } = D;
      const head = h("div");
      const toc = h("nav", { class: "dx-api-toc box", "aria-label": "API reference contents" });
      const body = h("div", { class: "dx-api-body" });
      root.append(head, h("div", { class: "dx-api-grid" }, toc, body));
      const sec = (id, title, ...kids) => h("section", { class: "dx-sec", id, style: "margin-top:0;border-top:0;padding-top:0;margin-bottom:22px" }, h("div", { class: "dx-sec-h" }, h("h2", { class: "dx-h2" }, title)), kids);
      const me = await OG.me();
      if (!ctx.alive()) return;
      const operator = OG.isAdmin(me);
      head.replaceChildren(OG.head("API reference", "JSON over HTTPS. Public market data and recommendations need no key; everything else needs an OpenGrid API key.",
        h("a", { class: "btn", href: "/developers" }, "Developer guide"),
        operator ? h("a", { class: "btn", href: "/docs", rel: "external" }, "Interactive docs (operator) ↗") : null,
        operator ? h("a", { class: "btn", href: "/openapi.json", rel: "external" }, "openapi.json (operator)") : null));

      const curlPub = `curl -s "${base}/v1/best/h100?mode=CHEAPEST&limit=1"`;
      const curlKey = `export ${KEY_VAR}=<your key>
curl -s "${base}/v1/me" -H "Authorization: Bearer $${KEY_VAR}"`;
      const curlPreview = `curl -s -X POST "${base}/v1/route/preview" \\
  -H "Authorization: Bearer $${KEY_VAR}" \\
  -H "Content-Type: application/json" \\
  -d '{"gpu": "h100-80gb-sxm5", "count": 8, "region": "US", "max_price_per_gpu_hour": 2.5, "duration_hours": 24, "mode": "BALANCED"}'`;
      const pubList = h("div");

      body.append(
        sec("quickstart", "Quickstart",
          p("No key, no sign-up: the cheapest fresh, in-stock listing of each H100 variant right now."), code(curlPub, "curl", "GET /v1/best/{gpu} · public"),
          p("Look at ", c("data.by_variant[].top[0]"), ": provider, region and ", c("price_per_gpu_hour"), " (an observed list price, not a quote). More first steps, including GPU recommendations for your own project: ", h("a", { class: "lnk", href: "/developers#quickstart" }, "Developer quickstart →"))),
        sec("auth", "Authentication",
          h("div", { class: "dx-scroll" }, h("table", { class: "dx-tbl" },
            h("thead", {}, h("tr", {}, h("th", {}, ""), h("th", {}, "Public"), h("th", {}, "Needs an API key"))),
            h("tbody", {},
              h("tr", {}, h("th", {}, "Which endpoints"), h("td", {}, "GET market data (", h("a", { class: "lnk", href: "#public" }, "every public endpoint below"), ") and three read-only computations: ", c("POST /v1/recommend"), ", ", c("/v1/recommend/estimate"), ", ", c("/v1/assistant"), "."),
                h("td", {}, "Routing previews, jobs, deployments, your account, keys, watchlists, billing, reservations (", h("a", { class: "lnk", href: "#private" }, "by scope below"), "), and the hosted MCP endpoint ", c("/mcp"), ".")),
              h("tr", {}, h("th", {}, "How"), h("td", {}, "Just call it. Anonymous requests are rate limited per IP address."), h("td", {}, c("Authorization: Bearer opg_live_…"), ", from the ", c(KEY_VAR), " environment variable. Never in a URL.")),
              h("tr", {}, h("th", {}, "Errors"), h("td", {}, h("b", {}, "429"), " over the anonymous budget (", c("Retry-After"), ")."), h("td", {}, h("b", {}, "401"), " missing, invalid, revoked or expired key · ", h("b", {}, "403"), " the key lacks the scope (the message names it) or the account is suspended · ", h("b", {}, "429"), " over the key's budget."))))),
          code(curlKey, "shell", "check a key"),
          operator ? D.accessCall(me) : D.accessCall(me, { title: "Keys are issued by the OpenGrid team.", lead: "Every public endpoint works without one." })),
        h("section", { class: "dx-sec", id: "public", style: "margin-top:0;border-top:0;padding-top:0;margin-bottom:22px" },
          h("div", { class: "dx-sec-h" }, h("h2", { class: "dx-h2" }, "Public endpoints"), h("span", { class: "dx-sec-sub" }, `${publicOps().length} operations · no key · Try it runs from this browser ${operator ? "as the signed-in operator" : "without a key"}`)),
          h("input", { class: "field", type: "search", placeholder: "Filter public endpoints", "aria-label": "Filter public endpoints", value: query.q || "", style: "max-width:320px;margin-bottom:8px", oninput: e => { OG.qs.set({ q: e.target.value || null }); drawPublic(e.target.value); } }),
          pubList),
        sec("private", "Endpoints that need a key",
          p("Summaries by scope. A key holds only the scopes it was issued with; ask for the ones you need. The full schema with every parameter is available to the operator."),
          h("div", { class: "dx-scroll" }, h("table", { class: "dx-tbl" },
            h("thead", {}, h("tr", {}, h("th", {}, "Endpoints"), h("th", {}, "Scope"), h("th", {}, "What they do"))),
            h("tbody", {}, PRIVATE.map(([a, s, d]) => h("tr", {}, h("td", { class: "mono", style: "white-space:normal" }, a), h("td", {}, OG.badge(s, s.includes("execute") ? "warn" : "")), h("td", {}, d)))))),
          h("h3", { class: "dx-h3" }, "Example: route preview"),
          p("“I need 8 H100s in the US under $2.50/hr for a day.” Free, never calls a provider; returns the selected listing, its quote, alternatives with per-factor scores, exclusions with reasons and an audit id (", c("GET /v1/route/{route_request_id}"), ")."),
          code(curlPreview, "curl", "POST /v1/route/preview · scope route:preview"),
          operator ? p(h("a", { class: "lnk", href: "/route" + OG.qs.stringify({ gpu: "h100-80gb-sxm5", count: 8, region: "US", max: 2.5, dur: 24 }) }, "Open this preview on the Route page →")) : null),
        sec("envelope", "Response envelope",
          h("div", { class: "dx-wins" },
            code(JSON.stringify({ data: { "…": "the payload" }, meta: { as_of: "2026-10-15T01:08:25Z", kind: "observed", methodology: "/methodology/data-kinds", price_concept: "observed_market_price", note: "…" } }, null, 2), "json", "every response"),
            h("div", {},
              p(c("meta.as_of"), " is when the response was built; ", c("meta.kind"), " is the data kind; ", c("meta.methodology"), " links the method behind every number. Lists page with ", c("limit"), " / ", c("offset"), " where offered."),
              p("A statistic OpenGrid cannot compute honestly is ", c("null"), " with a ", c("reason"), ", never a placeholder number."),
              p("Errors: ", c('{"detail": "…"}'), " with the HTTP status.")))),
        sec("kinds", "Data kinds & price concepts",
          h("div", { class: "dx-wins" },
            h("table", { class: "dx-tbl" }, h("tbody", {},
              h("tr", {}, h("th", {}, OG.kindBadge("observed")), h("td", {}, "read directly from a provider and normalized")),
              h("tr", {}, h("th", {}, OG.kindBadge("estimated")), h("td", {}, "a model or assumption fills a gap (memory, GPU count, cost scenarios)")),
              h("tr", {}, h("th", {}, OG.kindBadge("transaction")), h("td", {}, "what an OpenGrid execution actually quoted or paid")))),
            h("table", { class: "dx-tbl" }, h("tbody", {},
              h("tr", {}, h("th", {}, OG.conceptBadge("list")), h("td", {}, "the provider's published catalogue price")),
              h("tr", {}, h("th", {}, OG.conceptBadge("observed")), h("td", {}, "OpenGrid's normalized reading of a live listing")),
              h("tr", {}, h("th", {}, OG.conceptBadge("quote")), h("td", {}, "the price a route would transact at")),
              h("tr", {}, h("th", {}, OG.conceptBadge("execution")), h("td", {}, "what the provider charged a real deployment"))))),
          p("The four price concepts are never mixed in one field. ", h("a", { class: "lnk", href: "/methodology/data-kinds" }, "Data kinds methodology →"))),
        sec("limits", "Rate limits",
          p("Anonymous requests share a per-IP budget. With a key, the budget is per key and per request class: ", h("b", {}, "read"), " (GET/HEAD/OPTIONS), ", h("b", {}, "write"), " (other methods) and ", h("b", {}, "execute"),
            " (non-GET under /v1/route and /v1/deployments), each a token bucket refilled continuously, plus an account-wide budget so extra keys add nothing. Key responses carry ",
            c("X-RateLimit-Limit"), ", ", c("-Remaining"), ", ", c("-Reset"), ", ", c("-Class"), "; a 429 adds ", c("Retry-After"), ". ", c("GET /v1/me"), " shows the key's current budget.")));

      const opsHolder = h("div");
      if (operator) body.append(h("section", { class: "dx-sec", id: "all", style: "margin-top:0;border-top:0;padding-top:0" },
        h("div", { class: "dx-sec-h" }, h("h2", { class: "dx-h2" }, "Full endpoint list"), OG.badge("operator only", "warn")),
        p("Generated from /openapi.json, which only the operator can read. It includes admin and ops routes that are not part of the public API."), opsHolder));

      // ---------- public list + Try it ----------
      function tryIt(m, path, params) {
        if (m !== "GET") return p(h("span", { class: "dim" }, "Try it covers GET endpoints. Use the curl examples for POST: "), h("a", { class: "lnk", href: "/developers#rest" }, "REST examples →"));
        const names = String(params || "").split(",").map(s => s.trim()).filter(Boolean).map(s => ({ name: s.replace(/\*$/, ""), required: /\*$/.test(s), inPath: path.includes("{" + s.replace(/\*$/, "") + "}") }));
        const inputs = names.map(n => [n, h("input", { class: "field", value: n.inPath ? (SAMPLE[n.name] || "") : n.name === "limit" ? "5" : n.required ? (SAMPLE[n.name] || "") : "", placeholder: n.name, "aria-label": n.name })]);
        const urlEl = h("span", { class: "dx-try-url" }), out = h("div", { class: "dx-try-out" });
        const build = () => {
          let u = path; const q = {};
          for (const [n, i] of inputs) { const v = i.value.trim(); if (n.inPath) u = u.replace("{" + n.name + "}", encodeURIComponent(v || "{" + n.name + "}")); else if (v) q[n.name] = v; }
          return u + OG.qs.stringify(q);
        };
        const upd = () => { urlEl.textContent = "GET " + build(); };
        inputs.forEach(([, i]) => i.addEventListener("input", upd)); upd();
        const send = async () => {
          const u = build();
          if (/\{[^}]+\}/.test(u)) { out.replaceChildren(OG.error({ message: "fill every path parameter" })); return; }
          out.replaceChildren(OG.loading("GET " + u));
          const t0 = performance.now();
          try {
            // anonymous visitors: no credentials at all, so this is exactly what a key-less client gets
            const r = await fetch(u, { credentials: operator ? "same-origin" : "omit", headers: { accept: "application/json" } });
            const txt = await r.text();
            let pr = txt; try { pr = JSON.stringify(JSON.parse(txt), null, 2); } catch (e) { /* not JSON */ }
            const cut = pr.length > 60000;
            out.replaceChildren(h("div", { class: "dx-row" }, OG.badge("HTTP " + r.status, r.ok ? "good" : "bad"), h("span", { class: "dim mono" }, `${Math.round(performance.now() - t0)} ms · ${fmt.compact(txt.length)} bytes${cut ? " · first 60k shown" : ""}`),
              OG.copy(`curl -s "${base}${u}"`, { label: "Copy as curl" })),
              r.status === 429 ? p("Anonymous requests are rate limited per IP; wait a minute.") : null,
              code(cut ? pr.slice(0, 60000) + "\n…" : pr, "response", "JSON"));
          } catch (e) { out.replaceChildren(OG.error({ message: "network error" })); }
        };
        return h("div", { class: "dx-try" }, h("div", { class: "lbl" }, "Try it ", h("span", { class: "dimmer" }, operator ? "· runs as the signed-in operator" : "· runs from this browser with no key, like any anonymous client")),
          inputs.length ? h("div", { class: "dx-try-f" }, inputs.map(([n, i]) => h("label", {}, h("span", { class: "mono dim" }, n.name + (n.required ? " *" : "")), i))) : null,
          h("div", { class: "dx-row" }, h("button", { class: "btn pri", type: "button", onclick: send }, "Send"), urlEl), out);
      }
      function epRow(m, path, summary, params) {
        const det = h("details", { class: "dx-ep" },
          h("summary", {}, h("span", { class: "dx-m " + m.toLowerCase() }, m), h("span", { class: "dx-ep-p" }, path), h("span", { class: "dx-ep-s" }, summary), OG.badge("public", "good")));
        det.addEventListener("toggle", () => {
          if (!det.open || det.children.length > 1) return;
          det.append(h("div", { class: "dx-ep-b" }, p(summary), params ? p(h("span", { class: "dim" }, "Parameters: "), c(params), h("span", { class: "dim" }, "  (* required)")) : p(h("span", { class: "dim" }, "No parameters.")), tryIt(m, path, params)));
        });
        return det;
      }
      function drawPublic(q) {
        q = String(q || "").trim().toLowerCase();
        const list = pubList;
        const groups = PUBLIC.map(([g, anchor, ops]) => {
          const rows = ops.filter(([m, path, s]) => !q || path.toLowerCase().includes(q) || s.toLowerCase().includes(q));
          return rows.length ? h("div", { id: anchor, style: "margin-bottom:12px" }, h("h3", { class: "dx-h3" }, g, " ", h("span", { class: "dimmer" }, rows.length)), h("div", { class: "box" }, rows.map(r => epRow(...r)))) : null;
        }).filter(Boolean);
        list.replaceChildren(...(groups.length ? groups : [OG.empty("No public endpoint matches.")]));
      }
      drawPublic(query.q || "");

      toc.replaceChildren(h("div", { class: "eyebrow" }, "Guide"),
        ...[["quickstart", "Quickstart"], ["auth", "Authentication"], ["public", "Public endpoints"], ["private", "Endpoints with a key"], ["envelope", "Envelope"], ["kinds", "Data kinds"], ["limits", "Rate limits"]].map(([a, l]) => h("a", { href: "#" + a }, l)),
        h("div", { class: "eyebrow" }, "Public · " + publicOps().length),
        ...PUBLIC.map(([g, a, ops]) => h("a", { href: "#" + a }, g, h("span", { class: "dim mono" }, ops.length))),
        operator ? h("div", { class: "eyebrow" }, "Operator") : null, operator ? h("a", { href: "#all" }, "Full endpoint list") : null);
      if (location.hash) { const t = document.getElementById(location.hash.slice(1)); if (t) t.scrollIntoView(); }
      if (operator) fullList(opsHolder);

      // ---------- operator: the full schema, as before ----------
      async function fullList(holder) {
        holder.replaceChildren(OG.loading("Reading /openapi.json…"));
        let spec;
        try { spec = await ctx.api("/openapi.json"); } catch (e) { holder.replaceChildren(OG.error(e, () => fullList(holder))); return; }
        if (!ctx.alive()) return;
        const resolveRef = s => { let n = 0; while (s && s.$ref && n++ < 5) s = s.$ref.split("/").slice(1).reduce((a, k) => a && a[k], spec); return s; };
        const schemaText = s => { s = resolveRef(s) || {}; if (s.anyOf) return s.anyOf.map(schemaText).filter(t => t !== "null").join(" | "); if (s.enum) return s.enum.join(" | "); return s.type || "any"; };
        const pub = new Set(publicOps());
        const ops = [];
        for (const [path, item] of Object.entries(spec.paths || {})) {
          if (!path.startsWith("/v1")) continue;
          for (const [m, op] of Object.entries(item)) if (["get", "post", "put", "patch", "delete"].includes(m)) ops.push({ m: m.toUpperCase(), path, op });
        }
        ops.sort((a, b) => a.path.localeCompare(b.path) || a.m.localeCompare(b.m));
        const filter = h("input", { class: "field", type: "search", placeholder: "Filter all endpoints", "aria-label": "Filter all endpoints", style: "max-width:320px;margin-bottom:8px" });
        const list = h("div", { class: "box" });
        const draw = () => {
          const q = filter.value.trim().toLowerCase();
          const rows = ops.filter(o => !q || o.path.toLowerCase().includes(q) || (o.op.summary || "").toLowerCase().includes(q));
          list.replaceChildren(...(rows.length ? rows.map(o => {
            const det = h("details", { class: "dx-ep" }, h("summary", {}, h("span", { class: "dx-m " + o.m.toLowerCase() }, o.m), h("span", { class: "dx-ep-p" }, o.path), h("span", { class: "dx-ep-s" }, o.op.summary || ""),
              pub.has(o.m + " " + o.path) ? OG.badge("public", "good") : o.path.startsWith("/v1/admin") ? OG.badge("admin", "bad") : OG.badge("key")));
            det.addEventListener("toggle", () => {
              if (!det.open || det.children.length > 1) return;
              const prm = o.op.parameters || [];
              det.append(h("div", { class: "dx-ep-b" }, o.op.description && o.op.description !== o.op.summary ? p(o.op.description) : null,
                prm.length ? h("table", { class: "dx-tbl" }, h("tbody", {}, prm.map(x => h("tr", {}, h("td", { class: "mono" }, x.name + (x.required ? " *" : "")), h("td", {}, `${x.in} · ${schemaText(x.schema)}${x.schema && x.schema.default != null ? " · default " + x.schema.default : ""}`))))) : p(h("span", { class: "dim" }, "No parameters.")),
                o.m === "GET" ? tryIt("GET", o.path, prm.map(x => x.name + (x.required ? "*" : "")).join(", ")) : null));
            });
            return det;
          }) : [OG.empty("No endpoint matches.")]));
        };
        filter.addEventListener("input", draw);
        holder.replaceChildren(h("div", { class: "dim", style: "font-size:11.5px;margin-bottom:6px" }, `${ops.length} operations in ${spec.info ? spec.info.title + " " + (spec.info.version || "") : "the schema"}`), filter, list);
        draw();
      }
    },
  });
})();
