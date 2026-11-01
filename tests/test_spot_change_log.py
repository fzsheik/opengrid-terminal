"""AWS spot as a change log: DescribeSpotPriceHistory from the last successful poll, every change recorded.

Fetch half (mock EC2, no network): the window starts where the previous successful poll ended, a failure keeps it,
a fresh process resumes from the stored poll (or looks back SPOT_COLD_WINDOW), it never spans more than
SPOT_MAX_WINDOW, and an unchanged market stores the same body.
Database half (scratch database `og_test_spot_log`, dropped after): three polls through fetch.save ->
normalize.refresh; every change between polls becomes its own history row with AWS's exact time, the listing
shows the current price, and re-reading the same log writes nothing twice.

Run:  .venv/bin/python tests/test_spot_change_log.py
"""

import asyncio
import os
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("OPENGRID_NO_JOBS", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import scratchdb  # noqa: E402,F401  (blanks real credentials, refuses real network)
import httpx  # noqa: E402

import providers.aws as aws  # noqa: E402
from models import RawResponse  # noqa: E402
from providers.aws import AwsProvider, ec2_endpoint  # noqa: E402
from storage import content_hash  # noqa: E402

UTC = timezone.utc
ITEM = """<item><instanceType>{t}</instanceType><productDescription>Linux/UNIX</productDescription>
<spotPrice>{p}</spotPrice><timestamp>{ts}</timestamp><availabilityZone>{az}</availabilityZone></item>"""


def _xml(rows) -> str:
    items = "".join(ITEM.format(t=t, p=p, ts=ts, az=az) for t, az, p, ts in rows)
    return ('<DescribeSpotPriceHistoryResponse xmlns="http://ec2.amazonaws.com/doc/2016-11-15/">'
            f"<requestId>r</requestId><spotPriceHistorySet>{items}</spotPriceHistorySet><nextToken/>"
            "</DescribeSpotPriceHistoryResponse>")


class Ec2:
    """Answers DescribeSpotPriceHistory for us-east-1 with `rows`; other regions one fixed row; records forms."""

    def __init__(self):
        self.rows = [("p5.48xlarge", "us-east-1a", "30.00", "2026-10-30T00:00:00.000Z")]
        self.status = 200
        self.forms = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        form = dict(urllib.parse.parse_qsl(request.content.decode()))
        region = request.url.host.split(".")[1]
        if form.get("Action") != "DescribeSpotPriceHistory":
            return httpx.Response(400, text="<Response><Errors><Error><Code>X</Code><Message>m</Message></Error>"
                                            "</Errors></Response>")
        self.forms.append((region, form))
        if region == "us-east-1" and self.status != 200:
            return httpx.Response(self.status, text="<Response><Errors><Error><Code>InternalError</Code>"
                                                    "<Message>boom</Message></Error></Errors></Response>")
        rows = self.rows if region == "us-east-1" else [("g6e.xlarge", region + "a", "1.00", "2026-10-30T00:00:00Z")]
        return httpx.Response(200, text=_xml(rows), headers={"content-type": "text/xml"})

    def provider(self) -> AwsProvider:
        client = httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
        p = AwsProvider(client, "AKIDEXAMPLE", "secret")
        p._last = {"placement": datetime.max.replace(tzinfo=UTC), "capacity_blocks": datetime.max.replace(tzinfo=UTC)}
        return p

    def windows(self, region="us-east-1"):
        return [(f["StartTime"], f["EndTime"]) for r, f in self.forms if r == region]


def _spot(prov, now) -> RawResponse:
    return asyncio.run(prov._spot_prices("us-east-1", now))


def _iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_window_follows_the_last_successful_poll():
    aws.RETRY_DELAY = 0
    m, t0 = Ec2(), datetime(2026, 11, 1, 12, tzinfo=UTC)
    prov = m.provider()
    assert _spot(prov, t0).ok
    assert m.windows()[-1] == (_iso(t0 - aws.SPOT_COLD_WINDOW), _iso(t0)), "cold start: look back an hour"
    t1 = t0 + timedelta(minutes=5)
    assert _spot(prov, t1).ok and m.windows()[-1] == (_iso(t0), _iso(t1)), "next window starts at the last EndTime"
    m.status = 500
    t2 = t1 + timedelta(minutes=5)
    assert not _spot(prov, t2).ok
    m.status = 200
    t3 = t2 + timedelta(minutes=5)
    assert _spot(prov, t3).ok and m.windows()[-1] == (_iso(t1), _iso(t3)), "a failed poll leaves its window open"
    t4 = t3 + timedelta(days=3)
    assert _spot(prov, t4).ok and m.windows()[-1] == (_iso(t4 - aws.SPOT_MAX_WINDOW), _iso(t4)), "capped at a day"


def test_resume_from_the_stored_poll():
    m, t0 = Ec2(), datetime(2026, 11, 1, 12, tzinfo=UTC)
    prov = m.provider()
    stored = t0 - timedelta(minutes=40)
    prov.resume({ec2_endpoint("us-east-1", "DescribeSpotPriceHistory"): stored, "/other": t0})
    assert _spot(prov, t0).ok and m.windows()[-1] == (_iso(stored), _iso(t0))
    # resume never moves a window this process already has
    prov.resume({ec2_endpoint("us-east-1", "DescribeSpotPriceHistory"): stored - timedelta(hours=5)})
    assert prov.spot_window("us-east-1", t0 + timedelta(minutes=5))[0] == t0


def test_unchanged_market_stores_the_same_body():
    m, t0 = Ec2(), datetime(2026, 11, 1, 12, tzinfo=UTC)
    prov = m.provider()
    a, b = _spot(prov, t0), _spot(prov, t0 + timedelta(minutes=5))
    assert a.request["StartTime"] != b.request["StartTime"], "the window lives in the request"
    assert content_hash(a.payload) == content_hash(b.payload), "so an unchanged market dedups to one stored body"
    assert "as_of" not in a.payload


def test_listing_shows_current_price_and_carries_the_rest():
    m, t0 = Ec2(), datetime(2026, 11, 1, 12, tzinfo=UTC)
    m.rows = [("p5.48xlarge", "us-east-1a", "30.00", "2026-11-01T10:00:00.000Z"),
              ("p5.48xlarge", "us-east-1a", "29.00", "2026-11-01T11:10:00.000Z"),
              ("p5.48xlarge", "us-east-1a", "29.50", "2026-11-01T11:40:00.000Z")]
    raw = _spot(m.provider(), t0)
    from types import SimpleNamespace

    snap = SimpleNamespace(endpoint=raw.endpoint, payload=raw.payload, request=raw.request, fetched_at=t0)
    (l,) = aws.spot_listings({raw.endpoint: [snap]})
    assert l.price_per_instance_hour == Decimal("29.50")
    assert l.provider_changed_at == datetime(2026, 11, 1, 11, 40, tzinfo=UTC)
    assert [(e.provider_changed_at.hour, e.provider_changed_at.minute, e.price_per_instance_hour)
            for e in l.superseded] == [(10, 0, Decimal("30.00")), (11, 10, Decimal("29.00"))]
    assert l.superseded[1].price_per_gpu_hour == Decimal("29.00") / 8
    assert "superseded" not in l.model_dump(), "history only, never current state"


def test_pipeline_records_every_change():
    import fetch as fetch_mod
    import fixtures
    import normalize
    from sqlalchemy import select
    from tables import ComputeListingRow, ListingObservation as O

    db = "og_test_spot_log"
    url = scratchdb.create(db)
    saved = fetch_mod.SessionLocal, normalize.SessionLocal
    try:
        Session = fixtures.session(url)
        fetch_mod.SessionLocal = normalize.SessionLocal = Session
        now = datetime.now(UTC).replace(microsecond=0)
        t0, t1, t2 = now - timedelta(minutes=20), now - timedelta(minutes=10), now
        ts = lambda t: t.strftime("%Y-%m-%dT%H:%M:%S.000Z")  # noqa: E731
        a = ("p5.48xlarge", "us-east-1a", "30.00", ts(t0 - timedelta(hours=3)))
        b = ("p5.48xlarge", "us-east-1a", "29.70", ts(t0 + timedelta(minutes=2)))
        c = ("p5.48xlarge", "us-east-1a", "29.90", ts(t0 + timedelta(minutes=6)))
        d = ("p5.48xlarge", "us-east-1a", "30.00", ts(t1 + timedelta(minutes=3)))  # back to A's price
        m = Ec2()
        prov = m.provider()
        lid = "p5.48xlarge:us-east-1a:spot"

        def poll(at, rows):
            m.rows = rows
            raw = _spot(prov, at).model_copy(update={"fetched_at": at})
            assert raw.ok, raw.error
            fetch_mod.save([raw])
            normalize.refresh(["aws"])

        def history():
            with Session() as s:
                return list(s.scalars(select(O).where(O.listing_id == lid).order_by(O.observed_at)))

        poll(t0, [a])
        poll(t1, [a, b, c])           # two changes since t0
        poll(t2, [c, d])              # c is "in force at the start"; d is new
        rows = history()
        got = [(r.price_per_instance_hour, r.provider_changed_at, r.changed_at_basis) for r in rows]
        p = lambda v: Decimal(v)  # noqa: E731
        when = lambda x: datetime.strptime(x[3], "%Y-%m-%dT%H:%M:%S.000Z").replace(tzinfo=UTC)  # noqa: E731
        assert got == [(p("30.00"), when(a), "exact"), (p("29.70"), when(b), "exact"), (p("29.90"), when(c), "exact"),
                       (p("30.00"), when(d), "exact")], got
        assert rows[0].prev_observed_at is None
        assert rows[1].observed_at == t1 - timedelta(microseconds=1) and rows[2].observed_at == t1
        assert rows[1].prev_observed_at == t0 and rows[2].prev_observed_at == t0, "both happened in (t0, t1]"
        assert rows[3].observed_at == t2 and rows[3].prev_observed_at == t1
        with Session() as s:
            cur = s.get(ComputeListingRow, ("aws", lid))
        assert cur.price_per_instance_hour == p("30.00") and cur.observed_at == t2, "the listing shows the current price"
        # Re-reading the same stored log (a refresh with no new fetch) writes nothing.
        normalize.refresh(["aws"])
        assert len(history()) == 4
    finally:
        fetch_mod.SessionLocal, normalize.SessionLocal = saved
        scratchdb.drop(db)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(name, "ok")
    print("all passed")
