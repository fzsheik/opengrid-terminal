// Jobs UI pure parts (web/pages/jobs.js → OG.jobs): state mapping, the MNIST preset, form validation that
// mirrors workloads/spec.py, cost formatting, preview labelling (never a fake executable route), blocker
// actions, the 12 checks, persisted metrics → progress, log cursor paging, the job summary, commands.
// Shapes follow workloads/templates.py, workloads/select.py and jobs/models.py.
// Run: node tests/test_web_jobs.js
const OG = require("../web/core.js");
global.OG = OG;
require("../web/pages/jobs.js");
const J = OG.jobs;
let failures = 0;
const ok = (c, m) => { if (!c) { failures++; console.log("FAIL:", m); } else console.log("ok  :", m); };
const eq = (a, b, m) => ok(JSON.stringify(a) === JSON.stringify(b), m + " -> " + JSON.stringify(a));

// ---------- state mapping ----------
ok(J.FLOW[0] === "created" && J.FLOW[J.FLOW.length - 1] === "reconciled" && J.FLOW.length === 14, "lifecycle flow from JOBS_BRIEF / jobs/states.py");
ok(J.tone("training_failed") === "bad" && J.tone("out_of_memory") === "bad" && J.tone("termination_failed") === "bad" && J.tone("bootstrap_failed") === "bad", "failures red");
ok(J.tone("provisioning_uncertain") === "warn" && J.tone("orphan_suspected") === "warn" && J.tone("provider_timeout") === "warn" && J.tone("credentials_unavailable") === "warn", "uncertain amber");
ok(J.tone("reconciled") === "good" && J.tone("pending_approval") === "busy" && J.tone("cancel_requested") === "busy", "good / busy tones");
const marks = s => s.map(x => x.state + ":" + x.mark[0] + (x.tone ? "/" + x.tone : "")).join(" ");
const tr = J.strip("training", ["created", "validating", "quoted", "pending_approval", "approved", "provisioning", "initializing", "training"]);
ok(tr.find(x => x.mark === "current").state === "training" && tr.filter(x => x.mark === "done").length === 7, "training strip: 7 done, training current");
const fail = J.strip("bootstrap_failed", ["created", "validating", "quoted", "pending_approval", "approved", "provisioning", "initializing"]);
const fi = fail.findIndex(x => x.state === "bootstrap_failed");
ok(fi === 7 && fail[fi].mark === "current" && fail[fi].tone === "bad" && fail[fi].off, "an off-flow failure is inserted after the furthest state reached, red: " + marks(fail.slice(5, 9)));
const unc = J.strip("provisioning_uncertain", ["created", "quoted", "pending_approval", "approved", "provisioning"]);
ok(unc.find(x => x.mark === "current").tone === "warn" && unc.find(x => x.state === "validating").mark === "skipped", "uncertain amber; states never passed are skipped");
ok(J.strip("training_failed", []).filter(x => x.mark === "current").length === 1, "no history still one current step");
const sb = J.strip("training", ["created", "validating", "initializing", "training"], true);
ok(sb.length === 6 && !sb.some(x => x.state === "pending_approval") && sb[3].mark === "current", "sandbox strip: no quote / approval / provisioning / termination");
ok(J.isTerminal({ status: "reconciled" }) && J.isTerminal({ status: "no_eligible_provider" }) && J.isTerminal({ status: "approval_rejected" }), "terminal: reconciled / ended before launch");
ok(!J.isTerminal({ status: "training_failed" }) && !J.isTerminal({ status: "termination_failed" }) && !J.isTerminal({ status: "terminating" }) && !J.isTerminal({ status: "quote_expired" }), "a failed job still terminates + reconciles; quote_expired can be re-quoted: keep polling");
ok(J.isTerminal({ status: "training_completed", sandbox: true }) && !J.isTerminal({ status: "training_completed", sandbox: false }), "a sandbox job ends at training_completed; a real one goes on to terminate");
ok(J.isTerminal({ status: "training", finished: true }) && !J.isTerminal({ status: "reconciled", finished: false }), "the server's `finished` flag wins");
ok(J.MAY_BILL.includes("training_failed") && J.MAY_BILL.includes("provisioning_uncertain") && !J.MAY_BILL.includes("pending_approval"), "a failed job may still bill; pending approval cannot");
ok(J.outcomeOf({ outcome: "succeeded" }) === "succeeded" && J.outcomeOf({}) === null, "outcome separate from state");
ok(J.isSandbox({ sandbox: true }) && J.isSandbox({ simulated: true }) && J.isSandbox({ outcome: "simulated" }) && !J.isSandbox({ sandbox: false }), "sandbox detection");
ok(J.stateOf({ status: "training" }) === "training" && J.stateOf({ state: "quoted" }) === "quoted", "status (API) read first");

// ---------- cost formatting ----------
eq([J.usd(2), J.usd(0.215), J.usd(0.004), J.usd(0), J.usd(null), J.usd(-1.5), J.usd("x")], ["$2.00", "$0.22", "<$0.01", "$0.00", "-", "−$1.50", "-"], "usd (half-up, never $0.00 for a positive amount)");
eq([J.perGpuH(1.29), J.perGpuH(12.5), J.perGpuH(null)], ["$1.290/GPU·h", "$12.50/GPU·h", "-"], "per GPU-hour");
eq([J.duration(42), J.duration(125), J.duration(3720), J.duration(null)], ["42s", "2m 05s", "1h 02m", "-"], "duration");
eq([J.bytes(512), J.bytes(2048), J.bytes(3500000), J.bytes(null)], ["512 B", "2.0 KB", "3.34 MB", "-"], "bytes");

