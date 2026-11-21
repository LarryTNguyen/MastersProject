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
        env = mock.patch.dict(os.environ, {"ASSUME_SYNCHRONOUS": "true", "DATA_LABEL": "synthetic"})
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


if __name__ == "__main__":
    unittest.main()
