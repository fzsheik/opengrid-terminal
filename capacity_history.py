"""In-stock history from the capacity feeds: how much of the time a provider actually had a GPU in stock.

capacity_changes is change-only: a row's state holds from its observed_at until the next row for the same
(provider, sku, region_key). From that, per key and over 7 and 30 days, time is split into
    in stock        the provider said so (in_stock true, or a free count above zero where in_stock is not given)
    out of stock    the provider said not
    unknown         a row exists but carries no stock answer (AWS placement scores, a word OpenGrid does not map)
    no data         before the first row, after a closing "no longer reported" row, or past the stale rule
and keys are combined per (provider, GPU) or per listing: in stock when ANY joined key is in stock, else unknown when
any is unknown, else out of stock when any is out, else no data. "Any" because one in-stock size or region is
enough to launch; unknown beats out so a feed that cannot answer for one key never makes the others look sold out.

    pure      state_of, segments, combine, summarize, listing_keys (tested by hand in tests/test_capacity_history.py)
    reader    book(gpu, tier)   one GPU's keys, segments and current rows (cached 5 minutes, like routing's history())
              book_all(tier)    every GPU: the ops coverage read, and a listing page that spans many GPUs

The stale rule is api/capacity.py's: a row not refreshed within STALE_AFTER_POLLS of its provider's capacity
intervals is stale. A stale current row does not count as in or out of stock now, and its tail past that point is
no data, not a continuation of its last state. A closing row (capacity.EXPIRED) ends the key's data at its "last
seen" time plus the stale window, never later than the closing row itself; it is not "out of stock".
Methodology: methodology/capacity.md ("What reaches routing and listings").
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

import normalize
from cache import ttl_cache
from capacity import EXPIRED
from providers import PROVIDERS

IN, OUT, UNKNOWN = "in_stock", "out_of_stock", "unknown"
STALE_AFTER_POLLS = 3
WINDOWS = {"7d": timedelta(days=7), "30d": timedelta(days=30)}
MIN_KNOWN_HOURS = 24        # the same floor as routing's thin-history rule (MIN_HISTORY_HOURS)
_LAST_SEEN = re.compile(r"last seen ([0-9T:.+\-]+)")
_RANK = {IN: 3, UNKNOWN: 2, OUT: 1}


# --------------------------------------------------------------------------
# Pure
# --------------------------------------------------------------------------

def capacity_interval(provider: str) -> float:
    cls = PROVIDERS.get(provider)
    return cls.polling.capacity_seconds if cls else 900


def stale_window(provider: str) -> timedelta:
    return timedelta(seconds=STALE_AFTER_POLLS * capacity_interval(provider))


def is_stale(provider: str, observed_at: datetime, now: datetime) -> bool:
    return now - observed_at > stale_window(provider)


def state_of(in_stock, available_count=None) -> str:
    """The provider's own answer, as normalize_capacity already derived it: `in_stock` is filled for every shape
    (a tier word the provider uses for "available" is in stock, its "none" / "shortage" word is not, an unmapped
    word is null). A count without in_stock is in stock when above zero. No threshold is invented here."""
    if in_stock is not None:
        return IN if in_stock else OUT
    if available_count is not None:
        return IN if available_count > 0 else OUT
    return UNKNOWN


def is_closing(raw_value: str | None) -> bool:
    return bool(raw_value) and raw_value.startswith(EXPIRED)


def _last_seen(raw_value: str) -> datetime | None:
    m = _LAST_SEEN.search(raw_value or "")
    if not m:
        return None
    try:
        t = datetime.fromisoformat(m.group(1))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def segments(changes: list[dict], *, provider: str, current_observed_at: datetime | None,
             now: datetime) -> list[tuple[datetime, datetime, str]]:
    """One key's change rows (ascending observed_at) -> [(start, end, state)], adjacent equal states merged.

    current_observed_at: the key's capacity_current.observed_at, or None when it has no current row. The last state
    holds to min(now, last sighting + stale window). With no current row and no closing row (a source the provider
    stopped using, capacity.prune), the data ends at the last change: nothing says how long it held.
    """
    stale = stale_window(provider)
    out: list[tuple[datetime, datetime, str]] = []
    for i, c in enumerate(changes):
        if is_closing(c["raw_value"]):
            continue
        start = c["observed_at"]
        nxt = changes[i + 1] if i + 1 < len(changes) else None
        if nxt is None:
            end = min(now, current_observed_at + stale) if current_observed_at is not None else start
        elif is_closing(nxt["raw_value"]):
            seen = _last_seen(nxt["raw_value"])
            end = min(nxt["observed_at"], seen + stale) if seen else nxt["observed_at"]
        else:
            end = nxt["observed_at"]
        end = max(start, min(end, now))
        if end <= start:
            continue
        st = state_of(c["in_stock"], c.get("available_count"))
        if out and out[-1][2] == st and out[-1][1] == start:
            out[-1] = (out[-1][0], end, st)
        else:
            out.append((start, end, st))
    return out


def combine(lists: list[list[tuple[datetime, datetime, str]]]) -> list[tuple[datetime, datetime, str]]:
    """Several keys' segments -> one timeline: in stock if any key is, else unknown if any is, else out."""
    lists = [s for s in lists if s]
    if len(lists) == 1:
        return lists[0]
    cuts = sorted({t for segs in lists for a, b, _ in segs for t in (a, b)})
    out: list[tuple[datetime, datetime, str]] = []
    idx = [0] * len(lists)
    for a, b in zip(cuts, cuts[1:]):
        best = None
        for j, segs in enumerate(lists):
            while idx[j] < len(segs) and segs[idx[j]][1] <= a:
                idx[j] += 1
            if idx[j] < len(segs) and segs[idx[j]][0] <= a:
                st = segs[idx[j]][2]
                if best is None or _RANK[st] > _RANK[best]:
                    best = st
        if best is None:
            continue
        if out and out[-1][2] == best and out[-1][1] == a:
            out[-1] = (out[-1][0], b, best)
        else:
            out.append((a, b, best))
    return out


