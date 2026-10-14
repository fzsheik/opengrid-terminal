/* /find — Find Compute. The beginner's way into the market: describe a workload (a short guided form, or the JSON
   from `opengrid analyze . --json`), get the server's memory estimate and GPUs that fit, priced from observed data.
   Contract: docs/OPENGRID2_CONTRACTS.md §1–3. The browser NEVER estimates memory or ranks GPUs: it builds a
   WorkloadRequirements body, POSTs /v1/recommend (falls back to /v1/recommend/estimate) and renders the answer.

   Pure helpers on OG.find (tests/test_web_find.js):
     requirementsFromForm(state, answers)  form state (query-string shaped) -> WorkloadRequirements (unknown = absent)
     stateFromQuery(q) / queryFromState(state) / answersFromQuery(q) / answersToQuery(answers)   deep links
     modelFromText(text)                   "Llama 3.1 8B" | "7B" | "org/model" -> {name, parameters_b, architecture}
     checkRequirements(obj) / validatePaste(text)   client-side contract check (unknown keys, types, enums, size)
     applyAnswers(req, answers) / controlFor(field)  inline answers to the server's questions
     pickCards(rec) / readiness(option) / memoryFit(option, estimate) / runtimeInfo(estimate) / componentRows(memory)
     tiersOf(options) / noGpu(rec) / costLines(option) / routeHref(option) / gpuHref(option)
   Views on OG.views:
     OG.views.startHere(active)            the three beginner journeys strip (also on /jobs)
     OG.views.recommendation(rec, opts)    the recommendation screen for any Recommendation payload (the assistant may reuse it)
   Optional assistant hook: when OG.views.assistantPanel(el, {ctx, requirements(), onRequirements(req), onRecommendation(rec)})
   exists, a toggleable "Ask in plain English" panel renders it. The page is complete without it. */
