"""Writing and reading news. Sessions come from `normalize.SessionLocal` so tests can
point everything at a scratch database by patching that one name.

Write path (`ingest`), per source fetch, in one transaction:
    1. every parsed item is stored in news_raw_items first (a new version only if its
       guid/url/title/time/summary changed)
    2. an item whose canonical URL is new becomes a news_items row: publish time
       normalized (or first-seen, flagged), classified, clustered into a story
    3. an existing item whose raw version changed is updated and re-classified

Readers: `related` (the cross-agent contract), `list_stories`, `get_item`, `sources_health`.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, delete, exists, func, insert, or_, select, text, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

import normalize
from news import classify as cls
from news import dedupe, sources as registry
from store.news import NewsEntity, NewsFetchLog, NewsItem, NewsRawItem, NewsSource

FUTURE_TOLERANCE = timedelta(hours=24)
SHARED_STAMP_MIN = 5  # items in one feed sharing an exact timestamp: the feed's build time, not publish times


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- sources

def sync_sources() -> None:
    """Mirror the registry into news_sources, keeping each row's fetch state."""
    rows = [{k: v for k, v in registry.as_dict(s).items() if k != "key"} | {"consecutive_failures": 0}
            for s in registry.SOURCES.values()]
    with normalize.SessionLocal.begin() as s:
        for r in rows:
            stmt = pg_insert(NewsSource).values(**r)
            static = {k: stmt.excluded[k] for k in ("name", "url", "kind", "category", "trust_tier", "poll_seconds",
                                                     "topics", "entity_hints", "enabled", "note")}
            s.execute(stmt.on_conflict_do_update(index_elements=["id"], set_=static))


def source_states() -> dict[str, dict]:
    with normalize.SessionLocal() as s:
        return {r.id: {"etag": r.etag, "last_modified": r.last_modified, "last_fetched_at": r.last_fetched_at,
                       "url": r.url} for r in s.scalars(select(NewsSource))}


def log_fetch(source_id: str, res: dict, items: int, new_items: int, error: str | None = None) -> None:
    ok = res["ok"] and error is None
    err = error or res.get("error")
    with normalize.SessionLocal.begin() as s:
        s.add(NewsFetchLog(source_id=source_id, fetched_at=res["fetched_at"], ok=ok, status=res.get("status"),
                           not_modified=bool(res.get("not_modified")), error=err, items=items, new_items=new_items,
                           bytes=res.get("bytes"), duration_ms=res.get("duration_ms")))
        values = {"last_fetched_at": res["fetched_at"]}
        if ok:
            values.update(last_ok_at=res["fetched_at"], last_error=None, consecutive_failures=0)
            if res.get("etag") or res.get("last_modified"):
                values.update(etag=res.get("etag"), last_modified=res.get("last_modified"))
        else:
            values.update(last_error=err, consecutive_failures=NewsSource.consecutive_failures + 1)
        s.execute(update(NewsSource).where(NewsSource.id == source_id).values(**values))


# --------------------------------------------------------------------------- ingest

def _classify(src: registry.Source, title, summary, published_at, first_seen_at) -> dict:
    return cls.classify(title, summary, source_tier=src.trust_tier, source_topics=src.topics,
                        provider_hint=src.entity_hints.get("provider"),
                        published_at=published_at, first_seen_at=first_seen_at)


def _apply(s, item: NewsItem, c: dict, at: datetime) -> None:
    item.relevance = c["relevance"]
    item.relevance_components = c["relevance_components"]
    item.topics = c["topics"]
    item.entities = {**c["entities"], "topic_basis": c["topic_basis"]}
    item.classified_at = at
    s.execute(delete(NewsEntity).where(NewsEntity.item_id == item.id))
    rows = [{"item_id": item.id, "entity_type": t, "entity_value": v[:160]} for t, v in cls.entity_rows(c)]
    if rows:
        s.execute(insert(NewsEntity).values(rows))


def _newest(parsed: list[dict], n: int) -> list[dict]:
    floor = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(parsed, key=lambda d: d.get("published_at") or floor, reverse=True)[:n]


