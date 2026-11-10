"""Usability study kit: episode prep and loading, the schedule, and the session timer.
Runs on fake RCAEval cases, so no download, Jaeger or browser is needed."""
import csv
import io
import json
import os
import tempfile
import unittest
import unittest.mock
from collections import Counter

from experiments.tests.make_cases import make_case
from experiments.usability import episodes as eps
from experiments.usability import schedule as sched
from experiments.usability import session as sess


class FakeSink:
    def __init__(self):
        self.batches = []

    def send(self, batch):
        self.batches.append(list(batch))


class EpisodeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        data = os.path.join(cls.tmp.name, "data")
        cases = {"A": ("re2ob", "paymentservice"), "B": ("re2ob", "cartservice"),
                 "C": ("re2tt", "shippingservice"), "D": ("re2tt", "productcatalogservice")}
        names = {}
        for i, (x, (bench, svc)) in enumerate(cases.items()):
            names[x] = os.path.basename(make_case(data, bench, svc, "delay", 1, seed=i))
        cls.out = os.path.join(cls.tmp.name, "episodes")
        with unittest.mock.patch("sys.stdout", new=io.StringIO()):
            cls.key = eps.prepare(data, cls.out, names)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_prepare_writes_spans_sheet_and_key(self):
        for x in "ABCD":
            self.assertTrue(os.path.getsize(os.path.join(self.out, x, "spans.jsonl")) > 0)
        with open(os.path.join(self.out, "task_sheet.md")) as f:
            sheet = f.read()
        self.assertIn("Episode D", sheet)
        for ep in self.key.values():                 # the sheet must not give the answer away
            self.assertNotIn(ep["culprit"], sheet)
        self.assertEqual(self.key["A"]["culprit"], "paymentservice")

    def test_endpoint_is_not_the_culprit_and_got_slower(self):
        for ep in self.key.values():
            self.assertNotEqual(ep["endpoint_service"], ep["culprit"])
            self.assertGreater(ep["endpoint_p95_after_ms"], ep["endpoint_p95_before_ms"] * eps.MIN_RATIO)

    def test_window_keeps_whole_traces(self):
        from traceingest import spans as spanio
        with open(os.path.join(self.out, "A", "spans.jsonl")) as f:
            spans = list(spanio.read_jsonl(f))
        by = eps.by_trace(spans)
        self.assertEqual(len(by), self.key["A"]["traces"])
        for lst in by.values():
            self.assertEqual(sum(1 for s in lst if not s.parent_span_id), 1)

    def test_pick_endpoint_skips_health_checks(self):
        effects = [{"service": "x", "operation": "Check", "traces": 500, "p95_before_ms": 0.1, "p95_after_ms": 0.3, "ratio": 3},
                   {"service": "front", "operation": "home", "traces": 100, "p95_before_ms": 50, "p95_after_ms": 200, "ratio": 4},
                   {"service": "bad", "operation": "own", "traces": 900, "p95_before_ms": 50, "p95_after_ms": 900, "ratio": 18}]
        self.assertEqual(eps.pick_endpoint(effects, culprit="bad")["service"], "front")

    def test_load_moves_episode_to_now_and_fills_both_tools(self):
        now = 1_800_000_000_000_000
        sink = FakeSink()
        with tempfile.TemporaryDirectory() as dash:
            open(os.path.join(dash, "episode_A.jsonl"), "w").close()     # an earlier episode
            r = eps.load("B", self.out, tool="both", dashboard_dir=dash, now_us=now, sink=sink)
            sent = [s for b in sink.batches for s in b]
            self.assertEqual(len(sent), self.key["B"]["spans"])
            self.assertEqual(max(s.end_us for s in sent), now - 30_000_000)
            self.assertEqual(sorted(os.listdir(dash)), ["episode_B.jsonl", "rankings.json"])
            from app.store import JsonlStore
            store = JsonlStore(dash)
            self.assertEqual(store.count_traces(), self.key["B"]["traces"])
            # rankings moved by the same amount as the spans, so they line up with the traces
            with open(os.path.join(self.out, "B", "rankings.json")) as f:
                original = json.load(f)
            shift = now - 30_000_000 - self.key["B"]["end_us"]
            self.assertEqual([w["window_end_us"] for w in store.rankings(limit=100)],
                             sorted((w["window_end_us"] + shift for w in original), reverse=True))
        self.assertTrue(r["dashboard_rankings"])
        self.assertRegex(r["read_aloud"], r"became slow around \d\d:\d\d\.")

    def test_prepare_ranks_the_culprit_on_the_slow_endpoint(self):
        # fake cases: one service gets 150 ms slower after the injection
        for x in "ABCD":
            self.assertEqual(self.key[x]["rankings"]["endpoint_top_after_fault"], self.key[x]["culprit"], x)

    def test_load_refuses_a_jaeger_that_already_has_data(self):
        class Resp(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False
        opener = lambda url, timeout=5: Resp(json.dumps({"data": ["frontend", "jaeger-all-in-one"]}).encode())
        with self.assertRaises(SystemExit):
            eps.load("A", self.out, tool="jaeger", opener=opener)
        empty = lambda url, timeout=5: Resp(json.dumps({"data": ["jaeger-all-in-one"]}).encode())
        self.assertEqual(eps.jaeger_services("http://x", empty), [])


class ScheduleTests(unittest.TestCase):
    def test_balanced_over_four_participants(self):
        rows = sched.schedule(4)
        self.assertEqual(len(rows), 16)
        # every episode is done with each tool twice
        self.assertEqual(Counter((r["episode"], r["tool"]) for r in rows), {(e, t): 2 for e in "ABCD" for t in ("jaeger", "dashboard")})
        firsts = [r["tool"] for r in rows if r["order"] == 1]
        self.assertEqual(firsts, ["jaeger", "dashboard", "jaeger", "dashboard"])
        for p in ("P1", "P2", "P3", "P4"):
            mine = sched.for_participant(rows, p)
            for tool in ("jaeger", "dashboard"):
                got = sorted(r["episode"] for r in mine if r["tool"] == tool)
                self.assertEqual(len(got), 2)
                self.assertIn(got[0], "AB")      # one Online Boutique ...
                self.assertIn(got[1], "CD")      # ... and one Train Ticket per tool

    def test_cli_writes_csv(self):
        with unittest.mock.patch("sys.stdout", new=io.StringIO()) as out:
            sched.main(["2"])
        rows = list(csv.DictReader(io.StringIO(out.getvalue())))
        self.assertEqual(len(rows), 8)


class SessionTests(unittest.TestCase):
    def test_names_match_loosely(self):
        for said in ("route", "ts-route-service", "Route Service", " TS route "):
            self.assertTrue(sess.is_correct(said, "ts-route-service"), said)
        self.assertFalse(sess.is_correct("ts-travel2-service", "ts-travel-service"))
        self.assertTrue(sess.is_correct("checkout", "checkoutservice"))
        self.assertFalse(sess.is_correct("gave_up", "checkoutservice"))

    def test_session_times_and_logs_each_episode(self):
        key = {x: {"culprit": c, "endpoint": "frontend", "endpoint_service": "frontendservice"}
               for x, c in zip("ABCD", ["checkoutservice", "recommendationservice", "ts-route-service", "ts-travel-service"])}
        plan = sched.for_participant(sched.schedule(2), "P2")
        # P2: A dashboard, B Jaeger, C dashboard, D Jaeger; Jaeger episodes ask for a restart first
        replies = iter(["", "checkout", "flame graph",
                        "", "", "cart", "waterfall",
                        "", "route", "compare view",
                        "", "", "", "gave up"])
        t = iter([0, 42.5, 100, 190, 200, 230, 300, 360])
        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, "timing_log.csv")
            loaded = []
            rows = sess.run_session("P2", plan, key, log, ask=lambda _: next(replies), say=lambda _: None,
                                    clock=lambda: next(t),
                                    loader=lambda ep, tool: loaded.append((ep, tool)) or {"read_aloud": "x"})
            with open(log, newline="") as f:
                logged = list(csv.DictReader(f))
        self.assertEqual(loaded, [(r["episode"], r["tool"]) for r in plan])
        self.assertEqual([r["seconds"] for r in logged], ["42.5", "90", "30", "60"])
        self.assertEqual([r["correct"] for r in logged], ["1", "0", "1", "0"])
        self.assertEqual(logged[3]["answer"], "gave_up")
        self.assertEqual(rows[0]["tool"], "dashboard")      # P2 starts with the dashboard


if __name__ == "__main__":
    unittest.main()