// ---------- template + preset (workloads/templates.py shape) ----------
const tpl = { id: "pytorch-mnist", version: "1.0.0", job_type: "training", title: "Train Your First AI Model: PyTorch MNIST",
  config_schema: { epochs: { type: "integer", min: 1, max: 10, default: 3 }, batch_size: { type: "integer", min: 32, max: 512, default: 128 },
    learning_rate: { type: "number", min: 1e-5, max: 1e-1, default: 1e-3 }, seed: { type: "integer", min: 0, max: 2147483647, default: 42 } },
  requirements: { gpu_count: 1, min_vram_gb: 8, on_demand_only: true },
  limits_bounds: { max_runtime_minutes: { min: 5, max: 60 }, max_price_per_gpu_hour_usd: { min_exclusive: 0, max: 100 }, max_total_cost_usd: { min_exclusive: 0, max: 100 } },
  preset: { name: "opengrid-first-training-test", job_type: "training", template: "pytorch-mnist", config: { epochs: 3, batch_size: 128, learning_rate: 0.001, seed: 42 },
    resources: { gpu: "auto", gpu_count: 1, min_vram_gb: null, region: "US", strict_region: false, allow_spot: false }, routing: { strategy: "BALANCED", weights: null },
    limits: { max_price_per_gpu_hour_usd: 3, max_total_cost_usd: 2, max_runtime_minutes: 30 }, ssh_public_key: null, sandbox: false } };
const p = J.preset(tpl);
eq([p.name, p.job_type, p.template, p.gpu, p.gpu_count, p.region, p.strict_region, p.strategy, p.allow_spot, p.max_price_per_gpu_hour_usd, p.max_total_cost_usd, p.max_runtime_minutes, p.sandbox, p.ssh_public_key],
  ["opengrid-first-training-test", "training", "pytorch-mnist", "auto", 1, "US", false, "BALANCED", false, 3, 2, 30, false, ""], "founder's preset defaults");
eq(p.config, { epochs: 3, batch_size: 128, learning_rate: 0.001, seed: 42 }, "config defaults from the template");
const pNone = J.preset(null);
eq([pNone.name, pNone.region, pNone.max_total_cost_usd, pNone.max_runtime_minutes, pNone.config.epochs], ["opengrid-first-training-test", "US", 2, 30, 3], "no template yet: the same founder defaults");
const spec = J.configSpec(tpl), lb = J.limitBounds(tpl);
ok(spec.epochs.max === 10 && spec.batch_size.min === 32 && spec.epochs.integer && !spec.learning_rate.integer, "template bounds");
ok(J.configSpec(Object.assign({}, tpl, { preset: { config: { epochs: 5 } } })).epochs.default === 5, "the template preset's config is the default");
ok(lb.max_runtime_minutes.max === 60 && lb.max_total_cost_usd.min_exclusive === 0 && lb.gpu_count.max === 1, "limit bounds from limits_bounds + requirements.gpu_count");

// ---------- validation mirrors workloads/spec.py ----------
const v0 = J.validate(p, spec, lb);
ok(v0.ok, "the preset is valid: " + JSON.stringify(v0.errors));
eq(v0.body, { name: "opengrid-first-training-test", job_type: "training", template: "pytorch-mnist", config: { epochs: 3, batch_size: 128, learning_rate: 0.001, seed: 42 },
  resources: { gpu: "auto", gpu_count: 1, min_vram_gb: null, region: "US", strict_region: false, allow_spot: false }, routing: { strategy: "BALANCED", weights: null },
  limits: { max_price_per_gpu_hour_usd: 3, max_total_cost_usd: 2, max_runtime_minutes: 30 }, ssh_public_key: null, sandbox: false }, "body = the WorkloadSpec request shape");
const bad = (patch, key, m) => { const v = J.validate(Object.assign({}, p, patch, patch.config ? { config: Object.assign({}, p.config, patch.config) } : {}), spec, lb); ok(!v.ok && v.errors[key], m + " -> " + v.errors[key]); };
bad({ config: { epochs: 11 } }, "config.epochs", "epochs above the template max");
bad({ config: { epochs: 2.5 } }, "config.epochs", "epochs must be integer");
bad({ config: { batch_size: 8 } }, "config.batch_size", "batch below min");
bad({ config: { learning_rate: 1 } }, "config.learning_rate", "lr above max");
bad({ config: { seed: null } }, "config.seed", "missing seed");
bad({ name: "" }, "name", "name required");
bad({ name: "Opengrid-Test" }, "name", "name is lower case only (server NAME_RE)");
bad({ name: "bad name!" }, "name", "name charset");
bad({ gpu_count: 2 }, "gpu_count", "MNIST runs on exactly 1 GPU");
bad({ max_total_cost_usd: 0 }, "max_total_cost_usd", "max total must be > 0");
bad({ max_total_cost_usd: 101 }, "max_total_cost_usd", "max total ≤ 100");
bad({ max_price_per_gpu_hour_usd: 0 }, "max_price_per_gpu_hour_usd", "price cap > 0");
bad({ max_runtime_minutes: 2 }, "max_runtime_minutes", "runtime ≥ 5 min");
bad({ max_runtime_minutes: 61 }, "max_runtime_minutes", "runtime ≤ template max 60");
bad({ allow_spot: true }, "allow_spot", "spot refused in this release");
bad({ job_type: "inference" }, "job_type", "only training");
bad({ region: null, strict_region: true }, "region", "strict needs a region");
bad({ strategy: "YOLO" }, "strategy", "unknown strategy");
bad({ ssh_public_key: "-----BEGIN OPENSSH PRIVATE KEY-----\nabc" }, "ssh_public_key", "a private key is refused loudly");
bad({ ssh_public_key: "hello" }, "ssh_public_key", "not a public key");
ok(J.validate(Object.assign({}, p, { ssh_public_key: "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGq0 me@laptop" }), spec, lb).ok, "an ed25519 public key is accepted");
ok(J.validate(Object.assign({}, p, { ssh_public_key: "  " }), spec, lb).body.ssh_public_key === null, "blank key → null (ephemeral key server-side)");
ok(J.validate(Object.assign({}, p, { sandbox: true }), spec, lb).body.sandbox === true, "sandbox flag carried");
ok(J.validate(p, J.configSpec(null)).ok, "fallback bounds accept the preset");

