// Find Compute pure parts (web/pages/find.js → OG.find) and the plain job progress (web/pages/jobs.js →
// OG.jobs.stages / plainStatus): requirements from the guided form, deep-link state, analyzer JSON paste
// validation (contract keys only, size limit), reading a contract-shaped Recommendation (picks, memory fit,
// runtime never a made-up number, stale / sold-out never deployable, needs_gpu=false), and the job stage mapping
// for the sandbox and real paths. Contract: docs/OPENGRID2_CONTRACTS.md. Fixtures: tests/fixtures_web/.
// Run: node tests/test_web_find.js
const fs = require("fs");
const path = require("path");
const OG = require("../web/core.js");
global.OG = OG;
require("../web/pages/jobs.js");
require("../web/pages/find.js");
const F = OG.find, J = OG.jobs;
let failures = 0;
const ok = (c, m) => { if (!c) { failures++; console.log("FAIL:", m); } else console.log("ok  :", m); };
const eq = (a, b, m) => ok(JSON.stringify(a) === JSON.stringify(b), m + " -> " + JSON.stringify(a));
const fixture = n => JSON.parse(fs.readFileSync(path.join(__dirname, "fixtures_web", n), "utf8"));
const REC = fixture("recommendation.json"), NOGPU = fixture("recommendation_no_gpu.json"), ANALYZE = fixture("analyze_output.json");
const clone = o => JSON.parse(JSON.stringify(o));

// ---------- requirements from the guided form ----------
const ft = F.requirementsFromForm({ task: "finetune", model: "Llama 3.1 8B", method: "lora", budget: "50", region: "us", maxp: "" });
eq(ft, { source: "form", workload_type: "fine_tuning", model: { name: "meta-llama/Llama-3.1-8B", parameters_b: 8.03, architecture: "llama" },
  training: { method: "lora" }, constraints: { budget_usd: 50, region: "us" } }, "fine-tune LoRA Llama 3.1 8B, $50, US");
ok(F.checkRequirements(ft).ok, "the form body passes the contract check");
const tr = F.requirementsFromForm({ task: "train", model: "350M", seq: "1024", mbs: "8" });
eq(tr, { source: "form", workload_type: "training", model: { parameters_b: 0.35 }, training: { method: "full", seq_len: 1024, micro_batch_size: 8 } }, "training from scratch = full; a bare size → parameters_b only");
const inf = F.requirementsFromForm({ task: "infer", model: "Qwen/Qwen2.5-7B", ctx: "8192", conc: "16", quant: "awq", prec: "fp16", method: "lora" });
eq([inf.workload_type, inf.inference, inf.quantization, inf.precision, inf.training], ["inference", { context_len: 8192, concurrency: 16 }, "awq", "fp16", undefined],
  "inference: context + concurrency; a stray method is ignored (only for fine-tune)");
eq(inf.model, { name: "Qwen/Qwen2.5-7B", parameters_b: 7.62, architecture: "qwen2" }, "an HF id that matches a suggestion uses its facts");
eq(F.requirementsFromForm({ task: "unsure" }), { source: "form" }, "not sure + nothing else: nothing invented (unknown = absent)");
eq(F.requirementsFromForm({}), { source: "form" }, "empty form: no guesses");
eq(F.requirementsFromForm({ task: "finetune", model: "my-org/secret-model-13b" }).model, { name: "my-org/secret-model-13b" }, "an unknown model is sent by name; its size is never guessed from the text");
eq(F.requirementsFromForm({ task: "finetune", method: "" }).training, undefined, "fine-tune with method unknown: no training block at all");
eq(F.requirementsFromForm({ task: "infer", ctx: "abc", conc: "1.5", budget: "-3" }), { source: "form", workload_type: "inference" }, "invalid numbers are not sent");
eq([F.modelFromText("7B"), F.modelFromText("1.5 billion"), F.modelFromText(" mistral 7b "), F.modelFromText("")],
  [{ name: null, parameters_b: 7, architecture: null }, { name: null, parameters_b: 1.5, architecture: null }, { name: "mistralai/Mistral-7B-v0.3", parameters_b: 7.25, architecture: "mistral" }, null], "model text parsing");
