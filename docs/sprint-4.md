# Sprint 4 implementation and runbook

The source schedule is `Fall Sprint Plan - Team MAL.docx` in the neighboring
`Masters_Project` folder. Sprint 4 (October 30–November 12) calls for AWS
end-to-end operation, upload wiring, natural tail episodes, a consistency-check
case study, score-spike flags, cost measurements, and a timed Jaeger comparison.
The document itself is not checked into this repository.

## Status

| Goal | Repository status |
|---|---|
| Slow-versus-normal and flame graph | Existing dashboard views and tests |
| Tail-episode investigation | New `python -m diagnosis` command, grouped by run, endpoint, and fixed time window |
| Case-study consistency check | JSON and Markdown reports, supporting trace IDs, input hashes, recorded thresholds, adjusted latency |
| Score-spike flag | Delta from the immediately preceding eligible window; first observation does not alert |
| S3 upload → SQS → adapter | Existing adapter and presigned-upload API |
| Normalized S3 → SQS → reconstruction | New Lambda handler with per-message failures and scheduled quiet-window settlement |
| Full AWS deployment and measured costs | Requires the team's AWS infrastructure, database connectivity, and billing measurements; not deployed or measured here |
| First real diagnosed episode | Tooling ready; synthetic validation does not count as a natural production episode |
| Timed Jaeger comparison | Existing `experiments/usability/protocol.md` and timing log; needs human sessions |

## Run locally

Use the commands in the root README for the synthetic demo. It produces 40 traces,
five with a 200 ms payment delay, and computes the ranking rather than loading a
hardcoded result. `episodes.json` records the full configuration and input SHA-256;
`case-study.md` contains the readable evidence; `rankings.json` is accepted by the
dashboard's JSONL store. No AWS or PostgreSQL is needed for this demo.

For real canonical JSONL, from the root with the same virtual environment:

```powershell
.\.venv\Scripts\python.exe -m diagnosis path/to/spans.jsonl --output data/investigation --assume-synchronous --window-seconds 300 --min-samples 10
```

The input can also be a directory of canonical `*.jsonl` files. Keep reports outside
the input directory for repeated real-data runs. Review input provenance before
calling the report a production case study. The CLI validates the canonical schema.
To view computed rankings, copy `rankings.json` next to the corresponding span files
and set `DATA_DIR` to that directory. Use a single run per dashboard JSONL dataset;
the diagnosis report isolates runs, but the legacy JSONL dashboard groups by trace ID.

Without `--assume-synchronous`, critical-path-dependent diagnosis is unavailable.
The model selects the last-finishing child and preceding non-overlapping children,
attributes intervening time to the parent, and recursively partitions the selected
intervals. It is a heuristic, not observed causality. Input must have connected,
conflict-free, measured UNIX intervals contained within parents. Duration-only
Alibaba observations are excluded, not assigned fabricated intervals.

Within each endpoint/window, slow means at least 1.5 times the median duration.
The defaults require 10 traces and at least 5 ms service excess. Each service's
critical-path time and share are compared with normal traces, including zeros when
the service is absent. Score is positive median service excess divided by median
trace excess, capped at 1. A service must also have increased critical-path share.
This score is descriptive, not a probability, and is not an implementation of FIRM.
Windows without sufficient evidence explicitly report no diagnosis.

The consistency check subtracts each slow trace's service time above the normal
median and compares adjusted latency to normal latency. It does not simulate a
deployment change, queueing effects, or a true counterfactual intervention.
An alert flags a score increase of at least 0.25 against the immediately preceding
300-second window for the same run and endpoint. Gaps or insufficient samples do
not create an alert baseline. Flags appear in the report; no notification is sent.

## PostgreSQL path

With Docker Desktop running, start PostgreSQL using `docker compose up -d postgres`.
From `reconstruction/`, using `..\.venv\Scripts\python.exe`:

