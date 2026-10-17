# OpenGrid web UI: the complete page-by-page guide

This document describes every page of the OpenGrid website as built today (production commit `0bc4601`,
2026-10-16), after the visitor-shell, compute and developer redesigns and the multi-GPU recommendations. It
was written by reading the page source code, not from screenshots. Each page lists its layout top to bottom,
every button and link with its exact label, its states (loading, empty, error, signed out) and an
**Observations** list of real current issues found in the code.

## What OpenGrid is (context for the reviewer)

OpenGrid watches about 30 GPU cloud providers (27 polled live in production today), turns their prices into one market (GPU pages, indices,
events, news), recommends which GPU fits a workload ("Find Compute", now multi-GPU aware with estimated runtime
and whole-job cost), and can run a training job on the best provider through one API key, with every spend
approved by a human. AI assistants (Claude Code, Cursor, VS Code, Codex, claude.ai) can use it over MCP.

**Who uses it**
- **Visitors** (anonymous): public pages are on, so the market pages, Find Compute, Developers, the API
  reference and the Assistant work without signing in. The top bar says "Public access"; account pages carry
  a lock.
- **The operator** (the founder): signs in at `/login` with the site password; sees everything, including the
  Operator section of the menu.
- **Customers / developers**: use an API key (`opg_...`) from code, the CLI or an AI assistant. There is no
  self-serve sign-up and no payments yet: keys are issued by the OpenGrid team (requests via GitHub issues).

**Honest current state**
- Real GPU execution is built and tested but has never run on a real provider; production is in preview-only
  mode. The only job anyone can run today is the free simulated MNIST demo. Running your own code is not
  supported yet.
- Runtime and job-cost figures on Find Compute are theoretical estimates (spec-sheet throughput with stated
  assumptions), labelled as such; nothing is a benchmark.
- The Assistant runs without an AI model in production: it matches common phrasings.

## How this guide is organised

1. Part 1: the global shell (navigation, access indicator, status bar, search, design system) and the
   information pages (Home, Markets, Developers, API, Methodology, Assistant).
2. Part 2: the market-data pages.
3. Part 3: the customer journey: Find Compute, the assistant panel, My Jobs, New job, Job page, Route, API keys,
   Watchlists.
4. Part 4: execution and operator pages: Reserve, Deployments, Onboarding, Admin.

---

# Part 1: Global shell and information pages

Source read: web/index.html, web/core.js, web/app.css, web/devx.css, web/pages/home.js, overview.js, developers.js, api.js, methodology.js, assistant.js, api/pages.py, main.py (as of commit 0bc4601). Production setting assumed: PUBLIC_PAGES on. Anonymous visitors can open every page shell and all public data. Private data (jobs, keys, deployments, watchlists, admin, ops, /v1/me) needs the operator site login (the /login page, which always shows a browser password prompt) or an API key. Only /login triggers a browser password popup; every other private call answers 401 with a Bearer challenge, so the page shows an in-page message instead.

Vocabulary used below: "visitor" means signed out; "operator" means signed in with the site login (the only browser sign-in that exists). There is no customer browser account; customers use API keys from the CLI, MCP or curl.

### Global shell (every page)

**Who / sign-in:** everyone. The shell asks GET /v1/me once per page load (cached). For a visitor it fails quietly (401) and the shell stays in visitor mode; for the operator it switches the access indicator to "Operator", removes the lock icons and shows the Operator menu group.

**Purpose:** a dense, terminal-style frame: top bar with live price status, ticker and search; left menu; page content on the right.

#### Top bar (left to right)

1. **Menu button** (hamburger icon, aria label "Open menu" / "Close menu"). Only visible at 820px wide or less; opens the left menu as a full-width panel. Esc closes it; any navigation closes it.
2. **Brand** "OPENGRID" with a grid icon, links to "/". At 560px or less the word is hidden and only the icon shows.
3. **Status line** (hidden at 560px or less):
   - A coloured dot plus freshness text. Starts as "Connecting…". Then "Prices updated just now / 1 min ago / N min ago / 1 hour ago / N hours ago / N days ago" (green dot). Older than 10 minutes: "Prices may be out of date: last read N min ago" (stale tone). If the price service fails: "Can't reach OpenGrid" (red). Tooltip gives the exact local time ("Prices were last read from providers at ... (your time)"). The words are redrawn every 30 seconds.
   - "· N providers tracked" (from /polling). Hidden below 1600px wide. Tooltip: "OpenGrid reads prices from N cloud providers, the busiest every M min" plus "(price checks are paused on this server)" when polling is off.
4. **Ticker tape** (hidden at 1080px or less). Built from /market (24 hours) and /v1/tape:
   - Up to 16 price items: short GPU name in bold, lowest on-demand price per GPU-hour, and the 24-hour change as a green up or red down arrow with a percent (only GPUs with 2 or more providers). Each links to /gpu/{slug}. Tooltip explains "lowest on-demand price per GPU-hour across N providers, change over 24 hours".
   - Then up to 12 event items: provider name (or "Market"), short GPU name (or the event type), and a percent change. Each links to the GPU page, or /events.
   - Only whole items are shown; the rest collapse into a "+N more" link to /gpus ("See every GPU price"). Refits on window resize.
   - Reloads every 60 seconds while the tab is visible.
5. **Search / command bar**: text input, placeholder "Search GPUs, providers, pages", with a "/" key hint. Details below.
6. **Access indicator** button: grey dot plus "Public access" (just "Public" at 560px or less) for a visitor; blue dot plus "Operator" for the operator; "Signed in" for any other signed-in principal. Click opens a popover (details below).
7. **Theme toggle**: sun or moon icon, label "Switch to light mode" / "Switch to dark mode".

#### Left menu (in order)

Width about 168px. Groups with secondary links have a small chevron toggle ("Show GPU tools", "Show Find Compute pages", "Show market pages", "Show developer pages", "Show execution pages"). The group that contains the current page opens automatically and the current link is highlighted; a group the user opened by hand stays open (pinned).

- "Home" (/)
- Heading **"Explore"** (tooltip "Free to use, no account needed")
  - "GPUs" (/gpus), sub-links "Compare", "Explorer", "Heatmaps", "Providers"
  - "Find Compute" (/find), sub-link "Ask the assistant" (/assistant)
  - "Markets" (/markets), sub-links "Indices", "Opportunities", "Events", "News", "Watchlists" (lock: "Needs an OpenGrid account")
  - "Developers" (/developers), sub-links "API docs" (/api), "API keys" (/keys, lock), "Onboarding" (/onboarding, lock)
- Heading **"Your workspace"** with a lock icon (tooltip "Needs an OpenGrid account (invite only for now)")
  - "My Jobs" (/jobs, lock), sub-links "Deployments", "Reserve", "Route" (all locked)
- Utility link "Methodology" (/methodology)
- **"Operator"** group, hidden unless /v1/me says the viewer is the operator or holds an admin scope: "Execution", "Checklist", "Partners", "Value", "Ops".
- Footer hint: "/ search" and "? keyboard shortcuts" (hidden on small screens).

Locks: every link marked as needing an account shows a small lock at its right edge until the shell knows the viewer is signed in (the lock is CSS, removed when the body gets the signed-in class). Locked links are still clickable; the target page explains access itself.

#### Access popover

- Visitor: heading "You are browsing as a visitor"; "Everything under Explore in the menu is free and needs no account."; a list: "Free now" (GPU prices, comparisons, market data, Find Compute recommendations and the assistant), "Account" (My Jobs including the free simulated demo, API keys and watchlists; invite-only), "Real GPU runs" (early access, each launch waits for OpenGrid to approve the price). Buttons "Explore GPU prices" (/gpus) and "Use it from your AI assistant" (/developers). Footer: "OpenGrid staff: operator sign-in" (link to /login?next=current page).
- Operator: heading "Signed in as operator"; "every page is open to you, including the Operator tools at the bottom of the menu"; "To sign out, close this browser window: the site login is remembered until then."
- Closes on outside click, Esc, or clicking any link inside.

#### Search / command bar

- Press "/" anywhere (when not typing in a field) to focus it. Arrow keys move through suggestions, Enter runs, Esc clears and leaves.
- **Empty bar** (on focus) shows group "Go to": "Home", "GPUs: every price", "Find Compute for my project", "Ask the assistant", "Markets", "Developers", "My Jobs" (lock, hint "needs an account"); then group "Try typing": "h100" (open the H100 market), "c h100 h200" (compare two GPUs), "ask cheapest gpu for fine-tuning llama 8b" (ask in plain English). Clicking an example fills it in. Footer: "Type a GPU, provider or page. Press ? for every shortcut."
- **Typing** shows up to 12 suggestions: an exact command match first ("↵ <text>"), up to 3 menu pages (fuzzy on labels and hidden aliases, locked ones say "page · needs an account"), up to 2 GPU families ("GPU family · N variants"), up to 6 GPUs ("GPU · N providers" or "GPU · none priced now"), up to 4 providers, commands whose syntax starts with the text, and, for any multi-word text that is not a command, "Ask the assistant: "<text>"" (opens /assistant?q=...).
- **Commands**: "g <gpu>" open a GPU market (family or variant aware: "g h100" opens the family page, "g h100 sxm" the SXM5 variant; unknown text goes to /gpus?q=); "p <provider>"; "c <gpu> <gpu>" compare; "r <gpu> [n] [region]" route preview; single letters "e" events, "n" news, "o" opportunities, "x" explorer, "i" indices, "h" heatmaps, "m" methodology, "d" deployments; words "home", "gpus", "find", "markets", "developers"; "ask <question>"; "theme [light|dark]"; "?" or "help".
- **Shortcuts dialog** ("?" key or "help"): title "Search and shortcuts", "Esc to close"; sections "Keys" (/, ?, arrows plus Enter, Esc), "Find something (type in the search box)" (commands with arguments), "Jump to a page" (the rest, alphabetical); "Close" button. Click on the backdrop closes it.
- Searches are counted by the anonymous analytics tracker (no cookies, off when Do Not Track is set).

#### Shared states (used by every page)

