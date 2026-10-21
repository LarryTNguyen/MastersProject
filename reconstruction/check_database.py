"""End-to-end check for PostgresStore and ReconstructionWorker.

Run only against a fresh disposable database. The check commits data because it
exercises the same transaction boundaries used by the application.
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

from trace_reconstruction.store import PostgresStore
from trace_reconstruction.worker import ReconstructionWorker


ROOT = Path(__file__).resolve().parents[1]


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def main(dsn: str) -> None:
    sys.path.insert(0, str(ROOT / "trace_schema"))
    from validate import validate_record

    records = json.loads((ROOT / "trace_schema" / "examples" / "parallel-trace.json").read_text(encoding="utf-8"))
    root, payment, inventory = records
    run_id, trace_id = root["run_id"], root["trace_id"]
    store = PostgresStore.connect(dsn, validator=validate_record)
    try:
        store.create_run(
            run_id,
            application_name="test-shop",
            environment="integration",
            source_format="otlp_json",
            identity_kind="OTEL",
            quiet_window_ms=5000,
            max_open_ms=60000,
        )
        counts = store.ingest_many([payment, inventory, root])
        check(counts["INSERTED"] == 3, "child-before-parent records were not inserted")

        duplicate = copy.deepcopy(payment)
        duplicate["received_at_unix_ns"] = str(int(duplicate["received_at_unix_ns"]) + 1)
        duplicate["source"]["raw_reference"] = "s3://retry/batch"
        check(store.ingest_span(duplicate).value == "DUPLICATE", "retry was not deduplicated")

        worker = ReconstructionWorker(store)
        first = worker.finish_run(run_id)
        check((first.settled, first.analyzed) == (1, 1), "end-of-input did not settle and analyze")
        with store.connection.cursor() as cursor:
            cursor.execute(
                "SELECT input_revision, topology, quality FROM trace_v1.current_trace_analyses "
                "WHERE run_id=%s::uuid AND trace_id=%s AND algorithm_version='topology-v1'",
                (run_id, trace_id),
            )
            revision, topology, quality = cursor.fetchone()
        check(revision == 3 and topology == "CONNECTED", "wrong initial topology analysis")
        check(not quality["has_conflicts"], "clean trace was marked conflicted")

        late = copy.deepcopy(payment)
        late["span_id"] = "0000000000000004"
        late["operation_name"] = "capture"
        late["source"]["record_key"] = late["span_id"]
        check(store.ingest_span(late).value == "INSERTED", "late span was not inserted")
        second = worker.finish_run(run_id)
        check((second.settled, second.analyzed) == (1, 1), "late revision was not reanalyzed")

        conflict = copy.deepcopy(payment)
        conflict["operation_name"] = "conflicting-operation"
        check(store.ingest_span(conflict).value == "CONFLICT", "semantic conflict was not detected")
        third = worker.finish_run(run_id)
        check((third.settled, third.analyzed) == (1, 1), "conflicted revision was not analyzed")
        with store.connection.cursor() as cursor:
            cursor.execute(
                "SELECT input_revision, quality FROM trace_v1.current_trace_analyses "
                "WHERE run_id=%s::uuid AND trace_id=%s AND algorithm_version='topology-v1'",
                (run_id, trace_id),
            )
            revision, quality = cursor.fetchone()
        check(revision == 5, "late span and first conflict did not create revisions 4 and 5")
        check(quality["has_conflicts"], "conflict flag was not published in quality")
        print("PASS: PostgresStore and ReconstructionWorker end-to-end lifecycle")
    finally:
        store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", required=True)
    args = parser.parse_args()
    main(args.dsn)
