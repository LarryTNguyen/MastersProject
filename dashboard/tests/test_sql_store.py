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

    def test_postgres_placeholders(self):
        s = SqlStore(None, paramstyle="%s")
        self.assertEqual(s._q("SELECT ? , ?"), "SELECT %s , %s")


if __name__ == "__main__":
    unittest.main()
