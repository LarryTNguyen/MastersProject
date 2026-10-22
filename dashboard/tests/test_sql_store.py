import json
import os
import sqlite3
import unittest

from app.sql_store import SqlStore

from .test_stores import FX, StoreContract, read_fixture_spans


class SqlStoreTests(StoreContract, unittest.TestCase):
    def setUp(self):
        self.store = SqlStore(sqlite3.connect(":memory:"))
        self.store.create_schema()
        self.store.load_spans(read_fixture_spans())
        with open(os.path.join(FX, "rankings.json")) as f:
            self.store.load_rankings(json.load(f))

    def test_load_is_idempotent(self):
        self.store.load_spans(read_fixture_spans())
        self.assertEqual(self.store.count_traces(), 3)
        self.assertEqual(self.store.get_trace("t-slow")["summary"]["span_count"], 4)

    def test_ingest_stamps_round_trip(self):
        spans = read_fixture_spans()
        spans[0]["attributes"]["sta.received_us"] = 5
        spans[0]["attributes"]["sta.normalized_us"] = 9
        self.store.load_spans(spans)
        back = [s for s in self.store.get_trace(spans[0]["trace_id"])["spans"] if s["span_id"] == spans[0]["span_id"]][0]
        self.assertEqual((back["attributes"]["sta.received_us"], back["attributes"]["sta.normalized_us"]), (5, 9))
        self.assertEqual(self.store.pipeline(now_us=100)["spans_with_stamps"], 1)

    def test_postgres_placeholders(self):
        s = SqlStore(None, paramstyle="%s")
        self.assertEqual(s._q("SELECT ? , ?"), "SELECT %s , %s")


if __name__ == "__main__":
    unittest.main()
