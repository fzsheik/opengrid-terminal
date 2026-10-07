/* /route — best-execution order ticket.
   "I need 8 H100s in the US under $2.50/hr": the ticket builds a POST /v1/route/preview body, the
   result shows the selected listing, its QUOTE (distinct from the observed market price), the score
   breakdown per candidate, exclusions with reasons and the factors that have no data.
   "Route & provision" calls POST /v1/route only after an explicit confirmation, and shows what the
   server actually did (e.g. not_provisioned: live provisioning disabled) — never more.
   The command bar opens /route?gpu=<slug>&count=<n>&region=<group> ("r h100 8 us"); the ticket is URL-synced.
   Strict region (strict_region) and, for a GPU family, allow variants (allow_variants) are URL-synced too. */
(() => {
  const { h, fmt } = OG;

  // OG.dialog, OG.conceptBadge and OG.money live in core.js (shared with deployments / keys / watchlists).

  /* ---------- constants mirrored from routing/scoring.py ---------- */
  const REGIONS = ["US", "Canada", "Europe", "UK", "APAC", "Middle East", "LATAM", "Africa"];
  const MODES = [
    ["CHEAPEST", "Cheapest", "Lowest observed price wins; no other factor counts"],
    ["FASTEST_AVAILABLE", "Fastest", "Availability now, OpenGrid integration and freshness first"],
    ["BALANCED", "Balanced", "Price 40%, availability 20%, stability 15%, availability history 15%, freshness, integration"],
    ["MOST_STABLE", "Stable", "Availability history and price stability first"],
    ["USER_DEFINED", "Custom", "Your own weights over the factors that have data"],
  ];
  const FACTORS = [
    { key: "price", label: "Price", short: "price", color: "#4d94ff", kind: "observed" },
    { key: "availability_now", label: "Availability now", short: "avail", color: "#2fbf71", kind: "observed" },
    { key: "region_match", label: "Region match", short: "region", color: "#b39cff", kind: "observed" },
    { key: "freshness", label: "Data freshness", short: "fresh", color: "#8a96a3", kind: "observed" },
    { key: "availability_persistence", label: "Availability history", short: "hist", color: "#f5a524", kind: "inferred" },
    { key: "price_stability", label: "Price stability", short: "stab", color: "#e0729e", kind: "inferred" },
    { key: "integration_level", label: "OpenGrid integration", short: "integ", color: "#4fc3d0", kind: "registry" },
    { key: "reliability", label: "Reliability", short: "rel", color: "#555", kind: null, nodata: true },
    { key: "performance", label: "Performance", short: "perf", color: "#555", kind: null, nodata: true },
  ];
  const FACTOR = Object.fromEntries(FACTORS.map(f => [f.key, f]));
  const BALANCED_W = { price: 40, availability_now: 20, price_stability: 15, availability_persistence: 15, freshness: 5, integration_level: 5, region_match: 10 };
  const LEVEL = { 0: "market data only", 1: "availability check", 2: "provisioning", 3: "full lifecycle" };
  const EXCL = {
    over_max_price: "over max price", wrong_region: "wrong region", sold_out: "sold out", not_on_demand: "not on-demand",
    single_host_ask: "single-host ask", floor_price: "floor price", ineligible: "ineligible", no_price: "no price", stale: "stale",
    wrong_count: "wrong GPU count", excluded_by_preference: "excluded by you", below_required_level: "not provisionable",
    availability_unknown: "availability unknown", region_not_confirmed: "region unconfirmed", region_unconfirmed: "region unconfirmed",
    wrong_variant: "other variant", variant_not_allowed: "other variant",
  };

  const num = v => { const n = Number(v); return v != null && v !== "" && isFinite(n) && n > 0 ? n : null; };
  const regionOf = v => (v ? REGIONS.find(g => g.toLowerCase() === String(v).trim().toLowerCase()) || null : null);
  function parseW(s) {
    if (!s) return null;
    const out = {};
    for (const kv of String(s).split(",")) { const [k, v] = kv.split(":"); if (FACTOR[k] && !FACTOR[k].nodata && isFinite(Number(v))) out[k] = Math.max(0, Math.min(100, Number(v))); }
    return Object.keys(out).length ? out : null;
  }
  const levelBadge = lvl => lvl >= 2 ? OG.badge("OpenGrid can provision · L" + lvl, "good", "Integration level " + lvl + ": " + LEVEL[lvl])
    : OG.badge("market data only · L" + (lvl || 0), "bad", "Integration level " + (lvl || 0) + ": OpenGrid reads prices here but cannot launch an instance");

  /* ---------- score breakdown bar ---------- */
  function scoreBar(c, width) {
    const segs = FACTORS.filter(f => !f.nodata).map(f => {
      const x = c.factors && c.factors[f.key];
      if (!x || !x.contribution) return null;
      return h("i", { style: { width: (x.contribution * 100).toFixed(2) + "%", background: f.color }, class: x.imputed ? "imp" : null,
        title: `${f.label}: +${x.contribution.toFixed(3)} (weight ${(x.weight * 100).toFixed(0)}% × value ${x.imputed ? "0.5 imputed — no data" : x.value})` });
    });
    return h("span", { class: "rt-bar", style: width ? `width:${width}px` : null, title: "score " + (c.score != null ? c.score.toFixed(3) : "–") + " of 1.000" }, segs);
  }
  function legend(weights) {
    return h("div", { class: "rt-legend" }, FACTORS.map(f => {
      const w = weights ? weights[f.key] || 0 : 0;
      if (f.nodata) return h("span", { class: "rt-lk off", title: "No execution history yet: never weighted" }, h("i", { class: "sw", style: { background: "transparent", border: "1px dashed #4f5a66" } }), f.label, " · no data");
      return h("span", { class: "rt-lk" + (w ? "" : " off") }, h("i", { class: "sw", style: { background: f.color } }), f.label, " ", h("b", {}, (w * 100).toFixed(0) + "%"));
    }), h("span", { class: "rt-lk" }, h("i", { class: "sw imp-sw" }), "imputed 0.5 (thin history)"));
  }

  function regionText(c) {
    const parts = [c.region, c.country].filter(Boolean);
    return parts.length ? parts.join(", ") : "location not given";
  }
  function availText(c) {
    const f = c.factors && c.factors.availability_now;
    const basis = f && f.raw ? f.raw.basis : null;
    if (c.available === true) return h("span", { class: "up", title: "basis: " + (basis || "explicit") }, "available" + (basis === "inferred" ? " (inferred)" : ""));
    if (c.available === false) return h("span", { class: "down" }, "sold out");
    return h("span", { class: "dim", title: "The provider does not report stock" }, "unknown");
  }

  /* ---------- page ---------- */
  OG.page("/route", {
    title: "Route",
    async mount(el, params, query, ctx) {
      const root = h("div", { class: "pg-route" });
      el.append(root);
      const st = {
        gpu: query.gpu || "", gpuName: null,
        count: Math.max(1, Math.min(64, parseInt(query.count, 10) || 1)),
        region: regionOf(query.region), max: num(query.max), dur: num(query.dur), dl: num(query.dl),
        mode: MODES.some(m => m[0] === String(query.mode || "").toUpperCase()) ? String(query.mode).toUpperCase() : "BALANCED",
        weights: parseW(query.w) || Object.assign({}, BALANCED_W),
        ex: new Set(String(query.ex || "").split(",").filter(Boolean)),
        lvl: query.lvl === "1", avail: query.avail === "1",
        strict: query.strict === "1", variants: query.variants === "1", family: null,
        launch: { name: "", ssh_key: "", image: "", disk_gb: "" },
        last: null, lastBody: null,
      };
      const badRegion = query.region && !st.region ? query.region : null;

      root.append(OG.head("Route", "Best execution across every provider OpenGrid observes · preview is free and never calls a provider",
        h("a", { class: "btn", href: "/methodology/best-execution" }, "Scoring method"),
        h("a", { class: "btn", href: "/methodology/routing" }, "Routing & provisioning"),
        h("a", { class: "btn", href: "/deployments" }, "Deployments →")));
      const ticket = h("form", { class: "rt-ticket box", onsubmit: e => { e.preventDefault(); preview(); } });
      const out = h("div", { class: "rt-out" });
      const routed = h("div", { class: "rt-routed" });
      root.append(h("div", { class: "rt-grid" }, ticket, h("div", { class: "rt-main" }, routed, out)));

      /* ----- GPU picker (fuzzy) ----- */
      let gpus = [];
      const gpuIn = h("input", { class: "field rt-gpu", type: "text", autocomplete: "off", spellcheck: "false", placeholder: "h100, mi300x, b200…", "aria-label": "GPU", value: st.gpu });
      const gpuFull = h("div", { class: "rt-gpu-full dim" });
      const sug = h("div", { class: "rt-sug", role: "listbox", hidden: true });
      let sugItems = [], sugSel = 0;
      function drawSug() {
        const q = gpuIn.value.trim();
        const famItems = q ? OG.fuzzy(q, fams, f => [f.slug, f.id, f.name].filter(Boolean)).slice(0, 2).map(f => ({ family: f, slug: f.slug || f.id, short: f.name || f.slug })) : [];
        const list = famItems.concat(q ? OG.fuzzy(q, gpus, g => [g.slug, g.short, g.name]) : gpus.filter(g => g.live).sort((a, b) => b.weight - a.weight));
        sugItems = list.slice(0, 9); sugSel = 0;
        paintSug();
      }
      function paintSug() {
        sug.replaceChildren(...sugItems.map((g, i) => h("div", { class: "rt-sug-i" + (i === sugSel ? " on" : ""), role: "option", onmousedown: e => { e.preventDefault(); pickGpu(g); } },
          h("span", {}, g.short), h("span", { class: "dim" }, g.family ? "family" : g.live ? `${g.weight} prov` : "none live"))));
        sug.hidden = !sugItems.length || document.activeElement !== gpuIn;
      }
      function pickGpu(g) {
        if (g.family) { pickFamily(g.family); return; }
        st.family = null; drawFamily();
        st.gpu = g.slug; st.gpuName = g.name; gpuIn.value = g.short; gpuFull.textContent = g.name + (g.live ? ` · ${g.weight} providers live` : " · no live listings");
        sug.hidden = true; sync(); markDirty();
      }
      gpuIn.addEventListener("input", () => { st.gpu = gpuIn.value.trim(); st.gpuName = null; st.family = null; drawFamily(); gpuFull.textContent = ""; drawSug(); markDirty(); });

      /* ----- GPU families: the request names a family; variants are never merged unless allowed ----- */
      let fams = [];
      const famBox = h("div", { class: "rt-fam", hidden: true });
      async function pickFamily(f) {
        st.gpu = f.slug || f.id; st.gpuName = null; gpuIn.value = f.name || st.gpu; sug.hidden = true;
        gpuFull.textContent = "GPU family · loading variants…";
        const d = await ctx.api("/v1/families/" + encodeURIComponent(st.gpu)).catch(() => null);
        if (!ctx.alive()) return;
        st.family = Object.assign({}, f, d || {});
        gpuFull.textContent = (st.family.name || st.gpu) + " · GPU family" + (Array.isArray(st.family.variants) ? ` · ${st.family.variants.length} variants` : "");
        drawFamily(); sync(); markDirty();
      }
      function drawFamily() {
        const f = st.family;
        if (!f) { famBox.hidden = true; famBox.replaceChildren(); return; }
        const vs = Array.isArray(f.variants) ? f.variants : [];
        famBox.hidden = false;
        famBox.replaceChildren(
          h("div", { class: "rt-h" }, f.note || "Variants of one family are different products (memory, form factor, interconnect); OpenGrid never merges their prices."),
          vs.length ? h("ul", { class: "rt-fam-l" }, vs.map(v => h("li", {},
            h("a", { class: "lnk", href: "/gpu/" + (v.slug || OG.slug(v.gpu)), title: v.gpu }, OG.shortGpu(v.gpu || v.slug)),
            h("span", { class: "mono dim" }, v.low != null ? "from " + fmt.price(v.low) : "no live price"),
            h("span", { class: "dim" }, v.providers != null ? `${v.providers} prov` : ""),
            h("button", { type: "button", class: "btn sm", title: "Route this variant only", onclick: () => { const g = gpus.find(x => x.slug === (v.slug || OG.slug(v.gpu))); if (g) pickGpu(g); } }, "only this")))) : h("div", { class: "dim" }, "Variant list unavailable."),
          h("label", { class: "rt-chk", title: "allow_variants: rank listings of every variant of this family together; each candidate keeps its own GPU name and price" },
            h("input", { type: "checkbox", checked: st.variants ? true : null, onchange: e => { st.variants = e.target.checked; sync(); markDirty(); } }),
            "Allow variants (any ", f.name || st.gpu, " variant may be selected)"),
          st.variants ? null : h("div", { class: "rt-h warn-t" }, "Without this, pick one variant: a family alone is not one product."));
      }
      gpuIn.addEventListener("focus", drawSug);
      gpuIn.addEventListener("blur", () => setTimeout(() => { sug.hidden = true; }, 120));
      gpuIn.addEventListener("keydown", e => {
        if (sug.hidden) return;
        if (e.key === "ArrowDown") { e.preventDefault(); sugSel = Math.min(sugItems.length - 1, sugSel + 1); paintSug(); }
        else if (e.key === "ArrowUp") { e.preventDefault(); sugSel = Math.max(0, sugSel - 1); paintSug(); }
        else if (e.key === "Enter" && sugItems[sugSel]) { e.preventDefault(); pickGpu(sugItems[sugSel]); }
        else if (e.key === "Escape") { sug.hidden = true; }
      });

      /* ----- other fields ----- */
      const field = (label, input, hint) => h("label", { class: "rt-f" }, h("span", { class: "rt-l" }, label), input, hint ? h("span", { class: "rt-h" }, hint) : null);
      const numIn = (key, attrs) => {
        const i = h("input", Object.assign({ class: "field num", type: "number", value: st[key] == null ? "" : st[key] }, attrs));
        i.addEventListener("input", () => { st[key] = key === "count" ? Math.max(1, Math.min(64, parseInt(i.value, 10) || 1)) : num(i.value); sync(); markDirty(); });
        return i;
      };
      const countIn = numIn("count", { min: 1, max: 64, step: 1, "aria-label": "GPUs per instance" });
      const countChips = h("span", { class: "rt-chips" }, [1, 2, 4, 8].map(n => h("button", { type: "button", class: "chip", "aria-pressed": String(st.count === n), onclick: () => { st.count = n; countIn.value = n; drawCountChips(); sync(); markDirty(); } }, n + "×")));
      function drawCountChips() { countChips.querySelectorAll("button").forEach(b => b.setAttribute("aria-pressed", String(b.textContent === st.count + "×"))); }
      countIn.addEventListener("input", drawCountChips);
      const regionSel = h("select", { class: "field", "aria-label": "Region group", onchange: e => { st.region = e.target.value || null; strictIn.disabled = !st.region; sync(); drawWeights(); markDirty(); } },
        h("option", { value: "" }, "Any region"), REGIONS.map(g => h("option", { value: g, selected: st.region === g ? true : null }, g)));
      const maxIn = numIn("max", { min: 0, step: 0.01, placeholder: "none", "aria-label": "Max price per GPU-hour" });
      const durIn = numIn("dur", { min: 0, step: 1, placeholder: "–", "aria-label": "Duration hours" });
      const dlIn = numIn("dl", { min: 0, step: 1, placeholder: "–", "aria-label": "Deadline hours" });

      const modeSeg = OG.seg(MODES, st.mode, v => { st.mode = v; modeHint.textContent = MODES.find(m => m[0] === v)[2]; drawWeights(); sync(); markDirty(); });
      modeSeg.classList.add("rt-modes");
      const modeHint = h("div", { class: "rt-h" }, MODES.find(m => m[0] === st.mode)[2]);
      const weightsBox = h("div", { class: "rt-weights" });
      function drawWeights() {
        if (st.mode !== "USER_DEFINED") { weightsBox.hidden = true; weightsBox.replaceChildren(); return; }
        weightsBox.hidden = false;
        const usable = FACTORS.filter(f => !f.nodata && (f.key !== "region_match" || st.region));
        const total = usable.reduce((s, f) => s + (st.weights[f.key] || 0), 0);
        weightsBox.replaceChildren(...FACTORS.map(f => {
          if (f.nodata) return h("div", { class: "rt-w off", title: "OpenGrid has no execution history yet, so this factor cannot be weighted" },
            h("span", { class: "rt-wl" }, f.label), h("input", { type: "range", min: 0, max: 100, value: 0, disabled: true }), h("span", { class: "rt-wv" }, "no data — not used"));
          const off = f.key === "region_match" && !st.region;
          const v = st.weights[f.key] || 0;
          const r = h("input", { type: "range", min: 0, max: 100, step: 5, value: off ? 0 : v, disabled: off ? true : null, "aria-label": f.label + " weight",
            oninput: e => { st.weights[f.key] = Number(e.target.value); drawWeightLabels(); sync(); markDirty(); } });
          return h("div", { class: "rt-w" + (off ? " off" : ""), "data-f": f.key, title: off ? "Pick a region group to weight region match" : null },
            h("span", { class: "rt-wl" }, h("i", { class: "sw", style: { background: f.color } }), f.label), r,
            h("span", { class: "rt-wv mono" }, off ? "needs region" : total ? Math.round(v / total * 100) + "%" : "0%"));
        }), h("div", { class: "rt-h" }, "Weights are normalized to 100%. Reliability and performance stay at zero until OpenGrid has real execution records."));
        function drawWeightLabels() {
          const t = usable.reduce((s, f) => s + (st.weights[f.key] || 0), 0);
          weightsBox.querySelectorAll(".rt-w[data-f]").forEach(row => {
            const k = row.dataset.f; if (k === "region_match" && !st.region) return;
            row.querySelector(".rt-wv").textContent = t ? Math.round((st.weights[k] || 0) / t * 100) + "%" : "0%";
          });
        }
      }

      const exBox = h("div", { class: "rt-ex" });
      function drawEx(caps) {
        const provs = OG.data.providers().map(p => p.name).sort();
        exBox.replaceChildren(...provs.map(p => {
          const lvl = caps ? caps.get(p) : null;
          return h("button", { type: "button", class: "chip rt-exc", "aria-pressed": String(st.ex.has(p)), title: (st.ex.has(p) ? "Excluded. " : "Click to exclude. ") + (lvl != null ? `OpenGrid integration level ${lvl}: ${LEVEL[lvl]}` : ""),
            onclick: e => { st.ex.has(p) ? st.ex.delete(p) : st.ex.add(p); e.currentTarget.setAttribute("aria-pressed", String(st.ex.has(p))); sync(); markDirty(); } },
          lvl >= 2 ? h("i", { class: "rt-prov-dot", title: "OpenGrid can provision here" }) : null, OG.providerName(p));
        }));
      }
      const chk = (key, label, title) => h("label", { class: "rt-chk", title }, h("input", { type: "checkbox", checked: st[key] ? true : null, onchange: e => { st[key] = e.target.checked; sync(); markDirty(); } }), label);

      // strict_region: only listings whose location is confirmed in the group (unlocated ones are excluded, with the reason)
      const strictIn = h("input", { type: "checkbox", checked: st.strict ? true : null, disabled: st.region ? null : true,
        onchange: e => { st.strict = e.target.checked; sync(); markDirty(); } });
      const strictLbl = h("label", { class: "rt-chk rt-strict", title: "strict_region: exclude listings whose region is not confirmed as the chosen group (otherwise unlocated listings can still rank, scored lower on region match)" }, strictIn, "Strict region");
      const launchIn = (key, ph, attrs) => h("input", Object.assign({ class: "field", placeholder: ph, "aria-label": key, oninput: e => { st.launch[key] = e.target.value.trim(); } }, attrs || {}));
      const launchBox = h("details", { class: "rt-launch" }, h("summary", {}, "Launch parameters (route only)"),
        h("div", { class: "rt-h" }, "Used only by Route & provision on a provider that needs them (an SSH key registered with the provider for VMs; an image for RunPod / Vast pods). Env values are never stored."),
        field("Name", launchIn("name", "opengrid-job", { pattern: "[A-Za-z0-9][A-Za-z0-9-]*", maxlength: 60 })),
        field("SSH key", launchIn("ssh_key", "key name / id at the provider")),
        field("Image", launchIn("image", "e.g. pytorch/pytorch:latest")),
        field("Disk GB", launchIn("disk_gb", "–", { type: "number", min: 10, max: 20000, class: "field num" })));

      const previewBtn = h("button", { class: "btn pri rt-go", type: "submit" }, "Preview", h("kbd", {}, "↵"));
      const routeBtn = h("button", { class: "btn rt-exec", type: "button", onclick: () => routeNow() }, "Route & provision…");
      const dirty = h("span", { class: "rt-dirty", hidden: true }, "ticket changed — preview again");
      function markDirty() { if (st.last) dirty.hidden = false; }

      ticket.append(
        h("div", { class: "rt-tt" }, h("span", { class: "eyebrow" }, "Order ticket"), h("span", { class: "spacer" }), h("span", { class: "dim mono", style: "font-size:10.5px" }, "POST /v1/route/preview")),
        h("div", { class: "rt-f rt-gpuf" }, h("span", { class: "rt-l" }, "GPU"), h("div", { class: "rt-gpuw" }, gpuIn, sug), gpuFull, famBox),
        h("div", { class: "rt-row" }, field("GPUs / instance", h("span", { class: "rt-inl" }, countIn, countChips))),
        h("div", { class: "rt-row2" }, field("Region group", h("span", { class: "rt-inl" }, regionSel, strictLbl)), field("Max $/GPU·h", maxIn)),
        h("div", { class: "rt-row2" }, field("Duration h", durIn, "cost = quote × GPUs × hours"), field("Deadline h", dlIn, "if no duration: cost of the full window")),
        h("div", { class: "rt-f" }, h("span", { class: "rt-l" }, "Mode"), modeSeg, modeHint, weightsBox),
        h("div", { class: "rt-f" }, h("span", { class: "rt-l" }, "Exclude providers ", h("span", { class: "dimmer" }, "· ", h("i", { class: "rt-prov-dot" }), " = OpenGrid can provision")), exBox),
        h("div", { class: "rt-f" }, chk("lvl", "Require provisionable (OpenGrid integration ≥ 2)", "Only providers where OpenGrid has a provisioning adapter"),
          chk("avail", "Require explicit availability", "Only listings whose provider reports stock")),
        launchBox,
        h("div", { class: "rt-acts" }, previewBtn, routeBtn), dirty,
        h("p", { class: "rt-h" }, "Count is GPUs per instance; a route launches one instance. Preview writes an audit record but never calls a provider."));
      if (badRegion) ticket.prepend(h("div", { class: "state insufficient" }, h("b", {}, "Unknown region"), ` "${badRegion}" is not a region group; one of ${REGIONS.join(", ")}.`));
      drawWeights();
      drawEx(null);

      function sync() {
        const w = st.mode === "USER_DEFINED" ? FACTORS.filter(f => !f.nodata && st.weights[f.key]).map(f => f.key + ":" + st.weights[f.key]).join(",") : null;
        OG.qs.set({ gpu: st.gpu || null, count: st.count === 1 ? null : st.count, region: st.region, max: st.max, dur: st.dur, dl: st.dl,
          mode: st.mode === "BALANCED" ? null : st.mode, w, ex: st.ex.size ? [...st.ex].join(",") : null, lvl: st.lvl ? 1 : null, avail: st.avail ? 1 : null,
          strict: st.strict && st.region ? 1 : null, variants: st.family && st.variants ? 1 : null });
      }
      function body() {
        const b = { gpu: st.gpu, count: st.count, mode: st.mode };
        if (st.region) b.region = st.region;
        if (st.region && st.strict) b.strict_region = true;
        if (st.family && st.variants) b.allow_variants = true;
        if (st.max) b.max_price_per_gpu_hour = st.max;
        if (st.dur) b.duration_hours = st.dur;
        if (st.dl) b.deadline_hours = st.dl;
        if (st.mode === "USER_DEFINED") {
          b.weights = {};
          for (const f of FACTORS) if (!f.nodata && st.weights[f.key] > 0 && (f.key !== "region_match" || st.region)) b.weights[f.key] = st.weights[f.key];
        }
        const pref = {};
        if (st.ex.size) pref.exclude_providers = [...st.ex];
        if (st.lvl) pref.require_level = 2;
        if (st.avail) pref.require_available = true;
        if (Object.keys(pref).length) b.preferences = pref;
        const L = {};
        for (const [k, v] of Object.entries(st.launch)) if (v) L[k] = k === "disk_gb" ? parseInt(v, 10) : v;
        if (Object.keys(L).length) b.launch = L;
        return b;
      }

      /* ----- preview ----- */
      async function preview() {
        if (!st.gpu) { out.replaceChildren(OG.empty("Pick a GPU to preview a route.")); gpuIn.focus(); return; }
        if (!st.gpuName && !st.family) { const g = await OG.resolveGpu(st.gpu); if (g) { st.gpu = g.slug; st.gpuName = g.name; gpuIn.value = g.short; gpuFull.textContent = g.name; sync(); } }
        const b = body();
        out.replaceChildren(OG.loading("Ranking every eligible listing…"));
        try {
          const r = await ctx.api("/v1/route/preview", { method: "POST", body: b, full: true, slot: "route-preview" });
          st.last = r.data; st.lastBody = b; dirty.hidden = true;
          renderPreview(r.data, r.meta);
        } catch (e) {
          if (e.stale) return;
          const det = e.body && e.body.detail;
          if (det && det.code === "family_needs_variant") out.replaceChildren(OG.insufficient(String(det.message || e.message).replace(/[s.]*$/, ". ") + "Tick “Allow variants” or pick one variant under the GPU field.", "A family is not one product"));
          else out.replaceChildren(OG.error(e, preview));
        }
      }

      function cell(label, value, sub, badge) {
        return h("div", { class: "rt-c" }, h("div", { class: "rt-cl" }, label, badge || null), h("div", { class: "rt-cv" }, value), sub ? h("div", { class: "rt-cs" }, sub) : null);
      }

      function selectedCard(d) {
        const c = d.selected, q = d.quote || {}, m = d.market || {};
        const sv = q.savings_vs_median;
        const integ = c.factors && c.factors.integration_level;
        return h("div", { class: "box rt-sel" },
          h("div", { class: "rt-sel-h" },
            h("span", { class: "rt-rank" }, "#1"), OG.logo(c.provider, 18), h("a", { class: "lnk rt-pname", href: "/provider/" + encodeURIComponent(c.provider) }, c.provider_display || OG.providerName(c.provider)),
            h("span", { class: "mono dim" }, c.sku), h("span", { class: "dim" }, "·"), h("span", { class: "dim" }, regionText(c)), h("span", { class: "dim" }, "·"),
            h("span", { class: "mono" }, `${c.gpu_count}× `, OG.gpuLink(c.gpu)),
            h("span", { class: "spacer" }), levelBadge(c.integration_level)),
          h("div", { class: "rt-cells" },
            cell("Observed price", h("span", {}, fmt.price(c.price_per_gpu_hour), h("small", {}, "/GPU·h")), `${fmt.price(c.price_per_instance_hour)}/h for the instance`, OG.conceptBadge("observed")),
            cell("Quote", q.price_per_gpu_hour != null ? h("span", { class: "rt-q" }, fmt.price(q.price_per_gpu_hour), h("small", {}, "/GPU·h")) : OG.na("no quote"),
              q.basis === "observed_listing" ? "from the observed listing; not a live provider quote" : q.note, OG.conceptBadge("quote")),
            cell("Expected cost", q.expected_cost_usd != null ? OG.money(q.expected_cost_usd) : h("span", {}, OG.money(q.price_per_hour), h("small", {}, "/h")),
              q.expected_cost_usd != null ? `${fmt.num(q.price_per_hour, 2)}/h × ${fmt.num(q.hours)} h (${q.hours_from === "duration_hours" ? "duration" : "deadline window"})` : "no duration or deadline: hourly cost only"),
            cell("Market median", m.median != null ? h("span", {}, fmt.price(m.median), h("small", {}, "/GPU·h")) : OG.na("no market"),
              m.providers ? `${m.providers} providers · low ${fmt.price(m.low)} ${OG.providerName(m.low_provider)}` : null, OG.conceptBadge("observed")),
            cell("Savings vs median", sv ? h("span", { class: sv.per_gpu_hour >= 0 ? "up" : "down" }, sv.per_gpu_hour >= 0 ? "" : "−", fmt.price(Math.abs(sv.per_gpu_hour)), h("small", {}, "/GPU·h")) : OG.na("no market median"),
              sv ? `${fmt.num(Math.abs(sv.pct) * 100, 1)}% ${sv.pct >= 0 ? "below" : "above"} median${sv.total_usd != null ? " · " + OG.money(sv.total_usd) + " total" : ""}` : null),
            cell("Availability", availText(c), c.factors && c.factors.availability_now ? "basis: " + (c.factors.availability_now.raw.basis || "unknown") : null),
            cell("Freshness", OG.freshBadge(c.observed_at), c.factors && c.factors.freshness ? c.factors.freshness.note : null),
            cell("Can OpenGrid provision?", c.provisionable ? h("span", { class: "up" }, "yes · level " + c.integration_level) : h("span", { class: "down" }, "no · level " + (c.integration_level || 0)),
              integ ? integ.note : null)),
          h("div", { class: "rt-sel-f" }, h("span", { class: "rt-score" }, "score ", h("b", { class: "mono" }, c.score.toFixed(3))), scoreBar(c, 260),
            h("span", { class: "spacer" }), h("span", { class: "dim mono", title: c.listing_id }, "listing " + String(c.listing_id).slice(0, 28))));
      }

      function whyList(d) {
        const c = d.selected;
        const items = [];
        for (const f of FACTORS) {
          const x = c.factors[f.key];
          if (!x) continue;
          if (f.nodata) { items.push(h("li", { class: "nodata" }, h("b", {}, f.label), " — no data, not used (no execution history yet)")); continue; }
          if (!x.weight) continue;
          items.push(h("li", { class: x.imputed ? "imp" : null }, h("i", { class: "sw", style: { background: f.color } }), h("b", {}, f.label), " ", x.note.replace(/ \(scored neutral 0\.5\)$/, ""),
            x.imputed ? h("span", { class: "badge warn" }, "imputed 0.5") : null,
            h("span", { class: "rt-contrib mono" }, "+" + x.contribution.toFixed(3))));
        }
        return h("ul", { class: "rt-why" }, items);
      }

      function factorTable(d) {
        const c = d.selected;
        const rows = FACTORS.map(f => ({ f, x: c.factors[f.key] || {} }));
        return OG.table({
          columns: [
            { key: "factor", label: "Factor", value: r => r.f.label, fmt: (v, r) => h("span", { class: "rt-fl" }, h("i", { class: "sw", style: { background: r.f.nodata ? "transparent" : r.f.color, border: r.f.nodata ? "1px dashed #4f5a66" : null } }), r.f.label), sort: false },
            { key: "kind", label: "Kind", sort: false, fmt: (v, r) => r.x.kind === "observed" || r.x.kind === "inferred" ? OG.kindBadge(r.x.kind) : r.x.kind ? OG.badge(r.x.kind) : h("span", { class: "dim" }, "–") },
            { key: "weight", label: "Weight", num: true, sort: false, value: r => r.x.weight, fmt: (v, r) => r.f.nodata ? h("span", { class: "dim" }, "0 · locked") : r.x.weight ? (r.x.weight * 100).toFixed(1) + "%" : h("span", { class: "dim" }, "0") },
            { key: "value", label: "Value 0–1", num: true, sort: false, value: r => r.x.value, fmt: (v, r) => r.x.value != null ? r.x.value.toFixed(3) : OG.na(r.f.nodata ? "no data — not used" : "insufficient data") },
            { key: "contribution", label: "Contribution", num: true, sort: false, value: r => r.x.contribution, fmt: (v, r) => r.x.contribution ? h("span", {}, "+" + r.x.contribution.toFixed(3), r.x.imputed ? h("span", { class: "rt-imp", title: "No data for this listing: scored a neutral 0.5" }, "*") : null) : h("span", { class: "dim" }, "–") },
          ],
          rows, compact: true, csv: false, rowClass: r => (r.f.nodata || !r.x.weight) ? "out" : "",
        });
      }

      function candidatesTable(cands, d, title, extraToolbar) {
        const top = cands[0];
        return OG.table({
          title, toolbar: extraToolbar || [],
          columns: [
            { key: "rank", label: "#", num: true, width: "28px" },
            { key: "provider", label: "Provider", fmt: (v, r) => OG.providerLink(v) },
            { key: "gpu", label: "Variant", hidden: new Set(cands.map(c => c.gpu)).size < 2, fmt: v => OG.gpuLink(v), title: "allow_variants: each candidate keeps its own GPU; prices are per variant, never merged" },
            { key: "sku", label: "SKU", cls: "mono dim" },
            { key: "region", label: "Location", value: r => regionText(r), cls: "dim" },
            { key: "gpu_count", label: "GPUs", num: true },
            { key: "price_per_gpu_hour", label: "$/GPU·h", num: true, fmt: v => fmt.price(v), title: "Observed market price" },
            { key: "price_per_instance_hour", label: "$/h", num: true, fmt: v => fmt.price(v) },
            { key: "available", label: "Stock", fmt: (v, r) => availText(r) },
            { key: "age_seconds", label: "Seen", num: true, fmt: v => fmt.age(v) },
            { key: "integration_level", label: "Lvl", num: true, title: "OpenGrid integration level (2+ = can provision)", fmt: v => h("span", { class: v >= 2 ? "up" : "dim" }, v) },
            { key: "score", label: "Score", num: true, desc: true, fmt: v => v != null ? v.toFixed(3) : "–" },
            { key: "bar", label: "Breakdown", sort: false, csv: false, fmt: (v, r) => scoreBar(r, 150) },
            { key: "vs_selected", label: "vs #1", cls: "wrap dim", value: r => r === top ? "selected" : r.vs_selected, fmt: (v, r) => r === top ? h("b", { class: "up" }, "selected") : v || "–" },
          ],
          rows: cands, sort: { key: "rank", dir: "asc" }, compact: true, csv: "opengrid-route-candidates.csv",
          rowKey: r => r.provider + ":" + r.listing_id, rowClass: r => r === top ? "rt-top" : "",
        });
      }

      function exclusionsBlock(d) {
        const byCode = Object.entries(d.exclusions_by_code || {}).sort((a, b) => b[1] - a[1]);
        const filt = { code: null };
        const tbl = OG.table({
          columns: [
            { key: "provider", label: "Provider", fmt: v => OG.providerLink(v) },
            { key: "listing_id", label: "Listing", cls: "mono dim", fmt: v => String(v).slice(0, 34) },
            { key: "gpu_count", label: "GPUs", num: true },
            { key: "region", label: "Region", cls: "dim" },
            { key: "price_per_gpu_hour", label: "$/GPU·h", num: true, fmt: v => fmt.price(v) },
            { key: "code", label: "Code", fmt: v => OG.badge(EXCL[v] || v) },
            { key: "reason", label: "Reason", cls: "wrap dim" },
          ],
          rows: d.exclusions || [], sort: { key: "code", dir: "asc" }, compact: true, csv: "opengrid-route-exclusions.csv", empty: "Nothing was excluded.",
          title: `Exclusions · ${d.exclusions_total}${(d.exclusions || []).length < d.exclusions_total ? ` (first ${(d.exclusions || []).length} shown; all in the audit record)` : ""}`,
        });
        const chips = h("div", { class: "bar rt-exchips" }, byCode.map(([code, n]) => h("button", { class: "chip", type: "button", "aria-pressed": "false",
          onclick: e => {
            filt.code = filt.code === code ? null : code;
            chips.querySelectorAll("button").forEach(b => b.setAttribute("aria-pressed", "false"));
            if (filt.code) e.currentTarget.setAttribute("aria-pressed", "true");
            tbl.update((d.exclusions || []).filter(x => !filt.code || x.code === filt.code));
          } }, EXCL[code] || code, " ", h("b", { class: "mono" }, n))));
        return h("div", {}, chips, tbl);
      }

      function renderPreview(d, meta) {
        const kids = [];
        const req = [`${d.count}× ${OG.shortGpu(d.gpu)}`, d.region_group || "any region", st.lastBody.max_price_per_gpu_hour ? "≤ " + fmt.price(st.lastBody.max_price_per_gpu_hour) + "/GPU·h" : "no max",
          st.lastBody.duration_hours ? st.lastBody.duration_hours + " h" : st.lastBody.deadline_hours ? "deadline " + st.lastBody.deadline_hours + " h" : "open-ended", d.mode];
        kids.push(h("div", { class: "rt-reqbar" },
          h("span", { class: "eyebrow" }, "Preview"), h("span", { class: "mono" }, req.join("  ·  ")),
          meta && meta.kind ? OG.kindBadge(meta.kind) : null,
          h("span", { class: "spacer" }),
          d.live_provisioning_enabled ? OG.badge("live provisioning ON", "warn", "POST /v1/route on this server can launch paid instances") : OG.badge("live provisioning off here", null, "POST /v1/route on this server checks and quotes but never launches"),
          h("a", { class: "lnk mono rt-audit", href: "/v1/route/" + d.route_request_id, target: "_blank", rel: "external noopener", title: "The audit record: request, every candidate's factors, every exclusion" }, d.route_request_id, " ↗")));
        if (!d.selected) {
          kids.push(OG.insufficient(d.reason || "no eligible listing satisfies the request", "No route"));
          if (d.multi_instance_alternatives && d.multi_instance_alternatives.length) kids.push(multiBlock(d));
          kids.push(OG.section("Why nothing qualified", exclusionsBlock(d)));
          out.replaceChildren(...kids);
          return;
        }
        if (d.provisioning_note || !d.can_provision_selected) {
          kids.push(h("div", { class: "rt-warn" }, h("b", {}, "Provisioning: "),
            d.can_provision_selected ? "" : `OpenGrid cannot launch on ${OG.providerName(d.selected.provider)} (integration level ${d.selected.integration_level || 0}: ${LEVEL[d.selected.integration_level || 0]}). `,
            d.best_provisionable ? h("span", {}, "Best provisionable alternative: ", h("b", {}, OG.providerName(d.best_provisionable.provider)), ` at ${fmt.price(d.best_provisionable.price_per_gpu_hour)}/GPU·h (rank ${d.best_provisionable.rank}).`) :
              d.provisioning_note ? d.provisioning_note + "." : "",
            " Route & provision would skip non-provisionable candidates."));
        }
        kids.push(selectedCard(d));
        if (d.best_provisionable) kids.push(h("div", { class: "rt-bp" }, h("span", { class: "eyebrow" }, "Best provisionable"), OG.providerLink(d.best_provisionable.provider),
          h("span", { class: "mono" }, fmt.price(d.best_provisionable.price_per_gpu_hour) + "/GPU·h"), OG.conceptBadge("observed"), h("span", { class: "dim" }, regionText(d.best_provisionable)),
          h("span", { class: "dim" }, "rank " + d.best_provisionable.rank + " · score " + d.best_provisionable.score.toFixed(3)), scoreBar(d.best_provisionable, 140), levelBadge(d.best_provisionable.integration_level)));
        kids.push(h("div", { class: "cols-2 rt-two" },
          OG.section("Why this route", whyList(d), h("p", { class: "note" }, "Every factor is normalized 0–1 and weighted by the mode. A factor with thin history is scored a neutral 0.5 and flagged; reliability and performance are never invented. ",
            h("a", { class: "lnk", href: "/methodology/best-execution" }, "Method →"))),
          OG.section("Factor breakdown · selected", factorTable(d))));
        const all = [d.selected, ...(d.alternatives || [])];
        const moreBtn = h("button", { class: "btn sm", type: "button", title: "Load every ranked candidate from the audit record" }, "All candidates");
        const candBox = h("div", {}, legend(d.weights), candidatesTable(all, d, `Ranked candidates · top ${all.length}`, [moreBtn]));
        moreBtn.addEventListener("click", async () => {
          moreBtn.disabled = true; moreBtn.textContent = "Loading…";
          try {
            const rec = await ctx.api("/v1/route/" + d.route_request_id);
            const cs = (rec.decision && rec.decision.candidates) || [];
            candBox.replaceChildren(legend(d.weights), candidatesTable(cs, d, `Ranked candidates · all ${cs.length} (from the audit record)`));
          } catch (e) { moreBtn.replaceWith(OG.error(e)); }
        });
        kids.push(OG.section("Alternatives & score contributions", candBox));
        if (d.multi_instance_alternatives && d.multi_instance_alternatives.length) kids.push(multiBlock(d));
        kids.push(OG.section("Excluded listings", exclusionsBlock(d)));
        kids.push(h("p", { class: "note" }, "Prices: candidates carry ", h("b", {}, "observed market prices"), "; the route carries a ", h("b", {}, "quote"),
          "; a deployment carries the ", h("b", {}, "execution price"), " only once a provider reports one. ", h("a", { class: "lnk", href: "/methodology/data-kinds" }, "Data kinds →")));
        out.replaceChildren(...kids);
      }

      function multiBlock(d) {
        return OG.section("Multi-instance shapes (not auto-provisioned)", OG.table({
          columns: [
            { key: "provider", label: "Provider", fmt: v => OG.providerLink(v) },
            { key: "sku", label: "SKU", cls: "mono dim" },
            { key: "instances", label: "Instances", num: true, fmt: (v, r) => `${v} × ${r.gpu_count} GPU` },
            { key: "price_per_gpu_hour", label: "$/GPU·h", num: true, fmt: v => fmt.price(v) },
            { key: "region", label: "Location", value: r => regionText(r), cls: "dim" },
            { key: "score", label: "Score", num: true, fmt: v => v.toFixed(3) },
            { key: "note", label: "Note", cls: "wrap dim" },
          ], rows: d.multi_instance_alternatives, sort: { key: "rank", dir: "asc" }, compact: true, csv: false,
        }));
      }

      /* ----- route (spends money where live provisioning is on) ----- */
      async function routeNow() {
        if (!st.gpu) { out.replaceChildren(OG.empty("Pick a GPU first.")); return; }
        if (!st.last || !dirty.hidden) await preview();
        const d = st.last;
        if (!d) return;
        const q = d.quote || {};
        const live = d.live_provisioning_enabled;
        const b = body();
        const est = q.expected_cost_usd != null ? `${OG.money(q.expected_cost_usd)} (${fmt.price(q.price_per_gpu_hour)}/GPU·h × ${d.count} GPUs × ${q.hours} h)`
          : q.price_per_hour != null ? `${OG.money(q.price_per_hour)} per hour, open-ended: billed until the deployment is terminated` : "no quote (no eligible listing)";
        const ok = await OG.dialog({
          title: live ? "Route & provision — this spends real money" : "Route — live provisioning is off on this server",
          danger: true, ack: live ? "I understand this can launch a paid instance billed to this account" : "I understand that routing can spend real money where live provisioning is enabled",
          confirm: live ? "Route & provision" : "Route (check & quote only)",
          body: h("div", { class: "rt-dlg" },
            h("p", {}, "POST /v1/route checks live availability on each provisionable candidate in rank order, gets a quote, and ",
              live ? h("b", { class: "down" }, "launches a paid instance on the first one that passes.") : h("b", {}, "stops before launching: live provisioning is disabled in this environment, so nothing is launched or charged.")),
            h("table", { class: "rt-dlg-t" },
              h("tr", {}, h("th", {}, "Request"), h("td", { class: "mono" }, `${d.count}× ${OG.shortGpu(d.gpu)} · ${d.region_group || "any region"} · ${d.mode}`)),
              h("tr", {}, h("th", {}, "Estimated cost"), h("td", { class: "mono" }, est)),
              h("tr", {}, h("th", {}, "Price cap"), h("td", {}, b.max_price_per_gpu_hour ? `live quote must be ≤ ${fmt.price(b.max_price_per_gpu_hour)}/GPU·h` : h("span", { class: "warn-t" }, "none set: the live quote is not capped"))),
              h("tr", {}, h("th", {}, "Top candidate"), h("td", {}, d.selected ? `${OG.providerName(d.selected.provider)} — ${d.can_provision_selected ? "provisionable" : "OpenGrid cannot provision it; it will be skipped"}` : "none"))),
            !d.can_provision_selected && !d.best_provisionable ? h("p", { class: "warn-t" }, "No candidate in this preview is on a provider OpenGrid can provision; expect status not_provisioned.") : null,
            h("p", { class: "dim" }, "The estimate uses the observed listing price. The live quote can differ; the result shows it."))
        });
        if (!ok) return;
        routed.replaceChildren(OG.loading("Routing: checking live availability and quoting…"));
        try {
          const r = await ctx.api("/v1/route", { method: "POST", body: b, full: true });
          renderRouted(r.data);
        } catch (e) { routed.replaceChildren(OG.error(e)); }
      }

      function renderRouted(r) {
        const S = {
          provisioned: ["PROVISIONED", "good"], not_provisioned: ["NOT PROVISIONED", "warn"], failed: ["FAILED", "bad"], no_candidates: ["NO CANDIDATES", "bad"],
        }[r.status] || [String(r.status).toUpperCase(), ""];
        const dep = r.deployment;
        let line;
        if (r.status === "provisioned" && dep) line = h("span", {}, "Provider accepted the launch: deployment ", h("a", { class: "lnk mono", href: "/deployments?id=" + dep.deployment_id }, dep.deployment_id), ` on ${OG.providerName(dep.provider)}, status ${dep.status}.`);
        else if (r.status === "not_provisioned") line = h("span", {}, h("b", {}, r.reason || "not provisioned"), ". No instance was launched", dep ? "." : " and no deployment record was created. Nothing was charged.");
        else if (r.status === "failed") line = h("span", {}, h("b", { class: "down" }, r.reason || "failed"), dep ? h("span", {}, " · deployment ", h("a", { class: "lnk mono", href: "/deployments?id=" + dep.deployment_id }, dep.deployment_id)) : null);
        else line = h("span", {}, r.reason || "");
        const q = r.quote;
        const moved = q && r.selected && q.provider !== r.selected.provider;
        const kids = [
          h("div", { class: "rt-res-h" }, h("span", { class: "eyebrow" }, "Route result"), OG.badge(S[0], S[1]), line, h("span", { class: "spacer" }),
            h("a", { class: "lnk mono rt-audit", href: "/v1/route/" + r.route_request_id, target: "_blank", rel: "external noopener" }, r.route_request_id, " ↗"),
            h("button", { class: "btn sm", type: "button", onclick: () => routed.replaceChildren() }, "Dismiss")),
        ];
        if (q) kids.push(h("div", { class: "rt-res-q" },
          OG.conceptBadge("quote"), h("b", { class: "mono" }, fmt.price(q.price_per_gpu_hour) + "/GPU·h"),
          h("span", {}, "from ", OG.providerLink(q.provider)), h("span", { class: "mono dim" }, q.listing_id),
          h("span", { class: "dim" }, q.basis === "live_provider_api" ? "read from the provider API on this request" : "from the observed listing (no live price available)"),
          q.expected_cost_usd != null ? h("span", { class: "mono" }, "expected " + OG.money(q.expected_cost_usd)) : h("span", { class: "mono" }, OG.money(q.price_per_hour) + "/h"),
          q.availability ? h("span", { class: "dim" }, "live check: " + (q.availability.available === true ? "available" : q.availability.available === false ? "unavailable" : "unknown") + (q.availability.note ? " — " + q.availability.note : "")) : null));
        if (moved) kids.push(h("p", { class: "note" }, `Top-ranked ${OG.providerName(r.selected.provider)} could not be routed (see below); the quote is from the first candidate that passed the checks.`));
        if (r.considered && r.considered.length) kids.push(OG.table({
          title: "Candidates tried, in rank order",
          columns: [
            { key: "rank", label: "#", num: true },
            { key: "provider", label: "Provider", fmt: v => OG.providerLink(v) },
            { key: "listing_id", label: "Listing", cls: "mono dim", fmt: v => String(v).slice(0, 30) },
            { key: "step", label: "Step", cls: "mono" },
            { key: "outcome", label: "Outcome", fmt: v => OG.badge(v.replace(/_/g, " "), v === "ok" ? "good" : v === "not_attempted" ? "warn" : v === "failed" || v === "error" ? "bad" : "") },
            { key: "reason", label: "Reason", cls: "wrap dim" },
            { key: "quote", label: "Quote", num: true, fmt: v => v != null ? fmt.price(v) : "–" },
          ], rows: r.considered, sort: { key: "rank", dir: "asc" }, compact: true, csv: false,
        }));
        routed.replaceChildren(h("div", { class: "box rt-res rt-res-" + r.status }, kids));
      }

      // capabilities (provisionable marks) + GPU list in the background
      ctx.api("/v1/capabilities").then(caps => drawEx(new Map(caps.map(c => [c.provider, c.level_implemented]))), () => {});
      [gpus, fams] = await Promise.all([OG.data.gpus().catch(() => []), OG.data.families()]);
      if (!ctx.alive()) return;
      const famHit = st.gpu ? fams.find(f => [f.slug, f.id, f.name].filter(Boolean).some(k => OG.slug(k) === OG.slug(st.gpu) || String(k).toLowerCase() === st.gpu.toLowerCase())) : null;
      if (famHit && !gpus.some(g => g.slug === st.gpu)) { await pickFamily(famHit); if (ctx.alive()) preview(); }
      else if (st.gpu) {
        const g = await OG.resolveGpu(st.gpu);
        if (!ctx.alive()) return;
        if (g) { st.gpu = g.slug; st.gpuName = g.name; gpuIn.value = g.short; gpuFull.textContent = g.name + (g.live ? ` · ${g.weight} providers live` : " · no live listings"); sync(); preview(); }
        else out.replaceChildren(OG.insufficient(`"${st.gpu}" does not match a canonical GPU. OpenGrid never guesses a mapping; pick one from the list.`, "Unknown GPU"));
      } else {
        out.replaceChildren(h("div", { class: "rt-empty" },
          h("p", {}, "Describe a workload and OpenGrid ranks every eligible listing with the scoring shown: price, availability, freshness, history and whether OpenGrid can provision it."),
          h("p", { class: "dim" }, "Try: ", ["h100-80gb-sxm5:8:US:2.5", "h200-141gb-sxm5:1::", "a100-80gb-sxm4:8::"].map(s => {
            const [g, n, r, m] = s.split(":");
            return h("a", { class: "lnk mono rt-ex-l", href: "/route" + OG.qs.stringify({ gpu: g, count: n, region: r || null, max: m || null }) }, `${n}× ${g}${r ? " " + r : ""}${m ? " ≤ $" + m : ""}`);
          })),
          h("p", { class: "dim" }, "From anywhere: press ", h("kbd", {}, "/"), " and type ", h("code", { class: "mono" }, "r h100 8 us"), ".")));
      }
    },
  });
})();
