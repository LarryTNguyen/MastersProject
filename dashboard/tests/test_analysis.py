import unittest

from app import analysis

T0 = 1_000_000


def sp(sid, parent, svc, start, dur, op="op", attrs=None):
    return {"trace_id": "t", "span_id": sid, "parent_span_id": parent, "service": svc, "operation": op,
            "start_us": T0 + start, "duration_us": dur, "status": "ok", "attributes": attrs or {}}


# root 0..100ms; child A 10..40, child B 30..70 (overlaps A), grandchild under B 35..45
TREE = [sp("r", None, "frontend", 0, 100_000), sp("a", "r", "cart", 10_000, 30_000),
        sp("b", "r", "payment", 30_000, 40_000), sp("c", "b", "redis", 35_000, 10_000)]


class SelfTimeTests(unittest.TestCase):
    def test_overlapping_children_counted_once(self):
        st = analysis.self_times(TREE)
        self.assertEqual(st["r"], 100_000 - 60_000)    # children cover 10..70 = 60ms
        self.assertEqual(st["a"], 30_000)
        self.assertEqual(st["b"], 40_000 - 10_000)
        self.assertEqual(st["c"], 10_000)

    def test_missing_duration(self):
        spans = TREE + [sp("d", "r", "x", 80_000, None)]
        st = analysis.self_times(spans)
        self.assertIsNone(st["d"])
        self.assertEqual(st["r"], 40_000)           # a span with no duration covers nothing

    def test_child_outside_parent_is_clipped(self):
        spans = [sp("r", None, "f", 0, 10_000), sp("a", "r", "g", 5_000, 20_000)]   # child runs past parent
        self.assertEqual(analysis.self_times(spans)["r"], 5_000)


class CriticalPathTests(unittest.TestCase):
    def test_follows_latest_finishing_child(self):
        path = [s["span_id"] for s in analysis.critical_path(TREE)]
        self.assertEqual(path, ["r", "b", "c"])    # b ends at 70 > a at 40

    def test_share_per_service_adds_up_to_root(self):
        share = analysis.critical_path_share(TREE)
        self.assertEqual(share, {"frontend": 60_000, "payment": 30_000, "redis": 10_000})
        self.assertEqual(sum(share.values()), 100_000)

    def test_sequential_children_all_on_path(self):
        spans = [sp("r", None, "f", 0, 100_000), sp("a", "r", "x", 5_000, 30_000),
                 sp("b", "r", "y", 40_000, 30_000), sp("c", "r", "z", 75_000, 20_000)]
        self.assertEqual([s["span_id"] for s in analysis.critical_path(spans)], ["r", "c", "b", "a"])
        share = analysis.critical_path_share(spans)
        self.assertEqual(share, {"f": 20_000, "x": 30_000, "y": 30_000, "z": 20_000})

    def test_overlapping_children_only_last_on_path(self):
        spans = [sp("r", None, "f", 0, 100_000), sp("a", "r", "x", 5_000, 50_000), sp("b", "r", "y", 30_000, 50_000)]
        self.assertEqual([s["span_id"] for s in analysis.critical_path(spans)], ["r", "b"])

    def test_orphan_root(self):
        spans = [sp("a", "gone", "x", 0, 5_000), sp("b", "a", "y", 1_000, 1_000)]
        self.assertEqual([s["span_id"] for s in analysis.critical_path(spans)], ["a", "b"])

    def test_empty(self):
        self.assertEqual(analysis.critical_path([]), [])


def trace(i, pay_ms):
    spans = [sp("r", None, "frontend", 0, (20 + pay_ms) * 1000),
             sp("p", "r", "payment", 5_000, pay_ms * 1000), sp("c", "r", "cart", 2_000, 2_000)]
    for s in spans:
        s["trace_id"] = f"t{i}"
    summary = {"trace_id": f"t{i}", "duration_us": (20 + pay_ms) * 1000, "root_operation": "checkout"}
    return summary, spans


