import json
import unittest
import urllib.error

from traceingest.sinks import HttpSink

from .helpers import FakeResponse, load


class HttpSinkTests(unittest.TestCase):
    def test_sends_otlp_with_token(self):
        seen = []

        def opener(req, timeout):
            seen.append(req)
            return FakeResponse(200)

        sink = HttpSink("http://localhost:4318", token="abc", opener=opener)
        sink.send(load("otlp_small.json"))
        req = seen[0]
        self.assertEqual(req.full_url, "http://localhost:4318/v1/traces")
        self.assertEqual(req.get_header("Authorization"), "Bearer abc")
        self.assertIn("resourceSpans", json.loads(req.data))
        self.assertEqual(sink.sent, 2)

    def test_retries_server_errors(self):
        calls = []

        def opener(req, timeout):
            calls.append(1)
            if len(calls) < 3:
                raise urllib.error.HTTPError(req.full_url, 503, "busy", {}, None)
            return FakeResponse(200)

        sink = HttpSink("https://x.lambda-url.us-west-2.on.aws/", opener=opener, sleep=lambda s: None)
        sink.send(load("otlp_small.json"))
        self.assertEqual(len(calls), 3)

    def test_no_retry_on_bad_token(self):
        calls = []

        def opener(req, timeout):
            calls.append(1)
            raise urllib.error.HTTPError(req.full_url, 401, "no", {}, None)

        sink = HttpSink("https://collector.example", opener=opener, sleep=lambda s: None)
        with self.assertRaises(urllib.error.HTTPError):
            sink.send(load("otlp_small.json"))
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
