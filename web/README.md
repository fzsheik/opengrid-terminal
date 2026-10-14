# OpenGrid Terminal web client

No build step. Plain scripts loaded by `index.html` in this order:
`lib.js` (pure helpers, tested by `tests/test_lib.js`) → `core.js` (global `OG`) → `charts.js` (`OG.charts`)
→ `pages/*.js` (one file per section, each calls `OG.page`) → `OG.start()`.

`classic.html` is the older standalone page at `/classic` (tested by `tests/test_page.js`); leave it alone.
`app.js` is a legacy shim kept so `/static/app.js` still resolves.

The server (`api/pages.py`) serves `index.html` for every page path with the title, description,
canonical URL, Open Graph tags, a boot JSON block and an SEO summary (`#ssr`) filled in.
**A new page path must be added to `PAGES` in `api/pages.py`**, or reloading it 404s
(`tests/test_pages.py` checks every `OG.page` pattern has a server route).

## Writing a page (wave-2 agents: edit only your `pages/<name>.js`)

```js
OG.page("/gpu/:slug", {
  title: p => OG.shortGpu(OG.data.gpuName(p.slug) || p.slug), // string or (params, query) => string
  nav: "gpus",                                                 // left-nav key to highlight (default: first path segment)
  mount(el, params, query, ctx) {                              // may be async; may return a cleanup fn
    el.append(OG.head("H100 80GB SXM5", "sub line", OG.seg([["1", "1H"], ["24", "24H"]], "24", v => …)));
    ctx.api("/v1/gpus/" + params.slug).then(d => …, e => el.append(OG.error(e)));
    ctx.every(30000, refresh);   // cleared on navigation, skipped while the tab is hidden
    ctx.onCleanup(() => …);
  },
});
```

- Patterns: literals, `:name` (one path segment, non-greedy, may share a segment with literal text:
  `/compare/:a-vs-:b`), `*` (rest → `params.rest`). Most literal characters wins. Params are URI-decoded.
- `ctx`: `{path, params, query, ssr, alive(), onCleanup(fn), every(ms, fn), api(path, opts), setTitle(t)}`.
  `ctx.api` never settles after unmount, so late answers cannot paint over the next page.
  `ctx.ssr` is the server-rendered `#ssr` node for this path on the first page load (else null).
- Navigation: `OG.go(url, {replace})`. Plain `<a href="/x">` links are intercepted when a page matches
  (modifier-clicks, `target`, `download`, `rel="external"`, `/static`, `/v1`, `/docs` are left to the browser).
- URL state (shareable): `OG.qs.get(key, default)`, `OG.qs.all()`, `OG.qs.set({key: value | null}, {push})`
  — replaces the URL without remounting. Pure: `OG.qs.parse(str)`, `OG.qs.stringify(obj)`.

## Data

- `OG.api(path, {params, method, body, headers, full, slot, nocache, signal})` → `Promise<data>`.
  Unwraps `{data, meta}`; `{full: true}` (or `OG.api.full`) → `{data, meta}`. Non-envelope JSON passes through.
  Errors: `OG.ApiError {status, message (the API's detail), path, body}`; network failure is status 0.
  Identical concurrent GETs share one request. `slot: "name"`: only the newest request in that slot
  resolves; older ones reject with `err.stale === true` (ignore those).
- `OG.api.soft(path, opts)` → data or `null` on any error. Use it for endpoints that may not exist yet.
- `OG.data.gpuName(slug)` (sync, from the boot block), `OG.data.gpus()` → `[{slug, name, short, vendor, weight, live}]`,
  `OG.data.providers()` → provider_meta dicts, `OG.data.market(hours)` → cached `/market`.
  `OG.data.families()` (soft /v1/families, cached), `OG.data.family(slug)` (one family or null), `OG.data.intervals()`
  (Map provider → poll seconds). A `/gpu/<family>` path renders `pages/family.js` (variants side by side, never merged).
  `OG.slug(name)` equals `api.common.gpu_slug`. `OG.resolveGpu(q)` (async fuzzy), `OG.resolveProvider(q)`.
- `OG.boot` = `{base_url, gpus: [[slug, name]], providers: [...], methodology: [names]}`.

## Honesty components (use them; never print a placeholder number)

- `OG.insufficient(reason, title)` — block shown when the API returns `null` + a reason.
- `OG.na(reason)` — inline `n/a` carrying the reason; `OG.value(v, fmtFn, reason)` picks value or `na`.
- `OG.reasonOf(obj)` reads `reason | unavailable_reason | insufficient_reason | coverage_reason`.
- `OG.kindBadge("observed" | "estimated" | "transaction"; "inferred" renders nothing)` (links to /methodology/data-kinds),
  `OG.freshBadge(isoOrSeconds | "fresh" | "aging" | "stale", {intervalSeconds, provider, now})` — relative to the
  provider's poll interval when known (fresh ≤ 2×, aging ≤ 6×; `{provider}` looks it up from /polling via
  `OG.data.intervals()`), else fresh ≤ 30 min, aging ≤ 3 h. `OG.badge(text, tone)`.
