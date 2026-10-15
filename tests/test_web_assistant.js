// Assistant panel pure parts (web/pages/assistant.js -> OG.assistant): which renderer a card gets, the three picks
// of a recommendation, observed / estimated labels, links (/gpu, /route, /find with requirements), price / compare
// / context rows (family variants never merged), quick-reply buttons, the link guard and the context bound.
// Shapes follow api/assistant.py and docs/OPENGRID2_CONTRACTS.md §3 / §5.
// Run: node tests/test_web_assistant.js
const OG = require("../web/core.js");
global.OG = OG;
require("../web/pages/assistant.js");
const A = OG.assistant;
let failures = 0;
const ok = (c, m) => { if (!c) { failures++; console.log("FAIL:", m); } else console.log("ok  :", m); };
const eq = (a, b, m) => ok(JSON.stringify(a) === JSON.stringify(b), m + " -> " + JSON.stringify(a));

// ---------- card kinds ----------
eq(["recommendation", "gpu_prices", "compare", "specs", "market_context", "questions", "link", "launch", null].map(t => A.cardKind(t ? { type: t, data: {} } : null)),
  ["recommendation", "prices", "compare", "specs", "context", "questions", "link", "unknown", "unknown"], "card type -> renderer (unknown types render nothing)");
eq(["observed_market_price", "observed", "estimated", "inferred", null].map(A.kindOf), ["observed", "observed", "estimated", "inferred", "observed"], "data-kind badges");

// ---------- recommendation ----------
const opt = (id, gpu, slug, price, extra) => Object.assign({ option_id: id, gpu, gpu_slug: slug, gpu_count: 1, provider: "syn_a", provider_display: "Syn A",
  price_per_gpu_hour: price, price_kind: "observed", fresh: true, memory_fit: { fits: true, headroom_pct: 50 },
  execution: { executable: false, route_preview_url: `/route?gpu=${slug}&count=1` } }, extra || {});
const rec = {
  estimate: { needs_gpu: true, memory: { recommended_gb: 29.2, range_gb: [21, 34], confidence: "medium", assumptions: ["a", "b", "c", "d", "e"] }, gpu_count: { suggested: 1 } },
  picks: { cheapest_market: { option_id: "o1", reason: "cheapest" }, best_for_workload: { option_id: "o2", reason: "headroom" }, best_executable: null },
  options: [opt("o1", "NVIDIA L40S 48GB", "l40s-48gb", 0.87), opt("o2", "NVIDIA H100 80GB SXM5", "h100-80gb-sxm5", 2.49), opt("o3", "x", "x", 3), opt("o4", "y", "y", 4), opt("o5", "z", "z", 5), opt("o6", "w", "w", 6)],
  excluded: [{ code: "insufficient_vram" }], questions: [{ field: "training.method", question: "LoRA?" }], notes: ["Prices are observed list prices, not quotes."],
};
const v = A.recView(rec);
eq(v.picks.map(p => [p.key, p.option && p.option.option_id]), [["cheapest_market", "o1"], ["best_for_workload", "o2"], ["best_executable", null]], "three picks resolved by option_id; a null pick stays empty");
ok(v.memory.gb === 29.2 && v.memory.kind === "estimated" && v.memory.assumptions.length === 4, "memory is labelled estimated; assumptions capped");
ok(v.top.length === 5 && v.excluded === 1, "top 5 options, exclusions counted");
ok(A.recView({ estimate: { needs_gpu: false, needs_gpu_reason: "no ML framework" } }).needsGpu === false, "needs_gpu=false is passed through");
ok(A.recView(null).picks.every(p => p.option === null), "an empty recommendation renders empty picks, never invented ones");
const pickRef = A.recView({ picks: { cheapest_market: { option_id: "missing" } }, options: [] });
ok(pickRef.picks[0].option === null, "a pick pointing at an unknown option is not shown");

// ---------- links ----------
eq(A.optionLinks(rec.options[0]), [{ label: "Market", href: "/gpu/l40s-48gb" }, { label: "Route preview", href: "/route?gpu=l40s-48gb&count=1" }], "option links: market + route preview");
eq(A.optionLinks({ gpu: "NVIDIA H100 80GB SXM5", gpu_count: 2, execution: { route_preview_url: "javascript:alert(1)" } }).map(l => l.href),
  ["/gpu/h100-80gb-sxm5", "/route?gpu=h100-80gb-sxm5&count=2"], "a non-relative preview URL is replaced by our own route link");
ok(A.findHref(null) === "/find" && A.findHref({}) === "/find", "find link without requirements");
const fh = A.findHref({ workload_type: "fine_tuning", model: { parameters_b: 8 } });
ok(fh.startsWith("/find?mode=paste&req=") && JSON.parse(decodeURIComponent(fh.split("req=")[1])).model.parameters_b === 8, "find link carries the requirements");
ok(A.findHref({ model: { name: "x".repeat(3000) } }) === "/find", "oversized requirements are not put in a URL");
eq(["/gpu/h100", "//evil.com/x", "https://evil.com", "javascript:alert(1)", "/a b", null, 5].map(A.safeHref), ["/gpu/h100", null, null, null, null, null, null], "only same-site relative links");

