# Sprint 5 evidence summary — November 3, 2026

The implementation adds versioned latency/error baselines, quality-gated
slow-trace prevalence, explicit unknown-status denominators, retained invalid
and conflicting records, and an experiment report with supporting trace/span
IDs. SQLite provides immutable portable baseline snapshots. Existing PostgreSQL
revision handling remains the live-analysis authority.

## Executed checks

| Check | Result |
| --- | --- |
| Diagnosis, including reliability and both report CLIs | 19 passed |
| Reconstruction, message retry, experiment verification | 34 passed |
| Ingestion adapters | 52 passed |
| Canonical schema and SQL syntax | 16 passed |
| Adapter-to-canonical contract check | 15 records passed |
| Experiment utilities | 15 passed; 1 live PostgreSQL test skipped |
| Dashboard, including all six Chromium browser tests | 73 passed; 8 live PostgreSQL tests skipped |

Total: **209 tests passed, 9 live-database tests skipped**. The separate adapter
contract check validated 15 records. Python compilation and `git diff --check`
also passed.

Installed declared schema/dashboard/reconstruction/experiment dependencies and
the optional Playwright browser-test dependency into the existing `.venv`.
The schema SQL parser check now runs with `pglast`. Experiment utility tests
emit pre-existing resource-handle warnings; their assertions passed.

## Synthetic plumbing evidence

The reproducible commands in `docs/sprint-5.md` generated
`data/sprint5-synthetic/{baseline,evaluation}.jsonl`, `reliability.json`, and
`baselines.sqlite`. These ignored runtime files are intentionally labeled
synthetic; the generator and tests are tracked implementation files.

- 20 reference traces; 20 evaluation traces.
- Endpoint reference median and p95: 100 ms; p99 unavailable with only 20 samples.
- Slow threshold: 150 ms; 5/20 slow traces, prevalence 25%.
- Payment error rate: 5/20 (25%), reference 0/20 (0%); anomaly flag true.
- Three baseline snapshots and three windows: endpoint, checkout, payment.
- The temporary fault-report unit fixture compares five healthy and five faulty
  traces and identifies payment. This is manufactured test input, not real
  application evidence, and is removed when the test ends.

## Acceptance gaps

Docker's daemon was unavailable both inside and outside the sandbox:
`dockerDesktopLinuxEngine` named pipe not found. Therefore the live PostgreSQL
integration checks and the Astronomy Shop fault experiment were not executed.
There is no genuine application capture, injection manifest, or first real
diagnosis report from this implementation session. Those deliverables remain
open; Sprint 5 must not be marked complete yet.

The enhanced PostgreSQL integration script contains assertions for duplicate
revision/quiet-window stability and stale-analysis invalidation after late
arrival, but these new database assertions are unverified on a live database.
Local unit tests and offline snapshots do not establish AWS deployment behavior.
The new reliability metrics are available through the CLI/JSON/SQLite outputs;
they are not yet exposed as new dashboard views.

Measured interval requirements, derived metrics, synthetic evidence, incomplete
topology, and unavailable results are separated explicitly. Critical-path
ranking remains a synchronous estimate; arithmetic latency adjustment and error
association are not causal intervention evidence.