def summarize(segs: list[tuple[datetime, datetime, str]], now: datetime) -> dict:
    """{"7d": {...}, "30d": {...}}: hours and shares of the window in each state, and in_stock_share, the share of
    the hours with a stock answer (in + out) that were in stock: None below MIN_KNOWN_HOURS of answers."""
    out = {}
    for name, span in WINDOWS.items():
        t0 = now - span
        h = {IN: 0.0, OUT: 0.0, UNKNOWN: 0.0}
        for a, b, st in segs:
            a, b = max(a, t0), min(b, now)
            if b > a:
                h[st] += (b - a).total_seconds() / 3600
        total = span.total_seconds() / 3600
        covered = sum(h.values())
        known = h[IN] + h[OUT]
        out[name] = {
            "window_hours": round(total, 2), "covered_hours": round(covered, 2), "known_hours": round(known, 2),
            "in_stock_hours": round(h[IN], 2), "out_of_stock_hours": round(h[OUT], 2),
            "unknown_hours": round(h[UNKNOWN], 2), "no_data_hours": round(max(0.0, total - covered), 2),
            "share_in_stock": round(h[IN] / total, 4), "share_out_of_stock": round(h[OUT] / total, 4),
            "share_unknown": round(h[UNKNOWN] / total, 4), "share_no_data": round(max(0.0, total - covered) / total, 4),
            "in_stock_share": round(h[IN] / known, 4) if known >= MIN_KNOWN_HOURS else None,
        }
    return out


# --------------------------------------------------------------------------
# Joining capacity rows to price listings
# --------------------------------------------------------------------------
# A capacity row joins a listing of the same provider when its sku is the listing's sku or listing_id (each provider
# picked one on purpose, methodology/capacity.md) and its region fits the listing's. Where a provider's two sides use
# different vocabularies the rule is spelled out below, per provider, from its own normalize / normalize_capacity.
# Several rows joining one listing is normal (one listing priced for many regions): they combine "any in stock".

# Listing region is Shadeform's display text for every region it sells in, capacity regions are codes: they cannot
# be compared, and the listing stands for all its regions, so every region row of the sku joins.
REGION_TEXT = {"latitude", "denvr", "boostrun", "horizon", "phyntec", "paperspace", "crusoe"}
# Capacity rows that never join a listing: AWS's placement score is for spot requests and carries no in/out answer.
NO_LISTING_JOIN = {"aws"}
# No sku join (Massed Compute without its key reports Shadeform type names): (provider, GPU, GPU count) instead.
GPU_GRAIN = {"massedcompute"}


def _skus(row: dict) -> set[str]:
    prov, sku, lid = row["provider"], row.get("sku") or "", row.get("listing_id") or ""
    if prov == "jarvislabs":
        # listing_id <name>:1x:<region>:<workload>:<market>; capacity <name>:1x:<workload> counts on-demand GPUs only
        if row.get("market_type") != "on_demand" or lid.count(":") < 2:
            return set()
        return {f"{sku}:{lid.rsplit(':', 2)[1]}"}
    if prov == "hyperbolic":
        # listing_id <sku>:<region>:<machine>:<link>; capacity <sku>:<machine>:<link>
        tail = lid[len(sku) + 1:] if lid.startswith(sku + ":") else ""
        parts = tail.rsplit(":", 2)
        return {f"{sku}:{parts[1]}:{parts[2]}"} if len(parts) == 3 else set()
    return {sku, lid} - {""}


