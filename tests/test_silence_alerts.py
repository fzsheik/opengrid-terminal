"""Alert on silence (alerts/silence.py): a polled provider that stops producing data -> one ops alert, one recovery.

Covers: start, no repeat, recover, recurrence, 0-listings alert, unconfigured / killed / muted providers, the
thresholds, capacity silence, and restart persistence (the state is in Postgres; a fresh process does not re-send).
A fake clock: every run() gets an explicit `now`; rows are written at times relative to it.

Run:  .venv/bin/python tests/test_silence_alerts.py
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ["OPENGRID_NO_JOBS"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import scratchdb  # noqa: E402  (first: blanks credentials + blocks the network)
import fixtures  # noqa: E402
from sqlalchemy import text  # noqa: E402

import normalize  # noqa: E402
import poller  # noqa: E402
from alerts import notifier, ops, silence  # noqa: E402
from config import settings  # noqa: E402
from tables import ComputeListingRow, RawSnapshot  # noqa: E402

DB = "og_test_silence_alerts"
SENT: list = []
T0 = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
_Session = None


def fake_deliver(chs, body, secret):
    SENT.append(body)
    return ["in_app", "webhook"], {"webhook": "ok"}


def setup():
    global _Session
    if _Session is None:
        _Session = fixtures.session(scratchdb.create(DB))
    normalize.SessionLocal = _Session
    with _Session.begin() as s:
        for t in ("raw_snapshots", "compute_listings", "capacity_current", "ops_alert_state", "quality_incidents",
                  "provider_execution_flags"):
            s.execute(text(f"DELETE FROM {t}"))
    settings.ops_alert_webhook_url = "https://discord.com/api/webhooks/1/ops"
    settings.ops_alert_webhook_secret = "s3cret"
    settings.silence_alerts_enabled, settings.silence_alert_muted = True, ""
    settings.silence_alert_min_hours, settings.silence_alert_polls = 3.0, 6
    settings.silence_alert_empty_grace_minutes = 30
    notifier.deliver = fake_deliver
    poller.ACTIVE.clear()
    silence._WATCH_START.clear()
    SENT.clear()


def fetch(provider, at, ok=True):
    with _Session.begin() as s:
        s.add(RawSnapshot(provider=provider, endpoint="/prices", fetched_at=at, ok=ok,
                          status_code=200 if ok else 500, payload={} if ok else None, error=None if ok else "boom"))


def listings(provider, at, n=3):
    from sqlalchemy.dialects.postgresql import insert
    with _Session.begin() as s:
        for i in range(n):
            row = dict(provider=provider, listing_id=f"{provider}:{i}", sku=f"sku{i}", raw_gpu_name="H100",
                       canonical_gpu_name="NVIDIA H100 80GB SXM5", gpu_count=1, observed_at=at, first_seen_at=at)
            st = insert(ComputeListingRow).values(**row)
            s.execute(st.on_conflict_do_update(index_elements=["provider", "listing_id"],
                                               set_={"observed_at": st.excluded.observed_at}))


def healthy(provider, at):
    fetch(provider, at)
    listings(provider, at)


def kinds():
    return [(b["kind"], b["provider"]) for b in SENT]


def state(kind, provider):
    with _Session() as s:
        return s.execute(text("SELECT status, sent_count, first_at FROM ops_alert_state WHERE kind = :k AND subject = :p"),
                         {"k": kind, "p": provider}).first()


def test_thresholds():
    setup()
    assert silence.threshold(900) == timedelta(hours=3), "15-minute poll: the 3h floor"
    assert silence.threshold(1800) == timedelta(hours=3)
    assert silence.threshold(3600) == timedelta(hours=6), "hourly poll (AWS): 6 polls"
    settings.silence_alert_min_hours = 1
    assert silence.threshold(60) == timedelta(hours=1)


def test_start_no_repeat_recover_and_recur():
    setup()
    poller.ACTIVE.update({"jarvislabs": 900, "lambda": 900})
    healthy("jarvislabs", T0)
    healthy("lambda", T0)
    assert silence.run(T0 + timedelta(minutes=10))["started"] == [] and not SENT, "fresh data: nothing"

    # jarvislabs breaks: every poll fails. lambda keeps working.
    for m in range(15, 200, 15):
        fetch("jarvislabs", T0 + timedelta(minutes=m), ok=False)
        healthy("lambda", T0 + timedelta(minutes=m))
    r = silence.run(T0 + timedelta(hours=2, minutes=50))
    assert ("provider_silent", "jarvislabs") not in kinds(), "under 3h: not silent yet"
    assert ("provider_empty", "jarvislabs") in kinds(), "0 current listings (stale 37.5m + 30m grace) fires first"
    SENT.clear()
    r = silence.run(T0 + timedelta(hours=3, minutes=5))
    assert r["started"] == ["provider_silent:jarvislabs"] and kinds() == [("provider_silent", "jarvislabs")], r
    body = SENT[0]
    assert body["severity"] == "notable" and "no successful price fetch for 3h05m" in body["title"]
    assert body["embeds"][0]["description"] == ops.KINDS["provider_silent"], "Discord embed carries the kind text"
    assert state("provider_silent", "jarvislabs").status == "open"
    poller.ACTIVE.pop("lambda")    # healthy throughout: never alerted; out of the rest of this timeline

    SENT.clear()
    for h in (4, 8, 24, 72):
        silence.run(T0 + timedelta(hours=h))
    assert SENT == [], "still silent: never repeated"
    assert state("provider_silent", "jarvislabs").sent_count == 1

    # it comes back
    now = T0 + timedelta(hours=72, minutes=10)
    healthy("jarvislabs", now - timedelta(minutes=2))
    r = silence.run(now)
    assert sorted(r["recovered"]) == ["provider_empty:jarvislabs", "provider_silent:jarvislabs"], r
    assert {b["kind"] for b in SENT} == {"provider_silent_recovered", "provider_empty_recovered"}
    assert all(b["severity"] == "info" and b["title"].startswith("Recovered: jarvislabs") for b in SENT)
    assert state("provider_silent", "jarvislabs").status == "resolved"
    with _Session() as s:
        open_inc = s.execute(text("SELECT count(*) FROM quality_incidents WHERE provider = 'jarvislabs' "
                                  "AND kind LIKE 'exec_provider_%' AND status = 'open'")).scalar()
    assert open_inc == 0, "recovery closes the /ops incidents too"
    SENT.clear()
    silence.run(now + timedelta(minutes=10))
    assert SENT == [], "recovered once, not again"

    # and breaks again later: a new episode, alerted again
    silence.run(now + timedelta(hours=4))
    assert ("provider_silent", "jarvislabs") in kinds()
    assert state("provider_silent", "jarvislabs").status == "open"


def test_empty_while_fetching():
    """Fetches succeed but parse to nothing (the listings stop being refreshed): the 0-listings alert alone."""
    setup()
    poller.ACTIVE.update({"jarvislabs": 900})
    healthy("jarvislabs", T0)
    for m in range(15, 120, 15):
        fetch("jarvislabs", T0 + timedelta(minutes=m))       # ok, but no listings
    silence.run(T0 + timedelta(minutes=50))
    assert SENT == [], "inside the stale window + grace"
    silence.run(T0 + timedelta(minutes=70))
    assert kinds() == [("provider_empty", "jarvislabs")], kinds()
    assert "0 current listings (had 3)" in SENT[0]["title"]
    SENT.clear()
    silence.run(T0 + timedelta(minutes=110))
    assert SENT == []


def test_never_had_listings_is_not_empty():
    setup()
    poller.ACTIVE.update({"civo": 900})
    for m in range(0, 300, 15):
        fetch("civo", T0 + timedelta(minutes=m))
    silence.run(T0 + timedelta(hours=5))
    assert SENT == [], "a provider that never had listings does not 'drop to 0'"


def test_unconfigured_killed_and_muted_never_alert():
    setup()
    poller.ACTIVE.update({"lambda": 900, "vast": 900})     # jarvislabs: no key, its fetcher was never built
    for p in ("jarvislabs", "lambda", "vast"):
        healthy(p, T0)
    with _Session.begin() as s:
        s.execute(text("INSERT INTO provider_execution_flags (provider, adapter_status, supervised_enabled, "
                       "live_enabled, killed, updated_at) VALUES ('lambda', 'simulated', false, false, true, now())"))
    settings.silence_alert_muted = "vast, other"
    r = silence.run(T0 + timedelta(days=3))
    assert SENT == [] and r["started"] == [], (r, kinds())
    assert r["checked"] == 2, "only polled providers are looked at"

    settings.silence_alert_muted = ""
    silence.run(T0 + timedelta(days=3, minutes=10))
    assert {p for _, p in kinds()} == {"vast"}, "unmuted: it alerts; killed lambda and unpolled jarvislabs stay quiet"

    # poller off (POLLER_ENABLED=false / read-only dev server): nothing is polled, nothing alerts
    setup()
    healthy("lambda", T0)
    assert silence.run(T0 + timedelta(days=3))["checked"] == 0 and SENT == []
    settings.silence_alerts_enabled = False
    poller.ACTIVE.update({"lambda": 900})
    assert silence.run(T0 + timedelta(days=3))["started"] == [] and SENT == []


def test_never_successful_provider_uses_watch_start():
    setup()
    poller.ACTIVE.update({"jarvislabs": 900})
    for m in range(0, 400, 15):
        fetch("jarvislabs", T0 + timedelta(minutes=m), ok=False)
    silence.run(T0)                                   # watch starts here (in memory)
    silence.run(T0 + timedelta(hours=2))
    assert SENT == []
    silence.run(T0 + timedelta(hours=3, minutes=1))
    assert kinds() == [("provider_silent", "jarvislabs")] and "for 3h01m" in SENT[0]["title"]


def test_capacity_silence():
    setup()
    poller.ACTIVE.update({"koyeb": 900})
    healthy("koyeb", T0)
    with _Session.begin() as s:
        s.execute(text("""INSERT INTO capacity_current (provider, sku, region_key, raw_gpu_name, gpu_count, raw_value,
                          source, observed_at, first_seen_at, changed_at)
                          VALUES ('koyeb', 'h100', 'us', 'H100', 1, '5', 'api', :t, :t, :t)"""), {"t": T0})
    for m in range(15, 260, 15):
        healthy("koyeb", T0 + timedelta(minutes=m))   # prices keep arriving, capacity does not
    silence.run(T0 + timedelta(hours=2))
    assert SENT == []
    silence.run(T0 + timedelta(hours=4))
    assert kinds() == [("capacity_silent", "koyeb")], kinds()
    SENT.clear()
    silence.run(T0 + timedelta(hours=5))
    assert SENT == []
    with _Session.begin() as s:
        s.execute(text("UPDATE capacity_current SET observed_at = :t"), {"t": T0 + timedelta(hours=5)})
    silence.run(T0 + timedelta(hours=5, minutes=5))
    assert kinds() == [("capacity_silent_recovered", "koyeb")]


def test_slow_capacity_feed_uses_its_own_cadence():
    """Vast's census runs on a daily row budget (capacity_interval_seconds = 12h): silent only after two gaps."""
    setup()
    poller.ACTIVE.update({"vast": 900})
    healthy("vast", T0)
    with _Session.begin() as s:
        s.execute(text("""INSERT INTO capacity_current (provider, sku, region_key, raw_gpu_name, gpu_count, raw_value,
                          source, observed_at, first_seen_at, changed_at)
                          VALUES ('vast', 'h100', '', 'H100', 1, '5', 'api', :t, :t, :t)"""), {"t": T0})
    for h in range(1, 26):
        healthy("vast", T0 + timedelta(hours=h))
    silence.run(T0 + timedelta(hours=12))
    assert SENT == [], "12h without a census is normal for a quota-limited feed"
    silence.run(T0 + timedelta(hours=24, minutes=1))
    assert kinds() == [("capacity_silent", "vast")], kinds()


