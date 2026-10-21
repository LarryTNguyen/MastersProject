import io
import json
import unittest

from traceingest.canonical import NormalizationContext, from_record, to_record, write_jsonl

from .helpers import load


class CanonicalSchemaTests(unittest.TestCase):
    def setUp(self):
        self.context = NormalizationContext.for_source("fixture:test", run_id="00000000-0000-4000-8000-000000000001")

    def test_otlp_span_maps_to_measured_canonical_record(self):
        span = load("otlp_small.json")[0]
        record = to_record(span, self.context)
        self.assertEqual(record["schema_version"], "1.0.0")
        self.assertEqual(record["identity_kind"], "OTEL")
        self.assertEqual(record["record_kind"], "SPAN")
        self.assertEqual(record["time_domain"], "UNIX")
        self.assertEqual(record["timing_quality"], "MEASURED")
        self.assertEqual(int(record["end_time_ns"]) - int(record["start_time_ns"]),
                         int(record["duration_ns"]))
        self.assertEqual(record["resource"]["attributes"]["service.name"]["value"], span.service)

    def test_alibaba_is_duration_only_call_observation(self):
        span = load("alibaba_small.csv")[0]
        record = to_record(span, self.context)
        self.assertEqual(record["identity_kind"], "DATASET")
        self.assertEqual(record["record_kind"], "CALL_OBSERVATION")
        self.assertEqual(record["timing_quality"], "DURATION_ONLY")
        self.assertIsNone(record["start_time_ns"])
        self.assertIsNone(record["end_time_ns"])
        self.assertIsNotNone(record["duration_ns"])
        self.assertEqual(record["status"]["code"], "UNKNOWN")
        self.assertIn("replay_start_ns", record["source"]["details"])

    def test_canonical_jsonl_round_trip_keeps_replay_timing(self):
        spans = load("alibaba_small.csv")
        buf = io.StringIO()
        write_jsonl(spans, buf, self.context)
        records = [json.loads(line) for line in buf.getvalue().splitlines()]
        back = [from_record(record) for record in records]
        self.assertEqual([(s.trace_id, s.span_id, s.start_us, s.duration_us) for s in back],
                         [(s.trace_id, s.span_id, s.start_us, s.duration_us) for s in spans])
        self.assertTrue(all(s.status == "unknown" for s in back))

    def test_run_id_is_stable_for_same_source(self):
        first = NormalizationContext.for_source("s3://bucket/key")
        second = NormalizationContext.for_source("s3://bucket/key")
        other = NormalizationContext.for_source("s3://bucket/other")
        self.assertEqual(first.run_id, second.run_id)
        self.assertNotEqual(first.run_id, other.run_id)

    def test_invalid_context_is_rejected(self):
        with self.assertRaises(ValueError):
            NormalizationContext.for_source("fixture", run_id="not-a-uuid")


if __name__ == "__main__":
    unittest.main()