- Loading: spinner plus text, default "Loading…".
- Empty: one grey line, default "Nothing to show.".
- Error (OG.error): a title and one sentence, plus a "Technical details" fold holding the HTTP status, message and path. Titles: 401 "Sign-in required" (lock icon, text "This needs an OpenGrid account. Accounts are invite-only for now; there is no self-serve sign-up yet.", buttons "Explore free pages" (to /) and link "OpenGrid staff: operator sign-in"); 403 "Not included in your access"; 404 "Not found"; 429 "Too many requests"; network "Could not reach OpenGrid"; 5xx "OpenGrid had a problem"; other "Could not load this". Retryable errors get a "Try again" button when the page supplies a retry.
- Access note (OG.accessNote), drawn before data loads on private pages: bold label plus sentence. "Free, no account", "Needs an account" (lock), "OpenGrid staff only" (lock), "Simulation".
- Insufficient data: bold "Insufficient data" plus the server's reason, never a made-up number. Inline "n/a" carries the reason on hover. Stat strips fold missing values into one trailing cell "Not enough history yet" / "Not available yet".
- Data-kind badges "observed", "estimated", "transaction" link to /methodology/data-kinds.
- Not-found page (any unknown path): heading "Page not found", sub "There is no page at /path", text "The link may be old or mistyped. Try one of these, or press / to search.", buttons "Home", "GPU prices" (/gpus), "Find Compute" (/find). Browser tab title "Not found · OpenGrid".
- Old hash links from the first UI (#/market, #/market/<gpu>, #/routing, #/deploy) are redirected to the new paths.

#### Theme

Dark by default, or the OS preference on first visit; the choice is saved in the browser (localStorage "og-theme") and applied before first paint, so there is no flash. Toggle with the top-bar button or the "theme" command.

#### Design tokens (web/app.css, summary)

- Style rules stated in the file: thin 1px rules, no gradients, no shadows except focus, tabular monospace numbers, small-caps labels; all text colours meet WCAG AA on the background in both themes.
- Type scale: --fs-2xs 11px (mono labels, badges), --fs-xs 12px (captions), --fs-data 12.5px (table cells), --fs-sm 13px (notes, controls), --fs-base 14px (body), --fs-md 15px (emphasis, nav), --fs-lg 17px (stat values, sub-headings), --fs-xl 21px (page titles), --fs-2xl 24px (big figures), --fs-3xl 34px (homepage headline). Fonts: system sans for text, ui-monospace stack for numbers and code.
- Controls: --ctl-h 32px, --ctl-h-sm 26px, radius 4px; on touch screens every button, field and toggle is at least 44px (small buttons and chips 36px).
- Layout: top bar 46px; left menu 168px; content scrolls in its own pane.
- Colours (dark): background #090b0e, surface #0e1217, raised #131920, text #e7ebef with three lighter greys; up green #2fbf71, down red #ef5350, warning amber #f5a524, accent blue #4d94ff; plus families of blue, green, amber, red and purple tints for badges and banners, and chart colours. Light theme swaps every token (background #f5f6f8, surface white, accent #1a64cc, darker green and red).
- Breakpoints: 1600px hides the provider count; 1440 and 1280 shrink the search box; 1080 hides the ticker and stacks two-column grids; 820 turns the menu into a hamburger panel; 560 hides the status line, the "/" hint and the brand word.
- web/devx.css (developer pages and the assistant panel) uses only these tokens and dx- prefixed classes.

#### Observations (shell)

- The "Explore" heading says "Free to use, no account needed", yet three of its links are locked ("Watchlists", "API keys", "Onboarding").
- "Needs an account" is the label everywhere (nav locks, popover, 401 errors, command bar), but there is no customer account to sign into in the browser: the only sign-in is "operator sign-in". A non-staff visitor who clicks a locked page has no path forward except the Developers page's GitHub key request, and the error state never mentions API keys.
- Three different words for the same gate: "Needs an account", "Needs sign-in" (assistant), "operator sign-in" / "Sign in" (assistant, Developers), "API key" (Developers, API).
- No sign-out control: the operator is told to close the browser window.
- Freshness is not always "last price read": Home and Markets feed the status bar with /v1/overview meta.as_of, which is the time the response was built (server now(), cached up to 60s), not when providers were last read. On those pages the bar can say "Prices updated just now" even if price polling has stalled. The shell itself uses /market t1 (true last read), so the meaning changes by page.
- The ticker has two formats. The shell draws price items plus event items "Provider GPU ±%"; /markets replaces the whole tape with its own items (event titles with a severity dot). Both reload every 60 seconds, so on /markets the tape can flip between the two formats. Shell event items for events without a percent (sold out, restock) show a bare "-".
- Command bar shortcuts "d" (deployments) and "r <gpu>" (route preview) lead to locked pages but show no lock in suggestions or in the shortcuts dialog; the "Ask the assistant" fallback does not appear for multi-word text starting with a single letter and a space (for example "a cheap gpu").
- Label drift between menu, page heading and browser tab: "Markets" / "Market overview" / tab "Markets"; "API docs" / "API reference"; "Ask the assistant" / "OpenGrid Assistant" / tab "Assistant"; "My Jobs" / "Jobs".
- devx.css is described as loaded only for developer pages, but developers.js injects it at startup, so it loads on every page.
- Leftover CSS: app.css still has .pg-api .api-grid / .api-toc rules that the current /api page does not use (it uses dx-api-*); --nav-w grows from 168px to 172px at 1080px and below, the opposite of the usual direction.

### / (Home)

**URL:** / . Tab title "Find and run GPU compute · OpenGrid".

**Who / sign-in:** first-time visitors; everything is public. The only difference for the operator is the demo action (see below) and the absence of the lock tag.

**Purpose:** explain what OpenGrid is, what works today, and give three starting actions plus a small live price board.

**Layout, top to bottom**

1. **Hero, left column**
   - Eyebrow "OpenGrid · live GPU cloud prices".
   - Headline "Compare GPU prices. Find the right compute. Run supported workloads."
   - Sub text "OpenGrid reads the public price lists of N GPU cloud providers every few minutes. See what an hour of GPU time costs everywhere, find the right GPU for your AI project, and run supported jobs safely." (N filled from /polling.)
   - Box "What works today" with five rows and a status icon each: "Live GPU prices and comparisons" (free, no account), "GPU recommendations for your project" (free, no account), "Training demo (a simulation, no charge)" (needs an account), "Real GPU runs, each approved by OpenGrid" (early access), "Running your own code" (not yet).
2. **Hero, right column: three action cards** (each with title, arrow, one line and tags)
   - "Explore GPU Prices", "Every GPU, cheapest provider first, with price history.", tag "Free · no account". Link to /gpus. Highlighted as primary.
   - "Find Compute for My Project", "Describe your model and job; get the GPUs that fit, ranked by price.", tag "Free · no account". Link to /find.
   - "Try the Free Training Demo", "Watch a small AI model train, step by step.", tags "Simulation · free" and (visitor only) lock "Needs an account".
     - Visitor: it is a button that expands an explanation box: "The demo needs an OpenGrid account", why (runs in your own workspace, accounts are invite-only), what it does (a small PyTorch model learns handwritten digits, MNIST; simulation, nothing charged), links "How jobs work →" (/methodology/jobs) and "Have an API key? Run it from your AI assistant →" (/developers).
     - Operator (once /v1/me answers): the card becomes a direct link to /jobs/new?preset=mnist&sandbox=1 and the explanation box is removed.
3. **Stat strip** (5 cells): "GPUs priced" (sub "on-demand, in stock or stock unknown"), "Listings you can rent now" (observed badge), "Cheapest GPU-hour" (sub "<GPU> at <provider>"), "Sold out everywhere" (sub "GPUs with listings, none purchasable"), "Providers tracked" (sub "public prices and stock"). Missing values show n/a with a reason.
4. **Market row, left: "GPU prices right now"** with an "observed" badge, "as of HH:MM:SS", links "All GPUs →" (/gpus) and "Market dashboard →" (/markets). A compact sortable table of the 10 GPUs that the most providers sell: "GPU" (link), "Low $/GPU·h", "Cheapest at" (provider with logo, link), "Prov" (provider count), "24h" (matched median change, n/a with a reason when fewer than 2 providers were priced at both times), "24h low" (sparkline). Click a row or column header to open the GPU or sort. Empty: "No GPU is priced right now."
5. **Market row, right: "Price indices"** with link "All indices →". Four rows (GPU Compute, H100 80GB SXM5, H200 141GB SXM5, A100 80GB SXM4 indices): name, level (/GPU·h or pts, or "not published"), 24h change. Each links to /indices/{id}. Note: "An index is one number for the price of a GPU type across providers, like a stock index. These are list prices per GPU-hour, not quotes." with links "Data kinds" and "Index methodology".
6. **"How OpenGrid works"**: three numbered steps.
   - "01 Watch N providers" (observed badge), explanation, link "Providers and coverage →".
   - "02 Recommend for your project" (estimated badge), link "Find Compute →".
   - "03 Run safely" (badge "approved"): a price check is free; a real run needs OpenGrid approval, a spending cap and proof of shutdown; "Today the one supported job is a small training example." Link "How runs are kept safe →" (/methodology/execution-safety).
7. **"Also" row**: links "Use OpenGrid from your AI assistant, CLI or API", "Compare two GPUs", "Market events", "How every number is computed".

**States:** each block loads separately ("Loading live market…", "Loading prices…", "Loading indices…"). If /v1/overview fails, the board shows the standard error with "Try again" and the stat strip disappears; the status bar shows "Can't reach OpenGrid". If the API marks the board unavailable: "Market board unavailable" plus reason. Indices: "Indices did not load. Try again in a moment." or "No benchmark index has data yet.".

**Refresh:** overview, sparklines and indices reload every 60 seconds (paused while the tab is hidden).

**Observations**

- Column headers "Prov" and "Low $/GPU·h" are terse for a first-time visitor (the page is aimed at newcomers; tooltips explain them only on hover).
- The demo card says "Needs an account" and "Accounts are invite-only", but there is no way to request an account here; the only link is to API keys on /developers. The demo is the one action a newcomer cannot complete.
- Mixed capitalisation: action titles are Title Case ("Explore GPU Prices", "Try the Free Training Demo") while every other heading on the site is sentence case.
- "as of HH:MM:SS" next to the board is the response time, not the time prices were read (see shell freshness note); the status bar is driven by the same value.
- "GPUs priced" sub line "on-demand, in stock or stock unknown" is hard to parse.

### /markets (Market overview)

**URL:** /markets. Tab title "Markets · OpenGrid". Menu: "Markets".

**Who / sign-in:** public; no difference for the operator.

**Purpose:** a dense trading-style dashboard of the on-demand GPU market: indices, direction, movers, availability, spreads, recent changes, events and news.

**Layout, top to bottom**

1. **Header** "Market overview", sub "On-demand GPU market · observed list prices per GPU-hour", a mono line "hour <date time> · Nh recorded since <date time>" (or "no market history"), button "Methodology" (/methodology/events#overview).
2. **Indices strip**: up to 9 benchmark cells (GPU Compute, H100 SXM5, H200 SXM5, B200, A100 SXM4, L40S, H100 class, H200 class, A100 class), each with short name, level (/GPU·h or pts), 24h and 7d change, or "not published". Each links to /indices/{id}. Last cell "All N indices →".
3. **Summary line** separated by "│": "24h N up · N down · N flat of N GPUs · median move X%"; "Markets N live · N sold out · N GPUs sold out everywhere"; "Listings N available (±N 24h)" or "(24h n/a)"; "24h N sell-outs · N restocks".
4. **Panel grid** (5 columns on wide screens, 3 below 1280px, 2 below 1080px). Each panel has a title, a data-kind badge and sometimes a "?" rule tooltip, and shows up to 6 rows:
   - "Top gainers · 24h" and "Top losers · 24h": GPU, Median, 24h, Prov.
   - "Unusual moves": GPU, 24h, z, n.
   - "Most volatile · 7d": GPU, "σ 1h", "7d range".
   - "Widest spreads" (title links to /heatmaps): GPU, Low, High, Spread, Prov.
   - "Most liquid / covered": GPU, Priced, Avail, Listings (available/live).
   - "Availability gaining · 24h" and "Availability losing · 24h": GPU, Prov (then→now), Δ, Listings (then→now), Δ.
   - "Newly available · 7d": GPU, First at, Since, Low.
   - "Sold out everywhere": GPU, Providers, Last low (or "never").
   - "Recent provider price changes" (double width, up to 10 rows): Time, GPU, Provider, Was, Now, Chg.
   Every GPU and provider name is a link. Empty panels show a specific line, for example "no GPU's matched median rose in 24h", "no 24h move beyond |z| >= 2", "every GPU with live listings is purchasable somewhere".
5. **Lower row, left: "Market board · on-demand"** full table (up to 24 rows) with a CSV download ("opengrid-board.csv"): GPU, Prov, Low, Cheapest at, Median, High, Spread, 24h, 24h low (sparkline). Sortable; row click opens the GPU.
6. **Lower row, right**: "Latest notable events" (title links to /events?sev=notable, "all →") with up to 10 compact event rows; "Headlines" (links to /news, "all →") with up to 8 news items (age, headline opening the source in a new tab, source name plus "+N" other sources).

**States:** panels show "…" while loading, indices "loading indices…", summary "loading…". A section the API marks unavailable shows "unavailable <reason>". If /v1/overview fails, every panel shows "unavailable overview request failed: <message>" and the summary line shows the standard error with "Try again". Events: "No notable or major events recorded yet."; news: "No headlines ingested yet." or "unavailable news service did not answer (/v1/news)".

**Refresh:** everything reloads every 60 seconds while the tab is visible. The page also rewrites the global ticker with its own items.

**Observations**

- Heavy jargon with no plain-language layer: "σ 1h", "z", "n", "Δ", "Prov", "Most liquid / covered", "matched median", "Spread" (high / low minus 1). Help exists only in hover tooltips and the small "?" marks.
- Error text exposes internals: "overview request failed: HTTP 503", "index service did not answer (/v1/indices)", "news service did not answer (/v1/news)", and the lowercase "unavailable" prefix, unlike the friendly OG.error wording used elsewhere.
- Loading placeholders are a bare "…" in 13 places.
- Naming: menu "Markets", heading "Market overview", server page title "GPU compute market".
- 11 panels plus a 9-column board plus events and news on one screen is very dense; there is no prioritisation or grouping for a non-trader.
- The page overrides the global ticker (see shell observations), and its status bar freshness comes from the response time.

### /developers (Developers)

**URL:** /developers (anchors #quickstart, #assistant, #terminal, #rest, #access, #boundaries, #tools, #troubleshooting). Tab title "Developers · OpenGrid". Styles from web/devx.css.

**Who / sign-in:** developers who want the data in their editor, terminal or app. Public. The operator sees a different access banner and their row highlighted in the access table.

**Purpose:** three ways to use OpenGrid (AI assistant over MCP, CLI, REST), two quick first successes, setup snippets per client, access rules and limits.

**Layout, top to bottom**

1. **Header** "Developers", sub "Use OpenGrid's live GPU market data and workload-aware recommendations from your AI coding assistant, your terminal or your own code.", buttons "API reference" (/api) and "Ask the assistant" (/assistant).
2. **Three path cards**, each with a description, facts list ("Useful for", "You need", "API key", "Install", "Status: Available now") and a primary button that jumps down the page:
   - "Connect an AI coding assistant", button "Set up your assistant".
   - "Use OpenGrid from your terminal", button "Set up the CLI".
   - "Integrate OpenGrid into your application", button "Call the API".
3. **Access banner**: visitor: "No account needed to start." plus what needs a key, link "How to get one ↓". Operator: "Signed in as the operator." with buttons "Create a key" (/keys) and "Issue a key to a developer" (/admin/partners).
4. **On-this-page links**: "Quickstart", "AI assistant (MCP)", "Terminal (CLI)", "REST API", "Access & API keys", "What OpenGrid can and can't do", "MCP tools", "Troubleshooting".
5. **"Quickstart"** (sub "two results in a few minutes, no key needed"):
   - "First success 1: Find the cheapest available H100", tabs "curl" / "CLI" / "AI assistant". Each tab shows a code block with a label, a "where" hint and a "Copy" button (turns into "Copied" briefly). The AI tab has a copyable prompt and a link "ask the OpenGrid Assistant →" (/assistant?q=Cheapest available H100).
   - "First success 2: Analyze my project and recommend suitable GPUs", tabs "CLI" / "AI assistant" / "REST".
   - Table "What project analysis reads, and what leaves your computer": rows "Reads", "Never reads", "Sends to OpenGrid", "Allowed folders (MCP)".
   - "From a requirement to a run: what each step does": four steps with badges: "1 · Analyze requirements" (no key), "2 · Get a recommendation" (no key), "3 · Preview a route" (key: route:preview), "4 · Execute" (not available yet).
6. **"Connect an AI coding assistant"** (sub "MCP: the Model Context Protocol"):
   - Comparison table "Local server (recommended to start)" vs "Hosted endpoint": what it is, project files, API key, you need, tools (13 vs 12).
   - Client tabs "Claude Code", "Cursor", "VS Code", "Codex", "Claude Desktop / claude.ai". Each shows a "Verified · ..." or "Not yet tested · ..." line, a "Local server" block (badges "no key needed", "reads your project", "needs uv + git") and a "Hosted endpoint" block (badges "API key required", "nothing to install", "no project files") with copyable snippets, and a "Check it and troubleshoot" fold. Cursor's hosted block has an "Add to Cursor" deep-link button. The Claude Desktop tab has a copy button for the endpoint URL.
   - "Then ask": three copyable example prompts; note about prompt names cheapest_gpu, try_free_demo, size_my_project.
7. **"Use OpenGrid from your terminal"**: steps "1 · Install uv", "2 · Try it with nothing installed", "3 · Install the opengrid command (optional)", "More commands", a command table (Command, What it does, Key), and exit codes with a link to cli/README.md on GitHub.
8. **"Integrate OpenGrid into your application"**: base URL, envelope note, curl examples without a key and with a key, error codes, buttons "API reference: every public endpoint, with Try it" (/api) and "Data kinds".
9. **"Access & API keys"**: table "Your situation / What you can do / Next step" with rows "Exploring, no key" (link "Quickstart →"), "You need a key" (link "Request API access on GitHub ↗", note "Public issue: no secrets."), "You were issued a key" (snippets for `auth login`, the environment variable, minting a narrower key, and "who am I"), "You are the OpenGrid operator" (links "API keys →", "Design partners →"). The visitor's or operator's own row is highlighted. Footer about keys being shown once and stored hashed.
10. **"What OpenGrid can and can't do"** (sub "today"): capability matrix (CLI, Local MCP, Hosted MCP, REST, API key) with Yes / No cells, and a warning box "Execution." explaining that every provider adapter is still simulated and no real GPU job has run.
11. **"MCP tools"**: 13 tools with description and key scope ("local only" badge on opengrid_analyze_project); note that only three tools write anything.
12. **"Troubleshooting"**: uvx not found, cannot reach localhost:8000, tools not listed, outside allowed roots, 401, 403, 429, ChatGPT (needs OAuth, not offered).

**States:** static content renders immediately; only the access banner and table highlight change after /v1/me answers. No loading or error states beyond that. If the URL has a hash, the page scrolls to it after loading.

**Refresh:** none.

**Observations**

- Very long single page (12 sections, about 5 tab sets, many tables). The three path cards, the Quickstart, and the per-path sections repeat much of the same information (keys, uv, local vs hosted) three times.
- Jargon for a general developer audience: "MCP", "stdio", "streamable HTTP", "allowlisted", "scopes" (route:preview, deployments:read, route:execute, account:manage), "Bearer". Explained in places, but not at first use.
- The access message here is "API keys issued by the OpenGrid team, request on GitHub", while the shell calls the same thing "an OpenGrid account (invite only)". A visitor gets two different stories for one gate.
- Key requests go to a public GitHub issue form; there is no private channel, which may surprise a business user.
- "Verified" lines carry a fixed test date and client versions (2026-10-15) that will age silently.
- The table rows and status tags all say "Available now" for each path, while the boundaries table shows major gaps (no real GPU runs); the path cards do not hint at that.

### /api (API reference)

**URL:** /api (anchors #quickstart, #auth, #public, #private, #envelope, #kinds, #limits, group anchors such as #pub-gpus, and #all for the operator; ?q= keeps the endpoint filter). Tab title "API reference · OpenGrid". Menu: "API docs".

**Who / sign-in:** developers. Public. The operator additionally gets the full machine schema and runs "Try it" with their session.

**Purpose:** a curated REST reference with a browser "Try it" for every public GET endpoint.

**Layout, top to bottom**

1. **Header** "API reference", sub "JSON over HTTPS. Public market data and recommendations need no key; everything else needs an OpenGrid API key.", button "Developer guide" (/developers). Operator only: "Interactive docs (operator) ↗" (/docs) and "openapi.json (operator)".
2. **Two columns**: a sticky left table of contents ("Guide": Quickstart, Authentication, Public endpoints, Endpoints with a key, Envelope, Data kinds, Rate limits; "Public · N": each endpoint group with its count; operator: "Full endpoint list") and the body. Below 1000px the contents become a wrapped row above the body.
3. **"Quickstart"**: one curl example (cheapest H100 per variant) and what to read in the answer; link "Developer quickstart →".
4. **"Authentication"**: table Public vs "Needs an API key" (which endpoints, how, errors); a "check a key" snippet; then an access box: visitor: "Keys are issued by the OpenGrid team. There is no self-service sign-up yet. Every public endpoint works without one. To ask for a key, open a request on OpenGrid's GitHub..." with buttons "Request API access ↗", "Start without a key", "Operator sign in". Operator: "Signed in as the operator." with "Create a key" and "Issue a key to a developer".
5. **"Public endpoints"** (sub "N operations · no key · Try it runs from this browser without a key" or "as the signed-in operator"): a search field "Filter public endpoints" (filters by path or summary, updates ?q=), then 7 groups: "GPUs & hardware", "Market prices", "Best execution & recommendations", "Providers & data quality", "Indices & fixings", "Events & news", "Reference". Each endpoint is a collapsible row: method badge (GET/POST), path, summary, "public" badge. Opening it shows the summary, "Parameters: ... (* required)" and:
   - GET: a "Try it" box with one input per parameter (path parameters prefilled with samples such as h100-80gb-sxm5, lambda, h100-class; "limit" prefilled with 5), a live "GET <url>" preview, a "Send" button. The response shows an "HTTP <status>" badge (green or red), time in ms, size, a "Copy as curl" button and the pretty-printed JSON (first 60k characters). Visitors' requests are sent with no cookies, exactly like an anonymous client. On 429: "Anonymous requests are rate limited per IP; wait a minute."
   - POST: "Try it covers GET endpoints. Use the curl examples for POST: REST examples →".
   - No match: "No public endpoint matches."
6. **"Endpoints that need a key"**: table Endpoints / Scope (badge, amber for execute scopes) / What they do, 13 rows (route preview, jobs, deployments, route, me, keys, credentials, watchlists, billing, reservations). "Example: route preview" with a curl snippet. Operator only: link "Open this preview on the Route page →".
7. **"Response envelope"**: JSON example and explanation of meta.as_of, meta.kind, meta.methodology, null plus reason, error shape.
8. **"Data kinds & price concepts"**: badges observed / estimated / transaction and list / observed / quote / execution with one line each; link "Data kinds methodology →".
9. **"Rate limits"**: per-IP for anonymous; per key and per class (read, write, execute) token buckets; headers listed.
10. **Operator only: "Full endpoint list"** (badge "operator only"): loads /openapi.json ("Reading /openapi.json…"), shows "N operations in <title> <version>", a filter "Filter all endpoints", and every /v1 operation as a collapsible row with a "public", "admin" or "key" badge, a parameter table and "Try it" for GETs. Error: standard error with "Try again".

**States:** the header and body appear only after /v1/me answers (the area is blank briefly, with no loading indicator). Try it: "GET <url>" spinner while sending; "fill every path parameter" if a path value is empty; "network error" on failure.

**Refresh:** none.

**Observations**

- No loading state while /v1/me is pending: the page is empty until it answers.
- Some Try it forms send an incomplete request by default: required query parameters without a sample (for example "at" on /v1/timeline/around, "listing_id" on the listing context endpoint, "fixing_id") are left empty and the request goes out anyway, returning a validation error instead of a friendly prompt (only empty path parameters are checked).
- The visitor access box offers "Operator sign in" right next to "Request API access", which invites non-staff to a password prompt they cannot pass.
- Inline styles and a raw 11.5px font size in the operator list break the "only --fs-* tokens" rule; section spacing is overridden inline on every section.
- The server-side page description for /api still says "market data, routing and deployments with one key", which contradicts the page's "no key needed" message (search snippets and link previews).
- Dense developer jargon in the private table ("Idempotency-Key", "token bucket", "not_provisioned"), acceptable for the audience but there is no glossary link.

### /methodology and /methodology/{name}

**URL:** /methodology (index) and /methodology/{name} (one document, for example /methodology/data-kinds). Tab titles "Methodology · OpenGrid" and "<document title> · Methodology · OpenGrid". Menu: "Methodology".

**Who / sign-in:** anyone wanting to check how numbers are computed (analysts, reviewers). Public; no difference when signed in.

**Purpose:** the published documentation behind every number, one Markdown document per topic.

**Index layout**

1. **Header** "Methodology", sub "How OpenGrid computes every number it publishes. Every endpoint cites its document in meta.methodology.", buttons "JSON" (opens /v1/methodology in a new tab) and "API docs" (/api).
2. **Grouped document cards**: groups "Market data & indices", "Market structure", "Events & news", "Data trust & quality", "Routing & execution", "Accounts & billing", then "Other" for anything not listed. Each group heading shows a count. Each card: document title, its file name in mono, and a one-line summary (from the server, or the document's first paragraph, loaded afterwards; "…" until then).
3. Footer note: "N documents. Data kinds (observed / derived / estimated / transaction) and the four price concepts are defined in data-kinds; every figure on the site carries one of those labels."

**Index states:** "Loading documents…"; on error, the server-rendered list (if this was the first page load) or the standard error. No retry button.

**Document layout**

1. **Breadcrumb row**: "Methodology" link, " / ", the document name, its group name at the right, and button "raw markdown" (new tab).
2. **Two columns**: left sticky "On this page" contents built from the h2 and h3 headings (current section highlighted while scrolling; clicking scrolls and updates the URL hash; "← all documents" link at the bottom; "single section" if fewer than 2 headings). Right: the document, rendered on the server from Markdown. Every heading gets a "#" anchor link ("Link to this section"); wide tables scroll inside their own box; endpoint names in code are styled.
3. Below the document (from the server render): "All methodology · raw markdown".

**Document states:** spinner "Loading…"; standard error if the page cannot be fetched (in-app navigation fetches the server render); "No document." if empty. The browser tab title updates to the document's H1.

**Refresh:** none.

**Observations**

- Grouping is out of date: about 14 of the 30 documents are not in any group and fall into "Other" (economics, execution-safety, first-live-route, jobs, provider-capabilities, recommendations, reconciliation, reliability, reservations, routing-quality, runner, security, settlement, workloads), including the ones Home links to ("How jobs work", "How runs are kept safe"). The "Accounts & billing" group lists an "accounts" document that does not exist.
- The footer lists four data kinds including "derived", but the rest of the site shows only three badges (observed, estimated, transaction) and hides "inferred".
- "JSON" and "raw markdown" buttons open raw machine formats for a reader audience; "raw markdown" appears twice on each document (breadcrumb button and footer link).
- The index has no search or filter for 30 documents.
- The index sub line ("Every endpoint cites its document in meta.methodology") is aimed at API users, not readers.

### /assistant (OpenGrid Assistant) and the reusable assistant panel

**URL:** /assistant, optionally /assistant?q=<question> (asks it immediately; used by the command bar "ask ..." and links elsewhere). Tab title "Assistant · OpenGrid". Menu: "Ask the assistant" under "Find Compute".

**Who / sign-in:** anyone with a plain-English question about GPU prices or sizing. Asking is public (POST /v1/assistant is a public, read-only endpoint, rate limited per IP). Only the action buttons inside answers (start the demo, cancel a job) and route previews need the operator sign-in in the browser (or an API key elsewhere).

**Purpose:** a chat panel that answers a fixed set of question types from live data, with structured cards instead of free text numbers.

**Page layout**

1. **Header** "OpenGrid Assistant", sub "Plain-English questions about live GPU prices and which GPU fits your job. No sign-up needed.", buttons "Find Compute" (/find) and "Use it from your editor" (/developers#assistant).
2. **Main column: the panel** (no title on this page)
   - Head line note: "Answers come from OpenGrid's live market data; estimates are labelled. It never rents a paid GPU: it can only start the free simulated demo or cancel a job, when you click." Buttons "Examples" (hidden until the first question; brings the example chips back) and "New" (tooltip "Start a new conversation": clears the log and context).
   - Conversation log (up to 60 messages kept).
   - Example chips (shown before the first question and again after an answer the server did not understand): "Cheapest available H100", "What GPU should I use to fine-tune Llama 3 8B with LoRA?", "Affordable GPU for inference of a 7B model", "Compare H100 SXM and H200", "Why did H200 prices change?", "Can I fine-tune a 13B model with QLoRA within $50?", "Specs of the L40S". Clicking one asks it.
   - Input: two-line text box, placeholder "Ask about GPU prices, which GPU fits your job, comparisons…", max 2000 characters; Enter sends, Shift+Enter makes a new line; button "Ask".
3. **Side column** (300px), three boxes:
   - "What it answers": "Cheapest GPU", "Which GPU fits", "Within a budget", "Compare GPUs", "Specs", "Price moves", "Free demo and your jobs", each with a one-line description and a chip with an example question (the last is "Try a free training demo").
   - "What it doesn't do": not a general chatbot; never rents a paid GPU or runs your code; cannot read your files here (link "how" to /developers#quickstart).
   - "Reading the answers": observed badge (prices are listings, not quotes), estimated badge (memory, GPU count, cost), runtime never guessed without a benchmark, "Buttons that start or cancel jobs, and route previews, need sign-in."

**Reply anatomy:** the reply sentence, then zero or more cards, then a row of link buttons the server suggested, and, when the wording could not be matched to the data, "Some wording above could not be matched to the data; rely on the figures in the cards."

**Card types**

- Recommendation: "Needs about N GB of GPU memory (across N GPUs) · <confidence> confidence" (estimated badge); three pick tiles "Cheapest that fits", "Best for this workload", "Launchable through OpenGrid" (GPU count and name, provider and region, price per GPU-hour with badge, "stale" badge if old, memory headroom, reason, buttons "Market" and "Route preview"; visitors see "Route preview (sign-in)" instead; empty tile text "Nothing available right now." or "Nothing OpenGrid can launch for this workload today."); a table of the top 5 options (GPU, Provider, Region, "$ per GPU-hour", "Fits in memory", "Launch via OpenGrid": yes / not yet); up to 3 notes; legend "Memory and cost are estimates; prices are observed provider listings, not quotes; runtime is unknown without a benchmark."; button "Open in Find Compute →" (prefills /find in paste mode). If no GPU is needed: "No GPU needed" plus reason.
- Prices: table GPU, Provider, Region, "$/GPU-h" (with badge), Available (yes / no / not reported); family answers note "Variants are priced separately."; empty "No fresh, in-stock listing right now."
- Compare and Specs: table GPU, "Memory (GB)", "Memory bandwidth (TB/s)", "BF16 peak (TFLOPS)", "FP8 peak (TFLOPS)", "Lowest $ per GPU-hour", "Median $", Providers; optional note.
- Price context ("why did prices change"): warning "Correlation, not causation." (or the server note), per GPU a summary list and a moves table ("Hour (UTC)", Move, %, From, To, kind badge), then "News in the same window (untrusted public text; not a cause):" with outside links.
- Follow-up questions: each question with an optional reason and answer chips that send a ready-made message.
- Jobs / one job: list of job links with status text, "epoch X of Y", "simulated" badge, cost or "up to $X"; "All jobs →"; or "No jobs.".
- Template (real GPU run): template title and GPU count; notes "Only this allowlisted template runs...", "Every launch is approved by the OpenGrid operator first.", hard caps (total cost, minutes, price per GPU-hour), expected runtime band (estimated).
- Action: a button with the server's label (default "Start free simulation" or "Cancel <job id>"), an optional note, and "Needs sign-in" for visitors. Start demo runs on click ("Starting the free simulation…", then "Started." plus "Watch it run →"). Cancel opens a confirmation dialog "Cancel <job id>?" with buttons "Keep it running" and "Cancel job" (danger), then "Cancelling…", "Cancel requested." plus "Open the job →". On 401/403: "Starting or cancelling jobs on this website needs the operator sign-in. From your terminal or AI assistant it needs an API key with route:execute." with "Sign in" (operator login) and "How keys work →".

**States:** while waiting the bot message shows "Thinking…" and "Ask" is disabled. 429: "Too many questions in a short time; wait a minute and try again." 422: "That question or conversation is too long. Start a new conversation." (context is dropped). Other errors: the standard error block. Context is sent back with each question so follow-ups work; it is dropped if it grows too large.

**Reuse:** the same panel is embedded on /find (where a recommendation fills the Find Compute form instead of showing "Open in Find Compute →") and on /jobs.

**Refresh:** none; the conversation lives only in the page and is lost on navigation or reload (a reload with ?q= in the URL asks the first question again).

**Observations**

- Sign-in wording is mixed inside one feature: "Needs sign-in", "Route preview (sign-in)", "needs the operator sign-in", "Sign in" button, while the shell says "Needs an account". The only sign-in offered is the operator password prompt, so for a normal visitor these buttons lead to a dead end.
- The "Needs sign-in" tag on action cards depends on /v1/me having answered; an answer that arrives before that shows the button with no tag.
- The side box example "Try a free training demo" leads to an action every visitor will fail (needs sign-in), right after "No sign-up needed" in the page sub line.
- The "?q=" stays in the URL after asking, so refresh or a shared link silently re-asks (and spends rate limit).
- Table headers are technical for a plain-English tool: "BF16 peak (TFLOPS)", "FP8 peak (TFLOPS)", "Memory bandwidth (TB/s)", "%", an unlabeled badge column in the moves table.
- No way to keep or copy a conversation; "New" clears without confirmation.

---

# Part 2: Market-data pages

How to read this: OpenGrid is a single-page web app. Every page sits inside the same shell: a left menu, a command bar (press "/" to search, "?" for shortcuts) and a status line that says "Prices updated N min ago" (or "Prices may be out of date: last read ..." after 10 minutes). Most pages update that line when their data loads. The server also sends a plain-HTML summary of each page for search engines; the app replaces it once the page loads.

Shared building blocks that show up on almost every page below:

- **Page header**: a big title, a one-line subtitle, and controls on the right.
- **Stat strip**: a row of small cells, each with a label, a value and a short line under it. If a value is missing the cell shows "n/a", with the reason on hover. Some pages fold missing history-based cells into one trailing cell called "Not enough history yet" (or "Not available yet") that lists their names.
- **Tables**: click a column header to sort. Most tables have a title bar with a "CSV" button ("Download these rows as CSV"). Long tables show a "Show N more" button. A line under the table can say "No data yet: ..." (columns that are blank in every row, hidden automatically) or "Hidden at this width: ..." (low-priority columns dropped on narrow screens; the CSV still has them).
- **Segmented buttons**: small groups of toggle buttons such as "24H 7D 30D 90D".
- **Data-kind badge**: a small "observed" badge linking to /methodology/data-kinds.
- **States**: loading is a spinner plus text (for example "Loading GPU markets…"). Empty is a grey sentence. "Insufficient data" is a bold title plus a reason (for example "History building"). Errors use one shared box: a title such as "Could not reach OpenGrid", "OpenGrid had a problem", "Not found" or "Too many requests", one sentence, an optional "Try again" button, and a folded "Technical details" with the HTTP status. A 401 shows a lock icon, the title "Sign-in required", the text "This needs an OpenGrid account. Accounts are invite-only for now; there is no self-serve sign-up yet.", an "Explore free pages" button (to /) and a link "OpenGrid staff: operator sign-in".
- **Auto-refresh**: timers run only while the browser tab is visible and stop when you leave the page.

Access in production: PUBLIC_PAGES is on. Every page in this document except /ops reads only public data (/v1 areas gpus, markets, indices, history, spreads, events, news, providers, heatmaps, timeline, best, capabilities, opportunities, trust, families, fixings, compare, plus the legacy reads /market, /market/detail and /listings). So anonymous visitors see them in full. Two exceptions: /ops (all of it is private) and one section of /provider/{name} that reads the private /mapping.

In the menu, "Explore" (marked "Free to use, no account needed") holds "GPUs" with sub-links "Compare", "Explorer", "Heatmaps", "Providers". "Markets" holds "Indices", "Opportunities", "Events", "News" (and the account-only "Watchlists"). "Ops" sits in a hidden "Operator" group that appears only for admins. "Route" is listed under "Your workspace" and needs an account.

### /gpus (GPU markets)

- **URL**: /gpus. Filters and sort are kept in the URL (q, vendor, arch, vram, wl, ff, avail, all, sort, dir), so a filtered view can be shared.
- **Who / sign-in**: anyone; no sign-in. Reads /v1/gpus, /market?hours=24 and /v1/spreads, all public.
- **Purpose**: one table of every canonical GPU with its current on-demand price per GPU-hour across providers.

**Layout, top to bottom**

1. Header: title "GPU markets", subtitle "Every canonical GPU: current on-demand price per GPU-hour across providers, availability, dispersion and capability pricing". No buttons on the right.
2. Stat strip, six cells:
   - "GPUs priced now" (sub "of N tracked")
   - "Priced listings" (sub "N explicitly available")
   - "24h movers": "N▲ M▼", sub "N unchanged". When missing it says "no lowest price moved" or "24h board unavailable".
   - "Most efficient": a GPU name, sub "score N · N providers"; reason when missing is "needs 3+ providers"
   - "Most fragmented": same idea, sub "score N · spread X%"
   - "As of": time, with the date underneath
3. Filter bar, left to right:
   - search box, placeholder "Filter GPUs"
   - dropdowns "All vendors", "All architectures", VRAM ("Any VRAM", "≤ 24 GB", "25-48 GB", "49-96 GB", "97-160 GB", "> 160 GB"), "All workloads" and "All form factors". Each dropdown lists only the values present in the data.
   - toggle chip "Available now" (tooltip "only GPUs with at least one listing in stock (explicit availability)")
   - toggle chip "Include unpriced" ("also list GPUs nobody prices right now")
   - a "Reset" button on the far right, which clears every filter
4. Table titled "GPU markets · on-demand". The toolbar shows "N of M GPUs" and a "CSV" button. Columns:
   - "GPU": bold short name, links to /gpu/{slug}
   - "Vendor": hidden
   - "Arch"
   - "VRAM"
   - "Form"
   - "Class": workload class, an editorial label
   - "Low $/GPU·h"
   - "Median"
   - "High"
   - "24h": change of the lowest price, with an arrow
   - "24h trend": sparkline
   - "Prov": number of providers
   - "Avail": listings explicitly available
   - "Spread": "-" when only one provider
   - "Eff.": efficiency score 0 to 100, coloured; "n/a" when fewer than 3 providers
   - "$/GB·h"
   - "$/BF16 PF·h"

   Clicking a row opens /gpu/{slug}. Unpriced rows are greyed out. On narrow screens the columns drop in this order: "24h trend", "Form", "Arch", "Class". Default sort is "Prov" descending.
5. Note under the table: an observed badge, then "Observed list prices, on-demand, not interruptible, in stock or stock unknown; one vote per provider (its lowest). Spread, efficiency and 24h change are derived. $/GB and $/PFLOP ... are theoretical, not benchmarks." It ends with the links "Dispersion" and "Hardware" (methodology pages).

**States**: loading shows "Loading GPU markets…". If no row matches the filters, the table says "No GPUs match these filters.". If /v1/gpus fails, the shared error box appears with "Try again". If /market or /v1/spreads fails, the page still loads: the 24h and efficiency cells show "-" or "n/a" with a reason ("24h board unavailable", "spreads unavailable").

**Refresh**: reloads every 60 s, keeping filters and sort.

**Observations**
- The table is wide (16 visible columns) and uses abbreviations without spelling them out: "Prov", "Avail", "Eff.", "$/GB·h", "$/BF16 PF·h". The meaning is only in hover tooltips.
- The default sort is by provider count, not price, so a visitor who wants "cheapest first" has to click "Low $/GPU·h".
- "Most efficient" and "Most fragmented" are market-structure ideas (spread-based scores), not GPU performance. A newcomer could easily read "efficient" as a hardware claim.
- This page's "24h" is the change of the *lowest price* (from the legacy /market board). The GPU page header's 24h cells are *index* changes. The same label means different things on two linked pages.

### /gpu/{slug} (one GPU market)

- **URL**: /gpu/{canonical-slug}, for example /gpu/h100-80gb-sxm5. URL state: w (window), view, ov (overlays), sev, count. If the slug is not a canonical GPU, the page tries to load it as a GPU family and shows the family page instead (next section). If that fails too, it shows the title "Unknown GPU" with the slug, "No canonical GPU or GPU family has this slug." and a link "All GPU markets →".
- **Who / sign-in**: anyone. Every read is public: /v1/gpus/{slug}, /v1/indices/{slug}, /v1/markets/{slug}/context and /regions, /v1/history, /market/detail, /v1/timeline (and /around), /v1/events, /v1/news, /v1/best, /v1/trust/listings, /v1/heatmaps, /listings.
- **Purpose**: everything about one GPU's cloud market, laid out like a stock page: price, history, providers, regions, events, alternatives and specs.

**Layout, top to bottom**

1. Header. A small vendor eyebrow, the short GPU name, then badges (architecture · "80 GB HBM3" · form factor · workload class), then the full name. Buttons on the right:
   - "Route this →" (primary; tooltip "Preview where this would run (no provisioning without confirmation)"; goes to /route?gpu=)
   - "Compare" (/compare?a={slug})
   - "Index" (/indices/{slug})
   - "Listings" (/explorer?gpu={full name})
2. Quote strip. On the left, a big "OpenGrid index" block: the level in $/GPU·h with its 24h change, and under it "$/GPU·h · N providers · {time}", or the reason it is not published. Then a stat strip:
   - "Low" (sub: cheapest provider) and "Median" (sub "of provider lows")
   - "High" (sub: the dearest provider)
   - "Spread" (sub "$X high − low")
   - "Providers" (sub "priced now")
   - "Available" (sub "N unkn. · N out")
   - "24h chg", "7d chg", "30d chg", "90d chg" (all index changes)
   - "Volatility"
   - "Hist. low" and "Hist. high"
   - "Percentile" (for example "34th", sub "median vs 30d · label")

   The history-based cells are left out while loading, and left out when they have no value.
3. Context line. "Context" plus a generated sentence and a "how" link, or "Historical context: not available on this server." Then a "History" note naming the missing cells, for example "7d change · Volatility: not enough history yet".
4. Section nav, a row of buttons that scroll to sections: "Chart", "Availability", "Providers", "Regions", "Moves & news", "Events", "Alternatives", "Listings", "Best now", "Dispersion", "Distribution", "$/capability", "Hardware". The button for the section in view is highlighted.
5. Two columns. The main column comes first:
   - **"Price history"**. Controls on the right:
     - a view toggle "Market" / "Providers"
     - a window toggle "24H", "7D" (default), "30D", "90D", "ALL"
     - the word "overlay" followed by chips "Events", "News" and "Moves" (all on by default)

     "Market" view draws median, lowest, a low-high band and a dashed OG index line. "Providers" view draws one step line per provider. Event, news and move markers sit on the time axis. Clicking a move marker opens a "Related news and events around this move (not necessarily causal)" panel under the chart, with a "×" close button. A note under the chart explains the hatched area (before every current provider was being recorded) and links "Method".
   - **"Availability history"**: a small chart with three lines, "Available listings", "Priced providers" and "Sold-out listings", plus a note.
   - **"Provider ranking"**: table titled "N providers · one vote each (its lowest eligible price)". Columns:
     - "#" and "Provider"
     - "Best $/GPU·h"
     - "vs median"
     - "24h" (only shown when the data has it)
     - "Lst"
     - "Stock" ("in stock", "sold out" or "unknown")
     - "Region"
     - "Fresh": a freshness badge
     - "Trust": a confidence word
     - "Source"
     - an unlabelled "site ↗" link

     Has CSV. Hovering a row highlights that provider's line in the Providers chart view. A note follows, ending "Method".
   - **"Region pricing"**: table with columns:
     - "Region group"
     - "Cheapest $/GPU·h" and "at" (the provider)
     - a bar
     - "Median" and "vs global"
     - "Providers"
     - "Priced listings" and "Available"
     - a "compare →" link

     A note follows ("No listing now in: ...", "Region groups from each listing's stated location; never guessed.") with a link "All regional heatmaps →".
   - **"Price moves and related news"**: two columns.
     - Left, "Moves of the lowest price": a table titled "Click a move: related news and events", with columns "Hour", "Move", "From → to" and "Providers" ("set changed" or "same"). Clicking a row opens the same related-news panel below the table.
     - Right, "News mentioning this GPU": a list of date, source tier, headline link (opens a new tab) and source. Then a note: "Appearing near a price move does not mean it caused it." and a "More →" link to /news?gpu=.
   - **"Market events"**: severity toggle "All", "Notable+" (default), "Major". Table columns "When", "Sev", "Type", "Event", "Provider". Links "All events for this GPU →" and "How events are detected".
   - **"Related and alternative GPUs"**: table titled "Shares the listed dimensions, not equivalent products". Columns:
     - "GPU"
     - "Shares": badges such as "similar VRAM" or "training"
     - "Low" and "Median"
     - "Δ median $", "Δ VRAM", "Δ BF16", "Δ mem BW"
     - "Prov" and "Avail"
     - "compare →"
   - **"All current listings"**: table titled "N listings (all market types, incl. spot and sold out)". Columns "Provider", "SKU", "GPUs", "$/GPU·h", "$/inst·h", "Type", "Tier", "Region", "Stock", "Seen". Shows 25 rows, then "Show N more".
6. Right rail:
   - **"Best available now"**: a GPU-count toggle "1×", "2×", "4×", "8×". Shows the top-ranked listing: logo, provider, location, price "/GPU·h", "score N · mode · N× SKU" and a badge "provisionable" or "market data only". Under that, a factor table (factor name, bar, weight, contribution, "neutral" badge when there was no data), an explanation, a "Route this →" button and a "how it is scored" link. Then up to 5 runner-up rows, and a note: "N candidates, N excluded (...). Prices are observed market prices, not quotes. Reliability and performance are not used: no execution history yet."
   - **"Dispersion and efficiency"**: a big efficiency score "/100", a label, and "fragmentation N · confidence X". A component table with columns "component", "raw", "saturation", "w", "pts" and rows "CV (adj.)", "IQR / median", "High / low − 1". A key-value table "Spread", "IQR", "Std dev". A formula note.
   - **"Price distribution now"**: a dot plot with a "Providers" row and a "Listings" row. Then "Providers · listings", "Q1 to Q3 (providers)", "Listing median", "Mean (providers)".
   - **"Price per capability"**: a "theoretical" warning badge, then a table with columns "$", "spec", "at low", "at median" and rows "per GB VRAM·h", "per BF16 PFLOP·h", "per FP16 PFLOP·h", "per FP8 PFLOP·h", "per TB/s·h".
   - **"Hardware"**: a spec table:
     - "Vendor · arch" and "Released · segment"
     - "VRAM" and "Memory bandwidth"
     - "BF16 (dense)", "FP16 (dense)", "FP8 (dense)", "FP32"
     - "Form factor", "Host link", "GPU-GPU link"
     - "TDP" and "Workload class"
     - "Partition of", when the GPU is a slice of another

     Then source links and "Method".

**States**:
- Each section loads on its own with a spinner (for example "Loading market…", "Loading providers…", "Ranking listings…", "Loading history…").
- Insufficient states:
  - "No live price": "No provider is selling the X in stock on demand right now (...)"
  - "Not enough availability history"
  - "No regional data"
  - "Nothing to route": "No eligible N-GPU listing now (...)", with an "Open route preview →" button
  - "No efficiency score"
  - "No spec"
  - "No alternatives"
  - "Unavailable": events, news or best ranking not on this server
- Empty states:
  - "No price history for this GPU in this window."
  - "No hour-over-hour move of the lowest price ≥ 3% in this window."
  - "No news item mentioning this GPU has been ingested yet."
  - "No current listings."
- When hourly history is short (fewer than 6 points), the chart quietly switches to a fine-grained replay of listings, and the note says so.

**Refresh**: summary and best listing every 60 s. Index, chart, timeline, trust, regions and events every 5 min. News, listings and market changes load once. Pressing Escape (outside a text field) navigates to /gpus.

**Observations**
- The page is very dense: 4 header buttons, up to 16 stat cells, a 13-button section nav, 8 main sections and 5 rail sections. A first-time visitor has no obvious place to start.
- There are two "Route this →" buttons (header and Best now). Both go to /route, which the menu marks "Needs an OpenGrid account". Anonymous visitors on a public page are sent to a sign-in wall without warning.
- The "Index" button is always shown, even when the GPU has no published index. For an unindexed GPU it may lead to the "Not found" state on /indices/{slug}.
- Three different "24h" numbers appear for one GPU. The header cells are index changes. The provider table's "24h" is each provider's own lowest price change. The /gpus list's "24h" is the market lowest price change.
- The region "compare →" link always pairs the region with "US" (or with "Europe" when the row is US). The pairing is arbitrary, not the user's choice.
- The section nav only highlights main-column sections. Rail sections ("Best now", "Dispersion", "Distribution", "$/capability", "Hardware") never highlight, because only `.gp-main > .gp-sec` is checked.
- Pressing Escape leaves the page for /gpus. That is surprising, and easy to trigger by accident.
- Heavy jargon with no plain-language layer: "CV (adj.)", "IQR / median", "saturation", "w", "pts", "Lst", "Fresh", "Trust", "fragmentation", "confidence", "Percentile ... median vs 30d".
- The severity toggle here reads "All / Notable+ / Major". On /events the same control reads "ALL / NOTABLE+ / MAJOR".

### GPU family page (/gpu/{family-slug})

- **URL**: the family page has no route of its own. It is drawn by /gpu/{slug} when the slug names a family or architecture rather than one canonical GPU, for example /gpu/h100 or /gpu/blackwell. URL state: w (chart window).
- **Who / sign-in**: anyone. Reads /v1/families/{slug} (falling back to /v1/markets/{slug}), /v1/history/{variant} for each variant, and /v1/news?gpu=, all public.
- **Purpose**: show each variant of a GPU family side by side with its own price, never merged into one family price.

**Layout, top to bottom**

1. Header. An eyebrow "GPU family" or "GPU architecture", the family name, an architecture badge (families only) and "N variants". Buttons on the right:
   - "Compare variants" (only with 2+ variants; goes to /compare/{first}-vs-{second})
   - "Route →" (primary; tooltip "Route with this family: pick a variant or allow variants"; goes to /route?gpu={family})
2. Note box. Bold "Variants are different products." then "Memory size, form factor and interconnect differ, so OpenGrid never merges their prices into one family price; each row below is its own market." Plus the family's own note, and for architectures "Model families in this architecture: ..." with links.
3. Stat strip:
   - "Variants" (sub "N priced now")
   - "Cheapest variant now": observed price, sub "variant · provider"; tooltip "The lowest observed on-demand price of one variant, not a family price"
   - "Indexed variants" (sub "each with its own index")
4. "Variants side by side": a compact table with CSV. Columns:
   - "Variant": colour swatch plus link
   - "Low $/GPU·h" and "at" (the provider)
   - "Median" and "High"
   - "24h": the variant's index change
   - "Providers" and "Available"
   - "Index": links to /indices/{id}, or "n/a"
   - "market →"

   Clicking a row opens the variant. Hovering a row highlights its line in the chart. Default sort is "Low" ascending.
5. "Lowest price per variant": window toggle "24H", "7D" (default), "30D", "90D". One coloured line per variant. Note: "One line per variant: its own lowest observed on-demand price per GPU-hour, hourly. Lines are never averaged into a family line." It also says "N variants without history in this window." and links "Method".
6. "News mentioning the {family}": up to 12 headlines (date, tier, link, source). Note "Linked by mention of the family or one of its variants (matched by rules); not a claim about any price." and "More →" to /news?gpu={family}.

**States**: "Loading…" while the slug is resolved. Chart: "Loading each variant's history…", then insufficient "No history": "No variant has hourly price history in this window yet.". Table empty: "This family has no variants listed.". News: "No news item mentioning the {family} has been ingested yet.", or "Unavailable": "News is not available on this server.".

**Refresh**: chart every 5 min. The variants table and stat strip load once and never refresh.

**Observations**
- "Compare variants" always compares the first two variants in API order. You cannot choose which pair.
- "Route →" goes to the account-only /route page with no warning to anonymous visitors.
- The variants table and "Cheapest variant now" never refresh, while the chart below does every 5 min. On a page left open, the table and chart can disagree.
- The family page cannot be reached from /gpus (that table lists only canonical GPUs). Visitors reach it only through typed slugs, the command bar or links from other pages.

### /providers (provider league table)

- **URL**: /providers. URL state: days (7, 30, 90), class, sort, dir.
- **Who / sign-in**: anyone. Reads /v1/providers, /v1/capabilities and /v1/trust/providers, all public.
- **Purpose**: every GPU cloud OpenGrid reads, compared on coverage, price versus the market, feed health and integration level.

**Layout, top to bottom**

1. Header: "Providers", subtitle "Every GPU cloud OpenGrid reads: coverage, pricing vs the rest of the market, feed health and how far OpenGrid can route to it". On the right:
   - "Class" toggle: "All" plus each class present, for example "hyperscaler", "neocloud", "general cloud", "marketplace", "decentralized", "unclassified"
   - "Window" toggle: "7D", "30D" (default), "90D"
2. Stat strip:
   - "Providers" (sub "N with live listings here")
   - "Live listings" (sub "N priced"; observed badge)
   - "GPU models" (sub "canonical, across providers")
   - "Feeds healthy": "N/M", sub "last 24h of fetches"
   - "Rel. value": "N rated", or "n/a" with the reason "needs ≥ 24 compared hours in the last Nd; no provider has them yet"
   - "Routable (impl.)" (sub "L≥2 implemented · 0 verified live")
3. Table titled "N providers · window Nd", with CSV. The last two columns stay visible when the table scrolls sideways. Rows with no live listings are greyed. Clicking a row opens /provider/{name}. Default sort is "Listings" descending. Columns:
   - "Provider": logo plus link
   - "Class"
   - "Source": "auth API", "public API", "public page", "aggregator" or "price file"
   - "Integration": "L0 data only", "L1 avail. check", "L2 provision" or "L3 lifecycle", with a "·mock" suffix when the level is above 0. Hover shows six lines: what OpenGrid implements, what the provider API documents, verified live (no), and so on.
   - "GPUs"
   - "Listings": hover shows the priced count
   - "Regions": a list of region groups, or "unassigned"
   - "Avg prem.": coloured, negative means cheaper
   - "% cheapest"
   - "Avail."
   - "Vol (1d)"
   - "Lifetime"
   - "Feed": a dot "healthy" or "ok", or a badge such as "degraded", "idle" or "N% fail"
   - "Last ok": a freshness badge, or "never"
4. Note: observed badge, "coverage and feed health. Relative value: each provider vs the median of the other providers' lowest price ... Figures stay n/a (hover for the reason) until there are ≥ 24 compared hours. Greyed rows: no live listings on this server." Link "Methodology →".
5. Two boxes side by side:
   - **"Facts"**: generated bullets. Who lists the most GPU models, who has the most live listings, how many providers span two or more region groups, how many publish no location, and "No live listings on this server from: ... (no API key configured here, or the feed has not run)." Then relative-value facts (8 shown, then a button "Show all N facts" / "Show fewer").
   - **"Integration levels"**: a legend table L0 to L3 ("market data only", "availability check", "provisioning", "full lifecycle", each with a description). A note explains "·mock" and says no adapter has been run against a real provider account. Link "Routing methodology →".

**States**: while loading, the stat strip shows "Providers n/a (loading)" and the table area shows "Loading providers…". The shared error box (with "Try again") replaces the table if /v1/providers fails. If the capability registry is missing, Integration cells show "n/a" ("capability registry unavailable").

**Refresh**: every 2 min. Each refresh first replaces the table with the "Loading providers…" spinner, then redraws it.

**Observations**
- Every 2-minute refresh blanks the table to a spinner and rebuilds it. The page flickers, and a visitor reading or scrolling the table loses their place.
- The stat sub-line "L≥2 implemented · 0 verified live" has "0" hard-coded in the text. It does not come from the data.
- The "Integration" column ("L2 provision ·mock") is internal shorthand. Its meaning lives in a six-line tooltip and a legend box at the bottom of the page.
- The class filter shows raw lowercase ids ("general cloud", "unclassified"). It sits in the header beside the window toggle, apart from the table it filters.
- Several columns are usually all "n/a" on a young server (relative value needs ≥ 24 compared hours). They may be auto-hidden into a "No data yet:" footer, which makes the column set differ between servers and over time.

### /provider/{name} (one provider)

- **URL**: /provider/{provider-id}, for example /provider/runpod. URL state: days, gpu, window, hist.
- **Who / sign-in**: anyone, except one section. Public reads: /v1/providers/{p}, /v1/capabilities/{p}, /v1/timeline, /v1/providers/{p}/gpus/{gpu}/context, /v1/trust/*, /v1/events, /v1/news and /listings. The "Source field map" section reads /mapping, which is private.
- **Purpose**: one provider in depth: its catalogue against the market, relative-value history, price history per GPU, integration level, feed health, regions, events, news and every listing.

**Layout, top to bottom**

1. Header: logo, display name, class eyebrow, then the subtitle "{class} · prices read from {host} ({source type})". On the right:
   - "Window" toggle "7D", "30D" (default), "90D"
   - a "Compare with…" dropdown of every other provider (choosing one goes to /compare?a=&b=)
   - "Listings →" (/explorer?provider=)
   - "News →" (/news?provider=)
   - "Website ↗" (when known)

   An optional provider note follows.
2. Stat strip:
   - "GPUs" (sub: the first 3 architectures)
   - "Listings" (sub "N priced")
   - "Regions"
   - "Cheapest now": "N/M", sub "GPUs where it is rank 1"
   - "Median prem. now"
   - "Avg prem. Nd", "% cheapest", "Availability" and "Volatility": these fold into "Not enough history yet" when missing
3. Left column:
   - **"Facts"**: bullets, for example "{name} lists N GPU models across N live listings; its most widely traded is X at $Y/GPU-hr, rank R of M.", "Cheapest provider right now for ...", "Only provider OpenGrid sees selling ... No comparison possible."
   - **"Catalog vs market"**: table titled "N GPUs · market = every provider's lowest live price now", with CSV. Default sort is "Prem. now" ascending. Clicking a row picks that GPU for the price chart and scrolls to it. Columns:
     - "GPU"
     - "Best $/GPU·h"
     - "Mkt median"
     - "Prem. now"
     - "Rank" ("1/5")
     - "Avg Nd"
     - "% cheapest"
     - "In stock" ("3/4")
     - "Regions"
   - Two boxes, **"Where it beats the market"** and **"Where it is expensive"**: rows of GPU link, bar, % and a "now" or "Nd" badge. Empty text: "No GPU where it is below the market median." / "... above ...".
   - **"Relative value history"**: toggle "Avg premium", "Rank", "Cheapest share", "Availability", over a daily line chart. Fewer than 2 days gives insufficient "History building" with today's numbers.
   - **"Price history"**: a control bar with a "GPU" dropdown (each option shows its best price), a "Window" toggle ("24H", "7D" default, "30D") and a link "GPU market →". The chart has three lines: "Market lowest" (dotted), "Market median" (dashed) and "{name} lowest", with markers for events, price moves and news. Under it, context bullets ("Percentile vs its own history: ...") and a note.
   - **"Recent price changes"**: table titled "N listings whose price changed since first seen". Columns "When", "GPU", "Listing", "Tier", "From", "To", "Change".
4. Right column:
   - **"Routing integration"**: the integration level plus a "verified live" or "not verified live" badge, a sentence on what the adapter can do, and a key-value list:
     - "Provider API documents" and "OpenGrid implements"
     - "Verified live" and "Avail. check live-verified"
     - "Routed via"
     - "Resource" and "Credential"
     - "Configured here" and "Stop supported"
     - "API docs"
   - **"Source & feed health"**: a status word, a freshness badge and "polled every Nm". Then a key-value list:
     - "Source host", "Source type" and "Last ok fetch"
     - "Fetches 24h" and "Latency p50 / p95"
     - "Listings now / 24h ago"
     - "Schema changes 7d" and "Quarantined values"
     - "Consecutive failures"
     - "Listing lifetime (median)"

     A note with the links "Field map ↓" and "Data trust →".
   - **"Regions covered"**: rows of region group, "N GPUs" and "N listings", plus "As published:" raw locations.
   - **"Market events"**: up to 12 one-line event rows and "All events →".
   - **"Related news"** (with an "all →" link in the title): up to 8 headlines with "date · source · rel N · topics". Note "Matched by entity rules; shown as related, never as a cause of a price move."
5. **"Source field map"**: "Endpoints read" and "Quirks" boxes, then a "Field provenance" table with columns "Field", "Kind" (raw, derived, lookup, absent), "Source", "Note".
6. **"Current listings"**: table titled "N listings incl. sold-out and ineligible rows". Shows 50 rows. Columns:
   - "GPU": shows "raw · unmapped" when the name is not mapped to a canonical GPU
   - "Raw name"
   - "GPUs"
   - "$/GPU·h" and "$/inst·h"
   - "Type"
   - "Region"
   - "Stock"
   - "Fresh"
   - "Conf."

**States**: catalogue "Loading catalog…". No listings: insufficient "No catalog": "OpenGrid has no live listings from {name} on this server. On servers without this provider's API key, its feed does not run.". Unknown provider: the catalogue shows the "Not found" error and the subtitle becomes "Not a provider OpenGrid tracks". Price chart: "No GPU to chart." or insufficient "History building". Events and news show "n/a" when unavailable. Field map: "No field map for this provider.".

**Refresh**: every 2 min it reloads the main payload, feed health and listings. That also redraws the price chart (back to a spinner first).

**Observations**
- For anonymous visitors, /mapping is private and the request fails quietly. "Source field map" then says "No field map for this provider.", which is wrong (the map exists; the visitor just cannot read it). The "Field map ↓" link in the feed box points to an anchor that only exists when the map loaded.
- For an unknown provider, only the catalogue shows "Not found". Every other section still loads and shows its own empty or "n/a" message ("No live listings.", "No market events for this provider yet.", and so on), so the page looks half-broken instead of clearly saying "unknown provider".
- The page has about 15 sections with different time windows: the header "Window" (7D/30D/90D) drives the stats, catalogue and history, while the price chart has its own 24H/7D/30D toggle. Two "Window" controls on one page is confusing.
- Each 2-minute refresh rebuilds the price chart from a spinner, even when nothing changed.
- Jargon: "Prem. now", "Avg prem.", "% cheapest", "Conf.", "Quarantined values", "Schema changes 7d", "L2 ·mock".

### /compare and /compare/{a}-vs-{b}

- **URL**: /compare (empty picker) and /compare?a=&b=. The pair form is /compare/{a}-vs-{b}, for example /compare/h100-80gb-sxm5-vs-h200-141gb-sxm5, /compare/runpod-vs-lambda or /compare/us-vs-europe. Extra query: ?gpu= (the focus GPU for provider or region pairs), ?w= (chart window).
- **Who / sign-in**: anyone. Reads /v1/compare, /v1/gpus/{slug}, /v1/history, /market/detail and /v1/heatmaps, all public.
- **Purpose**: put two GPUs, two providers or two region groups side by side.

**Common header and picker**: the title is "A vs B" (or "Compare"). The subtitle depends on the pair type, for example "GPU vs GPU: price, availability, capability pricing and specs, side by side". The picker bar has:
- "A" input (placeholder "GPU, provider or region")
- a "⇄" swap button (tooltip "swap"; swaps and navigates at once)
- "B" input
- a "Compare" button (Enter also works)
- preset links on the right: "H100 vs H200", "H100 PCIe vs SXM", "A100 vs H100", "B200 vs H200", "RunPod vs Lambda", "Lium vs AWS", "US vs Europe"

Inputs autocomplete GPU slugs, "provider · X" and "region · X".

**Empty state (/compare with no pair)**: a section "Popular comparisons" with buttons "H100 vs H200", "H100 PCIe vs H100 SXM", "A100 vs H100", "H200 vs B200", "L40S vs A100 PCIe", "RTX 4090 vs RTX 5090". Below: "Or type c h100 h200 in the command bar."

**GPU vs GPU layout**

1. Two side cards (blue for A, orange for B): the name (links to the GPU page), "N providers · N priced listings", and the big low price "low /GPU·h".
2. Caveat note: the API's note plus "Shared dimensions:" badges, or "none".
3. Left column:
   - **"Verdicts"**: bullets "Lowest price now", "Median price now", "Per dense BF16 PFLOP (median)", "Per dense FP8 PFLOP (median)", "Per GB of VRAM (median)", "Per TB/s of memory bandwidth (median)". Each reads "X is N% cheaper (a vs b)", "about the same" or "n/a (X missing)". A "†" marks theoretical figures.
   - **"Price history"**: window toggle "7D", "30D" (default), "90D". Four lines: median solid and lowest dashed, for each GPU.
   - **"Same provider, both GPUs"**: table titled "N providers price both". Columns "Provider", A price with "#rank", B price with "#rank", "B vs A". Providers that sell only one are greyed and say "not sold".
4. Right column:
   - **"Market and capability pricing"**: a table with columns A, B and "B vs A". Rows:
     - "Lowest / Median / Highest $/GPU·h"
     - "Providers pricing", "Priced listings", "Available listings", "Sold-out listings"
     - "Efficiency score" and "Market label"
     - five "$ per ... (median)" rows

     Note: "B vs A on prices: green means B is cheaper."
   - **"Spec diff"**: 17 spec rows with columns A, B and "B / A" ("×1.76" or "differs"). Rows that differ are highlighted.

**Provider vs provider layout**: two provider cards (logo, class and note, "N GPUs · N priced"). Then a stat strip:
- "Common GPUs" (sub "N only A · N only B")
- "{A} cheaper on" and "{B} cheaper on" ("N of M")
- "Median B vs A"

Left column:
- a common-GPU table titled "Common GPUs · click one to chart it", with columns "GPU", A, B, "B vs A" and "market →"
- a chart headed "{GPU} at both providers", with window toggle "7D" (default), "30D", "90D", and lines for both providers plus a dotted "market lowest"

Right column:
- "Side by side", a table with rows:
  - "Class", "Source"
  - "GPUs listed", "Live listings", "Priced listings"
  - "Architectures", "Region groups"
  - "Avg premium vs market", "Cheapest-provider share", "Availability (hours)"
  - "Feed failure rate 24h", "Feed latency 24h", "Last good fetch"
- "Only one sells": GPU links per side

**Region vs region layout**: two region cards ("region group", "N GPUs priced"). An optional focus line for ?gpu= ("X: cheapest US $... · Europe $... · B vs A ...", "all regions →"). A stat strip: "GPUs in both", "{A} cheaper on", "{B} cheaper on". Then a table titled "Same GPU across two region groups". Columns:
- "GPU"
- "{A} cheapest" and "{B} cheapest"
- "B vs A"
- "{A} vs global" and "{B} vs global"
- "{A} listings" and "{B} listings"

A note follows with "Regional heatmaps →".

**States**: loading shows "Comparing…" / "Comparing regions…" and "Loading history…". Insufficient states:
- "Unknown": "\"x\" is not a canonical GPU slug, a provider or a region group."
- "These two cannot be compared": API 400s and mixed pairs, followed by "Try two GPUs or two providers."
- "No history to overlay"
- "Regional data unavailable"

Empty states: "No common GPU to chart." and "These providers have no GPU in common right now.". Other API failures use the shared error box without a "Try again" button.

**Refresh**: none. The page loads once.

**Observations**
- There are two different preset lists: seven links in the picker bar, and six "Popular comparisons" buttons that only show when the page is empty. They overlap only partly.
- The "⇄" swap button is an icon with only a "swap" tooltip, and it navigates at once.
- The default chart window differs by pair type: 30D for GPUs, 7D for providers.
- No auto-refresh, unlike every other market page. The "low /GPU·h" prices can go stale on an open tab.
- Errors other than 400 show the error box with no "Try again" (OG.error is called without a retry).
- The "B vs A" colour rule (green = B cheaper) is stated only in small notes and tooltips. A reader who swaps sides has to re-learn which colour is good.

### /explorer (listings explorer)

- **URL**: /explorer. Every filter and the sort live in the URL: gpu, provider, region, min, max, avail, type, tier, count, q, sort, dir.
- **Who / sign-in**: anyone. Reads /listings, plus /v1/gpus and /v1/trust/listings (optional extras), all public.
- **Purpose**: every current listing from every provider, raw, with filters and CSV export.

**Layout, top to bottom**

1. Header: "Explorer", subtitle "Every current listing from every provider, as observed. Filter, sort, share the URL, export CSV." A "Reset" button on the right clears all filters.
2. Filter bar of labelled fields:
   - "Text" (placeholder "gpu, sku, region…")
   - "GPU" (each GPU, plus "(unmapped names)")
   - "Provider", "Region", "Market" (market type), "Tier", "GPUs" ("1×", "2×" ...)
   - "Stock": "Any", "in stock", "not sold out", "sold out", "unknown"
   - "$ min" (placeholder "0.00") and "$ max" (placeholder "∞")

   Text inputs apply after a short pause.
3. Table titled "Listings", toolbar "N of M listings · N GPUs · N providers", CSV. Columns:
   - "GPU": link, or the raw name in grey
   - "VRAM" and "Arch" (when /v1/gpus answers)
   - "Provider"
   - "SKU"
   - "GPUs"
   - "$/GPU·h": "n/a" for 0
   - "Last chg"
   - "$/inst·h"
   - "Region" and "Ctry"
   - "Market" and "Tier"
   - "Intr" (interruptible)
   - "Stock"
   - "Stock basis" (when trust data answers)
   - "vCPU" and "RAM"
   - "Source"
   - "Seen": freshness badge
   - "First seen"

   Many columns drop on narrow screens. Default sort is "$/GPU·h" ascending. 400 rows show, then "Show N more".
4. Note: observed badge, "Prices and stock exactly as each provider reports them, normalized to USD per GPU-hour. Market lines elsewhere use a stricter subset (on-demand, not interruptible, in stock or unknown)." Link "Methodology".

**States**: "Loading listings…". No matches: "No listings match these filters.". A failure shows the shared error box with "Try again".

**Refresh**: reloads /listings every 2 min.

**Observations**
- If a refresh fails, the whole table is replaced by the error box, even though good data was on screen a moment before.
- The table is rebuilt up to twice after the first draw, when the optional /v1/gpus and /v1/trust/listings answers arrive. Columns pop in and the layout jumps.
- "Reset" sits in the page header, while on /gpus, /opportunities and /events "Reset" sits at the end of the filter bar. On /news the same action is called "Clear".
- Up to 24 columns, with terse labels: "Intr", "Ctry", "Tier", "Stock basis", "Last chg".

### /opportunities (opportunity monitor)

- **URL**: /opportunities. URL state: type, gpu, provider, min.
- **Who / sign-in**: anyone. Reads /v1/opportunities and /v1/events/types, both public.
- **Purpose**: list places where listed prices or supply look unusual right now, ranked by score.

**Layout, top to bottom**

1. Header: "Opportunity monitor", subtitle "Where listed prices or supply look unusual right now, ranked by score. Observations about listed prices, not advice or quotes: a listing can change or sell out before you act." On the right, an as-of text "hour {time} · N items" (or "no market history") and a "Methodology" button.
2. Filter bar:
   - "GPU" dropdown ("All GPUs", live GPUs only)
   - "Provider" dropdown ("All providers")
   - "Min score" toggle: "ANY", "25+", "50+", "75+"
   - "Reset"
3. Type tabs: "All · N", then one per type with a count, or "n/a" when the server could not evaluate it. The types are "Wide spread", "Price cut", "New cheap inventory", "Regional dislocation", "Below usual premium", "Scarcity", "New cheapest provider" and "Supply change". The tooltip is the type description or the reason it is unavailable.
4. Body:
   - With "All": one block per type that has items, ordered by best score. Each block title is a clickable type name, the count and the description.
   - With one type: a single table.

   Table columns:
   - "Score": bar plus number
   - "GPU"
   - "Provider / region": "market" when neither applies
   - "Why it is listed"
   - "Numbers": type-specific key-value pairs, for example "spread", "low", "median", "high", "30d p90" or "baseline", "providers"; or "24h ago", "now", "chg"; or "Δ pts", "samples"
   - "Kind": hidden when every row has the same kind; the badge then sits in the block title
   - an unlabelled column with "events" and "route" links

   Has CSV.
5. "Could not evaluate": a table of type and reason. Then "Evaluated, nothing found: ..." naming the types with no hits.

**States**: "Evaluating the market…". Empty: "No opportunities pass the rules right now." (or "... for these filters."), or "No market history yet: the hourly rollup has not run.". One empty type shows "Nothing of this type right now.". An unavailable type shows insufficient "Could not evaluate {type}". Failures show the shared error box with "Try again".

**Refresh**: every 60 s.

**Observations**
- Each row's "route" link goes to /route, which needs an account. The page itself says "not advice", yet invites routing.
- The "Numbers" column packs several tiny key-value pairs with jargon keys ("30d p90", "Δ pts", "ratio", "matched", "providers avail").
- "Min score" options are uppercase "ANY", while other toggles use mixed case ("All").
- A type can be chosen two ways (tabs, and clickable block titles), and both show the description. The page leans on vocabulary ("Regional dislocation", "Below usual premium") with no plain-language line.

### /events (market events)

- **URL**: /events. URL state: type, sev, gpu, provider, since (1, 7, 30, 90, all), cov.
- **Who / sign-in**: anyone. Reads /v1/events and /v1/events/types, both public.
- **Purpose**: a feed of structured changes detected in the hourly market record (price moves, new lows and highs, sold-outs, feed outages).

**Layout, top to bottom**

1. Header: "Market events", subtitle "Structured changes detected in the hourly on-demand market record. Events say what changed, never why." On the right, a count "N events" and a "Methodology" button.
2. Filter bar:
   - "Type" dropdown ("All types", plus each type in lowercase words)
   - "GPU" dropdown ("All GPUs")
   - "Provider" dropdown ("All providers")
   - "Severity" toggle: "ALL", "NOTABLE+", "MAJOR"
   - "Since" toggle: "24H", "7D" (default), "30D", "90D", "ALL"
   - chip "Coverage starts" (off by default; tooltip "coverage_started marks when OpenGrid began recording a provider: not a market change")
   - "Reset"
3. Main area:
   - Left:
     - an "Events per day" bar chart box, with a note such as "N events · N days" (or "newest 1000 of N events counted · ...")
     - the feed: day separators ("Mon Oct 13 2026", "N shown"), then one row per event. Each row has the time, a severity dot, a type tag, the title, numbers ("$2.10 → $1.95", change %), links (GPU, provider, region) and an optional kind tag. Hovering a row shows the full title, notes, cause and reason, and detected time.
     - at the bottom, "Load N more · N older", or "end of feed"
   - Right, **"Event types"** legend: one clickable card per type (tag, name, kind badge, description, "severity X"). Clicking a card filters to that type, and clicking again clears it. A key below explains the "info", "notable" and "major" dots.

   The type tags are "PRICE", "SOLD OUT", "RESTOCK", "ADDED", "REMOVED", "CHEAPEST", "30D LOW", "30D HIGH", "ATL", "ATH", "SPREAD", "REGIONAL", "CAPACITY", "FEED DOWN", "FEED UP" and "COVERAGE".

**States**: "Loading events…". Empty: "No events match these filters in this window.", or with no filters "No market events in this window. The detector runs hourly on the rollup; with little recorded history few events can fire.". If the catalogue fails: "Unavailable: the event catalogue (/v1/events/types) could not be loaded". Feed failure shows the shared error box with "Try again".

**Refresh**: every 60 s it quietly refreshes the first page and the histogram, but only while you have not clicked "Load more".

**Observations**
- The type tags "ATL" and "ATH" (all-time low and high) are trader shorthand, explained only in the side legend.
- The type filter exists twice (the "Type" dropdown and the legend cards). The severity labels are uppercase here ("ALL / NOTABLE+ / MAJOR") but mixed case on the GPU page.
- "Coverage starts" is an obscure toggle name. Its meaning (hide events that only mark when OpenGrid began recording a provider) is in a tooltip only.
- The catalogue failure message shows a raw endpoint path ("/v1/events/types") to visitors.

### /news (news and signals)

- **URL**: /news. URL state: gpu, provider, region, topic, source, min, q, view (feed or timeline), window.
- **Who / sign-in**: anyone. Reads /v1/news, /v1/news/topics, /v1/news/sources, /v1/news/families, /v1/news/{id} and /v1/timeline, all public.
- **Purpose**: GPU-compute news grouped by story and tagged with the GPUs, providers and regions it mentions, with an optional timeline that lines news up against prices.

**Layout, top to bottom**

1. Header: "News & signals", subtitle "GPU-compute news, story-clustered, tagged with the GPUs, providers and regions it mentions. Related to market moves by time and entity only, never presented as a cause." On the right, a view toggle "Feed" / "Timeline".
2. Filter bar:
   - "GPU" text input (placeholder "GPU or family", with suggestions of families and GPUs)
   - "Provider", "Region" and "Source" dropdowns (each defaults to "any")
   - "Min rel." toggle: "all", "20" (default), "40", "60"
   - "Search" input (placeholder "words in title / summary")
   - "Clear", shown only when a filter is set

   Text inputs apply on Enter or blur.
3. Topic chips: one per topic with items in 30 days, with its count. Clicking toggles the topic filter.
4. "Link to this view:" plus the current URL (only when filters are set).
5. Two columns.
   - Left, Feed view: "N of M stories", then story cards. Each card has a relevance score with a meter (hovering or focusing it shows the components: entity points, topic points, source tier ×, recency ×, the formula, and a "methodology →" link), the headline (opens a new tab), a summary, and a meta line. The meta line has the age ("3 h ago"), the source link, the tier, "+N sources: ..." and entity chips (GPU, family, provider, region, topic). Then "Load N more".
   - Left, Timeline view:
     - controls: "GPU variant" dropdown ("choose a GPU…"), "Window" toggle ("24H", "7D", "30D") and "provider: X" when that filter is set
     - a bold "Related, not necessarily causal." note
     - a price chart ("Lowest", "Median provider") with markers, and an "Available listings (observed)" chart ("In stock", "Sold out")
     - "Markers in this window (N)": a list of the newest 30 markers
   - Right:
     - **"Source health"**: "N enabled · N ok · N not ok", then a table with columns "Source", "Health", "Ok", "24h". Clicking a row filters to that source.
     - **"Topics · 30d"**: topic bars with counts (click to filter) and a note with "Methodology →".

**States**: "Loading news…". Empty: "No stored news matches these filters.". Timeline with no GPU: "Pick one GPU variant: prices are per canonical variant, so a family (e.g. “H100”) cannot be charted.". Too little history: insufficient "History building". Side panels show "n/a" when topics or sources are unavailable. Feed failure shows the shared error box with "Try again".

**Refresh**: every 5 min it refreshes the feed (Feed view only, and only if you have not loaded more). The timeline does not refresh.

**Observations**
- The topic filter appears twice: the chip row above the feed, and the "Topics · 30d" panel on the right. Both toggle the same filter.
- "Min rel." is jargon. Its options are bare numbers, where "all" actually means minimum 1 and the default "20" quietly hides lower-relevance stories.
- In the Timeline view, the GPU text box still accepts a family (for example "h100" from a family page's "More →" link), but the timeline needs one canonical variant. The variant dropdown then shows "choose a GPU…" while the filter is actually set.
- The same reset action is labelled "Clear" here and "Reset" on the other market pages.
- "Source health" is an internal ingestion panel ("not ok", "failing", "never") shown on a public news page.

### /heatmaps

- **URL**: /heatmaps. URL state: kind, days, order, empty.
- **Who / sign-in**: anyone. Reads /v1/heatmaps/{kind}, public.
- **Purpose**: matrices of where each GPU is cheap, dear, available or moving, across providers, regions or days.

**Layout, top to bottom**

1. Header: "Heatmaps", subtitle "Where each GPU is cheap, dear, available or moving, across providers, regions and days. Hatched = no data, not zero." On the right, "Order": a chip "Empty rows" (show GPUs with no data) and a toggle "Coverage" (default), "Name", "Value".
2. Control bar:
   - group tabs: "GPU × provider", "GPU × region", "GPU × day"
   - a kind toggle for the chosen group:
     - provider: "Premium vs market", "Availability", "24h change"
     - region: "Cheapest", "Premium vs global", "Priced listings"
     - day: "Volatility", "Availability", "Daily change"
   - on the right, a "Window" toggle "7D", "14D", "30D", "90D" for kinds that use days, otherwise the grey text "current snapshot"
3. Stat strip:
   - "GPUs"
   - "Providers", "Region groups" or "Days"
   - "Cells with data" ("N%", sub "N of M")
   - "No comparison" (sub "GPUs with ≤ 1 provider cell")
   - "Median coverage" or "Median regions"
   - "Widest spread": a GPU link, sub "low → high"
   - "Basis"
4. Legend line: "diverging" or "sequential", the bold kind name and its meaning (for example "Blue = cheaper than the rest of the market, red = dearer."), the unit, the clipping note, the hour, and "Methodology →". Then "N GPUs with no cell hidden ...: links ... Show them".
5. The heatmap. Rows are GPUs (row labels link to /gpu/{slug}). Columns are providers (linking to /provider/{name}), region groups or days. Hatched cells mean no data. Clicking a cell goes to /provider/{name}?gpu={slug} for provider kinds, or to /gpu/{slug} otherwise.

**States**: "Loading matrix…". Insufficient "Empty matrix": "No cells: nothing is priced for this view right now.", or for day kinds "No GPU has a full day in the daily summaries for the last N days yet ...". Failure shows the shared error box with "Try again".

**Refresh**: every 3 min, which replaces the matrix with the loading spinner first.

**Observations**
- The ordering controls ("Order", "Empty rows") sit in the page header, apart from the group and kind controls in the bar below.
- The legend's "Methodology →" link goes to /methodology/dispersion for every kind except provider availability, including volatility, regional cheapest and daily change, where dispersion is not the relevant method.
- Each 3-minute refresh blanks the matrix to a spinner.
- Labels such as "diverging", "sequential", "No comparison" and "Basis" assume charting and statistics vocabulary.

### /indices (index board) and /indices/{id} (one index)

#### /indices

- **URL**: /indices. URL state: family, status, q.
- **Who / sign-in**: anyone. Reads /v1/indices (and per index /v1/indices/{id} and /history on old servers), public.
- **Purpose**: the board of every OpenGrid price index, published or not.

**Layout, top to bottom**

1. Header: "Indices", subtitle "Every OpenGrid index: chain-linked, one vote per provider, published only with enough constituents". On the right:
   - "Family" toggle: "All", "Composites", "Per-GPU", "Spot", "Regional", "Provider class"
   - "Status" toggle: "All", "Published", "Unpublished"
   - search (placeholder "filter: h100, us, spot…")
   - "Methodology" button
2. Stat strip:
   - "GPU Compute Index" (level, 24h change)
   - "7d"
   - "Indices" (sub "N published · N not")
   - one cell per family ("N/M published")
   - "Index hour" (sub "date · vN")
3. Hero cards: one card per composite index (name, id, level, 24h change, 7d sparkline, "N components · 7d ..."). The main GPU Compute card is emphasised. Unpublished cards are dimmed.
4. One table per family, each with a description line above it and a title "{Family} · N/M published", plus CSV. Columns:
   - "Index": name plus id, links to the detail page
   - "Level" or "$/GPU·h": unpublished rows show the last level in dim text with "*"
   - "24h", "7d", "30d", "90d", "YTD", "All"
   - "High" and "Low"
   - "Vol 30d"
   - "Comp." or "Prov."
   - "7d": sparkline
   - "Status": "published", or "not published" with a short reason

   Unpublished rows are greyed. Cells still loading show "·".
5. Note: "observed market prices ... never quotes or execution prices. Changes are measured from the current published level; a change is n/a (hover) when ... * = last published level, shown dim when the index is not published now." Link "How indices are computed →".

**States**: "Loading indices…". Empty: "No index matches these filters.", or "No index has data yet: indices compute hourly from the market rollup once listings are recorded.". Failure shows the shared error box with "Try again".

**Refresh**: every 5 min.

#### /indices/{id}

- **URL**: /indices/{index-id}, for example /indices/gpu-compute or /indices/h100-80gb-sxm5. URL state: window, res, raw.
- **Who / sign-in**: anyone. Reads /v1/indices/{id}, /history, /fixings and /v1/fixings/{id}/verify, all public.
- **Purpose**: one index's level, history, publication record, constituents, definition and (for settlement indices) official fixings.

**Layout, top to bottom**

1. Header: a family eyebrow, the name and id. The subtitle is the description, or the spot warning. On the right:
   - "Window" toggle "24H", "7D", "30D" (default), "90D", "YTD", "ALL"
   - "Res" toggle "AUTO", "1H", "1D"
   - chip "raw cross-section" (not on composites; overlays the unchained statistic)
   - "{GPU} market →" (per-GPU indices)
   - "Methodology"
2. A "Not published" insufficient box when the index is not published now (the reason, plus "last published X at T").
3. Stat strip:
   - "Level"
   - "24h", "7d", "30d", "90d", "YTD", "All-time" (folded when missing)
   - "High" and "Low"
   - "Vol 7d" and "Vol 30d"
   - "Constituents" or "Components" (sub: the method)
   - "Coverage 30d" (sub "N/M h published")
4. Two columns:
   - Left: the level chart (rebases are marked as events, gaps where unpublished), then a publication strip (green "published", red "not published", "N/M hours published"), a hover readout ("hover the chart for each hour's publication status") and a note.
   - Right: **"Definition"**, a key-value list:
     - "Index id", "Family", "Segment", "Unit"
     - "GPU", "Region group", "Provider class"
     - "Price concept" and "Statistic"
     - "Methodology"
     - "First published" and "Chain segment"

     Then **"Related"**: links (GPU market, components) and "Other indices on this GPU" with level and 24h change.
5. **"Constituents now"** (or "Components now"):
   - Per-GPU indices: a table titled "N/M providers included · as of T · statistic: X". Columns "Provider", "Class", "Region", "Vote $/GPU·h", "Included" (for example "excluded: outlier (> 3× from median)"), "Weight in stat", "Recorded since".
   - Composites: columns "Component index", "Weight", "Level $/GPU·h", "Status", "Chain seg.".

   A method note follows.
6. **"Settlement fixings"** (only for settlement indices):
   - a stat strip: "Daily settlement" ("TWAP · time UTC"), "Daily status", "Latest hourly fixing", "Hourly status"
   - a table titled "Official fixings · N daily, N hourly (latest, effective)". Columns:
     - "Fixing time"
     - "Type"
     - "Value"
     - "Status": "final", "restated", "not published" or "provisional"
     - "Flags"
     - "Method v."
     - "Evidence": a truncated id linking to raw JSON
     - a "Verify" button that rechecks the hashes and shows "ok", "mismatch" or "error"
   - a note with links "all restatements" (raw JSON) and "Settlement methodology →"

**States**: "Loading index…" and "Loading history…". Insufficient "No published level": "none of the N stored hours in this window is published". Empty: "No constituent recorded for the latest stored hour." and "No fixing taken yet: an hour is fixed only once the rollup can no longer revise it (≈5 h).". An unknown id shows a header "Index {id}", the shared error box ("Not found", no retry) and "All indices →".

**Refresh**: every 5 min it refetches the index, redraws the constituents and reloads history.

**Observations**
- On the detail page, the 5-minute refresh updates only the constituents and the chart. The stat strip, the "Not published" banner, the definition and the "Related" panel keep their first-load values, so they can contradict the fresh chart.
- The header holds three toggles plus a chip ("Window", "Res", "raw cross-section"). "Res" and "raw cross-section" are expert terms with no plain explanation outside tooltips.
- "Evidence" and "all restatements" open raw JSON API responses in a new tab. That is fine for auditors, but jarring for a general visitor.
- The board packs 15 columns per family table, and the "*" convention (last published level) is explained only in the footnote.
- Errors on the detail page have no "Try again" button.

### /ops (operator data-quality console)

- **URL**: /ops. URL state: provider, q (quarantine status), inc, kind.
- **Who / sign-in**: OpenGrid operators only. Every main read is private (/v1/ops/summary, /providers, /quarantine, /incidents, /schema-changes, /unmapped, /stale). Only "News ingestion" reads the public /v1/news/sources. The menu link sits in the "Operator" group, shown only to admins.
- **Purpose**: one internal view of feed health, held (quarantined) values, incidents, schema changes, unmapped GPUs, stale listings, background jobs, routing probes and news ingestion.

**Layout, top to bottom**

1. Header: "Ops · data quality", subtitle "Internal: feed health, held values, incidents and pipeline jobs. Every figure is read from the quality layer; nothing is estimated." On the right:
   - "Provider" dropdown ("All providers" plus each provider)
   - an update text ("loading…", "updated HH:MM · auto 30s" or "refresh failed HH:MM")
   - a "Refresh" button
2. Stat strip:
   - "Feeds" ("healthy / degraded / down")
   - "Quarantine" ("values held, pending")
   - "Open incidents"
   - "Fetch errors 24h"
   - "Schema changes" ("last 7 days")
   - "Stale listings"
   - "Unmapped GPUs"
   - "Jobs" ("N/M", or "N failing/overdue")
3. A dismissible flash message (with "×") after a quarantine decision.
4. **"Status matrix"** ("provider × check · hover a cell for detail · click a row to filter the page"). Rows are providers, worst first. The columns are coloured cells: "Feed", "Last OK", "Consec. fail", "Fail 24h", "Errors 24h", "p95 lat.", "Listings Δ24h", "Quarantine", "Schema 7d", "Stale", "Incidents". A legend reads "ok", "attention", "failing", "no data", and a sentence names the unhealthy providers.
5. Two columns:
   - **"Feed health"** table. Columns:
     - "Provider", "Status", "Source"
     - "Last OK", "Consec", "Fail%"
     - "p50" and "p95"
     - "Now" and "24h ago"
     - "Last failure"
   - **"Source latency"**: a p50/p95 dot plot. **"Provider API failures"**: columns "Provider", "Endpoint", "Fails", "HTTP", "Last", "Last error". **"Anomalies"**: rows "Duplicate explosions", "Listing collapses", "Missing regions", "Suspicious price moves", "Instance price mismatch", each with a count and a "show →" link that filters the quarantine or incident table.
6. **"Quarantine queue"**: toolbar with a "rule: X ×" chip and a status toggle ("Pending", "Accepted", "Rejected", "Auto", "Superseded", "All"). Table columns:
   - "#", "Provider", "Listing", "Field", "Rule"
   - "Last good → held"
   - "Seen", "Auto"
   - "First", "Last"
   - "Status"
   - "Decision": "Accept" and "Reject" buttons. Clicking one shows "Apply this value?" / "Reject this value?" with a note field and "Confirm accept" / "Confirm reject" plus "Cancel"

   A note explains what accept and reject do.
7. **"Incidents"**: toolbar with a kind dropdown ("All kinds") and a toggle "Open", "Resolved", "All". Columns "Sev", "Kind", "Provider", "Listing / endpoint", "Detail", "Count", "First", "Last", "Status".
8. Two columns:
   - **"Unmapped GPUs"** (with a "JSON →" link): columns "Provider", "Raw GPU name", "Why"
   - **"Stale listings"**: columns "Provider", "GPU", "Region", "Age"
9. **"Schema changes"**: columns "Sev", "Provider", "Endpoint", "Added", "Removed", "Retyped", "Paths", "When", "Status".
10. Two columns:
    - **"Background jobs"**: columns "Job", "Status", "Every", "Runs", "Fails", "Last run", "Took", "Last error"
    - **"Routing & provisioning"**: a probe table with columns "Table", "Rows", "24h", "Failures 24h", "Recent errors", plus links "Deployments →" and "Routing methodology →"
11. **"News ingestion"**: columns:
    - "Source", "Category", "Tier", "Health"
    - "Last OK", "Consec"
    - "Fetch 24h", "New 24h", "Stored", "Avg"
    - "Last error"
12. Footer note with links "data quality" and "data trust", and "Auto-refreshes every 30 s while this tab is visible."

**States**: "loading…" in the header, with spinners in the matrix, health, quarantine and incidents. Empty states:
- "Nothing held: no value is awaiting a decision."
- "No incidents match."
- "Every raw GPU name maps to a canonical GPU."
- "No aging or stale listing."
- "No failed fetch in the last 24h."
- "No background jobs registered."
- "No source schema change recorded."

If the core read fails after a good load, the header just says "refresh failed HH:MM" and keeps the old data.

**Refresh**: every 30 s for every section, plus the manual "Refresh" button.

**Observations**
- The page never shows an up-front "OpenGrid staff only" note (OG.accessNote("operator") exists but is not used here). A non-operator who opens /ops gets the same sign-in box repeated in up to seven sections: matrix, feed health, quarantine, incidents, schema changes, unmapped and stale.
- That sign-in box uses the customer wording ("This needs an OpenGrid account. Accounts are invite-only..."), not the operator wording. An account would not help here.
- For a signed-out visitor, "News ingestion" still loads (its endpoint is public). The result is a page of lock boxes with one working table at the bottom.
- The 30-second timer keeps calling the private endpoints for a signed-out visitor, getting 401s every cycle.
- The page is very large: 13 sections, a matrix of 11 coded columns and many abbreviations ("Consec", "p95 lat.", "Listings Δ24h", "Retyped").

---

# Part 3: Customer journey (Find Compute, Jobs, Route, Keys, Watchlists, Assistant)

Source read: web/pages/find.js, assistant.js, jobs.js, job.js, route.js, keys.js, watchlists.js, web/core.js, plus intelligence/recommend.py and intelligence/performance.py for the data Find Compute shows. Read as of commit 0bc4601.

Access facts that apply to every page below:

- PUBLIC_PAGES is on, so anyone can load any page shell. Data calls decide what a visitor sees.
- Public without sign-in (api/pages.py): POST /v1/recommend, POST /v1/recommend/estimate, POST /v1/assistant, and GET market data such as /v1/capabilities, /v1/scopes, /v1/indices, /v1/families.
- Private (401 when signed out): /v1/me, /v1/jobs/*, /v1/route/*, /v1/keys, /v1/watchlists, /v1/alerts, /v1/usage, /v1/credentials, /v1/billing/*, /v1/deployments.
- The only sign-in that exists in the browser is the operator login at /login. Every "Sign in" button goes to /login?next=<current page>. The web app never sends an API key; API keys work only from the CLI, MCP or curl. So in practice "signed in" on the website means "the operator".
- Pages ask GET /v1/me once per page load to decide if you are signed in. A 401 there means "signed out".
- Confirmation dialogs are one shared component: a title, a body, a cancel button and a confirm button. Some add a checkbox you must tick before the confirm button enables. Escape or a click outside closes the dialog as "cancel".

---

### /find (Find Compute)

**URL:** /find. Deep-link parameters: task, model, method, prec, quant, ctx, conc, seq, mbs, tokens, epochs, budget, region, maxp, a.<field> (answers to follow-up questions), mode=paste, req=<json>, view=table, ask=1.

**Who it is for and sign-in:** Anyone. The header says "Free, no sign-in." The recommendation call (POST /v1/recommend) is public. Signing in only changes two things: whether the server can mark an option as runnable for your account, and whether the "Preview a route" buttons carry a "· sign-in" hint (they show it when signed out).

**Purpose:** Describe an AI workload and get a GPU memory estimate plus GPUs that fit it, priced from observed market data.

#### Layout, top to bottom

1. **Page header.** Title "Find Compute". Subtitle: "Tell OpenGrid what you want to run. It estimates the GPU memory you need and recommends GPUs that fit, priced from live market data. Free, no sign-in." Buttons:
   - "Ask in plain English": opens or closes the assistant panel right under the header (described in its own section below). Adds ?ask=1 to the URL while open.
   - "How it works": link to /methodology/recommendations.
2. **Assistant panel** (hidden until "Ask in plain English" is pressed).
3. **Two-column area:** the input box on the left, the results area on the right. On narrow screens (1100 px or less) the page scrolls to the results when you run.

#### Left column: the guided form ("Name a model" mode)

Numbered steps, each a fieldset with a round number.

**Step 1 "What are you trying to do?"** Four radio cards:
- "Fine-tune a model" (subtitle "Adapt Llama, Mistral, Qwen… to your data")
- "Train a model" ("From scratch, or your own architecture")
- "Run inference" ("Serve a model or batch-run it")
- "Explore options" ("Not sure yet: OpenGrid asks what matters"). This sends no workload type.

Picking a card redraws the form so that the fields for that task appear.

**Step 2 "What model or project?"** Hint: "A model name, a Hugging Face id or just its size. Or analyze your project with the CLI."
- Toggle with two buttons: "Name a model" and "Analyze my project" (switches to paste mode, below).
- Text box, placeholder "e.g. Llama 3.1 8B, Qwen/Qwen2.5-7B or 7B", with a dropdown of 7 suggestions.
- Five quick-pick chips: "Llama 3.1 8B", "Llama 3.1 70B", "Mistral 7B", "Qwen2.5 7B", "Qwen2.5 14B". Clicking fills the box.
- A live note under the box: "Leave blank if you don't know yet." or "<id> · <n>B parameters" or "<n>B parameters (no specific model)" or "OpenGrid looks this name up."
- Only for fine-tuning: "How will you fine-tune?" with a segmented control "Not sure", "Full", "LoRA", "QLoRA" and a one-line hint for each (for example "LoRA and QLoRA need far less memory than full fine-tuning.").

**Step 3 "What matters?"** Hint: "All optional. Options over your max price are left out."
- "Total budget ($)", placeholder "e.g. 50".
- "Max $ per GPU-hour", placeholder "e.g. 3".
- "Preferred region" dropdown: "Any region", "United States", "Europe".
- Training and fine-tuning only: hint "How much data? With it, OpenGrid estimates each option's runtime and the cost of the whole job, so you can see when more GPUs finish for less." then two fields:
  - **"Training data (tokens)"**: free text, placeholder "e.g. 200M or 1.5B", max 24 characters. Accepts forms like 200M, 1.5B, 50k, 2,000,000, 3e8, with or without the word "tokens". Anything else shows "Training data: a token count like 200M, 1.5B or 50000". Sent as training.dataset_tokens.
  - **"Epochs"**: number, placeholder "e.g. 3", must be more than 0 and at most 100,000. Sent as training.epochs. If you give tokens but no epochs, the server assumes 1 epoch and says so in its assumptions.
- Inference only: hint "Serving needs: longer context and more parallel requests need more GPU memory." then "Context length (tokens)" (e.g. 8192) and "Concurrent requests" (e.g. 8).
- A fold "Advanced: precision, quantization" (", batch" is appended for training tasks). Inside: "Precision" (Not sure, fp32, bf16, fp16, fp8, int8, int4), "Quantization" (Not sure, none, int8, int4, nf4 (bitsandbytes), GPTQ, AWQ, fp8), and for training "Sequence length" and "Micro batch size". Hint "Unknown is fine: OpenGrid states what it assumed." The fold opens by itself if any of these are already set.

**Buttons at the bottom of the form:**
- "Show recommendations": validates and runs the recommendation (free, read only).
- "Start over": clears every field and answer and shows the intro again on the right. No confirmation.
- Fold "Exactly what is sent": shows the JSON body ("Only this JSON leaves your browser (POST /v1/recommend).") It updates as you type.

Field errors show inline under each field as you type (for example "Epochs must be more than 0"). Every change is written into the URL, so the page is shareable.

#### Left column: paste mode ("Analyze my project")

Reached by "Analyze my project", by /find?mode=paste, or when the assistant hands over requirements.
- Step "1 Your project", hint "The analysis says what the workload is (model, training or inference, and any limits it found)." The same "Name a model / Analyze my project" toggle.
- Numbered instructions: run `opengrid analyze . --json` (with a "copy" button), with the note that the analyzer reads only allowlisted files and never runs code or reads secrets, weights or datasets; "Paste its output below. Only the requirements part is sent."; and a "Developer docs →" link to /developers.
- Textarea "Analyzer output (JSON, up to 64 KB)".
- A live check under it: grey "Paste the JSON to check it. Nothing is sent until you press the button."; red "This doesn't look like analyzer output yet: N problems" with "Paste the whole output of the command above, unchanged." and a "Technical details" fold listing each error; or green "Looks good" with how many files were inspected and skipped, any skipped paths and analyzer warnings.
- Note: "Your project gets a recommendation, not a launch: OpenGrid only runs its PyTorch MNIST example today."
- Buttons: "Show recommendations" (disabled until the JSON is valid) and "Clear" (empties the textarea, no confirmation). The same "Exactly what is sent" fold.
- When the assistant filled this in, a notice sits on top: "Filled in by the assistant." / "It turned your chat into the requirements below and ran them. Your own answers are kept." with a "Back to my answers" button (only if you came from the guided form).

#### Right column: before running

"What you'll get" with three numbered points: "One recommendation", "A few alternatives", "Whether OpenGrid can run it" ("today OpenGrid recommends GPUs for any workload and runs its PyTorch MNIST example."). Then "Unknowns are fine…" and a link "See an example job (simulated, free) →" to /jobs/new?preset=mnist&sandbox=1.

If the URL already holds form values or answers, the page runs the recommendation immediately on load instead.

#### Right column: the result

**Result header:** "RECOMMENDATION" eyebrow, a badge with the workload type (for example "fine tuning"), "market data as of <time>", and a link "How this works →" (methodology).

**Special case, no GPU needed:** if the server says the workload does not need a GPU: "You probably don't need a GPU", the server's reason, "Run it on your laptop or a regular CPU server…", buttons "Describe a different workload" (/find) and "See an example job". Nothing else is shown.

**Special case, estimate only:** if the server has no /v1/recommend (404 or 405), the page calls /v1/recommend/estimate and shows only the memory estimate block, the follow-up questions and a grey box "Market options not available" / "Live market options are not available on this server yet: this is the memory estimate only."

**1. The hero card ("Recommended").** The server returns up to four "picks" (see below). The hero is the "Best fit for your workload" pick; if missing, the "Lowest advertised price" pick; if missing, "Execution-ready through OpenGrid"; else the first option. Contents:
- Eyebrow "Recommended", a badge naming which pick it is, and a link "Full market for this GPU →".
- Big title "N× <GPU>" (always with a count, so "1× H100"), provider logo and link, region, "<VRAM> per GPU".
- A row of facts, each with a data-kind badge:
  - "Price" (badge observed): $/GPU·h, with "$X/h for N GPUs" for multi-GPU, else "per GPU-hour, observed list price".
  - "Memory needed" (badge estimated): GB needed, with "fits <VRAM> per GPU · NN% headroom" or "does not fit <VRAM>".
  - "Availability": "Available", "Sold out" or "Availability not reported", with "price seen <age>".
  - The fourth fact depends on what the server could estimate:
    - with a theoretical runtime (training plus a dataset size): "Est. runtime" "≈ 6.2 h" with the range and "theoretical, not a benchmark";
    - else, if you gave a budget: "Your budget" "≈ N h" "of runtime at this price";
    - else: "Runtime" "Unknown" with "add your dataset size to estimate it: cost is per hour until then".
  - Only when the server sent a whole-job cost: "Est. whole job" (badge estimated) "$X", with the range and "· runtime x price".
- For multi-GPU options, one line: "N GPUs", "<total VRAM> total, split across N GPUs", the interconnect label (hover shows its detail), and "≈ 1.8× one GPU (estimated)".
- "Why it fits": up to three bullets (the pick reason plus the option's own reasons).
- "Can OpenGrid run it?" line, one of: "Yes: OpenGrid can run this" ("After a free route preview and OpenGrid's approval of the exact price."); "Not right now: sold out"; "Not right now: the price is out of date"; "Not yet: recommendation only" ("Rent it from <provider> directly. OpenGrid only runs its PyTorch MNIST example today.").
- Buttons:
  - "View prices for this GPU" (primary): /gpu/<slug>.
  - "Preview a route": /route?gpu=<slug>&count=<n> (or the server's own preview URL). Tooltip "Estimates which provider OpenGrid would select for this GPU. Free: it spends nothing." plus "Needs sign-in." and a "· sign-in" suffix when signed out.
  - "See an example job" (only when OpenGrid cannot run this option): /jobs/new?preset=mnist&sandbox=1, a free simulation.

**2. "Other good options".** Up to three rows, other picks first, then the cheapest of each tier not shown yet. Header shows "3 of N · all in Advanced details" when there are more. Each row: its label (pick name or tier name), "N× GPU" link, provider and region, an availability badge, "NN% memory headroom" or "does not fit", "OpenGrid can run this" or "recommendation only", the $/GPU·h price with "$X/h total" or "observed", and when known "≈ $X job" (whole-job cost). The pick's reason appears as a small line under it.

**3. The answers strip.** Three or four side-by-side cells, one per pick, never merged:
- "Lowest advertised price": the cheapest fresh listing that fits the memory estimate.
- "Best fit for your workload": headroom and price in balance, on fresh data.
- "Execution-ready through OpenGrid": the best option OpenGrid itself can launch.
- **"Lowest cost for the whole job"** (fourth cell, only when the server returned the lowest_estimated_job_cost pick, which happens for training or fine-tuning with a known dataset size): shows "≈ $X for the job" instead of an hourly price.
Each cell reads like "2× H100 · Lambda · $2.49/GPU·h" and links to the GPU page. An empty cell says "Nothing fits right now"; the execution cell says "None for this workload today" plus why (for example "Sign in to see what OpenGrid can run for your account." with a "Sign in →" link, or "OpenGrid can only launch its PyTorch MNIST training demo today…" with "Try the training demo (free simulation) →").

**4. "Improve the estimate"** (only when the server asks questions). "Optional. Answer any of these and the recommendation updates." Up to three questions shown, the rest in a fold "N more questions". Each question has either choice buttons (pressing again clears the answer), or a number or text box with an "Update" button (Enter also works), or "Answer by editing your input." Every answer re-runs the recommendation at once and is stored in the URL as a.<field>. The server asks for the dataset size here too when it could estimate runtime with it.

**5. A grey explanation line:** "Prices are observed list prices, not quotes. Memory, cost and fit are estimates. Runtime is never guessed, so no GPU is called faster without a benchmark."

**6. "Advanced details"** fold (closed unless ?view=table). Summary: "(memory breakdown and assumptions, the three picks, all N options, excluded listings)". Inside:
- **Estimate block:** "Estimate" eyebrow, badges "estimated" and "confidence high/medium/low/none", the method. Big number "GPU memory needed" ("recommended, with headroom"), "Theoretical minimum", "Range", "Per GPU" (when more than one GPU). Facts "GPUs" (suggested count, "(min N)"), "Runtime" (always "Unknown" here, with the server's reason, which for training says each option has its own theoretical runtime), "Fast interconnect" ("Needed"/"Not needed"). "Where the memory goes": a coloured stacked bar plus a legend (Model weights, Gradients, Optimizer state, Activations, KV cache, Framework overhead). "Assumptions": first four, the rest in a fold "N more".
- **The pick cards**, one per pick (three or four), each with its title, a one-line description, the pick reason and a full option card, or "Nothing here" with the reason and, where useful, a button ("Sign in" or "Try the training demo (free simulation)").
- **"All options · N"** with a "Cards" / "Table" switch (the choice is saved in the URL as view=table).
  - Cards view: grouped by tier. Tiers are "Lower cost" ("Cheapest options that still fit"), "Balanced" ("Headroom and price in balance"), "More throughput" ("More total throughput across the GPUs, estimated from spec sheets (not benchmarked), for more money"), and "Other". Each compact option card shows: "N× GPU" link and variant and VRAM; a "more GPUs" badge for a scale-up option (a doubled count of a GPU that already fits; tooltip "A larger shape of a GPU that already fits: more total throughput, more per hour"); provider and region; an observed badge, price, "N GPUs = $X/h" or "1 GPU", a freshness badge, a "stale" badge if not fresh, an availability badge; the memory-fit bar ("X GB needed of Y GB per GPU · NN% headroom" or "does not fit"); the performance block; a fold "Cost, why and limitations"; the readiness line ("OpenGrid can run this", "Sold out, not deployable", "Price is stale, not deployable", "Recommendation only: OpenGrid can't run this workload yet", "Recommendation only: OpenGrid can't launch this for you yet") with a fold "N reasons"; and buttons "View GPU", "Preview a route" and (non-compact cards only) "See an example job".
  - **Performance block** inside each option card (only when the server sent a basis, a multi-GPU option or a runtime): heading "Performance" with an "estimated" badge and the basis label (for example the spec-sheet throughput it is based on). Lines: for multi-GPU "N GPUs", "<total> total, split across N GPUs", the interconnect label and "≈ 1.8× one GPU (1.6× to 1.9×)"; "1.6× the balanced pick (1.3× to 1.9×, spec sheet)" when it is not exactly 1; "Est. runtime ≈ 6.2 h (4.3 h to 11 h)" and "Est. whole job $X ($Y to $Z)"; and the footnote "Theoretical: spec-sheet throughput and stated assumptions, not a benchmark. Startup, storage and egress are not included."
  - **Cost block** in each card: "Cost if it runs for" (estimated) with a few "N h $X" scenarios, "Your budget buys about N h of runtime", "Not included: …", "billed per …".
  - **Table view** columns: "Tier", "GPU", "Provider", "Region", "$/GPU·h", "GPUs", "Rel. perf" (estimated total throughput vs the balanced pick, "1.6×"), "Est. job $" (whole-job cost), "Memory fit", "Available", "Seen", "OpenGrid" ("can run", "stale", "sold out", "recommendation only"), and two small buttons per row "GPU" and "Route". Sorted by price ascending; column headers sort; CSV download named opengrid-find-options.csv.
- **"Excluded · N"** fold: table "GPU", "Provider", "$/GPU·h", "Why" (code badge such as over max price), "Detail".
- Server notes (for example "Options are ranked by their estimated whole-job cost…", or "No multi-GPU scale-up options: a 3.8B-parameter model gains little from more GPUs…").
- Closing note on observed vs estimated.

**When nothing fits:**
- If listings were excluded only by your price limits: "Nothing fits your max price per GPU-hour" (or "your budget", or both), "GPUs with enough memory exist, but N listings were left out…", "Closest options above your limit" (up to three, with prices). Buttons (guided form only): "Show options up to $X/GPU·h" (sets max price to the cheapest excluded price and re-runs) and "Remove my price limits" (clears budget and max price and re-runs). In paste mode it says "Raise the limit in your requirements (constraints) and run it again."
- Otherwise: "No GPU listing fits this workload right now. Advanced details lists what was left out and why; relaxing the price or region usually helps." plus the estimate block.

#### What the server's performance data means (intelligence/performance.py, recommend.py)

- Every number in the performance block is estimated from vendor peak specs, never a benchmark. Training uses dense tensor throughput at the training precision; inference uses memory bandwidth.
- Multi-GPU scaling is an OpenGrid planning table by interconnect (NVLink, NVLink bridge, PCIe) and GPU count, shown with a range. Two GPUs are never shown as twice one GPU.
- Runtime is only estimated for training and fine-tuning, and only when the parameter count and the amount of data (dataset tokens, or samples and sequence length) are known: FLOPs = 6 x parameters x tokens for full training, 4 x for LoRA/QLoRA, plus 2 with gradient checkpointing, divided by GPUs x peak x scaling efficiency x utilization (35% central, 20 to 50% range).
- Whole-job cost = theoretical runtime x observed instance price, rounded to the provider's billing unit. When any option has one, ranking uses the whole-job cost instead of the hourly price, and the fourth pick "Lowest cost for the whole job" appears.
- For models of 1B+ parameters the server also adds a scale-up option (double the count) per fitting GPU, so "2× H100" can compete with "1× H200".

#### States

- Loading: "Estimating memory and ranking GPUs that fit…".
- Input errors: amber box "Check your input" with up to 8 errors.
- Server errors: a box with a plain title ("OpenGrid couldn't use this input" for 422, "Too many requests in a short time" for 429, "OpenGrid had a problem answering" for 5xx, "The request didn't go through", or "Couldn't reach OpenGrid"), one help sentence, a "Try again" button and a "Technical details" fold.
- Signed out: everything works; options are rarely executable, the execution cell says "Sign in to see what OpenGrid can run for your account.", and route buttons carry "· sign-in".

#### Refresh behaviour

No automatic refresh. A result is computed when you press "Show recommendations", answer a question, use a budget button, load a URL that has inputs, or when the assistant hands over requirements. A newer request cancels an older one.

#### Observations

- The page still promises that runtime is never estimated while it now shows runtime estimates. The grey line under the result says "Runtime is never guessed, so no GPU is called faster without a benchmark.", and the Advanced footnote says "Runtime is never guessed." The same result can show "Est. runtime", "Est. whole job", a "Rel. perf" column and a "More throughput" tier.
- The hero tells inference users to "add your dataset size to estimate it" when there is no runtime and no budget. Inference has no dataset field, and the server only estimates runtime for training.
- Copy still says "three" when there are four answers. The strip's aria-label is "Three different answers, never merged", the Advanced summary says "the three picks", and the picks section's aria-label is "Three picks", even when "Lowest cost for the whole job" is shown.
- Inside Advanced, the estimate block's "Runtime" fact always reads "Unknown" (the overall estimate never carries a runtime), even when every option card below it shows an "Est. runtime". The server's reason text explains this, but the "Unknown" sits right next to concrete runtimes.
- The "Est. whole job" hero fact starts its subtitle with " · runtime x price" when the server sends no range.
- find.js passes `requirements()` and `onRecommendation` to the assistant panel, but assistant.js never reads them. So the assistant does not know what you already typed in the form, and the recommendation it computes is not drawn directly; the page re-runs its own request from the requirements the assistant returns.
- If the assistant returns requirements that fail the client-side check, the hand-over is silently dropped: there is no message on the page.

---

### Assistant panel (as used on /find and /jobs)

**Where:** /find, behind "Ask in plain English"; /jobs, in the fold "Ask about your jobs in plain English". (The same panel is also the full /assistant page.)

**Who and sign-in:** Anyone can ask (POST /v1/assistant is public and read only). Two kinds of buttons inside replies need sign-in: "start the free demo" or "cancel a job" action cards, and route previews.

**Purpose:** Plain-English questions about GPU prices, which GPU fits a job, comparisons, price moves, and (on /jobs) your jobs.

#### Layout

- Header: title ("OpenGrid Assistant" on /find; "Ask about your jobs" on /jobs), a note, an "Examples" button (appears after the first question, brings the example chips back) and a "New" button (tooltip "Start a new conversation": clears the log and the context, no confirmation).
  - /find note: "Answers come from OpenGrid's live market data; estimates are labelled. It never rents a paid GPU: it can only start the free simulated demo or cancel a job, when you click."
  - /jobs note: "It can cancel a job only when you click and confirm. It never launches a paid GPU on its own."
- Conversation log (keeps the last 60 messages).
- Example chips (hidden after the first question; shown again when a reply was not understood):
  - /find: "Cheapest available H100", "What GPU should I use to fine-tune Llama 3 8B with LoRA?", "Affordable GPU for inference of a 7B model", "Compare H100 SXM and H200", "Why did H200 prices change?", "Can I fine-tune a 13B model with QLoRA within $50?", "Specs of the L40S".
  - /jobs signed in: "How are my jobs doing?", "Find a GPU to fine-tune Llama 3 8B under $50", "Cancel my job". Signed out: "Find a GPU to fine-tune Llama 3 8B under $50", "What can OpenGrid run today?".
- Input box (2 rows, max 2000 characters). Placeholder on /find "Ask about GPU prices, which GPU fits your job, comparisons…"; on /jobs "e.g. how is my job doing? · cancel my job · find a GPU to fine-tune Llama 3 8B" (or a shorter one when signed out). Enter sends; Shift+Enter is a new line. Button "Ask".

#### Reply cards

Each reply is a sentence plus cards:
- **Recommendation card:** "Needs about N GB of GPU memory across N GPUs · <confidence> confidence" (estimated), then three picks: "Cheapest that fits", "Best for this workload", "Launchable through OpenGrid", each with GPU, provider, region, price, "fits in memory · NN% headroom", the reason, and buttons "Market" and "Route preview" (shown as "Route preview (sign-in)" when signed out). A table of the top 5 options (GPU, Provider, Region, $ per GPU-hour, Fits in memory, Launch via OpenGrid). Legend: "Memory and cost are estimates; prices are observed provider listings, not quotes; runtime is unknown without a benchmark." On /jobs only, a button "Open in Find Compute →" (/find?mode=paste&req=…). On /find there is no such button: instead the requirements fill the Find Compute form (paste mode, with the "Filled in by the assistant." notice) and the page runs its own recommendation.
- **"No GPU needed"** card.
- **Prices card:** table GPU, Provider, Region, $/GPU-h, Available. Empty: "No fresh, in-stock listing right now."
- **Compare / specs card:** Memory (GB), Memory bandwidth (TB/s), BF16 peak, FP8 peak, Lowest $ per GPU-hour, Median $, Providers.
- **Market context card:** a warning "Correlation, not causation.", per-GPU summaries, a moves table (Hour (UTC), Move, %, From, To) and "News in the same window (untrusted public text; not a cause):".
- **Questions card:** a follow-up question with chip buttons; each chip sends a prepared message.
- **Jobs / job card:** job name links, status text, "epoch N of M", a "simulated" badge, cost ("up to $X" or actual). "All jobs →" when there are more. Empty: "No jobs."
- **Template card:** what the one allowlisted template is, "Every launch is approved by the OpenGrid operator first.", hard caps and the expected runtime.
- **Action card**, two kinds only:
  - Start demo, button label from the server (default "Start free simulation"). Free; runs on click with no confirmation (a sandbox MNIST job). On success: "Started." and "Watch it run →".
  - Cancel job, red button "Cancel job-…". Opens a dialog "Cancel job-…?" with the server's text (default "Before launch the approval is rejected; after launch the GPU is terminated. This cannot be undone."), buttons "Keep it running" and "Cancel job". On success "Cancel requested." and "Open the job →".
  - When signed out the card shows a "Needs sign-in" tag. On a 401/403 the card says "Starting or cancelling jobs on this website needs the operator sign-in. From your terminal or AI assistant it needs an API key with route:execute." with "Sign in" and "How keys work →".
  - On /jobs a successful action reloads the job list.
- Link cards are gathered into one row of small buttons under the reply.
- If the server's wording could not be matched to its data: "Some wording above could not be matched to the data; rely on the figures in the cards."

#### States

- Waiting: "Thinking…".
- 429: "Too many questions in a short time; wait a minute and try again."
- 422: "That question or conversation is too long. Start a new conversation." (the context is reset).
- Other errors: the standard error box.

#### Refresh behaviour

None. Each question is one request; the server's returned context is sent back with the next one (dropped if it grows over 8000 characters).

#### Observations

- The assistant's recommendation card still shows only three picks and no performance data. It ignores the new fourth pick "Lowest cost for the whole job" and per-option runtime and job cost. Its legend says "runtime is unknown without a benchmark", while Find Compute now shows theoretical runtimes for the same request.
- On /find, find.js passes the current form requirements, but the panel never uses them (see the /find observations). Asking "what about with QLoRA?" therefore starts from nothing, not from the form.
- The template card writes its runtime range with a typographic range dash between the two numbers, while the rest of the UI writes "10 to 20" or "10-20".
- Until /v1/me answers, signed-in state is unknown, so the "Needs sign-in" tag and the "(sign-in)" label are left off for that brief moment.

---

### /jobs (My Jobs)

**URL:** /jobs. Nav label "My Jobs", locked "Needs an OpenGrid account".

**Who and sign-in:** The account holder (in the browser, the operator). Signed out, the list is replaced by an access panel; the assistant fold still works.

**Purpose:** See your training jobs (simulated and real): what needs attention, what is running, what finished, and what each cost.

#### Layout, top to bottom

1. **Header** "My Jobs", subtitle "Your training jobs, simulated and real: where each one runs, how it's going and what it cost." Buttons:
   - "Refresh": reloads the list now.
   - "Start a job" (primary): /jobs/new.
2. **Job list** in groups, newest first in each:
   - "Needs attention" (count): jobs whose GPU may still be billing, whose termination failed, whose quote expired, or that failed with a GPU possibly still up.
   - "Active" (count).
   - "Completed" (count): first 10, then a button "Show N more" (shows all; stays expanded).
   - Each job is a clickable card linking to /jobs/<id>: the job name (or "unnamed job"), a tag "Simulation" or "Real GPU", the age ("just now" / "5 min ago"), a status line with an icon (for example "Training (simulated)", "Waiting for OpenGrid approval", "Finished (simulated)", "Failed: <reason>", "Cancelled", "Price quote expired: needs a new quote", "Needs attention: the GPU may still be billing"), and a meta line "No GPU used · free" or "<GPU> on <provider> · up to $X" or the actual cost.
   - Below the groups: "OpenGrid runs one supported job today: the PyTorch MNIST example, as a free simulation (real GPU runs once they're enabled)." and a button "Start a supported job" (/jobs/new).
3. **"All job details"** fold: "(every column: template, state, provider, GPU, cost estimate and actual)". A table with columns "Name", "Job", "Template", "State" (raw state badge), "Outcome", "Provider", "GPU", "Created", "Runtime", "Est. max", "Actual", "Run" (SIMULATED or real). Rows link to the job; rows that may be billing are highlighted; CSV opengrid-jobs.csv. A note explains Est. max vs actual and links "Execution safety →". The fold is hidden when there are no jobs or when signed out.
4. **"Ask about your jobs in plain English"** fold: the assistant panel in compact form (see above). Summary note: "(it can cancel a job only when you click and confirm; it never launches a paid GPU)".

#### States

- Loading: "Loading your jobs…".
- Empty: "No jobs yet" with "A job is a workload OpenGrid runs for you, from choosing a GPU to shutting it down and settling the cost. Today the supported job is a PyTorch MNIST training example." and "Start with the free simulation: it walks the example through every step, with no GPU and no charge." Buttons "See the example job" (/jobs/new?preset=mnist&sandbox=1) and "Find a GPU for my own model" (/find).
- Signed out (401): "Sign in to see your jobs", "Your jobs are private to your OpenGrid account. Signed out, you can still see how a job runs and get GPU recommendations." Buttons "Sign in", "See the example job", "Find Compute". A 403 says "This account can't see jobs" without the sign-in button.
- Server without the endpoint: "GET /v1/jobs is not available on this server yet." Other errors: the standard error box.

#### Refresh behaviour

Reloads every 15 seconds while the page is open (paused when the tab is hidden), plus the "Refresh" button, plus after an assistant action.

#### Observations

- Signed out, the page keeps calling GET /v1/jobs every 15 seconds and redraws the same sign-in panel each time, although the answer cannot change without a full page reload.
- "Show N more" has no way back to the short list.
- The sign-in panel says jobs are "private to your OpenGrid account", but the only browser sign-in is the operator's. A customer with an API key has no way to see their jobs on the website.

---

### /jobs/new (Start a job)

**URL:** /jobs/new. Parameters: preset=mnist or sandbox=1 (the example-job links: highlight the example card), real=1 (highlight the real-GPU card and check it at once), advanced=1 (open the full form directly), template=<id>, preview=1 (with advanced=1: preview at once).

**Who and sign-in:** Anyone can open it and read the walkthrough. Running the simulation, previewing a route and submitting all need sign-in.

**Purpose:** Start the one supported job (PyTorch MNIST), either as a free simulation or on a real GPU, or go to Find Compute for your own model.

#### Simple view (default)

**Header** "Start a job", subtitle "OpenGrid runs one supported job today: a PyTorch MNIST training example. Try it as a free simulation, or get a GPU recommendation for your own model." Button "← My Jobs".

Three cards side by side:

**1. "Example job"** (badge "simulated"). Title "See how OpenGrid runs an example AI training job". Text about the small digit-reading model. Facts "Simulated: no GPU is rented", "No provider charge", "Finishes in seconds".
- Signed in: an "Epochs" box (1 to 10, default 3) and a button **"Run the simulation"**. Free; no confirmation; creates a sandbox job and opens /jobs/<id>. While running: "Running the simulation…". On error, the plain error box.
- Signed out: amber notice "Running the simulation needs an OpenGrid account." / "Nothing runs and nothing is stored until you sign in. You can read the walkthrough below without one." with a "Sign in" button. The walkthrough opens by itself.
- Button "Show the walkthrough": shows a "Walkthrough" panel (badge "example only: not a real job", button "Hide") with four steps (Preparing, Running, Saving results, Complete) and a table "The placeholder numbers the simulation records" (Epoch, Train loss, Test accuracy), noting they are placeholders.

**2. "Real GPU"** (badge "max $2.00"). Title "Run the example on a real GPU". Text: "The same MNIST job on a rented GPU, with hard limits: max $2.00 total, automatic shutdown after 30 min. OpenGrid approves the exact price before anything launches." Then a status box filled from the server:
- "Real GPU runs are available" with a button **"Check the price (free)"**.
- "Live GPU execution is not enabled yet" with "Nothing can be launched on a real GPU right now. The simulation and the free route preview still work." and a list "What needs to happen first:" in plain words. Signed in: a button **"Preview the route (free)"** and, for admins, "Admin: execution settings →".
- Signed out: either "Live GPU execution is not enabled yet" (from public capabilities, no button) or "Sign in to see whether real GPU runs are available" with "Sign in".
- After checking: either green "Ready: N× GPU on <provider> · $X/GPU·h · max $2.00 total · automatic shutdown after 30 min" with a button **"Review and run…"** (opens the submit confirmation, below; this is the money path), or "Preview only. OpenGrid would pick <GPU on provider>. <plain reason> Nothing was rented or charged." with "See the full preview" (opens Advanced and scrolls to it) and, for admins, a "For admins" fold with the technical blocker and its fix link.

**3. "Your model"** (badge "recommendation only"). "Run your own model": "OpenGrid can't run your own code yet. Find Compute tells you which GPU fits your model, where it's cheapest, and whether OpenGrid could run it." Button "Find a GPU for my model" (/find).

**"Advanced details"** fold below: "(the full job form: route preview, execution readiness, limits, SSH access)", with the note "Allowlisted templates only. Preview is free and never calls a provider; a real run waits for OpenGrid's approval." and the full form.

#### Full form (inside Advanced, or the whole page with ?advanced=1)

With ?advanced=1 the header is "New training job", subtitle "Allowlisted templates only · preview is free and never calls a provider · a real run waits for OpenGrid approval", buttons "← My Jobs" and "Execution safety".

Left: "Job request" form.
- Button "Reset to the MNIST defaults" (refills: 1 GPU auto, US, balanced, on-demand, ≤ $3/GPU·h, ≤ $2 total, 30 min). No confirmation.
- "Name" (default opengrid-first-training-test; lower case letters, digits, . _ -).
- "Job type": fixed "Training" ("the only job type in this release").
- "Template" dropdown, with an info line (framework, dataset, model, GPU needs, "expected N-M min on a real GPU (estimate)"). If templates could not be loaded: " · sign in to load the template's exact limits (built-in defaults shown)" (signed out) or " · templates unavailable: built-in defaults; the server re-validates".
- "Training settings": "Epochs", "Batch size", "Learning rate", "Seed", each with "min to max · default N".
- "Resources": "GPU" (text, default auto, "auto = every GPU meeting the template's requirements"), "GPU count" ("the template runs on 1 GPU"), "Region" dropdown (Any region, US, Canada, Europe, UK, APAC, Middle East, LATAM, Africa) with a "Strict" checkbox and a hint explaining strict vs non-strict, "Min VRAM GB" ("blank = template minimum"). Line: "Capacity: on-demand only. Spot / interruptible GPUs aren't supported in this release."
- "Routing strategy" segmented: "Balanced", "Cheapest", "Soonest available", "Stable", each with a hint.
- "Limits (hard caps; the quote must fit)": "Max $/GPU·h", "Max total $", "Max runtime min" ("automatic shutdown after this · 5-60").
- "Access": "SSH public key (optional)" with a long note that, if empty, an ephemeral key is generated and discarded so nobody can SSH in. Pasting a private key gives "That is a PRIVATE key. Never paste it anywhere…".
- "Sandbox" checkbox: "SIMULATED: no GPU, no spend".
- Buttons: **"Preview route"** (Enter also works; free, writes an audit record, never calls a provider) and **"Submit for approval…"** (renamed "Submit sandbox job…" when Sandbox is ticked). A "form changed: preview again" marker appears after edits. A summary line "N fields to fix: …".
- Footer note about preview, submit, quote and admin approval.

Right: the readiness strip and the preview result.
- Readiness strip (from GET /v1/jobs/readiness): "READY FOR A REAL RUN" or "REAL RUN BLOCKED" with the closed gates, mode badges, and a fold "N checks · N provisionable providers: details". Signed out it is simply empty.
- Before previewing: "The MNIST defaults are filled in. Preview route shows the market's best listing, the route OpenGrid can actually execute now and why, or exactly what blocks a real run." and "Tick Sandbox to walk the whole lifecycle…".
- **Preview result** (shared with the job page): a request bar (GPU, region, caps, minutes, strategy, mode badge, "live provisioning off", market data freshness). Blockers: for non-admins "A real run can't start right now" with plain reasons and a "Technical details (N)" fold; for admins "NOTHING IS EXECUTABLE RIGHT NOW" with each blocker and its fix link, grouped per provider. "Submit would" line (for example "create the job + a quote → PENDING APPROVAL"). Three cards: "EXECUTION-READY ROUTE" (badge executable or not executable), "MARKET RECOMMENDATION" ("information only"), "SANDBOX". Cost cells "Est. typical total", "Est. max total", "Your cap", "Strategy". "Why it won" (factor list and score bar), "Workload requirements", "Next-best alternatives · N" table, "Exclusions · N", "Ineligible GPUs · N", notes.

#### Submitting (the money path)

1. If the form changed since the last preview, it previews again first.
2. If a real run is not executable: dialog "A real run can't start right now" ("OpenGrid only submits a real job when a GPU is execution-ready. Nothing was created or charged. The sandbox (simulated) run still works.") with reasons; buttons "Close" and "OK". Nothing is created.
3. Otherwise a confirmation dialog:
   - Sandbox: title "Run a SIMULATED sandbox job", buttons "Back" and "Run the simulation". Free.
   - Real: title "Submit the job for approval", buttons "Back" and "Submit for approval". You must tick "I accept a maximum cost exposure of $X and a N-minute runtime ceiling" first. The body table lists Job, Route, Max cost exposure, Price cap, Automatic shutdown, SSH. In LIVE mode it warns a provider may launch at once.
4. On success the browser opens /jobs/<id>. If the outcome is unknown (network), "Outcome unknown." with "Retry safely" (same request key, cannot create a second job).

#### States

- Loading: "Loading…" then cards; real card "Checking whether real GPU runs are available…"; preview "Validating the workload and ranking every eligible GPU…"; submit "Creating the job: validation, selection, quote…".
- Errors use plain titles: "Sign in to do this" (with a "Sign in" button), "Your account isn't allowed to do this", "Some values aren't accepted", "OpenGrid had a problem", "Couldn't reach OpenGrid", each with a "Technical details" fold (API keys need route:preview to preview and route:execute to submit).

#### Refresh behaviour

None automatic. Readiness and templates load once per visit.

#### Observations

- The "Real GPU" card's badge and text ("max $2.00", "30 min") are drawn once from the preset. Editing the limits in Advanced does not update them, and "Review and run…" submits the edited form values.
- Two routing vocabularies: this page offers "Balanced, Cheapest, Soonest available, Stable" while /route offers "Cheapest, Soonest available, Balanced, Most stable" (plus "Custom"), in a different order with a different name for the same mode.
- Signed out, the example card says the simulation "needs an OpenGrid account", but accounts are invite only and the browser sign-in is operator only, so a customer cannot run even the free simulation on the website.
- The page uses "Preview route" (the form button), "Preview the route (free)", "Check the price (free)" and "See the full preview" for closely related free actions, which makes it hard to tell them apart.

---

### /jobs/{id} (one job)

**URL:** /jobs/job-<hex>. Parameter adv=1 opens Advanced details.

**Who and sign-in:** The job's owner (the operator in the browser). Signed out: "Sign in" panel.

**Purpose:** Follow one job from start to finish: progress, results, cost, and whether a GPU might still be billing.

#### Layout, top to bottom

1. Link "← My Jobs".
2. **Header:** the job name as title, then where it runs ("Free simulation: no GPU, no provider, no charge" or "1× H100 on Lambda US" or "GPU not chosen yet"). The cancel control, when allowed:
   - "Cancel job…" (before launch). Dialog "Cancel this job?", body "Nothing has launched yet, so nothing is charged. The job ends here and its records are kept." plus "Safe to retry: a second click after a network error can't act twice." Buttons "Keep job" and "Cancel job".
   - "Terminate GPU…" (a GPU may exist). Dialog "Stop the job and terminate the GPU?", body about the provider confirming. You must tick "I understand the GPU and its local data are destroyed; results not yet uploaded are lost". Buttons "Keep running" and "Terminate GPU".
   - None for simulations, finished jobs, or jobs already cancelling or shutting down.
3. A big status line with icon plus the "Simulation" / "Real GPU" tag.
4. For simulations, a banner "SIMULATED" / "No GPU was used and nothing was charged. Every number on this page is synthetic."
5. Flash area (cancel progress, "Refresh failed" warnings).
6. Billing alarm for real jobs: red "POSSIBLY BILLING" with explanation and "Open deployment →", or amber "Job failed: instance not yet terminated".
7. Error block if the job failed: a plain title (for example "Training ran out of GPU memory"), one line about charges, and a "Technical details" fold (code, reason, admin fix link).
8. **"At a glance"** cells: "Run" (Simulation / Real GPU), "Status" (with "finished" or "updates every few seconds"), "Result" ("Yes", "No", "Cancelled", "The simulation completed", "Not known yet"), "GPU" ("No, no GPU was ever used", "Yes", "Possibly", "No, terminated (provider confirmed)"), "Cost" ("$0.00 (simulated)", "$X", "$0.00 (nothing launched)", "Not known yet"), "Outputs" (file count and names, or "None yet").
9. **"Next:"** line with one suggestion and, when relevant, a button: "Open the GPU's deployment", "Open the approval ticket" (admins), "Start a new job", "Start another job", "Find a GPU for my model". For a running simulation: "Simulations finish on their own; there's nothing to stop."
10. **"Progress"** box: badge "free simulation" or "real GPU run", then numbered stages (8 for real: Preparing, Selecting GPU, Awaiting approval, Starting GPU, Running, Saving results, Shutting down, Complete; 4 for a simulation: Preparing, Running, Saving results, Complete), each marked done, now, skipped or failed. The last stage reads "Ended: failed", "Ended: cancelled" or "Complete (simulated)" when relevant. For real runs it also answers "Can real money be spent?" and "Is action needed?".
11. **Approval box** (pending approval or quote expired): "PENDING APPROVAL" or "QUOTE EXPIRED", the quote countdown, the route request id. Admins get "Open approval ticket →" or "Open the ticket (re-quote) →" (to /route?rr=…). Others see "Waiting for OpenGrid approval. Nothing is launched or charged until an admin approves this exact quote." The quote price and any limit violations.
12. **"What happened"**: the job's events in plain words, oldest first, with times.
13. **"Training progress"** (with " · SIMULATED (synthetic numbers)" for simulations): stats "Epoch", "Latest loss", "Test accuracy", "Elapsed", "Progress" bar, "Runner result"; two small charts "Train loss" and "Test accuracy" by epoch. Before metrics: "Metrics appear once training reports its first epoch…" or "Training started; the first epoch's metrics arrive when it ends."
14. **"Artifacts · N"**: table Name (download link), Size, sha256 (with "copy"), and a "Download" button per file. Empty: "No artifacts were stored." or "model.pt, config.json, metrics.json and env.json are uploaded when training ends (sha256 verified on receipt)."
15. **"Advanced details"** fold: "(job and deployment state machines, logs, verification checks, cost reconciliation, events, request, routing decision)". Inside:
    - Meta line: job id with copy button, template, state badge, provider, GPU, deployment link, route request link, and a **"Refresh now"** button.
    - Stats: State since, Created, Started, Runtime (with ceiling), Est. max, Actual cost, Resource.
    - Two strips: "Job lifecycle" (every raw state) and "Deployment" (its own state machine).
    - **"Logs"**: line count, a "filter logs" search box, an "auto-scroll" checkbox, a "Copy all" button, the log text (last 2000 lines shown, 5000 kept).
    - "Verification · N / 12 verified" (or "SIMULATED (no real check)"): the 12 checks from instance created to termination verified; hover for evidence.
    - "Termination evidence" (or "SIMULATED: no instance existed, nothing to terminate.").
    - "Cost: four numbers, never merged": Quote, Expected cost, Provider-reported, OpenGrid transaction, plus estimates.
    - "Job events · N", "Request" (template, config, resources, strategy, limits, SSH, bootstrap hash), "Routing decision · at submission" (with the full preview panel when stored).
    - Footer: "Live: refreshed every 4 s while the job is active." or "Finished: polling stopped."

#### States

- Loading: "Loading job…".
- Signed out: "Sign in to do this" heading with "Jobs are private to your OpenGrid account. Sign in to see this one." Buttons "Sign in", "My Jobs", "See the example job".
- Not found: "Job not found" / "This job doesn't exist, or it belongs to another account."
- A failed poll after the first load: amber "Refresh failed … showing the last answer; retrying".

#### Refresh behaviour

Polls the job, metrics, artifacts, deployment and logs every 4 seconds while the job is active, skips polls while the tab is hidden, stops when the job is finished. A cancel restarts polling. "Refresh now" (inside Advanced) reloads at once.

#### Observations

- The only manual refresh button ("Refresh now") is hidden inside the closed "Advanced details" fold.
- The signed-out panel's heading is the generic "Sign in to do this" title from the shared error text, not a job-specific heading (the body text is job-specific).

---

### /route (Preview a route)

**URL:** /route. Parameters: gpu, count, region, max, dur, dl, maxrun, mode, w (custom weights), ex (excluded providers), lvl=1, avail=1, strict=1, variants=1, rr=<route request id> (reopens a ticket). Linked from Find Compute's "Preview a route" buttons. Nav label "Route", locked "Needs an OpenGrid account".

**Who and sign-in:** Signed-in users (in the browser, the operator). Previews and quotes are private. Signed out, the form is visible but the result area explains sign-in.

**Purpose:** See which provider OpenGrid would pick for a GPU request and why (free), and, if wanted, request a quote that an admin then approves (the money path).

#### Layout, top to bottom

1. **Header** "Preview a route", subtitle "See which provider OpenGrid would choose for a GPU request, and why. Previewing is free: it never calls a provider and never spends money." Buttons "How routing works" (/methodology/best-execution) and "Deployments →" (/deployments).
2. **Left: the ticket** "What do you need?"
   - "GPU": type-ahead box (placeholder "h100, mi300x, b200…"), arrow keys and Enter pick; suggestions show provider counts or "family". Picking a family shows its variants with prices and an "only this" button per variant, plus a checkbox "Allow variants (any <family> variant may be selected)" and the warning "Without this, pick one variant: a family alone is not one product."
   - "GPUs per instance": number 1 to 64 with chips "1×", "2×", "4×", "8×".
   - "Region" (Any region plus 8 groups) and "Budget: max $ per GPU-hour" ("Optional. Listings above it are left out.").
   - "Expected runtime (hours)" ("Optional. Only used to estimate the total cost; nothing stops at this time.").
   - "Routing preference": "Cheapest", "Soonest available", "Balanced", "Most stable", with a hint.
   - **"Advanced options"** fold: "Deadline (hours)", "Automatic shutdown (minutes)" ("OpenGrid terminates the GPU after this many minutes, whatever happens."), "Strict region" checkbox, "Custom routing weights" checkbox (sliders per factor; Reliability and Performance are locked at "no data, not used"), "Exclude providers" chips (a dot marks providers OpenGrid can provision on), "Only providers OpenGrid can launch on (integration level 2 or more)", "Only listings that report availability", and a "Launch parameters (route only)" fold (Name, SSH public key, SSH key name, Image, Disk GB).
   - Buttons **"Preview"** (Enter; free) and **"Request a quote…"** (money path, below). Marker "ticket changed: preview again". Footer note about preview vs quote.
3. **Right: results.**
   - Before anything: tips, "Not sure which GPU you need? Find Compute recommends one for your workload →", three example links (for example "8× h100-80gb-sxm5 US ≤ $2.5"), and the command-bar tip "r h100 8 us".
   - **Simple result:** "OpenGrid recommends <provider> for N× GPU · region", "because" with the top three factors, facts "Observed price", "Total per hour", "Expected cost" (needs a runtime), "Availability" (with "price seen"), a cost-assumptions line, "Can OpenGrid launch it?" ("Yes, after approval", "Preview only: live launches are switched off on OpenGrid", or "No: OpenGrid only reads market data for <provider>" with the best provider it can launch on), and "Next best" (up to three alternatives).
   - **"Technical details"** fold: request bar (with a "live provisioning ON/off" badge and the audit record id link), a provisioning warning, the selected card (Observed price, Quote, Expected cost, Market median, Savings vs median, Availability, Freshness, Can OpenGrid provision?, score bar), "Best provisionable", "Why this route", "Factor breakdown · selected", "Alternatives & score contributions" (legend, ranked table, button "All candidates" to load every candidate from the audit record), multi-instance shapes, "Excluded listings" (filter chips by reason plus a table), and the price-kind note.
   - No match: "No GPU matches this request" with advice and "Why nothing qualified".
4. **Ticket area** (after a quote request or ?rr=): "Route ticket" with status badge ("PENDING APPROVAL", "QUOTE EXPIRED" or the deployment state), the deployment and audit links, "Dismiss". Cells: Provider, Region, GPU × count, Quote, Observed, Est. hourly, Est. total, Quote expires (live countdown), Fees, Taxes ("not computed"), Billing unit, Min. commitment. SSH access block and the auto-terminate line ("if approved now"). Limit violations table. Actions:
   - Non-admin: "Waiting for OpenGrid approval. Nothing is launched or charged until an admin approves this exact quote."
   - Admin: **"Approve & launch…"** (or "Approve with override…"; disabled when a hard limit or a missing SSH key blocks it) and **"Reject…"**.
   - Expired: "Re-quote…".

#### Buttons that confirm or spend

- "Request a quote…": previews first if needed, then a dialog titled "Request route: creates a quote for OpenGrid approval" (or "Request route: LIVE mode may launch without approval", red, with a required checkbox "I understand a validated, live-enabled provider can launch a paid instance immediately"). Body table: Request, Estimate, Price cap, Top candidate. Buttons "Cancel" and "Request route". Creates a quote and a pending deployment; nothing launches under supervised mode until approval. "Retry (same Idempotency-Key)" on an unknown outcome.
- "Approve & launch…" (admin, spends money): a form dialog: you must type the provider name; tick "Override the limit violations above" and/or "Allow the provider's account-level SSH keys…" when needed; "Reason" (required for overrides). Confirm "Approve & launch". If the live price moved, a new quote is shown: "Not launched: … Review the NEW quote above and approve again."
- "Reject…" (admin): dialog "Reject this route" with a required "Reason (required, shown to the partner)".
- "Re-quote…": dialog "Re-quote this route?" explaining it cancels this ticket and requests again. Buttons "Cancel" and "Cancel & re-quote".

#### States

- Loading: "Ranking every eligible listing…", "Routing: live check, quote, cost guards…", "Loading route rr-…".
- Signed out: "Previewing a route needs sign-in", "A route preview estimates which provider OpenGrid would select for your request, without spending anything. It's tied to an OpenGrid account, so it needs sign-in." and "Without an account, Find Compute answers the same question publicly…". Buttons "Sign in", "Use Find Compute", "See this GPU's market" (when a GPU is set). A 403 says "This account can't preview routes".
- Unknown GPU: "Unknown GPU" / "… does not match a canonical GPU. OpenGrid never guesses a mapping; pick one from the list." Unknown region: "Unknown region" box above the form. A family without a variant: "A family is not one product".

#### Refresh behaviour

No automatic preview refresh. A ticket a non-admin is waiting on is re-checked every 10 seconds and redrawn when its state changes. The quote countdown redraws the ticket when it expires.

#### Observations

- The money button has three names. The ticket button says "Request a quote…", but its dialog is titled "Request route: …" with a "Request route" confirm button, and the page text says "Request route skips non-provisionable candidates." and "Used only by Request route".
- The routing preference names and order differ from /jobs/new (see there).
- The header button "Deployments →" sends signed-out visitors to another page that needs sign-in.
- Signed out, the whole ticket (including "Request a quote…") stays usable and only fails after you press it.

---

### /keys (API keys)

**URL:** /keys. Parameters: preset=read|assistant|custom, pane=limits|credentials|billing, h=24|168|720, and anchors #usage, #limits, #credentials, #billing. Nav label "API keys", locked "Needs an OpenGrid account".

**Who and sign-in:** Signed in (in the browser, the operator). Signed out: one explanation screen and no private calls.

**Purpose:** Create and revoke API keys for tools (CLI, MCP, scripts), and see usage, limits, provider credentials and billing previews.

#### Signed out

Header "API keys", subtitle "Keys let your tools use OpenGrid as your account", button "Developer guide". A notice: "API keys are issued by the OpenGrid team. There is no self-service sign-up yet. You can start without one: public market data, project analysis and GPU recommendations work with no key. To ask for a key, open a request on OpenGrid's GitHub (it is public: don't include secrets)." Buttons "Request API access ↗" (GitHub, new tab), "Start without a key", "Operator sign in". Two panels: "Works without a key" (public data over REST, CLI and local MCP; analyzing a project; GPU recommendations; the Assistant) with "Quickstart →", and "Already have a key?" with curl commands for /v1/me and /v1/keys and a link "Using your key with the CLI, MCP and curl →".

#### Signed in: layout, top to bottom

1. Header "API keys", subtitle "Keys for the operator account. A key holds only the scopes you pick; the secret is shown once." Buttons "Issue a key to a developer →" (operator only; /admin/partners) and "API reference".
2. A small line: "Signed in as the operator (site login): every scope · account #N · N active key(s)".
3. **Secret reveal** (after creating a key): badge "shown once", "Key "<name>" created", the secret with a "Copy key" button, "Copy it now: OpenGrid cannot show it again. Anyone holding it acts as this account with <scopes>.", shell and curl snippets, "Connect an assistant with it →". Button "Done" opens a dialog "Hide the key?" ("Once hidden it cannot be shown again…"), buttons "Cancel" and "I've stored it".
4. **"Create a key"**: three radio presets "Read-only" (data:read), "Assistant with previews and jobs" (data:read, route:preview, deployments:read), "Custom". "Name" (placeholder "e.g. claude-code-laptop"), "Expires" (90 days, 30 days, 1 year, never), "Read limit / min" (placeholder "default"), button **"Create key"**. Fold "Scopes · the exact permissions this key gets": checkboxes per scope with "spends money" or "sensitive" badges; a separate group "Operator-level: not for assistants or scripts" (route:execute, account:manage, admin). Ticking a box switches the preset to Custom. With any elevated scope, a red dialog "Create a key with …?" explains each, requires ticking "I will store this key like a password", buttons "Cancel" and "Create key". No scope picked: "pick at least one scope".
5. **"Your keys"** table: Name, Key (prefix…), Scopes (badges), State, Created, Last used, Expires, Read/min, and a "Revoke" button for active keys. Revoke opens a red dialog "Revoke "<name>" (<prefix>…)?" ("Every request using this key fails with 401 from now on, and keys it created are revoked too. This cannot be undone…"), buttons "Cancel" and "Revoke key". CSV opengrid-keys.csv. Empty: "No keys yet. Create one above; the secret is shown once."
6. **"Account details"** ("Usage, limits, provider credentials and billing. Not needed to create your first key.") with tabs:
   - "API usage": a "24H / 7D / 30D" switch, totals (Requests, OK, Rate limited, Errors, Avg latency), tables "By key", "Top paths", "Latest requests". Only API-key requests are counted, not the website. Empty: "No API-key requests in this window."
   - "Rate limits & authentication": per-class limits table (read, write, execute), how 429 and Retry-After work, the key format.
   - "Provider credentials": explanation of OpenGrid-managed vs bring-your-own, managed providers configured, a warning that website routes use managed credentials, a table of stored credentials with "Delete" (confirm "Delete your <provider> credential (…)?"), and an "Add a credential" form (Provider dropdown, secret, label, button "Store encrypted"; replacing an existing one asks "Replace your <provider> credential?").
   - "Billing preview": "preview" badge, "No payments are processed.", the fee policy, credits, metered usage and draft invoices.

#### States

Loading: spinner, then "Loading keys…". Errors: the standard error box with "Try again".

#### Refresh behaviour

None automatic. The keys table reloads after create and revoke; each tab loads when opened.

#### Observations

- None found in this page's code. Signed-out handling is deliberate and makes no private calls.

---

### /watchlists (Watchlists & alerts)

**URL:** /watchlists. Parameters: wl=<id> (selected list), add=<gpu slug> (pre-fills the add-item box; the command "w <gpu>" opens it). Nav label "Watchlists", locked "Needs an OpenGrid account".

**Who and sign-in:** Signed in (in the browser, the operator). There is no signed-out screen.

**Purpose:** Follow GPUs, providers, regions and indices with live observed values, and set price or availability alerts.

#### Layout, top to bottom

1. Header "Watchlists & alerts", subtitle "Follow GPUs, providers, regions and indices; alert on price and availability · observed data, unknown never fires". Buttons "Alerts method" and "API".
2. **Left rail "Watchlists"** (count): one button per list (name and item count; click to select), "None yet." when empty, and a "New watchlist" box with a **"Create"** button.
3. **Main panel** for the selected list: name, "N items · created <date>", an "observed" badge, buttons "Rename" (dialog with a text box, buttons "Cancel" and "Rename") and "Delete" (red dialog "Delete "<name>"?" / "Removes the watchlist and its N items. Alert rules are separate and are kept."). Table: Item (kind badge plus link), "Low $/GPU·h" (index level for indices), "Median", "24h" (change or n/a with a reason), "Availability", "Detail", and per row: "Alert…" (GPU and GPU+provider items: pre-fills the rule form), "Route" (GPU items: /route?gpu=…), "✕" (remove item, no confirmation). Below: an add form "Add" with a kind dropdown (GPU, Provider, GPU + provider, Region, Index), the matching inputs, and **"Add item"**. Empty list: "Empty watchlist: add an item below." No list: "No watchlist yet." / "Create one on the left, then add …" (and "Create a watchlist to add <gpu>." when ?add= is set).
4. **"Alert rules"** section:
   - Secret box after creating a rule with a webhook: "shown once", "Webhook signing secret for rule #N", how to verify the signature, the secret with a copy button, "Done".
   - Rule form: "Templates" chips "H100 below $2/hr", "B200 becomes available", "Lambda H100 drops 10%", "H100 US availability falls 20%", "OpenGrid H100 Index hits new 30d low". Fields: "Metric" (Lowest price now, Median price now, Provider price now, Listings available, Provider price change %, Region availability change %, Index level, Index new low), then only the fields that metric needs ("GPU", "Provider" or "Provider (optional)", "Region group", "Index", "Window h", "Window"), "Condition" (operator plus value plus unit), "Name", "Cooldown s". "Channels": "in-app feed (always)" (fixed), "webhook" URL, and "email: not implemented yet (recorded as not_implemented, never sent)". Button **"Create rule"**. A note explains edge triggering, cooldown and that unknown readings never fire.
   - Test result strip (after "Test now"): "Test · dry run", condition true/false/unknown, value, "would fire now" or "would not fire: …", "nothing stored or sent", "✕".
   - Rules table: Rule (name and condition text), State (paused, firing, ok, unknown, not evaluated), Last value, Reading, Evaluated, Last fired, Channels, and per row "Test now" (dry run, no state change, no delivery), "Pause" / "Resume", "✕" (red dialog "Delete rule "<name>"?"). Empty: "No alert rules. Start from a template above." Note about the evaluation job running about every 3 minutes.
5. **"Firings · last 7 days"** table: Fired, Rule, Value, Message, Delivery. Empty: "No alert has fired in the last 7 days."

#### States

Loading spinners in the main panel, rules and firings. Errors: the standard error box with "Try again".

#### Refresh behaviour

Every 60 seconds: reloads the lists (with fresh live values) and the rules (and with them the firings).

#### Observations

- No signed-out state. A signed-out visitor sees the full rule form and templates, a "Sign-in required" box in the main panel and another in the rules area, and an empty left rail with no "Create" box (the rail is only drawn after a successful load).
- Signed out, "Firings · last 7 days" spins forever: firings only load after the rules load, so their spinner is never replaced.
- Signed out, the public indices call can finish after the 401 and redraw the main panel as "No watchlist yet. Create one on the left…", hiding the sign-in message, while the left side has nothing to create with.
- Signed out, the page retries every 60 seconds and redraws the same errors.
- Removing an item ("✕") has no confirmation, while deleting a list or a rule does.
- The keys page was built to avoid "a stack of 401 boxes" for signed-out visitors; this page has exactly that.

---

# Part 4: Execution and operator pages

Source read: web/pages/reserve.js, deployments.js, onboarding.js, execution.js, checklist.js, value.js and the shared helpers in web/core.js, as they are in the repo now.

General notes that apply to every page below:

- The server (main.py, api/pages.py) treats the API areas `reservations`, `deployments`, `route`, `admin`, `me` and similar as private. With PUBLIC_PAGES on, the page shell loads for anyone, but every data call on these pages answers 401 unless the visitor is signed in at /login (the operator) or sends an API key. Partners and customers have no browser login; they use API keys from the API, CLI or MCP.
- "Admin" in the UI means `OG.isAdmin(me)`: the operator principal, or a key with the `*` or `admin` scope. `OG.me()` calls `GET /v1/me`; for a signed-out visitor it returns nothing.
- The shared sign-in state (`OG.error` on a 401) shows a lock, the title "Sign-in required", the text "This needs an OpenGrid account. Accounts are invite-only for now; there is no self-serve sign-up yet.", a button "Explore free pages" (to /), a link "OpenGrid staff: operator sign-in" (to /login?next=...), and a folded "Technical details" with the HTTP status and path.
- Confirmation helpers: `OG.dialog` is a modal with "Cancel" and a confirm button; when it has an `ack` text, the confirm button stays disabled until the checkbox is ticked. `OG.ask` is a modal form; its confirm button stays disabled until every required field is filled (with a minimum length) and every "type X to confirm" field matches exactly (case-insensitive). Esc or a click outside closes both.
- Idempotency: money-moving POSTs use `OG.intentFor` + `OG.api.intent`, which sends one `Idempotency-Key` per user intent. The same key is reused only when the previous attempt's outcome is unknown (for example a network error) and the action and body are identical, so the server replays the first answer instead of acting twice.
- Auto refresh (`ctx.every`) pauses while the browser tab is hidden and stops when you leave the page.
- Execution is in PREVIEW_ONLY mode in production and every provider adapter is still marked simulated, so most deployment lists and tables below are empty in practice.

### /reserve (Reserve: price-lock requests)

**URL:** /reserve (optional `?all=1` for the operator's all-accounts view, `?gpu=<slug>` to preselect a GPU). Terminal command `rsv` also opens it.

**Who:** customers with an API key that has `route:preview` (create and list requests), plus the operator. In a browser, effectively the operator only.

**Purpose:** ask OpenGrid for a price-locked block of reserved GPU-hours; see your past requests. Nothing is a quote or a binding price.

**Layout, top to bottom:**

1. Header "Reserve" with subtitle "Price-lock requests for reserved compute · reviewed by OpenGrid · indicative references, never quotes". On the right:
   - Operator only: a two-way switch "Mine" / "All accounts" (the second reads `GET /v1/admin/reservations`, and sets `?all=1`).
   - "Reload": reloads the request list.
   - "Methodology": link to /methodology/reservations.
2. Notice banner: bold "NOT A FINANCIAL CONTRACT", then "Price-lock requests are reviewed by OpenGrid. An offer is made only when provider capacity backs it. This is a commercial order for compute delivered by OpenGrid, not a financial contract, not tradable, no cash settlement." and link "How price-lock requests work →".
3. Two columns:
   - Left: form titled "New price-lock request · free · reviewed by OpenGrid". Fields, each with an inline error slot:
     - "GPU": dropdown, first shows "Loading GPUs…", then "Choose a GPU…" plus every GPU as "<short name> · N providers". H100 80GB SXM is preselected by default (or the `?gpu=` value). Hint: "One GPU variant (families and auto are not accepted for a price lock)". If the GPU list fails: "GPU list unavailable".
     - "GPUs" (number, default 8, hint "used at the same time") and "Total GPU-hours" (number, default 5376).
     - "Start after" (date and time, default one week from now, hint "your local time") and "Start before" (default two weeks from now, hint "start window end").
     - "Region": dropdown "Any region", US, Canada, Europe, UK, APAC, Middle East, LATAM, Africa. "Your max $/GPU·h" (default 2.5, hint "your limit, not a price").
     - A live line: "At your limit: up to $X over N GPU·h (your ceiling, not a price)".
     - "Notes": free text, placeholder "Optional: workload, interconnect, contract length, contact…", max 2000 characters.
     - Button "Submit request…" with the note "Nothing is reserved, launched or billed by submitting."
   - Right: the indicative reference box. Before a GPU is chosen: "Pick a GPU to see the indicative reference: the OpenGrid index level, the best on-demand listings now and the latest settlement fixing." Once a GPU is set it loads `GET /v1/reservations/reference` (about 0.35 s after GPU, count, region or max price changes) and shows a section "Indicative reference now" with an "indicative" badge and stat tiles: "Index level" (links to /indices/<id>), "Raw level", "Best on-demand", "Market median", "Settlement fixing", and "Your limit vs index" (percent). Below, a table of best listings: Provider, GPUs / instance, Region, $/GPU·h, Available, Observed. A note with "As of ...", "Fixing evidence →" (when available) and "Index methodology →".
4. Stat tiles about your requests: "Requests", "Open" (awaiting a decision), "Offers to review" (valid offers), "Accepted" (order form pending), "Fulfilled", "Closed" (declined · expired · withdrawn · cancelled).
5. Section "Your requests" (or "All requests"): sortable table (newest first) with columns Request, Status, GPU, GPU·h, Region, Start window, Your limit, Index at request, Offer, Account (all-accounts view only), Created. Each row opens /reserve/<id>. The table offers CSV export (opengrid-price-lock-requests.csv). Note under it: "Index at request = the OpenGrid index level captured when the request was made (indicative). An offer binds only in a signed order form."

**Submit flow (no money moves):**
- Client-side validation first. Errors appear next to fields ("pick one GPU variant", "a whole number of GPUs", "1 to 1024", "at least one hour on each GPU", "at most 2 decimals", "your limit is required", "> $0 and ≤ $1000", "in the past", "≥ 1 hour after start after", "within 2 years") and a banner "Fix the highlighted fields."
- Then a confirmation dialog "Submit price-lock request" summarising GPU, GPU-hours, start window, region and "Your limit" with the maximum total, plus the banner text. Buttons "Cancel" / "Submit request". No checkbox.
- `POST /v1/reservations/requests` with an Idempotency-Key. On success it goes to /reserve/<new id>. If the outcome is unknown it shows "Outcome unknown." and a button "Retry (same Idempotency-Key)" (which reopens the same dialog). A server field error is also written next to that field. A 403 adds "Requests need the route:preview scope; accepting an offer needs route:execute." and a link "API keys →".

**States:**
- Loading: "Loading price-lock requests…", "Loading the indicative reference…", "Submitting…".
- Empty: "No price-lock requests yet." with "Use the form above: say which GPU, how many GPU-hours, when it should start and your price limit. OpenGrid answers with an offer only when provider capacity backs it."
- Endpoint missing (404/405): "Not available yet" with "<endpoint> is not available on this server yet."
- Other errors: a compact alarm box with a bold code or "HTTP <status>", the message and the endpoint path.
- Signed out: see Observations; this page does not use the shared sign-in state.

**Refresh:** list reloads every 30 s, plus "Reload".

**Observations:**
- Signed-out visitors do not get the shared plain-language sign-in state. The page uses its own `apiErr`/`notYet` instead of `OG.error`, so a 401 shows a box titled "HTTP 401" with the raw server text ("Authentication required: sign in at /login or use an API key") and the endpoint path, both in the list section and in the reference box (the GPU auto-selects, so the reference call runs straight away). There is no access note before the form either, so a visitor can fill in the whole form and confirm the dialog before finding out they cannot submit.
- The 403 hint is the only access guidance, and it does not apply to 401.

### /reserve/{id} (one price-lock request)

**URL:** /reserve/<request_id>.

**Who:** the customer who made the request (API key) and the operator. If the customer endpoint answers 404 and the viewer is the operator, the page falls back to `GET /v1/admin/reservations/{id}`.

**Purpose:** follow one request through its lifecycle, read the offer, and accept or withdraw. The operator also reviews, offers, declines, fulfils or cancels here.

**Layout, top to bottom:**

1. Header: "<count>× <GPU>" and the request id; subtitle with GPU-hours, region ("any region" if none) and start window; a status badge. Buttons:
   - "Withdraw…" (or "Decline offer / withdraw…" when an offer is open), shown only when the server lists `withdraw` as an allowed action.
   - "Refresh": reloads the request.
   - "← Reserve": back to the list.
   - A copy button "Copy the request id".
2. The same "NOT A FINANCIAL CONTRACT" banner.
3. A message area (results and errors of actions).
4. Lifecycle strip: requested → under review → offered → accepted → fulfilled, with a closed state (declined, expired, withdrawn, cancelled) inserted where the request stopped.
5. Stat tiles: "Your limit" (with "up to $X"), "GPU-hours" ("N GPUs at once"), "Offer" (price, offer number and total, or "no offer yet"), "Offer valid until" (with time left), "Created" (operator or account id), "Status since".
6. Section "Offer" or "Offer #N":
   - No offer: "No offer yet. OpenGrid makes an offer only when a provider capacity commitment backs it; until then there is no price." (or "No offer was made." once closed).
   - With an offer: badges "open" / "not open" / "accepted" and "binds only in a signed order form"; a warning "Above your limit" if the offer exceeds the customer's limit; on accepted requests "Accepted: OpenGrid will contact you to sign the order form; nothing is billed by this action."; a table with Price, Total, Valid until, Terms, Supply basis, Offered.
   - Button "Accept offer #N…" (only when allowed), with the note "Records your intent only: no launch, no charge, no payment. OpenGrid then sends the order form."
7. Operator only, section "Operator" with an "admin" badge, showing only the buttons the server allows:
   - "Mark under review": form with optional "Note (optional)".
   - "Make offer…" or "Revise offer…": form with "Price $/GPU·h" (prefilled with the customer limit or the previous price, hint "> 0 and ≤ 1000"), "Valid until (ISO, UTC)" (prefilled three days ahead, hint "15 minutes to 30 days ahead"), "Terms" (required, at least 10 characters), "Supply basis (required)" (at least 10 characters, placeholder "Provider X written capacity commitment ref …"), "Note (optional)". Confirm "Make offer" / "Revise offer".
   - "Decline…": required "Reason (shown to the customer)", at least 3 characters.
   - "Mark fulfilled…": required "Order form / delivery reference".
   - "Cancel…": required "Reason".
   - Note: "An offer needs a supply basis: the provider capacity commitment that backs it. Every action is recorded with your reason."
8. Section "Request": GPU, Total GPU-hours, Start window, Region group, Your max price, Notes, and "Closed because" when closed.
9. Section "Indicative reference at request time": the same reference block as on /reserve, captured when the request was made.
10. Section "Events · N": newest first, each with time, "from → to" state, actor badge, reason and evidence.

**Actions and confirmation:**
- Accept: dialog "Accept offer #N" with GPU, Price, Total, Valid until and the accepted text. A required checkbox "I understand this records my intent only: nothing is launched, charged or paid, and the order form governs." enables "Accept offer". `POST /v1/reservations/{id}/accept` with one Idempotency-Key (needs `route:execute`). Success shows "Accepted" and the server message.
- Withdraw: form "Withdraw the request" (or "Decline the offer and withdraw"), optional reason, red "Withdraw" button. `POST /v1/reservations/{id}/withdraw` with one Idempotency-Key.
- If an outcome is unknown: "Outcome unknown: try again. It reuses the same Idempotency-Key."
- Operator actions post to `/v1/admin/reservations/{id}/<action>` without an Idempotency-Key.

**States:** "Loading the request…"; "Sending…" during actions; "No events recorded."; missing endpoint shows "Not available yet"; other errors show the header plus the compact alarm box.

**Refresh:** every 30 s while the request is not terminal, plus "Refresh".

**Observations:**
- Same as /reserve: a signed-out visitor sees an "HTTP 401" alarm with raw server text instead of the shared sign-in state.
- "Status since" shows a bare age (for example "3h") with no "ago", unlike the other age texts in the app.

### /deployments (Deployments list)

**URL:** /deployments (optional `?status=live|uncertain|pending|failed|terminated`; `?id=<id>` redirects to the detail page).

**Who:** customers with a key (their own deployments) and the operator.

**Purpose:** every deployment OpenGrid launched or is about to launch for this principal, with the risky ones first.

**Layout, top to bottom:**

1. Header "Deployments", subtitle "Compute OpenGrid launched or is about to launch · transaction data", a "transaction" badge (links to /methodology/data-kinds), a filter switch "All" / "Live" / "Uncertain" / "Pending approval" / "Failed" / "Terminated" (kept in the URL), "Reload", and a primary link "New route →" (to /route).
2. Red alarm, only when needed: "N deployment(s) in an uncertain or failed-termination state: an instance may exist and bill. Open each one; reconciliation evidence is on the detail page."
3. Stat tiles: "Deployments", "Live" (an instance may exist), "Running", "Uncertain" (needs evidence), "Pending approval", "GPU-hours observed".
4. Table, sorted with uncertain first, then live, then pre-launch, then the rest. Columns: a coloured dot, Deployment, State, Purpose ("validation" badge or "customer"), Provider ("not placed" if none), GPU, GPUs, Quote $/GPU·h, Execution $/GPU·h, Created, Runtime, Auto-terminate (live countdown for live deployments), Failure. Uncertain rows are highlighted. Rows open /deployments/<id>. CSV export (opengrid-deployments.csv). Note: "Quote = what the route quoted; execution price = what the provider reports charging. Never merged. Uptime is OpenGrid-observed." and "State machine →".

**Buttons:** none that spend money on this page.

**States:**
- Loading: "Loading deployments…".
- Empty: "No deployments yet." with "A deployment exists once POST /v1/route quotes a provisionable listing under SUPERVISED or LIVE execution. In PREVIEW_ONLY the route returns not_provisioned and creates none." and buttons "Preview a route →" and "Execution safety". This is what everyone sees today.
- Filter with no match: "No deployments match this filter."
- Error or signed out: the shared `OG.error` state (sign-in state on 401) with "Try again" for retryable errors.

**Refresh:** every 30 s, plus "Reload".

**Observations:**
- The list page has no access note before loading; signed-out visitors briefly see "Loading deployments…" and then the sign-in state, which is the intended pattern.
- None other verified.

### /deployments/{id} (one deployment)

**URL:** /deployments/<deployment_id>.

**Who:** the owning customer (key) and the operator. The operator also sees the admin trace and validation evidence.

**Purpose:** the full record of one deployment: state, costs, timestamps, SSH access, events, and the controls to stop or terminate it.

**Layout, top to bottom:**

1. Header "Deployment <id>"; subtitle with provider, "<count>× <GPU>", region, "validation" badge or "customer", and a link to the route ticket (/route?rr=...). A state badge. Buttons:
   - "Refresh from provider": asks the provider now (read-only, `?refresh=true`); then shows "Refreshed from the provider." or "Not refreshed: <reason>".
   - "Stop…": only when the provider supports stop, the state is running or degraded, and an instance id exists.
   - "Terminate…" (or "Cancel request…" before launch): shown in every non-terminal state.
   - Copy button "Copy the deployment id".
2. Message area for action results.
3. Red billing alarm when applicable: "PAST DEADLINE / POSSIBLY BILLING" (deadline passed while an instance may exist) or "POSSIBLY BILLING" (uncertain state), each with an extra "Terminate…" button.
4. A state note for non-happy states, for example "The provision call timed out. An instance MAY exist and bill. ..." or "The provider definitively created nothing (capacity / validation). Nothing bills."
5. State strip (created → quoted → pending approval → approved → provisioning → running → stopping → stopped → terminating → terminated).
6. Stat tiles: "State since", "Runtime", "Max runtime", "Auto-terminate" (live countdown, "PAST DEADLINE" after), "Accrued (est.)" (price × GPUs × observed runtime), "Last checked", "Reconciled".
7. Two columns:
   - "Cost: four numbers, never merged": Quote, Expected cost, Provider-reported, OpenGrid transaction, each with a badge; missing values say why (for example "not reconciled yet (runs after confirmed termination)"). After reconciliation: Quote error, Effective rate, Billing rounding, Unexpected fees, Runtime billed, Reconciled.
   - "Prices per GPU-hour · credential": Observed market, List, Quote, Execution, Credential (pinned; "BYO" or "OpenGrid-managed", never the secret), Instance, Client name, Approval, and "Limits overridden" if any.
8. "Runtime ceiling · lifecycle timestamps · SSH access": Max runtime, Auto-terminates, Terminate requested, Provider created, Provider running, Provider terminated, Billable start, Billable end ("open: may still be billing"), Billable basis; and an "SSH access:" block listing the customer key and OpenGrid operator access (red where a key is missing or operator access is not NONE).
9. "Limit violations at approval" table, only if any: Limit, Value, Cap, Override ("admin may override" or "never"), Message.
10. Two columns:
    - "Events · N" (time with seconds, from → to, actor, reason, evidence) and "Provision attempts · at most one per deployment" (Provider, Listing, Outcome, Error, HTTP, Latency ms, Started, plus Detail for the operator). Empty: "No provision call yet: it happens once, after approval." or "No provision call was made."
    - "Routing quality" (Winner, Runner-up, Market median, Expected savings, Realized savings, Quote error, or "No routing-quality row for this deployment yet (computed by the quality job)."); "Validation evidence" for validation deployments (operator only, step checklist); "Design-partner feedback".
11. Operator only: a folded "Trace · N steps (admin)" list.
12. Footer note: "Data kinds: quote / execution / transaction are never mixed." and "Execution safety →".

**Actions that can affect spend, and how they are confirmed:**
- "Terminate…" after launch: red dialog "Terminate <id>?" explaining it terminates the instance on the provider and that OpenGrid marks it terminated only when the provider confirms. A required checkbox "I understand the instance and its local data are destroyed at the provider" enables "Terminate". `POST /v1/deployments/{id}/terminate` with one Idempotency-Key.
- "Cancel request…" before launch: dialog "Cancel <id> before launch?" ("Nothing has been launched; this cancels the request (state → rejected)."), button "Cancel request", no checkbox.
- "Stop…": dialog "Stop <id>?" with the warning "A stopped instance may keep billing (storage or full price, per provider). Only terminate is guaranteed to end spend." Button "Stop", no checkbox.
- On an unknown outcome: "Outcome unknown. Retrying reuses the same Idempotency-Key (ogui-…), so the server replays instead of acting twice."
- Feedback form (only after "terminated"; otherwise "The feedback form opens once the deployment is terminated."): four questions each with "Yes" / "No" / "Not sure" ("Would you have chosen this provider yourself?", "Was the price better than what you normally pay?", "Was setup easier?", "Would you route the next workload through OpenGrid?"), "What broke?" and "Notes" text boxes, and "Send feedback" (or "Update feedback"). No money.

**States:** "Loading deployment…"; errors and signed out use the shared `OG.error` state below a header with "← Deployments".

**Refresh:** every 15 s while the deployment is not terminal.

**Observations:**
- Any failed auto-refresh (for example one network blip during the 15 s polling) replaces the whole page with the error state, removing the Terminate button until "Try again" is pressed. This matters most for live or uncertain deployments, which are exactly the ones being polled.
- The page reads `d.prices.execution_price` directly when drawing; if the API ever omits `prices`, the page throws instead of rendering (the cost block guards for this, the header code does not).
- `quote_expired` is not in the terminal list, so such deployments keep polling every 15 s and keep a "Cancel request…" button.

### /onboarding (Onboarding)

**URL:** /onboarding (operator: optional `?account=<id>` to view a partner's progress).

**Who:** design partners in principle, but in a browser only the operator can sign in. Signed-out visitors get a static explanation.

**Purpose:** the steps from an account to a first supervised GPU run, with a profile form and a free route preview.

**Signed out layout:**
1. Header "Onboarding", subtitle "How a team gets from first look to a first supervised GPU run with OpenGrid".
2. Info callout "Onboarding is run with the OpenGrid team today." with "There is no self-service sign-up yet. You can start without one: public market data, project analysis and GPU recommendations work with no key. To ask for a key, open a request on OpenGrid's GitHub (it is public: don't include secrets)." Buttons "Request API access ↗" (GitHub new issue, new tab), "Start without a key" (/developers#quickstart), "Operator sign in" (/login).
3. Four numbered steps: "Explore with no key" ("Developer quickstart →"), "Get an account and a key" ("Request API access ↗"), "Preview routes" ("Route preview API →"), "A first supervised run" ("Not available yet: every provider adapter is still simulated. When it opens, the OpenGrid operator approves each launch.").
No private API call is made.

**Signed in (operator) layout:**
1. Header "Onboarding", subtitle "From an account to a first supervised route" (or "Progress of <company> (account #N)"). On the right: a dropdown "Operator account" / "Partner: <company>" (switches `?account=`), the progress text from the server, and a "complete" badge.
2. Callout: on the operator's own view, "This is the operator account's own checklist. Design partners act with their API key, not this website. To follow a partner, pick them above; to onboard a new one, use Design partners." When viewing a partner: "Previewing a partner. The steps show their progress. Buttons on this page still act as you, the operator: a preview you run here is recorded on the operator account and does not complete their step."
3. Six steps, each marked done (tick and "done"), "next", or numbered, with server evidence:
   1. "Account created".
   2. "API key": link "API keys →" (to /keys); for a partner, text pointing to Design partners.
   3. "Provider credentials" (badge "optional"): explains Managed vs Bring your own; link "Provider credentials →" (/keys#credentials).
   4. "First route preview": shows the request built from the profile (count × GPU · region · max price · hours; default H100 80GB SXM5 × 1). Button "Run preview" (later "Run again") with "free · never calls a provider · records an audit ticket". Result: selected provider, price, "observed" badge, "OpenGrid can provision" or "market data only", market median, percent below or above median, and the route request id (opens the JSON in a new tab). Then "Why this provider won" with each scoring factor and its contribution, the runner-up, and "Full breakdown on Route →". If the profile changed since: "The profile changed since this result; run again to update it." No route: "No route" with the reason.
   5. "First supervised deployment": explanation, warning "Not available yet. Every provider adapter is still simulated, so no real deployment can run today.", button "Open the Route page →".
   6. "Feedback on the deployment": explanation only.
4. Section "Design-partner profile": form with Company (required), Technical contact, Contact email, Preferred GPUs (comma-separated names with suggestions), Regions, Workload type, Provider you use today, What you pay today ($/GPU·h), Max price ($/GPU·h), Expected GPU count, Expected duration (hours), Latency, Storage and Network requirements. Button "Save profile" (or "Create profile"); status text "saving…", "saved", "nothing changed". Note: "What you pay today is partner-reported and never shown as an OpenGrid observation. ..." When viewing a partner, saving PATCHes that partner's profile.

**Money:** nothing here spends money. The preview is a read-only `POST /v1/route/preview`.

**States:** "Loading onboarding…"; "Ranking every eligible listing…"; errors use the shared `OG.error`.

**Refresh:** none automatic. The onboarding status is re-read after a preview and after saving the profile.

**Observations:**
- Profile validation messages are shown through `OG.error({ message })`, which has no HTTP status, so the shared helper labels them "Could not reach OpenGrid. Check your connection, then try again." and hides the real message (for example "unknown GPU: ... (pick a name from the suggestions)") inside "Technical details".
- With `?account=<id>` that matches no partner, the page silently shows the operator's own checklist with no notice.

### /admin/partners (Design partners)

**URL:** /admin/partners (optional `?account=<id>` to open a partner's detail). Defined in onboarding.js.

**Who:** operator only.

**Purpose:** list design partners and their onboarding status; create a partner (account, profile and first API key in one step).

**Layout, top to bottom:**
1. Header "Design partners", subtitle "Accounts onboarding to a first real route", button "New partner…" (shows or hides the create form).
2. Result area after creating a partner: "<name> created (account #N)." and the key reveal: "Copy this key now. OpenGrid stores only a hash and cannot show it again.", the secret, a "Copy key" button, prefix and scopes. Or "No key created."
3. Hidden create form "Create partner: account + profile + first key, in one step": the same profile fields as on /onboarding, plus a checkbox "Grant route:execute on the first key (every launch still needs OpenGrid approval in SUPERVISED)" and button "Create partner". No confirmation dialog.
4. Partners table: Company, Acct, Status, Contact, Onboarding (progress or "complete"), Next step, GPUs, Regions, Normal $/GPU·h (with a "reported" badge), Max $/GPU·h, and a button "Onboarding view" per row. CSV export (opengrid-partners.csv). Clicking a row opens the detail below. Empty: "No design partners yet. Create one: it returns the first API key once."
5. Partner detail: "<company> · account #N" with buttons "Cost guards →" (/admin/execution?acct=N) and "Onboarding view →"; two tables: "Deployments · N" (State, Deployment, Provider, GPU, Quote, Created; empty "No deployments yet.") and "Feedback · N" (Deployment, Own pick?, Price better?, Easier?, Route next?, What broke, Notes; empty "No feedback yet (asked after each terminated deployment).").

**Money:** none directly. Granting `route:execute` gives the partner's key the right to request launches, which still need approval in SUPERVISED mode.

**States:** a spinner while loading; "Loading <company>…" for the detail; errors use `OG.error` with "Try again".

**Refresh:** none automatic.

**Observations:**
- Signed-out visitors (and non-admins) get `OG.error({status: 403})`, which reads "Not included in your access. You are signed in, but your account or API key does not cover this." A signed-out visitor is not signed in, and gets no sign-in link. The same issue exists on /admin/execution, /admin/checklist and /admin/value.
- "company is required" and profile validation errors show as "Could not reach OpenGrid" (same cause as on /onboarding).
- The partner detail loads every deployment (`/v1/admin/deployments?limit=2000`) and filters in the browser.

### /admin/execution (Execution control)

**URL:** /admin/execution (optional `?matrix=1` opens the capability matrix, `?vdep=<id>` follows a validation deployment, `?acct=<id>` opens account limits).

**Who:** operator only.

**Purpose:** the operator's control surface for anything that can launch or bill: uncertain deployments, orphans, kill switch, execution mode, approvals, provider flags, validation, reconciliation, cost guards and the audit log.

**Layout, top to bottom:**

1. Header "Execution control", subtitle "Mode, kill switches, providers, orphans and reconciliation · every change needs a reason and is logged", a mode badge ("mode PREVIEW_ONLY" in production), link "First-route checklist →", button "Reload" (reloads every section).
2. Uncertain deployments: either "No uncertain deployments. Every deployment's state is backed by provider evidence." or a red block "N UNCERTAIN DEPLOYMENT(S)" with a table (State, Deployment, Acct, Provider, GPU, Client name, Instance, Since, Accrued est., Reason) and a "Force-terminate" button per row.
3. Orphans (instances at a provider with no live deployment): "No open orphans" / "every instance the providers list is accounted for", or "N OPEN ORPHAN(S)" with an approximate hourly cost. Table: Status, Provider, Instance, Name, Kind, Deployment, Provider state, $/h, First seen, Seen, Ours? ("provably" / "unproven"), Credential, and Resolve buttons "terminate", "adopt" (disabled without a linked deployment), "ignore". CSV export.
4. Two columns:
   - "Global kill switch": button "STOP ALL LIVE PROVISIONING", or when active "ALL LIVE PROVISIONING IS STOPPED since ... by ...: <reason>" and "Restore by choosing a mode on the right (reason required)." Note: "Sets the mode to DISABLED: no new launch anywhere. Monitoring, status polling, reconciliation, stop and terminate keep working."
   - "Execution mode": the current mode banner, four mode buttons DISABLED, PREVIEW ONLY, SUPERVISED, LIVE, each with a one-line explanation ("capped to PREVIEW ONLY by the env ceiling" on SUPERVISED and LIVE while the env flag is off), and a table "Env ceiling" (ROUTING_LIVE_PROVISIONING true or false) and "Stored" (mode, who, when, reason).
5. "Approval queue · N": deployments pending approval, approved, or quote expired. Columns include Limits ("within limits" or the violated codes) and Requested; button "Review ticket →" opens /route?rr=... (approval happens on the Route ticket, not here). Empty: "Nothing waiting for approval."
6. "Live deployments · N · ≈ $X/h at quote": Runtime, Auto-terminate countdown, Accrued est., "Force-terminate". Empty: "Nothing is running."
7. "Temporary provider resources · leftover og-* SSH keys etc.": summary line, table (Provider, Type, Name, Resource id, Deployment, Status, Age, Last error) with "Cleanup…" for resources that are provably OpenGrid's, otherwise "not ours?". Empty: "No leftover temporary provider resources: every per-deployment key OpenGrid created is deleted."
8. "Provider execution flags": Provider, Lvl, Adapter ("validated" or "simulated"), Validated (with "evidence" link), Credentials, Supervised and Live toggles ("ON" / "off"; disabled until the adapter is validated), Kill switch ("Kill" or a "killed" badge with "Unkill"), Note. Footer explains a customer launch needs a validated adapter and the supervised or live flag.
9. Folded "Capability matrix · what each adapter can really do (evidence on hover)": capability rows by provider, YES / NO / PARTIAL cells, and provider-specific risks. Or "No matrix".
10. Two columns:
    - "Validation launcher": "Validation cap (not overridable): 1 instance · 1 GPU · ≤ $3.00/h total · ≤ 30 min runtime · auto-terminate · admin approval"; a preconditions panel "Validation preconditions · <provider>" with buttons "Send test ops alert" and "Re-check" and a green or red checklist; a provider dropdown, a "GPU slug (optional)" input and "Validate provider…" (disabled with "disabled until every precondition is green"); a table of validation deployments with "Approve on ticket →" or "Open →"; and an evidence panel with "Re-check & mark validated" (disabled while steps are missing).
    - "Reconciliation": last run age and status (or "never run"), button "Run now", and a runs table (#, Started, Trigger, Provider, Status, Findings, Error); clicking a row prints its findings as JSON below. Empty: "No reconciliation run recorded. ..."
11. "Account limits (cost guards)": dropdown "Choose an account…" (partners), "or account id" input and "Load". Once loaded: tiles Active deployments, Active GPUs, Month spend, Monthly limit; inputs Max $/GPU·h, Max $/h, Max total $, Max GPUs, Max active, Monthly $, Provider allowlist, Region allowlist (each with its source badge); "Save changes…" ("blank = settings default ...").
12. "Control audit log": At, Action, Target, Actor, Reason, After; up to 60 rows shown; CSV export. Empty: "No control changes recorded."

**Actions and their confirmations (strongest first):**
- "STOP ALL LIVE PROVISIONING": form requiring "Type STOP to confirm" and a reason (at least 3 characters); button "Stop all launches". `POST /v1/admin/execution/kill`.
- Mode buttons: form "Set execution mode X" with a required reason. LIVE also requires typing "LIVE". SUPERVISED and LIVE show "Check the first-route checklist is green before enabling launches." `POST /v1/admin/execution/mode`. The server still caps at PREVIEW_ONLY while the env flag is off.
- "Force-terminate": requires typing the deployment id and a reason. `POST /v1/admin/deployments/{id}/terminate` with one Idempotency-Key.
- Orphan "terminate": requires typing the instance id and a reason. "adopt" and "ignore": reason only. One Idempotency-Key per orphan and action.
- "Cleanup…": dialog "Delete <type> <name> on <provider>?" with "Delete now", no typed confirmation. One Idempotency-Key.
- Provider "Enable" live launches: requires typing the provider name and a reason. Supervised toggles, "Kill" and "Unkill": reason only.
- "Validate provider…": dialog "Validate <provider>" ("Picks a 1-GPU on-demand listing at or under $3.00/h, quotes it live and creates a validation deployment pending your approval (max 30 min, auto-terminated)." / "Nothing launches until you approve the ticket."), button "Create validation route". One Idempotency-Key.
- "Re-check & mark validated": no dialog, one Idempotency-Key.
- "Save changes…" on limits: form showing the JSON diff and a required reason, button "Save limits".
- "Run now": no dialog; the loading text warns it "may terminate provably-ours orphans".
- "Send test ops alert": no dialog; sends one alert to the ops channel.

**States:** spinners per section; 404 sections show "Not available yet"; other errors use `OG.error`; orphans and reconciliation fall back to the execution overview endpoint and say so in grey.

**Refresh:** deployments, orphans and resources every 20 s; everything else on "Reload" or after an action.

**Observations:**
- Signed-out visitors get the 403 "You are signed in, but..." message with no sign-in link (see /admin/partners).
- The 20 s refresh redraws the whole Validation launcher, so anything typed into "GPU slug (optional)" is cleared every 20 s.
- If no provider has integration level 2 or more, or the flags call fails, the Validation launcher shows a spinner forever instead of a message.
- Clicking reconciliation rows keeps appending JSON blocks; nothing clears them until a reload.
- Errors from actions are appended to their section (flags, kill, mode, live) and pile up; errors from "Force-terminate" in the uncertain table appear in the Live section, not next to the row that was clicked.
- The kill switch button is shown before the mode has loaded, so it briefly reads as not engaged even when the mode is DISABLED.

### /admin/checklist (First real route checklist)

**URL:** /admin/checklist (URL-synced `?provider=`, `?rr=`, `?acct=`).

**Who:** operator only.

**Purpose:** the server-computed gate before enabling SUPERVISED execution: every item green, red or unknown, with evidence and how to fix it.

**Layout, top to bottom:**
1. Header "First real route checklist", subtitle "The gate before SUPERVISED execution · computed live, nothing cached". Controls: provider dropdown (providers with integration level 2 or more; defaults to Vast if present), "route_request_id (rr_…) optional" text box, "acct" number box (check that account's limits), "Recheck", and primary "Probe provider" (one read-only API call with the configured credential).
2. Overall bar: "GREEN: ready for a supervised first route", "RED: do not enable SUPERVISED" or "NOT GREEN: unknown items are not green"; counts "N green · N red · N unknown"; provider, route id, computed time and "probed". Button "Go to execution mode →" when green, otherwise "Execution control" (both go to /admin/execution).
3. Ordered items: coloured dot, label, status badge (not shown for green), "optional" badge, evidence, and "How to fix" with a code snippet for non-green items.
4. Footer: the server's rule text and "First live route guide →".

**Money:** none. "Probe provider" makes one read-only call.

**States:** "Computing the checklist…", "Computing…", "Probing <provider> (read-only)…"; "No provisionable provider on this server." if none; errors use `OG.error` with "Try again".

**Refresh:** none automatic; changing any control or pressing "Recheck" recomputes.

**Observations:**
- Signed-out visitors get the 403 "You are signed in, but..." message.
- If `GET /v1/capabilities` fails, the page says "No provisionable provider on this server." rather than reporting an error.

### /admin/value (Value)

**URL:** /admin/value (optional `?days=7|30|90|3650`, default 30).

**Who:** operator only.

**Purpose:** is OpenGrid creating value: savings vs the market, routing quality, provider reliability, activation funnel and product usage, from real transactions only.

**Layout, top to bottom:**
1. Header "Value", subtitle "Savings, routing quality, reliability, funnel · real transactions only", a "transaction" badge, and a period switch "7D" / "30D" / "90D" / "ALL" (re-renders the page).
2. "Economic value vs the market median": tiles Deployments, With valid comparison, Customer savings, Mean savings, Median savings (each null value shows the server's reason); three small tables by GPU, by Provider, by Strategy (n, Savings $, Mean %, Median %; empty "No valid comparisons yet."); an "Excluded (no valid comparison)" note; and "Per deployment" table (Deployment, Acct, GPU, Provider, Strategy, Market median, Selected with "quote"/"exec" badge, GPU-h, Savings %, Savings $), CSV export. Empty "No deployments in this window."
3. "Routing quality": a key/value table (Route requests, Routing success, Provisioning success, Expected and Realized savings vs median, Savings vs previous provider, Provisioning latency, Quote accuracy, Within quote tolerance, Provider failure rate) with sample size "n=" and "n/a" plus status when thin; then "Per route (newest first)" (At, GPU, Mode, Winner, Runner-up, Median, Expected %, Realized %, Outcome), up to 50 rows, CSV export.
4. "Provider reliability (OpenGrid's own launches)": per provider and launch kind (customer or validation) metrics with n, or "No reliability data" ("No launches recorded yet: reliability needs OpenGrid's own transactions."), plus a note on the minimum sample size.
5. Two columns: "Activation funnel" (weekly table for 8 weeks plus total, and stage-to-stage conversions; over 100% is flagged) and "Product usage" (Visitors, Repeat, top four events, top pages, searches and GPUs viewed; note about anonymous ids and Do Not Track).

**Money:** none. Read-only.

**States:** a spinner per section; each section shows its own `OG.error` on failure.

**Refresh:** none automatic, and there is no Reload button; only the period switch reloads.

**Observations:**
- Signed-out visitors get the 403 "You are signed in, but..." message.
- Section errors are shown without a retry function, so the shared error state has no "Try again" button.
- With no real launches yet, most of this page is empty or "insufficient sample" by design.