def ingest(src: registry.Source, parsed: list[dict], fetched_at: datetime) -> dict:
    """Store raw items, derive news_items. Returns {"items", "new_items", "updated"}."""
    parsed = _newest(parsed, src.max_items)
    new = updated = 0
    with normalize.SessionLocal.begin() as s:
        prepared = []
        # Some site generators (Webflow-style blogs) stamp every item with the site's last publish
        # time, so 90 posts from 2023 all "published" this week. A timestamp shared by many items in
        # one feed is not a publish time: those items keep the stamp but are flagged inferred.
        stamps = Counter(d.get("published_at") for d in parsed if d.get("published_at"))
        shared = {t for t, n in stamps.items() if n >= SHARED_STAMP_MIN}
        for d in parsed:
            canon, uhash = dedupe.item_key(src.id, d, src.key)
            pub = d.get("published_at")
            if pub is not None and pub in shared:
                prepared.append((d, canon, uhash, pub, True))
                continue
            inferred = pub is None or pub > fetched_at + FUTURE_TOLERANCE
            prepared.append((d, canon, uhash, fetched_at if inferred else pub, inferred))
        if not prepared:
            return {"items": 0, "new_items": 0, "updated": 0}
        existing = {r.url_hash: r for r in s.scalars(
            select(NewsItem).where(NewsItem.url_hash.in_([p[2] for p in prepared])))}
        lo = min(p[3] for p in prepared) - dedupe.STORY_WINDOW
        hi = max(p[3] for p in prepared) + dedupe.STORY_WINDOW
        candidates = [{"story_id": r.story_id, "title_key": r.title_key, "at": r.published_at} for r in s.execute(
            select(NewsItem.story_id, NewsItem.title_key, NewsItem.published_at)
            .where(NewsItem.published_at.between(lo, hi), NewsItem.title_key.is_not(None)))]
        seen_hashes = set()
        for d, canon, uhash, pub, inferred in prepared:
            if uhash in seen_hashes:
                continue  # the same link twice in one feed
            seen_hashes.add(uhash)
            rhash = dedupe.raw_hash(src.id, d)
            parsed_fields = {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in d.items() if k != "raw"}
            raw_id = s.execute(pg_insert(NewsRawItem).values(
                source_id=src.id, fetched_at=fetched_at, raw_hash=rhash, guid=(d.get("guid") or None),
                url=d.get("url"), payload={"parsed": parsed_fields, "raw": d.get("raw")},
            ).on_conflict_do_nothing(constraint="uq_nri_source_hash").returning(NewsRawItem.id)).scalar()
            row = existing.get(uhash)
            if row is not None:
                if raw_id is not None and row.source_id == src.id:
                    # A new version of an article we have: refresh text, keep identity and first_seen.
                    row.title, row.summary, row.author = d.get("title"), d.get("summary"), d.get("author")
                    row.title_key = dedupe.normalize_title(d.get("title")) or None
                    row.raw_item_id = raw_id
                    if not inferred:
                        row.published_at, row.published_at_inferred = pub, False
                    _apply(s, row, _classify(src, row.title, row.summary, row.published_at, row.first_seen_at), fetched_at)
                    updated += 1
                continue
            tkey = dedupe.normalize_title(d.get("title")) or None
            item = NewsItem(source_id=src.id, url=d.get("url"), canonical_url=canon, url_hash=uhash,
                            title=d.get("title"), title_key=tkey, summary=d.get("summary"), author=d.get("author"),
                            published_at=pub, published_at_inferred=inferred, first_seen_at=fetched_at,
                            raw_item_id=raw_id, relevance=0, relevance_components={}, topics=[], entities={})
            s.add(item)
            s.flush()
            story = dedupe.find_story(tkey, pub, candidates) if tkey else None
            item.story_id = story if story is not None else item.id
            if tkey:
                candidates.append({"story_id": item.story_id, "title_key": tkey, "at": pub})
            _apply(s, item, _classify(src, item.title, item.summary, pub, fetched_at), fetched_at)
            new += 1
    return {"items": len(prepared), "new_items": new, "updated": updated}


def reclassify(batch: int = 500) -> dict:
    """Re-run classification over every stored item (after a table change). Dedupe is untouched."""
    done, last_id, at = 0, 0, _now()
    while True:
        with normalize.SessionLocal.begin() as s:
            rows = list(s.scalars(select(NewsItem).where(NewsItem.id > last_id).order_by(NewsItem.id).limit(batch)))
            if not rows:
                break
            for r in rows:
                src = registry.get(r.source_id) or registry.Source(r.source_id, r.source_id, "", "rss", "trade_press", "press")
                _apply(s, r, _classify(src, r.title, r.summary, r.published_at, r.first_seen_at), at)
            last_id = rows[-1].id
            done += len(rows)
    return {"reclassified": done}


# --------------------------------------------------------------------------- readers

