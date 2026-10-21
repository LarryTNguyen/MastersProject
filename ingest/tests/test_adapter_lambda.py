import io
import json
import unittest

from traceingest.adapter_lambda import handler, output_key
from traceingest.spans import read_jsonl

from .helpers import fx, load


class FakeBody:
    def __init__(self, data):
        self.data = data

    def read(self):
        return self.data


class FakeS3:
    def __init__(self, objects):
        self.objects = dict(objects)
        self.puts = []

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise KeyError(Key)
        return {"Body": FakeBody(self.objects[(Bucket, Key)])}

    def put_object(self, Bucket, Key, Body, **kw):
        self.puts.append((Bucket, Key))
        self.objects[(Bucket, Key)] = Body


class AdapterLambdaTests(unittest.TestCase):
    def setUp(self):
        with open(fx("sqs_s3_event.json")) as f:
            self.event = json.load(f)
        with open(fx("otlp_small.json"), "rb") as f:
            otlp_bytes = f.read()
        self.s3 = FakeS3({("sta-raw-dev", "uploads/my trace file.json"): otlp_bytes,
                          ("sta-raw-dev", "uploads/broken.csv"): b"what,is,this\n1,2,3\n"})

    def test_batch(self):
        out = handler(self.event, s3=self.s3)
        # message 1 normalized, message 2 reported as failed, message 3 (S3 test event) skipped
        self.assertEqual(out["batchItemFailures"], [{"itemIdentifier": "msg-2"}])
        self.assertEqual(self.s3.puts, [("sta-raw-dev", "normalized/uploads/my trace file.jsonl")])
        body = self.s3.objects[("sta-raw-dev", "normalized/uploads/my trace file.jsonl")].decode()
        spans = list(read_jsonl(io.StringIO(body)))
        self.assertEqual(spans, load("otlp_small.json"))

    def test_idempotent(self):
        handler(self.event, s3=self.s3)
        first = self.s3.objects[("sta-raw-dev", "normalized/uploads/my trace file.jsonl")]
        handler(self.event, s3=self.s3)
        self.assertEqual(self.s3.objects[("sta-raw-dev", "normalized/uploads/my trace file.jsonl")], first)
        self.assertEqual(len(set(self.s3.puts)), 1)

    def test_output_key(self):
        self.assertEqual(output_key("live/2026/10/14/batch1.json", "normalized/"), "normalized/live/2026/10/14/batch1.jsonl")
        self.assertEqual(output_key("other/x.csv", "normalized"), "normalized/other/x.jsonl")


if __name__ == "__main__":
    unittest.main()
