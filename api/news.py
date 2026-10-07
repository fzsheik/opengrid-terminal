"""News and external market signals, and their overlay on observed market moves.

    GET  /v1/news                    story-clustered news; filters gpu, provider, region, topic, source, q,
                                     since, min_relevance; limit / offset
    GET  /v1/news/sources            the source registry with fetch health
    GET  /v1/news/topics             topic catalogue: weight, description, 30-day counts
    GET  /v1/news/families           GPU families the classifier knows (families.py), tracked or not
    GET  /v1/news/{id}               one item: entities, relevance components, the rest of its story
    GET  /v1/timeline                prices + inferred moves + availability + market events + news, one window
    GET  /v1/timeline/around         news / events near one moment, ranked; "not necessarily causal"
    POST /v1/news/refresh            (admin) fetch now
    POST /v1/news/reclassify         (admin) re-run classification over stored items

News items are observed text from public sources; their entities, topics and relevance
are inferred by the stated rules in methodology/news.md. Nothing here says a news item
caused a price move.
"""

import re
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

import news.ingest  # noqa: F401  (registers the news poll job)
from accounts.auth import Principal, require_scope
from api.common import INFERRED, OBSERVED, envelope, resolve_gpu
from news import classify as cls
from news import sources as registry
from news import store, timeline as tl

router = APIRouter()
READ = require_scope("data:read")
ADMIN = require_scope("admin")
METHODOLOGY = "news"

Segment = Literal["on_demand", "spot"]
_REL = re.compile(r"^(\d+(?:\.\d+)?)\s*([hdw])$", re.I)
MAX_WINDOW = timedelta(days=180)


def _span(value: str) -> timedelta:
    m = _REL.match(value.strip())
    if not m:
        raise HTTPException(400, f"bad window {value!r}; use e.g. 24h, 7d, 30d, 2w")
    n, unit = float(m[1]), m[2].lower()
    span = timedelta(hours=n) if unit == "h" else timedelta(days=n * (7 if unit == "w" else 1))
    if span <= timedelta(0) or span > MAX_WINDOW:
        raise HTTPException(400, "window must be between 1h and 180d")
    return span


def _time(value: str | None, name: str) -> datetime | None:
    """ISO 8601 or a relative span back from now ('7d')."""
    if not value:
        return None
    if _REL.match(value.strip()):
        return datetime.now(timezone.utc) - _span(value)
    try:
        t = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(400, f"bad {name} {value!r}; ISO 8601 or e.g. 7d")
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _gpu(value: str | None, allow_family: bool = True) -> str | None:
    """Canonical name / slug -> canonical name; or a family ('h100', 'blackwell') -> family id."""
    if not value:
        return None
    hit = resolve_gpu(value, strict=False)
    if hit:
        return hit
    fam = cls.family_id(value)
    if fam and allow_family:
        return fam
    if fam:
        raise HTTPException(400, f"{value!r} is a GPU family; prices are per canonical variant "
                                 f"({', '.join(cls.FAMILY_VARIANTS.get(fam, [])) or 'none tracked'})")
    raise HTTPException(404, f"unknown GPU or GPU family {value!r}; see /v1/gpus")


def _region(value: str | None) -> str | None:
    if not value:
        return None
    try:
        import regions
        groups = regions.REGION_GROUPS
    except ImportError:
        groups = ("US", "Canada", "Europe", "UK", "APAC", "Middle East", "LATAM", "Africa")
    for g in groups:
        if g.lower() == value.strip().lower():
            return g
    raise HTTPException(400, f"unknown region group {value!r}; one of {', '.join(groups)}")


def _topic(value: str | None) -> str | None:
    if value and value not in cls.TOPIC_IDS:
        raise HTTPException(400, f"unknown topic {value!r}; see /v1/news/topics")
    return value


_CLASSIFICATION = "entities, topics and relevance are inferred by deterministic rules (methodology/news)"


