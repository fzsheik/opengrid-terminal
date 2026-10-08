// Execution UI pure parts of web/core.js: idempotency intents, the product tracker, the state strip.
// Run: node tests/test_web_exec.js
const OG = require("../web/core.js");
let failures = 0;
const ok = (c, m) => { if (!c) { failures++; console.log("FAIL:", m); } else console.log("ok  :", m); };
const eq = (a, b, m) => ok(JSON.stringify(a) === JSON.stringify(b), m + " -> " + JSON.stringify(a));

(async () => {
  // ---------- idempotency keys: one per user intent ----------
  const a = OG.intentFor(null, "terminate:dep-1", {});
  ok(/^ogui-[a-z0-9]{26}$/.test(a.key), "key format " + a.key);
  ok(OG.intentFor(null, "terminate:dep-1", {}).key !== a.key, "a new intent gets a new key");
  ok(OG.intentFor(a, "terminate:dep-1", {}) !== a, "an intent that never ran is not reused by the next click");
  OG.settleIntent(a, { status: 0, message: "network error" });
  ok(a.state === "unknown" && a.attempts === 1, "network error -> outcome unknown");
  ok(OG.intentFor(a, "terminate:dep-1", {}) === a, "retry of the same action after a network error reuses the key");
  ok(OG.intentFor(a, "terminate:dep-2", {}) !== a, "a different action never reuses the key");
  const b = OG.intentFor(null, "approve:rr_1", { quote_id: "q_1", override_limits: false });
  OG.settleIntent(b, { status: 502 });
  ok(OG.intentFor(b, "approve:rr_1", { override_limits: false, quote_id: "q_1" }) === b, "same body (any key order) after a 5xx reuses the key");
  ok(OG.intentFor(b, "approve:rr_1", { quote_id: "q_1", override_limits: true }) !== b, "a different body is a new intent (the server would 422 a reused key)");
  const c = OG.intentFor(null, "route", { gpu: "h100" });
  OG.settleIntent(c, { status: 409, body: { detail: { code: "idempotency_in_progress" } } });
  ok(c.state === "unknown" && OG.intentFor(c, "route", { gpu: "h100" }) === c, "409 idempotency_in_progress -> reuse");
  const d = OG.intentFor(null, "route", { gpu: "h100" });
  OG.settleIntent(d, { status: 409, body: { detail: { code: "limits_exceeded" } } });
  ok(d.state === "failed" && OG.intentFor(d, "route", { gpu: "h100" }) !== d, "a definitive 409 settles the intent: next click = new key");
  const e = OG.intentFor(null, "route", { gpu: "h100" });
  OG.settleIntent(e);
  ok(e.state === "done" && OG.intentFor(e, "route", { gpu: "h100" }) !== e, "after success a new click is a new intent");
  ok(OG.outcomeUnknown({ status: 429 }) && OG.outcomeUnknown({ status: 500 }) && !OG.outcomeUnknown({ status: 422 }) && !OG.outcomeUnknown({ status: 404 }) && !OG.outcomeUnknown(null), "unknown-outcome classification");
  eq(OG.stableJson({ b: 1, a: { d: [2, { y: 1, x: 0 }], c: null } }), '{"a":{"c":null,"d":[2,{"x":0,"y":1}]},"b":1}', "stable JSON sorts keys");

  // the header goes out on the request, and api.intent never retries by itself
  const sent = [];
  global.fetch = async (url, init) => { sent.push(init.headers["idempotency-key"]); return { ok: false, status: 503, headers: { get: () => "application/json" }, json: async () => ({ detail: "busy" }) }; };
  const i1 = OG.intentFor(null, "terminate:dep-9", {});
  await OG.api.intent(i1, "/v1/deployments/dep-9/terminate", { method: "POST" }).catch(() => {});
  ok(sent.length === 1 && sent[0] === i1.key, "Idempotency-Key header sent; exactly one request (no auto-retry)");
  const i2 = OG.intentFor(i1, "terminate:dep-9", {});
  await OG.api.intent(i2, "/v1/deployments/dep-9/terminate", { method: "POST" }).catch(() => {});
  ok(sent.length === 2 && sent[1] === i1.key, "the user's retry after 503 carries the same key");
  global.fetch = async (url, init) => { sent.push(init.headers["idempotency-key"]); return { ok: true, status: 202, headers: { get: () => "application/json" }, json: async () => ({ data: { ok: 1 }, meta: {} }) }; };
  await OG.api.intent(i2, "/v1/deployments/dep-9/terminate", { method: "POST" });
  ok(i2.state === "done" && OG.intentFor(i2, "terminate:dep-9", {}).key !== i1.key, "after the answer arrives, a new click gets a new key");
  await OG.api("/v1/x", { method: "POST" });
  ok(sent[sent.length - 1] === undefined, "no key unless the caller passes one");

  // ---------- tracker ----------
  const store = new Map(), storage = { getItem: k => store.get(k) || null, setItem: (k, v) => store.set(k, v) };
  const batches = [];
  const t = OG.makeTracker({ storage, send: b => { batches.push(b); return Promise.resolve(); } });
  ok(t.track("page_view", { page: "/gpus" }) && !t.track("route_preview", {}) && !t.track("nope"), "only client events are queued");
  for (let i = 0; i < 119; i++) t.track("search", { q: "h" + i });
  ok(t.queue.length === 120, "queued");
  ok(t.flush() === 3 && batches.map(x => x.length).join(",") === "50,50,20" && t.queue.length === 0, "flush batches by 50 and empties the queue");
  const anon = batches[0][0].anon_id;
  ok(/^[A-Za-z0-9_-]{8,64}$/.test(anon) && batches.every(bt => bt.every(x => x.anon_id === anon)) && store.get("og-anon") === anon, "one random anon id per session (sessionStorage)");
  ok(OG.makeTracker({ storage, send: () => {} }).anonId() === anon, "the same session keeps its id");
  ok(!Object.keys(batches[0][0]).some(k => /ip|email|cookie|user/i.test(k)), "no PII fields");
  const failing = OG.makeTracker({ storage, send: () => Promise.reject(new Error("down")) });
  failing.track("page_view", {});
  let threw = false;
  try { failing.flush(); await new Promise(r => setTimeout(r, 5)); } catch (err) { threw = true; }
  ok(!threw && failing.queue.length === 0, "failures are dropped silently (never re-queued)");
  const throwing = OG.makeTracker({ storage, send: () => { throw new Error("sync"); } });
  throwing.track("page_view", {});
  ok(throwing.flush() === 1, "a throwing sender does not break the page");
  const dnt = OG.makeTracker({ dnt: true, storage, send: b => batches.push(b) });
  const before = batches.length;
  ok(!dnt.track("page_view", {}) && dnt.flush() === 0 && batches.length === before && !dnt.enabled, "Do Not Track: nothing queued or sent");
  const big = OG.makeTracker({ storage, send: () => {} });
  for (let i = 0; i < 600; i++) big.track("page_view", {});
  ok(big.queue.length === 500, "the queue is bounded");

  // ---------- deployment state strip ----------
  const marks = s => s.map(x => x.state + ":" + x.mark[0] + (x.tone ? "/" + x.tone : "")).join(" ");
  const run = OG.dep.strip("running", ["created", "quoted", "pending_approval", "approved", "provisioning", "provisioning", "running"]);
  eq(marks(run), "created:d quoted:d pending_approval:d approved:d provisioning:d running:c/good stopping:t stopped:t terminating:t terminated:t", "running");
  const term = OG.dep.strip("terminated", ["created", "quoted", "pending_approval", "approved", "provisioning", "running", "terminating", "terminated"]);
  ok(term.find(x => x.state === "stopping").mark === "skipped" && term.find(x => x.state === "terminated").mark === "current", "never-visited states before the current one are skipped");
  const unk = OG.dep.strip("launch_unknown", ["created", "quoted", "pending_approval", "approved", "provisioning", "launch_unknown"]);
  ok(unk.length === 11 && unk[5].state === "launch_unknown" && unk[5].off && unk[5].tone === "warn" && unk[4].mark === "done" && unk[6].mark === "todo", "uncertain state inserted after the furthest reached step, amber");
  const tf = OG.dep.strip("termination_failed", ["created", "quoted", "pending_approval", "approved", "provisioning", "running", "terminating", "termination_failed"]);
  ok(tf[9].state === "termination_failed" && tf[9].tone === "bad", "termination_failed is red, after terminating");
  const rej = OG.dep.strip("rejected", ["created", "quoted", "pending_approval", "rejected"]);
  ok(rej[3].state === "rejected" && rej[3].tone === "bad" && rej[4].state === "approved" && rej[4].mark === "todo", "rejected after pending approval");
  ok(OG.dep.strip("provision_failed", []).filter(x => x.mark === "current").length === 1, "no history still yields one current step");
  ok(OG.dep.tone("orphan_suspected") === "warn" && OG.dep.tone("credentials_unavailable") === "warn" && OG.dep.tone("provision_failed") === "bad" && OG.dep.tone("pending_approval") === "busy", "tones");
  ok(OG.dep.LIVE.includes("launch_unknown") && !OG.dep.LIVE.includes("pending_approval"), "live states (may bill)");

  // ---------- SSH access + auto-terminate text (pure helpers in web/pages/deployments.js) ----------
  global.OG = OG;
  if (typeof OG.page !== "function") OG.page = () => {};
  require("../web/pages/deployments.js");
  const X = OG.execText;
  const s1 = X.sshAccess({ customer_key_fingerprint: "SHA256:abc123", operator_access: "NONE" }, { purpose: "customer" });
  eq(s1.text, "SSH access:\nCustomer supplied key: SHA256:abc123\nOpenGrid operator access: NONE", "founder's SSH format");
  ok(!s1.missingKey && !s1.needsOverride && s1.lines.every(l => !l.tone), "a customer key and no operator access: nothing red");
  ok(X.sshAccess({ customer_key_fingerprint: "SHA256:x", operator_access: "none" }).lines[1].value === "NONE", "lower-case none -> NONE");
  const s2 = X.sshAccess({ customer_key_fingerprint: "SHA256:x", operator_access: "provider_forced_account_key:override_by:admin-7" });
  ok(s2.lines[1].tone === "bad" && s2.lines[1].value.startsWith("provider_forced_account_key:override_by:admin-7") && /override by admin-7/.test(s2.lines[1].value), "operator override: red, exact value, who overrode");
  const s3 = X.sshAccess({ customer_key_fingerprint: "SHA256:x", operator_access: "blocked:provider_forced_account_key" });
  ok(s3.needsOverride && s3.lines[1].tone === "bad" && s3.lines[1].value.startsWith("blocked:provider_forced_account_key"), "provider may force account keys -> override needed, red");
  const s4 = X.sshAccess({ customer_key_fingerprint: null, operator_access: "NONE" }, { purpose: "customer" });
  ok(s4.missingKey && s4.lines[0].tone === "bad" && /MISSING/.test(s4.lines[0].value), "missing customer key is said and red");
  ok(!X.sshAccess({ customer_key_fingerprint: null, operator_access: "NONE" }, { keyRef: "my-key" }).missingKey, "a BYO key name is not a missing key");
  const s5 = X.sshAccess({ customer_key_fingerprint: null, operator_access: "validation_operator_key" }, { purpose: "validation" });
  ok(!s5.missingKey && s5.lines[1].value === "OpenGrid operator key (validation only)" && s5.lines[1].tone === "bad", "validation: operator key (validation only)");
  ok(X.sshAccess(null).lines[1].value.startsWith("UNKNOWN"), "no ssh_access in the response -> unknown, not NONE");

  const iso = "2026-10-07T14:05:09Z", dt = new Date(iso), P = n => String(n).padStart(2, "0");
  const local = `${dt.getFullYear()}-${P(dt.getMonth() + 1)}-${P(dt.getDate())} ${P(dt.getHours())}:${P(dt.getMinutes())}:${P(dt.getSeconds())}`;
  eq(X.exactTime(iso), `${local} local (UTC 2026-10-07 14:05:09)`, "exact local + UTC time");
  ok(X.exactTime(null) === null && X.exactTime("nope") === null, "no time -> null");
  eq(X.autoTerminate(iso, 60, "system_default"), `Auto-terminates: ${local} local (UTC 2026-10-07 14:05:09) — max runtime 60 min, source system default`, "auto-terminate line");
  ok(["request", "account", "system default", "hard max", "validation cap"].join() === ["request", "account", "system_default", "system_hard_max", "validation_cap"].map(X.ceilingSource).join(), "ceiling source labels");
  ok(/not set yet/.test(X.autoTerminate(null, 30, "validation_cap")), "deadline not set yet");
  const now = Date.parse(iso);
  eq(X.deadline("2026-10-07T15:09:21Z", now), { past: false, seconds: 3852, text: "in 1h 04m" }, "deadline countdown");
  eq(X.deadline("2026-10-07T14:01:59Z", now), { past: true, seconds: -190, text: "PAST DEADLINE by 3m 10s" }, "past deadline");
  ok(X.durationWarning(2, 60) && !X.durationWarning(1, 60) && !X.durationWarning(null, 60) && X.durationWarning(0.5, 10, "server says") === "server says", "duration vs ceiling warning");
  ok(X.billingAlarm({ status: "running", terminate_deadline_at: "2026-10-07T14:00:00Z" }, now).kind === "past_deadline", "running past the deadline -> PAST DEADLINE banner");
  ok(X.billingAlarm({ status: "running", terminate_deadline_at: "2026-10-07T15:00:00Z" }, now) === null, "running before the deadline -> no banner");
  ok(X.billingAlarm({ status: "terminated", terminate_deadline_at: "2026-10-07T14:00:00Z" }, now) === null, "terminated past the deadline -> no banner");
  ok(X.billingAlarm({ status: "launch_unknown" }, now).kind === "uncertain" && X.billingAlarm({ status: "termination_failed" }, now).title === "POSSIBLY BILLING", "uncertain -> POSSIBLY BILLING");

  console.log(failures ? `\n${failures} FAILED` : "\nall passed");
  process.exit(failures ? 1 : 0);
})();
