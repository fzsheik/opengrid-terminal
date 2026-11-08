/* The landing page at "/" (web/landing.html).
   1. Headline: types "Cheapest GPUs", live GPU prices ("H100 $2.99/hr", from GET /v1/best/<gpu>) and "OpenGrid" in
      turn; the grid mark draws its lines with the letters. The text box has a fixed width (the longest phrase), so
      nothing slides while it types.
   2. Search: the bar asks the OpenGrid Assistant (POST /v1/assistant: read-only, grounded in observed prices) and
      shows the answer as price rows. "for 8 hours" adds the total at the listed price. Follow-up questions come back
      as buttons that answer with the conversation's context.
   3. API key: POST /v1/instant-key (api/instant_key.py), copied to the clipboard, kept for this tab in
      sessionStorage so a refresh does not lose it; then one real call with the key, shown with its live response. */
(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const HDR = { "X-OpenGrid-Request": "1", "Accept": "application/json" };
  const HEADLINE_GPUS = [["h100-80gb-sxm5", "H100"], ["a100-80gb-sxm4", "A100"], ["h200-141gb-sxm5", "H200"]];
  const KEY_STORE = "og-instant-key";

  // ------------------------------------------------------------------ small helpers
  function el(tag, attrs, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === "class") n.className = v;
      else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else n.setAttribute(k, v === true ? "" : v);
    }
    for (const k of kids.flat()) if (k != null && k !== false) n.append(k instanceof Node ? k : String(k));
    return n;
  }
  const price = p => (p == null || !isFinite(p)) ? "-" : "$" + Number(p).toFixed(p >= 0.1 ? 2 : 3);
  const shortGpu = name => String(name || "").replace(/^(NVIDIA|AMD Instinct|AMD|Intel)\s+/i, "");
  function ago(sec) {
    if (sec == null || !isFinite(sec)) return null;
    if (sec < 90) return "just now";
    const m = Math.round(sec / 60);
    if (m < 90) return m + " min ago";
    const h = Math.round(m / 60);
    return h < 36 ? h + " h ago" : Math.round(h / 24) + " days ago";
  }
  const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
  const ageOf = iso => iso ? (Date.now() - Date.parse(iso)) / 1000 : null;
  function errText(body, fallback) {
    return (body && (body.detail || (body.error && body.error.message) || body.message)) || fallback;
  }
  async function getJSON(url, opts) {
    const r = await fetch(url, Object.assign({ credentials: "same-origin" }, opts));
    const body = await r.json().catch(() => ({}));
    if (!r.ok) { const e = new Error(errText(body, "Something went wrong. Try again.")); e.status = r.status; throw e; }
    return body.data;
  }

  // ------------------------------------------------------------------ theme follows the system unless chosen
  try {
    const mq = matchMedia("(prefers-color-scheme: dark)");
    mq.addEventListener("change", e => {
      let pinned = null;
      try { pinned = localStorage.getItem("og-theme"); } catch (_) { /* storage blocked */ }
      if (pinned !== "light" && pinned !== "dark") document.documentElement.dataset.theme = e.matches ? "dark" : "light";
    });
  } catch (_) { /* old browser */ }

  // ------------------------------------------------------------------ headline
  const phrases = [{ a: "Cheapest GPUs" }, { a: "OpenGrid" }];
  const head = {
    a: document.querySelector(".lp-type-a"), b: document.querySelector(".lp-type-b"),
    box: $("lp-type"), caret: document.querySelector(".lp-caret"), mark: $("lp-mark"),
  };
  const lines = [...head.mark.querySelectorAll("path")].map(p => {
    const len = p.getTotalLength();
    p.style.strokeDasharray = String(len);
    p.style.strokeDashoffset = String(len);
    return { p, len };
  });
  function draw(f) {
    lines.forEach(({ p, len }, i) => { p.style.strokeDashoffset = String(len * (1 - Math.min(1, Math.max(0, f * lines.length - i)))); });
    head.mark.classList.toggle("on", f >= 1);
  }
  function show(ph, n) {           // the first n characters of a phrase
    const a = ph.a, b = ph.b || "";
    head.a.textContent = a.slice(0, n);
    head.b.textContent = n > a.length ? b.slice(0, n - a.length) : "";
  }
  function fitWidth() {            // the box is as wide as the longest phrase, so the mark never moves
    const probe = head.box.cloneNode(true);
    probe.removeAttribute("id");
    Object.assign(probe.style, { position: "absolute", visibility: "hidden", minWidth: "0" });
    head.box.parentNode.append(probe);
    let w = 0;
    for (const ph of phrases) {
      probe.querySelector(".lp-type-a").textContent = ph.a;
      probe.querySelector(".lp-type-b").textContent = ph.b || "";
      w = Math.max(w, probe.getBoundingClientRect().width);
    }
    probe.remove();
    head.box.style.minWidth = Math.ceil(w) + "px";
  }
  function runHeadline() {
    if (still) { show(phrases[0], 99); draw(1); return; }
    const wait = ms => new Promise(r => setTimeout(r, ms));
    let k = 0;
    (async () => {
      show(phrases[0], 0); draw(0);
      await wait(300);
      for (;;) {
        const ph = phrases[k % phrases.length], len = ph.a.length + (ph.b || "").length;
        head.caret.classList.add("typing");
        for (let i = 1; i <= len; i++) { show(ph, i); draw(i / len); await wait(80 + Math.random() * 30 - 15); }
        head.caret.classList.remove("typing");
        await wait(2200);
        head.caret.classList.add("typing");
        for (let i = len - 1; i >= 0; i--) { show(ph, i); draw(i / len); await wait(36); }
        head.caret.classList.remove("typing");
        await wait(420);
        k++;
      }
    })();
  }
  fitWidth();
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(fitWidth);
  runHeadline();

  // ------------------------------------------------------------------ live prices: the headline and the proof line
  const live = {};
  Promise.all(HEADLINE_GPUS.map(([slug]) => getJSON("/v1/best/" + slug, { headers: { Accept: "application/json" } })
    .then(d => { live[slug] = d; }).catch(() => null))).then(() => {
    const priced = HEADLINE_GPUS.filter(([slug]) => live[slug] && live[slug].market && live[slug].market.low != null);
    if (!priced.length) return;
    // Cheapest GPUs → H100 $x/hr → OpenGrid → A100 $y/hr → … (all observed lows)
    const rows = priced.map(([slug, label]) => ({ a: label + " ", b: price(live[slug].market.low) + "/hr" }));
    phrases.splice(0, phrases.length, { a: "Cheapest GPUs" }, rows[0], { a: "OpenGrid" }, ...rows.slice(1));
    fitWidth();
    const [slug] = priced[0], d = live[slug], m = d.market;
    const cand = (d.candidates || []).find(c => c.provider === m.low_provider);
    const where = cand && cand.provider_display ? " on " + cand.provider_display : "";
    const when = ago(ageOf(m.as_of));
    const proof = $("lp-proof");
    proof.replaceChildren("Live: ", shortGpu(d.gpu), " from ", el("b", {}, price(m.low) + "/GPU-hr"), where,
      ` · ${m.providers} provider${m.providers === 1 ? "" : "s"}`, when ? " · observed " + when : "");
    proof.hidden = false;
  });

  // ------------------------------------------------------------------ search (the OpenGrid Assistant)
  const form = $("lp-form"), q = $("lp-q"), res = $("lp-res");
  let convo = null;              // the assistant's context, for follow-up answers
  let lastSlug = HEADLINE_GPUS[0][0];

  function hoursIn(text) {
    const h = /(\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hours?)\b/i.exec(text);
    if (h) return parseFloat(h[1]);
    const d = /(\d+(?:\.\d+)?)\s*days?\b/i.exec(text);
    return d ? parseFloat(d[1]) * 24 : null;
  }

  function row(gpu, sub, provider, perHour, gpus, hours) {
    const total = hours && perHour != null ? perHour * hours * (gpus || 1) : null;
    return el("li", { class: "lp-row" },
      el("span", { class: "lp-row-g" }, gpu, sub ? el("small", {}, sub) : null),
      el("span", { class: "lp-row-p" }, provider || ""),
      el("span", { class: "lp-row-c" }, el("b", {}, price(perHour)), el("span", {}, " /GPU-hr"),
        total != null ? el("em", {}, `${hours} h ≈ ${price(total)}${gpus > 1 ? ` for ${gpus} GPUs` : ""}`) : null));
  }

  function renderGpuPrices(card, hours) {
    const d = card.data;
    const out = [];
    let ages = [], link = null;
    if (Array.isArray(d.variants)) {                     // a GPU family: the best listing of each variant
      const best = d.variants.map(v => ({ v, c: (v.cheapest || [])[0] })).filter(x => x.c)
        .sort((x, y) => x.c.price_per_gpu_hour - y.c.price_per_gpu_hour).slice(0, 5);
      best.forEach(({ v, c }) => {
        out.push(row(shortGpu(v.gpu), v.market && v.market.providers ? plural(v.market.providers, "provider") : null,
          c.provider_display, c.price_per_gpu_hour, c.gpu_count, hours));
        ages.push(c.age_seconds);
      });
      if (best[0]) lastSlug = best[0].v.slug;
      link = d.family_slug ? ["Open the " + d.family + " market", "/gpu/" + d.family_slug] : null;
    } else if (d.gpu) {                                   // one GPU: its cheapest providers
      (d.cheapest || []).slice(0, 4).forEach((c, i) => {
        out.push(row(i ? "" : shortGpu(d.gpu), i ? null : (d.market && d.market.providers ? plural(d.market.providers, "provider") : null),
          c.provider_display, c.price_per_gpu_hour, c.gpu_count, hours));
        ages.push(c.age_seconds);
      });
      if (d.slug) { lastSlug = d.slug; link = ["Open the " + shortGpu(d.gpu) + " market", "/gpu/" + d.slug]; }
    }
    if (!out.length) return null;
    const oldest = ages.length ? Math.max(...ages.filter(a => a != null)) : null;
    return { nodes: [el("ul", { class: "lp-rows" }, out)], note: "Observed list prices, not quotes" + (ago(oldest) ? " · seen " + ago(oldest) : ""), link };
  }

  function renderRecommendation(card, hours) {
    const r = card.data, byId = new Map((r.options || []).map(o => [o.option_id, o]));
    const picks = [["cheapest_market", "Cheapest that fits"], ["best_for_workload", "Best for this job"]]
      .map(([k, label]) => [label, r.picks && r.picks[k] && byId.get(r.picks[k].option_id)]).filter(([, o]) => o);
    if (!picks.length) return null;
    const mem = r.estimate && r.estimate.memory && r.estimate.memory.recommended_gb;
    return { nodes: [
      mem ? el("p", { class: "lp-res-reply" }, `Needs about ${mem.toFixed(1)} GB of GPU memory (an estimate).`) : null,
      el("ul", { class: "lp-rows" }, picks.map(([label, o]) =>
        row(shortGpu(o.gpu) + (o.gpu_count > 1 ? ` ×${o.gpu_count}` : ""), label, o.provider_display, o.price_per_gpu_hour, o.gpu_count, hours))),
    ], note: "Observed list prices, not quotes", link: ["Refine in Find Compute", "/find"] };
  }

  function renderQuestions(card) {
    const qs = (card.data || []).filter(x => Array.isArray(x.options) && x.options.length);
    if (!qs.length) return null;
    const first = qs[0];
    return { nodes: [el("p", { class: "lp-res-reply lp-res-q" }, first.question),
      el("div", { class: "lp-ask" }, first.options.slice(0, 4).map(o =>
        el("button", { type: "button", onclick: () => search(o.message || o.label, true) }, o.label)))] };
  }

  function render(data, text) {
    const hours = hoursIn(text);
    const parts = [], notes = [], links = [];
    let hasRows = false;
    for (const card of data.cards || []) {
      const p = card.type === "gpu_prices" ? renderGpuPrices(card, hours)
        : card.type === "recommendation" ? renderRecommendation(card, hours)
        : card.type === "questions" ? renderQuestions(card) : null;
      if (!p) continue;
      parts.push(...p.nodes);
      if (p.note) { notes.push(p.note); hasRows = true; }
      if (p.link) links.push(p.link);
    }
    if (!hasRows && data.reply) parts.unshift(el("p", { class: "lp-res-reply" }, data.reply));
    links.push(["Continue in the Assistant", "/assistant?q=" + encodeURIComponent(text)]);
    parts.push(el("div", { class: "lp-res-meta" }, el("span", {}, notes.join(" · ")),
      el("span", { class: "lp-res-links" }, links.map(([t, h]) => el("a", { href: h }, t + " →")))));
    res.replaceChildren(...parts.filter(Boolean));
    res.hidden = false;
  }

  async function search(text, followUp) {
    text = String(text || "").trim();
    if (!text || form.classList.contains("busy")) return;
    if (!followUp) { convo = null; q.value = text; }
    form.classList.add("busy");
    try {
      const data = await getJSON("/v1/assistant", {
        method: "POST", headers: Object.assign({ "Content-Type": "application/json" }, HDR),
        body: JSON.stringify({ message: text, context: convo }),
      });
      convo = data.context || null;
      render(data, followUp ? q.value : text);
      if (keyShown()) refreshTry();
    } catch (e) {
      res.replaceChildren(el("p", { class: "lp-res-err" }, e.status === 429
        ? "Too many searches from this network for a minute. Wait a moment and try again." : e.message));
      res.hidden = false;
    } finally {
      form.classList.remove("busy");
    }
  }

  form.addEventListener("submit", e => { e.preventDefault(); search(q.value); });
  $("lp-try").addEventListener("click", e => {
    const b = e.target.closest("button[data-q]");
    if (b) search(b.dataset.q);
  });

  // ------------------------------------------------------------------ API key
  const gen = $("lp-gen"), genT = $("lp-gen-t"), err = $("lp-err"), ask = $("lp-key-ask"), out = $("lp-key-out");
  const secretEl = $("lp-secret"), copyBtn = $("lp-copy"), saved = $("lp-saved"), liveMsg = $("lp-live");
  let secret = null;
  const keyShown = () => !out.hidden;

  function selectKey() {
    const sel = getSelection(), range = document.createRange();
    range.selectNodeContents(secretEl);
    sel.removeAllRanges();
    sel.addRange(range);
  }
  async function copy() {
    try {
      await navigator.clipboard.writeText(secret);
      return true;
    } catch (_) {
      selectKey();
      return false;
    }
  }
  function savedLine(copied) {
    saved.replaceChildren(copied ? el("b", {}, "Copied to clipboard.") : "Select the key above and copy it.",
      " It can't be shown again, so save it somewhere safe.");
  }

  function refreshTry() {
    $("lp-curl").textContent = `curl ${location.origin}/v1/best/${lastSlug} \\\n  -H "Authorization: Bearer ${secret}"`;
    const respH = $("lp-resp-h"), resp = $("lp-resp");
    getJSON("/v1/best/" + lastSlug, { headers: { Authorization: "Bearer " + secret, Accept: "application/json" }, credentials: "omit" })
      .then(d => {
        const m = d.market || {};
        const trimmed = {
          gpu: d.gpu,
          market: { low: m.low, low_provider: m.low_provider, median: m.median, providers: m.providers, as_of: m.as_of, kind: m.kind },
          candidates: (d.candidates || []).slice(0, 2).map(c => {
            const o = {};
            for (const k of ["provider", "provider_display", "region", "price_per_gpu_hour", "available", "observed_at"]) if (k in c) o[k] = c[k];
            return o;
          }),
        };
        if ((d.candidates || []).length > 2) trimmed["…"] = `${d.candidates.length - 2} more candidates`;
        resp.textContent = JSON.stringify(trimmed, null, 2);
        respH.hidden = resp.hidden = false;
      })
      .catch(() => { respH.hidden = resp.hidden = true; });
  }

  function showKey(s, { fresh }) {
    secret = s;
    secretEl.textContent = s;
    ask.hidden = true;
    out.hidden = false;
    refreshTry();
    if (fresh) {
      copy().then(ok => {
        savedLine(ok);
        liveMsg.textContent = ok ? "Your API key is ready and copied to the clipboard." : "Your API key is ready. Select it and copy it.";
      });
      copyBtn.focus({ preventScroll: true });
      out.scrollIntoView({ behavior: still ? "auto" : "smooth", block: "nearest" });
    } else {
      saved.textContent = "Your key from earlier in this tab. Once you close the tab it can't be shown again.";
    }
  }

  async function generate() {
    if (gen.getAttribute("aria-busy") === "true") return;
    gen.setAttribute("aria-busy", "true");
    genT.textContent = "Generating…";
    err.hidden = true;
    try {
      const d = await getJSON("/v1/instant-key", { method: "POST", headers: HDR });
      if (!d || !d.secret) throw new Error("Couldn't make a key right now. Try again.");
      try { sessionStorage.setItem(KEY_STORE, d.secret); } catch (_) { /* storage blocked: the key still shows */ }
      showKey(d.secret, { fresh: true });
    } catch (e) {
      err.replaceChildren(e.message || "Couldn't make a key right now. Try again.",
        e.status === 429 || e.status === 503 ? [" ", el("a", { href: "/gpus" }, "Browse live prices without a key →")] : []);
      err.hidden = false;
    } finally {
      gen.removeAttribute("aria-busy");
      genT.textContent = "Generate API key";
    }
  }

  gen.addEventListener("click", generate);
  copyBtn.addEventListener("click", async () => {
    const ok = await copy();
    copyBtn.textContent = ok ? "Copied" : "Selected";
    savedLine(ok);
    liveMsg.textContent = ok ? "Copied." : "Key selected. Press Command or Control C to copy.";
    clearTimeout(copyBtn.t);
    copyBtn.t = setTimeout(() => { copyBtn.textContent = "Copy"; }, 1600);
  });

  // a refresh in the same tab keeps the key (sessionStorage dies with the tab)
  try {
    const kept = sessionStorage.getItem(KEY_STORE);
    if (kept) showKey(kept, { fresh: false });
  } catch (_) { /* storage blocked */ }
})();