- `OG.loading(text)`, `OG.empty(text)`, `OG.error(err, retryFn)`.

## Components

- `OG.h(tag, props, ...kids)` / `OG.s(tag, attrs, ...kids)` (SVG). Props: `class`, `style` (string or object),
  `on<event>`, any attribute; `null`/`false` skipped. Kids: nodes, strings, nested arrays.
  `append` / `prepend` / `replaceChildren` are patched app-wide to drop `null` / `undefined` / booleans and flatten
  arrays (`OG.cleanKids(kids)` is the pure helper; `OG.fill(el, ...kids)` the explicit form).
- `OG.head(title, sub, ...right)`, `OG.section(title, ...kids)`, `OG.stub(el, {title, line, links: [[href, label]]})`.
- `OG.stats([{label, value, sub, change, invert, reason, kind, title}])` — compact strip; `value: null` shows `n/a` + reason.
  `sub` is a short line, `title` the full text on hover; a long reason is shortened in the cell (`OG.shortReason`)
  and kept whole in the tooltip.
- `OG.dialog({title, body, confirm, cancel, danger, ack})` → `Promise<boolean>` (confirmation modal; `ack` = required checkbox).
- `OG.drawer({label, cls, title, onClose})` → `{el, head(...nodes), body(...nodes), append(...), close(silent), open}` (right panel; Esc closes).
- `OG.copy(text | () => text, {label, cls, title})` → copy-to-clipboard button; `OG.copyText(text)` → Promise<bool>.
- `OG.conceptBadge("list" | "observed" | "quote" | "execution")`, `OG.money(usd)`.
- `OG.seg(options, value, onChange)` / `OG.tabs(...)`; options `[[value, label, title?]]`; returns el with `.set(v)`.
- `OG.table(spec)` → element with `.update(rows)`, `.sorted()`, `.setSort({key, dir})`, `.highlight(rowKey)`:
  `spec = {columns, rows, sort: {key, dir}, onSort(sort), rowHref(row), onRow(row, ev), rowKey(row), rowClass(row),
  onHover(row|null), csv: "file.csv" | false, title, toolbar: [nodes], limit (500), compact, empty, stickyLast: n}`
  (`stickyLast`: the last n columns stay visible when the table scrolls sideways);
  `column = {key, label, num, fmt(value, row) → string|Node, value(row) (sort + CSV), href(row), sort: false,
  desc (first click descending), title, cls (string | row => string), width, hidden, csv: false | row => value, csvLabel}`.
  Nulls sort last in both directions; sticky header; numeric columns right-aligned tabular.
- `OG.csv(rows, columns, filename)` (download), `OG.csvString(rows, columns)` (formula-injection safe).
- `OG.chg(fraction, {invert, reason})` ▲/▼ span; `OG.logo(provider, size)`, `OG.providerLink(p)`, `OG.gpuLink(nameOrSlug)`.
- `OG.fmt.price/pct/num(v, digits)/compact/age(isoOrSeconds)/time/date/dateTime/dir/cls(v, invert)`.
- `OG.status.asOf(iso)`, `OG.status.live(ok, text)`, `OG.status.tape([nodes])` (top bar).
- Shared interim views (pages/market.js): `OG.views.marketBoard(el, ctx)`, `OG.views.windowSeg(hours, onChange)`, `OG.views.hours()`.

## Execution (money-moving actions, state, analytics)

- **Idempotency.** `POST /v1/route`, `/v1/route/{id}/approve`, `/v1/deployments/{id}/terminate|stop` (and the admin
  orphan / validation / force-terminate POSTs) need an `Idempotency-Key`. ONE key per user intent:
  `intent = OG.intentFor(previous, "terminate:" + id, body)` then `OG.api.intent(intent, path, {method: "POST", body})`.
  The key is reused only when the previous attempt of the SAME action + body ended with an unknown outcome
  (network error, 5xx, 429, 409 `idempotency_in_progress`); any definitive answer settles it and the next click gets
  a new key. Nothing retries automatically. `OG.api(path, {idempotencyKey})` sets the header directly.
- **State machine** (mirrors routing/deployments.py): `OG.dep.{FLOW, UNCERTAIN, FAILED, LIVE, tone(s), strip(status, history)}`;
  `OG.stateBadge(s)` (uncertain amber, failures red), `OG.stateStrip(status, events)`.
