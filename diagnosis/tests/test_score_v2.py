import copy
import unittest

from diagnosis.demo import records
from diagnosis.score_v2 import diagnose, bounded_excess, combine


def captures():
    data = list(records())
    for span in data:
        span['status'] = {'code': 'OK', 'message': None}
    return data[:60], data[60:]


class ScoreV2Tests(unittest.TestCase):
    def test_invalid_conflicting_and_missing_parent_records_are_excluded(self):
        base, target = captures()
        for change in ('invalid', 'conflict', 'missing'):
            damaged = copy.deepcopy(target)
            if change == 'invalid':
                damaged[0]['schema_version'] = 'bad'
            elif change == 'conflict':
                extra = copy.deepcopy(damaged[0])
                extra['operation_name'] = 'conflict'
                damaged.append(extra)
            else:
                damaged[1]['parent_span_id'] = '0000000000000099'
            result = diagnose(base, damaged, assume_synchronous=True)
            self.assertTrue(result['excluded_traces'], change)
            if change == 'conflict':
                self.assertLess(result['windows'][0]['rankings'][0]['confidence']['eligible_fraction'], 1)

    def test_configuration_rejects_invalid_weights(self):
        base, target = captures()
        for weights in ({}, {'latency': 1}):
            with self.assertRaises(ValueError):
                diagnose(base, target, weights=weights)
        with self.assertRaises(ValueError):
            diagnose(base, target, timing_policy='silently-measured')

    def test_normalization(self):
        self.assertEqual(bounded_excess(5, 10, 1), 0)
        self.assertEqual(bounded_excess(20, 10, 1), .5)
        self.assertLess(bounded_excess(10**9, 0, 1), 1)

    def test_missing_weight_is_renormalized_not_zero(self):
        signals = {'a': {'normalized': .8}, 'b': {'normalized': None}}
        score, weights = combine(signals, {'a': .25, 'b': .75})
        self.assertEqual(score, .8)
        self.assertEqual(weights, {'a': 1, 'b': 0})
        self.assertIsNone(combine({'a': {'normalized': None}}, {'a': 1})[0])

    def test_all_four_signals_and_evidence(self):
        base, target = captures()
        for span in target:
            if span['service_name'] == 'payment':
                span['status']['code'] = 'ERROR'
        report = diagnose(base, target, assume_synchronous=True)
        payment = next(r for r in report['windows'][0]['rankings'] if r['service_name'] == 'payment')
        for signal in payment['signals'].values():
            self.assertEqual(signal['availability'], 'available')
            self.assertGreater(signal['normalized'], 0)
            self.assertTrue(signal['evidence'][0]['span_ids'])
        self.assertEqual(payment['signals']['error']['raw'], 1.0)
        self.assertFalse(payment['confidence']['is_probability'])

    def test_unknown_status_is_missing(self):
        base, target = captures()
        for span in base + target:
            span['status']['code'] = 'UNSET'
        result = diagnose(base, target)
        for row in result['windows'][0]['rankings']:
            self.assertIsNone(row['signals']['error']['normalized'])
            self.assertEqual(row['effective_weights']['error'], 0)
            self.assertAlmostEqual(sum(row['effective_weights'].values()), 1)

    def test_low_samples_and_no_reference_preserve_candidates(self):
        base, target = captures()
        result = diagnose(base[:2], target[:2])
        self.assertTrue(result['windows'][0]['rankings'])
        self.assertTrue(all(r['confidence']['label'] == 'low' for r in result['windows'][0]['rankings']))
        result = diagnose([], target)
        self.assertTrue(all(r['score'] is None for r in result['windows'][0]['rankings']))

    def test_derived_policy_preserves_labels(self):
        base, target = captures()
        for span in base + target:
            span['timing_quality'] = 'DERIVED'
        original = copy.deepcopy(base + target)
        self.assertEqual(diagnose(base, target)['windows'], [])
        result = diagnose(base, target, timing_policy='measured-or-derived')
        self.assertTrue(result['windows'])
        self.assertEqual(result['timing_qualities'], ['DERIVED'])
        self.assertEqual(base + target, original)

    def test_uncontained_intervals_only_lose_critical_path(self):
        base, target = captures()
        for span in target:
            if span['service_name'] == 'payment':
                span['end_time_ns'] = str(int(span['end_time_ns']) + 500_000_000)
                span['duration_ns'] = str(int(span['duration_ns']) + 500_000_000)
        result = diagnose(base, target, assume_synchronous=True)
        for row in result['windows'][0]['rankings']:
            self.assertIsNone(row['signals']['critical_path']['normalized'])
            self.assertIsNotNone(row['signals']['latency']['normalized'])
            self.assertLess(row['confidence']['trace_quality'], 1)

    def test_caller_gap_is_not_invented_as_callee_work(self):
        base, target = captures()
        for span in target:
            if span['service_name'] == 'payment':
                span['duration_ns'] = '60000000'
                span['end_time_ns'] = str(int(span['start_time_ns']) + 60_000_000)
        report = diagnose(base, target, assume_synchronous=True)
        payment = next(r for r in report['windows'][0]['rankings'] if r['service_name'] == 'payment')
        self.assertEqual(payment['signals']['latency']['normalized'], 0)
        self.assertEqual(payment['signals']['critical_path']['normalized'], 0)

    def test_reproducible_with_duplicate_retries(self):
        base, target = captures()
        a = diagnose(base, target, assume_synchronous=True)
        b = diagnose(list(reversed(base)), list(reversed(target)), assume_synchronous=True)
        self.assertEqual(a, b)
        c = diagnose(base, target + target, assume_synchronous=True)
        self.assertEqual(a['windows'], c['windows'])
        with self.assertRaises(ValueError):
            diagnose(base, base)

    def test_error_only_fault_without_latency_anomaly(self):
        base, target = captures()
        for span in target:
            span['duration_ns'] = '100000000' if span['parent_relation'] == 'ROOT' else '60000000'
            span['end_time_ns'] = str(int(span['start_time_ns']) + int(span['duration_ns']))
            if span['service_name'] == 'payment':
                span['status']['code'] = 'ERROR'
        result = diagnose(base, target)
        row = result['windows'][0]['rankings'][0]
        self.assertEqual(row['service_name'], 'payment')
        self.assertEqual(row['signals']['latency']['normalized'], 0)
        self.assertEqual(row['signals']['error']['normalized'], 1)


if __name__ == '__main__':
    unittest.main()