def _gpu_terms(gpu: str) -> list[tuple[str, str]]:
    """Entity pairs that make an item related to `gpu` (canonical name or family id)."""
    fid = cls.family_id(gpu) if gpu not in cls.FAMILY_VARIANTS else gpu
    if fid:
        return [("gpu_family", fid)] + [("gpu", v) for v in cls.FAMILY_VARIANTS.get(fid, [])]
    return [("gpu", gpu)] + [("gpu_family", f) for f in cls.families_for_gpu(gpu)]


def _has(pairs: list[tuple[str, str]]):
    return exists().where(NewsEntity.item_id == NewsItem.id,
                          tuple_(NewsEntity.entity_type, NewsEntity.entity_value).in_(pairs))


def _filters(gpu=None, provider=None, region_group=None, topic=None, source=None, q=None,
             t0=None, t1=None, min_relevance=None) -> list:
    w = []
    if gpu:
        w.append(_has(_gpu_terms(gpu)))
    if provider:
        w.append(_has([("provider", provider)]))
    if region_group:
        w.append(_has([("region", region_group)]))
    if topic:
        w.append(_has([("topic", topic)]))
    if source:
        w.append(NewsItem.source_id == source)
    if q:
        like = f"%{q.strip()}%"
        w.append(or_(NewsItem.title.ilike(like), NewsItem.summary.ilike(like)))
    if t0 or t1:
        # A flagged date that is not the first-seen fallback (a feed's shared build stamp) is not a
        # publish time, so the item cannot be placed in a time window. First-seen fallbacks stay.
        w.append(or_(NewsItem.published_at_inferred.is_(False), NewsItem.published_at == NewsItem.first_seen_at))
    if t0:
        w.append(NewsItem.published_at >= t0)
    if t1:
        w.append(NewsItem.published_at <= t1)
    if min_relevance is not None:
        w.append(NewsItem.relevance >= min_relevance)
    return w


def item_dict(r: NewsItem, full: bool = False) -> dict:
    src = registry.get(r.source_id)
    d = {
        "id": r.id, "story_id": r.story_id, "source_id": r.source_id,
        "source_name": src.name if src else r.source_id, "trust_tier": src.trust_tier if src else None,
        "title": r.title, "url": r.url, "summary": r.summary, "author": r.author,
        "published_at": r.published_at, "published_at_inferred": r.published_at_inferred,
        "first_seen_at": r.first_seen_at, "relevance": r.relevance, "topics": r.topics,
        "gpus": (r.entities or {}).get("gpus", []),
        "gpu_families": [f["family"] for f in (r.entities or {}).get("gpu_families", [])],
        "providers": [p["id"] for p in (r.entities or {}).get("providers", [])],
        "regions": [g["group"] for g in (r.entities or {}).get("regions", [])],
        "relevance_components": r.relevance_components,
    }
    if full:
        d.update(canonical_url=r.canonical_url, entities=r.entities, relevance_components=r.relevance_components,
                 raw_item_id=r.raw_item_id, classified_at=r.classified_at)
    return d


def related(gpu=None, provider=None, region_group=None, t0=None, t1=None, limit=50, min_relevance=None) -> list[dict]:
    """News items related to a GPU (canonical name or family), provider and/or region group in [t0, t1].

    Newest first, one row per story (the most relevant item of each, with `source_count`).
    Related means "mentions the same entities in the same window", never "caused".
    """
    w = _filters(gpu=gpu, provider=provider, region_group=region_group, t0=t0, t1=t1, min_relevance=min_relevance)
    with normalize.SessionLocal() as s:
        rows = list(s.scalars(select(NewsItem).where(and_(*w)).order_by(NewsItem.published_at.desc())
                              .limit(max(1, limit) * 5)))
    by_story: dict[int, list] = defaultdict(list)
    for r in rows:
        by_story[r.story_id or r.id].append(r)
    out = []
    for items in by_story.values():
        best = max(items, key=lambda r: (r.relevance, -r.published_at.timestamp()))
        d = item_dict(best)
        d["source_count"] = len({r.source_id for r in items})
        out.append(d)
    out.sort(key=lambda d: d["published_at"], reverse=True)
    return out[:limit]


