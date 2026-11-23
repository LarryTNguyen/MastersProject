# V2 fixture comparison, November 19, 2026

Generated fixtures only: four delay cases and one error case. No weights were tuned.
This is a plumbing check, not evidence of real-world accuracy or a full RCAEval rerun.

| Method | acc@1 | acc@3 | MRR | No ranking |
|---|---:|---:|---:|---:|
| slowest_average | 0.000 | 1.000 | 0.367 | 0 |
| longest_span | 1.000 | 1.000 | 1.000 | 0 |
| critical_path_only | 1.000 | 1.000 | 1.000 | 0 |
| tail_share_excess | 0.800 | 0.800 | 0.800 | 1 |
| score_v2 | 1.000 | 1.000 | 1.000 | 0 |
| v1_measured | 0.000 | 0.000 | 0.000 | 5 |
| v2_measured | 0.000 | 0.000 | 0.000 | 5 |

The measured-only methods reject all five cases because their timing is DERIVED.
V1 misses the generated error case; v2 retains its error-rate signal. Longest-span
and critical-path-only also rank all five first, so this does not show superiority over those baselines.

The tiny included RCAEval fixture has two anomalous traces and no healthy reference.
V2 preserves candidates with null scores and records no_matching_reference; v1 has no ranking.
Its heuristic results are in small_fixture_summary.json. It is not a representative benchmark.

Validation: diagnosis 31 tests passed; experiments 37 run, 36 passed and one skipped
(PostgreSQL check: TEST_DATABASE_URL not set); reconstruction 34 passed, including generated verifier/report CLI evidence.
Initial default-encoding experiment run exposed an existing Windows cp1252 error in aws_freshness.py.
The documented -X utf8 invocation passes. No live PostgreSQL or Docker experiment was run.

Reproduce from the repository root:

```powershell
.\.venv\Scripts\python.exe -X utf8 -m experiments.tests.make_cases data/score-v2-fixtures
.\.venv\Scripts\python.exe -X utf8 -m experiments.run experiments/reported/2026-11-19-score-v2-fixtures/config.json
```

Full signal evidence is retained locally in experiments/results/score-v2-synthetic/rcaeval_evidence.json.
The smaller manifest.json records rankings, input hashes, algorithm/configuration for review.
See ../../../docs/score-v2.md for the formula, full comparison commands, collection procedure and limitations.
