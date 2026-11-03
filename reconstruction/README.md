# Trace reconstruction

This package implements the reconstruction and observed-timing boundary. It groups
canonical span records by `(run_id, trace_id)`, performs first-observation-wins
retry deduplication, reconstructs explicit/inferred parent relationships, and
publishes a revisioned topology and quality result after quiet-window, hard
timeout, or end-of-input settlement.

Self time subtracts the union of comparable child intervals, clipped to the
parent. Missing intervals, incomplete topology, and conflicts remain unavailable
with per-span reasons. `--assume-synchronous` enables a labeled last-finisher
critical-path estimate, with non-overlapping attributed segments and explicit
assumptions. The default leaves critical path unavailable: parent links alone
do not establish blocking dependencies. Worker versions are `timing-v1` and
`timing-v1-synchronous`, so existing topology-only analyses are recomputed.

See [the sprint 4 runbook](../docs/sprint-4.md) for episode diagnosis, the case-study
report, and the normalized S3/SQS consumer.

## Local setup

Start a fresh PostgreSQL database from the repository root:

```powershell
docker compose up -d postgres
```

The schema is installed only when the volume is created. During Sprint 3,
recreate the development database after a schema change:

```powershell
docker compose down -v
docker compose up -d postgres
```

Install dependencies and run tests from this directory:

```powershell
python -m pip install -r requirements.txt
python -m unittest discover -s tests -t .
```

`check_database.py --dsn <disposable-database-dsn>` performs an end-to-end
integration check using the real Python store and worker. It commits fixture
records, so it must only be pointed at a fresh disposable database.

The default DSN is:

```text
postgresql://trace:trace@localhost:55432/trace
```

## Commands

Inspect canonical JSONL without PostgreSQL:

```powershell
python -m trace_reconstruction inspect path/to/spans.jsonl
```

Create a run, ingest spans, run one settlement poll, and explicitly finish the
input:

```powershell
python -m trace_reconstruction init-run <run-uuid> --identity-kind OTEL
python -m trace_reconstruction ingest path/to/spans.jsonl
python -m trace_reconstruction poll
python -m trace_reconstruction finish <run-uuid>
```

For a long-running local consumer, use `poll --forever`. It checks once per
second by default. PostgreSQL compares the trace's current revision inside
`settle_trace`, so a late span or concurrent worker cannot settle a stale
revision. A late new span reopens the trace and makes the previous analysis
stale.

## Ownership boundaries

- Dataset adapters, including Alibaba, own source-specific caller/callee
  deduplication.
- PostgreSQL owns delivery retry deduplication, semantic conflicts, trace
  revisions, and lifecycle deadlines.
- `trace_reconstruction.topology` owns topology and quality classification.
- The worker publishes only results for the current settled revision.

Topology classifications are:

- `CONNECTED`: one root and every declared parent is present.
- `INCOMPLETE`: a declared parent or the root is missing.
- `INVALID`: multiple roots or a parent cycle.
- `UNKNOWN`: one or more parent relationships are unknown.

Inferred parents and clock-skew/out-of-bounds intervals remain quality evidence;
they do not by themselves make the topology invalid.
