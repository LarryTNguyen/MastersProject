"""PostgreSQL persistence for canonical spans and reconstructed topology."""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from .topology import IngestDisposition, ReconstructedTrace


@dataclass(frozen=True)
class TraceCandidate:
    run_id: str
    trace_id: str
    revision: int
    reason: str
    has_conflicts: bool = False


class StaleTraceError(RuntimeError):
    """The trace changed after a worker selected its revision."""


class PostgresStore:
    """Small DB-API wrapper; SQL functions remain the lifecycle authority."""

    def __init__(self, connection, validator: Optional[Callable[[dict], list[str]]] = None):
        self.connection = connection
        self.validator = validator

    @classmethod
    def connect(cls, dsn: str, validator=None) -> "PostgresStore":
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - exercised in installed environments
            raise RuntimeError("Install reconstruction/requirements.txt to use PostgreSQL") from exc
        return cls(psycopg.connect(dsn), validator=validator)

    def close(self) -> None:
        self.connection.close()

    def create_run(
        self,
        run_id: str,
        *,
        application_name: str,
        environment: str,
        source_format: str,
        identity_kind: str,
        dataset_name: Optional[str] = None,
        quiet_window_ms: int = 5000,
        max_open_ms: int = 60000,
        configuration: Optional[dict] = None,
    ) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO trace_v1.ingestion_runs
                  (run_id, application_name, environment, source_format, dataset_name,
                   identity_kind, quiet_window_ms, max_open_ms, configuration)
                VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (run_id) DO NOTHING
                """,
                (run_id, application_name, environment, source_format, dataset_name,
                 identity_kind, quiet_window_ms, max_open_ms, json.dumps(configuration or {})),
            )
        self.connection.commit()

    def ingest_span(self, record: dict) -> IngestDisposition:
        if self.validator:
            errors = self.validator(record)
            if errors:
                raise ValueError("invalid canonical span: " + "; ".join(errors))
        try:
            with self.connection.cursor() as cursor:
                cursor.execute("SELECT trace_v1.ingest_span(%s::jsonb)", (json.dumps(record),))
                disposition = cursor.fetchone()[0]
            self.connection.commit()
            return IngestDisposition(disposition)
        except Exception:
            self.connection.rollback()
            raise

    def ingest_many(self, records: Iterable[dict]) -> dict[str, int]:
        counts = {item.value: 0 for item in IngestDisposition}
        try:
            with self.connection.cursor() as cursor:
                for record in records:
                    if self.validator:
                        errors = self.validator(record)
                        if errors:
                            raise ValueError("invalid canonical span: " + "; ".join(errors))
                    cursor.execute("SELECT trace_v1.ingest_span(%s::jsonb)", (json.dumps(record),))
                    counts[cursor.fetchone()[0]] += 1
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return counts

    def due_traces(self, limit: int = 100) -> list[TraceCandidate]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT t.run_id::text, t.trace_id, t.revision,
                       CASE
                         WHEN t.opened_at + r.max_open_ms * interval '1 millisecond' <= clock_timestamp()
                           THEN 'HARD_TIMEOUT'
                         ELSE 'QUIET_WINDOW'
                       END AS reason,
                       t.has_conflicts
                FROM trace_v1.traces t
                JOIN trace_v1.ingestion_runs r USING (run_id)
                WHERE t.state = 'OPEN'
                  AND (t.last_changed_at + r.quiet_window_ms * interval '1 millisecond' <= clock_timestamp()
                    OR t.opened_at + r.max_open_ms * interval '1 millisecond' <= clock_timestamp())
                ORDER BY t.opened_at, t.trace_id
                LIMIT %s
                """,
                (limit,),
            )
            rows = cursor.fetchall()
        return [TraceCandidate(str(row[0]), row[1], int(row[2]), row[3], bool(row[4])) for row in rows]

    def pending_analyses(self, algorithm_version: str, limit: int = 100) -> list[TraceCandidate]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT t.run_id::text, t.trace_id, t.revision, t.settlement_reason, t.has_conflicts
                FROM trace_v1.traces t
                WHERE t.state <> 'OPEN'
                  AND NOT EXISTS (
                    SELECT 1 FROM trace_v1.trace_analyses a
                    WHERE a.run_id = t.run_id AND a.trace_id = t.trace_id
                      AND a.input_revision = t.revision AND a.algorithm_version = %s
                  )
                ORDER BY t.settled_at, t.trace_id
                LIMIT %s
                """,
                (algorithm_version, limit),
            )
            rows = cursor.fetchall()
        return [TraceCandidate(str(row[0]), row[1], int(row[2]), row[3], bool(row[4])) for row in rows]

    def settle(self, candidate: TraceCandidate) -> bool:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT trace_v1.settle_trace(%s::uuid, %s, %s, %s)",
                (candidate.run_id, candidate.trace_id, candidate.revision, candidate.reason),
            )
            settled = bool(cursor.fetchone()[0])
        self.connection.commit()
        return settled

    def fetch_trace(self, run_id: str, trace_id: str) -> tuple[list[dict], int, bool]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT revision, has_conflicts FROM trace_v1.traces WHERE run_id=%s::uuid AND trace_id=%s",
                (run_id, trace_id),
            )
            trace_row = cursor.fetchone()
            if trace_row is None:
                raise KeyError((run_id, trace_id))
            cursor.execute(
                """
                SELECT data FROM trace_v1.spans
                WHERE run_id=%s::uuid AND trace_id=%s
                ORDER BY (start_time_ns IS NULL), start_time_ns, span_id
                """,
                (run_id, trace_id),
            )
            records = [row[0] for row in cursor.fetchall()]
        return records, int(trace_row[0]), bool(trace_row[1])

    def publish_topology(
        self,
        trace: ReconstructedTrace,
        revision: int,
        *,
        algorithm_version: str,
        configuration: Optional[dict] = None,
    ) -> str:
        analysis_id = str(uuid.uuid4())
        starts = [int(span["start_time_ns"]) for span in trace.spans if span.get("start_time_ns") is not None]
        ends = [int(span["end_time_ns"]) for span in trace.spans if span.get("end_time_ns") is not None]
        observed_start = min(starts) if starts else None
        observed_end = max(ends) if ends else None
        try:
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO trace_v1.trace_analyses
                      (analysis_id, run_id, trace_id, input_revision, algorithm_version,
                       configuration, topology, root_span_id, observed_start_ns,
                       observed_end_ns, root_duration_ns, quality, span_features, critical_path)
                    VALUES (%s::uuid, %s::uuid, %s, %s, %s, %s::jsonb, %s, %s,
                            %s, %s, %s, %s::jsonb, %s::jsonb, NULL)
                    """,
                    (analysis_id, trace.run_id, trace.trace_id, revision, algorithm_version,
                     json.dumps(configuration or {}), trace.topology, trace.root_span_id,
                     observed_start, observed_end, None, json.dumps(trace.quality),
                     json.dumps(trace.span_features())),
                )
            self.connection.commit()
            return analysis_id
        except Exception as exc:
            self.connection.rollback()
            if "Analysis requires the current settled trace revision" in str(exc):
                raise StaleTraceError(str(exc)) from exc
            raise

    def end_run(self, run_id: str) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "UPDATE trace_v1.ingestion_runs SET ended_at=COALESCE(ended_at, clock_timestamp()) WHERE run_id=%s::uuid",
                (run_id,),
            )
        self.connection.commit()

    def open_traces_for_run(self, run_id: str) -> list[TraceCandidate]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT run_id::text, trace_id, revision, 'END_OF_INPUT', has_conflicts
                FROM trace_v1.traces WHERE run_id=%s::uuid AND state='OPEN'
                ORDER BY trace_id
                """,
                (run_id,),
            )
            rows = cursor.fetchall()
        return [TraceCandidate(str(row[0]), row[1], int(row[2]), row[3], bool(row[4])) for row in rows]
