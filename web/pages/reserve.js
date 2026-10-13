/* /reserve — reserved-compute price-lock requests (methodology/reservations.md).
   /reserve       your requests (GET /v1/reservations; the operator can switch to every account via
                  GET /v1/admin/reservations) + the new-request form with the INDICATIVE reference for the GPU
                  (GET /v1/reservations/reference) and Submit (POST /v1/reservations/requests, one Idempotency-Key
                  per intent).
   /reserve/:id   one request: status strip, the indicative reference captured when it was made, the offer (price,
                  validity, terms, supply basis), Accept (route:execute, records intent only) / Withdraw, the
                  operator's Review / Offer / Decline / Fulfil / Cancel, and every event.
   Nothing here is a quote, a binding price or a guaranteed reservation: OpenGrid has no provider capacity
   commitments of its own, so an offer exists only when the operator records the supply basis behind it, and an
   acceptance launches nothing and bills nothing. Pure helpers live on OG.reserve (tests/test_web_reserve.js). */
(() => {
  const { h, fmt } = OG;

  /* =============================== pure =============================== */
  // reservations/states.py
  const FLOW = ["requested", "under_review", "offered", "accepted", "fulfilled"];
  const CLOSED = ["declined", "expired", "withdrawn", "cancelled"];
  const OPEN = ["requested", "under_review", "offered"];
  const TERMINAL = ["declined", "expired", "withdrawn", "fulfilled", "cancelled"];
  const BANNER = "Price-lock requests are reviewed by OpenGrid. An offer is made only when provider capacity backs it. This is a commercial order for compute delivered by OpenGrid — not a financial contract, not tradable, no cash settlement.";
  const ACCEPTED = "Accepted — OpenGrid will contact you to sign the order form; nothing is billed by this action.";
  const REGIONS = ["US", "Canada", "Europe", "UK", "APAC", "Middle East", "LATAM", "Africa"];
  // reservations/service.py bounds (the server re-validates and is the authority)
  const BOUNDS = { gpu_count: { min: 1, max: 1024 }, total_gpu_hours: { min: 1, max: 10000000 }, max_price_per_gpu_hour: { min_exclusive: 0, max: 1000 }, notes: 2000 };

  function tone(s) {
    if (s === "accepted" || s === "fulfilled") return "good";
    if (s === "offered") return "busy";
    if (s === "declined" || s === "cancelled") return "bad";
    if (s === "expired" || s === "withdrawn") return "warn";
    return "";
  }
  // The strip: the main flow, with a closed state (declined / expired / withdrawn / cancelled) inserted after the
  // furthest main-flow state the history reached.
  function strip(status, history) {
    const seen = new Set((history || []).filter(Boolean));
    const cur = FLOW.indexOf(status);
    let after = cur;
    if (cur < 0) { after = 0; FLOW.forEach((s, i) => { if (seen.has(s)) after = i; }); }
    const steps = FLOW.map((s, i) => {
      let mark = i < (cur >= 0 ? cur : after + 1) ? "done" : "todo";
      if (i === cur) mark = "current";
      if (mark === "done" && seen.size && !seen.has(s)) mark = "skipped";
      return { state: s, mark, tone: mark === "current" ? tone(s) : "" };
    });
    if (cur < 0 && status) steps.splice(after + 1, 0, { state: status, mark: "current", tone: tone(status) || "bad", off: true });
    return steps;
  }
  const isTerminal = r => !!r && (r.terminal === true || TERMINAL.includes(r.status));

  function usd(v, digits) {
    if (v == null || v === "" || !isFinite(+v)) return "–";
    const d = digits == null ? 2 : digits;
    return "$" + (+v).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
  }
  const perGpuH = v => (v == null || !isFinite(+v) ? "–" : "$" + (+v).toFixed(+v < 10 ? 3 : 2) + "/GPU·h");
  const hoursText = v => (v == null || !isFinite(+v) ? "–" : (+v).toLocaleString("en-US", { maximumFractionDigits: 2 }) + " GPU·h");

  // <input type="datetime-local"> value (local time, no zone) -> ISO with zone; "" -> null
  function localToIso(v) {
    if (!v) return null;
    const d = new Date(v);
    return isNaN(d) ? null : d.toISOString();
  }
  function isoToLocal(iso) {
    const d = new Date(iso);
    if (isNaN(d)) return "";
    const p = n => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
  }
  function defaults(now) {
    const t = new Date(now || Date.now());
    t.setMinutes(0, 0, 0);
    const a = new Date(+t + 7 * 864e5), b = new Date(+t + 14 * 864e5);
    return { gpu: "", gpu_count: 8, total_gpu_hours: 5376, start_after: isoToLocal(a.toISOString()), start_before: isoToLocal(b.toISOString()),
      region_group: "", max_price_per_gpu_hour: 2.5, notes: "" };
  }
  const numOrNull = v => (v == null || v === "" || !isFinite(+v) ? null : +v);
  // form -> {ok, errors: {field: msg}, body}. Mirrors reservations/service.validate.
  function validate(f, now) {
    const errors = {};
    const n = now || Date.now();
    if (!f.gpu) errors.gpu = "pick one GPU variant";
    const c = numOrNull(f.gpu_count), hrs = numOrNull(f.total_gpu_hours), p = numOrNull(f.max_price_per_gpu_hour);
    if (c == null || !Number.isInteger(c)) errors.gpu_count = "a whole number of GPUs";
    else if (c < BOUNDS.gpu_count.min || c > BOUNDS.gpu_count.max) errors.gpu_count = `1 – ${BOUNDS.gpu_count.max}`;
    if (hrs == null) errors.total_gpu_hours = "required";
    else if (hrs < BOUNDS.total_gpu_hours.min || hrs > BOUNDS.total_gpu_hours.max) errors.total_gpu_hours = `1 – ${BOUNDS.total_gpu_hours.max.toLocaleString("en-US")}`;
    else if (c != null && hrs < c) errors.total_gpu_hours = "at least one hour on each GPU";
    else if (Math.round(hrs * 100) / 100 !== hrs) errors.total_gpu_hours = "at most 2 decimals";
    if (p == null) errors.max_price_per_gpu_hour = "your limit is required";
    else if (p <= 0 || p > BOUNDS.max_price_per_gpu_hour.max) errors.max_price_per_gpu_hour = "> $0 and ≤ $1000";
    const a = localToIso(f.start_after), b = localToIso(f.start_before);
    if (!a) errors.start_after = "required";
    else if (+new Date(a) < n - 5 * 60e3) errors.start_after = "in the past";
    if (!b) errors.start_before = "required";
    else if (a && +new Date(b) - +new Date(a) < 3600e3) errors.start_before = "≥ 1 hour after start after";
    else if (+new Date(b) > n + 731 * 864e5) errors.start_before = "within 2 years";
    if (f.notes && f.notes.length > BOUNDS.notes) errors.notes = `≤ ${BOUNDS.notes} characters`;
    const body = { gpu: f.gpu, gpu_count: c, total_gpu_hours: hrs, start_after: a, start_before: b, max_price_per_gpu_hour: p };
    if (f.region_group) body.region_group = f.region_group;
    if (f.notes && f.notes.trim()) body.notes = f.notes.trim();
    return { ok: !Object.keys(errors).length, errors, body };
  }
  // The indicative reference -> rows for display, every one labelled indicative (never a quote).
  function refRows(ref) {
    if (!ref) return [];
    const rows = [];
    const ix = ref.index || {};
    rows.push({ key: "index", label: "OpenGrid index", value: ix.level != null ? ix.level : null, raw: ix.raw_level, reason: ix.reason || (ix.index_id ? "not published now" : "no index for this GPU"), link: ix.link || null, sub: ix.hour ? "hour " + ix.hour : null });
    const best = (ref.best_on_demand || {}).listings || [];
    rows.push({ key: "best", label: "Best on-demand listing", value: best.length ? best[0].price_per_gpu_hour : null, reason: (ref.best_on_demand || {}).reason || "no listing", provider: best.length ? best[0].provider : null });
    const fx = ref.settlement_fixing || {};
    const d = fx.daily || fx.hourly;
    rows.push({ key: "fixing", label: "Latest settlement fixing", value: d && d.value != null ? d.value : null, reason: fx.reason || (d ? d.reason || "not published" : "none yet"), sub: d ? `${d.fixing_type} · ${d.fixing_time || ""} · ${d.status || ""}` : null, link: d && d.link ? d.link : null });
    return rows.map(r => Object.assign(r, { label_kind: "indicative" }));
  }
  function offerOf(r) { return r && r.offer && r.offer.offer_seq ? r.offer : null; }

  OG.reserve = { FLOW, CLOSED, OPEN, TERMINAL, BANNER, ACCEPTED, REGIONS, BOUNDS, tone, strip, isTerminal, usd, perGpuH, hoursText, localToIso, isoToLocal,
    defaults, validate, refRows, offerOf };

  OG.command(/^rsv$/i, () => OG.go("/reserve"), "rsv              price-lock requests (reserve)");

  if (typeof OG.page !== "function" || typeof document === "undefined") return;   // node tests: pure part only

  /* =============================== DOM helpers =============================== */
  const R = OG.reserve;
  const badge = s => h("span", { class: "badge og-st st-" + (tone(s) || "plain") }, String(s || "–").replace(/_/g, " "));
  const indBadge = () => OG.badge("indicative", "warn", "Observed public prices and OpenGrid index levels. Not a quote, not an offer, not a binding price.");
  const kv = rows => h("table", { class: "dep-kv" }, rows.filter(Boolean).map(([k, v, title]) => h("tr", { title: title || null }, h("th", {}, k), h("td", {}, v == null || v === "" ? h("span", { class: "dim" }, "–") : v))));
  const banner = () => h("div", { class: "rs-ban", role: "note" }, h("b", {}, "NOT A FINANCIAL CONTRACT"), h("span", {}, BANNER), h("a", { class: "lnk", href: "/methodology/reservations" }, "How price-lock requests work →"));
  const notYet = (what, e) => e && (e.status === 404 || e.status === 405) && !(e.body && e.body.detail && e.body.detail.code) ? OG.insufficient(what + " is not available on this server yet.", "Not available yet") : apiErr(e, what);
  function apiErr(e, what) {
    const d = e && e.body && e.body.detail;
    const o = d && typeof d === "object" && !Array.isArray(d) ? d : {};
    const fields = Array.isArray(d) ? d.map(x => ({ field: (x.loc || []).join("."), message: x.msg })) : null;
    return h("div", { class: "og-alarm" + (e && e.status >= 500 ? "" : " t-warn") }, h("b", {}, o.code ? o.code.replace(/_/g, " ") : e && e.status ? "HTTP " + e.status : "Error"),
      h("span", {}, o.message || (e && e.message) || "request failed"), o.field ? h("span", { class: "mono dim" }, "field " + o.field) : null,
      what ? h("span", { class: "mono dimmer" }, what) : null,
      fields ? h("ul", { class: "jb-dlg-l" }, fields.map(x => h("li", {}, x.field + ": " + x.message))) : null,
      e && e.status === 403 ? h("span", { class: "dim" }, "Requests need the route:preview scope; accepting an offer needs route:execute. ", h("a", { class: "lnk", href: "/keys" }, "API keys →")) : null);
  }
  R.apiErr = apiErr;

  function refBlock(ref, title) {
    if (!ref) return OG.section(title, OG.empty("No reference recorded."));
    const ix = ref.index || {}, best = ref.best_on_demand || {}, fx = ref.settlement_fixing || {};
    const fixing = fx.daily || fx.hourly;
    const lv = ref.limit_vs_index;
    const stats = OG.stats([
      { label: "Index level", value: ix.level != null ? perGpuH(ix.level) : null, reason: ix.reason || "not published", sub: ix.index_id ? h("a", { class: "lnk mono", href: "/indices/" + encodeURIComponent(ix.index_id) }, ix.index_id) : "no index", title: ix.raw_level != null ? "raw level " + perGpuH(ix.raw_level) + " · " + (ix.constituents || 0) + " constituents" : null },
      { label: "Raw level", value: ix.raw_level != null ? perGpuH(ix.raw_level) : null, reason: "not computed", sub: ix.constituents != null ? ix.constituents + " constituents" : null },
      { label: "Best on-demand", value: best.listings && best.listings.length ? perGpuH(best.listings[0].price_per_gpu_hour) : null, reason: best.reason || "no listing", sub: best.listings && best.listings.length ? OG.providerName(best.listings[0].provider) : null },
      { label: "Market median", value: best.market && best.market.median != null ? perGpuH(best.market.median) : null, reason: "no market", sub: best.market && best.market.providers != null ? best.market.providers + " providers" : null },
      { label: "Settlement fixing", value: fixing && fixing.value != null ? perGpuH(fixing.value) : null, reason: fx.reason || (fixing ? fixing.reason || "not published" : "none yet"), sub: fixing ? fixing.fixing_type + " · " + fmt.dateTime(fixing.fixing_time) + " · " + fixing.status : null },
      lv ? { label: "Your limit vs index", value: (lv.pct >= 0 ? "+" : "") + (lv.pct * 100).toFixed(1) + "%", sub: perGpuH(lv.customer_limit) } : null,
    ]);
    const tbl = best.listings && best.listings.length ? h("table", { class: "grid-t rs-best" },
      h("thead", {}, h("tr", {}, ["Provider", "GPUs / instance", "Region", "$/GPU·h", "Available", "Observed"].map((c, i) => h("th", { class: i >= 3 ? "num" : null }, c)))),
      h("tbody", {}, best.listings.map(l => h("tr", {}, h("td", {}, OG.providerLink(l.provider)), h("td", { class: "mono" }, String(l.gpu_count)),
        h("td", { class: "dim" }, [l.region, l.country].filter(Boolean).join(", ") || "not stated"), h("td", { class: "num mono" }, perGpuH(l.price_per_gpu_hour)),
        h("td", { class: "num" }, l.available === true ? "yes" : l.available === false ? "no" : h("span", { class: "dim" }, "unknown")),
        h("td", { class: "num dim" }, l.observed_at ? fmt.age(l.observed_at) + " ago" : "–"))))) : null;
    return OG.section(h("span", {}, title, " ", indBadge()), stats, tbl,
      h("p", { class: "note" }, ref.note || "Indicative only.", ref.as_of ? " As of " + fmt.dateTime(ref.as_of) + "." : "", " ",
        fixing && fixing.verify ? h("a", { class: "lnk", href: fixing.link }, "Fixing evidence →") : null, " ",
        h("a", { class: "lnk", href: "/methodology/indices" }, "Index methodology →")));
  }
  R.refBlock = refBlock;

  /* =============================== /reserve =============================== */
  OG.page("/reserve", {
    title: "Reserve",
    nav: "reserve",
    async mount(el, params, query, ctx) {
      const root = h("div", { class: "pg-jobs pg-route pg-dep pg-rsv" });   // reuse the jobs / ticket / table styles
      el.append(root);
      const me = await OG.me();
      if (!ctx.alive()) return;
      const admin = OG.isAdmin(me);
      let scope = admin && query.all === "1" ? "all" : "mine";
      const stats = h("div"), holder = h("div", {}, OG.loading("Loading price-lock requests…"));
      const scopeSeg = admin ? OG.seg([["mine", "Mine"], ["all", "All accounts", "GET /v1/admin/reservations (admin)"]], scope, v => { scope = v; OG.qs.set({ all: v === "all" ? "1" : null }); tbl = makeTable(); load(); }) : null;
      root.append(
        OG.head("Reserve", "Price-lock requests for reserved compute · reviewed by OpenGrid · indicative references, never quotes",
          scopeSeg, h("button", { class: "btn", type: "button", onclick: () => load() }, "Reload"),
          h("a", { class: "btn", href: "/methodology/reservations" }, "Methodology")),
        banner());

      /* ----- the form ----- */
      const f = defaults();
      const errs = {};
      const errEl = k => (errs[k] = errs[k] || h("span", { class: "jb-err", "aria-live": "polite" }));
      const field = (label, input, hint, k) => h("label", { class: "rt-f" }, h("span", { class: "rt-l" }, label), input, k ? errEl(k) : null, hint ? h("span", { class: "rt-h" }, hint) : null);
      const bind = (k, input) => { input.addEventListener("input", () => { f[k] = input.value; changed(k); }); input.addEventListener("change", () => { f[k] = input.value; changed(k); }); return input; };
      const gpuSel = bind("gpu", h("select", { class: "field jb-wide", "aria-label": "GPU" }, h("option", { value: "" }, "Loading GPUs…")));
      const countIn = bind("gpu_count", h("input", { class: "field num", type: "number", min: 1, max: BOUNDS.gpu_count.max, step: 1, value: f.gpu_count, "aria-label": "GPU count" }));
      const hoursIn = bind("total_gpu_hours", h("input", { class: "field num", type: "number", min: 1, step: "any", value: f.total_gpu_hours, "aria-label": "Total GPU-hours" }));
      const aIn = bind("start_after", h("input", { class: "field", type: "datetime-local", value: f.start_after, "aria-label": "Start after" }));
      const bIn = bind("start_before", h("input", { class: "field", type: "datetime-local", value: f.start_before, "aria-label": "Start before" }));
      const regionSel = bind("region_group", h("select", { class: "field", "aria-label": "Region group" }, h("option", { value: "" }, "Any region"), REGIONS.map(g => h("option", { value: g }, g))));
      const priceIn = bind("max_price_per_gpu_hour", h("input", { class: "field num", type: "number", min: 0.01, max: 1000, step: 0.01, value: f.max_price_per_gpu_hour, "aria-label": "Max price per GPU-hour" }));
      const notesIn = bind("notes", h("textarea", { class: "field jb-ssh", rows: 2, maxlength: BOUNDS.notes, placeholder: "Optional: workload, interconnect, contract length, contact…", "aria-label": "Notes" }));
      const totalLine = h("span", { class: "rt-h" });
      const formErr = h("div", { class: "jb-ferr" });
      const submitBtn = h("button", { class: "btn pri", type: "submit", title: "POST /v1/reservations/requests — reviewed by OpenGrid; nothing is reserved, launched or billed" }, "Submit request…");
      const form = h("form", { class: "box jb-form", onsubmit: e => { e.preventDefault(); submit(); } },
        h("div", { class: "rt-tt" }, h("b", {}, "New price-lock request"), h("span", { class: "dimmer" }, " · free · reviewed by OpenGrid")),
        field("GPU", gpuSel, "One GPU variant (families and auto are not accepted for a price lock)", "gpu"),
        h("div", { class: "rt-row2" }, field("GPUs", countIn, "used at the same time", "gpu_count"), field("Total GPU-hours", hoursIn, null, "total_gpu_hours")),
        h("div", { class: "rt-row2" }, field("Start after", aIn, "your local time", "start_after"), field("Start before", bIn, "start window end", "start_before")),
        h("div", { class: "rt-row2" }, field("Region", regionSel, null, "region_group"), field("Your max $/GPU·h", priceIn, "your limit, not a price", "max_price_per_gpu_hour")),
        totalLine,
        field("Notes", notesIn, null, "notes"),
        formErr,
        h("div", { class: "rs-acts" }, submitBtn, h("span", { class: "rt-h" }, "Nothing is reserved, launched or billed by submitting.")));
      const refBox = h("div", {}, h("div", { class: "jb-empty" }, h("p", {}, "Pick a GPU to see the indicative reference: the OpenGrid index level, the best on-demand listings now and the latest settlement fixing.")));
      const listSec = h("div");
      root.append(h("div", { class: "jb-grid" }, form, h("div", { class: "jb-main" }, refBox)), stats, listSec);
      listSec.append(holder);

      function drawTotal() {
        const v = validate(f);
        totalLine.textContent = v.body.total_gpu_hours && v.body.max_price_per_gpu_hour ? `At your limit: up to ${usd(v.body.total_gpu_hours * v.body.max_price_per_gpu_hour)} over ${hoursText(v.body.total_gpu_hours)} (your ceiling, not a price)` : "";
      }
      let refTimer = null, refKey = null;
      function changed(k) {
        for (const e of Object.values(errs)) e.textContent = "";
        formErr.replaceChildren();
        drawTotal();
        if (["gpu", "gpu_count", "region_group", "max_price_per_gpu_hour"].includes(k)) { clearTimeout(refTimer); refTimer = setTimeout(loadRef, 350); }
      }
      ctx.onCleanup(() => clearTimeout(refTimer));
      async function loadRef() {
        if (!f.gpu) return;
        const v = validate(f);
        const params = { gpu: f.gpu, gpu_count: v.body.gpu_count && v.body.gpu_count >= 1 ? v.body.gpu_count : 1 };
        if (f.region_group) params.region_group = f.region_group;
        if (v.body.max_price_per_gpu_hour > 0) params.max_price_per_gpu_hour = v.body.max_price_per_gpu_hour;
        const key = JSON.stringify(params);
        if (key === refKey) return;
        refKey = key;
        refBox.replaceChildren(OG.loading("Loading the indicative reference…"));
        try { const r = await ctx.api("/v1/reservations/reference", { params, slot: "rsv-ref" }); refBox.replaceChildren(refBlock(r, "Indicative reference now")); }
        catch (e) { if (!e.stale) { refKey = null; refBox.replaceChildren(notYet("GET /v1/reservations/reference", e)); } }
      }

      OG.data.gpus().then(list => {
        if (!ctx.alive()) return;
        const gs = (list || []).filter(g => g.name).sort((a, b) => (b.weight - a.weight) || a.short.localeCompare(b.short));
        const pick = query.gpu ? (gs.find(g => g.slug === query.gpu || g.name === query.gpu) || {}).name : (gs.find(g => /H100 80GB SXM/.test(g.name)) || gs[0] || {}).name;
        gpuSel.replaceChildren(h("option", { value: "" }, "Choose a GPU…"), gs.map(g => h("option", { value: g.slug, selected: g.name === pick ? true : null }, g.short + (g.weight ? `  · ${g.weight} providers` : ""))));
        f.gpu = gpuSel.value;
        drawTotal();
        loadRef();
      }).catch(() => gpuSel.replaceChildren(h("option", { value: "" }, "GPU list unavailable")));
      drawTotal();

      const I = { submit: null };
      async function submit() {
        const v = validate(f);
        for (const [k, e] of Object.entries(errs)) e.textContent = v.errors[k] || "";
        if (!v.ok) { formErr.replaceChildren(h("div", { class: "jb-ferr-b" }, "Fix the highlighted fields.")); return; }
        const b = v.body;
        const ok = await OG.dialog({
          title: "Submit price-lock request", confirm: "Submit request",
          body: h("div", { class: "jb-dlg" },
            h("p", {}, "OpenGrid reviews the request. ", h("b", {}, "An offer is made only when provider capacity backs it"), "; nothing is reserved, launched or billed by submitting."),
            h("table", { class: "rt-dlg-t" },
              h("tr", {}, h("th", {}, "GPU"), h("td", { class: "mono" }, `${b.gpu_count}× ${OG.shortGpu(OG.data.gpuName(b.gpu) || b.gpu)}`)),
              h("tr", {}, h("th", {}, "GPU-hours"), h("td", { class: "mono" }, hoursText(b.total_gpu_hours))),
              h("tr", {}, h("th", {}, "Start window"), h("td", { class: "mono" }, fmt.dateTime(b.start_after) + " → " + fmt.dateTime(b.start_before))),
              h("tr", {}, h("th", {}, "Region"), h("td", {}, b.region_group || "any")),
              h("tr", {}, h("th", {}, "Your limit"), h("td", { class: "mono" }, perGpuH(b.max_price_per_gpu_hour) + " · up to " + usd(b.max_price_per_gpu_hour * b.total_gpu_hours)))),
            h("p", { class: "dim" }, BANNER)),
        });
        if (!ok) return;
        I.submit = OG.intentFor(I.submit, "reservation:create", b);
        formErr.replaceChildren(OG.loading("Submitting…"));
        try {
          const r = await OG.api.intent(I.submit, "/v1/reservations/requests", { method: "POST", body: b });
          if (r && r.request_id) { OG.go("/reserve/" + encodeURIComponent(r.request_id)); return; }
          formErr.replaceChildren(h("div", { class: "og-ok" }, h("b", {}, "Submitted")));
          load();
        } catch (e) {
          formErr.replaceChildren(apiErr(e, "POST /v1/reservations/requests"), I.submit.state === "unknown" ? h("p", { class: "warn-t" }, "Outcome unknown. ",
            h("button", { class: "btn sm", type: "button", onclick: () => submit() }, "Retry (same Idempotency-Key)"), " — the server replays the first answer instead of creating a second request.") : null);
          const fld = e && e.body && e.body.detail && e.body.detail.field;
          if (fld && errs[fld]) errs[fld].textContent = e.body.detail.message;
        }
      }

      /* ----- the list ----- */
      const makeTable = () => OG.table({
        columns: [
          { key: "request_id", label: "Request", cls: "mono dim" },
          { key: "status", label: "Status", fmt: v => badge(v) },
          { key: "gpu", label: "GPU", fmt: (v, r) => h("span", {}, `${r.gpu_count}× `, OG.gpuLink(v)) },
          { key: "total_gpu_hours", label: "GPU·h", num: true, fmt: v => fmt.num(v) },
          { key: "region_group", label: "Region", fmt: v => v || h("span", { class: "dim" }, "any") },
          { key: "start_after", label: "Start window", fmt: (v, r) => h("span", { class: "mono dim" }, fmt.date(v) + " → " + fmt.date(r.start_before)) },
          { key: "max_price_per_gpu_hour", label: "Your limit", num: true, fmt: v => perGpuH(v) },
          { key: "index", label: "Index at request", num: true, value: r => r.indicative && r.indicative.index ? r.indicative.index.level : null, fmt: v => v != null ? perGpuH(v) : OG.na("index not published") },
          { key: "offer_price", label: "Offer", num: true, value: r => offerOf(r) ? r.offer.price_per_gpu_hour : null, fmt: (v, r) => v != null ? h("span", { class: offerOf(r).valid ? "" : "dim" }, perGpuH(v)) : h("span", { class: "dim" }, "–") },
          scope === "all" ? { key: "account_id", label: "Account", cls: "mono dim", fmt: v => v == null ? "operator" : String(v) } : null,
          { key: "created_at", label: "Created", num: true, fmt: v => fmt.dateTime(v) },
        ].filter(Boolean),
        rows: [], sort: { key: "created_at", dir: "desc" }, rowKey: r => r.request_id, rowHref: r => "/reserve/" + encodeURIComponent(r.request_id),
        csv: "opengrid-price-lock-requests.csv", compact: true, empty: "No price-lock requests yet.",
      });
      let tbl = makeTable();
      async function load() {
        let rows;
        try { const d = await ctx.api(scope === "all" ? "/v1/admin/reservations" : "/v1/reservations", { nocache: true }); rows = Array.isArray(d) ? d : []; }
        catch (e) { listSec.replaceChildren(OG.section("Your requests", notYet(scope === "all" ? "GET /v1/admin/reservations" : "GET /v1/reservations", e))); return; }
        stats.replaceChildren(OG.stats([
          { label: "Requests", value: fmt.num(rows.length) },
          { label: "Open", value: fmt.num(rows.filter(r => OPEN.includes(r.status)).length), sub: "awaiting a decision" },
          { label: "Offers to review", value: fmt.num(rows.filter(r => r.status === "offered" && offerOf(r) && offerOf(r).valid).length), sub: "valid offers" },
          { label: "Accepted", value: fmt.num(rows.filter(r => r.status === "accepted").length), sub: "order form pending" },
          { label: "Fulfilled", value: fmt.num(rows.filter(r => r.status === "fulfilled").length) },
          { label: "Closed", value: fmt.num(rows.filter(r => CLOSED.includes(r.status)).length), sub: "declined · expired · withdrawn · cancelled" },
        ]));
        if (!rows.length) { listSec.replaceChildren(OG.section(scope === "all" ? "All requests" : "Your requests", h("div", { class: "dep-empty" }, h("b", {}, "No price-lock requests yet."), h("p", {}, "Use the form above: say which GPU, how many GPU-hours, when it should start and your price limit. OpenGrid answers with an offer only when provider capacity backs it.")))); return; }
        tbl.update(rows);
        if (!listSec.contains(tbl)) listSec.replaceChildren(OG.section(scope === "all" ? "All requests" : "Your requests", tbl, h("p", { class: "note" }, "Index at request = the OpenGrid index level captured when the request was made (indicative). An offer binds only in a signed order form.")));
      }
      load();
      ctx.every(30000, load);
    },
  });

  /* =============================== /reserve/:id =============================== */
  OG.page("/reserve/:id", {
    title: p => "Price-lock request " + p.id,
    nav: "reserve",
    async mount(el, params, query, ctx) {
      const id = params.id, enc = encodeURIComponent(id);
      const root = h("div", { class: "pg-jobs pg-route pg-dep pg-depd pg-rsv" });
      el.append(root);
      root.append(OG.loading("Loading the request…"));
      const me = await OG.me();
      if (!ctx.alive()) return;
      const admin = OG.isAdmin(me);
      let r = null, viaAdmin = false;
      const flash = h("div", { class: "dep-flash" });
      const I = { accept: null, withdraw: null };

      async function load() {
        try { r = await ctx.api("/v1/reservations/" + enc, { nocache: true }); viaAdmin = false; }
        catch (e) {
          if (admin && e.status === 404) {
            try { r = await ctx.api("/v1/admin/reservations/" + enc, { nocache: true }); viaAdmin = true; }
            catch (e2) { root.replaceChildren(OG.head("Price-lock request " + id, null, h("a", { class: "btn", href: "/reserve" }, "← Reserve")), notYet("GET /v1/admin/reservations/{id}", e2)); return; }
          } else { root.replaceChildren(OG.head("Price-lock request " + id, null, h("a", { class: "btn", href: "/reserve" }, "← Reserve")), notYet("GET /v1/reservations/{id}", e)); return; }
        }
        draw();
      }

      function draw() {
        const acts = new Set(r.actions || []);
        const o = offerOf(r);
        const evs = r.events || [];
        root.replaceChildren(
          OG.head(h("span", { class: "dep-title" }, `${r.gpu_count}× ${OG.shortGpu(r.gpu)}`, " ", h("span", { class: "mono dim jb-id" }, r.request_id)),
            h("span", {}, hoursText(r.total_gpu_hours), " · ", r.region_group || "any region", " · start ", fmt.dateTime(r.start_after), " → ", fmt.dateTime(r.start_before)),
            badge(r.status),
            acts.has("withdraw") ? h("button", { class: "btn w4-danger", type: "button", onclick: () => withdraw() }, r.status === "offered" ? "Decline offer / withdraw…" : "Withdraw…") : null,
            h("button", { class: "btn", type: "button", onclick: () => load() }, "Refresh"),
            h("a", { class: "btn", href: "/reserve" }, "← Reserve"), OG.copy(r.request_id, { title: "Copy the request id" })),
          banner(),
          flash,
          h("ol", { class: "og-strip", "aria-label": "request lifecycle" }, strip(r.status, evs.map(e => e.to)).map(st => h("li", { class: "m-" + st.mark + (st.tone ? " t-" + st.tone : "") + (st.off ? " off" : ""), "aria-current": st.mark === "current" ? "step" : null }, st.state.replace(/_/g, " ")))),
          OG.stats([
            { label: "Your limit", value: perGpuH(r.max_price_per_gpu_hour), sub: "up to " + usd(r.max_total_usd) },
            { label: "GPU-hours", value: hoursText(r.total_gpu_hours), sub: r.gpu_count + " GPUs at once" },
            { label: "Offer", value: o ? perGpuH(o.price_per_gpu_hour) : null, reason: OPEN.includes(r.status) ? "no offer yet" : "no offer", sub: o ? `#${o.offer_seq} · ${usd(o.total_usd)} total` : null },
            { label: "Offer valid until", value: o ? fmt.dateTime(o.valid_until) : null, reason: "no offer", sub: o ? (o.valid ? "valid · " + fmt.age((+new Date(o.valid_until) - Date.now()) / 1000) + " left" : "not open") : null },
            { label: "Created", value: fmt.dateTime(r.created_at), sub: r.account_id == null ? "operator" : "account " + r.account_id },
            { label: "Status since", value: r.state_changed_at ? fmt.age(r.state_changed_at) : null, sub: r.state_changed_at ? fmt.dateTime(r.state_changed_at) : null, reason: "not reported" },
          ]),
          offerBlock(o, acts),
          adminBlock(acts),
          OG.section("Request", kv([
            ["GPU", h("span", {}, `${r.gpu_count}× `, OG.gpuLink(r.gpu))],
            ["Total GPU-hours", hoursText(r.total_gpu_hours)],
            ["Start window", fmt.dateTime(r.start_after) + " → " + fmt.dateTime(r.start_before)],
            ["Region group", r.region_group || "any"],
            ["Your max price", perGpuH(r.max_price_per_gpu_hour), "the customer's limit, not a price"],
            ["Notes", r.notes ? h("span", { class: "rs-notes" }, r.notes) : null],
            r.closed_reason ? ["Closed because", r.closed_reason] : null,
          ])),
          refBlock(r.indicative, "Indicative reference at request time"),
          eventsBlock(evs));
      }

      function offerBlock(o, acts) {
        if (!o) return OG.section("Offer", h("div", { class: "jb-empty" }, h("p", {}, OPEN.includes(r.status) ? "No offer yet. OpenGrid makes an offer only when a provider capacity commitment backs it; until then there is no price." : "No offer was made.")));
        const valid = o.valid;
        return OG.section(h("span", {}, "Offer #" + o.offer_seq, " ", valid ? OG.badge("open", "good") : OG.badge(r.status === "accepted" || r.status === "fulfilled" ? "accepted" : "not open", r.status === "accepted" ? "good" : ""), " ", OG.badge("binds only in a signed order form", "", "Not a financial contract; not tradable; no cash settlement")),
          o.above_customer_limit ? h("div", { class: "og-alarm t-warn" }, h("b", {}, "Above your limit"), h("span", {}, `The offer ${perGpuH(o.price_per_gpu_hour)} is above your limit ${perGpuH(r.max_price_per_gpu_hour)}.`)) : null,
          r.status === "accepted" ? h("div", { class: "og-ok" }, h("b", {}, "Accepted"), h("span", {}, ACCEPTED)) : null,
          kv([
            ["Price", h("b", { class: "mono" }, perGpuH(o.price_per_gpu_hour))],
            ["Total", h("span", { class: "mono" }, usd(o.total_usd) + " for " + hoursText(o.total_gpu_hours)), "offer total: not billed; binds only in a signed order form"],
            ["Valid until", fmt.dateTime(o.valid_until)],
            ["Terms", h("span", { class: "rs-notes" }, o.terms || "")],
            ["Supply basis", h("span", { class: "rs-notes" }, o.supply_basis || ""), "the provider capacity commitment that backs this offer"],
            ["Offered", fmt.dateTime(o.offered_at)],
          ]),
          acts.has("accept") ? h("div", { class: "dep-acts" }, h("button", { class: "btn pri", type: "button", onclick: () => accept(o) }, "Accept offer #" + o.offer_seq + "…"),
            h("span", { class: "rt-h" }, "Records your intent only: no launch, no charge, no payment. OpenGrid then sends the order form.")) : null);
      }

      function adminBlock(acts) {
        if (!admin) return null;
        const b = [];
        if (acts.has("review")) b.push(h("button", { class: "btn", type: "button", onclick: () => adminAct("review", { title: "Mark under review", fields: [{ key: "reason", label: "Note (optional)", type: "text" }] }) }, "Mark under review"));
        if (acts.has("offer")) b.push(h("button", { class: "btn pri", type: "button", onclick: () => makeOffer() }, offerOf(r) ? "Revise offer…" : "Make offer…"));
        if (acts.has("decline")) b.push(h("button", { class: "btn w4-danger", type: "button", onclick: () => adminAct("decline", { title: "Decline the request", danger: true, fields: [{ key: "reason", label: "Reason (shown to the customer)", type: "textarea", required: true, minLength: 3 }] }) }, "Decline…"));
        if (acts.has("fulfil")) b.push(h("button", { class: "btn", type: "button", onclick: () => adminAct("fulfil", { title: "Mark fulfilled", fields: [{ key: "reason", label: "Order form / delivery reference", type: "textarea", required: true, minLength: 3 }] }) }, "Mark fulfilled…"));
        if (acts.has("cancel")) b.push(h("button", { class: "btn w4-danger", type: "button", onclick: () => adminAct("cancel", { title: "Cancel the accepted request", danger: true, fields: [{ key: "reason", label: "Reason", type: "textarea", required: true, minLength: 3 }] }) }, "Cancel…"));
        if (!b.length) return null;
        return OG.section(h("span", {}, "Operator ", OG.badge("admin", "warn")), h("div", { class: "dep-acts" }, b),
          h("p", { class: "note" }, "An offer needs a supply basis: the provider capacity commitment that backs it. Every action is recorded with your reason."));
      }

      async function post(path, body, intent) {
        flash.replaceChildren(OG.loading("Sending…"));
        try {
          const out = intent ? await OG.api.intent(intent, path, { method: "POST", body }) : await OG.api(path, { method: "POST", body });
          r = out; draw();
          return out;
        } catch (e) {
          flash.replaceChildren(apiErr(e, "POST " + path.replace(id, "{id}")), intent && intent.state === "unknown" ? h("p", { class: "warn-t" }, "Outcome unknown: try again — it reuses the same Idempotency-Key.") : null);
          return null;
        }
      }
      async function accept(o) {
        const ok = await OG.dialog({
          title: "Accept offer #" + o.offer_seq, confirm: "Accept offer",
          ack: "I understand this records my intent only: nothing is launched, charged or paid, and the order form governs.",
          body: h("div", { class: "jb-dlg" },
            h("table", { class: "rt-dlg-t" },
              h("tr", {}, h("th", {}, "GPU"), h("td", { class: "mono" }, `${r.gpu_count}× ${OG.shortGpu(r.gpu)}`)),
              h("tr", {}, h("th", {}, "Price"), h("td", { class: "mono" }, perGpuH(o.price_per_gpu_hour))),
              h("tr", {}, h("th", {}, "Total"), h("td", { class: "mono" }, usd(o.total_usd) + " for " + hoursText(o.total_gpu_hours))),
              h("tr", {}, h("th", {}, "Valid until"), h("td", { class: "mono" }, fmt.dateTime(o.valid_until)))),
            h("p", {}, ACCEPTED)),
        });
        if (!ok) return;
        const body = { offer_seq: o.offer_seq };
        I.accept = OG.intentFor(I.accept, "reservation:accept:" + id, body);
        const out = await post("/v1/reservations/" + enc + "/accept", body, I.accept);
        if (out) flash.replaceChildren(h("div", { class: "og-ok" }, h("b", {}, "Accepted"), h("span", {}, out.message || ACCEPTED)));
      }
      async function withdraw() {
        const v = await OG.ask({ title: r.status === "offered" ? "Decline the offer and withdraw" : "Withdraw the request", danger: true, confirm: "Withdraw",
          fields: [{ key: "reason", label: "Reason (optional)", type: "textarea" }] });
        if (!v) return;
        const body = v.reason ? { reason: v.reason } : {};
        I.withdraw = OG.intentFor(I.withdraw, "reservation:withdraw:" + id, body);
        await post("/v1/reservations/" + enc + "/withdraw", body, I.withdraw);
      }
      async function adminAct(action, dlg) {
        const v = await OG.ask(Object.assign({ confirm: dlg.title }, dlg));
        if (!v) return;
        await post("/v1/admin/reservations/" + enc + "/" + action, v.reason ? { reason: v.reason } : {});
      }
      async function makeOffer() {
        const prev = offerOf(r);
        const until = new Date(Date.now() + 3 * 864e5);
        const v = await OG.ask({ title: prev ? "Revise offer #" + prev.offer_seq : "Make an offer", confirm: prev ? "Revise offer" : "Make offer",
          body: h("p", { class: "dim" }, `Customer limit ${perGpuH(r.max_price_per_gpu_hour)} for ${hoursText(r.total_gpu_hours)}. An offer binds only in a signed order form.`),
          fields: [
            { key: "price_per_gpu_hour", label: "Price $/GPU·h", type: "text", required: true, value: prev ? String(prev.price_per_gpu_hour) : String(r.max_price_per_gpu_hour), hint: "> 0 and ≤ 1000" },
            { key: "valid_until", label: "Valid until (ISO, UTC)", type: "text", required: true, value: prev ? prev.valid_until : until.toISOString().slice(0, 16) + "Z", hint: "15 minutes to 30 days ahead" },
            { key: "terms", label: "Terms", type: "textarea", required: true, minLength: 10, value: prev ? prev.terms : "", placeholder: "Delivery window, minimum term, cancellation, payment terms (in the order form)…" },
            { key: "supply_basis", label: "Supply basis (required)", type: "textarea", required: true, minLength: 10, value: prev ? prev.supply_basis : "", placeholder: "Provider X written capacity commitment ref …" },
            { key: "reason", label: "Note (optional)", type: "text" },
          ] });
        if (!v) return;
        const body = { price_per_gpu_hour: +v.price_per_gpu_hour, valid_until: v.valid_until, terms: v.terms, supply_basis: v.supply_basis };
        if (v.reason) body.reason = v.reason;
        await post("/v1/admin/reservations/" + enc + "/offer", body);
      }

      function eventsBlock(ev) {
        if (!ev.length) return OG.section("Events", OG.empty("No events recorded."));
        const evText = o => !o || typeof o !== "object" ? "" : Object.entries(o).filter(([, v]) => v != null && v !== "").map(([k, v]) => `${k}: ${typeof v === "object" ? JSON.stringify(v).slice(0, 160) : v}`).join(" · ");
        return OG.section("Events · " + ev.length, h("ol", { class: "dep-tl dep-tl2" }, ev.slice().reverse().map(e => h("li", { class: "t-" + (tone(e.to) || "plain") + (e.from === e.to ? " note-ev" : "") },
          h("span", { class: "mono dim dep-at" }, fmt.dateTime(e.at)),
          h("span", { class: "dep-tr" }, h("span", { class: "dim" }, String(e.from || "∅").replace(/_/g, " ") + " → "), badge(e.to)),
          e.actor ? h("span", { class: "badge dep-actor a-" + e.actor }, e.actor + (e.actor_id ? " · " + e.actor_id : "")) : null,
          h("span", { class: "dep-reason" }, e.reason || ""),
          e.evidence ? h("span", { class: "dep-det mono" }, evText(e.evidence)) : null))));
      }

      await load();
      ctx.every(30000, () => { if (r && !isTerminal(r)) load(); });
    },
  });
})();
