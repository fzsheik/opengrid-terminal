# Visitor shell (branch `ux/visitor-shell`): what changed and what the other sessions need to do

Session 1 owns the shared shell: `web/index.html`, `web/core.js`, `web/app.css`, `web/pages/home.js` and the shell
tests. This note lists what changed for every page, and the follow-ups that belong to Session 2 (Jobs, Route,
execution access) and Session 3 (API keys, Developers, onboarding). Nothing here was changed in those pages.

## What every page gets automatically

- **Type scale.** Every font size in `app.css` is a `--fs-*` token: 11 / 12 / 12.5 (data tables) / 13 / 14 (body) /
  15 / 17 / 21 (page titles) / 24 / 34. That is about one step larger than before. Data tables (`.grid-t`) use
  `--fs-data` (12.5px) so dense market tables keep their columns. `tests/test_web_shell.js` fails on any raw px font
  size.
- **Controls.** `.btn`, `.field`, `select.field` and the search box are 32px (`--ctl-h`); `.btn.sm` and `.chip` are
  26px (`--ctl-h-sm`). On touch screens they are at least 44px (36px for compact ones). Focus is a 2px accent ring.
- **Contrast.** `--muted` and `--dim` were raised (dark), and `--muted`, `--dim`, `--accent`, `--up`, `--down`,
  `--warn` were darkened (light), so all text tokens meet WCAG AA 4.5:1 on `--bg` and `--surface` (tested).
- **Errors.** `OG.error(err, retry)` now shows a title and one plain sentence plus a next step. The HTTP status and
  endpoint are in a folded "Technical details". A 401 renders as an access state: "Sign-in required: accounts are
  invite-only", plus "Explore free pages" and a small "OpenGrid staff: operator sign-in" link. A 404 offers no retry.
- **Access vocabulary.** `OG.ACCESS` and `OG.accessNote(kind)` (`"account"`, `"operator"`, `"simulated"`,
  `"public"`); `OG.signedIn()` after `/v1/me`; `body.og-signed-in` for CSS. The top bar shows "Public access" or
  "Operator" with a popover that explains what a visitor can use. The nav marks account pages with a lock
  (`data-access="account"`).
- **Nav.** Home is a nav item. Under "Explore" (public): GPUs, Find Compute (with "Ask the assistant"), Markets (now
  with Watchlists), Developers. Under "Your workspace" (needs an account): My Jobs with Deployments, Reserve, Route.
  Ops moved into the operator-only group (hidden unless `/v1/me` is an admin). A link whose `href` equals the current
  path is highlighted even when the page's `nav` key points elsewhere (so `/assistant` highlights "Ask the assistant").
- **Search.** The empty search box lists places to go and example queries; every row is clickable (rows with an
  argument fill in the box). New words: `home`, `gpus`, `find`, `markets`, `developers`, `ask <question>`. A
  multi-word query offers "Ask the assistant". Enter never runs a stale suggestion.
- **Status bar.** "Prices updated 3 min ago" (exact local time in the tooltip); after 10 minutes, "Prices may be out
  of date"; no answer, "Can't reach OpenGrid". Passing `"api unreachable"` to `OG.status.live(false, ...)` is mapped
  to that wording, so old call sites keep working.
- **Tape.** Only whole items show; the rest are counted in a "+N more" link to `/gpus`. Refits on resize.
- **Removed:** `web/pages/market.js` (registered no route; nothing called its helpers).

## Session 2 (Jobs, Route, execution access)

1. `/jobs` signed-out state (`jobs.js`, the 401 branch in `load()`): it offers a "Sign in" button. That is the
   operator login, not a customer sign-in. Replace the block with `OG.accessNote("account")`, or let `OG.error(e)`
   render it (it already says invite-only and labels the operator link as staff only).
2. `/jobs/new` for a visitor calls `/v1/jobs/templates` and `/v1/jobs/readiness` and shows two errors. Check
   `OG.me()` first; for a visitor, show `OG.accessNote("account")` above the three choices and disable "Start free
   demo" and "Check price" with that explanation.
3. The homepage demo action links to `/jobs/new?preset=mnist&sandbox=1` (`OG.home.DEMO`), only for a signed-in
   user. `?preset=mnist` is not read by `jobs.js` (harmless today).
4. Cancelling a free simulation still says "Cancel & terminate…" and talks about terminating an instance. For
   `sandbox` jobs use plain "Cancel" and say that nothing is running on a GPU.
5. `/route` is linked from Find Compute results for visitors; its data needs an account. Mark those links with
   `<i class="lock">` or check `OG.signedIn()`.

## Session 3 (API keys, Developers, onboarding)

1. `/keys` for a visitor renders two identical "Sign-in required" boxes (keys list and credentials). Show one
   `OG.accessNote("account")` at the top and skip the private reads when `!OG.signedIn()`.
2. `/keys` subtitle: "this page acts as the operator account". For a visitor, explain that keys come from OpenGrid
   while accounts are invite-only.
3. `/developers` is public and correct. Step 1 ("Create a key") links to `/keys?preset=assistant`, which a visitor
   cannot use. Add one line there: keys are issued by OpenGrid while accounts are invite-only.

## Backend (small, outside the shell)

1. **Unknown URLs.** With `PUBLIC_PAGES` on, an unknown path (for example `/nope`) answers a visitor with a plain-text
   401 "Authentication required". The SPA has a friendly "Page not found" (Home, GPU prices, Find Compute), but the
   server never serves the shell for unknown paths. Suggested fix in `main.py`: for GET requests that accept
   `text/html` and match no route, serve the shell with status 404 (no change to API paths).
2. `/openapi.json` and `/mapping` are private by an earlier decision, so the `/api` endpoint list and the provider
   "Source field map" fail for visitors (they now show the plain-language sign-in state instead of a raw 401).
3. The Markets mini-tables (`overview.js`) truncate the 24h change ("+0.…") at 1440px. This was already so before
   this branch. It needs a column-width change in that page.
