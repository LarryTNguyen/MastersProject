import base64
import json
import os
import unittest
from unittest import mock

from traceingest import adapter_lambda
from traceingest.collector_lambda import handler, raw_key

from .helpers import fx
from .test_adapter_lambda import FakeS3

ENV = {"RAW_BUCKET": "raw-bucket", "RAW_PREFIX": "live/", "INGEST_TOKEN": "s3cret"}
NOW = 1_795_000_000.0          # 2026-11-18 UTC


def url_event(body, token="s3cret", method="POST", path="/v1/traces", ctype="application/json", b64=False):
    """What a Lambda Function URL hands the handler (payload format 2.0)."""
    headers = {"content-type": ctype}
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    if b64:
        body = base64.b64encode(body.encode()).decode()
    return {"version": "2.0", "rawPath": path, "headers": headers, "body": body, "isBase64Encoded": b64,
            "requestContext": {"http": {"method": method, "path": path}}}


class CollectorTests(unittest.TestCase):
    def setUp(self):
        with open(fx("otlp_small.json")) as f:
            self.otlp = f.read()
        self.s3 = FakeS3({})
        patcher = mock.patch.dict(os.environ, ENV, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def call(self, event):
        return handler(event, s3=self.s3, now=lambda: NOW)

    def test_stores_the_batch_unchanged_and_answers_like_an_otlp_receiver(self):
        r = self.call(url_event(self.otlp))
        self.assertEqual(r["statusCode"], 200)
        self.assertEqual(json.loads(r["body"]), {"partialSuccess": {}})
        (bucket, key), = self.s3.puts
        self.assertEqual(bucket, "raw-bucket")
        self.assertTrue(key.startswith("live/2026/11/18/") and key.endswith(".json"), key)
        self.assertEqual(self.s3.objects[(bucket, key)], self.otlp.encode())

    def test_base64_bodies_are_decoded(self):
        self.assertEqual(self.call(url_event(self.otlp, b64=True))["statusCode"], 200)
        self.assertEqual(list(self.s3.objects.values()), [self.otlp.encode()])

    def test_wrong_or_missing_token_is_refused(self):
        for token in ("wrong", None, ""):
            self.assertEqual(self.call(url_event(self.otlp, token=token))["statusCode"], 401)
        self.assertEqual(self.s3.puts, [])

    def test_fails_closed_without_a_configured_token(self):
        with mock.patch.dict(os.environ, {"INGEST_TOKEN": ""}):
            self.assertEqual(self.call(url_event(self.otlp, token=""))["statusCode"], 401)

    def test_protobuf_wrong_path_wrong_method_empty_and_too_big(self):
        self.assertEqual(self.call(url_event(self.otlp, ctype="application/x-protobuf"))["statusCode"], 415)
        self.assertEqual(self.call(url_event(self.otlp, path="/admin"))["statusCode"], 404)
        self.assertEqual(self.call(url_event(self.otlp, method="GET"))["statusCode"], 405)
        self.assertEqual(self.call(url_event(""))["statusCode"], 400)
        with mock.patch.dict(os.environ, {"MAX_BYTES": "10"}):
            self.assertEqual(self.call(url_event(self.otlp))["statusCode"], 413)
        self.assertEqual(self.s3.puts, [])

    def test_root_path_and_charset_are_accepted(self):
        self.assertEqual(self.call(url_event(self.otlp, path="/", ctype="application/json; charset=utf-8"))["statusCode"], 200)

    def test_the_adapter_normalizes_what_the_collector_stored(self):
        self.call(url_event(self.otlp))
        (bucket, key), = self.s3.puts
        out = adapter_lambda.normalize_object(self.s3, bucket, key, bucket, "normalized/")
        self.assertGreater(out["spans"], 0)
        self.assertTrue(out["output"].startswith("s3://raw-bucket/normalized/live/2026/11/18/"))

    def test_raw_key_layout(self):
        self.assertEqual(raw_key("live", NOW, "abc"), "live/2026/11/18/abc.json")


if __name__ == "__main__":
    unittest.main()
