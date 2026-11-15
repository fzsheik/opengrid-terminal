"""Fetch one Lium feed and store the answer raw, change-only (lium_feed_snapshots).

Every answer is classified into a `state`:
    ok            200 with data rows
    empty         200 with no data row (a CSV with only its header, an empty JSON list)
    unauthorized  401: no key, or a key Lium does not know
    forbidden     403: the account is not flagged as a data partner, or the key lacks the read scope
    not_found     404: a dump not written yet, or a public feed Lium has switched off (/public/v1/requests)
    unavailable   503: Lium has not configured the feed (no hash salt)
    rate_limited  429 (Retry-After kept in `error`)
    invalid       422: a parameter Lium refused
    error         anything else, network failures included

A repeat of the latest answer for the same feed and parameters (same status, same content once volatile fields such
as `generated_at` are left out) is not stored again: that row's last_seen_at and seen_count move. A feed stuck at
403 or 404 is therefore one row, however long it lasts. `transition` says whether the state changed, so callers
log a warning once, not on every poll.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
from sqlalchemy import select

import provider_meta
from lium_data import feeds as F
from store.lium import LiumFeedSnapshot

log = logging.getLogger(__name__)

# feed name -> function(JSON document) -> the document to store. A feed whose answer carries live counters or
# identifiers OpenGrid does not keep (lium_data/public.py registers them) is stored slimmed, like `keep` for an object.
SLIMMERS: dict = {}
# feed name -> function(stored document) -> the part that decides "changed". Counters that tick on every read
# (jobs finished, average uptime) stay in the stored body but do not make a new row by themselves.
STABLE: dict = {}

_STATES = {200: "ok", 401: "unauthorized", 403: "forbidden", 404: "not_found", 422: "invalid", 429: "rate_limited",
           503: "unavailable"}
QUIET_STATES = ("forbidden", "unauthorized", "not_found", "unavailable")   # an expected answer, not a broken feed


@dataclass
class Fetched:
    feed: F.Feed
    url: str
    params: dict
    fetched_at: datetime
    status_code: int | None
    duration_ms: int | None
    body: str | None
    error: str | None = None
    headers: dict = field(default_factory=dict)


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(headers={"user-agent": F.USER_AGENT}, timeout=F.TIMEOUT, follow_redirects=True)


async def fetch(feed: F.Feed, http: httpx.AsyncClient, *, params: dict | None = None, path: dict | None = None,
                api_key: str | None = None) -> Fetched:
    """One GET. The partner key goes in X-API-Key and nowhere else (never stored, never logged)."""
    params = {k: v for k, v in (params or {}).items() if v is not None}
    url = F.url(feed, **(path or {}))
    headers = {"accept": "text/csv" if feed.fmt == "csv" else "application/json"}
    if feed.partner and api_key:
        headers["X-API-Key"] = api_key
    started = time.perf_counter()
    now = datetime.now(timezone.utc)
    shown = {**params, **(path or {})}
    try:
        r = await http.get(url, params=params, headers=headers)
    except httpx.HTTPError as exc:
        return Fetched(feed, url, shown, now, None, int((time.perf_counter() - started) * 1000), None,
                       f"{type(exc).__name__}: {exc}"[:500])
    return Fetched(feed, url, shown, now, r.status_code, int((time.perf_counter() - started) * 1000), r.text,
                   None if r.status_code == 200 else _error_text(r), dict(r.headers))


def _error_text(r: httpx.Response) -> str:
    """Lium's error code and hint (docs.lium.io/developers/error-codes), never the request headers."""
    try:
        e = r.json().get("error") or {}
        text = f"{e.get('code')}: {e.get('message')}" + (f" ({e.get('hint')})" if e.get("hint") else "")
    except (ValueError, AttributeError):
        text = (r.text or "")[:200]
    if r.status_code == 429 and r.headers.get("retry-after"):
        text += f"; retry-after {r.headers['retry-after']}s"
    return f"HTTP {r.status_code} {text}"[:500]


def state_of(f: Fetched, rows: int | None) -> str:
    if f.status_code is None:
        return "error"
    if f.status_code == 200:
        if f.error:                      # a 200 the parser could not read: kept raw, reported
            return "error"
        return "ok" if rows else "empty"
    return _STATES.get(f.status_code, "error")


