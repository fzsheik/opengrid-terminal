// Pure helpers of the market page. Run: node tests/test_lib.js
const L = require("../web/lib.js");
let failures = 0;
const ok = (c, m) => { if (!c) { failures++; console.log("FAIL:", m); } else console.log("ok  :", m); };

// names
ok(L.shortGpu("NVIDIA H100 80GB SXM5") === "H100 80GB SXM5", "strips NVIDIA");
ok(L.shortGpu("AMD Instinct MI300X 192GB") === "MI300X 192GB", "strips AMD Instinct");
ok(L.vendorOf("AMD Instinct MI300X 192GB") === "AMD" && L.vendorOf("Intel Gaudi 2 96GB") === "Intel" && L.vendorOf("NVIDIA X") === "NVIDIA", "vendors");
ok(L.providerName("voltagepark") === "Voltage Park" && L.providerName("newcloud") === "newcloud", "provider names, unknown passes through");

// formatting
ok(L.fmtPrice(1.3) === "$1.30" && L.fmtPrice(12.4) === "$12.40" && L.fmtPrice(0.045) === "$0.045" && L.fmtPrice(null) === "–", "prices");
ok(L.fmtPct(0.021) === "+2.1%" && L.fmtPct(-0.004) === "−0.4%" && L.fmtPct(0.5) === "+50%" && L.fmtPct(null) === "–", "percentages use a real minus sign");
ok(L.fmtPct(0.00001) === "0.0%", "a move that rounds to zero carries no sign: " + L.fmtPct(0.00001));
ok(L.direction(0.00001) === "flat" && L.direction(0.02) === "up" && L.direction(-0.02) === "down" && L.direction(undefined) === "none", "direction");

// ticks
const t = L.niceTicks(1.3, 6.9, 5);
ok(t.length >= 3 && t.every(v => v >= 1.3 && v <= 6.9), "ticks inside the range: " + t.join(","));
ok(t.every(v => Math.abs(v * 2 - Math.round(v * 2)) < 1e-9 || Math.abs(v * 10 - Math.round(v * 10)) < 1e-9), "ticks are round numbers");
ok(L.niceTicks(2, 2, 5).length === 1, "flat range gives one tick");

// label spreading: the part most likely to be wrong
function check(ys, gap, lo, hi, label) {
  const out = L.spread(ys, gap, lo, hi);
  const order = ys.map((y, i) => i).sort((a, b) => ys[a] - ys[b] || a - b);
  const sorted = order.map(i => out[i]);
  const g = ys.length > 1 ? Math.min(gap, (hi - lo) / (ys.length - 1)) : gap;
  const spaced = sorted.every((v, k) => k === 0 || v - sorted[k - 1] >= g - 1e-9);
  const inside = out.every(v => v >= lo - 1e-9 && v <= hi + 1e-9);
  ok(spaced && inside, `${label}: spaced >= ${g.toFixed(1)}, inside [${lo},${hi}] -> ${out.map(v => v.toFixed(0)).join(",")}`);
  return out;
}
check([10, 50, 90], 20, 0, 100, "already apart stays put");
let o = L.spread([10, 50, 90], 20, 0, 100); ok(o[0] === 10 && o[1] === 50 && o[2] === 90, "untouched when there is room");
check([50, 50, 50, 50], 20, 0, 200, "four identical positions");
check([98, 99, 100, 100], 20, 0, 100, "cluster at the bottom edge");
check([0, 1, 2, 3, 4, 5, 6, 7, 8, 9], 20, 0, 100, "ten labels in a hundred pixels squeeze the gap");
check([300, -40, 120], 20, 0, 200, "positions outside the bounds are clamped, order kept");
check([42], 20, 0, 100, "single label");
ok(L.spread([], 20, 0, 100).length === 0, "empty input");
o = L.spread([30, 10, 20], 5, 0, 100); ok(o[1] <= o[2] && o[2] <= o[0], "returns positions in the caller's order, not sorted order: " + o.join(","));
// labels keep their order: the one that was higher stays higher
for (let trial = 0; trial < 200; trial++) {
  const n = 1 + Math.floor(Math.random() * 12), ys = Array.from({ length: n }, () => Math.random() * 300 - 50);
  const out = L.spread(ys, 18, 10, 190);
  const idx = ys.map((y, i) => i).sort((a, b) => ys[a] - ys[b] || a - b);
  const g = n > 1 ? Math.min(18, 180 / (n - 1)) : 18;
  const good = idx.every((i, k) => k === 0 || out[i] - out[idx[k - 1]] >= g - 1e-6) && out.every(v => v >= 10 - 1e-6 && v <= 190 + 1e-6);
  if (!good) { failures++; console.log("FAIL: random trial", ys, out); break; }
  if (trial === 199) console.log("ok  : 200 random label sets stay ordered, spaced and inside the box");
}

