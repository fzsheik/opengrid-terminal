"""Major shifts (analytics/shifts.py, api/shifts.py): deterministic detection, stored related items, the API.

Pure: episode detection against the series' own volatility (and its floors and baseline requirement), provider
repricing clusters, archived per-GPU changes folded into a repricing, capacity episodes. Database: a scratch DB with
a synthetic market_gpu_hourly jump, a news item, an SEC filing, a provider announcement and OpenGrid market events
near it, archived prices before and after live coverage; run twice (idempotent, items stable), rebuild, and the
endpoints. Wording: every OpenGrid-authored string about a shift says "not necessarily causal" and never claims one.

Run:  .venv/bin/python tests/test_shifts.py
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("OPENGRID_NO_JOBS", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fixtures  # noqa: E402
import normalize  # noqa: E402
import scratchdb  # noqa: E402
from analytics import shifts  # noqa: E402

DB = "og_test_shifts"
UTC = timezone.utc
H = timedelta(hours=1)
CAUSAL = re.compile(r"\b(caused?|causes|because|due to|led to|drove|driven by|triggered|explains?)\b", re.I)
T0 = datetime(2026, 9, 1, tzinfo=UTC)


def series(n_hours=24 * 20, jump_at=24 * 15, jump=0.30, noise=0.01, jump_hours=3):
    """Deterministic 24h changes: +-noise alternating, then a jump lasting jump_hours."""
    times = [T0 + i * H for i in range(n_hours)]
    vals = [(noise if i % 2 else -noise) for i in range(n_hours)]
    for k in range(jump_hours):
        vals[jump_at + k] = jump - 0.01 * k
    return times, vals


# --------------------------------------------------------------------------- pure

def test_episodes_deterministic():
    times, vals = series()
    a = shifts.episodes(times, vals, shifts.MEDIAN_MIN)
    b = shifts.episodes(times, vals, shifts.MEDIAN_MIN)
    assert a == b and len(a) == 1, a
    ep = a[0]
    assert ep["at"] == ep["start"] == times[24 * 15] and ep["hours"] == 3 and ep["value"] == 0.30
    assert abs(ep["threshold"] - shifts.MEDIAN_MIN) < 1e-9, "a quiet series: the 10% floor binds"
    # Below the floor: nothing, however quiet the series.
    assert shifts.episodes(*series(jump=0.08), shifts.MEDIAN_MIN) == []
    # A noisy series raises its own bar: a 30% move inside +-12% noise (3 x RMS = 36%) is not a shift.
    assert shifts.episodes(*series(noise=0.12), shifts.MEDIAN_MIN) == []
    # Not enough baseline (first 7 days): no judgement, no shift.
    assert shifts.episodes(*series(jump_at=24 * 5), shifts.MEDIAN_MIN) == []
    # The move's own 24h is never part of its baseline: a second jump the next hour joins the same episode.
    t, v = series(jump_hours=1)
    v[24 * 15 + 5] = 0.35
    eps = shifts.episodes(t, v, shifts.MEDIAN_MIN)
    assert len(eps) == 1 and eps[0]["at"] == t[24 * 15 + 5] and eps[0]["start"] == t[24 * 15]
    # Opposite sign starts a new episode.
    v[24 * 15 + 8] = -0.40
    assert [e["sign"] for e in shifts.episodes(t, v, shifts.MEDIAN_MIN)] == [1, -1]
    assert shifts._sev(0.30, 0.10) == "major" and shifts._sev(0.12, 0.10) == "notable"


def test_repricing_archived_capacity_pure():
    t = T0
    ev = lambda i, gpu, pct, dt, prov="verda": {"id": i, "provider": prov, "gpu": gpu, "pct": pct, "occurred_at": t + dt}  # noqa: E731
    out = shifts.repricings([ev(1, "A", 0.12, H), ev(2, "B", 0.15, 3 * H), ev(3, "C", 0.20, 20 * H), ev(4, "D", -0.3, 2 * H),
                             ev(5, "E", 0.11, 30 * H), ev(6, "A", 0.5, 2 * H, "lambda")])
    assert len(out) == 1 and out[0]["provider"] == "verda" and out[0]["gpus"] == ["A", "B", "C"] and out[0]["sign"] == 1
    assert out[0]["median_pct"] == 0.15 and out[0]["events"] == [1, 2, 3]

    p = lambda prov, gpu, day, price: {"provider": prov, "gpu": gpu, "t": t + timedelta(days=day), "price": price,  # noqa: E731
                                       "source_url": f"https://web.archive.org/web/{day}/x"}
    pts = [p("nebius", g, 0, 2.0) for g in "XYZ"] + [p("nebius", g, 10, 2.6) for g in "XYZ"] + \
          [p("lambda", "X", 0, 2.0), p("lambda", "X", 5, 2.1), p("lambda", "X", 9, 1.5)]
    singles, reprices = shifts.archived_changes(pts)
    assert len(reprices) == 1 and len(reprices[0]["changes"]) == 3 and abs(reprices[0]["median"] - 0.3) < 1e-9
    assert len(singles) == 1 and singles[0]["provider"] == "lambda" and singles[0]["from"] == 2.1, \
        "consecutive captures only; 5% is not a change; the nebius GPUs are folded into the repricing"

    cap = lambda i, h, typ="sold_out", prov=None, provs=("a", "b"), sev="major", d=None: {  # noqa: E731
        "id": i, "type": typ, "gpu": "G" if typ == "sold_out" else None, "provider": prov, "severity": sev,
        "occurred_at": t + h * H, "detail": d or {"providers": list(provs)}}
    groups = shifts.capacity_episodes([cap(1, 0), cap(2, 10), cap(3, 200), cap(4, 1, provs=("a",)), cap(5, 2, prov="x"),
                                       cap(6, 5, "market_capacity", d={"direction": "tightening", "share": 0.5}),
                                       cap(7, 6, "market_capacity", sev="notable", d={"direction": "tightening"})])
    assert [[e["id"] for e in g] for g in groups] == [[1, 2], [6], [3]], "flapping within 3 days is one episode"


# --------------------------------------------------------------------------- database

def _seed(S, now):
    from sqlalchemy import insert, text

    from store.archive import ArchivedPrice
    from store.events import MarketEvent, MarketGpuHourly
    from store.analytics import MarketHourly
    gpu = "NVIDIA H100 80GB SXM5"
    times, vals = series()
    with S.begin() as s:
        s.execute(insert(MarketGpuHourly), [{"segment": "on_demand", "gpu": gpu, "hour": t, "chg24_median": v,
                                             "providers_priced": 4, "median": 2.0 * (1 + v)} for t, v in zip(times, vals)])
        # A thin GPU (one provider): never a shift, however large the move.
        s.execute(insert(MarketGpuHourly), [{"segment": "on_demand", "gpu": "NVIDIA L4 24GB", "hour": t, "chg24_median": v,
                                             "providers_priced": 1} for t, v in zip(*series(jump=0.9))])
        jump = times[24 * 15]
        s.execute(insert(MarketEvent), [
            {"occurred_at": jump - 2 * H, "detected_at": now, "type": "price_move", "segment": "on_demand", "gpu": gpu,
             "provider": None, "severity": "major", "title": "NVIDIA H100 80GB SXM5 market median up 28% in 24h",
             "detail": {}, "pct": 0.28, "dedupe_key": "e1"},
            {"occurred_at": jump - 100 * H, "detected_at": now, "type": "sold_out", "segment": "on_demand", "gpu": gpu,
             "provider": "lambda", "severity": "notable", "title": "outside the window", "detail": {}, "dedupe_key": "e2"},
        ])
        # Archived: lambda's H100 list price before live coverage (two captures, +40%), and after it (ignored).
        first_live = T0 + timedelta(days=2)
        s.execute(insert(MarketHourly), [{"segment": "on_demand", "gpu": gpu, "provider": "lambda", "region": "", "hour": first_live,
                                          "min_price": 2.49}])
        rows = []
        for i, (day, price) in enumerate(((-200, 1.99), (-150, 2.79), (5, 9.99))):
            rows.append({"source": "wayback", "source_url": f"https://web.archive.org/web/{i}/lambda.ai/pricing", "source_sha256": "x" * 64,
                         "parser": "t", "provider": "lambda", "listing_id": "h100", "raw_gpu_name": "H100 SXM", "gpu": gpu,
                         "gpu_count": 8, "price_per_gpu_hour": price, "currency": "USD", "market_type": "on_demand",
                         "observed_at": T0 + timedelta(days=day), "imported_at": now})
        s.execute(insert(ArchivedPrice), rows)
    return gpu, jump


def _news(now, jump):
    from news import sources as registry
    from news import store
    store.sync_sources()
    rfc = lambda t: t.strftime("%a, %d %b %Y %H:%M:%S +0000")  # noqa: E731
    item = lambda guid, url, title, summary, t: {"guid": guid, "url": url, "title": title, "summary": summary, "author": None,  # noqa: E731
                                                  "published_raw": rfc(t), "published_at": t, "raw": {}}
    store.ingest(registry.get("dcd"), [
        item("1", "https://www.datacenterdynamics.com/en/news/h100-shortage/", "H100 GPU shortage tightens cloud capacity",
             "Cloud providers report H100 GPU capacity sold out as demand rises", jump - 10 * H),
        item("2", "https://www.datacenterdynamics.com/en/news/unrelated/", "Data center campus planned in Ohio", "campus", jump - 5 * H),
        item("3", "https://www.datacenterdynamics.com/en/news/h100-later/", "H100 GPU prices outside the window", "H100 GPU", jump + 200 * H),
    ], now)
    store.ingest(registry.get("lambda_blog"), [item("4", "https://lambda.ai/blog/h100-pricing", "Lambda updates H100 SXM pricing",
                                                     "New H100 SXM GPU on-demand price", jump - 30 * H)], now)
    store.ingest(registry.get("sec_nvda"), [{"guid": "0001045810-26-000073", "url": "https://www.sec.gov/Archives/edgar/data/1045810/x/nvda.htm",
                                             "title": "NVIDIA CORP 8-K: Results of operations and financial condition (earnings release)",
                                             "summary": "SEC filing 8-K.", "author": "SEC EDGAR", "topics": ["earnings"],
                                             "published_raw": rfc(jump - 20 * H), "published_at": jump - 20 * H, "raw": {}}], now)
    # The archived window (-150 days): a backfilled Lambda post.
    store.ingest(registry.get("lambda_blog"), [item("5", "https://lambda.ai/blog/old-h100", "Lambda adds H100 SXM capacity",
                                                     "H100 SXM GPU cluster capacity", T0 - timedelta(days=152))], now,
                 backfill={"origin": "wayback_feed", "archive_url": "https://web.archive.org/web/1/lambda.ai/blog/rss.xml",
                           "captured_at": T0 - timedelta(days=140)})


def _authored(obj) -> list[str]:
    """OpenGrid-written strings of a shift payload (not third-party titles inside item snapshots)."""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("items", "top_items"):
                for it in v:
                    out += [it.get("score_components", {}).get("formula", ""), it.get("match") or ""]
                continue
            out += _authored(v) if isinstance(v, (dict, list)) else ([v] if isinstance(v, str) else [])
    elif isinstance(obj, list):
        for v in obj:
            out += _authored(v)
    return out


def test_db_shifts_items_api():
    url = scratchdb.create(DB)
    S = normalize.SessionLocal = fixtures.session(url)
    try:
        from sqlalchemy import text
        now = datetime.now(UTC)
        gpu, jump = _seed(S, now)
        _news(now, jump)

        r1 = shifts.run(now=now)
        rows, total = shifts.query(limit=50)
        by_type = {}
        for r in rows:
            by_type.setdefault(r["type"], []).append(r)
        assert set(by_type) == {"market_median", "archived_price_change"}, by_type.keys()
        med = by_type["market_median"]
        assert len(med) == 1 and med[0]["gpu"] == gpu and med[0]["at"] == jump and med[0]["kind"] == "observed"
        assert med[0]["severity"] == "major" and med[0]["window_start"] == jump - 24 * H - shifts.BEFORE
        arch = by_type["archived_price_change"]
        assert len(arch) == 1 and arch[0]["kind"] == "archived" and "archived" in arch[0]["label"]
        assert arch[0]["at"] == T0 - timedelta(days=150) and abs(arch[0]["magnitude"] - (2.79 / 1.99 - 1)) < 1e-9, \
            "only captures before live coverage; the 9.99 capture after it is ignored"

        d = shifts.get(med[0]["id"])
        kinds = {(i["item_type"], i["title"]) for i in d["items"]}
        titles = [i["title"] for i in d["items"]]
        assert ("news", "H100 GPU shortage tightens cloud capacity") in kinds
        assert ("provider_announcement", "Lambda updates H100 SXM pricing") in kinds
        assert any(t == "filing" and "earnings release" in ti for t, ti in kinds), "NVIDIA filing related by vendor"
        assert ("market_event", "NVIDIA H100 80GB SXM5 market median up 28% in 24h") in kinds
        assert "Data center campus planned in Ohio" not in titles and "H100 GPU prices outside the window" not in titles
        assert "outside the window" not in titles
        assert [i["rank"] for i in d["items"]] == list(range(1, len(d["items"]) + 1))
        assert all(d["items"][k]["score"] >= d["items"][k + 1]["score"] for k in range(len(d["items"]) - 1))
        filing = next(i for i in d["items"] if i["item_type"] == "filing")
        assert filing["match"] == "vendor" and filing["score_components"]["formula"].startswith("0.5 * relevance/100")
        assert d["heading"] == "Related news and events around this shift (not necessarily causal)"
        da = shifts.get(arch[0]["id"])
        old = [i for i in da["items"] if i["title"] == "Lambda adds H100 SXM capacity"]
        assert old and old[0]["backfilled"] and old[0]["provenance"] == "wayback_feed", "backfilled news attaches to archived shifts"

        # Idempotent: the second run finds the same shifts, writes nothing new, keeps the settled items.
        with S() as s:
            before = s.execute(text("SELECT id, shift_id, ref_id, score FROM market_shift_items ORDER BY id")).all()
        r2 = shifts.run(now=now)
        assert r2["new"] == 0 and r2["updated"] == 0 and r2["detected"] == r1["detected"] and r2["items_recomputed"] == 0, r2
        with S() as s:
            assert s.execute(text("SELECT id, shift_id, ref_id, score FROM market_shift_items ORDER BY id")).all() == before
        # A rebuild recomputes the same ranking (deterministic), under new row ids.
        shifts.recompute_items(now=now, force=True)
        with S() as s:
            after = s.execute(text("SELECT shift_id, ref_id, score FROM market_shift_items ORDER BY shift_id, rank")).all()
        assert sorted(after) == sorted((b.shift_id, b.ref_id, b.score) for b in before)

        # API: public read, list + detail + definition, filters, 404 / 422.
        import config
        config.settings.app_password = None
        import main
        from fastapi.testclient import TestClient
        client = TestClient(main.app, headers={"X-OpenGrid-Request": "1"})
        r = client.get("/v1/shifts", params={"gpu": "h100-80gb-sxm5"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["meta"]["heading"].endswith("(not necessarily causal)") and body["meta"]["methodology"] == "/methodology/shifts"
        assert {x["kind"] for x in body["data"]} == {"observed", "archived"}
        top = next(x for x in body["data"] if x["kind"] == "observed")["top_items"]
        assert top and top[0]["item_type"] != "market_event", "the list leads with outside items"
        assert client.get("/v1/shifts", params={"kind": "archived"}).json()["data"][0]["kind"] == "archived"
        assert client.get("/v1/shifts", params={"type": "bogus"}).status_code == 422
        det = client.get(f"/v1/shifts/{med[0]['id']}").json()
        assert det["data"]["heading"].endswith("(not necessarily causal)") and det["data"]["items"]
        assert client.get("/v1/shifts/999999").status_code == 404
        defn = client.get("/v1/shifts/definition").json()["data"]
        assert defn["thresholds"]["median_min"] == 0.10 and defn["thresholds"]["k_times_rms"] == 3.0
        from api import pages
        assert "shifts" in pages.PUBLIC_V1

        # Wording: every OpenGrid-authored string about shifts, and the methodology page, makes no causal claim.
        authored = _authored(body) + _authored(det) + _authored(defn) + [shifts.NOTE, shifts.NOT_CAUSAL]
        # A causal word may appear only negated ("does not mean one explains the other", "never says ... caused").
        bad = [x for x in authored if CAUSAL.search(x) and not re.search(r"\b(not|never|no)\b", x, re.I)]
        assert not bad, bad
        md = (Path(__file__).resolve().parent.parent / "methodology" / "shifts.md").read_text()
        assert "not necessarily causal" in md.lower()
        for line in md.splitlines():
            if CAUSAL.search(line):
                assert re.search(r"\b(never|not|no)\b", line, re.I), f"causal wording in methodology/shifts.md: {line}"
        core = (Path(__file__).resolve().parent.parent / "web" / "core.js").read_text()
        assert 'OG.SHIFT_HEADING = "Related news and events around this shift (not necessarily causal)"' in core
    finally:
        scratchdb.drop(DB)


if __name__ == "__main__":
    for t in (test_episodes_deterministic, test_repricing_archived_capacity_pure, test_db_shifts_items_api):
        t()
        print(t.__name__, "ok")