def _region_ok(provider: str, region_key: str, row: dict) -> bool:
    lr = row.get("region") or ""
    if provider == "lium":            # "" is "no city", not provider-wide
        return region_key == lr
    if provider == "voltagepark" and (row.get("listing_id") or "").startswith("bm:"):
        return region_key == "location:" + row["listing_id"].split(":")[1]   # bm:<location id>:<fabric>
    if region_key == "" or lr == "" or provider in REGION_TEXT:
        return True                   # a provider-wide row, or a listing priced for every location
    if region_key == lr or region_key[:64] == lr:   # listing regions are cut to 64 characters
        return True
    return region_key in {t.strip() for t in re.split(r"[,;]", lr)}   # one listing priced for several regions


def listing_keys(row: dict, keys: dict) -> tuple[list, str | None]:
    """(capacity keys joined to one listing row, grain): "listing" when they join by sku / listing_id and region,
    "gpu" for GPU_GRAIN providers joined by GPU and GPU count, None when nothing joins.

    keys: {(provider, sku, region_key): {"gpu_count": int | None, ...}} for the listing's GPU (Book.keys).
    """
    prov = row["provider"]
    if prov in NO_LISTING_JOIN:
        return [], None
    mine = [k for k in keys if k[0] == prov]
    if not mine:
        return [], None
    skus = _skus(row)
    hit = [k for k in mine if k[1] in skus and _region_ok(prov, k[2], row)]
    if hit:
        return hit, "listing"
    if prov in GPU_GRAIN and not any(k[1] in skus for k in mine):
        same = [k for k in mine if keys[k].get("gpu_count") == row.get("gpu_count")]
        if same:
            return same, "gpu"
    return [], None


# --------------------------------------------------------------------------
# Reader
# --------------------------------------------------------------------------

@dataclass
class Book:
    """One GPU's (or every GPU's) capacity history and current state, for one data tier."""
    now: datetime
    keys: dict = field(default_factory=dict)   # (provider, sku, region_key) -> {"gpu", "gpu_count", "segments", "current"}

    _index: dict = field(default_factory=dict, repr=False)

    def provider_keys(self, provider: str, gpu: str | None) -> dict:
        """{key: meta} of one provider's rows for one GPU."""
        if not self._index:
            for k, v in self.keys.items():
                self._index.setdefault((k[0], v["gpu"]), {})[k] = v
        return self._index.get((provider, gpu), {})

    def for_listing(self, row: dict) -> tuple[list, str | None]:
        """listing_keys() for a compute_listings row (its canonical GPU is `gpu` or `canonical_gpu_name`)."""
        gpu = row.get("gpu") or row.get("canonical_gpu_name")
        return listing_keys(row, self.provider_keys(row["provider"], gpu))

    def history(self, keys: list) -> dict:
        return summarize(combine([self.keys[k]["segments"] for k in keys]), self.now)

    def current(self, keys: list, now: datetime) -> dict:
        """The current stock answer over keys: fresh rows only. {"state", "observed_at", "fresh", "stale"}."""
        fresh, stale = [], 0
        for k in keys:
            cur = self.keys[k]["current"]
            if cur is None:
                continue
            if is_stale(k[0], cur["observed_at"], now):
                stale += 1
            else:
                fresh.append(cur)
        if not fresh:
            return {"state": None, "observed_at": None, "fresh": 0, "stale": stale}
        states = [state_of(c["in_stock"], c["available_count"]) for c in fresh]
        st = max(states, key=_RANK.get)
        return {"state": st, "observed_at": max(c["observed_at"] for c in fresh), "fresh": len(fresh), "stale": stale}


_NO_CURRENT = {"state": None, "observed_at": None, "fresh": 0, "stale": 0}


