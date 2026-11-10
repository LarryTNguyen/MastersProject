"""Freshness on AWS: the numbers from normalized files and from the dashboard API."""
import io
import json
import os
import tempfile
import unittest

from experiments import aws_freshness as af

T0 = 1_800_000_000_000_000          # microseconds


def span(i, end_us, received_us, normalized_us):
    return {"trace_id": f"t{i}", "span_id": f"s{i}", "parent_span_id": None, "service": "frontend",
            "operation": "GET /", "start_us": end_us - 1000, "duration_us": 1000, "status": "ok",
            "attributes": {"sta.received_us": received_us, "sta.normalized_us": normalized_us}}


class FilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.makedirs(os.path.join(self.tmp.name, "normalized", "live"))
        with open(os.path.join(self.tmp.name, "normalized", "live", "a.jsonl"), "w") as f:
            for i in range(100):               # span i is normalized (i + 1) * 100 ms after it ends
                end = T0 + i * 1_000_000
                f.write(json.dumps(span(i, end, end + 50_000, end + (i + 1) * 100_000)) + "\n")
            # an older run still in the bucket: normalized an hour earlier, and an hour stale
            f.write(json.dumps(span(999, T0 - 7_200_000_000, T0 - 3_600_000_100, T0 - 3_600_000_000)) + "\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_staleness_and_lag_from_the_ingest_stamps(self):
        s = af.from_files(self.tmp.name, since_us=T0 - 10_000_000)
        self.assertEqual(s["spans_with_stamps"], 100)
        self.assertAlmostEqual(s["staleness_ms"]["p50"], 5000, delta=150)
        self.assertAlmostEqual(s["staleness_ms"]["p95"], 9500, delta=150)
        self.assertGreater(s["lag_ms"]["p50"], 0)

    def test_since_drops_older_runs(self):
        everything = af.from_files(self.tmp.name)
        self.assertEqual(everything["spans_with_stamps"], 101)
        self.assertGreater(everything["staleness_ms"]["p99"], af.from_files(self.tmp.name, since_us=T0)["staleness_ms"]["p99"])

    def test_write_gives_the_runner_its_file(self):
        out = os.path.join(self.tmp.name, "out")
        af.write(af.from_files(self.tmp.name, since_us=T0 - 10_000_000), out, "cmd")
        with open(os.path.join(out, "freshness.json")) as f:
            data = json.load(f)
        self.assertEqual(data["where"], "AWS")
        self.assertIn("p95", data["staleness_ms"])
        with open(os.path.join(out, "summary.md")) as f:
            self.assertIn("**met**", f.read())

    def test_parse_since(self):
        self.assertEqual(af._parse_since("1970-01-01T00:00:01Z"), 1_000_000)
        self.assertIsNone(af._parse_since(None))


class DashboardTests(unittest.TestCase):
    def test_reads_the_pipeline_endpoint(self):
        class Resp(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False
        seen = []

        def opener(url, timeout=30):
            seen.append(url)
            return Resp(json.dumps({"spans": 10, "staleness_ms": {"p50": 900, "p95": 2000, "p99": 3000},
                                    "lag_ms": {"p50": 400, "p95": 800}}).encode())
        s = af.from_dashboard("https://x.lambda-url.us-west-2.on.aws/", opener=opener)
        self.assertEqual(seen, ["https://x.lambda-url.us-west-2.on.aws/api/pipeline?window_s=86400"])
        self.assertEqual(s["staleness_ms"]["p95"], 2000)


if __name__ == "__main__":
    unittest.main()