// ---------- prices / compare / context ----------
const priceCard = { gpu: "NVIDIA L40S 48GB", slug: "l40s-48gb", cheapest: [{ provider: "syn_a", provider_display: "Syn A", region: "us-east", price_per_gpu_hour: 0.8, price_kind: "observed_market_price", available: true },
  { provider: "syn_b", price_per_gpu_hour: 0.9, price_kind: "observed_market_price", available: null, country: "DE" }], market: {} };
eq(A.priceRows(priceCard).map(r => [r.provider, r.region, r.price, r.kind, r.available]), [["Syn A", "us-east", 0.8, "observed", "yes"], ["syn_b", "DE", 0.9, "observed", "not reported"]], "price rows: observed, unknown availability said so");
const fam = { family: "H100", variants: [Object.assign({}, priceCard, { gpu: "NVIDIA H100 80GB PCIe", slug: "h100-80gb-pcie" }), { gpu: "NVIDIA H100 80GB SXM5", slug: "h100-80gb-sxm5", cheapest: [] }] };
const fr = A.priceRows(fam);
ok(fr.length === 2 && fr.every(r => r.gpu === "NVIDIA H100 80GB PCIe"), "family: rows stay per variant (a variant without listings adds none)");
eq(A.compareRows({ items: [{ gpu: "A", slug: "a", hardware: { vram_gb: 80, memory_bandwidth_tbps: 3.35, bf16_tflops_dense: 989.5 }, market: { low: 1.3, median: 4.3, providers: 16 } }] })[0],
  { gpu: "A", slug: "a", vram: 80, mem: null, bw: 3.35, bf16: 989.5, fp8: undefined, form: null, low: 1.3, median: 4.3, providers: 16 }, "compare rows");
eq(A.compareRows(null), [], "no compare data, no rows");
const cr = A.contextRows({ items: [{ gpu: "G", slug: "g", summaries: ["s"], moves: [1, 2, 3, 4, 5, 6, 7, 8].map(i => ({ at: "t" + i })), move_threshold_pct: 3 }] });
ok(cr[0].moves.length === 6 && cr[0].moves[5].at === "t8" && cr[0].threshold === 3, "context: the latest 6 moves");

// ---------- questions ----------
const qs = A.questionButtons([{ field: "training.method", question: "Full, LoRA or QLoRA?", why: "memory", options: [{ label: "LoRA", message: "LoRA" }, { label: "bad" }, { label: "QLoRA", message: "QLoRA" }] },
  { field: "training.dataset_tokens", question: "How large is your dataset?" }, { nope: 1 }, null]);
ok(qs.length === 2 && qs[0].buttons.map(b => b.label).join() === "LoRA,QLoRA", "quick replies: options with a message only; question-less entries dropped");
ok(qs[1].buttons.length === 0 && qs[1].question === "How large is your dataset?", "an option-less question shows its text");
ok(A.questionButtons([{ question: "q", options: [{ label: "x", message: "y".repeat(5000) }] }])[0].buttons[0].message.length === A.MAX_MESSAGE, "a quick reply never exceeds the message bound");

// ---------- context bound ----------
ok(A.boundContext(null) === null && A.boundContext("x") === null, "no context");
const small = { requirements: { workload_type: "inference" }, pending: "recommend", asked: [] };
ok(A.boundContext(small) === small, "a normal context is echoed unchanged");
ok(A.boundContext({ requirements: { model: { name: "x".repeat(9000) } } }) === null, "an oversized context is dropped (fresh start), never sent");
ok(A.EXAMPLES.length >= 6 && A.EXAMPLES.includes("Why did H200 prices change?"), "example chips");

// ---------- job actions: only known actions render; cancel needs a real job id; hrefs rebuilt from the id ----------
eq(["action", "jobs", "job"].map(t => A.cardKind({ type: t, data: {} })), ["action", "jobs", "job"], "job card kinds");
ok(A.actionView({ action: "start_demo", label: "Start free simulation", epochs: 3, note: "Simulated" }).action === "start_demo", "start_demo action");
ok(A.actionView({ action: "start_demo", epochs: 500 }).epochs === 3 && A.actionView({ action: "start_demo", epochs: "2" }).epochs === 3, "bad epochs fall back to 3");
ok(A.actionView({ action: "cancel_job", job_id: "job-abc12345" }).jobId === "job-abc12345", "cancel_job with a job id");
ok(A.actionView({ action: "cancel_job", job_id: "../../v1/admin" }) === null && A.actionView({ action: "cancel_job" }) === null, "cancel_job without a valid id renders nothing");
ok(A.actionView({ action: "launch_gpu" }) === null && A.actionView({ action: "approve" }) === null && A.actionView(null) === null, "unknown actions render nothing");
const items = A.jobItems({ items: [{ job_id: "job-0123abcd", name: "a", status: "training", status_text: "Running, epoch 2 of 3", sandbox: true, progress: { epoch: 2, epochs: 3 }, cost: { est_max_usd: null, actual_usd: null }, href: "javascript:alert(1)", cancellable: true },
  { job_id: "bad id", name: "x" }] });
ok(items.length === 1 && items[0].href === "/jobs/job-0123abcd" && items[0].text === "Running, epoch 2 of 3" && items[0].progress.epoch === 2 && items[0].cancellable, "job items: invalid ids dropped, href rebuilt (never the server's)");
ok(A.jobItems({ job_id: "job-0123abcd", status: "pending_approval" }, true)[0].text === "pending approval", "single job card; status text falls back to the state");

if (failures) { console.log(`\n${failures} FAILED`); process.exit(1); }
console.log("\nall assistant UI checks passed");
