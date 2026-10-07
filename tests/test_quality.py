"""Data quality: rules, quarantine, malformed responses, schema watch, trust, _latest_raw, ops API.

Run:  .venv/Scripts/python tests/test_quality.py

Everything runs in a scratch database (og_test_quality); every provider and price is synthetic.
"""

import os
import random
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

os.environ.setdefault("OPENGRID_NO_JOBS", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy import select, text  # noqa: E402

import fixtures  # noqa: E402
import normalize  # noqa: E402
import scratchdb  # noqa: E402
from models import ComputeListing  # noqa: E402
from providers import PROVIDERS, PollingPolicy  # noqa: E402
from quality import checks, quarantine, schema_watch, trust  # noqa: E402
from tables import ComputeListingRow, ListingObservation, RawSnapshot  # noqa: E402
from store.quality import Incident, Quarantine  # noqa: E402

DB = "og_test_quality"
NOW = datetime.now(timezone.utc).replace(microsecond=0)
FAKE = "qa_fake"


# --------------------------------------------------------------------------
# A fake provider whose normalizer reads {"items": [...]} from its raw snapshot
# --------------------------------------------------------------------------


class FakeProvider:
    name = FAKE
    polling = PollingPolicy(interval_seconds=900)

    @staticmethod
    def normalize(by_endpoint):
        rows = [r for rs in by_endpoint.values() for r in rs]
        if not rows:
            return []
        snap = rows[0]
        if "boom" in snap.payload:
            raise KeyError("items")  # the response changed shape under the normalizer
        out = []
        for it in snap.payload["items"]:
            price = None if it.get("price") is None else D(str(it["price"]))
            count = it.get("count", 1)
            out.append(ComputeListing(
                provider=FAKE, sku=it["id"], listing_id=it["id"], raw_gpu_name=it.get("raw", "X"),
                canonical_gpu_name=it.get("gpu"), gpu_count=count, region=it.get("region", "us-1"),
                country=it.get("country", "US"), price_per_gpu_hour=price,
                price_per_instance_hour=None if price is None else price * count,
                market_type="on_demand", provider_tier=None, interruptible=False,
                available=it.get("avail", True), capacity=it.get("cap"), capacity_unit="gpu" if it.get("cap") is not None else None,
                vcpu=None, ram_gb=None, storage_gb=None, observed_at=snap.fetched_at,
            ))
        return out


PROVIDERS[FAKE] = FakeProvider


def item(i, price, **kw):
    return {"id": f"L{i}", "price": price, **kw}


class World:
    """A scratch DB plus a poll clock for the fake provider."""

    def __init__(self, Session):
        self.Session = Session
        self.t = NOW - timedelta(hours=12)

    def poll(self, items=None, payload=None, minutes=15):
        self.t += timedelta(minutes=minutes)
        with self.Session.begin() as s:
            s.add(RawSnapshot(provider=FAKE, endpoint="/items", fetched_at=self.t, ok=True, status_code=200,
                              payload=payload if payload is not None else {"items": items}))
        return normalize.refresh([FAKE])

    def listing(self, lid):
        with self.Session() as s:
            return s.get(ComputeListingRow, (FAKE, lid))

    def observations(self, lid):
        with self.Session() as s:
            return s.execute(select(ListingObservation).where(ListingObservation.provider == FAKE,
                                                              ListingObservation.listing_id == lid)
                             .order_by(ListingObservation.observed_at)).scalars().all()

    def holds(self, lid=None, status=None):
        with self.Session() as s:
            q = select(Quarantine).where(Quarantine.provider == FAKE).order_by(Quarantine.id)
            if lid:
                q = q.where(Quarantine.listing_id == lid)
            if status:
                q = q.where(Quarantine.status == status)
            return s.execute(q).scalars().all()

    def incidents(self, kind, status=None):
        with self.Session() as s:
            q = select(Incident).where(Incident.kind == kind)
            if status:
                q = q.where(Incident.status == status)
            return s.execute(q).scalars().all()


def fresh_world():
    url = scratchdb.create(DB)
    Session = fixtures.session(url)
    normalize.SessionLocal = Session
    trust.all_provider_health.cache_clear()
    return World(Session)


def base_items(n=8, **over):
    items = [item(i, 2.0 + i * 0.1) for i in range(n)]
    for i, kw in over.items():
        items[int(i)] = {**items[int(i)], **kw}
    return items


# --------------------------------------------------------------------------
# Rules, no database
# --------------------------------------------------------------------------


def L(**kw):
    d = dict(provider="p", sku="s", listing_id="l", raw_gpu_name="X", canonical_gpu_name=None, gpu_count=1,
             region="us", country="US", price_per_gpu_hour=D("2.50"), price_per_instance_hour=D("2.50"),
             market_type="on_demand", provider_tier=None, interruptible=False, available=True, capacity=4,
             capacity_unit="gpu", vcpu=None, ram_gb=None, storage_gb=None, observed_at=NOW)
    d.update(kw)
    return ComputeListing(**d)


PREV = {"price_per_gpu_hour": D("2.50"), "price_per_instance_hour": D("2.50"), "gpu_count": 1,
        "region": "us", "country": "US", "capacity": 4, "canonical_gpu_name": None}


def rules(listing, prev=PREV):
    return sorted(f.rule for f in checks.check_listing(listing, prev))


def test_rules():
    # Normal data: nothing fires, new or changed by a normal amount.
    assert rules(L()) == []
    assert rules(L(), None) == []
    assert rules(L(price_per_gpu_hour=D("3.10"), price_per_instance_hour=D("3.10"))) == []
    assert rules(L(capacity=40)) == []                     # 10x but only +36
    assert rules(L(region="eu")) == []                      # a region change is not a disappearance
    # The $2.50 -> $250 unit error: a jump, and above the ceiling, so it never auto-accepts.
    fs = checks.check_listing(L(price_per_gpu_hour=D("250"), price_per_instance_hour=D("250")), PREV)
    assert sorted(f.rule for f in fs) == ["price_above_ceiling", "price_jump"]
    assert not next(f for f in fs if f.rule == "price_jump").auto_acceptable
    # A 5x jump to a plausible price may auto-accept once it persists.
    f = checks.check_listing(L(price_per_gpu_hour=D("12.50"), price_per_instance_hour=D("12.50")), PREV)[0]
    assert f.rule == "price_jump" and f.auto_acceptable
    assert rules(L(price_per_gpu_hour=D("0.50"), price_per_instance_hour=D("0.50"))) == ["price_jump"]  # 0.2x
    assert rules(L(price_per_gpu_hour=D("0.51"), price_per_instance_hour=D("0.51"))) == []
    assert rules(L(price_per_gpu_hour=D("0"), price_per_instance_hour=D("0"))) == ["price_nonpositive"]
    assert rules(L(price_per_gpu_hour=D("-1"), price_per_instance_hour=D("-1")), None) == ["price_nonpositive"]
    # Datacenter floor, on a new listing too.
    h100 = "NVIDIA H100 80GB SXM5"
    assert rules(L(canonical_gpu_name=h100, raw_gpu_name="H100 SXM", price_per_gpu_hour=D("0.05"),
                   price_per_instance_hour=D("0.05")), None) == ["price_below_floor"]
    assert rules(L(canonical_gpu_name=h100, raw_gpu_name="H100 SXM", price_per_gpu_hour=D("1.20"),
                   price_per_instance_hour=D("1.20")), None) == []
    # An unchanged price is never re-judged (nothing new to hold).
    low = {**PREV, "price_per_gpu_hour": D("0.05"), "price_per_instance_hour": D("0.05"), "canonical_gpu_name": h100}
    assert rules(L(canonical_gpu_name=h100, raw_gpu_name="H100", price_per_gpu_hour=D("0.05"),
                   price_per_instance_hour=D("0.05")), low) == []
    # gpu_count
    assert rules(L(gpu_count=0, price_per_instance_hour=D("2.50"))) == ["gpu_count_invalid", ]
    assert rules(L(gpu_count=8, price_per_gpu_hour=D("2.50"), price_per_instance_hour=D("20.00"))) == ["gpu_count_changed"]
    # Region disappears; capacity spike.
    assert rules(L(region=None, country=None)) == ["region_disappeared"]
    assert rules(L(capacity=400)) == ["capacity_spike"]
    assert rules(L(capacity=400), {**PREV, "capacity": 0}) == []  # capacity returning from zero is normal
    # Impossible mapping: 40GB raw name mapped to an 80GB canonical; the 268 vs 288 reporting gap is fine.
    assert rules(L(raw_gpu_name="NVIDIA A100-SXM4-40GB", canonical_gpu_name="NVIDIA A100 80GB SXM4")) == ["vram_conflict"]
    assert rules(L(raw_gpu_name="B300 SXM6 268GB", canonical_gpu_name="NVIDIA B300 288GB SXM")) == []
    assert rules(L(raw_gpu_name="A100_80G sxm", canonical_gpu_name="NVIDIA A100 80GB SXM4")) == []
    assert rules(L(raw_gpu_name="A100 40GB/80GB", canonical_gpu_name="NVIDIA A100 80GB SXM4")) == []  # ambiguous
    # Instance price inconsistent with per-GPU price x count: a flag, not a hold.
    fs = checks.check_listing(L(gpu_count=2, price_per_instance_hour=D("2.50")), {**PREV, "gpu_count": 2})
    assert [(f.rule, f.action) for f in fs] == [("instance_price_mismatch", "flag")]
    # Provider-level counts.
    assert checks.norm_of([10, 10]) is None and checks.norm_of([10, 12, 11]) == 11
    assert [f.rule for f in checks.check_count(50, 10, 10)] == ["duplicate_explosion"]
    assert checks.check_count(25, 10, 10) == []           # 2.5x is not an explosion
    assert checks.check_count(31, 10, 10)[0].rule == "duplicate_explosion"
    assert checks.check_count(40, 25, 25) == []           # 1.6x
    assert [f.rule for f in checks.check_count(0, 10, 10)] == ["empty_response"]
    assert checks.check_count(0, None, 2) == []           # tiny providers may legitimately empty out
    assert [f.rule for f in checks.check_count(1, 20, 20)] == ["listing_collapse"]
    assert checks.check_count(12, 20, 20) == []


# --------------------------------------------------------------------------
# Quarantine flow through normalize.refresh
# --------------------------------------------------------------------------


def test_quarantine_flow():
    w = fresh_world()
    try:
        items = base_items()
        for _ in range(3):
            w.poll(items)
        assert w.listing("L0").price_per_gpu_hour == D("2.0") and not w.holds()
        assert len(w.observations("L0")) == 1

        # --- Unit error: 2.00 -> 250. Held; the listing keeps 2.00; no observation written.
        bad = base_items(**{"0": {"price": 250}})
        w.poll(bad)
        t_first = w.t
        row = w.listing("L0")
        assert row.price_per_gpu_hour == D("2.0") and row.observed_at == w.t, "held price, presence refreshed"
        assert len(w.observations("L0")) == 1
        (q,) = w.holds("L0")
        assert q.status == "pending" and q.rule == "price_jump" and not q.auto_acceptable
        assert q.new_value == 250 and q.previous_value == 2.0 and q.seen_count == 1
        assert set(q.detail["rules"]) == {"price_jump", "price_above_ceiling"}
        # Re-normalizing the same raw data does not count as another sighting.
        normalize.refresh([FAKE])
        assert w.holds("L0")[0].seen_count == 1
        # It persists, but 250/GPU-h is implausible: never auto-accepted.
        for _ in range(6):
            w.poll(bad)
        (q,) = w.holds("L0")
        assert q.status == "pending" and q.seen_count == 7
        # After the grace window the held listing is no longer refreshed: it ages out, never lies.
        row = w.listing("L0")
        assert row.price_per_gpu_hour == D("2.0")
        assert t_first < row.observed_at < w.t, (row.observed_at, w.t)
        assert row.observed_at - t_first <= quarantine.grace(FAKE)
        # The other listings carried on normally all along.
        assert w.listing("L1").observed_at == w.t

        # --- Operator accepts: applied now, with an observation.
        out = quarantine.resolve(q.id, "accept", "operator", "confirmed with provider")
        assert out["status"] == "accepted" and out["applied"]
        row = w.listing("L0")
        assert row.price_per_gpu_hour == D("250") and row.price_per_instance_hour == D("250")
        assert row.observed_at == w.t
        obs = w.observations("L0")
        assert len(obs) == 2 and obs[-1].price_per_gpu_hour == D("250") and obs[-1].observed_at == w.t
        # The accepted value is remembered: the next poll applies it and opens nothing new.
        w.poll(bad)
        assert [h.status for h in w.holds("L0")] == ["accepted"]
        assert w.listing("L0").observed_at == w.t
        try:
            quarantine.resolve(q.id, "reject", "operator")
            raise AssertionError("resolving twice must fail")
        except quarantine.NotPending:
            pass

        # --- Auto-accept: a plausible 6x move that persists K polls over >= min_span.
        moved = base_items(**{"0": {"price": 250}, "1": {"price": 12.6}})
        w.poll(moved)
        start = w.t
        for _ in range(quarantine.K_POLLS - 2):
            w.poll(moved)
            assert w.listing("L1").price_per_gpu_hour == D("2.1")
        assert w.holds("L1")[0].status == "pending"
        w.poll(moved)  # K-th sighting, span 45 min
        (q1,) = w.holds("L1")
        assert q1.status == "auto_accepted" and q1.resolved_by == "system:auto" and q1.seen_count == quarantine.K_POLLS
        assert w.t - start >= quarantine.min_span(FAKE)
        assert w.listing("L1").price_per_gpu_hour == D("12.6")
        assert w.observations("L1")[-1].observed_at == w.t, "history starts when it was applied"

        # --- Revert: a jump that goes away is superseded, never applied.
        blip = [dict(x) for x in moved]
        blip[2]["price"] = 40
        w.poll(blip)
        assert w.holds("L2")[0].status == "pending"
        w.poll(moved)
        assert w.holds("L2")[0].status == "superseded"
        assert w.listing("L2").price_per_gpu_hour == D("2.2")
        assert all(o.price_per_gpu_hour != D("40") for o in w.observations("L2"))

        # --- Reject: zero price. It stays held while the provider keeps sending it, and the
        # listing is withheld (ages out) rather than shown with an old price.
        zero = [dict(x) for x in moved]
        zero[3]["price"] = 0
        w.poll(zero)
        (q3,) = w.holds("L3")
        assert q3.rule == "price_nonpositive" and w.listing("L3").price_per_gpu_hour == D("2.3")
        quarantine.resolve(q3.id, "reject", "operator")
        seen_before = w.listing("L3").observed_at
        w.poll(zero)
        assert w.listing("L3").observed_at == seen_before, "rejected value: listing not refreshed"
        assert w.listing("L3").price_per_gpu_hour == D("2.3")
        assert [h.status for h in w.holds("L3")] == ["rejected"] and w.holds("L3")[0].seen_count == 2
        # The provider fixes it: normal again, nothing held.
        w.poll(moved)
        assert w.listing("L3").observed_at == w.t and w.listing("L3").price_per_gpu_hour == D("2.3")

        # --- Region disappears: held at the old region, then auto-accepted once persistent.
        noreg = [dict(x) for x in moved]
        noreg[4]["region"] = None
        noreg[4]["country"] = None
        w.poll(noreg)
        assert w.listing("L4").region == "us-1"
        assert w.holds("L4")[0].rule == "region_disappeared"

        # --- A conflicting mapping is withheld (canonical None), never guessed.
        vram = [dict(x) for x in noreg]
        vram[5].update(raw="NVIDIA A100-SXM4-40GB", gpu="NVIDIA A100 80GB SXM4")
        w.poll(vram)
        assert w.listing("L5").canonical_gpu_name is None
        assert w.holds("L5")[0].rule == "vram_conflict"

        # --- Flags: instance price inconsistent -> incident, value untouched; clears when fixed.
        quarantine_rows = len(w.holds())
        orig = FakeProvider.normalize

        def skewed(by_endpoint):
            return [l.model_copy(update={"price_per_instance_hour": D("99")}) if l.listing_id == "L6" else l
                    for l in orig(by_endpoint)]

        FakeProvider.normalize = staticmethod(skewed)
        try:
            w.poll(vram)
        finally:
            FakeProvider.normalize = staticmethod(orig)
        assert w.listing("L6").price_per_instance_hour == D("99")
        assert len(w.incidents("instance_price_mismatch", "open")) == 1 and len(w.holds()) == quarantine_rows
        w.poll(vram)
        assert len(w.incidents("instance_price_mismatch", "open")) == 0
    finally:
        scratchdb.drop(DB)


def test_malformed_and_explosion():
    w = fresh_world()
    try:
        items = base_items(10)
        for _ in range(4):
            w.poll(items)
        seen = w.listing("L0").observed_at

        # Normalizer raises: nothing saved, state kept, incident recorded, other providers unaffected.
        r = w.poll(payload={"boom": True})
        assert r["listings"] == 0
        assert w.listing("L0").observed_at == seen and w.listing("L0").price_per_gpu_hour == D("2.0")
        assert len(w.incidents("normalizer_error", "open")) == 1
        assert not w.incidents("empty_response"), "a raising normalizer is one incident, not two"

        # Empty result when the provider normally has 10: an incident, previous state kept.
        quarantine._normalizer_failed_at.clear()
        w.poll([])
        assert len(w.incidents("empty_response", "open")) == 1
        with w.Session() as s:
            assert s.execute(text("SELECT count(*) FROM compute_listings WHERE provider = :p"), {"p": FAKE}).scalar() == 10
        assert w.listing("L0").observed_at == seen

        # Back to normal: both incidents resolve.
        w.poll(items)
        assert not w.incidents("empty_response", "open") and not w.incidents("normalizer_error", "open")
        assert w.listing("L0").observed_at == w.t

        # Duplicate explosion: 10 -> 60. Existing listings update; the 50 new ids are held back.
        big = items + [item(100 + i, 1.5) for i in range(50)]
        w.poll(big)
        with w.Session() as s:
            n = s.execute(text("SELECT count(*) FROM compute_listings WHERE provider = :p"), {"p": FAKE}).scalar()
        assert n == 10, n
        (q,) = w.holds("*")
        assert q.rule == "duplicate_explosion" and q.field == "listing_count" and q.new_value == 60
        assert w.listing("L0").observed_at == w.t
        # It persists: auto-accepted after K sightings, then the new listings flow in.
        for _ in range(quarantine.K_POLLS - 1):
            w.poll(big)
        assert w.holds("*")[0].status == "auto_accepted"
        with w.Session() as s:
            n = s.execute(text("SELECT count(*) FROM compute_listings WHERE provider = :p"), {"p": FAKE}).scalar()
        assert n == 60, n
        # The next poll does not re-open it (accepted count remembered while the norm catches up).
        w.poll(big)
        assert len(w.holds("*")) == 1

        # Collapse to 2 of ~60: an incident; missing listings simply age, nothing marked gone.
        w.poll(items[:2])
        assert len(w.incidents("listing_collapse", "open")) == 1

        # The quality layer itself failing never stops ingestion (fail open).
        real = quarantine._screen_provider
        quarantine._screen_provider = lambda p, items: (_ for _ in ()).throw(RuntimeError("bug in quality"))
        try:
            r = w.poll(items)
        finally:
            quarantine._screen_provider = real
        assert r["listings"] == 10 and w.listing("L0").observed_at == w.t
        assert len(w.incidents("quality_layer_error", "open")) == 1
    finally:
        scratchdb.drop(DB)


# --------------------------------------------------------------------------
# Schema fingerprints
# --------------------------------------------------------------------------


def test_schema_watch():
    w = fresh_world()
    try:
        a, b = item(1, 2.0, extra="x"), item(2, 3.0)
        shape1 = {"items": [a, b], "meta": {"page": 1}}

        sent = []

        def snap(payload, provider="sp", endpoint="/e", ok=True):
            sent.append(payload)
            w.t += timedelta(minutes=15)
            with w.Session.begin() as s:
                s.add(RawSnapshot(provider=provider, endpoint=endpoint, fetched_at=w.t, ok=ok, payload=payload))

        snap(shape1)
        assert schema_watch.scan()["schema_changes"] == 0           # baseline
        snap({"items": [b, a], "meta": {"page": 2}})                 # same shape, other values / order
        snap({"items": [{**a, "extra": None}, b], "meta": {"page": 3}})  # a null is not a type change
        snap(None, ok=False)                                         # failed fetch: ignored
        assert schema_watch.scan() == {"snapshots": 2, "schema_changes": 0}
        # A renamed field: removed + added paths, one major incident.
        snap({"items": [{"id": "L1", "cost": 2.0}], "meta": {"page": 4}})
        assert schema_watch.scan()["schema_changes"] == 1
        (inc,) = schema_watch.changes()
        assert inc["severity"] == "major" and inc["provider"] == "sp" and inc["endpoint"] == "/e"
        assert ".items[].price" in inc["detail"]["removed"] and ".items[].cost" in inc["detail"]["added"]
        # Back to a known shape: not a new change. A type change is.
        snap(shape1)
        assert schema_watch.scan()["schema_changes"] == 0
        snap({"items": [{**a, "price": "2.0"}, b], "meta": {"page": 5}})
        assert schema_watch.scan()["schema_changes"] == 1
        # Map-like dicts (keys are data) do not churn as keys come and go.
        snap({"prices": {"p3.8xlarge": {"usd": 1}, "g5.xlarge": {"usd": 2}}}, endpoint="/aws")
        snap({"prices": {"p5.48xlarge": {"usd": 9}}}, endpoint="/aws")
        assert schema_watch.scan()["schema_changes"] == 0
        assert schema_watch.shape({"x": [1, None, "a"]})[0] == ["$:object", ".x:array", ".x[]:number", ".x[]:string"]
        # Bounded: huge payloads are truncated, not walked forever.
        paths, truncated = schema_watch.shape({"rows": [{f"k{i}": i} for i in range(5000)]})
        assert truncated and len(paths) <= schema_watch.MAX_PATHS
        # Raw payloads are untouched.
        with w.Session() as s:
            assert s.execute(select(RawSnapshot.payload).order_by(RawSnapshot.id)).scalars().all() == sent
    finally:
        scratchdb.drop(DB)


# --------------------------------------------------------------------------
# Trust labels and provider health
# --------------------------------------------------------------------------


def _row(s, provider, lid, seen, available=True, gpu="NVIDIA H100 80GB SXM5"):
    s.add(ComputeListingRow(provider=provider, listing_id=lid, sku=lid, raw_gpu_name="H100", canonical_gpu_name=gpu,
                            gpu_count=1, region="r", price_per_gpu_hour=D("2.0"), price_per_instance_hour=D("2.0"),
                            market_type="on_demand", available=available, observed_at=seen, first_seen_at=seen))


def test_trust():
    w = fresh_world()
    try:
        ago = lambda m: NOW - timedelta(minutes=m)  # noqa: E731
        with w.Session.begin() as s:
            for p in ("hyperstack", "aws", "vast", "nebius", "runpod"):
                s.add(RawSnapshot(provider=p, endpoint="/x", fetched_at=ago(2), ok=True, duration_ms=100))
            # runpod: some failures in 24h, latest response ok
            for m, ms in ((30, 300), (60, 200)):
                s.add(RawSnapshot(provider="runpod", endpoint="/x", fetched_at=ago(m), ok=False,
                                  duration_ms=ms, error="502 Bad Gateway", status_code=502))
            # lambda: nothing ok for hours -> down
            s.add(RawSnapshot(provider="lambda", endpoint="/x", fetched_at=ago(300), ok=True, duration_ms=50))
            for m in (100, 85, 70):
                s.add(RawSnapshot(provider="lambda", endpoint="/x", fetched_at=ago(m), ok=False, error="timeout"))
            _row(s, "hyperstack", "fresh", ago(5))
            _row(s, "hyperstack", "aging", ago(25))
            _row(s, "hyperstack", "stale", ago(60))
            _row(s, "hyperstack", "unknown-avail", ago(5), available=None)
            _row(s, "aws", "a", ago(5))
            _row(s, "vast", "v", ago(5))
            _row(s, "nebius", "n", ago(5))
            _row(s, "runpod", "r", ago(5))
            _row(s, "lambda", "l", ago(300))
            s.flush()
            s.add(ListingObservation(provider="hyperstack", listing_id="fresh", observed_at=ago(500), price_per_gpu_hour=D("2.0")))
            s.add(ListingObservation(provider="hyperstack", listing_id="fresh", observed_at=ago(100), price_per_gpu_hour=D("2.0")))

        def t(p, lid):
            with w.Session() as s:
                r = s.get(ComputeListingRow, (p, lid))
                return trust.listing_trust({c.name: getattr(r, c.name) for c in ComputeListingRow.__table__.columns})

        x = t("hyperstack", "fresh")
        assert x["source"] == "infrahub-api.nexgencloud.com" and x["source_type"] == "direct_api"
        assert x["availability_basis"] == "explicit" and x["freshness"] == "fresh"
        assert x["last_changed"] == ago(100) and x["last_fetched"] == ago(2), x
        assert 290 < x["age_seconds"] < 400
        assert x["confidence"] == "high", x
        assert t("hyperstack", "aging")["freshness"] == "aging" and t("hyperstack", "aging")["confidence"] == "medium"
        st = t("hyperstack", "stale")
        assert st["freshness"] == "stale" and st["confidence"] == "low"
        assert t("hyperstack", "unknown-avail")["availability_basis"] == "unknown"
        a = t("aws", "a")
        assert a["source_type"] == "pricing_file" and a["availability_basis"] == "unknown" and a["confidence"] == "medium"
        assert t("vast", "v")["availability_basis"] == "inferred"       # CONSTANT in mapping.py
        assert t("nebius", "n")["source_type"] == "public_page"
        lam = t("lambda", "l")
        assert lam["provider_status"] == "down" and lam["confidence"] == "low"

        # A pending hold lowers confidence.
        with w.Session.begin() as s:
            s.add(Quarantine(provider="hyperstack", listing_id="fresh", field="price_per_gpu_hour", rule="price_jump",
                             previous_value=2.0, new_value=20.0, auto_acceptable=True, first_seen=NOW,
                             last_seen=NOW, seen_count=1, status="pending"))
        h = t("hyperstack", "fresh")
        assert h["confidence"] == "low" and h["held"][0]["rule"] == "price_jump"

        rp = t("runpod", "r")
        assert rp["provider_status"] == "degraded" and rp["confidence"] == "medium" and rp["availability_basis"] == "inferred"
        hs = trust.provider_health("runpod")
        assert hs["fetches_24h"] == 3 and hs["failures_24h"] == 2 and abs(hs["failure_rate_24h"] - 2 / 3) < 1e-3
        assert hs["status"] == "degraded" and hs["consecutive_failures"] == 0
        assert hs["latency_ms_p50_24h"] == 200 and hs["last_failure"]["error"] == "502 Bad Gateway"
        assert hs["listings_24h_ago"] is None and hs["listings_24h_ago_note"]
        hy = trust.provider_health("hyperstack")
        assert hy["status"] == "healthy" and hy["listings_total"] == 4 and hy["listings_now"] == 3 and hy["quarantined"] == 1
        lh = trust.provider_health("lambda")
        assert lh["status"] == "down" and lh["consecutive_failures"] == 3
        assert trust.provider_health("aws")["status"] == "healthy"
        names = [h["provider"] for h in trust.all_provider_health()]
        assert names == sorted(["aws", "hyperstack", "lambda", "nebius", "runpod", "vast"]), names
        pub = trust.public_health(hs)
        assert "last_failure" not in pub and "502" not in str(pub)
    finally:
        scratchdb.drop(DB)


# --------------------------------------------------------------------------
# _latest_raw: the rewrite returns exactly what the old full scan did
# --------------------------------------------------------------------------


def _latest_raw_old(session, provider):
    rows = session.execute(
        select(RawSnapshot).where(RawSnapshot.provider == provider, RawSnapshot.ok.is_(True))
        .order_by(RawSnapshot.fetched_at.desc(), RawSnapshot.id.desc())
    ).scalars()
    by_endpoint = defaultdict(list)
    newest_seen = {}
    for row in rows:
        first = newest_seen.setdefault(row.endpoint, row.fetched_at)
        if (first - row.fetched_at).total_seconds() > 120:
            continue
        by_endpoint[row.endpoint].append(row)
    return by_endpoint


def test_latest_raw_equivalence():
    w = fresh_world()
    try:
        rng = random.Random(3)
        with w.Session.begin() as s:
            for p in ("a", "b", "c"):
                eps = [f"/ep{i}" for i in range(rng.randint(1, 5))]
                t = NOW - timedelta(days=2)
                for round_ in range(40):
                    t += timedelta(minutes=15)
                    for ep in eps:
                        if rng.random() < 0.2:
                            continue  # endpoint skipped this round
                        for k in range(rng.choice([1, 1, 3])):  # Salad-style many calls per round
                            jitter = timedelta(seconds=rng.choice([0, 0, 5, 59, 119, 120, 121, 200]))
                            s.add(RawSnapshot(provider=p, endpoint=ep, fetched_at=t + jitter, ok=rng.random() > 0.15,
                                              payload={"r": round_, "k": k}))
                # an endpoint that only ever failed
                s.add(RawSnapshot(provider=p, endpoint="/never-ok", fetched_at=t, ok=False))
                # identical timestamps: id breaks the tie
                s.add(RawSnapshot(provider=p, endpoint=eps[0], fetched_at=t + timedelta(hours=1), ok=True, payload={"tie": 1}))
                s.add(RawSnapshot(provider=p, endpoint=eps[0], fetched_at=t + timedelta(hours=1), ok=True, payload={"tie": 2}))
        with w.Session() as s:
            for p in ("a", "b", "c", "nobody"):
                old, new = _latest_raw_old(s, p), normalize._latest_raw(s, p)
                assert list(old) == list(new), (p, list(old), list(new))  # same endpoint order
                for ep in old:
                    assert [r.id for r in old[ep]] == [r.id for r in new[ep]], (p, ep)
                assert "/never-ok" not in new
            empty = normalize._latest_raw(s, "nobody")
            assert empty == {} and empty["/missing"] == []  # a defaultdict, as before
    finally:
        scratchdb.drop(DB)


# --------------------------------------------------------------------------
# Ops / trust API smoke
# --------------------------------------------------------------------------


def test_api():
    w = fresh_world()
    try:
        from fastapi.testclient import TestClient

        import main
        from config import settings

        settings.app_password = None  # open local dev: the caller is the operator
        client = TestClient(main.app)  # no lifespan: no poller, no jobs, no migrations
        for _ in range(3):
            w.poll(base_items())
        w.poll(base_items(**{"0": {"price": 250}}))
        schema_watch.scan()
        trust.all_provider_health.cache_clear()

        for path in ("/v1/ops/summary", "/v1/ops/providers", "/v1/ops/incidents", "/v1/ops/quarantine",
                     "/v1/ops/schema-changes", "/v1/ops/stale", "/v1/ops/unmapped", "/v1/ops/jobs",
                     "/v1/trust/providers", f"/v1/trust/listings?provider={FAKE}"):
            r = client.get(path)
            assert r.status_code == 200, (path, r.status_code, r.text[:500])
            assert "data" in r.json() and "as_of" in r.json()["meta"], path
        summ = client.get("/v1/ops/summary").json()["data"]
        assert summ["quarantine"]["pending"] == 1 and summ["suspicious_price_moves"]["pending"] == 1
        assert summ["routing"]["routing_decisions"]["available"] in (True, False)
        assert summ["providers"]["items"][0]["provider"] == FAKE
        listings = client.get(f"/v1/trust/listings?provider={FAKE}").json()["data"]
        l0 = next(x for x in listings if x["listing_id"] == "L0")
        assert l0["price_per_gpu_hour"] == 2.0 and l0["trust"]["confidence"] == "low" and l0["trust"]["held"]
        assert client.get("/v1/trust/listings?gpu=no-such-gpu").status_code == 404
        q = client.get("/v1/ops/quarantine").json()["data"]
        assert len(q) == 1
        r = client.post(f"/v1/ops/quarantine/{q[0]['id']}/reject", json={"note": "unit error"})
        assert r.status_code == 200 and r.json()["data"]["status"] == "rejected"
        assert client.post(f"/v1/ops/quarantine/{q[0]['id']}/accept").status_code == 409
        assert client.post("/v1/ops/quarantine/999999/accept").status_code == 404

        # A bearer key without the admin scope is refused.
        from accounts import auth

        import accounts.keys as keys

        real = keys.verify
        keys.verify = lambda token, request: auth.Principal(kind="api_key", account_id=1, key_id=7,
                                                            scopes=frozenset({"data:read"}))
        try:
            h = {"Authorization": "Bearer opg_test"}
            assert client.get("/v1/ops/summary", headers=h).status_code == 403
            assert client.get("/v1/trust/providers", headers=h).status_code == 200
        finally:
            keys.verify = real
    finally:
        scratchdb.drop(DB)


if __name__ == "__main__":
    for t in (test_rules, test_quarantine_flow, test_malformed_and_explosion, test_schema_watch, test_trust,
              test_latest_raw_equivalence, test_api):
        t()
        print(t.__name__, "ok")
