/* /jobs/:id: one training job, end to end (GET /v1/jobs/{id}, the jobs/models.py public() shape; polled every
   4 s while active, stopped once `finished`). Header (id, name, state, outcome, SIMULATED/real, deployment,
   provider, GPU, region, started, runtime, est + actual cost), the JOB lifecycle strip AND the DEPLOYMENT's own
   strip (two machines, never merged), the pending-approval block (admins approve on the existing route ticket
   /route?rr=…; no second approval path), live progress from PERSISTED metrics only, loss + accuracy charts,
   the cursor-paged log panel, the 12 verification checks with evidence, artifacts (download + sha256 + size),
   termination evidence, cost reconciliation (quote / expected / provider-reported / transaction), the routing
   decision, Cancel (one Idempotency-Key per intent, confirmed) and errors with their actionable message.
   A SIMULATED job carries a persistent banner and never shows a real check mark. */
(() => {
  if (typeof document === "undefined" || typeof OG.page !== "function") return;
  const { h, fmt } = OG;
  const J = OG.jobs;
  const POLL_MS = 4000;
  const kv = rows => h("table", { class: "dep-kv" }, rows.filter(Boolean).map(([k, v, title]) => h("tr", { title: title || null }, h("th", {}, k), h("td", {}, v == null || v === "" ? h("span", { class: "dim" }, "-") : v))));
  const evText = o => !o || typeof o !== "object" ? String(o ?? "") : Object.entries(o).filter(([, v]) => v != null && v !== "").map(([k, v]) => `${k}: ${typeof v === "object" ? JSON.stringify(v).slice(0, 160) : v}`).join(" · ");
  const exact = iso => (OG.execText && OG.execText.exactTime(iso)) || fmt.dateTime(iso);
  const DISPO = { none: ["nothing created", "dim"], not_launched_yet: ["nothing launched yet", "dim"], possibly_billing: ["POSSIBLY BILLING", "down"],
    terminated_confirmed: ["terminated (provider confirmed)", "up"], simulated_none: ["none (simulated)", "dim"] };

  OG.page("/jobs/:id", {
    title: p => "Job " + p.id,
    nav: "jobs",
    async mount(el, params, query, ctx) {
      if (OG.computeStyles) OG.computeStyles();
      const id = params.id;
      const enc = encodeURIComponent(id);
      const root = h("div", { class: "pg-jobs pg-job pg-route pg-dep pg-depd" });   // reuse ticket + deployment-detail styles
      el.append(root);
      root.append(OG.loading("Loading job…"));
      const me = await OG.me();
      if (!ctx.alive()) return;
      const admin = OG.isAdmin(me);
      const I = { cancel: null };
      let job = null, dep = null, metrics = [], arts = null, artErr = null, metErr = null, timer = null, stopped = false;

      // fixed regions: the log panel keeps its lines (and its scroll position) across polls
      const flash = h("div", { class: "dep-flash" });
      const top = h("div"), mid = h("div"), logBox = logPanel(), bottom = h("div"), advTop = h("div");
      // Everything technical (state machines, logs, checks, reconciliation, fingerprints) lives under one fold.
      const adv = h("details", { class: "jb-adv", open: query.adv === "1" ? true : null },
        h("summary", {}, h("b", {}, "Advanced details"), h("span", { class: "dim" }, " (job and deployment state machines, logs, verification checks, cost reconciliation, events, request, routing decision)")),
        advTop, logBox.el, bottom);
      adv.addEventListener("toggle", () => { if (ctx.alive()) OG.qs.set({ adv: adv.open ? "1" : null }); });

      async function load() {
        let j;
        try { j = await ctx.api("/v1/jobs/" + enc, { nocache: true }); }
        catch (e) {
          if (!job) {
            const p = J.plainError(e);
            root.replaceChildren(h("a", { class: "lnk jb-back", href: "/jobs" }, "← My Jobs"), OG.head("Job " + id, null),
              e && (e.status === 401 || e.status === 403 || e.status === 404) ? h("section", { class: "cx-access" }, h("h2", {}, e.status === 404 ? "Job not found" : p.title),
                h("p", {}, e.status === 404 ? "This job doesn't exist, or it belongs to another account." : e.status === 401 ? "Jobs are private to your OpenGrid account. Sign in to see this one." : p.text),
                h("div", { class: "cx-acts" }, e.status === 401 ? h("a", { class: "btn pri", href: OG.loginHref() }, "Sign in") : null, h("a", { class: "btn", href: "/jobs" }, "My Jobs"), h("a", { class: "btn", href: J.DEMO_HREF }, "See the example job")))
                : J.notYet("GET /v1/jobs/{id}", e));
            stopped = true; return;
          }
          flash.replaceChildren(h("div", { class: "og-alarm t-warn" }, h("b", {}, "Refresh failed"), h("span", {}, e.message || "network error"), h("span", { class: "dim" }, "showing the last answer; retrying")));
          return;
        }
        job = j && j.job ? j.job : j;
        const depId = J.summary(job).deployment_id;
        const [d, m, a] = await Promise.all([
          depId ? OG.api.soft("/v1/deployments/" + encodeURIComponent(depId), { nocache: true }) : null,
          OG.api("/v1/jobs/" + enc + "/metrics", { nocache: true }).catch(e => { metErr = e; return null; }),
          OG.api("/v1/jobs/" + enc + "/artifacts", { nocache: true }).catch(e => { artErr = e; return null; }),
        ]);
        if (!ctx.alive()) return;
        dep = d;
        if (m) { metErr = null; metrics = J.metricPoints(m); }
        else if (job.metrics_latest && !metrics.length) metrics = J.metricPoints(Array.isArray(job.metrics_latest) ? job.metrics_latest : job.metrics_latest.metrics || []);
        if (a) { artErr = null; arts = Array.isArray(a) ? a : a.artifacts || a.items || []; }
        else if (Array.isArray(job.artifacts)) { arts = job.artifacts; artErr = null; }
        if (flash.firstChild && flash.firstChild.classList && flash.firstChild.classList.contains("t-warn") && /Refresh failed/.test(flash.textContent)) flash.replaceChildren();
        draw();
        await logBox.pull();
      }
      function schedule() {
        clearTimeout(timer);
        if (!ctx.alive() || stopped) return;
        timer = setTimeout(async () => {
          if (!document.hidden) await load();
          if (job && J.isTerminal(job)) { stopped = true; draw(); return; }
          schedule();
        }, POLL_MS);
      }
      ctx.onCleanup(() => clearTimeout(timer));

      /* ---------------- blocks ---------------- */
      const st = () => J.stateOf(job);
      const sandbox = () => J.isSandbox(job);
      const S = () => J.summary(job, dep);

      function header() {
        const sm = S();
        // The control matches the real operation: none for a simulation (the server never cancels one; it finishes
        // on its own), "Cancel job" before launch, "Terminate GPU" once a GPU may exist.
        const ca = J.cancelAction(job);
        const where = sandbox() ? "Free simulation: no GPU, no provider, no charge"
          : [sm.gpu ? `${sm.gpu_count}× ${OG.shortGpu(sm.gpu)}` : null, sm.provider ? "on " + OG.providerName(sm.provider) : null, sm.region || sm.region_group || null].filter(Boolean).join(" ") || "GPU not chosen yet";
        return [
          h("a", { class: "lnk jb-back", href: "/jobs" }, "← My Jobs"),
          OG.head(h("span", { class: "dep-title" }, job.name || "Job"), h("span", {}, where),
            ca ? h("button", { class: "btn w4-danger", type: "button", onclick: () => cancel(ca) }, ca.button) : null),
          h("div", { class: "jb-bigline" }, J.lineEl(job), J.modeTag(sandbox())),
          resumeNote(),
          sandbox() ? h("div", { class: "jb-simban", role: "note" }, h("b", {}, "SIMULATED"), h("span", {}, "No GPU was used and nothing was charged. Every number on this page is synthetic.")) : null,
          flash,
          billingAlarm(),
          errorBlock(),
          glanceBlock(),
        ];
      }
      // "Resumed from epoch N of job X" (job.resume: pinned at submission, confirmed by the runner's `resumed` phase).
      function resumeNote() {
        const rl = J.resumeLine(job);
        if (!rl) return null;
        const i = rl.text.indexOf(rl.job_id);   // the source job id becomes a link
        return h("p", { class: "note" + (rl.tone ? " t-" + rl.tone : ""), role: "note" }, rl.text.slice(0, i),
          h("a", { class: "lnk mono", href: "/jobs/" + encodeURIComponent(rl.job_id) }, rl.job_id), rl.text.slice(i + rl.job_id.length));
      }
      // At a glance: simulation or real, status, result, GPU, cost, outputs; then the one next step.
      function glanceBlock() {
        const ps = Object.fromEntries(J.plainStatus(job, dep, arts, { admin }).map(p => [p.key, p]));
        const line = J.jobLine(job);
        const cell = (label, a, sub, tone) => h("div", { class: tone ? "t-" + tone : null }, h("div", { class: "cx-fact-k" }, label), h("div", { class: "cx-g-v" }, a), sub ? h("div", { class: "cx-g-s" }, sub) : null);
        const ns = J.nextStep(job, dep, { admin });
        const arr = Array.isArray(arts) ? arts : [];
        return [
          h("div", { class: "cx-glance", "aria-label": "At a glance" },
            cell("Run", sandbox() ? "Simulation" : "Real GPU", sandbox() ? "no GPU, no provider, no charge" : "on a rented GPU", sandbox() ? "warn" : ""),
            cell("Status", line.text, J.isTerminal(job) ? "finished" : "updates every few seconds", line.tone === "bad" ? "bad" : line.tone === "warn" ? "warn" : ""),
            cell("Result", ps.success.a, ps.success.detail, ps.success.tone),
            cell("GPU", ps.gpu.a, ps.gpu.detail, ps.gpu.tone),
            cell("Cost", ps.cost.a, ps.cost.detail, ps.cost.tone),
            cell("Outputs", arr.length ? `${arr.length} file${arr.length === 1 ? "" : "s"}` : "None yet", arr.length ? arr.map(a => a && J.artifactLabel(a)).filter(Boolean).join(", ") : ps.outputs.detail, arr.length ? "good" : "")),
          h("div", { class: "cx-next", role: "status" }, h("b", {}, "Next: "), h("span", {}, ns.text), ns.href ? h("a", { class: "btn sm pri", href: ns.href }, ns.label) : null,
            sandbox() && !J.isTerminal(job) ? h("span", { class: "dim" }, "Simulations finish on their own; there's nothing to stop.") : null),
        ];
      }
      // What happened, oldest first, from the job's recorded events (the raw state and reason are in the tooltip).
      function timelineBlock() {
        const tl = J.timeline(job);
        if (!tl.length) return null;
        return OG.section("What happened", h("ol", { class: "cx-tl" }, tl.map(x => h("li", { class: "t-" + (x.tone || "plain"), title: [x.state, x.reason].filter(Boolean).join(": ") },
          h("span", { class: "cx-tl-t" }, x.at ? fmt.time(x.at) : ""), h("i", { class: "cx-tl-d", "aria-hidden": "true" }), h("span", { class: "cx-tl-x" }, x.text)))));
      }
      function metaLine() {
        const sm = S();
        return h("div", { class: "jb-meta dim" }, h("span", { class: "mono" }, job.job_id || id), " ", OG.copy(job.job_id || id, { title: "Copy the job id" }),
          " · ", job.template || "", job.template_version ? " v" + job.template_version : "", " · state ", J.stateBadge(st()),
          sm.provider ? [" · ", OG.providerLink(sm.provider)] : null, sm.gpu ? [" · ", OG.gpuLink(sm.gpu)] : null,
          sm.deployment_id ? [" · ", h("a", { class: "lnk mono", href: "/deployments/" + encodeURIComponent(sm.deployment_id) }, sm.deployment_id)] : null,
          sm.route_request_id ? [" · ", h("a", { class: "lnk mono", href: "/route?rr=" + encodeURIComponent(sm.route_request_id) }, sm.route_request_id)] : null,
          " · ", h("button", { class: "btn sm", type: "button", onclick: () => load() }, "Refresh now"));
      }
      function statsStrip() {
        const s = st(), sm = S();
        const d = DISPO[sm.disposition] || [sm.disposition ? String(sm.disposition).replace(/_/g, " ") : null, ""];
        return OG.stats([
            { label: "State since", value: job.state_changed_at ? fmt.age(job.state_changed_at) : null, sub: job.state_changed_at ? fmt.dateTime(job.state_changed_at) : null, reason: "not reported" },
            { label: "Created", value: fmt.dateTime(job.created_at) },
            { label: "Started", value: job.started_at ? fmt.dateTime(job.started_at) : null, reason: J.PRE_LAUNCH.includes(s) ? "not started" : "not reported", title: job.started_at ? exact(job.started_at) : null },
            { label: "Runtime", value: sm.runtime_seconds != null ? J.duration(sm.runtime_seconds) : null, reason: "not started", sub: sm.runtime_ceiling ? "ceiling " + sm.runtime_ceiling + " min" : null },
            { label: "Est. max", value: sm.est_max != null ? J.usd(sm.est_max) : null, reason: "no estimate", kind: "estimated", sub: sm.est_typical != null ? "typical " + J.usd(sm.est_typical) : null },
            { label: "Actual cost", value: sm.actual != null ? J.usd(sm.actual) : null, reason: sandbox() ? "simulated: no spend" : "reconciled after confirmed termination", kind: sandbox() ? null : "transaction" },
            { label: "Resource", value: d[0] ? h("span", { class: d[1] }, d[0]) : null, reason: "unknown", sub: sm.deployment_status ? "deployment " + String(sm.deployment_status).replace(/_/g, " ") : null, title: sm.disposition_note },
          ]);
      }

      // Plain progress: 8 stages (4 for the sandbox) derived from the real job state, and the questions a
      // beginner asks, answered from the job / deployment / artifacts (OG.jobs.stages / plainStatus).
      function stageBlock() {
        const sg = J.stages(job);
        // the glance above answers status, result, GPU, cost and outputs; here only money and action, for real runs
        const ps = sandbox() ? [] : J.plainStatus(job, dep, arts, { admin }).filter(p => ["money", "action"].includes(p.key));
        return h("section", { class: "jb-stage box", "aria-label": "Progress" },
          h("div", { class: "jb-stage-h" }, h("span", { class: "eyebrow" }, "Progress"),
            sg.mode === "sandbox" ? OG.badge("free simulation", "warn") : OG.badge("real GPU run"),
            h("span", { class: "dim" }, sg.mode === "sandbox" ? "Simulated: no GPU is selected, approved, started or shut down." : "Each stage follows the job's recorded state.")),
          h("ol", { class: "jb-stages", "aria-label": "Stages" }, sg.steps.map((x, i) => h("li", { class: "m-" + x.mark + (x.tone ? " t-" + x.tone : ""), "aria-current": x.mark === "current" ? "step" : null,
            title: x.state ? "job state: " + String(x.state).replace(/_/g, " ") : x.mark === "skipped" ? "not passed through" : null },
            h("span", { class: "jb-sn", "aria-hidden": "true" }, x.mark === "done" ? "✓" : x.mark === "failed" ? "✗" : String(i + 1)),
            h("span", { class: "jb-sl" }, x.label),
            x.mark === "current" ? h("span", { class: "jb-sx" }, "now") : x.mark === "skipped" ? h("span", { class: "jb-sx" }, "skipped") : x.mark === "failed" ? h("span", { class: "jb-sx" }, "failed") : null))),
          ps.length ? h("dl", { class: "jb-plain" }, ps.map(p => h("div", { class: "jb-pq t-" + (p.tone || "plain") }, h("dt", {}, p.q), h("dd", {}, h("b", {}, p.a), p.detail ? h("span", { class: "dim" }, p.detail) : null)))) : null);
      }
      function billingAlarm() {
        if (sandbox()) return null;
        const s = st(), r = job.resource || {}, sm = S();
        if (J.UNCERTAIN.includes(s) || s === "termination_failed" || r.termination_unconfirmed || (dep && (OG.dep.UNCERTAIN.includes(dep.status) || dep.status === "termination_failed")))
          return h("div", { class: "og-alarm t-bad x-billing" }, h("b", {}, "POSSIBLY BILLING"),
            h("span", {}, r.termination_note || `Job ${String(s).replace(/_/g, " ")}${sm.deployment_status ? ", deployment " + String(sm.deployment_status).replace(/_/g, " ") : ""}: OpenGrid cannot confirm the instance is gone. Treat it as billing until the provider confirms.`),
            sm.deployment_id ? h("a", { class: "btn sm w4-danger", href: "/deployments/" + encodeURIComponent(sm.deployment_id) }, "Open deployment →") : null);
        if ((J.FAILED.includes(s) || J.outcomeOf(job) === "failed") && sm.possibly_billing)
          return h("div", { class: "og-alarm t-warn" }, h("b", {}, "Job failed: instance not yet terminated"), h("span", {}, "A failed job does not mean the GPU stopped billing. OpenGrid terminates it via the provider API; watch the deployment state."));
        return null;
      }
      // What went wrong in plain words; the failure code, the server's reason and (for admins) the fix link folded.
      function errorBlock() {
        const f = job.failure;
        if (!f) return null;
        const a = J.blockerAction({ code: f.code, message: f.reason });
        const failState = (job.events || []).map(e => e && (e.to || e.to_status)).filter(s => s && J.FAILED.includes(s)).pop() || (J.FAILED.includes(st()) ? st() : null);
        const title = failState ? J.plainState(failState, sandbox()) : "Something went wrong";
        return h("div", { class: "og-alarm" + (J.FAILED.includes(st()) || J.outcomeOf(job) === "failed" ? "" : " t-warn") }, h("b", {}, title),
          h("span", {}, sandbox() ? "Nothing was charged: this was a simulation." : J.summary(job, dep).possibly_billing ? "OpenGrid is shutting the GPU down; see the GPU status above." : "The GPU and cost status are shown above."),
          h("details", { class: "cx-tech" }, h("summary", {}, "Technical details"), h("span", { class: "mono" }, String(f.code || "error")), " · ", f.reason || "",
            admin && a.label ? h("div", {}, "For admins: ", a.href ? h("a", { class: "lnk", href: a.href }, a.label + " →") : a.label) : null));
      }

      function strips() {
        const sm = S();
        return h("div", { class: "jb-strips" },
          h("div", { class: "jb-stl" }, h("span", { class: "eyebrow" }, "Job lifecycle"), h("span", { class: "dim" }, sandbox() ? "SIMULATED run: no quote, approval, provisioning or termination" : "the workload and its outcome"),
            job.runner_phase ? h("span", { class: "dim mono" }, "runner phase " + job.runner_phase) : null, job.last_runner_event_at ? h("span", { class: "dim" }, "last runner event " + fmt.age(job.last_runner_event_at) + " ago") : null),
          J.stripEl(st(), job.events, sandbox()),
          h("div", { class: "jb-stl" }, h("span", { class: "eyebrow" }, "Deployment"),
            sm.deployment_id ? h("a", { class: "lnk mono", href: "/deployments/" + encodeURIComponent(sm.deployment_id) }, sm.deployment_id, " →") : null,
            h("span", { class: "dim" }, sandbox() ? "sandbox: no cloud resource" : sm.deployment_id ? "the cloud resource (its own state machine)" : "no deployment yet (created with the quote at submission)")),
          dep && dep.status ? OG.stateStrip(dep.status, dep.events)
            : sm.deployment_status ? OG.stateStrip(sm.deployment_status, []) : h("div", { class: "jb-nodep dim" }, sandbox() ? "SIMULATED: no deployment, no instance." : "-"));
      }

      function approvalBlock() {
        const s = st(), a = job.approval;
        if (!a && !["pending_approval", "quote_expired"].includes(s)) return null;
        const rr = S().route_request_id;
        const q = a || {};
        const expired = s === "quote_expired" || q.quote_status === "expired";
        const vs = q.limit_violations || [];
        return h("div", { class: "box jb-appr" },
          h("div", { class: "rt-res-h" }, h("span", { class: "eyebrow" }, "Approval"), OG.badge(expired ? "QUOTE EXPIRED" : "PENDING APPROVAL", expired ? "bad" : "warn"),
            q.quote_expires_at && !expired ? h("span", { class: "dim" }, "quote expires in ", OG.countdown(q.quote_expires_at)) : null, h("span", { class: "spacer" }),
            rr ? h("a", { class: "lnk mono", href: "/route?rr=" + encodeURIComponent(rr) }, rr) : null),
          h("div", { class: "jb-appr-b" },
            admin ? [h("p", {}, "Approve on the existing route ticket: it re-validates the quote live, shows SSH access and the auto-terminate deadline, then makes the single provision call."),
              rr ? h("a", { class: "btn pri", href: "/route?rr=" + encodeURIComponent(rr) }, expired ? "Open the ticket (re-quote) →" : "Open approval ticket →") : h("span", { class: "down" }, "No route_request_id on this job: cannot open the ticket.")]
              : h("p", { class: "tk-wait" }, h("i", { class: "spin" }), expired ? "The quote expired before approval; OpenGrid must re-quote. Nothing was launched or charged." : "Waiting for OpenGrid approval. Nothing is launched or charged until an admin approves this exact quote."),
            q.quote_price_per_gpu_hour != null ? h("span", { class: "mono" }, OG.conceptBadge("quote"), " ", J.perGpuH(q.quote_price_per_gpu_hour), q.est_total_cost != null ? " · est. total " + J.usd(q.est_total_cost) : "") : null,
            vs.length ? h("span", { class: "warn-t" }, `${vs.length} limit violation${vs.length === 1 ? "" : "s"}: an admin override with a reason is needed (${vs.map(v => v.code).join(", ")})`) : null,
            q.message ? h("span", { class: "dim jb-appr-m" }, q.message) : null));
      }

      function progressBlock() {
        const p = J.progress(metrics, job);
        const s = st();
        const lossEl = h("div", { class: "jb-chart" }), accEl = h("div", { class: "jb-chart" });
        const xs = metrics.map((m, i) => m.epoch ?? m.step ?? i + 1);
        line(lossEl, xs, metrics.map(m => m.loss), { label: "train loss", color: "#4d94ff", yFmt: v => v.toFixed(3) });
        line(accEl, xs, metrics.map(m => m.acc), { label: "test accuracy", color: "#2fbf71", yFmt: v => (v * 100).toFixed(1) + "%" });
        const res = job.result && typeof job.result === "object" ? job.result : null;
        return OG.section("Training progress" + (sandbox() ? " · SIMULATED (synthetic numbers)" : ""),
          OG.stats([
            { label: "Epoch", value: p.epoch != null ? `${p.epoch} / ${p.epochs ?? "?"}` : null, reason: metErr ? "metrics unavailable" : "no epoch reported yet" },
            { label: "Latest loss", value: p.loss != null ? p.loss.toFixed(4) : null, reason: "no loss reported yet" },
            { label: "Test accuracy", value: p.acc != null ? (p.acc * 100).toFixed(2) + "%" : null, reason: "no accuracy reported yet" },
            p.elapsed != null ? { label: "Elapsed", value: J.duration(p.elapsed), sub: "runner-reported" } : null,
            { label: "Progress", value: p.pct != null ? h("span", { class: "jb-pbar", title: Math.round(p.pct * 100) + "%" }, h("i", { style: { width: (p.pct * 100).toFixed(1) + "%" } })) : null, reason: "-" },
            res && res.outcome ? { label: "Runner result", value: String(res.outcome || "reported"), sub: res.summary && typeof res.summary === "object" ? evText(res.summary).slice(0, 60) : res.summary ? String(res.summary).slice(0, 60) : null, title: evText(res) } : null,
          ]),
          !metrics.length ? (metErr ? J.notYet("GET /v1/jobs/{id}/metrics", metErr) : h("p", { class: "note" }, s === "training" ? "Training started; the first epoch's metrics arrive when it ends." : "Metrics appear once training reports its first epoch. Only persisted runner metrics are shown; nothing is extrapolated."))
            : h("div", { class: "cols-2" }, h("div", {}, h("div", { class: "sec-h" }, "Train loss"), lossEl), h("div", {}, h("div", { class: "sec-h" }, "Test accuracy"), accEl)));
      }

      function checksBlock() {
        const cs = J.checks(job);
        const passed = cs.filter(c => c.status === "pass").length;
        const icon = c => ({ pass: "✓", fail: "✗", simulated: "~", pending: "…" })[c.status] || "…";
        return OG.section(`Verification · ${sandbox() ? "SIMULATED (no real check)" : `${passed} / ${cs.length} verified`}`,
          h("ul", { class: "ck-list jb-checks" }, cs.map(c => h("li", { class: "jb-ck-" + c.status, title: [c.status === "pending" ? "pending: no evidence yet" : c.status, c.source ? "source: " + c.source : c.expected_source ? "expected source: " + c.expected_source : null, c.evidence ? evText(c.evidence) : null, c.at ? exact(c.at) : null].filter(Boolean).join("\n") },
            h("span", { class: "jb-ck-i mono" }, icon(c)), h("b", {}, c.label),
            h("span", { class: "dim jb-ck-e" }, c.status === "simulated" ? "simulated, not a real check" : c.evidence ? evText(c.evidence) : c.status === "pending" ? "pending" + (c.expected_source ? " · from " + c.expected_source : "") : c.status),
            c.at ? h("span", { class: "mono dimmer" }, fmt.time(c.at)) : null))),
          h("p", { class: "note" }, "✓ only with recorded evidence from the named source (provider API, the runner on the GPU, or OpenGrid's own records). Hover a row for its evidence."));
      }

      function artifactsBlock() {
        if (artErr) return OG.section("Artifacts", J.notYet("GET /v1/jobs/{id}/artifacts", artErr));
        const so = J.splitOutputs(arts);
        const rows = so.results.concat(so.checkpoints);
        const href = n => `/v1/jobs/${enc}/artifacts/${encodeURIComponent(n)}`;
        const cp = job.checkpoints || {};
        // checkpoints: resumable state, not results; a sandbox only reports them (it stores none)
        const ckNote = so.checkpoints.length ? h("p", { class: "note" }, `Checkpoints are saved training progress a new job can resume from (not results); only the newest ${cp.keep || so.checkpoints.length} are kept.`)
          : sandbox() && (cp.reported_epochs || []).length ? h("p", { class: "note" }, `SIMULATED: checkpoints after epoch ${cp.reported_epochs.join(", ")} were reported but not stored; nothing here can be resumed.`) : null;
        return OG.section(`Artifacts · ${rows.length}` + (sandbox() ? " · SIMULATED" : ""), rows.length ? [OG.table({
          columns: [
            { key: "name", label: "Name", fmt: (v, r) => h("span", { title: (r.created_at ? "stored " + exact(r.created_at) : "") + (J.isCheckpoint(r) ? " · " + v : "") || null }, h("a", { class: "lnk" + (J.isCheckpoint(r) ? "" : " mono"), href: r.download || href(v), download: v, rel: "external" }, J.artifactLabel(r)), r.source === "simulated" ? [" ", OG.badge("simulated", "warn")] : null) },
            { key: "size_bytes", label: "Size", num: true, value: r => r.size_bytes ?? r.size ?? null, fmt: v => J.bytes(v) },
            { key: "sha256", label: "sha256", cls: "mono dim", fmt: v => v ? h("span", { class: "jb-sha", title: v }, String(v).slice(0, 10) + "…", " ", OG.copy(v, { label: "copy", title: "Copy the full sha256" })) : "-" },
            { key: "dl", label: "", sort: false, csv: false, fmt: (v, r) => h("a", { class: "btn sm", href: r.download || href(r.name), download: r.name, rel: "external" }, "Download") },
          ], rows, compact: true, csv: false, rowKey: r => r.name,
        }), ckNote] : [h("p", { class: "note" }, J.isTerminal(job) ? "No artifacts were stored." : "model.pt, config.json, metrics.json and env.json are uploaded when training ends (sha256 verified on receipt); checkpoints between epochs."), ckNote]);
      }

      function terminationBlock() {
        if (sandbox()) return OG.section("Termination", h("p", { class: "note" }, "SIMULATED: no instance existed, nothing to terminate."));
        const r = job.resource || {}, d = dep || {};
        const termEv = (d.events || []).filter(e => ["terminating", "terminated", "termination_failed"].includes(e.to)).pop();
        return OG.section("Termination evidence", kv([
          ["Auto-terminate deadline", r.terminate_deadline_at || d.terminate_deadline_at ? exact(r.terminate_deadline_at || d.terminate_deadline_at) : null, "set at approval: approval time + the runtime ceiling"],
          ["Terminate requested", r.terminate_requested_at || job.terminate_requested_at ? exact(r.terminate_requested_at || job.terminate_requested_at) : null],
          ["Provider terminated", r.terminated_at || d.provider_terminated_at ? exact(r.terminated_at || d.provider_terminated_at) : null, "the provider confirmed the instance gone (status read or instance list)"],
          ["Instance", r.provider_instance_id ? h("span", { class: "mono" }, r.provider_instance_id) : null],
          ["Last event", termEv ? h("span", {}, OG.stateBadge(termEv.to), " ", termEv.reason || "", termEv.evidence ? h("span", { class: "dep-det mono" }, " · " + evText(termEv.evidence)) : null) : null],
          r.termination_note ? ["Note", h("span", { class: "down" }, r.termination_note)] : null,
          ["Disposition", r.disposition ? h("span", { class: (DISPO[r.disposition] || [])[1] || "" }, (DISPO[r.disposition] || [r.disposition])[0]) : null, r.disposition_note],
        ]), d.ssh_access && OG.sshAccessBlock ? OG.sshAccessBlock(d.ssh_access, { purpose: d.purpose, keyRef: d.launch && d.launch.ssh_key, ephemeral: (job.ssh || {}).mode === "ephemeral" }) : null);
      }

      function costBlock() {
        const rec = dep && dep.reconciliation;
        const q = (rec && rec.quote) || {};
        const a = job.approval || {};
        const why = sandbox() ? "simulated: no spend" : dep && dep.reconciled_at ? "unavailable" : "reconciled after confirmed termination";
        const row = (label, badge, v, sub, reason) => h("div", { class: "dep-cost" }, h("div", { class: "rt-cl" }, label, badge),
          h("div", { class: "dep-cv" }, v != null ? J.usd(v) : OG.na(reason || why)), sub ? h("div", { class: "rt-cs" }, sub) : null);
        const qTotal = q.est_total_cost ?? a.est_total_cost ?? null;
        const qRate = q.price_per_gpu_hour ?? a.quote_price_per_gpu_hour ?? (dep && dep.prices ? dep.prices.quote : null);
        return OG.section("Cost: four numbers, never merged" + (sandbox() ? " · SIMULATED" : ""), h("div", { class: "dep-costs" },
          row("Quote", OG.conceptBadge("quote"), qTotal, qRate != null ? J.perGpuH(qRate) + (q.billing_unit ? " · billed " + q.billing_unit : "") : null, sandbox() ? "simulated: no quote" : qRate != null ? "hourly quote only" : "no quote yet (issued at submission)"),
          row("Expected cost", OG.kindBadge("estimated"), rec && rec.expected_cost ? rec.expected_cost.amount_usd : null, rec && rec.runtime ? `quote × ${fmt.num(rec.runtime.gpu_hours, 3)} metered GPU-h` : null),
          row("Provider-reported", OG.conceptBadge("execution"), rec && rec.provider_reported_cost ? rec.provider_reported_cost.amount_usd : null, rec && rec.provider_reported_cost ? rec.provider_reported_cost.basis || rec.provider_reported_cost.reason : null, rec && rec.provider_reported_cost && rec.provider_reported_cost.reason),
          row("OpenGrid transaction", OG.kindBadge("transaction"), rec && rec.opengrid_transaction_cost ? rec.opengrid_transaction_cost.amount_usd : null, rec && rec.opengrid_transaction_cost ? `${rec.opengrid_transaction_cost.usage_records} usage record(s) · fees ${J.usd(rec.opengrid_transaction_cost.fees_usd)} separate` : null)),
          h("p", { class: "note" }, "Estimates (", OG.kindBadge("estimated"), "): max ", J.usd(S().est_max), " · typical ", J.usd(S().est_typical),
            rec && rec.quote_error && rec.quote_error.amount_usd != null ? ` · quote error ${J.usd(rec.quote_error.amount_usd)} (${rec.quote_error.pct != null ? fmt.num(rec.quote_error.pct, 2) + "%" : "-"})` : "",
            dep ? [" · full reconciliation evidence on ", h("a", { class: "lnk mono", href: "/deployments/" + encodeURIComponent(dep.deployment_id) }, dep.deployment_id)] : null));
      }

      function decisionBlock() {
        const sel = job.selection || {};
        if (!sel.provider && !sel.market_best && !sel.why) return OG.section("Routing decision", h("p", { class: "note" }, sandbox() ? "Sandbox: no provider is called; any selection is informational." : "No selection stored on this job yet."));
        const mb = sel.market_best && typeof sel.market_best === "object" ? sel.market_best : null;
        const full = sel.execution_ready !== undefined || Array.isArray(sel.alternatives);
        return h("details", { class: "sec jb-dec", open: true }, h("summary", { class: "sec-h" }, "Routing decision · at submission"),
          kv([
            ["Selected", sel.provider ? h("span", {}, OG.providerLink(sel.provider), " · ", sel.gpu ? OG.gpuLink(sel.gpu) : "?", sel.region ? " · " + sel.region : "", sel.listing_id ? h("span", { class: "mono dimmer" }, " · " + sel.listing_id) : null) : null],
            ["Observed price", sel.price_per_gpu_hour != null ? h("span", {}, OG.conceptBadge("observed"), " ", J.perGpuH(sel.price_per_gpu_hour)) : null, "the observed listing price at selection; the quote is issued by the route"],
            ["Why", sel.why || null],
            ["Market recommendation", mb ? h("span", {}, OG.providerName(mb.provider), " · ", OG.shortGpu(mb.gpu || ""), " · ", J.perGpuH(mb.price_per_gpu_hour), mb.executable === false ? h("span", { class: "dim" }, " · not executable") : null)
              : typeof sel.market_best === "string" ? sel.market_best : null],
            ["Execution mode", sel.execution_mode || null],
          ]),
          full ? J.previewPanel(sel, { title: "Decision", sandbox: sandbox(), admin }) : null);
      }

      function eventsBlock() {
        const ev = job.events || [];
        if (!ev.length) return OG.section("Job events", OG.empty("No events recorded."));
        const to = e => e.to || e.to_status || e.state, from = e => e.from || e.from_status;
        return OG.section("Job events · " + ev.length, h("ol", { class: "dep-tl dep-tl2" }, ev.slice().sort((a, b) => String(b.at).localeCompare(String(a.at))).map(e => h("li", { class: "t-" + (J.tone(to(e)) || "plain") + (from(e) === to(e) ? " note-ev" : "") },
          h("span", { class: "mono dim dep-at", title: exact(e.at) }, fmt.dateTime(e.at) + ":" + String(new Date(e.at).getSeconds()).padStart(2, "0")),
          h("span", { class: "dep-tr" }, from(e) === to(e) ? h("span", { class: "dim" }, "note") : [h("span", { class: "dim" }, String(from(e) || "∅").replace(/_/g, " ") + " → "), J.stateBadge(to(e))]),
          e.actor ? h("span", { class: "badge dep-actor a-" + e.actor }, e.actor + (e.actor_id ? " · " + e.actor_id : "")) : null,
          h("span", { class: "dep-reason" }, e.reason || ""),
          e.evidence ? h("span", { class: "dep-det mono" }, evText(e.evidence)) : null))));
      }

      function configBlock() {
        const c = job.config || {}, rq = job.request || {}, r = rq.resources || {}, l = rq.limits || {}, ssh = job.ssh || {}, b = job.bootstrap || {};
        return OG.section("Request", kv([
          ["Template", job.template ? h("span", { class: "mono" }, job.template, job.template_version ? h("span", { class: "dim" }, " v" + job.template_version) : null) : null],
          ["Config", Object.keys(c).length ? Object.entries(c).map(([k, v]) => `${k} ${v}`).join(" · ") : null],
          ["Resources", `${r.gpu_count || 1}× ${r.gpu || "auto"} · ${r.region || "any region"}${r.strict_region ? " (strict)" : ""} · ${r.allow_spot ? "spot allowed" : "on-demand"}`],
          ["Strategy", (rq.routing && rq.routing.strategy) || null],
          ["Limits", `≤ ${J.perGpuH(l.max_price_per_gpu_hour_usd)} · ≤ ${J.usd(l.max_total_cost_usd)} total · ${l.max_runtime_minutes ?? "?"} min`],
          ["SSH", ssh.mode === "customer" ? h("span", { class: "mono" }, ssh.fingerprint || "your key") : ssh.mode === "ephemeral" ? "ephemeral key, private half discarded: nobody can SSH in" : null, ssh.note],
          ["Bootstrap", b.sha256 ? h("span", { class: "mono", title: b.sha256 }, String(b.sha256).slice(0, 16) + "…", b.template_version ? h("span", { class: "dim" }, " · v" + b.template_version) : null) : null, b.note || "sha256 of the rendered startup script (the script itself is never stored)"],
        ]));
      }

      function draw() {
        if (!job) return;
        top.replaceChildren(...header().filter(Boolean), stageBlock(), approvalBlock(), timelineBlock());
        mid.replaceChildren(progressBlock(), artifactsBlock());
        advTop.replaceChildren(metaLine(), statsStrip(), strips());
        bottom.replaceChildren(
          h("div", { class: "cols-2" }, checksBlock(), terminationBlock()),
          costBlock(),
          h("div", { class: "cols-2" }, eventsBlock(), configBlock()),
          decisionBlock(),
          h("p", { class: "note" }, stopped || J.isTerminal(job) ? "Finished: polling stopped. " : `Live: refreshed every ${POLL_MS / 1000} s while the job is active. `,
            "Job state and deployment state are separate machines. ", h("a", { class: "lnk", href: "/methodology/execution-safety" }, "Execution safety →")));
        if (root.firstChild !== top) root.replaceChildren(top, mid, adv);
      }

      /* ---------------- cancel ---------------- */
      async function cancel(ca) {
        ca = ca || J.cancelAction(job);
        if (!ca) return;
        const ok = await OG.dialog({
          title: ca.title, danger: true, confirm: ca.confirm, cancel: ca.keep, ack: ca.ack,
          body: h("div", {}, h("p", {}, ca.body), h("p", { class: "dim" }, "Safe to retry: a second click after a network error can't act twice.")),
        });
        if (!ok) return;
        I.cancel = OG.intentFor(I.cancel, "cancel:" + id, {});
        flash.replaceChildren(OG.loading(ca.kind === "terminate" ? "Asking the provider to terminate the GPU…" : "Cancelling…"));
        try {
          await OG.api.intent(I.cancel, `/v1/jobs/${enc}/cancel`, { method: "POST", body: {} });
          flash.replaceChildren(h("div", { class: "og-ok" }, h("b", {}, ca.kind === "terminate" ? "Termination requested" : "Cancel requested"),
            ca.kind === "terminate" ? h("span", { class: "dim" }, " The GPU counts as running until the provider confirms it is gone.") : null));
          stopped = false; await load(); schedule();
        } catch (e) {
          flash.replaceChildren(J.apiErr(e, "POST /v1/jobs/{id}/cancel", { admin }), I.cancel.state === "unknown" ? h("p", { class: "warn-t" }, "Outcome unknown. Press the button again: it reuses the same request key (" + I.cancel.key.slice(0, 12) + "…), so it can't act twice.") : null);
        }
      }

      /* ---------------- logs: cursor-paged, filter, auto-scroll ---------------- */
      function logPanel() {
        const MAX = 5000;
        let after = null, lines = [], q = "", auto = true, err = null, busy = false;
        const pre = h("pre", { class: "jb-log mono", tabindex: "0", "aria-label": "Job logs" });
        const filt = h("input", { class: "field jb-lf", type: "search", placeholder: "filter logs", spellcheck: "false", "aria-label": "Filter logs", oninput: e => { q = e.target.value.toLowerCase(); paint(); } });
        const autoIn = h("input", { type: "checkbox", checked: true, onchange: e => { auto = e.target.checked; if (auto) pre.scrollTop = pre.scrollHeight; } });
        const count = h("span", { class: "dim mono" });
        const status = h("div");
        const el = h("section", { class: "sec jb-logs" },
          h("div", { class: "jb-lh" }, h("h2", { class: "sec-h" }, "Logs"), count, h("span", { class: "spacer" }), filt,
            h("label", { class: "rt-chk" }, autoIn, "auto-scroll"), OG.copy(() => lines.map(l => l.text).join("\n"), { label: "Copy all" })),
          pre, status);
        function paint() {
          const shown = q ? lines.filter(l => l.text.toLowerCase().includes(q)) : lines;
          pre.replaceChildren(...shown.slice(-2000).map(l => h("span", { class: "jb-ll" + (l.stream && /err/.test(l.stream) ? " jb-ll-err" : l.stream && /warn|system/.test(l.stream) ? " jb-ll-warn" : l.stream === "simulated" ? " jb-ll-sim" : "") },
            l.at ? h("span", { class: "dimmer" }, fmt.time(l.at) + " ") : null, l.text + "\n")));
          count.textContent = q ? `${shown.length} of ${lines.length} lines` : `${lines.length} lines`;
          if (!shown.length) pre.append(h("span", { class: "dimmer" }, err ? "" : job && J.PRE_LAUNCH.includes(J.stateOf(job)) && !J.isSandbox(job) ? "No logs yet: the runner starts after approval and launch." : "No log lines yet."));
          if (auto) pre.scrollTop = pre.scrollHeight;
          status.replaceChildren(err ? J.notYet("GET /v1/jobs/{id}/logs", err) : null);
        }
        async function pull() {
          if (busy) return;
          busy = true;
          try {
            for (let i = 0; i < 10; i++) {
              const raw = await OG.api("/v1/jobs/" + enc + "/logs", { params: { after: after ?? undefined, limit: 500 }, nocache: true });
              const p = J.logPage(raw, after);
              err = null;
              if (p.lines.length) { lines = lines.concat(p.lines); if (lines.length > MAX) lines = lines.slice(-MAX); }
              const moved = p.next != null && p.next !== after;
              after = p.next;
              if (!p.more || !moved || !p.lines.length) break;
            }
          } catch (e) { err = e; }
          busy = false;
          if (ctx.alive()) paint();
        }
        paint();
        return { el, pull };
      }

      await load();
      if (!ctx.alive() || stopped) return;
      if (!J.isTerminal(job)) schedule(); else { stopped = true; draw(); }
    },
  });

  /* ---- a small line chart over epochs (pure SVG; x = epoch, not time) ---- */
  function line(el, xs, ys, o) {
    const pts = xs.map((x, i) => [x, ys[i]]).filter(p => p[1] != null && isFinite(p[1]));
    if (!pts.length) { el.replaceChildren(h("div", { class: "note" }, "No " + o.label + " points yet.")); return; }
    const W = 420, H = 150, L = 46, R = 10, T = 10, B = 22;
    const x0 = Math.min(...pts.map(p => p[0])), x1 = Math.max(...pts.map(p => p[0]));
    let y0 = Math.min(...pts.map(p => p[1])), y1 = Math.max(...pts.map(p => p[1]));
    if (y0 === y1) { y0 -= Math.abs(y0) * 0.1 || 0.5; y1 += Math.abs(y1) * 0.1 || 0.5; }
    const pad = (y1 - y0) * 0.08; y0 -= pad; y1 += pad;
    const sx = x => L + (x1 === x0 ? (W - L - R) / 2 : (x - x0) / (x1 - x0) * (W - L - R));
    const sy = y => T + (1 - (y - y0) / (y1 - y0)) * (H - T - B);
    const S = OG.s;
    const ticks = [y0 + pad, (y0 + y1) / 2, y1 - pad];
    const xt = [...new Set(pts.map(p => p[0]))]; const step = Math.ceil(xt.length / 8);
    const lastP = pts[pts.length - 1];
    el.replaceChildren(h("div", { class: "chart jb-line" }, S("svg", { viewBox: `0 0 ${W} ${H}`, width: "100%", height: H, role: "img", "aria-label": o.label },
      ticks.map(t => [S("line", { x1: L, x2: W - R, y1: sy(t), y2: sy(t), stroke: "var(--chart-grid)" }), S("text", { x: L - 6, y: sy(t) + 3, "text-anchor": "end", class: "c-ax" }, o.yFmt(t))]),
      xt.filter((x, i) => i % step === 0).map(x => S("text", { x: sx(x), y: H - 6, "text-anchor": "middle", class: "c-ax" }, String(x))),
      S("text", { x: W - R, y: H - 6, "text-anchor": "end", class: "c-ax" }, "epoch"),
      S("polyline", { points: pts.map(p => sx(p[0]).toFixed(1) + "," + sy(p[1]).toFixed(1)).join(" "), fill: "none", stroke: o.color, "stroke-width": 1.6 }),
      pts.map(p => S("circle", { cx: sx(p[0]), cy: sy(p[1]), r: 2.6, fill: o.color }, S("title", {}, `epoch ${p[0]}: ${o.yFmt(p[1])}`))),
      S("text", { x: Math.max(L + 30, sx(lastP[0]) - 4), y: Math.max(T + 8, sy(lastP[1]) - 6), "text-anchor": "end", class: "c-lab" }, o.yFmt(lastP[1])))));
  }
})();
