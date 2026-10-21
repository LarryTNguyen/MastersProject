from __future__ import annotations

import argparse
import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path

from smoke import astronomy_shop

from tests.helpers import fixture


class AstronomySmokeTests(unittest.TestCase):
    def test_prepare_inject_and_clear_keep_upstream_config_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            demo = root / "demo"
            source = demo / "src" / "flagd" / "demo.flagd.json"
            source.parent.mkdir(parents=True)
            original = {"flags": {"paymentFailure": {
                "defaultVariant": "off",
                "variants": {"off": 0, "100%": 1},
            }}}
            source.write_text(json.dumps(original), encoding="utf-8")
            runtime = root / "runtime"
            arguments = argparse.Namespace(
                runtime=str(runtime), demo_dir=str(demo), demo_version="test-tag",
                run_id="00000000-0000-4000-8000-000000000099",
                minimum_healthy=5, minimum_faulty=5,
            )
            with contextlib.redirect_stdout(io.StringIO()):
                astronomy_shop.cmd_prepare(arguments)
                astronomy_shop.cmd_inject(argparse.Namespace(runtime=str(runtime)))
            active = json.loads((runtime / "demo.flagd.json").read_text(encoding="utf-8"))
            self.assertEqual(active["flags"]["paymentFailure"]["defaultVariant"], "100%")
            self.assertEqual(json.loads(source.read_text(encoding="utf-8")), original)
            with contextlib.redirect_stdout(io.StringIO()):
                astronomy_shop.cmd_clear(argparse.Namespace(runtime=str(runtime)))
            cleared = json.loads((runtime / "demo.flagd.json").read_text(encoding="utf-8"))
            self.assertEqual(cleared["flags"]["paymentFailure"]["defaultVariant"], "off")

    def test_verify_accepts_healthy_then_payment_error_traces(self):
        with tempfile.TemporaryDirectory() as directory:
            root_dir = Path(directory)
            runtime = root_dir / "runtime"
            runtime.mkdir()
            run_id = "00000000-0000-4000-8000-000000000099"
            injection_ns = 2_000_000_000
            manifest = {
                "run_id": run_id,
                "injected_at_unix_ns": str(injection_ns),
                "minimum_healthy_checkout_traces": 5,
                "minimum_faulty_checkout_traces": 5,
            }
            (runtime / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            root, payment, _ = fixture()
            records = []
            for index in range(10):
                trace_id = f"{index + 1:032x}"
                start = 1_000_000_000 if index < 5 else 3_000_000_000
                trace_root = copy.deepcopy(root)
                trace_payment = copy.deepcopy(payment)
                for record in (trace_root, trace_payment):
                    record["run_id"] = run_id
                    record["trace_id"] = trace_id
                trace_root["service_name"] = "checkout"
                trace_root["start_time_ns"] = str(start)
                trace_root["end_time_ns"] = str(start + 1_000_000_000)
                trace_root["duration_ns"] = "1000000000"
                trace_payment["start_time_ns"] = str(start + 100_000_000)
                trace_payment["end_time_ns"] = str(start + 800_000_000)
                trace_payment["duration_ns"] = "700000000"
                trace_payment["status"] = ({"code": "OK", "message": None} if index < 5
                                             else {"code": "ERROR", "message": "injected"})
                records.extend((trace_root, trace_payment))
            capture = root_dir / "capture.jsonl"
            capture.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                astronomy_shop.cmd_verify(argparse.Namespace(runtime=str(runtime), input=str(capture)))
            result = json.loads(output.getvalue())
            self.assertTrue(result["passed"])
            self.assertEqual(result["healthy_checkout_traces"], 5)
            self.assertEqual(result["faulty_checkout_traces"], 5)


if __name__ == "__main__":
    unittest.main()