// lines with gaps
const x = i => i * 10, y = v => 100 - v;
ok(L.linePath([1, 2, 3], x, y) === "M0.0 99.0L10.0 98.0L20.0 97.0", "continuous line: " + L.linePath([1, 2, 3], x, y));
ok(L.linePath([null, 5, null, 6, 7], x, y) === "M10.0 95.0M30.0 94.0L40.0 93.0", "a null breaks the line instead of joining across it: " + L.linePath([null, 5, null, 6, 7], x, y));
ok(L.linePath([null, null], x, y) === "", "all-null series draws nothing");
ok(L.areaPath([null, 5, 6], x, y, 100).startsWith("M10.0 100L10.0 95.0L20.0 94.0L20.0 100Z"), "area closes down to the baseline: " + L.areaPath([null, 5, 6], x, y, 100));
ok((L.areaPath([1, null, 2], x, y, 100).match(/Z/g) || []).length === 2, "a gap makes two separate areas");

// step lines: prices are posted, never interpolated
ok(L.stepPath([1, 1, 3], x, y) === "M0.0 99.0H10.0V99.0H20.0V97.0", "a step holds, then jumps straight: " + L.stepPath([1, 1, 3], x, y));
ok(L.stepPath([null, 5, null, 6, 7], x, y) === "M10.0 95.0M30.0 94.0H40.0V93.0", "a gap breaks the step line: " + L.stepPath([null, 5, null, 6, 7], x, y));
ok(L.stepPath([], x, y) === "" && L.stepPath([null], x, y) === "", "empty and all-null draw nothing");
ok(!/L/.test(L.stepPath([1, 2, 3, 2, 1], x, y)), "no diagonal segments at all");