def list_stories(limit=50, offset=0, **filters) -> tuple[list[dict], int]:
    """Story-clustered listing: one entry per story_id, newest activity first, with every source."""
    w = _filters(**filters)
    sid = func.coalesce(NewsItem.story_id, NewsItem.id)
    grouped = (select(sid.label("story"), func.max(NewsItem.published_at).label("last"),
                      func.count().over().label("total"))
               .where(and_(*w)).group_by(sid).order_by(func.max(NewsItem.published_at).desc())
               .limit(limit).offset(offset))
    with normalize.SessionLocal() as s:
        page = s.execute(grouped).all()
        total = page[0].total if page else (s.execute(select(func.count(func.distinct(sid))).where(and_(*w))).scalar() or 0)
        ids = [p.story for p in page]
        members = list(s.scalars(select(NewsItem).where(func.coalesce(NewsItem.story_id, NewsItem.id).in_(ids)))) if ids else []
    by_story: dict[int, list] = defaultdict(list)
    for m in members:
        by_story[m.story_id or m.id].append(m)
    out = []
    for story in ids:
        items = sorted(by_story.get(story, []), key=lambda r: r.published_at)
        if not items:
            continue
        best = max(items, key=lambda r: (r.relevance, -r.published_at.timestamp()))
        d = item_dict(best)
        d.update(story_id=story, first_published_at=items[0].published_at, last_published_at=items[-1].published_at,
                 topics=sorted({t for r in items for t in (r.topics or [])}),
                 sources=[{"item_id": r.id, "source_id": r.source_id,
                           "source_name": (registry.get(r.source_id).name if registry.get(r.source_id) else r.source_id),
                           "url": r.url, "title": r.title, "published_at": r.published_at} for r in items],
                 source_count=len({r.source_id for r in items}))
        out.append(d)
    return out, int(total)


def get_item(item_id: int) -> dict | None:
    with normalize.SessionLocal() as s:
        r = s.get(NewsItem, item_id)
        if r is None:
            return None
        siblings = list(s.scalars(select(NewsItem).where(NewsItem.story_id == r.story_id, NewsItem.id != r.id)
                                  .order_by(NewsItem.published_at))) if r.story_id else []
        d = item_dict(r, full=True)
    d["story"] = [{"item_id": x.id, "source_id": x.source_id, "title": x.title, "url": x.url,
                   "published_at": x.published_at} for x in siblings]
    return d


def raw_item(raw_id: int) -> dict | None:
    with normalize.SessionLocal() as s:
        r = s.get(NewsRawItem, raw_id)
        return None if r is None else {"id": r.id, "source_id": r.source_id, "fetched_at": r.fetched_at, "payload": r.payload}


def sources_health() -> list[dict]:
    """Registry + table state + last-24h fetch stats per source."""
    since = _now() - timedelta(hours=24)
    with normalize.SessionLocal() as s:
        state = {r.id: r for r in s.scalars(select(NewsSource))}
        stats = {r.source_id: r for r in s.execute(text("""
            SELECT source_id, count(*) AS fetches, count(*) FILTER (WHERE ok) AS ok_fetches,
                   coalesce(sum(new_items), 0) AS new_items, avg(duration_ms) AS avg_ms
            FROM news_fetch_log WHERE fetched_at >= :since GROUP BY source_id"""), {"since": since})}
        counts = dict(s.execute(select(NewsItem.source_id, func.count()).group_by(NewsItem.source_id)).all())
        last_item = dict(s.execute(select(NewsItem.source_id, func.max(NewsItem.published_at)).group_by(NewsItem.source_id)).all())
    out = []
    for src in registry.SOURCES.values():
        st, fx = state.get(src.id), stats.get(src.id)
        out.append({**registry.as_dict(src),
                    "last_fetched_at": st.last_fetched_at if st else None, "last_ok_at": st.last_ok_at if st else None,
                    "last_error": st.last_error if st else None,
                    "consecutive_failures": st.consecutive_failures if st else 0,
                    "conditional_get": bool(st and (st.etag or st.last_modified)),
                    "fetches_24h": fx.fetches if fx else 0, "ok_fetches_24h": fx.ok_fetches if fx else 0,
                    "new_items_24h": int(fx.new_items) if fx else 0,
                    "avg_fetch_ms_24h": round(float(fx.avg_ms)) if fx and fx.avg_ms is not None else None,
                    "items_stored": counts.get(src.id, 0), "newest_item_at": last_item.get(src.id),
                    "health": _health(src, st)})
    return out


def _health(src, st) -> str:
    if not src.enabled:
        return "disabled"
    if st is None or st.last_fetched_at is None:
        return "never_fetched"
    if st.consecutive_failures:
        return "failing" if st.consecutive_failures >= 3 else "degraded"
    return "ok"


def topic_counts(days: int = 30) -> dict[str, int]:
    since = _now() - timedelta(days=days)
    with normalize.SessionLocal() as s:
        rows = s.execute(select(NewsEntity.entity_value, func.count()).join(NewsItem, NewsItem.id == NewsEntity.item_id)
                         .where(NewsEntity.entity_type == "topic", NewsItem.published_at >= since)
                         .group_by(NewsEntity.entity_value)).all()
    return dict(rows)