// ---------- preview labelling (workloads/select.py shape): never mark a non-executable candidate executable ----------
const cand = (prov, executable, reasons) => ({ rank: 1, provider: prov, listing_id: prov + "-1", gpu: "NVIDIA A10 24GB", gpu_count: 1, price_per_gpu_hour: 0.75, score: 0.8,
  executable, readiness: { ready: executable, reasons: reasons || [] }, cost: { est_max_total: 0.75, est_typical_total: 0.19, billed_minutes: 60 } });
const notValidated = { code: "adapter_not_validated", reason: "lambda: adapter simulated; run the supervised validation first", provider: "lambda" };
let v = J.views({ market_best: cand("vast", false, [{ code: "not_provisionable", reason: "vast: market data only", provider: "vast" }]), execution_ready: null, blockers: [notValidated] });
ok(!v.executable && !v.exec.executable && v.exec.cand === null && v.market.executable === false, "no execution_ready → nothing executable; market is information only");
ok(v.market.not_ready.length === 1, "the market recommendation's own readiness reasons are kept");
ok(v.blockers.length === 1 && v.blockers[0].message === notValidated.reason && v.blockers[0].action.href === "/admin/execution" && /Validate provider/.test(v.blockers[0].action.label), "adapter not validated → Admin → Execution → Validate provider");
v = J.views({ market_best: cand("vast", false), execution_ready: cand("lambda", true), blockers: [] });
ok(v.executable && v.exec.cand.provider === "lambda" && !v.market.same_as_exec, "execution_ready (executable) + no blocker → executable; market separate");
v = J.views({ market_best: cand("lambda", true), execution_ready: cand("lambda", true), blockers: [] });
ok(v.market.same_as_exec && v.market.executable === false, "same listing: flagged as the same; the market view itself is still information only");
v = J.views({ market_best: cand("vast", false), execution_ready: cand("lambda", true), blockers: [{ code: "mode_preview_only", reason: "execution mode PREVIEW_ONLY" }] });
ok(!v.executable && v.exec.cand === null && v.exec.blocked_cand.provider === "lambda", "a candidate with an outstanding blocker is NOT executable");
v = J.views({ execution_ready: cand("lambda", false), blockers: [] });
ok(!v.executable && v.exec.cand === null, "a candidate the server flags executable:false is never executable");
v = J.views({ execution_ready: cand("lambda", true, [notValidated]), blockers: [] });
ok(!v.executable, "readiness reasons veto even if executable says true");
v = J.views({ execution_ready: {}, blockers: [] });
ok(!v.executable, "an empty execution_ready object is not a route");
ok(!J.views(null).executable && J.views(null, { sandbox: true }).sandbox.selected && !J.views(null, { sandbox: true }).executable, "null preview → nothing executable; the sandbox is a separate view");
ok(/SIMULATED: no GPU, no spend/.test(J.views({}).sandbox.note), "sandbox labelled SIMULATED: no GPU, no spend");
ok(!J.candExecutable(cand("x", undefined)) && J.candExecutable(cand("x", undefined), true), "an alternative needs executable === true explicitly");
const viol = J.views({ execution_ready: Object.assign(cand("lambda", true), { limit_violations: [{ code: "max_total_cost" }] }), blockers: [] });
ok(viol.executable && viol.exec.violations.length === 1, "limit violations carried for the approval override warning");

// ---------- blocker actions (codes from workloads/select.py) ----------
const act = code => J.blockerAction({ code, provider: "lambda", reason: "x" });
ok(/ROUTING_LIVE_PROVISIONING/.test(act("env_ceiling").label) && /Railway variable/.test(act("env_ceiling").label), "env_ceiling → Railway variable");
ok(/PREVIEW_ONLY → Admin → Execution/.test(act("mode_preview_only").label) && act("mode_preview_only").href === "/admin/execution", "mode_preview_only → Admin → Execution");
ok(/Lambda|lambda/.test(act("adapter_not_validated").label) && /not validated → Admin → Execution → Validate provider/.test(act("adapter_not_validated").label), "adapter_not_validated names the provider");
ok(act("supervised_not_enabled").href === "/admin/execution" && act("provider_killed").href === "/admin/execution" && act("mode_disabled").href === "/admin/execution", "flags / kill switch / disabled → Admin → Execution");
ok(/LAMBDA_API_KEY/.test(act("no_credentials").label), "no_credentials → <PROVIDER>_API_KEY");
ok(act("no_eligible_listing").href === null && act("no_compatible_gpu").href === null, "request problems: no admin link");
ok(/Railway variable/.test(J.blockerAction({ message: "ROUTING_LIVE_PROVISIONING off" }).label), "text fallback: env flag");
ok(J.blockerAction({ message: "over max_total_cost" }).href === null && /limits/.test(J.blockerAction({ message: "over max_total_cost" }).label), "text fallback: budget → raise limits");
ok(J.blockerAction({ message: "x", href: "/somewhere", action: "Do it" }).href === "/somewhere", "server-provided href wins");
eq(J.normBlockers([{ code: "c1", reason: "r1" }, "plain"]).map(b => b.message), ["r1", "plain"], "blocker message from reason / string");
eq(J.estimateLine({ price_per_gpu_hour: 1.29, gpu_count: 1, billed_minutes: 60, billing_unit: "per hour" }), "$1.290/GPU·h × 1 GPU × 60 billed min (billing unit per hour)", "estimate line: price × GPUs × billed minutes + billing unit");

