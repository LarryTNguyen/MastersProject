import io
import json
import os
import unittest
from unittest import mock

from diagnosis import lambda_handler
from diagnosis.demo import records


class FakeS3:
    def __init__(self, objects):
        self.objects = objects

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}


def jsonl(rows):
    return "\n".join(json.dumps(r) for r in rows).encode()


class LambdaHandlerTests(unittest.TestCase):
    def setUp(self):
        self.rows = list(records())
        self.s3 = FakeS3({("raw", "normalized/uploads/demo.jsonl"): jsonl(self.rows)})
        published = mock.patch.object(lambda_handler, "publish", side_effect=lambda conn, result: len(result["windows"]))
        self.publish = published.start()
        self.addCleanup(published.stop)
        env = mock.patch.dict(os.environ, {"ASSUME_SYNCHRONOUS": "true", "DATA_LABEL": "synthetic", "DIAGNOSIS_METHOD": "v1"})
        env.start()
        self.addCleanup(env.stop)

    def test_direct_event_diagnoses_and_publishes(self):
        out = lambda_handler.handler({"bucket": "raw", "key": "normalized/uploads/demo.jsonl"}, s3=self.s3, conn=object())
        self.assertTrue(out["ok"])
        self.assertGreater(out["windows"], 0)
        result = self.publish.call_args[0][1]
        self.assertEqual(result["data_label"], "synthetic")
        self.assertEqual(result["input_files"][0]["path"], "s3://raw/normalized/uploads/demo.jsonl")
        self.assertEqual(result["windows"][0]["rankings"][0]["service_name"], "payment")

    def test_sqs_messages_and_per_message_failures(self):
        good = {"messageId": "1", "body": json.dumps({"Records": [
            {"s3": {"bucket": {"name": "raw"}, "object": {"key": "normalized/uploads/demo.jsonl"}}}]})}
        bad = {"messageId": "2", "body": json.dumps({"Records": [
            {"s3": {"bucket": {"name": "raw"}, "object": {"key": "uploads/not-normalized.json"}}}]})}
        out = lambda_handler.handler({"Records": [good, bad]}, s3=self.s3, conn=object())
        self.assertEqual(out["batchItemFailures"], [{"itemIdentifier": "2"}])
        self.assertEqual(len(out["results"]), 1)

    def test_only_flagged_synthetic_records_are_promoted(self):
        rows = [{"timing_quality": "DERIVED", "quality_flags": ["synthetic_demo"]},
                {"timing_quality": "DERIVED", "quality_flags": []}]
        out = lambda_handler._promote_synthetic_measured(rows)
        self.assertEqual([r["timing_quality"] for r in out], ["MEASURED", "DERIVED"])
        with mock.patch.dict(os.environ, {"SYNTHETIC_AS_MEASURED": "false"}):
            self.assertEqual(lambda_handler._promote_synthetic_measured(rows)[0]["timing_quality"], "DERIVED")


class FakeCursor:
    def __init__(self):
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.statements.append((sql, params))

    def fetchall(self):
        return []          # no settled traces in the manifest lookup


class FakeConn:
    def __init__(self):
        self.cur, self.commits = FakeCursor(), 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1


class ScoreV2HandlerTests(unittest.TestCase):
    def setUp(self):
        self.rows = list(records())
        self.s3 = FakeS3({("raw", "normalized/uploads/demo.jsonl"): jsonl(self.rows)})
        env = mock.patch.dict(os.environ, {"ASSUME_SYNCHRONOUS": "true", "DATA_LABEL": "synthetic"})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("DIAGNOSIS_METHOD", None)
        os.environ.pop("REFERENCE_FRACTION", None)

    def test_v2_is_the_default_and_publishes_v2_rows(self):
        conn = FakeConn()
        out = lambda_handler.handler({"bucket": "raw", "key": "normalized/uploads/demo.jsonl"}, s3=self.s3, conn=conn)
        self.assertTrue(out["ok"])
        self.assertEqual(out["method"], "bottleneck-score-v2")
        self.assertGreater(out["published"], 0)
        self.assertEqual(out["reference"]["reference_traces"] + out["reference"]["scored_traces"], 40)
        inserts = [p for sql, p in conn.cur.statements if "INSERT" in sql]
        self.assertEqual(len(inserts), out["published"])
        self.assertTrue(all(p["algorithm_version"] == "bottleneck-score-v2" for p in inserts))
        self.assertTrue(all(p["confidence"] in ("LOW", "MEDIUM", "HIGH") for p in inserts))
        self.assertTrue(all(0 <= p["score"] <= 1 for p in inserts))
        self.assertEqual(conn.commits, 1)

    def test_reference_is_the_start_of_the_file_and_disjoint(self):
        reference, scored, info = lambda_handler.split_reference(self.rows, 0.3)
        ref_ids = {r["trace_id"] for r in reference}
        self.assertTrue(ref_ids)
        self.assertFalse(ref_ids & {r["trace_id"] for r in scored})
        last_ref = max(int(r["start_time_ns"]) for r in reference)
        self.assertLess(last_ref, int(info["cut_ns"]))
        self.assertEqual(len(reference) + len(scored), len(self.rows))
        with self.assertRaises(ValueError):
            lambda_handler.split_reference(self.rows, 1.5)

    def test_unknown_method_is_an_error(self):
        with mock.patch.dict(os.environ, {"DIAGNOSIS_METHOD": "v9"}):
            with self.assertRaises(ValueError):
                lambda_handler.handler({"bucket": "raw", "key": "normalized/uploads/demo.jsonl"}, s3=self.s3, conn=FakeConn())

    def test_v2_rows_flatten_signals_and_skip_unscored_services(self):
        from diagnosis import publisher_v2
        sig = lambda n, raw, base: {"normalized": n, "raw": raw, "baseline": base, "reason": None,
                                    "baseline_sample_count": 30, "sample_count": 20,
                                    "evidence": [{"trace_id": f"t{i}"} for i in range(9)]}
        scored = {"service": "payment", "service_name": "payment", "score": 0.7, "effective_weights": {"latency": 1.0},
                  "confidence": {"value": 0.9, "sample_support": 1.0, "signal_coverage": 1.0},
                  "signals": {"critical_path": sig(0.8, 2e8, 1e7), "latency": sig(0.6, 1e8, 5e7),
                              "prevalence": sig(0.4, 0.4, 0.0), "error": sig(None, None, None)}}
        unscored = dict(scored, service="cart", service_name="cart", score=None)
        window = {"run_id": "11111111-1111-4111-8111-111111111111", "window_start_ns": "0", "window_end_ns": "300000000000",
                  "root_operation": "POST /checkout", "rankings": [scored, unscored]}
        rows = publisher_v2.rows_for_window(FakeConn(), {"configuration_id": "c"}, window)
        self.assertEqual([r["service_name"] for r in rows], ["payment"])
        r = rows[0]
        self.assertEqual(r["confidence"], "HIGH")
        self.assertEqual(r["signals"]["critical_path_change_ms"], 200.0)
        self.assertIsNone(r["signals"]["error"])
        self.assertEqual(r["evidence"][0], {"name": "strongest_signal", "value": "critical_path"})
        self.assertEqual(len(r["evidence"][1]["value"]), publisher_v2.TOP_TRACES)


if __name__ == "__main__":
    unittest.main()
