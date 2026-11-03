import unittest
from unittest.mock import MagicMock

from diagnosis.publisher import rows_for_window


class PublisherTests(unittest.TestCase):
    def test_rows_match_postgres_contract_and_keep_provenance(self):
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [
            ("trace-1", 2, "analysis-1"),
            ("trace-2", 1, None),
        ]
        result = {
            "method": "tail-share-excess-v1",
            "data_label": "synthetic",
            "configuration": {"assume_synchronous": True},
        }
        window = {
            "run_id": "00000000-0000-4000-8000-000000000001",
            "window_start_ns": "1700000000000000000",
            "window_end_ns": "1700000300000000000",
            "root_operation": "POST /checkout",
            "rankings": [{
                "rank": 1, "service": "shop/payment", "service_name": "payment",
                "score": 0.8, "excess_ms": 200.0,
                "supporting_trace_ids": ["trace-1"],
            }],
        }
        rows = rows_for_window(conn, result, window)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["endpoint_key"], "POST /checkout")
        self.assertEqual(row["service_namespace"], "shop")
        self.assertEqual(row["service_name"], "payment")
        self.assertEqual(row["confidence"], "MEDIUM")
        self.assertEqual(row["input_manifest"][0]["analysis_id"], "analysis-1")
        self.assertIsNone(row["input_manifest"][1]["analysis_id"])
        self.assertEqual(row["signals"]["excess_ms"], 200.0)

    def test_empty_window_publishes_no_fake_service(self):
        conn = MagicMock()
        window = {"rankings": []}
        self.assertEqual(rows_for_window(conn, {}, window), [])


if __name__ == "__main__":
    unittest.main()
