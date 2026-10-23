import csv
import json
import os
import shutil
import tempfile
import unittest

from experiments import baselines, metrics, rcaeval_eval, run
from experiments.tests.make_cases import make_suite
from experiments.usability.usability_stats import summarize as usab


class MetricsTests(unittest.TestCase):
    def test_rank_of_matches_loosely(self):
        self.assertEqual(metrics.rank_of("checkoutservice", ["cart", "checkout-service", "x"]), 2)
        self.assertEqual(metrics.rank_of("CheckoutService", ["checkoutservice"]), 1)
        self.assertIsNone(metrics.rank_of("payment", ["cart"]))
        self.assertIsNone(metrics.rank_of("ts", ["ts-auth-service"]))      # too short to substring-match

    def test_summary(self):
        s = metrics.summarize([1, 3, None, 2])
        self.assertEqual((s["acc@1"], s["acc@3"], s["not_found"]), (0.25, 0.75, 1))
        self.assertAlmostEqual(s["mrr"], (1 + 1 / 3 + 0 + 1 / 2) / 4)
        self.assertEqual(metrics.summarize([])["cases"], 0)


class BaselineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.cases = make_suite(cls.tmp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def test_delay_cases_found_by_self_time_baselines(self):
        methods = {"longest_span": baselines.longest_span, "critical_path_only": baselines.critical_path_only}
        for case in self.cases:
            if "_error_" in case:
                continue
            with self.subTest(case=os.path.basename(case)):
                for r in rcaeval_eval.evaluate_case(case, methods):
                    self.assertEqual(r["rank"], 1, f"{r['method']} ranked {r['top3']} for {r['truth']}")

    def test_slowest_average_is_fooled_by_the_root_span(self):
        # root span includes all its children, so this one blames the frontend
        rows = rcaeval_eval.evaluate_case(self.cases[3], {"slowest_average": baselines.slowest_average})
        self.assertEqual(rows[0]["top3"].split(";")[0], "frontend")
        self.assertNotEqual(rows[0]["rank"], 1)

    def test_split_uses_inject_time(self):
        rows = rcaeval_eval.evaluate_case(self.cases[0], {"slowest_average": baselines.slowest_average})
        self.assertEqual(rows[0]["normal_traces"] + rows[0]["anomalous_traces"], 120)
        self.assertGreater(rows[0]["normal_traces"], 50)

    def test_evaluate_suite_and_summary(self):
        res = rcaeval_eval.evaluate(self.cases, ["slowest_average", "longest_span", "critical_path_only"], log=lambda *_: None)
        s = res["summary"]["critical_path_only"]
        self.assertEqual(s["cases"], 5)
        self.assertGreaterEqual(s["acc@1"], 0.8)          # the error case may rank elsewhere
        self.assertLess(res["summary"]["slowest_average"]["acc@1"], s["acc@1"])
        self.assertIn("delay", s["by_fault"])
        self.assertIn("re2ob", s["by_benchmark"])
        self.assertEqual(res["failed"], [])

    def test_parallel_workers_give_the_same_answer(self):
        methods = ["slowest_average", "critical_path_only"]
        one = rcaeval_eval.evaluate(self.cases, methods, log=lambda *_: None, workers=1)
        two = rcaeval_eval.evaluate(self.cases, methods, log=lambda *_: None, workers=2)
        self.assertEqual(one["rows"], two["rows"])
        self.assertEqual(one["summary"], two["summary"])

    def test_bad_case_is_skipped_not_fatal(self):
        bad = os.path.join(self.tmp, "re2ob_nothing_delay_9")
        os.makedirs(bad, exist_ok=True)
        res = rcaeval_eval.evaluate([bad, self.cases[0]], ["slowest_average"], log=lambda *_: None)
        self.assertEqual(len(res["failed"]), 1)
        self.assertEqual(res["summary"]["slowest_average"]["cases"], 1)

    def test_load_method_by_path(self):
        fn = baselines.load_method("experiments.baselines:critical_path_only")
        self.assertIs(fn, baselines.critical_path_only)
        with self.assertRaises(ValueError):
            baselines.load_method("nope")


class UsabilityTests(unittest.TestCase):
    def test_summary_and_pairing(self):
        rows = [
            {"participant": "P1", "tool": "jaeger", "seconds": "120", "answer": "cart", "correct": "0"},
            {"participant": "P1", "tool": "dashboard", "seconds": "40", "answer": "payment", "correct": "1"},
            {"participant": "P2", "tool": "jaeger", "seconds": "90", "answer": "payment", "correct": "1"},
            {"participant": "P2", "tool": "dashboard", "seconds": "50", "answer": "payment", "correct": "1"},
            {"participant": "P3", "tool": "jaeger", "seconds": "200", "answer": "gave_up", "correct": "0"},
        ]
        s = usab(rows)
        self.assertEqual(s["tools"]["jaeger"]["n"], 3)
        self.assertEqual(s["tools"]["jaeger"]["gave_up"], 1)
        self.assertEqual(s["tools"]["dashboard"]["accuracy"], 1.0)
        self.assertEqual(s["paired_n"], 2)
        self.assertEqual(s["faster_with_dashboard"], 2)
        self.assertAlmostEqual(s["paired_mean_diff_s"], ((40 - 120) + (50 - 90)) / 2)


class RunnerTests(unittest.TestCase):
    def test_end_to_end(self):
        tmp = tempfile.mkdtemp()
        try:
            cases_dir = os.path.join(tmp, "cases")
            make_suite(cases_dir)
            log = os.path.join(tmp, "timing_log.csv")
            with open(log, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["participant", "tool", "episode", "order", "seconds", "answer", "correct", "notes"])
                w.writerow(["P1", "jaeger", "A", 1, 100, "cart", 0, ""])
                w.writerow(["P1", "dashboard", "B", 2, 35, "payment", 1, ""])
            cost = os.path.join(tmp, "cost.csv")
            with open(cost, "w") as f:
                f.write("name,usd_month\nserverless (idle),1.40\nalways-on EC2 + RDS,61.20\n")
            fresh = os.path.join(tmp, "freshness.json")
            json.dump({"staleness_ms": {"p50": 3000, "p95": 9000, "p99": 12000}, "incomplete_rate": 0.01,
                       "staleness_ms_values": [1000 * i for i in range(1, 40)]}, open(fresh, "w"))
            cfg = os.path.join(tmp, "config.json")
            json.dump({"out": os.path.join(tmp, "results"),
                       "rcaeval": {"cases": [os.path.join(cases_dir, "re2ob_*")], "methods": ["slowest_average", "critical_path_only"]},
                       "freshness": {"file": fresh}, "usability_log": log, "cost_csv": cost}, open(cfg, "w"))
            out = run.run(cfg, log=lambda *_: None)
            files = set(os.listdir(out))
            self.assertTrue({"config.json", "rcaeval_results.csv", "rcaeval_summary.json", "freshness.json",
                             "usability_summary.json", "cost.csv", "summary.md", "figures"} <= files, files)
            figs = set(os.listdir(os.path.join(out, "figures")))
            self.assertEqual(figs, {"rcaeval_accuracy.png", "rcaeval_by_fault.png", "freshness_cdf.png", "usability.png", "cost.png"})
            md = open(os.path.join(out, "summary.md")).read()
            self.assertIn("| critical_path_only | 5 |", md)
            self.assertIn("always-on EC2 + RDS: $61.20/month", md)
            self.assertTrue(os.path.exists(os.path.join(tmp, "results", "latest", "summary.md")))
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
