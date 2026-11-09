import copy
from contextlib import closing
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from diagnosis.demo import records
from diagnosis.reliability import Policy, analyze, observations, persist


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        data = list(records())
        for span in data:
            span['status'] = {'code': 'OK', 'message': None}
        self.baseline, self.target = data[:40], data[40:]

    def report(self, **kwargs):
        return analyze(self.baseline, self.target, application='shop', environment='test', synthetic=True, **kwargs)

    def endpoint(self, result):
        return next(w for w in result['windows'] if w['scope'] == 'endpoint')

    def test_baseline_prevalence_and_persistence_versions(self):
        result = self.report()
        window = self.endpoint(result)
        self.assertEqual(window['slow_trace_count'], 5)
        self.assertEqual(window['slow_prevalence'], .25)
        baseline = next(b for b in result['baselines'] if b['scope'] == 'endpoint')
        self.assertEqual(baseline['median_ns'], 100_000_000)
        self.assertEqual(baseline['p95_ns'], 100_000_000)
        self.assertIsNone(baseline['p99_ns'])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'baseline.sqlite'
            first = persist(path, result)
            self.assertEqual(persist(path, result), first)
            changed = self.report(policy=Policy(slow_ratio=2))
            self.assertNotEqual(persist(path, changed), first)
            with closing(sqlite3.connect(path)) as conn:
                self.assertEqual(conn.execute('SELECT count(*) FROM service_baselines').fetchone()[0], 6)

    def test_error_evidence_and_unknown_status(self):
        for span in self.target:
            if span['parent_relation'] != 'ROOT':
                span['status'] = {'code': 'ERROR', 'message': 'injected'}
        window = self.endpoint(self.report())
        self.assertTrue(window['error_anomaly'])
        self.assertEqual(window['error_rate'], 1)
        self.assertEqual(len(window['error_evidence']), 20)
        for span in self.target:
            span['status'] = {'code': 'UNKNOWN', 'message': None}
        window = self.endpoint(self.report())
        self.assertIsNone(window['error_rate'])
        self.assertIsNone(window['error_anomaly'])
        self.assertEqual(window['status_unknown_count'], 20)

    def test_sparse_zero_and_unavailable_are_distinct(self):
        self.target = self.target[:20]
        self.assertEqual(self.endpoint(self.report())['slow_prevalence'], 0)
        self.target = self.target[:2]
        self.assertEqual(self.endpoint(self.report())['availability'], 'insufficient_samples')
        self.target[1]['parent_span_id'] = '0000000000000099'
        window = self.endpoint(self.report())
        self.assertEqual(window['availability'], 'unavailable')
        self.assertEqual(window['excluded_trace_count'], 1)
        self.assertIsNone(window['slow_prevalence'])

    def test_integrated_duplicate_conflict_missing_parent_and_invalid(self):
        duplicate = copy.deepcopy(self.target[0])
        duplicate['received_at_unix_ns'] = str(int(duplicate['received_at_unix_ns']) + 1)
        duplicate['source']['raw_reference'] = 'retry'
        self.target.append(duplicate)
        self.assertEqual(self.endpoint(self.report())['eligible_trace_count'], 20)
        conflict = copy.deepcopy(self.target[2])
        conflict['operation_name'] = 'conflict'
        self.target.append(conflict)
        self.target[5]['parent_span_id'] = '0000000000000099'
        self.target[7]['duration_ns'] = '-1'
        result = self.report()
        self.assertEqual(self.endpoint(result)['eligible_trace_count'], 17)
        self.assertEqual(len(result['conflicts']), 1)
        self.assertEqual(len(result['invalid_records']), 1)
        self.assertEqual({e['reason'] for e in result['excluded_traces']},
                         {'conflicting_records', 'topology_incomplete', 'invalid_canonical_record'})
        self.assertEqual(result['conflicts'][0]['accepted']['operation_name'], self.target[2]['operation_name'])

    def test_late_span_changes_snapshot_identity(self):
        first = self.report()
        late = copy.deepcopy(self.baseline[1])
        late['span_id'] = '0000000000000099'
        self.baseline.append(late)
        self.assertNotEqual(first['baselines'][0]['baseline_id'], self.report()['baselines'][0]['baseline_id'])

    def test_invalid_records_never_reach_reconstruction(self):
        rows, excluded, invalid, _ = observations([None, {'run_id': 'bad', 'trace_id': 'bad'}])
        self.assertFalse(rows)
        self.assertEqual(len(invalid), 2)
        self.assertEqual(len(excluded), 1)

    def test_synthetic_and_overlap_guards(self):
        with self.assertRaisesRegex(ValueError, 'synthetic'):
            analyze(self.baseline, self.target, application='shop', environment='test')
        with self.assertRaisesRegex(ValueError, 'disjoint'):
            analyze(self.baseline, self.baseline, application='shop', environment='test', synthetic=True)

    def test_duration_only_is_retained_as_unavailable(self):
        for span in self.target:
            span.update(start_time_ns=None, end_time_ns=None, timing_quality='DURATION_ONLY', time_domain='UNKNOWN')
        result = self.report()
        self.assertFalse(result['windows'])
        self.assertEqual(len(result['excluded_traces']), 20)
        self.assertEqual(result['excluded_traces'][0]['reason'], 'requires_measured_unix_intervals')

    def test_cli_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            for name, data in [('baseline', self.baseline), ('evaluation', self.target)]:
                (output / f'{name}.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in data), encoding='utf-8')
            subprocess.run([sys.executable, '-m', 'diagnosis.reliability_cli', str(output / 'evaluation.jsonl'),
                            '--baseline', str(output / 'baseline.jsonl'), '--application', 'shop',
                            '--environment', 'test', '--synthetic', '--output', str(output)],
                           check=True, capture_output=True)
            report = json.loads((output / 'reliability.json').read_text())
            self.assertEqual(self.endpoint(report)['slow_prevalence'], .25)
            self.assertEqual(len(report['input_files']), 2)
            self.assertTrue((output / 'baselines.sqlite').exists())

    def test_fault_report_cli_with_manufactured_test_capture(self):
        # Temporary protocol fixture only; never saved as real experiment evidence.
        data = copy.deepcopy(self.baseline[:20])
        injection = int(data[10]['start_time_ns'])
        for i, span in enumerate(data):
            span['quality_flags'] = []
            if i >= 10 and span['service_name'] == 'payment':
                span['status'] = {'code': 'ERROR', 'message': 'test fixture'}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            manifest = {'run_id': data[0]['run_id'], 'root_cause_service': 'payment',
                        'injected_at_unix_ns': str(injection),
                        'cleared_at_unix_ns': str(int(data[-1]['end_time_ns']) + 1_000_000_000),
                        'minimum_healthy_checkout_traces': 5, 'minimum_faulty_checkout_traces': 5}
            (path / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
            (path / 'capture.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in data), encoding='utf-8')
            subprocess.run([sys.executable, '-m', 'diagnosis.fault_report', '--runtime', str(path),
                            '--input', str(path / 'capture.jsonl'), '--output', str(path / 'report')],
                           check=True, capture_output=True)
            report = json.loads((path / 'report/reliability.json').read_text())
            self.assertEqual(report['diagnosis']['top_ranked_services'], ['payment'])
            self.assertTrue(report['diagnosis']['injected_service_among_top'])
            self.assertEqual(report['verification']['healthy_checkout_traces'], 5)
            self.assertEqual(len(report['diagnosis']['rankings'][0]['error_evidence']), 5)


if __name__ == '__main__':
    unittest.main()