(() => {
  const { h, fmt } = OG;

  /* =============================== pure: the contract =============================== */
  const E = (...v) => ({ t: "enum", v });
  const S = max => ({ t: "str", max: max || 200 });
  const N = max => ({ t: "num", max: max == null ? 1e15 : max });
  const I = max => ({ t: "int", max: max == null ? 1e15 : max });
  const B = { t: "bool" };
  const A = (item, max) => ({ t: "arr", item, max: max || 50 });
  const O = props => ({ t: "obj", props });
  const SCALAR = { t: "scalar" };
  // docs/OPENGRID2_CONTRACTS.md §1 (intelligence.requirements.WorkloadRequirements, extra="forbid")
  const SCHEMA = O({
    workload_type: E("training", "fine_tuning", "inference", "batch", "no_gpu"),
    frameworks: A(S(40), 30),
    model: O({ name: S(200), architecture: S(60), parameters_b: N(100000), active_parameters_b: N(100000), num_layers: I(10000), hidden_size: I(1e6),
      num_attention_heads: I(10000), num_kv_heads: I(10000), head_dim: I(100000), vocab_size: I(1e8), max_context: I(1e9) }),
    precision: E("fp32", "bf16", "fp16", "fp8", "int8", "int4"),
    quantization: E("none", "int8", "int4", "nf4", "gptq", "awq", "fp8"),
    training: O({ method: E("full", "lora", "qlora"), optimizer: E("adamw", "adamw_8bit", "adafactor", "sgd"), micro_batch_size: I(1e6), global_batch_size: I(1e9),
      seq_len: I(1e8), gradient_checkpointing: B, lora_rank: I(100000), zero_stage: E(0, 1, 2, 3), cpu_offload: B, epochs: N(1e6), dataset_tokens: N(), dataset_samples: N() }),
    inference: O({ concurrency: I(1e6), context_len: I(1e8), output_tokens: I(1e8), target_latency_ms: N(1e9), target_tokens_per_s: N(1e9),
      engine: E("vllm", "tgi", "transformers", "llama.cpp") }),
    constraints: O({ budget_usd: N(1e9), max_price_per_gpu_hour: N(1e6), runtime_hours: N(1e6), region: S(32), max_gpus: I(100000), allow_spot: B,
      vendor: E("NVIDIA", "AMD"), include_providers: A(S(60), 100), exclude_providers: A(S(60), 100) }),
    hardware_hints: O({ gpus_mentioned: A(S(80), 50), gpu_count: I(100000), cuda_min: S(16) }),
    evidence: A(O({ field: S(80), value: SCALAR, source: S(300), confidence: E("high", "medium", "low") }), 200),
    source: E("analyzer", "form", "assistant", "api"),
  });
  // `opengrid analyze <dir> --json` (§4): only `requirements` is ever sent
  const ANALYZER_KEYS = ["root", "requirements", "files_inspected", "files_skipped", "signals", "questions", "warnings", "analyzer_version"];
  const MAX_PASTE = 64 * 1024;   // characters; the server's JSON body limit is 64 KB
  const MAX_ERRORS = 20;

  function isPlain(o) { return !!o && typeof o === "object" && !Array.isArray(o); }
  function checkValue(spec, v, path, errs) {
    if (errs.length >= MAX_ERRORS) return;
    if (v === null) return;   // unknown is null, always allowed
    const at = path || "(root)";
    switch (spec.t) {
      case "enum": if (!spec.v.includes(v)) errs.push(`${at}: ${JSON.stringify(v)} is not one of ${spec.v.map(x => JSON.stringify(x)).join(", ")}`); return;
      case "str": if (typeof v !== "string") errs.push(`${at}: expected text`); else if (v.length > spec.max) errs.push(`${at}: longer than ${spec.max} characters`); return;
      case "num": case "int":
        if (typeof v !== "number" || !isFinite(v)) errs.push(`${at}: expected a number`);
        else if (spec.t === "int" && !Number.isInteger(v)) errs.push(`${at}: expected a whole number`);
        else if (v < 0) errs.push(`${at}: must not be negative`);
        else if (v > spec.max) errs.push(`${at}: implausibly large (> ${spec.max})`);
        return;
      case "bool": if (typeof v !== "boolean") errs.push(`${at}: expected true or false`); return;
      case "scalar": {
        const ok1 = x => x === null || typeof x === "boolean" || (typeof x === "number" && isFinite(x)) || (typeof x === "string" && x.length <= 300);
        if (!(ok1(v) || (Array.isArray(v) && v.length <= 20 && v.every(ok1)))) errs.push(`${at}: expected a short text, number or true/false`);
        return;
      }
      case "arr":
        if (!Array.isArray(v)) { errs.push(`${at}: expected a list`); return; }
        if (v.length > spec.max) { errs.push(`${at}: more than ${spec.max} items`); return; }
        v.forEach((x, i) => checkValue(spec.item, x, `${at}[${i}]`, errs));
        return;
      case "obj":
        if (!isPlain(v)) { errs.push(`${at}: expected an object`); return; }
        for (const k of Object.keys(v)) {
          const p = path ? path + "." + k : k;
          if (!Object.prototype.hasOwnProperty.call(spec.props, k)) { errs.push(`${p}: unknown key — not part of the WorkloadRequirements contract`); continue; }
          checkValue(spec.props[k], v[k], p, errs);
        }
        return;
    }
  }
  // obj -> {ok, errors}: the WorkloadRequirements contract, checked before anything is sent
  function checkRequirements(obj) {
    const errors = [];
    if (!isPlain(obj)) return { ok: false, errors: ["expected a JSON object"] };
    checkValue(SCHEMA, obj, "", errors);
    return { ok: !errors.length, errors };
  }
  // Drop nulls, empty strings, empty lists and empty objects: absent == unknown (every field is optional).
  function prune(v) {
    if (Array.isArray(v)) { const a = v.map(prune).filter(x => x !== undefined); return a.length ? a : undefined; }
    if (isPlain(v)) {
      const o = {};
      for (const [k, x] of Object.entries(v)) { const y = k === "value" ? x : prune(x); if (y !== undefined) o[k] = y; }   // evidence.value: kept as is
      return Object.keys(o).length ? o : undefined;
    }
    return v === null || v === "" || v === undefined ? undefined : v;
  }
  // text -> {ok, errors, requirements, analyzer, bytes}. Accepts the analyzer output (only .requirements is
  // used) or a bare WorkloadRequirements object.
  function validatePaste(text) {
    const fail = (...errors) => ({ ok: false, errors, requirements: null, analyzer: null, bytes: typeof text === "string" ? text.length : 0 });
    if (typeof text !== "string") return fail("Paste the JSON as text");
    if (text.length > MAX_PASTE) return fail(`Too large: ${Math.ceil(text.length / 1024)} KB (limit ${MAX_PASTE / 1024} KB). Paste the output of \`opengrid analyze . --json\`, not your files.`);
    const t = text.trim();
    if (!t) return fail("Paste the JSON from `opengrid analyze . --json`");
    let obj;
    try { obj = JSON.parse(t); } catch (e) { return fail("Not valid JSON: " + String(e && e.message || e).slice(0, 160)); }
    if (!isPlain(obj)) return fail("Expected a JSON object ({ … })");
    let req = obj, analyzer = null;
    const errors = [];
    if (Object.prototype.hasOwnProperty.call(obj, "requirements")) {
      const extra = Object.keys(obj).filter(k => !ANALYZER_KEYS.includes(k));
      extra.slice(0, MAX_ERRORS).forEach(k => errors.push(`${k}: unknown key — not part of the analyzer output`));
      if (!isPlain(obj.requirements)) return fail("requirements: expected an object");
      req = obj.requirements;
      const list = k => (Array.isArray(obj[k]) ? obj[k] : []);
      analyzer = { root: typeof obj.root === "string" ? obj.root : null, files_inspected: list("files_inspected"), files_skipped: list("files_skipped"),
        signals: list("signals"), questions: list("questions"), warnings: list("warnings"), version: obj.analyzer_version || null };
    }
    const c = checkRequirements(req);
    errors.push(...c.errors);
    if (errors.length) return Object.assign(fail(...errors.slice(0, MAX_ERRORS)), { analyzer });
    const out = prune(req) || {};
    if (!out.source) out.source = analyzer ? "analyzer" : "api";
    return { ok: true, errors: [], requirements: out, analyzer, bytes: text.length };
  }

  /* =============================== pure: the guided form =============================== */
  const TASKS = [
    ["train", "Train a model from scratch", "Pre-training or training your own architecture", "training"],
    ["finetune", "Fine-tune an existing model", "Adapt Llama, Mistral, Qwen… to your data", "fine_tuning"],
    ["infer", "Run inference", "Serve or batch-run a model", "inference"],
    ["unsure", "Not sure yet", "We'll ask what matters", null],
  ];
  const METHODS = [["full", "Full", "All weights train: needs the most memory"], ["lora", "LoRA", "Small adapters on top of frozen weights"], ["qlora", "QLoRA", "LoRA on a 4-bit quantized base: least memory"]];
  const PRECISIONS = ["fp32", "bf16", "fp16", "fp8", "int8", "int4"];
  const QUANTS = [["none", "none"], ["int8", "int8"], ["int4", "int4"], ["nf4", "nf4 (bitsandbytes)"], ["gptq", "GPTQ"], ["awq", "AWQ"], ["fp8", "fp8"]];
  const REGIONS = [["", "Any region"], ["us", "United States"], ["eu", "Europe"]];
  // Suggestions only; facts from each model's published config (total parameters, billions).
  const MODELS = [
    { label: "Llama 3.1 8B", name: "meta-llama/Llama-3.1-8B", parameters_b: 8.03, architecture: "llama" },
    { label: "Llama 3.1 70B", name: "meta-llama/Llama-3.1-70B", parameters_b: 70.6, architecture: "llama" },
    { label: "Mistral 7B", name: "mistralai/Mistral-7B-v0.3", parameters_b: 7.25, architecture: "mistral" },
    { label: "Qwen2.5 7B", name: "Qwen/Qwen2.5-7B", parameters_b: 7.62, architecture: "qwen2" },
    { label: "Qwen2.5 14B", name: "Qwen/Qwen2.5-14B", parameters_b: 14.7, architecture: "qwen2" },
    { label: "Gemma 2 9B", name: "google/gemma-2-9b", parameters_b: 9.24, architecture: "gemma2" },
    { label: "Phi-3 mini 3.8B", name: "microsoft/Phi-3-mini-4k-instruct", parameters_b: 3.82, architecture: "phi3" },
  ];
  const FORM_KEYS = ["task", "model", "method", "prec", "quant", "ctx", "conc", "seq", "mbs", "budget", "region", "maxp"];
  const blankState = () => ({ task: "", model: "", method: "", prec: "", quant: "", ctx: "", conc: "", seq: "", mbs: "", budget: "", region: "", maxp: "" });
  const normKey = s => String(s || "").toLowerCase().replace(/[^a-z0-9.]+/g, "");
  // "Llama 3.1 8B" / "meta-llama/Llama-3.1-8B" -> the suggestion; "7B" / "1.5 billion" / "350M" -> a size only;
  // anything else is sent as the model name for the server to resolve (never guessed here).
  function modelFromText(text) {
    const t = String(text == null ? "" : text).trim().slice(0, 200);
    if (!t) return null;
    const m = MODELS.find(x => normKey(x.label) === normKey(t) || normKey(x.name) === normKey(t));
    if (m) return { name: m.name, parameters_b: m.parameters_b, architecture: m.architecture };
    const p = /^~?\s*(\d+(?:\.\d+)?)\s*(b|bn|billion|m|million)?\s*(params|parameters)?$/i.exec(t);
    if (p) {
      const n = +p[1], unit = (p[2] || "b").toLowerCase();
      const b = unit[0] === "m" ? n / 1000 : n;
      if (b > 0 && b <= 100000) return { name: null, parameters_b: +b.toFixed(4), architecture: null };
    }
    return { name: t, parameters_b: null, architecture: null };
  }
  const num = v => (v === "" || v == null || !isFinite(+v) ? null : +v);
  const int = v => { const n = num(v); return n != null && Number.isInteger(n) ? n : null; };
  const taskType = task => (TASKS.find(x => x[0] === task) || [])[3] || null;
  // Field checks (only what is filled in); {key: message}
  function formErrors(st) {
    const e = {};
    const pos = (k, label, o) => {
      const v = st[k];
      if (v === "" || v == null) return;
      const n = num(v);
      if (n == null) e[k] = label + " must be a number";
      else if (o.int && !Number.isInteger(n)) e[k] = label + " must be a whole number";
      else if (n <= 0) e[k] = label + " must be more than 0";
      else if (o.max != null && n > o.max) e[k] = `${label} must be at most ${o.max.toLocaleString("en-US")}`;
    };
    pos("ctx", "Context length", { int: true, max: 10000000 });
    pos("conc", "Concurrent requests", { int: true, max: 100000 });
    pos("seq", "Sequence length", { int: true, max: 10000000 });
    pos("mbs", "Micro batch size", { int: true, max: 100000 });
    pos("budget", "Budget", { max: 10000000 });
    pos("maxp", "Max price", { max: 1000 });
    if (st.model && String(st.model).length > 200) e.model = "Model name is too long";
    return e;
  }
  // form state + answers -> WorkloadRequirements (source "form"). Only what the user said; unknown stays absent.
  function requirementsFromForm(st, answers) {
    st = Object.assign(blankState(), st || {});
    const wt = taskType(st.task);
    const req = { source: "form", workload_type: wt };
    const m = modelFromText(st.model);
    if (m) req.model = m;
    if (PRECISIONS.includes(st.prec)) req.precision = st.prec;
    if (QUANTS.some(q => q[0] === st.quant)) req.quantization = st.quant;
    if (wt === "training" || wt === "fine_tuning") {
      req.training = {
        method: wt === "training" ? "full" : (METHODS.some(x => x[0] === st.method) ? st.method : null),   // from scratch = every weight trains
        seq_len: int(st.seq) > 0 ? int(st.seq) : null, micro_batch_size: int(st.mbs) > 0 ? int(st.mbs) : null,
      };
    }
    if (wt === "inference") req.inference = { context_len: int(st.ctx) > 0 ? int(st.ctx) : null, concurrency: int(st.conc) > 0 ? int(st.conc) : null };
    req.constraints = { budget_usd: num(st.budget) > 0 ? num(st.budget) : null, max_price_per_gpu_hour: num(st.maxp) > 0 ? num(st.maxp) : null,
      region: REGIONS.some(r => r[0] && r[0] === st.region) ? st.region : null };
    return prune(applyAnswers(req, answers)) || { source: "form" };
  }
  function stateFromQuery(q) {
    q = q || {};
    const st = blankState();
    const str = (k, max) => (typeof q[k] === "string" ? q[k].slice(0, max || 200) : "");
    st.task = TASKS.some(t => t[0] === q.task) ? q.task : "";
    st.model = str("model");
    st.method = METHODS.some(m => m[0] === q.method) ? q.method : "";
    st.prec = PRECISIONS.includes(q.prec) ? q.prec : "";
    st.quant = QUANTS.some(x => x[0] === q.quant) ? q.quant : "";
    st.region = REGIONS.some(r => r[0] && r[0] === q.region) ? q.region : "";
    for (const k of ["ctx", "conc", "seq", "mbs", "budget", "maxp"]) st[k] = num(q[k]) != null ? str(k, 20) : "";
    return st;
  }
  // -> a patch for OG.qs.set: every form key present (null clears it from the URL)
  function queryFromState(st) {
    const out = {};
    for (const k of FORM_KEYS) out[k] = st && st[k] !== "" && st[k] != null ? String(st[k]) : null;
    if (out.task !== "finetune") out.method = null;
    if (out.task !== "infer") { out.ctx = null; out.conc = null; }
    if (out.task !== "train" && out.task !== "finetune") { out.seq = null; out.mbs = null; }
    return out;
  }

  /* ---- answers to the server's questions: {field: value}; ?a.<field>=value in the URL ---- */
  function leafSpec(field) {
    let spec = SCHEMA;
    for (const part of String(field || "").split(".")) {
      if (!spec || spec.t !== "obj" || !Object.prototype.hasOwnProperty.call(spec.props, part)) return null;
      spec = spec.props[part];
    }
    return spec && spec.t !== "obj" && spec.t !== "arr" && spec.t !== "scalar" ? spec : null;
  }
  function coerce(spec, v) {
    if (v == null || v === "") return undefined;
    if (spec.t === "enum") { const hit = spec.v.find(x => String(x) === String(v)); return hit === undefined ? undefined : hit; }
    if (spec.t === "bool") { if (v === true || v === "true" || v === "yes" || v === "1") return true; if (v === false || v === "false" || v === "no" || v === "0") return false; return undefined; }
    if (spec.t === "num" || spec.t === "int") { const n = num(v); if (n == null || n < 0 || n > spec.max || (spec.t === "int" && !Number.isInteger(n))) return undefined; return n; }
    if (spec.t === "str") { const s = String(v).trim(); return s && s.length <= spec.max ? s : undefined; }
    return undefined;
  }
  // req + {field: value} -> req with each valid answer set at its path (invalid / unknown fields ignored)
  function applyAnswers(req, answers) {
    const out = JSON.parse(JSON.stringify(req || {}));
    for (const [field, raw] of Object.entries(answers || {})) {
      const spec = leafSpec(field);
      if (!spec) continue;
      const v = coerce(spec, raw);
      if (v === undefined) continue;
      const parts = field.split(".");
      let o = out;
      for (const p of parts.slice(0, -1)) { if (!isPlain(o[p])) o[p] = {}; o = o[p]; }
      o[parts[parts.length - 1]] = v;
    }
    return out;
  }
  function answersFromQuery(q) {
    const out = {};
    for (const [k, v] of Object.entries(q || {})) if (k.startsWith("a.") && leafSpec(k.slice(2)) && coerce(leafSpec(k.slice(2)), v) !== undefined) out[k.slice(2)] = String(v).slice(0, 200);
    return out;
  }
  function answersToQuery(answers, prevQuery) {
    const out = {};
    for (const k of Object.keys(prevQuery || {})) if (k.startsWith("a.")) out[k] = null;
    for (const [f, v] of Object.entries(answers || {})) if (v !== "" && v != null) out["a." + f] = String(v);
    return out;
  }
  const ENUM_LABEL = {
    "training.method": { full: "Full fine-tuning", lora: "LoRA", qlora: "QLoRA" },
    workload_type: { training: "Training", fine_tuning: "Fine-tuning", inference: "Inference", batch: "Batch", no_gpu: "No GPU" },
    quantization: { none: "None", nf4: "nf4 (bitsandbytes)", gptq: "GPTQ", awq: "AWQ" },
    "training.optimizer": { adamw: "AdamW", adamw_8bit: "AdamW 8-bit", adafactor: "Adafactor", sgd: "SGD" },
    "constraints.vendor": { NVIDIA: "NVIDIA", AMD: "AMD" },
  };
  // field -> {type: "enum"|"bool"|"int"|"num"|"text", options?: [[value, label]], min?, step?} | null (not answerable)
  function controlFor(field) {
    const spec = leafSpec(field);
    if (!spec) return null;
    if (spec.t === "enum") return { type: "enum", options: spec.v.map(v => [String(v), (ENUM_LABEL[field] || {})[v] || String(v)]) };
    if (spec.t === "bool") return { type: "bool", options: [["true", "Yes"], ["false", "No"]] };
    if (spec.t === "int") return { type: "int", min: 0, step: 1 };
    if (spec.t === "num") return { type: "num", min: 0, step: "any" };
    return { type: "text" };
  }

  /* =============================== pure: reading a Recommendation =============================== */
  const PICKS = [
    ["cheapest_market", "Cheapest advertised option", "The lowest observed price that fits the memory estimate. The market's answer — OpenGrid may not be able to run it for you."],
    ["best_for_workload", "Best for your workload", "Fits with headroom at a balanced price, fresh data. What we'd rent for this job."],
    ["best_executable", "Best OpenGrid can run", "The best option OpenGrid can actually launch for you today, after approval."],
  ];
  const TIERS = [["lower_cost", "Lower cost", "Cheapest options that still fit"], ["balanced", "Balanced", "Headroom and price in balance"], ["higher_performance", "Higher performance", "Faster or larger GPUs for more money"]];
  const DEMO_HREF = "/jobs/new?preset=mnist&sandbox=1";
  const COMPONENT_LABEL = { weights: "Model weights", gradients: "Gradients", optimizer: "Optimizer state", activations: "Activations", kv_cache: "KV cache", framework_overhead: "Framework overhead" };
  const arr = v => (Array.isArray(v) ? v : []);
  const est = rec => (rec && rec.estimate) || null;
  const optionById = (rec, id) => arr(rec && rec.options).find(o => o && o.option_id === id) || null;
  const finite = v => typeof v === "number" && isFinite(v);
  const AUTH_CODES = /auth|api_key|sign_in|signin|login|anonymous|scope/i;

  // Why there is no executable pick, from the options' own execution reasons (never assumed).
  function noExecutableReason(rec) {
    const opts = arr(rec && rec.options);
    if (!opts.length) return { text: "No option to run: nothing fits the estimate right now.", href: null };
    const reasons = opts.flatMap(o => arr(o.execution && o.execution.reasons));
    if (reasons.some(r => AUTH_CODES.test(String(r.code || "")))) return { text: "Sign in or use an API key: what OpenGrid can launch depends on your account.", href: "/keys", hrefLabel: "API keys" };
    if (opts.every(o => o.execution && o.execution.workload_supported === false))
      return { text: "OpenGrid can only launch its PyTorch MNIST training demo today. For this workload the options above are a recommendation: you rent the GPU from the provider directly.", href: DEMO_HREF, hrefLabel: "Try the training demo (free simulation)" };
    const counts = new Map();
    for (const r of reasons) { const k = r.reason || r.code; if (k) counts.set(k, (counts.get(k) || 0) + 1); }
    const top = [...counts.entries()].sort((a, b) => b[1] - a[1])[0];
    return { text: top ? "Nothing OpenGrid can launch for you: " + top[0] + "." : "The server marked no option as executable.", href: null };
  }
  // rec -> the three picks, each {key, label, blurb, option, reason, empty}
  function pickCards(rec) {
    const picks = (rec && rec.picks) || {};
    return PICKS.map(([key, label, blurb]) => {
      const ref = picks[key];
      const option = ref && ref.option_id ? optionById(rec, ref.option_id) : null;
      let empty = null;
      if (!option) {
        if (key === "best_executable") empty = noExecutableReason(rec);
        else if (key === "cheapest_market") empty = { text: "No fresh listing fits the memory estimate" + (arr(rec && rec.excluded).length ? ` (${arr(rec.excluded).length} excluded below)` : "") + ".", href: null };
        else empty = { text: "No fresh listing fits with headroom right now.", href: null };
      }
      return { key, label, blurb, option, reason: ref && ref.reason ? String(ref.reason) : null, empty };
    });
  }
  // Deployable ONLY when the server says executable AND the price is fresh AND it is not sold out.
  // -> {code, label, tone, deployable, detail, reasons}
  function readiness(o) {
    const ex = (o && o.execution) || {};
    const reasons = arr(ex.reasons);
    const pname = o ? o.provider_display || (OG.providerName ? OG.providerName(o.provider) : o.provider) : "the provider";
    if (o && o.available === false) return { code: "sold_out", label: "Sold out — not deployable", tone: "bad", deployable: false, detail: "The provider reported no capacity at the last observation.", reasons };
    if (o && o.fresh === false) return { code: "stale", label: "Price is stale — not deployable", tone: "warn", deployable: false, detail: "Not observed recently; the price and availability may have changed.", reasons };
    if (ex.executable === true) return { code: "executable", label: "OpenGrid can run this", tone: "good", deployable: true,
      detail: "Launch goes through a quote and an OpenGrid approval." + (o.available == null ? " Availability is not reported by the provider." : ""), reasons };
    if (ex.workload_supported === false) return { code: "recommendation_only", label: "Recommendation only — OpenGrid can't run this workload yet", tone: "", deployable: false,
      detail: `Rent it from ${pname} directly. OpenGrid only launches its PyTorch MNIST training demo today.`, reasons };
    const r0 = reasons[0];
    return { code: "blocked", label: "Recommendation only — OpenGrid can't launch this for you yet", tone: "", deployable: false, detail: r0 ? r0.reason || r0.code : "Not marked executable by the server.", reasons };
  }
  function availability(o) {
    if (!o || o.available == null) return { label: "Availability not reported", tone: "" };
    return o.available ? { label: "Available", tone: "good" } : { label: "Sold out", tone: "bad" };
  }
  const round1 = v => Math.round(v * 10) / 10;
  // option (+ estimate) -> {known, needed, vram, fill (0..1), headroom_pct, fits, over}
  function memoryFit(o, e) {
    const mf = (o && o.memory_fit) || {};
    const m = (e && e.memory) || {};
    const needed = finite(mf.needed_per_gpu_gb) ? mf.needed_per_gpu_gb : finite(m.per_gpu_gb) ? m.per_gpu_gb : null;
    const vram = finite(mf.vram_gb) ? mf.vram_gb : o && finite(o.vram_gb) ? o.vram_gb : null;
    if (needed == null || vram == null || vram <= 0) return { known: false, needed, vram, fill: null, headroom_pct: null, fits: typeof mf.fits === "boolean" ? mf.fits : null, over: false };
    const headroom = finite(mf.headroom_pct) ? mf.headroom_pct : round1((vram - needed) / vram * 100);
    const fits = typeof mf.fits === "boolean" ? mf.fits : needed <= vram;
    return { known: true, needed, vram, fill: Math.max(0, Math.min(1, needed / vram)), headroom_pct: headroom, fits, over: !fits };
  }
  function fmtGb(v) {
    if (!finite(v)) return "–";
    const s = v >= 100 || Number.isInteger(v) ? String(Math.round(v)) : v.toFixed(1);
    return s + " GB";
  }
  // Runtime is unknown unless the server says known AND gives a measured number: never a made-up figure.
  function runtimeInfo(e) {
    const r = (e && e.runtime) || {};
    const reason = r.reason || "no measured benchmark for this workload";
    if (r.known !== true) return { known: false, text: "Unknown", reason };
    const lo = Array.isArray(r.range_hours) ? r.range_hours[0] : null, hi = Array.isArray(r.range_hours) ? r.range_hours[1] : null;
    if (finite(lo) && finite(hi)) return { known: true, text: `${+lo.toFixed(2)}–${+hi.toFixed(2)} h`, reason: r.basis || r.reason || "measured benchmark" };
    if (finite(r.hours)) return { known: true, text: `${+r.hours.toFixed(2)} h`, reason: r.basis || r.reason || "measured benchmark" };
    return { known: false, text: "Unknown", reason };
  }
  // memory.components_gb -> [{key, label, gb, share}] largest first (zero parts dropped)
  function componentRows(memory) {
    const c = (memory && memory.components_gb) || {};
    const rows = Object.entries(c).filter(([, v]) => finite(v) && v > 0).map(([k, v]) => ({ key: k, label: COMPONENT_LABEL[k] || k.replace(/_/g, " "), gb: v }));
    const total = rows.reduce((s, r) => s + r.gb, 0);
    rows.forEach(r => { r.share = total > 0 ? r.gb / total : 0; });
    return rows.sort((a, b) => b.gb - a.gb);
  }
  function tiersOf(options) {
    const opts = arr(options);
    const out = TIERS.map(([tier, label, blurb]) => ({ tier, label, blurb, options: opts.filter(o => o && o.tier === tier) }));
    const other = opts.filter(o => o && !TIERS.some(t => t[0] === o.tier));
    if (other.length) out.push({ tier: "other", label: "Other", blurb: "", options: other });
    return out.filter(t => t.options.length);
  }
  // needs_gpu === false (only on evidence) -> {reason}; else null
  function noGpu(rec) {
    const e = est(rec);
    return e && e.needs_gpu === false ? { reason: e.needs_gpu_reason || "Nothing in this workload needs a GPU." } : null;
  }
  function costLines(o) {
    const c = (o && o.cost) || {};
    return {
      scenarios: arr(c.scenarios).filter(s => s && finite(s.hours) && finite(s.total_usd)).map(s => ({ hours: s.hours, total_usd: s.total_usd, assumption: s.assumption || `${s.hours} h of runtime` })),
      budget_hours: finite(c.budget_hours) ? c.budget_hours : null,
      unknown_costs: arr(c.unknown_costs).map(String),
      billing_unit: c.billing_unit || null,
    };
  }
  function gpuHref(o) { return "/gpu/" + encodeURIComponent((o && (o.gpu_slug || (o.gpu && OG.slug ? OG.slug(o.gpu) : ""))) || ""); }
  function routeHref(o) {
    const u = o && o.execution && o.execution.route_preview_url;
    if (typeof u === "string" && /^\/route(\?|$)/.test(u)) return u;
    return "/route?gpu=" + encodeURIComponent((o && o.gpu_slug) || "") + "&count=" + encodeURIComponent((o && o.gpu_count) || 1);
  }
  function questionsOf(rec) {
    const seen = new Set(), out = [];
    for (const q of arr(rec && rec.questions).concat(arr(est(rec) && est(rec).questions))) {
      if (!q || !q.field || seen.has(q.field)) continue;
      seen.add(q.field); out.push(q);
    }
    return out;
  }
  const usd = v => (finite(v) ? (v > 0 && v < 0.01 ? "<$0.01" : "$" + v.toFixed(2)) : "–");
  const perH = v => (finite(v) ? "$" + v.toFixed(2) + "/GPU·h" : "–");

  OG.find = { SCHEMA, TASKS, METHODS, MODELS, PICKS, TIERS, FORM_KEYS, MAX_PASTE, DEMO_HREF, ANALYZER_KEYS,
    checkRequirements, validatePaste, prune, modelFromText, formErrors, requirementsFromForm, stateFromQuery, queryFromState,
    applyAnswers, answersFromQuery, answersToQuery, controlFor, leafSpec,
    pickCards, readiness, availability, memoryFit, fmtGb, runtimeInfo, componentRows, tiersOf, noGpu, costLines, gpuHref, routeHref,
    questionsOf, noExecutableReason, optionById };

  if (typeof OG.page !== "function" || typeof document === "undefined") return;   // node tests: pure part only

  /* =============================== views =============================== */
  OG.views = OG.views || {};
  const F = OG.find;
  const JOURNEYS = [
    { key: "find", n: "A", title: "Find the right GPU", sub: "Describe your model: get a memory estimate and the GPUs that fit, with observed prices.", href: "/find" },
    { key: "demo", n: "B", title: "Train an example model", sub: "Walk a real training job end to end on the PyTorch MNIST demo.", href: DEMO_HREF, badge: ["free simulation", "warn"] },
    { key: "byo", n: "C", title: "Bring your own project", sub: "Run opengrid analyze . on your code and paste the result here. Recommendation only.", href: "/find?mode=paste", badge: ["recommendation only", ""] },
  ];
  OG.views.startHere = active => h("nav", { class: "og-start", "aria-label": "Start here" },
    h("span", { class: "og-start-k" }, "START HERE"),
    h("div", { class: "og-start-cs" }, JOURNEYS.map(j => h("a", { class: "og-start-c" + (j.key === active ? " on" : ""), href: j.href, "aria-current": j.key === active ? "page" : null },
      h("span", { class: "og-start-n", "aria-hidden": "true" }, j.n),
      h("span", { class: "og-start-t" }, h("b", {}, j.title), j.badge ? OG.badge(j.badge[0], j.badge[1]) : null),
      h("span", { class: "og-start-s" }, j.sub)))));

  /* ---- one option ---- */
  function fitBar(o, e) {
    const f = memoryFit(o, e);
    if (!f.known) return h("div", { class: "fd-fit" }, h("span", { class: "dim" }, "Memory fit: ", OG.na("no memory estimate for this option")));
    const label = `${fmtGb(f.needed)} needed of ${fmtGb(f.vram)} per GPU`;
    return h("div", { class: "fd-fit" + (f.over ? " over" : f.headroom_pct < 10 ? " tight" : "") },
      h("div", { class: "fd-fit-bar", role: "img", "aria-label": label + (f.over ? ": does not fit" : `: ${Math.round(f.headroom_pct)}% headroom`) },
        h("i", { style: { width: (f.fill * 100).toFixed(1) + "%" } })),
      h("div", { class: "fd-fit-t" }, h("span", { class: "mono" }, label), " · ",
        f.over ? h("b", { class: "down" }, "does not fit") : h("span", { class: f.headroom_pct < 10 ? "warn-t" : "" }, `${Math.round(f.headroom_pct)}% headroom`)));
  }
  function readyBlock(o, compact) {
    const r = readiness(o);
    return h("div", { class: "fd-ready t-" + (r.tone || "plain") + " r-" + r.code },
      h("b", {}, r.label), compact && r.code === "recommendation_only" ? null : h("span", { class: "dim" }, r.detail),
      r.reasons.length && r.code !== "executable" ? h("details", { class: "fd-reasons" }, h("summary", {}, `${r.reasons.length} reason${r.reasons.length === 1 ? "" : "s"}`),
        h("ul", {}, r.reasons.map(x => h("li", {}, x.reason || x.code, x.code ? h("span", { class: "mono dimmer" }, " " + x.code) : null)))) : null);
  }
  function costBlock(o) {
    const c = costLines(o);
    return h("div", { class: "fd-cost" },
      h("div", { class: "fd-cost-h" }, h("span", { class: "fd-k" }, "Cost if it runs for"), OG.kindBadge("estimated")),
      c.scenarios.length ? h("ul", { class: "fd-scn" }, c.scenarios.map(s => h("li", { title: s.assumption }, h("span", { class: "dim" }, `${fmt.num(s.hours, s.hours % 1 ? 1 : 0)} h`), " ", h("b", { class: "mono" }, usd(s.total_usd)))))
        : h("span", { class: "dim" }, "No cost scenarios returned."),
      c.budget_hours != null ? h("div", { class: "fd-bh" }, "Your budget buys about ", h("b", { class: "mono" }, fmt.num(c.budget_hours, 1) + " h"), " of runtime") : null,
      c.unknown_costs.length ? h("div", { class: "fd-unk dim" }, "Not included: ", c.unknown_costs.join(", ")) : null,
      c.billing_unit ? h("div", { class: "fd-unk dimmer" }, "billed " + String(c.billing_unit).replace(/_/g, " ")) : null);
  }
  function actions(o, compact) {
    const r = readiness(o);
    return h("div", { class: "fd-acts" },
      o.gpu_slug || o.gpu ? h("a", { class: "btn", href: gpuHref(o) }, "View GPU") : null,
      h("a", { class: "btn", href: routeHref(o), title: "The order ticket: compare executable routes for this GPU (advanced)" }, "Preview route"),
      r.code !== "executable" && !compact ? h("a", { class: "btn fd-demo", href: DEMO_HREF, title: "A free simulation of the full training-job flow on PyTorch MNIST" }, "Try the training demo") : null);
  }
  function optionCard(o, e, opts) {
    opts = opts || {};
    const r = readiness(o);
    const tier = TIERS.find(t => t[0] === o.tier);
    const variant = [o.gpu, o.form_factor && !String(o.gpu || "").includes(o.form_factor) ? o.form_factor : null].filter(Boolean).join(" · ");
    const why = arr(o.why), lim = arr(o.limitations);
    const lists = h("div", { class: "fd-lists" },
      why.length ? h("div", {}, h("span", { class: "fd-k" }, "Why"), h("ul", { class: "fd-why" }, why.map(x => h("li", {}, String(x))))) : null,
      lim.length ? h("div", {}, h("span", { class: "fd-k" }, "Limitations"), h("ul", { class: "fd-lim" }, lim.map(x => h("li", {}, String(x))))) : null);
    return h("article", { class: "fd-card r-" + r.code + (opts.compact ? " compact" : ""), "aria-label": `${OG.shortGpu(o.gpu || "")} on ${o.provider_display || o.provider}` },
      h("div", { class: "fd-card-h" },
        h("div", { class: "fd-card-g" }, h("a", { class: "lnk fd-gpu", href: gpuHref(o) }, `${o.gpu_count > 1 ? o.gpu_count + "× " : ""}${OG.shortGpu(o.gpu || o.gpu_slug || "?")}`),
          h("span", { class: "fd-var dim", title: variant }, variant, finite(o.vram_gb) ? ` · ${fmtGb(o.vram_gb)}` : "")),
        opts.hideTier || !tier ? null : OG.badge(tier[1], "", tier[2])),
      h("div", { class: "fd-card-p" }, OG.logo(o.provider, 16), h("a", { class: "lnk", href: "/provider/" + encodeURIComponent(o.provider || "") }, o.provider_display || OG.providerName(o.provider)),
        o.region ? h("span", { class: "dim" }, "· " + o.region) : null),
      h("div", { class: "fd-px" },
        OG.kindBadge(o.price_kind === "observed" || !o.price_kind ? "observed" : o.price_kind),
        h("b", { class: "mono fd-price" }, perH(o.price_per_gpu_hour)),
        o.gpu_count > 1 && finite(o.price_per_instance_hour) ? h("span", { class: "dim mono" }, `${o.gpu_count} GPUs = ${usd(o.price_per_instance_hour)}/h`) : h("span", { class: "dim" }, "1 GPU"),
        o.age_seconds != null || o.observed_at ? OG.freshBadge(o.age_seconds != null ? o.age_seconds : o.observed_at, { provider: o.provider }) : null,
        o.fresh === false ? OG.badge("stale", "warn", "Not observed recently: not presented as deployable") : null,
        (a => OG.badge(a.label, a.tone))(availability(o))),
      fitBar(o, e),
      opts.compact ? h("details", { class: "fd-more" }, h("summary", {}, "Cost, why and limitations"), costBlock(o), lists) : [costBlock(o), lists],
      readyBlock(o, opts.compact),
      actions(o, opts.compact));
  }

  /* ---- the estimate ---- */
  function estimateBlock(e) {
    const m = (e && e.memory) || {};
    const rt = runtimeInfo(e);
    const rows = componentRows(m);
    const gc = (e && e.gpu_count) || {};
    const ic = (e && e.interconnect) || {};
    const conf = m.confidence || "none";
    const assumptions = arr(m.assumptions);
    const COLORS = ["var(--accent)", "var(--up)", "var(--warn)", "var(--purple-t)", "var(--blue-t2)", "var(--muted)"];
    return h("section", { class: "fd-est box", "aria-label": "Memory estimate" },
      h("div", { class: "fd-est-h" }, h("span", { class: "eyebrow" }, "Estimate"), OG.kindBadge("estimated"),
        OG.badge("confidence " + conf, conf === "high" ? "good" : conf === "medium" ? "" : "warn", "How sure the estimate is, given what was known"),
        m.method && m.method !== "unavailable" ? h("span", { class: "dim mono" }, String(m.method).replace(/_/g, " ")) : null),
      h("div", { class: "fd-est-g" },
        h("div", { class: "fd-big" },
          h("div", { class: "fd-k" }, "GPU memory needed"),
          finite(m.recommended_gb) ? h("div", { class: "fd-big-v mono" }, fmtGb(m.recommended_gb)) : h("div", { class: "fd-big-v" }, OG.na("no memory estimate")),
          h("div", { class: "dim fd-big-s" }, finite(m.recommended_gb) ? "recommended, with headroom" : "not enough information yet"),
          h("div", { class: "fd-big-l" },
            h("span", {}, h("span", { class: "dim" }, "Theoretical minimum "), h("b", { class: "mono" }, finite(m.min_total_gb) ? fmtGb(m.min_total_gb) : "–")),
            Array.isArray(m.range_gb) && finite(m.range_gb[0]) && finite(m.range_gb[1]) ? h("span", {}, h("span", { class: "dim" }, "Range "), h("b", { class: "mono" }, `${fmtGb(m.range_gb[0]).replace(" GB", "")}–${fmtGb(m.range_gb[1])}`)) : null,
            finite(m.per_gpu_gb) && gc.suggested > 1 ? h("span", {}, h("span", { class: "dim" }, "Per GPU "), h("b", { class: "mono" }, fmtGb(m.per_gpu_gb))) : null)),
        h("div", { class: "fd-facts" },
          fact("GPUs", gc.suggested != null ? `${gc.suggested}${gc.min != null && gc.min !== gc.suggested ? " (min " + gc.min + ")" : ""}` : null, gc.reason),
          fact("Runtime", rt.known ? rt.text : h("span", { class: "fd-unknown" }, "Unknown"), rt.reason),
          fact("Fast interconnect", ic.needed === true ? "Needed" : ic.needed === false ? "Not needed" : null, ic.reason)),
        rows.length ? h("div", { class: "fd-comp" }, h("div", { class: "fd-k" }, "Where the memory goes"),
          h("div", { class: "fd-comp-bar", role: "img", "aria-label": rows.map(r => `${r.label} ${fmtGb(r.gb)}`).join(", ") },
            rows.map((r, i) => h("i", { style: { width: (r.share * 100).toFixed(2) + "%", background: COLORS[i % COLORS.length] }, title: `${r.label}: ${fmtGb(r.gb)}` }))),
          h("ul", { class: "fd-comp-l" }, rows.map((r, i) => h("li", {}, h("i", { class: "sw", style: { background: COLORS[i % COLORS.length] } }), h("span", {}, r.label), h("b", { class: "mono" }, fmtGb(r.gb)))))) : null),
      assumptions.length ? h("div", { class: "fd-assume" }, h("span", { class: "fd-k" }, "Assumptions"),
        h("ul", {}, assumptions.slice(0, 4).map(a => h("li", {}, String(a)))),
        assumptions.length > 4 ? h("details", {}, h("summary", {}, `${assumptions.length - 4} more`), h("ul", {}, assumptions.slice(4).map(a => h("li", {}, String(a))))) : null) : null);
    function fact(label, value, sub) {
      return h("div", { class: "fd-fact" }, h("div", { class: "fd-k" }, label), h("div", { class: "fd-fact-v" }, value == null ? OG.na(sub || "unknown") : value), sub ? h("div", { class: "dim fd-fact-s" }, sub) : null);
    }
  }

  /* ---- questions with inline answers ---- */
  function questionsBlock(qs, answers, onAnswer) {
    if (!qs.length) return null;
    return h("section", { class: "fd-qs box", "aria-label": "Questions that sharpen the estimate" },
      h("div", { class: "fd-qs-h" }, h("span", { class: "eyebrow" }, "Sharpen the estimate"), h("span", { class: "dim" }, "Optional. Answer any of these and the recommendation updates.")),
      h("ul", {}, qs.map(q => {
        const c = controlFor(q.field);
        const id = "fdq-" + q.field.replace(/[^a-z0-9]/gi, "-");
        const cur = answers[q.field] != null ? String(answers[q.field]) : "";
        let ctl;
        if (!c) ctl = h("span", { class: "dim" }, "Answer by editing your input.");
        else if (c.options) ctl = h("div", { class: "fd-qopts", role: "group", "aria-labelledby": id }, c.options.map(([v, l]) => h("button", { type: "button", class: "btn sm", "aria-pressed": String(cur === v), onclick: () => onAnswer(q.field, cur === v ? null : v) }, l)));
        else {
          const inp = h("input", { class: "field num", type: c.type === "text" ? "text" : "number", min: c.min, step: c.step, value: cur, "aria-labelledby": id, inputmode: c.type === "int" ? "numeric" : c.type === "num" ? "decimal" : null,
            onkeydown: ev => { if (ev.key === "Enter") { ev.preventDefault(); onAnswer(q.field, inp.value); } } });
          ctl = h("span", { class: "fd-qin" }, inp, h("button", { type: "button", class: "btn sm", onclick: () => onAnswer(q.field, inp.value) }, "Update"));
        }
        return h("li", {}, h("div", {}, h("b", { id }, q.question || q.field), q.why ? h("span", { class: "dim" }, " — " + q.why) : null, h("span", { class: "mono dimmer fd-qf" }, q.field)), ctl);
      })));
  }

  /* ---- the recommendation screen ---- */
  OG.views.recommendation = (rec, opts) => {
    opts = opts || {};
    const e = est(rec);
    const kids = [];
    kids.push(h("div", { class: "fd-res-h" }, h("span", { class: "eyebrow" }, "Recommendation"),
      e && e.workload_type ? OG.badge(String(e.workload_type).replace(/_/g, " ")) : null,
      rec && rec.as_of ? h("span", { class: "dim" }, "market data as of ", h("span", { class: "mono" }, fmt.time(rec.as_of))) : null,
      h("span", { class: "spacer" }),
      h("a", { class: "lnk dim", href: (rec && rec.methodology) || (e && e.methodology) || "/methodology" }, "How this works →")));
    const ng = noGpu(rec);
    if (ng) {
      kids.push(h("section", { class: "fd-nogpu box", role: "status" }, h("h2", {}, "You probably don't need a GPU"), h("p", {}, ng.reason),
        h("p", { class: "dim" }, "Run it on your laptop or a regular CPU server. If you add a model or an ML framework (PyTorch, TensorFlow, JAX…), check again."),
        h("div", { class: "fd-acts" }, h("a", { class: "btn", href: "/find" }, "Describe a different workload"), h("a", { class: "btn", href: DEMO_HREF }, "Try the training demo"))));
      if (arr(rec && rec.notes).length) kids.push(h("ul", { class: "note fd-notes" }, arr(rec.notes).map(n => h("li", {}, String(n)))));
      return h("div", { class: "fd-res" }, kids);
    }
    if (e) kids.push(estimateBlock(e));
    if (opts.onAnswer) kids.push(questionsBlock(questionsOf(rec), opts.answers || {}, opts.onAnswer));
    if (opts.estimateOnly) {
      kids.push(OG.insufficient(opts.estimateOnly, "Market options not available"));
      return h("div", { class: "fd-res" }, kids);
    }
    const picks = pickCards(rec);
    kids.push(h("section", { class: "fd-picks", "aria-label": "Three picks" }, picks.map(p => h("div", { class: "fd-pick p-" + p.key + (p.option ? "" : " empty") },
      h("div", { class: "fd-pick-h" }, h("h3", {}, p.label), h("p", { class: "dim" }, p.blurb)),
      p.option ? [p.reason ? h("div", { class: "fd-pick-r" }, p.reason) : null, optionCard(p.option, e, { hideTier: false })]
        : h("div", { class: "fd-pick-e" }, h("b", {}, "Nothing here"), h("p", {}, p.empty.text), p.empty.href ? h("a", { class: "btn", href: p.empty.href }, p.empty.hrefLabel || "Open") : null)))));
    const tiers = tiersOf(rec && rec.options);
    const total = arr(rec && rec.options).length;
    if (total) {
      const body = h("div");
      const view = OG.seg([["cards", "Cards"], ["table", "Table"]], opts.view || "cards", v => { draw(v); opts.onView && opts.onView(v); });
      const draw = v => body.replaceChildren(v === "table" ? optionTable(rec, e) : h("div", {}, tiers.map(t => h("div", { class: "fd-tier" },
        h("div", { class: "fd-tier-h" }, h("h3", {}, t.label), h("span", { class: "dim" }, t.blurb), h("span", { class: "dimmer" }, `${t.options.length} option${t.options.length === 1 ? "" : "s"}`)),
        h("div", { class: "fd-cards" }, t.options.map(o => optionCard(o, e, { compact: true, hideTier: true })))))));
      draw(opts.view || "cards");
      kids.push(h("section", { class: "sec fd-alts" }, h("div", { class: "fd-alts-h" }, h("h2", { class: "sec-h" }, `All options · ${total}`), view), body));
    } else kids.push(OG.empty("No GPU listing fits this workload right now. See what was excluded below, or relax your budget / max price."));
    const ex = arr(rec && rec.excluded);
    if (ex.length) kids.push(h("details", { class: "sec fd-excl" }, h("summary", { class: "sec-h" }, `Excluded · ${ex.length}`),
      OG.table({ columns: [
        { key: "gpu", label: "GPU", fmt: v => v ? OG.shortGpu(v) : "–" },
        { key: "provider", label: "Provider", fmt: v => v ? OG.providerName(v) : h("span", { class: "dim" }, "all") },
        { key: "code", label: "Why", fmt: v => v ? OG.badge(String(v).replace(/_/g, " ")) : "" },
        { key: "reason", label: "Detail", cls: "dim" },
      ], rows: ex, compact: true, csv: false })));
    if (arr(rec && rec.notes).length) kids.push(h("ul", { class: "note fd-notes" }, arr(rec.notes).map(n => h("li", {}, String(n)))));
    kids.push(h("p", { class: "note" }, "Prices are ", h("b", {}, "observed"), " list prices from provider data, not quotes. Memory, cost and fit are ", h("b", {}, "estimates"),
      " with the assumptions shown. Runtime is never guessed. Stale or sold-out listings are never shown as deployable."));
    return h("div", { class: "fd-res" }, kids);
  };
  function optionTable(rec, e) {
    return OG.table({
      columns: [
        { key: "tier", label: "Tier", fmt: v => (TIERS.find(t => t[0] === v) || [null, v || "–"])[1] },
        { key: "gpu", label: "GPU", fmt: (v, o) => h("a", { class: "lnk", href: gpuHref(o), title: v }, OG.shortGpu(v || "")) },
        { key: "provider", label: "Provider", fmt: (v, o) => h("span", { class: "fd-tp" }, OG.logo(v, 14), o.provider_display || OG.providerName(v)) },
        { key: "region", label: "Region", cls: "dim" },
        { key: "price_per_gpu_hour", label: "$/GPU·h", num: true, fmt: v => perH(v).replace("/GPU·h", ""), title: "observed" },
        { key: "gpu_count", label: "GPUs", num: true },
        { key: "fit", label: "Memory fit", value: o => memoryFit(o, e).headroom_pct, num: true, fmt: (v, o) => { const f = memoryFit(o, e); return f.known ? (f.over ? h("span", { class: "down" }, "does not fit") : `${fmtGb(f.needed).replace(" GB", "")}/${fmtGb(f.vram)} · ${Math.round(f.headroom_pct)}%`) : "–"; } },
        { key: "available", label: "Available", value: o => availability(o).label, fmt: (v, o) => OG.badge(availability(o).label.replace("Availability not reported", "not reported"), availability(o).tone) },
        { key: "age_seconds", label: "Seen", num: true, fmt: (v, o) => v != null || o.observed_at ? OG.freshBadge(v != null ? v : o.observed_at, { provider: o.provider }) : "–" },
        { key: "ready", label: "OpenGrid", value: o => readiness(o).code, fmt: (v, o) => { const r = readiness(o); return h("span", { class: "fd-tr t-" + (r.tone || "plain"), title: r.detail }, r.code === "executable" ? "can run" : r.code === "stale" ? "stale" : r.code === "sold_out" ? "sold out" : "recommendation only"); } },
        { key: "acts", label: "", sort: false, csv: false, fmt: (v, o) => h("span", { class: "fd-tacts" }, h("a", { class: "btn sm", href: gpuHref(o) }, "GPU"), h("a", { class: "btn sm", href: routeHref(o) }, "Route")) },
      ], rows: arr(rec.options), compact: true, csv: "opengrid-find-options.csv", rowKey: o => o.option_id, sort: { key: "price_per_gpu_hour", dir: "asc" },
    });
  }

  /* =============================== /find =============================== */
  OG.page("/find", {
    title: "Find Compute",
    nav: "find",
    mount(el, params, query, ctx) {
      const root = h("div", { class: "pg-find" });
      el.append(root);
      let st = stateFromQuery(query);
      let answers = answersFromQuery(query);
      let mode = query.mode === "paste" || query.req ? "paste" : "form";
      let pasteText = "";
      if (typeof query.req === "string" && query.req) { try { pasteText = JSON.stringify(JSON.parse(query.req), null, 2); } catch (e) { pasteText = query.req; } }
      let view = query.view === "table" ? "table" : "cards";
      let lastRec = null;

      const hasAssistant = typeof OG.views.assistantPanel === "function";
      const askBox = h("div", { class: "fd-ask box", hidden: true });
      let askMounted = false;
      const askBtn = hasAssistant ? h("button", { class: "btn", type: "button", "aria-expanded": "false", "aria-controls": "fd-ask", onclick: () => toggleAsk() }, "Ask in plain English") : null;
      askBox.id = "fd-ask";
      function toggleAsk(force) {
        const open = force != null ? force : askBox.hidden;
        askBox.hidden = !open;
        askBtn.setAttribute("aria-expanded", String(open));
        OG.qs.set({ ask: open ? "1" : null });
        if (open && !askMounted) {
          askMounted = true;
          try {
            OG.views.assistantPanel(askBox, { ctx, requirements: () => currentRequirements().requirements,
              onRequirements: req => { const c = checkRequirements(req); if (!c.ok) return c; mode = "paste"; pasteText = JSON.stringify(req, null, 2); drawInput(); run(); return c; },
              onRecommendation: rec => { lastRec = rec; drawResult(rec); } });
          } catch (e) { askBox.replaceChildren(OG.error(e)); }
        }
      }

      root.append(
        OG.head("Find Compute", "Describe your workload. OpenGrid estimates the GPU memory it needs and ranks the GPUs that fit, priced from observed market data. Free, no sign-in.",
          askBtn, h("a", { class: "btn", href: "/methodology" }, "Methodology")),
        OG.views.startHere(mode === "paste" ? "byo" : "find"),
        askBox);
      const inBox = h("div", { class: "fd-in box" });
      const out = h("div", { class: "fd-out", "aria-live": "polite" });
      root.append(h("div", { class: "fd-grid" }, inBox, out));

      /* ----- the input: guided form or paste ----- */
      const tabs = OG.tabs([["form", "Guided"], ["paste", "Paste from the CLI"]], mode, v => { mode = v; drawInput(); syncUrl(); });
      const inBody = h("div", { class: "fd-in-b" });
      inBox.append(h("div", { class: "fd-in-h" }, tabs), inBody);
      const sentPre = h("pre", { class: "fd-sent mono", tabindex: "0", "aria-label": "Request body that will be sent" });
      const sent = h("details", { class: "fd-sent-d" }, h("summary", {}, "What will be sent"), h("p", { class: "dim" }, "POST /v1/recommend — only this JSON leaves your browser."), sentPre);

      function currentRequirements() {
        if (mode === "paste") {
          const v = validatePaste(pasteText);
          return { ok: v.ok, errors: v.errors, requirements: v.ok ? applyAnswers(v.requirements, answers) : null, analyzer: v.analyzer };
        }
        const errs = formErrors(st);
        const req = requirementsFromForm(st, answers);
        const c = checkRequirements(req);
        return { ok: !Object.keys(errs).length && c.ok, errors: Object.values(errs).concat(c.errors), fieldErrors: errs, requirements: req };
      }
      function refreshSent() {
        const c = currentRequirements();
        sentPre.textContent = c.requirements ? JSON.stringify(c.requirements, null, 2) : "— fix the input first —";
        return c;
      }
      function syncUrl() {
        const patch = Object.assign(queryFromState(mode === "form" ? st : {}), answersToQuery(answers, OG.qs.all()), { mode: mode === "paste" ? "paste" : null, view: view === "table" ? "table" : null, req: null });
        if (mode === "paste") {
          const v = validatePaste(pasteText);
          const compact = v.ok ? JSON.stringify(v.requirements) : null;
          patch.req = compact && compact.length <= 1800 ? compact : null;
        }
        OG.qs.set(patch);
      }

      function drawInput() {
        tabs.set(mode);
        inBody.replaceChildren(mode === "paste" ? pasteForm() : guidedForm());
        refreshSent();
      }

      function guidedForm() {
        const errEls = {};
        const err = k => (errEls[k] = h("span", { class: "fd-err", "aria-live": "polite" }));
        const upd = (k, v) => { st[k] = v; showErr(); refreshSent(); };
        function showErr() { const e = formErrors(st); for (const [k, x] of Object.entries(errEls)) x.textContent = e[k] || ""; }
        const q = (n, title, hint, ...kids) => h("fieldset", { class: "fd-q" }, h("legend", {}, h("span", { class: "fd-qn" }, n), title), hint ? h("p", { class: "fd-hint" }, hint) : null, kids);
        const numIn = (k, label, attrs) => h("label", { class: "fd-f" }, h("span", { class: "fd-l" }, label),
          h("input", Object.assign({ class: "field", type: "number", min: 0, value: st[k], inputmode: "decimal", oninput: ev => upd(k, ev.target.value.trim()) }, attrs || {})), err(k));
        const taskCards = h("div", { class: "fd-tasks", role: "radiogroup", "aria-label": "What are you doing?" }, TASKS.map(([v, l, s]) => h("label", { class: "fd-task" + (st.task === v ? " on" : "") },
          h("input", { type: "radio", name: "fd-task", value: v, checked: st.task === v ? true : null, onchange: () => { st.task = v; drawInput(); syncUrl(); } }),
          h("b", {}, l), h("span", { class: "dim" }, s))));
        const wt = taskType(st.task);
        const modelIn = h("input", { class: "field fd-wide", type: "text", list: "fd-models", value: st.model, maxlength: 200, spellcheck: "false", autocomplete: "off",
          placeholder: "e.g. Llama 3.1 8B, Qwen/Qwen2.5-7B or 7B", "aria-label": "Model", oninput: ev => { upd("model", ev.target.value); drawModelNote(); } });
        const modelNote = h("span", { class: "fd-hint fd-mnote" });
        function drawModelNote() {
          const m = modelFromText(st.model);
          modelNote.textContent = !m ? "Leave blank if you don't know yet." : m.parameters_b != null && m.name ? `${m.name} · ${m.parameters_b}B parameters` : m.parameters_b != null ? `${m.parameters_b}B parameters (no specific model)` : "Sent as the model name; OpenGrid looks it up.";
        }
        drawModelNote();
        const chips = h("div", { class: "fd-chips" }, MODELS.slice(0, 5).map(m => h("button", { type: "button", class: "btn sm", "aria-pressed": String(normKey(st.model) === normKey(m.label)),
          onclick: () => { st.model = m.label; modelIn.value = m.label; drawModelNote(); refreshSent(); chips.querySelectorAll("button").forEach(b => b.setAttribute("aria-pressed", String(b.textContent === m.label))); } }, m.label)));
        const methodSeg = OG.seg([["", "Not sure"]].concat(METHODS.map(m => [m[0], m[1], m[2]])), st.method, v => { upd("method", v); methodHint.textContent = (METHODS.find(m => m[0] === v) || [, , "LoRA and QLoRA need far less memory than full fine-tuning."])[2]; });
        const methodHint = h("p", { class: "fd-hint" }, (METHODS.find(m => m[0] === st.method) || [, , "LoRA and QLoRA need far less memory than full fine-tuning."])[2]);
        const regionSel = h("select", { class: "field", "aria-label": "Region", onchange: ev => upd("region", ev.target.value) }, REGIONS.map(([v, l]) => h("option", { value: v, selected: st.region === v ? true : null }, l)));
        const precSel = h("select", { class: "field", "aria-label": "Precision", onchange: ev => upd("prec", ev.target.value) }, h("option", { value: "" }, "Not sure"), PRECISIONS.map(p => h("option", { value: p, selected: st.prec === p ? true : null }, p)));
        const quantSel = h("select", { class: "field", "aria-label": "Quantization", onchange: ev => upd("quant", ev.target.value) }, h("option", { value: "" }, "Not sure"), QUANTS.map(([v, l]) => h("option", { value: v, selected: st.quant === v ? true : null }, l)));
        const advOpen = !!(st.prec || st.quant || st.seq || st.mbs);
        const form = h("form", { class: "fd-form", novalidate: true, onsubmit: ev => { ev.preventDefault(); run(); } },
          q("1", "What are you doing?", null, taskCards),
          q("2", "Which model?", "A name, a Hugging Face id, or just its size.", modelIn, h("datalist", { id: "fd-models" }, MODELS.map(m => h("option", { value: m.label }))), chips, modelNote, err("model")),
          wt === "fine_tuning" ? q("3", "How will you fine-tune?", null, methodSeg, methodHint) : null,
          wt === "inference" ? q("3", "How will you serve it?", "Longer context and more parallel requests need more memory (KV cache).",
            h("div", { class: "fd-row2" }, numIn("ctx", "Context length (tokens)", { step: 1, placeholder: "e.g. 8192", inputmode: "numeric" }), numIn("conc", "Concurrent requests", { step: 1, placeholder: "e.g. 8", inputmode: "numeric" }))) : null,
          q(wt === "fine_tuning" || wt === "inference" ? "4" : "3", "Budget and location", "All optional. Options over your max price are left out.",
            h("div", { class: "fd-row2" }, numIn("budget", "Total budget ($)", { step: "any", placeholder: "e.g. 50" }), numIn("maxp", "Max $ per GPU-hour", { step: "any", placeholder: "e.g. 3" })),
            h("label", { class: "fd-f" }, h("span", { class: "fd-l" }, "Region"), regionSel)),
          h("details", { class: "fd-adv", open: advOpen ? true : null }, h("summary", {}, "Advanced: precision, quantization", wt === "training" || wt === "fine_tuning" ? ", batch" : ""),
            h("div", { class: "fd-row2" }, h("label", { class: "fd-f" }, h("span", { class: "fd-l" }, "Precision"), precSel), h("label", { class: "fd-f" }, h("span", { class: "fd-l" }, "Quantization"), quantSel)),
            wt === "training" || wt === "fine_tuning" ? h("div", { class: "fd-row2" }, numIn("seq", "Sequence length", { step: 1, placeholder: "e.g. 2048", inputmode: "numeric" }), numIn("mbs", "Micro batch size", { step: 1, placeholder: "e.g. 4", inputmode: "numeric" })) : null,
            h("p", { class: "fd-hint" }, "Unknown is fine: OpenGrid states what it assumed.")),
          h("div", { class: "fd-go" }, h("button", { class: "btn pri", type: "submit" }, "Find GPUs"),
            h("button", { class: "btn", type: "button", onclick: () => { st = blankState(); answers = {}; drawInput(); syncUrl(); drawIntro(); } }, "Reset")),
          sent);
        showErr();
        return form;
      }

      function pasteForm() {
        const ta = h("textarea", { class: "field fd-ta mono", rows: 10, spellcheck: "false", autocomplete: "off", "aria-label": "Analyzer JSON", "aria-describedby": "fd-pv",
          placeholder: '{ "requirements": { "workload_type": "fine_tuning", … } }' });
        ta.value = pasteText;
        const status = h("div", { id: "fd-pv", class: "fd-pv", "aria-live": "polite" });
        const go = h("button", { class: "btn pri", type: "submit" }, "Get recommendation");
        function check() {
          pasteText = ta.value;
          const v = validatePaste(pasteText);
          go.disabled = !v.ok;
          if (!pasteText.trim()) status.replaceChildren(h("span", { class: "dim" }, "Paste the JSON to check it. Nothing is sent until you press the button."));
          else if (!v.ok) status.replaceChildren(h("div", { class: "fd-bad" }, h("b", {}, `Can't use this yet — ${v.errors.length} problem${v.errors.length === 1 ? "" : "s"}`), h("ul", {}, v.errors.map(x => h("li", { class: "mono" }, x)))));
          else {
            const a = v.analyzer;
            status.replaceChildren(h("div", { class: "fd-good" }, h("b", {}, "Looks good"),
              h("span", {}, a ? `From opengrid analyze${a.version ? " v" + a.version : ""} · ${a.files_inspected.length} file${a.files_inspected.length === 1 ? "" : "s"} inspected · ${a.files_skipped.length} skipped. Only the requirements are sent; file names and paths stay in your browser.`
                : "A WorkloadRequirements object."),
              a && a.files_skipped.length ? h("span", { class: "dim" }, "Skipped: " + a.files_skipped.slice(0, 5).map(f => `${f.path} (${f.reason})`).join(", ")) : null,
              a && a.warnings.length ? h("span", { class: "warn-t" }, a.warnings.slice(0, 3).map(String).join(" · ")) : null));
          }
          refreshSent();
        }
        let t = null;
        ta.addEventListener("input", () => { clearTimeout(t); t = setTimeout(check, 150); });
        check();
        return h("form", { class: "fd-form", onsubmit: ev => { ev.preventDefault(); check(); if (validatePaste(pasteText).ok) run(); } },
          h("div", { class: "fd-byo" },
            h("b", {}, "Bring your own project"),
            h("ol", {},
              h("li", {}, "In your project folder, run ", h("code", {}, "opengrid analyze . --json"), OG.copy("opengrid analyze . --json", { label: "copy", title: "Copy the command" }),
                h("div", { class: "fd-hint" }, "It reads only allowlisted files (dependencies, configs, scripts) on your machine. It never runs your code, never reads secrets, weights or datasets.")),
              h("li", {}, "Paste its output below. Only the ", h("code", {}, "requirements"), " part is sent."),
              h("li", {}, "Using a coding assistant? The OpenGrid MCP server runs the same analysis. ", h("a", { class: "lnk fd-a", href: "/developers" }, "Developer docs →"))),
            h("p", { class: "fd-hint" }, "OpenGrid recommends GPUs for any workload but only launches its PyTorch MNIST demo today: your own project gets a recommendation, not a launch.")),
          h("label", { class: "fd-f" }, h("span", { class: "fd-l" }, "Analyzer output (JSON, up to 64 KB)"), ta),
          status,
          h("div", { class: "fd-go" }, go, h("button", { class: "btn", type: "button", onclick: () => { ta.value = ""; check(); syncUrl(); } }, "Clear")),
          sent);
      }

      /* ----- results ----- */
      function drawIntro() {
        out.replaceChildren(h("div", { class: "fd-intro" },
          h("h2", {}, "What you'll get"),
          h("ol", {},
            h("li", {}, h("b", {}, "A memory estimate"), " — how much GPU memory your workload needs, what it's made of, and every assumption."),
            h("li", {}, h("b", {}, "Three picks"), " — the cheapest advertised option, the best for your workload, and the best OpenGrid can actually run."),
            h("li", {}, h("b", {}, "Alternatives"), " — lower cost, balanced and higher performance, each with observed price, memory fit and cost per hour of runtime.")),
          h("p", { class: "dim" }, "Unknowns are fine: answer what you know, and OpenGrid tells you what it assumed and what to ask next."),
          h("p", { class: "dim" }, "Just want to see a training job end to end? ", h("a", { class: "lnk fd-a", href: DEMO_HREF }, "Train the example model (free simulation) →"))));
      }
      function drawResult(rec, extra) {
        out.replaceChildren(OG.views.recommendation(rec, Object.assign({ answers, view, onView: v => { view = v; syncUrl(); },
          onAnswer: (field, value) => { if (value == null || value === "") delete answers[field]; else answers[field] = value; syncUrl(); refreshSent(); run(); } }, extra || {})));
      }
      async function run() {
        const c = refreshSent();
        if (!c.ok) {
          out.replaceChildren(h("div", { class: "og-alarm t-warn", role: "alert" }, h("b", {}, "Check your input"), h("ul", { class: "fd-errl" }, c.errors.slice(0, 8).map(x => h("li", {}, x)))));
          return;
        }
        syncUrl();
        out.replaceChildren(OG.loading("Estimating memory and ranking GPUs that fit…"));
        if (window.matchMedia && window.matchMedia("(max-width: 1100px)").matches) out.scrollIntoView({ block: "start", behavior: "smooth" });
        try {
          let rec = await ctx.api("/v1/recommend", { method: "POST", body: c.requirements, slot: "find-recommend" });
          if (rec && !rec.estimate && rec.data && rec.data.estimate) rec = rec.data;   // a {data} wrapper without meta
          lastRec = rec;
          drawResult(rec);
        } catch (e) {
          if (e.stale) return;
          if (e.status === 404 || e.status === 405) {
            try {
              const est1 = await ctx.api("/v1/recommend/estimate", { method: "POST", body: c.requirements, slot: "find-recommend" });
              drawResult({ estimate: est1, questions: est1 && est1.questions }, { estimateOnly: "POST /v1/recommend is not available on this server yet: this is the memory estimate only, without market options." });
            } catch (e2) {
              if (e2.stale) return;
              out.replaceChildren(e2.status === 404 || e2.status === 405 ? OG.insufficient("The recommendation engine (POST /v1/recommend) is not available on this server yet.", "Not available yet") : errBox(e2));
            }
            return;
          }
          out.replaceChildren(errBox(e));
        }
      }
      function errBox(e) {
        const d = e && e.body && e.body.detail;
        const list = Array.isArray(d) ? d.map(x => `${(x.loc || []).filter(p => p !== "body").join(".")}: ${x.msg}`) : null;
        return h("div", { class: "og-alarm" + (e && e.status >= 500 ? "" : " t-warn"), role: "alert" },
          h("b", {}, e && e.status === 422 ? "The server could not use this input" : e && e.status ? "Request failed (HTTP " + e.status + ")" : "Network error"),
          h("span", {}, d && typeof d === "object" && !Array.isArray(d) ? d.message || d.code : (e && e.message) || ""),
          list ? h("ul", { class: "fd-errl" }, list.slice(0, 10).map(x => h("li", { class: "mono" }, x))) : null,
          h("button", { class: "btn sm", type: "button", onclick: () => run() }, "Try again"));
      }

      drawInput();
      const hasInput = mode === "paste" ? !!pasteText.trim() : FORM_KEYS.some(k => st[k]) || Object.keys(answers).length > 0;
      if (hasInput) run(); else drawIntro();
      if (hasAssistant && query.ask === "1") toggleAsk(true);
    },
  });
})();
