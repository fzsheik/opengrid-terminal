// Reserved prices UI pure parts (web/pages/reserved.js -> OG.reservedPrices): terms become columns in bucket order,
// one line per offer, the saving wording, grouping by GPU (unmapped last) and the exact rule / empty-state copy.
// Run: node tests/test_web_reserved.js
const OG = require("../web/core.js");
global.OG = OG;
require("../web/pages/reserved.js");
const R = OG.reservedPrices;
let failures = 0;
const ok = (c, m) => { if (!c) { failures++; console.log("FAIL:", m); } else console.log("ok  :", m); };

const TERMS = [["lt1m", "Under 1 month"], ["1m", "1 month"], ["3m", "3 months"], ["6m", "6 months"], ["1y", "1 year"], ["2y", "2 years"], ["3y", "3 years"], ["5y", "5 years"]]
  .map(([key, label]) => ({ key, label }));
const row = (o) => Object.assign({ provider: "azure", gpu: "NVIDIA H100 80GB SXM5", raw_gpu_name: "x", gpu_count: 8, region: "eastus", sku: "Standard_ND96isr_H100_v5",
  scope: "instance", derived: false, term_bucket: "1y", term_label: "1 Year", price_per_gpu_hour: 7.87, on_demand_per_gpu_hour: 12.29, on_demand_match: "same_listing", saving_pct: 36.0, stale: false }, o);
const rows = [row({}), row({ term_bucket: "3y", term_label: "3 Years", price_per_gpu_hour: 5.4, saving_pct: 56.1 }),
  row({ term_bucket: "5y", term_label: "5 Years", price_per_gpu_hour: 4.92, saving_pct: 60.0 }),
  row({ provider: "verda", region: null, sku: "8H100.80S.176V", derived: true, term_bucket: "1y", price_per_gpu_hour: 4.4, on_demand_per_gpu_hour: 4.79, saving_pct: 8 }),
  row({ gpu: null, raw_gpu_name: "Mystery", provider: "gcp", scope: "gpu_only" })];

ok(R.termsIn(rows, TERMS).map(t => t.key).join() === "1y,3y,5y", "only terms present, in bucket order");
const lines = R.pivot(rows.slice(0, 4));
ok(lines.length === 2, "one line per offer (provider, size, region, sku)");
const az = lines.find(l => l.provider === "azure");
ok(Object.keys(az.cells).join() === "1y,3y,5y" && az.cells["3y"].price_per_gpu_hour === 5.4 && az.on_demand === 12.29, "terms as columns, on-demand kept");
ok(lines.find(l => l.provider === "verda").derived === true, "derived flag carried to the line");
const g = R.groupByGpu(rows);
ok(g.length === 2 && g[0][0] === "NVIDIA H100 80GB SXM5" && g[1][0] === "", "grouped by GPU, unmapped last");
ok(R.savingText(36) === "save 36.0%" && R.savingText(-1.5) === "1.5% more" && R.savingText(0) === "same as on-demand" && R.savingText(null) === null, "saving wording");
ok(R.RULE.includes("never enter on-demand prices, indices, spreads or market statistics"), "the hard rule is on the page");
ok(/No reserved prices are stored on this server yet/.test(R.EMPTY) && R.FILTERED === "No reserved prices match these filters.", "honest empty states");
const src = require("fs").readFileSync(__dirname + "/../web/pages/reserved.js", "utf8");
ok(!/[–—]/.test(src), "no en or em dashes");

if (failures) { console.log(failures + " failed"); process.exit(1); }
console.log("all passed");
