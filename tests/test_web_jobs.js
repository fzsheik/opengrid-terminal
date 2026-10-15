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

console.log(failures ? `\n${failures} FAILED` : "\nall passed");
process.exit(failures ? 1 : 0);
