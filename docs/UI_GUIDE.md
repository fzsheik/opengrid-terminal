# OpenGrid web UI: the complete page-by-page guide

This document describes every page of the OpenGrid website as it is built today (production commit `096a526`,
2026-10-15), so a reviewer who has never seen it can understand it and suggest improvements. It was written by
reading the page source code, not from screenshots. Each page lists its layout top to bottom, every button and
link with its exact label, its states (loading, empty, error, signed out), and an **Observations** list of things
that look confusing, inconsistent, cluttered or unfinished.

## What OpenGrid is (context for the reviewer)

OpenGrid watches about 32 GPU cloud providers, turns their prices into one market (GPU pages, indices, events,
news), recommends which GPU fits a workload ("Find Compute"), and can run a training job on the best provider
through one API key, with every spend approved by a human and every dollar reconciled. AI assistants (Claude
Code, Cursor, VS Code, Codex, claude.ai) can use it over MCP.

**Who uses it**
- **Visitors** (anonymous): public pages are on, so they can browse every market page, Find Compute, Developers,
  the API docs and the Assistant without signing in.
- **The operator** (the founder): signs in at `/login` with the site password; sees everything including Admin.
- **Customers / developers**: use an API key (`opg_...`) from code, the CLI or an AI assistant. There is no
  self-serve sign-up and no payments yet: the operator creates keys.

**Honest current state**
- Real GPU execution is built and tested but has never run on a real provider; every provider adapter is still
  "simulated" and production is in preview-only mode. The only job anyone can run today is the free simulated
  MNIST demo (a tiny practice training job). Real runs need one supervised Lambda validation run first.
- Running your own code is not supported yet (only the built-in MNIST template).
- The Assistant runs without an AI model in production: it matches common phrasings, it does not truly
  understand free text.

**Known open questions for the reviewer** (deliberately not changed yet)
- `/openapi.json` (the API page's endpoint list) and `/mapping` (the provider page's "Source field map") are private
  by an earlier security decision, so those parts of `/api` and `/provider/{name}` fail for visitors.
- The browser can sign in only as the operator (site password); customers have no web login, so customer-only
  views (for example "Waiting for OpenGrid approval") are not reachable in the web UI.

## How this guide is organised

1. Part 1: the global shell (navigation, command bar, theme) and the information pages.
2. Part 2: the market-data pages.
3. Part 3: the customer journey: Find Compute, My Jobs, New job, Job page, API keys, Watchlists, and the
   Assistant panel.
4. Part 4: execution and operator pages: Route, Reserve, Deployments, Onboarding, Admin.


---

# Part 1: Global shell and information pages

Source read: web/index.html, web/core.js, web/app.css, web/pages/home.js, overview.js, market.js, developers.js, api.js, methodology.js, assistant.js, api/pages.py, main.py (auth gate). Everything below describes what the code does, not what it was meant to do.

Access model in production (PUBLIC_PAGES on): every page URL loads for anonymous visitors, and so do the read-only data APIs (gpus, markets, indices, events, news, overview, tape, methodology, recommend, assistant and a few more). Anything under jobs, keys, deployments, route, watchlists, reservations, admin, ops, me, usage and billing needs the operator site login (HTTP basic auth, reached through /login?next=...) or an API key. So a private page still renders its shell for an anonymous visitor, but its data calls fail with 401 and show the shared "Sign-in required" state.

### Global shell (every page)

**Who / sign-in:** everyone. Same frame on every URL.

**Purpose:** a dense, terminal-style frame: a thin top status bar, a narrow left nav, and a scrolling main area where the current page mounts.