// ---------- the 12 checks (jobs/models.py) ----------
let cs = J.checks({ checks: [{ key: "instance_created", label: "GPU instance created", status: "pass", source: "provider", evidence: { provider_instance_id: "i-1" } }, { key: "gpu_detected", status: "fail" }] });
ok(cs.length === 12 && cs[0].status === "pass" && cs[3].status === "fail" && cs[11].status === "pending" && cs[11].key === "termination_verified", "12 checks in order, pass/fail/pending");
cs = J.checks({ sandbox: true, checks: [{ key: "instance_created", status: "pass" }, { key: "training_ran", status: "simulated" }] });
ok(cs.every(c => c.status !== "pass") && cs[0].status === "simulated" && cs[5].status === "simulated", "a SIMULATED job never shows a real check mark");
ok(J.checks({ checks: [{ key: "custom_x", status: "pass" }] }).length === 13, "unknown server checks are appended, not dropped");
ok(J.checks({ checks: [{ key: "gpu_detected", status: "weird" }] })[3].status === "pending", "an unknown status is pending, never pass");

// ---------- metrics → progress (persisted only) ----------
const long = [{ epoch: 1, name: "train_loss", value: 0.21, at: "2026-10-13T10:00:00Z", source: "runner" }, { epoch: 1, name: "test_accuracy", value: 0.972 },
  { epoch: 2, name: "train_loss", value: 0.08 }, { epoch: 2, name: "test_accuracy", value: 98.1 }, { epoch: 2, name: "gpu_util", value: 90 }];
const pts = J.metricPoints({ metrics: long });
ok(pts.length === 2 && pts[0].epoch === 1 && pts[0].loss === 0.21 && Math.abs(pts[1].acc - 0.981) < 1e-9, "long job_metrics rows grouped per epoch; % accuracy → fraction; other metrics ignored");
const wide = J.metricPoints([{ seq: 3, data: { epoch: 2, epochs: 3, train_loss: 0.08, test_accuracy: 0.981, elapsed_seconds: 40 } }, { seq: 2, data: { epoch: 1, epochs: 3, train_loss: 0.21, test_accuracy: 0.97 } }]);
ok(wide.length === 2 && wide[1].elapsed === 40 && wide[0].epochs === 3, "wide rows under .data also read");
const ser = J.metricPoints({ series: { train_loss: [{ epoch: 1, value: 0.3 }, { epoch: 2, value: 0.1 }], test_accuracy: [{ epoch: 1, value: 0.95 }] } });
ok(ser.length === 2 && ser[0].acc === 0.95 && ser[1].acc === null, "series form");
const pr = J.progress(wide, { config: { epochs: 3 } });
eq([pr.epoch, pr.epochs, pr.loss, pr.acc, pr.elapsed, Math.round(pr.pct * 100)], [2, 3, 0.08, 0.981, 40, 67], "progress from the latest persisted metric");
eq(J.progress([], { config: { epochs: 3 } }), { epoch: null, epochs: 3, loss: null, acc: null, elapsed: null, pct: null }, "no metrics → nothing invented");

// ---------- logs: cursor paging ----------
let lp = J.logPage({ logs: [{ id: 11, seq: 1, line: "a", stream: "stdout" }, { id: 12, seq: 2, line: "b", stream: "stderr" }], next_after: 12, has_more: true }, null);
eq([lp.lines.map(l => l.text), lp.next, lp.more, lp.lines[1].stream], [["a", "b"], 12, true, "stderr"], "log page with cursor");
lp = J.logPage([{ id: 7, line: "c" }], 6);
ok(lp.next === 7 && !lp.more, "a bare list: cursor = the last row id");
ok(J.logPage({ logs: [] }, 9).next === 9, "an empty page keeps the cursor");

// ---------- the job summary (jobs/models.py public()) ----------
const job = { job_id: "job-1", status: "training", outcome: null, sandbox: false, request: { resources: { gpu: "auto", gpu_count: 1, region: "US" }, limits: { max_runtime_minutes: 30 } },
  resource: { deployment_id: "dep_1", route_request_id: "rr_1", deployment_status: "running", disposition: "possibly_billing", possibly_billing: true, provider: "lambda", gpu: "NVIDIA A10 24GB", gpu_count: 1, region: "us-east-1" },
  selection: { provider: "lambda", gpu: "NVIDIA A10 24GB" }, estimates: { est_max_total_usd: 0.75, est_typical_total_usd: 0.19, runtime_ceiling_minutes: 30 }, started_at: null };
