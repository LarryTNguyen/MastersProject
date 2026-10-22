"""Tests against the reconstruction step's real PostgreSQL schema.

Needs a throwaway database with trace_schema/schema.sql applied (compose.yaml
in the repo root gives you one):

    docker compose up -d postgres
    TEST_DATABASE_URL=postgresql://trace:trace@localhost:55432/trace python -m unittest tests.test_tracev1_store

Skipped when TEST_DATABASE_URL is not set. Uses its own run id and cleans up.
"""
import json
import os
import unittest
import uuid

from app.store import TraceQuery

DSN = os.environ.get("TEST_DATABASE_URL")
FX = os.path.join(os.path.dirname(__file__), "fixtures")
RUN = "22222222-2222-4222-8222-222222222222"


def canonical(span: dict, run_id: str) -> dict:
    """Minimal valid trace-schema v1 record from a fixture span."""
    start = span["start_us"] * 1000
    dur = span["duration_us"]
    return {
        "schema_version": "1.0.0", "run_id": run_id, "identity_kind": "DATASET", "record_kind": "SPAN",
        "trace_id": span["trace_id"], "span_id": span["span_id"], "parent_span_id": span["parent_span_id"],
        "parent_relation": "ROOT" if span["parent_span_id"] is None else "EXPLICIT",
        "service_namespace": "", "service_name": span["service"], "service_instance_id": None,
        "operation_name": span["operation"], "span_kind": "SERVER",
        "start_time_ns": None if dur is None else str(start),
        "end_time_ns": None if dur is None else str(start + dur * 1000),
        "duration_ns": None if dur is None else str(dur * 1000),
        "time_domain": "UNKNOWN" if dur is None else "UNIX",
        "timing_quality": "UNKNOWN" if dur is None else "DERIVED",
        "status": {"code": span["status"].upper(), "message": None}, "span_flags": None, "trace_state": None,
        "attributes": {k: {"type": "string", "value": str(v)} for k, v in span["attributes"].items()},
        "resource": {"attributes": {"service.name": {"type": "string", "value": span["service"]}}, "schema_url": None},
        "scope": {"name": None, "version": None, "attributes": {}, "schema_url": None},
        "events": [], "links": [],
        "dropped_counts": {"span_attributes": 0, "events": 0, "links": 0, "resource_attributes": 0, "scope_attributes": 0},
        "received_at_unix_ns": str(start + 5_000_000_000),
        "source": {"format": span["source"], "adapter_version": "test", "record_key": span["span_id"],
                   "raw_reference": None, "details": {"legacy_source": span["source"], "replay_start_ns": str(start)}},
        "quality_flags": [],
    }


