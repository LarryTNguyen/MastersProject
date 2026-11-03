"""Publish diagnosis windows into the dashboard's PostgreSQL store.

The diagnosis CLI intentionally writes portable JSON.  This module is the
local integration seam: it converts that JSON into the immutable
``trace_v1.service_window_results`` rows consumed by ``TraceV1Store``.
"""
from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path
from typing import Iterable


ALGORITHM_VERSION = "tail-share-excess-v1"


def _manifest(conn, run_id: str, window: dict) -> list[dict]:
    """Return settled trace provenance for one diagnosis window."""
    endpoint = window["root_operation"]
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT t.trace_id, t.revision, a.analysis_id
            FROM trace_v1.traces t
            JOIN trace_v1.trace_inventory i USING (run_id, trace_id)
            LEFT JOIN trace_v1.current_trace_analyses a USING (run_id, trace_id)
            WHERE t.run_id = %s::uuid
              AND t.state <> 'OPEN'
              AND i.observed_start_ns >= %s
              AND i.observed_start_ns < %s
              AND EXISTS (
                SELECT 1 FROM trace_v1.spans root
                WHERE root.run_id = t.run_id AND root.trace_id = t.trace_id
                  AND root.parent_relation = 'ROOT'
                  AND root.operation_name = %s
              )
            ORDER BY t.trace_id
            """,
            (run_id, int(window["window_start_ns"]), int(window["window_end_ns"]), endpoint),
        )
        return [
            {"trace_id": trace_id, "revision": revision,
             "analysis_id": str(analysis_id) if analysis_id else None}
            for trace_id, revision, analysis_id in cur.fetchall()
        ]


def rows_for_window(conn, result: dict, window: dict) -> list[dict]:
    """Build insert-ready rows for one ranked diagnosis window."""
    if not window.get("rankings"):
        return []
    run_id = window["run_id"]
    manifest = _manifest(conn, run_id, window)
    configuration = dict(result.get("configuration") or {})
    configuration.update({"method": result.get("method"), "data_label": result.get("data_label")})
    rows = []
    for ranking in window["rankings"]:
        evidence = []
        signals = {}
        for key, value in ranking.items():
            if key in {"rank", "service", "service_name", "score", "supporting_trace_ids"}:
                continue
            signals[key] = value
        evidence.append({"name": "supporting_trace_ids", "value": ranking["supporting_trace_ids"]})
        result_id = str(uuid.uuid5(
            uuid.NAMESPACE_URL,
            ":".join([run_id, str(window["window_start_ns"]), str(window["window_end_ns"]),
                       window["root_operation"], ranking["service"], ALGORITHM_VERSION]),
        ))
        rows.append({
            "result_id": result_id,
            "run_id": run_id,
            "window_start_ns": int(window["window_start_ns"]),
            "window_end_ns": int(window["window_end_ns"]),
            "time_domain": "UNIX",
            "endpoint_key": window["root_operation"],
            "service_namespace": ranking["service"].split("/", 1)[0] if "/" in ranking["service"] else "",
            "service_name": ranking["service_name"],
            "algorithm_version": ALGORITHM_VERSION,
            "configuration": configuration,
            "input_manifest": manifest,
            "signals": signals,
            "score": float(ranking["score"]),
            # This is a labeled heuristic estimate, so MEDIUM is deliberate.
            "confidence": "MEDIUM",
            "evidence": evidence,
        })
    return rows


def publish(conn, result: dict) -> int:
    """Insert all ranked windows and return the number of rows inserted.

    Result IDs are deterministic, making a repeat of the same publication a
    safe no-op.  New algorithm/configuration versions remain append-only.
    """
    rows = []
    for window in result.get("windows", []):
        rows.extend(rows_for_window(conn, result, window))
    with conn.cursor() as cur:
        for row in rows:
            cur.execute(
                """
                INSERT INTO trace_v1.service_window_results
                  (result_id, run_id, window_start_ns, window_end_ns, time_domain,
                   endpoint_key, service_namespace, service_name, algorithm_version,
                   configuration, input_manifest, signals, score, confidence, evidence)
                VALUES (%(result_id)s::uuid, %(run_id)s::uuid, %(window_start_ns)s,
                        %(window_end_ns)s, %(time_domain)s, %(endpoint_key)s,
                        %(service_namespace)s, %(service_name)s, %(algorithm_version)s,
                        %(configuration)s::jsonb, %(input_manifest)s::jsonb,
                        %(signals)s::jsonb, %(score)s, %(confidence)s, %(evidence)s::jsonb)
                ON CONFLICT (result_id) DO NOTHING
                """,
                {**row, "configuration": json.dumps(row["configuration"]),
                 "input_manifest": json.dumps(row["input_manifest"]),
                 "signals": json.dumps(row["signals"]), "evidence": json.dumps(row["evidence"])},
            )
    conn.commit()
    return len(rows)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result", type=Path, help="episodes.json produced by python -m diagnosis")
    parser.add_argument("--dsn", default="postgresql://trace:trace@localhost:55432/trace")
    args = parser.parse_args(argv)
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("Install reconstruction/requirements.txt to publish to PostgreSQL") from exc
    result = json.loads(args.result.read_text(encoding="utf-8"))
    with psycopg.connect(args.dsn) as conn:
        print(f"Published {publish(conn, result)} diagnosis rows")


if __name__ == "__main__":
    main()