def listing_stock(bk: Book, row: dict, now: datetime) -> dict:
    """Everything the capacity feed says about one listing row, for routing and the listing reads.

    stock / stock_source / stock_observed_at: the listing's own `available` when it has one (normalize owns it and
    wins), else a fresh joined capacity row's answer, else unknown. history: the joined rows' 7/30-day summary; with
    no join, the provider's rows for this GPU combined (history_grain "provider_gpu"), which routing may use for
    persistence but never for "out of stock now".
    """
    keys, grain = bk.for_listing(row)
    cur = bk.current(keys, now) if keys else _NO_CURRENT
    hist_keys, hist_grain = keys, grain
    if not keys and row["provider"] not in NO_LISTING_JOIN:
        hist_keys = list(bk.provider_keys(row["provider"], row.get("gpu") or row.get("canonical_gpu_name")))
        hist_grain = "provider_gpu" if hist_keys else None
    h = bk.history(hist_keys) if hist_keys else None
    avail = row.get("available")
    if avail is not None:
        stock, source, at = (IN if avail else OUT), "listing", row.get("observed_at")
    elif cur["state"] in (IN, OUT):
        stock, source, at = cur["state"], "capacity feed", cur["observed_at"]
    else:
        stock, source, at = UNKNOWN, None, None
    return {"stock": stock, "stock_source": source, "stock_observed_at": at, "join": grain, "current": cur,
            "history": h, "history_grain": hist_grain, "history_rows": len(hist_keys),
            "in_stock_share_30d": h["30d"]["in_stock_share"] if (h and keys) else None}


def listing_fields(st: dict) -> dict:
    """The stock fields a listing read shows (no counts, no raw words: those stay on /v1/capacity)."""
    at = st["stock_observed_at"]
    return {"stock": st["stock"], "stock_source": st["stock_source"],
            "stock_observed_at": at.isoformat() if at else None,
            "stock_feed": st["join"] is not None, "stock_rows_stale": st["current"]["stale"],
            "in_stock_share_30d": st["in_stock_share_30d"]}


_VIS = {"public":" AND {a}visibility = 'public'", "all": ""}

_CHANGES = """
    WITH k AS (
      SELECT DISTINCT provider, sku, region_key FROM capacity_changes
       WHERE {gpu_c} AND observed_at >= :t0 {vis_}
      UNION
      SELECT provider, sku, region_key FROM capacity_current WHERE {gpu_k} {vis_}
    )
    SELECT * FROM (
      SELECT c.id, c.provider, c.sku, c.region_key, c.canonical_gpu_name, c.observed_at, c.in_stock,
             c.available_count, c.raw_value
      FROM capacity_changes c JOIN k USING (provider, sku, region_key)
      WHERE c.observed_at >= :t0 {vis_c}
      UNION ALL
      SELECT * FROM (
        SELECT DISTINCT ON (c.provider, c.sku, c.region_key) c.id, c.provider, c.sku, c.region_key,
               c.canonical_gpu_name, c.observed_at, c.in_stock, c.available_count, c.raw_value
        FROM capacity_changes c JOIN k USING (provider, sku, region_key)
        WHERE c.observed_at < :t0 {vis_c}
        ORDER BY c.provider, c.sku, c.region_key, c.observed_at DESC, c.id DESC
      ) before
    ) x ORDER BY provider, sku, region_key, observed_at, id
"""

_CURRENT = """
    SELECT provider, sku, region_key, region, canonical_gpu_name, gpu_count, in_stock, tier, available_count,
           raw_value, observed_at
    FROM capacity_current WHERE {gpu_k} {vis_}
"""


def _sql(template: str, gpu: str | None, tier: str) -> str:
    v = _VIS[tier]
    return template.format(gpu_c="canonical_gpu_name = :gpu" if gpu else "canonical_gpu_name IS NOT NULL",
                           gpu_k="canonical_gpu_name = :gpu" if gpu else "canonical_gpu_name IS NOT NULL",
                           vis_=v.format(a=""), vis_c=v.format(a="c."))


def load(gpu: str | None, tier: str = "public", now: datetime | None = None) -> Book:
    """Read one GPU's capacity history (or every GPU's with gpu=None) and build its Book. Not cached."""
    if tier not in _VIS:
        raise ValueError(f"tier must be one of {tuple(_VIS)}")
    now = now or datetime.now(timezone.utc)
    params = {"gpu": gpu, "t0": now - max(WINDOWS.values())}
    book = Book(now=now)
    with normalize.SessionLocal() as s:
        current = {(r.provider, r.sku, r.region_key): dict(r._mapping)
                   for r in s.execute(text(_sql(_CURRENT, gpu, tier)), params)}
        key, rows, gpu_of = None, [], None

        def flush():
            if key is None:
                return
            cur = current.get(key)
            book.keys[key] = {
                "gpu": cur["canonical_gpu_name"] if cur else gpu_of, "gpu_count": cur["gpu_count"] if cur else None,
                "region": cur["region"] if cur else (key[2] or None), "current": cur,
                "segments": segments(rows, provider=key[0], current_observed_at=cur["observed_at"] if cur else None,
                                     now=now)}

        for r in s.execute(text(_sql(_CHANGES, gpu, tier)), params):
            k = (r.provider, r.sku, r.region_key)
            if k != key:
                flush()
                key, rows, gpu_of = k, [], None
            rows.append({"observed_at": r.observed_at, "in_stock": r.in_stock,
                         "available_count": r.available_count, "raw_value": r.raw_value})
            gpu_of = r.canonical_gpu_name or gpu_of
        flush()
    for k, cur in current.items():   # a current row with no change row (history pruned): its state since then
        if k not in book.keys:
            book.keys[k] = {"gpu": cur["canonical_gpu_name"], "gpu_count": cur["gpu_count"], "region": cur["region"],
                            "current": cur, "segments": []}
    return book