class CompareTests(unittest.TestCase):
    def setUp(self):
        # 18 normal traces with a 10ms payment, 2 slow ones with 200ms
        self.pairs = [trace(i, 10) for i in range(18)] + [trace(100 + i, 200) for i in range(2)]

    def test_split_by_fraction(self):
        slow, normal = analysis.split_slow(self.pairs)
        self.assertEqual((len(slow), len(normal)), (2, 18))

    def test_split_by_threshold(self):
        slow, normal = analysis.split_slow(self.pairs, threshold_ms=100)
        self.assertEqual((len(slow), len(normal)), (2, 18))

    def test_payment_is_the_difference(self):
        c = analysis.compare(self.pairs)
        top = c["services"][0]
        self.assertEqual(top["service"], "payment")
        self.assertAlmostEqual(top["slow_self_ms_p50"], 200.0)
        self.assertAlmostEqual(top["normal_self_ms_p50"], 10.0)
        self.assertAlmostEqual(top["added_ms"], 190.0)
        self.assertAlmostEqual(top["ratio"], 20.0)
        cart = next(r for r in c["services"] if r["service"] == "cart")
        self.assertAlmostEqual(cart["added_ms"], 0.0)
        self.assertEqual((c["slow_traces"], c["normal_traces"]), (2, 18))
        self.assertAlmostEqual(c["threshold_ms"], 220.0)

    def test_empty(self):
        self.assertEqual(analysis.compare([])["services"], [])

    def test_too_few_traces_means_no_slow_set(self):
        slow, normal = analysis.split_slow(self.pairs[:5])
        self.assertEqual((len(slow), len(normal)), (0, 5))


class FlameTests(unittest.TestCase):
    def test_merge(self):
        f = analysis.flame([spans for _, spans in [trace(1, 10), trace(2, 30)]])
        self.assertEqual(f["count"], 2)
        root = f["children"][0]
        self.assertEqual(root["name"], "frontend op")
        self.assertEqual(root["count"], 2)
        names = {c["name"]: c for c in root["children"]}
        self.assertAlmostEqual(names["payment op"]["value"], 40.0)         # 10 + 30
        self.assertAlmostEqual(names["cart op"]["value"], 4.0)
        self.assertAlmostEqual(root["total"], root["value"] + 40.0 + 4.0)
        self.assertEqual(root["children"][0]["name"], "payment op")         # biggest first

    def test_depth_limit(self):
        spans = [sp("s0", None, "a", 0, 100)]
        for i in range(1, 30):
            spans.append(sp(f"s{i}", f"s{i-1}", "a", i, 100 - i))
        node = analysis.flame([spans], max_depth=3)
        depth = 0
        while node["children"]:
            node = node["children"][0]
            depth += 1
        self.assertEqual(depth, 3)


class PipelineTests(unittest.TestCase):
    def test_stats(self):
        now = T0 + 10_000_000
        spans = [sp("a", None, "x", 0, 1_000, attrs={"sta.received_us": T0 + 2_000_000, "sta.normalized_us": T0 + 3_000_000}),
                 sp("b", None, "x", 0, 1_000, attrs={"sta.normalized_us": T0 + 5_000_000}),
                 sp("c", None, "x", 0, 1_000)]
        traces = [{"complete": True}, {"complete": False}]
        p = analysis.pipeline_stats(spans, traces, now, window_s=60)
        self.assertEqual((p["spans"], p["spans_with_stamps"], p["traces"]), (3, 2, 2))
        self.assertEqual(p["incomplete_rate"], 0.5)
        self.assertAlmostEqual(p["staleness_ms"]["p50"], 2999.0)           # (T0+3s) - (T0+1ms)
        self.assertAlmostEqual(p["staleness_ms"]["p95"], 4999.0)
        self.assertAlmostEqual(p["lag_ms"]["p50"], 1000.0)
        self.assertAlmostEqual(p["seconds_since_last"], 5.0)
        self.assertEqual(p["ingest_rate_per_min"], 2.0)
        self.assertEqual(sum(b["spans"] for b in p["series"]), 2)

    def test_no_stamps(self):
        p = analysis.pipeline_stats([sp("a", None, "x", 0, 1)], [], T0, 60)
        self.assertIsNone(p["staleness_ms"]["p50"])
        self.assertIsNone(p["seconds_since_last"])


if __name__ == "__main__":
    unittest.main()
