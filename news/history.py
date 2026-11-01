"""News history: recover past items for the news sources from legitimate archives (methodology/news-history.md).

Every recovered item goes through the live path (news/store.py `ingest`) with a `backfill` provenance block, so it
is stored raw first, deduplicated against live items on the same canonical-URL hash, clustered into stories and
classified by the same rules. Live data always wins: an archive copy never rewrites an item already stored.

Methods, per source (all idempotent: a re-run stores nothing new and, with the caches, downloads nothing):

    feed_archive   the feed's own archive pages, where it offers them: WordPress `?paged=N`, detected per source
                   (page 2 must be strictly older than page 1, else the feed ignores the parameter); the Federal
                   Register API by publication-date range; EDGAR's full submissions history (`files` pages).
    wayback_feed   the Internet Archive's captures of the feed URL (CDX API). Captures are walked newest to
                   oldest, each next capture chosen just after the oldest item of the one before, so the feed's
                   whole window is covered with as few downloads as possible; a gap between captures is a gap
                   in the history, reported, never filled.
    wayback_index  captures of blog / newsroom index pages for providers that publish no feed (Crusoe, Nebius,
                   Vast, ...): only items with a machine-readable date (JSON-LD datePublished, a Next.js page's
                   data, <time datetime>) are kept. An undated link is not news history.
    gdelt          the GDELT DOC 2.0 API (free for any use with attribution to the GDELT Project), month by month,
                   for GPU-market queries. The time is GDELT's first-seen time for the article (minutes after
                   publication in practice), stated in the provenance.

Politeness: one archive request at a time, at least 4.5 s apart on the Internet Archive (backfill/wayback.py),
5 s on GDELT (its stated limit), 0.25 s on EDGAR (well under the SEC's 10/s), 2 s on a publisher's own pages;
back-off on 429 / 5xx; every body cached on disk (OPENGRID_NEWS_ARCHIVE_CACHE, default ~/.cache/opengrid/news).

    python -m news.history run [--since 2021-01-01] [--source ID ...] [--method M ...] [--max-captures N]
                               [--max-pages N] [--no-download] [--report report.json]
    python -m news.history reach                          how far back each source reaches now
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit, parse_qsl

import httpx

from news import fetch as fetcher
from news import parse, sources as registry
from news import store

UTC = timezone.utc
DEFAULT_CACHE = Path(os.environ.get("OPENGRID_NEWS_ARCHIVE_CACHE", Path.home() / ".cache" / "opengrid" / "news"))
DEFAULT_SINCE = "2021-01-01"
FALLBACK_STEP = timedelta(days=30)   # next capture when a capture has no usable dates (single-date feeds)
OVERLAP = timedelta(days=1)          # captures overlap by a day so nothing falls between two of them
MAX_PAGES = 400
GDELT_DOC = "https://api.gdeltproject.org/api/v2/doc/doc"
GDELT_ATTRIBUTION = "GDELT Project (https://www.gdeltproject.org/)"
GDELT_QUERIES = registry._GDELT_Q


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- polite cached HTTP

class Http:
    """One request at a time per host class, cached by URL. `min_interval` per host suffix."""

    INTERVALS = {"web.archive.org": 4.5, "api.gdeltproject.org": 5.5, "sec.gov": 0.25}
    DEFAULT_INTERVAL = 2.0

    def __init__(self, cache_dir: Path | str | None = None, client: httpx.Client | None = None,
                 download: bool = True, sleep=time.sleep):
        self.cache = Path(cache_dir or DEFAULT_CACHE)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.client = client or httpx.Client(headers={"User-Agent": fetcher.USER_AGENT, "Accept": fetcher.ACCEPT},
                                             timeout=60, follow_redirects=True)
        self.download = download
        self.sleep = sleep
        self._last: dict[str, float] = {}
        self.requests = 0

    def _interval(self, host: str) -> tuple[str, float]:
        for suffix, v in self.INTERVALS.items():
            if host == suffix or host.endswith("." + suffix):
                return suffix, v
        return host, self.DEFAULT_INTERVAL

    def path(self, url: str) -> Path:
        return self.cache / f"{hashlib.sha256(url.encode()).hexdigest()}.body"

    def get(self, url: str, *, cache: bool = True) -> bytes | None:
        """Body (cached), or None for a 404 / 410 / empty. Raises after repeated 429 / 5xx / transport errors."""
        p = self.path(url)
        if cache and p.exists():
            b = p.read_bytes()
            return b or None
        if not self.download:
            return None
        host = httpx.URL(url).host
        key, interval = self._interval(host)
        headers = {"User-Agent": fetcher.sec_user_agent()} if host in fetcher.SEC_HOSTS else {}
        delay = 10.0
        for _ in range(6):
            wait = self._last.get(key, 0) + interval - time.monotonic()
            if wait > 0:
                self.sleep(wait)
            self._last[key] = time.monotonic()
            self.requests += 1
            try:
                r = self.client.get(url, headers=headers)
            except httpx.TransportError as exc:
                _log(f"history: {type(exc).__name__} on {url}; retry in {delay:.0f}s")
            else:
                if r.status_code in (404, 410):
                    if cache:
                        p.write_bytes(b"")
                    return None
                if r.status_code == 429 and "gdeltproject" in host and "limit requests" in r.text:
                    _log(f"history: GDELT asked to slow down; retry in {delay:.0f}s")
                elif r.status_code not in (429, 500, 502, 503, 504):
                    if not r.is_success:
                        raise RuntimeError(f"HTTP {r.status_code} on {url}")
                    if cache:
                        p.write_bytes(r.content)
                    return r.content or None
                else:
                    _log(f"history: HTTP {r.status_code} on {url}; retry in {delay:.0f}s")
            self.sleep(delay)
            delay = min(delay * 2, 300)
        raise RuntimeError(f"gave up on {url}")


# --------------------------------------------------------------------------- Internet Archive

@dataclass(frozen=True)
class Capture:
    url: str
    timestamp: str

    @property
    def captured_at(self) -> datetime:
        return datetime.strptime(self.timestamp, "%Y%m%d%H%M%S").replace(tzinfo=UTC)

    @property
    def archive_url(self) -> str:
        return f"https://web.archive.org/web/{self.timestamp}/{self.url}"

    @property
    def raw_url(self) -> str:
        return f"https://web.archive.org/web/{self.timestamp}id_/{self.url}"


def cdx_captures(http: Http, url: str, since: str, until: str, refresh: bool = False) -> list[Capture]:
    """Successful captures of `url` (YYYYMMDD bounds), at most one per UTC day, oldest first."""
    q = urlencode({"url": url, "from": since, "to": until, "filter": "statuscode:200", "collapse": "timestamp:8",
                   "output": "json", "fl": "original,timestamp"})
    body = http.get(f"https://web.archive.org/cdx/search/cdx?{q}", cache=not refresh)
    rows = json.loads(body) if body and body.strip() else []
    return [Capture(url=o, timestamp=t) for o, t in rows[1:]]


def pick_next(caps: list[Capture], current: datetime, oldest_item: datetime | None) -> Capture | None:
    """The capture to read after `current`: the newest one at or before (oldest item + OVERLAP), strictly older
    than `current`. Without a usable oldest date, the newest one at least FALLBACK_STEP older."""
    target = (oldest_item + OVERLAP) if oldest_item and oldest_item < current else current - FALLBACK_STEP
    best = None
    for c in caps:
        t = c.captured_at
        if t < current and t <= target:
            best = c  # caps ascend: the last match is the newest
    return best


def _dated(items: list[dict], src: registry.Source) -> list[datetime]:
    """Publish times usable to steer the walk: shared stamps (single-date feeds) excluded, as in store.ingest."""
    stamps = [d["published_at"] for d in items if d.get("published_at")]
    if src.day_dates or src.kind == "federal_register":
        return stamps
    from collections import Counter
    shared = {t for t, n in Counter(stamps).items() if n >= store.SHARED_STAMP_MIN}
    return [t for t in stamps if t not in shared]


@dataclass
class Report:
    source: str
    method: str
    requests: int = 0
    documents: int = 0
    items: int = 0
    new_items: int = 0
    oldest: datetime | None = None
    newest: datetime | None = None
    gaps: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    note: str | None = None

    def saw(self, items: list[dict], res: dict | None):
        self.documents += 1
        self.items += len(items)
        if res:
            self.new_items += res["new_items"]
        for d in items:
            t = d.get("published_at")
            if t:
                self.oldest = t if self.oldest is None or t < self.oldest else self.oldest
                self.newest = t if self.newest is None or t > self.newest else self.newest

    def as_dict(self) -> dict:
        return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in self.__dict__.items()}


def _store(src, items, origin, archive_url, captured_at, dry_run, via=None):
    if dry_run or not items:
        return None
    return store.ingest(src, items, datetime.now(UTC),
                        backfill={"origin": origin, "archive_url": archive_url, "captured_at": captured_at, "via": via})


def wayback_feed(http: Http, src: registry.Source, since: datetime, until: datetime, max_captures: int = 60,
                 dry_run: bool = False, url: str | None = None, origin: str = "wayback_feed") -> Report:
    rep = Report(src.id, origin)
    caps = cdx_captures(http, url or src.url, since.strftime("%Y%m%d"), until.strftime("%Y%m%d"))
    if not caps:
        rep.note = "no Internet Archive captures of this URL in the window"
        return rep
    cur = caps[-1]
    seen = 0
    while cur is not None and seen < max_captures and cur.captured_at >= since:
        seen += 1
        try:
            body = http.get(cur.raw_url)
            items = parse.parse(body, src.kind) if body else []
        except Exception as exc:  # a capture that is not a feed (an error page archived with 200) is skipped
            rep.errors.append(f"{cur.timestamp}: {type(exc).__name__}: {str(exc)[:160]}")
            items = []
        res = _store(src, items, origin, cur.archive_url, cur.captured_at, dry_run)
        rep.saw(items, res)
        dated = _dated(items, src)
        oldest = min(dated) if dated else None
        nxt = pick_next(caps, cur.captured_at, oldest)
        if nxt is not None and oldest is not None and nxt.captured_at < oldest - OVERLAP:
            rep.gaps.append({"from": nxt.captured_at.isoformat(), "to": oldest.isoformat()})
        if oldest is not None and oldest < since:
            break
        cur = nxt
    return rep


# --------------------------------------------------------------------------- the feed's own archive pages

def _with_query(url: str, **params) -> str:
    parts = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in params]
    q += [(k, str(v)) for k, v in params.items()]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q, safe="[]%"), ""))


def feed_pages(http: Http, src: registry.Source, since: datetime, max_pages: int = MAX_PAGES,
               dry_run: bool = False) -> Report:
    """WordPress-style `?paged=N`. Supported only when page 2 is strictly older than page 1."""
    rep = Report(src.id, "feed_archive")
    prev_oldest = None
    for n in range(1, max_pages + 1):
        url = src.url if n == 1 else _with_query(src.url, paged=n)
        try:
            body = http.get(url, cache=n > 1)  # page 1 is the live feed: always fresh
        except RuntimeError as exc:
            rep.errors.append(str(exc)[:200])
            break
        if not body:
            break
        try:
            items = parse.parse(body, src.kind)
        except Exception as exc:
            rep.errors.append(f"page {n}: {type(exc).__name__}")
            break
        dated = _dated(items, src)
        if not dated:
            break
        if n == 2 and prev_oldest is not None and max(dated) >= prev_oldest + OVERLAP:
            rep.note = "the feed ignores ?paged= (page 2 repeats page 1): no own archive"
            break
        if n > 1:
            res = _store(src, items, "feed_archive", url, None, dry_run)
            rep.saw(items, res)
        prev_oldest = min(dated)
        if prev_oldest < since:
            break
    if rep.documents == 0 and rep.note is None:
        rep.note = "no older pages"
    return rep


def federal_register(http: Http, src: registry.Source, since: datetime, max_pages: int = MAX_PAGES,
                     dry_run: bool = False) -> Report:
    rep = Report(src.id, "feed_archive")
    base = _with_query(src.url.replace("per_page=50", "per_page=100"),
                       **{"conditions[publication_date][gte]": since.strftime("%Y-%m-%d")})
    for n in range(1, max_pages + 1):
        url = _with_query(base, page=n)
        body = http.get(url)
        if not body:
            break
        items = parse.parse(body, "federal_register")
        if not items:
            break
        rep.saw(items, _store(src, items, "feed_archive", url, None, dry_run))
        if not json.loads(body).get("next_page_url"):
            break
    return rep


def edgar(http: Http, src: registry.Source, since: datetime, dry_run: bool = False) -> Report:
    """The submissions document (recent filings) and every older-filings page that reaches into the window."""
    rep = Report(src.id, "edgar")
    body = http.get(src.url, cache=False)
    if not body:
        rep.note = "no submissions document"
        return rep
    doc = json.loads(body)
    name = doc.get("name")
    items = parse.parse_edgar(body)
    rep.saw(items, _store(src, items, "edgar", src.url, None, dry_run))
    for f in (doc.get("filings") or {}).get("files") or []:
        if (f.get("filingTo") or "9999") < since.strftime("%Y-%m-%d"):
            continue
        url = f"https://data.sec.gov/submissions/{f['name']}"
        b = http.get(url)
        if b:
            older = parse.parse_edgar(b, name=name)
            rep.saw(older, _store(src, older, "edgar", url, None, dry_run))
    return rep


# --------------------------------------------------------------------------- GDELT

def _months(since: datetime, until: datetime):
    t = since.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while t < until:
        nxt = (t + timedelta(days=32)).replace(day=1)
        yield t, min(nxt, until)
        t = nxt


def gdelt(http: Http, src: registry.Source, since: datetime, until: datetime, max_pages: int = MAX_PAGES,
          dry_run: bool = False) -> Report:
    rep = Report(src.id, "gdelt", note=f"attribution: {GDELT_ATTRIBUTION}")
    query = GDELT_QUERIES[src.id]
    for n, (a, b) in enumerate(reversed(list(_months(max(since, datetime(2017, 1, 1, tzinfo=UTC)), until)))):
        if n >= max_pages:
            break
        url = GDELT_DOC + "?" + urlencode({"query": query, "mode": "artlist", "format": "json", "maxrecords": 250,
                                           "sort": "hybridrel", "startdatetime": a.strftime("%Y%m%d%H%M%S"),
                                           "enddatetime": b.strftime("%Y%m%d%H%M%S")})
        try:
            body = http.get(url)
            items = parse.parse_gdelt(body) if body else []
        except Exception as exc:
            rep.errors.append(f"{a:%Y-%m}: {type(exc).__name__}: {str(exc)[:120]}")
            continue
        rep.saw(items, _store(src, items, "gdelt", url, None, dry_run, via=GDELT_ATTRIBUTION))
    return rep


# --------------------------------------------------------------------------- archived index pages (no feed)

_LD = re.compile(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", re.S | re.I)
_NEXT = re.compile(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S | re.I)
_ARTICLE = re.compile(r"<article\b.*?</article>", re.S | re.I)
_TIME = re.compile(r'<time[^>]+datetime="([^"]+)"', re.I)
_HREF = re.compile(r'<a\b[^>]*href="([^"#]+)"[^>]*>(.*?)</a>', re.S | re.I)
_HEAD = re.compile(r"<h[1-4][^>]*>(.*?)</h[1-4]>", re.S | re.I)
_DATE_KEYS = ("datePublished", "publishedAt", "published_at", "publishDate", "publish_date", "date", "createdAt")
_TITLE_KEYS = ("headline", "title", "name")
_URL_KEYS = ("url", "href", "permalink", "slug")


def _walk(o, out, depth=0):
    if depth > 12:
        return
    if isinstance(o, dict):
        title = next((o[k] for k in _TITLE_KEYS if isinstance(o.get(k), str) and len(o[k]) > 8), None)
        date = next((o[k] for k in _DATE_KEYS if isinstance(o.get(k), str) and parse.parse_time(o[k])), None)
        link = next((o[k] for k in _URL_KEYS if isinstance(o.get(k), str) and o[k]), None)
        if title and date and link:
            out.append((title, link, date))
        for v in o.values():
            _walk(v, out, depth + 1)
    elif isinstance(o, list):
        for v in o:
            _walk(v, out, depth + 1)


def parse_index_page(body: bytes, page_url: str, path_hint: str) -> list[dict]:
    """Dated article links on a blog / newsroom index page. Only machine-readable dates count."""
    text = body.decode("utf-8", "replace")
    found: list[tuple[str, str, str]] = []
    for m in _LD.finditer(text):
        try:
            _walk(json.loads(html.unescape(m.group(1).strip())), found)
        except ValueError:
            continue
    for m in _NEXT.finditer(text):
        try:
            _walk(json.loads(m.group(1)), found)
        except ValueError:
            pass
    for block in _ARTICLE.findall(text):
        t, a = _TIME.search(block), _HREF.search(block)
        if t and a:
            head = _HEAD.search(block)
            found.append((parse.clean_text(head.group(1) if head else a.group(2)) or "", a.group(1), t.group(1)))
    items, seen = [], set()
    for title, link, date in found:
        if not link.startswith("http"):
            link = link if link.startswith("/") else (path_hint.rstrip("/") + "/" + link)
            link = urljoin(page_url, link)
        # Archived pages rewrite links to web.archive.org/web/<ts>/<original>: keep the original.
        m = re.match(r"^https?://web\.archive\.org/web/\d+[a-z_]*/(.+)$", link)
        link = m.group(1) if m else link
        title = parse.clean_text(title)
        if not title or path_hint not in urlsplit(link).path or link in seen:
            continue
        seen.add(link)
        items.append({"guid": link, "url": link, "title": title, "summary": None, "author": None, "categories": [],
                      "published_raw": date, "raw": {"title": title, "url": link, "date": date, "page": page_url}})
    return parse._finish(items)


# Providers that publish no feed: their index pages, as the Archive captured them.
INDEX_PAGES = {
    "crusoe_blog": ("https://www.crusoe.ai/resources/blog", "/blog/"),
    "nebius_blog": ("https://nebius.com/blog", "/blog/"),
    "vast_blog": ("https://vast.ai/article", "/article/"),
    "verda_blog": ("https://verda.com/blog", "/blog/"),
    "fluidstack_news": ("https://www.fluidstack.io/resources", "/"),
    "hyperbolic_blog": ("https://www.hyperbolic.ai/blog", "/blog/"),
    "silicondata_blog": ("https://www.silicondata.com/blog", "/blog/"),
}


def wayback_index(http: Http, src: registry.Source, since: datetime, until: datetime, max_captures: int = 24,
                  dry_run: bool = False) -> Report:
    rep = Report(src.id, "wayback_index")
    page, hint = INDEX_PAGES[src.id]
    caps = cdx_captures(http, page, since.strftime("%Y%m%d"), until.strftime("%Y%m%d"))
    if not caps:
        rep.note = "no Internet Archive captures of the index page"
        return rep
    # Spread the budget evenly over the captures (an index page shows the newest N posts, so even spacing works).
    step = max(1, len(caps) // max(1, max_captures))
    for c in list(reversed(caps))[::step][:max_captures]:
        try:
            body = http.get(c.raw_url)
            items = parse_index_page(body, page, hint) if body else []
        except Exception as exc:
            rep.errors.append(f"{c.timestamp}: {type(exc).__name__}: {str(exc)[:120]}")
            continue
        rep.saw(items, _store(src, items, "wayback_index", c.archive_url, c.captured_at, dry_run))
    if rep.items == 0:
        rep.note = "captures carry no machine-readable dates (client-rendered or undated page): nothing stored"
    return rep


# --------------------------------------------------------------------------- plan + run

def methods_for(src: registry.Source) -> list[str]:
    if src.id in INDEX_PAGES:
        return ["wayback_index"]
    if src.kind == "gdelt":
        return ["gdelt"]
    if src.kind == "edgar":
        return ["edgar"]
    if src.kind == "federal_register":
        return ["feed_archive"]
    if not src.enabled:
        return []
    return ["feed_archive", "wayback_feed"]


def run(since: datetime, until: datetime | None = None, only: list[str] | None = None,
        methods: list[str] | None = None, max_captures: int = 60, max_pages: int = MAX_PAGES,
        download: bool = True, dry_run: bool = False, http: Http | None = None) -> list[dict]:
    until = until or datetime.now(UTC)
    http = http or Http(download=download)
    if not dry_run:
        store.sync_sources()
    out = []
    for src in registry.SOURCES.values():
        if only and src.id not in only:
            continue
        for m in methods_for(src):
            if methods and m not in methods:
                continue
            before = http.requests
            try:
                if m == "wayback_feed":
                    rep = wayback_feed(http, src, since, until, max_captures=max_captures, dry_run=dry_run)
                elif m == "wayback_index":
                    rep = wayback_index(http, src, since, until, max_captures=max_captures, dry_run=dry_run)
                elif m == "edgar":
                    rep = edgar(http, src, since, dry_run=dry_run)
                elif m == "gdelt":
                    rep = gdelt(http, src, since, until, max_pages=max_pages, dry_run=dry_run)
                elif src.kind == "federal_register":
                    rep = federal_register(http, src, since, max_pages=max_pages, dry_run=dry_run)
                else:
                    rep = feed_pages(http, src, since, max_pages=max_pages, dry_run=dry_run)
            except Exception as exc:
                rep = Report(src.id, m, errors=[f"{type(exc).__name__}: {str(exc)[:200]}"])
            rep.requests = http.requests - before
            d = rep.as_dict()
            _log(f"{src.id:28} {m:13} docs={rep.documents:4} items={rep.items:5} new={rep.new_items:5} "
                 f"oldest={d['oldest'] or '-'} {('· ' + rep.note) if rep.note else ''}"
                 f"{(' · errors=' + str(len(rep.errors))) if rep.errors else ''}")
            out.append(d)
    return out


def reach() -> list[dict]:
    """Per source: oldest dated item, newest, items, backfilled items (measured from the database)."""
    from sqlalchemy import text

    import normalize
    with normalize.SessionLocal() as s:
        rows = s.execute(text("""
            SELECT source_id, min(published_at) FILTER (WHERE NOT published_at_inferred) AS oldest,
                   max(published_at) AS newest, count(*) AS items, count(*) FILTER (WHERE backfilled) AS backfilled,
                   min(published_at) FILTER (WHERE NOT published_at_inferred AND NOT backfilled) AS oldest_live
            FROM news_items GROUP BY source_id ORDER BY oldest NULLS LAST""")).all()
    return [dict(r._mapping) for r in rows]


def main(argv=None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m news.history", description="recover past news items from archives")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("--since", default=DEFAULT_SINCE)
    p.add_argument("--until", default=None)
    p.add_argument("--source", action="append", help="only these source ids (repeatable)")
    p.add_argument("--method", action="append", choices=["feed_archive", "wayback_feed", "wayback_index", "edgar", "gdelt"])
    p.add_argument("--max-captures", type=int, default=60, help="Internet Archive captures read per source")
    p.add_argument("--max-pages", type=int, default=MAX_PAGES, help="archive pages / GDELT months per source")
    p.add_argument("--no-download", action="store_true", help="read only what is cached")
    p.add_argument("--dry-run", action="store_true", help="parse and report, store nothing")
    p.add_argument("--report", help="write the JSON report here")
    sub.add_parser("reach")
    a = ap.parse_args(argv)
    if a.cmd == "reach":
        for r in reach():
            print(f"{r['source_id']:28} oldest={r['oldest'] and r['oldest'].date()} live_oldest="
                  f"{r['oldest_live'] and r['oldest_live'].date()} items={r['items']} backfilled={r['backfilled']}")
        return 0
    t0 = datetime.fromisoformat(a.since).replace(tzinfo=UTC)
    t1 = datetime.fromisoformat(a.until).replace(tzinfo=UTC) if a.until else None
    rep = run(t0, t1, only=a.source, methods=a.method, max_captures=a.max_captures, max_pages=a.max_pages,
              download=not a.no_download, dry_run=a.dry_run)
    if a.report:
        Path(a.report).write_text(json.dumps(rep, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
