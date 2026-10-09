"""Channel feeds (alerts/channels.py): deployments, market and news webhooks.

Run:  .venv/bin/python tests/test_channels.py
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy import text  # noqa: E402

import fixtures  # noqa: E402
import normalize  # noqa: E402
import scratchdb  # noqa: E402
from alerts import channels, notifier  # noqa: E402
from config import settings  # noqa: E402

DB = "og_test_channels"
SENT: list = []
FAIL = {"n": 0}


def fake_deliver(chs, body, secret):
    if FAIL["n"] > 0:
        FAIL["n"] -= 1
        return ["in_app"], {"webhook": "delivery_failed"}
    SENT.append((chs[0]["url"], body))
    return ["in_app", "webhook"], {"webhook": "ok"}


def setup():
    url = scratchdb.create(DB)
    S = fixtures.session(url)
    normalize.SessionLocal = S
    for name in ("deployments_webhook_url", "market_webhook_url", "news_webhook_url"):
        setattr(settings, name, f"https://discord.com/api/webhooks/1/{name}")
    settings.ops_alert_webhook_url = "https://discord.com/api/webhooks/1/ops"
    settings.ops_alert_webhook_secret = "s3cret"
    notifier.deliver = fake_deliver
    return S


def market_event(s, i, at, severity="notable", type_="price_move"):
    s.execute(text("""INSERT INTO market_events (occurred_at, detected_at, type, severity, title, gpu, provider,
                      value_before, value_after, pct, dedupe_key)
                      VALUES (:at, :at, :t, :sev, :title, 'NVIDIA H100 80GB SXM5', 'lium', 1.48, 1.30, -0.12, :k)"""),
              {"at": at, "t": type_, "sev": severity, "title": f"event {i}", "k": f"k{i}"})


def test_market_feed_once_in_order_with_retry():
    S = setup()
    try:
        now = datetime.now(timezone.utc)
        with S.begin() as s:
            market_event(s, 0, now - timedelta(hours=2))   # history before the channel existed
        r = channels.run_channel("market", now=now)
        assert r["skipped"] == "watermark initialised" and not SENT, "first run never floods history"
        with S.begin() as s:
            market_event(s, 1, now + timedelta(seconds=1))
            market_event(s, 2, now + timedelta(seconds=2), severity="info")            # below threshold
            market_event(s, 3, now + timedelta(seconds=3), type_="coverage_started")   # not a market event
            market_event(s, 4, now + timedelta(seconds=4), severity="major")
        FAIL["n"] = 1                       # first send fails: nothing posted, retried next run
        r = channels.run_channel("market")
        assert r["posted"] == 0 and r["failed"] == 1 and not SENT
        r = channels.run_channel("market")
        titles = [b["embeds"][0]["title"] for _, b in SENT]
        assert r["posted"] == 2 and titles[0].endswith("event 1") and titles[1].endswith("event 4"), titles
        assert channels.run_channel("market")["posted"] == 0, "never posted twice"
        e = SENT[0][1]["embeds"][0]
        assert e["color"] == 0x91C61D and SENT[0][1]["username"] == "OpenGrid"
        assert SENT[0][1]["allowed_mentions"] == {"parse": []}
        assert "$1.48 → $1.30 (-12.0%)" in [f["value"] for f in e["fields"]]
    finally:
        SENT.clear()
        scratchdb.drop(DB)


def test_news_feed_relevance_and_dates():
    S = setup()
    try:
        now = datetime.now(timezone.utc)
        channels.run_channel("news", now=now)   # watermark
        with S.begin() as s:
            s.execute(text("INSERT INTO news_sources (id, name, url, kind, category, trust_tier, poll_seconds, "
                           "topics, entity_hints, enabled, consecutive_failures) VALUES ('src', 'Test Source', "
                           "'https://x.example/feed', 'rss', 'trade_press', 'press', 3600, '[]', '{}', true, 0) "
                           "ON CONFLICT DO NOTHING"))
            rows = [("high", 80, now, False), ("low", 20, now, False),
                    ("stale-stamp", 90, now - timedelta(days=1), True),   # unreliable feed date
                    ("old", 90, now - timedelta(days=10), False)]          # archive item seen today
            for i, (title, rel, pub, inferred) in enumerate(rows):
                s.execute(text("""INSERT INTO news_items (source_id, url, canonical_url, url_hash, title, published_at,
                                  published_at_inferred, first_seen_at, relevance, relevance_components, topics,
                                  entities) VALUES ('src', :u, :u, :h, :t, :p, :inf, :seen, :r, '{}', '[]', '{}')"""),
                          {"u": f"https://x.example/{i}", "h": f"h{i}", "t": title, "p": pub, "inf": inferred,
                           "seen": now + timedelta(seconds=i + 1), "r": rel})
        r = channels.run_channel("news")
        titles = [b["embeds"][0]["title"] for _, b in SENT]
        assert r["posted"] == 1 and titles == ["📰 high"], titles
        assert SENT[0][1]["embeds"][0]["url"] == "https://x.example/0"
    finally:
        SENT.clear()
        scratchdb.drop(DB)


def test_builders_respect_discord_limits():
    long = "x" * 5000
    now = datetime.now(timezone.utc)
    for payload in (channels.deployment_message({"deployment_id": "dep-1", "to_status": "running", "at": now,
                                                 "reason": long, "provider": "lambda", "gpu": long}),
                    channels.market_message({"type": "price_move", "severity": "major", "title": long}),
                    channels.news_message({"title": long, "summary": long, "url": "https://x.example"})):
        e = payload["embeds"][0]
        assert len(e["title"]) <= 256 and len(e.get("description", "")) <= 4096
        assert all(len(f["value"]) <= 1024 for f in e["fields"]) and e["color"] == 0x91C61D
    tests = channels.test_messages()
    assert set(tests) == {"ops", "deployments", "market", "news"}
    assert len(channels.plain_text(tests["market"])) <= 1900


if __name__ == "__main__":
    for t in (test_builders_respect_discord_limits, test_market_feed_once_in_order_with_retry,
              test_news_feed_relevance_and_dates):
        t(); print(t.__name__, "ok")
