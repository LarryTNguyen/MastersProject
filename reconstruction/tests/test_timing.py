import json
import unittest

from trace_reconstruction.topology import reconstruct_trace
from trace_reconstruction.store import PostgresStore
from tests.helpers import fixture, cloned
from tests.test_store import FakeConnection


class TimingTests(unittest.TestCase):
    def test_overlapping_children_subtract_union_and_default_has_no_path(self):
        trace = reconstruct_trace(fixture())
        self.assertEqual(trace.span_features()[trace.root_span_id]['observed_self_time_ns'], '300000000')
        self.assertIsNone(trace.critical_path)

    def test_estimate_partitions_root_without_double_counting(self):
        trace = reconstruct_trace(fixture(), assume_synchronous=True)
        segments = trace.critical_path['segments']
        self.assertEqual(sum(int(s['end_time_ns']) - int(s['start_time_ns']) for s in segments), 1_000_000_000)
        self.assertTrue(all(a['end_time_ns'] == b['start_time_ns'] for a, b in zip(segments, segments[1:])))
        self.assertEqual(sum(int(f['critical_path_contribution_ns']) for f in trace.timing_features.values()), 1_000_000_000)
        self.assertTrue(trace.critical_path['is_estimate'])

    def test_missing_timing_does_not_become_zero(self):
        root, child, _ = fixture()
        child = cloned(child, start_time_ns=None, end_time_ns=None, time_domain='UNKNOWN')
        trace = reconstruct_trace([root, child], assume_synchronous=True)
        self.assertIsNone(trace.timing_features[root['span_id']]['observed_self_time_ns'])
        self.assertIsNone(trace.critical_path)

    def test_skew_clips_self_time_but_disables_path(self):
        root, child, _ = fixture()
        child = cloned(child, start_time_ns=str(int(root['start_time_ns']) - 1))
        trace = reconstruct_trace([root, child], assume_synchronous=True)
        self.assertEqual(trace.timing_features[root['span_id']]['observed_self_time_ns'], '200000000')
        self.assertIsNone(trace.critical_path)

    def test_conflicts_disable_timing(self):
        trace = reconstruct_trace(fixture(), has_conflicts=True, assume_synchronous=True)
        self.assertFalse(trace.quality['self_time_ready'])
        self.assertIsNone(trace.critical_path)

    def test_database_payload_contains_timing_and_root_duration(self):
        connection = FakeConnection()
        trace = reconstruct_trace(fixture(), assume_synchronous=True)
        PostgresStore(connection).publish_topology(trace, 3, algorithm_version='timing-v1-synchronous')
        _, params = connection.executed[0]
        self.assertEqual(params[10], '1000000000')
        self.assertEqual(json.loads(params[13])['model'], 'synchronous-last-finisher-v1')
