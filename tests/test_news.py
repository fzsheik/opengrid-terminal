"""News: parsing, URL canonicalization, story clustering, classification, relevance,
ingestion and timeline on a scratch database, endpoint smoke.

Run:  .venv/Scripts/python tests/test_news.py
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("OPENGRID_NO_JOBS", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402

import fixtures  # noqa: E402
import normalize  # noqa: E402
import scratchdb  # noqa: E402
from news import classify as cls  # noqa: E402
from news import dedupe, parse  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_news"
DB = "og_test_news"
UTC = timezone.utc


def _read(name: str) -> bytes:
    return (FIX / name).read_bytes()


# --------------------------------------------------------------------------- parsing

def test_parse_fixtures():
    rss = parse.parse(_read("dcd_rss.xml"), "rss")
    assert len(rss) == 4 and all(i["url"].startswith("https://www.datacenterdynamics.com/") for i in rss)
    assert rss[1]["published_at"] == datetime(2026, 10, 6, 16, 19, 9, tzinfo=UTC)
    assert "<" not in (rss[0]["summary"] or ""), "HTML stripped from summaries"

    atom = parse.parse(_read("digitalocean_atom.xml"), "atom")
    assert len(atom) == 2 and atom[0]["url"] == "https://www.digitalocean.com/blog/introducing-agent-droplets"
    assert atom[0]["published_at"].tzinfo is not None and atom[0]["raw"]["xml"].startswith("<")

    fr = parse.parse(_read("federal_register_bis.json"), "federal_register")
    assert len(fr) == 3 and fr[2]["title"] == "Revisions to the Entity List"
    assert fr[0]["published_at"] == datetime(2026, 9, 24, tzinfo=UTC), "a plain date is 00:00 UTC"
    assert fr[0]["guid"] and "Industry and Security" in (fr[0]["author"] or "")

    jf = parse.parse(_read("jsonfeed.json"), "json_api")
    assert jf[0]["published_at"] == datetime(2026, 10, 1, 13, 30, tzinfo=UTC), "offset converted to UTC"
    assert jf[1]["published_at"] is None, "no date stays None; the store falls back to first-seen"

    rdf = parse.parse(_read("rss1_rdf.xml"), "rss")
    assert len(rdf) == 1 and rdf[0]["published_at"] == datetime(2026, 9, 30, 12, tzinfo=UTC)

    bad = parse.parse(_read("malformed_rss.xml"), "rss")
    assert [i["title"] for i in bad] == ["Nvidia & AMD ship MI355X and B300 SXM systems", "Second item survives"]
    assert bad[0]["author"] == "Jane Doe" and bad[1]["published_at"] is None

    cp = '<?xml version="1.0" encoding="utf-8"?><rss><channel><item><title>NVIDIA HGX® B300 ’ café</title>'          '<link>https://x.test/a</link></item></channel></rss>'
    assert parse.parse(cp.encode("cp1252"), "rss")[0]["title"] == "NVIDIA HGX® B300 ’ café", "mislabelled cp1252"
    # Sniffing: an Atom body under kind="rss" still parses.
    assert len(parse.parse(_read("digitalocean_atom.xml"), "rss")) == 2
    for body in (b"<html><body>not a feed</body></html>", b"<!DOCTYPE x [<!ENTITY a 'b'>]><rss/>", b"{}"):
        try:
            parse.parse(body, "rss")
            raise AssertionError(f"should refuse {body!r}")
        except (parse.FeedError, ValueError):
            pass


def test_parse_time():
    assert parse.parse_time("Tue, 06 Oct 2026 16:40:11 +0000") == datetime(2026, 10, 6, 16, 40, 11, tzinfo=UTC)
    assert parse.parse_time("Tue, 06 Oct 2026 12:40:11 EDT") == datetime(2026, 10, 6, 16, 40, 11, tzinfo=UTC)
    assert parse.parse_time("2026-10-06T18:58:44.908Z").tzinfo == UTC
    assert parse.parse_time("2026-10-06T10:00:00") == datetime(2026, 10, 6, 10, tzinfo=UTC), "naive read as UTC"
    assert parse.parse_time("yesterday") is None and parse.parse_time(None) is None


# --------------------------------------------------------------------------- dedupe

def test_canonical_url():
    c = dedupe.canonical_url
    base = "https://example.com/news/gpu-prices"
    for u in ("http://www.example.com/news/gpu-prices/", "https://EXAMPLE.com/news/gpu-prices?utm_source=rss&utm_medium=x",
              "https://example.com/news/gpu-prices#comments", "https://example.com:443/news/gpu-prices?fbclid=abc",
              "https://m.example.com/news/gpu-prices/amp/", "https://example.com//news/gpu-prices?gclid=1&ref=feed"):
        assert c(u) == base, (u, c(u))
    assert c("https://example.com/a?b=2&a=1&utm_campaign=z") == "https://example.com/a?a=1&b=2", "params sorted, kept"
    assert c("https://example.com/Case/Path") != c("https://example.com/case/path"), "path case kept"
    assert dedupe.url_hash(c("http://www.example.com/x/")) == dedupe.url_hash(c("https://example.com/x?utm_x=1"))
    k1 = dedupe.item_key("s", {"url": None, "guid": "g1"})
    assert k1[0] == "urn:opengrid:s:g1"


def test_story_clustering():
    n = dedupe.normalize_title
    a = n("CoreWeave signs $14 billion AI compute deal with Meta - The Register")
    b = n("CoreWeave signs $14 billion AI compute deal with Meta | DCD")
    c = n("CoreWeave signs $14bn AI compute deal with Meta Platforms")
    d = n("CoreWeave cuts H100 prices in Europe")
    e = n("Meta signs AI compute deal with Google")
    assert a == b, "outlet suffix dropped"
    assert dedupe.same_story(a, c), dedupe.title_similarity(a, c)
    assert not dedupe.same_story(a, d) and not dedupe.same_story(a, e), "different stories stay apart"
    assert not dedupe.same_story(n("AWS update"), n("AWS updates")), "short titles need an exact match"
    t = datetime(2026, 10, 1, tzinfo=UTC)
    cands = [{"story_id": 7, "title_key": a, "at": t}, {"story_id": 9, "title_key": d, "at": t}]
    assert dedupe.find_story(c, t + timedelta(hours=10), cands) == 7
    assert dedupe.find_story(c, t + timedelta(days=5), cands) is None, "outside the window: a new story"


# --------------------------------------------------------------------------- classification

def _c(title, summary=None, **kw):
    return cls.classify(title, summary, **kw)


def test_classify_gpus():
    r = _c("Neocloud adds thousands of H100 GPUs")
    assert r["entities"]["gpus"] == [], "a bare H100 never becomes a specific variant"
    fam = {f["family"]: f for f in r["entities"]["gpu_families"]}
    assert set(fam["H100"]["variants"]) == {"NVIDIA H100 80GB SXM5", "NVIDIA H100 80GB PCIe",
                                            "NVIDIA H100 80GB PCIe NVLink", "NVIDIA H100 94GB NVL"}
    assert _c("H100 SXM clusters online")["entities"]["gpus"] == ["NVIDIA H100 80GB SXM5"]
    assert _c("New HGX H100 nodes")["entities"]["gpus"] == ["NVIDIA H100 80GB SXM5"]
    assert _c("H100 PCIe instances")["entities"]["gpus"] == ["NVIDIA H100 80GB PCIe"]
    assert _c("H100 PCIe NVLink pairs")["entities"]["gpus"] == ["NVIDIA H100 80GB PCIe NVLink"]
    assert _c("H200 NVL racks")["entities"]["gpus"] == ["NVIDIA H200 143GB NVL"]
    assert _c("A100 SXM servers")["entities"]["gpus"] == [], "A100 SXM hides 40 vs 80GB: family only"
    assert _c("A100 80GB SXM servers")["entities"]["gpus"] == ["NVIDIA A100 80GB SXM4"]
    gh = {f["family"] for f in _c("GH200 superchip and GB200 NVL72 racks")["entities"]["gpu_families"]}
    assert "GH200" in gh and "GB200" in gh and "H200" not in gh and "B200" not in gh, gh
    bw = {f["family"]: f for f in _c("NVIDIA Blackwell GPUs ship")["entities"]["gpu_families"]}
    assert "NVIDIA B200 180GB SXM" in bw["Blackwell"]["variants"]
    mi = {f["family"] for f in _c("AMD MI300X and MI355X GPUs")["entities"]["gpu_families"]}
    assert {"MI300X", "MI355X"} <= mi
    assert not _c("Rubin wins the election")["entities"]["gpu_families"], "Rubin needs compute context"
    assert {"Rubin"} <= {f["family"] for f in _c("NVIDIA Vera Rubin GPUs")["entities"]["gpu_families"]}
    assert set(cls.families_for_gpu("NVIDIA H100 80GB SXM5")) == {"H100", "Hopper"}


def test_classify_providers_regions_topics():
    p = lambda t, **kw: [x["id"] for x in _c(t, **kw)["entities"]["providers"]]  # noqa: E731
    assert p("Lambda Labs raises $480M for GPU cloud") == ["lambda"]
    assert p("DataCrunch rebrands") == ["verda"]
    assert "lambda" not in p("AWS Lambda adds a new runtime for GPU inference"), "AWS Lambda is not Lambda"
    assert "lambda" in p("Lambda opens GPU cluster in Kansas City")
    assert "crusoe" not in p("Robinson Crusoe review"), "ambiguous names need compute context"
    assert p("Vast.ai lists RTX 5090") == ["vast"] and p("A vast GPU market") == []
    hint = _c("How to fine-tune models", provider_hint="runpod")["entities"]["providers"]
    assert hint[0]["id"] == "runpod" and hint[0]["via"] == "source"

    r = _c("CoreWeave adds 50MW data center in Texas, expands to Norway")
    assert {g["group"] for g in r["entities"]["regions"]} == {"US", "Europe"}
    assert {"capacity", "datacenter", "region_launch", "neocloud_expansion"} <= set(r["topics"]), r["topics"]
    assert "price_change" in _c("Hyperstack cuts H100 prices")["topics"]
    assert "price_change" in _c("Now $1.99/GPU-hour for H100")["topics"]
    assert "export_controls" in _c("Commerce adds firms to Entity List")["topics"]
    assert "export_controls" in _c("Rule text", source_topics=("export_controls",))["topics"], "source default topic"
    assert "gpu_launch" in _c("AMD unveils MI355X")["topics"]
    assert "futures" in _c("Exchange plans GPU compute futures")["topics"]
    assert "futures" not in _c("Restricting stockpiling of polysilicon derivatives")["topics"]
    assert "supply_deal" in _c("xAI orders 100,000 Nvidia GB200 GPUs")["topics"]
    assert "supply_deal" not in _c("When to buy or rent GPUs")["topics"], "a supply deal needs a quantity"
    assert "UK" in {g["group"] for g in _c("Nscale opens London site")["entities"]["regions"]}
    assert set(cls.TOPIC_IDS) >= {"price_change", "capacity", "availability", "outage", "region_launch", "gpu_launch",
                                  "funding", "capex", "supply_deal", "export_controls", "power", "contracts",
                                  "futures", "pricing_report", "neocloud_expansion"}


def test_relevance():
    t, s = "CoreWeave cuts H100 SXM prices in Texas", "GPU capacity expands"
    pub = datetime(2026, 10, 1, tzinfo=UTC)
    a = _c(t, s, source_tier="official", published_at=pub, first_seen_at=pub)
    b = _c(t, s, source_tier="official", published_at=pub, first_seen_at=pub)
    assert a == b, "deterministic"
    comp = a["relevance_components"]
    expect = round(min(100, (comp["entity_points"] + comp["topic_points"]) * 1.25 * comp["tier_mult"] * comp["recency_mult"]))
    assert a["relevance"] == expect and 0 <= a["relevance"] <= 100
    press = _c(t, s, source_tier="press", published_at=pub, first_seen_at=pub)
    old = _c(t, s, source_tier="official", published_at=pub - timedelta(days=60), first_seen_at=pub)
    assert press["relevance"] < a["relevance"] and old["relevance"] < a["relevance"]
    noise = _c("Amazon S3 adds a new storage class", source_tier="official")
    assert noise["relevance"] < 20 < a["relevance"], (noise["relevance"], a["relevance"])
    assert _c("Unrelated cooking recipe")["relevance"] == 0
    dax = _c("Amazon DynamoDB Accelerator (DAX) is now available in additional Regions", source_tier="official")
    assert "availability" not in dax["topics"] and dax["relevance"] < 20, "generic topics need compute context"
    p5 = _c("Amazon EC2 P5 instances now available in additional Regions", source_tier="official")
    assert {"availability", "region_launch"} <= set(p5["topics"]) and p5["relevance"] > dax["relevance"]


# --------------------------------------------------------------------------- database

def _setup_db():
    url = scratchdb.create(DB)
    Session = fixtures.session(url)
    normalize.SessionLocal = Session
    return Session


def _feed(items):
    """A small RSS body from (title, link, pubDate) tuples."""
    body = "".join(f"<item><title>{t}</title><link>{l}</link><description>{d}</description><pubDate>{p}</pubDate></item>"
                   for t, l, d, p in items)
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>{body}</channel></rss>'.encode()


def test_db_ingest_cluster_timeline():
    Session = _setup_db()
    try:
        from analytics import rollups
        from news import ingest, store, timeline
        from news import sources as registry
        from sqlalchemy import text

        meta = fixtures.seed(Session, days=6)
        rollups.refresh()
        now = meta["now"]
        rfc = lambda t: t.strftime("%a, %d %b %Y %H:%M:%S +0000")  # noqa: E731
        t_news = now - timedelta(hours=30)
        title = "CoreWeave signs $14 billion GPU compute deal for H100 capacity"
        bodies = {
            "dcd": _feed([(title + " - DCD", "https://www.datacenterdynamics.com/en/news/cw-deal/?utm_source=rss",
                           "GPU capacity deal.", rfc(t_news)),
                          ("AWS plans data center campus in Pennsylvania", "https://www.datacenterdynamics.com/en/news/aws-pa/",
                           "Campus.", rfc(now - timedelta(hours=5)))]),
            "the_register": _feed([(title, "https://www.theregister.com/2026/10/01/coreweave_deal/", "H100 GPUs.",
                                    rfc(t_news + timedelta(hours=2)))]),
            "hpcwire": _feed([(title.replace("$14 billion", "$14bn"), "https://www.hpcwire.com/cw-deal/", "deal",
                               rfc(t_news + timedelta(hours=3)))]),
            "lambda_blog": _feed([("Lambda adds H100 SXM in Texas", "https://lambda.ai/blog/h100-tx", "GPU capacity", "garbage date")]),
        }
        calls = []

        def handler(req: httpx.Request):
            calls.append((str(req.url), req.headers.get("if-none-match")))
            for sid, body in bodies.items():
                if req.url.host.endswith(registry.get(sid).url.split("/")[2].removeprefix("www.")):
                    if req.headers.get("if-none-match") == '"v1"':
                        return httpx.Response(304)
                    return httpx.Response(200, content=body, headers={"etag": '"v1"'})
            if "techcrunch" in req.url.host:
                raise httpx.ConnectError("boom")
            return httpx.Response(500, text="nope")

        from news import fetch as fetcher
        real_client = fetcher.client
        fetcher.client = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
        only = list(bodies) + ["techcrunch_ai", "hpcwire"]
        try:
            r = ingest.run(force=True, only=only)
            assert r["sources"]["dcd"]["new_items"] == 2 and r["sources"]["the_register"]["new_items"] == 1, r
            assert r["sources"]["techcrunch_ai"]["ok"] is False, "a failing source is logged, others still ingest"
            # Second run: conditional GET -> 304, nothing new, nothing duplicated.
            r2 = ingest.run(force=True, only=only)
            assert r2["sources"]["dcd"]["not_modified"] and r2["sources"]["dcd"]["new_items"] == 0, r2
            assert any(h == '"v1"' for _u, h in calls), "If-None-Match sent"
            # Without validators the same items arrive again and are still not duplicated.
            r3 = ingest.run(force=True, only=["dcd"], conditional=False)
            assert r3["sources"]["dcd"]["new_items"] == 0
        finally:
            fetcher.client = real_client

        with Session() as s:
            n_items = s.execute(text("SELECT count(*) FROM news_items")).scalar()
            n_raw = s.execute(text("SELECT count(*) FROM news_raw_items")).scalar()
            logs = s.execute(text("SELECT count(*), count(*) FILTER (WHERE NOT ok) FROM news_fetch_log")).one()
            stories = s.execute(text("SELECT story_id, count(*) FROM news_items GROUP BY story_id HAVING count(*) > 1")).all()
            inferred = s.execute(text("SELECT published_at_inferred FROM news_items WHERE source_id='lambda_blog'")).scalar()
        assert n_items == 5 and n_raw == 5, (n_items, n_raw)
        assert tuple(logs) == (11, 2), logs
        assert len(stories) == 1 and stories[0][1] == 3, "one story syndicated by three outlets"
        assert inferred is True, "unreadable date -> first-seen, flagged"

        listed, total = store.list_stories(limit=10, min_relevance=0)
        assert total == 3, total
        deal = next(x for x in listed if x["source_count"] == 3)
        assert {x["source_id"] for x in deal["sources"]} == {"dcd", "the_register", "hpcwire"}

        rel = store.related(gpu="NVIDIA H100 80GB SXM5")
        assert {x["source_count"] for x in rel} == {3, 1}, "family-level H100 news is related to the SXM5 variant"
        assert store.related(gpu="NVIDIA H100 80GB PCIe")[0]["title"].startswith("CoreWeave"), \
            "the SXM-only Lambda post is not related to PCIe"
        assert len(store.related(gpu="NVIDIA H100 80GB PCIe")) == 1
        assert store.related(provider="lambda")[0]["providers"] == ["lambda"]
        assert store.related(region_group="US")
        assert store.related(gpu="H100"), "family id accepted"
        assert store.reclassify()["reclassified"] == 5
        health = {h["id"]: h for h in store.sources_health()}
        assert health["dcd"]["health"] == "ok" and health["dcd"]["conditional_get"]
        assert health["techcrunch_ai"]["health"] == "degraded" and health["cme_press"]["health"] == "disabled"

        gpu = "NVIDIA H100 80GB SXM5"
        # create_all may have built the events agent's table; the timeline must cope without it.
        with Session.begin() as s:
            s.execute(text("DROP TABLE IF EXISTS market_events CASCADE"))
        tl = timeline.timeline(gpu=gpu, t0=now - timedelta(days=3), t1=now + timedelta(hours=1))
        assert tl["prices"]["points"] and tl["prices"]["kind"] == "observed"
        assert tl["market_events"]["available"] is False, "no market_events table yet: said so, not faked"
        assert tl["news"]["count"] == 2

        # A synthetic market_events table shaped like the events agent's contract (test only).
        with Session.begin() as s:
            s.execute(text("""CREATE TABLE IF NOT EXISTS market_events (id bigserial PRIMARY KEY, occurred_at timestamptz,
                detected_at timestamptz, type text, segment text, gpu text, provider text, region_group text,
                severity text, title text, detail jsonb, dedupe_key text UNIQUE)"""))
            s.execute(text("""INSERT INTO market_events (occurred_at, detected_at, type, segment, gpu, provider, severity, title, detail, dedupe_key)
                VALUES (:a, :a, 'price_move', 'on_demand', :g, 'syn_alpha', 'notable', 'syn_alpha H100 price -8%', '{}', 'k1'),
                       (:b, :b, 'sold_out', 'on_demand', 'NVIDIA L40S 48GB', 'syn_beta', 'info', 'other gpu', '{}', 'k2')"""),
                      {"a": t_news + timedelta(hours=6), "b": t_news, "g": gpu})
        tl = timeline.timeline(gpu=gpu, t0=now - timedelta(days=3), t1=now + timedelta(hours=1))
        assert tl["market_events"]["available"] and [e["dedupe_key"] for e in tl["market_events"]["events"]] == ["k1"]
        ar = timeline.around_move(gpu, t_news + timedelta(hours=6), window_hours=48)
        assert ar["heading"] == "Related news and events around this move (not necessarily causal)"
        assert ar["related"][0]["rank"] >= ar["related"][-1]["rank"] and len(ar["related"]) == 3
        assert {r["type"] for r in ar["related"]} == {"news", "market_event"}
        dumped = json.dumps(ar, default=str).lower() + json.dumps(tl, default=str).lower()
        for word in ("caused", "because", "due to"):
            assert word not in dumped, word
        prov = timeline.timeline(provider="syn_alpha", t0=now - timedelta(days=2), t1=now)
        assert prov["prices"]["by_gpu"], "provider-only timeline keeps one line per GPU"
        empty = timeline.timeline(gpu="NVIDIA GH200 96GB", t0=now - timedelta(days=2), t1=now)
        assert "insufficient coverage" in empty["prices"]["unavailable"]
    finally:
        scratchdb.drop(DB)


def test_endpoints():
    Session = _setup_db()
    try:
        import config
        config.settings.app_password = None  # open local-dev operator for the smoke test
        import main
        from fastapi.testclient import TestClient
        from news import ingest, store
        from news import sources as registry

        now = datetime.now(UTC)
        store.sync_sources()
        src = registry.get("nvidia_newsroom")
        store.ingest(src, [{"guid": "1", "url": "https://nvidianews.nvidia.com/news/b300", "title": "NVIDIA ships HGX B300 systems to CoreWeave",
                            "summary": "Blackwell Ultra GPUs", "author": None, "published_raw": None,
                            "published_at": now - timedelta(hours=3), "raw": {}}], now)
        client = TestClient(main.app, headers={"X-OpenGrid-Request": "1"})  # CSRF header, as web/core.js sends  # no context manager: no lifespan, so no migrations against the dev DB
        r = client.get("/v1/news")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["meta"]["methodology"] == "/methodology/news" and body["data"][0]["gpus"] == ["NVIDIA B300 288GB SXM"]
        item_id = body["data"][0]["id"]
        assert client.get("/v1/news", params={"gpu": "b300"}).json()["data"], "family filter"
        assert client.get("/v1/news", params={"gpu": "b300-288gb-sxm"}).json()["data"], "slug filter"
        assert client.get("/v1/news", params={"provider": "coreweave", "topic": "gpu_launch"}).json()["data"]
        assert client.get("/v1/news", params={"q": "nothing-matches-this"}).json()["data"] == []
        assert client.get("/v1/news", params={"region": "Mars"}).status_code == 400
        assert client.get("/v1/news", params={"topic": "bogus"}).status_code == 400
        assert client.get("/v1/news", params={"gpu": "not-a-gpu"}).status_code == 404
        d = client.get(f"/v1/news/{item_id}").json()["data"]
        assert d["relevance_components"]["formula"] and d["entities"]["gpu_families"]
        assert client.get("/v1/news/999999").status_code == 404
        srcs = client.get("/v1/news/sources").json()
        assert srcs["meta"]["enabled"] >= 30 and srcs["meta"]["disabled"] >= 5
        topics = client.get("/v1/news/topics").json()["data"]
        assert any(t["id"] == "gpu_launch" and t["items_30d"] == 1 for t in topics)
        t = client.get("/v1/timeline", params={"gpu": "h100-80gb-sxm5", "window": "7d"})
        assert t.status_code == 200 and "unavailable" in t.json()["data"]["prices"], t.text
        assert client.get("/v1/timeline").status_code == 400
        assert client.get("/v1/timeline", params={"gpu": "h100"}).status_code == 400, "a family is not one price line"
        a = client.get("/v1/timeline/around", params={"gpu": "b300-288gb-sxm", "at": now.isoformat(), "window_hours": 24})
        assert a.status_code == 200 and a.json()["data"]["heading"].endswith("(not necessarily causal)")
        assert a.json()["data"]["related"][0]["type"] == "news"
        real_run = ingest.run
        ingest.run = lambda **kw: {"fetched": 0, "sources": {}, "kw": kw}
        try:
            r = client.post("/v1/news/refresh", params={"source": "dcd"})
            assert r.status_code == 200 and r.json()["data"]["kw"]["only"] == ["dcd"]
            assert client.post("/v1/news/refresh", params={"source": "cme_press"}).status_code == 404
        finally:
            ingest.run = real_run
        assert client.post("/v1/news/reclassify").json()["data"]["reclassified"] == 1
    finally:
        scratchdb.drop(DB)


if __name__ == "__main__":
    for t in (test_parse_fixtures, test_parse_time, test_canonical_url, test_story_clustering, test_classify_gpus,
              test_classify_providers_regions_topics, test_relevance, test_db_ingest_cluster_timeline, test_endpoints):
        t(); print(t.__name__, "ok")
