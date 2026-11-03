import copy
import unittest

from diagnosis.demo import records
from diagnosis.episodes import diagnose


class EpisodeTests(unittest.TestCase):
    def test_known_excess_collapses_to_normal_and_has_support(self):
        result = diagnose(records(), assume_synchronous=True)
        row = result['windows'][0]['rankings'][0]
        self.assertEqual(row['service'], 'shop/payment')
        self.assertEqual(row['excess_ms'], 200)
        self.assertEqual(row['adjusted_slow_duration_ms'], row['normal_duration_ms'])
        self.assertEqual(row['gap_reduction_fraction'], 1)
        self.assertEqual(len(row['supporting_trace_ids']), 5)

    def test_default_requires_explicit_assumption(self):
        result = diagnose(records())
        self.assertFalse(result['windows'])
        self.assertEqual(sum(result['excluded_traces'].values()), 40)

    def test_constant_latency_has_no_diagnosis(self):
        result = diagnose(list(records())[:60], assume_synchronous=True)
        self.assertFalse(result['windows'][0]['rankings'])
        self.assertEqual(result['windows'][0]['note'], 'No separable tail episode.')

    def test_runs_are_isolated_and_input_order_does_not_matter(self):
        original = list(records())
        other = copy.deepcopy(original)
        for span in other:
            span['run_id'] = '00000000-0000-4000-8000-000000000002'
        a = diagnose(original + other, assume_synchronous=True)
        b = diagnose(reversed(original + other), assume_synchronous=True)
        self.assertEqual(a, b)
        self.assertEqual(len(a['windows']), 2)

    def test_duration_only_data_is_excluded(self):
        data = list(records())
        for span in data:
            span.update(start_time_ns=None, end_time_ns=None, timing_quality='DURATION_ONLY', time_domain='UNKNOWN')
        result = diagnose(data, assume_synchronous=True)
        self.assertFalse(result['windows'])
        self.assertEqual(sum(result['excluded_traces'].values()), 40)

    def test_small_window_reports_insufficient_evidence(self):
        result = diagnose(list(records())[:4], assume_synchronous=True)
        self.assertIn('Insufficient', result['windows'][0]['note'])

    def test_alert_compares_adjacent_windows_not_first_observation(self):
        baseline = list(records())[:60]
        later = list(records())
        for span in later:
            span['trace_id'] = f'{int(span["trace_id"], 16) + 100:032x}'
            for key in ('start_time_ns', 'end_time_ns'):
                span[key] = str(int(span[key]) + 300_000_000_000)
        result = diagnose(baseline + later, assume_synchronous=True)
        self.assertFalse(result['windows'][0]['alerts'])
        self.assertEqual(result['windows'][1]['alerts'][0]['service'], 'shop/payment')
