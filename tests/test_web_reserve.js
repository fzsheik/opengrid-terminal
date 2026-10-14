// Reserve UI pure parts (web/pages/reserve.js → OG.reserve): status tones and strip (reservations/states.py),
// form validation mirroring reservations/service.validate, the indicative reference rows (never a quote),
// money / time formatting and the exact banner + acceptance copy.
// Run: node tests/test_web_reserve.js
const OG = require("../web/core.js");
global.OG = OG;
require("../web/pages/reserve.js");
const R = OG.reserve;
let failures = 0;
const ok = (c, m) => { if (!c) { failures++; console.log("FAIL:", m); } else console.log("ok  :", m); };

// ---------- states ----------
ok(R.FLOW.join(",") === "requested,under_review,offered,accepted,fulfilled", "main flow");
ok(R.TERMINAL.join(",") === "declined,expired,withdrawn,fulfilled,cancelled", "terminal states = reservations/states.py");
ok(R.tone("accepted") === "good" && R.tone("offered") === "busy" && R.tone("declined") === "bad" && R.tone("expired") === "warn", "tones");
const s1 = R.strip("offered", ["requested", "under_review", "offered"]);
ok(s1.filter(x => x.mark === "done").length === 2 && s1[2].mark === "current", "offered: two done, offered current");
const s2 = R.strip("declined", ["requested", "under_review", "declined"]);
const di = s2.findIndex(x => x.state === "declined");
ok(di === 2 && s2[di].off && s2[di].tone === "bad", "declined inserted after under_review, red");
const s3 = R.strip("withdrawn", ["requested", "withdrawn"]);
ok(s3.findIndex(x => x.state === "withdrawn") === 1 && s3[1].tone === "warn", "withdrawn straight from requested");
ok(R.isTerminal({ status: "expired" }) && !R.isTerminal({ status: "accepted" }) && R.isTerminal({ status: "x", terminal: true }), "terminal");

// ---------- validation ----------
const now = Date.parse("2026-10-13T12:00:00Z");
const base = Object.assign(R.defaults(now), { gpu: "h100-80gb-sxm5" });
let v = R.validate(base, now);
ok(v.ok && v.body.gpu_count === 8 && v.body.total_gpu_hours === 5376 && v.body.max_price_per_gpu_hour === 2.5, "defaults validate: " + JSON.stringify(v.errors));
ok(/Z$/.test(v.body.start_after) && /Z$/.test(v.body.start_before), "times sent as ISO with a zone");
ok(!("region_group" in v.body) && !("notes" in v.body), "empty optional fields are omitted");
const bad = (patch, field, m) => { const r = R.validate(Object.assign({}, base, patch), now); ok(!r.ok && r.errors[field], m + " -> " + (r.errors[field] || "no error")); };
bad({ gpu: "" }, "gpu", "gpu required");
bad({ gpu_count: "2.5" }, "gpu_count", "whole GPUs");
bad({ gpu_count: 2000 }, "gpu_count", "≤ 1024 GPUs");
bad({ total_gpu_hours: 4 }, "total_gpu_hours", "≥ one hour per GPU");
bad({ total_gpu_hours: 10.123 }, "total_gpu_hours", "2 decimals");
bad({ max_price_per_gpu_hour: 0 }, "max_price_per_gpu_hour", "limit > 0");
bad({ max_price_per_gpu_hour: "" }, "max_price_per_gpu_hour", "limit required");
bad({ start_after: R.isoToLocal("2026-10-01T00:00:00Z") }, "start_after", "start in the past");
bad({ start_before: R.isoToLocal(new Date(Date.parse(R.localToIso(base.start_after)) + 1800e3).toISOString()) }, "start_before", "window ≥ 1 hour");
bad({ notes: "x".repeat(2001) }, "notes", "notes length");
v = R.validate(Object.assign({}, base, { region_group: "Europe", notes: "  NVLink  " }), now);
ok(v.body.region_group === "Europe" && v.body.notes === "NVLink", "region + trimmed notes");

// ---------- the indicative reference ----------
const ref = { label: "indicative", is_quote: false, index: { index_id: "h100-80gb-sxm5", level: 2.31, raw_level: 2.29, link: "/indices/h100-80gb-sxm5", hour: "2026-10-13T11:00:00+00:00" },
  best_on_demand: { listings: [{ provider: "runpod", price_per_gpu_hour: 2.19 }] }, settlement_fixing: { available: false, reason: "settlement fixings are not available on this server" } };
const rows = R.refRows(ref);
ok(rows.length === 3 && rows.every(r => r.label_kind === "indicative"), "every reference row labelled indicative");
ok(rows[0].value === 2.31 && rows[1].value === 2.19 && rows[1].provider === "runpod", "index + best listing");
ok(rows[2].value === null && /not available/.test(rows[2].reason), "missing fixing: null with the reason, never invented");
ok(!JSON.stringify(rows).toLowerCase().includes("quote"), "no row calls anything a quote");
ok(R.refRows({ index: { level: null, reason: "no eligible constituents" }, best_on_demand: { listings: [], reason: "no listing" }, settlement_fixing: {} })[0].reason === "no eligible constituents", "unpublished index keeps its reason");

// ---------- offers / copy / formatting ----------
ok(R.offerOf({ offer: { offer_seq: 2 } }).offer_seq === 2 && R.offerOf({ offer: null }) === null, "offerOf");
ok(R.ACCEPTED === "Accepted: OpenGrid will contact you to sign the order form; nothing is billed by this action.", "acceptance copy (exact)");
ok(R.BANNER.includes("not a financial contract, not tradable, no cash settlement") && R.BANNER.includes("only when provider capacity backs it"), "banner copy");
ok(R.usd(12345.6) === "$12,345.60" && R.usd(null) === "-", "usd");
ok(R.perGpuH(2.19) === "$2.190/GPU·h" && R.perGpuH(12.5) === "$12.50/GPU·h", "per GPU-hour");
ok(R.hoursText(5376) === "5,376 GPU·h", "GPU-hours");
ok(R.localToIso("") === null && R.localToIso("not a date") === null, "localToIso guards");

console.log(failures ? `\n${failures} FAILED` : "\nall passed");
process.exit(failures ? 1 : 0);
