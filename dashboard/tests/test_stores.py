"""The same expectations run against both stores, so they cannot drift apart."""
import json
import os
import shutil
import tempfile
import unittest

from app.store import JsonlStore, TraceQuery

FX = os.path.join(os.path.dirname(__file__), "fixtures")


def read_fixture_spans():
    with open(os.path.join(FX, "spans.jsonl")) as f:
        return [json.loads(line) for line in f if line.strip()]


def ids(result):
    return [t["trace_id"] for t in result["items"]]


class StoreContract:
    """Mixin: subclasses set self.store in setUp."""

    def test_counts(self):
        self.assertEqual(self.store.count_traces(), 3)

    def test_services(self):
        svcs = {s["service"]: s for s in self.store.services()}
        self.assertEqual(set(svcs), {"frontend", "cartservice", "redis", "checkoutservice", "paymentservice"})
        self.assertEqual(svcs["cartservice"]["span_count"], 3)
        self.assertEqual(svcs["cartservice"]["trace_count"], 3)
        self.assertEqual(svcs["paymentservice"]["error_rate"], 1.0)
        self.assertEqual(svcs["cartservice"]["error_rate"], 0.0)       # the 'unset' span is not counted
        self.assertAlmostEqual(svcs["frontend"]["p99_ms"], 300.0)
        self.assertAlmostEqual(svcs["cartservice"]["p50_ms"], 5.0)     # nearest rank of [5, 10]; the span with no duration is skipped

    def test_operations(self):
        self.assertEqual(self.store.operations("cartservice"), ["GetCart"])
        self.assertIn("POST /checkout", self.store.operations())

    def test_search_default_newest_first(self):
        r = self.store.search_traces(TraceQuery())
        self.assertEqual(ids(r), ["t-inc", "t-slow", "t-fast"])
        self.assertEqual(r["total"], 3)

    def test_search_filters(self):
        self.assertEqual(ids(self.store.search_traces(TraceQuery(min_duration_ms=100))), ["t-slow"])
        self.assertEqual(ids(self.store.search_traces(TraceQuery(max_duration_ms=25))), ["t-inc", "t-fast"])
        self.assertEqual(ids(self.store.search_traces(TraceQuery(status="error"))), ["t-slow"])
        self.assertEqual(ids(self.store.search_traces(TraceQuery(service="redis", sort="duration_asc"))), ["t-inc", "t-fast"])
        self.assertEqual(self.store.search_traces(TraceQuery(operation="CHECKout"))["total"], 1)
        self.assertEqual(ids(self.store.search_traces(TraceQuery(complete=False))), ["t-inc"])
        self.assertEqual(ids(self.store.search_traces(TraceQuery(start_after_us=1_700_000_000_500_000, sort="start_asc"))),
                         ["t-slow", "t-inc"])

    def test_search_paging(self):
        r = self.store.search_traces(TraceQuery(sort="duration_desc", offset=1, limit=1))
        self.assertEqual(ids(r), ["t-fast"])
        self.assertEqual((r["total"], r["offset"], r["limit"]), (3, 1, 1))

    def test_trace_summary(self):
        t = self.store.get_trace("t-slow")["summary"]
        self.assertEqual((t["root_service"], t["root_operation"]), ("frontend", "POST /checkout"))
        self.assertEqual(t["duration_us"], 300_000)
        self.assertEqual((t["span_count"], t["service_count"], t["status"], t["complete"]), (4, 4, "error", True))

    def test_incomplete_trace_uses_orphan_as_root(self):
        t = self.store.get_trace("t-inc")["summary"]
        self.assertEqual(t["root_service"], "cartservice")
        self.assertFalse(t["complete"])
        self.assertEqual(t["status"], "unset")
        self.assertEqual(t["source"], "alibaba")

    def test_trace_spans_sorted_with_attributes(self):
        d = self.store.get_trace("t-inc")
        self.assertEqual([s["span_id"] for s in d["spans"]], ["c1", "c2"])
        self.assertEqual(d["spans"][0]["attributes"], {"alibaba.rpcid": "0.1"})
        self.assertIsNone(d["spans"][0]["duration_us"])

    def test_missing_trace(self):
        self.assertIsNone(self.store.get_trace("nope"))

    def test_rankings(self):
        ws = self.store.rankings()
        self.assertEqual(len(ws), 2)
        self.assertEqual(ws[0]["endpoint"], "POST /checkout")               # newest window first
        self.assertEqual([r["service"] for r in ws[0]["rankings"]], ["paymentservice", "checkoutservice"])
        self.assertEqual(ws[0]["rankings"][0]["evidence"], {"critical_path_ms_slow": 280.0})
        self.assertEqual(ws[1]["note"], "no service clearly explains a slowdown")
        self.assertEqual(len(self.store.rankings(limit=1)[0]["rankings"]), 1)
        self.assertEqual(len(self.store.rankings(endpoint="POST /checkout")), 2)   # that endpoint + the all-endpoints window
        self.assertEqual(len(self.store.rankings(endpoint="GET /other")), 1)


class JsonlStoreTests(StoreContract, unittest.TestCase):
    def setUp(self):
        self.store = JsonlStore(FX)

    def test_reload_on_change(self):
        tmp = tempfile.mkdtemp()
        try:
            shutil.copy(os.path.join(FX, "spans.jsonl"), tmp)
            store = JsonlStore(tmp)
            self.assertEqual(store.count_traces(), 3)
            with open(os.path.join(tmp, "more.jsonl"), "w") as f:
                f.write(json.dumps({"trace_id": "t-new", "span_id": "z", "parent_span_id": None, "service": "x",
                                    "operation": "y", "start_us": 1, "duration_us": 1, "status": "ok"}) + "\n")
            self.assertEqual(store.count_traces(), 4)
        finally:
            shutil.rmtree(tmp)

    def test_duplicate_span_across_files_counted_once(self):
        tmp = tempfile.mkdtemp()
        try:
            shutil.copy(os.path.join(FX, "spans.jsonl"), os.path.join(tmp, "a.jsonl"))
            shutil.copy(os.path.join(FX, "spans.jsonl"), os.path.join(tmp, "b.jsonl"))
            self.assertEqual(JsonlStore(tmp).get_trace("t-slow")["summary"]["span_count"], 4)
        finally:
            shutil.rmtree(tmp)

    def test_empty_dir(self):
        tmp = tempfile.mkdtemp()
        try:
            store = JsonlStore(tmp)
            self.assertEqual(store.count_traces(), 0)
            self.assertEqual(store.services(), [])
            self.assertEqual(store.rankings(), [])
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
