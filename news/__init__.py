"""News and external signals: public feeds that can move compute markets.

Pipeline, same shape as the market pipeline (raw first, derive after):

    sources.py   the hand-maintained registry (official feeds/APIs only, each verified)
    fetch.py     conditional GET of one source; every attempt logged to news_fetch_log
    parse.py     RSS 2.0 / RSS 1.0 / Atom / JSON Feed / Federal Register -> plain dicts
    dedupe.py    URL canonicalization + hashing, near-duplicate titles -> story clusters
    classify.py  deterministic entities (GPU, GPU family, provider, region), topics, relevance
    store.py     write raw items, derive news_items, readers (`related`, `list_stories`)
    ingest.py    the poll job: due sources, concurrency cap, per-source timeouts
    timeline.py  news overlaid on observed price/availability moves, never as a cause

Nothing here claims causation: a news item near a price move is "related in time",
and every response that puts them together says so.
"""