@unittest.skipIf(not DSN, "TEST_DATABASE_URL not set")
class TraceV1StoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from app.tracev1_store import TraceV1Store
        cls.conn = psycopg.connect(DSN, autocommit=True)
        with open(os.path.join(FX, "spans.jsonl")) as f:
            spans = [json.loads(line) for line in f if line.strip()]
        cls._cleanup()
        with cls.conn.cursor() as cur:
            cur.execute("INSERT INTO trace_v1.ingestion_runs (run_id, application_name, environment, source_format, identity_kind) "
                        "VALUES (%s, 'test', 'test', 'otlp_json', 'DATASET')", (RUN,))
            for s in spans:
                cur.execute("SELECT trace_v1.ingest_span(%s::jsonb)", (json.dumps(canonical(s, RUN)),))
            # end the run and settle every trace, the way the worker's "finish" does
            cur.execute("UPDATE trace_v1.ingestion_runs SET ended_at = clock_timestamp() WHERE run_id = %s", (RUN,))
            cur.execute("SELECT trace_id, revision FROM trace_v1.traces WHERE run_id = %s", (RUN,))
            for tid, rev in cur.fetchall():
                cur.execute("SELECT trace_v1.settle_trace(%s, %s, %s, 'END_OF_INPUT')", (RUN, tid, rev))
            cur.execute("INSERT INTO trace_v1.service_window_results (result_id, run_id, window_start_ns, window_end_ns, "
                        "time_domain, endpoint_key, service_namespace, service_name, algorithm_version, configuration, "
                        "input_manifest, signals, score, confidence, evidence) VALUES "
                        "(%s, %s, 1700000000000000000, 1700000003000000000, 'UNIX', 'POST /checkout', '', 'paymentservice', "
                        "'v0', '{}', '[]', '{\"critical_path_ms_slow\": 280.0}', 0.9, 'HIGH', '[{\"name\": \"error_rate\", \"value\": 1.0}]'),"
                        "(%s, %s, 1700000000000000000, 1700000003000000000, 'UNIX', 'POST /checkout', '', 'checkoutservice', "
                        "'v0', '{}', '[]', '{}', 0.1, 'HIGH', '[]'),"
                        "(%s, %s, 1699999000000000000, 1700000000000000000, 'UNIX', '*', '', 'frontend', "
                        "'v0', '{}', '[]', '{}', NULL, 'UNAVAILABLE', '[]')",
                        (str(uuid.uuid4()), RUN, str(uuid.uuid4()), RUN, str(uuid.uuid4()), RUN))
        cls.store = TraceV1Store(cls.conn, run_id=RUN)

    @classmethod
    def _cleanup(cls):
        with cls.conn.cursor() as cur:
            for table in ("service_window_results", "trace_analyses", "spans", "traces", "ingestion_runs"):
                cur.execute(f"DELETE FROM trace_v1.{table} WHERE run_id = %s", (RUN,))

    @classmethod
    def tearDownClass(cls):
        cls._cleanup()
        cls.conn.close()

    def test_counts_and_runs(self):
        self.assertEqual(self.store.count_traces(), 3)
        self.assertIn(RUN, [r["run_id"] for r in self.store.runs()])

    def test_services(self):
        svcs = {s["service"]: s for s in self.store.services()}
        self.assertEqual(svcs["cartservice"]["span_count"], 3)
        self.assertEqual(svcs["paymentservice"]["error_rate"], 1.0)
        self.assertAlmostEqual(svcs["frontend"]["p99_ms"], 300.0)

    def test_search_and_filters(self):
        r = self.store.search_traces(TraceQuery(sort="duration_desc"))
        self.assertEqual([t["trace_id"] for t in r["items"]], ["t-slow", "t-fast", "t-inc"])
        self.assertEqual([t["trace_id"] for t in self.store.search_traces(TraceQuery(status="error"))["items"]], ["t-slow"])
        self.assertEqual(self.store.search_traces(TraceQuery(service="redis"))["total"], 2)
        self.assertEqual(self.store.search_traces(TraceQuery(min_duration_ms=100))["total"], 1)
        self.assertEqual([t["trace_id"] for t in self.store.search_traces(TraceQuery(complete=False))["items"]], ["t-inc"])
        self.assertEqual(r["items"][0]["state"], "SETTLED")

    def test_trace_detail_keeps_attributes_and_timing(self):
        d = self.store.get_trace("t-inc")
        self.assertEqual([s["span_id"] for s in d["spans"]], ["c1", "c2"])
        self.assertEqual(d["spans"][0]["attributes"]["alibaba.rpcid"], "0.1")
        self.assertIsNone(d["spans"][0]["duration_us"])
        self.assertEqual(d["spans"][1]["attributes"]["sta.timing_quality"], "DERIVED")
        self.assertFalse(d["summary"]["complete"])
        self.assertIsNone(self.store.get_trace("nope"))

    def test_rankings_from_window_results(self):
        ws = self.store.rankings()
        self.assertEqual(len(ws), 2)
        top = ws[0]
        self.assertEqual(top["endpoint"], "POST /checkout")
        self.assertEqual([r["service"] for r in top["rankings"]], ["paymentservice", "checkoutservice"])
        self.assertEqual(top["rankings"][0]["evidence"], {"critical_path_ms_slow": 280.0, "error_rate": 1.0})
        self.assertEqual((top["confidence_label"], top["confidence"]), ("HIGH", 0.9))
        self.assertIsNone(ws[1]["endpoint"])
        self.assertIn("no service clearly explains", ws[1]["note"])

    def test_analysis_views(self):
        c = self.store.compare(threshold_ms=100)
        self.assertEqual(c["services"][0]["service"], "paymentservice")
        self.assertEqual(self.store.flame("GET /cart")["count"], 1)
        p = self.store.pipeline(now_us=1_700_000_010_000_000)
        self.assertEqual(p["spans_with_stamps"], 9)       # received_at_unix_ns on every record


if __name__ == "__main__":
    unittest.main()