const fe = F.formErrors({ ctx: "1.5", conc: "0", budget: "x", maxp: "5000", mbs: "4" });
eq(Object.keys(fe).sort(), ["budget", "conc", "ctx", "maxp"], "form errors only for filled, invalid fields");

// ---------- deep links ----------
const st = F.stateFromQuery({ task: "finetune", model: "Llama 3.1 8B", method: "qlora", budget: "50", region: "eu", bogus: "1", prec: "fp99", ctx: "abc" });
eq([st.task, st.model, st.method, st.budget, st.region, st.prec, st.ctx], ["finetune", "Llama 3.1 8B", "qlora", "50", "eu", "", ""], "query → state (unknown values dropped)");
const q = F.queryFromState(st);
eq([q.task, q.method, q.model, q.ctx, q.prec], ["finetune", "qlora", "Llama 3.1 8B", null, null], "state → query patch (empties clear)");
eq(F.queryFromState(Object.assign({}, st, { task: "infer" })).method, null, "method dropped from the URL unless fine-tuning");
eq(F.stateFromQuery(F.queryFromState(st)), st, "round trip");
const ans = F.answersFromQuery({ "a.training.seq_len": "4096", "a.training.method": "lora", "a.nope.x": "1", "a.training.micro_batch_size": "-2", task: "x" });
eq(ans, { "training.seq_len": "4096", "training.method": "lora" }, "answers from ?a.<field>= (unknown fields / invalid values dropped)");
eq(F.answersToQuery({ "training.seq_len": "4096" }, { "a.training.method": "lora", task: "x" }), { "a.training.method": null, "a.training.seq_len": "4096" }, "answers → query clears stale answers");
const withAns = F.requirementsFromForm({ task: "finetune", model: "7B" }, { "training.micro_batch_size": "4", "training.gradient_checkpointing": "true", "training.zero_stage": "2", "constraints.vendor": "Intel" });
eq(withAns.training, { micro_batch_size: 4, gradient_checkpointing: true, zero_stage: 2 }, "answers set typed values at their path; invalid enum ignored");
ok(F.checkRequirements(withAns).ok, "answered body still passes the contract check");
eq([F.controlFor("training.method").type, F.controlFor("training.method").options.map(o => o[0]), F.controlFor("training.seq_len").type, F.controlFor("training.gradient_checkpointing").type, F.controlFor("model.name").type, F.controlFor("nope"), F.controlFor("training")],
  ["enum", ["full", "lora", "qlora"], "int", "bool", "text", null, null], "inline answer controls from the contract");

// ---------- paste validation ----------
const pv = F.validatePaste(JSON.stringify(ANALYZE));
ok(pv.ok && pv.analyzer && pv.analyzer.files_inspected.length === 2 && pv.analyzer.files_skipped[0].path === ".env", "analyzer output accepted; file lists kept locally");
ok(pv.requirements && !("root" in pv.requirements) && !("files_inspected" in pv.requirements) && pv.requirements.workload_type === "fine_tuning", "only requirements are sent (no root path, no file names)");
ok(!("micro_batch_size" in pv.requirements.training) && pv.requirements.source === "analyzer", "nulls pruned; source kept");
const bare = F.validatePaste(JSON.stringify(ANALYZE.requirements));
ok(bare.ok && !bare.analyzer, "a bare WorkloadRequirements object is accepted");
const bad1 = F.validatePaste(JSON.stringify({ workload_type: "fine_tuning", model: { name: "x", secret_path: "/home" } }));
ok(!bad1.ok && bad1.errors.some(e => /model\.secret_path: unknown key/.test(e)), "nested non-contract key rejected: " + bad1.errors[0]);
const bad2 = F.validatePaste(JSON.stringify({ requirements: {}, file_contents: "import torch" }));
ok(!bad2.ok && bad2.errors.some(e => /file_contents: unknown key/.test(e)), "unknown analyzer top-level key rejected (file contents never sent)");
const bad3 = F.validatePaste(JSON.stringify({ workload_type: "mining", precision: "fp64", training: { micro_batch_size: 2.5, gradient_checkpointing: "yes" }, constraints: { budget_usd: -1 } }));
eq(bad3.errors.length, 5, "enum / integer / boolean / negative checks: " + bad3.errors.join(" | "));
ok(!F.validatePaste("x".repeat(F.MAX_PASTE + 1)).ok && /Too large/.test(F.validatePaste("x".repeat(F.MAX_PASTE + 1)).errors[0]), "over 64 KB rejected before parsing");
const big = { workload_type: "training", evidence: Array.from({ length: 201 }, () => ({ field: "a", value: 1, source: "s", confidence: "high" })) };
ok(!F.validatePaste(JSON.stringify(big)).ok, "evidence over 200 items rejected");
ok(!F.validatePaste("{not json").ok && !F.validatePaste("[1,2]").ok && !F.validatePaste("   ").ok && !F.validatePaste(JSON.stringify({ requirements: [] })).ok, "invalid JSON / arrays / empty / non-object requirements rejected");
ok(F.validatePaste(JSON.stringify({ model: { name: "x".repeat(201) } })).errors.some(e => /longer than 200/.test(e)), "string length capped");
ok(F.checkRequirements({ workload_type: null, model: null, training: { method: null } }).ok, "explicit nulls are fine (unknown)");

