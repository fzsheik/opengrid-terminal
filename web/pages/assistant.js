/* The OpenGrid Assistant: a compact chat panel over POST /v1/assistant (api/assistant.py, contract §5).

   OG.views.assistantPanel(el, {onRequirements, initial, compact}) -> {send(text), reset(), root}
       Embeddable (the /find page mounts it when present). onRequirements(requirements, response) is called when a
       reply carries a recommendation, so the host can fill its own form with the same WorkloadRequirements.
   OG.page("/assistant")  the same panel on its own page (?q= asks a first question).

   The browser talks only to this server (CSP connect-src 'self'); the server answers from OpenGrid's live market
   data. Every number shown comes from the response cards; prices are observed (not quotes), memory / cost are
   estimated, and each is badged as such. The panel never builds a number itself.

   Pure helpers on OG.assistant (tested by tests/test_web_assistant.js): cardKind, recView, optionLinks, findHref,
   priceRows, compareRows, contextRows, questionButtons, safeHref, boundContext, kindOf. */
(() => {
  const { h } = OG;
  const fmt = OG.fmt || {};
  const EXAMPLES = [
    "Cheapest available H100",
    "What GPU should I use to fine-tune Llama 3 8B with LoRA?",
    "Affordable GPU for inference of a 7B model",
    "Compare H100 SXM and H200",
    "Why did H200 prices change?",
    "Can I fine-tune a 13B model with QLoRA within $50?",
    "Specs of the L40S",
  ];
  const MAX_MESSAGE = 2000, MAX_CONTEXT = 8000;
  const PICKS = [["cheapest_market", "Cheapest that fits"], ["best_for_workload", "Best for this workload"], ["best_executable", "Launchable through OpenGrid"]];
  const KINDS = { recommendation: "recommendation", gpu_prices: "prices", compare: "compare", specs: "specs", market_context: "context", questions: "questions", link: "link",
    action: "action", jobs: "jobs", job: "job" };
  // The only actions a card may offer. The browser performs one only when the user clicks (and, for cancel,
  // confirms); the assistant endpoint itself never changes anything.
  const ACTIONS = ["start_demo", "cancel_job"];
  const JOB_ID = /^job-[0-9a-f]{8,32}$/;

  /* ============================ pure ============================ */
  // Only same-site relative links from the server are rendered as in-app links ("/gpu/x"), never "//host" or a scheme.
  const safeHref = v => (typeof v === "string" && /^\/(?!\/)/.test(v) && !/[\s\\]/.test(v) ? v : null);
  const cardKind = c => (c && typeof c === "object" && KINDS[c.type]) || "unknown";
  // data kinds for badges: observed_market_price / observed -> observed; anything estimated stays estimated
  const kindOf = k => (!k ? "observed" : /^observed/.test(k) ? "observed" : /^estimat/.test(k) ? "estimated" : /^infer/.test(k) ? "inferred" : k);
  const findHref = req => {
    if (!req || typeof req !== "object" || !Object.keys(req).length) return "/find";
    const s = JSON.stringify(req);
    return s.length > 1800 ? "/find" : "/find?mode=paste&req=" + encodeURIComponent(s);  // find.js reads ?req= (paste mode)
  };
  function optionLinks(o) {
    if (!o) return [];
    const slug = o.gpu_slug || OG.slug(o.gpu || "");
    const out = [];
    if (slug) out.push({ label: "Market", href: "/gpu/" + slug });
    const pv = safeHref(o.execution && o.execution.route_preview_url) || (slug ? `/route?gpu=${encodeURIComponent(slug)}&count=${o.gpu_count || 1}` : null);
    if (pv) out.push({ label: "Route preview", href: pv });
    return out;
  }
  function recView(rec) {
    rec = rec || {};
    const est = rec.estimate || {}, mem = est.memory || {};
    const opts = Array.isArray(rec.options) ? rec.options : [];
    const byId = new Map(opts.map(o => [o.option_id, o]));
    const picks = PICKS.map(([key, label]) => {
      const ref = rec.picks && rec.picks[key];
      const option = ref && byId.get(ref.option_id) || null;
      return { key, label, option, reason: (ref && ref.reason) || null };
    });
    return {
      needsGpu: est.needs_gpu, needsGpuReason: est.needs_gpu_reason || null,
      memory: mem.recommended_gb != null ? { gb: mem.recommended_gb, range: mem.range_gb || null, confidence: mem.confidence || null, kind: "estimated", assumptions: (mem.assumptions || []).slice(0, 4) } : null,
      gpuCount: est.gpu_count && est.gpu_count.suggested || null,
      picks, top: opts.slice(0, 5), excluded: (rec.excluded || []).length,
      questions: rec.questions || [], notes: (rec.notes || []).slice(0, 3),
    };
  }
  // gpu_prices card -> table rows; a family card lists each variant's rows (never one merged market)
  function priceRows(data) {
    if (!data) return [];
    const one = d => (d.cheapest || []).map(c => ({ gpu: d.gpu, slug: d.slug, provider: c.provider_display || c.provider, region: c.region || c.country || null,
      price: c.price_per_gpu_hour, kind: kindOf(c.price_kind), available: c.available === true ? "yes" : c.available === false ? "no" : "not reported" }));
    return data.variants ? data.variants.flatMap(one) : one(data);
  }
  function compareRows(data) {
    return ((data && data.items) || []).map(it => {
      const hw = it.hardware || {}, m = it.market || {};
      return { gpu: it.gpu, slug: it.slug, vram: hw.vram_gb, mem: hw.memory_type || null, bw: hw.memory_bandwidth_tbps, bf16: hw.bf16_tflops_dense,
        fp8: hw.fp8_tflops_dense, form: hw.form_factor || null, low: m.low, median: m.median, providers: m.providers || 0 };
    });
  }
  function contextRows(data) {
    return ((data && data.items) || []).map(it => ({ gpu: it.gpu, slug: it.slug, current: it.current || {}, summaries: it.summaries || [],
      moves: (it.moves || []).slice(-6), threshold: it.move_threshold_pct, reason: it.reason || null }));
  }
  // questions card -> [{question, why, buttons:[{label, message}]}]; option-less questions still show their text
  function questionButtons(qs) {
    return (Array.isArray(qs) ? qs : []).filter(q => q && q.question).map(q => ({
      field: q.field || null, question: String(q.question), why: q.why || null,
      buttons: (Array.isArray(q.options) ? q.options : []).filter(o => o && o.label && o.message).slice(0, 8)
        .map(o => ({ label: String(o.label), message: String(o.message).slice(0, MAX_MESSAGE) })),
    }));
  }
  // what is sent back: the server's context, dropped (fresh start) if it ever grows past the server's bound
  function boundContext(ctx) {
    if (!ctx || typeof ctx !== "object") return null;
    try { return JSON.stringify(ctx).length > MAX_CONTEXT ? null : ctx; } catch (e) { return null; }
  }
  // action card -> {action, label, jobId, epochs, confirm, note} or null when it is not an action this UI knows
  function actionView(d) {
    if (!d || typeof d !== "object" || !ACTIONS.includes(d.action)) return null;
    const jobId = d.job_id == null ? null : String(d.job_id);
    if (d.action === "cancel_job" && !JOB_ID.test(jobId || "")) return null;
    const epochs = Number.isInteger(d.epochs) && d.epochs >= 1 && d.epochs <= 10 ? d.epochs : 3;
    const label = String(d.label || (d.action === "start_demo" ? "Start free simulation" : "Cancel " + jobId)).slice(0, 80);
    return { action: d.action, label, jobId, epochs, confirm: d.confirm ? String(d.confirm).slice(0, 600) : null, note: d.note ? String(d.note).slice(0, 300) : null };
  }
  // jobs / job card -> job items with only the fields the UI shows; hrefs are rebuilt from the id, never trusted
  function jobItems(d, single) {
    const list = single ? [d] : d && Array.isArray(d.items) ? d.items : [];
    return list.filter(j => j && JOB_ID.test(String(j.job_id || ""))).slice(0, 20).map(j => {
      const p = j.progress && typeof j.progress === "object" ? j.progress : null, c = j.cost && typeof j.cost === "object" ? j.cost : {};
      return { job_id: String(j.job_id), name: j.name ? String(j.name) : null, status: j.status ? String(j.status) : null,
        text: String(j.status_text || (j.status ? String(j.status).replace(/_/g, " ") : "unknown")), sandbox: j.sandbox === true,
        progress: p && Number.isFinite(p.epoch) && Number.isFinite(p.epochs) ? { epoch: p.epoch, epochs: p.epochs } : null,
        est_max_usd: Number.isFinite(c.est_max_usd) ? c.est_max_usd : null, actual_usd: Number.isFinite(c.actual_usd) ? c.actual_usd : null,
        created_at: j.created_at || null, cancellable: j.cancellable === true, href: "/jobs/" + encodeURIComponent(String(j.job_id)) };
    });
  }
  OG.assistant = { EXAMPLES, MAX_MESSAGE, PICKS, ACTIONS, safeHref, cardKind, kindOf, findHref, optionLinks, recView, priceRows, compareRows, contextRows, questionButtons, boundContext, actionView, jobItems };

  if (typeof OG.page !== "function" || typeof document === "undefined" || !OG.h) return;   // node tests: pure part only

  /* ============================ DOM ============================ */
  const money = v => (v == null ? "-" : OG.money ? OG.money(v) : "$" + Number(v).toFixed(2));
  const num = v => (v == null ? "-" : fmt.num ? fmt.num(v) : String(v));
  const badge = k => (OG.kindBadge ? OG.kindBadge(kindOf(k)) : h("span", { class: "badge" }, kindOf(k)));
  const lnk = (l, cls) => { const href = safeHref(l.href); return href ? h("a", { class: cls || "lnk", href }, l.label) : null; };
  const gpuA = (name, slug) => (slug ? h("a", { class: "lnk", href: "/gpu/" + slug }, OG.shortGpu ? OG.shortGpu(name) : name) : h("span", {}, name || "-"));
  const tbl = (columns, rows, empty) => (OG.table ? OG.table({ columns, rows, compact: true, autoHide: false, empty: empty || "No rows." }) : h("div"));

  function recCard(data, ctx) {
    const v = recView(data);
    if (v.needsGpu === false) return h("div", { class: "ag-card" }, h("b", {}, "No GPU needed"), " ", v.needsGpuReason || "", " ", badge("estimated"));
    const picks = v.picks.map(p => h("li", { class: "ag-pick" },
      h("span", { class: "ag-pick-l" }, p.label),
      p.option ? [h("span", {}, p.option.gpu_count > 1 ? p.option.gpu_count + "× " : "", gpuA(p.option.gpu, p.option.gpu_slug), " · ", p.option.provider_display || p.option.provider),
        h("span", { class: "mono" }, money(p.option.price_per_gpu_hour), "/GPU-h "), badge(p.option.price_kind),
        p.option.fresh === false ? OG.badge("stale", "warn") : null,
        p.reason ? h("span", { class: "dim ag-why" }, p.reason) : null,
        h("span", { class: "ag-links" }, optionLinks(p.option).map(l => lnk(l, "btn sm")))]
        : h("span", { class: "dim" }, p.key === "best_executable" ? "none for you right now" : "none available right now")));
    const top = v.top.length ? tbl([
      { key: "gpu", label: "GPU", fmt: (x, r) => gpuA(r.gpu, r.gpu_slug) },
      { key: "provider_display", label: "Provider", fmt: (x, r) => x || r.provider },
      { key: "region", label: "Region" },
      { key: "price_per_gpu_hour", label: "$/GPU-h", num: true, fmt: (x, r) => h("span", {}, money(x), " ", badge(r.price_kind)) },
      { key: "fit", label: "Fits", value: r => r.memory_fit ? (r.memory_fit.fits ? "yes" : "no") : null },
      { key: "exec", label: "OpenGrid", value: r => r.execution ? (r.execution.executable ? "launchable" : "preview only") : null },
    ], v.top) : null;
    return h("div", { class: "ag-card" },
      v.memory ? h("div", { class: "ag-mem" }, "Memory ≈ ", h("b", {}, num(v.memory.gb), " GB"), v.gpuCount > 1 ? ` · ${v.gpuCount} GPUs` : "",
        v.memory.confidence ? h("span", { class: "dim" }, ` · ${v.memory.confidence} confidence `) : " ", badge("estimated")) : null,
      h("ul", { class: "ag-picks" }, picks), top,
      v.notes.length ? h("ul", { class: "ag-notes" }, v.notes.map(n => h("li", {}, String(n)))) : null,
      h("div", { class: "ag-links" }, h("a", { class: "btn sm", href: findHref(ctx && ctx.requirements) }, "Refine in Find Compute →")));
  }
  function pricesCard(data) {
    const rows = priceRows(data);
    return h("div", { class: "ag-card" }, data.variants ? h("div", { class: "dim" }, data.note || "Variants are priced separately.") : null,
      tbl([{ key: "gpu", label: "GPU", fmt: (x, r) => gpuA(r.gpu, r.slug) }, { key: "provider", label: "Provider" }, { key: "region", label: "Region" },
        { key: "price", label: "$/GPU-h", num: true, fmt: (x, r) => h("span", {}, money(x), " ", badge(r.kind)) }, { key: "available", label: "Available" }],
      rows, "No fresh, in-stock listing right now."));
  }
  function compareCard(data) {
    return h("div", { class: "ag-card" }, tbl([
      { key: "gpu", label: "GPU", fmt: (x, r) => gpuA(r.gpu, r.slug) }, { key: "vram", label: "VRAM GB", num: true }, { key: "bw", label: "TB/s", num: true },
      { key: "bf16", label: "BF16 TFLOPS", num: true }, { key: "fp8", label: "FP8 TFLOPS", num: true },
      { key: "low", label: "Low $/GPU-h", num: true, fmt: x => (x == null ? "-" : h("span", {}, money(x), " ", badge("observed"))) },
      { key: "median", label: "Median", num: true, fmt: x => money(x) }, { key: "providers", label: "Providers", num: true },
    ], compareRows(data)), data.note ? h("div", { class: "dim ag-foot" }, data.note) : null);
  }
  function contextCard(data) {
    const rows = contextRows(data);
    return h("div", { class: "ag-card" },
      h("div", { class: "ag-warn", role: "note" }, data.note || "Correlation, not causation."),
      rows.map(r => h("div", { class: "ag-ctx" }, h("b", {}, gpuA(r.gpu, r.slug)),
        r.summaries.length ? h("ul", {}, r.summaries.map(s => h("li", {}, s))) : r.reason ? h("div", { class: "dim" }, r.reason) : null,
        r.moves.length ? tbl([{ key: "at", label: "Hour (UTC)", fmt: x => String(x || "").slice(0, 16).replace("T", " ") },
          { key: "direction", label: "Move" }, { key: "change_pct", label: "%", num: true, fmt: x => num(x) + "%" },
          { key: "from", label: "From", num: true, fmt: x => money(x) }, { key: "to", label: "To", num: true, fmt: x => money(x) },
          { key: "kind", label: "", fmt: x => badge(x || "inferred") }], r.moves) : null)),
      (data.news || []).length ? h("div", { class: "ag-news" }, h("div", { class: "dim" }, "News in the same window (untrusted public text; not a cause):"),
        h("ul", {}, data.news.map(n => h("li", {}, n.url ? h("a", { class: "lnk", href: n.url, rel: "noopener noreferrer nofollow", target: "_blank" }, n.title) : n.title,
          n.source ? h("span", { class: "dim" }, " · " + n.source) : null)))) : null);
  }
  function questionsCard(data, ask) {
    return h("div", { class: "ag-card ag-qs" }, questionButtons(data).map(q => h("div", { class: "ag-q" },
      h("div", {}, q.question, q.why ? h("span", { class: "dim" }, " (" + q.why + ")") : null),
      q.buttons.length ? h("div", { class: "ag-chips", role: "group", "aria-label": q.question },
        q.buttons.map(b => h("button", { type: "button", class: "chip", onclick: () => ask(b.message) }, b.label))) : null)));
  }
  function jobsCard(items, more) {
    if (!items.length) return h("div", { class: "ag-card" }, h("span", { class: "dim" }, "No jobs."));
    return h("div", { class: "ag-card" }, h("ul", { class: "ag-jobs" }, items.map(j => h("li", {},
      h("a", { class: "lnk", href: j.href }, j.name || j.job_id), " ",
      h("span", { class: "ag-jst" }, j.text), j.progress ? h("span", { class: "dim" }, ` · epoch ${j.progress.epoch} of ${j.progress.epochs}`) : null,
      j.sandbox ? [" ", OG.badge("simulated", "warn")] : null,
      j.actual_usd != null ? h("span", { class: "mono dim" }, " · " + money(j.actual_usd)) : j.est_max_usd != null && !j.sandbox ? h("span", { class: "mono dim" }, " · up to " + money(j.est_max_usd)) : null))),
      more ? h("a", { class: "lnk", href: "/jobs" }, "All jobs →") : null);
  }
  // An action button. start_demo is free (SIMULATED) and runs on click; cancel_job asks for confirmation first.
  function actionCard(a, hooks) {
    const msgEl = h("div", { class: "ag-act-msg", "aria-live": "polite" });
    const btn = h("button", { type: "button", class: "btn " + (a.action === "cancel_job" ? "w4-danger" : "pri") }, a.label);
    btn.addEventListener("click", async () => {
      if (a.action === "cancel_job") {
        const ok = await OG.dialog({ title: "Cancel " + a.jobId + "?", danger: true, confirm: "Cancel job", cancel: "Keep it running",
          body: h("p", {}, a.confirm || "Before launch the approval is rejected; after launch the GPU is terminated. This cannot be undone.") });
        if (!ok) return;
      }
      btn.disabled = true;
      msgEl.replaceChildren(OG.loading ? OG.loading(a.action === "start_demo" ? "Starting the free simulation…" : "Cancelling…") : "…");
      try {
        if (a.action === "start_demo") {
          const job = await OG.jobs.startDemo(a.epochs);
          msgEl.replaceChildren(h("span", { class: "up" }, "Started. "), h("a", { class: "btn sm pri", href: "/jobs/" + encodeURIComponent(job.job_id) }, "Watch it run →"));
        } else {
          hooks.cancelIntent[a.jobId] = OG.intentFor(hooks.cancelIntent[a.jobId], "cancel:" + a.jobId, {});
          await OG.api.intent(hooks.cancelIntent[a.jobId], "/v1/jobs/" + encodeURIComponent(a.jobId) + "/cancel", { method: "POST", body: {} });
          msgEl.replaceChildren(h("span", { class: "up" }, "Cancel requested. "), h("a", { class: "lnk", href: "/jobs/" + encodeURIComponent(a.jobId) }, "Open the job →"));
        }
        if (typeof hooks.onJobsChanged === "function") { try { hooks.onJobsChanged(); } catch (e) { console.error(e); } }
      } catch (e) {
        btn.disabled = false;
        msgEl.replaceChildren(e && (e.status === 401 || e.status === 403) ? h("span", { class: "warn-t" }, "You need to be signed in with permission to run jobs (an API key with route:execute). ", h("a", { class: "lnk", href: "/keys" }, "API keys →"))
          : OG.error ? OG.error(e) : h("span", {}, "Something went wrong."));
      }
    });
    return h("div", { class: "ag-card ag-act" }, h("div", { class: "ag-act-r" }, btn, a.note ? h("span", { class: "dim" }, a.note) : null), msgEl);
  }
  function renderCard(card, ask, ctx, hooks) {
    const k = cardKind(card), d = card && card.data;
    try {
      if (k === "action") { const a = actionView(d); return a ? actionCard(a, hooks || { cancelIntent: {} }) : null; }
      if (k === "jobs") return jobsCard(jobItems(d), d && d.more === true);
      if (k === "job") return jobsCard(jobItems(d, true), false);
      if (k === "recommendation") return recCard(d, ctx);
      if (k === "prices") return pricesCard(d);
      if (k === "compare" || k === "specs") return compareCard(d);
      if (k === "context") return contextCard(d);
      if (k === "questions") return questionsCard(d, ask);
      if (k === "link") return null;   // gathered into one link row below the reply
    } catch (e) { console.error(e); }
    return null;
  }

  OG.views = OG.views || {};
  OG.views.assistantPanel = (el, opts) => {
    opts = opts || {};
    let context = boundContext(opts.context || null), busy = false;
    const hooks = { cancelIntent: {}, onJobsChanged: opts.onJobsChanged };
    const examples = Array.isArray(opts.examples) && opts.examples.length ? opts.examples : EXAMPLES;
    const log = h("div", { class: "ag-log", role: "log", "aria-live": "polite", "aria-relevant": "additions", "aria-label": "Conversation" });
    const input = h("textarea", { class: "field ag-in", rows: "2", maxlength: String(MAX_MESSAGE), placeholder: opts.placeholder || "Ask about GPU prices, which GPU fits your job, comparisons…", "aria-label": opts.title || "Ask the OpenGrid Assistant" });
    const sendBtn = h("button", { type: "submit", class: "btn pri" }, "Ask");
    const form = h("form", { class: "ag-form", onsubmit: e => { e.preventDefault(); ask(input.value); } }, input, sendBtn);
    input.addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); ask(input.value); } });
    const chips = h("div", { class: "ag-chips ag-examples", role: "group", "aria-label": "Example questions" },
      examples.map(x => h("button", { type: "button", class: "chip", onclick: () => ask(x) }, x)));
    const root = h("section", { class: "ag" + (opts.compact ? " ag-compact" : ""), "aria-label": "OpenGrid Assistant" },
      h("div", { class: "ag-head" }, h("b", {}, opts.title || "OpenGrid Assistant"),
        h("span", { class: "ag-note" }, opts.note || "Answers use OpenGrid's live market data; estimates are labelled. It cannot launch or buy anything."),
        h("button", { type: "button", class: "btn sm", onclick: () => reset(), title: "Start a new conversation" }, "New")),
      log, chips, form);
    el.append(root);

    function msg(role, ...kids) {
      const m = h("div", { class: "ag-msg ag-" + role }, kids);
      log.append(m);
      while (log.children.length > 60) log.firstChild.remove();
      log.scrollTop = log.scrollHeight;
      return m;
    }
    function reset() { context = null; log.textContent = ""; chips.hidden = false; input.focus(); }
    async function ask(text) {
      text = String(text || "").trim().slice(0, MAX_MESSAGE);
      if (!text || busy) return;
      busy = true; sendBtn.disabled = true; input.value = ""; chips.hidden = true;
      msg("user", text);
      const wait = msg("bot", OG.loading ? OG.loading("Thinking…") : "…");
      try {
        const r = await OG.api("/v1/assistant", { method: "POST", body: { message: text, context: boundContext(context) } });
        context = boundContext(r.context);
        const links = (r.cards || []).filter(c => cardKind(c) === "link").map(c => lnk(c.data || {}, "btn sm")).filter(Boolean);
        wait.replaceChildren(h("p", { class: "ag-reply" }, r.reply),
          (r.cards || []).map(c => renderCard(c, ask, r.context, hooks)),
          links.length ? h("div", { class: "ag-links" }, links) : null,
          h("div", { class: "ag-meta dim" }, r.engine === "deterministic" ? "deterministic answer" : "phrased by " + String(r.engine).replace(/^llm:/, ""),
            r.grounded ? " · grounded in the cards above" : " · check the cards"));
        const rec = (r.cards || []).find(c => cardKind(c) === "recommendation");
        if (rec && typeof opts.onRequirements === "function" && r.context && r.context.requirements) {
          try { opts.onRequirements(r.context.requirements, r); } catch (e) { console.error(e); }
        }
      } catch (e) {
        wait.replaceChildren(e && e.status === 429 ? h("p", { class: "ag-reply" }, "Too many questions in a short time; wait a minute and try again.")
          : e && e.status === 422 ? h("p", { class: "ag-reply" }, "That question or conversation is too long. Start a new conversation.")
          : OG.error ? OG.error(e) : h("p", {}, "Something went wrong."));
        if (e && e.status === 422) context = null;
      } finally {
        busy = false; sendBtn.disabled = false; input.focus();
      }
    }
    if (opts.initial) ask(opts.initial);
    return { send: ask, reset, root };
  };

  OG.page("/assistant", {
    title: "Assistant",
    nav: "find",  // the assistant is the plain-English way into Find Compute
    mount(el, params, query) {
      const host = h("div", { class: "ag-page" });
      el.append(OG.head("Assistant", "Ask about live GPU prices, which GPU fits a training, fine-tuning or inference job, comparisons and price moves"), host);
      OG.views.assistantPanel(host, { initial: query && query.q ? String(query.q) : null });
    },
  });
})();
