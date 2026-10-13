"""Real Postgres coverage of deduplication, rolling upgrades, and retention safety.

Run with TEST_PG_URL pointing at a disposable local Postgres server.
"""

import os
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import scratchdb

DB = "og_test_storage"
os.environ["DATABASE_URL"] = scratchdb.url(DB)
os.environ["OPENGRID_NO_JOBS"] = "1"

from alembic import command
from sqlalchemy import select, text

import db
import fetch
import normalize
import raw_store
import storage
from models import RawResponse
from quality import schema_watch
from tables import ComputeListingRow, ListingObservation, RawPayload, RawSnapshot

NOW = datetime.now(timezone.utc)


def response(payload, *, provider="storage_test", endpoint="/items", at=NOW, request=None, ok=True):
    return RawResponse(provider=provider, endpoint=endpoint, fetched_at=at,
                       payload=payload, request=request, ok=ok, status_code=200 if ok else 503)


class StorageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        scratchdb.create(DB)
        db.init_db()

    @classmethod
    def tearDownClass(cls):
        db.engine.dispose()
        scratchdb.drop(DB)

    def setUp(self):
        with db.engine.begin() as c:
            self.assertEqual(c.execute(text("select current_database()")).scalar(), DB)
            c.execute(text("TRUNCATE raw_snapshots, raw_payloads, quality_cursor, source_schemas, news_fetch_log, compute_listings CASCADE"))

    def test_dedupe_preserves_attempts_requests_freshness_and_reads(self):
        first = NOW - timedelta(minutes=15)
        body = {"items": [{"id": "a", "price": 1}]}
        fetch.save([response(body, at=first), response(body, request={"class": "a"}),
                    response(body, request={"class": "b"}), response(None, ok=False),
                    response({}, endpoint="/empty"), response([], endpoint="/list")])
        with db.SessionLocal() as s:
            self.assertEqual(s.query(RawSnapshot).count(), 6)
            self.assertEqual(s.query(RawPayload).count(), 3)
            latest = normalize._latest_raw(s, "storage_test")["/items"]
            self.assertEqual(len(latest), 2)
            self.assertEqual({r.request["class"] for r in latest}, {"a", "b"})
            self.assertTrue(all(r.payload == body and r.fetched_at == NOW for r in latest))
            self.assertEqual(s.execute(select(RawSnapshot.payload).where(RawSnapshot.ok, RawSnapshot.endpoint == "/items")).scalars().all(), [body]*3)
        self.assertEqual(raw_store.latest_payload("storage_test", "/empty"), {})
        sizes = raw_store.snapshots()
        self.assertTrue(all(r["bytes"] > 0 for r in sizes if r["ok"]))
        self.assertGreater(schema_watch.scan()["snapshots"], 0)
        fetch.save([response({"changed": True})])
        self.assertEqual(schema_watch.scan()["snapshots"], 1)

    def test_legacy_backfill_is_lossless_and_idempotent(self):
        with db.SessionLocal.begin() as s:
            for payload in [{"a": 1, "b": 2}, {"b": 2, "a": 1}, {}, [], None]:
                s.add(RawSnapshot(provider="legacy", endpoint="/x", fetched_at=NOW, payload=payload))
            # Old SQLAlchemy JSONB mapping stored Python None as JSON null.
            s.execute(text("INSERT INTO raw_snapshots(provider,endpoint,method,fetched_at,ok,payload) VALUES ('legacy','/x','GET',now(),true,'null'::jsonb)"))
        self.assertEqual(storage.deduplicate_batch(10), 5)
        self.assertEqual(storage.deduplicate_batch(10), 0)
        with db.SessionLocal() as s:
            self.assertEqual(s.query(RawPayload).count(), 3)
            payloads = s.execute(select(RawSnapshot.payload).order_by(RawSnapshot.id)).scalars().all()
            self.assertEqual(payloads, [None, {"a": 1,"b":2}, {"a":1,"b":2}, {}, [], None])
        self.assertEqual(storage.status()["raw"]["inline_pending"], 0)

    def test_retention_protects_latest_successful_round_and_cursor(self):
        ancient = NOW - timedelta(days=100)
        with db.SessionLocal.begin() as s:
            s.add(ComputeListingRow(provider="history", listing_id="kept", sku="kept", raw_gpu_name="H100",
                                    gpu_count=1, currency="USD", observed_at=NOW, first_seen_at=ancient))
            s.flush()
            s.add(ListingObservation(provider="history", listing_id="kept", observed_at=ancient,
                                     price_per_gpu_hour=2))
        fetch.save([
            response({"old": 0}, at=ancient-timedelta(days=1), endpoint="/offline"),
            response({"class": 1}, at=ancient, endpoint="/offline", request={"class":1}),
            response({"class": 2}, at=ancient+timedelta(seconds=5), endpoint="/offline", request={"class":2}),
            response({"old_error":1}, at=NOW-timedelta(days=95), endpoint="/offline", ok=False),
            response({"error":1}, at=NOW, endpoint="/offline", ok=False),
            response({"old":1}, at=ancient),
            response({"middle":1}, at=NOW-timedelta(days=40)),
            response({"new":1}),
        ])
        with db.SessionLocal.begin() as s:
            s.execute(text("INSERT INTO quality_cursor(name,position,updated_at) VALUES ('schema_watch',0,now())"))
        self.assertEqual(storage.retention_batch(NOW)["expired_snapshots"], 0)
        with db.SessionLocal.begin() as s:
            s.execute(text("UPDATE quality_cursor SET position = (SELECT max(id) FROM raw_snapshots)"))
            s.execute(text("INSERT INTO news_fetch_log(source_id,fetched_at,ok,not_modified,items,new_items) VALUES ('test',:old,true,false,0,0),('test',:now,true,false,0,0)"), {"old":ancient,"now":NOW})
        result = storage.retention_batch(NOW)
        self.assertEqual(result["expired_snapshots"], 3)
        self.assertEqual(result["expired_news_fetches"], 1)
        with db.SessionLocal() as s:
            offline = normalize._latest_raw(s, "storage_test")["/offline"]
            self.assertEqual({r.payload["class"] for r in offline}, {1, 2})
            middle = s.execute(select(RawSnapshot).where(RawSnapshot.fetched_at == NOW-timedelta(days=40))).scalar_one()
            self.assertIsNone(middle.payload)
            self.assertIsNotNone(middle.sha256)
            history = s.query(ListingObservation).one()
            self.assertEqual(history.observed_at, ancient)
            self.assertEqual(history.price_per_gpu_hour, 2)
        self.assertEqual(storage.retention_batch(NOW)["expired_bodies"], 0)

    def test_invalid_retention_settings_are_rejected(self):
        from config import Settings
        from pydantic import ValidationError
        for values in [{"raw_payload_retention_days": 0}, {"raw_payload_retention_days": 91},
                       {"raw_snapshot_retention_days": -1}, {"news_fetch_retention_days": 0}]:
            with self.assertRaises(ValidationError):
                Settings(**values)

    def test_concurrent_ingest_and_collection(self):
        def ingest(_):
            fetch.save([response({"shared": True})])
        with ThreadPoolExecutor(max_workers=4) as pool:
            tasks = [pool.submit(ingest, i) for i in range(12)]
            tasks += [pool.submit(storage.retention_batch, NOW) for _ in range(3)]
            for task in tasks:
                task.result()
        with db.SessionLocal() as s:
            self.assertEqual(s.query(RawPayload).count(), 1)
            self.assertEqual(s.query(RawSnapshot).count(), 12)
            self.assertTrue(all(r.payload == {"shared":True} for r in s.query(RawSnapshot)))

    def test_migration_roundtrip_preserves_legacy_and_shared_bodies(self):
        body = {"migration": True}
        fetch.save([response(body)])
        command.downgrade(db._alembic_config(), "0015_lifecycle")
        with db.engine.connect() as c:
            self.assertEqual(c.execute(text("SELECT payload FROM raw_snapshots")).scalar_one(), body)
        command.upgrade(db._alembic_config(), "head")
        self.assertEqual(storage.deduplicate_batch(), 1)
        self.assertEqual(raw_store.latest_payload("storage_test", "/items"), body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
