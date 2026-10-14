import io
import unittest

from traceingest import alibaba
from traceingest.spans import STATUS_UNSET, validate

from .helpers import by_rpcid, fx


class AlibabaTests(unittest.TestCase):
    def setUp(self):
        self.stats = alibaba.AlibabaStats()
        with open(fx("alibaba_small.csv")) as f:
            self.spans = alibaba.read_alibaba(f, stats=self.stats)
        self.t1 = by_rpcid([s for s in self.spans if s.trace_id == "T1"])

    def test_one_span_per_call(self):
        self.assertEqual(len(self.spans), 7)
        self.assertEqual(self.stats.duplicates_dropped, 1)
        self.assertEqual(self.stats.bad_rows, 1)

    def test_duplicate_keeps_caller_side_time(self):
        s = self.t1["0.1"]
        self.assertEqual(s.duration_us, 40_000)     # caller-side 40 ms, not callee 35 ms
        self.assertEqual(s.start_us, 1_005_000)     # earliest timestamp

    def test_parent_links_follow_rpcid(self):
        self.assertIsNone(self.t1["0"].parent_span_id)
        self.assertEqual(self.t1["0.1"].parent_span_id, self.t1["0"].span_id)
        self.assertEqual(self.t1["0.2.1"].parent_span_id, self.t1["0.2"].span_id)

    def test_service_is_callee_and_peer_is_caller(self):
        s = self.t1["0.2"]
        self.assertEqual((s.service, s.peer_service, s.operation), ("svcC", "svcA", "price"))
        self.assertEqual(self.t1["0.2.1"].operation, "db")   # no interface -> rpc type

    def test_negative_rt_is_missing_duration(self):
        self.assertIsNone(self.t1["0.3"].duration_us)
        self.assertEqual(self.stats.missing_rt, 1)

    def test_orphans_counted(self):
        self.assertEqual(self.stats.orphans, 2)       # T2 has calls whose parent 0.1 was not logged

    def test_status_unset_and_valid(self):
        for s in self.spans:
            self.assertEqual(s.status, STATUS_UNSET)
            self.assertEqual(validate(s), [])

    def test_base_epoch(self):
        with open(fx("alibaba_small.csv")) as f:
            spans = alibaba.read_alibaba(f, base_epoch_us=1_625_097_600_000_000)
        self.assertEqual(by_rpcid(spans)["0"].start_us, 1_625_097_600_000_000 + 1_000_000)

    def test_real_sample_rows(self):
        stats = alibaba.AlibabaStats()
        with open(fx("alibaba_real_rows.csv")) as f:
            spans = alibaba.read_alibaba(f, stats=stats)
        self.assertEqual(len(spans), 4)
        self.assertTrue(all(s.service.startswith("b1dbd3a6") for s in spans))
        self.assertEqual(stats.orphans, 4)            # 4 sampled rows, no parents included

    def test_rejects_other_csv(self):
        with self.assertRaises(ValueError):
            alibaba.read_alibaba(io.StringIO("a,b,c\n1,2,3\n"))


if __name__ == "__main__":
    unittest.main()
