# Serverless Trace Analyzer

The repository contains these components:

- [`ingest`](ingest/README.md) converts Alibaba, RCAEval, and OTLP inputs,
  replays traces, and normalizes uploads.
- [`trace_schema`](trace_schema/README.md) defines and validates the canonical
  reconstruction/storage contract and PostgreSQL schema.
- [`reconstruction`](reconstruction/README.md) groups and deduplicates spans,
  reconstructs parent topology, settles traces after a quiet window, stores
  revisioned quality results, and provides the Astronomy Shop smoke test.
- `diagnosis` finds tail episodes in canonical traces, ranks excess estimated
  critical-path time, and exports case-study evidence and dashboard rankings.
- [`dashboard`](dashboard/README.md) serves trace search, waterfalls, comparisons,
  flame graphs, and pipeline health.
- [`experiments`](experiments/README.md) runs benchmarks and summarizes measurements.

## Try it on Windows

From the repository root (Python 3.10+):

```powershell
py -m venv .venv                         # first setup only
.\.venv\Scripts\python.exe -m pip install -r dashboard/requirements.txt -r reconstruction/requirements.txt
.\.venv\Scripts\python.exe -m diagnosis.demo
.\.venv\Scripts\python.exe -m diagnosis data/sprint4/spans.jsonl --output data/sprint4 --assume-synchronous --synthetic
$env:STORE = 'jsonl'
$env:DATA_DIR = "$PWD/data/sprint4"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir dashboard --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000. Inspect **Bottlenecks**, select `POST /checkout` in
**Slow versus normal**, and open a slow trace's waterfall. The synthetic payment
delay is 200 ms; the generated report is `data/sprint4/case-study.md`.
Stop the server with Ctrl+C. The existing `.venv` can be reused without activation.

For the larger pre-existing UI demo, run
`.\.venv\Scripts\python.exe dashboard/tools/make_demo_data.py dashboard/data --now`
and set `$env:DATA_DIR = "$PWD/dashboard/data"` before starting the server.
Its rankings are illustrative, prewritten examples; the sprint 4 demo computes its rankings.

See [Sprint 4 scope, real-data commands, AWS wiring, and verification](docs/sprint-4.md).

Normalized JSONL from ingestion conforms to trace schema version 1. OTLP replay
remains a standards-compatible `ExportTraceServiceRequest` for collectors.

For local Sprint 3 development, [`compose.yaml`](compose.yaml) starts PostgreSQL
16 on port `55432` and applies the version 1 schema to a new database volume.