// ---------- picks ----------
const picks = F.pickCards(REC);
eq(picks.map(p => p.label), ["Cheapest advertised option", "Best for your workload", "Best OpenGrid can run"], "three distinct pick labels, in order");
eq([picks[0].option && picks[0].option.option_id, picks[1].option && picks[1].option.option_id, picks[2].option], ["l40s-48gb:runpod:us-ks-2:1", "a100-80gb-sxm4:lambda:us-east-1:1", null], "picks resolve option refs");
ok(picks[2].empty && /MNIST/.test(picks[2].empty.text) && picks[2].empty.href === F.DEMO_HREF, "no executable pick explained: only the MNIST demo runs (links the demo)");
const anon = clone(REC); anon.options.forEach(o => { o.execution.workload_supported = true; o.execution.reasons = [{ code: "auth_required", reason: "sign in / API key required" }]; });
ok(/Sign in or use an API key/.test(F.pickCards(anon)[2].empty.text), "anonymous caller: sign in / API key, never assumed executable");
const none = clone(REC); none.picks = { cheapest_market: null, best_for_workload: null, best_executable: null }; none.options = [];
ok(F.pickCards(none).every(p => !p.option && p.empty && p.empty.text), "every empty pick carries an explanation");
const dangling = clone(REC); dangling.picks.best_for_workload = { option_id: "missing", reason: "x" };
ok(F.pickCards(dangling)[1].option === null && F.pickCards(dangling)[1].empty, "a pick pointing at no option renders as empty, not broken");

