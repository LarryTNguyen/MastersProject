import io
import json
import unittest
from unittest.mock import MagicMock

from diagnosis import lambda_handler as lh
from diagnosis.lambda_handler import _promote_synthetic_measured, handler


def _span(**overrides):
    base = {
        "schema_version": "1.0.0",
        "run_id": "baf0b700-7910-5fcb-a776-37e042145d18",
        "identity_kind": "OTEL",
        "record_kind": "SPAN",
        "trace_id": "1",
        "span_id": "a",
        "parent_span_id": None,
        "parent_relation": "ROOT",
        "service_namespace": "shop",
        "service_name": "checkout",
        "operation_name": "POST /checkout",
        "start_time_ns": "1791936000000000000",
        "end_time_ns": "1791936000100000000",
        "duration_ns": "100000000",
        "time_domain": "UNIX",
        "timing_quality": "DERIVED",
        "quality_flags": ["synthetic_demo"],
        "status": {"code": "UNSET", "message": None},
        "attributes": {},
        "resource": {"attributes": {}, "schema_url": None},
        "scope": {"name": None, "version": None, "attributes": {}, "schema_url": None},
        "events": [],
        "links": [],
        "dropped_counts": {},
        "source": {"format": "otlp_json", "adapter_version": "t", "record_key": "a",
                   "raw_reference": None, "details": {}},
    }
    base.update(overrides)
    return base


class PromoteTests(unittest.TestCase):
    def test_synthetic_derived_becomes_measured(self):
        records = _promote_synthetic_measured([_span()])
        self.assertEqual(records[0]["timing_quality"], "MEASURED")


class HandlerTests(unittest.TestCase):
    def test_direct_invoke_publishes(self):
        s3 = MagicMock()
        s3.get_object.return_value = {"Body": io.BytesIO((json.dumps(_span()) + "\n").encode())}
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value.fetchall.return_value = []

        def fake_diagnose(records, **kwargs):
            return {
                "method": "tail-share-excess-v1",
                "configuration": kwargs,
                "windows": [{
                    "run_id": "baf0b700-7910-5fcb-a776-37e042145d18",
                    "window_start_ns": "1791936000000000000",
                    "window_end_ns": "1791936300000000000",
                    "root_operation": "POST /checkout",
                    "rankings": [{
                        "rank": 1, "service": "shop/payment", "service_name": "payment",
                        "score": 1.0, "excess_ms": 200.0, "supporting_trace_ids": ["t1"],
                    }],
                }],
                "excluded_traces": {},
            }

        real = lh.diagnose
        lh.diagnose = fake_diagnose
        try:
            response = handler(
                {"bucket": "b", "key": "normalized/uploads/spans.jsonl"},
                s3=s3, conn=conn,
            )
        finally:
            lh.diagnose = real
        self.assertTrue(response["ok"])
        self.assertEqual(response["published"], 1)
        self.assertEqual(response["windows"], 1)


if __name__ == "__main__":
    unittest.main()
