/* /api: the API reference.

   Everyone sees a short public reference first: one curl call, then every PUBLIC endpoint (PUBLIC catalogue below,
   checked against the server's real public routes by tests/test_developer_docs.py) with a "Try it" that runs in
   the browser without a key. Folded below: authentication, the endpoints that need a key (by scope), the
   {data, meta} envelope, data kinds and rate limits.
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
      root.append(OG.loading("Loading the API reference…"));
      const base = ((OG.boot && OG.boot.base_url) || location.origin).replace(/\/$/, "");
      const D = OG.dx;
      const { code, p, c, fold } = D;
      const me = await OG.me();
      if (!ctx.alive()) return;
      root.replaceChildren();
      const operator = OG.isAdmin(me);
      root.append(OG.head("API reference", "JSON over HTTPS. The public endpoints below need no key.",
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
      const sec = (id, title, sub, ...kids) => h("section", { class: "dx-sec", id }, h("div", { class: "dx-sec-h" }, h("h2", { class: "dx-h2" }, title), sub ? h("span", { class: "dx-sec-sub" }, sub) : null), kids);
      const pubList = h("div");

      root.append(
        sec("quickstart", "Quickstart", null,
          code(curlPub, "curl", "no key"),
          p("The cheapest in-stock listing of each H100 variant: read ", c("data.by_variant[].top[0]"), " (an observed list price, not a quote). Every response is ", c("{data, meta}"), ". Endpoints not listed below need an API key: ", h("a", { class: "lnk", href: "#auth" }, "authentication"), ".")),
        sec("public", "Public endpoints", `${publicOps().length} · no key · Try it runs ${operator ? "as the signed-in operator" : "from this browser without a key"}`,
          h("input", { class: "field dx-filter", type: "search", placeholder: "Filter public endpoints", "aria-label": "Filter public endpoints", value: query.q || "", oninput: e => { OG.qs.set({ q: e.target.value || null }); drawPublic(e.target.value); } }),
          pubList));

      const opsHolder = h("div");
      const allFold = operator ? fold("all", h("span", {}, "Full endpoint list ", OG.badge("operator only", "warn")),
        p("Generated from /openapi.json, which only the operator can read. It includes admin and ops routes that are not part of the public API."), opsHolder) : null;
      root.append(sec("more", "More", null,
        fold("auth", "Authentication and API keys",
          p("Send a key as ", c("Authorization: Bearer $" + KEY_VAR), ", never in a URL. ", h("b", {}, "401"), " missing, invalid, revoked or expired key · ", h("b", {}, "403"), " the key lacks the scope (the message names it) · ", h("b", {}, "429"), " over the budget (see ", c("Retry-After"), ")."),
          code(curlKey, "shell", "check a key"),
          D.accessCall(me, { noStart: true })),
        fold("private", "Endpoints that need a key",
          h("div", { class: "dx-scroll" }, h("table", { class: "dx-tbl" },
            h("thead", {}, h("tr", {}, h("th", {}, "Endpoints"), h("th", {}, "Scope"), h("th", {}, "What they do"))),
            h("tbody", {}, PRIVATE.map(([a, s, d]) => h("tr", {}, h("td", { class: "mono dx-wrap" }, a), h("td", {}, OG.badge(s, s.includes("execute") ? "warn" : "")), h("td", {}, d)))))),
          code(curlPreview, "curl", "example: POST /v1/route/preview · free, never calls a provider"),
          operator ? p(h("a", { class: "lnk", href: "/route" + OG.qs.stringify({ gpu: "h100-80gb-sxm5", count: 8, region: "US", max: 2.5, dur: 24 }) }, "Open this preview on the Route page →")) : null),
        fold("envelope", "Response envelope",
          code(JSON.stringify({ data: { "…": "the payload" }, meta: { as_of: "2026-10-15T01:08:25Z", kind: "observed", methodology: "/methodology/data-kinds" } }, null, 2), "json", "every response"),
          p(c("meta.as_of"), " is when the response was built, ", c("meta.kind"), " the data kind. A number OpenGrid cannot compute honestly is ", c("null"), " with a ", c("reason"), ". Errors are ", c('{"detail": "…"}'), " with the HTTP status.")),
        fold("kinds", "Data kinds and price concepts",
          h("table", { class: "dx-tbl" }, h("tbody", {},
            h("tr", {}, h("th", {}, OG.kindBadge("observed")), h("td", {}, "read directly from a provider and normalized")),
            h("tr", {}, h("th", {}, OG.kindBadge("estimated")), h("td", {}, "a model or assumption fills a gap (memory, GPU count, cost scenarios)")),
            h("tr", {}, h("th", {}, OG.kindBadge("transaction")), h("td", {}, "what an OpenGrid execution actually quoted or paid")),
            h("tr", {}, h("th", {}, OG.conceptBadge("list")), h("td", {}, "the provider's published catalogue price")),
            h("tr", {}, h("th", {}, OG.conceptBadge("quote")), h("td", {}, "the price a route would transact at")))),
          p(h("a", { class: "lnk", href: "/methodology/data-kinds" }, "Data kinds methodology →"))),
        fold("limits", "Rate limits",
          p("Anonymous requests share a per-IP budget. A key gets its own budget per request class (read, write, execute), plus an account-wide cap. Responses carry ",
            c("X-RateLimit-Remaining"), "; a 429 adds ", c("Retry-After"), ". ", c("GET /v1/me"), " shows a key's current budget.")),
        allFold));

      // ---------- public list + Try it ----------
      function tryIt(m, path, params) {
        if (m !== "GET") return p(h("span", { class: "dim" }, "Try it covers GET endpoints. Use curl for POST: "), h("a", { class: "lnk", href: "/developers#rest" }, "REST examples →"));
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
          const missing = inputs.filter(([n, i]) => n.required && !i.value.trim()).map(([n]) => n.name);
          if (missing.length) { out.replaceChildren(D.formError(new Error("Fill in the required " + (missing.length > 1 ? "parameters: " : "parameter: ") + missing.join(", ") + "."))); return; }
          const u = build();
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
          } catch (e) { out.replaceChildren(OG.error({ status: 0, message: "network error", path: u })); }
        };
        return h("div", { class: "dx-try" }, h("div", { class: "lbl" }, "Try it"),
          inputs.length ? h("div", { class: "dx-try-f" }, inputs.map(([n, i]) => h("label", {}, h("span", { class: "mono dim" }, n.name + (n.required ? " *" : "")), i))) : null,
          h("div", { class: "dx-row" }, h("button", { class: "btn pri", type: "button", onclick: send }, "Send"), urlEl), out);
      }
      function epRow(m, path, summary, params) {
        const det = h("details", { class: "dx-ep" },
          h("summary", {}, h("span", { class: "dx-m " + m.toLowerCase() }, m), h("span", { class: "dx-ep-p" }, path), h("span", { class: "dx-ep-s" }, summary)));
        det.addEventListener("toggle", () => {
          if (!det.open || det.children.length > 1) return;
          det.append(h("div", { class: "dx-ep-b" }, p(summary), params ? p(h("span", { class: "dim" }, "Parameters: "), c(params), h("span", { class: "dim" }, "  (* required)")) : p(h("span", { class: "dim" }, "No parameters.")), tryIt(m, path, params)));
        });
        return det;
      }
      function drawPublic(q) {
        q = String(q || "").trim().toLowerCase();
        const groups = PUBLIC.map(([g, anchor, ops]) => {
          const rows = ops.filter(([m, path, s]) => !q || path.toLowerCase().includes(q) || s.toLowerCase().includes(q));
          return rows.length ? h("div", { id: anchor, class: "dx-group" }, h("h3", { class: "dx-h3" }, g, " ", h("span", { class: "dimmer" }, rows.length)), h("div", { class: "box" }, rows.map(r => epRow(...r)))) : null;
        }).filter(Boolean);
        pubList.replaceChildren(...(groups.length ? groups : [OG.empty("No public endpoint matches.")]));
      }
      drawPublic(query.q || "");
      if (allFold) {
        let loaded = false;
        allFold.addEventListener("toggle", () => { if (!allFold.open || loaded) return; loaded = true; if (operator) fullList(opsHolder); });
      }
      D.wireAnchors(root);

      // ---------- operator: the full schema, read only when the fold is opened ----------
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
        const filter = h("input", { class: "field dx-filter", type: "search", placeholder: "Filter all endpoints", "aria-label": "Filter all endpoints" });
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
        holder.replaceChildren(h("div", { class: "dim dx-count" }, `${ops.length} operations in ${spec.info ? spec.info.title + " " + (spec.info.version || "") : "the schema"}`), filter, list);
        draw();
      }
    },
  });
})();
