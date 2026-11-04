# Alibaba production traces through the local pipeline

Source: Alibaba cluster-trace v2021, `MSCallGraph_0.csv` (the first 5 minutes of the
release, 6.1M rows). Samples cut with `ingest/tools/alibaba_sample.py` from the
traces entering through the busiest entry service (`7695b43b…`, 69,653 traces):

| sample | rows | spans after merging caller/callee rows | traces | services | offered rate |
|---|---|---|---|---|---|
| 2% (`--fraction 0.02`) | 80,346 | 62,390 | 1,441 | 1,484 | about 210 spans/s |
| 5% (`--fraction 0.05`) | 202,166 | 154,264 | 3,564 | 2,151 | about 510 spans/s |

Both replayed at 1x (original timing) for the full 5 minutes through the local
collector, the adapter Lambda's handler, reconstruction on PostgreSQL 16 and the
dashboard's pipeline numbers, all in one process on a 2-core Linux machine.
Commands are in each json file and in `REPRODUCE.md`.

## 2%: freshness (N1)

| | p50 | p95 | p99 |
|---|---|---|---|
| staleness (span end → normalized, the Pipeline view's number) | 0.51 s | 1.19 s | 2.00 s |
| adapter lag (collector → normalized) | 0.16 s | 0.57 s | |
| settle (last span end → trace settled, includes the 5 s quiet window) | 6.09 s | 7.79 s | 10.09 s |

All 62,390 spans stored, no duplicates or conflicts, no adapter failures, and all
1,441 traces settled through the quiet window. Same picture as the RCAEval run
(`2026-11-02-local-freshness`, p95 1.10 s), now on real production call graphs with
about 1,500 services. The N1 target (p95 under 60 s) is met with a wide margin.

## 5%: where one process stops keeping up

At about 510 spans/s the single local process falls behind: staleness grows to p50
31 s and p95 76 s. The reconstruction worker only runs between long stretches of
ingest, so by the time it looks at most traces they are past the 60 s maximum open
time: 3,136 of 3,564 end by the hard timeout instead of the quiet window, and settle
time grows to minutes. Nothing is lost (all 154,264 spans stored, no duplicates), it
is only late.

Where the time goes (seconds of work in each stage over the run):

| stage | 2% (323 s run) | 5% (410 s run) |
|---|---|---|
| adapter (OTLP → canonical records) | 3 | 8 |
| ingest (validate each record, then `trace_v1.ingest_span`) | 132 | 365 |
| reconstruction worker (settle + topology) | 19 | 29 |

Ingest is the limit: about 2.1–2.4 ms per span, so one process tops out around
420–470 spans/s. Roughly half of that is the JSON Schema check run on every record
before it is stored (about 1.2 ms per record measured on its own).

What this means for AWS: there each raw batch is its own adapter invocation, so the
ingest work is spread over concurrent Lambdas instead of one loop, and the ceiling
above is a property of the local harness, not the design. It does tell us what to
watch in the Spring throughput campaign (N2): per-span ingest cost and database
round trips, with batched inserts and a cheaper validation path as the obvious
levers if the deployed numbers come out close to this.