const su = J.summary(job);
eq([su.provider, su.gpu, su.deployment_id, su.route_request_id, su.possibly_billing, su.est_max, su.est_typical, su.runtime_ceiling, su.actual], ["lambda", "NVIDIA A10 24GB", "dep_1", "rr_1", true, 0.75, 0.19, 30, null], "summary from resource + estimates; no actual cost before reconciliation");
const su2 = J.summary(Object.assign({}, job, { resource: {} }), { status: "terminated", reconciliation: { opengrid_transaction_cost: { amount_usd: 0.31 } } });
ok(su2.provider === "lambda" && su2.actual === 0.31 && su2.actual_basis === "transaction", "selection fallback; actual = the reconciled transaction cost");
ok(J.summary(Object.assign({}, job, { sandbox: true }), { reconciliation: { opengrid_transaction_cost: { amount_usd: 1 } } }).actual === null, "a sandbox job never has an actual cost");
ok(J.summary({ sandbox: true, resource: {}, selection: { provider: "vast", gpu: "NVIDIA RTX 3090 24GB" } }).provider === null, "a sandbox job's informational selection is never shown as its placement");

// ---------- commands ----------
const hit = OG.findCommand("j new");
ok(hit && /new training job/.test(hit.cmd.usage), "`j new` command");
ok(OG.findCommand("j") && /jobs/.test(OG.findCommand("j").cmd.usage) && !/new/.test(OG.findCommand("j").cmd.usage), "`j` command");

// ---------- simple My Jobs: one plain line per job, the one-click demo, plain blocker text ----------
const L = j => J.jobLine(j);
ok(L({ status: "training", sandbox: true, events: [{ to: "created" }, { to: "training" }] }).text === "Training (simulated)", "sandbox training -> Training (simulated)");
ok(L({ status: "pending_approval" }).text === "Waiting for OpenGrid approval" && L({ status: "pending_approval" }).tone === "warn", "pending approval, amber");
ok(L({ status: "training_completed", sandbox: true, finished: true, outcome: "simulated" }).text === "Finished (simulated)", "finished sandbox");
ok(L({ status: "reconciled", finished: true, outcome: "succeeded" }).text === "Finished" && L({ status: "reconciled", finished: true, outcome: "succeeded" }).tone === "good", "reconciled real job -> Finished, green");
ok(/^Failed: out of memory/.test(L({ status: "out_of_memory", finished: true, outcome: "failed", sandbox: true, failure: { code: "out_of_memory" } }).text), "failure reason in plain words");
ok(L({ status: "provisioning_uncertain" }).tone === "bad" && /may still be billing/.test(L({ status: "provisioning_uncertain" }).text), "uncertain -> billing warning");
ok(L({ status: "approval_rejected", finished: true, outcome: "cancelled" }).text === "Cancelled", "cancelled before launch");
ok(L({ status: "quote_expired" }).text.startsWith("Price quote expired"), "expired quote");
const db = J.demoBody(null, 99);
ok(db && db.sandbox === true && db.template === "pytorch-mnist" && db.name === "free-demo-mnist" && db.config.epochs === J.DEFAULT_CONFIG.epochs.max, "demo body: sandbox forced on, MNIST, epochs clamped to the template bound");
ok(J.demoBody(null).config.epochs === J.DEFAULT_CONFIG.epochs.default && J.demoBody(null).limits.max_total_cost_usd === 2, "demo body defaults from the preset");
ok(/aren't switched on/.test(J.plainBlocker({ code: "env_ceiling" })) && /aren't switched on/.test(J.plainBlocker({ code: "adapter_not_validated" })), "platform blockers -> plain setup sentence");
ok(/price cap/.test(J.plainBlocker({ code: "no_eligible_listing" })) && J.plainBlocker(null) === "No GPU is ready for a real run right now.", "listing blockers and fallback");

// ---------- the beginner layer (plain words over the same states) ----------
ok(J.isDemoLink({ preset: "mnist" }) && J.isDemoLink({ sandbox: "1" }) && J.isDemoLink({ preset: "mnist", sandbox: "1" }) && !J.isDemoLink({}) && !J.isDemoLink({ preset: "other" }), "demo deep links: ?preset=mnist or ?sandbox=1");
const rOff = { ready: false, blockers: [{ code: "env_live_provisioning", reason: "ROUTING_LIVE_PROVISIONING is off" }, { code: "execution_mode", reason: "PREVIEW_ONLY" }, { code: "provider_ready", reason: "none" }, { code: "worker_reconcile", reason: "x" }, { code: "worker_job_sync", reason: "y" }] };
const lsOff = J.liveStatus(rOff, null);
eq([lsOff.state, lsOff.title], ["off", "Live GPU execution is not enabled yet"], "readiness off -> not enabled yet");
eq(lsOff.steps.length, 4, "what needs to happen: one plain step per distinct need (workers merged)");
ok(!lsOff.steps.join(" ").match(/ROUTING_LIVE_PROVISIONING|Railway|admin\/|PREVIEW_ONLY/), "steps never name deployment variables, modes or operator pages");
eq(J.liveStatus({ ready: true, blockers: [] }, null).state, "enabled", "readiness ready -> enabled");
eq(J.liveStatus(null, { live_provisioning_enabled: false }).state, "off", "anonymous: the public capabilities flag says off");
eq(J.liveStatus(null, { live_provisioning_enabled: true }).state, "unknown", "anonymous + flag on: unknown (sign in), never 'enabled'");
eq(J.liveStatus(null, null).state, "unknown", "nothing known: unknown");
eq(J.walkthrough(3), [{ epoch: 1, train_loss: 1, test_accuracy: 0.6 }, { epoch: 2, train_loss: 0.5, test_accuracy: 0.7 }, { epoch: 3, train_loss: 0.3333, test_accuracy: 0.8 }],
  "walkthrough = the simulation's own placeholder numbers (jobs/orchestrator.run_simulated)");
