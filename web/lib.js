// Pure helpers for the market page: no DOM, so they can be tested in Node.
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.Lib = factory();
})(typeof self !== "undefined" ? self : this, function () {
  const MINUS = "−";

  const PROVIDER_NAMES = {
    salad: "Salad", hyperstack: "Hyperstack", lambda: "Lambda", runpod: "RunPod", hyperbolic: "Hyperbolic",
    voltagepark: "Voltage Park", lium: "Lium", vast: "Vast.ai", nebius: "Nebius", massedcompute: "Massed Compute",
    digitalocean: "DigitalOcean", aws: "AWS", verda: "Verda", crusoe: "Crusoe", latitude: "Latitude.sh", denvr: "Denvr",
  };
  const providerName = p => PROVIDER_NAMES[p] || p;

  // One fixed colour per provider, so a provider looks the same on every chart.
  // Muted hues spread around the wheel, picked to stay apart on a dark background.
  const PROVIDER_COLORS = {
    massedcompute: "#ef4444", crusoe: "#f97316", aws: "#f59e0b", nebius: "#facc15", vast: "#84cc16",
    hyperstack: "#22c55e", verda: "#14b8a6", lium: "#22d3ee", latitude: "#38bdf8", digitalocean: "#3b82f6",
    voltagepark: "#6366f1", runpod: "#a855f7", denvr: "#d946ef", hyperbolic: "#ec4899", salad: "#fb7185", lambda: "#e5e7eb",
  };
  const providerColor = p => PROVIDER_COLORS[p] || "#94a3b8";

  // "NVIDIA H100 80GB SXM5" -> "H100 80GB SXM5"
  const shortGpu = name => String(name).replace(/^(NVIDIA|AMD Instinct|AMD|Intel)\s+/i, "");
  const vendorOf = name => /^NVIDIA/i.test(name) ? "NVIDIA" : /^AMD/i.test(name) ? "AMD" : /^Intel/i.test(name) ? "Intel" : "Other";

  // $1.30, $12.40, $0.045: more places when the price is small, so cheap GPUs do not all read $0.02
  function fmtPrice(p) {
    if (p == null || !isFinite(p)) return "–";
    const d = p >= 10 ? 2 : p >= 0.1 ? 2 : 3;
    return "$" + p.toFixed(d);
  }
  function fmtPct(p) {
    if (p == null || !isFinite(p)) return "–";
    const v = Math.abs(p * 100);
    const s = v < 10 ? v.toFixed(1) : v.toFixed(0);
    return (p > 0 && Number(s) !== 0 ? "+" : p < 0 && Number(s) !== 0 ? MINUS : "") + s + "%";
  }
  // "up" / "down" / "flat": a rounded 0.0% is flat, never a coloured arrow
  function direction(p) {
    if (p == null || !isFinite(p)) return "none";
    return Math.abs(p * 100) < 0.05 ? "flat" : p > 0 ? "up" : "down";
  }

  // Round tick values covering [min, max], about `count` of them
  function niceTicks(min, max, count) {
    if (!(max > min)) return [min];
    const raw = (max - min) / Math.max(1, count);
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => s >= raw) || 10 * mag;
    const out = [];
    for (let v = Math.ceil(min / step) * step; v <= max + step * 1e-9; v += step) out.push(Number(v.toFixed(10)));
    return out;
  }

  // Keep labels at least `gap` apart, in their original order, inside [lo, hi].
  // Returns new y positions. Order is what matters: label i stays above label i+1.
  function spread(ys, gap, lo, hi) {
    const n = ys.length;
    if (!n) return [];
    const idx = ys.map((y, i) => i).sort((a, b) => ys[a] - ys[b] || a - b);
    const pos = idx.map(i => Math.min(hi, Math.max(lo, ys[i])));
    // Too many labels for the room: squeeze the gap rather than overflow
    const g = n > 1 ? Math.min(gap, (hi - lo) / (n - 1)) : gap;
    for (let k = 1; k < n; k++) if (pos[k] < pos[k - 1] + g) pos[k] = pos[k - 1] + g;
    if (pos[n - 1] > hi) {                       // pushed past the bottom: walk back up
      pos[n - 1] = hi;
      for (let k = n - 2; k >= 0; k--) if (pos[k] > pos[k + 1] - g) pos[k] = pos[k + 1] - g;
    }
    const out = new Array(n);
    idx.forEach((i, k) => { out[i] = pos[k]; });
    return out;
  }

  // SVG path pieces for a series with gaps: one "M x y L x y ..." per unbroken run
  function linePath(values, xAt, yAt) {
    let d = "", pen = false;
    values.forEach((v, i) => {
      if (v == null) { pen = false; return; }
      d += (pen ? "L" : "M") + xAt(i).toFixed(1) + " " + yAt(v).toFixed(1);
      pen = true;
    });
    return d;
  }
  // A step line: each price holds until the next sample, then moves straight up or down.
  // Prices are posted, not interpolated, so a slope between two samples would be invented.
  function stepPath(values, xAt, yAt) {
    let d = "", prev = null;
    values.forEach((v, i) => {
      if (v == null) { prev = null; return; }
      d += prev == null ? "M" + xAt(i).toFixed(1) + " " + yAt(v).toFixed(1)
        : "H" + xAt(i).toFixed(1) + "V" + yAt(v).toFixed(1);
      prev = v;
    });
    return d;
  }

  // Smooth curve through the points of each unbroken run: a monotone cubic (Fritsch-Carlson),
  // so the curve bends through every sample but never overshoots above or below the data,
  // which a plain spline would do next to a step in price.
  function smoothSegments(pts) {
    const n = pts.length;
    if (n < 2) return [];
    const h = [], d = [];
    for (let i = 0; i < n - 1; i++) { h.push(pts[i + 1][0] - pts[i][0]); d.push((pts[i + 1][1] - pts[i][1]) / (h[i] || 1)); }
    const m = new Array(n);
    m[0] = d[0]; m[n - 1] = d[n - 2];
    for (let i = 1; i < n - 1; i++) m[i] = d[i - 1] * d[i] <= 0 ? 0 : (d[i - 1] + d[i]) / 2;
    for (let i = 0; i < n - 1; i++) {
      if (d[i] === 0) { m[i] = 0; m[i + 1] = 0; continue; }
      const a = m[i] / d[i], b = m[i + 1] / d[i], q = a * a + b * b;
      if (q > 9) { const t = 3 / Math.sqrt(q); m[i] = t * a * d[i]; m[i + 1] = t * b * d[i]; }
    }
    const seg = [];
    for (let i = 0; i < n - 1; i++) {
      const dx = h[i] / 3;
      seg.push([pts[i][0] + dx, pts[i][1] + m[i] * dx, pts[i + 1][0] - dx, pts[i + 1][1] - m[i + 1] * dx, pts[i + 1][0], pts[i + 1][1]]);
    }
    return seg;
  }
  function runsOf(values, xAt, yAt) {
    const runs = []; let cur = null;
    values.forEach((v, i) => {
      if (v == null) { cur = null; return; }
      if (!cur) { cur = []; runs.push(cur); }
      cur.push([xAt(i), yAt(v)]);
    });
    return runs.filter(r => r.length >= 2);
  }
  const f1 = n => n.toFixed(1);
  function smoothPath(values, xAt, yAt) {
    return runsOf(values, xAt, yAt).map(r => "M" + f1(r[0][0]) + " " + f1(r[0][1]) +
      smoothSegments(r).map(c => "C" + c.map(f1).join(" ")).join("")).join("");
  }
  function smoothArea(values, xAt, yAt, base) {
    return runsOf(values, xAt, yAt).map(r => "M" + f1(r[0][0]) + " " + base + "L" + f1(r[0][0]) + " " + f1(r[0][1]) +
      smoothSegments(r).map(c => "C" + c.map(f1).join(" ")).join("") +
      "L" + f1(r[r.length - 1][0]) + " " + base + "Z").join("");
  }

  // Closed area under each unbroken run, down to `base`
  function areaPath(values, xAt, yAt, base) {
    const runs = []; let cur = null;
    values.forEach((v, i) => {
      if (v == null) { cur = null; return; }
      if (!cur) { cur = []; runs.push(cur); }
      cur.push([xAt(i), yAt(v)]);
    });
    return runs.map(r => "M" + r[0][0].toFixed(1) + " " + base + r.map(p => "L" + p[0].toFixed(1) + " " + p[1].toFixed(1)).join("") +
      "L" + r[r.length - 1][0].toFixed(1) + " " + base + "Z").join("");
  }

  // Axis label for a time, shaped by the span being shown
  function timeLabel(iso, spanHours) {
    const d = new Date(iso);
    const p = n => String(n).padStart(2, "0");
    if (spanHours <= 36) return p(d.getHours()) + ":" + p(d.getMinutes());
    return (d.getMonth() + 1) + "/" + d.getDate() + (spanHours <= 24 * 4 ? " " + p(d.getHours()) + "h" : "");
  }
  const spanHours = (t0, t1) => (new Date(t1) - new Date(t0)) / 36e5;

  // Index of the grid time nearest x (for the hover crosshair)
  function nearestIndex(x, n, left, right) {
    if (n <= 1) return 0;
    const f = (x - left) / (right - left);
    return Math.max(0, Math.min(n - 1, Math.round(f * (n - 1))));
  }

  // Position of sample i of n along an axis, 0..1. One sample sits in the middle instead of dividing by zero.
  const xFrac = (i, n) => n > 1 ? i / (n - 1) : 0.5;

  function finite(arr) { return arr.filter(v => v != null && isFinite(v)); }

  return { providerName, shortGpu, vendorOf, fmtPrice, fmtPct, direction, niceTicks, spread, linePath, stepPath, smoothPath, smoothArea, smoothSegments, providerColor, PROVIDER_COLORS, areaPath,
    timeLabel, spanHours, nearestIndex, xFrac, finite, PROVIDER_NAMES, MINUS };
});