- `OG.countdown(iso, {expired, onExpire, soon})`, `OG.modeBanner(modeStatus)` (+ `OG.MODE_HELP`),
  `OG.ask({title, body, fields: [{key, label, type, required, minLength, match, show}], confirm, danger})` → values | null
  (typed confirmations: provider name, instance id, "STOP"), `OG.violationTable(limit_violations)` (pages/deployments.js).
- `OG.me()` (cached `/v1/me`), `OG.isAdmin(me)`; the nav's `.nav-admin` entries are shown only to admins.
- **Product analytics**: `OG.track(event, props)` for page_view / search / gpu_view / provider_view / compare /
  watchlist_create (page views, command-bar searches and GPU / provider / compare views are tracked by core.js).
  Anonymous id in sessionStorage, no cookies, no PII; batches of ≤ 50 to `POST /v1/events/track` every 10 s and on
  pagehide; failures dropped; nothing is tracked when `navigator.doNotTrack === "1"`. Pure part: `OG.makeTracker`.
- Pages: `/route` (ticket: `?rr=<route_request_id>`), `/deployments`, `/deployments/:id`, `/onboarding`
  (`?account=<id>` operator preview), `/admin/execution`, `/admin/checklist`, `/admin/partners`, `/admin/value`.
  Tests: `node tests/test_web_exec.js`.

## Jobs (`pages/jobs.js`, `pages/job.js`; nav Execute → Jobs; commands `j`, `j new`)

A JOB is the workload and its outcome; a DEPLOYMENT is the cloud resource. Both states are always shown, never merged.

- `/jobs` — `GET /v1/jobs`: name, id, template, state, outcome, provider, GPU, created, runtime, est. max, actual
  (reconciled transaction only), SIMULATED / real; the "Train Your First AI Model — PyTorch MNIST" CTA.
- `/jobs/new` — templates from `GET /v1/jobs/templates` (config_schema bounds, limits_bounds, preset); the MNIST preset
  (founder's defaults); client validation mirroring `workloads/spec.py`; optional SSH public key (empty = ephemeral key,
  private half discarded: nobody can SSH in); Sandbox ("SIMULATED — no GPU, no spend"); readiness from
  `GET /v1/jobs/readiness`. **Preview route** = `POST /v1/jobs/preview`: three separate views — MARKET RECOMMENDATION
  (`market_best`, information only), EXECUTION-READY ROUTE (`execution_ready`), SANDBOX — plus the estimate (typical /
  max, billed minutes, billing unit, basis), why it won (score breakdown), alternatives, exclusions, ineligible GPUs,
  `submission` and a BLOCKERS box with links (global first, then per provider). **Submit** = `POST /v1/jobs` (one
  Idempotency-Key per intent) after a dialog stating the max cost exposure, the runtime ceiling and that approval is
  required. A real job cannot be submitted without an execution-ready route.
- `/jobs/:id` — `GET /v1/jobs/{id}` polled every 4 s until `finished`: header, SIMULATED banner, job lifecycle strip
  (sandbox: its shorter flow) + the deployment's own strip (`GET /v1/deployments/{id}`), approval block (admins approve on
  the existing ticket `/route?rr=…`; no second approval path), progress + loss / accuracy charts from `/metrics` only,
  cursor-paged logs (`?after=<id>`, filter, auto-scroll), the 12 checks (✓ only with evidence; sandbox shows `~
  simulated`, never ✓), artifacts (`/artifacts/{name}`, sha256, size), termination evidence, the four cost numbers,
  events, request, routing decision, Cancel (`POST /v1/jobs/{id}/cancel`, Idempotency-Key, confirmed).
- Pure helpers on `OG.jobs` (state tones / strip / terminal rule, preset, validate, views, blocker actions, checks,
  metricPoints, logPage, summary, usd): `node tests/test_web_jobs.js`.

Beginner layer: `/jobs/new?preset=mnist&sandbox=1` is the guided free-simulation path (the full form sits under
"Advanced details"; `&advanced=1` opens the classic view). `/jobs/:id` leads with a stage strip + plain status box from
`OG.jobs.stages(job)` (8 stages, 4 for the sandbox, derived from the real state + event history) and
`OG.jobs.plainStatus(job, dep, artifacts, {admin})`; everything technical is under "Advanced details" (`?adv=1`).

## Find Compute (`pages/find.js`, `/find`)