// smooth curves: through the data, never beyond it
const pts = [[0, 100], [10, 100], [20, 60], [30, 60], [40, 90]];     // pixel y: a drop, a flat, a rise
const seg = L.smoothSegments(pts);
ok(seg.length === 4 && seg[3][4] === 40 && seg[3][5] === 90 && seg[0][4] === 10 && seg[0][5] === 100, "curve ends exactly on every sample");
ok(seg.every((c, i) => {                                             // no control point past its two neighbours: no overshoot
  const lo = Math.min(pts[i][1], pts[i + 1][1]), hi = Math.max(pts[i][1], pts[i + 1][1]);
  return c[1] >= lo - 1e-9 && c[1] <= hi + 1e-9 && c[3] >= lo - 1e-9 && c[3] <= hi + 1e-9;
}), "control points stay between neighbouring samples (no overshoot at a step)");
ok(seg[0][1] === 100 && seg[0][3] === 100 && seg[2][1] === 60 && seg[2][3] === 60, "a flat stretch stays exactly flat");
ok(L.smoothSegments([[0, 5]]).length === 0 && L.smoothSegments([]).length === 0, "fewer than two points: nothing to draw");
const two = L.smoothSegments([[0, 0], [30, 30]]);
ok(two.length === 1 && Math.abs(two[0][1] - 10) < 1e-9 && Math.abs(two[0][3] - 20) < 1e-9, "two points make a straight line: " + two[0].join(","));
const mono = L.smoothSegments([[0, 0], [1, 1], [2, 5], [3, 5.2], [4, 20]]);
ok(mono.every((c, i) => c[1] >= (i === 0 ? 0 : [0, 1, 5, 5.2][i]) - 1e-9 && c[3] <= [1, 5, 5.2, 20][i] + 1e-9), "rising data gives a rising curve");
const sp = L.smoothPath([1, 2, 3, null, 4, 5], x, y);
ok(sp.startsWith("M0.0 99.0C") && (sp.match(/M/g) || []).length === 2 && !/NaN/.test(sp), "a gap starts a new curve: " + sp.slice(0, 60));
ok(L.smoothPath([1, null, 2], x, y) === "" && L.smoothPath([], x, y) === "", "single points and empty series draw nothing");
ok(/^M0\.0 100L0\.0 99\.0C.*L20\.0 100Z$/.test(L.smoothArea([1, 2, 3], x, y, 100)), "area closes to the baseline: " + L.smoothArea([1, 2, 3], x, y, 100));
ok((L.smoothArea([1, 2, null, 3, 4], x, y, 100).match(/Z/g) || []).length === 2, "a gap makes two areas");
const wild = Array.from({ length: 200 }, (_, i) => (i % 17 === 0 ? null : Math.round(Math.random() * 50)));
ok(!/NaN|Infinity/.test(L.smoothPath(wild, x, y) + L.smoothArea(wild, x, y, 100)), "random series with gaps never produce NaN");
let over = false;
for (let t = 0; t < 100 && !over; t++) {
  const ys = Array.from({ length: 12 }, () => Math.round(Math.random() * 40));
  const P = ys.map((v, i) => [i * 10, v]);
  L.smoothSegments(P).forEach((c, i) => { const lo = Math.min(P[i][1], P[i + 1][1]), hi = Math.max(P[i][1], P[i + 1][1]); if (c[1] < lo - 1e-6 || c[1] > hi + 1e-6 || c[3] < lo - 1e-6 || c[3] > hi + 1e-6) over = true; });
}
ok(!over, "100 random price series: the curve never overshoots the data");

// colours
const colours = Object.values(L.PROVIDER_COLORS);
ok(new Set(colours).size === colours.length, "every provider has its own colour");
ok(Object.keys(L.PROVIDER_COLORS).every(k => k in L.PROVIDER_NAMES) && Object.keys(L.PROVIDER_NAMES).every(k => k in L.PROVIDER_COLORS), "every known provider has both a name and a colour");
ok(L.providerColor("unknown") === "#94a3b8" && L.providerColor("lium") === L.PROVIDER_COLORS.lium, "unknown providers get a neutral colour");

// time and hover
ok(L.timeLabel("2026-10-04T14:05:00", 6) === "14:05" && /^10\/4 \d\dh$/.test(L.timeLabel("2026-10-04T14:05:00", 72)) && L.timeLabel("2026-10-04T14:05:00", 500) === "10/4", "time labels by span");
ok(L.nearestIndex(0, 11, 0, 100) === 0 && L.nearestIndex(100, 11, 0, 100) === 10 && L.nearestIndex(54, 11, 0, 100) === 5 && L.nearestIndex(-30, 11, 0, 100) === 0 && L.nearestIndex(500, 11, 0, 100) === 10, "hover maps to the nearest sample and clamps");
ok(L.nearestIndex(5, 1, 0, 100) === 0, "single sample");
ok(L.finite([1, null, NaN, 2, undefined]).join() === "1,2", "finite filter");
ok(L.xFrac(0, 5) === 0 && L.xFrac(4, 5) === 1 && L.xFrac(2, 5) === 0.5, "axis fractions");
ok(L.xFrac(0, 1) === 0.5 && !Number.isNaN(L.xFrac(0, 1)) && !Number.isNaN(L.xFrac(0, 0)), "one sample centres instead of NaN");
ok(!/NaN/.test(L.linePath([3], i => L.xFrac(i, 1) * 100, v => 50)), "a one-point line has no NaN in its path");

console.log(failures ? `\n${failures} FAILED` : "\nall passed");
process.exit(failures ? 1 : 0);
