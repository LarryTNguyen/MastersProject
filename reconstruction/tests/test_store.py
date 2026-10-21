from __future__ import annotations

import unittest

from trace_reconstruction.store import PostgresStore, StaleTraceError
from trace_reconstruction.topology import reconstruct_trace

from tests.helpers import fixture


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=()):
        self.connection.executed.append((" ".join(sql.split()), params))
        if self.connection.error is not None:
            error, self.connection.error = self.connection.error, None
            raise error
        if "ingest_span" in sql:
            self.rows = [(self.connection.dispositions.pop(0),)]

    def fetchone(self):
        return self.rows.pop(0)

    def fetchall(self):
        rows, self.rows = self.rows, []
        return rows


class FakeConnection:
    def __init__(self, dispositions=None, error=None):
        self.dispositions = list(dispositions or [])
        self.executed = []
        self.commits = 0
        self.rollbacks = 0
        self.error = error

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


class StoreTests(unittest.TestCase):
    def test_ingest_many_reports_database_dispositions(self):
        connection = FakeConnection(["INSERTED", "DUPLICATE", "CONFLICT"])
        store = PostgresStore(connection, validator=lambda record: [])
        records = fixture()
        counts = store.ingest_many(records)
        self.assertEqual(counts, {"INSERTED": 1, "DUPLICATE": 1, "CONFLICT": 1})
        self.assertEqual(connection.commits, 1)
        self.assertTrue(all("trace_v1.ingest_span" in sql for sql, _ in connection.executed))

    def test_validation_failure_rolls_back_batch(self):
        connection = FakeConnection(["INSERTED"])
        store = PostgresStore(connection, validator=lambda record: ["bad record"])
        with self.assertRaisesRegex(ValueError, "bad record"):
            store.ingest_many([fixture()[0]])
        self.assertEqual(connection.rollbacks, 1)
        self.assertEqual(connection.commits, 0)

    def test_stale_analysis_error_rolls_back_and_is_classified(self):
        connection = FakeConnection(error=RuntimeError("Analysis requires the current settled trace revision"))
        store = PostgresStore(connection)
        trace = reconstruct_trace(fixture())
        with self.assertRaises(StaleTraceError):
            store.publish_topology(trace, 3, algorithm_version="topology-v1")
        self.assertEqual(connection.rollbacks, 1)


if __name__ == "__main__":
    unittest.main()