def stored_body(f: Fetched) -> tuple[str | None, str | None]:
    """(body to store, content hash). JSON: the `keep` keys only when the feed names them; the hash leaves the
    volatile keys out. CSV: verbatim. Error bodies are stored too (Lium's error object), hashed as they are."""
    body = f.body
    if body is None:
        return None, None
    if f.status_code == 200 and f.feed.fmt == "json":
        try:
            doc = json.loads(body)
        except ValueError:
            return body, hashlib.sha256(body.encode()).hexdigest()
        if f.feed.keep and isinstance(doc, dict):
            doc = {k: doc.get(k) for k in f.feed.keep}
            body = json.dumps(doc, sort_keys=True, separators=(",", ":"))
        slim = SLIMMERS.get(f.feed.name)
        if slim is not None:
            try:
                doc = slim(doc)
            except (ValueError, KeyError, TypeError, AttributeError):
                return body, hashlib.sha256(body.encode()).hexdigest()   # unreadable: kept as received, parse reports it
            body = json.dumps(doc, sort_keys=True, separators=(",", ":"))
        stable = {k: v for k, v in doc.items() if k not in f.feed.volatile} if isinstance(doc, dict) else doc
        if slim is not None and f.feed.name in STABLE:
            stable = STABLE[f.feed.name](doc)
        return body, hashlib.sha256(json.dumps(stable, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if f.status_code != 200:
        # error bodies carry a request id and a timestamp: hash only the status and Lium's error code
        try:
            code = (json.loads(body).get("error") or {}).get("code")
        except (ValueError, AttributeError):
            code = None
        return body, hashlib.sha256(f"{f.status_code}:{code}".encode()).hexdigest()
    return body, hashlib.sha256(body.encode()).hexdigest()


def params_key(params: dict) -> str:
    return json.dumps(params, sort_keys=True, separators=(",", ":"), default=str)[:200]


def visibility(feed: F.Feed) -> str:
    """The tier every row of this feed is stored with: the registry decides, enforce() fails closed."""
    return provider_meta.enforce("lium", provider_meta.visibility("lium", feed.path, "signals"))


@dataclass
class Stored:
    snapshot: LiumFeedSnapshot
    new: bool              # a new row (a changed answer); False: the latest row was seen again
    state: str
    previous_state: str | None

    @property
    def transition(self) -> bool:
        return self.state != self.previous_state


def store(s, f: Fetched, rows: int | None = None, rejected: int | None = None) -> Stored:
    """Store `f` change-only in session `s` (flushed, not committed)."""
    body, sha = stored_body(f)
    state = state_of(f, rows)
    key = params_key(f.params)
    last = s.execute(select(LiumFeedSnapshot).where(LiumFeedSnapshot.feed == f.feed.name,
                                                    LiumFeedSnapshot.params_key == key)
                     .order_by(LiumFeedSnapshot.fetched_at.desc(), LiumFeedSnapshot.id.desc()).limit(1)).scalar()
    prev = last.state if last else None
    if last is not None and last.status_code == f.status_code and last.state == state \
            and last.content_sha256 == sha and sha is not None:
        last.last_seen_at = f.fetched_at
        last.seen_count = (last.seen_count or 1) + 1
        s.flush()
        return Stored(last, False, state, prev)
    snap = LiumFeedSnapshot(feed=f.feed.name, url=f.url, params=f.params, params_key=key, fetched_at=f.fetched_at,
                            last_seen_at=f.fetched_at, seen_count=1, status_code=f.status_code,
                            duration_ms=f.duration_ms, ok=state in ("ok", "empty"), state=state, error=f.error,
                            content_sha256=sha, body=body, rows=rows, rejected=rejected,
                            visibility=visibility(f.feed))
    s.add(snap)
    s.flush()
    return Stored(snap, True, state, prev)


def log_outcome(st: Stored, f: Fetched) -> None:
    """One warning when a feed changes into a failing state; a quiet state (403, 404 ...) then stays at INFO."""
    if st.state in ("ok", "empty"):
        if st.transition and st.previous_state not in (None, "ok", "empty"):
            log.info("lium %s: back to %s after %s", f.feed.name, st.state, st.previous_state)
        return
    level = logging.WARNING if st.transition else logging.DEBUG
    if st.state == "not_found" and not f.feed.partner:
        level = logging.INFO if st.transition else logging.DEBUG     # a public feed switched off: expected
    log.log(level, "lium %s: %s (%s)", f.feed.name, st.state, f.error)
