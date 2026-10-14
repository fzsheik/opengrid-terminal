/* /jobs — training jobs (the founder's flow: Execute → Jobs → New Training Job → Preview Route → Submit →
   Approve → Train → Results).
   /jobs        job history (GET /v1/jobs) + the card-free "Train Your First AI Model — PyTorch MNIST" CTA.
   /jobs/new    the job form: templates from GET /v1/jobs/templates, the MNIST preset (founder's defaults),
                client-side bounds that mirror the server's, Preview route (POST /v1/jobs/preview: MARKET
                RECOMMENDATION vs EXECUTION-READY ROUTE vs SANDBOX, blockers with actionable links) and Submit
                (POST /v1/jobs, one Idempotency-Key per intent, confirmation with the cost exposure).
   /jobs/:id    lives in pages/job.js (detail, polling, logs, metrics, checks, artifacts, cost, cancel).
   A JOB is the workload and its outcome; a DEPLOYMENT is the cloud resource. Both states are shown, never merged.
   Nothing here invents an executable route: a candidate is "execution-ready" only when the server says so and
   no blocker stands. Pure helpers live on OG.jobs (tested by tests/test_web_jobs.js). */
(() => {
  const { h, fmt } = OG;

  /* =============================== pure =============================== */
  // Job lifecycle (JOBS_BRIEF): the main flow; failure / uncertain states are separate.
  const FLOW = ["created", "validating", "quoted", "pending_approval", "approved", "provisioning", "initializing", "training",
    "saving_artifacts", "training_completed", "terminating", "termination_confirmed", "reconciliation_pending", "reconciled"];
  const FAILED = ["validation_failed", "no_eligible_provider", "quote_expired", "approval_rejected", "bootstrap_failed", "training_failed",
    "out_of_memory", "training_timed_out", "artifact_upload_failed", "termination_failed"];
  const UNCERTAIN = ["provider_timeout", "provisioning_uncertain", "orphan_suspected", "credentials_unavailable"];
  const CANCEL = ["cancel_requested", "cancelling"];
  // Nothing was ever launched: the job is over (jobs/states.py FINISHED minus reconciled). Failures after a
  // launch still go through terminating → reconciled. quote_expired is NOT final: it can be re-quoted.
  const ENDED_BEFORE_LAUNCH = ["validation_failed", "no_eligible_provider", "approval_rejected"];
  const FINISHED = ["reconciled"].concat(ENDED_BEFORE_LAUNCH);
  // Sandbox (SIMULATED) jobs end where the simulation ends: nothing existed to terminate.
  const SANDBOX_FLOW = ["created", "validating", "initializing", "training", "saving_artifacts", "training_completed"];
  const SANDBOX_FINISHED = ["training_completed", "validation_failed", "bootstrap_failed", "training_failed", "out_of_memory", "training_timed_out", "artifact_upload_failed"];
  const PRE_LAUNCH = ["created", "validating", "quoted", "pending_approval", "approved", "quote_expired"];
  // States in which an instance may exist at the provider (may bill).
  const MAY_BILL = ["provisioning", "initializing", "training", "saving_artifacts", "training_completed", "terminating", "provider_timeout",
    "provisioning_uncertain", "bootstrap_failed", "training_failed", "out_of_memory", "training_timed_out", "artifact_upload_failed",
    "termination_failed", "orphan_suspected", "credentials_unavailable", "cancel_requested", "cancelling"];

  function tone(s) {
    if (FAILED.includes(s)) return "bad";
    if (UNCERTAIN.includes(s)) return "warn";
    if (s === "reconciled" || s === "training_completed" || s === "termination_confirmed") return "good";
    if (s === "training") return "good";
    if (CANCEL.includes(s) || ["pending_approval", "approved", "provisioning", "initializing", "saving_artifacts", "terminating", "reconciliation_pending", "validating"].includes(s)) return "busy";
    return "";
  }
  const stateOf = job => job ? job.status || job.state || null : null;
  // Terminal (stop polling): the server's `finished` flag wins (jobs/states.is_finished); else the same rule.
  function isTerminal(job) {
    if (!job) return false;
    if (typeof job.finished === "boolean") return job.finished;
    if (typeof job.terminal === "boolean") return job.terminal;
    if (job.finished_at) return true;
    const s = stateOf(job);
    return (isSandbox(job) ? SANDBOX_FINISHED : FINISHED).includes(s);
  }
  // The strip: main flow marks done / current / todo / skipped; an off-flow current state (failure, uncertain,
  // cancel) is inserted right after the furthest main-flow state reached (same rule as OG.dep.strip).
  // A sandbox job has its own shorter flow (no quote, approval, provisioning or termination).
  function strip(status, history, sandbox) {
    const F = sandbox ? SANDBOX_FLOW : FLOW;
    const seen = new Set((history || []).filter(Boolean));
    const known = seen.size > 0;
    const cur = F.indexOf(status);
    let after = cur;
    if (cur < 0) { after = -1; F.forEach((s, i) => { if (seen.has(s)) after = i; }); if (!known) after = 0; }
    const steps = F.map((s, i) => {
      let mark = i < (cur >= 0 ? cur : after + 1) ? "done" : "todo";
      if (i === cur) mark = "current";
      if (mark === "done" && known && !seen.has(s)) mark = "skipped";
      return { state: s, mark, tone: mark === "current" ? tone(s) : "" };
    });
    if (cur < 0 && status) steps.splice(after + 1, 0, { state: status, mark: "current", tone: tone(status) || "bad", off: true });
    return steps;
  }
  // Outcome (succeeded / failed / cancelled / simulated) is separate from the state and from resource disposition.
  const OUTCOME_TONE = { succeeded: "good", failed: "bad", cancelled: "warn", simulated: "" };
  function outcomeOf(job) {
    if (!job) return null;
    const o = job.outcome && typeof job.outcome === "object" ? job.outcome.outcome || job.outcome.value : job.outcome;
    return o || null;
  }
  const isSandbox = job => !!job && (job.sandbox === true || job.simulated === true || outcomeOf(job) === "simulated" || job.mode === "sandbox");

  // Money: "$1.23"; positive amounts under a cent are "<$0.01" (never shown as $0.00); null → "–".
  function usd(v, digits) {
    if (v == null || v === "" || !isFinite(+v)) return "–";
    const n = +v;
    if (n > 0 && n < 0.01) return "<$0.01";
    const d = digits == null ? 2 : digits;
    return (n < 0 ? "−$" : "$") + Number(Math.round(Math.abs(n) + "e" + d) + "e-" + d).toFixed(d);   // half-up, no float drift (0.215 → 0.22)
  }
  const perGpuH = v => (v == null || !isFinite(+v) ? "–" : "$" + (+v).toFixed(+v < 10 ? 3 : 2) + "/GPU·h");
  function duration(sec) {
    if (sec == null || !isFinite(+sec)) return "–";
    const s = Math.max(0, Math.round(+sec));
    if (s < 60) return s + "s";
    if (s < 3600) return Math.floor(s / 60) + "m " + String(s % 60).padStart(2, "0") + "s";
    if (s < 86400) return Math.floor(s / 3600) + "h " + String(Math.floor(s % 3600 / 60)).padStart(2, "0") + "m";
    return Math.floor(s / 86400) + "d " + Math.floor(s % 86400 / 3600) + "h";
  }
  function bytes(n) {
    if (n == null || !isFinite(+n)) return "–";
    n = +n;
    if (n < 1024) return n + " B";
    if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
    if (n < 1073741824) return (n / 1048576).toFixed(n < 10485760 ? 2 : 1) + " MB";
    return (n / 1073741824).toFixed(2) + " GB";
  }

  /* ---- the job form: the founder's preset and bounds that mirror the server ---- */
  const REGIONS = ["US", "Canada", "Europe", "UK", "APAC", "Middle East", "LATAM", "Africa"];
  const STRATEGIES = [
    ["BALANCED", "Balanced", "Price 40%, availability 20%, stability 15%, availability history 15%, freshness, integration"],
    ["CHEAPEST", "Cheapest", "Lowest observed price wins; no other factor counts"],
    ["FASTEST_AVAILABLE", "Fastest", "Availability now, OpenGrid integration and freshness first"],
    ["MOST_STABLE", "Stable", "Availability history and price stability first"],
  ];
  // Fallback bounds = workloads/templates.py pytorch-mnist, used only until GET /v1/jobs/templates answers with
  // the template's own (the server validates again regardless: 422 {code, message, field}).
  const DEFAULT_CONFIG = {
    epochs: { default: 3, min: 1, max: 10, step: 1, integer: true, label: "Epochs" },
    batch_size: { default: 128, min: 32, max: 512, step: 1, integer: true, label: "Batch size" },
    learning_rate: { default: 0.001, min: 0.00001, max: 0.1, step: 0.0001, label: "Learning rate" },
    seed: { default: 42, min: 0, max: 2147483647, step: 1, integer: true, label: "Seed" },
  };
  // template.limits_bounds (min_exclusive = strictly greater) + the template's gpu_count (exactly 1 for MNIST)
  const LIMIT_BOUNDS = {
    gpu_count: { min: 1, max: 1, integer: true },
    max_price_per_gpu_hour_usd: { min_exclusive: 0, max: 100 },
    max_total_cost_usd: { min_exclusive: 0, max: 100 },
    max_runtime_minutes: { min: 5, max: 60, integer: true },
  };
  const NAME_RE = /^[a-z0-9][a-z0-9._-]{0,63}$/;   // workloads/spec.py NAME_RE
  const SSH_RE = /^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)|sk-ssh-ed25519@openssh\.com|sk-ecdsa-sha2-nistp256@openssh\.com) [A-Za-z0-9+/]+={0,3}( [^\r\n]*)?$/;
  const LABEL = { epochs: "Epochs", batch_size: "Batch size", learning_rate: "Learning rate", seed: "Seed" };

  // The template's config_schema ({key: {type, min, max, default, description}}; JSON-schema properties and
  // minimum/maximum accepted too) → {key: {default, min, max, step, integer, label, hint}}.
  function configSpec(tpl) {
    const src = tpl && (tpl.config_schema || tpl.parameters);
    const props = src && src.properties && typeof src.properties === "object" ? src.properties : src;
    const out = {};
    if (!props || typeof props !== "object" || !Object.keys(props).length) {
      for (const [k, d] of Object.entries(DEFAULT_CONFIG)) out[k] = Object.assign({}, d);
    } else {
      for (const [k, v] of Object.entries(props)) {
        if (!v || typeof v !== "object") continue;
        const base = DEFAULT_CONFIG[k] || { label: LABEL[k] || k.replace(/_/g, " "), step: "any" };
        const min = v.min ?? v.minimum, max = v.max ?? v.maximum;
        const integer = v.type ? v.type === "integer" : !!base.integer;
        out[k] = Object.assign({}, base, { default: v.default != null ? v.default : base.default, min: min != null ? min : base.min,
          max: max != null ? max : base.max, integer, label: v.label || v.title || base.label, hint: v.description || null, step: integer ? 1 : base.step || "any" });
      }
    }
    const pre = tpl && tpl.preset && tpl.preset.config;
    if (pre) for (const [k, v] of Object.entries(pre)) if (out[k] && typeof v === "number") out[k].default = v;
    return out;
  }
  function limitBounds(tpl) {
    const lb = (tpl && tpl.limits_bounds) || {};
    const out = {};
    for (const [k, d] of Object.entries(LIMIT_BOUNDS)) out[k] = Object.assign({}, d, lb[k] || {});
    const n = tpl && tpl.requirements && tpl.requirements.gpu_count;
    if (n) out.gpu_count = { min: n, max: n, integer: true };
    return out;
  }
  function templateId(t) { return t ? t.id || t.template || t.slug || t.name : null; }
  // The founder's defaults for the MNIST preset (JOBS_BRIEF; the template carries the same as .preset).
  const FOUNDER = { name: "opengrid-first-training-test", job_type: "training", gpu: "auto", gpu_count: 1, min_vram_gb: null, region: "US",
    strict_region: false, allow_spot: false, strategy: "BALANCED", max_price_per_gpu_hour_usd: 3, max_total_cost_usd: 2, max_runtime_minutes: 30 };
  function preset(tpl) {
    const spec = configSpec(tpl);
    const config = {};
    for (const [k, v] of Object.entries(spec)) if (v.default != null) config[k] = v.default;
    const p = (tpl && tpl.preset) || {};
    const r = p.resources || {}, l = p.limits || {}, ro = p.routing || {};
    const pick = (v, d) => (v === undefined ? d : v);
    return {
      name: pick(p.name, FOUNDER.name), job_type: pick(p.job_type, FOUNDER.job_type), template: templateId(tpl) || "pytorch-mnist",
      config, gpu: pick(r.gpu, FOUNDER.gpu), gpu_count: pick(r.gpu_count, FOUNDER.gpu_count), min_vram_gb: pick(r.min_vram_gb, null),
      region: pick(r.region, FOUNDER.region), strict_region: pick(r.strict_region, false), allow_spot: false,
      strategy: pick(ro.strategy, FOUNDER.strategy), max_price_per_gpu_hour_usd: pick(l.max_price_per_gpu_hour_usd, FOUNDER.max_price_per_gpu_hour_usd),
      max_total_cost_usd: pick(l.max_total_cost_usd, FOUNDER.max_total_cost_usd), max_runtime_minutes: pick(l.max_runtime_minutes, FOUNDER.max_runtime_minutes),
      ssh_public_key: "", sandbox: false,
    };
  }
  const numOrNull = v => (v == null || v === "" || !isFinite(+v) ? null : +v);
  function checkNum(v, b, label) {
    const n = numOrNull(v);
    if (n == null) return label + " is required";
    if (b.integer && !Number.isInteger(n)) return label + " must be a whole number";
    if (b.min_exclusive != null && n <= b.min_exclusive) return `${label} must be > ${b.min_exclusive}`;
    if (b.min != null && n < b.min) return `${label} must be ≥ ${b.min}`;
    if (b.max != null && n > b.max) return `${label} must be ≤ ${b.max}`;
    return null;
  }
  const LIMIT_LABEL = { gpu_count: "GPU count", max_price_per_gpu_hour_usd: "Max $/GPU·h", max_total_cost_usd: "Max total $", max_runtime_minutes: "Max runtime (min)" };
  // form → {ok, errors: {field: message}, body} (the POST /v1/jobs[/preview] body). Mirrors workloads/spec.py.
  function validate(form, spec, lbounds) {
    spec = spec || configSpec(null);
    const lb = lbounds || LIMIT_BOUNDS;
    const errors = {};
    const name = String(form.name || "").trim();
    if (!name) errors.name = "Name is required";
    else if (!NAME_RE.test(name)) errors.name = "1–64 characters a-z 0-9 . _ - (lower case), starting with a letter or digit";
    if (!form.template) errors.template = "Pick a template";
    if ((form.job_type || "training") !== "training") errors.job_type = "Only training is supported in this release";
    const config = {};
    for (const [k, b] of Object.entries(spec)) {
      const e = checkNum(form.config && form.config[k], b, b.label || k);
      if (e) errors["config." + k] = e; else config[k] = +form.config[k];
    }
    for (const k of Object.keys(LIMIT_BOUNDS)) {
      const e = checkNum(form[k], lb[k] || LIMIT_BOUNDS[k], LIMIT_LABEL[k]);
      if (e) errors[k] = e;
    }
    if (form.min_vram_gb != null && form.min_vram_gb !== "") { const e = checkNum(form.min_vram_gb, { min_exclusive: 0, max: 1024 }, "Min VRAM"); if (e) errors.min_vram_gb = e; }
    if (form.region && !REGIONS.includes(form.region)) errors.region = "Unknown region group";
    if (form.strict_region && !form.region) errors.region = "Strict region needs a region group";
    if (form.allow_spot) errors.allow_spot = "Spot / interruptible capacity is refused in this release";
    if (!STRATEGIES.some(s => s[0] === form.strategy)) errors.strategy = "Unknown strategy";
    const key = String(form.ssh_public_key || "").trim();
    if (key) {
      if (/PRIVATE KEY/.test(key)) errors.ssh_public_key = "That is a PRIVATE key. Never paste it anywhere: paste the .pub file (one line starting ssh-ed25519 …)";
      else if (/[\r\n]/.test(key)) errors.ssh_public_key = "One key, one line";
      else if (!SSH_RE.test(key)) errors.ssh_public_key = "Not an OpenSSH public key (ssh-ed25519 AAAA… comment)";
    }
    const ok = !Object.keys(errors).length;
    const body = {
      name, job_type: form.job_type || "training", template: form.template, config,
      resources: { gpu: form.gpu || "auto", gpu_count: numOrNull(form.gpu_count) || 1, min_vram_gb: numOrNull(form.min_vram_gb),
        region: form.region || null, strict_region: !!(form.region && form.strict_region), allow_spot: !!form.allow_spot },
      routing: { strategy: form.strategy || "BALANCED", weights: null },
      limits: { max_price_per_gpu_hour_usd: numOrNull(form.max_price_per_gpu_hour_usd), max_total_cost_usd: numOrNull(form.max_total_cost_usd),
        max_runtime_minutes: numOrNull(form.max_runtime_minutes) },
      ssh_public_key: key || null,
      sandbox: !!form.sandbox,
    };
    return { ok, errors, body };
  }

  /* ---- preview labelling: three views, never a fake executable route ---- */
  const candKey = c => c ? [c.provider, c.listing_id, c.gpu].join("|") : null;
  const isCand = c => !!c && typeof c === "object" && !!(c.provider || c.listing_id);
  // Is this candidate executable? Only with the server's explicit executable === true (workloads/select.py) or,
  // for an execution_ready candidate from an older shape, no contrary flag. Readiness reasons always veto.
  function candExecutable(c, isExecSlot) {
    if (!isCand(c)) return false;
    const reasons = c.readiness && Array.isArray(c.readiness.reasons) ? c.readiness.reasons : [];
    if (reasons.length || (c.readiness && c.readiness.ready === false)) return false;
    if (c.executable === false || c.execution_ready === false || c.blocked === true) return false;
    return c.executable === true || (isExecSlot && c.executable === undefined);
  }
  // sel = POST /v1/jobs/preview data. -> {market, exec, sandbox, blockers, executable}
  // exec.executable is true ONLY when the server returned an execution_ready candidate that is executable and no
  // blocker stands. The market recommendation is information only, even when it happens to be executable.
  function views(sel, opts) {
    opts = opts || {};
    const s = sel || {};
    const blockers = normBlockers(s.blockers);
    const er = isCand(s.execution_ready) ? s.execution_ready : null;
    const executable = !!er && candExecutable(er, true) && blockers.length === 0;
    const mb = isCand(s.market_best) ? s.market_best : null;
    const mbReasons = mb && mb.readiness && Array.isArray(mb.readiness.reasons) ? normBlockers(mb.readiness.reasons) : [];
    return {
      market: { label: "MARKET RECOMMENDATION", cand: mb, executable: false, same_as_exec: !!(mb && er && candKey(mb) === candKey(er)),
        not_ready: mbReasons, note: "Best observed listing for this workload regardless of whether OpenGrid can run on it. Information only." },
      exec: { label: "EXECUTION-READY ROUTE", cand: executable ? er : null, blocked_cand: executable ? null : er, executable,
        violations: executable && Array.isArray(er.limit_violations) ? er.limit_violations : [],
        note: executable ? "The best candidate OpenGrid can actually run now. Submit creates a quote; an admin approves it before anything launches." : "Nothing is executable right now: see the blockers." },
      sandbox: { label: "SANDBOX", selected: !!opts.sandbox, note: "SIMULATED — no GPU, no spend. Runs the job lifecycle with synthetic numbers, labelled simulated everywhere." },
      blockers, executable,
    };
  }
  // A blocker is a string or {code, reason|message, provider?, providers?}; each gets an actionable next step.
  function normBlockers(list) {
    return (Array.isArray(list) ? list : list ? [list] : []).map(b => {
      const o = typeof b === "string" ? { message: b } : Object.assign({}, b);
      o.message = o.message || o.reason || o.detail || o.code || "blocked";
      o.action = o.action && typeof o.action === "object" ? o.action : blockerAction(o);
      return o;
    });
  }
  // Global blockers first (env / mode: they block every provider), then one group per provider.
  function groupBlockers(list) {
    const bl = normBlockers(list);
    const global = { provider: null, items: bl.filter(b => !b.provider) };
    const by = new Map();
    for (const b of bl.filter(b => b.provider)) { if (!by.has(b.provider)) by.set(b.provider, { provider: b.provider, items: [] }); by.get(b.provider).items.push(b); }
    return (global.items.length ? [global] : []).concat([...by.values()]);
  }
  const pname = p => (p && OG.providerName ? OG.providerName(p) : p);
  // By the server's blocker / readiness codes (workloads/select.py) first, then by the text.
  const CODE_ACTION = {
    env_ceiling: () => ({ label: "ROUTING_LIVE_PROVISIONING is off (Railway variable): set it to true on the deploy and redeploy", href: "/admin/checklist" }),
    mode_preview_only: () => ({ label: "Execution mode PREVIEW_ONLY → Admin → Execution → set SUPERVISED", href: "/admin/execution" }),
    mode_disabled: () => ({ label: "Execution mode DISABLED → Admin → Execution", href: "/admin/execution" }),
    adapter_not_validated: b => ({ label: `${pname(b.provider) || "Provider"} not validated → Admin → Execution → Validate provider`, href: "/admin/execution" }),
    supervised_not_enabled: b => ({ label: `${pname(b.provider) || "Provider"}: supervised launches off → Admin → Execution → Provider execution flags`, href: "/admin/execution" }),
    provider_not_enabled: b => ({ label: `${pname(b.provider) || "Provider"}: launches off → Admin → Execution → Provider execution flags`, href: "/admin/execution" }),
    provider_killed: b => ({ label: `${pname(b.provider) || "Provider"} kill switch on → Admin → Execution`, href: "/admin/execution" }),
    launch_not_permitted: () => ({ label: "Admin → Execution → mode and provider flags", href: "/admin/execution" }),
    flags_unreadable: () => ({ label: "Admin → Execution (control plane unreadable)", href: "/admin/execution" }),
    no_credentials: b => ({ label: `${(/\b([A-Z][A-Z0-9]+_API_KEY)\b/.exec(b.message || b.reason || "") || [])[1] || (b.provider || "PROVIDER").toUpperCase() + "_API_KEY"} on the server (Railway variable), or a BYO key → Onboarding`, href: "/onboarding" }),
    ssh_key_registration_unsupported: () => ({ label: "Use a provider that registers a per-job SSH key, or BYO credentials → Onboarding", href: "/onboarding" }),
    forces_account_ssh_key: () => ({ label: "Capability matrix: this provider may install account SSH keys (Admin → Execution)", href: "/admin/execution" }),
    no_startup_script: () => ({ label: "Capability matrix (Admin → Execution): no startup-script support", href: "/admin/execution" }),
    not_provisionable: () => ({ label: "Market data only: OpenGrid has no adapter for these providers", href: "/methodology/routing" }),
    missing_launch_parameters: () => ({ label: "Capability matrix (Admin → Execution)", href: "/admin/execution" }),
    launch_spec_unchecked: () => ({ label: "Admin → Execution → capability matrix", href: "/admin/execution" }),
    no_compatible_gpu: () => ({ label: "Pick GPU auto, or a GPU that meets the template (see Ineligible GPUs)", href: null }),
    no_eligible_listing: () => ({ label: "Relax the limits / region (see Exclusions)", href: null }),
    // GET /v1/jobs/readiness check codes (jobs/orchestrator.readiness)
    env_live_provisioning: b => CODE_ACTION.env_ceiling(b),
    execution_mode: () => ({ label: "Admin → Execution → set SUPERVISED", href: "/admin/execution" }),
    provider_ready: () => ({ label: "Admin → Execution → validate a provider + enable supervised launches", href: "/admin/execution" }),
    job_job_sync: () => ({ label: "Ops → background jobs", href: "/ops" }),
    job_routing_tracker: () => ({ label: "Ops → background jobs", href: "/ops" }),
    job_reconcile: () => ({ label: "Ops → background jobs", href: "/ops" }),
    ops_alerts: () => ({ label: "OPS_ALERT_WEBHOOK_URL (Railway variable) → Admin → Checklist", href: "/admin/checklist" }),
    callback_url: () => ({ label: "PUBLIC_BASE_URL (Railway variable): the runner must reach OpenGrid", href: "/admin/checklist" }),
    artifact_store: () => ({ label: "Artifact store settings (Railway variables)", href: "/admin/checklist" }),
    budget_active_deployments: () => ({ label: "Account limits → Admin → Execution", href: "/admin/execution" }),
    // submit refusals (jobs/orchestrator._REFUSAL)
    execution_disabled: () => ({ label: "Admin → Execution → execution mode and provider flags", href: "/admin/execution" }),
    provider_unvalidated: () => ({ label: "Admin → Execution → Validate provider", href: "/admin/execution" }),
    credentials_unavailable: () => ({ label: "Provider credentials → Onboarding", href: "/onboarding" }),
    budget_exceeded: () => ({ label: "Raise max_total_cost_usd or lower the runtime ceiling", href: null }),
  };
  function blockerAction(b) {
    if (b.href) return { label: b.action_label || (typeof b.action === "string" ? b.action : "Open"), href: b.href };
    if (b.code && CODE_ACTION[b.code]) return CODE_ACTION[b.code](b);
    const t = ((b.code || "") + " " + (b.message || b.reason || "") + " " + (typeof b.action === "string" ? b.action : "")).toLowerCase();
    const prov = pname(b.provider);
    if (/routing_live_provisioning|live_provisioning|env[_ ]?(flag|ceiling)/.test(t)) return CODE_ACTION.env_ceiling(b);
    if (/preview_only/.test(t)) return CODE_ACTION.mode_preview_only(b);
    if (/execution[_ ]mode|mode_disabled/.test(t)) return { label: "Admin → Execution → Execution mode", href: "/admin/execution" };
    if (/kill/.test(t)) return { label: "Admin → Execution → kill switches", href: "/admin/execution" };
    if (/validat|simulated/.test(t)) return { label: (prov ? prov + " not validated → " : "") + "Admin → Execution → Validate provider", href: "/admin/execution" };
    if (/supervised|flag|enabled/.test(t)) return { label: (prov ? prov + ": " : "") + "Admin → Execution → Provider execution flags", href: "/admin/execution" };
    if (/credential|api[_ ]key/.test(t)) return { label: "Provider credentials → Onboarding", href: "/onboarding" };
    if (/over_budget|budget|max_total|max_price|over_max/.test(t)) return { label: "Raise the job's limits (max $/GPU·h, max total) or pick a cheaper strategy", href: null };
    if (/region/.test(t)) return { label: "Widen the region (any region, or turn strict region off)", href: null };
    if (/spot/.test(t)) return { label: "Turn spot off: on-demand only in this release", href: null };
    if (/alert|health/.test(t)) return { label: "Ops → health and alerts", href: "/ops" };
    return { label: null, href: null };
  }
  // "$1.290/GPU·h × 1 GPU × 60 billed min (billing unit hour)"
  function estimateLine(est) {
    if (!est) return null;
    const parts = [];
    if (est.price_per_gpu_hour != null) parts.push(perGpuH(est.price_per_gpu_hour));
    if (est.gpu_count != null) parts.push(`${est.gpu_count} GPU`);
    if (est.billed_minutes != null) parts.push(`${+(+est.billed_minutes).toFixed(2)} billed min`);
    return parts.join(" × ") + (est.billing_unit ? ` (billing unit ${est.billing_unit}${est.billing_assumed ? ", assumed" : ""})` : "");
  }

  /* ---- verification checks: the 12 the founder asked for (jobs/models.py CHECKS) ---- */
  const CHECKS = [
    ["instance_created", "GPU instance created"], ["runtime_initialized", "Runtime initialized on the instance"], ["torch_installed", "PyTorch installed"],
    ["gpu_detected", "GPU detected"], ["cuda_available", "CUDA available to PyTorch"], ["training_ran", "Training ran"],
    ["params_updated", "Model parameters updated"], ["real_metrics", "Real loss / accuracy reported"], ["weights_saved", "Weights saved (model.pt)"],
    ["artifacts_persisted", "Artifacts persisted in OpenGrid (sha256 verified)"], ["gpu_terminated", "GPU termination requested and accepted"], ["termination_verified", "Termination verified with the provider"],
  ];
  // job.checks: [{key, label, status: pending|pass|fail|simulated, source, expected_source, evidence, at}] (or a
  // {key: {...}|bool} map). A sandbox job never shows "pass".
  function checks(job) {
    const raw = job && (job.checks || job.verification);
    const map = new Map();
    const put = (k, v) => {
      if (!k) return;
      const o = v && typeof v === "object" ? v : { ok: v };
      let status = o.status || (o.ok === true || o.passed === true ? "pass" : o.ok === false || o.passed === false ? "fail" : "pending");
      status = { ok: "pass", passed: "pass", failed: "fail", unknown: "pending", skipped: "pending" }[status] || status;
      if (!["pass", "fail", "pending", "simulated"].includes(status)) status = "pending";
      map.set(k, { key: k, label: o.label || null, status, source: o.source || null, expected_source: o.expected_source || null, evidence: o.evidence ?? o.detail ?? null, at: o.at || null });
    };
    if (Array.isArray(raw)) raw.forEach(c => put(c && (c.key || c.name || c.check), c));
    else if (raw && typeof raw === "object") Object.entries(raw).forEach(([k, v]) => put(k, v));
    const known = CHECKS.map(([k, label]) => Object.assign({ key: k, status: "pending", source: null, evidence: null, at: null }, map.get(k) || {}, { label: (map.get(k) || {}).label || label }));
    const extra = [...map.values()].filter(c => !CHECKS.some(([k]) => k === c.key)).map(c => Object.assign({}, c, { label: c.label || c.key.replace(/_/g, " ") }));
    const out = known.concat(extra);
    if (isSandbox(job)) out.forEach(c => { if (c.status === "pass") c.status = "simulated"; });
    return out;
  }

  /* ---- persisted metrics → progress (never derived from anything else) ---- */
  const LOSS = ["train_loss", "loss", "training_loss"], ACC = ["test_accuracy", "accuracy", "acc", "test_acc", "val_accuracy"];
  // GET /v1/jobs/{id}/metrics. Accepts the long rows of job_metrics ({epoch, step, name, value, at, source}),
  // {series: {name: [{epoch, value, at}]}}, wide rows ({epoch, train_loss, test_accuracy, ...}, possibly under
  // .data), or any of those under {metrics | items | points | epochs}. -> per-epoch points sorted.
  function metricPoints(raw) {
    const num = v => (v == null || v === "" || !isFinite(+v) ? null : +v);
    let list = Array.isArray(raw) ? raw : raw && (raw.metrics || raw.items || raw.points || raw.epochs || raw.rows) || [];
    if (raw && !Array.isArray(raw) && raw.series && typeof raw.series === "object" && !Array.isArray(raw.series)) {
      list = [];
      for (const [name, arr] of Object.entries(raw.series)) for (const r of Array.isArray(arr) ? arr : []) list.push(Object.assign({ name }, r));
    }
    const by = new Map();
    const slot = (key, d) => { let p = by.get(key); if (!p) { p = { epoch: null, epochs: null, step: null, loss: null, acc: null, at: null, elapsed: null, source: null }; by.set(key, p); } return p; };
    for (const m of Array.isArray(list) ? list : []) {
      if (!m || typeof m !== "object") continue;
      const d = Object.assign({}, m.data && typeof m.data === "object" ? m.data : {}, m);
      const epoch = num(d.epoch), step = num(d.step);
      const key = epoch != null ? "e" + epoch : step != null ? "s" + step : "a" + (d.at || by.size);
      const longName = typeof d.name === "string" && d.value !== undefined ? d.name : null;
      let loss = null, acc = null;
      if (longName) { if (LOSS.includes(longName)) loss = num(d.value); else if (ACC.includes(longName)) acc = num(d.value); else if (longName === "epochs") { slot(key).epochs = num(d.value); continue; } else if (/elapsed/.test(longName)) { slot(key).elapsed = num(d.value); continue; } else continue; }
      else { loss = num(LOSS.map(k => d[k]).find(v => v != null)); acc = num(ACC.map(k => d[k]).find(v => v != null)); }
      if (loss == null && acc == null && d.epochs == null) continue;
      const p = slot(key);
      p.epoch = epoch ?? p.epoch; p.step = step ?? p.step;
      if (loss != null) p.loss = loss;
      if (acc != null) p.acc = acc > 1.0001 ? acc / 100 : acc;   // a percentage → fraction
      p.epochs = num(d.epochs ?? d.total_epochs) ?? p.epochs;
      p.elapsed = num(d.elapsed_seconds ?? d.elapsed_s ?? d.elapsed) ?? p.elapsed;
      p.at = d.at || d.ts || d.created_at || p.at;
      p.source = d.source || p.source;
    }
    const pts = [...by.values()].filter(p => p.loss != null || p.acc != null);
    pts.sort((a, b) => (a.epoch ?? a.step ?? 0) - (b.epoch ?? b.step ?? 0) || String(a.at).localeCompare(String(b.at)));
    return pts;
  }
  function progress(points, job) {
    const pts = points || [];
    const last = pts[pts.length - 1] || null;
    const cfg = job && (job.config || (job.request && job.request.config));
    const epochs = (last && last.epochs) || (cfg && cfg.epochs) || null;
    const withLoss = pts.filter(p => p.loss != null), withAcc = pts.filter(p => p.acc != null);
    return {
      epoch: last && last.epoch != null ? last.epoch : null, epochs,
      loss: withLoss.length ? withLoss[withLoss.length - 1].loss : null,
      acc: withAcc.length ? withAcc[withAcc.length - 1].acc : null,
      elapsed: last && last.elapsed != null ? last.elapsed : null,
      pct: last && last.epoch != null && epochs ? Math.max(0, Math.min(1, last.epoch / epochs)) : null,
    };
  }
  // GET /v1/jobs/{id}/logs?after=&limit=: [{id, seq, at, stream, line}] or {logs|items|lines, next_after|next|cursor, has_more}.
  // The cursor is the row id (job_logs.id), else seq.
  function logPage(raw, after) {
    const list = Array.isArray(raw) ? raw : raw && (raw.logs || raw.items || raw.lines) || [];
    const lines = list.map(l => typeof l === "string" ? { id: null, at: null, text: l, stream: null } : {
      id: l.id ?? l.cursor ?? l.seq ?? null, at: l.at || l.ts || null, text: String(l.line ?? l.message ?? l.text ?? (l.data && (l.data.line ?? l.data.message)) ?? ""),
      stream: l.stream || l.level || (l.data && (l.data.stream || l.data.level)) || null,
    });
    let next = raw && !Array.isArray(raw) ? raw.next_after ?? raw.next_cursor ?? raw.next ?? raw.cursor ?? null : null;
    if (next == null) { const ids = lines.map(l => l.id).filter(s => s != null); next = ids.length ? ids[ids.length - 1] : after ?? null; }
    const more = !!(raw && !Array.isArray(raw) && (raw.has_more || raw.more));
    return { lines, next, more };
  }

  /* ---- the job as rows and headers read it (jobs/models.py public()) ---- */
  // Placement comes from the deployment (job.resource) once there is one, else the selection snapshot.
  // Estimates are job.estimates (estimated); the actual cost is a TRANSACTION number from reconciliation only.
  function summary(job, dep) {
    const j = job || {}, r = j.resource || {}, sel = j.selection || {}, req = j.request || {}, rr = req.resources || {}, e = j.estimates || {};
    const rec = dep && dep.reconciliation;
    const tx = rec && rec.opengrid_transaction_cost ? rec.opengrid_transaction_cost.amount_usd : null;
    const prov = rec && rec.provider_reported_cost ? rec.provider_reported_cost.amount_usd : null;
    const started = j.started_at ? +new Date(j.started_at) : null;
    const end = j.finished_at ? +new Date(j.finished_at) : null;
    return {
      state: stateOf(j), outcome: outcomeOf(j), sandbox: isSandbox(j),
      // a sandbox job never ran anywhere: its selection is informational, never shown as the placement
      provider: r.provider || (isSandbox(j) ? null : sel.provider) || null,
      gpu: r.gpu || (isSandbox(j) ? null : sel.gpu) || (rr.gpu && rr.gpu !== "auto" ? rr.gpu : null),
      gpu_count: r.gpu_count || rr.gpu_count || 1, region: r.region || (isSandbox(j) ? null : sel.region) || null, region_group: rr.region || null,
      deployment_id: r.deployment_id || j.deployment_id || null, route_request_id: r.route_request_id || j.route_request_id || null,
      deployment_status: r.deployment_status || (dep && dep.status) || null,
      disposition: r.disposition || null, disposition_note: r.disposition_note || null, possibly_billing: r.possibly_billing === true,
      est_max: e.est_max_total_usd ?? null, est_typical: e.est_typical_total_usd ?? null,
      runtime_ceiling: e.runtime_ceiling_minutes ?? (req.limits && req.limits.max_runtime_minutes) ?? null,
      runtime_seconds: j.runtime_seconds ?? (started && !isNaN(started) ? Math.max(0, ((end && !isNaN(end) ? end : Date.now()) - started) / 1000) : null),
      actual: isSandbox(j) ? null : (j.actual_cost_usd ?? tx ?? null), actual_basis: j.actual_cost_usd != null ? "job" : tx != null ? "transaction" : prov != null ? "provider-reported" : null,
      provider_reported: prov,
    };
  }

  /* ---- plain-language progress: the 14-state machine as 8 stages (or 4 for the sandbox) ---- */
  // Derived ONLY from the real job state (jobs/states.py) and its event history; nothing is assumed.
  const STAGES = [["preparing", "Preparing"], ["selecting", "Selecting GPU"], ["approval", "Awaiting approval"], ["starting", "Starting GPU"],
    ["running", "Running"], ["saving", "Saving results"], ["shutting_down", "Shutting down"], ["complete", "Complete"]];
  // The sandbox never selects, waits for approval, starts or shuts down a GPU (jobs/states.py SANDBOX path).
  const SANDBOX_STAGES = ["preparing", "running", "saving", "complete"];
  const STAGE_OF = {
    created: "preparing", validating: "preparing", validation_failed: "preparing",
    quoted: "selecting", no_eligible_provider: "selecting",
    pending_approval: "approval", quote_expired: "approval", approval_rejected: "approval",
    approved: "starting", provisioning: "starting", provider_timeout: "starting", provisioning_uncertain: "starting", initializing: "starting", bootstrap_failed: "starting",
    training: "running", training_failed: "running", out_of_memory: "running", training_timed_out: "running",
    saving_artifacts: "saving", artifact_upload_failed: "saving",
    training_completed: "shutting_down", terminating: "shutting_down", termination_confirmed: "shutting_down", termination_failed: "shutting_down",
    orphan_suspected: "shutting_down", reconciliation_pending: "shutting_down", cancelling: "shutting_down",
    reconciled: "complete",
  };
  // sandbox: setup is simulated (no GPU to start); the simulation ends at training_completed
  const SANDBOX_STAGE_OF = Object.assign({}, STAGE_OF, { initializing: "preparing", bootstrap_failed: "preparing", training_completed: "complete" });
  // states that end (or fail) the stage they sit in
  const STAGE_FAILS = ["validation_failed", "no_eligible_provider", "approval_rejected", "bootstrap_failed", "training_failed", "out_of_memory", "training_timed_out", "artifact_upload_failed"];
  // states whose stage depends on where the job was when they happened
  const STAGE_FROM_HISTORY = ["credentials_unavailable", "cancel_requested"];
  // job -> {mode, steps: [{key, label, mark: done|current|failed|todo|skipped, tone, state}], current, failed_stage, ended, outcome, attention}
  function stages(job) {
    const sb = isSandbox(job), s = stateOf(job), o = outcomeOf(job);
    const keys = sb ? SANDBOX_STAGES : STAGES.map(x => x[0]);
    const map = sb ? SANDBOX_STAGE_OF : STAGE_OF;
    const hist = ((job && job.events) || []).map(e => e && (e.to || e.to_status || e.state)).filter(Boolean);
    const known = hist.length > 0;
    const all = hist.concat(s ? [s] : []);
    const stageOf = st => map[st] || null;
    const furthest = list => { let best = -1; for (const st of list) { const i = keys.indexOf(stageOf(st)); if (i > best) best = i; } return best; };
    let cur = stageOf(s);
    if (!cur && STAGE_FROM_HISTORY.includes(s)) {
      // cancel after a launch is the shut-down path; before a launch (or credentials lost mid-run) stay where the job was
      const launched = all.some(st => MAY_BILL.includes(st) && !STAGE_FROM_HISTORY.includes(st));
      const i = furthest(hist);
      cur = s === "cancel_requested" && launched ? "shutting_down" : i >= 0 ? keys[i] : keys[0];
    }
    if (!cur || !keys.includes(cur)) cur = keys[Math.max(0, furthest(all))];
    const idx = keys.indexOf(cur);
    const ended = isTerminal(job);
    const failSt = all.find(st => STAGE_FAILS.includes(st)) || null;
    let failedStage = failSt ? stageOf(failSt) : null;
    if (failedStage && !keys.includes(failedStage)) failedStage = null;
    const fi = failedStage ? keys.indexOf(failedStage) : -1;
    const visited = new Set(all.map(stageOf).filter(Boolean));
    if (all.includes("approved")) visited.add("approval");   // approved sits in Starting GPU but proves approval happened
    const warn = UNCERTAIN.includes(s) || s === "termination_failed" || s === "quote_expired";
    const steps = keys.map((k, i) => {
      const label = STAGES.find(x => x[0] === k)[1];
      let mark;
      if (i < idx) mark = i === fi ? "failed" : known && !visited.has(k) ? "skipped" : "done";
      else if (i === idx) {
        if (i === fi && (STAGE_FAILS.includes(s) || ended)) mark = "failed";
        else if (k === "complete" && ended) mark = "done";
        else mark = ended ? (STAGE_FAILS.includes(s) ? "failed" : "done") : "current";
      } else mark = !ended ? "todo" : k === "complete" ? "done" : "skipped";   // an ended job always shows how it ended
      let tone = "";
      if (mark === "failed") tone = o === "cancelled" ? "warn" : "bad";
      else if (mark === "current") tone = warn ? "warn" : "busy";
      else if (k === "complete" && mark === "done") tone = o === "failed" ? "bad" : o === "cancelled" ? "warn" : "good";
      return { key: k, label: k === "complete" ? completeLabel(ended, o, sb) : label, mark, tone, state: i === idx ? s : null };
    });
    return { mode: sb ? "sandbox" : "real", steps, current: cur, failed_stage: failedStage, ended, outcome: o,
      attention: warn || s === "pending_approval" || (!sb && summary(job).possibly_billing && ended === false && FAILED.includes(s)) };
  }
  function completeLabel(ended, o, sb) {
    if (!ended) return "Complete";
    if (o === "failed") return "Ended — failed";
    if (o === "cancelled") return "Ended — cancelled";
    if (sb || o === "simulated") return "Complete (simulated)";
    return "Complete";
  }

  // The questions a beginner asks, answered from the real job / deployment / artifacts only.
  // -> [{key, q, a, tone, detail}]   tone: good | warn | bad | "" ; detail is a short supporting line
  function plainStatus(job, dep, artifacts, opts) {
    opts = opts || {};
    const j = job || {}, s = stateOf(j), o = outcomeOf(j), sb = isSandbox(j), sm = summary(j, dep), ended = isTerminal(j);
    const out = [];
    const add = (key, q, a, tone, detail) => out.push({ key, q, a, tone: tone || "", detail: detail || null });
    const cap = sm.est_max != null ? usd(sm.est_max) : (j.request && j.request.limits && j.request.limits.max_total_cost_usd != null ? usd(j.request.limits.max_total_cost_usd) : null);
    const uncertain = UNCERTAIN.includes(s) || s === "termination_failed" || sm.possibly_billing;
    // 1. money
    if (sb) add("money", "Can real money be spent?", "No", "good", "This is a free simulation: no provider is called and nothing is charged.");
    else if (ENDED_BEFORE_LAUNCH.includes(s)) add("money", "Can real money be spent?", "No", "good", "The job ended before anything was launched.");
    else if (s === "reconciled") add("money", "Can real money be spent?", "Not any more", "good", "The GPU was terminated and the provider confirmed it.");
    else if (uncertain) add("money", "Can real money be spent?", "Yes — possibly right now", "bad", "OpenGrid cannot yet confirm the GPU is gone; treat it as billing.");
    else if (MAY_BILL.includes(s)) add("money", "Can real money be spent?", "Yes", "warn", "A rented GPU exists for this job" + (cap ? ` (cost ceiling ${cap})` : "") + ".");
    else add("money", "Can real money be spent?", "Not yet", "", "Nothing is charged until an OpenGrid admin approves the quote" + (cap ? ` (max ${cap})` : "") + ".");
    // 2. simulation
    add("sim", "Is this a simulation?", sb ? "Yes" : "No", sb ? "warn" : "", sb ? "Every log line, metric and file is synthetic and labelled SIMULATED." : "A real provider run on a rented GPU.");
    // 3. running
    if (ended) add("running", "Is it running?", "No — finished", "");
    else if (RUNNING.includes(s)) add("running", "Is it running?", "Yes", "good", s === "training" ? (sb ? "Simulated training in progress." : "Training on the GPU.") : String(s).replace(/_/g, " ") + ".");
    else if (s === "pending_approval") add("running", "Is it running?", "Not yet — waiting for approval", "warn");
    else if (PRE_LAUNCH.includes(s)) add("running", "Is it running?", "Not yet", "", String(s).replace(/_/g, " ") + ".");
    else add("running", "Is it running?", "No — wrapping up", "", String(s).replace(/_/g, " ") + ".");
    // 4. GPU
    if (sb) add("gpu", "Is a GPU still active?", "No — no GPU was ever used", "good");
    else if (uncertain) add("gpu", "Is a GPU still active?", "Possibly", "bad", "Termination is not confirmed by the provider yet.");
    else if (sm.disposition === "terminated_confirmed" || s === "reconciled" || s === "termination_confirmed" || s === "reconciliation_pending") add("gpu", "Is a GPU still active?", "No — terminated (provider confirmed)", "good");
    else if (MAY_BILL.includes(s)) add("gpu", "Is a GPU still active?", "Yes", "warn", "OpenGrid terminates it when the run ends, fails or hits its runtime ceiling.");
    else add("gpu", "Is a GPU still active?", "No", "good", "Nothing has been launched.");
    // 5. action
    if (uncertain) add("action", "Is action needed?", "Yes — check the deployment", "bad", "OpenGrid keeps retrying termination and the operator is alerted.");
    else if (s === "pending_approval") add("action", "Is action needed?", opts.admin ? "Yes — approve or reject the quote" : "Not from you — waiting for OpenGrid approval", "warn");
    else if (s === "quote_expired") add("action", "Is action needed?", "Yes — the quote expired and must be re-quoted", "warn");
    else add("action", "Is action needed?", "No", "");
    // 6. success
    const reason = j.failure && (j.failure.reason || j.failure.code);
    if (o === "succeeded") add("success", "Did it succeed?", "Yes", "good");
    else if (o === "failed") add("success", "Did it succeed?", "No", "bad", reason ? String(reason).slice(0, 200) : null);
    else if (o === "cancelled") add("success", "Did it succeed?", "Cancelled", "warn");
    else if (sb && ended && STAGE_FAILS.includes(s)) add("success", "Did it succeed?", "No — the simulation failed", "bad", reason ? String(reason).slice(0, 200) : null);
    else if (sb && ended) add("success", "Did it succeed?", "The simulation completed", "good", "Synthetic numbers: this is not a real training result.");
    else add("success", "Did it succeed?", "Not known yet", "");
    // 7. outputs
    const arts = Array.isArray(artifacts) ? artifacts : [];
    add("outputs", "What outputs exist?", arts.length ? arts.map(a => a && a.name).filter(Boolean).join(", ") : "None yet", arts.length ? "good" : "",
      arts.length ? (sb ? "Simulated files." : "sha256-verified on receipt.") : ended ? "No files were stored." : "Files are saved when training ends.");
    // 8. cost
    if (sb) add("cost", "What did it cost?", "$0.00 — simulated", "good");
    else if (sm.actual != null) add("cost", "What did it cost?", usd(sm.actual), "", sm.actual_basis === "transaction" ? "Reconciled from provider usage." : null);
    else if (ENDED_BEFORE_LAUNCH.includes(s)) add("cost", "What did it cost?", "$0.00 — nothing launched", "good");
    else add("cost", "What did it cost?", "Not known yet", "", "Known after termination is confirmed and reconciled" + (cap ? ` · ceiling ${cap}` : "") + ".");
    return out;
  }
  const RUNNING = ["provisioning", "initializing", "training", "saving_artifacts"];

  OG.jobs = { FLOW, SANDBOX_FLOW, FAILED, UNCERTAIN, CANCEL, PRE_LAUNCH, MAY_BILL, ENDED_BEFORE_LAUNCH, FINISHED, SANDBOX_FINISHED, REGIONS, STRATEGIES,
    DEFAULT_CONFIG, LIMIT_BOUNDS, CHECKS, CODE_ACTION, FOUNDER, STAGES, SANDBOX_STAGES, STAGE_OF,
    tone, strip, stateOf, isTerminal, outcomeOf, OUTCOME_TONE, isSandbox, usd, perGpuH, duration, bytes, configSpec, limitBounds, templateId, preset,
    validate, views, candExecutable, normBlockers, groupBlockers, blockerAction, estimateLine, checks, metricPoints, progress, logPage, candKey, summary,
    stages, plainStatus };

  /* =============================== commands =============================== */
  OG.command(/^j\s+new$/i, () => OG.go("/jobs/new"), "j new            new training job (PyTorch MNIST)");
  OG.command(/^j$/i, () => OG.go("/jobs"), "j                jobs");

  if (typeof OG.page !== "function" || typeof document === "undefined") return;   // node tests: pure part only

  /* =============================== DOM helpers (shared with job.js) =============================== */
  const J = OG.jobs;
  J.stateBadge = s => h("span", { class: "badge og-st st-" + (tone(s) || "plain"), title: UNCERTAIN.includes(s) ? "uncertain: an instance may exist and bill" : FAILED.includes(s) ? "failure state" : null }, String(s || "–").replace(/_/g, " "));
  J.outcomeBadge = o => o ? OG.badge(o, OUTCOME_TONE[o] || "", o === "simulated" ? "SIMULATED — no GPU was used; numbers are synthetic" : null) : h("span", { class: "dim" }, "–");
  J.sandboxBadge = sb => sb ? OG.badge("SIMULATED", "warn", "Sandbox: no provider, no GPU, no spend. Numbers are synthetic.") : OG.badge("real", "", "A real provider run (after approval)");
  J.stripEl = (status, events, sandbox) => h("ol", { class: "og-strip jb-strip", "aria-label": "job lifecycle" },
    strip(status, (events || []).map(e => e.to || e.to_status || e.state), sandbox).map(st => h("li", { class: "m-" + st.mark + (st.tone ? " t-" + st.tone : "") + (st.off ? " off" : ""), title: st.mark === "skipped" ? st.state + ": not passed through" : st.state, "aria-current": st.mark === "current" ? "step" : null }, st.state.replace(/_/g, " "))));
  J.notYet = (what, e) => e && (e.status === 404 || e.status === 405) ? OG.insufficient(what + " is not available on this server yet.", "Not available yet") : OG.error(e);

  // Candidate score breakdown: the same factors and colours as /route (routing/scoring.py).
  const FACTORS = [
    { key: "price", label: "Price", color: "#4d94ff" }, { key: "availability_now", label: "Availability now", color: "#2fbf71" },
    { key: "region_match", label: "Region match", color: "#b39cff" }, { key: "freshness", label: "Data freshness", color: "#8a96a3" },
    { key: "availability_persistence", label: "Availability history", color: "#f5a524" }, { key: "price_stability", label: "Price stability", color: "#e0729e" },
    { key: "integration_level", label: "OpenGrid integration", color: "#4fc3d0" },
  ];
  J.scoreBar = (c, width) => h("span", { class: "rt-bar", style: width ? `width:${width}px` : null, title: "score " + (c && c.score != null ? (+c.score).toFixed(3) : "–") + " of 1.000" },
    FACTORS.map(f => {
      const x = c && c.factors && c.factors[f.key];
      if (!x || !x.contribution) return null;
      return h("i", { style: { width: (x.contribution * 100).toFixed(2) + "%", background: f.color }, class: x.imputed ? "imp" : null,
        title: `${f.label}: +${(+x.contribution).toFixed(3)} (weight ${((x.weight || 0) * 100).toFixed(0)}% × value ${x.imputed ? "0.5 imputed — no data" : x.value})` });
    }));
  J.whyList = c => {
    const fs = (c && c.factors) || {};
    const items = FACTORS.map(f => {
      const x = fs[f.key];
      if (!x || !x.weight) return null;
      return h("li", { class: x.imputed ? "imp" : null }, h("i", { class: "sw", style: { background: f.color } }), h("b", {}, f.label), " ", String(x.note || "").replace(/ \(scored neutral 0\.5\)$/, ""),
        x.imputed ? h("span", { class: "badge warn" }, "imputed 0.5") : null, h("span", { class: "rt-contrib mono" }, "+" + (+x.contribution || 0).toFixed(3)));
    }).filter(Boolean);
    if (!items.length) return c && c.explanation ? h("p", { class: "note" }, c.explanation) : h("p", { class: "note" }, "No score breakdown in the response.");
    return h("ul", { class: "rt-why" }, items, (c.not_used || ["reliability", "performance"]).length ? h("li", { class: "nodata" }, h("b", {}, "Reliability, performance"), " — no data, not used (no execution history yet)") : null);
  };
  J.regionText = c => [c && c.region, c && c.country].filter(Boolean).join(", ") || "location not given";

  /* =============================== /jobs =============================== */
  OG.page("/jobs", {
    title: "Jobs",
    nav: "jobs",
    mount(el, params, query, ctx) {
      const root = h("div", { class: "pg-jobs pg-dep" });   // pg-dep: reuse the deployments table styles
      el.append(root);
      const holder = h("div", {}, OG.loading("Loading jobs…")), stats = h("div");
      root.append(
        OG.head("Jobs", "Training workloads OpenGrid routes, runs and verifies · job state and deployment state shown separately",
          h("button", { class: "btn", type: "button", onclick: () => load() }, "Reload"),
          h("a", { class: "btn pri", href: "/jobs/new" }, "New job →")),
        OG.views && OG.views.startHere ? OG.views.startHere(null) : null,
        h("a", { class: "jb-cta", href: "/jobs/new?preset=mnist" },
          h("span", { class: "jb-cta-k" }, "TEMPLATE · pytorch-mnist"),
          h("b", {}, "Train Your First AI Model — PyTorch MNIST"),
          h("span", { class: "jb-cta-s" }, "A small CNN on MNIST, 1 GPU, ≤ 30 min, ≤ $2 total. Preview the route for free; nothing launches before OpenGrid approves the quote. No card needed: try the SIMULATED sandbox first."),
          h("span", { class: "btn pri jb-cta-b" }, "Start →")),
        stats, holder);
      const tbl = OG.table({
        columns: [
          { key: "name", label: "Name", fmt: (v, r) => h("span", { class: "jb-nm" }, v || h("span", { class: "dim" }, "unnamed")) },
          { key: "job_id", label: "Job", cls: "mono dim", fmt: v => String(v || "") },
          { key: "template", label: "Template", cls: "mono dim" },
          { key: "status", label: "State", value: r => stateOf(r), fmt: v => J.stateBadge(v) },
          { key: "outcome", label: "Outcome", value: r => outcomeOf(r), fmt: v => J.outcomeBadge(v) },
          { key: "provider", label: "Provider", value: r => summary(r).provider, fmt: (v, r) => v ? OG.providerLink(v) : h("span", { class: "dim" }, isSandbox(r) ? "sandbox" : "not placed") },
          { key: "gpu", label: "GPU", value: r => summary(r).gpu, fmt: v => v ? OG.gpuLink(v) : h("span", { class: "dim" }, "auto") },
          { key: "created_at", label: "Created", num: true, fmt: v => fmt.dateTime(v) },
          { key: "runtime", label: "Runtime", num: true, value: r => summary(r).runtime_seconds, fmt: v => duration(v) },
          { key: "est", label: "Est. max", num: true, value: r => summary(r).est_max, fmt: v => v != null ? usd(v) : OG.na("no estimate") },
          { key: "actual", label: "Actual", num: true, value: r => summary(r).actual, fmt: (v, r) => v != null ? usd(v) : OG.na(isSandbox(r) ? "simulated: no spend" : "reconciled after termination (on the job page)") },
          { key: "sandbox", label: "Run", value: r => isSandbox(r) ? 1 : 0, fmt: (v, r) => J.sandboxBadge(isSandbox(r)) },
        ],
        rows: [], sort: { key: "created_at", dir: "desc" }, rowKey: r => r.job_id, rowHref: r => "/jobs/" + encodeURIComponent(r.job_id),
        rowClass: r => (UNCERTAIN.includes(stateOf(r)) || summary(r).possibly_billing && FAILED.includes(stateOf(r)) ? "dep-hot" : ""), csv: "opengrid-jobs.csv", compact: true, empty: "No jobs yet.",
      });
      async function load() {
        let rows;
        try { const d = await ctx.api("/v1/jobs", { nocache: true }); rows = Array.isArray(d) ? d : (d && (d.jobs || d.items)) || []; }
        catch (e) { holder.replaceChildren(J.notYet("GET /v1/jobs", e)); return; }
        stats.replaceChildren(OG.stats([
          { label: "Jobs", value: fmt.num(rows.length) },
          { label: "Active", value: fmt.num(rows.filter(r => !isTerminal(r)).length) },
          { label: "Succeeded", value: fmt.num(rows.filter(r => outcomeOf(r) === "succeeded").length), sub: "real runs" },
          { label: "Simulated", value: fmt.num(rows.filter(r => isSandbox(r)).length), sub: "sandbox: no spend" },
          { label: "Pending approval", value: fmt.num(rows.filter(r => stateOf(r) === "pending_approval").length) },
          { label: "Possibly billing", value: fmt.num(rows.filter(r => summary(r).possibly_billing).length), sub: "a GPU may exist" },
          { label: "Est. max (real)", value: rows.some(r => !isSandbox(r) && summary(r).est_max != null) ? usd(rows.reduce((s, r) => s + (isSandbox(r) ? 0 : summary(r).est_max || 0), 0)) : null, reason: "no real job yet", kind: "estimated" },
        ]));
        if (!rows.length) { holder.replaceChildren(h("div", { class: "dep-empty" }, h("b", {}, "No jobs yet."), h("p", {}, "Start with the MNIST template above: preview is free, the sandbox is simulated, a real run waits for OpenGrid approval."))); return; }
        tbl.update(rows);
        if (holder.firstChild !== tbl) holder.replaceChildren(tbl, h("p", { class: "note" }, "Est. max = the job's cost ceiling from the quote; actual = the reconciled provider cost (transaction). SIMULATED jobs never spend. ",
          h("a", { class: "lnk", href: "/methodology/execution-safety" }, "Execution safety →")));
      }
      load();
      ctx.every(15000, load);
    },
  });

  /* =============================== /jobs/new =============================== */
  OG.page("/jobs/new", {
    title: "New training job",
    nav: "jobs",
    async mount(el, params, query, ctx) {
      const root = h("div", { class: "pg-jobs pg-jobnew pg-route" });   // pg-route: reuse the order-ticket field styles
      el.append(root);
      // Journey B: /jobs/new?preset=mnist&sandbox=1 is the beginner's guided path (free simulation). The full form
      // stays on the page, folded under "Advanced details" (?advanced=1 opens the classic view directly).
      const beginner = query.sandbox === "1" && query.preset === "mnist" && query.advanced !== "1";
      const fullHead = OG.head("New training job", "Allowlisted templates only · preview is free and never calls a provider · a real run waits for OpenGrid approval",
        h("a", { class: "btn", href: "/jobs" }, "← Jobs"), h("a", { class: "btn", href: "/methodology/execution-safety" }, "Execution safety"));
      const formBox = h("form", { class: "box jb-form", onsubmit: e => { e.preventDefault(); preview(); } });
      const out = h("div", { class: "jb-out" });
      const readiness = h("div", { class: "jb-ready" });
      const fullGrid = h("div", { class: "jb-grid" }, formBox, h("div", { class: "jb-main" }, readiness, out));
      const easyBox = h("div", { class: "jb-easy-w" }, OG.loading("Loading the example…"));
      if (beginner) {
        root.append(OG.head("Train an example model", "Free simulation · PyTorch MNIST · no GPU, no spend, no card",
            h("a", { class: "btn", href: "/jobs" }, "← Jobs"), h("a", { class: "btn", href: "/jobs/new?preset=mnist&sandbox=1&advanced=1" }, "Full job form")),
          OG.views && OG.views.startHere ? OG.views.startHere("demo") : null,
          easyBox,
          h("details", { class: "jb-advd" }, h("summary", {}, h("b", {}, "Advanced details"), h("span", { class: "dim" }, " — the full job form: route preview, execution readiness, limits, SSH access, real-GPU submission")),
            fullHead, fullGrid));
      } else root.append(fullHead, fullGrid);

      const me = await OG.me();
      if (!ctx.alive()) return;
      const admin = OG.isAdmin(me);
      let templates = null, tplErr = null;
      try { const d = await ctx.api("/v1/jobs/templates"); templates = Array.isArray(d) ? d : (d && (d.templates || d.items)) || []; }
      catch (e) { tplErr = e; templates = []; }
      if (!ctx.alive()) return;
      // until the server lists templates, the only allowlisted one is known by name (bounds re-checked server-side)
      const tpls = templates.length ? templates : [{ id: "pytorch-mnist", name: "Train Your First AI Model — PyTorch MNIST", job_type: "training", _fallback: true }];
      let tpl = tpls.find(t => templateId(t) === (query.template || "pytorch-mnist")) || tpls[0];
      let spec = configSpec(tpl), lb = limitBounds(tpl);
      const f = preset(tpl);
      if (query.sandbox === "1") f.sandbox = true;
      let last = null, lastBody = null;
      const I = { submit: null };

      /* ----- fields ----- */
      const errs = {};
      const errEl = key => (errs[key] = errs[key] || h("span", { class: "jb-err", "aria-live": "polite" }));
      const field = (label, input, hint, key) => h("label", { class: "rt-f" }, h("span", { class: "rt-l" }, label), input, key ? errEl(key) : null, hint ? h("span", { class: "rt-h" }, hint) : null);
      const bind = (key, input, conv) => { input.addEventListener("input", () => { f[key] = conv ? conv(input.value) : input.value; changed(); }); return input; };
      const numInput = (key, b, attrs) => bind(key, h("input", Object.assign({ class: "field num", type: "number", value: f[key] ?? "", min: b && b.min, max: b && b.max, step: (b && b.step) || "any" }, attrs || {})));
      const nameIn = bind("name", h("input", { class: "field jb-wide", type: "text", value: f.name, maxlength: 64, spellcheck: "false", autocomplete: "off", "aria-label": "Job name" }));
      const tplSel = h("select", { class: "field jb-wide", "aria-label": "Template", onchange: e => { tpl = tpls.find(t => templateId(t) === e.target.value) || tpl; spec = configSpec(tpl); lb = limitBounds(tpl); f.template = templateId(tpl); drawConfig(); changed(); } },
        tpls.map(t => h("option", { value: templateId(t), selected: t === tpl ? true : null }, (t.title || t.name || templateId(t)) + (t.job_type && t.job_type !== "training" ? " (" + t.job_type + ")" : ""))));
      const tplInfo = h("div", { class: "rt-h jb-tplinfo" });
      const cfgBox = h("div", { class: "rt-row2 jb-cfg" });
      function drawConfig() {
        const r = tpl.requirements || {};
        tplInfo.replaceChildren(
          h("span", { title: tpl.description || null }, [tpl.framework ? (tpl.framework.name || tpl.framework) + (tpl.framework.torch ? " " + tpl.framework.torch : "") + (tpl.framework.cuda ? " · CUDA " + tpl.framework.cuda : "") : "PyTorch",
            tpl.dataset ? tpl.dataset.name || tpl.dataset : "MNIST", tpl.model ? (tpl.model.name || tpl.model) + (tpl.model.architecture ? " (" + tpl.model.architecture + ")" : "") : "small CNN"].join(" · ")),
          r.min_vram_gb || r.min_compute_capability ? h("span", { class: "dimmer", title: [r.vram_note, r.max_compute_capability_reason, r.on_demand_note].filter(Boolean).join("\n") || null },
            ` · needs ${r.vendor || "NVIDIA"} CUDA${r.min_vram_gb ? ", ≥ " + r.min_vram_gb + " GB VRAM" : ""}${r.min_compute_capability ? ", compute " + r.min_compute_capability + (r.max_compute_capability ? "–" + r.max_compute_capability : "+") : ""}${r.on_demand_only ? ", on-demand only" : ""}`) : null,
          tpl.expected_runtime && tpl.expected_runtime.default_band_minutes ? h("span", { class: "dimmer", title: tpl.expected_runtime.note || null }, ` · expected ${tpl.expected_runtime.default_band_minutes.join("–")} min (estimate)`) : null,
          tpl._fallback ? h("span", { class: "warn-t" }, tplErr && (tplErr.status === 404 || tplErr.status === 405) ? " · GET /v1/jobs/templates not available yet: built-in defaults; the server re-validates" : " · templates unavailable: built-in defaults") : null);
        cfgBox.replaceChildren(...Object.entries(spec).map(([k, b]) => {
          if (f.config[k] == null && b.default != null) f.config[k] = b.default;
          const i = h("input", { class: "field num", type: "number", value: f.config[k] ?? "", min: b.min, max: b.max, step: b.step || "any", "aria-label": b.label || k });
          i.addEventListener("input", () => { f.config[k] = i.value === "" ? null : +i.value; changed(); });
          return field(b.label || k, i, h("span", { title: b.hint || null }, `${b.min ?? "–"} – ${b.max ?? "–"} · default ${b.default ?? "–"}`), "config." + k);
        }));
      }
      const presetBtn = h("button", { type: "button", class: "btn jb-preset", title: "Fill the founder's defaults: opengrid-first-training-test, 1 GPU auto, US, BALANCED, on-demand, ≤ $3/GPU·h, ≤ $2 total, 30 min" },
        "MNIST preset", h("span", { class: "dimmer" }, " · founder's defaults"));
      presetBtn.addEventListener("click", () => {
        const sb = f.sandbox;
        Object.assign(f, preset(tpls.find(t => templateId(t) === "pytorch-mnist") || tpl), { sandbox: sb });
        tpl = tpls.find(t => templateId(t) === f.template) || tpl; spec = configSpec(tpl); lb = limitBounds(tpl);
        paint(); changed();
      });
      const typeSel = h("select", { class: "field", "aria-label": "Job type", onchange: e => { f.job_type = e.target.value; changed(); } },
        [["training", "Training"], ["finetuning", "Fine-tuning (not yet)"], ["inference", "Inference (not yet)"], ["batch_compute", "Batch compute (not yet)"], ["custom_container", "Custom container (not yet)"]]
          .map(([v, l]) => h("option", { value: v, disabled: v !== "training" ? true : null }, l)));
      const gpuIn = bind("gpu", h("input", { class: "field", type: "text", value: f.gpu, spellcheck: "false", autocomplete: "off", "aria-label": "GPU", placeholder: "auto" }), v => v.trim() || "auto");
      const countIn = numInput("gpu_count", lb.gpu_count, { "aria-label": "GPU count" });
      const vramIn = numInput("min_vram_gb", { min: 1, max: 1024, step: 1 }, { placeholder: "template", "aria-label": "Min VRAM GB" });
      const regionSel = h("select", { class: "field", "aria-label": "Region group", onchange: e => { f.region = e.target.value || null; strictIn.disabled = !f.region; if (!f.region) { f.strict_region = false; strictIn.checked = false; } drawStrictHint(); changed(); } },
        h("option", { value: "" }, "Any region"), REGIONS.map(g => h("option", { value: g, selected: f.region === g ? true : null }, g)));
      const strictIn = h("input", { type: "checkbox", checked: f.strict_region ? true : null, disabled: f.region ? null : true, onchange: e => { f.strict_region = e.target.checked; drawStrictHint(); changed(); } });
      const strictHint = h("span", { class: "rt-h" });
      function drawStrictHint() {
        strictHint.textContent = !f.region ? "Any region: every location can rank."
          : f.strict_region ? `Strict: only listings whose location is CONFIRMED in ${f.region}; listings without a stated location are excluded (with the reason).`
            : `Non-strict: ${f.region} listings rank first; listings whose location is not stated can still rank (scored lower on region match).`;
      }
      const stratSeg = OG.seg(STRATEGIES.map(s => [s[0], s[1], s[2]]), f.strategy, v => { f.strategy = v; stratHint.textContent = STRATEGIES.find(s => s[0] === v)[2]; changed(); });
      stratSeg.classList.add("jb-strat");
      const stratHint = h("span", { class: "rt-h" }, STRATEGIES.find(s => s[0] === f.strategy)[2]);
      const spotIn = h("input", { type: "checkbox", checked: f.allow_spot ? true : null, onchange: e => { f.allow_spot = e.target.checked; changed(); } });
      const maxPriceIn = numInput("max_price_per_gpu_hour_usd", lb.max_price_per_gpu_hour_usd, { step: 0.01, "aria-label": "Max price per GPU-hour" });
      const maxTotalIn = numInput("max_total_cost_usd", lb.max_total_cost_usd, { step: 0.01, "aria-label": "Max total cost" });
      const maxRunIn = numInput("max_runtime_minutes", lb.max_runtime_minutes, { step: 1, "aria-label": "Max runtime minutes" });
      const sshIn = bind("ssh_public_key", h("textarea", { class: "field jb-ssh", rows: 2, spellcheck: "false", autocomplete: "off", placeholder: "ssh-ed25519 AAAA… you@laptop   (optional — PUBLIC key only)", "aria-label": "SSH public key" }));
      const sbIn = h("input", { type: "checkbox", checked: f.sandbox ? true : null, onchange: e => { f.sandbox = e.target.checked; drawSandbox(); changed(); } });
      const sbBox = h("label", { class: "jb-sb" }, sbIn, h("span", {}, h("b", {}, "Sandbox"), h("span", { class: "jb-sim" }, "SIMULATED — no GPU, no spend"), h("span", { class: "rt-h" }, "No provider is called. The lifecycle, logs and metrics are synthetic and labelled simulated everywhere.")));
      function drawSandbox() { sbBox.classList.toggle("on", !!f.sandbox); submitBtn.textContent = f.sandbox ? "Submit sandbox job…" : "Submit for approval…"; }

      const previewBtn = h("button", { class: "btn pri", type: "submit", title: "POST /v1/jobs/preview: validation + workload-aware selection; never calls a provider" }, "Preview route", h("kbd", {}, "↵"));
      const submitBtn = h("button", { class: "btn jb-submit", type: "button", onclick: () => submit() }, "Submit for approval…");
      const dirty = h("span", { class: "rt-dirty", hidden: true }, "form changed — preview again");
      const formErr = h("div", { class: "jb-ferr" });

      function paint() {
        nameIn.value = f.name; typeSel.value = f.job_type; gpuIn.value = f.gpu; countIn.value = f.gpu_count; vramIn.value = f.min_vram_gb ?? "";
        regionSel.value = f.region || ""; strictIn.checked = !!f.strict_region; strictIn.disabled = !f.region; stratSeg.set(f.strategy);
        stratHint.textContent = STRATEGIES.find(s => s[0] === f.strategy)[2]; spotIn.checked = !!f.allow_spot;
        maxPriceIn.value = f.max_price_per_gpu_hour_usd; maxTotalIn.value = f.max_total_cost_usd; maxRunIn.value = f.max_runtime_minutes;
        sshIn.value = f.ssh_public_key || ""; sbIn.checked = !!f.sandbox; tplSel.value = f.template;
        drawConfig(); drawStrictHint(); drawSandbox();
      }
      function showErrors(v) {
        for (const [k, e] of Object.entries(errs)) e.textContent = v.errors[k] || "";
        const n = Object.keys(v.errors).length;
        formErr.replaceChildren(n ? h("div", { class: "jb-ferr-b" }, `${n} field${n === 1 ? "" : "s"} to fix: `, Object.values(v.errors).join(" · ")) : null);
      }
      function changed() {
        const v = validate(f, spec, lb);
        showErrors(v);
        if (last) dirty.hidden = false;
      }

      formBox.append(
        h("div", { class: "rt-tt" }, h("span", { class: "eyebrow" }, "Job request"), h("span", { class: "spacer" }), presetBtn),
        field("Name", nameIn, null, "name"),
        h("div", { class: "rt-row2" }, field("Job type", typeSel, "training only in this release", "job_type"), field("Template", tplSel, null, "template")),
        tplInfo,
        h("div", { class: "jb-sub" }, "Training config"), cfgBox,
        h("div", { class: "jb-sub" }, "Resources"),
        h("div", { class: "rt-row2" }, field("GPU", gpuIn, "auto = every GPU meeting the template's requirements", "gpu"), field("GPU count", countIn, "the template runs on 1 GPU", "gpu_count")),
        h("div", { class: "rt-row2" }, field("Region", h("span", { class: "rt-inl" }, regionSel, h("label", { class: "rt-chk", title: "strict_region" }, strictIn, "Strict")), null, "region"), field("Min VRAM GB", vramIn, "blank = template minimum", "min_vram_gb")),
        strictHint,
        h("label", { class: "rt-chk" }, spotIn, "Allow spot / interruptible", errEl("allow_spot")),
        h("span", { class: "rt-h" }, "Off: on-demand only (spot is refused in this release)."),
        h("div", { class: "rt-f" }, h("span", { class: "rt-l" }, "Routing strategy"), stratSeg, stratHint, errEl("strategy")),
        h("div", { class: "jb-sub" }, "Limits (hard caps; the quote must fit)"),
        h("div", { class: "jb-row3" }, field("Max $/GPU·h", maxPriceIn, null, "max_price_per_gpu_hour_usd"), field("Max total $", maxTotalIn, null, "max_total_cost_usd"), field("Max runtime min", maxRunIn, `auto-terminate · ${lb.max_runtime_minutes.min}–${lb.max_runtime_minutes.max}`, "max_runtime_minutes")),
        h("div", { class: "jb-sub" }, "Access"),
        field("SSH public key (optional)", sshIn, null, "ssh_public_key"),
        h("p", { class: "rt-h jb-sshnote" }, "If empty, OpenGrid generates an ", h("b", {}, "ephemeral"), " key pair for the launch and discards the private half at once: ", h("b", {}, "nobody — including OpenGrid — can SSH in."),
          " Results arrive through the job's logs, metrics and artifacts. Paste only a PUBLIC key (.pub)."),
        sbBox,
        h("div", { class: "rt-acts" }, previewBtn, submitBtn), dirty, formErr,
        h("p", { class: "rt-h" }, "Preview writes an audit record but never calls a provider. Submit creates the job and a quote; under SUPERVISED execution an OpenGrid admin approves that exact quote before anything launches. Training failure also terminates the instance."));
      paint();

      /* ----- Journey B: the guided free simulation (same job body as the full form, sandbox forced on) ----- */
      if (beginner) {
        const eb = spec.epochs || DEFAULT_CONFIG.epochs;
        const epochsIn = h("input", { class: "field num", type: "number", min: eb.min, max: eb.max, step: 1, value: f.config.epochs ?? eb.default, inputmode: "numeric", "aria-label": "Epochs",
          oninput: e => { f.config.epochs = e.target.value === "" ? null : +e.target.value; drawConfig(); changed(); } });
        const easyMsg = h("div", { class: "jb-easy-msg", "aria-live": "polite" });
        const startBtn = h("button", { class: "btn pri jb-easy-go", type: "button", onclick: () => startEasy() }, "Start free simulation");
        async function startEasy() {
          const v = validate(Object.assign({}, f, { sandbox: true }), spec, lb);
          showErrors(v);
          if (!v.ok) { easyMsg.replaceChildren(h("div", { class: "jb-ferr-b" }, Object.values(v.errors).join(" · "))); return; }
          startBtn.disabled = true;
          easyMsg.replaceChildren();
          await doSubmit(v.body, easyMsg);
          if (ctx.alive()) startBtn.disabled = false;
        }
        easyBox.replaceChildren(h("section", { class: "jb-easy box", "aria-label": "Train an example model" },
          h("div", { class: "jb-easy-ban", role: "note" }, h("b", {}, "FREE SIMULATION"), h("span", { class: "jb-sim" }, "SIMULATED — no GPU, no spend"),
            h("span", { class: "dim" }, "Nothing is rented and nothing is charged. No card, no approval.")),
          h("div", { class: "jb-easy-g" },
            h("div", {},
              h("h2", {}, "Teach a small network to read handwritten digits"),
              h("p", {}, "This is OpenGrid's PyTorch MNIST example: a small convolutional network learns from 60,000 images of handwritten digits and is tested on 10,000 more. You'll see exactly what a real training job looks like — without renting a GPU."),
              h("ol", { class: "jb-easy-steps" },
                h("li", {}, h("b", {}, "Preparing"), " — OpenGrid creates and checks the job, exactly as for a real run."),
                h("li", {}, h("b", {}, "Running"), " — a simulator plays the training epochs. Loss and accuracy are synthetic."),
                h("li", {}, h("b", {}, "Saving results"), " — you get logs, metrics and output files, all labelled SIMULATED."),
                h("li", {}, h("b", {}, "Complete"), " — usually within a minute. Cost: $0.00."))),
            h("div", { class: "jb-easy-side" },
              h("label", { class: "rt-f" }, h("span", { class: "rt-l" }, "Epochs (passes over the data)"), epochsIn, h("span", { class: "rt-h" }, `${eb.min}–${eb.max} · more epochs = a longer simulated run`)),
              startBtn,
              h("p", { class: "rt-h" }, "Starts at once and opens the job page."),
              easyMsg)),
          h("p", { class: "note jb-easy-real" }, "Ready for a real GPU? Real runs use the same job with a hard cost cap and an OpenGrid approval before anything launches. Open ",
            h("b", {}, "Advanced details"), " below, untick Sandbox and preview the route. Want to size a GPU for your own model? ", h("a", { class: "lnk", href: "/find" }, "Find Compute →"))));
      }

      /* ----- readiness (what blocks a real run now) ----- */
      ctx.api("/v1/jobs/readiness", { nocache: true }).then(r => { if (ctx.alive()) readiness.replaceChildren(readinessBlock(r, admin)); },
        e => { if (ctx.alive()) readiness.replaceChildren(e.status === 404 || e.status === 405 ? h("div", { class: "og-mode m-unknown" }, h("b", {}, "READINESS"), h("span", {}, "GET /v1/jobs/readiness is not available on this server yet. Preview lists the blockers.")) : null); });

      /* ----- preview ----- */
      async function preview() {
        const v = validate(f, spec, lb);
        showErrors(v);
        if (!v.ok) return null;
        out.replaceChildren(OG.loading("Validating the workload and ranking every eligible GPU…"));
        try {
          const r = await ctx.api("/v1/jobs/preview", { method: "POST", body: v.body, full: true, slot: "job-preview" });
          last = r.data; lastBody = v.body; dirty.hidden = true;
          renderPreview(r.data, r.meta, v.body);
          return r.data;
        } catch (e) {
          if (e.stale) return null;
          out.replaceChildren(apiErr(e, "POST /v1/jobs/preview"));
          return null;
        }
      }
      function renderPreview(sel, meta, body) {
        out.replaceChildren(J.previewPanel(sel, { body, meta, sandbox: body.sandbox }));
      }

      /* ----- submit: one Idempotency-Key per intent; confirmation states the exposure ----- */
      async function submit() {
        const v = validate(f, spec, lb);
        showErrors(v);
        if (!v.ok) return;
        if (!last || !dirty.hidden || JSON.stringify(lastBody) !== JSON.stringify(v.body)) { if (!(await preview())) return; }
        const vw = views(last, { sandbox: v.body.sandbox });
        const L = v.body.limits, est = last.estimate || {};
        const sub = last.submission || {};
        if (!v.body.sandbox && (!vw.executable || sub.can_submit === false)) {
          await OG.dialog({ title: "Nothing is executable right now", confirm: "OK", cancel: "Close",
            body: h("div", {}, h("p", {}, "OpenGrid will not submit a real job without an execution-ready route. Fix the blockers shown in the preview, or tick Sandbox to run a SIMULATED job (no GPU, no spend)."),
              vw.blockers.length ? h("ul", { class: "jb-dlg-l" }, vw.blockers.map(b => h("li", {}, b.message))) : null,
              sub.refusal ? h("p", { class: "warn-t" }, "The server would refuse: ", sub.refusal.message || sub.refusal.code) : null) });
          return;
        }
        const c = vw.exec.cand;
        const ok = await OG.dialog({
          title: v.body.sandbox ? "Submit SIMULATED sandbox job" : "Submit training job for approval",
          confirm: v.body.sandbox ? "Submit sandbox job" : "Submit for approval",
          ack: v.body.sandbox ? null : `I accept a maximum cost exposure of ${usd(L.max_total_cost_usd)} and a ${L.max_runtime_minutes}-minute runtime ceiling`,
          body: h("div", { class: "jb-dlg" },
            v.body.sandbox ? h("p", { class: "jb-sim-b" }, h("b", {}, "SIMULATED — no GPU, no spend."), " No provider is called; every number is synthetic and labelled so.")
              : h("p", {}, "Creates the job and a ", h("b", {}, "quote"), ". ", h("b", {}, sub.would === "launch" ? "LIVE mode: a validated, live-enabled provider may launch at once (within cost guards)." : "Approval is required before anything launches:"),
                sub.would === "launch" ? "" : " an OpenGrid admin approves that exact quote on the route ticket. Nothing is charged before then."),
            h("table", { class: "rt-dlg-t" },
              h("tr", {}, h("th", {}, "Job"), h("td", { class: "mono" }, `${v.body.name} · ${v.body.template} · ${v.body.config.epochs ?? "?"} epochs`)),
              !v.body.sandbox && c ? h("tr", {}, h("th", {}, "Route"), h("td", { class: "mono" }, `${OG.providerName(c.provider)} · ${c.gpu_count || v.body.resources.gpu_count}× ${OG.shortGpu(c.gpu || "")} · ${J.regionText(c)}`)) : null,
              !v.body.sandbox ? h("tr", {}, h("th", {}, "Max cost exposure"), h("td", { class: "mono" }, h("b", {}, usd(L.max_total_cost_usd)), est.est_max_total != null ? ` · est. max ${usd(est.est_max_total)} · typical ${usd(est.est_typical_total)}` : "")) : null,
              h("tr", {}, h("th", {}, "Price cap"), h("td", { class: "mono" }, `${perGpuH(L.max_price_per_gpu_hour_usd)}`)),
              h("tr", {}, h("th", {}, "Runtime ceiling"), h("td", { class: "mono" }, `${L.max_runtime_minutes} min — the instance is terminated at the deadline (set at approval), training failure terminates too`)),
              h("tr", {}, h("th", {}, "SSH"), h("td", {}, v.body.ssh_public_key ? "your public key only" : "ephemeral key, private half discarded: nobody can SSH in")))),
        });
        if (!ok) return;
        await doSubmit(v.body);
      }
      async function doSubmit(body, where) {
        I.submit = OG.intentFor(I.submit, "job:create", body);
        const msg = h("div", {}, OG.loading(body.sandbox ? "Creating the sandbox job…" : "Creating the job: validation, selection, quote…"));
        (where || out).prepend(msg);
        try {
          const r = await OG.api.intent(I.submit, "/v1/jobs", { method: "POST", body });
          const job = r && (r.job || r);
          if (job && job.job_id) { OG.go("/jobs/" + encodeURIComponent(job.job_id)); return; }
          msg.replaceChildren(h("div", { class: "og-ok" }, h("b", {}, "Submitted"), " ", JSON.stringify(r).slice(0, 200)));
        } catch (e) {
          msg.replaceChildren(apiErr(e, "POST /v1/jobs"), I.submit.state === "unknown" ? h("p", { class: "warn-t" }, "Outcome unknown. ",
            h("button", { class: "btn sm", type: "button", onclick: () => { msg.remove(); doSubmit(body, where); } }, "Retry (same Idempotency-Key)"), " — the server replays the first answer instead of creating a second job.") : null);
        }
      }
      if (query.preset === "mnist" || !query.template) { /* defaults already applied */ }
      if (query.preview === "1") preview();
      else out.replaceChildren(h("div", { class: "jb-empty" }, h("p", {}, "The MNIST preset is filled in. ", h("b", {}, "Preview route"), " shows the market's best listing, the route OpenGrid can actually execute now and why — or exactly what blocks a real run."),
        h("p", { class: "dim" }, "Tick ", h("b", {}, "Sandbox"), " to walk the whole lifecycle with synthetic numbers first: no GPU, no spend.")));
    },
  });

  // An API error with its actionable message: 422 {code, message, field?, blockers?, hint?, job_id?} (workloads /
  // jobs refusals), 409 idempotency, 403 scope.
  function apiErr(e, what) {
    if (e && (e.status === 404 || e.status === 405) && !(e.body && e.body.detail && e.body.detail.code === "job_not_found")) return J.notYet(what, e);
    const d = e && e.body && e.body.detail;
    const o = d && typeof d === "object" && !Array.isArray(d) ? d : {};
    const code = o.code || null;
    const fields = Array.isArray(d) ? d.map(x => ({ field: (x.loc || []).join("."), message: x.msg })) : null;
    const bl = normBlockers(o.blockers);
    const a = code && CODE_ACTION[code] ? CODE_ACTION[code](o) : null;
    return h("div", { class: "og-alarm jb-apierr" + (e && e.status >= 500 ? "" : " t-warn") }, h("b", {}, code ? code.replace(/_/g, " ") : e && e.status ? "HTTP " + e.status : "Error"),
      h("span", {}, o.message || (e && e.message) || "request failed"),
      o.field ? h("span", { class: "mono dim" }, "field " + o.field) : null,
      a && a.label ? (a.href ? h("a", { class: "lnk", href: a.href }, a.label + " →") : h("span", { class: "dim" }, "→ " + a.label)) : null,
      o.job_id ? h("a", { class: "lnk mono", href: "/jobs/" + encodeURIComponent(o.job_id) }, "job " + o.job_id + " (recorded) →") : null,
      bl.length ? h("ol", { class: "jb-dlg-l" }, bl.map(b => h("li", {}, b.message, b.action && b.action.label ? [" ", b.action.href ? h("a", { class: "lnk", href: b.action.href }, b.action.label + " →") : h("span", { class: "dim" }, "→ " + b.action.label)] : null))) : null,
      o.hint ? h("span", { class: "dim jb-hint" }, "Hint: " + o.hint) : null,
      fields ? h("ul", { class: "jb-dlg-l" }, fields.map(x => h("li", {}, x.field + ": " + x.message))) : null,
      e && e.status === 403 ? h("span", { class: "dim" }, "Your key needs the route:preview (preview) or route:execute (submit / cancel) scope. ", h("a", { class: "lnk", href: "/keys" }, "API keys →")) : null);
  }
  J.apiErr = apiErr;

  // GET /v1/jobs/readiness: {ready, execution_mode, live_provisioning_enabled, blockers[{code, reason}],
  // checks[{code, ok, required, detail}], providers[{provider, ready, reasons, flags}], note}
  function readinessBlock(r, admin) {
    if (!r) return null;
    const items = Array.isArray(r.checks) ? r.checks : [];
    const bl = normBlockers(r.blockers);
    const provs = Array.isArray(r.providers) ? r.providers : [];
    const ready = r.ready === true;
    const link = i => { const a = blockerAction(i); return !i.ok && a.label ? (a.href && admin ? h("a", { class: "lnk", href: a.href }, a.label + " →") : h("span", { class: "dim" }, "→ " + a.label)) : null; };
    return h("div", { class: "og-mode " + (ready ? "m-supervised" : "m-preview_only") + " jb-readiness" },
      h("b", {}, ready ? "READY FOR A REAL RUN" : "REAL RUN BLOCKED"),
      h("span", {}, ready ? "Every required gate is open; an admin still approves each quote." : `${bl.length} required gate${bl.length === 1 ? "" : "s"} closed: ${bl.map(b => String(b.code || "").replace(/^job_job_/, "job_").replace(/_/g, " ")).join(", ")}. Sandbox jobs still run (simulated).`),
      r.execution_mode ? OG.badge("mode " + r.execution_mode) : null,
      r.live_provisioning_enabled === false ? OG.badge("live provisioning off") : null,
      items.length ? h("details", { class: "jb-rd" }, h("summary", {}, `${items.length} checks · ${provs.length} provisionable provider${provs.length === 1 ? "" : "s"} — details`),
        h("ul", { class: "ck-list" }, items.map(i => h("li", { class: i.ok ? "ck-green" : i.required === false ? "ck-unknown" : "ck-red" }, h("i", { class: "ck-dot" }),
          h("b", {}, String(i.code || i.key || "").replace(/^job_job_/, "job_").replace(/_/g, " ")), h("span", { class: "dim" }, i.detail || i.reason || ""), link(i))),
          provs.map(p => h("li", { class: p.ready ? "ck-green" : "ck-red" }, h("i", { class: "ck-dot" }), h("b", {}, OG.providerName(p.provider)),
            h("span", { class: "dim", title: (p.reasons || []).map(x => x.reason).join("\n") || null }, p.ready ? "execution-ready" + (p.approval_mode ? " · " + p.approval_mode : "") : (p.reasons || []).map(x => String(x.code || "").replace(/_/g, " ")).join(" · ") || "not ready"),
            !p.ready && (p.reasons || [])[0] ? link(Object.assign({ provider: p.provider }, p.reasons[0])) : null))),
        r.note ? h("p", { class: "note" }, r.note) : null) : null);
  }

  /* ---- the preview panel (also used by job.js for the stored routing decision) ---- */
  J.previewPanel = (sel, o) => {
    o = o || {};
    const s = sel || {};
    const vw = views(s, { sandbox: o.sandbox });
    const est = s.estimate || null;
    const body = o.body || {};
    const R = body.resources || (s.request && s.request.resources) || null, Lm = body.limits || (s.request && s.request.limits) || null;
    const req = R && Lm ? [`${R.gpu_count || 1}× ${!R.gpu || R.gpu === "auto" ? "GPU auto" : OG.shortGpu(R.gpu)}`, R.region ? R.region + (R.strict_region ? " (strict)" : "") : "any region",
      "≤ " + perGpuH(Lm.max_price_per_gpu_hour_usd), "≤ " + usd(Lm.max_total_cost_usd) + " total", Lm.max_runtime_minutes + " min", s.strategy || (body.routing && body.routing.strategy)] : [s.strategy];
    const fr = s.market_data_freshness;
    const frBadge = fr && typeof fr === "object" ? (fr.selected_age_seconds != null ? OG.freshBadge(fr.selected_age_seconds) : fr.newest_observed_at ? OG.freshBadge(fr.newest_observed_at) : null) : fr ? OG.freshBadge(fr) : null;
    const kids = [];
    kids.push(h("div", { class: "rt-reqbar" }, h("span", { class: "eyebrow" }, o.title || "Preview"), h("span", { class: "mono" }, req.filter(Boolean).join("  ·  ")),
      o.meta && o.meta.kind ? OG.kindBadge(String(o.meta.kind).toLowerCase()) : null, h("span", { class: "spacer" }),
      s.execution_mode ? OG.badge("mode " + s.execution_mode, s.execution_mode === "SUPERVISED" ? "warn" : s.execution_mode === "LIVE" ? "bad" : "", OG.MODE_HELP && OG.MODE_HELP[s.execution_mode]) : null,
      s.live_provisioning_enabled === false ? OG.badge("live provisioning off", "", "ROUTING_LIVE_PROVISIONING=false on this deploy: nothing can launch") : null,
      frBadge ? h("span", { class: "dim jb-fresh", title: fr && fr.rule ? fr.rule : null }, "market data ", frBadge) : null,
      s.as_of ? h("span", { class: "dim mono" }, "as of " + fmt.time(s.as_of)) : null));

    if (vw.blockers.length) {
      const act = b => b.action && b.action.label ? (b.action.href ? h("a", { class: "lnk jb-bl-a", href: b.action.href }, b.action.label + " →") : h("span", { class: "jb-bl-a" }, "→ " + b.action.label)) : null;
      const strip = (b, p) => p && String(b.message).toLowerCase().startsWith(String(p).toLowerCase() + ":") ? String(b.message).slice(String(p).length + 1).trim() : b.message;
      const groups = groupBlockers(s.blockers);
      kids.push(h("div", { class: "jb-blockers", role: "alert" },
        h("div", { class: "jb-bl-h" }, h("b", {}, "NOTHING IS EXECUTABLE RIGHT NOW"), h("span", { class: "dim" }, vw.blockers.length + " blocker" + (vw.blockers.length === 1 ? "" : "s") + " — a real run cannot launch until they are cleared" + (o.sandbox ? " (the sandbox still runs: simulated)" : ""))),
        groups.filter(g => !g.provider).map(g => h("ol", { class: "jb-bl-glob" }, g.items.map(b => h("li", {}, h("span", {}, b.message), b.code ? h("span", { class: "mono dimmer" }, b.code) : null, act(b))))),
        groups.some(g => g.provider) ? h("details", { class: "jb-bl-prov", open: groups.filter(g => g.provider).length <= 2 ? true : null },
          h("summary", {}, `Per provider · ${groups.filter(g => g.provider).length} provisionable provider${groups.filter(g => g.provider).length === 1 ? "" : "s"} not ready (codes; open for the exact reasons)`),
          groups.filter(g => g.provider).map(g => h("div", { class: "jb-bl-g" }, h("span", { class: "jb-bl-p" }, OG.providerName(g.provider)),
            h("div", {}, h("div", { class: "jb-bl-codes" }, g.items.map(b => h("span", { class: "badge", title: strip(b, g.provider) }, String(b.code || "blocked").replace(/_/g, " "))), act(g.items[0])),
              h("ul", { class: "jb-bl-full" }, g.items.map(b => h("li", {}, strip(b, g.provider), " ", act(b)))))))) : null));
    }
    else if (!vw.executable) kids.push(h("div", { class: "jb-blockers" }, h("div", { class: "jb-bl-h" }, h("b", {}, "NOTHING IS EXECUTABLE RIGHT NOW"), h("span", { class: "dim" }, "the server returned no execution-ready route" + (vw.exec.blocked_cand ? " (its candidate is flagged not executable)" : " and no blocker")))));
    const sub = s.submission;
    if (sub) kids.push(h("div", { class: "jb-subm " + (sub.can_submit ? "ok" : "no") }, h("span", { class: "eyebrow" }, "Submit would"),
      h("b", { class: "mono" }, sub.would === "pending_approval" ? "create the job + a quote → PENDING APPROVAL" : sub.would === "simulated" ? "run a SIMULATED job (no provider)" : sub.would === "launch" ? "LAUNCH (LIVE mode)" : sub.would === "refused" ? "be REFUSED" : String(sub.would || "?")),
      sub.refusal ? h("span", { class: "dim" }, (sub.refusal.code || "").replace(/_/g, " ")) : null, sub.note ? h("span", { class: "dim" }, sub.note) : null,
      sub.ssh ? h("span", { class: "dim" }, "SSH: ", sub.ssh.mode === "customer" ? h("span", { class: "mono" }, sub.ssh.fingerprint || "your key") : "ephemeral key, private half discarded — nobody can SSH in") : null));
    if (vw.exec.violations.length) kids.push(h("div", { class: "og-alarm t-warn" }, h("b", {}, `${vw.exec.violations.length} account limit violation${vw.exec.violations.length === 1 ? "" : "s"}`),
      h("span", {}, "the route is executable but an admin must override these at approval (recorded with a reason): "), h("span", { class: "mono" }, vw.exec.violations.map(v => v.code || v.message).join(", "))));

    kids.push(h("div", { class: "jb-views" },
      viewCard("exec", vw.exec.label, vw.exec.cand || vw.exec.blocked_cand, vw.exec.executable, vw.exec.executable ? vw.exec.note : vw.exec.blocked_cand ? "The server named a candidate but it is not executable." : vw.exec.note),
      viewCard("market", vw.market.label, vw.market.cand, false, vw.market.same_as_exec ? "Same listing as the execution-ready route." : vw.market.note),
      h("div", { class: "jb-view jb-v-sandbox" + (o.sandbox ? " on" : "") }, h("div", { class: "jb-vh" }, h("span", { class: "jb-vl" }, vw.sandbox.label), o.sandbox ? OG.badge("selected", "warn") : OG.badge("available")),
        h("div", { class: "jb-sim" }, "SIMULATED — no GPU, no spend"), h("p", { class: "rt-h" }, s.sandbox_note || vw.sandbox.note))));

    const forTxt = est ? (est.for === "execution_ready" ? "for the execution-ready route" : "for the market recommendation (NOT executable)") : null;
    kids.push(h("div", { class: "jb-est" },
      cell("Est. typical total", est && est.est_typical_total != null ? usd(est.est_typical_total) : OG.na("no candidate"),
        est ? (est.est_typical_range ? `${usd(est.est_typical_range[0])}–${usd(est.est_typical_range[1])} · ` : "") + (est.typical_minutes ? `${est.typical_minutes.join("–")} min expected (estimate)` : "") : null, OG.kindBadge("estimated")),
      cell("Est. max total", est && est.est_max_total != null ? h("span", { class: "rt-q" }, usd(est.est_max_total)) : OG.na("no candidate"), est ? estimateLine(est) : null, OG.kindBadge("estimated")),
      cell("Your cap", Lm ? usd(Lm.max_total_cost_usd) : "–", Lm ? "max_total_cost_usd: candidates over it are excluded" : null),
      cell("Strategy", s.strategy || "–", (STRATEGIES.find(x => x[0] === s.strategy) || [])[2] || null)));
    if (est) kids.push(h("p", { class: "note jb-basis" }, h("b", {}, "Estimate " + forTxt + ": "), est.basis || ""));

    const top = vw.exec.cand || vw.market.cand;
    kids.push(h("div", { class: "cols-2" },
      top && top.factors ? OG.section("Why it won · " + OG.providerName(top.provider) + (vw.exec.cand ? "" : " (market recommendation)"), J.whyList(top),
        h("div", { class: "jb-sbar" }, "score ", h("b", { class: "mono" }, top.score != null ? (+top.score).toFixed(3) : "–"), " ", J.scoreBar(top, 220)),
        top.why ? h("details", { class: "note jb-why" }, h("summary", {}, "Full explanation"), top.why) : null) : OG.section("Why it won", h("p", { class: "note" }, "No candidate to explain.")),
      OG.section("Workload requirements", requirementsBlock(s))));

    const alts = Array.isArray(s.alternatives) ? s.alternatives : [];
    kids.push(OG.section(`Next-best alternatives · ${alts.length}`, alts.length ? OG.table({
      columns: [
        { key: "rank", label: "#", num: true, width: "28px" },
        { key: "provider", label: "Provider", fmt: v => v ? OG.providerLink(v) : "–" },
        { key: "gpu", label: "GPU", fmt: v => v ? OG.gpuLink(v) : "–" },
        { key: "region", label: "Location", value: r => J.regionText(r), cls: "dim" },
        { key: "price_per_gpu_hour", label: "$/GPU·h", num: true, fmt: v => fmt.price(v), title: "Observed market price" },
        { key: "est_max", label: "Est. max", num: true, value: r => r.cost ? r.cost.est_max_total : null, fmt: v => v != null ? usd(v) : "–" },
        { key: "executable", label: "Executable", value: r => candExecutable(r) && !vw.blockers.length ? 1 : 0,
          fmt: (v, r) => v ? OG.badge("executable", "good") : h("span", { class: "dim", title: ((r.readiness && r.readiness.reasons) || []).map(x => x.reason).join("\n") || null }, "no" + (r.readiness && r.readiness.reasons && r.readiness.reasons.length ? ` · ${r.readiness.reasons.length} reason${r.readiness.reasons.length === 1 ? "" : "s"}` : "")) },
        { key: "score", label: "Score", num: true, fmt: v => v != null ? (+v).toFixed(3) : "–" },
        { key: "bar", label: "Breakdown", sort: false, csv: false, fmt: (v, r) => J.scoreBar(r, 110) },
        { key: "vs_selected", label: "vs #1", cls: "dim jb-rs", fmt: v => h("span", { title: v || null }, v || "") },
      ], rows: alts, sort: { key: "rank", dir: "asc" }, compact: true, csv: "opengrid-job-alternatives.csv",
    }) : h("p", { class: "note" }, "No other candidate.")));
    const ex = Array.isArray(s.exclusions) ? s.exclusions : [];
    const inel = Array.isArray(s.ineligible_gpus) ? s.ineligible_gpus : [];
    const byCode = Object.entries(s.exclusions_by_code || {}).sort((a, b) => b[1] - a[1]);
    kids.push(h("div", { class: "jb-exw" },
      OG.section(`Exclusions · ${s.exclusions_total ?? ex.length}`, byCode.length ? h("div", { class: "jb-codes mono dim" }, byCode.map(([c, n]) => `${c.replace(/_/g, " ")} ${n}`).join(" · ")) : null,
        ex.length ? OG.table({
          columns: [
            { key: "provider", label: "Provider", fmt: v => v ? OG.providerLink(v) : "–" },
            { key: "gpu", label: "GPU", fmt: v => v ? OG.shortGpu(v) : "–", cls: "dim" },
            { key: "price_per_gpu_hour", label: "$/GPU·h", num: true, fmt: v => v != null ? fmt.price(v) : "–" },
            { key: "code", label: "Code", fmt: v => v ? OG.badge(String(v).replace(/_/g, " ")) : "" },
            { key: "reason", label: "Reason", cls: "dim jb-rs", fmt: v => h("span", { title: v || null }, v || "") },
          ], rows: ex, compact: true, csv: "opengrid-job-exclusions.csv", limit: 60,
          title: (s.exclusions_total || 0) > ex.length ? `first ${ex.length} shown` : null,
        }) : h("p", { class: "note" }, "Nothing excluded.")),
      OG.section(`Ineligible GPUs · ${inel.length}`, inel.length ? OG.table({
        columns: [
          { key: "gpu", label: "GPU", fmt: v => v ? OG.shortGpu(v) : "–" },
          { key: "code", label: "Code", fmt: v => v ? OG.badge(String(v).replace(/_/g, " ")) : "" },
          { key: "reason", label: "Why not", cls: "dim jb-rs", fmt: v => h("span", { title: v || null }, v || "") },
        ], rows: inel, compact: true, csv: false, limit: 300,
      }) : h("p", { class: "note" }, "Every known GPU meets the requirements."))));
    if (Array.isArray(s.notes) && s.notes.length) kids.push(h("ul", { class: "note jb-notes" }, s.notes.map(n => h("li", {}, n))));
    kids.push(h("p", { class: "note" }, "Prices: candidates carry ", h("b", {}, "observed market prices"), "; Submit produces a ", h("b", {}, "quote"), " (re-validated live before launch); a deployment carries the ",
      h("b", {}, "execution price"), " only once a provider reports one. A market recommendation is never presented as executable. ",
      h("a", { class: "lnk", href: s.methodology || "/methodology/data-kinds" }, "Method →")));
    return h("div", { class: "pg-route jb-pv" }, kids);

    function cell(label, value, sub, badge) {
      return h("div", { class: "rt-c" }, h("div", { class: "rt-cl" }, label, badge || null), h("div", { class: "rt-cv" }, value), sub ? h("div", { class: "rt-cs", title: typeof sub === "string" ? sub : null }, sub) : null);
    }
    function viewCard(kind, label, c, executable, note) {
      const head = h("div", { class: "jb-vh" }, h("span", { class: "jb-vl" }, label),
        kind === "exec" ? (executable ? OG.badge("executable", "good", "OpenGrid can run this now (after approval)") : OG.badge("not executable", "bad"))
          : OG.badge("information only", "", "Best observed listing for the workload; OpenGrid may not be able to run it"));
      if (!c) return h("div", { class: "jb-view jb-v-" + kind + (kind === "exec" ? " no" : "") }, head, h("div", { class: "jb-none" }, kind === "exec" ? "No execution-ready route" : "No compatible listing"), h("p", { class: "rt-h" }, note));
      const reasons = c.readiness && Array.isArray(c.readiness.reasons) ? c.readiness.reasons : [];
      const k = c.cost || {};
      return h("div", { class: "jb-view jb-v-" + kind + (kind === "exec" ? (executable ? " ok" : " no") : "") }, head,
        h("div", { class: "jb-vp" }, OG.logo(c.provider, 16), h("a", { class: "lnk jb-vpn", href: "/provider/" + encodeURIComponent(c.provider) }, c.provider_display || OG.providerName(c.provider)),
          c.rank ? h("span", { class: "dimmer mono" }, "rank " + c.rank) : null),
        h("div", { class: "jb-vg mono", title: c.gpu || null }, `${c.gpu_count || 1}× `, c.gpu ? OG.gpuLink(c.gpu) : "?", c.sku ? h("span", { class: "dimmer" }, " · " + c.sku) : null),
        h("div", { class: "dim jb-vr" }, J.regionText(c), c.observed_at ? [" · ", OG.freshBadge(c.age_seconds != null ? c.age_seconds : c.observed_at, { provider: c.provider })] : null),
        h("div", { class: "jb-vpx" },
          h("span", {}, OG.conceptBadge("observed"), " ", h("b", { class: "mono" }, perGpuH(c.price_per_gpu_hour))),
          kind === "exec" ? h("span", {}, OG.conceptBadge("quote"), " ", executable ? h("span", { class: "dim" }, "issued at Submit, re-validated live before launch") : OG.na("no quote: not executable")) : null,
          k.est_max_total != null ? h("span", { class: "dim" }, "est. max ", h("b", { class: "mono" }, usd(k.est_max_total)), k.est_typical_total != null ? " · typical " + usd(k.est_typical_total) : "", k.billed_minutes != null ? ` · ${+(+k.billed_minutes).toFixed(2)} billed min` : "") : null),
        reasons.length ? h("ul", { class: "jb-vreasons" }, reasons.slice(0, 4).map(r => h("li", {}, r.reason || r.code)), reasons.length > 4 ? h("li", { class: "dimmer" }, `+${reasons.length - 4} more`) : null) : null,
        c.readiness && c.readiness.approval_note && executable ? h("div", { class: "rt-h" }, c.readiness.approval_note) : null,
        h("p", { class: "rt-h" }, note));
    }
  };
  function requirementsBlock(s) {
    const r = s.requirements || null;
    const el = (s.eligible_gpus || []).map(g => typeof g === "string" ? { gpu: g } : g);
    if (!r && !el.length) return h("p", { class: "note" }, "Requirements not returned.");
    const rows = r ? [
      ["GPU", `${r.vendor || "NVIDIA"}${r.cuda_required ? " · CUDA" : ""}${r.gpu_count ? " · " + r.gpu_count + " GPU" : ""}`],
      ["Compute capability", r.min_compute_capability != null ? `${r.min_compute_capability}${r.max_compute_capability != null ? " – " + r.max_compute_capability : "+"}` : null, r.max_compute_capability_reason],
      ["VRAM", r.min_vram_gb != null ? `≥ ${r.min_vram_gb} GB` : null, r.vram_note],
      ["Host", [r.host_cpu_arch, r.min_vcpu != null ? "≥ " + r.min_vcpu + " vCPU" : null, r.min_ram_gb != null ? "≥ " + r.min_ram_gb + " GB RAM" : null, r.min_disk_gb != null ? "≥ " + r.min_disk_gb + " GB disk" : null].filter(Boolean).join(" · ") || null, r.resource_minimums_note],
      ["Capacity", r.on_demand_only ? "on-demand only" : null, r.on_demand_note],
    ].filter(x => x[1]) : [];
    return h("div", {}, rows.length ? h("table", { class: "dep-kv" }, rows.map(([k, v, t]) => h("tr", { title: t || null }, h("th", {}, k), h("td", {}, v)))) : null,
      el.length ? h("details", { class: "note jb-elig" }, h("summary", {}, h("b", {}, `${el.length} compatible GPU model${el.length === 1 ? "" : "s"}`), ` · ${el.filter(g => g.candidates).length} with candidates now`),
        el.map(g => OG.shortGpu(g.gpu || "") + (g.listings != null ? ` (${g.candidates ?? 0}/${g.listings})` : "")).join(", "), el.some(g => g.listings != null) ? h("span", { class: "dimmer" }, " · candidates/listings") : null) : null);
  }
})();
