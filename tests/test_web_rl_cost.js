// RL cost UI pure parts (web/pages/rl_cost.js -> OG.rlCost): presets into the form, the request body and its bounds,
// totals as "≥ $X" for lower bounds, unknown lines never drawn as $0, saving wording with its honesty bound, line
// descriptions, and the sensitivity grid. Run: node tests/test_web_rl_cost.js
const OG = require("../web/core.js");
global.OG = OG;
require("../web/pages/rl_cost.js");
const C = OG.rlCost;
let failures = 0;
const ok = (c, m) => { if (!c) { failures++; console.log("FAIL:", m); } else console.log("ok  :", m); };

// form -> body
const good = C.buildBody(C.DEFAULTS);
ok(!good.errors && good.body.trainer.count === 8 && good.body.trainer.nodes === 1 && good.body.rollouts.count === 32
  && good.body.rollouts.gpu === "auto" && good.body.rollouts.min_vram_gb === 80 && good.body.weights_gb === 140
  && good.body.syncs_per_hour === 4 && good.body.hours === 72 && good.body.storage_gb === 2000 && !("sync_fanout" in good.body)
  && !("sandboxes" in good.body), "defaults are RL1; TB sent as GB; blank fanout means the server default");
const bad = C.buildBody(Object.assign({}, C.DEFAULTS, { trainer_gpu: "", trainer_count: "2.5", rollout_count: "0", weights_gb: "", hours: "-1", overhead: "0.5", sync_fanout: "0" }));
ok(bad.errors && ["trainer_gpu", "trainer_count", "rollout_count", "weights_gb", "hours", "overhead", "sync_fanout"].every(k => bad.errors[k]) && !bad.body, "every bad field named, nothing sent");
const sb = C.buildBody(Object.assign({}, C.DEFAULTS, { sb_count: "8", sb_vcpu: "2", sb_ram_gb: "4", sb_price: "1.5", sync_fanout: "3", interruptible: true }));
ok(sb.body.sandboxes.count === 8 && sb.body.sandboxes.price_per_hour === 1.5 && sb.body.sync_fanout === 3 && sb.body.rollouts.interruptible === true, "sandboxes, fanout, spot");
ok(C.buildBody(Object.assign({}, C.DEFAULTS, { sb_count: "8" })).errors.sb_vcpu, "a sandbox needs vCPU or RAM");
ok(C.buildBody(Object.assign({}, C.DEFAULTS, { rollout_count: "4097" })).errors.rollout_count, "rollout count capped like the API (4,096)");

// presets from GET /v1/costs/rl-presets (public ids with gpu_name beside them)
const st = C.fromPreset({ preset: "RL2", trainer: { gpu: "nvidia-h100-sxm5-80gb", gpu_name: "NVIDIA H100 80GB SXM5", count: 8, nodes: 1 },
  rollouts: { gpu: "auto", count: 128, min_vram_gb: 48, interruptible: true, interruption_overhead: 1.15 }, weights_gb: 140, syncs_per_hour: 4,
  sync_fanout: null, hours: 72, storage_gb: 2000, sandboxes: null, same_cluster_only: false });
ok(st.trainer_gpu === "NVIDIA H100 80GB SXM5" && st.rollout_gpu === "auto" && st.rollout_count === "128" && st.interruptible === true
  && st.storage_tb === "2" && st.sync_fanout === "" && st.sb_count === "", "a preset fills the form with names, not ids");
const back = C.buildBody(st).body;
ok(back.rollouts.count === 128 && back.rollouts.interruption_overhead === 1.15 && back.storage_gb === 2000, "a preset round-trips into the same job");

// totals and savings
ok(C.totalText({ total: 630, total_is_lower_bound: false }) === "$630.00" && C.totalText({ total: 521, total_is_lower_bound: true }) === "≥ $521.00"
  && C.totalText(null) === null, "total, lower bound with ≥");
ok(C.savingText({ saving: 847.2, saving_pct: 57.4, bound: "exact" }) === "$847.20 saved (57.4%)", "exact saving");
ok(C.savingText({ saving: 629.2, saving_pct: 54.7, bound: "at_most" }) === "at most $629.20 saved (54.7%)", "split is a lower bound: at most");
ok(C.savingText({ saving: -100, saving_pct: -5, bound: "exact" }) === "$100.00 more (5.0%)", "splitting costs more");
ok(/both totals are lower bounds/.test(C.savingText({ saving: 1, saving_pct: 1, bound: "indeterminate" })) && C.savingText(null) === null, "indeterminate, none");
ok(/egress \(.*\), ingress \(.*\), sandboxes/.test(C.unknownText({ unknown: ["sync_egress:verda", "sync_ingress:aws", "sandbox"] })) && C.unknownText({ unknown: [] }) === null, "unknowns named");

// lines
const unk = C.lineAmount({ item: "sync_egress", amount: null, known: false, notes: ["verda publishes no checked internet egress price"] });
ok(unk.text === "unknown" && unk.tone === "warn" && /lower bound/.test(unk.title), "an unknown line is the word 'unknown', never $0");
ok(C.lineAmount({ amount: 0, known: true, kind: "assumption" }).tone === "dim" && C.lineAmount({ amount: 0, known: true, kind: "manual" }).tone === "good", "assumed $0 is not shown as a free price");
ok(C.lineAmount({ amount: 137.6, known: true, kind: "observed" }).text === "$137.60", "a known line in dollars");
ok(C.lineWhat({ item: "rollout_compute", gpus: 16, machines: 16, gpu_name: "NVIDIA L40S 48GB", interruptible: true }).includes("16×") && /interruptible/.test(C.lineWhat({ item: "rollout_compute", gpus: 1, machines: 1, interruptible: true })), "rollout line says GPUs, machines and spot");
ok(C.lineWhat({ item: "sync_egress", gb: 40320 }) === "40,320 GB" && C.lineRate({ unit_price: 0.4, billed_hours: 10, interruption_overhead: 1.15 }) === "10 h × $0.40/h × 1.15", "sync volume and rate wording");

// sensitivity grid
const g = C.sensGrid({ interruption_overheads: [1, 1.15], syncs_per_hour: [1, 4], rows: [
  { interruption_overhead: 1, syncs_per_hour: 1, saving: 900, saving_bound: "exact", same_cluster_total: 1500, split_total: 600 },
  { interruption_overhead: 1, syncs_per_hour: 4, saving: -50, saving_bound: "exact" },
  { interruption_overhead: 1.15, syncs_per_hour: 1, saving: 800, saving_bound: "at_most", current: true },
  { interruption_overhead: 1.15, syncs_per_hour: 4, saving: null }] });
ok(g.length === 2 && g[0].cells[0].text === "$900" && g[0].cells[0].tone === "up" && g[0].cells[1].text === "−$50" && g[0].cells[1].tone === "down", "grid cells: saving and cost more");
ok(g[1].cells[0].text === "~$800" && g[1].cells[0].current && g[1].cells[1].text === "n/a", "lower-bound cells marked ~, the current job marked, missing n/a");
ok(C.sensGrid(null).length === 0, "no sensitivity -> empty grid");
ok(!/40.?55/.test(C.RULE) && /not benchmarked/.test(C.RULE), "rule copy: honest, no outside RL share figure");

if (failures) { console.log(`\n${failures} FAILED`); process.exit(1); }
console.log("\nall rl cost UI tests passed");
