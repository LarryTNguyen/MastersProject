import json
import unittest

from app.canonical import is_canonical, span_from_any, span_from_record

RECORD = {
    "schema_version": "1.0.0", "run_id": "r", "identity_kind": "DATASET", "record_kind": "CALL_OBSERVATION",
    "trace_id": "T1", "span_id": "abc", "parent_span_id": None, "parent_relation": "ROOT",
    "service_namespace": "", "service_name": "svcA", "service_instance_id": None, "operation_name": "/checkout",
    "span_kind": "UNKNOWN", "start_time_ns": None, "end_time_ns": None, "duration_ns": "120000000",
    "time_domain": "UNKNOWN", "timing_quality": "DURATION_ONLY", "status": {"code": "UNKNOWN", "message": None},
    "attributes": {"alibaba.rpcid": {"type": "string", "value": "0"}, "n": {"type": "int", "value": "7"}},
    "received_at_unix_ns": "1700000005000000000",
    "source": {"format": "alibaba", "details": {"legacy_source": "alibaba", "replay_start_ns": "1000000000",
                                                "peer_service": "USER"}},
    "quality_flags": ["call_observation", "missing_interval"],
}


class CanonicalTests(unittest.TestCase):
    def test_alibaba_call_observation(self):
        s = span_from_record(RECORD)
        self.assertEqual((s["trace_id"], s["span_id"], s["service"], s["operation"]), ("T1", "abc", "svcA", "/checkout"))
        self.assertEqual(s["start_us"], 1_000_000)             # replay timestamp from provenance
        self.assertEqual(s["duration_us"], 120_000)
        self.assertEqual(s["status"], "unset")                  # UNKNOWN -> unset
        self.assertIsNone(s["kind"])
        self.assertEqual(s["peer_service"], "USER")
        self.assertEqual(s["source"], "alibaba")
        self.assertEqual(s["attributes"]["alibaba.rpcid"], "0")
        self.assertEqual(s["attributes"]["n"], 7)               # typed int back to int
        self.assertEqual(s["attributes"]["sta.received_us"], 1_700_000_005_000_000)
        self.assertEqual(s["attributes"]["sta.timing_quality"], "DURATION_ONLY")

    def test_otel_span(self):
        r = dict(RECORD, record_kind="SPAN", span_kind="SERVER", start_time_ns="2000000000", end_time_ns="2050000000",
                 duration_ns="50000000", status={"code": "ERROR", "message": "boom"}, parent_span_id="p1",
                 source={"format": "otlp", "details": {}}, received_at_unix_ns="0")
        s = span_from_record(r)
        self.assertEqual((s["start_us"], s["duration_us"], s["status"], s["kind"], s["parent_span_id"]),
                         (2_000_000, 50_000, "error", "server", "p1"))
        self.assertNotIn("sta.received_us", s["attributes"])    # "0" means unknown
        self.assertEqual(s["source"], "otlp")

    def test_old_format_passes_through(self):
        old = {"trace_id": "t", "span_id": "s", "parent_span_id": None, "service": "x", "operation": "y",
               "start_us": 1, "duration_us": 2, "status": "ok"}
        self.assertFalse(is_canonical(old))
        self.assertEqual(span_from_any(json.loads(json.dumps(old)))["service"], "x")
        self.assertEqual(span_from_any(old)["attributes"], {})


if __name__ == "__main__":
    unittest.main()
