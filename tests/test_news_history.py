"""News history backfill (news/history.py) and the new sources: EDGAR, GDELT, archived index pages.

No network: every archive answer is a fixture served by a fake HTTP layer. Checks parsing, the adaptive walk over
Internet Archive captures, raw-first storage with provenance, dedupe of backfilled vs live items (both orders),
idempotent re-runs, the single-date-feed rule inside an archived capture, day-dated sources, the capture time as
the publish-time horizon, recency measured from the capture, feed pagination detection, EDGAR item topics surviving
reclassification, and the per-source reach reader.

Run:  .venv/bin/python tests/test_news_history.py
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("OPENGRID_NO_JOBS", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fixtures  # noqa: E402
import normalize  # noqa: E402
import scratchdb  # noqa: E402
from news import history, parse  # noqa: E402
from news import sources as registry  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_news"
DB = "og_test_news_history"
UTC = timezone.utc


def _read(name):
    return (FIX / name).read_bytes()


def rfc(t):
    return t.strftime("%a, %d %b %Y %H:%M:%S +0000")


def feed(items):
    body = "".join(f"<item><title>{t}</title><link>{l}</link><description>{d}</description><pubDate>{p}</pubDate></item>"
                   for t, l, d, p in items)
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>{body}</channel></rss>'.encode()


class FakeHttp:
    """history.Http stand-in: url -> bytes (prefix match allowed for CDX queries); counts requests."""

    def __init__(self, routes):
        self.routes, self.requests, self.seen = routes, 0, []

    def get(self, url, cache=True):
        self.requests += 1
        self.seen.append(url)
        if url in self.routes:
            return self.routes[url]
        for k, v in self.routes.items():
            if k.endswith("*") and url.startswith(k[:-1]):
                return v
        return None


def cdx(url, stamps):
    return json.dumps([["original", "timestamp"]] + [[url, s] for s in stamps]).encode()


# --------------------------------------------------------------------------- pure

def test_parse_edgar_gdelt_index():
    items = parse.parse(_read("edgar_submissions.json"), "edgar")
    titles = [i["title"] for i in items]
    assert len(items) == 3, titles  # the 5.02-only 8-K and the Form 4 are dropped
    earn = next(i for i in items if "2.02" in i["categories"])
    assert earn["topics"] == ["earnings"] and earn["published_at"] == datetime(2026, 8, 26, 20, 21, 19, tzinfo=UTC)
    assert earn["url"] == "https://www.sec.gov/Archives/edgar/data/1045810/000104581026000073/nvda-20260826.htm"
    assert earn["guid"] == "0001045810-26-000073" and earn["title"].startswith("NVIDIA CORP 8-K: Results of operations")
    assert next(i for i in items if i["categories"][0] == "10-Q")["topics"] == ["earnings"]
    d = parse.parse(_read("edgar_formd.json"), "edgar")
    assert len(d) == 2 and all(i["topics"] == ["funding"] for i in d) and "Form D" not in d[0]["title"]
    assert d[0]["title"].startswith("Lambda, Inc./DE D: Notice of an exempt offering")

    g = parse.parse(_read("gdelt_artlist.json"), "gdelt")
    assert len(g) == 2, "a GDELT article without a safe http(s) link is dropped"
    assert g[0]["published_at"] == datetime(2024, 1, 18, 22, 45, tzinfo=UTC) and g[0]["author"] == "example-news.test"
    try:
        parse.parse(b"Please limit requests to one every 5 seconds", "gdelt")
        raise AssertionError("a GDELT rate-limit page is not a feed")
    except parse.FeedError:
        pass

    idx = history.parse_index_page(_read("index_page.html"), "https://www.crusoe.ai/resources/blog", "/blog/")
    by = {i["url"]: i for i in idx}
    assert set(by) == {"https://www.crusoe.ai/resources/blog/b200-texas", "https://www.crusoe.ai/resources/blog/h200-price-update"}, by
    assert by["https://www.crusoe.ai/resources/blog/h200-price-update"]["published_at"] == datetime(2025, 2, 11, tzinfo=UTC)
    assert not any("undated" in u for u in by), "an undated link is not history"


def test_pick_next_walk():
    caps = [history.Capture("u", t) for t in ("20240101000000", "20240301000000", "20240601000000", "20240901000000")]
    cur = caps[-1].captured_at
    # the feed reached back to 2024-05-20: the next capture is the newest one at or before 05-21
    assert history.pick_next(caps, cur, datetime(2024, 5, 20, tzinfo=UTC)).timestamp == "20240301000000"
    # no usable dates (a single-date feed): step back 30 days
    assert history.pick_next(caps, cur, None).timestamp == "20240601000000"
    assert history.pick_next(caps, caps[0].captured_at, None) is None
    # an oldest item newer than the capture itself (nonsense) also steps back
    assert history.pick_next(caps, cur, cur + timedelta(days=3)).timestamp == "20240601000000"


def test_sources_registry():
    s = registry.SOURCES
    edgar = [x for x in s.values() if x.kind == "edgar"]
    assert len(edgar) >= 14 and all(x.url.startswith("https://data.sec.gov/submissions/CIK") and x.category == "filings"
                                    for x in edgar)
    assert s["sec_nvda"].entity_hints == {"vendor": "nvidia"} and s["sec_crwv"].entity_hints == {"provider": "coreweave"}
    assert s["digitalocean_release_notes"].day_dates and s["together_changelog"].key == "guid"
    assert s["gdelt_gpu_cloud"].kind == "gdelt" and "gdeltproject.org" in s["gdelt_gpu_cloud"].note
    import re
    for sid, q in registry._GDELT_Q.items():
        short = [ph for ph in re.findall(r'"([^"]+)"', q) if " " not in ph and len(ph) < 7]
        assert not short, f"{sid}: GDELT rejects short quoted phrases ({short}): leave them unquoted"
    # Counts (CAPABILITIES.md states them): 93 registered, 78 enabled; 26 EDGAR filers, 4 GDELT queries.
    assert (len(s), len(registry.enabled())) == (93, 78), (len(s), len(registry.enabled()))
    assert len(edgar) == 26 and sum(x.kind == "gdelt" for x in s.values()) == 4

    # Bitcoin miners moving into AI compute: two GDELT queries shaped like the GPU-market ones.
    from urllib.parse import parse_qs, urlsplit
    from news import classify
    miners = ("gdelt_miner_gpu_clouds", "gdelt_miner_datacenters")
    for sid in miners:
        m, g, q = s[sid], s["gdelt_gpu_market"], registry._GDELT_Q[sid]
        assert (m.kind, m.category, m.trust_tier, m.poll_seconds, m.max_items, m.topics, m.enabled) == \
            (g.kind, g.category, g.trust_tier, g.poll_seconds, g.max_items, g.topics, True), sid
        assert parse_qs(urlsplit(m.url).query) == {"query": [q], "mode": ["artlist"], "format": ["json"],
                                                   "maxrecords": ["100"], "sort": ["datedesc"], "timespan": ["1d"]}
        assert history.methods_for(m) == ["gdelt"] and history.GDELT_QUERIES[sid] == q
        # The shape GDELT accepted live on 2026-11-15: two OR groups in parentheses (names, then compute words),
        # English only, 249 characters at most (the longest one sent; a longer query was never tried).
        names, context = re.fullmatch(r"\((.+?)\) \((.+?)\) sourcelang:english", q).groups()
        assert len(q) <= 249 and q.count('"') % 2 == 0, (sid, len(q))
        assert 2 <= len(names.split(" OR ")) <= 9 and context == 'AI OR GPU OR HPC OR "data center" OR "artificial intelligence"'
        for term in names.split(" OR "):
            assert term.startswith('"') == (" " in term), f"{sid}: quote a phrase, never a single word ({term})"
            # Every name searched for is one the classifier knows, or its hits could never carry the company.
            found = classify.classify(f"{term.strip(chr(34))} opens an AI data center", None)["entities"]["providers"]
            assert len(found) == 1, (sid, term, found)
    cls_of = lambda sid: {classify.provider_class(p["id"]) for t in registry._GDELT_Q[sid].split(") (")[0][1:].split(" OR ")  # noqa: E731
                          for p in classify.classify(f"{t.strip(chr(34))} opens an AI data center", None)["entities"]["providers"]}
    assert cls_of(miners[0]) == {"neocloud"} and cls_of(miners[1]) == {"datacenter"}
    # Their EDGAR filers: the same submissions API, the company from the hint (a filing's title has no subject).
    filers = {"sec_btdr": "bitdeer", "sec_iren": "iren", "sec_hut": "hut8", "sec_hive": "hive", "sec_btbt": "bitdigital",
              "sec_wyfi": "bitdigital", "sec_apld": "applieddigital", "sec_corz": "corescientific", "sec_cifr": "cipher",
              "sec_wulf": "terawulf"}
    for sid, pid in filers.items():
        assert s[sid].entity_hints == {"provider": pid} and pid in classify.PROVIDERS and s[sid].enabled, sid
        assert (s[sid].max_items, s[sid].poll_seconds, s[sid].trust_tier) == (40, 3 * 3600, "official")
    assert len({x.url for x in edgar}) == len(edgar), "one source per CIK"
    assert not {"sec_clsk", "sec_riot", "sec_mara", "sec_glxy"} & set(s), "mostly mining / crypto finance filings: GDELT only"
    assert history.methods_for(s["lambda_blog"]) == ["feed_archive", "wayback_feed"]
    assert history.methods_for(s["crusoe_blog"]) == ["wayback_index"], "feedless provider: archived index pages"
    assert history.methods_for(s["sec_amd"]) == ["edgar"] and history.methods_for(s["cme_press"]) == []
    from news import fetch
    from config import settings
    saved = settings.sec_user_agent
    try:
        settings.sec_user_agent = "OpenGrid ops ops@example.test"
        assert fetch.sec_user_agent() == "OpenGrid ops ops@example.test"
        settings.sec_user_agent = None
        assert fetch.sec_user_agent().startswith("OpenGrid Terminal news (+")
    finally:
        settings.sec_user_agent = saved


# --------------------------------------------------------------------------- database

def test_db_backfill_dedupe_provenance():
    url = scratchdb.create(DB)
    normalize.SessionLocal = fixtures.session(url)
    try:
        from sqlalchemy import text

        from news import store
        S = normalize.SessionLocal
        store.sync_sources()
        src = registry.get("lambda_blog")
        now = datetime.now(UTC).replace(microsecond=0)

        # A live item first.
        live = {"guid": "a", "url": "https://lambda.ai/blog/h100-price?utm_source=rss", "title": "Lambda cuts H100 prices",
                "summary": "H100 GPU price cut", "author": None, "published_raw": rfc(now - timedelta(hours=3)),
                "published_at": now - timedelta(hours=3), "raw": {}}
        assert store.ingest(src, [live], now)["new_items"] == 1

        old = now - timedelta(days=400)
        c1, c2 = (old + timedelta(days=60)).strftime("%Y%m%d%H%M%S"), old.strftime("%Y%m%d%H%M%S")
        cap1 = feed([("Lambda cuts H100 prices", "https://lambda.ai/blog/h100-price", "H100 GPU price cut", rfc(now - timedelta(hours=3))),
                     ("Lambda opens GPU cloud region in Texas", "https://lambda.ai/blog/texas", "H100 capacity", rfc(old + timedelta(days=50))),
                     ("Lambda B200 launch", "https://lambda.ai/blog/b200", "B200 GPUs", rfc(old + timedelta(days=20))),
                     ("Dated after its own capture", "https://lambda.ai/blog/future", "x", rfc(old + timedelta(days=200)))])
        # An older capture from a "single-date" period: every item carries the build stamp.
        stamp = rfc(old)
        cap2 = feed([(f"Old post {i}", f"https://lambda.ai/blog/old-{i}", "GPU", stamp) for i in range(6)])
        routes = {"https://web.archive.org/cdx/search/cdx?*": cdx(src.url, [c2, c1]),
                  f"https://web.archive.org/web/{c1}id_/{src.url}": cap1,
                  f"https://web.archive.org/web/{c2}id_/{src.url}": cap2}
        http = FakeHttp(routes)
        rep = history.wayback_feed(http, src, now - timedelta(days=1000), now)
        assert rep.documents == 2 and rep.new_items == 3 + 6, rep.as_dict()  # the live article is not duplicated
        with S() as s:
            n = s.execute(text("SELECT count(*) FROM news_items WHERE canonical_url = 'https://lambda.ai/blog/h100-price'")).scalar()
            live_row = s.execute(text("SELECT backfilled, provenance FROM news_items WHERE canonical_url = 'https://lambda.ai/blog/h100-price'")).one()
            tex = s.execute(text("SELECT backfilled, provenance, published_at_inferred, relevance_components FROM news_items WHERE url = 'https://lambda.ai/blog/texas'")).one()
            fut = s.execute(text("SELECT published_at, published_at_inferred FROM news_items WHERE url = 'https://lambda.ai/blog/future'")).one()
            olds = s.execute(text("SELECT bool_and(published_at_inferred) FROM news_items WHERE url LIKE 'https://lambda.ai/blog/old-%'")).scalar()
            raw = s.execute(text("SELECT origin, archive_url, captured_at FROM news_raw_items WHERE url = 'https://lambda.ai/blog/texas'")).one()
            raw_live = s.execute(text("SELECT count(*) FROM news_raw_items WHERE origin = 'live'")).scalar()
        assert n == 1 and live_row.backfilled is False and live_row.provenance is None, "live always wins"
        assert tex.backfilled and tex.provenance["method"] == "wayback_feed" and tex.provenance["archive_url"].startswith("https://web.archive.org/web/")
        assert not tex.published_at_inferred and tex.relevance_components["recency_mult"] == 0.75, \
            "recency measured from the capture (10 days after publication), not from today"
        assert fut.published_at_inferred, "a date after the capture is not a publish time"
        assert olds is True, "single-date feed rule applies inside an archived capture"
        assert raw.origin == "wayback_feed" and raw.archive_url.endswith(src.url) and raw.captured_at is not None
        assert raw_live == 1

        # Idempotent: a second run adds nothing (raw versions identical, items keyed by URL).
        rep2 = history.wayback_feed(FakeHttp(routes), src, now - timedelta(days=1000), now)
        assert rep2.new_items == 0
        with S() as s:
            assert s.execute(text("SELECT count(*) FROM news_raw_items")).scalar() == 1 + 4 + 6

        # The other order: backfilled first, then a live poll sees the same article -> still one row.
        b = {"guid": "b", "url": "https://lambda.ai/blog/b200", "title": "Lambda B200 launch", "summary": "B200 GPUs",
             "author": None, "published_raw": rfc(old + timedelta(days=20)), "published_at": old + timedelta(days=20), "raw": {}}
        assert store.ingest(src, [b], now)["new_items"] == 0
        with S() as s:
            row = s.execute(text("SELECT count(*), bool_and(backfilled) FROM news_items WHERE canonical_url = 'https://lambda.ai/blog/b200'")).one()
        assert tuple(row) == (1, True)

        # A day-dated source keeps shared day stamps as real dates.
        do = registry.get("digitalocean_release_notes")
        day = rfc(now.replace(hour=0, minute=0, second=0) - timedelta(days=2))
        store.ingest(do, [{"guid": f"n{i}", "url": f"https://docs.digitalocean.com/notes/2026/n{i}/", "title": f"Note {i}",
                           "summary": "GPU Droplets", "author": None, "published_raw": day,
                           "published_at": parse.parse_time(day), "raw": {}} for i in range(6)], now)
        with S() as s:
            assert s.execute(text("SELECT bool_or(published_at_inferred) FROM news_items WHERE source_id = 'digitalocean_release_notes'")).scalar() is False

        # The feed's own archive: page 2 repeating page 1 means no pagination.
        vb = registry.get("voltagepark_blog")
        p1 = feed([(f"VP {i}", f"https://www.voltagepark.com/blog/{i}", "GPU", rfc(now - timedelta(days=i))) for i in range(5)])
        rep = history.feed_pages(FakeHttp({vb.url: p1, history._with_query(vb.url, paged=2): p1}), vb, now - timedelta(days=900))
        assert rep.documents == 0 and "ignores" in rep.note
        p2 = feed([(f"VP old {i}", f"https://www.voltagepark.com/blog/old-{i}", "GPU", rfc(now - timedelta(days=100 + i))) for i in range(5)])
        rep = history.feed_pages(FakeHttp({vb.url: p1, history._with_query(vb.url, paged=2): p2}), vb, now - timedelta(days=900))
        assert rep.documents == 1 and rep.new_items == 5 and rep.oldest == now - timedelta(days=104)

        # EDGAR: full submissions; item topics survive reclassification (they live in the raw payload).
        nv = registry.get("sec_nvda")
        rep = history.edgar(FakeHttp({nv.url: _read("edgar_submissions.json")}), nv, now - timedelta(days=2000))
        assert rep.new_items == 3
        store.reclassify()
        with S() as s:
            topics = s.execute(text("SELECT topics FROM news_items WHERE url LIKE '%nvda-20260826.htm'")).scalar()
            prov = s.execute(text("SELECT provenance->>'method' FROM news_items WHERE url LIKE '%nvda-20260826.htm'")).scalar()
        assert "earnings" in topics and prov == "edgar"

        # GDELT: a URL already stored live is not duplicated; attribution in the provenance.
        dcd = registry.get("dcd")
        store.ingest(dcd, [{"guid": "d", "url": "https://www.datacenterdynamics.com/en/news/cw-deal/", "title": "CoreWeave signs GPU deal",
                            "summary": "", "author": None, "published_raw": None, "published_at": now - timedelta(days=1), "raw": {}}], now)
        gd = registry.get("gdelt_gpu_cloud")
        rep = history.gdelt(FakeHttp({history.GDELT_DOC + "?*": _read("gdelt_artlist.json")}), gd, now - timedelta(days=40), now, max_pages=1)
        assert rep.new_items == 1, rep.as_dict()
        with S() as s:
            via = s.execute(text("SELECT provenance->>'via' FROM news_items WHERE source_id = 'gdelt_gpu_cloud'")).scalar()
        assert "gdeltproject.org" in via

        # Reach: the oldest dated item per source, backfilled counted, flagged dates excluded.
        reach = {r["source_id"]: r for r in history.reach()}
        assert reach["lambda_blog"]["oldest"] == old + timedelta(days=20) and reach["lambda_blog"]["backfilled"] == 9
        assert reach["lambda_blog"]["oldest_live"] == now - timedelta(hours=3)
        health = {h["id"]: h for h in store.sources_health()}
        assert health["lambda_blog"]["backfilled_items"] == 9 and health["lambda_blog"]["oldest_dated_item_at"] == old + timedelta(days=20)
        # A backfilled item reads as such through the API dict.
        listed, _ = store.list_stories(limit=50, source="lambda_blog", min_relevance=0, backfilled=True)
        assert listed and all(x["backfilled"] and x["provenance"]["method"] == "wayback_feed" for x in listed)
    finally:
        scratchdb.drop(DB)


def test_db_miner_history():
    """The history run of a Bitcoin-miner GDELT query over GDELT's real answer for September 2026 (fetched
    2026-11-15; trimmed from 88 articles to 9, each article object untouched), and EDGAR's --since window."""
    url = scratchdb.create(DB + "_miners")
    normalize.SessionLocal = fixtures.session(url)
    try:
        from sqlalchemy import text
        S = normalize.SessionLocal
        sid = "gdelt_miner_datacenters"
        a, b = datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 10, 1, tzinfo=UTC)
        routes = {history.GDELT_DOC + "?*": _read("gdelt_miner_datacenters_2026-09.json")}

        http = FakeHttp(routes)
        rep = history.run(a, b, only=[sid], methods=["gdelt"], http=http)
        assert len(rep) == 1 and rep[0]["items"] == 9 and rep[0]["new_items"] == 9 and not rep[0]["errors"], rep
        from urllib.parse import parse_qs, urlsplit
        asked = parse_qs(urlsplit(http.seen[0]).query)
        assert http.requests == 1, "one request for one month"
        assert asked["query"] == [registry._GDELT_Q[sid]] and asked["maxrecords"] == ["250"]
        assert (asked["startdatetime"], asked["enddatetime"]) == (["20260901000000"], ["20261001000000"])

        def providers():
            with S() as s:
                rows = s.execute(text("""
                    SELECT i.title, coalesce((SELECT string_agg(e.entity_value, ',' ORDER BY e.entity_value) FROM news_entities e
                                              WHERE e.item_id = i.id AND e.entity_type = 'provider'), '') AS p,
                           i.backfilled, i.provenance, i.source_id, i.published_at
                    FROM news_items i""")).all()
            return rows
        rows = providers()
        assert len(rows) == 9 and all(r.backfilled and r.source_id == sid for r in rows)
        assert all(r.provenance["method"] == "gdelt" and "gdeltproject.org" in r.provenance["via"]
                   and "startdatetime=20260901000000" in r.provenance["archive_url"] for r in rows)
        assert all(a <= r.published_at < b for r in rows), "GDELT's first-seen time, inside the month asked for"
        by = lambda start: {r.p for r in rows if r.title.startswith(start)}  # noqa: E731
        # Compute context in the title ("GPU Financing", "AI Infrastructure"): the miners are named on the item.
        assert by("IREN Climbs 4 % on $2 . 4B Blue Owl - Led GPU Financing ; Cipher Digital Gains") == {"cipher,iren"}
        assert by("Cipher Digital Just Scored a Huge AI Infrastructure Breakthrough") == {"cipher"}
        # A share-price title with no compute word: IREN counts anywhere, the miners' names do not (two outlets).
        assert by("IREN Sinks 4 % as Rising Yields Reverse Monday Rally , TeraWulf Falls 4 %, Cipher Mining Drops") == {"iren"}
        assert by("TeraWulf Drops 4 % on Profit Taking") == {"applieddigital,iren"}
        assert by("Crypto Mining Stocks Fall Harder Than Bitcoin") == {""}, "mining-only news carries no company"
        assert by("FluidStack Closes") == {"fluidstack"}
        assert sum("cipher" in r.p or "terawulf" in r.p for r in rows) == 2

        # Resumable: the same run again stores nothing (the real Http would not even download: the month is cached).
        with S() as s:
            raw = s.execute(text("SELECT count(*) FROM news_raw_items")).scalar()
        rep2 = history.run(a, b, only=[sid], methods=["gdelt"], http=FakeHttp(routes))
        assert rep2[0]["items"] == 9 and rep2[0]["new_items"] == 0
        with S() as s:
            assert s.execute(text("SELECT count(*) FROM news_raw_items")).scalar() == raw == 9
        assert [tuple(r) for r in providers()] == [tuple(r) for r in rows]
        # A month GDELT refused is an error on the report, never a stored empty month.
        class Refused(FakeHttp):
            def get(self, url, cache=True):
                raise RuntimeError("gave up on " + url)
        bad = history.run(a, b, only=[sid], methods=["gdelt"], http=Refused({}))
        assert bad[0]["errors"] and bad[0]["errors"][0].startswith("2026-09: RuntimeError") and bad[0]["new_items"] == 0

        # EDGAR: filings from before --since are not stored (a small filer's recent block reaches back decades).
        hut = registry.get("sec_hut")
        docs = {hut.url: _read("edgar_submissions.json")}
        rep = history.run(a, only=["sec_hut"], methods=["edgar"], http=FakeHttp(docs))  # the fixture: 2 kept filings in August, 1 in September
        with S() as s:
            got = s.execute(text("""SELECT min(i.published_at), count(*), count(*) FILTER (WHERE EXISTS (SELECT 1 FROM news_entities e
                WHERE e.item_id = i.id AND e.entity_type = 'provider' AND e.entity_value = 'hut8')) FROM news_items i
                WHERE i.source_id = 'sec_hut'""")).one()
        assert rep[0]["new_items"] == got[1] == got[2] == 1, (rep, tuple(got))
        assert got[0] >= a, "the company comes from the source hint; older filings are left out"
    finally:
        scratchdb.drop(DB + "_miners")


if __name__ == "__main__":
    for t in (test_parse_edgar_gdelt_index, test_pick_next_walk, test_sources_registry, test_db_backfill_dedupe_provenance,
              test_db_miner_history):
        t()
        print(t.__name__, "ok")
