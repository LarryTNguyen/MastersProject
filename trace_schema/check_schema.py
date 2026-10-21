"""Meaningful contract checks; optional PostgreSQL parser, no running DB needed."""
import copy
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator
from validate import SCHEMA, load_records, observed_self_time_ns, trace_quality, validate_record

ROOT = Path(__file__).resolve().parent

class ContractChecks(unittest.TestCase):
    def setUp(self):
        self.parallel = load_records(ROOT / 'examples/parallel-trace.json')
        self.root = copy.deepcopy(self.parallel[0])

    def test_schema_and_examples(self):
        Draft202012Validator.check_schema(SCHEMA)
        for filename in ['parallel-trace.json', 'missing-parent.json', 'alibaba-call.json', 'typed-values.json']:
            for record in load_records(ROOT / 'examples' / filename):
                self.assertEqual(validate_record(record), [], filename)

    def test_parallel_children_use_interval_union(self):
        expected = json.loads((ROOT / 'examples/expected-parallel-analysis.json').read_text())
        self.assertEqual(observed_self_time_ns(self.root, self.parallel[1:]), int(expected['root_observed_self_time_ns']))
        self.assertTrue(trace_quality(self.parallel)['self_time_ready'])
        self.assertFalse(trace_quality(self.parallel)['critical_path_ready'])

    def test_intervals_are_clipped_without_rewriting(self):
        child = copy.deepcopy(self.parallel[1])
        child['start_time_ns'] = str(int(self.root['start_time_ns']) - 1000000)
        child['end_time_ns'] = str(int(self.root['end_time_ns']) + 1000000)
        self.assertEqual(observed_self_time_ns(self.root, [child]), 0)
        self.assertFalse(trace_quality([self.root, child])['self_time_ready'])

    def test_missing_parent_is_accepted_but_analysis_is_limited(self):
        quality = trace_quality(load_records(ROOT / 'examples/missing-parent.json'))
        self.assertEqual(quality['topology'], 'INCOMPLETE')
        self.assertEqual(len(quality['missing_parent_ids']), 1)
        self.assertFalse(quality['self_time_ready'])

    def test_duration_only_does_not_enable_self_time(self):
        records = load_records(ROOT / 'examples/alibaba-call.json')
        self.assertEqual(validate_record(records[0]), [])
        self.assertFalse(trace_quality(records)['self_time_ready'])
        with self.assertRaises(ValueError):
            observed_self_time_ns(records[0], [])

    def test_nanosecond_precision_and_bigint_bounds(self):
        self.assertEqual(int(self.root['start_time_ns']) + int(self.root['duration_ns']), int(self.root['end_time_ns']))
        self.root['received_at_unix_ns'] = str(2**63)
        self.assertTrue(validate_record(self.root))

    def test_malformed_and_zero_ids(self):
        for value in ['0' * 32, 'abc', 'A' * 32]:
            bad = copy.deepcopy(self.root)
            bad['trace_id'] = value
            self.assertTrue(validate_record(bad))

    def test_negative_inconsistent_and_partial_timing(self):
        for key, value in [('duration_ns', '-1'), ('duration_ns', '1'), ('end_time_ns', None), ('time_domain', 'UNKNOWN')]:
            bad = copy.deepcopy(self.root)
            bad[key] = value
            self.assertTrue(validate_record(bad))

    def test_unknown_is_distinct_from_zero(self):
        bad = load_records(ROOT / 'examples/alibaba-call.json')[0]
        self.assertIsNone(bad['start_time_ns'])
        self.assertEqual(bad['status']['code'], 'UNKNOWN')
        bad['timing_quality'] = 'MEASURED'
        self.assertTrue(validate_record(bad))

    def test_invalid_typed_values_and_resource_identity(self):
        for value in [{'type': 'int', 'value': str(2**63)}, {'type': 'bytes', 'value': '***'}, {'type': 'double', 'value': float('inf')}]:
            bad = copy.deepcopy(self.root)
            bad['attributes'] = {'bad': value}
            self.assertTrue(validate_record(bad))
        self.root['resource']['attributes']['service.name']['value'] = 'different'
        self.assertTrue(validate_record(self.root))

    def test_parent_rules_and_cycles(self):
        self.root['parent_span_id'] = self.root['span_id']
        self.root['parent_relation'] = 'EXPLICIT'
        self.assertTrue(validate_record(self.root))
        records = copy.deepcopy(self.parallel)
        records[0]['parent_span_id'] = records[1]['span_id']
        records[0]['parent_relation'] = 'EXPLICIT'
        self.assertEqual(trace_quality(records)['topology'], 'INVALID')

    def test_multiple_roots_unknown_relations_and_inference(self):
        records = copy.deepcopy(self.parallel)
        records[1]['parent_span_id'] = None
        records[1]['parent_relation'] = 'ROOT'
        self.assertEqual(trace_quality(records)['topology'], 'INVALID')
        records[1]['parent_relation'] = 'UNKNOWN'
        self.assertEqual(trace_quality(records)['topology'], 'UNKNOWN')
        records = copy.deepcopy(self.parallel)
        records[1]['parent_relation'] = 'INFERRED'
        self.assertFalse(trace_quality(records)['self_time_ready'])

    def test_unknown_status_is_not_success(self):
        self.root['status'] = {'code': 'OK', 'message': 'incorrect'}
        self.assertTrue(validate_record(self.root))

    def test_postgresql_incompatible_strings_are_rejected(self):
        for value in ['embedded\x00zero', '\ud800']:
            bad = copy.deepcopy(self.root)
            bad['source']['details'] = {'bad': value}
            self.assertTrue(validate_record(bad))

    def test_fractional_json_nanoseconds_are_rejected(self):
        self.root['duration_ns'] = 1000.5
        self.assertTrue(validate_record(self.root))

    def test_sql_syntax(self):
        try:
            from pglast import parse_sql
        except ImportError:
            self.skipTest('pglast unavailable; SQL parser check skipped')
        statements = parse_sql((ROOT / 'schema.sql').read_text(encoding='utf-8'))
        self.assertGreater(len(statements), 10)

if __name__ == '__main__':
    unittest.main(verbosity=2)