eq([J.walkthrough(99).length, J.walkthrough(0).length, J.walkthrough(6)[5].test_accuracy], [10, 3, 0.9], "walkthrough bounds (1-10 epochs; accuracy capped at 0.9)");
const gj = J.jobGroups([
  { job_id: "a", created_at: "2026-10-01", status: "reconciled", finished: true, outcome: "succeeded" },
  { job_id: "b", created_at: "2026-10-03", status: "training" },
  { job_id: "c", created_at: "2026-10-02", status: "orphan_suspected" },
  { job_id: "d", created_at: "2026-10-04", status: "training_completed", finished: true, sandbox: true, outcome: "simulated" },
  { job_id: "e", created_at: "2026-10-05", status: "quote_expired" },
]);
eq([gj.attention.map(j => j.job_id), gj.active.map(j => j.job_id), gj.completed.map(j => j.job_id)], [["e", "c"], ["b"], ["d", "a"]], "My Jobs groups: attention, active, completed (newest first)");
ok(J.cancelAction({ status: "training_completed", finished: true, sandbox: true }) === null && J.cancelAction({ status: "training", sandbox: true }) === null, "simulation: no cancel control (the server never cancels one)");
const cPre = J.cancelAction({ status: "pending_approval" });
eq([cPre.kind, cPre.button, cPre.confirm, cPre.keep, cPre.ack], ["cancel", "Cancel job…", "Cancel job", "Keep job", null], "before launch: Cancel job vs Keep job");
const cRun = J.cancelAction({ status: "training" });
eq([cRun.kind, cRun.button, cRun.confirm, cRun.keep], ["terminate", "Terminate GPU…", "Terminate GPU", "Keep running"], "running on a GPU: Terminate GPU vs Keep running");
ok(!!cRun.ack && cRun.confirm !== cRun.keep && !/^Cancel$/.test(cRun.keep) && !/^Cancel$/.test(cPre.keep), "never 'Cancel' as both dismiss and destroy; terminating needs an acknowledgement");
ok(J.cancelAction({ status: "cancelling" }) === null && J.cancelAction({ status: "reconciled", finished: true }) === null && J.cancelAction({ status: "terminating" }) === null, "no control while cancelling, terminating or finished");
const tl = J.timeline({ sandbox: true, events: [{ at: "2026-10-01T00:00:02Z", from: "validating", to: "initializing" }, { at: "2026-10-01T00:00:01Z", from: null, to: "created" },
  { at: "2026-10-01T00:00:03Z", from: "initializing", to: "initializing", reason: "note" }, { at: "2026-10-01T00:00:04Z", from: "saving_artifacts", to: "training_completed" }] });