#### Overall visual style (app.css)
- Self-described "Dense, data-first, dark by default with a light theme". Rules stated in the file: thin 1px rules, no gradients, no shadows, tabular mono numerals, small-caps labels.
- Base font 13px system sans (Inter / SF); numbers, prices, the brand, badges, the status bar, the tape and the command bar use a monospace font with tabular numerals. Most UI text is 11 to 12.5px; page H1 is 17px (homepage hero H1 is 26px).
- Dark palette: near-black background (#090b0e), slightly lighter surfaces, grey hairlines, light grey text, muted greys for secondary text. Accent blue #4d94ff. Green for up, red for down, amber for warnings. Light theme swaps to a pale grey background, white surfaces, darker blue accent.
- Controls are small: inputs and buttons 24px tall, "sm" buttons 20px, 3px corner radius. Badges are tiny uppercase mono tags (9.5px) with a coloured border.
- Data kinds have fixed badge colours: observed = blue, estimated = amber, transaction = green (inferred = purple but never rendered).
- Layout: top bar 34px tall; left nav 156px wide (128px under 1080px); page content max width 1680px with 12 to 18px padding. Homepage and Developers cap at 1320px. The body itself does not scroll: only the main area scrolls (on desktop).
- Responsive: under 1280px the command box narrows; under 1080px the market tape disappears and the nav narrows; under 820px the nav becomes a hamburger menu (a grid of all groups, all expanded, closing on navigation and on Esc), the "N providers" status item hides and the page scrolls normally; under 560px the whole status block hides and the search box fills the bar.
- Theme: an inline script in index.html applies the saved theme (localStorage "og-theme") or the OS preference before first paint, so there is no flash.

#### Top bar, left to right
1. Hamburger button (only under 820px), aria label "Open menu" / "Close menu".
2. Brand: grid icon plus "OPENGRID" in spaced mono capitals, links to "/". This is the only link to the homepage (there is no "Home" nav item).
3. Status block (mono, 11px):
   - A coloured dot plus a word: "connecting" (grey, initial), "live" (green), "stale" (red, when the last data timestamp is over 10 minutes old), or an error word such as "api unreachable" (red).
   - "N providers" (dim; tooltip "N providers polled, fastest every Xm", plus "(poller not running on this server)" when relevant). Shows "-" until loaded.
   - "AS OF hh:mm:ss" (the data timestamp; tooltip with the full date). "-" until loaded.
4. Market tape: a single non-scrolling row (overflow hidden, no animation) of up to 16 GPU price items ("H100 $2.49 ▲ 1.2%": short GPU name, lowest on-demand price, change), then up to 12 recent move items from /v1/tape ("Lambda H100 ▼ 3.0%" or "Market ..."). Each item links to its GPU page (or /events). On /markets the page replaces the tape with its own version (14 prices plus up to 8 event headlines with a severity dot).
5. Command bar: a text input, placeholder "Search or command  ( / )". Suggestion dropdown below (described below).
6. Theme toggle: an icon-only button (sun in dark mode, moon in light mode), tooltip "Switch to light mode" / "Switch to dark mode".

There is no sign-in button, account indicator or user menu anywhere in the top bar.

#### Left navigation (exact labels, in order)
Five primary groups, each a bold link; groups with sub-pages have a small chevron toggle on the right. A group's sub-links show only when the group is open: it opens automatically when one of its pages is current, or when the user clicks the chevron (then it stays pinned open). Other unpinned groups close on navigation. The current link gets a blue left border and a lighter background.

1. "GPUs" (/gpus); sub: "Compare", "Explorer", "Heatmaps", "Providers"
2. "Find Compute" (/find); no sub-links
3. "My Jobs" (/jobs); sub: "Deployments", "Reserve", "Watchlists", "Route"
4. "Markets" (/markets); sub: "Indices", "Opportunities", "Events", "News"
5. "Developers" (/developers); sub: "API docs" (/api), "API keys" (/keys), "Onboarding"
6. Utility links (smaller, dim, separated by a rule): "Methodology", "Ops"
7. "Admin" section (hidden unless /v1/me says the visitor is the operator or holds an admin-scoped key): "Execution", "Checklist", "Partners", "Value"
8. Footer hint (desktop only): "/ search" and "? commands" with keycaps.

Highlight rules: "/" highlights nothing (homepage). /gpu/* highlights GPUs, /provider/* highlights Providers, /compare/* Compare, /methodology/* Methodology, /assistant highlights "Find Compute".

#### Command bar and keyboard
- "/" anywhere (when not typing in a field) focuses and selects the command input. "?" opens the commands help. Esc closes help, or clears and leaves the input.
- With the input focused and empty, the dropdown lists every command usage line (hint "COMMAND"). These rows do nothing when clicked or when Enter is pressed: they are reference only.
- While typing, up to 12 suggestions, each with a label and a small uppercase hint on the right:
  - If the text is a full command: first row "↵ <text>" with the command description as hint.
  - Up to 2 GPU families ("GPU family · N variants"), up to 6 GPUs ("GPU · N providers" or "GPU · none live"), up to 4 providers ("provider"), up to 3 pages from the nav (matched on label and hidden aliases, hint "page"). Fuzzy matching.
- Arrow Up/Down moves the selection, Enter runs it, mouse click runs it. Searches are logged anonymously.
- Commands, in the order the help dialog lists them (page files register theirs first):
  - "j new" new training job (PyTorch MNIST) to /jobs/new
  - "j" jobs
  - "rsv" price-lock requests (reserve)
  - "w <gpu>" add a GPU to a watchlist (/watchlists?add=...)
  - "g <gpu>" open a GPU market (g h100 = family, g h100 sxm = variant); falls back to /gpus?q=...
  - "p <provider>" open a provider (p lambda); falls back to /providers
  - "c <gpu> <gpu>" compare two GPUs (c h100 h200)
  - "r <gpu> [n] [region]" route preview (r h100 8 us)
  - single letters: "e" events, "n" news, "o" opportunities, "x" explorer, "i" indices, "h" heatmaps, "m" methodology, "d" deployments
  - "theme [light|dark]" switch colour theme
  - "?" this help
- Help dialog: dark scrim, box titled "Commands" with "press / to type · Esc to close", the usage lines in a mono block, and a "Close" button. Clicking the scrim closes it.

#### Shared components and states (used by every page)
- Page header: H1 plus a grey one-line subtitle on the left, buttons on the right.
- Loading: small spinner plus text ("Loading…" by default; pages pass their own text).
- Empty: grey line ("Nothing to show." by default).
- Error: bold red title chosen by status: "Not found" (404), "Sign-in required" (401), "Not allowed" (403), else "Could not load"; then the server message; then a "Sign in" button on 401 (goes to /login?next=<this page>, which triggers the browser's basic-auth prompt and comes back) and a "Retry" button when the page supplies one.
- Insufficient data: dashed box, amber bold "Insufficient data" (or a custom title) plus the reason; default reason "not enough observed history to compute this honestly". The API returns null plus a reason instead of a number and the UI shows the reason.
- Inline "n/a": dim mono text with a dotted underline, the reason on hover.
- Change: "▲ 1.2%" green / "▼ 1.2%" red / "-" grey with a reason tooltip when there is not enough history.
- Kind badge: "OBSERVED" / "ESTIMATED" / "TRANSACTION", each a link to /methodology/data-kinds with a one-line tooltip. Price-concept badge: "LIST" / "OBSERVED" / "QUOTE" / "EXECUTION", also linking there.
- Freshness badge: coloured dot plus age ("now", "42s", "5m", "3h", "2d"); green fresh, amber aging, red stale. Thresholds are 2x and 6x the provider's poll interval when known, else 30 minutes and 3 hours. Tooltip explains the basis.
- Stat strip: a row of label / value / sub-line cells. Missing values show "n/a" with a short reason; items marked foldable collapse into one trailing cell "Not enough history yet: 7d · 30d ..." so a strip is never a row of n/a.
- Segmented control and tabs: rows of small toggle buttons.
- Data table: sortable column headers (click or Enter; caret ▲/▼; missing values always sort last), clickable rows (Cmd/Ctrl click opens a new tab), optional title bar with a "CSV" download button, a "Show N more" button after the row limit (default 500). Columns where every cell is empty are hidden and named in a footer "No data yet: Col · Col"; low-priority columns drop when the table is too narrow and are named in "Hidden at this width: ..." (tooltip: the CSV has every column). Overflowing tables scroll sideways with a fade at the clipped edge; some tables keep the last columns sticky.
- Copy button: "Copy" turns into "Copied" (or "Copy failed") for 1.4 seconds.
- Confirmation dialog: modal with title, body, "Cancel" and "Confirm" (labels overridable; red style for dangerous actions). Optional acknowledgement checkbox that must be ticked before Confirm enables. Esc or clicking outside cancels.
- Form dialog ("ask"): same modal with fields (text, textarea, checkbox, select); Confirm stays disabled until required fields are valid, including "type the exact name" checks. Enter submits.
- Drawer: right-hand panel with a "Close ✕" button; Esc closes it.
- Execution mode banner (used on execution pages): "DISABLED", "PREVIEW ONLY", "SUPERVISED", "LIVE" with an explanation, or "EXECUTION MODE UNKNOWN" for non-admins.
- Countdown: live "4m 12s" timer that turns into "expired".
- Stub page: page title plus a "COMING ONLINE" badge and a line of links.
- Unknown URL: header "Not found" with the path as subtitle, and "No page at this address.".

#### Refresh behaviour
- Shell: tape and status refresh every 60 seconds; the provider count loads once. Pages register their own timers through ctx.every, which pause while the browser tab is hidden and stop on navigation.
- Navigation is client-side (links are intercepted; back/forward works). Each page change resets scroll to the top. The server renders a short SEO summary (H1 plus description, or real data for some pages) inside the page container, which the app removes on mount (methodology pages reuse it as their content).
- Anonymous, cookie-less analytics: page views, searches, GPU/provider views, compares (skipped under Do Not Track).

#### Observations
- No visible sign-in or account state anywhere. A visitor learns about sign-in only from a 401 error box on a private page. There is no sign-out.
- (Fixed 2026-10-15, no longer an issue: anonymous background calls now get a Bearer 401, so no password popup.) On every page load the shell calls /v1/me (to decide whether to show Admin). That endpoint is private, and the server used to answer anonymous requests to private paths with a 401 carrying a basic-auth challenge; worth checking whether browsers pop a password dialog for anonymous visitors.
- The nav mixes public and private destinations with no marker: "My Jobs", "Deployments", "Reserve", "Watchlists", "Route", "API keys", "Onboarding" and "Ops" all need sign-in, but look the same as public pages.
- "My Jobs" groups Deployments, Reserve, Watchlists and Route, which are not really jobs. Watchlists (market alerts) sits under jobs, not under Markets or GPUs.
- The Assistant page (/assistant) has no nav item and no link anywhere in the web code; it is only reachable by URL (it highlights "Find Compute" when open). The command bar cannot find it either, because page search only scrapes the nav.
- No "Home" in the nav; only the small brand logo returns to "/".
- Commands exist for events, news, deployments, etc., but not for Markets, Find Compute, Developers or the homepage. Command letters are opaque ("x" explorer, "o" opportunities, "rsv").
- Empty-input command list looks clickable but does nothing.
- The tape is a static row clipped at the right edge (no scrolling or marquee), so most items are never visible on normal widths. It has two different formats (shell version vs /markets version) and both timers write to it.
- "connecting" / "live" / "stale" status and "AS OF" use technical, terminal-style wording; "AS OF" shows a time without a date or timezone.
- Very small type throughout (9.5 to 12px for most labels, badges and buttons); 20 to 24px tall controls.
- app.css carries leftover rules for older layouts (for example .dv-choices, .dv-ch, .dv-sec, .dv-cols for a previous Developers design that the current code no longer renders) and dozens of near-duplicate colour tokens (blue-bg to blue-bg6, red-t to red-t4).

### Homepage

**URL:** /

**Who / sign-in:** public. First-time visitors. Browser title "Find and run GPU compute · OpenGrid".

**Purpose:** say what OpenGrid is, offer three entry actions, and show a compact live price board.

#### Layout, top to bottom
1. **Hero** (two columns on desktop, stacked under 1080px):
   - Left: eyebrow "OpenGrid · GPU compute market"; H1 "The smartest way to find and run GPU compute."; line "Compare GPU prices across N cloud providers, find compute suited to your workload, and manage execution through one platform." (N fills in from the poll schedule; until then it reads "cloud providers"); then a small bordered note: "Prices are observed from provider price lists and APIs, labelled with their age. Real launches are supervised: each one waits for OpenGrid approval. The training demo is a free simulation: no GPU, no spend."
   - Right: three stacked action cards, each a full link with title, arrow and a one-line description:
     - "Explore GPUs →" (primary, blue tint) "Live prices, availability and history for every GPU across providers." to /gpus
     - "Find Compute →" "Describe your workload; get GPUs that fit it, ranked by price and fit." to /find
     - "Try a Training Demo →" "Train PyTorch MNIST end to end in the sandbox." with an amber badge "SIMULATED · FREE", to /jobs/new?preset=mnist&sandbox=1
2. **Stat strip** (5 cells): "GPUs priced" (sub "on-demand, in stock or stock unknown"), "Listings available" (with OBSERVED badge), "Cheapest GPU-hour" (sub "H100 at Lambda" style), "Sold out everywhere" (sub "GPUs with listings, none purchasable"), "Providers polled" (sub "public prices and availability"). Missing values show n/a with reasons such as "no GPU is priced right now", "capacity unavailable", "the poll schedule (/polling) did not answer".
3. **Market row** (two columns: board plus a 320px side column; stacked under 1080px):
   - **Board:** header "Live market · most-covered GPUs" plus OBSERVED badge plus "as of hh:mm:ss", and right-aligned links "All GPUs →" (/gpus) and "Market dashboard →" (/markets). Table of the 10 GPUs with the most providers (ties: cheapest first), compact, no CSV. Columns: "GPU" (bold short name, link), "Low $/GPU·h", "Cheapest at" (provider logo plus name, link), "Prov", "24h" (matched median change; n/a reason "needs >= 2 providers priced 24h ago and now (N matched)"), "24h low" (sparkline). Default sort: Prov descending. Rows link to the GPU page. Empty text "No GPU is priced right now."
   - **Side: "Price indices"** with link "All →" (/indices). Up to 4 rows (GPU Compute, H100 SXM5, H200 SXM5, A100 SXM4 indices): name, level ("$x.xx/GPU·h" or "pts", or "not published"), 24h change. Each links to the index page. Note below: "On-demand list prices per GPU-hour, not quotes. Data kinds · Index methodology" (two links).
4. **"How OpenGrid works":** three numbered columns:
   - "01 Observe N providers" + OBSERVED badge; text about polling and n/a with reasons; link "Providers and coverage →"
   - "02 Recommend for your workload" + ESTIMATED badge; text about memory estimates; link "Find Compute →"
   - "03 Run safely" + amber "SUPERVISED" badge; text about free previews, admin approval, cost ceiling, "Today the runnable workload is the MNIST training template."; link "Execution safety →" (/methodology/execution-safety)
5. **"Also" link row:** "Developers: CLI · MCP · API", "Compare GPUs", "Reserve capacity", "Watchlists & alerts", "Market events", "Methodology".

#### States
- Each block loads on its own: "Loading live market…", "Loading prices…", "Loading indices…".
- Overview request fails: board area shows the error box with "Retry"; the stat strip empties; the top bar status turns to "api unreachable".
- Board marked unavailable by the API: insufficient box titled "Market board unavailable" with the reason.
- Indices service down: "The index service did not answer (/v1/indices)."; no data: "No benchmark index has data yet." or "No index has data yet."
- Sparklines missing: "-".

#### Refresh
Overview, sparklines and indices reload every 60 seconds (paused while the tab is hidden).

#### Observations
- "Try a Training Demo" (the third main call to action) and "Reserve capacity" and "Watchlists & alerts" lead to pages that need sign-in; anonymous visitors will hit "Sign-in required". The hero text says the demo is free but not that it needs an account.
- The headline "The smartest way to find and run GPU compute." is a marketing claim, while the rest of the page is careful, caveat-heavy copy; the small grey "honest" paragraph is dense.
- Board column labels are terse ("Prov", "Low $/GPU·h", "24h low") and rely on tooltips.
- "24h" (change) and "24h low" (sparkline) side by side can be confused.
- Index names are shortened by stripping "OpenGrid" and "Index", so rows can read like GPU names ("H100 SXM5"), easy to mistake for the board.
- The stat strip appears before the board with no heading.
- Error text exposes internal endpoints ("/v1/indices", "/polling").
- "Market dashboard →" points to the same place as the "Markets" nav item, but uses a different name.

### Markets

**URL:** /markets (registered in web/pages/overview.js; web/pages/market.js holds a different shared board used by /gpus, not this page)

**Who / sign-in:** public. Traders/analysts who want the whole market at once. Browser title "Markets · OpenGrid"; nav "Markets" highlighted.

**Purpose:** a single-screen market dashboard: indices, a 24h summary, eleven "movers" panels, a full board, notable events and news headlines.

#### Layout, top to bottom
1. **Header:** H1 "Market overview"; subtitle "On-demand GPU market · observed list prices per GPU-hour"; on the right a mono line "hour <date time> · Nh recorded since <date time>" (or "no market history") and a button "Methodology" (to /methodology/events#overview).
2. **Indices strip:** up to 9 tiles (GPU Compute, H100 SXM5, H200 SXM5, B200, A100 SXM4, L40S, H100 class, H200 class, A100 class), each: short name, level with unit ("/GPU·h" or "pts"), and "24h" change plus "7d" change when available; unpublished ones show "-" and "not published" (dimmed). A final tile "All N indices →". Each tile links to its index page; tooltip has full name, reason and constituent count.
3. **Summary line:** segments separated by "│":
   - "24h N up · N down · N flat of N GPUs · median move ▲x%" (or "Direction no GPU has >= 2 providers priced at both times", or "24h direction unavailable: reason")
   - "Markets N live · N sold out · N GPUs sold out everywhere"
   - "Listings N available (+N 24h)" or "(24h n/a)"
   - "24h N sell-outs · N restocks"
4. **Panel grid** (4 columns, 3 under 1280px, 2 under 1080px). Each panel: title (some are links), optional kind badge, optional "?" with a rule tooltip, and a mini table of up to 6 rows:
   - "Top gainers · 24h": GPU, Median, 24h, Prov (empty: "no GPU's matched median rose in 24h")
   - "Top losers · 24h": same columns (empty: "no GPU's matched median fell in 24h")
   - "Unusual moves": GPU, 24h, z, n (empty: "no 24h move beyond |z| >= 2")
   - "Most volatile · 7d": GPU, σ 1h, 7d range
   - "Widest spreads" (title links to /heatmaps): GPU, Low, High, Spread, Prov (empty: "no GPU has 2+ priced providers")
   - "Most liquid / covered": GPU, Priced, Avail, Listings (available/live)
   - "Availability gaining · 24h": GPU, Prov (then→now), Δ, Listings (then→now), Δ
   - "Availability losing · 24h": same
   - "Newly available · 7d": GPU, First at, Since, Low
   - "Sold out everywhere": GPU, Providers, Last low ("never" if never priced)
   - "Recent provider price changes" (double width, up to 10 rows): Time, GPU, Provider, Was, Now, Chg
5. **Lower row** (board plus a 420px side column; stacked under 1080px):
   - **Board** titled "Market board · on-demand" with a "CSV" button (file opengrid-board.csv). Columns: "GPU", "Prov", "Low", "Cheapest at", "Median", "High", "Spread", "24h", "24h low" (sparkline). Default sort Prov descending, 24 rows, then "Show N more". Rows link to GPU pages.
   - **Side:** panel "Latest notable events" (title links to /events?sev=notable; "all →" to /events): up to 10 compact event rows (time or date, severity mark, type tag, "Provider · GPU", numbers; full title on hover). Panel "Headlines" (title and "all →" go to /news): up to 8 news items (age, headline opening in a new tab, source "+N").

#### States
- Initial: panels show "…", indices "loading indices…", summary "loading…".
- Overview request fails: every panel shows "unavailable overview request failed: <message>"; the summary line becomes the error box with "Retry"; status bar "api unreachable".
- Section marked unavailable by the API: "unavailable <reason>" in the panel.
- Empty sections: the specific grey sentences above, or "none right now".
- Spreads, events, news, indices each fail independently ("events request failed: ...", "news service did not answer (/v1/news)", "index service did not answer (/v1/indices)", "no benchmark index has data yet", "no index has data yet: the index job has not produced levels"). No events: "No notable or major events recorded yet."; no news: "No headlines ingested yet."

#### Refresh
All six loaders (overview, indices, spreads, events, news, sparklines) rerun every 60 seconds.

#### Observations
- Page naming is inconsistent: nav and browser title say "Markets", the H1 says "Market overview", the homepage link says "Market dashboard".
- Very dense: 9 index tiles, a 4-part summary line, 11 panels, a 24-row board and two side feeds on one page, mostly 11 to 12px text with jargon headers ("σ 1h", "z", "n", "Δ", "Prov", "Avail").
- "Widest spreads" title links to Heatmaps, which is not obvious from its label; other panel titles are plain text, so some titles are links and some are not.
- The board duplicates the homepage board and the /gpus board with slightly different columns and labels ("Low" here, "Low $/GPU·h" on home, "Lowest $/GPU·h" on /gpus).
- Error messages are developer-facing ("overview request failed: HTTP 500").
- The "?" rule markers are tooltip-only and tiny.
- Header right side shows raw recording metadata ("hour ... · 412h recorded since ...") rather than a plain "Updated at".

### Developers

**URL:** /developers

**Who / sign-in:** public page; developers and AI-assistant users. Creating the key it asks for needs sign-in. Browser title "Developers · OpenGrid".

**Purpose:** connect OpenGrid to an AI assistant (MCP) in three steps; REST and local options folded below. Static page: no API calls, nothing refreshes.

#### Layout, top to bottom
1. **Header:** H1 "Connect your AI assistant"; subtitle "Ask Claude, Cursor, VS Code or Codex about GPU prices, what your model needs, and your jobs. Three steps, nothing to install."; button "API reference" (to /api).
2. **Step 1 "Get an API key"** (numbered circle): "Create a key for your assistant. It can read prices and your jobs; it cannot rent a GPU."; big primary button "Create an assistant key →" (to /keys?preset=assistant); text about saving the key in ~/.zshrc or ~/.bashrc; code block labelled "~/.zshrc" with `export OPENGRID_API_KEY=<paste your key>` and a "Copy" button.
3. **Step 2 "Add OpenGrid to your app"**: segmented tabs "Claude Code" (default), "Cursor", "VS Code", "Codex", "claude.ai / Desktop", "Terminal". The panel below swaps per tab:
   - Claude Code: "Run this once in a terminal:" plus a `claude mcp add ... --header 'Authorization: Bearer ${OPENGRID_API_KEY}'` code block with "Copy"; hint to check with `claude mcp list`.
   - Cursor: primary button "Add to Cursor" (a cursor:// deep link) "or paste this into ~/.cursor/mcp.json:" plus JSON block; note to restart Cursor.
   - VS Code: .vscode/mcp.json JSON (prompts for the key once).
   - Codex: `codex mcp add opengrid --url ... --bearer-token-env-var OPENGRID_API_KEY`.
   - claude.ai / Desktop: numbered list: open "Settings → Connectors → Add custom connector"; URL `<site>/mcp` with its own "Copy"; add header Authorization "Bearer <your key>". Note that calls come from Anthropic's servers.
   - Terminal: "No assistant? Use OpenGrid straight from your terminal. Needs uv and git." (uv links to docs.astral.sh in a new tab); a terminal block (export URL and key, `uvx --from "git+..." opengrid gpu search --model H100`) and an "optional: install it" block.
4. **Step 3 "Ask"**: "Restart your app, then try:"; three quoted example prompts each with "Copy" ("Find the cheapest H100 on OpenGrid", "Which GPU should I use to fine-tune Llama 3 8B? Use OpenGrid", "How are my OpenGrid jobs doing?"); a bullet list of capabilities ("Find GPU prices", "Size your workload", "Run the free demo", "Check your jobs", each with a grey description); grey note "Every number comes from OpenGrid's live data. Your assistant can never rent a paid GPU: real runs are started and approved on this website."
5. **Four collapsible sections** (closed by default):
   - "Analyze a project on your computer (local server: reads your project folder ...)": explanation, three code blocks (Claude Code, Cursor/JSON, Codex CLI) for a local stdio MCP server, and a privacy note.
   - "Use it from your own code (REST API, JSON over HTTPS)": two curl blocks ("GET /v1/gpus · every GPU with its current market", "POST /v1/recommend · GPUs that fit a workload"); "Responses are {data, meta}. Full API reference · openapi.json".
   - "All tools (what your assistant can call)": table "Tool" / "What it does" / "Key permission" with 13 MCP tools (permission "route:execute" shown in warning colour, "none" dimmed).
   - "Troubleshooting": Q and A list: tools not listed, "401 Unauthorized", "403 Forbidden" (with "Create a key" link), "uvx: command not found, or a git error", "ChatGPT" (not supported: needs OAuth), "429 Too Many Requests".

#### States
None (static). Snippets use the server's base URL.

#### Observations
- Nav and title say "Developers", H1 says "Connect your AI assistant": the page is really an MCP setup guide; CLI and REST are hidden in collapsibles.
- Subtitle says "Three steps, nothing to install", but the Terminal tab and the local server need uv and git.
- "Create an assistant key →" goes to /keys, which needs the operator sign-in; there is no self-serve sign-up, so an outside developer cannot complete step 1. The page does not say this.
- "live prices across 32 clouds" is hard-coded text, while other pages compute the provider count live.
- The env variable is OPENGRID_API_KEY here, but the /api page's examples use $OPENGRID_KEY.
- The "Copy" button text is identical everywhere; code block labels are tiny grey mono.
- The CSS still contains rules for an older version of this page that is no longer rendered.

### API docs

**URL:** /api

**Who / sign-in:** public page; integrators. Browser title "API docs · OpenGrid"; nav item "API docs" under Developers.

**Purpose:** reference for the REST API: auth, envelope, data kinds, rate limits, an example, and a filterable endpoint list generated from /openapi.json with a "Try it" for GET endpoints.

#### Layout, top to bottom
1. **Header:** H1 "API"; subtitle "One OpenGrid API key for market data, indices, routing, deployments and alerts · JSON over HTTPS"; buttons "Interactive docs ↗" (/docs), "openapi.json" (/openapi.json), primary "Get a key →" (/keys).
2. **Two-column body:** a 190px left table of contents (sticky box) and the main column (max 1180px). Stacked under 1000px.
   - **TOC** (appears only after the schema loads): "GUIDE" with "Auth & scopes", "Envelope", "Data kinds", "Rate limits", "Example"; then "ENDPOINTS · N" with one link per group plus its count.
   - **"Authentication & scopes"**: code `Authorization: Bearer opg_live_…`; note "Every /v1 endpoint needs a key (or the site's own session, which acts as the operator). Create and revoke keys on API keys. ..."; status codes 401 / 403 / 429; a table of 9 scopes with descriptions (route:execute in warning colour: "provision compute through OpenGrid (spends money)").
   - **"Response envelope"**: example JSON {data, meta} and notes on meta.as_of, meta.kind, meta.methodology, limit/offset paging, null plus reason, error shape.
   - **"Data kinds & price concepts"**: two small tables: kind badges (observed, estimated, transaction) and price-concept badges (list, observed, quote, execution); link "Data kinds methodology →".
   - **"Rate limits"**: paragraph on read / write / execute classes, token bucket, X-RateLimit headers, Retry-After, GET /v1/me.
   - **"Example: route preview"**: quoted scenario "I need 8 H100s in the US under $2.50/hr for a day."; curl block with "Copy"; link "Run this preview in the terminal →" (to /route with the same parameters); note on POST /v1/route.
   - **Endpoints:** while loading "Reading /openapi.json…". Then a bar: "Endpoints" heading, input "Filter endpoints" (filters on path and summary, kept in ?q=), and a grey line "N operations in <title> <version> · GET endpoints have Try it". Then one section per group (Market data, Indices, Events, News, Routing, Deployments, Accounts, Billing, Watchlists / Alerts, Ops & reference, Other) with a count. Each operation is a collapsible row: coloured method tag, mono path, summary, and a scope badge on the right (route:execute amber, admin red).
   - **Expanded operation:** description; parameter table (name with red * if required, location, type, default) or "No parameters."; "JSON body" sketch with * for required fields; for GET: a "Try it" box ("Try it · runs in this browser as the signed-in operator; your key is not needed here"), one input per parameter (path params pre-filled with samples like h100-80gb-sxm5, lambda, h100-class), a primary "Send" button and the live "GET <url>" preview. The result shows an "HTTP 200" badge (green or red), "N ms · N bytes", a "Copy" button (copies an equivalent curl), and the pretty-printed JSON (first 60k characters). For non-GET: "Try it is offered for GET endpoints only; use curl with your key for writes."
   - No match for the filter: "No endpoint matches."

#### States
- Schema load fails: error box in place of the endpoint list (no Retry button); the TOC box stays empty.
- Try it with an empty path parameter: error "fill every path parameter"; network failure: "network error".
- Opening /api#anchor scrolls to that section after the schema loads.

#### Refresh
None.

#### Observations
- /openapi.json and /docs are not on the server's public list, so for anonymous visitors in production the endpoint list (the bulk of the page) and the "Interactive docs ↗" / "openapi.json" buttons probably fail with 401 or a browser password prompt.
- Copy contradicts public mode: "Every /v1 endpoint needs a key" (many data endpoints are public now) and "Try it runs ... as the signed-in operator" (anonymous visitors are not signed in).
- Naming: nav "API docs", browser title "API docs", H1 "API"; Developers page calls it "API reference".
- "Run this preview in the terminal →" goes to /route, which needs sign-in; "terminal" here means this web app, while Developers uses "terminal" for a shell.
- Env variable $OPENGRID_KEY differs from OPENGRID_API_KEY on Developers.
- Two documentation surfaces (this page and the external /docs) with no explanation of the difference.
- The left TOC is an empty bordered box until the schema arrives.

### Methodology index

**URL:** /methodology

**Who / sign-in:** public. Anyone checking how numbers are computed. Browser title "Methodology · OpenGrid"; nav "Methodology" (small utility link).

**Purpose:** list every methodology document, grouped by topic, with one-line descriptions.

#### Layout, top to bottom
1. **Header:** H1 "Methodology"; subtitle "How OpenGrid computes every number it publishes. Every endpoint cites its document in meta.methodology."; buttons "JSON" (opens /v1/methodology in a new tab) and "API docs" (/api).
2. **Grid of groups** (auto-fill columns, min 380px): each group has a heading with a document count, then a list of document rows. Each row is a link: title (bold), mono slug on the right (for example "data-kinds"), and a grey description line underneath (server summary, else the first paragraph of the markdown, fetched lazily; "…" while loading). Groups in order: "Market data & indices", "Market structure", "Events & news", "Data trust & quality", "Routing & execution", "Accounts & billing", then "Other" for anything not mapped.
3. **Footer note:** "N documents. Data kinds (observed / derived / estimated / transaction) and the four price concepts are defined in data-kinds; every figure on the site carries one of those labels."

#### States
"Loading documents…"; on failure it falls back to the server-rendered plain list if present, else the error box.

#### Refresh
None.

#### Observations
- Of the 30 documents, 14 land in "Other" (economics, execution-safety, first-live-route, jobs, provider-capabilities, recommendations, reconciliation, reliability, reservations, routing-quality, runner, security, settlement, workloads), so "Other" is likely the biggest group, including "execution-safety", which the homepage links to as a key trust document.
- The group list names a document "accounts" that does not exist.
- The footer lists a "derived" data kind that no badge in the UI ever shows; the UI shows observed, estimated, transaction (inferred is hidden).
- The "JSON" button is a developer affordance in the page header of a reader-facing page.
- Methodology is a small dim utility link at the bottom of the nav, despite the product's emphasis on transparency.

### Methodology document

**URL:** /methodology/{name} (for example /methodology/data-kinds)

**Who / sign-in:** public. Browser title "<document H1> · Methodology · OpenGrid".

**Purpose:** read one methodology document (markdown rendered on the server).

#### Layout, top to bottom
1. **Breadcrumb bar:** "Methodology" (link) / mono slug; on the right the group name in small uppercase and a small button "raw markdown" (opens /v1/methodology/{name} in a new tab).
2. **Two columns** (stacked under 1080px):
   - Left (210px, sticky): "ON THIS PAGE" and a list of H2 and H3 headings (H3 indented); the heading in view is highlighted with a blue left border as you scroll; clicking scrolls and updates the #anchor; footer link "← all documents". If the document has fewer than two headings: "single section".
   - Right (max 880px): the article. H2 / H3 / H4 get a "#" anchor link that appears on hover. Tables are wrapped in their own horizontally scrolling bordered box with uppercase mono headers. Inline code that looks like an endpoint ("GET ...", "/v1/...") is tinted blue. The server-rendered body ends with a note "All methodology · raw markdown".

#### States
Loading spinner; error box (404 shows "Not found" for an unknown name); "No document." if empty. On first load the content comes from the server-rendered HTML; on in-app navigation the page fetches that HTML again.

#### Refresh
None.

#### Observations
- "raw markdown" appears twice (button in the breadcrumb bar and link at the end of the article), as does the way back ("Methodology" breadcrumb, "← all documents", "All methodology").
- The code comment promises a copy affordance for endpoint code; in practice it only changes the colour.
- Group label shows "OTHER" for nearly half the documents.

### Assistant

**URL:** /assistant (optional ?q=<question> asks a first question on load)

**Who / sign-in:** public (POST /v1/assistant is on the public list and read-only). Its action cards (start the demo, cancel a job) need sign-in. Browser title "Assistant · OpenGrid"; nav highlights "Find Compute".

**Purpose:** plain-English chat over OpenGrid's live market data that answers with structured cards (prices, recommendations, comparisons, price moves, jobs).

#### Layout, top to bottom
1. **Page header:** H1 "Assistant"; subtitle "Ask about live GPU prices, which GPU fits a training, fine-tuning or inference job, comparisons and price moves".
2. **Assistant panel** (bordered box, max 980px), which is the same reusable panel that /find and /jobs embed:
   - Head row: bold "OpenGrid Assistant", grey note "Answers use OpenGrid's live market data; estimates are labelled. It cannot launch or buy anything.", and a small "New" button (tooltip "Start a new conversation": clears the log and context, shows the examples again).
   - Conversation log (scrolls, up to 62% of the viewport height, keeps the last 60 messages). User messages are right-aligned grey bubbles; replies are full-width bordered boxes.
   - Example chips (hidden after the first question until "New"): "Cheapest available H100", "What GPU should I use to fine-tune Llama 3 8B with LoRA?", "Affordable GPU for inference of a 7B model", "Compare H100 SXM and H200", "Why did H200 prices change?", "Can I fine-tune a 13B model with QLoRA within $50?", "Specs of the L40S". Clicking one asks it.
   - Input: two-line resizable textarea, placeholder "Ask about GPU prices, which GPU fits your job, comparisons…" (max 2000 characters); primary button "Ask". Enter sends, Shift+Enter makes a new line. While waiting, the button is disabled and the reply box shows "Thinking…".

#### A reply contains
1. The reply text paragraph.
2. Zero or more cards, each separated by a thin rule:
   - **Recommendation:** "Memory ≈ N GB · N GPUs · <confidence> confidence" + ESTIMATED badge; three picks: "Cheapest that fits", "Best for this workload", "Launchable through OpenGrid", each with GPU count, GPU link, provider, "$x.xx/GPU-h", kind badge, amber "STALE" badge if not fresh, a grey reason, and small buttons "Market" (GPU page) and "Route preview" (/route...). Missing picks read "none available right now" (or "none for you right now" for the launchable one). Then a compact table of up to 5 options: "GPU", "Provider", "Region", "$/GPU-h", "Fits" (yes/no), "OpenGrid" (launchable / preview only); up to 3 notes; button "Refine in Find Compute →" (opens /find pre-filled with the same requirements). If no GPU is needed: "No GPU needed" plus reason plus ESTIMATED badge.
   - **Prices:** table "GPU", "Provider", "Region", "$/GPU-h" (with kind badge), "Available" (yes / no / not reported); for a GPU family a note "Variants are priced separately."; empty: "No fresh, in-stock listing right now."
   - **Compare / Specs:** table "GPU", "VRAM GB", "TB/s", "BF16 TFLOPS", "FP8 TFLOPS", "Low $/GPU-h" (OBSERVED badge), "Median", "Providers"; optional grey note.
   - **Market context ("why did prices change"):** amber note (default "Correlation, not causation."); per GPU: bullet summaries or a reason, and a table of recent moves "Hour (UTC)", "Move", "%", "From", "To", kind badge; then "News in the same window (untrusted public text; not a cause):" with headline links (new tab) and sources.
   - **Questions (clarifying):** each question with an optional "(why)" and chip buttons with suggested answers; clicking a chip sends that answer.
   - **Jobs / Job:** list of jobs: name or id link (to /jobs/{id}), status text, "· epoch N of M", amber "SIMULATED" badge for sandbox jobs, cost ("· $x.xx" actual or "· up to $x.xx"); "All jobs →" when there are more; empty "No jobs."
   - **Action cards** (only two kinds are accepted, anything else is dropped):
     - Start demo: primary button with the server's label (default "Start free simulation") and an optional grey note. Runs on click with no confirmation; shows "Starting the free simulation…", then "Started. " plus a button "Watch it run →" (to the new job page).
     - Cancel job: red button (default label "Cancel job-…"). Opens a confirmation dialog titled "Cancel job-…?" with the server's text or "Before launch the approval is rejected; after launch the GPU is terminated. This cannot be undone.", buttons "Keep it running" and "Cancel job". Then "Cancelling…", then "Cancel requested. " plus link "Open the job →". Retries reuse the same idempotency key.
     - Either action, on 401/403: "You need to be signed in with permission to run jobs (an API key with route:execute). " plus link "API keys →". Other errors: the standard error box; the button re-enables.
   - Link cards are gathered into one row of small buttons under the reply.
3. A small grey meta line: "deterministic answer" or "phrased by <engine name>", followed by " · grounded in the cards above" or " · check the cards".

#### States
- Too many questions (429): "Too many questions in a short time; wait a minute and try again."
- Too long (422): "That question or conversation is too long. Start a new conversation." (and the context is dropped).
- Other errors: standard error box (no Retry).
- Context is kept in memory only; navigating away loses the conversation.

#### Refresh
None (request and response only).

#### Observations
- Not reachable from the nav, the homepage, or the command bar; only by URL (or embedded on /find and /jobs).
- The panel note says "It cannot launch or buy anything." while the same panel can show "Start free simulation" and red "Cancel job" buttons.
- The sign-in error points to "an API key with route:execute" and "API keys →", but in the web app the operator signs in with the site login, not a key, and the keys page itself needs sign-in. Anonymous visitors who get an action card cannot act on it and are not offered the "Sign in" link that the standard error box has.
- The page H1 "Assistant" sits directly above the panel's own bold title "OpenGrid Assistant": two titles for one thing.
- Example chips disappear after the first question and come back only via "New".
- The meta line exposes implementation details ("deterministic answer", "phrased by <model>", "check the cards").
- Highlighting "Find Compute" in the nav while on /assistant may confuse, since there is no Assistant item.
- Column headers in cards are terse ("TB/s", "BF16 TFLOPS", "Fits", "OpenGrid").

---

# Part 2: Market-data pages

Source read: web/pages/*.js, web/core.js, web/index.html, api/pages.py, main.py. Everything below is what the code does, not what it was meant to do.

### Shared conventions (apply to every page below)

**Access.** With PUBLIC_PAGES on (production), every page shell in this document loads for anonymous visitors. Data calls are public when they hit a public API area (`/v1/gpus`, `markets`, `indices`, `history`, `spreads`, `events`, `news`, `providers`, `heatmaps`, `timeline`, `best`, `capabilities`, `opportunities`, `trust`, `families`, `fixings`, ...) or one of the legacy reads (`/market`, `/market/detail`, `/listings`, `/polling`, `/changes`). Anything else returns HTTP 401 with a Basic-auth challenge (`WWW-Authenticate: Basic`), so a browser may pop up its own login box. Pages that depend on non-public calls are flagged per page (notably `/compare`, `/ops`, and the field map on `/provider/{name}`).

**Navigation (left sidebar).** "GPUs" (`/gpus`) group with sub-links "Compare", "Explorer", "Heatmaps", "Providers". "Markets" (`/markets`) group with sub-links "Indices", "Opportunities", "Events", "News". The utility links "Methodology" and "Ops" sit at the bottom and are visible to everyone (the "Admin" group is hidden for non-admins, but "Ops" is not). The sidebar footer shows "/ search" and "? commands".

**Page header.** Built with `OG.head(title, subtitle, ...right-side controls)`: an H1, a grey subtitle line, and buttons or filters on the right.

**Stat strip.** `OG.stats`: a row of small cells (label, big value, small sub-line). A missing value shows "n/a" with the reason on hover. On some pages, history-based cells with no value are folded into one cell called "Not enough history yet" (or "Not available yet") that lists their labels.

**Tables.** `OG.table`, used almost everywhere:
- A title bar (bold title on the left, optional toolbar items, and a "CSV" button on the right that downloads the visible sorted rows).
- Clicking a column header sorts it (a second click reverses), with a ▲ or ▼ caret. Keyboard: Enter or Space.
- Rows can be clickable (the whole row navigates, and Cmd or Ctrl-click opens a new window).
- If a column is empty in every row, it is hidden automatically and named in a footer: "No data yet: Col A · Col B" (reasons on hover).
- Low-priority columns are dropped when the table is too narrow and named in the footer: "Hidden at this width: ..." (the tooltip says the CSV still has them).
- When there are more rows than the limit (500 by default, lower on some tables), a button reads "Show N more" or "Show 500 more (N hidden)".
- An empty table shows one row with the page's own empty text.

**States.** Loading: a spinner plus text (for example "Loading GPU markets…"). Error: a red box with a bold "Could not load" (or "Not found" for 404, "Sign-in required" for 401, "Not allowed" for 403), the message, a "Sign in" button on 401 and a "Retry" button where the page provides one. Empty: grey text. Insufficient data: a box with a bold title (default "Insufficient data") and the reason. The rule is to show a reason and never a made-up number.

**Badges.** The data-kind badge ("observed", "estimated", "transaction") links to `/methodology/data-kinds`. "inferred" is never shown as a badge. The freshness badge shows a coloured dot plus an age ("5m"); its tooltip says fresh, aging or stale relative to that provider's polling interval.

**Change cells.** `OG.chg`: "▲ +1.2%" or "▼ -0.8%", coloured; "-" when there is no value (reason on hover).

**Refresh.** `ctx.every(ms)` timers pause while the browser tab is hidden. Most pages also update the global "as of" status in the app header.

**URL state.** Most filters write to the query string, so a filtered view can be shared as a link. Defaults are left out of the URL.

**Note lines.** Most sections end with a small grey "note" paragraph that explains the basis and links to a methodology page ("Method", "Methodology →").

---

### /gpus (GPU markets)

- **File:** web/pages/gpus.js. Nav: "GPUs".
- **Who and access:** public. For anyone browsing GPU cloud prices. No sign-in needed (it reads `/v1/gpus`, `/market` and `/v1/spreads`, all public).
- **Purpose:** one big table of every canonical GPU, showing the current lowest, median and highest on-demand price per GPU-hour across providers, with filters.

#### Layout, top to bottom
1. **Header.** H1 "GPU markets". Subtitle: "Every canonical GPU: current on-demand price per GPU-hour across providers, availability, dispersion and capability pricing". Nothing on the right.
2. **Stat strip** (drawn after the data loads):
   - "GPUs priced now": count, sub "of N tracked".
   - "Priced listings": count, sub "N explicitly available".
   - "24h movers": "N▲ M▼", sub "K unchanged". If nothing moved: n/a "no lowest price moved". If the 24h board failed: "24h board unavailable".
   - "Most efficient": short GPU name, sub "score NN · N providers" (n/a "needs 3+ providers").
   - "Most fragmented": short GPU name, sub "score NN · spread X%".
   - "As of": time, sub date.
3. **Filter bar** (built after the first load):
   - Search box, placeholder "Filter GPUs". Matches the name or slug, live.
   - Select "All vendors" plus one option per vendor.
   - Select "All architectures" plus options.
   - Select of VRAM bands: "Any VRAM", "≤ 24 GB", "25-48 GB", "49-96 GB", "97-160 GB", "> 160 GB".
   - Select "All workloads" plus workload classes.
   - Select "All form factors" plus options.
   - Toggle chip "Available now" (tooltip: only GPUs with at least one listing explicitly in stock).
   - Toggle chip "Include unpriced" (also list GPUs nobody prices right now; they are hidden by default).
   - A spacer, then the button "Reset" (clears every filter).
4. **Table**, titled "GPU markets · on-demand". The toolbar shows "N of M GPUs" and there is a "CSV" button. Default sort: "Prov" descending. Columns:
   - "GPU": bold short name, links to `/gpu/{slug}`.
   - ("Vendor" exists but is hidden.)
   - "Arch".
   - "VRAM" ("80 GB").
   - "Form" (form factor).
   - "Class" (workload class; tooltip "editorial, from vendor positioning").
   - "Low $/GPU·h" (bold).
   - "Median".
   - "High".
   - "24h": change of the lowest price, from `/market`.
   - "24h trend": sparkline.
   - "Prov": providers with a priced listing.
   - "Avail": listings with explicit availability.
   - "Spread": high / low minus 1. "-" with only one provider.
   - "Eff.": efficiency score 0 to 100, coloured green at 75 or more, red under 50, amber in between. n/a with a reason when there are fewer than 3 providers.
   - "$/GB·h": low price divided by VRAM.
   - "$/BF16 PF·h": low price per dense BF16 petaFLOP-hour.
   
   Minor columns (dropped first on narrow screens, in order): "24h trend", "Form", "Arch", "Class". Clicking a row opens `/gpu/{slug}`. Unpriced rows are greyed (class "out").
5. **Note:** an "observed" badge, then "Observed list prices, on-demand, not interruptible, in stock or stock unknown; one vote per provider (its lowest). Spread, efficiency and 24h change are derived. $/GB and $/PFLOP ... are theoretical, not benchmarks." Links "Dispersion" and "Hardware".

#### States
- Loading: "Loading GPU markets…".
- Error from `/v1/gpus`: error box with "Retry". If `/market` or `/v1/spreads` fail, the page still renders, with n/a reasons in "24h" and "Eff.".
- Empty after filtering: "No GPUs match these filters."

#### Refresh
Every 60 s it re-reads all three sources (filters stay as they are).

#### Observations
- 17 visible columns is dense. Several headers are cryptic abbreviations ("Prov", "Avail", "Eff.", "$/BF16 PF·h", "Class", "Form"); their meaning is only in hover tooltips, which don't work on touch.
- "Avail" (count of listings with explicit availability) sits next to "Prov" (count of providers). The units differ, which is easy to misread.
- The "Most efficient" and "Most fragmented" stats use jargon ("efficiency score", "fragmentation") without an inline explanation.
- Unpriced GPUs are hidden by default, so "N of M GPUs" can look like missing data.
- web/pages/market.js says it is "Used by /gpus", but /gpus does not use it (see the market.js section).

---

### /gpu/{slug} (one GPU market)

- **File:** web/pages/gpu.js (falls through to family.js for family slugs). Nav highlight: "GPUs".
- **Who and access:** public. For someone researching one GPU (for example H100 SXM). No sign-in for reading. The "Route this →" buttons lead to `/route`, which is a non-public area (`/v1/route` is private).
- **Purpose:** a stock-quote-style page for one canonical GPU: headline price, index, history chart, provider ranking, regions, events, news, alternatives, specs.

#### Slug resolution
- If the slug is a canonical GPU, the page below renders.
- Otherwise it shows "Loading…" and tries the family endpoint. If the slug is a family or architecture (for example `/gpu/h100`, `/gpu/blackwell`), it renders the **family page** (next section).
- Otherwise: H1 "Unknown GPU", sub = the slug, empty text "No canonical GPU or GPU family has this slug.", and the link "All GPU markets →".

#### Layout, top to bottom
1. **Header.** A small eyebrow with the vendor, the short GPU name as the H1, a badge line ("Hopper · 80 GB HBM3 · SXM · training"), and the full name in small text. Right-side buttons:
   - "Route this →" (primary; tooltip "Preview where this would run (no provisioning without confirmation)"). Goes to `/route?gpu={slug}`.
   - "Compare": `/compare?a={slug}`. The second side is empty, so you land on the picker.
   - "Index": `/indices/{slug}`.
   - "Listings": `/explorer?gpu={full name}`.
2. **Quote strip.**
   - A big block on the left: label "OpenGrid index" plus an observed badge, the index level in $ plus its 24h change, and a sub-line "$/GPU·h · N providers · {hour}" (or the reason it is not published, or "index not available on this server").
   - Then the stat cells: "Low" (sub: cheapest provider), "Median" (sub "of provider lows"), "High" (sub: the most expensive provider), "Spread" (sub "$X high − low"), "Providers" (sub "priced now"), "Available" (sub "N unkn. · M out"), "24h chg", "7d chg", "30d chg", "90d chg" (index changes), "Volatility" (sub "30d annualized" or "7d annualized"), "Hist. low", "Hist. high" (sub "of lowest · date"), and "Percentile" (for example "34th", sub "median vs 30d · {label}").
   - History cells with no value are removed from the strip, not shown as n/a.
3. **Context line.** Either "Context" plus the API's summary sentences and a "how" link to `/methodology/historical-context`, or "Historical context: not available on this server.", or "Historical context: {reason}". After it comes a "History" note listing the missing history cells: "7d change · Volatility ...: not enough history yet".
4. **Section nav** (a horizontal row of buttons that scroll to each section): "Chart", "Availability", "Providers", "Regions", "Moves & news", "Events", "Alternatives", "Listings", "Best now", "Dispersion", "Distribution", "$/capability", "Hardware". The button for the section in view is highlighted (main column only).
5. **Two-column grid.**

**Main column:**
- **"Price history".** Controls on the right:
  - View segmented "Market" or "Providers" (tooltips "low-high band, median, lowest, index" and "one step line per provider").
  - Window "24H", "7D" (default), "30D", "90D", "ALL".
  - "overlay" chips "Events", "News", "Moves" (all on by default).
  
  The chart is 340 px tall:
  - Market view: a low-high band, a "Median" line, a "Lowest" line (green or red by direction), and a dashed orange "OG index" line.
  - Providers view: one coloured step line per provider plus a "Best price" halo line.
  - Hours before every current provider was recorded are hatched.
  - Event, news and move markers sit on the time axis. Clicking a move marker opens a panel under the chart, titled "Related news and events around this move (not necessarily causal)", with a "×" close button. It lists up to 12 items with "+Nh" or "-Nh" offsets and news/event badges.
  - If hourly history has fewer than 6 points, the chart falls back to `/market/detail` (a fine-grained replay) and the note says so.
  - Note text: hourly cross-section, hatching explanation, "Dashed: OpenGrid index", "N markers on the time axis (hover)", and a "Method" link.
- **"Availability history".** A 150 px chart with three lines: "Available listings", "Priced providers", "Sold-out listings". With fewer than 2 hours of data: insufficient box "Not enough availability history" ("only N hours recorded in this window"). Note: "Available = explicit availability; many providers publish none (counted as unknown, not available)", plus "N provider availability transitions in this window."
- **"Provider ranking".** Table titled "N providers · one vote each (its lowest eligible price)", with CSV. Columns:
  - "#".
  - "Provider" (colour swatch plus link).
  - "Best $/GPU·h" (bold).
  - "vs median" (own lowest vs the others' median).
  - "24h" (shown only if any row has it).
  - "Lst" (priced eligible listings).
  - "Stock" ("in stock" green, "sold out" red, "unknown" grey).
  - "Region".
  - "Fresh" (freshness badge, "…" while trust data loads).
  - "Trust" (confidence word; reasons on hover).
  - "Source".
  - A link column "site ↗".
  
  Hovering a row highlights that provider's line when the chart is in Providers view. Note: "N sold-out listings excluded" and a "Method" link. Empty: insufficient box "No live price" ("No provider is selling the {GPU} in stock on demand right now (N live listings, M sold out).").
- **"Region pricing".** Table with columns:
  - "Region group" ("Unassigned" in grey, tooltip "no location given, or a listing spanning several groups").
  - "Cheapest $/GPU·h".
  - "at" (provider, when known).
  - An unlabelled bar.
  - "Median".
  - "vs global" (or "Regional median vs global" in the fallback version).
  - "Providers".
  - "Priced listings".
  - "Available".
  - A "compare →" link that opens `/compare/{region}-vs-{US or Europe}?gpu={slug}`.
  
  Note: "No listing now in: ...", "Region groups from each listing's stated location; never guessed", "As of ...", and the link "All regional heatmaps →". It falls back to the heatmap matrices if `/v1/markets/{slug}/regions` is missing. Empty states: "No regional data", or "No priced listing for this GPU in any region now."
- **"Price moves and related news".** Two sub-columns:
  - Left: label "Moves of the lowest price", then a table titled "Click a move: related news and events" with columns "Hour", "Move" (+/- %), "From → to", "Providers" ("same" or "set changed", with a tooltip listing who joined or left). Clicking a row opens the same "around this move" panel below the table. Empty: "No hour-over-hour move of the lowest price ≥ 3% in this window."
  - Right: label "News mentioning this GPU", then a list (date, source-tier tag, title as an external link, source name, "+N" other sources). Note: "Linked by mention of this GPU (matched by rules). Appearing near a price move does not mean it caused it." Link "More →" goes to `/news?gpu=`. Empty: "No news item mentioning this GPU has been ingested yet."
- **"Market events".** Severity segmented control on the right: "All", "Notable+" (default), "Major". Table columns: "When", "Sev" (coloured dot plus word), "Type", "Event" (title, plus the data kind when it is not observed or inferred), "Provider" (or "market"). 15 rows, with CSV. Links "All events for this GPU →" and "How events are detected". Empty: "No market events for this GPU at this severity."
- **"Related and alternative GPUs".** Table titled "Shares the listed dimensions, not equivalent products". Columns:
  - "GPU".
  - "Shares" (badges such as "similar VRAM", "same architecture", "training", "similar $/TFLOP").
  - "Low".
  - "Median".
  - "Δ median $".
  - "Δ VRAM".
  - "Δ BF16".
  - "Δ mem BW".
  - "Prov".
  - "Avail".
  - A "compare →" link to `/compare/{this}-vs-{alt}`.
  
  12 rows. Note: "... Deltas are the alternative relative to the {GPU}." Empty: "No alternatives" with a reason.
- **"All current listings".** Table titled "N listings (all market types, incl. spot and sold out)". Columns: "Provider", "SKU", "GPUs", "$/GPU·h", "$/inst·h", "Type", "Tier", "Region", "Stock", "Seen" (freshness badge). 25 rows, then "Show more". Empty: "No current listings."

**Right rail:**
- **"Best available now".** Segmented control "1×", "2×", "4×", "8×" (GPU count). It shows the top-ranked listing:
  - A header with the provider logo, name, location (or "location not given") and a big price "/GPU·h".
  - "score 0.83 · {mode} · N× {sku}", plus a badge "provisionable" or "market data only".
  - A factor table: factor name, bar, "×weight", contribution, and a "neutral" badge when the factor was imputed.
  - The explanation sentence, a "Route this →" button, and the link "how it is scored".
  - Up to 5 runner-up rows (rank, provider, region, price, score).
  - Note: "N candidates, M excluded (...). Prices are observed market prices, not quotes. Reliability and performance are not used: no execution history yet."
  - Empty: "Nothing to route" ("No eligible N-GPU listing now (...)") plus a button "Open route preview →". On 404: "Unavailable".
- **"Dispersion and efficiency".** A big efficiency number "/ 100", a label (coloured), "fragmentation N · confidence X", a component table ("component", "raw", "saturation" bar, "w", "pts") with rows "CV (adj.)", "IQR / median", "High / low − 1", and key-value rows "Spread", "IQR", "Std dev". Note gives the formula: "Fragmentation = 100 × Σ w·min(raw / saturation, 1); efficiency = 100 − fragmentation." When not computable: insufficient box "No efficiency score" plus the spread.
- **"Price distribution now".** A dot plot. Row "Providers" has one dot per provider (linking to the provider page). Row "Listings" has fresh on-demand listings. Key-value rows: "Providers · listings", "Q1 to Q3 (providers)", "Listing median", "Mean (providers)". Note: "Tick: median. Bar: low-high. ..." Empty: "No prices now."
- **"Price per capability"** with a "theoretical" warning badge. Table columns "$", "spec", "at low", "at median". Rows: "per GB VRAM·h", "per BF16 PFLOP·h", "per FP16 PFLOP·h", "per FP8 PFLOP·h", "per TB/s·h".
- **"Hardware".** Key-value list: "Vendor · arch", "Released · segment", "VRAM", "Memory bandwidth", "BF16 (dense)", "FP16 (dense)", "FP8 (dense)", "FP32", "Form factor", "Host link", "GPU-GPU link", "TDP", "Workload class", and "Partition of" (for MIG-style partitions). Then source links and "Method".

#### States
Each section loads on its own, with its own spinner ("Loading market…", "Loading providers…", "Ranking listings…", "Loading history…").

#### Refresh
- Every 60 s: summary (quote strip, providers, dispersion, distribution, hardware, capability, alternatives) and "Best available now".
- Every 5 min: index, chart, timeline, trust, regions, events.
- News and listings load only once.

#### Keyboard
Pressing Escape anywhere on the page (outside inputs) navigates to `/gpus`.

#### Observations
- This is a very long page with about 13 sections and a 13-button section nav. The nav only highlights main-column sections; rail sections never light up.
- The quote strip can have up to 15 cells. The big headline number is the "OpenGrid index", not the "Low" price, which may confuse a visitor who wants the cheapest price.
- "Route this →" appears twice (header and rail), but `/route` is not a public area, so anonymous visitors hit a sign-in wall.
- The "Compare" header button opens the picker with only side A filled, not a ready comparison.
- Jargon: "one vote each", "eligible", "coherent", "fragmentation", "CV (adj.)", "saturation", "IQR", "imputed", "provisionable", "chain".
- Terms differ between the chart ("OG index") and the header ("OpenGrid index"). The rail's "Best available now" uses a score with no units.
- Pressing Escape to leave the page is unexpected and undiscoverable.
- News and listings never refresh, while other sections refresh on 60 s and 5 min cycles, so the page can look partly stale.

---

### GPU family page (served at /gpu/{family-slug})

- **File:** web/pages/family.js (exposes `OG.views.family`, mounted by gpu.js).
- **URL:** there is no separate route. The family page is shown at `/gpu/{slug}` when the slug is a family or architecture rather than a canonical GPU, for example `/gpu/h100` or `/gpu/blackwell`. The command bar's "g h100" also lands here. Nav highlight: "GPUs".
- **Who and access:** public (reads `/v1/families/{slug}`, `/v1/history/{variant}`, `/v1/news`).
- **Purpose:** show every variant of a GPU family side by side, each with its own price, and never merge them into one family price.

#### Layout, top to bottom
1. **Header.** Eyebrow "GPU family" (or "GPU architecture"), the family name as H1, an architecture badge (family kind only), and a small "N variants". Right side:
   - "Compare variants" (only with 2 or more variants): `/compare/{first}-vs-{second}`.
   - "Route →" (primary; tooltip "Route with this family: pick a variant or allow variants"): `/route?gpu={family}`.
2. **Callout box** (role note): bold "Variants are different products." then "Memory size, form factor and interconnect differ, so OpenGrid never merges their prices into one family price; each row below is its own market." Plus the API note. For architectures it adds "Model families in this architecture: ..." with links.
3. **Stat strip:**
   - "Variants": N, sub "M priced now".
   - "Cheapest variant now": price (observed badge), sub "{variant} · {provider}". Tooltip: "The lowest observed on-demand price of one variant, not a family price".
   - "Indexed variants": N, sub "each with its own index".
4. **"Variants side by side".** Table sorted by "Low" ascending, with CSV. Columns:
   - "Variant" (colour swatch plus bold link).
   - "Low $/GPU·h".
   - "at" (provider; shown only if any row has it).
   - "Median".
   - "High".
   - "24h" (change of the variant's own index).
   - "Providers".
   - "Available".
   - "Index" (level, links to `/indices/{id}`).
   - A "market →" link.
   
   Clicking a row opens `/gpu/{variant}`. Hovering a row highlights its line in the chart. Empty: "This family has no variants listed."
5. **"Lowest price per variant".** Window segmented control "24H", "7D" (default), "30D", "90D". A 240 px chart with one line per variant, coloured to match the table swatches. Gap tooltips say "no recorded price: X, Y". Note: "One line per variant ... Lines are never averaged into a family line. N variants without history in this window." Empty: insufficient box "No history" ("No variant has hourly price history in this window yet.").
6. **"News mentioning the {family}".** A list like the GPU page's, with "More →" to `/news?gpu={family}`. Empty: "No news item mentioning the {family} has been ingested yet."

#### Refresh
The chart re-reads every 5 min. The table and stats do not refresh (they come from the initial family read).

#### Observations
- Same URL pattern as a single GPU, so users may not realise `/gpu/h100` is a different kind of page. The eyebrow "GPU family" is the only cue besides the callout.
- The warning callout is long and sits above the data.
- "Compare variants" always picks the first two variants in API order, not the two the user cares about.
- The family window choices (no "ALL") differ from the GPU page's (which has "ALL").
- The variant table and stats never refresh, while the chart does.

---

### /providers (provider league table)

- **File:** web/pages/providers.js. Nav: "Providers" (under GPUs).
- **Who and access:** public. For someone choosing between clouds, or checking how OpenGrid covers each provider. No sign-in (`/v1/providers`, `/v1/capabilities` and `/v1/trust/providers` are public).
- **Purpose:** every provider OpenGrid reads, with coverage, price vs market, feed health and integration level.

#### Layout, top to bottom
1. **Header.** H1 "Providers". Subtitle: "Every GPU cloud OpenGrid reads: coverage, pricing vs the rest of the market, feed health and how far OpenGrid can route to it". Right side:
   - "Class" segmented control: "All" plus whichever classes are present ("hyperscaler", "neocloud", "general cloud", "marketplace", "decentralized", "unclassified").
   - "Window" segmented control: "7D", "30D" (default), "90D".
2. **Stat strip:**
   - "Providers": N, sub "N with live listings here".
   - "Live listings": N (observed), sub "N priced".
   - "GPU models": N, sub "canonical, across providers".
   - "Feeds healthy": "N/M", sub "last 24h of fetches".
   - "Rel. value": "N rated" (sub "of M, last 30d"). Otherwise n/a: "needs ≥ 24 compared hours in the last 30d; no provider has them yet".
   - "Routable (impl.)": N, sub "L≥2 implemented · 0 verified live".
3. **Table** titled "N providers · window 30d", with CSV. Default sort: "Listings" descending. The last two columns stay sticky when scrolling sideways. Columns:
   - "Provider" (logo plus link).
   - "Class".
   - "Source" ("auth API", "public API", "public page", "aggregator", "price file").
   - "Integration": "L0 data only", "L1 avail. check", "L2 provision" or "L3 lifecycle", plus "·mock" when implemented above L0. The hover lists what OpenGrid implements, what the provider API documents, "Verified live against a real account: no", and so on.
   - "GPUs".
   - "Listings" (hover shows the priced count).
   - "Regions" ("US · Europe", or "unassigned").
   - "Avg prem." (green when negative, meaning cheaper).
   - "% cheapest".
   - "Avail.".
   - "Vol (1d)".
   - "Lifetime" (median listing lifetime, "36.0h" or "5d").
   - "Feed": a green dot plus "healthy" or "ok", or a badge "degraded", "down", "idle", "N% fail" or "no feed".
   - "Last ok": freshness badge, or "never".
   
   Rows with no live listings are greyed. Clicking a row opens `/provider/{name}`.
4. **Note:** observed badge, "coverage and feed health. Relative value: each provider vs the median of the other providers' lowest price for the same GPU in the same hour ... Figures stay n/a (hover for the reason) until there are ≥ 24 compared hours. Greyed rows: no live listings on this server." Link "Methodology →".
5. **Two boxes side by side:**
   - **"Facts".** First "observed · right now" bullets: who lists the most GPU models, who has the most live listings, how many providers list in two or more region groups, how many publish no location, and "No live listings on this server from: ... (no API key configured here, or the feed has not run)". Then "Relative value, last 30 days" bullets from the API: 8 shown, then the button "Show all N facts" / "Show fewer". With none, a grey line explains they need 24 compared hours.
   - **"Integration levels".** A legend table: L0 "market data only", L1 "availability check", L2 "provisioning", L3 "full lifecycle", each with a description. Note: "·mock = implemented in OpenGrid's adapter and tested against mocked HTTP ... No adapter has been run against a real provider account yet (verified_live = false for every provider) ...". Link "Routing methodology →".

#### States
Loading: "Loading providers…" (the stats show "loading"). Error with "Retry".

#### Refresh
Every 2 min, a full reload. The table spinner reappears on each reload, so the table flashes.

#### Observations
- 14 columns, many abbreviated ("Avg prem.", "% cheapest", "Avail.", "Vol (1d)", "Lifetime", "Last ok").
- Many cells are n/a until there is 24 hours of compared history, so a new deployment shows a mostly empty value section.
- The "·mock" suffix and "verified_live = false" are honest but read as internal engineering language.
- The "Integration" column is about OpenGrid's capability, not the provider's quality. It sits among market metrics and could be mistaken for a rating.
- The Facts bullet "No live listings on this server from: ... (no API key configured here ...)" exposes server configuration to public visitors.
- The table re-shows a loading spinner on every 2 min refresh, unlike other pages that update in place.

---

### /provider/{name} (one provider)

- **File:** web/pages/provider.js. Nav highlight: "Providers".
- **Who and access:** public. For someone evaluating one cloud (for example RunPod). The "Source field map" section calls `/mapping`, which is not public. For anonymous visitors the code swallows the error and shows "No field map for this provider." (the browser may also show a Basic-auth prompt). Everything else is public.
- **Purpose:** one provider in depth: what it lists, how its prices compare with the market, history, integration, feed health, events, news and raw listings.

#### Layout, top to bottom
1. **Header.** Logo, display name (H1), and a class eyebrow. Subtitle: "{class} · prices read from {host} ({source type})". Right side:
   - "Window" segmented control "7D", "30D" (default), "90D".
   - Select "Compare with…" (all other providers). Choosing one goes to `/compare?a={this}&b={other}`.
   - Buttons "Listings →" (`/explorer?provider=`), "News →" (`/news?provider=`) and "Website ↗" (external, when known).
   
   If provider metadata has a note, a note paragraph follows.
2. **Stat strip:**
   - "GPUs" (sub: up to 3 architectures).
   - "Listings" (sub "N priced").
   - "Regions" (folded if empty).
   - "Cheapest now" ("N/M", sub "GPUs where it is rank 1").
   - "Median prem. now".
   - "Avg prem. 30d", "% cheapest", "Availability", "Volatility": all folded into "Not enough history yet" when empty.
3. **Two columns.**

**Left column:**
- **"Facts".** Bullets: API facts; "{name} lists N GPU models across N live listings; its most widely traded is {GPU} at $X/GPU-hr, rank R of M"; "Cheapest provider right now for ..."; "Only provider OpenGrid sees selling ... No comparison possible." With no data: "No priced live listings from {name} on this server." Grey hint about the 24 compared hours.
- **"Catalog vs market".** Table titled "N GPUs · market = every provider's lowest live price now", sorted by "Prem. now" ascending, with CSV. Columns:
  - "GPU".
  - "Best $/GPU·h".
  - "Mkt median".
  - "Prem. now" (negative = cheaper).
  - "Rank" ("1/5", green when 1).
  - "Avg 30d".
  - "% cheapest".
  - "In stock" ("3/7").
  - "Regions".
  
  Clicking a row selects that GPU for the "Price history" chart below and scrolls to it. It does not navigate. Empty: insufficient box "No catalog" ("OpenGrid has no live listings from {name} on this server. On servers without this provider's API key, its feed does not run.").
- **Two small boxes side by side:**
  - "Where it beats the market": rows of GPU link, bar, percentage, and a badge "now" or "30d".
  - "Where it is expensive": same layout.
  - Empty texts: "No GPU where it is below the market median." and "No GPU where it is above the market median."
- **"Relative value history".** Segmented control in the heading: "Avg premium", "Rank", "Cheapest share", "Availability". A daily line chart (180 px); "Cheapest share" adds a dashed "Top-3 share" line. With fewer than 2 days: insufficient box "History building" ("N days of rollup history ... Today so far: premium X, mean rank Y, cheapest Z of N market hours."). The note explains the daily pooled basis.
- **"Price history".** Controls:
  - "GPU" select (options show short name plus best price).
  - "Window" segmented control "24H", "7D" (default), "30D".
  - Link "GPU market →".
  
  Chart (240 px): dashed grey "Market lowest" and "Market median" lines plus a bold "{name} lowest" line, with markers for events, price moves and news. Below it are context bullets (observed summary sentences, "Percentile vs its own history: {reason}"). With fewer than 2 hours: "History building". Empty: "No GPU to chart."
- **"Recent price changes".** Table titled "N listings whose price changed since first seen". Columns: "When" ("3h ago"), "GPU", "Listing", "Tier", "From", "To", "Change". 12 rows. Empty: "No listing has changed price since OpenGrid started recording it."

**Right column:**
- **"Routing integration".** The integration level text plus a badge "not verified live" (or "verified live"). A sentence: "OpenGrid's adapter can launch, check status and terminate ... It was tested against mocked HTTP ... no real provisioning call has been made." (or "OpenGrid reads {name}'s prices only. It cannot check stock live, quote or provision here ..."). Key-value rows: "Provider API documents", "OpenGrid implements", "Verified live", "Avail. check live-verified", "Routed via", "Resource", "Credential", "Configured here", "Stop supported", "API docs" (external link).
- **"Source & feed health".** Feed status, freshness badge, "polled every 15m". Key-value rows: "Source host", "Source type", "Last ok fetch", "Fetches 24h", "Latency p50 / p95", "Listings now / 24h ago", "Schema changes 7d", "Quarantined values", "Consecutive failures", "Listing lifetime (median)". Links "Field map ↓" (in-page anchor) and "Data trust →".
- **"Regions covered".** A table of region group, "N GPUs", "N listings". Then "As published:" followed by the raw location strings with counts (or "This feed publishes no location for its listings."). Empty: "No live listings."
- **"Market events".** Compact one-line event rows (up to 12), then the link "All events →". Empty: "No market events for this provider yet."
- **"Related news"** with an "all →" link in the heading. Up to 8 items (title link, then "date · source +N · rel {score} · topics"). Note: "Matched by entity rules; shown as related, never as a cause of a price move." Empty: "No stored news mentions {name}."

**Full width, below the columns:**
- **"Source field map".** Two boxes: "Endpoints read" (key-value) and "Quirks" (list). Then a table "Field provenance" with columns "Field", "Kind" (badge "raw", "derived", "lookup" or "absent"), "Source", "Note". Empty (and for anonymous visitors): "No field map for this provider."
- **"Current listings".** Table titled "N listings incl. sold-out and ineligible rows", 50 rows, with CSV. Columns:
  - "GPU" (or raw name plus "· unmapped").
  - "Raw name".
  - "GPUs".
  - "$/GPU·h".
  - "$/inst·h".
  - "Type" ("on_demand · interruptible · tier").
  - "Region".
  - "Stock".
  - "Fresh".
  - "Conf.".

#### States
If `/v1/providers/{name}` returns 404, the subtitle changes to "Not a provider OpenGrid tracks", the title becomes "Unknown provider", and an error box replaces the catalog. The rest of the page skeleton still shows.

#### Refresh
Every 2 min it reloads the main payload, then feed health and listings. Integration, events, news and the field map load once.

#### Observations
- Very long, with about 14 sections. The engineering sections ("Source field map", "Field provenance", "Quarantined values", "Schema changes 7d") sit on a public page next to buyer-facing price info.
- The field map silently shows "No field map" to anonymous users because of auth. That looks like missing data rather than restricted data.
- Clicking a catalog row changes the chart instead of navigating, while most other tables navigate on click. This is inconsistent.
- An unknown provider leaves a half-built page (empty sections) instead of a clean not-found state.
- Two different "premium" concepts ("Prem. now" vs "Avg 30d" / "Avg prem.") and two "%" share metrics can confuse.
- The "Compare with…" select navigates immediately on change, with no confirm button.

---

### /compare and /compare/{pair}

- **File:** web/pages/compare.js. Routes:
  - `/compare/:a-vs-:b`, for example `/compare/h100-80gb-sxm5-vs-h200-141gb-sxm5`.
  - `/compare?a=&b=` (optional `&gpu=`).
  
  Nav: "Compare" (under GPUs).
- **Who and access:** (Fixed 2026-10-15: `/v1/compare`, `/v1/regions` and `/v1/hardware` are now public, so the note below no longer applies.) The page loads publicly, but **the GPU vs GPU and provider vs provider views call `/v1/compare`, and "compare" is not in the public API list (PUBLIC_V1 in api/pages.py)**. For an anonymous visitor in production that call returns 401: the page shows the error box "Sign-in required" with a "Sign in" button (and the browser may pop up a Basic-auth prompt). Region vs region uses `/v1/heatmaps` (public) and works anonymously. The server-side SEO summary for compare URLs is removed once the app mounts. Worth verifying in production.
- **Purpose:** put two GPUs, two providers or two region groups side by side.

#### Common top part
- **Header.** H1 "{A} vs {B}" (or "Compare"). The subtitle depends on the pair type:
  - "GPU vs GPU: price, availability, capability pricing and specs, side by side".
  - "Provider vs provider: coverage, pricing on common GPUs, relative value and feed health".
  - "Region vs region: the cheapest price of each GPU in two region groups".
  - "Pick two GPUs, two providers or two regions".
  - With nothing picked: "Two GPUs (H100 vs H200), two providers (RunPod vs Lambda), or two regions (US vs Europe)".
- **Picker bar:**
  - "A" text input with an autocomplete list (GPU slugs, "provider · X", "region · X"), placeholder "GPU, provider or region".
  - A swap button "⇄" (swaps the two inputs and runs the comparison).
  - "B" input.
  - Primary button "Compare". Enter in either input does the same.
  - On the right, preset links: "H100 vs H200", "H100 PCIe vs SXM", "A100 vs H100", "B200 vs H200", "RunPod vs Lambda", "Lium vs AWS", "US vs Europe".

#### Empty state (no pair)
Section "Popular comparisons" with buttons (only pairs that exist): "H100 vs H200", "H100 PCIe vs H100 SXM", "A100 vs H100", "H200 vs B200", "L40S vs A100 PCIe", "RTX 4090 vs RTX 5090". Note: "Or type `c h100 h200` in the command bar."

#### Error states
- An unknown side: insufficient box "Unknown" ("\"x\" is not a canonical GPU slug, a provider or a region group.").
- Mixed types: the API's message or "{A} is a gpu and {B} is a provider: compare two GPUs, two providers or two region groups." under the title "These two cannot be compared", plus "Try two GPUs or two providers." links.

#### GPU vs GPU layout
1. Two side cards (blue for A, orange for B): the GPU name links to its market, "N providers · M priced listings", and a big low price "low /GPU·h" (or n/a "no live price").
2. A caveat line: the API note plus "Shared dimensions:" badges (or "none").
3. Two-column grid.
   - **Left column:**
     - **"Verdicts":** bullets, each reading "{label}: **{cheaper GPU}** is X% cheaper ($a vs $b /unit)" (or "about the same", or n/a "one side has no value"), for "Lowest price now", "Median price now", "Per dense BF16 PFLOP (median)", "Per dense FP8 PFLOP (median)", "Per GB of VRAM (median)" and "Per TB/s of memory bandwidth (median)". Theoretical rows carry "†", and the footnote explains it.
     - **"Price history":** window "7D", "30D" (default), "90D". An overlay chart with A median (solid blue), A lowest (dashed blue), B median (solid orange), B lowest (dashed orange). Note says hourly or fine replay. Empty: "No history to overlay".
     - **"Same provider, both GPUs":** table titled "N providers price both". Columns "Provider", "{A}" (price "#rank" or "not sold"), "{B}", "B vs A" (green = B cheaper). Rows not selling both are greyed. CSV.
   - **Right column:**
     - **"Market and capability pricing":** a 3-column table ("", A, B, "B vs A") with rows "Lowest $/GPU·h", "Median $/GPU·h", "Highest $/GPU·h", "Providers pricing", "Priced listings", "Available listings", "Sold-out listings", "Efficiency score", "Market label", and five "$ per ... (median)" rows. Note: "B vs A on prices: green means B is cheaper. ..."
     - **"Spec diff":** rows "Vendor", "Architecture", "Generation", "Released", "Segment", "VRAM (GB)", "Memory", "Bandwidth (TB/s)", "BF16/FP16/FP8 TFLOPS (dense)", "FP32 TFLOPS", "Form factor", "Host link", "GPU-GPU link", "TDP (W)", "Workload class". The last column "B / A" shows "×1.76" or "differs". Rows that differ are highlighted.

#### Provider vs provider layout
1. Two side cards: logo plus name (link), class and note, "N GPUs · M priced".
2. Stat strip: "Common GPUs" (sub "N only A · M only B"), "{A} cheaper on" ("x of n"), "{B} cheaper on", "Median B vs A" (sub "{X} cheaper").
3. Grid.
   - **Left:**
     - A table titled "Common GPUs · click one to chart it". Columns "GPU", "{A}" ("$ #rank"), "{B}", "B vs A", and a "market →" link. Clicking a row selects it (highlighted) and charts it.
     - A chart section whose heading reads "{GPU} at both providers", with window "7D" (default), "30D", "90D". Lines: A, B, and a dotted "market lowest". Empty: "These providers have no GPU in common right now." / "No common GPU to chart."
   - **Right:**
     - **"Side by side":** rows "Class", "Source", "GPUs listed", "Live listings", "Priced listings", "Architectures", "Region groups", "Avg premium vs market", "Cheapest-provider share", "Availability (hours)", "Feed failure rate 24h", "Feed latency 24h", "Last good fetch".
     - **"Only one sells":** two rows "only {A}" and "only {B}" with GPU links.

#### Region vs region layout
1. Side cards: region name, "region group", and "N GPUs priced" (or n/a).
2. If `?gpu=` is set, a focus line: "**{GPU}**: cheapest US $x · Europe $y · B vs A z%" plus "all regions →".
3. Stats: "GPUs in both", "{A} cheaper on", "{B} cheaper on".
4. Table "Same GPU across two region groups". Columns "GPU", "{A} cheapest", "{B} cheapest", "B vs A", "{A} vs global", "{B} vs global", "{A} listings", "{B} listings". The focus GPU row is highlighted.
5. Note about region groups and the link "Regional heatmaps →".

#### Refresh
None. Compare pages never auto-refresh.

#### Observations
- Likely the biggest functional issue in this scope: anonymous visitors cannot load GPU vs GPU or provider vs provider comparisons (no "compare" in PUBLIC_V1), even though the compare pages are advertised as public and linked from GPU pages, alternatives and presets. Worth verifying in production.
- Three different comparison modes share one free-text picker. The autocomplete mixes GPU slugs (for example "h100-80gb-sxm5"), "provider · X" and "region · X", and the inputs display raw slugs.
- Default windows differ by mode (30D for GPU vs GPU, 7D for provider vs provider). Region mode has no history.
- Colour coding is used for meaning: "B vs A" green means B is cheaper, but in "Spec diff" green ("up") means B's spec is bigger. The same colour means different things in adjacent tables.
- Preset labels differ slightly between the top bar ("B200 vs H200") and "Popular comparisons" ("H200 vs B200").
- Rank shown as "#2" next to prices without explanation.

---

### /explorer (listings explorer)

- **File:** web/pages/explorer.js. Nav: "Explorer" (under GPUs; alias "Listings").
- **Who and access:** public (reads `/listings`, plus soft reads of `/v1/gpus` and `/v1/trust/listings`). For power users who want raw rows.
- **Purpose:** every current listing from every provider, filterable, sortable, shareable and exportable.

#### Layout, top to bottom
1. **Header.** H1 "Explorer". Subtitle: "Every current listing from every provider, as observed. Filter, sort, share the URL, export CSV." Right side: button "Reset" (clears all filters).
2. **Filter bar.** Each control is a small label plus a control:
   - "Text" input, placeholder "gpu, sku, region…" (searches many fields, with a 140 ms debounce).
   - "GPU" select: "All", canonical GPUs, and "(unmapped names)".
   - "Provider" select ("All" plus providers).
   - "Region" select (raw region strings).
   - "Market" select (market types such as on_demand or spot).
   - "Tier" select.
   - "GPUs" select ("1×", "8×", ...).
   - "Stock" select: "Any", "in stock", "not sold out", "sold out", "unknown".
   - "$ min" input (placeholder "0.00").
   - "$ max" input (placeholder "∞").
3. **Table** titled "Listings". The toolbar shows "N of M listings · G GPUs · P providers", with CSV. Default sort: "$/GPU·h" ascending. 400 rows, then "Show more". Columns:
   - "GPU" (link; or the raw name in grey when unmapped).
   - "VRAM" (when `/v1/gpus` answered, "80G").
   - "Arch".
   - "Provider".
   - "SKU".
   - "GPUs".
   - "$/GPU·h" (n/a when no price was published).
   - "Last chg" (change vs previous recorded price; hover "was $X, changed {date}").
   - "$/inst·h".
   - "Region".
   - "Ctry".
   - "Market".
   - "Tier".
   - "Intr" (interruptible yes/no).
   - "Stock".
   - "Stock basis" (when trust data exists).
   - "vCPU".
   - "RAM".
   - "Source".
   - "Seen" (freshness badge; tooltip "fresh ≤30m, aging ≤3h, else stale").
   - "First seen".
   
   Many of these are minor and drop out on narrow screens. Rows are not clickable; only the GPU and provider links navigate.
4. **Note:** observed badge, "Prices and stock exactly as each provider reports them, normalized to USD per GPU-hour. Market lines elsewhere use a stricter subset (on-demand, not interruptible, in stock or unknown)." Link "Methodology".

#### States
Loading: "Loading listings…". Error with "Retry". Empty: "No listings match these filters."

#### Refresh
Every 2 min, `/listings` is re-read. Filter options are built only once (on the first load).

#### Observations
- Up to 21 columns, most of them abbreviated ("Ctry", "Intr", "Tier", "Last chg").
- Ten filters in a single bar. "Region" lists raw provider region strings (for example "us-east-1", "US-TX-3"), not the region groups used elsewhere.
- Filter options are never rebuilt after the first load, so a GPU or provider that appears later is missing from the selects until reload.
- The "Reset" button is in the header, far from the filter bar it resets.
- The table is rebuilt when the optional enrichment calls return, so sort and scroll can jump shortly after load.
- `?gpu=` uses the full canonical name (for example "NVIDIA H100 80GB SXM5"), not the slug used everywhere else, which makes URLs long.

---

### /opportunities (opportunity monitor)

- **File:** web/pages/opportunities.js. Nav: "Opportunities" (under Markets).
- **Who and access:** public (`/v1/opportunities`, `/v1/events/types`). For deal-hunters.
- **Purpose:** list places where listed prices or supply look unusual right now, ranked by a 0 to 100 score.

#### Layout, top to bottom
1. **Header.** H1 "Opportunity monitor". Subtitle: "Where listed prices or supply look unusual right now, ranked by score. Observations about listed prices, not advice or quotes: a listing can change or sell out before you act." Right side: a mono "hour {time} · N items" (or "no market history") and the button "Methodology".
2. **Filter bar:**
   - "GPU" select ("All GPUs" plus live GPUs).
   - "Provider" select ("All providers" plus providers).
   - "Min score" segmented control: "ANY", "25+", "50+", "75+".
   - A spacer, then the button "Reset".
   
   GPU, provider and min score refetch from the server.
3. **Type tabs:** "All · N", then one tab per type with its count or "n/a": "Wide spread", "Price cut", "New cheap inventory", "Regional dislocation", "Below usual premium", "Scarcity", "New cheapest provider", "Supply change". Tab tooltips give the type description or the reason it is unavailable.
4. **Body.**
   - "All" tab: one block per type that has items, ordered by its best score. Each block's title is the clickable type name (opens that tab), a count, and the description, plus a data-kind badge if uniform.
   - A single-type tab: one table, or an insufficient box "Could not evaluate {type}" with the reason.
   
   Table columns:
   - "Score" (bar plus number).
   - "Type" (hidden in grouped view).
   - "GPU".
   - "Provider / region" (provider link, a region pill, or "market").
   - "Why it is listed" (explanation sentence).
   - "Numbers": type-specific key/value chips, for example for Wide spread "spread, low ($ + provider), median, high, 30d p90 / baseline, providers"; for Price cut "24h ago, now, chg"; for Scarcity "available, 30d avg, ratio, providers avail".
   - "Kind" (hidden when uniform).
   - Links "events" and "route".
   
   Default sort: score descending. CSV per table.
5. **Below:** a "Could not evaluate" table (type and reason) when some types failed, and a note "Evaluated, nothing found: X, Y."

#### States
- Loading: "Evaluating the market…".
- Empty: "No opportunities pass the rules right now." / "... for these filters." / "No market history yet: the hourly rollup has not run."
- Per-table empty: "Nothing of this type right now."
- Error with "Retry".

#### Refresh
Every 60 s.

#### Observations
- The H1 "Opportunity monitor" doesn't match the nav label "Opportunities".
- Up to 9 tabs plus grouped blocks repeat the same information twice (tab counts and block counts).
- Type names are jargon ("Regional dislocation", "Below usual premium", "Supply change").
- The "Numbers" chips use tiny labels and differ by type, so they are hard to scan across rows.
- The "route" link leads to the private `/route` area for anonymous users.
- "Reset" changes three filters plus the tab in one go. There is no indication which filters are active apart from the controls themselves.

---

### /events (market events)

- **File:** web/pages/events.js (also exports the event row used by /markets and /provider). Nav: "Events" (under Markets).
- **Who and access:** public (`/v1/events`, `/v1/events/types`).
- **Purpose:** a feed of structured changes detected in the hourly market record (price moves, new lows and highs, sold out, restocks, feed outages).

#### Layout, top to bottom
1. **Header.** H1 "Market events". Subtitle: "Structured changes detected in the hourly on-demand market record. Events say what changed, never why." Right side: "N events" (mono) and the button "Methodology".
2. **Filter bar:**
   - "Type" select ("All types" plus type names).
   - "GPU" select ("All GPUs").
   - "Provider" select ("All providers").
   - "Severity" segmented control: "ALL", "NOTABLE+", "MAJOR".
   - "Since" segmented control: "24H", "7D" (default), "30D", "90D", "ALL".
   - Toggle chip "Coverage starts" (tooltip: "coverage_started marks when OpenGrid began recording a provider: not a market change"; off by default, which excludes those events).
   - A spacer, then the button "Reset".
3. **Main area, two columns.**
   - **Left:**
     - A box "Events per day" showing a small vertical bar chart (72 px) of daily counts over the window (up to 90 days). Its note reads "N events · D days", or "newest 1000 of N events counted · ..." when truncated.
     - The feed: day separators ("Mon 12 Oct 2026", "N shown"), then one row per event. Each row shows the time (HH:MM), a severity dot, a type tag ("PRICE", "SOLD OUT", "RESTOCK", "ADDED", "REMOVED", "CHEAPEST", "30D LOW", "30D HIGH", "ATL", "ATH", "SPREAD", "REGIONAL", "CAPACITY", "FEED DOWN", "FEED UP", "COVERAGE"), the title, numbers ("$2.10 → $1.95", "▼ -7.1%" or "45% spread"), GPU, provider and region links, and a non-default data-kind tag. The row tooltip gives the title, note, cause, reason and detected time.
     - Below: the button "Load N more · M older", or "end of feed".
   - **Right sidebar "Event types":** one button per type (tag, readable name, kind badge, description, "severity X"). Clicking filters to that type (clicking again clears it). The active one is highlighted. A legend for "info", "notable", "major" dots.

#### States
- Loading: "Loading events…".
- Empty with filters: "No events match these filters in this window."
- Empty without filters: "No market events in this window. The detector runs hourly on the rollup; with little recorded history few events can fire."
- Catalogue failure: "Unavailable" in the sidebar.

#### Refresh
Every 60 s, but only if the user has not paged past the first 100. It re-reads the first page and the histogram, and keeps the current feed on error.

#### Observations
- Type tags like "ATL", "ATH", "CAPACITY", "COVERAGE" are trader shorthand.
- Severity labels are uppercase in the filter ("NOTABLE+") but lowercase in the legend, and other pages use "Notable+" (GPU page). This is inconsistent.
- Event rows rely on hover tooltips for detail (cause and reason), which touch users cannot see.
- The "Events per day" chart counts at most the newest 1000 events, so long windows can be truncated (it says so in small text).
- The sidebar catalogue repeats the type select's options.

---

### /news (news and signals)

- **File:** web/pages/news.js. Nav: "News" (under Markets).
- **Who and access:** public (`/v1/news`, `/v1/news/topics`, `/v1/news/sources`, `/v1/news/families`, `/v1/timeline`).
- **Purpose:** GPU compute news, clustered into stories, tagged with the GPUs, providers, regions and topics it mentions, with an optional timeline view against prices.

#### Layout, top to bottom
1. **Header.** H1 "News & signals". Subtitle: "GPU-compute news, story-clustered, tagged with the GPUs, providers and regions it mentions. Related to market moves by time and entity only, never presented as a cause." Right side: view segmented control "Feed" (default) / "Timeline".
2. **Filter bar:**
   - "GPU" text input with suggestions (families such as "H100" or "Blackwell", plus GPU slugs), placeholder "GPU or family". Applied on Enter or change.
   - "Provider" select (first option "any").
   - "Region" select ("any", "US", "Canada", "Europe", "UK", "APAC", "Middle East", "LATAM", "Africa").
   - "Source" select ("any" plus enabled sources).
   - "Min rel." segmented control: "all", "20" (default), "40", "60".
   - "Search" input, placeholder "words in title / summary".
   - The button "Clear" (only when a filter is set).
3. **Topic chips:** one toggle chip per topic with items in 30 days ("pricing 12"). The tooltip gives the description, weight and count.
4. **"Link to this view:"** followed by the current URL as a link (only when filters are active).
5. **Two columns.**
   - **Left (Feed view):**
     - A count "N of M stories".
     - Story cards. Each card has:
       - A relevance badge (number plus meter; hover or focus opens a popover with "Relevance N / 100", "entity points", "topic points", "source tier ×", "recency ×", the formula, "topics from", "terms" and "methodology →").
       - The title (external link).
       - The summary.
       - A meta line: "3h ago", source name (filters by source), source tier, "+N sources: A, B" (external links), and entity pills (GPU links, family pills, provider pills, region pills, and topic buttons that apply the topic filter).
     - Low-relevance stories are styled differently.
     - Below: the button "Load N more".
   - **Left (Timeline view):**
     - Controls: "GPU variant" select ("choose a GPU…") and "Window" segmented control "24H", "7D" (default), "30D", plus "provider: X" when a provider filter is set.
     - Without a GPU: "Pick one GPU variant: prices are per canonical variant, so a family (e.g. “H100”) cannot be charted."
     - With a GPU:
       - A bold "Related, not necessarily causal." line plus the API note.
       - A label "{GPU} · market lowest and median provider price · N hourly points".
       - A price chart (260 px, "Lowest" plus dashed "Median provider", with markers for events, moves, availability changes and news).
       - "Available listings (observed)" chart (110 px, "In stock" and dashed "Sold out").
       - A "Markers in this window (N)" list (colour swatch, time, kind, label; up to 30).
     - With fewer than 2 hours: "History building".
   - **Right:**
     - **"Source health":** "N enabled · N ok · N not ok", then a table with columns "Source", "Health" (ok dot or badge), "Ok" (age or "never"), "24h" (new items). Clicking a row toggles that source filter.
     - **"Topics · 30d":** a bar list of topics with counts (clicking toggles the topic), and the note "Topics and relevance by deterministic rules." plus "Methodology →".

#### States
- Loading: "Loading news…".
- Empty: "No stored news matches these filters."
- Topics or sources unavailable: n/a.
- Errors with "Retry".

#### Refresh
Every 5 min the feed re-reads (Feed view only, and only while on the first page). The timeline view does not auto-refresh.

#### Observations
- The H1 "News & signals" doesn't match the nav label "News".
- Seven filters plus topic chips plus a right-side topic list: the topic filter appears in three places (chips, card buttons, sidebar).
- The "Source health" panel is an operational detail on a public reader page.
- "Min rel." and the relevance score popover (entity points, tier multiplier, recency multiplier, formula) are heavy for a news reader.
- The GPU input accepts families in Feed view, but Timeline requires a variant. The field changes meaning between views.
- The "Link to this view:" line duplicates the browser address bar.

---

### /heatmaps

- **File:** web/pages/heatmaps.js. Nav: "Heatmaps" (under GPUs).
- **Who and access:** public (`/v1/heatmaps/{kind}`).
- **Purpose:** matrices showing where each GPU is cheap, expensive, available or moving, across providers, regions or days.

#### Layout, top to bottom
1. **Header.** H1 "Heatmaps". Subtitle: "Where each GPU is cheap, dear, available or moving, across providers, regions and days. Hatched = no data, not zero." Right side: "Order" control, which contains a toggle chip "Empty rows" (show GPUs whose every cell is empty) and a segmented control "Coverage" (default), "Name", "Value".
2. **Control bar:**
   - Group tabs: "GPU × provider", "GPU × region", "GPU × day".
   - Kind segmented control for the current group:
     - Provider: "Premium vs market", "Availability", "24h change".
     - Region: "Cheapest", "Premium vs global", "Priced listings".
     - Day: "Volatility", "Availability", "Daily change".
   - On the right, either "Window" "7D", "14D", "30D", "90D" (for kinds with a time window; default 7D, or 30D for the day group) or the grey text "current snapshot".
3. **Stat strip:**
   - "GPUs".
   - "Providers" / "Region groups" / "Days".
   - "Cells with data" ("34%", sub "N of M").
   - "No comparison" (GPUs with at most 1 cell; not shown for the day group).
   - "Median coverage" / "Median regions".
   - "Widest spread" (GPU link, sub "lo → hi").
   - "Basis".
4. **Legend line:**
   - "diverging" or "sequential".
   - "**{kind label}:** {what a colour means}", for example "Blue = cheaper than the rest of the market, red = dearer."
   - Unit, definition, minimum hours, and the colour clip value.
   - "Hour {time}".
   - "Methodology →".
   - If rows were hidden: "N GPUs with no cell hidden ...: list ... Show them".
5. **Heatmap.** Rows are GPUs (row labels link to `/gpu/{slug}`); columns are providers (links to the provider page), region groups, or days. Null cells are hatched. Hover a cell for its value. Clicking a cell opens `/provider/{p}?gpu={slug}` (provider kinds) or `/gpu/{slug}` (other kinds).

#### States
- Loading: "Loading matrix…". Error with "Retry".
- Empty: insufficient box "Empty matrix" ("No cells: nothing is priced for this view right now." or, for days, "No GPU has a full day in the daily summaries for the last N days yet ...").

#### Refresh
Every 3 min.

#### Observations
- The "Order" controls and the "Empty rows" toggle sit in the page header, separate from the other controls in the bar below. This is odd placement.
- Nine heatmap kinds across two levels of tabs. Terms such as "Premium vs market" and "Premium vs global" are finance jargon.
- The colour meaning changes per kind (diverging blue/red vs sequential, and "Brighter = cheaper" for Cheapest, which is inverted). The legend sentence carries all of that.
- Clicking a cell in the region or day views opens the GPU page, not a filtered regional or daily view.
- "Widest spread" compares raw cell values, which have different units per kind.

---

### /indices (index board)

- **File:** web/pages/indices.js. Nav: "Indices" (under Markets).
- **Who and access:** public (`/v1/indices`, details, history, fixings).
- **Purpose:** a board of every OpenGrid price index (composite, per GPU, spot, regional, provider class), with levels and changes.

#### Layout, top to bottom
1. **Header.** H1 "Indices". Subtitle: "Every OpenGrid index: chain-linked, one vote per provider, published only with enough constituents". Right side:
   - "Family" segmented control: "All", "Composites", "Per-GPU", "Spot", "Regional", "Provider class".
   - "Status" segmented control: "All", "Published", "Unpublished".
   - Search input, placeholder "filter: h100, us, spot…".
   - The button "Methodology".
2. **Stat strip:**
   - "GPU Compute Index" (level plus 24h change, observed badge, sub "24h change").
   - "7d" (when it exists).
   - "Indices" (N, sub "P published · U not").
   - One cell per family: "Composites", "Per-GPU", "Spot", "Regional", "Provider class" (each "published/total", sub "published").
   - "Index hour" (time, sub "date · v{methodology version}").
3. **Hero cards.** One card per composite index, linking to its detail page. Each shows the name, a grey id, the level plus 24h change, a 7d sparkline, and "N components · 7d +x%" (or "not published · N components published"). The main "gpu-compute" card is styled larger. Unpublished cards are dimmed.
4. **One table per family** (in the order Composites, Per-GPU · on-demand, Spot · interruptible, Regional, Provider class). Each has a description line above it and is titled "{family} · P/N published", with CSV. Columns:
   - "Index" (name link plus mono id).
   - "Level" (composites) or "$/GPU·h" (unpublished shows the last level dimmed with "*").
   - "24h", "7d", "30d", "90d", "YTD", "All".
   - "High".
   - "Low".
   - "Vol 30d".
   - "Comp." or "Prov." (constituents; tooltip "Included providers this hour (3 needed)").
   - "7d" sparkline.
   - "Status" (green "published", or "not published" plus a short reason such as "2/3 providers").
   
   Cells show "·" while the extras are still loading. Clicking a row opens `/indices/{id}`. Unpublished rows are greyed.
5. **Note:** "observed market prices ... never quotes or execution prices. Changes are measured from the current published level; a change is n/a (hover) when history does not cover the window or the chain was rebased. * = last published level ...", and "How indices are computed →".

#### States
- Loading: "Loading indices…".
- Empty: "No index matches these filters." or "No index has data yet: indices compute hourly from the market rollup once listings are recorded."

#### Refresh
Every 5 min.

#### Observations
- There are two columns labelled "7d" in the same table (the change column and the sparkline column).
- Many indices show "not published" with technical reasons ("2/3 providers", "chain rebased in window", "history shorter than window").
- Vocabulary: "chain-linked", "constituents", "one vote per provider", "rebased", "Comp.", "Prov.". Very finance-heavy for GPU buyers.
- Up to 5 stacked tables with 15 columns each.
- The "*" convention for last published levels is explained only in the bottom note.

---

### /indices/{id} (one index)

- **File:** web/pages/indices.js (detail route). Nav highlight: "Indices".
- **Who and access:** public. The "Verify" button calls `/v1/fixings/{id}/verify` (public area "fixings"). Evidence links open raw JSON.
- **Purpose:** one index's level, history, publication record, definition, constituents and, for settlement indices, official fixings.

#### Layout, top to bottom
1. **Header.** Eyebrow with the family label, the index name (H1), and a mono id. Subtitle: the definition description (or for spot: "Interruptible capacity: spot instances can be reclaimed by the provider."). Right side:
   - "Window" segmented control: "24H", "7D", "30D" (default), "90D", "YTD", "ALL".
   - "Res" segmented control: "AUTO", "1H", "1D".
   - Toggle chip "raw cross-section" (not on composites; overlays the unchained statistic).
   - The button "{GPU} market →" (per-GPU indices only).
   - The button "Methodology".
2. **Not-published banner** (only when unpublished): insufficient box "Not published" with the reason and "last published $X at {time}".
3. **Stat strip:**
   - "Level" (sub "$/GPU·h · {hour}" or "points · {hour}").
   - "24h", "7d", "30d", "90d", "YTD", "All-time" (folded when empty; sub "from $X").
   - "High".
   - "Low".
   - "Vol 7d" and "Vol 30d" (folded when empty).
   - "Constituents" or "Components" (sub: method name).
   - "Coverage 30d" (percentage, sub "N/M h published").
4. **Two columns.**
   - **Left:**
     - Level chart (300 px). Unpublished hours are gaps (hover gives the reason), and "Chain rebased" markers appear on the axis.
     - Under it, a **publication strip**: green and red bars for published and unpublished runs (hover gives the time range and reason), with a legend "published", "not published", "N/M hours published".
     - A hover readout line ("hover the chart for each hour's publication status", which turns into "{time} published $X  N constituents, M linked" or "not published: reason").
     - Note: explains chain-linking, the number of unpublished hours, rebases, and isolated points.
   - **Right:**
     - **"Definition":** a description paragraph, then key-value rows "Index id", "Family", "Segment", "Unit", "GPU", "Region group", "Provider class", "Price concept", "Statistic", "Methodology" (link plus version), "First published", "Chain segment". Then "Latest hour: {note}".
     - **"Related":** a link to the GPU market and component index links, then "Other indices on this GPU" (name, level or "not published", 24h change).
5. **"Constituents now"** (or "Components now" for composites).
   - Per-GPU indices: a table titled "N/M providers included · as of {time} · statistic: median", with columns "Provider", "Class", "Region", "Vote $/GPU·h", "Included" ("included", "excluded: feed down", "excluded: outlier (> 3× from median)", ...), "Weight in stat" ("50%", "trimmed", "0%"), and "Recorded since". Note explains one vote per provider, the exclusion rules and that weights are derived.
   - Composites: columns "Component index", "Weight", "Level $/GPU·h", "Status", "Chain seg.". Note on the 50% weight rule.
   - Empty: "No constituent recorded for the latest stored hour."
6. **"Settlement fixings"** (shown only for settlement indices):
   - Stat strip: "Daily settlement" ("TWAP · {UTC time}"), "Daily status" (badge "final", "restated", "not published" or "provisional"), "Latest hourly fixing", "Hourly status".
   - Table "Official fixings · N daily, M hourly (latest, effective)" with columns "Fixing time" (UTC), "Type" ("daily" bold or "hourly"), "Value", "Status" (plus "correction"), "Flags" (badges), "Method v.", "Evidence" (a mono id link that opens the JSON in a new tab), and a "Verify" chip. "Verify" shows "verifying…" and then an "ok" or "mismatch" or "error" badge.
   - Note with links "all restatements" (raw JSON) and "Settlement methodology →".
   - Empty: "No fixing taken yet: an hour is fixed only once the rollup can no longer revise it (≈5 h)."

#### States
- Loading: "Loading index…".
- Error (for example an unknown id): H1 "Index {id}", an error box, and the link "All indices →".
- Chart empty: insufficient box "No published level" ("none of the N stored hours in this window is published" / "no stored level in this window").

#### Refresh
Every 5 min it re-reads the index (redrawing constituents only) and the history. The stat strip, definition and fixings are not redrawn, so the strip can go stale while the chart updates.

#### Observations
- The densest page for a non-specialist: TWAP, fixings, restatements, chain segments, rebases, raw cross-section, trimmed mean and weight-in-stat all appear.
- "Res" ("AUTO", "1H", "1D") and "raw cross-section" are expert controls placed next to the main window control.
- The stat strip does not refresh on the 5 min cycle, but the chart and constituents do, so they can disagree.
- Evidence links and "all restatements" open raw API JSON in a new tab, with no human-readable view.
- Mixed time zones: fixings are in UTC, everything else is in local time.

---

### /ops (Ops · data quality)

- **File:** web/pages/ops.js. Nav: "Ops" in the bottom utility area, **visible to everyone** (alias "Feed health status").
- **Who and access:** operator only in practice. Every read is `/v1/ops/*`, which is a private API area (admin scope, see api/ops.py). The page shell loads for anyone (`/ops` matches the public page list). An anonymous visitor gets 401 on every call:
  - "Status matrix" and "Feed health" show the error box "Sign-in required" with "Sign in" and "Retry".
  - The other boxes show errors.
  - The header shows "refresh failed {time}".
  - The browser may also show Basic-auth prompts.
  - PAGES marks it non-indexable.
- **Purpose:** an internal terminal for data quality: feed health, held (quarantined) values, incidents, schema changes, unmapped GPUs, stale listings, jobs, routing probes and news ingestion. It is also the one page in this scope with write actions (Accept or Reject quarantined values).

#### Layout, top to bottom
1. **Header.** H1 "Ops · data quality". Subtitle: "Internal: feed health, held values, incidents and pipeline jobs. Every figure is read from the quality layer; nothing is estimated." Right side: "Provider" select ("All providers" plus providers), an update stamp ("updated HH:MM · auto 30s" or red "refresh failed ..."), and the button "Refresh" (forces an uncached reload).
2. **Stat strip:**
   - "Feeds" (healthy / degraded / down, coloured).
   - "Quarantine" (sub "values held, pending").
   - "Open incidents" (sub: top kinds).
   - "Fetch errors 24h".
   - "Schema changes" (sub "last 7 days").
   - "Stale listings".
   - "Unmapped GPUs" (sub "raw names, no canonical").
   - "Jobs" (ok/total, sub "N failing/overdue").
3. A dismissable flash message (after decisions) with a "×" button.
4. **"Status matrix"** (sub "provider × check · hover a cell for detail · click a row to filter the page"). A grid of providers by checks: "Feed", "Last OK", "Consec. fail", "Fail 24h", "Errors 24h", "p95 lat.", "Listings Δ24h", "Quarantine", "Schema 7d", "Stale", "Incidents". Cells are coloured ok, warn, bad, na or info; headers have tooltips. Clicking a row filters the whole page to that provider (clicking again clears it). Legend chips "ok", "attention", "failing", "no data", plus a sentence naming the unhealthy providers.
5. **Two columns:**
   - **"Feed health"** (per provider, last 24h): table with columns "Provider", "Status", "Source", "Last OK", "Consec", "Fail%", "p50", "p95", "Now", "24h ago", "Last failure".
   - Right stack:
     - **"Source latency"**: dot plot of p50 and p95 per provider, plus a note.
     - **"Provider API failures"**: table with columns "Provider", "Endpoint", "Fails", "HTTP", "Last", "Last error"; empty: "No failed fetch in the last 24h."
     - **"Anomalies"**: rows "Duplicate explosions", "Listing collapses", "Missing regions", "Suspicious price moves", "Instance price mismatch", each with a count, a description and a "show →" link that jumps to and filters the quarantine or incidents list.
6. **"Quarantine queue"** (sub "held values: the listing keeps its last good value until a decision"):
   - Table titled "N pending holds". Toolbar: an optional "rule: X ×" chip and a status segmented control "Pending" (default), "Accepted", "Rejected", "Auto", "Superseded", "All".
   - Columns "#", "Provider", "Listing", "Field", "Rule", "Last good → held", "Seen", "Auto", "First", "Last", "Status", "Decision".
   - "Decision" has buttons "Accept" and "Reject". Clicking one opens inline confirmation: "Apply this value?" / "Reject this value?", a note input "note (optional)", "Confirm accept" / "Confirm reject", and "Cancel". The result appears as a flash.
   - Note: "Accept applies the held value to current state now ... Both are recorded with your identity." Link "Lifecycle →".
   - Empty: "Nothing held: no value is awaiting a decision."
7. **"Incidents"**: table titled "N incidents · M major · K notable". Toolbar: a kind select ("All kinds" plus kinds) and a status segmented control "Open" (default), "Resolved", "All". Columns "Sev", "Kind", "Provider" (or "system"), "Listing / endpoint", "Detail", "Count", "First", "Last", "Status". Empty: "No incidents match."
8. **Two columns:**
   - **"Unmapped GPUs"** (sub "the mapping to-do", with a "JSON →" link): columns "Provider", "Raw GPU name", "Why". Note about canonical.py.
   - **"Stale listings"**: titled "N aging/stale · K stale", with columns "Provider", "GPU", "Region", "Age".
9. **"Schema changes"**: columns "Sev", "Provider", "Endpoint", "Added", "Removed", "Retyped", "Paths", "When", "Status".
10. **Two columns:**
    - **"Background jobs"** (sub "jobs.JOBS on this server process"): columns "Job", "Status", "Every", "Runs", "Fails", "Last run", "Took", "Last error". Note when nothing has run.
    - **"Routing & provisioning"**: a probe table with columns "Table", "Rows", "24h", "Failures 24h", "Recent errors". Links "Deployments →" and "Routing methodology →".
11. **"News ingestion"**: titled "N sources · N failing/degraded · N never fetched", with columns "Source", "Category", "Tier", "Health", "Last OK", "Consec", "Fetch 24h", "New 24h", "Stored", "Avg", "Last error".
12. **Footer note:** links "data quality" and "data trust", and "Auto-refreshes every 30 s while this tab is visible."

#### Refresh
Every 30 s, an uncached refresh of every section. An open quarantine confirmation is never wiped by the refresh.

#### Observations
- The "Ops" link is shown to every visitor in the main nav even though the page is useless without operator credentials. The "Admin" group is hidden for non-admins, but "Ops" is not.
- For anonymous users the page renders a dozen error boxes and may trigger browser Basic-auth dialogs. There is no single friendly "operators only" message.
- Extremely dense: about 14 sections and 10 or more tables. Internal identifiers appear in the UI ("jobs.JOBS", "canonical.py", "quality/checks.py", "raw_snapshots", rule codes).
- It is the only page in this scope with write actions. The confirmation is inline, which is good, but sits in a narrow table cell.

---

### market.js (no URL of its own)

- **File:** web/pages/market.js. It registers **no route**. It only defines shared helpers: `OG.views.windowSeg` / `OG.views.hours` (a "1H", "6H", "24H", "1W", "ALL" range control that remembers the choice in localStorage and `?h=`) and `OG.views.marketBoard` (a dense table of every GPU from `/market`).
- Its header comment says it is "Used by /gpus", but no page calls these helpers (a search of web/ finds no other reference). /gpus builds its own table. This looks like leftover code from the first UI.

### overview.js (serves /markets)

- **File:** web/pages/overview.js. It registers **`/markets`** (title "Markets", nav "Markets"), the market overview dashboard with an indices strip and panels such as "Top gainers · 24h", "Top losers · 24h", "Unusual moves", "Most volatile · 7d", "Widest spreads", "Most liquid / covered", "Availability gaining · 24h", "Availability losing · 24h", "Newly available · 7d", "Sold out everywhere", "Recent provider price changes" and a "Market board · on-demand" table. That page is documented in detail elsewhere.

---

### Cross-page observations (for a reviewer)

1. **Access mismatches.**
   - (Fixed 2026-10-15) `/compare` (GPU and provider modes) depended on `/v1/compare`, which was not public.
   - `/provider/{name}` "Source field map" depends on `/mapping`, which is not public and fails silently.
   - `/ops` is linked for everyone but works only for operators.
   - "Route" buttons and links (GPU page, family page, opportunities) lead to the private `/route`.
   - Non-public calls answer with a Basic-auth challenge, which can pop native browser dialogs.
2. **Inconsistent time-window controls.**
   - GPU page: "24H", "7D", "30D", "90D", "ALL" (default 7D).
   - Family page: no "ALL".
   - Compare: "7D", "30D", "90D" (default 30D or 7D depending on mode).
   - Provider: "7D", "30D", "90D" for stats plus "24H", "7D", "30D" for its chart.
   - Events: "24H" to "ALL".
   - Indices: adds "YTD".
   - Heatmaps: "7D", "14D", "30D", "90D".
   - Severity labels: "Notable+" vs "NOTABLE+".
3. **Labels differ between nav and H1.** "Opportunities" vs "Opportunity monitor", "News" vs "News & signals", "GPUs" vs "GPU markets", "Events" vs "Market events".
4. **Jargon density.** Spread, efficiency, fragmentation, premium, one vote per provider, eligible, chain-linked, rebased, constituents, TWAP, fixings, ·mock, L0 to L3, IQR, CV. The definitions mostly live in hover tooltips and methodology links, which don't help touch users.
5. **Column overload.** Main tables carry 14 to 21 columns of abbreviations. The auto-hide footers ("No data yet:", "Hidden at this width:") help but add more text.
6. **Click behaviour varies.** Rows navigate on most tables, select a chart series on `/provider` catalog and `/compare` common GPUs, open a panel on GPU moves, and do nothing on `/explorer`.
7. **Refresh rates vary by page and even by section** (30 s, 60 s, 2 min, 3 min, 5 min, never for `/compare`), so parts of the same page can be out of sync (GPU page news and listings, family table, index stat strip).
8. **Honesty notes everywhere.** Nearly every section ends with a caveat paragraph. It is principled, but visually repetitive.
9. **Internal and operational information on public pages.** Feed health, source field maps, quarantine counts, "no API key configured here", and source health on /news.
10. **Unused code.** market.js `marketBoard` is unused.

---

# Part 3: Customer journey (Find Compute, Jobs, Keys, Watchlists, Assistant)

Scope: /find, /jobs, /jobs/new, /jobs/{id}, /keys, /watchlists, plus the shared assistant panel (assistant.js) as it appears on /jobs and /find. Everything below is read from the source (web/pages/*.js, web/core.js, api/pages.py, assistant/engine.py). It describes what the code does, not what a design doc intends.

### Background that applies to every page

- The app is a single page app. Each page draws a header block (`OG.head`): a title, a grey subtitle line, and buttons on the right.
- Left navigation (web/index.html): "Find Compute" is a top-level item; "My Jobs" is a top-level item with a sub-menu containing Deployments, Reserve, Watchlists and Route; "API keys" sits under Developers.
- Sign-in model. In production `PUBLIC_PAGES` is on: anyone can open every page shell. The data APIs for jobs, keys, watchlists, alerts, usage, billing, credentials, route and deployments are private (`PRIVATE_V1` in api/pages.py). They need either the operator login (the browser site login, reached via `/login?next=<current page>`) or an API key. Three POSTs are public: `/v1/recommend`, `/v1/recommend/estimate` and `/v1/assistant`.
- The generic error box (`OG.error`) shows a bold title ("Sign-in required" for 401, "Not allowed" for 403, "Not found" for 404, otherwise "Could not load"), the error message, a "Sign in" button for 401 (goes to /login and comes back), and sometimes a "Retry" button.
- Confirmation dialogs (`OG.dialog`) are modal with a title, body, a "Cancel" style button and a confirm button. For anything that spends money or destroys something, some dialogs add an acknowledgement checkbox that must be ticked before the confirm button is enabled. Escape or clicking outside closes the dialog (counts as cancel).
- Auto-refresh timers (`ctx.every`) pause while the browser tab is hidden and stop when you navigate away.
- Data badges: "observed" (a price read from a provider), "estimated" (a model or assumption), "transaction" (what was actually quoted or paid). Each badge links to /methodology/data-kinds. "n/a" placeholders carry the reason as a hover tooltip.

---

### /find (Find Compute)

**URL:** `/find`. Query parameters: `task, model, method, prec, quant, ctx, conc, seq, mbs, budget, region, maxp` (form fields), `a.<field>=value` (answers to follow-up questions), `mode=paste`, `req=<json>` (pre-filled paste), `view=table`, `ask=1` (opens the assistant panel).

**Who / sign-in:** Anyone. Fully public: the recommendation call is a public POST. The subtitle says "Free, no sign-in."

**Purpose:** Describe an AI workload and get a GPU memory estimate plus ranked GPU options priced from observed market data.

#### Layout, top to bottom

1. **Header.** Title "Find Compute". Subtitle: "Describe your workload. OpenGrid estimates the GPU memory it needs and ranks the GPUs that fit, priced from observed market data. Free, no sign-in." Right side buttons:
   - "Ask in plain English" (only if the assistant script is loaded, which it is in production). Toggles the assistant panel open and closed below the strip, and sets or clears `?ask=1`.
   - "Methodology" links to /methodology.
2. **"START HERE" strip** (three journey tiles, the current one highlighted). Also reused on other pages.
   - A "Find the right GPU": "Describe your model: get a memory estimate and the GPUs that fit, with observed prices." Links to /find.
   - B "Train an example model" with badge "free simulation": "Walk a real training job end to end on the PyTorch MNIST demo." Links to /jobs/new?preset=mnist&sandbox=1.
   - C "Bring your own project" with badge "recommendation only": "Run opengrid analyze . on your code and paste the result here. Recommendation only." Links to /find?mode=paste.
   - Tile A is highlighted in Guided mode, tile C in paste mode.
3. **Assistant panel** (hidden until "Ask in plain English" is pressed). See the "Assistant panel" section at the end. On /find it uses the default title "OpenGrid Assistant", the default note "Answers use OpenGrid's live market data; estimates are labelled. It cannot launch or buy anything." and the default example chips (for example "Cheapest available H100", "What GPU should I use to fine-tune Llama 3 8B with LoRA?", "Compare H100 SXM and H200", "Specs of the L40S").
4. **Two-column grid:** the input box on the left, results on the right (on screens up to 1100 px wide, the page scrolls to results when a search starts).

#### Input box: tabs "Guided" and "Paste from the CLI"

**Guided tab** (a form; Enter or "Find GPUs" submits):
- **1 "What are you doing?"**: four radio cards: "Train a model from scratch" (Pre-training or training your own architecture), "Fine-tune an existing model" (Adapt Llama, Mistral, Qwen… to your data), "Run inference" (Serve or batch-run a model), "Not sure yet" (We'll ask what matters). Picking one redraws the form (later steps change) and updates the URL.
- **2 "Which model?"**: hint "A name, a Hugging Face id, or just its size." A text box (placeholder "e.g. Llama 3.1 8B, Qwen/Qwen2.5-7B or 7B") with an autocomplete list of 7 models, and five quick chips: "Llama 3.1 8B", "Llama 3.1 70B", "Mistral 7B", "Qwen2.5 7B", "Qwen2.5 14B". A live note under it says one of: "Leave blank if you don't know yet.", "<hf id> · <N>B parameters", "<N>B parameters (no specific model)", or "Sent as the model name; OpenGrid looks it up."
- **3 (only for fine-tuning) "How will you fine-tune?"**: segmented control "Not sure", "Full", "LoRA", "QLoRA", with a hint line explaining the choice (default hint "LoRA and QLoRA need far less memory than full fine-tuning.").
- **3 (only for inference) "How will you serve it?"**: hint "Longer context and more parallel requests need more memory (KV cache)." Fields "Context length (tokens)" (e.g. 8192) and "Concurrent requests" (e.g. 8).
- **"Budget and location"** (numbered 3 or 4): hint "All optional. Options over your max price are left out." Fields "Total budget ($)" (e.g. 50), "Max $ per GPU-hour" (e.g. 3), "Region" select ("Any region", "United States", "Europe").
- **Fold "Advanced: precision, quantization"** (plus ", batch" for training or fine-tuning). Opens by itself if any of these are set. Selects "Precision" (Not sure, fp32, bf16, fp16, fp8, int8, int4) and "Quantization" (Not sure, none, int8, int4, nf4 (bitsandbytes), GPTQ, AWQ, fp8). For training or fine-tuning also "Sequence length" and "Micro batch size". Hint: "Unknown is fine: OpenGrid states what it assumed."
- Buttons: **"Find GPUs"** (primary, runs the recommendation) and **"Reset"** (clears the form and answers and shows the intro again).
- **Fold "What will be sent"**: "POST /v1/recommend. Only this JSON leaves your browser." followed by the live JSON body (or "(fix the input first)").
- Inline field errors appear under number fields as you type (for example "Context length must be a whole number", "Max price must be at most 1,000").

**Paste from the CLI tab:**
- A "Bring your own project" box with three numbered steps: (1) run `opengrid analyze . --json` in your project folder, with a "copy" button, and a note that the analyzer reads only allowlisted files and never runs code or reads secrets, weights or datasets; (2) "Paste its output below. Only the requirements part is sent."; (3) "Using a coding assistant? The OpenGrid MCP server runs the same analysis." with a "Developer docs →" link to /developers. Then a note: "OpenGrid recommends GPUs for any workload but only launches its PyTorch MNIST demo today: your own project gets a recommendation, not a launch."
- Textarea "Analyzer output (JSON, up to 64 KB)".
- A live status line checked 150 ms after typing stops: empty: "Paste the JSON to check it. Nothing is sent until you press the button."; invalid: "Can't use this yet: N problems" with a list of technical errors (for example "training.method: \"x\" is not one of ..."); valid: "Looks good" plus "From opengrid analyze vX · N files inspected · M skipped. Only the requirements are sent; file names and paths stay in your browser.", skipped files and warnings.
- Buttons **"Get recommendation"** (disabled until the JSON is valid) and **"Clear"**.
- The same "What will be sent" fold.

#### Results column

- **Before any search (intro):** "What you'll get" with three numbered points (A memory estimate, Three picks, Alternatives), two grey lines, and a link "Train the example model (free simulation) →" to the demo.
- If the URL already carries form values or a pasted request, the search runs automatically on load.
- **Loading:** spinner "Estimating memory and ranking GPUs that fit…".
- **Input errors:** a warning box "Check your input" with up to 8 errors.
- **Server errors:** a box titled "The server could not use this input" (422), "Request failed (HTTP N)" or "Network error", the server's field errors, and a "Try again" button. If `/v1/recommend` is missing (404 or 405) the page falls back to `/v1/recommend/estimate` and shows the estimate only with a notice "Market options not available".

**The recommendation screen** (also reusable by other pages):
1. Top line: "Recommendation" eyebrow, a workload type badge (for example "fine tuning"), "market data as of <time>", and "How this works →" (methodology link).
2. **No GPU case:** if the server says no GPU is needed: heading "You probably don't need a GPU", the reason, advice to run on a laptop or CPU server, and buttons "Describe a different workload" and "Try the training demo". Nothing else is shown.
3. **Estimate box:** eyebrow "Estimate", "estimated" badge, "confidence high / medium / low / none" badge, and the method name. Big number "GPU memory needed" (for example "18 GB", "recommended, with headroom", or "n/a" plus "not enough information yet"). Sub-line with "Theoretical minimum", "Range", and "Per GPU" when more than one GPU. Facts: "GPUs" (suggested count and minimum), "Runtime" (shows "Unknown" unless the server has a measured benchmark), "Fast interconnect" (Needed / Not needed). A coloured stacked bar "Where the memory goes" with a legend (Model weights, Gradients, Optimizer state, Activations, KV cache, Framework overhead). "Assumptions" list (first 4, rest in a "N more" fold).
4. **"Sharpen the estimate"** box (only if the server returned questions): "Optional. Answer any of these and the recommendation updates." Each question shows its text, a reason in brackets, and the raw field name in small mono text (for example `training.lora_rank`). The answer control is either a row of toggle buttons (enum or yes/no) or a number input with an "Update" button (Enter works too). Answering re-runs the search immediately and writes `a.<field>` to the URL.
5. **Three picks** side by side: "Cheapest advertised option" ("The lowest observed price that fits the memory estimate. The market's answer. OpenGrid may not be able to run it for you."), "Best for your workload" ("Fits with headroom at a balanced price, fresh data. What we'd rent for this job."), "Best OpenGrid can run" ("The best option OpenGrid can actually launch for you today, after approval."). Each shows the server's reason and a full option card, or "Nothing here" plus an explanation. For "Best OpenGrid can run" the empty explanation is usually "OpenGrid can only launch its PyTorch MNIST training demo today. For this workload the options above are a recommendation: you rent the GPU from the provider directly." with a button "Try the training demo (free simulation)", or "Sign in or use an API key: what OpenGrid can launch depends on your account." with an "API keys" button.
6. **"All options · N"** section with a "Cards" / "Table" toggle (saved as `?view=table`).
   - Cards view: grouped into tiers "Lower cost" (Cheapest options that still fit), "Balanced" (Headroom and price in balance), "Higher performance" (Faster or larger GPUs for more money), and "Other", each with a count. Cards here are compact.
   - Table view: columns Tier, GPU, Provider, Region, $/GPU·h, GPUs, Memory fit, Available, Seen, OpenGrid ("can run", "stale", "sold out", "recommendation only"), and small buttons "GPU" and "Route". Sorted by price; has CSV export ("opengrid-find-options.csv").
   - If there are no options: "No GPU listing fits this workload right now. See what was excluded below, or relax your budget / max price."
7. **Fold "Excluded · N"**: table GPU, Provider, Why (code badge), Detail.
8. Server notes as a bullet list, and a footer note: prices are observed list prices, not quotes; memory, cost and fit are estimates; runtime is never guessed; stale or sold-out listings are never shown as deployable.

**One option card** contains: GPU name (with "2×" prefix for multi-GPU) linked to /gpu/<slug>, variant and VRAM, a tier badge (picks only); provider logo and link plus region; price badge "observed", price "$X.XX/GPU·h", "1 GPU" or "N GPUs = $Y/h", a freshness badge (age, coloured fresh / aging / stale), "stale" badge if not fresh, availability badge ("Available", "Sold out", "Availability not reported"); a memory fit bar ("16 GB needed of 24 GB per GPU · 33% headroom", amber under 10% headroom, red "does not fit"); a cost block "Cost if it runs for" (estimated) listing hours and totals, "Your budget buys about N h of runtime", "Not included: ...", "billed per ..."; "Why" and "Limitations" lists (in compact cards these and the cost sit inside a fold "Cost, why and limitations"); a readiness block with a bold label ("OpenGrid can run this", "Recommendation only: OpenGrid can't run this workload yet", "Recommendation only: OpenGrid can't launch this for you yet", "Price is stale, not deployable", "Sold out, not deployable"), a detail line, and an "N reasons" fold; and action buttons "View GPU", "Preview route" (to /route, tooltip "The order ticket: compare executable routes for this GPU (advanced)"), and, on non-compact non-executable cards, "Try the training demo".

#### Assistant on /find
When the assistant answers with a recommendation and the requirements it built, /find switches the input to the **Paste** tab, fills the textarea with the JSON requirements, and runs the search, so the main results column updates to match the chat. The panel's own recommendation card also has "Refine in Find Compute →".

#### Refresh behaviour
No auto-refresh. Results update only on "Find GPUs" / "Get recommendation", on answering a question, or on assistant hand-off. The URL is kept in sync so a result can be shared or reloaded.

#### Observations
- Very dense results: estimate box, questions, three picks (each a full card), then N more cards per tier, excluded table, notes. The same option can appear as a pick and again in the tier list.
- The "Best OpenGrid can run" pick is nearly always empty for real workloads (only the MNIST demo can run), so one of the three headline slots is usually "Nothing here". Many cards repeat "Recommendation only: OpenGrid can't run this workload yet".
- Jargon visible to beginners: "headroom", "KV cache", "micro batch size", "nf4", "GPTQ", "WorkloadRequirements" (in paste errors), raw field paths in questions (`constraints.budget_usd`), "POST /v1/recommend" in "What will be sent".
- "Preview route" and the table "Route" button go to /route, which is a private page: an anonymous visitor (the audience this page invites with "no sign-in") will hit a sign-in wall.
- Assistant hand-off silently flips a Guided user to the Paste tab with raw JSON, which may feel like their form was replaced.
- The "START HERE" strip at the top and the intro panel both offer the demo; tile B links to `/jobs/new?preset=mnist&sandbox=1`, but /jobs/new ignores `preset` and only uses `sandbox=1` to tick a checkbox inside the folded Advanced form (see /jobs/new).
- Two different names for the same three picks: on /find "Cheapest advertised option / Best for your workload / Best OpenGrid can run", in the assistant card "Cheapest that fits / Best for this workload / Launchable through OpenGrid".
- The Guided form numbering skips step 3 for "Train from scratch" and "Not sure yet" (Budget becomes 3), fine, but the step titles are shown as fieldset legends with small numbers that may read as a wizard although it is one long form.
- Changing the region dropdown or typing does not update the URL until a search runs; changing the task does.

---

### Assistant panel (assistant.js), as used on /jobs and /find

**Where:** embedded on /jobs (always shown, as "What do you want to run?") and on /find (behind "Ask in plain English"). It also has its own page /assistant.

**Sign-in:** the panel itself is public (POST /v1/assistant is public and read-only). Questions about your jobs need sign-in: the server answers signed-out visitors with "To see your jobs here you need to be signed in, or to use an API key with the deployments:read scope. Anonymous visitors cannot see any jobs." plus a "Sign in or create an API key" link button to /keys.

#### Layout
- Head row: bold title, a grey note, and a small **"New"** button (clears the conversation and context, shows the example chips again).
- Conversation log (keeps the last 60 messages; user messages and bot replies).
- Example chips (hidden after the first question, shown again after "New"). Clicking a chip sends it.
- Textarea plus **"Ask"** button. Enter sends; Shift+Enter is a new line. Max 2000 characters.

#### A reply
- Spinner "Thinking…", then the reply text, then cards, then a row of link buttons gathered from link cards (for example "Refine this in Find Compute", "Open the guided demo page", "Check price and run on a real GPU", "Your jobs", "Open job-…").
- A small meta line: "deterministic answer" or "phrased by <model>", then " · grounded in the cards above" or " · check the cards".
- Errors: 429 gives "Too many questions in a short time; wait a minute and try again."; 422 gives "That question or conversation is too long. Start a new conversation." (and resets context); other errors use the generic error box.

#### Card types the browser renders
- **Recommendation:** "Memory ≈ N GB · K GPUs · <confidence> confidence" with an "estimated" badge; three picks "Cheapest that fits", "Best for this workload", "Launchable through OpenGrid" (each: GPU link, provider, price "$/GPU-h" with badge, "stale" if stale, reason, small buttons "Market" and "Route preview"; or "none available right now" / "none for you right now"); a top-5 table (GPU, Provider, Region, $/GPU-h, Fits, OpenGrid "launchable" / "preview only"); notes; button "Refine in Find Compute →" (opens /find in paste mode with the requirements).
- **Prices:** table GPU, Provider, Region, $/GPU-h, Available (empty: "No fresh, in-stock listing right now.").
- **Compare / specs:** table GPU, VRAM GB, TB/s, BF16 TFLOPS, FP8 TFLOPS, Low $/GPU-h, Median, Providers.
- **Market context:** a "Correlation, not causation." warning, per-GPU summaries, a moves table (Hour (UTC), Move, %, From, To) and a news list "News in the same window (untrusted public text; not a cause):".
- **Questions:** each follow-up question with clickable chips (for example "$25", "$50", "$100") that send a message.
- **Jobs / job:** a list of your jobs: name linked to the job page, a plain status text, "epoch X of Y", a "simulated" badge, cost ("$X" actual or "up to $X" estimated). "All jobs →" link when there are more. "No jobs." when empty.
- **Action cards** (only two actions exist):
  - **Start demo:** a primary button "Start free simulation" with the note "Simulated: no GPU, no provider, no spend". Clicking it starts the free MNIST simulation **immediately, with no confirmation** (it is free). Then: "Started. " and a button "Watch it run →" (to the job page). On /jobs the job list reloads.
  - **Cancel job:** a red button "Cancel job-xxxxxxxx". Clicking opens a danger dialog titled "Cancel job-xxxxxxxx?" with the server's text (for example "Cancel <name> (<id>)? ...") and buttons **"Keep it running"** and **"Cancel job"**. On confirm: "Cancel requested. " and "Open the job →". On /jobs the list reloads.
  - If not allowed (401 or 403): "You need to be signed in with permission to run jobs (an API key with route:execute). API keys →".
  - The assistant never acts by itself; when several jobs could be cancelled it asks "Which job?" with chips "Cancel job-…".

#### Observations
- The server also sends a "template" card for "run on a real GPU" questions (the MNIST limits), but the browser has no renderer for it, so it is silently dropped; only the reply text and link buttons show.
- On /find the note says "It cannot launch or buy anything", while on /jobs the same assistant can start a simulation and cancel jobs; the two notes are different but both accurate for their context.
- Start demo has no confirmation while cancel does: reasonable (one is free), but the demo button on /jobs starts a job and stays on the page, while the empty-state demo button on /jobs and the one on /jobs/new navigate straight to the job page. Three different post-start behaviours.
- Reply metadata ("deterministic answer · grounded in the cards above") is developer-facing.
- Cancel button labels use the raw job id ("Cancel job-3f2a9c1d") instead of the job's name.

---

### /jobs (My Jobs)

**URL:** `/jobs`.

**Who / sign-in:** People running or watching jobs. The page shell and the assistant box are visible to everyone; the job list needs the operator login or an API key with `deployments:read`.

**Purpose:** See your jobs in plain words and start something new, by typing or by clicking.

#### Layout, top to bottom
1. **Header.** Title "My Jobs", subtitle "Tell OpenGrid what you want to run, or start the free demo", button **"New job"** (to /jobs/new).
2. **Assistant box** (compact panel, see above) with:
   - Title "What do you want to run?"
   - Note: "It can start the free demo or cancel a job only when you click and confirm. It never launches a paid GPU on its own."
   - Placeholder: "e.g. try a free demo · fine-tune Llama 3 8B under $50 · how is my job doing?"
   - Example chips: "Try a free demo", "Train the MNIST example on a real GPU", "Find a GPU to fine-tune Llama 3 8B", "How are my jobs doing?", "Cancel my job".
   - Any start or cancel done from the box reloads the list below.
3. **"Your jobs"** heading with a small **"Refresh"** button on the right.
4. **Job list:**
   - **"Active N"** group (jobs not finished), then **"Finished N"** group (newest 20; if more: "N older jobs are in "Show all details" below.").
   - Each job is a **card that is one big link** to /jobs/{id}: bold name (or "unnamed job") and "<age> ago"; a plain status line with an icon and colour (examples: "● Getting ready", "● Choosing a GPU", "● Waiting for OpenGrid approval", "● Starting the GPU", "● Training", "● Training (simulated)", "● Saving results", "● Shutting down the GPU", "… Cancelling", "! Price quote expired: needs a new quote", "! Needs attention: the GPU may still be billing", "✗ Failed: <reason>", "✗ Failed: shutting down the GPU", "- Cancelled", "✓ Finished", "✓ Finished (simulated)"); a grey line with where it runs ("Simulation, no GPU", "<GPU> on <Provider>", or "GPU not chosen yet") and cost ("free", actual "$X", or "up to $X"); and a small "Open" button look-alike.
5. **Fold "Show all details"** ("(every column: template, state, provider, GPU, cost estimate and actual)"). Inside: a sortable table (newest first) with columns Name, Job (id), Template, State (raw state badge such as "pending approval"), Outcome, Provider ("sandbox" / "not placed"), GPU ("auto"), Created, Runtime, Est. max, Actual, Run ("SIMULATED" or "real" badge). Rows link to the job; rows that may be billing are highlighted. CSV export "opengrid-jobs.csv". A note explains Est. max vs actual and links "Execution safety →".

#### States
- **Loading:** "Loading your jobs…".
- **Signed out (401 or 403):** "Sign in to see your jobs." with a primary **"Sign in"** button (to /login, returns here), "or use an API key with deployments:read.", and "API keys →".
- **Endpoint missing:** "GET /v1/jobs is not available on this server yet." ("Not available yet").
- **Empty:** a box "No jobs yet." with "Watch a small AI model learn to read handwritten digits. It's a free simulation, so no GPU is rented and nothing is charged. It takes about a minute." and a primary **"Start the free demo"** button (free, no confirmation; spinner "Starting the free simulation…", then goes to the new job's page; errors show inline).

#### Refresh behaviour
Reloads the list every 15 seconds (paused when the tab is hidden), on "Refresh", and after assistant actions. The details table redraws when the fold is open.

#### Observations
- With the signed-out state, the assistant box still invites "How are my jobs doing?" and "Cancel my job", which will only answer with a sign-in message.
- Status lines are friendly, but the folded table switches back to raw machine states ("pending approval", "reconciliation pending") and terms like "Est. max", "transaction", "SIMULATED / real".
- The card has an "Open" element styled as a button that is not a separate button (the whole card is the link).
- "New job" in the header and the assistant box both lead to starting things; the page has three entry points for the same demo (chip, empty-state button, New job page).
- There is no cancel or other action on the cards themselves; cancel lives on the job page or in the assistant.
- Finished jobs beyond 20 are only reachable through the folded table.

---

### /jobs/new (New job)

**URL:** `/jobs/new`. Parameters: `?advanced=1` (show the full form directly, no simple view), `?real=1` (highlight the real-GPU choice and run its price check at once; simple view only), `?sandbox=1` (ticks the Sandbox checkbox in the full form), `?template=<id>` (pick a template, default pytorch-mnist), `?preview=1` (auto-run Preview route; full view only). `?preset=mnist` is used in links from other pages but is **not read** by this page.

**Who / sign-in:** Anyone can open it. Starting the demo, checking the price and submitting all need the operator login or an API key (route:preview to preview, route:execute to submit). Admin users see extra admin hints.

**Purpose:** Start a job: the free simulation, the MNIST example on a real GPU with a hard cost cap, or get pointed to Find Compute for your own model.

#### Simple view (default)
1. **Header.** Title "New job", subtitle "Pick one. Nothing is charged without your confirmation and OpenGrid's approval.", button **"← My Jobs"**.
2. While account and templates load: "Loading…".
3. **Three choice boxes:**
   - **"FREE · SIMULATED" / "Try the demo":** "Watch a small network learn to read handwritten digits (PyTorch MNIST). No GPU is rented and nothing is charged. About a minute." An **"Epochs"** number box (default 3, bounds from the template, 1 to 10), and a primary **"Start free demo"** button. Free, no confirmation: shows "Starting…" then opens the new job page; errors appear inline.
   - **"REAL GPU · $2.00 MAX" / "Run the demo on a real GPU":** "The same MNIST job on a rented GPU, with a hard cap: max $2.00 total · stops after 30 min. OpenGrid approves the price before it launches." (the numbers come from the preset). Button **"Check price"**: free (runs a preview; never calls a provider). Spinner "Finding the best GPU OpenGrid can run this on…". Results:
     - Ready: "Ready: 1× <GPU> on <Provider> · $X/GPU·h · max $2.00 total · stops after 30 min", a primary **"Run it…"** button, and "You confirm the maximum cost on the next screen. OpenGrid approves the exact price before anything launches." "Run it…" opens the submit confirmation (see below). This is the money path.
     - Not available: "Not available right now. " plus one plain sentence, for example "Real GPU runs aren't switched on for this OpenGrid yet. That's a setup step on OpenGrid's side, not something you did." or "No GPU fits the price cap right now. Prices change often, so try again later." or "The cheapest GPU right now would cost more than the cap." Admins also see "For admins: <technical message> <admin action link>". Then "Nothing was rented or charged. The free demo works any time." and a small **"See every reason"** button that opens the Advanced fold and scrolls to it.
     - Failure (for example signed out): "Could not check right now. Open Advanced details below for the full error."
   - **"YOUR MODEL" / "Run your own model":** "OpenGrid can't run your own code yet. It can tell you which GPU fits your model and where it's cheapest." Button **"Find a GPU for my model"** (to /find).
4. **Fold "Advanced details"** ("(the full job form: route preview, execution readiness, limits, SSH access)"). Contains the whole full view below, including its own header.

#### Full view (Advanced fold, or the whole page with ?advanced=1)
**Header:** "New training job", subtitle "Allowlisted templates only · preview is free and never calls a provider · a real run waits for OpenGrid approval", buttons **"← My Jobs"** and **"Execution safety"**.

**Left: the job form** (eyebrow "Job request"):
- Button **"MNIST preset · founder's defaults"** (tooltip lists the defaults: opengrid-first-training-test, 1 GPU auto, US, BALANCED, on-demand, ≤ $3/GPU·h, ≤ $2 total, 30 min). Refills the form, keeping the Sandbox tick.
- **Name** (default "opengrid-first-training-test"; lower case a-z, 0-9, ".", "_", "-", 1 to 64 characters).
- **Job type** select: "Training"; disabled options "Fine-tuning (not yet)", "Inference (not yet)", "Batch compute (not yet)", "Custom container (not yet)". Hint "training only in this release".
- **Template** select (from the server, else a built-in "Train Your First AI Model: PyTorch MNIST"). An info line: framework, dataset, model, requirements ("needs NVIDIA CUDA, ≥ N GB VRAM, compute X+, on-demand only"), "expected N-M min (estimate)", and a warning if templates could not be loaded ("templates unavailable: built-in defaults").
- **"Training config":** Epochs, Batch size, Learning rate, Seed, each with "min to max · default N".
- **"Resources":** GPU (text, "auto"; hint "auto = every GPU meeting the template's requirements"), GPU count (hint "the template runs on 1 GPU"), Region select ("Any region", US, Canada, Europe, UK, APAC, Middle East, LATAM, Africa) with a **"Strict"** checkbox (disabled for any region) and a hint explaining strict vs non-strict, Min VRAM GB ("blank = template minimum"), checkbox **"Allow spot / interruptible"** with "Off: on-demand only (spot is refused in this release)." (ticking it produces a validation error), **"Routing strategy"** segmented control "Balanced", "Cheapest", "Fastest", "Stable" with a hint (for example "Price 40%, availability 20%, stability 15%, ...").
- **"Limits (hard caps; the quote must fit)":** Max $/GPU·h, Max total $, Max runtime min ("auto-terminate · 5-60").
- **"Access":** "SSH public key (optional)" textarea (placeholder "ssh-ed25519 AAAA… you@laptop (optional, PUBLIC key only)") and a note: if empty, OpenGrid generates an ephemeral key and discards the private half, so nobody can SSH in. Pasting a private key gives an explicit error.
- **Sandbox** checkbox box: "Sandbox · SIMULATED: no GPU, no spend · No provider is called. The lifecycle, logs and metrics are synthetic and labelled simulated everywhere."
- Buttons: **"Preview route ↵"** (primary; free, writes an audit record, never calls a provider) and **"Submit for approval…"** (label changes to **"Submit sandbox job…"** when Sandbox is ticked). A "form changed: preview again" marker appears after edits following a preview. A summary "N fields to fix: ..." appears under the buttons. Footer note explains preview vs submit vs admin approval.

**Right: readiness and preview output:**
- **Readiness bar** (from GET /v1/jobs/readiness): "READY FOR A REAL RUN" ("Every required gate is open; an admin still approves each quote.") or "REAL RUN BLOCKED" ("N required gates closed: <codes>. Sandbox jobs still run (simulated)."), with badges "mode <MODE>" and "live provisioning off", and a fold "N checks · M provisionable providers: details" listing each check with a red or green dot and (for admins) a link to the fix. If signed out this bar stays empty.
- **Output area**, initially: "The MNIST preset is filled in. Preview route shows the market's best listing, the route OpenGrid can actually execute now and why, or exactly what blocks a real run." and "Tick Sandbox to walk the whole lifecycle with synthetic numbers first: no GPU, no spend."
- **Preview panel** after "Preview route" (spinner "Validating the workload and ranking every eligible GPU…"):
  - Request bar: "Preview" plus a summary like "1× GPU auto · US · ≤ $3.000/GPU·h · ≤ $2.00 total · 30 min · BALANCED", data kind badge, "mode SUPERVISED" badge, "live provisioning off" badge, market data freshness, "as of".
  - Blocker box "NOTHING IS EXECUTABLE RIGHT NOW" with "N blockers: a real run cannot launch until they are cleared", global blockers with code and a fix link (mostly to Admin pages, for example "ROUTING_LIVE_PROVISIONING is off (Railway variable): set it to true on the deploy and redeploy"), and a fold "Per provider · N provisionable providers not ready (codes; open for the exact reasons)".
  - "Submit would" line: "create the job + a quote → PENDING APPROVAL", "run a SIMULATED job (no provider)", "LAUNCH (LIVE mode)" or "be REFUSED", plus SSH mode.
  - Account limit violation warning if any.
  - Three cards: **"EXECUTION-READY ROUTE"** ("executable" or "not executable" badge; provider, GPU, region, freshness, observed price, quote note, est. max and typical), **"MARKET RECOMMENDATION"** ("information only"), **"SANDBOX"** ("selected" or "available"; "SIMULATED: no GPU, no spend").
  - Cost cells: "Est. typical total", "Est. max total", "Your cap", "Strategy", and a basis note.
  - "Why it won · <Provider>" score breakdown (colour bar of factors with contributions) and "Workload requirements" table with a fold of compatible GPU models.
  - "Next-best alternatives · N" table (#, Provider, GPU, Location, $/GPU·h, Est. max, Executable, Score, Breakdown, vs #1; CSV).
  - "Exclusions · N" table (Provider, GPU, $/GPU·h, Code, Reason; CSV, first 60) and "Ineligible GPUs · N" table.
  - Footer note on observed price vs quote vs execution price, "Method →".
  - Errors use a technical box: the code (for example "budget exceeded"), message, field, fix link, blockers, hint, and for 403 "Your key needs the route:preview (preview) or route:execute (submit / cancel) scope. API keys →".

#### Submitting (both "Run it…" and the Submit buttons)
- Validates the form, and re-runs the preview if the form changed.
- **Real run with nothing executable:** dialog "Nothing is executable right now" ("OpenGrid will not submit a real job without an execution-ready route. Fix the blockers shown in the preview, or tick Sandbox to run a SIMULATED job (no GPU, no spend).") with the blocker list; buttons "Close" and "OK" (both just close).
- **Real run:** dialog "Submit training job for approval": "Creates the job and a quote. Approval is required before anything launches: an OpenGrid admin approves that exact quote on the route ticket. Nothing is charged before then." (In LIVE mode it instead says a validated provider may launch at once within cost guards.) A table: Job, Route, Max cost exposure, Price cap, Runtime ceiling, SSH. An acknowledgement checkbox that must be ticked: "I accept a maximum cost exposure of $2.00 and a 30-minute runtime ceiling". Buttons "Cancel" and **"Submit for approval"**.
- **Sandbox run:** dialog "Submit SIMULATED sandbox job" ("SIMULATED: no GPU, no spend. ..."), no checkbox, buttons "Cancel" and **"Submit sandbox job"**.
- On success it goes to the new job's page. On error it shows the technical error; if the outcome is unknown (network), a **"Retry (same Idempotency-Key)"** button replays safely.

#### Signed-out behaviour
The simple view still renders. Templates fail quietly (built-in defaults, with a warning only visible in the Advanced fold), the readiness bar is empty, "Start free demo" shows an "HTTP 401" style error inline, and "Check price" says "Could not check right now. Open Advanced details below for the full error." There is no clear "sign in first" message or Sign in button on this page.

#### Refresh behaviour
None. Readiness loads once; preview runs on demand.

#### Observations
- The simple view is clear, but the Advanced fold contains a second full page header ("New training job" with its own "← My Jobs"), which looks odd nested inside a fold.
- The full form is extremely technical for a "New job" page: routing strategy weights, strict region, compute capability, idempotency keys, "SUPERVISED", "ROUTING_LIVE_PROVISIONING (Railway variable)", "audit record", "route ticket". Many blocker fix links point to admin-only pages (/admin/execution, /admin/checklist, /ops).
- "Allow spot / interruptible" is offered as a checkbox but always fails validation, and four of five job types are visible but disabled ("not yet").
- `?preset=mnist` (used by /find and the assistant) is ignored; `?sandbox=1` only ticks the hidden Sandbox checkbox, so a visitor following "Train an example model (free simulation)" lands on the three-choice view, not a pre-selected demo. "Check price" also unticks Sandbox.
- "MNIST preset · founder's defaults" exposes internal wording ("founder") to customers.
- Signed-out visitors get no sign-in prompt; errors are generic or hidden in the fold.
- In the common case (real runs not switched on) the real-GPU choice will always end in "Not available right now", so one of the three choices is effectively dead in production.
- Two Submit paths (simple "Run it…" and Advanced "Submit for approval…") share one dialog, which is good, but the simple path shows its loading and errors inside the choice box while the Advanced path shows them in the output column.

---

### /jobs/{id} (Job page)

**URL:** `/jobs/<job-id>`. Parameter `?adv=1` opens the Advanced fold (kept in the URL when you open or close it).

**Who / sign-in:** The job's owner. Needs the operator login or an API key that can read jobs; cancel needs route:execute. Admins see approval controls.

**Purpose:** Follow one job in plain words (is it running, can it spend money, what did it cost) with all technical evidence folded away.

#### States before the job loads
- "Loading job…" spinner.
- If the first load fails: header "Job <id>" with a "← Jobs" button, and the error box (signed out: "Sign-in required" with a **"Sign in"** button; missing endpoint: "GET /v1/jobs/{id} is not available on this server yet."). Polling stops.

#### Layout, top to bottom
1. **"← My Jobs"** link.
2. **Header:** the job name as title; subtitle saying where it runs ("Free simulation: no GPU, no provider, no spend", "1× <GPU> on <Provider> <region>", or "GPU not chosen yet"). Right side: a red cancel button, shown while the job can still be cancelled: **"Cancel job…"** before launch, **"Cancel & terminate…"** after launch.
3. **Big status line:** the same plain line as on My Jobs (icon, text, colour), plus a "real" badge for real runs.
4. **SIMULATED banner** for sandbox jobs: "SIMULATED · No GPU was used and nothing was charged. Every number on this page is synthetic."
5. Flash area (messages such as "Cancelling…", "Cancel requested", "Refresh failed ... showing the last answer; retrying").
6. **Billing alarms** (real jobs only): red "POSSIBLY BILLING" with an explanation and **"Open deployment →"**; or amber "Job failed: instance not yet terminated".
7. **Failure box** if the job failed: the failure code, reason and a fix link.
8. **Progress box:** eyebrow "Progress", badge "free simulation" or "real GPU run", and a grey line. A numbered stage strip: real runs have 8 stages "Preparing", "Selecting GPU", "Awaiting approval", "Starting GPU", "Running", "Saving results", "Shutting down", "Complete"; simulations have 4: "Preparing", "Running", "Saving results", "Complete". Each stage shows ✓ done, ✗ failed, the number, "skipped", or (current) the raw state name. The last stage label becomes "Ended: failed", "Ended: cancelled" or "Complete (simulated)". Below, four plain questions with bold answers and a grey detail:
   - "Can real money be spent?" (No / Not any more / Not yet / Yes / Yes, possibly right now)
   - "Is it running?" (Yes / Not yet / Not yet, waiting for approval / No, wrapping up / No, finished)
   - "Is action needed?" (No / Not from you, waiting for OpenGrid approval / Yes, approve or reject the quote (admins) / Yes, the quote expired ... / Yes, check the deployment)
   - "What did it cost?" ("$0.00 (simulated)", "$0.00 (nothing launched)", "$X", or "Not known yet" with "Known after termination is confirmed and reconciled · ceiling $X.")
9. **Approval box** (only when pending approval or quote expired): badge "PENDING APPROVAL" or "QUOTE EXPIRED", a countdown "quote expires in ...", the route request id link. Non-admins see a spinner and "Waiting for OpenGrid approval. Nothing is launched or charged until an admin approves this exact quote." (or the expired text). Admins see an explanation and a primary **"Open approval ticket →"** (or "Open the ticket (re-quote) →") to /route?rr=…. Also the quoted price, estimated total and any limit violations.
10. **"Training progress"** section (suffix "· SIMULATED (synthetic numbers)" for simulations): stats Epoch "X / Y", Latest loss, Test accuracy, Elapsed, a Progress bar, Runner result. Then two small line charts "Train loss" and "Test accuracy" by epoch, or a note such as "Metrics appear once training reports its first epoch. Only persisted runner metrics are shown; nothing is extrapolated."
11. **"Artifacts · N"** section: table Name (download link, "simulated" badge), Size, sha256 (shortened, "copy"), and a **"Download"** button; or "model.pt, config.json, metrics.json and env.json are uploaded when training ends (sha256 verified on receipt)." / "No artifacts were stored."
12. **Fold "Advanced details"** ("(job and deployment state machines, logs, verification checks, cost reconciliation, events, request, routing decision)"), containing:
    - A meta line: job id with copy button, template and version, "state <badge>", provider link, GPU link, deployment id link, route request id link, and a small **"Refresh now"** button.
    - Stats: State since, Created, Started, Runtime (with ceiling), Est. max (with typical), Actual cost, Resource (disposition such as "terminated (provider confirmed)" or "POSSIBLY BILLING").
    - Two state strips: "Job lifecycle" (all raw states) and "Deployment" (its own strip, or "SIMULATED: no deployment, no instance.").
    - **Logs** panel: line count, a "filter logs" search box, an **"auto-scroll"** checkbox, **"Copy all"** button, the log text (errors red, warnings amber), "No logs yet: the runner starts after approval and launch." when empty.
    - "Verification · N / 12 verified" (or "SIMULATED (no real check)"): 12 checks such as "GPU instance created", "CUDA available to PyTorch", "Weights saved (model.pt)", "Termination verified with the provider", each with ✓, ✗, ~ or … and evidence on hover.
    - "Termination evidence" (deadline, requested, provider terminated, instance id, last event, disposition, SSH access block) or for simulations "SIMULATED: no instance existed, nothing to terminate."
    - "Cost: four numbers, never merged": Quote, Expected cost, Provider-reported, OpenGrid transaction, and an estimates note.
    - "Job events · N" timeline (time, from → to state, actor, reason, evidence) and "Request" (template, config, resources, strategy, limits, SSH, bootstrap hash).
    - "Routing decision · at submission" (open fold): selected listing, observed price, why, market recommendation, execution mode, and the full preview panel from /jobs/new.
    - Footer: "Live: refreshed every 4 s while the job is active." or "Finished: polling stopped." plus "Job state and deployment state are separate machines." and "Execution safety →".

#### Cancel
Opens a danger dialog:
- Before launch: title "Cancel this job before launch?", body "Nothing has launched: the route is rejected and the job ends. Records are kept.", buttons "Cancel" and **"Cancel job"**.
- After launch (real): title "Cancel the job and terminate its instance?", body about requesting termination through the provider API, plus a required checkbox "I understand the instance and its local data are destroyed; artifacts not yet uploaded are lost", buttons "Cancel" and **"Cancel & terminate"**.
- Simulation: same "terminate its instance" title, body "Stops the simulated run.", no checkbox.
- All variants add "One Idempotency-Key per click: a retry after a network error cannot act twice." On success: "Cancel requested" and polling restarts. If the outcome is unknown: "Outcome unknown. Cancel again: it reuses the same Idempotency-Key (…)."

#### Refresh behaviour
Polls the job, deployment, metrics, artifacts and new log lines every 4 seconds while the job is active (skipped while the tab is hidden), and stops when the job is finished. "Refresh now" (inside Advanced) reloads immediately. A failed refresh keeps the last data and shows a warning.

#### Observations
- The top half is genuinely plain-language; the Advanced half is a full operator console (state machines, idempotency keys, sha256, reconciliation "four numbers, never merged"), which is a big tone jump.
- The only manual refresh button is hidden inside Advanced (though polling makes it mostly unnecessary).
- For a running simulation the cancel button reads "Cancel & terminate…" and the dialog title says "terminate its instance", although there is no instance; the assistant meanwhile says simulations "have nothing to cancel". Inconsistent.
- The "cancel" dialog uses "Cancel" for the dismiss button and "Cancel job" for the confirm button, which is easy to misclick (the assistant's version uses "Keep it running", which is clearer).
- The four plain questions omit "Is this a simulation?", "Is a GPU still active?", "Did it succeed?" and "What outputs exist?" even though the code computes them; success is only implied by the status line and stage strip.
- The current stage shows the raw state name under it (for example "pending approval", "saving artifacts"), mixing plain and technical words.
- The signed-out back button says "← Jobs" while the normal one says "← My Jobs".

---

### /keys (API keys)

**URL:** `/keys`. Parameters: `?preset=assistant` (pre-fills a key for an AI assistant), `?h=168` or `?h=720` (usage window). Anchors `#credentials`, `#billing`.

**Who / sign-in:** The account operator or a developer. Everything needs the operator login (the page says it "acts as the operator account") or an API key with account powers.

**Purpose:** Create and revoke OpenGrid API keys, see usage and rate limits, store bring-your-own provider credentials, and preview billing.

#### Layout, top to bottom
1. **Header.** Title "API keys", subtitle "One OpenGrid key for market data, routing and deployments · this page acts as the operator account", buttons **"API docs →"** (/api) and **"Billing method"** (/methodology/billing).
2. **Who am I** stats: "Signed in as" (operator or key name; sub "site password / open dev: every scope" or "API key <prefix>"), "Account" (name, #id, plan, status), "Scopes" ("all" or count and list), "Active keys" (N, "M total"), "Rate budget now" (remaining/limit, or n/a "not rate limited: the operator is not an API key").
3. **Two columns.**
   - Main column:
     - A "shown once" secret panel appears here right after creating a key: "New key "<name>" created", warning "Copy this key now. OpenGrid stores only an HMAC of it and cannot show it again. Treat it like a password: ...", the secret with a copy button, a sample `curl` line, and a **"Done"** button that asks "Hide the secret?" ("OpenGrid keeps only a hash of this key. Once hidden it cannot be shown again; you would have to create a new key.", confirm **"I've stored it"**).
     - **"Keys"** table: Name, Key (prefix…), Scopes (badges; route:execute amber, admin and account:manage red), State, Created, Last used (IP on hover), Expires, Revoked, Limit/min, and a **"Revoke"** button for active keys. CSV export. Empty: "No API keys yet. Create one below; the secret is shown once." Revoke opens a danger dialog "Revoke "<name>" (<prefix>…)?" ("Every request using this key fails with 401 from now on. Revoking cannot be undone; create a new key instead." plus last-used info), confirm **"Revoke key"**.
     - **"Create a key"** form: "Name" (placeholder "e.g. research-notebook"), "Expires" ("never expires", "30 days", "90 days", "1 year"), "Read limit / min" (placeholder "default"), primary **"Create key"**. Then "Scopes · data reads are separate from anything that spends money · from <source>" and a grid of checkboxes, each with the scope name, a description and badges: data:read (read market data...), route:preview (dry-run routing decisions), route:execute ("provision compute through OpenGrid (spends money)", badge "spends money"), deployments:read, deployments:write, watchlists, billing:read, account:manage (badge "sensitive"), admin (badge "sensitive"). Default ticked: data:read. A note: "A key can never grant scopes its creator does not hold. ..." With no scope ticked: error "pick at least one scope". With route:execute or admin ticked, a danger dialog "Create a key that can spend money?" (or "administer OpenGrid?") with explanation and a required checkbox "I will store this key like a password", confirm **"Create key"**.
     - **With `?preset=assistant`:** name pre-filled "ai-assistant", scopes data:read, route:preview and deployments:read pre-ticked, and a note: "Assistant key: reads prices and your jobs and can preview routes. Tick route:execute too if you want your assistant to start the free demo or cancel a job. OpenGrid's assistant tools can't rent a paid GPU, but route:execute also allows real launches through the API, so keep that key private."
   - Side column:
     - **"Rate limits"**: a small table of classes read (GET / HEAD / OPTIONS), write (other methods), execute (non-GET under /v1/route, /v1/deployments) with per-minute defaults when known, and notes on token buckets, X-RateLimit headers, 429 and Retry-After.
     - **"Authentication"**: `Authorization: Bearer opg_live_…`, a note about key format and hashing, and "Auth & scopes in the API docs →".
4. **"API usage"** section: a "24H" / "7D" / "30D" toggle and "since ... · rows kept N days · only API-key requests are logged (not this UI)"; stats Requests, OK, Rate limited (429), Errors, Avg latency; tables "By key" and "Top paths" (CSV), "Latest requests" (20 rows). Empty: "No API-key requests in this window."
5. **"Provider credentials · OpenGrid-managed vs bring-your-own"**: two explainer boxes, "default / OpenGrid-managed" (lists providers configured on the server, or a warning "none: routing here has no managed provider credentials") and "optional / Bring your own (BYO)" (encrypted at rest, last 4 characters shown, one per provider; for the operator a warning that web UI routes use managed credentials only). A table Provider, Label, Secret ("…abcd (masked)"), State, Added, Last used by routing, and a **"Delete"** button (danger dialog "Delete your <Provider> credential (<hint>)?", confirm **"Delete"**). Form "Add a BYO credential": provider select (each marked "(OpenGrid can provision)" or "(no adapter: stored, not used)"), password field "provider API key", "label (optional)", primary **"Store encrypted"**, and a provider hint (for example Verda needs client_id:client_secret). Replacing an existing one asks "Replace your <Provider> credential?" (confirm "Replace").
6. **"Billing preview"**: a "preview" badge and "No payments are processed. OpenGrid charges no cards and simulates no payment processor. Invoices are drafts ..."; "Fee policy in force" (policy name, version, components such as "N% of provider cost") or "No fee policy"; "Credits" table or "No credits."; "Metered usage" stats and table (Period, Deployment, Kind, Provider, GPU, GPU-h, Provider cost, Charge lines; CSV) or "No metered compute: usage is recorded only for real deployments."; "Invoices (drafts)" as expandable rows with subtotal, credits, total and line items, or "No invoices. ...".

#### States
Each box shows its own spinner first. Signed out: the who-am-I box, Keys, API usage, Provider credentials and each billing box each show their own "Sign-in required" error with a "Sign in" button (several identical errors on one page). The Create a key form and the side boxes still render, and pressing "Create key" fails with an error.

#### Refresh behaviour
No auto-refresh. Lists reload after create, revoke, store or delete. The usage window toggle reloads usage.

#### Observations
- One page mixes four products: API keys, API usage analytics, provider credentials (BYO vs managed) and billing previews. For an assistant user who came via `?preset=assistant`, the page is very long and the key form is in the middle.
- Heavy jargon: HMAC, token bucket, X-RateLimit-Class, Fernet, scope names like `account:manage`, "site password / open dev", "operator".
- Signed out produces five or more separate error boxes instead of one clear sign-in prompt.
- The "Create a key" form is placed after the keys table; for a first-time user the table is empty and the action is below it.
- `?preset=assistant` explains route:execute well, but the scope grid still lists admin and account:manage to everyone.
- The billing preview says "No payments are processed", which may confuse a customer who just read "spends money" on route:execute.
- Revoke and Delete buttons share a style class "ky-rev" with watchlists' Delete; consistent, good.

---

### /watchlists (Watchlists & alerts)

**URL:** `/watchlists`. Parameters: `?wl=<id>` (selected watchlist), `?add=<gpu-slug>` (pre-fills the GPU in the add-item form; also reached from the command bar "w <gpu>"). Anchor `#alerts`.

**Who / sign-in:** Signed-in users who want to track prices and get alerts. All data needs the operator login or an API key with the watchlists scope.

**Purpose:** Follow GPUs, providers, regions and indices with live observed values, and set price or availability alert rules.

#### Layout, top to bottom
1. **Header.** Title "Watchlists & alerts", subtitle "Follow GPUs, providers, regions and indices; alert on price and availability · observed data, unknown never fires", buttons **"Alerts method"** (/methodology/alerts) and **"API"** (/api#ep-watchlists-alerts).
2. **Two-column grid:**
   - **Left rail:** eyebrow "Watchlists" with a count; one button per watchlist (name and item count; click selects it and sets `?wl=`); "None yet." if empty; a "New watchlist" text box with a **"Create"** button.
   - **Main panel:**
     - No watchlist: "No watchlist yet." and "Create one on the left, then add GPUs, providers, a GPU at one provider, a region group or an OpenGrid index. Values are observed market data, read live." With `?add=`: "Create a watchlist to add <gpu>."
     - Selected watchlist: header with the name, "N items · created <date>", an "observed" badge, **"Rename"** (dialog with a text box, confirm "Rename") and **"Delete"** (danger dialog "Delete "<name>"?" "Removes the watchlist and its N items. Alert rules are separate and are kept.", confirm "Delete").
     - Items table: Item (kind badge plus links), Low $/GPU·h (or index points), Median, 24h change, Availability (for example "3 avail · 2 unknown · 1 sold out"), Detail (for example "low at <Provider> · N providers"), and actions: **"Alert…"** (GPU or GPU + provider items; pre-fills the alert form below and scrolls to it), **"Route"** (GPU items; to /route?gpu=…), **"✕"** (removes the item immediately, no confirmation). Missing values show "n/a" with a reason tooltip. Empty: "Empty watchlist: add an item below." Values fill in after the table appears.
     - Add-item form: "Add" label, kind select ("GPU", "Provider", "GPU + provider", "Region", "Index"), then only the relevant inputs: GPU slug text box with autocomplete (placeholder "gpu slug, e.g. h100-80gb-sxm5", or "gpu (optional)" for Region), Provider select, Region group select, Index select; primary **"Add item"**.
3. **"Alert rules"** section:
   - A "shown once" webhook secret panel after creating a rule with a webhook: "Webhook signing secret for rule #N", verification instructions (X-OpenGrid-Signature, HMAC-SHA256), the secret with copy, and **"Done"** (hides it with no confirmation).
   - **Rule form:** "Templates" chips: "H100 below $2/hr", "B200 becomes available", "Lambda H100 drops 10%", "H100 US availability falls 20%", "OpenGrid H100 Index hits new 30d low" (each fills the form). Fields shown depend on the metric: "Metric" (Lowest price now, Median price now, Provider price now, Listings available, Provider price change %, Region availability change %, Index level, Index new low), "GPU", "Provider" or "Provider (optional)", "Region group", "Index", "Window h", "Window", "Condition" (operator <, <=, >, >=, ==, != plus a number and a unit such as "$/GPU·h"), "Name" (optional), "Cooldown s" (default 3600). "Channels": "in-app feed (always)" (ticked, disabled), "webhook" URL box (placeholder "https://… (signed, HMAC-SHA256)"), and an email checkbox labelled "email: not implemented yet (recorded as not_implemented, never sent)". Primary **"Create rule"**. A help note explains the metric, edge triggering, cooldown, and that unknown readings never fire. Defaults: Lowest price now, H100 80GB SXM5, < 2.
   - **Rules table:** Rule (name or #id, with the condition in words, for example "Lowest price now < 2: H100 SXM5"), State badge ("ok", "firing", "unknown", "paused", "not evaluated"), Last value, Reading, Evaluated ("N ago" / "never"), Last fired, Channels (badges; email amber "not implemented: never sent"), and actions **"Test now"** (dry run: shows a box "Test · dry run" with "condition true / false / unknown", the value, "would fire now" or "would not fire: <reason>", "nothing stored or sent", and a "✕" to close), **"Pause"** / **"Resume"**, **"✕"** (danger dialog "Delete rule "<name>"?" with the condition, confirm "Delete"). Empty: "No alert rules. Start from a template above." A note explains the states and the ~3 minute evaluation cycle. CSV export.
4. **"Firings · last 7 days"** table: Fired, Rule, Value, Message, Delivery (badges like "webhook: ok", "email: not_implemented"). CSV. Empty: "No alert has fired in the last 7 days."

#### States
- Spinners in the main panel, rules and firings boxes while loading.
- Signed out: the main panel shows "Sign-in required" with "Sign in" and "Retry"; the left rail stays blank (it is only drawn after a successful load, so even the "New watchlist" box is missing); the rules box shows "Sign-in required"; the firings box keeps its spinner forever (firings load only after rules succeed). The alert rule form still renders and "Create rule" fails with an error.

#### Refresh behaviour
Every 60 seconds (paused when hidden): reloads lists, live values and rules (and firings). Also after every create, rename, delete, add, pause or resume.

#### Observations
- The page is two products stacked: watchlists on top, then a full alert rule builder, rule table and firing log. Alerts are not tied to a watchlist even though the "Alert…" button implies they are.
- GPU input expects a slug ("h100-80gb-sxm5") typed into a free text box, which is developer-oriented; the autocomplete helps but shows slugs as values.
- The email channel is shown but not implemented; a visible, tickable option that never sends is likely confusing.
- Removing an item has no confirmation and no undo, while deleting a list or rule does.
- Signed-out experience is broken-looking: blank rail, permanent spinner in Firings, and a usable-looking alert form that cannot save.
- Jargon: "edge-triggered", "cooldown s", "HMAC-SHA256", "region group", "index new low (1 = new low)", "not_implemented".
- The "Route" action leads to a private, operator-style page.

---

# Part 4: Execution and operator pages

Source: web/pages/route.js, reserve.js, deployments.js, onboarding.js (also holds /admin/partners), execution.js, checklist.js, value.js, plus shared helpers in web/core.js. Page list: api/pages.py PAGES (every page below is marked non-public there).

### How sign-in works for all pages in this part (read first)

- With PUBLIC_PAGES on (production), the HTML shell of every page loads for anyone, including these. The data behind them does not: every /v1 area these pages use (route, deployments, reservations, admin, partners, onboarding, quotes) is private.
- The browser UI has exactly one way to sign in: the site login (HTTP Basic password) via "/login?next=<this page>". The front end never sends an API key. A browser signed in that way is the "operator" principal, which the UI treats as admin (OG.isAdmin: principal operator, or scope "*" or "admin"). So in practice, everyone who can use these pages in a browser is the operator. API keys with scopes (route:preview, route:execute, deployments:read/write, account:manage, admin) apply to the CLI, MCP and REST, not to the web pages.
- Signed out, a private request gets HTTP 401 with a Basic challenge. Where a page uses the shared error box (OG.error), it shows "Sign-in required" + "Authentication required" + a "Sign in" button (to /login?next=...) + "Retry" when the page passes one. The server's 401 carries `WWW-Authenticate: Basic`, so some browsers may also pop their native password dialog on those background requests (not handled in code).
- The four /admin/* pages check OG.me() first. Signed out (or not admin), they render only the title and a red "Not allowed admin scope required" box (status 403 styling), with no "Sign in" button.
- The left nav "Admin" group (Execution, Checklist, Partners, Value) is hidden unless OG.me() says admin. Route, Deployments and Reserve sit under "My Jobs" in the nav; Onboarding sits under "Developers".
- Shared confirm dialogs: OG.dialog (title, body, Cancel + confirm button; an optional "ack" checkbox that must be ticked before the confirm enables) and OG.ask (form dialog; the confirm stays disabled until every required field is valid; a "match" field must equal an exact word, case-insensitive). Esc or clicking outside cancels.
- Money-moving POSTs use one Idempotency-Key per user intent (OG.intentFor). A new click with the same body gets a fresh key, unless the last attempt's outcome was unknown (network error), in which case "Retry (same Idempotency-Key)" reuses it so the server replays instead of acting twice.
- Auto-refresh timers (ctx.every) pause while the browser tab is hidden and stop when you navigate away.

---

### /route (Route: best-execution order ticket)

- URL: /route. Query parameters are kept in sync with the form: gpu, count, region, max, dur, dl, maxrun, mode, w (custom weights "price:40,..."), ex (excluded providers), lvl=1, avail=1, strict=1, variants=1. /route?rr=<route_request_id> reopens a route ticket. The command bar "r h100 8 us" opens this page prefilled.
- Who: written for customers (request a route) and the operator (approve or reject it). In the browser only the operator can actually use it (see above).
- Signed out: the form renders and the GPU list and provider chips load (public data), but "Preview" returns the "Sign-in required" error box in the results area. ?rr= tickets show the same error.
- Purpose: describe a GPU workload, see which listing OpenGrid would pick and why, then request a quote that an admin approves before anything launches.

#### Layout, top to bottom

1. Page header: title "Route", subline "Best execution across every provider OpenGrid observes · preview is free and never calls a provider". Right side buttons (links): "Scoring method" (/methodology/best-execution), "Routing & provisioning" (/methodology/routing), "Deployments →" (/deployments).
2. Two-column grid. Left: the order ticket form. Right: a mode banner, then a "routed" area (route result or ticket), then the preview output.

#### Left column: "Order ticket" form

- Header row: eyebrow "Order ticket" and grey mono text "POST /v1/route/preview".
- If the URL had an unknown region: a yellow box "Unknown region "<x>" is not a region group; one of US, Canada, ...".
- "GPU": text input, placeholder "h100, mi300x, b200…". Fuzzy suggestion list (up to 9) appears on focus/typing: up to 2 GPU families first (tagged "family"), then GPUs (tagged "<n> prov" or "none live"). Empty input shows live GPUs by provider count. Arrow keys, Enter, Esc work. Below the input a grey line names the full GPU ("... · N providers live" or "· no live listings").
  - Picking a family shows a family box: a note ("Variants of one family are different products ...; OpenGrid never merges their prices."), a list of variants (link to the GPU page, "from $x" or "no live price", "<n> prov", and a button "only this" that switches to that variant), a checkbox "Allow variants (any <family> variant may be selected)", and if unticked a warning "Without this, pick one variant: a family alone is not one product."
- "GPUs / instance": number input (1 to 64) plus chips "1×", "2×", "4×", "8×".
- "Region group": select "Any region", US, Canada, Europe, UK, APAC, Middle East, LATAM, Africa; next to it checkbox "Strict region" (disabled until a region is chosen; tooltip explains it excludes unconfirmed locations).
- "Max $/GPU·h": number, placeholder "none".
- "Duration h" (hint "cost = quote × GPUs × hours") and "Deadline h" (hint "if no duration: cost of the full window").
- "Auto-terminate min" (hint "max_runtime_minutes: terminated after this").
- "Mode": segmented control "Cheapest", "Fastest", "Balanced" (default), "Stable", "Custom", with a one-line hint for the chosen mode (for example Balanced: "Price 40%, availability 20%, stability 15%, availability history 15%, freshness, integration"). "Custom" opens sliders (0 to 100, step 5) for Price, Availability now, Region match (disabled with "needs region" until a region is picked), Data freshness, Availability history, Price stability, OpenGrid integration; each shows its normalized percent. Reliability and Performance sliders are disabled with "no data, not used". Note: "Weights are normalized to 100%. Reliability and performance stay at zero until OpenGrid has real execution records."
- "Exclude providers · (dot) = OpenGrid can provision": one toggle chip per provider; a dot marks providers with integration level 2 or more; click to exclude.
- Checkboxes: "Require provisionable (OpenGrid integration ≥ 2)" and "Require explicit availability".
- Collapsible "Launch parameters (route only)": help text about SSH public key vs provider key name (BYO only) and "Env values are never stored"; fields "Name" (placeholder "opengrid-job"), "SSH public key" ("ssh-ed25519 AAAA… you@host"), "SSH key name" ("BYO credentials only"), "Image" ("e.g. pytorch/pytorch:latest"), "Disk GB".
- Buttons: "Preview ↵" (primary, submit; Enter key) and "Request route…" (tooltip "POST /v1/route: live check + quote + cost guards; SUPERVISED mode waits for admin approval").
- After a preview, editing any field shows "ticket changed: preview again".
- Footer note: "Count is GPUs per instance; a route launches one instance. Preview writes an audit record but never calls a provider. Request route issues a quote; under SUPERVISED execution an OpenGrid admin approves it before anything launches."

#### Right column

- Mode banner (admins only, from GET /v1/admin/execution/mode): coloured strip with the mode name (DISABLED, PREVIEW ONLY, SUPERVISED, LIVE) and its meaning; if stored mode is capped it adds "stored mode X, capped by the deploy-level env flag (ROUTING_LIVE_PROVISIONING=false)". Non-admins never get this banner here (but the ticket shows "EXECUTION MODE UNKNOWN ... (admin only)").
- Empty state (no GPU in URL): explanation paragraph, "Try:" with three example links ("8× h100-80gb-sxm5 US ≤ $2.5", "1× h200-141gb-sxm5", "8× a100-80gb-sxm4"), and "From anywhere: press / and type r h100 8 us."
- Unknown GPU in URL: "Unknown GPU" box ("... does not match a canonical GPU. OpenGrid never guesses a mapping; pick one from the list.").
- Loading: "Ranking every eligible listing…". A family without "Allow variants" returns "A family is not one product ... Tick “Allow variants” or pick one variant under the GPU field."

##### Preview result (after "Preview")
1. Request bar: eyebrow "Preview", summary "8× H100 · US · ≤ $2.50/GPU·h · 10 h · BALANCED", a data-kind badge, a badge "live provisioning ON" (warn) or "live provisioning off here", and the route_request_id as an external link "rr_... ↗" to the raw JSON audit record (/v1/route/<id>, new tab).
2. If nothing qualifies: "No route" box with the reason, optional multi-instance table, and section "Why nothing qualified" with the exclusions block. Stops here.
3. Provisioning warning (when the winner cannot be launched by OpenGrid): "Provisioning: OpenGrid cannot launch on X (integration level N: market data only). Best provisionable alternative: Y at $z/GPU·h (rank r). Request route skips non-provisionable candidates."
4. Selected card: "#1", logo, provider link, SKU, location, "N× GPU", level badge ("OpenGrid can provision · L2" green or "market data only · L0" red). Eight cells: "Observed price" (with "observed" badge, plus instance $/h), "Quote" (with "quote" badge; sub "from the observed listing; not a live provider quote"), "Expected cost" (total, or hourly only), "Market median", "Savings vs median" (green/red, percent, total), "Availability" (available / sold out / unknown), "Freshness" badge, "Can OpenGrid provision?" (yes/no · level). Footer: "score 0.xyz", coloured score bar, truncated listing id.
5. "Best provisionable" strip (if different): provider, price, badges, rank and score, mini bar.
6. Two sections side by side: "Why this route" (one line per weighted factor with its note, "imputed 0.5" badge where data is thin, "+0.123" contribution; method link "Method →") and "Factor breakdown · selected" (table: Factor, Kind, Weight, Value 0-1, Contribution; Reliability and Performance show "0 · locked" and "no data, not used").
7. Section "Alternatives & score contributions": colour legend with weight per factor, then table "Ranked candidates · top N" (#, Provider, Variant (only if several), SKU, Location, GPUs, $/GPU·h, $/h, Stock, Seen, Lvl, Score, Breakdown bar, vs #1). Toolbar button "All candidates" loads every candidate from the audit record ("Loading…"). CSV export "opengrid-route-candidates.csv".
8. "Multi-instance shapes (not auto-provisioned)" table, if any.
9. Section "Excluded listings": filter chips per reason code with counts (click to filter, click again to clear), then table "Exclusions · N (first M shown; all in the audit record)" with Provider, Listing, GPUs, Region, $/GPU·h, Code (badge such as "over max price", "sold out", "not provisionable"), Reason. CSV export.
10. Note: "Prices: candidates carry observed market prices; the route carries a quote; a deployment carries the execution price only once a provider reports one. Data kinds →".

##### "Request route…" (money path, step 1)
- Runs a fresh preview first if none exists or the ticket changed.
- Opens a confirm dialog. Title depends on the mode: "Request route: creates a quote for OpenGrid approval" normally, "Request route: LIVE mode may launch without approval" in LIVE (red confirm and a required checkbox "I understand a validated, live-enabled provider can launch a paid instance immediately"). Body explains the live check, quote and cost guards, then a mode-specific bold line (SUPERVISED: "Nothing launches until an OpenGrid admin approves that exact quote."; other modes: "Execution mode is X: nothing will be launched."; unknown: generic). Table rows: "Request", "Estimate" (total or per-hour; open-ended warns to set a duration or cap), "Price cap" ("live quote must be ≤ $x/GPU·h" or warning "none set: cost guards still apply"), "Top candidate" (provisionable or "not provisionable: skipped"). Buttons "Cancel" and "Request route".
- Sends POST /v1/route with an Idempotency-Key. Loading: "Routing: live check, quote, cost guards…".
- On error: error box; if the outcome is unknown, "Outcome unknown. Retry (same Idempotency-Key). The server replays the first answer instead of routing twice."
- If the result is pending approval, the URL gets ?rr=<id> and the Route ticket appears (below). Otherwise a "Route result" box: status badge ("PROVISIONED", "NOT PROVISIONED", "FAILED", "NO CANDIDATES", "OUTCOME UNKNOWN", "PROVIDER TIMEOUT"), a sentence (for example "... Nothing was launched or charged; no deployment was created."), mode badge, audit link, "Dismiss" button, a quote strip, and table "Candidates tried, in rank order" (#, Provider, Listing, Step, Outcome, Reason, Quote).

##### Route ticket (pending approval; also opened via ?rr=)
- Header: eyebrow "Route ticket", badge "PENDING APPROVAL" (or "QUOTE EXPIRED", or the deployment state), "validation" badge if it is a validation launch, the reason, deployment id link (/deployments/<id>), audit link, "Dismiss" (clears ?rr).
- Mode banner (or "EXECUTION MODE UNKNOWN" for non-admins).
- Twelve cells: "Provider" (with listing id), "Region", "GPU × count" ("one instance"), "Quote" ("live provider check" or "observed listing (no live price)"), "Observed", "Est. hourly", "Est. total" (or "no duration given"), "Quote expires" (live countdown like "4m 12s", turns red under 60 s), "Fees" (total and line items, or the fee note), "Taxes" ("not computed"), "Billing unit", "Min. commitment".
- Safety block: "SSH access:" with "Customer supplied key: <fingerprint>" or red "MISSING: no SSH public key on this request", and "OpenGrid operator access: NONE" or red alternatives; an auto-terminate line "Auto-terminates: <local time> local (UTC ...), max runtime N min, source X (if approved now)" with "If approved now. The deadline is set at approval: approval time + N min (re-stated at launch)."; a red "Duration vs ceiling:" warning when the requested hours exceed the runtime cap.
- Limit violations (if any): "N limit violations · the launch is blocked unless an admin overrides (recorded with a reason)" and a table (Limit, Value, Cap, Override: "admin may override" or "never", Message).
- Actions, depending on state:
  - Not pending: "This route is <state>: open the deployment →".
  - Quote expired: badge "quote expired", "A launch never uses an expired quote.", button "Re-quote…" (disabled if the original request is unknown). Its dialog "Re-quote this route?" lists "1. Cancels this ticket (...: nothing was launched)." and "2. Sends the same request again ..."; confirm "Cancel & re-quote". It terminates (cancels) the old deployment with its own Idempotency-Key, then re-sends POST /v1/route.
  - Admin, pending: red button "Approve & launch…" (or "Approve with override…" when there are violations or an SSH override is needed), button "Reject…", and a hint: "Approval re-validates the quote live; a price move beyond tolerance issues a new quote instead of launching." The approve button is disabled with "Blocked: <codes> cannot be overridden" or "Blocked: <missing SSH key reason>".
  - Non-admin, pending: spinner "Waiting for OpenGrid approval. Nothing is launched or charged until an admin approves this exact quote." (the ticket polls every 10 s and redraws on state change).
- Approve dialog (money path, step 2): title "Approve launch on <Provider>", red confirm "Approve & launch". Body: "Launches ONE instance: N× GPU at the quote $x/GPU·h ($y/h, est. $z total). The quote is re-validated live first.", the SSH block, the auto-terminate line, warnings. Fields: "Type the provider name to confirm: <provider>" (must match), checkbox "Override the limit violations above (recorded in the control log)" (only with violations, required), checkbox "Allow the provider's account-level SSH keys on this machine (OpenGrid operator access; recorded with your name)" (only when needed, required), "Reason for the override (required)" or "Reason (optional)". POST /v1/route/<rr>/approve with an Idempotency-Key. Loading: "Approving: re-validating the quote, then the single provision call…".
  - Success: green "Provider accepted the launch" (or "Already approved", or "Approved · <status>") with button "Open live deployment →", plus the server's auto-terminate deadline and SSH block.
  - Price moved: the ticket redraws with the new quote and says "Not launched: <message>. Review the NEW quote above and approve again."
  - Unknown outcome: "Outcome unknown. Retry (same Idempotency-Key) It cannot launch twice."
- Reject dialog: "Reject this route", body "Nothing is launched; the quote is expired and the deployment ends as rejected.", required "Reason (required, shown to the partner)", red "Reject".

#### Refresh
- No auto-refresh of the preview. The ticket polls every 10 s only for non-admins waiting on approval. The expiry countdown redraws the ticket when it hits zero.

#### Observations
- Extremely dense: one page holds a form with 15+ controls, an 8-cell card, two explanation panels, two big tables, a filterable exclusions table and a full approval ticket. A first-time user has no clear "what do I do next".
- API jargon is shown to users: "POST /v1/route/preview", "max_runtime_minutes", "strict_region", "allow_variants", "Idempotency-Key", "imputed 0.5", "L2", raw "rr_..." ids linking to raw JSON.
- Two price ideas ("Observed price" and "Quote") usually show the same number in preview (quote basis is the observed listing), which may look redundant or confusing.
- "Duration h", "Deadline h" and "Auto-terminate min" are three overlapping time inputs with different units (hours vs minutes).
- The non-admin waiting state and "EXECUTION MODE UNKNOWN" banner are effectively unreachable in the browser, since browser sign-in is always the operator.
- "Request route…" label hides that it is the step that can lead to spending money; the confirm dialog carries that weight.
- Exclusion chips and candidate tables repeat information from the score panels.

---

### /reserve (Reserve: price-lock request list and form)

- URL: /reserve. Query: gpu=<slug> preselects a GPU; all=1 (admin) shows every account.
- Who: customers request reserved compute; the operator reviews (admin can switch scope). In the browser, the operator.
- Signed out: header, banner and form render; the GPU dropdown loads (public data). The reference panel and the list fail. Note: these failures use this page's own error box, which shows "HTTP 401 Authentication required GET /v1/reservations" style text and no "Sign in" button. A 403 adds "Requests need the route:preview scope; accepting an offer needs route:execute. API keys →".
- Purpose: ask OpenGrid for a price lock on future GPU capacity; nothing is billed.

#### Layout
1. Header: title "Reserve", subline "Price-lock requests for reserved compute · reviewed by OpenGrid · indicative references, never quotes". Right: admin-only segmented "Mine" / "All accounts", button "Reload", link "Methodology" (/methodology/reservations).
2. Banner: bold "NOT A FINANCIAL CONTRACT" + "Price-lock requests are reviewed by OpenGrid. An offer is made only when provider capacity backs it. This is a commercial order for compute delivered by OpenGrid, not a financial contract, not tradable, no cash settlement." + link "How price-lock requests work →".
3. Two-column grid:
   - Left form "New price-lock request · free · reviewed by OpenGrid":
     - "GPU" select ("Loading GPUs…", then "Choose a GPU…" plus GPUs with provider counts; defaults to H100 80GB SXM). Hint "One GPU variant (families and auto are not accepted for a price lock)".
     - "GPUs" (default 8, hint "used at the same time") and "Total GPU-hours" (default 5376).
     - "Start after" (default 7 days from now, hint "your local time") and "Start before" (default 14 days, hint "start window end"), both date-time pickers.
     - "Region" (Any region or a group) and "Your max $/GPU·h" (default 2.50, hint "your limit, not a price").
     - Running total line: "At your limit: up to $13,440.00 over 5,376 GPU·h (your ceiling, not a price)".
     - "Notes" textarea ("Optional: workload, interconnect, contract length, contact…").
     - Inline per-field errors (for example "in the past", "≥ 1 hour after start after", "at least one hour on each GPU"), and a general "Fix the highlighted fields."
     - Button "Submit request…" and note "Nothing is reserved, launched or billed by submitting."
   - Right: indicative reference. Before a GPU: "Pick a GPU to see the indicative reference: the OpenGrid index level, the best on-demand listings now and the latest settlement fixing." After (reloads 350 ms after GPU, count, region or price changes): section "Indicative reference now" + yellow "indicative" badge; stat strip "Index level" (links to /indices/<id>), "Raw level", "Best on-demand", "Market median", "Settlement fixing", "Your limit vs index" (+/- percent); table of best listings (Provider, GPUs / instance, Region, $/GPU·h, Available, Observed); note with "As of ...", "Fixing evidence →", "Index methodology →". If the endpoint is missing: "Not available yet".
4. Stat strip: "Requests", "Open" (awaiting a decision), "Offers to review" (valid offers), "Accepted" (order form pending), "Fulfilled", "Closed" (declined · expired · withdrawn · cancelled).
5. Section "Your requests" (or "All requests"): table Request, Status (badge), GPU, GPU·h, Region, Start window, Your limit, Index at request, Offer, Account (all-accounts view only; "operator" when none), Created. Rows link to /reserve/<id>. CSV "opengrid-price-lock-requests.csv". Note: "Index at request = the OpenGrid index level captured when the request was made (indicative). An offer binds only in a signed order form." Empty: "No price-lock requests yet." plus "Use the form above: ...".

#### Submit flow
- Client-side validation first. Then dialog "Submit price-lock request": "OpenGrid reviews the request. An offer is made only when provider capacity backs it; nothing is reserved, launched or billed by submitting." Table: GPU, GPU-hours, Start window, Region, Your limit (with "up to $total"), then the banner text. Confirm "Submit request".
- POST /v1/reservations/requests with an Idempotency-Key ("Submitting…"). Success navigates to /reserve/<new id>. Unknown outcome: "Outcome unknown. Retry (same Idempotency-Key): the server replays the first answer instead of creating a second request." Server field errors are placed under the matching field.

#### Refresh
- List reloads every 30 s; "Reload" button.

#### Observations
- Defaults (8 GPUs, 5376 GPU-hours, $2.50) are prefilled; a user might submit without noticing the numbers.
- Indicative reference uses finance vocabulary ("settlement fixing", "raw level", "constituents") that a compute buyer may not know.
- The legal disclaimer appears three times (banner, dialog, notes).
- Error boxes on this page differ from the rest of the app (no Sign in button, raw "HTTP 401").
- "GPUs" vs "Total GPU-hours" relationship is only explained by the validation message.

---

### /reserve/{id} (Price-lock request detail)

- URL: /reserve/<request_id>.
- Who: the requesting customer (accept, withdraw) and the operator (review, offer, decline, fulfil, cancel). If GET /v1/reservations/<id> is 404 and the user is admin, it retries the admin endpoint (other accounts).
- Signed out: loading, then header "Price-lock request <id>" with "← Reserve" and the page's own error box (no Sign in button).
- Purpose: track one request through requested, under review, offered, accepted, fulfilled (or declined, expired, withdrawn, cancelled).

#### Layout
1. Header: title "N× GPU" + grey request id; subline "<GPU-hours> · <region or any region> · start <time> → <time>"; status badge; buttons: red "Withdraw…" (or "Decline offer / withdraw…" when offered; only if the server allows it), "Refresh", link "← Reserve", "Copy" (copies the id; shows "Copied").
2. The NOT A FINANCIAL CONTRACT banner (same as /reserve).
3. Flash area for action results and errors.
4. Lifecycle strip: requested → under review → offered → accepted → fulfilled; a closed state is inserted after the furthest step reached; skipped steps marked.
5. Stats: "Your limit" (with "up to $max total"), "GPU-hours" ("N GPUs at once"), "Offer" ("#seq · $total total" or "no offer yet"), "Offer valid until" ("valid · Xh left" or "not open"), "Created" ("operator" or "account N"), "Status since".
6. Section "Offer": without an offer, "No offer yet. OpenGrid makes an offer only when a provider capacity commitment backs it; until then there is no price." (or "No offer was made."). With an offer: title "Offer #N" + badge "open" / "accepted" / "not open" + badge "binds only in a signed order form"; warning "Above your limit" if relevant; green "Accepted: OpenGrid will contact you to sign the order form; nothing is billed by this action."; key/value rows Price, Total, Valid until, Terms, Supply basis, Offered. Button "Accept offer #N…" with note "Records your intent only: no launch, no charge, no payment. OpenGrid then sends the order form."
7. Section "Operator" + "admin" badge (admin only, only the actions the server allows): "Mark under review" (optional note), "Make offer…" / "Revise offer…", red "Decline…" (required reason "shown to the customer"), "Mark fulfilled…" (required "Order form / delivery reference"), red "Cancel…" (required reason). Note: "An offer needs a supply basis ... Every action is recorded with your reason."
   - Make offer dialog fields: "Price $/GPU·h" (prefilled with the customer limit, hint "> 0 and ≤ 1000"), "Valid until (ISO, UTC)" (prefilled now + 3 days as text, hint "15 minutes to 30 days ahead"), "Terms" (min 10 chars), "Supply basis (required)" (min 10 chars), "Note (optional)".
8. Section "Request": GPU, Total GPU-hours, Start window, Region group, Your max price, Notes, Closed because.
9. Section "Indicative reference at request time" (same block as on /reserve, captured at creation).
10. Section "Events · N": newest first, each with time, from → to badge, actor badge, reason, evidence text.

#### Money and confirmation
- "Accept offer" dialog: table GPU, Price, Total, Valid until + accepted text; required checkbox "I understand this records my intent only: nothing is launched, charged or paid, and the order form governs."; confirm "Accept offer". Idempotency-Key per intent.
- "Withdraw": dialog with optional reason, red "Withdraw"; Idempotency-Key.
- Admin actions are plain POSTs (no Idempotency-Key) after their dialogs.
- Unknown outcome: "Outcome unknown: try again. It reuses the same Idempotency-Key."

#### Refresh
- Every 30 s while not terminal; "Refresh" button.

#### Observations
- Customer and operator controls share one page; the operator section is only distinguished by a small "admin" badge.
- "Valid until (ISO, UTC)" is a free-text field expecting an ISO string, while the customer form uses date pickers.
- "Decline offer / withdraw…" merges two concepts in one button.
- Terms like "supply basis" and "offer_seq" ("#1") are internal.

---

### /deployments (Deployments list)

- URL: /deployments. Query: status=live|uncertain|pending|failed|terminated. /deployments?id=X redirects to /deployments/X.
- Who: customers see their own deployments; the operator sees what GET /v1/deployments returns for the operator.
- Signed out: header renders; the table area shows "Sign-in required Authentication required" with "Sign in" and "Retry".
- Purpose: every instance OpenGrid launched or is about to launch, with the risky ones first.

#### Layout
1. Header: title "Deployments", subline "Compute OpenGrid launched or is about to launch · transaction data"; right: "transaction" data-kind badge, segmented filter "All", "Live", "Uncertain", "Pending approval", "Failed", "Terminated", button "Reload", primary link "New route →" (/route).
2. Alarm (only if any): red box "N deployment(s) in an uncertain or failed-termination state: an instance may exist and bill. Open each one; reconciliation evidence is on the detail page."
3. Stat strip: "Deployments", "Live" (an instance may exist), "Running", "Uncertain" (needs evidence), "Pending approval", "GPU-hours observed".
4. Table (sorted by attention: uncertain, live, pre-launch, then the rest): coloured dot, Deployment id, State badge, Purpose ("validation" badge or "customer"), Provider (or "not placed"), GPU, GPUs, Quote $/GPU·h, Execution $/GPU·h (n/a with reason), Created, Runtime, Auto-terminate (live countdown for live ones), Failure. Whole row links to the detail page. CSV "opengrid-deployments.csv". Filter empty: "No deployments match this filter."
5. Note: "Quote = what the route quoted; execution price = what the provider reports charging. Never merged. Uptime is OpenGrid-observed. State machine →".

#### States
- Loading: "Loading deployments…".
- Empty: "No deployments yet." + "A deployment exists once POST /v1/route quotes a provisionable listing under SUPERVISED or LIVE execution. In PREVIEW_ONLY the route returns not_provisioned and creates none." + buttons "Preview a route →" and "Execution safety".

#### Refresh
- Every 30 s; "Reload".

#### Observations
- Empty state speaks in API terms (POST /v1/route, PREVIEW_ONLY, not_provisioned).
- Two price columns are often n/a for execution price; the n/a reason is only in a tooltip.
- No actions on the list itself (stop/terminate only on the detail page).

---

### /deployments/{id} (Deployment detail)

- URL: /deployments/<deployment_id>.
- Who: the owner (customer) and the operator; admins get extra trace, validation evidence and provision error detail.
- Signed out: header "Deployment <id>" with "← Deployments" and the "Sign-in required" box with "Sign in" and "Retry".
- Purpose: everything about one deployment: state, cost, timing, SSH access, events, and the controls to stop or terminate it.

#### Layout, top to bottom
1. Header: title "Deployment <id>"; subline provider link, "N× GPU", region, "validation" badge or "customer", and a link to the route ticket (/route?rr=<id>). Right: state badge and buttons:
   - "Refresh from provider" (read-only; asks the provider now; then "Refreshed from the provider. ..." or "Not refreshed: <reason>").
   - "Stop…" (only if the provider supports stop and the state is running/degraded with an instance id).
   - Red "Terminate…" (or "Cancel request…" before launch); hidden in terminal states.
   - "Copy" (id).
2. Flash area (action results).
3. Billing alarm (red), when the deadline has passed while live: "PAST DEADLINE / POSSIBLY BILLING ... Terminate now and confirm in the provider console." or in uncertain states: "POSSIBLY BILLING ... Treat it as billing until reconciliation or the provider says otherwise." with a small "Terminate…" button.
4. State note banner for non-happy states (provider_timeout, launch_unknown, orphan_suspected, credentials_unavailable, termination_failed, degraded, provision_failed, provider_rejected, quote_expired, rejected), each with a one-line explanation plus the failure reason.
5. State-machine strip (each state as a step; current highlighted; skipped greyed).
6. Stat strip: "State since", "Runtime", "Max runtime" (source), "Auto-terminate" (live countdown, "PAST DEADLINE" when over), "Accrued (est.)" (price × GPUs × observed runtime), "Last checked", "Reconciled".
7. Two columns:
   - "Cost: four numbers, never merged": "Quote", "Expected cost" (estimated), "Provider-reported" (execution), "OpenGrid transaction". Then, after reconciliation, a table: Quote error, Effective rate, Billing rounding, Unexpected fees, Runtime billed, Reconciled. Before: "Cost reconciliation: reconciled after termination." or similar.
   - "Prices per GPU-hour · credential": Observed market, List, Quote, Execution, "Credential (pinned)" ("BYO: the customer's own provider account" or "OpenGrid-managed provider account" + ref), Instance, Client name, Approval (mode, by whom, when), "Limits overridden" (if so).
8. "Runtime ceiling · lifecycle timestamps · SSH access": Max runtime, Auto-terminates (exact local and UTC time + "in 1h 04m" or "PAST DEADLINE by ..."), Terminate requested, Provider created, Provider running, Provider terminated, Billable start, Billable end ("open: may still be billing"), Billable basis; next to it the "SSH access:" block (customer key fingerprint, operator access).
9. "Limit violations at approval" table (if any).
10. Two columns: left "Events · N" (timeline: time, from → to, actor badge, reason, evidence) and "Provision attempts · at most one per deployment" (table Provider, Listing, Outcome, Error, HTTP, Latency ms, Started, and "Detail" for admins; or "No provision call yet: it happens once, after approval."). Right: "Routing quality" (Winner, Runner-up, Market median, Expected savings, Realized savings, Quote error; or "No routing-quality row for this deployment yet ..."), "Validation evidence" (validation deployments only; checklist of steps; "Admin only." for others), "Design-partner feedback".
11. Feedback form (only after terminated; before: "The feedback form opens once the deployment is terminated."): four Yes / No / Not sure questions ("Would you have chosen this provider yourself?", "Was the price better than what you normally pay?", "Was setup easier?", "Would you route the next workload through OpenGrid?"), "What broke?" textarea, "Notes" textarea, button "Send feedback" / "Update feedback", status "saved · thank you". Note "Opinions only: never used in reliability or savings figures."
12. Admin only: collapsible "Trace · N steps (admin) · rr_..." with every internal step.
13. Footer note: "Data kinds: quote / execution / transaction are never mixed. Execution safety →".

#### Money and confirmation
- "Terminate…": dialog "Terminate <id>?" with body "Terminates the instance on <provider> (N× GPU)." and "OpenGrid marks it terminated only when the provider confirms ... One Idempotency-Key per click: a retry after a network error cannot terminate twice."; required checkbox "I understand the instance and its local data are destroyed at the provider"; red "Terminate".
- Before launch, "Cancel request…": "Cancel <id> before launch?", "Nothing has been launched; this cancels the request (state → rejected).", confirm "Cancel request" (no checkbox).
- "Stop…": "Stop <id>?" with warning "A stopped instance may keep billing (storage or full price, per provider). Only terminate is guaranteed to end spend."; confirm "Stop".
- Results: "Terminate requested" / "Stop requested" + note. Unknown outcome: "Outcome unknown. Retrying reuses the same Idempotency-Key (ogui-xxxx…), so the server replays instead of acting twice."

#### Refresh
- Reloads every 15 s until the deployment is terminal. Countdowns tick every second.

#### Observations
- Very long page with many overlapping time and money concepts (quote, expected, provider-reported, transaction, observed, list, execution, accrued est.). Hard to find "how much did this cost me".
- Some section titles read like specs ("Cost: four numbers, never merged", "Provision attempts · at most one per deployment").
- The feedback section says the deployment page "asks five questions" on /onboarding, but the form has four yes/no questions plus two text fields.
- The page shows internal fields (client name, credential ref, billable basis "provider_running_at") to customers.
- Event timestamps are built by appending seconds to a formatted date-time string, which may look odd.

---

### /onboarding (Onboarding)

- URL: /onboarding. Query: account=<id> (admin preview of a partner's progress).
- Who: design partners (customers) getting to a first route; the operator can preview any partner.
- Signed out: loading, then "Onboarding" title and "Sign-in required Authentication required" with "Sign in" and "Retry".
- Purpose: a numbered checklist from account to first supervised deployment, plus the partner profile form.

#### Layout
1. Header: title "Onboarding", subline "From account to a first supervised route" (or "Previewing <Company> (account #N) · actions on this page run as you, not as the partner"). Right: admin-only select "Signed-in account" / "Preview: <Company>" (switches ?account=), progress text (for example "3/7"), "complete" badge.
2. Numbered steps (done ones show a check and "done" badge, the next one a "next" badge, with grey evidence text):
   1. "Account created": one line of explanation.
   2. "Create an API key": key name input (default "first-route"), checkbox "route:execute (request routes; launches still need OpenGrid approval)", button "Create API key". Creates a key with data:read, route:preview, deployments:read, deployments:write (+ route:execute if ticked). Shows the secret once: "Copy this key now. OpenGrid stores only a hash and cannot show it again.", the key, "Copy key", prefix and scopes. When previewing a partner: a note pointing to /admin/partners.
   3. "Connect provider credentials" (badge "optional"): explains Managed (default) vs BYO; button "Manage credentials →" (/keys).
   4. "Run a route preview": "Prefilled from the profile: 1× h100-80gb-sxm5 ..." (falls back to H100 if no profile GPU), button "Run preview", "free · never calls a provider". Result strip: "Winner", provider, GPU, price, observed badge, "OpenGrid can provision" or "market data only", market median, percent vs median, audit link.
   5. "Understand why the provider won": after the preview, a list of factors with "weight% × value = +contribution", imputed badges, runner-up line, and "Full breakdown on /route →". Before: "Run the preview: each factor's weight × value is shown, imputed factors flagged."
   6. "Request a supervised deployment": explanation and primary link "Open the order ticket →" (/route prefilled from the profile).
   7. "Tell us how it went": "After termination, the deployment page asks five questions: provider choice, price, setup, next workload, what broke."
3. Section "Design-partner profile": grid of fields "Company", "Technical contact", "Contact email", "Preferred GPUs (slugs, comma-separated)", "Regions (US, Europe, …)", "Workload type", "Normal provider", "Normal $/GPU·h (what you pay today)", "Max $/GPU·h", "Expected GPU count", "Expected duration (h)", "Latency requirements", "Storage requirements", "Network requirements". Button "Create profile" or "Save profile"; status "saving…", "saved", "nothing changed". Note: "Normal price is partner-reported, never shown as an OpenGrid observation. ..." Saves via POST/PATCH /v1/partners/me, or PATCH /v1/admin/partners/<id> when previewing.

#### Money
- Nothing here spends money. "Create API key" has no confirmation dialog.

#### Refresh
- None (manual). Saving the profile reloads the page data.

#### Observations
- Step 5 has no completion marker (no server step) so it never shows "done".
- Step 7 text says "five questions" but the form has four plus free text.
- "Preferred GPUs (slugs, comma-separated)" asks users to know internal slugs.
- The admin "Preview" mode keeps the "Create API key" step text but swaps the control, and the header warns that actions run as the operator; easy to confuse.
- Step 4 runs a real preview each time the page redraws once it has been run (saving the profile re-runs it).

---

### /admin/execution (Execution control)

- URL: /admin/execution. Query: acct=<id> (account limits), vdep=<deployment> (validation evidence to follow), matrix=1 (open the capability matrix).
- Who: operator/admin only. Signed out or non-admin: title plus "Not allowed admin scope required".
- Purpose: the operator's control room: uncertain deployments, orphans, the global kill switch, execution mode, approvals, live deployments, provider flags, validation, reconciliation, cost guards and the audit log.

#### Layout, top to bottom
1. Header: title "Execution control", subline "Mode, kill switches, providers, orphans and reconciliation · every change needs a reason and is logged". Right: mode badge ("mode SUPERVISED" etc.), link "First-route checklist →", button "Reload".
2. Uncertain deployments: green "No uncertain deployments. Every deployment's state is backed by provider evidence." or red block "N UNCERTAIN DEPLOYMENTS · an instance may exist and bill · ..." with table State, Deployment (link), Acct, Provider, GPU, Client name, Instance ("unknown"), Since, Accrued est., Reason, and a red "Force-terminate" button per row.
3. Orphans: "No open orphans · every instance the providers list is accounted for" or "N OPEN ORPHANS ≈ $x/h at the provider's listed price ... OpenGrid never auto-terminates an instance it cannot prove it owns", plus the data source. Table: Status, Provider, Instance, Name, Kind, Deployment, Provider state, $/h, First seen, Seen, Ours? ("provably" / "unproven"), Credential, Resolve buttons "terminate" (red), "adopt" (disabled without a deployment), "ignore". CSV export.
4. Two columns:
   - "Global kill switch": big button "STOP ALL LIVE PROVISIONING" (or, when active, "ALL LIVE PROVISIONING IS STOPPED since ... by ...: <reason>" and "Restore by choosing a mode on the right (reason required)."). Note: "Sets the mode to DISABLED: no new launch anywhere. Monitoring, status polling, reconciliation, stop and terminate keep working."
   - "Execution mode": the mode banner, four mode cards "DISABLED", "PREVIEW ONLY", "SUPERVISED", "LIVE" with descriptions (stored one pressed, effective one marked; SUPERVISED and LIVE say "capped to PREVIEW ONLY by the env ceiling" when the env flag is off), and a table "Env ceiling" (ROUTING_LIVE_PROVISIONING true/false) and "Stored" (mode, who, when, reason).
5. "Approval queue · N": table State, Deployment, Purpose, Acct, Provider, GPU, Quote $/GPU·h, Limits ("within limits" or codes), Requested, and "Review ticket →" (to /route?rr=). Empty "Nothing waiting for approval."
6. "Live deployments · N · ≈ $x/h at quote": same columns plus Runtime, Auto-terminate (countdown), Accrued est., "Force-terminate". Empty "Nothing is running."
7. "Temporary provider resources · leftover og-* SSH keys etc.": note with counts; table Provider, Type, Name, Resource id, Deployment, Status, Age, Last error, and "Cleanup…" (red) when provably OpenGrid's, else "not ours?". CSV.
8. "Provider execution flags": table Provider, Lvl, Adapter ("validated" or "simulated"), Validated (date + "evidence" link), Credentials ("configured" / "none"), Supervised toggle ("ON"/"off"), Live toggle, Kill switch ("Kill" red, or "killed" badge + "Unkill"), Note. Toggles are disabled to turn on until the adapter is validated. Note: "Customer launches need a validated adapter AND the supervised ... or live flag. validated is set only by a complete, provider-confirmed validation cycle; it can be demoted, never typed in."
9. Collapsible "Capability matrix · what each adapter can really do (evidence on hover)": grid of capabilities (quote, live availability, launch, ssh key injection, ... error semantics, provider-specific risks) by provider, cells YES / NO / PARTIAL with evidence tooltips.
10. Two columns:
    - "Validation launcher": "Validation cap (not overridable): 1 instance · 1 GPU · ≤ $3.00/h total · ≤ 30 min runtime · auto-terminate · admin approval". Preconditions panel "Validation preconditions · <Provider>" with buttons "Send test ops alert" and "Re-check", a green/red checklist, and "All preconditions green: the launcher is enabled." or "N precondition(s) red: the launcher stays disabled.". Then provider select ("<Provider> · validated/simulated"), "GPU slug (optional)" input, button "Validate provider…" (disabled until green; "disabled until every precondition is green"). Note on the process. Table of validation deployments (State, clickable id to follow evidence, Provider, GPU, $/GPU·h, "Approve on ticket →" or "Open →"). Evidence panel "Evidence · <id>" with step checklist, badge "validated" or "N step(s) missing", button "Re-check & mark validated" (disabled until complete).
    - "Reconciliation": "Last run X ago · ok/failed" or "never run", source, button "Run now". Table #, Started, Trigger, Provider, Status, Findings (badges), Error; clicking a row appends its raw findings JSON below. Empty: "No reconciliation run recorded. The job runs every few minutes in a deployed server (not with OPENGRID_NO_JOBS)."
11. "Account limits (cost guards)": select "Choose an account…" (partners) or "or account id" input + "Load". Then stats Active deployments, Active GPUs, Month spend, Monthly limit; inputs "Max $/GPU·h", "Max $/h", "Max total $", "Max GPUs", "Max active", "Monthly $", "Provider allowlist", "Region allowlist" (each with a source badge, placeholder "default"); button "Save changes…" with note "blank = settings default · checked at quote AND again at launch; exceeding blocks the launch unless an admin overrides with a reason".
12. "Control audit log": table At, Action (badge coloured by risk), Target, Actor, Reason, After. 60 rows shown, CSV "opengrid-control-log.csv". Empty "No control changes recorded."

#### Confirmations (all need a reason of 3+ characters, all logged)
- Kill switch: dialog "STOP ALL LIVE PROVISIONING", "Type STOP to confirm" + reason; red "Stop all launches".
- Set mode: "Set execution mode X", mode description, warning "Check the first-route checklist is green before enabling launches." for SUPERVISED/LIVE; LIVE also needs "Type LIVE to confirm"; confirm "Set X" (red for SUPERVISED and LIVE).
- Force-terminate: "Type the deployment id to confirm" + reason; Idempotency-Key.
- Orphan terminate: "Type the instance id to confirm: <id>" + reason; adopt/ignore need only a reason; Idempotency-Key.
- Resource cleanup: dialog "Delete <type> <name> on <Provider>?" with explanation, red "Delete now" (no reason field); Idempotency-Key.
- Provider flags: "Enable/Disable supervised|live (no per-launch approval) launches on <Provider>"; enabling live needs "Type <provider> to confirm"; reason.
- Provider kill/unkill: reason; "Kill provider" / "Lift kill switch".
- Validation start: dialog "Validate <Provider>" ("... creates a validation deployment pending your approval (max 30 min, auto-terminated). Nothing launches until you approve the ticket."), confirm "Create validation route"; Idempotency-Key. Result: "Validation route created: <id>" + "Approve on ticket →".
- Mark validated: no dialog; Idempotency-Key.
- Send test ops alert, Run now (reconciliation, which "may terminate provably-ours orphans"): no dialog.
- Save limits: dialog showing the JSON diff + reason; "Save limits".

#### Refresh
- Deployments, orphans and resources every 20 s. Everything else on "Reload" or after an action. Missing endpoints show "Not available yet" rather than empty.

#### Observations
- The page is a very long single scroll with about 12 major panels; highest-risk items are at the top, but day-to-day controls (approval queue) sit below the kill switch and mode.
- Heavy jargon: env flag names (ROUTING_LIVE_PROVISIONING, OPENGRID_NO_JOBS), "adopt", "provably ours", "og-*", "Lvl", raw endpoint names as data sources.
- "Run now" for reconciliation can terminate orphans but has no confirmation, unlike every other destructive action.
- "Mark validated" and "Send test ops alert" also act without a dialog.
- Orphan resolve buttons are lowercase ("terminate", "adopt", "ignore") unlike the rest of the app.
- Errors from actions are appended to the bottom of their panel and accumulate; nothing clears them except a reload.
- The raw findings JSON appended on row click also accumulates.

---

### /admin/checklist (First real route checklist)

- URL: /admin/checklist. Query: provider, rr, acct (all URL-synced).
- Who: operator/admin only. Signed out or non-admin: "Not allowed admin scope required".
- Purpose: a live go/no-go gate for turning on SUPERVISED execution for a provider.

#### Layout
1. Header: title "First real route checklist", subline "The gate before SUPERVISED execution · computed live, nothing cached". Right: provider select (only providers that can provision; defaults to vast), input "route_request_id (rr_…) optional", number input "acct" (account limits to check), button "Recheck", primary button "Probe provider" (one read-only API call with the configured credential).
2. Overall bar: "GREEN: ready for a supervised first route", "RED: do not enable SUPERVISED" or "NOT GREEN: unknown items are not green"; counts "N green · N red · N unknown"; provider, rr, "computed <time>", "· probed". Right: "Go to execution mode →" (primary, when green) or "Execution control" (when not).
3. Ordered checklist: each item with a coloured dot, bold label, status badge (red/unknown), "optional" badge if not required, mono evidence line, and for non-green items "How to fix" with a code-styled fix.
4. Note: the server's rule text + "First live route guide →".

#### States
- Loading "Computing the checklist…" / "Probing <Provider> (read-only)…"; "No provisionable provider on this server."; errors with "Retry".

#### Refresh
- None automatic; "Recheck" / "Probe provider", or changing an input.

#### Observations
- Inputs (rr id, account number) have no labels beyond placeholders; their effect is unclear.
- "Probe provider" is styled as the primary action though it is the optional deeper check.
- Fix text is shown as code, which suits engineers but not other operators.

---

### /admin/partners (Design partners)

- URL: /admin/partners. Query: account=<id> selects a partner. Code lives in web/pages/onboarding.js.
- Who: operator/admin only. Signed out or non-admin: "Not allowed admin scope required".
- Purpose: list design-partner accounts and their onboarding progress, create a new partner, and see each partner's deployments and feedback.

#### Layout
1. Header: title "Design partners", subline "Accounts onboarding to a first real route", primary button "New partner…" (toggles the form).
2. Created message (after creation): "<Name> created (account #N)." and the one-time key reveal ("Copy this key now ...", key, "Copy key", prefix and scopes) or "No key created."
3. Hidden form box "Create partner: account + profile + first key, in one step": the same profile fields as /onboarding, plus checkbox "Grant route:execute on the first key (every launch still needs OpenGrid approval in SUPERVISED)", button "Create partner". "company is required" if blank.
4. Partners table: Company, Acct, Status ("active" green), Contact (name · email), Onboarding (progress or "complete"), Next step, GPUs, Regions, Normal $/GPU·h (with "reported" badge), Max $/GPU·h, and button "Onboarding view" (/onboarding?account=N). Clicking a row selects it. CSV "opengrid-partners.csv". Empty: "No design partners yet. Create one: it returns the first API key once."
5. Detail for the selected partner: section "<Company> · account #N" with buttons "Cost guards →" (/admin/execution?acct=N) and "Onboarding view →"; two columns "Deployments · N" (State, Deployment link, Provider, GPU, Quote, Created; "No deployments yet.") and "Feedback · N" (Deployment, Own pick?, Price better?, Easier?, Route next?, What broke, Notes; or "No feedback yet (asked after each terminated deployment).").

#### Money
- None. Creating a partner issues an API key with no confirmation dialog.

#### Refresh
- None automatic.

#### Observations
- The detail panel loads up to 2000 deployments for all accounts and filters on the client.
- Two ways to open the onboarding view (table button and detail button).
- Row click vs "Onboarding view" button behaviour is not signposted.
- The creation form reuses the full 14-field partner profile; only Company is required, but nothing marks it.

---

### /admin/value (Value)

- URL: /admin/value. Query: days=7|30|90|3650 (default 30).
- Who: operator/admin only. Signed out or non-admin: "Not allowed admin scope required".
- Purpose: is OpenGrid creating value: savings vs market, routing quality, provider reliability, activation funnel, product usage.

#### Layout
1. Header: title "Value", subline "Savings, routing quality, reliability, funnel · real transactions only". Right: "transaction" badge, segmented "7D", "30D", "90D", "ALL" (re-renders the page).
2. "Economic value vs the market median": stats Deployments, With valid comparison ("same GPU, on-demand, median of ≥ 3 providers"), Customer savings, Mean savings, Median savings; three small tables by GPU, by Provider, by Strategy (n, Savings $, Mean %, Median %; "No valid comparisons yet."); "Excluded (no valid comparison): reason × n"; table "Per deployment" (Deployment link, Acct, GPU, Provider, Strategy, Market median, Selected with "quote"/"exec" badge, GPU-h, Savings % or the reason, Savings $). CSV "opengrid-economics.csv".
3. "Routing quality": key/value table Route requests (previews · routes), Routing success, Provisioning success, Expected savings vs median, Realized savings vs median, Savings vs previous provider, Provisioning latency, Quote accuracy (mean |error|), Within quote tolerance, Provider failure rate; each value shows "n=" or the status (for example insufficient sample). Table "Per route (newest first)": At, GPU, Mode, Winner, Runner-up, Median, Expected %, Realized %, Outcome (state badge linking to the deployment). 50 rows, CSV.
4. "Provider reliability (OpenGrid's own launches)": table Provider, Launches of ("customer" / "validation"), Launches, Launch success, Provisioning ms, Time to capacity, Unexpected terminations, API errors, Termination success, Quote accuracy, Score, Failed (rej / timeout / unknown). Or "No reliability data: No launches recorded yet ...". Note on minimum samples.
5. Two columns: "Activation funnel" (weekly table for 8 weeks plus total, stage-to-stage conversion; over 100% flagged) and "Product usage" (stats Visitors, Repeat, top 4 events; tables top pages, top searches, top GPUs viewed; note "anonymous ids only (sessionStorage), Do Not Track respected.").

#### States
- Each section has its own loading spinner and error box.

#### Refresh
- None automatic; changing the window re-renders.

#### Observations
- Reliability ignores the time window (always all time) while other sections follow it; the funnel is always 8 weeks; product usage is capped at 365 days. The "ALL" chip therefore means different things per section.
- Mostly tables of percentages with "n=" and internal status words; little visual summary.
- Metric definitions are only in row tooltips.
