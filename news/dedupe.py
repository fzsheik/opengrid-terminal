"""Two kinds of duplicate, handled separately.

Exact: the same article reached by different URLs (tracking parameters, fragments,
`www.`, `http`, trailing slash). `canonical_url` normalizes those and `url_hash` is the
unique key of news_items, so an article is stored once however often it is fetched.

Near: the same story syndicated by several outlets, each with its own URL and a lightly
edited headline. `same_story` compares normalized titles (word-bigram Jaccard, and
token-set Jaccard for short reworded titles) within a time window. Matches share a
story_id, so the story is listed once with every source that carried it. Thresholds are
deliberately strict: two different stories about the same company must stay apart;
missing a syndication only shows the story twice.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

STORY_WINDOW = timedelta(hours=72)
BIGRAM_THRESHOLD = 0.6
TOKEN_THRESHOLD = 0.8
MIN_TOKENS = 4  # shorter titles cluster only on an exact normalized match

_TRACKING_PREFIXES = ("utm_", "mc_", "pk_", "hsa_", "_hs", "mkt_", "oly_", "vero_", "wt.", "at_", "itm_")
_TRACKING = {"fbclid", "gclid", "dclid", "msclkid", "yclid", "igshid", "ref", "ref_src", "referrer", "source",
             "cmpid", "cmp", "ncid", "sr_share", "spm", "_ga", "_gl", "guccounter", "guce_referrer",
             "guce_referrer_sig", "trk", "trkcampaign", "s_cid", "mbid", "ito",
             "smid", "__s", "hsenc", "hsctatracking"}
_DEFAULT_PORTS = {"http": "80", "https": "443"}


def canonical_url(url: str | None) -> str | None:
    """Normalize an article URL so tracking variants of one link compare equal.

    Lowercase scheme/host, http -> https, drop `www.`/`m.`/`amp.` host prefixes, default
    ports, fragments, tracking parameters, trailing slashes and `/amp` suffixes; sort the
    remaining query parameters. Path case is kept (servers may treat it as significant).
    """
    if not url:
        return None
    url = url.strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.scheme or not parts.netloc:
        return url
    scheme = "https" if parts.scheme.lower() in ("http", "https") else parts.scheme.lower()
    host = (parts.hostname or "").lower().rstrip(".")
    for prefix in ("www.", "m.", "amp."):
        if host.startswith(prefix) and host.count(".") >= 2:
            host = host[len(prefix):]
    port = parts.port
    netloc = host if port is None or str(port) == _DEFAULT_PORTS.get(parts.scheme.lower()) else f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    path = re.sub(r"/amp/?$", "/", path)
    if len(path) > 1:
        path = path.rstrip("/")
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not (k.lower() in _TRACKING or k.lower().startswith(_TRACKING_PREFIXES))]
    query.sort()
    return urlunsplit((scheme, netloc, path, urlencode(query), ""))


def url_hash(canonical: str) -> str:
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def item_key(source_id: str, item: dict, by: str = "url") -> tuple[str, str]:
    """(canonical url or a stand-in, its hash). Items without a link (or sources keyed by
    guid) key on guid, then title."""
    c = canonical_url(item.get("url")) if by == "url" else None
    if not c:
        c = f"urn:opengrid:{source_id}:{item.get('guid') or item.get('title') or ''}"
    return c, url_hash(c)


def raw_hash(source_id: str, item: dict) -> str:
    """Identity of one version of an item: an edited title or summary is a new raw version."""
    key = "\x1f".join(str(item.get(k) or "") for k in ("guid", "url", "title", "published_raw", "summary"))
    return hashlib.sha256(f"{source_id}\x1f{key}".encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- titles

_STOP = set("""a an the and or of to in on for with by at from as is are be was were its it this that
              into over after amid new says said will can how why what who""".split())
# " - The Register", " | DCD", " – HPCwire": an outlet suffix is not part of the headline.
_SUFFIX = re.compile(r"\s+[-|–—:]\s+[^-|–—:]{2,40}$")


def normalize_title(title: str | None) -> str:
    if not title:
        return ""
    t = title.strip()
    t2 = _SUFFIX.sub("", t)
    if len(t2) >= 20:  # keep the suffix if stripping would leave almost nothing
        t = t2
    t = t.lower().replace("’", "'")
    t = re.sub(r"[^a-z0-9$%. ]+", " ", t)
    t = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", t)  # keep decimals like 2.5, drop sentence dots
    # "$14 billion" / "$14bn" / "14b": one spelling, so outlets' house styles do not split a story.
    t = re.sub(r"\$?(\d+(?:\.\d+)?)\s*(?:billion|bn|b)\b", r"$\1bn", t)
    t = re.sub(r"\$?(\d+(?:\.\d+)?)\s*(?:million|mn|m)\b", r"$\1m", t)
    return re.sub(r"\s+", " ", t).strip()


def tokens(norm_title: str) -> list[str]:
    return [w for w in norm_title.split() if w not in _STOP]


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def title_similarity(a: str, b: str) -> dict:
    """{"bigram": j, "token": j} for two normalized titles."""
    ta, tb = tokens(a), tokens(b)
    ba = {(x, y) for x, y in zip(ta, ta[1:])}
    bb = {(x, y) for x, y in zip(tb, tb[1:])}
    return {"bigram": _jaccard(ba, bb), "token": _jaccard(set(ta), set(tb)), "n": min(len(ta), len(tb))}


def same_story(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    s = title_similarity(a, b)
    if s["n"] < MIN_TOKENS:
        return False
    return s["bigram"] >= BIGRAM_THRESHOLD or s["token"] >= TOKEN_THRESHOLD


def find_story(title_key: str, at: datetime, candidates: list[dict]) -> int | None:
    """story_id of the best near-duplicate among `candidates` ({story_id, title_key, at}), or None."""
    best, best_score = None, 0.0
    for c in candidates:
        if abs(c["at"] - at) > STORY_WINDOW or not same_story(title_key, c["title_key"]):
            continue
        s = title_similarity(title_key, c["title_key"])
        score = 2.0 if title_key == c["title_key"] else s["bigram"] + s["token"]
        if score > best_score:
            best, best_score = c["story_id"], score
    return best
