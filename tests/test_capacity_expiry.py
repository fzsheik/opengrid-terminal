"""Capacity: a current row its provider has stopped reporting is removed after a day (capacity.expire).

Before this, a machine type that left a marketplace kept its last state in capacity_current for ever, so "in stock"
could describe a row nobody had seen for weeks (2026-11-16: 349 of 2,863 rows, 294 of them "in stock").

Covers: a row left out for less than a day only ages; after a day it is deleted and the history gets one closing
row; a row still reported is never touched however old its first sighting; a provider that did not answer in this
run keeps everything; a provider whose capacity is read rarely (Vast's census) gets six reads, not one day; a row
that comes back starts again.

Run:  .venv/bin/python tests/test_capacity_expiry.py
"""

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
import capacity  # noqa: E402
from models import CapacityObservation  # noqa: E402
from providers import PROVIDERS  # noqa: E402
from sqlalchemy import select  # noqa: E402
from store.capacity import CapacityChange, CapacityCurrent  # noqa: E402

DB = "og_test_capacity_expiry"
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)


def obs(provider, sku, at, region="na", in_stock=True):
    return CapacityObservation(provider=provider, sku=sku, region=region, raw_gpu_name="H100", canonical_gpu_name=None,
                               gpu_count=1, in_stock=in_stock, tier=None, available_count=None, total_count=None,
                               raw_value="x", source="/x", observed_at=at)


def current(Session, provider):
    with Session() as s:
        return {r.sku: r for r in s.scalars(select(CapacityCurrent).where(CapacityCurrent.provider == provider))}


def closing(Session):
    with Session() as s:
        return [(c.provider, c.sku, c.in_stock, c.raw_value) for c in s.scalars(
            select(CapacityChange).where(CapacityChange.raw_value.like(capacity.EXPIRED + "%")).order_by(CapacityChange.id))]


def test_expiry():
    url = scratchdb.create(DB)
    try:
        Session = fixtures.session(url)
        normalize.SessionLocal = Session
        hour = timedelta(hours=1)
        assert PROVIDERS["koyeb"].polling.capacity_seconds * capacity.EXPIRE_AFTER_READS < 24 * 3600, "koyeb: a day decides"

        # Three koyeb rows, last seen 30 h, 5 h and just now; one lium row 30 h old.
        capacity.save([obs("koyeb", "gone", NOW - 30 * hour), obs("koyeb", "skipped", NOW - 5 * hour),
                       obs("koyeb", "live", NOW - 30 * hour), obs("lium", "old", NOW - 30 * hour)])
        # This run: koyeb answers with "live" only; lium does not answer at all.
        run = [obs("koyeb", "live", NOW)]
        capacity.save(run)
        assert capacity.expire(run, NOW) == 1
        assert set(current(Session, "koyeb")) == {"skipped", "live"}, "30 h unseen: removed; 5 h unseen: only stale"
        assert set(current(Session, "lium")) == {"old"}, "a provider that did not answer keeps everything"
        ends = closing(Session)
        assert len(ends) == 1 and ends[0][:3] == ("koyeb", "gone", None), ends
        assert (NOW - 30 * hour).isoformat() in ends[0][3], "the closing row says when it was last seen"
        assert capacity.expire(run, NOW) == 0 and len(closing(Session)) == 1, "nothing left to expire: no second closing row"

        # A reported row is never expired, however old the row it replaces was.
        assert current(Session, "koyeb")["live"].observed_at == NOW

        # 20 h later "skipped" has been unseen for 25 h: now it goes too.
        later = NOW + 20 * hour
        run = [obs("koyeb", "live", later)]
        capacity.save(run)
        assert capacity.expire(run, later) == 1 and set(current(Session, "koyeb")) == {"live"}

        # The row comes back: a new current row and an ordinary change, as for any first sighting.
        back = [obs("koyeb", "gone", later + hour), obs("koyeb", "live", later + hour)]
        assert capacity.save(back)["changes"] == 1
        assert capacity.expire(back, later + hour) == 0 and set(current(Session, "koyeb")) == {"gone", "live"}

        # Vast's census runs up to 12 h apart: six reads (3 days), not one day.
        vast_wait = timedelta(seconds=PROVIDERS["vast"].polling.capacity_seconds * capacity.EXPIRE_AFTER_READS)
        assert vast_wait > capacity.EXPIRE_AFTER
        capacity.save([obs("vast", "A", NOW - 30 * hour, region=None), obs("vast", "B", NOW - vast_wait - hour, region=None)])
        run = [obs("vast", "C", NOW, region=None)]
        capacity.save(run)
        assert capacity.expire(run, NOW) == 1 and set(current(Session, "vast")) == {"A", "C"}

        # refresh() reports it beside pruned.
        capacity.save([obs("lium", "old", datetime.now(timezone.utc) - 30 * hour)])
        real = capacity.normalize_all
        capacity.normalize_all = lambda only=None: [obs("lium", "new", datetime.now(timezone.utc))]
        try:
            out = capacity.refresh()
        finally:
            capacity.normalize_all = real
        assert out["expired"] == 1 and set(current(Session, "lium")) == {"new"}, out
    finally:
        scratchdb.drop(DB)


if __name__ == "__main__":
    test_expiry()
    print("test_expiry ok")
    print("all passed")