@ttl_cache(300)
def book(gpu: str, tier: str = "public") -> Book:
    """One GPU's capacity Book, cached like routing's history(): five minutes."""
    return load(gpu, tier)


@ttl_cache(300, maxsize=4)
def book_all(tier: str = "all") -> Book:
    return load(None, tier)


def _pct(h: dict | None, window: str, key: str = "in_stock_share"):
    return None if h is None else h[window][key]


def coverage(now: datetime | None = None, bk: Book | None = None) -> list[dict]:
    """Per provider: which GPUs have a stock feed, how many live listings join to it, in-stock shares, stale rows,
    last read. Every tier (an admin read). Live listings: priced, canonical, seen within market.stale_after."""
    import market

    now = now or datetime.now(timezone.utc)
    bk = bk or book_all("all")
    with normalize.SessionLocal() as s:
        listings = [dict(r._mapping) for r in s.execute(text(
            "SELECT provider, listing_id, sku, canonical_gpu_name, gpu_count, region, market_type, available, "
            "observed_at FROM compute_listings WHERE canonical_gpu_name IS NOT NULL AND price_per_gpu_hour > 0"))]
        cur = s.execute(text("SELECT provider, observed_at, canonical_gpu_name IS NULL AS unmapped "
                             "FROM capacity_current")).all()
    out: dict[str, dict] = {}

    def prov(p):
        return out.setdefault(p, {"provider": p, "stock_feed": False, "gpus": {}, "listings_live": 0,
                                  "listings_joined": 0, "listings_own_stock": 0, "listings_unknown_stock": 0,
                                  "rows_current": 0, "rows_stale": 0, "rows_unmapped_gpu": 0, "last_read": None})

    for r in cur:
        p = prov(r.provider)
        p["stock_feed"] = True
        p["rows_current"] += 1
        p["rows_stale"] += is_stale(r.provider, r.observed_at, now)
        p["rows_unmapped_gpu"] += bool(r.unmapped)
        p["last_read"] = max(p["last_read"] or r.observed_at, r.observed_at)
    for (provider, gpu), keys in _by_provider_gpu(bk).items():
        p = prov(provider)
        p["stock_feed"] = True
        h = bk.history(list(keys))
        p["gpus"][gpu] = {"gpu": gpu, "rows": len(keys), "listings_live": 0, "listings_joined": 0,
                          "in_stock_share_7d": _pct(h, "7d"), "in_stock_share_30d": _pct(h, "30d"),
                          "share_no_data_30d": _pct(h, "30d", "share_no_data"),
                          "share_unknown_30d": _pct(h, "30d", "share_unknown")}
    for r in listings:
        if now - r["observed_at"] > market.stale_after(r["provider"]):
            continue
        p = prov(r["provider"])
        st = listing_stock(bk, r, now)
        joined = st["join"] is not None
        p["listings_live"] += 1
        p["listings_joined"] += joined
        p["listings_own_stock"] += r["available"] is not None
        p["listings_unknown_stock"] += st["stock"] == UNKNOWN
        g = p["gpus"].get(r["canonical_gpu_name"])
        if g is not None:
            g["listings_live"] += 1
            g["listings_joined"] += joined
    rows = []
    for p in sorted(out.values(), key=lambda x: x["provider"]):
        keys = [k for k in bk.keys if k[0] == p["provider"]]
        h = bk.history(keys) if keys else None
        p["listings_joined_share"] = round(p["listings_joined"] / p["listings_live"], 4) if p["listings_live"] else None
        p["in_stock_share_7d"], p["in_stock_share_30d"] = _pct(h, "7d"), _pct(h, "30d")
        p["gpus"] = sorted(p["gpus"].values(), key=lambda g: g["gpu"])
        p["last_read"] = p["last_read"].isoformat() if p["last_read"] else None
        rows.append(p)
    return rows


def _by_provider_gpu(bk: Book) -> dict:
    out: dict = {}
    for k, v in bk.keys.items():
        out.setdefault((k[0], v["gpu"]), []).append(k)
    return out