eq(tl.map(x => x.text), ["Job created", "Setting up (simulated)", "Simulation finished"], "timeline: oldest first, plain words, notes skipped, sandbox wording");
eq(J.plainState("pending_approval"), "Waiting for OpenGrid to approve the price", "plain state words");
eq(J.nextStep({ status: "training_completed", finished: true, sandbox: true, outcome: "simulated" }).href, "/find", "after the simulation: find a GPU for my model");
eq(J.nextStep({ status: "orphan_suspected", resource: { deployment_id: "dep_1" } }).href, "/deployments/dep_1", "possibly billing: open the deployment");
eq(J.nextStep({ status: "pending_approval", route_request_id: "rr_1" }, null, { admin: true }).href, "/route?rr=rr_1", "admin + pending approval: the approval ticket");
eq(J.nextStep({ status: "pending_approval" }, null, {}).href, null, "customer + pending approval: nothing to do");
// runner_lost (jobs/sync.py: the runner reported, then went silent): a red workload failure, terminated like the others
eq(J.plainState("runner_lost"), "Lost contact with the GPU", "runner_lost in plain words");
ok(J.FAILED.includes("runner_lost") && J.MAY_BILL.includes("runner_lost") && J.tone("runner_lost") === "bad" && !J.isTerminal({ status: "runner_lost" }), "runner_lost: failed, may bill, keeps polling");
const lostRun = { status: "runner_lost", outcome: "failed", failure: { code: "runner_lost", reason: "the GPU machine stopped reporting for 12 minutes" }, resource: { deployment_id: "dep_9", possibly_billing: true, disposition: "possibly_billing" } };
const lostNext = J.nextStep(lostRun);
ok(/stopped reporting/.test(lostNext.text) && lostNext.href === "/deployments/dep_9", "runner_lost while the GPU may bill: says so, links the deployment");
ok(J.needsAttention(lostRun) && J.stages(lostRun).steps.find(x => x.key === "running").mark === "failed", "runner_lost needs attention while billing; fails the Running stage");
eq(J.timeline({ events: [{ at: "2026-10-01T00:00:01Z", from: "training", to: "runner_lost" }] }).map(x => [x.text, x.tone]), [["Lost contact with the GPU", "bad"]], "timeline: runner_lost, red");
const ended = { status: "reconciled", finished: true, outcome: "failed", failure: { code: "gpu_interrupted" }, resource: { disposition: "terminated_confirmed" } };
ok(/stopped before training finished/.test(J.nextStep(ended).text) && J.nextStep(ended).href === "/jobs/new", "interrupted, reconciled: says the run is incomplete, start a new job");
ok(J.PLAIN_FAILURE.gpu_interrupted && J.PLAIN_FAILURE.runner_lost && !J.needsAttention(ended), "failure-code titles; a reconciled interruption needs no attention");
const pe = (status, detail) => J.plainError({ status, body: detail ? { detail } : null, message: "raw" });
ok(pe(401).signIn === true && /OpenGrid account/.test(pe(401).title) && /invite-only/.test(pe(401).text), "401 -> needs an OpenGrid account (invite-only), not a customer sign-in");
ok(/account/.test(J.liveStatus(null, null).title) && !/Sign in/.test(J.liveStatus(null, null).title), "real-GPU status signed out: account wording, no sign-in prompt");
// one set of routing mode names on /jobs/new and /route
eq(J.STRATEGIES.map(s => s[1]), ["Balanced", "Cheapest", "Soonest available", "Most stable"], "routing modes: one set of names, in one order");
{
  const fs = require("fs"), path = require("path");
  const rsrc = fs.readFileSync(path.join(__dirname, "..", "web", "pages", "route.js"), "utf8");
  const jsrc = fs.readFileSync(path.join(__dirname, "..", "web", "pages", "jobs.js"), "utf8");
  ok(/OG\.jobs\.STRATEGIES/.test(rsrc) && !/"Stable"/.test(rsrc + jsrc), "/route reads the same mode list as /jobs/new");
  ok(!/Request route/.test(rsrc) && /"Request a quote…"/.test(rsrc) && /confirm: "Request a quote"/.test(rsrc), "the quote action has one name: button, dialog and text");
  ok(!/>\s*"Sign in"\)|, "Sign in"\)/.test(jsrc + rsrc), "no customer-facing 'Sign in' button on Jobs or Route (operator sign-in is a small staff link at most)");
  ok(/every\(15000, \(\) => \{ if \(!denied\) load\(\); \}\)/.test(jsrc) && /if \(!me\) \{/.test(jsrc), "/jobs never polls without access");
  ok(!/[–—]/.test(rsrc + jsrc), "no en or em dashes in route.js / jobs.js");
}
ok(/isn't allowed/.test(pe(403).title) && !/scope/.test(pe(403).text), "403 -> plain, no scope jargon");
ok(/aren't switched on/.test(pe(422, { code: "execution_disabled", message: "real execution is disabled in this environment" }).text), "refusal codes -> plain sentences");
ok(!/Railway|ROUTING_LIVE|admin\//.test(Object.values(J.PLAIN_STATE).join(" ") + ["execution_disabled", "provider_unvalidated", "credentials_unavailable"].map(c => pe(422, { code: c }).text).join(" ")), "plain errors never send a user to a deployment variable or an operator page");
ok(/Couldn't reach/.test(pe(0).title) && pe(0).retry === true, "network error -> check My Jobs before retrying");

// ---------- checkpoints / resume (jobs/artifacts.py kind "checkpoint"; jobs/models.py resume block) ----------
const ck = (e, extra) => ({ name: `ckpt-e${String(e).padStart(4, "0")}.pt`, kind: "checkpoint", epoch: e, size: 1, sha256: "a".repeat(64), ...extra });
eq([J.artifactLabel(ck(3)), J.artifactLabel({ name: "ckpt-e0012.pt" }), J.artifactLabel({ name: "model.pt", kind: "output" })],
  ["Checkpoint (epoch 3)", "Checkpoint (epoch 12)", "model.pt"], "checkpoints labelled with their epoch; results by name");
ok(!J.isCheckpoint({ name: "ckpt-e1.pt" }) && !J.isCheckpoint({ name: "model.pt" }) && J.isCheckpoint({ name: "x", kind: "checkpoint" }), "checkpoint detection mirrors the server's name pattern / kind");
const so = J.splitOutputs([ck(1), { name: "model.pt", kind: "output" }, ck(3), null, { name: "env.json" }]);
eq([so.results.map(a => a.name), so.checkpoints.map(a => a.epoch)], [["model.pt", "env.json"], [3, 1]], "results first, checkpoints newest first");
eq(J.resumeLine({ status: "training", resume: { job_id: "job-aa", epoch: 3, confirmed: true, confirmed_from_epoch: 3 } }).text, "Resumed from epoch 3 of job job-aa", "confirmed resume");
eq(J.resumeLine({ status: "pending_approval", resume: { job_id: "job-aa", epoch: 2, confirmed: false } }).text, "Will resume from epoch 2 of job job-aa", "pinned, not yet confirmed");
ok(/simulated/.test(J.resumeLine({ status: "training_completed", sandbox: true, finished: true, resume: { job_id: "job-aa", epoch: 2, confirmed: true, confirmed_from_epoch: 2, simulated: true } }).text), "a simulated resume says so");
ok(J.resumeLine({ status: "training" }) === null && J.resumeLine({ resume: null }) === null, "no resume, no line");

// ---------- automatic relaunch chain (jobs/relaunch.py chain block) ----------
const att2 = { job_id: "job-b2", status: "pending_approval", created_at: "2026-10-16T10:05:00Z",
  chain: { attempt: 2, max_attempts: 3, max_total_usd: 6, previous_job_id: "job-a1", next_job_id: null, spend_so_far_usd: 0.21,
    relaunched_after: { code: "gpu_interrupted", provider: "lambda", resume_from_epoch: 2, from_scratch: false }, relaunch: null, stopped: null } };
const att1 = { job_id: "job-a1", status: "reconciled", finished: true, outcome: "failed", created_at: "2026-10-16T10:00:00Z",
  failure: { code: "gpu_interrupted" }, chain: { attempt: 1, max_attempts: 3, max_total_usd: 6, previous_job_id: null, next_job_id: "job-b2",
    relaunch: { status: "relaunched" }, stopped: null } };
eq(J.chainLine(att2).text, "Attempt 2 of 3 · relaunched after the GPU on Lambda was interrupted · resumed from epoch 2", "attempt line: number, cause, provider, resume epoch");
eq([J.chainLine(att2).prev, J.chainLine(att1).next], ["job-a1", "job-b2"], "links both ways");
ok(/stopped reporting/.test(J.chainLine({ chain: { attempt: 2, max_attempts: 2, relaunched_after: { code: "runner_lost", provider: "runpod", from_scratch: true } } }).text)
  && /from scratch/.test(J.chainLine({ chain: { attempt: 2, max_attempts: 2, relaunched_after: { code: "runner_lost", from_scratch: true } } }).text), "runner_lost + no checkpoint said plainly");
ok(J.chainLine({ status: "training" }) === null && J.chainLine({ chain: { attempt: 1, max_attempts: 3 } }) === null, "no chain line for an ordinary job / an untouched attempt 1");
ok(J.chainStatus(att1).href === "/jobs/job-b2" && /attempt 2/.test(J.chainStatus(att1).text), "relaunched: link to the next attempt");
const stopped = { status: "reconciled", finished: true, failure: { code: "gpu_interrupted" }, chain: { attempt: 1, max_attempts: 3, stopped: { code: "retry_budget_exhausted", reason: "x" }, relaunch: { status: "stopped", code: "retry_budget_exhausted" } } };
ok(/Not relaunched: another attempt could cost more than the job's total budget/.test(J.chainStatus(stopped).text) && J.chainStatus(stopped).tone === "warn", "chain stop reason in plain words");
ok(J.chainStatus({ ...stopped, chain: { ...stopped.chain, next_job_id: "job-r3" } }).tone === "warn" && /refused/.test(J.chainStatus({ ...stopped, chain: { ...stopped.chain, next_job_id: "job-r3" } }).label), "a stop wins over a refused successor row");
ok(/Lambda|provider/.test(J.STOP_PLAIN.no_eligible_provider) && !/[A-Z_]{6,}/.test(Object.values(J.STOP_PLAIN).join(" ")), "stop reasons never show codes or env vars");
ok(/nothing is billing/.test(J.chainStatus({ chain: { attempt: 1, max_attempts: 2, relaunch: { status: "blocked" } } }).text), "blocked: says nothing bills and the operator knows");
ok(/Waiting for the provider/.test(J.chainStatus({ chain: { attempt: 1, max_attempts: 2, relaunch: { status: "pending", reason: "waiting for the provider to confirm the GPU is gone before relaunching" } } }).text), "pending: waits for the confirmed termination");
eq(J.chainSpend(att2), "Spent so far $0.21 of $6.00 across all attempts", "spend so far vs the ceiling");
ok(J.chainSpend({ chain: { attempt: 1, max_attempts: 3, max_total_usd: 6 } }) === null, "no spend line before a chain exists");
// next step + timeline
const ns1 = J.nextStep(att1);
ok(ns1.href === "/jobs/job-b2" && /relaunched this job automatically/.test(ns1.text), "interrupted + relaunched: next step opens the next attempt");
ok(J.nextStep(stopped).href === "/jobs/new" && /Not relaunched/.test(J.nextStep(stopped).text), "interrupted + chain stopped: says why, start a new job");
const tlr = J.timeline({ status: "reconciled", events: [
  { at: "2026-10-16T10:00:00Z", from: null, to: "created" },
  { at: "2026-10-16T10:09:00Z", from: "reconciled", to: "reconciled", reason: "relaunched as job-b2 on lambdb resuming from epoch 2", evidence: { relaunch_event: "relaunched", attempt: 2, provider: "lambdb", successor_job_id: "job-b2" } },
  { at: "2026-10-16T10:08:00Z", from: "reconciled", to: "reconciled", reason: "termination evidence" }] });
eq(tlr.map(t => t.text), ["Job created", "Relaunched automatically as attempt 2 on Lambdb"], "timeline keeps relaunch notes, skips other notes");
ok(J.timeline({ events: [{ at: "x", from: null, to: "created", evidence: { relaunch_event: "relaunched_from", relaunch: { attempt: 2, retry_of: "job-a1" } } }] })[0].text === "Created automatically as attempt 2", "the successor's first event");
// My Jobs: a chain shows its newest attempt; an older attempt that may bill is never hidden
const g2 = J.jobGroups([att1, att2]);
eq([g2.active.map(j => j.job_id), g2.completed.map(j => j.job_id)], [["job-b2"], []], "My Jobs groups a chain under its newest attempt");
const billing = { ...att1, status: "termination_failed", finished: false, resource: { possibly_billing: true } };
ok(J.jobGroups([billing, att2]).attention.some(j => j.job_id === "job-a1"), "an older attempt that may still bill stays visible (needs attention)");
ok(J.collapseChains([att1]).length === 1, "the previous attempt stays when its successor is not in the list");
{
  const fs = require("fs"), path = require("path");
  const jsrc = fs.readFileSync(path.join(__dirname, "..", "web", "pages", "job.js"), "utf8");
  ok(/chainNote\(\)/.test(jsrc) && /J\.chainLine\(job\)/.test(jsrc), "the job page shows the chain note");
}

console.log(failures ? `\n${failures} FAILED` : "\nall passed");
process.exit(failures ? 1 : 0);
