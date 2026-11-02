"""Tiered polling: price and capacity cadences, the request budget, rate-limit headers, 429 as throttling.

Unit half (no network, no database): PollingPolicy tiers and cadences, the retuned providers never faster than
their documented limits, the poller's due schedule, the token bucket, header parsing, Provider.call() back-off,
and the providers that split their fetch by cadence (AWS, Nebius, Verda).
Database half (scratch database `og_test_tiered_polling`, dropped after): a throttled fetch is stored as
throttled, counted neither as a failure in provider health nor as silence; the capacity stale flag and the
capacity-silence threshold follow the capacity cadence.

Run:  .venv/bin/python tests/test_tiered_polling.py
"""

import asyncio
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ["OPENGRID_NO_JOBS"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import scratchdb  # noqa: E402  (first: blanks credentials + blocks the network)
import httpx  # noqa: E402

import poller  # noqa: E402
import providers.base as base  # noqa: E402
from models import RawResponse  # noqa: E402
from providers import PROVIDERS  # noqa: E402
from providers.base import CAPACITY, PRICE, PollingPolicy, Provider, RateBudget, rate_limit_wait  # noqa: E402

UTC = timezone.utc


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- policy


def test_policy_tiers_and_cadences():
    p = PollingPolicy(interval_seconds=360, capacity_interval_seconds=120)
    assert p.loop_seconds == 120 and p.capacity_seconds == 120 and p.cadence(PRICE) == 360 and p.tier == "fast"
    slow_cap = PollingPolicy(interval_seconds=300, capacity_interval_seconds=43200)
    assert slow_cap.loop_seconds == 300 and slow_cap.capacity_seconds == 43200 and slow_cap.tier == "medium"
    sub = PollingPolicy(interval_seconds=3600, poll_seconds=300)
    assert sub.loop_seconds == 300 and sub.capacity_seconds == 300 and sub.cadence(PRICE) == 3600
    assert PollingPolicy(interval_seconds=21600).tier == "slow" and PollingPolicy(interval_seconds=900).tier == "medium"
    try:
        PollingPolicy(interval_seconds=60, tier="warp")
    except ValueError:
        pass
    else:
        raise AssertionError("an unknown tier is refused")


def test_retuned_providers_stay_within_documented_limits():
    pol = {n: c.polling for n, c in PROVIDERS.items()}
    # fast tier: generous documented limits
    assert pol["lambda"].interval_seconds == 120 and pol["lambda"].rate_limit == (1, 2.0)   # Lambda: 1 req/s
    assert pol["hyperstack"].interval_seconds == 120 and pol["hyperstack"].rate_limit[0] <= 250  # 500/min per IP
    assert pol["verda"].capacity_interval_seconds == 120 and pol["verda"].interval_seconds == 360
    assert pol["verda"].rate_limit == (30, 60.0)                                             # 60/min per endpoint
    assert pol["vast"].interval_seconds == 300 and pol["vast"].rate_limit == (1, 1.0)        # 1 req/s
    import providers.vast as vast
    assert pol["vast"].capacity_interval_seconds == int(vast.CENSUS_MAX_GAP.total_seconds()), "census budget stays"
    assert vast.CENSUS_DAILY_ROW_BUDGET == 16_000
    # medium: AWS and Nebius spot every 5 minutes, list prices hourly
    assert (pol["aws"].poll_seconds, pol["aws"].interval_seconds) == (300, 3600)
    assert (pol["nebius"].poll_seconds, pol["nebius"].interval_seconds) == (300, 3600)
    assert pol["salad"].interval_seconds == 300 and pol["salad"].rate_limit[0] <= 120          # 240/min
    # slow: list prices that change by announcement poll less, not more
    for n in ("azure", "gcp", "oracle", "ibm"):
        assert pol[n].interval_seconds == 21600 and pol[n].tier == "slow", n
    assert pol["azure"].rate_limit == (5, 60.0)                                                # ~10/min
    for n in ("coreweave", "together", "modal", "replicate", "baseten", "beam"):
        assert pol[n].interval_seconds == 3600 and pol[n].tier == "slow", n
    # Lium stays on its REST poll (the WebSocket is later); undocumented limits are unchanged
    assert pol["lium"].interval_seconds == 900 and pol["denvr"].interval_seconds == 900
    # providers/batch-nokey: public no-key catalogues with undocumented limits stay medium (15 min); Exoscale's
    # price list changes by announcement and polls hourly
    for n in ("akash", "cudo", "gpuai", "huggingface", "hydrahost", "paperspace"):
        assert pol[n].interval_seconds == 900 and pol[n].tier == "medium", n
    assert pol["exoscale"].interval_seconds == 3600 and pol["exoscale"].tier == "slow"
    for n in ("gcore", "fireworks", "ionos", "hetzner", "yotta", "sakura"):   # providers/batch-pages: price pages, hourly
        assert pol[n].interval_seconds == 3600 and pol[n].tier == "slow", n
    for n, p in pol.items():
        assert p.loop_seconds >= 60, f"{n}: never faster than a minute"


def test_instance_policy_without_keys():
    from providers.aws import AwsProvider
    from providers.verda import VerdaProvider

    a = AwsProvider(httpx.AsyncClient())
    assert a.polling.poll_seconds is None and a.polling.loop_seconds == 3600, "no spot feed: hourly"
    assert AwsProvider(httpx.AsyncClient(), "k", "s").polling.loop_seconds == 300
    assert VerdaProvider(httpx.AsyncClient()).polling.loop_seconds == 360, "no capacity without credentials"
    assert VerdaProvider(httpx.AsyncClient(), "id", "secret").polling.loop_seconds == 120


def test_schedule_due():
    sched = poller.Schedule(PollingPolicy(interval_seconds=360, capacity_interval_seconds=120))
    t = 1000.0
    assert sched.due(t) == {PRICE, CAPACITY}, "first poll: everything"
    sched.ran({PRICE, CAPACITY}, t)
    assert sched.due(t + 120) == {CAPACITY}
    sched.ran({CAPACITY}, t + 120)
    assert sched.due(t + 240) == {CAPACITY}
    sched.ran({CAPACITY}, t + 240)
    assert sched.due(t + 360) == {PRICE, CAPACITY}, "price every third loop"
    sub = poller.Schedule(PollingPolicy(interval_seconds=3600, poll_seconds=300))
    sub.ran({PRICE, CAPACITY}, t)
    assert sub.due(t + 300) == {CAPACITY}, "a sub-feed loop: capacity rides every loop, the list price does not"


# --------------------------------------------------------------------------- budget and headers


def test_token_bucket():
    b = RateBudget(2, 1.0)
    assert b.wait_for() == 0 and b.wait_for() == 0
    w = b.wait_for()
    assert 0.4 < w <= 0.5, w


def test_rate_limit_headers():
    now = 1_800_000_000.0
    assert rate_limit_wait({"Retry-After": "7"}, now) == 7
    assert rate_limit_wait({"Retry-After": "Fri, 15 Jan 2027 08:00:30 GMT"},
                           datetime(2027, 1, 15, 8, 0, tzinfo=UTC).timestamp()) == 30
    assert rate_limit_wait({"RateLimit-Remaining": "0", "RateLimit-Reset": "12"}, now) == 12          # RunPod/Verda
    assert rate_limit_wait({"ratelimit-remaining": "0", "ratelimit-reset": str(now + 40)}, now) == 40  # DO epoch
    assert rate_limit_wait({"X-RateLimit-Remaining": "5", "X-RateLimit-Reset": "30"}, now) is None
    assert rate_limit_wait({"RateLimit": "limit=100, remaining=0, reset=9"}, now) == 9
    assert rate_limit_wait({"x-ms-ratelimit-remaining-retailprices": "0"}, now) == 60
    assert rate_limit_wait({}, now) is None and rate_limit_wait({"Retry-After": "soon"}, now) is None
    # Linode: Retry-After on every 200 is its window length, not a request to wait; on a 429 it is.
    linode = {"X-RateLimit-Limit": "1840", "X-RateLimit-Remaining": "1839", "X-RateLimit-Reset": str(now + 60),
              "Retry-After": "60"}
    assert rate_limit_wait(linode, now, status=200) is None
    assert rate_limit_wait(linode, now, status=429) == 60 and rate_limit_wait(linode, now, status=503) == 60


class Toy(Provider):
    name = "toy"
    polling = PollingPolicy(interval_seconds=120, rate_limit=(100, 1.0))

    async def fetch(self):
        return [await self.get("/a")]

    @staticmethod
    def normalize(by_endpoint):
        return []


def _toy(responses):
    seq = iter(responses)
    calls = []

    def handler(request):
        calls.append(request.url.path)
        status, headers = next(seq)
        return httpx.Response(status, json={"x": 1}, headers=headers)

    return Toy(httpx.AsyncClient(base_url="https://toy", transport=httpx.MockTransport(handler))), calls


def test_429_is_throttled_and_backs_off():
    t, calls = _toy([(429, {"Retry-After": "120"}), (200, {})])
    r = run(t.get("/a"))
    assert r.status_code == 429 and not r.ok and r.throttled
    assert t.blocked_until - time.time() > 100
    skipped = run(t.get("/b"))
    assert skipped.throttled and not skipped.ok and skipped.status_code is None and "not called" in skipped.error
    assert calls == ["/a"], "a long requested wait: the next call is not made"


def test_429_without_header_backs_off_briefly_and_resets():
    t, calls = _toy([(429, {}), (200, {}), (429, {}), (429, {})])
    r1 = run(t.get("/a"))
    assert r1.throttled and 1.5 < t.blocked_until - time.time() <= 2.0, "first back-off 2 s"
    r2 = run(t.get("/a"))  # waits ~2 s inline, then goes ahead
    assert r2.ok and not r2.throttled and t._backoff == 0
    run(t.get("/a"))
    t.blocked_until = 0
    run(t.get("/a"))
    assert t._backoff == 4.0, "doubles on a consecutive 429"


def test_backoff_false_records_without_pausing():
    t, calls = _toy([(429, {"Retry-After": "600"}), (200, {})])
    r = run(t.call("GET", "/census", backoff=False))
    assert r.throttled and t.blocked_until == 0.0
    assert run(t.get("/prices")).ok, "the provider's other calls go on"


def test_success_with_zero_remaining_paces_next_call():
    t, _ = _toy([(200, {"RateLimit-Remaining": "0", "RateLimit-Reset": "300"})])
    r = run(t.get("/a"))
    assert r.ok and not r.throttled and t.blocked_until - time.time() > 250


def test_get_page_throttle():
    class Page(Toy):
        name = "page"

    def handler(request):
        return httpx.Response(429, text="slow down", headers={"Retry-After": "90"})

    p = Page(httpx.AsyncClient(base_url="https://page", transport=httpx.MockTransport(handler)))
    r = run(base.get_page(p, "/pricing", lambda html: {"x": 1}))
    assert r.throttled and not r.ok and r.status_code == 429 and p.blocked_until > time.time() + 60


# --------------------------------------------------------------------------- split fetches


def test_split_fetches_follow_due():
    from providers.nebius import ENDPOINT, SPOT_ENDPOINT, NebiusProvider
    from providers.verda import VerdaProvider

    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(500, json={})

    neb = NebiusProvider(httpx.AsyncClient(base_url="https://nebius.com", transport=httpx.MockTransport(handler)),
                         httpx.AsyncClient(base_url="https://api", transport=httpx.MockTransport(handler)))
    neb.due = frozenset()
    out = run(neb.fetch())
    assert [r.endpoint for r in out] == [SPOT_ENDPOINT] and ENDPOINT not in seen, "spot only between price polls"
    neb.due = frozenset({PRICE})
    assert [r.endpoint for r in run(neb.fetch())] == [ENDPOINT, SPOT_ENDPOINT]

    seen.clear()
    v = VerdaProvider(httpx.AsyncClient(base_url="https://api.verda.com/v1", transport=httpx.MockTransport(handler)),
                      "id", "secret")
    v.due = frozenset({CAPACITY})
    run(v.fetch())
    assert seen and all("instance-types" not in p for p in seen), seen
    seen.clear()
    v.due = frozenset({PRICE})
    run(v.fetch())
    assert seen and not any("availability" in p or "token" in p for p in seen), seen


def test_aws_price_file_on_price_cadence_only():
    from providers.aws import ENDPOINT, AwsProvider

    hosts = []

    def handler(request):
        hosts.append(request.url.host)
        return httpx.Response(500, text="x")

    a = AwsProvider(httpx.AsyncClient(base_url="https://b0.p.awsstatic.com", transport=httpx.MockTransport(handler)))
    a.due = frozenset({CAPACITY})
    assert run(a.fetch()) == [] and hosts == []
    a.due = frozenset({PRICE})
    assert {r.endpoint for r in run(a.fetch())} >= {ENDPOINT}


# --------------------------------------------------------------------------- database: health, silence, stale


def test_throttled_is_neither_failure_nor_silence():
    import fetch as fetch_mod
    import fixtures
    import normalize
    from sqlalchemy import text
    from alerts import silence
    from config import settings
    from quality import trust

    db = "og_test_tiered_polling"
    url = scratchdb.create(db)
    saved = fetch_mod.SessionLocal, normalize.SessionLocal
    try:
        Session = fixtures.session(url)
        fetch_mod.SessionLocal = normalize.SessionLocal = Session
        now = datetime.now(UTC).replace(microsecond=0)
        ok = RawResponse(provider="salad", endpoint="/x", fetched_at=now - timedelta(hours=5), status_code=200,
                         payload={"a": 1})
        thr = [RawResponse(provider="salad", endpoint="/x", fetched_at=now - timedelta(minutes=m), status_code=429,
                           ok=False, throttled=True, error="too many") for m in (30, 20, 10)]
        bad = RawResponse(provider="salad", endpoint="/x", fetched_at=now - timedelta(hours=6), status_code=500,
                          ok=False, error="boom")
        fetch_mod.save([ok, bad, *thr])
        with Session() as s:
            assert s.execute(text("SELECT count(*) FROM raw_snapshots WHERE throttled")).scalar() == 3
        h = trust.provider_health("salad")
        assert h["failures_24h"] == 1 and h["throttled_24h"] == 3, h
        assert h["consecutive_failures"] == 0, "throttled rows after the last good one are not failures"
        assert h["last_failure"]["status_code"] == 500

        settings.silence_alert_min_hours, settings.silence_alert_polls = 3.0, 6
        poller.ACTIVE.clear()
        poller.ACTIVE["salad"] = 300.0
        state = silence.evaluate("salad", now)
        assert state["provider_silent"][0] is False, "throttled for 30 min after 5 h: being paced is not silence"
        poller.ACTIVE.clear()

        # capacity cadence: Vast (12 h census) is not stale after 13 h, a fast-capacity provider is
        from api import capacity as cap_api
        from store.capacity import CapacityCurrent
        row = lambda p, age: CapacityCurrent(  # noqa: E731
            provider=p, sku="s", region_key="", region=None, country=None, raw_gpu_name="H100",
            canonical_gpu_name=None, gpu_count=1, in_stock=True, tier=None, available_count=None, total_count=None,
            count_unit=None, raw_value="1", source="x", observed_at=now - age, first_seen_at=now, changed_at=now)
        assert cap_api._row(row("vast", timedelta(hours=13)), now)["stale"] is False
        assert cap_api._row(row("verda", timedelta(minutes=7)), now)["stale"] is True, "3 x 120 s"
        assert cap_api._row(row("verda", timedelta(minutes=5)), now)["stale"] is False
        assert silence._capacity_limit("vast", timedelta(hours=3)) == timedelta(hours=24)
        assert silence._capacity_limit("verda", timedelta(hours=3)) == timedelta(hours=3)
    finally:
        fetch_mod.SessionLocal, normalize.SessionLocal = saved
        scratchdb.drop(db)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(name, "ok")
    print("all passed")