@router.get("/v1/news")
def news_list(gpu: str | None = None, provider: str | None = None, region: str | None = None,
              topic: str | None = None, source: str | None = None, q: str | None = Query(None, max_length=200),
              since: str | None = None, min_relevance: int = Query(1, ge=0, le=100),
              limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
              _: Principal = Depends(READ)):
    if source and registry.get(source) is None:
        raise HTTPException(404, f"unknown source {source!r}; see /v1/news/sources")
    stories, total = store.list_stories(
        limit=limit, offset=offset, gpu=_gpu(gpu), provider=provider.lower() if provider else None,
        region_group=_region(region), topic=_topic(topic), source=source, q=q, t0=_time(since, "since"),
        min_relevance=min_relevance)
    return envelope(stories, kind=OBSERVED, methodology=METHODOLOGY, classification=_CLASSIFICATION,
                    clustered_by="story_id", pagination={"limit": limit, "offset": offset, "total": total})


@router.get("/v1/news/sources")
def news_sources(_: Principal = Depends(READ)):
    rows = store.sources_health()
    return envelope(rows, methodology=METHODOLOGY, enabled=sum(1 for r in rows if r["enabled"]),
                    disabled=sum(1 for r in rows if not r["enabled"]))


@router.get("/v1/news/topics")
def news_topics(_: Principal = Depends(READ)):
    counts = store.topic_counts(30)
    data = [{**t, "items_30d": counts.get(t["id"], 0)} for t in cls.topic_catalog()]
    return envelope(data, kind=INFERRED, methodology=METHODOLOGY)


@router.get("/v1/news/families")
def news_families(_: Principal = Depends(READ)):
    import families

    data = families.classifier_families()
    return envelope(data, kind=INFERRED, methodology="families", count=len(data),
                    note="every GPU family the news classifier recognises; tracked=false means OpenGrid has no "
                         "canonical variant (no market data) for it yet. " + families.NOTE)


@router.get("/v1/news/{item_id}")
def news_item(item_id: int, _: Principal = Depends(READ)):
    d = store.get_item(item_id)
    if d is None:
        raise HTTPException(404, f"no news item {item_id}")
    return envelope(d, kind=OBSERVED, methodology=METHODOLOGY, classification=_CLASSIFICATION)


@router.get("/v1/timeline")
def timeline(gpu: str | None = None, provider: str | None = None, window: str = "7d",
             end: str | None = None, segment: Segment = "on_demand",
             move_pct: float = Query(tl.MOVE_PCT, gt=0, le=100), _: Principal = Depends(READ)):
    if not gpu and not provider:
        raise HTTPException(400, "give gpu and/or provider")
    g = _gpu(gpu, allow_family=False)
    t1 = _time(end, "end") or datetime.now(timezone.utc)
    data = tl.timeline(gpu=g, provider=provider.lower() if provider else None, t0=t1 - _span(window), t1=t1,
                       segment=segment, move_pct=move_pct)
    return envelope(data, methodology=METHODOLOGY, window=window)


@router.get("/v1/timeline/around")
def timeline_around(gpu: str, at: str, window_hours: float = Query(48, gt=0, le=24 * 30),
                    provider: str | None = None, segment: Segment = "on_demand", _: Principal = Depends(READ)):
    g = _gpu(gpu, allow_family=False)
    data = tl.around_move(g, _time(at, "at"), window_hours=window_hours,
                          provider=provider.lower() if provider else None, segment=segment)
    return envelope(data, methodology=METHODOLOGY, note=tl.NOT_CAUSAL)


@router.post("/v1/news/refresh")
def news_refresh(source: str | None = None, _: Principal = Depends(ADMIN)):
    if source and (registry.get(source) is None or not registry.get(source).enabled):
        raise HTTPException(404, f"no enabled source {source!r}")
    result = news.ingest.run(force=True, only=[source] if source else None)
    return envelope(result, methodology=METHODOLOGY)


@router.post("/v1/news/reclassify")
def news_reclassify(_: Principal = Depends(ADMIN)):
    return envelope(store.reclassify(), methodology=METHODOLOGY)