// ---------- readiness: stale / sold-out never deployable ----------
const byId = id => F.optionById(REC, id);
eq(F.readiness(byId("a100-80gb-sxm4:lambda:us-east-1:1")).code, "recommendation_only", "unsupported workload → recommendation only");
ok(/can't run this workload yet/.test(F.readiness(byId("a100-80gb-sxm4:lambda:us-east-1:1")).label), "honest label");
eq(F.readiness(byId("rtx-a6000-48gb:vastai:us:1")).code, "stale", "stale price → stale");
eq(F.readiness(byId("h100-80gb-sxm5:lambda:us-east-1:1")).code, "sold_out", "sold out → sold out");
const exe = clone(byId("a100-80gb-sxm4:lambda:us-east-1:1")); exe.execution = { executable: true, workload_supported: true, reasons: [] };
ok(F.readiness(exe).deployable && F.readiness(exe).code === "executable", "executable + fresh + available → deployable");
ok(!F.readiness(Object.assign(clone(exe), { fresh: false })).deployable, "executable but STALE → never deployable");
ok(!F.readiness(Object.assign(clone(exe), { available: false })).deployable, "executable but SOLD OUT → never deployable");
ok(F.readiness(Object.assign(clone(exe), { available: null })).deployable && /not reported/.test(F.readiness(Object.assign(clone(exe), { available: null })).detail), "unknown availability: deployable, said so");
ok(!F.readiness(Object.assign(clone(exe), { execution: {} })).deployable, "no executable flag → not deployable");
ok(REC.options.every(o => !F.readiness(o).deployable), "nothing in the fixture is labelled deployable");
eq([F.availability({ available: true }).label, F.availability({ available: false }).label, F.availability({ available: null }).label], ["Available", "Sold out", "Availability not reported"], "availability labels");

// ---------- memory fit ----------
const mf = F.memoryFit(byId("l40s-48gb:runpod:us-ks-2:1"), REC.estimate);
eq([mf.known, mf.needed, mf.vram, +mf.fill.toFixed(4), mf.headroom_pct, mf.fits, mf.over], [true, 29.2, 48, 0.6083, 39.2, true, false], "fit from memory_fit");
const mf2 = F.memoryFit({ vram_gb: 24 }, { memory: { per_gpu_gb: 29.2 } });
eq([mf2.fill, mf2.headroom_pct, mf2.fits, mf2.over], [1, -21.7, false, true], "no memory_fit: from the estimate; over VRAM → fill capped, does not fit");
eq(F.memoryFit({ vram_gb: 80 }, { memory: { per_gpu_gb: null } }).known, false, "no estimate → fit unknown (never invented)");
eq([F.fmtGb(29.2), F.fmtGb(80), F.fmtGb(141), F.fmtGb(null)], ["29.2 GB", "80 GB", "141 GB", "-"], "GB formatting");

// ---------- runtime: unknown is never a number ----------
const rt = F.runtimeInfo(REC.estimate);
ok(!rt.known && rt.text === "Unknown" && !/\d/.test(rt.text) && /benchmark/.test(rt.reason), "unknown runtime renders 'Unknown' with the reason");
ok(F.runtimeInfo({ runtime: { known: false, hours: 3.5 } }).text === "Unknown", "known=false with a stray number: still Unknown");
ok(F.runtimeInfo({ runtime: { known: true } }).text === "Unknown", "known=true without a measured number: Unknown");
eq(F.runtimeInfo({ runtime: { known: true, hours: 1.25, basis: "benchmark mnist-a100" } }).text, "1.25 h", "measured runtime shown");
eq(F.runtimeInfo(null).text, "Unknown", "no estimate → Unknown");

// ---------- estimate pieces, tiers, cost ----------
eq(F.componentRows(REC.estimate.memory).map(r => r.key), ["weights", "activations", "framework_overhead", "optimizer", "gradients"], "components largest first, zero parts dropped");
ok(Math.abs(F.componentRows(REC.estimate.memory).reduce((s, r) => s + r.share, 0) - 1) < 1e-9, "component shares sum to 1");
eq(F.tiersOf(REC.options).map(t => [t.tier, t.options.length]), [["lower_cost", 2], ["balanced", 1], ["higher_performance", 2]], "tiers in order");
eq(F.tiersOf([{ tier: "weird" }]).map(t => t.tier), ["other"], "unknown tier kept under Other");
const cl = F.costLines(byId("a100-80gb-sxm4:lambda:us-east-1:1"));
eq([cl.scenarios.length, cl.scenarios[0].assumption, cl.budget_hours, cl.unknown_costs], [2, "1 hour of runtime", 38.8, ["storage", "egress", "startup time"]], "cost scenarios carry assumptions + unknown costs");
eq(F.costLines({ cost: { scenarios: [{ hours: 1, total_usd: null }] } }).scenarios, [], "a scenario without a number is dropped, never shown as $0");
eq([F.routeHref(byId("a100-80gb-sxm4:lambda:us-east-1:1")), F.routeHref({ gpu_slug: "h100-80gb-sxm5", gpu_count: 2, execution: { route_preview_url: "javascript:alert(1)" } }), F.gpuHref({ gpu_slug: "a100-80gb-sxm4" })],
  ["/route?gpu=a100-80gb-sxm4&count=1", "/route?gpu=h100-80gb-sxm5&count=2", "/gpu/a100-80gb-sxm4"], "action links (only /route URLs from the server are used)");
eq(F.questionsOf(REC).map(q => q.field), ["training.micro_batch_size", "training.seq_len"], "questions de-duplicated across estimate + recommendation");

// ---------- needs_gpu = false ----------
ok(F.noGpu(NOGPU) && /CPU/.test(F.noGpu(NOGPU).reason), "needs_gpu=false → the 'no GPU' result with the reason");
ok(F.noGpu(REC) === null && F.noGpu({ estimate: { needs_gpu: null } }) === null, "needs_gpu true / null → normal result");
ok(F.pickCards(NOGPU).every(p => !p.option) && F.tiersOf(NOGPU.options).length === 0, "no options when no GPU is needed");

// ---------- job stages: sandbox ----------
const ev = (...s) => s.map(x => ({ to: x }));
const marks = sg => sg.steps.map(x => x.key + ":" + x.mark).join(" ");
const sbRun = J.stages({ status: "training", sandbox: true, events: ev("created", "validating", "initializing", "training") });
eq(sbRun.steps.map(x => x.key), ["preparing", "running", "saving", "complete"], "sandbox: 4 stages (no selecting / approval / starting / shutting down)");
eq(marks(sbRun), "preparing:done running:current saving:todo complete:todo", "sandbox training");
eq(sbRun.mode, "sandbox", "mode sandbox");
const sbDone = J.stages({ status: "training_completed", sandbox: true, outcome: "simulated", finished: true, events: ev("created", "validating", "initializing", "training", "saving_artifacts", "training_completed") });
eq(marks(sbDone), "preparing:done running:done saving:done complete:done", "sandbox finished: all done");
eq(sbDone.steps[3].label, "Complete (simulated)", "the simulated end says so");
const sbInit = J.stages({ status: "initializing", sandbox: true, events: ev("created", "validating", "initializing") });
eq(sbInit.current, "preparing", "sandbox initializing is still Preparing (no GPU to start)");
const sbFail = J.stages({ status: "training_failed", sandbox: true, outcome: "failed", finished: true, events: ev("created", "validating", "initializing", "training", "training_failed") });
eq(marks(sbFail), "preparing:done running:failed saving:skipped complete:done", "sandbox failure: running failed, saving skipped, ended");
eq(sbFail.steps[3].label, "Ended: failed", "the failed simulation says it ended failed");

// ---------- job stages: real ----------
const real = (status, hist, extra) => J.stages(Object.assign({ status, sandbox: false, events: ev(...hist) }, extra || {}));
const pa = real("pending_approval", ["created", "validating", "quoted", "pending_approval"]);
eq(pa.steps.length, 8, "real: 8 stages");
eq(marks(pa), "preparing:done selecting:done approval:current starting:todo running:todo saving:todo shutting_down:todo complete:todo", "real pending approval");
ok(pa.attention, "pending approval needs attention");
eq(real("provisioning", ["created", "validating", "quoted", "pending_approval", "approved", "provisioning"]).current, "starting", "provisioning → Starting GPU");
eq(real("initializing", ["created", "validating", "quoted", "pending_approval", "approved", "provisioning", "initializing"]).current, "starting", "real initializing → Starting GPU");
eq(real("training", ["created", "validating", "quoted", "pending_approval", "approved", "provisioning", "initializing", "training"]).current, "running", "training → Running");
eq(real("saving_artifacts", []).current, "saving", "saving_artifacts → Saving results");
eq(real("terminating", ["created", "quoted", "pending_approval", "approved", "provisioning", "initializing", "training", "saving_artifacts", "training_completed", "terminating"]).current, "shutting_down", "terminating → Shutting down");
const live = real("training", ["created", "validating", "quoted", "approved", "provisioning", "initializing", "training"]);
eq(live.steps.find(x => x.key === "approval").mark, "done", "approved without pending_approval: approval stage still passed (approved)");
const liveNoAppr = real("training", ["created", "validating", "quoted", "provisioning", "initializing", "training"]);
eq(liveNoAppr.steps.find(x => x.key === "approval").mark, "skipped", "LIVE launch with no approval state: approval skipped");
const recon = real("reconciled", ["created", "validating", "quoted", "pending_approval", "approved", "provisioning", "initializing", "training", "saving_artifacts", "training_completed", "terminating", "termination_confirmed", "reconciled"], { outcome: "succeeded", finished: true });
eq(marks(recon), "preparing:done selecting:done approval:done starting:done running:done saving:done shutting_down:done complete:done", "real succeeded + reconciled");
eq([recon.steps[7].label, recon.steps[7].tone], ["Complete", "good"], "complete, green");
const oom = real("reconciled", ["created", "validating", "quoted", "pending_approval", "approved", "provisioning", "initializing", "training", "out_of_memory", "terminating", "termination_confirmed", "reconciled"], { outcome: "failed", finished: true });
eq(marks(oom), "preparing:done selecting:done approval:done starting:done running:failed saving:skipped shutting_down:done complete:done", "workload failed: running failed, the GPU still shut down, then reconciled");
eq([oom.steps[7].label, oom.steps[7].tone, oom.failed_stage], ["Ended: failed", "bad", "running"], "a failed real job ends 'failed'");
const failing = real("training_failed", ["created", "quoted", "pending_approval", "approved", "provisioning", "initializing", "training", "training_failed"], { outcome: "failed" });
eq([failing.steps[4].mark, failing.steps[6].mark, failing.ended], ["failed", "todo", false], "just failed: shutting down still to come (not finished)");
const vf = real("validation_failed", ["created", "validating", "validation_failed"], { outcome: "failed", finished: true });
eq(marks(vf), "preparing:failed selecting:skipped approval:skipped starting:skipped running:skipped saving:skipped shutting_down:skipped complete:done", "validation failed: nothing after it ran");
const nep = real("no_eligible_provider", ["created", "validating", "no_eligible_provider"], { outcome: "failed", finished: true });
eq(nep.steps[1].mark, "failed", "no eligible provider fails at Selecting GPU");
const rej = real("approval_rejected", ["created", "validating", "quoted", "pending_approval", "approval_rejected"], { outcome: "cancelled", finished: true });
eq([rej.steps[2].mark, rej.steps[2].tone, rej.steps[7].label], ["failed", "warn", "Ended: cancelled"], "rejected / cancelled before launch: amber, ended cancelled");
const unc = real("provisioning_uncertain", ["created", "quoted", "pending_approval", "approved", "provisioning", "provisioning_uncertain"]);
eq([unc.current, unc.steps[3].tone, unc.attention], ["starting", "warn", true], "uncertain launch: Starting GPU, amber, attention");
const cred = real("credentials_unavailable", ["created", "quoted", "approved", "provisioning", "initializing", "training", "credentials_unavailable"]);
eq([cred.current, cred.steps[4].tone], ["running", "warn"], "credentials lost mid-run: stays at Running, amber (from history)");
const cxl = real("cancel_requested", ["created", "quoted", "approved", "provisioning", "initializing", "training", "cancel_requested"]);
eq(cxl.current, "shutting_down", "cancel after launch → Shutting down");
const cxlPre = real("cancel_requested", ["created", "validating", "quoted", "pending_approval", "approved", "cancel_requested"]);
eq(cxlPre.current, "starting", "cancel right after approval (nothing launched) stays where it was");
eq(real("quote_expired", ["created", "quoted", "pending_approval", "quote_expired"]).steps[2].tone, "warn", "quote expired: amber at approval");
eq(J.stages({ status: "training", sandbox: false }).steps.filter(x => x.mark === "done").length, 4, "no history: earlier stages assumed passed (none marked skipped)");
ok(Object.keys(J.STAGE_OF).every(s => ["created", "validating", "quoted", "pending_approval", "approved", "provisioning", "initializing", "training", "saving_artifacts", "training_completed", "terminating", "termination_confirmed", "reconciliation_pending", "reconciled",
  "validation_failed", "no_eligible_provider", "quote_expired", "approval_rejected", "provider_timeout", "provisioning_uncertain", "bootstrap_failed", "training_failed", "out_of_memory", "cancelling", "training_timed_out", "artifact_upload_failed", "termination_failed", "orphan_suspected"].includes(s)),
  "every mapped state exists in jobs/states.py");
const allStates = J.FLOW.concat(J.FAILED, J.UNCERTAIN, J.CANCEL);
ok(allStates.every(s => { const sg = J.stages({ status: s, sandbox: false }); return sg.steps.filter(x => x.mark === "current" || x.mark === "failed").length >= 1 || sg.ended; }), "every job state maps to a stage");

// ---------- plain status ----------
const ans1 = (list, key) => list.find(x => x.key === key);
const psSb = J.plainStatus({ status: "training", sandbox: true, outcome: null }, null, [], {});
eq([ans1(psSb, "money").a, ans1(psSb, "sim").a, ans1(psSb, "running").a, ans1(psSb, "gpu").a, ans1(psSb, "cost").a], ["No", "Yes", "Yes", "No, no GPU was ever used", "$0.00 (simulated)"], "sandbox: no money, simulated, running, no GPU, $0");
eq(psSb.map(x => x.q), ["Can real money be spent?", "Is this a simulation?", "Is it running?", "Is a GPU still active?", "Is action needed?", "Did it succeed?", "What outputs exist?", "What did it cost?"], "the eight beginner questions, in order");
const psSbDone = J.plainStatus({ status: "training_completed", sandbox: true, outcome: "simulated", finished: true }, null, [{ name: "model.pt" }, { name: "metrics.json" }]);
eq([ans1(psSbDone, "success").a, ans1(psSbDone, "outputs").a, ans1(psSbDone, "running").a], ["The simulation completed", "model.pt, metrics.json", "No, finished"], "sandbox done: completed (synthetic), outputs listed");
const psPa = J.plainStatus({ status: "pending_approval", sandbox: false, estimates: { est_max_total_usd: 1.5 } }, null, [], { admin: false });
eq([ans1(psPa, "money").a, ans1(psPa, "action").a, ans1(psPa, "cost").a], ["Not yet", "Not from you, waiting for OpenGrid approval", "Not known yet"], "pending approval (customer)");
eq(ans1(J.plainStatus({ status: "pending_approval", sandbox: false }, null, [], { admin: true }), "action").a, "Yes, approve or reject the quote", "pending approval (admin)");
const psRun = J.plainStatus({ status: "training", sandbox: false, resource: { disposition: "possibly_billing", possibly_billing: true } }, null, []);
eq([ans1(psRun, "money").tone, ans1(psRun, "gpu").a], ["bad", "Possibly"], "possibly billing is never played down");
const psTrain = J.plainStatus({ status: "training", sandbox: false, resource: { disposition: "none" } }, null, []);
eq([ans1(psTrain, "money").a, ans1(psTrain, "gpu").a, ans1(psTrain, "action").a], ["Yes", "Yes", "No"], "real run: money can be spent, GPU active");
const psRec = J.plainStatus({ status: "reconciled", sandbox: false, outcome: "succeeded", finished: true, actual_cost_usd: 0.42, resource: { disposition: "terminated_confirmed" } }, null, [{ name: "model.pt" }]);
eq([ans1(psRec, "money").a, ans1(psRec, "gpu").a, ans1(psRec, "success").a, ans1(psRec, "cost").a], ["Not any more", "No, terminated (provider confirmed)", "Yes", "$0.42"], "real reconciled");
const psOrph = J.plainStatus({ status: "orphan_suspected", sandbox: false }, null, []);
eq([ans1(psOrph, "action").tone, ans1(psOrph, "money").tone], ["bad", "bad"], "orphan suspected: action needed, money at risk");
const psVf = J.plainStatus({ status: "validation_failed", sandbox: false, outcome: "failed", finished: true, failure: { code: "validation_failed", reason: "epochs must be ≤ 10" } }, null, []);
eq([ans1(psVf, "money").a, ans1(psVf, "success").a, ans1(psVf, "success").detail, ans1(psVf, "cost").a], ["No", "No", "epochs must be ≤ 10", "$0.00 (nothing launched)"], "failed before launch");

// ---------- the new page is registered (server route checked by tests/test_pages.py) ----------
const src = fs.readFileSync(path.join(__dirname, "..", "web", "pages", "find.js"), "utf8");
ok(/OG\.page\("\/find"/.test(src), "find.js registers /find");
ok(fs.readFileSync(path.join(__dirname, "..", "web", "index.html"), "utf8").includes('<script src="/static/pages/find.js"></script>'), "index.html loads find.js");
ok(!/fetch\(|XMLHttpRequest|https?:\/\//.test(src.replace(/https:\/\/[^\s"']*huggingface[^\s"']*/g, "")), "find.js talks only to its own API through OG.api (CSP connect-src self)");

console.log(failures ? `\n${failures} FAILED` : "\nall passed");
process.exit(failures ? 1 : 0);