Builds a WorkloadRequirements body (docs/OPENGRID2_CONTRACTS.md §1) from a guided form or pasted `opengrid analyze . --json`
output, POSTs `/v1/recommend` (fallback `/v1/recommend/estimate`) and renders the Recommendation; it never estimates memory or
ranks GPUs itself. State is in the query string (`task model method prec quant ctx conc seq mbs budget region maxp`,
`a.<field>` answers, `mode=paste&req=<json>`, `view=table`, `ask=1`). Pure helpers on `OG.find` (requirementsFromForm,
validatePaste, checkRequirements, pickCards, readiness, memoryFit, runtimeInfo, …); views `OG.views.startHere(active)` and
`OG.views.recommendation(rec, opts)`. Optional assistant hook: `OG.views.assistantPanel(el, {ctx, requirements(),
onRequirements(req), onRecommendation(rec)})` renders in a toggleable panel when defined. Tests: `node tests/test_web_find.js`
(fixtures in `tests/fixtures_web/`).

## Charts (`OG.charts`, pure SVG; each returns `{el, update(partialOpts), destroy()}`)

- `timeseries(el, {times: [iso], series: [{key, label, values, color, step = true, width, dash, opacity, halo, strong}],
  band: {lo: [], hi: [], label, color, step}, events: [{t, label, kind, severity: info|notable|major, detail, href, color}],
  yFmt, height = 260, hatchBefore: iso, yZero, endLabels = true, legend = true, onHover(i|null), emptyText, label,
  gapReason(i) → text})` + `.highlight(key | null)`. Crosshair + tooltip, event markers on the time axis with hover cards
  (`onClick(event)` or `href`), right-edge labels; a value with null on both sides is drawn as a dot.
- `sparkline(values, {dir, width = 96, height = 22, color})` → `<svg>` element (not a container).
- `bars(el, {items: [{label, value, color, href, title, reason, short}], fmt, horizontal = true, height})`.
- `histogram(el, {values, bins = 20, fmt, marks: [{value, label}], height, color})`.
- `heatmap(el, {rows: [label], cols: [label], values: [[v | null]], scale: "sequential" | "diverging",
  domain: [lo, hi] | [lo, mid, hi], fmt, cellTitle(r, c, v), onCell(r, c, v), rowHref(r), colHref(c), nullText,
  labelWidth | labelMax = 260, units, invert})` — null cells hatched, legend (with `units`), hover outline; `invert` gives
  low values the strong colour on a sequential scale (cheap prices stand out).
- `dotplot(el, {rows: [{label, href, points: [{key, value, color, label, href}], low, median, high}], fmt, axisMin, axisMax})`.
- `dotplot` truncates row labels in narrow columns (full text on hover) and drops colliding axis labels.
- Pure (tested): `scale`, `extent`, `padExtent`, `timeTicks`, `bins`, `lerpColor`, `colorSeq`, `colorDiv`, `bandPath`, `nearest`,
  `isolated`, `thinLabels`, `fitLabel`.
- Provider colours: `OG.Lib.providerColor(p)` — fixed per provider, never by rank.

## Navigation (information architecture)

Five primary destinations in `index.html`, each a `.nav-sec` group whose secondary links stay in the DOM
(the command bar's page suggestions scrape every `.nav a[data-nav]`, with `data-alias` as extra search text):

| Primary | Secondary |
|---|---|
| GPUs `/gpus` | Compare, Explorer, Heatmaps, Providers |
| Find Compute `/find` | (page built separately; script tag placeholder in index.html) |
| My Jobs `/jobs` | Deployments, Reserve, Watchlists, Route |
| Markets `/markets` (the market dashboard, pages/overview.js) | Indices, Opportunities, Events, News |
| Developers `/developers` | API docs, API keys, Onboarding |

Utility links (Methodology, Ops) sit below; the Admin group is shown only to admins. A group opens when its
page is current (`OG.navKeyFor(path)` or the page's `nav`; every data-nav must be unique) or when its chevron
is clicked. Under 820px the nav is a menu behind `#nav-toggle` and closes on navigation. `/` is the homepage
(pages/home.js, nav key `home`: no nav link, the brand is home). Tests: `node tests/test_web_nav.js`.

## Commands (terminal mode)

`/` focuses the command bar, `?` shows help, `Esc` closes. Built in: `g <gpu>` (`g h100` → the H100 family page when it has
several variants, `g h100 sxm` → the SXM5 variant; `OG.familyTarget` is the pure rule), `p <provider>`, `c <gpu> <gpu>`,
`r <gpu> [count] [region]` (→ `/route?gpu=&count=&region=`), `e`, `n`, `o`, `x`, `i`, `h`, `m`, `d`, `?`.
Plain text searches GPUs, providers and pages. Add your own:

```js
OG.command(/^w\s+(.+)$/i, async (match, text) => OG.go("/watchlists?add=" + match[1]), "w <gpu>          add to watchlist");
```

## Tests

`node tests/test_web_core.js` (router, qs, fmt, sort, CSV, commands, fuzzy, API client, chart math),
`node tests/test_lib.js`, `node tests/test_page.js`, `.venv/Scripts/python tests/test_pages.py`.
