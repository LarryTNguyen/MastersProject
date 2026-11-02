"""Local pipeline harness: the pieces that don't need a database, plus one short
end-to-end run when TEST_DATABASE_URL points at a throwaway PostgreSQL with
trace_schema/schema.sql applied (docker compose up -d postgres)."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone

from experiments import local_pipeline as lp

DSN = os.environ.get("TEST_DATABASE_URL")
HERE = os.path.dirname(os.path.abspath(__file__))
OTLP_FIXTURE = os.path.join(lp.ROOT, "ingest", "tests", "fixtures", "otlp_small.json")


class HelperTests(unittest.TestCase):
    def test_folder_s3_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            s3 = lp.FolderS3(d)
            s3.put_object(Bucket="x", Key="raw/2026/11/02/a.json", Body=b"{}")
            self.assertEqual(s3.get_object(Bucket="x", Key="raw/2026/11/02/a.json")["Body"].read(), b"{}")

    def test_s3_event_has_the_shape_the_adapter_reads(self):
        when = datetime(2026, 11, 2, 12, 0, 0, tzinfo=timezone.utc)
        ev = lp.s3_event("raw/a.json", when)
        rec = json.loads(ev["Records"][0]["body"])["Records"][0]
        self.assertEqual(rec["s3"]["object"]["key"], "raw/a.json")
        self.assertEqual(rec["eventTime"], "2026-11-02T12:00:00Z")

    def test_adapter_runs_on_a_collector_batch(self):
        from traceingest import adapter_lambda
        with tempfile.TemporaryDirectory() as d:
            s3 = lp.FolderS3(d)
            with open(OTLP_FIXTURE, "rb") as f:
                s3.put_object(Bucket="local", Key="raw/b.json", Body=f.read())
            with contextlib.redirect_stdout(io.StringIO()):     # the handler prints a log line
                out = adapter_lambda.handler(lp.s3_event("raw/b.json", datetime.now(timezone.utc)), s3=s3)
            self.assertEqual(out["batchItemFailures"], [])
            rows = lp.read_jsonl(os.path.join(d, "normalized", "raw", "b.jsonl"))
            self.assertGreater(len(rows), 0)
            self.assertIn("sta.normalized_us", rows[0]["attributes"])
            self.assertNotEqual(rows[0]["received_at_unix_ns"], "0")

    def test_percentiles(self):
        self.assertIsNone(lp.pct([], 95))
        self.assertEqual(lp.pct([1, 2, 3, 4], 50), 2)
        self.assertEqual(lp.pct(list(range(1, 101)), 95), 95)


@unittest.skipIf(not DSN, "TEST_DATABASE_URL not set")
class EndToEndTests(unittest.TestCase):
    def test_short_run_lands_settled_traces_with_stamps(self):
        import argparse
        with tempfile.TemporaryDirectory() as d:
            args = argparse.Namespace(input=OTLP_FIXTURE, seconds=8, speed="max", settle_wait=8, dsn=DSN,
                                      out=d, keep_values=False)
            r = lp.run(args)
            self.assertGreater(r["spans"], 0)
            self.assertEqual(r["spans_with_stamps"], r["spans"])
            self.assertEqual(r["adapter_failures"], 0)
            self.assertIsNotNone(r["staleness_ms"]["p95"])
            self.assertTrue(os.path.exists(os.path.join(d, "summary.md")))
            self._cleanup(r["run_id"])

    def _cleanup(self, run_id):
        import psycopg
        with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
            for table in ("service_window_results", "trace_analyses", "spans", "traces", "ingestion_runs"):
                cur.execute(f"DELETE FROM trace_v1.{table} WHERE run_id = %s", (run_id,))


if __name__ == "__main__":
    unittest.main()
