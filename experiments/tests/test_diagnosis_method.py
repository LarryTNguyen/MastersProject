"""The diagnosis package's ranking, scored like the baselines."""
import os
import tempfile
import unittest

from experiments import diagnosis_method as dm
from experiments.rcaeval_eval import evaluate_case
from experiments.tests.make_cases import make_case


class RankWindowsTests(unittest.TestCase):
    def test_weights_by_slow_traces_and_skips_windows_before_the_fault(self):
        windows = [
            {"window_start_ns": "0", "window_end_ns": "300", "slow_traces": 50,
             "rankings": [{"service": "ns/early", "service_name": "early", "score": 1.0}]},
            {"window_start_ns": "300", "window_end_ns": "600", "slow_traces": 10,
             "rankings": [{"service_name": "a", "service": "a", "score": 0.9},
                          {"service_name": "b", "service": "b", "score": 0.5}]},
            {"window_start_ns": "600", "window_end_ns": "900", "slow_traces": 40,
             "rankings": [{"service_name": "b", "service": "b", "score": 0.5}]},
            {"window_start_ns": "900", "window_end_ns": "1200", "slow_traces": 0, "rankings": []},
        ]
        self.assertEqual(dm.rank_windows(windows, from_ns=450), ["b", "a"])   # b: 5 + 20, a: 9
        self.assertEqual(dm.rank_windows(windows, from_ns=0)[0], "early")


class CaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.case = make_case(cls.tmp.name, "re2ob", "paymentservice", "delay", 1, traces=200)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_finds_the_delayed_service(self):
        rows = evaluate_case(self.case, {"tail_share_excess": dm.tail_share_excess})
        self.assertEqual(rows[0]["rank"], 1, rows[0]["top3"])
        self.assertGreater(dm.LAST["windows_after_ranked"], 0)

    def test_without_accepting_derived_intervals_nothing_is_eligible(self):
        from experiments.rcaeval_eval import split_case
        from traceingest import rcaeval
        spans, label = rcaeval.read_case(self.case)
        normal, anomalous = split_case([s.to_dict() for s in spans], label.inject_time_s)
        self.assertEqual(dm.tail_share_excess(normal, anomalous, accept_derived=False), [])
        self.assertIn("requires_measured_unix_intervals", dm.LAST["excluded_traces"])


if __name__ == "__main__":
    unittest.main()