```powershell
..\.venv\Scripts\python.exe -m trace_reconstruction init-run 00000000-0000-4000-8000-000000000001 --identity-kind OTEL
..\.venv\Scripts\python.exe -m trace_reconstruction ingest ../data/sprint4/spans.jsonl
..\.venv\Scripts\python.exe -m trace_reconstruction finish 00000000-0000-4000-8000-000000000001 --assume-synchronous
```

The worker stores root duration, observed self time, attributed estimated critical
path segments, and quality reasons using the existing revision guard. No schema
migration is needed. To view stored traces, from the root:

```powershell
$env:STORE = 'trace_v1'
$env:DATABASE_URL = 'postgresql://trace:trace@localhost:55432/trace'
$env:RUN_ID = '00000000-0000-4000-8000-000000000001'
.\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir dashboard --port 8000
```

The diagnosis CLI writes portable JSON first. To publish the same results into
the PostgreSQL-backed dashboard locally, run the publisher after reconstruction
has settled and analyzed the run:

```powershell
docker compose up -d postgres
& .\.venv\Scripts\python.exe -m reconstruction.trace_reconstruction init-run 00000000-0000-4000-8000-000000000001 --identity-kind OTEL
& .\.venv\Scripts\python.exe -m reconstruction.trace_reconstruction ingest data\sprint4\spans.jsonl
& .\.venv\Scripts\python.exe -m reconstruction.trace_reconstruction finish 00000000-0000-4000-8000-000000000001 --assume-synchronous
& .\.venv\Scripts\python.exe -m diagnosis data\sprint4\spans.jsonl --output data\sprint4 --assume-synchronous --synthetic
& .\.venv\Scripts\python.exe -m diagnosis.publisher data\sprint4\episodes.json
```

The publisher inserts one immutable `service_window_results` row per ranked
service, retaining the trace revision and reconstruction analysis IDs in
`input_manifest`. Repeating the same publication is safe because result IDs are
deterministic. The dashboard can then be started with `STORE=trace_v1` and the
local PostgreSQL DSN shown above.

## AWS integration boundary

The new handler is `trace_reconstruction.lambda_handler.handler`. Package
`trace_reconstruction/`, sibling `trace_schema/` (including its validator and JSON
schema), and Linux-compatible dependencies from `reconstruction/requirements.txt`.
AWS supplies boto3. Set `DATABASE_URL` from the team's secret configuration,
`NORMALIZED_PREFIX=normalized/`, and optionally `ASSUME_SYNCHRONOUS=true`.

Connect object-created events for `normalized/` to a dedicated SQS queue and this
handler. Enable `ReportBatchItemFailures`, configure a dead-letter queue, and give
the Lambda access only to that S3 prefix, its queue, and the existing database.
Configure database networking and connection limits for the team's deployment.
S3 version IDs are honored when provided. Delivery retries use the database's
first-observation-wins deduplication and revision conflict handling.

Also invoke this handler on a regular EventBridge schedule (`source=aws.events`)
so the final trace settles after its quiet window even if no new objects arrive.
One invocation processes at most 100 due/pending traces; size the polling cadence
and concurrency for the workload and measure freshness. Do not call `finish_run`
per object because a run may span many uploads. Raw and normalized notifications
must use distinct prefix filters to avoid loops. This change supplies the consumer
code, not an AWS infrastructure stack or proof of end-to-end cloud operation.

## Verification

```powershell
# From repository root
.\.venv\Scripts\python.exe -m unittest discover -s diagnosis/tests -t .
# Run each package's tests from its directory to avoid colliding `tests` packages:
Push-Location reconstruction
..\.venv\Scripts\python.exe -m unittest discover -s tests -t .
Pop-Location
Push-Location dashboard
..\.venv\Scripts\python.exe -m unittest discover -s tests -t .
Pop-Location
```

Tests cover interval overlap, unavailable timing, skew, conflicts, persistence
payloads, per-message retry results, scheduled settlement, known synthetic excess,
no-tail results, run isolation, ordering, sparse windows, and adjacent-window flags.
Real database tests require Docker/PostgreSQL; browser smoke tests require Playwright.
