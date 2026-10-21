# Serverless Trace Analyzer

The repository currently contains three connected components:

- [`ingest`](ingest/README.md) converts Alibaba, RCAEval, and OTLP inputs,
  replays traces, and normalizes uploads.
- [`trace_schema`](trace_schema/README.md) defines and validates the canonical
  reconstruction/storage contract and PostgreSQL schema.
- [`reconstruction`](reconstruction/README.md) groups and deduplicates spans,
  reconstructs parent topology, settles traces after a quiet window, stores
  revisioned quality results, and provides the Astronomy Shop smoke test.

Normalized JSONL from ingestion conforms to trace schema version 1. OTLP replay
remains a standards-compatible `ExportTraceServiceRequest` for collectors.

For local Sprint 3 development, [`compose.yaml`](compose.yaml) starts PostgreSQL
16 on port `55432` and applies the version 1 schema to a new database volume.
