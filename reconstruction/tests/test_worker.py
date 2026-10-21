from __future__ import annotations

import unittest

from trace_reconstruction.store import TraceCandidate
from trace_reconstruction.worker import ReconstructionWorker

from tests.helpers import fixture


class FakeStore:
    def __init__(self):
        self.records = fixture()
        self.revision = 3
        self.due = [TraceCandidate(self.records[0]["run_id"], self.records[0]["trace_id"], 3, "QUIET_WINDOW")]
        self.pending = []
        self.published = []
        self.ended = []

    def due_traces(self, limit):
        return list(self.due)

    def settle(self, candidate):
        self.due = []
        self.pending = [candidate]
        return True

    def pending_analyses(self, algorithm_version, limit):
        result, self.pending = self.pending, []
        return result

    def fetch_trace(self, run_id, trace_id):
        return self.records, self.revision, False

    def publish_topology(self, trace, revision, **kwargs):
        self.published.append((trace, revision, kwargs))

    def end_run(self, run_id):
        self.ended.append(run_id)

    def open_traces_for_run(self, run_id):
        return list(self.due)


class WorkerTests(unittest.TestCase):
    def test_poll_settles_then_publishes_topology(self):
        store = FakeStore()
        stats = ReconstructionWorker(store).poll_once()
        self.assertEqual((stats.settled, stats.analyzed, stats.stale), (1, 1, 0))
        self.assertEqual(store.published[0][0].topology, "CONNECTED")
        self.assertEqual(store.published[0][1], 3)

    def test_stale_revision_is_not_published(self):
        store = FakeStore()
        store.pending = list(store.due)
        store.due = []
        store.revision = 4
        stats = ReconstructionWorker(store).poll_once()
        self.assertEqual(stats.stale, 1)
        self.assertFalse(store.published)

    def test_finish_run_uses_end_of_input(self):
        store = FakeStore()
        store.due[0] = TraceCandidate(store.due[0].run_id, store.due[0].trace_id, 3, "END_OF_INPUT")
        stats = ReconstructionWorker(store).finish_run(store.due[0].run_id)
        self.assertEqual(stats.settled, 1)
        self.assertEqual(stats.analyzed, 1)
        self.assertEqual(len(store.ended), 1)


if __name__ == "__main__":
    unittest.main()
