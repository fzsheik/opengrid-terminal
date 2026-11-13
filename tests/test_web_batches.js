// Batches UI pure parts (web/pages/batches.js -> OG.batches): the sweep form -> the POST /v1/batches body (the shape
// jobs/batches.py validates), value lists, the counts line; no en / em dashes in the page source.
// Run: node tests/test_web_batches.js
const fs = require("fs");
const path = require("path");
const OG = require("../web/core.js");
global.OG = OG;
require("../web/pages/batches.js");
const Bt = OG.batches;
let failures = 0;
const ok = (c, m) => { if (!c) { failures++; console.log("FAIL:", m); } else console.log("ok  :", m); };
const eq = (a, b, m) => ok(JSON.stringify(a) === JSON.stringify(b), m + " -> " + JSON.stringify(a));

eq(Bt.numList("0.001, 0.003  0.01", "lr"), [0.001, 0.003, 0.01], "comma / space separated values");
eq(Bt.numList("   ", "lr"), null, "empty list -> not swept");
let threw = null;
try { Bt.numList("0.1, abc", "learning rates"); } catch (e) { threw = e.message; }
ok(threw && threw.includes("learning rates") && threw.includes("abc"), "a non-number names the field: " + threw);

const b = Bt.bodyFrom({ name: " lr-sweep ", learning_rate: "0.001, 0.01", batch_size: "", epochs: "1 3", budget: "12",
  max_running: "2", share: "0.5", on_failure: "stop_on_first_failure", max_attempts: "2", sandbox: true });
eq(b.sweep.grid, { learning_rate: [0.001, 0.01], epochs: [1, 3] }, "only the filled lists are swept");
ok(b.name === "lr-sweep" && b.template === "pytorch-mnist" && b.job_type === "training", "template + trimmed name");
ok(b.budget.max_total_usd === 12 && b.concurrency.max_running === 2 && b.placement.max_share_per_provider === 0.5, "budget / concurrency / cap");
ok(b.policy.on_failure === "stop_on_first_failure" && b.policy.max_attempts === 2, "failure policy + attempts");
ok(b.sandbox === true && b.limits.max_runtime_minutes === 30 && b.limits.max_price_per_gpu_hour_usd === 3, "sandbox by default, template limits");
ok(!("shards" in b) && !("max_total_cost_usd" in b.limits), "a sweep, never both; the per-shard limit is the server's default");
ok(Bt.bodyFrom({ learning_rate: "0.001", sandbox: false }).sandbox === false, "a real run only when unticked");
threw = null;
try { Bt.bodyFrom({ learning_rate: "", batch_size: "", epochs: "" }); } catch (e) { threw = e.message; }
ok(threw && /at least one/.test(threw), "nothing to sweep is refused before any request");

ok(Bt.countsText({ succeeded: 3, failed: 1, queued: 0 }) === "3 succeeded · 1 failed", "counts line skips zeros");
ok(Bt.countsText({ pending_approval: 2 }) === "2 waiting for approval", "plain words for states");
ok(Bt.countsText({}) === "no shards", "empty counts");
ok(Bt.isDone({ finished: true }) && !Bt.isDone({ finished: false }) && !Bt.isDone(null), "finished");

const src = fs.readFileSync(path.join(__dirname, "..", "web", "pages", "batches.js"), "utf8");
ok(!/[–—]/.test(src), "no en or em dashes in batches.js");
ok(src.includes('"/v1/batches/preview"') && src.includes("OG.api.intent(intent, \"/v1/batches\""), "preview, then one Idempotency-Key per submit intent");

console.log(failures ? `${failures} FAILED` : "all passed");
process.exit(failures ? 1 : 0);
