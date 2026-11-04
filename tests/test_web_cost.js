// True cost UI pure parts (web/pages/cost.js -> OG.trueCost): form validation and the request body (presets, TB),
// totals as "≥ $X" when a component is unknown, unknown cells never drawn as $0, % above compute, the network and
// terms badges, and the rule / empty-state copy. Run: node tests/test_web_cost.js
const OG = require("../web/core.js");
global.OG = OG;
require("../web/pages/cost.js");
const C = OG.trueCost;
let failures = 0;
const ok = (c, m) => { if (!c) { failures++; console.log("FAIL:", m); } else console.log("ok  :", m); };

// form -> body
const good = C.buildBody(C.DEFAULTS);
ok(!good.errors && good.body.gpu === "NVIDIA H100 80GB SXM5" && good.body.count === 8 && good.body.hours === 72
  && good.body.egress_tb === 10 && good.body.storage_tb === 20 && good.body.storage_class === "auto" && !("min_fabric_gbps" in good.body),
  "defaults are R1-shaped, TB sent as TB, no fabric minimum unless asked");
ok(JSON.stringify(C.buildBody(Object.assign({}, C.DEFAULTS, { preset: "R3", count: "zz" }))) === JSON.stringify({ body: { preset: "R3" } }),
  "a preset sends only the preset (other inputs ignored)");
const bad = C.buildBody(Object.assign({}, C.DEFAULTS, { gpu: "", count: "2.5", hours: "0", egress_tb: "-1", storage_tb: "abc" }));
ok(bad.errors && ["gpu", "count", "hours", "egress_tb", "storage_tb"].every(k => bad.errors[k]) && !bad.body, "every bad field named, nothing sent");
ok(C.buildBody(Object.assign({}, C.DEFAULTS, { egress_tb: "", storage_tb: "" })).body.egress_tb === 0, "blank TB means 0");
ok(C.buildBody(Object.assign({}, C.DEFAULTS, { min_fabric_gbps: "3200" })).body.min_fabric_gbps === 3200, "fabric minimum as a number");
ok(C.buildBody(Object.assign({}, C.DEFAULTS, { count: "1025" })).errors.count, "count capped like the API (1,024)");

// rows from POST /v1/costs/estimate
const row = o => Object.assign({ provider: "aws", sku: "p5.48xlarge", total: 5011.69, total_is_lower_bound: false, unknown: [],
  extras_vs_compute_pct: 26.5, effective_per_gpu_hour: 8.700851,
  components: { compute: { amount: 3962.88, billed_hours: 72, exact: true, price_per_instance_hour: 55.04 },
    egress_internet: { amount: 891, known: true, product: "Data Transfer Out", origin: "api" },
    storage: { amount: 157.81, known: true, class: "block_storage", product: "EBS gp3", origin: "manual", as_of: "2026-11-01" } },
  specs: { node_fabric: "efa", node_fabric_gbps: 3200, multi_node: true, billing_increment_s: 1, billing_minimum_s: 60 } }, o);
const aws = row({});
const verda = row({ provider: "verda", total: 3155.27, total_is_lower_bound: true, unknown: ["egress_internet"], extras_vs_compute_pct: 14.3,
  components: { compute: { amount: 2760.48, billed_hours: 72, price_per_instance_hour: 38.34 }, egress_internet: { amount: null, known: false },
    storage: { amount: 394.52, known: true, class: "block_storage", origin: "api" } },
  specs: { node_fabric: null, billing_increment_s: 600, billing_minimum_s: 600 } });
ok(C.totalText(aws) === "$5,011.69" && C.totalText(verda) === "≥ $3,155.27" && C.totalText(row({ total: null })) === null, "total, lower bound with ≥, no total");
ok(C.extraText(aws) === "+26.5%" && C.extraText(verda) === "≥ +14.3%" && C.extraText(row({ extras_vs_compute_pct: null })) === null, "% above compute");
ok(C.unknownText(verda) === "Not published, not counted: egress" && C.unknownText(aws) === null, "unknown components named");
const unk = C.partView(verda, "egress_internet");
ok(unk.text === "unknown" && unk.tone === "warn" && /lower bound/.test(unk.title), "an unknown component is the word 'unknown', never $0");
ok(C.partView(aws, "egress_internet").text === "$891.00", "a known component in dollars");
ok(C.partView(row({ components: { compute: {}, egress_internet: { amount: 0, known: true } } }), "egress_internet").tone === "good", "free egress marked good");
ok(C.partView(row({ components: { compute: {} } }), "storage").text === "-", "no storage in the job: a dash, not unknown");
ok(/hand table, checked 2026-11-01/.test(C.partView(aws, "storage").title), "hand-table values say when they were checked");
ok(/billed per second/.test(C.computeTitle(aws)) && /10-minute steps/.test(C.computeTitle(verda)), "billing increment in the compute tooltip");
ok(/minimum 1 h/.test(C.computeTitle(row({ specs: { billing_increment_s: 1, billing_minimum_s: 3600 } }))), "billing minimum shown");
ok(/not published/.test(C.computeTitle(row({ specs: {} }))), "unpublished increment said so");

// badges
const b = (specs, n) => C.badges(specs, n).map(x => x.text);
ok(b(aws.specs).join() === "EFA 3.2 Tbps" && C.badges(aws.specs)[0].tone === "good", "multi-node fabric badge");
ok(b({ node_fabric: "ethernet", node_fabric_gbps: 25, multi_node: false, fabric_notes: "no RDMA fabric" }).join() === "Ethernet 25 Gbps"
  && /no RDMA/.test(C.badges({ node_fabric: "ethernet", node_fabric_gbps: 25, fabric_notes: "no RDMA fabric" })[0].title), "Ethernet node network, with the note");
ok(b({}, 8).join() === "fabric n/a", "unknown fabric said, not hidden");
ok(b({ node_fabric: "infiniband", node_fabric_gbps: 3200, on_demand_deployable: false }).includes("contract only"), "not deployable on demand");
ok(b({ gpu_link: "pcie" }).includes("PCIe only") && b({ billing_minimum_s: 3600 }).includes("1 h min"), "PCIe and hour-minimum badges");

ok(b({}, 1).length === 0 && b({}, 8).join() === "fabric n/a", "no fabric badge for one GPU; said for a multi-GPU machine");
ok(C.hoursText(1 / 6) === "10 min" && C.hoursText(72) === "72 h" && C.hoursText(1.5) === "1.5 h" && C.hoursText(730) === "730 h", "hours wording");

// copy
ok(/never part of listings, indices, spreads, market statistics or routing/.test(C.RULE) && /not quotes/.test(C.RULE), "the rule is on the page");
ok(/No current on-demand listing of H100 SXM5 with exactly this many GPUs per machine/.test(C.EMPTY("H100 SXM5")), "honest empty state");
ok(C.PRESETS.join() === "R0,R1,R2,R3" && C.presetLabel({ preset: "R1", label: "Training run" }) === "R1: Training run", "presets");
const src = require("fs").readFileSync(__dirname + "/../web/pages/cost.js", "utf8");
ok(!/[–—]/.test(src), "no en or em dashes");
ok(/\/methodology\/full-cost/.test(src) && /"\/v1\/costs\/estimate"/.test(src), "links the methodology and calls the estimator");
const html = require("fs").readFileSync(__dirname + "/../web/index.html", "utf8");
ok(/<a href="\/cost" data-nav="cost"[^>]*>True cost<\/a>/.test(html) && html.includes('src="/static/pages/cost.js"'), "in the GPUs nav group, script loaded");

if (failures) { console.log(failures + " failed"); process.exit(1); }
console.log("all passed");
