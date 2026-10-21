from __future__ import annotations

import unittest

from trace_reconstruction.topology import (
    IngestDisposition,
    TraceGroup,
    group_records,
    reconstruct_trace,
)

from tests.helpers import cloned, fixture


class GroupingTests(unittest.TestCase):
    def setUp(self):
        self.records = fixture()
        self.root, self.payment, self.inventory = self.records

    def test_parent_first_and_child_first_produce_same_topology(self):
        expected = reconstruct_trace(self.records)
        actual = reconstruct_trace([self.payment, self.inventory, self.root])
        self.assertEqual(actual.topology, "CONNECTED")
        self.assertEqual(actual.parents, expected.parents)
        self.assertEqual(actual.children, expected.children)

    def test_exact_retry_ignores_arrival_metadata(self):
        group = TraceGroup(self.root["run_id"], self.root["trace_id"])
        self.assertEqual(group.add(self.root), IngestDisposition.INSERTED)
        retry = cloned(self.root, received_at_unix_ns="1791936002999999999")
        retry["source"]["raw_reference"] = "s3://retry/batch"
        self.assertEqual(group.add(retry), IngestDisposition.DUPLICATE)
        self.assertEqual(group.duplicate_count, 1)
        quality = group.reconstruct().quality
        self.assertTrue(quality["duplicate_count_available"])
        self.assertEqual(quality["duplicate_count"], 1)

    def test_semantic_conflict_keeps_first_observation(self):
        group = TraceGroup(self.root["run_id"], self.root["trace_id"])
        group.add(self.root)
        conflict = cloned(self.root, operation_name="different-operation")
        self.assertEqual(group.add(conflict), IngestDisposition.CONFLICT)
        self.assertEqual(group.records[self.root["span_id"]]["operation_name"], "POST /checkout")
        self.assertEqual(group.reconstruct().quality["conflict_span_ids"], [self.root["span_id"]])

    def test_same_trace_ids_in_different_runs_are_isolated(self):
        other = [cloned(record, run_id="00000000-0000-4000-8000-000000000002") for record in self.records]
        groups = group_records(self.records + other)
        self.assertEqual(len(groups), 2)
        self.assertTrue(all(len(group.records) == 3 for group in groups.values()))

    def test_rejects_mixed_trace_input(self):
        changed = cloned(self.payment, trace_id="33333333333333333333333333333333")
        with self.assertRaisesRegex(ValueError, "exactly one run and trace"):
            reconstruct_trace([self.root, changed])

    def test_rejects_undeduplicated_input(self):
        with self.assertRaisesRegex(ValueError, "deduplicate"):
            reconstruct_trace([self.root, self.root])


class TopologyQualityTests(unittest.TestCase):
    def setUp(self):
        self.root, self.payment, self.inventory = fixture()

    def test_connected_trace_and_deterministic_child_order(self):
        trace = reconstruct_trace([self.inventory, self.root, self.payment])
        self.assertEqual(trace.topology, "CONNECTED")
        self.assertEqual(trace.root_span_id, self.root["span_id"])
        self.assertEqual(trace.children[self.root["span_id"]],
                         (self.payment["span_id"], self.inventory["span_id"]))
        self.assertFalse(trace.quality["self_time_ready"])
        self.assertFalse(trace.quality["critical_path_ready"])
        self.assertFalse(trace.quality["duplicate_count_available"])

    def test_missing_parent_is_incomplete(self):
        trace = reconstruct_trace(fixture("missing-parent.json"))
        self.assertEqual(trace.topology, "INCOMPLETE")
        self.assertEqual(trace.quality["missing_parent_ids"], ["0000000000000099"])

    def test_no_root_is_incomplete(self):
        child = cloned(self.payment, parent_span_id="0000000000000099")
        trace = reconstruct_trace([child])
        self.assertEqual(trace.topology, "INCOMPLETE")
        self.assertEqual(trace.quality["root_count"], 0)

    def test_multiple_roots_are_invalid(self):
        second_root = cloned(self.payment, parent_span_id=None, parent_relation="ROOT")
        trace = reconstruct_trace([self.root, second_root])
        self.assertEqual(trace.topology, "INVALID")
        self.assertEqual(trace.quality["root_count"], 2)

    def test_parent_cycle_is_invalid(self):
        root_in_cycle = cloned(self.root, parent_span_id=self.payment["span_id"], parent_relation="EXPLICIT")
        trace = reconstruct_trace([root_in_cycle, self.payment])
        self.assertEqual(trace.topology, "INVALID")
        self.assertTrue(trace.quality["cycle"])

    def test_unknown_relationship_has_unknown_topology(self):
        unknown = cloned(self.root, parent_span_id=None, parent_relation="UNKNOWN")
        trace = reconstruct_trace([unknown])
        self.assertEqual(trace.topology, "UNKNOWN")
        self.assertEqual(trace.quality["unknown_parent_count"], 1)

    def test_inferred_parent_can_be_connected_but_is_reported(self):
        inferred = cloned(self.payment, parent_relation="INFERRED")
        trace = reconstruct_trace([self.root, inferred])
        self.assertEqual(trace.topology, "CONNECTED")
        self.assertEqual(trace.quality["inferred_parent_count"], 1)

    def test_out_of_bounds_interval_is_quality_evidence_not_invalid_topology(self):
        late_child = cloned(
            self.payment,
            start_time_ns="1791935999999999999",
            duration_ns="800000001",
        )
        late_child["end_time_ns"] = "1791936000800000000"
        trace = reconstruct_trace([self.root, late_child])
        self.assertEqual(trace.topology, "CONNECTED")
        self.assertEqual(trace.quality["out_of_bounds_child_count"], 1)

    def test_links_do_not_create_parent_edges(self):
        linked = cloned(self.payment)
        linked["links"] = [{"trace_id": self.root["trace_id"], "span_id": self.inventory["span_id"]}]
        trace = reconstruct_trace([self.root, linked])
        self.assertEqual(trace.parents[linked["span_id"]], self.root["span_id"])

    def test_spans_without_start_time_sort_by_span_id_after_timed_spans(self):
        untimed = cloned(self.payment, start_time_ns=None, end_time_ns=None,
                         duration_ns="700000000", time_domain="UNKNOWN", timing_quality="DURATION_ONLY")
        trace = reconstruct_trace([untimed, self.root])
        self.assertEqual([span["span_id"] for span in trace.spans],
                         [self.root["span_id"], untimed["span_id"]])


if __name__ == "__main__":
    unittest.main()