def test_restart_persistence():
    """A new process (empty memory) sees the open condition in Postgres: no re-send; recovery still fires once."""
    setup()
    poller.ACTIVE.update({"jarvislabs": 900})
    healthy("jarvislabs", T0)
    silence.run(T0 + timedelta(hours=4))
    assert ("provider_silent", "jarvislabs") in kinds()
    SENT.clear()

    # "restart": a fresh module state and a fresh engine/session factory on the same database
    import importlib
    poller.ACTIVE.clear()
    importlib.reload(silence)
    normalize.SessionLocal = fixtures.session(scratchdb.url(DB))
    poller.ACTIVE.update({"jarvislabs": 900})
    silence.run(T0 + timedelta(hours=5))
    silence.run(T0 + timedelta(hours=6))
    assert SENT == [], "after the restart the open alert is not re-sent"
    healthy("jarvislabs", T0 + timedelta(hours=6, minutes=5))
    r = silence.run(T0 + timedelta(hours=6, minutes=10))
    assert "provider_silent:jarvislabs" in r["recovered"] and ("provider_silent_recovered", "jarvislabs") in kinds()


def test_job_registered():
    import jobs
    assert "silence_alerts" in jobs.JOBS and jobs.JOBS["silence_alerts"].every_seconds == 600


if __name__ == "__main__":
    try:
        for t in (test_thresholds, test_start_no_repeat_recover_and_recur, test_empty_while_fetching,
                  test_never_had_listings_is_not_empty, test_unconfigured_killed_and_muted_never_alert,
                  test_never_successful_provider_uses_watch_start, test_capacity_silence, test_slow_capacity_feed_uses_its_own_cadence, test_restart_persistence,
                  test_job_registered):
            t()
            print(t.__name__, "ok")
    finally:
        scratchdb.drop(DB)
