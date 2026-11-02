# Local freshness run

- input: `data/rcaeval/re2ob_checkoutservice_cpu_1` at 1x for 180 s (one machine, local PostgreSQL 16)
- spans: 46409 in 172 batches; ingest {'INSERTED': 46409, 'DUPLICATE': 0, 'CONFLICT': 0}; adapter failures 0
- trace states at the end: {'SETTLED': 2827}

| | p50 | p95 | p99 |
|---|---|---|---|
| staleness (span end → normalized, what the Pipeline view shows) | 0.61 s | 1.10 s | 1.23 s |
| adapter lag (collector → normalized) | 0.15 s | 0.34 s | – |
| settle (last span end → trace settled) | 6.48 s | 8.16 s | 9.31 s |

N1 target: p95 staleness under 60 s → **met** locally. Settle time includes the 5 s quiet window by design. AWS adds the SQS hop and Lambda start-up, so these are a lower bound for the deployed pipeline.
